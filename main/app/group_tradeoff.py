"""GUI-free data, colour and figure layer behind the Group Analysis
Trade-off tab (app/gui/group_analysis_window.py).

Metric definitions are IDENTICAL to the single-participant Trade-off tab:
every trial point is app.participant_analysis.compute_trial_speed_accuracy
output (x = mean RT of the trial's correct-key events in ms under the
existing timeout/carry-over/correct-key filtering; y = Main Finger Accuracy
for B/C and hidden-target finger agreement for A), so the two windows can
never disagree. Trials without a valid RT or without an analysed finger
outcome are excluded and reported, never plotted as 0. Condition A remains
descriptive context and never enters the planned B/C inference.

Aggregation hierarchy (participants are the only independent unit):

  trial points          -> participant x condition (x level) centroids
  participant centroids -> group centroids, 95% t-CI over PARTICIPANTS

Group centroids and their intervals are always computed from the
participant-level centroids (each participant enters with equal weight);
events or trials are never pooled across participants for a group mean
or CI, and cells with fewer than 2 participants get a mean but no
interval (app.group_analysis.group_center enforces both).

Everything here is descriptive - the inferential statistics stay on the
Contrasts tab. No Qt import anywhere so the tests can run headless; the
figures are plain matplotlib.figure.Figure objects the window wraps in
canvases.
"""

import colorsys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from .group_analysis import (
    CONDITIONS,
    LEVELS,
    LEVEL_DISPLAY_LABELS,
    group_center,
)
from .participant_analysis import compute_trial_speed_accuracy

# Base condition hues - the same values as the GUI palette
# (app.gui.participant_analysis_window.CONDITION_COLORS); the window
# passes its own palette into the builders, this copy only serves
# GUI-free callers (tests) without importing Qt.
CONDITION_COLORS = {"A": "#8a8a8a", "B": "#3a76c4", "C": "#d9663d"}

# Discrete z positions of the difficulty axis; tick labels show the
# symbols, never the raw integers.
LEVEL_Z = {"alpha": 0, "beta": 1, "gamma": 2}

# Marker shapes double-code difficulty (3D-by-difficulty figure) so the
# planes stay tellable apart in print / with impaired colour vision.
LEVEL_MARKERS = {"alpha": "o", "beta": "^", "gamma": "s"}

# In the 3D-by-participant figure the marker codes the condition
# (colour lightness codes the participant there).
CONDITION_MARKERS = {"A": "o", "B": "s", "C": "D"}

# Lightness levels of the single shade() generator below. alpha stays
# dark enough (< 0.75) to be visible on a white background.
LEVEL_LIGHTNESS = {"alpha": 0.72, "beta": 0.55, "gamma": 0.35}
_PARTICIPANT_LIGHTNESS_RANGE = (0.74, 0.32)  # first participant lightest

FA_LIMITS = (0.0, 105.0)  # % axis: 0-100 plus slight error-bar padding


# ---------------------------------------------------------------------------
# Colour variants (single generator - no hand-picked shades anywhere)

def shade(base_hex: str, lightness: float) -> str:
    """The one colour-variant generator: keeps the hue/saturation of a
    condition base colour and replaces its lightness (0 black, 1 white),
    clamped to [0, 1] so out-of-range requests can never crash."""
    r, g, b = (int(base_hex[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, _l, s = colorsys.rgb_to_hls(r, g, b)
    lightness = min(max(float(lightness), 0.0), 1.0)
    r2, g2, b2 = colorsys.hls_to_rgb(h, lightness, s)
    return "#{:02x}{:02x}{:02x}".format(round(r2 * 255), round(g2 * 255), round(b2 * 255))


def level_color(condition: str, level: str,
                palette: Optional[Dict[str, str]] = None) -> str:
    """Difficulty variant of the condition hue: α light, β mid, γ dark."""
    palette = palette or CONDITION_COLORS
    return shade(palette[condition], LEVEL_LIGHTNESS[level])


def participant_lightness(index: int, n: int) -> float:
    """Evenly spaced lightness for participant `index` of `n` (given
    display order): the first participant is the lightest. Values stay
    inside a fixed visible band for ANY n, so growing the sample never
    yields white/black or out-of-range colours."""
    hi, lo = _PARTICIPANT_LIGHTNESS_RANGE
    if n <= 1:
        return (hi + lo) / 2
    return float(np.linspace(hi, lo, n)[index])


def participant_color(condition: str, index: int, n: int,
                      palette: Optional[Dict[str, str]] = None) -> str:
    """Participant variant of the condition hue. The lightness step
    depends only on (index, n), so one participant keeps the same
    variant rank across A/B/C."""
    palette = palette or CONDITION_COLORS
    return shade(palette[condition], participant_lightness(index, n))


# ---------------------------------------------------------------------------
# Data: trial points -> participant centroids -> group centroids

def trial_points(trial_rows: List[dict]) -> pd.DataFrame:
    """One row per participant x condition x trial under the
    single-participant definitions (compute_trial_speed_accuracy):
    rt_ms / fa_pct / included, plus the trial's valid event count
    (targets minus the confirmed carry-over exclusions)."""
    if not trial_rows:  # empty selection: keep the schema, never KeyError
        return pd.DataFrame(columns=["participant", "condition", "trial_index",
                                     "level", "sequence", "rt_ms", "fa_pct",
                                     "included", "valid_event_count"])
    df = compute_trial_speed_accuracy(trial_rows)
    df["valid_event_count"] = [
        int((t.get("note_count") or 0) - (t.get("excluded_carryover") or 0))
        for t in trial_rows
    ]
    return df


_PCENT_COLS = ["n_trials", "rt_ms", "fa_pct", "valid_event_count"]


def participant_centroids(points: pd.DataFrame, by_level: bool = False) -> pd.DataFrame:
    """Centroid of each participant's INCLUDED trials per condition
    (optionally per condition x level): plain means of rt_ms / fa_pct.
    Cells without any included trial produce no row - never a zero."""
    keys = ["participant", "condition"] + (["level"] if by_level else [])
    inc = points[points["included"]] if len(points) else points
    if not len(inc):
        return pd.DataFrame(columns=keys + _PCENT_COLS)
    return (inc.groupby(keys, sort=True)
               .agg(n_trials=("rt_ms", "size"),
                    rt_ms=("rt_ms", "mean"),
                    fa_pct=("fa_pct", "mean"),
                    valid_event_count=("valid_event_count", "sum"))
               .reset_index())


def group_centroids(pcent: pd.DataFrame, by_level: bool = False) -> pd.DataFrame:
    """Group centroid per condition (x level): mean and 95% t-CI of the
    PARTICIPANT centroids (n = participants; CI absent below n = 2).
    Never computed from pooled trials or events - that would treat
    dependent observations as independent samples."""
    keys = ["condition"] + (["level"] if by_level else [])
    rt = (group_center(pcent, "rt_ms", keys)
          .rename(columns={"n": "n_participants", "mean": "rt_ms", "sd": "rt_sd",
                           "ci95_lo": "rt_ci95_lo", "ci95_hi": "rt_ci95_hi"})
          .drop(columns=["sem"]))
    fa = (group_center(pcent, "fa_pct", keys)
          .rename(columns={"mean": "fa_pct", "sd": "fa_sd",
                           "ci95_lo": "fa_ci95_lo", "ci95_hi": "fa_ci95_hi"})
          .drop(columns=["sem", "n"]))
    return rt.merge(fa, on=keys, how="outer")


@dataclass
class TradeoffData:
    """Everything the tab and the export need, computed once per Analyse."""

    participants: List[str] = field(default_factory=list)
    conditions: List[str] = field(default_factory=list)
    points: pd.DataFrame = None
    pcent: pd.DataFrame = None          # participant x condition
    pcent_level: pd.DataFrame = None    # participant x condition x level
    gcent: pd.DataFrame = None          # condition
    gcent_level: pd.DataFrame = None    # condition x level


def compute(trial_rows: List[dict], participants: List[str]) -> TradeoffData:
    """All trade-off aggregates for the included participants, in the
    display order the caller passes (the window's existing participant
    ordering - it is reused verbatim, never re-sorted here)."""
    points = trial_points(trial_rows)
    pcent = participant_centroids(points)
    pcent_level = participant_centroids(points, by_level=True)
    return TradeoffData(
        participants=list(participants),
        conditions=[c for c in CONDITIONS
                    if len(points) and (points["condition"] == c).any()],
        points=points,
        pcent=pcent,
        pcent_level=pcent_level,
        gcent=group_centroids(pcent),
        gcent_level=group_centroids(pcent_level, by_level=True),
    )


# ---------------------------------------------------------------------------
# Descriptive summary + missing cells (reported, never zero-filled)

def group_summary(gcent: pd.DataFrame) -> dict:
    """Per-condition group means plus the C−B deltas. The verdict
    sentence appears ONLY when C is descriptively both faster and more
    accurate than B on the group means - no significance claim ever."""
    conds: Dict[str, dict] = {}
    if len(gcent):
        for r in gcent.itertuples():
            if not np.isnan(r.rt_ms) or not np.isnan(r.fa_pct):
                conds[r.condition] = {"n": int(r.n_participants),
                                      "rt_ms": float(r.rt_ms), "fa_pct": float(r.fa_pct)}
    delta = verdict = None
    if "B" in conds and "C" in conds:
        delta = {"rt_ms": conds["C"]["rt_ms"] - conds["B"]["rt_ms"],
                 "fa_pct": conds["C"]["fa_pct"] - conds["B"]["fa_pct"]}
        if delta["rt_ms"] < 0 and delta["fa_pct"] > 0:
            verdict = "C is descriptively faster and more accurate than B."
    return {"conditions": conds, "delta_c_minus_b": delta, "verdict": verdict}


def missing_cells(data: TradeoffData) -> List[str]:
    """participant x condition x level cells contributing no included
    trial, formatted for the caption (e.g. "P02 B/β (beta)")."""
    inc = data.points[data.points["included"]] if len(data.points) else data.points
    have = {(r.participant, r.condition, r.level) for r in inc.itertuples()}
    return [f"{p} {c}/{LEVEL_DISPLAY_LABELS[lv]}"
            for p in data.participants for c in data.conditions for lv in LEVELS
            if (p, c, lv) not in have]


# ---------------------------------------------------------------------------
# Export datasets (the three tidy CSVs)

def _difficulty(levels) -> pd.Series:
    """Machine-readable difficulty names for CSV export.

    Keep alpha/beta/gamma as the data values; A/B/C belong exclusively to
    the separate condition column.  Figures add the Greek symbols through
    LEVEL_DISPLAY_LABELS.
    """
    return pd.Series([str(level) for level in levels], dtype=object)


def export_datasets(data: TradeoffData) -> Dict[str, pd.DataFrame]:
    """slug -> tidy DataFrame for Export figures + data. Centroid rows
    carry difficulty "all" where they aggregate over the three levels."""
    points = data.points
    trial_df = pd.DataFrame({
        "participant_id": points["participant"],
        "condition": points["condition"],
        "difficulty": _difficulty(points["level"]),
        "trial_order": points["trial_index"],
        "mean_rt_correct_key_ms": points["rt_ms"],
        "main_finger_accuracy": points["fa_pct"],
        "valid_event_count": points["valid_event_count"],
        "included": points["included"],
        "centroid_level": "trial",
    })

    def pcent_frame(df: pd.DataFrame, with_level: bool) -> pd.DataFrame:
        return pd.DataFrame({
            "participant_id": df["participant"],
            "condition": df["condition"],
            "difficulty": _difficulty(df["level"]) if with_level
            else pd.Series(["all"] * len(df), dtype=object),
            "n_trials": df["n_trials"],
            "mean_rt_correct_key_ms": df["rt_ms"],
            "main_finger_accuracy": df["fa_pct"],
            "valid_event_count": df["valid_event_count"],
            "centroid_level": "participant",
        })

    def gcent_frame(df: pd.DataFrame, with_level: bool) -> pd.DataFrame:
        return pd.DataFrame({
            "condition": df["condition"],
            "difficulty": _difficulty(df["level"]) if with_level
            else pd.Series(["all"] * len(df), dtype=object),
            "n_participants": df["n_participants"],
            "mean_rt_correct_key_ms": df["rt_ms"],
            "rt_sd": df["rt_sd"],
            "rt_ci95_lo": df["rt_ci95_lo"],
            "rt_ci95_hi": df["rt_ci95_hi"],
            "main_finger_accuracy": df["fa_pct"],
            "fa_sd": df["fa_sd"],
            "fa_ci95_lo": df["fa_ci95_lo"],
            "fa_ci95_hi": df["fa_ci95_hi"],
            "centroid_level": "group",
        })

    return {
        "group_tradeoff_trial_points": trial_df,
        "group_tradeoff_participant_centroids": pd.concat(
            [pcent_frame(data.pcent, False), pcent_frame(data.pcent_level, True)],
            ignore_index=True),
        "group_tradeoff_group_centroids": pd.concat(
            [gcent_frame(data.gcent, False), gcent_frame(data.gcent_level, True)],
            ignore_index=True),
    }


# ---------------------------------------------------------------------------
# Figures

@dataclass
class TradeoffOptions:
    """Display toggles (defaults: everything a small N can afford)."""

    show_trial_points: bool = True
    show_participant_centroids: bool = True
    show_group_centroids: bool = True
    connect_b_to_c: bool = True
    # Group-centroid error bars: "ci" = 95% t-CI over participants (the
    # inferentially honest default - huge at N = 2 by construction),
    # "sd" = ±SD over participants (descriptive spread, stays readable
    # at small N). Both are participant-level; trials are never pooled.
    error_bars: str = "ci"


def error_bounds(row, axis: str, mode: str):
    """(lo, hi) of a group-centroid error bar. axis: "rt" (ms) or "fa"
    (%); mode: "ci" or "sd" (TradeoffOptions.error_bars). None when the
    cell has no spread value (fewer than 2 participants) - the caller
    then draws the mean without a bar, never a fabricated interval."""
    mean_col, prefix = ("rt_ms", "rt") if axis == "rt" else ("fa_pct", "fa")
    if mode == "sd":
        sd = float(row[f"{prefix}_sd"])
        if np.isnan(sd):
            return None
        return float(row[mean_col]) - sd, float(row[mean_col]) + sd
    lo, hi = float(row[f"{prefix}_ci95_lo"]), float(row[f"{prefix}_ci95_hi"])
    if np.isnan(lo):
        return None
    return lo, hi


def error_bar_label(mode: str) -> str:
    return "95% t-CI" if mode == "ci" else "SD"


def shared_rt_limits(points: pd.DataFrame) -> tuple:
    """One zero-based x-range for all three figures so they compare directly.

    The axis starts at 0 ms rather than at the fastest trial: a cropped
    RT axis makes the gap between two conditions look like whatever the
    crop chooses, and these three figures are the ones a reader uses to
    judge how large the haptic advantage is.
    """
    inc = points[points["included"]] if len(points) else points
    if not len(inc):
        return (0.0, 1000.0)
    hi = float(inc["rt_ms"].max())
    return (0.0, hi * 1.05)


_X_LABEL = "Mean RT of correct-key events (ms)"
_Y_LABEL_2D = "Finger outcome (%)\n(B/C: Main FA; A: hidden-target agreement)"
_Y_LABEL_3D = "Finger outcome (%)"


def _cond_label(cond_titles: Optional[Dict[str, str]], c: str) -> str:
    return cond_titles[c] if cond_titles and c in cond_titles else c


def build_group_tradeoff_2d(data: TradeoffData,
                            cond_titles: Optional[Dict[str, str]] = None,
                            colors: Optional[Dict[str, str]] = None,
                            options: Optional[TradeoffOptions] = None,
                            xlim: Optional[tuple] = None) -> Figure:
    """Main figure: trial points (small, translucent), participant
    centroids (hollow rings), group centroids (large diamonds with
    participant-level 95% t-CI bars). No regression / connecting lines
    on purpose. Group centroids without a CI (n < 2 participants) still
    plot - they are descriptive either way."""
    colors = colors or CONDITION_COLORS
    options = options or TradeoffOptions()
    xlim = xlim or shared_rt_limits(data.points)

    fig = Figure(figsize=(10.5, 5.4))
    ax = fig.subplots(1, 1)
    for c in data.conditions:
        if options.show_trial_points:
            pts = data.points[(data.points["condition"] == c) & data.points["included"]]
            ax.scatter(pts["rt_ms"], pts["fa_pct"], s=16, alpha=0.35,
                       color=colors[c], linewidths=0, zorder=2)
        if options.show_participant_centroids:
            pc = data.pcent[data.pcent["condition"] == c]
            ax.scatter(pc["rt_ms"], pc["fa_pct"], s=70, facecolors="none",
                       edgecolors=colors[c], linewidths=1.4, zorder=3)
        if options.show_group_centroids:
            row = data.gcent[data.gcent["condition"] == c]
            if len(row) and not np.isnan(float(row["rt_ms"].iloc[0])):
                r = row.iloc[0]
                x_b = error_bounds(r, "rt", options.error_bars)
                y_b = error_bounds(r, "fa", options.error_bars)
                if x_b or y_b:
                    ax.errorbar(
                        [r["rt_ms"]], [r["fa_pct"]],
                        xerr=[[r["rt_ms"] - x_b[0]], [x_b[1] - r["rt_ms"]]] if x_b else None,
                        yerr=[[r["fa_pct"] - y_b[0]], [y_b[1] - r["fa_pct"]]] if y_b else None,
                        color="black", capsize=4, linewidth=1.3, zorder=4)
                ax.scatter([r["rt_ms"]], [r["fa_pct"]], s=210, marker="D",
                           color=colors[c], edgecolor="black", zorder=5)
    ax.set_xlim(xlim)
    ax.set_ylim(FA_LIMITS)
    ax.set_xlabel(_X_LABEL)
    ax.set_ylabel(_Y_LABEL_2D)
    ax.set_title("Group speed–accuracy trade-off", fontsize=11)
    handles = [Line2D([0], [0], marker="o", linestyle="none", markersize=6,
                      color=colors[c], label=_cond_label(cond_titles, c))
               for c in data.conditions]
    handles += [
        Line2D([0], [0], marker="o", linestyle="none", markersize=8,
               markerfacecolor="none", markeredgecolor="#555555",
               label="participant × condition centroid"),
        Line2D([0], [0], marker="D", linestyle="none", markersize=9,
               color="#555555", markeredgecolor="black",
               label=f"group centroid ± {error_bar_label(options.error_bars)} (participants)"),
    ]
    # Legend outside the axes so it can never cover data points.
    ax.legend(handles=handles, fontsize=8, loc="center left",
              bbox_to_anchor=(1.01, 0.5), borderaxespad=0)
    fig.tight_layout(rect=(0, 0, 0.99, 1))
    fig.subplots_adjust(right=0.72)
    return fig


def _plane_error_bars(ax, r, z: float, mode: str) -> None:
    """x/y error bars (95% t-CI or ±SD per `mode`) drawn flat inside a
    fixed-z plane of a 3D axis (no z error bar - difficulty/participant
    are design categories)."""
    x_b = error_bounds(r, "rt", mode)
    if x_b:
        ax.plot([x_b[0], x_b[1]], [r["fa_pct"], r["fa_pct"]],
                [z, z], color="black", linewidth=1.0, zorder=5)
    y_b = error_bounds(r, "fa", mode)
    if y_b:
        ax.plot([r["rt_ms"], r["rt_ms"]], [y_b[0], y_b[1]],
                [z, z], color="black", linewidth=1.0, zorder=5)


def _style_3d_axes(ax, xlim: tuple, z_label: str) -> None:
    ax.set_xlim(xlim)
    ax.set_ylim(FA_LIMITS)
    ax.set_xlabel(_X_LABEL, fontsize=8, labelpad=8)
    ax.set_ylabel(_Y_LABEL_3D, fontsize=8, labelpad=8)
    ax.set_zlabel(z_label, fontsize=8, labelpad=14)
    ax.tick_params(labelsize=7)
    # Fixed viewpoint chosen so the discrete z planes stay separated and
    # no axis label collides; the static export must be readable without
    # any 3D interaction.
    ax.view_init(elev=18, azim=-58)


def build_group_tradeoff_by_difficulty_3d(
        data: TradeoffData,
        cond_titles: Optional[Dict[str, str]] = None,
        colors: Optional[Dict[str, str]] = None,
        options: Optional[TradeoffOptions] = None,
        xlim: Optional[tuple] = None) -> Figure:
    """3D exploration: z = difficulty (discrete α/β/γ planes). Colour
    lightness AND marker shape both code the level inside the condition
    hue, so print / colour-vision-limited readers can still separate
    them. Group centroids come from participant x condition x level
    means, i.e. participants stay equally weighted."""
    colors = colors or CONDITION_COLORS
    options = options or TradeoffOptions()
    xlim = xlim or shared_rt_limits(data.points)

    fig = Figure(figsize=(6.2, 5.8))
    ax = fig.add_subplot(projection="3d")
    for c in data.conditions:
        for lv in LEVELS:
            z = LEVEL_Z[lv]
            col = level_color(c, lv, colors)
            marker = LEVEL_MARKERS[lv]
            if options.show_trial_points:
                pts = data.points[(data.points["condition"] == c)
                                  & (data.points["level"] == lv)
                                  & data.points["included"]]
                if len(pts):
                    ax.scatter(pts["rt_ms"], pts["fa_pct"], np.full(len(pts), z),
                               marker=marker, s=14, alpha=0.35, color=col,
                               linewidths=0, depthshade=False)
            if options.show_participant_centroids:
                pc = data.pcent_level[(data.pcent_level["condition"] == c)
                                      & (data.pcent_level["level"] == lv)]
                if len(pc):
                    ax.scatter(pc["rt_ms"], pc["fa_pct"], np.full(len(pc), z),
                               marker=marker, s=45, color=col,
                               edgecolor="black", linewidths=0.6, depthshade=False)
            if options.show_group_centroids:
                row = data.gcent_level[(data.gcent_level["condition"] == c)
                                       & (data.gcent_level["level"] == lv)]
                if len(row) and not np.isnan(float(row["rt_ms"].iloc[0])):
                    r = row.iloc[0]
                    _plane_error_bars(ax, r, z, options.error_bars)
                    ax.scatter([r["rt_ms"]], [r["fa_pct"]], [z], marker=marker,
                               s=130, color=col, edgecolor="black",
                               linewidths=1.2, depthshade=False)
    ax.set_zticks([LEVEL_Z[lv] for lv in LEVELS])
    ax.set_zticklabels([LEVEL_DISPLAY_LABELS[lv] for lv in LEVELS])
    ax.set_zlim(-0.4, 2.4)
    _style_3d_axes(ax, xlim, "difficulty level")
    ax.set_title("Speed–accuracy trade-off by difficulty", fontsize=10)
    handles = [Line2D([0], [0], marker="s", linestyle="none", markersize=7,
                      color=level_color(c, "beta", colors),
                      label=f"Feedback condition {_cond_label(cond_titles, c)}")
               for c in data.conditions]
    handles += [Line2D([0], [0], marker=LEVEL_MARKERS[lv], linestyle="none",
                       markersize=6, color=shade("#8a8a8a", LEVEL_LIGHTNESS[lv]),
                       label=(f"Difficulty {LEVEL_DISPLAY_LABELS[lv]} "
                              f"({'light' if lv == 'alpha' else 'mid' if lv == 'beta' else 'dark'} shade)"))
                for lv in LEVELS]
    fig.legend(handles=handles, fontsize=6.5, loc="upper left", ncols=2)
    fig.subplots_adjust(left=0.0, right=0.98, bottom=0.04, top=0.86)
    return fig


def build_group_tradeoff_by_participant_3d(
        data: TradeoffData,
        cond_titles: Optional[Dict[str, str]] = None,
        colors: Optional[Dict[str, str]] = None,
        options: Optional[TradeoffOptions] = None,
        xlim: Optional[tuple] = None) -> Figure:
    """3D exploration: z = participant, one discrete plane each, ticks
    labelled with the real IDs in the caller's existing order. Marker
    shape codes the condition, colour lightness the participant (same
    lightness rank across A/B/C). A thin B→C line per participant shows
    the visual→haptic shift direction (plain line - matplotlib's 3D
    arrows are not reliable enough for export). The optional group-mean
    centroids sit on their own labelled "Group mean" z position, never
    inside any participant's plane."""
    colors = colors or CONDITION_COLORS
    options = options or TradeoffOptions()
    xlim = xlim or shared_rt_limits(data.points)
    participants = data.participants
    n = len(participants)
    z_of = {p: i for i, p in enumerate(participants)}

    fig = Figure(figsize=(6.2, 5.8))
    ax = fig.add_subplot(projection="3d")
    pcent_by = data.pcent.set_index(["participant", "condition"]) if len(data.pcent) else None
    for c in data.conditions:
        marker = CONDITION_MARKERS[c]
        for p in participants:
            z = z_of[p]
            col = participant_color(c, z, n, colors)
            if options.show_trial_points:
                pts = data.points[(data.points["condition"] == c)
                                  & (data.points["participant"] == p)
                                  & data.points["included"]]
                if len(pts):
                    ax.scatter(pts["rt_ms"], pts["fa_pct"], np.full(len(pts), z),
                               marker=marker, s=14, alpha=0.35, color=col,
                               linewidths=0, depthshade=False)
            if options.show_participant_centroids and pcent_by is not None \
                    and (p, c) in pcent_by.index:
                r = pcent_by.loc[(p, c)]
                ax.scatter([r["rt_ms"]], [r["fa_pct"]], [z], marker=marker,
                           s=60, color=col, edgecolor="black", linewidths=0.8,
                           depthshade=False)
    if options.connect_b_to_c and pcent_by is not None:
        for p in participants:
            if (p, "B") in pcent_by.index and (p, "C") in pcent_by.index:
                b, cc = pcent_by.loc[(p, "B")], pcent_by.loc[(p, "C")]
                z = z_of[p]
                ax.plot([b["rt_ms"], cc["rt_ms"]], [b["fa_pct"], cc["fa_pct"]],
                        [z, z], color="#444444", alpha=0.35, linewidth=1.0)

    ticks = list(range(n))
    labels = list(participants)
    if options.show_group_centroids and len(data.gcent):
        # Group-mean centroids on a separate, clearly labelled z slot.
        z_group = n
        for c in data.conditions:
            row = data.gcent[data.gcent["condition"] == c]
            if len(row) and not np.isnan(float(row["rt_ms"].iloc[0])):
                r = row.iloc[0]
                ax.scatter([r["rt_ms"]], [r["fa_pct"]], [z_group],
                           marker=CONDITION_MARKERS[c], s=130, color=colors[c],
                           edgecolor="black", linewidths=1.2, depthshade=False)
        ticks.append(z_group)
        labels.append("Group mean")
    ax.set_zticks(ticks)
    ax.set_zticklabels(labels)
    ax.set_zlim(-0.5, max(ticks) + 0.5 if ticks else 0.5)
    _style_3d_axes(ax, xlim, "participant")
    ax.set_title("Speed–accuracy trade-off by participant", fontsize=10)
    handles = [Line2D([0], [0], marker=CONDITION_MARKERS[c], linestyle="none",
                      markersize=6, color=colors[c],
                      label=_cond_label(cond_titles, c)) for c in data.conditions]
    if options.connect_b_to_c:
        handles.append(Line2D([0], [0], color="#444444", alpha=0.5,
                              linewidth=1.0, label="B → C shift"))
    fig.legend(handles=handles, fontsize=6.5, loc="upper left", ncols=2)
    fig.subplots_adjust(left=0.0, right=0.98, bottom=0.04, top=0.86)
    return fig


def build_figures(data: TradeoffData,
                  cond_titles: Optional[Dict[str, str]] = None,
                  colors: Optional[Dict[str, str]] = None,
                  options: Optional[TradeoffOptions] = None) -> Dict[str, Figure]:
    """slug -> Figure for the three trade-off charts, sharing one
    x-range. The slugs double as the export file names."""
    xlim = shared_rt_limits(data.points)
    return {
        "group_tradeoff_2d": build_group_tradeoff_2d(
            data, cond_titles, colors, options, xlim),
        "group_tradeoff_by_difficulty_3d": build_group_tradeoff_by_difficulty_3d(
            data, cond_titles, colors, options, xlim),
        "group_tradeoff_by_participant_3d": build_group_tradeoff_by_participant_3d(
            data, cond_titles, colors, options, xlim),
    }
