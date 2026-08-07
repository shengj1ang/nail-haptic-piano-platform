"""Initial-Setup-equivalent MIDI mapping for Tele-training.

The two pages intentionally mirror ``app.gui.midi_mapping_wizard``:

  connect + verify middle C -> map keys over a highlighted live camera view

Only ownership and saving differ.  This copy uses the camera selected by the
Tele-training setup, lists only profiles created by that setup, writes through
``setup_store.writable_profile_dir()``, and never writes ``config.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
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

from app.camera import Camera
from app.config import CameraConfig, Config
from app.gui.image_view import ImageView, _default_max_size
from app.keyboard.midi_mapping import MidiMapping, note_name
from app.keyboard.template import KeyboardTemplate
from app.keyboard.visualize import draw_labels
from app.midi import MidiListener, ambiguous_port_names, list_input_ports

from .setup_store import (
    MIDI_MAPPING_FILENAME,
    TEMPLATE_FILENAME,
    SetupError,
    list_remote_profiles,
    writable_profile_dir,
)

HIGHLIGHT_COLOR = (0, 255, 255)
MAPPED_COLOR = (0, 200, 0)
UNMAPPED_COLOR = (60, 60, 60)

MIDDLE_C_IMAGE = (
    Path(__file__).resolve().parent.parent / "app" / "assets" / "image" / "MiddleC-Keyboard.png"
)
MIDDLE_C_REFERENCE_URL = "https://www.phys.unsw.edu.au/jw/notes.html"

PAGE_CONNECT, PAGE_MAP = range(2)


class ConnectPage(QWizardPage):
    def __init__(self, wizard: "RemoteMidiMappingWizard"):
        super().__init__()
        self.setTitle("Step 3a - Connect the MIDI keyboard")
        self.setSubTitle("Verify middle C first, then connect the keyboard's MIDI port.")
        self._wizard = wizard

        hint = QLabel(
            "If this is a custom/non-standard MIDI keyboard, verify its middle C sends note 60 "
            "(check/adjust the keyboard's own octave/transpose setting first) - after connecting "
            "below, press middle C and confirm the readout says note 60 / C4."
        )
        hint.setWordWrap(True)

        self.reference_image_label = QLabel()
        pixmap = QPixmap(str(MIDDLE_C_IMAGE))
        if not pixmap.isNull():
            self.reference_image_label.setPixmap(pixmap)
        self.reference_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        reference_link = QLabel(
            f'<a href="{MIDDLE_C_REFERENCE_URL}">{MIDDLE_C_REFERENCE_URL}</a> '
            "(piano key layout / note number chart)"
        )
        reference_link.setOpenExternalLinks(True)

        self.port_combo = QComboBox()
        self.refresh_ports_btn = QPushButton("Refresh ports")
        self.connect_btn = QPushButton("Connect MIDI")
        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._connect_midi)

        self.port_hint = QLabel("")
        self.port_hint.setWordWrap(True)
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
        layout.addWidget(self.reference_image_label)
        layout.addWidget(reference_link)
        layout.addLayout(port_row)
        layout.addWidget(self.port_hint)
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
        current = self.port_combo.currentText() or (self._wizard.preferred_port_name or "")
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if current in ports:
            self.port_combo.setCurrentText(current)

        duplicates = ambiguous_port_names()
        if duplicates:
            names = ", ".join(repr(name) for name in duplicates)
            self.port_hint.setText(
                f"More than one instrument reports {names}. The #1/#2 numbers follow this machine's "
                "enumeration order and can change when a keyboard is replugged - verify this is the "
                "keyboard in front of the person being set up."
            )
        else:
            self.port_hint.setText("")
        self.port_hint.setVisible(bool(duplicates))

    def _connect_midi(self) -> None:
        self._wizard.release_midi()
        port_name = self.port_combo.currentText() or None
        try:
            midi = MidiListener(port_name)
        except RuntimeError as exc:
            QMessageBox.warning(self, "MIDI connection failed", str(exc))
            return

        self._wizard.midi = midi
        self._wizard.port_name = midi.port_name
        self.status_label.setText(f"Connected to '{midi.port_name}'.")
        self.completeChanged.emit()

    def _poll_midi(self) -> None:
        if self._wizard.midi is None:
            return
        for event in self._wizard.midi.pop_events():
            self.live_note_label.setText(
                f"Last key pressed: note {event.note} ({note_name(event.note)})"
            )

    def isComplete(self) -> bool:
        return self._wizard.midi is not None


class MapKeysPage(QWizardPage):
    def __init__(self, wizard: "RemoteMidiMappingWizard"):
        super().__init__()
        self.setTitle("Step 3b - Map keys to MIDI notes")
        self.setSubTitle("Press the highlighted key on the physical keyboard, in order.")
        self._wizard = wizard

        self.template: KeyboardTemplate | None = None
        self.mapping: dict[int, int] = {}
        self.order: list[int] = []
        self.pos = 0

        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._load_profile)

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

        button_row = QHBoxLayout()
        button_row.addWidget(self.undo_btn)
        button_row.addWidget(self.skip_btn)
        button_row.addWidget(self.reset_btn)
        button_row.addWidget(self.save_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(profile_row)
        layout.addWidget(self.view)
        layout.addLayout(info_row)
        layout.addLayout(button_row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def initializePage(self) -> None:
        self._refresh_profiles()

    def set_polling(self, active: bool) -> None:
        if active:
            self._timer.start(33)
        else:
            self._timer.stop()

    def _refresh_profiles(self) -> None:
        profiles = list_remote_profiles(self._wizard.profile_data_dir)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        self.profile_combo.blockSignals(False)

        if not profiles:
            self.template = None
            self.status_label.setText(
                "No Tele-training profiles found. Complete step 2 calibration first."
            )
            return

        target = (
            self._wizard.preferred_profile_name
            if self._wizard.preferred_profile_name in profiles
            else profiles[0]
        )
        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, name: str) -> None:
        if not name:
            return
        try:
            directory = writable_profile_dir(name, self._wizard.profile_data_dir)
        except SetupError as exc:
            self.template = None
            self.status_label.setText(str(exc))
            return

        path = directory / TEMPLATE_FILENAME
        if not path.exists():
            self.template = None
            self.status_label.setText(f"Profile '{name}' has no template.")
            return

        self.template = KeyboardTemplate.load(path)
        self.order = list(range(len(self.template.keys)))
        self.mapping = {}
        self.pos = 0
        self._wizard.profile_name = name
        self._update_progress()

    def _current_key_id(self):
        return self.order[self.pos] if self.pos < len(self.order) else None

    def _tick(self) -> None:
        frame = self._wizard.camera.read()
        if frame is None:
            return

        if self.template is not None and frame.shape[:2] == self.template.key_map.shape[:2]:
            self._draw_overlay(frame)
        elif self.template is not None:
            fh, fw = frame.shape[:2]
            th, tw = self.template.key_map.shape[:2]
            self.status_label.setText(
                f"Camera frame is {fw} x {fh}, but this profile is {tw} x {th}; "
                "showing the camera without a resized mask."
            )

        self.view.set_frame(frame)

        if self._wizard.midi is not None:
            for event in self._wizard.midi.pop_events():
                self._on_note(event.note)

    def _draw_overlay(self, frame: np.ndarray) -> None:
        key_map = self.template.key_map
        current_id = self._current_key_id()

        tint = np.zeros_like(frame)
        for key_value in np.unique(key_map):
            if key_value == 0:
                continue
            key_id = int(key_value) - 1
            if key_id == current_id:
                color = HIGHLIGHT_COLOR
            elif key_id in self.mapping:
                color = MAPPED_COLOR
            else:
                color = UNMAPPED_COLOR
            tint[key_map == key_value] = color

        blended = cv2.addWeighted(frame, 0.5, tint, 0.5, 0)
        mask = key_map > 0
        frame[mask] = blended[mask]
        draw_labels(frame, key_map, len(self.template.keys))

    def _on_note(self, note: int) -> None:
        key_id = self._current_key_id()
        if key_id is None:
            self.status_label.setText(f"All keys mapped - note {note} ignored.")
            return

        self.mapping[key_id] = int(note)
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
            self.progress_label.setText(
                f"{mapped}/{total} keys mapped - press key #{current_id + 1} now."
            )

    def _save(self) -> None:
        if not self.mapping:
            QMessageBox.warning(self, "Nothing to save", "No keys have been mapped yet.")
            return

        profile_name = self.profile_combo.currentText()
        try:
            directory = writable_profile_dir(profile_name, self._wizard.profile_data_dir)
        except SetupError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return

        mapping = MidiMapping(
            port_name=self._wizard.port_name,
            key_to_note=dict(self.mapping),
        )
        path = directory / MIDI_MAPPING_FILENAME
        mapping.save(path)

        self._wizard.profile_name = profile_name
        self._wizard.saved_mapping = dict(self.mapping)
        self._wizard.mappingSaved.emit(
            profile_name,
            len(self.mapping),
            self._wizard.port_name or "",
        )
        QMessageBox.information(
            self,
            "Saved",
            f"Saved {len(self.mapping)} key-to-note mappings to {path}\n"
            "No config.json setting or non-Tele-training profile was changed.",
        )


class RemoteMidiMappingWizard(QWizard):
    """Initial Setup's visual MIDI mapping flow with isolated saving."""

    mappingSaved = Signal(str, int, str)

    def __init__(
        self,
        cfg: Config,
        camera_config: CameraConfig,
        profile_data_dir: Path,
        preferred_profile_name: str = "",
        preferred_port_name: str | None = None,
        camera_factory: Callable[[CameraConfig], Camera] = Camera,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Tele-training MIDI Key Mapping Wizard")
        self.setOptions(QWizard.WizardOption.NoBackButtonOnLastPage)

        # cfg is read-only; it is retained only for parity with the Initial
        # Setup flow and the shared calibration/mapping defaults it exposes.
        self.cfg = cfg
        self.camera_config = CameraConfig(
            index=camera_config.index,
            width=camera_config.width,
            height=camera_config.height,
            fps=camera_config.fps,
            flip_vertical=camera_config.flip_vertical,
            flip_horizontal=camera_config.flip_horizontal,
        )
        self.profile_data_dir = Path(profile_data_dir)
        self.preferred_profile_name = preferred_profile_name
        self.preferred_port_name = preferred_port_name
        self.profile_name = preferred_profile_name
        self.port_name = preferred_port_name
        self.saved_mapping: dict[int, int] = {}

        self.camera = camera_factory(self.camera_config)
        self.midi: MidiListener | None = None
        self._hardware_released = False

        self.connect_page = ConnectPage(self)
        self.map_page = MapKeysPage(self)
        self.setPage(PAGE_CONNECT, self.connect_page)
        self.setPage(PAGE_MAP, self.map_page)
        self.setStartId(PAGE_CONNECT)
        self.currentIdChanged.connect(self._on_page_changed)
        self._on_page_changed(PAGE_CONNECT)

    def _on_page_changed(self, page_id: int) -> None:
        self.connect_page.set_polling(page_id == PAGE_CONNECT)
        self.map_page.set_polling(page_id == PAGE_MAP)

    def release_midi(self) -> None:
        if self.midi is not None:
            self.midi.close()
            self.midi = None

    def _release_hardware(self) -> None:
        if self._hardware_released:
            return
        self.connect_page.set_polling(False)
        self.map_page.set_polling(False)
        self.camera.release()
        self.release_midi()
        self._hardware_released = True

    def done(self, result: int) -> None:
        self._release_hardware()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._release_hardware()
        super().closeEvent(event)
