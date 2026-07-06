"""Thin wrapper around cv2.VideoCapture driven by CameraConfig.

Never raises just because the configured camera isn't actually available
(wrong or unplugged index, already in use by another app, etc.) - read()
simply returns None in that case, exactly like it already does for a
single dropped frame from a camera that opened fine. Every call site in
this codebase already treats a None frame as "nothing to show yet", so a
missing camera degrades to no preview instead of crashing the tool -
whether it's opened through the launcher (which only catches exceptions
raised while *building* a tool) or run as a standalone script (which
mostly doesn't catch anything at all).
"""

from typing import Callable, List, Optional

import cv2

from .config import CameraConfig


def probe_camera_indices(
    max_index: int = 8, progress_callback: Optional[Callable[[int, int], None]] = None
) -> List[int]:
    """Which of indices 0..max_index-1 currently open successfully - for a
    camera-picker UI to offer (see app.gui.camera_selection_window). Not
    fast: each candidate index is briefly opened and released just to test
    it, and a closed backend can take a noticeable moment to fail - hence
    progress_callback(indices_checked, max_index), for a caller that wants
    to show scan progress instead of appearing to hang (as
    app.gui.camera_selection_window's background probe thread does)."""
    available = []
    for i, index in enumerate(range(max_index), start=1):
        cap = cv2.VideoCapture(index)
        try:
            if cap.isOpened():
                available.append(index)
        finally:
            cap.release()
        if progress_callback is not None:
            progress_callback(i, max_index)
    return available


class Camera:
    def __init__(self, cfg: CameraConfig):
        self._cfg = cfg
        self._cap = cv2.VideoCapture(cfg.index)
        self.is_opened = self._cap.isOpened()

        if self.is_opened:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
            self._cap.set(cv2.CAP_PROP_FPS, cfg.fps)

    def read(self):
        if not self.is_opened:
            return None

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
