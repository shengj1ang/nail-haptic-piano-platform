"""Experiment Sequence Generator.

Builds the constrained motor-sequence stimuli used by the controlled
pilot study (final_report_2026/method/method.tex, "Sequence Design and
Difficulty Levels") and saves them as a "song" under data/music/ - the
same layout music_recording_wizard.py produces from a real recording, so
a generated sequence is immediately playable in music_playback.py and
usable as a quiz in student_quiz.py / student_quiz_haptic.py. See
app/sequence_generator.py for the generation logic and
app/gui/sequence_generator_window.py for this window.
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from app.gui.sequence_generator_window import SequenceGeneratorWindow


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = SequenceGeneratorWindow(cfg)
    window.resize(1150, 620)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
