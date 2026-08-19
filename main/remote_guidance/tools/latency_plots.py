"""Report figures for one latency run.

Made to be printed in the dissertation, so they follow print rules
rather than screen ones: vector PDF beside the PNG, ~9 pt type to sit
under a caption, no chart junk, and one measure per axis (never two
y-scales). Every figure is drawn from the run's own samples.csv and
summary.json - nothing here recomputes a latency or reinterprets one.

Colour is assigned by the job it does, from a validated categorical
palette, never more than three hues at once, and every series is also
direct-labelled, so identity never rests on colour alone.

These figures **describe and do not grade**. No threshold for "too slow"
is drawn on them, because that threshold is a requirement, not a
property of the measurement. Where scale is needed, the comparison drawn
is the study's own measured reaction time - the delay this one is added
to - and the reader is left to conclude.

What each figure is for:

- `latency`        the overview: how the round trip behaved over the run,
                   and its distribution as an ECDF (a histogram hides the
                   tail a real-time path is actually judged on).
- `latency_hops`   where the time goes - the teacher->relay leg against
                   the whole round trip.
- `latency_jitter` consecutive-probe variation (RFC 3393 IPDV), which is
                   closer to what a cue is felt as than absolute delay.
- `latency_human`  the transport delay beside the reaction time it is
                   added to, which is what makes a millisecond figure
                   mean anything.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Validated categorical slots 1-3 (blue, orange, aqua) plus the ink and
# surface tokens that go with them. Three is the documented all-pairs
# safe count; a fourth series would have to become a facet instead.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
INK, INK_SOFT, GRID = "#0b0b0b", "#52514e", "#d8d7d2"
SURFACE = "#fcfcfb"
# State shading: recessive, deliberately not a fourth series colour.
STATE_BAND = "#ebeae5"

FIGURE_WIDTH_IN = 6.3  # one text width in the report's layout

RC = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.size": 9,
    "axes.titlesize": 9.5,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": INK_SOFT,
    "ytick.color": INK_SOFT,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "axes.axisbelow": True,
    "lines.linewidth": 1.4,
    "legend.frameon": False,
}


def _tidy(ax) -> None:
    """Recessive chrome: no top/right rule, horizontal guides only."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.xaxis.grid(False)


def ecdf(values: Sequence[float]) -> Tuple[List[float], List[float]]:
    """x sorted, y = fraction of probes at or below x."""
    ordered = sorted(values)
    count = len(ordered)
    return ordered, [(index + 1) / count for index in range(count)]


def split_states(values: Sequence[float]) -> Optional[Tuple[float, float, float]]:
    """Two-means on one dimension, returning (low, high, boundary) only
    when the split is real rather than an artefact of asking for two.

    "Real" means the two centres differ by at least a factor of two and
    neither holds under a tenth of the probes. The case this exists for
    is a link that alternates between two states; on a link that does
    not, the figure must not claim it does."""
    ordered = sorted(values)
    if len(ordered) < 20:
        return None
    low, high = ordered[len(ordered) // 10], ordered[-len(ordered) // 10]
    for _ in range(50):
        boundary = (low + high) / 2
        left = [value for value in ordered if value <= boundary]
        right = [value for value in ordered if value > boundary]
        if not left or not right:
            return None
        new_low, new_high = statistics.mean(left), statistics.mean(right)
        if abs(new_low - low) < 1e-9 and abs(new_high - high) < 1e-9:
            break
        low, high = new_low, new_high
    boundary = (low + high) / 2
    left = [value for value in ordered if value <= boundary]
    right = [value for value in ordered if value > boundary]
    if high < low * 2 or min(len(left), len(right)) / len(ordered) < 0.10:
        return None
    return statistics.median(left), statistics.median(right), boundary


def episodes(flags: Sequence[bool], minimum_run: int = 4) -> List[Tuple[int, int]]:
    """Runs of consecutive elevated probes, so the overview shades
    episodes rather than peppering itself with single spikes."""
    spans, start = [], None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start >= minimum_run:
                spans.append((start, index))
            start = None
    if start is not None and len(flags) - start >= minimum_run:
        spans.append((start, len(flags)))
    return spans


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _save(fig, directory: Path, stem: str) -> List[Path]:
    """PNG for the results page, PDF for the thesis - the same figure,
    so the printed one can never drift from the one on screen."""
    written = []
    for suffix, dpi in ((".png", 200), (".pdf", None)):
        path = directory / f"{stem}{suffix}"
        fig.savefig(path, dpi=dpi, bbox_inches="tight")
        written.append(path)
    return written


def overview_figure(rtt_ms: List[float], seqs: List[int], summary: Dict[str, Any], directory: Path,
                    plt) -> List[Path]:
    """Trend over time, and the distribution as an ECDF.

    An ECDF rather than a histogram: a reader of a real-time path wants
    to read p95 and p99 straight off the curve, and a histogram's bins
    bury exactly that end."""
    states = split_states(rtt_ms)
    fig, (ax_run, ax_dist) = plt.subplots(
        1, 2, figsize=(FIGURE_WIDTH_IN, 2.6), gridspec_kw={"width_ratios": [1.9, 1]}
    )

    if states:
        low, high, boundary = states
        for start, end in episodes([value > boundary for value in rtt_ms]):
            ax_run.axvspan(seqs[start], seqs[min(end, len(seqs) - 1)], color=STATE_BAND, linewidth=0, zorder=0)
    ax_run.plot(seqs, rtt_ms, color=SERIES[0], linewidth=0.7)
    ax_run.set_xlabel("probe")
    ax_run.set_ylabel("round-trip time (ms)")
    ax_run.set_title("Round trip over the run", loc="left", color=INK)
    _tidy(ax_run)
    if states:
        low, high, _ = states
        # Named on the plot, not in a legend: they are annotations of one
        # series, not two series.
        ax_run.annotate(f"elevated state, median {high:.0f} ms", xy=(0.99, 0.94),
                        xycoords="axes fraction", ha="right", va="top", fontsize=7.5, color=INK_SOFT)
        ax_run.annotate(f"base state, median {low:.0f} ms", xy=(0.99, 0.83),
                        xycoords="axes fraction", ha="right", va="top", fontsize=7.5, color=INK_SOFT)

    ordered, fractions = ecdf(rtt_ms)
    ax_dist.plot(ordered, [f * 100 for f in fractions], color=SERIES[0])
    # Offsets in points, staggered downward: p95 and p99 sit within four
    # percent of each other and would otherwise print on top of one
    # another. The right margin keeps the last one on the page.
    for fraction, drop in ((0.5, -3), (0.95, -9), (0.99, -19)):
        value = _percentile(ordered, fraction)
        ax_dist.plot([value], [fraction * 100], marker="o", markersize=4, color=SERIES[0],
                     markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
        ax_dist.annotate(f"p{fraction * 100:g}  {value:.0f} ms", xy=(value, fraction * 100),
                         xytext=(7, drop), textcoords="offset points",
                         fontsize=7.5, color=INK_SOFT, va="center")
    ax_dist.set_xlim(0, max(ordered) * 1.45)
    ax_dist.set_xlabel("round-trip time (ms)")
    ax_dist.set_ylabel("probes at or below (%)")
    ax_dist.set_ylim(0, 104)
    ax_dist.set_title("Distribution", loc="left", color=INK)
    _tidy(ax_dist)

    counts = summary.get("counts") or {}
    fig.text(
        0, -0.06,
        f"n = {counts.get('attempted', len(rtt_ms))} measured probes, "
        f"{counts.get('lost', 0)} lost ({(counts.get('loss_rate') or 0) * 100:.2f}%). "
        "Software transport timing, not physical cue onset.",
        fontsize=7.5, color=INK_SOFT,
    )
    fig.tight_layout()
    return _save(fig, directory, "latency")


def hops_figure(rtt_ms: List[float], ack_ms: List[float], directory: Path, plt) -> List[Path]:
    """Which leg the time is spent on.

    Two ECDFs on one axis - the same measure, so one scale - with the
    teacher->relay round trip under the full one. The gap between them
    is the relay->student->relay remainder."""
    if len(ack_ms) < 10:
        return []
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH_IN, 2.6))
    for values, colour, label in (
        (rtt_ms, SERIES[0], "teacher → student → teacher"),
        (ack_ms, SERIES[1], "teacher → relay → teacher"),
    ):
        ordered, fractions = ecdf(values)
        ax.plot(ordered, [f * 100 for f in fractions], color=colour, label=label)
        median = _percentile(ordered, 0.5)
        # The legend carries identity; these carry the number, so they
        # stay short enough to sit clear of the other curve.
        ax.annotate(f"median {median:.0f} ms", xy=(median, 50),
                    xytext=(-6, 26) if colour == SERIES[1] else (8, -22),
                    textcoords="offset points", ha="right" if colour == SERIES[1] else "left",
                    fontsize=7.5, color=INK_SOFT,
                    arrowprops={"arrowstyle": "-", "color": GRID, "linewidth": 0.8})
    ax.set_xscale("log")
    ax.set_xlabel("time (ms, log scale)")
    ax.set_ylabel("probes at or below (%)")
    ax.set_ylim(0, 104)
    ax.set_title("Where the round trip is spent", loc="left", color=INK)
    ax.legend(loc="lower right")
    _tidy(ax)
    fig.tight_layout()
    return _save(fig, directory, "latency_hops")


def jitter_figure(rtt_ms: List[float], directory: Path, plt) -> List[Path]:
    """Consecutive-probe variation (RFC 3393 IPDV).

    A cue that is late by a steady amount can be compensated for; one
    that moves cannot, which is why this gets its own figure rather than
    a line in a table."""
    if len(rtt_ms) < 20:
        return []
    ipdv = [abs(later - earlier) for earlier, later in zip(rtt_ms, rtt_ms[1:])]
    ordered, fractions = ecdf(ipdv)
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH_IN, 2.4))
    ax.plot(ordered, [f * 100 for f in fractions], color=SERIES[2])
    for fraction in (0.5, 0.95):
        value = _percentile(ordered, fraction)
        ax.plot([value], [fraction * 100], marker="o", markersize=4, color=SERIES[2],
                markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
        ax.annotate(f" p{fraction * 100:g} {value:.0f} ms", xy=(value, fraction * 100),
                    fontsize=7.5, color=INK_SOFT, va="center")
    ax.set_xlabel("|change in round-trip time between consecutive probes| (ms)")
    ax.set_ylabel("probe pairs at or below (%)")
    ax.set_ylim(0, 104)
    ax.set_title("Probe-to-probe variation (IPDV)", loc="left", color=INK)
    _tidy(ax)
    fig.tight_layout()
    return _save(fig, directory, "latency_jitter")


def human_scale_figure(rtt_ms: List[float], human: Dict[str, Any], directory: Path, plt) -> List[Path]:
    """The transport delay beside the reaction time it is added to.

    The only figure here that answers "does this matter", and it answers
    it by putting both distributions on one axis and stopping - no
    threshold, no verdict."""
    median_s, log_sigma = human.get("median_s"), human.get("log_sigma")
    if not median_s or not log_sigma:
        return []
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH_IN, 2.7))

    ordered, fractions = ecdf(rtt_ms)
    ax.plot(ordered, [f * 100 for f in fractions], color=SERIES[0], label="transport round trip (measured)")

    # The participants' own fitted distribution, drawn as its analytic
    # curve rather than resampled - it is a fit, and should look like one.
    points = [median_s * 1000 * math.exp(log_sigma * z / 100.0) for z in range(-380, 381, 4)]
    curve = [_lognormal_cdf(value / 1000.0, median_s, log_sigma) * 100 for value in points]
    ax.plot(points, curve, color=SERIES[1], linestyle=(0, (5, 2)),
            label=f"participant reaction time ({human.get('source', 'fitted')})")

    ax.set_xscale("log")
    ax.set_xlabel("time (ms, log scale)")
    ax.set_ylabel("at or below (%)")
    ax.set_ylim(0, 104)
    ax.set_title("Transport delay against the reaction time it is added to", loc="left", color=INK)
    # Below the axes: the empty space inside the plot is where the
    # callout has to go, and the two would collide up there.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2)

    transport_median = _percentile(ordered, 0.5)
    ax.annotate(
        f"median {transport_median:.0f} ms - {transport_median / (median_s * 1000) * 100:.0f}% of the "
        f"{median_s * 1000:.0f} ms reaction-time median",
        xy=(transport_median, 50), xytext=(24, -6), textcoords="offset points",
        fontsize=7.5, color=INK_SOFT,
        arrowprops={"arrowstyle": "-", "color": GRID, "linewidth": 0.8},
    )
    _tidy(ax)
    fig.tight_layout()
    return _save(fig, directory, "latency_human")


def _lognormal_cdf(value_s: float, median_s: float, log_sigma: float) -> float:
    if value_s <= 0:
        return 0.0
    z = (math.log(value_s) - math.log(median_s)) / log_sigma
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def write_figures(samples: Sequence[Any], summary: Dict[str, Any], directory: Path) -> List[Path]:
    """Every figure this run's data supports, as PNG and PDF.

    Matplotlib is already a platform dependency, but a missing backend is
    not fatal: the CSV and the JSON are the results, the figures are a
    reading of them."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []

    measured = [s for s in samples if not s.warmup and s.rtt_ns is not None]
    if not measured:
        return []
    rtt_ms = [s.rtt_ns / 1e6 for s in measured]
    seqs = [s.seq for s in measured]
    ack_ms = [s.server_ack_rtt_ns / 1e6 for s in measured if s.server_ack_rtt_ns is not None]
    human = ((summary.get("pacing") or {}).get("human")) or {}

    written: List[Path] = []
    with plt.rc_context(RC):
        written += overview_figure(rtt_ms, seqs, summary, directory, plt)
        written += hops_figure(rtt_ms, ack_ms, directory, plt)
        written += jitter_figure(rtt_ms, directory, plt)
        written += human_scale_figure(rtt_ms, human, directory, plt)
        plt.close("all")
    return written
