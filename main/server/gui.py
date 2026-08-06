"""Qt window for the relay - status, log, and the relay's own settings.

The relay owns its settings the same way each client owns theirs: one
Settings button here opens server_config.json's editable fields (bind
address, port, registration, token lifetimes) in their own dialog. The
launcher has no remote-settings window any more, and nothing outside this
folder edits this file - which is also what keeps server/ copyable to a
host on its own.

The server itself runs in a *separate* process driven by QProcess, not on
a thread inside this window. That is deliberate: uvicorn installs signal
handlers and owns an asyncio loop, and a stop button has to be able to
end it reliably - terminate(), then kill() if it ignores that. A thread
would leave a half-stopped event loop behind and a port that is still
bound.

Room and connection state shown here is parsed from the child's own log
lines (this window owns that process, so it is the one consumer entitled
to them) and cross-checked against /api/v1/health for the authoritative
counts. No new unauthenticated endpoint is introduced just to populate a
dashboard.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Set

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import DEFAULT_CONFIG_PATH, ServerConfig

SERVER_PARENT_DIR = Path(__file__).resolve().parent.parent
HEALTH_POLL_MS = 2000
MAX_LOG_BLOCKS = 2000

# Matches the two lines server/websocket.py logs on connect/disconnect.
_CONNECT_RE = re.compile(r"ws connect: (?P<user>\S+) \((?P<role>\w+)\) -> room (?P<room>\S+)")
_DISCONNECT_RE = re.compile(r"ws disconnect: (?P<user>\S+) from room (?P<room>\S+)")


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(int(value))
    return spin


class ServerSettingsDialog(QDialog):
    """server_config.json's editable fields, and nothing else.

    Deliberately not everything in ServerConfig: the JWT key paths and
    the TLS certificate are file locations an operator sets up once with
    server/tools/, and a form that let them be retyped would mostly
    produce a relay that cannot sign a token. Those are shown read-only
    on the main window instead.

    Saving writes the file the panel was started from and says that a
    running relay keeps its old values until restarted - the child
    process read its config when it started."""

    def __init__(self, config: ServerConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Relay Server Settings")
        self.config = config
        self.saved = False

        self.host_edit = QLineEdit(config.bind_host)
        self.host_edit.setToolTip(
            "127.0.0.1 answers only this machine; 0.0.0.0 answers any interface, which is what a second "
            "machine needs to reach it."
        )
        self.port_spin = _spin(1, 65535, config.port)
        self.registration_check = QCheckBox("Allow new accounts to register")
        self.registration_check.setChecked(config.allow_registration)
        self.registration_check.setToolTip(
            "Turn this off once the accounts exist. The first account on an empty database is always allowed "
            "through, so this cannot lock you out of a fresh install."
        )
        self.access_ttl = _spin(1, 365, config.access_token_ttl_days)
        self.access_ttl.setSuffix(" days")
        self.refresh_ttl = _spin(1, 3650, config.refresh_token_ttl_days)
        self.refresh_ttl.setSuffix(" days")

        listener = QGroupBox("Listener")
        listener_form = QFormLayout(listener)
        listener_form.addRow("Bind host:", self.host_edit)
        listener_form.addRow("Port:", self.port_spin)

        accounts = QGroupBox("Accounts and tokens")
        accounts_form = QFormLayout(accounts)
        accounts_form.addRow("", self.registration_check)
        accounts_form.addRow("Access token:", self.access_ttl)
        accounts_form.addRow("Refresh token:", self.refresh_ttl)

        note = QLabel(
            "The clients must be pointed at this port too - it is their default in the platform's "
            "config.json. TLS certificates and the JWT signing keys are files, set up with server/tools/ "
            "and named in server_config.json; they are not editable here."
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(listener)
        layout.addWidget(accounts)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def collect(self) -> ServerConfig:
        self.config.bind_host = self.host_edit.text().strip() or "127.0.0.1"
        self.config.port = self.port_spin.value()
        self.config.allow_registration = self.registration_check.isChecked()
        self.config.access_token_ttl_days = self.access_ttl.value()
        self.config.refresh_token_ttl_days = self.refresh_ttl.value()
        return self.config

    def _save(self) -> None:
        config = self.collect()
        try:
            config.save()
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", f"{config.source_path or 'server_config.json'}: {exc}")
            return
        self.saved = True
        self.accept()


class ServerControlPanel(QMainWindow):
    def __init__(self, config: ServerConfig, config_path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Remote Guidance - Relay Server")
        self.config = config
        self.config_path = config_path
        self.process: Optional[QProcess] = None
        # room id -> {"user (role)"} as seen in the child's log
        self._rooms: Dict[str, Set[str]] = {}

        # Everything about the listener is shown, not edited, here: the
        # bind address and the port are settings, and they live behind
        # the one Settings button below - the same shape the two clients
        # have.
        self.host_label = QLabel(config.bind_host)
        self.port_label = QLabel(str(config.port))
        self.registration_label = QLabel("")

        self.db_label = QLabel(str(config.database_file))
        self.db_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.http_label = QLabel("")
        self.http_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.ws_label = QLabel("")
        self.ws_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tls_label = QLabel("on (https/wss)" if config.tls.enabled else "off (http/ws)")
        self.state_label = QLabel("stopped")

        info_box = QGroupBox("Server")
        info_form = QFormLayout(info_box)
        info_form.addRow("Bind host:", self.host_label)
        info_form.addRow("Port:", self.port_label)
        info_form.addRow("Registration:", self.registration_label)
        info_form.addRow("Database:", self.db_label)
        info_form.addRow("HTTP API:", self.http_label)
        info_form.addRow("WebSocket:", self.ws_label)
        info_form.addRow("TLS:", self.tls_label)
        info_form.addRow("Status:", self.state_label)

        self.start_btn = QPushButton("Start server")
        self.stop_btn = QPushButton("Stop server")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_server)
        self.stop_btn.clicked.connect(self.stop_server)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setToolTip("Bind address, port, registration and token lifetimes. Opens its own window.")
        self.settings_btn.clicked.connect(self.open_settings)
        self.clear_log_btn = QPushButton("Clear log")
        self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.settings_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.clear_log_btn)

        self.rooms_tree = QTreeWidget()
        self.rooms_tree.setHeaderLabels(["Room / client", "Role"])
        self.rooms_tree.setColumnWidth(0, 340)
        rooms_box = QGroupBox("Rooms and connected clients")
        rooms_layout = QVBoxLayout(rooms_box)
        rooms_layout.addWidget(self.rooms_tree)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_BLOCKS)
        log_box = QGroupBox("Server log")
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.log_view)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(info_box)
        layout.addLayout(btn_row)
        layout.addWidget(rooms_box, 1)
        layout.addWidget(log_box, 2)
        self.setCentralWidget(central)

        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._poll_health)
        self._health_timer.start(HEALTH_POLL_MS)

        self._refresh_addresses()
        self._append_log(
            f"Config: {config_path or config.source_path or '(built-in defaults)'}\n"
            f"JWT signing keys: {config.jwt_private_key_file}\n"
            "Press Start server to launch the relay as its own process.\n"
        )

    # ------------------------------------------------------------------

    def _bind_host(self) -> str:
        return self.config.bind_host

    def _client_host(self) -> str:
        host = self._bind_host()
        return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    def _refresh_addresses(self) -> None:
        port = self.config.port
        scheme, ws_scheme = self.config.scheme, self.config.ws_scheme
        host = self._client_host()
        self.host_label.setText(
            self._bind_host()
            + ("  (this machine only)" if self._bind_host() not in ("0.0.0.0", "::") else "  (any interface)")
        )
        self.port_label.setText(str(port))
        self.registration_label.setText("open" if self.config.allow_registration else "closed")
        self.http_label.setText(f"{scheme}://{host}:{port}/api/v1")
        self.ws_label.setText(f"{ws_scheme}://{host}:{port}/ws/v1/rooms/<room_id>")

    def open_settings(self) -> None:
        """The relay's one Settings button. Refused while the child is
        running: it read server_config.json when it started, so an edit
        now would describe a listener that is not the one answering."""
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(
                self,
                "Server is running",
                "Stop the server before changing its settings - the running process read its configuration "
                "when it started.",
            )
            return
        dialog = ServerSettingsDialog(self.config, parent=self)
        dialog.exec()
        if not dialog.saved:
            return
        self._refresh_addresses()
        self._append_log(
            f"settings saved to {self.config.source_path or DEFAULT_CONFIG_PATH}\n"
            "Point the clients at this address if the port changed.\n"
        )

    # ------------------------------------------------------------------

    def start_server(self) -> None:
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            return

        args = ["-m", "server", "--host", self._bind_host(), "--port", str(self.config.port)]
        if self.config_path:
            args += ["--config", str(self.config_path)]

        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(args)
        self.process.setWorkingDirectory(str(SERVER_PARENT_DIR))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process_output)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)
        self.process.start()

        if not self.process.waitForStarted(5000):
            QMessageBox.warning(self, "Could not start server", self.process.errorString())
            self.process = None
            return

        self.state_label.setText("starting...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.settings_btn.setEnabled(False)
        self._append_log(f"$ {sys.executable} {' '.join(args)}\n")

    def stop_server(self) -> None:
        if self.process is None:
            return
        self.stop_btn.setEnabled(False)
        self._append_log("stopping server...\n")
        self.process.terminate()
        if not self.process.waitForFinished(5000):
            self._append_log("server did not exit on terminate - killing it\n")
            self.process.kill()
            self.process.waitForFinished(3000)

    def _on_finished(self, exit_code: int, _status) -> None:
        self._append_log(f"server process exited (code {exit_code})\n")
        self.process = None
        self._rooms.clear()
        self._refresh_rooms_tree()
        self.state_label.setText("stopped")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.settings_btn.setEnabled(True)

    def _on_error(self, error) -> None:
        if self.process is not None:
            self._append_log(f"process error: {self.process.errorString()}\n")

    # ------------------------------------------------------------------

    def _read_process_output(self) -> None:
        if self.process is None:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._append_log(text)
        changed = False
        for line in text.splitlines():
            changed |= self._track_presence(line)
        if changed:
            self._refresh_rooms_tree()

    def _track_presence(self, line: str) -> bool:
        m = _CONNECT_RE.search(line)
        if m:
            self._rooms.setdefault(m["room"], set()).add(f"{m['user']}\t{m['role']}")
            return True
        m = _DISCONNECT_RE.search(line)
        if m:
            members = self._rooms.get(m["room"])
            if members:
                for entry in list(members):
                    if entry.split("\t")[0] == m["user"]:
                        members.discard(entry)
                if not members:
                    self._rooms.pop(m["room"], None)
            return True
        return False

    def _refresh_rooms_tree(self) -> None:
        self.rooms_tree.clear()
        for room_id, members in sorted(self._rooms.items()):
            room_item = QTreeWidgetItem([room_id, f"{len(members)} connected"])
            for entry in sorted(members):
                username, role = entry.split("\t")
                room_item.addChild(QTreeWidgetItem([username, role]))
            self.rooms_tree.addTopLevelItem(room_item)
        self.rooms_tree.expandAll()

    def _append_log(self, text: str) -> None:
        self.log_view.moveCursor(self.log_view.textCursor().MoveOperation.End)
        self.log_view.insertPlainText(text)
        self.log_view.moveCursor(self.log_view.textCursor().MoveOperation.End)

    # ------------------------------------------------------------------

    def _poll_health(self) -> None:
        if self.process is None or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        health = fetch_health(self._client_host(), self.config.port, self.config.scheme, timeout=1.0)
        if health is None:
            self.state_label.setText("starting... (health check not answering yet)")
            return
        self.state_label.setText(
            f"running - protocol v{health.get('protocol_version')}, schema v{health.get('schema_version')}, "
            f"{health.get('rooms_online', 0)} room(s), {health.get('connections', 0)} connection(s)"
        )

    def closeEvent(self, event) -> None:
        self._health_timer.stop()
        if self.process is not None:
            self.stop_server()
        super().closeEvent(event)


def fetch_health(host: str, port: int, scheme: str = "http", timeout: float = 1.0) -> Optional[dict]:
    """GET /api/v1/health, or None if nothing is listening. Used by the
    panel's status line and by the launcher's Launch Local Stack check."""
    url = f"{scheme}://{host}:{port}/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def run_gui(config: ServerConfig, config_path: Optional[Path] = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = ServerControlPanel(config, config_path=config_path)
    window.resize(900, 720)
    window.show()
    return app.exec()


def main() -> int:
    return run_gui(ServerConfig.load())


if __name__ == "__main__":
    raise SystemExit(main())
