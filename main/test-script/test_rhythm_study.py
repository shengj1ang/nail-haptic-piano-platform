"""Tests for the rhythm experiment's trial schedule and its isolation.

Run from main/:  python test-script/test_rhythm_study.py

Two things are checked here. First, that the schedule is the design:
Training x5 -> Probe -> (x3) -> Final test, with the right guidance
channels on every row. Second - and the reason several of these tests
look paranoid - that this study cannot disturb the Main User Study,
whose data collection is finished and whose stimulus pools are frozen.
"""

import ast
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rhythm_study import schedule as rs  # noqa: E402

MAIN = Path(__file__).resolve().parent.parent


def imported_modules(path: Path):
    """Every module name a file imports, including the ones imported
    inside a function - a deferred import is still a dependency."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.trials = rs.build_schedule("melody_seed42")

    def test_the_schedule_is_nineteen_trials(self):
        self.assertEqual(len(self.trials), 19)
        self.assertEqual(rs.TOTAL_TRIALS, 19)

    def test_the_phase_order_is_five_training_then_a_probe_three_times(self):
        expected = (["training"] * 5 + ["probe"]) * 3 + ["final"]
        self.assertEqual([t["phase"] for t in self.trials], expected)

    def test_there_are_fifteen_training_trials_three_probes_one_final(self):
        phases = [t["phase"] for t in self.trials]
        self.assertEqual(phases.count("training"), 15)
        self.assertEqual(phases.count("probe"), 3)
        self.assertEqual(phases.count("final"), 1)

    def test_training_has_both_channels_probe_drops_haptic_final_drops_both(self):
        for trial in self.trials:
            with self.subTest(index=trial["index"], phase=trial["phase"]):
                if trial["phase"] == "training":
                    self.assertTrue(trial["backlight"])
                    self.assertTrue(trial["haptic"])
                elif trial["phase"] == "probe":
                    self.assertTrue(trial["backlight"])
                    self.assertFalse(trial["haptic"])
                else:
                    self.assertFalse(trial["backlight"])
                    self.assertFalse(trial["haptic"])

    def test_the_final_test_is_last(self):
        self.assertEqual(self.trials[-1]["phase"], "final")
        self.assertEqual(self.trials[-1]["index"], 19)

    def test_probes_are_numbered_one_to_three_in_order(self):
        probes = [t for t in self.trials if t["phase"] == "probe"]
        self.assertEqual([p["phase_number"] for p in probes], [1, 2, 3])
        self.assertEqual([p["index"] for p in probes], [6, 12, 18])

    def test_training_is_numbered_continuously_across_blocks(self):
        # "Training 6/15" after the first probe, not "Training 1" again -
        # the participant has done six by then, and the row has to say so.
        training = [t for t in self.trials if t["phase"] == "training"]
        self.assertEqual([t["phase_number"] for t in training], list(range(1, 16)))

    def test_every_trial_runs_the_same_melody(self):
        self.assertEqual({t["melody"] for t in self.trials}, {"melody_seed42"})

    def test_a_rest_follows_each_probe_but_not_the_last_trial(self):
        rests = [t["index"] for t in self.trials if t["rest_after"]]
        self.assertEqual(rests, [6, 12, 18])

    def test_every_trial_starts_pending_with_no_timestamps(self):
        for trial in self.trials:
            self.assertEqual(trial["status"], rs.TRIAL_STATUS_PENDING)
            self.assertIsNone(trial["started_at"])
            self.assertIsNone(trial["completed_at"])

    def test_the_schedule_is_fixed_not_sampled(self):
        # No seed anywhere: two builds must be identical, and the module
        # must not even offer a seed parameter to get that.
        self.assertEqual(rs.build_schedule("m"), rs.build_schedule("m"))


class TestQuizNaming(unittest.TestCase):
    def test_quiz_names_carry_the_rhythm_prefix_and_a_padded_index(self):
        self.assertEqual(rs.trial_quiz_base_name("P01", 1), "rhythm-P01-T01")
        self.assertEqual(rs.trial_quiz_base_name("P01", 19), "rhythm-P01-T19")

    def test_the_prefix_is_what_separates_the_studies_in_data_quiz(self):
        # data/quiz/ is shared with the Main User Study, whose folders are
        # "<participant>-T<NN>-<condition><level>". The prefix is the only
        # thing keeping the two apart, so it must always be there.
        for index in range(1, rs.TOTAL_TRIALS + 1):
            self.assertTrue(rs.trial_quiz_base_name("P07", index).startswith("rhythm-"))


class TestProgressAndResume(unittest.TestCase):
    def setUp(self):
        self.doc = {
            "participant": {"name": "P01"},
            "trials": rs.build_schedule("melody_seed42"),
        }
        rs.recompute_progress(self.doc)

    def test_a_fresh_schedule_resumes_from_trial_one(self):
        self.assertEqual(self.doc["progress"]["completed"], 0)
        self.assertEqual(rs.next_pending_trial(self.doc)["index"], 1)
        self.assertFalse(self.doc["progress"]["finished"])

    def test_completing_trials_moves_the_resume_point(self):
        for index in (1, 2, 3):
            rs.mark_trial_completed(self.doc, index)
        self.assertEqual(self.doc["progress"]["completed"], 3)
        self.assertEqual(rs.next_pending_trial(self.doc)["index"], 4)

    def test_an_in_progress_trial_is_offered_again_not_skipped(self):
        # A crash mid-trial must re-run that trial, not step over it.
        rs.mark_trial_completed(self.doc, 1)
        rs.mark_trial_started(self.doc, 2)
        self.assertEqual(rs.next_pending_trial(self.doc)["index"], 2)

    def test_a_finished_session_has_no_pending_trial(self):
        for trial in self.doc["trials"]:
            rs.mark_trial_completed(self.doc, trial["index"])
        self.assertIsNone(rs.next_pending_trial(self.doc))
        self.assertTrue(self.doc["progress"]["finished"])

    def test_saving_repairs_a_summary_that_was_hand_edited(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.doc["progress"] = {"completed": 999, "total": 0}
            rs.mark_trial_completed(self.doc, 1)
            path = rs.save_trial_structure(self.doc, data_dir=Path(tmp))
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["progress"]["completed"], 1)
            self.assertEqual(written["progress"]["total"], 19)


class TestSaveLoad(unittest.TestCase):
    def test_a_saved_schedule_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            doc = {
                "participant": {"name": "P02"},
                "melody": "melody_seed42",
                "trials": rs.build_schedule("melody_seed42"),
            }
            rs.mark_trial_completed(doc, 1)
            rs.save_trial_structure(doc, data_dir=data_dir)

            self.assertEqual(rs.list_participants(data_dir=data_dir), ["P02"])
            loaded = rs.load_trial_structure("P02", data_dir=data_dir)
            self.assertEqual(loaded["melody"], "melody_seed42")
            self.assertEqual(loaded["progress"]["completed"], 1)
            self.assertEqual(rs.next_pending_trial(loaded)["index"], 2)

    def test_participant_names_are_made_folder_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            doc = {"participant": {"name": "P 03/x"}, "trials": rs.build_schedule("m")}
            path = rs.save_trial_structure(doc, data_dir=data_dir)
            self.assertNotIn("/", path.parent.name)
            self.assertNotIn(" ", path.parent.name)

    def test_listing_ignores_folders_with_no_trial_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "stray_folder").mkdir()
            doc = {"participant": {"name": "P04"}, "trials": rs.build_schedule("m")}
            rs.save_trial_structure(doc, data_dir=data_dir)
            self.assertEqual(rs.list_participants(data_dir=data_dir), ["P04"])


class TestIsolationFromTheMainStudy(unittest.TestCase):
    """The main study's data is collected and its pools are frozen; this
    study must not be able to reach either."""

    def test_schedules_are_written_under_data_RhythmStudy(self):
        self.assertEqual(rs.DATA_DIR.name, "RhythmStudy")
        self.assertNotEqual(rs.DATA_DIR.name, "MainUserStudy")

    def test_stimuli_are_read_from_data_rhythm_experiment(self):
        self.assertEqual(rs.MELODY_DIR.name, "rhythm_experiment")

    def test_no_module_here_imports_the_main_study_s_design(self):
        # Structural, not a convention: if anything here ever imports
        # app.pilot_study or app.sequence_generator, the two studies'
        # designs have started sharing code and can drift into each
        # other. Parsed rather than grepped, so prose about where this
        # was copied from doesn't count as a dependency.
        forbidden = {"pilot_study", "sequence_generator", "song_library"}
        for path in sorted((MAIN / "rhythm_study").glob("*.py")):
            for module in imported_modules(path):
                with self.subTest(module=path.name, imports=module):
                    self.assertFalse(
                        forbidden & set(module.split(".")),
                        f"{path.name} imports {module}",
                    )

    def test_nothing_in_the_package_writes_config_json(self):
        for path in sorted((MAIN / "rhythm_study").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("cfg.save()", source)
                self.assertNotIn("Config.save", source)

    def test_the_runner_keeps_results_json_an_ordinary_quiz(self):
        # app.quiz.load_quiz_results does QuizResult(**item), so an extra
        # key in results.json raises TypeError in every main-study
        # analysis window that opens one of these quizzes. The re-cue
        # record must therefore go to a sidecar file instead.
        source = (MAIN / "rhythm_study" / "runner_window.py").read_text(encoding="utf-8")
        self.assertIn("RECUE_SIDECAR_FILENAME", source)
        self.assertIn("rhythm_recues.json", source)


class TestWriteUp(unittest.TestCase):
    """The write-up is part of the deliverable, and it goes stale silently.

    The main README once described this section as "Two buttons" long after
    there were four, which is exactly the failure these check for: not
    prose quality, just that the document still names the things that
    exist.
    """

    DOC = MAIN.parent / "doc" / "RHYTHM_EXPERIMENT.md"

    def test_the_write_up_exists(self):
        self.assertTrue(self.DOC.is_file(), f"{self.DOC.name} is missing")

    def test_it_is_structured_as_introduction_method_results_discussion(self):
        text = self.DOC.read_text(encoding="utf-8")
        for heading in ("## Introduction", "## Method", "## Results", "## Discussion"):
            self.assertIn(heading, text)
        # Method must come before Results, or the numbering has been shuffled.
        self.assertLess(text.index("## Method"), text.index("## Results"))
        self.assertLess(text.index("## Results"), text.index("## Discussion"))

    def test_it_names_every_button_the_launcher_actually_has(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import launcher

        text = self.DOC.read_text(encoding="utf-8")
        section = next(tools for title, tools in launcher.SECTIONS if title.startswith("11."))
        for entry in section:
            label = entry[0]
            # The doc may shorten "(15-note)" etc., so match the stem.
            stem = label.split("(")[0].strip()
            with self.subTest(button=label):
                self.assertIn(stem, text, f"RHYTHM_EXPERIMENT.md does not mention '{stem}'")

    def test_it_records_the_design_the_code_implements(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn(str(rs.TOTAL_TRIALS), text)
        self.assertIn(str(rs.TRAINING_TRIALS), text)
        for phase in ("Training", "Probe", "Final test"):
            self.assertIn(phase, text)

    def test_it_places_this_study_among_the_platform_s_other_experiments(self):
        # This document used to open by calling the rhythm experiment "a
        # second study", which is wrong: the actuator validation
        # experiments and the tele-training work both came first. Asserted
        # positively - the doc quotes the mistaken phrase in order to
        # correct it, so a bare "not in" check would fail on the fix.
        text = self.DOC.read_text(encoding="utf-8")
        for strand in ("Validation experiments", "Main User Study", "Tele-training"):
            self.assertIn(strand, text)
        self.assertIn("validation-experiments.md", text)

    def test_it_states_the_timing_measures_are_not_interchangeable(self):
        # The one place a reader could do real damage is by subtracting a
        # training reaction time from a probe onset error, so the write-up
        # has to say that outright.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("reaction time", text)
        self.assertIn("cue/response", text)


class TestDocumentationLinks(unittest.TestCase):
    """Every relative link in the repository's documentation resolves.

    The docs were reorganised into a front door plus one document per
    subject, which means they now lean on cross-links instead of
    repeating each other. A broken link in that arrangement is not a
    cosmetic problem - it is the only route to the content.
    """

    REPO = MAIN.parent

    def docs(self):
        skip = ("__pycache__", ".pytest_cache", "main/runtime/", "archived/")
        return [
            path
            for path in sorted(self.REPO.rglob("*.md"))
            if not any(part in str(path) for part in skip)
        ]

    def test_there_are_docs_to_check(self):
        # Guards the guard: a glob that silently matches nothing would
        # make every test below pass for the wrong reason.
        self.assertGreater(len(self.docs()), 8)

    def test_every_relative_link_resolves(self):
        import re

        broken = []
        for path in self.docs():
            text = path.read_text(encoding="utf-8", errors="replace")
            for _, target in re.findall(r"\[([^\]]*)\]\(([^)]+)\)", text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                resolved = (path.parent / target.split("#")[0]).resolve()
                if not resolved.exists():
                    broken.append(f"{path.relative_to(self.REPO)} -> {target}")
        self.assertEqual(broken, [], "broken documentation links")

    def test_no_link_points_at_a_file_git_does_not_track(self):
        """A link can resolve here and still be broken for everyone else.

        `main/normalisation/` is in .gitignore, so linking its README
        worked in the working tree and 404'd in a clone - which is the
        only view a reviewer gets. Checking existence alone cannot see
        that, so this asks git what is actually in the repository.
        """
        import re
        import subprocess

        try:
            listing = subprocess.run(
                ["git", "ls-files"], cwd=self.REPO,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available")
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        tracked = set(listing.stdout.split())

        broken = []
        for path in self.docs():
            text = path.read_text(encoding="utf-8", errors="replace")
            for _, target in re.findall(r"\[([^\]]*)\]\(([^)]+)\)", text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                resolved = (path.parent / target.split("#")[0]).resolve()
                if resolved.is_dir():
                    continue
                try:
                    rel = str(resolved.relative_to(self.REPO.resolve()))
                except ValueError:
                    continue  # outside the repo, e.g. the report
                if rel not in tracked:
                    broken.append(f"{path.relative_to(self.REPO)} -> {target}")
        self.assertEqual(broken, [], "documentation links to untracked files")

    def test_the_front_door_points_at_every_study(self):
        text = (self.REPO / "README.md").read_text(encoding="utf-8")
        for doc in (
            "doc/PLATFORM.md",
            "doc/USER_MANUAL.md",
            "doc/REMOTE_GUIDANCE.md",
            "doc/RHYTHM_EXPERIMENT.md",
            "doc/SEQUENCE_GENERATOR_ALGORITHM.md",
            "doc/validation-experiments.md",
            "doc/firmware.md",
        ):
            self.assertIn(doc, text, f"the root README does not link {doc}")

    def test_no_doc_hard_codes_a_launcher_section_count(self):
        """Section counts go stale the moment a section is added.

        "nine sections" outlived three additions in the old root README,
        and "11 launcher sections" was wrong within a day of being
        written. Prose that names the sections is fine; prose that counts
        them is a promise the docs cannot keep.
        """
        import re

        offenders = []
        pattern = re.compile(
            r"\b(?:nine|ten|eleven|twelve|thirteen|\d{1,2})\s+(?:launcher\s+|numbered\s+)?sections?\b",
            re.IGNORECASE,
        )
        for path in self.docs():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = pattern.search(line)
                if match and "section" in match.group(0).lower():
                    offenders.append(f"{path.relative_to(self.REPO)}: {line.strip()[:90]}")
        self.assertEqual(offenders, [], "documentation counts launcher sections")

    def test_the_platform_readme_delegates_rather_than_repeats(self):
        # The two sections that have their own document must stay
        # orientation-sized. They were 575 and 100 lines before the
        # documents existed, which is how they went stale.
        text = (MAIN.parent / "doc" / "PLATFORM.md").read_text(encoding="utf-8")
        for heading, doc in (
            ("## Tele-training (launcher section 8)", "REMOTE_GUIDANCE.md"),
            ("## Rhythm experiment (launcher section 11)", "RHYTHM_EXPERIMENT.md"),
        ):
            with self.subTest(section=heading):
                start = text.index(heading)
                rest = text[start + len(heading):]
                nxt = rest.find("\n## ")
                body = rest if nxt < 0 else rest[:nxt]
                self.assertLess(
                    len(body.splitlines()), 80,
                    f"{heading} has grown back into a chapter; it belongs in {doc}",
                )
                self.assertIn(doc, body)


class TestMelodyDiscovery(unittest.TestCase):
    def test_an_absent_melody_folder_lists_nothing_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rs.discover_melodies(Path(tmp) / "nope"), [])

    def test_an_unknown_melody_name_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rs.RhythmStudyError):
                rs.load_trial_melody("no_such_melody", melody_dir=Path(tmp))

    def test_a_schedule_cannot_be_built_without_a_melody(self):
        with self.assertRaises(rs.RhythmStudyError):
            rs.new_trial_structure(participant={"name": "P01"}, melody="", keyboard_profile="p")


class TestRealMelody(unittest.TestCase):
    """Against whatever is actually in data/rhythm_experiment/ - skipped
    when the folder is empty, so a clean checkout still passes."""

    def setUp(self):
        names = rs.discover_melodies()
        if not names:
            self.skipTest("no melodies under data/rhythm_experiment/")
        self.name = names[0]

    def test_a_real_melody_summarises(self):
        summary = rs.melody_summary(self.name)
        self.assertGreater(summary["note_count"], 0)
        self.assertEqual(summary["name"], self.name)

    def test_a_real_melody_loads_with_fingering_the_haptic_cue_can_use(self):
        from app.haptic_cue import FINGER_TO_MOTOR

        melody = rs.load_trial_melody(self.name)
        self.assertTrue(melody.notes)
        for note in melody.notes:
            with self.subTest(note=note.event_index):
                # A finger the rig has no motor for would silently cue
                # nothing during training - the one failure mode that
                # looks like a participant simply not responding.
                self.assertIn(note.finger, FINGER_TO_MOTOR)

    def test_a_full_trial_structure_records_what_was_played(self):
        doc = rs.new_trial_structure(
            participant={"name": "P01"}, melody=self.name, keyboard_profile="test_profile"
        )
        self.assertEqual(doc["study"], "rhythm")
        self.assertEqual(doc["melody"], self.name)
        self.assertEqual(len(doc["trials"]), 19)
        # The snapshot exists so the file still says what was played even
        # if data/rhythm_experiment/ is later regenerated or cleared.
        self.assertEqual(doc["melody_summary"]["name"], self.name)
        self.assertEqual(doc["quiz_name_prefix"], "rhythm")


class FakeNoteOn:
    """One MIDI note-on, shaped like what RawMidiRecorder yields."""

    def __init__(self, note, abs_time):
        self.type = "note_on"
        self.note = note
        self.abs_time = abs_time


def make_runner(phase, backlight, haptic, melody):
    """A trial runner wired to a real melody, with no trial started.

    The camera and MIDI rig are not available in a test run; QuizWindow
    tolerates that at construction (it only fails when a quiz is
    actually started), which is what lets the cue/response logic be
    exercised here without hardware.
    """
    from app.config import Config

    from rhythm_study.cue import RhythmCue
    from rhythm_study.runner_window import RhythmRunnerWindow

    cue = RhythmCue()
    runner = RhythmRunnerWindow(Config.load(), cue)
    runner._load_melody_targets(melody)
    runner._trial = {
        "index": 1,
        "phase": phase,
        "melody": melody,
        "backlight": backlight,
        "haptic": haptic,
    }
    runner.results = []
    runner.current_index = 0
    runner._first_cue_onsets = []
    runner._recue_counts = []
    runner._note_first_cue = None
    runner._note_recues = 0
    runner.timeout_s = 5.0
    return runner, cue


class TestRecueLoop(unittest.TestCase):
    """The study's core behavioural change: a timeout re-cues the note
    instead of scoring it as a miss and moving on."""

    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        names = rs.discover_melodies()
        if not names:
            raise unittest.SkipTest("no melodies under data/rhythm_experiment/")
        cls.melody = names[0]

    def setUp(self):
        self.runner, self.cue = make_runner("training", True, True, self.melody)
        self.addCleanup(self.cue.close)
        self.addCleanup(self._close_runner)

    def _close_runner(self):
        self.runner._allow_close = True
        self.runner.close()

    def test_a_timeout_does_not_advance_the_note(self):
        self.runner._show_current_target()
        self.runner._record_result(timed_out=True)
        self.assertEqual(self.runner.current_index, 0)
        self.assertEqual(self.runner.results, [])

    def test_a_timeout_re_issues_the_cue(self):
        self.runner._show_current_target()
        first = self.runner.cue_onset_time
        self.runner._record_result(timed_out=True)
        self.assertGreater(self.runner.cue_onset_time, first)
        self.assertEqual(self.runner.phase, "presenting")

    def test_re_cues_accumulate_until_a_key_is_pressed(self):
        self.runner._show_current_target()
        for _ in range(3):
            self.runner._record_result(timed_out=True)
        self.assertEqual(self.runner._note_recues, 3)
        self.runner._record_result(
            timed_out=False, actual_note=self.runner.targets[0].note, keypress_time=time.time()
        )
        self.assertEqual(self.runner.current_index, 1)
        self.assertEqual(self.runner._recue_counts[0], 3)

    def test_both_rt_baselines_share_one_anchor(self):
        # first_cue_onset must be the same reading as the first
        # presentation's cue_onset_time, not a second time.time() taken
        # either side of the LED's serial write - otherwise "RT from the
        # first cue" and "RT from the last cue" are measured from
        # different points and stop being comparable.
        self.runner._show_current_target()
        anchor = self.runner.cue_onset_time
        self.assertEqual(self.runner._note_first_cue, anchor)
        self.runner._record_result(timed_out=True)
        self.assertEqual(self.runner._note_first_cue, anchor)
        self.runner._record_result(
            timed_out=False, actual_note=self.runner.targets[0].note, keypress_time=time.time()
        )
        self.assertEqual(self.runner._first_cue_onsets[0], anchor)

    def test_a_re_cued_note_is_not_recorded_as_timed_out(self):
        # timed_out means "no response" in every other quiz. Here the
        # note was answered, just late, so the flag must stay False or
        # every existing analysis would count it as a miss.
        self.runner._show_current_target()
        self.runner._record_result(timed_out=True)
        self.runner._record_result(
            timed_out=False, actual_note=self.runner.targets[0].note, keypress_time=time.time()
        )
        self.assertFalse(self.runner.results[0].timed_out)
        self.assertTrue(self.runner.results[0].note_correct)

    def test_a_wrong_key_still_advances(self):
        # Decision A: any press is an answer; only "no press at all"
        # re-cues. A wrong key is scored wrong and the note moves on.
        self.runner._show_current_target()
        self.runner._record_result(
            timed_out=False, actual_note=self.runner.targets[0].note + 1, keypress_time=time.time()
        )
        self.assertEqual(self.runner.current_index, 1)
        self.assertFalse(self.runner.results[0].note_correct)

    def test_bookkeeping_resets_between_notes(self):
        self.runner._show_current_target()
        self.runner._record_result(timed_out=True)
        self.runner._record_result(
            timed_out=False, actual_note=self.runner.targets[0].note, keypress_time=time.time()
        )
        self.assertIsNone(self.runner._note_first_cue)
        self.assertEqual(self.runner._note_recues, 0)


class TestTrimmedUI(unittest.TestCase):
    """The runner inherits QuizWindow's UI, which is built for someone
    choosing what to practise. In a scheduled session those choices are
    already made, so the controls for them must not be on screen."""

    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        names = rs.discover_melodies()
        if not names:
            raise unittest.SkipTest("no melodies under data/rhythm_experiment/")
        cls.melody = names[0]

    def setUp(self):
        self.runner, self.cue = make_runner("training", True, True, self.melody)
        self.addCleanup(self.cue.close)
        self.addCleanup(self._close_runner)

    def _close_runner(self):
        self.runner._allow_close = True
        self.runner.close()

    @staticmethod
    def in_layout(layout, target):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is target:
                return True
            sub = item.layout()
            if sub is not None and TestTrimmedUI.in_layout(sub, target):
                return True
        return False

    def layout(self):
        return self.runner.centralWidget().layout()

    def test_the_song_picker_is_not_on_screen(self):
        self.assertFalse(self.in_layout(self.layout(), self.runner.song_combo))

    def test_the_quiz_name_box_is_not_on_screen(self):
        self.assertFalse(self.in_layout(self.layout(), self.runner.quiz_name_edit))

    def test_the_removed_widgets_still_work_for_the_base_class(self):
        # QuizWindow._start_quiz reads the quiz name out of this box, so
        # taking it off screen must not take it out of the object.
        self.runner.quiz_name_edit.setText("rhythm-P01-T01")
        self.assertEqual(self.runner.quiz_name_edit.text(), "rhythm-P01-T01")
        self.runner.song_combo.addItem("anything")
        self.assertEqual(self.runner.song_combo.count(), 1)

    def test_the_timeout_control_stays_and_says_what_it_now_does(self):
        # It is the re-cue interval here, not a miss deadline - the old
        # "Timeout:" label would mislead an experimenter mid-session.
        from PySide6.QtWidgets import QLabel

        self.assertTrue(self.in_layout(self.layout(), self.runner.timeout_spin))
        labels = []

        def collect(layout):
            for i in range(layout.count()):
                item = layout.itemAt(i)
                widget = item.widget()
                if isinstance(widget, QLabel):
                    labels.append(widget.text())
                sub = item.layout()
                if sub is not None:
                    collect(sub)

        collect(self.layout())
        self.assertIn("Re-cue after:", labels)
        self.assertNotIn("Timeout:", labels)

    def test_no_label_still_tells_the_experimenter_to_pick_a_song(self):
        self.assertNotIn("Pick a song", self.runner.status_label.text())

    def test_the_hardware_setup_controls_are_untouched(self):
        # Only the dead controls go: the LED, MIDI port, timbre and
        # profile preview are all still needed before a session.
        for widget in (
            self.runner.port_combo,
            self.runner.led_connect_btn,
            self.runner.timbre_combo,
            self.runner.preview_profile_btn,
            self.runner.cancel_btn,
        ):
            self.assertTrue(self.in_layout(self.layout(), widget))


class PerformanceTestBase(unittest.TestCase):
    """Shared setup for the two performance phases. A probe and the final
    test are both played straight through against the melody's own time
    grid; they differ in whether the backlight shows that grid, and
    therefore in what the onsets are measured from."""

    GUIDED = True
    PHASE = "probe"
    START = 1000.0

    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        names = rs.discover_melodies()
        if not names:
            raise unittest.SkipTest("no melodies under data/rhythm_experiment/")
        cls.melody = names[0]

    def setUp(self):
        self.runner, self.cue = make_runner(self.PHASE, self.GUIDED, False, self.melody)
        self.addCleanup(self.cue.close)
        self.addCleanup(self._close_runner)
        self.runner.phase = "perform"
        self.runner._perform_mode = True
        self.runner._perform_guided = self.GUIDED
        self.runner._perform_start = self.START
        self.runner.video_start_time = self.START
        self.saved = []
        self.runner._finish_quiz = lambda: self.saved.append(True)

    def _close_runner(self):
        self.runner._allow_close = True
        self.runner.close()

    def play(self, count, wrong_at=(), late_by=None, shift=0.0):
        """Simulate a performance of `count` notes on the melody's grid.

        `shift` moves the whole performance later - a participant who
        started a beat behind, which is a different thing from playing
        the rhythm badly.
        """
        late_by = late_by or {}
        events = []
        for i in range(count):
            note = self.runner.targets[i].note
            if i in wrong_at:
                note += 1
            onset = self.START + self.runner._target_onsets[i] + shift + late_by.get(i, 0.0)
            events.append(FakeNoteOn(note, onset))
        self.runner.raw_events = events


class TestProbeIsNotAPerformance(unittest.TestCase):
    """A probe names the key and withholds the time.

    It was briefly run as a performance with the backlight walking the
    melody's grid. Tester 1's data showed that measured light-tracking
    rather than recall - their onset error started 1.6 s behind the light
    and shrank through the trial - so a probe is now note-by-note: the
    backlight lights the target, waits, and goes out at the key press.
    When to press and when to release are the participant's.
    """

    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        names = rs.discover_melodies()
        if not names:
            raise unittest.SkipTest("no melodies under data/rhythm_experiment/")
        cls.melody = names[0]

    def setUp(self):
        self.runner, self.cue = make_runner("probe", True, False, self.melody)
        self.addCleanup(self.cue.close)
        self.addCleanup(self._close_runner)
        self.runner.led_connected = True
        self.runner.led_mapper = FakeLedMapper()

    def _close_runner(self):
        self.runner._allow_close = True
        self.runner.close()

    def test_a_probe_is_note_by_note_not_a_performance(self):
        self.assertFalse(self.runner._perform_mode)

    def test_a_probe_never_holds_the_cue_for_the_beat(self):
        # Holding it would show how long the note lasts, which is exactly
        # the thing the participant is being asked to remember.
        self.assertIsNone(self.runner._sustain_length())

    def test_the_backlight_goes_out_at_the_key_press(self):
        self.runner._show_current_target()
        note = self.runner.targets[0].note
        self.assertIn(note, self.runner.led_mapper.lit)
        self.runner._record_result(
            timed_out=False, actual_note=note, keypress_time=time.time()
        )
        self.assertNotIn(note, self.runner.led_mapper.lit)
        self.assertEqual(self.runner.phase, "gap")

    def test_the_next_note_waits_for_the_participant(self):
        # No clock: the cue for note 2 must not appear on its own while
        # note 1 is still unanswered.
        self.runner._show_current_target()
        first = self.runner.cue_onset_time
        for _ in range(5):
            self.runner._tick()
            time.sleep(0.01)
        self.assertEqual(self.runner.current_index, 0)
        self.assertEqual(self.runner.cue_onset_time, first)

    def test_a_probe_delivers_no_haptic(self):
        fake = FakeHaptic()
        self.cue._haptic = fake
        self.runner._show_current_target()
        self.assertFalse(fake.on, "the haptic cue fired during a probe")


class TestFinalPerformance(PerformanceTestBase):
    """The final test: no grid is shown, so the participant's own first
    note is the reference. Their overall start time is not a rhythm
    error and must not be scored as one."""

    GUIDED = False
    PHASE = "final"

    def test_a_late_start_is_not_timing_error_when_nothing_showed_the_clock(self):
        self.play(len(self.runner.targets), shift=2.0)
        self.runner._finish_performance()
        self.assertTrue(all(abs(r.timing_error_s) < 1e-6 for r in self.runner.results))

    def test_the_rhythm_within_the_performance_is_still_scored(self):
        # Re-anchoring forgives when they started, not how they played.
        self.play(len(self.runner.targets), shift=2.0, late_by={4: 0.3})
        self.runner._finish_performance()
        self.assertAlmostEqual(self.runner.results[4].timing_error_s, 0.3, places=6)
        self.assertTrue(abs(self.runner.results[0].timing_error_s) < 1e-6)

    def test_only_the_experimenter_ends_it(self):
        # Nothing tells the participant the melody is over, and there is
        # no self-ending branch: the final test is the one trial that
        # runs until someone stops it.
        self.play(len(self.runner.targets))
        self.runner._perform_start = time.time() - 10_000.0
        for _ in range(3):
            self.runner._tick()
        self.assertEqual(self.saved, [])
        self.runner._finish_performance()
        self.assertEqual(self.saved, [True])

    def test_a_performance_with_no_presses_at_all_still_saves(self):
        self.runner.raw_events = []
        self.runner._finish_performance()
        self.assertEqual(len(self.runner.results), len(self.runner.targets))
        self.assertTrue(all(r.timed_out for r in self.runner.results))


class FakeHaptic:
    """Stands in for the vibration rig, and remembers whether it is on."""

    def __init__(self):
        self.on = False
        self.log = []
        self.closed = False

    def show_target(self, note, finger):
        self.on = True
        self.log.append(("buzz", finger))

    def clear(self):
        if self.on:
            self.log.append(("stop",))
        self.on = False

    def close(self):
        self.on = False
        self.closed = True


class FakeLedMapper:
    def __init__(self):
        self.lit = set()
        self.log = []

    def light_key(self, note):
        self.lit.add(note)
        self.log.append(("on", note))

    def clear_key(self, note):
        self.lit.discard(note)
        self.log.append(("off", note))

    def clear_all(self):
        self.lit.clear()


class TestCueSustain(unittest.TestCase):
    """A cue outlasts the key press by the note's own length.

    The melody has 1-, 2- and 3-beat notes, and how long a note lasts is
    part of what the participant has to learn. Every other quiz in this
    project clears the cue the instant a response is recorded, which
    teaches the onset only: a 3-beat note and a 1-beat note would feel
    identical. In training the buzz and the lit key therefore continue
    past the press and stop when the beat ends.
    """

    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        names = rs.discover_melodies()
        if not names:
            raise unittest.SkipTest("no melodies under data/rhythm_experiment/")
        cls.melody = names[0]

    def runner(self, phase="training", haptic=True, leds=False):
        from app.config import Config

        from rhythm_study.cue import RhythmCue
        from rhythm_study.runner_window import RhythmRunnerWindow

        self.fake = FakeHaptic()
        cue = RhythmCue(haptic=self.fake)
        runner = RhythmRunnerWindow(Config.load(), cue)
        runner._load_melody_targets(self.melody)
        runner._trial = {
            "index": 1, "phase": phase, "melody": self.melody,
            "backlight": True, "haptic": haptic,
        }
        runner.results = []
        runner.current_index = 0
        runner._first_cue_onsets = []
        runner._recue_counts = []
        runner._sustain_lengths = []
        runner._note_first_cue = None
        runner._note_recues = 0
        runner.timeout_s = 5.0
        if leds:
            runner.led_connected = True
            runner.led_mapper = FakeLedMapper()
        cue.set_phase(haptic=haptic)

        def close():
            runner._allow_close = True
            runner.close()
            cue.close()

        self.addCleanup(close)
        return runner, cue

    def press(self, runner, index=None):
        index = runner.current_index if index is None else index
        runner._record_result(
            timed_out=False,
            actual_note=runner.targets[index].note,
            keypress_time=time.time(),
        )

    # -- the buzz ------------------------------------------------------

    def test_the_buzz_survives_the_key_press(self):
        runner, _ = self.runner()
        runner._show_current_target()
        self.assertTrue(self.fake.on)
        self.press(runner)
        self.assertTrue(self.fake.on, "the motor stopped the moment the key went down")
        self.assertEqual(runner.phase, "sustain")

    def test_the_buzz_stops_when_the_beat_ends(self):
        runner, _ = self.runner()
        runner._show_current_target()
        self.press(runner)
        runner._sustain_until = time.time() - 0.01
        runner._tick()
        self.assertFalse(self.fake.on)
        self.assertEqual(runner.phase, "gap")

    def test_the_motor_is_not_stopped_and_restarted_around_the_press(self):
        # A clear() followed by a fresh show_target() would be audible as
        # a gap, and would teach a rest that is not in the melody.
        runner, _ = self.runner()
        runner._show_current_target()
        self.press(runner)
        runner._sustain_until = time.time() - 0.01
        runner._tick()
        self.assertEqual(self.fake.log, [("buzz", runner.targets[0].finger), ("stop",)])

    def test_a_longer_note_is_held_longer(self):
        # The whole point: a 2-beat note has to feel like a 2-beat note.
        runner, _ = self.runner()
        durations = runner._target_durations
        short = min(range(len(durations)), key=lambda i: durations[i])
        long = max(range(len(durations)), key=lambda i: durations[i])
        self.assertGreater(durations[long], durations[short])

        held = {}
        for index in (short, long):
            runner.current_index = index
            runner._show_current_target()
            before = time.time()
            self.press(runner, index)
            held[index] = runner._sustain_until - before
            runner._sustain_until = time.time() - 0.01
            runner._tick()
        self.assertAlmostEqual(held[short], durations[short], places=1)
        self.assertAlmostEqual(held[long], durations[long], places=1)
        self.assertGreater(held[long], held[short])

    def test_the_hold_is_measured_from_the_press_not_the_cue(self):
        # A training trial is paced by the participant, so the note's
        # slot begins when they play it. Anchoring on the cue would mean
        # a slow participant felt less of the note - or none of it.
        runner, _ = self.runner()
        runner._show_current_target()
        cue_time = runner.cue_onset_time
        time.sleep(0.15)
        press_time = time.time()
        self.press(runner)
        from_press = runner._sustain_until - press_time
        from_cue = runner._sustain_until - cue_time
        self.assertAlmostEqual(from_press, runner._target_durations[0], places=1)
        self.assertGreater(from_cue, from_press)

    # -- the LED -------------------------------------------------------

    def test_the_lit_key_stays_lit_for_the_whole_beat(self):
        runner, _ = self.runner(leds=True)
        runner._show_current_target()
        note = runner.targets[0].note
        self.assertIn(note, runner.led_mapper.lit)
        self.press(runner)
        self.assertIn(note, runner.led_mapper.lit, "the key went dark at the key press")
        runner._sustain_until = time.time() - 0.01
        runner._tick()
        self.assertNotIn(note, runner.led_mapper.lit)
        self.assertEqual(runner.led_mapper.log, [("on", note), ("off", note)])

    # -- the phases that must NOT sustain ------------------------------

    def test_a_probe_does_not_sustain(self):
        # A probe is a performance: the backlight already runs on the
        # melody's real on/off times, so there is nothing to hold.
        runner, _ = self.runner(phase="probe", haptic=False)
        self.assertIsNone(runner._sustain_length())

    def test_the_final_test_does_not_sustain(self):
        runner, _ = self.runner(phase="final", haptic=False)
        self.assertIsNone(runner._sustain_length())

    # -- nothing may leave the motor running ---------------------------

    def test_cancelling_mid_beat_stops_the_motor(self):
        runner, _ = self.runner()
        runner._show_current_target()
        self.press(runner)
        self.assertTrue(self.fake.on)
        runner._cancel_quiz()
        self.assertFalse(self.fake.on, "cancelling left the motor running")
        self.assertFalse(runner._sustaining)

    def test_finishing_a_trial_stops_the_motor(self):
        runner, _ = self.runner()
        runner._show_current_target()
        self.press(runner)
        self.assertTrue(self.fake.on)
        runner._open_analysis_window()  # what _finish_quiz calls at the end
        self.assertFalse(self.fake.on, "the last note's cue outlived its trial")
        self.assertFalse(runner._sustaining)

    def test_starting_the_next_trial_cannot_inherit_a_held_cue(self):
        from rhythm_study.cue import RhythmCue

        fake = FakeHaptic()
        cue = RhythmCue(haptic=fake)
        self.addCleanup(cue.close)
        cue.set_phase(haptic=True)
        cue.show_target(60, "L1")
        cue.begin_hold()
        cue.clear()
        self.assertTrue(fake.on)  # held
        cue.set_phase(haptic=True)  # the next trial starts
        self.assertFalse(fake.on, "a held cue survived into the next trial")

    def test_closing_the_cue_always_stops_the_motor(self):
        from rhythm_study.cue import RhythmCue

        fake = FakeHaptic()
        cue = RhythmCue(haptic=fake)
        cue.set_phase(haptic=True)
        cue.show_target(60, "L1")
        cue.begin_hold()
        cue.close()
        self.assertTrue(fake.closed)
        self.assertFalse(fake.on)

    # -- the sidecar must never escape its own quiz folder -------------

    def test_an_unnamed_trial_writes_no_sidecar_at_all(self):
        """quiz_dir("") is data/quiz itself, not a folder inside it.

        QuizWindow starts quiz_name empty, so a trial that ends before
        _start_quiz has named it would drop rhythm_recues.json straight
        into the folder every main-study quiz lives in - and lose its own
        record while doing it. This happened once for real.
        """
        from app.quiz import quiz_dir

        from rhythm_study.runner_window import RECUE_SIDECAR_FILENAME

        stray = quiz_dir("") / RECUE_SIDECAR_FILENAME
        self.assertEqual(
            quiz_dir(""), quiz_dir("x").parent,
            "the empty name no longer resolves to the shared folder; "
            "this test's premise needs rechecking",
        )
        existed = stray.exists()

        runner, _ = self.runner()
        runner._show_current_target()
        self.press(runner)
        runner.quiz_name = ""  # never named
        runner._save_recue_sidecar()

        self.assertEqual(stray.exists(), existed, f"wrote a stray sidecar to {stray}")

    # -- provenance ----------------------------------------------------

    def test_the_hold_is_recorded_per_note(self):
        runner, _ = self.runner()
        runner._show_current_target()
        self.press(runner)
        self.assertEqual(len(runner._sustain_lengths), 1)
        self.assertAlmostEqual(runner._sustain_lengths[0], runner._target_durations[0])


class TestCueSwitching(unittest.TestCase):
    """The cue is a switch, not a screen: it must not reach for the
    vibration rig on a trial that has no haptic guidance."""

    def test_a_fresh_cue_has_not_connected_to_anything(self):
        from rhythm_study.cue import RhythmCue

        cue = RhythmCue()
        self.addCleanup(cue.close)
        self.assertFalse(cue.haptic_connected)

    def test_a_haptic_free_phase_never_connects_the_rig(self):
        # Probes and the final test must run on a machine with no rig
        # attached; only training needs it.
        from rhythm_study.cue import RhythmCue

        cue = RhythmCue()
        self.addCleanup(cue.close)
        cue.set_phase(haptic=False)
        self.assertFalse(cue.haptic_connected)
        cue.show_target(60, "L1")  # must be a no-op, not a crash
        cue.clear()

    def test_a_haptic_phase_delivers_to_the_rig(self):
        from rhythm_study.cue import RhythmCue

        class FakeHaptic:
            def __init__(self):
                self.shown = []
                self.cleared = 0
                self.closed = False

            def show_target(self, note, finger):
                self.shown.append((note, finger))

            def clear(self):
                self.cleared += 1

            def close(self):
                self.closed = True

        fake = FakeHaptic()
        cue = RhythmCue(haptic=fake)
        cue.set_phase(haptic=True)
        cue.show_target(60, "L1")
        self.assertEqual(fake.shown, [(60, "L1")])
        cue.close()
        self.assertTrue(fake.closed)

    def test_the_cue_has_no_participant_facing_window(self):
        # The main study's ExperimentCue owns a CueWindow; this study has
        # no visual guidance, so acquiring one would be a design change.
        from rhythm_study.cue import RhythmCue

        cue = RhythmCue()
        self.addCleanup(cue.close)
        self.assertFalse(hasattr(cue, "window"))


if __name__ == "__main__":
    unittest.main()
