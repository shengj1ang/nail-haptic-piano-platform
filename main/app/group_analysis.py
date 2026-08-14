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
from scipy import stats as sstats

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
from .pilot_study import list_participants
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


def anova_cell_frame(pf_df: pd.DataFrame, metric: str,
                     conditions: Optional[List[str]] = None):
    """(frame, dropped) for the balanced (participant x condition x
    finger) design the ANOVA needs.

    A participant enters only with a non-NaN value in ALL
    len(conditions) x 5 cells; anyone short of that is excluded whole
    (listwise, as a repeated-measures model requires) and appears in
    dropped as {participant: "missing B/F3, C/F5"}. Nothing is imputed
    and no cell is zero-filled."""
    conditions = list(conditions or ANOVA_CONDITIONS)
    cols = ["participant", "condition", "finger_id", metric]
    empty = pd.DataFrame(columns=cols)
    if pf_df.empty or metric not in pf_df.columns:
        return empty, {}
    sub = pf_df[pf_df["condition"].isin(conditions)
                & pf_df["finger_id"].isin(FINGER_IDS)][cols]
    kept, dropped = [], {}
    for participant, prow in sub.groupby("participant", sort=True):
        present = prow.dropna(subset=[metric])
        have = set(zip(present["condition"], present["finger_id"]))
        missing = [(c, f) for c in conditions for f in FINGER_IDS if (c, f) not in have]
        if missing:
            dropped[str(participant)] = "missing " + ", ".join(f"{c}/F{f}" for c, f in missing)
        else:
            kept.append(present)
    if not kept:
        return empty, dropped
    # Ordered by the requested condition order (not alphabetically) so the
    # frame reads in the same order as everything else in the window; the
    # column itself stays a plain string for pingouin and group_center.
    order = {c: i for i, c in enumerate(conditions)}
    frame = pd.concat(kept, ignore_index=True)
    frame = (frame.assign(_ord=frame["condition"].map(order))
                  .sort_values(["participant", "_ord", "finger_id"], ignore_index=True)
                  .drop(columns="_ord"))
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


def rm_anova_finger(pf_df: pd.DataFrame, metric: str = "rt_complete_s",
                    conditions: Optional[List[str]] = None) -> dict:
    """Two-way within-participant ANOVA, Condition x Finger ID, on one
    per-finger metric. See the module docstring for the model, the error
    terms, the effect size and why only reaction time is fitted.

    Returns a dict that is always complete enough to render: when the
    model cannot run, "reason" says why and "effects" is empty. Never
    raises for a data reason - a Group Analysis tab must not be able to
    take the window down."""
    conditions = list(conditions or ANOVA_CONDITIONS)
    frame, dropped = anova_cell_frame(pf_df, metric, conditions)
    n = int(frame["participant"].nunique()) if len(frame) else 0
    result = {
        "metric": metric,
        "conditions": conditions,
        "n_participants": n,
        "n_dropped": len(dropped),
        "dropped": dropped,
        "n_cells": len(frame),
        "cells_per_participant": len(conditions) * len(FINGER_IDS),
        "exploratory": n < EXPLORATORY_N,
        "effects": [],
        "frame": frame,
        "reason": None,
    }
    if n < MIN_TEST_N:
        result["reason"] = (
            f"requires N ≥ {MIN_TEST_N} participants with a complete "
            f"{len(conditions)} × {len(FINGER_IDS)} cell grid on {metric} (have {n})"
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
        aov = pg.rm_anova(data=frame, dv=metric, within=["condition", "finger_id"],
                          subject="participant", detailed=True, effsize="np2")
    except Exception as e:  # degenerate data (zero variance, singular error term)
        result["reason"] = f"ANOVA could not be fitted ({type(e).__name__}: {e})"
        return result

    sphericity_within = {
        "condition": ["condition"],
        "finger_id": ["finger_id"],
        "condition * finger_id": ["condition", "finger_id"],
    }
    aov = aov.set_index("Source")
    for source, label in ANOVA_EFFECT_LABELS.items():
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
    """Group mean / SD / 95% t-CI per (condition, finger_id) over the
    same complete-case participants the ANOVA uses, so the descriptive
    table and the model describe one identical grid. n counts
    PARTICIPANTS, as everywhere else in this module."""
    frame, _ = anova_cell_frame(pf_df, metric, conditions)
    if frame.empty:
        return pd.DataFrame(columns=["condition", "finger_id", "n", "mean", "sd",
                                     "sem", "ci95_lo", "ci95_hi"])
    return group_center(frame, metric, ["condition", "finger_id"])


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
    C-B mean difference, t interval/test and Cohen's dz; a paired Wilcoxon
    signed-rank result is retained as a small-sample sensitivity check. No
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
    half = float(sstats.t.ppf(0.975, n - 1)) * sem
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
        "ci95_lo": mean_diff - half,
        "ci95_hi": mean_diff + half,
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
