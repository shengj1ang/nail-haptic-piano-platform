"""Analysis 2 of the finger-benefit family: does the vibrotactile cue
EQUALISE the five digits - does the spread of reaction time across
fingers shrink from B to C - rather than only shifting every digit down
by the same amount?

Per participant and condition the five finger cell means give one
dispersion value; the participants' paired B -> C change in that value is
the test. Three dispersion measures are reported because they fail in
different ways:

  sd      standard deviation of the five cell means (ddof = 1)
  range   max - min across the five cells, the most intuitive and the
          least robust - it is decided by two cells out of five
  cv      sd / mean, the scale-free one

Why the CV is not optional. C is roughly a quarter faster than B
overall, and reaction-time spread grows with the mean. A purely
multiplicative speed-up - every finger's RT multiplied by the same
constant k < 1, no equalisation whatsoever - reduces sd and range by
exactly that factor k while leaving cv untouched. So sd or range
shrinking is expected under the null of "uniform proportional speed-up"
and proves nothing on its own; cv shrinking is what distinguishes real
equalisation from a proportional shift. Both are reported and the caller
is expected to read them together.

Sampling noise in the dispersion, and the correction
----------------------------------------------------
Each cell mean is estimated from a finite number of events, so the
observed spread across the five cells is inflated by that estimation
noise:

    Var_observed = Var_true + E[SEM_cell^2].

This matters here and it is not symmetric: B's reaction times are slower
and more variable within a cell, so B's cell means are the noisier ones
and B's observed spread is inflated MORE than C's. That inflation alone
would produce an apparent equalisation - once again in the direction of
the hypothesis. sd_corrected therefore subtracts the mean squared
per-cell standard error before taking the root,

    sd_corrected = sqrt(max(Var_observed - mean(SEM_cell^2), 0)),

and both the raw and the corrected values are reported, together with
each condition's mean per-cell SEM so the size of the correction is
visible rather than buried. A conclusion that survives on sd_corrected
AND on cv is the one worth writing down.

Inference is on the participants' paired differences (C - B): mean, 95%
t-CI, Cohen's dz, a paired t-test and a paired Wilcoxon signed-rank as
the distribution-free backup, all with n = participants.
"""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats as sstats

from . import finger_common as fc
from .group_analysis import (
    EXPLORATORY_N,
    FINGER_ORDER,
    MIN_TEST_N,
    per_finger_metrics,
    valid_events,
)

# Dispersion measures, in reporting order: (key, label, scale-free?)
MEASURES = [
    ("sd_raw", "SD across the five fingers", False),
    ("sd_corrected", "SD, noise-corrected", False),
    ("range", "Range (max − min)", False),
    ("cv_raw", "Coefficient of variation (SD / mean)", True),
]


def _event_rts(event_rows: List[dict], metric: str):
    """Which events feed a cell of `metric` - the same rule
    per_finger_metrics uses, so the SEM describes exactly the mean it
    corrects."""
    events = [e for e in valid_events(event_rows)
              if not e["timed_out"] and e.get("target_finger") in FINGER_ORDER
              and e.get("rt_s") is not None]
    if metric == "rt_complete_s":
        events = [e for e in events if e["key_correct"] and e["finger_correct"]]
    return events


def cell_standard_errors(event_rows: List[dict], metric: str = "rt_complete_s") -> Dict:
    """{(participant, condition, finger_id): SEM of that cell's mean}.
    NaN where a cell holds fewer than two usable events - a single event
    carries no estimate of its own spread."""
    buckets: Dict = {}
    for e in _event_rts(event_rows, metric):
        key = (e["participant"], e["condition"], int(e["target_finger"][1]))
        buckets.setdefault(key, []).append(float(e["rt_s"]))
    out = {}
    for key, rts in buckets.items():
        arr = np.asarray(rts, dtype=float)
        out[key] = (float(np.std(arr, ddof=1) / np.sqrt(arr.size))
                    if arr.size >= 2 else np.nan)
    return out


def dispersion_table(event_rows: List[dict], metric: str = "rt_complete_s",
                     conditions: Optional[List[str]] = None) -> pd.DataFrame:
    """One row per (participant, condition): the mean over the five
    finger cells, the three dispersion measures, the noise-corrected SD
    and the mean per-cell SEM that produced the correction. Only
    participants with a complete grid in every requested condition
    appear."""
    conditions = list(conditions or [fc.BASELINE_CONDITION, fc.CUED_CONDITION])
    cols = ["participant", "condition", "mean", "sd_raw", "sd_corrected", "range",
            "cv_raw", "cv_corrected", "mean_cell_sem", "n_fingers"]
    pf = per_finger_metrics(event_rows)
    pairs, _dropped = fc.paired_finger_cells(pf, metric, *conditions[:2])
    if pairs.empty:
        return pd.DataFrame(columns=cols)
    sems = cell_standard_errors(event_rows, metric)
    long = pd.concat([
        pairs[["participant", "finger_id"]].assign(condition=conditions[0],
                                                   value=pairs["baseline"]),
        pairs[["participant", "finger_id"]].assign(condition=conditions[1],
                                                   value=pairs["cued"]),
    ], ignore_index=True)

    rows = []
    for (participant, condition), sub in long.groupby(["participant", "condition"], sort=True):
        vals = sub["value"].to_numpy(dtype=float)
        cell_sems = np.array([sems.get((participant, condition, int(f)), np.nan)
                              for f in sub["finger_id"]], dtype=float)
        mean = float(np.mean(vals))
        sd_raw = float(np.std(vals, ddof=1)) if vals.size >= 2 else np.nan
        noise_var = float(np.nanmean(cell_sems ** 2)) if np.isfinite(cell_sems).any() else np.nan
        if np.isfinite(sd_raw) and np.isfinite(noise_var):
            sd_corrected = float(np.sqrt(max(sd_raw ** 2 - noise_var, 0.0)))
        else:
            sd_corrected = np.nan
        rows.append({
            "participant": participant, "condition": condition,
            "mean": mean,
            "sd_raw": sd_raw,
            "sd_corrected": sd_corrected,
            "range": float(np.max(vals) - np.min(vals)) if vals.size else np.nan,
            "cv_raw": sd_raw / mean if mean else np.nan,
            "cv_corrected": sd_corrected / mean if mean else np.nan,
            "mean_cell_sem": float(np.nanmean(cell_sems)) if np.isfinite(cell_sems).any() else np.nan,
            "n_fingers": int(vals.size),
        })
    return pd.DataFrame(rows, columns=cols)


def _paired_test(diffs: np.ndarray) -> dict:
    """Paired t-test plus Wilcoxon on the participants' C - B change,
    with the descriptive interval and dz. Both tests are reported: at
    n = 7 the t-test needs the differences to be roughly normal and the
    Wilcoxon does not, and disagreement between them is itself news."""
    out = dict(fc.paired_summary(diffs))
    out.update(t=np.nan, p_t=np.nan, w=np.nan, p_wilcoxon=np.nan, note=None)
    vals = np.asarray([v for v in np.asarray(diffs, dtype=float) if np.isfinite(v)])
    if vals.size < MIN_TEST_N:
        out["note"] = f"requires N ≥ {MIN_TEST_N} participants (have {vals.size})"
        return out
    if np.all(vals == 0):
        out["note"] = "all paired differences are zero"
        return out
    t, p = sstats.ttest_1samp(vals, 0.0)
    out["t"], out["p_t"] = float(t), float(p)
    try:
        w, pw = sstats.wilcoxon(vals)
        out["w"], out["p_wilcoxon"] = float(w), float(pw)
    except ValueError as e:
        out["note"] = f"Wilcoxon not computable ({e})"
    return out


def equalisation_analysis(event_rows: List[dict], metric: str = "rt_complete_s",
                          baseline: str = fc.BASELINE_CONDITION,
                          cued: str = fc.CUED_CONDITION) -> dict:
    """Paired B -> C comparison of across-finger dispersion. Negative
    differences mean the cued condition is the more even one."""
    table = dispersion_table(event_rows, metric, [baseline, cued])
    n = int(table["participant"].nunique()) if len(table) else 0
    result = {
        "metric": metric, "baseline": baseline, "cued": cued,
        "n_participants": n, "table": table,
        "exploratory": n < EXPLORATORY_N,
        "measures": [], "means": {}, "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with a complete "
            f"{baseline}/{cued} × {len(fc.FINGER_IDS)}-finger grid on {metric} (have {n})")
        return result

    wide = table.pivot(index="participant", columns="condition")
    for condition in (baseline, cued):
        result["means"][condition] = {
            "mean": float(wide[("mean", condition)].mean()),
            "mean_cell_sem": float(wide[("mean_cell_sem", condition)].mean()),
        }
    # A purely proportional speed-up predicts exactly this shrink factor
    # for sd and range, and none at all for cv - the reference the raw
    # measures have to beat before "equalisation" means anything.
    ratio = result["means"][cued]["mean"] / result["means"][baseline]["mean"] \
        if result["means"][baseline]["mean"] else np.nan
    result["proportional_null_ratio"] = float(ratio) if np.isfinite(ratio) else np.nan

    for key, label, scale_free in MEASURES:
        b = wide[(key, baseline)].to_numpy(dtype=float)
        c = wide[(key, cued)].to_numpy(dtype=float)
        diffs = c - b
        entry = {
            "key": key, "label": label, "scale_free": scale_free,
            "baseline_mean": float(np.nanmean(b)) if np.isfinite(b).any() else np.nan,
            "cued_mean": float(np.nanmean(c)) if np.isfinite(c).any() else np.nan,
            "ratio": (float(np.nanmean(c) / np.nanmean(b))
                      if np.isfinite(b).any() and np.nanmean(b) else np.nan),
            "n_shrunk": int(np.sum(diffs < 0)),
            "n_pairs": int(np.sum(np.isfinite(diffs))),
            "test": _paired_test(diffs),
        }
        result["measures"].append(entry)
    return result


def verdict(result: dict) -> Optional[str]:
    """One sentence that refuses to call a proportional speed-up
    equalisation: the scale-free measure has to move too."""
    if result.get("reason"):
        return None
    by = {m["key"]: m for m in result["measures"]}
    if "cv_raw" not in by or "sd_corrected" not in by:
        return None
    cv, sd = by["cv_raw"], by["sd_corrected"]
    cv_down = np.isfinite(cv["test"]["mean"]) and cv["test"]["mean"] < 0
    sd_down = np.isfinite(sd["test"]["mean"]) and sd["test"]["mean"] < 0
    if cv_down and sd_down:
        return ("Both the noise-corrected SD and the scale-free CV fall from "
                f"{result['baseline']} to {result['cued']}: the evening-out is not "
                "explained by the overall speed-up alone.")
    if sd_down and not cv_down:
        return ("The absolute spread falls but the coefficient of variation does not — "
                "consistent with a proportional speed-up rather than genuine "
                "inter-finger equalisation.")
    if cv_down and not sd_down:
        return ("The scale-free CV falls while the absolute SD does not — a weak and "
                "internally inconsistent signal; treat as no equalisation evidence.")
    return ("Neither the noise-corrected SD nor the CV falls: no evidence of "
            "inter-finger equalisation in this sample.")
