"""One-click export of a Main User Study participant's data for offline
analysis - triggered from the Quiz Analysis window.

Writes two flat CSVs next to the participant's TrialStructure.json
(data/MainUserStudy/<participant>/), ready for pandas/R:

  <participant>_trials.csv - one row per completed trial: the schedule
      factors (condition, level, sequence, presentation position) joined
      with every per-trial outcome from app.quiz.summarize() (the three
      finger-accuracy views, RT statistics and trend, error breakdown,
      detection quality, hand-transition and per-finger profiles,
      threshold sensitivity) plus the sync-alignment audit state.

  <participant>_events.csv - one row per cue event (27 x 30 for a full
      session): targets, responses, verdicts, reaction time, the full
      stored finger-probability distribution (p_L1..p_R5, kept so
      threshold re-scoring stays possible), and whether the finger was
      manually corrected.

Absolute wall-clock timestamps are deliberately NOT exported - after the
sync-anchored analysis has run, later statistics only need reaction
times and verdicts, not when events happened. Participant demographics
already live in TrialStructure.json in the same folder, so they are not
duplicated.
"""

import csv
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .pilot_study import DATA_DIR as STUDY_DATA_DIR
from .quiz import (
    FINGER_LABELS,
    META_FILENAME,
    QUIZ_DATA_DIR,
    RESULTS_FILENAME,
    SENSITIVITY_THRESHOLDS,
    QuizMeta,
    finger_manually_corrected,
    full_summary,
    load_quiz_results,
    quiz_dir,
    quiz_raw_dir,
    suspected_carryover,
)
from .sync_led import load_sync_alignment

TRIAL_STRUCTURE_FILENAME = "TrialStructure.json"


def export_paths(participant: str) -> List[Path]:
    out = STUDY_DATA_DIR / participant
    return [out / f"{participant}_trials.csv", out / f"{participant}_events.csv"]


def _resolve_quiz_dir(trial: dict) -> Optional[str]:
    """The quiz folder actually holding this trial's data. The disk is
    the source of truth, not TrialStructure's recorded attempt name: quiz
    folders get curated by hand (bad retakes deleted, names promoted), so
    take the trial's base name (its -rN suffix stripped) and pick the
    HIGHEST-numbered existing retake folder - base-rN over base-r2 over
    the plain base name."""
    base = trial.get("quiz_name") or (trial.get("quiz_attempts") or [""])[0]
    base = re.sub(r"-r\d+$", "", base)
    if not base or not QUIZ_DATA_DIR.exists():
        return None
    retake = re.compile(re.escape(base) + r"-r(\d+)$")
    best: Optional[tuple] = None
    for p in QUIZ_DATA_DIR.iterdir():
        if not (p / META_FILENAME).exists():
            continue
        if p.name == base:
            rank = 0
        else:
            m = retake.match(p.name)
            if m is None:
                continue
            rank = int(m.group(1))
        if best is None or rank > best[0]:
            best = (rank, p.name)
    return best[1] if best else None


def _trial_row(participant: str, trial: dict, quiz_name: str, meta: QuizMeta, s: dict) -> Dict[str, object]:
    align = load_sync_alignment(quiz_raw_dir(quiz_name))
    row: Dict[str, object] = {
        "participant": participant,
        "trial_index": trial["index"],
        "condition": trial["condition"],
        "condition_label": trial.get("condition_label"),
        "level": trial["level"],
        "level_symbol": trial.get("level_symbol"),
        "sequence": trial["sequence"],
        "quiz_name": quiz_name,
        "guidance_type": meta.guidance_type,
        "note_count": meta.note_count,
        "analyzed": meta.analyzed,
        "sync_method": align.method if align else "none",
        "sync_offset_s": align.led_vs_start_times_offset_s if align else None,
        # Headline accuracy
        "key_accuracy": s["note_accuracy"],
        "hits": s["hits"],
        "misses": s["misses"],
        "fa_main": s["fa_main"],
        "fa_given_key": s["fa_key"],
        "key_acc_given_finger": s["fa_finger"],
        # Reaction time
        "mean_rt_s": s["mean_timing_error_s"],
        "rt_correct_key_s": s["rt_correct_key_s"],
        "rt_complete_s": s["rt_complete_s"],
        "median_rt_s": s["timing_stats"]["median_s"],
        "sd_rt_s": s["timing_stats"]["sd_s"],
        "min_rt_s": s["timing_stats"]["min_s"],
        "max_rt_s": s["timing_stats"]["max_s"],
        "p95_rt_s": s["timing_stats"]["p95_s"],
        "rt_slope_s_per_event": s["rt_slope_s_per_event"],
        "first_half_fa": s["first_half"]["fa_main"],
        "first_half_rt_s": s["first_half"]["mean_timing_error_s"],
        "second_half_fa": s["second_half"]["fa_main"],
        "second_half_rt_s": s["second_half"]["mean_timing_error_s"],
        # Errors
        "timeout_rate": s["timeout_rate"],
        "wrong_key": s["wrong_key"],
        "key_ok_wrong_finger": s["key_ok_wrong_finger"],
        "wrong_key_mean_semitones": s["wrong_key_stats"]["mean_abs_semitones"],
        "wrong_key_below": s["wrong_key_stats"]["below"],
        "wrong_key_above": s["wrong_key_stats"]["above"],
        # Detection quality / audit
        "unresolved_rate": s["unresolved_rate"],
        "ambiguous_rate": s["ambiguous_rate"],
        "mean_target_prob": s["confidence"]["mean_target_prob"],
        "mean_top_margin": s["confidence"]["mean_top_margin"],
        "borderline_events": s["confidence"]["borderline"],
        "manual_corrections": s["manual_corrections"],
        # Carry-over review state (see app.quiz.suspected_carryover):
        # suspected = matched RT < 100 ms awaiting a manual verdict;
        # excluded = manually confirmed carry-over, already removed from
        # every statistic in this row.
        "suspected_carryover": s["suspected_carryover"],
        "excluded_carryover": s["excluded_carryover"],
        # QC/debug only - unmatched raw presses (double-hits + inter-trial
        # strays). NOT false starts/anticipation; keep out of main plots.
        "qc_extra_presses": s["extra"]["extra_presses"] if s.get("extra") else None,
        "qc_double_hits": s["extra"]["double_hits"] if s.get("extra") else None,
        "qc_inter_trial_presses": s["extra"]["inter_trial_presses"] if s.get("extra") else None,
        # Hand transitions
        "same_hand_n": s["same_hand"]["n"],
        "same_hand_fa": s["same_hand"]["fa_main"],
        "same_hand_rt_s": s["same_hand"]["mean_timing_error_s"],
        "hand_switch_n": s["hand_switch"]["n"],
        "hand_switch_fa": s["hand_switch"]["fa_main"],
        "hand_switch_rt_s": s["hand_switch"]["mean_timing_error_s"],
    }
    for f in FINGER_LABELS:
        row[f"fa_{f}"] = s["finger_stats"][f]["fa_main"]
        row[f"te_{f}_s"] = s["finger_stats"][f]["mean_timing_error_s"]
        row[f"n_{f}"] = s["finger_stats"][f]["n"]
    for theta in SENSITIVITY_THRESHOLDS:
        key = f"{theta:.2f}"
        row[f"fa_theta_{key}"] = s["fa_theta"][key]
    return row


def _event_rows(participant: str, trial: dict, quiz_name: str, results) -> List[Dict[str, object]]:
    rows = []
    for r in results:
        row: Dict[str, object] = {
            "participant": participant,
            "trial_index": trial["index"],
            "condition": trial["condition"],
            "level": trial["level"],
            "sequence": trial["sequence"],
            "quiz_name": quiz_name,
            "event_index": r.index,
            "target_note": r.target_note,
            "target_note_name": r.target_note_name,
            # Calibrated key indices (keyboard-profile order) - the honest
            # "how many keys away" measure on a white-key-only layout,
            # where MIDI semitone distance would overcount across E-F/B-C.
            "target_key_id": r.target_key_id,
            "actual_key_id": r.actual_key_id,
            "target_finger": r.target_finger,
            "target_hand": r.target_finger[0] if r.target_finger else None,
            "timed_out": r.timed_out,
            "actual_note": r.actual_note,
            "key_correct": r.note_correct,
            "actual_finger": r.actual_finger,
            "finger_correct": r.finger_correct,
            "target_finger_probability": r.target_finger_probability,
            "manually_corrected": (not r.timed_out) and finger_manually_corrected(r),
            "rt_s": r.timing_error_s,
            # Carry-over audit trail: invalid events are excluded from the
            # trial-level statistics but still exported here.
            "validity": r.validity,
            "suspected_carryover": suspected_carryover(r),
        }
        probs = r.finger_probabilities or {}
        for f in FINGER_LABELS:
            row[f"p_{f}"] = probs.get(f)
        rows.append(row)
    return rows


def collect_participant_data(participant: str):
    """All of a participant's cross-trial data in memory: (trial_rows,
    event_rows, missing). The same rows the CSV export writes - also used
    directly by the participant analysis window, so the two can never
    disagree."""
    structure_path = STUDY_DATA_DIR / participant / TRIAL_STRUCTURE_FILENAME
    with open(structure_path) as f:
        structure = json.load(f)

    trial_rows: List[Dict[str, object]] = []
    event_rows: List[Dict[str, object]] = []
    missing: List[str] = []
    for trial in structure.get("trials", []):
        quiz_name = _resolve_quiz_dir(trial)
        if quiz_name is None:
            missing.append(f"T{trial['index']:02d} ({trial.get('quiz_name') or 'no quiz recorded'})")
            continue
        meta = QuizMeta.load(quiz_dir(quiz_name) / META_FILENAME)
        results = load_quiz_results(quiz_dir(quiz_name) / RESULTS_FILENAME)
        summary = full_summary(quiz_name, results)
        trial_rows.append(_trial_row(participant, trial, quiz_name, meta, summary))
        event_rows.extend(_event_rows(participant, trial, quiz_name, results))
    return trial_rows, event_rows, missing


def export_participant(participant: str) -> Dict[str, object]:
    """Writes both CSVs (overwriting silently - the GUI asks first) and
    returns {trials, events, missing, paths}."""
    trial_rows, event_rows, missing = collect_participant_data(participant)

    trials_path, events_path = export_paths(participant)
    for path, rows in ((trials_path, trial_rows), (events_path, event_rows)):
        with open(path, "w", newline="") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
    return {
        "trials": len(trial_rows),
        "events": len(event_rows),
        "missing": missing,
        "paths": [trials_path, events_path],
    }
