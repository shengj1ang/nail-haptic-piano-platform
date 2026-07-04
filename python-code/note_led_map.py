"""
LED wiring for the Teensy WS2812 strips behind this specific 25-key
keyboard - which pixel(s) light up for the Nth white/black key, left to
right (see test-script/test_led_array.py). This is a fixed, hand-measured
fact about this one LED rig and has nothing to do with which MIDI note any
given key actually sends - that part is keyboard/calibration-specific and
comes from a profile instead (see profile_led_mapper.py, which combines the
two into a note -> LED lookup and is what other code should actually import).
"""

from common.led_controller import LEDArrayController

# LED positions per physical key, left to right - see test-script/test_led_array.py.
WHITE_LEDS = [[(0, i)] for i in range(15)]
BLACK_LEDS = [
    [(1, 0), (1, 2)],
    [(1, 4), (1, 5)],
    [(1, 9), (1, 10)],
    [(1, 12), (1, 13)],
    [(1, 15), (1, 16)],
    [(1, 20), (1, 21)],
    [(1, 23), (1, 25)],
    [(1, 28), (1, 30)],
    [(1, 32), (1, 33)],
    [(1, 35), (1, 36)],
]


class NoteLEDMapper:
    """Light up the LED(s) that correspond to a given MIDI note number,
    using a caller-supplied note -> LED position mapping. Use
    profile_led_mapper.build_note_to_leds() to build that mapping from a
    calibration profile instead of hand-writing one."""

    def __init__(self, led: LEDArrayController, note_to_leds: dict[int, list[tuple[int, int]]]):
        self.led = led
        self.note_to_leds = note_to_leds

    def light_key(self, note: int, r: int = 255, g: int = 255, b: int = 255, brightness: int = 255) -> bool:
        """Light the LED(s) for MIDI `note`. Returns False if unmapped."""
        positions = self.note_to_leds.get(note)
        if not positions:
            return False
        for strip, idx in positions:
            self.led.set_pixel(strip, idx, r, g, b, brightness)
        return True

    def clear_key(self, note: int) -> bool:
        """Turn off the LED(s) for MIDI `note`."""
        positions = self.note_to_leds.get(note)
        if not positions:
            return False
        for strip, idx in positions:
            self.led.set_pixel(strip, idx, 0, 0, 0, 0)
        return True

    def clear_all(self):
        self.led.clear(-1)
