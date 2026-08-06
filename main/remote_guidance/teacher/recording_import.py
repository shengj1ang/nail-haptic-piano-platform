"""Turn an existing song folder into an uploadable recording.

The asynchronous tele-training mode is "the teacher records a fingering
plan once and the student plays it back locally". The platform already
produces exactly that artefact - data/music/<name>/ from the Song
Recording Wizard, or data/sequence/<name>/ from the Experiment Sequence
Generator - so this module converts, it does not re-record.

Everything comes from the existing loaders:

    app.song_library.list_song_entries  which songs exist, and where
    app.music_recording.load_playback_events
                                       fingering.json merged with the
                                       real note_on/note_off durations
                                       from raw/midi_raw.json
    app.music_recording.SongMeta        title, difficulty, note count

score.mid is not uploaded and neither is the video. What the student
needs to drive cues is the note/finger/timing list, and keeping the media
out means the relay's database stays small and no performance footage
leaves the teacher's machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.music_recording import (
    FINGERING_FILENAME,
    META_FILENAME,
    SCORE_MIDI_FILENAME,
    SongMeta,
    load_playback_events,
    song_dir,
)
from app.song_library import SongEntry, find_song_entry, list_song_entries


@dataclass
class ImportedRecording:
    """A recording ready for POST /rooms/{id}/recordings."""

    name: str
    source: str  # "music" or "sequence" - which library it came from
    events: List[Dict[str, Any]]
    meta: Dict[str, Any]

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def duration_s(self) -> float:
        if not self.events:
            return 0.0
        return max(e["rel_time_s"] + e["duration_s"] for e in self.events)

    def to_payload(self) -> Dict[str, Any]:
        return {"name": self.name, "source": self.source, "meta": self.meta, "events": self.events}


def list_available_songs() -> List[SongEntry]:
    """Both libraries, labelled "music/<name>" / "sequence/<name>" - the
    same picker list the local quiz and playback tools show."""
    return list_song_entries()


def import_song(label: str) -> ImportedRecording:
    """Convert one library entry, addressed by its picker label."""
    entry = find_song_entry(label)
    if entry is None:
        raise FileNotFoundError(f"no song called {label!r} under data/music/ or data/sequence/")
    return import_song_entry(entry)


def import_song_entry(entry: SongEntry) -> ImportedRecording:
    directory = song_dir(entry.name, entry.data_dir)
    if not (directory / FINGERING_FILENAME).exists():
        raise FileNotFoundError(
            f"{directory / FINGERING_FILENAME} is missing - this song has no fingering to guide with"
        )

    playback_events = load_playback_events(entry.name, data_dir=entry.data_dir)
    events = [
        {
            "event_order": order,
            "rel_time_s": float(event.time),
            "duration_s": float(event.duration),
            "note": int(event.note),
            "note_name": event.note_name,
            "key_id": event.key_id,
            "finger": event.finger,
        }
        for order, event in enumerate(playback_events)
    ]

    meta = _song_meta(directory)
    meta.update(
        {
            "library": entry.label.split("/", 1)[0],
            "song_name": entry.name,
            "unresolved_fingers": sum(1 for e in events if not e["finger"]),
            # Recorded so the student can tell whether a score file exists
            # on the teacher's side, without it being uploaded.
            "has_score_midi": (directory / SCORE_MIDI_FILENAME).exists(),
            "media_uploaded": False,
        }
    )
    return ImportedRecording(
        name=entry.name,
        source=entry.label.split("/", 1)[0],
        events=events,
        meta=meta,
    )


def _song_meta(directory: Path) -> Dict[str, Any]:
    """meta.json if the song has one. A missing or unreadable meta is not
    fatal - the fingering is what guidance needs."""
    path = directory / META_FILENAME
    if not path.exists():
        return {}
    try:
        song_meta = SongMeta.load(path)
    except (ValueError, TypeError, OSError):
        return {}
    return {
        "title": song_meta.title,
        "difficulty": song_meta.difficulty,
        "created_at": song_meta.created_at,
        "duration_s": song_meta.duration_s,
        "note_count": song_meta.note_count,
        "generation_seed": song_meta.generation_seed,
    }


def describe(recording: ImportedRecording) -> str:
    """One line for a picker or a status bar."""
    unresolved = recording.meta.get("unresolved_fingers", 0)
    text = f"{recording.name}: {recording.event_count} events, {recording.duration_s:.1f}s"
    if unresolved:
        text += f" ({unresolved} without a resolved finger)"
    return text


def events_from_detail(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The student side of the same shape: the event list out of
    GET /recordings/{id}, sorted the way it will be played."""
    events: List[Dict[str, Any]] = list(detail.get("events") or [])
    events.sort(key=lambda e: (e.get("event_order", 0), e.get("rel_time_s", 0.0)))
    return events


def find_entry_for_name(name: str) -> Optional[SongEntry]:
    for entry in list_song_entries():
        if entry.name == name:
            return entry
    return None
