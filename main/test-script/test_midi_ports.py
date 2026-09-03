"""MIDI port identity: two identical keyboards must stay two keyboards.

The bug this guards against is silent. mido de-duplicates input ports by
name and resolves a name with list.index(), so with two keyboards of the
same model (the normal tele-training setup - see doc/REMOTE_GUIDANCE.md) the
second one vanishes from every picker, and anything asking for it by name
opens the first one instead. Nothing raises; the teacher simply receives
the student's notes.

app.midi therefore enumerates and opens through python-rtmidi by index.
None of these tests need a keyboard: rtmidi is faked.
"""

import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.midi import (  # noqa: E402
    MidiInputPort,
    MidiInputReader,
    MidiListener,
    _label_ports,
    ambiguous_port_names,
    list_input_port_details,
    list_input_ports,
    resolve_input_port,
)
from app.music_recording import RawMidiRecorder  # noqa: E402

NOTE_ON = 0x90
NOTE_OFF = 0x80


class FakeMidiIn:
    """Stands in for rtmidi.MidiIn. Records what it was asked to open."""

    ports: list = []
    busy: set = set()
    instances: list = []

    def __init__(self):
        self.opened_index = None
        self.closed = False
        self.deleted = False
        self.messages: list = []
        FakeMidiIn.instances.append(self)

    def get_ports(self):
        return list(FakeMidiIn.ports)

    def open_port(self, index):
        if index in FakeMidiIn.busy:
            raise OSError(f"port {index} is already in use")
        self.opened_index = index

    def get_message(self):
        return self.messages.pop(0) if self.messages else None

    def close_port(self):
        self.closed = True

    def delete(self):
        self.deleted = True


def fake_rtmidi(port_names, busy=()):
    FakeMidiIn.ports = list(port_names)
    FakeMidiIn.busy = set(busy)
    FakeMidiIn.instances = []
    return mock.patch.object(sys.modules["app.midi"], "rtmidi", types.SimpleNamespace(MidiIn=FakeMidiIn))


class PortLabellingTests(unittest.TestCase):
    def test_a_unique_name_is_left_exactly_as_the_driver_reports_it(self):
        """One keyboard must look the way it always did - a config.json
        written before labels existed still names its port this way."""
        ports = _label_ports(["SE25 MIDI1", "SE25 MIDI2"])
        self.assertEqual([p.label for p in ports], ["SE25 MIDI1", "SE25 MIDI2"])
        self.assertFalse(any(p.is_ambiguous for p in ports))

    def test_duplicate_names_are_numbered_in_enumeration_order(self):
        ports = _label_ports(["SE25 MIDI1", "SE25 MIDI2", "SE25 MIDI1", "SE25 MIDI2"])
        self.assertEqual(
            [(p.index, p.label) for p in ports],
            [
                (0, "SE25 MIDI1 #1"),
                (1, "SE25 MIDI2 #1"),
                (2, "SE25 MIDI1 #2"),
                (3, "SE25 MIDI2 #2"),
            ],
        )
        self.assertTrue(all(p.is_ambiguous for p in ports))

    def test_every_port_survives_and_every_label_is_unique(self):
        """The actual regression: mido returned 2 of these 4."""
        with fake_rtmidi(["SE25 MIDI1", "SE25 MIDI2", "SE25 MIDI1", "SE25 MIDI2"]):
            labels = list_input_ports()
        self.assertEqual(len(labels), 4)
        self.assertEqual(len(set(labels)), 4)

    def test_only_the_shared_name_gets_numbered(self):
        ports = _label_ports(["SE25 MIDI1", "Impact LX", "SE25 MIDI1"])
        self.assertEqual([p.label for p in ports], ["SE25 MIDI1 #1", "Impact LX", "SE25 MIDI1 #2"])

    def test_ambiguous_names_are_reported_for_a_ui_warning(self):
        with fake_rtmidi(["SE25 MIDI1", "Impact LX", "SE25 MIDI1"]):
            self.assertEqual(ambiguous_port_names(), ["SE25 MIDI1"])
        with fake_rtmidi(["SE25 MIDI1", "Impact LX"]):
            self.assertEqual(ambiguous_port_names(), [])

    def test_enumeration_failure_is_an_empty_list_not_a_crash(self):
        """A port picker opening on a machine with no MIDI stack at all."""
        broken = types.SimpleNamespace(MidiIn=mock.Mock(side_effect=OSError("no MIDI backend")))
        with mock.patch.object(sys.modules["app.midi"], "rtmidi", broken):
            self.assertEqual(list_input_port_details(), [])
            self.assertEqual(list_input_ports(), [])


class PortResolutionTests(unittest.TestCase):
    DUPLICATES = ["SE25 MIDI1", "SE25 MIDI2", "SE25 MIDI1", "SE25 MIDI2"]

    def test_a_label_resolves_to_its_own_port(self):
        with fake_rtmidi(self.DUPLICATES):
            self.assertEqual(resolve_input_port("SE25 MIDI1 #1").index, 0)
            self.assertEqual(resolve_input_port("SE25 MIDI1 #2").index, 2)
            self.assertEqual(resolve_input_port("SE25 MIDI2 #2").index, 3)

    def test_a_bare_name_from_an_older_config_still_works(self):
        """config.json and every profile's midi_mapping.json hold names
        written before labels existed. They must keep resolving - to the
        first match, which is what mido did."""
        with fake_rtmidi(self.DUPLICATES):
            port = resolve_input_port("SE25 MIDI1")
        self.assertEqual(port.index, 0)
        self.assertEqual(port.label, "SE25 MIDI1 #1")

    def test_none_means_the_first_port(self):
        with fake_rtmidi(self.DUPLICATES):
            self.assertEqual(resolve_input_port(None).index, 0)

    def test_an_unknown_name_lists_what_is_actually_there(self):
        with fake_rtmidi(self.DUPLICATES):
            with self.assertRaises(RuntimeError) as raised:
                resolve_input_port("Teacher Keyboard")
        message = str(raised.exception)
        self.assertIn("Teacher Keyboard", message)
        self.assertIn("SE25 MIDI1 #2", message)

    def test_no_ports_at_all_says_so(self):
        with fake_rtmidi([]):
            with self.assertRaisesRegex(RuntimeError, "No MIDI input ports available"):
                resolve_input_port(None)
            with self.assertRaisesRegex(RuntimeError, "No MIDI input ports available"):
                resolve_input_port("SE25 MIDI1")

    def test_a_device_genuinely_named_like_a_suffix_wins_over_the_suffix(self):
        """Exact labels are matched before anything else, so a real port
        called 'Keystation #2' is itself, not the second 'Keystation'."""
        with fake_rtmidi(["Keystation", "Keystation #2"]):
            port = resolve_input_port("Keystation #2")
        self.assertEqual(port.index, 1)
        self.assertEqual(port.raw_name, "Keystation #2")


class MidiInputReaderTests(unittest.TestCase):
    DUPLICATES = ["SE25 MIDI1", "SE25 MIDI2", "SE25 MIDI1", "SE25 MIDI2"]

    def test_the_second_identical_keyboard_is_opened_by_its_own_index(self):
        """The whole point. mido.open_input('SE25 MIDI1') could only ever
        open index 0."""
        with fake_rtmidi(self.DUPLICATES):
            reader = MidiInputReader("SE25 MIDI1 #2")
            self.assertEqual(reader._midi_in.opened_index, 2)
            self.assertEqual(reader.port_name, "SE25 MIDI1 #2")
            reader.close()

    def test_two_readers_hold_two_different_ports(self):
        with fake_rtmidi(self.DUPLICATES):
            teacher = MidiInputReader("SE25 MIDI1 #1")
            student = MidiInputReader("SE25 MIDI1 #2")
            self.assertNotEqual(teacher._midi_in.opened_index, student._midi_in.opened_index)
            teacher.close()
            student.close()

    def test_note_messages_are_decoded_and_everything_else_ignored(self):
        with fake_rtmidi(self.DUPLICATES):
            reader = MidiInputReader("SE25 MIDI1 #1")
            reader._midi_in.messages = [
                ([NOTE_ON | 0x03, 60, 100], 0.0),  # channel 4 - channel is not part of the note
                ([NOTE_OFF, 60, 0], 0.01),
                ([NOTE_ON, 62, 0], 0.02),  # running-status note-off, reported as mido reports it
                ([0xB0, 7, 127], 0.03),  # control change - not a note
                ([0xF8], 0.04),  # clock - too short to be a note
            ]
            messages = reader.poll()
            self.assertEqual(reader.poll(), [], "the queue should drain completely")
            reader.close()

        self.assertEqual(
            [(m.type, m.note, m.velocity) for m in messages],
            [("note_on", 60, 100), ("note_off", 60, 0), ("note_on", 62, 0)],
        )

    def test_a_port_that_cannot_be_opened_raises_and_releases_rtmidi(self):
        with fake_rtmidi(self.DUPLICATES, busy=[2]):
            with self.assertRaisesRegex(RuntimeError, "SE25 MIDI1 #2"):
                MidiInputReader("SE25 MIDI1 #2")
            self.assertTrue(FakeMidiIn.instances[-1].deleted, "the failed MidiIn was not deleted")

    def test_close_closes_and_deletes(self):
        with fake_rtmidi(self.DUPLICATES):
            reader = MidiInputReader("SE25 MIDI2 #2")
            reader.close()
            self.assertTrue(reader._midi_in.closed)
            self.assertTrue(reader._midi_in.deleted)


class ListenerAndRecorderTests(unittest.TestCase):
    """MidiListener and RawMidiRecorder are the two things every tool
    actually constructs; both must address the chosen keyboard and report
    the label they ended up on."""

    DUPLICATES = ["SE25 MIDI1", "SE25 MIDI2", "SE25 MIDI1", "SE25 MIDI2"]

    def _wait_for(self, predicate, timeout_s=2.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_listener_opens_the_requested_port_and_keeps_note_ons(self):
        with fake_rtmidi(self.DUPLICATES):
            listener = MidiListener("SE25 MIDI1 #2")
            self.addCleanup(listener.close)
            reader = listener._reader
            self.assertEqual(reader._midi_in.opened_index, 2)
            self.assertEqual(listener.port_name, "SE25 MIDI1 #2")

            reader._midi_in.messages = [
                ([NOTE_ON, 64, 90], 0.0),
                ([NOTE_ON, 65, 0], 0.01),  # a release, not a press
                ([NOTE_OFF, 64, 0], 0.02),
            ]
            self.assertTrue(self._wait_for(lambda: listener._events))
            events = listener.pop_events()

        self.assertEqual([e.note for e in events], [64])
        self.assertGreater(events[0].time, 1_600_000_000, "timestamps are absolute wall-clock time.time()")

    def test_listener_releases_the_port_when_it_stops(self):
        with fake_rtmidi(self.DUPLICATES):
            listener = MidiListener("SE25 MIDI1 #1")
            midi_in = listener._reader._midi_in
            listener.close()
            self.assertTrue(self._wait_for(lambda: midi_in.deleted))

    def test_recorder_opens_the_requested_port_and_keeps_both_edges(self):
        with fake_rtmidi(self.DUPLICATES):
            recorder = RawMidiRecorder("SE25 MIDI2 #2")
            self.addCleanup(recorder.close)
            reader = recorder._reader
            self.assertEqual(reader._midi_in.opened_index, 3)
            self.assertEqual(recorder.port_name, "SE25 MIDI2 #2")

            reader._midi_in.messages = [
                ([NOTE_ON, 60, 80], 0.0),
                ([NOTE_ON, 60, 0], 0.01),  # velocity-0 note-on is a release
                ([NOTE_OFF, 62, 0], 0.02),
            ]
            self.assertTrue(self._wait_for(lambda: len(recorder._events) >= 3))
            events = recorder.pop_events()

        self.assertEqual(
            [(e.type, e.note) for e in events],
            [("note_on", 60), ("note_off", 60), ("note_off", 62)],
        )

    def test_an_unknown_port_fails_where_the_caller_can_report_it(self):
        """Every caller catches RuntimeError from these constructors; a
        listener that failed to open on its worker thread would just look
        like a keyboard nobody is pressing."""
        with fake_rtmidi(self.DUPLICATES):
            with self.assertRaises(RuntimeError):
                MidiListener("Not A Keyboard")
            with self.assertRaises(RuntimeError):
                RawMidiRecorder("Not A Keyboard")


class NoThreadLeakTests(unittest.TestCase):
    def test_stopping_a_listener_ends_its_thread(self):
        before = threading.active_count()
        with fake_rtmidi(["SE25 MIDI1"]):
            listener = MidiListener()
            self.assertEqual(listener.port_name, "SE25 MIDI1")
            listener.close()
            deadline = time.monotonic() + 2.0
            while listener._thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertFalse(listener._thread.is_alive())
        self.assertLessEqual(threading.active_count(), before)


class MidiInputPortTests(unittest.TestCase):
    def test_str_is_the_label_so_it_can_be_printed_into_a_status_line(self):
        self.assertEqual(str(MidiInputPort(index=2, raw_name="SE25 MIDI1", label="SE25 MIDI1 #2")), "SE25 MIDI1 #2")


if __name__ == "__main__":
    unittest.main()
