"""Generates constrained bimanual motor-sequence stimuli for the controlled
pilot study described in final_report_2026/method/method.tex ("Sequence
Design and Difficulty Levels" onward).

Each stimulus is an ordered sequence of T = 30 single-key cue events
e_t = (h_t, f_t, m_t) - one active hand, one target finger, one target MIDI
note per event; no chord-like or two-key events are ever generated. Every
sequence must use both hands across its 30 events (bimanual at the
sequence level, single-key at the event level).

Difficulty is the multidimensional representation D = (C_m, C_s, C_c) from
method.tex - groups of measurable features, never a weighted scalar score:

  - C_m (motor movement cost): mean cross-event displacement d_seq, mean
    and 95th-percentile same-hand key displacement d_m, mean same-hand
    finger-transition distance d_f, and per-hand note ranges R_L / R_R.
  - C_s (sequence complexity): normalised transition-class entropy H_norm,
    transition variability V_trans, and first-order predictability P_pred.
  - C_c (bimanual coordination): hand alternation A_h, hand-transition
    entropy H_hand, hand balance B_h, hand-region overlap O_LR, and
    cross-region frequency/extent X_f / X_e.

A candidate is accepted for a level (alpha / beta / gamma) only when every
constrained component falls inside that level's ranges (method.tex Table
"Bimanual sequence grammar and quantitative difficulty constraints",
LEVEL_CONSTRAINTS below) and it passes the structural rejection rules: no
repeated identical three-event chunk, no hand with more than 60% of
events, no finger used for more than three consecutive occurrences by
the same hand, and a hand that presses the same key on consecutive
occurrences must use the same finger for both.

The valid note pool comes from the active calibrated profile's own
midi_mapping.json (START_NOTE = min(V), END_NOTE = max(V), optionally
narrowed by the experimenter), restricted to the white-key subset
V_white = {m in V : m mod 12 in {0,2,4,5,7,9,11}}. Hand operating regions
are derived from the keyboard midpoint k_mid = k_min + S/2 with the
level-specific overlap ratio r (alpha 0, beta 0.15, gamma 0.30); only
gamma additionally permits limited cross-region movement, gated by the
ergonomic checks in _CROSS_* below.

Levels build matched families (default 9 sequences per level, 1-50
allowed) via the pairwise tolerances in FAMILY_TOLERANCES and a greedy
clique search, mirroring method.tex Table "Pairwise matching tolerances".

GUI-free by design (see app/gui/sequence_generator_window.py for the Qt
wrapper). A generated sequence is saved in the same
data/sequence/<name>/{meta.json, fingering.json} layout a real recording
produces under data/music/ (app.music_recording), so it loads unmodified
in music_playback.py, student_quiz.py and student_quiz_haptic.py.
"""

import csv
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
LEVEL_SYMBOL = {"alpha": "α", "beta": "β", "gamma": "γ"}
LEVEL_LABEL = {level: f"Level {LEVEL_SYMBOL[level]}" for level in LEVELS}
LEVEL_DIFFICULTY = {"alpha": 1, "beta": 2, "gamma": 3}

# method.tex, "Sequence Design and Difficulty Levels": every formal
# sequence trial contains exactly 30 single-key cue events.
SEQUENCE_LENGTH = 30

# How many matched sequences to generate per difficulty level, by default -
# user-configurable in the GUI (method.tex: "the default is 9 sequences per
# bin, with an allowed range of 1-50 sequences").
DEFAULT_FAMILY_COUNT = 9
MIN_FAMILY_COUNT, MAX_FAMILY_COUNT = 1, 50

# Level-specific hand-region overlap ratio r (method.tex "Hand Regions and
# Sequence Construction"): R_L(r) = [k_min, k_mid + rS/2],
# R_R(r) = [k_mid - rS/2, k_max].
LEVEL_OVERLAP_RATIO = {"alpha": 0.0, "beta": 0.15, "gamma": 0.30}

# method.tex Table "Bimanual sequence grammar and quantitative difficulty
# constraints". Every entry is an inclusive (lo, hi) range; the *_s keys
# are normalised by the note span S = k_max - k_min. STRICT_LOWER marks
# the bounds the table writes as strict ("0.08 < ..." / "0 < ...").
#
# H_hand is deliberately NOT a level acceptance range: given each level's
# own A_h lower bound, B_h floor, and the 60% single-hand share rule, the
# achievable four-class hand-transition entropy is mathematically pinned
# to ~[0.81, 1.0] for alpha/beta, and H_hand is non-monotonic in A_h (it
# peaks at A_h = 0.5, inside gamma's range), so it cannot order the
# levels. Per method.tex it is retained only as a descriptive diagnostic:
# computed, displayed, and logged, but neither a level acceptance range
# nor a family-matching tolerance.
LEVEL_CONSTRAINTS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "alpha": {
        "d_seq_mean_s": (0.0, 0.12),
        "d_m_mean_s": (0.0, 0.10),
        "d_m_p95_s": (0.0, 0.16),
        "d_f_mean": (0.0, 1.0),
        "r_h_s": (0.0, 0.30),
        "h_norm": (0.15, 0.35),
        "a_h": (0.15, 0.35),
        "b_h": (0.65, 1.0),
        "o_lr": (0.0, 0.0),
        "x_f": (0.0, 0.0),
        "x_e_s": (0.0, 0.0),
    },
    "beta": {
        "d_seq_mean_s": (0.08, 0.22),
        "d_m_mean_s": (0.08, 0.20),
        "d_m_p95_s": (0.0, 0.30),
        "d_f_mean": (0.0, 2.0),
        "r_h_s": (0.0, 0.45),
        "h_norm": (0.40, 0.65),
        "a_h": (0.30, 0.55),
        "b_h": (0.70, 1.0),
        "o_lr": (0.0, 0.15),
        "x_f": (0.0, 0.0),
        "x_e_s": (0.0, 0.0),  # implied by X_f = 0
    },
    "gamma": {
        "d_seq_mean_s": (0.15, 0.38),
        "d_m_mean_s": (0.15, 0.35),
        "d_m_p95_s": (0.0, 0.45),
        "d_f_mean": (0.0, 3.0),
        "r_h_s": (0.0, 0.65),
        "h_norm": (0.65, 0.90),
        "a_h": (0.50, 0.80),
        "b_h": (0.75, 1.0),
        "o_lr": (0.10, 0.30),
        "x_f": (0.0, 0.20),
        "x_e_s": (0.0, 0.20),
    },
}
STRICT_LOWER: Dict[str, Set[str]] = {
    "alpha": set(),
    "beta": {"d_seq_mean_s", "d_m_mean_s", "o_lr"},
    "gamma": {"d_seq_mean_s", "d_m_mean_s", "x_f"},
}

# Structural rejection rules (method.tex "Hand Regions and Sequence
# Construction", final paragraph).
MAX_SINGLE_HAND_SHARE = 0.60
MAX_CONSECUTIVE_SAME_FINGER = 3

# Gamma-level cross-region ergonomic checks (method.tex): a cross-region
# candidate is legal only if crossing extent X_e/S <= the level's x_e_s
# bound, no more than two consecutive cross-region events, no jump into or
# out of cross-region movement greater than 0.45 S, and no adjacent target
# notes assigned to physically implausible opposing fingers.
_CROSS_MAX_CONSECUTIVE = 2
_CROSS_MAX_JUMP_S = 0.45
# "Physically implausible opposing fingers" is operationalised as: in an
# adjacent opposite-hand pair whose notes are in crossed spatial order
# (the left-hand note above the right-hand note), the crossing hand must
# use a thumb-side finger (1-3) - hand-over crossing with the ring or
# little finger is not a plausible piano movement.
_CROSS_PLAUSIBLE_FINGERS = {1, 2, 3}

# Proposal-distribution steering (sampling efficiency only - acceptance is
# still decided solely by LEVEL_CONSTRAINTS + structural_violations, so
# these do not change *what* is accepted, only how quickly the random walk
# lands inside the level's H_norm / d_seq ranges).
#
# _CLASS_PERSISTENCE: probability of steering a transition back to the
# dominant transition class (of the same switch/no-switch type) observed
# so far. Low-difficulty levels need highly repetitive class structure
# (alpha's H_norm <= 0.35 corresponds to ~2-3 dominant classes over 29
# transitions - essentially scale-like walks), which a uniform choice
# would almost never produce.
_CLASS_PERSISTENCE = {"alpha": 0.99, "beta": 0.80, "gamma": 0.0}
# _SWITCH_LOCALITY levels steer each hand's first note and the notes
# adjacent to a hand switch toward the keyboard midpoint / the previous
# note, so both hands' working windows hug the register boundary and a
# switch does not burn the level's d_seq budget on a register-to-register
# jump.
_SWITCH_LOCALITY = {"alpha": True, "beta": True, "gamma": False}
_LOCALITY_TOP_NOTES = 3
# _FINGER_JUMP_WINDOW: preferred same-hand finger-transition distance
# |f_j - f_i| per level, applied with probability _FINGER_JUMP_BIAS. The
# constraint table only caps mean d_f from above (1.0 / 2.0 / 3.0), so
# without this bias every level's typical finger jump looks alike and the
# pools fail d_f's monotonic-difficulty validation.
_FINGER_JUMP_WINDOW = {"alpha": (0, 1), "beta": (1, 2), "gamma": (2, 4)}
_FINGER_JUMP_BIAS = 0.6
# _MOVE_PERSISTENCE: probability of steering back to the dominant *exact*
# complete relative move (hand switch, signed finger step, signed note
# step) rather than just its class - this is what separates the levels on
# P_pred, whose value is precisely the dominant complete move's share.
_MOVE_PERSISTENCE = {"alpha": 0.5, "beta": 0.2, "gamma": 0.0}

# method.tex Table "Pairwise matching tolerances for sequences within the
# same level pool". Keys name SequenceStats metrics; d_m_mean_s is
# span-normalised mean same-hand displacement.
FAMILY_TOLERANCES: Dict[str, float] = {
    "h_norm": 0.05,
    "d_m_mean_s": 0.04,
    "a_h": 0.08,
    "b_h": 0.10,
    "o_lr": 0.05,
}

# Evenly spaced playback schedule for a generated sequence: the same
# default note-hold length used for a real recording's un-timed notes
# (app.music_recording.DEFAULT_NOTE_DURATION_S) plus a 0.75 s gap. Used
# only by music_playback.py's demo transport; student_quiz.py never
# reads this field.
INTER_NOTE_INTERVAL_S = DEFAULT_NOTE_DURATION_S + 0.75

# Displacement binning b(d) shared by the transition classes of C_s
# (method.tex "Sequence Complexity"): 0 / (0,2] / (2,5] / >5.
def displacement_bin(d: float) -> int:
    if d == 0:
        return 0
    if d <= 2:
        return 1
    if d <= 5:
        return 2
    return 3


# K in H_norm's denominator: "the number of distinct transition classes
# available under the active generator grammar". A transition class is
# c_t = (I(hand switch), b(d_seq), phi) where phi = b(|f_t - f_{t-1}|) on a
# same-hand transition and the dedicated "switch" symbol otherwise.
#   - same-hand (switch = 0): 4 spatial bins x 3 reachable finger bins
#     (|delta finger| is 0..4, so b(.) only reaches bins 0-2) = 12 classes
#   - hand switch (switch = 1): 4 spatial bins x the single "switch" phi
#     = 4 classes
# giving K = 16 for every level (the grammar's bins and the possibility of
# a hand switch are level-independent; levels differ in the *ranges*
# allowed, not in the class vocabulary).
_PHI_SWITCH = -1
TRANSITION_CLASS_COUNT = 4 * 3 + 4  # = 16


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
    note range (method.tex: START_NOTE = min(V), END_NOTE = max(V))."""
    mapping = MidiMapping.load(Path(profile_data_dir) / keyboard_profile_name / "midi_mapping.json")
    return sorted(set(mapping.key_to_note.values()))


def white_notes_for_profile(keyboard_profile_name: str, profile_data_dir: Path = PROFILE_DATA_DIR) -> List[int]:
    """The response set is restricted to the C-major white-key subset
    V_white (method.tex "Profile-Based MIDI Note Bounds")."""
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
    """One cue event e_t = (h_t, f_t, m_t) - method.tex mandates exactly
    one hand, one finger, one key per event."""

    hand: str  # "L" or "R"
    finger: int  # 1 (thumb) .. 5 (little finger)
    note: int  # the actual MIDI note this key sends, from the profile's mapping

    @property
    def finger_label(self) -> str:
        return f"{self.hand}{self.finger}"


Sequence = List[Action]


@dataclass(frozen=True)
class SequenceStats:
    """All scalar components of D = (C_m, C_s, C_c) for one sequence,
    plus the note bounds (k_min, k_max) they were computed against so the
    span-normalised values (the *_s constraint keys) are reproducible."""

    k_min: int
    k_max: int
    # C_m - semitone distances on the calibrated profile
    d_seq_mean: float  # mean |m_t - m_{t-1}| across consecutive cue events
    d_m_mean: float  # mean same-hand key displacement
    d_m_p95: float  # 95th percentile same-hand key displacement
    d_f_mean: float  # mean same-hand finger-transition distance
    r_l: float  # left-hand MIDI-note range
    r_r: float  # right-hand MIDI-note range
    # C_s
    h_norm: float  # normalised transition-class entropy
    v_trans: float  # transition variability (distinct classes / (T-1))
    p_pred: float  # first-order predictability
    # C_c
    a_h: float  # hand alternation frequency
    h_hand: float  # normalised hand-transition entropy
    b_h: float  # left/right event-count balance
    o_lr: float  # overlap of the two hands' used note regions
    x_f: float  # cross-region event frequency
    x_e: float  # cross-region extent (semitones from the midpoint)

    @property
    def span(self) -> float:
        return float(max(self.k_max - self.k_min, 1))

    @property
    def k_mid(self) -> float:
        return self.k_min + (self.k_max - self.k_min) / 2.0

    def metric(self, key: str) -> float:
        """Value for a LEVEL_CONSTRAINTS / FAMILY_TOLERANCES key. Keys
        ending in _s are normalised by the span S; r_h_s is the larger of
        the two hand ranges (the table's R_h constraint applies to each
        hand, so the max is the binding value)."""
        if key == "d_seq_mean_s":
            return self.d_seq_mean / self.span
        if key == "d_m_mean_s":
            return self.d_m_mean / self.span
        if key == "d_m_p95_s":
            return self.d_m_p95 / self.span
        if key == "r_h_s":
            return max(self.r_l, self.r_r) / self.span
        if key == "x_e_s":
            return self.x_e / self.span
        return float(getattr(self, key))


@dataclass(frozen=True)
class ComponentSpec:
    """One scalar component of D, with the metadata the difficulty
    validation and the metrics viewer need: which C-group it belongs to
    and how it is expected to behave across alpha -> beta -> gamma
    (method.tex "Matched Sequence Families and Difficulty Validation")."""

    key: str  # attribute name on SequenceStats
    label: str  # display label
    group: str  # "C_m" | "C_s" | "C_c"
    # "increase": medians must rise with difficulty; "decrease": fall
    # (P_pred); "matching": a matching constraint, not a difficulty
    # dimension (B_h); "diagnostic": computed and reported only (H_hand);
    # "cross": validated separately against the level-specific
    # cross-region limits (X_f, X_e).
    direction: str


COMPONENTS: Tuple[ComponentSpec, ...] = (
    ComponentSpec("d_seq_mean", "d̄_seq", "C_m", "increase"),
    ComponentSpec("d_m_mean", "d̄_m", "C_m", "increase"),
    ComponentSpec("d_m_p95", "d_m95", "C_m", "increase"),
    ComponentSpec("d_f_mean", "d̄_f", "C_m", "increase"),
    ComponentSpec("r_l", "R_L", "C_m", "increase"),
    ComponentSpec("r_r", "R_R", "C_m", "increase"),
    ComponentSpec("h_norm", "H_norm", "C_s", "increase"),
    ComponentSpec("v_trans", "V_trans", "C_s", "increase"),
    ComponentSpec("p_pred", "P_pred", "C_s", "decrease"),
    ComponentSpec("a_h", "A_h", "C_c", "increase"),
    # H_hand is a descriptive diagnostic only - see the LEVEL_CONSTRAINTS
    # comment.
    ComponentSpec("h_hand", "H_hand", "C_c", "diagnostic"),
    ComponentSpec("b_h", "B_h", "C_c", "matching"),
    ComponentSpec("o_lr", "O_LR", "C_c", "increase"),
    ComponentSpec("x_f", "X_f", "C_c", "cross"),
    ComponentSpec("x_e", "X_e", "C_c", "cross"),
)


class SequenceGenerationError(Exception):
    pass


# ---------------------------------------------------------------------------
# Hand regions
# ---------------------------------------------------------------------------


def hand_regions(k_min: float, k_max: float, level: str) -> Dict[str, Tuple[float, float]]:
    """R_L(r) = [k_min, k_mid + rS/2], R_R(r) = [k_mid - rS/2, k_max] with
    the level-specific overlap ratio r (method.tex "Hand Regions and
    Sequence Construction")."""
    span = k_max - k_min
    k_mid = k_min + span / 2.0
    r = LEVEL_OVERLAP_RATIO[level]
    return {
        "L": (k_min, k_mid + r * span / 2.0),
        "R": (k_mid - r * span / 2.0, k_max),
    }


# ---------------------------------------------------------------------------
# D = (C_m, C_s, C_c) metrics
# ---------------------------------------------------------------------------


def _percentile(values: List[float], q: float) -> float:
    """Linear-interpolation percentile (numpy's default method), kept
    dependency-free so this module stays importable without numpy."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _hand_indices(actions: Sequence, hand: str) -> List[int]:
    return [t for t, a in enumerate(actions) if a.hand == hand]


def _same_hand_displacements(actions: Sequence) -> Tuple[List[float], List[float]]:
    """(key displacements d_m, finger distances d_f) over consecutive
    active actions of the same hand, pooled across both hands - i,j
    consecutive in Q_h, not necessarily adjacent cue events."""
    d_m: List[float] = []
    d_f: List[float] = []
    for hand in ("L", "R"):
        idx = _hand_indices(actions, hand)
        for i, j in zip(idx, idx[1:]):
            d_m.append(abs(actions[j].note - actions[i].note))
            d_f.append(abs(actions[j].finger - actions[i].finger))
    return d_m, d_f


def _transition_classes(actions: Sequence) -> List[Tuple[int, int, int]]:
    """c_t = (I(h_t != h_{t-1}), b(d_seq_t), phi_t) with phi_t the binned
    same-hand finger distance, or the dedicated switch symbol on a
    hand-switch transition (method.tex "Sequence Complexity")."""
    classes = []
    for prev, curr in zip(actions, actions[1:]):
        switch = 1 if curr.hand != prev.hand else 0
        phi = _PHI_SWITCH if switch else displacement_bin(abs(curr.finger - prev.finger))
        classes.append((switch, displacement_bin(abs(curr.note - prev.note)), phi))
    return classes


def normalized_entropy(actions: Sequence) -> float:
    """H_norm = -sum(p(c) log2 p(c)) / log2 K over the empirical
    transition-class distribution, K = TRANSITION_CLASS_COUNT."""
    classes = _transition_classes(actions)
    if not classes:
        return 0.0
    counts: Dict[Tuple[int, int, int], int] = {}
    for c in classes:
        counts[c] = counts.get(c, 0) + 1
    total = len(classes)
    h = -sum((n / total) * math.log2(n / total) for n in counts.values())
    return h / math.log2(TRANSITION_CLASS_COUNT)


def transition_variability(actions: Sequence) -> float:
    """V_trans = |{c_t}| / (T - 1)."""
    classes = _transition_classes(actions)
    if not classes:
        return 0.0
    return len(set(classes)) / len(classes)


def predictability(actions: Sequence) -> float:
    """P_pred: predictability of the next *complete* motor action
    a_{t+1} = (h_{t+1}, f_{t+1}, m_{t+1}) given a_t (method.tex "Sequence
    Complexity") - hand and finger count, not only the MIDI note.

    A single 30-event sequence cannot support a full action-to-action
    count table N(a, a'): most complete actions occur exactly once, so
    every singleton context would yield max p(a'|a) = 1 and the *most
    varied* sequences would score as maximally predictable. The
    conditional is therefore estimated translation-invariantly: p(a'|a)
    is parameterised by the complete relative move

        delta(a, a') = (I(h' != h), f' - f, m' - m),

    whose distribution is pooled over the sequence's T-1 transitions.
    max_{a'} p(a'|a_t) then equals the share of the dominant complete
    move for every t, so P_pred is exactly that share: the best
    single-guess accuracy of a first-order predictor of the next complete
    action. A regular key pattern with inconsistent hand or finger
    assignments spreads the move distribution and lowers P_pred, as
    method.tex requires."""
    if len(actions) < 2:
        return 0.0
    moves: Dict[Tuple[bool, int, int], int] = {}
    for prev, curr in zip(actions, actions[1:]):
        delta = (curr.hand != prev.hand, curr.finger - prev.finger, curr.note - prev.note)
        moves[delta] = moves.get(delta, 0) + 1
    return max(moves.values()) / (len(actions) - 1)


def hand_alternation(actions: Sequence) -> float:
    """A_h = (1/(T-1)) sum I(h_t != h_{t-1})."""
    if len(actions) < 2:
        return 0.0
    switches = sum(1 for prev, curr in zip(actions, actions[1:]) if curr.hand != prev.hand)
    return switches / (len(actions) - 1)


def hand_transition_entropy(actions: Sequence) -> float:
    """H_hand: entropy of the empirical first-order hand-transition
    distribution over {LL, LR, RL, RR}, normalised by log2 4."""
    if len(actions) < 2:
        return 0.0
    counts: Dict[Tuple[str, str], int] = {}
    for prev, curr in zip(actions, actions[1:]):
        g = (prev.hand, curr.hand)
        counts[g] = counts.get(g, 0) + 1
    total = len(actions) - 1
    h = -sum((n / total) * math.log2(n / total) for n in counts.values())
    return h / 2.0  # log2(4)


def hand_balance(actions: Sequence) -> float:
    """B_h = 1 - |n_L - n_R| / T."""
    if not actions:
        return 0.0
    n_l = sum(1 for a in actions if a.hand == "L")
    n_r = len(actions) - n_l
    return 1.0 - abs(n_l - n_r) / len(actions)


def region_overlap(actions: Sequence) -> float:
    """O_LR = |U_L ∩ U_R| / |U_L ∪ U_R| over the two hands' used note
    regions U_h = [min Q_h, max Q_h]. Interval size is counted as the
    number of semitone positions covered *inclusive* of both endpoints,
    so two hands that share only the boundary note k_mid produce a small
    but non-zero overlap - this is what makes beta's "X_f = 0 yet
    0 < O_LR" combination satisfiable (both hands touch the midpoint
    without either entering the other's register)."""
    l_idx = _hand_indices(actions, "L")
    r_idx = _hand_indices(actions, "R")
    if not l_idx or not r_idx:
        return 0.0
    l_notes = [actions[t].note for t in l_idx]
    r_notes = [actions[t].note for t in r_idx]
    l_lo, l_hi = min(l_notes), max(l_notes)
    r_lo, r_hi = min(r_notes), max(r_notes)
    inter = max(0, min(l_hi, r_hi) - max(l_lo, r_lo) + 1)
    union = (l_hi - l_lo + 1) + (r_hi - r_lo + 1) - inter
    return inter / union if union else 0.0


def _is_cross_event(action: Action, k_mid: float) -> bool:
    """X_t = I((h=L and m > k_mid) or (h=R and m < k_mid))."""
    return (action.hand == "L" and action.note > k_mid) or (action.hand == "R" and action.note < k_mid)


def cross_region_metrics(actions: Sequence, k_mid: float) -> Tuple[float, float]:
    """(X_f, X_e): cross-region frequency over all T events and the
    maximum cross-region distance |m_t - k_mid| in semitones."""
    if not actions:
        return 0.0, 0.0
    crosses = [a for a in actions if _is_cross_event(a, k_mid)]
    x_f = len(crosses) / len(actions)
    x_e = max((abs(a.note - k_mid) for a in crosses), default=0.0)
    return x_f, x_e


def compute_stats(actions: Sequence, k_min: int, k_max: int) -> SequenceStats:
    """All scalar components of D = (C_m, C_s, C_c) for one sequence,
    against the note bounds the sequence was (or is being) generated
    for. The same function serves generation-time acceptance and the
    metrics viewer's recomputation for saved stimuli, so the two always
    agree (method.tex, last paragraph of "Matched Sequence Families and
    Difficulty Validation")."""
    d_seq = [abs(curr.note - prev.note) for prev, curr in zip(actions, actions[1:])]
    d_m, d_f = _same_hand_displacements(actions)

    ranges = {}
    for hand in ("L", "R"):
        notes = [a.note for a in actions if a.hand == hand]
        ranges[hand] = (max(notes) - min(notes)) if notes else 0.0

    k_mid = k_min + (k_max - k_min) / 2.0
    x_f, x_e = cross_region_metrics(actions, k_mid)

    return SequenceStats(
        k_min=k_min,
        k_max=k_max,
        d_seq_mean=sum(d_seq) / len(d_seq) if d_seq else 0.0,
        d_m_mean=sum(d_m) / len(d_m) if d_m else 0.0,
        d_m_p95=_percentile(d_m, 95.0),
        d_f_mean=sum(d_f) / len(d_f) if d_f else 0.0,
        r_l=float(ranges["L"]),
        r_r=float(ranges["R"]),
        h_norm=normalized_entropy(actions),
        v_trans=transition_variability(actions),
        p_pred=predictability(actions),
        a_h=hand_alternation(actions),
        h_hand=hand_transition_entropy(actions),
        b_h=hand_balance(actions),
        o_lr=region_overlap(actions),
        x_f=x_f,
        x_e=x_e,
    )


# ---------------------------------------------------------------------------
# Constraint and structural checks
# ---------------------------------------------------------------------------


def constraint_violations(stats: SequenceStats, level: str) -> List[str]:
    """Which LEVEL_CONSTRAINTS entries this sequence's stats violate -
    empty means the sequence sits fully inside the level's table row."""
    violations = []
    strict = STRICT_LOWER[level]
    for key, (lo, hi) in LEVEL_CONSTRAINTS[level].items():
        value = stats.metric(key)
        lower_ok = value > lo if key in strict else value >= lo
        if not (lower_ok and value <= hi):
            bound = f"({lo}, {hi}]" if key in strict else f"[{lo}, {hi}]"
            violations.append(f"{key}={value:.3f} outside {bound}")
    return violations


def within_level_constraints(stats: SequenceStats, level: str) -> bool:
    return not constraint_violations(stats, level)


def has_repeated_trigram(actions: Sequence) -> bool:
    """Rejects a candidate that repeats an identical three-event chunk."""
    grams = [tuple((a.hand, a.finger, a.note) for a in actions[i : i + 3]) for i in range(len(actions) - 2)]
    return len(grams) != len(set(grams))


def structural_violations(actions: Sequence) -> List[str]:
    """The method.tex structural rejection rules, applicable to any
    action sequence (fresh candidate or a saved stimulus re-checked by
    the validation): both hands present, no repeated three-event chunk,
    no hand over 60% of events, no finger used more than three
    consecutive times by the same hand, and a hand that repeats its
    previous note must keep the same finger."""
    problems = []
    hands = {a.hand for a in actions}
    if hands != {"L", "R"}:
        problems.append("sequence does not use both hands")
    if has_repeated_trigram(actions):
        problems.append("contains a repeated identical three-event chunk")

    if actions:
        n_l = sum(1 for a in actions if a.hand == "L")
        if max(n_l, len(actions) - n_l) / len(actions) > MAX_SINGLE_HAND_SHARE:
            problems.append(f"one hand exceeds {MAX_SINGLE_HAND_SHARE:.0%} of events")

    for hand in ("L", "R"):
        fingers = [a.finger for a in actions if a.hand == hand]
        run = 0
        prev_finger = None
        for f in fingers:
            run = run + 1 if f == prev_finger else 1
            prev_finger = f
            if run > MAX_CONSECUTIVE_SAME_FINGER:
                problems.append(f"{hand} hand uses finger {f} more than {MAX_CONSECUTIVE_SAME_FINGER} times in a row")
                break

    for hand in ("L", "R"):
        hand_actions = [a for a in actions if a.hand == hand]
        for prev, curr in zip(hand_actions, hand_actions[1:]):
            if curr.note == prev.note and curr.finger != prev.finger:
                problems.append(
                    f"{hand} hand repeats note {curr.note} with a different finger "
                    f"({prev.finger} then {curr.finger})"
                )
                break
    return problems


# ---------------------------------------------------------------------------
# Note pool
# ---------------------------------------------------------------------------


def _notes_pool(
    keyboard_profile_name: str, start_note: Optional[int], end_note: Optional[int], profile_data_dir: Path
) -> Tuple[List[int], int, int]:
    """(white-key pool, k_min, k_max) for the requested range. k_min/k_max
    are the effective bounds the generator normalises by: the profile's
    own START_NOTE/END_NOTE by default, or the experimenter's narrower
    range if one was chosen - the hand regions, midpoint, and span S then
    describe the keyboard area actually in play."""
    all_notes = valid_notes_for_profile(keyboard_profile_name, profile_data_dir)
    if not all_notes:
        raise SequenceGenerationError(f"Profile {keyboard_profile_name!r} has no notes in its MIDI mapping yet.")
    lo, hi = min(all_notes), max(all_notes)

    start_note = start_note if start_note is not None else lo
    end_note = end_note if end_note is not None else hi
    if not (lo <= start_note <= end_note <= hi):
        raise ValueError(f"Note range must satisfy {lo} <= start <= end <= {hi} for profile {keyboard_profile_name!r}")

    pool = [n for n in all_notes if start_note <= n <= end_note and not _is_black_note(n)]
    if len(pool) < 4:
        raise SequenceGenerationError(
            f"Only {len(pool)} white-key note(s) fall within {start_note}-{end_note} "
            f"(out of the profile's full {lo}-{hi} range) - need at least 4 for a bimanual sequence."
        )
    return pool, start_note, end_note


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _sample_hand_labels(level: str, rng: random.Random, max_attempts: int = 400) -> Optional[List[str]]:
    """A length-T L/R hand-label sequence satisfying the level's A_h and
    B_h constraints plus the 60% single-hand share rule - sampled first so
    the note/finger walk knows every hand assignment (and hence every
    same-hand transition) in advance (method.tex "Hand Regions and
    Sequence Construction"). H_hand is not gated here (see the
    LEVEL_CONSTRAINTS comment)."""
    cons = LEVEL_CONSTRAINTS[level]
    t_total = SEQUENCE_LENGTH
    transitions = t_total - 1
    switch_lo = math.ceil(cons["a_h"][0] * transitions)
    switch_hi = math.floor(cons["a_h"][1] * transitions)
    if switch_lo > switch_hi or switch_hi < 1:
        return None

    for _ in range(max_attempts):
        n_switch = rng.randint(max(switch_lo, 1), switch_hi)
        switch_positions = set(rng.sample(range(1, t_total), n_switch))
        hand = rng.choice(("L", "R"))
        labels = [hand]
        for t in range(1, t_total):
            if t in switch_positions:
                hand = "L" if hand == "R" else "R"
            labels.append(hand)

        n_l = labels.count("L")
        n_r = t_total - n_l
        if max(n_l, n_r) / t_total > MAX_SINGLE_HAND_SHARE:
            continue
        if 1.0 - abs(n_l - n_r) / t_total < cons["b_h"][0]:
            continue
        return labels
    return None


def _build_one_sequence(
    level: str,
    notes_pool: List[int],
    k_min: int,
    k_max: int,
    rng: random.Random,
) -> Optional[Sequence]:
    """One full candidate: sample a hand-label sequence, then walk the 30
    events choosing (note, finger) uniformly from the legal continuations
    at each step. Legality enforces the hand-region rule, the per-step and
    running-budget movement constraints, the cross-region ergonomic
    checks, and the consecutive-finger rule; the level's mean/entropy
    ranges are verified afterwards on the finished candidate. Returns
    None (restart) when no legal continuation exists (method.tex "Hand
    Regions and Sequence Construction")."""
    cons = LEVEL_CONSTRAINTS[level]
    span = k_max - k_min
    k_mid = k_min + span / 2.0
    regions = hand_regions(k_min, k_max, level)
    hand_notes = {h: [n for n in notes_pool if lo <= n <= hi] for h, (lo, hi) in regions.items()}
    if not hand_notes["L"] or not hand_notes["R"]:
        return None

    labels = _sample_hand_labels(level, rng)
    if labels is None:
        return None
    t_total = len(labels)

    # Running budgets against the level's *upper* bounds. The number of
    # same-hand consecutive-active pairs is fixed by the hand labels:
    # (|Q_L| - 1) + (|Q_R| - 1) = T - 2.
    n_same_pairs = t_total - 2
    dm_budget = cons["d_m_mean_s"][1] * span * n_same_pairs
    df_budget = cons["d_f_mean"][1] * n_same_pairs
    dseq_budget = cons["d_seq_mean_s"][1] * span * (t_total - 1)
    dm_step_cap = cons["d_m_p95_s"][1] * span
    rh_cap = cons["r_h_s"][1] * span
    x_budget = math.floor(cons["x_f"][1] * t_total)
    x_required = "x_f" in STRICT_LOWER[level]

    actions: Sequence = []
    dm_total = df_total = dseq_total = 0.0
    last_action: Dict[str, Optional[Action]] = {"L": None, "R": None}
    note_lo: Dict[str, Optional[int]] = {"L": None, "R": None}
    note_hi: Dict[str, Optional[int]] = {"L": None, "R": None}
    finger_run: Dict[str, Tuple[Optional[int], int]] = {"L": (None, 0), "R": (None, 0)}
    class_counts: Dict[Tuple[int, int, int], int] = {}  # realized transition classes, for persistence steering
    move_counts: Dict[Tuple[bool, int, int], int] = {}  # realized exact complete moves, for P_pred steering
    crosses_so_far = 0
    consecutive_crosses = 0

    for t, hand in enumerate(labels):
        prev_event = actions[-1] if actions else None
        prev_same = last_action[hand]
        remaining = t_total - t  # events still to place, including this one

        candidates: List[Tuple[int, int, bool]] = []  # (note, finger, is_cross)
        for note in hand_notes[hand]:
            is_cross = _is_cross_event(Action(hand, 1, note), k_mid)
            if is_cross:
                if crosses_so_far + 1 > x_budget:
                    continue
                if consecutive_crosses + 1 > _CROSS_MAX_CONSECUTIVE:
                    continue
                if abs(note - k_mid) > cons["x_e_s"][1] * span:
                    continue
                # Jump *into* cross-region movement.
                if prev_event is not None and not _is_cross_event(prev_event, k_mid):
                    if abs(note - prev_event.note) > _CROSS_MAX_JUMP_S * span:
                        continue
            elif prev_event is not None and _is_cross_event(prev_event, k_mid):
                # Jump *out of* cross-region movement.
                if abs(note - prev_event.note) > _CROSS_MAX_JUMP_S * span:
                    continue

            if prev_same is not None:
                d = abs(note - prev_same.note)
                if d > dm_step_cap or dm_total + d > dm_budget:
                    continue
            if prev_event is not None and dseq_total + abs(note - prev_event.note) > dseq_budget:
                continue

            lo = note if note_lo[hand] is None else min(note_lo[hand], note)
            hi = note if note_hi[hand] is None else max(note_hi[hand], note)
            if hi - lo > rh_cap:
                continue

            # O_LR = 0 levels: the two hands' used note ranges must never
            # intersect, even at a single shared semitone (inclusive-count
            # overlap semantics, see region_overlap()).
            if cons["o_lr"][1] == 0.0:
                other = "R" if hand == "L" else "L"
                if note_lo[other] is not None and min(hi, note_hi[other]) >= max(lo, note_lo[other]):
                    continue

            # Crossed adjacent opposite-hand pair (left-hand note above the
            # right-hand note): every event of the pair sitting on the wrong
            # side of k_mid must already use a plausible crossing finger. The
            # previous event's finger is fixed, so an implausibly fingered
            # previous event rules out crossed-order notes entirely.
            crossed_pair = False
            if prev_event is not None and prev_event.hand != hand:
                l_note = note if hand == "L" else prev_event.note
                r_note = note if hand == "R" else prev_event.note
                crossed_pair = l_note > r_note
                if crossed_pair and _is_cross_event(prev_event, k_mid):
                    if prev_event.finger not in _CROSS_PLAUSIBLE_FINGERS:
                        continue

            for finger in range(1, 6):
                run_finger, run_len = finger_run[hand]
                if finger == run_finger and run_len + 1 > MAX_CONSECUTIVE_SAME_FINGER:
                    continue
                # A hand that repeats its previous note must keep the same
                # finger (structural_violations enforces the same rule on
                # finished/saved sequences).
                if prev_same is not None and note == prev_same.note and finger != prev_same.finger:
                    continue
                if prev_same is not None:
                    df = abs(finger - prev_same.finger)
                    if df_total + df > df_budget:
                        continue
                if crossed_pair and is_cross and finger not in _CROSS_PLAUSIBLE_FINGERS:
                    continue
                candidates.append((note, finger, is_cross))

        if not candidates:
            return None

        # --- proposal steering (see _CLASS_PERSISTENCE/_SWITCH_LOCALITY
        # above; acceptance stays strictly constraint-driven) ---

        # Keep each hand's working window near the register boundary and
        # the notes around a hand switch local, so a switch does not burn
        # the level's d_seq budget on a register-to-register jump.
        if _SWITCH_LOCALITY[level]:
            anchor = None
            if last_action[hand] is None:
                anchor = k_mid  # hand's first note: place its window at the boundary
            elif prev_event is not None and prev_event.hand != hand:
                anchor = prev_event.note  # event right after a switch
            elif t + 1 < t_total and labels[t + 1] != hand:
                anchor = k_mid  # event right before a switch
            if anchor is not None:
                distinct = sorted({c[0] for c in candidates}, key=lambda n: abs(n - anchor))
                keep = set(distinct[:_LOCALITY_TOP_NOTES])
                candidates = [c for c in candidates if c[0] in keep]

        # Prefer the level's typical same-hand finger-jump distance (see
        # _FINGER_JUMP_WINDOW) so mean d_f actually separates the levels.
        if prev_same is not None and rng.random() < _FINGER_JUMP_BIAS:
            jump_lo, jump_hi = _FINGER_JUMP_WINDOW[level]
            windowed = [c for c in candidates if jump_lo <= abs(c[1] - prev_same.finger) <= jump_hi]
            if windowed:
                candidates = windowed

        # Steer back to the dominant transition class (of this
        # transition's switch/no-switch type) with the level's persistence
        # probability - this is what pulls H_norm down into the alpha/beta
        # ranges, which uniform sampling essentially never reaches over 29
        # transitions.
        if prev_event is not None and class_counts and rng.random() < _CLASS_PERSISTENCE[level]:
            switch_flag = 1 if hand != prev_event.hand else 0
            typed = {c: n for c, n in class_counts.items() if c[0] == switch_flag}
            if typed:
                target = max(typed, key=typed.get)

                def _candidate_class(c: Tuple[int, int, bool]) -> Tuple[int, int, int]:
                    phi = _PHI_SWITCH if switch_flag else displacement_bin(abs(c[1] - prev_event.finger))
                    return (switch_flag, displacement_bin(abs(c[0] - prev_event.note)), phi)

                same_class = [c for c in candidates if _candidate_class(c) == target]
                if same_class:
                    candidates = same_class

        # Steer back to the dominant *exact* complete relative move (see
        # _MOVE_PERSISTENCE) - repeating exact moves is what raises the
        # dominant-move share P_pred for the easier levels.
        if prev_event is not None and move_counts and rng.random() < _MOVE_PERSISTENCE[level]:
            switch_flag_m = hand != prev_event.hand
            typed_moves = {m: n for m, n in move_counts.items() if m[0] == switch_flag_m}
            if typed_moves:
                target_move = max(typed_moves, key=typed_moves.get)
                same_move = [
                    c
                    for c in candidates
                    if (switch_flag_m, c[1] - prev_event.finger, c[0] - prev_event.note) == target_move
                ]
                if same_move:
                    candidates = same_move

        # Gentle steering for the two "must eventually happen" targets
        # that pure uniform sampling rarely hits: gamma needs at least one
        # cross-region event (X_f > 0), and O_LR > 0 levels need the two
        # hands' ranges to actually meet.
        if x_required and crosses_so_far == 0 and remaining <= 6:
            cross_candidates = [c for c in candidates if c[2]]
            if cross_candidates:
                candidates = cross_candidates
        needs_overlap = cons["o_lr"][0] > 0.0 or "o_lr" in STRICT_LOWER[level]
        if needs_overlap:
            other = "R" if hand == "L" else "L"
            hand_remaining = labels[t:].count(hand)
            overlap_reached = (
                note_lo[other] is not None
                and note_lo[hand] is not None
                and min(note_hi[hand], note_hi[other]) >= max(note_lo[hand], note_lo[other])
            )
            if not overlap_reached and note_lo[other] is not None and hand_remaining <= 6:
                # Prefer notes that make the two hands' used ranges meet.
                def _would_meet(candidate_note: int) -> bool:
                    new_lo = candidate_note if note_lo[hand] is None else min(note_lo[hand], candidate_note)
                    new_hi = candidate_note if note_hi[hand] is None else max(note_hi[hand], candidate_note)
                    return min(new_hi, note_hi[other]) >= max(new_lo, note_lo[other])

                meeting = [c for c in candidates if _would_meet(c[0])]
                if meeting:
                    candidates = meeting

        note, finger, is_cross = rng.choice(candidates)
        action = Action(hand, finger, note)

        if prev_same is not None:
            dm_total += abs(note - prev_same.note)
            df_total += abs(finger - prev_same.finger)
        if prev_event is not None:
            dseq_total += abs(note - prev_event.note)
            realized = _transition_classes([prev_event, action])[0]
            class_counts[realized] = class_counts.get(realized, 0) + 1
            realized_move = (hand != prev_event.hand, finger - prev_event.finger, note - prev_event.note)
            move_counts[realized_move] = move_counts.get(realized_move, 0) + 1
        last_action[hand] = action
        note_lo[hand] = note if note_lo[hand] is None else min(note_lo[hand], note)
        note_hi[hand] = note if note_hi[hand] is None else max(note_hi[hand], note)
        run_finger, run_len = finger_run[hand]
        finger_run[hand] = (finger, run_len + 1 if finger == run_finger else 1)
        crosses_so_far += 1 if is_cross else 0
        consecutive_crosses = consecutive_crosses + 1 if is_cross else 0
        actions.append(action)

    if structural_violations(actions):
        return None
    return actions


# ---------------------------------------------------------------------------
# Generation entry points
# ---------------------------------------------------------------------------


def generate_single(
    level: str,
    keyboard_profile_name: str,
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
    rng: Optional[random.Random] = None,
    max_attempts: int = 4000,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Tuple[Sequence, SequenceStats]:
    """One 30-event sequence satisfying level's D = (C_m, C_s, C_c)
    constraints, drawn from keyboard_profile_name's own valid MIDI notes."""
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}; expected one of {LEVELS}")

    rng = rng or random.Random()
    pool, k_min, k_max = _notes_pool(keyboard_profile_name, start_note, end_note, profile_data_dir)

    for _ in range(max_attempts):
        actions = _build_one_sequence(level, pool, k_min, k_max, rng)
        if actions is None:
            continue
        stats = compute_stats(actions, k_min, k_max)
        if within_level_constraints(stats, level):
            return actions, stats

    raise SequenceGenerationError(
        f"Could not generate a {LEVEL_LABEL[level]} sequence satisfying the difficulty constraints in "
        f"{max_attempts} attempts. Try widening the note range or rerunning generation."
    )


def _pairwise_compatible(a: SequenceStats, b: SequenceStats) -> bool:
    return all(abs(a.metric(key) - b.metric(key)) <= tol for key, tol in FAMILY_TOLERANCES.items())


def _largest_matched_group(stats_list: List[SequenceStats], count: int) -> List[int]:
    """Indices into stats_list forming the largest found group (up to
    count) that is mutually within tolerance pairwise - i.e. a clique in
    the "pairwise compatible" graph. Exact max-clique is NP-hard, so this
    is a standard greedy heuristic (expand from each node via its
    highest-degree common neighbours) rather than an exhaustive search."""
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
    count: int = DEFAULT_FAMILY_COUNT,
    rng: Optional[random.Random] = None,
    pool_size: Optional[int] = None,
    max_pool_attempts: int = 60000,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Dict[int, Tuple[Sequence, SequenceStats]]:
    """`count` sequences (ids 1..count) for one difficulty level, mutually
    matched within FAMILY_TOLERANCES (method.tex Table "Pairwise matching
    tolerances"), drawn from keyboard_profile_name's own valid MIDI
    notes. Candidates are deduplicated on their exact event list before
    matching."""
    if not (MIN_FAMILY_COUNT <= count <= MAX_FAMILY_COUNT):
        raise ValueError(f"count must be between {MIN_FAMILY_COUNT} and {MAX_FAMILY_COUNT}")

    rng = rng or random.Random()
    if pool_size is None:
        # A *matched* group needs many more raw candidates than its final
        # size, since every additional member has to be pairwise
        # compatible with everyone already picked.
        pool_size = max(count * 25, 40)
    max_pool_attempts = max(max_pool_attempts, pool_size * 100)
    pool, k_min, k_max = _notes_pool(keyboard_profile_name, start_note, end_note, profile_data_dir)

    candidates: List[Tuple[Sequence, SequenceStats]] = []
    seen = set()
    attempts = 0
    while len(candidates) < pool_size and attempts < max_pool_attempts:
        attempts += 1
        actions = _build_one_sequence(level, pool, k_min, k_max, rng)
        if actions is None:
            continue
        dedup_key = tuple((a.hand, a.finger, a.note) for a in actions)
        if dedup_key in seen:
            continue
        stats = compute_stats(actions, k_min, k_max)
        if not within_level_constraints(stats, level):
            continue
        seen.add(dedup_key)
        candidates.append((actions, stats))

    if len(candidates) < count:
        raise SequenceGenerationError(
            f"Only found {len(candidates)} valid {LEVEL_LABEL[level]} sequence(s) in {attempts} attempts - "
            f"need at least {count} to build a matched family. Try widening the note range, rerunning "
            "generation, or requesting a smaller pool."
        )

    group_indices = _largest_matched_group([c[1] for c in candidates], count)
    if len(group_indices) < count:
        tolerances = ", ".join(f"{key} +/-{tol}" for key, tol in FAMILY_TOLERANCES.items())
        raise SequenceGenerationError(
            f"Could only find a group of {len(group_indices)} (needed {count}) matched within tolerance "
            f"({tolerances}) out of {len(candidates)} candidates. Try widening the note range, relaxing the "
            "matching tolerances, rerunning generation, or requesting a smaller pool."
        )

    return {i + 1: candidates[idx] for i, idx in enumerate(group_indices)}


def generate_all_matched_families(
    keyboard_profile_name: str,
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
    count: int = DEFAULT_FAMILY_COUNT,
    rng: Optional[random.Random] = None,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Tuple[Dict[str, Dict[int, Tuple[Sequence, SequenceStats]]], Dict[str, str]]:
    """Generate a matched `count`-sequence family for every difficulty
    level (alpha, beta, gamma) in one call, since a stimulus set always
    needs all three. Returns (families, errors): families has one entry
    per level that generated successfully; errors has one entry per level
    that couldn't, so a caller can show whichever levels worked alongside
    a warning about the rest, instead of failing the whole batch over one
    level."""
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
                count=count,
                rng=rng,
                profile_data_dir=profile_data_dir,
            )
        except SequenceGenerationError as exc:
            errors[level] = str(exc)
    return families, errors


# ---------------------------------------------------------------------------
# Saving / loading in the shared song layout
# ---------------------------------------------------------------------------


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
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
    generation_seed: Optional[int] = None,
    generation_count: Optional[int] = None,
    data_dir: Path = SEQUENCE_DATA_DIR,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Path:
    """Writes data/sequence/<name>/{meta.json, fingering.json} - the exact
    layout app.music_recording produces from a real recording (just under
    a separate top-level folder from data/music/), so the result loads
    unmodified in music_playback.py, student_quiz.py and
    student_quiz_haptic.py via app.song_library. start_note/end_note (the
    effective generation bounds) go into meta.json so the metrics viewer
    can later recompute the span-normalised components of D against the
    same k_min/k_max the generator used; generation_seed/generation_count
    record the provenance needed to regenerate the batch exactly (same
    profile + bounds + count + seed + software version)."""
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
        start_note=start_note,
        end_note=end_note,
        generation_seed=generation_seed,
        generation_count=generation_count,
    )
    meta.save(s_dir / META_FILENAME)
    return s_dir


def export_batch_csv(
    entries: List[Tuple[str, str, Sequence, SequenceStats]],
    path: Path,
    seed: Optional[int] = None,
    start_note: Optional[int] = None,
    end_note: Optional[int] = None,
) -> Path:
    """Write one generated batch as a single CSV overview file - one row
    per sequence with its name, level, the full finger/note lists (same
    display strings as the generator table), the family-matching
    statistics plus the H_hand diagnostic, and the generation settings
    (seed, note bounds) repeated on every row so the file is
    self-contained. `entries` are (name, level, actions, stats) in table
    order. Written with utf-8-sig so the α/β/γ in sequence names survive
    a double-click into Excel. This is an overview export only - the
    per-sequence meta.json/fingering.json files remain the format every
    tool actually loads."""
    stat_keys = list(FAMILY_TOLERANCES) + ["h_hand"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "level", "seed", "start_note", "end_note", "fingers", "notes", *stat_keys])
        for name, level, actions, stats in entries:
            fingers, notes = format_sequence_for_display(actions)
            writer.writerow(
                [name, level, seed, start_note, end_note, fingers, notes]
                + [f"{stats.metric(key):.6f}" for key in stat_keys]
            )
    return path


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
    used during generation, so compute_stats() means exactly the same
    thing for either source. Entries with no resolved finger -
    unmatched/ambiguous camera frames, only possible on a real recording -
    are skipped, since an Action needs a finger; see evaluate_song()."""
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


def evaluate_song(
    name: str,
    data_dir: Path,
    fallback_bounds: Optional[Tuple[int, int]] = None,
) -> Tuple[Sequence, SequenceStats, int]:
    """Recompute every component of D = (C_m, C_s, C_c) for an
    already-saved song - a real recording (data_dir=MUSIC_DATA_DIR) or a
    generated sequence (data_dir=SEQUENCE_DATA_DIR), see app.song_library -
    using the exact same compute_stats() machinery as generation, so the
    numbers are directly comparable either way (method.tex: "The sequence
    metrics viewer recomputes all components ... using the same functions
    as the generator").

    Span-dependent components need note bounds (k_min, k_max). These come
    from, in order: the saved meta.json's start_note/end_note (present on
    newly generated sequences), the caller's fallback_bounds (e.g. the
    active profile's range, for real recordings), or the sequence's own
    min/max note as a last resort.

    Returns (actions, stats, total_note_count): actions only includes
    notes with a resolved finger (see sequence_from_fingering), so
    total_note_count can be larger than len(actions) for a real recording
    with unresolved notes."""
    entries = load_fingering(song_dir(name, data_dir) / FINGERING_FILENAME)
    actions = sequence_from_fingering(entries)

    k_min = k_max = None
    try:
        meta = SongMeta.load(song_dir(name, data_dir) / META_FILENAME)
        k_min, k_max = meta.start_note, meta.end_note
    except Exception:
        pass
    if k_min is None or k_max is None:
        if fallback_bounds is not None:
            k_min, k_max = fallback_bounds
        elif actions:
            k_min = min(a.note for a in actions)
            k_max = max(a.note for a in actions)
        else:
            k_min = k_max = 0

    return actions, compute_stats(actions, k_min, k_max), len(entries)
