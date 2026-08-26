"""Read back a melody this package wrote, for playback and inspection.

:mod:`export` writes four files per melody; this is the other direction. The
``.json`` is the authoritative record - it carries the fingering, which a MIDI
file cannot - so that is what a loaded melody is built from, and the sibling
``.mid`` is parsed as a cross-check that the two still agree. A ``.mid`` with
no ``.json`` beside it can still be loaded and played, just without fingering.

Nothing here depends on the generator: a melody can be loaded, checked and
played long after the code that produced it has changed.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .midi_writer import read_midi_file
from .theory import note_name as _note_name

FORMAT_PREFIX = "melody_generator/"


@dataclass(frozen=True)
class LoadedNote:
    """One note-on/note-off pair, with the fields playback needs."""

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


@dataclass(frozen=True)
class LoadedRest:
    rest_index: int
    onset_beat: float
    duration_beats: float
    start_time_sec: float
    end_time_sec: float


@dataclass(frozen=True)
class LoadedMelody:
    name: str
    json_path: Optional[Path]
    midi_path: Optional[Path]
    seed: Optional[int]
    key_display: str
    layout: str
    bpm: float
    notes: Tuple[LoadedNote, ...]
    rests: Tuple[LoadedRest, ...]
    total_seconds: float
    key_to_finger: Dict[int, str]
    fingers_used: Tuple[str, ...]
    musicality: Optional[float]
    difficulty: Optional[float]
    validation_ok: Optional[bool]
    #: None when there is no .mid to compare against; otherwise whether the
    #: MIDI file's note-on times and pitches match the .json's.
    midi_agrees: Optional[bool]
    midi_note_count: Optional[int]

    @property
    def has_fingering(self) -> bool:
        return any(note.finger for note in self.notes)

    def summary_line(self) -> str:
        bits = [self.name]
        if self.key_display:
            bits.append(self.key_display)
        if self.layout:
            bits.append(self.layout)
        bits.append(f"{len(self.notes)} notes")
        if self.rests:
            bits.append(f"{len(self.rests)} rests")
        bits.append(f"{self.total_seconds:.1f} s")
        return "  |  ".join(bits)


class MelodyLoadError(RuntimeError):
    pass


def list_melodies(folder: Path) -> List[Path]:
    """Every melody in ``folder``, as the path to play.

    A ``.json`` written by this package is preferred; a ``.mid`` with no
    ``.json`` beside it is listed too, so nothing in the folder is invisible.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out: List[Path] = []
    seen_stems = set()
    for path in sorted(folder.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                head = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if str(head.get("format", "")).startswith(FORMAT_PREFIX):
            out.append(path)
            seen_stems.add(path.stem)
    for path in sorted(folder.glob("*.mid")):
        if path.stem not in seen_stems:
            out.append(path)
    return out


def load_melody(path: Path) -> LoadedMelody:
    """Load a melody from its ``.json`` (preferred) or bare ``.mid``."""
    path = Path(path)
    if path.suffix.lower() == ".mid":
        sibling = path.with_suffix(".json")
        if sibling.is_file():
            path = sibling
        else:
            return _load_from_midi(path)
    return _load_from_json(path)


def _load_from_json(path: Path) -> LoadedMelody:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise MelodyLoadError(f"cannot read {path.name}: {error}") from None
    except json.JSONDecodeError as error:
        raise MelodyLoadError(f"{path.name} is not valid JSON: {error}") from None

    if not str(data.get("format", "")).startswith(FORMAT_PREFIX):
        raise MelodyLoadError(
            f"{path.name} was not written by melody_generator "
            f"(format {data.get('format')!r})"
        )

    music = data.get("music", {})
    events = data.get("events", [])
    if not events:
        raise MelodyLoadError(f"{path.name} contains no note events")

    notes = tuple(
        LoadedNote(
            event_index=int(event.get("event_index", index)),
            hand=str(event.get("hand", "")),
            finger=str(event.get("finger", "")),
            midi_note=int(event["midi_note"]),
            note_name=str(event.get("note_name") or _note_name(int(event["midi_note"]))),
            onset_beat=float(event.get("onset_beat", 0.0)),
            duration_beats=float(event.get("duration_beats", 0.0)),
            note_on_time_sec=float(event["note_on_time_sec"]),
            note_off_time_sec=float(event["note_off_time_sec"]),
            velocity=int(event.get("velocity", 80)),
        )
        for index, event in enumerate(events)
    )
    rests = tuple(
        LoadedRest(
            rest_index=int(rest.get("rest_index", index)),
            onset_beat=float(rest.get("onset_beat", 0.0)),
            duration_beats=float(rest.get("duration_beats", 0.0)),
            start_time_sec=float(rest.get("start_time_sec", 0.0)),
            end_time_sec=float(rest.get("end_time_sec", 0.0)),
        )
        for index, rest in enumerate(data.get("rests", []))
    )

    fingering = data.get("fingering", {})
    key_to_finger = {
        int(midi): str(label)
        for midi, label in (fingering.get("key_to_finger") or {}).items()
    }
    scores = data.get("scores", {})
    validation = data.get("validation", {})

    total_seconds = float(
        music.get("total_seconds")
        or max((note.note_off_time_sec for note in notes), default=0.0)
    )

    midi_path = path.with_suffix(".mid")
    agrees, midi_count = _cross_check_midi(midi_path, notes)

    return LoadedMelody(
        name=str(data.get("name") or path.stem),
        json_path=path,
        midi_path=midi_path if midi_path.is_file() else None,
        seed=data.get("seed"),
        key_display=str(music.get("key_display", "")),
        layout=str(music.get("layout", "")),
        bpm=float(music.get("bpm", 60.0)),
        notes=notes,
        rests=rests,
        total_seconds=total_seconds,
        key_to_finger=key_to_finger,
        fingers_used=tuple(fingering.get("fingers_used") or ()),
        musicality=scores.get("musicality"),
        difficulty=scores.get("difficulty"),
        validation_ok=validation.get("ok"),
        midi_agrees=agrees,
        midi_note_count=midi_count,
    )


def _cross_check_midi(
    midi_path: Path, notes: Tuple[LoadedNote, ...]
) -> Tuple[Optional[bool], Optional[int]]:
    """Do the .mid and the .json still describe the same performance?"""
    if not midi_path.is_file():
        return None, None
    try:
        parsed = read_midi_file(midi_path)
    except (OSError, ValueError, IndexError):
        return False, None
    midi_notes = sorted(parsed["notes"], key=lambda n: (n["on_beat"], n["midi_note"]))
    if len(midi_notes) != len(notes):
        return False, len(midi_notes)
    beat_seconds = 60.0 / float(parsed["bpm"] or 60.0)
    for expected, actual in zip(notes, midi_notes):
        if expected.midi_note != actual["midi_note"]:
            return False, len(midi_notes)
        if abs(actual["on_beat"] * beat_seconds - expected.note_on_time_sec) > 0.01:
            return False, len(midi_notes)
    return True, len(midi_notes)


def _load_from_midi(path: Path) -> LoadedMelody:
    """A .mid with no .json beside it: playable, but with no fingering."""
    try:
        parsed = read_midi_file(path)
    except (OSError, ValueError, IndexError) as error:
        raise MelodyLoadError(f"cannot read {path.name}: {error}") from None
    beat_seconds = 60.0 / float(parsed["bpm"] or 60.0)
    notes = tuple(
        LoadedNote(
            event_index=index,
            hand="",
            finger="",
            midi_note=int(event["midi_note"]),
            note_name=_note_name(int(event["midi_note"])),
            onset_beat=float(event["on_beat"]),
            duration_beats=float(event["off_beat"] - event["on_beat"]),
            note_on_time_sec=float(event["on_beat"]) * beat_seconds,
            note_off_time_sec=float(event["off_beat"]) * beat_seconds,
            velocity=int(event.get("velocity", 80)),
        )
        for index, event in enumerate(
            sorted(parsed["notes"], key=lambda n: (n["on_beat"], n["midi_note"]))
        )
    )
    if not notes:
        raise MelodyLoadError(f"{path.name} contains no notes")
    return LoadedMelody(
        name=path.stem,
        json_path=None,
        midi_path=path,
        seed=None,
        key_display="",
        layout="",
        bpm=float(parsed["bpm"]),
        notes=notes,
        rests=(),
        total_seconds=max(note.note_off_time_sec for note in notes),
        key_to_finger={},
        fingers_used=(),
        musicality=None,
        difficulty=None,
        validation_ok=None,
        midi_agrees=None,
        midi_note_count=len(notes),
    )
