"""Shared QThread wrappers around the slow offline video passes -
app.offline.analyze_recording (MediaPipe finger matching) and
app.review_video.render_review_video (annotated re-encode) - used by any
GUI tool that needs them without freezing while they work through every
frame (see recording_wizard.py's Review page and student_quiz.py's
post-quiz step).
"""

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from ..offline import analyze_recording
from ..review_video import render_review_video


class AnalyzeWorker(QThread):
    """Runs app.offline.analyze_recording (one MediaPipe pass per video
    frame - the slow part) off the GUI thread, so the caller's window
    doesn't freeze while it works through a whole recording."""

    progress = Signal(int, int)  # frames_done, total_frames (total may be 0 if unknown)
    succeeded = Signal(list)  # List[Optional[FingerMatch]]
    failed = Signal(str)

    def __init__(
        self,
        video_path: Path,
        notes_path: Path,
        keyboard_profile_name: str,
        sync_path: Optional[Path] = None,
        hands_out_path: Optional[Path] = None,
    ):
        super().__init__()
        self.video_path = video_path
        self.notes_path = notes_path
        self.keyboard_profile_name = keyboard_profile_name
        self.sync_path = sync_path
        self.hands_out_path = hands_out_path

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            # Emitting a cross-thread signal per frame is overkill for a
            # multi-minute recording - a few updates a second is plenty.
            if done % 3 == 0 or done == total:
                self.progress.emit(done, total)

        try:
            matches = analyze_recording(
                self.video_path,
                self.notes_path,
                self.keyboard_profile_name,
                progress_callback=on_progress,
                sync_path=self.sync_path,
                hands_out_path=self.hands_out_path,
            )
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.succeeded.emit(matches)


class ReviewVideoWorker(QThread):
    """Runs app.review_video.render_review_video (decode + annotate +
    re-encode, no MediaPipe) off the GUI thread."""

    progress = Signal(int, int)  # frames_done, total_frames (total may be 0 if unknown)
    succeeded = Signal(str)  # path of the written review video
    failed = Signal(str)

    def __init__(
        self,
        video_path: Path,
        out_path: Path,
        results: List,  # List[QuizResult], already analyzed
        keyboard_profile_name: str,
        sync_path: Optional[Path] = None,
        hands_path: Optional[Path] = None,
    ):
        super().__init__()
        self.video_path = video_path
        self.out_path = out_path
        self.results = results
        self.keyboard_profile_name = keyboard_profile_name
        self.sync_path = sync_path
        self.hands_path = hands_path

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            if done % 10 == 0 or done == total:
                self.progress.emit(done, total)
                # The annotate+encode loop is CPU-bound pure-Python/OpenCV
                # work that can hold the GIL for the whole ~20 s of a
                # re-render, starving the GUI thread so its progress bar
                # never repaints (it looks frozen). A short sleep here
                # releases the GIL a few times a second - enough for the GUI
                # to process the signal above and stay responsive - while
                # adding only tens of milliseconds to the whole render.
                self.usleep(300)

        try:
            out = render_review_video(
                self.video_path,
                self.out_path,
                self.results,
                self.keyboard_profile_name,
                sync_path=self.sync_path,
                hands_path=self.hands_path,
                progress_callback=on_progress,
            )
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.succeeded.emit(str(out))
