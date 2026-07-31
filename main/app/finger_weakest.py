"""Analysis 3 of the finger-benefit family: does the vibrotactile cue
help each participant's OWN weakest digit most?

Comparing a fixed pair (little vs index) assumes every participant is
limited by the same finger. Instead each participant's weakest finger is
identified under the visual baseline B - slowest by reaction time, or
least accurate by finger accuracy - and the question becomes whether the
B -> C benefit at that self-selected finger exceeds the benefit averaged
over that participant's other four.

Regression to the mean is the whole problem
-------------------------------------------
Selecting the extreme of five noisily-estimated cells and then
re-measuring it is the textbook setup for regression to the mean. A cell
is picked BECAUSE its estimate is high, which means its sampling error
is more likely to have been positive; on any fresh measurement it falls
back towards the participant's own average. With a benefit defined as
RT_B - RT_C, and RT_B being exactly the quantity the selection was made
on, the selected finger shows an inflated "benefit" even when the haptic
cue does nothing at all for it. The effect is largest precisely where
cells are noisiest, and it points in the direction of the hypothesis.

Two estimates are therefore reported for every criterion:

  naive       selection and measurement on the same trials. This is the
              analysis as usually described, and it is biased upwards.
  split_half  the weakest finger is chosen from one half of the trials
              and the benefit is measured on the other half (the C side
              may use all trials - it plays no part in the selection).
              Selection noise and measurement noise are then independent,
              so under a uniform benefit the expected advantage is zero.

The difference between the two is the regression-to-the-mean component.

Selection stability is reported first, and can settle the question on
its own: the weakest finger is identified independently in each half of
the trials and the two answers are compared. If they agree barely more
often than the 1-in-5 chance rate, then "this participant's weakest
finger" is not a stable property of the participant but a description of
that half's noise, and no downstream number about it means anything -
whichever way the test comes out.

Inference is on the participants' paired advantage (benefit at the
weakest finger minus the mean benefit at the other four): mean, 95%
t-CI, Cohen's dz, a one-sample t-test and a Wilcoxon signed-rank, all
with n = participants.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as sstats

from . import finger_common as fc
from .group_analysis import EXPLORATORY_N, MIN_TEST_N, per_finger_metrics

# (key, label, metric, higher_is_worse) - how "weakest" is defined.
CRITERIA = [
    ("rt", "slowest finger", "rt_complete_s", True),
    ("fa", "least accurate finger", "fa", False),
]

CHANCE_AGREEMENT = 1.0 / len(fc.FINGER_IDS)


def _cell_map(pf: pd.DataFrame, condition: str, metric: str) -> Dict[Tuple[str, int], float]:
    if pf.empty or metric not in pf.columns:
        return {}
    sub = pf[pf["condition"] == condition]
    return {(r.participant, int(r.finger_id)): float(getattr(r, metric))
            for r in sub.itertuples() if np.isfinite(getattr(r, metric))}


def weakest_by_participant(cells: Dict[Tuple[str, int], float],
                           higher_is_worse: bool) -> Dict[str, int]:
    """{participant: their weakest finger_id} from one cell map. Only
    participants with all five fingers present are considered - picking
    "the worst of three" would not be the same quantity. Ties resolve to
    the lowest finger ID, deterministically."""
    by_participant: Dict[str, Dict[int, float]] = {}
    for (participant, fid), value in cells.items():
        by_participant.setdefault(participant, {})[fid] = value
    out = {}
    for participant, fingers in by_participant.items():
        if len(fingers) < len(fc.FINGER_IDS):
            continue
        ordered = sorted(fingers.items(), key=lambda kv: (-kv[1] if higher_is_worse else kv[1],
                                                          kv[0]))
        out[participant] = int(ordered[0][0])
    return out


def selection_stability(event_rows: List[dict], select_metric: str,
                        higher_is_worse: bool,
                        baseline: str = fc.BASELINE_CONDITION) -> dict:
    """Is "this participant's weakest finger" a stable property or a
    description of noise? The weakest finger is identified independently
    in each half of the trials and the two answers compared against the
    1-in-5 chance rate."""
    pf_a, pf_b = fc.split_half_cells(event_rows)
    pick_a = weakest_by_participant(_cell_map(pf_a, baseline, select_metric), higher_is_worse)
    pick_b = weakest_by_participant(_cell_map(pf_b, baseline, select_metric), higher_is_worse)
    shared = sorted(set(pick_a) & set(pick_b))
    agree = [p for p in shared if pick_a[p] == pick_b[p]]
    n = len(shared)
    rate = len(agree) / n if n else np.nan
    p_binom = np.nan
    if n:
        # One-sided: is agreement better than picking a finger at random?
        p_binom = float(sstats.binomtest(len(agree), n, CHANCE_AGREEMENT,
                                         alternative="greater").pvalue)
    return {
        "n_compared": n, "n_agree": len(agree), "agreement_rate": rate,
        "chance_rate": CHANCE_AGREEMENT, "p_vs_chance": p_binom,
        "half1": pick_a, "half2": pick_b,
        "agreeing_participants": agree,
    }


def _advantage_rows(picks: Dict[str, int], benefit: Dict[Tuple[str, int], float]) -> pd.DataFrame:
    """Per participant: the benefit at their selected finger, the mean
    benefit over their other four, and the difference between them."""
    rows = []
    for participant, weakest in sorted(picks.items()):
        others = [benefit[(participant, f)] for f in fc.FINGER_IDS
                  if f != weakest and (participant, f) in benefit]
        if (participant, weakest) not in benefit or len(others) < 2:
            continue
        weak_benefit = benefit[(participant, weakest)]
        others_mean = float(np.mean(others))
        rows.append({
            "participant": participant,
            "weakest_finger": weakest,
            "weakest_label": fc.finger_label(weakest),
            "benefit_weakest": weak_benefit,
            "benefit_others_mean": others_mean,
            "n_others": len(others),
            "advantage": weak_benefit - others_mean,
        })
    return pd.DataFrame(rows, columns=["participant", "weakest_finger", "weakest_label",
                                       "benefit_weakest", "benefit_others_mean",
                                       "n_others", "advantage"])


def _test_advantage(advantages: np.ndarray) -> dict:
    out = dict(fc.paired_summary(advantages))
    out.update(t=np.nan, p_t=np.nan, w=np.nan, p_wilcoxon=np.nan, note=None)
    vals = np.asarray([v for v in np.asarray(advantages, dtype=float) if np.isfinite(v)])
    if vals.size < MIN_TEST_N:
        out["note"] = f"requires N ≥ {MIN_TEST_N} participants (have {vals.size})"
        return out
    if np.all(vals == 0):
        out["note"] = "all advantages are exactly zero"
        return out
    t, p = sstats.ttest_1samp(vals, 0.0)
    out["t"], out["p_t"] = float(t), float(p)
    try:
        w, pw = sstats.wilcoxon(vals)
        out["w"], out["p_wilcoxon"] = float(w), float(pw)
    except ValueError as e:
        out["note"] = f"Wilcoxon not computable ({e})"
    return out


def _benefit_map(pf_baseline: pd.DataFrame, pf_cued: pd.DataFrame, metric: str,
                 baseline: str, cued: str) -> Dict[Tuple[str, int], float]:
    """Signed so positive always means the cued condition is better,
    taking the baseline side from one table and the cued side from
    another (the same table for the naive estimate, disjoint halves for
    the split-half one)."""
    base = _cell_map(pf_baseline, baseline, metric)
    cue = _cell_map(pf_cued, cued, metric)
    sign = fc.benefit_sign(metric)
    return {k: sign * (base[k] - cue[k]) for k in set(base) & set(cue)}


def weakest_finger_analysis(event_rows: List[dict], criterion: str = "rt",
                            benefit_metric: str = "rt_complete_s",
                            baseline: str = fc.BASELINE_CONDITION,
                            cued: str = fc.CUED_CONDITION) -> dict:
    """Does the participant's own weakest finger gain more than their
    others? Returns the selection-stability diagnostic plus a naive and a
    split-half estimate of the advantage. Never raises for a data
    reason."""
    spec = next((c for c in CRITERIA if c[0] == criterion), None)
    if spec is None:
        raise ValueError(f"unknown criterion {criterion!r}; expected one of "
                         f"{[c[0] for c in CRITERIA]}")
    _key, label, select_metric, higher_is_worse = spec

    pf_full = per_finger_metrics(event_rows)
    pf_a, pf_b = fc.split_half_cells(event_rows)
    picks_full = weakest_by_participant(_cell_map(pf_full, baseline, select_metric),
                                        higher_is_worse)
    benefit_full = _benefit_map(pf_full, pf_full, benefit_metric, baseline, cued)
    # Split-half: select on half 1, take the benefit's baseline side from
    # half 2, and let the cued side use every trial - it is absent from
    # the selection, so it contributes no coupling and keeping it whole
    # keeps that term precise.
    picks_half = weakest_by_participant(_cell_map(pf_a, baseline, select_metric),
                                        higher_is_worse)
    benefit_half = _benefit_map(pf_b, pf_full, benefit_metric, baseline, cued)

    n = len(picks_full)
    result = {
        "criterion": criterion, "criterion_label": label,
        "select_metric": select_metric, "benefit_metric": benefit_metric,
        "baseline": baseline, "cued": cued,
        "n_participants": n,
        "exploratory": n < EXPLORATORY_N,
        "picks": picks_full,
        "finger_counts": {fid: sum(1 for f in picks_full.values() if f == fid)
                          for fid in fc.FINGER_IDS},
        "stability": selection_stability(event_rows, select_metric, higher_is_worse, baseline),
        "estimates": [],
        "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with all "
            f"{len(fc.FINGER_IDS)} fingers under {baseline} on {select_metric} (have {n})")
        return result

    for key, est_label, picks, benefit in (
        ("naive", "Naive — selected and measured on the same trials",
         picks_full, benefit_full),
        ("split_half", "Split-half — selected on half 1, measured on half 2",
         picks_half, benefit_half),
    ):
        table = _advantage_rows(picks, benefit)
        result["estimates"].append({
            "key": key, "label": est_label,
            "table": table,
            "n_participants": len(table),
            "test": _test_advantage(table["advantage"].to_numpy(dtype=float))
                    if len(table) else _test_advantage(np.array([])),
        })
    return result


def regression_to_mean_gap(result: dict) -> Optional[Dict[str, float]]:
    """Naive advantage minus split-half advantage: the part of the
    "weakest finger gains most" effect that selection alone produces."""
    by = {e["key"]: e for e in result.get("estimates", [])}
    if "naive" not in by or "split_half" not in by:
        return None
    a = by["naive"]["test"]["mean"]
    b = by["split_half"]["test"]["mean"]
    if not (np.isfinite(a) and np.isfinite(b)):
        return None
    return {"naive": float(a), "split_half": float(b), "gap": float(a - b)}


def stability_verdict(result: dict) -> str:
    """Whether the downstream numbers are worth reading at all."""
    s = result["stability"]
    if not s["n_compared"]:
        return "Selection stability could not be assessed (no participant had both halves complete)."
    rate, chance = s["agreement_rate"], s["chance_rate"]
    head = (f"The weakest finger picked from half 1 matches the one picked from half 2 for "
            f"{s['n_agree']}/{s['n_compared']} participants ({rate * 100:.0f}%, chance "
            f"{chance * 100:.0f}%).")
    if rate <= chance + 1e-9:
        return head + (" That is at or below chance: within this sample the weakest finger is "
                       "a property of the half, not of the participant, and every number below "
                       "should be read as descriptive noise.")
    if rate < 0.5:
        return head + (" That is above chance but far from reliable — treat the advantage "
                       "estimates below as weak, and prefer the split-half column.")
    return head + (" Stable enough for the self-selected weakest finger to be a meaningful "
                   "per-participant property; the split-half estimate is still the unbiased one.")
