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

Condition x Finger repeated-measures ANOVA
------------------------------------------
The pre-registered finger-specific model (Method, "Outcome Measures and
Pilot Analysis Plan") is a two-way within-participant ANOVA with
Condition (visual B, haptic C) and homologous Finger ID (1..5) as
repeated factors, fitted on the per-finger cell means produced by
per_finger_metrics(). For participant i, condition j (a = 2 levels) and
finger k (b = 5 levels) the cell mean Y_ijk is modelled as

    Y_ijk = mu + pi_i + alpha_j + beta_k + (alpha*beta)_jk + e_ijk,

with pi_i the participant (block) effect. Because every participant
supplies every cell, each effect is tested against its own
participant-by-effect interaction as the error term:

    F_A  = MS_A  / MS_AxS   with df = (a-1),        (a-1)(n-1)
    F_B  = MS_B  / MS_BxS   with df = (b-1),        (b-1)(n-1)
    F_AB = MS_AB / MS_ABxS  with df = (a-1)(b-1),   (a-1)(b-1)(n-1)

so at n = 7 the degrees of freedom are (1, 6) for Condition and (4, 24)
for both Finger and the interaction. Effect size is partial eta squared,

    eta_p^2 = SS_effect / (SS_effect + SS_error(effect)),

reported for every effect because with a pilot-sized sample the effect
magnitude carries the information, not the binary p verdict.

Sphericity. Mauchly's W tests whether the covariance matrix of the
orthonormalised contrasts of a repeated factor is spherical; when it is
violated the uncorrected F is liberal. Condition has only a = 2 levels,
hence a single contrast and no sphericity assumption to violate
(epsilon = 1 by construction), so Mauchly and Greenhouse-Geisser apply
only to the Finger main effect and to the Condition x Finger
interaction. At this pilot N, Mauchly has little power, so every effect
with more than one contrast reports Greenhouse-Geisser-rescaled degrees
of freedom and p as the primary value; Mauchly remains diagnostic only.

Why reaction time carries the ANOVA and finger accuracy does not: the
per-finger FA cells are bounded proportions sitting against the ceiling
(in the collected sample the 2 x 5 cells run 0.837-1.000 with about a
third exactly at 1.000). At the ceiling the cell variance is compressed
towards zero and is a deterministic function of the mean, so the
normality and homogeneity assumptions behind an F ratio - and above all
the interaction test, which asks whether a condition difference differs
across fingers - are not credible. FA is therefore reported as a
descriptive mean / SD / 95% CI table over the identical cell grid, with
the ceiling diagnostics stated alongside it, and no F test is computed
on it. ceiling_diagnostics() recomputes that justification from
whatever data is actually loaded rather than trusting the numbers above.

What the ceiling rules out is the F ratio, not every model. The accuracy
question is answered instead by glmm_accuracy() further down this module:
the same cells as a binomial mixed model with a participant random
intercept, where a cell at 100% is an ordinary observation. That is where
the Condition x Finger test on accuracy lives, and it is a sensitivity
analysis - the participant-level paired contrast stays the pre-specified
inference.

The ANOVA needs a complete (participant x condition x finger) grid;
participants missing any cell are dropped from the model as a whole and
listed by name, never imputed. Pingouin supplies the fit (one call
returns the table, partial eta squared, Mauchly and Greenhouse-Geisser);
it is imported lazily so that a missing optional dependency degrades
this one tab instead of taking the whole Group Analysis window down.
"""

import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import optimize as soptimize
from scipy import stats as sstats
from scipy.special import expit

# The participant-LEVEL half of this analysis lives in
# app.participant_analysis (see its docstring): everything that reduces
# ONE participant's rows to that participant's own numbers, so the
# single-participant window can show the same tables without importing a
# group module. They are re-exported here under their original names -
# `ga.per_finger_metrics(...)` and friends keep working - and this module
# adds only what genuinely needs more than one participant.
from .participant_analysis import (  # noqa: F401  (re-exported)
    CATEGORIES,
    CONDITIONS,
    FINGER_ID_NAMES,
    FINGER_IDS,
    FINGER_ORDER,
    GUIDANCE_CONDITIONS,
    LEVELS,
    THRESHOLD_SENSITIVITY_VALUES,
    UNRESOLVED,
    VALIDITY_INVALID_CARRYOVER,
    classify_event_outcome,
    participant_cell_metrics,
    participant_condition_metrics,
    participant_outcome_proportions,
    participant_repetition_metrics,
    per_finger_metrics,
    quality_summary,
    session_position_metrics,
    threshold_sensitivity,
    valid_events,
    within_cell_repetition,
)
from .participant_export import export_paths
from .pilot_study import DATA_DIR as STUDY_DATA_DIR
from .pilot_study import list_participants, load_trial_structure
from .sequence_generator import (
    LEVEL_DISPLAY,
    LEVEL_SYMBOL,
    LEVEL_TICK_LABEL,
)

LEVEL_SYMBOLS: Dict[str, str] = dict(LEVEL_SYMBOL)
LEVEL_DISPLAY_LABELS: Dict[str, str] = dict(LEVEL_DISPLAY)
LEVEL_TICK_LABELS: Dict[str, str] = dict(LEVEL_TICK_LABEL)

# Within-participant performance contrast, (minuend, subtrahend).
CONTRASTS: List[Tuple[str, str]] = [("C", "B")]

# Inferential gating: below MIN_TEST_N complete cases no test is run at
# all (a paired Wilcoxon cannot even reach p < .05 two-sided before
# n = 5); everything below EXPLORATORY_N is labelled exploratory.
MIN_TEST_N = 5
EXPLORATORY_N = 15


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
    "actual_finger", "finger_correct", "target_finger_probability", "rt_s", "validity",
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


def participant_handedness(participants: Sequence[str],
                           data_dir: Path = STUDY_DATA_DIR) -> Dict[str, str]:
    """{participant: self-reported handedness} from TrialStructure.json.

    Descriptive metadata only (app.pilot_study.HANDEDNESS_OPTIONS): it
    labels figures and enters no statistic. A participant whose file is
    missing or unreadable is left out instead of raising, so a group whose
    schedules were archived elsewhere still analyses.
    """
    handedness: Dict[str, str] = {}
    for participant in participants:
        try:
            doc = load_trial_structure(participant, data_dir)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        value = str(doc.get("participant", {}).get("handedness", "")).strip().lower()
        if value:
            handedness[participant] = value
    return handedness


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
# Level 2: group centre over participant-level values
#
# Level 1 - one participant's trial rows to that participant's condition
# and condition x level means - is participant_condition_metrics /
# participant_cell_metrics, imported above from app.participant_analysis.

# Percentile bootstrap over participants is the study's uniform 95% CI for
# every Group Analysis figure: resample the participant-level values with
# replacement, recompute the mean, and read the 2.5 / 97.5 percentiles. It
# replaces the older t-interval so a near-ceiling accuracy CI cannot cross
# 0/100%. Seeded (fixed) so a figure and its exported CSV reproduce exactly.
BOOTSTRAP_RESAMPLES = 10000
BOOTSTRAP_SEED = 20260901


def bootstrap_ci(values, n_resamples: int = BOOTSTRAP_RESAMPLES, rng=None):
    """Percentile bootstrap 95% CI of the mean of participant-level values.

    Resample the values (participants, the independent unit) with
    replacement, recompute the mean, and take the 2.5 / 97.5 percentiles
    over ``n_resamples`` draws.  Every resampled mean averages observed
    values, so the interval stays inside their range - a near-ceiling
    accuracy CI can never cross 100%.  Returns ``(nan, nan)`` for fewer than
    two finite values.  Pass ``rng`` to share one generator across a batch
    of cells (keeps a whole figure's CIs reproducible in one call);
    otherwise a fixed-seed generator makes a standalone call reproducible.
    """
    values = np.asarray(values, dtype=float)
    # Sort so the CI depends only on the multiset of values, not the row
    # order they arrive in: the same participants give the same interval
    # whether computed for a figure or its caption/CSV.
    values = np.sort(values[np.isfinite(values)])
    n = len(values)
    if n < 2:
        return float("nan"), float("nan")
    if rng is None:
        rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = rng.integers(0, n, size=(n_resamples, n))
    means = values[draws].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def group_center(df: pd.DataFrame, value_col: str, group_cols: List[str]) -> pd.DataFrame:
    """Mean / SD / percentile-bootstrap 95% CI of participant-level values
    per group cell.  n counts PARTICIPANT values (the independent unit),
    NaNs dropped; the interval is a participant bootstrap (:func:`bootstrap_ci`)
    and is NaN when n < 2 - never fabricated."""
    out_cols = group_cols + ["n", "mean", "sd", "sem", "ci95_lo", "ci95_hi"]
    if df.empty:
        return pd.DataFrame(columns=out_cols)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    rows = []
    for key, sub in df.groupby(group_cols, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        vals = sub[value_col].dropna().to_numpy(dtype=float)
        n = len(vals)
        mean = float(np.mean(vals)) if n else np.nan
        sd = float(np.std(vals, ddof=1)) if n >= 2 else np.nan
        sem = sd / np.sqrt(n) if n >= 2 else np.nan
        lo, hi = bootstrap_ci(vals, rng=rng)
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
    contrast): the planned C-B guidance comparison. Participants missing
    either side contribute no row (pairing is never broken or imputed)."""
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
# Condition x Finger repeated-measures ANOVA (model and rationale: module
# docstring). Fitted on the per_finger_metrics() cells; the participant is
# the block, so n is always the participant count.

# Only the two guidance conditions enter the model: A is key-only and
# carries no finger cue, so "does the finger cue help this digit more"
# is undefined there. A is still visible in the descriptive Fingers tab.
ANOVA_CONDITIONS: List[str] = list(GUIDANCE_CONDITIONS)

# Reaction-time metrics the ANOVA can be fitted on, primary first.
ANOVA_METRICS: List[Tuple[str, str]] = [
    ("rt_complete_s", "RT — key-and-finger-correct events"),
    ("rt_s", "RT — all responded events"),
]

# Displayed names for pingouin's Source values, in reporting order.
ANOVA_EFFECT_LABELS: Dict[str, str] = {
    "condition": "Condition (B vs C)",
    "finger_id": "Finger ID (1–5)",
    "condition * finger_id": "Condition × Finger ID",
}

# The second repeated factor is a parameter, not a constant: the same
# two-way model is fitted with Finger ID, difficulty level, or within-cell
# repetition in that slot. Each entry is (levels, display label); the
# levels list also fixes the reporting order and is what a participant
# must be complete on to enter the model.
ANOVA_FACTORS: Dict[str, Tuple[list, str]] = {
    "finger_id": (list(FINGER_IDS), "Finger ID (1–5)"),
    "level": (list(LEVELS), "Difficulty (α, β, γ)"),
    "repetition": ([1, 2, 3], "Repetition (1–3)"),
}


def anova_effect_labels(factor: str) -> Dict[str, str]:
    """Source -> display label for a two-way model on `factor`."""
    label = ANOVA_FACTORS[factor][1]
    return {
        "condition": "Condition (B vs C)",
        factor: label,
        f"condition * {factor}": f"Condition × {label.split(' (')[0]}",
    }

# A cell proportion is "at ceiling" when every judged event in it was
# correct; compared with a tolerance because fa is a computed ratio.
CEILING_TOL = 1e-9

_pingouin = None  # lazily imported module, or the import error string


def load_pingouin():
    """Import pingouin on first use and cache it. Returns (module, error):
    exactly one is None. Deferred rather than imported at module level
    because pingouin pulls in statsmodels/seaborn (seconds of start-up)
    and because a missing optional dependency must degrade the ANOVA tab
    only, never the whole Group Analysis window."""
    global _pingouin
    if _pingouin is None:
        try:
            import pingouin  # noqa: PLC0415 - deliberate lazy import
            _pingouin = pingouin
        except Exception as e:
            # Name the interpreter: the usual cause is pip having
            # installed into a different Python than the one running the
            # GUI, and "not installed" alone sends people to reinstall it
            # into the same wrong environment again.
            _pingouin = (f"pingouin is not importable in this interpreter "
                         f"({type(e).__name__}: {e}); running: {sys.executable}")
    return (None, _pingouin) if isinstance(_pingouin, str) else (_pingouin, None)


def two_way_cell_frame(df: pd.DataFrame, metric: str, factor: str,
                       conditions: Optional[List[str]] = None):
    """(frame, dropped) for the balanced (participant x condition x
    factor) design a two-way repeated-measures ANOVA needs.

    Generalises the original finger-only version: `factor` is any key of
    ANOVA_FACTORS, and completeness is judged against that factor's own
    level list. Listwise as before - a participant missing any cell is
    excluded whole and named in dropped."""
    levels, _label = ANOVA_FACTORS[factor]
    conditions = list(conditions or ANOVA_CONDITIONS)
    cols = ["participant", "condition", factor, metric]
    empty = pd.DataFrame(columns=cols)
    if df.empty or metric not in df.columns or factor not in df.columns:
        return empty, {}
    sub = df[df["condition"].isin(conditions) & df[factor].isin(levels)][cols]
    kept, dropped = [], {}
    for participant, prow in sub.groupby("participant", sort=True):
        present = prow.dropna(subset=[metric])
        have = set(zip(present["condition"], present[factor]))
        missing = [(c, f) for c in conditions for f in levels if (c, f) not in have]
        if missing:
            dropped[str(participant)] = "missing " + ", ".join(f"{c}/{f}" for c, f in missing)
        else:
            kept.append(present)
    if not kept:
        return empty, dropped
    order = {c: i for i, c in enumerate(conditions)}
    lorder = {lv: i for i, lv in enumerate(levels)}
    frame = pd.concat(kept, ignore_index=True)
    frame = (frame.assign(_c=frame["condition"].map(order), _f=frame[factor].map(lorder))
                  .sort_values(["participant", "_c", "_f"], ignore_index=True)
                  .drop(columns=["_c", "_f"]))
    return frame, dropped


def anova_cell_frame(pf_df: pd.DataFrame, metric: str,
                     conditions: Optional[List[str]] = None):
    """(frame, dropped) for the balanced (participant x condition x
    finger) design the ANOVA needs.

    A participant enters only with a non-NaN value in ALL
    len(conditions) x 5 cells; anyone short of that is excluded whole
    (listwise, as a repeated-measures model requires) and appears in
    dropped as {participant: "missing B/F3, C/F5"}. Nothing is imputed
    and no cell is zero-filled."""
    frame, dropped = two_way_cell_frame(pf_df, metric, "finger_id", conditions)
    # Historical wording of the dropped-cell note, kept so the existing
    # design paragraph and its test read unchanged.
    dropped = {k: v.replace("missing ", "missing ").replace("/", "/F")
               for k, v in dropped.items()}
    return frame, dropped


def _aov_column(row, *names):
    """Read one pingouin ANOVA column across naming schemes (>=0.6 uses
    p_unc / p_GG_corr, earlier releases p-unc / p-GG-corr); NaN when the
    release emits none of them (e.g. no correction column at all)."""
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return float(row[name])
    return np.nan


def _mauchly(pg, frame: pd.DataFrame, metric: str, within: List[str]) -> dict:
    """Mauchly's test of sphericity for one repeated factor (or, with two
    entries in within, for their interaction contrasts). A factor with
    two levels has a single contrast and therefore no sphericity
    assumption - flagged applicable=False instead of being tested."""
    out = {"applicable": True, "W": np.nan, "chi2": np.nan, "dof": np.nan,
           "p": np.nan, "spherical": None, "note": None}
    n_levels = [frame[f].nunique() for f in within]
    if max(n_levels) <= 2 and len(within) == 1:
        out.update(applicable=False, spherical=True,
                   note="2 levels — sphericity holds by definition (ε = 1)")
        return out
    try:
        res = pg.sphericity(data=frame, dv=metric, subject="participant", within=within)
    except Exception as e:
        out["note"] = f"not computable ({type(e).__name__}: {e})"
        return out
    spher, w, chi2, dof, pval = tuple(res)[:5]
    out.update(W=float(w) if w is not None else np.nan,
               chi2=float(chi2) if chi2 is not None else np.nan,
               dof=float(dof) if dof is not None else np.nan,
               p=float(pval) if pval is not None else np.nan,
               spherical=bool(spher))
    return out


def rm_anova_two_way(df: pd.DataFrame, metric: str, factor: str,
                     conditions: Optional[List[str]] = None) -> dict:
    """Two-way within-participant ANOVA, Condition x `factor`, on one
    participant-level cell metric. See the module docstring for the
    model, the error terms and the effect size.

    `factor` is any key of ANOVA_FACTORS: "finger_id" for the primary
    per-digit model, "level" for the difficulty interaction, or
    "repetition" for the within-cell practice interaction. The three are
    the same model with a different second repeated factor, so they share
    one implementation and one reporting contract.

    Returns a dict that is always complete enough to render: when the
    model cannot run, "reason" says why and "effects" is empty. Never
    raises for a data reason - a Group Analysis tab must not be able to
    take the window down."""
    levels, factor_label = ANOVA_FACTORS[factor]
    effect_labels = anova_effect_labels(factor)
    conditions = list(conditions or ANOVA_CONDITIONS)
    frame, dropped = two_way_cell_frame(df, metric, factor, conditions)
    n = int(frame["participant"].nunique()) if len(frame) else 0
    result = {
        "metric": metric,
        "conditions": conditions,
        "n_participants": n,
        "n_dropped": len(dropped),
        "dropped": dropped,
        "n_cells": len(frame),
        "factor": factor,
        "factor_label": factor_label,
        "levels": list(levels),
        "cells_per_participant": len(conditions) * len(levels),
        "exploratory": n < EXPLORATORY_N,
        "effects": [],
        "frame": frame,
        "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with a complete "
            f"{len(conditions)} × {len(levels)} cell grid on {metric} (have {n})"
            + (f"; dropped for incomplete cells: {', '.join(sorted(dropped))}" if dropped else "")
        )
        return result
    pg, error = load_pingouin()
    if error:
        # Deliberately NOT "-r requirements.txt": that file pins the whole
        # stack, and replaying it into an existing working environment can
        # move numpy/pandas out from under mediapipe/PySide6. pingouin is
        # the only thing missing here.
        result["reason"] = (error + " — run `python -m pip install pingouin` with THAT "
                            "interpreter to fit the ANOVA; the cell means and the "
                            "descriptive tables below need no extra dependency")
        return result

    try:
        aov = pg.rm_anova(data=frame, dv=metric, within=["condition", factor],
                          subject="participant", detailed=True, effsize="np2")
    except Exception as e:  # degenerate data (zero variance, singular error term)
        result["reason"] = f"ANOVA could not be fitted ({type(e).__name__}: {e})"
        return result

    sphericity_within = {
        "condition": ["condition"],
        factor: [factor],
        f"condition * {factor}": ["condition", factor],
    }
    aov = aov.set_index("Source")
    for source, label in effect_labels.items():
        if source not in aov.index:
            continue
        row = aov.loc[source]
        df1 = float(row["ddof1"])
        df2 = float(row["ddof2"])
        p_unc = _aov_column(row, "p_unc", "p-unc")
        eps = _aov_column(row, "eps")
        p_gg = _aov_column(row, "p_GG_corr", "p-GG-corr")
        mauchly = _mauchly(pg, frame, metric, sphericity_within[source])
        # Pilot-N rule: whenever an effect has more than one contrast,
        # headline Greenhouse-Geisser-rescaled df and p regardless of the
        # low-powered Mauchly verdict. Mauchly remains visible as a
        # diagnostic. The two-level Condition effect has one contrast and
        # is never corrected.
        gg_applicable = mauchly["applicable"] and df1 > 1 and not np.isnan(p_gg)
        violated = bool(gg_applicable and mauchly["p"] < 0.05)
        result["effects"].append({
            "source": source,
            "label": label,
            "df1": df1, "df2": df2,
            "ss": float(row["SS"]), "ms": float(row["MS"]),
            "F": float(row["F"]),
            "p_unc": p_unc,
            "np2": float(row["np2"]) if "np2" in row.index else np.nan,
            "eps": eps,
            "p_gg": p_gg if gg_applicable else np.nan,
            "df1_gg": df1 * eps if gg_applicable and not np.isnan(eps) else np.nan,
            "df2_gg": df2 * eps if gg_applicable and not np.isnan(eps) else np.nan,
            "mauchly": mauchly,
            "gg_applicable": gg_applicable,
            "sphericity_violated": violated,
            "p_reported": p_gg if gg_applicable else p_unc,
            "correction": "Greenhouse–Geisser" if gg_applicable else "none",
        })
    return result


def rm_anova_finger(pf_df: pd.DataFrame, metric: str = "rt_complete_s",
                    conditions: Optional[List[str]] = None) -> dict:
    """Primary model: Condition × Finger ID on the per-finger cells."""
    return rm_anova_two_way(pf_df, metric, "finger_id", conditions)


def rm_anova_difficulty(cell_df: pd.DataFrame, metric: str = "rt_complete_s",
                        conditions: Optional[List[str]] = None) -> dict:
    """Condition × Difficulty on the participant × condition × level
    cells. The interaction tests whether the guidance advantage changes
    with generated sequence difficulty; without it, a flat-looking set of
    per-level differences is a description and not a result."""
    return rm_anova_two_way(cell_df, metric, "level", conditions)


def rm_anova_repetition(rep_df: pd.DataFrame, metric: str = "rt_complete_s",
                        conditions: Optional[List[str]] = None) -> dict:
    """Condition × Repetition on the participant × condition ×
    within-cell repetition means. The interaction tests whether the two
    guidance conditions improve at different rates across the three
    repetitions of a cell, i.e. whether the gap closes with practice."""
    return rm_anova_two_way(rep_df, metric, "repetition", conditions)


def ceiling_diagnostics(pf_df: pd.DataFrame, metric: str = "fa",
                        conditions: Optional[List[str]] = None) -> dict:
    """How hard a bounded per-finger proportion sits against its ceiling,
    over the same complete-case grid the ANOVA would use. This is the
    stated, recomputed reason FA is reported descriptively instead of
    being pushed through an F test (module docstring)."""
    frame, dropped = anova_cell_frame(pf_df, metric, conditions)
    vals = frame[metric].to_numpy(dtype=float) if len(frame) else np.array([])
    at_ceiling = int((vals >= 1.0 - CEILING_TOL).sum()) if vals.size else 0
    return {
        "metric": metric,
        "n_participants": int(frame["participant"].nunique()) if len(frame) else 0,
        "n_cells": int(vals.size),
        "n_at_ceiling": at_ceiling,
        "prop_at_ceiling": at_ceiling / vals.size if vals.size else np.nan,
        "min": float(np.min(vals)) if vals.size else np.nan,
        "max": float(np.max(vals)) if vals.size else np.nan,
        "dropped": dropped,
    }


def finger_cell_descriptives(pf_df: pd.DataFrame, metric: str = "fa",
                             conditions: Optional[List[str]] = None) -> pd.DataFrame:
    """Group mean / SD / 95% bootstrap CI per (condition, finger_id) over the
    same complete-case participants the ANOVA uses, so the descriptive
    table and the model describe one identical grid. n counts
    PARTICIPANTS, as everywhere else in this module."""
    frame, _ = anova_cell_frame(pf_df, metric, conditions)
    if frame.empty:
        return pd.DataFrame(columns=["condition", "finger_id", "n", "mean", "sd",
                                     "sem", "ci95_lo", "ci95_hi"])
    return group_center(frame, metric, ["condition", "finger_id"])


# ---------------------------------------------------------------------------
# Binomial GLMM on the bounded accuracy outcome (model and rationale below)
#
# The ANOVA above is fitted on reaction time only: finger accuracy is a
# bounded proportion sitting against 1.0, and at the ceiling the cell
# variance is compressed and tied to the mean, so an F ratio on those
# cells - above all its interaction term - is not credible. That argument
# rules out the ANOVA. It does NOT rule out a model built for bounded
# outcomes, and saying "no test at all" where one exists would be the
# weaker claim, so the accuracy question is answered here instead by the
# model the outcome actually has:
#
#     y_ijk ~ Binomial(n_ijk, p_ijk)
#     logit(p_ijk) = b0 + b_C*C_j + b_F*F_k + b_CF*(C_j x F_k) + u_i
#     u_i ~ N(0, sigma^2)
#
# for participant i, condition j and homologous finger k. The logit link
# removes the ceiling problem outright - a cell at 1.0 is a large linear
# predictor, not a zero-variance cell - and the binomial part supplies
# the mean-variance relationship the ANOVA had to assume away. The
# participant random intercept u_i is what keeps events from being
# treated as independent observations: it is the model-based version of
# the aggregation rule this module follows everywhere else.
#
# Fitted on the same events as the per-finger cells (accuracy_cell_counts),
# by maximum likelihood with adaptive Gauss-Hermite quadrature over u_i.
# Only numpy/scipy are needed, so unlike the ANOVA this analysis has no
# optional dependency to degrade around.
#
# Three things about the reporting are deliberate:
#
# 1. Headline standard errors are CLUSTER-ROBUST by participant (sandwich,
#    with the G/(G-1) small-sample scaling and a t(G-1) reference), not the
#    model-based ones. A random intercept alone assumes the condition
#    contrast is homogeneous across participants; if it is not, the
#    model-based SE of that contrast is anti-conservative. The sandwich
#    stays valid under that misspecification, and t(G-1) is the same
#    reference distribution as the participant-level paired t-test, which
#    is the conservative counterpart this model has to agree with.
# 2. Effects are tested by likelihood-ratio tests over the nested models
#    (Type II: each term against the model holding everything else),
#    reported alongside the Wald z, because the LRT is the better-behaved
#    test for a variance-component model at 20 clusters.
# 3. The model is a SENSITIVITY ANALYSIS. The pre-specified inference is
#    the participant-level paired contrast; this exists to show that the
#    accuracy conclusion does not depend on the aggregation, and it is
#    reported as agreeing or not agreeing with it, never as replacing it.

GLMM_QUAD_NODES = 15          # adaptive Gauss-Hermite nodes per participant
GLMM_TERM_LABELS: Dict[str, str] = {
    "condition": "Condition (B vs C)",
    "finger_id": "Finger ID (1–5)",
    "condition * finger_id": "Condition × Finger ID",
}


def accuracy_cell_counts(event_rows: List[dict],
                         conditions: Optional[List[str]] = None) -> pd.DataFrame:
    """Per (participant, condition, finger_id) SUCCESS / TRIAL counts over
    exactly the events per_finger_metrics() judges: valid, responded,
    target finger known, finger verdict present. `successes` counts the
    events that were key AND finger correct, so successes / trials is the
    same `fa` the per-finger cells and ceiling_diagnostics() are computed
    from - the model and the descriptive table then describe one identical
    set of events, cell by cell, and cannot drift apart.

    Counts rather than proportions because the model is fitted on them:
    every event in a cell shares one covariate row, so a Bernoulli GLMM
    over events and a binomial GLMM over these counts have the same
    likelihood. Aggregating costs nothing statistically and keeps the fit
    at (participants x conditions x 5) rows instead of tens of thousands.
    """
    conditions = list(conditions or ANOVA_CONDITIONS)
    events = [e for e in valid_events(event_rows)
              if not e["timed_out"]
              and e.get("target_finger") in FINGER_ORDER
              and e["condition"] in conditions
              and e.get("finger_correct") is not None]
    rows: Dict[tuple, List[int]] = {}
    for e in events:
        key = (e["participant"], e["condition"], int(e["target_finger"][1]))
        cell = rows.setdefault(key, [0, 0])
        cell[0] += 1 if (e["key_correct"] and e["finger_correct"]) else 0
        cell[1] += 1
    order = {c: i for i, c in enumerate(conditions)}
    out = pd.DataFrame(
        [{"participant": p, "condition": c, "finger_id": f,
          "successes": s, "trials": n, "fa": s / n}
         for (p, c, f), (s, n) in rows.items()],
        columns=["participant", "condition", "finger_id", "successes", "trials", "fa"])
    if out.empty:
        return out
    return (out.assign(_c=out["condition"].map(order))
               .sort_values(["participant", "_c", "finger_id"], ignore_index=True)
               .drop(columns=["_c"]))


def _glmm_design(counts: pd.DataFrame, conditions: List[str]):
    """Treatment-coded design for logit(p) = 1 + condition * finger_id.

    The reference cell is (conditions[0], FINGER_IDS[0]) - B and the thumb -
    so the condition coefficient is the C-vs-B log odds ratio AT the thumb
    and the interaction terms are the other digits' departures from it.
    Two consequences the callers depend on: the single overall condition
    effect has to come from the additive model rather than from this one,
    and a per-digit contrast has to be built from the covariance matrix
    (_glmm_simple_effects) rather than read off a coefficient.

    term_cols groups the columns by model term, which is what makes the
    Type II likelihood-ratio tests a matter of dropping a column block.
    Returns (y, n, X, names, term_cols, group_index, participants)."""
    fingers = list(FINGER_IDS)
    participants = sorted(counts["participant"].unique())
    pidx = {p: i for i, p in enumerate(participants)}
    cond = counts["condition"].to_numpy()
    fing = counts["finger_id"].to_numpy(dtype=int)

    names = ["Intercept"]
    columns = [np.ones(len(counts))]
    term_cols: Dict[str, List[int]] = {"condition": [], "finger_id": [],
                                       "condition * finger_id": []}

    def add(term: str, name: str, column) -> None:
        term_cols[term].append(len(names))
        names.append(name)
        columns.append(column.astype(float))

    for c in conditions[1:]:
        add("condition", f"condition[{c}]", cond == c)
    for f in fingers[1:]:
        add("finger_id", f"finger[{f}]", fing == f)
    for c in conditions[1:]:
        for f in fingers[1:]:
            add("condition * finger_id", f"condition[{c}]:finger[{f}]",
                (cond == c) & (fing == f))

    X = np.column_stack(columns)
    y = counts["successes"].to_numpy(dtype=float)
    n = counts["trials"].to_numpy(dtype=float)
    gi = np.array([pidx[p] for p in counts["participant"]], dtype=int)
    return y, n, X, names, term_cols, gi, participants


def _irls_logistic(y, n, X, iters: int = 50):
    """Plain (no random effect) binomial IRLS - starting values for the
    GLMM and the sigma = 0 reference the tests compare against."""
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        eta = X @ beta
        p = expit(eta)
        w = n * p * (1 - p)
        w = np.maximum(w, 1e-10)
        z = eta + (y - n * p) / w
        wx = X * w[:, None]
        try:
            step = np.linalg.solve(X.T @ wx, wx.T @ z)
        except np.linalg.LinAlgError:
            break
        if not np.all(np.isfinite(step)):
            break
        delta = np.max(np.abs(step - beta))
        beta = step
        if delta < 1e-10:
            break
    return beta


def _logsumexp(a, axis=0):
    peak = np.max(a, axis=axis)
    return peak + np.log(np.sum(np.exp(a - np.expand_dims(peak, axis)), axis=axis))


def _cluster_logliks(theta, y, n, X, gi, n_groups, z_nodes, log_w_nodes):
    """Per-participant marginal log-likelihood, integrating u_i out by
    ADAPTIVE Gauss-Hermite quadrature.

    Adaptive (nodes centred on each participant's own posterior mode and
    scaled by its curvature) rather than plain: with hundreds of events
    per participant the integrand is sharply peaked, and fixed nodes on
    the N(0, sigma^2) scale would miss the peak entirely. One node
    reproduces the Laplace approximation, which is what glmer's default
    computes; more nodes refine it.

    theta is [beta..., log sigma]. Returns a vector of length n_groups."""
    beta, log_sigma = theta[:-1], theta[-1]
    sigma = float(np.exp(log_sigma))
    eta0 = X @ beta
    # Posterior mode per participant: Newton on a strictly concave
    # function (binomial log-likelihood + Gaussian prior), so it needs no
    # safeguarding beyond an iteration cap.
    u = np.zeros(n_groups)
    curv = np.ones(n_groups)
    for _ in range(60):
        p = expit(eta0 + u[gi])
        grad = np.bincount(gi, weights=(y - n * p), minlength=n_groups) - u / sigma ** 2
        curv = np.bincount(gi, weights=(n * p * (1 - p)), minlength=n_groups) + 1 / sigma ** 2
        step = grad / curv
        u = u + step
        if np.max(np.abs(step)) < 1e-10:
            break
    tau = 1.0 / np.sqrt(curv)
    # log integrand at each node, then log-sum-exp over the node axis.
    terms = np.empty((len(z_nodes), n_groups))
    for m, zm in enumerate(z_nodes):
        um = u + np.sqrt(2.0) * tau * zm
        eta = eta0 + um[gi]
        ll = np.bincount(gi, weights=(y * eta - n * np.logaddexp(0.0, eta)),
                         minlength=n_groups)
        log_prior = -0.5 * (um / sigma) ** 2 - np.log(sigma) - 0.5 * np.log(2 * np.pi)
        terms[m] = log_w_nodes[m] + zm ** 2 + ll + log_prior
    return np.log(np.sqrt(2.0) * tau) + _logsumexp(terms, axis=0)


def _numeric_gradient(fun, x, step: float = 1e-5):
    """Central-difference gradient - used to decide whether a fit really
    is at an optimum, which a quasi-Newton optimiser working from forward
    differences cannot reliably tell us."""
    g = np.zeros(len(x))
    h = step * np.maximum(1.0, np.abs(x))
    for i in range(len(x)):
        xp, xm = x.copy(), x.copy()
        xp[i] += h[i]
        xm[i] -= h[i]
        g[i] = (fun(xp) - fun(xm)) / (2 * h[i])
    return g


def _numeric_hessian(fun, x, step: float = 1e-4):
    """Central-difference Hessian of a scalar function. The likelihood
    here has no closed-form second derivative worth hand-coding for 11
    parameters, and the simulation test checks these standard errors
    against the empirical spread of the estimator."""
    k = len(x)
    h = step * np.maximum(1.0, np.abs(x))
    H = np.zeros((k, k))
    for i in range(k):
        for j in range(i, k):
            xa, xb, xc, xd = x.copy(), x.copy(), x.copy(), x.copy()
            xa[i] += h[i]; xa[j] += h[j]
            xb[i] += h[i]; xb[j] -= h[j]
            xc[i] -= h[i]; xc[j] += h[j]
            xd[i] -= h[i]; xd[j] -= h[j]
            H[i, j] = H[j, i] = ((fun(xa) - fun(xb) - fun(xc) + fun(xd))
                                 / (4.0 * h[i] * h[j]))
    return H


def _fit_binomial_glmm(y, n, X, gi, n_groups, nodes: int = GLMM_QUAD_NODES,
                       robust: bool = True) -> dict:
    """ML fit of the random-intercept binomial GLMM. Returns a dict with
    beta, the model-based and cluster-robust covariances, sigma and the
    log-likelihood; `converged` False means the optimiser stopped short,
    which the caller reports rather than hides."""
    z_nodes, w_nodes = np.polynomial.hermite.hermgauss(nodes)
    log_w = np.log(w_nodes)

    def neg_ll(theta):
        try:
            value = -float(np.sum(_cluster_logliks(theta, y, n, X, gi, n_groups,
                                                   z_nodes, log_w)))
        except (FloatingPointError, ValueError):
            return np.inf
        return value if np.isfinite(value) else np.inf

    beta0 = _irls_logistic(y, n, X)
    theta = np.append(beta0, np.log(0.5))
    # BFGS on a numerically differentiated likelihood routinely stops with
    # "precision loss" while sitting exactly on the optimum, so convergence
    # is judged by a gradient we measure ourselves rather than by the
    # optimiser's own verdict, and a restart from the stopping point is
    # allowed to clear a premature stop.
    message = ""
    for _ in range(3):
        res = soptimize.minimize(neg_ll, theta, method="BFGS",
                                 options={"maxiter": 500, "gtol": 1e-6})
        message = str(res.message)
        moved = float(np.max(np.abs(res.x - theta)))
        theta = res.x
        if res.success or moved < 1e-8:
            break
    grad = _numeric_gradient(neg_ll, theta)
    converged = bool(np.max(np.abs(grad)) < 1e-3)
    k_beta = X.shape[1]
    out = {
        "beta": theta[:-1],
        "sigma": float(np.exp(theta[-1])),
        "loglik": -float(res.fun),
        "n_params": len(theta),
        "converged": converged,
        "grad_max": float(np.max(np.abs(grad))),
        "message": message,
        # Covariances are returned sliced to the FIXED EFFECTS. The fitted
        # parameter vector carries log sigma in its last slot, and a
        # contrast built over the coefficient names must not silently pick
        # up that row.
        "cov": None, "cov_robust": None, "se_log_sigma": np.nan,
        "n_groups": n_groups,
    }
    H = _numeric_hessian(neg_ll, theta)
    try:
        cov = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return out
    if not np.all(np.isfinite(cov)) or np.any(np.diag(cov) <= 0):
        return out
    out["cov"] = cov[:k_beta, :k_beta]
    out["se_log_sigma"] = float(np.sqrt(cov[-1, -1]))
    if not robust:
        return out
    # Cluster-robust sandwich: the score of each participant's own
    # marginal log-likelihood, by central differences on the per-cluster
    # vector the likelihood already returns.
    k = len(theta)
    scores = np.zeros((n_groups, k))
    h = 1e-5 * np.maximum(1.0, np.abs(theta))
    for j in range(k):
        tp, tm = theta.copy(), theta.copy()
        tp[j] += h[j]
        tm[j] -= h[j]
        scores[:, j] = (_cluster_logliks(tp, y, n, X, gi, n_groups, z_nodes, log_w)
                        - _cluster_logliks(tm, y, n, X, gi, n_groups, z_nodes, log_w)
                        ) / (2 * h[j])
    meat = scores.T @ scores
    scale = n_groups / max(n_groups - 1, 1)      # G/(G-1) small-sample scaling
    robust_cov = scale * (cov @ meat @ cov)
    if np.all(np.isfinite(robust_cov)) and np.all(np.diag(robust_cov) > 0):
        out["cov_robust"] = robust_cov[:k_beta, :k_beta]
    return out


def _glmm_separation(counts: pd.DataFrame, conditions: List[str]) -> List[str]:
    """Design cells with no errors (or no successes) POOLED over
    participants. A single participant's perfect cell is ordinary data
    for this model - that is the point of it - but a whole condition x
    finger cell without one error makes its coefficient diverge, and a
    number printed from a diverging coefficient is worse than no number."""
    bad = []
    for (c, f), g in counts.groupby(["condition", "finger_id"], sort=False):
        s, t = int(g["successes"].sum()), int(g["trials"].sum())
        if s == t:
            bad.append(f"{c}/F{f} has no errors ({t} events)")
        elif s == 0:
            bad.append(f"{c}/F{f} has no correct events ({t} events)")
    return bad


def _glmm_effect_rows(names, beta, cov, cov_robust, df_t) -> List[dict]:
    """One row per fixed effect: estimate on the logit scale, both
    standard errors, and the odds ratio with the robust interval."""
    se = np.sqrt(np.diag(cov)) if cov is not None else np.full(len(beta), np.nan)
    se_r = (np.sqrt(np.diag(cov_robust)) if cov_robust is not None else se)
    rows = []
    for j, name in enumerate(names):
        s = float(se_r[j])
        t = float(beta[j] / s) if s > 0 else np.nan
        crit = float(sstats.t.ppf(0.975, df_t)) if df_t > 0 else np.nan
        rows.append({
            "term": name,
            "estimate": float(beta[j]),
            "se_model": float(se[j]),
            "se_robust": s,
            "t": t,
            "p": float(2 * sstats.t.sf(abs(t), df_t)) if np.isfinite(t) else np.nan,
            "odds_ratio": float(np.exp(beta[j])),
            "or_lo": float(np.exp(beta[j] - crit * s)),
            "or_hi": float(np.exp(beta[j] + crit * s)),
        })
    return rows


def _glmm_simple_effects(names, beta, cov_use, conditions, df_t) -> List[dict]:
    """The C-vs-B log odds ratio WITHIN each finger, i.e. the contrast the
    ANOVA's interaction term would have been about. At the reference
    finger it is the condition coefficient; elsewhere it is that
    coefficient plus the finger's interaction term, so the variance comes
    from the full covariance matrix and not from adding two intervals."""
    if len(conditions) < 2 or cov_use is None:
        return []
    c = conditions[1]
    index = {name: j for j, name in enumerate(names)}
    rows = []
    crit = float(sstats.t.ppf(0.975, df_t)) if df_t > 0 else np.nan
    for f in FINGER_IDS:
        contrast = np.zeros(len(beta))
        contrast[index[f"condition[{c}]"]] = 1.0
        key = f"condition[{c}]:finger[{f}]"
        if key in index:
            contrast[index[key]] = 1.0
        est = float(contrast @ beta)
        var = float(contrast @ cov_use @ contrast)
        se = float(np.sqrt(var)) if var > 0 else np.nan
        t = est / se if se and np.isfinite(se) else np.nan
        rows.append({
            "finger_id": f,
            "finger": FINGER_ID_NAMES[f],
            "log_or": est,
            "se": se,
            "t": t,
            "p": float(2 * sstats.t.sf(abs(t), df_t)) if np.isfinite(t) else np.nan,
            "odds_ratio": float(np.exp(est)),
            "or_lo": float(np.exp(est - crit * se)) if np.isfinite(se) else np.nan,
            "or_hi": float(np.exp(est + crit * se)) if np.isfinite(se) else np.nan,
        })
    return rows


def _glmm_fitted(counts, names, beta, sigma, conditions, nodes: int) -> pd.DataFrame:
    """Model-implied and observed accuracy per (condition, finger).

    The model-implied value is the POPULATION AVERAGE, E[expit(eta + u)]
    over u ~ N(0, sigma^2) by the same quadrature the fit uses, because
    that is what the observed group mean of the cell proportions
    estimates. expit(eta) alone would be the median participant's value
    and sits systematically higher near the ceiling."""
    z_nodes, w_nodes = np.polynomial.hermite.hermgauss(nodes)
    weights = w_nodes / np.sqrt(np.pi)
    index = {name: j for j, name in enumerate(names)}
    observed = counts.groupby(["condition", "finger_id"])["fa"].mean()
    rows = []
    for c in conditions:
        for f in FINGER_IDS:
            eta = beta[index["Intercept"]]
            for key in (f"condition[{c}]", f"finger[{f}]", f"condition[{c}]:finger[{f}]"):
                if key in index:
                    eta += beta[index[key]]
            p_avg = float(np.sum(weights * expit(eta + np.sqrt(2.0) * sigma * z_nodes)))
            rows.append({
                "condition": c, "finger_id": f,
                "p_fitted": p_avg,
                "p_median_participant": float(expit(eta)),
                "observed": float(observed.get((c, f), np.nan)),
            })
    return pd.DataFrame(rows, columns=["condition", "finger_id", "p_fitted",
                                       "p_median_participant", "observed"])


def glmm_accuracy(event_rows: List[dict], conditions: Optional[List[str]] = None,
                  nodes: int = GLMM_QUAD_NODES) -> dict:
    """Binomial GLMM with a participant random intercept on finger
    accuracy - the bounded-outcome sensitivity analysis for the ANOVA the
    ceiling rules out (rationale: the section comment above).

    Unlike the repeated-measures ANOVA this model needs no complete cell
    grid: a participant missing a cell contributes the cells they have
    instead of being deleted listwise, so no data is discarded to make the
    design rectangular. Effects are tested by Type II likelihood-ratio
    tests and reported with cluster-robust (by participant) Wald
    intervals on a t(G-1) reference.

    Returns a dict that is always complete enough to render: "reason"
    says why when the model cannot be fitted and everything else stays
    empty. Never raises for a data reason - a Group Analysis tab must not
    be able to take the window down."""
    conditions = list(conditions or ANOVA_CONDITIONS)
    counts = accuracy_cell_counts(event_rows, conditions)
    n_participants = int(counts["participant"].nunique()) if len(counts) else 0
    grid = len(conditions) * len(FINGER_IDS)
    incomplete = sorted(p for p, g in counts.groupby("participant")
                        if len(g) < grid) if len(counts) else []
    result = {
        "conditions": conditions,
        "counts": counts,
        "n_participants": n_participants,
        "n_cells": len(counts),
        "n_events": int(counts["trials"].sum()) if len(counts) else 0,
        "n_successes": int(counts["successes"].sum()) if len(counts) else 0,
        "cells_per_participant": grid,
        "incomplete_participants": incomplete,
        "exploratory": n_participants < EXPLORATORY_N,
        "quadrature_nodes": nodes,
        "effects": [], "coefficients": [], "simple_effects": [],
        "condition_effect": None,
        "fitted": pd.DataFrame(),
        "sigma": np.nan, "icc": np.nan,
        "converged": False, "robust": False,
        "separation": [],
        "reason": None,
    }
    if n_participants < MIN_TEST_N:
        result["reason"] = (f"requires N ≥ {MIN_TEST_N} participants with judged "
                            f"finger events in {'/'.join(conditions)} (have "
                            f"{n_participants})")
        return result
    separation = _glmm_separation(counts, conditions)
    if separation:
        result["separation"] = separation
        result["reason"] = ("a condition × finger cell is completely separated, so its "
                            "coefficient does not exist: " + "; ".join(separation))
        return result
    y, n, X, names, term_cols, gi, participants = _glmm_design(counts, conditions)
    n_groups = len(participants)
    try:
        full = _fit_binomial_glmm(y, n, X, gi, n_groups, nodes)
    except Exception as e:  # numerical failure is a reportable outcome, not a crash
        result["reason"] = f"GLMM could not be fitted ({type(e).__name__}: {e})"
        return result
    if full["cov"] is None:
        result["reason"] = ("GLMM standard errors are not available (the likelihood "
                            "Hessian is singular at the optimum)"
                            + ("" if full["converged"] else
                               f"; the optimiser also stopped early: {full['message']}"))
        return result

    df_t = max(n_participants - 1, 1)
    cov_use = full["cov_robust"] if full["cov_robust"] is not None else full["cov"]
    result["converged"] = full["converged"]
    result["robust"] = full["cov_robust"] is not None
    result["sigma"] = full["sigma"]
    # ICC on the latent logit scale: sigma^2 / (sigma^2 + pi^2/3), the
    # share of the latent variance that is between participants. Quoted
    # because it says how much the participant random effect is doing.
    result["icc"] = float(full["sigma"] ** 2 / (full["sigma"] ** 2 + np.pi ** 2 / 3))
    result["loglik"] = full["loglik"]
    result["coefficients"] = _glmm_effect_rows(names, full["beta"], full["cov"],
                                               full["cov_robust"], df_t)
    result["simple_effects"] = _glmm_simple_effects(names, full["beta"], cov_use,
                                                    conditions, df_t)
    result["fitted"] = _glmm_fitted(counts, names, full["beta"], full["sigma"],
                                    conditions, nodes)

    # Type II likelihood-ratio tests: each term against the model that
    # holds every other term. Refitting three reduced models is cheap
    # here (the design is 10 columns over a few hundred binomial rows)
    # and an LRT behaves better than a Wald test on a variance-component
    # model with this many clusters.
    keep_all = set(range(X.shape[1]))
    additive_cols = sorted(keep_all - set(term_cols["condition * finger_id"]))
    # The additive model is fitted once with robust standard errors and
    # then reused: it is both the reference for the main-effect LRTs and
    # the model the single overall C-vs-B odds ratio comes from. Quoting
    # that number off the interaction model instead would silently make it
    # the thumb's effect, because the thumb is the reference finger.
    additive = _fit_binomial_glmm(y, n, X[:, additive_cols], gi, n_groups, nodes)
    additive_names = [names[j] for j in additive_cols]
    if additive["cov"] is not None and len(conditions) > 1:
        rows = _glmm_effect_rows(additive_names, additive["beta"], additive["cov"],
                                 additive["cov_robust"], df_t)
        term = f"condition[{conditions[1]}]"
        result["condition_effect"] = next((r for r in rows if r["term"] == term), None)
    for source in ("condition", "finger_id", "condition * finger_id"):
        drop = set(term_cols[source])
        if not drop:
            continue
        if source != "condition * finger_id":
            drop |= set(term_cols["condition * finger_id"])  # marginality
        cols = sorted(keep_all - drop)
        reference = full
        if source != "condition * finger_id":
            # Main effects are tested inside the additive model, so the
            # comparison has to be additive on both sides.
            reference = additive
        reduced = _fit_binomial_glmm(y, n, X[:, cols], gi, n_groups, nodes,
                                     robust=False)
        chi2 = 2.0 * (reference["loglik"] - reduced["loglik"])
        df = reference["n_params"] - reduced["n_params"]
        result["effects"].append({
            "source": source,
            "label": GLMM_TERM_LABELS[source],
            "chi2": float(max(chi2, 0.0)),
            "df": int(df),
            "p": float(sstats.chi2.sf(max(chi2, 0.0), df)) if df > 0 else np.nan,
            "converged": bool(reduced["converged"] and reference["converged"]),
        })
    return result


def glmm_effect_table(res: dict) -> pd.DataFrame:
    """The rendered GLMM as one tidy export table: the LRT rows, the
    fixed-effect rows and the per-finger simple effects, tagged by which
    part of the model they came from so the CSV stands alone."""
    rows = []
    for e in res.get("effects", []):
        rows.append({"part": "lrt", "term": e["source"], "label": e["label"],
                     "chi2": e["chi2"], "df": e["df"], "p": e["p"]})
    for c in res.get("coefficients", []):
        rows.append({"part": "fixed_effect", "term": c["term"], "label": c["term"],
                     "estimate_logit": c["estimate"], "se_model": c["se_model"],
                     "se_robust": c["se_robust"], "t": c["t"], "p": c["p"],
                     "odds_ratio": c["odds_ratio"], "or_lo": c["or_lo"],
                     "or_hi": c["or_hi"]})
    overall = res.get("condition_effect")
    if overall:
        rows.append({"part": "condition_effect", "term": overall["term"],
                     "label": "C vs B, additive model",
                     "estimate_logit": overall["estimate"],
                     "se_model": overall["se_model"], "se_robust": overall["se_robust"],
                     "t": overall["t"], "p": overall["p"],
                     "odds_ratio": overall["odds_ratio"], "or_lo": overall["or_lo"],
                     "or_hi": overall["or_hi"]})
    for s in res.get("simple_effects", []):
        rows.append({"part": "simple_effect", "term": f"C vs B | finger {s['finger_id']}",
                     "label": f"{s['finger_id']} {s['finger']}",
                     "estimate_logit": s["log_or"], "se_robust": s["se"], "t": s["t"],
                     "p": s["p"], "odds_ratio": s["odds_ratio"],
                     "or_lo": s["or_lo"], "or_hi": s["or_hi"]})
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "n_participants", res.get("n_participants"))
        df.insert(1, "n_events", res.get("n_events"))
        df["sigma_participant"] = res.get("sigma")
        df["icc_latent"] = res.get("icc")
        df["se_type"] = "cluster-robust by participant" if res.get("robust") else "model-based"
    return df


# ---------------------------------------------------------------------------
# Pooled finger confusion

def finger_confusion(event_rows: List[dict]) -> pd.DataFrame:
    """Target vs detected finger pooled over every participant and trial.

    One row per (condition, target_finger, actual_finger) with two counts,
    because they answer different questions and the difference is easy to
    misread:

      n       how many events landed in the cell;
      passed  how many of those still scored finger-correct.

    The detected finger is the softmax argmax while the verdict is the
    theta rule on the *target* finger's mass (app.finger_matching), so an
    off-diagonal cell whose events all passed is adjacent-fingertip
    ambiguity the scoring forgave, not a substitution the participant
    made. Without `passed` a pooled matrix looks like it contradicts the
    group's finger accuracy - the same reason the per-trial matrix in the
    quiz detail window carries both.

    actual_finger is UNRESOLVED where no fingertip was detected. Timeouts
    and events with no cued finger are out; carry-over exclusions are
    already gone via valid_events().
    """
    counts: Dict[tuple, List[int]] = {}
    for e in valid_events(event_rows):
        if e["timed_out"] or e["target_finger"] not in FINGER_ORDER:
            continue
        actual = e["actual_finger"] if e["actual_finger"] in FINGER_ORDER else UNRESOLVED
        cell = counts.setdefault((e["condition"], e["target_finger"], actual), [0, 0])
        cell[0] += 1
        cell[1] += 1 if e["finger_correct"] else 0
    return pd.DataFrame(
        [{"condition": c, "target_finger": t, "actual_finger": a, "n": n, "passed": p}
         for (c, t, a), (n, p) in sorted(counts.items())]
    )


def confusion_grid(confusion_df: pd.DataFrame,
                   conditions: Optional[Sequence[str]] = None) -> Dict[str, object]:
    """10x10 count and passed grids over the given conditions - one, several
    (GUIDANCE_CONDITIONS pools the two cued ones), or every condition when
    None. Rows target, columns detected, both in FINGER_ORDER, plus
    per-target unresolved counts and the total behind the grid."""
    sub = confusion_df
    if conditions is not None and len(sub):
        wanted = [conditions] if isinstance(conditions, str) else list(conditions)
        sub = sub[sub["condition"].isin(wanted)]
    matrix = [[0] * len(FINGER_ORDER) for _ in FINGER_ORDER]
    passed = [[0] * len(FINGER_ORDER) for _ in FINGER_ORDER]
    unresolved = {f: 0 for f in FINGER_ORDER}
    total = 0
    for row in sub.itertuples():
        total += row.n
        if row.actual_finger == UNRESOLVED:
            unresolved[row.target_finger] += row.n
            continue
        i = FINGER_ORDER.index(row.target_finger)
        j = FINGER_ORDER.index(row.actual_finger)
        matrix[i][j] += row.n
        passed[i][j] += row.passed
    return {"matrix": matrix, "passed": passed, "unresolved": unresolved, "total": total}


def confusion_totals(grid: Dict[str, object]) -> Dict[str, int]:
    """Headline counts for a grid: events on the diagonal, off-diagonal
    events that still scored correct (near-ties), off-diagonal events that
    did not (genuine substitutions), and unresolved."""
    matrix, passed = grid["matrix"], grid["passed"]
    n = len(FINGER_ORDER)
    diagonal = sum(matrix[i][i] for i in range(n))
    off = sum(matrix[i][j] for i in range(n) for j in range(n) if i != j)
    off_passed = sum(passed[i][j] for i in range(n) for j in range(n) if i != j)
    return {
        "diagonal": diagonal,
        "near_tie": off_passed,
        "substitution": off - off_passed,
        "unresolved": sum(grid["unresolved"].values()),
    }


# ---------------------------------------------------------------------------
# Data quality / audit (never an outcome)

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
    """Participant-level B/C paired inference for one performance metric.

    A is intentionally absent: without target-finger information its
    hidden-target agreement and response-selection complexity are not
    commensurate performance baselines. The primary summary is the paired
    C-B mean difference with a participant bootstrap 95% CI, a paired t-test
    and Cohen's dz; a paired Wilcoxon signed-rank result is retained as a
    small-sample sensitivity check. No
    independent-samples test is used and no test runs below MIN_TEST_N.
    """
    pivot = condition_pivot(pc_df, metric)
    complete = pivot[GUIDANCE_CONDITIONS].dropna()
    n_total = len(pivot)
    n = len(complete)
    result = {
        "metric": metric,
        "n_participants": n_total,
        "n_complete": n,
        "n_missing_pairs": n_total - n,
        "exploratory": n < EXPLORATORY_N,
        "friedman": None,
        "paired_t": None,
        "pairwise": [],
        "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with both B and C "
            f"(have {n}) — descriptive results only"
        )
        return result

    diffs = (complete["C"] - complete["B"]).to_numpy(dtype=float)
    mean_diff = float(np.mean(diffs))
    sd_diff = float(np.std(diffs, ddof=1))
    sem = sd_diff / np.sqrt(n)
    # The reported 95% CI is a participant bootstrap of the mean paired
    # difference (the study's uniform CI method). The paired t-test t/p and
    # Cohen's dz below stay the significance test.
    boot_lo, boot_hi = bootstrap_ci(diffs)
    # A paired t-test is a one-sample t-test of the paired differences.
    # Compute it directly so a synthetic or ceiling-limited sample with
    # exactly constant differences has an explicit result rather than a
    # SciPy catastrophic-cancellation warning.
    if sd_diff == 0.0:
        if mean_diff == 0.0:
            t_stat, t_p = np.nan, np.nan
        else:
            t_stat, t_p = float(np.copysign(np.inf, mean_diff)), 0.0
    else:
        t_stat = mean_diff / sem
        t_p = float(2 * sstats.t.sf(abs(t_stat), df=n - 1))
    result["paired_t"] = {
        "test": "paired t-test (C−B)",
        "contrast": "C−B",
        "n": n,
        "mean_diff": mean_diff,
        "ci95_lo": boot_lo,
        "ci95_hi": boot_hi,
        "statistic": float(t_stat),
        "p": float(t_p),
        "effect_size": mean_diff / sd_diff if sd_diff > 0 else np.nan,
        "effect_name": "Cohen's dz",
    }

    entry = {
        "contrast": "C−B",
        "test": "paired Wilcoxon signed-rank",
        "n_pairs": n,
        "mean_diff": mean_diff,
        "statistic": np.nan, "p": np.nan, "p_holm": np.nan,
        "effect_size": np.nan, "effect_name": "rank-biserial r",
        "note": None,
    }
    if np.all(diffs == 0):
        entry["note"] = "all paired differences are zero"
    else:
        stat, p = sstats.wilcoxon(diffs)
        entry["statistic"] = float(stat)
        entry["p"] = float(p)
        entry["p_holm"] = float(p)  # one planned contrast; no multiplicity adjustment
        entry["effect_size"] = _rank_biserial(diffs)
    result["pairwise"] = [entry]
    return result
