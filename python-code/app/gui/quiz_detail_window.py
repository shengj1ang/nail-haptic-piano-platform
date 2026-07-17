"""Per-trial detail view - double-click a quiz row in the analysis
window (app/gui/quiz_analysis_window.py) to open it.

Deep dive into a single quiz, built for the manual audit loop: the top
table lists all 30 events with their target/actual key and finger,
probability, and verdicts - borderline finger verdicts (within
BORDERLINE_MARGIN of the threshold) are highlighted so you know which
events to check. Double-clicking an event opens the per-event review
window (app/gui/event_review_window.py): playback around the keypress
and correction of the detected finger; hand-corrected events are flagged
in the Manual column. Below the table: the target-vs-detected finger
confusion matrix and the distribution/trend statistics that don't fit
the main table (timing spread, within-trial trend, wrong-key spatial
profile, detection confidence, false starts).
"""

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..finger_matching import FINGER_PROBABILITY_THRESHOLD
from ..quiz import (
    BORDERLINE_MARGIN,
    META_FILENAME,
    RESULTS_FILENAME,
    QuizMeta,
    finger_manually_corrected,
    full_summary,
    load_quiz_results,
    note_name,
    quiz_dir,
)
from .event_review_window import EventReviewWindow

# Row/cell tints for the event table (light, readable on white).
COLOR_TIMEOUT = QColor(230, 230, 230)
COLOR_WRONG_KEY = QColor(255, 220, 220)
COLOR_WRONG_FINGER = QColor(255, 238, 215)
COLOR_BORDERLINE = QColor(255, 250, 190)
COLOR_MANUAL = QColor(220, 235, 255)

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
]


def _ms(x) -> str:
    return f"{x * 1000:.0f} ms" if x is not None else "n/a"


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


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

        bottom = QHBoxLayout()
        bottom.addWidget(self._build_confusion_box(summary), 1)
        bottom.addWidget(self._build_stats_box(summary), 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(header)
        layout.addWidget(events_table, 3)
        layout.addLayout(bottom, 2)
        self.setCentralWidget(central)  # deletes the previous central widget

    # ------------------------------------------------------------------

    def _open_event_review(self, row: int, _col: int) -> None:
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

    # ------------------------------------------------------------------

    def _build_events_table(self, results) -> QTableWidget:
        table = QTableWidget(len(results), len(EVENT_COLUMNS))
        table.setHorizontalHeaderLabels(EVENT_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)

        for row, r in enumerate(results):
            p = r.target_finger_probability
            borderline = p is not None and abs(p - FINGER_PROBABILITY_THRESHOLD) <= BORDERLINE_MARGIN
            manual = not r.timed_out and finger_manually_corrected(r)
            if r.timed_out:
                cells = [str(r.index), r.target_note_name, "—", "—", r.target_finger or "—",
                         "—", "—", "—", "timeout", ""]
                tint: Optional[QColor] = COLOR_TIMEOUT
            else:
                cells = [
                    str(r.index),
                    r.target_note_name,
                    note_name(r.actual_note) if r.actual_note is not None else "?",
                    "✓" if r.note_correct else "✗",
                    r.target_finger or "—",
                    r.actual_finger or "unresolved",
                    f"{p:.2f}" if p is not None else "n/a",
                    "—" if r.finger_correct is None else ("✓" if r.finger_correct else "✗"),
                    _ms(r.timing_error_s),
                    "✎ manual" if manual else "",
                ]
                if not r.note_correct:
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
                if manual and col == len(cells) - 1:
                    item.setBackground(COLOR_MANUAL)
                    item.setToolTip(
                        "Actual Finger was hand-corrected in the event review window: it no longer "
                        "matches the stored softmax argmax (the probabilities are kept unmodified as "
                        "the audit trail)."
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
        for i, target in enumerate(order):
            row_counts = confusion.get(target, {})
            for j, actual in enumerate(order + [None]):
                count = row_counts.get(actual, 0)
                item = QTableWidgetItem(str(count) if count else "")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if count:
                    # Diagonal = correct detections (green); off-diagonal =
                    # substitutions (red); "?" column = unresolved (gray).
                    if actual == target:
                        item.setBackground(QColor(215, 240, 215))
                    elif actual is None:
                        item.setBackground(QColor(230, 230, 230))
                    else:
                        item.setBackground(QColor(255, 220, 220))
                table.setItem(i, j, item)
        layout = QVBoxLayout(box)
        layout.addWidget(table)
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
            f"<b>Anticipation</b> (RT &lt; 100 ms): {summary['anticipation']}",
            f"<b>Manually corrected events</b>: {summary['manual_corrections']}",
            "<b>False starts</b> (unmatched raw presses): "
            + (str(extra["extra_presses"]) + f" (of {extra['note_on_total']} total presses)" if extra else "n/a"),
        ]
        label = QLabel("<br>".join(line for line in lines if line))
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout = QVBoxLayout(box)
        layout.addWidget(label)
        return box
