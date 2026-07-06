"""Generates constrained motor-sequence stimuli for the controlled pilot
study described in final_report_2026/method/method.tex, section "Sequence
Design and Difficulty Levels": 12-action (hand, finger, note) sequences at
difficulty levels alpha/beta/gamma, matched in a configurable-size group
(default 9, one per level) within tolerance for transition entropy, mean
motor cost, and hand-switch probability. Each sequence in a level's group
is named "<level symbol>-<id>" (id = 1..count).

GUI-free by design (see app/gui/sequence_generator_window.py for the Qt
wrapper), so a sequence can be generated and inspected without a display.
A generated sequence is turned into the same data/music/<name>/{meta.json,
fingering.json} layout a real recording produces (app.music_recording), so
it loads unmodified in music_playback.py, student_quiz.py and
student_quiz_haptic.py - none of those look at how a song's fingering.json
was produced.

Unlike method.tex's fixed "key index 1 = C4" formula (which assumes one
specific physical keyboard/transpose setting), the valid range here comes
straight from *this calibrated profile's own* midi_mapping.json: every
note that appears anywhere in that file is a note this specific keyboard
can actually send, and the profile's valid range is simply the min/max of
those notes (see valid_notes_for_profile()). There is no separate
abstract "key index" - a generated action's position is just its actual
MIDI note number, so LEVEL_MAX_KEY_JUMP below is measured directly in
semitones, not in method.tex's chromatic key-index units (the thesis text
is expected to be revised to match).

Two further constraints keep generation biomechanically and spatially
plausible, and both scale to any keyboard size because they are expressed
as fractions of the profile's own [K_min, K_max] range rather than as
absolute key numbers:

  - Left/right hand *regions* (see hand_regions()): each hand only ever
    plays notes from its own proportional span of the keyboard, with a
    shared overlap band around the middle so cross-hand transitions in
    Level gamma land somewhere both hands could plausibly reach, instead
    of teleporting a hand to the opposite end of the instrument.
  - A biomechanical cost function (motor_cost()) combining key-distance,
    finger-jump penalty, and hand-shift penalty, evaluated per transition
    at generation time (implausible finger transitions and excessive
    jumps are rejected via the FINGER_MATRICES/LEVEL_MAX_FINGER_JUMP/
    LEVEL_MAX_KEY_JUMP thresholds below) and in aggregate afterwards
    (candidate sequences whose mean cost/entropy/hand-switch profile
    falls outside a level's target range are discarded and regenerated).
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .keyboard.midi_mapping import MidiMapping
from .keyboard.midi_mapping import note_name as midi_note_name
from .music_recording import (
    DEFAULT_NOTE_DURATION_S,
    FINGERING_FILENAME,
    META_FILENAME,
    FingeringEntry,
    SongMeta,
    load_fingering,
    sanitize_song_name,
    save_fingering,
    song_dir,
)
from .profiles import DATA_DIR as PROFILE_DATA_DIR

# Generated sequences get their own top-level folder, data/sequence/<name>/,
# kept separate from data/music/<name>/ (real recordings, app.music_recording)
# even though both use the identical meta.json/fingering.json layout - see
# app.song_library, which lists both and labels each "music/<name>" or
# "sequence/<name>" for pickers that offer both (music_playback.py,
# student_quiz.py, student_quiz_haptic.py).
SEQUENCE_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "sequence"

LEVELS = ("alpha", "beta", "gamma")
LEVEL_SYMBOL = {"alpha": "α", "beta": "β", "gamma": "γ"}  # alpha, beta, gamma
LEVEL_LABEL = {level: f"Level {LEVEL_SYMBOL[level]}" for level in LEVELS}
LEVEL_DIFFICULTY = {"alpha": 1, "beta": 2, "gamma": 3}

# F_alpha, F_beta, F_gamma from method.tex: legal within-hand finger
# transitions (1 = allowed, 0 = disallowed), indexed [from - 1][to - 1]
# for fingers 1 (thumb) .. 5 (little finger).
FINGER_MATRICES: Dict[str, List[List[int]]] = {
    "alpha": [
        [1, 1, 0, 0, 0],
        [1, 1, 1, 0, 0],
        [0, 1, 1, 1, 0],
        [0, 0, 1, 1, 1],
        [0, 0, 0, 1, 1],
    ],
    "beta": [
        [1, 1, 1, 0, 0],
        [1, 1, 1, 1, 0],
        [1, 1, 1, 1, 1],
        [0, 1, 1, 1, 1],
        [0, 0, 1, 1, 1],
    ],
    "gamma": [[1] * 5 for _ in range(5)],
}

# method.tex, Table "Motor-sequence grammar and quantitative difficulty
# constraints", with LEVEL_MAX_KEY_JUMP reinterpreted in semitones between
# actual MIDI notes (see module docstring): 2 semitones covers one
# adjacent white key (occasionally 1, at the E-F/B-C boundary), so alpha=2
# means "adjacent white key only".
LEVEL_MAX_FINGER_JUMP = {"alpha": 1, "beta": 2, "gamma": 4}
LEVEL_MAX_KEY_JUMP = {"alpha": 2, "beta": 7, "gamma": None}  # gamma: bounded only by the active range
LEVEL_HAND_SWITCH_RANGE = {"alpha": (0.0, 0.0), "beta": (0.0, 0.0), "gamma": (0.40, 0.55)}
LEVEL_ENTROPY_RANGE = {"alpha": (0.15, 0.30), "beta": (0.50, 0.65), "gamma": (0.80, 0.95)}

# method.tex, "Sequence Design and Difficulty Levels": family-matching
# tolerances between the sequences of one level's group.
FAMILY_TOLERANCE_ENTROPY = 0.05
FAMILY_TOLERANCE_HAND_SWITCH = 0.05
FAMILY_TOLERANCE_MOTOR_COST = 0.25

DEFAULT_N_ACTIONS = 12
MIN_N_ACTIONS, MAX_N_ACTIONS = 12, 16
# How many matched sequences to generate per difficulty level, by default -
# user-configurable in the GUI, not fixed to the thesis's original 3 (X/Y/Z).
DEFAULT_FAMILY_COUNT = 9
MIN_FAMILY_COUNT, MAX_FAMILY_COUNT = 1, 50
# Fixed 750ms blank interval before the next cue (method.tex "Trial
# Structure"), plus the same default note-hold length used for a real
# recording's un-timed notes (app.music_recording.DEFAULT_NOTE_DURATION_S) -
# used only to give a generated sequence a plausible, evenly spaced
# playback schedule; student_quiz.py never reads this field, only
# music_playback.py's demo transport does.
INTER_NOTE_INTERVAL_S = DEFAULT_NOTE_DURATION_S + 0.75

# Left/right hand operating regions, as proportional spans of the profile's
# full [K_min, K_max] range rather than absolute key numbers, so this
# generalizes across keyboards of any size. HAND_REGION_ALPHA (alpha) is
# each hand's share of the keyboard's non-overlapping territory - the
# default of ~0.33 gives the left hand the lower third and the right hand
# the remaining two-thirds (treble/melody register), matching how a
# pianist's hands are typically distributed rather than splitting the
# keyboard exactly in half. HAND_OVERLAP_RATIO is the width of the shared
# middle band (both hands may reach into it) as a fraction of the full
# range; method.tex-style guidance recommends ~10-20%. See hand_regions().
HAND_REGION_ALPHA = 0.33
HAND_OVERLAP_RATIO = 0.15


def hand_regions(k_min: float, k_max: float) -> Dict[str, Tuple[float, float]]:
    """Left/right hand regions over [k_min, k_max], as proportional spans
    (see module-level constants above) - not absolute key numbers, so this
    is computed fresh for whatever keyboard/profile range is in play. The
    two regions always union to cover the full [k_min, k_max] range (no
    dead zone reachable by neither hand), overlapping by exactly
    HAND_OVERLAP_RATIO of the total span around the middle."""
    span = k_max - k_min
    overlap_width = HAND_OVERLAP_RATIO * span
    non_overlap_width = span - overlap_width
    left_exclusive = HAND_REGION_ALPHA * non_overlap_width
    right_exclusive = non_overlap_width - left_exclusive
    return {
        "L": (k_min, k_min + left_exclusive + overlap_width),
        "R": (k_max - right_exclusive - overlap_width, k_max),
    }


# "White"/"black" is a fixed fact about a MIDI note's pitch class, not
# something that needs the profile's own key-shape calibration - so this
# doesn't depend on keyboard_template.json at all, only midi_mapping.json.
_BLACK_PITCH_CLASSES = {1, 3, 6, 8, 10}  # C#, D#, F#, G#, A#


def _is_black_note(note: int) -> bool:
    return (note % 12) in _BLACK_PITCH_CLASSES


def valid_notes_for_profile(keyboard_profile_name: str, profile_data_dir: Path = PROFILE_DATA_DIR) -> List[int]:
    """Every MIDI note that appears anywhere in this profile's
    midi_mapping.json - i.e. every note this specific physical keyboard
    can actually send. min()/max() of this list is the profile's valid
    note range."""
    mapping = MidiMapping.load(Path(profile_data_dir) / keyboard_profile_name / "midi_mapping.json")
    return sorted(set(mapping.key_to_note.values()))


def white_notes_for_profile(keyboard_profile_name: str, profile_data_dir: Path = PROFILE_DATA_DIR) -> List[int]:
    """The tonal surface is restricted to the C-major white-key subset
    (method.tex): no black keys, no chords."""
    return [n for n in valid_notes_for_profile(keyboard_profile_name, profile_data_dir) if not _is_black_note(n)]


def profile_note_range(keyboard_profile_name: str, profile_data_dir: Path = PROFILE_DATA_DIR) -> Tuple[int, int]:
    """(min note, max note) this profile's midi_mapping.json covers - the
    bounds a START_NOTE/END_NOTE picker should offer."""
    notes = valid_notes_for_profile(keyboard_profile_name, profile_data_dir)
    if not notes:
        raise SequenceGenerationError(f"Profile {keyboard_profile_name!r} has no notes in its MIDI mapping yet.")
    return min(notes), max(notes)


@dataclass(frozen=True)
class Action:
    hand: str  # "L" or "R"
    finger: int  # 1 (thumb) .. 5 (little finger)
    note: int  # the actual MIDI note this key sends, from the profile's mapping

    @property
    def finger_label(self) -> str:
        return f"{self.hand}{self.finger}"


Sequence = List[Action]


@dataclass(frozen=True)
class SequenceStats:
    h_norm: float
    mean_motor_cost: float
    hand_switch_prob: float


class SequenceGenerationError(Exception):
    pass


def motor_cost(prev: Action, curr: Action) -> float:
    """The biomechanical transition cost: M_t = |f_t - f_{t-1}| (finger-jump
    penalty) + 0.5|k_t - k_{t-1}| (key-distance penalty) + 2 * I(hand
    switch) (hand-shift penalty)."""
    return abs(curr.finger - prev.finger) + 0.5 * abs(curr.note - prev.note) + (
        2.0 if curr.hand != prev.hand else 0.0
    )


def _key_jump_bin(displacement: int) -> int:
    # b(.) bins note displacement (in semitones); method.tex does not spell
    # out exact bin edges, so this uses the same breakpoints as
    # LEVEL_MAX_KEY_JUMP rather than inventing an unrelated scheme.
    if displacement == 0:
        return 0
    if displacement <= 2:
        return 1
    if displacement <= 7:
        return 2
    return 3


def _transition_class(prev: Action, curr: Action) -> Tuple[int, int, int]:
    return (
        1 if curr.hand != prev.hand else 0,
        abs(curr.finger - prev.finger),
        _key_jump_bin(abs(curr.note - prev.note)),
    )


def normalized_entropy(actions: Sequence) -> float:
    """H_norm = -sum(p(c) log2 p(c)) / log2(T) over first-order transition
    classes c."""
    transitions = [_transition_class(actions[i - 1], actions[i]) for i in range(1, len(actions))]
    t = len(transitions)
    if t <= 1:
        return 0.0
    counts: Dict[Tuple[int, int, int], int] = {}
    for c in transitions:
        counts[c] = counts.get(c, 0) + 1
    h = -sum((n / t) * math.log2(n / t) for n in counts.values())
    return h / math.log2(t)


def hand_switch_probability(actions: Sequence) -> float:
    if len(actions) <= 1:
        return 0.0
    switches = sum(1 for i in range(1, len(actions)) if actions[i].hand != actions[i - 1].hand)
    return switches / (len(actions) - 1)


def mean_motor_cost(actions: Sequence) -> float:
    if len(actions) <= 1:
        return 0.0
    costs = [motor_cost(actions[i - 1], actions[i]) for i in range(1, len(actions))]
    return sum(costs) / len(costs)


def compute_stats(actions: Sequence) -> SequenceStats:
    return SequenceStats(
        h_norm=normalized_entropy(actions),
        mean_motor_cost=mean_motor_cost(actions),
        hand_switch_prob=hand_switch_probability(actions),
    )


def has_repeated_trigram(actions: Sequence) -> bool:
    """Rejects a candidate that repeats an identical three-action chunk."""
    grams = [tuple((a.hand, a.finger, a.note) for a in actions[i : i + 3]) for i in range(len(actions) - 2)]
    return len(grams) != len(set(grams))


def _other_hand(hand: str) -> str:
    return "L" if hand == "R" else "R"


def _build_one_sequence(
    level: str,
    notes_pool: List[int],
    regions: Dict[str, Tuple[float, float]],
    n_actions: int,
    rng: random.Random,
) -> Optional[Sequence]:
    if len(notes_pool) < 2:
        return None

    # A hand may only be assigned notes from its own region (see
    # hand_regions()) - this is what keeps cross-hand transitions in Level
    # gamma spatially plausible instead of an arbitrary "hand switch"
    # teleporting a hand to the far end of the keyboard.
    hand_notes = {hand: [n for n in notes_pool if lo <= n <= hi] for hand, (lo, hi) in regions.items()}
    starting_hands = [h for h in ("L", "R") if hand_notes[h]]
    if not starting_hands:
        return None

    allow_hand_switch = level == "gamma"
    matrix = FINGER_MATRICES[level]
    max_finger_jump = LEVEL_MAX_FINGER_JUMP[level]
    max_key_jump = LEVEL_MAX_KEY_JUMP[level]

    target_switches = 0
    if allow_hand_switch:
        lo, hi = LEVEL_HAND_SWITCH_RANGE[level]
        target_switches = round(rng.uniform(lo, hi) * (n_actions - 1))

    hand = rng.choice(starting_hands)
    finger = rng.randint(1, 5)
    note = rng.choice(hand_notes[hand])
    actions: Sequence = [Action(hand, finger, note)]
    switches_so_far = 0

    for t in range(1, n_actions):
        prev = actions[-1]
        remaining_after_this = n_actions - 1 - t
        candidates = []
        for f in range(1, 6):
            if matrix[prev.finger - 1][f - 1] != 1:
                continue
            if abs(f - prev.finger) > max_finger_jump:
                continue
            switch_options = [False, True] if allow_hand_switch else [False]
            for do_switch in switch_options:
                if do_switch and switches_so_far >= target_switches:
                    continue
                # Still owe some hand switches - don't let "no switch" use up
                # the last slot(s) that need to carry them.
                if not do_switch and (target_switches - switches_so_far) > remaining_after_this:
                    continue
                new_hand = _other_hand(prev.hand) if do_switch else prev.hand
                for n in hand_notes[new_hand]:
                    if max_key_jump is not None and abs(n - prev.note) > max_key_jump:
                        continue
                    candidates.append((f, new_hand, n, do_switch))
        if not candidates:
            return None
        f, new_hand, n, do_switch = rng.choice(candidates)
        if do_switch:
            switches_so_far += 1
        actions.append(Action(new_hand, f, n))

    if has_repeated_trigram(actions):
        return None
    return actions


def _within_level_targets(level: str, stats: SequenceStats) -> bool:
    lo, hi = LEVEL_ENTROPY_RANGE[level]
    if not (lo <= stats.h_norm <= hi):
        return False
    lo_hs, hi_hs = LEVEL_HAND_SWITCH_RANGE[level]
    if lo_hs == hi_hs == 0.0:
        if stats.hand_switch_prob != 0.0:
            return False
    elif not (lo_hs <= stats.hand_switch_prob <= hi_hs):
        return False
    return True


def _notes_pool(
    keyboard_profile_name: str, start_note: Optional[int], end_note: Optional[int], profile_data_dir: Path
) -> List[int]:
    all_notes = valid_notes_for_profile(keyboard_profile_name, profile_data_dir)
    if not all_notes:
        raise SequenceGenerationError(f"Profile {keyboard_profile_name!r} has no notes in its MIDI mapping yet.")
    lo, hi = min(all_notes), max(all_notes)

    start_note = start_note if start_note is not None else lo
    end_note = end_note if end_note is not None else hi
    if not (lo <= start_note <= end_note <= hi):
        raise ValueError(f"Note range must satisfy {lo} <= start <= end <= {hi} for profile {keyboard_profile_name!r}")

    pool = [n for n in all_notes if start_note <= n <= end_note and not _is_black_note(n)]
    if len(pool) < 2:
        raise SequenceGenerationError(
            f"Only {len(pool)} white-key note(s) fall within {start_note}-{end_note} "
            f"(out of the profile's full {lo}-{hi} range) - need at least 2."
        )
    return pool


def generate_single(
    level: str,
    keyboard_profile_name: str,
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
    n_actions: int = DEFAULT_N_ACTIONS,
    rng: Optional[random.Random] = None,
    max_attempts: int = 500,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Tuple[Sequence, SequenceStats]:
    """One sequence satisfying level's finger/hand/entropy constraints,
    drawn from keyboard_profile_name's own valid MIDI notes."""
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}; expected one of {LEVELS}")
    if not (MIN_N_ACTIONS <= n_actions <= MAX_N_ACTIONS):
        raise ValueError(f"n_actions must be between {MIN_N_ACTIONS} and {MAX_N_ACTIONS}")

    rng = rng or random.Random()
    notes_pool = _notes_pool(keyboard_profile_name, start_note, end_note, profile_data_dir)
    k_min, k_max = profile_note_range(keyboard_profile_name, profile_data_dir)
    regions = hand_regions(k_min, k_max)

    for _ in range(max_attempts):
        actions = _build_one_sequence(level, notes_pool, regions, n_actions, rng)
        if actions is None:
            continue
        stats = compute_stats(actions)
        if _within_level_targets(level, stats):
            return actions, stats

    raise SequenceGenerationError(
        f"Could not generate a {LEVEL_LABEL[level]} sequence satisfying the difficulty constraints in "
        f"{max_attempts} attempts. Try a wider note range or a different action count."
    )


def _family_matches(stats_list: List[SequenceStats]) -> bool:
    for a, b in combinations(stats_list, 2):
        if abs(a.h_norm - b.h_norm) > FAMILY_TOLERANCE_ENTROPY:
            return False
        if abs(a.hand_switch_prob - b.hand_switch_prob) > FAMILY_TOLERANCE_HAND_SWITCH:
            return False
        if abs(a.mean_motor_cost - b.mean_motor_cost) > FAMILY_TOLERANCE_MOTOR_COST:
            return False
    return True


def _pairwise_compatible(a: SequenceStats, b: SequenceStats) -> bool:
    return (
        abs(a.h_norm - b.h_norm) <= FAMILY_TOLERANCE_ENTROPY
        and abs(a.hand_switch_prob - b.hand_switch_prob) <= FAMILY_TOLERANCE_HAND_SWITCH
        and abs(a.mean_motor_cost - b.mean_motor_cost) <= FAMILY_TOLERANCE_MOTOR_COST
    )


def _largest_matched_group(stats_list: List[SequenceStats], count: int) -> List[int]:
    """Indices into stats_list forming the largest found group (up to
    count) that is mutually within tolerance pairwise - i.e. a clique in
    the "pairwise compatible" graph. Exact max-clique is NP-hard, so this
    is a standard greedy heuristic (expand from each node via its
    highest-degree common neighbours) rather than an exhaustive search -
    exhaustively checking every size-`count` combination out of a pool
    only stays feasible for count <= ~3."""
    n = len(stats_list)
    neighbors = [set() for _ in range(n)]
    for i, j in combinations(range(n), 2):
        if _pairwise_compatible(stats_list[i], stats_list[j]):
            neighbors[i].add(j)
            neighbors[j].add(i)

    best: List[int] = []
    for start in sorted(range(n), key=lambda i: -len(neighbors[i])):
        if len(neighbors[start]) + 1 <= len(best):
            break  # sorted by degree descending - no later start can beat `best` either
        clique = [start]
        remaining = set(neighbors[start])
        while remaining and len(clique) < count:
            nxt = max(remaining, key=lambda c: len(neighbors[c] & remaining))
            clique.append(nxt)
            remaining &= neighbors[nxt]
        if len(clique) > len(best):
            best = clique
        if len(best) >= count:
            break
    return best


def generate_matched_family(
    level: str,
    keyboard_profile_name: str,
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
    n_actions: int = DEFAULT_N_ACTIONS,
    count: int = DEFAULT_FAMILY_COUNT,
    rng: Optional[random.Random] = None,
    pool_size: Optional[int] = None,
    max_pool_attempts: int = 4000,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Dict[int, Tuple[Sequence, SequenceStats]]:
    """`count` sequences (ids 1..count) for one difficulty level, mutually
    matched within tolerance for H_norm, mean motor cost, and hand-switch
    probability (method.tex's family-matching rule), drawn from
    keyboard_profile_name's own valid MIDI notes. Falls back to the
    tightest-matching group found (and raises if even that misses
    tolerance) rather than looping forever when the requested note range
    makes an exact match hard to find."""
    if not (MIN_FAMILY_COUNT <= count <= MAX_FAMILY_COUNT):
        raise ValueError(f"count must be between {MIN_FAMILY_COUNT} and {MAX_FAMILY_COUNT}")

    rng = rng or random.Random()
    if pool_size is None:
        # A *matched* group needs many more raw candidates than its final
        # size - e.g. finding 9 mutually-compatible sequences reliably
        # needs a noticeably bigger pool than finding 3 does, since every
        # additional member has to be pairwise compatible with everyone
        # already picked. 30x was tuned empirically against Level gamma
        # (the hardest level to match, since its key jumps are the least
        # constrained) so count=9 succeeds consistently.
        pool_size = max(count * 30, 40)
    max_pool_attempts = max(max_pool_attempts, pool_size * 20)
    notes_pool = _notes_pool(keyboard_profile_name, start_note, end_note, profile_data_dir)
    k_min, k_max = profile_note_range(keyboard_profile_name, profile_data_dir)
    regions = hand_regions(k_min, k_max)

    candidates: List[Tuple[Sequence, SequenceStats]] = []
    seen = set()
    attempts = 0
    while len(candidates) < pool_size and attempts < max_pool_attempts:
        attempts += 1
        actions = _build_one_sequence(level, notes_pool, regions, n_actions, rng)
        if actions is None:
            continue
        dedup_key = tuple((a.hand, a.finger, a.note) for a in actions)
        if dedup_key in seen:
            continue
        stats = compute_stats(actions)
        if not _within_level_targets(level, stats):
            continue
        seen.add(dedup_key)
        candidates.append((actions, stats))

    if len(candidates) < count:
        raise SequenceGenerationError(
            f"Only found {len(candidates)} valid {LEVEL_LABEL[level]} sequence(s) in {attempts} attempts - "
            f"need at least {count} to build a matched family. Try a wider note range, a different action "
            "count, or a smaller Count."
        )

    group_indices = _largest_matched_group([c[1] for c in candidates], count)
    if len(group_indices) < count:
        raise SequenceGenerationError(
            f"Could only find a group of {len(group_indices)} (needed {count}) matched within tolerance "
            f"(entropy +/-{FAMILY_TOLERANCE_ENTROPY}, hand-switch +/-{FAMILY_TOLERANCE_HAND_SWITCH}, "
            f"motor cost +/-{FAMILY_TOLERANCE_MOTOR_COST}) out of {len(candidates)} candidates. "
            "Try again, widen the note range, increase the action count, or use a smaller Count."
        )

    return {i + 1: candidates[idx] for i, idx in enumerate(group_indices)}


def generate_all_matched_families(
    keyboard_profile_name: str,
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
    n_actions: int = DEFAULT_N_ACTIONS,
    count: int = DEFAULT_FAMILY_COUNT,
    rng: Optional[random.Random] = None,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Tuple[Dict[str, Dict[int, Tuple[Sequence, SequenceStats]]], Dict[str, str]]:
    """Generate a matched `count`-sequence family for every difficulty
    level (alpha, beta, gamma) in one call, since a stimulus set always
    needs all three. Returns (families, errors): families has one entry
    per level that generated successfully; errors has one entry per level
    that couldn't (e.g. a note range too narrow for gamma's larger jumps),
    so a caller can show whichever levels worked alongside a warning about
    the rest, instead of failing the whole batch over one level."""
    rng = rng or random.Random()
    families: Dict[str, Dict[int, Tuple[Sequence, SequenceStats]]] = {}
    errors: Dict[str, str] = {}
    for level in LEVELS:
        try:
            families[level] = generate_matched_family(
                level,
                keyboard_profile_name,
                start_note=start_note,
                end_note=end_note,
                n_actions=n_actions,
                count=count,
                rng=rng,
                profile_data_dir=profile_data_dir,
            )
        except SequenceGenerationError as exc:
            errors[level] = str(exc)
    return families, errors


def to_fingering_entries(actions: Sequence, mapping: MidiMapping) -> List[FingeringEntry]:
    entries = []
    for i, a in enumerate(actions):
        entries.append(
            FingeringEntry(
                time=i * INTER_NOTE_INTERVAL_S,
                note=a.note,
                note_name=midi_note_name(a.note),
                key_id=mapping.key_for_note(a.note),
                finger=a.finger_label,
                inside=True,
            )
        )
    return entries


def save_sequence_as_song(
    name: str,
    actions: Sequence,
    level: str,
    keyboard_profile_name: str,
    data_dir: Path = SEQUENCE_DATA_DIR,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Path:
    """Writes data/sequence/<name>/{meta.json, fingering.json} - the exact
    layout app.music_recording produces from a real recording (just under
    a separate top-level folder from data/music/), so the result loads
    unmodified in music_playback.py, student_quiz.py and
    student_quiz_haptic.py via app.song_library."""
    mapping = MidiMapping.load(Path(profile_data_dir) / keyboard_profile_name / "midi_mapping.json")

    song_name = sanitize_song_name(name)
    entries = to_fingering_entries(actions, mapping)
    s_dir = song_dir(song_name, data_dir)

    save_fingering(entries, s_dir / FINGERING_FILENAME)

    duration_s = (len(actions) - 1) * INTER_NOTE_INTERVAL_S + DEFAULT_NOTE_DURATION_S if actions else 0.0
    meta = SongMeta(
        title=name.strip() or song_name,
        difficulty=LEVEL_DIFFICULTY[level],
        created_at=datetime.now(timezone.utc).isoformat(),
        duration_s=duration_s,
        note_count=len(actions),
    )
    meta.save(s_dir / META_FILENAME)
    return s_dir


def format_sequence_for_display(actions: Sequence) -> Tuple[str, str]:
    """Fingers and notes as comma-separated display strings (e.g.
    "R1, R2, R3" / "60 (C4), 62 (D4), ..."), shared between
    app.gui.sequence_generator_window and app.gui.sequence_metrics_window
    so both show a sequence identically."""
    fingers = ", ".join(a.finger_label for a in actions)
    notes = ", ".join(f"{a.note} ({midi_note_name(a.note)})" for a in actions)
    return fingers, notes


def sequence_from_fingering(entries: List[FingeringEntry]) -> Sequence:
    """Turn a saved song's fingering.json entries (a real recording under
    data/music/, or a generated sequence under data/sequence/ - see
    app.music_recording) back into the same Action-based Sequence type
    used during generation, so compute_stats() (H_norm, mean motor cost,
    hand-switch probability) means exactly the same thing for either
    source. Entries with no resolved finger - unmatched/ambiguous camera
    frames, only possible on a real recording - are skipped, since an
    Action needs a finger; see evaluate_song()."""
    actions = []
    for e in entries:
        finger = e.finger
        if not finger or finger[0] not in ("L", "R"):
            continue
        try:
            finger_num = int(finger[1:])
        except ValueError:
            continue
        actions.append(Action(hand=finger[0], finger=finger_num, note=e.note))
    return actions


def evaluate_song(name: str, data_dir: Path) -> Tuple[Sequence, SequenceStats, int]:
    """Recompute H_norm/mean motor cost/hand-switch probability for an
    already-saved song - a real recording (data_dir=MUSIC_DATA_DIR) or a
    generated sequence (data_dir=SEQUENCE_DATA_DIR), see app.song_library -
    using the exact same Action/compute_stats machinery as generation, so
    the numbers are directly comparable either way. Returns (actions,
    stats, total_note_count): actions only includes notes with a resolved
    finger (see sequence_from_fingering), so total_note_count can be
    larger than len(actions) for a real recording with unresolved notes."""
    entries = load_fingering(song_dir(name, data_dir) / FINGERING_FILENAME)
    actions = sequence_from_fingering(entries)
    return actions, compute_stats(actions), len(entries)
