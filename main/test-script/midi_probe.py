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

Press the 15 white keys left to right, then the 10 black keys left to
right, to get a readable summary at the end, or just press whatever keys
you're curious about - the numbering here is only for display.

Ctrl+C to stop.
"""

import mido

PORT_NAME = "SE25 MIDI1"


def main():
    white_count = 0
    black_count = 0
    print(f"Listening on {PORT_NAME!r}.")
    print("Press the 15 white keys left to right, then the 10 black keys left to right (Ctrl+C when done).\n")
    try:
        with mido.open_input(PORT_NAME) as inport:
            for msg in inport:
                if msg.type == "note_on" and msg.velocity > 0:
                    if white_count < 15:
                        print(f"white[{white_count}]: note={msg.note}")
                        white_count += 1
                    else:
                        print(f"black[{black_count}]: note={msg.note}")
                        black_count += 1
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
