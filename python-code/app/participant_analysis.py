"""GUI-free computations behind the Participant Analysis window's
Trade-off / Errors / Confusion tabs (app/gui/participant_analysis_window.py).

Input rows are the unified per-trial / per-event dicts produced by
app.participant_export.collect_participant_data - that export schema IS
the adapter layer over the raw quiz files: it already carries the final
(hand-verified where corrected) actual_finger and finger_correct
verdicts, the reaction times under the existing filtering rules, and
the calibrated key indices. Nothing here re-derives correctness or
re-filters RTs; it only aggregates the final fields, so these numbers
can never drift from the CSVs or the Quiz Analysis table.

Group-analysis ready: every compute_* function takes plain row lists
(or an already-concatenated list covering SEVERAL participants - the
participant column just passes through) and returns a tidy pandas
DataFrame with no GUI dependency, so the future multi-participant
window can call the same functions on pooled rows and aggregate with
groupby("participant", ...).
"""

import statistics
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# Mutually exclusive per-event outcome categories, display order.
CAT_NO_RESPONSE = "No response (timeout)"
CAT_UNRESOLVED = "Unresolved finger"
CAT_CK_CF = "Correct key + correct finger"
CAT_CK_WF = "Correct key + wrong finger"
CAT_WK_CF = "Wrong key + correct finger"
CAT_WK_WF = "Wrong key + wrong finger"
CATEGORIES = [CAT_CK_CF, CAT_CK_WF, CAT_WK_CF, CAT_WK_WF, CAT_UNRESOLVED, CAT_NO_RESPONSE]
ERROR_CATEGORIES = [CAT_CK_WF, CAT_WK_CF, CAT_WK_WF, CAT_UNRESOLVED, CAT_NO_RESPONSE]

FINGER_ORDER = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]
UNRESOLVED = "unresolved"  # actual-finger value used in the confusion long table


# ---------------------------------------------------------------------------
# Speed-accuracy trade-off (trial level)


def compute_trial_speed_accuracy(trial_rows: List[dict]) -> pd.DataFrame:
    """One row per trial: participant, condition, trial_index, level,
    sequence, rt_ms, fa_pct, included.

    A trial with no valid correct-key RT (rt_correct_key_s is None under
    the existing filtering rule) or without an analyzed FA gets
    included=False and NaN metrics - it is counted, never plotted as 0.
    FA main keeps its stored denominator (all target events)."""
    rows = []
    for t in trial_rows:
        rt = t["rt_correct_key_s"]
        fa = t["fa_main"] if t["analyzed"] else None
        included = rt is not None and fa is not None
        rows.append(
            {
                "participant": t.get("participant"),
                "condition": t["condition"],
                "trial_index": t["trial_index"],
                "level": t["level"],
                "sequence": t["sequence"],
                "rt_ms": rt * 1000 if included else np.nan,
                "fa_pct": fa * 100 if included else np.nan,
                "included": included,
            }
        )
    return pd.DataFrame(rows)


def speed_accuracy_centroids(df: pd.DataFrame) -> pd.DataFrame:
    """Per-condition centroid of the included trials: n, mean and SD of
    rt_ms / fa_pct. Aggregates over everything in df - feed it one
    participant's rows or many."""
    inc = df[df["included"]]
    out = (
        inc.groupby("condition")
        .agg(
            n=("rt_ms", "size"),
            rt_ms=("rt_ms", "mean"),
            fa_pct=("fa_pct", "mean"),
            rt_sd_ms=("rt_ms", "std"),
            fa_sd_pct=("fa_pct", "std"),
        )
        .reset_index()
    )
    return out


def tradeoff_verdict(centroids: pd.DataFrame) -> Optional[str]:
    """Descriptive B-vs-C sentence (no statistics). None when either
    centroid is missing."""
    by_c = centroids.set_index("condition")
    if "B" not in by_c.index or "C" not in by_c.index:
        return None
    b, c = by_c.loc["B"], by_c.loc["C"]
    more_accurate = c["fa_pct"] > b["fa_pct"]
    faster = c["rt_ms"] < b["rt_ms"]
    if more_accurate and faster:
        text = ("For this participant, vibrotactile guidance was both faster and more "
                "accurate than visual finger guidance.")
    elif more_accurate:
        text = ("The higher haptic accuracy was accompanied by slower responses, "
                "indicating a possible speed-accuracy trade-off.")
    elif faster:
        text = ("Haptic responses were faster but less accurate than visual ones, "
                "indicating a possible speed-accuracy trade-off in the opposite direction.")
    else:
        text = "Visual guidance was both faster and more accurate for this participant."
    return text + " Descriptive only; inferential comparison is performed at the group level."


# ---------------------------------------------------------------------------
# Event-level error breakdown


def classify_event_outcome(e) -> str:
    """Mutually exclusive category from the FINAL verified fields.

    - timeout -> No response (kept out of the key/finger cells);
    - responded but no usable finger verdict (no detected/corrected
      finger, or no verdict at all) -> Unresolved, reported as its own
      category rather than silently folded into "wrong finger" (the main
      FA definition counts it as incorrect - stated in the captions);
    - otherwise key_correct x finger_correct."""
    if e["timed_out"]:
        return CAT_NO_RESPONSE
    if e["actual_finger"] is None or e["finger_correct"] is None:
        return CAT_UNRESOLVED
    if e["key_correct"]:
        return CAT_CK_CF if e["finger_correct"] else CAT_CK_WF
    return CAT_WK_CF if e["finger_correct"] else CAT_WK_WF


def classify_event_outcomes(event_rows: List[dict]) -> pd.DataFrame:
    """The event rows as a DataFrame plus an "outcome" category column."""
    df = pd.DataFrame(event_rows)
    df["outcome"] = [classify_event_outcome(e) for e in event_rows]
    return df


def compute_error_breakdown(event_rows: List[dict]) -> pd.DataFrame:
    """Tidy counts: one row per (condition, category), zero-filled so
    pivots stay rectangular. Add "participant" to the groupby when
    aggregating pooled rows later."""
    df = classify_event_outcomes(event_rows)
    counts = df.groupby(["condition", "outcome"]).size()
    conditions = sorted(df["condition"].unique())
    rows = [
        {"condition": c, "category": cat, "count": int(counts.get((c, cat), 0))}
        for c in conditions
        for cat in CATEGORIES
    ]
    return pd.DataFrame(rows)


def relative_reduction(b_count: int, c_count: int) -> Optional[float]:
    """(B - C) / B as a fraction; None when B is 0 (undefined, per spec)."""
    if b_count == 0:
        return None
    return (b_count - c_count) / b_count


# ---------------------------------------------------------------------------
# Finger confusion


def compute_finger_confusion(event_rows: List[dict]) -> pd.DataFrame:
    """Tidy long form over responded events with a known target finger:
    one row per (condition, target_finger, actual_finger, count), where
    actual_finger is a finger label or "unresolved". Cross-hand errors
    are ordinary rows - nothing assumes target and actual share a hand."""
    rows: Dict[tuple, int] = {}
    for e in event_rows:
        if e["timed_out"] or e["target_finger"] not in FINGER_ORDER:
            continue
        actual = e["actual_finger"] if e["actual_finger"] in FINGER_ORDER else UNRESOLVED
        key = (e["condition"], e["target_finger"], actual)
        rows[key] = rows.get(key, 0) + 1
    return pd.DataFrame(
        [{"condition": c, "target_finger": t, "actual_finger": a, "count": n}
         for (c, t, a), n in sorted(rows.items())]
    )


def confusion_matrix(confusion_df: pd.DataFrame, condition: str) -> Dict[str, object]:
    """10x10 counts for one condition (rows: target, cols: actual, both
    in FINGER_ORDER) plus per-target unresolved counts and the total
    responded-event count."""
    sub = confusion_df[confusion_df["condition"] == condition] if len(confusion_df) else confusion_df
    matrix = [[0] * len(FINGER_ORDER) for _ in FINGER_ORDER]
    unresolved = {f: 0 for f in FINGER_ORDER}
    total = 0
    for row in sub.itertuples():
        total += row.count
        if row.actual_finger == UNRESOLVED:
            unresolved[row.target_finger] += row.count
        else:
            matrix[FINGER_ORDER.index(row.target_finger)][FINGER_ORDER.index(row.actual_finger)] += row.count
    return {"matrix": matrix, "unresolved": unresolved, "total": total}


def top_confusion(matrix: List[List[int]]) -> Optional[dict]:
    """Most frequent off-diagonal cell: {"target", "actual", "n"}."""
    best = None
    for i, row in enumerate(matrix):
        for j, n in enumerate(row):
            if i != j and n > 0 and (best is None or n > best["n"]):
                best = {"target": FINGER_ORDER[i], "actual": FINGER_ORDER[j], "n": n}
    return best


def confusion_pair_count(matrix: List[List[int]], finger_a: str, finger_b: str) -> int:
    """Substitutions between two fingers, both directions (e.g. ring-little)."""
    i, j = FINGER_ORDER.index(finger_a), FINGER_ORDER.index(finger_b)
    return matrix[i][j] + matrix[j][i]


# ---------------------------------------------------------------------------
# Wrong-key distance


def compute_wrong_key_distance(event_rows: List[dict]) -> pd.DataFrame:
    """One row per wrong-key event (valid pressed key != target key):
    participant, condition, trial_index, event_index, distance, metric.

    Prefers the calibrated key-index difference (keyboard-profile order -
    the true "how many keys away" on a white-key layout); events missing
    a key id fall back to MIDI semitone distance, with the metric used
    recorded per row so the GUI can report it."""
    rows = []
    for e in event_rows:
        if e["timed_out"] or e["actual_note"] is None or e["key_correct"]:
            continue
        if e.get("target_key_id") is not None and e.get("actual_key_id") is not None:
            dist, metric = abs(e["actual_key_id"] - e["target_key_id"]), "key-index"
        else:
            dist, metric = abs(e["actual_note"] - e["target_note"]), "semitones"
        rows.append(
            {
                "participant": e.get("participant"),
                "condition": e["condition"],
                "trial_index": e.get("trial_index"),
                "event_index": e.get("event_index"),
                "distance": dist,
                "metric": metric,
            }
        )
    return pd.DataFrame(rows, columns=["participant", "condition", "trial_index", "event_index",
                                       "distance", "metric"])


def wrong_key_stats(distance_df: pd.DataFrame) -> pd.DataFrame:
    """Per-condition n / median / mean / max distance plus the metric(s)
    actually used."""
    if distance_df.empty:
        return pd.DataFrame(columns=["condition", "n", "median", "mean", "max", "metric"])
    out = []
    for c, sub in distance_df.groupby("condition"):
        d = sub["distance"]
        out.append(
            {
                "condition": c,
                "n": len(d),
                "median": statistics.median(d),
                "mean": d.mean(),
                "max": d.max(),
                "metric": " + ".join(sorted(sub["metric"].unique())),
            }
        )
    return pd.DataFrame(out)
