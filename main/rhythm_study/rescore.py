"""Re-derive a recorded trial's results.json from its raw MIDI log.

Two bugs made the live scoring wrong, and both are fixed in the runner
now - but the trials already on disk were recorded before that. Their raw
MIDI log is complete and untouched, so the scoring can simply be redone
from it.

WHAT WAS WRONG
==============
1. **Swallowed presses (every phase).** The quiz loop only accepted a
   press from the cue onwards, so anything played during the 0.4 s gap
   between one note and the next cue was discarded - the participant
   pressed, nothing happened, and they had to press again. Holding a
   training cue for the whole beat made that easy to hit: the natural
   moment to play the next note is the instant the buzz stops, which is
   exactly one gap before the cue. In P01's session 26 presses were lost
   this way.

2. **Positional pairing (performances).** A probe or the final test
   paired the n-th press with the n-th note. One missed or extra note
   therefore scored every later note wrong: P01's third probe scored
   5/15 when the participant had in fact played all fifteen notes.

HOW EACH IS REDONE
==================
**Only performances are re-paired.**

A probe or the final test is one performance, so its pairing is redone by
global sequence alignment (:mod:`rhythm_study.alignment`), which may
leave a note unplayed or a press unmatched and resynchronise afterwards.
That is what positional pairing could not do, and it is worth several
notes a trial.

**Training is left exactly as it was recorded**, and that is deliberate.
Training is cue/response: each note had its own response window, and the
press the loop accepted for a note really was the participant's answer to
it. A swallowed press there cost that note's *reaction time* - the
participant had to play it twice - but not its key or finger, which are
scored from the press that was accepted.

Re-pairing training on a widened window was tried and made the data
worse. Nothing distinguishes "the right note, played in the gap and
ignored" from "a slip, immediately corrected" except the note itself,
and crediting the first press in the window credits the slip: in P01's
seventh training trial it took an A#3 played 0.4 s before the cue as the
answer to a B3 the participant went on to play correctly, and every note
after it shifted by one. The live loop's rule - the first press at or
after the cue - was right for this task shape. Training trials are only
annotated here, never rewritten.

WHAT IS AND IS NOT PRESERVED
============================
`raw/midi_raw.json` is never written - it is the source. The original
results.json is copied to `results_before_realign.json` the first time a
trial is rescored, so nothing is lost and the change can be inspected.

Cue times are kept exactly as recorded. A press recovered from the gap is
marked `recovered_early` in the sidecar: its key and finger are sound,
but its reaction time is measured against a cue that the bug delayed, so
it is not comparable with an ordinary one and the analysis should leave
it out of timing.
"""

import json
import shutil
from typing import Dict, List

from app.quiz import (
    META_FILENAME,
    RAW_MIDI_FILENAME,
    RESULTS_FILENAME,
    QuizMeta,
    quiz_dir,
    quiz_raw_dir,
)

from .alignment import match_targets
from .runner_window import RECUE_SIDECAR_FILENAME
from .schedule import PHASE_TRAINING, load_trial_melody, load_trial_structure

BACKUP_FILENAME = "results_before_realign.json"


def _presses(quiz_name: str) -> List[dict]:
    path = quiz_raw_dir(quiz_name) / RAW_MIDI_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    events = raw if isinstance(raw, list) else raw.get("events", [])
    return sorted(
        (e for e in events if e.get("type") == "note_on"),
        key=lambda e: e["abs_time"],
    )


def _sidecar(quiz_name: str) -> dict:
    path = quiz_dir(quiz_name) / RECUE_SIDECAR_FILENAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def rescore_trial(quiz_name: str, melody_name: str, phase: str) -> dict:
    """Rewrite one trial's results.json. Returns what changed."""
    folder = quiz_dir(quiz_name)
    results = json.loads((folder / RESULTS_FILENAME).read_text(encoding="utf-8"))
    presses = _presses(quiz_name)
    melody = load_trial_melody(melody_name)

    before = sum(1 for r in results if r.get("note_correct"))

    if phase == PHASE_TRAINING:
        # Annotated, not re-paired - see the module docstring.
        swallowed = len(presses) - sum(1 for r in results if r.get("keypress_time"))
        _annotate_training(quiz_name, swallowed)
        return {
            "quiz": quiz_name,
            "phase": phase,
            "before": before,
            "after": before,
            "notes": len(results),
            "rewritten": False,
            "swallowed_presses": swallowed,
            "extra_presses": swallowed,
        }

    answered, extra = match_targets(
        [r["target_note"] for r in results], [p["note"] for p in presses]
    )
    for i, result in enumerate(results):
        j = answered[i]
        press = presses[j] if j is not None else None
        result["actual_note"] = press["note"] if press else None
        result["keypress_time"] = press["abs_time"] if press else None
        result["timed_out"] = press is None
        result["timing_error_s"] = (
            press["abs_time"] - result["cue_onset_time"] if press else None
        )
        result["note_correct"] = bool(press and press["note"] == result["target_note"])
        if press is None:
            # A note that was never played has no finger either; leaving a
            # stale verdict would credit a note the participant skipped.
            result["actual_note"] = None
            result["actual_key_id"] = None
            result["note_correct"] = False

    backup = folder / BACKUP_FILENAME
    if not backup.exists():
        shutil.copy2(folder / RESULTS_FILENAME, backup)
    (folder / RESULTS_FILENAME).write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    after = sum(1 for r in results if r["note_correct"])
    _refresh_meta(quiz_name, results)
    _mark_sidecar(quiz_name, extra)
    return {
        "quiz": quiz_name,
        "phase": phase,
        "before": before,
        "after": after,
        "notes": len(results),
        "rewritten": True,
        "swallowed_presses": 0,
        "extra_presses": len(extra),
        "melody_notes": len(melody.notes),
    }


def _refresh_meta(quiz_name: str, results: List[dict]) -> None:
    """meta.json caches the headline counts; leaving them stale would make
    the quiz list disagree with the file it lists."""
    path = quiz_dir(quiz_name) / META_FILENAME
    meta = QuizMeta.load(path)
    hits = sum(1 for r in results if r["note_correct"])
    meta.hits = hits
    meta.misses = len(results) - hits
    meta.note_accuracy = hits / len(results) if results else 0.0
    meta.save(path)


def _mark_sidecar(quiz_name: str, extra: List[int]) -> None:
    path = quiz_dir(quiz_name) / RECUE_SIDECAR_FILENAME
    if not path.exists():
        return
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["pairing"] = "sequence_alignment"
    doc["extra_press_count"] = len(extra)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def _annotate_training(quiz_name: str, swallowed: int) -> None:
    """Record how many presses the live loop discarded, without touching
    the scoring. Their key and finger are sound - the participant played
    the note again - but the note's reaction time includes a wasted
    attempt, so timing from a trial with swallowed presses is not
    comparable with one without."""
    path = quiz_dir(quiz_name) / RECUE_SIDECAR_FILENAME
    if not path.exists():
        return
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["pairing"] = "as_recorded"
    doc["swallowed_presses"] = swallowed
    doc["timing_reliable"] = swallowed == 0
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def rescore_participant(participant: str) -> List[dict]:
    """Rescore every completed trial of one participant, in trial order."""
    doc = load_trial_structure(participant)
    out = []
    for trial in doc["trials"]:
        quiz_name = trial.get("quiz_name")
        if not quiz_name or not (quiz_dir(quiz_name) / RESULTS_FILENAME).exists():
            continue
        out.append(rescore_trial(quiz_name, trial["melody"], trial["phase"]))
    return out


def summarise(rows: List[Dict]) -> str:
    lines = [
        f"{'trial':<20}{'phase':<10}{'key accuracy':>18}{'extra presses':>15}",
        "-" * 65,
    ]
    for row in rows:
        if row["rewritten"]:
            change = f"{row['before']}/{row['notes']} -> {row['after']}/{row['notes']}"
        else:
            change = f"{row['before']}/{row['notes']} (kept)"
        lines.append(
            f"{row['quiz']:<20}{row['phase']:<10}{change:>18}{row['extra_presses']:>15}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys

    for name in sys.argv[1:] or [""]:
        if not name:
            raise SystemExit("usage: python -m rhythm_study.rescore <participant> ...")
        print(f"\n{name}")
        print(summarise(rescore_participant(name)))
