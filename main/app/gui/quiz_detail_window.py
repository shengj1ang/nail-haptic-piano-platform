"""Per-trial detail view - double-click a quiz row in the analysis
window (app/gui/quiz_analysis_window.py) to open it.

Deep dive into a single quiz, built for the manual audit loop: the top
table lists all 30 events with their target/actual key and finger,
probability, and verdicts - borderline finger verdicts (within
BORDERLINE_MARGIN of the threshold) are highlighted so you know which
events to check.

Actual finger is the detector's argmax while Finger ✓ is the θ rule on
the *cued* finger's mass (app.finger_matching), so the two legitimately
disagree in both directions. Rather than leave a ✓ sitting next to a
different finger, the column names the disagreement: "R3 (≈R2)" for an
event scored as the cued R3 that a neighbour narrowly won on
probability, and "R4 (p<θ)" for the mirror case where the argmax is the
cued finger but never cleared θ. Both are display only - see
_finger_cell(); results.json keeps the argmax, because the confusion
matrix, the unresolved rate and the Condition A strategy measures are
all statements about what the detector saw, and every accuracy figure
reads finger_correct rather than re-deriving it from the two labels.

Double-clicking an event opens the per-event review
window (app/gui/event_review_window.py): playback around the keypress
and correction of the detected finger; hand-corrected events are flagged
in the Manual column. Suspected carry-over responses (matched RT below
CARRYOVER_RT_THRESHOLD_S) are flagged in the Validity column; right-click
the row to confirm one as invalid carry-over (excluded from all stats) or
to restore it. Below the table: the target-vs-detected finger confusion
matrix and the distribution/trend statistics that don't fit the main
table (timing spread, within-trial trend, wrong-key spatial profile,
detection confidence, QC press counts).
"""

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..finger_matching import FINGER_PROBABILITY_THRESHOLD
from ..quiz import (
    BORDERLINE_MARGIN,
    CARRYOVER_RT_THRESHOLD_S,
    FINGER_REVIEW_FLOOR,
    META_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    VALIDITY_INVALID_CARRYOVER,
    VALIDITY_VALID,
    QuizMeta,
    finger_manually_corrected,
    full_summary,
    load_quiz_results,
    needs_finger_review,
    note_name,
    quiz_dir,
    quiz_raw_dir,
    save_quiz_results,
    scored_near_tie,
    subthreshold_match,
    suspected_carryover,
)
from .event_review_window import EventReviewWindow
from .missing_video import require_video

# Row/cell tints for the event table (light, readable on white).
COLOR_TIMEOUT = QColor(230, 230, 230)
COLOR_WRONG_KEY = QColor(255, 220, 220)
COLOR_WRONG_FINGER = QColor(255, 238, 215)
COLOR_BORDERLINE = QColor(255, 250, 190)
COLOR_MANUAL = QColor(220, 235, 255)
COLOR_SUSPECTED = QColor(255, 210, 160)  # carry-over suspect - review me
COLOR_INVALID = QColor(205, 205, 205)  # manually excluded from all stats
# Confusion-matrix cells, coloured by the scored verdict rather than by
# where the cell sits: a neighbour can be the argmax while the target still
# clears the threshold (near-tie), which is not an error.
COLOR_TO_REVIEW = QColor(255, 225, 150)  # in the review queue - watch this one
COLOR_REVIEWED = QColor(225, 235, 225)  # a human has already ruled on it
COLOR_CORRECT = QColor(215, 240, 215)
COLOR_NEAR_TIE = QColor(255, 240, 200)
COLOR_WRONG_FINGER_CELL = QColor(255, 220, 220)
COLOR_UNRESOLVED = QColor(230, 230, 230)

EVENT_COLUMNS = [
    "#",
    "Target key",
    "Actual key",
    "Key ✓",
    "Target finger",
    "Actual finger",
    "p(target)",
    "Finger ✓",
    "RT",
    "Manual",
    "Review",
    "Validity",
]
COL_ACTUAL_FINGER = 5
COL_FINGER_OK = 7
COL_REVIEW = 10

ACTUAL_FINGER_HEADER_TIP = (
    "The fingertip the detector reported (the softmax argmax). The verdict in Finger ✓ is a "
    f"different question - whether the *cued* finger held at least θ = {FINGER_PROBABILITY_THRESHOLD:.2f} "
    "of the mass - so the two can disagree in either direction. Where they do, this cell spells "
    "out which is which:\n\n"
    "  R3 (≈R2)  - scored as the cued R3; R2 was fractionally more probable and is what the\n"
    "              results file stores in actual_finger.\n"
    "  R4 (p<θ)  - R4 was the most probable fingertip and is the cued one, but it never cleared\n"
    "              θ, so the automatic rule cannot credit the event.\n\n"
    "Display only - results.json is untouched, and every statistic still reads finger_correct."
)


def _ms(x) -> str:
    return f"{x * 1000:.0f} ms" if x is not None else "n/a"


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def _finger_cell(r):
    """(text, tooltip, tint) for the Actual finger column.

    actual_finger is the softmax argmax; finger_correct is the θ rule on
    the *target* finger's mass (see app.finger_matching). Printing the raw
    argmax beside a ✓ reads as a contradiction it isn't, so the two
    disagreeing cases are named in the cell instead of being left for the
    reader to reconstruct from the p(target) column. Nothing here writes
    to results.json - the stored value stays the argmax, which is what the
    confusion matrix, the unresolved rate and the Condition A strategy
    measures need it to be."""
    if r.actual_finger is None:
        return "unresolved", None, None

    probs = r.finger_probabilities or {}
    p_target = r.target_finger_probability
    p_actual = probs.get(r.actual_finger)
    p_t = f"{p_target:.2f}" if p_target is not None else "n/a"
    p_a = f"{p_actual:.2f}" if p_actual is not None else "n/a"

    if scored_near_tie(r):
        return (
            f"{r.target_finger} (≈{r.actual_finger})",
            f"Scored as the cued {r.target_finger}: it held p = {p_t} ≥ θ = "
            f"{FINGER_PROBABILITY_THRESHOLD:.2f}, so the benefit of the doubt goes to the "
            f"learner. {r.actual_finger} was fractionally more probable (p = {p_a}) and is what "
            "results.json stores as actual_finger - the camera cannot separate two fingertips "
            f"this close. The confusion matrix below therefore still counts this event as "
            f"{r.target_finger} → {r.actual_finger} (amber), and every statistic reads the ✓.",
            COLOR_NEAR_TIE,
        )
    if subthreshold_match(r):
        return (
            f"{r.actual_finger} (p<θ)",
            f"The most probable fingertip was {r.actual_finger}, which is the cued finger - but it "
            f"held only p = {p_t} < θ = {FINGER_PROBABILITY_THRESHOLD:.2f}, so the mass was split "
            "across several fingertips over the key and the automatic rule cannot credit the "
            "event. That is why Finger ✓ is ✗ next to a matching finger. Double-click the row to "
            "watch the keypress and rule on it.",
            None,
        )
    return r.actual_finger, None, None


class QuizDetailWindow(QMainWindow):
    changed = Signal(str)  # quiz name, emitted after an event correction is saved

    def __init__(self, quiz_name: str):
        super().__init__()
        self.quiz_name = quiz_name
        self.setWindowTitle(f"Quiz Detail - {quiz_name}")
        self.resize(1000, 750)
        self._event_windows: list = []  # keep references so Qt doesn't GC them
        self._build()

    def _build(self) -> None:
        """(Re)load everything from disk and rebuild the whole view -
        also called after a per-event correction is saved, so the table,
        confusion matrix and stats always reflect the current results.json."""
        self.meta = QuizMeta.load(quiz_dir(self.quiz_name) / META_FILENAME)
        results = load_quiz_results(quiz_dir(self.quiz_name) / RESULTS_FILENAME)
        summary = full_summary(self.quiz_name, results)

        header = QLabel(
            f"<b>{self.quiz_name}</b> — guidance {self.meta.guidance_type}, song {self.meta.song_name}, "
            f"{self.meta.note_count} events, {'analyzed' if self.meta.analyzed else 'not analyzed'}<br>"
            f"Key Accuracy {_pct(summary['note_accuracy'])} | FA main {_pct(summary['fa_main'])} | "
            f"FA|key {_pct(summary['fa_key'])} | NoteAcc|finger {_pct(summary['fa_finger'])} | "
            f"Timing {_ms(summary['mean_timing_error_s'])}<br>"
            f"<i>Double-click an event row to play back its keypress and correct the detected finger.</i>"
        )
        header.setWordWrap(True)

        events_table = self._build_events_table(results)
        events_table.cellDoubleClicked.connect(self._open_event_review)
        events_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        events_table.customContextMenuRequested.connect(self._show_event_menu)
        self._events_table = events_table

        to_review = summary["to_review"]
        queue_check = QCheckBox(f"Only events to review ({to_review})")
        queue_check.setToolTip(
            "Show just the events still waiting for a verdict: the finger rule failed and the "
            f"target finger held at least p = {FINGER_REVIEW_FLOOR:.2f}. Watching one takes it off "
            "the list, whether you change the finger or confirm it as is."
        )
        queue_check.setEnabled(bool(to_review))
        queue_check.toggled.connect(self._filter_to_review)
        # Keep the filter on across the rebuild that follows a correction,
        # so working through the queue doesn't reset the view every time.
        queue_check.setChecked(getattr(self, "_queue_only", False) and bool(to_review))
        self._queue_check = queue_check
        self._filter_to_review(queue_check.isChecked())

        bottom = QHBoxLayout()
        bottom.addWidget(self._build_confusion_box(summary), 1)
        bottom.addWidget(self._build_stats_box(summary), 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(header)
        layout.addWidget(queue_check)
        layout.addWidget(events_table, 3)
        layout.addLayout(bottom, 2)
        self.setCentralWidget(central)  # deletes the previous central widget

    # ------------------------------------------------------------------

    def _filter_to_review(self, only_queue: bool) -> None:
        self._queue_only = only_queue
        for row, queued in enumerate(self._row_in_queue):
            self._events_table.setRowHidden(row, only_queue and not queued)

    def _open_event_review(self, row: int, _col: int) -> None:
        if not require_video(self, quiz_raw_dir(self.quiz_name) / RAW_VIDEO_FILENAME):
            return
        try:
            window = EventReviewWindow(self.quiz_name, row, self.meta.keyboard_profile_name)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't open event review", f"event {row}: {e}")
            return
        window.saved.connect(self._on_event_corrected)
        self._event_windows = [w for w in self._event_windows if w.isVisible()]
        self._event_windows.append(window)
        window.show()

    def _on_event_corrected(self, _name: str) -> None:
        self._build()
        self.changed.emit(self.quiz_name)

    def _show_event_menu(self, pos) -> None:
        """Right-click an event row: the manual carry-over verdict. Only a
        human ever sets validity - the code merely flags suspects (RT <
        CARRYOVER_RT_THRESHOLD_S) for this review."""
        row = self._events_table.rowAt(pos.y())
        if row < 0:
            return
        results = load_quiz_results(quiz_dir(self.quiz_name) / RESULTS_FILENAME)
        if row >= len(results):
            return
        r = results[row]
        if r.timed_out:
            return  # no matched response - nothing to rule on
        menu = QMenu(self)
        if r.validity == VALIDITY_INVALID_CARRYOVER:
            action = menu.addAction("Restore event (mark valid again)")
            new_validity = VALIDITY_VALID
        else:
            action = menu.addAction("Confirm as invalid carry-over (exclude from RT && accuracy)")
            new_validity = VALIDITY_INVALID_CARRYOVER
        if menu.exec(self._events_table.viewport().mapToGlobal(pos)) is action:
            r.validity = new_validity
            save_quiz_results(results, quiz_dir(self.quiz_name) / RESULTS_FILENAME)
            self._build()
            self.changed.emit(self.quiz_name)

    # ------------------------------------------------------------------

    def _build_events_table(self, results) -> QTableWidget:
        table = QTableWidget(len(results), len(EVENT_COLUMNS))
        table.setHorizontalHeaderLabels(EVENT_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeaderItem(COL_ACTUAL_FINGER).setToolTip(ACTUAL_FINGER_HEADER_TIP)
        self._row_in_queue = [needs_finger_review(r) for r in results]
        responded = [r for r in results if not r.timed_out]
        self._near_ties = sum(1 for r in responded if scored_near_tie(r))
        self._subthreshold_matches = sum(1 for r in responded if subthreshold_match(r))

        for row, r in enumerate(results):
            p = r.target_finger_probability
            borderline = p is not None and abs(p - FINGER_PROBABILITY_THRESHOLD) <= BORDERLINE_MARGIN
            manual = not r.timed_out and finger_manually_corrected(r)
            invalid = r.validity == VALIDITY_INVALID_CARRYOVER
            suspected = suspected_carryover(r)
            to_review = needs_finger_review(r)
            review_text = "▶ watch" if to_review else ("✔ reviewed" if r.finger_reviewed else "")
            validity_text = "excluded" if invalid else ("review!" if suspected else "")
            finger_tip: Optional[str] = None
            finger_tint: Optional[QColor] = None
            if r.timed_out:
                cells = [str(r.index), r.target_note_name, "—", "—", r.target_finger or "—",
                         "—", "—", "—", "timeout", "", "", validity_text]
                tint: Optional[QColor] = COLOR_TIMEOUT
            else:
                finger_text, finger_tip, finger_tint = _finger_cell(r)
                cells = [
                    str(r.index),
                    r.target_note_name,
                    note_name(r.actual_note) if r.actual_note is not None else "?",
                    "✓" if r.note_correct else "✗",
                    r.target_finger or "—",
                    finger_text,
                    f"{p:.2f}" if p is not None else "n/a",
                    "—" if r.finger_correct is None else ("✓" if r.finger_correct else "✗"),
                    _ms(r.timing_error_s),
                    "✎ manual" if manual else "",
                    review_text,
                    validity_text,
                ]
                if invalid:
                    tint = COLOR_INVALID
                elif not r.note_correct:
                    tint = COLOR_WRONG_KEY
                elif r.finger_correct is False:
                    tint = COLOR_WRONG_FINGER
                else:
                    tint = None
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if tint is not None:
                    item.setBackground(tint)
                # Borderline p(target) overrides the row tint on its own
                # cell - it's the audit priority signal.
                if borderline and col == 6:
                    item.setBackground(COLOR_BORDERLINE)
                    item.setToolTip(
                        f"Within ±{BORDERLINE_MARGIN:.2f} of the θ={FINGER_PROBABILITY_THRESHOLD:.2f} "
                        "threshold - verdict could flip; check this event in the review video."
                    )
                # Detected finger vs verdict disagree - say which is which
                # rather than leaving a ✓ next to a different finger.
                if col == COL_ACTUAL_FINGER and finger_tip is not None:
                    if finger_tint is not None:
                        item.setBackground(finger_tint)
                    item.setToolTip(finger_tip)
                # On a corrected event the verdict is the reviewer's exact
                # match, so p(target) beside it is the detector's old score
                # and no longer decides anything.
                if manual and col == COL_FINGER_OK:
                    item.setToolTip(
                        "Verdict from manual review, not the θ rule: the reviewer's finger is "
                        "matched exactly against the cue. The p(target) cell is the detector's "
                        "original score and can sit on the other side of θ without contradicting "
                        "this."
                    )
                if col == COL_REVIEW and review_text:
                    item.setBackground(COLOR_TO_REVIEW if to_review else COLOR_REVIEWED)
                    item.setToolTip(
                        "Failed the finger rule and nobody has ruled on it yet - double-click the "
                        "row to watch the keypress. Events below "
                        f"p(target) = {FINGER_REVIEW_FLOOR:.2f} are not queued: the detection isn't "
                        "a close call there, so the automatic verdict stands."
                        if to_review else
                        "A human has watched this event in the review window."
                    )
                if manual and col == 9:
                    item.setBackground(COLOR_MANUAL)
                    item.setToolTip(
                        "Actual Finger was hand-corrected in the event review window: it no longer "
                        "matches the stored softmax argmax (the probabilities are kept unmodified as "
                        "the audit trail)."
                    )
                if col == len(cells) - 1:
                    if invalid:
                        item.setToolTip(
                            "Manually confirmed carry-over from the previous event - excluded from "
                            "every statistic (RT and accuracy). Right-click to restore."
                        )
                    elif suspected:
                        item.setBackground(COLOR_SUSPECTED)
                        item.setToolTip(
                            f"RT < {CARRYOVER_RT_THRESHOLD_S * 1000:.0f} ms - too fast for a reaction "
                            "to this cue; likely the tail of the previous event's presses. Check the "
                            "review video, then right-click to mark it invalid carry-over."
                        )
                table.setItem(row, col, item)
        return table

    def _build_confusion_box(self, summary: dict) -> QGroupBox:
        box = QGroupBox("Finger confusion (rows: target, columns: detected)")
        confusion = summary["confusion"]
        # Physical keyboard order, left pinky to right pinky - so
        # neighbouring-finger substitutions sit next to the diagonal.
        order = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]
        cols = order + ["?"]
        table = QTableWidget(len(order), len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setVerticalHeaderLabels(order)
        table.horizontalHeaderItem(len(order)).setToolTip(
            "Unresolved: the keypress was responded to, but no fingertip could be "
            "detected at that moment (hand not visible / tracking failed)."
        )
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        near_ties = 0
        for i, target in enumerate(order):
            row_counts = confusion.get(target, {})
            for j, actual in enumerate(order + [None]):
                cell = row_counts.get(actual)
                count = cell["n"] if cell else 0
                passed = cell["passed"] if cell else 0
                item = QTableWidgetItem(str(count) if count else "")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if count:
                    # Colour by the scored verdict, not by the cell's
                    # position: an off-diagonal cell whose events all
                    # cleared the threshold is a near-tie, not an error.
                    if actual is None:
                        item.setBackground(COLOR_UNRESOLVED)
                        item.setToolTip("No fingertip detected at the keypress - counts as incorrect.")
                    elif passed == count:
                        item.setBackground(COLOR_CORRECT if actual == target else COLOR_NEAR_TIE)
                        if actual != target:
                            near_ties += count
                            item.setToolTip(
                                f"{actual} was fractionally more probable, but {target} still cleared "
                                f"θ = {FINGER_PROBABILITY_THRESHOLD:.2f} - scored as the right finger."
                            )
                    else:
                        item.setBackground(COLOR_WRONG_FINGER_CELL)
                        near_ties += passed
                        item.setToolTip(
                            f"{passed} of {count} still cleared θ = {FINGER_PROBABILITY_THRESHOLD:.2f} "
                            f"and scored correct; {count - passed} did not."
                        )
                table.setItem(i, j, item)

        note = QLabel(
            "Rows are the cued finger, columns the most probable detected fingertip - so this "
            "matrix stays in detector terms, which is what the pre-screen's per-finger error "
            "profile needs. Off the diagonal is not automatically an error: the event still scores "
            f"correct when the target finger holds at least θ = {FINGER_PROBABILITY_THRESHOLD:.2f} "
            f"of the mass (amber, {near_ties} here) - those are the events the table above prints "
            "as \"R3 (≈R2)\". Red failed the rule, grey is unresolved."
        )
        note.setWordWrap(True)
        layout = QVBoxLayout(box)
        layout.addWidget(table)
        layout.addWidget(note)
        return box

    def _build_stats_box(self, summary: dict) -> QGroupBox:
        box = QGroupBox("Distribution / trend / audit stats")
        t = summary["timing_stats"]
        slope = summary["rt_slope_s_per_event"]
        wk = summary["wrong_key_stats"]
        conf = summary["confidence"]
        extra = summary["extra"]
        first, second = summary["first_half"], summary["second_half"]

        lines = [
            "<b>Timing distribution</b> (responded events): "
            f"median {_ms(t['median_s'])}, SD {_ms(t['sd_s'])}, "
            f"min {_ms(t['min_s'])}, max {_ms(t['max_s'])}, p95 {_ms(t['p95_s'])}",
            "<b>Within-trial trend</b>: "
            f"RT slope {slope * 1000:+.1f} ms/event" if slope is not None else
            "<b>Within-trial trend</b>: RT slope n/a",
            f"1st half: FA {_pct(first['fa_main'])}, RT {_ms(first['mean_timing_error_s'])} — "
            f"2nd half: FA {_pct(second['fa_main'])}, RT {_ms(second['mean_timing_error_s'])}",
            "<b>Wrong-key profile</b>: "
            f"mean distance {wk['mean_abs_semitones']:.1f} semitones, "
            f"{wk['below']} low / {wk['above']} high" if wk["mean_abs_semitones"] is not None else
            "<b>Wrong-key profile</b>: no wrong-key events",
            "<b>Detection confidence</b>: "
            f"mean p(target) {conf['mean_target_prob']:.2f}" if conf["mean_target_prob"] is not None else
            "<b>Detection confidence</b>: n/a (not analyzed)",
            f"mean top-1/top-2 margin {conf['mean_top_margin']:.2f}" if conf["mean_top_margin"] is not None else "",
            f"<b>Borderline events</b> (θ±{BORDERLINE_MARGIN:.2f}): {conf['borderline']} — audit these first",
            f"<b>Near-tie events</b> (scored correct, a neighbour was the argmax — shown as "
            f"\"R3 (≈R2)\" above): {self._near_ties}",
            f"<b>Sub-threshold matches</b> (argmax was the cued finger but it never cleared θ — "
            f"shown as \"R4 (p&lt;θ)\" above): {self._subthreshold_matches}",
            f"<b>Suspected carry-over</b> (matched RT &lt; {CARRYOVER_RT_THRESHOLD_S * 1000:.0f} ms, needs manual "
            f"review — right-click the event row): {summary['suspected_carryover']}",
            f"<b>Excluded carry-over</b> (manually confirmed, removed from all stats): "
            f"{summary['excluded_carryover']}",
            f"<b>Manually corrected events</b>: {summary['manual_corrections']}",
            f"<b>Still to review</b> (finger rule failed, p(target) ≥ {FINGER_REVIEW_FLOOR:.2f}, "
            f"no human verdict yet): {summary['to_review']}",
            "<b>QC — unmatched raw presses</b> (not scored, not anticipation): "
            + (
                f"{extra['extra_presses']} ({extra['double_hits']} simultaneous double-hits, "
                f"{extra['inter_trial_presses']} inter-trial strays, of {extra['note_on_total']} total presses)"
                if extra else "n/a"
            ),
        ]
        label = QLabel("<br>".join(line for line in lines if line))
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout = QVBoxLayout(box)
        layout.addWidget(label)
        return box
