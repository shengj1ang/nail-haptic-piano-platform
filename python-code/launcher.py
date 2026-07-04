"""One hub window for the whole FingerAccuracy pipeline.

Click a button, get the corresponding tool as a sub-window - reusing the
exact same window classes that step1_keyboard_wizard.py, step2_midi_mapping.py,
demo_fingeraccuracy.py, and demo_keyboard_preview.py each already wrap, so none of those
scripts need to change (they stay usable standalone too).

Only one tool window is open at a time: opening another one closes whichever
is currently open first, since they all want exclusive access to the same
camera.
"""

import sys

from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.gui.calibration_wizard import KeyboardCalibrationWizard
from app.gui.finger_detector_window import FingerDetectorWindow
from app.gui.key_preview import KeyPreviewWindow
from app.gui.midi_mapping_wizard import MidiMappingWizard
from app.gui.recording_wizard import RecordingWizard

TOOLS = [
    ("Step 1 - Keyboard Calibration Wizard", KeyboardCalibrationWizard),
    ("Step 2 - MIDI Mapping Wizard", MidiMappingWizard),
    ("Step 3 - Song Recording Wizard", RecordingWizard),
    ("Main - Live Finger Detection", FingerDetectorWindow),
    ("Demo - Keyboard Preview", KeyPreviewWindow),
]


class LauncherWindow(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("FingerAccuracy")
        self.cfg = cfg
        self._current = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick a tool:"))

        for label, window_cls in TOOLS:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, cls=window_cls: self._open(cls))
            layout.addWidget(btn)

    def _open(self, window_cls) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None

        try:
            window = window_cls(self.cfg)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't open tool", str(e))
            return

        window.show()
        self._current = window

    def closeEvent(self, event) -> None:
        if self._current is not None:
            self._current.close()
        super().closeEvent(event)


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = LauncherWindow(cfg)
    window.resize(360, 220)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
