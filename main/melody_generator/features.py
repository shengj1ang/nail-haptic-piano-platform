"""Shared descriptive features, used by both the rejection rules and the score.

Everything here is a pure function over an already-assembled candidate, so the
validator and the scorer always measure the same quantities in the same way.
"""

import math
from collections import Counter
from typing import Dict, Sequence, Tuple

from .melody import Phrase
from .timing import NoteEvent


def step_intervals(indices: Sequence[int]) -> Tuple[int, ...]:
    """Melodic intervals in scale steps (+1 = next white key up)."""
    return tuple(b - a for a, b in zip(indices, indices[1:]))


def interval_vector(indices: Sequence[int]) -> Tuple[int, ...]:
    return step_intervals(indices)


def contour(indices: Sequence[int]) -> Tuple[int, ...]:
    """Sign of each interval: the shape, ignoring interval size."""
    return tuple((b > a) - (b < a) for a, b in zip(indices, indices[1:]))


def stepwise_ratio(indices: Sequence[int]) -> float:
    """Share of transitions that are a step or a repeat rather than a leap."""
    deltas = step_intervals(indices)
    if not deltas:
        return 1.0
    return sum(1 for d in deltas if abs(d) <= 1) / len(deltas)


def leap_positions(indices: Sequence[int], threshold: int = 2) -> Tuple[int, ...]:
    return tuple(
        i for i, d in enumerate(step_intervals(indices)) if abs(d) >= threshold
    )


def max_consecutive_leaps(indices: Sequence[int], threshold: int = 2) -> int:
    run = best = 0
    for delta in step_intervals(indices):
        run = run + 1 if abs(delta) >= threshold else 0
        best = max(best, run)
    return best


def direction_changes(indices: Sequence[int]) -> int:
    signs = [s for s in contour(indices) if s != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def motif_repeat_count(indices: Sequence[int], length: int = 2) -> int:
    """How many interval patterns of ``length`` intervals occur more than once.

    Counted on intervals rather than absolute pitches, so a motif repeated at a
    different scale degree still registers as a repetition - which is exactly
    what makes a tune sound composed rather than sampled.
    """
    deltas = step_intervals(indices)
    if len(deltas) < length:
        return 0
    grams = Counter(
        tuple(deltas[i : i + length]) for i in range(len(deltas) - length + 1)
    )
    return sum(count - 1 for count in grams.values() if count > 1)


def parallel_phrase_pairs(phrases: Sequence[Phrase]) -> int:
    """Pairs of phrases sharing an interval pattern or a contour."""
    pairs = 0
    for i in range(len(phrases)):
        for j in range(i + 1, len(phrases)):
            first, second = phrases[i].indices, phrases[j].indices
            if len(first) != len(second):
                continue
            if interval_vector(first) == interval_vector(second):
                pairs += 2  # an exact repetition or transposition counts double
            elif contour(first) == contour(second):
                pairs += 1
    return pairs


def pitch_class_entropy(midis: Sequence[int], available_classes: int) -> float:
    """Normalised entropy of the pitch classes used, in [0, 1].

    1.0 means every available note is used equally often, which is what a
    string of random numbers looks like; a tune leans on its tonic and
    dominant and lands well below that.
    """
    if not midis or available_classes <= 1:
        return 0.0
    counts = Counter(m % 12 for m in midis)
    total = sum(counts.values())
    entropy = -sum(
        (c / total) * math.log(c / total) for c in counts.values() if c
    )
    return entropy / math.log(available_classes)


def repeated_note_ratio(indices: Sequence[int]) -> float:
    deltas = step_intervals(indices)
    if not deltas:
        return 0.0
    return sum(1 for d in deltas if d == 0) / len(deltas)


def hand_switch_count(notes: Sequence[NoteEvent]) -> int:
    return sum(1 for a, b in zip(notes, notes[1:]) if a.hand != b.hand)


def max_alternation_run(notes: Sequence[NoteEvent]) -> int:
    run = best = 0
    for a, b in zip(notes, notes[1:]):
        run = run + 1 if a.hand != b.hand else 0
        best = max(best, run)
    return best


def max_same_finger_run(notes: Sequence[NoteEvent]) -> int:
    run = best = 1
    for a, b in zip(notes, notes[1:]):
        run = run + 1 if a.finger == b.finger else 1
        best = max(best, run)
    return best


def duration_histogram(notes: Sequence[NoteEvent]) -> Dict[float, int]:
    return dict(Counter(n.duration_beats for n in notes))


def long_note_ratio(notes: Sequence[NoteEvent]) -> float:
    if not notes:
        return 0.0
    return sum(1 for n in notes if n.duration_beats >= 2) / len(notes)


def mean_absolute_semitones(notes: Sequence[NoteEvent]) -> float:
    if len(notes) < 2:
        return 0.0
    same_hand = [
        abs(b.midi_note - a.midi_note)
        for a, b in zip(notes, notes[1:])
        if a.hand == b.hand
    ]
    if not same_hand:
        return 0.0
    return sum(same_hand) / len(same_hand)


def distinct_fingers(notes: Sequence[NoteEvent]) -> int:
    return len({n.finger for n in notes})
