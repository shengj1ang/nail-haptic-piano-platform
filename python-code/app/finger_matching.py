"""Given a MIDI note-on and the current hand landmarks, work out which
fingertip pressed the corresponding key.

A finger is preferred if its tip pixel actually falls inside the key's
pixel region; if none do (finger detection is imperfect, or the tip is
just outside the calibrated region), the fingertip closest to the key's
bounding box wins instead. Works with any number of visible hands - 0, 1,
or 2 - there is no requirement that both be present.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .hand_tracking import Hand
from .keyboard.midi_mapping import MidiMapping
from .keyboard.template import KeyboardTemplate

Point = Tuple[int, int]
Box = Tuple[int, int, int, int]


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


def _key_bbox(key_map: np.ndarray, key_id: int) -> Optional[Box]:
    ys, xs = np.where(key_map == key_id + 1)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _score_finger(key_map: np.ndarray, key_id: int, bbox: Box, fx: int, fy: int) -> Tuple[bool, float]:
    x1, y1, x2, y2 = bbox
    inside = False
    if 0 <= fy < key_map.shape[0] and 0 <= fx < key_map.shape[1]:
        inside = bool(key_map[fy, fx] == key_id + 1)

    # Distance to the key's bounding box (0 if already inside it).
    cx = min(max(fx, x1), x2)
    cy = min(max(fy, y1), y2)
    distance = float(np.hypot(fx - cx, fy - cy))
    return inside, distance


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

    bbox = _key_bbox(template.key_map, key_id)
    if bbox is None:
        return None

    best_finger = None
    best_point = None
    best_distance = None
    best_inside = False

    for finger, (fx, fy) in fingertips.items():
        inside, distance = _score_finger(template.key_map, key_id, bbox, fx, fy)

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


def match_notes_to_fingers(
    notes: List[int],
    template: KeyboardTemplate,
    mapping: MidiMapping,
    hands: Dict[str, Hand],
) -> List[Optional[FingerMatch]]:
    """Batch version of match_note_to_finger for notes that fired together
    (a chord). Each note is scored against every fingertip exactly like
    match_note_to_finger, but candidates are then assigned greedily,
    best pairing first, so no single fingertip is credited with two notes
    at once. Returns one result per note, same order as the input."""
    results: List[Optional[FingerMatch]] = [None] * len(notes)

    fingertips = collect_fingertips(hands)
    if not fingertips:
        return results

    key_map = template.key_map
    candidates = []  # (not_inside, distance, note_index, key_id, finger, point)

    for idx, note in enumerate(notes):
        key_id = mapping.key_for_note(note)
        if key_id is None:
            continue
        bbox = _key_bbox(key_map, key_id)
        if bbox is None:
            continue

        for finger, (fx, fy) in fingertips.items():
            inside, distance = _score_finger(key_map, key_id, bbox, fx, fy)
            candidates.append((not inside, distance, idx, key_id, finger, (fx, fy)))

    candidates.sort(key=lambda c: (c[0], c[1]))

    claimed_notes = set()
    claimed_fingers = set()
    for not_inside, distance, idx, key_id, finger, point in candidates:
        if idx in claimed_notes or finger in claimed_fingers:
            continue
        claimed_notes.add(idx)
        claimed_fingers.add(finger)
        results[idx] = FingerMatch(
            note=notes[idx],
            key_id=key_id,
            finger=finger,
            point=point,
            distance_px=distance,
            inside=not not_inside,
        )

    return results
