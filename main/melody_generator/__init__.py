"""Standalone beginner-melody generator for the piano haptic follow-up study.

Completely self-contained: it does not import, call or share data with
``main/app/sequence_generator.py`` (the controlled bimanual pilot-study
stimulus generator) and has no third-party dependencies - the Standard MIDI
File writer in :mod:`melody_generator.midi_writer` is pure stdlib.

Typical use::

    cd individual_project_2026/main
    python -m melody_generator --seed 42 --out ./melody_out

See README.md in this folder for the full option list.
"""

from .config import GeneratorConfig, MelodyConfig, RhythmConfig, TimingConfig, ValidationConfig
from .generator import MelodySequence, generate_sequence, generate_and_export
from .layouts import LAYOUTS, Layout
from .theory import KEYS, note_name

__all__ = [
    "GeneratorConfig",
    "MelodyConfig",
    "RhythmConfig",
    "TimingConfig",
    "ValidationConfig",
    "MelodySequence",
    "generate_sequence",
    "generate_and_export",
    "LAYOUTS",
    "Layout",
    "KEYS",
    "note_name",
]

__version__ = "1.0.0"
