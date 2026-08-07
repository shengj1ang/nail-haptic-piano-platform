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

  0. Camera        click Scan -> pick a detected index -> live preview
  1. Calibration   name -> capture -> boundary -> cropped edges -> fill
                   keys -> Finish, which creates the profile folder
  2. MIDI mapping  Middle C check -> live camera/profile overlay -> press
                   each highlighted key in turn, recording its note

The camera comes first because the calibration is a pixel mask of *that*
camera's frame: step 2 captures through step 1's settings rather than
asking again, so the two cannot disagree.

The camera picker and calibration deliberately reproduce Initial Setup's
behaviour inside ``remote_guidance``.  The picker stays idle until the user
presses Scan; calibration uses the same five-page crop/edge/fill flow; and
MIDI mapping retains Initial Setup's Middle C and live highlighted-camera
images. Only their write boundary differs: this wizard never saves Config
and only creates or updates a protected Tele-training profile.

What may be written where is `setup_store`, deliberately GUI-free and
separately tested; this file is the UI over it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.camera import Camera, probe_camera_indices
from app.config import CameraConfig, Config
from app.gui.image_view import ImageView
from app.keyboard import KeyboardTemplate
from app.profiles import DATA_DIR as PROFILE_DATA_DIR

from .calibration_wizard import RemoteKeyboardCalibrationWizard
from .gui_common import STATUS_STYLES
from .midi_mapping_wizard import RemoteMidiMappingWizard
from .setup_store import (
    TEMPLATE_FILENAME,
)

log = logging.getLogger("remote_guidance.setup_wizard")

TICK_MS = 33
STEP_NAMES = ["1. Camera", "2. Calibration", "3. MIDI mapping"]
STEP_CAMERA, STEP_CALIBRATION, STEP_MIDI = range(3)

# Keep the same scan range as Initial Setup's camera picker.
CAMERA_PROBE_MAX_INDEX = 8


class _CameraProbeWorker(QThread):
    """Probe camera indices without freezing the Tele-training window."""

    progress = Signal(int, int)
    finished_scan = Signal(list)

    def __init__(self, max_index: int = CAMERA_PROBE_MAX_INDEX):
        super().__init__()
        self.max_index = max_index

    def run(self) -> None:
        available = probe_camera_indices(self.max_index, progress_callback=self.progress.emit)
        self.finished_scan.emit(available)


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
        self.camera_config = CameraConfig(
            index=cfg.camera.index,
            width=cfg.camera.width,
            height=cfg.camera.height,
            fps=cfg.camera.fps,
            flip_vertical=cfg.camera.flip_vertical,
            flip_horizontal=cfg.camera.flip_horizontal,
        )
        self.camera: Optional[Camera] = None
        self._camera_selected = False
        self._probe_worker: Optional[_CameraProbeWorker] = None
        self.calibration_wizard: Optional[RemoteKeyboardCalibrationWizard] = None
        self.mapping_wizard: Optional[RemoteMidiMappingWizard] = None
        self.mapping_profile_name = ""
        self.mapping_count = 0
        self.mapping_port_name: Optional[str] = None

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

        # The parent preview owns the camera only on step 1.  Step 2 opens
        # its own Initial-Setup-style calibration window after releasing it.
        if step != STEP_CAMERA:
            self._release_camera()

        self.stack.setCurrentIndex(step)
        self._sync_step_buttons()
        self._on_enter_step(step)

    def _blocked_reason(self, step: int) -> str:
        if step == STEP_CALIBRATION and not self._camera_selected:
            return "Click Scan for cameras, then select the camera to calibrate before opening step 2."
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
            if self._camera_selected:
                self._open_camera()
        elif step == STEP_CALIBRATION:
            self._refresh_calibration_summary()
            self._launch_calibration()
        elif step == STEP_MIDI:
            self._refresh_mapping_summary()
            self._launch_mapping()
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
    # Step 1 - camera
    # ------------------------------------------------------------------

    def _build_camera_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.index_combo = QComboBox()
        self.index_combo.addItem("Click Scan for cameras first", None)
        self.index_combo.setEnabled(False)
        self.scan_btn = QPushButton("Scan for cameras")
        self.scan_btn.clicked.connect(self._scan_cameras)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(160, 7680)
        self.width_spin.setValue(self.camera_config.width)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(120, 4320)
        self.height_spin.setValue(self.camera_config.height)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(self.camera_config.fps)

        self.flip_h = QCheckBox("Flip horizontal")
        self.flip_v = QCheckBox("Flip vertical")
        self.flip_h.setChecked(self.camera_config.flip_horizontal)
        self.flip_v.setChecked(self.camera_config.flip_vertical)

        # Connect only after populating the controls.  setRange/setValue emit
        # signals, and the old order opened camera 0 while the window itself
        # was still being constructed.
        self.index_combo.currentIndexChanged.connect(self._on_camera_changed)
        for spin in (self.width_spin, self.height_spin, self.fps_spin):
            spin.valueChanged.connect(self._on_camera_changed)
        for box in (self.flip_h, self.flip_v):
            box.stateChanged.connect(self._on_camera_changed)

        self.camera_progress = QProgressBar()
        self.camera_progress.setVisible(False)
        self.camera_progress.setRange(0, CAMERA_PROBE_MAX_INDEX)

        top = QHBoxLayout()
        top.addWidget(QLabel("Camera:"))
        top.addWidget(self.index_combo, 1)
        top.addWidget(self.scan_btn)

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
            "Opening this wizard does not open a camera. Click Scan, wait for the scan to finish, then "
            "choose one of the detected cameras to start its preview. The next step's calibration is a "
            "pixel mask of that camera's frame, so moving it later requires a new profile."
        )
        note.setWordWrap(True)
        note.setStyleSheet(STATUS_STYLES["idle"])

        layout.addLayout(top)
        layout.addWidget(self.camera_progress)
        layout.addLayout(size_row)
        layout.addWidget(self.camera_view, 1)
        layout.addWidget(note)
        return page

    def _scan_cameras(self) -> None:
        if self._probe_worker is not None:
            return
        self._release_camera()
        self._camera_selected = False
        self._sync_step_buttons()
        self.index_combo.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.camera_progress.setVisible(True)
        self.camera_progress.setValue(0)
        self._set_status("Scanning camera indices...", "idle")

        self._probe_worker = _CameraProbeWorker()
        self._probe_worker.progress.connect(self._on_scan_progress)
        self._probe_worker.finished_scan.connect(self._on_scan_finished)
        self._probe_worker.start()

    def _on_scan_progress(self, checked: int, total: int) -> None:
        self.camera_progress.setMaximum(total)
        self.camera_progress.setValue(checked)

    def _on_scan_finished(self, found: List[int]) -> None:
        self._probe_worker = None
        self.camera_progress.setVisible(False)
        self.scan_btn.setEnabled(True)

        self.index_combo.blockSignals(True)
        self.index_combo.clear()
        self.index_combo.addItem("Select a detected camera...", None)
        for index in found:
            self.index_combo.addItem(f"Camera {index}", index)
        self.index_combo.setCurrentIndex(0)
        self.index_combo.blockSignals(False)

        self.index_combo.setEnabled(bool(found))
        if found:
            self._set_status(
                f"Found {len(found)} camera(s). Select one from the list to open its preview.", "ok"
            )
        else:
            self._set_status("No cameras detected. Check the connection, then click Scan again.", "warn")

    def _on_camera_changed(self) -> None:
        data = self.index_combo.currentData()
        if data is None:
            self._camera_selected = False
            self._release_camera()
            self._sync_step_buttons()
            return
        self.camera_config = CameraConfig(
            index=data,
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps_spin.value(),
            flip_horizontal=self.flip_h.isChecked(),
            flip_vertical=self.flip_v.isChecked(),
        )
        self._camera_selected = True
        self._sync_step_buttons()
        self._open_camera()

    def _open_camera(self) -> None:
        self._release_camera()
        if not self._camera_selected:
            return
        self.camera = Camera(self.camera_config)
        if not self.camera.is_opened:
            self._set_status(
                f"Camera {self.camera_config.index!r} could not be opened - it may be unplugged or in use "
                "by another tool.",
                "warn",
            )
        else:
            self._set_status(
                f"Previewing camera {self.camera_config.index}. Continue to step 2 to calibrate this view.",
                "ok",
            )

    def _release_camera(self) -> None:
        if self.camera is not None:
            self.camera.release()
            self.camera = None

    # ------------------------------------------------------------------
    # Step 2 - calibration
    # ------------------------------------------------------------------

    def _build_calibration_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("Keyboard calibration")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        explanation = QLabel(
            "This opens the same five-page calibration flow as Initial Setup: name the profile, capture "
            "a photo, mark the keyboard boundary, tune edges on the cropped keyboard, then fill keys. "
            "The Tele-training copy only changes where Finish saves: it creates a new protected profile "
            "and never changes config.json or an existing profile."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(STATUS_STYLES["idle"])

        self.calibration_summary = QLabel("")
        self.calibration_summary.setWordWrap(True)
        self.launch_calibration_btn = QPushButton("Open keyboard calibration wizard")
        self.launch_calibration_btn.clicked.connect(self._launch_calibration)

        layout.addWidget(title)
        layout.addWidget(explanation)
        layout.addWidget(self.calibration_summary)
        layout.addStretch(1)
        layout.addWidget(self.launch_calibration_btn)
        return page

    def _refresh_calibration_summary(self) -> None:
        if self.profile_name:
            keys = self._template_keys()
            self.calibration_summary.setText(
                f"Current Tele-training profile: {self.profile_name!r} ({keys} calibrated keys). "
                "Continue to step 3 for MIDI mapping, or open the wizard again with a new profile name."
            )
        else:
            self.calibration_summary.setText(
                f"Selected camera: {self.camera_config.index} at "
                f"{self.camera_config.width} x {self.camera_config.height}."
            )

    def _launch_calibration(self) -> None:
        if self.calibration_wizard is not None and self.calibration_wizard.isVisible():
            self.calibration_wizard.raise_()
            self.calibration_wizard.activateWindow()
            return

        self._release_camera()
        wizard = RemoteKeyboardCalibrationWizard(
            self.base_cfg,
            self.camera_config,
            self.profile_data_dir,
            parent=self,
        )
        wizard.setWindowModality(Qt.WindowModality.WindowModal)
        wizard.finished.connect(self._on_calibration_finished)
        self.calibration_wizard = wizard
        self.launch_calibration_btn.setEnabled(False)
        wizard.show()

    def _on_calibration_finished(self, result: int) -> None:
        wizard = self.calibration_wizard
        self.calibration_wizard = None
        self.launch_calibration_btn.setEnabled(True)
        if wizard is None:
            return

        if result == QDialog.DialogCode.Accepted and wizard.profile_name:
            self.profile_name = wizard.profile_name
            self._sync_step_buttons()
            self._refresh_calibration_summary()
            self._set_status(
                f"Saved Tele-training profile {self.profile_name!r}. Next: step 3 maps its MIDI notes. "
                "No config.json setting or existing profile was changed.",
                "ok",
            )
        else:
            self._set_status("Calibration closed without saving a profile.", "idle")
        wizard.deleteLater()

    def _template_keys(self) -> int:
        """How many keys the current saved Tele-training profile has."""
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
    # Step 3 - MIDI mapping
    # ------------------------------------------------------------------

    def _build_midi_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("MIDI key mapping")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        explanation = QLabel(
            "This opens the same two-page visual mapping flow as Initial Setup. Page 1 shows the Middle C "
            "reference image and a live MIDI-note check. Page 2 shows this camera live with the selected "
            "profile overlaid: yellow is the next key to press, green is mapped, and grey is not reached. "
            "Only Tele-training-created profiles can be selected or saved."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(STATUS_STYLES["idle"])

        self.mapping_summary = QLabel("")
        self.mapping_summary.setWordWrap(True)
        self.launch_mapping_btn = QPushButton("Open MIDI key mapping wizard")
        self.launch_mapping_btn.clicked.connect(self._launch_mapping)

        layout.addWidget(title)
        layout.addWidget(explanation)
        layout.addWidget(self.mapping_summary)
        layout.addStretch(1)
        layout.addWidget(self.launch_mapping_btn)
        return page

    def _refresh_mapping_summary(self) -> None:
        if self.mapping_count:
            self.mapping_summary.setText(
                f"Saved {self.mapping_count} key-to-note mappings for {self.mapping_profile_name!r} "
                f"from MIDI port {self.mapping_port_name!r}. Open the wizard again to redo them."
            )
        else:
            self.mapping_summary.setText(
                f"Profile {self.profile_name!r} is ready. The mapping wizard will use camera "
                f"{self.camera_config.index} for the live highlighted-key view."
            )

    def _launch_mapping(self) -> None:
        if self.mapping_wizard is not None and self.mapping_wizard.isVisible():
            self.mapping_wizard.raise_()
            self.mapping_wizard.activateWindow()
            return

        self._release_camera()
        wizard = RemoteMidiMappingWizard(
            self.base_cfg,
            self.camera_config,
            self.profile_data_dir,
            preferred_profile_name=self.profile_name,
            preferred_port_name=self.mapping_port_name,
            parent=self,
        )
        wizard.setWindowModality(Qt.WindowModality.WindowModal)
        wizard.mappingSaved.connect(self._on_mapping_saved)
        wizard.finished.connect(self._on_mapping_finished)
        self.mapping_wizard = wizard
        self.launch_mapping_btn.setEnabled(False)
        wizard.show()

    def _on_mapping_saved(self, profile_name: str, count: int, port_name: str) -> None:
        self.profile_name = profile_name
        self.mapping_profile_name = profile_name
        self.mapping_count = count
        self.mapping_port_name = port_name or None
        self._refresh_mapping_summary()
        self._set_status(
            f"Saved {count} MIDI mappings into profile {profile_name!r}. That is the whole wizard - "
            "select this profile in the Student or Teacher Client's Settings to use it.",
            "ok",
        )

    def _on_mapping_finished(self, _result: int) -> None:
        wizard = self.mapping_wizard
        self.mapping_wizard = None
        self.launch_mapping_btn.setEnabled(True)
        if wizard is not None:
            wizard.deleteLater()

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        step = self.stack.currentIndex()
        if step == STEP_CAMERA and self.camera is not None:
            frame = self.camera.read()
            if frame is not None:
                self.camera_view.set_frame(frame)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self._probe_worker is not None:
            self._probe_worker.wait()
            self._probe_worker = None
        if self.calibration_wizard is not None:
            self.calibration_wizard.close()
            self.calibration_wizard = None
        if self.mapping_wizard is not None:
            self.mapping_wizard.close()
            self.mapping_wizard = None
        self._release_camera()
        super().closeEvent(event)
