"""A minimal Standard MIDI File writer (and reader, for self-checking).

Pure standard library on purpose: this generator has no third-party
dependencies, so it runs on any Python 3.9+ with nothing installed. The output
is an ordinary format-0 SMF that any player, DAW or MIDI library opens.

File layout produced::

    MThd  format 0, 1 track, 480 ticks per quarter note
    MTrk  track name, time signature 4/4, tempo, program change,
          note on/off pairs in tick order, end of track
"""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_TICKS_PER_BEAT = 480
NOTE_OFF = 0x80
NOTE_ON = 0x90
PROGRAM_CHANGE = 0xC0
META = 0xFF


@dataclass(frozen=True)
class MidiNote:
    """What the writer needs to know about one note."""

    midi_note: int
    velocity: int
    on_beat: float
    off_beat: float
    channel: int = 0


def _vlq(value: int) -> bytes:
    """MIDI variable-length quantity."""
    if value < 0:
        raise ValueError("negative delta time")
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([META, kind]) + _vlq(len(payload)) + payload


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack(">I", len(payload)) + payload


def write_midi_file(
    path: Path,
    notes: Sequence[MidiNote],
    bpm: float,
    track_name: str = "melody",
    ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
    program: int = 0,
    channels: Optional[Iterable[int]] = None,
) -> Path:
    """Write ``notes`` to ``path`` as a format-0 Standard MIDI File."""
    if bpm <= 0:
        raise ValueError("bpm must be positive")
    microseconds_per_beat = int(round(60_000_000 / bpm))

    events: List[Tuple[int, int, bytes]] = []
    for note in notes:
        on_tick = int(round(note.on_beat * ticks_per_beat))
        off_tick = int(round(note.off_beat * ticks_per_beat))
        if off_tick <= on_tick:
            off_tick = on_tick + 1
        # sort key 0 for note-off so a repeated key is released before it is
        # struck again when two events land on the same tick
        events.append(
            (off_tick, 0, bytes([NOTE_OFF | note.channel, note.midi_note, 0]))
        )
        events.append(
            (
                on_tick,
                1,
                bytes([NOTE_ON | note.channel, note.midi_note, note.velocity]),
            )
        )
    events.sort(key=lambda item: (item[0], item[1]))

    used_channels = sorted(set(channels)) if channels else sorted(
        {note.channel for note in notes} or {0}
    )

    body = bytearray()
    body += _vlq(0) + _meta(0x03, track_name.encode("utf-8"))
    body += _vlq(0) + _meta(0x58, bytes([4, 2, 24, 8]))  # 4/4
    body += _vlq(0) + _meta(
        0x51, struct.pack(">I", microseconds_per_beat)[1:]
    )
    for channel in used_channels:
        body += _vlq(0) + bytes([PROGRAM_CHANGE | channel, program])

    previous_tick = 0
    for tick, _, message in events:
        body += _vlq(tick - previous_tick)
        body += message
        previous_tick = tick
    body += _vlq(0) + _meta(0x2F, b"")  # end of track

    header = struct.pack(">HHH", 0, 1, ticks_per_beat)
    data = _chunk(b"MThd", header) + _chunk(b"MTrk", bytes(body))
    path = Path(path)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# reader - only rich enough to verify what this module wrote
# --------------------------------------------------------------------------

def _read_vlq(data: bytes, position: int) -> Tuple[int, int]:
    value = 0
    while True:
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, position


def read_midi_file(path: Path) -> Dict[str, object]:
    """Parse back a file written by :func:`write_midi_file`.

    Returns the tempo, the ticks per beat and the note-on/note-off pairs in
    beats, so a test can assert that what was written is what was intended.
    """
    data = Path(path).read_bytes()
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    (header_length,) = struct.unpack(">I", data[4:8])
    fmt, track_count, ticks_per_beat = struct.unpack(
        ">HHH", data[8 : 8 + header_length][:6]
    )
    position = 8 + header_length
    tempo = 500_000
    note_events: List[Dict[str, object]] = []
    open_notes: Dict[Tuple[int, int], Tuple[int, int]] = {}

    for _ in range(track_count):
        if data[position : position + 4] != b"MTrk":
            raise ValueError("missing track chunk")
        (track_length,) = struct.unpack(">I", data[position + 4 : position + 8])
        position += 8
        end = position + track_length
        tick = 0
        running_status = 0
        while position < end:
            delta, position = _read_vlq(data, position)
            tick += delta
            status = data[position]
            if status & 0x80:
                position += 1
                running_status = status
            else:
                status = running_status
            if status == META:
                kind = data[position]
                position += 1
                length, position = _read_vlq(data, position)
                payload = data[position : position + length]
                position += length
                if kind == 0x51:
                    tempo = int.from_bytes(payload, "big")
                continue
            if status in (0xF0, 0xF7):
                length, position = _read_vlq(data, position)
                position += length
                continue
            kind = status & 0xF0
            channel = status & 0x0F
            if kind in (NOTE_ON, NOTE_OFF):
                note, velocity = data[position], data[position + 1]
                position += 2
                if kind == NOTE_ON and velocity > 0:
                    open_notes[(channel, note)] = (tick, velocity)
                else:
                    started = open_notes.pop((channel, note), None)
                    if started is not None:
                        note_events.append(
                            {
                                "midi_note": note,
                                "channel": channel,
                                "velocity": started[1],
                                "on_beat": started[0] / ticks_per_beat,
                                "off_beat": tick / ticks_per_beat,
                            }
                        )
            elif kind in (0xA0, 0xB0, 0xE0):
                position += 2
            elif kind in (0xC0, 0xD0):
                position += 1
            else:
                raise ValueError(f"unexpected status byte {status:#x}")
        position = end

    note_events.sort(key=lambda event: (event["on_beat"], event["midi_note"]))
    return {
        "format": fmt,
        "ticks_per_beat": ticks_per_beat,
        "bpm": 60_000_000 / tempo,
        "notes": note_events,
    }
