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
from remote_guidance.calibration_wizard import (  # noqa: E402
    PAGE_BOUNDARY,
    PAGE_CAPTURE,
    PAGE_EDGES,
    PAGE_FILL,
    PAGE_PROFILE,
    RemoteKeyboardCalibrationWizard,
)
from remote_guidance.midi_mapping_wizard import (  # noqa: E402
    PAGE_CONNECT as MIDI_PAGE_CONNECT,
    PAGE_MAP as MIDI_PAGE_MAP,
    HIGHLIGHT_COLOR,
    RemoteMidiMappingWizard,
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
    """The window over the store. Built headless: opening it performs no
    scan and constructs no Camera."""

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
        """Stand in for a completed child calibration in parent wiring tests."""
        directory = create_profile(name, self.root)
        key_map = np.zeros((120, 160), dtype=np.uint8)
        for key_id in range(keys):
            key_map[10:110, 10 + key_id * 30 : 30 + key_id * 30] = key_id + 1
        KeyboardTemplate(
            frame_width=160,
            frame_height=120,
            region={"x": 0, "y": 0, "w": 160, "h": 120},
            keys=[KeyBox(id=i, kind="white") for i in range(keys)],
            key_map=key_map,
        ).save(directory / TEMPLATE_FILENAME)
        self.wizard.profile_name = name
        self.wizard._sync_step_buttons()

    def test_it_opens_on_the_camera_step(self):
        """Step 1 is the camera: the calibration is a pixel mask of that
        camera's frame, so it cannot be chosen afterwards. Opening the
        Tele-training wizard itself must not scan or claim anything."""
        self.assertEqual(self.wizard.step_buttons[0].text(), "1. Camera")
        self.assertEqual(self.wizard.stack.currentIndex(), 0)
        self.assertIsNone(self.wizard.camera)
        self.assertIsNone(self.wizard._probe_worker)
        self.assertFalse(self.wizard._camera_selected)
        self.assertIsNone(self.wizard.mapping_wizard, 'step 3 has not opened its child wizard')

    def test_constructing_the_window_never_constructs_a_camera(self):
        from unittest import mock

        from remote_guidance.setup_wizard import RemoteSetupWizard

        with mock.patch("remote_guidance.setup_wizard.Camera") as camera_factory:
            another = RemoteSetupWizard(Config.load(self.config_path), profile_data_dir=self.root)
            self.addCleanup(another.close)

        camera_factory.assert_not_called()

    def test_scan_results_do_not_open_a_camera_until_the_user_selects_one(self):
        from unittest import mock

        class FakeCamera:
            opened = []

            def __init__(self, cfg):
                self.cfg = cfg
                self.is_opened = True
                self.released = False
                type(self).opened.append(cfg.index)

            def read(self):
                return None

            def release(self):
                self.released = True

        with mock.patch("remote_guidance.setup_wizard.Camera", FakeCamera):
            self.wizard._on_scan_finished([1, 3])
            self.assertEqual(FakeCamera.opened, [])
            self.assertFalse(self.wizard._camera_selected)
            self.assertEqual(self.wizard.index_combo.currentData(), None)

            self.wizard.index_combo.setCurrentIndex(2)

        self.assertEqual(FakeCamera.opened, [3])
        self.assertTrue(self.wizard._camera_selected)
        self.assertEqual(self.wizard.camera_config.index, 3)

    def test_calibration_is_blocked_until_scan_then_selection(self):
        from remote_guidance.setup_wizard import STEP_CALIBRATION

        self.assertIn("Scan for cameras", self.wizard._blocked_reason(STEP_CALIBRATION))
        self.wizard._on_scan_finished([2])
        self.assertIn("select the camera", self.wizard._blocked_reason(STEP_CALIBRATION))

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

    def test_a_completed_calibration_enables_mapping(self):
        self._calibrate("remote-desk-20260807")

        self.assertEqual(self.wizard.profile_name, "remote-desk-20260807")
        self.assertTrue(is_remote_profile("remote-desk-20260807", self.root))
        self.assertTrue((self.root / "remote-desk-20260807" / TEMPLATE_FILENAME).exists())

    def test_there_is_no_role_anywhere(self):
        """A calibration describes a camera and a keyboard, not a person."""
        self.assertFalse(hasattr(self.wizard, "role"))
        self.assertFalse(hasattr(self.wizard, "role_combo"))

    def test_parent_flow_leaves_config_json_byte_identical(self):
        before = self.config_path.read_bytes()

        self._calibrate("remote-desk")

        self.assertEqual(self.config_path.read_bytes(), before)

    def test_the_isolation_note_names_what_will_not_be_touched(self):
        text = self.wizard.isolation_note.text()
        self.assertIn(EXPERIMENT_PROFILE, text)
        self.assertIn("Experiment Keyboard", text)

    def test_the_wizard_has_no_way_to_write_config_json_at_all(self):
        """The strongest form of the rule: neither module mentions saving
        a config, and neither imports the remote config object."""
        import inspect

        from remote_guidance import calibration_wizard, midi_mapping_wizard, setup_store, setup_wizard

        for module in (setup_wizard, calibration_wizard, midi_mapping_wizard, setup_store):
            source = inspect.getsource(module)
            for forbidden in ("cfg.save()", "base_cfg.save()", "active_keyboard_profile =",
                              "RemoteGuidanceConfig", "atomic_write_json"):
                self.assertNotIn(forbidden, source, f"{module.__name__} contains {forbidden!r}")


class CalibrationCopyTests(unittest.TestCase):
    """Tele-training uses Initial Setup's five calibration stages and crop
    coordinate system, with only the save destination changed."""

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
        self.cfg = Config.load(self.config_path)

        class FakeCamera:
            def __init__(camera_self, camera_cfg):
                camera_self.cfg = camera_cfg
                camera_self.released = False

            def read(camera_self):
                return np.zeros((120, 160, 3), dtype=np.uint8)

            def release(camera_self):
                camera_self.released = True

        self.wizard = RemoteKeyboardCalibrationWizard(
            self.cfg,
            self.cfg.camera,
            self.root,
            camera_factory=FakeCamera,
        )
        self.addCleanup(self.wizard.close)

    def _ready_fill_page(self, profile_name="remote-desk"):
        from unittest import mock

        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        self.wizard.state.update(
            keyboard_profile_name=profile_name,
            frame=frame,
            boundary=(20, 15, 100, 80),
            crop=frame[15:95, 20:120].copy(),
            edges=np.zeros((80, 100), dtype=np.uint8),
        )
        page = self.wizard.page(PAGE_FILL)
        page.fill_wizard = mock.Mock()
        page.fill_wizard.keys = [KeyBox(id=0, kind="white"), KeyBox(id=1, kind="black")]
        local_map = np.zeros((80, 100), dtype=np.uint8)
        local_map[5:75, 5:45] = 1
        local_map[10:50, 55:80] = 2
        page.fill_wizard.build_key_map.return_value = local_map
        return page, local_map

    def test_it_has_the_same_five_pages_as_initial_setup(self):
        self.assertEqual(
            [self.wizard.page(page_id).title() for page_id in range(5)],
            [
                "Step 0 - Name this camera profile",
                "Step 1 - Capture a photo",
                "Step 2 - Mark the keyboard boundary",
                "Step 3 - Tune edge detection",
                "Step 4 - Mark keys",
            ],
        )
        self.assertIsNotNone(self.wizard.page(PAGE_PROFILE))
        self.assertIsNotNone(self.wizard.page(PAGE_CAPTURE))

    def test_boundary_edges_and_fill_use_the_same_crop_local_coordinates(self):
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        self.wizard.state["frame"] = frame

        boundary_page = self.wizard.page(PAGE_BOUNDARY)
        boundary_page.initializePage()
        boundary_page._on_click(20, 15)
        boundary_page._on_click(120, 95)
        self.assertTrue(boundary_page.validatePage())

        edge_page = self.wizard.page(PAGE_EDGES)
        edge_page.initializePage()
        self.assertEqual(self.wizard.state["crop"].shape, (80, 100, 3))
        self.assertEqual(self.wizard.state["edges"].shape, (80, 100))

        fill_page = self.wizard.page(PAGE_FILL)
        fill_page.initializePage()
        self.assertEqual(fill_page.fill_wizard.frame.shape, (80, 100, 3))
        self.assertEqual(fill_page.fill_wizard.boundary, (0, 0, 100, 80))
        self.assertEqual(
            fill_page.fill_wizard.max_fill_radius,
            int(self.cfg.wizard.max_fill_size_ratio * 100),
        )

    def test_finish_pastes_the_crop_map_into_a_full_frame_profile(self):
        from unittest import mock

        before = self.config_path.read_bytes()
        page, local_map = self._ready_fill_page("remote-desk")
        with mock.patch("remote_guidance.calibration_wizard.QMessageBox.information"):
            self.assertTrue(page.validatePage())

        template = KeyboardTemplate.load(self.root / "remote-desk" / TEMPLATE_FILENAME)
        self.assertEqual(template.key_map.shape, (120, 160))
        np.testing.assert_array_equal(template.key_map[15:95, 20:120], local_map)
        self.assertFalse(template.key_map[:15].any())
        self.assertFalse(template.key_map[:, :20].any())
        self.assertTrue(is_remote_profile("remote-desk", self.root))
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_finish_refuses_an_existing_profile_instead_of_overwriting_it(self):
        from unittest import mock

        existing = write_profile(self.root, EXPERIMENT_PROFILE)
        original = (existing / TEMPLATE_FILENAME).read_bytes()
        page, _ = self._ready_fill_page(EXPERIMENT_PROFILE)

        with mock.patch("remote_guidance.calibration_wizard.QMessageBox.warning") as warning:
            self.assertFalse(page.validatePage())

        self.assertIn("already exists", warning.call_args.args[2])
        self.assertEqual((existing / TEMPLATE_FILENAME).read_bytes(), original)
        self.assertFalse(is_remote_profile(EXPERIMENT_PROFILE, self.root))


class MidiMappingCopyTests(unittest.TestCase):
    """The Tele-training MIDI step keeps both images from Initial Setup:
    the Middle C reference and the live camera/profile overlay."""

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
        self.cfg = Config.load(self.config_path)

        directory = create_profile("remote-desk", self.root)
        key_map = np.zeros((120, 160), dtype=np.uint8)
        key_map[10:110, 10:60] = 1
        key_map[10:110, 70:120] = 2
        KeyboardTemplate(
            frame_width=160,
            frame_height=120,
            region={"x": 10, "y": 10, "w": 110, "h": 100},
            keys=[KeyBox(id=0, kind="white"), KeyBox(id=1, kind="white")],
            key_map=key_map,
        ).save(directory / TEMPLATE_FILENAME)
        write_profile(self.root, EXPERIMENT_PROFILE)

        class FakeCamera:
            def __init__(camera_self, camera_cfg):
                camera_self.cfg = camera_cfg
                camera_self.released = False
                camera_self.last_frame = None

            def read(camera_self):
                camera_self.last_frame = np.zeros((120, 160, 3), dtype=np.uint8)
                return camera_self.last_frame

            def release(camera_self):
                camera_self.released = True

        self.wizard = RemoteMidiMappingWizard(
            self.cfg,
            self.cfg.camera,
            self.root,
            preferred_profile_name="remote-desk",
            preferred_port_name="SE25 MIDI1 #1",
            camera_factory=FakeCamera,
        )
        self.addCleanup(self.wizard.close)

    def test_it_has_initial_setup_s_two_image_pages(self):
        self.assertEqual(self.wizard.page(MIDI_PAGE_CONNECT).title(), "Step 3a - Connect the MIDI keyboard")
        self.assertEqual(self.wizard.page(MIDI_PAGE_MAP).title(), "Step 3b - Map keys to MIDI notes")

        reference = self.wizard.connect_page.reference_image_label.pixmap()
        self.assertIsNotNone(reference)
        self.assertFalse(reference.isNull(), "the Middle C reference image was not loaded")

    def test_map_page_shows_live_camera_with_the_next_key_highlighted(self):
        page = self.wizard.map_page
        page.initializePage()
        page._tick()

        displayed = page.view.pixmap()
        self.assertIsNotNone(displayed)
        self.assertFalse(displayed.isNull(), "the live mapping image was not displayed")
        self.assertIsNotNone(self.wizard.camera.last_frame)
        self.assertGreater(
            int(self.wizard.camera.last_frame[20, 20].sum()),
            0,
            f"the current key did not receive the {HIGHLIGHT_COLOR} overlay",
        )
        self.assertIn("press key #1 now", page.progress_label.text())

    def test_profile_picker_only_offers_tele_training_profiles(self):
        page = self.wizard.map_page
        page._refresh_profiles()
        offered = [page.profile_combo.itemText(i) for i in range(page.profile_combo.count())]
        self.assertEqual(offered, ["remote-desk"])
        self.assertNotIn(EXPERIMENT_PROFILE, offered)

    def test_map_page_refuses_an_injected_experiment_profile(self):
        page = self.wizard.map_page
        page._load_profile(EXPERIMENT_PROFILE)
        self.assertIsNone(page.template)
        self.assertIn("not created by the tele-training setup", page.status_label.text())

    def test_saving_mapping_writes_only_the_tele_training_profile(self):
        from unittest import mock

        before = self.config_path.read_bytes()
        page = self.wizard.map_page
        page._refresh_profiles()
        page.mapping = {0: 60, 1: 62}
        page.pos = 2
        self.wizard.port_name = "SE25 MIDI1 #1"

        with mock.patch("remote_guidance.midi_mapping_wizard.QMessageBox.information"):
            page._save()

        mapping = MidiMapping.load(self.root / "remote-desk" / "midi_mapping.json")
        self.assertEqual(mapping.key_to_note, {0: 60, 1: 62})
        self.assertEqual(mapping.port_name, "SE25 MIDI1 #1")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse((self.root / EXPERIMENT_PROFILE / "midi_mapping.json").exists())


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
