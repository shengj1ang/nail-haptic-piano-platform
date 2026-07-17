"""Manual video-sync alignment window - opened from Quiz Analysis's
per-row "Video Sync" button.

Shows three consecutive frames of the recording's first seconds side by
side (previous / current / next), with a frame-precision slider. The task:
put the exact frame where the sync LED first lights up in the middle -
the previous frame must still be dark. On open, the automatic flash
detection runs and the slider jumps to its best guess, so usually this is
just a visual confirm-and-save.

Saving writes raw/sync_align.json with method "manual"
(app.sync_led.ALIGN_FILENAME); every later analysis of this quiz then maps
MIDI timestamps to frames through that manually confirmed anchor and never
re-detects over it.
"""

from typing import List, Optional

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..music_recording import SyncInfo
from ..quiz import RAW_SYNC_FILENAME, RAW_VIDEO_FILENAME, quiz_raw_dir
from ..sync_led import (
    ALIGN_FILENAME,
    anchor_from_flash_frame,
    compute_sync_anchor,
    load_sync_alignment,
)

PRELOAD_SECONDS = 5.0
THUMB_WIDTH = 360  # three across plus margins stays under ~1200 px


class VideoSyncWindow(QMainWindow):
    saved = Signal(str)  # quiz name, emitted after sync_align.json is written

    def __init__(self, quiz_name: str, keyboard_profile_name: str):
        super().__init__()
        self.quiz_name = quiz_name
        self.keyboard_profile_name = keyboard_profile_name
        self.setWindowTitle(f"Video Sync - {quiz_name}")

        raw_dir = quiz_raw_dir(quiz_name)
        self.video_path = raw_dir / RAW_VIDEO_FILENAME
        self.align_path = raw_dir / ALIGN_FILENAME
        self.sync = SyncInfo.load(raw_dir / RAW_SYNC_FILENAME)
        if not self.sync.led_on_time:
            raise RuntimeError("sync.json has no led_on_time - nothing to align against")

        self.fps, self.frames = self._load_frames()
        if len(self.frames) < 3:
            raise RuntimeError("video too short to preview")

        # --- widgets ---------------------------------------------------
        self.thumb_labels: List[QLabel] = []
        captions = [
            "Previous frame (LED should still be OFF)",
            "Make sure THIS frame is the exact led_on moment - the previous frame must still be dark",
            "Next frame (LED should stay ON)",
        ]
        thumbs_row = QHBoxLayout()
        for i, caption in enumerate(captions):
            img = QLabel()
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap_label = QLabel(caption)
            cap_label.setWordWrap(True)
            cap_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap_label.setFixedWidth(THUMB_WIDTH)
            if i == 1:
                cap_label.setStyleSheet("font-weight: bold; color: #c03030;")
            col = QVBoxLayout()
            col.addWidget(img)
            col.addWidget(cap_label)
            thumbs_row.addLayout(col)
            self.thumb_labels.append(img)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(self.frames) - 1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._show_frame)

        prev_btn = QPushButton("◀ Prev frame")
        prev_btn.clicked.connect(lambda: self.slider.setValue(self.slider.value() - 1))
        next_btn = QPushButton("Next frame ▶")
        next_btn.clicked.connect(lambda: self.slider.setValue(self.slider.value() + 1))
        auto_btn = QPushButton("Auto-detect")
        auto_btn.clicked.connect(self._auto_detect)
        save_btn = QPushButton("Save (manual alignment)")
        save_btn.clicked.connect(self._save)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(prev_btn)
        controls.addWidget(next_btn)
        controls.addStretch(1)
        controls.addWidget(auto_btn)
        controls.addWidget(save_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(thumbs_row)
        layout.addWidget(self.slider)
        layout.addLayout(controls)
        layout.addWidget(self.info_label)
        self.setCentralWidget(central)

        # --- initial position ------------------------------------------
        # A saved alignment wins; else run detection; else the software
        # expectation. Either way the user lands right where the flash
        # should be and only has to nudge if it's off.
        start_frame = None
        existing = load_sync_alignment(raw_dir)
        if existing is not None and existing.flash_on_frame is not None:
            start_frame = existing.flash_on_frame
            self._detection_note = f"saved alignment ({existing.method})"
        else:
            start_frame = self._detect(quiet=True)
            self._detection_note = "auto-detection" if start_frame is not None else "software-timestamp estimate (detection failed)"
        if start_frame is None:
            start_frame = int(round((self.sync.led_on_time - self.sync.video_start_time) * self.fps))
        self.slider.setValue(min(max(start_frame, 0), len(self.frames) - 1))
        self._show_frame(self.slider.value())

    # ------------------------------------------------------------------

    def _load_frames(self):
        cap = cv2.VideoCapture(str(self.video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = []
        for _ in range(int(PRELOAD_SECONDS * fps)):
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            scale = THUMB_WIDTH / w
            frames.append(cv2.resize(frame, (THUMB_WIDTH, int(h * scale))))
        cap.release()
        return fps, frames

    def _pixmap(self, idx: int) -> Optional[QPixmap]:
        if not (0 <= idx < len(self.frames)):
            return None
        frame = cv2.cvtColor(self.frames[idx], cv2.COLOR_BGR2RGB)
        h, w, _ = frame.shape
        return QPixmap.fromImage(QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888))

    def _show_frame(self, idx: int) -> None:
        for offset, label in zip((-1, 0, 1), self.thumb_labels):
            pm = self._pixmap(idx + offset)
            if pm is None:
                label.clear()
                label.setText("(no such frame)")
            else:
                label.setPixmap(pm)
        expected = (self.sync.led_on_time - self.sync.video_start_time) * self.fps
        offset_s = (idx - expected) / self.fps
        self.info_label.setText(
            f"Frame {idx} / {len(self.frames) - 1}  |  video time {idx / self.fps:.3f}s  |  "
            f"offset vs software timestamps {offset_s:+.3f}s  |  initial position from: {self._detection_note}"
        )

    # ------------------------------------------------------------------

    def _detect(self, quiet: bool = False) -> Optional[int]:
        anchor = compute_sync_anchor(self.video_path, self.sync, self.keyboard_profile_name)
        if anchor.method == "led":
            return anchor.flash_on_frame
        if not quiet:
            QMessageBox.warning(self, "Auto-detection failed", anchor.reason)
        return None

    def _auto_detect(self) -> None:
        frame = self._detect()
        if frame is not None:
            self._detection_note = "auto-detection"
            self.slider.setValue(min(max(frame, 0), len(self.frames) - 1))

    def _save(self) -> None:
        anchor = anchor_from_flash_frame(self.slider.value(), self.fps, self.sync, method="manual")
        anchor.save(self.align_path)
        self._detection_note = "saved alignment (manual)"
        self._show_frame(self.slider.value())
        self.info_label.setText(self.info_label.text() + "  ✔ saved")
        self.saved.emit(self.quiz_name)
