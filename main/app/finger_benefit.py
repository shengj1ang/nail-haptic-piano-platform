"""Analysis 1 of the finger-benefit family: is the vibrotactile benefit
COMPENSATORY - larger on the digits that are already slow under the
visual cue - or a uniform speed-up applied to every digit alike?

The question is asked as a correlation, over (participant x finger)
cells, between

    Benefit = RT_B - RT_C          and    the finger's baseline RT_B.

A positive correlation means the slow digits gained more, i.e. the
haptic cue compensates rather than merely accelerates.

Why this cannot be answered with the obvious regression
-------------------------------------------------------
Benefit contains RT_B, so the two axes share RT_B's sampling noise.
With RT_B = mu_B + e_B and RT_C = mu_C + e_C for one cell,

    Cov(Benefit, RT_B) = Var(mu_B) + Var(e_B) - Cov(RT_B, RT_C)

and because the two conditions' cells are estimated from disjoint events
their noise is independent, Cov(e_B, e_C) = 0. The Var(e_B) term
survives: a POSITIVE "slower fingers benefit more" correlation is
produced by the arithmetic alone, in exactly the direction of the
hypothesis, even when the true benefit is identical for all five digits.
Reporting the naive r as evidence would therefore be circular.

Three estimators are computed and shown together:

  naive       r(Benefit, RT_B).  The quantity as usually written down.
              Kept because readers expect it, labelled as coupled.
  oldham      r(Benefit, (RT_B + RT_C)/2).  Oldham's (1962) method: the
              average is used as the x-axis because
              Cov(Benefit, mean) = (Var(RT_B) - Var(RT_C))/2, which is
              zero under the null when the two conditions have equal
              variance - so the coupling term cancels. Caveat, and it
              matters here: if the haptic cue really does compress the
              spread across fingers (see app.finger_equalisation) then
              Var(RT_B) > Var(RT_C) and Oldham is biased positive too,
              just far less than the naive version.
  split_half  the only estimator with no coupling at all. The baseline
              x-axis is RT_B measured on one half of the trials; the
              benefit y-axis is RT_B on the OTHER half minus RT_C (which
              never enters the x-axis, so it may use all trials). The
              two halves' noise is independent by construction, so
              E[r] = 0 under a uniform benefit. It costs precision -
              each half carries about half the events - which is the
              price of an unbiased answer.

The GAP between naive and split_half is the size of the artefact, and is
more informative than either number alone.

Inference. The independent unit is the participant, never the cell. Each
estimator is summarised two ways: a two-stage (summary-measures) test -
one Pearson r per participant over their own five fingers, Fisher
z-transformed, then a one-sample t-test of mean z against 0 with
df = N-1 - and, when pingouin is available, a repeated-measures
correlation (pg.rm_corr) which pools the cells after removing each
participant's own intercept. The two-stage test is the conservative
primary; rm_corr is the more powerful secondary that assumes a common
slope across participants.
"""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats as sstats

from . import finger_common as fc
from .group_analysis import EXPLORATORY_N, MIN_TEST_N, load_pingouin, per_finger_metrics

# Slope units are the metric's own units per unit of baseline, i.e.
# dimensionless for RT: "ms of benefit per ms of baseline RT".
ESTIMATORS = [
    ("naive", "Naive — benefit vs baseline RT_B",
     "baseline RT under B",
     "Mathematically coupled: the y-axis contains the x-axis, which "
     "manufactures a positive correlation even under a uniform benefit. "
     "Shown for completeness, not as evidence."),
    ("oldham", "Oldham — benefit vs the (B, C) average",
     "mean of B and C RT",
     "Oldham's correction removes the shared-noise term; residual bias "
     "remains only to the extent that B and C differ in variance."),
    ("split_half", "Split-half — baseline and benefit from disjoint trials",
     "baseline RT under B (half 1 of the trials)",
     "No coupling by construction: the baseline is measured on trials "
     "that contribute nothing to the benefit. Noisier, but unbiased."),
]


def _pearson(x: np.ndarray, y: np.ndarray):
    """Pearson r over one participant's fingers. NaN (not 0) whenever the
    correlation is undefined - too few points, or a constant axis."""
    if x.size < 3 or np.std(x) == 0 or np.std(y) == 0:
        return np.nan, np.nan
    r, p = sstats.pearsonr(x, y)
    return float(r), float(p)


def _slope(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 2 or np.std(x) == 0:
        return np.nan
    return float(np.polyfit(x, y, 1)[0])


def _per_participant(points: pd.DataFrame) -> pd.DataFrame:
    """One row per participant: their own r, Fisher z and OLS slope over
    their five finger cells."""
    rows = []
    for participant, sub in points.groupby("participant", sort=True):
        x = sub["x"].to_numpy(dtype=float)
        y = sub["y"].to_numpy(dtype=float)
        keep = np.isfinite(x) & np.isfinite(y)
        x, y = x[keep], y[keep]
        r, p = _pearson(x, y)
        rows.append({
            "participant": participant, "n_points": int(x.size),
            "r": r, "p": p,
            # arctanh is undefined at |r| = 1, which 5 points can reach.
            "z": float(np.arctanh(np.clip(r, -0.9999, 0.9999))) if np.isfinite(r) else np.nan,
            "slope": _slope(x, y),
        })
    return pd.DataFrame(rows, columns=["participant", "n_points", "r", "p", "z", "slope"])


def _two_stage(per_participant: pd.DataFrame) -> dict:
    """One-sample t-test of the participants' Fisher z against 0, with
    the mean and interval transformed back to the r scale for reading.
    n counts participants - the cells are not independent observations."""
    zs = per_participant["z"].dropna().to_numpy(dtype=float)
    n = int(zs.size)
    out = {"n": n, "mean_z": np.nan, "r": np.nan, "t": np.nan, "df": n - 1,
           "p": np.nan, "ci95_lo_r": np.nan, "ci95_hi_r": np.nan, "note": None}
    if n < MIN_TEST_N:
        out["note"] = f"requires N ≥ {MIN_TEST_N} participants with a defined r (have {n})"
        if n:
            out["mean_z"] = float(np.mean(zs))
            out["r"] = float(np.tanh(out["mean_z"]))
        return out
    mean_z = float(np.mean(zs))
    sd = float(np.std(zs, ddof=1))
    if sd == 0:
        out.update(mean_z=mean_z, r=float(np.tanh(mean_z)),
                   note="all participants share an identical correlation (no spread to test)")
        return out
    t, p = sstats.ttest_1samp(zs, 0.0)
    half = float(sstats.t.ppf(0.975, n - 1)) * sd / np.sqrt(n)
    out.update(mean_z=mean_z, r=float(np.tanh(mean_z)), t=float(t), p=float(p),
               ci95_lo_r=float(np.tanh(mean_z - half)),
               ci95_hi_r=float(np.tanh(mean_z + half)))
    return out


def _slope_summary(per_participant: pd.DataFrame) -> dict:
    """Group test on the per-participant OLS slopes - the same two-stage
    logic in the metric's own units, which read more concretely than r."""
    slopes = per_participant["slope"].dropna().to_numpy(dtype=float)
    summary = fc.paired_summary(slopes)
    summary["t"] = summary["p"] = np.nan
    if summary["n"] >= MIN_TEST_N and summary["sd"] and summary["sd"] > 0:
        t, p = sstats.ttest_1samp(slopes, 0.0)
        summary["t"], summary["p"] = float(t), float(p)
    return summary


def _rm_corr(points: pd.DataFrame) -> Optional[dict]:
    """Repeated-measures correlation: pools the cells but fits a separate
    intercept per participant, so between-participant differences cannot
    drive it. None when pingouin is missing or the fit fails."""
    pg, error = load_pingouin()
    if error:
        return {"available": False, "note": error}
    data = points.dropna(subset=["x", "y"])
    if data["participant"].nunique() < 3:
        return {"available": False, "note": "needs at least 3 participants"}
    try:
        res = pg.rm_corr(data=data, x="x", y="y", subject="participant")
    except Exception as e:
        return {"available": False, "note": f"{type(e).__name__}: {e}"}
    row = res.iloc[0]
    # The interval column is "CI95" in pingouin >= 0.6 and "CI95%" before.
    ci = next((row[name] for name in ("CI95", "CI95%") if name in row.index), None)
    lo, hi = (float(ci[0]), float(ci[1])) if ci is not None else (np.nan, np.nan)
    return {"available": True, "r": float(row["r"]), "dof": int(row["dof"]),
            "p": float(row["pval"]), "ci95_lo": lo, "ci95_hi": hi}


# ---------------------------------------------------------------------------
# Point sets, one per estimator

def _coupled_points(pairs: pd.DataFrame, x_col: str) -> pd.DataFrame:
    return pd.DataFrame({
        "participant": pairs["participant"], "finger_id": pairs["finger_id"],
        "x": pairs[x_col].astype(float), "y": pairs["benefit"].astype(float),
    })


def _split_half_points(event_rows: List[dict], metric: str,
                       baseline: str, cued: str) -> pd.DataFrame:
    """x = baseline measured on half 1 of the trials; y = the benefit with
    its baseline side measured on half 2. The cued side may use all
    trials: it never appears on the x-axis, so it introduces no coupling
    and using every event keeps that term as precise as possible."""
    cols = ["participant", "finger_id", "x", "y"]
    pf_a, pf_b = fc.split_half_cells(event_rows)
    pf_full = per_finger_metrics(event_rows)
    if pf_a.empty or pf_b.empty or pf_full.empty:
        return pd.DataFrame(columns=cols)

    def cell_map(pf, condition):
        sub = pf[pf["condition"] == condition]
        return {(r.participant, int(r.finger_id)): float(getattr(r, metric))
                for r in sub.itertuples() if np.isfinite(getattr(r, metric))}

    base_a = cell_map(pf_a, baseline)
    base_b = cell_map(pf_b, baseline)
    cued_full = cell_map(pf_full, cued)
    sign = fc.benefit_sign(metric)
    rows = []
    for key in sorted(set(base_a) & set(base_b) & set(cued_full)):
        participant, fid = key
        rows.append({"participant": participant, "finger_id": fid,
                     "x": base_a[key], "y": sign * (base_b[key] - cued_full[key])})
    frame = pd.DataFrame(rows, columns=cols)
    if frame.empty:
        return frame
    # Keep only participants who still have all five fingers after the
    # split, so this estimator describes the same complete-case logic.
    complete = (frame.groupby("participant")["finger_id"].nunique()
                == len(fc.FINGER_IDS))
    keep = set(complete[complete].index)
    return frame[frame["participant"].isin(keep)].reset_index(drop=True)


def compensation_analysis(event_rows: List[dict], metric: str = "rt_complete_s",
                          baseline: str = fc.BASELINE_CONDITION,
                          cued: str = fc.CUED_CONDITION) -> dict:
    """Does the benefit grow with the baseline? Returns one entry per
    estimator (naive / Oldham / split-half), each with its points, the
    per-participant correlations and slopes, the two-stage group test and
    the repeated-measures correlation. Never raises for a data reason."""
    pf = per_finger_metrics(event_rows)
    pairs, dropped = fc.paired_finger_cells(pf, metric, baseline, cued)
    n = int(pairs["participant"].nunique()) if len(pairs) else 0
    result = {
        "metric": metric, "baseline": baseline, "cued": cued,
        "n_participants": n, "dropped": dropped,
        "dropped_note": fc.describe_dropped(dropped),
        "exploratory": n < EXPLORATORY_N,
        "pairs": pairs, "estimators": [], "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with a complete "
            f"{baseline}/{cued} × {len(fc.FINGER_IDS)}-finger grid on {metric} (have {n})")
        return result

    point_sets = {
        "naive": _coupled_points(pairs, "baseline"),
        "oldham": _coupled_points(pairs, "average"),
        "split_half": _split_half_points(event_rows, metric, baseline, cued),
    }
    for key, label, x_label, caveat in ESTIMATORS:
        points = point_sets[key]
        per_p = _per_participant(points) if len(points) else pd.DataFrame(
            columns=["participant", "n_points", "r", "p", "z", "slope"])
        result["estimators"].append({
            "key": key, "label": label, "x_label": x_label, "caveat": caveat,
            "n_participants": int(points["participant"].nunique()) if len(points) else 0,
            "n_points": len(points),
            "points": points,
            "per_participant": per_p,
            "group": _two_stage(per_p),
            "slope": _slope_summary(per_p),
            "rm_corr": _rm_corr(points) if len(points) else None,
        })
    return result


def artefact_gap(result: dict) -> Optional[Dict[str, float]]:
    """How much of the naive correlation is arithmetic rather than
    finding: naive r minus split-half r, both on the two-stage scale.
    None when either estimator did not produce a group r."""
    by = {e["key"]: e for e in result.get("estimators", [])}
    if "naive" not in by or "split_half" not in by:
        return None
    naive_r = by["naive"]["group"]["r"]
    clean_r = by["split_half"]["group"]["r"]
    if not (np.isfinite(naive_r) and np.isfinite(clean_r)):
        return None
    return {"naive_r": float(naive_r), "split_half_r": float(clean_r),
            "gap": float(naive_r - clean_r)}
