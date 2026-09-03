"""Cueing a chord - several fingers at once, remote guidance only.

The teacher's chord detection already produces several GuidanceActions in
one envelope (app.finger_matching.match_notes_to_fingers). The student
used to cue actions[0] and drop the rest, so a three-finger chord arrived
as a one-finger instruction.

Three of the four channels can show a set: the vibration rig's command is
already a bitmask, the dot view is ten independent circles, and the key
LEDs are independent pixels. The hand-photo view is the exception - one
photo per finger and no combined assets - so it cycles through them
instead.

Two things these tests exist to hold down:

  - **The local quiz and the main user study must be unaffected.** They
    only ever call show_target()/set_target(), the single-finger API, and
    that path has to stay exactly what it was - no chord state, and above
    all no photo-cycling timer, since the visual cue is condition B's
    stimulus in an experiment that has already been run.
  - **Cueing a chord is not scoring a chord.** A chord is still judged on
    its primary note. Widening the cue must not quietly become a second
    definition of "correct" (doc/REMOTE_GUIDANCE.md §4.4, §4.11).
"""

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.haptic_cue import FINGER_TO_MOTOR, HapticCueOutput  # noqa: E402
from app.quiz import CueOutput  # noqa: E402
from remote_guidance.cue_outputs import CompositeCueOutput, LedKeyCue  # noqa: E402

CHORD = [(60, "R1"), (64, "R3"), (67, "R5")]


class FakeController:
    """Stands in for the vibration rig's serial controller."""

    def __init__(self):
        self.sent = []

    def connect(self):
        return self

    def send(self, command):
        self.sent.append(command)

    def stop_all(self):
        self.sent.append("STOP")

    def close(self):
        pass


class FakeStrip:
    def __init__(self):
        self.events = []

    @contextmanager
    def batch(self, show=True):
        self.events.append("BATCH-OPEN")
        try:
            yield self
        finally:
            self.events.append("SHOW")


class FakeMapper:
    """note_led_map.NoteLEDMapper's two-method surface, plus the strip the
    real one exposes as .led so LedKeyCue can batch through it."""

    def __init__(self, unmapped=()):
        self.led = FakeStrip()
        self.unmapped = set(unmapped)

    def light_key(self, note):
        if note in self.unmapped:
            return False
        self.led.events.append(f"light {note}")
        return True

    def clear_key(self, note):
        self.led.events.append(f"clear {note}")
        return True


class RecordingCue(CueOutput):
    def __init__(self):
        self.calls = []

    def show_target(self, note, finger):
        self.calls.append(("single", note, finger))

    def show_message(self, text):
        pass

    def clear(self):
        self.calls.append(("clear",))


class MultiCue(RecordingCue):
    def show_targets(self, targets):
        self.calls.append(("multi", list(targets)))


class HapticChordTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.cue = HapticCueOutput(controller=self.controller)
        self.controller.sent.clear()

    def test_one_finger_sends_exactly_what_it_always_did(self):
        self.cue.show_target(60, "R1")
        self.assertEqual(self.controller.sent, [f"S {1 << FINGER_TO_MOTOR['R1']} {self.cue.amplitude}"])

    def test_a_chord_is_one_command_with_the_motors_or_ed_together(self):
        """The rig's command is a bitmask, so the whole set starts
        together - not a burst of writes with the last finger late."""
        self.cue.show_targets(CHORD)

        commands = [c for c in self.controller.sent if c.startswith("S ")]
        self.assertEqual(len(commands), 1, f"expected one drive command, got {self.controller.sent}")
        expected = 0
        for _, finger in CHORD:
            expected |= 1 << FINGER_TO_MOTOR[finger]
        self.assertEqual(commands[0], f"S {expected} {self.cue.amplitude}")

    def test_a_single_target_list_goes_through_the_unchanged_path(self):
        self.cue.show_targets([(60, "R1")])
        self.assertEqual(
            [c for c in self.controller.sent if c.startswith("S ")],
            [f"S {1 << FINGER_TO_MOTOR['R1']} {self.cue.amplitude}"],
        )

    def test_unresolved_fingers_buzz_nothing_rather_than_everything(self):
        self.cue.show_targets([(60, None), (64, None)])
        self.assertEqual([c for c in self.controller.sent if c.startswith("S ")], [])

    def test_clearing_stops_every_motor_of_a_chord(self):
        self.cue.show_targets(CHORD)
        self.controller.sent.clear()
        self.cue.clear()
        self.assertIn("STOP", self.controller.sent)


class VisualChordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _cue(self, style):
        from app.gui.cue_window import ScreenCueOutput

        cue = ScreenCueOutput(style)
        self.addCleanup(cue.close)
        return cue, cue.window.widget

    def test_the_dot_view_lights_every_finger_of_a_chord(self):
        cue, widget = self._cue("dot")
        cue.show_targets(CHORD)
        self.assertEqual(widget.active_fingers, ["R1", "R3", "R5"])

    def test_the_dot_view_needs_no_cycling(self):
        """Ten circles can show a set outright."""
        cue, widget = self._cue("dot")
        cue.show_targets(CHORD)
        self.assertFalse(widget.cycling)

    def test_the_hand_view_cycles_through_the_chord_s_photos(self):
        cue, widget = self._cue("hand")
        cue.show_targets(CHORD)
        self.assertTrue(widget.cycling, "the hand style has no combined photo, so it must cycle")

        seen = [widget.active_fingers[widget._cycle_index]]
        for _ in range(len(CHORD) - 1):
            widget._advance_cycle()
            seen.append(widget.active_fingers[widget._cycle_index])
        self.assertEqual(seen, ["R1", "R3", "R5"])

        widget._advance_cycle()  # wraps
        self.assertEqual(widget.active_fingers[widget._cycle_index], "R1")

    def test_the_label_names_the_whole_chord_in_both_styles(self):
        """The photos can only be shown one at a time, so the text is what
        makes the chord unambiguous."""
        for style in ("dot", "hand"):
            cue, widget = self._cue(style)
            cue.show_targets(CHORD)
            for finger in ("R1", "R3", "R5"):
                self.assertIn(finger, widget.message)
            self.assertIn("C4", widget.message)
            self.assertIn("G4", widget.message)

    def test_clearing_stops_the_cycle(self):
        cue, widget = self._cue("hand")
        cue.show_targets(CHORD)
        cue.clear()
        self.assertFalse(widget.cycling)
        self.assertEqual(widget.active_fingers, [])

    def test_switching_style_away_from_hand_stops_the_cycle(self):
        cue, widget = self._cue("hand")
        cue.show_targets(CHORD)
        cue.set_style("dot")
        self.assertFalse(widget.cycling)
        self.assertEqual(widget.active_fingers, ["R1", "R3", "R5"], "the chord itself must survive the switch")


class SingleFingerPathIsUntouchedTests(unittest.TestCase):
    """The guarantee the local quiz and the main user study rest on.

    student_quiz.py and app/gui/experiment_cue.py only ever call the
    singular API. If cueing one finger ever started a timer or left chord
    state behind, the visual stimulus of an already-run experiment would
    have changed underneath its data."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, style):
        from app.gui.cue_window import CueWindow

        window = CueWindow(style)
        self.addCleanup(window.close)
        return window.widget

    def test_set_target_never_starts_a_cycle_in_either_style(self):
        for style in ("dot", "hand"):
            widget = self._widget(style)
            widget.set_target("R1", "Press: C4")
            self.assertFalse(widget.cycling, f"{style}: a single finger must not animate")
            self.assertEqual(widget.active_fingers, ["R1"])
            self.assertEqual(widget.active_finger, "R1")

    def test_set_target_clears_chord_state_left_by_a_previous_cue(self):
        widget = self._widget("hand")
        widget.set_targets(["R1", "R3", "R5"], "chord")
        self.assertTrue(widget.cycling)
        widget.set_target("L2", "Press: D3")
        self.assertFalse(widget.cycling)
        self.assertEqual(widget.active_fingers, ["L2"])

    def test_status_only_mode_stops_everything(self):
        """The main study's key-only and haptic trials use this so no
        visual finger cue can leak into a condition without one."""
        widget = self._widget("hand")
        widget.set_targets(["R1", "R3"], "chord")
        widget.set_status("KEY-LIGHT ONLY")
        self.assertFalse(widget.cycling)
        self.assertEqual(widget.active_fingers, [])
        self.assertTrue(widget.status_only)

    def test_the_experiment_cue_does_not_inherit_chord_behaviour(self):
        """ExperimentCue takes app.quiz.CueOutput's default show_targets,
        which cues the primary target only - the main study never shows a
        chord, and this is what keeps it that way."""
        from app.gui.experiment_cue import ExperimentCue

        self.assertIs(ExperimentCue.show_targets, CueOutput.show_targets)

    def test_the_default_implementation_cues_the_primary_target_only(self):
        cue = RecordingCue()
        cue.show_targets(CHORD)
        self.assertEqual(cue.calls, [("single", 60, "R1")])


class LedChordTests(unittest.TestCase):
    def test_every_key_of_a_chord_lights_in_one_flush(self):
        mapper = FakeMapper()
        led = LedKeyCue(mapper)
        stamp = led.light_many([60, 64, 67])

        self.assertIsNotNone(stamp)
        self.assertEqual(
            mapper.led.events,
            ["BATCH-OPEN", "light 60", "light 64", "light 67", "SHOW"],
            "the strip must refresh once, not once per key",
        )

    def test_clearing_turns_off_every_key_of_the_chord(self):
        mapper = FakeMapper()
        led = LedKeyCue(mapper)
        led.light_many([60, 64, 67])
        mapper.led.events.clear()
        led.clear()
        self.assertEqual(mapper.led.events, ["clear 60", "clear 64", "clear 67"])

    def test_a_new_cue_clears_the_previous_chord_first(self):
        mapper = FakeMapper()
        led = LedKeyCue(mapper)
        led.light_many([60, 64])
        mapper.led.events.clear()
        led.light_many([72])
        self.assertEqual(mapper.led.events[:2], ["clear 60", "clear 64"])

    def test_light_still_takes_one_note(self):
        mapper = FakeMapper()
        led = LedKeyCue(mapper)
        self.assertIsNotNone(led.light(60))
        self.assertEqual(led._lit_note, 60)

    def test_notes_with_no_led_mapping_do_not_count_as_lit(self):
        mapper = FakeMapper(unmapped=[60, 64])
        led = LedKeyCue(mapper)
        self.assertIsNone(led.light_many([60, 64]), "nothing lit means no completion stamp")

    def test_a_partly_mapped_chord_still_stamps_and_only_clears_what_lit(self):
        mapper = FakeMapper(unmapped=[64])
        led = LedKeyCue(mapper)
        self.assertIsNotNone(led.light_many([60, 64, 67]))
        mapper.led.events.clear()
        led.clear()
        self.assertEqual(mapper.led.events, ["clear 60", "clear 67"])


class CompositeChordTests(unittest.TestCase):
    def _composite(self, **kwargs):
        return CompositeCueOutput(led=LedKeyCue(FakeMapper()), **kwargs)

    def test_every_channel_is_given_the_whole_chord(self):
        visual, haptic = MultiCue(), MultiCue()
        self._composite(visual=visual, haptic=haptic).show_targets(CHORD)

        self.assertEqual(visual.calls, [("multi", CHORD)])
        self.assertEqual(haptic.calls, [("multi", CHORD)])

    def test_a_chord_is_one_cue_event_with_one_cue_ready(self):
        """Reaction time needs exactly one origin, however many notes the
        cue names (§4.1)."""
        dispatch = self._composite(visual=MultiCue(), haptic=MultiCue()).show_targets(CHORD)

        self.assertGreater(dispatch.cue_ready_monotonic_ns, 0)
        self.assertEqual(dispatch.errors, [])
        self.assertEqual(
            dispatch.cue_ready_monotonic_ns,
            max(
                t
                for t in (
                    dispatch.led_command_complete_monotonic_ns,
                    dispatch.visual_painted_monotonic_ns,
                    dispatch.haptic_command_complete_monotonic_ns,
                )
                if t is not None
            ),
        )

    def test_show_target_still_works_and_reaches_a_channel(self):
        visual = MultiCue()
        self._composite(visual=visual).show_target(60, "R1")
        self.assertEqual(visual.calls, [("multi", [(60, "R1")])])

    def test_a_channel_that_cannot_show_a_set_falls_back_on_its_own(self):
        """CompositeCueOutput does not special-case anything: a channel
        without its own show_targets uses CueOutput's default."""
        visual = RecordingCue()  # no show_targets override
        self._composite(visual=visual).show_targets(CHORD)
        self.assertEqual(visual.calls, [("single", 60, "R1")])

    def test_a_failing_channel_does_not_stop_the_others(self):
        class Exploding(MultiCue):
            def show_targets(self, targets):
                raise RuntimeError("boom")

        visual = MultiCue()
        dispatch = self._composite(visual=visual, haptic=Exploding()).show_targets(CHORD)
        self.assertEqual(visual.calls, [("multi", CHORD)])
        self.assertTrue(any("haptic" in e for e in dispatch.errors))
        self.assertIsNone(dispatch.haptic_command_complete_monotonic_ns, "a failed channel must not fake a stamp")


class ScoringStaysSingleNoteTests(unittest.TestCase):
    """The decision this feature was scoped around: the cue widened, the
    scoring did not."""

    def _event(self):
        from remote_guidance.protocol import GuidanceAction, guidance_payload, make_envelope
        from remote_guidance.student.session import StudentSession

        session = StudentSession()
        session.start()
        envelope = make_envelope(
            "guidance.live",
            payload=guidance_payload([GuidanceAction(note=note, finger=finger) for note, finger in CHORD]),
        )
        return session, session.accept(envelope)

    def test_a_chord_arrives_as_several_actions(self):
        _, event = self._event()
        self.assertEqual([(a.note, a.finger) for a in event.actions], CHORD)

    def test_the_primary_note_is_still_what_gets_scored(self):
        _, event = self._event()
        self.assertEqual(event.target_note, 60)
        self.assertEqual(event.target_finger, "R1")

    def test_the_first_press_finishes_the_event_and_is_judged_against_the_primary(self):
        session, event = self._event()
        session.tick()  # present it
        scored = session.on_note(60)
        self.assertIsNotNone(scored)
        self.assertTrue(scored.note_correct)

    def test_pressing_a_different_chord_note_is_not_credited(self):
        """Documented consequence, not an accident: E4 is in the chord but
        is not the primary note, so it scores as wrong."""
        session, event = self._event()
        session.tick()
        scored = session.on_note(64)
        self.assertFalse(scored.note_correct)


class StudentWindowPresentsTheWholeChordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_present_event_passes_every_action_to_the_cue(self):
        from unittest import mock

        from remote_guidance.protocol import GuidanceAction
        from remote_guidance.student.session import RemoteEvent
        from remote_guidance.student.window import StudentRemoteWindow

        event = RemoteEvent(
            index=0,
            message_id="m1",
            seq=1,
            actions=[GuidanceAction(note=note, finger=finger) for note, finger in CHORD],
            timeout_s=5.0,
        )
        window = mock.Mock(spec=StudentRemoteWindow)
        window.cue = MultiCue()
        StudentRemoteWindow._present_event(window, event)

        self.assertEqual(window.cue.calls, [("multi", CHORD)])


if __name__ == "__main__":
    unittest.main()
