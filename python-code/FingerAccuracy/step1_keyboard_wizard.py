"""STEP 1 of the pipeline - Keyboard Calibration Wizard.

Marks out where every key of the physical piano/MIDI keyboard sits in the
camera's view, and saves that as a reusable profile under data/<name>/.
Later steps (hand/finger tracking, MIDI-note matching, ...) read that
profile instead of re-detecting the keyboard each time.

Launches a PyQt window - mouse and buttons only, no keyboard shortcuts. Run
this once per physical camera/keyboard setup; the saved profile stays valid
as long as neither one moves relative to the other.
"""

import sys

from PySide6.QtWidgets import QApplication

from fingeraccuracy.config import Config
from fingeraccuracy.gui.calibration_wizard import KeyboardCalibrationWizard


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    wizard = KeyboardCalibrationWizard(cfg)
    wizard.resize(960, 720)
    wizard.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
