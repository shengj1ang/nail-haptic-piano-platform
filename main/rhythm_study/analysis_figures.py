"""Rhythm experiment: the figures.

Separate from :mod:`rhythm_study.analysis` so the numbers can be
computed, exported and tested without matplotlib being involved, and so
a plotting failure can never cost a run its CSVs.

Two conventions are inherited from the main study's figures and are not
cosmetic:

* **a latency axis starts at zero.** An error-in-milliseconds panel
  cropped to its data makes a 20 ms difference look like a large one.
* **an accuracy axis is left to autoscale.** Forcing 0-100% on data that
  lives between 0.75 and 0.95 hides the entire result; a 0-1 axis was
  tried on the main study's figures and rejected for that reason.

Every probe panel shows each participant as a thin line and the group on
top, because the participant is the unit of inference: the reader should
be able to see how many individuals actually move, not just that a mean
does.
"""

from pathlib import Path
from typing import List, Optional, Sequence

from .analysis import (
    PROBE_LABELS,
    WITHDRAWAL_PAIRS,
    AnalysisResult,
    probe_table,
    training_curve,
)
from .schedule import PHASE_FINAL

# Figures are written, never shown: this runs from a Qt window and from
# a CLI, and an interactive backend would try to open a window in both.
FIGURE_DPI = 150
FIGURE_SIZE = (7.0, 5.0)

INDIVIDUAL_STYLE = dict(color="#7f8c9a", alpha=0.55, linewidth=1.0, marker="o", markersize=3)
GROUP_STYLE = dict(color="#1f4e79", linewidth=2.6, marker="o", markersize=7, zorder=5)


def _plt():
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    return plt


def _mean_and_ci(columns: Sequence[Sequence[float]]):
    """Column means with a 95% bootstrap CI, as (means, low, high).

    Bootstrapped for the same reason the tests are non-parametric: these
    are small samples of bounded measures where a normal interval can
    run past 100% accuracy.
    """
    import numpy as np  # noqa: PLC0415

    rng = np.random.default_rng(20260827)
    means, lows, highs = [], [], []
    for column in columns:
        data = np.asarray([v for v in column if v is not None], dtype=float)
        if data.size == 0:
            means.append(np.nan)
            lows.append(np.nan)
            highs.append(np.nan)
            continue
        means.append(float(data.mean()))
        if data.size < 2:
            lows.append(float(data.mean()))
            highs.append(float(data.mean()))
            continue
        draws = rng.choice(data, size=(4000, data.size), replace=True).mean(axis=1)
        lows.append(float(np.percentile(draws, 2.5)))
        highs.append(float(np.percentile(draws, 97.5)))
    return means, lows, highs


def _finish(ax, fig, path: Path, *, zero_floor: bool):
    if zero_floor:
        top = ax.get_ylim()[1]
        ax.set_ylim(0, top)
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=FIGURE_DPI)
    _plt().close(fig)
    return path


# ---------------------------------------------------------------------------
# The two primary probe figures
# ---------------------------------------------------------------------------


def probe_progression(
    result: AnalysisResult, metric: str, path: Path, *, ylabel: str, zero_floor: bool
) -> Optional[Path]:
    """Probe 1 -> 2 -> 3, one thin line per participant plus the group.

    The whole retention question is in this shape, so it is deliberately
    the plainest figure in the set: three x positions, no smoothing, no
    second axis.
    """
    plt = _plt()
    table = probe_table(result.trials, metric)
    series = {p: v for p, v in table.items() if any(x is not None for x in v)}
    if not series:
        return None

    fig, ax = plt.subplots(figsize=FIGURE_SIZE)
    x = list(range(len(PROBE_LABELS)))
    for values in series.values():
        pairs = [(i, v) for i, v in zip(x, values) if v is not None]
        ax.plot([i for i, _ in pairs], [v for _, v in pairs], **INDIVIDUAL_STYLE)

    columns = [[v[i] for v in series.values()] for i in x]
    means, lows, highs = _mean_and_ci(columns)
    ax.errorbar(
        x, means,
        yerr=[[m - lo for m, lo in zip(means, lows)], [hi - m for m, hi in zip(means, highs)]],
        capsize=5, label=f"group mean, 95% CI (n={len(series)})", **GROUP_STYLE,
    )
    ax.set_xticks(x, PROBE_LABELS)
    ax.set_xlim(-0.3, len(PROBE_LABELS) - 0.7)
    ax.set_ylabel(ylabel)
    ax.set_title("Haptic removed: performance across the three probes")
    ax.legend(frameon=False, loc="best")
    return _finish(ax, fig, path, zero_floor=zero_floor)


# ---------------------------------------------------------------------------
# Training learning curves (descriptive)
# ---------------------------------------------------------------------------


def training_learning_curve(
    result: AnalysisResult, metric: str, path: Path, *, ylabel: str, zero_floor: bool,
    caveat: str = "",
) -> Optional[Path]:
    plt = _plt()
    curve = training_curve(result.trials, metric)
    series = {p: v for p, v in curve.items() if any(x is not None for x in v)}
    if not series:
        return None

    fig, ax = plt.subplots(figsize=FIGURE_SIZE)
    x = list(range(1, 16))
    for values in series.values():
        pairs = [(i, v) for i, v in zip(x, values) if v is not None]
        ax.plot([i for i, _ in pairs], [v for _, v in pairs], **INDIVIDUAL_STYLE)
    columns = [[v[i] for v in series.values()] for i in range(15)]
    means, lows, highs = _mean_and_ci(columns)
    ax.plot(x, means, label=f"group mean (n={len(series)})", **GROUP_STYLE)
    ax.fill_between(x, lows, highs, color=GROUP_STYLE["color"], alpha=0.15, linewidth=0)

    # Where the haptic cue was taken away, so the curve is never read as
    # if training and probes were one continuous series.
    for repetition, _ in WITHDRAWAL_PAIRS:
        ax.axvline(repetition + 0.5, color="#b03a2e", linestyle="--", linewidth=1.0, alpha=0.7)
    ax.annotate(
        "dashed: a probe follows",
        xy=(0.99, 0.02), xycoords="axes fraction", ha="right", fontsize=8, color="#b03a2e",
    )
    ax.set_xticks(x)
    ax.set_xlabel("Training repetition (haptic cue present throughout)")
    ax.set_ylabel(ylabel)
    title = "Training: performance while the haptic cue is available"
    ax.set_title(title if not caveat else f"{title}\n{caveat}", fontsize=10)
    ax.legend(frameon=False, loc="best")
    return _finish(ax, fig, path, zero_floor=zero_floor)


# ---------------------------------------------------------------------------
# Withdrawal cost
# ---------------------------------------------------------------------------


def withdrawal_cost_figure(result: AnalysisResult, metric: str, path: Path) -> Optional[Path]:
    """How much is lost the moment the haptic cue goes, after 5, 10 and
    15 repetitions. A cost that shrinks is the retention signature."""
    plt = _plt()
    cost = result.withdrawal.get(metric)
    if cost is None:
        return None
    series = {p: v for p, v in cost["per_participant"].items() if any(x is not None for x in v)}
    if not series:
        return None

    fig, ax = plt.subplots(figsize=FIGURE_SIZE)
    x = list(range(len(WITHDRAWAL_PAIRS)))
    for values in series.values():
        pairs = [(i, v) for i, v in zip(x, values) if v is not None]
        ax.plot([i for i, _ in pairs], [v for _, v in pairs], **INDIVIDUAL_STYLE)
    columns = [[v[i] for v in series.values()] for i in x]
    means, lows, highs = _mean_and_ci(columns)
    ax.errorbar(
        x, means,
        yerr=[[m - lo for m, lo in zip(means, lows)], [hi - m for m, hi in zip(means, highs)]],
        capsize=5, label=f"group mean, 95% CI (n={len(series)})", **GROUP_STYLE,
    )
    # Zero is the meaningful line here - it is "no cost at all" - so it
    # is drawn rather than left to the reader to locate.
    ax.axhline(0, color="#444", linewidth=1.0)
    ax.set_xticks(x, [f"after {rep}\n(-> Probe {probe})" for rep, probe in WITHDRAWAL_PAIRS])
    ax.set_xlim(-0.3, len(WITHDRAWAL_PAIRS) - 0.7)
    ax.set_xlabel("Training repetitions completed")
    ax.set_ylabel(f"{cost['label']}: probe - preceding training")
    ax.set_title("Haptic withdrawal cost\n(above zero = the probe was no worse)", fontsize=10)
    ax.legend(frameon=False, loc="best")
    # Signed data straddling zero: a zero floor would cut the figure in half.
    return _finish(ax, fig, path, zero_floor=False)


# ---------------------------------------------------------------------------
# Final unguided test
# ---------------------------------------------------------------------------


def final_test_figure(result: AnalysisResult, path: Path) -> Optional[Path]:
    """Probe 3 against the final test, per participant.

    Two panels rather than one: accuracy and timing do not share units,
    and putting them on a twin axis would invite reading a crossing as
    meaningful.
    """
    plt = _plt()
    panels = [
        ("finger_accuracy", "Finger accuracy", False),
        ("mean_absolute_onset_error_ms", "Absolute onset error (ms)", True),
    ]
    probe3 = {m: probe_table(result.trials, m) for m, _, _ in panels}
    finals = {
        m: {r["participant"]: r.get(m) for r in result.trials if r["phase"] == PHASE_FINAL}
        for m, _, _ in panels
    }
    if not any(finals[m] for m, _, _ in panels):
        return None

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 5.0))
    for ax, (metric, label, zero_floor) in zip(axes, panels):
        pairs = [
            (name, probe3[metric][name][2], finals[metric][name])
            for name in sorted(set(probe3[metric]) & set(finals[metric]))
            if probe3[metric].get(name)
            and probe3[metric][name][2] is not None
            and finals[metric][name] is not None
        ]
        for _, before, after in pairs:
            ax.plot([0, 1], [before, after], **INDIVIDUAL_STYLE)
        if pairs:
            means, lows, highs = _mean_and_ci([[p for _, p, _ in pairs], [f for _, _, f in pairs]])
            ax.errorbar(
                [0, 1], means,
                yerr=[[m - lo for m, lo in zip(means, lows)], [hi - m for m, hi in zip(means, highs)]],
                capsize=5, **GROUP_STYLE,
            )
        ax.set_xticks([0, 1], ["Probe 3\n(backlight on)", "Final\n(nothing)"])
        ax.set_xlim(-0.3, 1.3)
        ax.set_ylabel(label)
        if zero_floor:
            ax.set_ylim(0, ax.get_ylim()[1])
        ax.grid(True, axis="y", alpha=0.25, linewidth=0.6)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle(
        "Final unguided test - a TRANSFER comparison, not a fourth probe\n"
        "(the backlight is removed here as well as the haptic cue)",
        fontsize=10,
    )
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------


FIGURE_FILENAMES = {
    "probe_finger_accuracy": "probe_finger_accuracy.png",
    "probe_onset_error": "probe_absolute_onset_error.png",
    "training_finger_accuracy": "training_finger_accuracy.png",
    "training_timing": "training_response_time.png",
    "withdrawal_cost": "withdrawal_cost_finger_accuracy.png",
    "final_test": "final_unguided_test.png",
}


def render_all(result: AnalysisResult, out_dir) -> List[Path]:
    """Every figure the report needs, in one call. Returns what was
    actually written - a panel with no data is skipped, not faked."""
    out = Path(out_dir)
    made: List[Path] = []
    names = FIGURE_FILENAMES

    for path in (
        probe_progression(
            result, "finger_accuracy", out / names["probe_finger_accuracy"],
            ylabel="Finger accuracy (correct key AND finger)", zero_floor=False,
        ),
        probe_progression(
            result, "mean_absolute_onset_error_ms", out / names["probe_onset_error"],
            ylabel="Absolute onset error (ms)", zero_floor=True,
        ),
        training_learning_curve(
            result, "finger_accuracy", out / names["training_finger_accuracy"],
            ylabel="Finger accuracy (correct key AND finger)", zero_floor=False,
        ),
        training_learning_curve(
            result, "mean_rt_ms", out / names["training_timing"],
            ylabel="Response time (ms)", zero_floor=True,
            caveat="training is cue/response, so this is reaction time - not onset error",
        ),
        withdrawal_cost_figure(result, "finger_accuracy", out / names["withdrawal_cost"]),
        final_test_figure(result, out / names["final_test"]),
    ):
        if path is not None:
            made.append(path)
    return made
