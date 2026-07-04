"""Demo / utility - Keyboard Key Preview.

Not a pipeline step - just a sanity-check viewer. PyQt window with a
dropdown listing every profile found under data/keyboard-profile/ (each one saved by
step1_keyboard_wizard.py). Pick one and its keys are colored live on the
camera feed, so you can confirm the calibration still lines up with the
physical keyboard. If the profile also has a midi_mapping.json (from
step2_midi_mapping.py), you can switch the key labels to show MIDI note
names instead of raw key numbers.

No detection happens here - it only redraws whichever profile's saved
key_map is selected, so this stays correct only as long as the camera and
keyboard haven't moved since that profile was calibrated.
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from app.gui.key_preview import KeyPreviewWindow


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = KeyPreviewWindow(cfg)
    window.resize(960, 720)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
