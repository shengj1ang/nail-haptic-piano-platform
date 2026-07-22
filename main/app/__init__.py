"""FingerAccuracy - camera + MIDI based piano finger-usage detection.

Public API, usable both live and offline (see README.md):

    from app import (
        Config, Camera, HandTracker, MidiListener, MidiEvent,
        save_midi_log, load_midi_log, KeyboardTemplate, MidiMapping,
        match_note_to_finger, analyze_recording,
    )
"""

from .camera import Camera
from .config import Config
from .finger_matching import (
    FINGER_PROBABILITY_THRESHOLD,
    FingerMatch,
    collect_fingertips,
    is_finger_correct,
    match_note_to_finger,
    match_notes_to_fingers,
    softmax_probabilities,
)
from .hand_tracking import Hand, HandTracker
from .keyboard import KeyBox, KeyboardTemplate, MidiMapping, note_name
from .midi import MidiEvent, MidiListener, list_input_ports, load_midi_log, save_midi_log
from .offline import analyze_recording

__all__ = [
    "Config",
    "Camera",
    "HandTracker",
    "Hand",
    "FingerMatch",
    "FINGER_PROBABILITY_THRESHOLD",
    "collect_fingertips",
    "is_finger_correct",
    "match_note_to_finger",
    "match_notes_to_fingers",
    "softmax_probabilities",
    "MidiListener",
    "MidiEvent",
    "list_input_ports",
    "save_midi_log",
    "load_midi_log",
    "KeyboardTemplate",
    "KeyBox",
    "MidiMapping",
    "note_name",
    "analyze_recording",
]
