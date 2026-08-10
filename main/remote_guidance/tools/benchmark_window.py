"""A small GUI around the self-contained latency benchmark CLI.

The run itself happens in a *separate* process driven by QProcess, not on
a thread in this window. A benchmark is a long, precisely paced loop -
1000 probes at 500 ms is over eight minutes - and its timing must not
share a process with a Qt event loop that could stall it. Running it out
of process also means Stop genuinely stops it.

The two password boxes feed the child's stdin once and are never stored,
never put on the command line (where they would be visible in a process
list) and never written to config.json.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import Config

from ..config import RemoteGuidanceConfig
from ..gui_common import DEMO_ACCOUNTS, STATUS_STYLES
from .latency_benchmark import DEFAULT_COUNT, DEFAULT_INTERVAL_S, DEFAULT_TIMEOUT_S, DEFAULT_WARMUP, RESULTS_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class LatencyBenchmarkWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None, remote: Optional[RemoteGuidanceConfig] = None):
        super().__init__()
        self.setWindowTitle("Remote Guidance - Network Latency Benchmark")
        self.remote = remote or RemoteGuidanceConfig.load()
        self.process: Optional[QProcess] = None

        network = self.remote.network
        teacher_demo = DEMO_ACCOUNTS.get("teacher", ("", ""))
        student_demo = DEMO_ACCOUNTS.get("student", ("", ""))
        self.server_edit = QLineEdit(network.server_url)
        # network.username is shared by the two normal clients and may
        # therefore contain the last Student login on a one-machine run.
        # Prefer the role-specific demo identity here instead of silently
        # putting a Student account in the Teacher field.
        teacher_username = teacher_demo[0] or network.username
        self.username_edit = QLineEdit(teacher_username)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        if teacher_username == teacher_demo[0]:
            self.password_edit.setText(teacher_demo[1])
        self.password_edit.setPlaceholderText("teacher password (sent once, never stored)")
        self.student_username_edit = QLineEdit(student_demo[0])
        self.student_password_edit = QLineEdit(student_demo[1])
        self.student_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.student_password_edit.setPlaceholderText("student password (sent once, never stored)")
        self.external_check = QCheckBox("Use an external Student Client for real UI / LED / haptic timing")
        self.external_check.toggled.connect(self._set_external_mode)
        self.room_edit = QLineEdit(network.room_id)
        self.room_edit.setPlaceholderText("created automatically in built-in mode")

        self.count_spin = _spin(1, 100000, DEFAULT_COUNT)
        self.warmup_spin = _spin(0, 1000, DEFAULT_WARMUP)
        self.interval_spin = _double(0.01, 10.0, DEFAULT_INTERVAL_S, " s")
        self.timeout_spin = _double(0.1, 60.0, DEFAULT_TIMEOUT_S, " s")
        self.trigger_check = QCheckBox("Ask the student to fire its real LED/haptic cue for each probe")
        self.synced_check = QCheckBox("Both hosts are NTP-synchronised (enables a clock-corrected one-way figure)")
        self.verify_check = QCheckBox("Verify TLS certificates")
        self.verify_check.setChecked(network.verify_tls)

        conn_box = QGroupBox("Connection")
        conn_form = QFormLayout(conn_box)
        conn_form.addRow("Server:", self.server_edit)
        conn_form.addRow("Teacher username:", self.username_edit)
        conn_form.addRow("Teacher password:", self.password_edit)
        conn_form.addRow("Student username:", self.student_username_edit)
        conn_form.addRow("Student password:", self.student_password_edit)
        conn_form.addRow("External room id:", self.room_edit)
        conn_form.addRow("", self.verify_check)

        run_box = QGroupBox("Run")
        run_form = QFormLayout(run_box)
        run_form.addRow("Measured probes:", self.count_spin)
        run_form.addRow("Warm-up probes:", self.warmup_spin)
        run_form.addRow("Interval:", self.interval_spin)
        run_form.addRow("Ack timeout:", self.timeout_spin)
        run_form.addRow("", self.external_check)
        run_form.addRow("", self.trigger_check)
        run_form.addRow("", self.synced_check)

        self.start_btn = QPushButton("Start benchmark")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.estimate_label = QLabel("")
        for spin in (self.count_spin, self.warmup_spin):
            spin.valueChanged.connect(self._update_estimate)
        self.interval_spin.valueChanged.connect(self._update_estimate)

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.estimate_label)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(5000)

        note = QLabel(
            "Default: only the Relay Server and this window are needed. The benchmark logs in as both roles, "
            "creates a temporary room, opens both WebSockets, then closes (but does not delete) that room. "
            "Both accounts must already exist on the relay. The built-in Student measures network/relay timing, "
            "not real Student UI or hardware.\n"
            "External Student mode is retained only for real UI / LED / haptic dispatch tests. Results are written "
            f"to {RESULTS_DIR} as samples.csv, summary.json and latency.png.\n"
            "Round-trip time is the primary metric - it needs no clock synchronisation. In external mode, an "
            "unsynchronised one-way value is shown as RTT/2 and labelled a symmetry-based estimate. Every cue "
            "time here is software dispatch/render timing, not physical LED or actuator onset."
        )
        note.setWordWrap(True)
        note.setStyleSheet(STATUS_STYLES["idle"])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(conn_box)
        layout.addWidget(run_box)
        layout.addLayout(buttons)
        layout.addWidget(note)
        layout.addWidget(self.output, 1)
        self.setCentralWidget(central)

        self._set_external_mode(False)
        self._update_estimate()

    # ------------------------------------------------------------------

    def _update_estimate(self) -> None:
        total = self.count_spin.value() + self.warmup_spin.value()
        minutes = total * self.interval_spin.value() / 60.0
        self.estimate_label.setText(f"{total} probes ~ {minutes:.1f} min")

    def _set_external_mode(self, external: bool) -> None:
        self.student_username_edit.setEnabled(not external)
        self.student_password_edit.setEnabled(not external)
        self.room_edit.setEnabled(external)
        self.trigger_check.setEnabled(external)
        self.trigger_check.setChecked(False)
        # The two built-in endpoints are in this one process and share a
        # host clock exactly. External hosts need an explicit NTP claim.
        self.synced_check.setChecked(not external)
        self.synced_check.setEnabled(external)
        self.synced_check.setText(
            "Both hosts are NTP-synchronised (enables a clock-corrected one-way figure)"
            if external
            else "Built-in Teacher and Student share this computer's clock"
        )

    def build_arguments(self) -> list[str]:
        args = [
            "-u",  # unbuffered, so progress lines reach the log as they happen
            str(PROJECT_ROOT / "remote_latency_benchmark.py"),
            "--server",
            self.server_edit.text().strip(),
            "--username",
            self.username_edit.text().strip(),
            "--student-username",
            self.student_username_edit.text().strip(),
            "--count",
            str(self.count_spin.value()),
            "--warmup",
            str(self.warmup_spin.value()),
            "--interval",
            str(self.interval_spin.value()),
            "--timeout",
            str(self.timeout_spin.value()),
        ]
        if self.external_check.isChecked():
            args.extend(["--external-student", "--room-id", self.room_edit.text().strip()])
        if self.trigger_check.isChecked():
            args.append("--trigger-cue")
        if self.synced_check.isChecked():
            args.append("--clocks-synced")
        if not self.verify_check.isChecked():
            args.append("--no-verify-tls")
        return args

    def start(self) -> None:
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.username_edit.text().strip():
            self._append("Enter the Teacher username first.\n")
            return
        if not self.external_check.isChecked() and not self.student_username_edit.text().strip():
            self._append("Enter the Student username first.\n")
            return
        if self.external_check.isChecked() and not self.room_edit.text().strip():
            self._append("External Student mode needs the room id that Student Client joined.\n")
            return

        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(self.build_arguments())
        self.process.setWorkingDirectory(str(PROJECT_ROOT))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._on_finished)
        self.process.start()

        if not self.process.waitForStarted(5000):
            self._append(f"could not start the benchmark: {self.process.errorString()}\n")
            self.process = None
            return

        # The CLI prompts on stdin when no password argument is given, so
        # neither secret appears in the argument list or a process listing.
        passwords = [self.password_edit.text()]
        if not self.external_check.isChecked():
            passwords.append(self.student_password_edit.text())
        self.process.write(("\n".join(passwords) + "\n").encode("utf-8"))
        self.password_edit.clear()
        self.student_password_edit.clear()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._append("$ " + " ".join(self.build_arguments()) + "\n")

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        if not self.process.waitForFinished(4000):
            self.process.kill()

    def _read_output(self) -> None:
        if self.process is None:
            return
        self._append(bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace"))

    def _on_finished(self, exit_code: int, _status) -> None:
        self._append(f"\nbenchmark process exited (code {exit_code})\n")
        self.process = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _append(self, text: str) -> None:
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)

    def closeEvent(self, event) -> None:
        if self.process is not None:
            self.stop()
        super().closeEvent(event)


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    return spin


def _double(minimum: float, maximum: float, value: float, suffix: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setDecimals(3)
    spin.setRange(minimum, maximum)
    spin.setSingleStep(0.1)
    spin.setValue(value)
    if suffix:
        spin.setSuffix(suffix)
    return spin


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = LatencyBenchmarkWindow()
    window.resize(820, 760)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
