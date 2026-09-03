"""Tests for the saved-song side of app/music_recording.py, and for the
two Song Recording Wizards that write it.

The one thing here worth a test file of its own is the lead-in. Every
stored time in this project is an absolute `time.time()` value, and the
length of a saved song is an *elapsed* one - so the code that turns the
first note's timestamp into "how long before the playing started" is a
place where the two get mixed up silently. It did: after the July 2026
epoch-timestamp migration both wizards subtracted an epoch stamp
(~1.79e9) from an elapsed duration and clamped at zero, so every song
recorded since carried `duration_s: 0.0` in its meta.json. Nothing
crashed and nothing said so.

No camera, MIDI device, serial port or MediaPipe pass is touched, and
every song is written into a temporary directory - never data/music/.

Run from main/:  python -m pytest test-script/test_music_recording.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.music_recording import (  # noqa: E402
    RawMidiEvent,
    SongMeta,
    first_note_on_time,
    lead_in_seconds,
    trim_to_first_note,
)

# A twelve-second capture: the sync flash and four seconds of getting
# ready, then eight seconds of playing.
RECORDING_START = 1_790_000_000.0
CAPTURED_S = 12.0
PLAYED_S = 8.0
EVENTS = [
    RawMidiEvent(abs_time=RECORDING_START + 4.0, type="note_on", note=60, velocity=100),
    RawMidiEvent(abs_time=RECORDING_START + 4.5, type="note_off", note=60, velocity=0),
    RawMidiEvent(abs_time=RECORDING_START + 11.0, type="note_on", note=64, velocity=100),
    RawMidiEvent(abs_time=RECORDING_START + 12.0, type="note_off", note=64, velocity=0),
]


class LeadInTests(unittest.TestCase):
    def test_the_lead_in_is_measured_from_the_start_of_the_recording(self):
        self.assertAlmostEqual(lead_in_seconds(EVENTS, RECORDING_START), 4.0)
        self.assertAlmostEqual(CAPTURED_S - lead_in_seconds(EVENTS, RECORDING_START), PLAYED_S)

    def test_it_is_not_the_first_notes_timestamp(self):
        """The regression itself: `first_note_on_time` is an epoch value,
        and a saved duration is elapsed seconds."""
        self.assertGreater(first_note_on_time(EVENTS), 1e9)
        self.assertLess(lead_in_seconds(EVENTS, RECORDING_START), CAPTURED_S)

    def test_a_recording_with_no_notes_has_no_lead_in(self):
        silence = [RawMidiEvent(abs_time=RECORDING_START + 1.0, type="note_off", note=60, velocity=0)]
        self.assertEqual(lead_in_seconds(silence, RECORDING_START), 0.0)
        self.assertEqual(lead_in_seconds([], RECORDING_START), 0.0)

    def test_an_unknown_start_moment_claims_no_lead_in(self):
        """Better a song that says it is the full length of the capture
        than one that says it is zero seconds long."""
        self.assertEqual(lead_in_seconds(EVENTS, None), 0.0)
        self.assertEqual(lead_in_seconds(EVENTS, 0.0), 0.0)

    def test_a_note_before_the_recorded_start_never_goes_negative(self):
        early = [RawMidiEvent(abs_time=RECORDING_START - 0.2, type="note_on", note=60, velocity=100)]
        self.assertEqual(lead_in_seconds(early, RECORDING_START), 0.0)

    def test_trimming_is_unaffected_and_still_starts_the_song_at_zero(self):
        trimmed = trim_to_first_note(EVENTS)
        self.assertAlmostEqual(trimmed[0].abs_time, 0.0)
        self.assertAlmostEqual(trimmed[-1].abs_time, 8.0)


class _WizardDurationChecks:
    """Drive one wizard's Review page over a synthetic recording and read
    the duration back out of the meta.json it wrote.

    Both wizards are the same three-stage flow over the same format - the
    Tele-training one is a deliberate copy, not a subclass (see
    doc/REMOTE_GUIDANCE.md §1) - so the bug existed twice and the test has to
    run twice."""

    module = ""  # dotted path of the wizard module, for patching

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_wizard(self, cfg):
        raise NotImplementedError

    def _saved_meta(self) -> SongMeta:
        from app.config import Config

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        songs = Path(directory.name)

        class FakeCamera:
            def __init__(self, _camera_cfg):
                pass

            def read(self):
                return None

            def release(self):
                pass

        cfg = Config.load()
        cfg.active_keyboard_profile = "a-profile"
        with mock.patch(f"{self.module}.Camera", FakeCamera):
            wizard = self.make_wizard(cfg)
        self.addCleanup(wizard.close)

        record = wizard.record_page
        record.raw_events = list(EVENTS)
        record.video_start_time = RECORDING_START
        record.midi_start_time = RECORDING_START
        record.duration_s = CAPTURED_S
        record.video_path = songs / "raw" / "performance.mp4"
        wizard.info_page.title_edit.setText("recorded song")

        with mock.patch(f"{self.module}.song_dir", side_effect=lambda name: songs / name), mock.patch(
            f"{self.module}.raw_dir", side_effect=lambda name: songs / name / "raw"
        ), mock.patch(f"{self.module}.snapshot_profile"), mock.patch(f"{self.module}.AnalyzeWorker"):
            wizard.review_page._save()
            wizard.review_page._finish_save([None, None])

        self.assertTrue(wizard.review_page.isComplete(), "the wizard did not finish saving")
        return SongMeta.load(songs / "recorded song" / "meta.json")

    def test_the_saved_song_is_as_long_as_the_playing_not_the_capture(self):
        meta = self._saved_meta()
        self.assertAlmostEqual(meta.duration_s, PLAYED_S)
        self.assertEqual(meta.note_count, 2)


class SectionThreeWizardDurationTests(_WizardDurationChecks, unittest.TestCase):
    module = "app.gui.recording_wizard"

    def make_wizard(self, cfg):
        from app.gui.recording_wizard import RecordingWizard

        return RecordingWizard(cfg)


class TeleTrainingWizardDurationTests(_WizardDurationChecks, unittest.TestCase):
    module = "remote_guidance.teacher.recording_wizard"

    def make_wizard(self, cfg):
        from remote_guidance.teacher.recording_wizard import TeacherRecordingWizard

        return TeacherRecordingWizard(cfg)


if __name__ == "__main__":
    unittest.main()
