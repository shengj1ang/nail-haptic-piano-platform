"""PyQt live preview window: pick a saved profile from a dropdown, see its
keys colored on the camera feed. No detection happens here - it just reads
whichever profile is selected and redraws its key_map every frame.

If the profile also has a midi_mapping.json (from setup_midi_mapping_wizard.py),
a checkbox lets you switch the on-screen labels from raw key numbers to
the MIDI note name each key actually sends."""

import cv2
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..camera import Camera
from ..config import Config
from ..keyboard.midi_mapping import MidiMapping, note_name
from ..keyboard.template import KeyboardTemplate
from ..keyboard.visualize import build_color_luts, draw_labels, overlay_keys
from ..profiles import DATA_DIR, list_profiles
from .image_view import ImageView


class KeyPreviewWindow(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Keyboard Key Preview")

        self.cfg = cfg
        self.camera = Camera(cfg.camera)
        self.template = None
        self.luts = None
        self.midi_mapping = None

        self.profile_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh list")
        self.mapping_check = QCheckBox("Show MIDI note names")
        self.mapping_check.setEnabled(False)
        self.status_label = QLabel("")
        self.view = ImageView()

        self.refresh_btn.clicked.connect(self.refresh_profiles)
        self.profile_combo.currentTextChanged.connect(self._load_profile)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Profile:"))
        top_row.addWidget(self.profile_combo, 1)
        top_row.addWidget(self.refresh_btn)
        top_row.addWidget(self.mapping_check)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.view)
        layout.addWidget(self.status_label)

        self.refresh_profiles()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_frame)
        self._timer.start(33)

    def refresh_profiles(self) -> None:
        profiles = list_profiles(DATA_DIR)
        current = self.profile_combo.currentText()

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        self.profile_combo.blockSignals(False)

        if not profiles:
            self.template = None
            self.status_label.setText("No profiles found under data/keyboard-profile/. Run setup_keyboard_wizard.py first.")
            return

        target = current if current in profiles else self.cfg.active_profile
        if target not in profiles:
            target = profiles[0]

        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, name: str) -> None:
        if not name:
            return

        template_path = DATA_DIR / name / "keyboard_template.json"
        if not template_path.exists():
            self.template = None
            self.status_label.setText(f"Profile '{name}' has no template.")
            return

        self.template = KeyboardTemplate.load(template_path)
        self.luts = build_color_luts(len(self.template.keys))

        mapping_path = DATA_DIR / name / "midi_mapping.json"
        if mapping_path.exists():
            self.midi_mapping = MidiMapping.load(mapping_path)
            self.mapping_check.setEnabled(True)
            status = f"Loaded '{name}' - {len(self.template.keys)} keys, {len(self.midi_mapping.key_to_note)} mapped to MIDI notes"
        else:
            self.midi_mapping = None
            self.mapping_check.setEnabled(False)
            self.mapping_check.setChecked(False)
            status = f"Loaded '{name}' - {len(self.template.keys)} keys (no midi_mapping.json yet)"

        self.status_label.setText(status)

    def _update_frame(self) -> None:
        frame = self.camera.read()
        if frame is None:
            return

        if self.template is not None:
            if frame.shape[:2] == self.template.key_map.shape[:2]:
                overlay_keys(frame, self.template.key_map, self.luts)

                label_map = None
                if self.mapping_check.isChecked() and self.midi_mapping is not None:
                    label_map = {
                        key_id + 1: note_name(note) for key_id, note in self.midi_mapping.key_to_note.items()
                    }

                draw_labels(frame, self.template.key_map, len(self.template.keys), label_map)
            else:
                cv2.putText(
                    frame,
                    "Resolution mismatch with this profile - recalibrate",
                    (20, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),
                    2,
                )

        self.view.set_frame(frame)

    def closeEvent(self, event) -> None:
        self._timer.stop()

        name = self.profile_combo.currentText()
        if name:
            self.cfg.active_profile = name
            self.cfg.save()

        self.camera.release()
        super().closeEvent(event)
