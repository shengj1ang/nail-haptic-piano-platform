"""A small GUI around the self-contained latency benchmark CLI.

The run itself happens in a *separate* process driven by QProcess, not on
a thread in this window. A benchmark is a long, precisely paced loop -
1000 probes at 500 ms is over eight minutes - and its timing must not
share a process with a Qt event loop that could stall it. Running it out
of process also means Stop genuinely stops it.

The two password boxes feed the child's stdin once and are never stored,
never put on the command line (where they would be visible in a process
list) and never written to config.json.

Nothing here asks the operator to pick a room. Every run creates its own
temporary room and joins both accounts to it, so the Room row is a live
report of what the child process did - the room id, its join code and the
membership the relay confirmed - rather than a field to fill in. The one
manual case left is an external Student Client that has already joined a
room of its own, which is why that box only appears in external mode.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess, Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import Config

from ..config import RemoteGuidanceConfig
from ..gui_common import DEMO_ACCOUNTS, STATUS_STYLES
from .latency_benchmark import (
    BenchmarkConfig,
    DEFAULT_COUNT,
    DEFAULT_INTERVAL_JITTER,
    DEFAULT_INTERVAL_MODE,
    DEFAULT_INTERVAL_S,
    DEFAULT_STUDENT_WAIT_S,
    DEFAULT_TIMEOUT_S,
    DEFAULT_WARMUP,
    INTERVAL_MODES,
    RESULTS_DIR,
    ROOM_LINE,
    analyse_summary,
    cached_human_pacing,
    clear_pacing_cache,
    describe_pacing,
    expected_interval_s,
    format_summary,
    measure_human_pacing,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class _PacingAnalysisWorker(QThread):
    """Fits the human pacing model off the GUI thread.

    The analysis reads every participant's every trial. It takes well
    under a second today, but the window must not be the thing that
    decides that: a study twice this size, or a slow disk, would freeze
    the UI mid-click. Off the thread it cannot, whatever the corpus
    grows to."""

    progressed = Signal(int, int, str)
    fitted = Signal(object)  # HumanPacing, or None if cancelled/failed

    def __init__(self, force: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._force = force
        self._cancelled = False

    def cancel(self) -> None:
        """Asks the analysis to stop at its next progress point. A
        QThread cannot be safely killed from outside, so cancellation is
        cooperative."""
        self._cancelled = True

    def run(self) -> None:
        class _Cancelled(Exception):
            pass

        def report(done: int, total: int, message: str) -> None:
            if self._cancelled:
                raise _Cancelled
            self.progressed.emit(done, total, message)

        try:
            self.fitted.emit(measure_human_pacing(progress=report, force=self._force))
        except _Cancelled:
            self.fitted.emit(None)
        except Exception as exc:  # noqa: BLE001 - report, never take the window down
            self.progressed.emit(0, 0, f"could not fit the pacing model: {exc}")
            self.fitted.emit(None)


class BenchmarkResultsPage(QWidget):
    """Earlier runs, read back out of RESULTS_DIR.

    A finished run leaves three files in a folder - samples.csv,
    summary.json and latency.png - and until this page existed the only
    way to look at one again was to go and open them by hand, which meant
    the comparison that actually matters (this run against the last one)
    never got made. Nothing here recomputes anything: the numbers are the
    ones the run itself wrote.
    """

    def __init__(self, results_dir: Path = RESULTS_DIR, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.results_dir = Path(results_dir)
        self._plot = QPixmap()
        self._figure_dir: Optional[Path] = None

        self.run_list = QListWidget()
        self.run_list.currentTextChanged.connect(self.show_run)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.rescan)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Runs (newest first):"))
        left_layout.addWidget(self.run_list, 1)
        left_layout.addWidget(self.refresh_btn)

        self.summary_text = QPlainTextEdit()
        self.summary_text.setReadOnly(True)
        # A run writes several figures now; one selector beats stacking
        # them all in a scroll area nobody reaches the bottom of.
        self.figure_combo = QComboBox()
        self.figure_combo.currentTextChanged.connect(self._show_figure)
        self.plot_label = QLabel()
        self.plot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plot_scroll = QScrollArea()
        self.plot_scroll.setWidget(self.plot_label)
        self.plot_scroll.setWidgetResizable(True)

        figures = QWidget()
        figures_layout = QVBoxLayout(figures)
        figures_layout.setContentsMargins(0, 0, 0, 0)
        figure_row = QHBoxLayout()
        figure_row.addWidget(QLabel("Figure:"))
        figure_row.addWidget(self.figure_combo, 1)
        figures_layout.addLayout(figure_row)
        figures_layout.addWidget(self.plot_scroll, 1)

        detail = QSplitter(Qt.Orientation.Vertical)
        detail.addWidget(self.summary_text)
        detail.addWidget(figures)
        detail.setSizes([420, 380])

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(detail)
        split.setSizes([200, 660])

        self.path_label = QLabel(str(self.results_dir))
        self.path_label.setStyleSheet(STATUS_STYLES["idle"])
        self.path_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addWidget(self.path_label)
        self.reload()

    # ------------------------------------------------------------------

    def run_ids(self) -> list[str]:
        """Newest first - run ids are timestamps, so that is just reverse
        alphabetical, and a folder without a summary is a run that was
        interrupted before it wrote one."""
        if not self.results_dir.is_dir():
            return []
        return sorted(
            (d.name for d in self.results_dir.iterdir() if d.is_dir() and (d / "summary.json").is_file()),
            reverse=True,
        )

    def rescan(self) -> None:
        """Refresh also drops the human-pacing fit, so a participant
        recorded while this window was open is picked up without a
        restart. (The fit is keyed on a fingerprint of the quiz folder,
        so it would notice by itself too - this just makes Refresh mean
        one thing.)"""
        clear_pacing_cache()
        self.reload()

    def show_newest(self) -> None:
        runs = self.run_ids()
        self.reload(select=runs[0] if runs else None)

    def reload(self, select: Optional[str] = None) -> None:
        """Keeps whatever run is open unless asked for a specific one, so
        a Refresh mid-read does not move the page out from under it."""
        current = select
        if current is None and self.run_list.currentItem() is not None:
            current = self.run_list.currentItem().text()
        runs = self.run_ids()
        self.run_list.blockSignals(True)
        self.run_list.clear()
        self.run_list.addItems(runs)
        self.run_list.blockSignals(False)
        if not runs:
            self.summary_text.setPlainText(
                f"No finished runs in {self.results_dir} yet.\n\n"
                "Run a benchmark on the first page; its summary, analysis and plot appear here."
            )
            self.plot_label.setPixmap(QPixmap())
            self._plot = QPixmap()
            return
        wanted = current if current in runs else runs[0]
        self.run_list.setCurrentRow(runs.index(wanted))

    def show_run(self, run_id: str) -> None:
        if not run_id:
            return
        directory = self.results_dir / run_id
        try:
            summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.summary_text.setPlainText(f"{directory}\n\ncould not read summary.json: {exc}")
            self._show_run_figures(directory)
            return

        analysis = "\n".join(f"  - {line}" for line in analyse_summary(summary))
        self.summary_text.setPlainText(
            f"{run_id}\n{'=' * len(run_id)}\n\nAnalysis\n{analysis}\n{format_summary(summary)}\n\n{directory}"
        )
        self._show_run_figures(directory)

    def _show_run_figures(self, directory: Path) -> None:
        """The run's PNGs, overview first.

        A run whose matplotlib backend was missing has none; that is not
        an error - the CSV and JSON are the results."""
        self._figure_dir = directory
        names = sorted((path.name for path in directory.glob("*.png")),
                       key=lambda name: (name != "latency.png", name))
        self.figure_combo.blockSignals(True)
        self.figure_combo.clear()
        self.figure_combo.addItems(names)
        self.figure_combo.blockSignals(False)
        self.figure_combo.setEnabled(bool(names))
        if names:
            self.figure_combo.setCurrentIndex(0)
            self._show_figure(names[0])
        else:
            self._set_plot(QPixmap())

    def _show_figure(self, name: str) -> None:
        if not name or self._figure_dir is None:
            return
        path = self._figure_dir / name
        self._set_plot(QPixmap(str(path)) if path.is_file() else QPixmap())

    def _set_plot(self, pixmap: QPixmap) -> None:
        self._plot = pixmap
        self._rescale_plot()

    def _rescale_plot(self) -> None:
        """Scaled down to the pane, never up: an 1800 px wide plot blown
        past its own resolution would only look worse."""
        if self._plot.isNull():
            self.plot_label.setPixmap(QPixmap())
            self.plot_label.setText("(this run wrote no figures)")
            return
        self.plot_label.setText("")
        width = max(200, self.plot_scroll.viewport().width() - 4)
        self.plot_label.setPixmap(
            self._plot.scaledToWidth(min(width, self._plot.width()), Qt.TransformationMode.SmoothTransformation)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale_plot()


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
        # Deliberately empty rather than seeded from network.room_id: that
        # value is the last room a *normal* client used, and reusing a
        # stale room silently is worse than creating a fresh one.
        self.room_edit = QLineEdit()
        self.room_edit.setPlaceholderText("leave empty - a room is created for this run")
        self.room_status = QLabel()
        self.room_status.setWordWrap(True)

        self.count_spin = _spin(1, 100000, DEFAULT_COUNT)
        self.warmup_spin = _spin(0, 1000, DEFAULT_WARMUP)
        self.interval_spin = _double(0.01, 10.0, DEFAULT_INTERVAL_S, " s")
        self.timeout_spin = _double(0.1, 60.0, DEFAULT_TIMEOUT_S, " s")
        # Pacing, and the range it actually produces. A fixed period
        # samples the same phase of anything periodic in the path on
        # every probe; see latency_benchmark.INTERVAL_MODES.
        self.pacing_combo = QComboBox()
        for mode, label in (
            ("fixed", "Fixed period"),
            ("uniform", "Uniform jitter around the interval"),
            ("poisson", "Poisson - exponential waits (RFC 2330)"),
            ("human", "Human - measured participant reaction times"),
        ):
            self.pacing_combo.addItem(label, mode)
        self.pacing_combo.setCurrentIndex(INTERVAL_MODES.index(DEFAULT_INTERVAL_MODE))
        self.jitter_spin = _spin(0, 95, int(DEFAULT_INTERVAL_JITTER * 100))
        self.jitter_spin.setPrefix("+/- ")
        self.jitter_spin.setSuffix(" %")
        # Sharing a row with a stretching combo, it would otherwise be
        # squeezed down to a sliver of its own text.
        self.jitter_spin.setMinimumWidth(110)
        self.pacing_range = QLabel()
        self.pacing_combo.currentIndexChanged.connect(self._update_pacing)
        self.jitter_spin.valueChanged.connect(self._update_pacing)
        self.interval_spin.valueChanged.connect(self._update_pacing)

        pacing_row = QHBoxLayout()
        pacing_row.addWidget(self.pacing_combo, 1)
        pacing_row.addWidget(self.jitter_spin)
        self.trigger_check = QCheckBox("Ask the student to fire its real LED/haptic cue for each probe")
        self.synced_check = QCheckBox("Both hosts are NTP-synchronised (enables a clock-corrected one-way figure)")
        self.verify_check = QCheckBox("Verify TLS certificates")
        self.verify_check.setChecked(network.verify_tls)

        conn_box = QGroupBox("Connection")
        # The room line is a wrapping sentence, not a field: a
        # word-wrapped QLabel inside a QFormLayout row gets one line's
        # worth of height and clips the rest, so it goes under the form.
        conn_layout = QVBoxLayout(conn_box)
        conn_form = QFormLayout()
        conn_layout.addLayout(conn_form)
        conn_layout.addWidget(self.room_status)
        conn_form.addRow("Server:", self.server_edit)
        conn_form.addRow("Teacher username:", self.username_edit)
        conn_form.addRow("Teacher password:", self.password_edit)
        conn_form.addRow("Student username:", self.student_username_edit)
        conn_form.addRow("Student password:", self.student_password_edit)
        self.room_edit_label = QLabel("Existing room id:")
        conn_form.addRow(self.room_edit_label, self.room_edit)
        conn_form.addRow("", self.verify_check)

        run_box = QGroupBox("Run")
        run_form = QFormLayout(run_box)
        run_form.addRow("Measured probes:", self.count_spin)
        run_form.addRow("Warm-up probes:", self.warmup_spin)
        run_form.addRow("Interval (mean):", self.interval_spin)
        run_form.addRow("Pacing:", pacing_row)
        run_form.addRow("", self.pacing_range)
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
            "Default: only the Relay Server and this window are needed. Press Start and the benchmark logs in as "
            "both roles, creates a temporary room, joins the Teacher and the Student to it, opens both WebSockets, "
            "measures, then closes (but does not delete) that room. No room has to be picked or created by hand; "
            "the Room row above reports the one each run made. Both accounts must already exist on the relay. The "
            "built-in Student measures network/relay timing, not real Student UI or hardware.\n"
            "External Student mode is retained only for real UI / LED / haptic dispatch tests: the room is still "
            "created here and its join code shown, and probing waits up to "
            f"{DEFAULT_STUDENT_WAIT_S:.0f}s for Student Client to join it. Results are written "
            f"to {RESULTS_DIR} as samples.csv, summary.json and latency.png.\n"
            "Round-trip time is the primary metric - it needs no clock synchronisation. In external mode, an "
            "unsynchronised one-way value is shown as RTT/2 and labelled a symmetry-based estimate. Every cue "
            "time here is software dispatch/render timing, not physical LED or actuator onset."
        )
        note.setWordWrap(True)
        note.setStyleSheet(STATUS_STYLES["idle"])

        run_page = QWidget()
        layout = QVBoxLayout(run_page)
        layout.addWidget(conn_box)
        layout.addWidget(run_box)
        layout.addLayout(buttons)
        layout.addWidget(note)
        layout.addWidget(self.output, 1)

        # Two pages, because they answer different questions: this run,
        # and every run before it.
        self.results_page = BenchmarkResultsPage()
        self.tabs = QTabWidget()
        self.tabs.addTab(run_page, "Benchmark")
        self.tabs.addTab(self.results_page, "Past results")
        self.setCentralWidget(self.tabs)

        self._room_facts: dict[str, str] = {}
        self._pending_line = ""
        self._pacing_mode = DEFAULT_INTERVAL_MODE
        self._pacing_worker: Optional[_PacingAnalysisWorker] = None
        self._pacing_dialog: Optional[QProgressDialog] = None
        self._set_external_mode(False)
        self._update_pacing()
        self._update_estimate()

    # ------------------------------------------------------------------

    def _update_estimate(self) -> None:
        total = self.count_spin.value() + self.warmup_spin.value()
        minutes = total * expected_interval_s(self.pacing_config()) / 60.0
        # Randomised pacing keeps the mean, so the estimate holds - but
        # only on average, and the label should not pretend otherwise.
        mean = "" if self.pacing_combo.currentData() == "fixed" else " on average"
        self.estimate_label.setText(f"{total} probes ~ {minutes:.1f} min{mean}")

    def pacing_config(self) -> BenchmarkConfig:
        """The pacing half of what Start will pass to the child.

        Built here so the range on screen is rendered by the benchmark's
        own describe_pacing() from the same numbers the run will use,
        rather than by a second copy of the arithmetic."""
        return BenchmarkConfig(
            interval_s=self.interval_spin.value(),
            interval_mode=self.pacing_combo.currentData(),
            interval_jitter=self.jitter_spin.value() / 100.0,
        )

    def _mode_default_interval(self, mode: str) -> float:
        """Human pacing defaults to the participant median as currently
        fitted; the other modes keep the platform's own interval.

        Only ever asked once the fit is in hand, so it cannot be what
        blocks the GUI thread."""
        if mode != "human":
            return DEFAULT_INTERVAL_S
        fit = cached_human_pacing()
        return fit.median_s if fit is not None else DEFAULT_INTERVAL_S

    def _update_pacing(self) -> None:
        mode = self.pacing_combo.currentData()
        # The jitter width only means anything for uniform; poisson's
        # spread is set by its mean, and fixed has none.
        self.jitter_spin.setEnabled(mode == "uniform")

        if mode == "human" and cached_human_pacing() is None:
            # Selecting Human is what triggers the analysis, and until it
            # lands there is no range to show and no median to default
            # the interval to.
            self._pacing_mode = mode
            self.pacing_range.setText("Each wait: analysing every participant's reaction times...")
            self.pacing_range.setStyleSheet(STATUS_STYLES["warn"])
            self._start_pacing_analysis()
            return

        # Follow the mode's own default only while the box still holds
        # the outgoing mode's default - anything typed here is the
        # operator's and stays put.
        previous_default = self._mode_default_interval(self._pacing_mode)
        if self._pacing_mode != mode and abs(self.interval_spin.value() - previous_default) < 1e-9:
            self.interval_spin.setValue(self._mode_default_interval(mode))
        self._pacing_mode = mode
        self.pacing_range.setText(f"Each wait: {describe_pacing(self.pacing_config())}")
        self.pacing_range.setStyleSheet(STATUS_STYLES["idle"])
        self._update_estimate()

    def _start_pacing_analysis(self, force: bool = False) -> None:
        """Fit the human model behind a progress dialog.

        Cancelling returns the mode to Fixed rather than leaving Human
        selected with nothing behind it."""
        if self._pacing_worker is not None and self._pacing_worker.isRunning():
            return
        dialog = QProgressDialog("Reading participant quiz data...", "Cancel", 0, 0, self)
        dialog.setWindowTitle("Fitting human pacing")
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        self._pacing_dialog = dialog

        worker = self._pacing_worker = _PacingAnalysisWorker(force=force, parent=self)
        worker.progressed.connect(self._on_pacing_progress)
        worker.fitted.connect(self._on_pacing_fitted)
        dialog.canceled.connect(worker.cancel)
        worker.start()
        dialog.show()

    def _on_pacing_progress(self, done: int, total: int, message: str) -> None:
        if self._pacing_dialog is None:
            return
        self._pacing_dialog.setMaximum(total)
        self._pacing_dialog.setValue(done)
        self._pacing_dialog.setLabelText(message)

    def _on_pacing_fitted(self, fit) -> None:
        if self._pacing_dialog is not None:
            self._pacing_dialog.close()
            self._pacing_dialog = None
        self._pacing_worker = None
        if fit is None:
            # Cancelled or failed: Human has nothing to stand on.
            self.pacing_combo.setCurrentIndex(INTERVAL_MODES.index("fixed"))
            return
        self.interval_spin.setValue(fit.median_s)
        self._update_pacing()

    def _set_external_mode(self, external: bool) -> None:
        self.student_username_edit.setEnabled(not external)
        self.student_password_edit.setEnabled(not external)
        # Only an external Student Client can already be sitting in a room
        # of its own, so that box has nothing to do in built-in mode.
        self.room_edit.setEnabled(external)
        self.room_edit.setVisible(external)
        self.room_edit_label.setVisible(external)
        if not external:
            self.room_edit.clear()
        self._reset_room_status()
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

    # -- room status ----------------------------------------------------

    def _reset_room_status(self) -> None:
        """What will happen, until the child says what did happen."""
        self._room_facts = {}
        self._pending_line = ""
        if not self.external_check.isChecked():
            self._show_room_status(
                "created automatically for this run - the Teacher and Student accounts above are both "
                "joined to it, and it is closed again afterwards",
                "idle",
            )
        elif self.room_edit.text().strip():
            self._show_room_status("the existing room entered below, which Student Client must already be in", "idle")
        else:
            self._show_room_status(
                "created automatically for this run - the Teacher joins it and its join code is shown here "
                "for Student Client",
                "idle",
            )

    def _note_room_line(self, line: str) -> None:
        """Fold one `room:` line from the child into the Room row.

        Kept as a small set of named facts rather than the raw last line,
        so the row reads as one sentence about the room instead of
        flickering between unrelated fragments."""
        detail = line[len(ROOM_LINE):].strip()
        if detail.startswith("created "):
            self._room_facts["room"] = detail[len("created "):].strip()
        elif detail.startswith("using the existing room "):
            self._room_facts["room"] = detail[len("using the existing room "):].split()[0]
            self._room_facts["state"] = "joined by Student Client itself"
        elif detail.startswith("join code "):
            self._room_facts["code"] = detail[len("join code "):].strip()
        else:
            self._room_facts["state"] = detail
        state = self._room_facts.get("state", "")
        parts = []
        if self._room_facts.get("room"):
            parts.append(self._room_facts["room"])
        if self._room_facts.get("code"):
            parts.append(f"join code {self._room_facts['code']}")
        if state:
            parts.append(state)
        # A room that exists is good news unless the line itself is a
        # complaint - a failed close should not be reported in green.
        trouble = "warning" in state or "could not" in state
        level = "warn" if trouble or not self._room_facts.get("room") else "ok"
        self._show_room_status(" - ".join(parts), level)

    def _show_room_status(self, text: str, level: str) -> None:
        # Carries its own label, since it sits outside the form's two
        # columns rather than in a labelled row.
        self.room_status.setText(f"Room: {text}")
        self.room_status.setStyleSheet(STATUS_STYLES[level])

    # ------------------------------------------------------------------

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
            "--interval-mode",
            self.pacing_combo.currentData(),
            "--timeout",
            str(self.timeout_spin.value()),
        ]
        if self.pacing_combo.currentData() == "uniform":
            args.extend(["--interval-jitter", str(self.jitter_spin.value() / 100.0)])
        if self.external_check.isChecked():
            args.append("--external-student")
            # No room id means "make one", which is the normal case now;
            # passing an empty --room-id would look like a mistake instead.
            existing_room = self.room_edit.text().strip()
            if existing_room:
                args.extend(["--room-id", existing_room])
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

        self._reset_room_status()
        if not self.room_edit.text().strip():
            self._show_room_status("creating a room for this run...", "warn")
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
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._append(text)
        # Room lines can be split across two reads, so complete lines are
        # parsed and the remainder waits for the rest of itself.
        self._pending_line += text
        *lines, self._pending_line = self._pending_line.split("\n")
        for line in lines:
            if line.startswith(ROOM_LINE):
                self._note_room_line(line)

    def _on_finished(self, exit_code: int, _status) -> None:
        self._append(f"\nbenchmark process exited (code {exit_code})\n")
        if exit_code == 0:
            # A run that just took several minutes should not have to be
            # gone looking for; show what it produced.
            self.results_page.show_newest()
            self.tabs.setCurrentWidget(self.results_page)
        if exit_code != 0 and not self._room_facts.get("room"):
            # Otherwise the row would still promise a room that the run
            # never got as far as creating.
            self._show_room_status("no room was created - see the log below", "error")
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
        if self._pacing_worker is not None and self._pacing_worker.isRunning():
            self._pacing_worker.cancel()
            self._pacing_worker.wait(2000)
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
    window.resize(880, 860)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
