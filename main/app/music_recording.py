"""Reusable, GUI-free pieces of the "teacher records a song" pipeline
(see app/gui/recording_wizard.py, music_recording_wizard.py).

A recording session produces two kinds of files under
data/music/<song_name>/:

  - the "score": score.mid (a standard MIDI file, playable/reusable by any
    tool) and fingering.json (which finger the teacher used for every note
    in that file, matched via app.finger_matching against the recorded
    video - see app.offline.analyze_recording), plus meta.json (title,
    difficulty, profile, ...).
  - raw/: the untouched recording this was computed from, kept for later
    diagnosis - performance.mp4, midi_raw.json (every MIDI message with a
    time.time() timestamp), notes.json (note-on events only, timestamped
    the same way app.midi.MidiListener does, ready for
    app.offline.analyze_recording), and sync.json (see below).

Video and MIDI capture start on separate clocks (a camera pipeline and a
MIDI thread), so their timestamps can drift apart over a long recording.
The convention here is the same "flash-frame sync" trick used with
clapperboards: right as a recording starts, the first white key's
backlight LED is switched fully on and then off while both the camera and
the MIDI reader are already running, and the wall-clock time.time() of
each switch is saved to sync.json. Anyone reviewing the raw recording
later can compare that logged on/off time against the video frame the
flash actually appears/disappears on to measure (and correct for) any
clock drift.
"""

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import mido

from .keyboard.midi_mapping import note_name
from .midi import MidiEvent, list_input_ports

MUSIC_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "music"

RAW_VIDEO_FILENAME = "performance.mp4"
RAW_MIDI_FILENAME = "midi_raw.json"
RAW_NOTES_FILENAME = "notes.json"
RAW_SYNC_FILENAME = "sync.json"
SCORE_MIDI_FILENAME = "score.mid"
FINGERING_FILENAME = "fingering.json"
META_FILENAME = "meta.json"

_INVALID_NAME_CHARS = '\\/:*?"<>|'


def sanitize_song_name(name: str) -> str:
    """Turn a teacher-entered song title into a filesystem-safe folder name.
    Only strips characters that are actually invalid in a path - anything
    else (including non-ASCII titles) is kept as-is."""
    cleaned = "".join("_" if ch in _INVALID_NAME_CHARS else ch for ch in name.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "untitled"


def song_dir(song_name: str, data_dir: Path = MUSIC_DATA_DIR) -> Path:
    return data_dir / song_name


def raw_dir(song_name: str, data_dir: Path = MUSIC_DATA_DIR) -> Path:
    return song_dir(song_name, data_dir) / "raw"


def list_songs(data_dir: Path = MUSIC_DATA_DIR) -> List[str]:
    """Song names under data/music/<name>/ that have finished saving (i.e.
    have a meta.json) - for a playback tool's song picker."""
    if not data_dir.exists():
        return []
    return sorted(p.name for p in data_dir.iterdir() if (p / META_FILENAME).exists())


# ---------------------------------------------------------------------------
# Raw MIDI capture - every message (not just note-on), each timestamped with
# both time.time() (for cross-referencing against sync.json/video) and a
# recorder-relative time (the same convention as app.midi.MidiListener).
# ---------------------------------------------------------------------------


@dataclass
class RawMidiEvent:
    abs_time: float  # absolute wall-clock time.time() when the message was received
    type: str  # "note_on" or "note_off"
    note: int
    velocity: int


class RawMidiRecorder:
    """Like app.midi.MidiListener, but keeps every note_on/note_off message
    (not just note-on), each stamped with an absolute wall-clock
    time.time() value, for the raw diagnostic log (see module docstring)."""

    def __init__(self, port_name: Optional[str] = None):
        available = list_input_ports()

        if port_name is None:
            port_name = available[0] if available else None
        if port_name is None:
            raise RuntimeError("No MIDI input ports available.")
        if port_name not in available:
            raise RuntimeError(f"MIDI port '{port_name}' not found. Available: {available}")

        self.port_name = port_name
        self.start_time = time.time()
        self._events: deque = deque()
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with mido.open_input(self.port_name) as port:
                while self._running:
                    for msg in port.iter_pending():
                        if msg.type not in ("note_on", "note_off"):
                            continue
                        velocity = int(getattr(msg, "velocity", 0))
                        msg_type = "note_on" if (msg.type == "note_on" and velocity > 0) else "note_off"
                        event = RawMidiEvent(
                            abs_time=time.time(),
                            type=msg_type,
                            note=int(msg.note),
                            velocity=velocity,
                        )
                        with self._lock:
                            self._events.append(event)
                    time.sleep(0.001)
        except Exception as e:
            print(f"Raw MIDI recorder error on port {self.port_name}: {e}")

    def pop_events(self) -> List[RawMidiEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def close(self) -> None:
        self._running = False


def save_raw_midi_log(events: List[RawMidiEvent], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in events], f, indent=2)


def load_raw_midi_log(path: Path) -> List[RawMidiEvent]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Older logs also carried a recorder-relative rel_time - ignored now;
    # abs_time is the one wall-clock format used everywhere.
    return [
        RawMidiEvent(abs_time=item["abs_time"], type=item["type"], note=item["note"], velocity=item["velocity"])
        for item in data
    ]


def notes_only(events: List[RawMidiEvent]) -> List[MidiEvent]:
    """The note-on subset of a raw log, in app.midi's MidiEvent format
    (absolute wall-clock time), ready for app.offline.analyze_recording."""
    return [MidiEvent(time=e.abs_time, note=e.note) for e in events if e.type == "note_on"]


def first_note_on_time(events: List[RawMidiEvent]) -> float:
    """Wall-clock moment of the first real note (after the LED sync flash
    and however long the performer takes to actually start) - see
    trim_to_first_note()."""
    times = [e.abs_time for e in events if e.type == "note_on"]
    return min(times) if times else 0.0


def trim_to_first_note(events: List[RawMidiEvent]) -> List[RawMidiEvent]:
    """Shift every event onto a song-position clock: the first note_on
    lands at 0, events before it are dropped. The result's abs_time is
    deliberately no longer a wall-clock value - it's a position within the
    piece, only ever used to build the saved score/fingering (so a song
    starts playing back the instant it's pressed play). Never applied to
    raw/, which has to stay on absolute wall-clock time to stay lined up
    with the video (see app.offline.analyze_recording)."""
    t0 = first_note_on_time(events)
    if t0 <= 0:
        return list(events)
    return [
        RawMidiEvent(abs_time=e.abs_time - t0, type=e.type, note=e.note, velocity=e.velocity)
        for e in events
        if e.abs_time >= t0
    ]


def build_score_midi(events: List[RawMidiEvent], path: Path, bpm: float = 120.0, ticks_per_beat: int = 480) -> None:
    """Write a standard .mid file from a raw log's note_on/note_off pairs,
    using each event's recorder-relative time to place it. bpm is an
    arbitrary encoding constant (this is a captured performance, not a
    musical transcription) - it and ticks_per_beat only need to be fine
    enough to place events at their real elapsed time with sub-10ms
    precision, which the defaults comfortably are."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))

    seconds_per_tick = (60.0 / bpm) / ticks_per_beat
    last_tick = 0
    for e in sorted((e for e in events if e.type in ("note_on", "note_off")), key=lambda e: e.abs_time):
        tick = max(int(round(e.abs_time / seconds_per_tick)), 0)
        delta = max(tick - last_tick, 0)
        last_tick = tick
        velocity = e.velocity if e.type == "note_on" else 0
        track.append(mido.Message(e.type, note=e.note, velocity=velocity, time=delta))

    mid.save(str(path))


# ---------------------------------------------------------------------------
# Sync marks (LED flash calibration)
# ---------------------------------------------------------------------------


@dataclass
class SyncInfo:
    video_start_time: float  # time.time() when video capture began
    midi_start_time: float  # time.time() when the MIDI recorder began (RawMidiRecorder.start_time)
    led_on_time: float  # time.time() when the sync flash switched the sync LED on
    led_off_time: float  # time.time() when the sync flash switched it back off

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "SyncInfo":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))


# ---------------------------------------------------------------------------
# Song metadata + fingering annotations
# ---------------------------------------------------------------------------


@dataclass
class SongMeta:
    # No keyboard_profile_name/port_name here - a song outlives any one
    # profile/port, and freezing them in at record time just goes stale the
    # next time the keyboard is recalibrated or plugged into a different
    # port. Whatever uses this song (student_quiz.py, music_playback.py)
    # reads the *current* Config for that instead.
    title: str
    difficulty: int  # 1, 2, or 3 - a label only, not otherwise interpreted here
    created_at: float  # absolute wall-clock time.time() timestamp
    duration_s: float
    note_count: int
    # Effective generation note bounds, present only on generated
    # sequences (app.sequence_generator.save_sequence_as_song): the
    # k_min/k_max the span-normalised difficulty components were computed
    # against, so the metrics viewer can recompute them identically.
    # Real recordings (app/gui/recording_wizard.py) leave these None.
    start_note: Optional[int] = None
    end_note: Optional[int] = None
    # Stimulus-generation provenance, present only on generated sequences:
    # rerunning the generator with the same calibrated profile, the same
    # start/end notes, the same per-level count, the same seed, and the
    # same software version reproduces the batch this sequence came from
    # exactly. The seed alone is not enough - it only fixes the random
    # stream; the settings decide how that stream is consumed.
    generation_seed: Optional[int] = None
    generation_count: Optional[int] = None

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "SongMeta":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))


@dataclass
class FingeringEntry:
    time: float  # seconds, same relative clock as raw/notes.json
    note: int
    note_name: str
    key_id: Optional[int]
    finger: Optional[str]
    inside: bool


def build_fingering_entries(notes: List[MidiEvent], matches: List) -> List[FingeringEntry]:
    """Zip a note-on log with app.offline.analyze_recording's per-note
    FingerMatch results (None where nothing could be resolved) into the
    saved fingering.json format."""
    entries = []
    for note_event, match in zip(notes, matches):
        entries.append(
            FingeringEntry(
                time=note_event.time,
                note=note_event.note,
                note_name=note_name(note_event.note),
                key_id=match.key_id if match else None,
                finger=match.finger if match else None,
                inside=bool(match.inside) if match else False,
            )
        )
    return entries


def save_fingering(entries: List[FingeringEntry], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in entries], f, indent=2)


def load_fingering(path: Path) -> List[FingeringEntry]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [FingeringEntry(**item) for item in data]


# ---------------------------------------------------------------------------
# Playback - turning a saved song back into a timed sequence of "this note,
# this finger, held for this long" events, for a UI to step through.
# ---------------------------------------------------------------------------

DEFAULT_NOTE_DURATION_S = 0.4  # used when raw/midi_raw.json is missing or has no matching note_off


@dataclass
class PlaybackEvent:
    time: float  # seconds from the start of the recording, same clock as fingering.json
    duration: float  # seconds the key was actually held, if known (else DEFAULT_NOTE_DURATION_S)
    note: int
    note_name: str
    key_id: Optional[int]
    finger: Optional[str]  # "L1".."L5"/"R1".."R5", or None if unresolved


def _note_durations(raw_events: List[RawMidiEvent]) -> Dict[int, List[float]]:
    """note -> durations (seconds) of each note_on/note_off pair for that
    note, in chronological order, by pairing them up FIFO per note number."""
    pending: Dict[int, List[float]] = {}
    durations: Dict[int, List[float]] = {}
    for e in sorted(raw_events, key=lambda e: e.abs_time):
        if e.type == "note_on":
            pending.setdefault(e.note, []).append(e.abs_time)
        elif e.type == "note_off":
            queue = pending.get(e.note)
            if queue:
                start = queue.pop(0)
                durations.setdefault(e.note, []).append(max(e.abs_time - start, 0.05))
    return durations


def load_playback_events(song_name: str, data_dir: Path = MUSIC_DATA_DIR) -> List[PlaybackEvent]:
    """fingering.json plus (if present) raw/midi_raw.json's real note_on/
    note_off timing, merged into one played-back-in-order event list."""
    with open(song_dir(song_name, data_dir) / FINGERING_FILENAME, encoding="utf-8") as f:
        fingering = json.load(f)

    raw_path = raw_dir(song_name, data_dir) / RAW_MIDI_FILENAME
    durations_by_note = _note_durations(load_raw_midi_log(raw_path)) if raw_path.exists() else {}
    next_duration_idx: Dict[int, int] = {}

    events = []
    for item in fingering:
        note = int(item["note"])
        queue = durations_by_note.get(note, [])
        idx = next_duration_idx.get(note, 0)
        duration = queue[idx] if idx < len(queue) else DEFAULT_NOTE_DURATION_S
        next_duration_idx[note] = idx + 1

        events.append(
            PlaybackEvent(
                time=float(item["time"]),
                duration=duration,
                note=note,
                note_name=item["note_name"],
                key_id=item.get("key_id"),
                finger=item.get("finger"),
            )
        )

    events.sort(key=lambda e: e.time)
    return events
