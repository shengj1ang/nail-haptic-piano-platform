"""STEP 3 of the pipeline - Song Recording Wizard.

Records a teacher's performance (video + MIDI) of a song, flashes every
key's backlight once for video/MIDI sync right as the recording starts,
then computes which finger played each note and saves everything under
data/music/<song title>/ - see app/music_recording.py for the exact file
layout.
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from app.gui.recording_wizard import RecordingWizard


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = RecordingWizard(cfg)
    window.resize(1000, 860)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
