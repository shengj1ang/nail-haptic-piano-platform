"""Real-time finger-accuracy detector.

Shows the calibrated keyboard's colored key overlay and the live hand
skeleton together in one preview. On every MIDI note, works out which
fingertip pressed the corresponding key (see finger_matching.py) and logs
the result - "was the correct finger used for this key" is a comparison
against a reference fingering that plugs in on top of this, one level up.
"""

import time

import cv2
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..camera import Camera
from ..config import Config
from ..finger_matching import match_note_to_finger
from ..hand_tracking import HandTracker, draw_hands
from ..keyboard.midi_mapping import MidiMapping, note_name
from ..keyboard.template import KeyboardTemplate
from ..keyboard.visualize import build_color_luts, draw_labels, overlay_keys
from ..midi import MidiListener, list_input_ports
from ..profiles import DATA_DIR, list_profiles
from .image_view import ImageView

MATCH_HIGHLIGHT_SECONDS = 1.0
MATCH_COLOR = (0, 0, 255)


class FingerDetectorWindow(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Finger Accuracy - Live Detection")

        self.cfg = cfg
        self.camera = Camera(cfg.camera)
        self.hand_tracker = HandTracker()

        self.template = None
        self.mapping = None
        self.luts = None
        self.midi = None
        self.last_hands = {}
        self.last_match = None
        self.last_match_time = 0.0

        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._load_profile)

        self.port_combo = QComboBox()
        self.refresh_ports_btn = QPushButton("Refresh ports")
        self.connect_btn = QPushButton("Connect MIDI")
        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._connect_midi)

        self.view = ImageView()
        self.status_label = QLabel("")
        self.log_list = QListWidget()

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        profile_row.addWidget(self.profile_combo, 1)

        midi_row = QHBoxLayout()
        midi_row.addWidget(QLabel("MIDI port:"))
        midi_row.addWidget(self.port_combo, 1)
        midi_row.addWidget(self.refresh_ports_btn)
        midi_row.addWidget(self.connect_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(profile_row)
        layout.addLayout(midi_row)
        layout.addWidget(self.view)
        layout.addWidget(self.status_label)
        layout.addWidget(QLabel("Recent matches:"))
        layout.addWidget(self.log_list)

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
            self.status_label.setText("No profiles found under data/keyboard-profile/. Run step1_keyboard_wizard.py first.")
            return

        target = self.cfg.active_profile if self.cfg.active_profile in profiles else profiles[0]
        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, name: str) -> None:
        if not name:
            return

        template_path = DATA_DIR / name / "keyboard_template.json"
        mapping_path = DATA_DIR / name / "midi_mapping.json"

        if not template_path.exists():
            self.template = None
            self.status_label.setText(f"Profile '{name}' has no keyboard_template.json.")
            return

        self.template = KeyboardTemplate.load(template_path)
        self.luts = build_color_luts(len(self.template.keys))

        if mapping_path.exists():
            self.mapping = MidiMapping.load(mapping_path)
            self.status_label.setText(
                f"Loaded '{name}' - {len(self.template.keys)} keys, "
                f"{len(self.mapping.key_to_note)} mapped to MIDI notes"
            )
        else:
            self.mapping = None
            self.status_label.setText(f"Profile '{name}' has no midi_mapping.json - run step2_midi_mapping.py.")

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
            self.status_label.setText(f"MIDI connection failed: {e}")
            return

        self.cfg.midi.port_name = self.midi.port_name
        self.cfg.save()
        self.status_label.setText(f"Connected to '{self.midi.port_name}'.")

    # ------------------------------------------------------------------
    # Per-frame loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        frame = self.camera.read()
        if frame is None:
            return

        self.last_hands = self.hand_tracker.process(frame)

        if self.template is not None and frame.shape[:2] == self.template.key_map.shape[:2]:
            overlay_keys(frame, self.template.key_map, self.luts, alpha=0.35)
            draw_labels(frame, self.template.key_map, len(self.template.keys))

            if self.last_match is not None and time.time() - self.last_match_time <= MATCH_HIGHLIGHT_SECONDS:
                self._draw_match(frame, self.last_match)

        draw_hands(frame, self.last_hands)
        self.view.set_frame(frame)

        if self.midi is not None:
            for event in self.midi.pop_events():
                self._on_note(event.note)

    def _draw_match(self, frame: np.ndarray, match) -> None:
        mask = self.template.key_map == match.key_id + 1
        tint = np.zeros_like(frame)
        tint[mask] = MATCH_COLOR
        blended = cv2.addWeighted(frame, 0.5, tint, 0.5, 0)
        frame[mask] = blended[mask]

        cv2.circle(frame, match.point, 16, MATCH_COLOR, 3)

    def _on_note(self, note: int) -> None:
        if self.template is None or self.mapping is None:
            self.status_label.setText(f"Note {note} ({note_name(note)}) received, but no profile/mapping loaded.")
            return

        match = match_note_to_finger(note, self.template, self.mapping, self.last_hands)
        if match is None:
            text = f"Note {note} ({note_name(note)}) - no matching key/finger (is a hand visible?)"
            self.status_label.setText(text)
            self.log_list.insertItem(0, text)
            return

        self.last_match = match
        self.last_match_time = time.time()

        where = "inside key" if match.inside else f"~{match.distance_px:.0f}px from key"
        text = f"Key {match.key_id + 1} / note {note} ({note_name(note)}) -> {match.finger} ({where})"
        self.status_label.setText(text)
        self.log_list.insertItem(0, text)

        while self.log_list.count() > 50:
            self.log_list.takeItem(self.log_list.count() - 1)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self.camera.release()
        self.hand_tracker.close()
        if self.midi is not None:
            self.midi.close()
        super().closeEvent(event)
