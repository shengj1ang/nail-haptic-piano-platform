"""Experiment Sequence Generator.

Builds the constrained bimanual motor-sequence stimuli used by the
main user study (final_report_2026/method/method.tex, §"Controlled
Stimulus Sequence Generator"): 30 single-key cue events per sequence,
difficulty levels alpha/beta/gamma defined by measurable constraints on
D = (C_m, C_s, C_c), matched families per level, and built-in difficulty
validation. Saves each sequence as a "song" under data/sequence/ - the
same layout music_recording_wizard.py produces from a real recording
under data/music/, so a generated sequence is immediately playable in
music_playback.py and usable as a quiz in student_quiz.py /
student_quiz_haptic.py. See app/sequence_generator.py for the generation
logic, app/stimulus_validation.py for the difficulty validation, and
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
