"""Difficulty-aligned within-session progression data.

The ordinary session-position view answers what happened at positions 1--27,
but each position contains a different randomised mix of condition and
difficulty.  This module provides a complementary view requested for the group
analysis: within each participant and difficulty level, sort the nine trials by
their real session position and relabel them occurrence 1--9.

All A/B/C trials stay in this descriptive view.  For each metric we export both
the observed value and a condition-adjusted value:

    adjusted = observed
               - participant x difficulty x condition mean
               + participant x difficulty mean.

The adjustment keeps the difficulty level's scale while preventing a changing
mix of A/B/C at occurrence 1--9 from looking like practice or fatigue.  Raw
values, conditions and original session positions remain in the tidy export so
the transformation is fully inspectable.
"""

from typing import Dict, List

import numpy as np
import pandas as pd

from .group_analysis import LEVELS, LEVEL_SYMBOLS, group_center


METRICS: Dict[str, str] = {
    "rt_correct_key_s_raw": "rt_correct_key_s_adjusted",
    "key_accuracy_raw": "key_accuracy_adjusted",
}

TRIAL_COLUMNS = [
    "participant",
    "level",
    "level_symbol",
    "difficulty_occurrence",
    "session_position",
    "condition",
    "rt_correct_key_s_raw",
    "rt_correct_key_s_adjusted",
    "key_accuracy_raw",
    "key_accuracy_adjusted",
]

SUMMARY_COLUMNS = [
    "level",
    "level_symbol",
    "difficulty_occurrence",
    "metric",
    "n",
    "mean",
    "sd",
    "sem",
    "ci95_lo",
    "ci95_hi",
]


def _number(value) -> float:
    """Return a finite float or NaN without turning missing data into zero."""
    if value is None or pd.isna(value):
        return np.nan
    value = float(value)
    return value if np.isfinite(value) else np.nan


def difficulty_progression_metrics(trial_rows: List[dict]) -> pd.DataFrame:
    """One row per trial on a participant x difficulty occurrence axis.

    ``difficulty_occurrence`` is the trial's order among that participant's
    trials at the same difficulty, based on the original ``trial_index``.  A
    complete schedule therefore has occurrences 1--9 for each of alpha, beta
    and gamma.  No trial is dropped merely because its condition is A.
    """
    if not trial_rows:
        return pd.DataFrame(columns=TRIAL_COLUMNS)

    rows = []
    participants = sorted({str(t["participant"]) for t in trial_rows})
    for participant in participants:
        participant_rows = [
            t for t in trial_rows if str(t["participant"]) == participant
        ]
        for level in LEVELS:
            ordered = sorted(
                (t for t in participant_rows if t.get("level") == level),
                key=lambda t: int(t["trial_index"]),
            )
            for occurrence, trial in enumerate(ordered, start=1):
                rows.append({
                    "participant": participant,
                    "level": level,
                    "level_symbol": LEVEL_SYMBOLS[level],
                    "difficulty_occurrence": occurrence,
                    "session_position": int(trial["trial_index"]),
                    "condition": trial["condition"],
                    "rt_correct_key_s_raw": _number(
                        trial.get("rt_correct_key_s")),
                    "key_accuracy_raw": _number(trial.get("key_accuracy")),
                })

    frame = pd.DataFrame(rows)
    for raw_col, adjusted_col in METRICS.items():
        frame[adjusted_col] = np.nan
        for (_participant, _level), index in frame.groupby(
                ["participant", "level"], sort=False).groups.items():
            cell = frame.loc[index]
            valid = cell.dropna(subset=[raw_col])
            if valid.empty:
                continue
            difficulty_mean = float(valid[raw_col].mean())
            condition_means = valid.groupby("condition")[raw_col].mean()
            for row_index in index:
                raw = frame.at[row_index, raw_col]
                condition = frame.at[row_index, "condition"]
                if pd.notna(raw) and condition in condition_means.index:
                    frame.at[row_index, adjusted_col] = (
                        float(raw) - float(condition_means.loc[condition])
                        + difficulty_mean
                    )

    return frame[TRIAL_COLUMNS]


def difficulty_progression_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Participant-weighted group mean/SD/95% CI for every plotted point."""
    if frame.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    summaries = []
    for metric in [*METRICS.keys(), *METRICS.values()]:
        center = group_center(
            frame, metric, ["level", "difficulty_occurrence"])
        if len(center):
            center["level_symbol"] = center["level"].map(LEVEL_SYMBOLS)
            summaries.append(center.assign(metric=metric))
    if not summaries:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)
    out = pd.concat(summaries, ignore_index=True)
    return out[SUMMARY_COLUMNS].sort_values(
        ["metric", "level", "difficulty_occurrence"], ignore_index=True)
