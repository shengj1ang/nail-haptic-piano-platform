"""Shared QThread wrapper around app.offline.analyze_recording, used by
any GUI tool that needs to compute per-note finger matches from a
recorded video without freezing while it works through every frame (see
recording_wizard.py's Review page and student_quiz.py's post-quiz step).
"""

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..offline import analyze_recording


class AnalyzeWorker(QThread):
    """Runs app.offline.analyze_recording (one MediaPipe pass per video
    frame - the slow part) off the GUI thread, so the caller's window
    doesn't freeze while it works through a whole recording."""

    progress = Signal(int, int)  # frames_done, total_frames (total may be 0 if unknown)
    succeeded = Signal(list)  # List[Optional[FingerMatch]]
    failed = Signal(str)

    def __init__(self, video_path: Path, notes_path: Path, profile_name: str):
        super().__init__()
        self.video_path = video_path
        self.notes_path = notes_path
        self.profile_name = profile_name

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            # Emitting a cross-thread signal per frame is overkill for a
            # multi-minute recording - a few updates a second is plenty.
            if done % 3 == 0 or done == total:
                self.progress.emit(done, total)

        try:
            matches = analyze_recording(
                self.video_path, self.notes_path, self.profile_name, progress_callback=on_progress
            )
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.succeeded.emit(matches)
