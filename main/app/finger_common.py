"""Shared preparation for the three finger-level benefit analyses
(app.finger_benefit, app.finger_equalisation, app.finger_weakest).

All three ask a variant of the same question - does the vibrotactile
finger cue COMPENSATE the digits that are already slow under the visual
cue, rather than speeding every digit up by the same amount - so they all
work from one shape: the participant x finger table of the baseline
condition (B, visual) beside the cued condition (C, haptic), restricted
to participants who supply every cell. That preparation, and the
split-half machinery the unbiased variants need, live here so the three
analysis modules stay short and independent of each other.

Split-half and why it exists
----------------------------
Two of the three analyses select or condition on the baseline and then
measure a change that also contains the baseline:

    Benefit = RT_B - RT_C          regressed on RT_B          (benefit)
    "weakest finger under B"       then measured B -> C       (weakest)

Both are mathematically coupled to their own selection variable. Writing
RT_B = mu_B + e_B and RT_C = mu_C + e_C for a single cell, with e the
sampling noise of that cell's finite event count,

    Cov(Benefit, RT_B) = Var(RT_B) - Cov(RT_B, RT_C) = Var(e_B) + ...

which is positive whenever the two cells' noise is independent - i.e. a
"slower fingers benefit more" correlation appears even when the true
benefit is identical for every finger. Selecting the extreme finger
first (the weakest-finger analysis) is the same effect in its sharpest
form: regression to the mean guarantees that the selected cell moves
towards the participant's average on any re-measurement.

split_half_events() cuts a participant's trials within each condition
into two interleaved halves by presentation order. The two halves have
independent sampling noise, so the baseline computed on half 1 can be
regressed against (or used to select for) a benefit computed on half 2
with no coupling at all. The cost is precision - each half carries about
half the events - which is why the analysis modules report the naive and
the split-half estimate side by side: the GAP between them is the size
of the artefact.

Nothing here re-derives correctness or reaction times; everything comes
from app.group_analysis.per_finger_metrics over the exported event rows,
so these analyses can never disagree with the Fingers or RM-ANOVA tabs.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .group_analysis import (
    FINGER_ID_NAMES,
    FINGER_IDS,
    anova_cell_frame,
    per_finger_metrics,
)

# The contrast every module in this family is about: what does adding the
# vibrotactile finger cue (C) do relative to the visual finger cue (B)?
# A carries no finger cue at all and is therefore not a baseline for a
# per-digit cue benefit.
BASELINE_CONDITION = "B"
CUED_CONDITION = "C"

# Positive benefit = the cued condition is FASTER (RT_B - RT_C) or MORE
# accurate (FA_C - FA_B). Kept explicit so no module has to remember the
# sign convention of its own metric.
LOWER_IS_BETTER = {"rt_s": True, "rt_complete_s": True, "fa": False}

__all__ = [
    "BASELINE_CONDITION", "CUED_CONDITION", "FINGER_IDS", "FINGER_ID_NAMES",
    "LOWER_IS_BETTER", "benefit_sign", "paired_finger_cells",
    "split_half_events", "split_half_cells", "describe_dropped",
]


def benefit_sign(metric: str) -> float:
    """+1 or -1 such that benefit = sign * (baseline - cued) is positive
    when the cued condition is BETTER on that metric."""
    return 1.0 if LOWER_IS_BETTER.get(metric, True) else -1.0


def paired_finger_cells(pf_df: pd.DataFrame, metric: str,
                        baseline: str = BASELINE_CONDITION,
                        cued: str = CUED_CONDITION) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """(frame, dropped): one row per (participant, finger_id) carrying

        baseline  - the participant's cell value under `baseline`
        cued      - the same cell under `cued`
        benefit   - signed so that positive always means "cued is better"
        average   - (baseline + cued) / 2, Oldham's coupling-free x-axis

    Only participants with all 2 x 5 cells enter (listwise, via
    group_analysis.anova_cell_frame), so every downstream mean, paired
    test and correlation runs on one identical set of participants."""
    cols = ["participant", "finger_id", "baseline", "cued", "benefit", "average"]
    frame, dropped = anova_cell_frame(pf_df, metric, [baseline, cued])
    if frame.empty:
        return pd.DataFrame(columns=cols), dropped
    wide = frame.pivot_table(index=["participant", "finger_id"],
                             columns="condition", values=metric).reset_index()
    sign = benefit_sign(metric)
    out = pd.DataFrame({
        "participant": wide["participant"],
        "finger_id": wide["finger_id"].astype(int),
        "baseline": wide[baseline].astype(float),
        "cued": wide[cued].astype(float),
    })
    out["benefit"] = sign * (out["baseline"] - out["cued"])
    out["average"] = (out["baseline"] + out["cued"]) / 2.0
    return out.sort_values(["participant", "finger_id"], ignore_index=True), dropped


def split_half_events(event_rows: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Split events into two halves with independent sampling noise.

    The cut is by TRIAL, not by event: a participant's trials within a
    condition are ranked by their own trial_index and assigned
    alternately, so the halves are balanced over the session and over the
    difficulty levels, and no two events from the same trial (same
    sequence, same moment, correlated errors) ever land on opposite sides
    of an estimate that is supposed to be independent.

    Deterministic - no RNG - so a reported split-half result is
    reproducible from the exported CSVs alone."""
    by_cell: Dict[Tuple[str, str], set] = {}
    for e in event_rows:
        key = (e.get("participant"), e.get("condition"))
        by_cell.setdefault(key, set()).add(e.get("trial_index"))
    first_half: Dict[Tuple[str, str], set] = {}
    for key, trials in by_cell.items():
        ordered = sorted(t for t in trials if t is not None)
        first_half[key] = {t for i, t in enumerate(ordered) if i % 2 == 0}
    half_a, half_b = [], []
    for e in event_rows:
        key = (e.get("participant"), e.get("condition"))
        (half_a if e.get("trial_index") in first_half.get(key, set()) else half_b).append(e)
    return half_a, half_b


def split_half_cells(event_rows: List[dict]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """per_finger_metrics computed separately on the two independent
    halves of the trials. Cells whose half holds no usable event come
    back as NaN, exactly as in the full-sample table."""
    half_a, half_b = split_half_events(event_rows)
    return per_finger_metrics(half_a), per_finger_metrics(half_b)


def describe_dropped(dropped: Dict[str, str]) -> Optional[str]:
    """One sentence naming the participants left out for incomplete
    cells, or None when the grid was complete. Every module states this
    before its numbers - a shrunken denominator must never be silent."""
    if not dropped:
        return None
    return ("Excluded for incomplete cells (listwise, never imputed): "
            + "; ".join(f"{p} ({why})" for p, why in sorted(dropped.items())))


def finger_label(finger_id) -> str:
    """'3 middle' - the label every figure and table in this family uses."""
    fid = int(finger_id)
    return f"{fid} {FINGER_ID_NAMES.get(fid, '?')}"


def paired_summary(values: np.ndarray) -> dict:
    """Mean / SD / SEM / 95% t-CI / Cohen's dz of a set of PARTICIPANT
    level paired differences. n counts participants; the interval and dz
    are NaN below n = 2 rather than fabricated."""
    vals = np.asarray([v for v in np.asarray(values, dtype=float) if np.isfinite(v)])
    n = int(vals.size)
    out = {"n": n, "mean": float(np.mean(vals)) if n else np.nan,
           "sd": np.nan, "sem": np.nan, "ci95_lo": np.nan, "ci95_hi": np.nan,
           "dz": np.nan}
    if n < 2:
        return out
    from scipy import stats as sstats  # local: keeps the import cost off callers
    sd = float(np.std(vals, ddof=1))
    sem = sd / np.sqrt(n)
    half = float(sstats.t.ppf(0.975, n - 1)) * sem
    out.update(sd=sd, sem=sem, ci95_lo=out["mean"] - half, ci95_hi=out["mean"] + half,
               dz=(out["mean"] / sd if sd > 0 else np.nan))
    return out
