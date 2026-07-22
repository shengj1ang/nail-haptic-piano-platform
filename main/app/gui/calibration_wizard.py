"""PyQt (PySide6) calibration wizard - mouse and buttons only, no keyboard shortcuts.

Five pages:
  0. Profile   - name this camera/keyboard setup; saved under data/keyboard-profile/<name>/.
  1. Capture   - live preview, click "Capture" to freeze a photo.
  2. Boundary  - click the keyboard's top-left corner, then its bottom-right
     corner (two plain clicks, no dragging).
  3. Edges     - sliders tune Canny low/high until key seams are clean lines.
  4. Fill keys - click once inside each key to flood-fill its range.

This is meant to be run once per physical setup: as long as the camera and
keyboard don't move relative to each other, data/keyboard-profile/<profile>/keyboard_template.json
from the last run stays valid. Multiple profiles can coexist side by side -
whichever was saved most recently becomes config.json's active_keyboard_profile.
"""

import re

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
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

from ..camera import Camera
from ..config import Config
from .image_view import ImageView
from ..keyboard import KeyboardDetector, KeyboardTemplate, KeyFillWizard
from ..profiles import DATA_DIR

PAGE_PROFILE, PAGE_CAPTURE, PAGE_BOUNDARY, PAGE_EDGES, PAGE_FILL = range(5)


class ProfileNamePage(QWizardPage):
    def __init__(self, wizard: "KeyboardCalibrationWizard"):
        super().__init__()
        self.setTitle("Step 0 - Name this camera profile")
        self.setSubTitle(
            "Saved under data/keyboard-profile/<name>/ - use a different name per camera/keyboard "
            "setup so you can switch between them later."
        )
        self._wizard = wizard

        self.name_edit = QLineEdit(wizard.cfg.active_keyboard_profile or "default")
        self.name_edit.textChanged.connect(lambda _: self.completeChanged.emit())

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Profile name:"))
        layout.addWidget(self.name_edit)

    @staticmethod
    def _sanitize(raw: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", raw.strip()).strip("_")

    def isComplete(self) -> bool:
        return bool(self._sanitize(self.name_edit.text()))

    def validatePage(self) -> bool:
        self._wizard.state["keyboard_profile_name"] = self._sanitize(self.name_edit.text())
        return True


class CapturePage(QWizardPage):
    def __init__(self, wizard: "KeyboardCalibrationWizard"):
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

    def _capture(self):
        frame = self._wizard.latest_live_frame
        if frame is not None:
            self._wizard.state["frame"] = frame.copy()
            self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._wizard.state.get("frame") is not None


class BoundaryPage(QWizardPage):
    def __init__(self, wizard: "KeyboardCalibrationWizard"):
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

    def initializePage(self):
        self.points = []
        self._redraw()

    def _on_click(self, x, y):
        if len(self.points) >= 2:
            return
        self.points.append((x, y))
        self._redraw()
        self.completeChanged.emit()

    def _reset(self):
        self.points = []
        self._redraw()
        self.completeChanged.emit()

    def _redraw(self):
        frame = self._wizard.state["frame"].copy()

        for p in self.points:
            cv2.circle(frame, p, 6, (0, 255, 0), -1)

        if len(self.points) == 2:
            (x1, y1), (x2, y2) = self.points
            cv2.rectangle(frame, (min(x1, x2), min(y1, y2)), (max(x1, x2), max(y1, y2)), (0, 255, 0), 2)
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
    def __init__(self, wizard: "KeyboardCalibrationWizard"):
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

    def initializePage(self):
        frame = self._wizard.state["frame"]
        bx, by, bw, bh = self._wizard.state["boundary"]
        self._crop = frame[by : by + bh, bx : bx + bw].copy()
        self._wizard.state["crop"] = self._crop

        cfg = self._wizard.cfg.keyboard_detection
        self.low_slider.setValue(cfg.canny_low)
        self.high_slider.setValue(cfg.canny_high)
        self._update()

    def _update(self):
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
        cfg = self._wizard.cfg.keyboard_detection
        cfg.canny_low = self._wizard.state["canny_low"]
        cfg.canny_high = self._wizard.state["canny_high"]
        self._wizard.cfg.save()
        return True


class FillKeysPage(QWizardPage):
    def __init__(self, wizard: "KeyboardCalibrationWizard"):
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

    def initializePage(self):
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

    def _on_mode_changed(self):
        if self.fill_wizard is not None:
            self.fill_wizard.mode = "white" if self.white_radio.isChecked() else "black"

    def _on_click(self, x, y):
        key = self.fill_wizard.try_fill(x, y)
        if key is None:
            self.status_label.setText("Click ignored (edge / already filled).")
        else:
            self.status_label.setText(f"Filled key {key.id + 1} ({key.kind}).")
        self._redraw()

    def _undo(self):
        self.fill_wizard.undo()
        self._redraw()

    def _reset(self):
        self.fill_wizard.reset()
        self._redraw()

    def _redraw(self):
        display = self._wizard.state["crop"].copy()

        if self.edges_check.isChecked():
            edges = self._wizard.state["edges"]
            display[edges > 0] = (0, 0, 255)

        self.fill_wizard.overlay(display)
        self.view.set_frame(display)
        self.setSubTitle(f"Keys marked so far: {len(self.fill_wizard.keys)}")

    def validatePage(self) -> bool:
        frame = self._wizard.state["frame"]
        h, w = frame.shape[:2]
        bx, by, bw, bh = self._wizard.state["boundary"]

        # The fill wizard's key_map is crop-local pixels - paste it into a
        # full-frame-sized map at the boundary's offset so pixel lookups
        # later can use raw camera coordinates directly.
        key_map = np.zeros((h, w), dtype=np.uint8)
        key_map[by : by + bh, bx : bx + bw] = self.fill_wizard.build_key_map()

        template = KeyboardTemplate(
            frame_width=w,
            frame_height=h,
            region={"x": bx, "y": by, "w": bw, "h": bh},
            keys=list(self.fill_wizard.keys),
            key_map=key_map,
        )

        keyboard_profile_name = self._wizard.state["keyboard_profile_name"]
        template_path = DATA_DIR / keyboard_profile_name / "keyboard_template.json"
        template.save(template_path)

        self._wizard.cfg.active_keyboard_profile = keyboard_profile_name
        self._wizard.cfg.save()

        QMessageBox.information(
            self,
            "Saved",
            f"Saved {len(template.keys)} keys to {template_path}\nActive profile: {keyboard_profile_name}",
        )
        return True


class KeyboardCalibrationWizard(QWizard):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Keyboard Calibration Wizard")
        self.setOptions(QWizard.WizardOption.NoBackButtonOnLastPage)

        self.cfg = cfg
        self.detector = KeyboardDetector(cfg.keyboard_detection)
        self.camera = Camera(cfg.camera)
        self.state: dict = {}
        self.latest_live_frame = None

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

    def closeEvent(self, event) -> None:
        self._preview_timer.stop()
        self.camera.release()
        super().closeEvent(event)
