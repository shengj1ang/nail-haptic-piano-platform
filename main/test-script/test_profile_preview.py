"""The "does the camera still match the calibration?" preview.

Shared by the two quizzes, the main study's trial runner and both remote
guidance Settings dialogs, which is the point: the finger judgement is
only as good as the profile still lining up with what the camera sees, so
every window about to rely on that can show it.

The one behaviour worth a test of its own is the refusal: a mask that
does not match the frame must be reported, never resized. A stretched
mask makes a wrong calibration look plausibly aligned, which is exactly
the outcome the preview exists to rule out.

No camera and no keyboard are needed - frames are arrays and the profile
is written into a temp directory.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.gui.profile_preview import (  # noqa: E402
    KeyboardProfilePreviewDialog,
    load_profile_template,
    overlay_profile_mask,
    overlay_template,
)
from app.keyboard.template import KeyBox, KeyboardTemplate  # noqa: E402

FRAME_W, FRAME_H = 160, 120


def write_profile(root: Path, name: str = "test-profile", width: int = FRAME_W, height: int = FRAME_H, keys: int = 3):
    key_map = np.zeros((height, width), dtype=np.uint8)
    for key_id in range(keys):
        key_map[10:height - 10, 10 + key_id * 30 : 30 + key_id * 30] = key_id + 1
    KeyboardTemplate(
        frame_width=width,
        frame_height=height,
        keys=[KeyBox(id=i, kind="white") for i in range(keys)],
        key_map=key_map,
    ).save(root / name / "keyboard_template.json")
    return name


def blank_frame(width: int = FRAME_W, height: int = FRAME_H, value: int = 40) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


class FakeCamera:
    """A camera that opens nothing. The quiz windows claim their camera in
    __init__, and on a machine without camera permission that call can sit
    waiting on an authorization prompt nobody is there to answer."""

    is_opened = False

    def __init__(self, _config):
        pass

    def read(self):
        return None

    def release(self):
        pass


def quiet_window():
    """Build a quiz window without touching hardware or this machine's own
    song library.

    Both matter. The camera is claimed in __init__, and __init__ also loads
    the first song it finds - which pops a modal "couldn't load song" box
    when that song's profile is missing, and a modal box in a test run is a
    hang, not a failure."""
    import student_quiz

    return mock.patch.multiple(student_quiz, Camera=FakeCamera, list_song_entries=lambda: [])


class LoadProfileTemplateTests(unittest.TestCase):
    def test_an_empty_name_asks_for_a_choice(self):
        with self.assertRaisesRegex(ValueError, "Choose a keyboard calibration profile"):
            load_profile_template("   ")

    def test_an_uncalibrated_profile_names_the_file_it_wanted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError) as raised:
                load_profile_template("never-calibrated", Path(temp_dir))
        self.assertIn("keyboard_template.json", str(raised.exception))
        self.assertIn("never-calibrated", str(raised.exception))

    def test_a_calibrated_profile_loads_with_its_mask(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            name = write_profile(Path(temp_dir), keys=3)
            template = load_profile_template(name, Path(temp_dir))
        self.assertEqual(len(template.keys), 3)
        self.assertEqual(template.key_map.shape, (FRAME_H, FRAME_W))


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.name = write_profile(self.root)

    def test_the_mask_is_drawn_and_the_source_frame_is_left_alone(self):
        source = blank_frame()
        preview, template = overlay_profile_mask(source, self.name, self.root)

        self.assertEqual(preview.shape, source.shape)
        self.assertEqual(len(template.keys), 3)
        self.assertTrue((source == 40).all(), "the caller's frame was drawn on")
        self.assertFalse(np.array_equal(preview[60, 20], source[60, 20]), "no mask was drawn inside a key")
        self.assertTrue(np.array_equal(preview[2, 2], source[2, 2]), "pixels outside every key were changed")

    def test_a_resolution_mismatch_is_refused_rather_than_resized(self):
        """A stretched mask would make a wrong calibration look right."""
        with self.assertRaises(ValueError) as raised:
            overlay_profile_mask(blank_frame(width=80, height=60), self.name, self.root)
        message = str(raised.exception)
        self.assertIn("80 × 60", message)
        self.assertIn(f"{FRAME_W} × {FRAME_H}", message)
        self.assertIn(self.name, message)

    def test_the_mismatch_message_names_the_profile_it_was_given(self):
        template = load_profile_template(self.name, self.root)
        with self.assertRaisesRegex(ValueError, "'a-different-name'"):
            overlay_template(blank_frame(width=80, height=60), template, "a-different-name")

    def test_a_profile_whose_mask_image_is_gone_names_the_profile(self):
        """KeyboardTemplate.load raises with only the missing path; this
        lands in a message box, so it has to say whose profile it is."""
        name = write_profile(self.root, name="lost-mask")
        for mask in (self.root / name).glob("*.png"):
            mask.unlink()

        with self.assertRaises(RuntimeError) as raised:
            load_profile_template(name, self.root)
        self.assertIn("lost-mask", str(raised.exception))
        self.assertIn("Recalibrate", str(raised.exception))

    def test_a_template_with_no_mask_in_memory_is_refused_by_the_drawing(self):
        template = KeyboardTemplate(frame_width=FRAME_W, frame_height=FRAME_H, keys=[KeyBox(id=0, kind="white")])
        with self.assertRaisesRegex(RuntimeError, "no readable keyboard key map"):
            overlay_template(blank_frame(), template, "in-memory-profile")


class PreviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_the_dialog_reports_the_size_profile_and_key_count(self):
        dialog = KeyboardProfilePreviewDialog(blank_frame(), "desk-profile", 25)
        self.addCleanup(dialog.deleteLater)
        from PySide6.QtWidgets import QLabel

        text = " ".join(label.text() for label in dialog.findChildren(QLabel) if label.text())
        self.assertIn(f"{FRAME_W} × {FRAME_H}", text)
        self.assertIn("desk-profile", text)
        self.assertIn("25 keys", text)
        self.assertIn("not saved", text)
        self.assertIn("desk-profile", dialog.windowTitle())


class QuizWindowPreviewButtonTests(unittest.TestCase):
    """The quiz windows hold their camera open for their whole lifetime,
    so the button must annotate the frame already on screen - opening a
    second capture on the same device fails on most backends."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.name = write_profile(self.root)

        import student_quiz
        from app.config import Config

        self.student_quiz = student_quiz
        cfg = Config()
        cfg.active_keyboard_profile = self.name
        with quiet_window():
            self.window = student_quiz.QuizWindow(cfg)
        self.addCleanup(self.window.close)

    def _preview(self):
        """Run the handler, with the real overlay redirected at the temp
        profile directory.

        The redirect goes through the window's own reference rather than
        app.gui.profile_preview.PROFILE_DATA_DIR, because that constant is
        a default argument and was bound when the function was defined -
        patching the module attribute would change nothing and the test
        would quietly be reading the developer's own profiles."""
        real_overlay = self.student_quiz.overlay_profile_mask

        def overlay_from_temp_dir(frame, profile_name):
            return real_overlay(frame, profile_name, self.root)

        with mock.patch.object(
            self.student_quiz, "overlay_profile_mask", overlay_from_temp_dir
        ), mock.patch.object(self.student_quiz, "KeyboardProfilePreviewDialog") as dialog:
            with mock.patch.object(self.student_quiz.QMessageBox, "warning") as warning:
                self.window._preview_profile()
        return dialog, warning

    def test_the_button_is_offered_and_says_what_it_does(self):
        self.assertIn("keyboard profile", self.window.preview_profile_btn.text().lower())
        self.assertIn("calibration", self.window.preview_profile_btn.toolTip().lower())

    def test_it_annotates_the_frame_already_on_screen(self):
        self.window._last_frame = blank_frame()
        dialog, warning = self._preview()

        warning.assert_not_called()
        frame, profile_name, key_count = dialog.call_args.args
        self.assertEqual(frame.shape, (FRAME_H, FRAME_W, 3))
        self.assertEqual(profile_name, self.name)
        self.assertEqual(key_count, 3)
        dialog.return_value.exec.assert_called_once_with()

    def test_it_uses_the_active_profile_not_a_separate_choice(self):
        """The preview has to be of the setup that is about to record."""
        self.window._last_frame = blank_frame()
        dialog, _ = self._preview()
        self.assertEqual(dialog.call_args.args[1], self.window.cfg.active_keyboard_profile)

    def test_no_frame_yet_warns_instead_of_crashing(self):
        self.window._last_frame = None
        dialog, warning = self._preview()

        dialog.assert_not_called()
        self.assertIn("camera", warning.call_args.args[1].lower())

    def test_a_profile_problem_is_reported_not_raised(self):
        self.window._last_frame = blank_frame(width=80, height=60)
        dialog, warning = self._preview()

        dialog.assert_not_called()
        self.assertIn("Resolution mismatch", warning.call_args.args[2])

    def test_the_preview_is_locked_while_a_quiz_is_recording(self):
        """A modal window over a trial being recorded is not wanted."""
        self.assertTrue(self.window.preview_profile_btn.isEnabled())
        for widget in (self.window.song_combo, self.window.quiz_name_edit,
                       self.window.port_combo, self.window.timeout_spin,
                       self.window.preview_profile_btn):
            widget.setEnabled(False)
        self.window._unlock_inputs()
        self.assertTrue(self.window.preview_profile_btn.isEnabled())


class InheritedByEveryQuizWindowTests(unittest.TestCase):
    """Section 5's two quizzes and section 6's trial runner are all
    QuizWindow subclasses, so one button covers the three of them."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_the_haptic_quiz_and_the_trial_runner_both_have_it(self):
        from app.config import Config
        from app.gui.experiment_runner_window import ExperimentRunnerWindow
        from student_quiz_haptic import HapticQuizWindow

        cfg = Config()
        with quiet_window():
            runner = ExperimentRunnerWindow(cfg, cue=mock.MagicMock())
        self.addCleanup(runner.close)
        self.assertTrue(runner.preview_profile_btn.isEnabled())
        self.assertTrue(runner.preview_profile_btn.isVisibleTo(runner))
        # The runner hides Start; the preview must not have gone with it.
        self.assertTrue(runner.start_btn.isHidden())

        self.assertTrue(hasattr(HapticQuizWindow, "_preview_profile"))


if __name__ == "__main__":
    unittest.main()
