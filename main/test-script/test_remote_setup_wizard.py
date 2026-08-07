"""The tele-training setup wizard must not be able to touch the experiment.

A new student joining a tele-training session needs their own camera,
calibration and MIDI mapping. Getting them from the launcher's Initial
Setup wizards is what this replaces: those write config.json's top-level
`camera`, `midi.port_name` and `active_keyboard_profile`, and they save
over `data/keyboard-profile/<name>/`. Any of those repoints the formal
experiment; the last one can destroy the calibration its already-recorded
sessions were scored against.

So most of this file is about what the wizard *cannot* do. Two rules:

  - config.json is never written at all - the wizard's only output is a
    profile folder, so running it changes nothing on this machine until
    someone selects the new profile in a client's own Settings;
  - the wizard may only write into profile folders it created itself,
    which it marks, and it never overwrites an existing folder.

Everything here runs against a temporary config file and a temporary
profile directory. No camera, no keyboard, no Qt in the store tests.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import Config  # noqa: E402
from app.keyboard.midi_mapping import MidiMapping  # noqa: E402
from app.keyboard.template import KeyBox, KeyboardTemplate  # noqa: E402
from remote_guidance.setup_store import (  # noqa: E402
    MARKER_FILENAME,
    TEMPLATE_FILENAME,
    RemoteProfileMarker,
    SetupError,
    create_profile,
    is_remote_profile,
    list_remote_profiles,
    profile_status,
    sanitize_profile_name,
    writable_profile_dir,
)

EXPERIMENT_PROFILE = "white-city-lab-20260717"

LEGACY_CONFIG = {
    "camera": {"index": 0, "width": 1280, "height": 720, "fps": 30},
    "midi": {"port_name": "Experiment Keyboard"},
    "active_keyboard_profile": EXPERIMENT_PROFILE,
    "visual_cue_style": "dot",
}


def write_profile(root: Path, name: str, keys: int = 3) -> Path:
    """A calibrated profile as the launcher's own wizard would leave it -
    no remote marker."""
    key_map = np.zeros((120, 160), dtype=np.uint8)
    for key_id in range(keys):
        key_map[10:110, 10 + key_id * 30 : 30 + key_id * 30] = key_id + 1
    KeyboardTemplate(
        frame_width=160,
        frame_height=120,
        keys=[KeyBox(id=i, kind="white") for i in range(keys)],
        key_map=key_map,
    ).save(root / name / TEMPLATE_FILENAME)
    return root / name


class ProfileClaimTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def test_a_new_profile_is_created_and_marked_as_the_wizard_s_own(self):
        directory = create_profile("student-desk-20260807", self.root)

        self.assertTrue(directory.exists())
        self.assertTrue(is_remote_profile("student-desk-20260807", self.root))
        marker = RemoteProfileMarker.load(directory / MARKER_FILENAME)
        self.assertEqual(marker.profile_name, "student-desk-20260807")
        self.assertGreater(marker.created_at, 1_600_000_000)

    def test_it_refuses_to_reuse_the_name_of_an_experiment_profile(self):
        """The refusal that matters most: this is the folder a recorded
        session's scoring is reproducible from."""
        write_profile(self.root, EXPERIMENT_PROFILE)

        with self.assertRaises(SetupError) as raised:
            create_profile(EXPERIMENT_PROFILE, self.root)
        self.assertIn("already exists", str(raised.exception))
        # And it is still exactly as it was.
        self.assertTrue((self.root / EXPERIMENT_PROFILE / TEMPLATE_FILENAME).exists())
        self.assertFalse(is_remote_profile(EXPERIMENT_PROFILE, self.root))

    def test_it_refuses_to_reuse_even_its_own_earlier_name(self):
        """No overwrite path at all - not even for a profile it made. A
        remote session may have been recorded against that one too."""
        create_profile("student-desk", self.root)
        with self.assertRaises(SetupError):
            create_profile("student-desk", self.root)

    def test_an_empty_or_unusable_name_is_refused(self):
        for name in ("", "   ", "///", "..."):
            with self.assertRaises(SetupError):
                create_profile(name, self.root)

    def test_a_profile_carries_no_role(self):
        """A calibration is a camera looking at a keyboard, not a
        person. Recording a role enforced nothing and only implied a
        constraint that does not exist."""
        create_profile("some-desk", self.root)
        with open(self.root / "some-desk" / MARKER_FILENAME, encoding="utf-8") as f:
            marker = json.load(f)
        self.assertNotIn("role", marker)

    def test_names_are_made_filesystem_safe(self):
        self.assertEqual(sanitize_profile_name("student desk/2026"), "student-desk-2026")
        self.assertEqual(sanitize_profile_name("  ok-1.2  "), "ok-1.2")
        self.assertEqual(sanitize_profile_name("   "), "")


class WritableProfileTests(unittest.TestCase):
    """"Redo just the MIDI mapping" has to be able to write into a profile
    that already exists. This is the guard that keeps that from being a
    way to reach the experiment's profiles."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def test_a_profile_the_wizard_made_is_writable(self):
        create_profile("teacher-desk", self.root)
        self.assertEqual(writable_profile_dir("teacher-desk", self.root), self.root / "teacher-desk")

    def test_a_profile_the_wizard_did_not_make_is_refused(self):
        write_profile(self.root, EXPERIMENT_PROFILE)

        with self.assertRaises(SetupError) as raised:
            writable_profile_dir(EXPERIMENT_PROFILE, self.root)
        message = str(raised.exception)
        self.assertIn("not created by the tele-training setup", message)
        self.assertIn("formal experiment", message)

    def test_a_profile_that_does_not_exist_is_refused(self):
        with self.assertRaises(SetupError):
            writable_profile_dir("never-made", self.root)

    def test_only_marked_profiles_are_offered_for_revision(self):
        write_profile(self.root, EXPERIMENT_PROFILE)
        write_profile(self.root, "another-experiment-one")
        create_profile("remote-one", self.root)
        create_profile("remote-two", self.root)

        self.assertEqual(list_remote_profiles(self.root), ["remote-one", "remote-two"])

    def test_status_reports_what_a_profile_already_has(self):
        create_profile("half-done", self.root)
        status = profile_status("half-done", self.root)
        self.assertTrue(status["exists"])
        self.assertTrue(status["remote"])
        self.assertFalse(status["calibrated"])
        self.assertFalse(status["mapped"])


class WizardWiringTests(unittest.TestCase):
    """The window over the store. Built headless - no camera is opened
    until a step that shows one is entered."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "profiles"
        self.root.mkdir()
        self.config_path = Path(self._dir.name) / "config.json"
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(LEGACY_CONFIG, f, indent=2)

        from remote_guidance.setup_wizard import RemoteSetupWizard

        self.wizard = RemoteSetupWizard(Config.load(self.config_path), profile_data_dir=self.root)
        self.addCleanup(self.wizard.close)

    def _calibrate(self, name="remote-desk", keys=3):
        """Stand in for the clicking: a filled-in calibration ready to save."""
        from unittest import mock

        import numpy as np

        self.wizard.captured_frame = np.zeros((120, 160, 3), dtype=np.uint8)
        self.wizard.boundary = (0, 0, 160, 120)
        self.wizard.fill_wizard = mock.Mock()
        self.wizard.fill_wizard.keys = [KeyBox(id=i, kind="white") for i in range(keys)]
        key_map = np.zeros((120, 160), dtype=np.uint8)
        for key_id in range(keys):
            key_map[10:110, 10 + key_id * 30 : 30 + key_id * 30] = key_id + 1
        self.wizard.fill_wizard.build_key_map.return_value = key_map
        self.wizard.new_profile_edit.setText(name)
        self.wizard._save_template()

    def test_it_opens_on_the_camera_step(self):
        """Step 1 is the camera: the calibration is a pixel mask of that
        camera's frame, so it cannot be chosen afterwards. The camera is
        claimed straight away because showing it is the step's whole job -
        the launcher's one-tool-at-a-time rule keeps that from clashing
        with a client."""
        self.assertEqual(self.wizard.step_buttons[0].text(), "1. Camera")
        self.assertEqual(self.wizard.stack.currentIndex(), 0)
        self.assertIsNone(self.wizard.midi, 'no MIDI port is opened until step 3')

    def test_the_three_steps_are_camera_calibration_mapping(self):
        self.assertEqual(
            [b.text() for b in self.wizard.step_buttons],
            ["1. Camera", "2. Calibration", "3. MIDI mapping"],
        )

    def test_mapping_is_blocked_until_a_calibration_is_saved(self):
        """The mapping counts up to the number of calibrated keys, so it
        has nothing to count to before then."""
        from remote_guidance.setup_wizard import STEP_MIDI

        self.assertIn("Calibrate and save a profile first", self.wizard._blocked_reason(STEP_MIDI))
        self.wizard._go(STEP_MIDI)
        self.assertEqual(self.wizard.stack.currentIndex(), 0, "a blocked step was entered anyway")

        self._calibrate()
        self.assertEqual(self.wizard._blocked_reason(STEP_MIDI), "")

    def test_saving_a_calibration_creates_and_marks_the_profile(self):
        self._calibrate("remote-desk-20260807")

        self.assertEqual(self.wizard.profile_name, "remote-desk-20260807")
        self.assertTrue(is_remote_profile("remote-desk-20260807", self.root))
        self.assertTrue((self.root / "remote-desk-20260807" / TEMPLATE_FILENAME).exists())

    def test_a_second_save_under_the_same_name_is_refused(self):
        from unittest import mock

        self._calibrate("remote-desk")
        with mock.patch("remote_guidance.setup_wizard.QMessageBox.warning") as warning:
            self._calibrate("remote-desk")
        warning.assert_called_once()
        self.assertIn("already exists", warning.call_args.args[2])

    def test_the_mapping_picker_only_offers_the_wizard_s_own_profiles(self):
        write_profile(self.root, EXPERIMENT_PROFILE)
        create_profile("remote-one", self.root)
        self.wizard._refresh_profile_choices()

        offered = [self.wizard.profile_combo.itemText(i) for i in range(self.wizard.profile_combo.count())]
        self.assertEqual(offered, ["remote-one"])

    def test_the_mapping_step_refuses_a_profile_the_wizard_did_not_create(self):
        from unittest import mock

        write_profile(self.root, EXPERIMENT_PROFILE)
        self.wizard.profile_combo.blockSignals(True)
        self.wizard.profile_combo.addItem(EXPERIMENT_PROFILE)
        self.wizard.profile_combo.setCurrentText(EXPERIMENT_PROFILE)
        self.wizard.profile_combo.blockSignals(False)

        with mock.patch("remote_guidance.setup_wizard.QMessageBox.warning") as warning:
            self.wizard._on_profile_selected()

        warning.assert_called_once()
        self.assertEqual(self.wizard.profile_name, "", "an experiment profile was adopted")

    def test_saving_a_mapping_writes_only_into_the_profile_folder(self):
        self._calibrate("remote-desk")
        self.wizard.port_name = "SE25 MIDI1 #1"
        self.wizard.key_to_note = {0: 60, 1: 62, 2: 64}
        self.wizard._save_mapping()

        mapping = MidiMapping.load(self.root / "remote-desk" / "midi_mapping.json")
        self.assertEqual(mapping.key_to_note, {0: 60, 1: 62, 2: 64})
        self.assertEqual(mapping.port_name, "SE25 MIDI1 #1")

    def test_there_is_no_role_anywhere(self):
        """A calibration describes a camera and a keyboard, not a person."""
        self.assertFalse(hasattr(self.wizard, "role"))
        self.assertFalse(hasattr(self.wizard, "role_combo"))

    def test_running_the_whole_wizard_leaves_config_json_byte_identical(self):
        """The rule the whole feature exists for. Nothing this wizard does
        may change what any tool on this machine reads."""
        before = self.config_path.read_bytes()

        self._calibrate("remote-desk")
        self.wizard.port_name = "SE25 MIDI1 #1"
        self.wizard.key_to_note = {0: 60}
        self.wizard._save_mapping()

        self.assertEqual(self.config_path.read_bytes(), before)

    def test_the_isolation_note_names_what_will_not_be_touched(self):
        text = self.wizard.isolation_note.text()
        self.assertIn(EXPERIMENT_PROFILE, text)
        self.assertIn("Experiment Keyboard", text)

    def test_the_wizard_has_no_way_to_write_config_json_at_all(self):
        """The strongest form of the rule: neither module mentions saving
        a config, and neither imports the remote config object."""
        import inspect

        from remote_guidance import setup_store, setup_wizard

        for module in (setup_wizard, setup_store):
            source = inspect.getsource(module)
            for forbidden in ("cfg.save()", "base_cfg.save()", "active_keyboard_profile =",
                              "RemoteGuidanceConfig", "atomic_write_json"):
                self.assertNotIn(forbidden, source, f"{module.__name__} contains {forbidden!r}")


class LauncherEntryTests(unittest.TestCase):
    def test_section_eight_offers_the_setup_wizard_as_an_ordinary_window(self):
        """A ProcessEntry would let it run beside a client and fight it for
        the camera; it is a sub-window so the launcher's one-tool-at-a-time
        rule applies."""
        import launcher as launcher_module

        from remote_guidance.setup_wizard import RemoteSetupWizard

        section = next(s for s in launcher_module.SECTIONS if s[0].startswith("8."))
        entries = dict(section[1])
        self.assertIn("Tele-training Setup Wizard", entries)
        self.assertIs(entries["Tele-training Setup Wizard"], RemoteSetupWizard)
        self.assertNotIsInstance(entries["Tele-training Setup Wizard"], launcher_module.ProcessEntry)


if __name__ == "__main__":
    unittest.main()
