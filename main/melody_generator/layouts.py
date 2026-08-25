"""Hand layouts: the fixed five-finger positions the melody is written for.

The generator never asks a participant to move a hand, pass the thumb under or
substitute fingers. Instead each hand occupies a contiguous block of five white
keys, and a key's fingering follows from which block it falls in. Fingering is
therefore a *static map* from pitch to (hand, finger): the same key is always
played by the same finger, in every repetition of the melody - the property a
fixed-fingering learning study needs.

Two layout modes:

``pitch``  the melody moves freely through all keys of the layout and the hand
           follows the pitch. ``middle_c`` (both thumbs share middle C, so the
           two blocks form nine contiguous white keys F3..G4) is the default.
``echo``   both hands hold the *same* five-note shape an octave apart, and a
           whole phrase is played by one hand. A repeated phrase handed to the
           other hand is a literal octave echo, which sounds intentional rather
           than accidental.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import KEYBOARD_MAX_MIDI, KEYBOARD_MIN_MIDI
from .theory import is_white, note_name


@dataclass(frozen=True)
class FingerSlot:
    """One playable key and the finger that plays it."""

    midi: int
    hand: str    # "L" or "R"
    finger: int  # 1 = thumb .. 5 = little finger

    @property
    def label(self) -> str:
        return f"{self.hand}{self.finger}"

    def describe(self) -> str:
        return f"{self.label}={note_name(self.midi)}({self.midi})"


@dataclass(frozen=True)
class Layout:
    name: str
    description: str
    mode: str                       # "pitch" | "echo"
    slots: Tuple[FingerSlot, ...]   # ascending by midi; a shared key has two

    # --- key/slot lookups -------------------------------------------------

    def keys(self) -> Tuple[int, ...]:
        """Distinct playable MIDI notes, ascending."""
        return tuple(sorted({slot.midi for slot in self.slots}))

    def slots_for(self, midi: int) -> Tuple[FingerSlot, ...]:
        return tuple(slot for slot in self.slots if slot.midi == midi)

    def hands(self) -> Tuple[str, ...]:
        return tuple(sorted({slot.hand for slot in self.slots}))

    def blocks(self) -> Dict[str, Tuple[int, ...]]:
        """Per-hand key block, ascending by MIDI."""
        out: Dict[str, List[int]] = {}
        for slot in self.slots:
            out.setdefault(slot.hand, []).append(slot.midi)
        return {hand: tuple(sorted(keys)) for hand, keys in out.items()}

    # --- the index space the melody generator works in --------------------

    def degree_count(self) -> int:
        """How many scale positions the melody may move through."""
        if self.mode == "echo":
            return len(next(iter(self.blocks().values())))
        return len(self.keys())

    def midi_for(self, index: int, hand: Optional[str] = None) -> int:
        """Melodic index -> MIDI note.

        In ``echo`` mode the same index means a different octave depending on
        which hand plays the phrase, so ``hand`` is required there.
        """
        if self.mode == "echo":
            if hand is None:
                raise ValueError(f"layout {self.name!r} needs a hand per note")
            return self.blocks()[hand][index]
        return self.keys()[index]

    def hands_at_index(self, index: int) -> Tuple[str, ...]:
        """Which hand(s) can play a melodic index.

        In ``echo`` mode every index exists in both hands; in ``pitch`` mode
        it is the hand whose block holds that key (both, for a shared key).
        """
        if self.mode == "echo":
            return self.hands()
        midi = self.keys()[index]
        return tuple(sorted({slot.hand for slot in self.slots_for(midi)}))

    def pitch_class_at(self, index: int) -> int:
        """Pitch class of a melodic index (octave-independent in echo mode)."""
        if self.mode == "echo":
            hand = self.hands()[0]
            return self.blocks()[hand][index] % 12
        return self.keys()[index] % 12

    def pitch_classes(self) -> Tuple[int, ...]:
        return tuple(self.pitch_class_at(i) for i in range(self.degree_count()))

    def span_semitones(self) -> int:
        keys = self.keys()
        return keys[-1] - keys[0]

    # --- self-consistency -------------------------------------------------

    def validate(self) -> None:
        """Structural checks on the layout definition itself."""
        if self.mode not in ("pitch", "echo"):
            raise ValueError(f"{self.name}: unknown mode {self.mode!r}")
        if not self.slots:
            raise ValueError(f"{self.name}: no finger slots")
        for slot in self.slots:
            if not KEYBOARD_MIN_MIDI <= slot.midi <= KEYBOARD_MAX_MIDI:
                raise ValueError(
                    f"{self.name}: {slot.describe()} is outside the "
                    f"{KEYBOARD_MIN_MIDI}-{KEYBOARD_MAX_MIDI} keyboard"
                )
            if not is_white(slot.midi):
                raise ValueError(f"{self.name}: {slot.describe()} is a black key")
            if slot.hand not in ("L", "R") or not 1 <= slot.finger <= 5:
                raise ValueError(f"{self.name}: bad finger {slot.label!r}")
        labels = [slot.label for slot in self.slots]
        if len(labels) != len(set(labels)):
            raise ValueError(f"{self.name}: a finger is assigned to two keys")
        for hand, keys in self.blocks().items():
            if len(keys) != len(set(keys)):
                raise ValueError(f"{self.name}: duplicate key in hand {hand}")
        # A hand's fingers must run in the same direction as the keyboard:
        # ascending pitch means ascending finger number for the right hand and
        # descending for the left, otherwise the position is a knot.
        for hand in self.hands():
            hand_slots = sorted(
                (s for s in self.slots if s.hand == hand), key=lambda s: s.midi
            )
            fingers = [s.finger for s in hand_slots]
            expected = sorted(fingers, reverse=(hand == "L"))
            if fingers != expected:
                raise ValueError(f"{self.name}: hand {hand} fingers cross over")
        if self.mode == "echo":
            blocks = self.blocks()
            if len(blocks) != 2:
                raise ValueError(f"{self.name}: echo mode needs exactly two hands")
            lower, upper = sorted(blocks.values(), key=lambda b: b[0])
            if len(lower) != len(upper):
                raise ValueError(f"{self.name}: echo blocks differ in size")
            offsets = {high - low for low, high in zip(lower, upper)}
            if offsets != {12}:
                raise ValueError(f"{self.name}: echo blocks are not one octave apart")
        else:
            # In pitch mode the blocks may share at most one key (the thumbs
            # meeting at middle C) and must not otherwise overlap.
            blocks = self.blocks()
            if len(blocks) == 2:
                left, right = blocks["L"], blocks["R"]
                shared = set(left) & set(right)
                if len(shared) > 1:
                    raise ValueError(f"{self.name}: hands overlap on {sorted(shared)}")
                if max(left) > min(right):
                    raise ValueError(f"{self.name}: left hand reaches above the right")


def _slots(spec: Tuple[Tuple[str, int, int], ...]) -> Tuple[FingerSlot, ...]:
    return tuple(
        sorted(
            (FingerSlot(midi=midi, hand=hand, finger=finger) for hand, finger, midi in spec),
            key=lambda s: (s.midi, s.hand),
        )
    )


#: Layout registry. Keys are the values accepted by ``--layout``.
LAYOUTS: Dict[str, Layout] = {
    "middle_c": Layout(
        name="middle_c",
        description=(
            "Both hands in the classic Middle C position: thumbs meet on C4, "
            "giving nine contiguous white keys F3-G4 with no gap and no "
            "thumb-under. Supports C major and A minor."
        ),
        mode="pitch",
        slots=_slots((
            ("L", 5, 53), ("L", 4, 55), ("L", 3, 57), ("L", 2, 59), ("L", 1, 60),
            ("R", 1, 60), ("R", 2, 62), ("R", 3, 64), ("R", 4, 65), ("R", 5, 67),
        )),
    ),
    "right_c_position": Layout(
        name="right_c_position",
        description="Right hand only, C4-G4 under R1-R5. The simplest option.",
        mode="pitch",
        slots=_slots((
            ("R", 1, 60), ("R", 2, 62), ("R", 3, 64), ("R", 4, 65), ("R", 5, 67),
        )),
    ),
    "left_c_position": Layout(
        name="left_c_position",
        description="Left hand only, C3-G3 under L5-L1.",
        mode="pitch",
        slots=_slots((
            ("L", 5, 48), ("L", 4, 50), ("L", 3, 52), ("L", 2, 53), ("L", 1, 55),
        )),
    ),
    "octave_echo": Layout(
        name="octave_echo",
        description=(
            "Left hand C3-G3, right hand C4-G4: the same five-note shape an "
            "octave apart. Each phrase is played by one hand, so a repeated "
            "phrase becomes an octave echo in the other hand."
        ),
        mode="echo",
        slots=_slots((
            ("L", 5, 48), ("L", 4, 50), ("L", 3, 52), ("L", 2, 53), ("L", 1, 55),
            ("R", 1, 60), ("R", 2, 62), ("R", 3, 64), ("R", 4, 65), ("R", 5, 67),
        )),
    ),
}

for _layout in LAYOUTS.values():
    _layout.validate()


def get_layout(name: str) -> Layout:
    try:
        return LAYOUTS[name]
    except KeyError:
        raise ValueError(
            f"unknown layout {name!r}; available: {', '.join(sorted(LAYOUTS))}"
        ) from None


def describe_layouts() -> str:
    lines = []
    for name, layout in LAYOUTS.items():
        keys = " ".join(
            f"{s.label}:{note_name(s.midi)}" for s in layout.slots
        )
        lines.append(f"{name}  ({layout.mode} mode)")
        lines.append(f"    {layout.description}")
        lines.append(f"    {keys}")
    return "\n".join(lines)
