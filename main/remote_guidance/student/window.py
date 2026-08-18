"""Student Client - receives remote guidance, cues it locally, records
and scores what was played.

Everything hardware happens here, on the student's own machine: camera,
MIDI keyboard, LED backlight and the nail-mounted actuators. The relay
only ever sees note/finger events.

Reused wholesale rather than reimplemented:

    app.camera.Camera / app.hand_tracking.HandTracker  live finger pass
    app.music_recording.RawMidiRecorder, SyncInfo      capture + sync mark
    app.gui.cue_window.ScreenCueOutput                 the visual cue
    app.haptic_cue.HapticCueOutput                     the vibration cue
    profile_led_mapper + note_led_map                  the key backlight
    app.quiz.QuizResult / save_quiz_results / summarize scoring + storage
    app.offline.analyze_recording (via AnalyzeWorker)  the final pass
    app.gui.quiz_analysis_window.QuizAnalysisWindow    post-session review

No device is opened until guidance actually starts. Signing in, choosing
a room and arriving on the session page touch no hardware: the camera,
MediaPipe, the LED strip, MIDI port and local note-audio stream are all
claimed by "Ready for guidance" and released when the session ends, on
"Change room" and on close. Audio consumes the recorder's existing MIDI
events, so it never opens a second MIDI connection.

The LED strip has no port to pick - it is auto-detected and connected at
that same moment, and a failure to find it is a warning rather than a
refusal. "Connect LED" stays on the page as a manual override for
checking the wiring beforehand.

A session is named by the teacher and by nobody else. "Ready for guidance"
opens the camera, MediaPipe, the keyboard, the LED strip and the cue
outputs, and stops there; the teacher's `session.start` brings the name and
the instant to begin, and only then is a video writer opened, under
`data/quiz/<that name>/`. The teacher records the same lesson under
`data/music/<that name>/`, so the two halves share one name from the first
frame and nothing is renamed afterwards.

Which camera, MIDI port and keyboard profile this client uses is set from
its own **Settings** button (remote_guidance/settings_window.py), not
from the launcher.

The session's own rules - queueing, timestamps, reaction time - live in
session.py, which has no Qt in it. This file is the wiring: devices in,
Qt signals out, timer tick driving both.

Threading: the WebSocket runs on its own threads inside
remote_guidance.qt_bridge and reaches this window only as Qt signals, so
nothing here blocks on the network.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import profile_led_mapper
from app.camera import Camera
from app.config import Config
from app.finger_matching import match_note_to_finger
from app.gui.analyze_worker import AnalyzeWorker
from app.gui.cue_window import CUE_STYLES, DEFAULT_CUE_STYLE, ScreenCueOutput
from app.gui.image_view import ImageView
from app.gui.quiz_analysis_window import QuizAnalysisWindow
from app.hand_tracking import HandTracker, draw_hands
from app.haptic_cue import HapticCueOutput
from app.keyboard.midi_mapping import MidiMapping, note_name
from app.keyboard.template import KeyboardTemplate
from app.midi import MidiEvent, save_midi_log
from app.music_recording import RawMidiRecorder, SyncInfo, save_raw_midi_log
from app.profiles import DATA_DIR as PROFILE_DATA_DIR
from app.quiz import (
    META_FILENAME,
    QUIZ_DATA_DIR,
    RAW_MIDI_FILENAME,
    RAW_NOTES_FILENAME,
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    QuizMeta,
    quiz_dir,
    quiz_raw_dir,
    sanitize_quiz_name,
    save_quiz_results,
    summarize,
)
from common.led_controller import LEDArrayController
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import WHITE_LEDS

from ..config import RemoteGuidanceConfig, student_config
from ..client_styles import STUDENT_STATUS_STYLES, STUDENT_STYLE_SHEET
from ..cue_outputs import build_student_cue, wants_haptic, wants_visual
from ..gui_common import STATUS_STYLES, StageWindow, header_label, room_summary
from ..protocol import (
    GUIDANCE_MODES,
    PLAYBACK_PACED,
    STAGE_FINAL,
    STAGE_PROVISIONAL,
    TYPE_ERROR,
    TYPE_GUIDANCE_LIVE,
    TYPE_GUIDANCE_PRESENTED,
    TYPE_GUIDANCE_RECEIVED,
    TYPE_LATENCY_PRESENTED,
    TYPE_LATENCY_PROBE,
    TYPE_LATENCY_RECEIVED,
    TYPE_PERFORMANCE_RESPONSE,
    TYPE_RECORDING_PAUSE,
    TYPE_RECORDING_READY,
    TYPE_RECORDING_START,
    TYPE_RECORDING_STOP,
    TYPE_SESSION_FINISHED,
    TYPE_SESSION_PAUSE,
    TYPE_SESSION_RESUME,
    TYPE_SESSION_START,
    TYPE_SESSION_STOP,
    GuidanceAction,
)
from ..qt_bridge import RemoteClientBridge
from ..timing import mono_ns, wall_ns
from ..vision_worker import LatestVisionWorker
from .session import LocalRecordingScheduler, RemoteEvent, StudentSession

log = logging.getLogger("remote_guidance.student.window")

TICK_MS = 33
LED_FLASH_DELAY_MS = 300
LED_FLASH_DURATION_MS = 800
# Same convention as student_quiz.py: flash the first five white keys so
# the mark survives a hand covering the leftmost one, switching key 0
# first because that is the region app.sync_led looks at.
SYNC_LED_PIXELS = [key[0] for key in WHITE_LEDS[:5]]

COL_SEQ, COL_TARGET, COL_ACTUAL, COL_RT, COL_NET, COL_QUEUE, COL_RESULT = range(7)

# One line per session, outside every session folder, written before the
# first frame and again once the folder is closed.
#
# A session folder is otherwise the only record that a session happened:
# if it never appears, or stops being there, nothing on this machine says
# what was opened or where. That is not a hypothetical - it is why this
# file exists. The log is append-only, is never read by the application,
# and no failure to write it can stop a lesson.
SESSION_LOG_FILENAME = "_sessions.log"


def append_session_log(line: str) -> None:
    try:
        path = QUIZ_DATA_DIR / SESSION_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        log.exception("could not append to %s", SESSION_LOG_FILENAME)


class StudentRemoteWindow(QMainWindow, StageWindow):
    def __init__(self, cfg: Config, remote: Optional[RemoteGuidanceConfig] = None):
        super().__init__()
        self.setWindowTitle("Remote Guidance - Student Client")
        self.resize(1180, 760)

        self.remote = remote or RemoteGuidanceConfig.load()
        # The platform-wide config, kept so the Settings dialog can offer
        # "use this machine's setup" and so apply_settings() can rebuild
        # the role view below.
        self.base_cfg = cfg
        # A Config view carrying the *student's* camera, MIDI port and
        # keyboard profile. The caller's cfg - shared with every other
        # tool in this process - is not touched.
        self.cfg = student_config(cfg, self.remote)

        self.camera: Optional[Camera] = None
        self.tracker: Optional[HandTracker] = None
        self._vision_worker: Optional[LatestVisionWorker] = None
        self._vision_sequence = 0
        self.midi_recorder: Optional[RawMidiRecorder] = None
        self.audio: Optional[NoteAudioPlayer] = None
        self.led = LEDArrayController(port=self.remote.student.led.port)
        self.led_connected = False
        self.led_mapper = None
        self.template: Optional[KeyboardTemplate] = None
        self.mapping: Optional[MidiMapping] = None

        self.bridge: Optional[RemoteClientBridge] = None
        self.session: Optional[StudentSession] = None
        self.scheduler: Optional[LocalRecordingScheduler] = None
        self._pending_recording_start: Optional[Dict[str, Any]] = None
        self.session_id: Optional[str] = None
        self.session_name = ""
        # Counting the lesson in. The teacher names the instant both
        # ends open their writers; this is what is left to wait.
        self._countdown_timer: Optional[QTimer] = None
        self._countdown_left = 0
        self.cue = None
        self.last_hands: Dict[str, Any] = {}

        self.video_writer: Optional[cv2.VideoWriter] = None
        self.video_path: Optional[Path] = None
        self.video_start_time: Optional[float] = None
        self._video_fps = 0.0
        self._frames_written = 0
        self.raw_events: List[Any] = []
        self.led_on_time: Optional[float] = None
        self.led_off_time: Optional[float] = None
        self._analysis_window: Optional[QuizAnalysisWindow] = None
        self._analyze_worker: Optional[AnalyzeWorker] = None

        self.room: Optional[dict] = None

        self._build_ui()
        self._load_profile()
        self._check_serial_ports()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.link_label = header_label("")
        self.status_label = header_label("Sign in to the relay to begin.")

        stages = self.build_stages(self.remote, "student", self._build_session_page())
        self.sign_in_panel.statusChanged.connect(self._set_status)
        self.room_panel.statusChanged.connect(self._set_status)
        for panel in (self.sign_in_panel, self.room_panel):
            panel.statusChanged.connect(
                lambda _message, level, current=panel: current.status_label.setStyleSheet(
                    STUDENT_STATUS_STYLES.get(level, "")
                )
            )
        for label in stages.findChildren(QLabel):
            if label.styleSheet() == STATUS_STYLES["idle"]:
                label.setStyleSheet(STUDENT_STATUS_STYLES["idle"])

        self.role_label = QLabel("STUDENT  /  PRACTICE STUDIO")
        self.role_label.setObjectName("roleTitle")
        self.stage_label.setObjectName("stageChip")
        self.stage_label.setStyleSheet("")
        self.link_label.setObjectName("linkStatus")
        self.link_label.setWordWrap(False)
        self.link_label.setStyleSheet("")
        self.link_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_label.setObjectName("globalStatus")
        self.status_label.setWordWrap(False)
        self.status_label.setMaximumHeight(30)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.status_label.setStyleSheet(STUDENT_STATUS_STYLES["idle"])

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        header = QHBoxLayout(top_bar)
        header.setContentsMargins(12, 7, 8, 7)
        header.setSpacing(9)
        header.addWidget(self.role_label)
        header.addWidget(self.stage_label)
        header.addWidget(self.link_label, 1)
        header.addWidget(self.build_settings_button())

        central = QWidget()
        central.setObjectName("studentCentral")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(7)
        layout.addWidget(top_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(stages, 1)
        self.setCentralWidget(central)
        self.setStyleSheet(STUDENT_STYLE_SHEET)

    def _build_session_page(self) -> QWidget:
        self.room_label = header_label("")
        self.room_label.setObjectName("roomChip")
        self.room_label.setStyleSheet("")
        self.change_room_btn = QPushButton("Change room")
        self.change_room_btn.clicked.connect(self.back_to_rooms)

        top = QHBoxLayout()
        top.addWidget(self.room_label, 1)
        top.addWidget(self.change_room_btn)

        self.guidance_combo = QComboBox()
        for mode in GUIDANCE_MODES:
            self.guidance_combo.addItem(
                {
                    "visual": "Visual (screen finger cue + key LED)",
                    "haptic": "Haptic (nail actuator + key LED)",
                    "both": "Both (screen + actuator + key LED)",
                }[mode],
                mode,
            )
        index = self.guidance_combo.findData(self.remote.student.default_guidance_mode)
        self.guidance_combo.setCurrentIndex(max(index, 0))

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(1.0, 30.0)
        self.timeout_spin.setSingleStep(0.5)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(self.remote.student.default_timeout_s)

        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0))
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)

        self.record_check = QCheckBox("Record video + MIDI (needed for the final finger pass)")
        self.record_check.setChecked(self.remote.student.record_video)

        self.led_status = QLabel("LED: connects with Ready for guidance")

        self.start_btn = QPushButton("Ready for guidance")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_session)
        self.stop_btn = QPushButton("End session")
        self.stop_btn.setProperty("role", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self._finish_session(reason="stopped by the student"))

        # The controls form a compact side card rather than a pair of
        # very long rows.  That keeps labels close to their fields and
        # gives the camera a complete, moderate-size preview beside it.
        setup_box = QGroupBox("Practice setup")
        setup_box.setMinimumWidth(360)
        setup = QVBoxLayout(setup_box)
        guidance_row = QHBoxLayout()
        guidance_row.addWidget(QLabel("Guidance:"))
        guidance_row.addWidget(self.guidance_combo, 1)
        setup.addLayout(guidance_row)
        response_row = QHBoxLayout()
        response_row.addWidget(QLabel("Timeout:"))
        response_row.addWidget(self.timeout_spin)
        response_row.addWidget(QLabel("Instrument:"))
        response_row.addWidget(self.timbre_combo, 1)
        setup.addLayout(response_row)
        setup.addWidget(self.record_check)
        setup.addWidget(self.led_status)
        setup.addStretch(1)
        self.lesson_label = QLabel("Recording starts when the teacher begins the lesson.")
        self.lesson_label.setWordWrap(True)
        setup.addWidget(self.lesson_label)
        # Whether this machine is writing a video is otherwise only in the
        # status line, which nobody reads while playing. Hidden until the
        # writer is actually open, so it never claims a recording that is
        # not running.
        self.recording_chip = QLabel("")
        self.recording_chip.setObjectName("recordingChip")
        self.recording_chip.setWordWrap(True)
        self.recording_chip.setVisible(False)
        setup.addWidget(self.recording_chip)
        session_actions = QHBoxLayout()
        session_actions.addWidget(self.start_btn, 1)
        session_actions.addWidget(self.stop_btn)
        setup.addLayout(session_actions)

        self.view = ImageView(max_size=(560, 360))
        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["#", "Target", "Played", "RT (ms)", "Net (ms)", "Queue (ms)", "Result"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)

        self.camera_panel = QGroupBox("Camera preview")
        self.camera_panel.setObjectName("cameraCard")
        camera_layout = QVBoxLayout(self.camera_panel)
        camera_layout.addWidget(self.view, 1, Qt.AlignmentFlag.AlignCenter)
        camera_layout.addWidget(self.progress)

        self.practice_page = QWidget()
        practice_layout = QHBoxLayout(self.practice_page)
        practice_layout.setContentsMargins(9, 9, 9, 9)
        practice_splitter = QSplitter(Qt.Orientation.Horizontal)
        practice_splitter.addWidget(setup_box)
        practice_splitter.addWidget(self.camera_panel)
        practice_splitter.setStretchFactor(0, 1)
        practice_splitter.setStretchFactor(1, 1)
        practice_splitter.setSizes([470, 560])
        practice_splitter.setChildrenCollapsible(False)
        practice_layout.addWidget(practice_splitter)

        self.results_page = QWidget()
        results_layout = QVBoxLayout(self.results_page)
        results_layout.setContentsMargins(9, 9, 9, 9)
        results_intro = QLabel(
            "Responses appear here during the lesson. The complete session remains readable without "
            "reducing the camera preview."
        )
        results_intro.setObjectName("mutedText")
        results_intro.setWordWrap(True)
        results_layout.addWidget(results_intro)
        results_layout.addWidget(self.table, 1)

        self.workspace_tabs = QTabWidget()
        self.practice_tab_index = self.workspace_tabs.addTab(self.practice_page, "Practice studio")
        self.results_tab_index = self.workspace_tabs.addTab(self.results_page, "Session results")
        self.workspace_tabs.setCurrentIndex(self.practice_tab_index)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        layout.addLayout(top)
        layout.addWidget(self.workspace_tabs, 1)
        return page

    def _set_status(self, message: str, level: str = "idle") -> None:
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
        self.status_label.setStyleSheet(STUDENT_STATUS_STYLES.get(level, ""))

    def apply_settings(self) -> None:
        """Rebuild the student's Config view from the saved settings.

        No device is open when this runs - the Settings button is
        disabled for the length of a session - so reloading the profile
        is all that is needed; the configured MIDI port, camera and LED
        mapper are used fresh at the next start."""
        self.cfg = student_config(self.base_cfg, self.remote)
        self._load_profile()
        if self.template is None:
            return  # _load_profile already said what is wrong with it
        self._set_status(
            f"Settings saved. Camera {self.cfg.camera.index}, MIDI {self.cfg.midi.port_name!r}, "
            f"profile {self.cfg.active_keyboard_profile!r} - used from the next session.",
            "ok",
        )

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def _check_serial_ports(self) -> None:
        """The LED strip and the vibration rig are two separate boards. A
        shared port would send motor commands to the LED controller (or
        the reverse), so it is refused up front rather than discovered
        mid-session."""
        problems = self.remote.serial_port_problems()
        if problems:
            self._set_status(problems[0], "error")
            QMessageBox.warning(self, "Serial port conflict", problems[0])

    def _ensure_camera(self) -> None:
        """Claim the camera and MediaPipe. Called when a session starts,
        never on the way in - see enter_session."""
        if self.camera is None:
            self.camera = Camera(self.cfg.camera)
        if self.tracker is None:
            self.tracker = HandTracker()
        if self._vision_worker is None:
            self._vision_sequence = 0
            self._vision_worker = LatestVisionWorker(
                self.camera,
                self.tracker,
                annotate=lambda frame, hands: draw_hands(frame, hands),
                name="remote-student-vision",
                max_fps=self.cfg.camera.fps or 30,
                # What gets recorded is the camera's own frame, exactly as
                # the local quiz records it - draw_hands() paints the
                # skeleton, the joints and the finger labels over the
                # hands, and the offline pass has to find those same hands
                # again in the recording.
                keep_raw=True,
            )
            self._vision_worker.start()

    def _load_profile(self) -> None:
        name = self.cfg.active_keyboard_profile
        try:
            self.template = KeyboardTemplate.load(PROFILE_DATA_DIR / name / "keyboard_template.json")
            self.mapping = MidiMapping.load(PROFILE_DATA_DIR / name / "midi_mapping.json")
        except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
            self.template = self.mapping = None
            self._set_status(
                f"Student keyboard profile {name!r} could not be loaded ({exc}). Cues will still show the "
                "note, but no finger can be scored locally.",
                "warn",
            )

    def _connect_led(self) -> Optional[str]:
        """Open the LED strip, or return why it could not be opened.

        **There is no port to choose.** The strip is found by
        `common.serial_utils.auto_detect_port` (via LEDArrayController)
        unless `student.led.port` has been pinned by hand in config.json,
        so neither the settings dialog nor this page asks for one."""
        if self.led_connected:
            return None
        try:
            self.led.connect()
            self.led.off()
            self.led_mapper = profile_led_mapper.build_mapper(self.led, self.cfg.active_keyboard_profile)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the client
            self.led_mapper = None
            return str(exc)
        self.led_connected = True
        self.led_status.setText(f"LED: connected on {self.led.port}")
        return None

    def _ensure_led(self) -> None:
        """Connect the strip as a session starts. A failure is a warning,
        not a refusal: without it the key backlight is missing but the
        finger cue and the scoring still work, and saying so beats
        refusing to run."""
        problem = self._connect_led()
        if problem is not None:
            self._set_status(
                f"LED strip not found ({problem}) - running without the key backlight cue.", "warn"
            )

    def enter_session(self, room: dict) -> None:
        """Reaching the session page opens the WebSocket and nothing
        else. The camera, MediaPipe and the MIDI port are claimed by
        "Ready for guidance" (see _start_session), using the MIDI port
        saved in this client's Settings - the LED strip included."""
        self.room = room
        self.room_label.setText(room_summary(room))
        self.persist_connection(self.remote, room)

        self.bridge = RemoteClientBridge(
            ws_url=self.ws_url(room["room_id"]),
            access_token=self.api.session.access_token,
            room_id=room["room_id"],
            verify_tls=self.remote.network.verify_tls,
            heartbeat_interval_s=self.remote.network.heartbeat_interval_s,
            reconnect_initial_delay_s=self.remote.network.reconnect_initial_delay_s,
            reconnect_max_delay_s=self.remote.network.reconnect_max_delay_s,
            parent=self,
        )
        self.bridge.messageReceived.connect(self._on_message)
        self.bridge.stateChanged.connect(self._on_link_state)
        self.bridge.errorOccurred.connect(lambda detail: self._set_status(detail, "error"))
        self.bridge.start()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_status(
            "In the room. Camera and MIDI open when you press \"Ready for guidance\".", "idle"
        )

    def leave_session(self) -> None:
        """Hand the camera, the tracker and the socket back. Going back
        to the room list should not keep a device busy."""
        if self.session is not None:
            self._finish_session(reason="left the session")
        self._close_audio()
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        self._release_camera()
        self.room = None
        self.session_id = None
        self._pending_recording_start = None
        self.table.setRowCount(0)
        self.link_label.setText("")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.set_settings_enabled(True)

    def _release_camera(self) -> None:
        if self._vision_worker is not None:
            self._vision_worker.close()
            self._vision_worker = None
            self.tracker = None
            self.camera = None
            self._vision_sequence = 0
            self.last_hands = {}
            self.view.clear()
            return
        if self.tracker is not None:
            try:
                self.tracker.close()
            except Exception:  # noqa: BLE001
                log.exception("closing the hand tracker failed")
            self.tracker = None
        if self.camera is not None:
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing the camera failed")
            self.camera = None
        self.last_hands = {}
        # Otherwise the last captured frame sits there looking live.
        self.view.clear()

    def _open_audio(self) -> None:
        """Open one non-exclusive output stream for this active session.

        MIDI is not reopened: _tick() feeds the same RawMidiRecorder
        events to both StudentSession and this player. Selecting Mute
        keeps the output device entirely unclaimed.
        """
        timbre = self.timbre_combo.currentData() or DEFAULT_TIMBRE
        if timbre == "mute":
            self._close_audio()
            return
        if self.audio is not None:
            self.audio.set_timbre(timbre)
            return
        try:
            self.audio = NoteAudioPlayer(timbre=timbre)
        except Exception as exc:  # noqa: BLE001 - the lesson can continue without local sound
            self.audio = None
            QMessageBox.warning(
                self,
                "Audio output not available",
                f"Could not start audio playback ({exc}). Continuing without sound feedback.",
            )

    def _close_audio(self) -> None:
        """Stop every voice and release this client's output stream."""
        if self.audio is None:
            return
        audio, self.audio = self.audio, None
        try:
            audio.stop_all()
        except Exception:  # noqa: BLE001 - close still has to run
            log.exception("stopping the student audio voices failed")
        try:
            audio.close()
        except Exception:  # noqa: BLE001 - never prevent the other devices being released
            log.exception("closing the student audio stream failed")

    def _on_timbre_changed(self, index: int) -> None:
        timbre = self.timbre_combo.itemData(index) or DEFAULT_TIMBRE
        if timbre == "mute":
            self._close_audio()
        elif self.audio is not None:
            self.audio.set_timbre(timbre)
        elif self.midi_recorder is not None:
            self._open_audio()

    def _on_link_state(self, state: str, detail: str) -> None:
        self.link_label.setText(f"Link: {state} {('- ' + detail) if detail else ''}")
        self.link_label.setStyleSheet(
            STUDENT_STATUS_STYLES["ok" if state == "connected" else "warn"]
        )

    def _send(self, type_: str, payload: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None) -> None:
        """session_id names an older session explicitly - the offline
        finger pass reports against the lesson it analysed, which may no
        longer be the current one by the time it finishes."""
        if self.bridge is None:
            return
        self.bridge.send(type_, payload, session_id=session_id or self.session_id)

    def _on_message(self, envelope: Dict[str, Any]) -> None:
        """Runs on the GUI thread (queued signal from the network
        thread), so it is safe to touch widgets and hardware here."""
        msg_type = envelope.get("type")

        if msg_type == TYPE_GUIDANCE_LIVE:
            self._on_guidance(envelope)
        elif msg_type == TYPE_LATENCY_PROBE:
            self._on_latency_probe(envelope)
        elif msg_type == TYPE_SESSION_START:
            self._on_remote_session_start(envelope)
        elif msg_type in (TYPE_SESSION_PAUSE, TYPE_RECORDING_PAUSE):
            self._on_pause()
        elif msg_type == TYPE_SESSION_RESUME:
            self._on_resume()
        elif msg_type in (TYPE_SESSION_STOP, TYPE_RECORDING_STOP):
            self._finish_session(reason="stopped by the teacher")
        elif msg_type == TYPE_RECORDING_START:
            self._on_recording_start(envelope)
        elif msg_type == TYPE_ERROR:
            payload = envelope.get("payload") or {}
            self._set_status(f"Server: {payload.get('detail')}", "error")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _start_session(self) -> None:
        if self.session is not None:
            return
        self.workspace_tabs.setCurrentIndex(self.practice_tab_index)
        # The last lesson's id and name must not survive into this one.
        # Announcing readiness before the teacher has opened the session
        # is the normal order, and carrying the previous lesson's name
        # into that moment is exactly how a second lesson used to be
        # recorded on top of the first. Cleared on the bridge too:
        # send() falls back to the client's own copy when handed None.
        self.session_id = None
        self.session_name = ""
        if self.bridge is not None:
            self.bridge.session_id = None
        guidance_mode = self.guidance_combo.currentData()
        # Off from the press itself, not from the far end of the device
        # setup below: opening the camera, MediaPipe and the MIDI port
        # takes long enough that a still-live button reads as "nothing
        # happened" and gets pressed again. Every path that gives up
        # before the session exists puts it back.
        self.start_btn.setEnabled(False)

        problems = self.remote.serial_port_problems()
        if problems and wants_haptic(guidance_mode):
            self.start_btn.setEnabled(True)
            QMessageBox.warning(self, "Serial port conflict", problems[0])
            return

        # The camera, MediaPipe, the LED strip and the MIDI port are all
        # claimed here rather than on arriving at this page: nothing
        # should hold hardware while the student is still choosing a mode
        # or a name. The LED comes first because _build_cue() below reads
        # led_mapper to decide whether the key cue has a channel.
        self._ensure_led()
        try:
            self._ensure_camera()
            self.cue = self._build_cue(guidance_mode)
        except Exception as exc:  # noqa: BLE001
            self._release_camera()
            self.start_btn.setEnabled(True)
            QMessageBox.warning(self, "Could not start the cue", str(exc))
            return

        port_name = self.cfg.midi.port_name or None
        try:
            self.midi_recorder = RawMidiRecorder(port_name)
        except RuntimeError as exc:
            self.cue.close()
            self.cue = None
            # Nothing started, so give the camera back too.
            self._release_camera()
            self.start_btn.setEnabled(True)
            QMessageBox.warning(self, "MIDI connection failed", str(exc))
            return

        self._open_audio()

        self.raw_events = []
        self.table.setRowCount(0)
        self.led_on_time = self.led_off_time = None
        # Not recorded yet, and deliberately: the camera, MediaPipe, the
        # keyboard, the LED strip and the cue outputs are all up, but the
        # video writer waits for the teacher to begin the lesson. Only
        # then does this session have a name to be written under.

        self.session = StudentSession(
            timeout_s=self.timeout_spin.value(),
            queue_size=self.remote.network.guidance_queue_size,
            mapping=self.mapping,
            present=self._present_event,
            clear_cue=self._clear_cue,
            send_received=self._send_received,
            send_presented=self._send_presented,
            send_response=self._send_response,
            resolve_finger=self._resolve_finger,
        )
        self.session.start()

        self.stop_btn.setEnabled(True)
        for widget in (self.guidance_combo, self.timeout_spin, self.record_check):
            widget.setEnabled(False)
        # Devices are open now; changing which ones to open is meaningless
        # until this session ends.
        self.set_settings_enabled(False)

        self.cue.show_message("Waiting for the teacher...")
        self.lesson_label.setText("Devices ready. Recording starts when the teacher begins the lesson.")
        self._set_status(
            f"Ready - guidance mode {guidance_mode}, channels: {', '.join(self.cue.enabled_channels) or 'none'}."
            " Not recording yet.",
            "ok",
        )
        # Tell the teacher the student is standing by, with what. If the
        # teacher has not opened the relay session yet this first message
        # has no session id; _on_remote_session_start announces it again
        # once the id is known so the relay can attach the student's own
        # guidance choice to the durable session record.
        self._announce_ready()
        self._play_pending_recording_if_ready()

    def _announce_ready(self) -> None:
        if self.session is None or self.cue is None:
            return
        self._send(
            TYPE_RECORDING_READY,
            {
                "guidance_mode": self.guidance_combo.currentData(),
                "channels": self.cue.enabled_channels,
                "timeout_s": self.timeout_spin.value(),
                "recording_video": self.video_writer is not None,
                # The name this session will be saved under, which is not
                # necessarily the one it started under.
                "session_name": self.session_name or None,
            },
        )

    def _play_pending_recording_if_ready(self) -> None:
        if self.session is None or self._pending_recording_start is None:
            return
        envelope, self._pending_recording_start = self._pending_recording_start, None
        self._on_recording_start(envelope)

    def _build_cue(self, guidance_mode: str):
        cue_style = self.cfg.visual_cue_style if self.cfg.visual_cue_style in CUE_STYLES else DEFAULT_CUE_STYLE
        return build_student_cue(
            guidance_mode,
            led_mapper=self.led_mapper if self.led_connected else None,
            visual_factory=(lambda: ScreenCueOutput(cue_style)) if wants_visual(guidance_mode) else None,
            haptic_factory=(
                (lambda: HapticCueOutput(port=self.remote.student.haptic.port))
                if wants_haptic(guidance_mode)
                else None
            ),
        )

    def _set_recording_indicator(self, name: str = "") -> None:
        """Say, in one unmissable place, whether this machine is writing a
        recording right now. An empty name means it is not."""
        self.recording_chip.setVisible(bool(name))
        self.recording_chip.setText(f"\u25cf  RECORDING  \u2014  data/quiz/{name}/" if name else "")

    def _start_recording(self, name: str) -> bool:
        """Open the video writer for this session. False means the student
        chose not to run without one.

        A camera that produces nothing used to leave a line on the status
        bar and let the lesson start anyway. Nobody reads the status bar
        while playing, and by the time the session is over the recording
        it needed is unrecoverable - so a session that cannot record now
        asks, and defaults to not starting."""
        raw = quiz_raw_dir(name)
        raw.mkdir(parents=True, exist_ok=True)
        self.video_path = raw / RAW_VIDEO_FILENAME
        if self.video_path.is_file() and self.video_path.stat().st_size > 0:
            # Belt and braces. Names now come from the teacher and are
            # made fresh per lesson, so this should be unreachable - but
            # opening a VideoWriter truncates whatever is at the path, and
            # a lesson recorded over another lesson is unrecoverable. It
            # happened: the previous session's name outlived it, and each
            # lesson was written over the one before.
            append_session_log(f"{name}: REFUSED to reopen an existing {self.video_path}")
            QMessageBox.critical(
                self,
                "That session already has a recording",
                f"{self.video_path} already exists and is not empty.\n\n"
                "Opening it again would destroy it, so this session will not be recorded. Nothing has "
                "been changed on disk.",
            )
            return False
        if self._vision_worker is not None:
            snapshot = self._vision_worker.wait_for_frame(timeout=3.0)
            # The recording's own frame decides the writer's size.
            frame = snapshot.raw_frame if snapshot.raw_frame is not None else snapshot.frame
        else:
            frame = self.camera.read() if self.camera else None
        if frame is None:
            append_session_log(f"{name}: camera produced no frame, no video writer opened")
            return self._confirm_without_video(
                "The camera produced no frame, so this session cannot be recorded."
            )
        h, w = frame.shape[:2]
        fps = self.cfg.camera.fps or 30
        writer = cv2.VideoWriter(str(self.video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not writer.isOpened():
            # An unopened writer swallows every write() silently and
            # leaves a zero-byte file behind, which is exactly the
            # failure this whole path exists to stop being silent about.
            writer.release()
            append_session_log(f"{name}: VideoWriter refused to open {self.video_path}")
            return self._confirm_without_video(
                f"The video file could not be opened for writing:\n\n{self.video_path}"
            )
        self.video_writer = writer
        self._video_fps = fps
        self._frames_written = 0
        self.video_start_time = time.time()
        append_session_log(f"{name}: recording to {self.video_path} ({w}x{h} @ {fps}fps)")
        self._set_status(f"Recording to {self.video_path}", "ok")
        self._set_recording_indicator(name)
        if self.led_connected:
            QTimer.singleShot(LED_FLASH_DELAY_MS, self._flash_leds_on)
        return True

    def _confirm_without_video(self, problem: str) -> bool:
        answer = QMessageBox.critical(
            self,
            "This session cannot be recorded",
            f"{problem}\n\n"
            "Without a video there is no recorded finger pass and no footage to review by hand: "
            "the live finger verdicts would be all this session ever has, and nothing can "
            "re-create them afterwards.\n\n"
            "Start the session anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _flash_leds_on(self) -> None:
        """The clapperboard mark app.sync_led looks for. Same procedure as
        the local quiz, so the same offline alignment works here."""
        if self.video_writer is None or not self.led_connected:
            return
        self.led_on_time = time.time()
        for strip, idx in SYNC_LED_PIXELS:
            self.led.set_pixel(strip, idx, 255, 255, 255, 255)
        QTimer.singleShot(LED_FLASH_DURATION_MS, self._flash_leds_off)

    def _flash_leds_off(self) -> None:
        if self.video_writer is None or not self.led_connected:
            return
        strip0, idx0 = SYNC_LED_PIXELS[0]
        self.led.set_pixel(strip0, idx0, 0, 0, 0, 0)
        self.led_off_time = time.time()
        for strip, idx in SYNC_LED_PIXELS[1:]:
            self.led.set_pixel(strip, idx, 0, 0, 0, 0)

    def _on_remote_session_start(self, envelope: Dict[str, Any]) -> None:
        """The teacher has begun the lesson. This is where a session gets
        its name, and - after the count-in - its recording.

        The name arrives; it is never invented here and never carried
        over from the last lesson. data/quiz/<name>/ and the teacher's
        data/music/<name>/ are the two halves of one lesson and are
        written under one name from the first frame, so nothing has to be
        renamed afterwards and no lesson can be opened on top of another."""
        payload = envelope.get("payload") or {}
        self.session_id = envelope.get("session_id")
        if self.bridge is not None:
            self.bridge.session_id = self.session_id
        name = sanitize_quiz_name(payload.get("session_name") or "")
        if self.session is None:
            self._set_status(
                "The teacher started a session, but this client is not ready yet - press "
                "\"Ready for guidance\".",
                "warn",
            )
            return
        self.session.resume()
        # The student's selection owns the session's guidance modality.
        # Repeat the ready message now that session_id is available; the
        # teacher's session.start deliberately contains no modality.
        self._announce_ready()
        self._set_status(f"Lesson started by the teacher ({payload.get('mode', 'live')}).", "ok")
        if name:
            self._schedule_recording(name, payload.get("start_at_unix_ns"))

    def _schedule_recording(self, name: str, start_at_wall_ns: Any) -> None:
        """Open the writer at the instant the teacher named, not on
        arrival.

        The teacher sends a wall-clock moment a few seconds out and
        counts the same seconds off itself. Mapping it through this
        machine's own monotonic clock - the same arithmetic
        _on_recording_start() already uses for pre-recorded playback -
        means the two writers open together even though only the delay,
        never the absolute time, is comparable across machines."""
        if self.video_writer is not None:
            return
        self.session_name = name
        if not self.record_check.isChecked():
            self.lesson_label.setText(f"Lesson {name} - recording is switched off for this session.")
            self._set_recording_indicator()
            return
        delay_ns = 0
        if start_at_wall_ns:
            delay_ns = max(0, int(start_at_wall_ns) - wall_ns())
        self._countdown_left = int(round(delay_ns / 1e9))
        self._tick_countdown()
        if delay_ns <= 0:
            self._begin_recording(name)
            return
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._countdown_timer.start()
        QTimer.singleShot(int(delay_ns / 1e6), lambda: self._begin_recording(name))

    def _tick_countdown(self) -> None:
        if self._countdown_left > 0:
            self.lesson_label.setText(f"Recording starts in {self._countdown_left}...")
            self.cue.show_message(f"Starting in {self._countdown_left}") if self.cue else None
            self._countdown_left -= 1

    def _begin_recording(self, name: str) -> None:
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        if self.session is None or self.video_writer is not None:
            return
        if not self._start_recording(name):
            self._set_status("Continuing without a recording, as chosen.", "warn")
            return
        # The folder is named by the chip beside it; this line stays the
        # lesson's own state.
        self.lesson_label.setText(f"Lesson {name} is running.")
        if self.cue is not None:
            self.cue.show_message("Waiting for the teacher...")

    def _on_pause(self) -> None:
        if self.session is not None:
            self.session.pause()
        if self.scheduler is not None:
            self.scheduler.pause()
        self._set_status("Paused by the teacher.", "warn")

    def _on_resume(self) -> None:
        if self.session is not None:
            self.session.resume()
        if self.scheduler is not None:
            self.scheduler.resume()
        self._set_status("Resumed.", "ok")

    # ------------------------------------------------------------------
    # Pre-recorded playback
    # ------------------------------------------------------------------

    def _on_recording_start(self, envelope: Dict[str, Any]) -> None:
        """Teacher-triggered playback of a stored sequence.

        The event list is downloaded in full first, so every cue after
        this point is scheduled from local storage and no note waits on
        the network. The server names a short-future start moment in
        *its* wall clock; it is mapped onto this machine's monotonic
        clock through the wall/monotonic pair read here. Any error in
        that mapping shifts only when playback begins - each event's own
        cue timings are still measured locally."""
        payload = envelope.get("payload") or {}
        recording_id = payload.get("recording_id")
        mode = payload.get("playback_mode") or PLAYBACK_PACED
        if not recording_id:
            self._set_status("Recording start ignored - no recording id.", "warn")
            return
        if self.session is None:
            self._pending_recording_start = envelope
            self._set_status(
                "Recorded guidance is waiting - press \"Ready for guidance\" to download and play it.",
                "warn",
            )
            return

        try:
            detail = self.api.get_recording(recording_id)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not download the recording: {exc}", "error")
            return

        start_at_wall_ns = int(payload.get("start_at_unix_ns") or 0)
        now_wall, now_mono = wall_ns(), mono_ns()
        start_mono = now_mono + max(0, start_at_wall_ns - now_wall) if start_at_wall_ns else now_mono

        self.scheduler = LocalRecordingScheduler(
            events=list(detail.get("events") or []),
            mode=mode,
            start_monotonic_ns=start_mono,
        )
        self.session_id = envelope.get("session_id") or self.session_id
        if self.bridge is not None:
            self.bridge.session_id = self.session_id
        self._set_status(
            f"Playing {detail.get('name')!r} locally: {len(self.scheduler.events)} events, {mode} mode.", "ok"
        )

    def _on_guidance(self, envelope: Dict[str, Any]) -> None:
        """One live cue from the teacher.

        Handing the envelope straight to StudentSession is the whole job.
        accept() stamps arrival on this machine's clock, acknowledges with
        guidance.received *before* any cue work, and queues; the timer's
        session.tick() presents it. Nothing here may show the cue itself -
        both the live and the recorded path go through that one path so
        every event's timings are produced identically (§4.1, §4.9)."""
        if self.session is None:
            # Not ready yet, so there is nothing to present it with and no
            # event to refuse with. Say so rather than dropping it in
            # silence - the teacher is watching a cue that went nowhere.
            self._set_status(
                "The teacher sent guidance before this client was ready - press Ready for guidance.", "warn"
            )
            return
        self.session.accept(envelope)

    def _release_scheduled_event(self, event: Dict[str, Any]) -> None:
        """Feed a locally scheduled event through the same path a live
        one takes, so its timings are recorded identically. It carries no
        teacher/server wall stamps because it never crossed the network -
        which is exactly what makes those fields None rather than zero."""
        if self.session is None:
            return
        action = GuidanceAction(
            note=int(event["note"]),
            finger=event.get("finger"),
            key_id=event.get("key_id"),
            note_name=event.get("note_name"),
        )
        self.session.accept(
            {
                "message_id": f"local-{event.get('event_order', 0)}",
                "seq": int(event.get("event_order", 0)),
                "sent_at_unix_ns": None,
                "payload": {"actions": [action.to_dict()], "timeout_s": self.timeout_spin.value()},
                "server": {},
            }
        )

    # ------------------------------------------------------------------
    # Session callbacks (all on the GUI thread)
    # ------------------------------------------------------------------

    def _present_event(self, event: RemoteEvent):
        """Cue everything the teacher played at once.

        A chord arrives as several actions in one envelope and is cued as
        a set - all its keys lit, all its motors buzzing, every dot on.
        Scoring still judges the primary note only (§4.11): this is the
        cue widening, not a second definition of correct."""
        if not event.actions or self.cue is None:
            return None
        return self.cue.show_targets([(a.note, a.finger) for a in event.actions])

    def _clear_cue(self) -> None:
        if self.cue is not None:
            self.cue.clear()

    def _resolve_finger(self, note: int):
        """Provisional finger verdict from the live hand landmarks,
        through the shared matcher - never a second implementation."""
        if self.template is None or self.mapping is None:
            return None
        return match_note_to_finger(note, self.template, self.mapping, self.last_hands)

    def _send_received(self, event: RemoteEvent, accepted: bool) -> None:
        self._send(TYPE_GUIDANCE_RECEIVED, event.received_payload(accepted))
        if not accepted:
            self._set_status(event.refused_reason or "guidance refused", "warn")

    def _send_presented(self, event: RemoteEvent) -> None:
        self._send(TYPE_GUIDANCE_PRESENTED, event.presented_payload())
        target = event.target
        if target is not None:
            self._set_status(
                f"Press {target.note_name or note_name(target.note)} with finger {target.finger or '?'}", "ok"
            )

    def _send_response(self, event: RemoteEvent) -> None:
        self._send(TYPE_PERFORMANCE_RESPONSE, event.response_payload(STAGE_PROVISIONAL))
        self._add_row(event)

    def _add_row(self, event: RemoteEvent) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        target = event.target
        transport = event.transport_ns()
        queue_wait = event.queue_wait_ns
        rt = event.reaction_time_ns

        cells = {
            COL_SEQ: str(event.index + 1),
            COL_TARGET: f"{target.note_name or note_name(target.note)} / {target.finger or '?'}" if target else "-",
            COL_ACTUAL: (
                f"{note_name(event.actual_note)} / {event.actual_finger or '?'}"
                if event.actual_note is not None
                else "(no press)"
            ),
            COL_RT: f"{rt / 1e6:.1f}" if rt is not None else "-",
            COL_NET: f"{transport / 1e6:.1f}" if transport is not None else "-",
            COL_QUEUE: f"{queue_wait / 1e6:.1f}" if queue_wait is not None else "-",
            COL_RESULT: "timeout" if event.timed_out else ("correct" if event.note_correct else "wrong key"),
        }
        for column, text in cells.items():
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()

    # ------------------------------------------------------------------
    # Latency probes
    # ------------------------------------------------------------------

    def _on_latency_probe(self, envelope: Dict[str, Any]) -> None:
        """Ack immediately, then (optionally) drive the real cue and ack
        again once it is ready.

        The first reply is what the teacher's transport RTT ends on, so
        it must not wait for any hardware. The second carries the local
        dispatch duration measured on this machine's own monotonic clock,
        which the teacher relays back as an opaque value."""
        payload = envelope.get("payload") or {}
        probe_id = payload.get("probe_id")
        receive_mono, receive_wall = mono_ns(), wall_ns()

        self._send(
            TYPE_LATENCY_RECEIVED,
            {
                "probe_id": probe_id,
                "probe_message_id": envelope.get("message_id"),
                "student_receive_wall_ns": receive_wall,
                # Opaque to the teacher: only this machine may subtract it.
                "student_receive_monotonic_ns": receive_mono,
            },
        )

        if not payload.get("trigger_cue"):
            return

        dispatch = None
        if self.cue is not None:
            try:
                dispatch = self.cue.show_target(int(payload.get("note", 60)), payload.get("finger"))
                self.cue.clear()
            except Exception as exc:  # noqa: BLE001
                log.warning("probe cue failed: %s", exc)

        self._send(
            TYPE_LATENCY_PRESENTED,
            {
                "probe_id": probe_id,
                "probe_message_id": envelope.get("message_id"),
                "dispatch_ns": (dispatch.local_dispatch_ns if dispatch else mono_ns() - receive_mono),
                "cue_ready_wall_ns": dispatch.cue_ready_wall_ns if dispatch else wall_ns(),
                "channels": self.cue.enabled_channels if self.cue else [],
                # Never call this a physical LED/actuator onset.
                "timing_kind": "software_dispatch_render",
            },
        )

    # ------------------------------------------------------------------
    # Per-frame loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        worker = self._vision_worker
        if worker is not None:
            # Reading an already-completed snapshot is lock-only and does
            # not wait for camera/MediaPipe. Do it before response MIDI so
            # provisional finger matching uses the freshest available
            # hands, while frame painting remains below the cue fast path.
            self.last_hands = worker.snapshot().hands

        # Latency-sensitive work comes first. In particular, an accepted
        # guidance event must reach session.tick() before a possibly slow
        # camera read/MediaPipe pass; otherwise the screen/LED/haptic cue
        # waits behind vision even though the network frame is already on
        # this machine.
        if self.midi_recorder is not None:
            new_events = self.midi_recorder.pop_events()
            self.raw_events.extend(new_events)
            for raw_event in new_events:
                if self.audio is not None:
                    if raw_event.type == "note_on":
                        self.audio.play_key(raw_event.note)
                    else:
                        self.audio.stop_key(raw_event.note)
                if raw_event.type == "note_on" and self.session is not None:
                    self.session.on_note(raw_event.note, wall_time_ns=int(raw_event.abs_time * 1e9))

        if self.session is not None:
            self.session.tick()

        if self.scheduler is not None and self.session is not None:
            due = self.scheduler.due(session_idle=self.session.current is None and self.session.pending == 0)
            if due is not None:
                self._release_scheduled_event(due)
            elif self.scheduler.finished and self.session.current is None and self.session.pending == 0:
                self.scheduler = None
                self._finish_session(reason="recording finished")

        if worker is not None:
            snapshot = worker.snapshot()
            if snapshot.sequence > self._vision_sequence:
                self._vision_sequence = snapshot.sequence
                self.last_hands = snapshot.hands
                if snapshot.frame is not None:
                    # The overlaid copy is for the person watching only.
                    self.view.set_frame(snapshot.frame)
            # The recording takes the camera's frame at capture rate, so a
            # slow tracking pass costs preview latency and never a
            # duplicated - visibly frozen - stretch of video.
            frame = snapshot.raw_frame if snapshot.raw_frame is not None else snapshot.frame
        else:
            # Synchronous fallback for headless/injected tests. A real
            # session always creates LatestVisionWorker in _ensure_camera.
            frame = self.camera.read() if self.camera is not None else None
            if frame is not None:
                if self.tracker is not None:
                    self.last_hands = self.tracker.process(frame)
                    draw_hands(frame, self.last_hands)
                self.view.set_frame(frame)
        if frame is not None:
            if self.video_writer is not None:
                self._write_video_frame(frame)

    def _write_video_frame(self, frame) -> None:
        # Same catch-up rule as the local quiz: the writer must end up
        # with elapsed*fps frames or the encoded clip plays back fast and
        # app.offline's frame mapping drifts. Capped at a second of
        # catch-up per tick so one stall cannot spin re-encoding.
        elapsed = time.time() - (self.video_start_time or time.time())
        target_frames = min(int(elapsed * self._video_fps) + 1, self._frames_written + int(self._video_fps) + 1)
        while self._frames_written < target_frames:
            self.video_writer.write(frame)
            self._frames_written += 1

    # ------------------------------------------------------------------
    # Finishing
    # ------------------------------------------------------------------

    def _finish_session(self, reason: str = "") -> None:
        if self.session is None:
            return
        session, self.session = self.session, None
        self.scheduler = None
        session.stop()

        midi_start = self.midi_recorder.start_time if self.midi_recorder else self.video_start_time
        if self.midi_recorder is not None:
            self.raw_events.extend(self.midi_recorder.pop_events())
            self.midi_recorder.close()
            self.midi_recorder = None
        self._close_audio()
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        self._set_recording_indicator()
        if self.cue is not None:
            self.cue.close()
            self.cue = None
        # Symmetric with _start_session: guidance is over, so the camera
        # goes back. The final finger pass below reads the recorded file,
        # not the live device, so this cannot cut it short.
        self._release_camera()

        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None

        results = session.results()
        summary = summarize(results)
        self._save_session(session, results, summary, midi_start)
        # After the writer is closed and the folder is written: the last
        # moment at which every path is final and the first at which the
        # answer cannot still change.
        recording_problem = self._verify_recording()

        # Provisional summary first, so the teacher sees the outcome
        # immediately; the final one follows the offline pass below.
        self._send(
            TYPE_SESSION_FINISHED,
            {
                "stage": STAGE_PROVISIONAL,
                "reason": reason,
                "session_name": self.session_name,
                "event_count": len(results),
                "summary": summary,
            },
        )

        self.stop_btn.setEnabled(False)
        self.start_btn.setEnabled(True)
        for widget in (self.guidance_combo, self.timeout_spin, self.record_check):
            widget.setEnabled(True)
        self.set_settings_enabled(True)
        self._set_status(
            f"Session finished ({reason}). {len(results)} events saved to data/quiz/{self.session_name}/.",
            "ok",
        )
        self.lesson_label.setText("Recording starts when the teacher begins the lesson.")
        self.workspace_tabs.setCurrentIndex(self.results_tab_index)

        # Bound now rather than read later: the pass takes minutes, and
        # the next lesson may well have started by the time it reports.
        self._run_final_pass(session, results, self.session_id)

        # Last, so the session is fully saved and sent before anything
        # blocks: the finger pass above runs on its own thread and is not
        # held up by this box.
        if recording_problem:
            QMessageBox.critical(
                self,
                "This session has no video",
                f"The session was saved, but its recording is not on disk:\n\n{recording_problem}\n\n"
                "Nothing can re-create it. This session's finger verdicts are the live ones and "
                "cannot be reviewed by hand. Check the camera before running the next session.",
            )

    def _verify_recording(self) -> str:
        """What is wrong with this session's video, or "" if it is there.

        Checked on disk rather than inferred from the writer: an
        unopened writer, a camera that stopped producing frames and a
        folder that never appeared all end the same way - a session that
        looks finished and has nothing to review - and all three are
        invisible until somebody goes looking, which is too late."""
        if not self.record_check.isChecked() or not self.session_name:
            return ""
        problem, note = self._recording_state()
        append_session_log(f"{self.session_name}: {note}")
        return problem

    def _recording_state(self) -> tuple:
        directory = quiz_dir(self.session_name)
        if not directory.is_dir():
            return f"{directory} was never created.", f"FOLDER MISSING {directory}"
        video = quiz_raw_dir(self.session_name) / RAW_VIDEO_FILENAME
        if not video.is_file():
            return f"{video} does not exist.", f"VIDEO MISSING {video}"
        size = video.stat().st_size
        if size <= 0:
            return f"{video} is empty (0 bytes).", f"VIDEO EMPTY {video}"
        if self._frames_written <= 0:
            return (
                f"{video} exists but no frames were ever written into it.",
                f"VIDEO HAS NO FRAMES {video}",
            )
        return "", f"saved {video} ({size / 1e6:.1f} MB, {self._frames_written} frames)"

    def _save_session(self, session: StudentSession, results, summary, midi_start) -> None:
        """Writes the standard data/quiz/<name>/ layout, so every
        existing analysis tool - the quiz analysis window, the detail
        view, the participant export - reads a remote session exactly
        like a local one."""
        if not self.session_name:
            return
        directory = quiz_dir(self.session_name)
        raw = quiz_raw_dir(self.session_name)
        raw.mkdir(parents=True, exist_ok=True)

        if self.raw_events:
            save_raw_midi_log(self.raw_events, raw / RAW_MIDI_FILENAME)
        if self.video_start_time is not None:
            SyncInfo(
                video_start_time=self.video_start_time,
                midi_start_time=midi_start or self.video_start_time,
                led_on_time=self.led_on_time or 0.0,
                led_off_time=self.led_off_time or 0.0,
            ).save(raw / RAW_SYNC_FILENAME)

        pressed = [r for r in results if not r.timed_out and r.keypress_time and r.actual_note is not None]
        save_midi_log(
            [MidiEvent(time=r.keypress_time, note=r.actual_note) for r in pressed], raw / RAW_NOTES_FILENAME
        )
        save_quiz_results(results, directory / RESULTS_FILENAME)

        QuizMeta(
            quiz_name=self.session_name,
            song_name=f"remote:{self.session_id or 'live'}",
            keyboard_profile_name=self.cfg.active_keyboard_profile,
            port_name=self.cfg.midi.port_name,
            created_at=time.time(),
            timeout_s=self.timeout_spin.value(),
            note_count=len(results),
            hits=summary["hits"],
            misses=summary["misses"],
            note_accuracy=summary["note_accuracy"],
            mean_timing_error_s=summary["mean_timing_error_s"],
            finger_accuracy=None,
            guidance_type=f"remote-{self.guidance_combo.currentData()}",
            analyzed=False,
        ).save(directory / META_FILENAME)

    def _run_final_pass(self, session: StudentSession, results, session_id: Optional[str] = None) -> None:
        """Re-judge the fingers from the recorded video with
        app.offline.analyze_recording (through the shared AnalyzeWorker),
        then send the final results. Skipped silently when nothing was
        recorded - the provisional verdicts then stand, and say so."""
        raw = quiz_raw_dir(self.session_name)
        video = raw / RAW_VIDEO_FILENAME
        notes = raw / RAW_NOTES_FILENAME
        if not video.exists() or not notes.exists():
            self._set_status("No video recorded - provisional finger verdicts are final for this session.", "warn")
            return

        if self._analyze_running():
            self._set_status("A finger pass is already running - waiting for it to finish.", "warn")
            return

        responded = [e for e in session.events if not e.timed_out and e.actual_note is not None]
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._set_status("Re-checking fingers against the recorded video...", "idle")

        worker = AnalyzeWorker(video, notes, self.cfg.active_keyboard_profile, sync_path=raw / RAW_SYNC_FILENAME)
        worker.progress.connect(self._on_analyze_progress)
        worker.succeeded.connect(
            lambda matches: self._on_final_matches(session, responded, matches, session_id)
        )
        worker.failed.connect(self._on_analyze_failed)
        worker.finished.connect(lambda w=worker: self._release_analyze_worker(w))
        self._analyze_worker = worker
        worker.start()

    def _analyze_running(self) -> bool:
        """Is the offline finger pass still going? Same guard as
        ConnectionPanel._worker_running: once deleteLater() has destroyed
        the QThread, the surviving Python wrapper raises RuntimeError on
        any method call, so a dead wrapper must read as "not running"
        rather than crash the second session."""
        if self._analyze_worker is None:
            return False
        try:
            return self._analyze_worker.isRunning()
        except RuntimeError:
            self._analyze_worker = None
            return False

    def _release_analyze_worker(self, worker: AnalyzeWorker) -> None:
        if self._analyze_worker is worker:
            self._analyze_worker = None
        worker.deleteLater()

    def _on_analyze_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _on_analyze_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self._set_status(f"Final finger pass failed: {message} - provisional verdicts stand.", "warn")

    def _on_final_matches(
        self,
        session: StudentSession,
        responded: List[RemoteEvent],
        matches: List[Any],
        session_id: Optional[str] = None,
    ) -> None:
        self.progress.setVisible(False)
        for event, match in zip(responded, matches):
            updated = session.apply_offline_match(event.index, match)
            if updated is not None:
                self._send(
                    TYPE_PERFORMANCE_RESPONSE, updated.response_payload(STAGE_FINAL), session_id=session_id
                )

        results = session.results()
        summary = summarize(results)
        save_quiz_results(results, quiz_dir(self.session_name) / RESULTS_FILENAME)

        meta_path = quiz_dir(self.session_name) / META_FILENAME
        try:
            meta = QuizMeta.load(meta_path)
            meta.finger_accuracy = summary["finger_accuracy"]
            meta.analyzed = True
            meta.save(meta_path)
        except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
            log.warning("could not update %s: %s", meta_path, exc)

        self._send(
            TYPE_SESSION_FINISHED,
            {
                "stage": STAGE_FINAL,
                "session_name": self.session_name,
                "event_count": len(results),
                "summary": summary,
            },
            session_id=session_id,
        )
        self._set_status(
            f"Final results sent. Finger accuracy {(summary['finger_accuracy'] or 0) * 100:.0f}%.", "ok"
        )
        self._open_analysis_window()

    def _open_analysis_window(self) -> None:
        """The same review window a local quiz opens - a remote session's
        files are in the same place and the same format."""
        self._analysis_window = QuizAnalysisWindow(self.cfg, initial_quiz_name=self.session_name)
        self._analysis_window.show()

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._timer.stop()
        # leave_session() already finishes any running session and gives
        # the camera, tracker and socket back; the rest below are the
        # devices it does not own.
        self.leave_session()
        if self.midi_recorder is not None:
            self.midi_recorder.close()
        self._close_audio()
        if self.video_writer is not None:
            self.video_writer.release()
        if self.cue is not None:
            self.cue.close()
        if self.led_connected:
            self.led.close()
        super().closeEvent(event)
