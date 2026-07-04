"""Data Analysis - (re-)run finger-matching over any saved quiz
(data/quiz/<name>/) and see its Note Accuracy / Timing Error / Finger
Accuracy.

This is the standalone entry point for
app/gui/quiz_analysis_window.py - the same window student_quiz.py opens
automatically right after a quiz finishes.
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from app.gui.quiz_analysis_window import QuizAnalysisWindow


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = QuizAnalysisWindow(cfg)
    window.resize(700, 500)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
