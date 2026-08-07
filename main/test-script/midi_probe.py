"""
Quick, no-profile-needed diagnostic: prints each MIDI note as you press
physical keyboard keys, in order pressed.

This is a raw sanity check, not part of the calibration pipeline - the
actual key_id -> note data other tools rely on comes from
setup_keyboard_wizard.py + setup_midi_mapping_wizard.py (saved into a profile
under data/keyboard-profile/<profile>/, and consumed by
profile_led_mapper.py for LED mapping). Use this script instead when you
just want a fast answer to "what note does this key send right now" -
e.g. verifying the keyboard's own transpose/octave setting is correct
(middle C should send note 60) before running the real calibration wizard.

It is also the quickest way to answer "which of these two identical
keyboards is #1?" - run it with one port, press a key, and see whether
anything arrives:

    python test-script/midi_probe.py                 # list the ports, use the first
    python test-script/midi_probe.py "SE25 MIDI1 #2" # a specific one

Press the 15 white keys left to right, then the 10 black keys left to
right, to get a readable summary at the end, or just press whatever keys
you're curious about - the numbering here is only for display.

Ctrl+C to stop.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.midi import MidiInputReader, list_input_ports  # noqa: E402


def main():
    port_name = sys.argv[1] if len(sys.argv) > 1 else None

    ports = list_input_ports()
    if not ports:
        print("No MIDI input ports available.")
        return
    print("MIDI inputs on this machine:")
    for name in ports:
        print(f"  {name}")

    try:
        reader = MidiInputReader(port_name)
    except RuntimeError as exc:
        print(f"\n{exc}")
        return

    white_count = 0
    black_count = 0
    print(f"\nListening on {reader.port_name!r}.")
    print("Press the 15 white keys left to right, then the 10 black keys left to right (Ctrl+C when done).\n")
    try:
        while True:
            for msg in reader.poll():
                if msg.type == "note_on" and msg.velocity > 0:
                    if white_count < 15:
                        print(f"white[{white_count}]: note={msg.note}")
                        white_count += 1
                    else:
                        print(f"black[{black_count}]: note={msg.note}")
                        black_count += 1
            time.sleep(0.001)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
