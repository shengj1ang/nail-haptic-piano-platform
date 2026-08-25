"""Every tunable parameter of the generator, in one place.

The config is split by concern (melody / rhythm / timing / validation) so the
generation stages stay independent, and every stage receives only the block it
needs. All blocks are frozen dataclasses: a config is hashable, printable and
serialised verbatim into the .json output, so a result can always be traced
back to the exact parameters that produced it.
"""

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Tuple

# The 25-key controller used in the study: C3..C5 inclusive.
KEYBOARD_MIN_MIDI = 48
KEYBOARD_MAX_MIDI = 72

# Pitch classes of the white keys, C=0.
WHITE_PITCH_CLASSES = frozenset({0, 2, 4, 5, 7, 9, 11})


@dataclass(frozen=True)
class MelodyConfig:
    """Pitch-side parameters: key, hand layout, phrase plan, allowed motion."""

    key: str = "c_major"
    layout: str = "middle_c"

    # Number of note-on events in the sequence. The phrase plan is derived from
    # it unless phrase_plan is given explicitly.
    note_count: int = 15
    phrase_plan: Tuple[int, ...] = ()

    # Melodic motion, measured in scale steps (1 = neighbouring white key).
    # Weights are relative; they are renormalised after the legality filter.
    interval_weights: Tuple[Tuple[int, float], ...] = (
        (0, 0.08),    # repeated note
        (1, 0.24),    # step up
        (-1, 0.24),   # step down
        (2, 0.15),    # third up
        (-2, 0.15),   # third down
        (3, 0.07),    # fourth up
        (-3, 0.07),   # fourth down
    )
    max_leap_steps: int = 3          # nothing wider than a fourth, ever
    leap_threshold_steps: int = 2    # >= this counts as a "leap"
    recover_after_leap_prob: float = 0.85  # leap -> opposite-direction step
    max_phrase_span_steps: int = 4   # a phrase stays inside a five-key window
    min_phrase_span_steps: int = 2   # ...but never sits on just two keys
    min_phrase_distinct: int = 3     # distinct keys per phrase (capped by length)
    max_total_span_steps: int = 8    # whole melody stays inside the layout
    max_repeated_notes: int = 2      # at most two identical notes in a row
    max_phrase_join_steps: int = 3   # no huge jump across a phrase boundary

    # Relative weights of the phrase transformations, per phrase role.
    answer_ops: Tuple[Tuple[str, float], ...] = (
        ("repeat", 0.22),
        ("transpose", 0.30),
        ("vary_tail", 0.36),
        ("mirror", 0.12),
    )
    contrast_ops: Tuple[Tuple[str, float], ...] = (
        ("new_contrast", 0.45),
        ("transpose", 0.35),
        ("mirror", 0.20),
    )
    transpose_steps: Tuple[int, ...] = (1, -1, 2, -2)


@dataclass(frozen=True)
class RhythmConfig:
    """Rhythm-side parameters. Everything is on a whole-beat grid."""

    note_beats: Tuple[int, ...] = (1, 2, 3)
    long_note_beats: Tuple[int, ...] = (2, 3)
    phrase_end_beats: Tuple[int, ...] = (2, 3)
    phrase_end_long_weights: Tuple[float, ...] = (0.7, 0.3)  # weights of (2, 3)
    final_note_beats: int = 3
    rest_beats: int = 1
    min_rests: int = 1
    max_rests: int = 3
    min_long_notes: int = 2
    max_long_note_ratio: float = 0.40
    # Probability of promoting one interior note of one phrase to two beats.
    internal_long_prob: float = 0.25


@dataclass(frozen=True)
class TimingConfig:
    """Beat -> second conversion, articulation (gate time) and velocity.

    gate_mode "fixed_gap" (default) releases every note a constant
    ``release_gap_beats`` before the next grid slot, so a 1-beat note sounds
    for 0.75 beat, a 2-beat note for 1.75 and a 3-beat note for 2.75. That is
    ordinary non-legato piano articulation: the gap stays audible but a held
    note still sounds held. gate_mode "ratio" instead scales the whole
    duration by ``gate_ratio`` (a 3-beat note then sounds for 2.25 beats).
    """

    bpm: float = 60.0
    gate_mode: str = "fixed_gap"
    release_gap_beats: float = 0.25
    gate_ratio: float = 0.75
    min_sounding_beats: float = 0.35
    base_velocity: int = 80
    phrase_accent: int = 0  # velocity added to the first note of each phrase


@dataclass(frozen=True)
class ValidationConfig:
    """Hard rejection thresholds. A candidate failing any check is discarded."""

    midi_min: int = KEYBOARD_MIN_MIDI
    midi_max: int = KEYBOARD_MAX_MIDI
    white_keys_only: bool = True
    unique_finger_per_key: bool = True
    max_consecutive_same_finger: int = 3
    min_distinct_keys: int = 5       # a 15-note tune on four keys is not a tune
    min_notes_per_hand: int = 1      # 0 disables; a two-hand position must
                                     # actually use both hands. Raise it for a
                                     # more evenly bimanual sequence.
    max_hand_switches: int = 9
    max_consecutive_alternations: int = 5
    max_leap_steps: int = 3
    max_consecutive_leaps: int = 1        # never two leaps back to back
    min_stepwise_ratio: float = 0.55      # steps + repeats, of all transitions
    min_distinct_durations: int = 2
    max_pitch_class_entropy: float = 0.95  # above this the melody reads random
    min_motif_repeats: int = 1            # repeated 3-note interval patterns
    require_tonic_ending: bool = True
    max_cadence_approach_steps: int = 2


@dataclass(frozen=True)
class ScoringConfig:
    """Candidate selection. Deliberately small - six features per score."""

    candidate_attempts: int = 400   # how many candidates to try at most
    candidate_pool: int = 40        # stop early once this many pass validation
    max_difficulty: float = 0.50    # safety net; the ranking does the work
    min_musicality: float = 0.55    # reject candidates less tuneful than this


@dataclass(frozen=True)
class GeneratorConfig:
    melody: MelodyConfig = field(default_factory=MelodyConfig)
    rhythm: RhythmConfig = field(default_factory=RhythmConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def with_overrides(self, **blocks: Any) -> "GeneratorConfig":
        """Return a copy with whole blocks replaced, e.g. ``melody=...``."""
        return replace(self, **blocks)


def default_phrase_plan(note_count: int) -> Tuple[int, ...]:
    """Split ``note_count`` notes into 3-5 note phrases.

    15 -> (4, 4, 4, 3): three four-note phrases plus a three-note cadence,
    which is the shape the phrase generator is built around.
    """
    if note_count < 6:
        raise ValueError("note_count must be at least 6 to form phrases")
    phrases = []
    remaining = note_count
    while remaining > 7:
        phrases.append(4)
        remaining -= 4
    # 3..7 notes left. Six or seven become a normal phrase plus a three-note
    # cadence; anything shorter is the cadence phrase itself. No phrase is
    # ever shorter than three notes.
    if remaining >= 6:
        phrases.append(remaining - 3)
        phrases.append(3)
    else:
        phrases.append(remaining)
    if len(phrases) < 2:
        raise ValueError("note_count too small for a two-phrase melody")
    return tuple(phrases)
