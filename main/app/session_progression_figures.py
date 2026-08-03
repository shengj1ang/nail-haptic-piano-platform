"""Figures for difficulty-aligned group session progression.

Thin coloured lines are individual participants; thick marked lines are the
participant-weighted group means.  The three difficulty levels share one
occurrence axis (1--9) and are double-coded by colour and marker shape.
"""

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from .group_analysis import LEVEL_DISPLAY_LABELS, LEVELS


LEVEL_COLORS = {
    "alpha": "#3a76c4",
    "beta": "#d9663d",
    "gamma": "#4c956c",
}
LEVEL_MARKERS = {"alpha": "o", "beta": "^", "gamma": "s"}


def _plot_panel(ax, frame: pd.DataFrame, summary: pd.DataFrame, metric: str,
                scale: float, ylabel: str, title: str,
                ylim: Optional[Tuple[float, float]] = None) -> None:
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
    if ylim is not None:
        ax.set_ylim(*ylim)


def build_progression_figure(
        frame: pd.DataFrame,
        summary: pd.DataFrame,
        *,
        raw_metric: str,
        adjusted_metric: str,
        scale: float,
        metric_title: str,
        raw_ylabel: str,
        adjusted_ylabel: str,
        raw_ylim: Optional[Tuple[float, float]] = None) -> Figure:
    """Observed and composition-adjusted progression in aligned panels."""
    fig = Figure(figsize=(10.5, 4.6))
    raw_ax, adjusted_ax = fig.subplots(1, 2)
    _plot_panel(raw_ax, frame, summary, raw_metric, scale, raw_ylabel,
                "Observed progression by difficulty", raw_ylim)
    _plot_panel(adjusted_ax, frame, summary, adjusted_metric, scale,
                adjusted_ylabel, "Composition-adjusted progression by difficulty")

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
    fig.legend(handles=level_handles + style_handles, loc="upper center",
               ncols=5, fontsize=7.5, frameon=False, title="Difficulty")
    fig.suptitle(
        f"Difficulty-aligned session progression — {metric_title}",
        fontsize=11, y=0.90)
    fig.tight_layout(rect=(0, 0, 1, 0.82))
    return fig


def build_difficulty_progression_figures(
        frame: pd.DataFrame, summary: pd.DataFrame) -> Dict[str, Figure]:
    """Stable export slugs for the RT and comparable key-accuracy views."""
    return {
        "group_learning_difficulty_progression_rt": build_progression_figure(
            frame,
            summary,
            raw_metric="rt_correct_key_s_raw",
            adjusted_metric="rt_correct_key_s_adjusted",
            scale=1000,
            metric_title="correct-key reaction time",
            raw_ylabel="RT (ms)",
            adjusted_ylabel="adjusted RT (ms)",
        ),
        "group_learning_difficulty_progression_key_accuracy": (
            build_progression_figure(
                frame,
                summary,
                raw_metric="key_accuracy_raw",
                adjusted_metric="key_accuracy_adjusted",
                scale=100,
                metric_title="key accuracy",
                raw_ylabel="key accuracy (%)",
                adjusted_ylabel="adjusted key accuracy (%)",
                raw_ylim=(0, 105),
            )
        ),
    }
