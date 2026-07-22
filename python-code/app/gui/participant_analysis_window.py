"""Single-participant cross-trial analysis - launcher section 7.

Pick a Main User Study participant and get the within-subject picture
across their 27 trials (3 conditions x 3 levels x 3 unique sequences):
condition and difficulty comparisons, learning progression (both across
the whole session and the trial 1 -> 3 order within each
condition-level cell), per-finger profiles, reaction-time
distributions, and a data-quality audit. Every figure carries a caption
with the computed numbers, so the tab is readable without the chart.

Data comes from app.participant_export.collect_participant_data - the
exact rows the CSV export writes - so charts, CSVs and the Quiz
Analysis table can never disagree. Everything here is descriptive and
within-subject; the cross-participant statistics (paired contrasts,
repeated-measures ANOVA) belong to the later multi-participant
analysis, not this window.
"""

from typing import Dict, List, Optional

import numpy as np
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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
LEVEL_SYMBOLS = {"alpha": "α", "beta": "β", "gamma": "γ"}
FINGER_ORDER = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]


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
        top.addWidget(self.save_figs_btn)

        self.tabs = QTabWidget()

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
            trials, events, missing = collect_participant_data(participant)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't load participant", f"{participant}: {e}")
            return
        if not trials:
            QMessageBox.information(self, "No data", f"{participant} has no completed trials with quiz data.")
            return

        analyzed = [t for t in trials if t["analyzed"]]
        note = f"{participant}: {len(trials)} trials loaded, {len(analyzed)} analyzed from video"
        if missing:
            note += f", {len(missing)} missing ({', '.join(missing)})"
        if len(analyzed) < len(trials):
            note += " — finger-based charts use analyzed trials only; run the video analysis for the rest."
        self.status_label.setText(note)

        self.tabs.clear()
        self._participant = participant
        # slug -> Figure; every registered figure is exported.
        self._figures: Dict[str, Figure] = {}
        # slug -> tidy DataFrame written next to the figures on export.
        self._datasets: Dict[str, object] = {}
        self._add_tab("Overview", *self._build_overview(participant, trials))
        self._add_tab("Learning", *self._build_learning(trials))
        self._add_tab("Difficulty", *self._build_difficulty(trials))
        self._add_tab("Trade-off", *self._build_tradeoff(trials))
        self._add_tab("Errors", *self._build_errors(trials, events))
        self._add_confusion_tab(trials, events)
        self._add_tab("Fingers", *self._build_fingers(events))
        self._add_tab("Timing", *self._build_timing(events))
        self._add_tab("Quality", *self._build_quality(trials))
        self.save_figs_btn.setEnabled(True)

    def _save_figures(self) -> None:
        """300 dpi PNG per figure plus the underlying tidy CSVs, all named
        <participant>_<slug>.* so the report can cite files verbatim."""
        out_dir = STUDY_DATA_DIR / self._participant / "figures"
        out_dir.mkdir(parents=True, exist_ok=True)
        figs = csvs = 0
        for slug, fig in self._figures.items():
            fig.savefig(out_dir / f"{self._participant}_{slug}.png", dpi=300, bbox_inches="tight")
            figs += 1
        for slug, df in self._datasets.items():
            df.to_csv(out_dir / f"{self._participant}_{slug}.csv", index=False)
            csvs += 1
        self.status_label.setText(f"Exported {figs} PNGs (300 dpi) + {csvs} CSVs to {out_dir}")

    def _add_tab(self, title: str, caption_html: str, figspecs: List[tuple]) -> None:
        """figspecs: list of (slug, Figure) - slug names the export files."""
        for slug, fig in figspecs:
            self._figures[slug] = fig
        content = QWidget()
        layout = QVBoxLayout(content)
        caption = QLabel(caption_html)
        caption.setWordWrap(True)
        caption.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(caption)
        for _slug, fig in figspecs:
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
            "Per-condition means (each over up to 9 trials: 3 difficulty levels × 3 unique sequences):"
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
        return "".join(f"<p>{line}</p>" for line in lines), [("overview", fig)]

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

        caption = (
            "<h3>Learning progression</h3>"
            "<p><b>Left figure pair — whole session:</b> every trial in presentation order, coloured by "
            "condition, with a least-squares trend line. "
            f"Overall FA trend {session_fa_slope:+.2f} pp/trial, RT trend {session_rt_slope:+.1f} ms/trial "
            "(negative RT slope = getting faster). Session-level trends mix conditions and difficulty, so read "
            "them as general familiarisation, not condition learning.</p>"
            "<p><b>Right figure pair — trial 1 → 3 within each condition-level cell:</b> the three unique "
            "sequences of a cell are averaged by their occurrence order. Because every sequence is seen only "
            "once, this is short-term exposure to the condition and difficulty, not sequence memorisation "
            "(the report's trial-order trend).</p>"
            "<p><b>1st → 3rd occurrence:</b><br>" + "<br>".join(improvements) + "</p>"
        )
        fig1.tight_layout()
        return caption, [("learning_session", fig1), ("learning_withincell", fig2)]

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
            parts = [f"{LEVEL_SYMBOLS[lv]} {_fmt_pct(fa)}" for lv, fa in zip(LEVELS, fa_vals)]
            summary_lines.append(f"{cond_titles[c]}: FA " + " / ".join(parts))
        for ax, ylabel, title in ((ax1, "FA main (%)", "Finger accuracy by difficulty"),
                                  (ax2, "RT (ms)", "Reaction time by difficulty")):
            ax.set_xticks(range(3), [LEVEL_SYMBOLS[lv] for lv in LEVELS])
            ax.set_xlabel("difficulty level")
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=8)
        fig.tight_layout()
        caption = (
            "<h3>Difficulty effect</h3>"
            "<p>Each point averages one condition-level cell (3 unique sequences). The three levels were "
            "generated as matched families with validated increasing motor/sequence/bimanual cost "
            "(α &lt; β &lt; γ), so a falling FA line or rising RT line means difficulty is biting; where the "
            "conditions separate is where guidance modality matters most for this participant.</p>"
            "<p>" + "<br>".join(summary_lines) + "</p>"
        )
        return caption, [("difficulty", fig)]

    # ------------------------------------------------------------------
    # Speed-accuracy trade-off

    def _build_tradeoff(self, trials: List[dict]):
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        df = compute_trial_speed_accuracy(trials)
        centroids = speed_accuracy_centroids(df)
        self._datasets["tradeoff_trials"] = df
        self._datasets["tradeoff_centroids"] = centroids
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
        return "".join(f"<p>{line}</p>" for line in lines), [("tradeoff", fig)]

    # ------------------------------------------------------------------
    # Event-level error breakdown (+ wrong-key distance)

    def _build_errors(self, trials: List[dict], events: List[dict]):
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        breakdown_df = compute_error_breakdown(events)
        self._datasets["error_breakdown"] = breakdown_df
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
        self._datasets["wrongkey_distance"] = distance_df
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
                     "Timeouts have no keypress and stay outside the key/finger cells. "
                     "Descriptive only — group-level inference is reported separately.")
        caption = "".join(f"<p>{line}</p>" for line in lines)
        return caption, [("errors_composition", fig_a),
                         ("errors_counts", fig_b),
                         ("errors_wrongkey_distance", fig_c)]

    # ------------------------------------------------------------------
    # Finger confusion matrices per condition

    def _add_confusion_tab(self, trials: List[dict], events: List[dict]) -> None:
        cond_titles = {c: self._condition_title(trials, c) for c in CONDITIONS}
        confusion_df = compute_finger_confusion(events)
        self._datasets["confusion_long"] = confusion_df
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
                bx.text(pos, bx.get_ylim()[0], f"{len(rts)}", ha="center", va="bottom", fontsize=6,
                        color="#555555")
        bx.set_xticks(range(len(FINGER_ORDER)), FINGER_ORDER)
        bx.set_ylabel("RT (ms)")
        bx.set_title("Per-finger RT distributions, B (blue) vs C (orange) - correct-key events; "
                     "n under each box, fingers with n<3 drawn as raw points", fontsize=9)
        bx.legend(handles=[Patch(facecolor=CONDITION_COLORS["B"], alpha=0.55, label="B"),
                           Patch(facecolor=CONDITION_COLORS["C"], alpha=0.55, label="C")], fontsize=8)
        fig2.tight_layout()
        if small_n_notes:
            caption_notes.append("small n (points, not boxes): " + ", ".join(small_n_notes))

        caption = (
            "<h3>Per-finger profiles</h3>"
            "<p>The report's main-figure view, computed within this participant: accuracy and RT stratified "
            "by target finger, in physical keyboard order (left pinky → right pinky), grouped by condition. "
            "Passive mechanical and neuromuscular coupling differ across digits, so a modality effect "
            "concentrated in the ring/little fingers shows up here while staying hidden in the overall mean. "
            "Bars use responded events only; unresolved finger verdicts count as incorrect.</p>"
            "<p>" + "; ".join(caption_notes) + "</p>"
        )
        return caption, [("fingers_accuracy", fig), ("fingers_rt_boxplot", fig2)]

    # ------------------------------------------------------------------
    # Timing

    def _build_timing(self, events: List[dict]):
        fig = Figure(figsize=(9, 3.8))
        ax1, ax2 = fig.subplots(1, 2)
        stats_lines = []
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
        fig.tight_layout()
        caption = (
            "<h3>Reaction-time distributions</h3>"
            "<p>Correct-key events only, pooled over all levels and sequences of each condition. The "
            "histogram shows shape (a long right tail usually means hesitation events rather than slow "
            "motor execution); the box plot shows spread and outliers. A condition can match on mean RT "
            "yet differ in consistency — SD and p95 carry that.</p>"
            "<p>" + "<br>".join(stats_lines) + "</p>"
        )
        return caption, [("timing_rt_distributions", fig)]

    # ------------------------------------------------------------------
    # Quality

    def _build_quality(self, trials: List[dict]):
        fig = Figure(figsize=(9, 3.4))
        ax = fig.subplots(1, 1)
        cats = ["Timeouts", "Wrong key", "Extra presses (QC)", "Manual corrections"]
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
        ax.set_xticks(x, cats)
        ax.set_ylabel("count (all trials of the condition)")
        ax.set_title("Error and audit counts by condition", fontsize=10)
        ax.legend(fontsize=8)
        fig.tight_layout()
        caption = (
            "<h3>Data quality & audit</h3>"
            "<p>Not an outcome — the trust context for every other tab. High unresolved/ambiguous rates "
            "mean the camera evidence is weak for those trials (check occlusion or sync); borderline events "
            "are the ones whose finger verdict sits within ±0.05 of the θ=0.40 threshold and were the "
            "manual-audit priority; sync methods show which trials rest on an auto-detected vs "
            "manually confirmed vs missing alignment.</p>"
            "<p>" + "<br>".join(lines) + "</p>"
        )
        return caption, [("quality_audit", fig)]
