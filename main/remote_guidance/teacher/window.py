"""Teacher Client - play a key, the student's finger cue fires.

The window walks through three stages, one on screen at a time (see
remote_guidance.gui_common.StageWindow): **sign in**, then **choose a
room**, then the session itself. Nothing about guiding is shown - and no
device is opened - until a room has actually been chosen.

In the session stage two tabs separate the ways to guide:

  - **Live**: the teacher's own camera + MIDI keyboard run the platform's
    existing detection chain (see live_detector.py), and every note-on
    becomes a `guidance.live` event carrying the note and the finger that
    played it.
  - **Pre-recorded**: an existing data/music or data/sequence folder is
    uploaded as a recording (metadata + note/finger events, no video) and
    triggered remotely. The student downloads it in full and schedules it
    locally, so no individual note waits on the network. The same tab can
    open the platform's existing Song Recording Wizard to create a new
    data/music entry first.

Signing in, choosing a room and arriving on the session page all touch no
device. Hardware is claimed only by an explicit action: **Start live
session**, the manual **Connect MIDI** check, or **Record a new song...**.
The live devices are handed back on Stop, on "Change room" and on close;
the recording wizard releases its devices when that window closes.

The live table shows what the student reported back - received, cue
presented, what was actually played, and the student's reaction time -
with each delay component in its own column so none of them can be
mistaken for another. The teacher's machine never re-scores anything:
correctness comes from the student, which is where the finger matching
actually ran.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.gui.image_view import ImageView
from app.gui.recording_wizard import RecordingWizard
from app.keyboard.midi_mapping import note_name
from app.music_recording import sanitize_song_name

from ..config import RemoteGuidanceConfig, teacher_config
from ..gui_common import (
    STATUS_STYLES,
    ApiCallWorker,
    StageWindow,
    header_label,
    room_summary,
)
from ..protocol import (
    PLAYBACK_MODES,
    PLAYBACK_PACED,
    STAGE_FINAL,
    TYPE_ERROR,
    TYPE_GUIDANCE_PRESENTED,
    TYPE_GUIDANCE_RECEIVED,
    TYPE_PERFORMANCE_RESPONSE,
    TYPE_PRESENCE,
    TYPE_RECORDING_PAUSE,
    TYPE_RECORDING_READY,
    TYPE_RECORDING_START,
    TYPE_RECORDING_STOP,
    TYPE_SESSION_FINISHED,
    TYPE_SESSION_PAUSE,
    TYPE_SESSION_RESUME,
    TYPE_SESSION_START,
    TYPE_SESSION_STOP,
    guidance_payload,
)
from ..qt_bridge import RemoteClientBridge
from ..timing import mono_ns, wall_ns
from .live_detector import TeacherLiveDetector
from .recording_import import ImportedRecording, describe, import_song, list_available_songs

log = logging.getLogger("remote_guidance.teacher.window")

TICK_MS = 33
# The student is told to begin this far in the future, so both ends have
# time to be ready. Only the *start* moment depends on this; every cue
# timing is still measured on the student's own clock.
REMOTE_START_LEAD_MS = 500

(
    COL_SEQ,
    COL_TARGET,
    COL_RECEIVED,
    COL_PRESENTED,
    COL_NET,
    COL_QUEUE,
    COL_DISPATCH,
    COL_ACTUAL,
    COL_RT,
    COL_NOTE_OK,
    COL_FINGER_OK,
    COL_STAGE,
) = range(12)

HEADERS = [
    "#",
    "Target",
    "Received",
    "Cue shown",
    "Net RTT (ms)",
    "Queue (ms)",
    "Dispatch (ms)",
    "Played",
    "Student RT (ms)",
    "Note",
    "Finger",
    "Stage",
]


class TeacherRemoteWindow(QMainWindow, StageWindow):
    def __init__(self, cfg: Config, remote: Optional[RemoteGuidanceConfig] = None):
        super().__init__()
        self.setWindowTitle("Remote Guidance - Teacher Client")

        self.remote = remote or RemoteGuidanceConfig.load()
        # The platform-wide config, kept so the Settings dialog can offer
        # "use this machine's setup" and so apply_settings() can rebuild
        # the role view below.
        self.base_cfg = cfg
        # The teacher's own camera/MIDI/profile - a copy, so this window
        # cannot repoint the shared settings the local tools use.
        self.cfg = teacher_config(cfg, self.remote)

        # Opened when the session stage is entered, not here.
        self.detector: Optional[TeacherLiveDetector] = None
        self.bridge: Optional[RemoteClientBridge] = None
        self.room: Optional[dict] = None
        self.session_id: Optional[str] = None
        self.session_active = False
        self.session_mode: Optional[str] = None
        self.recording_id: Optional[str] = None
        self.imported: Optional[ImportedRecording] = None
        self.recording_wizard: Optional[RecordingWizard] = None
        self._worker: Optional[ApiCallWorker] = None

        # message_id -> row, plus the teacher's own monotonic send stamp
        # for a clock-sync-free round-trip figure.
        self._rows: Dict[str, int] = {}
        self._send_mono: Dict[str, int] = {}

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.link_label = header_label("")
        self.status_label = header_label("Sign in to the relay to begin.")

        stages = self.build_stages(self.remote, "teacher", self._build_session_page())
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
        self.change_room_btn = QPushButton("Change room")
        self.change_room_btn.clicked.connect(self.back_to_rooms)

        top = QHBoxLayout()
        top.addWidget(self.room_label, 1)
        top.addWidget(self.change_room_btn)

        # -- live guidance ---------------------------------------------
        self.midi_btn = QPushButton("Connect MIDI")
        self.midi_btn.clicked.connect(self._toggle_midi)
        self.chord_check = QCheckBox("Chord detection")
        self.chord_check.setToolTip("Match simultaneous note-ons together so no fingertip is credited with two.")
        self.chord_check.setChecked(self.remote.teacher.chord_detection)
        self.chord_check.toggled.connect(self._on_chord_toggled)

        self.live_btn = QPushButton("Start live session")
        self.live_btn.clicked.connect(self._start_live_session)
        self.pause_btn = QPushButton("Pause")
        self.resume_btn = QPushButton("Resume")
        self.stop_btn = QPushButton("Stop")
        self.pause_btn.clicked.connect(self._pause)
        self.resume_btn.clicked.connect(self._resume)
        self.stop_btn.clicked.connect(self._stop)
        for button in (self.pause_btn, self.resume_btn, self.stop_btn):
            button.setEnabled(False)

        live_box = QGroupBox("Live guidance")
        live = QVBoxLayout(live_box)
        self.live_controls_row = QHBoxLayout()
        self.live_controls_row.addWidget(self.midi_btn)
        self.live_controls_row.addWidget(self.chord_check)
        self.live_controls_row.addStretch(1)
        self.live_controls_row.addWidget(self.live_btn)
        self.live_controls_row.addWidget(self.pause_btn)
        self.live_controls_row.addWidget(self.resume_btn)
        self.live_controls_row.addWidget(self.stop_btn)
        live.addLayout(self.live_controls_row)

        # -- pre-recorded ----------------------------------------------
        self.song_combo = QComboBox()
        self.song_refresh_btn = QPushButton("Refresh")
        self.song_refresh_btn.clicked.connect(self._refresh_songs)
        self.upload_btn = QPushButton("Upload")
        self.upload_btn.setEnabled(False)
        self.upload_btn.clicked.connect(self._upload_recording)
        self.playback_combo = QComboBox()
        for mode in PLAYBACK_MODES:
            self.playback_combo.addItem(
                {"paced": "Paced (wait for each response)", "original_timing": "Original timing"}[mode], mode
            )
        self.trigger_btn = QPushButton("Trigger on student")
        self.trigger_btn.setEnabled(False)
        self.trigger_btn.clicked.connect(self._trigger_recording)

        self.record_song_btn = QPushButton("Record a new song...")
        self.record_song_btn.setToolTip(
            "Open the platform's Song Recording Wizard using this teacher's camera, MIDI port and profile."
        )
        self.record_song_btn.clicked.connect(self._open_recording_wizard)
        self.recording_pause_btn = QPushButton("Pause")
        self.recording_resume_btn = QPushButton("Resume")
        self.recording_stop_btn = QPushButton("Stop")
        self.recording_pause_btn.clicked.connect(self._pause)
        self.recording_resume_btn.clicked.connect(self._resume)
        self.recording_stop_btn.clicked.connect(self._stop)
        for button in (self.recording_pause_btn, self.recording_resume_btn, self.recording_stop_btn):
            button.setEnabled(False)

        recording_box = QGroupBox("Song library and remote playback")
        recording = QVBoxLayout(recording_box)
        recording_intro = QLabel(
            "Record a teacher performance, or choose an existing music/sequence entry. Upload sends only "
            "note/finger timing data to the relay; Trigger starts a separate recorded-guidance session."
        )
        recording_intro.setWordWrap(True)
        recording.addWidget(recording_intro)

        song_row = QHBoxLayout()
        song_row.addWidget(QLabel("Song:"))
        song_row.addWidget(self.song_combo, 1)
        song_row.addWidget(self.song_refresh_btn)
        song_row.addWidget(self.upload_btn)
        song_row.addWidget(self.playback_combo)
        song_row.addWidget(self.trigger_btn)
        recording.addLayout(song_row)

        recorded_actions = QHBoxLayout()
        recorded_actions.addWidget(self.record_song_btn)
        recorded_actions.addStretch(1)
        recorded_actions.addWidget(self.recording_pause_btn)
        recorded_actions.addWidget(self.recording_resume_btn)
        recorded_actions.addWidget(self.recording_stop_btn)
        recording.addLayout(recorded_actions)

        # -- live view -------------------------------------------------
        self.view = ImageView()
        self.detect_label = header_label("Press Start live session to open the camera and MIDI keyboard.")

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.summary_view = QTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setMaximumHeight(130)
        self.summary_view.setPlaceholderText(
            "The student's own summary appears here when the session ends. It is computed on the student's "
            "machine by the platform's shared scoring - the server and this window never re-derive it."
        )

        camera_panel = QWidget()
        camera_layout = QVBoxLayout(camera_panel)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        camera_layout.addWidget(self.view, 1)
        camera_layout.addWidget(self.detect_label)

        events_panel = QWidget()
        events_layout = QVBoxLayout(events_panel)
        events_layout.setContentsMargins(0, 0, 0, 0)
        events_layout.addWidget(self.table, 1)
        events_layout.addWidget(self.summary_view)

        self.live_guidance_page = QWidget()
        live_layout = QVBoxLayout(self.live_guidance_page)
        live_layout.setContentsMargins(6, 6, 6, 6)
        live_layout.addWidget(live_box)
        live_layout.addWidget(camera_panel, 1)

        self.recorded_guidance_page = QWidget()
        recorded_layout = QVBoxLayout(self.recorded_guidance_page)
        recorded_layout.setContentsMargins(6, 6, 6, 6)
        recorded_layout.addWidget(recording_box)
        recorded_layout.addStretch(1)

        self.guidance_tabs = QTabWidget()
        self.live_tab_index = self.guidance_tabs.addTab(self.live_guidance_page, "Live guidance")
        self.recorded_tab_index = self.guidance_tabs.addTab(self.recorded_guidance_page, "Recorded guidance")
        self.guidance_tabs.setCurrentIndex(self.live_tab_index)

        # A splitter rather than fixed stretch factors: how much room the
        # active guidance page needs versus the event table depends on the
        # lesson, and dragging is quicker than resizing the window.
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.guidance_tabs)
        splitter.addWidget(events_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        self.guidance_tabs.setMinimumHeight(230)
        events_panel.setMinimumHeight(150)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)

        self._refresh_songs()
        return page

    def _set_status(self, message: str, level: str = "idle") -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(STATUS_STYLES.get(level, ""))

    def apply_settings(self) -> None:
        """Rebuild the teacher's Config view from the saved settings.

        No device is open when this runs - the Settings button is
        disabled for the length of a session - so the next
        _open_detector() simply picks up the new camera and profile."""
        self.cfg = teacher_config(self.base_cfg, self.remote)
        self.chord_check.setChecked(self.remote.teacher.chord_detection)
        self._set_status(
            f"Settings saved. Camera {self.cfg.camera.index}, MIDI {self.cfg.midi.port_name!r}, "
            f"profile {self.cfg.active_keyboard_profile!r} - used from the next session.",
            "ok",
        )

    # ------------------------------------------------------------------
    # Stage transitions
    # ------------------------------------------------------------------

    def enter_session(self, room: dict) -> None:
        """Reaching the session page opens the WebSocket and nothing
        else. The camera, MediaPipe and the MIDI port wait for Start
        live session. The MIDI port comes from this client's Settings."""
        self.room = room
        self.room_label.setText(room_summary(room))
        self.persist_connection(self.remote, room)
        self._connect_websocket(room)
        self._refresh_songs()
        self._sync_guidance_controls()
        self.detect_label.setText(
            "Camera and MIDI open when you press Start live session."
        )

    def leave_session(self) -> None:
        """Hand the camera and the socket back. Going back to the room
        list should not keep a device busy."""
        if self.recording_wizard is not None:
            self.recording_wizard.close()
            self.recording_wizard = None
        if self.session_active and self.session_id and self.bridge is not None:
            self._send(TYPE_SESSION_STOP, {"reason": "teacher left the session"})
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        self._close_detector()
        self.session_active = False
        self.session_mode = None
        self.session_id = None
        self.recording_id = None
        self.room = None
        self._rows.clear()
        self._send_mono.clear()
        self.table.setRowCount(0)
        self.summary_view.clear()
        self.link_label.setText("")
        self.live_btn.setEnabled(True)
        self._sync_guidance_controls()

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def _open_detector(self) -> bool:
        """Claim the camera and MediaPipe, if they are not already open.

        Idempotent, because both Start live session and the manual
        Connect MIDI button call it. A camera failure is reported and
        not raised: the teacher can still trigger a pre-recorded
        sequence, and note-only guidance still works from MIDI alone."""
        if self.detector is not None:
            return True
        try:
            self.detector = TeacherLiveDetector(self.cfg, chord_detection=self.chord_check.isChecked())
        except Exception as exc:  # noqa: BLE001 - the client is still useful without a camera
            self.detector = None
            self._set_status(f"Camera/hand tracking unavailable ({exc}). Live finger detection is off.", "warn")
            return False
        if self.detector.profile_error:
            self._set_status(self.detector.profile_error, "warn")
        else:
            self._set_status(f"Teacher profile {self.cfg.active_keyboard_profile!r} loaded.", "ok")
        return True

    def _close_detector(self) -> None:
        """Give the camera, MediaPipe and the MIDI port back the moment
        guiding stops, so the next tool to want them is not blocked."""
        if self.detector is not None:
            self.detector.close()
            self.detector = None
        self.midi_btn.setText("Connect MIDI")
        self.view.clear()

    def _connect_midi(self) -> bool:
        """Open the MIDI port saved in this client's Settings, opening
        the detector that owns it first if it is not up yet."""
        if not self._open_detector() or self.detector is None:
            return False
        if self.detector.midi_connected:
            return True
        try:
            port = self.detector.connect_midi(self.cfg.midi.port_name or None)
        except RuntimeError as exc:
            QMessageBox.warning(self, "MIDI connection failed", str(exc))
            return False
        self.midi_btn.setText("Disconnect MIDI")
        self.detect_label.setText(f"MIDI connected on {port}. Play a key to send guidance.")
        return True

    def _toggle_midi(self) -> None:
        if self.detector is not None and self.detector.midi_connected:
            self.detector.disconnect_midi()
            self.midi_btn.setText("Connect MIDI")
            self.detect_label.setText("MIDI disconnected.")
            return
        self._connect_midi()

    def _on_chord_toggled(self, checked: bool) -> None:
        if self.detector is not None:
            self.detector.chord_detection = checked

    def _refresh_songs(self, preferred_label: Optional[str] = None) -> None:
        current = preferred_label or self.song_combo.currentData()
        self.song_combo.clear()
        for entry in list_available_songs():
            self.song_combo.addItem(entry.label, entry.label)
        if current:
            index = self.song_combo.findData(current)
            if index >= 0:
                self.song_combo.setCurrentIndex(index)
        self._sync_guidance_controls()

    def _sync_guidance_controls(self) -> None:
        """Keep the two tabs mutually exclusive when hardware/session state is active."""
        wizard_open = self.recording_wizard is not None
        idle = not self.session_active
        live_active = self.session_active and self.session_mode == "live"
        recorded_active = self.session_active and self.session_mode == "recording"

        self.change_room_btn.setEnabled(not wizard_open)
        self.guidance_tabs.setTabEnabled(self.live_tab_index, not wizard_open and (idle or live_active))
        self.guidance_tabs.setTabEnabled(self.recorded_tab_index, idle or recorded_active or wizard_open)

        self.live_btn.setEnabled(idle and not wizard_open)
        self.midi_btn.setEnabled(not wizard_open and (idle or live_active))
        self.chord_check.setEnabled(not wizard_open and (idle or live_active))
        for button in (self.pause_btn, self.resume_btn, self.stop_btn):
            button.setEnabled(live_active)

        have_song = self.room is not None and self.song_combo.count() > 0
        self.record_song_btn.setEnabled(idle and not wizard_open)
        self.song_combo.setEnabled(idle and not wizard_open)
        self.song_refresh_btn.setEnabled(idle and not wizard_open)
        self.upload_btn.setEnabled(have_song and idle and not wizard_open)
        self.playback_combo.setEnabled(idle and not wizard_open)
        self.trigger_btn.setEnabled(self.recording_id is not None and idle and not wizard_open)
        for button in (self.recording_pause_btn, self.recording_resume_btn, self.recording_stop_btn):
            button.setEnabled(recorded_active)

        if hasattr(self, "settings_btn"):
            self.set_settings_enabled(idle and not wizard_open)

    def _open_recording_wizard(self) -> None:
        """Open the platform's existing song wizard from the recorded tab."""
        if self.room is None or self.session_active:
            return
        if self.recording_wizard is not None:
            self.recording_wizard.raise_()
            self.recording_wizard.activateWindow()
            return
        # Connect MIDI can open the live detector before a session. The
        # wizard needs exclusive ownership of the same teacher devices.
        self._close_detector()
        try:
            wizard = RecordingWizard(self.cfg)
        except Exception as exc:  # camera construction can fail before the wizard is shown
            QMessageBox.warning(self, "Could not open Song Recording Wizard", str(exc))
            return

        self.recording_wizard = wizard
        wizard.setParent(self, Qt.WindowType.Window)
        wizard.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        wizard.resize(1000, 860)
        wizard.finished.connect(lambda result, opened=wizard: self._on_recording_wizard_finished(opened, result))
        wizard.show()
        self._sync_guidance_controls()
        self._set_status(
            "Song Recording Wizard opened with the teacher camera, MIDI port and keyboard profile.",
            "idle",
        )

    def _on_recording_wizard_finished(self, wizard: RecordingWizard, _result: int) -> None:
        saved = wizard.review_page.isComplete()
        song_label = None
        if saved:
            song_label = f"music/{sanitize_song_name(wizard.info_page.song_name())}"
        if self.recording_wizard is wizard:
            self.recording_wizard = None
        self._refresh_songs(song_label)
        self._sync_guidance_controls()
        if saved:
            self._set_status("Song saved and selected. Upload it when ready.", "ok")

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _connect_websocket(self, room: dict) -> None:
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

    def _on_link_state(self, state: str, detail: str) -> None:
        self.link_label.setText(f"Link: {state} {('- ' + detail) if detail else ''}")
        self.link_label.setStyleSheet(STATUS_STYLES["ok" if state == "connected" else "warn"])

    def _run_api(self, call, on_ok, busy: str) -> None:
        if self._worker_running():
            return
        self._set_status(busy)
        worker = ApiCallWorker(call, self)
        worker.succeeded.connect(on_ok)
        worker.failed.connect(lambda message: self._set_status(message, "error"))
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        self._worker = worker
        worker.start()

    def _worker_running(self) -> bool:
        """Is a REST call still in flight? A QThread already handed to
        deleteLater() leaves a Python wrapper whose C++ object is gone,
        and calling isRunning() on that raises rather than returning
        False - so a dead wrapper has to read as "not running"."""
        if self._worker is None:
            return False
        try:
            return self._worker.isRunning()
        except RuntimeError:
            self._worker = None
            return False

    def _release_worker(self, worker: ApiCallWorker) -> None:
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _send(self, type_: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self.bridge is None:
            self._set_status("Not connected to the relay.", "warn")
            return None
        return self.bridge.send(type_, payload, session_id=self.session_id)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _start_live_session(self) -> None:
        """Devices are claimed here, not on arriving at this page.

        Both are opened *before* the session is created on the relay, so
        a camera or MIDI problem is seen while nothing has been started
        yet. Neither failure aborts: a session with no camera can still
        send note-only guidance and can still trigger a pre-recorded
        sequence, which is the mode that needs no teacher hardware at
        all."""
        if not self.room or self.session_active or self.recording_wizard is not None:
            return
        self._open_detector()
        if not self._connect_midi():
            self._set_status(
                "No MIDI keyboard connected - the session will start, but live cues need one. "
                "Choose a port in Settings, then press Connect MIDI.",
                "warn",
            )
        self._run_api(
            lambda: self.api.create_session(self.room["room_id"], {"mode": "live"}),
            self._on_session_created,
            "Opening a session...",
        )

    def _on_session_created(self, session: dict) -> None:
        self._activate_session(session, "live")
        self._set_status("Session running. Play a key to guide the student.", "ok")

    def _activate_session(self, session: dict, mode: str) -> None:
        self.session_id = session["session_id"]
        self.session_active = True
        self.session_mode = mode
        if self.bridge is not None:
            self.bridge.session_id = self.session_id
        self.table.setRowCount(0)
        self._rows.clear()
        self._send_mono.clear()
        self._send(
            TYPE_SESSION_START,
            {
                "mode": mode,
                "started_at_unix_ns": wall_ns(),
            },
        )
        self.guidance_tabs.setCurrentIndex(
            self.live_tab_index if mode == "live" else self.recorded_tab_index
        )
        self._sync_guidance_controls()

    def _pause(self) -> None:
        self._send(TYPE_SESSION_PAUSE, {})
        self._send(TYPE_RECORDING_PAUSE, {})
        self._set_status("Paused.", "warn")

    def _resume(self) -> None:
        self._send(TYPE_SESSION_RESUME, {})
        self._set_status("Resumed.", "ok")

    def _stop(self) -> None:
        self._send(TYPE_SESSION_STOP, {})
        self._send(TYPE_RECORDING_STOP, {})
        self.session_active = False
        self.session_mode = None
        # Symmetric with the start: guiding is over, so the camera and
        # the MIDI port go back. Starting again reopens them.
        self._close_detector()
        self._sync_guidance_controls()
        self.detect_label.setText("Camera and MIDI released. Start live session opens them again.")
        self._set_status("Stop sent. Waiting for the student's results...", "idle")

    # ------------------------------------------------------------------
    # Pre-recorded
    # ------------------------------------------------------------------

    def _upload_recording(self) -> None:
        label = self.song_combo.currentData()
        if not self.room or not label:
            return
        try:
            imported = import_song(label)
        except (FileNotFoundError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not read that song", str(exc))
            return
        self.imported = imported
        self._run_api(
            lambda: self.api.upload_recording(self.room["room_id"], imported.to_payload()),
            self._on_recording_uploaded,
            f"Uploading {describe(imported)}...",
        )

    def _on_recording_uploaded(self, summary: dict) -> None:
        self.recording_id = summary["recording_id"]
        self._sync_guidance_controls()
        self._set_status(
            f"Uploaded {summary['name']!r}: {summary['event_count']} events. "
            "Only the note/finger events were sent - no video. Press Trigger on student when ready.",
            "ok",
        )

    def _trigger_recording(self) -> None:
        """Create a recorded-guidance session, then trigger local playback."""
        if not self.recording_id or not self.room or self.session_active:
            return
        # Recorded playback runs on the student; no teacher camera/MIDI
        # should remain claimed from an earlier manual Connect MIDI.
        self._close_detector()
        playback_mode = self.playback_combo.currentData() or PLAYBACK_PACED
        self._run_api(
            lambda: self.api.create_session(
                self.room["room_id"],
                {
                    "mode": "recording",
                    "playback_mode": playback_mode,
                    "recording_id": self.recording_id,
                },
            ),
            self._on_recording_session_created,
            "Opening a recorded-guidance session...",
        )

    def _on_recording_session_created(self, session: dict) -> None:
        self._activate_session(session, "recording")
        # The student is given a short-future start moment rather than
        # "now", so both ends can be ready; after download, every cue is
        # scheduled locally and no individual note waits on the network.
        self._send(
            TYPE_RECORDING_START,
            {
                "recording_id": self.recording_id,
                "playback_mode": self.playback_combo.currentData() or PLAYBACK_PACED,
                "start_at_unix_ns": wall_ns() + REMOTE_START_LEAD_MS * 1_000_000,
                "server_lead_ms": REMOTE_START_LEAD_MS,
            },
        )
        self._set_status("Recorded guidance triggered on the student.", "ok")

    # ------------------------------------------------------------------
    # Live detection -> guidance
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        # No detector until the session stage is entered, so the timer is
        # harmless on the sign-in and room pages.
        if self.detector is None:
            return
        result = self.detector.poll()
        if result.frame is not None:
            self.view.set_frame(result.frame)
        if result.actions:
            self._send_guidance(result.actions)

    def _send_guidance(self, actions) -> None:
        if self.session_id is None:
            self.detect_label.setText("Detected a note, but no session is running - press Start live session.")
            return
        payload = guidance_payload(actions, timeout_s=self.remote.student.default_timeout_s)
        # Stamped on the teacher's own monotonic clock, before the send,
        # so the round trip below needs no clock synchronisation.
        sent_mono = mono_ns()
        envelope = self._send("guidance.live", payload)
        if envelope is None:
            return
        self._send_mono[envelope["message_id"]] = sent_mono
        self._add_row(envelope["message_id"], actions)

        summary = ", ".join(
            f"{a.note_name or note_name(a.note)}->{a.finger or '?'}"
            f"{f' p={a.finger_probability:.2f}' if a.finger_probability is not None else ''}"
            for a in actions
        )
        self.detect_label.setText(f"Sent: {summary}")

    def _add_row(self, message_id: str, actions) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[message_id] = row
        target = ", ".join(f"{a.note_name or note_name(a.note)}/{a.finger or '?'}" for a in actions)
        self._set_cell(row, COL_SEQ, str(row + 1))
        self._set_cell(row, COL_TARGET, target)
        for column in (
            COL_RECEIVED,
            COL_PRESENTED,
            COL_NET,
            COL_QUEUE,
            COL_DISPATCH,
            COL_ACTUAL,
            COL_RT,
            COL_NOTE_OK,
            COL_FINGER_OK,
        ):
            self._set_cell(row, column, "-")
        self._set_cell(row, COL_STAGE, "sent")
        self.table.scrollToBottom()

    def _set_cell(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, column, item)

    # ------------------------------------------------------------------
    # Student events
    # ------------------------------------------------------------------

    def _on_message(self, envelope: Dict[str, Any]) -> None:
        msg_type = envelope.get("type")
        payload = envelope.get("payload") or {}

        if msg_type == TYPE_GUIDANCE_RECEIVED:
            self._on_received(payload)
        elif msg_type == TYPE_GUIDANCE_PRESENTED:
            self._on_presented(payload)
        elif msg_type == TYPE_PERFORMANCE_RESPONSE:
            self._on_response(payload)
        elif msg_type == TYPE_SESSION_FINISHED:
            self._on_finished(payload, envelope.get("session_id"))
        elif msg_type == TYPE_RECORDING_READY:
            self._set_status(
                f"Student ready: {payload.get('guidance_mode')} guidance, channels "
                f"{', '.join(payload.get('channels') or []) or 'none'}.",
                "ok",
            )
        elif msg_type == TYPE_PRESENCE:
            members = payload.get("members") or []
            self.link_label.setText("Link: connected - " + ", ".join(f"{m['username']} ({m['role']})" for m in members))
        elif msg_type == TYPE_ERROR:
            self._set_status(f"Server: {payload.get('detail')}", "error")

    def _row_for(self, payload: Dict[str, Any]) -> Optional[int]:
        return self._rows.get(payload.get("guidance_message_id", ""))

    def _on_received(self, payload: Dict[str, Any]) -> None:
        row = self._row_for(payload)
        if row is None:
            return
        # Measured entirely on this machine's monotonic clock: send stamp
        # to ack arrival. No clock synchronisation is involved, which is
        # exactly why this is the delay figure shown rather than a
        # difference between the two hosts' wall clocks.
        sent = self._send_mono.get(payload.get("guidance_message_id", ""))
        if sent is not None:
            self._set_cell(row, COL_NET, f"{(mono_ns() - sent) / 1e6:.1f}")
        self._set_cell(row, COL_RECEIVED, "yes" if payload.get("accepted", True) else "REFUSED")
        if not payload.get("accepted", True):
            self._set_status(f"Student refused a cue: {payload.get('refused_reason')}", "warn")

    def _on_presented(self, payload: Dict[str, Any]) -> None:
        row = self._row_for(payload)
        if row is None:
            return
        timings = payload.get("timings") or {}
        self._set_cell(row, COL_PRESENTED, "yes")
        self._set_cell(row, COL_QUEUE, _ms(timings.get("queue_wait_ns")))
        self._set_cell(row, COL_DISPATCH, _ms(timings.get("local_dispatch_ns")))

    def _on_response(self, payload: Dict[str, Any]) -> None:
        row = self._row_for(payload)
        if row is None:
            return
        actual = payload.get("actual_note")
        self._set_cell(
            row,
            COL_ACTUAL,
            "(no press)" if actual is None else f"{note_name(int(actual))}/{payload.get('actual_finger') or '?'}",
        )
        self._set_cell(row, COL_RT, _ms(payload.get("reaction_time_ns")))
        self._set_cell(row, COL_NOTE_OK, _tick_mark(payload.get("note_correct")))
        self._set_cell(row, COL_FINGER_OK, _tick_mark(payload.get("finger_correct")))
        self._set_cell(row, COL_STAGE, str(payload.get("stage", "provisional")))

    def _on_finished(self, payload: Dict[str, Any], finished_session_id: Optional[str] = None) -> None:
        stage = payload.get("stage", "provisional")
        summary = payload.get("summary") or {}
        if self.session_active and (finished_session_id is None or finished_session_id == self.session_id):
            # The student has released its devices and scheduler. Keep
            # session_id for the stored-summary fetch, but unlock both
            # guidance tabs so the teacher can prepare the next run.
            self.session_active = False
            self.session_mode = None
            self._close_detector()
            self._sync_guidance_controls()
        lines = [
            f"Session {stage} results - {payload.get('event_count', 0)} events "
            f"({payload.get('session_name', '')})",
            f"  Note accuracy:   {_pct(summary.get('note_accuracy'))}",
            f"  Finger accuracy: {_pct(summary.get('finger_accuracy'))} (FA main)",
            f"  Mean RT:         {_secs(summary.get('mean_timing_error_s'))}",
            f"  Timeouts:        {summary.get('misses', 0)}",
            "",
            "Computed on the student's machine by the platform's shared scoring "
            "(app.quiz.summarize) - not re-derived here or on the server.",
        ]
        self.summary_view.setPlainText("\n".join(lines))
        if stage == STAGE_FINAL:
            self._set_status("Final results received.", "ok")
            self._fetch_stored_summary()
        else:
            self._set_status("Provisional results received - waiting for the student's offline finger pass.", "idle")

    def _fetch_stored_summary(self) -> None:
        """Pull the persisted session back from the relay, so what the
        teacher keeps is the stored record rather than only what happened
        to arrive live."""
        if not self.session_id:
            return
        self._run_api(
            lambda: self.api.session_summary(self.session_id),
            self._on_stored_summary,
            "Fetching the stored session...",
        )

    def _on_stored_summary(self, data: Dict[str, Any]) -> None:
        counts = data.get("counts", {})
        reported = data.get("student_reported_summary") or {}
        text = self.summary_view.toPlainText()
        self.summary_view.setPlainText(
            text
            + "\n\nStored on the relay: "
            + f"{counts.get('guidance_events', 0)} guidance / {counts.get('performance_events', 0)} performance "
            + f"events ({counts.get('final', 0)} final).\n"
            + f"Stored summary source: {data.get('summary_source') or 'none yet'}; "
            + f"note accuracy {_pct(reported.get('note_accuracy'))}."
        )

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self.leave_session()
        super().closeEvent(event)


def _ms(value: Optional[int]) -> str:
    return "-" if value is None else f"{value / 1e6:.1f}"


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _secs(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.3f}s"


def _tick_mark(value: Optional[bool]) -> str:
    if value is None:
        return "?"
    return "yes" if value else "no"
