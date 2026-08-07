"""Driving the student's three cue channels together, and timestamping
each one.

Nothing here re-implements a cue. The screen cue is
app.gui.cue_window.ScreenCueOutput, the vibration cue is
app.haptic_cue.HapticCueOutput, and the key backlight is
profile_led_mapper + note_led_map exactly as the local quiz uses them.
CompositeCueOutput is a app.quiz.CueOutput that fans one show_target()
call out across whichever of them a session enabled, so the quiz-side
interface is unchanged and QuizWindow is not duplicated or subclassed.

The LED key cue is present in every guidance mode. "visual", "haptic" and
"both" name only how the *finger* is conveyed.

What the timestamps mean
------------------------
Each channel is stamped with `perf_counter_ns()` the moment its work
returned: the LED serial write flushed, the cue widget finished
repainting, the haptic serial write flushed. `cue_ready` is the maximum
across the enabled channels, i.e. the moment the student could first have
had the complete cue.

These are **software dispatch and render timings**. They are not the
instant the LED emitted light or the motor began to move - those lag by
firmware, driver and mechanical rise time, and measuring them needs a
photodiode and an accelerometer on one acquisition clock. Field names and
docstrings keep saying so on purpose.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from app.quiz import CueOutput

from .protocol import GUIDANCE_BOTH, GUIDANCE_HAPTIC, GUIDANCE_MODES, GUIDANCE_VISUAL
from .timing import mono_ns, wall_ns

log = logging.getLogger("remote_guidance.cue")


class KeyCue(Protocol):
    """Just enough of note_led_map.NoteLEDMapper for the composite to
    drive it - so a test can pass a recording stub instead of a strip."""

    def light_key(self, note: int) -> bool: ...

    def clear_key(self, note: int) -> bool: ...


@dataclass
class CueDispatch:
    """When each enabled channel became ready for one cue event."""

    dispatch_start_monotonic_ns: int
    led_command_complete_monotonic_ns: Optional[int] = None
    visual_painted_monotonic_ns: Optional[int] = None
    haptic_command_complete_monotonic_ns: Optional[int] = None
    cue_ready_monotonic_ns: int = 0
    cue_ready_wall_ns: int = 0
    # Channels that raised. A failed channel never fakes a timestamp, so
    # a cue_ready computed from the rest cannot silently include it.
    errors: List[str] = field(default_factory=list)

    @property
    def local_dispatch_ns(self) -> int:
        return self.cue_ready_monotonic_ns - self.dispatch_start_monotonic_ns


class LedKeyCue:
    """The keyboard backlight, wrapped so its completion can be stamped.

    Deliberately thin: the note->pixel table is built by
    profile_led_mapper.build_mapper() from the calibration profile, and
    the serial writes are LEDArrayController's. `light()` returns after
    the controller's own flush, which is what makes the stamp meaningful."""

    def __init__(self, mapper: Optional[KeyCue]):
        self.mapper = mapper
        self._lit_notes: List[int] = []

    @property
    def _lit_note(self) -> Optional[int]:
        """The primary lit key. Kept because a chord lights several and
        most callers only care that something is lit."""
        return self._lit_notes[0] if self._lit_notes else None

    def light(self, note: int) -> Optional[int]:
        """Returns the monotonic ns when the command finished, or None if
        there is no LED mapping for this note (or no strip at all)."""
        return self.light_many([note])

    def light_many(self, notes: Sequence[int]) -> Optional[int]:
        """Light every key of a chord, flushed once.

        The strip's own batch() holds the refresh until all the pixels are
        set, so a three-note chord costs one `U` rather than three - the
        keys come up together and the stamp below describes one flush
        instead of the last of several."""
        if self.mapper is None:
            return None
        self.clear()

        # The strip is reachable through the mapper in a real session; a
        # test stub is just the two-method KeyCue protocol, which is why
        # this is optional rather than assumed.
        batch = getattr(getattr(self.mapper, "led", None), "batch", None)
        context = batch() if callable(batch) else nullcontext()

        lit: List[int] = []
        try:
            with context:
                for note in notes:
                    if self.mapper.light_key(note):
                        lit.append(note)
        except Exception as exc:  # noqa: BLE001 - a dead strip must not end the session
            log.warning("LED cue failed for notes %s: %s", list(notes), exc)
            self._lit_notes = lit
            raise
        if not lit:
            return None
        self._lit_notes = lit
        return mono_ns()

    def clear(self) -> None:
        if self.mapper is None or not self._lit_notes:
            return
        try:
            for note in self._lit_notes:
                self.mapper.clear_key(note)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not clear LED for notes %s: %s", self._lit_notes, exc)
        finally:
            self._lit_notes = []


class CompositeCueOutput(CueOutput):
    """One CueOutput over the key LED, the screen cue and the haptic cue.

    Construct it with whichever channels a session enabled; `None` means
    that channel is off. Every call is forwarded to each present channel,
    and show_target() records a CueDispatch describing when each became
    ready.

    `visual` and `haptic` are ordinary app.quiz.CueOutput objects - the
    real ScreenCueOutput/HapticCueOutput in a session, fakes in tests - so
    this class needs no Qt and no serial port of its own."""

    def __init__(
        self,
        led: Optional[LedKeyCue] = None,
        visual: Optional[CueOutput] = None,
        haptic: Optional[CueOutput] = None,
        guidance_mode: str = GUIDANCE_BOTH,
        clock: Callable[[], int] = mono_ns,
        wall_clock: Callable[[], int] = wall_ns,
    ):
        if guidance_mode not in GUIDANCE_MODES:
            raise ValueError(f"unknown guidance mode {guidance_mode!r}, expected one of {list(GUIDANCE_MODES)}")
        self.led = led
        self.visual = visual
        self.haptic = haptic
        self.guidance_mode = guidance_mode
        self._clock = clock
        self._wall_clock = wall_clock
        self.last_dispatch: Optional[CueDispatch] = None

    # -- CueOutput -----------------------------------------------------

    def show_target(self, note: int, finger: Optional[str]) -> CueDispatch:
        return self.show_targets([(note, finger)])

    def show_targets(self, targets: Sequence[Tuple[int, Optional[str]]]) -> CueDispatch:
        """Fire every enabled channel and stamp each completion.

        Channels are driven in the order LED, haptic, visual: the two
        serial writes go out first so neither waits behind a synchronous
        repaint, and the screen - the slowest and the one whose completion
        is hardest to pin down - is stamped last. cue_ready is the maximum
        regardless of order, so this only affects how early each channel
        starts, not the recorded result.

        A chord is one cue event, not several: each channel is told the
        whole set once, so all of it becomes ready together and there is
        still exactly one cue_ready to measure a reaction against (§4.1).
        Channels that cannot show a set fall back to the primary target on
        their own (app.quiz.CueOutput.show_targets)."""
        dispatch = CueDispatch(dispatch_start_monotonic_ns=self._clock())
        targets = list(targets)

        if self.led is not None:
            try:
                dispatch.led_command_complete_monotonic_ns = self.led.light_many([note for note, _ in targets])
            except Exception as exc:  # noqa: BLE001
                dispatch.errors.append(f"led: {exc}")

        if self.haptic is not None:
            try:
                self.haptic.show_targets(targets)
                _flush(self.haptic)
                dispatch.haptic_command_complete_monotonic_ns = self._clock()
            except Exception as exc:  # noqa: BLE001
                dispatch.errors.append(f"haptic: {exc}")

        if self.visual is not None:
            try:
                self.visual.show_targets(targets)
                # Forces the repaint, so the stamp is after the frame was
                # drawn rather than after it was merely scheduled.
                _flush(self.visual)
                dispatch.visual_painted_monotonic_ns = self._clock()
            except Exception as exc:  # noqa: BLE001
                dispatch.errors.append(f"visual: {exc}")

        ready = [
            t
            for t in (
                dispatch.led_command_complete_monotonic_ns,
                dispatch.visual_painted_monotonic_ns,
                dispatch.haptic_command_complete_monotonic_ns,
            )
            if t is not None
        ]
        now_mono = self._clock()
        dispatch.cue_ready_monotonic_ns = max(ready) if ready else now_mono
        # Wall equivalent derived from this machine's own two clocks read
        # back to back - never from another host's timestamp.
        dispatch.cue_ready_wall_ns = self._wall_clock() - (now_mono - dispatch.cue_ready_monotonic_ns)

        self.last_dispatch = dispatch
        return dispatch

    def show_message(self, text: str) -> None:
        # Only the screen has a text channel; the LED and the motors have
        # nothing to say a message with (HapticCueOutput.show_message is
        # already a no-op).
        for channel in (self.visual, self.haptic):
            if channel is not None:
                try:
                    channel.show_message(text)
                except Exception as exc:  # noqa: BLE001
                    log.warning("show_message failed on %s: %s", type(channel).__name__, exc)

    def clear(self) -> None:
        if self.led is not None:
            self.led.clear()
        for channel in (self.visual, self.haptic):
            if channel is not None:
                try:
                    channel.clear()
                except Exception as exc:  # noqa: BLE001
                    log.warning("clear failed on %s: %s", type(channel).__name__, exc)

    def close(self) -> None:
        """Release every channel, even if an earlier one fails - a screen
        cue that refuses to close must not leave the motors buzzing."""
        errors: List[str] = []
        if self.led is not None:
            try:
                self.led.clear()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        for channel in (self.haptic, self.visual):
            if channel is None:
                continue
            try:
                channel.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(channel).__name__}: {exc}")
        if errors:
            log.warning("errors while closing cue channels: %s", "; ".join(errors))

    # -- introspection -------------------------------------------------

    @property
    def enabled_channels(self) -> List[str]:
        names = []
        if self.led is not None:
            names.append("led")
        if self.visual is not None:
            names.append("visual")
        if self.haptic is not None:
            names.append("haptic")
        return names

    def describe(self) -> Dict[str, Any]:
        return {
            "guidance_mode": self.guidance_mode,
            "channels": self.enabled_channels,
            "timing_kind": "software_dispatch_render",
        }


def _flush(channel: Any) -> None:
    """Ask a channel to complete its work now, if it can.

    ScreenCueOutput.flush() repaints synchronously. HapticCueOutput has no
    flush because VibratorController.send() already flushes the serial
    port before returning. Anything without the method is simply assumed
    to have finished when its call returned."""
    flush = getattr(channel, "flush", None)
    if callable(flush):
        flush()


def wants_visual(guidance_mode: str) -> bool:
    return guidance_mode in (GUIDANCE_VISUAL, GUIDANCE_BOTH)


def wants_haptic(guidance_mode: str) -> bool:
    return guidance_mode in (GUIDANCE_HAPTIC, GUIDANCE_BOTH)


def build_student_cue(
    guidance_mode: str,
    led_mapper: Optional[KeyCue] = None,
    visual_factory: Optional[Callable[[], CueOutput]] = None,
    haptic_factory: Optional[Callable[[], CueOutput]] = None,
) -> CompositeCueOutput:
    """Assemble the cue for one student session.

    The two factories are called only when the mode needs them, so a
    "haptic" session never opens a cue window and a "visual" session never
    touches the vibration rig's serial port. Passing factories rather than
    instances is what keeps this function usable in a test with no
    hardware and no display."""
    if guidance_mode not in GUIDANCE_MODES:
        raise ValueError(f"unknown guidance mode {guidance_mode!r}, expected one of {list(GUIDANCE_MODES)}")

    visual = visual_factory() if (wants_visual(guidance_mode) and visual_factory is not None) else None
    haptic = None
    if wants_haptic(guidance_mode) and haptic_factory is not None:
        try:
            haptic = haptic_factory()
        except Exception:
            # The screen cue is already open at this point; close it
            # rather than leaking a window when the rig is unavailable.
            if visual is not None:
                visual.close()
            raise

    return CompositeCueOutput(
        led=LedKeyCue(led_mapper) if led_mapper is not None else None,
        visual=visual,
        haptic=haptic,
        guidance_mode=guidance_mode,
    )
