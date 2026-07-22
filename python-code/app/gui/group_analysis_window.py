"""Group Analysis (multi-participant) - launcher section 7.

Select any subset of exported Main User Study participants and get the
group-level picture over the same within-subject design the
single-participant window shows: condition and difficulty comparisons,
paired condition contrasts, a descriptive speed-accuracy trade-off,
learning/order trends, event-outcome composition, homologous per-finger
profiles, and a data-quality audit.

All computation lives in app.group_analysis, which reads the
reviewed-and-exported <participant>_{trials,events}.csv files (the
Participant Export schema) - the same final verdicts and validity rules
as the single-participant window, so the two can never disagree.

Design rules enforced here:
  - the independent unit of every mean, interval and test is the
    PARTICIPANT (thin lines/dots per participant, group centre on top);
  - conditions are compared within-subject (paired contrasts; Friedman /
    paired Wilcoxon only, gated by complete-case N and labelled
    exploratory at small N - at N=1 no inferential test runs at all);
  - missing cells stay missing (no zero-fill), carry-over-invalidated
    events stay out of outcomes, QC counters stay out of outcomes;
  - every Analyse click recomputes from disk for exactly the current
    selection - no cached results survive a selection change.

Export ("Export figures + data", participant-window pattern) currently
covers the Trade-off tab's figures and tidy CSVs; the other tabs stay
review-in-window only for now.
"""

from typing import Dict, List

import numpy as np
from matplotlib.figure import Figure
from matplotlib.patches import Patch
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import group_analysis as ga
from .. import group_tradeoff as gt
from ..pilot_study import DATA_DIR as STUDY_DATA_DIR
from ..participant_analysis import (
    CAT_CK_WF,
    CATEGORIES,
    compute_error_breakdown,
    compute_wrong_key_distance,
    wrong_key_stats,
)
from .participant_analysis_window import (
    CATEGORY_COLORS,
    CONDITION_COLORS,
    ScrollFriendlyCanvas,
)

CONDITIONS = ga.CONDITIONS
LEVELS = ga.LEVELS
LEVEL_SYMBOLS = ga.LEVEL_SYMBOLS
PARTICIPANT_LINE = "#9a9a9a"


def _fmt(x, decimals=0, suffix="") -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x:.{decimals}f}{suffix}"


def _fmt_p(p) -> str:
    if p is None or np.isnan(p):
        return "n/a"
    return "< 0.001" if p < 0.001 else f"= {p:.3f}"


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
        self.tabs = QTabWidget()

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

        self._add_tab("Overview", *self._build_overview())
        self._add_tab("Condition × Difficulty", *self._build_condition_difficulty())
        self._add_tab("Contrasts", *self._build_contrasts())
        self._add_tradeoff_tab()
        self._add_tab("Learning / Order", *self._build_learning())
        self._add_tab("Errors", *self._build_errors())
        self._add_tab("Fingers", *self._build_fingers())
        self._add_tab("Quality", *self._build_quality())
        self.save_figs_btn.setEnabled(True)

    @staticmethod
    def _condition_titles(trial_rows: List[dict]) -> Dict[str, str]:
        titles = {}
        for c in CONDITIONS:
            labels = {t.get("condition_label") for t in trial_rows
                      if t["condition"] == c and t.get("condition_label")}
            label = next(iter(sorted(labels)), "")
            titles[c] = f"{c} ({label})" if label else c
        return titles

    def _add_tab(self, title: str, caption_html: str, figures: List[Figure]) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        caption = QLabel(caption_html)
        caption.setWordWrap(True)
        caption.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(caption)
        for fig in figures:
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

    # ------------------------------------------------------------------
    # Shared plotting: participant lines/dots + group centre per condition

    def _condition_axis(self, ax, metric: str, scale: float, ylabel: str, title: str) -> None:
        """One participant = one thin line across A/B/C (pairing kept
        visible); diamonds = group mean, error bars = 95% t-CI over
        participants (absent when N < 2)."""
        pivot = ga.condition_pivot(self._pc, metric) * scale
        x = np.arange(len(CONDITIONS))
        for _, row in pivot.iterrows():
            ys = [row.get(c, np.nan) for c in CONDITIONS]
            ax.plot(x, ys, "-", color=PARTICIPANT_LINE, linewidth=0.9, alpha=0.7, zorder=1)
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
        ax.set_xticks(x, CONDITIONS)
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
                      + "".join(f"<th>{LEVEL_SYMBOLS[lv]}</th>" for lv in LEVELS) + "</tr>"]
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
            ("fa_main", 100, "%", "Main Finger Accuracy (key + finger correct)"),
            ("fa_given_key", 100, "%", "Finger Accuracy | correct key (auxiliary)"),
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
            "across participants and require N ≥ 2. Descriptive only.")

        fig1 = Figure(figsize=(10.5, 3.6))
        axes = fig1.subplots(1, 3)
        for ax, (metric, title) in zip(axes, [("key_accuracy", "Key Accuracy"),
                                              ("fa_main", "Main Finger Accuracy"),
                                              ("fa_given_key", "Finger Acc | correct key")]):
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
        return "".join(f"<p>{line}</p>" for line in lines), [fig1, fig2]

    # ------------------------------------------------------------------
    # Condition x Difficulty

    def _build_condition_difficulty(self):
        cells = self._cells
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
            for c in CONDITIONS:
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
            ax.set_xticks(x, [LEVEL_SYMBOLS[lv] for lv in LEVELS])
            ax.set_xlabel("difficulty level")
            ax.set_ylabel(unit)
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=7)
        fig.tight_layout()

        # Caption: group means per cell + missing-cell report.
        fa_center = ga.group_center(cells, "fa_main", ["condition", "level"])
        rt_center = ga.group_center(cells, "rt_correct_key_s", ["condition", "level"])
        cap_rows = []
        for c in CONDITIONS:
            fa_parts, rt_parts = [], []
            for lv in LEVELS:
                fa = fa_center[(fa_center["condition"] == c) & (fa_center["level"] == lv)]
                rt = rt_center[(rt_center["condition"] == c) & (rt_center["level"] == lv)]
                fa_parts.append(f"{LEVEL_SYMBOLS[lv]} "
                                + (_fmt(float(fa['mean'].iloc[0]) * 100, 0, '%') if len(fa) else "n/a"))
                rt_parts.append(f"{LEVEL_SYMBOLS[lv]} "
                                + (_fmt(float(rt['mean'].iloc[0]) * 1000, 0, ' ms') if len(rt) else "n/a"))
            cap_rows.append(f"<b>{self._cond_titles[c]}</b>: FA {' / '.join(fa_parts)}; "
                            f"RT {' / '.join(rt_parts)}")

        expected = {(p, c, lv) for p in self._data.included for c in CONDITIONS for lv in LEVELS}
        have = {(r.participant, r.condition, r.level) for r in cells.itertuples()}
        missing = sorted(expected - have)
        missing_note = ("All included participants have data in every condition × level cell."
                        if not missing else
                        "Missing cells (left out of the means, never filled with zeros): "
                        + ", ".join(f"{p} {c}/{LEVEL_SYMBOLS[lv]}" for p, c, lv in missing))
        caption = (
            "<h3>Condition × Difficulty</h3>"
            "<p>Faint lines: one per participant per condition (their mean over that cell's trials). "
            "Bold lines: group mean across participants; shaded band = 95% t-CI (needs N ≥ 2). "
            "Cell means per group:</p>"
            "<p>" + "<br>".join(cap_rows) + "</p>"
            f"<p>{missing_note}</p>"
            "<p>Descriptive within-subject comparison; the full condition × level interaction model "
            "is not fitted in this version (see Contrasts for the paired condition tests).</p>")
        return caption, [fig]

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
        fig = Figure(figsize=(10.5, 7.4))
        axes = fig.subplots(2, 2).ravel()
        cap_blocks = ["<h3>Paired condition contrasts (within-participant)</h3>",
                      "B−A: adding the visual finger cue over key-only; C−A: adding the haptic "
                      "finger cue over key-only; C−B: haptic vs visual finger cue. One dot per "
                      "participant (their paired difference), diamond = group mean, bar = 95% t-CI "
                      "(needs N ≥ 2). Accuracy differences are in percentage points; the underlying "
                      "proportions (previous tabs) stay the computation basis."]
        for ax, (metric, scale, unit, title) in zip(axes, specs):
            diffs = ga.paired_differences(self._pc, metric)
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
        return "".join(f"<p>{b}</p>" for b in cap_blocks), [fig]

    def _inference_html(self, metric: str, scale: float, unit: str) -> str:
        res = ga.condition_inference(self._pc, metric)
        if res["reason"]:
            return f"<i>Inferential tests not run: {res['reason']}.</i>"
        parts = []
        tag = " <i>(exploratory — small N)</i>" if res["exploratory"] else ""
        fried = res["friedman"]
        parts.append(
            f"{fried['test']}: N = {fried['n']} complete cases "
            f"({res['n_missing_pairs']} participant(s) incomplete), "
            f"χ² = {_fmt(fried['statistic'], 2)}, p {_fmt_p(fried['p'])}, "
            f"{fried['effect_name']} = {_fmt(fried['effect_size'], 2)}{tag}")
        if not np.isnan(fried["p"]) and fried["p"] < 0.10:
            for e in res["pairwise"]:
                if e["note"]:
                    parts.append(f"{e['contrast']}: {e['test']} — {e['note']}")
                    continue
                parts.append(
                    f"{e['contrast']}: {e['test']}, n = {e['n_pairs']} pairs, "
                    f"mean diff {e['mean_diff'] * scale:+.1f} {unit}, "
                    f"W = {_fmt(e['statistic'], 1)}, p {_fmt_p(e['p'])}, "
                    f"Holm-corrected p {_fmt_p(e['p_holm'])}, "
                    f"{e['effect_name']} = {_fmt(e['effect_size'], 2)}")
        else:
            parts.append("Pairwise Wilcoxon contrasts omitted "
                         f"(Friedman p {_fmt_p(fried['p'])} gives no reason to pursue them).")
        return "<i>" + "<br>".join(parts) + "</i>"

    # ------------------------------------------------------------------
    # Trade-off (descriptive; computed in app.group_tradeoff)

    def _add_tradeoff_tab(self) -> None:
        """Descriptive group speed-accuracy trade-off: one large 2D main
        figure, two exploratory 3D supplements below it, with display
        toggles for busy plots. Registers the figures and tidy CSVs for
        Export figures + data."""
        self._tradeoff = gt.compute(self._data.trial_rows, self._data.included)
        self._datasets.update(gt.export_datasets(self._tradeoff))

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
            f"{len(points)} trials loaded, {n_included} plotted; each small point is one "
            "participant × condition × trial (up to 9 per participant and condition): "
            "x = mean RT of that trial's correct-key events (ms), y = the trial's Main Finger "
            "Accuracy (%), both under the single-participant definitions (existing timeout, "
            "carry-over and correct-key rules; confirmed carry-over events are excluded). "
            "In the 3D supplements z is the difficulty level (α/β/γ) or the participant.",
            "Hollow rings = participant × condition centroids (mean over that participant's "
            "included trials); large diamonds = group centroids computed FROM the participant "
            "centroids (every participant weighs equally — trials and events are never pooled "
            "across participants), error bars = 95% t-CI over participants by default, "
            "switchable to ±SD (both need ≥ 2; the t-CI is wide at small N by construction — "
            "t(0.975, n−1) = 12.7 at N = 2 — while ±SD shows descriptive spread only). "
            "Trials without a valid correct-key RT or without an analyzed FA are excluded and "
            "counted below, never plotted as 0.",
            "<b>Colour coding:</b> condition sets the hue (A grey, B blue, C orange). In the "
            "difficulty 3D figure, lightness and marker shape code the level (α light/circle, "
            "β mid/triangle, γ dark/square) within the condition hue; in the participant 3D "
            "figure, the marker codes the condition (A circle, B square, C diamond) and "
            "lightness codes the participant (same rank in A/B/C), with the z position and ID "
            "label as the primary grouping cue.",
        ]

        cond_lines = []
        for c in CONDITIONS:
            if c in summary["conditions"]:
                s = summary["conditions"][c]
                cond_lines.append(f"{self._cond_titles[c]}: mean RT {s['rt_ms']:.0f} ms, "
                                  f"mean FA {s['fa_pct']:.0f}% (n = {s['n']} participants)")
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
                     "supplements. Inferential statistics (Friedman / paired Wilcoxon) stay on "
                     "the <b>Contrasts</b> tab — this page makes no significance claims.")
        lines.append(" ".join(notes))
        return "".join(f"<p>{line}</p>" for line in lines)

    def _save_figures(self) -> None:
        """Export figures + data (participant-window pattern): 300 dpi
        PNG + SVG per registered figure plus the tidy CSVs, under
        data/MainUserStudy/group_figures/."""
        out_dir = STUDY_DATA_DIR / "group_figures"
        out_dir.mkdir(parents=True, exist_ok=True)
        figs = csvs = 0
        for slug, fig in self._figures.items():
            fig.savefig(out_dir / f"{slug}.png", dpi=300, bbox_inches="tight")
            fig.savefig(out_dir / f"{slug}.svg", bbox_inches="tight")
            figs += 1
        for slug, df in self._datasets.items():
            df.to_csv(out_dir / f"{slug}.csv", index=False)
            csvs += 1
        self.status_label.setText(
            f"Exported {figs} figures (300 dpi PNG + SVG) + {csvs} CSVs to {out_dir}")

    # ------------------------------------------------------------------
    # Learning / order

    def _build_learning(self):
        rep = ga.participant_repetition_metrics(self._data.trial_rows)
        fig1 = Figure(figsize=(10.5, 3.8))
        ax_fa, ax_rt = fig1.subplots(1, 2)
        for ax, metric, scale, ylabel in ((ax_fa, "fa_main", 100, "Main FA (%)"),
                                          (ax_rt, "rt_correct_key_s", 1000, "RT (ms)")):
            for c in CONDITIONS:
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
        for ax, metric, scale, ylabel in ((bx_fa, "fa_main", 100, "Main FA (%)"),
                                          (bx_rt, "rt_correct_key_s", 1000, "RT (ms)")):
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
            ax.set_title(f"Session progression — {ylabel}", fontsize=10)
            ax.legend(fontsize=7)
        fig2.tight_layout()

        caption = (
            "<h3>Learning / order</h3>"
            "<p><b>Top — within-cell repetition:</b> each participant's 1st/2nd/3rd trial inside a "
            "condition × level cell (ordered by that participant's own schedule), averaged over the "
            "three levels; bold line = group mean across participants. Each repetition is a "
            "different unique sequence, so this is a short-term trial-order trend under the "
            "condition, not sequence memorisation or long-term learning.</p>"
            "<p><b>Bottom — session progression:</b> every trial at its actual presentation "
            "position (1–27). The condition and difficulty at a given position differ across "
            "participants' randomised schedules, so this curve mixes them by design and reads as "
            "session-level progression/fatigue only — never as a condition comparison. Grey lines: "
            "individual participants; black: group mean over the participants contributing at each "
            "position.</p>")
        return caption, [fig1, fig2]

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
            "here. The stacked bars average participant-level proportions (each participant weighs "
            "equally); the pooled counts below are supplementary and weigh events instead.</p>"
            + (f"<p>{b_c_note}</p>" if b_c_note else "")
            + "<p>" + "".join(pooled_tab) + "</p>"
            + "<p><b>Wrong-key distance</b> (pooled over participants, descriptive): "
            + "; ".join(wk_lines) + "</p>")
        return caption, [fig1, fig2]

    # ------------------------------------------------------------------
    # Fingers

    def _build_fingers(self):
        pf = ga.per_finger_metrics(self._data.event_rows)
        fig = Figure(figsize=(10.5, 7.2))
        ax_fa, ax_rt = fig.subplots(2, 1)
        x = np.arange(len(ga.FINGER_IDS))
        width = 0.25
        cap_lines = []
        for i, c in enumerate(CONDITIONS):
            sub = pf[pf["condition"] == c]
            fa_center = (ga.group_center(sub, "fa", ["finger_id"])
                         .set_index("finger_id").reindex(ga.FINGER_IDS))
            rt_center = (ga.group_center(sub, "rt_s", ["finger_id"])
                         .set_index("finger_id").reindex(ga.FINGER_IDS))
            ax_fa.bar(x + (i - 1) * width, fa_center["mean"].to_numpy(dtype=float) * 100,
                      width, color=CONDITION_COLORS[c], label=self._cond_titles[c])
            ax_rt.bar(x + (i - 1) * width, rt_center["mean"].to_numpy(dtype=float) * 1000,
                      width, color=CONDITION_COLORS[c], label=self._cond_titles[c])
            for xi, fid in enumerate(ga.FINGER_IDS):
                fsub = sub[sub["finger_id"] == fid]
                ax_fa.scatter(np.full(len(fsub), xi + (i - 1) * width),
                              fsub["fa"].to_numpy(dtype=float) * 100,
                              s=14, color="black", alpha=0.55, zorder=3)
                ax_rt.scatter(np.full(len(fsub), xi + (i - 1) * width),
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
            "<p>Left- and right-hand observations are pooled by homologous finger ID (1 = thumb … "
            "5 = little); the L/R observation counts under each tick show exactly what was merged. "
            "Bars = group mean of participant-level values (each participant's own per-finger rate "
            "first, so no participant dominates); black dots = the individual participants. "
            "FA follows the single-participant Fingers tab definition: key AND finger correct over "
            "valid responded events with a finger verdict; RT averages valid responded events. "
            "Cells with few observations per participant are noisy — check the counts before "
            "reading differences.</p>"
            "<p>" + "; ".join(cap_lines) + "</p>")
        return caption, [fig]

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

        theta = ga.threshold_sensitivity(self._data.trial_rows)
        theta_html = ""
        if not theta.empty:
            thetas = sorted(theta["theta"].unique())
            t_tab = ["<table border='0' cellspacing='0' cellpadding='3'>"
                     "<tr><th align='left'>Mean FA by detection threshold θ</th>"
                     + "".join(f"<th>{th}</th>" for th in thetas) + "</tr>"]
            for c in CONDITIONS:
                sub = theta[theta["condition"] == c]
                cells = []
                for th in thetas:
                    center = ga.group_center(sub[sub["theta"] == th], "fa", ["theta"])
                    v = float(center["mean"].iloc[0]) * 100 if len(center) else np.nan
                    cells.append(f"<td align='center'>{_fmt(v, 0, '%')}</td>")
                t_tab.append(f"<tr><td><b>{c}</b></td>{''.join(cells)}</tr>")
            t_tab.append("</table>")
            theta_html = ("<p><b>Threshold sensitivity</b> (group mean of participant-level FA "
                          "under alternative detection thresholds; θ=0.40 is the main analysis): "
                          + "".join(t_tab) + "</p>")

        caption = (
            "<h3>Data quality & audit</h3>"
            "<p>Audit context only — none of these counters feed the outcome tabs. Excluded "
            "carry-over events were manually confirmed and are removed from every accuracy/RT "
            "statistic; suspected-but-unresolved carry-over still awaits a manual verdict; extra "
            "presses are unmatched raw MIDI presses (QC only). Unresolved/ambiguous rates and "
            "borderline counts describe finger-detection confidence on the video-analyzed trials.</p>"
            "<p>" + "".join(table) + "</p>" + theta_html)
        return caption, [fig]
