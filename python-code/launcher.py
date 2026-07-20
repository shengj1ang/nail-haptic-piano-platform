"""One hub window for the whole platform.

This is meant to grow into a multi-modal learning-assistant platform;
Click a button, get the corresponding tool as a sub-window - reusing the exact same
window classes each entry-point script (setup_keyboard_wizard.py,
setup_midi_mapping_wizard.py, test_keyboard_preview.py, test_finger_accuracy.py,
test_virtual_piano_led.py, music_recording_wizard.py, music_playback.py) already
wraps, so none of those scripts need to change (they stay usable standalone
too). Tools are grouped into the same three stages a teacher/researcher
actually moves through: set up a keyboard once, test that it works, then
record and replay songs with it.

Only one tool window is open at a time: opening another one closes whichever
is currently open first, since several of them want exclusive access to the
same camera.
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# Sections are laid out left-to-right, top-to-bottom in a grid this many
# columns wide, instead of one long vertical stack that runs off the
# screen once there are more than a handful of sections/buttons.
SECTION_COLUMNS = 2

APP_ICON = Path(__file__).resolve().parent / "app" / "assets" / "image" / "icon.png"

from app.config import Config
from app.gui.calibration_wizard import KeyboardCalibrationWizard
from app.gui.camera_selection_window import CameraSelectionWindow
from app.gui.cue_selection_window import CueSelectionWindow
from app.gui.experiment_session_window import ExperimentSessionWindow
from app.gui.finger_detector_window import FingerDetectorWindow
from app.gui.key_preview import KeyPreviewWindow
from app.gui.midi_mapping_wizard import MidiMappingWizard
from app.gui.pilot_schedule_window import PilotScheduleWindow
from app.gui.group_analysis_window import GroupAnalysisWindow
from app.gui.participant_analysis_window import ParticipantAnalysisWindow
from app.gui.quiz_analysis_window import QuizAnalysisWindow
from app.gui.recording_wizard import RecordingWizard
from app.gui.sequence_generator_window import SequenceGeneratorWindow
from app.gui.sequence_metrics_window import SequenceMetricsWindow
from music_playback import PlaybackWindow
from student_quiz import QuizWindow
from student_quiz_haptic import HapticQuizWindow
from test_haptic_vibrator import HapticTestWindow
from test_virtual_piano_led import PianoWindow

# (section heading, [(button label, window class), ...])
SECTIONS = [
    (
        "1. Initial Setup",
        [
            ("Camera Selection Wizard", CameraSelectionWindow),
            ("Keyboard Calibration Wizard", KeyboardCalibrationWizard),
            ("MIDI Mapping Wizard", MidiMappingWizard),
            ("Visual Guidance Cue Selection", CueSelectionWindow),
        ],
    ),
    (
        "2. Feature Testing",
        [
            ("Keyboard Region Preview", KeyPreviewWindow),
            ("Live Finger Detection", FingerDetectorWindow),
            ("Virtual Piano + LED Test", PianoWindow),
            ("Haptic Vibrator Test", HapticTestWindow),
        ],
    ),
    (
        "3. Experiment Sequence Design",
        [
            ("Experiment Sequence Generator", SequenceGeneratorWindow),
            ("Sequence/Music Metrics", SequenceMetricsWindow),
        ],
    ),
    (
        "4. Recording && Playback",
        [
            ("Song Recording Wizard", RecordingWizard),
            ("Song Playback", PlaybackWindow),
        ],
    ),
    (
        "5. Practice && Assessment",
        [
            ("Quiz - Visual Guidance", QuizWindow),
            ("Quiz - Haptic Guidance", HapticQuizWindow),
        ],
    ),
    (
        "6. Main User Study",
        [
            ("Participant Trial Schedule", PilotScheduleWindow),
            # Opens three windows together: the session controller, the
            # trial runner, and the persistent participant-facing cue screen.
            ("Formal Experiment Session", ExperimentSessionWindow),
        ],
    ),
    (
        "7. Data Analysis",
        [
            ("Quiz Analysis", QuizAnalysisWindow),
            ("Participant Analysis", ParticipantAnalysisWindow),
            ("Group Analysis (Multi-Participant)", GroupAnalysisWindow),
        ],
    ),
    (
        # Placeholder - tele-training tools land here next (method.tex
        # "Tele-training Guidance Modes").
        "8. Tele-training",
        [],
    ),
]

STYLE_SHEET = """
QWidget#launcherRoot {
    background: #1e1f24;
}
QLabel#title {
    color: #f2f2f5;
}
QLabel#subtitle {
    color: #9a9ba5;
}
QGroupBox {
    color: #d8d9e0;
    font-weight: 600;
    font-size: 13px;
    border: 1px solid #35363e;
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px 10px 10px 10px;
    background: #26272e;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #7fb2ff;
}
QPushButton {
    text-align: left;
    padding: 9px 14px;
    border-radius: 6px;
    border: 1px solid #3a3b44;
    background: #2f303a;
    color: #eceef2;
}
QPushButton:hover {
    background: #3a3c48;
    border-color: #7fb2ff;
}
QPushButton:pressed {
    background: #24252c;
}
"""


class LauncherWindow(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setObjectName("launcherRoot")
        self.setWindowTitle("Multi-Modal Platform")
        self.setStyleSheet(STYLE_SHEET)
        self.cfg = cfg
        self._current = None

        title = QLabel("Multi-Modal Platform")
        title.setObjectName("title")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 8)
        title_font.setBold(True)
        title.setFont(title_font)

        subtitle = QLabel("• Platform built based on the ideas from dissertation \"Nail-Mounted Haptic Cues for Piano Training and Tele-training\"\n• Pick a tool below; only one runs at a time.")
        subtitle.setObjectName("subtitle")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        grid = QGridLayout()
        grid.setSpacing(12)
        for col in range(SECTION_COLUMNS):
            grid.setColumnStretch(col, 1)

        for i, (section_title, tools) in enumerate(SECTIONS):
            box = QGroupBox(section_title)
            box_layout = QVBoxLayout(box)
            box_layout.setSpacing(8)
            for label, window_cls in tools:
                btn = QPushButton(label)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _checked=False, cls=window_cls: self._open(cls))
                box_layout.addWidget(btn)
            if not tools:
                hint = QLabel("(coming soon)")
                hint.setObjectName("subtitle")
                box_layout.addWidget(hint)
            box_layout.addStretch(1)
            grid.addWidget(box, i // SECTION_COLUMNS, i % SECTION_COLUMNS)

        layout.addLayout(grid)
        layout.addStretch(1)

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
    app.setWindowIcon(QIcon(str(APP_ICON)))  # on macOS this also sets the Dock icon
    window = LauncherWindow(cfg)
    window.resize(760, 640)  # 8 sections in a 2-column grid need the extra height
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
