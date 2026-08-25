"""Two small, readable scores used to pick the best candidate.

This is deliberately not a difficulty model. It is a ranking device: several
hundred candidates all pass the hard rules in :mod:`validation`, and these
scores decide which one is handed to the participant. Each score is the plain
weighted mean of a handful of features in [0, 1], and every feature is reported
alongside the score in the .json output, so a choice can always be explained.

``musicality``  higher is better: singable motion, real repetition, a proper
                ending, a shape that goes somewhere and comes back.
``difficulty``  lower is better: small intervals, few hand changes, few
                distinct fingers, a narrow range, a plain rhythm.
"""

from dataclasses import dataclass
from typing import Dict, Sequence, Tuple

from . import features
from .config import GeneratorConfig
from .melody import MelodyLine, PitchSpace
from .rhythm import RhythmPlan, phrase_end_positions
from .timing import NoteEvent, RestEvent


@dataclass(frozen=True)
class Scores:
    musicality: float
    difficulty: float
    musicality_parts: Dict[str, float]
    difficulty_parts: Dict[str, float]

    def to_dict(self) -> Dict[str, object]:
        return {
            "musicality": round(self.musicality, 4),
            "difficulty": round(self.difficulty, 4),
            "musicality_parts": {
                k: round(v, 4) for k, v in self.musicality_parts.items()
            },
            "difficulty_parts": {
                k: round(v, 4) for k, v in self.difficulty_parts.items()
            },
        }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _peak(value: float, ideal: float, tolerance: float) -> float:
    """1.0 at ``ideal``, falling linearly to 0 at ``tolerance`` away."""
    return _clamp(1.0 - abs(value - ideal) / tolerance)


def _weighted_mean(parts: Dict[str, float], weights: Dict[str, float]) -> float:
    total = sum(weights.values())
    return sum(parts[k] * weights[k] for k in parts) / total


def musicality(
    notes: Sequence[NoteEvent],
    rests: Sequence[RestEvent],
    line: MelodyLine,
    space: PitchSpace,
    phrase_lengths: Sequence[int],
) -> Tuple[float, Dict[str, float]]:
    indices = line.indices
    deltas = features.step_intervals(indices)

    singable = _peak(features.stepwise_ratio(indices), ideal=0.75, tolerance=0.40)

    repeats = features.motif_repeat_count(indices, length=2)
    parallels = features.parallel_phrase_pairs(line.phrases)
    repetition = _clamp((repeats + parallels) / 4.0)

    ends_on_tonic = notes[-1].midi_note % 12 == space.key.tonic_pc
    approach = abs(deltas[-1]) if deltas else 0
    if ends_on_tonic and approach == 1:
        cadence = 1.0
    elif ends_on_tonic and approach == 2:
        cadence = 0.7
    elif ends_on_tonic:
        cadence = 0.4
    else:
        cadence = 0.0

    turns = features.direction_changes(indices)
    possible = max(1, len(deltas) - 1)
    shape = _peak(turns / possible, ideal=0.45, tolerance=0.45)

    available = len(set(space.pitch_classes))
    entropy = features.pitch_class_entropy([n.midi_note for n in notes], available)
    # Both extremes are bad: a flat distribution reads as random digits, and a
    # very peaked one means the tune is circling three notes.
    tonal_focus = _peak(entropy, ideal=0.82, tolerance=0.40)
    variety = _clamp((len({n.midi_note % 12 for n in notes}) - 2) / 4.0)

    ends = set(phrase_end_positions(phrase_lengths))
    held = [n.event_index for n in notes if n.duration_beats >= 2]
    at_phrase_end = sum(1 for i in held if i in ends) / len(held) if held else 0.0
    breathing = 0.7 * at_phrase_end + 0.3 * (1.0 if rests else 0.0)

    parts = {
        "singable_motion": singable,
        "repetition": repetition,
        "cadence": cadence,
        "contour_shape": shape,
        "tonal_focus": tonal_focus,
        "note_variety": variety,
        "phrasing": breathing,
    }
    weights = {
        "singable_motion": 1.0,
        "repetition": 1.3,
        "cadence": 1.0,
        "contour_shape": 0.7,
        "tonal_focus": 0.8,
        "note_variety": 0.9,
        "phrasing": 1.0,
    }
    return _weighted_mean(parts, weights), parts


def difficulty(
    notes: Sequence[NoteEvent],
    rests: Sequence[RestEvent],
    line: MelodyLine,
    phrase_lengths: Sequence[int],
    cfg: GeneratorConfig,
) -> Tuple[float, Dict[str, float]]:
    indices = line.indices
    deltas = features.step_intervals(indices)
    limit = max(1, cfg.melody.max_leap_steps)

    # All-stepwise motion is the easy end of each interval scale, not zero
    # motion, so both interval features are measured from one step upwards.
    mean_interval = sum(abs(d) for d in deltas) / len(deltas) if deltas else 0.0
    widest = max((abs(d) for d in deltas), default=0)
    interval_floor = max(1, limit - 1)

    switches = features.hand_switch_count(notes) / max(1, len(notes) - 1)
    # Three fingers is the easiest a 15-note melody gets; ten is the hardest.
    finger_load = (features.distinct_fingers(notes) - 3) / 5.0
    # Held notes at phrase ends and a rest between phrases are the design, not
    # a difficulty. What costs a beginner is a held note *inside* a phrase and
    # having to count several rests.
    phrase_ends = set(phrase_end_positions(phrase_lengths))
    interior_long = sum(
        1 for n in notes if n.duration_beats >= 2 and n.event_index not in phrase_ends
    )
    extra_rests = max(0, len(rests) - cfg.rhythm.min_rests)
    rhythm_load = 0.6 * _clamp(interior_long / 2.0) + 0.4 * _clamp(extra_rests / 3.0)
    span = (max(indices) - min(indices)) / max(1, cfg.melody.max_total_span_steps)

    parts = {
        "mean_interval": _clamp((mean_interval - 1.0) / interval_floor),
        "widest_interval": _clamp((widest - 1.0) / interval_floor),
        "hand_changes": _clamp(switches),
        "finger_load": _clamp(finger_load),
        "rhythm_load": rhythm_load,
        "range_span": _clamp(span),
    }
    weights = {
        "mean_interval": 1.2,
        "widest_interval": 1.0,
        "hand_changes": 0.8,
        "finger_load": 0.6,
        "rhythm_load": 0.8,
        "range_span": 0.8,
    }
    return _weighted_mean(parts, weights), parts


def score_candidate(
    notes: Sequence[NoteEvent],
    rests: Sequence[RestEvent],
    rhythm: RhythmPlan,
    line: MelodyLine,
    space: PitchSpace,
    phrase_lengths: Sequence[int],
    cfg: GeneratorConfig,
) -> Scores:
    music, music_parts = musicality(notes, rests, line, space, phrase_lengths)
    hard, hard_parts = difficulty(notes, rests, line, phrase_lengths, cfg)
    return Scores(
        musicality=music,
        difficulty=hard,
        musicality_parts=music_parts,
        difficulty_parts=hard_parts,
    )
