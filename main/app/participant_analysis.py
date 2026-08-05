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
DataFrame with no GUI dependency, so the multi-participant window calls
the same functions on pooled rows and aggregates with
groupby("participant", ...).

That promise is why the participant-LEVEL half of the group analysis
lives here rather than in app.group_analysis: valid_events,
participant_condition_metrics, participant_cell_metrics,
within_cell_repetition, session_position_metrics,
participant_outcome_proportions, per_finger_metrics,
threshold_sensitivity and quality_summary all reduce one participant's
rows to that participant's own numbers, and both windows need exactly
those. app.group_analysis imports and re-exports them under the same
names, so it keeps only what genuinely needs more than one participant:
loading several exports, the group centre/interval over PARTICIPANT
values, the paired contrasts, and the repeated-measures ANOVA.
"""

import statistics
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .pilot_study import CONDITIONS as _CONDITIONS
from .sequence_generator import LEVELS as _LEVELS

CONDITIONS: List[str] = list(_CONDITIONS)
LEVELS: List[str] = list(_LEVELS)

# B and C provide the same target-finger information through different
# modalities and are therefore the only performance-comparable guidance
# conditions. A provides no target-finger cue: its hidden-target
# agreement and lower-choice-complexity RT remain descriptive
# task-reference measures, never a modality baseline.
GUIDANCE_CONDITIONS: List[str] = ["B", "C"]

# Homologous finger IDs (1 thumb ... 5 little), left/right merged by ID.
FINGER_IDS = [1, 2, 3, 4, 5]
FINGER_ID_NAMES = {1: "thumb", 2: "index", 3: "middle", 4: "ring", 5: "little"}

# Detection thresholds the FA sensitivity curve is recomputed at.
THRESHOLD_SENSITIVITY_VALUES = (0.30, 0.35, 0.40, 0.45, 0.50)

VALIDITY_INVALID_CARRYOVER = "invalid_carryover"  # mirrors app.quiz

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


# ---------------------------------------------------------------------------
# Event validity


def valid_events(event_rows: List[dict]) -> List[dict]:
    """Outcome-eligible events: everything except manually confirmed
    carry-over. This is the only filter - unresolved fingers and
    timeouts stay in (they are real outcomes under the main FA
    definition).

    The trial-level export already removed these events from every
    per-trial statistic, so any event-level tab that skips this filter
    silently disagrees with the trial-level numbers beside it."""
    return [e for e in event_rows if e.get("validity") != VALIDITY_INVALID_CARRYOVER]


# ---------------------------------------------------------------------------
# Trial rows -> participant-level cell means
#
# One participant's condition (and condition x level) means: the unit the
# group window then aggregates over, and the table the single-participant
# window exports.


def _nanmean(values) -> float:
    """Mean of the non-None values, NaN when there are none - missing
    data must never enter a mean as 0."""
    vals = [v for v in values if v is not None]
    return float(np.mean(vals)) if vals else np.nan


_METRIC_COLS = ["key_accuracy", "fa_main", "fa_given_key", "rt_correct_key_s", "rt_complete_s"]


def _trial_metric_row(trials: List[dict]) -> dict:
    """The shared metric definitions, identical to the Overview tab: FA
    metrics average analyzed trials only; RT metrics average trials with
    a valid (non-None) RT; key accuracy averages all trials. Missing
    data yields NaN, never 0."""
    analyzed = [t for t in trials if t.get("analyzed")]
    return {
        "n_trials": len(trials),
        "n_analyzed": len(analyzed),
        "key_accuracy": _nanmean([t["key_accuracy"] for t in trials]),
        "fa_main": _nanmean([t["fa_main"] for t in analyzed]),
        "fa_given_key": _nanmean([t["fa_given_key"] for t in analyzed]),
        "rt_correct_key_s": _nanmean([t["rt_correct_key_s"] for t in trials]),
        "rt_complete_s": _nanmean([t["rt_complete_s"] for t in trials]),
    }


def participant_condition_metrics(trial_rows: List[dict]) -> pd.DataFrame:
    """One row per (participant, condition): the participant-level means
    that are the unit of every group statistic. Conditions a participant
    never ran simply have no row (no zero-fill)."""
    rows = []
    df_keys = sorted({(t["participant"], t["condition"]) for t in trial_rows})
    for participant, condition in df_keys:
        trials = [t for t in trial_rows
                  if t["participant"] == participant and t["condition"] == condition]
        rows.append({"participant": participant, "condition": condition,
                     **_trial_metric_row(trials)})
    return pd.DataFrame(rows, columns=["participant", "condition", "n_trials", "n_analyzed",
                                       *_METRIC_COLS])


def participant_cell_metrics(trial_rows: List[dict]) -> pd.DataFrame:
    """One row per (participant, condition, level) cell - same metric
    definitions. Missing cells are absent rows, never zeros."""
    rows = []
    keys = sorted({(t["participant"], t["condition"], t["level"]) for t in trial_rows})
    for participant, condition, level in keys:
        trials = [t for t in trial_rows if t["participant"] == participant
                  and t["condition"] == condition and t["level"] == level]
        rows.append({"participant": participant, "condition": condition, "level": level,
                     **_trial_metric_row(trials)})
    return pd.DataFrame(rows, columns=["participant", "condition", "level",
                                       "n_trials", "n_analyzed", *_METRIC_COLS])


# ---------------------------------------------------------------------------
# Learning / order


def within_cell_repetition(trial_rows: List[dict]) -> pd.DataFrame:
    """Occurrence order INSIDE each participant's condition x level cell
    (repetition 1..3 by that participant's own trial_index order), so
    participants with different randomised schedules still align by
    repetition. Distinct from session position below - do not mix."""
    rows = []
    cells = sorted({(t["participant"], t["condition"], t["level"]) for t in trial_rows})
    for participant, condition, level in cells:
        cell = sorted((t for t in trial_rows if t["participant"] == participant
                       and t["condition"] == condition and t["level"] == level),
                      key=lambda t: t["trial_index"])
        for rep, t in enumerate(cell, start=1):
            if rep > 3:
                break
            rows.append({
                "participant": participant, "condition": condition, "level": level,
                "repetition": rep,
                "fa_main": t["fa_main"] if t.get("analyzed") else np.nan,
                "rt_correct_key_s": t["rt_correct_key_s"] if t["rt_correct_key_s"] is not None else np.nan,
            })
    return pd.DataFrame(rows, columns=["participant", "condition", "level", "repetition",
                                       "fa_main", "rt_correct_key_s"])


def participant_repetition_metrics(trial_rows: List[dict]) -> pd.DataFrame:
    """Repetition curves aggregated to the participant level: per
    (participant, condition, repetition), the mean over that
    participant's levels. Read directly for one participant's curve, or
    feed to group_analysis.group_center for the group curve."""
    # A supplies no target-finger cue, so its hidden-target agreement and
    # lower-choice-complexity RT are not placed on a performance trajectory
    # with the two guidance modalities.
    rep = within_cell_repetition(
        [t for t in trial_rows if t.get("condition") in GUIDANCE_CONDITIONS])
    if rep.empty:
        return pd.DataFrame(columns=["participant", "condition", "repetition",
                                     "fa_main", "rt_correct_key_s"])
    out = (rep.groupby(["participant", "condition", "repetition"], sort=True)
              [["fa_main", "rt_correct_key_s"]].mean().reset_index())
    return out


def session_position_metrics(trial_rows: List[dict]) -> pd.DataFrame:
    """Condition/difficulty-adjusted B/C progression by actual position.

    A raw mean at one session position mixes whichever conditions and
    levels happened to be scheduled there. It is therefore not a
    learning/fatigue trajectory - and that holds within a single
    participant, whose 27 positions each carry exactly one randomly
    assigned condition x level cell. This function instead keeps only the
    performance-comparable guidance conditions B/C and, within each
    participant, removes that participant's condition x level cell mean from
    each trial before adding back the participant's B/C grand mean:

        adjusted = raw - mean(participant, condition, level)
                       + mean(participant, B/C trials).

    The resulting curve retains the participant's scale and within-cell
    temporal departures while neutralising the changing mix of modality and
    difficulty over positions. Raw and adjusted columns are both exported so
    the transformation is auditable; only adjusted columns should be plotted
    as session progression.
    """
    cols = [
        "participant", "position", "condition", "level",
        "fa_main_raw", "fa_main_adjusted",
        "rt_correct_key_s_raw", "rt_correct_key_s_adjusted",
    ]
    eligible = [t for t in trial_rows if t.get("condition") in GUIDANCE_CONDITIONS]
    if not eligible:
        return pd.DataFrame(columns=cols)

    rows = []
    for participant in sorted({str(t["participant"]) for t in eligible}):
        trials = sorted(
            (t for t in eligible if str(t["participant"]) == participant),
            key=lambda t: t["trial_index"],
        )
        records = []
        for t in trials:
            records.append({
                "participant": participant,
                "position": t["trial_index"],
                "condition": t["condition"],
                "level": t["level"],
                "fa_main_raw": (t.get("fa_main") if t.get("analyzed") else np.nan),
                "rt_correct_key_s_raw": (
                    t.get("rt_correct_key_s")
                    if t.get("rt_correct_key_s") is not None else np.nan),
            })

        for raw_col, adjusted_col in (
                ("fa_main_raw", "fa_main_adjusted"),
                ("rt_correct_key_s_raw", "rt_correct_key_s_adjusted")):
            valid = [r for r in records if pd.notna(r[raw_col])]
            grand = float(np.mean([r[raw_col] for r in valid])) if valid else np.nan
            cell_means = {}
            for key in {(r["condition"], r["level"]) for r in valid}:
                values = [r[raw_col] for r in valid
                          if (r["condition"], r["level"]) == key]
                cell_means[key] = float(np.mean(values))
            for r in records:
                key = (r["condition"], r["level"])
                r[adjusted_col] = (
                    float(r[raw_col]) - cell_means[key] + grand
                    if pd.notna(r[raw_col]) and key in cell_means else np.nan)
        rows.extend(records)
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Event outcome composition as proportions


def participant_outcome_proportions(event_rows: List[dict]) -> pd.DataFrame:
    """Per (participant, condition, category): count and proportion of
    that participant's VALID events in the condition. Categories are the
    mutually exclusive set above and sum to 1 within each participant x
    condition; the denominator is that participant's valid events there.
    Conditions a participant never ran get no rows."""
    rows = []
    events = valid_events(event_rows)
    keys = sorted({(e["participant"], e["condition"]) for e in events})
    for participant, condition in keys:
        evs = [e for e in events
               if e["participant"] == participant and e["condition"] == condition]
        n = len(evs)
        counts = {cat: 0 for cat in CATEGORIES}
        for e in evs:
            counts[classify_event_outcome(e)] += 1
        for cat in CATEGORIES:
            rows.append({"participant": participant, "condition": condition,
                         "category": cat, "count": counts[cat], "n_events": n,
                         "proportion": counts[cat] / n if n else np.nan})
    return pd.DataFrame(rows, columns=["participant", "condition", "category",
                                       "count", "n_events", "proportion"])


# ---------------------------------------------------------------------------
# Per-finger cells (homologous L/R merge, counts kept visible)


def per_finger_metrics(event_rows: List[dict]) -> pd.DataFrame:
    """Per (participant, condition, finger_id 1..5) over valid responded
    events whose target finger is known, left/right merged by homologous
    ID but with n_left / n_right kept so the merge is auditable.

    Definitions mirror the Fingers tab's L5..R5 bars: fa is key-correct
    AND finger-correct over the events with a finger verdict (n_judged);
    rt_s averages responded events with an RT.

    rt_complete_s is the same average restricted to key-AND-finger
    correct events (n_rt_complete of them) - the "correct complete
    action" reaction time the Method's finger-specific ANOVA is defined
    on. It is a separate column rather than a replacement, so the
    unsplit Fingers bars keep their existing all-responded definition
    and the two can be compared as a sensitivity check."""
    rows = []
    events = [e for e in valid_events(event_rows)
              if not e["timed_out"] and e.get("target_finger") in FINGER_ORDER]
    keys = sorted({(e["participant"], e["condition"]) for e in events})
    for participant, condition in keys:
        evs = [e for e in events
               if e["participant"] == participant and e["condition"] == condition]
        for fid in FINGER_IDS:
            fe = [e for e in evs if int(e["target_finger"][1]) == fid]
            judged = [e for e in fe if e["finger_correct"] is not None]
            rts = [e["rt_s"] for e in fe if e["rt_s"] is not None]
            complete_rts = [e["rt_s"] for e in fe if e["rt_s"] is not None
                            and e["key_correct"] and e["finger_correct"]]
            rows.append({
                "participant": participant, "condition": condition, "finger_id": fid,
                "n": len(fe),
                "n_left": sum(1 for e in fe if e["target_finger"][0] == "L"),
                "n_right": sum(1 for e in fe if e["target_finger"][0] == "R"),
                "n_judged": len(judged),
                "fa": (sum(1 for e in judged if e["key_correct"] and e["finger_correct"])
                       / len(judged)) if judged else np.nan,
                "rt_s": float(np.mean(rts)) if rts else np.nan,
                "n_rt_complete": len(complete_rts),
                "rt_complete_s": float(np.mean(complete_rts)) if complete_rts else np.nan,
            })
    return pd.DataFrame(rows, columns=["participant", "condition", "finger_id", "n",
                                       "n_left", "n_right", "n_judged", "fa", "rt_s",
                                       "n_rt_complete", "rt_complete_s"])


# ---------------------------------------------------------------------------
# Data quality / audit (never an outcome)


def quality_summary(trial_rows: List[dict], event_rows: List[dict]) -> pd.DataFrame:
    """Per-participant audit counters straight from the exported fields."""
    rows = []
    for participant in sorted({t["participant"] for t in trial_rows}):
        ts = [t for t in trial_rows if t["participant"] == participant]
        evs = [e for e in event_rows if e["participant"] == participant]
        analyzed = [t for t in ts if t.get("analyzed")]
        rows.append({
            "participant": participant,
            "n_trials": len(ts),
            "n_analyzed": len(analyzed),
            "n_events": len(evs),
            "n_valid_events": len(valid_events(evs)),
            "excluded_carryover": sum(t["excluded_carryover"] or 0 for t in ts),
            "suspected_carryover": sum(t["suspected_carryover"] or 0 for t in ts),
            "manual_corrections": sum(t["manual_corrections"] or 0 for t in ts),
            "unresolved_rate": _nanmean([t["unresolved_rate"] for t in analyzed]),
            "ambiguous_rate": _nanmean([t["ambiguous_rate"] for t in analyzed]),
            "borderline_events": sum(t["borderline_events"] or 0 for t in analyzed),
            "qc_extra_presses": sum(t["qc_extra_presses"] or 0 for t in ts),
            "sync_methods": ", ".join(sorted({t["sync_method"] for t in ts})),
        })
    return pd.DataFrame(rows, columns=[
        "participant", "n_trials", "n_analyzed", "n_events", "n_valid_events",
        "excluded_carryover", "suspected_carryover", "manual_corrections",
        "unresolved_rate", "ambiguous_rate", "borderline_events",
        "qc_extra_presses", "sync_methods"])


def threshold_sensitivity(trial_rows: List[dict],
                          event_rows: Optional[List[dict]] = None) -> pd.DataFrame:
    """Automatic probability-threshold sensitivity for guidance B/C.

    Every theta, including 0.40, is recomputed from the same unedited
    event-level target_finger_probability values. The final reviewed
    ``fa_main`` verdict is deliberately not inserted into this curve: it is
    a different measurement stream and belongs in a separate reference
    table. Per-trial proportions are averaged within participant/condition,
    matching the main cross-trial aggregation hierarchy.
    """
    cols = ["participant", "condition", "theta", "fa", "n_trials", "n_events",
            "source"]
    if not trial_rows or not event_rows:
        return pd.DataFrame(columns=cols)

    analyzed_trials = {
        (str(t["participant"]), int(t["trial_index"]))
        for t in trial_rows
        if t.get("analyzed") and t.get("condition") in GUIDANCE_CONDITIONS
    }
    eligible = [
        e for e in valid_events(event_rows)
        if (str(e["participant"]), int(e["trial_index"])) in analyzed_trials
        and e.get("condition") in GUIDANCE_CONDITIONS
    ]
    if not eligible:
        return pd.DataFrame(columns=cols)

    rows = []
    keys = sorted({(str(e["participant"]), e["condition"]) for e in eligible})
    for participant, condition in keys:
        condition_events = [
            e for e in eligible
            if str(e["participant"]) == participant and e["condition"] == condition
        ]
        trial_ids = sorted({int(e["trial_index"]) for e in condition_events})
        for theta in THRESHOLD_SENSITIVITY_VALUES:
            trial_values = []
            n_events = 0
            for trial_id in trial_ids:
                events = [e for e in condition_events if int(e["trial_index"]) == trial_id]
                if not events:
                    continue
                correct = sum(
                    1 for e in events
                    if bool(e.get("key_correct"))
                    and e.get("target_finger_probability") is not None
                    and float(e["target_finger_probability"]) >= theta
                )
                trial_values.append(correct / len(events))
                n_events += len(events)
            rows.append({
                "participant": participant,
                "condition": condition,
                "theta": theta,
                "fa": float(np.mean(trial_values)) if trial_values else np.nan,
                "n_trials": len(trial_values),
                "n_events": n_events,
                "source": "automatic_target_probability",
            })
    return pd.DataFrame(rows, columns=cols)
