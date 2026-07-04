"""Background MIDI note-on listener.

Runs mido's blocking port read on its own thread; note-on events land in a
small queue that the GUI thread drains on a timer, so the UI never blocks
waiting on MIDI I/O.

Events are timestamped relative to when the listener was created, the same
convention HandTracker uses for its video timestamps - so if a MIDI log
saved here and a video recording were started at the same moment, their
timestamps line up for offline analysis (see app/offline.py).
"""

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

import mido


def list_input_ports() -> List[str]:
    try:
        return mido.get_input_names()
    except Exception:
        return []


@dataclass
class MidiEvent:
    time: float  # seconds since the listener started
    note: int


def save_midi_log(events: List[MidiEvent], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump([asdict(e) for e in events], f, indent=2)


def load_midi_log(path: Path) -> List[MidiEvent]:
    with open(path) as f:
        data = json.load(f)
    return [MidiEvent(time=item["time"], note=int(item["note"])) for item in data]


class MidiListener:
    def __init__(self, port_name: Optional[str] = None):
        available = list_input_ports()

        if port_name is None:
            port_name = available[0] if available else None
        if port_name is None:
            raise RuntimeError("No MIDI input ports available.")
        if port_name not in available:
            raise RuntimeError(f"MIDI port '{port_name}' not found. Available: {available}")

        self.port_name = port_name
        self._start_time = time.time()
        self._events: deque = deque(maxlen=256)
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with mido.open_input(self.port_name) as port:
                while self._running:
                    for msg in port.iter_pending():
                        if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
                            event = MidiEvent(time=time.time() - self._start_time, note=int(msg.note))
                            with self._lock:
                                self._events.append(event)
                    time.sleep(0.001)
        except Exception as e:
            print(f"MIDI listener error on port {self.port_name}: {e}")

    def pop_events(self) -> List[MidiEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def close(self) -> None:
        self._running = False
