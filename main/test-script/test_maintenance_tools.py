"""Tests for the Tools section: the two maintenance backends, the
executable lookup they share, and the windows over them.

These tools are the only ones in the platform that delete a file, so
what is pinned here is mostly what they must NOT do. A conversion that
fails, is stopped, or finds anything unexpected has to leave the
original review.mp4 byte-for-byte where it was; a backup must never
overwrite an archive, never touch a source directory, and never write a
Pxx.zip for a participant whose trials are not all in - that last one
matters because a premature archive is skipped as "done" by every later
run, which would silently freeze a participant's backup.

The other half is the frontend/backend split. The console scripts and
the launcher windows are meant to be two faces of one implementation, so
the tests drive the backend directly and then drive the windows through
the same jobs, and check the windows add no decisions of their own.

Every test runs against a temporary data/quiz built for it; the
project's own data directory is never read or written.

Run from main/:  python test-script/test_maintenance_tools.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app import quiz_backup, review_compress, tool_binaries  # noqa: E402
from app.gui.quiz_backup_window import QuizBackupWindow  # noqa: E402
from app.gui.review_compress_window import ReviewCompressWindow  # noqa: E402

_APP = QApplication.instance() or QApplication([])

FFMPEG = tool_binaries.find_ffmpeg()
SEVEN_ZIP = tool_binaries.find_seven_zip()


def make_video(path: Path, codec: str = "mpeg4", seconds: float = 0.5) -> None:
    """A tiny real video, so ffmpeg's own probing is what is being tested."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=64x64:rate=10",
            "-c:v", codec,
            str(path),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )


class _TempQuizDir(unittest.TestCase):
    """Points both backends at a scratch data/quiz for the whole test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.quiz_dir = root / "quiz"
        self.backup_dir = root / "quiz-zip"
        self.quiz_dir.mkdir()
        self._originals = (
            review_compress.QUIZ_DIR,
            quiz_backup.QUIZ_DIR,
            quiz_backup.BACKUP_DIR,
        )
        review_compress.QUIZ_DIR = self.quiz_dir
        quiz_backup.QUIZ_DIR = self.quiz_dir
        quiz_backup.BACKUP_DIR = self.backup_dir

    def tearDown(self):
        (
            review_compress.QUIZ_DIR,
            quiz_backup.QUIZ_DIR,
            quiz_backup.BACKUP_DIR,
        ) = self._originals
        self._tmp.cleanup()


class TestToolLookup(unittest.TestCase):
    """Where an executable is found, and what is said when it is not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original = tool_binaries.RUNTIME_BIN
        tool_binaries.RUNTIME_BIN = Path(self._tmp.name)

    def tearDown(self):
        tool_binaries.RUNTIME_BIN = self._original
        self._tmp.cleanup()

    def _fake_executable(self, name: str) -> Path:
        path = tool_binaries.RUNTIME_BIN / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    @unittest.skipIf(os.name == "nt", "POSIX executable bit")
    def test_runtime_bin_wins_over_path(self):
        """The whole point of the folder: a copy dropped next to the code
        is used even on a machine that has its own installed."""
        bundled = self._fake_executable("ffmpeg")
        self.assertEqual(Path(tool_binaries.find_ffmpeg()), bundled)
        self.assertTrue(tool_binaries.is_bundled(str(bundled)))

    @unittest.skipIf(os.name == "nt", "POSIX executable bit")
    def test_any_of_the_seven_zip_names_is_accepted(self):
        """7-Zip ships as 7z, 7zz or 7za depending on where it came
        from, and all three can write the ZIP this project wants."""
        bundled = self._fake_executable("7zz")
        self.assertEqual(Path(tool_binaries.find_tool(("7z", "7zz", "7za"))), bundled)

    def test_an_empty_runtime_bin_falls_back_to_path(self):
        self.assertEqual(tool_binaries.find_ffmpeg(), FFMPEG)

    def test_the_missing_message_names_both_ways_to_fix_it(self):
        message = tool_binaries.missing_tool_message("ffmpeg", tool_binaries.FFMPEG_NAMES)
        self.assertIn("PATH", message)
        self.assertIn(str(tool_binaries.RUNTIME_BIN), message)

    def test_runtime_bin_is_added_to_this_process_only(self):
        """Children inherit it; nothing outside this process is touched."""
        tool_binaries.ensure_runtime_bin_on_path()
        self.assertEqual(
            os.environ["PATH"].split(os.pathsep)[0], str(tool_binaries.RUNTIME_BIN)
        )


@unittest.skipIf(FFMPEG is None, "ffmpeg is not available")
class TestReviewCompress(_TempQuizDir):

    def _attempt(self, name: str = "P01-T01-Bβ", codec: str = "mpeg4") -> Path:
        path = self.quiz_dir / name / "review.mp4"
        make_video(path, codec)
        return path

    def test_scan_reports_each_codec(self):
        self._attempt("P01-T01-Bβ", "mpeg4")
        self._attempt("P01-T02-Cα", "libx264")
        videos = {v.attempt: v for v in review_compress.scan_review_videos()}
        self.assertEqual(videos["P01-T01-Bβ"].codec, "mpeg4")
        self.assertTrue(videos["P01-T02-Cα"].is_h264)
        self.assertTrue(videos["P01-T01-Bβ"].needs_conversion)
        self.assertFalse(videos["P01-T02-Cα"].needs_conversion)

    def test_raw_recordings_are_out_of_reach(self):
        """The scan is one level deep, so raw/performance.mp4 - the only
        irreplaceable video in the directory - is never a candidate."""
        self._attempt()
        make_video(self.quiz_dir / "P01-T01-Bβ" / "raw" / "performance.mp4")
        found = review_compress.find_review_videos()
        self.assertEqual([p.parent.name for p in found], ["P01-T01-Bβ"])

    def test_conversion_replaces_the_file_and_removes_the_temporary(self):
        review = self._attempt()
        before = review_compress.decoded_frame_count(review)[0]
        video = review_compress.scan_review_videos()[0]

        self.assertTrue(review_compress.convert_review(video))

        self.assertEqual(review_compress.probe_codec(review)[0], "h264")
        self.assertEqual(review_compress.decoded_frame_count(review)[0], before)
        self.assertEqual(
            sorted(p.name for p in review.parent.iterdir()), ["review.mp4"]
        )

    def test_an_existing_temporary_blocks_that_file_untouched(self):
        """A tmp-review.mp4 is an interrupted earlier run's original. It
        is never overwritten, and the file beside it is left alone."""
        review = self._attempt()
        temporary = review.with_name("tmp-review.mp4")
        temporary.write_bytes(b"an earlier run's original")
        original = review.read_bytes()
        video = review_compress.scan_review_videos()[0]

        messages = []
        self.assertFalse(review_compress.convert_review(video, log=messages.append))

        self.assertEqual(review.read_bytes(), original)
        self.assertEqual(temporary.read_bytes(), b"an earlier run's original")
        self.assertIn("SKIPPED", messages[0])

    def test_a_stopped_conversion_puts_the_original_back(self):
        review = self._attempt()
        original = review.read_bytes()
        video = review_compress.scan_review_videos()[0]

        # Stop as soon as the encode is under way, which is the only
        # window in which a half-written review.mp4 exists.
        state = {"stop": False}

        def progress(stage, done, total):
            if stage == review_compress.STAGE_ENCODE and done > 0:
                state["stop"] = True

        ok = review_compress.convert_review(
            video, on_progress=progress, cancelled=lambda: state["stop"]
        )

        self.assertFalse(ok)
        self.assertEqual(review.read_bytes(), original)
        self.assertFalse(review.with_name("tmp-review.mp4").exists())

    def test_a_file_stopped_before_it_started_is_not_touched_at_all(self):
        review = self._attempt()
        original = review.read_bytes()
        video = review_compress.scan_review_videos()[0]

        self.assertFalse(review_compress.convert_review(video, cancelled=lambda: True))

        self.assertEqual(review.read_bytes(), original)
        self.assertEqual([p.name for p in review.parent.iterdir()], ["review.mp4"])

    def test_stopping_a_batch_still_accounts_for_every_file(self):
        for name in ("P01-T01-Bβ", "P01-T02-Cα", "P01-T03-Cγ"):
            self._attempt(name)
        pending = review_compress.scan_review_videos()
        converted, failed = review_compress.convert_all(pending, cancelled=lambda: True)
        self.assertEqual((converted, failed), (0, 3))

    def test_progress_is_reported_against_the_source_frame_count(self):
        """The bar has to be measured against something known before the
        encode starts - the frames the original was proved to have."""
        review = self._attempt()
        total = review_compress.decoded_frame_count(review)[0]
        video = review_compress.scan_review_videos()[0]

        seen = []
        review_compress.convert_review(
            video, on_progress=lambda stage, done, t: seen.append((stage, done, t))
        )
        encode = [(done, t) for stage, done, t in seen if stage == review_compress.STAGE_ENCODE]
        self.assertTrue(encode)
        self.assertTrue(all(t == total for _done, t in encode))
        self.assertTrue(all(done <= total for done, _t in encode))


class TestQuizBackupPlan(_TempQuizDir):
    """Who is eligible, and who is deliberately not."""

    def _participant(self, participant: str, trials) -> None:
        for trial in trials:
            directory = self.quiz_dir / f"{participant}-T{trial:02d}-Bβ"
            directory.mkdir(parents=True)
            (directory / "results.json").write_text("{}")

    def test_only_a_complete_participant_is_ready(self):
        self._participant("P01", range(1, 28))
        self._participant("P02", range(1, 27))
        plan = quiz_backup.plan_backups()
        self.assertEqual([b.participant for b in plan.pending], ["P01"])
        self.assertEqual([b.participant for b in plan.incomplete], ["P02"])
        self.assertEqual(plan.incomplete[0].missing_trials, (27,))

    def test_non_participant_directories_are_ignored_not_archived(self):
        self._participant("P01", range(1, 28))
        for name in ("TEST-run", "remote-1", "P21-T01-Bβ", "P01-T28-Bβ"):
            (self.quiz_dir / name).mkdir()
        plan = quiz_backup.plan_backups()
        self.assertEqual([b.participant for b in plan.pending], ["P01"])
        self.assertEqual(
            sorted(p.name for p in plan.ignored),
            ["P01-T28-Bβ", "P21-T01-Bβ", "TEST-run", "remote-1"],
        )

    def test_an_existing_archive_is_never_a_candidate(self):
        self._participant("P01", range(1, 28))
        self.backup_dir.mkdir()
        (self.backup_dir / "P01.zip").write_bytes(b"already backed up")
        plan = quiz_backup.plan_backups()
        self.assertEqual(plan.pending, [])
        self.assertEqual([b.participant for b in plan.existing], ["P01"])
        self.assertEqual(plan.state_of(plan.existing[0]), "archive exists")

    def test_a_leftover_temporary_blocks_that_participant(self):
        self._participant("P01", range(1, 28))
        self.backup_dir.mkdir()
        (self.backup_dir / "tmp-P01.zip").write_bytes(b"half written")
        plan = quiz_backup.plan_backups()
        self.assertEqual(plan.pending, [])
        self.assertEqual([b.participant for b in plan.blocked_by_temporary], ["P01"])


@unittest.skipIf(SEVEN_ZIP is None, "7z is not available")
class TestQuizBackupArchiving(_TempQuizDir):

    def _complete_participant(self, participant: str = "P01") -> None:
        for trial in range(1, 28):
            directory = self.quiz_dir / f"{participant}-T{trial:02d}-Bβ"
            directory.mkdir(parents=True)
            (directory / "results.json").write_text(f'{{"trial": {trial}}}')
            (directory / ".DS_Store").write_bytes(b"\x00")

    def _entries(self, archive: Path) -> list:
        listing = subprocess.run(
            [SEVEN_ZIP, "l", "-slt", str(archive)],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        return [
            line.split("=", 1)[1].strip()
            for line in listing.splitlines()
            if line.startswith("Path = ")
        ]

    def test_a_complete_participant_is_archived_and_tested(self):
        self._complete_participant()
        plan = quiz_backup.plan_backups()
        self.assertIsNone(quiz_backup.create_backup_dir())

        self.assertTrue(quiz_backup.archive_participant(plan.pending[0]))

        archive = self.backup_dir / "P01.zip"
        self.assertTrue(archive.is_file())
        self.assertFalse((self.backup_dir / "tmp-P01.zip").exists())
        # Sources are untouched: the tool only ever adds a file.
        self.assertEqual(len(list(self.quiz_dir.iterdir())), 27)

    def test_the_archive_holds_the_trials_at_its_root_without_ds_store(self):
        self._complete_participant()
        plan = quiz_backup.plan_backups()
        quiz_backup.create_backup_dir()
        # With a progress callback 7z's output is read rather than
        # inherited, which is also the launcher window's path through it.
        quiz_backup.archive_participant(plan.pending[0], on_progress=lambda *_: None)

        entries = self._entries(self.backup_dir / "P01.zip")
        self.assertIn("P01-T01-Bβ/results.json", entries)
        self.assertFalse([e for e in entries if e.endswith(".DS_Store")])
        # No repository path above the trial directories.
        self.assertFalse([e for e in entries if e.startswith("..")])

    def test_stopping_before_a_participant_starts_creates_nothing(self):
        self._complete_participant()
        plan = quiz_backup.plan_backups()
        quiz_backup.create_backup_dir()

        self.assertFalse(
            quiz_backup.archive_participant(plan.pending[0], cancelled=lambda: True)
        )

        self.assertEqual(list(self.backup_dir.iterdir()), [])
        self.assertEqual(len(list(self.quiz_dir.iterdir())), 27)

    @unittest.skipIf(os.name == "nt", "POSIX shell stand-in for 7z")
    def test_a_failed_run_keeps_the_temporary_and_every_source(self):
        """A run that dies partway - 7z failing, the machine losing
        power, Stop being pressed mid-compression - must not look like a
        finished one. What is left behind wears a tmp- name, which the
        next run refuses to touch and never mistakes for a backup.

        7z is stood in for here so the half-written state is reached on
        purpose rather than by winning a race against a fast run.
        """
        self._complete_participant()
        plan = quiz_backup.plan_backups()
        quiz_backup.create_backup_dir()

        fake = Path(self._tmp.name) / "fake-7z"
        fake.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in ../quiz-zip/tmp-*) printf partial > "$arg" ;; esac\n'
            "done\n"
            "exit 1\n"
        )
        fake.chmod(0o755)
        original = quiz_backup.find_seven_zip
        quiz_backup.find_seven_zip = lambda: str(fake)
        self.addCleanup(setattr, quiz_backup, "find_seven_zip", original)

        messages = []
        ok = quiz_backup.archive_participant(plan.pending[0], log=messages.append)

        self.assertFalse(ok)
        self.assertFalse((self.backup_dir / "P01.zip").exists())
        self.assertTrue((self.backup_dir / "tmp-P01.zip").is_file())
        self.assertEqual(len(list(self.quiz_dir.iterdir())), 27)
        self.assertTrue(any("preserved" in message for message in messages))
        self.assertEqual(
            [b.participant for b in quiz_backup.plan_backups().blocked_by_temporary],
            ["P01"],
        )


class TestWindows(_TempQuizDir):
    """The windows add no decisions - they show what the backend found
    and hand the same list back to it."""

    def _run_job(self, window) -> None:
        """Run the window's current job to completion, synchronously."""
        job = window._job
        self.assertIsNotNone(job)
        job.wait(60000)
        _APP.processEvents()

    @unittest.skipIf(FFMPEG is None, "ffmpeg is not available")
    def test_the_compression_window_scans_into_its_table(self):
        make_video(self.quiz_dir / "P01-T01-Bβ" / "review.mp4", "mpeg4")
        make_video(self.quiz_dir / "P01-T02-Cα" / "review.mp4", "libx264")
        window = ReviewCompressWindow(None)
        self.addCleanup(window.close)

        window.start_scan()
        self._run_job(window)

        self.assertEqual(window.table.rowCount(), 2)
        states = {
            window.table.item(row, 0).text(): window.table.item(row, 2).text()
            for row in range(2)
        }
        self.assertEqual(states["P01-T01-Bβ"], "needs conversion")
        self.assertEqual(states["P01-T02-Cα"], "already H.264")
        # Only the file that needs it is offered for conversion.
        self.assertTrue(window.run_btn.isEnabled())
        self.assertEqual([v.attempt for v in window._pending()], ["P01-T01-Bβ"])

    @unittest.skipIf(FFMPEG is None, "ffmpeg is not available")
    def test_nothing_to_convert_leaves_the_button_disabled(self):
        make_video(self.quiz_dir / "P01-T01-Bβ" / "review.mp4", "libx264")
        window = ReviewCompressWindow(None)
        self.addCleanup(window.close)

        window.start_scan()
        self._run_job(window)

        self.assertFalse(window.run_btn.isEnabled())

    def test_the_backup_window_shows_the_plan_and_its_reasons(self):
        for trial in range(1, 28):
            (self.quiz_dir / f"P01-T{trial:02d}-Bβ").mkdir()
        (self.quiz_dir / "P02-T01-Bβ").mkdir()
        window = QuizBackupWindow(None)
        self.addCleanup(window.close)

        window.start_scan()
        self._run_job(window)

        rows = {
            window.table.item(row, 0).text(): (
                window.table.item(row, 2).text(),
                window.table.item(row, 3).text(),
            )
            for row in range(window.table.rowCount())
        }
        self.assertEqual(rows["P01"][0], "ready")
        self.assertEqual(rows["P02"][0], "incomplete")
        # 26 trials named in full would make the column wider than the
        # window, so the cell counts them and shows the first few; the
        # log keeps the whole list.
        self.assertIn("missing 26 trials: T02", rows["P02"][1])
        self.assertIn("...", rows["P02"][1])
        self.assertIn("missing T02, T03", window.log_view.toPlainText())

    def test_a_missing_program_is_said_out_loud_and_disables_the_run(self):
        """Nothing here works without the external program, so the window
        says which one it wanted and where to put it, rather than failing
        at the moment a file is about to be changed."""
        window = ReviewCompressWindow(None)
        self.addCleanup(window.close)
        window.find_tool = lambda: None

        self.assertFalse(window.refresh_tool_status())

        self.assertEqual(window.tool_status.objectName(), "toolMissing")
        self.assertIn(str(tool_binaries.RUNTIME_BIN), window.tool_status.text())
        self.assertFalse(window.scan_btn.isEnabled())
        self.assertFalse(window.run_btn.isEnabled())

    def test_the_backup_window_can_still_scan_without_7z(self):
        """Seeing who is complete is a directory listing; only creating
        the archives needs the program."""
        window = QuizBackupWindow(None)
        self.addCleanup(window.close)
        window.find_tool = lambda: None

        window.refresh_tool_status()

        self.assertTrue(window.scan_btn.isEnabled())
        self.assertFalse(window.run_btn.isEnabled())


class TestConsoleAndWindowShareOneImplementation(unittest.TestCase):
    """The scripts are frontends, not second copies.

    If either grew its own scanning or archiving, these tools could start
    disagreeing about which files are eligible - the kind of difference
    that is only noticed after something is deleted.
    """

    def test_the_console_tools_only_import_the_backend(self):
        for script, backend in (
            ("tool_compress_review_videos.py", "app.review_compress"),
            ("tool_backup_quiz_to_zip.py", "app.quiz_backup"),
        ):
            source = (PROJECT_ROOT / script).read_text()
            self.assertIn(f"from {backend} import", source, script)
            self.assertNotIn("subprocess", source, script)

    def test_the_windows_never_shell_out_themselves(self):
        for window in ("review_compress_window.py", "quiz_backup_window.py"):
            source = (PROJECT_ROOT / "app" / "gui" / window).read_text()
            self.assertNotIn("subprocess", source, window)

    def test_the_backends_never_import_qt(self):
        """They have to stay usable from a terminal with no display."""
        for module in ("review_compress.py", "quiz_backup.py", "tool_binaries.py"):
            source = (PROJECT_ROOT / "app" / module).read_text()
            self.assertNotIn("PySide6", source, module)

    def test_the_launcher_offers_both_tools_in_one_section(self):
        import launcher

        section = dict(launcher.SECTIONS)["9. Tools"]
        self.assertEqual(
            [entry for _label, entry in section],
            [ReviewCompressWindow, QuizBackupWindow],
        )
        # A run of either takes minutes; another button must not kill it.
        self.assertTrue(set(dict(section).values()) <= launcher.CONCURRENT_TOOLS)


if __name__ == "__main__":
    if FFMPEG is None:
        print("NOTE: ffmpeg not found - the conversion tests will be skipped.")
    if SEVEN_ZIP is None:
        print("NOTE: 7z not found - the archiving tests will be skipped.")
    unittest.main(verbosity=2)
