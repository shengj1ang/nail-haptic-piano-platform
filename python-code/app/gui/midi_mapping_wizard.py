"""PyQt window for step 3: press the keyboard's keys in order 1, 2, 3, ...
and record which MIDI note number each one sends.

No vision/detection happens here - the camera feed is only shown so you can
see which key is highlighted as "next" against the profile's key_map.
"""

import cv2
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..camera import Camera
from ..config import Config
from ..keyboard.midi_mapping import MidiMapping, note_name
from ..keyboard.template import KeyboardTemplate
from ..keyboard.visualize import build_color_luts, draw_labels
from ..midi import MidiListener, list_input_ports
from ..profiles import DATA_DIR, list_profiles
from .image_view import ImageView

HIGHLIGHT_COLOR = (0, 255, 255)  # bright - the key you should press next
MAPPED_COLOR = (0, 200, 0)  # already captured
UNMAPPED_COLOR = (60, 60, 60)  # not reached yet


class MidiMappingWindow(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Step 3 - MIDI Key Mapping")

        self.cfg = cfg
        self.camera = Camera(cfg.camera)
        self.template = None
        self.luts = None
        self.midi = None
        self.mapping: dict = {}
        self.order: list = []
        self.pos = 0

        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._load_profile)

        self.port_combo = QComboBox()
        self.refresh_ports_btn = QPushButton("Refresh ports")
        self.connect_btn = QPushButton("Connect MIDI")
        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._connect_midi)

        self.view = ImageView()
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

        midi_row = QHBoxLayout()
        midi_row.addWidget(QLabel("MIDI port:"))
        midi_row.addWidget(self.port_combo, 1)
        midi_row.addWidget(self.refresh_ports_btn)
        midi_row.addWidget(self.connect_btn)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.undo_btn)
        btn_row.addWidget(self.skip_btn)
        btn_row.addWidget(self.reset_btn)
        btn_row.addWidget(self.save_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(profile_row)
        layout.addLayout(midi_row)
        layout.addWidget(self.view)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)

        self._refresh_profiles()
        self._refresh_ports()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

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
            self.status_label.setText("No profiles found under data/. Run step1_keyboard_wizard.py first.")
            return

        target = self.cfg.active_profile if self.cfg.active_profile in profiles else profiles[0]
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
    # MIDI handling
    # ------------------------------------------------------------------

    def _refresh_ports(self) -> None:
        ports = list_input_ports()
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if self.cfg.midi.port_name in ports:
            self.port_combo.setCurrentText(self.cfg.midi.port_name)

    def _connect_midi(self) -> None:
        if self.midi is not None:
            self.midi.close()
            self.midi = None

        port_name = self.port_combo.currentText() or None
        try:
            self.midi = MidiListener(port_name)
        except RuntimeError as e:
            QMessageBox.warning(self, "MIDI connection failed", str(e))
            return

        self.cfg.midi.port_name = self.midi.port_name
        self.cfg.save()
        self.status_label.setText(f"Connected to '{self.midi.port_name}'.")

    # ------------------------------------------------------------------
    # Stepping through keys
    # ------------------------------------------------------------------

    def _current_key_id(self):
        return self.order[self.pos] if self.pos < len(self.order) else None

    def _tick(self) -> None:
        frame = self.camera.read()
        if frame is None:
            return

        if self.template is not None and frame.shape[:2] == self.template.key_map.shape[:2]:
            self._draw_overlay(frame)

        self.view.set_frame(frame)

        if self.midi is not None:
            for event in self.midi.pop_events():
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

        profile_name = self.profile_combo.currentText()
        path = DATA_DIR / profile_name / "midi_mapping.json"
        port_name = self.midi.port_name if self.midi else self.cfg.midi.port_name

        mapping = MidiMapping(port_name=port_name, key_to_note=dict(self.mapping))
        mapping.save(path)

        QMessageBox.information(self, "Saved", f"Saved {len(self.mapping)} key-to-note mappings to {path}")

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self.camera.release()
        if self.midi is not None:
            self.midi.close()
        super().closeEvent(event)
