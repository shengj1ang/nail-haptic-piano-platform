"""Condition-A free-fingering tab for the group analysis window.

The computation lives in :mod:`app.condition_a_strategy`; this module only
turns its tidy tables into participant-weighted figures and an auditable
caption, returning the same ``(caption, figures, datasets)`` contract used by
the other group-analysis tabs.
"""

from collections import Counter
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from .. import condition_a_strategy as cas
from .. import figure_prefs
from ..figure_axes import zero_based_ylim


PARTICIPANT_LINE = "#aaaaaa"
LEVEL_COLORS = {"alpha": "#3a76c4", "beta": "#d9663d", "gamma": "#4c956c"}
LEVEL_MARKERS = {"alpha": "o", "beta": "^", "gamma": "s"}
LEVEL_LABELS = {"alpha": "α (alpha)", "beta": "β (beta)", "gamma": "γ (gamma)"}

# Self-reported handedness (TrialStructure.json) is descriptive participant
# metadata, so it is annotated on the participant axis rather than used to
# reorder, group or weight anything: the bars keep encoding observed hand use
# only, and the reader can compare the two.  Right-handers stay unmarked
# because they are the majority baseline.
# handedness value -> (axis mark, legend marker, legend wording)
HANDEDNESS_MARKS = {
    "left": ("*", "*", "self-reported left-hander"),
    "ambidextrous": ("†", "P", "self-reported ambidextrous"),
}
HANDEDNESS_MARK_COLOR = "#6a3d9a"
# The hidden generated fingering splits the keys almost evenly between the
# hands, so its per-participant left-hand share is the balanced baseline the
# free choice can be read against - a principled version of a plain 50% line,
# which is why it replaces one.
REFERENCE_COLOR = "#222222"


def _reported_handedness(hand_usage: pd.DataFrame) -> Dict[str, str]:
    """{participant: handedness} carried by the tidy table, if it has any.

    The column is optional so a caller holding an older frame (or a group
    whose TrialStructure.json files are unavailable) still plots, simply
    without marks.
    """
    if "handedness" not in hand_usage.columns:
        return {}
    reported = {}
    for participant, value in zip(hand_usage["participant"], hand_usage["handedness"]):
        text = str(value).strip().lower()
        if text and text != "nan":
            reported[str(participant)] = text
    return reported


def _reference_left_share(hand_usage: pd.DataFrame, participants) -> Optional[np.ndarray]:
    """Left-hand share the generated fingering would have produced, or None.

    The column is optional for the same reason ``handedness`` is: an older
    frame simply falls back to the plain 50% guide.
    """
    if "reference_share_pct" not in hand_usage.columns:
        return None
    reference = (hand_usage.pivot(index="participant", columns="hand",
                                  values="reference_share_pct")
                 .reindex(index=participants, columns=["L", "R"]))
    values = reference["L"].to_numpy(dtype=float)
    return values if np.isfinite(values).any() else None


def _handedness_phrase(value: str, count: int) -> str:
    return f"{count} {value if value == 'ambidextrous' else value + '-handed'}"


def _spatial_finger_tick_labels():
    return [
        f"{finger}\n{cas.FINGER_NAMES[int(finger[1])]}"
        for finger in cas.SPATIAL_FINGER_ORDER
    ]


def _progression_panel(ax, trials: pd.DataFrame, summary: pd.DataFrame,
                       metric: str, ylabel: str, title: str,
                       ylim=None) -> None:
    occurrences = [1, 2, 3]
    for level in cas.LEVELS:
        level_trials = trials[trials["level"] == level]
        color = LEVEL_COLORS[level]
        if figure_prefs.show_participant_traces:
            for _participant, rows in level_trials.groupby("participant"):
                values = (rows.set_index("level_occurrence")[metric]
                          .reindex(occurrences).to_numpy(dtype=float))
                ax.plot(occurrences, values, color=color,
                        linewidth=0.7, alpha=0.15, zorder=1)
        center = (summary[
            (summary["metric"] == metric) & (summary["level"] == level)
        ].set_index("level_occurrence").reindex(occurrences))
        means = center["mean"].to_numpy(dtype=float)
        lo = center["ci95_lo"].to_numpy(dtype=float)
        hi = center["ci95_hi"].to_numpy(dtype=float)
        ax.plot(
            occurrences, means, color=color, marker=LEVEL_MARKERS[level],
            linewidth=2.4, markersize=4.5, label=LEVEL_LABELS[level], zorder=3,
        )
        finite_interval = np.isfinite(lo) & np.isfinite(hi)
        if finite_interval.any():
            ax.fill_between(
                np.asarray(occurrences)[finite_interval],
                lo[finite_interval], hi[finite_interval],
                color=color, alpha=0.10, linewidth=0, zorder=2,
            )
    ax.set_xticks(
        occurrences,
        ["first\nA trial", "second\nA trial", "third\nA trial"],
    )
    ax.set_xlabel("three repetitions at the same difficulty")
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=9.5)
    ax.grid(axis="y", color="#dddddd", linewidth=0.6, alpha=0.7)
    if ylim is not None:
        ax.set_ylim(*ylim)


def _strategy_figure(trials: pd.DataFrame, usage_summary: pd.DataFrame,
                     level_occurrence: pd.DataFrame) -> Figure:
    fig = Figure(figsize=(10.5, 4.1))
    usage_ax, dominant_ax, effective_ax = fig.subplots(1, 3)

    ordered = (usage_summary.set_index("finger")
               .reindex(cas.SPATIAL_FINGER_ORDER))
    means = ordered["mean"].to_numpy(dtype=float)
    lo = ordered["ci95_lo"].to_numpy(dtype=float)
    hi = ordered["ci95_hi"].to_numpy(dtype=float)
    yerr = np.vstack([
        np.where(np.isfinite(lo), means - lo, 0),
        np.where(np.isfinite(hi), hi - means, 0),
    ])
    colors = ["#5b8db8" if finger.startswith("L") else "#d77a47"
              for finger in cas.SPATIAL_FINGER_ORDER]
    usage_ax.bar(np.arange(len(cas.SPATIAL_FINGER_ORDER)), means, color=colors,
                 edgecolor="white", linewidth=0.5, yerr=yerr,
                 error_kw={"linewidth": 0.8, "capsize": 2, "color": "#555555"})
    usage_ax.set_xticks(
        np.arange(len(cas.SPATIAL_FINGER_ORDER)),
        cas.SPATIAL_FINGER_ORDER,
        fontsize=8,
    )
    usage_ax.set_ylabel("mean share of resolved responses (%)")
    usage_ax.set_title("Which fingers were freely selected?", fontsize=9.5)
    usage_ax.grid(axis="y", color="#dddddd", linewidth=0.6, alpha=0.7)

    _progression_panel(
        dominant_ax, trials, level_occurrence, "dominant_share_pct",
        "dominant-finger share (%)", "Concentration within each trial",
        ylim=(0, 102),
    )
    _progression_panel(
        effective_ax, trials, level_occurrence, "effective_fingers",
        "effective number of fingers", "Finger diversity within each trial",
        ylim=(1, 10),
    )
    dominant_ax.legend(fontsize=6.8, frameon=False, loc="best")
    fig.suptitle("Condition A — free-fingering preference and strategy", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def _participant_hand_usage_figure(hand_usage: pd.DataFrame) -> Figure:
    """One 100%-stacked L/R bar per participant on a fixed-width canvas."""
    fig = Figure(figsize=(10.5, 4.0))
    ax = fig.subplots(1, 1)
    participants = sorted(hand_usage["participant"].unique())
    reported = _reported_handedness(hand_usage)
    marks = [HANDEDNESS_MARKS.get(reported.get(p, ""), ("", "", ""))[0]
             for p in participants]
    dense = len(participants) > 14
    bar_width = 0.52 if dense else 0.72
    value_fontsize = 5.5 if dense else 7
    pivot = (hand_usage.pivot(index="participant", columns="hand", values="share_pct")
             .reindex(index=participants, columns=["L", "R"]).fillna(0))
    x = np.arange(len(participants))
    left = pivot["L"].to_numpy(dtype=float)
    right = pivot["R"].to_numpy(dtype=float)
    ax.bar(x, left, color="#5b8db8", label="left hand", width=bar_width)
    ax.bar(x, right, bottom=left, color="#d77a47", label="right hand", width=bar_width)
    for position, (left_share, right_share) in enumerate(zip(left, right)):
        if left_share >= 8:
            label = f"{left_share:.0f}%" if dense else f"L {left_share:.1f}%"
            ax.text(position, left_share / 2, label,
                    ha="center", va="center", fontsize=value_fontsize, color="white")
        if right_share >= 8:
            label = f"{right_share:.0f}%" if dense else f"R {right_share:.1f}%"
            ax.text(position, left_share + right_share / 2, label,
                    ha="center", va="center", fontsize=value_fontsize, color="white")
    reference = _reference_left_share(hand_usage, participants)
    if reference is None:
        ax.axhline(50, color="#333333", linewidth=0.8, linestyle=":", alpha=0.75)
    else:
        for position, value in enumerate(reference):
            if np.isfinite(value):
                ax.plot([position - bar_width / 2, position + bar_width / 2],
                        [value, value], color=REFERENCE_COLOR, linewidth=1.6,
                        solid_capstyle="butt", zorder=3)
    ax.set_xticks(
        x,
        [f"{p} {mark}" if mark else p for p, mark in zip(participants, marks)],
    )
    if dense:
        ax.tick_params(axis="x", labelsize=6.5)
    for label, mark in zip(ax.get_xticklabels(), marks):
        if dense:
            label.set_rotation(45)
            label.set_horizontalalignment("right")
        if mark:
            label.set_color(HANDEDNESS_MARK_COLOR)
            label.set_fontweight("bold")
    ax.set_ylim(0, 100)
    ax.set_xlabel("participant")
    ax.set_ylabel("share of resolved Condition A responses (%)")
    ax.set_title("Condition A — left- versus right-hand use by participant",
                 fontsize=11, pad=24)
    ax.grid(axis="y", color="#dddddd", linewidth=0.6, alpha=0.5)
    handles, labels = ax.get_legend_handles_labels()
    if reference is not None:
        handles.append(Line2D([], [], color=REFERENCE_COLOR, linewidth=1.6))
        labels.append("hidden generated-fingering share")
    for value, (mark, marker, wording) in HANDEDNESS_MARKS.items():
        if value in reported.values():
            handles.append(Line2D([], [], linestyle="none", marker=marker,
                                  color=HANDEDNESS_MARK_COLOR, markersize=8))
            labels.append(f"{mark} {wording}")
    # The bars fill the whole 0-100% axis, so the legend sits above it: inside,
    # the orange key is invisible against the right-hand segments.
    ax.legend(handles, labels, frameon=False, ncols=len(labels), fontsize=8.5,
              loc="lower center", bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout()
    return fig


def _participant_finger_usage_figure(usage: pd.DataFrame) -> Figure:
    """Annotated participant × ten-finger percentage heatmap."""
    fig = Figure(figsize=(10.5, 5.0))
    ax = fig.subplots(1, 1)
    participants = sorted(usage["participant"].unique())
    dense = len(participants) > 16
    annotation_fontsize = 5.3 if dense else 6.5
    matrix = (usage.pivot(index="participant", columns="finger", values="share_pct")
              .reindex(index=participants, columns=cas.SPATIAL_FINGER_ORDER).fillna(0))
    values = matrix.to_numpy(dtype=float)
    maximum = float(np.nanmax(values)) if values.size else 1.0
    color_max = max(10.0, np.ceil(maximum / 10.0) * 10.0)
    image = ax.imshow(values, aspect="auto", cmap="YlGnBu", vmin=0, vmax=color_max)
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            value = values[row, column]
            text_color = "white" if value >= color_max * 0.52 else "#222222"
            ax.text(column, row, f"{value:.1f}", ha="center", va="center",
                    fontsize=annotation_fontsize, color=text_color)
    ax.axvline(4.5, color="white", linewidth=2.0)
    ax.set_xticks(
        np.arange(len(cas.SPATIAL_FINGER_ORDER)),
        _spatial_finger_tick_labels(),
    )
    ax.set_yticks(np.arange(len(participants)), participants)
    if dense:
        ax.tick_params(axis="y", labelsize=6.5)
    ax.set_xlabel("observed finger")
    ax.set_ylabel("participant")
    ax.set_title(
        "Condition A — each participant’s ten-finger usage (%)",
        fontsize=11,
        pad=28,
    )
    ax.text(0.25, 1.015, "Left hand", transform=ax.transAxes,
            ha="center", va="bottom", fontsize=8, color="#5b8db8")
    ax.text(0.75, 1.015, "Right hand", transform=ax.transAxes,
            ha="center", va="bottom", fontsize=8, color="#d77a47")
    colorbar = fig.colorbar(image, ax=ax, fraction=0.025, pad=0.025)
    colorbar.set_label("share of participant’s resolved responses (%)")
    fig.tight_layout()
    return fig


def _performance_figure(trials: pd.DataFrame,
                        level_occurrence: pd.DataFrame) -> Figure:
    fig = Figure(figsize=(10.5, 4.0))
    switch_ax, rt_ax, accuracy_ax = fig.subplots(1, 3)
    _progression_panel(
        switch_ax, trials, level_occurrence, "finger_switch_rate_pct",
        "finger-switch transitions (%)", "Digit switching",
        ylim=(0, 102),
    )
    _progression_panel(
        rt_ax, trials, level_occurrence, "median_correct_key_rt_ms",
        "median correct-key RT (ms)", "Response speed",
    )
    _progression_panel(
        accuracy_ax, trials, level_occurrence, "key_accuracy_pct",
        "key accuracy (%)", "Accuracy remains near ceiling",
        ylim=(90, 101),
    )
    # Response speed autoscales (~350-550 ms): a zero baseline wasted the
    # lower two-thirds of the panel. The accuracy panel keeps its 90-101%
    # window, where the per-repetition movement is actually visible.
    switch_ax.legend(fontsize=6.8, frameon=False, loc="best")
    fig.suptitle(
        "Condition A — strategy simplification alongside performance",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def _effort_figure(participant_effort: pd.DataFrame,
                   effort_summary: pd.DataFrame) -> Figure:
    fig = Figure(figsize=(10.5, 3.9))
    axes = fig.subplots(1, 3)
    specs = [
        ("same_hand_key_travel", "same-hand key travel\n(semitones)"),
        ("finger_transition_distance", "same-hand finger transition\n(finger steps)"),
        ("hand_switch_rate", "adjacent hand switches\n(%)"),
    ]
    x = np.arange(len(cas.LEVELS), dtype=float)
    offset = 0.07
    for ax, (metric, ylabel) in zip(axes, specs):
        rows = participant_effort[participant_effort["metric"] == metric]
        # The paired participant lines are the uncertainty display in this
        # figure: the summary table's CI is for actual - reference, not for
        # either endpoint separately. Keep the pairs in both trace modes so
        # the clean export never presents two bare means with no variability.
        for level_index, level in enumerate(cas.LEVELS):
            level_rows = rows[rows["level"] == level]
            for _participant, row in level_rows.groupby("participant"):
                ax.plot(
                    [level_index - offset, level_index + offset],
                    [float(row["generated_reference"].iloc[0]),
                     float(row["actual"].iloc[0])],
                    "-", color=PARTICIPANT_LINE, linewidth=0.7,
                    alpha=0.42, zorder=1,
                )
        metric_summary = (effort_summary[effort_summary["metric"] == metric]
                          .set_index("level").reindex(cas.LEVELS))
        reference = metric_summary["generated_reference_mean"].to_numpy(dtype=float)
        actual = metric_summary["actual_mean"].to_numpy(dtype=float)
        ax.plot(x - offset, reference, "s--", color="#d77a47",
                linewidth=2.3, markersize=5, label="hidden generated reference", zorder=3)
        ax.plot(x + offset, actual, "o-", color="#3a76c4",
                linewidth=2.6, markersize=5, label="observed free choice", zorder=3)
        ax.set_title(ylabel.replace("\n", " "), fontsize=9.5)
        ax.set_xticks(x, ["α\n(alpha)", "β\n(beta)", "γ\n(gamma)"])
        ax.set_xlabel("generated sequence difficulty")
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", color="#dddddd", linewidth=0.6, alpha=0.7)
        # These are motor-demand magnitudes with a real zero, and the
        # figure's whole claim is "observed sits below generated", so the
        # gap must be read against the full quantity, not a crop of it.
        zero_based_ylim(ax)
    effort_handles = [
        Line2D([0], [0], color="#d77a47", marker="s", linestyle="--",
               linewidth=2.3, markersize=5, label="hidden generated reference"),
        Line2D([0], [0], color="#3a76c4", marker="o",
               linewidth=2.6, markersize=5, label="observed free choice"),
        Line2D([0], [0], color=PARTICIPANT_LINE,
               linewidth=0.7, label="participant pair"),
    ]
    fig.legend(handles=effort_handles, loc="upper center",
               ncols=len(effort_handles), frameon=False, fontsize=8)
    fig.suptitle(
        "Condition A motor-demand proxies by generated difficulty",
        fontsize=11, y=0.91,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.79))
    return fig


def _summary_value(summary: pd.DataFrame, metric: str,
                   level: str, occurrence: int) -> float:
    row = summary[
        (summary["metric"] == metric)
        & (summary["level"] == level)
        & (summary["level_occurrence"] == occurrence)
    ]
    return float(row["mean"].iloc[0]) if len(row) else np.nan


def _caption(trials: pd.DataFrame, usage: pd.DataFrame,
             participants: pd.DataFrame, level_occurrence: pd.DataFrame,
             effort_summary: pd.DataFrame, hand_usage: pd.DataFrame) -> str:
    n_participants = int(trials["participant"].nunique())
    usage_means = usage.groupby("finger")["share_pct"].mean().reindex(cas.FINGERS)
    most_used = str(usage_means.idxmax())
    most_used_share = float(usage_means.max())
    index_share = float(usage_means.reindex(["L2", "R2"]).sum())
    dominant_counts = participants["dominant_finger"].value_counts()
    most_used_dominant_n = int(dominant_counts.get(most_used, 0))

    def _changes(metric: str, decimals: int) -> str:
        values = []
        for level in cas.LEVELS:
            first = _summary_value(level_occurrence, metric, level, 1)
            third = _summary_value(level_occurrence, metric, level, 3)
            values.append(
                f"{LEVEL_LABELS[level]} {first:.{decimals}f}→{third:.{decimals}f}")
        return "; ".join(values)

    dominant_changes = _changes("dominant_share_pct", 1)
    effective_changes = _changes("effective_fingers", 2)
    switch_changes = _changes("finger_switch_rate_pct", 1)
    rt_changes = _changes("median_correct_key_rt_ms", 0)
    accuracy_changes = _changes("key_accuracy_pct", 1)

    effort_by_metric = effort_summary.set_index(["metric", "level"])

    def _deltas(metric: str) -> str:
        values = [
            float(effort_by_metric.loc[(metric, level), "difference_mean"])
            for level in cas.LEVELS
        ]
        return "/".join(f"{value:+.2f}" for value in values)

    key_deltas = _deltas("same_hand_key_travel")
    finger_deltas = _deltas("finger_transition_distance")
    hand_deltas = _deltas("hand_switch_rate")
    coverage = float(trials["selection_coverage_pct"].mean())

    reported = _reported_handedness(hand_usage)
    handedness_note = ""
    if reported:
        counts = Counter(reported.values())
        breakdown = ", ".join(_handedness_phrase(value, counts[value])
                              for value in sorted(counts))
        marked = []
        for value, (mark, _marker, _wording) in HANDEDNESS_MARKS.items():
            named = sorted(p for p, v in reported.items() if v == value)
            if named:
                marked.append(f"{mark} = {value} ({', '.join(named)})")
        handedness_note = (
            " Self-reported handedness from each participant’s TrialStructure.json "
            f"({breakdown}) is annotated on the participant axis"
            + (f": {'; '.join(marked)}" if marked else
               ", where every included participant reported right-handedness")
            + ". It is descriptive metadata only: no bar is reordered, weighted or "
              "tested by it, and the exported hand-usage table carries the same column "
              "so the marking is auditable."
        )

    reference_note = ""
    left_rows = hand_usage[hand_usage["hand"] == "L"] if len(hand_usage) else hand_usage
    if "difference_pct" in left_rows and left_rows["difference_pct"].notna().any():
        lo = float(left_rows["reference_share_pct"].min())
        hi = float(left_rows["reference_share_pct"].max())
        reference_note = (
            " The black dash on each bar is that participant’s generated-fingering "
            f"left-hand share ({lo:.1f}–{hi:.1f}% here) — where the blue/orange boundary "
            "would sit had the free choice followed the hidden generated fingering on the "
            "same key sequences — so the gap to the boundary is observed − reference, "
            "exported as difference_pct."
        )
        if reported:
            group_means = left_rows.groupby("handedness")["difference_pct"].agg(
                ["mean", "count"])
            phrases = [
                f"{row['mean']:+.1f} pp".replace("-", "−")
                + f" for {value if value == 'ambidextrous' else value + '-handed'}"
                + f" participants (n = {int(row['count'])})"
                for value, row in group_means.iterrows()
            ]
            towards_left = int((left_rows["difference_pct"] > 0).sum())
            reference_note += (
                " Mean left-hand deviation was " + " and ".join(phrases) + "; "
                + ("no included participant deviated towards the left hand"
                   if not towards_left else
                   f"{towards_left} participant(s) deviated towards the left hand")
                + ". Group sizes here are very unequal and no test is applied, so this "
                "is a description of the figure, not a handedness effect."
            )

    return (
        "<h3>Condition A — free-fingering strategy</h3>"
        "<p><b>What is being analysed:</b> Condition A revealed the target key but not the "
        "generated target finger. The final reviewed <i>actual_finger</i> is therefore treated "
        "as a free choice, never as a correct/incorrect finger verdict. Invalid carry-over "
        f"events are excluded; resolved-finger coverage was {coverage:.1f}% across "
        f"{n_participants} participants.</p>"
        "<p><b>Finger preference:</b> participant-weighted use was concentrated on "
        f"{most_used} ({most_used_share:.1f}% of resolved responses); {most_used_dominant_n}/"
        f"{n_participants} participants used it most often overall. The two index fingers "
        f"(L2 + R2) together accounted for {index_share:.1f}% of use. Effective finger count "
        "is exp(Shannon entropy): 1 means one finger only and 10 means all ten equally.</p>"
        "<p><b>Participant distributions:</b> the next figure sums the five detected fingers "
        "within each hand to show every participant’s left/right usage; the following heatmap "
        "shows all ten finger percentages separately. Each row is normalised within that "
        "participant, while the exported tables retain both counts and percentages."
        f"{handedness_note}{reference_note}</p>"
        "<p><b>Change over A exposure, holding difficulty fixed:</b> first→third trial "
        f"dominant-finger share (%) was {dominant_changes}; effective finger count was "
        f"{effective_changes}; and finger-switch rate (%) was {switch_changes}. Median "
        f"correct-key RT (ms) was {rt_changes}, while key accuracy (%) was "
        f"{accuracy_changes}. The within-difficulty alignment prevents a changing α/β/γ "
        "mix from masquerading as strategy adaptation. Here first/second/third means the "
        "chronological order of that participant’s three Condition A trials at the same "
        "difficulty—not the first three keypress events. These co-occurring trends remain "
        "descriptive; the figure does not claim that finger concentration caused the RT "
        "change.</p>"
        "<p><b>Motor-demand proxies:</b> the hidden generated fingering is <i>not</i> a "
        "minimum-effort optimum; its motor demands intentionally rise from α to γ. It is used "
        "only as the same-sequence experimental reference. Actual − reference differences for "
        f"α/β/γ were {key_deltas} semitones for same-hand key travel, {finger_deltas} finger "
        f"steps for same-hand finger-transition distance, and {hand_deltas} percentage points "
        "for hand switching. Negative values mean free choice bypassed part of the generated "
        "motor demand. These are action-pattern proxies, not directly measured muscular effort; "
        "the paired lines remain descriptive.</p>"
    )


def build(event_rows: list, handedness: Optional[Dict[str, str]] = None
          ) -> Tuple[str, Dict[str, Figure], Dict[str, pd.DataFrame]]:
    """Return caption HTML, figures and tidy export tables for the tab.

    ``handedness`` is the self-reported {participant: value} metadata from
    app.group_analysis.participant_handedness; omitting it only drops the
    axis annotation.
    """
    trials = cas.condition_a_trial_strategy(event_rows)
    usage = cas.condition_a_finger_usage(event_rows)
    hand_usage = cas.participant_hand_usage(usage, handedness)
    participants = cas.participant_strategy_summary(trials, usage)
    occurrence = cas.occurrence_summary(trials)
    level_occurrence = cas.level_occurrence_summary(trials)
    usage_summary = cas.finger_usage_group_summary(usage)
    participant_effort = cas.participant_motor_effort(trials)
    effort_summary = cas.motor_effort_group_summary(participant_effort)

    if trials.empty:
        figure = Figure(figsize=(10.5, 2.2))
        ax = figure.subplots(1, 1)
        ax.text(0.5, 0.5, "No usable Condition A events", ha="center", va="center")
        ax.set_axis_off()
        return (
            "<h3>Condition A — free-fingering strategy</h3>"
            "<p>No usable Condition A events were available.</p>",
            {"group_condition_a_no_data": figure},
            {},
        )

    figures = {
        "group_condition_a_finger_strategy": _strategy_figure(
            trials, usage_summary, level_occurrence),
        "group_condition_a_hand_usage_by_participant": (
            _participant_hand_usage_figure(hand_usage)
        ),
        "group_condition_a_finger_usage_by_participant": (
            _participant_finger_usage_figure(usage)
        ),
        "group_condition_a_strategy_performance": _performance_figure(
            trials, level_occurrence),
        "group_condition_a_motor_demand": _effort_figure(
            participant_effort, effort_summary),
    }
    datasets = {
        "condition_a_trial_strategy": trials,
        "condition_a_finger_usage": usage,
        "condition_a_hand_usage": hand_usage,
        "condition_a_participant_summary": participants,
        "condition_a_occurrence_summary": occurrence,
        "condition_a_within_difficulty_summary": level_occurrence,
        "condition_a_finger_usage_group_summary": usage_summary,
        "condition_a_motor_demand_participants": participant_effort,
        "condition_a_motor_demand_group_summary": effort_summary,
    }
    return (
        _caption(trials, usage, participants, level_occurrence, effort_summary,
                 hand_usage),
        figures,
        datasets,
    )
