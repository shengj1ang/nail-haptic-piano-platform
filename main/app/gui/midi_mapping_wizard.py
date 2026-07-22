"""PyQt (PySide6) wizard for step 2: press the keyboard's keys in order 1, 2, 3, ...
and record which MIDI note number each one sends.

Two pages:
  0. Connect - reminder to verify the keyboard's middle C sends note 60 (with a
     reference image/link), then pick and connect the MIDI port. A live "last
     key pressed" readout lets you test-press middle C right here and confirm
     it reads note 60 before doing anything else.
  1. Map keys - pick a profile, then step through its keys in the order
     setup_keyboard_wizard.py numbered them, pressing whichever physical key
     the camera view highlights as "next". No vision/detection happens on
     this page - the camera feed is only shown so you can see which key is
     highlighted.
"""

from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from ..camera import Camera
from ..config import Config
from ..keyboard.midi_mapping import MidiMapping, note_name
from ..keyboard.template import KeyboardTemplate
from ..keyboard.visualize import build_color_luts, draw_labels
from ..midi import MidiListener, list_input_ports
from ..profiles import DATA_DIR, list_profiles
from .image_view import ImageView, _default_max_size

HIGHLIGHT_COLOR = (0, 255, 255)  # bright - the key you should press next
MAPPED_COLOR = (0, 200, 0)  # already captured
UNMAPPED_COLOR = (60, 60, 60)  # not reached yet

MIDDLE_C_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "image" / "MiddleC-Keyboard.png"
MIDDLE_C_REFERENCE_URL = "https://www.phys.unsw.edu.au/jw/notes.html"

PAGE_CONNECT, PAGE_MAP = range(2)


class ConnectPage(QWizardPage):
    def __init__(self, wizard: "MidiMappingWizard"):
        super().__init__()
        self.setTitle("Step 2a - Connect the MIDI keyboard")
        self.setSubTitle("Verify middle C first, then connect the keyboard's MIDI port.")
        self._wizard = wizard

        hint = QLabel(
            "If this is a custom/non-standard MIDI keyboard, verify its middle C sends note 60 "
            "(check/adjust the keyboard's own octave/transpose setting first) - after connecting "
            "below, press middle C and confirm the readout says note 60 / C4."
        )
        hint.setWordWrap(True)

        image_label = QLabel()
        pixmap = QPixmap(str(MIDDLE_C_IMAGE))
        if not pixmap.isNull():
            image_label.setPixmap(pixmap)
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        reference_link = QLabel(
            f'Reference: <a href="{MIDDLE_C_REFERENCE_URL}">{MIDDLE_C_REFERENCE_URL}</a> '
            "(piano key layout / note number chart)"
        )
        reference_link.setOpenExternalLinks(True)

        self.port_combo = QComboBox()
        self.refresh_ports_btn = QPushButton("Refresh ports")
        self.connect_btn = QPushButton("Connect MIDI")
        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._connect_midi)

        self.status_label = QLabel("Not connected.")

        self.live_note_label = QLabel("Last key pressed: -")
        big_font = self.live_note_label.font()
        big_font.setPointSize(big_font.pointSize() + 4)
        self.live_note_label.setFont(big_font)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("MIDI port:"))
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.refresh_ports_btn)
        port_row.addWidget(self.connect_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(image_label)
        layout.addWidget(reference_link)
        layout.addLayout(port_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.live_note_label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_midi)

    def initializePage(self) -> None:
        self._refresh_ports()

    def set_polling(self, active: bool) -> None:
        if active:
            self._timer.start(33)
        else:
            self._timer.stop()

    def _refresh_ports(self) -> None:
        ports = list_input_ports()
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if self._wizard.cfg.midi.port_name in ports:
            self.port_combo.setCurrentText(self._wizard.cfg.midi.port_name)

    def _connect_midi(self) -> None:
        if self._wizard.midi is not None:
            self._wizard.midi.close()
            self._wizard.midi = None

        port_name = self.port_combo.currentText() or None
        try:
            midi = MidiListener(port_name)
        except RuntimeError as e:
            QMessageBox.warning(self, "MIDI connection failed", str(e))
            return

        self._wizard.midi = midi
        self._wizard.cfg.midi.port_name = midi.port_name
        self._wizard.cfg.save()
        self.status_label.setText(f"Connected to '{midi.port_name}'.")
        self.completeChanged.emit()

    def _poll_midi(self) -> None:
        if self._wizard.midi is None:
            return
        for event in self._wizard.midi.pop_events():
            self.live_note_label.setText(f"Last key pressed: note {event.note} ({note_name(event.note)})")

    def isComplete(self) -> bool:
        return self._wizard.midi is not None


class MapKeysPage(QWizardPage):
    def __init__(self, wizard: "MidiMappingWizard"):
        super().__init__()
        self.setTitle("Step 2b - Map keys to MIDI notes")
        self.setSubTitle("Press the highlighted key on the physical keyboard, in order.")
        self._wizard = wizard

        self.template = None
        self.luts = None
        self.mapping: dict = {}
        self.order: list = []
        self.pos = 0

        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._load_profile)

        # This page stacks a profile row above the video and progress/status/button
        # rows below it - more surrounding chrome than ImageView's default sizing
        # assumes, so ask for a shorter (but still full-width) box to make sure the
        # whole frame stays visible instead of getting clipped by the wizard's fixed
        # window size.
        default_w, default_h = _default_max_size()
        self.view = ImageView(max_size=(default_w, int(default_h * 0.75)))
        self.progress_label = QLabel("")
        self.status_label = QLabel("")

        self.undo_btn = QPushButton("Undo last")
        self.skip_btn = QPushButton("Skip this key")
        self.reset_btn = QPushButton("Reset all")
        self.save_btn = QPushButton("Save mapping")
        self.undo_btn.clicked.connect(self._undo)
        self.skip_btn.clicked.connect(self._skip)
        self.reset_btn.clicked.connect(self._reset)
        self.save_btn.clicked.connect(self._save)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        profile_row.addWidget(self.profile_combo, 1)

        info_row = QHBoxLayout()
        info_row.addWidget(self.progress_label, 1)
        info_row.addWidget(self.status_label, 1)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.undo_btn)
        btn_row.addWidget(self.skip_btn)
        btn_row.addWidget(self.reset_btn)
        btn_row.addWidget(self.save_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(profile_row)
        layout.addWidget(self.view)
        layout.addLayout(info_row)
        layout.addLayout(btn_row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def initializePage(self) -> None:
        self._refresh_profiles()

    def set_polling(self, active: bool) -> None:
        if active:
            self._timer.start(33)
        else:
            self._timer.stop()

    # ------------------------------------------------------------------
    # Profile handling
    # ------------------------------------------------------------------

    def _refresh_profiles(self) -> None:
        profiles = list_profiles(DATA_DIR)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        self.profile_combo.blockSignals(False)

        if not profiles:
            self.status_label.setText("No profiles found under data/keyboard-profile/. Run setup_keyboard_wizard.py first.")
            return

        target = self._wizard.cfg.active_keyboard_profile if self._wizard.cfg.active_keyboard_profile in profiles else profiles[0]
        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, name: str) -> None:
        if not name:
            return

        path = DATA_DIR / name / "keyboard_template.json"
        if not path.exists():
            self.template = None
            self.status_label.setText(f"Profile '{name}' has no template.")
            return

        self.template = KeyboardTemplate.load(path)
        self.luts = build_color_luts(len(self.template.keys))
        self.order = list(range(len(self.template.keys)))
        self.mapping = {}
        self.pos = 0
        self._update_progress()

    # ------------------------------------------------------------------
    # Stepping through keys
    # ------------------------------------------------------------------

    def _current_key_id(self):
        return self.order[self.pos] if self.pos < len(self.order) else None

    def _tick(self) -> None:
        frame = self._wizard.camera.read()
        if frame is None:
            return

        if self.template is not None and frame.shape[:2] == self.template.key_map.shape[:2]:
            self._draw_overlay(frame)

        self.view.set_frame(frame)

        if self._wizard.midi is not None:
            for event in self._wizard.midi.pop_events():
                self._on_note(event.note)

    def _draw_overlay(self, frame: np.ndarray) -> None:
        key_map = self.template.key_map
        current_id = self._current_key_id()

        tint = np.zeros_like(frame)
        for kid in np.unique(key_map):
            if kid == 0:
                continue
            key_id = int(kid) - 1
            if key_id == current_id:
                color = HIGHLIGHT_COLOR
            elif key_id in self.mapping:
                color = MAPPED_COLOR
            else:
                color = UNMAPPED_COLOR
            tint[key_map == kid] = color

        alpha = 0.5
        blended = cv2.addWeighted(frame, 1 - alpha, tint, alpha, 0)
        mask = key_map > 0
        frame[mask] = blended[mask]

        draw_labels(frame, key_map, len(self.template.keys))

    def _on_note(self, note: int) -> None:
        key_id = self._current_key_id()
        if key_id is None:
            self.status_label.setText(f"All keys mapped - note {note} ignored.")
            return

        self.mapping[key_id] = note
        self.status_label.setText(f"Key {key_id + 1} -> MIDI {note} ({note_name(note)})")
        self.pos += 1
        self._update_progress()

    def _undo(self) -> None:
        if self.pos == 0:
            return
        self.pos -= 1
        self.mapping.pop(self.order[self.pos], None)
        self._update_progress()

    def _skip(self) -> None:
        if self._current_key_id() is None:
            return
        self.pos += 1
        self._update_progress()

    def _reset(self) -> None:
        self.mapping = {}
        self.pos = 0
        self._update_progress()

    def _update_progress(self) -> None:
        total = len(self.order)
        mapped = len(self.mapping)
        current_id = self._current_key_id()

        if current_id is None:
            self.progress_label.setText(f"{mapped}/{total} keys mapped - all done, ready to save.")
        else:
            self.progress_label.setText(f"{mapped}/{total} keys mapped - press key #{current_id + 1} now.")

    def _save(self) -> None:
        if not self.mapping:
            QMessageBox.warning(self, "Nothing to save", "No keys have been mapped yet.")
            return

        keyboard_profile_name = self.profile_combo.currentText()
        path = DATA_DIR / keyboard_profile_name / "midi_mapping.json"
        port_name = self._wizard.midi.port_name if self._wizard.midi else self._wizard.cfg.midi.port_name

        mapping = MidiMapping(port_name=port_name, key_to_note=dict(self.mapping))
        mapping.save(path)

        QMessageBox.information(self, "Saved", f"Saved {len(self.mapping)} key-to-note mappings to {path}")


class MidiMappingWizard(QWizard):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("MIDI Key Mapping Wizard")
        self.setOptions(QWizard.WizardOption.NoBackButtonOnLastPage)

        self.cfg = cfg
        self.camera = Camera(cfg.camera)
        self.midi: MidiListener | None = None

        self.connect_page = ConnectPage(self)
        self.map_page = MapKeysPage(self)
        self.setPage(PAGE_CONNECT, self.connect_page)
        self.setPage(PAGE_MAP, self.map_page)
        self.setStartId(PAGE_CONNECT)

        self.currentIdChanged.connect(self._on_page_changed)

    def _on_page_changed(self, page_id: int) -> None:
        self.connect_page.set_polling(page_id == PAGE_CONNECT)
        self.map_page.set_polling(page_id == PAGE_MAP)

    def closeEvent(self, event) -> None:
        self.connect_page.set_polling(False)
        self.map_page.set_polling(False)
        self.camera.release()
        if self.midi is not None:
            self.midi.close()
        super().closeEvent(event)
