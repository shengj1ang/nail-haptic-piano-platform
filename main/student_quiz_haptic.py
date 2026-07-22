"""Student Quiz - Haptic Guidance.

Identical to student_quiz.py (see its module docstring) except which
finger to press is conveyed by a vibration motor (app/haptic_cue.py)
instead of the on-screen cue window - everything else (LED key cueing,
timeout/countdown, recording, analysis, results) is the same QuizWindow,
just constructed with guidance_type="haptic".
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from student_quiz import QuizWindow


class HapticQuizWindow(QuizWindow):
    """QuizWindow pinned to guidance_type="haptic" - so the launcher (which
    only ever constructs a tool as window_cls(cfg)) can offer it as its own
    button alongside the visual one."""

    def __init__(self, cfg: Config):
        super().__init__(cfg, guidance_type="haptic")


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = HapticQuizWindow(cfg)
    window.resize(1000, 780)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
