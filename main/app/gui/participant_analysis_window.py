"""Single-participant cross-trial analysis - launcher section 7.

Pick a Main User Study participant and get the within-subject picture
across their 27 trials (3 conditions x 3 levels x 3 unique sequences):
condition and difficulty comparisons, learning progression (across the
whole session, the trial 1 -> 3 order within each condition-level cell,
and the two composition-adjusted views that stop a changing
condition/difficulty mix from reading as practice), per-finger profiles
both by physical finger and by homologous finger ID, the per-finger
benefit of the haptic cue, reaction-time distributions, and a
data-quality audit with the detection-threshold sensitivity curve.
Every figure carries a caption with the computed numbers, so the tab is
readable without the chart.

Data comes from app.participant_export.collect_participant_data - the
exact rows the CSV export writes - so charts, CSVs and the Quiz
Analysis table can never disagree. Manually confirmed carry-over events
are removed once, at load (app.participant_analysis.valid_events), so
every event-level tab uses the same denominator as the trial-level
statistics that already excluded them; they survive only as counters in
the Quality tab.

Everything here is descriptive and within-subject. The computations are
shared with the Group Analysis window through app.participant_analysis,
which holds every reduction that turns ONE participant's rows into that
participant's own numbers; what needs several participants - the group
centre and its interval, the paired B/C contrasts, the repeated-measures
ANOVA, the finger-benefit group tests - stays in app.group_analysis and
is deliberately absent here. Where a group figure has an inferential
counterpart, this window shows the descriptive quantity and says so.

Export ("Export figures + data") covers every tab: each figure as a
300 dpi PNG and an SVG, every tidy table behind it as CSV, plus a
_manifest.csv recording which participant and how many trials/events the
files came from. Registration happens in _add_tab, not in the individual
_build_ methods, so a new tab cannot ship without its output.
"""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import finger_benefit as fb
from ..figure_axes import zero_based_ylim, zero_based_xlim
from .. import finger_common as fc
from .. import finger_equalisation as fq
from .. import participant_analysis as pa
from .. import session_progression as sp
from .. import session_progression_figures as sp_figures
from ..participant_analysis import (
    CAT_CK_CF,
    CAT_CK_WF,
    CAT_NO_RESPONSE,
    CAT_UNRESOLVED,
    CAT_WK_CF,
    CAT_WK_WF,
    CATEGORIES,
    ERROR_CATEGORIES,
    compute_error_breakdown,
    compute_finger_confusion,
    compute_trial_speed_accuracy,
    compute_wrong_key_distance,
    confusion_matrix,
    confusion_pair_count,
    relative_reduction,
    speed_accuracy_centroids,
    top_confusion,
    tradeoff_verdict,
    wrong_key_stats,
)
from ..participant_export import collect_participant_data
from ..pilot_study import DATA_DIR as STUDY_DATA_DIR
from ..pilot_study import list_participants
from ..sequence_generator import LEVEL_DISPLAY, LEVEL_TICK_LABEL
from .analysis_export import export_analysis
from .progress_task import run_with_progress
from .wrapping_tabs import WrappingTabWidget
from .stats_format import fmt, fmt_signed

CATEGORY_COLORS = {
    CAT_CK_CF: "#4a9d5b",
    CAT_CK_WF: "#e0a13c",
    CAT_WK_CF: "#7a6fb3",
    CAT_WK_WF: "#c94f4f",
    CAT_UNRESOLVED: "#a0a0a0",
    CAT_NO_RESPONSE: "#d8d8d8",
}

class ScrollFriendlyCanvas(FigureCanvas):
    """FigureCanvas that lets the mouse wheel through to the surrounding
    QScrollArea. The stock canvas forwards wheel events to matplotlib's
    scroll_event (nothing here uses them), which swallowed page scrolling
    whenever the cursor sat over a chart."""

    def wheelEvent(self, event) -> None:
        event.ignore()


CONDITIONS = ["A", "B", "C"]
CONDITION_COLORS = {"A": "#8a8a8a", "B": "#3a76c4", "C": "#d9663d"}
LEVELS = ["alpha", "beta", "gamma"]
FINGER_ORDER = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]

# Formatting lives in .stats_format so the "&lt;" rule (a bare "<" in a
# RichText caption opens a tag and Qt eats the rest of the line) has one
# home shared with the other analysis windows.
_fmt = fmt
_fmt_signed = fmt_signed


def _mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return float(np.mean(vals)) if vals else None


def _fmt_pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def _fmt_ms(x) -> str:
    return f"{x * 1000:.0f} ms" if x is not None else "n/a"


class ParticipantAnalysisWindow(QMainWindow):
    def __init__(self, cfg=None):
        super().__init__()
        self.setWindowTitle("Participant Analysis")
        self.resize(1150, 820)

        self.participant_combo = QComboBox()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_participants)
        analyze_btn = QPushButton("Analyze")
        analyze_btn.clicked.connect(self._analyze)
        self.analyze_all_btn = QPushButton("Analyze + export ALL")
        self.analyze_all_btn.setToolTip(
            "Walk every Main User Study participant, build all tabs and write the same "
            "figures + CSVs each Export button writes, into each participant's own "
            "data/MainUserStudy/<participant>/figures/ folder. Failures are skipped and "
            "listed at the end; the window is left showing the last participant analyzed."
        )
        self.analyze_all_btn.clicked.connect(self._analyze_and_export_all)
        self.save_figs_btn = QPushButton("Export figures + data")
        self.save_figs_btn.setToolTip(
            "Write every chart as a 300 dpi PNG plus the underlying tidy CSVs under "
            "data/MainUserStudy/<participant>/figures/, named <participant>_<slug>.png/.csv "
            "for direct citation in the report."
        )
        self.save_figs_btn.setEnabled(False)
        self.save_figs_btn.clicked.connect(self._save_figures)
        self.status_label = QLabel("Pick a participant and click Analyze.")
        self.status_label.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("Participant:"))
        top.addWidget(self.participant_combo, 1)
        top.addWidget(refresh_btn)
        top.addWidget(analyze_btn)
        top.addWidget(self.analyze_all_btn)
        top.addWidget(self.save_figs_btn)

        self.tabs = WrappingTabWidget()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(self.status_label)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._refresh_participants()

    def _refresh_participants(self) -> None:
        self.participant_combo.clear()
        self.participant_combo.addItems(list_participants())

    # ------------------------------------------------------------------

    def _analyze(self) -> None:
        participant = self.participant_combo.currentText()
        if not participant:
            return
        try:
            note = self._load_participant(participant)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't load participant", f"{participant}: {e}")
            return
        if note is None:
            self.status_label.setText("Analysis cancelled — nothing to show or export.")
            return
        self.status_label.setText(note)

    def _load_participant(self, participant: str) -> Optional[str]:
        """Load `participant`'s data and build every tab onto self, ready
        for export. Returns the one-line status note on success, or None if
        the user cancelled the build; raises on a load failure (missing /
        unreadable data, or no completed trials). Shows no dialogs of its
        own, so both the single-participant button and the batch can decide
        how to report - the batch skips a raise and keeps going.

        Side effects mirror what the export needs: self._participant /
        _trials / _events / _all_events / _figures / _datasets are the exact
        state _save_figures reads."""
        trials, all_events, missing = collect_participant_data(participant)
        if not trials:
            raise ValueError("no completed trials with quiz data")

        # One filter, once, at the door: manually confirmed carry-over
        # presses are already out of every per-trial statistic in the
        # export, so an event-level tab that kept them would quietly
        # contradict the trial-level numbers printed beside it. The
        # unfiltered list stays for the Quality tab's counters.
        events = pa.valid_events(all_events)

        analyzed = [t for t in trials if t["analyzed"]]
        note = f"{participant}: {len(trials)} trials loaded, {len(analyzed)} analyzed from video"
        if missing:
            note += f", {len(missing)} missing ({', '.join(missing)})"
        excluded = len(all_events) - len(events)
        note += (f"; {len(events)} of {len(all_events)} events valid"
                 + (f" ({excluded} excluded as confirmed carry-over)" if excluded else ""))
        if len(analyzed) < len(trials):
            note += " — finger-based charts use analyzed trials only; run the video analysis for the rest."
        self.status_label.setText(note)

        self.tabs.clear()
        self._participant = participant
        self._trials = trials
        self._events = events
        self._all_events = all_events
        # slug -> Figure; every registered figure is exported.
        self._figures: Dict[str, Figure] = {}
        # slug -> tidy DataFrame written next to the figures on export.
        self._datasets: Dict[str, object] = {}
        if not run_with_progress(self, f"Analyzing {participant}",
                                 [(title, self._tab_runner(title, build))
                                  for title, build in self._tab_builders()]):
            # A half-built window would export a partial set of figures as
            # though it were the whole analysis, so cancelling leaves
            # nothing rather than something misleading.
            self.tabs.clear()
            self._figures.clear()
            self._datasets.clear()
            self.save_figs_btn.setEnabled(False)
            return None
        self.save_figs_btn.setEnabled(True)
        return note

    def _analyze_and_export_all(self) -> None:
        """One click: for every Main User Study participant, run the same
        load-and-build the Analyze button runs, then write the same
        figures + CSVs the Export button writes into that participant's own
        figures/ folder.

        A participant that fails to load is skipped and named in the final
        summary rather than aborting the run; the window is left showing the
        last participant that built, and its export button stays live for
        that one. Cancelling either the build or the write dialog stops the
        whole batch (Cancel means stop, not skip)."""
        participants = list_participants()
        if not participants:
            QMessageBox.information(self, "No participants",
                                    "No Main User Study participants found.")
            return

        exported: List[str] = []
        failed: List[str] = []
        cancelled = False
        for participant in participants:
            # Keep the dropdown in step so the window ends on the last one
            # analyzed, and so a later manual Export targets that participant.
            self.participant_combo.setCurrentText(participant)
            try:
                note = self._load_participant(participant)
            except Exception as e:
                failed.append(f"{participant} ({type(e).__name__}: {e})")
                continue
            if note is None:  # build dialog cancelled
                cancelled = True
                break
            result = self._export_current()
            if "CANCELLED" in result:  # export dialog cancelled
                cancelled = True
                exported.append(f"{participant} (incomplete)")
                break
            exported.append(participant)

        summary = f"Batch export: {len(exported)} exported"
        if failed:
            summary += f", {len(failed)} skipped"
        if cancelled:
            summary += " — CANCELLED before finishing"
        if exported:
            summary += ".  Exported: " + ", ".join(exported)
        if failed:
            summary += ".  Skipped: " + "; ".join(failed)
        self.status_label.setText(summary)

    def _save_figures(self) -> None:
        """Every figure as a PNG and an SVG, every tidy table as a CSV,
        plus a manifest - all named <participant>_<slug>.* so the report
        can cite files verbatim.

        Registration happens in _add_tab, so this writes exactly what the
        window is showing. The writing itself, and its progress dialog,
        live in app.gui.analysis_export - shared with the group window,
        which exports the same way without the participant prefix.

        A CSV sitting in a report appendix has to be self-identifying, so
        the manifest's provenance row records whose data it is and on what
        denominator."""
        self.status_label.setText(self._export_current())

    def _export_current(self) -> str:
        """Write the figures + CSVs for whichever participant is currently
        built onto self, and return the status line. Split out from
        _save_figures so the batch can export each participant without the
        per-participant status line, then post its own summary."""
        analyzed = sum(1 for t in self._trials if t.get("analyzed"))
        provenance = {
            "rows": self._participant,
            "columns": (f"{len(self._trials)} trials ({analyzed} analyzed), "
                        f"{len(self._all_events)} events "
                        f"({len(self._events)} valid); "
                        f"exported {pd.Timestamp.now().isoformat(timespec='seconds')}"),
        }
        return export_analysis(
            self, STUDY_DATA_DIR / self._participant / "figures",
            self._figures, self._datasets, provenance, prefix=self._participant)

    def _tab_builders(self):
        """Every tab in display order, as (title, builder).

        This list is the analysis: _analyze() walks it and the progress
        dialog sizes itself from it, so adding an analysis means adding one
        line here and nothing else - no count to bump, no label to keep in
        step. test_participant_analysis_window.py asserts the built tabs
        match this list, which is what catches a tab added anywhere else.

        Builders read the participant data off self, set up by _analyze()
        just above, and return the (caption, figures, datasets) triple that
        _add_tab takes - or None if the builder adds its own tab, as the
        confusion tab does for its view selector.
        """
        trials, events, all_events = self._trials, self._events, self._all_events
        return [
            ("Overview", lambda: self._build_overview(self._participant, trials)),
            ("Learning", lambda: self._build_learning(trials)),
            ("Difficulty", lambda: self._build_difficulty(trials)),
            ("Trade-off", lambda: self._build_tradeoff(trials)),
            ("Errors", lambda: self._build_errors(trials, events)),
            ("Confusion", lambda: self._add_confusion_tab(trials, events)),
            ("Fingers", lambda: self._build_fingers(events)),
            ("Finger benefit", lambda: self._build_finger_benefit(events)),
            ("Timing", lambda: self._build_timing(events)),
            ("Quality", lambda: self._build_quality(trials, all_events)),
        ]

    def _tab_runner(self, title: str, build):
        """Wrap a builder as a no-argument step for run_with_progress."""
        def run() -> None:
            result = build()
            if result is not None:  # None = the builder added its own tab
                self._add_tab(title, *result)
        return run

    def _add_tab(self, title: str, caption_html: str,
                 figures: Dict[str, Figure],
                 datasets: Optional[Dict[str, object]] = None) -> None:
        """Render one tab AND register everything on it for export.

        Registration happens here, not in the individual _build_ methods,
        so a tab physically cannot be added without its figures and the
        tidy tables behind them joining the export. Keys are the file
        stems _save_figures writes after the participant prefix."""
        self._figures.update(figures)
        self._register_datasets(datasets)
        content = QWidget()
        layout = QVBoxLayout(content)
        caption = QLabel(caption_html)
        caption.setWordWrap(True)
        caption.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(caption)
        for fig in figures.values():
            canvas = ScrollFriendlyCanvas(fig)
            # Fixed height at the figure's designed size: the page then
            # overflows the viewport and scrolls vertically instead of
            # squashing charts to fit.
            canvas.setFixedHeight(int(fig.get_figheight() * 100))
            canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(canvas)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, title)

    def _register_datasets(self, datasets: Optional[Dict[str, object]]) -> None:
        """Register tidy tables for export, skipping the empty ones.

        A table can be legitimately empty - threshold sensitivity when no
        trial was video-analyzed, the benefit cells when a condition is
        missing - and a zero-row CSV in a results folder is noise that
        reads like a failed export. The captions state those cases in
        words."""
        for slug, df in (datasets or {}).items():
            if df is not None and len(df):
                self._datasets[slug] = df

    # ------------------------------------------------------------------
    # Helpers over trial/event rows

    @staticmethod
    def _by_condition(rows: List[dict]) -> Dict[str, List[dict]]:
        return {c: [r for r in rows if r["condition"] == c] for c in CONDITIONS}

    @staticmethod
    def _condition_title(trials: List[dict], c: str) -> str:
        labels = {t.get("condition_label") or t.get("guidance_type") for t in trials if t["condition"] == c}
        label = next(iter(labels), "")
        return f"{c} ({label})" if label else c

    # ------------------------------------------------------------------
    # Overview

    def _build_overview(self, participant: str, trials: List[dict]):
        by_c = self._by_condition(trials)
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}

        lines = [f"<h3>{participant} — session overview</h3>"]
        lines.append(
            f"{len(trials)} trials, {sum(t['note_count'] for t in trials)} target events. "
            "Per-condition means (each over up to 9 trials: 3 difficulty levels — α (alpha), "
            "β (beta), γ (gamma) — × 3 unique sequences):"
        )
        rows_html = ["<table border='0' cellspacing='0' cellpadding='4'>"
                     "<tr><th align='left'>Condition</th><th>Key Acc</th><th>FA main</th><th>FA|key</th>"
                     "<th>RT (key ok)</th><th>Timeouts</th><th>Excluded carry-over</th></tr>"]
        for c in CONDITIONS:
            ts = by_c[c]
            if not ts:
                continue
            rows_html.append(
                f"<tr><td><b>{cond_titles[c]}</b></td>"
                f"<td align='center'>{_fmt_pct(_mean([t['key_accuracy'] for t in ts]))}</td>"
                f"<td align='center'>{_fmt_pct(_mean([t['fa_main'] for t in ts if t['analyzed']]))}</td>"
                f"<td align='center'>{_fmt_pct(_mean([t['fa_given_key'] for t in ts if t['analyzed']]))}</td>"
                f"<td align='center'>{_fmt_ms(_mean([t['rt_correct_key_s'] for t in ts]))}</td>"
                f"<td align='center'>{sum(t['misses'] for t in ts)}</td>"
                f"<td align='center'>{sum(t['excluded_carryover'] for t in ts)}</td></tr>"
            )
        rows_html.append("</table>")
        lines.append("".join(rows_html))

        fa_b = _mean([t["fa_main"] for t in by_c["B"] if t["analyzed"]])
        fa_c = _mean([t["fa_main"] for t in by_c["C"] if t["analyzed"]])
        if fa_b is not None and fa_c is not None:
            diff = (fa_c - fa_b) * 100
            direction = "higher" if diff >= 0 else "lower"
            lines.append(
                f"<b>Headline contrast (this participant only):</b> haptic (C) finger accuracy is "
                f"{abs(diff):.0f} pp {direction} than visual (B). Descriptive only — the paired "
                "statistical comparison happens at the group level."
            )

        fig = Figure(figsize=(9, 3.6))
        ax1, ax2 = fig.subplots(1, 2)
        metrics = [("key_accuracy", "Key Acc"), ("fa_main", "FA main"), ("fa_given_key", "FA|key")]
        width = 0.25
        x = np.arange(len(metrics))
        for i, c in enumerate(CONDITIONS):
            ts = [t for t in by_c[c] if t["analyzed"]] or by_c[c]
            vals = [(_mean([t[m] for t in ts]) or 0) * 100 for m, _ in metrics]
            ax1.bar(x + (i - 1) * width, vals, width, label=cond_titles[c], color=CONDITION_COLORS[c])
        ax1.set_xticks(x, [lbl for _, lbl in metrics])
        ax1.set_ylabel("%")
        ax1.set_ylim(0, 105)
        ax1.set_title("Accuracy by condition")
        ax1.legend(fontsize=8)

        for i, c in enumerate(CONDITIONS):
            ts = by_c[c]
            vals = [t["rt_correct_key_s"] for t in ts if t["rt_correct_key_s"] is not None]
            if vals:
                ax2.bar(i, np.mean(vals) * 1000, 0.6, color=CONDITION_COLORS[c])
                ax2.errorbar(i, np.mean(vals) * 1000, yerr=np.std(vals) * 1000, color="black", capsize=4)
        ax2.set_xticks(range(len(CONDITIONS)), CONDITIONS)  # short labels; full names in the left legend
        ax2.set_ylabel("ms")
        ax2.set_title("Mean RT, correct-key events (±SD across trials)")
        fig.tight_layout()
        datasets = {
            # The same two tables the group window aggregates over, here
            # for one participant: the condition means are what a group
            # analysis would consume as this participant's contribution.
            "overview_condition_metrics": pa.participant_condition_metrics(trials),
            "overview_cell_metrics": pa.participant_cell_metrics(trials),
        }
        return "".join(f"<p>{line}</p>" for line in lines), {"overview": fig}, datasets

    # ------------------------------------------------------------------
    # Learning progression

    def _build_learning(self, trials: List[dict]):
        ordered = sorted(trials, key=lambda t: t["trial_index"])
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}

        # Whole-session trend
        fig1 = Figure(figsize=(9, 3.6))
        ax1, ax2 = fig1.subplots(1, 2)
        xs = [t["trial_index"] for t in ordered]
        for ax, key, scale, ylabel, title in (
            (ax1, "fa_main", 100, "FA main (%)", "Finger accuracy across the session"),
            (ax2, "rt_correct_key_s", 1000, "RT (ms)", "Reaction time across the session"),
        ):
            pts_x, pts_y = [], []
            for t in ordered:
                v = t[key]
                if v is None:
                    continue
                ax.scatter(t["trial_index"], v * scale, color=CONDITION_COLORS[t["condition"]], s=28)
                pts_x.append(t["trial_index"])
                pts_y.append(v * scale)
            if len(pts_x) >= 2:
                slope, intercept = np.polyfit(pts_x, pts_y, 1)
                ax.plot(xs, [slope * x + intercept for x in xs], "--", color="black", linewidth=1)
            ax.set_xlabel("presentation order (trial 1-27)")
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontsize=10)
        zero_based_ylim(ax2)
        session_fa_slope = self._session_slope(ordered, "fa_main", 100)
        session_rt_slope = self._session_slope(ordered, "rt_correct_key_s", 1000)

        # Within-cell trial 1 -> 3 (occurrence order inside each condition-level cell)
        pos_stats: Dict[str, Dict[int, Dict[str, Optional[float]]]] = {}
        for c in CONDITIONS:
            pos_stats[c] = {1: {}, 2: {}, 3: {}}
            fa_by_pos = {1: [], 2: [], 3: []}
            rt_by_pos = {1: [], 2: [], 3: []}
            for level in LEVELS:
                cell = sorted(
                    (t for t in trials if t["condition"] == c and t["level"] == level),
                    key=lambda t: t["trial_index"],
                )
                for pos, t in enumerate(cell, start=1):
                    if pos > 3:
                        break
                    fa_by_pos[pos].append(t["fa_main"])
                    rt_by_pos[pos].append(t["rt_correct_key_s"])
            for pos in (1, 2, 3):
                pos_stats[c][pos] = {"fa": _mean(fa_by_pos[pos]), "rt": _mean(rt_by_pos[pos])}

        fig2 = Figure(figsize=(9, 3.6))
        bx1, bx2 = fig2.subplots(1, 2)
        for c in CONDITIONS:
            fa_vals = [pos_stats[c][p]["fa"] for p in (1, 2, 3)]
            rt_vals = [pos_stats[c][p]["rt"] for p in (1, 2, 3)]
            if any(v is not None for v in fa_vals):
                bx1.plot([1, 2, 3], [None if v is None else v * 100 for v in fa_vals],
                         "o-", color=CONDITION_COLORS[c], label=cond_titles[c])
            if any(v is not None for v in rt_vals):
                bx2.plot([1, 2, 3], [None if v is None else v * 1000 for v in rt_vals],
                         "o-", color=CONDITION_COLORS[c], label=cond_titles[c])
        for bx, ylabel, title in ((bx1, "FA main (%)", "FA by within-cell position"),
                                  (bx2, "RT (ms)", "RT by within-cell position")):
            bx.set_xticks([1, 2, 3], ["1st", "2nd", "3rd"])
            bx.set_xlabel("occurrence within condition-level cell")
            bx.set_ylabel(ylabel)
            bx.set_title(title, fontsize=10)
            bx.legend(fontsize=8)
        zero_based_ylim(bx2)
        fig2.tight_layout()

        improvements = []
        for c in CONDITIONS:
            fa1, fa3 = pos_stats[c][1]["fa"], pos_stats[c][3]["fa"]
            rt1, rt3 = pos_stats[c][1]["rt"], pos_stats[c][3]["rt"]
            if fa1 is not None and fa3 is not None:
                improvements.append(
                    f"{cond_titles[c]}: FA {_fmt_pct(fa1)} → {_fmt_pct(fa3)} "
                    f"({(fa3 - fa1) * 100:+.0f} pp), RT {_fmt_ms(rt1)} → {_fmt_ms(rt3)}"
                    + (f" ({(rt3 - rt1) * 1000:+.0f} ms)" if rt1 is not None and rt3 is not None else "")
                )

        # The per-position table is still exported (it carries both the
        # observed and the condition-adjusted value per position), but it
        # gets no figure of its own: figure 1 above already plots every
        # observed trial at its real position, and the adjusted series
        # would be the only thing a second figure added.
        pos = pa.session_position_metrics(trials)

        # Difficulty-aligned progression: all 27 trials, relabelled
        # occurrence 1-9 within each difficulty level.
        difficulty_progression = sp.difficulty_progression_metrics(trials)
        difficulty_summary = sp.difficulty_progression_summary(difficulty_progression)
        difficulty_figures = {
            slug.replace("group_learning_", "learning_"): fig
            for slug, fig in sp_figures.build_difficulty_progression_figures(
                difficulty_progression, difficulty_summary).items()
        }

        caption = (
            "<h3>Learning progression</h3>"
            "<p><b>Figure 1 — whole session:</b> every trial in presentation order, coloured by "
            "condition, with a least-squares trend line. "
            f"Overall FA trend {session_fa_slope:+.2f} pp/trial, RT trend {session_rt_slope:+.1f} ms/trial "
            "(negative RT slope = getting faster). This trend mixes conditions and difficulty, so read "
            "it as general familiarisation, not condition learning. The per-position table exported "
            "with this tab carries a condition-adjusted value beside every observed one if you need "
            "to separate the two numerically.</p>"
            "<p><b>Figure 2 — trial 1 → 3 within each condition-level cell:</b> the three unique "
            "sequences of a cell are averaged by their occurrence order. Because every sequence is seen only "
            "once, this is short-term exposure to the condition and difficulty, not sequence memorisation "
            "(the report's trial-order trend).</p>"
            "<p><b>1st → 3rd occurrence:</b><br>" + "<br>".join(improvements) + "</p>"
            "<p><b>Figure 3 — difficulty-aligned progression:</b> a complementary view keeping all 27 "
            "trials. Within each difficulty, the nine trials are sorted by their real session position and "
            "relabelled occurrence 1–9, so α (alpha), β (beta) and γ (gamma) can be overlaid on one axis. "
            "Correct-key RT on the left, key accuracy on the right. Every plotted point is an observed "
            "trial; at N = 1 the bold \"group mean\" line coincides with this participant's own "
            "trajectory. Each position carries one randomly assigned condition, so part of any wobble is "
            "that mix rather than progression — the exported "
            "<i>learning_difficulty_progression_trials</i> table holds the condition-adjusted value "
            "beside every observed one for checking that.</p>"
            "<p><b>Axes:</b> RT panels start at zero on every figure in this tab. Accuracy panels "
            "autoscale to this participant's own range, so their vertical scale differs between "
            "participants — compare the tick values, not the shape.</p>"
        )
        fig1.tight_layout()
        figures = {"learning_session": fig1, "learning_withincell": fig2}
        figures.update(difficulty_figures)
        datasets = {
            "learning_within_cell_repetition": pa.within_cell_repetition(trials),
            "learning_repetition_by_condition": pa.participant_repetition_metrics(trials),
            "learning_session_position": pos,
            "learning_difficulty_progression_trials": difficulty_progression,
            "learning_difficulty_progression_summary": difficulty_summary,
        }
        return caption, figures, datasets

    @staticmethod
    def _session_slope(ordered: List[dict], key: str, scale: float) -> float:
        pts = [(t["trial_index"], t[key] * scale) for t in ordered if t[key] is not None]
        if len(pts) < 2:
            return 0.0
        xs, ys = zip(*pts)
        return float(np.polyfit(xs, ys, 1)[0])

    # ------------------------------------------------------------------
    # Difficulty

    def _build_difficulty(self, trials: List[dict]):
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        fig = Figure(figsize=(9, 3.6))
        ax1, ax2 = fig.subplots(1, 2)
        summary_lines = []
        for c in CONDITIONS:
            fa_vals, rt_vals = [], []
            for level in LEVELS:
                cell = [t for t in trials if t["condition"] == c and t["level"] == level]
                fa_vals.append(_mean([t["fa_main"] for t in cell if t["analyzed"]]))
                rt_vals.append(_mean([t["rt_correct_key_s"] for t in cell]))
            if any(v is not None for v in fa_vals):
                ax1.plot(range(3), [None if v is None else v * 100 for v in fa_vals],
                         "o-", color=CONDITION_COLORS[c], label=cond_titles[c])
            if any(v is not None for v in rt_vals):
                ax2.plot(range(3), [None if v is None else v * 1000 for v in rt_vals],
                         "o-", color=CONDITION_COLORS[c], label=cond_titles[c])
            parts = [f"{LEVEL_DISPLAY[lv]} {_fmt_pct(fa)}"
                     for lv, fa in zip(LEVELS, fa_vals)]
            summary_lines.append(f"{cond_titles[c]}: FA " + " / ".join(parts))
        for ax, ylabel, title in ((ax1, "FA main (%)", "Finger accuracy by difficulty"),
                                  (ax2, "RT (ms)", "Reaction time by difficulty")):
            ax.set_xticks(range(3), [LEVEL_TICK_LABEL[lv] for lv in LEVELS])
            ax.set_xlabel("difficulty level")
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=8, title="Feedback condition", title_fontsize=8)
        zero_based_ylim(ax2)
        fig.tight_layout()
        caption = (
            "<h3>Difficulty effect</h3>"
            "<p>Each point averages one condition-level cell (3 unique sequences). The three levels were "
            "generated as matched families with validated increasing motor/sequence/bimanual cost "
            "(α (alpha) &lt; β (beta) &lt; γ (gamma)), so a falling FA line or rising RT line means "
            "difficulty is biting; where the "
            "conditions separate is where guidance modality matters most for this participant.</p>"
            "<p>" + "<br>".join(summary_lines) + "</p>"
        )
        return caption, {"difficulty": fig}, {
            "difficulty_cell_metrics": pa.participant_cell_metrics(trials),
        }

    # ------------------------------------------------------------------
    # Speed-accuracy trade-off

    def _build_tradeoff(self, trials: List[dict]):
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        df = compute_trial_speed_accuracy(trials)
        centroids = speed_accuracy_centroids(df)
        by_c = centroids.set_index("condition")

        fig = Figure(figsize=(9, 5.2))
        ax = fig.subplots(1, 1)
        for c in CONDITIONS:
            pts = df[(df["condition"] == c) & df["included"]]
            if pts.empty:
                continue
            ax.scatter(pts["rt_ms"], pts["fa_pct"], s=34, alpha=0.65,
                       color=CONDITION_COLORS[c], label=cond_titles[c])
            cen = by_c.loc[c]
            ax.errorbar(cen["rt_ms"], cen["fa_pct"],
                        xerr=0 if np.isnan(cen["rt_sd_ms"]) else cen["rt_sd_ms"],
                        yerr=0 if np.isnan(cen["fa_sd_pct"]) else cen["fa_sd_pct"],
                        color=CONDITION_COLORS[c], capsize=4, linewidth=1.4, zorder=4)
            ax.scatter([cen["rt_ms"]], [cen["fa_pct"]], s=230, marker="D",
                       color=CONDITION_COLORS[c], edgecolor="black", zorder=5)
        ax.set_xlabel("mean RT of correct-key events (ms)")
        ax.set_ylabel("FA main (%)")
        zero_based_xlim(ax)
        ax.set_title("Speed-accuracy trade-off: one point per trial, diamonds = condition centroids (±SD)",
                     fontsize=10)
        ax.legend(fontsize=8)
        fig.tight_layout()

        lines = ["<h3>Speed-accuracy trade-off</h3>"]
        for c in CONDITIONS:
            if c in by_c.index:
                cen = by_c.loc[c]
                lines.append(f"{cond_titles[c]}: mean RT {cen['rt_ms']:.0f} ms, mean FA {cen['fa_pct']:.0f}% "
                             f"(n={int(cen['n'])} trials)")
        if "B" in by_c.index and "C" in by_c.index:
            d_fa = by_c.loc["C", "fa_pct"] - by_c.loc["B", "fa_pct"]
            d_rt = by_c.loc["C", "rt_ms"] - by_c.loc["B", "rt_ms"]
            lines.append(f"<b>C vs B:</b> FA {d_fa:+.0f} pp, RT {d_rt:+.0f} ms.")
            verdict = tradeoff_verdict(centroids)
            if verdict:
                lines.append(f"<b>{verdict}</b>")
        excluded = df[~df["included"]]
        if len(excluded):
            per_c = ", ".join(f"{c}: {n}" for c, n in excluded.groupby("condition").size().items())
            lines.append(f"Excluded trials (no valid correct-key RT or not analyzed - never plotted as 0): "
                         f"{len(excluded)} ({per_c}).")
        lines.append("No regression or connecting lines on purpose: 9 trials per condition are too few "
                     "for a trustworthy fit, and the three centroids stand on their own.")
        return "".join(f"<p>{line}</p>" for line in lines), {"tradeoff": fig}, {
            "tradeoff_trials": df,
            "tradeoff_centroids": centroids,
        }

    # ------------------------------------------------------------------
    # Event-level error breakdown (+ wrong-key distance)

    def _build_errors(self, trials: List[dict], events: List[dict]):
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        breakdown_df = compute_error_breakdown(events)
        # condition -> {category: count}, for the existing plotting code.
        breakdown = {
            c: dict(zip(sub["category"], sub["count"]))
            for c, sub in breakdown_df.groupby("condition")
        }

        # Figure A: 100% stacked composition
        fig_a = Figure(figsize=(9, 4.2))
        ax = fig_a.subplots(1, 1)
        present = [c for c in CONDITIONS if c in breakdown]
        for i, c in enumerate(present):
            counts = breakdown[c]
            total = sum(counts.values())
            bottom = 0.0
            for cat in CATEGORIES:
                pct = 100 * counts[cat] / total if total else 0
                if pct == 0:
                    continue
                ax.bar(i, pct, 0.55, bottom=bottom, color=CATEGORY_COLORS[cat],
                       label=cat if i == 0 else None)
                if pct >= 4:
                    ax.text(i, bottom + pct / 2, f"{pct:.0f}%", ha="center", va="center", fontsize=8)
                bottom += pct
        # One legend entry per category, regardless of which bar drew it first.
        handles = [Patch(facecolor=CATEGORY_COLORS[cat], label=cat) for cat in CATEGORIES]
        ax.legend(handles=handles, fontsize=7, loc="center left", bbox_to_anchor=(1.0, 0.5))
        ax.set_xticks(range(len(present)), present)
        ax.set_ylabel("% of all target events")
        ax.set_ylim(0, 100)
        ax.set_title("Event outcome composition per condition (100% stacked)", fontsize=10)
        fig_a.tight_layout()

        # Figure B: error-only counts
        fig_b = Figure(figsize=(9, 3.8))
        axb = fig_b.subplots(1, 1)
        width = 0.25
        x = np.arange(len(ERROR_CATEGORIES))
        for i, c in enumerate(present):
            vals = [breakdown[c][cat] for cat in ERROR_CATEGORIES]
            bars = axb.bar(x + (i - 1) * width, vals, width, color=CONDITION_COLORS[c], label=c)
            for bar, v in zip(bars, vals):
                if v:
                    axb.text(bar.get_x() + bar.get_width() / 2, v, str(v), ha="center", va="bottom", fontsize=8)
        axb.set_xticks(x, [cat.replace(" + ", "+\n") for cat in ERROR_CATEGORIES], fontsize=8)
        axb.set_ylabel("event count")
        axb.set_title("Error events only (counts) - where do B and C differ?", fontsize=10)
        axb.legend(fontsize=8)
        fig_b.tight_layout()

        # Figure C: wrong-key distance
        distance_df = compute_wrong_key_distance(events)
        stats_df = wrong_key_stats(distance_df)
        stats_by_c = stats_df.set_index("condition") if len(stats_df) else stats_df
        fig_c = Figure(figsize=(9, 3.4))
        axc = fig_c.subplots(1, 1)
        max_d = int(stats_df["max"].max()) if len(stats_df) else 1
        xd = np.arange(1, max_d + 1)
        dist_lines = []
        for i, c in enumerate(CONDITIONS):
            if not len(stats_df) or c not in stats_by_c.index:
                dist_lines.append(f"{c}: no wrong-key events")
                continue
            dists = list(distance_df[distance_df["condition"] == c]["distance"])
            counts = [dists.count(k) for k in xd]
            axc.bar(xd + (i - 1) * 0.25, counts, 0.25, color=CONDITION_COLORS[c], label=c)
            d = stats_by_c.loc[c]
            dist_lines.append(f"{c}: n={int(d['n'])}, median {d['median']:.0f}, mean {d['mean']:.1f}, "
                              f"max {int(d['max'])} ({d['metric']})")
        axc.set_xticks(xd)
        axc.set_xlabel("distance from target key")
        axc.set_ylabel("wrong-key events")
        axc.set_title("Wrong-key distance (calibrated key-order index)", fontsize=10)
        if len(stats_df):  # a flawless session draws no bars, hence no legend
            axc.legend(fontsize=8)
        fig_c.tight_layout()

        # Caption with the B-vs-C arithmetic
        lines = ["<h3>Event-level error breakdown</h3>"]
        for c in present:
            counts = breakdown[c]
            total = sum(counts.values())
            ckwf = counts[CAT_CK_WF]
            lines.append(
                f"<b>{cond_titles[c]}</b> ({total} events): complete correct {counts[CAT_CK_CF]}, "
                f"correct key + wrong finger {ckwf} ({100 * ckwf / total:.0f}%), "
                f"wrong key total {counts[CAT_WK_CF] + counts[CAT_WK_WF]}, "
                f"unresolved {counts[CAT_UNRESOLVED]}, timeouts {counts[CAT_NO_RESPONSE]}"
            )
        if "B" in breakdown and "C" in breakdown:
            b_n, c_n = breakdown["B"][CAT_CK_WF], breakdown["C"][CAT_CK_WF]
            rel = relative_reduction(b_n, c_n)
            msg = f"<b>C vs B:</b> correct-key/wrong-finger events {b_n} → {c_n} ({b_n - c_n:+d} × −1)"
            if rel is not None:
                msg = (f"<b>C vs B:</b> correct-key/wrong-finger events {b_n} → {c_n}, "
                       f"a {rel * 100:.0f}% relative reduction.")
            lines.append(msg)
            key_b = sum(1 for e in events if e["condition"] == "B" and e["key_correct"])
            key_c = sum(1 for e in events if e["condition"] == "C" and e["key_correct"])
            tot_b = sum(breakdown["B"].values()) or 1
            tot_c = sum(breakdown["C"].values()) or 1
            if rel is not None and rel >= 0.2 and abs(key_b / tot_b - key_c / tot_c) <= 0.05:
                lines.append("<b>For this participant, the apparent haptic advantage was primarily "
                             "associated with fewer finger-selection errors on otherwise correct "
                             "keypresses.</b>")
        lines.append("Unresolved events are shown as their own category here, never folded into "
                     "\"wrong finger\"; under the primary FA definition they count as incorrect. "
                     "Timeouts have no keypress and stay outside the key/finger cells. Denominator: "
                     "this participant's valid events — confirmed carry-over presses are already "
                     "excluded, and unmatched extra presses are QC-only (Quality tab). "
                     "Descriptive only — group-level inference is reported separately.")
        caption = "".join(f"<p>{line}</p>" for line in lines)
        datasets = {
            "error_breakdown": breakdown_df,
            # Proportions rather than counts: the form the group window
            # averages, and the one that stays comparable if a condition
            # ends up with a different number of valid events.
            "error_proportions": pa.participant_outcome_proportions(events),
            "wrongkey_distance": distance_df,
            "wrongkey_distance_summary": stats_df,
        }
        return caption, {"errors_composition": fig_a,
                         "errors_counts": fig_b,
                         "errors_wrongkey_distance": fig_c}, datasets

    # ------------------------------------------------------------------
    # Finger confusion matrices per condition

    def _add_confusion_tab(self, trials: List[dict], events: List[dict]) -> None:
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        confusion_df = compute_finger_confusion(events)
        self._register_datasets({"confusion_long": confusion_df})
        data = {c: confusion_matrix(confusion_df, c) for c in CONDITIONS}

        def draw_matrix(ax, c: str, normalized: bool, vmax: float, cell_fontsize: float) -> None:
            m = np.array(data[c]["matrix"], dtype=float)
            if normalized:
                sums = m.sum(axis=1, keepdims=True)
                m = np.divide(m * 100, sums, out=np.zeros_like(m), where=sums > 0)
            ax.imshow(m, cmap="Blues", vmin=0, vmax=vmax)
            for i in range(10):
                for j in range(10):
                    v = m[i][j]
                    if v >= 0.5:
                        ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=cell_fontsize,
                                color="white" if v > 0.55 * vmax else "#1a3a5c")
            ax.set_xticks(range(10), FINGER_ORDER, fontsize=7)
            ax.set_yticks(range(10), FINGER_ORDER, fontsize=7)
            ax.tick_params(length=0)
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.set_title(f"{c} ({data[c]['total']} events)", fontsize=10)
            ax.set_xlabel("actual finger", fontsize=8)

        def make_fig(normalized: bool) -> Figure:
            fig = Figure(figsize=(9.8, 3.9))
            axes = fig.subplots(1, 3)
            vmax = 100.0 if normalized else (
                max((max(max(row) for row in data[c]["matrix"]) for c in CONDITIONS), default=1) or 1
            )
            for ax, c in zip(axes, CONDITIONS):
                draw_matrix(ax, c, normalized, vmax, cell_fontsize=6.5)
            axes[0].set_ylabel("target finger", fontsize=8)
            fig.suptitle("Row-normalized % per target finger" if normalized
                         else "Event counts (shared colour scale)", fontsize=10)
            fig.tight_layout()
            return fig

        def make_paper_fig() -> Figure:
            # Paper figure: B and C only, row-normalized, roomier cells.
            fig = Figure(figsize=(8.2, 4.3))
            axes = fig.subplots(1, 2)
            for ax, c in zip(axes, ("B", "C")):
                draw_matrix(ax, c, normalized=True, vmax=100.0, cell_fontsize=7.5)
                ax.set_title(f"{cond_titles[c]} ({data[c]['total']} events)", fontsize=10)
            axes[0].set_ylabel("target finger", fontsize=8)
            fig.suptitle("Target vs actual finger, row-normalized % — "
                         "A has no target-finger cue and is therefore omitted from the paper figure.",
                         fontsize=9)
            fig.tight_layout()
            return fig

        fig_norm, fig_counts, fig_paper = make_fig(True), make_fig(False), make_paper_fig()
        self._figures["confusion_paper"] = fig_paper
        self._figures["confusion_rownorm"] = fig_norm
        self._figures["confusion_counts"] = fig_counts

        lines = ["<h3>Finger confusion per condition</h3>",
                 "Rows = target finger, columns = final verified actual finger, physical order "
                 "L5→R5 both ways (cross-hand errors are possible and land outside the hand's block). "
                 "Unresolved events are not in the matrix; nonzero unresolved counts per target finger "
                 "are listed below. <b>Condition A has no target-finger cue</b> — its matrix shows "
                 "unguided finger-use behaviour and must not be read like B/C."]
        for c in CONDITIONS:
            top = top_confusion(data[c]["matrix"])
            if top:
                lines.append(f"<b>{cond_titles[c]}</b>: most frequent confusion — target {top['target']} "
                             f"executed as {top['actual']} (n={top['n']}).")
            unres = {f: n for f, n in data[c]["unresolved"].items() if n}
            if unres:
                lines.append(f"{c} unresolved by target finger: "
                             + ", ".join(f"{f}: {n}" for f, n in unres.items()))
        pair_lines = []
        for label, pairs in (("ring–little", [("L4", "L5"), ("R4", "R5")]),
                             ("middle–ring", [("L3", "L4"), ("R3", "R4")])):
            b_n = sum(confusion_pair_count(data["B"]["matrix"], *p) for p in pairs)
            c_n = sum(confusion_pair_count(data["C"]["matrix"], *p) for p in pairs)
            pair_lines.append(f"{label} substitutions: B {b_n} vs C {c_n}")
        lines.append("<b>B vs C neighbouring-finger substitutions</b> (both hands, both directions): "
                     + "; ".join(pair_lines) + ".")
        lines.append("Single-participant patterns — not generalisable finger-physiology claims. "
                     "Descriptive only — group-level inference is reported separately.")
        caption_html = "".join(f"<p>{line}</p>" for line in lines)

        # Custom tab: view selector over three prebuilt canvases; the
        # row-normalized view is the default.
        content = QWidget()
        layout = QVBoxLayout(content)
        caption = QLabel(caption_html)
        caption.setWordWrap(True)
        caption.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(caption)
        toggle_row = QHBoxLayout()
        toggle_row.addWidget(QLabel("View:"))
        toggle = QComboBox()
        toggle.addItems(["Row-normalized %", "Counts", "Paper view (B & C only)"])
        toggle_row.addWidget(toggle)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)
        stack = QStackedWidget()
        max_h = 0
        for fig in (fig_norm, fig_counts, fig_paper):
            canvas = ScrollFriendlyCanvas(fig)
            h = int(fig.get_figheight() * 100)
            max_h = max(max_h, h)
            canvas.setFixedHeight(h)
            canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            stack.addWidget(canvas)
        stack.setFixedHeight(max_h + 10)
        toggle.currentIndexChanged.connect(stack.setCurrentIndex)
        layout.addWidget(stack)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, "Confusion")

    # ------------------------------------------------------------------
    # Fingers

    def _build_fingers(self, events: List[dict]):
        fig = Figure(figsize=(9, 6.4))
        (ax1, ax2) = fig.subplots(2, 1)
        caption_notes = []
        width = 0.25
        x = np.arange(len(FINGER_ORDER))
        per_target_rows = []
        for i, c in enumerate(CONDITIONS):
            evs = [e for e in events if e["condition"] == c and not e["timed_out"]]
            fa_vals, rt_vals, ns = [], [], []
            for f in FINGER_ORDER:
                fe = [e for e in evs if e["target_finger"] == f]
                ns.append(len(fe))
                judged = [e for e in fe if e["finger_correct"] is not None]
                fa_vals.append(
                    100 * sum(1 for e in judged if e["key_correct"] and e["finger_correct"]) / len(judged)
                    if judged else np.nan
                )
                rts = [e["rt_s"] for e in fe if e["rt_s"] is not None]
                rt_vals.append(1000 * np.mean(rts) if rts else np.nan)
                per_target_rows.append({
                    "condition": c, "target_finger": f, "n": len(fe), "n_judged": len(judged),
                    "fa": fa_vals[-1] / 100 if not np.isnan(fa_vals[-1]) else np.nan,
                    "rt_s": rt_vals[-1] / 1000 if not np.isnan(rt_vals[-1]) else np.nan,
                })
            ax1.bar(x + (i - 1) * width, fa_vals, width, color=CONDITION_COLORS[c], label=c)
            ax2.bar(x + (i - 1) * width, rt_vals, width, color=CONDITION_COLORS[c], label=c)
            weakest = min(
                ((f, v) for f, v in zip(FINGER_ORDER, fa_vals) if not np.isnan(v)),
                key=lambda p: p[1], default=None,
            )
            if weakest:
                caption_notes.append(f"condition {c}: weakest finger {weakest[0]} ({weakest[1]:.0f}%)")
        ax1.set_xticks(x, FINGER_ORDER)
        ax1.set_ylabel("FA main (%)")
        ax1.set_title("Finger accuracy per target finger (physical keyboard order)", fontsize=10)
        ax1.legend(fontsize=8)
        ax2.set_xticks(x, FINGER_ORDER)
        ax2.set_ylabel("RT (ms)")
        ax2.set_title("Mean reaction time per target finger", fontsize=10)
        ax2.legend(fontsize=8)
        zero_based_ylim(ax2)
        fig.tight_layout()

        # B vs C per-finger RT distributions (boxplots; correct-key events).
        fig2 = Figure(figsize=(9, 3.8))
        bx = fig2.subplots(1, 1)
        small_n_notes = []
        for i, c in enumerate(("B", "C")):
            offset = -0.18 if c == "B" else 0.18
            for fi, f in enumerate(FINGER_ORDER):
                rts = [e["rt_s"] * 1000 for e in events
                       if e["condition"] == c and e["target_finger"] == f
                       and e["key_correct"] and e["rt_s"] is not None]
                pos = fi + offset
                if len(rts) >= 3:
                    box = bx.boxplot([rts], positions=[pos], widths=0.3, patch_artist=True,
                                     showfliers=False)
                    box["boxes"][0].set_facecolor(CONDITION_COLORS[c])
                    box["boxes"][0].set_alpha(0.55)
                else:
                    # Too few for a trustworthy box - show raw points instead.
                    bx.scatter([pos] * len(rts), rts, s=18, color=CONDITION_COLORS[c], alpha=0.8)
                    if rts:
                        small_n_notes.append(f"{c}/{f} (n={len(rts)})")
                # y in axes fraction, so the count stays pinned to the
                # bottom of the panel after the axis range is fixed below.
                bx.text(pos, 0.01, f"{len(rts)}", ha="center", va="bottom", fontsize=6,
                        color="#555555", transform=bx.get_xaxis_transform())
        bx.set_xticks(range(len(FINGER_ORDER)), FINGER_ORDER)
        bx.set_ylabel("RT (ms)")
        bx.set_title("Per-finger RT distributions, B (blue) vs C (orange) - correct-key events; "
                     "n under each box, fingers with n<3 drawn as raw points", fontsize=9)
        bx.legend(handles=[Patch(facecolor=CONDITION_COLORS["B"], alpha=0.55, label="B"),
                           Patch(facecolor=CONDITION_COLORS["C"], alpha=0.55, label="C")], fontsize=8)
        zero_based_ylim(bx)
        fig2.tight_layout()
        if small_n_notes:
            caption_notes.append("small n (points, not boxes): " + ", ".join(small_n_notes))

        # Homologous L/R merge (finger ID 1 thumb ... 5 little) - the cell
        # grid the group window's Fingers tab and the Condition x Finger
        # ANOVA are defined on. Ten cells of ~9 events each are thin; five
        # merged cells roughly double the events behind every bar, at the
        # cost of assuming the two hands behave alike for a given digit.
        pf = pa.per_finger_metrics(events)
        pf_bc = pf[pf["condition"].isin(pa.GUIDANCE_CONDITIONS)]
        fig3 = Figure(figsize=(9, 6.6))
        hx_fa, hx_rt = fig3.subplots(2, 1)
        hx = np.arange(len(pa.FINGER_IDS))
        hwidth = 0.32
        offset_mid = (len(pa.GUIDANCE_CONDITIONS) - 1) / 2
        homolog_notes = []
        for i, c in enumerate(pa.GUIDANCE_CONDITIONS):
            sub = pf_bc[pf_bc["condition"] == c].set_index("finger_id").reindex(pa.FINGER_IDS)
            offset = (i - offset_mid) * hwidth
            hx_fa.bar(hx + offset, sub["fa"].to_numpy(dtype=float) * 100, hwidth,
                      color=CONDITION_COLORS[c], label=self._condition_title(self._trials, c))
            hx_rt.bar(hx + offset, sub["rt_s"].to_numpy(dtype=float) * 1000, hwidth,
                      color=CONDITION_COLORS[c], label=f"{c} — all responded")
            # The ANOVA's "correct complete action" RT, drawn as a marker on
            # the same bar so the two definitions can be compared at a glance.
            hx_rt.scatter(hx + offset, sub["rt_complete_s"].to_numpy(dtype=float) * 1000,
                          s=260, marker="_", color="black", linewidths=1.8, zorder=3,
                          label="key- and finger-correct only" if i == 0 else None)
            if sub["fa"].notna().any():
                weakest_id = int(sub["fa"].idxmin())
                homolog_notes.append(
                    f"{c}: weakest homologous finger {fc.finger_label(weakest_id)} "
                    f"({_fmt(sub.loc[weakest_id, 'fa'] * 100, 0, '%')})")
        counts = pf_bc.groupby("finger_id")[["n_left", "n_right"]].sum()
        htick_labels = [
            f"{fc.finger_label(fid)}\nL {int(counts.loc[fid, 'n_left']) if fid in counts.index else 0}"
            f" / R {int(counts.loc[fid, 'n_right']) if fid in counts.index else 0}"
            for fid in pa.FINGER_IDS
        ]
        for ax, ylabel, title in (
                (hx_fa, "Main FA (%)", "Finger accuracy by homologous finger ID (B/C)"),
                (hx_rt, "RT (ms)", "Reaction time by homologous finger ID (B/C)")):
            ax.set_xticks(hx, htick_labels, fontsize=8)
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=7)
        zero_based_ylim(hx_rt)
        fig3.tight_layout()

        caption = (
            "<h3>Per-finger profiles</h3>"
            "<p><b>Figures 1–2 — by physical target finger.</b> The report's main-figure view, computed "
            "within this participant: accuracy and RT stratified by target finger, in physical keyboard "
            "order (left pinky → right pinky), grouped by condition. Passive mechanical and neuromuscular "
            "coupling differ across digits, so a modality effect concentrated in the ring/little fingers "
            "shows up here while staying hidden in the overall mean. Bars use responded events only; "
            "unresolved finger verdicts count as incorrect.</p>"
            "<p>" + "; ".join(caption_notes) + "</p>"
            "<p><b>Figure 3 — by homologous finger ID (1 = thumb … 5 = little), B/C only.</b> The same "
            "events with the two hands pooled by digit — the cell grid the group analysis and its "
            "Condition × Finger ANOVA are defined on, shown here for one participant so the individual "
            "profile and the group figure are directly comparable. The L/R counts under each tick state "
            "exactly what was merged, and the black dashes mark the stricter "
            "key-<i>and</i>-finger-correct RT (the ANOVA's \"correct complete action\" definition) beside "
            "the all-responded bars. Condition A carries no target-finger cue and is not a per-finger "
            "baseline, so it is omitted here while remaining in Figures 1–2.</p>"
            "<p>" + "; ".join(homolog_notes) + "</p>"
        )
        datasets = {
            "fingers_by_target_finger": pd.DataFrame(per_target_rows),
            "fingers_homologous_cells": pf,
        }
        return caption, {"fingers_accuracy": fig,
                         "fingers_rt_boxplot": fig2,
                         "fingers_homologous": fig3}, datasets

    # ------------------------------------------------------------------
    # Finger benefit
    #
    # The single-participant half of the Group Analysis window's Finger
    # Benefit tab. The group version answers "does the haptic cue
    # compensate the slow digits" with across-participant tests that need
    # N >= 5; the question underneath it, however, is asked over ONE
    # participant's five finger cells, so the descriptive quantities -
    # the per-finger benefit, its three de-coupled correlations, and the
    # across-finger dispersion - are computable here and are what this
    # tab shows. No test is run and none is implied.

    def _build_finger_benefit(self, events: List[dict]):
        metric = "rt_complete_s"  # the RT the group RM-ANOVA leads with
        baseline, cued = fc.BASELINE_CONDITION, fc.CUED_CONDITION
        pf = pa.per_finger_metrics(events)
        pairs, dropped = fc.paired_finger_cells(pf, metric, baseline, cued)
        dropped_note = fc.describe_dropped(dropped)

        if pairs.empty:
            fig = Figure(figsize=(9, 2.4))
            ax = fig.subplots(1, 1)
            ax.axis("off")
            ax.text(0.5, 0.5, "No complete B/C × 5-finger grid for this participant",
                    ha="center", va="center", fontsize=11, color="#666666")
            caption = (
                "<h3>Per-finger benefit of the haptic cue</h3>"
                "<p>This participant has no complete visual (B) / haptic (C) × five-finger grid on "
                "the key- and finger-correct reaction time, so no benefit can be formed. "
                + (f"{dropped_note}</p>" if dropped_note else
                   "Cells are missing where a finger produced no key- and finger-correct response "
                   "in one of the two conditions.</p>"))
            return caption, {"finger_benefit": fig}, {}

        # Figure 1: the benefit per finger, and the two cells behind it.
        fig1 = Figure(figsize=(9, 6.2))
        bx_cells, bx_benefit = fig1.subplots(2, 1)
        x = np.arange(len(pa.FINGER_IDS))
        by_finger = pairs.set_index("finger_id").reindex(pa.FINGER_IDS)
        for i, (col, cond) in enumerate((("baseline", baseline), ("cued", cued))):
            bx_cells.bar(x + (i - 0.5) * 0.34, by_finger[col].to_numpy(dtype=float) * 1000, 0.34,
                         color=CONDITION_COLORS[cond],
                         label=self._condition_title(self._trials, cond))
        bx_cells.set_xticks(x, [fc.finger_label(f) for f in pa.FINGER_IDS], fontsize=8)
        bx_cells.set_ylabel("RT (ms)")
        bx_cells.set_title("Key- and finger-correct RT per homologous finger", fontsize=10)
        bx_cells.legend(fontsize=7)

        benefits_ms = by_finger["benefit"].to_numpy(dtype=float) * 1000
        colors = ["#4a9d5b" if b > 0 else "#c94f4f" for b in benefits_ms]
        bars = bx_benefit.bar(x, benefits_ms, 0.55, color=colors)
        for bar, v in zip(bars, benefits_ms):
            if np.isfinite(v):
                bx_benefit.text(bar.get_x() + bar.get_width() / 2, v,
                                f"{v:+.0f}", ha="center",
                                va="bottom" if v >= 0 else "top", fontsize=8)
        bx_benefit.axhline(0, color="black", linewidth=0.9)
        mean_benefit = float(np.nanmean(benefits_ms)) if np.isfinite(benefits_ms).any() else np.nan
        if np.isfinite(mean_benefit):
            bx_benefit.axhline(mean_benefit, color="#555555", linestyle="--", linewidth=1,
                               label=f"mean across fingers ({mean_benefit:+.0f} ms)")
            bx_benefit.legend(fontsize=7)
        bx_benefit.set_xticks(x, [fc.finger_label(f) for f in pa.FINGER_IDS], fontsize=8)
        bx_benefit.set_ylabel("benefit (ms, positive = C faster)")
        bx_benefit.set_title(f"Haptic benefit per finger ({cued} vs {baseline})", fontsize=10)
        fig1.tight_layout()

        # Figure 2: benefit vs baseline under the three estimators. The
        # naive panel is the coupled one and is labelled as such.
        estimators = fb.descriptive_estimators(events, pairs, metric, baseline, cued)
        fig2 = Figure(figsize=(10.2, 3.7))
        axes = fig2.subplots(1, len(estimators))
        est_lines = []
        for ax, entry in zip(np.atleast_1d(axes), estimators):
            points = entry["points"]
            per_p = entry["per_participant"]
            r = float(per_p["r"].iloc[0]) if len(per_p) else np.nan
            slope = float(per_p["slope"].iloc[0]) if len(per_p) else np.nan
            if len(points):
                xs = points["x"].to_numpy(dtype=float) * 1000
                ys = points["y"].to_numpy(dtype=float) * 1000
                ax.scatter(xs, ys, s=40, color=CONDITION_COLORS[cued], zorder=2)
                for xi, yi, fid in zip(xs, ys, points["finger_id"]):
                    ax.annotate(str(int(fid)), (xi, yi), fontsize=7,
                                textcoords="offset points", xytext=(4, 3))
                if np.isfinite(slope) and xs.size >= 2:
                    line_x = np.array([xs.min(), xs.max()])
                    intercept = ys.mean() - slope * xs.mean()
                    ax.plot(line_x, slope * line_x + intercept, "--", color="black", linewidth=1)
            ax.axhline(0, color="#999999", linewidth=0.8)
            ax.set_xlabel(entry["x_label"] + " (ms)", fontsize=8)
            ax.set_ylabel("benefit (ms)", fontsize=8)
            ax.set_title(f"{entry['label'].split('—')[0].strip()}\nr = {_fmt(r, 2)}, "
                         f"slope = {_fmt(slope, 2)}", fontsize=9)
            est_lines.append(
                f"<b>{entry['label']}</b>: r = {_fmt(r, 2)}, slope = {_fmt(slope, 2)} "
                f"({entry['n_points']} finger cells). {entry['caveat']}")
        fig2.tight_layout()

        # Figure 3: across-finger dispersion, the equalisation question.
        dispersion = fq.dispersion_table(events, metric, [baseline, cued])
        fig3 = Figure(figsize=(9, 3.4))
        dx = fig3.subplots(1, 1)
        measures = [("sd_raw", "SD"), ("sd_corrected", "SD, noise-corrected"),
                    ("range", "Range"), ("cv_raw", "CV (×1000, scale-free)")]
        disp_by_c = dispersion.set_index("condition") if len(dispersion) else dispersion
        dxs = np.arange(len(measures))
        disp_lines = []
        for i, cond in enumerate((baseline, cued)):
            if not len(dispersion) or cond not in disp_by_c.index:
                continue
            row = disp_by_c.loc[cond]
            vals = [row["sd_raw"] * 1000, row["sd_corrected"] * 1000,
                    row["range"] * 1000, row["cv_raw"] * 1000]
            dx.bar(dxs + (i - 0.5) * 0.34, vals, 0.34, color=CONDITION_COLORS[cond],
                   label=self._condition_title(self._trials, cond))
            disp_lines.append(
                f"{cond}: mean {_fmt(row['mean'] * 1000, 0, ' ms')}, SD {_fmt(row['sd_raw'] * 1000, 1)}, "
                f"noise-corrected SD {_fmt(row['sd_corrected'] * 1000, 1)}, "
                f"CV {_fmt(row['cv_raw'], 3)}")
        dx.set_xticks(dxs, [label for _, label in measures], fontsize=8)
        dx.set_ylabel("ms (CV scaled ×1000)")
        dx.set_title("Spread of RT across the five fingers — is the cued condition more even?",
                     fontsize=10)
        dx.legend(fontsize=7)
        fig3.tight_layout()

        n_better = int(np.sum(benefits_ms > 0)) if np.isfinite(benefits_ms).any() else 0
        n_defined = int(np.sum(np.isfinite(benefits_ms)))
        caption = (
            "<h3>Per-finger benefit of the haptic cue</h3>"
            f"<p>Metric: key- <i>and</i> finger-correct reaction time, {cued} vs {baseline}; positive "
            "benefit means the haptic condition was faster on that digit. "
            f"{n_better} of {n_defined} fingers benefited, mean "
            f"{_fmt_signed(mean_benefit, 0, ' ms')} across fingers.</p>"
            + (f"<p>{dropped_note}</p>" if dropped_note else "")
            + "<p><b>Does the cue compensate the slow digits, or speed every digit up alike?</b> "
            "Asked as the correlation between a finger's benefit and its baseline RT — but the "
            "benefit <i>contains</i> the baseline, so their shared sampling noise manufactures a "
            "positive correlation even under a perfectly uniform speed-up. The three panels are the "
            "same question with progressively less of that artefact; the gap between the naive and "
            "the split-half value is the size of the artefact, and is more informative than either "
            "number alone. Point labels are finger IDs.</p>"
            "<p>" + "<br>".join(est_lines) + "</p>"
            "<p><b>Evenness across fingers.</b> A cue that genuinely equalises the hand should shrink "
            "the spread across digits by more than a proportional speed-up would. The noise-corrected "
            "SD removes the part of the spread that is just each cell's own sampling error, and the "
            "CV is scale-free — a uniform proportional speed-up leaves it unchanged, so only a fall in "
            "the CV counts as evening-out.</p>"
            "<p>" + "<br>".join(disp_lines) + "</p>"
            "<p><b>Descriptive only, and thin.</b> Every number on this tab rests on five finger cells "
            "from one participant, so a single noisy digit can turn a correlation around. The group "
            "window runs the same three estimators with participants as the independent unit and is "
            "the only place a compensation or equalisation claim is tested.</p>"
        )
        datasets = {
            "finger_benefit_cells": pairs,
            "finger_benefit_estimators": pd.concat(
                [e["points"].assign(estimator=e["key"]) for e in estimators if len(e["points"])],
                ignore_index=True) if any(len(e["points"]) for e in estimators) else pd.DataFrame(),
            "finger_benefit_dispersion": dispersion,
        }
        return caption, {"finger_benefit": fig1,
                         "finger_benefit_estimators": fig2,
                         "finger_benefit_dispersion": fig3}, datasets

    # ------------------------------------------------------------------
    # Timing

    def _build_timing(self, events: List[dict]):
        fig = Figure(figsize=(9, 3.8))
        ax1, ax2 = fig.subplots(1, 2)
        stats_lines = []
        summary_rows = []
        rt_sets, labels, colors = [], [], []
        for c in CONDITIONS:
            rts = [e["rt_s"] * 1000 for e in events
                   if e["condition"] == c and e["key_correct"] and e["rt_s"] is not None]
            if not rts:
                continue
            rt_sets.append(rts)
            labels.append(c)
            colors.append(CONDITION_COLORS[c])
            ax1.hist(rts, bins=25, alpha=0.45, color=CONDITION_COLORS[c], label=c)
            stats_lines.append(
                f"{c}: median {np.median(rts):.0f} ms, mean {np.mean(rts):.0f} ms, "
                f"SD {np.std(rts):.0f} ms, p95 {np.percentile(rts, 95):.0f} ms (n={len(rts)})"
            )
            summary_rows.append({
                "condition": c, "n_events": len(rts),
                "median_ms": float(np.median(rts)), "mean_ms": float(np.mean(rts)),
                "sd_ms": float(np.std(rts)), "min_ms": float(np.min(rts)),
                "p95_ms": float(np.percentile(rts, 95)), "max_ms": float(np.max(rts)),
            })
        ax1.set_xlabel("RT (ms), correct-key events")
        ax1.set_ylabel("events")
        ax1.set_title("RT distribution by condition", fontsize=10)
        ax1.legend(fontsize=8)
        if rt_sets:
            box = ax2.boxplot(rt_sets, tick_labels=labels, patch_artist=True, showfliers=True)
            for patch, color in zip(box["boxes"], colors):
                patch.set_facecolor(color)
                patch.set_alpha(0.5)
        ax2.set_ylabel("RT (ms)")
        ax2.set_title("RT spread by condition", fontsize=10)
        zero_based_ylim(ax2)
        fig.tight_layout()
        caption = (
            "<h3>Reaction-time distributions</h3>"
            "<p>Correct-key events only, pooled over all levels and sequences of each condition. The "
            "histogram shows shape (a long right tail usually means hesitation events rather than slow "
            "motor execution); the box plot shows spread and outliers. A condition can match on mean RT "
            "yet differ in consistency — SD and p95 carry that.</p>"
            "<p>" + "<br>".join(stats_lines) + "</p>"
        )
        return caption, {"timing_rt_distributions": fig}, {
            "timing_rt_summary": pd.DataFrame(summary_rows),
        }

    # ------------------------------------------------------------------
    # Quality

    def _build_quality(self, trials: List[dict], all_events: List[dict]):
        fig = Figure(figsize=(9, 3.4))
        ax = fig.subplots(1, 1)
        cats = ["Timeouts", "Wrong key", "Extra presses (QC)", "Manual corrections",
                "Excluded carry-over", "Suspected carry-over"]
        width = 0.25
        x = np.arange(len(cats))
        lines = []
        for i, c in enumerate(CONDITIONS):
            ts = [t for t in trials if t["condition"] == c]
            vals = [
                sum(t["misses"] for t in ts),
                sum(t["wrong_key"] for t in ts),
                sum(t["qc_extra_presses"] or 0 for t in ts),
                sum(t["manual_corrections"] for t in ts),
                sum(t["excluded_carryover"] or 0 for t in ts),
                sum(t["suspected_carryover"] or 0 for t in ts),
            ]
            ax.bar(x + (i - 1) * width, vals, width, color=CONDITION_COLORS[c], label=c)
            unresolved = _mean([t["unresolved_rate"] for t in ts if t["analyzed"]])
            ambiguous = _mean([t["ambiguous_rate"] for t in ts if t["analyzed"]])
            borderline = sum(t["borderline_events"] for t in ts if t["analyzed"])
            sync_methods = {t["sync_method"] for t in ts}
            lines.append(
                f"<b>{c}</b>: unresolved {_fmt_pct(unresolved)}, ambiguous {_fmt_pct(ambiguous)}, "
                f"borderline events {borderline}, sync methods {{{', '.join(sorted(sync_methods))}}}"
            )
        ax.set_xticks(x, cats, fontsize=7.5)
        ax.set_ylabel("count (all trials of the condition)")
        ax.set_title("Error and audit counts by condition", fontsize=10)
        ax.legend(fontsize=8)
        fig.tight_layout()

        # Session-level audit row - the same counters the group window
        # tabulates per participant, so this participant's line can be
        # read against the group table without recomputation.
        audit = pa.quality_summary(trials, all_events)
        audit_html = ""
        if len(audit):
            r = audit.iloc[0]
            audit_html = (
                "<table border='0' cellspacing='0' cellpadding='4'>"
                "<tr><th align='left'>Session totals</th><th>Trials (analyzed)</th>"
                "<th>Valid / total events</th><th>Excluded carry-over</th>"
                "<th>Suspected unresolved</th><th>Manual corrections</th>"
                "<th>Unresolved</th><th>Ambiguous</th><th>Borderline</th>"
                "<th>Extra presses (QC)</th><th>Sync</th></tr>"
                f"<tr><td><b>{r['participant']}</b></td>"
                f"<td align='center'>{int(r['n_trials'])} ({int(r['n_analyzed'])})</td>"
                f"<td align='center'>{int(r['n_valid_events'])} / {int(r['n_events'])}</td>"
                f"<td align='center'>{int(r['excluded_carryover'])}</td>"
                f"<td align='center'>{int(r['suspected_carryover'])}</td>"
                f"<td align='center'>{int(r['manual_corrections'])}</td>"
                f"<td align='center'>{_fmt(r['unresolved_rate'] * 100, 1, '%')}</td>"
                f"<td align='center'>{_fmt(r['ambiguous_rate'] * 100, 1, '%')}</td>"
                f"<td align='center'>{int(r['borderline_events'])}</td>"
                f"<td align='center'>{int(r['qc_extra_presses'])}</td>"
                f"<td align='center'>{r['sync_methods']}</td></tr></table>")

        # Threshold sensitivity: how much of this participant's finger
        # accuracy is an artefact of where the detection threshold sits.
        theta = pa.threshold_sensitivity(trials, all_events)
        theta_html = ""
        fig_theta = None
        if not theta.empty:
            thetas = sorted(theta["theta"].unique())
            fig_theta = Figure(figsize=(9, 3.2))
            tx = fig_theta.subplots(1, 1)
            t_tab = ["<table border='0' cellspacing='0' cellpadding='3'>"
                     "<tr><th align='left'>FA by detection threshold θ</th>"
                     + "".join(f"<th>{th:.2f}</th>" for th in thetas) + "</tr>"]
            for c in pa.GUIDANCE_CONDITIONS:
                sub = theta[theta["condition"] == c].set_index("theta").reindex(thetas)
                if sub["fa"].notna().any():
                    tx.plot(thetas, sub["fa"].to_numpy(dtype=float) * 100, "o-",
                            color=CONDITION_COLORS[c], label=self._condition_title(trials, c))
                cells = "".join(f"<td align='center'>{_fmt(v * 100, 0, '%')}</td>"
                                for v in sub["fa"])
                t_tab.append(f"<tr><td><b>{c}</b></td>{cells}</tr>")
            t_tab.append("</table>")
            tx.axvline(0.40, color="#999999", linestyle="--", linewidth=1)
            tx.annotate("θ = 0.40 (analysis value)", (0.40, tx.get_ylim()[0]),
                        fontsize=7, color="#666666", xytext=(4, 4),
                        textcoords="offset points")
            tx.set_xlabel("finger-detection probability threshold θ")
            tx.set_ylabel("FA (%)")
            tx.set_title("Threshold sensitivity of finger accuracy (B/C, analyzed trials)",
                         fontsize=10)
            tx.legend(fontsize=8)
            fig_theta.tight_layout()

            final_rows = []
            for c in pa.GUIDANCE_CONDITIONS:
                ts = [t for t in trials if t["condition"] == c and t["analyzed"]]
                final_rows.append(
                    f"<tr><td><b>{c}</b></td><td align='center'>"
                    f"{_fmt((_mean([t['fa_main'] for t in ts]) or np.nan) * 100, 1, '%')}"
                    "</td></tr>")
            theta_html = (
                "<p><b>Threshold sensitivity.</b> Every θ, including the 0.40 the analysis runs at, is "
                "recomputed from the same unedited event-level target-finger probabilities, so the curve "
                "shows how much of this participant's finger accuracy is a threshold choice rather than a "
                "behavioural difference — a B/C gap that survives across θ is not an artefact of where the "
                "line was drawn. The final reviewed Main FA is a different measurement stream (it carries "
                "the hand-verified corrections) and is therefore listed separately, never plotted on the "
                "curve.</p><p>" + "".join(t_tab) + "<br>"
                "<table border='0' cellspacing='0' cellpadding='3'>"
                "<tr><th align='left'>Final reviewed Main FA (separate reference)</th><th>Mean</th></tr>"
                + "".join(final_rows) + "</table></p>")
        else:
            theta_html = ("<p><b>Threshold sensitivity:</b> not available — no video-analyzed B/C trial "
                          "carries event-level target-finger probabilities.</p>")

        caption = (
            "<h3>Data quality & audit</h3>"
            "<p>Not an outcome — the trust context for every other tab. High unresolved/ambiguous rates "
            "mean the camera evidence is weak for those trials (check occlusion or sync); borderline events "
            "are the ones whose finger verdict sits within ±0.05 of the θ=0.40 threshold and were the "
            "manual-audit priority; sync methods show which trials rest on an auto-detected vs "
            "manually confirmed vs missing alignment. Excluded carry-over events were manually confirmed "
            "and are already out of every other tab; suspected carry-over still awaits a verdict and is "
            "still counted as a real response; extra presses are unmatched raw MIDI presses (QC only).</p>"
            "<p>" + "<br>".join(lines) + "</p>"
            + (f"<p>{audit_html}</p>" if audit_html else "")
            + theta_html
        )
        figures = {"quality_audit": fig}
        if fig_theta is not None:
            figures["quality_threshold_sensitivity"] = fig_theta
        datasets = {
            "quality_audit": audit,
            "quality_threshold_sensitivity": theta,
        }
        return caption, figures, datasets
