"""Shared shell for the launcher's Tools section.

Both maintenance tools - review-video compression and participant ZIP
backup - are the same shape of job: find out what is there, show it,
ask, then work through a list of items by shelling out to an external
program that takes minutes. This module holds everything that shape
needs and neither tool should re-invent: the worker thread, the two
progress bars, the running log, and the check for whether the external
program (ffmpeg, 7z) can be found at all.

The split with app/review_compress.py and app/quiz_backup.py is strict.
Those modules decide what happens to files and never import Qt; the
windows here only start them, show what they report, and pass on a
Stop. That is what lets the console scripts do exactly what the buttons
do - there is one implementation, not two.

Threading: the work runs in a ToolJob (a QThread), which is what keeps
the window repainting during a multi-minute ffmpeg run. The job spends
almost all of its time waiting on a subprocess, so it is not fighting
the GUI thread for the GIL, and it talks back only through signals.

Stopping is cooperative: the flag is polled between items and while an
external program's output is being read, and a stop mid-run is handled
by the backends as a failed step - which for both tools means the
original data is put back or left alone. The one case with no graceful
answer is the process being killed outright (or the launcher quitting
mid-run), and both backends are built for that too: the half-finished
work is left under a tmp- name that no later run will touch or mistake
for a finished one.
"""

from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .quiz_style import STYLE_SHEET

# The status line for the external program, on top of the shared sheet:
# found is unremarkable, missing is the one thing on the window that has
# to be noticed, since nothing can run without it.
TOOL_STATUS_SHEET = """
QLabel#toolOk {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(46, 158, 91, 0.35);
    background: rgba(46, 158, 91, 0.10);
}
QLabel#toolMissing {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(208, 83, 83, 0.45);
    background: rgba(208, 83, 83, 0.12);
    font-weight: 600;
}
"""

# How many lines of running commentary to keep. A full conversion run
# logs a handful of lines per file; this is far more than one run
# produces and still bounds the window's memory.
LOG_LINE_LIMIT = 5000


class ToolJob(QThread):
    """One unit of maintenance work, run off the GUI thread.

    The work is passed in as a callable taking this job, so it can log,
    report progress and poll for cancellation without the backends
    knowing anything about Qt: the job IS the set of callbacks they
    already take.
    """

    logged = Signal(str)
    progress = Signal(str, int, int)   # stage label, done, total (0 total = unknown)
    overall = Signal(str, int, int)    # what is being worked through, done, total
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[["ToolJob"], object]):
        super().__init__()
        self._work = work
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    # Plain methods rather than the signals themselves, so a backend can
    # be handed job.log / job.report as ordinary callables.
    def log(self, message: str) -> None:
        self.logged.emit(message)

    def report(self, stage: str, done: int, total: int) -> None:
        self.progress.emit(stage, done, total)

    def report_overall(self, label: str, done: int, total: int) -> None:
        self.overall.emit(label, done, total)

    def run(self) -> None:
        try:
            result = self._work(self)
        except Exception as exc:  # noqa: BLE001 - the window shows whatever went wrong
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(result)


class MaintenanceWindow(QMainWindow):
    """Window shell: tool status, a table of what was found, two progress
    bars, a log, and Scan/Run/Stop.

    Subclasses supply the wording and the columns as class attributes,
    and implement start_scan() and start_run() in terms of _start().
    """

    TITLE = "Maintenance Tool"
    HEADER = ""
    NOTE = ""
    COLUMNS: Sequence[str] = ()
    SCAN_LABEL = "Scan"
    RUN_LABEL = "Run"

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(self.TITLE)
        self._job: Optional[ToolJob] = None

        header = QLabel(self.HEADER)
        header.setObjectName("header")
        header.setWordWrap(True)

        self.tool_status = QLabel("")
        self.tool_status.setWordWrap(True)
        self.recheck_btn = QPushButton("Re-check")
        self.recheck_btn.setToolTip(
            "Look for the program again - use this after installing it, or after "
            "dropping the executable into runtime/bin, without reopening this window."
        )
        self.recheck_btn.clicked.connect(self.refresh_tool_status)

        tool_row = QHBoxLayout()
        tool_row.addWidget(self.tool_status, 1)
        tool_row.addWidget(self.recheck_btn)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        self.scan_btn = QPushButton(self.SCAN_LABEL)
        self.scan_btn.clicked.connect(self.start_scan)
        self.run_btn = QPushButton(self.RUN_LABEL)
        self.run_btn.setObjectName("primaryBtn")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self.start_run)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)

        button_row = QHBoxLayout()
        button_row.addWidget(self.scan_btn)
        button_row.addWidget(self.run_btn)
        button_row.addWidget(self.stop_btn)
        button_row.addStretch(1)

        # Two bars, because one number cannot answer both questions a
        # person actually has: how far through the list are we, and is
        # the file on screen right now still moving.
        self.status = QLabel("Nothing scanned yet.")
        self.overall_bar = QProgressBar()
        self.overall_bar.setFormat("%v / %m")
        self.overall_bar.setRange(0, 1)
        self.overall_bar.setValue(0)
        # Only meaningful while something is running - an empty second
        # bar sitting under a finished run reads as a stalled one.
        self.current_bar = QProgressBar()
        self.current_bar.setRange(0, 1)
        self.current_bar.setValue(0)
        self.current_bar.setVisible(False)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINE_LIMIT)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText("The run log appears here.")

        note = QLabel(self.NOTE)
        note.setObjectName("note")
        note.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(header)
        layout.addLayout(tool_row)
        layout.addWidget(self.table, 3)
        layout.addLayout(button_row)
        layout.addWidget(self.status)
        layout.addWidget(self.overall_bar)
        layout.addWidget(self.current_bar)
        layout.addWidget(self.log_view, 2)
        if self.NOTE:
            layout.addWidget(note)
        central.setStyleSheet(STYLE_SHEET + TOOL_STATUS_SHEET)
        self.setCentralWidget(central)
        self.resize(900, 760)

        self.refresh_tool_status()

    # ------------------------------------------------------------------
    # External program
    # ------------------------------------------------------------------

    def find_tool(self) -> Optional[str]:
        raise NotImplementedError

    def tool_description(self, path: Optional[str]) -> str:
        raise NotImplementedError

    def refresh_tool_status(self) -> bool:
        """Show which copy of the external program will be used, or why
        there is none. Returns True if one was found."""
        path = self.find_tool()
        self.tool_status.setText(self.tool_description(path))
        self.tool_status.setObjectName("toolOk" if path else "toolMissing")
        # A changed object name only takes effect on the next polish.
        self.tool_status.style().unpolish(self.tool_status)
        self.tool_status.style().polish(self.tool_status)
        self.scan_btn.setEnabled(path is not None and self._job is None)
        if path is None:
            self.run_btn.setEnabled(False)
        return path is not None

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def start_scan(self) -> None:
        raise NotImplementedError

    def start_run(self) -> None:
        raise NotImplementedError

    def _start(
        self,
        work: Callable[[ToolJob], object],
        on_success: Callable[[object], None],
    ) -> None:
        """Run `work` in a ToolJob, wiring its reports into this window.

        Refuses to start a second job while one is running: both tools
        write files, and two runs over the same directories would race
        each other for the same temporary names.
        """
        if self._job is not None:
            return
        job = ToolJob(work)
        self._job = job
        job.logged.connect(self.append_log)
        job.progress.connect(self._on_progress)
        job.overall.connect(self._on_overall)
        job.succeeded.connect(on_success)
        job.succeeded.connect(lambda _result: self._job_done())
        job.failed.connect(self._on_failed)
        self.scan_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.current_bar.setRange(0, 1)
        self.current_bar.setValue(0)
        self.current_bar.setFormat("")
        self.current_bar.setVisible(True)
        job.start()

    def stop(self) -> None:
        if self._job is None:
            return
        self._job.cancel()
        self.stop_btn.setEnabled(False)
        self.status.setText("Stopping - finishing or undoing the step in progress ...")

    def _job_done(self) -> None:
        self._job = None
        self.stop_btn.setEnabled(False)
        self.current_bar.setRange(0, 1)
        self.current_bar.setValue(0)
        self.current_bar.setFormat("")
        self.current_bar.setVisible(False)
        self.refresh_tool_status()

    def _on_failed(self, message: str) -> None:
        self.append_log(f"ERROR: {message}")
        self.status.setText(f"Stopped after an unexpected error: {message}")
        self._job_done()

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        if total <= 0:
            # Unknown length: a busy indicator plus the running count,
            # which is the only honest thing to show while ffmpeg decodes
            # a file whose frame count is what it is trying to find out.
            self.current_bar.setRange(0, 0)
            self.current_bar.setFormat(f"{stage} - {done} frames" if done else stage)
            return
        self.current_bar.setRange(0, total)
        self.current_bar.setValue(min(done, total))
        self.current_bar.setFormat(f"{stage} - %v / %m")

    def _on_overall(self, label: str, done: int, total: int) -> None:
        self.overall_bar.setRange(0, max(total, 1))
        self.overall_bar.setValue(min(done, total))
        self.status.setText(label)

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_rows(self, rows: Sequence[Sequence[str]]) -> None:
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, text in enumerate(row):
                item = self.table.item(row_index, column_index)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row_index, column_index, item)
                item.setText(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Ask a running job to stop and give it a moment to unwind.

        The wait matters: the backends undo a half-done step when their
        external program dies, and that undo is what puts an original
        review.mp4 back. Cutting the thread off mid-rollback is the one
        way to leave a directory in the tmp- state on purpose.
        """
        job = self._job
        if job is not None and job.isRunning():
            job.cancel()
            job.wait(10000)
        super().closeEvent(event)
