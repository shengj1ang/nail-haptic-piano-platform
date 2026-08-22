"""Free-fingering strategy measures for Condition A (key-only practice).

Condition A reveals the target key but withholds the generated target-finger
cue.  The exported event rows nevertheless retain both the final detected
``actual_finger`` and the hidden ``target_finger``.  That makes it possible to
describe what participants choose when no digit is prescribed and to compare
their realised movement pattern with the generated fingering on the same key
sequence.

The movement comparisons are deliberately called motor-demand *proxies*:
same-hand key travel, same-hand finger-transition distance and hand-switch
rate.  They are auditable properties of the observed action sequence, not a
direct measurement of muscular force or subjective effort.
"""

from math import exp, log
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats as sstats


FINGERS = [f"{hand}{digit}" for hand in ("L", "R") for digit in range(1, 6)]
# Keyboard-space display order: left little finger towards the two thumbs,
# then outwards to the right little finger.  Keep FINGERS above as the
# canonical identity/validation list; this order is purely presentational.
SPATIAL_FINGER_ORDER = [
    "L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5",
]
LEVELS = ("alpha", "beta", "gamma")
FINGER_NAMES = {
    1: "thumb",
    2: "index",
    3: "middle",
    4: "ring",
    5: "little",
}
INVALID_CARRYOVER = "invalid_carryover"

TRIAL_COLUMNS = [
    "participant",
    "trial_index",
    "level",
    "a_occurrence",
    "level_occurrence",
    "n_target_events",
    "n_resolved_responses",
    "selection_coverage_pct",
    "dominant_finger",
    "dominant_share_pct",
    "unique_fingers",
    "effective_fingers",
    "finger_switch_rate_pct",
    "median_correct_key_rt_ms",
    "key_accuracy_pct",
    "n_effort_events",
    "actual_key_travel_semitones",
    "reference_key_travel_semitones",
    "actual_finger_transition_steps",
    "reference_finger_transition_steps",
    "actual_hand_switch_pct",
    "reference_hand_switch_pct",
]

OCCURRENCE_METRICS = [
    "dominant_share_pct",
    "unique_fingers",
    "effective_fingers",
    "finger_switch_rate_pct",
    "median_correct_key_rt_ms",
    "key_accuracy_pct",
]

EFFORT_METRICS: Dict[str, Tuple[str, str, str]] = {
    "same_hand_key_travel": (
        "reference_key_travel_semitones",
        "actual_key_travel_semitones",
        "semitones",
    ),
    "finger_transition_distance": (
        "reference_finger_transition_steps",
        "actual_finger_transition_steps",
        "finger steps",
    ),
    "hand_switch_rate": (
        "reference_hand_switch_pct",
        "actual_hand_switch_pct",
        "%",
    ),
}


def _is_finger(value) -> bool:
    return isinstance(value, str) and value in FINGERS


def _finite(value) -> bool:
    if value is None:
        return False
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _valid_condition_a_events(event_rows: List[dict]) -> List[dict]:
    """Condition-A events after the shared carry-over exclusion only."""
    return [
        e for e in event_rows
        if e.get("condition") == "A"
        and e.get("validity") != INVALID_CARRYOVER
    ]


def _effective_count(counts: Iterable[int]) -> float:
    """exp(Shannon entropy): 1 for one finger, K for K equal fingers."""
    values = np.asarray(list(counts), dtype=float)
    total = float(values.sum())
    if total <= 0:
        return np.nan
    probabilities = values[values > 0] / total
    return float(exp(-sum(float(p) * log(float(p)) for p in probabilities)))


def _motor_proxies(actions: Sequence[Tuple[str, int, float]]) -> Tuple[float, float, float]:
    """Mean same-hand key travel, finger distance and adjacent hand switches.

    Same-hand transitions follow the generator's definition: consecutive
    actions of each hand, even if the other hand acted between them.
    """
    key_travel: List[float] = []
    finger_distance: List[float] = []
    for hand in ("L", "R"):
        hand_actions = [action for action in actions if action[0] == hand]
        for previous, current in zip(hand_actions, hand_actions[1:]):
            key_travel.append(abs(current[2] - previous[2]))
            finger_distance.append(abs(current[1] - previous[1]))
    hand_switches = [
        previous[0] != current[0]
        for previous, current in zip(actions, actions[1:])
    ]
    return (
        float(np.mean(key_travel)) if key_travel else np.nan,
        float(np.mean(finger_distance)) if finger_distance else np.nan,
        float(np.mean(hand_switches) * 100) if hand_switches else np.nan,
    )


def condition_a_trial_strategy(event_rows: List[dict]) -> pd.DataFrame:
    """One row per Condition-A trial with choice, performance and effort.

    Finger-choice metrics use every valid, responded event with a resolved
    actual finger.  The actual-versus-prescribed movement comparison is
    stricter: it uses correct-key events with both finger labels resolved, so
    both paths contain the identical note sequence and differ only in the
    chosen hand/finger assignment.
    """
    events = _valid_condition_a_events(event_rows)
    if not events:
        return pd.DataFrame(columns=TRIAL_COLUMNS)

    trial_keys = sorted({
        (str(e.get("participant")), int(e.get("trial_index")))
        for e in events
        if e.get("participant") is not None and e.get("trial_index") is not None
    })
    overall_occurrence: Dict[Tuple[str, int], int] = {}
    level_occurrence: Dict[Tuple[str, int], int] = {}
    by_participant: Dict[str, List[int]] = {}
    for participant, trial_index in trial_keys:
        by_participant.setdefault(participant, []).append(trial_index)
    for participant, indices in by_participant.items():
        level_counts: Dict[str, int] = {}
        for occurrence, trial_index in enumerate(sorted(indices), start=1):
            key = (participant, trial_index)
            trial_events = [
                e for e in events
                if str(e.get("participant")) == participant
                and int(e.get("trial_index")) == trial_index
            ]
            level = str(trial_events[0].get("level"))
            overall_occurrence[key] = occurrence
            level_counts[level] = level_counts.get(level, 0) + 1
            level_occurrence[key] = level_counts[level]

    rows = []
    for participant, trial_index in trial_keys:
        trial = sorted(
            (e for e in events
             if str(e.get("participant")) == participant
             and int(e.get("trial_index")) == trial_index),
            key=lambda e: int(e.get("event_index", 0)),
        )
        selection = [
            e for e in trial
            if not bool(e.get("timed_out")) and _is_finger(e.get("actual_finger"))
        ]
        counts = {finger: 0 for finger in FINGERS}
        for event in selection:
            counts[str(event["actual_finger"])] += 1
        dominant_finger = max(FINGERS, key=lambda finger: (counts[finger], -FINGERS.index(finger)))
        dominant_n = counts[dominant_finger]
        n_selection = len(selection)
        finger_sequence = [str(e["actual_finger"]) for e in selection]
        switches = [
            previous != current
            for previous, current in zip(finger_sequence, finger_sequence[1:])
        ]

        correct_rt = [
            float(e["rt_s"]) * 1000
            for e in trial
            if bool(e.get("key_correct")) and _finite(e.get("rt_s"))
        ]
        effort_events = [
            e for e in selection
            if bool(e.get("key_correct"))
            and _is_finger(e.get("target_finger"))
            and _finite(e.get("target_note"))
        ]
        actual_actions = [
            (str(e["actual_finger"])[0], int(str(e["actual_finger"])[1]),
             float(e["target_note"]))
            for e in effort_events
        ]
        prescribed_actions = [
            (str(e["target_finger"])[0], int(str(e["target_finger"])[1]),
             float(e["target_note"]))
            for e in effort_events
        ]
        actual_key, actual_finger, actual_hand = _motor_proxies(actual_actions)
        target_key, target_finger, target_hand = _motor_proxies(prescribed_actions)
        n_target = len(trial)

        rows.append({
            "participant": participant,
            "trial_index": trial_index,
            "level": str(trial[0].get("level")),
            "a_occurrence": overall_occurrence[(participant, trial_index)],
            "level_occurrence": level_occurrence[(participant, trial_index)],
            "n_target_events": n_target,
            "n_resolved_responses": n_selection,
            "selection_coverage_pct": 100 * n_selection / n_target if n_target else np.nan,
            "dominant_finger": dominant_finger if dominant_n else None,
            "dominant_share_pct": 100 * dominant_n / n_selection if n_selection else np.nan,
            "unique_fingers": sum(count > 0 for count in counts.values()),
            "effective_fingers": _effective_count(counts.values()),
            "finger_switch_rate_pct": float(np.mean(switches) * 100) if switches else np.nan,
            "median_correct_key_rt_ms": float(np.median(correct_rt)) if correct_rt else np.nan,
            "key_accuracy_pct": (
                100 * sum(bool(e.get("key_correct")) for e in trial) / n_target
                if n_target else np.nan
            ),
            "n_effort_events": len(effort_events),
            "actual_key_travel_semitones": actual_key,
            "reference_key_travel_semitones": target_key,
            "actual_finger_transition_steps": actual_finger,
            "reference_finger_transition_steps": target_finger,
            "actual_hand_switch_pct": actual_hand,
            "reference_hand_switch_pct": target_hand,
        })
    return pd.DataFrame(rows, columns=TRIAL_COLUMNS).sort_values(
        ["participant", "a_occurrence"], ignore_index=True)


def condition_a_finger_usage(event_rows: List[dict]) -> pd.DataFrame:
    """Zero-filled participant × finger counts and within-participant shares.

    Each row also carries what the hidden generated fingering would have
    required over *exactly the same events* (``reference_*``).  Sharing the
    denominator is what makes observed − reference a like-for-like
    difference rather than two differently filtered tallies; the generated
    fingering stays the same-sequence experimental reference, never an
    optimum the free choice should have matched.
    """
    events = _valid_condition_a_events(event_rows)
    participants = sorted({str(e.get("participant")) for e in events})
    rows = []
    for participant in participants:
        resolved = [
            e for e in events
            if str(e.get("participant")) == participant
            and not bool(e.get("timed_out"))
            and _is_finger(e.get("actual_finger"))
        ]
        total = len(resolved)
        for finger in FINGERS:
            count = sum(e.get("actual_finger") == finger for e in resolved)
            reference = sum(e.get("target_finger") == finger for e in resolved)
            rows.append({
                "participant": participant,
                "finger": finger,
                "hand": finger[0],
                "finger_id": int(finger[1]),
                "finger_name": FINGER_NAMES[int(finger[1])],
                "n": count,
                "share_pct": 100 * count / total if total else np.nan,
                "reference_n": reference,
                "reference_share_pct": 100 * reference / total if total else np.nan,
            })
    return pd.DataFrame(rows, columns=[
        "participant", "finger", "hand", "finger_id", "finger_name",
        "n", "share_pct", "reference_n", "reference_share_pct",
    ])


def participant_hand_usage(
        finger_usage: pd.DataFrame,
        handedness: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Participant × hand counts/shares obtained by summing the five digits.

    ``handedness`` is the self-reported metadata from TrialStructure.json
    (app.group_analysis.participant_handedness).  It travels with the table
    so the exported CSV records which participants the figure marks; an
    unknown participant simply keeps an empty string.  Observed hand use and
    reported handedness stay separate columns - neither is derived from the
    other.

    ``reference_share_pct`` is the same sum over the hidden generated
    fingering, and ``difference_pct`` is observed − reference: a negative
    left-hand value means the free choice moved work to the right hand
    relative to the generated fingering for the same key sequences.
    """
    columns = ["participant", "handedness", "hand", "n", "share_pct",
               "reference_n", "reference_share_pct", "difference_pct"]
    reported = handedness or {}
    if finger_usage.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for participant in sorted(finger_usage["participant"].unique()):
        participant_rows = finger_usage[finger_usage["participant"] == participant]
        for hand in ("L", "R"):
            hand_rows = participant_rows[participant_rows["hand"] == hand]
            observed = float(hand_rows["share_pct"].sum())
            reference = (float(hand_rows["reference_share_pct"].sum())
                         if "reference_share_pct" in hand_rows else np.nan)
            rows.append({
                "participant": participant,
                "handedness": reported.get(participant, ""),
                "hand": hand,
                "n": int(hand_rows["n"].sum()),
                "share_pct": observed,
                "reference_n": (int(hand_rows["reference_n"].sum())
                                if "reference_n" in hand_rows else 0),
                "reference_share_pct": reference,
                "difference_pct": observed - reference,
            })
    return pd.DataFrame(rows, columns=columns)


def participant_strategy_summary(
        trials: pd.DataFrame, usage: pd.DataFrame) -> pd.DataFrame:
    """One row per participant, including their whole-condition dominant digit."""
    columns = [
        "participant", "n_trials", "n_resolved_responses", "dominant_finger",
        "dominant_share_pct", "effective_fingers", "mean_finger_switch_rate_pct",
        "mean_median_correct_key_rt_ms", "mean_key_accuracy_pct",
    ]
    if trials.empty or usage.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for participant, participant_trials in trials.groupby("participant"):
        participant_usage = usage[usage["participant"] == participant].set_index("finger")
        counts = participant_usage.reindex(FINGERS)["n"].fillna(0).to_numpy(dtype=float)
        shares = participant_usage.reindex(FINGERS)["share_pct"].to_numpy(dtype=float)
        dominant_index = int(np.nanargmax(shares))
        rows.append({
            "participant": participant,
            "n_trials": len(participant_trials),
            "n_resolved_responses": int(np.nansum(counts)),
            "dominant_finger": FINGERS[dominant_index],
            "dominant_share_pct": float(shares[dominant_index]),
            "effective_fingers": _effective_count(counts),
            "mean_finger_switch_rate_pct": float(participant_trials["finger_switch_rate_pct"].mean()),
            "mean_median_correct_key_rt_ms": float(participant_trials["median_correct_key_rt_ms"].mean()),
            "mean_key_accuracy_pct": float(participant_trials["key_accuracy_pct"].mean()),
        })
    return pd.DataFrame(rows, columns=columns).sort_values("participant", ignore_index=True)


def _center(values: pd.Series) -> dict:
    finite = values.dropna().to_numpy(dtype=float)
    n = len(finite)
    mean = float(np.mean(finite)) if n else np.nan
    sd = float(np.std(finite, ddof=1)) if n >= 2 else np.nan
    sem = sd / np.sqrt(n) if n >= 2 else np.nan
    if n >= 2:
        half = float(sstats.t.ppf(0.975, n - 1)) * sem
        lo, hi = mean - half, mean + half
    else:
        lo = hi = np.nan
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "sem": sem,
        "ci95_lo": lo,
        "ci95_hi": hi,
    }


def occurrence_summary(trials: pd.DataFrame) -> pd.DataFrame:
    """Long participant-weighted group summary for occurrences 1–9."""
    columns = [
        "a_occurrence", "metric", "n", "mean", "sd", "sem", "ci95_lo", "ci95_hi",
    ]
    if trials.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for occurrence, group in trials.groupby("a_occurrence"):
        for metric in OCCURRENCE_METRICS:
            rows.append({
                "a_occurrence": int(occurrence),
                "metric": metric,
                **_center(group[metric]),
            })
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["metric", "a_occurrence"], ignore_index=True)


def level_occurrence_summary(trials: pd.DataFrame) -> pd.DataFrame:
    """Group summary for 1st–3rd A trial within each difficulty.

    This is the interpretable learning/strategy view: unlike overall A trial
    order, every line holds generated difficulty fixed and therefore cannot
    mistake a changing alpha/beta/gamma mix for strategy adaptation.
    """
    columns = [
        "level", "level_occurrence", "metric", "n", "mean", "sd", "sem",
        "ci95_lo", "ci95_hi",
    ]
    if trials.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for (level, occurrence), group in trials.groupby(
            ["level", "level_occurrence"]):
        for metric in OCCURRENCE_METRICS:
            rows.append({
                "level": str(level),
                "level_occurrence": int(occurrence),
                "metric": metric,
                **_center(group[metric]),
            })
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["metric", "level", "level_occurrence"], ignore_index=True)


def finger_usage_group_summary(usage: pd.DataFrame) -> pd.DataFrame:
    """Participant-weighted mean and 95% CI for each actual finger share."""
    columns = ["finger", "n", "mean", "sd", "sem", "ci95_lo", "ci95_hi"]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for finger in FINGERS:
        rows.append({
            "finger": finger,
            **_center(usage.loc[usage["finger"] == finger, "share_pct"]),
        })
    return pd.DataFrame(rows, columns=columns)


def participant_motor_effort(trials: pd.DataFrame) -> pd.DataFrame:
    """Participant × difficulty paired observed-versus-generated proxies."""
    columns = [
        "participant", "level", "metric", "unit", "generated_reference", "actual",
        "difference_actual_minus_reference",
    ]
    if trials.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for (participant, level), group in trials.groupby(["participant", "level"]):
        for metric, (reference_col, actual_col, unit) in EFFORT_METRICS.items():
            reference = float(group[reference_col].mean())
            actual = float(group[actual_col].mean())
            rows.append({
                "participant": participant,
                "level": level,
                "metric": metric,
                "unit": unit,
                "generated_reference": reference,
                "actual": actual,
                "difference_actual_minus_reference": actual - reference,
            })
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["metric", "level", "participant"], ignore_index=True)


def motor_effort_group_summary(participant_effort: pd.DataFrame) -> pd.DataFrame:
    """Group means plus a paired 95% CI for each motor-demand proxy."""
    columns = [
        "metric", "level", "unit", "n", "generated_reference_mean", "actual_mean", "difference_mean",
        "difference_sd", "difference_sem", "difference_ci95_lo", "difference_ci95_hi",
    ]
    if participant_effort.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for (metric, level), group in participant_effort.groupby(
            ["metric", "level"], sort=False):
        center = _center(group["difference_actual_minus_reference"])
        rows.append({
            "metric": metric,
            "level": level,
            "unit": str(group["unit"].iloc[0]),
            "n": center["n"],
            "generated_reference_mean": float(group["generated_reference"].mean()),
            "actual_mean": float(group["actual"].mean()),
            "difference_mean": center["mean"],
            "difference_sd": center["sd"],
            "difference_sem": center["sem"],
            "difference_ci95_lo": center["ci95_lo"],
            "difference_ci95_hi": center["ci95_hi"],
        })
    return pd.DataFrame(rows, columns=columns)
