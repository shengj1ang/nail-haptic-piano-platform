"""The student session model - no Qt, no hardware, fully testable.

One `RemoteEvent` per guidance instruction. The window feeds this class
three things (an arriving envelope, a clock tick, a MIDI note-on) and
reads back what to display and what to send; every timing rule lives
here.

The rules
---------
1. **Acknowledge before presenting.** `accept()` stamps arrival and asks
   the caller to send `guidance.received` *before* any cue work starts,
   so the teacher's "student received it" time is not inflated by the
   student's own dispatch.

2. **Queue, never overwrite.** A cue that arrives while another is still
   live goes into a bounded queue. It is never dropped silently and never
   replaces the live one - if the queue is full the event is refused
   explicitly and the teacher is told.

3. **Queue wait is its own number.** The time an event spent waiting for
   its turn is recorded as `queue_wait_ns` and is part of neither the
   network delay nor the student's reaction time.

4. **Reaction time starts at the local cue-ready moment.**

       reaction_time_ns = student_response_monotonic_ns - cue_ready_monotonic_ns

   Both terms from this machine's monotonic clock. Starting it at
   teacher_send / server_receive / student_receive would fold the network
   into a measurement of a person; those stamps are all kept on the same
   event, just never used for this.

Scoring is not reimplemented here. Note correctness is the same target-vs-
actual comparison the local quiz makes, finger correctness is
app.finger_matching.is_finger_correct, and the summary is
app.quiz.summarize over QuizResult objects this module fills in - so a
remote session and a local quiz can never disagree about what "correct"
means.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from app.finger_matching import FingerMatch, is_finger_correct
from app.keyboard.midi_mapping import MidiMapping, note_name
from app.quiz import QuizResult

from ..protocol import (
    STAGE_FINAL,
    STAGE_PROVISIONAL,
    GuidanceAction,
    parse_actions,
)
from ..timing import DispatchTimings, mono_ns, wall_ns

log = logging.getLogger("remote_guidance.student.session")

STATE_QUEUED = "queued"
STATE_PRESENTING = "presenting"
STATE_DONE = "done"
STATE_REFUSED = "refused"


@dataclass
class RemoteEvent:
    """One guidance instruction and everything measured about it."""

    index: int
    message_id: str
    seq: int
    actions: List[GuidanceAction]
    timeout_s: float
    timings: DispatchTimings = field(default_factory=DispatchTimings)
    state: str = STATE_QUEUED
    refused_reason: Optional[str] = None

    timed_out: bool = False
    actual_note: Optional[int] = None
    actual_key_id: Optional[int] = None
    actual_finger: Optional[str] = None
    note_correct: bool = False
    finger_correct: Optional[bool] = None
    finger_probabilities: Optional[Dict[str, float]] = None
    target_finger_probability: Optional[float] = None
    actual_finger_point: Optional[List[int]] = None
    # Flipped once the post-session offline pass has re-judged the finger.
    stage: str = STAGE_PROVISIONAL

    # -- the target -----------------------------------------------------

    @property
    def target(self) -> Optional[GuidanceAction]:
        """The primary action. A chord arrives as several actions; the
        first is the one scored per event, and the rest travel with it so
        nothing is lost."""
        return self.actions[0] if self.actions else None

    @property
    def target_note(self) -> Optional[int]:
        return self.target.note if self.target else None

    @property
    def target_finger(self) -> Optional[str]:
        return self.target.finger if self.target else None

    # -- derived timings ------------------------------------------------

    @property
    def reaction_time_ns(self) -> Optional[int]:
        return self.timings.reaction_time_ns

    @property
    def reaction_time_s(self) -> Optional[float]:
        rt = self.timings.reaction_time_ns
        return None if rt is None else rt / 1e9

    @property
    def queue_wait_ns(self) -> Optional[int]:
        return self.timings.queue_wait_ns

    def transport_ns(self) -> Optional[int]:
        """Teacher send -> student receive, from the two *wall* clocks.

        Only meaningful if the two machines' clocks are synchronised, and
        it is never used for reaction time. Kept because the teacher's
        event table shows it, always labelled as a wall-clock difference
        rather than a measured one-way latency."""
        if self.timings.teacher_send_wall_ns is None or self.timings.student_receive_wall_ns is None:
            return None
        return self.timings.student_receive_wall_ns - self.timings.teacher_send_wall_ns

    # -- reporting ------------------------------------------------------

    def to_quiz_result(self) -> QuizResult:
        """The platform's own per-event record, so a remote session lands
        in data/quiz/<name>/results.json exactly like a local quiz and
        every existing analysis tool reads it unchanged.

        cue_onset_time/keypress_time keep their wall-clock meaning for
        that compatibility; timing_error_s is filled from the *monotonic*
        reaction time, which is the accurate one - see the module
        docstring."""
        target = self.target
        rt = self.reaction_time_s
        return QuizResult(
            index=self.index,
            target_note=target.note if target else -1,
            target_note_name=(target.note_name or note_name(target.note)) if target else "",
            target_key_id=target.key_id if target else None,
            target_finger=target.finger if target else None,
            cue_onset_time=(self.timings.cue_ready_wall_ns or 0) / 1e9,
            timed_out=self.timed_out,
            actual_note=self.actual_note,
            actual_key_id=self.actual_key_id,
            keypress_time=(self.timings.student_response_wall_ns / 1e9)
            if self.timings.student_response_wall_ns
            else None,
            timing_error_s=rt,
            note_correct=self.note_correct,
            actual_finger=self.actual_finger,
            finger_correct=self.finger_correct,
            finger_probabilities=self.finger_probabilities,
            target_finger_probability=self.target_finger_probability,
            actual_finger_point=self.actual_finger_point,
        )

    def response_payload(self, stage: Optional[str] = None) -> Dict[str, Any]:
        """What the teacher's live table is built from."""
        target = self.target
        return {
            "stage": stage or self.stage,
            "guidance_message_id": self.message_id,
            "index": self.index,
            "guidance_seq": self.seq,
            "target_note": target.note if target else None,
            "target_note_name": (target.note_name or (note_name(target.note) if target else None)) if target else None,
            "target_finger": target.finger if target else None,
            "actual_note": self.actual_note,
            "actual_note_name": note_name(self.actual_note) if self.actual_note is not None else None,
            "actual_finger": self.actual_finger,
            "note_correct": self.note_correct,
            "finger_correct": self.finger_correct,
            "timed_out": self.timed_out,
            "target_finger_probability": self.target_finger_probability,
            "reaction_time_ns": self.reaction_time_ns,
            "timings": self.timings.as_dict(),
        }

    def presented_payload(self) -> Dict[str, Any]:
        return {
            "guidance_message_id": self.message_id,
            "index": self.index,
            "timings": self.timings.as_dict(),
            # Said explicitly on every frame so no consumer can read these
            # as physical LED/actuator onset moments.
            "timing_kind": "software_dispatch_render",
        }

    def received_payload(self, accepted: bool = True) -> Dict[str, Any]:
        return {
            "guidance_message_id": self.message_id,
            "index": self.index,
            "accepted": accepted,
            "refused_reason": self.refused_reason,
            "student_receive_wall_ns": self.timings.student_receive_wall_ns,
            "teacher_send_wall_ns": self.timings.teacher_send_wall_ns,
            "server_receive_wall_ns": self.timings.server_receive_wall_ns,
        }


# ---------------------------------------------------------------------------


class StudentSession:
    """Sequences guidance events onto the local cue and scores responses.

    Callbacks (all optional, all called synchronously on the caller's
    thread - the window's GUI thread):

        send_received(event, accepted) -> emit guidance.received, at once
        send_presented(event)         -> emit guidance.presented, after cue ready
        send_response(event)          -> emit performance.response
        present(event)                -> drive the cue; returns a CueDispatch
        clear_cue()                   -> stop the current cue
        resolve_finger(note)          -> Optional[FingerMatch], live pass

    `resolve_finger` is how the provisional finger verdict is produced:
    the window passes the current hand landmarks through the existing
    app.finger_matching.match_note_to_finger. This class never looks at a
    camera itself."""

    def __init__(
        self,
        timeout_s: float = 5.0,
        queue_size: int = 16,
        mapping: Optional[MidiMapping] = None,
        present: Optional[Callable[[RemoteEvent], Any]] = None,
        clear_cue: Optional[Callable[[], None]] = None,
        send_received: Optional[Callable[[RemoteEvent, bool], None]] = None,
        send_presented: Optional[Callable[[RemoteEvent], None]] = None,
        send_response: Optional[Callable[[RemoteEvent], None]] = None,
        resolve_finger: Optional[Callable[[int], Optional[FingerMatch]]] = None,
        clock: Callable[[], int] = mono_ns,
        wall_clock: Callable[[], int] = wall_ns,
    ):
        self.timeout_s = timeout_s
        self.queue_size = max(1, queue_size)
        self.mapping = mapping
        self.present = present
        self.clear_cue = clear_cue
        self.send_received = send_received
        self.send_presented = send_presented
        self.send_response = send_response
        self.resolve_finger = resolve_finger
        self._clock = clock
        self._wall_clock = wall_clock

        self.events: List[RemoteEvent] = []
        self._queue: Deque[RemoteEvent] = deque()
        self.current: Optional[RemoteEvent] = None
        self.paused = False
        self.running = False
        self._next_index = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self.running = True
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def stop(self) -> None:
        """Ends the session and abandons whatever is queued. A cue that
        was live when stop arrived is closed out as a timeout rather than
        left half-recorded."""
        self.running = False
        if self.current is not None:
            self._finish_current(timed_out=True)
        self._queue.clear()

    @property
    def pending(self) -> int:
        return len(self._queue)

    # -- incoming guidance ----------------------------------------------

    def accept(self, envelope: Dict[str, Any]) -> RemoteEvent:
        """Take one guidance.live envelope.

        Stamps arrival first, then hands the caller a `guidance.received`
        to send, then queues. A refused event (queue full) is returned
        with state REFUSED and is still acknowledged, so the teacher sees
        that the student is behind instead of a cue vanishing."""
        receive_mono = self._clock()
        receive_wall = self._wall_clock()

        payload = envelope.get("payload") or {}
        server = envelope.get("server") or {}
        event = RemoteEvent(
            index=self._next_index,
            message_id=envelope.get("message_id", ""),
            seq=int(envelope.get("seq", 0) or 0),
            actions=parse_actions(payload),
            timeout_s=float(payload.get("timeout_s") or self.timeout_s),
        )
        event.timings.teacher_send_wall_ns = envelope.get("sent_at_unix_ns") or None
        event.timings.server_receive_wall_ns = server.get("receive_wall_ns")
        event.timings.student_receive_wall_ns = receive_wall
        event.timings.student_receive_monotonic_ns = receive_mono

        if len(self._queue) >= self.queue_size:
            event.state = STATE_REFUSED
            event.refused_reason = f"student guidance queue is full ({self.queue_size} waiting)"
            log.warning("refusing guidance %s: %s", event.message_id, event.refused_reason)
            _safe(self.send_received, event, False)
            return event

        self._next_index += 1
        self.events.append(event)
        # Acknowledged before anything is presented - see rule 1.
        _safe(self.send_received, event, True)
        event.timings.queue_enter_monotonic_ns = self._clock()
        self._queue.append(event)
        return event

    # -- driving --------------------------------------------------------

    def tick(self) -> None:
        """Call from the window's timer. Starts the next cue when the
        current one has finished, and times the current one out."""
        if not self.running or self.paused:
            return
        if self.current is None:
            self._present_next()
            return
        # Timed from cue-ready, the same origin as the reaction time, so a
        # slow dispatch shortens nobody's response window unfairly.
        started = self.current.timings.cue_ready_monotonic_ns
        if started is None:
            return
        if (self._clock() - started) >= self.current.timeout_s * 1e9:
            self._finish_current(timed_out=True)

    def _present_next(self) -> None:
        if not self._queue:
            return
        event = self._queue.popleft()
        event.state = STATE_PRESENTING
        event.timings.cue_dispatch_start_monotonic_ns = self._clock()
        self.current = event

        dispatch = None
        if self.present is not None:
            try:
                dispatch = self.present(event)
            except Exception:  # noqa: BLE001 - a dead cue channel ends this event, not the session
                log.exception("presenting event %s failed", event.message_id)
        if dispatch is not None:
            event.timings.led_command_complete_monotonic_ns = dispatch.led_command_complete_monotonic_ns
            event.timings.visual_painted_monotonic_ns = dispatch.visual_painted_monotonic_ns
            event.timings.haptic_command_complete_monotonic_ns = dispatch.haptic_command_complete_monotonic_ns
            event.timings.cue_ready_monotonic_ns = dispatch.cue_ready_monotonic_ns
            event.timings.cue_ready_wall_ns = dispatch.cue_ready_wall_ns
        else:
            # No cue backend (headless test, or every channel disabled):
            # ready is simply now, computed the same way.
            event.timings.compute_cue_ready(self._clock(), self._wall_clock())

        _safe(self.send_presented, event)

    # -- responses ------------------------------------------------------

    def on_note(self, note: int, wall_time_ns: Optional[int] = None) -> Optional[RemoteEvent]:
        """A MIDI note-on from the student's keyboard.

        Only counts while a cue is live - presses in the gap between
        events are not attributed to the next cue (the same convention
        the local quiz uses, which only matches presses at or after cue
        onset)."""
        if self.current is None or not self.running or self.paused:
            return None

        event = self.current
        event.timings.student_response_monotonic_ns = self._clock()
        event.timings.student_response_wall_ns = wall_time_ns if wall_time_ns is not None else self._wall_clock()
        event.actual_note = int(note)
        event.actual_key_id = self.mapping.key_for_note(int(note)) if self.mapping else None
        event.note_correct = event.target_note is not None and int(note) == event.target_note

        self._apply_finger_match(event, self.resolve_finger(int(note)) if self.resolve_finger else None)
        self._finish_current(timed_out=False)
        return event

    def _apply_finger_match(self, event: RemoteEvent, match: Optional[FingerMatch]) -> None:
        """Fills the finger verdict from a FingerMatch, using the shared
        threshold rule (app.finger_matching.is_finger_correct) - never a
        local re-definition of "right finger"."""
        if match is None:
            event.actual_finger = None
            event.finger_probabilities = None
            event.target_finger_probability = None
            event.finger_correct = False if event.target_finger else None
            return
        event.actual_finger = match.finger
        event.finger_probabilities = dict(match.probabilities)
        event.actual_finger_point = list(match.point) if match.point else None
        if event.target_finger:
            event.target_finger_probability = match.probabilities.get(event.target_finger, 0.0)
        event.finger_correct = is_finger_correct(match, event.target_finger)

    def apply_offline_match(self, index: int, match: Optional[FingerMatch]) -> Optional[RemoteEvent]:
        """Re-judge one event from the post-session offline video pass and
        mark it final. Same scoring path as the live verdict, just with
        better hand data."""
        event = next((e for e in self.events if e.index == index), None)
        if event is None:
            return None
        self._apply_finger_match(event, match)
        event.stage = STAGE_FINAL
        return event

    def _finish_current(self, timed_out: bool) -> None:
        event = self.current
        if event is None:
            return
        event.timed_out = timed_out
        if timed_out:
            event.note_correct = False
        event.state = STATE_DONE
        self.current = None
        if self.clear_cue is not None:
            try:
                self.clear_cue()
            except Exception:  # noqa: BLE001
                log.exception("clearing the cue failed")
        _safe(self.send_response, event)

    # -- results --------------------------------------------------------

    def results(self) -> List[QuizResult]:
        """Completed events as the platform's own QuizResult records -
        ready for app.quiz.save_quiz_results and app.quiz.summarize."""
        return [e.to_quiz_result() for e in self.events if e.state == STATE_DONE]

    def summary(self) -> Dict[str, Any]:
        """The session's headline numbers, computed by the *existing*
        app.quiz.summarize - this module deliberately owns no accuracy
        formula of its own."""
        from app.quiz import summarize

        return summarize(self.results())


def _safe(callback: Optional[Callable], *args: Any) -> None:
    """Run a session callback. A failed send (socket dropped mid-session)
    must not stall the cue sequence - the event is still recorded locally
    and the teacher catches up from the stored session afterwards."""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:  # noqa: BLE001
        log.exception("session callback %r failed", getattr(callback, "__name__", callback))


# ---------------------------------------------------------------------------
# Pre-recorded playback
# ---------------------------------------------------------------------------


class LocalRecordingScheduler:
    """Schedules a downloaded recording's events on the *student's* clock.

    The whole event list is fetched over REST before playback starts, so
    no individual note ever waits on the network - which is the point of
    the asynchronous mode. Two modes:

      - "paced": one event at a time; the next is released only after the
        current one is finished (response or timeout), so the student
        sets the pace.
      - "original_timing": events are released at the teacher's own
        recorded relative times, measured from the agreed start moment on
        this machine's monotonic clock.

    `start_monotonic_ns` is supplied by the caller. For a teacher-
    triggered start the caller maps the server's short-future start time
    onto this clock through its estimated offset (see timing.py); the
    mapping error affects only when playback begins, never the per-event
    cue timings, which are all measured locally."""

    def __init__(
        self,
        events: List[Dict[str, Any]],
        mode: str,
        start_monotonic_ns: int,
        clock: Callable[[], int] = mono_ns,
    ):
        self.events = sorted(events, key=lambda e: (e.get("event_order", 0), e.get("rel_time_s", 0.0)))
        self.mode = mode
        self.start_monotonic_ns = start_monotonic_ns
        self._clock = clock
        self._next = 0
        self.paused = False
        self._paused_at: Optional[int] = None

    @property
    def finished(self) -> bool:
        return self._next >= len(self.events)

    @property
    def remaining(self) -> int:
        return max(0, len(self.events) - self._next)

    def pause(self) -> None:
        if not self.paused:
            self.paused = True
            self._paused_at = self._clock()

    def resume(self) -> None:
        """Shifts the whole schedule by however long the pause lasted, so
        resuming does not fire a burst of events that came due while
        stopped."""
        if self.paused:
            if self._paused_at is not None:
                self.start_monotonic_ns += self._clock() - self._paused_at
            self.paused = False
            self._paused_at = None

    def due(self, session_idle: bool) -> Optional[Dict[str, Any]]:
        """The next event to present, or None if it is not time yet."""
        if self.paused or self.finished:
            return None
        event = self.events[self._next]
        if self.mode == "paced":
            if not session_idle:
                return None
        else:
            due_at = self.start_monotonic_ns + int(float(event.get("rel_time_s", 0.0)) * 1e9)
            if self._clock() < due_at:
                return None
        self._next += 1
        return event
