"""QLabel that shows a BGR numpy frame (scaled to fit the screen) and reports
clicks translated back into the original frame's pixel coordinates."""

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel


def bgr_to_qpixmap(frame: np.ndarray) -> QPixmap:
    rgb = np.ascontiguousarray(frame[:, :, ::-1])
    h, w, ch = rgb.shape
    qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


def _default_max_size() -> tuple[int, int]:
    screen = QApplication.primaryScreen()
    if screen is None:
        return 900, 560

    geo = screen.availableGeometry()
    # Leave room for the wizard's title, subtitle, controls below the image,
    # and its Back/Next/Cancel buttons.
    return int(geo.width() * 0.6), int(geo.height() * 0.5)


class ImageView(QLabel):
    clicked = Signal(int, int)

    def __init__(self, parent=None, max_size: tuple[int, int] | None = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 240)
        self.max_size = max_size or _default_max_size()
        self._scale = 1.0

    def set_frame(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        max_w, max_h = self.max_size
        self._scale = min(max_w / w, max_h / h, 1.0)

        display_frame = frame
        if self._scale < 1.0:
            display_frame = cv2.resize(
                frame, (int(w * self._scale), int(h * self._scale)), interpolation=cv2.INTER_AREA
            )

        pixmap = bgr_to_qpixmap(display_frame)
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.pixmap() is not None:
            pos = event.position().toPoint()
            x = int(pos.x() / self._scale)
            y = int(pos.y() / self._scale)
            self.clicked.emit(x, y)
        super().mousePressEvent(event)
