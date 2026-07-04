"""Main app - real-time finger-accuracy detection.

Combines everything the earlier steps set up:
  - the keyboard profile from step1_keyboard_wizard.py (where every key is,
    pixel-exact)
  - the MIDI mapping from step2_midi_mapping.py (which note each key sends)
  - live MediaPipe hand tracking

into one live view: the colored keyboard overlay and the hand skeleton are
drawn together, and every MIDI note-on is resolved to the fingertip that
was on the corresponding key when it fired (or the closest one, if none
were exactly inside it - see fingeraccuracy/finger_matching.py).

Left hand thumb..pinky = L1..L5, right hand thumb..pinky = R1..R5.
"""

import sys

from PySide6.QtWidgets import QApplication

from fingeraccuracy.config import Config
from fingeraccuracy.gui.finger_detector_window import FingerDetectorWindow


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = FingerDetectorWindow(cfg)
    window.resize(1000, 800)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
