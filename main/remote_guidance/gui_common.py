"""The pieces both remote clients need before any guidance happens:
sign in, then pick a room.

They are two separate widgets on purpose. Each client shows one thing at
a time - the login form, then room selection, then the actual tools (see
StageWindow below) - rather than putting every control on screen at once
and leaving the user to work out which parts apply yet.

Credentials are never persisted: the password box is used once, at login,
and cleared afterwards. Only the server URL, the username and the last
room id go back into config.json.

The two demo accounts (see DEMO_ACCOUNTS) are pre-filled into the form so
a run does not start by typing the same pair into two windows. That is a
convenience constant in this file, not stored state - config.json still
holds no password.

Every network call runs on a worker thread, so a window never blocks: the
REST calls go through ApiCallWorker below, and the WebSocket lives in
remote_guidance.qt_bridge.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .config import NetworkConfig, RemoteGuidanceConfig
from .network_client import RemoteApiClient, RemoteApiError
from .protocol import JOIN_CODE_LENGTH, looks_like_join_code

# Shown greyed out when the field is empty - taken from the config
# default so the hint can never drift from what a fresh install uses.
DEFAULT_SERVER_URL = NetworkConfig.server_url

STATUS_STYLES = {
    "ok": "color: #2e7d32;",
    "warn": "color: #ef6c00;",
    "error": "color: #c62828;",
    "idle": "color: #555;",
}

# The stages every remote client walks through, in order.
STAGE_SIGN_IN, STAGE_ROOM, STAGE_SESSION = 0, 1, 2
STAGE_NAMES = ("Sign in", "Room", "Session")

# A stand-in "room" for the launcher's Demo Mode (launcher.py section 12):
# a client can show its full session page for screenshots without a relay,
# a login or a peer. It only fills the room chip - enter_session() skips all
# networking when the window was built with demo=True, so nothing here is
# ever sent anywhere.
DEMO_ROOM = {"name": "Demo (offline)"}

# The demo accounts that exist on the development relay, pre-filled into
# the sign-in form: role -> (username, password). Both clients are
# started for every trial run, and typing the same two pairs each time
# is pure friction.
#
# These are deliberately *not* config: rule 4.7 (see doc/REMOTE_GUIDANCE.md)
# keeps every password out of config.json, and a constant here cannot be
# written back by RemoteGuidanceConfig.save(). Point a client at a real
# relay and the box is simply overtyped - nothing here is sent unless
# the user presses Sign in. For a deployment with real accounts, empty
# this mapping.
DEMO_ACCOUNTS = {
    "student": ("demostudent", "demostudent"),
    "teacher": ("demoteacher", "demoteacher"),
}


class ApiCallWorker(QThread):
    """Runs one REST call off the GUI thread.

    Login, room creation and recording upload all block on the network,
    and doing any of them inline would freeze the window for as long as
    the server takes to answer - which on a bad link is exactly when the
    user most wants the UI to stay responsive."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, call: Callable[[], Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._call = call

    def run(self) -> None:
        try:
            result = self._call()
        except RemoteApiError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - report, never crash the window
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(result)


class _ApiPanel(QWidget):
    """Shared REST plumbing for the sign-in and room panels.

    The worker guard is the interesting part: a finished QThread is
    handed to deleteLater(), which destroys the C++ object while Python
    still holds the wrapper. Calling isRunning() on that raises
    RuntimeError, which used to make the *second* click on any button
    fail. A dead wrapper reads as "nothing running"."""

    statusChanged = Signal(str, str)  # message, level

    def __init__(self, api: RemoteApiClient, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.api = api
        self._worker: Optional[ApiCallWorker] = None
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

    def set_status(self, message: str, level: str = "idle") -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(STATUS_STYLES.get(level, ""))
        self.statusChanged.emit(message, level)

    def busy(self) -> bool:
        if self._worker is None:
            return False
        try:
            return self._worker.isRunning()
        except RuntimeError:
            self._worker = None
            return False

    def run_call(self, call: Callable[[], Any], on_ok: Callable[[Any], None], busy_message: str) -> None:
        if self.busy():
            return
        self.set_status(busy_message, "idle")
        self._set_enabled(False)

        worker = ApiCallWorker(call, self)
        worker.succeeded.connect(lambda result: (self._set_enabled(True), on_ok(result)))
        worker.failed.connect(lambda message: (self._set_enabled(True), self.set_status(message, "error")))
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        self._worker = worker
        worker.start()

    def _release_worker(self, worker: ApiCallWorker) -> None:
        """Forget a finished worker before letting Qt delete it. Identity
        is checked because `finished` arrives as a queued event, so a
        newer call could already own the slot."""
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _set_enabled(self, enabled: bool) -> None:
        """Subclasses disable their buttons while a call is in flight."""


class SignInPanel(_ApiPanel):
    """Step 1, and the only thing on screen until it succeeds."""

    signedIn = Signal(object)

    def __init__(self, remote: RemoteGuidanceConfig, role: str, api: RemoteApiClient,
                 parent: Optional[QWidget] = None):
        super().__init__(api, parent)
        self.remote = remote
        self.role = role

        self.server_edit = QLineEdit(remote.network.server_url)
        self.server_edit.setPlaceholderText(DEFAULT_SERVER_URL)

        # config.json keeps a single network.username shared by both
        # roles, so whichever client ran last leaves its name behind. A
        # value belonging to the *other* role would pre-fill the wrong
        # account here, so it is ignored in favour of this role's demo
        # user; anything else the user typed themselves is kept.
        self.demo_username, self.demo_password = DEMO_ACCOUNTS.get(role, ("", ""))
        other_demo_users = {user for name, (user, _) in DEMO_ACCOUNTS.items() if name != role}
        stored_username = (remote.network.username or "").strip()
        keep_stored = bool(stored_username) and stored_username not in other_demo_users
        username = stored_username if keep_stored else self.demo_username

        self.username_edit = QLineEdit(username)
        self.username_edit.setPlaceholderText("username")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("password (never saved to config.json)")
        # Only the demo account's own password is offered; a custom
        # username starts with an empty box rather than a wrong secret.
        self.restore_demo_password()
        self.password_edit.returnPressed.connect(self.sign_in)
        self.username_edit.returnPressed.connect(lambda: self.password_edit.setFocus())

        self.login_btn = QPushButton("Sign in")
        self.login_btn.setDefault(True)
        self.login_btn.clicked.connect(self.sign_in)
        self.register_btn = QPushButton("Create account")
        self.register_btn.clicked.connect(self.register)
        self.health_btn = QPushButton("Test server")
        self.health_btn.clicked.connect(self.check_health)

        form = QFormLayout()
        form.addRow("Relay server:", self.server_edit)
        form.addRow("Username:", self.username_edit)
        form.addRow("Password:", self.password_edit)

        buttons = QHBoxLayout()
        buttons.addWidget(self.health_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.register_btn)
        buttons.addWidget(self.login_btn)

        box = QGroupBox(f"Sign in as {role}")
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        box_layout = QVBoxLayout(box)
        box_layout.addLayout(form)
        box_layout.addLayout(buttons)
        box_layout.addWidget(self.status_label)

        hint = QLabel(
            "The relay routes guidance between the two clients; it holds no camera, MIDI or actuator. "
            "Start one with \"python -m server --gui\", or point this at a remote address."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(STATUS_STYLES["idle"])

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(box)
        layout.addWidget(hint)
        layout.addStretch(2)

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.login_btn, self.register_btn, self.health_btn):
            widget.setEnabled(enabled)

    def restore_demo_password(self) -> None:
        """Put the demo password back in the box - at startup, and again
        after signing out, since _on_signed_in empties it. Does nothing
        once the username has been changed to a real account, which is
        what keeps a demo secret from being sent to a real relay."""
        if self.demo_password and self.username_edit.text().strip() == self.demo_username:
            self.password_edit.setText(self.demo_password)
        else:
            self.password_edit.clear()

    def _sync_base_url(self) -> None:
        self.api.base_url = self.server_edit.text().strip().rstrip("/")

    def check_health(self) -> None:
        self._sync_base_url()
        self.run_call(
            self.api.health,
            lambda data: self.set_status(
                f"Server ok - protocol v{data.get('protocol_version')}, "
                f"{data.get('rooms_online', 0)} room(s) online.",
                "ok",
            ),
            "Contacting server...",
        )

    def sign_in(self) -> None:
        username, password = self.username_edit.text().strip(), self.password_edit.text()
        if not username or not password:
            self.set_status("Enter a username and password first.", "warn")
            return
        self._sync_base_url()
        self.run_call(lambda: self.api.login(username, password), self._on_signed_in, "Signing in...")

    def register(self) -> None:
        username, password = self.username_edit.text().strip(), self.password_edit.text()
        if not username or not password:
            self.set_status("Enter a username and password to register with.", "warn")
            return
        self._sync_base_url()
        self.run_call(
            lambda: self.api.register(username, password, self.role), self._on_signed_in, "Creating the account..."
        )

    def _on_signed_in(self, session) -> None:
        # The password did its one job; clearing the box means it is not
        # sitting in a widget for the rest of the session either.
        self.password_edit.clear()
        if session.role != self.role:
            self.set_status(
                f"Signed in as {session.username}, but that account's role is {session.role!r} and this is the "
                f"{self.role} client. Sign in with a {self.role} account.",
                "error",
            )
            return
        self.set_status(f"Signed in as {session.username}.", "ok")
        self.signedIn.emit(session)


class RoomPanel(_ApiPanel):
    """Step 2. Shown only once signed in, and nothing else is on screen
    while the user decides which room to work in.

    **One field, no mode selector, and no room id anywhere.** The join
    code is the only room identifier a person ever sees or types; the
    uuid stays in the relay's database and in config.json, where it is
    written and read but never shown.

    Both roles behave the same way. The student types the code the
    teacher read out. The teacher's field takes a code too - reopening
    yesterday's room, which is what the pre-filled value does on the next
    launch - and anything that is *not* a code is taken as the name of a
    new room. `POST /rooms/join` already returns the room to its own
    owner without adding a membership row, so one endpoint covers both.

    This replaced a two-entry mode combo per role ("create" / "reopen by
    id", "join" / "rejoin by id") whose default action for the teacher
    was Create while the pre-filled value was the last room *id* - one
    click made a room named after a uuid."""

    roomReady = Signal(object)
    signOutRequested = Signal()

    def __init__(self, remote: RemoteGuidanceConfig, role: str, api: RemoteApiClient,
                 parent: Optional[QWidget] = None):
        super().__init__(api, parent)
        self.remote = remote
        self.role = role
        self.room: Optional[dict] = None
        # What the user actually typed, when it was a code. The relay
        # returns join_code only to a room's owner, so for a student this
        # is the only place the code exists to be saved for next time.
        self.entered_code = ""

        self.who_label = QLabel("")
        self.who_label.setStyleSheet(STATUS_STYLES["idle"])

        self.value_edit = QLineEdit(remote.network.join_code)
        self.value_edit.setPlaceholderText(
            "join code, e.g. K7M4PQ"
            if role != "teacher"
            else "join code to reopen a room, e.g. K7M4PQ"
        )
        self.value_edit.returnPressed.connect(self.go)
        self.go_btn = QPushButton("Continue")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self.go)
        self.back_btn = QPushButton("Sign out")
        self.back_btn.clicked.connect(self.signOutRequested)

        form = QFormLayout()
        # The teacher's field takes a name as well as a code, so it is
        # not labelled as if a code were the only thing allowed.
        form.addRow("Join code:" if role != "teacher" else "Room:", self.value_edit)

        hint = QLabel(
            "The teacher's join code. Ask for it, or reuse the one filled in from last time."
            if role != "teacher"
            else "Type a code to reopen that room, or any other text to start a new room with that name."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(STATUS_STYLES["idle"])

        buttons = QHBoxLayout()
        buttons.addWidget(self.back_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.go_btn)

        box = QGroupBox("Choose a room")
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self.who_label)
        box_layout.addLayout(form)
        box_layout.addWidget(hint)
        box_layout.addLayout(buttons)
        box_layout.addWidget(self.status_label)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(box)
        layout.addStretch(2)

    def adopt_session(self, session) -> None:
        self.who_label.setText(f"Signed in as {session.username} ({session.role}).")

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.go_btn, self.back_btn, self.value_edit):
            widget.setEnabled(enabled)

    def go(self) -> None:
        value = self.value_edit.text().strip()
        if not value:
            self.set_status(
                "Enter the teacher's join code first."
                if self.role != "teacher"
                else "Enter a join code to reopen a room, or a name for a new one.",
                "warn",
            )
            return

        if looks_like_join_code(value):
            self.entered_code = value.upper()
            self.run_call(lambda: self.api.join_room(value), self._on_room, "Joining the room...")
            return

        if self.role != "teacher":
            # Students have nothing else this field could mean, and
            # "that is not a code" is a better answer than a 404 from
            # the relay.
            self.set_status(
                f"{value!r} is not a join code - they are {JOIN_CODE_LENGTH} characters, like K7M4PQ.", "warn"
            )
            return

        self.entered_code = ""
        self.run_call(lambda: self.api.create_room(value), self._on_room, "Creating the room...")

    def _on_room(self, room: dict) -> None:
        self.room = room
        self.set_status(f"Room {room['name']!r} ready.", "ok")
        self.roomReady.emit(room)


class StageWindow:
    """Mixin giving a client its sign-in -> room -> session flow.

    The three stages live in a QStackedWidget, so exactly one is on
    screen at a time. Sub-classes build the session page and say what to
    do when it is entered and left - which is where each client opens and
    releases its camera, so nothing touches a device until the user has
    actually reached the point of using one."""

    def build_stages(self, remote: RemoteGuidanceConfig, role: str, session_page: QWidget) -> QWidget:
        self.stage_role = role
        self.api = RemoteApiClient(remote.network.http_base, verify_tls=remote.network.verify_tls)
        self.sign_in_panel = SignInPanel(remote, role, self.api)
        self.room_panel = RoomPanel(remote, role, self.api)

        self.sign_in_panel.signedIn.connect(self._on_signed_in)
        self.room_panel.roomReady.connect(self._on_room_chosen)
        self.room_panel.signOutRequested.connect(self.sign_out)

        self.stages = QStackedWidget()
        self.stages.addWidget(self.sign_in_panel)
        self.stages.addWidget(self.room_panel)
        self.stages.addWidget(session_page)

        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet(STATUS_STYLES["idle"])
        self._show_stage(STAGE_SIGN_IN)
        return self.stages

    # -- stage transitions ---------------------------------------------

    def _show_stage(self, stage: int) -> None:
        self.stages.setCurrentIndex(stage)
        self.stage_label.setText(f"Step {stage + 1} of 3 - {STAGE_NAMES[stage]}")

    def _on_signed_in(self, session) -> None:
        self.room_panel.adopt_session(session)
        self._show_stage(STAGE_ROOM)

    def _on_room_chosen(self, room: dict) -> None:
        self._show_stage(STAGE_SESSION)
        self.enter_session(room)

    def sign_out(self) -> None:
        """Back to step 1, releasing whatever the session stage held."""
        self.leave_session()
        self.api.logout()
        self.sign_in_panel.restore_demo_password()
        self.sign_in_panel.set_status("Signed out.", "idle")
        self._show_stage(STAGE_SIGN_IN)

    def back_to_rooms(self) -> None:
        self.leave_session()
        self.room_panel.set_status("Pick a room to continue.", "idle")
        self._show_stage(STAGE_ROOM)

    # -- settings -------------------------------------------------------

    def build_settings_button(self) -> QPushButton:
        """The client's single Settings button.

        One per client, and the only way into its settings: the launcher
        no longer has a remote-settings window, and neither client can
        see the other's devices. It stays on screen at every stage but
        goes insensitive while a session is running - changing which
        camera to open is meaningless once one is open."""
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setToolTip(
            f"This {self.stage_role}'s camera, MIDI port and keyboard profile. Opens in its own window."
        )
        self.settings_btn.clicked.connect(self.open_settings)
        return self.settings_btn

    def open_settings(self) -> None:
        # Imported here rather than at module scope: settings_window
        # imports this module for its shared styling, so a top-level
        # import either way round would be circular.
        from .settings_window import RemoteSettingsDialog

        dialog = RemoteSettingsDialog(
            self.stage_role, self.base_cfg, RemoteGuidanceConfig.load(), parent=self
        )
        dialog.exec()
        if not dialog.saved:
            return
        self.remote = dialog.remote
        self.apply_settings()

    def set_settings_enabled(self, enabled: bool) -> None:
        button = getattr(self, "settings_btn", None)
        if button is not None:
            button.setEnabled(enabled)

    # -- for subclasses -------------------------------------------------

    def enter_session(self, room: dict) -> None:
        raise NotImplementedError

    def leave_session(self) -> None:
        raise NotImplementedError

    def apply_settings(self) -> None:
        """Re-read whatever the new settings changed. Called after the
        dialog saved; devices are never open at that point (the button is
        disabled during a session), so nothing has to be reopened."""

    # -- helpers --------------------------------------------------------

    def ws_url(self, room_id: str) -> str:
        base = self.sign_in_panel.server_edit.text().strip().rstrip("/")
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        else:
            ws_base = base
        return f"{ws_base}/ws/v1/rooms/{room_id}"

    def persist_connection(self, remote: RemoteGuidanceConfig, room: Optional[dict]) -> None:
        """Save the parts that are not secrets, so the next launch starts
        where this one left off.

        The room id is still stored - the relay's REST calls are keyed by
        it - but it is now write-only as far as the user is concerned:
        the room field they see is the join code. The relay returns
        join_code only to a room's owner, so for a student the code has
        to come from what they typed."""
        remote.network.server_url = self.sign_in_panel.server_edit.text().strip().rstrip("/")
        remote.network.username = self.sign_in_panel.username_edit.text().strip()
        if room:
            remote.network.room_id = room.get("room_id", "")
            code = room.get("join_code") or self.room_panel.entered_code
            if code:
                remote.network.join_code = code
        remote.save()


def room_summary(room: dict) -> str:
    text = f"Room {room['name']!r}"
    if room.get("join_code"):
        text += f"  -  join code {room['join_code']}"
    return text


def header_label(text: str = "", level: str = "idle") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(STATUS_STYLES.get(level, ""))
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label
