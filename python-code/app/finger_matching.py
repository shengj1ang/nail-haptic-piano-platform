"""Given a MIDI note-on and the current hand landmarks, work out which
fingertip pressed the corresponding key.

A finger is preferred if its tip pixel actually falls inside the key's
pixel region; if none do (finger detection is imperfect, or the tip is
just outside the calibrated region), the fingertip closest to the key's
bounding box wins instead. Works with any number of visible hands - 0, 1,
or 2 - there is no requirement that both be present.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

from .hand_tracking import Hand
from .keyboard.midi_mapping import MidiMapping
from .keyboard.template import KeyboardTemplate

Point = Tuple[int, int]


@dataclass
class FingerMatch:
    note: int
    key_id: int
    finger: str
    point: Point
    distance_px: float
    inside: bool


def collect_fingertips(hands: Dict[str, Hand]) -> Dict[str, Point]:
    fingertips: Dict[str, Point] = {}
    for hand in hands.values():
        fingertips.update(hand.fingertips)
    return fingertips


def match_note_to_finger(
    note: int,
    template: KeyboardTemplate,
    mapping: MidiMapping,
    hands: Dict[str, Hand],
) -> Optional[FingerMatch]:
    key_id = mapping.key_for_note(note)
    if key_id is None:
        return None

    fingertips = collect_fingertips(hands)
    if not fingertips:
        return None

    key_map = template.key_map
    target_value = key_id + 1
    ys, xs = np.where(key_map == target_value)
    if len(xs) == 0:
        return None

    x1, y1, x2, y2 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())

    best_finger = None
    best_point = None
    best_distance = None
    best_inside = False

    for finger, (fx, fy) in fingertips.items():
        inside = False
        if 0 <= fy < key_map.shape[0] and 0 <= fx < key_map.shape[1]:
            inside = bool(key_map[fy, fx] == target_value)

        # Distance to the key's bounding box (0 if already inside it).
        cx = min(max(fx, x1), x2)
        cy = min(max(fy, y1), y2)
        distance = float(np.hypot(fx - cx, fy - cy))

        better = (
            best_finger is None
            or (inside and not best_inside)
            or (inside == best_inside and distance < best_distance)
        )
        if better:
            best_finger = finger
            best_point = (fx, fy)
            best_distance = distance
            best_inside = inside

    if best_finger is None:
        return None

    return FingerMatch(
        note=note,
        key_id=key_id,
        finger=best_finger,
        point=best_point,
        distance_px=best_distance,
        inside=best_inside,
    )
