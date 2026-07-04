"""
Maps physical piano/MIDI keyboard notes to LED positions on the Teensy
WS2812 strips, so a MIDI note number can be lit up directly.

Two data sources are combined:

1. key_id -> MIDI note, measured directly off the real keyboard with
   midi_probe.py (press all 25 keys left to right, one at a time - see that
   script for how key_id order was assigned). This matches the physical
   left-to-right key order confirmed against the real hardware.

   Earlier this used the FingerAccuracy calibration profile
   (data/keyboard-profile/20260603A/midi_mapping.json) instead, with 4 key_ids
   "corrected" based on an assumption about which keys should be natural
   notes. That assumption was wrong - the direct MIDI probe confirmed the
   FingerAccuracy profile's original (uncorrected) values were right all
   along, so that "correction" has been reverted here.

2. key_id -> LED pixel position(s), from the layout used in test_led_array.py:
   white keys (key_id 0-14) sit one-to-one on strip 0 (pixels 0-14), and
   black keys (key_id 15-24) each get two pixels on strip 1.
"""

from common.led_controller import LEDArrayController

# key_id -> MIDI note number (measured via midi_probe.py, see module docstring).
KEY_ID_TO_NOTE = {
    0: 50, 1: 52, 2: 54, 3: 55, 4: 57, 5: 59, 6: 61, 7: 62, 8: 64,
    9: 66, 10: 67, 11: 69, 12: 71, 13: 73, 14: 74,
    15: 51, 16: 53, 17: 56, 18: 58, 19: 60, 20: 63, 21: 65, 22: 68,
    23: 70, 24: 72,
}
NOTE_TO_KEY_ID = {note: key_id for key_id, note in KEY_ID_TO_NOTE.items()}

# key_id -> [(strip, pixel_idx), ...], from test_led_array.py's layout.
KEY_ID_TO_LEDS = {
    0: [(0, 0)], 1: [(0, 1)], 2: [(0, 2)], 3: [(0, 3)], 4: [(0, 4)],
    5: [(0, 5)], 6: [(0, 6)], 7: [(0, 7)], 8: [(0, 8)], 9: [(0, 9)],
    10: [(0, 10)], 11: [(0, 11)], 12: [(0, 12)], 13: [(0, 13)], 14: [(0, 14)],
    15: [(1, 0), (1, 2)],
    16: [(1, 4), (1, 5)],
    17: [(1, 9), (1, 10)],
    18: [(1, 12), (1, 13)],
    19: [(1, 15), (1, 16)],
    20: [(1, 20), (1, 21)],
    21: [(1, 23), (1, 25)],
    22: [(1, 28), (1, 30)],
    23: [(1, 32), (1, 33)],
    24: [(1, 35), (1, 36)],
}

class NoteLEDMapper:
    """
    Light up the LED(s) that correspond to a given key_id (0-24) or MIDI note.

    key_id is the primary identifier - it's what the physical LED wiring and
    the on-screen piano (piano_led_gui.py) both key off of. The note-based
    methods are a thin convenience layer on top, for callers that only have
    a MIDI note number (e.g. reading real MIDI input).
    """

    def __init__(self, led: LEDArrayController):
        self.led = led

    def light_key(self, key_id: int, r: int = 255, g: int = 255, b: int = 255, brightness: int = 255) -> bool:
        """Light the LED(s) for `key_id`. Returns False if key_id isn't mapped."""
        positions = KEY_ID_TO_LEDS.get(key_id)
        if not positions:
            return False
        for strip, idx in positions:
            self.led.set_pixel(strip, idx, r, g, b, brightness)
        return True

    def clear_key(self, key_id: int) -> bool:
        """Turn off the LED(s) for `key_id`."""
        positions = KEY_ID_TO_LEDS.get(key_id)
        if not positions:
            return False
        for strip, idx in positions:
            self.led.set_pixel(strip, idx, 0, 0, 0, 0)
        return True

    def light_note(self, note: int, r: int = 255, g: int = 255, b: int = 255, brightness: int = 255) -> bool:
        """Light the LED(s) for a MIDI `note` number. Returns False if unmapped."""
        key_id = NOTE_TO_KEY_ID.get(note)
        if key_id is None:
            return False
        return self.light_key(key_id, r, g, b, brightness)

    def clear_note(self, note: int) -> bool:
        """Turn off the LED(s) for a MIDI `note` number."""
        key_id = NOTE_TO_KEY_ID.get(note)
        if key_id is None:
            return False
        return self.clear_key(key_id)

    def clear_all(self):
        self.led.clear(-1)


if __name__ == "__main__":
    import time

    with LEDArrayController() as led:
        led.off()
        mapper = NoteLEDMapper(led)

        # Demo: light A4 (MIDI note 69) for 2 seconds, then clear.
        note = 69
        if mapper.light_note(note):
            print(f"Lit note {note}")
        else:
            print(f"Note {note} is not mapped")
        time.sleep(2)
        mapper.clear_all()
