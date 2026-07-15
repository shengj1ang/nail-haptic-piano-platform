"""The formal experiment session's ONE participant-facing cue output.

The standalone quizzes create a fresh cue window (or vibration-rig
connection) per run; a 27-trial pilot session can't work that way - the
cue window lives on an external display the participant faces, and
re-opening it every trial would drop it back onto the experimenter's
screen (un-fullscreened) 27 times. So the session controller
(app/gui/experiment_session_window.py) constructs this ONE ExperimentCue
up front, the experimenter drags/fullscreens its window once, and every
trial routes through it.

It is a normal app.quiz.CueOutput, so the trial runner (a
student_quiz.QuizWindow subclass) drives it exactly like the standalone
quizzes drive theirs. What changes per trial is set_condition():

- "B" (visual finger cue): behaves like cue_window.ScreenCueOutput -
  the finger dot/hand photo lights up on the window.
- "C" (vibrotactile): the buzz goes to the finger's motor
  (app.haptic_cue.HapticCueOutput, connected lazily on the first C
  trial and kept open for the rest of the session); the window shows
  only "HAPTIC CUE" plus the trial status, never a finger graphic.
- "A" (key-only): the lit key LED is the entire cue; the window again
  shows only a status line.
"""

from typing import Optional

from ..haptic_cue import HapticCueOutput
from ..keyboard.midi_mapping import note_name
from ..quiz import CueOutput
from .cue_window import DEFAULT_CUE_STYLE, CueWindow


class ExperimentCue(CueOutput):
    def __init__(self, style: str = DEFAULT_CUE_STYLE):
        self.window = CueWindow(style)
        self.window.setWindowTitle("Pilot Study - Participant Cue Screen")
        self.window.show()
        self.condition: Optional[str] = None  # None = no trial running
        self.trial_status = ""  # e.g. "Trial 5/27 - Level β - Condition C"
        self._haptic: Optional[HapticCueOutput] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Session-controller side
    # ------------------------------------------------------------------

    def set_condition(self, condition: str, trial_status: str) -> None:
        """Called before each trial starts. Raises (like HapticCueOutput's
        constructor) if the trial needs the vibration rig and it can't be
        reached - the caller surfaces that and the trial doesn't start."""
        self.clear()
        if condition == "C" and self._haptic is None:
            self._haptic = HapticCueOutput()
        self.condition = condition
        self.trial_status = trial_status
        if self.window.isHidden():  # re-show after an accidental close
            self.window.show()

    def set_idle(self, text: str) -> None:
        """Back to no-trial state (between trials, rests, session start)."""
        self.clear()
        self.condition = None
        self.trial_status = ""
        if self.window.isHidden():
            self.window.show()
        self.window.set_status(text)

    # ------------------------------------------------------------------
    # CueOutput interface (driven by the trial runner)
    # ------------------------------------------------------------------

    def show_target(self, note: int, finger: Optional[str]) -> None:
        if self.condition == "B":
            # Same wording as the standalone visual quiz's ScreenCueOutput.
            label = f"Press: {note_name(note)}"
            if finger:
                label += f"  -  finger {finger}"
            else:
                label += "  -  finger unknown"
            self.window.set_target(finger, label)
        elif self.condition == "C":
            self._haptic.show_target(note, finger)
            self.window.set_status(f"HAPTIC CUE\n{self.trial_status}\nFollow the vibration on your finger.")
        else:  # "A" or idle: never reveal the finger on screen
            self.window.set_status(f"KEY-LIGHT ONLY\n{self.trial_status}\nFollow the lit key on the keyboard.")

    def show_message(self, text: str) -> None:
        # Countdowns / get-ready / end-of-trial notes. Keep the dot/hand
        # canvas visible under the message during visual trials (matching
        # the standalone quiz); everything else is status-only.
        if self.condition == "B":
            self.window.set_target(None, text)
        elif self.trial_status:
            self.window.set_status(f"{text}\n{self.trial_status}")
        else:
            self.window.set_status(text)

    def clear(self) -> None:
        if self._haptic is not None:
            self._haptic.clear()
        if self.condition == "B":
            self.window.set_target(None, "")
        # A/C: keep the status text up between notes - blanking and
        # re-painting the same line every note would just flicker.

    def close(self) -> None:
        # Both the runner's closeEvent and the session controller's call
        # this - make the second call a no-op instead of a double-close.
        if self._closed:
            return
        self._closed = True
        if self._haptic is not None:
            self._haptic.close()
            self._haptic = None
        self.window.close()
