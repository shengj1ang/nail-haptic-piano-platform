"""Figures for difficulty-aligned group session progression.

Thin coloured lines are individual participants; thick marked lines are the
participant-weighted group means.  The three difficulty levels share one
occurrence axis (1--9) and are double-coded by colour and marker shape.

Only observed trial values are drawn.  The composition-adjusted columns
stay in the tidy export so the condition-mix confound can still be checked
numerically, but no figure plots a transformed value: a reader must be able
to point at any line here and find it in the raw trial table.
"""

from typing import Dict

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from . import figure_prefs
from .group_analysis import LEVEL_DISPLAY_LABELS, LEVELS


LEVEL_COLORS = {
    "alpha": "#3a76c4",
    "beta": "#d9663d",
    "gamma": "#4c956c",
}
LEVEL_MARKERS = {"alpha": "o", "beta": "^", "gamma": "s"}


def _plot_panel(ax, frame: pd.DataFrame, summary: pd.DataFrame, metric: str,
                scale: float, ylabel: str, title: str) -> None:
    """Draw all participant trajectories and the three group means."""
    occurrences = sorted(frame["difficulty_occurrence"].unique())
    occ_arr = np.asarray(occurrences, dtype=float)
    for level in LEVELS:
        level_rows = frame[frame["level"] == level]
        color = LEVEL_COLORS[level]
        if figure_prefs.show_participant_traces:
            for _participant, participant_rows in level_rows.groupby("participant"):
                values = (participant_rows.set_index("difficulty_occurrence")[metric]
                          .reindex(occurrences).to_numpy(dtype=float) * scale)
                ax.plot(occurrences, values, color=color, linewidth=0.8,
                        alpha=0.20, zorder=1)

        mean_rows = (summary[(summary["level"] == level)
                             & (summary["metric"] == metric)]
                     .set_index("difficulty_occurrence")
                     .reindex(occurrences))
        if not figure_prefs.show_participant_traces:
            # Spread now comes from a 95% CI band rather than the traces.
            # Alpha is kept low (0.08) because all three difficulty levels
            # overlay their bands on one axis; near the accuracy ceiling
            # they overlap heavily and a heavier fill blends into one blob.
            lo = mean_rows["ci95_lo"].to_numpy(dtype=float) * scale
            hi = mean_rows["ci95_hi"].to_numpy(dtype=float) * scale
            mask = np.isfinite(lo) & np.isfinite(hi)
            if mask.any():
                ax.fill_between(occ_arr[mask], lo[mask], hi[mask],
                                color=color, alpha=0.08, linewidth=0, zorder=2)
        ax.plot(
            occurrences,
            mean_rows["mean"].to_numpy(dtype=float) * scale,
            color=color,
            marker=LEVEL_MARKERS[level],
            markersize=4.5,
            linewidth=2.6,
            zorder=3,
        )

    ax.set_xticks(occurrences)
    ax.set_xlabel("occurrence within difficulty (1–9)")
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10)
    ax.grid(axis="y", color="#dddddd", linewidth=0.6, alpha=0.7)


def build_progression_figure(frame: pd.DataFrame,
                             summary: pd.DataFrame) -> Figure:
    """Observed RT and key accuracy side by side on one occurrence axis.

    One row, not two stacked figures: the pair is read together, and the
    two panels carry different metrics (titles and units say which), so
    they cannot be mistaken for the same picture drawn twice.
    """
    fig = Figure(figsize=(10.5, 4.2))
    rt_ax, key_ax = fig.subplots(1, 2)
    _plot_panel(rt_ax, frame, summary, "rt_correct_key_s_raw", 1000,
                "Reaction Time (ms)", "Correct-key reaction time")
    _plot_panel(key_ax, frame, summary, "key_accuracy_raw", 100,
                "key accuracy (%)", "Key accuracy")
    # Both panels autoscale. This auxiliary figure is a within-difficulty
    # progression (a trend across occurrences 1–9), not a B/C gap, so its RT
    # axis is deliberately not pinned to zero the way the primary latency
    # panels are: the data sit around 580–1000 ms and a 0–~500 ms baseline
    # was all empty space, flattening the trend the figure exists to show.

    level_handles = [
        Line2D([0], [0], color=LEVEL_COLORS[level],
               marker=LEVEL_MARKERS[level], linewidth=2.4, markersize=5,
               label=LEVEL_DISPLAY_LABELS[level])
        for level in LEVELS
    ]
    # With the individual traces hidden, the only lines left are the three
    # coloured difficulty means, which the difficulty handles already key; a
    # lone black "group mean" swatch (no black line is ever drawn) just makes
    # a reader hunt for a line that is not there. Show the thin/thick style
    # key only when the traces are actually on.
    style_handles = []
    if figure_prefs.show_participant_traces:
        style_handles = [
            Line2D([0], [0], color="#777777", linewidth=0.8, alpha=0.45,
                   label="individual participant"),
            Line2D([0], [0], color="#333333", marker="o", linewidth=2.6,
                   markersize=4.5, label="group mean"),
        ]
    fig.suptitle(
        "Difficulty-aligned session progression — observed trials",
        fontsize=11, y=0.985)
    fig.legend(handles=level_handles + style_handles, loc="upper center",
               bbox_to_anchor=(0.5, 0.93), ncols=5, fontsize=7.5,
               frameon=False, title="Difficulty", title_fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.89))
    return fig


def build_difficulty_progression_figures(
        frame: pd.DataFrame, summary: pd.DataFrame) -> Dict[str, Figure]:
    """Stable export slug for the one-row RT + key-accuracy view."""
    return {
        "group_learning_difficulty_progression":
            build_progression_figure(frame, summary),
    }
