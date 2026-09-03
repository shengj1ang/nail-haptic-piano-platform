"""Background MIDI note-on listener, and the port identity every tool uses.

Runs a non-blocking MIDI port read on its own thread; note-on events land
in a small queue that the GUI thread drains on a timer, so the UI never
blocks waiting on MIDI I/O.

Every event carries an absolute wall-clock timestamp (time.time()) - the
single time format used across the whole codebase. Offline analysis
(app/offline.py) maps those absolute times onto video frames through the
recording's sync anchors (see app.sync_led), so no relative clocks are
ever stored.

Ports are enumerated and opened through python-rtmidi directly, not
through mido, because mido cannot address two identical keyboards:

  - mido.get_input_names() goes through mido.backends.rtmidi.get_devices(),
    which de-duplicates on the port-name string. Two keyboards of the same
    model get the same name from the OS ("SE25 MIDI1" twice on macOS), so
    the second one disappears from the list entirely.
  - mido.open_input(name) resolves the name with list.index(), which
    always returns the *first* port with that name. Even with a complete
    list, the second keyboard would be unreachable - and silently so: the
    port opens, it just belongs to the other instrument.

That matters because the whole tele-training module (see
doc/REMOTE_GUIDANCE.md) expects a teacher keyboard and a student keyboard,
which in practice are two of the same model.

So a port here has three parts (MidiInputPort): the rtmidi `index` it is
actually opened by, the `raw_name` the driver reports, and a `label` that
is unique within one enumeration. A name the OS reports only once is its
own label, unchanged - which is why config files written before this
existed keep working. Duplicates get a " #1", " #2" suffix in the order
the OS lists them.

**That order is the OS's, and it can change when devices are replugged.**
Nothing in MIDI identifies a physical instrument portably, so "#2" means
"the second one the OS listed just now", not "that keyboard over there".

A stable per-device identity was investigated and deliberately not built:
macOS could supply one (CoreMIDI persists a `uniqueID` against the USB
port), but Windows - a supported deployment target here - cannot. WinMM's
`MIDIINCAPS` carries only manufacturer id, product id, driver version and
name, all four identical for two keyboards of the same model. A feature
that silently degrades to enumeration order on the deployment platform
would be worse than the honest suffix. The full finding, for both the
keyboards and the cameras, is in doc/REMOTE_GUIDANCE.md §10.

On macOS the durable fix is to rename each instrument in Audio MIDI Setup
(MIDI Studio), which makes the raw names differ and the suffixes go away.
"""

import json
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, NamedTuple, Optional

import rtmidi


@dataclass(frozen=True)
class MidiInputPort:
    """One MIDI input, as both the OS and the user see it."""

    index: int  # rtmidi port index - the only thing a port can be opened by
    raw_name: str  # what the driver calls it; not necessarily unique
    label: str  # unique within one enumeration; what config.json stores

    @property
    def is_ambiguous(self) -> bool:
        """True when another port reported the same raw name, so this
        label's number came from OS enumeration order (see module docstring)."""
        return self.label != self.raw_name

    def __str__(self) -> str:
        return self.label


class MidiMessage(NamedTuple):
    """A note message, with mido's field names and mido's conventions - a
    note_on with velocity 0 stays a note_on here, exactly as mido reports
    it, and callers apply their own running-status handling."""

    type: str  # "note_on" or "note_off"
    note: int
    velocity: int


def _rtmidi_input_names() -> List[str]:
    midi_in = rtmidi.MidiIn()
    try:
        return list(midi_in.get_ports())
    finally:
        midi_in.delete()


def _label_ports(names: List[str]) -> List[MidiInputPort]:
    """Give every port a unique label, leaving unique names untouched."""
    totals = Counter(names)
    seen: Counter = Counter()
    ports = []
    for index, raw_name in enumerate(names):
        if totals[raw_name] == 1:
            label = raw_name
        else:
            seen[raw_name] += 1
            label = f"{raw_name} #{seen[raw_name]}"
        ports.append(MidiInputPort(index=index, raw_name=raw_name, label=label))
    return ports


def list_input_port_details() -> List[MidiInputPort]:
    """Every MIDI input the OS currently reports, duplicates included."""
    try:
        return _label_ports(_rtmidi_input_names())
    except Exception:
        return []


def list_input_ports() -> List[str]:
    """The unique labels of list_input_port_details(), for a port picker.
    A single keyboard produces exactly what mido used to return."""
    return [port.label for port in list_input_port_details()]


def resolve_input_port(port_name: Optional[str] = None) -> MidiInputPort:
    """Find the port a stored/typed name refers to, or raise RuntimeError
    listing what is actually there.

    None means "the first one", the long-standing default. A label is
    matched exactly; failing that a bare raw name is accepted and resolves
    to the first port with that name - that is what a config.json written
    before labels existed holds, and what someone reading the name off the
    OS would type."""
    ports = list_input_port_details()
    if not ports:
        raise RuntimeError("No MIDI input ports available.")

    if port_name is None:
        return ports[0]

    for port in ports:
        if port.label == port_name:
            return port
    for port in ports:
        if port.raw_name == port_name:
            return port

    available = [port.label for port in ports]
    raise RuntimeError(f"MIDI port '{port_name}' not found. Available: {available}")


def ambiguous_port_names(ports: Optional[List[MidiInputPort]] = None) -> List[str]:
    """Raw names reported by more than one port right now, i.e. the ones
    whose "#1"/"#2" depends on enumeration order. For a UI that wants to
    warn before someone picks the wrong keyboard."""
    ports = list_input_port_details() if ports is None else ports
    return sorted({port.raw_name for port in ports if port.is_ambiguous})


class MidiInputReader:
    """One open MIDI input, opened by index and read by polling.

    Opening happens here, in the constructor, so a port that is missing or
    already taken fails where the caller can report it - every caller
    already handles RuntimeError from these constructors.
    """

    def __init__(self, port_name: Optional[str] = None):
        self.port = resolve_input_port(port_name)
        self._midi_in = rtmidi.MidiIn()
        try:
            self._midi_in.open_port(self.port.index)
        except Exception as e:
            self._midi_in.delete()
            raise RuntimeError(f"Could not open MIDI port '{self.port.label}': {e}") from e

    @property
    def port_name(self) -> str:
        return self.port.label

    def poll(self) -> List[MidiMessage]:
        """Every note message waiting on the port, oldest first. Never
        blocks; rtmidi buffers between calls, so the 1 ms poll loops below
        lose nothing."""
        messages = []
        while True:
            item = self._midi_in.get_message()
            if item is None:
                break
            data = item[0]
            if len(data) < 3:
                continue
            status = data[0] & 0xF0
            if status == 0x90:
                messages.append(MidiMessage("note_on", int(data[1]), int(data[2])))
            elif status == 0x80:
                messages.append(MidiMessage("note_off", int(data[1]), int(data[2])))
        return messages

    def close(self) -> None:
        try:
            self._midi_in.close_port()
        finally:
            self._midi_in.delete()


@dataclass
class MidiEvent:
    time: float  # absolute wall-clock time.time() when the note was received
    note: int


def save_midi_log(events: List[MidiEvent], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in events], f, indent=2, ensure_ascii=False)


def load_midi_log(path: Path) -> List[MidiEvent]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [MidiEvent(time=item["time"], note=int(item["note"])) for item in data]


class MidiListener:
    def __init__(self, port_name: Optional[str] = None):
        self._reader = MidiInputReader(port_name)
        self.port_name = self._reader.port_name
        self._events: deque = deque(maxlen=256)
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while self._running:
                for msg in self._reader.poll():
                    if msg.type == "note_on" and msg.velocity > 0:
                        event = MidiEvent(time=time.time(), note=int(msg.note))
                        with self._lock:
                            self._events.append(event)
                time.sleep(0.001)
        except Exception as e:
            print(f"MIDI listener error on port {self.port_name}: {e}")
        finally:
            self._reader.close()

    def pop_events(self) -> List[MidiEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def close(self) -> None:
        self._running = False
