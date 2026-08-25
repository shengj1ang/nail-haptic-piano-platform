"""Fingering: turn melodic indices into (MIDI note, hand, finger).

Design rule, and the reason this module is short: **fingering is a static map
from key to finger**, fixed by the hand layout, not searched for per note.
Because each hand keeps a five-key position for the whole melody, there is no
thumb-under, no hand shift, no finger substitution, and every repetition of the
melody is played with exactly the same fingers. Awkward fingering is not
filtered out afterwards - it cannot be generated in the first place.

Two things still need deciding, and they are what this module does:

* **shared keys.** In the Middle C layout both thumbs can play C4. The hand is
  chosen once per melody by looking at which side C4's neighbours fall on, so
  the "one key, one finger" property still holds within a sequence.
* **which hand plays a phrase**, in ``echo`` layouts where both hands hold the
  same shape an octave apart.
"""

import random
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .config import ValidationConfig
from .layouts import Layout
from .melody import MelodyLine
from .theory import note_name


@dataclass(frozen=True)
class Placement:
    """One note's key and the finger that plays it."""

    midi: int
    hand: str
    finger: int

    @property
    def label(self) -> str:
        return f"{self.hand}{self.finger}"

    @property
    def note_name(self) -> str:
        return note_name(self.midi)


@dataclass(frozen=True)
class FingeringPlan:
    placements: Tuple[Placement, ...]
    phrase_hands: Tuple[str, ...]
    key_to_finger: Dict[int, str]
    shared_key_decisions: Dict[int, str]


class FingeringError(RuntimeError):
    pass


def plan_phrase_hands(
    line: MelodyLine, layout: Layout, rng: random.Random
) -> Tuple[str, ...]:
    """Which hand plays each phrase (``echo`` layouts only).

    The lower hand gets a phrase that is a repetition or transposition of the
    opening, so the hand change reads as a deliberate octave echo. The closing
    phrase always stays in the upper hand so the melody ends where it began.
    """
    phrases = line.phrases
    if layout.mode != "echo":
        return tuple("" for _ in phrases)
    low_hand, high_hand = "L", "R"
    hands = [high_hand] * len(phrases)
    echoable = [
        p.position
        for p in phrases[1:-1]
        if p.op in ("repeat", "transpose", "mirror")
    ]
    if not echoable:
        echoable = [p.position for p in phrases[1:-1]]
    if echoable:
        hands[rng.choice(echoable)] = low_hand
    if low_hand not in hands and len(phrases) >= 3:
        hands[1] = low_hand
    return tuple(hands)


def _preferred_hand_for_shared_key(
    indices: Sequence[int], position: int, midis: Sequence[Optional[int]]
) -> Optional[str]:
    """Vote from the neighbouring notes: lower neighbours mean the left hand."""
    midi = midis[position]
    votes = Counter()
    for neighbour in (position - 1, position + 1):
        if not 0 <= neighbour < len(midis):
            continue
        other = midis[neighbour]
        if other is None or other == midi:
            continue
        votes["L" if other < midi else "R"] += 1
    if not votes:
        return None
    ranked = votes.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def assign_fingering(
    line: MelodyLine,
    layout: Layout,
    validation: ValidationConfig,
    rng: random.Random,
) -> FingeringPlan:
    indices = line.indices
    phrase_hands = plan_phrase_hands(line, layout, rng)
    note_phrase = line.phrase_of_note()

    if layout.mode == "echo":
        placements = []
        blocks = layout.blocks()
        for index, phrase_position in zip(indices, note_phrase):
            hand = phrase_hands[phrase_position]
            midi = blocks[hand][index]
            slots = [s for s in layout.slots_for(midi) if s.hand == hand]
            if not slots:
                raise FingeringError(f"no finger for {note_name(midi)} in hand {hand}")
            placements.append(
                Placement(midi=midi, hand=hand, finger=slots[0].finger)
            )
    else:
        midis = [layout.midi_for(index) for index in indices]
        # Resolve shared keys (both thumbs can reach middle C) once per melody
        # so that a given key keeps one finger throughout the sequence.
        shared_decisions: Dict[int, str] = {}
        per_key_votes: Dict[int, Counter] = {}
        for position, midi in enumerate(midis):
            if len(layout.slots_for(midi)) < 2:
                continue
            vote = _preferred_hand_for_shared_key(indices, position, midis)
            per_key_votes.setdefault(midi, Counter())
            if vote:
                per_key_votes[midi][vote] += 1
        for midi, votes in per_key_votes.items():
            hands = [s.hand for s in layout.slots_for(midi)]
            if votes:
                ranked = votes.most_common()
                if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                    chosen = "R" if "R" in hands else hands[0]
                else:
                    chosen = ranked[0][0]
            else:
                chosen = "R" if "R" in hands else hands[0]
            shared_decisions[midi] = chosen

        placements = []
        for midi in midis:
            slots = layout.slots_for(midi)
            if not slots:
                raise FingeringError(f"{note_name(midi)} is not in layout {layout.name}")
            if len(slots) == 1:
                slot = slots[0]
            else:
                hand = shared_decisions[midi]
                slot = next(s for s in slots if s.hand == hand)
            placements.append(
                Placement(midi=slot.midi, hand=slot.hand, finger=slot.finger)
            )

    key_to_finger: Dict[int, str] = {}
    for placement in placements:
        key_to_finger.setdefault(placement.midi, placement.label)

    return FingeringPlan(
        placements=tuple(placements),
        phrase_hands=phrase_hands,
        key_to_finger=key_to_finger,
        shared_key_decisions=(
            shared_decisions if layout.mode != "echo" else {}
        ),
    )
