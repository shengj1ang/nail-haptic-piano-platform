"""
Reusable, GUI-free glue between a calibration profile (data/keyboard-profile/<name>/,
produced by setup_keyboard_wizard.py + setup_midi_mapping_wizard.py) and the physical
LED wiring in note_led_map.py.

Import this directly in any script that needs "MIDI note -> light the right
LED" without pulling in the PySide6 demo UI - test_virtual_piano_led.py is
just a thin manual-test wrapper around this.

The profile's key_id numbering is whatever order the vision wizard happened
to be clicked in (arbitrary), so it can't be zipped directly against
note_led_map.py's left-to-right wiring lists. build_note_to_leds() instead
sorts each kind ("white"/"black") by actual pixel position to recover true
physical left-to-right order first.
"""

from pathlib import Path

import numpy as np

from app.keyboard.midi_mapping import MidiMapping
from app.keyboard.template import KeyboardTemplate
from app.profiles import DATA_DIR
from common.led_controller import LEDArrayController
from note_led_map import BLACK_LEDS, NoteLEDMapper, WHITE_LEDS

LedPositions = list[tuple[int, int]]


def _left_to_right_key_ids(template: KeyboardTemplate, kind: str) -> list[int]:
    """Key ids of the given kind ("white"/"black"), in true physical
    left-to-right order (by pixel centroid) - not calibration click order."""

    def centroid_x(key_id: int) -> float:
        ys, xs = np.where(template.key_map == key_id + 1)
        return float(xs.mean())

    ids = [k.id for k in template.keys if k.kind == kind]
    return sorted(ids, key=centroid_x)


def build_ordered_notes(
    profile_name: str, data_dir: Path = DATA_DIR
) -> tuple[list[int | None], list[int | None]]:
    """This profile's white/black key notes, in true physical left-to-right
    order (by pixel centroid, not calibration click order). A None entry
    means that physical key has no MIDI note recorded yet (step 2 wasn't
    run/finished for it)."""
    profile_dir = data_dir / profile_name
    template = KeyboardTemplate.load(profile_dir / "keyboard_template.json")
    mapping = MidiMapping.load(profile_dir / "midi_mapping.json")

    white_ids = _left_to_right_key_ids(template, "white")
    black_ids = _left_to_right_key_ids(template, "black")
    return (
        [mapping.note_for_key(key_id) for key_id in white_ids],
        [mapping.note_for_key(key_id) for key_id in black_ids],
    )


def build_note_to_leds(profile_name: str, data_dir: Path = DATA_DIR) -> dict[int, LedPositions]:
    """Combine a profile's calibrated key_id -> note mapping with this rig's
    fixed LED wiring into a single note -> LED positions table."""
    white_notes, black_notes = build_ordered_notes(profile_name, data_dir)

    if len(white_notes) != len(WHITE_LEDS) or len(black_notes) != len(BLACK_LEDS):
        raise ValueError(
            f"profile {profile_name!r} has {len(white_notes)} white / {len(black_notes)} black keys, "
            f"but this LED rig is wired for exactly {len(WHITE_LEDS)} white / {len(BLACK_LEDS)} black "
            "keys - it doesn't support a different key count."
        )

    note_to_leds: dict[int, LedPositions] = {}
    for note, leds in zip(white_notes, WHITE_LEDS):
        if note is not None:
            note_to_leds[note] = leds
    for note, leds in zip(black_notes, BLACK_LEDS):
        if note is not None:
            note_to_leds[note] = leds

    return note_to_leds


def build_mapper(led: LEDArrayController, profile_name: str, data_dir: Path = DATA_DIR) -> NoteLEDMapper:
    """Convenience one-shot: load a profile and hand back a ready-to-use NoteLEDMapper."""
    return NoteLEDMapper(led, build_note_to_leds(profile_name, data_dir))
