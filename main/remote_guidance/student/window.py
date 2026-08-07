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
MediaPipe, the LED strip and the MIDI port are all claimed by "Ready for
guidance" and released when the session ends, on "Change room" and on
close.

The LED strip has no port to pick - it is auto-detected and connected at
that same moment, and a failure to find it is a warning rather than a
refusal. "Connect LED" stays on the page as a manual override for
checking the wiring beforehand.

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
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
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
from note_led_map import WHITE_LEDS

from ..config import RemoteGuidanceConfig, student_config
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



class StudentRemoteWindow(QMainWindow, StageWindow):
    def __init__(self, cfg: Config, remote: Optional[RemoteGuidanceConfig] = None):
        super().__init__()
        self.setWindowTitle("Remote Guidance - Student Client")

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
        self.midi_recorder: Optional[RawMidiRecorder] = None
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

        # The stage label and the client's one Settings button share the
        # top row, so settings are reachable from every stage without
        # taking a line of their own.
        header = QHBoxLayout()
        header.addWidget(self.stage_label, 1)
        header.addWidget(self.build_settings_button())

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.addLayout(header)
        layout.addWidget(self.link_label)
        layout.addWidget(self.status_label)
        layout.addWidget(stages, 1)
        self.setCentralWidget(central)

    def _build_session_page(self) -> QWidget:
        self.room_label = header_label("")
        back_btn = QPushButton("Change room")
        back_btn.clicked.connect(self.back_to_rooms)

        top = QHBoxLayout()
        top.addWidget(self.room_label, 1)
        top.addWidget(back_btn)

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

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("session name (folder under data/quiz/)")

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(1.0, 30.0)
        self.timeout_spin.setSingleStep(0.5)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(self.remote.student.default_timeout_s)

        self.record_check = QCheckBox("Record video + MIDI (needed for the final finger pass)")
        self.record_check.setChecked(self.remote.student.record_video)

        self.led_btn = QPushButton("Connect LED")
        self.led_btn.setToolTip(
            "Optional - the strip is found and connected automatically when a session starts. This is for "
            "checking the wiring beforehand."
        )
        self.led_btn.clicked.connect(self._toggle_led)
        self.led_status = QLabel("LED: auto-connects at start")

        self.start_btn = QPushButton("Ready for guidance")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_session)
        self.stop_btn = QPushButton("End session")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self._finish_session(reason="stopped by the student"))

        # Every control here is locked once a session is running (see
        # _start_session); Start/End sit beside them rather than in a
        # separate header, since this whole page only exists once a room
        # has been chosen.
        setup_box = QGroupBox("Session")
        setup = QVBoxLayout(setup_box)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Guidance:"))
        row1.addWidget(self.guidance_combo, 1)
        row1.addWidget(QLabel("Timeout:"))
        row1.addWidget(self.timeout_spin)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Name:"))
        row2.addWidget(self.name_edit, 1)
        row2.addWidget(self.led_status)
        row2.addWidget(self.led_btn)
        row2.addWidget(self.record_check)
        row2.addStretch(1)
        row2.addWidget(self.start_btn)
        row2.addWidget(self.stop_btn)
        setup.addLayout(row1)
        setup.addLayout(row2)

        self.view = ImageView()
        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["#", "Target", "Played", "RT (ms)", "Net (ms)", "Queue (ms)", "Result"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        # The student's own camera over the per-event results. A splitter,
        # so either half can be given the space - a student watching their
        # hands wants the camera large, one checking reaction times wants
        # the table.
        camera_panel = QWidget()
        camera_layout = QVBoxLayout(camera_panel)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        camera_layout.addWidget(self.view, 1)
        camera_layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(camera_panel)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        camera_panel.setMinimumHeight(180)
        self.table.setMinimumHeight(140)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(setup_box)
        layout.addWidget(splitter, 1)
        return page

    def _set_status(self, message: str, level: str = "idle") -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(STATUS_STYLES.get(level, ""))

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
        self.led_btn.setText("Disconnect LED")
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

    def _toggle_led(self) -> None:
        """Manual override, for checking the wiring before a session."""
        if self.led_connected:
            if self.led_mapper is not None:
                self.led_mapper.clear_all()
            self.led.close()
            self.led_connected = False
            self.led_status.setText("LED: not connected")
            self.led_btn.setText("Connect LED")
            return
        problem = self._connect_led()
        if problem is not None:
            QMessageBox.warning(self, "LED connection failed", problem)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def enter_session(self, room: dict) -> None:
        """Reaching the session page opens the WebSocket and nothing
        else. The camera, MediaPipe and the MIDI port are claimed by
        "Ready for guidance" (see _start_session), using the MIDI port
        saved in this client's Settings. The LED stays on its own manual
        button."""
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

    def _on_link_state(self, state: str, detail: str) -> None:
        self.link_label.setText(f"Link: {state} {('- ' + detail) if detail else ''}")
        self.link_label.setStyleSheet(STATUS_STYLES["ok" if state == "connected" else "warn"])

    def _send(self, type_: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if self.bridge is None:
            return
        self.bridge.send(type_, payload, session_id=self.session_id)

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
        name = sanitize_quiz_name(self.name_edit.text() or f"remote-{int(time.time())}")
        guidance_mode = self.guidance_combo.currentData()

        problems = self.remote.serial_port_problems()
        if problems and wants_haptic(guidance_mode):
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
            QMessageBox.warning(self, "MIDI connection failed", str(exc))
            return

        self.session_name = name
        self.raw_events = []
        self.table.setRowCount(0)
        self.led_on_time = self.led_off_time = None

        if self.record_check.isChecked():
            self._start_recording(name)

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

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        for widget in (self.guidance_combo, self.name_edit, self.timeout_spin, self.record_check):
            widget.setEnabled(False)
        # Devices are open now; changing which ones to open is meaningless
        # until this session ends.
        self.set_settings_enabled(False)

        self.cue.show_message("Waiting for the teacher...")
        self._set_status(
            f"Ready - guidance mode {guidance_mode}, channels: {', '.join(self.cue.enabled_channels) or 'none'}.",
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
                "session_name": self.session_name,
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

    def _start_recording(self, name: str) -> None:
        raw = quiz_raw_dir(name)
        raw.mkdir(parents=True, exist_ok=True)
        self.video_path = raw / RAW_VIDEO_FILENAME
        frame = self.camera.read() if self.camera else None
        if frame is None:
            self._set_status("Camera not available - continuing without video (no final finger pass).", "warn")
            return
        h, w = frame.shape[:2]
        fps = self.cfg.camera.fps or 30
        self.video_writer = cv2.VideoWriter(str(self.video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        self._video_fps = fps
        self._frames_written = 0
        self.video_start_time = time.time()
        if self.led_connected:
            QTimer.singleShot(LED_FLASH_DELAY_MS, self._flash_leds_on)

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
        payload = envelope.get("payload") or {}
        self.session_id = envelope.get("session_id")
        if self.bridge is not None:
            self.bridge.session_id = self.session_id
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
        self._set_status(f"Session started by the teacher ({payload.get('mode', 'live')}).", "ok")

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
        target = event.target
        if target is None or self.cue is None:
            return None
        return self.cue.show_target(target.note, target.finger)

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
        frame = self.camera.read() if self.camera is not None else None
        if frame is not None:
            if self.tracker is not None:
                self.last_hands = self.tracker.process(frame)
                draw_hands(frame, self.last_hands)
            self.view.set_frame(frame)
            if self.video_writer is not None:
                self._write_video_frame(frame)

        if self.midi_recorder is not None:
            new_events = self.midi_recorder.pop_events()
            self.raw_events.extend(new_events)
            for raw_event in new_events:
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
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        if self.cue is not None:
            self.cue.close()
            self.cue = None
        # Symmetric with _start_session: guidance is over, so the camera
        # goes back. The final finger pass below reads the recorded file,
        # not the live device, so this cannot cut it short.
        self._release_camera()

        results = session.results()
        summary = summarize(results)
        self._save_session(session, results, summary, midi_start)

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
        for widget in (self.guidance_combo, self.name_edit, self.timeout_spin, self.record_check):
            widget.setEnabled(True)
        self.set_settings_enabled(True)
        self._set_status(f"Session finished ({reason}). {len(results)} events saved.", "ok")

        self._run_final_pass(session, results)

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

    def _run_final_pass(self, session: StudentSession, results) -> None:
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
        worker.succeeded.connect(lambda matches: self._on_final_matches(session, responded, matches))
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

    def _on_final_matches(self, session: StudentSession, responded: List[RemoteEvent], matches: List[Any]) -> None:
        self.progress.setVisible(False)
        for event, match in zip(responded, matches):
            updated = session.apply_offline_match(event.index, match)
            if updated is not None:
                self._send(TYPE_PERFORMANCE_RESPONSE, updated.response_payload(STAGE_FINAL))

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
        if self.video_writer is not None:
            self.video_writer.release()
        if self.cue is not None:
            self.cue.close()
        if self.led_connected:
            self.led.close()
        super().closeEvent(event)
