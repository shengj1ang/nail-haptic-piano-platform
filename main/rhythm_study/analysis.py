"""Rhythm experiment: event extraction, metrics and statistics.

The GUI-free analysis half. The scientific question it exists to answer:

    Does repeated haptic-guided practice let participants keep the
    intended fingering and timing once the haptic cue is taken away?

so the main comparison is PROBE 1 vs PROBE 2 vs PROBE 3 - the three
haptic-off measurements taken after 5, 10 and 15 training repetitions -
with the participant as the unit of inference throughout. Training is
descriptive, and the final unguided test is reported separately because
its condition differs on two channels, not one.

WHAT THE DATA ACTUALLY CONTAINS
===============================
Read before changing anything here; the schema was not designed for this
analysis, it was inherited from the quizzes.

* ``results.json`` is a list of ``app.quiz.QuizResult``. It carries the
  target/actual note, the target/actual finger, and ONE timestamp per
  event (``keypress_time``) against one reference (``cue_onset_time``).
  It has **no note-off**, so nothing about duration can come from it.
* ``raw/midi_raw.json`` is the complete MIDI log - note_on AND note_off,
  both stamped on the same clock as ``keypress_time``. Durations are
  reconstructed from here (see :func:`_pair_note_offs`).
* ``rhythm_recues.json`` is this study's sidecar: per note, the first and
  last cue timestamps and the re-cue count. It exists because
  ``app.quiz.load_quiz_results`` does ``QuizResult(**item)``, so an extra
  key in results.json would raise TypeError in the main study's analysis
  windows.
* the melody's own ``.json`` under ``data/rhythm_experiment/`` is where
  every TARGET time comes from - onsets, offsets and the 1/2/3-beat
  durations. It is the grid a performance is scored against.
* ``actual_finger`` is filled in by the video finger-matching pass, not
  at record time. A trial that has not been through it has no finger
  data at all, and this module refuses to report on such a set rather
  than quietly averaging over whatever happens to be present.

THE ONE THING THAT IS NOT COMPARABLE ACROSS PHASES
==================================================
Training is a cue/response task: the next note is not cued until the
last one is answered, so a participant cannot play ahead of the
apparatus and their note times are its, not theirs. Training therefore
yields a REACTION TIME (``rt_ms``) and no onset error at all.

Probes and the final test are performances against the melody's grid,
so they yield a real ONSET ERROR (``signed_onset_error_ms``) and no
reaction time.

These two are different measurements of different things and this module
never puts them in the same column. It follows that the timing half of
the "haptic withdrawal cost" (Training 5 -> Probe 1) cannot be computed;
:func:`withdrawal_costs` returns the accuracy half only and says so.
"""

import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.quiz import (
    META_FILENAME,
    RAW_MIDI_FILENAME,
    RESULTS_FILENAME,
    VALIDITY_INVALID_CARRYOVER,
    QuizMeta,
    load_quiz_results,
    quiz_dir,
    quiz_raw_dir,
)

from .runner_window import RECUE_SIDECAR_FILENAME
from .schedule import (
    PHASE_FINAL,
    PHASE_PROBE,
    PHASE_TRAINING,
    TRIAL_STATUS_COMPLETED,
    RhythmStudyError,
    list_participants,
    load_trial_melody,
    load_trial_structure,
)

# Default window an onset must land in to count towards the combined
# "complete performance" score. Deliberately a parameter and not a
# constant baked into the metric: 200 ms is a starting value for a
# first look, not a finding.
DEFAULT_ONSET_TOLERANCE_MS = 200.0

PROBE_PHASES = (PHASE_PROBE,)
PROBE_LABELS = ("Probe 1", "Probe 2", "Probe 3")


class RhythmAnalysisError(RhythmStudyError):
    pass


@dataclass(frozen=True)
class AnalysisConfig:
    """Every threshold the analysis takes a view on, in one place."""

    onset_tolerance_ms: float = DEFAULT_ONSET_TOLERANCE_MS
    #: Drop events a human marked as carry-over from a previous note, the
    #: same exclusion app.quiz.summarize applies.
    exclude_invalid_carryover: bool = True


# ---------------------------------------------------------------------------
# Reading one trial off disk
# ---------------------------------------------------------------------------


def _pair_note_offs(raw_events: Sequence[dict]) -> Dict[float, float]:
    """Map each note_on's timestamp to its matching note_off's.

    The melody is one voice and the quizzes record one keyboard, so a
    pitch is not normally held while it sounds again - but a participant
    CAN retrigger a key before releasing it, so this pairs per pitch with
    a stack rather than assuming strict alternation. A note_on never
    released (the recording stopped mid-press) simply has no entry.
    """
    open_by_note: Dict[int, List[float]] = {}
    offs: Dict[float, float] = {}
    for event in sorted(raw_events, key=lambda e: e.get("abs_time", 0.0)):
        note = event.get("note")
        when = event.get("abs_time")
        if note is None or when is None:
            continue
        if event.get("type") == "note_on":
            open_by_note.setdefault(note, []).append(when)
        elif event.get("type") == "note_off":
            stack = open_by_note.get(note)
            if stack:
                offs[stack.pop(0)] = when
    return offs


def _load_raw_midi(quiz_name: str) -> List[dict]:
    path = quiz_raw_dir(quiz_name) / RAW_MIDI_FILENAME
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else list(data.get("events", []))


def _load_sidecar(quiz_name: str) -> Dict[int, dict]:
    """Per-note re-cue record, keyed by event index. Empty when a trial
    predates the sidecar or was written by something else."""
    path = quiz_dir(quiz_name) / RECUE_SIDECAR_FILENAME
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {int(n["index"]): n for n in doc.get("notes", [])}


@dataclass
class TrialData:
    participant: str
    trial: dict
    quiz_name: str
    meta: QuizMeta
    events: List[dict] = field(default_factory=list)


def _event_rows(
    participant: str,
    trial: dict,
    quiz_name: str,
    results,
    raw_events: Sequence[dict],
    sidecar: Dict[int, dict],
    melody,
    cfg: AnalysisConfig,
) -> List[dict]:
    """One row per TARGET note - never per key press.

    Anchoring the rows on the melody rather than on what was played is
    what makes a missing note visible: a participant who played 13 of 15
    notes produces 15 rows, two of them with no actual note. Counting
    presses instead would silently rescale every accuracy.
    """
    offs = _pair_note_offs(raw_events)
    is_performance = trial["phase"] != PHASE_TRAINING
    notes = melody.notes

    rows: List[dict] = []
    for i, result in enumerate(results):
        target = notes[i] if i < len(notes) else None
        side = sidecar.get(result.index, {})

        # --- timing -----------------------------------------------------
        # cue_onset_time carries different things in the two trial types
        # (see the module docstring): the moment the note was DUE in a
        # performance, the moment it was CUED in training.
        onset_error_s = result.timing_error_s
        rt_ms = None
        signed_onset_ms = None
        if result.keypress_time is not None and onset_error_s is not None:
            if is_performance:
                signed_onset_ms = onset_error_s * 1000.0
            else:
                rt_ms = onset_error_s * 1000.0

        # --- duration ---------------------------------------------------
        actual_off = offs.get(result.keypress_time) if result.keypress_time is not None else None
        actual_duration_s = (
            actual_off - result.keypress_time
            if (actual_off is not None and result.keypress_time is not None)
            else None
        )
        target_duration_s = (
            target.note_off_time_sec - target.note_on_time_sec if target is not None else None
        )
        duration_error_ms = (
            (actual_duration_s - target_duration_s) * 1000.0
            if (actual_duration_s is not None and target_duration_s is not None)
            else None
        )

        # --- combined success ------------------------------------------
        within_tolerance = (
            signed_onset_ms is not None and abs(signed_onset_ms) <= cfg.onset_tolerance_ms
        )
        complete = bool(result.note_correct and result.finger_correct and within_tolerance)

        rows.append(
            {
                "participant": participant,
                "trial_index": trial["index"],
                "phase": trial["phase"],
                "phase_number": trial["phase_number"],
                "is_performance": is_performance,
                "quiz_name": quiz_name,
                "event_index": result.index,
                "melody": trial["melody"],
                # what was asked for
                "target_note": result.target_note,
                "target_note_name": result.target_note_name,
                "target_finger": result.target_finger,
                "target_onset_s": target.note_on_time_sec if target else None,
                "target_offset_s": target.note_off_time_sec if target else None,
                "target_duration_s": target_duration_s,
                "target_duration_beats": target.duration_beats if target else None,
                "target_hand": target.hand if target else None,
                # what happened
                "actual_note": result.actual_note,
                "actual_finger": result.actual_finger,
                "actual_onset_time": result.keypress_time,
                "actual_offset_time": actual_off,
                "actual_duration_s": actual_duration_s,
                "due_time": result.cue_onset_time if is_performance else None,
                "cue_onset_time": result.cue_onset_time if not is_performance else None,
                # scoring
                "key_correct": bool(result.note_correct),
                "finger_correct": result.finger_correct,
                "finger_reviewed": result.finger_reviewed,
                "played": result.keypress_time is not None,
                "signed_onset_error_ms": signed_onset_ms,
                "absolute_onset_error_ms": abs(signed_onset_ms) if signed_onset_ms is not None else None,
                "rt_ms": rt_ms,
                "duration_error_ms": duration_error_ms,
                "absolute_duration_error_ms": abs(duration_error_ms) if duration_error_ms is not None else None,
                "complete_success": complete,
                # provenance
                "recue_count": side.get("recue_count"),
                "first_cue_onset_time": side.get("first_cue_onset_time"),
                "validity": result.validity,
            }
        )

    if cfg.exclude_invalid_carryover:
        rows = [r for r in rows if r["validity"] != VALIDITY_INVALID_CARRYOVER]
    return rows


def collect_trial(participant: str, trial: dict, cfg: AnalysisConfig) -> Optional[TrialData]:
    """Every event of one completed trial, or None if it never ran."""
    if trial["status"] != TRIAL_STATUS_COMPLETED:
        return None
    quiz_name = trial.get("quiz_name")
    if not quiz_name:
        return None
    folder = quiz_dir(quiz_name)
    if not (folder / META_FILENAME).exists():
        raise RhythmAnalysisError(
            f"{participant} trial {trial['index']} is marked completed but its quiz folder "
            f"'{quiz_name}' is missing from data/quiz/."
        )
    meta = QuizMeta.load(folder / META_FILENAME)
    results = load_quiz_results(folder / RESULTS_FILENAME)
    melody = load_trial_melody(trial["melody"])
    rows = _event_rows(
        participant, trial, quiz_name, results,
        _load_raw_midi(quiz_name), _load_sidecar(quiz_name), melody, cfg,
    )
    return TrialData(participant, trial, quiz_name, meta, rows)


def collect_participant(participant: str, cfg: Optional[AnalysisConfig] = None) -> List[dict]:
    """Every event row for one participant, in trial order.

    Raises if any completed trial has not been through the video
    finger-matching pass: finger accuracy is the primary outcome, and a
    set where it is missing from some trials would produce a number that
    silently means something different per participant.
    """
    cfg = cfg or AnalysisConfig()
    doc = load_trial_structure(participant)
    rows: List[dict] = []
    unanalysed: List[int] = []
    for trial in doc["trials"]:
        data = collect_trial(participant, trial, cfg)
        if data is None:
            continue
        if not data.meta.analyzed:
            unanalysed.append(trial["index"])
        rows.extend(data.events)
    if unanalysed:
        raise RhythmAnalysisError(
            f"{participant}: trials {unanalysed} have no finger analysis yet. "
            "Run Quiz Analysis on them first - finger accuracy is a primary outcome and "
            "cannot be averaged over a partly-analysed set."
        )
    return rows


def collect_group(participants: Sequence[str], cfg: Optional[AnalysisConfig] = None) -> List[dict]:
    rows: List[dict] = []
    for participant in participants:
        rows.extend(collect_participant(participant, cfg))
    return rows


def available_participants() -> List[str]:
    return list_participants()


# ---------------------------------------------------------------------------
# Aggregating events into trial- and participant-level measures
# ---------------------------------------------------------------------------


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    return statistics.fmean(clean) if clean else None


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    return statistics.median(clean) if clean else None


def _proportion(flags: Sequence[Optional[bool]]) -> Optional[float]:
    """Proportion true out of ALL target notes, not out of the ones that
    have a verdict. A note the participant never played is a note they
    did not get right, and dropping it would flatter every accuracy."""
    if not flags:
        return None
    return sum(1 for f in flags if f) / len(flags)


def trial_summary(rows: Sequence[dict]) -> dict:
    """Per-trial outcome measures. `rows` must be one trial's events."""
    first = rows[0]
    return {
        "participant": first["participant"],
        "trial_index": first["trial_index"],
        "phase": first["phase"],
        "phase_number": first["phase_number"],
        "is_performance": first["is_performance"],
        "quiz_name": first["quiz_name"],
        "n_events": len(rows),
        "n_played": sum(1 for r in rows if r["played"]),
        # primary
        "finger_accuracy": _proportion([r["key_correct"] and bool(r["finger_correct"]) for r in rows]),
        "mean_absolute_onset_error_ms": _mean([r["absolute_onset_error_ms"] for r in rows]),
        "median_absolute_onset_error_ms": _median([r["absolute_onset_error_ms"] for r in rows]),
        "mean_signed_onset_error_ms": _mean([r["signed_onset_error_ms"] for r in rows]),
        # secondary
        "key_accuracy": _proportion([r["key_correct"] for r in rows]),
        "finger_accuracy_given_key": _proportion(
            [bool(r["finger_correct"]) for r in rows if r["key_correct"]]
        ),
        "mean_duration_error_ms": _mean([r["duration_error_ms"] for r in rows]),
        "mean_absolute_duration_error_ms": _mean([r["absolute_duration_error_ms"] for r in rows]),
        "complete_success_rate": _proportion([r["complete_success"] for r in rows]),
        # training only
        "mean_rt_ms": _mean([r["rt_ms"] for r in rows]),
        "total_recues": sum(r["recue_count"] or 0 for r in rows),
    }


def trial_summaries(rows: Sequence[dict]) -> List[dict]:
    by_trial: Dict[Tuple[str, int], List[dict]] = {}
    for row in rows:
        by_trial.setdefault((row["participant"], row["trial_index"]), []).append(row)
    return [trial_summary(v) for _, v in sorted(by_trial.items())]


#: The measures reported for every phase, and whether lower is better.
METRICS = {
    "finger_accuracy": ("Finger accuracy", False),
    "key_accuracy": ("Key accuracy", False),
    "mean_absolute_onset_error_ms": ("Absolute onset error (ms)", True),
    "median_absolute_onset_error_ms": ("Median absolute onset error (ms)", True),
    "mean_signed_onset_error_ms": ("Signed onset error (ms)", True),
    "mean_absolute_duration_error_ms": ("Absolute duration error (ms)", True),
    "mean_duration_error_ms": ("Duration error (ms)", True),
    "complete_success_rate": ("Complete-performance rate", False),
    "mean_rt_ms": ("Response time (ms)", True),
}

#: Metrics that only exist for a performance (probe / final). Training
#: has no grid, so asking for its onset error is a category error.
PERFORMANCE_ONLY_METRICS = (
    "mean_absolute_onset_error_ms",
    "median_absolute_onset_error_ms",
    "mean_signed_onset_error_ms",
    "complete_success_rate",
)


def probe_table(trials: Sequence[dict], metric: str) -> Dict[str, List[Optional[float]]]:
    """participant -> [Probe 1, Probe 2, Probe 3] for one metric."""
    table: Dict[str, List[Optional[float]]] = {}
    for row in trials:
        if row["phase"] != PHASE_PROBE:
            continue
        slot = row["phase_number"] - 1
        if not 0 <= slot < len(PROBE_LABELS):
            continue
        table.setdefault(row["participant"], [None] * len(PROBE_LABELS))[slot] = row.get(metric)
    return table


def complete_probe_rows(trials: Sequence[dict], metric: str) -> Tuple[List[str], List[List[float]]]:
    """Only participants with all three probes present for `metric` - a
    within-participant test cannot use a partial row, and dropping them
    silently would change what the test is about."""
    table = probe_table(trials, metric)
    names, values = [], []
    for participant in sorted(table):
        triple = table[participant]
        if all(v is not None for v in triple):
            names.append(participant)
            values.append([float(v) for v in triple])
    return names, values


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
#
# The unit of inference is the PARTICIPANT: every test below is run on
# one number per participant per probe, never on individual note events.
# Treating events as independent would inflate n by a factor of 15 and
# turn any tendency into a significant one.
#
# scipy only - no pingouin. The main study's Group Analysis loads
# pingouin lazily for its factorial RM-ANOVAs, but nothing here needs
# more than a Friedman and paired Wilcoxons, which scipy has, and this
# study should not acquire an optional dependency that the machine
# running it may not have.

#: Below this many complete participants, report descriptives and say the
#: test was not run rather than printing a p-value nobody should read.
MIN_TEST_N = 5


def _scipy_stats():
    from scipy import stats  # noqa: PLC0415 - deferred: seconds of import time

    return stats


def kendalls_w(values: Sequence[Sequence[float]]) -> Optional[float]:
    """Effect size for the Friedman test: 0 = no agreement in how
    participants rank the three probes, 1 = every participant ranks them
    identically."""
    n = len(values)
    if n == 0:
        return None
    k = len(values[0])
    if k < 2:
        return None
    stats = _scipy_stats()
    ranks = [stats.rankdata(row) for row in values]
    column_sums = [sum(r[j] for r in ranks) for j in range(k)]
    mean_sum = sum(column_sums) / k
    s = sum((c - mean_sum) ** 2 for c in column_sums)
    denominator = n**2 * (k**3 - k) / 12.0
    return s / denominator if denominator else None


def rank_biserial(differences: Sequence[float]) -> Optional[float]:
    """Matched-pairs rank-biserial correlation - the effect size that
    belongs with a Wilcoxon signed-rank test. +1 means every pair moved
    one way, -1 every pair the other."""
    stats = _scipy_stats()
    nonzero = [d for d in differences if d != 0]
    if not nonzero:
        return 0.0
    ranks = stats.rankdata([abs(d) for d in nonzero])
    total = ranks.sum()
    positive = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    negative = total - positive
    return float((positive - negative) / total)


def bootstrap_ci(
    differences: Sequence[float],
    confidence: float = 0.95,
    resamples: int = 10000,
    seed: int = 20260827,
) -> Tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap CI for the mean paired difference.

    Bootstrapped rather than taken from a t distribution because these
    are small samples of a bounded, often near-ceiling proportion, where
    the normal approximation is exactly what cannot be relied on. The
    seed is fixed so a reported interval is reproducible.
    """
    import numpy as np  # noqa: PLC0415

    if len(differences) < 2:
        return (None, None)
    rng = np.random.default_rng(seed)
    data = np.asarray(differences, dtype=float)
    draws = rng.choice(data, size=(resamples, data.size), replace=True).mean(axis=1)
    lower = float(np.percentile(draws, 100 * (1 - confidence) / 2))
    upper = float(np.percentile(draws, 100 * (1 + confidence) / 2))
    return (lower, upper)


def holm_adjust(pvalues: Sequence[float]) -> List[float]:
    """Holm-Bonferroni. Three post-hoc pairs on the same participants is
    three chances at the same claim, so they are corrected."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    for rank, index in enumerate(order):
        value = (len(pvalues) - rank) * pvalues[index]
        running = max(running, min(1.0, value))
        adjusted[index] = running
    return adjusted


def describe(values: Sequence[float]) -> dict:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"n": 0}
    return {
        "n": len(clean),
        "mean": statistics.fmean(clean),
        "median": statistics.median(clean),
        "sd": statistics.stdev(clean) if len(clean) > 1 else 0.0,
        "min": min(clean),
        "max": max(clean),
    }


def friedman_probes(trials: Sequence[dict], metric: str) -> dict:
    """Does `metric` change across Probe 1/2/3?

    Friedman rather than a repeated-measures ANOVA by default: finger
    accuracy is a bounded proportion that may sit near ceiling, which is
    exactly where the ANOVA's assumptions fail and its p-value stops
    meaning anything. Reported with Kendall's W, and followed by
    Holm-corrected paired Wilcoxons.
    """
    participants, rows = complete_probe_rows(trials, metric)
    label = METRICS.get(metric, (metric, False))[0]
    out = {
        "metric": metric,
        "label": label,
        "participants": participants,
        "n": len(participants),
        "per_probe": {
            PROBE_LABELS[j]: describe([row[j] for row in rows]) for j in range(len(PROBE_LABELS))
        },
        "test": "Friedman",
        "ran": False,
    }
    if len(participants) < MIN_TEST_N:
        out["note"] = (
            f"only {len(participants)} participant(s) have all three probes for this measure; "
            f"no test run below n={MIN_TEST_N}."
        )
        return out

    stats = _scipy_stats()
    columns = [[row[j] for row in rows] for j in range(len(PROBE_LABELS))]
    # Every participant identical across probes leaves the statistic
    # undefined; that is a ceiling result, not an error.
    if all(len(set(row)) == 1 for row in rows):
        out["note"] = "every participant scored identically at all three probes; Friedman is undefined."
        return out

    statistic, pvalue = stats.friedmanchisquare(*columns)
    out.update(
        {
            "ran": True,
            "statistic": float(statistic),
            "p": float(pvalue),
            "kendalls_w": kendalls_w(rows),
            "posthoc": _posthoc_pairs(rows),
        }
    )
    return out


def _posthoc_pairs(rows: Sequence[Sequence[float]]) -> List[dict]:
    stats = _scipy_stats()
    pairs = [(0, 1), (1, 2), (0, 2)]
    results = []
    for a, b in pairs:
        differences = [row[b] - row[a] for row in rows]
        entry = {
            "comparison": f"{PROBE_LABELS[a]} vs {PROBE_LABELS[b]}",
            "mean_difference": statistics.fmean(differences),
            "median_difference": statistics.median(differences),
            "effect_size_rank_biserial": rank_biserial(differences),
        }
        low, high = bootstrap_ci(differences)
        entry["ci95_mean_difference"] = [low, high]
        if any(d != 0 for d in differences):
            statistic, pvalue = stats.wilcoxon(differences)
            entry["statistic"], entry["p"] = float(statistic), float(pvalue)
        else:
            entry["statistic"], entry["p"] = None, 1.0
            entry["note"] = "no participant differed between these two probes."
        results.append(entry)
    adjusted = holm_adjust([r["p"] for r in results])
    for entry, value in zip(results, adjusted):
        entry["p_holm"] = value
    return results


def final_vs_probe3(trials: Sequence[dict], metric: str) -> dict:
    """The final unguided test against the last probe.

    Explicitly NOT "Probe 4". The probe removes one channel (haptic);
    the final test removes two (haptic AND backlight), so a difference
    here is a transfer effect, not another point on the withdrawal
    curve. Reported as a paired comparison but labelled as such.
    """
    probes = probe_table(trials, metric)
    finals = {
        row["participant"]: row.get(metric)
        for row in trials
        if row["phase"] == PHASE_FINAL
    }
    paired = [
        (name, probes[name][2], finals[name])
        for name in sorted(set(probes) & set(finals))
        if probes.get(name) and probes[name][2] is not None and finals[name] is not None
    ]
    out = {
        "metric": metric,
        "label": METRICS.get(metric, (metric, False))[0],
        "comparison": "Probe 3 vs Final (transfer: backlight also removed)",
        "n": len(paired),
        "probe3": describe([p for _, p, _ in paired]),
        "final": describe([f for _, _, f in paired]),
        "ran": False,
    }
    if len(paired) < MIN_TEST_N:
        out["note"] = f"n={len(paired)}; no test run below n={MIN_TEST_N}."
        return out
    differences = [f - p for _, p, f in paired]
    out["mean_difference"] = statistics.fmean(differences)
    low, high = bootstrap_ci(differences)
    out["ci95_mean_difference"] = [low, high]
    out["effect_size_rank_biserial"] = rank_biserial(differences)
    if any(d != 0 for d in differences):
        stats = _scipy_stats()
        statistic, pvalue = stats.wilcoxon(differences)
        out.update({"ran": True, "statistic": float(statistic), "p": float(pvalue)})
    else:
        out["note"] = "no participant differed between Probe 3 and the final test."
    return out


# ---------------------------------------------------------------------------
# Haptic withdrawal cost
# ---------------------------------------------------------------------------

#: (training repetition, probe number) - the last training trial before
#: each probe, and the probe that follows it.
WITHDRAWAL_PAIRS = ((5, 1), (10, 2), (15, 3))

#: Measures that survive the training -> probe comparison. Timing does
#: NOT: training yields a reaction time and a probe an onset error (see
#: the module docstring), so subtracting one from the other would be
#: arithmetic on two different quantities.
WITHDRAWAL_METRICS = ("finger_accuracy", "key_accuracy")

WITHDRAWAL_TIMING_NOTE = (
    "No timing withdrawal cost is reported. Training is a cue/response task and yields a "
    "reaction time; a probe is a performance against the melody's grid and yields an onset "
    "error. They measure different things, so their difference is not a cost."
)


def withdrawal_costs(trials: Sequence[dict], metric: str = "finger_accuracy") -> dict:
    """probe accuracy - preceding training accuracy, per participant.

    A cost that shrinks from repetition 5 to 10 to 15 is the signature
    of retention: the participant is depending less on the haptic cue.
    """
    if metric not in WITHDRAWAL_METRICS:
        raise RhythmAnalysisError(
            f"'{metric}' cannot be compared across training and probe. {WITHDRAWAL_TIMING_NOTE}"
        )
    by_key = {(r["participant"], r["phase"], r["phase_number"]): r.get(metric) for r in trials}
    participants = sorted({r["participant"] for r in trials})

    costs: Dict[str, List[Optional[float]]] = {}
    for participant in participants:
        row: List[Optional[float]] = []
        for training_rep, probe_number in WITHDRAWAL_PAIRS:
            before = by_key.get((participant, PHASE_TRAINING, training_rep))
            after = by_key.get((participant, PHASE_PROBE, probe_number))
            row.append(after - before if (before is not None and after is not None) else None)
        costs[participant] = row
    complete = [row for row in costs.values() if all(v is not None for v in row)]
    return {
        "metric": metric,
        "label": METRICS.get(metric, (metric, False))[0],
        "pairs": [f"Training {t} -> Probe {p}" for t, p in WITHDRAWAL_PAIRS],
        "per_participant": costs,
        "n_complete": len(complete),
        "per_pair": [
            describe([row[j] for row in complete]) for j in range(len(WITHDRAWAL_PAIRS))
        ],
        "timing_note": WITHDRAWAL_TIMING_NOTE,
    }


def training_curve(trials: Sequence[dict], metric: str) -> Dict[str, List[Optional[float]]]:
    """participant -> value at each training repetition 1..15."""
    curve: Dict[str, List[Optional[float]]] = {}
    for row in trials:
        if row["phase"] != PHASE_TRAINING:
            continue
        slot = row["phase_number"] - 1
        if slot < 0:
            continue
        series = curve.setdefault(row["participant"], [None] * 15)
        if slot < len(series):
            series[slot] = row.get(metric)
    return curve


# ---------------------------------------------------------------------------
# The whole analysis, in one call
# ---------------------------------------------------------------------------


@dataclass
class AnalysisResult:
    config: AnalysisConfig
    participants: List[str]
    events: List[dict]
    trials: List[dict]
    probe_tests: Dict[str, dict]
    final_tests: Dict[str, dict]
    withdrawal: Dict[str, dict]
    coverage: dict


#: The two headline measures, in the order the report leads with them.
PRIMARY_METRICS = ("finger_accuracy", "mean_absolute_onset_error_ms")
SECONDARY_METRICS = (
    "key_accuracy",
    "mean_signed_onset_error_ms",
    "mean_absolute_duration_error_ms",
    "complete_success_rate",
)


def analyse(participants: Sequence[str], cfg: Optional[AnalysisConfig] = None) -> AnalysisResult:
    cfg = cfg or AnalysisConfig()
    events = collect_group(participants, cfg)
    if not events:
        raise RhythmAnalysisError(
            "No completed trials found for the selected participants."
        )
    trials = trial_summaries(events)
    metrics = PRIMARY_METRICS + SECONDARY_METRICS
    return AnalysisResult(
        config=cfg,
        participants=list(participants),
        events=events,
        trials=trials,
        probe_tests={m: friedman_probes(trials, m) for m in metrics},
        final_tests={m: final_vs_probe3(trials, m) for m in metrics},
        withdrawal={m: withdrawal_costs(trials, m) for m in WITHDRAWAL_METRICS},
        coverage=_coverage(trials),
    )


def _coverage(trials: Sequence[dict]) -> dict:
    by_phase: Dict[str, int] = {}
    for row in trials:
        by_phase[row["phase"]] = by_phase.get(row["phase"], 0) + 1
    return {
        "participants": sorted({r["participant"] for r in trials}),
        "trials_by_phase": by_phase,
        "total_trials": len(trials),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

EVENT_CSV = "rhythm_events.csv"
TRIAL_CSV = "rhythm_trials.csv"
PARTICIPANT_CSV = "rhythm_participants.csv"
STATS_JSON = "rhythm_stats.json"
SUMMARY_TXT = "rhythm_summary.txt"


def participant_summaries(trials: Sequence[dict]) -> List[dict]:
    """One row per participant per phase-occurrence - the shape the
    statistics consume and the one a reader can scan."""
    rows = []
    for row in sorted(trials, key=lambda r: (r["participant"], r["trial_index"])):
        entry = {
            "participant": row["participant"],
            "phase": row["phase"],
            "phase_number": row["phase_number"],
            "trial_index": row["trial_index"],
        }
        for metric in METRICS:
            entry[metric] = row.get(metric)
        rows.append(entry)
    return rows


def _write_csv(path, rows: Sequence[dict]) -> None:
    import csv  # noqa: PLC0415

    from pathlib import Path as _Path  # noqa: PLC0415

    path = _Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export(result: AnalysisResult, out_dir) -> List:
    """Write every table and the statistics. Figures are separate - see
    rhythm_study.analysis_figures."""
    from pathlib import Path as _Path  # noqa: PLC0415

    out = _Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []

    _write_csv(out / EVENT_CSV, result.events)
    _write_csv(out / TRIAL_CSV, result.trials)
    _write_csv(out / PARTICIPANT_CSV, participant_summaries(result.trials))
    written += [out / EVENT_CSV, out / TRIAL_CSV, out / PARTICIPANT_CSV]

    stats_path = out / STATS_JSON
    stats_path.write_text(
        json.dumps(
            {
                "config": {
                    "onset_tolerance_ms": result.config.onset_tolerance_ms,
                    "exclude_invalid_carryover": result.config.exclude_invalid_carryover,
                },
                "coverage": result.coverage,
                "probe_tests": result.probe_tests,
                "final_vs_probe3": result.final_tests,
                "withdrawal_cost": result.withdrawal,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    written.append(stats_path)

    summary_path = out / SUMMARY_TXT
    summary_path.write_text(summary_text(result), encoding="utf-8")
    written.append(summary_path)
    return written


def _fmt(value: Optional[float], places: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def summary_text(result: AnalysisResult) -> str:
    """A readable account of what the numbers say, including what was
    not tested and why."""
    lines: List[str] = []
    add = lines.append

    coverage = result.coverage
    add("RHYTHM EXPERIMENT - ANALYSIS SUMMARY")
    add("=" * 60)
    add("")
    add(f"Participants: {len(coverage['participants'])} "
        f"({', '.join(coverage['participants']) or 'none'})")
    add(f"Completed trials: {coverage['total_trials']}  {coverage['trials_by_phase']}")
    add(f"Onset tolerance for the combined score: +/-{result.config.onset_tolerance_ms:.0f} ms")
    add("")
    add("The question: does haptic-guided practice let participants keep the intended")
    add("fingering and timing once the haptic cue is removed? The probes answer it;")
    add("training is descriptive and the final test is a transfer condition.")
    add("")

    add("PRIMARY - PROBE 1 vs 2 vs 3")
    add("-" * 60)
    for metric in PRIMARY_METRICS:
        test = result.probe_tests[metric]
        add(f"{test['label']}  (n={test['n']})")
        for label in PROBE_LABELS:
            stats = test["per_probe"].get(label, {})
            if stats.get("n"):
                add(f"    {label}:  mean {_fmt(stats['mean'])}   median {_fmt(stats['median'])}"
                    f"   sd {_fmt(stats.get('sd'))}")
        if test.get("ran"):
            add(f"    Friedman chi2={_fmt(test['statistic'], 2)}  p={_fmt(test['p'], 4)}"
                f"  Kendall's W={_fmt(test.get('kendalls_w'))}")
            for pair in test.get("posthoc", []):
                ci = pair.get("ci95_mean_difference") or [None, None]
                add(f"      {pair['comparison']}: diff {_fmt(pair['mean_difference'])}"
                    f"  95% CI [{_fmt(ci[0])}, {_fmt(ci[1])}]"
                    f"  p={_fmt(pair['p'], 4)}  p_holm={_fmt(pair['p_holm'], 4)}"
                    f"  r_rb={_fmt(pair['effect_size_rank_biserial'], 2)}")
        else:
            add(f"    not tested: {test.get('note', 'insufficient data')}")
        add("")

    add("SECONDARY - PROBE 1 vs 2 vs 3")
    add("-" * 60)
    for metric in SECONDARY_METRICS:
        test = result.probe_tests[metric]
        verdict = (f"p={_fmt(test['p'], 4)}" if test.get("ran")
                   else test.get("note", "not tested"))
        cells = "  ".join(
            f"{label} {_fmt((test['per_probe'].get(label) or {}).get('mean'))}"
            for label in PROBE_LABELS
        )
        add(f"{test['label']}: {cells}   {verdict}")
    add("")

    add("HAPTIC WITHDRAWAL COST (probe - preceding training)")
    add("-" * 60)
    for metric, cost in result.withdrawal.items():
        add(f"{cost['label']}  (n={cost['n_complete']} with all three pairs)")
        for pair_label, stats in zip(cost["pairs"], cost["per_pair"]):
            if stats.get("n"):
                add(f"    {pair_label}:  mean {_fmt(stats['mean'])}   median {_fmt(stats['median'])}")
        add("")
    add(f"    {WITHDRAWAL_TIMING_NOTE}")
    add("")

    add("FINAL UNGUIDED TEST (transfer - haptic AND backlight removed)")
    add("-" * 60)
    for metric in PRIMARY_METRICS + SECONDARY_METRICS:
        test = result.final_tests[metric]
        if not test["final"].get("n"):
            continue
        add(f"{test['label']}: Probe 3 mean {_fmt(test['probe3'].get('mean'))}"
            f"  ->  Final mean {_fmt(test['final'].get('mean'))}"
            + (f"   p={_fmt(test['p'], 4)}" if test.get("ran") else ""))
    add("")
    add("    This is a transfer comparison, not a fourth probe: the final test removes the")
    add("    backlight as well as the haptic cue, so a drop here has two possible causes.")
    add("")

    add("READING NOTE")
    add("-" * 60)
    add("    Improvement across training trials, on its own, is NOT evidence of retention:")
    add("    the haptic cue is present throughout training. Retention is what the probes")
    add("    and the withdrawal cost measure.")
    return "\n".join(lines)
