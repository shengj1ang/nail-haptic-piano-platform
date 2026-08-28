"""Unit tests for the client-side remote guidance core: the config
block, the per-role Config views, the composite cue output, the session's
timing rules, latency statistics and the launcher's process actions.

The point of most of these is that a mistake here would be silent and
serious: a remote save quietly moving the local quiz's camera, the LED
strip and the vibration rig ending up on one port, or a reaction time
that starts on the teacher's machine instead of the student's.

Every test runs against a TEMPORARY config.json - the project's own
config file is never read or written - and no test touches a camera, a
serial port, a MIDI device or a network socket.

Run from main/:  python -m pytest test-script/test_remote_guidance_config.py
"""

import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import CameraConfig, Config  # noqa: E402
from app.finger_matching import FingerMatch  # noqa: E402
from app.quiz import CueOutput  # noqa: E402
from remote_guidance import config as rgc  # noqa: E402
from remote_guidance.cue_outputs import (  # noqa: E402
    CompositeCueOutput,
    LedKeyCue,
    build_student_cue,
)
from remote_guidance.protocol import GuidanceAction, guidance_payload, make_envelope  # noqa: E402
from remote_guidance.student.session import (  # noqa: E402
    LocalRecordingScheduler,
    StudentSession,
)
from remote_guidance.timing import (  # noqa: E402
    ClockOffset,
    ClockOffsetEstimator,
    DispatchTimings,
    one_way_estimate,
    percentile,
    summarize_latency,
)

# A config.json exactly as it looked before this module existed - no
# remote_guidance key anywhere in it.
LEGACY_CONFIG = {
    "camera": {"index": 3, "width": 640, "height": 480, "fps": 15, "flip_vertical": True, "flip_horizontal": False},
    "midi": {"port_name": "Local Keyboard"},
    "active_keyboard_profile": "lab-profile-2026",
    "visual_cue_style": "hand",
    "seeds": {"sequence_generator": 42, "pilot_schedule": 7},
    "haptic": {"using": "lra", "lra": {"default_frequency": 224, "default_amp": 64}},
}


class _TempConfigCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self._dir.name) / "config.json"

    def tearDown(self):
        self._dir.cleanup()

    def write(self, payload: dict) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def read(self) -> dict:
        with open(self.config_path, encoding="utf-8") as f:
            return json.load(f)


class BackwardCompatibilityTests(_TempConfigCase):
    def test_config_without_remote_block_loads_defaults(self):
        """An older config.json must not break the remote module."""
        self.write(LEGACY_CONFIG)
        remote = rgc.RemoteGuidanceConfig.load(self.config_path)

        self.assertEqual(remote.schema_version, rgc.SCHEMA_VERSION)
        self.assertEqual(remote.network.server_url, rgc.NetworkConfig().server_url)
        self.assertEqual(remote.student.default_guidance_mode, "both")
        self.assertIsNone(remote.student.led.port)
        self.assertEqual(remote.validate(), [])

    def test_loading_does_not_write_the_file(self):
        self.write(LEGACY_CONFIG)
        before = self.read()
        rgc.RemoteGuidanceConfig.load(self.config_path)
        self.assertEqual(self.read(), before)

    def test_missing_file_yields_defaults(self):
        remote = rgc.RemoteGuidanceConfig.load(self.config_path / "nope.json")
        self.assertEqual(remote.schema_version, rgc.SCHEMA_VERSION)

    def test_partial_block_fills_in_the_rest(self):
        payload = dict(LEGACY_CONFIG)
        payload["remote_guidance"] = {"network": {"server_url": "https://relay.example.ac.uk"}}
        self.write(payload)

        remote = rgc.RemoteGuidanceConfig.load(self.config_path)
        self.assertEqual(remote.network.server_url, "https://relay.example.ac.uk")
        self.assertEqual(remote.network.heartbeat_interval_s, rgc.NetworkConfig().heartbeat_interval_s)
        self.assertEqual(remote.student.keyboard_profile, "default")

    def test_unknown_keys_are_ignored_not_fatal(self):
        payload = dict(LEGACY_CONFIG)
        payload["remote_guidance"] = {
            "schema_version": rgc.SCHEMA_VERSION,
            "network": {"server_url": "http://x:1", "invented_by_a_newer_build": True},
            "student": {"camera": {"index": 1, "future_field": 9}},
        }
        self.write(payload)
        remote = rgc.RemoteGuidanceConfig.load(self.config_path)
        self.assertEqual(remote.student.camera.index, 1)

    def test_ws_url_follows_the_http_scheme(self):
        remote = rgc.RemoteGuidanceConfig()
        remote.network.server_url = "https://relay.example.ac.uk"
        self.assertEqual(remote.network.ws_base, "wss://relay.example.ac.uk")
        remote.network.server_url = "http://127.0.0.1:8765"
        self.assertEqual(remote.network.ws_base, "ws://127.0.0.1:8765")
        self.assertEqual(
            remote.network.room_ws_url("abc"), "ws://127.0.0.1:8765/ws/v1/rooms/abc"
        )


class SaveIsolationTests(_TempConfigCase):
    def test_saving_remote_config_leaves_shared_settings_alone(self):
        """The whole point of the separate block: saving remote settings
        must never move the ordinary tools' camera, MIDI port or
        keyboard profile."""
        self.write(LEGACY_CONFIG)

        remote = rgc.RemoteGuidanceConfig.load(self.config_path)
        remote.student.camera = CameraConfig(index=9, width=1920, height=1080, fps=60)
        remote.student.midi.port_name = "Student Keyboard"
        remote.student.keyboard_profile = "student-desk"
        remote.teacher.camera = CameraConfig(index=8)
        remote.teacher.midi.port_name = "Teacher Keyboard"
        remote.teacher.keyboard_profile = "teacher-desk"
        remote.save(self.config_path)

        saved = self.read()
        self.assertEqual(saved["camera"], LEGACY_CONFIG["camera"])
        self.assertEqual(saved["midi"], LEGACY_CONFIG["midi"])
        self.assertEqual(saved["active_keyboard_profile"], LEGACY_CONFIG["active_keyboard_profile"])
        self.assertEqual(saved["visual_cue_style"], LEGACY_CONFIG["visual_cue_style"])
        self.assertEqual(saved["seeds"], LEGACY_CONFIG["seeds"])
        self.assertEqual(saved["remote_guidance"]["student"]["camera"]["index"], 9)

    def test_save_then_load_round_trips(self):
        self.write(LEGACY_CONFIG)
        remote = rgc.RemoteGuidanceConfig.load(self.config_path)
        remote.student.led.port = "/dev/tty.led"
        remote.student.haptic.port = "/dev/tty.haptic"
        remote.local_server.port = 9999
        remote.save(self.config_path)

        reloaded = rgc.RemoteGuidanceConfig.load(self.config_path)
        self.assertEqual(reloaded.student.led.port, "/dev/tty.led")
        self.assertEqual(reloaded.student.haptic.port, "/dev/tty.haptic")
        self.assertEqual(reloaded.local_server.port, 9999)

    def test_a_normal_config_save_keeps_the_remote_block(self):
        """The reverse direction: Config.save() merges rather than
        replaces, so an ordinary tool writing its own settings must not
        delete the remote block."""
        self.write(LEGACY_CONFIG)
        remote = rgc.RemoteGuidanceConfig.load(self.config_path)
        remote.network.server_url = "https://relay.example.ac.uk"
        remote.save(self.config_path)

        cfg = Config.load(self.config_path)
        cfg.camera.index = 5
        cfg.save(self.config_path)

        saved = self.read()
        self.assertEqual(saved["camera"]["index"], 5)
        self.assertEqual(saved["remote_guidance"]["network"]["server_url"], "https://relay.example.ac.uk")


class RoleIsolationTests(_TempConfigCase):
    def setUp(self):
        super().setUp()
        self.write(LEGACY_CONFIG)
        self.cfg = Config.load(self.config_path)
        self.remote = rgc.RemoteGuidanceConfig()
        self.remote.student.camera = CameraConfig(index=1, width=1280, height=720, fps=30)
        self.remote.student.midi.port_name = "Student Keyboard"
        self.remote.student.keyboard_profile = "student-desk"
        self.remote.teacher.camera = CameraConfig(index=2, width=640, height=480, fps=60)
        self.remote.teacher.midi.port_name = "Teacher Keyboard"
        self.remote.teacher.keyboard_profile = "teacher-desk"

    def test_each_role_gets_its_own_devices(self):
        student = rgc.student_config(self.cfg, self.remote)
        teacher = rgc.teacher_config(self.cfg, self.remote)

        self.assertEqual(student.camera.index, 1)
        self.assertEqual(teacher.camera.index, 2)
        self.assertEqual(student.midi.port_name, "Student Keyboard")
        self.assertEqual(teacher.midi.port_name, "Teacher Keyboard")
        self.assertEqual(student.active_keyboard_profile, "student-desk")
        self.assertEqual(teacher.active_keyboard_profile, "teacher-desk")

    def test_role_views_do_not_mutate_the_shared_config(self):
        """Opening a remote window must not repoint the camera every
        other tool in the process is using."""
        rgc.student_config(self.cfg, self.remote)
        rgc.teacher_config(self.cfg, self.remote)

        self.assertEqual(self.cfg.camera.index, LEGACY_CONFIG["camera"]["index"])
        self.assertEqual(self.cfg.midi.port_name, LEGACY_CONFIG["midi"]["port_name"])
        self.assertEqual(self.cfg.active_keyboard_profile, LEGACY_CONFIG["active_keyboard_profile"])

    def test_editing_a_role_view_does_not_leak_back(self):
        student = rgc.student_config(self.cfg, self.remote)
        student.camera.index = 77
        student.midi.port_name = "changed"

        self.assertEqual(self.cfg.camera.index, LEGACY_CONFIG["camera"]["index"])
        self.assertEqual(self.remote.student.camera.index, 1)
        self.assertEqual(self.remote.student.midi.port_name, "Student Keyboard")

    def test_the_two_role_views_are_independent(self):
        student = rgc.student_config(self.cfg, self.remote)
        teacher = rgc.teacher_config(self.cfg, self.remote)
        student.camera.width = 320
        self.assertEqual(teacher.camera.width, 640)

    def test_unknown_role_is_refused(self):
        with self.assertRaises(rgc.RemoteConfigError):
            rgc.role_config(self.cfg, self.remote, "supervisor")


class TeacherMidiEventSharingTests(unittest.TestCase):
    """Teacher guidance and local sound must share one MIDI input."""

    def test_one_reader_returns_guidance_note_ons_and_audio_releases(self):
        from unittest import mock

        from app.midi import MidiMessage
        from remote_guidance.teacher.live_detector import TeacherLiveDetector

        class FakeReader:
            instances = []

            def __init__(self, port_name):
                self.port_name = port_name
                self.closed = False
                self.__class__.instances.append(self)

            def poll(self):
                return [
                    MidiMessage("note_on", 60, 100),
                    MidiMessage("note_off", 60, 64),
                    MidiMessage("note_on", 61, 0),
                ]

            def close(self):
                self.closed = True

        cfg = Config()
        cfg.midi.port_name = "Teacher Keyboard"
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "remote_guidance.teacher.live_detector.MidiInputReader", FakeReader
        ):
            detector = TeacherLiveDetector(cfg, profile_data_dir=Path(directory), open_camera=False)
            self.assertEqual(detector.connect_midi(), "Teacher Keyboard")
            detector.camera = mock.MagicMock()
            result = detector.poll()
            detector.camera.read.assert_not_called()
            detector.close()

        self.assertEqual(len(FakeReader.instances), 1)
        self.assertEqual([action.note for action in result.actions], [60])
        self.assertEqual([event.note for event in result.midi_events], [60, 60, 61])
        self.assertTrue(FakeReader.instances[0].closed)


class VisionWorkerTests(unittest.TestCase):
    def test_latest_frame_is_processed_off_thread_and_resources_close(self):
        import threading

        from remote_guidance.vision_worker import LatestVisionWorker

        class Camera:
            def __init__(self):
                self.calls = 0
                self.released = False

            def read(self):
                self.calls += 1
                return {"frame": self.calls}

            def release(self):
                self.released = True

        class Tracker:
            def __init__(self):
                self.closed = False
                self.called_on = None

            def process(self, frame):
                self.called_on = threading.current_thread().name
                return {"Right": frame["frame"]}

            def close(self):
                self.closed = True

        camera, tracker = Camera(), Tracker()
        worker = LatestVisionWorker(
            camera,
            tracker,
            annotate=lambda frame, _hands: frame.update(annotated=True),
            name="test-remote-vision",
        )
        worker.start()
        snapshot = worker.wait_for_frame(timeout=1.0)
        worker.close()

        self.assertGreater(snapshot.sequence, 0)
        self.assertTrue(snapshot.frame["annotated"])
        self.assertEqual(snapshot.hands["Right"], snapshot.frame["frame"])
        self.assertEqual(tracker.called_on, "test-remote-vision")
        self.assertTrue(camera.released)
        self.assertTrue(tracker.closed)

    def test_a_slow_tracker_does_not_slow_the_camera(self):
        """What a stuttering recording is made of.

        Reading the camera and running MediaPipe in one loop cycles at the
        slower of the two, so the recording inherited the tracker's rate
        and the GUI padded the difference by repeating the last frame it
        saw. A real student session came out 56.6% duplicated at ~13
        distinct fps, freezing up to 0.3s at a time; the same camera in
        the local quiz, which has no tracking in its capture loop, was
        0%."""
        import threading

        from remote_guidance.vision_worker import LatestVisionWorker

        class Camera:
            def __init__(self):
                self.reads = 0

            def read(self):
                self.reads += 1
                return {"n": self.reads}

            def release(self):
                pass

        class BlockedTracker:
            def __init__(self):
                self.release = threading.Event()

            def process(self, _frame):
                self.release.wait(timeout=2.0)
                return {}

            def close(self):
                pass

        camera, tracker = Camera(), BlockedTracker()
        worker = LatestVisionWorker(
            camera, tracker, name="test-slow-tracker", max_fps=200.0, keep_raw=True
        )
        try:
            worker.start()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and camera.reads < 10:
                time.sleep(0.005)
            captured = camera.reads
            tracked = worker.snapshot().sequence
        finally:
            tracker.release.set()
            worker.close()

        self.assertGreaterEqual(
            captured, 10, "capture stalled behind the tracker - the recording would freeze"
        )
        self.assertEqual(tracked, 0, "the tracker was blocked throughout, so nothing should have completed")
        self.assertEqual(worker.latest_raw_frame()["n"], captured)

    def test_the_overlay_never_touches_the_frame_being_recorded(self):
        """draw_hands() paints the skeleton, the joints and the finger
        labels straight over the hands. The offline pass has to find those
        same hands again in the recording, so what is written has to be
        the frame the camera produced."""
        from remote_guidance.vision_worker import LatestVisionWorker

        produced = []

        class Camera:
            def read(self):
                frame = {"pixels": "clean"}
                produced.append(frame)
                return frame

            def release(self):
                pass

        class Tracker:
            def process(self, _frame):
                return {"Right": {}}

            def close(self):
                pass

        worker = LatestVisionWorker(
            Camera(),
            Tracker(),
            annotate=lambda frame, _hands: frame.update(pixels="skeleton drawn on"),
            name="test-overlay-isolation",
            keep_raw=True,
        )
        try:
            worker.start()
            snapshot = worker.wait_for_frame(timeout=1.0)
        finally:
            worker.close()

        self.assertEqual(snapshot.frame["pixels"], "skeleton drawn on", "the preview should be annotated")
        self.assertEqual(snapshot.raw_frame["pixels"], "clean", "the recorded frame must not be drawn on")
        for frame in produced:
            self.assertEqual(frame["pixels"], "clean", "a camera frame was annotated in place")

    def test_keep_raw_carries_the_frame_from_before_the_overlay(self):
        """The teacher preview paints the whole key map over the frame,
        hands included. Recording that would hand the offline finger pass
        a video MediaPipe cannot track in, so a recording asks for the
        frame as the camera produced it."""
        from remote_guidance.vision_worker import LatestVisionWorker

        class Camera:
            def read(self):
                return {"pixels": "clean"}

            def release(self):
                pass

        class Tracker:
            def process(self, _frame):
                return {}

            def close(self):
                pass

        worker = LatestVisionWorker(
            Camera(),
            Tracker(),
            annotate=lambda frame, _hands: frame.update(pixels="overlaid"),
            name="test-raw-vision",
        )
        try:
            worker.start()
            plain = worker.wait_for_frame(timeout=1.0)
            self.assertIsNone(plain.raw_frame, "the copy must cost nothing until a recording wants it")

            worker.set_keep_raw(True)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                snapshot = worker.snapshot()
                if snapshot.raw_frame is not None:
                    break
                time.sleep(0.005)
        finally:
            worker.close()

        self.assertIsNotNone(snapshot.raw_frame, "keep_raw produced no unannotated frame")
        self.assertEqual(snapshot.raw_frame["pixels"], "clean")
        self.assertEqual(snapshot.frame["pixels"], "overlaid")


class SerialPortValidationTests(unittest.TestCase):
    """The LED strip and the vibration rig are separate boards, and
    auto-detection scores them similarly. One port for both would drive a
    cue onto the wrong device."""

    def test_identical_ports_are_rejected(self):
        remote = rgc.RemoteGuidanceConfig()
        remote.student.led.port = "/dev/tty.usbmodem1101"
        remote.student.haptic.port = "/dev/tty.usbmodem1101"

        problems = remote.serial_port_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("usbmodem1101", problems[0])
        self.assertIn(problems[0], remote.validate())
        with self.assertRaises(rgc.RemoteConfigError):
            remote.require_valid()

    def test_different_ports_are_accepted(self):
        remote = rgc.RemoteGuidanceConfig()
        remote.student.led.port = "/dev/tty.usbmodem1101"
        remote.student.haptic.port = "/dev/tty.usbmodem2201"
        self.assertEqual(remote.serial_port_problems(), [])

    def test_both_unset_is_allowed(self):
        """Auto-detection is still fine when only one board is attached."""
        self.assertEqual(rgc.RemoteGuidanceConfig().serial_port_problems(), [])

    def test_only_one_set_is_allowed(self):
        remote = rgc.RemoteGuidanceConfig()
        remote.student.led.port = "/dev/tty.usbmodem1101"
        self.assertEqual(remote.serial_port_problems(), [])

    def test_bad_guidance_mode_and_url_are_reported(self):
        remote = rgc.RemoteGuidanceConfig()
        remote.student.default_guidance_mode = "telepathy"
        remote.network.server_url = "relay.example.ac.uk"
        problems = remote.validate()
        self.assertTrue(any("guidance_mode" in p for p in problems))
        self.assertTrue(any("server_url" in p for p in problems))


# ---------------------------------------------------------------------------
# Cue output
# ---------------------------------------------------------------------------


class FakeCue(CueOutput):
    """Stands in for ScreenCueOutput / HapticCueOutput - same interface,
    no window and no serial port."""

    def __init__(self, name: str, flushes: bool = False):
        self.name = name
        self.flushes = flushes
        self.targets = []
        self.messages = []
        self.cleared = 0
        self.closed = 0

    def show_target(self, note, finger):
        self.targets.append((note, finger))

    def show_message(self, text):
        self.messages.append(text)

    def clear(self):
        self.cleared += 1

    def close(self):
        self.closed += 1

    def flush(self):
        if self.flushes:
            self.messages.append("__flush__")


class FakeMapper:
    def __init__(self, known=(60, 62, 64)):
        self.known = set(known)
        self.lit = []
        self.cleared = []

    def light_key(self, note):
        if note not in self.known:
            return False
        self.lit.append(note)
        return True

    def clear_key(self, note):
        self.cleared.append(note)
        return True


class _StepClock:
    """Monotonic-looking clock that advances a fixed amount per read, so
    a test can assert on which stamp is the maximum."""

    def __init__(self, start=1_000_000_000, step=1_000_000):
        self.value = start
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


class CompositeCueTests(unittest.TestCase):
    def test_visual_mode_has_no_haptic_channel(self):
        visual = FakeCue("visual")
        cue = build_student_cue("visual", led_mapper=FakeMapper(), visual_factory=lambda: visual,
                                haptic_factory=lambda: FakeCue("haptic"))
        self.assertEqual(cue.enabled_channels, ["led", "visual"])
        cue.show_target(60, "R1")
        self.assertEqual(visual.targets, [(60, "R1")])

    def test_haptic_mode_never_opens_a_cue_window(self):
        """The visual factory must not even be called, or a haptic-only
        session would put a finger cue on screen."""
        calls = []
        cue = build_student_cue(
            "haptic",
            led_mapper=FakeMapper(),
            visual_factory=lambda: calls.append("visual") or FakeCue("visual"),
            haptic_factory=lambda: FakeCue("haptic"),
        )
        self.assertEqual(calls, [])
        self.assertEqual(cue.enabled_channels, ["led", "haptic"])

    def test_both_mode_drives_all_three(self):
        visual, haptic, mapper = FakeCue("visual"), FakeCue("haptic"), FakeMapper()
        cue = build_student_cue("both", led_mapper=mapper, visual_factory=lambda: visual,
                                haptic_factory=lambda: haptic)
        self.assertEqual(cue.enabled_channels, ["led", "visual", "haptic"])

        dispatch = cue.show_target(60, "R1")
        self.assertEqual(mapper.lit, [60])
        self.assertEqual(visual.targets, [(60, "R1")])
        self.assertEqual(haptic.targets, [(60, "R1")])
        for stamp in (
            dispatch.led_command_complete_monotonic_ns,
            dispatch.visual_painted_monotonic_ns,
            dispatch.haptic_command_complete_monotonic_ns,
        ):
            self.assertIsNotNone(stamp)

    def test_visual_is_rendered_before_slow_hardware_channels(self):
        order = []

        class OrderedMapper(FakeMapper):
            def light_key(self, note):
                order.append("led")
                return super().light_key(note)

        class OrderedCue(FakeCue):
            def show_target(self, note, finger):
                order.append(self.name)
                super().show_target(note, finger)

        cue = CompositeCueOutput(
            led=LedKeyCue(OrderedMapper()),
            visual=OrderedCue("visual"),
            haptic=OrderedCue("haptic"),
            guidance_mode="both",
        )
        cue.show_target(60, "R1")
        self.assertEqual(order, ["visual", "led", "haptic"])

    def test_cue_ready_is_the_last_channel_to_finish(self):
        clock = _StepClock()
        cue = CompositeCueOutput(
            led=LedKeyCue(FakeMapper()),
            visual=FakeCue("visual"),
            haptic=FakeCue("haptic"),
            guidance_mode="both",
            clock=clock,
            wall_clock=lambda: 1_700_000_000_000_000_000,
        )
        dispatch = cue.show_target(60, "R1")
        self.assertEqual(
            dispatch.cue_ready_monotonic_ns,
            max(
                dispatch.led_command_complete_monotonic_ns,
                dispatch.visual_painted_monotonic_ns,
                dispatch.haptic_command_complete_monotonic_ns,
            ),
        )

    def test_single_finger_channel_still_includes_the_led(self):
        """The key LED is in every guidance mode, so cue_ready must
        account for it even when only one finger channel is on."""
        haptic = FakeCue("haptic")
        cue = build_student_cue("haptic", led_mapper=FakeMapper(), haptic_factory=lambda: haptic)
        dispatch = cue.show_target(60, "R1")
        self.assertIsNotNone(dispatch.led_command_complete_monotonic_ns)
        self.assertIsNotNone(dispatch.haptic_command_complete_monotonic_ns)
        self.assertIsNone(dispatch.visual_painted_monotonic_ns)
        self.assertGreaterEqual(
            dispatch.cue_ready_monotonic_ns, dispatch.led_command_complete_monotonic_ns
        )

    def test_unmapped_note_leaves_the_led_stamp_empty(self):
        """A channel that did not fire must not contribute a timestamp -
        otherwise cue_ready would claim a cue the student never got."""
        cue = build_student_cue("visual", led_mapper=FakeMapper(known=(60,)), visual_factory=lambda: FakeCue("v"))
        dispatch = cue.show_target(99, "R1")
        self.assertIsNone(dispatch.led_command_complete_monotonic_ns)
        self.assertIsNotNone(dispatch.visual_painted_monotonic_ns)

    def test_flush_is_used_when_the_channel_offers_one(self):
        visual = FakeCue("visual", flushes=True)
        cue = CompositeCueOutput(visual=visual, guidance_mode="visual")
        cue.show_target(60, "R1")
        self.assertIn("__flush__", visual.messages)

    def test_close_releases_every_channel_even_if_one_fails(self):
        class Exploding(FakeCue):
            def close(self):
                raise RuntimeError("cue window refused to close")

        visual, haptic = Exploding("visual"), FakeCue("haptic")
        cue = CompositeCueOutput(led=LedKeyCue(FakeMapper()), visual=visual, haptic=haptic, guidance_mode="both")
        cue.show_target(60, "R1")
        cue.close()
        self.assertEqual(haptic.closed, 1)

    def test_unknown_guidance_mode_is_refused(self):
        with self.assertRaises(ValueError):
            CompositeCueOutput(guidance_mode="telepathy")
        with self.assertRaises(ValueError):
            build_student_cue("telepathy")


class HapticPortInjectionTests(unittest.TestCase):
    """HapticCueOutput gained an optional port/controller so the remote
    student can name the vibration rig explicitly (it has an LED board
    attached too). The default path must be byte-for-byte the old
    behaviour, or the existing haptic quiz breaks."""

    class FakeController:
        def __init__(self, port=None):
            self.port = port
            self.sent = []

        def connect(self):
            pass

        def stop_all(self):
            self.sent.append("X")

        def send(self, cmd):
            self.sent.append(cmd)

        def close(self):
            pass

    def test_explicit_port_is_passed_through(self):
        from app import haptic_cue

        captured = {}

        def factory(port=None):
            captured["port"] = port
            return self.FakeController(port)

        original = haptic_cue.VibratorController
        haptic_cue.VibratorController = factory
        try:
            haptic_cue.HapticCueOutput(port="/dev/tty.haptic")
        finally:
            haptic_cue.VibratorController = original
        self.assertEqual(captured["port"], "/dev/tty.haptic")

    def test_default_construction_takes_no_arguments(self):
        """The original call shape - a stand-in taking no arguments must
        still work, which is what the existing quiz test relies on."""
        from app import haptic_cue

        fake = self.FakeController()
        original = haptic_cue.VibratorController
        haptic_cue.VibratorController = lambda: fake
        try:
            haptic_cue.HapticCueOutput()
        finally:
            haptic_cue.VibratorController = original
        self.assertIn("X", fake.sent)

    def test_injected_controller_is_used_as_is(self):
        from app import haptic_cue

        fake = self.FakeController()
        cue = haptic_cue.HapticCueOutput(controller=fake)
        cue.show_target(60, "R2")
        motor = haptic_cue.FINGER_TO_MOTOR["R2"]
        self.assertIn(f"S {1 << motor} {cue.amplitude}", fake.sent)


# ---------------------------------------------------------------------------
# Session timing
# ---------------------------------------------------------------------------


class FakeDispatch:
    def __init__(self, led=None, visual=None, haptic=None, ready=None, wall=0):
        self.led_command_complete_monotonic_ns = led
        self.visual_painted_monotonic_ns = visual
        self.haptic_command_complete_monotonic_ns = haptic
        self.cue_ready_monotonic_ns = ready if ready is not None else max(x for x in (led, visual, haptic) if x)
        self.cue_ready_wall_ns = wall


class ManualClock:
    def __init__(self, value=1_000_000_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance_ms(self, ms):
        self.value += int(ms * 1_000_000)


def _guidance_envelope(note=60, finger="R1", seq=1, message_id="m1", teacher_send=None, server_receive=None):
    envelope = make_envelope(
        "guidance.live",
        room_id="room",
        seq=seq,
        payload=guidance_payload([GuidanceAction(note=note, finger=finger)], timeout_s=5.0),
        message_id=message_id,
    )
    if teacher_send is not None:
        envelope["sent_at_unix_ns"] = teacher_send
    if server_receive is not None:
        envelope["server"] = {"receive_wall_ns": server_receive}
    return envelope


class SessionTimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.wall = ManualClock(1_700_000_000_000_000_000)
        self.received = []
        self.presented = []
        self.responses = []
        self.session = StudentSession(
            timeout_s=5.0,
            present=self._present,
            send_received=lambda event, accepted: self.received.append((event, accepted)),
            send_presented=self.presented.append,
            send_response=self.responses.append,
            clock=self.clock,
            wall_clock=self.wall,
        )
        self.session.start()

    def _present(self, event):
        # Pretend dispatch took 12 ms and that the haptic channel was the
        # last to finish.
        start = self.clock()
        return FakeDispatch(led=start + 4_000_000, visual=start + 9_000_000, haptic=start + 12_000_000,
                            wall=self.wall())

    def test_reaction_time_is_measured_from_local_cue_ready(self):
        """The rule the whole module is built around: reaction time never
        starts at a timestamp taken on the teacher's machine."""
        teacher_send = 1_600_000_000_000_000_000  # a wildly different clock
        self.session.accept(_guidance_envelope(teacher_send=teacher_send, server_receive=teacher_send + 5_000_000))
        self.session.tick()
        event = self.session.current

        self.clock.advance_ms(400)
        self.session.on_note(60)

        cue_ready = event.timings.cue_ready_monotonic_ns
        response = event.timings.student_response_monotonic_ns
        self.assertEqual(event.reaction_time_ns, response - cue_ready)
        # And emphatically not any of these.
        self.assertNotEqual(event.reaction_time_ns, response - event.timings.teacher_send_wall_ns)
        self.assertNotEqual(event.reaction_time_ns, response - event.timings.student_receive_monotonic_ns)
        self.assertLess(event.reaction_time_ns, 400 * 1_000_000)  # dispatch time is excluded

    def test_cue_ready_is_the_max_of_the_enabled_channels(self):
        self.session.accept(_guidance_envelope())
        self.session.tick()
        timings = self.session.current.timings
        self.assertEqual(
            timings.cue_ready_monotonic_ns,
            max(
                timings.led_command_complete_monotonic_ns,
                timings.visual_painted_monotonic_ns,
                timings.haptic_command_complete_monotonic_ns,
            ),
        )

    def test_queue_wait_is_recorded_separately(self):
        """A cue that waited behind another must not have that wait
        counted as network delay or as the student's reaction."""
        self.session.accept(_guidance_envelope(message_id="a", seq=1))
        self.session.accept(_guidance_envelope(message_id="b", seq=2, note=62))
        self.session.tick()  # presents a; b waits

        self.clock.advance_ms(250)
        self.session.on_note(60)  # finishes a
        self.session.tick()  # presents b

        second = self.session.events[1]
        self.assertIsNotNone(second.queue_wait_ns)
        self.assertGreaterEqual(second.queue_wait_ns, 250 * 1_000_000)
        # The wait is not folded into the reaction time.
        self.clock.advance_ms(100)
        self.session.on_note(62)
        self.assertLess(second.reaction_time_ns, 150 * 1_000_000)

    def test_received_is_sent_before_the_cue_is_presented(self):
        self.session.accept(_guidance_envelope())
        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.presented), 0)
        self.session.tick()
        self.assertEqual(len(self.presented), 1)

    def test_a_full_queue_refuses_rather_than_overwrites(self):
        session = StudentSession(queue_size=2, present=self._present,
                                 send_received=lambda e, a: self.received.append((e, a)),
                                 clock=self.clock, wall_clock=self.wall)
        session.start()
        for i in range(4):
            session.accept(_guidance_envelope(message_id=f"m{i}", seq=i))

        refused = [event for event, accepted in self.received if not accepted]
        self.assertTrue(refused)
        self.assertIn("queue is full", refused[0].refused_reason)
        # Nothing was silently dropped: every refusal was acknowledged.
        self.assertEqual(len(self.received), 4)
        # And the accepted ones are still all there, in order.
        self.assertEqual([e.actions[0].note for e in session.events], [60, 60])

    def test_timeout_records_a_miss(self):
        self.session.accept(_guidance_envelope())
        self.session.tick()
        self.clock.advance_ms(6000)
        self.session.tick()

        event = self.session.events[0]
        self.assertTrue(event.timed_out)
        self.assertFalse(event.note_correct)
        self.assertIsNone(event.reaction_time_ns)

    def test_wrong_key_is_scored_wrong(self):
        self.session.accept(_guidance_envelope(note=60))
        self.session.tick()
        self.clock.advance_ms(300)
        self.session.on_note(61)
        self.assertFalse(self.session.events[0].note_correct)

    def test_finger_verdict_uses_the_shared_threshold_rule(self):
        """Correctness must come from app.finger_matching, not a second
        implementation living in the remote module."""
        session = StudentSession(
            present=self._present,
            resolve_finger=lambda note: FingerMatch(
                note=note, key_id=1, finger="R2", point=(10, 10), distance_px=3.0, inside=True,
                probability=0.55, probabilities={"R2": 0.55, "R1": 0.45},
            ),
            clock=self.clock,
            wall_clock=self.wall,
        )
        session.start()
        session.accept(_guidance_envelope(finger="R1"))
        session.tick()
        session.on_note(60)

        event = session.events[0]
        self.assertEqual(event.actual_finger, "R2")
        self.assertAlmostEqual(event.target_finger_probability, 0.45)
        # 0.45 clears the shared 0.40 threshold, so the near-tied
        # neighbour does not count against the student.
        self.assertTrue(event.finger_correct)

    def test_results_are_quiz_results_and_summarize_works(self):
        for i, note in enumerate((60, 62)):
            self.session.accept(_guidance_envelope(note=note, message_id=f"m{i}", seq=i))
            self.session.tick()
            self.clock.advance_ms(300)
            self.session.on_note(note)

        results = self.session.results()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.note_correct for r in results))
        summary = self.session.summary()
        self.assertEqual(summary["note_accuracy"], 1.0)
        self.assertEqual(summary["hits"], 2)
        # timing_error_s comes from the monotonic reaction time, so the
        # 12 ms the fake cue spent dispatching is excluded: 300 - 12 ms.
        self.assertAlmostEqual(results[0].timing_error_s, 0.288, places=3)

    def test_presses_between_events_are_not_attributed(self):
        self.session.accept(_guidance_envelope())
        self.session.tick()
        self.clock.advance_ms(200)
        self.session.on_note(60)  # finishes the event
        self.assertIsNone(self.session.on_note(60))  # a stray press afterwards

    def test_offline_pass_marks_the_event_final(self):
        self.session.accept(_guidance_envelope(finger="R1"))
        self.session.tick()
        self.clock.advance_ms(200)
        self.session.on_note(60)

        updated = self.session.apply_offline_match(
            0,
            FingerMatch(note=60, key_id=1, finger="R1", point=(1, 1), distance_px=0.0, inside=True,
                        probability=0.9, probabilities={"R1": 0.9, "R2": 0.1}),
        )
        self.assertEqual(updated.stage, "final")
        self.assertTrue(updated.finger_correct)


class DispatchTimingsTests(unittest.TestCase):
    def test_reaction_time_needs_both_local_stamps(self):
        timings = DispatchTimings(teacher_send_wall_ns=1, server_receive_wall_ns=2, student_receive_wall_ns=3)
        self.assertIsNone(timings.reaction_time_ns)

    def test_as_dict_labels_the_timing_kind(self):
        self.assertEqual(DispatchTimings().as_dict()["timing_kind"], "software_dispatch_render")

    def test_compute_cue_ready_takes_the_maximum(self):
        timings = DispatchTimings(
            led_command_complete_monotonic_ns=100,
            visual_painted_monotonic_ns=300,
            haptic_command_complete_monotonic_ns=200,
        )
        self.assertEqual(timings.compute_cue_ready(monotonic_now=400, wall_now=999), 300)


class RecordingSchedulerTests(unittest.TestCase):
    def _events(self):
        return [
            {"event_order": 0, "rel_time_s": 0.0, "note": 60, "finger": "R1", "duration_s": 0.4},
            {"event_order": 1, "rel_time_s": 1.0, "note": 62, "finger": "R2", "duration_s": 0.4},
        ]

    def test_paced_mode_waits_for_the_student(self):
        clock = ManualClock()
        scheduler = LocalRecordingScheduler(self._events(), "paced", clock(), clock=clock)
        self.assertIsNotNone(scheduler.due(session_idle=True))
        self.assertIsNone(scheduler.due(session_idle=False))
        self.assertIsNotNone(scheduler.due(session_idle=True))
        self.assertTrue(scheduler.finished)

    def test_original_timing_follows_the_recorded_offsets(self):
        clock = ManualClock()
        scheduler = LocalRecordingScheduler(self._events(), "original_timing", clock(), clock=clock)
        self.assertIsNotNone(scheduler.due(session_idle=True))
        self.assertIsNone(scheduler.due(session_idle=True))  # second event is not due yet
        clock.advance_ms(1000)
        self.assertIsNotNone(scheduler.due(session_idle=True))

    def test_pause_shifts_the_schedule_instead_of_bursting(self):
        clock = ManualClock()
        scheduler = LocalRecordingScheduler(self._events(), "original_timing", clock(), clock=clock)
        scheduler.due(session_idle=True)
        scheduler.pause()
        clock.advance_ms(5000)
        self.assertIsNone(scheduler.due(session_idle=True))
        scheduler.resume()
        # The 5s spent paused does not make the 1s event overdue.
        self.assertIsNone(scheduler.due(session_idle=True))
        clock.advance_ms(1000)
        self.assertIsNotNone(scheduler.due(session_idle=True))


# ---------------------------------------------------------------------------
# Latency statistics
# ---------------------------------------------------------------------------


class LatencyStatsTests(unittest.TestCase):
    def test_percentiles_are_actual_observations(self):
        values = list(range(1, 101))
        self.assertEqual(percentile(values, 0.95), 95)
        self.assertEqual(percentile(values, 0.99), 99)
        self.assertEqual(percentile(values, 1.0), 100)
        self.assertIsNone(percentile([], 0.95))

    def test_summary_fields_and_units(self):
        samples = [n * 1_000_000 for n in (10, 12, 14, 16, 100)]  # ns
        stats = summarize_latency(samples, lost=0, label="rtt")
        data = stats.as_dict()
        self.assertEqual(data["n"], 5)
        self.assertAlmostEqual(data["min_ms"], 10.0)
        self.assertAlmostEqual(data["max_ms"], 100.0)
        self.assertAlmostEqual(data["median_ms"], 14.0)
        self.assertAlmostEqual(data["p95_ms"], 100.0)

    def test_loss_rate_is_over_attempts(self):
        stats = summarize_latency([1, 2, 3], lost=1)
        self.assertEqual(stats.n, 3)
        self.assertEqual(stats.lost, 1)
        self.assertAlmostEqual(stats.loss_rate, 0.25)

    def test_empty_sample_set_is_not_a_crash(self):
        stats = summarize_latency([], lost=5)
        self.assertEqual(stats.n, 0)
        self.assertEqual(stats.loss_rate, 1.0)
        self.assertIsNone(stats.mean_ns)

    def test_sd_of_a_single_sample_is_zero_not_an_error(self):
        self.assertEqual(summarize_latency([5]).sd_ns, 0.0)


class SelfContainedLatencyBenchmarkTests(unittest.TestCase):
    def test_built_in_student_answers_only_valid_latency_probes(self):
        from remote_guidance.protocol import TYPE_LATENCY_RECEIVED
        from remote_guidance.tools.latency_benchmark import SimulatedStudentResponder

        class FakeClient:
            def __init__(self):
                self.sent = []

            def send(self, type_, payload):
                self.sent.append((type_, payload))

        client = FakeClient()
        responder = SimulatedStudentResponder(client)
        responder.on_message({"type": "presence", "payload": {}})
        responder.on_message({"type": "latency.probe", "message_id": "m1", "payload": {}})
        self.assertEqual(client.sent, [])

        responder.on_message(
            {"type": "latency.probe", "message_id": "m2", "payload": {"probe_id": "probe-2"}}
        )
        self.assertEqual(len(client.sent), 1)
        message_type, payload = client.sent[0]
        self.assertEqual(message_type, TYPE_LATENCY_RECEIVED)
        self.assertEqual(payload["probe_id"], "probe-2")
        self.assertEqual(payload["probe_message_id"], "m2")
        self.assertEqual(payload["responder"], "built_in_simulated_student")
        self.assertGreater(payload["student_receive_wall_ns"], 0)
        self.assertGreater(payload["student_receive_monotonic_ns"], 0)

    def test_cli_defaults_to_two_internal_roles_and_no_existing_room(self):
        from remote_guidance.tools.latency_benchmark import build_parser

        args = build_parser().parse_args([])
        self.assertFalse(args.external_student)
        self.assertEqual(args.room_id, "")
        self.assertTrue(args.teacher_username)
        self.assertEqual(args.student_username, "demostudent")

    def test_pacing_modes_share_one_mean_and_stay_in_range(self):
        """Switching pacing must change the spacing pattern and nothing
        else - not the probe rate, not the expected run length."""
        from remote_guidance.tools.latency_benchmark import (
            POISSON_MAX_INTERVAL_FACTOR,
            BenchmarkConfig,
            LatencyBenchmark,
        )

        fixed = LatencyBenchmark(None, BenchmarkConfig(interval_s=0.5, interval_mode="fixed"))
        self.assertEqual({fixed.next_interval() for _ in range(20)}, {0.5})

        uniform = LatencyBenchmark(
            None, BenchmarkConfig(interval_s=0.5, interval_mode="uniform", interval_jitter=0.4, interval_seed=1)
        )
        draws = [uniform.next_interval() for _ in range(400)]
        self.assertTrue(all(0.3 <= d <= 0.7 for d in draws))
        self.assertGreater(len(set(draws)), 1)
        self.assertAlmostEqual(sum(draws) / len(draws), 0.5, delta=0.03)

        poisson = LatencyBenchmark(
            None, BenchmarkConfig(interval_s=0.5, interval_mode="poisson", interval_seed=1)
        )
        draws = [poisson.next_interval() for _ in range(4000)]
        self.assertTrue(all(0.0 < d <= 0.5 * POISSON_MAX_INTERVAL_FACTOR for d in draws))
        # Truncating the tail costs a little of the mean, but not much.
        self.assertAlmostEqual(sum(draws) / len(draws), 0.5, delta=0.05)

    def test_pacing_is_reproducible_from_the_recorded_seed(self):
        """A randomised run is only defensible if it can be repeated, so
        the seed is drawn once and written into summary.json."""
        from remote_guidance.tools.latency_benchmark import BenchmarkConfig, LatencyBenchmark

        drawn = LatencyBenchmark(None, BenchmarkConfig(interval_mode="poisson"))
        self.assertIsNotNone(drawn.config.interval_seed)
        self.assertIsNotNone(drawn.summarize()["pacing"]["seed"])

        repeat = LatencyBenchmark(
            None, BenchmarkConfig(interval_mode="poisson", interval_seed=drawn.config.interval_seed)
        )
        self.assertEqual(
            [drawn.next_interval() for _ in range(10)],
            [repeat.next_interval() for _ in range(10)],
        )

    def test_a_nonsense_pacing_is_refused_rather_than_silently_fixed(self):
        from remote_guidance.tools.latency_benchmark import BenchmarkConfig, LatencyBenchmark

        with self.assertRaises(ValueError):
            LatencyBenchmark(None, BenchmarkConfig(interval_mode="sometimes"))
        with self.assertRaises(ValueError):
            LatencyBenchmark(None, BenchmarkConfig(interval_mode="uniform", interval_jitter=1.0))

    def test_pacing_description_states_the_actual_range(self):
        """The operator is shown a range, not a mode name - the window
        renders its Pacing row with this same function."""
        from remote_guidance.tools.latency_benchmark import BenchmarkConfig, describe_pacing

        self.assertEqual(describe_pacing(BenchmarkConfig(interval_s=0.5)), "500 ms intervals (fixed)")
        self.assertIn(
            "250-750 ms",
            describe_pacing(BenchmarkConfig(interval_s=0.5, interval_mode="uniform", interval_jitter=0.5)),
        )
        poisson = describe_pacing(BenchmarkConfig(interval_s=0.5, interval_mode="poisson", interval_seed=7))
        self.assertIn("mean 500 ms", poisson)
        self.assertIn("capped at 2500 ms", poisson)
        self.assertIn("seed 7", poisson)

    def test_realised_spacing_is_reported_next_to_the_requested_one(self):
        """Sleep-based pacing pays each send's cost on top of its wait,
        so the run records what the gaps really were."""
        from remote_guidance.tools.latency_benchmark import BenchmarkConfig, LatencyBenchmark, ProbeSample

        benchmark = LatencyBenchmark(None, BenchmarkConfig(interval_s=0.5, interval_mode="uniform"))
        # Real epoch stamps: a zero sent_wall_ns means "never sent", and
        # pacing_summary is right to leave those out of the gaps.
        base_ns = 1_700_000_000_000_000_000
        for seq, sent_ms in enumerate((0, 400, 1000)):
            sample = ProbeSample(seq=seq, probe_id=f"p{seq}", sent_wall_ns=base_ns + sent_ms * 1_000_000)
            benchmark.samples[sample.probe_id] = sample
            benchmark.order.append(sample.probe_id)

        pacing = benchmark.pacing_summary()
        self.assertEqual(pacing["mode"], "uniform")
        self.assertEqual(pacing["gaps"], 2)
        self.assertAlmostEqual(pacing["nominal_mean_ms"], 500.0)
        self.assertAlmostEqual(pacing["realised_min_ms"], 400.0)
        self.assertAlmostEqual(pacing["realised_max_ms"], 600.0)
        self.assertAlmostEqual(pacing["realised_mean_ms"], 500.0)

    def test_cli_rejects_a_jitter_that_is_not_a_fraction(self):
        import contextlib
        import io

        from remote_guidance.tools.latency_benchmark import main as cli_main

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli_main(["--server", "http://127.0.0.1:1", "--interval-jitter", "1.4"]), 2)
        self.assertIn("--interval-jitter", stderr.getvalue())

    def test_external_mode_creates_its_own_room_unless_one_is_given(self):
        """The operator never has to make a room by hand: --room-id is the
        exception, not the requirement it used to be."""
        from remote_guidance.tools.latency_benchmark import build_parser

        args = build_parser().parse_args(["--external-student"])
        self.assertTrue(args.external_student)
        self.assertEqual(args.room_id, "")
        self.assertGreater(args.wait_for_student, 0)

        args = build_parser().parse_args(["--external-student", "--room-id", "room-7"])
        self.assertEqual(args.room_id, "room-7")

    def test_a_room_id_without_external_student_is_refused(self):
        """Built-in mode makes its own room, so a room id passed with it
        can only mean the caller expected something else to happen."""
        import contextlib
        import io

        from remote_guidance.tools.latency_benchmark import main as cli_main

        # The refusal happens before any client is built; the dead server
        # and the supplied passwords keep a regression here from reaching
        # the network or blocking on a getpass prompt.
        argv = [
            "--server", "http://127.0.0.1:1", "--room-id", "room-7",
            "--password", "unused", "--student-password", "unused",
        ]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli_main(argv), 2)
        self.assertIn("--room-id", stderr.getvalue())

    def test_created_room_is_named_and_reports_its_join_code(self):
        from remote_guidance.tools.latency_benchmark import create_benchmark_room

        class FakeApi:
            def __init__(self):
                self.names = []

            def create_room(self, name):
                self.names.append(name)
                return {"room_id": "room-1", "join_code": "ABC123", "name": name}

        api = FakeApi()
        room = create_benchmark_room(api)
        self.assertEqual(room, {"room_id": "room-1", "join_code": "ABC123"})
        self.assertIn("latency benchmark", api.names[0].lower())

    def test_membership_readback_names_both_roles(self):
        """A run whose two endpoints sat in different rooms would lose
        every probe, so the confirmed membership is printed, not assumed."""
        from remote_guidance.tools.latency_benchmark import format_room_members

        text = format_room_members(
            [
                {"role": "teacher", "username": "demoteacher", "online": True},
                {"role": "student", "username": "demostudent", "online": False},
            ]
        )
        self.assertEqual(text, "teacher demoteacher, student demostudent (offline)")
        self.assertEqual(format_room_members([]), "none")

    def test_a_failed_membership_readback_does_not_abandon_the_run(self):
        """Both joins already succeeded at this point; losing the
        confirmation line is not worth losing the measurement over."""
        import contextlib
        import io

        from remote_guidance.network_client import RemoteApiError
        from remote_guidance.tools.latency_benchmark import report_room_membership

        class FailingApi:
            def room_members(self, room_id):
                raise RemoteApiError("403 forbidden")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report_room_membership(FailingApi(), "room-1")
        self.assertIn("403 forbidden", out.getvalue())

    def test_presence_wait_ignores_the_built_in_student(self):
        """In built-in mode the student seat is ours, and its own arrival
        must not be mistaken for a real Student Client joining."""
        from remote_guidance.tools.latency_benchmark import StudentPresenceWatcher

        watcher = StudentPresenceWatcher(ignore_username="demostudent")
        watcher.on_message(
            {
                "type": "presence",
                "payload": {
                    "members": [
                        {"role": "teacher", "username": "demoteacher"},
                        {"role": "student", "username": "demostudent"},
                    ]
                },
            }
        )
        self.assertFalse(watcher.joined)
        self.assertFalse(watcher.wait(0.0))

        watcher.on_message({"type": "presence", "payload": {"members": [{"role": "student", "username": "lab2"}]}})
        self.assertTrue(watcher.joined)
        self.assertEqual(watcher.student_username, "lab2")

    def test_presence_wait_ignores_non_presence_traffic(self):
        from remote_guidance.tools.latency_benchmark import StudentPresenceWatcher

        watcher = StudentPresenceWatcher()
        watcher.on_message({"type": "latency.probe", "payload": {"members": [{"role": "student", "username": "x"}]}})
        watcher.on_message({"type": "presence", "payload": {}})
        watcher.on_message({"type": "presence", "payload": {"members": [{"role": "teacher", "username": "t"}]}})
        self.assertFalse(watcher.joined)

    def test_expected_socket_close_is_not_reported_as_a_connection_failure(self):
        from remote_guidance.network_client import ClientCallbacks, RemoteWebSocketClient

        errors = []
        client = RemoteWebSocketClient(
            "ws://example.test/ws", "token", "room",
            callbacks=ClientCallbacks(on_error=errors.append), auto_reconnect=False,
        )

        def close_then_wake_receiver():
            client._stop.set()
            raise OSError(9, "Bad file descriptor")

        client._connect_once = close_then_wake_receiver
        client._run()
        self.assertEqual(errors, [])

    def test_gui_defaults_to_relay_plus_benchmark_only(self):
        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import LatencyBenchmarkWindow

        self.app = QApplication.instance() or QApplication([])
        remote = rgc.RemoteGuidanceConfig()
        remote.network.username = "demostudent"  # last normal client on this machine
        window = LatencyBenchmarkWindow(remote=remote)
        self.addCleanup(window.deleteLater)

        args = window.build_arguments()
        self.assertEqual(window.username_edit.text(), "demoteacher")
        self.assertNotIn("--external-student", args)
        self.assertNotIn("--room-id", args)
        self.assertIn("--student-username", args)
        # The room is this window's job, not the operator's: no box to
        # fill in, and the row explains what Start will create.
        self.assertTrue(window.room_edit.isHidden())
        self.assertTrue(window.room_edit_label.isHidden())
        self.assertEqual(window.room_edit.text(), "")
        self.assertIn("created automatically", window.room_status.text())
        self.assertTrue(window.student_username_edit.isEnabled())
        self.assertFalse(window.trigger_check.isEnabled())
        self.assertTrue(window.synced_check.isChecked())
        self.assertFalse(window.synced_check.isEnabled())
        self.assertIn("share this computer", window.synced_check.text())

        window.external_check.setChecked(True)
        args = window.build_arguments()
        self.assertIn("--external-student", args)
        # Still no room to pick by hand - the child creates one and shows
        # its join code for the real Student Client.
        self.assertNotIn("--room-id", args)
        self.assertFalse(window.room_edit.isHidden())
        self.assertIn("created automatically", window.room_status.text())
        self.assertFalse(window.student_username_edit.isEnabled())
        self.assertTrue(window.trigger_check.isEnabled())
        self.assertFalse(window.synced_check.isChecked())
        self.assertIn("NTP", window.synced_check.text())

        # An existing room is still reachable for a Student Client that
        # has already joined one.
        window.room_edit.setText("room-7")
        self.assertIn("--room-id", window.build_arguments())

        # Going back to built-in mode must not leave that room id armed.
        window.external_check.setChecked(False)
        self.assertEqual(window.room_edit.text(), "")
        self.assertNotIn("--room-id", window.build_arguments())

    def test_window_offers_randomised_pacing_and_shows_its_range(self):
        """The operator picks a pacing mode and is shown the range it
        produces, rendered from the benchmark's own description."""
        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import LatencyBenchmarkWindow

        self.app = QApplication.instance() or QApplication([])
        window = LatencyBenchmarkWindow(remote=rgc.RemoteGuidanceConfig())
        self.addCleanup(window.deleteLater)

        # Fixed stays the default, so an existing run is reproduced by
        # pressing Start and changing nothing.
        self.assertEqual(window.pacing_combo.currentData(), "fixed")
        self.assertFalse(window.jitter_spin.isEnabled())
        self.assertIn("500 ms intervals (fixed)", window.pacing_range.text())
        args = window.build_arguments()
        self.assertIn("--interval-mode", args)
        self.assertEqual(args[args.index("--interval-mode") + 1], "fixed")
        self.assertNotIn("--interval-jitter", args)

        window.pacing_combo.setCurrentIndex(1)
        window.jitter_spin.setValue(20)
        self.assertEqual(window.pacing_combo.currentData(), "uniform")
        self.assertTrue(window.jitter_spin.isEnabled())
        self.assertIn("400-600 ms", window.pacing_range.text())
        args = window.build_arguments()
        self.assertEqual(args[args.index("--interval-mode") + 1], "uniform")
        self.assertEqual(args[args.index("--interval-jitter") + 1], "0.2")

        # A randomised run only keeps its duration on average.
        self.assertIn("on average", window.estimate_label.text())

        window.pacing_combo.setCurrentIndex(2)
        self.assertEqual(window.pacing_combo.currentData(), "poisson")
        self.assertFalse(window.jitter_spin.isEnabled())
        self.assertIn("capped at", window.pacing_range.text())
        self.assertNotIn("--interval-jitter", window.build_arguments())

        # The range must follow the interval, not just the mode.
        window.pacing_combo.setCurrentIndex(1)
        window.interval_spin.setValue(0.25)
        self.assertIn("200-300 ms", window.pacing_range.text())

    def _write_quiz(self, root, participant, trial, reaction_times, **overrides):
        """One quiz folder in the layout app.quiz.load_quiz_results reads."""
        directory = root / f"{participant}-T{trial:02d}-B\u03b2"
        directory.mkdir(parents=True, exist_ok=True)
        cue = 1784133982.0
        events = []
        for index, rt in enumerate(reaction_times):
            event = {
                "index": index,
                "target_note": 64,
                "target_note_name": "E4",
                "target_key_id": 9,
                "target_finger": "R2",
                "cue_onset_time": cue + index * 3.0,
                "timed_out": False,
                "keypress_time": cue + index * 3.0 + rt,
                "timing_error_s": rt,
                "validity": "valid",
            }
            event.update(overrides)
            events.append(event)
        (directory / "results.json").write_text(json.dumps(events), encoding="utf-8")
        return directory

    def _lognormal_rts(self, median, log_sigma, n=120):
        """A deterministic lognormal sample, laid out on the
        distribution's own quantiles.

        Not random draws: this recovers the median and sigma from a
        fraction of the trials a random sample would need, which keeps
        the fixture small - and it removes the seed as something that
        could quietly make an assertion flaky."""
        normal = statistics.NormalDist()
        return [median * math.exp(log_sigma * normal.inv_cdf((i + 0.5) / n)) for i in range(n)]

    def test_human_pacing_is_fitted_to_whichever_participants_exist(self):
        """The study grows past P14, so the model has to be measured at
        run time - a constant would keep describing 14 people forever."""
        from remote_guidance.tools.latency_benchmark import clear_pacing_cache, measure_human_pacing

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(clear_pacing_cache)
        for participant in ("P01", "P02", "P03"):
            self._write_quiz(root, participant, 1, self._lognormal_rts(0.8, 0.35))

        clear_pacing_cache()
        fit = measure_human_pacing(root)
        self.assertEqual(fit.participant_count, 3)
        self.assertEqual(fit.trials, 360)
        self.assertAlmostEqual(fit.median_s, 0.8, delta=0.03)
        self.assertAlmostEqual(fit.log_sigma, 0.35, delta=0.03)
        self.assertIn("3 participants", fit.source)

        # A participant recorded later moves the fit, with no code change.
        self._write_quiz(root, "P20", 1, self._lognormal_rts(2.0, 0.35))
        clear_pacing_cache()
        moved = measure_human_pacing(root)
        self.assertEqual(moved.participant_count, 4)
        self.assertGreater(moved.median_s, fit.median_s)

    def test_human_fit_uses_the_quiz_module_definition_of_a_usable_trial(self):
        """Not its own: a second definition of "valid trial" in this
        codebase would eventually disagree with the study's analysis."""
        from remote_guidance.tools.latency_benchmark import clear_pacing_cache, measure_human_pacing

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(clear_pacing_cache)
        self._write_quiz(root, "P01", 1, self._lognormal_rts(0.7, 0.3))
        # None of these may reach the fit: timed out, manually confirmed
        # carry-over, and the press-before-cue artefact (no logarithm).
        self._write_quiz(root, "P01", 2, [0.4] * 50, timed_out=True)
        self._write_quiz(root, "P01", 3, [0.05] * 50, validity="invalid_carryover")
        self._write_quiz(root, "P01", 4, [-0.2] * 50)
        # An unreadable quiz is skipped, not fatal.
        broken = root / "P02-T01-B\u03b2"
        broken.mkdir()
        (broken / "results.json").write_text("{not json", encoding="utf-8")

        clear_pacing_cache()
        fit = measure_human_pacing(root)
        self.assertEqual(fit.trials, 120)
        self.assertEqual(fit.participant_count, 1)
        self.assertAlmostEqual(fit.median_s, 0.7, delta=0.05)

    def test_human_fit_falls_back_rather_than_fitting_a_handful_of_trials(self):
        from remote_guidance.tools.latency_benchmark import (
            FALLBACK_HUMAN_LOG_SIGMA,
            FALLBACK_HUMAN_MEDIAN_S,
            MIN_HUMAN_TRIALS,
            clear_pacing_cache,
            measure_human_pacing,
        )

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(clear_pacing_cache)

        clear_pacing_cache()
        empty = measure_human_pacing(root / "no-quiz-data-here")
        self.assertEqual(empty.median_s, FALLBACK_HUMAN_MEDIAN_S)
        self.assertEqual(empty.log_sigma, FALLBACK_HUMAN_LOG_SIGMA)
        self.assertIn("fallback", empty.source)

        self._write_quiz(root, "P01", 1, [0.7] * (MIN_HUMAN_TRIALS - 1))
        clear_pacing_cache()
        self.assertIn("fallback", measure_human_pacing(root).source)

    def test_human_pacing_draws_match_the_fit_it_was_given(self):
        from remote_guidance.tools.latency_benchmark import (
            BenchmarkConfig,
            LatencyBenchmark,
            expected_interval_s,
            human_pacing_for,
        )

        config = BenchmarkConfig(interval_s=0.672, interval_mode="human", human_log_sigma=0.441,
                                 interval_seed=3)
        benchmark = LatencyBenchmark(None, config)
        draws = sorted(benchmark.next_interval() for _ in range(20000))
        percentile_at = lambda q: draws[int(q * len(draws))]  # noqa: E731

        self.assertTrue(all(d > 0 for d in draws))
        self.assertAlmostEqual(percentile_at(0.05), 0.672 * math.exp(-1.645 * 0.441), delta=0.02)
        self.assertAlmostEqual(percentile_at(0.50), 0.672, delta=0.02)
        self.assertAlmostEqual(percentile_at(0.95), 0.672 * math.exp(1.645 * 0.441), delta=0.05)
        # Right-skewed, like reaction times themselves - not symmetric.
        self.assertGreater(percentile_at(0.95) - percentile_at(0.5), percentile_at(0.5) - percentile_at(0.05))
        # A lognormal's mean is above its median, and the run-length
        # estimate must use the mean or it under-states by minutes.
        self.assertGreater(expected_interval_s(config), config.interval_s)
        self.assertAlmostEqual(expected_interval_s(config), sum(draws) / len(draws), delta=0.02)
        self.assertEqual(human_pacing_for(config).source, "sigma given on the command line")

    def test_the_fit_recomputes_when_a_participant_is_added_and_not_otherwise(self):
        """Cached against a fingerprint of the quiz folder, so it is not
        something anyone has to remember to invalidate."""
        from remote_guidance.tools.latency_benchmark import (
            cached_human_pacing,
            clear_pacing_cache,
            measure_human_pacing,
        )

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(clear_pacing_cache)
        clear_pacing_cache()
        self._write_quiz(root, "P01", 1, self._lognormal_rts(0.8, 0.35))
        self._write_quiz(root, "P02", 1, self._lognormal_rts(0.8, 0.35))

        self.assertIsNone(cached_human_pacing(root), "nothing is fitted until it is asked for")
        first = measure_human_pacing(root)
        self.assertIsNotNone(cached_human_pacing(root))
        self.assertIs(measure_human_pacing(root), first, "a second read reuses the fit")

        self._write_quiz(root, "P03", 1, self._lognormal_rts(0.8, 0.35))
        self.assertIsNone(cached_human_pacing(root), "new data invalidates it by itself")
        self.assertEqual(measure_human_pacing(root).participant_count, 3)

    def test_the_fit_reports_progress_while_it_reads(self):
        from remote_guidance.tools.latency_benchmark import clear_pacing_cache, measure_human_pacing

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(clear_pacing_cache)
        for participant in ("P01", "P02"):
            self._write_quiz(root, participant, 1, self._lognormal_rts(0.8, 0.35))

        steps = []
        clear_pacing_cache()
        measure_human_pacing(root, progress=lambda done, total, message: steps.append((done, total, message)))
        self.assertGreaterEqual(len(steps), 3)
        self.assertTrue(all(0 < done <= total for done, total, _ in steps))
        self.assertEqual([done for done, _, _ in steps], sorted(done for done, _, _ in steps))
        self.assertIn("P01", " ".join(message for _, _, message in steps))

    def test_a_cancelled_analysis_stops_where_it_was_asked_to(self):
        """The GUI cancels cooperatively - the callback raises - and a
        half-read corpus must not be cached as if it were a fit."""
        from remote_guidance.tools.latency_benchmark import (
            cached_human_pacing,
            clear_pacing_cache,
            measure_human_pacing,
        )

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(clear_pacing_cache)
        for participant in ("P01", "P02", "P03"):
            self._write_quiz(root, participant, 1, self._lognormal_rts(0.8, 0.35))

        class Stop(Exception):
            pass

        def give_up(done, total, message):
            if done > 1:
                raise Stop

        clear_pacing_cache()
        with self.assertRaises(Stop):
            measure_human_pacing(root, progress=give_up)
        self.assertIsNone(cached_human_pacing(root))

    def test_human_pacing_samples_per_participant_not_from_one_pooled_blob(self):
        """Pooling every trial mixes within-person spread with
        between-person differences; drawing a participant first
        reproduces both."""
        from remote_guidance.tools.latency_benchmark import (
            BenchmarkConfig,
            HumanPacing,
            LatencyBenchmark,
            ParticipantPacing,
        )

        # Two very different people, each individually tight.
        fit = HumanPacing(
            median_s=0.6,
            log_sigma=0.5,
            participants=[
                ParticipantPacing("P01", median_s=0.3, log_sigma=0.05, trials=500),
                ParticipantPacing("P02", median_s=1.2, log_sigma=0.05, trials=500),
            ],
            trials=1000,
            source="test",
        )
        benchmark = LatencyBenchmark(None, BenchmarkConfig(interval_s=0.6, interval_mode="human",
                                                           interval_seed=5))
        benchmark.human_pacing = fit
        draws = [benchmark.next_interval() for _ in range(4000)]

        # Bimodal around the two participants' medians, which a single
        # pooled lognormal could not produce.
        fast = [d for d in draws if d < 0.6]
        slow = [d for d in draws if d >= 0.6]
        self.assertGreater(len(fast), 1000)
        self.assertGreater(len(slow), 1000)
        self.assertAlmostEqual(statistics.median(fast), 0.3, delta=0.05)
        self.assertAlmostEqual(statistics.median(slow), 1.2, delta=0.1)

    def test_the_analysis_line_never_triggers_a_corpus_scan(self):
        """A page of past results must not stall on fitting anything, so
        the scale reference comes from the run itself or from a fit
        already in hand."""
        from remote_guidance.tools.latency_benchmark import analyse_summary, clear_pacing_cache

        self.addCleanup(clear_pacing_cache)
        clear_pacing_cache()
        summary = {
            "counts": {"attempted": 100, "warmup": 0, "lost": 0, "loss_rate": 0.0},
            "metrics": {"transport_rtt": {"median_ms": 40.0, "p95_ms": 60.0, "p99_ms": 70.0, "max_ms": 80.0}},
        }
        # No pacing block and nothing cached: the line is simply absent.
        self.assertNotIn("for scale", " | ".join(analyse_summary(summary)))

        # A human-paced run carries the population it used, and that is
        # the right reference for it even years later.
        summary["pacing"] = {"mode": "human", "nominal_mean_ms": 664.0,
                             "human": {"median_s": 0.664, "source": "measured from 14 participants"}}
        text = " | ".join(analyse_summary(summary))
        self.assertIn("664 ms median reaction time", text)
        self.assertIn("measured from 14 participants", text)

    def test_a_run_records_which_population_it_was_paced_like(self):
        """"Measured from 14 participants" and "from 20" are different
        claims, so the run carries the one it used."""
        from remote_guidance.tools.latency_benchmark import BenchmarkConfig, LatencyBenchmark

        benchmark = LatencyBenchmark(None, BenchmarkConfig(interval_mode="human"))
        human = benchmark.pacing_summary()["human"]
        self.assertIn("participant_count", human)
        self.assertIn("participants", human)
        self.assertIn("trials", human)
        self.assertIn("source", human)
        # The evidence for the model travels with the run too.
        self.assertIn("families", human)
        self.assertIn("median_ci", human)
        self.assertEqual(human["log_sigma"], benchmark.human_pacing.log_sigma)
        # What was asked for stays None - "fit it" - so a later read of
        # this config cannot mistake the fit for an operator's choice.
        self.assertIsNone(benchmark.config.human_log_sigma)
        self.assertNotIn("human", LatencyBenchmark(None, BenchmarkConfig()).pacing_summary())

    def test_the_cli_leaves_the_interval_unset_for_the_mode_to_resolve(self):
        from remote_guidance.tools.latency_benchmark import build_parser

        # None means "use this mode's own default" - fixed keeps the
        # platform interval, human takes the fitted participant median.
        self.assertIsNone(build_parser().parse_args([]).interval)
        self.assertIsNone(build_parser().parse_args(["--interval-mode", "human"]).interval)
        self.assertAlmostEqual(build_parser().parse_args(["--interval", "0.2"]).interval, 0.2)

    def test_analysis_describes_the_run_without_grading_it(self):
        from remote_guidance.tools.latency_benchmark import analyse_summary

        summary = {
            "counts": {"attempted": 1000, "warmup": 20, "lost": 3, "loss_rate": 0.003},
            "metrics": {"transport_rtt": {"median_ms": 36.6, "p95_ms": 150.1, "p99_ms": 258.0, "max_ms": 446.7}},
            "pacing": {"mode": "human", "nominal_mean_ms": 672.0, "realised_mean_ms": 742.0, "seed": 99},
            "config": {"built_in_student": True},
        }
        text = " | ".join(analyse_summary(summary))
        self.assertIn("1000 measured probes, 3 lost (0.30%)", text)
        self.assertIn("median 36.6 ms", text)
        self.assertIn("4.1x the median", text)
        self.assertIn("--interval-seed 99", text)
        self.assertIn("not Student UI or hardware", text)
        # Reports the shape; does not pronounce on whether it is good.
        for verdict in ("acceptable", "too slow", "fine", "bad", "good enough"):
            self.assertNotIn(verdict, text.lower())

    def test_analysis_survives_a_run_that_recorded_nothing(self):
        from remote_guidance.tools.latency_benchmark import analyse_summary

        self.assertEqual(analyse_summary({}), [])
        self.assertEqual(analyse_summary({"counts": {"attempted": 0}, "metrics": {}}), [])

    def test_results_page_lists_past_runs_newest_first(self):
        import json as json_module

        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import BenchmarkResultsPage

        self.app = QApplication.instance() or QApplication([])
        results = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, results, True)
        for run_id in ("20260101-000000", "20260817-225801"):
            (results / run_id).mkdir()
            (results / run_id / "summary.json").write_text(
                json_module.dumps(
                    {
                        "counts": {"attempted": 10, "warmup": 2, "lost": 0, "loss_rate": 0.0},
                        "metrics": {"transport_rtt": {"n": 10, "median_ms": 12.0, "p95_ms": 20.0,
                                                      "p99_ms": 22.0, "max_ms": 25.0}},
                        "pacing": {"mode": "fixed", "nominal_mean_ms": 500.0},
                    }
                )
            )
        # A folder with no summary.json is an interrupted run, not a result.
        (results / "20260818-000000").mkdir()

        page = BenchmarkResultsPage(results_dir=results)
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.run_ids(), ["20260817-225801", "20260101-000000"])
        self.assertEqual(page.run_list.currentItem().text(), "20260817-225801")

        shown = page.summary_text.toPlainText()
        self.assertIn("20260817-225801", shown)
        self.assertIn("Analysis", shown)
        self.assertIn("median 12.0 ms", shown)
        self.assertIn("transport_rtt", shown)  # the run's own summary table
        # No latency.png in this run, which is not an error.
        self.assertIn("no figures", page.plot_label.text())

        # Refresh keeps the run being read rather than jumping to newest.
        page.run_list.setCurrentRow(1)
        page.reload()
        self.assertEqual(page.run_list.currentItem().text(), "20260101-000000")
        page.show_newest()
        self.assertEqual(page.run_list.currentItem().text(), "20260817-225801")

    def test_results_page_offers_every_figure_a_run_wrote(self):
        """A run writes an overview plus per-question figures now; the
        page has to let the reader reach the ones that are not first."""
        import json as json_module

        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import BenchmarkResultsPage

        self.app = QApplication.instance() or QApplication([])
        results = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, results, True)
        run = results / "20260817-225801"
        run.mkdir()
        (run / "summary.json").write_text(json_module.dumps({"counts": {"attempted": 5}, "metrics": {}}))
        # Written out of order, and with a PDF beside each PNG.
        for name in ("latency_jitter.png", "latency.png", "latency_hops.png", "latency.pdf"):
            (run / name).write_bytes(b"")

        page = BenchmarkResultsPage(results_dir=results)
        self.addCleanup(page.deleteLater)
        names = [page.figure_combo.itemText(i) for i in range(page.figure_combo.count())]
        # PNGs only - the PDF is for the thesis, not for this pane - and
        # the overview leads.
        self.assertEqual(names, ["latency.png", "latency_hops.png", "latency_jitter.png"])
        self.assertEqual(page.figure_combo.currentText(), "latency.png")

    def test_figures_only_claim_two_link_states_when_there_are_two(self):
        """The overview shades episodes of an elevated state. On a link
        that does not have one, it must not invent it."""
        import random as random_module

        from remote_guidance.tools.latency_plots import episodes, split_states

        rng = random_module.Random(4)
        one_state = [rng.gauss(35, 4) for _ in range(500)]
        self.assertIsNone(split_states(one_state), "a single-state link must not be split")

        two_states = [rng.gauss(35, 4) for _ in range(300)] + [rng.gauss(140, 8) for _ in range(200)]
        split = split_states(two_states)
        self.assertIsNotNone(split)
        low, high, boundary = split
        self.assertAlmostEqual(low, 35, delta=4)
        self.assertAlmostEqual(high, 140, delta=8)
        self.assertTrue(low < boundary < high)

        # A lone spike is not an episode; a sustained stretch is.
        flags = [False] * 10 + [True] + [False] * 10 + [True] * 6 + [False] * 5
        self.assertEqual(episodes(flags), [(21, 27)])

    def test_results_page_is_calm_about_an_empty_or_broken_results_dir(self):
        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import BenchmarkResultsPage

        self.app = QApplication.instance() or QApplication([])
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)

        page = BenchmarkResultsPage(results_dir=empty / "not-created-yet")
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.run_ids(), [])
        self.assertIn("No finished runs", page.summary_text.toPlainText())

        (empty / "20260101-000000").mkdir()
        (empty / "20260101-000000" / "summary.json").write_text("{not json")
        broken = BenchmarkResultsPage(results_dir=empty)
        self.addCleanup(broken.deleteLater)
        self.assertIn("could not read summary.json", broken.summary_text.toPlainText())

    def test_window_shows_the_results_page_after_a_successful_run(self):
        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import LatencyBenchmarkWindow

        self.app = QApplication.instance() or QApplication([])
        window = LatencyBenchmarkWindow(remote=rgc.RemoteGuidanceConfig())
        self.addCleanup(window.deleteLater)

        self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())],
                         ["Benchmark", "Past results"])
        self.assertEqual(window.tabs.currentIndex(), 0)

        window._on_finished(1, None)
        self.assertEqual(window.tabs.currentIndex(), 0, "a failed run stays on the log that explains it")
        window._on_finished(0, None)
        self.assertIs(window.tabs.currentWidget(), window.results_page)

    def test_selecting_human_pacing_analyses_behind_a_progress_dialog(self):
        """The fit reads every participant's every trial. Whatever that
        grows to, it must not happen on the GUI thread."""
        import time as time_module

        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import LatencyBenchmarkWindow
        from remote_guidance.tools.latency_benchmark import cached_human_pacing, clear_pacing_cache

        self.app = QApplication.instance() or QApplication([])
        self.addCleanup(clear_pacing_cache)
        clear_pacing_cache()

        window = LatencyBenchmarkWindow(remote=rgc.RemoteGuidanceConfig())
        self.addCleanup(window.deleteLater)
        # Opening the window fits nothing - not even the results page's
        # analysis line may trigger it.
        self.assertIsNone(cached_human_pacing())

        window.pacing_combo.setCurrentIndex(3)
        self.assertEqual(window.pacing_combo.currentData(), "human")
        self.assertIn("analysing", window.pacing_range.text())
        self.assertIsNotNone(window._pacing_dialog)
        self.assertTrue(window._pacing_worker.isRunning())

        deadline = time_module.time() + 60
        while window._pacing_worker is not None and time_module.time() < deadline:
            self.app.processEvents()
            time_module.sleep(0.01)
        self.assertIsNone(window._pacing_worker, "the analysis never finished")
        self.assertIsNone(window._pacing_dialog, "the progress dialog was left open")

        fit = cached_human_pacing()
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(window.interval_spin.value(), fit.median_s, places=3)
        self.assertIn("human reaction time", window.pacing_range.text())
        self.assertIn(str(fit.participant_count), window.pacing_range.text())

        # Already fitted, so re-selecting is instant and starts nothing.
        window.pacing_combo.setCurrentIndex(0)
        window.pacing_combo.setCurrentIndex(3)
        self.assertIsNone(window._pacing_worker)

    def test_window_reports_the_room_its_run_created(self):
        from PySide6.QtWidgets import QApplication

        from remote_guidance.tools.benchmark_window import LatencyBenchmarkWindow

        self.app = QApplication.instance() or QApplication([])
        window = LatencyBenchmarkWindow(remote=rgc.RemoteGuidanceConfig())
        self.addCleanup(window.deleteLater)

        for line in (
            "room: created 2549395a",
            "room: join code FJXP7Z",
            "room: members confirmed: teacher demoteacher, student demostudent",
        ):
            window._note_room_line(line)
        status = window.room_status.text()
        self.assertIn("2549395a", status)
        self.assertIn("FJXP7Z", status)
        self.assertIn("teacher demoteacher, student demostudent", status)

        # Starting another run must not show the previous room's id.
        window._reset_room_status()
        self.assertNotIn("2549395a", window.room_status.text())


class OneWayEstimateTests(unittest.TestCase):
    """RTT and one-way must never be quoted as the same kind of number."""

    def test_without_synced_clocks_it_is_rtt_over_two_and_says_so(self):
        rtts = [20_000_000, 30_000_000, 40_000_000]
        estimate = one_way_estimate(rtts)
        self.assertEqual(estimate.label, "one_way_symmetry_estimate")
        self.assertIn("ESTIMATE", estimate.caveat)
        self.assertAlmostEqual(estimate.mean_ns, 15_000_000)

    def test_unsynced_clocks_do_not_use_cross_machine_deltas(self):
        """Even with wall deltas available, an unconfirmed clock must
        fall back to RTT/2 - the deltas are not a latency."""
        offset = ClockOffset(offset_ns=5_000_000, uncertainty_ns=1_000_000, best_rtt_ns=2_000_000,
                             samples=10, synchronised=False)
        estimate = one_way_estimate([20_000_000], clock_offset=offset, receive_deltas=[900_000_000])
        self.assertEqual(estimate.label, "one_way_symmetry_estimate")
        self.assertAlmostEqual(estimate.mean_ns, 10_000_000)

    def test_synced_clocks_give_a_corrected_measurement_with_uncertainty(self):
        offset = ClockOffset(offset_ns=5_000_000, uncertainty_ns=1_000_000, best_rtt_ns=2_000_000,
                             samples=10, synchronised=True)
        estimate = one_way_estimate([20_000_000], clock_offset=offset, receive_deltas=[17_000_000])
        self.assertEqual(estimate.label, "one_way_clock_corrected")
        self.assertAlmostEqual(estimate.mean_ns, 12_000_000)
        self.assertIn("uncertainty", estimate.caveat)

    def test_the_two_metrics_carry_different_labels_and_caveats(self):
        rtt = summarize_latency([20_000_000], label="transport_rtt", caveat="PRIMARY")
        one_way = one_way_estimate([20_000_000])
        self.assertNotEqual(rtt.label, one_way.label)
        self.assertNotEqual(rtt.caveat, one_way.caveat)
        self.assertNotEqual(rtt.mean_ns, one_way.mean_ns)


class ClockOffsetTests(unittest.TestCase):
    def test_offset_recovered_from_symmetric_exchanges(self):
        estimator = ClockOffsetEstimator()
        true_offset = 250_000_000  # remote clock is 250 ms ahead
        for i in range(10):
            one_way = 5_000_000 + i * 1_000_000
            t1 = 1_000_000_000 * i
            t2 = t1 + one_way + true_offset
            t3 = t2 + 1_000_000
            t4 = t3 - true_offset + one_way
            estimator.add_exchange(t1, t2, t3, t4)

        estimate = estimator.estimate()
        self.assertAlmostEqual(estimate.offset_ns, true_offset, delta=2_000_000)
        self.assertGreater(estimate.uncertainty_ns, 0)
        self.assertEqual(estimate.samples, 10)
        self.assertFalse(estimate.synchronised)

    def test_no_samples_yields_no_estimate(self):
        self.assertIsNone(ClockOffsetEstimator().estimate())

    def test_the_method_is_labelled_as_not_ntp(self):
        estimator = ClockOffsetEstimator()
        estimator.add_exchange(0, 100, 200, 300)
        self.assertIn("not NTP", estimator.estimate().as_dict()["method"])


# ---------------------------------------------------------------------------
# Launcher actions
# ---------------------------------------------------------------------------


class ApiWorkerLifecycleTests(unittest.TestCase):
    """Regression: clicking Sign in / Create account / Go a second time
    used to raise

        RuntimeError: libshiboken: Internal C++ object (ApiCallWorker)
        already deleted

    because the finished worker was handed to deleteLater() while the
    panel still held the Python wrapper, and the next call asked that dead
    wrapper isRunning(). These run a real Qt event loop so the deletion
    actually happens."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _drain(self, predicate, timeout_ms=5000):
        """Spin the event loop until predicate() is true (or time out), so
        queued signals and deleteLater() are genuinely processed."""
        from PySide6.QtCore import QDeadlineTimer, QEventLoop

        deadline = QDeadlineTimer(timeout_ms)
        while not predicate() and not deadline.hasExpired():
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        return predicate()

    def _panel(self):
        from remote_guidance.gui_common import SignInPanel
        from remote_guidance.network_client import RemoteApiClient

        panel = SignInPanel(rgc.RemoteGuidanceConfig(), "teacher", RemoteApiClient("http://127.0.0.1:9"))
        self.addCleanup(panel.deleteLater)
        return panel

    def test_a_second_call_runs_after_the_first_finished(self):
        panel = self._panel()
        results = []

        for expected in (1, 2, 3):
            panel.run_call(lambda: "ok", results.append, "working...")
            self.assertTrue(
                self._drain(lambda: len(results) == expected and panel._worker is None),
                "worker reference was not released",
            )

        self.assertEqual(results, ["ok", "ok", "ok"], "a later call was swallowed by a stale worker guard")

    def test_worker_running_survives_a_deleted_wrapper(self):
        """The exact failure mode, forced directly: a wrapper whose C++
        object is gone must read as 'not running', not raise."""
        panel = self._panel()
        panel.run_call(lambda: "ok", lambda _r: None, "working...")
        worker = panel._worker
        self.assertTrue(self._drain(lambda: panel._worker is None))

        # Put the now-deleted wrapper back and make sure the guard copes.
        panel._worker = worker
        self.assertFalse(panel.busy())
        self.assertIsNone(panel._worker)

    def test_a_failing_call_also_releases_the_worker(self):
        panel = self._panel()
        errors = []
        panel.statusChanged.connect(lambda message, level: errors.append(level))

        panel.run_call(lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda _r: None, "working...")
        self.assertTrue(self._drain(lambda: panel._worker is None))
        self.assertIn("error", errors)

        # And the panel is usable again afterwards.
        results = []
        panel.run_call(lambda: "recovered", results.append, "working...")
        self.assertTrue(self._drain(lambda: results == ["recovered"]))

    def test_teacher_window_uses_the_same_guard(self):
        """The teacher window has its own copy of the call runner; the
        fix has to be in both or the bug just moves."""
        from remote_guidance.teacher.window import TeacherRemoteWindow

        for name in ("_worker_running", "_release_worker"):
            self.assertTrue(hasattr(TeacherRemoteWindow, name), f"TeacherRemoteWindow is missing {name}")

    def test_both_stage_panels_share_the_guarded_runner(self):
        from remote_guidance.gui_common import RoomPanel, SignInPanel, _ApiPanel

        for cls in (SignInPanel, RoomPanel):
            self.assertTrue(issubclass(cls, _ApiPanel), f"{cls.__name__} must reuse the guarded call runner")

    def test_student_window_guards_its_analysis_worker(self):
        from remote_guidance.student.window import StudentRemoteWindow

        for name in ("_analyze_running", "_release_analyze_worker"):
            self.assertTrue(hasattr(StudentRemoteWindow, name), f"StudentRemoteWindow is missing {name}")

    def test_every_message_type_has_a_handler_that_exists(self):
        """_on_message dispatches by message type, so a handler that was
        never written (or was renamed) is only found when that type
        actually arrives - on the WebSocket thread, mid-lesson.

        This shipped once: guidance.live called self._on_guidance(), which
        did not exist, so every live cue raised AttributeError inside the
        student's dispatcher. The teacher detected notes and sent them
        happily, the relay forwarded them, and the student sat on
        "Waiting for the teacher..." with nothing on screen to say why."""
        import ast
        import inspect
        import textwrap

        from remote_guidance.student.window import StudentRemoteWindow
        from remote_guidance.teacher.window import TeacherRemoteWindow

        for cls in (StudentRemoteWindow, TeacherRemoteWindow):
            source = textwrap.dedent(inspect.getsource(cls._on_message))
            tree = ast.parse(source)
            called = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            }
            self.assertTrue(called, f"{cls.__name__}._on_message dispatches to nothing - did it move?")
            for name in sorted(called):
                self.assertTrue(
                    hasattr(cls, name),
                    f"{cls.__name__}._on_message calls self.{name}(), which does not exist",
                )


class JoinCodeTests(unittest.TestCase):
    """The join code is the only room identifier a person ever types, so
    telling one apart from a room name has to be reliable - it is what
    decides whether the teacher's single field joins or creates."""

    def test_the_client_and_server_agree_on_the_format(self):
        """Duplicated on purpose (server/ imports nothing from here), so
        only a test can catch them drifting apart."""
        from remote_guidance.protocol import JOIN_CODE_ALPHABET, JOIN_CODE_LENGTH
        from server import database as dbm

        self.assertEqual(JOIN_CODE_LENGTH, dbm.JOIN_CODE_LENGTH)
        self.assertEqual(JOIN_CODE_ALPHABET, dbm._JOIN_CODE_ALPHABET)

    def test_a_generated_code_is_always_recognised(self):
        from remote_guidance.protocol import looks_like_join_code
        from server import database as dbm

        for _ in range(200):
            self.assertTrue(looks_like_join_code(dbm.generate_join_code()))

    def test_room_names_are_not_mistaken_for_codes(self):
        """The alphabet leaves out I, L, O, 0 and 1, which is what makes
        an ordinary six-letter word fail the test."""
        from remote_guidance.protocol import looks_like_join_code

        for name in ("Tuesday lesson", "Monday", "lesson", "P01", "", "   ", "K7M4PQZ", "K7M4P"):
            self.assertFalse(looks_like_join_code(name), f"{name!r} was read as a join code")

    def test_case_and_padding_do_not_matter(self):
        from remote_guidance.protocol import looks_like_join_code

        self.assertTrue(looks_like_join_code("k7m4pq"))
        self.assertTrue(looks_like_join_code("  K7M4PQ  "))


class RoomPanelTests(unittest.TestCase):
    """One field, no mode selector, no room id on screen.

    The old panel had a two-entry combo per role whose "by id" options
    asked a person to type a uuid, and whose teacher default was Create
    while the pre-filled value was the last room *id* - one click made a
    room named after a uuid."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, role, remote=None):
        from unittest import mock

        from remote_guidance.gui_common import RoomPanel
        from remote_guidance.network_client import RemoteApiClient

        panel = RoomPanel(remote or rgc.RemoteGuidanceConfig(), role, RemoteApiClient("http://127.0.0.1:9"))
        self.addCleanup(panel.deleteLater)
        panel.api = mock.MagicMock()
        # Run the REST call inline instead of on a worker thread.
        panel.run_call = lambda call, on_ok, busy: call()
        return panel

    def test_there_is_no_mode_selector_and_no_room_id_field(self):
        from PySide6.QtWidgets import QLabel

        for role in ("teacher", "student"):
            panel = self._panel(role)
            self.assertFalse(hasattr(panel, "mode_combo"), f"the {role} panel still asks the user to choose")
            text = " ".join(child.text() for child in panel.findChildren(QLabel) if child.text()).lower()
            self.assertNotIn("uuid", text, f"the {role} panel still mentions a uuid")
            self.assertNotIn("room id", text, f"the {role} panel still asks for a room id")

    def test_a_code_joins_for_both_roles(self):
        """POST /rooms/join returns a room to its own owner too, so the
        teacher reopens with the same call the student joins with."""
        for role in ("teacher", "student"):
            panel = self._panel(role)
            panel.value_edit.setText("K7M4PQ")
            panel.go()
            panel.api.join_room.assert_called_once_with("K7M4PQ")
            self.assertFalse(panel.api.create_room.called, f"the {role} panel created a room from a code")

    def test_anything_else_creates_a_room_for_the_teacher(self):
        panel = self._panel("teacher")
        panel.value_edit.setText("Tuesday lesson")
        panel.go()
        panel.api.create_room.assert_called_once_with("Tuesday lesson")
        self.assertFalse(panel.api.join_room.called)

    def test_a_student_is_told_when_it_is_not_a_code(self):
        """A student has nothing else the field could mean, so say so
        here rather than sending it and relaying a 404."""
        panel = self._panel("student")
        panel.value_edit.setText("Tuesday lesson")
        panel.go()
        self.assertFalse(panel.api.join_room.called)
        self.assertFalse(panel.api.create_room.called)
        self.assertIn("not a join code", panel.status_label.text())

    def test_an_empty_field_calls_nothing(self):
        for role in ("teacher", "student"):
            panel = self._panel(role)
            panel.value_edit.setText("   ")
            panel.go()
            self.assertFalse(panel.api.join_room.called)
            self.assertFalse(panel.api.create_room.called)

    def test_the_field_starts_on_the_last_join_code_for_both_roles(self):
        """Not the room id - which is what the teacher's field used to
        be pre-filled with."""
        remote = rgc.RemoteGuidanceConfig()
        remote.network.join_code = "K7M4PQ"
        remote.network.room_id = "98badf6a-42f7-442d-9c56-dc06d5759ff9"
        for role in ("teacher", "student"):
            panel = self._panel(role, remote)
            self.assertEqual(panel.value_edit.text(), "K7M4PQ")

    def test_the_code_a_student_typed_is_remembered(self):
        """The relay returns join_code only to a room's owner, so a
        student's next launch has nothing to pre-fill unless what they
        typed is kept."""
        panel = self._panel("student")
        panel.value_edit.setText("k7m4pq")
        panel.go()
        self.assertEqual(panel.entered_code, "K7M4PQ")


class RemoteSettingsDialogTests(unittest.TestCase):
    """Each client owns its settings, and sees only its own.

    This replaced one launcher window with Network / Student / Teacher
    tabs, where whoever opened it was looking mostly at settings that
    were not theirs - and had to leave the client to change anything."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, role, remote=None):
        from app.config import Config

        from remote_guidance.settings_window import RemoteSettingsDialog

        dialog = RemoteSettingsDialog(role, Config.load(), remote or rgc.RemoteGuidanceConfig())
        self.addCleanup(dialog.deleteLater)
        return dialog

    def _dialog_with_ports(self, role, labels, ambiguous=(), remote=None):
        """Build the dialog against a made-up MIDI port list - no keyboard
        has to be plugged in, and the machine running the tests may well
        have its own."""
        from unittest import mock

        with mock.patch("remote_guidance.settings_window.list_input_ports", return_value=list(labels)), mock.patch(
            "remote_guidance.settings_window.ambiguous_port_names", return_value=list(ambiguous)
        ):
            return self._dialog(role, remote)

    def _remote_with_distinct_roles(self):
        remote = rgc.RemoteGuidanceConfig()
        remote.student.camera = CameraConfig(index=0, width=1280, height=720, fps=30)
        remote.student.midi.port_name = "Student Keyboard"
        remote.student.keyboard_profile = "student-profile"
        remote.teacher.camera = CameraConfig(index=1, width=640, height=480, fps=60)
        remote.teacher.midi.port_name = "Teacher Keyboard"
        remote.teacher.keyboard_profile = "teacher-profile"
        return remote

    def test_each_dialog_shows_only_its_own_role(self):
        remote = self._remote_with_distinct_roles()
        for role, index, port, profile in (
            ("student", "0", "Student Keyboard", "student-profile"),
            ("teacher", "1", "Teacher Keyboard", "teacher-profile"),
        ):
            dialog = self._dialog(role, remote)
            self.assertEqual(dialog.camera_group.index_edit.text(), index)
            self.assertEqual(dialog.port_combo.currentText(), port)
            self.assertFalse(dialog.port_combo.isEditable())
            self.assertEqual(dialog.profile_combo.currentText(), profile)

    def test_midi_port_is_a_dropdown_not_a_text_field(self):
        dialog = self._dialog_with_ports("teacher", ["Keyboard A", "Keyboard B"])
        self.assertFalse(dialog.port_combo.isEditable())
        self.assertEqual(
            [dialog.port_combo.itemText(i) for i in range(dialog.port_combo.count())],
            ["Keyboard A", "Keyboard B"],
        )
        dialog.port_combo.setCurrentIndex(1)
        self.assertEqual(dialog.port_combo.currentText(), "Keyboard B")

    def test_empty_midi_scan_keeps_the_saved_port_as_the_only_option(self):
        from PySide6.QtCore import Qt

        remote = rgc.RemoteGuidanceConfig()
        remote.teacher.midi.port_name = "Saved Teacher Keyboard"
        dialog = self._dialog_with_ports("teacher", [], remote=remote)
        self.assertEqual(dialog.port_combo.count(), 1)
        self.assertEqual(dialog.port_combo.currentText(), "Saved Teacher Keyboard")
        self.assertIn(
            "not currently detected",
            dialog.port_combo.itemData(0, Qt.ItemDataRole.ToolTipRole),
        )

    def test_empty_midi_scan_without_a_saved_port_stays_empty(self):
        from unittest import mock

        remote = rgc.RemoteGuidanceConfig()
        remote.student.midi.port_name = None
        dialog = self._dialog_with_ports("student", [], remote=remote)
        self.assertEqual(dialog.port_combo.count(), 0)
        self.assertEqual(dialog.port_combo.currentIndex(), -1)
        self.assertEqual(dialog.port_combo.placeholderText(), "No MIDI inputs detected")
        self.assertFalse(dialog.midi_test_btn.isEnabled())
        with mock.patch("remote_guidance.settings_window.MidiListener") as listener:
            dialog._toggle_midi_test()
        listener.assert_not_called()
        self.assertIn("plug in", dialog.midi_test_output.text().lower())

    def test_settings_use_role_themes_and_a_two_column_device_workspace(self):
        dialogs = []
        for role, accent in (("teacher", "#f2a65a"), ("student", "#57c7ff")):
            dialog = self._dialog(role)
            dialogs.append(dialog)
            self.assertEqual(dialog.objectName(), f"{role}Settings")
            self.assertIn(accent, dialog.styleSheet())
            self.assertIn(role.upper(), dialog.settings_title.text())
            self.assertEqual(dialog.settings_header.objectName(), "topBar")
            self.assertGreaterEqual(dialog.devices_layout.indexOf(dialog.camera_group), 0)
            self.assertGreaterEqual(dialog.devices_layout.indexOf(dialog.keyboard_box), 0)
            self.assertEqual(dialog.midi_test_output.objectName(), "midiMonitor")
            self.assertEqual(dialog.save_btn.property("role"), "primary")
            self.assertEqual(dialog.preview_btn.property("role"), "primary")
            # A selected box must carry an actual tick, not merely swap
            # from a dark square to an accent-coloured square. Likewise,
            # spin boxes keep direct text entry but expose large, themed
            # up/down button subcontrols instead of the tiny native glyphs.
            self.assertIn(f"checkbox_tick_{role}.svg", dialog.styleSheet())
            self.assertIn(f"spin_up_{role}.svg", dialog.styleSheet())
            self.assertIn(f"spin_down_{role}.svg", dialog.styleSheet())
            self.assertIn("QSpinBox::up-button", dialog.styleSheet())
            self.assertIn("QSpinBox::down-button", dialog.styleSheet())
            spin = dialog.camera_group.width_spin
            self.assertFalse(spin.isReadOnly())
            spin.lineEdit().setText("1440")
            spin.interpretText()
            self.assertEqual(spin.value(), 1440)
            spin.stepUp()
            self.assertEqual(spin.value(), 1441)
            self.assertLess(dialog.minimumSizeHint().height(), 700)
            self.assertLess(dialog.minimumSizeHint().width(), 1100)

        self.assertNotEqual(dialogs[0].styleSheet(), dialogs[1].styleSheet())

    def test_no_tele_training_checkbox_style_replaces_the_native_tick_with_only_colour(self):
        custom_indicator_files = []
        for folder in (PROJECT_ROOT / "remote_guidance", PROJECT_ROOT / "server"):
            for path in folder.rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                if "QCheckBox::indicator:checked" in source:
                    custom_indicator_files.append(path.relative_to(PROJECT_ROOT).as_posix())
                    self.assertIn("image: url(", source, str(path))

        # Every other checkbox in section 8 deliberately retains Qt's
        # platform indicator, which already paints a real check mark.
        self.assertEqual(custom_indicator_files, ["remote_guidance/client_styles.py"])

    def test_settings_status_is_a_compact_strip_with_full_text_in_its_tooltip(self):
        dialog = self._dialog("student")
        message = "A deliberately long status message whose full detail must remain available."
        dialog._set_status(message, "ok")
        self.assertLessEqual(dialog.status.maximumHeight(), 32)
        self.assertFalse(dialog.status.wordWrap())
        self.assertEqual(dialog.status.toolTip(), message)

    def test_two_identical_keyboards_are_both_offered(self):
        """Teacher and student normally use the same model of keyboard, so
        the picker has to show both. mido's own port list showed one."""
        labels = ["SE25 MIDI1 #1", "SE25 MIDI2 #1", "SE25 MIDI1 #2", "SE25 MIDI2 #2"]
        dialog = self._dialog_with_ports("teacher", labels, ambiguous=["SE25 MIDI1", "SE25 MIDI2"])
        offered = [dialog.port_combo.itemText(i) for i in range(dialog.port_combo.count())]
        self.assertEqual(offered, labels)

    def test_a_shared_keyboard_name_is_called_out_in_the_dialog(self):
        """The #1/#2 numbering is enumeration order, not an identity, so
        picking the wrong one is easy and silent - say so before it costs
        someone a lesson."""
        dialog = self._dialog_with_ports(
            "student",
            ["SE25 MIDI1 #1", "SE25 MIDI1 #2"],
            ambiguous=["SE25 MIDI1"],
        )
        self.assertTrue(dialog.port_hint.isVisibleTo(dialog))
        hint = dialog.port_hint.text()
        self.assertIn("SE25 MIDI1", hint)
        self.assertIn("replug", hint.lower())

    def test_one_keyboard_gets_no_warning_and_no_suffix(self):
        dialog = self._dialog_with_ports("student", ["SE25 MIDI1", "SE25 MIDI2"])
        self.assertFalse(dialog.port_hint.isVisibleTo(dialog))
        self.assertEqual(dialog.port_hint.text(), "")

    def test_refresh_re_reads_the_ports_and_keeps_the_current_choice(self):
        from unittest import mock

        dialog = self._dialog_with_ports("teacher", ["SE25 MIDI1"])
        dialog.port_combo.setCurrentText("SE25 MIDI1")
        with mock.patch(
            "remote_guidance.settings_window.list_input_ports",
            return_value=["SE25 MIDI1 #1", "SE25 MIDI1 #2"],
        ), mock.patch(
            "remote_guidance.settings_window.ambiguous_port_names", return_value=["SE25 MIDI1"]
        ):
            dialog._refresh_ports()

        self.assertEqual(dialog.port_combo.currentText(), "SE25 MIDI1")
        self.assertEqual(dialog.port_combo.count(), 3)
        self.assertEqual(
            [dialog.port_combo.itemText(i) for i in range(dialog.port_combo.count())],
            ["SE25 MIDI1", "SE25 MIDI1 #1", "SE25 MIDI1 #2"],
        )
        self.assertTrue(dialog.port_hint.isVisibleTo(dialog))

    def test_both_roles_can_temporarily_connect_and_see_pressed_notes(self):
        from types import SimpleNamespace
        from unittest import mock

        listeners = []

        class FakeMidiListener:
            def __init__(self, port_name):
                self.port_name = port_name
                self.closed = False
                self.events = [SimpleNamespace(note=64)]
                listeners.append(self)

            def pop_events(self):
                events, self.events = self.events, []
                return events

            def close(self):
                self.closed = True

        for role in ("student", "teacher"):
            dialog = self._dialog_with_ports(role, ["SE25 MIDI1 #1", "SE25 MIDI1 #2"])
            dialog.port_combo.setCurrentText("SE25 MIDI1 #2")
            with mock.patch("remote_guidance.settings_window.MidiListener", FakeMidiListener):
                dialog._toggle_midi_test()

            self.assertIs(dialog.midi_test_listener, listeners[-1])
            self.assertTrue(dialog._midi_test_timer.isActive())
            self.assertIn("Disconnect", dialog.midi_test_btn.text())
            dialog._poll_midi_test()
            output = dialog.midi_test_output.text()
            self.assertIn("note 64", output)
            self.assertIn("SE25 MIDI1 #2", output)
            dialog.reject()
            self.assertTrue(listeners[-1].closed)

    def test_changing_the_selected_port_releases_the_test_keyboard(self):
        from unittest import mock

        class FakeMidiListener:
            def __init__(self, port_name):
                self.port_name = port_name
                self.closed = False

            def pop_events(self):
                return []

            def close(self):
                self.closed = True

        dialog = self._dialog_with_ports("teacher", ["Keyboard #1", "Keyboard #2"])
        dialog.port_combo.setCurrentText("Keyboard #1")
        with mock.patch("remote_guidance.settings_window.MidiListener", FakeMidiListener):
            dialog._toggle_midi_test()
        listener = dialog.midi_test_listener

        dialog.port_combo.setCurrentText("Keyboard #2")

        self.assertTrue(listener.closed)
        self.assertIsNone(dialog.midi_test_listener)
        self.assertFalse(dialog._midi_test_timer.isActive())
        self.assertIn("selection changed", dialog.midi_test_output.text())

    def test_accept_cancel_and_window_close_all_release_the_test_keyboard(self):
        from unittest import mock

        class FakeMidiListener:
            instances = []

            def __init__(self, port_name):
                self.port_name = port_name or "Keyboard"
                self.closed = False
                type(self).instances.append(self)

            def pop_events(self):
                return []

            def close(self):
                self.closed = True

        for finish in (lambda dialog: dialog.accept(), lambda dialog: dialog.reject(), lambda dialog: dialog.close()):
            dialog = self._dialog_with_ports("student", ["Keyboard"])
            with mock.patch("remote_guidance.settings_window.MidiListener", FakeMidiListener):
                dialog._toggle_midi_test()
            listener = FakeMidiListener.instances[-1]

            finish(dialog)

            self.assertTrue(listener.closed)
            self.assertIsNone(dialog.midi_test_listener)
            self.assertFalse(dialog._midi_test_timer.isActive())

    def test_both_roles_offer_the_separate_camera_profile_preview(self):
        for role in ("student", "teacher"):
            dialog = self._dialog(role)
            self.assertTrue(hasattr(dialog, "preview_btn"))
            self.assertIn("profile preview", dialog.preview_btn.text().lower())

    def test_preview_uses_the_unsaved_values_currently_in_the_form(self):
        from types import SimpleNamespace
        from unittest import mock

        import numpy as np

        remote = self._remote_with_distinct_roles()
        dialog = self._dialog("teacher", remote)
        dialog.camera_group.index_edit.setText("8")
        dialog.camera_group.width_spin.setValue(800)
        dialog.camera_group.height_spin.setValue(600)
        dialog.profile_combo.setCurrentText("desk-preview")
        frame = np.zeros((600, 800, 3), dtype=np.uint8)

        with mock.patch(
            "remote_guidance.settings_window.capture_profile_preview",
            return_value=(frame, SimpleNamespace(keys=[object(), object()])),
        ) as capture, mock.patch(
            "remote_guidance.settings_window.KeyboardProfilePreviewDialog"
        ) as preview_dialog:
            dialog._capture_profile_preview()

        used_camera, used_profile = capture.call_args.args
        self.assertEqual(used_camera.index, 8)
        self.assertEqual((used_camera.width, used_camera.height), (800, 600))
        self.assertEqual(used_profile, "desk-preview")
        preview_dialog.return_value.exec.assert_called_once_with()
        # Previewing must not have the side effect of collecting/saving.
        self.assertEqual(remote.teacher.camera.index, 1)
        self.assertEqual(remote.teacher.keyboard_profile, "teacher-profile")
        self.assertIn("Nothing was saved", dialog.status.text())

    def test_capture_renders_the_saved_pixel_mask_and_releases_the_camera(self):
        import numpy as np

        from app.keyboard.template import KeyBox, KeyboardTemplate
        from remote_guidance.settings_window import PREVIEW_CAPTURE_READS, capture_profile_preview

        with tempfile.TemporaryDirectory() as temp_dir:
            profile_dir = Path(temp_dir) / "test-profile"
            key_map = np.zeros((120, 160), dtype=np.uint8)
            key_map[30:90, 40:120] = 1
            KeyboardTemplate(
                frame_width=160,
                frame_height=120,
                keys=[KeyBox(id=0, kind="white")],
                key_map=key_map,
            ).save(profile_dir / "keyboard_template.json")

            source = np.full((120, 160, 3), 80, dtype=np.uint8)
            cameras = []

            class FakeCamera:
                is_opened = True

                def __init__(self, _config):
                    self.reads = 0
                    self.released = False
                    cameras.append(self)

                def read(self):
                    self.reads += 1
                    return source.copy()

                def release(self):
                    self.released = True

            preview, template = capture_profile_preview(
                CameraConfig(index=7, width=160, height=120),
                "test-profile",
                profile_data_dir=Path(temp_dir),
                camera_factory=FakeCamera,
            )

        self.assertEqual(preview.shape, source.shape)
        self.assertEqual(len(template.keys), 1)
        self.assertTrue(np.array_equal(preview[0, 0], source[0, 0]), "pixels outside the mask were changed")
        self.assertFalse(np.array_equal(preview[50, 50], source[50, 50]), "the keyboard mask was not rendered")
        self.assertEqual(cameras[0].reads, PREVIEW_CAPTURE_READS)
        self.assertTrue(cameras[0].released)

    def test_capture_refuses_a_resolution_mismatch_instead_of_resizing_the_mask(self):
        import numpy as np

        from app.keyboard.template import KeyBox, KeyboardTemplate
        from remote_guidance.settings_window import capture_profile_preview

        with tempfile.TemporaryDirectory() as temp_dir:
            profile_dir = Path(temp_dir) / "test-profile"
            KeyboardTemplate(
                frame_width=160,
                frame_height=120,
                keys=[KeyBox(id=0, kind="white")],
                key_map=np.ones((120, 160), dtype=np.uint8),
            ).save(profile_dir / "keyboard_template.json")

            class WrongSizeCamera:
                is_opened = True
                released = False

                def __init__(self, _config):
                    pass

                def read(self):
                    return np.zeros((60, 80, 3), dtype=np.uint8)

                def release(self):
                    type(self).released = True

            with self.assertRaisesRegex(ValueError, "camera returned 80 × 60.*calibrated at 160 × 120"):
                capture_profile_preview(
                    CameraConfig(index=7, width=80, height=60),
                    "test-profile",
                    profile_data_dir=Path(temp_dir),
                    camera_factory=WrongSizeCamera,
                )

        self.assertTrue(WrongSizeCamera.released)

    def test_saving_one_role_leaves_the_other_untouched(self):
        remote = self._remote_with_distinct_roles()
        dialog = self._dialog("student", remote)
        dialog.camera_group.index_edit.setText("7")
        dialog.port_combo.addItem("A Different Keyboard")
        dialog.port_combo.setCurrentText("A Different Keyboard")
        dialog.profile_combo.setCurrentText("new-profile")
        collected = dialog.collect()

        self.assertEqual(collected.student.camera.index, 7)
        self.assertEqual(collected.student.midi.port_name, "A Different Keyboard")
        self.assertEqual(collected.student.keyboard_profile, "new-profile")
        self.assertEqual(collected.teacher.camera.index, 1)
        self.assertEqual(collected.teacher.midi.port_name, "Teacher Keyboard")
        self.assertEqual(collected.teacher.keyboard_profile, "teacher-profile")

    def test_no_serial_port_is_ever_asked_for(self):
        """The LED strip and the actuators auto-detect and auto-connect;
        a port field here would only be a way to get them wrong."""
        from PySide6.QtWidgets import QLabel, QLineEdit

        for role in ("student", "teacher"):
            dialog = self._dialog(role)
            for edit in dialog.findChildren(QLineEdit):
                self.assertNotIn("tty", edit.placeholderText().lower())
                self.assertNotIn("com4", edit.placeholderText().lower())
            labels = " ".join(w.text() for w in dialog.findChildren(QLabel) if w.text()).lower()
            self.assertNotIn("led strip port", labels)
            self.assertNotIn("haptic rig port", labels)

    def test_the_network_block_is_not_editable_from_a_client(self):
        """Server URL is on the sign-in page; the rest of the network
        block is config.json only. Nothing here writes it."""
        remote = rgc.RemoteGuidanceConfig()
        remote.network.server_url = "https://relay.example.ac.uk"
        remote.network.guidance_queue_size = 99
        collected = self._dialog("student", remote).collect()
        self.assertEqual(collected.network.server_url, "https://relay.example.ac.uk")
        self.assertEqual(collected.network.guidance_queue_size, 99)

    def test_the_old_all_in_one_window_is_gone(self):
        import remote_guidance.settings_window as settings_module

        self.assertFalse(
            hasattr(settings_module, "RemoteGuidanceSettingsWindow"),
            "the combined launcher settings window must not come back",
        )


class TeacherRecordingWizardTests(unittest.TestCase):
    """Tele-training owns a styled copy; section 3 remains independent."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _wizard(self):
        from unittest import mock

        from app.config import CameraConfig, Config
        from remote_guidance.teacher.recording_wizard import TeacherRecordingWizard

        class FakeCamera:
            instances = []

            def __init__(self, camera_cfg):
                self.camera_cfg = camera_cfg
                self.released = False
                type(self).instances.append(self)

            def read(self):
                return None

            def release(self):
                self.released = True

        cfg = Config.load()
        cfg.camera = CameraConfig(index=7, width=800, height=600, fps=25)
        cfg.midi.port_name = "Teacher Settings MIDI"
        cfg.active_keyboard_profile = "teacher-settings-profile"
        with mock.patch("remote_guidance.teacher.recording_wizard.Camera", FakeCamera), mock.patch(
            "remote_guidance.teacher.recording_wizard.list_profiles",
            return_value=["teacher-settings-profile"],
        ):
            wizard = TeacherRecordingWizard(cfg)
            wizard.info_page.initializePage()
        self.addCleanup(wizard.close)
        return wizard, cfg, FakeCamera.instances[-1]

    def test_private_wizard_reads_all_devices_from_teacher_settings(self):
        from unittest import mock

        wizard, cfg, camera = self._wizard()
        self.assertIs(camera.camera_cfg, cfg.camera)
        self.assertEqual(wizard.info_page.port_name(), "Teacher Settings MIDI")
        self.assertEqual(wizard.info_page.keyboard_profile_name(), "teacher-settings-profile")
        self.assertIn("Camera 7", wizard.info_page.camera_value.text())
        self.assertIn("Teacher Settings MIDI", wizard.info_page.midi_value.text())
        self.assertFalse(hasattr(wizard.info_page, "port_combo"))
        self.assertFalse(hasattr(wizard.info_page, "profile_combo"))

        wizard.info_page.title_edit.setText("Remote lesson song")
        with mock.patch(
            "remote_guidance.teacher.recording_wizard.list_profiles",
            return_value=["teacher-settings-profile"],
        ):
            self.assertTrue(wizard.info_page.isComplete())

    def test_private_wizard_is_not_the_section_three_wizard_and_uses_teacher_ui(self):
        from app.gui.recording_wizard import RecordingWizard as SectionThreeRecordingWizard
        from remote_guidance.teacher.recording_wizard import TeacherRecordingWizard

        wizard, _cfg, _camera = self._wizard()
        self.assertFalse(issubclass(TeacherRecordingWizard, SectionThreeRecordingWizard))
        self.assertEqual(TeacherRecordingWizard.__module__, "remote_guidance.teacher.recording_wizard")
        self.assertEqual(wizard.objectName(), "teacherRecordingWizard")
        self.assertIn("#f2a65a", wizard.styleSheet())
        self.assertEqual(wizard.wizardStyle(), wizard.WizardStyle.ClassicStyle)
        self.assertEqual(wizard.record_page.view.max_size, (560, 360))
        self.assertEqual(wizard.record_page.start_btn.property("role"), "primary")
        self.assertEqual(wizard.record_page.stop_btn.property("role"), "danger")
        self.assertLess(wizard.minimumHeight(), 700)

    def test_closing_private_wizard_releases_every_claimed_device(self):
        from unittest import mock

        wizard, _cfg, camera = self._wizard()
        recorder = mock.MagicMock()
        audio = mock.MagicMock()
        writer = mock.MagicMock()
        led = mock.MagicMock()
        wizard.record_page.midi_recorder = recorder
        wizard.record_page.audio = audio
        wizard.record_page.video_writer = writer
        wizard.record_page.led = led
        wizard.show()
        self.app.processEvents()
        wizard.close()
        self.app.processEvents()

        recorder.close.assert_called_once_with()
        audio.stop_all.assert_called_once_with()
        audio.close.assert_called_once_with()
        writer.release.assert_called_once_with()
        led.close.assert_called_once_with()
        self.assertTrue(camera.released)


class TeacherLiveRecorderTests(unittest.TestCase):
    """A live lesson has to land in data/music/ in exactly the layout the
    Song Recording Wizard writes - anything else and the song picker, the
    uploader and the playback tools would each need a second code path."""

    def setUp(self):
        from unittest import mock

        self._dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)

        self.writer = mock.MagicMock()
        self.writer.isOpened.return_value = True
        patcher = mock.patch(
            "remote_guidance.teacher.live_recorder.cv2.VideoWriter", return_value=self.writer
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        profile_patcher = mock.patch("remote_guidance.teacher.live_recorder.snapshot_profile")
        self.snapshot_profile = profile_patcher.start()
        self.addCleanup(profile_patcher.stop)

    def _recorder(self, name="remote-1770000000"):
        from types import SimpleNamespace

        from remote_guidance.teacher.live_recorder import TeacherLiveRecorder

        recorder = TeacherLiveRecorder(
            name,
            fps=30,
            profile_name="teacher-profile",
            data_dir=self.data_dir,
            sync_flash=False,
        )
        recorder.start(SimpleNamespace(shape=(480, 640, 3)))
        return recorder

    def _play(self, recorder, notes=(60, 62)):
        from app.midi import MidiMessage

        now = time.time()
        for offset, note in enumerate(notes):
            recorder.add_midi([MidiMessage("note_on", note, 100)], now + offset)
            recorder.add_midi([MidiMessage("note_off", note, 0)], now + offset + 0.25)

    def test_a_lesson_is_saved_in_the_wizards_layout(self):
        from app.music_recording import list_songs, load_fingering, load_raw_midi_log

        recorder = self._recorder()
        self._play(recorder)
        recorder.stop()
        recorder.save_raw()

        raw = self.data_dir / "remote-1770000000" / "raw"
        for name in ("midi_raw.json", "notes.json", "sync.json"):
            self.assertTrue((raw / name).exists(), f"{name} is missing from the lesson recording")
        self.assertTrue((self.data_dir / "remote-1770000000" / "score.mid").exists())
        self.snapshot_profile.assert_called_once()
        self.assertEqual(self.snapshot_profile.call_args.args[0], "teacher-profile")

        # Not a song until the offline pass has been through it, exactly
        # as the wizard behaves: meta.json is written last.
        self.assertEqual(list_songs(self.data_dir), [])
        recorder.save_fingering_and_meta([None, None])
        self.assertEqual(list_songs(self.data_dir), ["remote-1770000000"])

        events = load_raw_midi_log(raw / "midi_raw.json")
        self.assertEqual([e.type for e in events], ["note_on", "note_off", "note_on", "note_off"])
        self.assertTrue(all(e.abs_time > 1e6 for e in events), "raw times must be absolute epoch seconds")
        self.assertEqual([e.note for e in load_fingering(self.data_dir / "remote-1770000000" / "fingering.json")], [60, 62])

    def test_the_sync_file_records_the_teachers_own_clocks(self):
        from app.music_recording import SyncInfo

        recorder = self._recorder()
        self._play(recorder, notes=(64,))
        recorder.stop()
        recorder.save_raw()

        sync = SyncInfo.load(self.data_dir / "remote-1770000000" / "raw" / "sync.json")
        self.assertGreater(sync.video_start_time, 1e6)
        self.assertGreater(sync.midi_start_time, 1e6)
        # No strip answered, so app.sync_led falls back to the start times
        # rather than being handed a flash that never happened.
        self.assertEqual((sync.led_on_time, sync.led_off_time), (0.0, 0.0))

    def test_the_saved_duration_drops_the_lead_in_before_the_first_note(self):
        """Every stored time here is an absolute epoch value, so the
        lead-in has to be measured from the start of the recording.
        Subtracting the first note's epoch stamp from an elapsed duration
        would make every lesson 0.0 seconds long."""
        from app.midi import MidiMessage
        from app.music_recording import SongMeta

        recorder = self._recorder()
        recorder.video_start_time = 1_770_000_000.0
        recorder.add_midi([MidiMessage("note_on", 60, 100)], 1_770_000_004.0)
        recorder.add_midi([MidiMessage("note_off", 60, 0)], 1_770_000_005.0)
        recorder.stop()
        recorder.duration_s = 10.0  # four seconds of talking, six of playing
        recorder.save_raw()
        recorder.save_fingering_and_meta([None])

        meta = SongMeta.load(self.data_dir / "remote-1770000000" / "meta.json")
        self.assertAlmostEqual(meta.duration_s, 6.0)
        self.assertEqual(meta.note_count, 1)

    def test_velocity_zero_note_ons_are_stored_as_note_offs(self):
        """mido's convention, applied exactly as RawMidiRecorder applies
        it - otherwise a keyboard that never sends note_off would look
        like a recording of notes nobody released."""
        from app.midi import MidiMessage

        recorder = self._recorder()
        recorder.add_midi([MidiMessage("note_on", 60, 100), MidiMessage("note_on", 60, 0)], time.time())
        self.assertEqual([e.type for e in recorder.raw_events], ["note_on", "note_off"])
        self.assertEqual(recorder.note_count, 1)

    def test_nothing_is_written_after_stop(self):
        recorder = self._recorder()
        recorder.stop()
        self._play(recorder)
        recorder.write_frame()
        self.assertEqual(recorder.raw_events, [])
        self.writer.release.assert_called_once_with()

    def test_the_session_name_is_epoch_seconds(self):
        from remote_guidance.teacher.live_recorder import live_session_name

        self.assertEqual(live_session_name(1770000000.9), "remote-1770000000")


class _ClientStageChecks:
    """Shared checks for both remote clients' staged flow.

    They used to put sign-in, room selection, device setup, the camera
    and the results table all on screen at once - the teacher window
    needed ~960px - and the teacher opened its camera in __init__. Each
    client now shows one stage at a time (sign in -> room -> session) and
    opens no device until the session stage is reached.

    A plain mixin rather than a TestCase subclass, so the shared checks
    are not also collected as a class of their own. The window is built
    once per class: constructing one loads MediaPipe."""

    max_minimum_height = 700  # must fit a 768px screen, menu bar and dock
    required_widgets: tuple = ()
    device_attributes: tuple = ()  # must all be None before the session stage
    hardware_attributes: tuple = ()  # camera/MIDI - not opened until a session starts
    window_module = ""  # dotted path, for patching that module's bridge

    @classmethod
    def make_window(cls):
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        # No camera here: both windows report the failure and degrade
        # rather than refusing to build, so this still works headless.
        cls.window = cls.make_window()
        cls.window.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "window", None) is not None:
            cls.window.close()

    def setUp(self):
        # One window is shared across the class (building one loads
        # MediaPipe), so put it back on step 1 before each test rather
        # than letting execution order decide what stage it is on.
        from remote_guidance.gui_common import STAGE_SIGN_IN

        self.window._show_stage(STAGE_SIGN_IN)
        self.app.processEvents()

    def test_it_fits_a_short_laptop_screen(self):
        height = self.window.minimumSizeHint().height()
        self.assertLess(
            height,
            self.max_minimum_height,
            f"{type(self.window).__name__} needs {height}px minimum - it has to fit a 768px-tall screen "
            "with room for the menu bar and dock",
        )

    def test_it_opens_on_the_sign_in_stage_alone(self):
        """Step 1 is the login form and nothing else - no room controls,
        no device setup, no camera view."""
        from remote_guidance.gui_common import STAGE_SIGN_IN

        self.assertEqual(self.window.stages.currentIndex(), STAGE_SIGN_IN)
        self.assertTrue(self.window.sign_in_panel.isVisible())
        self.assertFalse(self.window.room_panel.isVisible())
        self.assertFalse(self.window.stages.widget(2).isVisible())

    def test_no_device_is_opened_before_the_session_stage(self):
        """The whole point of deferring: opening the client must not
        claim the camera, so another tool can still use it."""
        for name in self.device_attributes:
            self.assertIsNone(
                getattr(self.window, name),
                f"{name} was opened during __init__ - it must wait for the session stage",
            )

    def test_the_three_stages_are_in_order(self):
        from remote_guidance.gui_common import STAGE_NAMES, STAGE_ROOM, STAGE_SESSION, STAGE_SIGN_IN

        self.assertEqual(self.window.stages.count(), 3)
        self.assertEqual((STAGE_SIGN_IN, STAGE_ROOM, STAGE_SESSION), (0, 1, 2))
        self.assertEqual(STAGE_NAMES, ("Sign in", "Room", "Session"))

    def test_stage_transitions_move_forward_and_back(self):
        from remote_guidance.gui_common import STAGE_ROOM, STAGE_SESSION, STAGE_SIGN_IN
        from remote_guidance.network_client import Session

        session = Session(access_token="t", username="someone", role=self.window.sign_in_panel.role)
        self.window._on_signed_in(session)
        self.app.processEvents()
        self.assertEqual(self.window.stages.currentIndex(), STAGE_ROOM)
        self.assertTrue(self.window.room_panel.isVisible())
        self.assertFalse(self.window.sign_in_panel.isVisible())

        self.window._show_stage(STAGE_SESSION)
        self.app.processEvents()
        self.assertTrue(self.window.stages.widget(2).isVisible())

        self.window.back_to_rooms()
        self.app.processEvents()
        self.assertEqual(self.window.stages.currentIndex(), STAGE_ROOM)
        # Leaving the session stage must hand every device back.
        for name in self.device_attributes:
            self.assertIsNone(getattr(self.window, name), f"{name} survived leaving the session stage")

        self.window._show_stage(STAGE_SIGN_IN)

    def test_the_stage_label_says_where_the_user_is(self):
        self.assertIn("Step 1 of 3", self.window.stage_label.text())

    def test_every_widget_the_window_drives_still_exists(self):
        for name in self.required_widgets:
            self.assertTrue(hasattr(self.window, name), f"{name} went missing in the layout rework")

    def test_the_password_is_never_kept(self):
        """It is used once, at login, and the box is cleared - so it is
        not sitting in a widget for the rest of the session."""
        panel = self.window.sign_in_panel
        panel.password_edit.setText("a-long-enough-password")
        panel._on_signed_in(_FakeSession(panel.role))
        self.assertEqual(panel.password_edit.text(), "")

    def test_the_demo_account_is_ready_to_use(self):
        """Both clients are started for every run; typing the same pair
        into two windows each time is friction the demo defaults remove.
        The password is a constant in gui_common, never config."""
        from remote_guidance.gui_common import DEMO_ACCOUNTS

        panel = self.window.sign_in_panel
        username, password = DEMO_ACCOUNTS[panel.role]
        self.assertEqual(panel.username_edit.text(), username)
        self.assertEqual(panel.password_edit.text(), password)

    def test_signing_out_offers_the_demo_password_again(self):
        """_on_signed_in empties the box, so without this the second
        sign-in of a run would be the one that needs typing."""
        panel = self.window.sign_in_panel
        panel._on_signed_in(_FakeSession(panel.role))
        self.assertEqual(panel.password_edit.text(), "")

        self.window.sign_out()
        self.app.processEvents()
        self.assertEqual(panel.password_edit.text(), panel.demo_password)

    def test_a_custom_username_gets_no_prefilled_password(self):
        """A demo secret must never be offered up to a real account."""
        panel = self.window.sign_in_panel
        panel.username_edit.setText("a.real.person")
        panel.restore_demo_password()
        self.assertEqual(panel.password_edit.text(), "")

        panel.username_edit.setText(panel.demo_username)
        panel.restore_demo_password()
        self.assertEqual(panel.password_edit.text(), panel.demo_password)

    def test_there_is_exactly_one_settings_button(self):
        """One per client, reachable from every stage. The launcher has
        no remote-settings window any more, so this is the only way in."""
        from PySide6.QtWidgets import QPushButton

        self.assertTrue(hasattr(self.window, "settings_btn"))
        settings_buttons = [
            b for b in self.window.findChildren(QPushButton) if b.text().strip().lower() == "settings"
        ]
        self.assertEqual(len(settings_buttons), 1, "a client must have exactly one Settings button")
        self.assertTrue(self.window.settings_btn.isEnabled())

    def test_the_settings_button_opens_this_client_role(self):
        """A student client must never open the teacher's devices."""
        from unittest import mock

        with mock.patch("remote_guidance.settings_window.RemoteSettingsDialog") as dialog_cls:
            dialog_cls.return_value.saved = False
            self.window.open_settings()
        role = dialog_cls.call_args.args[0]
        self.assertEqual(role, self.window.sign_in_panel.role)

    def test_settings_can_be_locked_while_a_session_runs(self):
        self.window.set_settings_enabled(False)
        self.assertFalse(self.window.settings_btn.isEnabled())
        self.window.set_settings_enabled(True)
        self.assertTrue(self.window.settings_btn.isEnabled())

    def test_reaching_the_session_stage_opens_no_hardware(self):
        """Arriving in a room used to claim the camera immediately, which
        held it through the whole of setup and locked out every other
        tool. Only pressing start may open a device now."""
        from unittest import mock

        with mock.patch(f"{self.window_module}.RemoteClientBridge"), mock.patch.object(
            self.window, "persist_connection"
        ):
            self.window.api = mock.MagicMock()
            self.window.enter_session({"room_id": "r-1", "name": "test room"})
            self.app.processEvents()

        try:
            for name in self.hardware_attributes:
                self.assertIsNone(
                    getattr(self.window, name),
                    f"{name} was opened on entering the session stage - it must wait for the start button",
                )
        finally:
            self.window.leave_session()
            self.app.processEvents()


class _FakeSession:
    def __init__(self, role):
        self.role = role
        self.username = "someone"
        self.access_token = "t"


class ClientStyleSheetTests(unittest.TestCase):
    """A button that can no longer be pressed has to look like it."""

    def test_a_disabled_highlighted_button_stops_looking_highlighted(self):
        """A role attribute and a pseudo-class weigh the same in Qt's
        stylesheets, so whichever rule comes last wins. With the accent
        rules last, a disabled primary button kept the full accent
        background - which is how a pressed "Ready for guidance" went on
        looking unpressed."""
        from remote_guidance.client_styles import STUDENT_STYLE_SHEET, TEACHER_STYLE_SHEET

        for sheet in (TEACHER_STYLE_SHEET, STUDENT_STYLE_SHEET):
            accent = sheet.index('QPushButton[role="primary"] {')
            dimmed = sheet.index('QPushButton[role="primary"]:disabled')
            self.assertGreater(dimmed, accent)
            self.assertGreater(sheet.index('QPushButton[role="danger"] {'), -1)
            self.assertIn('QLabel#recordingChip', sheet)


class TeacherWindowLayoutTests(_ClientStageChecks, unittest.TestCase):
    @classmethod
    def make_window(cls):
        from app.config import Config

        from remote_guidance.teacher.window import TeacherRemoteWindow

        return TeacherRemoteWindow(Config.load(), rgc.RemoteGuidanceConfig())

    device_attributes = ("detector", "audio", "bridge", "live_recorder")
    hardware_attributes = ("detector", "audio", "live_recorder")
    window_module = "remote_guidance.teacher.window"
    required_widgets = (
        "view", "table", "summary_view", "detect_label", "link_label", "status_label",
        "sign_in_panel", "room_panel", "room_label", "song_combo", "upload_btn", "trigger_btn",
        "live_btn", "begin_btn", "student_ready_label", "pause_btn", "resume_btn", "stop_btn",
        "chord_check", "timbre_combo", "playback_combo", "guidance_tabs", "live_guidance_page",
        "recorded_guidance_page", "results_guidance_page", "record_song_btn", "recording_pause_btn",
        "recording_resume_btn", "recording_stop_btn", "song_refresh_btn", "stages", "stage_label",
        "role_label", "camera_panel", "results_tab_index", "record_check", "progress",
        "recording_chip",
    )

    def test_a_lesson_names_itself_when_it_begins_not_when_devices_open(self):
        """One lesson, one name: the teacher's data/music/remote-<epoch>/
        and the student's data/quiz/remote-<epoch>/ are two halves of the
        same session, and neither end invents its own name for it.

        Made at Start teaching rather than at Get ready, because a
        name that exists before the lesson does is a name the next lesson
        can inherit - which is how a second lesson came to be recorded
        into the first one's folder."""
        from unittest import mock

        old_room, old_name = self.window.room, self.window.session_name
        old_active, old_mode = self.window.session_active, self.window.session_mode
        old_armed, old_ready = self.window.live_armed, self.window.student_ready
        self.window.room = {"room_id": "r-1", "name": "test room"}
        self.window.record_check.setChecked(True)
        try:
            with mock.patch.object(self.window, "_open_detector", return_value=True), mock.patch.object(
                self.window, "_connect_midi", return_value=True
            ), mock.patch.object(self.window, "_start_live_recording") as start_recording:
                self.window._start_live_session()
                self.assertIsNone(self.window.session_name, "arming the devices must not name a lesson")
                start_recording.assert_not_called()

                self.window.student_ready = True
                with mock.patch.object(self.window, "_recording_preflight", return_value=True), \
                        mock.patch.object(self.window, "_run_api"):
                    self.window._begin_lesson()
                session_name = self.window.session_name
                self.assertRegex(session_name, r"^remote-\d{10}$")

                with mock.patch.object(self.window, "_send") as send:
                    self.window._on_session_created({"session_id": "s-live"})
                # Still nothing recorded: the count-in has not run out.
                start_recording.assert_not_called()
                self.window._on_lesson_start()
                start_recording.assert_called_once_with(session_name)
        finally:
            if self.window._countdown_timer is not None:
                self.window._countdown_timer.stop()
                self.window._countdown_timer = None
            self.window.room = old_room
            self.window.session_name = old_name
            self.window.session_active, self.window.session_mode = old_active, old_mode
            self.window.live_armed, self.window.student_ready = old_armed, old_ready
            self.window._sync_guidance_controls()

        payload = send.call_args.args[1]
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(payload["session_name"], session_name)
        self.assertGreater(payload["start_at_unix_ns"], 0, "both ends need one agreed start instant")
        self.assertEqual(payload["lead_ms"], 3000)

    def test_a_stopped_lesson_takes_its_name_with_it(self):
        """The bug this whole two-stage split exists to kill: a name that
        outlives its session is one the next session can be written
        under, on top of what is already there."""
        from unittest import mock

        old_name, old_active = self.window.session_name, self.window.session_active
        self.window.session_name = "remote-1770000000"
        self.window.session_active = True
        try:
            with mock.patch.object(self.window, "_send"), mock.patch.object(self.window, "_close_detector"):
                self.window._stop()
            self.assertIsNone(self.window.session_name)
        finally:
            self.window.session_name, self.window.session_active = old_name, old_active
            self.window._sync_guidance_controls()

    def test_the_lesson_recording_is_optional_and_fixed_for_the_session(self):
        from unittest import mock

        old_room, old_name = self.window.room, self.window.session_name
        self.window.room = {"room_id": "r-1", "name": "test room"}
        self.window.record_check.setChecked(False)
        self.window.live_armed = self.window.student_ready = True
        self.window.session_active = False
        try:
            with mock.patch.object(self.window, "_open_detector", return_value=True), mock.patch.object(
                self.window, "_connect_midi", return_value=True
            ), mock.patch.object(self.window, "_start_live_recording") as start_recording, mock.patch.object(
                self.window, "_run_api"
            ):
                self.window._begin_lesson()
                self.window._on_lesson_start()
            start_recording.assert_not_called()
            # Still named: the student names its own folder after the
            # session whether or not the teacher keeps a copy.
            self.assertRegex(self.window.session_name, r"^remote-\d{10}$")
        finally:
            self.window.room = old_room
            self.window.session_name = old_name
            self.window.live_armed = self.window.student_ready = False
            self.window.record_check.setChecked(True)
            self.window._sync_guidance_controls()

    def test_the_lesson_is_recorded_without_the_preview_overlay(self):
        """The frame on screen has the whole key map painted over it,
        hands included. Recording that would leave the offline finger
        pass with a video it cannot track in."""
        from types import SimpleNamespace
        from unittest import mock

        from app.midi import MidiMessage

        old_detector, old_recorder, old_audio = (
            self.window.detector,
            self.window.live_recorder,
            self.window.audio,
        )
        detector = mock.MagicMock()
        detector.poll.return_value = SimpleNamespace(
            frame="frame-with-overlay",
            raw_frame="frame-as-the-camera-produced-it",
            midi_events=[MidiMessage("note_on", 60, 100)],
            actions=[],
            hands={},
        )
        recorder = mock.MagicMock()
        self.window.detector = detector
        self.window.live_recorder = recorder
        self.window.audio = None
        try:
            with mock.patch.object(self.window.view, "set_frame"):
                self.window._tick()
        finally:
            self.window.detector, self.window.live_recorder = old_detector, old_recorder
            self.window.audio = old_audio

        recorder.write_frame.assert_called_once_with("frame-as-the-camera-produced-it")
        messages, stamp = recorder.add_midi.call_args.args
        self.assertEqual([m.note for m in messages], [60])
        self.assertGreater(stamp, 1e6)

    def test_the_students_cue_is_never_delayed_by_the_teachers_archive(self):
        """Encoding a frame takes milliseconds. Guidance goes out first
        and the recording catches up afterwards - never the other way."""
        from types import SimpleNamespace
        from unittest import mock

        from app.midi import MidiMessage
        from remote_guidance.protocol import GuidanceAction

        order = []
        old_detector, old_recorder, old_audio = (
            self.window.detector,
            self.window.live_recorder,
            self.window.audio,
        )
        detector = mock.MagicMock()
        detector.poll.return_value = SimpleNamespace(
            frame=None,
            raw_frame=None,
            midi_events=[MidiMessage("note_on", 60, 100)],
            actions=[GuidanceAction(note=60, note_name="C4", finger="R1")],
            hands={},
        )
        recorder = mock.MagicMock()
        recorder.write_frame.side_effect = lambda *_a: order.append("record")
        recorder.add_midi.side_effect = lambda *_a: order.append("record")
        self.window.detector = detector
        self.window.live_recorder = recorder
        self.window.audio = None
        try:
            with mock.patch.object(self.window, "_send_guidance", side_effect=lambda *_a: order.append("cue")):
                self.window._tick()
        finally:
            self.window.detector, self.window.live_recorder = old_detector, old_recorder
            self.window.audio = old_audio

        self.assertEqual(order[0], "cue", f"the recording ran before the cue was sent: {order}")

    def test_handing_the_camera_back_closes_the_recording_first(self):
        """There is no state where the camera is gone but the recording
        it feeds is still open, whichever way the session ended."""
        from unittest import mock

        old_detector = self.window.detector
        detector = mock.MagicMock()
        self.window.detector = detector
        order = []
        try:
            with mock.patch.object(
                self.window, "_finish_live_recording", side_effect=lambda: order.append("recording")
            ):
                detector.close.side_effect = lambda: order.append("camera")
                self.window._close_detector()
        finally:
            self.window.detector = old_detector

        self.assertEqual(order, ["recording", "camera"])

    def test_a_lesson_becomes_a_song_only_once_its_fingers_are_matched(self):
        from unittest import mock

        recorder = mock.MagicMock()
        recorder.song_name = "remote-1770000000"
        recorder.note_count = 12
        with mock.patch.object(self.window, "_refresh_songs") as refresh:
            self.window._on_recording_analyzed(recorder, [None])
        recorder.save_fingering_and_meta.assert_called_once_with([None])
        refresh.assert_called_once_with("music/remote-1770000000")
        self.assertIn("remote-1770000000", self.window.status_label.text())
        self.assertFalse(self.window.progress.isVisible())

    def test_guidance_and_results_have_separate_workspaces(self):
        self.assertEqual(self.window.guidance_tabs.count(), 3)
        self.assertEqual(
            [self.window.guidance_tabs.tabText(i) for i in range(3)],
            ["Live studio", "Recording library", "Student results"],
        )
        self.assertTrue(self.window.live_guidance_page.isAncestorOf(self.window.live_btn))
        self.assertTrue(self.window.live_guidance_page.isAncestorOf(self.window.view))
        self.assertTrue(self.window.recorded_guidance_page.isAncestorOf(self.window.song_combo))
        self.assertTrue(self.window.recorded_guidance_page.isAncestorOf(self.window.record_song_btn))
        self.assertTrue(self.window.results_guidance_page.isAncestorOf(self.window.table))
        self.assertTrue(self.window.results_guidance_page.isAncestorOf(self.window.summary_view))
        self.assertFalse(self.window.live_guidance_page.isAncestorOf(self.window.table))

    def test_teacher_has_a_compact_warm_role_header(self):
        self.assertEqual(self.window.centralWidget().objectName(), "teacherCentral")
        self.assertIn("TEACHER", self.window.role_label.text())
        self.assertIn("#f2a65a", self.window.styleSheet())
        self.assertEqual(self.window.role_label.parent().objectName(), "topBar")
        self.assertIs(self.window.stage_label.parent(), self.window.role_label.parent())
        self.assertIs(self.window.link_label.parent(), self.window.role_label.parent())
        self.assertIs(self.window.settings_btn.parent(), self.window.role_label.parent())
        self.assertLessEqual(self.window.status_label.maximumHeight(), 30)

    def test_teacher_camera_preview_is_complete_but_bounded(self):
        self.assertEqual(self.window.view.max_size, (560, 360))
        self.assertTrue(self.window.camera_panel.isAncestorOf(self.window.view))

    def test_recording_wizard_uses_the_private_copy_with_teacher_configuration(self):
        from unittest import mock

        old_room = self.window.room
        wizard = mock.MagicMock()
        wizard.review_page.isComplete.return_value = False
        self.window.room = {"room_id": "r-1", "name": "test room"}
        try:
            with (
                mock.patch(
                    "remote_guidance.teacher.window.TeacherRecordingWizard", return_value=wizard
                ) as wizard_cls,
                mock.patch.object(self.window, "_close_detector") as close_detector,
            ):
                self.window._open_recording_wizard()
                finished = wizard.finished.connect.call_args.args[0]
                self.assertFalse(self.window.guidance_tabs.isTabEnabled(self.window.live_tab_index))
                finished(0)
        finally:
            self.window.recording_wizard = None
            self.window.room = old_room
            self.window._sync_guidance_controls()

        wizard_cls.assert_called_once_with(self.window.cfg)
        close_detector.assert_called_once_with()
        wizard.show.assert_called_once_with()
        self.assertTrue(self.window.guidance_tabs.isTabEnabled(self.window.live_tab_index))

    def test_recorded_trigger_opens_its_own_session_without_guidance_mode(self):
        from unittest import mock

        old_api = self.window.api
        old_room = self.window.room
        old_recording_id = self.window.recording_id
        old_active, old_mode = self.window.session_active, self.window.session_mode
        api = mock.MagicMock()
        self.window.api = api
        self.window.room = {"room_id": "r-1", "name": "test room"}
        self.window.recording_id = "rec-1"
        self.window.session_active = False
        self.window.session_mode = None
        try:
            with mock.patch.object(self.window, "_run_api", side_effect=lambda call, *_args: call()):
                self.window._trigger_recording()
        finally:
            self.window.api = old_api
            self.window.room = old_room
            self.window.recording_id = old_recording_id
            self.window.session_active, self.window.session_mode = old_active, old_mode
            self.window._sync_guidance_controls()

        request = api.create_session.call_args.args[1]
        self.assertEqual(request["mode"], "recording")
        self.assertEqual(request["recording_id"], "rec-1")
        self.assertNotIn("guidance_mode", request)

    def test_song_upload_no_longer_requires_starting_a_live_session(self):
        from types import SimpleNamespace
        from unittest import mock

        old_room = self.window.room
        old_active = self.window.session_active
        self.window.room = {"room_id": "r-1", "name": "test room"}
        self.window.session_active = False
        try:
            with mock.patch(
                "remote_guidance.teacher.window.list_available_songs",
                return_value=[SimpleNamespace(label="music/new-song")],
            ):
                self.window._refresh_songs()
                self.assertTrue(self.window.upload_btn.isEnabled())
        finally:
            self.window.room = old_room
            self.window.session_active = old_active
            self.window._refresh_songs()

    def test_recorded_session_start_and_playback_are_sent_in_order(self):
        from unittest import mock

        old_session_id = self.window.session_id
        old_active, old_mode = self.window.session_active, self.window.session_mode
        old_recording_id = self.window.recording_id
        self.window.recording_id = "rec-1"
        try:
            with mock.patch.object(self.window, "_send") as send:
                self.window._on_recording_session_created({"session_id": "s-recorded", "mode": "recording"})
        finally:
            self.window.session_id = old_session_id
            self.window.session_active, self.window.session_mode = old_active, old_mode
            self.window.recording_id = old_recording_id
            self.window._sync_guidance_controls()

        from remote_guidance.protocol import TYPE_RECORDING_START, TYPE_SESSION_START

        self.assertEqual([call.args[0] for call in send.call_args_list], [TYPE_SESSION_START, TYPE_RECORDING_START])
        self.assertEqual(send.call_args_list[0].args[1]["mode"], "recording")
        self.assertEqual(send.call_args_list[1].args[1]["recording_id"], "rec-1")

    def test_teacher_has_no_student_guidance_selector(self):
        """Visual/haptic/both belongs to the student who renders it."""
        from PySide6.QtWidgets import QLabel

        self.assertFalse(hasattr(self.window, "guidance_combo"))
        labels = {label.text().strip() for label in self.window.findChildren(QLabel)}
        self.assertNotIn("Student guidance:", labels)

    def test_session_page_does_not_repeat_the_settings_midi_picker(self):
        """The saved port has one owner: this client's Settings dialog.
        The session page keeps only the manual connect action."""
        self.assertFalse(hasattr(self.window, "port_combo"))
        self.assertFalse(hasattr(self.window, "_refresh_ports"))

    def test_there_is_no_separate_connect_midi_action(self):
        """Get ready opens the keyboard. A second button that
        did the same thing was one more state to get wrong, and nothing
        it offered was not already part of arming a session."""
        self.assertFalse(hasattr(self.window, "midi_btn"))
        self.assertFalse(hasattr(self.window, "_toggle_midi"))
        self.assertGreaterEqual(self.window.live_controls_row.indexOf(self.window.chord_check), 0)

    def test_beginning_a_lesson_needs_both_devices_and_a_ready_student(self):
        """One teacher and one student by design; several students at
        once is future work."""
        self.window.session_active = False
        self.window.recording_wizard = None
        for armed, ready, expected in (
            (False, False, False), (True, False, False), (False, True, False), (True, True, True)
        ):
            self.window.live_armed, self.window.student_ready = armed, ready
            self.window._sync_guidance_controls()
            self.assertIs(
                self.window.begin_btn.isEnabled(), expected,
                f"armed={armed} ready={ready} should leave Start teaching {'on' if expected else 'off'}",
            )
        self.window.live_armed = self.window.student_ready = False
        self.window._sync_guidance_controls()

    def test_the_accent_marks_the_one_step_that_is_next(self):
        """Two staged buttons with no other difference between them left
        the teacher guessing which one the session was waiting on. Only
        the pressable one is highlighted, so a spent step and a step that
        is not reachable yet both read as spent."""
        old = (self.window.session_active, self.window.live_armed, self.window.student_ready)
        self.window.recording_wizard = None
        try:
            for active, armed, ready, highlighted in (
                (False, False, False, self.window.live_btn),
                (False, True, False, None),
                (False, True, True, self.window.begin_btn),
                (True, True, True, None),
            ):
                self.window.session_active, self.window.session_mode = active, "live" if active else None
                self.window.live_armed, self.window.student_ready = armed, ready
                self.window._sync_guidance_controls()
                for button in (self.window.live_btn, self.window.begin_btn):
                    self.assertEqual(
                        button.property("role") == "primary", button is highlighted,
                        f"active={active} armed={armed} ready={ready}: {button.text()!r}",
                    )
                    self.assertEqual(button.property("role") == "primary", button.isEnabled())
        finally:
            self.window.session_active, self.window.live_armed, self.window.student_ready = old
            self.window.session_mode = None
            self.window._sync_guidance_controls()

    def test_start_teaching_leaves_the_screen_once_the_lesson_is_running(self):
        """It can do nothing while a lesson is on, and a button that
        answers nothing is one the teacher presses twice wondering why."""
        old = (self.window.session_active, self.window.live_armed, self.window.student_ready)
        try:
            self.window.session_active, self.window.session_mode = False, None
            self.window.live_armed = self.window.student_ready = True
            self.window._sync_guidance_controls()
            self.assertFalse(self.window.begin_btn.isHidden())
            self.window.session_active, self.window.session_mode = True, "live"
            self.window._sync_guidance_controls()
            self.assertTrue(self.window.begin_btn.isHidden())
            self.assertFalse(self.window.begin_btn.isEnabled())
        finally:
            self.window.session_active, self.window.live_armed, self.window.student_ready = old
            self.window.session_mode = None
            self.window._sync_guidance_controls()

    def test_the_teacher_is_told_while_the_lesson_is_being_recorded(self):
        """The status line says it once and is then written over. Whether
        this machine is writing data/music has to be readable at any
        moment of the lesson, and it follows the writer rather than the
        checkbox: a lesson whose camera failed is not being recorded."""
        from types import SimpleNamespace

        old = self.window.live_recorder
        try:
            self.window.live_recorder = SimpleNamespace(song_name="remote-1760000000")
            self.window._update_recording_indicator()
            self.assertFalse(self.window.recording_chip.isHidden())
            self.assertIn("remote-1760000000", self.window.recording_chip.text())
            self.assertIn("RECORDING", self.window.recording_chip.text())
            self.window.live_recorder = None
            self.window._update_recording_indicator()
            self.assertTrue(self.window.recording_chip.isHidden())
        finally:
            self.window.live_recorder = old
            self.window._update_recording_indicator()

    def test_teacher_offers_the_quiz_timbres(self):
        from note_audio import DEFAULT_TIMBRE, TIMBRES

        choices = [self.window.timbre_combo.itemData(i) for i in range(self.window.timbre_combo.count())]
        self.assertEqual(choices, list(TIMBRES))
        self.assertEqual(self.window.timbre_combo.currentData(), DEFAULT_TIMBRE)

    def test_teacher_mute_does_not_claim_the_audio_output(self):
        from unittest import mock

        old_index = self.window.timbre_combo.currentIndex()
        old_detector, old_audio = self.window.detector, self.window.audio
        self.window.detector = mock.MagicMock(midi_connected=True)
        self.window.audio = None
        try:
            with mock.patch("remote_guidance.teacher.window.NoteAudioPlayer") as player:
                self.window.timbre_combo.setCurrentIndex(self.window.timbre_combo.findData("mute"))
                self.window._open_audio()
            player.assert_not_called()
            self.assertIsNone(self.window.audio)
        finally:
            self.window.detector = None
            self.window.timbre_combo.setCurrentIndex(old_index)
            self.window.detector, self.window.audio = old_detector, old_audio

    def test_teacher_audio_reuses_detector_events_and_releases_with_it(self):
        from unittest import mock

        from app.midi import MidiMessage
        from remote_guidance.teacher.live_detector import DetectionResult

        detector = mock.MagicMock()
        detector.poll.return_value = DetectionResult(
            midi_events=[
                MidiMessage("note_on", 60, 90),
                MidiMessage("note_on", 61, 0),
                MidiMessage("note_off", 62, 64),
            ]
        )
        audio = mock.MagicMock()
        old_detector, old_audio = self.window.detector, self.window.audio
        self.window.detector, self.window.audio = detector, audio
        try:
            self.window._tick()
            audio.play_key.assert_called_once_with(60)
            self.assertEqual([call.args[0] for call in audio.stop_key.call_args_list], [61, 62])
            detector.connect_midi.assert_not_called()

            self.window._close_detector()
            audio.stop_all.assert_called_once_with()
            audio.close.assert_called_once_with()
            detector.close.assert_called_once_with()
            self.assertIsNone(self.window.audio)
            self.assertIsNone(self.window.detector)
        finally:
            self.window.detector, self.window.audio = old_detector, old_audio

    def test_connect_midi_uses_the_port_saved_in_settings(self):
        from unittest import mock

        old_port = self.window.cfg.midi.port_name
        old_audio = self.window.audio
        detector = mock.MagicMock()
        detector.midi_connected = False
        detector.connect_midi.return_value = "Configured Teacher MIDI"
        self.window.cfg.midi.port_name = "Configured Teacher MIDI"
        self.window.detector = detector
        try:
            with mock.patch.object(self.window, "_open_detector", return_value=True), mock.patch(
                "remote_guidance.teacher.window.NoteAudioPlayer"
            ):
                self.assertTrue(self.window._connect_midi())
        finally:
            self.window._close_audio()
            self.window.audio = old_audio
            self.window.detector = None
            self.window.cfg.midi.port_name = old_port

        detector.connect_midi.assert_called_once_with("Configured Teacher MIDI")

    def test_arming_opens_the_devices_and_the_relay_waits_for_begin(self):
        """Camera and MIDI first and by themselves, so a hardware problem
        surfaces while no session exists - and no session is created
        until the lesson is actually begun."""
        from unittest import mock

        order = []
        self.window.room = {"room_id": "r-1", "name": "test room"}
        try:
            with mock.patch.object(
                self.window, "_open_detector", side_effect=lambda: order.append("camera") or True
            ), mock.patch.object(
                self.window, "_connect_midi", side_effect=lambda: order.append("midi") or True
            ), mock.patch.object(
                self.window, "_run_api", side_effect=lambda *a, **k: order.append("relay")
            ):
                self.window._start_live_session()
                self.assertEqual(order, ["camera", "midi"])
                self.window.student_ready = True
                with mock.patch.object(self.window, "_recording_preflight", return_value=True):
                    self.window._begin_lesson()
        finally:
            self.window.room = None
            self.window.live_armed = self.window.student_ready = False
        self.assertEqual(order, ["camera", "midi", "relay"])

    def test_live_session_request_does_not_choose_student_guidance(self):
        from unittest import mock

        old_api = getattr(self.window, "api", None)
        api = mock.MagicMock()
        self.window.api = api
        self.window.room = {"room_id": "r-1", "name": "test room"}
        try:
            with mock.patch.object(self.window, "_open_detector", return_value=True), mock.patch.object(
                self.window, "_connect_midi", return_value=True
            ), mock.patch.object(
                self.window, "_run_api", side_effect=lambda call, *_args: call()
            ), mock.patch.object(self.window, "_recording_preflight", return_value=True):
                self.window._start_live_session()
                self.window.student_ready = True
                self.window._begin_lesson()
        finally:
            self.window.room = None
            self.window.api = old_api
            self.window.live_armed = self.window.student_ready = False

        api.create_session.assert_called_once_with("r-1", {"mode": "live"})

    def test_session_start_frame_contains_control_not_student_modality(self):
        from unittest import mock

        old_session_id = self.window.session_id
        old_active, old_mode = self.window.session_active, self.window.session_mode
        old_states = tuple(
            button.isEnabled()
            for button in (self.window.live_btn, self.window.pause_btn, self.window.resume_btn, self.window.stop_btn)
        )
        try:
            with mock.patch.object(self.window, "_send") as send, mock.patch.object(
                self.window, "set_settings_enabled"
            ):
                self.window._on_session_created(
                    {"session_id": "s-1", "mode": "live", "guidance_mode": "student_choice"}
                )
        finally:
            self.window.session_id = old_session_id
            self.window.session_active, self.window.session_mode = old_active, old_mode
            for button, enabled in zip(
                (self.window.live_btn, self.window.pause_btn, self.window.resume_btn, self.window.stop_btn),
                old_states,
            ):
                button.setEnabled(enabled)
            self.window._sync_guidance_controls()

        payload = send.call_args.args[1]
        self.assertEqual(payload["mode"], "live")
        self.assertNotIn("guidance_mode", payload)

    def test_a_missing_midi_keyboard_still_lets_the_lesson_begin(self):
        """Pre-recorded playback needs no teacher hardware at all, so a
        MIDI failure must warn rather than block the session."""
        from unittest import mock

        self.window.room = {"room_id": "r-1", "name": "test room"}
        try:
            with mock.patch.object(self.window, "_open_detector", return_value=False), mock.patch.object(
                self.window, "_connect_midi", return_value=False
            ), mock.patch.object(self.window, "_run_api") as run_api, mock.patch.object(
                self.window, "_recording_preflight", return_value=True
            ):
                self.window._start_live_session()
                self.assertIn("MIDI", self.window.status_label.text())
                self.window.student_ready = True
                self.window._begin_lesson()
        finally:
            self.window.room = None
            self.window.live_armed = self.window.student_ready = False
        self.assertTrue(run_api.called, "the lesson was not created after a MIDI failure")

    def test_stopping_hands_the_devices_back(self):
        """Otherwise the camera stays claimed between lessons, which is
        the whole problem the deferred open was meant to fix."""
        from unittest import mock

        with mock.patch.object(self.window, "_send"), mock.patch.object(
            self.window, "_close_detector"
        ) as close:
            self.window._stop()
        self.assertTrue(close.called, "Stop must release the camera and the MIDI port")


class StudentWindowLayoutTests(_ClientStageChecks, unittest.TestCase):
    @classmethod
    def make_window(cls):
        from app.config import Config

        from remote_guidance.student.window import StudentRemoteWindow

        return StudentRemoteWindow(Config.load(), rgc.RemoteGuidanceConfig())

    device_attributes = ("camera", "tracker", "_vision_worker", "audio", "bridge")
    hardware_attributes = ("camera", "tracker", "_vision_worker", "audio")
    window_module = "remote_guidance.student.window"
    required_widgets = (
        "view", "table", "progress", "link_label", "status_label", "sign_in_panel", "room_panel",
        "room_label", "guidance_combo", "timeout_spin", "timbre_combo", "record_check",
        "lesson_label", "led_status", "start_btn", "stop_btn", "workspace_tabs", "practice_page",
        "results_page", "practice_tab_index", "results_tab_index", "role_label", "camera_panel", "stages",
        "stage_label", "recording_chip",
    )

    def test_practice_and_results_have_separate_workspaces(self):
        self.assertEqual(self.window.workspace_tabs.count(), 2)
        self.assertEqual(
            [self.window.workspace_tabs.tabText(i) for i in range(2)],
            ["Practice studio", "Session results"],
        )
        self.assertTrue(self.window.practice_page.isAncestorOf(self.window.guidance_combo))
        self.assertTrue(self.window.practice_page.isAncestorOf(self.window.view))
        self.assertTrue(self.window.results_page.isAncestorOf(self.window.table))
        self.assertFalse(self.window.practice_page.isAncestorOf(self.window.table))

    def test_student_has_a_compact_cool_role_header(self):
        self.assertEqual(self.window.centralWidget().objectName(), "studentCentral")
        self.assertIn("STUDENT", self.window.role_label.text())
        self.assertIn("#57c7ff", self.window.styleSheet())
        self.assertEqual(self.window.role_label.parent().objectName(), "topBar")
        self.assertIs(self.window.stage_label.parent(), self.window.role_label.parent())
        self.assertIs(self.window.link_label.parent(), self.window.role_label.parent())
        self.assertIs(self.window.settings_btn.parent(), self.window.role_label.parent())
        self.assertLessEqual(self.window.status_label.maximumHeight(), 30)

    def test_student_camera_preview_is_complete_but_bounded(self):
        self.assertEqual(self.window.view.max_size, (560, 360))
        self.assertTrue(self.window.camera_panel.isAncestorOf(self.window.view))

    def test_session_page_does_not_repeat_the_settings_midi_picker(self):
        self.assertFalse(hasattr(self.window, "port_combo"))
        self.assertFalse(hasattr(self.window, "_refresh_ports"))

    def test_ready_for_guidance_goes_off_with_the_press(self):
        """Opening the camera, MediaPipe and the MIDI port takes seconds.
        A button still lit through all of it reads as "nothing happened"
        and gets pressed again - and there is no other sign on this page
        that the student has announced itself."""
        from unittest import mock

        old = (self.window.session, self.window.cue, self.window.midi_recorder)
        cue = mock.MagicMock(enabled_channels=["visual"])
        try:
            self.window.start_btn.setEnabled(True)
            with mock.patch.object(self.window.remote, "serial_port_problems", return_value=[]), \
                    mock.patch.object(self.window, "_ensure_led"), \
                    mock.patch.object(self.window, "_ensure_camera"), \
                    mock.patch.object(self.window, "_build_cue", return_value=cue), \
                    mock.patch("remote_guidance.student.window.RawMidiRecorder"), \
                    mock.patch.object(self.window, "_open_audio"), \
                    mock.patch.object(self.window, "_send"):
                self.window._start_session()
            self.assertFalse(self.window.start_btn.isEnabled())
            self.assertTrue(self.window.stop_btn.isEnabled())
        finally:
            if self.window.session is not None:
                self.window.session.stop()
            self.window.session, self.window.cue, self.window.midi_recorder = old
            self.window.set_settings_enabled(True)
            for widget in (self.window.guidance_combo, self.window.timeout_spin, self.window.record_check):
                widget.setEnabled(True)

    def test_a_start_that_never_happened_gives_the_button_back(self):
        """The press is not what makes the session - a busy serial port,
        a camera that will not open or a missing keyboard all end it
        before there is one. The button has to come back, or the student
        cannot try again after fixing the cable."""
        from unittest import mock

        cue = mock.MagicMock(enabled_channels=["visual"])
        failures = (
            {"serial_port_problems": ["port busy"]},
            {"ensure_camera": RuntimeError("no camera")},
            {"midi": RuntimeError("no port")},
        )
        old_index = self.window.guidance_combo.currentIndex()
        try:
            # Haptic, so the serial port conflict is the student's problem.
            self.window.guidance_combo.setCurrentIndex(self.window.guidance_combo.findData("haptic"))
            for case in failures:
                self.window.start_btn.setEnabled(True)
                with mock.patch.object(
                    self.window.remote, "serial_port_problems",
                    return_value=case.get("serial_port_problems", []),
                ), mock.patch.object(self.window, "_ensure_led"), mock.patch.object(
                    self.window, "_ensure_camera", side_effect=case.get("ensure_camera")
                ), mock.patch.object(
                    self.window, "_build_cue", return_value=cue
                ), mock.patch(
                    "remote_guidance.student.window.RawMidiRecorder", side_effect=case.get("midi")
                ), mock.patch(
                    "remote_guidance.student.window.QMessageBox.warning"
                ) as warning:
                    self.window._start_session()
                self.assertTrue(warning.called, case)
                self.assertIsNone(self.window.session, case)
                self.assertTrue(self.window.start_btn.isEnabled(), case)
        finally:
            self.window.guidance_combo.setCurrentIndex(old_index)

    def test_the_student_is_told_while_it_is_recording(self):
        """Same reason as the teacher's: the one line saying a writer is
        open scrolls away, and the student cannot tell a recorded lesson
        from an unrecorded one while playing it."""
        self.window._set_recording_indicator("remote-1760000000")
        self.assertFalse(self.window.recording_chip.isHidden())
        self.assertIn("remote-1760000000", self.window.recording_chip.text())
        self.assertIn("RECORDING", self.window.recording_chip.text())
        self.window._set_recording_indicator()
        self.assertTrue(self.window.recording_chip.isHidden())

    def test_student_offers_the_quiz_timbres(self):
        from note_audio import DEFAULT_TIMBRE, TIMBRES

        choices = [self.window.timbre_combo.itemData(i) for i in range(self.window.timbre_combo.count())]
        self.assertEqual(choices, list(TIMBRES))
        self.assertEqual(self.window.timbre_combo.currentData(), DEFAULT_TIMBRE)

    def test_student_mute_does_not_claim_the_audio_output(self):
        from unittest import mock

        old_index = self.window.timbre_combo.currentIndex()
        old_recorder, old_audio = self.window.midi_recorder, self.window.audio
        self.window.midi_recorder = mock.MagicMock()
        self.window.audio = None
        try:
            with mock.patch("remote_guidance.student.window.NoteAudioPlayer") as player:
                self.window.timbre_combo.setCurrentIndex(self.window.timbre_combo.findData("mute"))
                self.window._open_audio()
            player.assert_not_called()
            self.assertIsNone(self.window.audio)
        finally:
            self.window.midi_recorder = None
            self.window.timbre_combo.setCurrentIndex(old_index)
            self.window.midi_recorder, self.window.audio = old_recorder, old_audio

    def test_student_audio_reuses_recorder_events_and_releases(self):
        from types import SimpleNamespace
        from unittest import mock

        events = [
            SimpleNamespace(type="note_on", note=64, abs_time=12.0),
            SimpleNamespace(type="note_off", note=64, abs_time=12.2),
        ]
        recorder = mock.MagicMock()
        recorder.pop_events.return_value = events
        audio = mock.MagicMock()
        session = mock.MagicMock()
        old = (
            self.window.midi_recorder,
            self.window.audio,
            self.window.session,
            self.window.scheduler,
            self.window.raw_events,
        )
        self.window.midi_recorder = recorder
        self.window.audio = audio
        self.window.session = session
        self.window.scheduler = None
        self.window.raw_events = []
        try:
            self.window._tick()
            audio.play_key.assert_called_once_with(64)
            audio.stop_key.assert_called_once_with(64)
            session.on_note.assert_called_once_with(64, wall_time_ns=12_000_000_000)
            self.assertEqual(self.window.raw_events, events)

            self.window._close_audio()
            audio.stop_all.assert_called_once_with()
            audio.close.assert_called_once_with()
            self.assertIsNone(self.window.audio)
        finally:
            (
                self.window.midi_recorder,
                self.window.audio,
                self.window.session,
                self.window.scheduler,
                self.window.raw_events,
            ) = old

    def test_student_presents_queued_guidance_before_vision_work(self):
        from unittest import mock

        order = []
        camera = mock.MagicMock()
        camera.read.side_effect = lambda: order.append("vision") or None
        session = mock.MagicMock()
        session.tick.side_effect = lambda: order.append("session")
        old = (
            self.window.camera,
            self.window.tracker,
            self.window._vision_worker,
            self.window.midi_recorder,
            self.window.session,
            self.window.scheduler,
        )
        self.window.camera = camera
        self.window.tracker = None
        self.window._vision_worker = None
        self.window.midi_recorder = None
        self.window.session = session
        self.window.scheduler = None
        try:
            self.window._tick()
            self.assertEqual(order, ["session", "vision"])
        finally:
            (
                self.window.camera,
                self.window.tracker,
                self.window._vision_worker,
                self.window.midi_recorder,
                self.window.session,
                self.window.scheduler,
            ) = old

    def test_a_live_cue_reaches_the_session_through_accept(self):
        """The dispatcher's whole job for guidance.live: hand the envelope
        to StudentSession. It must not present the cue itself - accept()
        is what stamps arrival and acknowledges before any cue work, and
        the recorded path relies on going through the same door."""
        from unittest import mock

        from remote_guidance.protocol import TYPE_GUIDANCE_LIVE

        envelope = make_envelope(
            TYPE_GUIDANCE_LIVE,
            payload=guidance_payload([GuidanceAction(note=60, finger="R1")], timeout_s=4.0),
        )
        session = mock.MagicMock()
        old_session = self.window.session
        self.window.session = session
        try:
            self.window._on_message(envelope)
        finally:
            self.window.session = old_session

        session.accept.assert_called_once_with(envelope)

    def test_a_cue_arriving_before_ready_is_reported_not_dropped(self):
        from unittest import mock

        from remote_guidance.protocol import TYPE_GUIDANCE_LIVE

        envelope = make_envelope(
            TYPE_GUIDANCE_LIVE, payload=guidance_payload([GuidanceAction(note=60, finger="R1")])
        )
        old_session = self.window.session
        self.window.session = None
        try:
            with mock.patch.object(self.window, "_set_status") as status:
                self.window._on_message(envelope)  # must not raise
        finally:
            self.window.session = old_session

        status.assert_called_once()
        self.assertIn("ready", status.call_args.args[0].lower())

    def test_session_start_uses_the_port_saved_in_settings(self):
        from unittest import mock

        old_port = self.window.cfg.midi.port_name
        self.window.cfg.midi.port_name = "Configured Student MIDI"
        cue = mock.MagicMock()
        try:
            with mock.patch.object(self.window, "_ensure_led"), mock.patch.object(
                self.window, "_ensure_camera"
            ), mock.patch.object(self.window, "_build_cue", return_value=cue), mock.patch(
                "remote_guidance.student.window.RawMidiRecorder", side_effect=RuntimeError("test stop")
            ) as recorder, mock.patch("remote_guidance.student.window.QMessageBox"):
                self.window._start_session()
        finally:
            self.window.cfg.midi.port_name = old_port

        recorder.assert_called_once_with("Configured Student MIDI")
        self.assertIsNone(self.window.session)

    def test_ready_message_reports_the_students_guidance_choice(self):
        from unittest import mock

        old_session, old_cue, old_name = self.window.session, self.window.cue, self.window.session_name
        self.window.session = mock.MagicMock()
        self.window.cue = mock.MagicMock(enabled_channels=["visual", "led"])
        self.window.session_name = "student-owned-mode"
        try:
            with mock.patch.object(self.window, "_send") as send:
                self.window._announce_ready()
        finally:
            self.window.session, self.window.cue, self.window.session_name = old_session, old_cue, old_name

        payload = send.call_args.args[1]
        self.assertEqual(payload["guidance_mode"], self.window.guidance_combo.currentData())

    def test_recorded_trigger_waits_if_student_has_not_pressed_ready(self):
        from unittest import mock

        old_session = self.window.session
        old_pending = self.window._pending_recording_start
        envelope = {
            "session_id": "s-recorded",
            "payload": {"recording_id": "rec-1", "playback_mode": "paced"},
        }
        self.window.session = None
        try:
            self.window._on_recording_start(envelope)
            self.assertIs(self.window._pending_recording_start, envelope)
            self.assertIn("Ready for guidance", self.window.status_label.text())

            self.window.session = mock.MagicMock()
            with mock.patch.object(self.window, "_on_recording_start") as replay:
                self.window._play_pending_recording_if_ready()
            replay.assert_called_once_with(envelope)
            self.assertIsNone(self.window._pending_recording_start)
        finally:
            self.window.session = old_session
            self.window._pending_recording_start = old_pending

    def test_the_led_strip_connects_itself_when_a_session_starts(self):
        """There is no LED port to pick anywhere in the UI, so starting a
        session has to be what connects it - and a strip that is not
        found must warn rather than refuse to run."""
        from unittest import mock

        with mock.patch.object(self.window, "_ensure_led") as ensure_led, mock.patch.object(
            self.window, "_ensure_camera", side_effect=RuntimeError("no camera in a test")
        ), mock.patch("remote_guidance.student.window.QMessageBox"):
            self.window._start_session()
        self.assertTrue(ensure_led.called, "the LED strip was not connected at the start of a session")
        self.assertIsNone(self.window.session)

    def test_a_missing_led_strip_only_warns(self):
        from unittest import mock

        with mock.patch.object(self.window, "_connect_led", return_value="no such port"):
            self.window._ensure_led()
        self.assertFalse(self.window.led_connected)
        self.assertIn("LED strip not found", self.window.status_label.text())

    def _remote_session_start(self, name="remote-1770000000"):
        return {"session_id": "s-live", "payload": {"mode": "live", "session_name": name}}

    def test_the_session_is_named_by_the_teacher_and_only_there(self):
        """Both machines then hold one lesson under one name from the
        first frame: data/music/remote-<epoch>/ on the teacher,
        data/quiz/remote-<epoch>/ here. Nothing is renamed afterwards."""
        from unittest import mock

        old = (self.window.session, self.window.session_name)
        self.window.session = mock.MagicMock()
        try:
            with mock.patch.object(self.window, "_send"), mock.patch.object(
                self.window, "_schedule_recording"
            ) as scheduled:
                self.window._on_remote_session_start(self._remote_session_start())
            scheduled.assert_called_once()
            self.assertEqual(scheduled.call_args[0][0], "remote-1770000000")
        finally:
            self.window.session, self.window.session_name = old

    def test_this_client_never_invents_a_session_name(self):
        """The name outliving the session that owned it is what let a
        second lesson be recorded into the first one's folder: the
        student reused the previous lesson's name, opened its video
        writer over the previous video, and renamed the folder at the
        end. There is now nowhere for a name to come from but the
        teacher."""
        import inspect

        from remote_guidance.student.window import StudentRemoteWindow

        for gone in (
            "_resolve_session_name", "_adopt_teacher_session_name",
            "_rename_to_teacher_session_name", "_final_session_name",
        ):
            self.assertFalse(hasattr(StudentRemoteWindow, gone), f"{gone}() should be gone")
        self.assertNotIn("remote-", inspect.getsource(StudentRemoteWindow._start_session))

    def test_ready_for_guidance_opens_devices_but_does_not_record(self):
        import inspect

        from remote_guidance.student.window import StudentRemoteWindow

        source = inspect.getsource(StudentRemoteWindow._start_session)
        self.assertNotIn("_start_recording", source)
        for device in ("_ensure_led", "_ensure_camera", "RawMidiRecorder", "_build_cue"):
            self.assertIn(device, source, f"Ready for guidance should still open {device}")

    def test_recording_waits_for_the_instant_the_teacher_named(self):
        """Both ends count the same seconds off one wall stamp, so the
        two writers open together rather than each on arrival."""
        from unittest import mock

        from remote_guidance.timing import wall_ns

        old = self.window.session_name
        self.window.session = mock.MagicMock()
        try:
            with mock.patch.object(self.window, "_begin_recording") as begun:
                self.window._schedule_recording("remote-1770000000", wall_ns() + int(3e9))
            begun.assert_not_called()
            self.assertEqual(self.window.session_name, "remote-1770000000")
            self.assertIsNotNone(self.window._countdown_timer)
        finally:
            if self.window._countdown_timer is not None:
                self.window._countdown_timer.stop()
                self.window._countdown_timer = None
            self.window.session = None
            self.window.session_name = old

    def test_recording_starts_at_once_when_no_instant_is_given(self):
        from unittest import mock

        old = self.window.session_name
        self.window.session = mock.MagicMock()
        try:
            with mock.patch.object(self.window, "_begin_recording") as begun:
                self.window._schedule_recording("remote-1770000000", None)
            begun.assert_called_once_with("remote-1770000000")
        finally:
            self.window.session = None
            self.window.session_name = old

    required_widgets = (
        "view", "table", "progress", "link_label", "status_label", "sign_in_panel", "room_panel",
        "room_label", "guidance_combo", "timeout_spin", "record_check", "lesson_label",
        "timbre_combo", "led_status", "start_btn", "stop_btn", "stages", "stage_label",
    )


class RelayPortDefaultTests(unittest.TestCase):
    """One port number, three separate defaults.

    The clients' default URL, the launcher's local-server port and the
    relay's own bind port live in two packages that deliberately do not
    import each other (rule 4.5 - server/ must stay copyable on its own),
    so nothing but a test can keep them in step. A mismatch is silent:
    the relay starts, and every client fails to connect to a port with
    nothing on it."""

    expected_port = 18765

    def test_the_client_and_server_defaults_agree(self):
        from server.config import ServerConfig

        self.assertEqual(ServerConfig().port, self.expected_port)
        self.assertEqual(rgc.LocalServerConfig().port, self.expected_port)
        self.assertEqual(rgc.NetworkConfig().server_url, f"http://127.0.0.1:{self.expected_port}")

    def test_the_example_server_config_uses_the_same_port(self):
        example = json.loads((PROJECT_ROOT / "server" / "server_config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["port"], self.expected_port)


class RelayServerWindowLayoutTests(unittest.TestCase):
    """The relay dashboard must stay usable without occupying most of a
    laptop display. Its complete content lives in one scroll area, while
    the rooms tree and log retain their own scrolling."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        from server.config import ServerConfig
        from server.gui import ServerControlPanel

        cls.app = QApplication.instance() or QApplication([])
        cls.window = ServerControlPanel(ServerConfig())
        cls.window.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()

    def test_the_default_window_is_compact(self):
        from server.gui import DEFAULT_WINDOW_HEIGHT, DEFAULT_WINDOW_WIDTH

        self.assertEqual(self.window.width(), DEFAULT_WINDOW_WIDTH)
        self.assertEqual(self.window.height(), DEFAULT_WINDOW_HEIGHT)
        self.assertEqual(DEFAULT_WINDOW_WIDTH, 440)
        self.assertEqual(DEFAULT_WINDOW_HEIGHT, 360)

    def test_the_complete_dashboard_is_in_a_resizable_scroll_area(self):
        from PySide6.QtWidgets import QScrollArea

        self.assertIsInstance(self.window.scroll_area, QScrollArea)
        self.assertTrue(self.window.scroll_area.widgetResizable())
        self.assertIs(self.window.scroll_area.widget(), self.window.content_widget)
        self.assertGreater(
            self.window.scroll_area.verticalScrollBar().maximum(),
            0,
            "the compact default size should scroll instead of hiding or removing dashboard content",
        )

    def test_no_existing_dashboard_control_was_removed(self):
        for name in (
            "host_label",
            "port_label",
            "registration_label",
            "db_label",
            "http_label",
            "ws_label",
            "tls_label",
            "state_label",
            "start_btn",
            "stop_btn",
            "settings_btn",
            "clear_log_btn",
            "rooms_tree",
            "log_view",
        ):
            self.assertTrue(hasattr(self.window, name), f"relay dashboard lost {name}")


class LauncherActionTests(unittest.TestCase):
    """No hardware and no server: only which command each Tele-training
    button would run."""

    def setUp(self):
        from remote_guidance import launcher_actions

        self.actions = launcher_actions
        self.remote = rgc.RemoteGuidanceConfig()

    def test_every_entry_point_script_exists(self):
        for spec in (self.actions.student_spec(), self.actions.teacher_spec(), self.actions.benchmark_spec()):
            self.assertIsNone(spec.missing_script(), f"{spec.label} points at a missing script")

    def test_specs_use_the_running_interpreter(self):
        self.assertEqual(self.actions.student_spec().program, sys.executable)

    def test_server_spec_carries_host_and_port(self):
        self.remote.local_server.host = "0.0.0.0"
        self.remote.local_server.port = 9100
        spec = self.actions.server_spec(self.remote, gui=False)
        self.assertEqual(spec.arguments[:2], ["-m", "server"])
        self.assertIn("0.0.0.0", spec.arguments)
        self.assertIn("9100", spec.arguments)
        self.assertNotIn("--gui", spec.arguments)
        self.assertIn("--gui", self.actions.server_spec(self.remote, gui=True).arguments)

    def test_benchmark_spec_opens_the_gui_wrapper(self):
        self.assertIn("--gui", self.actions.benchmark_spec().arguments)

    def test_health_url_targets_the_configured_relay(self):
        self.remote.network.server_url = "http://relay.local:8765"
        self.remote.local_server.port = 8765
        self.assertEqual(self.actions.health_url(self.remote), "http://relay.local:8765/api/v1/health")

    def test_health_check_on_a_dead_port_returns_none(self):
        self.assertIsNone(self.actions.check_health("http://127.0.0.1:9/api/v1/health", timeout=0.2))

    def test_launcher_section_eight_actions_all_resolve(self):
        """Every Tele-training button must name a method that exists on
        the launcher window - a typo here would only show up as a
        crash when a user clicks it."""
        import launcher as launcher_module

        section = next(s for s in launcher_module.SECTIONS if s[0].startswith("8."))
        entries = [(label, entry) for label, entry in section[1]]
        self.assertTrue(entries, "section 8 is empty")

        process_entries = [
            (label, entry) for label, entry in entries if isinstance(entry, launcher_module.ProcessEntry)
        ]
        self.assertEqual(len(process_entries), 4)
        for label, entry in process_entries:
            self.assertTrue(
                hasattr(launcher_module.LauncherWindow, entry.action),
                f"{label!r} names a missing action {entry.action!r}",
            )

    def test_section_eight_is_four_processes_plus_the_setup_wizard(self):
        """No *settings* window in the launcher: each of the three
        endpoints owns its settings and has its own Settings button, so a
        launcher entry showing all three roles' devices at once is
        exactly what was removed.

        The setup wizard is not that and is deliberately here instead of
        in a client: calibrating claims the camera, and a client may be
        holding it. It is an ordinary sub-window, so the launcher's
        one-tool-at-a-time rule keeps the two apart."""
        import launcher as launcher_module

        from remote_guidance.setup_wizard import RemoteSetupWizard

        section = next(s for s in launcher_module.SECTIONS if s[0].startswith("8."))
        labels = [label for label, _ in section[1]]
        non_process = [
            (label, entry)
            for label, entry in section[1]
            if not isinstance(entry, launcher_module.ProcessEntry)
        ]
        self.assertEqual(
            non_process,
            [("Tele-training Setup Wizard", RemoteSetupWizard)],
            f"section 8 gained an unexpected non-process entry: {labels}",
        )
        self.assertFalse(
            any("setting" in label.lower() for label in labels),
            f"the launcher must not offer remote settings any more: {labels}",
        )

    def test_the_relay_entry_is_just_called_relay_server(self):
        import launcher as launcher_module

        labels = [label for label, _ in next(s for s in launcher_module.SECTIONS if s[0].startswith("8."))[1]]
        self.assertIn("Relay Server", labels)
        self.assertFalse(any("Control Panel" in label for label in labels))
        self.assertEqual(self.actions.server_spec(self.remote, gui=True).label, "Relay Server")

    def test_launch_local_stack_is_gone(self):
        """Removed on request. The three endpoints are started
        individually; nothing should be left referring to the combined
        action, or the button would come back as a dead one."""
        import launcher as launcher_module

        from remote_guidance import launcher_actions

        labels = [label for label, _ in next(s for s in launcher_module.SECTIONS if s[0].startswith("8."))[1]]
        self.assertNotIn("Launch Local Stack (server + student + teacher)", labels)
        self.assertFalse(any("Local Stack" in label for label in labels))
        self.assertFalse(hasattr(launcher_module.LauncherWindow, "launch_local_stack"))
        self.assertFalse(hasattr(launcher_actions, "plan_local_stack"))
        self.assertFalse(hasattr(launcher_actions, "StackPlan"))

    def test_launcher_columns_are_balanced(self):
        """Sections used to be dealt out round-robin, which put both
        six-button sections and Tele-training in the middle column and
        let it set the window height on its own."""
        import launcher as launcher_module

        weights = [launcher_module.section_weight(tools) for _, tools in launcher_module.SECTIONS]
        groups = launcher_module.balance_columns(weights, launcher_module.SECTION_COLUMNS)

        self.assertEqual(sum(len(g) for g in groups), len(launcher_module.SECTIONS), "a section went missing")
        self.assertEqual(
            [i for g in groups for i in g],
            list(range(len(launcher_module.SECTIONS))),
            "sections must stay in their numbered order, reading down each column",
        )

        heights = [sum(weights[i] for i in g) for g in groups]
        round_robin = [
            sum(weights[i] for i in range(len(weights)) if i % 3 == c) for c in range(3)
        ]
        self.assertLess(
            max(heights),
            max(round_robin),
            f"the balanced layout ({max(heights):.1f}) is no shorter than round-robin ({max(round_robin):.1f})",
        )

    def test_balance_columns_handles_awkward_inputs(self):
        import launcher as launcher_module

        balance = launcher_module.balance_columns
        self.assertEqual(balance([], 3), [])
        # More columns than sections: no empty column is produced.
        groups = balance([1.0, 2.0], 5)
        self.assertEqual([i for g in groups for i in g], [0, 1])
        self.assertTrue(all(g for g in groups))
        # One column keeps everything together, in order.
        self.assertEqual(balance([1.0, 2.0, 3.0], 1), [[0, 1, 2]])

    def test_balance_columns_finds_the_optimum(self):
        """Hand-checked case: the only way to keep every run at 3 is to
        cut after the first and third entries."""
        self.assertEqual(
            __import__("launcher").balance_columns([3.0, 1.0, 2.0, 3.0], 3),
            [[0], [1, 2], [3]],
        )

    def test_launcher_still_lists_every_original_section(self):
        """Adding the remote tools must not disturb the existing ones."""
        import launcher as launcher_module

        titles = [title for title, _ in launcher_module.SECTIONS]
        for expected in (
            "1. Initial Setup",
            "2. Feature Testing",
            "3. Recording && Playback",
            "5. Practice && Assessment",
            "6. Main User Study",
            "7. Data Analysis",
            "10. Validation Experiments",
        ):
            self.assertIn(expected, titles)


class StudentRecordingGuardTests(unittest.TestCase):
    """A session that recorded nothing must say so before the next one
    starts.

    The failure this guards against is not a crash: the writer is opened,
    the lesson runs, results are saved and sent, and only the video is
    missing. Everything downstream still looks like a finished session,
    so the loss is invisible until somebody opens the folder to review
    the fingers by hand - by which time the lesson is over and cannot be
    played again."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _window(self, quiz_root, frames_written):
        """A StudentRemoteWindow with its quiz folder redirected into a
        temporary tree. Only _recording_state() is exercised, so no
        device is opened and no session is run."""
        from unittest import mock

        from app.config import Config

        from remote_guidance.student.window import StudentRemoteWindow

        window = StudentRemoteWindow(Config.load(), rgc.RemoteGuidanceConfig())
        self.addCleanup(window.close)
        window.session_name = "remote-1"
        window._frames_written = frames_written
        window.record_check.setChecked(True)
        root = Path(quiz_root)
        patches = [
            mock.patch("remote_guidance.student.window.quiz_dir", lambda name: root / name),
            mock.patch("remote_guidance.student.window.quiz_raw_dir", lambda name: root / name / "raw"),
            # The session log is deliberately outside every session
            # folder, which also puts it outside the redirect above: a
            # test run must not append to the live data/quiz/ log.
            mock.patch("remote_guidance.student.window.QUIZ_DATA_DIR", root),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return window, root

    def test_the_session_log_follows_the_quiz_folder_under_test(self):
        """Otherwise a test run writes into the live data/quiz/ log - and
        the whole point of that log is to be a trustworthy record of what
        real sessions did."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            window, root = self._window(directory, frames_written=0)
            raw = root / "remote-1" / "raw"
            raw.mkdir(parents=True)
            (raw / "performance.mp4").write_bytes(b"already here")
            with mock.patch("remote_guidance.student.window.QMessageBox"):
                window._start_recording("remote-1")
            self.assertTrue((root / "_sessions.log").exists(), "the log did not follow the patched root")

    def test_a_folder_that_was_never_created_is_a_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            window, _ = self._window(directory, frames_written=100)
            problem, note = window._recording_state()
            self.assertIn("never created", problem)
            self.assertIn("FOLDER MISSING", note)

    def test_a_missing_video_is_a_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            window, root = self._window(directory, frames_written=100)
            (root / "remote-1" / "raw").mkdir(parents=True)
            problem, note = window._recording_state()
            self.assertIn("does not exist", problem)
            self.assertIn("VIDEO MISSING", note)

    def test_a_zero_byte_video_is_a_problem(self):
        """What an unopened cv2.VideoWriter leaves behind: every write()
        is swallowed and the file is still there, at zero bytes."""
        with tempfile.TemporaryDirectory() as directory:
            window, root = self._window(directory, frames_written=100)
            raw = root / "remote-1" / "raw"
            raw.mkdir(parents=True)
            (raw / "performance.mp4").touch()
            problem, note = window._recording_state()
            self.assertIn("empty", problem)
            self.assertIn("VIDEO EMPTY", note)

    def test_a_video_nothing_was_written_into_is_a_problem(self):
        """A camera that opens and then produces no frames: _tick() never
        reaches _write_video_frame(), so the file has a header and
        nothing else."""
        with tempfile.TemporaryDirectory() as directory:
            window, root = self._window(directory, frames_written=0)
            raw = root / "remote-1" / "raw"
            raw.mkdir(parents=True)
            (raw / "performance.mp4").write_bytes(b"\x00" * 1024)
            problem, note = window._recording_state()
            self.assertIn("no frames", problem)
            self.assertIn("NO FRAMES", note)

    def test_an_existing_recording_is_never_reopened(self):
        """cv2.VideoWriter truncates whatever is at the path it is given.
        A lesson recorded over another lesson cannot be recovered, so the
        writer is not opened at all rather than opened over something."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            window, root = self._window(directory, frames_written=0)
            raw = root / "remote-1" / "raw"
            raw.mkdir(parents=True)
            existing = raw / "performance.mp4"
            existing.write_bytes(b"an earlier lesson" * 64)
            before = existing.read_bytes()

            with mock.patch.object(window, "_confirm_without_video") as asked, mock.patch(
                "remote_guidance.student.window.QMessageBox"
            ) as box:
                started = window._start_recording("remote-1")
            self.assertTrue(box.critical.called, "refusing silently is what caused the loss")

            self.assertFalse(started, "an existing recording must stop this session recording")
            asked.assert_not_called()
            self.assertIsNone(window.video_writer)
            self.assertEqual(existing.read_bytes(), before, "the earlier recording was touched")

    def test_a_real_recording_is_not_a_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            window, root = self._window(directory, frames_written=900)
            raw = root / "remote-1" / "raw"
            raw.mkdir(parents=True)
            (raw / "performance.mp4").write_bytes(b"\x00" * 4096)
            problem, note = window._recording_state()
            self.assertEqual(problem, "")
            self.assertIn("saved", note)

    def test_finish_session_checks_the_recording_and_start_session_can_abort(self):
        """The checks are only worth anything if they are wired in: a
        _verify_recording() nobody calls, or a _start_recording() whose
        refusal is ignored, would pass every test above."""
        import ast
        import inspect
        import textwrap

        from remote_guidance.student.window import StudentRemoteWindow

        def calls(method):
            tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
            return {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            }

        self.assertIn("_verify_recording", calls(StudentRemoteWindow._finish_session))
        # Recording is begun by the teacher, not by pressing Ready.
        self.assertNotIn("_start_recording", calls(StudentRemoteWindow._start_session))
        self.assertIn("_start_recording", calls(StudentRemoteWindow._begin_recording))

    def test_saving_never_depends_on_analysing(self):
        """"Do not analyse it" must never mean "do not keep it". The
        offline pass runs after the folder is written and reads the video
        it was given; nothing on that path may remove or rewrite it."""
        import inspect

        from remote_guidance.student import window as student_window

        for name in ("_run_final_pass", "_on_final_matches", "_on_analyze_failed"):
            source = inspect.getsource(getattr(student_window.StudentRemoteWindow, name))
            for forbidden in ("unlink", "rmtree", "os.remove", "VideoWriter"):
                self.assertNotIn(
                    forbidden, source, f"{name}() must not {forbidden} - it only reads the recording"
                )


class StudentRecordsTheCameraFrameTests(unittest.TestCase):
    """The student's recording has to be what the local quiz's is: the
    camera's frame, at the camera's rate. The offline finger pass reads
    it back with MediaPipe, which cannot find hands under a drawing of
    the hands."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_the_tick_writes_the_unannotated_frame(self):
        import numpy as np

        from app.config import Config

        from remote_guidance.student.window import StudentRemoteWindow
        from remote_guidance.vision_worker import VisionSnapshot

        raw = np.zeros((4, 4, 3), dtype=np.uint8)
        overlaid = np.full((4, 4, 3), 255, dtype=np.uint8)

        class Worker:
            def snapshot(self):
                return VisionSnapshot(sequence=1, frame=overlaid, hands={}, raw_frame=raw)

            def close(self, timeout=3.0):
                pass

        written = []

        class Writer:
            def write(self, frame):
                written.append(frame)

            def release(self):
                pass

        window = StudentRemoteWindow(Config.load(), rgc.RemoteGuidanceConfig())
        self.addCleanup(window.close)
        window._vision_worker = Worker()
        window.video_writer = Writer()
        window.video_start_time = time.time()
        window._video_fps = 30
        window._frames_written = 0

        window._tick()

        self.assertTrue(written, "_tick() wrote no frame")
        for frame in written:
            self.assertTrue(
                np.array_equal(frame, raw),
                "the annotated preview frame was recorded instead of the camera's",
            )

    def test_the_worker_is_asked_for_the_raw_frame(self):
        """keep_raw is what makes snapshot() carry it at all - without it
        the tick falls back to the annotated frame and the drawing is
        back in the recording."""
        import ast
        import inspect
        import textwrap

        from remote_guidance.student.window import StudentRemoteWindow

        tree = ast.parse(textwrap.dedent(inspect.getsource(StudentRemoteWindow._ensure_camera)))
        keywords = [
            keyword
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "keep_raw"
        ]
        self.assertTrue(keywords, "the student's vision worker must be built with keep_raw")
        self.assertTrue(all(k.value.value is True for k in keywords))


class StudentSessionIdTests(unittest.TestCase):
    """Which lesson a message belongs to.

    "Ready for guidance" is normally pressed before the teacher opens the
    session, so the id the client is holding at that moment is the
    *previous* lesson's - and the relay, seeing a valid id, filed this
    lesson's modality against that one. The offline finger pass has the
    mirror-image problem from the other end: it reports minutes later,
    when the current id may already belong to the next lesson."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from app.config import Config

        from remote_guidance.student.window import StudentRemoteWindow

        window = StudentRemoteWindow(Config.load(), rgc.RemoteGuidanceConfig())
        self.addCleanup(window.close)

        sent = []

        class _Bridge:
            session_id = None

            def send(self, type_, payload=None, session_id=None, message_id=None):
                sent.append((type_, session_id))

            def stop(self):
                """Called by leave_session() when the window closes."""

        window.bridge = _Bridge()
        return window, sent

    def test_a_message_defaults_to_the_current_session(self):
        window, sent = self._window()
        window.session_id = "current"
        window._send("recording.ready", {})
        self.assertEqual(sent, [("recording.ready", "current")])

    def test_an_explicit_session_id_wins(self):
        """What the finger pass uses: the lesson it analysed, not
        whichever one happens to be open when it finishes."""
        window, sent = self._window()
        window.session_id = "next-lesson"
        window._send("session.finished", {}, session_id="the-one-analysed")
        self.assertEqual(sent, [("session.finished", "the-one-analysed")])

    def test_starting_a_session_drops_the_previous_id_before_announcing(self):
        import ast
        import inspect
        import textwrap

        from remote_guidance.student.window import StudentRemoteWindow

        tree = ast.parse(textwrap.dedent(inspect.getsource(StudentRemoteWindow._start_session)))
        cleared = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "session_id" and isinstance(t.value, ast.Name)
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
        ]
        self.assertTrue(cleared, "_start_session() must clear session_id - a stale id mislabels the relay")

        announced = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_announce_ready"
        ]
        self.assertTrue(announced, "_start_session() no longer announces readiness - did it move?")
        self.assertLess(
            min(cleared), min(announced), "the id must be cleared before readiness is announced"
        )

    def test_the_final_pass_carries_the_session_it_analysed(self):
        import inspect

        from remote_guidance.student.window import StudentRemoteWindow

        for name in ("_run_final_pass", "_on_final_matches"):
            parameters = inspect.signature(getattr(StudentRemoteWindow, name)).parameters
            self.assertIn("session_id", parameters, f"{name}() must be told which session it is reporting on")


class StaleSyncAlignmentTests(unittest.TestCase):
    """A saved alignment outranks everything, which is right for a manual
    correction and catastrophic for one a *different* recording left
    behind.

    When a session folder was reused (REMOTE_GUIDANCE.md trap 11) the
    previous session's sync_align.json survived into it, 145s adrift of a
    40s video. Every event mapped to a frame index past the end, no hands
    were found there, and the pass returned "no finger" for the whole
    session - with 100% key accuracy, no error, and nothing in any log."""

    def _sync(self, video_start_time):
        from app.music_recording import SyncInfo

        return SyncInfo(
            video_start_time=video_start_time,
            midi_start_time=video_start_time,
            led_on_time=video_start_time + 0.3,
            led_off_time=video_start_time + 1.1,
        )

    def _anchor(self, frame0_epoch, method="auto"):
        from app.sync_led import SyncAnchor

        return SyncAnchor(method=method, fps=30.0, frame0_epoch=frame0_epoch)

    def test_an_alignment_from_another_recording_is_rejected(self):
        from unittest import mock

        from app.sync_led import alignment_belongs_to

        with mock.patch("app.sync_led._video_duration_s", return_value=40.4):
            self.assertFalse(
                alignment_belongs_to(self._anchor(1786985994.5), self._sync(1786986139.7), Path("v.mp4"))
            )

    def test_this_recordings_own_alignment_is_kept(self):
        """The LED flash resolves a real sub-second offset, and frame 0's
        content legitimately predates video_start_time. Rejecting that
        would throw away every genuine alignment."""
        from unittest import mock

        from app.sync_led import alignment_belongs_to

        start = 1786986139.7
        with mock.patch("app.sync_led._video_duration_s", return_value=40.4):
            for offset in (-0.55, -0.2, 0.0, 0.3, 1.0):
                self.assertTrue(
                    alignment_belongs_to(self._anchor(start + offset), self._sync(start), Path("v.mp4")),
                    f"a {offset:+.2f}s alignment is normal and must not be rejected",
                )

    def test_a_manual_alignment_well_inside_the_recording_is_kept(self):
        """Manual picking can legitimately land anywhere in the video."""
        from unittest import mock

        from app.sync_led import alignment_belongs_to

        start = 1786986139.7
        with mock.patch("app.sync_led._video_duration_s", return_value=40.4):
            self.assertTrue(
                alignment_belongs_to(self._anchor(start - 39.0, "manual"), self._sync(start), Path("v.mp4"))
            )

    def test_an_unreadable_video_falls_back_to_the_tighter_margin(self):
        from unittest import mock

        from app.sync_led import alignment_belongs_to

        start = 1786986139.7
        with mock.patch("app.sync_led._video_duration_s", return_value=None):
            self.assertTrue(alignment_belongs_to(self._anchor(start + 1.0), self._sync(start), Path("v.mp4")))
            self.assertFalse(alignment_belongs_to(self._anchor(start + 600), self._sync(start), Path("v.mp4")))

    def test_a_rejected_alignment_makes_the_resolver_re_detect(self):
        """Rejecting it is only half the fix - the analysis has to go on
        and produce a usable anchor from this recording."""
        from unittest import mock

        from app import sync_led

        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            video = raw / "performance.mp4"
            video.write_bytes(b"")
            self._anchor(1786985994.5).save(raw / sync_led.ALIGN_FILENAME)
            fresh = self._anchor(1786986139.9, "led")
            with mock.patch.object(sync_led, "_video_duration_s", return_value=40.4), mock.patch.object(
                sync_led, "compute_sync_anchor", return_value=fresh
            ) as computed:
                anchor = sync_led.resolve_sync_anchor(video, self._sync(1786986139.7), "a-profile")
            computed.assert_called_once()
            self.assertEqual(anchor.frame0_epoch, fresh.frame0_epoch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
