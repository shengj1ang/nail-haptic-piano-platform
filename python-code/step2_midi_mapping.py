"""STEP 2 of the pipeline - MIDI Key Mapping Wizard.

Press the physical keyboard's keys in the same 1, 2, 3, ... order they're
numbered in by step1_keyboard_wizard.py, and this reads the MIDI note
number each one actually sends. Saved as midi_mapping.json inside the
chosen profile's folder (data/keyboard-profile/<profile>/), alongside keyboard_template.json
and keyboard_key_map.png - so a profile fully describes both "where is each
key on camera" and "which MIDI note does it send".

Mouse and buttons only for navigation; the keyboard itself is what you type
on (through MIDI), not this window.
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from app.gui.midi_mapping_wizard import MidiMappingWindow


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = MidiMappingWindow(cfg)
    window.resize(960, 760)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
