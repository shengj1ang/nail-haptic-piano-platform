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

Capture and tracking are **two** threads, because a recording must not run
at MediaPipe's speed. Reading the camera and processing it in one loop
means the loop cycles at the slower of the two, and a 720p MediaPipe pass
is far slower than a camera frame - with both clients on one machine it
measured ~13 completed frames a second. The GUI's duplicate-frame catch-up
then padded the video back up to its declared 30fps by repeating whatever
frame it last saw, which is what a stuttering recording actually is: a real
student session came out 56.6% duplicated, freezing for up to 0.3s at a
time, against 0% for the same camera in the local quiz - whose capture loop
has no MediaPipe in it at all.

So the capture thread only reads the camera and publishes the frame, at the
camera's own rate; the tracking thread takes whatever the newest frame is
whenever it is free, and publishes hands and the annotated preview. A slow
tracker now costs preview and provisional-finger latency, never recorded
frames.

``keep_raw`` additionally carries an *unannotated* copy of each frame. The
teacher's preview draws the whole calibrated key map over the frame, and
that wash covers the hands wherever they are above the keys - recording it
would hand the offline finger pass a video MediaPipe cannot read. The copy
is made on this thread, before ``annotate`` runs, and only while a
recording actually wants it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("remote_guidance.vision")


@dataclass(frozen=True)
class VisionSnapshot:
    sequence: int
    frame: Any
    hands: Dict[str, Any]
    # The newest frame the camera produced, before anything drew on it -
    # None unless the worker was asked to keep it (see keep_raw below).
    # Newer than `frame`, which waits for a tracking pass; a recording
    # wants the freshest picture, not the one the hands belong to.
    raw_frame: Any = None


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
        keep_raw: bool = False,
    ):
        self.camera = camera
        self.tracker = tracker
        self.annotate = annotate
        self.name = name
        self.retry_delay_s = max(float(retry_delay_s), 0.001)
        self.frame_interval_s = 0.0 if not max_fps or max_fps <= 0 else 1.0 / float(max_fps)
        self.keep_raw = bool(keep_raw)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frame_ready = threading.Event()
        self._raw_ready = threading.Event()
        # The tracking thread keeps self.name: it is the one callers mean
        # by "off the GUI thread", and the one their tests name.
        self._thread: Optional[threading.Thread] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._snapshot = VisionSnapshot(sequence=0, frame=None, hands={})
        self._raw_frame: Any = None
        self._raw_sequence = 0
        self._closed = False
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._closed:
            raise RuntimeError("a closed vision worker cannot be restarted")
        self._stop.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_run, name=f"{self.name}-capture", daemon=True
        )
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._capture_thread.start()
        self._thread.start()

    def set_keep_raw(self, keep_raw: bool) -> None:
        """Start or stop carrying the unannotated frame on snapshots.

        Since capture and tracking split, the camera's frame is never
        drawn on at all - the overlay goes on a copy - so this is only
        about whether snapshot() bothers to attach it. Nothing has to be
        arranged in advance any more, and the next snapshot has it."""
        self.keep_raw = bool(keep_raw)

    def snapshot(self) -> VisionSnapshot:
        """Return immediately with the newest completed result.

        `frame` and `hands` are the newest finished tracking pass;
        `raw_frame`, when a recording asked for it, is the newest frame
        the camera has produced, which is usually newer still."""
        with self._lock:
            if not self.keep_raw:
                return self._snapshot
            return replace(self._snapshot, raw_frame=self._raw_frame)

    def latest_raw_frame(self) -> Any:
        """The newest camera frame, at capture rate. What a recording
        writes: it must not inherit the tracker's cadence."""
        with self._lock:
            return self._raw_frame

    def wait_for_frame(self, timeout: float = 3.0) -> VisionSnapshot:
        """Startup-only wait used before opening a video writer."""
        self._frame_ready.wait(timeout=max(float(timeout), 0.0))
        return self.snapshot()

    def _capture_run(self) -> None:
        """Read the camera and publish, and nothing else. Whatever the
        tracker is doing, this loop keeps the camera's own rate - which
        is the rate a recording is written at."""
        try:
            while not self._stop.is_set():
                frame_started = time.perf_counter()
                try:
                    frame = self.camera.read()
                except Exception as exc:  # noqa: BLE001 - a later frame may recover
                    message = str(exc)
                    if message != self.last_error:
                        log.warning("%s capture failed: %s", self.name, exc)
                    self.last_error = message
                    self._stop.wait(self.retry_delay_s)
                    continue
                if frame is None:
                    self._stop.wait(self.retry_delay_s)
                    continue

                with self._lock:
                    self._raw_sequence += 1
                    self._raw_frame = frame
                self._raw_ready.set()
                if self.frame_interval_s > 0:
                    self._stop.wait(max(0.0, self.frame_interval_s - (time.perf_counter() - frame_started)))
        finally:
            # The camera is this thread's alone once start() returns.
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing %s camera failed", self.name)

    def _run(self) -> None:
        """Track the newest captured frame, as often as it can.

        Frames captured while a pass is running are skipped rather than
        queued - guidance wants the newest hand state, and the recording
        already has every frame from the capture thread."""
        tracked_sequence = 0
        try:
            while not self._stop.is_set():
                with self._lock:
                    sequence, frame = self._raw_sequence, self._raw_frame
                if frame is None or sequence == tracked_sequence:
                    self._raw_ready.wait(self.retry_delay_s)
                    self._raw_ready.clear()
                    continue
                tracked_sequence = sequence
                try:
                    hands = self.tracker.process(frame)
                    # The capture thread's frame belongs to the recording;
                    # the overlay goes on a copy of it, never on it.
                    shown = frame
                    if self.annotate is not None:
                        shown = frame.copy()
                        self.annotate(shown, hands)
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
                        frame=shown,
                        hands=dict(hands),
                    )
                    self.last_error = None
                self._frame_ready.set()
        finally:
            # Native objects are normally created by the GUI thread just
            # before start(), but used only here afterward. Final cleanup
            # on their owning work thread also means a delayed process()
            # can unwind safely after close()'s bounded wait.
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
        # The tracking thread parks on this between frames.
        self._raw_ready.set()
        capture, thread = self._capture_thread, self._thread
        first_wait = min(max(float(timeout), 0.0), 0.5)
        if capture is not None and capture.is_alive():
            capture.join(timeout=first_wait)
        if capture is not None and capture.is_alive():
            # Only the capture thread can be stuck inside read(); releasing
            # the camera under it is the conventional way to unblock it.
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing %s camera to unblock capture failed", self.name)
            capture.join(timeout=max(float(timeout) - first_wait, 0.0))
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(float(timeout) - first_wait, 0.0))

        if capture is None and thread is None:
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing %s camera failed", self.name)
            try:
                self.tracker.close()
            except Exception:  # noqa: BLE001
                log.exception("closing %s tracker failed", self.name)
        elif thread is not None and thread.is_alive():
            # Do not close MediaPipe underneath an in-flight native call;
            # _run() owns eventual cleanup once process() returns.
            log.error("%s did not stop within %.1fs; tracker close deferred", self.name, timeout)
        if capture is None or not capture.is_alive():
            self._capture_thread = None
        if thread is None or not thread.is_alive():
            self._thread = None
