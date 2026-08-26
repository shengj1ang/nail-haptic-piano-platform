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

import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from .figure_axes import zero_based_ylim
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
    for level in LEVELS:
        level_rows = frame[frame["level"] == level]
        color = LEVEL_COLORS[level]
        for _participant, participant_rows in level_rows.groupby("participant"):
            values = (participant_rows.set_index("difficulty_occurrence")[metric]
                      .reindex(occurrences).to_numpy(dtype=float) * scale)
            ax.plot(occurrences, values, color=color, linewidth=0.8,
                    alpha=0.20, zorder=1)

        mean_rows = (summary[(summary["level"] == level)
                             & (summary["metric"] == metric)]
                     .set_index("difficulty_occurrence")
                     .reindex(occurrences))
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
                "RT (ms)", "Correct-key reaction time")
    _plot_panel(key_ax, frame, summary, "key_accuracy_raw", 100,
                "key accuracy (%)", "Key accuracy")
    # RT from zero; key accuracy autoscales to its own near-ceiling range,
    # the same rule the other accuracy panels follow.
    zero_based_ylim(rt_ax)

    level_handles = [
        Line2D([0], [0], color=LEVEL_COLORS[level],
               marker=LEVEL_MARKERS[level], linewidth=2.4, markersize=5,
               label=LEVEL_DISPLAY_LABELS[level])
        for level in LEVELS
    ]
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
