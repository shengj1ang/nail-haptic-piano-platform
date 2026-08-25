"""Minimal music theory: note names, white-key scales, keys and scale degrees.

Only what a white-key, single-voice, beginner melody needs. Pitch is always a
MIDI number with middle C = 60 = "C4", matching the note naming already used
elsewhere in this project.
"""

from dataclasses import dataclass
from typing import Dict, Tuple

from .config import WHITE_PITCH_CLASSES

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Ascending white-key pitch classes, the raw material for every key here.
WHITE_SCALE = (0, 2, 4, 5, 7, 9, 11)


def note_name(midi: int) -> str:
    """MIDI number -> scientific pitch name (60 -> "C4")."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def is_white(midi: int) -> bool:
    return midi % 12 in WHITE_PITCH_CLASSES


@dataclass(frozen=True)
class Key:
    """A white-key key: a tonic plus the roles its pitch classes play.

    ``stable_pcs`` are the tonic-triad members - safe places to begin a phrase
    and to sit on. ``open_pcs`` are the tones that sound unfinished, used to
    end every phrase except the last one, which must land on the tonic.
    """

    name: str
    display: str
    tonic_pc: int
    stable_pcs: Tuple[int, ...]
    open_pcs: Tuple[int, ...]

    def scale(self) -> Tuple[int, ...]:
        """Pitch classes ordered from the tonic upwards."""
        start = WHITE_SCALE.index(self.tonic_pc)
        return tuple(WHITE_SCALE[(start + i) % 7] for i in range(7))

    def degree_of(self, pc: int) -> int:
        """Scale degree (1..7) of a pitch class in this key."""
        return self.scale().index(pc % 12) + 1


KEYS: Dict[str, Key] = {
    "c_major": Key(
        name="c_major",
        display="C major",
        tonic_pc=0,
        stable_pcs=(0, 4, 7),      # C E G
        open_pcs=(2, 7, 11),       # D G B - supertonic, dominant, leading tone
    ),
    "a_minor": Key(
        name="a_minor",
        display="A natural minor",
        tonic_pc=9,
        stable_pcs=(9, 0, 4),      # A C E
        open_pcs=(11, 4, 7),       # B E G
    ),
}


def get_key(name: str) -> Key:
    try:
        return KEYS[name]
    except KeyError:
        raise ValueError(
            f"unknown key {name!r}; available: {', '.join(sorted(KEYS))}"
        ) from None
