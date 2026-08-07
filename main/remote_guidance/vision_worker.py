"""Background camera + MediaPipe loop for the two Tele-training clients.

The local quiz historically performs ``camera.read()`` and
``HandTracker.process()`` on its GUI timer. That is tolerable for one local
window, but Tele-training commonly runs teacher and student processes on
the same computer. Under load, a native camera read or MediaPipe pass can
occupy either GUI thread long enough to delay a ready MIDI/network event.

``LatestVisionWorker`` owns no Qt object. It continuously produces one
latest-only frame/hand snapshot on a daemon thread; the GUI consumes that
snapshot without waiting. Dropping intermediate preview frames is
intentional: guidance needs the newest completed hand state, not a queue of
old video frames. Recording still writes at its declared time base from the
GUI's latest frame, using the existing duplicate-frame catch-up logic.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("remote_guidance.vision")


@dataclass(frozen=True)
class VisionSnapshot:
    sequence: int
    frame: Any
    hands: Dict[str, Any]


class LatestVisionWorker:
    """Continuously capture/process frames without blocking the GUI.

    ``camera`` must provide ``read()/release()`` and ``tracker`` must
    provide ``process()/close()``. They remain exclusively used by this
    worker between ``start()`` and ``close()``. ``annotate`` runs on the
    worker too, before a frame becomes visible to the GUI.
    """

    def __init__(
        self,
        camera: Any,
        tracker: Any,
        *,
        annotate: Optional[Callable[[Any, Dict[str, Any]], None]] = None,
        name: str = "remote-vision",
        retry_delay_s: float = 0.01,
        max_fps: Optional[float] = 30.0,
    ):
        self.camera = camera
        self.tracker = tracker
        self.annotate = annotate
        self.name = name
        self.retry_delay_s = max(float(retry_delay_s), 0.001)
        self.frame_interval_s = 0.0 if not max_fps or max_fps <= 0 else 1.0 / float(max_fps)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frame_ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot = VisionSnapshot(sequence=0, frame=None, hands={})
        self._closed = False
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._closed:
            raise RuntimeError("a closed vision worker cannot be restarted")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def snapshot(self) -> VisionSnapshot:
        """Return immediately with the newest completed result."""
        with self._lock:
            return self._snapshot

    def wait_for_frame(self, timeout: float = 3.0) -> VisionSnapshot:
        """Startup-only wait used before opening a video writer."""
        self._frame_ready.wait(timeout=max(float(timeout), 0.0))
        return self.snapshot()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                frame_started = time.perf_counter()
                try:
                    frame = self.camera.read()
                    if frame is None:
                        self._stop.wait(self.retry_delay_s)
                        continue
                    hands = self.tracker.process(frame)
                    if self.annotate is not None:
                        self.annotate(frame, hands)
                except Exception as exc:  # noqa: BLE001 - a later frame may recover
                    message = str(exc)
                    if message != self.last_error:
                        log.warning("%s frame failed: %s", self.name, exc)
                    self.last_error = message
                    self._stop.wait(self.retry_delay_s)
                    continue

                with self._lock:
                    self._snapshot = VisionSnapshot(
                        sequence=self._snapshot.sequence + 1,
                        frame=frame,
                        hands=dict(hands),
                    )
                    self.last_error = None
                self._frame_ready.set()
                if self.frame_interval_s > 0:
                    self._stop.wait(max(0.0, self.frame_interval_s - (time.perf_counter() - frame_started)))
        finally:
            # Native objects are normally created by the GUI thread just
            # before start(), but used only here afterward. Final cleanup
            # on their owning work thread also means a delayed process()
            # can unwind safely after close()'s bounded wait.
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing %s camera failed", self.name)
            try:
                self.tracker.close()
            except Exception:  # noqa: BLE001
                log.exception("closing %s tracker failed", self.name)

    def close(self, timeout: float = 3.0) -> None:
        """Stop the loop and release camera/tracker exactly once.

        A normal capture exits at the next frame boundary. If a backend
        is stuck inside ``read()``, releasing the camera after the first
        bounded join is the conventional way to unblock it; a second join
        then keeps tracker.close() from racing an active process call.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread = self._thread
        first_wait = min(max(float(timeout), 0.0), 0.5)
        if thread is not None and thread.is_alive():
            thread.join(timeout=first_wait)

        if thread is not None and thread.is_alive():
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing %s camera to unblock capture failed", self.name)
            thread.join(timeout=max(float(timeout) - first_wait, 0.0))

        if thread is None:
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing %s camera failed", self.name)
            try:
                self.tracker.close()
            except Exception:  # noqa: BLE001
                log.exception("closing %s tracker failed", self.name)
        elif thread.is_alive():
            # Do not close MediaPipe underneath an in-flight native call;
            # _run() owns eventual cleanup after camera release unblocks it.
            log.error("%s did not stop within %.1fs; tracker close deferred", self.name, timeout)
        if thread is None or not thread.is_alive():
            self._thread = None
