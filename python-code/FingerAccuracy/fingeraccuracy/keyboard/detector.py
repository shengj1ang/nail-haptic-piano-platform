"""Canny edge detection, tuned per-profile in step1_keyboard_wizard.py.

Automatic Hough-line keyboard/key detection used to live here too, but it
was too fragile against real camera distortion and got replaced by manual,
click-based calibration (see wizard.py) - this module is now just the edge
map that calibration's paint-bucket fill uses as its walls.
"""

from typing import Optional

import cv2
import numpy as np

from ..config import KeyboardDetectionConfig


class KeyboardDetector:
    def __init__(self, cfg: KeyboardDetectionConfig):
        self.cfg = cfg

    def compute_edges(self, gray: np.ndarray, low: Optional[int] = None, high: Optional[int] = None) -> np.ndarray:
        low = self.cfg.canny_low if low is None else low
        high = self.cfg.canny_high if high is None else high
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, low, high)
        return cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
