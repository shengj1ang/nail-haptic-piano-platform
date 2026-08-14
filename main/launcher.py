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

Section 8 (Tele-training) is the one exception, and it has to be: a
student, a teacher and a relay have to run *simultaneously*, and the two
clients hold different cameras and different MIDI ports. Those entries
therefore start independent processes (see remote_guidance/
launcher_actions.py) instead of going through _open(), which would close
whatever was already running.
"""

import itertools
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Sections keep their numbered order and are split into this many
# columns, top-to-bottom then on to the next column. How many sections
# land in each column is COMPUTED from their sizes (see balance_columns)
# rather than fixed: dealing them out round-robin used to put the two
# six-button sections and Tele-training in the same column, making it
# far taller than its neighbours and setting the window height on its
# own.
SECTION_COLUMNS = 4

# Roughly how much vertical space a section's group box costs beyond its
# buttons - title, margins and the gap below it - expressed in button
# heights, so balance_columns can compare a two-button box against a
# six-button one meaningfully.
BOX_CHROME_WEIGHT = 1.5

APP_ICON = Path(__file__).resolve().parent / "app" / "assets" / "image" / "icon.png"

from app.config import Config
from app.gui.accelerometer_window import AccelerometerWindow
from app.gui.calibration_wizard import KeyboardCalibrationWizard
from app.gui.camera_selection_window import CameraSelectionWindow
from app.gui.cue_selection_window import CueSelectionWindow
from app.gui.experiment_session_window import ExperimentSessionWindow
from app.gui.finger_detector_window import FingerDetectorWindow
from app.gui.haptic_config_window import HapticConfigWindow
from app.gui.key_preview import KeyPreviewWindow
from app.gui.midi_mapping_wizard import MidiMappingWizard
from app.gui.pilot_schedule_window import PilotScheduleWindow
from app.gui.group_analysis_window import GroupAnalysisWindow
from app.gui.participant_analysis_window import ParticipantAnalysisWindow
from app.gui.quiz_analysis_window import QuizAnalysisWindow
from app.gui.recording_wizard import RecordingWizard
from app.gui.sequence_generator_window import SequenceGeneratorWindow
from app.gui.sequence_metrics_window import SequenceMetricsWindow
from app.gui.single_song_metrics_window import SingleSongMetricsWindow
from app.gui.validation_experiment_window import (
    AdhesionComparisonWindow,
    AmplitudeSweepWindow,
    FrequencySweepWindow,
    MotorAccDelayWindow,
    SpectrogramWindow,
)
from app.gui.wiring_guide_window import WiringGuideWindow
from remote_guidance.launcher_actions import (
    ProcessSpec,
    benchmark_spec,
    check_health,
    health_url,
    student_spec,
    teacher_spec,
    server_spec,
)
from remote_guidance.config import RemoteGuidanceConfig
from remote_guidance.setup_wizard import RemoteSetupWizard
from music_playback import PlaybackWindow
from student_quiz import QuizWindow
from student_quiz_haptic import HapticQuizWindow
from test_haptic_single_motor import SingleMotorHapticWindow
from test_haptic_vibrator import HapticTestWindow
from test_virtual_piano_led import PianoWindow

def section_weight(tools) -> float:
    """Approximate height of one section, in button heights."""
    return max(len(tools), 1) + BOX_CHROME_WEIGHT


def balance_columns(weights, columns: int):
    """Split an ordered list of section weights into `columns` runs whose
    tallest run is as short as possible.

    Runs are contiguous, so sections stay in their numbered order and the
    grid still reads 1, 2, 3, ... down each column and on to the next -
    only where the column breaks fall is chosen. Returns a list of index
    lists, one per column.

    Brute force over the possible cut positions: with nine sections and
    four columns that is 56 combinations, so an exact answer costs
    nothing and there is no heuristic to be wrong."""
    n = len(weights)
    if n == 0:
        return []
    columns = max(1, min(columns, n))

    best = None
    best_cost = None
    for cuts in itertools.combinations(range(1, n), columns - 1):
        bounds = (0, *cuts, n)
        runs = [list(range(bounds[i], bounds[i + 1])) for i in range(columns)]
        cost = max(sum(weights[j] for j in run) for run in runs)
        if best_cost is None or cost < best_cost:
            best, best_cost = runs, cost
    return best


class ProcessEntry:
    """A launcher button that starts its own process instead of opening a
    sub-window.

    Used only by the Tele-training section: those endpoints must be able
    to run alongside each other (and alongside a relay), which the
    single-sub-window rule cannot express. `action` names a method on
    LauncherWindow so the buttons stay declarative alongside the window
    classes."""

    def __init__(self, action: str):
        self.action = action


# (section heading, [(button label, window class or ProcessEntry), ...])
SECTIONS = [
    (
        "1. Initial Setup",
        [
            ("Wiring Guide", WiringGuideWindow),
            ("Camera Selection Wizard", CameraSelectionWindow),
            ("Keyboard Calibration Wizard", KeyboardCalibrationWizard),
            ("MIDI Mapping Wizard", MidiMappingWizard),
            ("Keyboard Profile Selection, Region Preview", KeyPreviewWindow),
            ("Visual Guidance Cue Selection", CueSelectionWindow),
            # Which actuator the rig drives (LRA/ERM) and each type's
            # default frequency/amp - the whole project's haptic
            # defaults come from here (config.json "haptic").
            ("Haptic Actuator Defaults", HapticConfigWindow),
        ],
    ),
    (
        "2. Feature Testing",
        [
            ("Live Finger Detection", FingerDetectorWindow),
            ("Virtual Piano + LED Test", PianoWindow),
            ("Haptic Vibrator Test", HapticTestWindow),
            ("Haptic Motor Bench", SingleMotorHapticWindow),
            ("Accelerometer Live View", AccelerometerWindow),
        ],
    ),
    (
        "3. Recording && Playback",
        [
            ("Song Recording Wizard", RecordingWizard),
            ("Song Playback", PlaybackWindow),
        ],
    ),
    (
        "4. Experiment Sequence Design",
        [
            ("Experiment Sequence Generator", SequenceGeneratorWindow),
            ("Sequence/Music Metrics", SequenceMetricsWindow),
            ("Single Song Complexity Evaluation", SingleSongMetricsWindow),
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
        # Remote guidance (method.tex "Tele-training Guidance Modes"):
        # every entry here starts an independent process, because the
        # student, the teacher and the relay run at the same time on
        # different devices.
        #
        # There is deliberately no settings entry. Each of the three owns
        # its own settings and has its own Settings button - a launcher
        # window showing all three roles' devices at once meant every
        # person was looking mostly at settings that were not theirs.
        "8. Tele-training",
        [
            # An ordinary sub-window, not a ProcessEntry: it claims the
            # camera and the MIDI keyboard, so the launcher's usual
            # one-tool-at-a-time rule is exactly what should apply to it.
            # It is setup, not settings - it writes only the
            # remote_guidance block and its own profile folder, and never
            # the values the formal experiment reads.
            ("Relay Server", ProcessEntry("launch_server")),
            ("Teacher Client", ProcessEntry("launch_teacher")), 
            ("Student Client", ProcessEntry("launch_student")),
            ("Tele-training Setup Wizard", RemoteSetupWizard),
            ("Network Latency Benchmark", ProcessEntry("launch_benchmark")),
        ],
    ),
    (
        # Small hardware-validation experiments (validation_experiments/)
        # that informed the platform's design constants - kept separate
        # from the Main User Study protocol.
        "9. Validation Experiments",
        [
            ("Actuator Spectrogram (ERM/LRA)", SpectrogramWindow),
            ("LRA Frequency Sweep (Resonance)", FrequencySweepWindow),
            ("LRA Amplitude Sweep (Intensity)", AmplitudeSweepWindow),
            ("Motor → ACC Delay (LRA/ERM)", MotorAccDelayWindow),
            ("Adhesion Vibration Comparison (LRA)", AdhesionComparisonWindow),
        ],
    ),
]

# Tools exempt from the one-tool-at-a-time rule, which exists because most
# of these windows claim the camera or the MIDI keyboard and two of them
# running at once would fight over the device.
#
# These two claim no hardware - they are read-only views over the exported
# CSVs - and they are the pair you actually want side by side, reading a
# participant's own numbers against the group they sit in. They coexist
# with each other and with whatever exclusive tool is open.
CONCURRENT_TOOLS = {
    ParticipantAnalysisWindow,
    GroupAnalysisWindow,
}

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
        self._current = None          # the one exclusive tool, if any
        self._concurrent = {}         # window_cls -> its open window
        self.remote = RemoteGuidanceConfig.load()

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

        # Independent vertical columns rather than a grid: a grid couples
        # every box in a row to the tallest one, so a short section
        # (e.g. "3. Recording & Playback", 2 buttons) sharing a row with a
        # tall one (5-6 buttons) was stretched to match and wasted the gap.
        # Columns let each box hug its own content, and the freed vertical
        # space flows to the sections that need it (e.g. "9. Validation").
        #
        # Which sections go in which column is balanced by size rather
        # than dealt out round-robin - see balance_columns.
        self.column_groups = balance_columns([section_weight(tools) for _, tools in SECTIONS], SECTION_COLUMNS)
        column_of = {index: column for column, group in enumerate(self.column_groups) for index in group}

        columns_row = QHBoxLayout()
        columns_row.setSpacing(12)
        column_layouts = []
        for _ in self.column_groups:
            col_widget = QWidget()
            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(0, 0, 0, 0)
            col_layout.setSpacing(12)
            columns_row.addWidget(col_widget, 1)
            column_layouts.append(col_layout)

        for i, (section_title, tools) in enumerate(SECTIONS):
            box = QGroupBox(section_title)
            # Hug content vertically so a short section stays short.
            box.setSizePolicy(QSizePolicy.Policy.Preferred,
                              QSizePolicy.Policy.Maximum)
            box_layout = QVBoxLayout(box)
            box_layout.setSpacing(8)
            for label, entry in tools:
                btn = QPushButton(label)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _checked=False, item=entry: self._activate(item))
                box_layout.addWidget(btn)
            if not tools:
                hint = QLabel("(coming soon)")
                hint.setObjectName("subtitle")
                box_layout.addWidget(hint)
            column_layouts[column_of[i]].addWidget(box)

        for col_layout in column_layouts:
            col_layout.addStretch(1)   # push boxes to the top of each column

        layout.addLayout(columns_row)
        layout.addStretch(1)

    def _activate(self, entry) -> None:
        if isinstance(entry, ProcessEntry):
            getattr(self, entry.action)()
        else:
            self._open(entry)

    def _open(self, window_cls) -> None:
        """Open a tool, honouring the one-tool-at-a-time rule - except for
        the tools in CONCURRENT_TOOLS, which may stay open alongside
        anything else (see that constant for why).

        A concurrent tool that is already open is raised rather than
        duplicated: a second copy would show the same data, and rebuilding
        an analysis takes seconds the user has already spent."""
        if window_cls in CONCURRENT_TOOLS:
            existing = self._concurrent.get(window_cls)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return
            window = self._create(window_cls)
            if window is None:
                return
            self._concurrent[window_cls] = window
        else:
            # Exclusive tools replace each other, but deliberately leave the
            # concurrent ones alone: those hold no hardware and closing them
            # would throw away an analysis the user is reading.
            if self._current is not None:
                self._current.close()
                self._current = None
            window = self._create(window_cls)
            if window is None:
                return
            self._current = window
        window.show()

    def _create(self, window_cls):
        try:
            return window_cls(self.cfg)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't open tool", str(e))
            return None

    # ------------------------------------------------------------------
    # Tele-training: independent processes
    # ------------------------------------------------------------------

    def _start_process(self, spec: ProcessSpec) -> bool:
        """Start one remote endpoint detached from this launcher.

        Detached on purpose: a session should survive the launcher being
        closed, and three endpoints have to coexist - neither of which
        works if they are children tied to this window's lifetime."""
        missing = spec.missing_script()
        if missing is not None:
            QMessageBox.warning(self, "Couldn't start " + spec.label, f"{missing} is missing.")
            return False

        ok, _pid = QProcess.startDetached(spec.program, spec.arguments, str(spec.working_directory))
        if not ok:
            QMessageBox.warning(
                self,
                "Couldn't start " + spec.label,
                f"The process did not start.\n\n{spec.command_line()}\n\n"
                f"Working directory: {spec.working_directory}",
            )
        return ok

    def launch_student(self) -> bool:
        return self._start_process(student_spec())

    def launch_teacher(self) -> bool:
        return self._start_process(teacher_spec())

    def launch_benchmark(self) -> bool:
        return self._start_process(benchmark_spec())

    def launch_server(self) -> bool:
        self.remote = RemoteGuidanceConfig.load()
        if check_health(health_url(self.remote)) is not None:
            QMessageBox.information(
                self,
                "Relay already running",
                "A relay is already answering on that address. Opening a second one would try to "
                "bind the same port and fail - stop the running one first if you meant to restart it.",
            )
            return False
        return self._start_process(server_spec(self.remote, gui=True))

    def closeEvent(self, event) -> None:
        """Closing the launcher ends the session: every window it opened
        goes with it, and so does anything those windows opened themselves.

        Sub-windows are the reason this cannot just close self._current.
        The quiz detail view opens per-event review windows, the experiment
        session opens a runner and a participant-facing cue screen, and each
        of those is a top-level window that on its own keeps the process
        alive after the launcher is gone - the app looks quit while still
        holding the camera. Sweeping every top-level widget catches them
        without the launcher having to know what each tool spawns.

        The detached tele-training processes are deliberately NOT touched:
        _start_process starts them precisely so a session survives the
        launcher, and killing a relay would drop a connected student.
        """
        for window in list(self._concurrent.values()):
            window.close()
        self._concurrent.clear()
        if self._current is not None:
            self._current.close()
            self._current = None
        for widget in QApplication.topLevelWidgets():
            if widget is not self:
                widget.close()
        super().closeEvent(event)
        # exit(0), not quit(): Qt 6's quit() asks every window to close
        # first and ABANDONS the shutdown if any one of them ignores the
        # request, so a single window refusing - a confirmation prompt, a
        # thread still winding down - would leave the process alive with no
        # launcher left to quit it. Every window has already been asked to
        # close politely above; this is the part that does not take no for
        # an answer.
        QApplication.exit(0)


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(APP_ICON)))  # on macOS this also sets the Dock icon
    window = LauncherWindow(cfg)
    # Four size-balanced columns (see balance_columns): wider than the
    # old three-column grid, but shorter, because no single column has to
    # carry the two six-button sections at once.
    window.resize(1180, 700)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
