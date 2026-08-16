"""Reusable "run finger-matching over saved quizzes" window.

Deliberately independent of how a quiz's cues were delivered - it only
ever looks at a quiz's recorded video/MIDI (data/quiz/<name>/raw/) plus
the target-vs-actual note/timing results already saved by the quiz
runner, never at the cue mechanism itself. That means the exact same
window works for the screen-guided quiz (student_quiz.py, guidance_type
"visual"), the vibration-motor one (student_quiz_haptic.py, "haptic")
and any future guidance_type without changes here.

Used two ways:
  - Automatically, right after a quiz finishes (student_quiz.py opens
    this with initial_quiz_name set, which checks just that quiz and
    starts the analysis right away).
  - Standalone (quiz_analysis.py, also reachable from the launcher's
    "Data Analysis" section): every saved quiz is listed with its
    guidance type and headline metrics; tick any subset (e.g. all of one
    participant via the filter box) and (re-)analyze them in one batch.

Analysis is one pass: MediaPipe finger-matching over the raw recording,
saving results.json/meta.json, then the review video. The second half of
the manual-audit loop happens in the detail window instead - correcting a
finger or ruling on a carry-over there rewrites results.json and re-caches
meta.json on the spot (app.quiz.save_quiz_summary), so nothing here has to
be re-run afterwards. What does still have to be re-run after a round of
corrections is the participant export: the CSVs Group Analysis reads are
snapshots.
"""

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..finger_matching import FINGER_PROBABILITY_THRESHOLD, is_finger_correct
from ..quiz import (
    FINGER_LABELS,
    META_FILENAME,
    RAW_HANDS_FILENAME,
    RAW_NOTES_FILENAME,
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    REVIEW_VIDEO_FILENAME,
    SENSITIVITY_THRESHOLDS,
    QuizMeta,
    apply_summary,
    full_summary,
    list_quizzes,
    load_quiz_results,
    quiz_dir,
    quiz_raw_dir,
    save_quiz_results,
)
from ..music_recording import SyncInfo
from ..participant_export import export_paths, export_participant
from ..pilot_study import list_participants
from ..sync_led import load_sync_alignment, resolve_sync_anchor
from .analyze_worker import AnalyzeWorker, ReviewVideoWorker
from .missing_video import MISSING_VIDEO_TITLE, missing_video_message, require_video
from .quiz_detail_window import QuizDetailWindow
from .quiz_style import STYLE_SHEET
from .video_sync_window import VideoSyncWindow

COL_QUIZ = 0
# First entry of the export picker - re-exports every participant, which
# is what you want after a round of finger corrections, since Group
# Analysis reads the CSVs rather than results.json.
EXPORT_ALL_LABEL = "All participants (overwrite)"

COL_SYNC = 1  # not aligned / auto-aligned / manually aligned
COL_OFFSET = 2  # aligned flash frame vs software timestamps, in seconds
COL_SYNC_BTN = 3  # per-row Video Sync button
COL_STATUS = 4
METRIC_COL0 = 5  # first metric column


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def _ms(x) -> str:
    return f"{x * 1000:.0f} ms" if x is not None else "n/a"


def _build_metric_columns():
    """The per-trial metric columns, following the report's outcome-measure
    plan (final_report_2026/method/method.tex): (title, tooltip,
    needs_analysis, getter(meta, summary) -> str). needs_analysis columns
    show an em dash until the finger-matching video pass has run - their
    values would be meaningless before it."""
    cols = [
        ("Guidance", "How the cue was delivered (visual / haptic / key-only).", False,
         lambda m, s: m.guidance_type),
        ("Song", "The recorded song or generated sequence this quiz played.", False,
         lambda m, s: m.song_name),
        ("Notes", "Number of target events (T).", False,
         lambda m, s: str(m.note_count)),
        ("Key Accuracy", "sum(K) / T - correct-key events over all events.", False,
         lambda m, s: f"{_pct(s['note_accuracy'])} ({s['hits']}/{m.note_count})"),
        ("FA main", "sum(K and F) / T - key AND finger correct in the same event, over all events. "
                    "The report's primary measure; unresolved fingers count as incorrect.", True,
         lambda m, s: _pct(s["fa_main"])),
        ("FA | key ok", "sum(K and F) / sum(K) - finger correct, among correct-key events.", True,
         lambda m, s: _pct(s["fa_key"])),
        ("Note Accuracy | finger ok", "sum(K and F) / sum(F) - key correct, among correct-finger events.", True,
         lambda m, s: _pct(s["fa_finger"])),
        ("Timing Error", "Mean cue-to-keypress interval over every responded event.", False,
         lambda m, s: _ms(s["mean_timing_error_s"])),
        ("RT (key ok)", "Mean reaction time over correct-key events only.", False,
         lambda m, s: _ms(s["rt_correct_key_s"])),
        ("RT (complete)", "Mean reaction time over complete (key AND finger correct) events only.", True,
         lambda m, s: _ms(s["rt_complete_s"])),
        ("Timeouts", "Events with no keypress inside the response window.", False,
         lambda m, s: str(s["misses"])),
        ("Wrong key", "Responded events whose key didn't match the target.", False,
         lambda m, s: str(s["wrong_key"])),
        ("Carry-over", "Suspected carry-over responses (matched RT < 100 ms, need manual review in the "
                       "detail window) / manually confirmed ones (excluded from all stats).", False,
         lambda m, s: f"{s['suspected_carryover']}? / {s['excluded_carryover']} excl"
         if (s["suspected_carryover"] or s["excluded_carryover"]) else ""),
        ("QC extra presses", "QC only - note_on presses in the raw MIDI log never matched to a cue event: "
                             "near-simultaneous double-hits plus inter-trial strays (the tail of the "
                             "previous response). NOT false starts or anticipation; kept out of the main "
                             "outcome measures. n/a if midi_raw.json is missing.", True,
         lambda m, s: str(s["extra"]["extra_presses"]) if s.get("extra") else "n/a"),
        ("To review", "Events whose finger verdict failed and nobody has ruled on yet, with the "
                      "target finger above the review floor - the videos still to watch in the "
                      "detail window. Below the floor the automatic verdict stands.", True,
         lambda m, s: str(s["to_review"]) if s["to_review"] else ""),
        ("Key ok, wrong finger", "Correct-key events that failed the finger threshold rule.", True,
         lambda m, s: str(s["key_ok_wrong_finger"])),
        ("Unresolved", "Responded events where no fingertip could be detected at the keypress.", True,
         lambda m, s: _pct(s["unresolved_rate"])),
        ("Ambiguous", "Resolved events where no single fingertip held a majority (>= 0.5) of the "
                      "softmax probability mass.", True,
         lambda m, s: _pct(s["ambiguous_rate"])),
        ("FA same-hand", "FA main over events whose target hand matches the previous event's.", True,
         lambda m, s: _pct(s["same_hand"]["fa_main"])),
        ("FA hand-switch", "FA main over events whose target hand differs from the previous event's.", True,
         lambda m, s: _pct(s["hand_switch"]["fa_main"])),
        ("TE same-hand", "Mean timing error over same-hand transition events.", False,
         lambda m, s: _ms(s["same_hand"]["mean_timing_error_s"])),
        ("TE hand-switch", "Mean timing error over hand-switch transition events.", False,
         lambda m, s: _ms(s["hand_switch"]["mean_timing_error_s"])),
    ]
    for f in FINGER_LABELS:
        cols.append((f"FA {f}", f"FA main over events targeting finger {f}; n/a when the sequence "
                                "never targets it.", True,
                     lambda m, s, f=f: _pct(s["finger_stats"][f]["fa_main"])))
    for f in FINGER_LABELS:
        cols.append((f"TE {f}", f"Mean timing error over responded events targeting finger {f}.", False,
                     lambda m, s, f=f: _ms(s["finger_stats"][f]["mean_timing_error_s"])))
    for theta in SENSITIVITY_THRESHOLDS:
        key = f"{theta:.2f}"
        cols.append((f"FA θ={key}", f"FA main re-judged with finger threshold {key} instead of "
                                    f"{FINGER_PROBABILITY_THRESHOLD:.2f} (sensitivity analysis), from the "
                                    "stored probabilities - no video pass.", True,
                     lambda m, s, key=key: _pct(s["fa_theta"][key])))
    return cols


METRIC_COLUMNS = _build_metric_columns()
COLUMN_TITLES = ["Quiz", "Sync", "Video Offset", "", "Status"] + [
    title for title, _tip, _needs, _get in METRIC_COLUMNS
]


class QuizAnalysisWindow(QMainWindow):
    def __init__(self, cfg: Config, initial_quiz_name: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Quiz Analysis")
        self.resize(1050, 600)  # wide enough for the full metrics table
        self.cfg = cfg

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter quizzes (e.g. P02)...")
        self.filter_edit.textChanged.connect(self._apply_filter)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(lambda: self._refresh_quizzes())
        select_all_btn = QPushButton("Select shown")
        select_all_btn.clicked.connect(lambda: self._select_rows(lambda analyzed: True))
        select_unanalyzed_btn = QPushButton("Select not analyzed")
        select_unanalyzed_btn.clicked.connect(lambda: self._select_rows(lambda analyzed: not analyzed))
        select_analyzed_btn = QPushButton("Select analyzed")
        select_analyzed_btn.clicked.connect(lambda: self._select_rows(lambda analyzed: analyzed))
        select_none_btn = QPushButton("Select none")
        select_none_btn.clicked.connect(lambda: self._select_rows(None))

        self.table = QTableWidget(0, len(COLUMN_TITLES))
        self.table.setHorizontalHeaderLabels(COLUMN_TITLES)
        self.table.horizontalHeaderItem(COL_SYNC).setToolTip(
            "Video/MIDI alignment state: not aligned (falls back to software timestamps - unreliable) / auto-aligned (LED flash detection) / manually aligned (confirmed in the Video Sync window)."
        )
        self.table.horizontalHeaderItem(COL_OFFSET).setToolTip(
            "Aligned flash frame vs the software timestamps, in seconds. Unknown (not 0!) until aligned - the old analysis effectively assumed 0."
        )
        for i, (_title, tip, _needs, _get) in enumerate(METRIC_COLUMNS):
            self.table.horizontalHeaderItem(METRIC_COL0 + i).setToolTip(tip)
        self.table.verticalHeader().setVisible(False)
        # 40+ metric columns to scan sideways - banded rows keep the eye on
        # one quiz. (The detail window's table has no banding on purpose:
        # every cell there carries a verdict tint of its own.)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # The Video Sync column has no header text and no item text, so
        # ResizeToContents sized it to an empty string and clipped the
        # button living in it ("'ideo Syn"). Measure one styled button
        # instead - hard-coding a width would clip again under a different
        # UI font (Segoe UI on Windows, whatever the Linux desktop sets).
        probe = QPushButton("Video Sync")
        probe.setObjectName("cellBtn")
        probe.setStyleSheet(STYLE_SHEET)
        self.table.horizontalHeader().setSectionResizeMode(COL_SYNC_BTN, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_SYNC_BTN, probe.sizeHint().width() + 12)
        # There are far more metric columns than fit on screen - scroll
        # sideways (per pixel, not per column) to reach the rest.
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._open_detail)

        self.align_btn = QPushButton("Auto-align selected")
        self.align_btn.setObjectName("syncBtn")  # alignment family - teal
        self.align_btn.setToolTip(
            "Run LED flash detection on every checked quiz and save it as an auto alignment; quizzes already aligned (manual included) are skipped, untrusted detections are marked failed."
        )
        self.align_btn.setEnabled(False)
        self.align_btn.clicked.connect(self._auto_align_selected)
        self.analyze_btn = QPushButton("Analyze selected (from video)")
        self.analyze_btn.setObjectName("primaryBtn")  # the window's main action
        self.analyze_btn.setToolTip(
            "Full pipeline: MediaPipe finger-matching over the raw video, then the review video."
        )
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.clicked.connect(self._start_batch)
        self.cancel_btn = QPushButton("Cancel remaining")
        self.cancel_btn.setObjectName("stopBtn")  # interrupts a run in progress
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_batch)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_label = QLabel(
            f"Finger Accuracy counts a note as correct when the target finger's probability is "
            f"≥ {FINGER_PROBABILITY_THRESHOLD:.2f}."
        )
        # Deliberately not the muted "note" style: this label doubles as
        # the batch status line ("[3/12] analyzing ...", failures, export
        # results), which has to stay as readable as the table.
        self.status_label.setWordWrap(True)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(self.filter_edit, 1)
        top_row.addWidget(refresh_btn)

        select_label = QLabel("Select:")
        select_label.setObjectName("note")
        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        select_row.addWidget(select_label)
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_unanalyzed_btn)
        select_row.addWidget(select_analyzed_btn)
        select_row.addWidget(select_none_btn)
        select_row.addStretch(1)

        export_btn = QPushButton("Export participant data...")
        export_btn.setObjectName("exportBtn")  # stored-data family, outline
        export_btn.setToolTip(
            "Pick a Main User Study participant - or all of them at once - and write "
            "<participant>_trials.csv and <participant>_events.csv (all metrics + per-event data, "
            "no wall-clock timestamps) next to their TrialStructure.json. Group Analysis reads "
            "these files, so re-export after correcting fingers."
        )
        export_btn.clicked.connect(self._export_participant)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        bottom_row.addWidget(self.align_btn)
        bottom_row.addWidget(self.analyze_btn)
        bottom_row.addWidget(export_btn)
        bottom_row.addWidget(self.cancel_btn)
        bottom_row.addStretch(1)

        central = QWidget()
        central.setStyleSheet(STYLE_SHEET)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)
        layout.addLayout(top_row)
        layout.addLayout(select_row)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom_row)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        # Batch state: the names still to analyze, plus the quiz currently
        # going through the analyze -> review-video pipeline.
        self._queue: List[str] = []
        self._batch_total = 0
        self._batch_failed = 0
        self._current_name: Optional[str] = None
        self._current_meta: Optional[QuizMeta] = None
        self._current_results: list = []
        self._worker: Optional[AnalyzeWorker] = None
        self._review_worker: Optional[ReviewVideoWorker] = None
        self._detail_windows: list = []  # keep references so Qt doesn't GC them
        self._sync_windows: list = []

        self._refresh_quizzes(check_only=initial_quiz_name)
        if initial_quiz_name is not None and self._checked_names():
            self._start_batch()

    # ------------------------------------------------------------------
    # Table population / selection

    def _refresh_quizzes(self, check_only: Optional[str] = None) -> None:
        """Rebuild the table from disk. check_only pre-checks just that
        quiz (the post-quiz auto-analysis path); otherwise nothing is
        checked."""
        quizzes = list_quizzes()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setRowCount(len(quizzes))
        for row, name in enumerate(quizzes):
            quiz_item = QTableWidgetItem(name)
            quiz_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            quiz_item.setCheckState(Qt.CheckState.Checked if name == check_only else Qt.CheckState.Unchecked)
            self.table.setItem(row, COL_QUIZ, quiz_item)
            for col in range(1, len(COLUMN_TITLES)):
                item = QTableWidgetItem("")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, col, item)
            sync_btn = QPushButton("Video Sync")
            sync_btn.setObjectName("cellBtn")  # short padding - it sits in a table row
            sync_btn.clicked.connect(lambda _=False, n=name: self._open_sync_window(n))
            self.table.setCellWidget(row, COL_SYNC_BTN, sync_btn)
            try:
                meta = QuizMeta.load(quiz_dir(name) / META_FILENAME)
                results = load_quiz_results(quiz_dir(name) / RESULTS_FILENAME)
            except Exception as e:
                self.table.item(row, COL_STATUS).setText(f"couldn't load: {e}")
                continue
            self._fill_row(row, meta, full_summary(name, results))
        self.table.blockSignals(False)

        if not quizzes:
            self.status_label.setText("No quizzes found under data/quiz/. Run one with student_quiz.py first.")
        self._apply_filter(self.filter_edit.text())
        self._update_analyze_btn()

    def _fill_row(self, row: int, meta: QuizMeta, summary: dict) -> None:
        # Key accuracy and timing come straight from the quiz runner, so
        # they're shown even before the finger-matching pass has run; the
        # finger-based columns (needs_analysis) stay dashed until then.
        for i, (_title, _tip, needs_analysis, getter) in enumerate(METRIC_COLUMNS):
            self.table.item(row, METRIC_COL0 + i).setText(
                "—" if needs_analysis and not meta.analyzed else getter(meta, summary)
            )
        self.table.item(row, COL_STATUS).setText("analyzed" if meta.analyzed else "not analyzed")
        # Remembered per row so "Select (not) analyzed" doesn't have to
        # re-read every meta.json.
        self.table.item(row, COL_QUIZ).setData(Qt.ItemDataRole.UserRole, meta.analyzed)
        self._fill_sync_cols(row, meta.quiz_name)

    def _fill_sync_cols(self, row: int, name: str) -> None:
        """Sync status + offset columns, from raw/sync_align.json. Offset before any
        alignment is unknown ("?"), not zero - the software-timestamp
        fallback silently assumes 0, which is exactly what alignment fixes."""
        raw = quiz_raw_dir(name)
        btn = self.table.cellWidget(row, COL_SYNC_BTN)
        if not (raw / RAW_SYNC_FILENAME).exists():
            self.table.item(row, COL_SYNC).setText("no sync.json")
            self.table.item(row, COL_OFFSET).setText("—")
            if btn is not None:
                btn.setEnabled(False)
            return
        align = load_sync_alignment(raw)
        if align is None:
            self.table.item(row, COL_SYNC).setText("not aligned")
            self.table.item(row, COL_OFFSET).setText("?")
        else:
            self.table.item(row, COL_SYNC).setText("manually aligned" if align.method == "manual" else "auto-aligned")
            offset = align.led_vs_start_times_offset_s
            self.table.item(row, COL_OFFSET).setText(f"{offset:+.3f} s" if offset is not None else "?")

    def _row_of(self, name: str) -> Optional[int]:
        for row in range(self.table.rowCount()):
            if self.table.item(row, COL_QUIZ).text() == name:
                return row
        return None

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            name = self.table.item(row, COL_QUIZ).text().lower()
            self.table.setRowHidden(row, bool(needle) and needle not in name)

    def _row_analyzed(self, row: int) -> bool:
        return bool(self.table.item(row, COL_QUIZ).data(Qt.ItemDataRole.UserRole))

    def _select_rows(self, predicate) -> None:
        """Replace the current selection: check the visible rows whose
        analyzed-state passes predicate (so a 'P02' filter + Select not
        analyzed checks exactly that participant's unanalyzed quizzes),
        uncheck everything else, hidden rows included. predicate None =
        Select none."""
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            check = (
                predicate is not None
                and not self.table.isRowHidden(row)
                and predicate(self._row_analyzed(row))
            )
            self.table.item(row, COL_QUIZ).setCheckState(
                Qt.CheckState.Checked if check else Qt.CheckState.Unchecked
            )
        self.table.blockSignals(False)
        self._update_analyze_btn()

    def _checked_names(self) -> List[str]:
        return [
            self.table.item(row, COL_QUIZ).text()
            for row in range(self.table.rowCount())
            if self.table.item(row, COL_QUIZ).checkState() == Qt.CheckState.Checked
        ]

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == COL_QUIZ:
            self._update_analyze_btn()

    def _export_participant(self) -> None:
        """Re-export one participant, or all of them at once. The CSVs are
        what Group Analysis reads, so anything corrected in the detail
        window since the last export only reaches the group numbers once
        this has run again - hence the export-everything option."""
        participants = list_participants()
        if not participants:
            QMessageBox.information(self, "No participants", "No participants under data/MainUserStudy/.")
            return
        choice, ok = QInputDialog.getItem(
            self, "Export participant data", "Participant:", [EXPORT_ALL_LABEL] + participants, 0, False
        )
        if not ok or not choice:
            return
        targets = participants if choice == EXPORT_ALL_LABEL else [choice]

        existing = [p for t in targets for p in export_paths(t) if p.exists()]
        if existing:
            names = "\n".join(p.name for p in existing[:12])
            if len(existing) > 12:
                names += f"\n... and {len(existing) - 12} more"
            answer = QMessageBox.warning(
                self,
                "Files already exist",
                f"These export files already exist and will be overwritten:\n\n{names}\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        done: List[dict] = []
        failed: List[str] = []
        missing_notes: List[str] = []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for i, participant in enumerate(targets, 1):
                self.status_label.setText(f"Exporting {participant} ({i}/{len(targets)})...")
                QApplication.processEvents()
                try:
                    result = export_participant(participant)
                except Exception as e:
                    failed.append(f"{participant}: {e}")
                    continue
                done.append(result)
                if result["missing"]:
                    missing_notes.append(
                        f"{participant}: {len(result['missing'])} trials had no quiz data ("
                        + ", ".join(result["missing"]) + ")"
                    )
        finally:
            QApplication.restoreOverrideCursor()

        trials = sum(r["trials"] for r in done)
        events = sum(r["events"] for r in done)
        if len(targets) == 1 and done:
            message = (
                f"Exported {targets[0]}: {trials} trials, {events} events -> "
                + ", ".join(p.name for p in done[0]["paths"])
            )
        else:
            message = f"Exported {len(done)}/{len(targets)} participants: {trials} trials, {events} events"
        if missing_notes:
            message += "  |  " + "  |  ".join(missing_notes)
        if failed:
            message += f"  |  {len(failed)} failed"
            QMessageBox.warning(self, "Export failed", "\n".join(failed))
        self.status_label.setText(message)

    def _open_sync_window(self, name: str) -> None:
        if not require_video(self, quiz_raw_dir(name) / RAW_VIDEO_FILENAME):
            return
        try:
            meta = QuizMeta.load(quiz_dir(name) / META_FILENAME)
            window = VideoSyncWindow(name, meta.keyboard_profile_name)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't open Video Sync", f"{name}: {e}")
            return
        window.saved.connect(self._on_sync_saved)
        self._sync_windows = [w for w in self._sync_windows if w.isVisible()]
        self._sync_windows.append(window)
        window.show()

    def _on_sync_saved(self, name: str) -> None:
        row = self._row_of(name)
        if row is not None:
            self._fill_sync_cols(row, name)

    def _auto_align_selected(self) -> None:
        """LED flash detection + auto-alignment save, over every checked quiz. Existing
        alignments (manual included) are left untouched; untrusted
        detections are reported per row and not persisted."""
        names = self._checked_names()
        if not names:
            return
        answer = QMessageBox.warning(
            self,
            "Auto-align selected quizzes?",
            f"This will run LED flash detection on the {len(names)} checked quiz(zes) and save each "
            "trusted detection as that quiz's alignment (sync_align.json). Every later video analysis "
            "will use the saved alignment.\n\n"
            "Quizzes already aligned (manual alignments included) are skipped, so nothing existing is "
            "overwritten. Detection reads each raw video and can take a while.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        aligned = skipped = failed = 0
        missing_video = []
        for name in names:
            row = self._row_of(name)
            raw = quiz_raw_dir(name)
            if not (raw / RAW_VIDEO_FILENAME).exists():
                # No raw video in this checkout - LED detection can't run.
                failed += 1
                missing_video.append(name)
                if row is not None:
                    self.table.item(row, COL_SYNC).setText(
                        "no raw video (contact ICL for full data)")
                continue
            if not (raw / RAW_SYNC_FILENAME).exists():
                failed += 1
                continue
            if load_sync_alignment(raw) is not None:
                skipped += 1
                continue
            try:
                meta = QuizMeta.load(quiz_dir(name) / META_FILENAME)
                sync = SyncInfo.load(raw / RAW_SYNC_FILENAME)
                anchor = resolve_sync_anchor(
                    raw / RAW_VIDEO_FILENAME, sync, meta.keyboard_profile_name
                )
            except Exception as e:
                anchor = None
                reason = str(e)
            if anchor is not None and anchor.method in ("led", "auto", "manual"):
                aligned += 1
            else:
                failed += 1
                reason = anchor.reason if anchor is not None else reason
                if row is not None:
                    self.table.item(row, COL_SYNC).setText("not aligned (detection failed)")
                    self.table.item(row, COL_SYNC).setToolTip(reason)
                    self.table.item(row, COL_OFFSET).setText("?")
                continue
            if row is not None:
                self._fill_sync_cols(row, name)
            QApplication.processEvents()
        self.status_label.setText(
            f"Auto-align done: {aligned} aligned, {skipped} already aligned (skipped), {failed} failed (need manual alignment)."
        )
        if missing_video:
            QMessageBox.information(self, MISSING_VIDEO_TITLE,
                                   missing_video_message(len(missing_video)))

    def _open_detail(self, row: int, _col: int) -> None:
        name = self.table.item(row, COL_QUIZ).text()
        try:
            detail = QuizDetailWindow(name)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't open quiz", f"{name}: {e}")
            return
        detail.changed.connect(self._on_results_edited)
        self._detail_windows = [w for w in self._detail_windows if w.isVisible()]
        self._detail_windows.append(detail)
        detail.show()

    def _on_results_edited(self, name: str) -> None:
        """An event was corrected or ruled on in a detail window: refresh
        the row from the edited files. Both are already up to date on disk
        - the detail window re-caches meta.json as part of saving - so this
        is a plain reload, no recomputation to persist."""
        row = self._row_of(name)
        if row is None:
            return
        try:
            meta = QuizMeta.load(quiz_dir(name) / META_FILENAME)
            results = load_quiz_results(quiz_dir(name) / RESULTS_FILENAME)
        except Exception:
            return
        self._fill_row(row, meta, full_summary(name, results))

    def _update_analyze_btn(self) -> None:
        n = len(self._checked_names())
        idle = self._current_name is None
        self.analyze_btn.setText(f"Analyze selected (from video) ({n})" if n else "Analyze selected (from video)")
        self.analyze_btn.setEnabled(n > 0 and idle)
        self.align_btn.setText(f"Auto-align selected ({n})" if n else "Auto-align selected")
        self.align_btn.setEnabled(n > 0 and idle)

    # ------------------------------------------------------------------
    # Batch driver

    def _start_batch(self) -> None:
        names = self._checked_names()
        if not names:
            return
        answer = QMessageBox.warning(
            self,
            "Analyze selected from video?",
            f"This will run the full video pipeline on the {len(names)} checked quiz(zes): MediaPipe "
            "finger matching over the raw video, then a re-rendered review video.\n\n"
            "It OVERWRITES each quiz's detected-finger fields in results.json - any manual finger "
            "corrections made in the event review window are LOST and must be redone. (Carry-over "
            "validity verdicts are kept.) review.mp4 is replaced. This can take a long time.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # The video pipeline needs each quiz's raw performance.mp4, which is
        # not committed to the repository - warn once and skip those.
        missing = [n for n in names
                   if not (quiz_raw_dir(n) / RAW_VIDEO_FILENAME).exists()]
        if missing:
            for name in missing:
                row = self._row_of(name)
                if row is not None:
                    self.table.item(row, COL_STATUS).setText(
                        "no raw video (contact ICL for full data)")
            QMessageBox.information(self, MISSING_VIDEO_TITLE,
                                    missing_video_message(len(missing)))
            names = [n for n in names if n not in set(missing)]
        if not names:
            return

        self._queue = names
        self._batch_total = len(self._queue)
        self._batch_failed = 0
        self.analyze_btn.setEnabled(False)
        self.align_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.filter_edit.setEnabled(False)
        for name in self._queue:
            row = self._row_of(name)
            if row is not None:
                self.table.item(row, COL_STATUS).setText("queued")
        self._run_next()

    def _cancel_batch(self) -> None:
        """Drop everything still queued; the quiz currently being analyzed
        finishes normally (its results are saved as usual)."""
        for name in self._queue:
            row = self._row_of(name)
            if row is not None:
                self.table.item(row, COL_STATUS).setText("cancelled")
        self._batch_total -= len(self._queue)
        self._queue = []
        self.cancel_btn.setEnabled(False)

    def _run_next(self) -> None:
        if not self._queue:
            self._end_batch()
            return
        name = self._queue.pop(0)
        self._current_name = name
        row = self._row_of(name)

        try:
            self._current_meta = QuizMeta.load(quiz_dir(name) / META_FILENAME)
            self._current_results = load_quiz_results(quiz_dir(name) / RESULTS_FILENAME)
        except Exception as e:
            self._fail_current(f"couldn't load: {e}")
            return

        pressed = [r for r in self._current_results if not r.timed_out]
        if not pressed:
            self._finalize([])
            return

        if row is not None:
            self.table.item(row, COL_STATUS).setText("analyzing...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self._set_status(f"{name}: matching fingers against the video...")

        self._worker = AnalyzeWorker(
            quiz_raw_dir(name) / RAW_VIDEO_FILENAME,
            quiz_raw_dir(name) / RAW_NOTES_FILENAME,
            self._current_meta.keyboard_profile_name,
            sync_path=quiz_raw_dir(name) / RAW_SYNC_FILENAME,
            hands_out_path=quiz_raw_dir(name) / RAW_HANDS_FILENAME,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._finalize)
        self._worker.failed.connect(self._fail_current)
        self._worker.start()

    def _batch_position(self) -> str:
        done = self._batch_total - len(self._queue)
        return f"{done}/{self._batch_total}"

    def _set_status(self, text: str) -> None:
        self.status_label.setText(f"[{self._batch_position()}] {text}")

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            self._set_status(f"{self._current_name}: matching fingers against the video... {done}/{total} frames")
        else:
            self._set_status(f"{self._current_name}: matching fingers against the video... {done} frames")

    def _fail_current(self, message: str) -> None:
        self._batch_failed += 1
        row = self._row_of(self._current_name) if self._current_name else None
        if row is not None:
            self.table.item(row, COL_STATUS).setText(f"failed: {message}")
        self._current_name = None
        self._run_next()

    def _finalize(self, matches: list) -> None:
        pressed = [r for r in self._current_results if not r.timed_out]
        for result, match in zip(pressed, matches):
            result.actual_finger = match.finger if match else None
            result.finger_probabilities = match.probabilities if match else None
            result.target_finger_probability = (
                match.probabilities.get(result.target_finger, 0.0) if (match and result.target_finger) else None
            )
            result.finger_correct = is_finger_correct(match, result.target_finger)
            result.actual_finger_point = list(match.point) if match else None

        name = self._current_name
        meta = self._current_meta
        save_quiz_results(self._current_results, quiz_dir(name) / RESULTS_FILENAME)

        summary = full_summary(name, self._current_results)
        apply_summary(meta, summary)
        meta.analyzed = True
        meta.save(quiz_dir(name) / META_FILENAME)

        row = self._row_of(name)
        if row is not None:
            self._fill_row(row, meta, summary)

        self._start_review_render()

    # ------------------------------------------------------------------
    # Review video (per quiz, right after its analysis)

    def _start_review_render(self) -> None:
        """Re-encode the raw video with the per-note verdicts drawn on top
        (see app.review_video), for manual auditing of the scoring."""
        name = self._current_name
        video_path = quiz_raw_dir(name) / RAW_VIDEO_FILENAME
        if not video_path.exists():
            self._finish_current("analyzed (no raw video for review)")
            return

        row = self._row_of(name)
        if row is not None:
            self.table.item(row, COL_STATUS).setText("rendering review video...")
        self.progress_bar.setRange(0, 0)
        self._set_status(f"{name}: rendering review video...")

        self._review_worker = ReviewVideoWorker(
            video_path,
            quiz_dir(name) / REVIEW_VIDEO_FILENAME,
            self._current_results,
            self._current_meta.keyboard_profile_name,
            sync_path=quiz_raw_dir(name) / RAW_SYNC_FILENAME,
            hands_path=quiz_raw_dir(name) / RAW_HANDS_FILENAME,
        )
        self._review_worker.progress.connect(self._on_review_progress)
        self._review_worker.succeeded.connect(lambda _out: self._finish_current("analyzed"))
        self._review_worker.failed.connect(lambda msg: self._finish_current(f"analyzed (review video failed: {msg})"))
        self._review_worker.start()

    def _on_review_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)

    def _finish_current(self, status: str) -> None:
        row = self._row_of(self._current_name) if self._current_name else None
        if row is not None:
            self.table.item(row, COL_STATUS).setText(status)
        self._current_name = None
        self._run_next()

    def _end_batch(self) -> None:
        self._current_name = None
        self.progress_bar.setVisible(False)
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.filter_edit.setEnabled(True)
        ok = self._batch_total - self._batch_failed
        failed = f", {self._batch_failed} failed" if self._batch_failed else ""
        self.status_label.setText(f"Done: {ok}/{self._batch_total} quizzes analyzed{failed}.")
        self._update_analyze_btn()
