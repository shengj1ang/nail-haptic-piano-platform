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

HOW HARD IT BUZZES is not fixed here: the drive amp and the PWM
frequency come from config.json's haptic block (common.haptic_config),
for whichever actuator type is currently in use - so switching the rig
from LRA to ERM in Initial Setup changes what the cue sends, with no
code change. The frequency matters as much as the amp: the firmware
boots every motor pin at the LRA's resonance, which an ERM cannot even
start from, so the cue sets each finger port's frequency when it
connects.
"""

from typing import Optional

from common import haptic_config as hc
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

def cue_amplitude() -> int:
    """The configured cue strength of the actuator in use.

    The LRA default (amp 64 at its 224 Hz resonance) measures 0.49 m/s^2
    RMS - the middle of the 0.4-0.6 m/s^2 "clearly perceptible, not
    annoying" target band; see
    validation_experiments/lra_resonance_intensity_calibration/README.md.
    Changing the actuator (or its amp) in Initial Setup changes this."""
    return hc.get_active_haptic_defaults().default_amp


def cue_frequency() -> int:
    """The configured drive frequency of the actuator in use."""
    return hc.get_active_haptic_defaults().default_frequency


class HapticCueOutput(CueOutput):
    """Connects to the vibration rig on construction - raises if it can't,
    same as this quiz's Camera()/RawMidiRecorder() do for their hardware,
    since haptic feedback is the entire point of this guidance_type.

    The configured drive is read ONCE, on construction, so a config edit
    mid-quiz can never change the cue between two events of the same
    session (and the session's own record says what it delivered).

    `port` and `controller` are both optional and both default to the
    original behaviour (auto-detect a single attached board), so
    HapticQuizWindow and every other existing caller is unaffected. They
    exist for two later needs: the remote student machine has *two*
    serial boards attached - the LED strip and this rig - and
    auto-detection cannot tell them apart, so it passes an explicit port
    (see remote_guidance/config.py); and tests inject a fake controller
    to exercise cue logic with no hardware present."""

    def __init__(self, port: Optional[str] = None, controller: Optional[VibratorController] = None):
        config = hc.get_haptic_config()
        self.actuator_type = config.using
        self.amplitude = config.active.default_amp
        self.frequency = config.active.default_frequency
        if controller is not None:
            self.controller = controller
        elif port is None:
            # Constructed with no arguments in the default case, exactly as
            # before this parameter existed - existing callers and tests
            # that substitute a zero-argument VibratorController stand-in
            # keep working.
            self.controller = VibratorController()
        else:
            self.controller = VibratorController(port=port)
        self.controller.connect()
        self.controller.stop_all()
        # Every motor pin boots at the firmware's default PWM frequency
        # (the LRA's resonance): an ERM chopped that slowly never starts,
        # so each finger port is retuned to the configured frequency
        # before the first cue.
        for motor in sorted(set(FINGER_TO_MOTOR.values())):
            self.controller.send(f"F {motor} {self.frequency}")
        self._active_motor: Optional[int] = None

    def show_target(self, note: int, finger: Optional[str]) -> None:
        self.clear()
        motor = FINGER_TO_MOTOR.get(finger) if finger else None
        if motor is None:
            return  # unresolved finger - nothing to buzz
        self.controller.send(f"S {1 << motor} {self.amplitude}")
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
