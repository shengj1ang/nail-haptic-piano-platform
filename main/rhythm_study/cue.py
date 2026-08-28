"""The rhythm experiment's cue output: haptic, or nothing at all.

This is where this study diverges most sharply from the Main User Study.
Section 6 routes every trial through ``app/gui/experiment_cue.py``, which
owns a participant-facing *window* - a finger dot or a hand photo on an
external display. The rhythm experiment has no visual guidance and no
participant-facing screen: the participant looks at the keyboard, and the
only things that can tell them what to play are

* the **backlight** - the keyboard's own LED strip, lit by the quiz
  runner itself (``student_quiz._show_current_target``), not by this
  class; and
* the **haptic** cue - a buzz on the target finger's motor, which is
  what this class delivers.

So a CueOutput here is a switch, not a display. :meth:`set_phase` is
called before each trial with that trial's guidance flags (from
``rhythm_study.schedule.PHASE_GUIDANCE``), and after that the runner
drives it exactly like any other quiz cue:

===========  ========  ============================================
Phase        haptic    show_target() does
===========  ========  ============================================
Training     on        buzz the target finger's motor
Probe        off       nothing - the lit key is the whole cue
Final test   off       nothing, and the runner leaves the LED dark
===========  ========  ============================================

The rig connection is made lazily, on the first trial that actually needs
it, and then kept open for the rest of the session - the same lifetime
``ExperimentCue`` gives it, and for the same reason: connecting per trial
would re-initialise every motor 15 times in one session.
"""

from typing import Optional

from app.haptic_cue import HapticCueOutput
from app.quiz import CueOutput


class RhythmCue(CueOutput):
    """One cue output for a whole session; the phase decides what it does.

    Deliberately has no window and no `set_idle`: there is nothing for a
    participant to read. Status text belongs on the experimenter's
    screen, which is where the session controller puts it.
    """

    def __init__(self, haptic: Optional[HapticCueOutput] = None):
        # `haptic` is injectable so the schedule/session logic can be
        # exercised without the vibration rig attached; left None it is
        # connected on demand by set_phase().
        self._haptic: Optional[HapticCueOutput] = haptic
        self._haptic_enabled = False
        self._closed = False

    # ------------------------------------------------------------------
    # Session-controller side
    # ------------------------------------------------------------------

    def set_phase(self, haptic: bool) -> None:
        """Called before each trial starts. Raises (like
        HapticCueOutput's constructor) if the trial needs the vibration
        rig and it can't be reached - the caller surfaces that and the
        trial doesn't start, rather than a training trial silently
        running with no haptic guidance."""
        self.clear()
        if haptic and self._haptic is None:
            self._haptic = HapticCueOutput()
        self._haptic_enabled = haptic

    @property
    def haptic_connected(self) -> bool:
        return self._haptic is not None

    # ------------------------------------------------------------------
    # CueOutput - what the trial runner calls
    # ------------------------------------------------------------------

    def show_target(self, note: int, finger: Optional[str]) -> None:
        if self._haptic_enabled and self._haptic is not None:
            self._haptic.show_target(note, finger)

    def show_message(self, text: str) -> None:
        # Countdowns and "get ready" text have no participant-facing
        # surface in this study; the experimenter's window shows them.
        pass

    def clear(self) -> None:
        if self._haptic is not None:
            self._haptic.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._haptic is not None:
            self._haptic.close()
            self._haptic = None
