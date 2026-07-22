"""Camera Selection Wizard.

There's no cross-platform "camera name" API - only numeric indices
(0, 1, 2, ...) that mean something different on every machine - so picking
the right one for config.json's camera.index by guessing is trial and
error. This window scans which indices currently open successfully,
lets the user preview each live before committing, and saves the chosen
index (plus the flip flags, since they're only meaningful once you can
see the effect) back into config.json via Config.save() - the same file
every other tool reads through Config.load(), so this only needs running
once per machine/camera setup.

The scan itself runs on a background thread (_CameraProbeWorker) with a
progress bar, rather than blocking the GUI thread - a closed backend can
take a noticeable moment per index to fail, so scanning even a handful of
indices synchronously would make the window appear to hang.

Opening a camera that turns out not to exist (unplugged, wrong index, in
use elsewhere) never raises here - see app.camera.Camera - so switching
through indices that don't work just shows "could not open", not a crash.
"""

from typing import List, Optional

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..camera import Camera, probe_camera_indices
from ..config import CameraConfig, Config
from .image_view import ImageView

MAX_CAMERA_INDEX = 8


class _CameraProbeWorker(QThread):
    """Runs probe_camera_indices() off the GUI thread, since a closed
    backend can take a noticeable moment per index to fail - scanning even
    a handful of indices synchronously would freeze the window."""

    progress = Signal(int, int)  # indices checked so far, total to check
    finished_scan = Signal(list)  # available camera indices

    def __init__(self, max_index: int = MAX_CAMERA_INDEX):
        super().__init__()
        self.max_index = max_index

    def run(self) -> None:
        available = probe_camera_indices(self.max_index, progress_callback=self.progress.emit)
        self.finished_scan.emit(available)


class CameraSelectionWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Camera Selection")
        self.cfg = cfg

        self._preview_camera: Optional[Camera] = None
        self._probe_worker: Optional[_CameraProbeWorker] = None

        self.index_combo = QComboBox()
        self.index_combo.currentIndexChanged.connect(self._refresh_preview)
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self._rescan)

        self.flip_h_check = QCheckBox("Flip horizontal")
        self.flip_h_check.toggled.connect(self._refresh_preview)
        self.flip_v_check = QCheckBox("Flip vertical")
        self.flip_v_check.toggled.connect(self._refresh_preview)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)

        self.view = ImageView()
        self.status_label = QLabel("Scanning for cameras...")
        self.status_label.setWordWrap(True)

        self.save_btn = QPushButton("Save as default camera")
        self.save_btn.clicked.connect(self._save)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Camera index:"))
        top_row.addWidget(self.index_combo, 1)
        top_row.addWidget(self.rescan_btn)

        flip_row = QHBoxLayout()
        flip_row.addWidget(self.flip_h_check)
        flip_row.addWidget(self.flip_v_check)
        flip_row.addStretch(1)

        # Everything except the progress bar and status text lives in one
        # container, so a scan in progress can hide it as a single unit -
        # while scanning, there's nothing valid for the user to look at or
        # click yet (no camera list, no preview).
        self.content_widget = QWidget()
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(top_row)
        content_layout.addLayout(flip_row)
        content_layout.addWidget(self.view)
        content_layout.addWidget(self.save_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.content_widget)
        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._rescan()

    # ------------------------------------------------------------------
    # Scanning (background thread, so the window never appears to hang)
    # ------------------------------------------------------------------

    def _rescan(self) -> None:
        if self._probe_worker is not None:
            return  # a scan is already running

        self._release_preview()
        self.content_widget.setVisible(False)
        self.status_label.setText("Scanning for cameras...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, MAX_CAMERA_INDEX)
        self.progress_bar.setValue(0)

        self._probe_worker = _CameraProbeWorker()
        self._probe_worker.progress.connect(self._on_scan_progress)
        self._probe_worker.finished_scan.connect(self._on_scan_finished)
        self._probe_worker.start()

    def _on_scan_progress(self, checked: int, total: int) -> None:
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(checked)

    def _on_scan_finished(self, available: List[int]) -> None:
        self._probe_worker = None
        self.progress_bar.setVisible(False)
        self.content_widget.setVisible(True)

        current = self.cfg.camera.index
        self.index_combo.blockSignals(True)
        self.index_combo.clear()
        for index in available:
            self.index_combo.addItem(f"Camera {index}", index)
        if isinstance(current, int) and current not in available:
            # Keep the already-configured index selectable even if this scan
            # didn't find it (e.g. it's mid-reconnect) instead of silently
            # dropping the user's existing choice.
            self.index_combo.addItem(f"Camera {current} (configured, not detected)", current)
        self.index_combo.blockSignals(False)

        if not available:
            self.status_label.setText("No cameras detected. Check the connection, then click Rescan.")
        else:
            self.status_label.setText(f"Found {len(available)} camera(s). Pick one below to preview it.")

        target_row = self.index_combo.findData(current)
        self.index_combo.setCurrentIndex(target_row if target_row >= 0 else 0)
        self.flip_h_check.setChecked(self.cfg.camera.flip_horizontal)
        self.flip_v_check.setChecked(self.cfg.camera.flip_vertical)
        self._refresh_preview()

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _current_preview_config(self) -> Optional[CameraConfig]:
        index = self.index_combo.currentData()
        if index is None:
            return None
        return CameraConfig(
            index=index,
            width=self.cfg.camera.width,
            height=self.cfg.camera.height,
            fps=self.cfg.camera.fps,
            flip_horizontal=self.flip_h_check.isChecked(),
            flip_vertical=self.flip_v_check.isChecked(),
        )

    def _release_preview(self) -> None:
        if self._preview_camera is not None:
            self._preview_camera.release()
            self._preview_camera = None

    def _refresh_preview(self) -> None:
        self._release_preview()

        preview_cfg = self._current_preview_config()
        if preview_cfg is None:
            self.save_btn.setEnabled(False)
            return

        self._preview_camera = Camera(preview_cfg)
        self.save_btn.setEnabled(True)
        if not self._preview_camera.is_opened:
            self.status_label.setText(
                f"Could not open camera {preview_cfg.index} - it may be disconnected or already in use elsewhere."
            )

    def _tick(self) -> None:
        if self._preview_camera is None:
            return
        frame = self._preview_camera.read()
        if frame is not None:
            self.view.set_frame(frame)

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _save(self) -> None:
        preview_cfg = self._current_preview_config()
        if preview_cfg is None:
            QMessageBox.warning(self, "No camera selected", "Pick a camera first.")
            return
        if self._preview_camera is None or not self._preview_camera.is_opened:
            reply = QMessageBox.question(
                self,
                "Camera not currently working",
                f"Camera {preview_cfg.index} isn't opening right now. Save it as the default anyway?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.cfg.camera.index = preview_cfg.index
        self.cfg.camera.flip_horizontal = preview_cfg.flip_horizontal
        self.cfg.camera.flip_vertical = preview_cfg.flip_vertical
        self.cfg.save()
        self.status_label.setText(f"Saved camera {preview_cfg.index} as the default in config.json.")

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self._probe_worker is not None:
            self._probe_worker.wait()
        self._release_preview()
        super().closeEvent(event)
