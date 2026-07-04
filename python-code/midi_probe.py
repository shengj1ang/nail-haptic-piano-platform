"""
Ground-truth calibration probe: prints each MIDI note as you press physical
keyboard keys, in order pressed.

Run this, then press all 25 keys on the real keyboard LEFT TO RIGHT, one at a
time, starting from the lowest key on the far left (white1, black1, white2,
black2, white3, white4, ... matching key_id 0..24 in piano_led_gui.py's
VISUAL_SEQUENCE). The Nth key you press gets labeled "key_id N" below.

This sidesteps the FingerAccuracy calibration profile entirely (which turned
out to have at least one mismatch) - it's a direct, no-assumptions capture
of what THIS keyboard actually sends for each physical key.

Ctrl+C to stop.
"""

import mido

PORT_NAME = "SE25 MIDI1"


def main():
    count = 0
    print(f"Listening on {PORT_NAME!r}.")
    print("Press all 25 keys, left to right, one at a time (Ctrl+C when done).\n")
    try:
        with mido.open_input(PORT_NAME) as inport:
            for msg in inport:
                if msg.type == "note_on" and msg.velocity > 0:
                    print(f"key_id {count}: note={msg.note}")
                    count += 1
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
