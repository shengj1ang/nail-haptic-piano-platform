"""GUI-free loading, aggregation and small-sample statistics behind the
Group Analysis (multi-participant) window (app/gui/group_analysis_window.py).

Data source: the reviewed-and-exported per-participant CSVs written by
app.participant_export.export_participant
(data/MainUserStudy/<participant>/<participant>_{trials,events}.csv).
Nothing here re-derives correctness, re-filters reaction times or goes
back to raw video/MIDI - the export schema already carries the final
(hand-verified where corrected) verdicts and the carry-over audit state,
so group numbers can never drift from the single-participant window or
the CSVs. Participants without both CSVs are reported as "not exported",
never silently skipped.

Aggregation hierarchy (within-participant design - events and trials of
one participant are NOT independent observations):

  event rows  -> per-trial outcomes (already done by the export)
  trial rows  -> per-participant cell/condition means (this module)
  participant -> group centre / spread / paired contrasts (this module)

Every group-level mean, interval and inferential test therefore has
n = number of participants. Confirmed invalid_carryover events are
excluded from all outcome statistics (valid_events) and surface only in
the quality audit; unmatched extra presses stay QC-only, exactly as in
the single-participant analysis.
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as sstats

from .participant_analysis import (
    CATEGORIES,
    FINGER_ORDER,
    classify_event_outcome,
)
from .participant_export import export_paths
from .pilot_study import CONDITIONS as _CONDITIONS
from .pilot_study import DATA_DIR as STUDY_DATA_DIR
from .pilot_study import list_participants
from .sequence_generator import LEVEL_SYMBOL, LEVELS as _LEVELS

CONDITIONS: List[str] = list(_CONDITIONS)
LEVELS: List[str] = list(_LEVELS)
LEVEL_SYMBOLS: Dict[str, str] = dict(LEVEL_SYMBOL)

# Within-participant paired contrasts, (minuend, subtrahend):
# B-A visual finger cue vs key-only, C-A haptic vs key-only, C-B haptic vs visual.
CONTRASTS: List[Tuple[str, str]] = [("B", "A"), ("C", "A"), ("C", "B")]

# Homologous finger IDs (1 thumb ... 5 little), left/right merged by ID.
FINGER_IDS = [1, 2, 3, 4, 5]
FINGER_ID_NAMES = {1: "thumb", 2: "index", 3: "middle", 4: "ring", 5: "little"}

# Inferential gating: below MIN_TEST_N complete cases no test is run at
# all (a paired Wilcoxon cannot even reach p < .05 two-sided before
# n = 5); everything below EXPLORATORY_N is labelled exploratory.
MIN_TEST_N = 5
EXPLORATORY_N = 15

VALIDITY_INVALID_CARRYOVER = "invalid_carryover"  # mirrors app.quiz


# ---------------------------------------------------------------------------
# Loading the exported CSVs back into the export's in-memory row schema

# Columns that must stay strings (everything else is generically parsed:
# "" -> None, "True"/"False" -> bool, then int, then float, else str).
_TRIAL_STR_COLS = {
    "participant", "condition", "condition_label", "level", "level_symbol",
    "sequence", "quiz_name", "guidance_type", "sync_method",
}
_EVENT_STR_COLS = {
    "participant", "condition", "level", "sequence", "quiz_name",
    "target_note_name", "target_finger", "target_hand", "actual_finger",
    "validity",
}

# The subset of exported columns the group computations actually read;
# missing ones make a participant "incomplete" rather than crashing later.
REQUIRED_TRIAL_COLS = [
    "participant", "trial_index", "condition", "level", "sequence", "analyzed",
    "note_count", "key_accuracy", "fa_main", "fa_given_key",
    "rt_correct_key_s", "rt_complete_s", "misses",
    "suspected_carryover", "excluded_carryover", "manual_corrections",
    "unresolved_rate", "ambiguous_rate", "borderline_events",
    "qc_extra_presses", "sync_method",
]
REQUIRED_EVENT_COLS = [
    "participant", "trial_index", "condition", "level", "event_index",
    "target_finger", "timed_out", "actual_note", "key_correct",
    "actual_finger", "finger_correct", "rt_s", "validity",
    "manually_corrected",
]


def _parse_value(raw: str):
    if raw == "":
        return None
    if raw == "True":
        return True
    if raw == "False":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _load_csv_rows(path: Path, str_cols: set) -> List[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            {k: (v if k in str_cols else _parse_value(v)) for k, v in raw.items()}
            for raw in reader
        ]


def load_participant_rows(participant: str, data_dir: Path = STUDY_DATA_DIR):
    """(trial_rows, event_rows) from the participant's exported CSVs,
    with the CSV strings converted back to the export's in-memory types
    (bool / int / float / None). Raises FileNotFoundError when either
    CSV is missing - the participant has not been exported yet."""
    trials_path, events_path = export_paths(participant)
    if data_dir != STUDY_DATA_DIR:  # tests point at a temp dir
        trials_path = data_dir / participant / trials_path.name
        events_path = data_dir / participant / events_path.name
    for p in (trials_path, events_path):
        if not p.is_file():
            raise FileNotFoundError(f"{participant}: missing {p.name} (run Participant Export first)")
    trial_rows = _load_csv_rows(trials_path, _TRIAL_STR_COLS)
    event_rows = _load_csv_rows(events_path, _EVENT_STR_COLS)
    return trial_rows, event_rows


@dataclass
class ParticipantStatus:
    """Per-participant analysability shown in the selection list."""

    participant: str
    exported: bool = False
    n_trials: int = 0
    n_events: int = 0
    n_analyzed: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.exported and self.n_trials > 0 and not self.problems

    def summary(self) -> str:
        if not self.exported:
            return "not exported yet (run Participant Export in Quiz Analysis)"
        if self.problems:
            return "; ".join(self.problems)
        note = f"{self.n_trials} trials, {self.n_events} events"
        if self.n_analyzed < self.n_trials:
            note += f", only {self.n_analyzed} analyzed from video"
        return note


def scan_participants(data_dir: Path = STUDY_DATA_DIR) -> List[ParticipantStatus]:
    """Status for every participant folder holding a TrialStructure.json.
    A broken participant becomes a status with problems, never an
    exception - one bad folder must not take the window down."""
    statuses = []
    for participant in list_participants(data_dir):
        status = ParticipantStatus(participant=participant)
        try:
            trial_rows, event_rows = load_participant_rows(participant, data_dir)
        except FileNotFoundError:
            statuses.append(status)
            continue
        except Exception as e:  # unreadable/corrupt CSV
            status.exported = True
            status.problems.append(f"unreadable export: {e}")
            statuses.append(status)
            continue
        status.exported = True
        status.n_trials = len(trial_rows)
        status.n_events = len(event_rows)
        if not trial_rows:
            status.problems.append("trials CSV is empty")
        else:
            missing_t = [c for c in REQUIRED_TRIAL_COLS if c not in trial_rows[0]]
            if missing_t:
                status.problems.append(f"trials CSV missing columns: {', '.join(missing_t)}")
        if not event_rows:
            status.problems.append("events CSV is empty")
        else:
            missing_e = [c for c in REQUIRED_EVENT_COLS if c not in event_rows[0]]
            if missing_e:
                status.problems.append(f"events CSV missing columns: {', '.join(missing_e)}")
        if trial_rows and not status.problems:
            status.n_analyzed = sum(1 for t in trial_rows if t.get("analyzed"))
        statuses.append(status)
    return statuses


@dataclass
class GroupData:
    """Everything one Analyse click works from - rebuilt from disk on
    every call, so a changed selection can never inherit stale rows."""

    included: List[str] = field(default_factory=list)
    trial_rows: List[dict] = field(default_factory=list)
    event_rows: List[dict] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.included)


def load_group(participants: List[str], data_dir: Path = STUDY_DATA_DIR) -> GroupData:
    """Loads exactly the requested participants (order-normalised).
    Failures land in .errors per participant; the rest still load."""
    data = GroupData()
    for participant in sorted(set(participants)):
        try:
            trial_rows, event_rows = load_participant_rows(participant, data_dir)
        except Exception as e:
            data.errors[participant] = str(e)
            continue
        if not trial_rows:
            data.errors[participant] = "trials CSV is empty"
            continue
        data.included.append(participant)
        data.trial_rows.extend(trial_rows)
        data.event_rows.extend(event_rows)
    return data


# ---------------------------------------------------------------------------
# Event validity

def valid_events(event_rows: List[dict]) -> List[dict]:
    """Outcome-eligible events: everything except manually confirmed
    carry-over. This is the only filter - unresolved fingers and
    timeouts stay in (they are real outcomes under the main FA
    definition)."""
    return [e for e in event_rows if e.get("validity") != VALIDITY_INVALID_CARRYOVER]


# ---------------------------------------------------------------------------
# Level 1: participant-level aggregation of trial rows

def _mean(values) -> float:
    vals = [v for v in values if v is not None]
    return float(np.mean(vals)) if vals else np.nan


_METRIC_COLS = ["key_accuracy", "fa_main", "fa_given_key", "rt_correct_key_s", "rt_complete_s"]


def _trial_metric_row(trials: List[dict]) -> dict:
    """The shared metric definitions, identical to the single-participant
    Overview tab: FA metrics average analyzed trials only; RT metrics
    average trials with a valid (non-None) RT; key accuracy averages all
    trials. Missing data yields NaN, never 0."""
    analyzed = [t for t in trials if t.get("analyzed")]
    return {
        "n_trials": len(trials),
        "n_analyzed": len(analyzed),
        "key_accuracy": _mean([t["key_accuracy"] for t in trials]),
        "fa_main": _mean([t["fa_main"] for t in analyzed]),
        "fa_given_key": _mean([t["fa_given_key"] for t in analyzed]),
        "rt_correct_key_s": _mean([t["rt_correct_key_s"] for t in trials]),
        "rt_complete_s": _mean([t["rt_complete_s"] for t in trials]),
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
# Level 2: group centre over participant-level values

def group_center(df: pd.DataFrame, value_col: str, group_cols: List[str]) -> pd.DataFrame:
    """Mean / SD / 95% t-CI of participant-level values per group cell.
    n counts PARTICIPANT values (the independent unit), NaNs dropped;
    the interval is NaN when n < 2 - never fabricated."""
    out_cols = group_cols + ["n", "mean", "sd", "sem", "ci95_lo", "ci95_hi"]
    if df.empty:
        return pd.DataFrame(columns=out_cols)
    rows = []
    for key, sub in df.groupby(group_cols, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        vals = sub[value_col].dropna().to_numpy(dtype=float)
        n = len(vals)
        mean = float(np.mean(vals)) if n else np.nan
        sd = float(np.std(vals, ddof=1)) if n >= 2 else np.nan
        sem = sd / np.sqrt(n) if n >= 2 else np.nan
        if n >= 2:
            half = float(sstats.t.ppf(0.975, n - 1)) * sem
            lo, hi = mean - half, mean + half
        else:
            lo = hi = np.nan
        rows.append(dict(zip(group_cols, key), n=n, mean=mean, sd=sd, sem=sem,
                         ci95_lo=lo, ci95_hi=hi))
    return pd.DataFrame(rows, columns=out_cols)


def condition_pivot(pc_df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """participant x condition table of one metric (NaN where missing) -
    the shape paired contrasts and repeated-measures tests work on."""
    if pc_df.empty:
        return pd.DataFrame(columns=CONDITIONS)
    pivot = pc_df.pivot(index="participant", columns="condition", values=metric)
    return pivot.reindex(columns=CONDITIONS)


def paired_differences(pc_df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Within-participant contrast values, one row per (participant,
    contrast): B-A, C-A, C-B. Participants missing either side of a
    contrast contribute no row (pairing is never broken or imputed)."""
    pivot = condition_pivot(pc_df, metric)
    rows = []
    for a, b in CONTRASTS:  # a - b
        label = f"{a}−{b}"
        for participant, row in pivot.iterrows():
            if pd.notna(row.get(a)) and pd.notna(row.get(b)):
                rows.append({"participant": participant, "contrast": label,
                             "diff": float(row[a] - row[b])})
    return pd.DataFrame(rows, columns=["participant", "contrast", "diff"])


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
    participant's levels. Feed to group_center for the group curve."""
    rep = within_cell_repetition(trial_rows)
    if rep.empty:
        return pd.DataFrame(columns=["participant", "condition", "repetition",
                                     "fa_main", "rt_correct_key_s"])
    out = (rep.groupby(["participant", "condition", "repetition"], sort=True)
              [["fa_main", "rt_correct_key_s"]].mean().reset_index())
    return out


def session_position_metrics(trial_rows: List[dict]) -> pd.DataFrame:
    """Whole-session progression by ACTUAL presentation position
    (trial_index 1..27). Condition and level at a given position differ
    across participants, so this is session progression / fatigue only -
    never a condition comparison."""
    rows = []
    for t in sorted(trial_rows, key=lambda t: (t["participant"], t["trial_index"])):
        rows.append({
            "participant": t["participant"],
            "position": t["trial_index"],
            "condition": t["condition"],
            "fa_main": t["fa_main"] if t.get("analyzed") else np.nan,
            "rt_correct_key_s": t["rt_correct_key_s"] if t["rt_correct_key_s"] is not None else np.nan,
        })
    return pd.DataFrame(rows, columns=["participant", "position", "condition",
                                       "fa_main", "rt_correct_key_s"])


# ---------------------------------------------------------------------------
# Event outcome composition (participant-level proportions + pooled counts)

def participant_outcome_proportions(event_rows: List[dict]) -> pd.DataFrame:
    """Per (participant, condition, category): count and proportion of
    that participant's VALID events in the condition. Categories are the
    existing mutually exclusive set (app.participant_analysis.CATEGORIES)
    and sum to 1 within each participant x condition; the denominator is
    that participant's valid events there. Conditions a participant
    never ran get no rows."""
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
# Per-finger (homologous L/R merge, counts kept visible)

def per_finger_metrics(event_rows: List[dict]) -> pd.DataFrame:
    """Per (participant, condition, finger_id 1..5) over valid responded
    events whose target finger is known, left/right merged by homologous
    ID but with n_left / n_right kept so the merge is auditable.

    Definitions mirror the single-participant Fingers tab: fa is
    key-correct AND finger-correct over the events with a finger verdict
    (n_judged); rt_s averages responded events with an RT."""
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
            rows.append({
                "participant": participant, "condition": condition, "finger_id": fid,
                "n": len(fe),
                "n_left": sum(1 for e in fe if e["target_finger"][0] == "L"),
                "n_right": sum(1 for e in fe if e["target_finger"][0] == "R"),
                "n_judged": len(judged),
                "fa": (sum(1 for e in judged if e["key_correct"] and e["finger_correct"])
                       / len(judged)) if judged else np.nan,
                "rt_s": float(np.mean(rts)) if rts else np.nan,
            })
    return pd.DataFrame(rows, columns=["participant", "condition", "finger_id", "n",
                                       "n_left", "n_right", "n_judged", "fa", "rt_s"])


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
            "unresolved_rate": _mean([t["unresolved_rate"] for t in analyzed]),
            "ambiguous_rate": _mean([t["ambiguous_rate"] for t in analyzed]),
            "borderline_events": sum(t["borderline_events"] or 0 for t in analyzed),
            "qc_extra_presses": sum(t["qc_extra_presses"] or 0 for t in ts),
            "sync_methods": ", ".join(sorted({t["sync_method"] for t in ts})),
        })
    return pd.DataFrame(rows, columns=[
        "participant", "n_trials", "n_analyzed", "n_events", "n_valid_events",
        "excluded_carryover", "suspected_carryover", "manual_corrections",
        "unresolved_rate", "ambiguous_rate", "borderline_events",
        "qc_extra_presses", "sync_methods"])


def threshold_sensitivity(trial_rows: List[dict]) -> pd.DataFrame:
    """Per (participant, condition, theta): mean FA over analyzed trials
    under each exported alternative detection threshold (fa_theta_*
    columns), with fa_main slotted in as the θ=0.40 main analysis.
    Empty when the export carries no fa_theta_ columns."""
    cols = ["participant", "condition", "theta", "fa"]
    if not trial_rows:
        return pd.DataFrame(columns=cols)
    theta_cols = sorted(k for k in trial_rows[0] if k.startswith("fa_theta_"))
    if not theta_cols:
        return pd.DataFrame(columns=cols)
    rows = []
    keys = sorted({(t["participant"], t["condition"]) for t in trial_rows})
    for participant, condition in keys:
        analyzed = [t for t in trial_rows if t["participant"] == participant
                    and t["condition"] == condition and t.get("analyzed")]
        values = {col.replace("fa_theta_", ""): _mean([t.get(col) for t in analyzed])
                  for col in theta_cols}
        values["0.40"] = _mean([t["fa_main"] for t in analyzed])
        for theta in sorted(values):
            rows.append({"participant": participant, "condition": condition,
                         "theta": theta, "fa": values[theta]})
    return pd.DataFrame(rows, columns=cols)


def cell_availability(trial_rows: List[dict]) -> pd.DataFrame:
    """Per (condition, level): how many of the included participants have
    at least one trial, and at least one video-analyzed trial, there."""
    rows = []
    for condition in CONDITIONS:
        for level in LEVELS:
            cell = [t for t in trial_rows
                    if t["condition"] == condition and t["level"] == level]
            rows.append({
                "condition": condition, "level": level,
                "n_participants": len({t["participant"] for t in cell}),
                "n_analyzed_participants": len({t["participant"] for t in cell
                                                if t.get("analyzed")}),
            })
    return pd.DataFrame(rows, columns=["condition", "level", "n_participants",
                                       "n_analyzed_participants"])


# ---------------------------------------------------------------------------
# Within-subject inference (exploratory; gated by complete-case N)

def _holm(pvalues: List[float]) -> List[float]:
    """Holm step-down adjusted p-values, input order preserved."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvalues[i])
        adjusted[i] = min(1.0, running)
    return adjusted


def _rank_biserial(diffs: np.ndarray) -> float:
    """Matched-pairs rank-biserial correlation from the signed diffs
    (zeros dropped, matching Wilcoxon's default zero handling).
    +1 means every pair moved in the positive direction."""
    d = diffs[diffs != 0]
    ranks = sstats.rankdata(np.abs(d))
    w_plus = float(ranks[d > 0].sum())
    w_minus = float(ranks[d < 0].sum())
    total = w_plus + w_minus
    return (w_plus - w_minus) / total if total else np.nan


def condition_inference(pc_df: pd.DataFrame, metric: str) -> dict:
    """Repeated-measures comparison of the three conditions on one
    participant-level metric.

    Overall: Friedman test over complete cases (all three conditions
    present). Pairwise: paired Wilcoxon signed-rank per contrast with
    Holm correction across the three contrasts. No test runs below
    MIN_TEST_N complete cases - the returned dict then carries only the
    reason string. Independent-samples tests are never used here."""
    pivot = condition_pivot(pc_df, metric)
    complete = pivot.dropna()
    n_total = len(pivot)
    n = len(complete)
    result = {
        "metric": metric,
        "n_participants": n_total,
        "n_complete": n,
        "n_missing_pairs": n_total - n,
        "exploratory": n < EXPLORATORY_N,
        "friedman": None,
        "pairwise": [],
        "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with all three conditions "
            f"(have {n}) — descriptive results only"
        )
        return result

    a, b, c = (complete[cond].to_numpy(dtype=float) for cond in CONDITIONS)
    try:
        stat, p = sstats.friedmanchisquare(a, b, c)
        kendalls_w = float(stat) / (n * (len(CONDITIONS) - 1))
        result["friedman"] = {
            "test": "Friedman test (repeated measures)",
            "n": n, "statistic": float(stat), "p": float(p),
            "effect_size": kendalls_w, "effect_name": "Kendall's W",
        }
    except ValueError as e:  # e.g. all values identical
        result["friedman"] = {"test": "Friedman test (repeated measures)",
                              "n": n, "statistic": np.nan, "p": np.nan,
                              "effect_size": np.nan, "effect_name": "Kendall's W",
                              "note": str(e)}

    raw_ps, tests = [], []
    for hi, lo in CONTRASTS:
        pair = pivot[[hi, lo]].dropna()
        diffs = (pair[hi] - pair[lo]).to_numpy(dtype=float)
        entry = {
            "contrast": f"{hi}−{lo}",
            "test": "paired Wilcoxon signed-rank",
            "n_pairs": len(diffs),
            "mean_diff": float(np.mean(diffs)) if len(diffs) else np.nan,
            "statistic": np.nan, "p": np.nan, "p_holm": np.nan,
            "effect_size": np.nan, "effect_name": "rank-biserial r",
            "note": None,
        }
        if len(diffs) < MIN_TEST_N:
            entry["note"] = f"requires N ≥ {MIN_TEST_N} pairs (have {len(diffs)})"
        elif np.all(diffs == 0):
            entry["note"] = "all paired differences are zero"
        else:
            stat, p = sstats.wilcoxon(diffs)
            entry["statistic"] = float(stat)
            entry["p"] = float(p)
            entry["effect_size"] = _rank_biserial(diffs)
            raw_ps.append(float(p))
        tests.append(entry)
    if raw_ps:
        adjusted = _holm(raw_ps)
        i = 0
        for entry in tests:
            if not np.isnan(entry["p"]):
                entry["p_holm"] = adjusted[i]
                i += 1
    result["pairwise"] = tests
    return result
