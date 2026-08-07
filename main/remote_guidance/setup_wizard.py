"""Tele-training Setup Wizard - build a keyboard profile, change nothing else.

A new person joining a tele-training session needs a calibration: a pixel
mask of their camera's view of their keyboard, plus that keyboard's
key->note mapping. The launcher's Initial Setup wizards produce exactly
that - and also write config.json's top-level `camera`, `midi.port_name`
and `active_keyboard_profile`, and save over an existing
`data/keyboard-profile/<name>/`. Those side effects configure the machine
the *formal experiment* runs on, so using them to onboard a remote partner
repoints the experiment's devices and can destroy the calibration its
recorded sessions were scored against.

This wizard produces the profile and **nothing else**. It writes no
config.json key at all - not even the `remote_guidance` block - so running
it cannot change what any tool on this machine does. The new profile is
picked up by selecting it in a client's own Settings dialog, which is
where choosing devices already lives.

Three steps, in order, either of the later two re-enterable from the step
bar so one part can be redone:

  0. Camera        pick the index, flips, live preview
  1. Calibration   capture -> boundary -> edges -> fill keys -> name and
                   save, which is what creates the profile folder
  2. MIDI mapping  press each key in turn, recording its note

The camera comes first because the calibration is a pixel mask of *that*
camera's frame: step 2 captures through step 1's settings rather than
asking again, so the two cannot disagree.

Not reused: the Initial Setup wizard *windows*, because their saving is
what makes them unsafe here. Reused: everything below the UI - the
paint-bucket segmentation (`app.keyboard.wizard.KeyFillWizard`), the edge
detector, `KeyboardTemplate`, `MidiMapping`, `Camera`, `MidiListener`.
Those define what a calibration *is*; a second copy would drift, and the
saved profile has to stay readable by the existing analysis tools.

What may be written where is `setup_store`, deliberately GUI-free and
separately tested; this file is the UI over it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.camera import Camera, probe_camera_indices
from app.config import CameraConfig, Config
from app.gui.image_view import ImageView
from app.keyboard import KeyboardDetector, KeyboardTemplate, KeyFillWizard
from app.keyboard.midi_mapping import MidiMapping, note_name
from app.midi import MidiListener, ambiguous_port_names, list_input_ports
from app.profiles import DATA_DIR as PROFILE_DATA_DIR

from .gui_common import STATUS_STYLES
from .setup_store import (
    MIDI_MAPPING_FILENAME,
    TEMPLATE_FILENAME,
    SetupError,
    create_profile,
    list_remote_profiles,
    sanitize_profile_name,
    writable_profile_dir,
)

log = logging.getLogger("remote_guidance.setup_wizard")

TICK_MS = 33
STEP_NAMES = ["1. Camera", "2. Calibration", "3. MIDI mapping"]
STEP_CAMERA, STEP_CALIBRATION, STEP_MIDI = range(3)

# Enough of a scan to find a second webcam without a long wait.
CAMERA_PROBE_MAX_INDEX = 6
# A freshly opened camera hands back a dark or stale first frame.
CAPTURE_READS = 3


class RemoteSetupWizard(QMainWindow):
    """The wizard window. Its only output is a profile folder."""

    def __init__(
        self,
        cfg: Config,
        profile_data_dir: Path = PROFILE_DATA_DIR,
    ):
        super().__init__()
        self.setWindowTitle("Tele-training Setup Wizard")

        # Read-only: the edge/fill defaults, and the values quoted in the
        # "what this will not touch" line. Never saved - this wizard has
        # no code path that writes config.json at all.
        self.base_cfg = cfg
        self.profile_data_dir = Path(profile_data_dir)

        self.profile_name = ""
        self.camera_config = CameraConfig()
        self.port_name: Optional[str] = None

        self.camera: Optional[Camera] = None
        self.captured_frame: Optional[np.ndarray] = None
        self.edges: Optional[np.ndarray] = None
        self.boundary: Optional[tuple] = None
        self.fill_wizard: Optional[KeyFillWizard] = None
        self.detector = KeyboardDetector(cfg.keyboard_detection)
        self.midi: Optional[MidiListener] = None
        self.key_to_note: Dict[int, int] = {}
        self._mapping_key_id = 0
        self._boundary_clicks: List[tuple] = []

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)
        self._go(STEP_CAMERA)

    # ------------------------------------------------------------------
    # UI scaffolding
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.step_buttons: List[QPushButton] = []
        step_row = QHBoxLayout()
        for index, name in enumerate(STEP_NAMES):
            button = QPushButton(name)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked, i=index: self._go(i))
            step_row.addWidget(button)
            self.step_buttons.append(button)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_camera_page())
        self.stack.addWidget(self._build_calibration_page())
        self.stack.addWidget(self._build_midi_page())

        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.isolation_note = QLabel("")
        self.isolation_note.setWordWrap(True)
        self.isolation_note.setStyleSheet(STATUS_STYLES["idle"])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(step_row)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.isolation_note)
        self.setCentralWidget(central)
        self.resize(940, 700)

    def _set_status(self, text: str, kind: str = "idle") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(STATUS_STYLES.get(kind, STATUS_STYLES["idle"]))

    def _go(self, step: int) -> None:
        """Move to a step, refusing the ones whose input is not there yet.

        Every step is directly reachable so a single part can be redone -
        remapping MIDI without recalibrating, most of all - but a step
        cannot be entered without what it consumes."""
        problem = self._blocked_reason(step)
        if problem:
            self._set_status(problem, "warn")
            self._sync_step_buttons()
            return

        # The camera belongs to whichever step is showing it.
        if step not in (STEP_CAMERA, STEP_CALIBRATION):
            self._release_camera()

        self.stack.setCurrentIndex(step)
        self._sync_step_buttons()
        self._on_enter_step(step)

    def _blocked_reason(self, step: int) -> str:
        if step == STEP_MIDI and not self._template_keys():
            return (
                "Calibrate and save a profile first (step 2) - the mapping counts up to the number of "
                "keys that were filled in."
            )
        return ""

    def _sync_step_buttons(self) -> None:
        current = self.stack.currentIndex()
        for index, button in enumerate(self.step_buttons):
            button.setChecked(index == current)
            button.setEnabled(not self._blocked_reason(index) or index == current)

    def _on_enter_step(self, step: int) -> None:
        if step == STEP_CAMERA:
            self._open_camera()
        elif step == STEP_CALIBRATION:
            self._open_camera()
            self._refresh_calibration_state()
        elif step == STEP_MIDI:
            self._refresh_profile_choices()
            self._refresh_midi_ports()
            self._refresh_mapping_label()
        self._refresh_isolation_note()

    def _refresh_isolation_note(self) -> None:
        cfg = self.base_cfg
        self.isolation_note.setText(
            "This wizard writes a profile folder and nothing else - no config.json key at all. This "
            f"machine's own settings (camera index {cfg.camera.index}, MIDI {cfg.midi.port_name!r}, "
            f"active profile {cfg.active_keyboard_profile!r}) and every profile this wizard did not "
            "create are left exactly as they are. To use the new profile, select it in the Student or "
            "Teacher Client's own Settings."
        )

    # ------------------------------------------------------------------
    # Profile bookkeeping (no page of its own any more - the calibration
    # step names and creates the profile, the MIDI step picks one)
    # ------------------------------------------------------------------

    def _refresh_profile_choices(self) -> None:
        """The MIDI step's picker. Only this wizard's own profiles, so
        remapping can never reach one the experiment depends on."""
        current = self.profile_combo.currentText() or self.profile_name
        self.profile_combo.clear()
        self.profile_combo.addItems(list_remote_profiles(self.profile_data_dir))
        if current:
            self.profile_combo.setCurrentText(current)

    def _on_profile_selected(self) -> None:
        name = self.profile_combo.currentText().strip()
        if not name or name == self.profile_name:
            return
        try:
            writable_profile_dir(name, self.profile_data_dir)
        except SetupError as exc:
            QMessageBox.warning(self, "Cannot use that profile", str(exc))
            return
        self.profile_name = name
        self.key_to_note = {}
        self._mapping_key_id = 0
        self._load_existing_profile()
        self._refresh_mapping_label()
        self._sync_step_buttons()

    def _load_existing_profile(self) -> None:
        """Pull back whatever this profile already has, so one part can be
        redone without the others."""
        directory = self.profile_data_dir / self.profile_name
        mapping_path = directory / MIDI_MAPPING_FILENAME
        if mapping_path.exists():
            mapping = MidiMapping.load(mapping_path)
            self.key_to_note = dict(mapping.key_to_note)
            self.port_name = mapping.port_name or self.port_name

    # ------------------------------------------------------------------
    # Step 1 - camera
    # ------------------------------------------------------------------

    def _build_camera_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.index_combo = QComboBox()
        self.index_combo.currentIndexChanged.connect(self._on_camera_changed)
        scan_btn = QPushButton("Scan for cameras")
        scan_btn.clicked.connect(self._scan_cameras)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(160, 7680)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(120, 4320)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        for spin in (self.width_spin, self.height_spin, self.fps_spin):
            spin.valueChanged.connect(self._on_camera_changed)

        self.flip_h = QCheckBox("Flip horizontal")
        self.flip_v = QCheckBox("Flip vertical")
        for box in (self.flip_h, self.flip_v):
            box.stateChanged.connect(self._on_camera_changed)

        top = QHBoxLayout()
        top.addWidget(QLabel("Camera:"))
        top.addWidget(self.index_combo, 1)
        top.addWidget(scan_btn)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Resolution:"))
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("x"))
        size_row.addWidget(self.height_spin)
        size_row.addWidget(QLabel("@"))
        size_row.addWidget(self.fps_spin)
        size_row.addWidget(QLabel("fps"))
        size_row.addWidget(self.flip_h)
        size_row.addWidget(self.flip_v)
        size_row.addStretch(1)

        self.camera_view = ImageView()
        note = QLabel(
            "The next step's calibration is a pixel mask of this camera's frame, so it only matches while "
            "this camera stays where it is. Move the camera or change the resolution afterwards and the "
            "calibration has to be redone."
        )
        note.setWordWrap(True)
        note.setStyleSheet(STATUS_STYLES["idle"])

        layout.addLayout(top)
        layout.addLayout(size_row)
        layout.addWidget(self.camera_view, 1)
        layout.addWidget(note)
        return page

    def _scan_cameras(self) -> None:
        self._release_camera()
        self._set_status("Scanning camera indices...", "idle")
        found = probe_camera_indices(CAMERA_PROBE_MAX_INDEX)
        current = self.camera_config.index
        self.index_combo.blockSignals(True)
        self.index_combo.clear()
        for index in found:
            self.index_combo.addItem(f"Camera {index}", index)
        if not found:
            self.index_combo.addItem("No camera found", 0)
        self.index_combo.blockSignals(False)
        position = self.index_combo.findData(current)
        if position >= 0:
            self.index_combo.setCurrentIndex(position)
        self._set_status(f"Found {len(found)} camera(s).", "ok" if found else "warn")
        self._open_camera()

    def _on_camera_changed(self) -> None:
        data = self.index_combo.currentData()
        self.camera_config = CameraConfig(
            index=data if data is not None else self.camera_config.index,
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps_spin.value(),
            flip_horizontal=self.flip_h.isChecked(),
            flip_vertical=self.flip_v.isChecked(),
        )
        self._open_camera()

    def _open_camera(self) -> None:
        self._release_camera()
        self.camera = Camera(self.camera_config)
        if not self.camera.is_opened:
            self._set_status(
                f"Camera {self.camera_config.index!r} could not be opened - it may be unplugged or in use "
                "by another tool.",
                "warn",
            )

    def _release_camera(self) -> None:
        if self.camera is not None:
            self.camera.release()
            self.camera = None

    # ------------------------------------------------------------------
    # Step 3 - calibration
    # ------------------------------------------------------------------

    def _build_calibration_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.capture_btn = QPushButton("Capture photo")
        self.capture_btn.clicked.connect(self._capture)
        self.recapture_btn = QPushButton("Back to live view")
        self.recapture_btn.clicked.connect(self._recapture)

        self.low_slider = QSlider(Qt.Orientation.Horizontal)
        self.low_slider.setRange(0, 500)
        self.low_slider.setValue(self.base_cfg.keyboard_detection.canny_low)
        self.high_slider = QSlider(Qt.Orientation.Horizontal)
        self.high_slider.setRange(0, 500)
        self.high_slider.setValue(self.base_cfg.keyboard_detection.canny_high)
        for slider in (self.low_slider, self.high_slider):
            slider.valueChanged.connect(self._refresh_edges)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Filling white keys", "white")
        self.kind_combo.addItem("Filling black keys", "black")
        undo_btn = QPushButton("Undo last key")
        undo_btn.clicked.connect(self._undo_fill)
        reset_btn = QPushButton("Start the fills over")
        reset_btn.clicked.connect(self._reset_fills)

        # Naming happens here rather than up front: saving is what creates
        # the folder, so there is no half-made profile lying around if the
        # calibration is abandoned.
        self.new_profile_edit = QLineEdit()
        self.new_profile_edit.setPlaceholderText("New profile name, e.g. remote-desk-20260807")
        self.save_template_btn = QPushButton("Save as a new profile")
        self.save_template_btn.clicked.connect(self._save_template)

        self.calibration_view = ImageView()
        self.calibration_view.clicked.connect(self._on_calibration_click)

        self.calibration_hint = QLabel("")
        self.calibration_hint.setWordWrap(True)

        capture_row = QHBoxLayout()
        capture_row.addWidget(self.capture_btn)
        capture_row.addWidget(self.recapture_btn)
        capture_row.addStretch(1)

        edge_row = QHBoxLayout()
        edge_row.addWidget(QLabel("Edge low:"))
        edge_row.addWidget(self.low_slider, 1)
        edge_row.addWidget(QLabel("high:"))
        edge_row.addWidget(self.high_slider, 1)

        fill_row = QHBoxLayout()
        fill_row.addWidget(self.kind_combo)
        fill_row.addWidget(undo_btn)
        fill_row.addWidget(reset_btn)
        fill_row.addStretch(1)

        save_row = QHBoxLayout()
        save_row.addWidget(self.new_profile_edit, 1)
        save_row.addWidget(self.save_template_btn)

        layout.addLayout(capture_row)
        layout.addLayout(edge_row)
        layout.addLayout(fill_row)
        layout.addWidget(self.calibration_view, 1)
        layout.addWidget(self.calibration_hint)
        layout.addLayout(save_row)
        return page

    def _reset_calibration_state(self) -> None:
        self.captured_frame = None
        self.edges = None
        self.boundary = None
        self.fill_wizard = None
        self._boundary_clicks = []

    def _refresh_calibration_state(self) -> None:
        if self.captured_frame is None:
            self.calibration_hint.setText("Point the camera at the keyboard, then press Capture photo.")
        elif self.boundary is None:
            self.calibration_hint.setText(
                "Click the keyboard's top-left corner, then its bottom-right corner."
            )
        else:
            self.calibration_hint.setText(
                "Click once inside each key, left to right: all the white keys first, then switch to black. "
                f"{len(self.fill_wizard.keys) if self.fill_wizard else 0} keys so far."
            )

    def _capture(self) -> None:
        frame = None
        for _ in range(CAPTURE_READS):
            candidate = self.camera.read() if self.camera is not None else None
            if candidate is not None:
                frame = candidate
        if frame is None:
            QMessageBox.warning(
                self, "No image", "The camera did not return a frame. Check step 2 and try again."
            )
            return
        self.captured_frame = frame.copy()
        self.boundary = None
        self.fill_wizard = None
        self._boundary_clicks = []
        self._refresh_edges()
        self._refresh_calibration_state()

    def _recapture(self) -> None:
        self._reset_calibration_state()
        self._refresh_calibration_state()

    def _refresh_edges(self) -> None:
        if self.captured_frame is None:
            return
        gray = cv2.cvtColor(self.captured_frame, cv2.COLOR_BGR2GRAY)
        self.edges = self.detector.compute_edges(
            gray, low=self.low_slider.value(), high=self.high_slider.value()
        )
        if self.boundary is not None and self.fill_wizard is None:
            self._build_fill_wizard()
        self._render_calibration()

    def _build_fill_wizard(self) -> None:
        self.fill_wizard = KeyFillWizard(
            self.captured_frame,
            self.boundary,
            self.edges,
            fill_tolerance=self.base_cfg.wizard.fill_tolerance,
            min_fill_px=self.base_cfg.wizard.min_fill_px,
        )

    def _on_calibration_click(self, x: int, y: int) -> None:
        if self.captured_frame is None:
            return
        if self.boundary is None:
            self._boundary_clicks.append((x, y))
            if len(self._boundary_clicks) == 2:
                (x0, y0), (x1, y1) = self._boundary_clicks
                self.boundary = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
                self._build_fill_wizard()
            self._render_calibration()
            self._refresh_calibration_state()
            return

        if self.fill_wizard is None:
            return
        self.fill_wizard.mode = self.kind_combo.currentData()
        if self.fill_wizard.try_fill(x, y) is None:
            self._set_status("Nothing filled there - that pixel is on an edge, outside the box, or taken.", "warn")
        self._render_calibration()
        self._refresh_calibration_state()

    def _undo_fill(self) -> None:
        if self.fill_wizard is not None:
            self.fill_wizard.undo()
            self._render_calibration()
            self._refresh_calibration_state()

    def _reset_fills(self) -> None:
        if self.fill_wizard is not None:
            self.fill_wizard.reset()
            self._render_calibration()
            self._refresh_calibration_state()

    def _render_calibration(self) -> None:
        if self.captured_frame is None:
            return
        canvas = self.captured_frame.copy()
        if self.edges is not None:
            canvas[self.edges > 0] = (0, 0, 255)
        if self.boundary is not None:
            bx, by, bw, bh = self.boundary
            cv2.rectangle(canvas, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
        if self.fill_wizard is not None:
            self.fill_wizard.overlay(canvas)
        self.calibration_view.set_frame(canvas)

    def _save_template(self) -> None:
        """Saving is what creates the profile folder.

        Always a *new* one: this wizard has no overwrite path, because a
        recorded session may have been scored against an existing
        calibration (setup_store.create_profile)."""
        if self.fill_wizard is None or not self.fill_wizard.keys:
            QMessageBox.warning(self, "Nothing to save", "Fill at least one key first.")
            return
        try:
            directory = create_profile(self.new_profile_edit.text(), self.profile_data_dir)
        except SetupError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return
        self.profile_name = sanitize_profile_name(self.new_profile_edit.text())
        self.key_to_note = {}
        self._mapping_key_id = 0

        height, width = self.captured_frame.shape[:2]
        bx, by, bw, bh = self.boundary
        template = KeyboardTemplate(
            frame_width=width,
            frame_height=height,
            region={"x": bx, "y": by, "w": bw, "h": bh},
            keys=list(self.fill_wizard.keys),
            key_map=self.fill_wizard.build_key_map(),
        )
        template.save(directory / TEMPLATE_FILENAME)
        self._set_status(
            f"Saved {len(template.keys)} keys as profile {self.profile_name!r} "
            f"({width} x {height}, camera {self.camera_config.index}). "
            "Next: step 3 maps each key to its MIDI note. Nothing in config.json was changed.",
            "ok",
        )
        self._refresh_profile_choices()
        self._sync_step_buttons()

    def _template_keys(self) -> int:
        """How many keys this profile has, from whichever is current - the
        fills on screen, or the template already saved."""
        if self.fill_wizard is not None and self.fill_wizard.keys:
            return len(self.fill_wizard.keys)
        if not self.profile_name:
            return 0
        path = self.profile_data_dir / self.profile_name / TEMPLATE_FILENAME
        if not path.exists():
            return 0
        try:
            return len(KeyboardTemplate.load(path).keys)
        except Exception:  # noqa: BLE001 - an unreadable template is "not calibrated"
            return 0

    # ------------------------------------------------------------------
    # Step 4 - MIDI mapping
    # ------------------------------------------------------------------

    def _build_midi_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        # Which profile to map. Preselected after a calibration; changing
        # it is how "redo only the MIDI mapping" works, and only this
        # wizard's own profiles are ever listed.
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)

        self.port_combo = QComboBox()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_midi_ports)
        self.connect_btn = QPushButton("Connect MIDI")
        self.connect_btn.clicked.connect(self._connect_midi)

        self.port_hint = QLabel("")
        self.port_hint.setWordWrap(True)
        self.port_hint.setStyleSheet(STATUS_STYLES["warn"])

        self.mapping_label = QLabel("")
        self.mapping_label.setWordWrap(True)
        restart_btn = QPushButton("Start the mapping over")
        restart_btn.clicked.connect(self._restart_mapping)
        self.save_mapping_btn = QPushButton("Save MIDI mapping")
        self.save_mapping_btn.clicked.connect(self._save_mapping)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        profile_row.addWidget(self.profile_combo, 1)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("MIDI port:"))
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(refresh_btn)
        port_row.addWidget(self.connect_btn)

        action_row = QHBoxLayout()
        action_row.addWidget(restart_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.save_mapping_btn)

        explain = QLabel(
            "Connect the keyboard, then press its keys in the same order they were filled in step 3 - "
            "key 1 first. Each press records that key's MIDI note."
        )
        explain.setWordWrap(True)
        explain.setStyleSheet(STATUS_STYLES["idle"])

        layout.addLayout(profile_row)
        layout.addLayout(port_row)
        layout.addWidget(self.port_hint)
        layout.addWidget(explain)
        layout.addWidget(self.mapping_label, 1)
        layout.addLayout(action_row)
        return page

    def _refresh_midi_ports(self) -> None:
        current = self.port_combo.currentText() or (self.port_name or "")
        self.port_combo.clear()
        self.port_combo.addItems(list_input_ports())
        if current:
            self.port_combo.setCurrentText(current)
        duplicates = ambiguous_port_names()
        if duplicates:
            names = ", ".join(repr(name) for name in duplicates)
            self.port_hint.setText(
                f"More than one instrument reports {names}. The #1/#2 numbers follow this machine's "
                "enumeration order and can change when a keyboard is replugged - make sure this is the "
                "one in front of the person being set up."
            )
        else:
            self.port_hint.setText("")
        self.port_hint.setVisible(bool(duplicates))

    def _connect_midi(self) -> None:
        self._release_midi()
        name = self.port_combo.currentText().strip() or None
        try:
            self.midi = MidiListener(name)
        except RuntimeError as exc:
            QMessageBox.warning(self, "MIDI connection failed", str(exc))
            return
        self.port_name = self.midi.port_name
        self._set_status(f"Listening on {self.midi.port_name!r}. Press key {self._mapping_key_id + 1}.", "ok")
        self._refresh_mapping_label()

    def _release_midi(self) -> None:
        if self.midi is not None:
            self.midi.close()
            self.midi = None

    def _restart_mapping(self) -> None:
        self.key_to_note = {}
        self._mapping_key_id = 0
        self._refresh_mapping_label()

    def _refresh_mapping_label(self) -> None:
        total = self._template_keys()
        done = len(self.key_to_note)
        lines = [f"{done} of {total} keys mapped."]
        if done:
            lines.append(
                "  ".join(f"key {k + 1}={note_name(n)}" for k, n in sorted(self.key_to_note.items()))
            )
        if done < total:
            lines.append(f"Next: press key {self._mapping_key_id + 1}.")
        self.mapping_label.setText("\n".join(lines))

    def _record_note(self, note: int) -> None:
        total = self._template_keys()
        if self._mapping_key_id >= total:
            return
        self.key_to_note[self._mapping_key_id] = int(note)
        self._mapping_key_id += 1
        self._refresh_mapping_label()

    def _save_mapping(self) -> None:
        if not self.key_to_note:
            QMessageBox.warning(self, "Nothing to save", "Map at least one key first.")
            return
        try:
            directory = writable_profile_dir(self.profile_name, self.profile_data_dir)
        except SetupError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return
        MidiMapping(port_name=self.port_name, key_to_note=dict(self.key_to_note)).save(
            directory / MIDI_MAPPING_FILENAME
        )
        self._set_status(
            f"Saved the MIDI mapping into profile {self.profile_name!r}. That is the whole wizard - "
            "select this profile in the Student or Teacher Client's Settings to use it.",
            "ok",
        )

    # ------------------------------------------------------------------
    # Step 5 - review and save
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------

    def _tick(self) -> None:
        step = self.stack.currentIndex()
        if step == STEP_CAMERA and self.camera is not None:
            frame = self.camera.read()
            if frame is not None:
                self.camera_view.set_frame(frame)
        elif step == STEP_CALIBRATION and self.captured_frame is None and self.camera is not None:
            frame = self.camera.read()
            if frame is not None:
                self.calibration_view.set_frame(frame)

        if self.midi is not None:
            for event in self.midi.pop_events():
                self._record_note(event.note)

    def closeEvent(self, event) -> None:
        self._release_camera()
        self._release_midi()
        super().closeEvent(event)
