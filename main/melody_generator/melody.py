"""Pitch generation: motif, phrase transformations, cadence.

The melody is written in *scale-step space*, not in MIDI numbers. A "melodic
index" is a position in the layout's list of white keys, so +1 is always the
neighbouring white key (a step), +2 a third, +3 a fourth. Everything about
melodic shape - allowed intervals, leap recovery, phrase span - is expressed in
those units, and the mapping back to MIDI happens in :mod:`fingering`.

A melody is built as a small number of phrases:

    phrase 1  statement  A      a freshly grown motif, ending on an open tone
    phrase 2  answer     A'     A repeated / transposed / re-tailed / mirrored
    phrase 3  contrast   B      a new or strongly varied idea
    phrase 4  cadence    A''    A's opening, then a step onto the tonic

Repetition is what makes 15 notes sound like a tune instead of a list of
numbers, and it is also what makes the tune learnable in a fixed number of
practice repetitions - which is the point of the experiment.
"""

import random
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .config import MelodyConfig
from .layouts import Layout
from .theory import Key

# Roles a phrase can play, in order.
ROLE_STATEMENT = "statement"
ROLE_ANSWER = "answer"
ROLE_CONTRAST = "contrast"
ROLE_CADENCE = "cadence"


@dataclass(frozen=True)
class PitchSpace:
    """The scale positions available to a melody, with their musical roles."""

    layout: Layout
    key: Key
    size: int
    pitch_classes: Tuple[int, ...]
    tonic_indices: Tuple[int, ...]
    stable_indices: Tuple[int, ...]
    open_indices: Tuple[int, ...]

    def degree(self, index: int) -> int:
        return self.key.degree_of(self.pitch_classes[index])


@dataclass(frozen=True)
class Phrase:
    position: int
    role: str
    op: str
    indices: Tuple[int, ...]


@dataclass(frozen=True)
class MelodyLine:
    phrases: Tuple[Phrase, ...]
    window: Tuple[int, int]

    @property
    def indices(self) -> Tuple[int, ...]:
        return tuple(i for phrase in self.phrases for i in phrase.indices)

    def phrase_of_note(self) -> Tuple[int, ...]:
        return tuple(
            phrase.position for phrase in self.phrases for _ in phrase.indices
        )


class MelodyGenerationError(RuntimeError):
    """Raised when a candidate cannot be built under the current constraints."""


def build_pitch_space(layout: Layout, key: Key) -> PitchSpace:
    size = layout.degree_count()
    pcs = tuple(layout.pitch_class_at(i) for i in range(size))
    tonic = tuple(i for i, pc in enumerate(pcs) if pc == key.tonic_pc)
    stable = tuple(i for i, pc in enumerate(pcs) if pc in key.stable_pcs)
    open_ = tuple(i for i, pc in enumerate(pcs) if pc in key.open_pcs)
    if not tonic:
        raise ValueError(
            f"layout {layout.name!r} contains no {key.display} tonic, so a "
            f"melody in that key cannot end properly; pick another layout"
        )
    if not stable or not open_:
        raise ValueError(f"layout {layout.name!r} is too small for {key.display}")
    return PitchSpace(
        layout=layout,
        key=key,
        size=size,
        pitch_classes=pcs,
        tonic_indices=tonic,
        stable_indices=stable,
        open_indices=open_,
    )


# --------------------------------------------------------------------------
# low-level move legality
# --------------------------------------------------------------------------

def _is_leap(delta: int, cfg: MelodyConfig) -> bool:
    return abs(delta) >= cfg.leap_threshold_steps


def _legal_moves(
    notes: Sequence[int],
    window: Tuple[int, int],
    cfg: MelodyConfig,
    force_recover: bool,
) -> List[Tuple[int, float]]:
    """Candidate next indices with weights, after every melodic rule."""
    lo, hi = window
    current = notes[-1]
    prev_delta = notes[-1] - notes[-2] if len(notes) >= 2 else 0
    span_lo = min(notes)
    span_hi = max(notes)
    moves: List[Tuple[int, float]] = []
    for delta, weight in cfg.interval_weights:
        if abs(delta) > cfg.max_leap_steps:
            continue
        nxt = current + delta
        if not lo <= nxt <= hi:
            continue
        if max(span_hi, nxt) - min(span_lo, nxt) > cfg.max_phrase_span_steps:
            continue
        # no more than `max_repeated_notes` identical notes in a row
        if delta == 0:
            run = 1
            for earlier in reversed(notes[:-1]):
                if earlier == current:
                    run += 1
                else:
                    break
            if run + 1 > cfg.max_repeated_notes:
                continue
        if prev_delta and _is_leap(prev_delta, cfg):
            # never two leaps in a row in the same direction
            if _is_leap(delta, cfg) and (delta > 0) == (prev_delta > 0):
                continue
            # after a leap, usually resolve by stepping back the other way
            if force_recover:
                if delta == 0:
                    continue
                if (delta > 0) == (prev_delta > 0):
                    continue
                if abs(delta) > 2:
                    continue
        moves.append((nxt, weight))
    return moves


def _weighted_choice(rng: random.Random, moves: Sequence[Tuple[int, float]]) -> int:
    total = sum(weight for _, weight in moves)
    threshold = rng.random() * total
    upto = 0.0
    for value, weight in moves:
        upto += weight
        if upto >= threshold:
            return value
    return moves[-1][0]


def _grow_motif(
    space: PitchSpace,
    cfg: MelodyConfig,
    rng: random.Random,
    length: int,
    start: int,
    window: Tuple[int, int],
    end_class: Optional[Sequence[int]] = None,
) -> Optional[Tuple[int, ...]]:
    """Grow a motif one note at a time under the melodic rules."""
    notes = [start]
    for position in range(1, length):
        prev_delta = notes[-1] - notes[-2] if len(notes) >= 2 else 0
        force_recover = (
            bool(prev_delta)
            and _is_leap(prev_delta, cfg)
            and rng.random() < cfg.recover_after_leap_prob
        )
        moves = _legal_moves(notes, window, cfg, force_recover)
        if not moves and force_recover:
            moves = _legal_moves(notes, window, cfg, False)
        if not moves:
            return None
        is_last = position == length - 1
        if is_last and end_class:
            wanted = [m for m in moves if m[0] in set(end_class)]
            if wanted:
                moves = wanted
        if is_last and length >= 3:
            span = max(notes) - min(notes)
            if span < cfg.min_phrase_span_steps:
                widening = [
                    m
                    for m in moves
                    if max(max(notes), m[0]) - min(min(notes), m[0])
                    >= cfg.min_phrase_span_steps
                ]
                if widening:
                    moves = widening
        notes.append(_weighted_choice(rng, moves))
    return tuple(notes)


def _phrase_ok(
    indices: Sequence[int],
    window: Tuple[int, int],
    cfg: MelodyConfig,
    previous_tail: Sequence[int] = (),
) -> bool:
    """Check a phrase, including how it joins onto the previous one.

    ``previous_tail`` is the last one or two notes of the preceding phrase, so
    the seam between phrases is held to the same interval rules as the inside
    of a phrase - otherwise a melody passes here and fails validation later.
    """
    lo, hi = window
    if not indices:
        return False
    if any(not lo <= i <= hi for i in indices):
        return False
    span = max(indices) - min(indices)
    if span > cfg.max_phrase_span_steps:
        return False
    # A phrase that sits on two neighbouring keys is not a phrase.
    if len(indices) >= 3:
        if span < cfg.min_phrase_span_steps:
            return False
        if len(set(indices)) < min(cfg.min_phrase_distinct, len(indices) - 1):
            return False
    if previous_tail:
        if abs(indices[0] - previous_tail[-1]) > cfg.max_phrase_join_steps:
            return False
    combined = list(previous_tail) + list(indices)
    deltas = [b - a for a, b in zip(combined, combined[1:])]
    if any(abs(d) > cfg.max_leap_steps for d in deltas):
        return False
    for first, second in zip(deltas, deltas[1:]):
        if _is_leap(first, cfg) and _is_leap(second, cfg):
            return False
    run = 1
    for first, second in zip(combined, combined[1:]):
        run = run + 1 if first == second else 1
        if run > cfg.max_repeated_notes:
            return False
    return True


# --------------------------------------------------------------------------
# phrase transformations
# --------------------------------------------------------------------------

def _op_repeat(statement: Tuple[int, ...], **_: object) -> Optional[Tuple[int, ...]]:
    return tuple(statement)


def _op_transpose(
    statement: Tuple[int, ...],
    cfg: MelodyConfig,
    rng: random.Random,
    window: Tuple[int, int],
    **_: object,
) -> Optional[Tuple[int, ...]]:
    for step in rng.sample(list(cfg.transpose_steps), len(cfg.transpose_steps)):
        moved = tuple(i + step for i in statement)
        if all(window[0] <= i <= window[1] for i in moved):
            return moved
    return None


def _op_mirror(
    statement: Tuple[int, ...],
    window: Tuple[int, int],
    **_: object,
) -> Optional[Tuple[int, ...]]:
    notes = [statement[0]]
    for first, second in zip(statement, statement[1:]):
        notes.append(notes[-1] - (second - first))
    moved = tuple(notes)
    if all(window[0] <= i <= window[1] for i in moved):
        return moved
    return None


def _op_vary_tail(
    statement: Tuple[int, ...],
    space: PitchSpace,
    cfg: MelodyConfig,
    rng: random.Random,
    window: Tuple[int, int],
    end_class: Sequence[int],
    **_: object,
) -> Optional[Tuple[int, ...]]:
    keep = max(1, len(statement) // 2)
    head = list(statement[:keep])
    tail_length = len(statement) - keep + 1  # +1 because head's last is reused
    grown = _grow_motif(
        space, cfg, rng, tail_length, head[-1], window, end_class=end_class
    )
    if grown is None:
        return None
    return tuple(head[:-1] + list(grown))


def _op_new_contrast(
    statement: Tuple[int, ...],
    space: PitchSpace,
    cfg: MelodyConfig,
    rng: random.Random,
    window: Tuple[int, int],
    end_class: Sequence[int],
    length: int,
    **_: object,
) -> Optional[Tuple[int, ...]]:
    """A fresh motif that deliberately starts away from the statement."""
    far = [
        i
        for i in space.stable_indices + space.open_indices
        if window[0] <= i <= window[1] and abs(i - statement[0]) >= 2
    ]
    candidates = far or [i for i in space.stable_indices if window[0] <= i <= window[1]]
    if not candidates:
        return None
    layout = space.layout
    if layout.mode == "pitch" and len(layout.hands()) == 2:
        home = set(layout.hands_at_index(statement[0]))
        other_side = [
            i for i in candidates if not set(layout.hands_at_index(i)) & home
        ]
        if other_side and rng.random() < 0.75:
            candidates = other_side
    start = rng.choice(sorted(set(candidates)))
    return _grow_motif(space, cfg, rng, length, start, window, end_class=end_class)


_OPS = {
    "repeat": _op_repeat,
    "transpose": _op_transpose,
    "mirror": _op_mirror,
    "vary_tail": _op_vary_tail,
    "new_contrast": _op_new_contrast,
}


def _pick_op(rng: random.Random, table: Sequence[Tuple[str, float]]) -> str:
    total = sum(weight for _, weight in table)
    threshold = rng.random() * total
    upto = 0.0
    for name, weight in table:
        upto += weight
        if upto >= threshold:
            return name
    return table[-1][0]


# --------------------------------------------------------------------------
# cadence
# --------------------------------------------------------------------------

def _cadence_phrase(
    statement: Tuple[int, ...],
    space: PitchSpace,
    cfg: MelodyConfig,
    rng: random.Random,
    window: Tuple[int, int],
    length: int,
    previous_last: int,
) -> Optional[Tuple[int, ...]]:
    """Quote the statement's opening, then step onto the tonic.

    A stepwise approach to the tonic is what makes a short tune sound finished
    rather than merely stopped, so the last two notes are fixed by rule: the
    supertonic falling to the tonic, or the leading tone rising to it.
    """
    tonics = [i for i in space.tonic_indices if window[0] <= i <= window[1]]
    if not tonics:
        return None
    tonic = min(tonics, key=lambda i: abs(i - previous_last))
    approaches = [i for i in (tonic + 1, tonic - 1) if window[0] <= i <= window[1]]
    if not approaches:
        return None
    # 2-1 (from above) is the plainer, more conclusive of the two.
    above = [i for i in approaches if i > tonic]
    approach = (
        above[0] if above and rng.random() < 0.6 else rng.choice(approaches)
    )

    head_length = length - 2
    head: List[int] = list(statement[:head_length]) if head_length > 0 else []

    def _head_works(candidate_head: List[int]) -> bool:
        """The quote only survives if it reaches the approach note legally
        and leaves the phrase wider than the two closing notes alone."""
        if not candidate_head:
            return False
        if abs(approach - candidate_head[-1]) > cfg.max_leap_steps:
            return False
        whole = candidate_head + [approach, tonic]
        return max(whole) - min(whole) >= cfg.min_phrase_span_steps

    if head_length > 0 and (len(head) < head_length or not _head_works(head)):
        reachable = [
            i
            for i in set(space.stable_indices + space.open_indices)
            if window[0] <= i <= window[1]
            and abs(approach - i) <= cfg.max_leap_steps
            and abs(i - previous_last) <= cfg.max_phrase_join_steps
            and abs(i - tonic) >= cfg.min_phrase_span_steps
        ]
        if not reachable:
            return None
        seed_note = min(reachable, key=lambda i: abs(i - statement[0]))
        if head_length == 1:
            head = [seed_note]
        else:
            landing = [
                i
                for i in range(window[0], window[1] + 1)
                if abs(approach - i) <= cfg.max_leap_steps
            ]
            grown = _grow_motif(
                space, cfg, rng, head_length, seed_note, window, end_class=landing
            )
            if grown is None:
                return None
            head = list(grown)
    return tuple(head + [approach, tonic])


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def _choose_window(
    space: PitchSpace, cfg: MelodyConfig, rng: random.Random
) -> Tuple[int, int]:
    """Pick the stretch of keys this melody lives in; it must hold a tonic."""
    full = space.size - 1
    width = min(full, cfg.max_total_span_steps)
    if width < 4:
        width = full
    layout = space.layout
    both_hands = len(layout.hands()) == 2
    choices = []
    for candidate_width in {width, max(4, width - 1), max(4, width - 2)}:
        if candidate_width > full:
            continue
        for lo in range(0, full - candidate_width + 1):
            hi = lo + candidate_width
            if not any(lo <= t <= hi for t in space.tonic_indices):
                continue
            if both_hands and layout.mode == "pitch":
                # The window has to give each hand something to play, or the
                # melody can only ever come out one-handed.
                reachable = {
                    hand
                    for index in range(lo, hi + 1)
                    for hand in layout.hands_at_index(index)
                }
                if len(reachable) < 2:
                    continue
            choices.append((lo, hi))
    if not choices:
        return (0, full)
    return rng.choice(sorted(set(choices)))


def _choose_start(
    space: PitchSpace, window: Tuple[int, int], rng: random.Random
) -> Optional[int]:
    lo, hi = window
    centre = (lo + hi) / 2
    options = [i for i in space.stable_indices if lo <= i <= hi]
    if not options:
        return None
    # Prefer stable tones near the middle of the window: starting at the very
    # edge would push the whole melody against the end of the hand position.
    weights = [(i, 1.0 / (1.0 + abs(i - centre))) for i in options]
    return _weighted_choice(rng, weights)


def generate_melody_line(
    space: PitchSpace,
    plan: Sequence[int],
    cfg: MelodyConfig,
    rng: random.Random,
    attempts: int = 24,
) -> MelodyLine:
    """Build one candidate melody: phrases of pitch indices."""
    window = _choose_window(space, cfg, rng)
    open_class = [i for i in space.open_indices if window[0] <= i <= window[1]]
    if not open_class:
        open_class = [i for i in range(window[0], window[1] + 1)]

    start = _choose_start(space, window, rng)
    if start is None:
        raise MelodyGenerationError("no stable starting note inside the window")

    statement: Optional[Tuple[int, ...]] = None
    for _ in range(attempts):
        candidate = _grow_motif(
            space, cfg, rng, plan[0], start, window, end_class=open_class
        )
        if candidate is not None and _phrase_ok(candidate, window, cfg):
            statement = candidate
            break
    if statement is None:
        raise MelodyGenerationError("could not grow an opening motif")

    phrases: List[Phrase] = [Phrase(0, ROLE_STATEMENT, "new", statement)]

    for position in range(1, len(plan)):
        length = plan[position]
        previous_tail = phrases[-1].indices[-2:]
        previous_last = previous_tail[-1]
        is_last = position == len(plan) - 1
        if is_last:
            built = None
            for _ in range(attempts):
                candidate = _cadence_phrase(
                    statement, space, cfg, rng, window, length, previous_last
                )
                if candidate is not None and _phrase_ok(
                    candidate, window, cfg, previous_tail
                ):
                    built = candidate
                    break
            if built is None:
                raise MelodyGenerationError("could not build a cadence phrase")
            phrases.append(Phrase(position, ROLE_CADENCE, "cadence", built))
            continue

        role = ROLE_ANSWER if position == 1 else ROLE_CONTRAST
        table = cfg.answer_ops if role == ROLE_ANSWER else cfg.contrast_ops
        built = None
        used_op = "grow"
        for _ in range(attempts):
            op_name = _pick_op(rng, table)
            source = statement if role == ROLE_ANSWER else phrases[0].indices
            # Every transformation quotes the source note for note, so it can
            # only be used when the two phrases are the same length; a phrase
            # plan with mixed lengths falls back to a fresh motif.
            if len(source) != length and op_name != "new_contrast":
                op_name = "new_contrast"
            candidate = _OPS[op_name](
                statement=source,
                space=space,
                cfg=cfg,
                rng=rng,
                window=window,
                end_class=open_class,
                length=length,
            )
            if candidate is not None and len(candidate) != length:
                candidate = None
            if candidate is not None and _phrase_ok(
                candidate, window, cfg, previous_tail
            ):
                built = candidate
                used_op = op_name
                break
        if built is None:
            # Fall back to simply growing a new motif from the previous note.
            for _ in range(attempts):
                seed_note = previous_last
                candidate = _grow_motif(
                    space, cfg, rng, length, seed_note, window, end_class=open_class
                )
                if candidate is not None and _phrase_ok(
                    candidate, window, cfg, previous_tail
                ):
                    built = candidate
                    used_op = "grow"
                    break
        if built is None:
            raise MelodyGenerationError(f"could not build phrase {position}")
        phrases.append(Phrase(position, role, used_op, built))

    line = MelodyLine(phrases=tuple(phrases), window=window)
    if len(line.indices) != sum(plan):
        raise MelodyGenerationError(
            f"built {len(line.indices)} notes but the phrase plan asks for "
            f"{sum(plan)}"
        )
    return line
