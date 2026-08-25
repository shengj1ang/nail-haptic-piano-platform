"""Timing: beats to seconds, and the gate time that releases each key.

Two things are decided here.

**Tempo.** The grid is in beats; at the default 60 BPM one beat is exactly one
second, so ``onset_beat`` and ``note_on_time_sec`` coincide. Everything is still
computed through the tempo, so another BPM works without touching anything else.

**Gate time (articulation).** A key must be released before the next one is
pressed, otherwise consecutive notes overlap and the recording no longer has
one note sounding at a time. The default ``fixed_gap`` mode releases every note
a constant 0.25 beat before its slot ends:

    1-beat note  -> sounds 0.75 beat, 0.25 beat of silence
    2-beat note  -> sounds 1.75 beats
    3-beat note  -> sounds 2.75 beats

A constant *gap* rather than a constant *ratio* is what makes a held note still
sound held: scaling a 3-beat note by 0.75 would clip three quarters of a second
off it and it would read as a shortened note instead of a long one. The ratio
mode is kept as an option for comparison.
"""

from dataclasses import asdict, dataclass
from typing import Dict, List, Sequence, Tuple

from .config import TimingConfig
from .fingering import Placement
from .rhythm import RhythmPlan
from .theory import note_name

ROUND = 6


@dataclass(frozen=True)
class NoteEvent:
    """One note-on/note-off pair, with every field the experiment needs."""

    event_index: int
    hand: str
    finger: str
    midi_note: int
    note_name: str
    onset_beat: float
    duration_beats: float
    note_on_time_sec: float
    note_off_time_sec: float
    velocity: int
    # context, useful when checking a sequence by hand
    phrase_index: int
    scale_degree: int
    is_phrase_start: bool
    sounding_beats: float
    slot_end_beat: float
    slot_end_time_sec: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RestEvent:
    rest_index: int
    onset_beat: float
    duration_beats: float
    start_time_sec: float
    end_time_sec: float
    after_event_index: int

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def beats_to_seconds(beats: float, bpm: float) -> float:
    return beats * (60.0 / bpm)


def sounding_beats(duration_beats: float, cfg: TimingConfig) -> float:
    """How long the key is actually held, for a note occupying ``duration``."""
    if cfg.gate_mode == "fixed_gap":
        held = duration_beats - cfg.release_gap_beats
    elif cfg.gate_mode == "ratio":
        held = duration_beats * cfg.gate_ratio
    else:
        raise ValueError(f"unknown gate_mode {cfg.gate_mode!r}")
    held = max(held, cfg.min_sounding_beats)
    # Never let a note reach the next grid slot: one key at a time, always.
    return min(held, duration_beats - 1e-9)


def build_events(
    placements: Sequence[Placement],
    rhythm: RhythmPlan,
    phrase_of_note: Sequence[int],
    scale_degrees: Sequence[int],
    phrase_start_positions: Sequence[int],
    cfg: TimingConfig,
) -> Tuple[Tuple[NoteEvent, ...], Tuple[RestEvent, ...]]:
    if not (len(placements) == len(rhythm.durations) == len(rhythm.onsets)):
        raise ValueError("melody, fingering and rhythm lengths disagree")

    starts = set(phrase_start_positions)
    notes: List[NoteEvent] = []
    for position, placement in enumerate(placements):
        onset = float(rhythm.onsets[position])
        duration = float(rhythm.durations[position])
        held = sounding_beats(duration, cfg)
        is_start = position in starts
        velocity = cfg.base_velocity + (cfg.phrase_accent if is_start else 0)
        velocity = max(1, min(127, velocity))
        notes.append(
            NoteEvent(
                event_index=position,
                hand=placement.hand,
                finger=placement.label,
                midi_note=placement.midi,
                note_name=note_name(placement.midi),
                onset_beat=round(onset, ROUND),
                duration_beats=round(duration, ROUND),
                note_on_time_sec=round(beats_to_seconds(onset, cfg.bpm), ROUND),
                note_off_time_sec=round(
                    beats_to_seconds(onset + held, cfg.bpm), ROUND
                ),
                velocity=velocity,
                phrase_index=int(phrase_of_note[position]),
                scale_degree=int(scale_degrees[position]),
                is_phrase_start=is_start,
                sounding_beats=round(held, ROUND),
                slot_end_beat=round(onset + duration, ROUND),
                slot_end_time_sec=round(
                    beats_to_seconds(onset + duration, cfg.bpm), ROUND
                ),
            )
        )

    rests: List[RestEvent] = []
    for slot in rhythm.rests:
        rests.append(
            RestEvent(
                rest_index=slot.rest_index,
                onset_beat=round(float(slot.onset_beat), ROUND),
                duration_beats=round(float(slot.duration_beats), ROUND),
                start_time_sec=round(
                    beats_to_seconds(slot.onset_beat, cfg.bpm), ROUND
                ),
                end_time_sec=round(
                    beats_to_seconds(
                        slot.onset_beat + slot.duration_beats, cfg.bpm
                    ),
                    ROUND,
                ),
                after_event_index=slot.after_note,
            )
        )
    return tuple(notes), tuple(rests)
