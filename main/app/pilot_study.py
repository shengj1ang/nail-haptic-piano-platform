"""Main user study: participant trial schedules.

Implements the locked trial structure from final_report_2026/method/
method.tex ("Trial Structure" + "Within-Subject Randomisation and
Counterbalancing"): 3 feedback conditions (A key-only, B visual finger
cue, C vibrotactile finger cue) x 3 difficulty levels (alpha, beta,
gamma) x 3 unique sequence trials per condition-level cell = 27 formal
trials per participant, randomised as one interleaved list (not condition
blocks) with a recorded seed, and 2-minute scheduled rests after trials 9
and 18.

Sequences come from the generated stimulus pools under data/sequence/
(app.sequence_generator), whose default names follow
"<batch>-<level symbol>-<id>" (e.g. "1577174918-α-4"); this module groups
them by batch so one generation run can be locked as a participant's
stimulus set. Per method.tex, the three trials within a condition-level
cell must be *different* sequences from that level's pool; when the pool
holds at least 9 sequences per level (the generator's default family
count), every sequence is additionally used at most once across the three
conditions.

The schedule is saved to
data/MainUserStudy/<participant>/TrialStructure.json together with
the participant metadata (method.tex logs handedness etc. as descriptive
metadata, not experimental factors). Every trial row carries its own
status/timestamps and the document carries a derived progress block, so
the future experiment runner can crash at any point and resume from
next_pending_trial() - the file on disk is always the source of truth for
how far the session got. This module is GUI-free by design; see
app/gui/pilot_schedule_window.py for the Qt wrapper.
"""

import json
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from .music_recording import list_songs, sanitize_song_name
from .sequence_generator import LEVEL_SYMBOL, LEVELS, SEQUENCE_DATA_DIR

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "MainUserStudy"
TRIAL_STRUCTURE_FILENAME = "TrialStructure.json"
SCHEMA_VERSION = 1

# method.tex Table "Pilot study feedback conditions".
CONDITIONS = ("A", "B", "C")
CONDITION_LABEL = {
    "A": "Key-only practice",
    "B": "Visual finger-cue guidance",
    "C": "Vibrotactile finger guidance",
}

TRIALS_PER_CELL = 3  # unique sequences per condition-level cell
TOTAL_TRIALS = len(CONDITIONS) * len(LEVELS) * TRIALS_PER_CELL  # = 27
# 2-min seated rests after these trial indices (method.tex "Trial
# Structure": fatigue management only, not condition/difficulty blocks).
REST_AFTER_TRIALS = (9, 18)
REST_DURATION_S = 120

# Participant metadata options. The study records *biological* sex
# (male/female), but declining to answer must always be possible, so the
# ethics-standard "prefer not to say" is offered too. Handedness is
# descriptive metadata, not an experimental factor (method.tex
# "Handedness and Experimental Factors").
SEX_OPTIONS = ("male", "female", "prefer not to say")
HANDEDNESS_OPTIONS = ("right", "left", "ambidextrous")

TRIAL_STATUS_PENDING = "pending"
TRIAL_STATUS_IN_PROGRESS = "in_progress"
TRIAL_STATUS_COMPLETED = "completed"

# The Experiment Sequence Generator's default row naming,
# "<batch>-<level symbol>-<id>" or "<level symbol>-<id>" with no batch
# (same shape app/gui/sequence_metrics_window.py parses batch names out
# of). Hand-renamed sequences that don't match are simply not offered here.
_SEQUENCE_NAME_PATTERN = re.compile(r"^(?:(?P<batch>.+)-)?(?P<symbol>[αβγ])-(?P<id>\d+)$")
_SYMBOL_TO_LEVEL = {symbol: level for level, symbol in LEVEL_SYMBOL.items()}

# Batch key used for generator-named sequences saved without a batch name.
NO_BATCH_KEY = "(no batch name)"


class PilotStudyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Discovering stimulus batches under data/sequence/
# ---------------------------------------------------------------------------


def discover_sequence_batches(sequence_data_dir: Path = SEQUENCE_DATA_DIR) -> Dict[str, Dict[str, List[str]]]:
    """batch name -> level -> sorted sequence names, parsed from every
    generator-named sequence under data/sequence/. A batch is one
    generation run (the Sequence Generator's Batch name / seed), so
    locking a participant to a batch locks them to one matched family per
    level."""
    batches: Dict[str, Dict[str, List[str]]] = {}
    for name in list_songs(sequence_data_dir):
        match = _SEQUENCE_NAME_PATTERN.match(name)
        if not match:
            continue
        level = _SYMBOL_TO_LEVEL[match.group("symbol")]
        batch = match.group("batch") or NO_BATCH_KEY
        batches.setdefault(batch, {lvl: [] for lvl in LEVELS})[level].append(name)

    def _id_of(seq_name: str) -> int:
        m = _SEQUENCE_NAME_PATTERN.match(seq_name)
        return int(m.group("id")) if m else 0

    for pools in batches.values():
        for level in LEVELS:
            pools[level].sort(key=_id_of)
    return batches


# ---------------------------------------------------------------------------
# Building a participant's schedule
# ---------------------------------------------------------------------------


def build_schedule(pools: Dict[str, List[str]], seed: int) -> List[dict]:
    """The 27-trial randomised schedule (method.tex pseudocode): for every
    condition-level cell, sample TRIALS_PER_CELL *different* sequences
    from that level's pool, then shuffle the complete factorial list into
    one interleaved order with the given seed. With >= 9 sequences per
    level, sampling is without replacement across the three conditions
    too, so no sequence is seen twice in the whole session."""
    rng = random.Random(seed)
    need_per_level = TRIALS_PER_CELL * len(CONDITIONS)

    cells: List[dict] = []
    for level in LEVELS:
        pool = list(pools.get(level, []))
        if len(pool) < TRIALS_PER_CELL:
            raise PilotStudyError(
                f"Level {LEVEL_SYMBOL[level]} has only {len(pool)} sequence(s) in this batch - "
                f"need at least {TRIALS_PER_CELL} per level."
            )
        if len(pool) >= need_per_level:
            picks = rng.sample(pool, need_per_level)
        else:
            # Small pool: still unique within each cell (a hard method.tex
            # requirement); sequences may repeat across conditions.
            picks = []
            for _ in CONDITIONS:
                picks.extend(rng.sample(pool, TRIALS_PER_CELL))
        for c_idx, condition in enumerate(CONDITIONS):
            for seq_name in picks[c_idx * TRIALS_PER_CELL : (c_idx + 1) * TRIALS_PER_CELL]:
                cells.append({"condition": condition, "level": level, "sequence": seq_name})

    rng.shuffle(cells)

    trials = []
    for index, cell in enumerate(cells, start=1):
        trials.append(
            {
                "index": index,
                "condition": cell["condition"],
                "condition_label": CONDITION_LABEL[cell["condition"]],
                "level": cell["level"],
                "level_symbol": LEVEL_SYMBOL[cell["level"]],
                "sequence": cell["sequence"],
                "rest_after": index in REST_AFTER_TRIALS,
                "status": TRIAL_STATUS_PENDING,
                "started_at": None,
                "completed_at": None,
            }
        )
    return trials


def new_trial_structure(
    participant: dict,
    sequence_batch: str,
    pools: Dict[str, List[str]],
    seed: int,
    keyboard_profile: str,
) -> dict:
    """A complete TrialStructure.json document. `participant` is the
    metadata dict (name, sex, age, handedness, ...); everything needed to
    audit or re-derive the schedule (batch, seed, profile) is recorded,
    per method.tex: "The generated schedule, random seed, condition order
    as actually presented ... are saved in the session metadata"."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "created_at": time.time(),
        "participant": dict(participant),
        "keyboard_profile": keyboard_profile,
        "sequence_batch": sequence_batch,
        "schedule_seed": seed,
        "conditions": dict(CONDITION_LABEL),
        "rest_after_trials": list(REST_AFTER_TRIALS),
        "rest_duration_s": REST_DURATION_S,
        "trials": build_schedule(pools, seed),
    }
    recompute_progress(doc)
    return doc


# ---------------------------------------------------------------------------
# Progress tracking (crash-resume support for the experiment runner)
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
    """The trial the experiment runner should (re)start from: the first
    one not yet completed - an in_progress trial (a crash mid-trial) is
    returned again, so it gets rerun rather than silently skipped."""
    return next((t for t in doc["trials"] if t["status"] != TRIAL_STATUS_COMPLETED), None)


def _find_trial(doc: dict, index: int) -> dict:
    for trial in doc["trials"]:
        if trial["index"] == index:
            return trial
    raise PilotStudyError(f"No trial with index {index} (1..{len(doc['trials'])}).")


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
# Saving / loading
# ---------------------------------------------------------------------------


def participant_dir(participant_name: str, data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / sanitize_song_name(participant_name)


def trial_structure_path(participant_name: str, data_dir: Path = DATA_DIR) -> Path:
    return participant_dir(participant_name, data_dir) / TRIAL_STRUCTURE_FILENAME


def save_trial_structure(doc: dict, data_dir: Path = DATA_DIR) -> Path:
    """Write (or rewrite) the participant's TrialStructure.json. The
    experiment runner calls this after every status change, so the file
    on disk always reflects the latest progress."""
    recompute_progress(doc)
    path = trial_structure_path(doc["participant"]["name"], data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False keeps the α/β/γ sequence names human-readable.
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_trial_structure(participant_name: str, data_dir: Path = DATA_DIR) -> dict:
    path = trial_structure_path(participant_name, data_dir)
    doc = json.loads(path.read_text(encoding="utf-8"))
    recompute_progress(doc)  # tolerate a file whose summary drifted (hand edits)
    return doc


def list_participants(data_dir: Path = DATA_DIR) -> List[str]:
    """Every participant folder under data/MainUserStudy/ that
    already holds a TrialStructure.json."""
    root = Path(data_dir)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / TRIAL_STRUCTURE_FILENAME).is_file())
