"""Listen on every MIDI input at once and print which port each note came
from.

This is the fastest way to find out which physical instrument is behind
which label when two identical keyboards are plugged in: run it, press a
key, and read the label off the line that appears. See app/midi.py for why
the labels can carry a "#1"/"#2" suffix and why that number is not stable
across replugging.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.midi import MidiInputReader, list_input_port_details  # noqa: E402

running = True


def listen(port):
    reader = MidiInputReader(port.label)
    print(f"Listening on: {reader.port_name} (rtmidi index {port.index})")
    try:
        while running:
            for msg in reader.poll():
                print(f"[{reader.port_name}] {msg.type} note={msg.note} velocity={msg.velocity}")
            time.sleep(0.001)
    finally:
        reader.close()


ports = list_input_port_details()
if not ports:
    print("No MIDI input ports available.")
    raise SystemExit

threads = []
for port in ports:
    t = threading.Thread(target=listen, args=(port,), daemon=True)
    t.start()
    threads.append(t)

input("Press Enter to quit...\n")
running = False
