"""Group Analysis (multi-participant) - launcher section 7.

Select any subset of exported Main User Study participants and get the
group-level picture over the same within-subject design the
single-participant window shows: condition and difficulty comparisons,
paired condition contrasts, a descriptive speed-accuracy trade-off,
learning/order trends, Condition-A free-fingering strategy,
event-outcome composition, homologous per-finger profiles, the
Condition x Finger repeated-measures ANOVA, the
finger-benefit (compensation / equalisation / weakest-finger) analyses,
and a data-quality audit.

The Finger Benefit tab is rendered by .finger_benefit_tab and computed
by app.finger_benefit, app.finger_equalisation and app.finger_weakest -
one module per question, none of them importing the others.

Core aggregation lives in app.group_analysis; the difficulty-aligned
progression calculation and figures live in the clearly separated
app.session_progression and app.session_progression_figures modules. All read
the reviewed-and-exported <participant>_{trials,events}.csv files (the
Participant Export schema), using the same final verdicts and validity rules as
the single-participant window.

Design rules enforced here:
  - the independent unit of every mean, interval and test is the
    PARTICIPANT (thin lines/dots per participant, group centre on top);
  - the comparable guidance conditions B/C are compared within-subject
    (paired t/Wilcoxon contrasts and the two-way repeated-measures
    ANOVA over Condition x Finger, all gated by complete-case N and
    labelled exploratory at small N - at N=1 no inferential test runs
    at all);
  - missing cells stay missing (no zero-fill), carry-over-invalidated
    events stay out of outcomes, QC counters stay out of outcomes;
  - every Analyse click recomputes from disk for exactly the current
    selection - no cached results survive a selection change.

Export ("Export figures + data", participant-window pattern) covers
EVERY tab: each figure as 300 dpi PNG + SVG, every tidy table behind it
as CSV, and a _manifest.csv naming the files and the participants they
came from. Registration is done by _add_tab, not by the individual
_build_ methods, so a new tab cannot ship without its output; empty
tables (e.g. "missing cells" when nothing is missing) are skipped rather
than written as zero-row files. test-script/test_group_export.py asserts
that every tab contributes at least one figure and one table.
"""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.patches import Patch, Rectangle
from PySide6.QtCore import Qt
from scipy import stats as sstats
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import group_analysis as ga
from .. import group_tradeoff as gt
from .. import session_progression as sp
from .. import session_progression_figures as sp_figures
from ..pilot_study import DATA_DIR as STUDY_DATA_DIR
from ..participant_analysis import (
    CAT_CK_WF,
    CATEGORIES,
    compute_error_breakdown,
    compute_wrong_key_distance,
    top_confusion,
    wrong_key_stats,
)
from . import condition_a_strategy_tab
from . import finger_benefit_tab
from .analysis_export import export_analysis
from .progress_task import run_with_progress
from .wrapping_tabs import WrappingTabWidget
from .participant_analysis_window import (
    CATEGORY_COLORS,
    CONDITION_COLORS,
    ScrollFriendlyCanvas,
)
from .stats_format import fmt, fmt_p

CONDITIONS = ga.CONDITIONS
LEVELS = ga.LEVELS
LEVEL_DISPLAY_LABELS = ga.LEVEL_DISPLAY_LABELS
LEVEL_TICK_LABELS = ga.LEVEL_TICK_LABELS
PARTICIPANT_LINE = "#9a9a9a"
# Outlines the confusion cells the theta rule forgave, matching the amber
# the per-trial matrix uses for the same events (app/gui/quiz_detail_window.py).
NEAR_TIE_COLOR = "#e0a020"


# Formatting lives in .stats_format so the "&lt;" rule (a bare "<" in a
# RichText caption opens a tag and Qt eats the rest of the line) has one
# home shared with the other analysis tabs.
_fmt = fmt
_fmt_p = fmt_p


class GroupAnalysisWindow(QMainWindow):
    def __init__(self, cfg=None):
        super().__init__()
        self.setWindowTitle("Group Analysis — Multi-Participant")
        self.resize(1280, 860)

        # --- left: participant selection ---------------------------------
        self.participant_list = QListWidget()
        self.participant_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_participants)
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._set_all_checks(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._set_all_checks(False))
        analyze_btn = QPushButton("Analyse selected participants")
        analyze_btn.clicked.connect(self._analyze)
        self.save_figs_btn = QPushButton("Export figures + data")
        self.save_figs_btn.setToolTip(
            "Write the group trade-off figures as 300 dpi PNG + SVG plus the underlying "
            "tidy CSVs to data/MainUserStudy/group_figures/ (same export pattern as the "
            "single-participant window)."
        )
        self.save_figs_btn.setEnabled(False)
        self.save_figs_btn.clicked.connect(self._save_figures)

        left = QWidget()
        left.setFixedWidth(300)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Participants (data/MainUserStudy):"))
        left_layout.addWidget(self.participant_list, 1)
        row = QHBoxLayout()
        row.addWidget(refresh_btn)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        left_layout.addLayout(row)
        left_layout.addWidget(analyze_btn)
        left_layout.addWidget(self.save_figs_btn)

        # --- right: header + tabs -----------------------------------------
        self.header_label = QLabel("Included in analysis: none (N = 0)")
        header_font = self.header_label.font()
        header_font.setBold(True)
        self.header_label.setFont(header_font)
        self.status_label = QLabel(
            "Tick the participants to include, then click “Analyse selected participants”.")
        self.status_label.setWordWrap(True)
        self.tabs = WrappingTabWidget()

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.header_label)
        right_layout.addWidget(self.status_label)
        right_layout.addWidget(self.tabs, 1)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(left)
        layout.addWidget(right, 1)
        self.setCentralWidget(central)

        self._refresh_participants()

    # ------------------------------------------------------------------
    # Selection handling

    def _refresh_participants(self) -> None:
        """Re-scan the study folder; keep the checked state of
        participants that are still present and ready."""
        previously_checked = {
            self.participant_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.participant_list.count())
            if self.participant_list.item(i).checkState() == Qt.CheckState.Checked
        }
        self.participant_list.clear()
        for status in ga.scan_participants():
            item = QListWidgetItem(f"{status.participant} — {status.summary()}")
            item.setData(Qt.ItemDataRole.UserRole, status.participant)
            item.setToolTip(status.summary())
            if status.ready:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if status.participant in previously_checked
                    else Qt.CheckState.Unchecked)
            else:
                # Visible but not selectable - a broken participant must
                # never crash or silently join the analysis.
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.participant_list.addItem(item)

    def _set_all_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.participant_list.count()):
            item = self.participant_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(state)

    def _checked_participants(self) -> List[str]:
        return [
            self.participant_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.participant_list.count())
            if self.participant_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    # ------------------------------------------------------------------
    # Analysis (full rebuild on every click - no cross-selection caching)

    def _analyze(self) -> None:
        self.tabs.clear()
        # slug -> Figure, every registered figure is exported
        # (participant-window pattern); slug -> DataFrame for the tidy CSVs.
        self._figures: Dict[str, Figure] = {}
        self._datasets: Dict[str, object] = {}
        self.save_figs_btn.setEnabled(False)
        selection = self._checked_participants()
        if not selection:
            self.header_label.setText("Included in analysis: none (N = 0)")
            self.status_label.setText("No participants selected — tick at least one and try again.")
            return

        data = ga.load_group(selection)
        if not data.included:
            self.header_label.setText("Included in analysis: none (N = 0)")
            self.status_label.setText(
                "None of the selected participants could be loaded: "
                + "; ".join(f"{p}: {msg}" for p, msg in data.errors.items()))
            return

        self.header_label.setText(
            f"Included in analysis: {', '.join(data.included)}  (N = {data.n})")
        note = (f"N = {data.n} participant{'s' if data.n != 1 else ''}, "
                f"{len(data.trial_rows)} trials, {len(data.event_rows)} events "
                f"({len(ga.valid_events(data.event_rows))} valid after carry-over exclusion).")
        if data.errors:
            note += ("  Skipped: "
                     + "; ".join(f"{p}: {msg}" for p, msg in data.errors.items()))
        if data.n == 1:
            note += ("  With N = 1 all group results are the single participant's values; "
                     "intervals and inferential tests need more participants.")
        self.status_label.setText(note)

        # Fresh state per click.
        self._data = data
        self._pc = ga.participant_condition_metrics(data.trial_rows)
        self._cells = ga.participant_cell_metrics(data.trial_rows)
        self._cond_titles = self._condition_titles(data.trial_rows)

        if not run_with_progress(self, "Analyzing group data",
                                 [(title, self._tab_runner(title, build))
                                  for title, build in self._tab_builders()]):
            # A half-built window would export a partial set of figures as
            # though it were the whole analysis, so cancelling leaves
            # nothing rather than something misleading.
            self.tabs.clear()
            self._figures.clear()
            self._datasets.clear()
            self.status_label.setText("Analysis cancelled — nothing to show or export.")
            return
        self.save_figs_btn.setEnabled(True)

    def _tab_builders(self):
        """Every tab in display order, as (title, builder).

        This list is the analysis: _analyze() walks it and the progress
        dialog sizes itself from it, so adding an analysis means adding one
        line here and nothing else - no count to bump, no label to keep in
        step. test_group_analysis_window.py asserts the built tabs match
        this list, which is what catches a tab added anywhere else.

        A builder returns the (caption, figures, datasets) triple that
        _add_tab takes, or None if it adds its own tab - the trade-off tab
        does, because it owns display toggles that rebuild its figures.
        """
        return [
            ("Overview", self._build_overview),
            ("Condition × Difficulty", self._build_condition_difficulty),
            ("Contrasts", self._build_contrasts),
            ("Trade-off", self._add_tradeoff_tab),
            ("Learning / Order", self._build_learning),
            ("Condition A Strategy", self._build_condition_a_strategy),
            ("Errors", self._build_errors),
            ("Fingers", self._build_fingers),
            ("Finger Confusion", self._build_finger_confusion),
            ("RM-ANOVA", self._build_rm_anova),
            ("Finger Benefit", self._build_finger_benefit),
            ("Quality", self._build_quality),
        ]

    def _tab_runner(self, title: str, build):
        """Wrap a builder as a no-argument step for run_with_progress."""
        def run() -> None:
            result = build()
            if result is not None:  # None = the builder added its own tab
                self._add_tab(title, *result)
        return run

    @staticmethod
    def _condition_titles(trial_rows: List[dict]) -> Dict[str, str]:
        titles = {}
        for c in CONDITIONS:
            labels = {t.get("condition_label") for t in trial_rows
                      if t["condition"] == c and t.get("condition_label")}
            label = next(iter(sorted(labels)), "")
            titles[c] = f"{c} ({label})" if label else c
        return titles

    def _add_tab(self, title: str, caption_html: str,
                 figures: Dict[str, Figure],
                 datasets: Optional[Dict[str, object]] = None) -> None:
        """Render one tab AND register everything on it for export.

        Registration happens here, not in the individual _build_ methods,
        so a tab physically cannot be added without its figures and its
        underlying tidy tables joining the export - the previous
        arrangement let six tabs ship with nothing exported at all. Keys
        are the file stems written by _save_figures."""
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

        A table can be legitimately empty - "missing cells" when nothing
        is missing, threshold sensitivity when the export carries no
        fa_theta_ columns - and a zero-row CSV in a results folder is
        noise that reads like a failed export. The captions already state
        those cases in words."""
        for slug, df in (datasets or {}).items():
            if df is not None and len(df):
                self._datasets[slug] = df

    # ------------------------------------------------------------------
    # Shared plotting: participant lines/dots + group centre per condition

    def _condition_axis(self, ax, metric: str, scale: float, ylabel: str, title: str) -> None:
        """Descriptive A-B-C trace plus condition points per participant.

        The line deliberately retains A-B and B-C to make each participant's
        three observed condition summaries visible. It is descriptive context,
        not an inferential contrast: formal performance comparisons remain B/C
        only. Diamonds are group means and bars are 95% t-CIs.
        """
        pivot = ga.condition_pivot(self._pc, metric) * scale
        x = np.arange(len(CONDITIONS))
        for _, row in pivot.iterrows():
            ys = [row.get(c, np.nan) for c in CONDITIONS]
            # Keep the complete descriptive trace. Do not remove A-B simply
            # because A is excluded from inference; the caption states the
            # difference between visual context and a statistical baseline.
            ax.plot(x, ys, "-", color=PARTICIPANT_LINE,
                    linewidth=0.9, alpha=0.7, zorder=1)
            for xi, (c, y) in enumerate(zip(CONDITIONS, ys)):
                if not np.isnan(y):
                    ax.scatter([xi], [y], s=22, color=CONDITION_COLORS[c], alpha=0.85, zorder=2)
        center = ga.group_center(self._pc, metric, ["condition"]).set_index("condition")
        for xi, c in enumerate(CONDITIONS):
            if c not in center.index or center.loc[c, "n"] == 0:
                continue
            mean = center.loc[c, "mean"] * scale
            if np.isnan(mean):
                continue
            if not np.isnan(center.loc[c, "ci95_lo"]):
                ax.errorbar([xi], [mean],
                            yerr=[[mean - center.loc[c, "ci95_lo"] * scale],
                                  [center.loc[c, "ci95_hi"] * scale - mean]],
                            color="black", capsize=4, linewidth=1.3, zorder=3)
            ax.scatter([xi], [mean], s=150, marker="D", color=CONDITION_COLORS[c],
                       edgecolor="black", zorder=4)
        ax.set_xticks(x, ["A\nreference", "B", "C"])
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=10)

    def _center_table_html(self, metric: str, scale: float, unit: str) -> str:
        center = ga.group_center(self._pc, metric, ["condition"]).set_index("condition")
        cells = []
        for c in CONDITIONS:
            if c not in center.index or center.loc[c, "n"] == 0 or np.isnan(center.loc[c, "mean"]):
                cells.append("n/a")
                continue
            txt = f"{center.loc[c, 'mean'] * scale:.1f}{unit}"
            if not np.isnan(center.loc[c, "sd"]):
                txt += f" ± {center.loc[c, 'sd'] * scale:.1f}"
            cells.append(f"{txt} (n={int(center.loc[c, 'n'])})")
        return "".join(f"<td align='center'>{v}</td>" for v in cells)

    # ------------------------------------------------------------------
    # Overview

    def _build_overview(self):
        data = self._data
        quality = ga.quality_summary(data.trial_rows, data.event_rows)
        avail = ga.cell_availability(data.trial_rows)

        lines = [f"<h3>Group overview — N = {data.n} ({', '.join(data.included)})</h3>"]
        per_p = [
            f"{r.participant}: {int(r.n_trials)} trials ({int(r.n_analyzed)} analyzed), "
            f"{int(r.n_valid_events)} valid events "
            f"({int(r.n_events) - int(r.n_valid_events)} excluded as carry-over)"
            for r in quality.itertuples()
        ]
        lines.append("<b>Data completeness:</b><br>" + "<br>".join(per_p))

        avail_html = ["<table border='0' cellspacing='0' cellpadding='4'>"
                      "<tr><th align='left'>Participants per cell</th>"
                      + "".join(f"<th>{LEVEL_DISPLAY_LABELS[lv]}</th>" for lv in LEVELS)
                      + "</tr>"]
        av = avail.set_index(["condition", "level"])
        for c in CONDITIONS:
            row = "".join(
                f"<td align='center'>{int(av.loc[(c, lv), 'n_participants'])}"
                f" ({int(av.loc[(c, lv), 'n_analyzed_participants'])} analyzed)</td>"
                for lv in LEVELS)
            avail_html.append(f"<tr><td><b>{self._cond_titles[c]}</b></td>{row}</tr>")
        avail_html.append("</table>")
        lines.append("".join(avail_html))

        metric_specs = [
            ("key_accuracy", 100, "%", "Key Accuracy"),
            ("fa_main", 100, "%", "Main Finger Accuracy (B/C); A hidden-target agreement"),
            ("fa_given_key", 100, "%", "Finger Accuracy | correct key (B/C); A agreement"),
            ("rt_correct_key_s", 1000, " ms", "RT, correct-key events"),
            ("rt_complete_s", 1000, " ms", "RT, key-and-finger-correct events"),
        ]
        table = ["<table border='0' cellspacing='0' cellpadding='4'>"
                 "<tr><th align='left'>Group mean ± SD across participants</th>"
                 + "".join(f"<th>{c}</th>" for c in CONDITIONS) + "</tr>"]
        for metric, scale, unit, label in metric_specs:
            table.append(f"<tr><td>{label}</td>{self._center_table_html(metric, scale, unit)}</tr>")
        table.append("</table>")
        lines.append("".join(table))
        lines.append(
            "Each participant enters every mean once (their own across-trial mean under the "
            "single-participant definitions: FA over video-analyzed trials, RT over trials with a "
            "valid RT, carry-over events already excluded). Error bars in the figures are 95% t-CIs "
            "across participants and require N ≥ 2. Descriptive only. <b>Condition A is a task "
            "reference, not a modality-performance baseline:</b> it supplies no target-finger cue, "
            "so its finger score is hidden-target agreement and its RT has lower response-selection "
            "complexity. The thin A–B–C trace is retained only to show each participant's three "
            "observed condition summaries; it is not an A-based inferential contrast. Inferential "
            "and performance-comparison tabs therefore use B versus C only.")

        fig1 = Figure(figsize=(10.5, 3.6))
        axes = fig1.subplots(1, 3)
        for ax, (metric, title) in zip(axes, [("key_accuracy", "Key Accuracy"),
                                              ("fa_main", "FA (B/C); A hidden-target agreement"),
                                              ("fa_given_key", "FA | key (B/C); A agreement")]):
            self._condition_axis(ax, metric, 100, "%", title)
            ax.set_ylim(0, 105)
        fig1.tight_layout()

        fig2 = Figure(figsize=(10.5, 3.6))
        axes2 = fig2.subplots(1, 2)
        self._condition_axis(axes2[0], "rt_correct_key_s", 1000, "ms", "RT — correct-key events")
        self._condition_axis(axes2[1], "rt_complete_s", 1000, "ms", "RT — key-and-finger-correct events")
        fig2.tight_layout()

        legend_note = ("<i>Figures: thin grey lines = individual participants (within-subject "
                       "pairing), diamonds = group mean, bars = 95% CI across participants.</i>")
        lines.append(legend_note)

        # The participant x condition table is the source every group
        # number on this page is derived from, so it is exported first
        # and the group summary beside it - a reader must be able to
        # recompute the summary from the tidy file.
        datasets = {
            "overview_participant_condition_metrics": self._pc,
            "overview_group_summary_by_condition": self._metric_centers(
                self._pc, [m[0] for m in metric_specs], ["condition"]),
            "overview_cell_availability": avail,
            "overview_data_completeness": quality,
        }
        return ("".join(f"<p>{line}</p>" for line in lines),
                {"group_overview_accuracy": fig1, "group_overview_rt": fig2},
                datasets)

    @staticmethod
    def _metric_centers(df, metrics: List[str], group_cols: List[str]):
        """Long tidy table of group_center over several metrics at once:
        one row per (group cell, metric) with n / mean / sd / 95% CI.
        This is the shape the report tables are built from."""
        frames = []
        for metric in metrics:
            center = ga.group_center(df, metric, group_cols)
            if len(center):
                frames.append(center.assign(metric=metric))
        if not frames:
            return pd.DataFrame(columns=group_cols + ["metric", "n", "mean", "sd", "sem",
                                                      "ci95_lo", "ci95_hi"])
        out = pd.concat(frames, ignore_index=True)
        return out[group_cols + ["metric", "n", "mean", "sd", "sem", "ci95_lo", "ci95_hi"]]

    # ------------------------------------------------------------------
    # Condition x Difficulty

    def _build_condition_difficulty(self):
        cells = self._cells[self._cells["condition"].isin(ga.GUIDANCE_CONDITIONS)]
        specs = [
            ("fa_main", 100, "%", "Main Finger Accuracy"),
            ("key_accuracy", 100, "%", "Key Accuracy"),
            ("rt_correct_key_s", 1000, "ms", "RT — correct-key"),
            ("rt_complete_s", 1000, "ms", "RT — complete correct"),
        ]
        fig = Figure(figsize=(10.5, 7.4))
        axes = fig.subplots(2, 2).ravel()
        x = np.arange(len(LEVELS))
        for ax, (metric, scale, unit, title) in zip(axes, specs):
            for c in ga.GUIDANCE_CONDITIONS:
                sub = cells[cells["condition"] == c]
                # Thin per-participant lines
                for p, prow in sub.groupby("participant"):
                    by_level = prow.set_index("level")[metric].reindex(LEVELS) * scale
                    ax.plot(x, by_level.to_numpy(dtype=float), "-",
                            color=CONDITION_COLORS[c], linewidth=0.8, alpha=0.3, zorder=1)
                center = ga.group_center(sub, metric, ["level"]).set_index("level").reindex(LEVELS)
                means = center["mean"].to_numpy(dtype=float) * scale
                ax.plot(x, means, "o-", color=CONDITION_COLORS[c], linewidth=2.0,
                        label=self._cond_titles[c], zorder=3)
                if (center["n"] >= 2).any():
                    lo = center["ci95_lo"].to_numpy(dtype=float) * scale
                    hi = center["ci95_hi"].to_numpy(dtype=float) * scale
                    ax.fill_between(x, lo, hi, color=CONDITION_COLORS[c], alpha=0.12, zorder=2)
            ax.set_xticks(x, [LEVEL_TICK_LABELS[lv] for lv in LEVELS])
            ax.set_xlabel("difficulty level")
            ax.set_ylabel(unit)
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=7, title="Feedback condition", title_fontsize=7)
        fig.tight_layout()

        # Caption: group means per cell + missing-cell report.
        fa_center = ga.group_center(cells, "fa_main", ["condition", "level"])
        rt_center = ga.group_center(cells, "rt_correct_key_s", ["condition", "level"])
        cap_rows = []
        for c in ga.GUIDANCE_CONDITIONS:
            fa_parts, rt_parts = [], []
            for lv in LEVELS:
                fa = fa_center[(fa_center["condition"] == c) & (fa_center["level"] == lv)]
                rt = rt_center[(rt_center["condition"] == c) & (rt_center["level"] == lv)]
                fa_parts.append(f"{LEVEL_DISPLAY_LABELS[lv]} "
                                + (_fmt(float(fa['mean'].iloc[0]) * 100, 0, '%') if len(fa) else "n/a"))
                rt_parts.append(f"{LEVEL_DISPLAY_LABELS[lv]} "
                                + (_fmt(float(rt['mean'].iloc[0]) * 1000, 0, ' ms') if len(rt) else "n/a"))
            cap_rows.append(f"<b>{self._cond_titles[c]}</b>: FA {' / '.join(fa_parts)}; "
                            f"RT {' / '.join(rt_parts)}")

        expected = {(p, c, lv) for p in self._data.included
                    for c in ga.GUIDANCE_CONDITIONS for lv in LEVELS}
        have = {(r.participant, r.condition, r.level) for r in cells.itertuples()}
        missing = sorted(expected - have)
        missing_note = ("All included participants have data in every condition × level cell."
                        if not missing else
                        "Missing cells (left out of the means, never filled with zeros): "
                        + ", ".join(
                            f"{p} {c}/{LEVEL_DISPLAY_LABELS[lv]}"
                            for p, c, lv in missing))
        caption = (
            "<h3>Condition × Difficulty</h3>"
            "<p>Performance-comparable guidance conditions only: B visual versus C haptic. "
            "Condition A is excluded because it provides no target-finger information.</p>"
            "<p>Faint lines: one per participant per condition (their mean over that cell's trials). "
            "Bold lines: group mean across participants; shaded band = 95% t-CI (needs N ≥ 2). "
            "Cell means per group:</p>"
            "<p>" + "<br>".join(cap_rows) + "</p>"
            f"<p>{missing_note}</p>"
            "<p>Descriptive within-subject comparison; the full condition × level interaction model "
            "is not fitted in this version (see Contrasts for the paired condition tests).</p>")
        datasets = {
            "condition_difficulty_participant_cells": cells,
            "condition_difficulty_group_summary": self._metric_centers(
                cells, [m[0] for m in specs], ["condition", "level"]),
            "condition_difficulty_missing_cells": pd.DataFrame(
                missing, columns=["participant", "condition", "level"]),
        }
        return caption, {"group_condition_difficulty": fig}, datasets

    # ------------------------------------------------------------------
    # Paired contrasts

    def _build_contrasts(self):
        specs = [
            ("fa_main", 100, "pp", "Main Finger Accuracy (percentage points)"),
            ("key_accuracy", 100, "pp", "Key Accuracy (percentage points)"),
            ("rt_correct_key_s", 1000, "ms", "RT — correct-key (ms)"),
            ("rt_complete_s", 1000, "ms", "RT — complete correct (ms)"),
        ]
        contrast_labels = [f"{a}−{b}" for a, b in ga.CONTRASTS]
        all_diffs, inference_rows = [], []
        fig = Figure(figsize=(10.5, 7.4))
        axes = fig.subplots(2, 2).ravel()
        cap_blocks = ["<h3>Paired B–C guidance contrast (within-participant)</h3>",
                      "C−B compares haptic with visual target-finger guidance. Condition A is not "
                      "an inferential baseline because it provides no target-finger information. One dot per "
                      "participant (their paired difference), diamond = group mean, bar = 95% t-CI "
                      "(needs N ≥ 2). Accuracy differences are in percentage points; the underlying "
                      "proportions (previous tabs) stay the computation basis."]
        for ax, (metric, scale, unit, title) in zip(axes, specs):
            diffs = ga.paired_differences(self._pc, metric)
            all_diffs.append(diffs.assign(metric=metric))
            inference_rows.extend(self._inference_rows(metric))
            x = np.arange(len(contrast_labels))
            ax.axhline(0, color="#bbbbbb", linewidth=1)
            metric_lines = [f"<b>{title}</b>"]
            for xi, label in enumerate(contrast_labels):
                sub = diffs[diffs["contrast"] == label]["diff"] * scale
                n_pairs = len(sub)
                jitter = (np.arange(n_pairs) - (n_pairs - 1) / 2) * (0.28 / max(n_pairs, 1))
                ax.scatter(xi + jitter, sub, s=26, color="#3a76c4", alpha=0.75, zorder=2)
                if n_pairs:
                    mean = float(sub.mean())
                    if n_pairs >= 2:
                        sd = float(sub.std(ddof=1))
                        half = float(sstats.t.ppf(0.975, n_pairs - 1)) * sd / np.sqrt(n_pairs)
                        ax.errorbar([xi], [mean], yerr=[[half], [half]], color="black",
                                    capsize=4, linewidth=1.3, zorder=3)
                    ax.scatter([xi], [mean], s=140, marker="D", color="#d9663d",
                               edgecolor="black", zorder=4)
                    missing = self._data.n - n_pairs
                    metric_lines.append(
                        f"{label}: mean {mean:+.1f} {unit}, n = {n_pairs} pairs"
                        + (f" ({missing} participant(s) missing a side)" if missing else ""))
                else:
                    metric_lines.append(f"{label}: no complete pairs")
            ax.set_xticks(x, contrast_labels)
            ax.set_ylabel(unit)
            ax.set_title(title, fontsize=10)
            metric_lines.append(self._inference_html(metric, scale, unit))
            cap_blocks.append("<br>".join(metric_lines))
        fig.tight_layout()
        datasets = {
            "contrasts_participant_differences": (pd.concat(all_diffs, ignore_index=True)
                                                  if all_diffs else pd.DataFrame()),
            "contrasts_tests": pd.DataFrame(inference_rows),
        }
        return ("".join(f"<p>{b}</p>" for b in cap_blocks),
                {"group_contrasts": fig}, datasets)

    def _inference_rows(self, metric: str) -> List[dict]:
        """The paired B/C t and Wilcoxon results as tidy export rows."""
        res = ga.condition_inference(self._pc, metric)
        base = {"metric": metric, "n_participants": res["n_participants"],
                "n_complete": res["n_complete"], "exploratory": res["exploratory"],
                "reason": res["reason"]}
        if res["reason"]:
            return [dict(base, test="not run")]
        t = res["paired_t"]
        rows = [dict(base, test=t["test"], contrast=t["contrast"], n=t["n"],
                     statistic=t["statistic"], p=t["p"], p_holm=np.nan,
                     effect_name=t["effect_name"], effect_size=t["effect_size"],
                     mean_diff=t["mean_diff"], ci95_lo=t["ci95_lo"],
                     ci95_hi=t["ci95_hi"], note=None)]
        for e in res["pairwise"]:
            rows.append(dict(base, test=e["test"], contrast=e["contrast"], n=e["n_pairs"],
                             statistic=e["statistic"], p=e["p"], p_holm=e["p_holm"],
                             effect_name=e["effect_name"], effect_size=e["effect_size"],
                             mean_diff=e["mean_diff"], ci95_lo=np.nan, ci95_hi=np.nan,
                             note=e["note"]))
        return rows

    def _inference_html(self, metric: str, scale: float, unit: str) -> str:
        res = ga.condition_inference(self._pc, metric)
        if res["reason"]:
            return f"<i>Inferential tests not run: {res['reason']}.</i>"
        parts = []
        tag = " <i>(exploratory — small N)</i>" if res["exploratory"] else ""
        paired = res["paired_t"]
        parts.append(
            f"{paired['test']}: N = {paired['n']} complete pairs "
            f"({res['n_missing_pairs']} participant(s) incomplete), "
            f"mean diff {paired['mean_diff'] * scale:+.1f} {unit}, 95% CI "
            f"[{paired['ci95_lo'] * scale:+.1f}, {paired['ci95_hi'] * scale:+.1f}], "
            f"t = {_fmt(paired['statistic'], 2)}, p {_fmt_p(paired['p'])}, "
            f"{paired['effect_name']} = {_fmt(paired['effect_size'], 2)}{tag}")
        for e in res["pairwise"]:
            if e["note"]:
                parts.append(f"{e['contrast']}: {e['test']} — {e['note']}")
                continue
            parts.append(
                f"Sensitivity: {e['test']}, n = {e['n_pairs']} pairs, "
                f"W = {_fmt(e['statistic'], 1)}, p {_fmt_p(e['p'])}, "
                f"{e['effect_name']} = {_fmt(e['effect_size'], 2)}")
        return "<i>" + "<br>".join(parts) + "</i>"

    # ------------------------------------------------------------------
    # Trade-off (descriptive; computed in app.group_tradeoff)

    def _add_tradeoff_tab(self) -> None:
        """Descriptive group speed-accuracy trade-off: one large 2D main
        figure, two exploratory 3D supplements below it, with display
        toggles for busy plots. Registers the figures and tidy CSVs for
        Export figures + data."""
        # Keep A here as descriptive context: its lower-choice-complexity RT
        # and hidden-target agreement are visually informative, even though A
        # is never a performance baseline or an inferential contrast.
        self._tradeoff = gt.compute(self._data.trial_rows, self._data.included)
        self._register_datasets(gt.export_datasets(self._tradeoff))

        content = QWidget()
        layout = QVBoxLayout(content)
        caption = QLabel(self._tradeoff_caption())
        caption.setWordWrap(True)
        caption.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(caption)

        toggles = QHBoxLayout()
        toggles.addWidget(QLabel("Show:"))
        self.tr_points_check = QCheckBox("Trial points")
        self.tr_pcent_check = QCheckBox("Participant centroids")
        self.tr_gcent_check = QCheckBox("Group centroids / CI")
        self.tr_connect_check = QCheckBox("Connect B → C within participant")
        for check in (self.tr_points_check, self.tr_pcent_check,
                      self.tr_gcent_check, self.tr_connect_check):
            check.setChecked(True)  # default everything on; untick trial
            # points when a larger N makes the clouds too dense.
            check.toggled.connect(self._refresh_tradeoff_figures)
            toggles.addWidget(check)
        toggles.addWidget(QLabel("Error bars:"))
        self.tr_errbar_combo = QComboBox()
        self.tr_errbar_combo.addItems(["95% t-CI", "±SD"])
        self.tr_errbar_combo.setToolTip(
            "Group-centroid error bars, both across PARTICIPANT centroids: 95% t-CI is the "
            "inferentially honest default but is very wide at small N (t = 12.7 at N = 2); "
            "±SD shows descriptive spread and stays readable. Trials are never pooled either way."
        )
        self.tr_errbar_combo.currentIndexChanged.connect(self._refresh_tradeoff_figures)
        toggles.addWidget(self.tr_errbar_combo)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        # 2D on top (full width), the two 3D supplements side by side
        # below; the fixed-height canvases shrink horizontally when the
        # window narrows.
        self._tradeoff_fig_box = QVBoxLayout()
        layout.addLayout(self._tradeoff_fig_box)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, "Trade-off")
        self._refresh_tradeoff_figures()

    def _refresh_tradeoff_figures(self) -> None:
        """(Re)build the three figures under the current toggles and
        swap them into the tab; the export registry sees exactly what is
        on screen."""
        options = gt.TradeoffOptions(
            show_trial_points=self.tr_points_check.isChecked(),
            show_participant_centroids=self.tr_pcent_check.isChecked(),
            show_group_centroids=self.tr_gcent_check.isChecked(),
            connect_b_to_c=self.tr_connect_check.isChecked(),
            error_bars="sd" if self.tr_errbar_combo.currentIndex() == 1 else "ci",
        )
        figs = gt.build_figures(self._tradeoff, cond_titles=self._cond_titles,
                                colors=CONDITION_COLORS, options=options)
        self._figures["group_tradeoff_2d"] = figs["group_tradeoff_2d"]
        self._figures["group_tradeoff_by_difficulty_3d"] = figs["group_tradeoff_by_difficulty_3d"]
        self._figures["group_tradeoff_by_participant_3d"] = figs["group_tradeoff_by_participant_3d"]

        self._clear_layout(self._tradeoff_fig_box)

        def make_canvas(fig):
            canvas = ScrollFriendlyCanvas(fig)
            canvas.setFixedHeight(int(fig.get_figheight() * 100))
            canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            return canvas

        self._tradeoff_fig_box.addWidget(make_canvas(figs["group_tradeoff_2d"]))
        row = QHBoxLayout()
        row.addWidget(make_canvas(figs["group_tradeoff_by_difficulty_3d"]))
        row.addWidget(make_canvas(figs["group_tradeoff_by_participant_3d"]))
        self._tradeoff_fig_box.addLayout(row)

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                GroupAnalysisWindow._clear_layout(item.layout())

    def _tradeoff_caption(self) -> str:
        data = self._tradeoff
        points = data.points
        n_included = int(points["included"].sum()) if len(points) else 0
        summary = gt.group_summary(data.gcent)

        lines = [
            f"<h3>Group speed–accuracy trade-off — N = {len(data.participants)} "
            f"({', '.join(data.participants)})</h3>",
            "All three conditions are retained on this descriptive view because A provides useful "
            "context for lower response-selection complexity and self-selected finger behaviour. "
            "For B/C, y is Main Finger Accuracy; for A, y is agreement with the unshown balanced "
            "target-finger label. The A points are therefore a task reference, not an ordinary "
            "accuracy/RT baseline, and do not enter the B/C contrasts or RM-ANOVA.",
            f"{len(points)} trials loaded, {n_included} plotted; each small point is one "
            "participant × condition × trial (up to 9 per participant and condition): "
            "x = mean RT of that trial's correct-key events (ms), y = the condition-appropriate "
            "finger outcome described above (%), both under the single-participant definitions (existing timeout, "
            "carry-over and correct-key rules; confirmed carry-over events are excluded). "
            "In the 3D supplements z is the difficulty level (α (alpha), β (beta), or "
            "γ (gamma)) or the participant.",
            "Hollow rings = participant × condition centroids (mean over that participant's "
            "included trials); large diamonds = group centroids computed FROM the participant "
            "centroids (every participant weighs equally — trials and events are never pooled "
            "across participants), error bars = 95% t-CI over participants by default, "
            "switchable to ±SD (both need ≥ 2; the t-CI is wide at small N by construction — "
            "t(0.975, n−1) = 12.7 at N = 2 — while ±SD shows descriptive spread only). "
            "Trials without a valid correct-key RT or without an analyzed FA are excluded and "
            "counted below, never plotted as 0.",
            "<b>Colour coding:</b> condition sets the hue (A grey, B blue, C orange). In the "
            "difficulty 3D figure, lightness and marker shape code the level (α (alpha) "
            "light/circle, β (beta) mid/triangle, γ (gamma) dark/square) within the condition "
            "hue; in the participant 3D "
            "figure, the marker codes the condition (A circle, B square, C diamond) and "
            "lightness codes the participant (same rank in A/B/C), with the z position and ID "
            "label as the primary grouping cue.",
        ]

        cond_lines = []
        for c in data.conditions:
            if c in summary["conditions"]:
                s = summary["conditions"][c]
                finger_label = "hidden-target agreement" if c == "A" else "Main FA"
                cond_lines.append(f"{self._cond_titles[c]}: mean RT {s['rt_ms']:.0f} ms, "
                                  f"mean {finger_label} {s['fa_pct']:.0f}% "
                                  f"(n = {s['n']} participants)")
            else:
                cond_lines.append(f"{self._cond_titles[c]}: no participant with plottable trials")
        if summary["delta_c_minus_b"]:
            d = summary["delta_c_minus_b"]
            cond_lines.append(f"<b>C − B:</b> FA {d['fa_pct']:+.1f} pp, RT {d['rt_ms']:+.0f} ms.")
        if summary["verdict"]:
            cond_lines.append(f"<b>{summary['verdict']}</b>")
        lines.append("<b>Group means (participant-weighted):</b><br>" + "<br>".join(cond_lines))

        notes = []
        excluded = points[~points["included"]] if len(points) else points
        if len(excluded):
            per_c = ", ".join(f"{c}: {n}" for c, n in excluded.groupby("condition").size().items())
            notes.append(f"Excluded trials (no valid RT / not analyzed): {len(excluded)} ({per_c}).")
        missing = gt.missing_cells(data)
        if missing:
            notes.append("Cells without any plottable trial (skipped, never zero-filled): "
                         + ", ".join(missing) + ".")
        if len(data.gcent) and (data.gcent["n_participants"] < 2).any():
            notes.append("Cells with fewer than 2 participants show a mean but no CI/SD bar.")
        if data.participants and len(data.participants) < ga.MIN_TEST_N:
            notes.append("Below the inferential-N threshold everything on this page is "
                         "descriptive only.")
        notes.append("The 2D figure is the main view; the 3D figures are exploratory "
                     "supplements. Paired B/C inferential statistics stay on "
                     "the <b>Contrasts</b> tab — this page makes no significance claims.")
        lines.append(" ".join(notes))
        return "".join(f"<p>{line}</p>" for line in lines)

    def _save_figures(self) -> None:
        """Export figures + data: every figure on every tab as PNG + SVG,
        every tidy table behind them as CSV, plus a manifest, under
        data/MainUserStudy/group_figures/.

        Registration happens in _add_tab, so this writes exactly what the
        window is showing. The writing itself, and its progress dialog,
        live in app.gui.analysis_export - shared with the participant
        window, which exports the same way under a per-participant prefix.

        The manifest makes a flat folder of ~110 files navigable, and its
        provenance row records WHICH participants the export came from - a
        CSV lifted into a report appendix has to be self-identifying."""
        provenance = {
            "rows": f"N = {self._data.n}",
            "columns": (f"participants: {', '.join(self._data.included)}; "
                        f"{len(self._data.trial_rows)} trials, "
                        f"{len(self._data.event_rows)} events "
                        f"({len(ga.valid_events(self._data.event_rows))} valid); "
                        f"exported {pd.Timestamp.now().isoformat(timespec='seconds')}"),
        }
        self.status_label.setText(export_analysis(
            self, STUDY_DATA_DIR / "group_figures",
            self._figures, self._datasets, provenance))

    # ------------------------------------------------------------------
    # Learning / order

    def _build_learning(self):
        rep = ga.participant_repetition_metrics(self._data.trial_rows)
        fig1 = Figure(figsize=(10.5, 3.8))
        ax_fa, ax_rt = fig1.subplots(1, 2)
        for ax, metric, scale, ylabel in ((ax_fa, "fa_main", 100, "Main FA (%)"),
                                          (ax_rt, "rt_correct_key_s", 1000, "RT (ms)")):
            for c in ga.GUIDANCE_CONDITIONS:
                sub = rep[rep["condition"] == c]
                for _, prow in sub.groupby("participant"):
                    by_rep = prow.set_index("repetition")[metric].reindex([1, 2, 3]) * scale
                    ax.plot([1, 2, 3], by_rep.to_numpy(dtype=float), "-",
                            color=CONDITION_COLORS[c], linewidth=0.8, alpha=0.3, zorder=1)
                center = (ga.group_center(sub, metric, ["repetition"])
                          .set_index("repetition").reindex([1, 2, 3]))
                ax.plot([1, 2, 3], center["mean"].to_numpy(dtype=float) * scale, "o-",
                        color=CONDITION_COLORS[c], linewidth=2.0,
                        label=self._cond_titles[c], zorder=3)
            ax.set_xticks([1, 2, 3], ["1st", "2nd", "3rd"])
            ax.set_xlabel("occurrence within condition × level cell")
            ax.set_ylabel(ylabel)
            ax.set_title(f"Within-cell repetition 1 → 3 — {ylabel}", fontsize=10)
            ax.legend(fontsize=7)
        fig1.tight_layout()

        pos = ga.session_position_metrics(self._data.trial_rows)
        fig2 = Figure(figsize=(10.5, 3.8))
        bx_fa, bx_rt = fig2.subplots(1, 2)
        for ax, metric, scale, ylabel in (
                (bx_fa, "fa_main_adjusted", 100, "Adjusted Main FA (%)"),
                (bx_rt, "rt_correct_key_s_adjusted", 1000, "Adjusted RT (ms)")):
            positions = sorted(pos["position"].unique())
            for _, prow in pos.groupby("participant"):
                by_pos = prow.set_index("position")[metric].reindex(positions) * scale
                ax.plot(positions, by_pos.to_numpy(dtype=float), "-",
                        color=PARTICIPANT_LINE, linewidth=0.8, alpha=0.55, zorder=1)
            center = (ga.group_center(pos, metric, ["position"])
                      .set_index("position").reindex(positions))
            ax.plot(positions, center["mean"].to_numpy(dtype=float) * scale, "-",
                    color="black", linewidth=2.0, label="group mean", zorder=3)
            ax.set_xlabel("actual trial position in session (1–27)")
            ax.set_ylabel(ylabel)
            ax.set_title(f"Adjusted session progression — {ylabel}", fontsize=10)
            ax.legend(fontsize=7)
        fig2.tight_layout()

        difficulty_progression = sp.difficulty_progression_metrics(
            self._data.trial_rows)
        difficulty_progression_summary = sp.difficulty_progression_summary(
            difficulty_progression)
        difficulty_figures = sp_figures.build_difficulty_progression_figures(
            difficulty_progression, difficulty_progression_summary)
        difficulty_figures[
            "group_learning_difficulty_progression_key_error_log"
        ] = sp_figures.build_key_error_log_figure(
            difficulty_progression, difficulty_progression_summary)

        caption = (
            "<h3>Learning / order</h3>"
            "<p><b>Top — within-cell repetition:</b> each participant's 1st/2nd/3rd trial inside a "
            "condition × level cell (ordered by that participant's own schedule), averaged over the "
            "three levels; bold line = group mean across participants. Each repetition is a "
            "different unique sequence, so this is a short-term trial-order trend under the "
            "condition, not sequence memorisation or long-term learning.</p>"
            "<p><b>Bottom — adjusted session progression:</b> B/C guidance trials at their actual "
            "presentation positions (1–27). Within each participant and metric, each raw trial is "
            "centred on that participant's Condition × Difficulty cell mean and returned to their "
            "B/C grand mean before aggregation. This prevents the changing randomised mix of B/C "
            "and α (alpha), β (beta), or γ (gamma) at a position from masquerading as "
            "learning or fatigue. Grey lines: "
            "individual adjusted trajectories; black: group mean among participants contributing "
            "at each position. Condition A is excluded from this B/C performance progression.</p>"
            "<p><b>Difficulty-aligned progression:</b> a complementary view using all trials. Within "
            "each participant and difficulty, the actual session order is relabelled occurrence "
            "1–9, so all individual trajectories can be overlaid with a bold participant-weighted "
            "group mean. α (alpha), β (beta), and γ (gamma) are shown together using different "
            "colours and markers. The left "
            "panel contains the observed trials; the right panel subtracts that participant's "
            "Condition×Difficulty mean and restores their mean for that difficulty, retaining every trial while "
            "removing changing condition composition as a source of apparent progression. RT is the "
            "primary view; key accuracy is a companion because its definition is directly comparable "
            "across the full data set. Adjusted key-accuracy values are centred display scores and can therefore "
            "fall slightly outside 0–100%; the observed panel contains the actual percentages. This "
            "remains descriptive and does not make a condition-effect claim.</p>"
            "<p><b>Near-ceiling key-accuracy trend:</b> the final figure uses only observed values and "
            "plots key error rate (100% − key accuracy) on a symmetric-log scale. Lower is better. "
            "The small linear region around 0 keeps perfect trials visible, while the logarithmic "
            "region separates small non-zero error rates that overlap near 100% accuracy. No adjusted "
            "scores are used in this figure.</p>")
        datasets = {
            "learning_within_cell_repetition": ga.within_cell_repetition(
                [t for t in self._data.trial_rows
                 if t.get("condition") in ga.GUIDANCE_CONDITIONS]),
            "learning_participant_repetition": rep,
            "learning_repetition_group_summary": self._metric_centers(
                rep, ["fa_main", "rt_correct_key_s"], ["condition", "repetition"]),
            "learning_session_position": pos,
            "learning_session_position_group_summary": self._metric_centers(
                pos, ["fa_main_adjusted", "rt_correct_key_s_adjusted"], ["position"]),
            "learning_difficulty_progression_trials": difficulty_progression,
            "learning_difficulty_progression_group_summary": difficulty_progression_summary,
        }
        figures = {"group_learning_repetition": fig1,
                   "group_learning_session_position": fig2}
        figures.update(difficulty_figures)
        return caption, figures, datasets

    # ------------------------------------------------------------------
    # Condition A free-fingering strategy

    def _build_condition_a_strategy(self):
        return condition_a_strategy_tab.build(self._data.event_rows)

    # ------------------------------------------------------------------
    # Errors

    def _build_errors(self):
        props = ga.participant_outcome_proportions(self._data.event_rows)
        valid = ga.valid_events(self._data.event_rows)
        pooled = compute_error_breakdown(valid)

        # Fig 1: stacked mean participant-level proportions per condition.
        fig1 = Figure(figsize=(10.5, 4.2))
        ax = fig1.subplots(1, 1)
        present = [c for c in CONDITIONS if (props["condition"] == c).any()]
        for i, c in enumerate(present):
            bottom = 0.0
            for cat in CATEGORIES:
                sub = props[(props["condition"] == c) & (props["category"] == cat)]
                center = ga.group_center(sub, "proportion", ["condition"])
                pct = float(center["mean"].iloc[0]) * 100 if len(center) else 0.0
                if np.isnan(pct) or pct == 0:
                    continue
                ax.bar(i, pct, 0.55, bottom=bottom, color=CATEGORY_COLORS[cat])
                if pct >= 4:
                    ax.text(i, bottom + pct / 2, f"{pct:.0f}%", ha="center", va="center", fontsize=8)
                bottom += pct
        ax.legend(handles=[Patch(facecolor=CATEGORY_COLORS[cat], label=cat) for cat in CATEGORIES],
                  fontsize=7, loc="center left", bbox_to_anchor=(1.0, 0.5))
        ax.set_xticks(range(len(present)), present)
        ax.set_ylabel("mean of participant-level % of valid events")
        ax.set_ylim(0, 100)
        ax.set_title("Event outcome composition (mean of participant-level proportions)", fontsize=10)
        fig1.tight_layout()

        # Fig 2: the headline error type - correct key + wrong finger.
        fig2 = Figure(figsize=(10.5, 3.6))
        ax2 = fig2.subplots(1, 1)
        ckwf = props[props["category"] == CAT_CK_WF]
        for xi, c in enumerate(CONDITIONS):
            sub = ckwf[ckwf["condition"] == c]
            vals = sub["proportion"].to_numpy(dtype=float) * 100
            jitter = (np.arange(len(vals)) - (len(vals) - 1) / 2) * (0.25 / max(len(vals), 1))
            ax2.scatter(xi + jitter, vals, s=30, color=CONDITION_COLORS[c], alpha=0.8, zorder=2)
            center = ga.group_center(sub, "proportion", ["condition"])
            if len(center) and not np.isnan(float(center["mean"].iloc[0])):
                mean = float(center["mean"].iloc[0]) * 100
                if not np.isnan(float(center["ci95_lo"].iloc[0])):
                    ax2.errorbar([xi], [mean],
                                 yerr=[[mean - float(center["ci95_lo"].iloc[0]) * 100],
                                       [float(center["ci95_hi"].iloc[0]) * 100 - mean]],
                                 color="black", capsize=4, linewidth=1.3, zorder=3)
                ax2.scatter([xi], [mean], s=140, marker="D", color=CONDITION_COLORS[c],
                            edgecolor="black", zorder=4)
        ax2.set_xticks(range(len(CONDITIONS)), CONDITIONS)
        ax2.set_ylabel("% of valid events")
        ax2.set_title("Correct key + wrong finger — participant-level rate by condition", fontsize=10)
        fig2.tight_layout()

        # Caption: pooled counts (supplementary) + wrong-key distance.
        pooled_tab = ["<table border='0' cellspacing='0' cellpadding='3'>"
                      "<tr><th align='left'>Pooled event counts (suppl.)</th>"
                      + "".join(f"<th>{c}</th>" for c in present) + "</tr>"]
        pooled_by = {(r.condition, r.category): r.count for r in pooled.itertuples()}
        for cat in CATEGORIES:
            row = "".join(f"<td align='center'>{pooled_by.get((c, cat), 0)}</td>" for c in present)
            pooled_tab.append(f"<tr><td>{cat}</td>{row}</tr>")
        pooled_tab.append("</table>")

        wk_stats = wrong_key_stats(compute_wrong_key_distance(valid))
        wk_lines = [
            f"{r.condition}: n={int(r.n)}, median {r.median:.0f}, mean {r.mean:.1f}, "
            f"max {int(r.max)} ({r.metric})"
            for r in wk_stats.itertuples()
        ] or ["no wrong-key events"]

        b_c_note = ""
        if {"B", "C"} <= set(present):
            def _mean_ckwf(c):
                sub = ckwf[ckwf["condition"] == c]["proportion"].dropna()
                return float(sub.mean()) * 100 if len(sub) else np.nan
            b, c = _mean_ckwf("B"), _mean_ckwf("C")
            if not (np.isnan(b) or np.isnan(c)):
                b_c_note = (f"<b>C vs B (headline):</b> mean participant-level "
                            f"correct-key/wrong-finger rate {b:.1f}% → {c:.1f}% "
                            f"({c - b:+.1f} pp). Descriptive; see Contrasts for the paired tests.")

        caption = (
            "<h3>Event outcome composition</h3>"
            "<p>Categories are the existing mutually exclusive set (timeouts have no keypress and "
            "unresolved finger verdicts are their own class, counted incorrect under the main FA "
            "definition). Denominator: each participant's valid events in the condition — confirmed "
            "carry-over events are excluded, unmatched extra presses are QC-only and never appear "
            "here. In A, finger-related categories denote agreement with the hidden target label, "
            "not failure to follow a supplied finger cue. The stacked bars average participant-level proportions (each participant weighs "
            "equally); the pooled counts below are supplementary and weigh events instead.</p>"
            + (f"<p>{b_c_note}</p>" if b_c_note else "")
            + "<p>" + "".join(pooled_tab) + "</p>"
            + "<p><b>Wrong-key distance</b> (pooled over participants, descriptive): "
            + "; ".join(wk_lines) + "</p>")
        datasets = {
            # Participant-level proportions are the analysis basis; the
            # pooled counts are the supplement, and both are exported so
            # the distinction survives into the report's appendix.
            "errors_participant_proportions": props,
            "errors_group_summary": ga.group_center(props, "proportion",
                                                    ["condition", "category"]),
            "errors_pooled_counts": pooled,
            "errors_wrong_key_distance": wk_stats,
        }
        return caption, {"group_errors_composition": fig1,
                         "group_errors_correct_key_wrong_finger": fig2}, datasets

    # ------------------------------------------------------------------
    # Finger confusion, pooled over every participant and trial

    def _draw_confusion(self, ax, grid: dict, title: str, normalized: bool,
                        vmax: float, cell_fontsize: float) -> None:
        """One target x detected heatmap, with the cells the scoring rule
        forgave outlined rather than left to read as errors.

        An off-diagonal cell where every event still passed is adjacent-
        fingertip ambiguity, not a substitution, so it gets a solid amber
        outline; a partly-passed cell gets a dashed one. This is the same
        distinction the per-trial matrix in the quiz detail window draws
        with cell tints - without it a pooled matrix looks like it
        disagrees with the group's finger accuracy."""
        matrix = np.array(grid["matrix"], dtype=float)
        passed = np.array(grid["passed"], dtype=float)
        shown = matrix
        if normalized:
            sums = matrix.sum(axis=1, keepdims=True)
            shown = np.divide(matrix * 100, sums, out=np.zeros_like(matrix), where=sums > 0)
        ax.imshow(shown, cmap="Blues", vmin=0, vmax=vmax)
        n = len(ga.FINGER_ORDER)
        for i in range(n):
            for j in range(n):
                v = shown[i][j]
                if v >= 0.5:
                    ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=cell_fontsize,
                            color="white" if v > 0.55 * vmax else "#1a3a5c")
                if i != j and passed[i][j] > 0:
                    ax.add_patch(Rectangle(
                        (j - 0.5, i - 0.5), 1, 1, fill=False, linewidth=1.3,
                        edgecolor=NEAR_TIE_COLOR,
                        linestyle="-" if passed[i][j] == matrix[i][j] else (0, (2, 1)),
                    ))
        ax.set_xticks(range(n), ga.FINGER_ORDER, fontsize=7)
        ax.set_yticks(range(n), ga.FINGER_ORDER, fontsize=7)
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("detected finger", fontsize=8)

    def _build_finger_confusion(self):
        events = self._data.event_rows
        confusion_df = ga.finger_confusion(events)
        # The headline matrix pools the two *cued* conditions only. A shows
        # the same axes but cannot be read the same way - nobody was told
        # which finger to use, so its off-diagonal mass is free choice, not
        # error, and folding it in would drag the diagonal down by a third
        # for a reason that has nothing to do with execution or detection.
        # A gets its own panel in fig 2, and the all-condition totals are
        # still reported in the caption and exported.
        guided = ga.confusion_grid(confusion_df, ga.GUIDANCE_CONDITIONS)
        pooled = ga.confusion_grid(confusion_df)
        by_cond = {c: ga.confusion_grid(confusion_df, c) for c in CONDITIONS}
        totals = ga.confusion_totals(guided)
        all_totals = ga.confusion_totals(pooled)

        # Fig 1: every participant and every cued trial in one matrix.
        fig1 = Figure(figsize=(10.5, 5.4))
        ax_counts, ax_norm = fig1.subplots(1, 2)
        vmax = max((max(row) for row in guided["matrix"]), default=1) or 1
        self._draw_confusion(ax_counts, guided, f"Event counts (n = {guided['total']})",
                             normalized=False, vmax=vmax, cell_fontsize=6.5)
        self._draw_confusion(ax_norm, guided, "Row-normalized % per target finger",
                             normalized=True, vmax=100.0, cell_fontsize=6.5)
        ax_counts.set_ylabel("target finger", fontsize=8)
        fig1.suptitle(
            f"Target vs detected finger — all {self._data.n} participants, all cued trials "
            f"(B + C) pooled", fontsize=11)
        fig1.legend(handles=[
            Patch(facecolor="none", edgecolor=NEAR_TIE_COLOR, label="all events passed θ (near-tie)"),
            Patch(facecolor="none", edgecolor=NEAR_TIE_COLOR, linestyle="--",
                  label="some passed θ, some did not"),
        ], loc="lower center", ncol=2, fontsize=7, frameon=False)
        fig1.tight_layout(rect=(0, 0.06, 1, 1))

        # Fig 2: the same thing split by condition, row-normalized so the
        # unequal event counts per cell don't drive the colour.
        fig2 = Figure(figsize=(10.5, 4.0))
        axes = fig2.subplots(1, len(CONDITIONS))
        for ax, c in zip(axes, CONDITIONS):
            self._draw_confusion(ax, by_cond[c],
                                 f"{self._cond_titles[c]} (n = {by_cond[c]['total']})",
                                 normalized=True, vmax=100.0, cell_fontsize=6.0)
        axes[0].set_ylabel("target finger", fontsize=8)
        fig2.suptitle("Row-normalized % per target finger, by condition", fontsize=11)
        fig2.tight_layout()

        top = top_confusion(guided["matrix"])
        lines = [
            "<h3>Finger confusion pooled across the whole study</h3>",
            "Rows are the cued finger, columns the fingertip the detector reported (its softmax "
            "argmax), both in physical order L5→R5, so neighbouring-finger substitutions sit next "
            "to the diagonal and cross-hand errors land outside the hand's block.",
            f"Of {guided['total']} cued events (B + C): <b>{totals['diagonal']}</b> on the "
            f"diagonal, <b>{totals['near_tie']}</b> off-diagonal that still scored correct "
            "(outlined amber — the cued finger held at least θ of the probability mass while a "
            f"neighbour was fractionally more probable), <b>{totals['substitution']}</b> "
            f"off-diagonal that did not, and <b>{totals['unresolved']}</b> unresolved (no fingertip "
            "detected; kept out of the grid and listed per target finger below).",
            "<b>Off the diagonal is therefore not the same as an error.</b> The amber cells are "
            "detector ambiguity between two adjacent fingertips over one key, and they are scored "
            "as the right finger — the same reading the per-trial matrix in the quiz detail window "
            "shows. Only the un-outlined off-diagonal cells are finger substitutions.",
        ]
        if top:
            lines.append(f"Most frequent off-diagonal cell in B + C: target {top['target']} "
                         f"detected as {top['actual']} (n = {top['n']}).")
        unres = {f: n for f, n in guided["unresolved"].items() if n}
        if unres:
            lines.append("Unresolved by target finger: "
                         + ", ".join(f"{f}: {n}" for f, n in unres.items()) + ".")
        lines.append(
            "<b>Condition A is not in the pooled matrix above.</b> It has no target-finger cue, so "
            "its off-diagonal mass is free choice rather than error and pooling it would move the "
            "diagonal for a reason that is not about execution or detection. It has its own panel "
            f"below, and for reference all three conditions together come to {pooled['total']} "
            f"events: {all_totals['diagonal']} diagonal, {all_totals['near_tie']} near-tie, "
            f"{all_totals['substitution']} off-diagonal, {all_totals['unresolved']} unresolved "
            "(also in the exported totals table).")
        lines.append("Descriptive only; the inferential comparisons are in Contrasts and RM-ANOVA.")
        caption = "".join(f"<p>{line}</p>" for line in lines)

        totals_df = pd.DataFrame([
            {"condition": c, **ga.confusion_totals(by_cond[c]), "total": by_cond[c]["total"]}
            for c in CONDITIONS
        ] + [
            {"condition": "B+C", **totals, "total": guided["total"]},
            {"condition": "all", **all_totals, "total": pooled["total"]},
        ])
        datasets = {
            "confusion_group_long": confusion_df,
            "confusion_group_totals": totals_df,
        }
        return caption, {"group_confusion_pooled": fig1,
                         "group_confusion_by_condition": fig2}, datasets

    # ------------------------------------------------------------------
    # Fingers

    def _build_fingers(self):
        pf = ga.per_finger_metrics(self._data.event_rows)
        pf = pf[pf["condition"].isin(ga.GUIDANCE_CONDITIONS)]
        fig = Figure(figsize=(10.5, 7.2))
        ax_fa, ax_rt = fig.subplots(2, 1)
        x = np.arange(len(ga.FINGER_IDS))
        width = 0.32
        cap_lines = []
        offset_mid = (len(ga.GUIDANCE_CONDITIONS) - 1) / 2
        for i, c in enumerate(ga.GUIDANCE_CONDITIONS):
            sub = pf[pf["condition"] == c]
            fa_center = (ga.group_center(sub, "fa", ["finger_id"])
                         .set_index("finger_id").reindex(ga.FINGER_IDS))
            rt_center = (ga.group_center(sub, "rt_s", ["finger_id"])
                         .set_index("finger_id").reindex(ga.FINGER_IDS))
            offset = (i - offset_mid) * width
            ax_fa.bar(x + offset, fa_center["mean"].to_numpy(dtype=float) * 100,
                      width, color=CONDITION_COLORS[c], label=self._cond_titles[c])
            ax_rt.bar(x + offset, rt_center["mean"].to_numpy(dtype=float) * 1000,
                      width, color=CONDITION_COLORS[c], label=self._cond_titles[c])
            for xi, fid in enumerate(ga.FINGER_IDS):
                fsub = sub[sub["finger_id"] == fid]
                ax_fa.scatter(np.full(len(fsub), xi + offset),
                              fsub["fa"].to_numpy(dtype=float) * 100,
                              s=14, color="black", alpha=0.55, zorder=3)
                ax_rt.scatter(np.full(len(fsub), xi + offset),
                              fsub["rt_s"].to_numpy(dtype=float) * 1000,
                              s=14, color="black", alpha=0.55, zorder=3)
            weakest = fa_center["mean"].idxmin() if fa_center["mean"].notna().any() else None
            if weakest is not None:
                cap_lines.append(
                    f"{self._cond_titles[c]}: weakest finger {weakest} "
                    f"({ga.FINGER_ID_NAMES[weakest]}, "
                    f"{float(fa_center.loc[weakest, 'mean']) * 100:.0f}%)")
        counts = pf.groupby("finger_id")[["n_left", "n_right"]].sum()
        tick_labels = [
            f"{fid} {ga.FINGER_ID_NAMES[fid]}\nL {int(counts.loc[fid, 'n_left']) if fid in counts.index else 0}"
            f" / R {int(counts.loc[fid, 'n_right']) if fid in counts.index else 0}"
            for fid in ga.FINGER_IDS
        ]
        for ax, ylabel, title in ((ax_fa, "Main FA (%)", "Finger accuracy by homologous finger ID"),
                                  (ax_rt, "RT (ms)", "Reaction time by homologous finger ID")):
            ax.set_xticks(x, tick_labels, fontsize=8)
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=7)
        fig.tight_layout()

        caption = (
            "<h3>Per-finger analysis (homologous L/R merge)</h3>"
            "<p>B/C guidance conditions only; Condition A has no target-finger cue and is not a "
            "per-finger performance baseline.</p>"
            "<p>Left- and right-hand observations are pooled by homologous finger ID (1 = thumb … "
            "5 = little); the L/R observation counts under each tick show exactly what was merged. "
            "Bars = group mean of participant-level values (each participant's own per-finger rate "
            "first, so no participant dominates); black dots = the individual participants. "
            "FA follows the single-participant Fingers tab definition: key AND finger correct over "
            "valid responded events with a finger verdict; RT averages valid responded events. "
            "Cells with few observations per participant are noisy — check the counts before "
            "reading differences.</p>"
            "<p>" + "; ".join(cap_lines) + "</p>")
        datasets = {
            # The per-finger cell table is the input to the RM-ANOVA and
            # to all three Finger Benefit analyses, so it is the single
            # most re-used export on this window.
            "fingers_participant_cells": pf,
            "fingers_group_summary": self._metric_centers(
                pf, ["fa", "rt_s", "rt_complete_s"], ["condition", "finger_id"]),
            "fingers_left_right_counts": (pf.groupby(["condition", "finger_id"])
                                          [["n", "n_left", "n_right", "n_judged",
                                            "n_rt_complete"]].sum().reset_index()),
        }
        return caption, {"group_fingers": fig}, datasets

    # ------------------------------------------------------------------
    # Condition x Finger repeated-measures ANOVA
    #
    # The Method's finger-specific model. Everything numeric is computed
    # in app.group_analysis (model, error terms, Mauchly, Greenhouse-
    # Geisser, and the ceiling diagnostics that justify keeping finger
    # accuracy descriptive); this section only renders it.

    def _build_rm_anova(self):
        pf = ga.per_finger_metrics(self._data.event_rows)
        conditions = ga.ANOVA_CONDITIONS
        primary_metric, primary_label = ga.ANOVA_METRICS[0]
        results = [(label, ga.rm_anova_finger(pf, metric, conditions))
                   for metric, label in ga.ANOVA_METRICS]
        primary = results[0][1]
        fa_desc = ga.finger_cell_descriptives(pf, "fa", conditions)
        ceiling = ga.ceiling_diagnostics(pf, "fa", conditions)

        blocks = [
            "<h3>Condition × Finger repeated-measures ANOVA</h3>",
            "Two-way within-participant ANOVA with <b>Condition</b> (B visual, C haptic) and "
            "<b>homologous Finger ID</b> (1 thumb … 5 little) as repeated factors, fitted on the "
            "per-finger cell means of the Fingers tab. Each participant contributes one value per "
            f"cell, so the design is {len(conditions)} × {len(ga.FINGER_IDS)} = "
            f"{len(conditions) * len(ga.FINGER_IDS)} cells per participant and the independent unit "
            "stays the participant. Every effect is tested against its own participant × effect "
            "interaction as the error term: F<sub>Condition</sub> = MS<sub>C</sub>/MS<sub>C×S</sub>, "
            "F<sub>Finger</sub> = MS<sub>F</sub>/MS<sub>F×S</sub>, F<sub>C×F</sub> = "
            "MS<sub>C×F</sub>/MS<sub>C×F×S</sub>. Effect size is partial η² = "
            "SS<sub>effect</sub> / (SS<sub>effect</sub> + SS<sub>error</sub>). "
            "Condition A is excluded by design — it carries no finger cue, so a per-digit cue "
            "effect is undefined there.",
            self._anova_design_html(primary, pf, conditions),
        ]

        for i, (label, res) in enumerate(results):
            role = ("<b>Primary model</b>" if i == 0 else
                    "<b>Sensitivity model</b> (same design, the Fingers tab's all-responded RT "
                    "definition instead of the correct-complete-action one)")
            blocks.append(f"{role} — {label}<br>" + self._anova_table_html(res))

        blocks.append(self._fa_descriptive_html(fa_desc, ceiling, conditions))

        figs, datasets = {}, {}
        # The RT panels are descriptive (cell means and the paired C − B
        # difference) and need no pingouin, so they are drawn whenever a
        # cell grid exists - a failed or gated fit must not also cost the
        # reader the picture the model is about.
        if len(primary["frame"]):
            figs["group_rm_anova_rt"] = self._anova_rt_figure(primary, primary_label)
            datasets["rm_anova_cells"] = primary["frame"]
        if not fa_desc.empty:
            figs["group_rm_anova_fa_descriptive"] = self._anova_fa_figure(
                pf, fa_desc, conditions)
            datasets["rm_anova_fa_descriptives"] = fa_desc
            datasets["rm_anova_fa_ceiling"] = pd.DataFrame([
                {k: v for k, v in ceiling.items() if k != "dropped"}])
        # Both models, not only the primary one: the sensitivity fit is
        # rendered on the page, so it belongs in the export too.
        effect_tables = [self._anova_effect_table(res) for _label, res in results]
        effect_tables = [t for t in effect_tables if not t.empty]
        if effect_tables:
            datasets["rm_anova_effects"] = pd.concat(effect_tables, ignore_index=True)

        blocks.append(
            "<i>Figures: left/top — cell means per condition across finger IDs (thin lines = "
            "individual participants, bold = group mean, bars = 95% t-CI across participants); "
            "right/bottom — the paired C − B difference per finger, which is the interaction term "
            "made visible (dots = participants, diamond = group mean, bar = 95% t-CI, dashed line "
            "= no difference).</i>")
        return "".join(f"<p>{b}</p>" for b in blocks), figs, datasets

    @staticmethod
    def _anova_design_html(res: dict, pf, conditions: List[str]) -> str:
        """Who is actually in the model and how many events back each
        cell - stated before any F, because a repeated-measures fit on a
        silently shrunk grid is the easiest way to mislead here."""
        parts = [f"<b>Design realised:</b> N = {res['n_participants']} participant(s) with a "
                 f"complete grid, {res['n_cells']} cells "
                 f"({res['cells_per_participant']} per participant)."]
        if res["dropped"]:
            parts.append("Dropped for incomplete cells (listwise, never imputed): "
                         + "; ".join(f"{p} ({why})" for p, why in sorted(res["dropped"].items()))
                         + ".")
        cells = pf[pf["condition"].isin(conditions)]
        if len(cells):
            for col, name in (("n_judged", "judged events"), ("n_rt_complete", "correct-complete events")):
                if col not in cells.columns:
                    continue
                counts = cells[col].to_numpy(dtype=float)
                parts.append(f"Per-cell {name}: min {int(np.nanmin(counts))}, "
                             f"median {np.nanmedian(counts):.0f}, max {int(np.nanmax(counts))}.")
        if res["exploratory"] and not res["reason"]:
            parts.append("<i>Exploratory at this sample size — read the effect sizes and "
                         "intervals for direction and future power planning, not as "
                         "generalisable inference.</i>")
        return " ".join(parts)

    @staticmethod
    def _anova_effect_table(res: dict):
        """The rendered effect table as a tidy DataFrame (export)."""
        rows = []
        for e in res["effects"]:
            m = e["mauchly"]
            rows.append({
                "metric": res["metric"], "effect": e["source"], "label": e["label"],
                "df1": e["df1"], "df2": e["df2"], "SS": e["ss"], "MS": e["ms"],
                "F": e["F"], "p_uncorrected": e["p_unc"], "partial_eta_sq": e["np2"],
                "mauchly_applicable": m["applicable"], "mauchly_W": m["W"],
                "mauchly_p": m["p"], "gg_epsilon": e["eps"],
                "gg_df1": e["df1_gg"], "gg_df2": e["df2_gg"], "p_gg": e["p_gg"],
                "correction_applied": e["correction"], "p_reported": e["p_reported"],
                "n_participants": res["n_participants"],
            })
        return pd.DataFrame(rows)

    def _anova_table_html(self, res: dict) -> str:
        if res["reason"]:
            return f"<i>Not fitted: {res['reason']}.</i>"
        head = ("<table border='0' cellspacing='0' cellpadding='4'>"
                "<tr><th align='left'>Effect</th><th>F</th><th>df</th><th>p</th>"
                "<th>partial η²</th><th>Mauchly W (p)</th><th>ε<sub>GG</sub></th>"
                "<th>p<sub>GG</sub></th></tr>")
        rows = []
        for e in res["effects"]:
            m = e["mauchly"]
            if not m["applicable"]:
                mauchly_cell = "n/a — 2 levels"
            elif np.isnan(m["W"]):
                mauchly_cell = m["note"] or "n/a"
            else:
                mauchly_cell = f"{m['W']:.3f} (p {_fmt_p(m['p'])})"
            eps_cell = _fmt(e["eps"], 3) if e["gg_applicable"] else "1.000 (fixed)"
            if not e["gg_applicable"]:
                gg_cell = "n/a"
            else:
                gg_cell = _fmt_p(e["p_gg"]).lstrip("= ")
                gg_cell = (f"<b>{gg_cell}</b> "
                           f"[df {e['df1_gg']:.2f}, {e['df2_gg']:.2f}]")
            p_cell = _fmt_p(e["p_unc"]).lstrip("= ")
            if e["gg_applicable"]:
                qualifier = "liberal" if e["sphericity_violated"] else "uncorrected"
                p_cell += f" <span style='color:#888'>({qualifier})</span>"
            rows.append(
                f"<tr><td>{e['label']}</td>"
                f"<td align='center'>{_fmt(e['F'], 3)}</td>"
                f"<td align='center'>{e['df1']:.0f}, {e['df2']:.0f}</td>"
                f"<td align='center'>{p_cell}</td>"
                f"<td align='center'>{_fmt(e['np2'], 3)}</td>"
                f"<td align='center'>{mauchly_cell}</td>"
                f"<td align='center'>{eps_cell}</td>"
                f"<td align='center'>{gg_cell}</td></tr>")
        note = ("For every multi-contrast effect, the bold Greenhouse–Geisser p with rescaled df "
                "is the primary reported value regardless of the low-powered Mauchly verdict; "
                "Mauchly W and p are diagnostic. Condition has only two levels, i.e. a single contrast, so it has no sphericity "
                 "assumption to violate (ε = 1 by construction) and is never GG-corrected.")
        return head + "".join(rows) + "</table><i>" + note + "</i>"

    @staticmethod
    def _fa_descriptive_html(fa_desc, ceiling: dict, conditions: List[str]) -> str:
        if fa_desc.empty:
            return ("<b>Finger accuracy — descriptive only.</b> No complete-case cell grid "
                    "available for the finger-accuracy table.")
        idx = fa_desc.set_index(["condition", "finger_id"])
        table = ["<table border='0' cellspacing='0' cellpadding='4'>"
                 "<tr><th align='left'>Main FA — mean ± SD [95% CI], % of judged events</th>"
                 + "".join(f"<th>{fid} {ga.FINGER_ID_NAMES[fid]}</th>" for fid in ga.FINGER_IDS)
                 + "</tr>"]
        for c in conditions:
            cells = []
            for fid in ga.FINGER_IDS:
                if (c, fid) not in idx.index:
                    cells.append("<td align='center'>n/a</td>")
                    continue
                r = idx.loc[(c, fid)]
                txt = f"{r['mean'] * 100:.1f} ± {_fmt(r['sd'] * 100, 1)}"
                if not np.isnan(r["ci95_lo"]):
                    txt += f"<br>[{r['ci95_lo'] * 100:.1f}, {r['ci95_hi'] * 100:.1f}]"
                cells.append(f"<td align='center'>{txt}<br><span style='color:#888'>"
                             f"n={int(r['n'])}</span></td>")
            table.append(f"<tr><td><b>{c}</b></td>{''.join(cells)}</tr>")
        table.append("</table>")
        pct = (ceiling["prop_at_ceiling"] * 100 if not np.isnan(ceiling["prop_at_ceiling"]) else np.nan)
        return (
            "<b>Finger accuracy — descriptive only, no ANOVA.</b> "
            + "".join(table)
            + "<i>Main FA is a bounded proportion (key AND finger correct over judged events) and "
              f"in this sample the {ceiling['n_cells']} cells run "
              f"{_fmt(ceiling['min'] * 100, 1, '%')}–{_fmt(ceiling['max'] * 100, 1, '%')}, with "
              f"{ceiling['n_at_ceiling']} of them ({_fmt(pct, 0, '%')}) exactly at 100%. Against "
              "the ceiling the cell variance is compressed and tied to the mean, so the normality "
              "and homogeneity assumptions behind an F ratio — and above all the Condition × Finger "
              "interaction test — are not credible. FA is therefore reported here as means, SDs and "
              "95% t-CIs over the same complete-case participants as the reaction-time model, and "
              "no F test is computed on it. Reaction time carries the inferential result.</i>")

    def _anova_rt_figure(self, res: dict, metric_label: str) -> Figure:
        """Cell means per condition across finger IDs, plus the paired
        C − B difference per finger (the interaction term, drawn)."""
        metric = res["metric"]
        frame = res["frame"]
        conditions = res["conditions"]
        fig = Figure(figsize=(10.5, 4.0))
        ax, ax_d = fig.subplots(1, 2)
        x = np.arange(len(ga.FINGER_IDS))

        for c in conditions:
            sub = frame[frame["condition"] == c]
            for _, prow in sub.groupby("participant"):
                ys = prow.set_index("finger_id")[metric].reindex(ga.FINGER_IDS) * 1000
                ax.plot(x, ys.to_numpy(dtype=float), "-", color=CONDITION_COLORS[c],
                        linewidth=0.8, alpha=0.3, zorder=1)
            center = (ga.group_center(sub, metric, ["finger_id"])
                      .set_index("finger_id").reindex(ga.FINGER_IDS))
            means = center["mean"].to_numpy(dtype=float) * 1000
            lo = center["ci95_lo"].to_numpy(dtype=float) * 1000
            hi = center["ci95_hi"].to_numpy(dtype=float) * 1000
            ax.errorbar(x, means, yerr=[means - lo, hi - means], fmt="o-",
                        color=CONDITION_COLORS[c], linewidth=2.0, capsize=4,
                        label=self._cond_titles[c], zorder=3)
        ax.set_xticks(x, [f"{fid}\n{ga.FINGER_ID_NAMES[fid]}" for fid in ga.FINGER_IDS], fontsize=8)
        ax.set_xlabel("homologous finger ID")
        ax.set_ylabel("RT (ms)")
        ax.set_title(f"{metric_label} — condition × finger cell means", fontsize=10)
        ax.legend(fontsize=7)

        # Paired difference: only defined for exactly two conditions.
        ax_d.axhline(0, color="#bbbbbb", linewidth=1, linestyle="--")
        if len(conditions) == 2:
            lo_c, hi_c = conditions[0], conditions[1]
            pivot = frame.pivot_table(index=["participant", "finger_id"],
                                      columns="condition", values=metric)
            diffs = (pivot[hi_c] - pivot[lo_c]).rename("diff").reset_index()
            for xi, fid in enumerate(ga.FINGER_IDS):
                vals = diffs[diffs["finger_id"] == fid]["diff"].to_numpy(dtype=float) * 1000
                jitter = (np.arange(len(vals)) - (len(vals) - 1) / 2) * (0.3 / max(len(vals), 1))
                ax_d.scatter(xi + jitter, vals, s=26, color="#3a76c4", alpha=0.75, zorder=2)
            center = ga.group_center(diffs, "diff", ["finger_id"]).set_index("finger_id").reindex(
                ga.FINGER_IDS)
            means = center["mean"].to_numpy(dtype=float) * 1000
            lo = center["ci95_lo"].to_numpy(dtype=float) * 1000
            hi = center["ci95_hi"].to_numpy(dtype=float) * 1000
            ax_d.errorbar(x, means, yerr=[means - lo, hi - means], fmt="D", markersize=9,
                          color="#d9663d", markeredgecolor="black", capsize=4,
                          linewidth=1.3, linestyle="none", zorder=4)
            ax_d.set_title(f"Paired {hi_c} − {lo_c} per finger (interaction term)", fontsize=10)
            ax_d.set_ylabel(f"{hi_c} − {lo_c} RT (ms)")
        else:
            ax_d.set_title("Paired difference needs exactly two conditions", fontsize=10)
        ax_d.set_xticks(x, [str(fid) for fid in ga.FINGER_IDS])
        ax_d.set_xlabel("homologous finger ID")
        fig.tight_layout()
        return fig

    def _anova_fa_figure(self, pf, fa_desc, conditions: List[str]) -> Figure:
        """Descriptive finger-accuracy cells with the 100% ceiling drawn,
        so the reason for keeping FA out of the ANOVA is visible rather
        than only asserted."""
        frame, _ = ga.anova_cell_frame(pf, "fa", conditions)
        fig = Figure(figsize=(10.5, 3.8))
        ax = fig.subplots(1, 1)
        x = np.arange(len(ga.FINGER_IDS))
        offset_step = 0.16
        ax.axhline(100, color="#c23b22", linewidth=1.2, linestyle="--", zorder=1,
                   label="100% ceiling")
        idx = fa_desc.set_index(["condition", "finger_id"])
        floor = 100.0
        # Markers + CI rather than bars: the point of this panel is how
        # far the cells sit from 100%, which needs a zoomed axis, and a
        # zoomed axis under bars would misrepresent the proportions.
        for i, c in enumerate(conditions):
            offset = (i - (len(conditions) - 1) / 2) * offset_step
            means, lo, hi = [], [], []
            for fid in ga.FINGER_IDS:
                r = idx.loc[(c, fid)] if (c, fid) in idx.index else None
                means.append(float(r["mean"]) * 100 if r is not None else np.nan)
                lo.append(float(r["ci95_lo"]) * 100 if r is not None else np.nan)
                hi.append(float(r["ci95_hi"]) * 100 if r is not None else np.nan)
            means, lo, hi = np.array(means), np.array(lo), np.array(hi)
            err_lo = np.where(np.isnan(lo), 0, means - lo)
            err_hi = np.where(np.isnan(hi), 0, hi - means)
            ax.errorbar(x + offset, means, yerr=[err_lo, err_hi], fmt="D", markersize=8,
                        color=CONDITION_COLORS[c], markeredgecolor="black", capsize=4,
                        linewidth=1.3, linestyle="none", zorder=4,
                        label=self._cond_titles[c])
            sub = frame[frame["condition"] == c]
            for xi, fid in enumerate(ga.FINGER_IDS):
                vals = sub[sub["finger_id"] == fid]["fa"].to_numpy(dtype=float) * 100
                jitter = (np.arange(len(vals)) - (len(vals) - 1) / 2) * (0.10 / max(len(vals), 1))
                ax.scatter(xi + offset + jitter, vals, s=16, color=CONDITION_COLORS[c],
                           alpha=0.55, edgecolor="none", zorder=3)
                if len(vals):
                    floor = min(floor, float(np.nanmin(vals)))
            finite = np.concatenate([lo[np.isfinite(lo)], means[np.isfinite(means)]])
            if finite.size:
                floor = min(floor, float(finite.min()))
        ax.set_xticks(x, [f"{fid} {ga.FINGER_ID_NAMES[fid]}" for fid in ga.FINGER_IDS], fontsize=8)
        ax.set_ylim(max(0.0, floor - 3.0), 102.5)
        ax.set_xlim(-0.5, len(ga.FINGER_IDS) - 0.5)
        ax.set_ylabel("Main FA (%)")
        ax.set_title("Finger accuracy per cell — descriptive only, no ANOVA "
                     "(bounded proportion against the 100% ceiling)", fontsize=10)
        ax.legend(fontsize=7, loc="lower right")
        fig.tight_layout()
        return fig

    # ------------------------------------------------------------------
    # Finger Benefit (compensation / equalisation / weakest finger)
    #
    # Rendered by .finger_benefit_tab, computed by app.finger_benefit,
    # app.finger_equalisation and app.finger_weakest - kept out of this
    # file so each of the three questions stays independently readable.

    def _build_finger_benefit(self):
        metric = ga.ANOVA_METRICS[0][0]  # the same RT the RM-ANOVA leads with
        caption, figures, datasets = finger_benefit_tab.build(
            self._data.event_rows, metric=metric)
        return caption, figures, datasets

    # ------------------------------------------------------------------
    # Quality

    def _build_quality(self):
        q = ga.quality_summary(self._data.trial_rows, self._data.event_rows)
        table = ["<table border='0' cellspacing='0' cellpadding='4'>"
                 "<tr><th align='left'>Participant</th><th>Trials (analyzed)</th>"
                 "<th>Valid / total events</th><th>Excluded carry-over</th>"
                 "<th>Suspected unresolved</th><th>Manual corrections</th>"
                 "<th>Unresolved</th><th>Ambiguous</th><th>Borderline</th>"
                 "<th>Extra presses (QC)</th><th>Sync</th></tr>"]
        for r in q.itertuples():
            table.append(
                f"<tr><td><b>{r.participant}</b></td>"
                f"<td align='center'>{int(r.n_trials)} ({int(r.n_analyzed)})</td>"
                f"<td align='center'>{int(r.n_valid_events)} / {int(r.n_events)}</td>"
                f"<td align='center'>{int(r.excluded_carryover)}</td>"
                f"<td align='center'>{int(r.suspected_carryover)}</td>"
                f"<td align='center'>{int(r.manual_corrections)}</td>"
                f"<td align='center'>{_fmt(r.unresolved_rate * 100 if not np.isnan(r.unresolved_rate) else np.nan, 1, '%')}</td>"
                f"<td align='center'>{_fmt(r.ambiguous_rate * 100 if not np.isnan(r.ambiguous_rate) else np.nan, 1, '%')}</td>"
                f"<td align='center'>{int(r.borderline_events)}</td>"
                f"<td align='center'>{int(r.qc_extra_presses)}</td>"
                f"<td align='center'>{r.sync_methods}</td></tr>")
        table.append("</table>")

        fig = Figure(figsize=(10.5, 3.6))
        ax = fig.subplots(1, 1)
        cats = [("excluded_carryover", "Excluded carry-over"),
                ("suspected_carryover", "Suspected unresolved"),
                ("manual_corrections", "Manual corrections"),
                ("qc_extra_presses", "Extra presses (QC)")]
        x = np.arange(len(cats))
        n_p = len(q)
        for i, r in enumerate(q.itertuples()):
            vals = [getattr(r, key) for key, _ in cats]
            ax.bar(x + (i - (n_p - 1) / 2) * (0.8 / max(n_p, 1)), vals, 0.8 / max(n_p, 1),
                   label=r.participant)
        ax.set_xticks(x, [label for _, label in cats], fontsize=8)
        ax.set_ylabel("count")
        ax.set_title("Audit counters per participant", fontsize=10)
        ax.legend(fontsize=7)
        fig.tight_layout()

        theta = ga.threshold_sensitivity(self._data.trial_rows, self._data.event_rows)
        theta_html = ""
        final_fa = self._pc[self._pc["condition"].isin(ga.GUIDANCE_CONDITIONS)][
            ["participant", "condition", "fa_main"]]
        if not theta.empty:
            thetas = sorted(theta["theta"].unique())
            t_tab = ["<table border='0' cellspacing='0' cellpadding='3'>"
                     "<tr><th align='left'>Mean FA by detection threshold θ</th>"
                     + "".join(f"<th>{th:.2f}</th>" for th in thetas) + "</tr>"]
            for c in ga.GUIDANCE_CONDITIONS:
                sub = theta[theta["condition"] == c]
                cells = []
                for th in thetas:
                    center = ga.group_center(sub[sub["theta"] == th], "fa", ["theta"])
                    v = float(center["mean"].iloc[0]) * 100 if len(center) else np.nan
                    cells.append(f"<td align='center'>{_fmt(v, 0, '%')}</td>")
                t_tab.append(f"<tr><td><b>{c}</b></td>{''.join(cells)}</tr>")
            t_tab.append("</table>")
            final_tab = ["<table border='0' cellspacing='0' cellpadding='3'>"
                         "<tr><th align='left'>Final reviewed Main FA (separate reference)</th>"
                         "<th>Mean</th></tr>"]
            for c in ga.GUIDANCE_CONDITIONS:
                center = ga.group_center(final_fa[final_fa["condition"] == c],
                                         "fa_main", ["condition"])
                value = float(center["mean"].iloc[0]) * 100 if len(center) else np.nan
                final_tab.append(f"<tr><td><b>{c}</b></td>"
                                 f"<td align='center'>{_fmt(value, 1, '%')}</td></tr>")
            final_tab.append("</table>")
            theta_html = (
                "<p><b>Threshold sensitivity</b> (B/C only): every θ, including 0.40, is "
                "recomputed from the same automatic event-level target-finger probability. "
                "The final reviewed Main FA is shown separately and is not inserted into the "
                "threshold curve. " + "".join(t_tab) + "<br>" + "".join(final_tab) + "</p>")

        caption = (
            "<h3>Data quality & audit</h3>"
            "<p>Audit context only — none of these counters feed the outcome tabs. Excluded "
            "carry-over events were manually confirmed and are removed from every accuracy/RT "
            "statistic; suspected-but-unresolved carry-over still awaits a manual verdict; extra "
            "presses are unmatched raw MIDI presses (QC only). Unresolved/ambiguous rates and "
            "borderline counts describe finger-detection confidence on the video-analyzed trials.</p>"
            "<p>" + "".join(table) + "</p>" + theta_html)
        datasets = {
            "quality_participant_audit": q,
            "quality_threshold_sensitivity": theta,
            "quality_threshold_final_fa_reference": final_fa,
            "quality_threshold_group_summary": (
                ga.group_center(theta, "fa", ["condition", "theta"]) if not theta.empty
                else pd.DataFrame()),
        }
        return caption, {"group_quality_audit": fig}, datasets
