"""Initial-Setup-equivalent calibration for Tele-training.

The page flow and calibration math intentionally mirror
``app.gui.calibration_wizard.KeyboardCalibrationWizard``:

  profile name -> live capture -> boundary -> cropped edge tuning -> key fill

The difference is only the write boundary.  The Initial Setup wizard updates
the platform's active profile and edge settings; this copy writes a brand-new,
Tele-training-owned profile through :mod:`remote_guidance.setup_store` and
never writes ``config.json``.  Keeping this as a separate implementation means
Tele-training changes cannot alter section 1 or the formal experiment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from app.camera import Camera
from app.config import CameraConfig, Config
from app.gui.image_view import ImageView
from app.keyboard import KeyboardDetector, KeyboardTemplate, KeyFillWizard

from .setup_store import TEMPLATE_FILENAME, SetupError, create_profile, sanitize_profile_name

PAGE_PROFILE, PAGE_CAPTURE, PAGE_BOUNDARY, PAGE_EDGES, PAGE_FILL = range(5)


class ProfileNamePage(QWizardPage):
    def __init__(self, wizard: "RemoteKeyboardCalibrationWizard"):
        super().__init__()
        self.setTitle("Step 0 - Name this camera profile")
        self.setSubTitle(
            "Saved under data/keyboard-profile/<name>/ - use a different name per camera/keyboard "
            "setup so you can switch between them later. Tele-training never overwrites a profile."
        )
        self._wizard = wizard

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. remote-desk-20260807")
        self.name_edit.textChanged.connect(lambda _: self.completeChanged.emit())

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Profile name:"))
        layout.addWidget(self.name_edit)

    def isComplete(self) -> bool:
        return bool(sanitize_profile_name(self.name_edit.text()))

    def validatePage(self) -> bool:
        self._wizard.state["keyboard_profile_name"] = sanitize_profile_name(self.name_edit.text())
        return True


class CapturePage(QWizardPage):
    def __init__(self, wizard: "RemoteKeyboardCalibrationWizard"):
        super().__init__()
        self.setTitle("Step 1 - Capture a photo")
        self.setSubTitle("Point the camera at the keyboard, then click Capture.")
        self._wizard = wizard

        self.view = ImageView()
        self.capture_btn = QPushButton("Capture")
        self.capture_btn.clicked.connect(self._capture)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addWidget(self.capture_btn)

    def _capture(self) -> None:
        frame = self._wizard.latest_live_frame
        if frame is not None:
            self._wizard.state["frame"] = frame.copy()
            self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._wizard.state.get("frame") is not None


class BoundaryPage(QWizardPage):
    def __init__(self, wizard: "RemoteKeyboardCalibrationWizard"):
        super().__init__()
        self.setTitle("Step 2 - Mark the keyboard boundary")
        self.setSubTitle("Click the top-left corner of the keyboard, then the bottom-right corner.")
        self._wizard = wizard
        self.points = []

        self.view = ImageView()
        self.view.clicked.connect(self._on_click)

        self.hint = QLabel("")
        self.reset_btn = QPushButton("Reset points")
        self.reset_btn.clicked.connect(self._reset)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addWidget(self.hint)
        layout.addWidget(self.reset_btn)

    def initializePage(self) -> None:
        self.points = []
        self._redraw()

    def _on_click(self, x: int, y: int) -> None:
        if len(self.points) >= 2:
            return
        self.points.append((x, y))
        self._redraw()
        self.completeChanged.emit()

    def _reset(self) -> None:
        self.points = []
        self._redraw()
        self.completeChanged.emit()

    def _redraw(self) -> None:
        frame = self._wizard.state["frame"].copy()

        for point in self.points:
            cv2.circle(frame, point, 6, (0, 255, 0), -1)

        if len(self.points) == 2:
            (x1, y1), (x2, y2) = self.points
            cv2.rectangle(
                frame,
                (min(x1, x2), min(y1, y2)),
                (max(x1, x2), max(y1, y2)),
                (0, 255, 0),
                2,
            )
            self.hint.setText("Boundary set. Click Next, or Reset to redo.")
        else:
            self.hint.setText(f"Click corner {len(self.points) + 1} of 2.")

        self.view.set_frame(frame)

    def isComplete(self) -> bool:
        return len(self.points) == 2

    def validatePage(self) -> bool:
        (x1, y1), (x2, y2) = self.points
        bx, by = min(x1, x2), min(y1, y2)
        bw, bh = abs(x2 - x1), abs(y2 - y1)

        if bw < 20 or bh < 20:
            QMessageBox.warning(self, "Boundary too small", "Pick two corners further apart.")
            return False

        self._wizard.state["boundary"] = (bx, by, bw, bh)
        return True


class EdgeTunePage(QWizardPage):
    def __init__(self, wizard: "RemoteKeyboardCalibrationWizard"):
        super().__init__()
        self.setTitle("Step 3 - Tune edge detection")
        self.setSubTitle("Drag the sliders until the seams between keys form clean, closed lines.")
        self._wizard = wizard
        self._crop = None

        self.view = ImageView()
        self.low_slider = QSlider(Qt.Orientation.Horizontal)
        self.low_slider.setRange(0, 500)
        self.high_slider = QSlider(Qt.Orientation.Horizontal)
        self.high_slider.setRange(0, 500)
        self.low_label = QLabel()
        self.high_label = QLabel()

        self.low_slider.valueChanged.connect(self._update)
        self.high_slider.valueChanged.connect(self._update)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Low"))
        row1.addWidget(self.low_slider)
        row1.addWidget(self.low_label)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("High"))
        row2.addWidget(self.high_slider)
        row2.addWidget(self.high_label)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addLayout(row1)
        layout.addLayout(row2)

    def initializePage(self) -> None:
        frame = self._wizard.state["frame"]
        bx, by, bw, bh = self._wizard.state["boundary"]
        self._crop = frame[by : by + bh, bx : bx + bw].copy()
        self._wizard.state["crop"] = self._crop

        cfg = self._wizard.cfg.keyboard_detection
        self.low_slider.setValue(cfg.canny_low)
        self.high_slider.setValue(cfg.canny_high)
        self._update()

    def _update(self) -> None:
        if self._crop is None:
            return
        low = self.low_slider.value()
        high = self.high_slider.value()
        self.low_label.setText(str(low))
        self.high_label.setText(str(high))

        gray = cv2.cvtColor(self._crop, cv2.COLOR_BGR2GRAY)
        edges = self._wizard.detector.compute_edges(gray, low=low, high=high)

        display = self._crop.copy()
        display[edges > 0] = (0, 0, 255)
        self.view.set_frame(display)

        self._wizard.state["edges"] = edges
        self._wizard.state["canny_low"] = low
        self._wizard.state["canny_high"] = high

    def validatePage(self) -> bool:
        # Unlike Initial Setup, these values are calibration-local.  Writing
        # them back would change section 1 and the formal experiment.
        return True


class FillKeysPage(QWizardPage):
    def __init__(self, wizard: "RemoteKeyboardCalibrationWizard"):
        super().__init__()
        self.setTitle("Step 4 - Mark keys")
        self.setSubTitle("Click inside each key to fill its range, like a paint bucket.")
        self._wizard = wizard
        self.fill_wizard: KeyFillWizard | None = None

        self.view = ImageView()
        self.view.clicked.connect(self._on_click)

        self.white_radio = QRadioButton("White key")
        self.black_radio = QRadioButton("Black key")
        self.white_radio.setChecked(True)
        self.white_radio.toggled.connect(self._on_mode_changed)

        self.edges_check = QCheckBox("Show edge overlay")
        self.edges_check.setChecked(True)
        self.edges_check.toggled.connect(self._redraw)

        self.undo_btn = QPushButton("Undo last")
        self.undo_btn.clicked.connect(self._undo)
        self.reset_btn = QPushButton("Reset all")
        self.reset_btn.clicked.connect(self._reset)

        self.status_label = QLabel("")

        mode_row = QHBoxLayout()
        mode_row.addWidget(self.white_radio)
        mode_row.addWidget(self.black_radio)
        mode_row.addWidget(self.edges_check)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.undo_btn)
        btn_row.addWidget(self.reset_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addLayout(mode_row)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)

    def initializePage(self) -> None:
        crop = self._wizard.state["crop"]
        edges = self._wizard.state["edges"]
        ch, cw = crop.shape[:2]
        wizard_cfg = self._wizard.cfg.wizard

        self.fill_wizard = KeyFillWizard(
            crop,
            (0, 0, cw, ch),
            edges,
            fill_tolerance=wizard_cfg.fill_tolerance,
            min_fill_px=wizard_cfg.min_fill_px,
            max_fill_radius=int(wizard_cfg.max_fill_size_ratio * max(cw, ch)),
        )
        self._redraw()

    def _on_mode_changed(self) -> None:
        if self.fill_wizard is not None:
            self.fill_wizard.mode = "white" if self.white_radio.isChecked() else "black"

    def _on_click(self, x: int, y: int) -> None:
        if self.fill_wizard is None:
            return
        key = self.fill_wizard.try_fill(x, y)
        if key is None:
            self.status_label.setText("Click ignored (edge / already filled).")
        else:
            self.status_label.setText(f"Filled key {key.id + 1} ({key.kind}).")
        self._redraw()

    def _undo(self) -> None:
        if self.fill_wizard is not None:
            self.fill_wizard.undo()
            self._redraw()

    def _reset(self) -> None:
        if self.fill_wizard is not None:
            self.fill_wizard.reset()
            self._redraw()

    def _redraw(self) -> None:
        if self.fill_wizard is None:
            return
        display = self._wizard.state["crop"].copy()

        if self.edges_check.isChecked():
            edges = self._wizard.state["edges"]
            display[edges > 0] = (0, 0, 255)

        self.fill_wizard.overlay(display)
        self.view.set_frame(display)
        self.setSubTitle(f"Keys marked so far: {len(self.fill_wizard.keys)}")

    def validatePage(self) -> bool:
        if self.fill_wizard is None or not self.fill_wizard.keys:
            QMessageBox.warning(self, "Nothing to save", "Fill at least one key first.")
            return False

        profile_name = self._wizard.state["keyboard_profile_name"]
        try:
            directory = create_profile(profile_name, self._wizard.profile_data_dir)
        except SetupError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return False

        frame = self._wizard.state["frame"]
        height, width = frame.shape[:2]
        bx, by, bw, bh = self._wizard.state["boundary"]

        # The fill wizard operates on the cropped keyboard, exactly as the
        # Initial Setup wizard does.  Persist a full-frame map so all existing
        # hit-testing and analysis code can use raw camera coordinates.
        key_map = np.zeros((height, width), dtype=np.uint8)
        key_map[by : by + bh, bx : bx + bw] = self.fill_wizard.build_key_map()

        template = KeyboardTemplate(
            frame_width=width,
            frame_height=height,
            region={"x": bx, "y": by, "w": bw, "h": bh},
            keys=list(self.fill_wizard.keys),
            key_map=key_map,
        )
        template_path = directory / TEMPLATE_FILENAME
        template.save(template_path)

        self._wizard.profile_name = profile_name
        self._wizard.profileSaved.emit(profile_name)
        QMessageBox.information(
            self,
            "Saved",
            f"Saved {len(template.keys)} keys to {template_path}\n"
            "No config.json setting or existing profile was changed.",
        )
        return True


class RemoteKeyboardCalibrationWizard(QWizard):
    """The Initial Setup calibration flow with a Tele-training-only save."""

    profileSaved = Signal(str)

    def __init__(
        self,
        cfg: Config,
        camera_config: CameraConfig,
        profile_data_dir: Path,
        camera_factory: Callable[[CameraConfig], Camera] = Camera,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Tele-training Keyboard Calibration Wizard")
        self.setOptions(QWizard.WizardOption.NoBackButtonOnLastPage)

        # cfg is read-only.  It supplies the same edge/fill defaults as the
        # Initial Setup wizard, but this class contains no Config.save path.
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
        self.detector = KeyboardDetector(cfg.keyboard_detection)
        self.camera = camera_factory(self.camera_config)
        self.state: dict = {}
        self.latest_live_frame = None
        self.profile_name = ""
        self._camera_released = False

        self.setPage(PAGE_PROFILE, ProfileNamePage(self))
        self.setPage(PAGE_CAPTURE, CapturePage(self))
        self.setPage(PAGE_BOUNDARY, BoundaryPage(self))
        self.setPage(PAGE_EDGES, EdgeTunePage(self))
        self.setPage(PAGE_FILL, FillKeysPage(self))
        self.setStartId(PAGE_PROFILE)

        self.currentIdChanged.connect(self._on_page_changed)

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._update_capture_preview)
        self._preview_timer.start(33)

    def _on_page_changed(self, page_id: int) -> None:
        if page_id == PAGE_CAPTURE:
            self._preview_timer.start(33)
        else:
            self._preview_timer.stop()

    def _update_capture_preview(self) -> None:
        page = self.currentPage()
        if not isinstance(page, CapturePage):
            return

        frame = self.camera.read()
        if frame is not None:
            self.latest_live_frame = frame
            page.view.set_frame(frame)

    def _release_camera(self) -> None:
        if not self._camera_released:
            self._preview_timer.stop()
            self.camera.release()
            self._camera_released = True

    def done(self, result: int) -> None:
        self._release_camera()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._release_camera()
        super().closeEvent(event)
