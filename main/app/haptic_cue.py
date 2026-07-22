"""The vibration-motor implementation of app.quiz.CueOutput.

Counterpart to app/gui/cue_window.py's ScreenCueOutput: instead of
lighting a dot for the target finger on screen, this continuously
vibrates that finger's motor until clear() is called (student pressed the
key, or the quiz timed out on this note) - the same "on until told
otherwise" convention the LED key-cueing already uses, via the Teensy
vibration rig's "S <mask> <amp>" persistent-mask command (see
common/controller.py and test-script/demo_keyboard_control.py, which
drives the same rig from a keyboard for manual testing).

Finger -> motor index is a fixed fact about how this rig is wired, not
tied to any keyboard profile - motor indices 10/11 exist on the board but
aren't wired to a finger, so no finger maps to them.
"""

from typing import Optional

from common.controller import VibratorController

from .quiz import CueOutput

FINGER_TO_MOTOR = {
    "L5": 0,
    "L4": 1,
    "L3": 2,
    "L2": 3,
    "L1": 4,
    "R1": 5,
    "R2": 6,
    "R3": 7,
    "R4": 8,
    "R5": 9,
}

# Calibrated cue strength: at the LRA's 224 Hz resonance, amp=64 measures
# 0.49 m/s^2 RMS - the middle of the 0.4-0.6 m/s^2 "clearly perceptible,
# not annoying" target band. See
# validation_experiments/lra_resonance_intensity_calibration/README.md.
HAPTIC_AMPLITUDE = 64


class HapticCueOutput(CueOutput):
    """Connects to the vibration rig on construction - raises if it can't,
    same as this quiz's Camera()/RawMidiRecorder() do for their hardware,
    since haptic feedback is the entire point of this guidance_type."""

    def __init__(self):
        self.controller = VibratorController()
        self.controller.connect()
        self.controller.stop_all()
        self._active_motor: Optional[int] = None

    def show_target(self, note: int, finger: Optional[str]) -> None:
        self.clear()
        motor = FINGER_TO_MOTOR.get(finger) if finger else None
        if motor is None:
            return  # unresolved finger - nothing to buzz
        self.controller.send(f"S {1 << motor} {HAPTIC_AMPLITUDE}")
        self._active_motor = motor

    def show_message(self, text: str) -> None:
        pass  # no text channel for haptic feedback

    def clear(self) -> None:
        if self._active_motor is not None:
            self.controller.stop_all()
            self._active_motor = None

    def close(self) -> None:
        self.controller.stop_all()
        self.controller.close()
