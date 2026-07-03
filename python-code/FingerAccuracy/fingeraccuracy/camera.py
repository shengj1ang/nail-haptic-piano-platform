"""Thin wrapper around cv2.VideoCapture driven by CameraConfig."""

import cv2

from .config import CameraConfig


class Camera:
    def __init__(self, cfg: CameraConfig):
        self._cfg = cfg
        self._cap = cv2.VideoCapture(cfg.index)

        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera index {cfg.index}")

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self._cap.set(cv2.CAP_PROP_FPS, cfg.fps)

    def read(self):
        ok, frame = self._cap.read()
        if not ok:
            return None

        if self._cfg.flip_vertical and self._cfg.flip_horizontal:
            frame = cv2.flip(frame, -1)
        elif self._cfg.flip_vertical:
            frame = cv2.flip(frame, 0)
        elif self._cfg.flip_horizontal:
            frame = cv2.flip(frame, 1)

        return frame

    def release(self) -> None:
        self._cap.release()

    def __enter__(self) -> "Camera":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
