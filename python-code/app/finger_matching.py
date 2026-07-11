"""Given a MIDI note-on and the current hand landmarks, work out which
fingertip pressed the corresponding key.

Rather than a single hard yes/no pick, every visible fingertip gets a
probability that *it* pressed the key: a softmax over each tip's pixel
distance to the key's region (a tip inside the region has distance 0, so
it naturally dominates). The reported finger is still the most probable
one, but the full distribution is kept on the match, because hand
tracking can't always tell apart two adjacent fingers hovering over the
same key - e.g. the index finger pressed but the middle fingertip sits
almost as close. Downstream accuracy checks therefore don't ask "was the
argmax exactly the target finger?" but "did the *target* finger get at
least FINGER_PROBABILITY_THRESHOLD of the probability mass?" - see
is_finger_correct(), the one shared judgment used by both the live
detector and the offline analysis.

Works with any number of visible hands - 0, 1, or 2 - there is no
requirement that both be present.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .config import Config
from .hand_tracking import Hand
from .keyboard.midi_mapping import MidiMapping
from .keyboard.template import KeyboardTemplate

Point = Tuple[int, int]
Box = Tuple[int, int, int, int]

# Both knobs live in config.json's "finger_matching" section (see
# app.config.FingerMatchingConfig for what they mean and their defaults),
# loaded once here so every consumer - live detector, offline analysis,
# review video - is guaranteed to use the same values.
_cfg = Config.load().finger_matching

# Softmax sharpness. The default (25px) is roughly half a key's width, so a
# tip on the key overwhelmingly outweighs one a full key away, while two
# tips straddling the same key split the mass.
SOFTMAX_TEMPERATURE_PX = _cfg.softmax_temperature_px

# A target finger "passes" if it holds at least this share of the softmax
# mass. The default (0.4) is below 0.5 on purpose: when the target and one
# neighbouring finger are visually ambiguous (~50/50), the target still
# passes; it fails only when some *other* finger clearly dominates.
FINGER_PROBABILITY_THRESHOLD = _cfg.probability_threshold


@dataclass
class FingerMatch:
    note: int
    key_id: int
    finger: str  # the most probable fingertip
    point: Point
    distance_px: float
    inside: bool
    probability: float = 1.0  # softmax mass of `finger`
    probabilities: Dict[str, float] = field(default_factory=dict)  # every visible fingertip


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


def softmax_probabilities(distances: Dict[str, float]) -> Dict[str, float]:
    """Turn per-finger distances-to-key into a probability distribution:
    p(finger) ∝ exp(-distance / SOFTMAX_TEMPERATURE_PX). Shifted by the
    minimum distance before exponentiating, purely for numerical safety."""
    fingers = list(distances)
    d = np.array([distances[f] for f in fingers], dtype=np.float64)
    weights = np.exp(-(d - d.min()) / SOFTMAX_TEMPERATURE_PX)
    weights /= weights.sum()
    return {f: float(p) for f, p in zip(fingers, weights)}


def is_finger_correct(
    match: Optional["FingerMatch"],
    target_finger: Optional[str],
    threshold: float = FINGER_PROBABILITY_THRESHOLD,
) -> Optional[bool]:
    """The one shared "did they use the right finger?" judgment (see module
    docstring): correct iff the *target* finger's share of the softmax mass
    clears the threshold - the argmax finger being a near-tied neighbour
    doesn't count against the student. None if there's no match to judge or
    no target to judge against."""
    if match is None or target_finger is None:
        return None
    return match.probabilities.get(target_finger, 0.0) >= threshold


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
    distances: Dict[str, float] = {}

    for finger, (fx, fy) in fingertips.items():
        inside, distance = _score_finger(template.key_map, key_id, bbox, fx, fy)
        distances[finger] = distance

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

    probabilities = softmax_probabilities(distances)
    return FingerMatch(
        note=note,
        key_id=key_id,
        finger=best_finger,
        point=best_point,
        distance_px=best_distance,
        inside=best_inside,
        probability=probabilities[best_finger],
        probabilities=probabilities,
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
    note_distances: Dict[int, Dict[str, float]] = {}  # note_index -> finger -> distance

    for idx, note in enumerate(notes):
        key_id = mapping.key_for_note(note)
        if key_id is None:
            continue
        bbox = _key_bbox(key_map, key_id)
        if bbox is None:
            continue

        for finger, (fx, fy) in fingertips.items():
            inside, distance = _score_finger(key_map, key_id, bbox, fx, fy)
            note_distances.setdefault(idx, {})[finger] = distance
            candidates.append((not inside, distance, idx, key_id, finger, (fx, fy)))

    candidates.sort(key=lambda c: (c[0], c[1]))

    claimed_notes = set()
    claimed_fingers = set()
    for not_inside, distance, idx, key_id, finger, point in candidates:
        if idx in claimed_notes or finger in claimed_fingers:
            continue
        claimed_notes.add(idx)
        claimed_fingers.add(finger)
        # The distribution is over *all* fingertips, same as
        # match_note_to_finger - the greedy claiming only decides which
        # finger gets reported, not how the probability mass is shared.
        probabilities = softmax_probabilities(note_distances[idx])
        results[idx] = FingerMatch(
            note=notes[idx],
            key_id=key_id,
            finger=finger,
            point=point,
            distance_px=distance,
            inside=not not_inside,
            probability=probabilities[finger],
            probabilities=probabilities,
        )

    return results
