"""Rhythm experiment: the participant trial schedule.

The GUI-free half, copied from ``app/pilot_study.py`` and cut down to
this study's design. The schedule is *fixed*, not sampled:

    Training x5 -> Probe 1 -> Training x5 -> Probe 2 -> Training x5 -> Probe 3 -> Final test

19 trials, all on the one melody the participant is locked to when their
schedule is made. Because the order is fixed there is no shuffle, no
seed to record and nothing to reproduce - the whole of build_schedule()
is a deterministic walk over PHASE_PLAN.

What varies across trials is the guidance available, not the stimulus:
training has backlight + haptic, a probe drops the haptic, and the final
test drops both so the participant plays the melody from memory. Those
are the ``backlight``/``haptic`` booleans on every trial row; the runner
reads them rather than re-deriving anything from the phase name.

The document is saved to
data/RhythmStudy/<participant>/TrialStructure.json, and every trial row
carries its own status/timestamps so the session can crash at any point
and resume from next_pending_trial() - the file on disk is always the
source of truth for how far the session got. See
rhythm_study/schedule_window.py for the Qt wrapper.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from melody_generator.load import MelodyLoadError, list_melodies, load_melody

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "RhythmStudy"
MELODY_DIR = Path(__file__).resolve().parent.parent / "data" / "rhythm_experiment"
TRIAL_STRUCTURE_FILENAME = "TrialStructure.json"
SCHEMA_VERSION = 1

# Quiz-folder prefix for every recording this study makes. The trials are
# saved into the shared data/quiz/ folder so the existing analysis tools
# work on them unchanged; this prefix is what keeps them identifiable
# (and sortable) next to the main study's quizzes.
QUIZ_NAME_PREFIX = "rhythm"

PHASE_TRAINING = "training"
PHASE_PROBE = "probe"
PHASE_FINAL = "final"

PHASE_LABEL = {
    PHASE_TRAINING: "Training (backlight + haptic)",
    PHASE_PROBE: "Probe (backlight only)",
    PHASE_FINAL: "Final test (no guidance)",
}

# Which guidance channels each phase has. The single source of truth for
# what the runner turns on - see rhythm_study/cue.py and runner_window.py.
PHASE_GUIDANCE = {
    PHASE_TRAINING: {"backlight": True, "haptic": True},
    PHASE_PROBE: {"backlight": True, "haptic": False},
    PHASE_FINAL: {"backlight": False, "haptic": False},
}

TRAINING_BLOCK_SIZE = 5  # training trials between consecutive probes
PROBE_COUNT = 3
TRAINING_TRIALS = TRAINING_BLOCK_SIZE * PROBE_COUNT  # 15

# The schedule, written out once so it can be read rather than inferred:
# five training trials, a probe, twice more, then the final test.
PHASE_PLAN: List[str] = (
    ([PHASE_TRAINING] * TRAINING_BLOCK_SIZE + [PHASE_PROBE]) * PROBE_COUNT + [PHASE_FINAL]
)
TOTAL_TRIALS = len(PHASE_PLAN)  # = 19

# A rest is offered after each probe - the natural seam in the design,
# and the only place the participant is not mid-way through a training
# block. Unlike the main study's fixed 2-min rests these are untimed:
# the session controller just pauses until the experimenter continues.
REST_AFTER_PHASES = (PHASE_PROBE,)

# Participant metadata options - same as the main study's, so the two
# studies' demographics stay directly comparable.
SEX_OPTIONS = ("male", "female", "prefer not to say")
HANDEDNESS_OPTIONS = ("right", "left", "ambidextrous")

TRIAL_STATUS_PENDING = "pending"
TRIAL_STATUS_IN_PROGRESS = "in_progress"
TRIAL_STATUS_COMPLETED = "completed"


class RhythmStudyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Discovering melodies under data/rhythm_experiment/
# ---------------------------------------------------------------------------


def discover_melodies(melody_dir: Path = MELODY_DIR) -> List[str]:
    """Every melody stem under data/rhythm_experiment/ that can actually
    be loaded. A melody is a set of files sharing one stem;
    melody_generator.load.list_melodies returns the authoritative one per
    stem (the .json where there is one), so this is just its names."""
    folder = Path(melody_dir)
    if not folder.exists():
        return []
    return sorted(path.stem for path in list_melodies(folder))


def melody_summary(name: str, melody_dir: Path = MELODY_DIR) -> Dict[str, object]:
    """Enough of a melody to show in the picker and to freeze into the
    participant's file: how many notes they will be playing, which
    fingers, and the key it sits in."""
    try:
        melody = load_melody(_melody_path(name, melody_dir))
    except (MelodyLoadError, OSError) as exc:
        raise RhythmStudyError(f"Couldn't read melody '{name}': {exc}") from exc
    return {
        "name": name,
        "note_count": len(melody.notes),
        "fingers_used": list(melody.fingers_used),
        "key_display": melody.key_display,
        "layout": melody.layout,
        "bpm": melody.bpm,
        "duration_s": melody.total_seconds,
        "seed": melody.seed,
    }


def _melody_path(name: str, melody_dir: Path = MELODY_DIR) -> Path:
    """The file load_melody() should be pointed at for a stem - the
    .json when it exists (it carries the fingering), else the .mid."""
    folder = Path(melody_dir)
    for path in list_melodies(folder):
        if path.stem == name:
            return path
    raise RhythmStudyError(f"Melody '{name}' was not found under {folder}.")


def load_trial_melody(name: str, melody_dir: Path = MELODY_DIR):
    """The melody a trial runs on, as a melody_generator LoadedMelody."""
    try:
        return load_melody(_melody_path(name, melody_dir))
    except (MelodyLoadError, OSError) as exc:
        raise RhythmStudyError(f"Couldn't read melody '{name}': {exc}") from exc


# ---------------------------------------------------------------------------
# Building a participant's schedule
# ---------------------------------------------------------------------------


def build_schedule(melody: str) -> List[dict]:
    """The fixed 19-trial schedule. Every trial runs the same melody; the
    phase decides which guidance channels it gets, and the per-phase
    counter is carried on the row so the UI can say "Training 3/5" or
    "Probe 2" without recounting."""
    trials: List[dict] = []
    phase_counts: Dict[str, int] = {}
    for index, phase in enumerate(PHASE_PLAN, start=1):
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        guidance = PHASE_GUIDANCE[phase]
        trials.append(
            {
                "index": index,
                "phase": phase,
                "phase_label": PHASE_LABEL[phase],
                "phase_number": phase_counts[phase],
                "melody": melody,
                "backlight": guidance["backlight"],
                "haptic": guidance["haptic"],
                "rest_after": phase in REST_AFTER_PHASES and index != len(PHASE_PLAN),
                "status": TRIAL_STATUS_PENDING,
                "started_at": None,
                "completed_at": None,
            }
        )
    return trials


def new_trial_structure(
    participant: dict,
    melody: str,
    keyboard_profile: str,
    melody_dir: Path = MELODY_DIR,
) -> dict:
    """A complete TrialStructure.json document. The melody is locked in
    here, and a snapshot of it is stored alongside the name so the file
    still says what was played even if data/rhythm_experiment/ is later
    regenerated or cleaned out."""
    if not melody:
        raise RhythmStudyError("Pick a melody before generating the schedule.")
    doc = {
        "schema_version": SCHEMA_VERSION,
        "study": "rhythm",
        "created_at": time.time(),
        "participant": dict(participant),
        "keyboard_profile": keyboard_profile,
        "melody": melody,
        "melody_summary": melody_summary(melody, melody_dir),
        "phases": dict(PHASE_LABEL),
        "phase_guidance": {phase: dict(g) for phase, g in PHASE_GUIDANCE.items()},
        "quiz_name_prefix": QUIZ_NAME_PREFIX,
        "trials": build_schedule(melody),
    }
    recompute_progress(doc)
    return doc


# ---------------------------------------------------------------------------
# Progress tracking (crash-resume support for the session controller)
# ---------------------------------------------------------------------------


def recompute_progress(doc: dict) -> None:
    """Derive the progress block from the per-trial statuses - called on
    every save so the file's summary can never drift from its trials."""
    trials = doc["trials"]
    completed = sum(1 for t in trials if t["status"] == TRIAL_STATUS_COMPLETED)
    pending = next((t for t in trials if t["status"] != TRIAL_STATUS_COMPLETED), None)
    doc["progress"] = {
        "completed": completed,
        "total": len(trials),
        "next_trial_index": pending["index"] if pending else None,
        "finished": pending is None,
    }


def next_pending_trial(doc: dict) -> Optional[dict]:
    """The trial the session should (re)start from: the first one not yet
    completed - an in_progress trial (a crash mid-trial) is returned
    again, so it gets rerun rather than silently skipped."""
    return next((t for t in doc["trials"] if t["status"] != TRIAL_STATUS_COMPLETED), None)


def _find_trial(doc: dict, index: int) -> dict:
    for trial in doc["trials"]:
        if trial["index"] == index:
            return trial
    raise RhythmStudyError(f"No trial with index {index} (1..{len(doc['trials'])}).")


def mark_trial_started(doc: dict, index: int) -> None:
    trial = _find_trial(doc, index)
    trial["status"] = TRIAL_STATUS_IN_PROGRESS
    trial["started_at"] = time.time()
    recompute_progress(doc)


def mark_trial_completed(doc: dict, index: int) -> None:
    trial = _find_trial(doc, index)
    trial["status"] = TRIAL_STATUS_COMPLETED
    trial["completed_at"] = time.time()
    recompute_progress(doc)


# ---------------------------------------------------------------------------
# Quiz naming
# ---------------------------------------------------------------------------


def trial_quiz_base_name(participant: str, index: int) -> str:
    """The data/quiz/ folder name for one trial, e.g.
    "rhythm-P01-T01". The prefix is what separates this study's
    recordings from the main study's in that shared folder, so it is
    added here rather than left to each caller."""
    return f"{QUIZ_NAME_PREFIX}-{participant}-T{index:02d}"


# ---------------------------------------------------------------------------
# Saving / loading
# ---------------------------------------------------------------------------


def _sanitize(name: str) -> str:
    """Folder-safe participant name. Deliberately a local copy of the
    rule rather than app.music_recording.sanitize_song_name: this study
    must not start depending on the main study's naming ever changing."""
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in name.strip())
    return safe or "unnamed"


def participant_dir(participant_name: str, data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / _sanitize(participant_name)


def trial_structure_path(participant_name: str, data_dir: Path = DATA_DIR) -> Path:
    return participant_dir(participant_name, data_dir) / TRIAL_STRUCTURE_FILENAME


def save_trial_structure(doc: dict, data_dir: Path = DATA_DIR) -> Path:
    """Write (or rewrite) the participant's TrialStructure.json. The
    session controller calls this after every status change, so the file
    on disk always reflects the latest progress."""
    recompute_progress(doc)
    path = trial_structure_path(doc["participant"]["name"], data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_trial_structure(participant_name: str, data_dir: Path = DATA_DIR) -> dict:
    path = trial_structure_path(participant_name, data_dir)
    doc = json.loads(path.read_text(encoding="utf-8"))
    recompute_progress(doc)  # tolerate a file whose summary drifted (hand edits)
    return doc


def list_participants(data_dir: Path = DATA_DIR) -> List[str]:
    """Every participant folder under data/RhythmStudy/ that already
    holds a TrialStructure.json."""
    root = Path(data_dir)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / TRIAL_STRUCTURE_FILENAME).is_file())
