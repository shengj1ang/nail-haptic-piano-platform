"""Rhythm: a whole-beat grid of 1/2/3-beat notes and 1-beat rests.

The rhythmic idea is deliberately one sentence long: **every phrase ends on a
held note, and some phrase endings are followed by a one-beat rest.** Nothing
else varies. That gives the ear an obvious grouping (which is what makes a
15-note tune singable and learnable), keeps the grid free of anything a
non-pianist has to count, and makes parallel phrases share a rhythm, which
reinforces the repetition already present in the pitches.

No eighth notes, no ties across the beat, no syncopation: every onset lands on
a whole beat, and at 60 BPM one beat is one second.
"""

import random
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .config import RhythmConfig


@dataclass(frozen=True)
class RestSlot:
    rest_index: int
    onset_beat: float
    duration_beats: int
    after_note: int  # index of the note this rest follows


@dataclass(frozen=True)
class RhythmPlan:
    durations: Tuple[int, ...]      # beats per note, aligned with the melody
    onsets: Tuple[float, ...]       # onset of each note, in beats from the start
    rests: Tuple[RestSlot, ...]
    total_beats: float

    @property
    def long_note_positions(self) -> Tuple[int, ...]:
        return tuple(i for i, d in enumerate(self.durations) if d >= 2)


class RhythmGenerationError(RuntimeError):
    pass


def phrase_end_positions(phrase_lengths: Sequence[int]) -> Tuple[int, ...]:
    """Note index of the last note of each phrase."""
    ends: List[int] = []
    running = 0
    for length in phrase_lengths:
        running += length
        ends.append(running - 1)
    return tuple(ends)


def _weighted_choice(
    rng: random.Random, values: Sequence[int], weights: Sequence[float]
) -> int:
    total = sum(weights)
    threshold = rng.random() * total
    upto = 0.0
    for value, weight in zip(values, weights):
        upto += weight
        if upto >= threshold:
            return value
    return values[-1]


def generate_rhythm(
    phrase_lengths: Sequence[int], cfg: RhythmConfig, rng: random.Random
) -> RhythmPlan:
    note_count = sum(phrase_lengths)
    if note_count < 3:
        raise RhythmGenerationError("too few notes for a rhythm")
    durations = [1] * note_count
    ends = phrase_end_positions(phrase_lengths)

    # Held note at the end of every phrase; the final one is the longest.
    for end in ends[:-1]:
        durations[end] = _weighted_choice(
            rng, cfg.phrase_end_beats, cfg.phrase_end_long_weights
        )
    durations[ends[-1]] = cfg.final_note_beats

    # Optionally one interior note is held for two beats, for a little variety
    # inside a phrase rather than only at its edges.
    if rng.random() < cfg.internal_long_prob:
        interior = [
            i
            for i in range(note_count)
            if i not in ends and i > 0 and durations[i] == 1
        ]
        if interior:
            durations[rng.choice(interior)] = 2

    long_count = sum(1 for d in durations if d >= 2)
    if long_count < cfg.min_long_notes:
        raise RhythmGenerationError("not enough held notes")
    if long_count / note_count > cfg.max_long_note_ratio:
        raise RhythmGenerationError("too many held notes")

    # Rests only ever sit between phrases, never at the start or the end.
    candidates = list(ends[:-1])
    lowest = min(cfg.min_rests, len(candidates))
    highest = min(cfg.max_rests, len(candidates))
    rest_after = set()
    if highest >= 1 and highest >= lowest:
        count = rng.randint(lowest, highest)
        if count:
            rest_after = set(rng.sample(candidates, count))

    onsets: List[float] = []
    rests: List[RestSlot] = []
    cursor = 0.0
    for position, duration in enumerate(durations):
        onsets.append(cursor)
        cursor += duration
        if position in rest_after:
            rests.append(
                RestSlot(
                    rest_index=len(rests),
                    onset_beat=cursor,
                    duration_beats=cfg.rest_beats,
                    after_note=position,
                )
            )
            cursor += cfg.rest_beats

    return RhythmPlan(
        durations=tuple(durations),
        onsets=tuple(onsets),
        rests=tuple(rests),
        total_beats=cursor,
    )
