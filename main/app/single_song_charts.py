"""Figures for the Single Song Complexity Evaluation window.

Kept out of :mod:`app.gui.single_song_metrics_window` for the same reason
:mod:`app.single_song_metrics` is: the drawing can then be exercised
without a Qt event loop, and the window keeps only layout code.

Every figure here is a *view* of values that already exist on a
:class:`~app.single_song_metrics.SingleSongEvaluation`.  Nothing is
recomputed, and in particular nothing is aggregated: there is
deliberately no "constraints satisfied per level" bar chart, because
ranking the levels by pass count is exactly the coefficient-free
comparison the analysis refuses to make (see
``non_dominated_reference_levels``).  A reader who saw such a chart
would take the tallest bar for a classification.  The Pareto board
below therefore plots the set-valued result the code actually produces,
ties included.
"""

from typing import Dict, List, Tuple

from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from .sequence_generator import (
    LEVEL_CONSTRAINTS,
    LEVEL_SYMBOL,
    LEVELS,
    STRICT_LOWER,
    Sequence,
    SequenceStats,
)
from .single_song_metrics import (
    ALL_CONSTRAINT_KEYS,
    CONSTRAINT_LABELS,
    SingleSongEvaluation,
)

# One colour per difficulty level, used identically in all three figures
# so a reader can carry α/β/γ across them without a second look at the
# legend.  Blue/amber/red are the launcher's existing accent family
# (app/gui/quiz_style.py), here ordered easiest -> hardest.
LEVEL_COLORS: Dict[str, str] = {"alpha": "#3b7ddd", "beta": "#f0b429", "gamma": "#d05353"}
HAND_COLORS: Dict[str, str] = {"L": "#6b5bd2", "R": "#2a9d8f"}
INK = "#1c1c1e"
MUTED = "#8a8c93"
GRID = "#d7d9de"
TITLE_COLOR = "#3c3f46"

GROUP_TITLES: Dict[str, str] = {
    "C_m": "C_m motor",
    "C_s": "C_s sequence",
    "C_c": "C_c coordination",
}


def satisfies(level: str, key: str, value: float) -> bool:
    """Whether ``value`` lies inside one level's interval for ``key``.

    Mirrors the generator's own inclusive/strict-lower semantics so the
    solid/faded bands in :func:`constraint_band_figure` cannot disagree
    with the pass/violation rows the table shows.
    """
    lo, hi = LEVEL_CONSTRAINTS[level][key]
    lower_ok = value > lo if key in STRICT_LOWER[level] else value >= lo
    return lower_ok and value <= hi


def _strip_frame(ax, keep: Tuple[str, ...] = ()) -> None:
    for name, spine in ax.spines.items():
        spine.set_visible(name in keep)


def constraint_band_figure(stats: SequenceStats) -> Figure:
    """One row per constraint: the three intervals, and where the song sits.

    Each row is scaled by its own largest upper bound, because the eleven
    constraints have unrelated units (semitone ratios, a finger-number
    change, entropies).  That per-row normalisation is a drawing choice
    only: no value is compared with a value from another row, which is
    the same rule ``_violation_costs`` follows.
    """
    keys: List[str] = list(ALL_CONSTRAINT_KEYS)
    rows = len(keys)
    fig = Figure(figsize=(9.3, 0.52 * rows + 1.35))
    ax = fig.add_subplot(111)
    row_h = 0.26

    for index, key in enumerate(keys):
        y0 = rows - 1 - index
        value = stats.metric(key)
        # A zero-width interval (α's O_LR, α/β's X_f) still has to be
        # visible, hence the floor on the drawn width.
        scale = max(max(LEVEL_CONSTRAINTS[level][key][1] for level in LEVELS), value, 1e-9)
        if index % 2 == 0:
            ax.axhspan(y0 - 0.5, y0 + 0.5, color="#000000", alpha=0.035, zorder=0)
        for offset, level in enumerate(LEVELS):
            lo, hi = LEVEL_CONSTRAINTS[level][key]
            inside = satisfies(level, key, value)
            ax.barh(
                y0 + (1 - offset) * row_h,
                max((hi - lo) / scale, 0.010),
                left=lo / scale,
                height=row_h * 0.70,
                color=LEVEL_COLORS[level],
                alpha=0.95 if inside else 0.18,
                edgecolor=LEVEL_COLORS[level],
                linewidth=0.9,
                zorder=2,
            )
        marker = value / scale
        ax.plot(
            [marker, marker],
            [y0 - row_h * 1.15, y0 + row_h * 1.15],
            color=INK,
            lw=1.7,
            solid_capstyle="butt",
            zorder=5,
        )
        ax.text(
            1.045,
            y0,
            f"{value:.3f}",
            va="center",
            ha="right",
            fontsize=8.5,
            family="monospace",
            transform=ax.get_yaxis_transform(),
        )

    ax.set_yticks(range(rows))
    ax.set_yticklabels([CONSTRAINT_LABELS[key] for key in reversed(keys)], fontsize=9)
    ax.tick_params(axis="y", length=0)
    ax.set_xticks([])
    ax.set_xlim(-0.01, 1.05)
    ax.set_ylim(-0.5, rows - 0.5)
    _strip_frame(ax)
    ax.set_title(
        "Each metric against the three reference intervals\n"
        "Black tick = this song. A solid band means the value falls inside that level's interval.",
        fontsize=10,
        loc="left",
        color=TITLE_COLOR,
    )
    handles: List = [
        Patch(facecolor=LEVEL_COLORS[level], label=f"{LEVEL_SYMBOL[level]} {level}")
        for level in LEVELS
    ]
    handles.append(Line2D([], [], color=INK, lw=1.7, label="this song"))
    fig.legend(handles=handles, ncol=4, fontsize=9, frameon=False,
               loc="lower center", bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.055, 1, 1))
    return fig


def contour_figure(actions: Sequence, stats: SequenceStats) -> Figure:
    """The parsed sequence as a contour instead of two lines of text.

    Shows in one picture what the finger/note text boxes spell out: the
    order of the events, which hand plays each, the finger used, and how
    far the two hands' note ranges reach into one another (O_LR).
    """
    fig = Figure(figsize=(9.3, 3.3))
    ax = fig.add_subplot(111)
    xs = list(range(1, len(actions) + 1))
    present = [hand for hand in ("L", "R") if any(a.hand == hand for a in actions)]

    for hand in present:
        notes = [a.note for a in actions if a.hand == hand]
        ax.axhspan(min(notes), max(notes), color=HAND_COLORS[hand], alpha=0.08, zorder=0)
    ax.plot(xs, [a.note for a in actions], color="#b9bcc4", lw=1.1, zorder=1)
    for hand in present:
        ax.scatter(
            [x for x, a in zip(xs, actions) if a.hand == hand],
            [a.note for a in actions if a.hand == hand],
            s=56,
            color=HAND_COLORS[hand],
            edgecolor="white",
            linewidth=0.9,
            zorder=3,
        )
    for x, action in zip(xs, actions):
        ax.annotate(str(action.finger), (x, action.note), color="white", fontsize=6.5,
                    ha="center", va="center", zorder=4)

    midpoint = (stats.k_min + stats.k_max) / 2
    ax.axhline(midpoint, color=MUTED, ls="--", lw=0.9, zorder=2)
    ax.set_xlabel("cue event", fontsize=9)
    ax.set_ylabel("MIDI note", fontsize=9)
    ax.set_title(
        "Note contour, hand assignment and the two hand regions\n"
        "The number inside a marker is the finger. Shaded bands are each hand's own "
        "note range; how far they overlap is O_LR.",
        fontsize=10,
        loc="left",
        color=TITLE_COLOR,
    )
    handles = [
        Line2D([], [], marker="o", ls="", color=HAND_COLORS[hand],
               label="left hand" if hand == "L" else "right hand")
        for hand in present
    ]
    handles.append(Line2D([], [], ls="--", color=MUTED, label=f"keyboard midpoint ({midpoint:g})"))
    fig.legend(handles=handles, ncol=len(handles), fontsize=9, frameon=False,
               loc="lower center", bbox_to_anchor=(0.5, 0.0))
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    _strip_frame(ax, keep=("left", "bottom"))
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    return fig


def pareto_board_figure(result: SingleSongEvaluation) -> Figure:
    """The set-valued Pareto outcome per C-group, drawn as it is computed.

    A filled disc is a level ``non_dominated_reference_levels`` kept; a
    hollow one is a level some other level dominated on that group's
    constraints.  Two or three filled discs in a row is the honest
    "mixed" result, not a rendering fault, so the caption says so.
    """
    profiles = {profile.group: profile for profile in result.group_reference_profiles}
    groups = ["C_m", "C_s", "C_c"]
    fig = Figure(figsize=(9.3, 2.7))
    ax = fig.add_subplot(111)

    for row, group in enumerate(groups):
        profile = profiles[group]
        y = len(groups) - 1 - row
        if not profile.comparable:
            # One sentence across the whole row rather than the same two
            # words under each of the three columns: nothing was compared
            # here, so there is nothing per-level to say.
            ax.axhspan(y - 0.42, y + 0.42, color="#000000", alpha=0.035, zorder=1)
            ax.text((len(LEVELS) - 1) / 2, y, f"not comparable — {profile.limitation}",
                    ha="center", va="center", fontsize=8.5, color=MUTED, zorder=3)
            continue
        for column, level in enumerate(LEVELS):
            x = column
            kept = level in profile.non_dominated_levels
            ax.scatter(
                [x], [y],
                s=1150,
                color=LEVEL_COLORS[level] if kept else "#ffffff",
                edgecolor=LEVEL_COLORS[level] if kept else "#c8cad0",
                linewidth=1.6,
                zorder=2,
            )
            ax.text(x, y, "kept" if kept else "dropped", ha="center", va="center",
                    fontsize=7.5, color="#ffffff" if kept else MUTED, zorder=3)

    ax.set_xticks(range(len(LEVELS)), [f"{LEVEL_SYMBOL[l]} ({l})" for l in LEVELS], fontsize=9.5)
    ax.set_yticks(range(len(groups)), [GROUP_TITLES[g] for g in reversed(groups)], fontsize=9.5)
    ax.set_xlim(-0.6, len(LEVELS) - 0.4)
    ax.set_ylim(-0.6, len(groups) - 0.4)
    ax.tick_params(length=0)
    ax.grid(color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    _strip_frame(ax)
    ax.set_title(
        "Pareto result per component group\n"
        "A level is dropped only when another is no worse on every constraint of that group and "
        "strictly better on one.\nSeveral kept levels is a genuine tie, not a missing answer.",
        fontsize=10,
        loc="left",
        color=TITLE_COLOR,
    )
    fig.tight_layout()
    return fig
