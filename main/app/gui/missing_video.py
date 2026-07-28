"""Shared user-facing notice for when a quiz's raw performance video is
absent from this checkout.

The raw performance videos (performance.mp4) are deliberately not
committed to the repository - they are large and are gitignored - so any
feature that reads the video (finger matching from video, LED-sync
detection, the per-event review clip) cannot run from a clone alone.
Rather than fail with a cryptic OpenCV/"file not found" error, the
video-dependent windows check first and show this one consistent
explanation, pointing the user at Imperial College London for the full
dataset.
"""

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

MISSING_VIDEO_TITLE = "Performance video not available"


def missing_video_message(count: int = 1) -> str:
    """The standard English explanation. `count` lets a batch action say
    how many quizzes are affected while keeping the wording identical."""
    subject = (
        "This feature needs the raw performance video, which is not present "
        "in this repository."
        if count == 1
        else f"This feature needs the raw performance videos, and {count} of "
        "the selected quizzes have none in this repository."
    )
    return (
        f"{subject}\n\n"
        "Because of their size, the recorded videos are not uploaded to the "
        "repository. To obtain the complete dataset, please contact the "
        "relevant department at Imperial College London."
    )


def require_video(parent, video_path) -> bool:
    """True if the video exists; otherwise show the standard notice and
    return False. Callers abort the video-dependent action on False."""
    if Path(video_path).exists():
        return True
    QMessageBox.information(parent, MISSING_VIDEO_TITLE, missing_video_message())
    return False
