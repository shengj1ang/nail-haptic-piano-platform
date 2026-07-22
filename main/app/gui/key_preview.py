"""PyQt live preview window: pick a saved profile from a dropdown, see its
keys colored on the camera feed. No detection happens here - it just reads
whichever profile is selected and redraws its key_map every frame.

Picking a profile here immediately saves it as config.json's
active_keyboard_profile - this is where a user decides "this is the
keyboard setup everything else should use"; tools that no longer show
their own profile picker (Song Recording Wizard, Experiment Sequence
Generator) read that value by default.

Three independent checkboxes control what each key is labelled with - any
combination of key_id (this profile's own key number), note_id (the raw
MIDI note number, e.g. 48), and note_name (e.g. "C3"). The latter two need
a midi_mapping.json for this profile (from setup_midi_mapping_wizard.py)
and are disabled until one exists. If nothing is checked, key_id is shown
by default so a key is never left unlabelled."""

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
        self.key_id_check = QCheckBox("key_id")
        self.key_id_check.setChecked(True)
        self.note_id_check = QCheckBox("note_id")
        self.note_id_check.setEnabled(False)
        self.note_name_check = QCheckBox("note_name")
        self.note_name_check.setEnabled(False)
        self.status_label = QLabel("")
        self.view = ImageView()

        self.refresh_btn.clicked.connect(self.refresh_profiles)
        self.profile_combo.currentTextChanged.connect(self._load_profile)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Profile:"))
        top_row.addWidget(self.profile_combo, 1)
        top_row.addWidget(self.refresh_btn)
        top_row.addWidget(QLabel("Label:"))
        top_row.addWidget(self.key_id_check)
        top_row.addWidget(self.note_id_check)
        top_row.addWidget(self.note_name_check)

        self.profile_hint_label = QLabel(
            "Selecting a profile above saves it immediately as config.json's active_keyboard_profile - the Song "
            "Recording Wizard and Experiment Sequence Generator use that one by default."
        )
        self.profile_hint_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.profile_hint_label)
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

        target = current if current in profiles else self.cfg.active_keyboard_profile
        if target not in profiles:
            target = profiles[0]

        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, name: str) -> None:
        if not name:
            return

        # Saved right away (not just on close) - this preview is where a
        # user decides "this is the profile everything else should use",
        # so later tools (Song Recording Wizard, Experiment Sequence
        # Generator) should see it as soon as it's picked here.
        self.cfg.active_keyboard_profile = name
        self.cfg.save()

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
            self.note_id_check.setEnabled(True)
            self.note_name_check.setEnabled(True)
            status = f"Loaded '{name}' - {len(self.template.keys)} keys, {len(self.midi_mapping.key_to_note)} mapped to MIDI notes"
        else:
            self.midi_mapping = None
            self.note_id_check.setEnabled(False)
            self.note_id_check.setChecked(False)
            self.note_name_check.setEnabled(False)
            self.note_name_check.setChecked(False)
            status = f"Loaded '{name}' - {len(self.template.keys)} keys (no midi_mapping.json yet)"

        self.status_label.setText(status)

    def _build_label_map(self):
        """Each key's label is whichever of key_id/note_id/note_name are
        checked, joined with '/' (e.g. "5/48/C3") - note_id/note_name only
        apply to keys this profile's midi_mapping.json actually covers, an
        unmapped key just falls back to its key_id (see draw_labels). If
        nothing is checked, key_id is shown anyway so a key is never left
        unlabelled."""
        show_key_id = self.key_id_check.isChecked()
        show_note_id = self.note_id_check.isChecked() and self.midi_mapping is not None
        show_note_name = self.note_name_check.isChecked() and self.midi_mapping is not None

        if not (show_key_id or show_note_id or show_note_name):
            return None  # draw_labels defaults to the plain key_id already

        if not (show_note_id or show_note_name):
            return None  # key_id only - same as the no-label-map default

        label_map = {}
        for key_id, note in self.midi_mapping.key_to_note.items():
            parts = []
            if show_key_id:
                parts.append(str(key_id + 1))
            if show_note_id:
                parts.append(str(note))
            if show_note_name:
                parts.append(note_name(note))
            label_map[key_id + 1] = "/".join(parts)
        return label_map

    def _update_frame(self) -> None:
        frame = self.camera.read()
        if frame is None:
            return

        if self.template is not None:
            if frame.shape[:2] == self.template.key_map.shape[:2]:
                overlay_keys(frame, self.template.key_map, self.luts)
                draw_labels(frame, self.template.key_map, len(self.template.keys), self._build_label_map())
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
        self.camera.release()
        super().closeEvent(event)
