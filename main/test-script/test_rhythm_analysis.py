"""Tests for the rhythm experiment's analysis pipeline.

Run from main/:  python test-script/test_rhythm_analysis.py

There is no recorded rhythm data yet, so these build synthetic sessions
with known properties and check the pipeline recovers them. That is the
point: a metric that cannot recover an effect deliberately planted in
its input will not be trusted to find a real one.

The properties planted, and what each test holds the pipeline to:

* finger accuracy rising across the three probes -> the Friedman test
  finds it and the post-hoc pairs point the right way;
* onset error shrinking across the probes -> the same, in milliseconds;
* held notes of 1, 2 and 3 beats -> duration error is reconstructed from
  the raw MIDI log, which is the only place note-offs exist;
* notes never played -> they still count against accuracy.
"""

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.quiz as app_quiz  # noqa: E402
from app.quiz import QuizMeta  # noqa: E402
from rhythm_study import analysis as ra  # noqa: E402
from rhythm_study import schedule as rs  # noqa: E402

FINGERS = ("L1", "L2", "L3", "L4", "L5", "R1", "R2", "R3", "R4", "R5")


class SyntheticStudy:
    """A temp tree holding whole rhythm sessions, wired up so the analysis
    reads it instead of the real data folders."""

    def __init__(self, melody_name):
        self.root = Path(tempfile.mkdtemp())
        self.melody_name = melody_name
        self.melody = rs.load_trial_melody(melody_name)
        self._patches = []

    def install(self, test):
        """Redirect every data lookup at this tree for one test."""
        originals = {
            "quiz_dir": ra.quiz_dir,
            "quiz_raw_dir": ra.quiz_raw_dir,
            "load_trial_structure": ra.load_trial_structure,
            "list_participants": ra.list_participants,
            "QUIZ_DATA_DIR": app_quiz.QUIZ_DATA_DIR,
        }
        root = self.root
        ra.quiz_dir = lambda name, data_dir=None: root / "quiz" / name
        ra.quiz_raw_dir = lambda name, data_dir=None: root / "quiz" / name / "raw"
        ra.load_trial_structure = lambda name: rs.load_trial_structure(
            name, data_dir=root / "RhythmStudy"
        )
        ra.list_participants = lambda: rs.list_participants(data_dir=root / "RhythmStudy")
        app_quiz.QUIZ_DATA_DIR = root / "quiz"

        def restore():
            ra.quiz_dir = originals["quiz_dir"]
            ra.quiz_raw_dir = originals["quiz_raw_dir"]
            ra.load_trial_structure = originals["load_trial_structure"]
            ra.list_participants = originals["list_participants"]
            app_quiz.QUIZ_DATA_DIR = originals["QUIZ_DATA_DIR"]

        test.addCleanup(restore)

    # -- building ------------------------------------------------------

    def add_participant(
        self,
        name,
        *,
        probe_finger=(0.55, 0.72, 0.86),
        probe_onset_sd=(0.26, 0.16, 0.10),
        final_finger=0.78,
        final_onset_sd=0.19,
        seed=0,
        analysed=True,
        played_notes=None,
        duration_jitter=0.10,
    ):
        rng = random.Random(seed)
        notes = self.melody.notes
        trials = rs.build_schedule(self.melody_name)
        limit = len(notes) if played_notes is None else played_notes

        for trial in trials:
            trial["status"] = rs.TRIAL_STATUS_COMPLETED
            quiz_name = rs.trial_quiz_base_name(name, trial["index"])
            trial["quiz_name"] = quiz_name
            trial["quiz_attempts"] = [quiz_name]
            self._write_trial(
                name, trial, quiz_name, rng, probe_finger, probe_onset_sd,
                final_finger, final_onset_sd, analysed, limit, duration_jitter,
            )

        folder = self.root / "RhythmStudy" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / rs.TRIAL_STRUCTURE_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "study": "rhythm",
                    "participant": {"name": name},
                    "melody": self.melody_name,
                    "trials": trials,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_trial(self, name, trial, quiz_name, rng, probe_finger, probe_onset_sd,
                     final_finger, final_onset_sd, analysed, limit, duration_jitter):
        notes = self.melody.notes
        phase, number = trial["phase"], trial["phase_number"]
        if phase == rs.PHASE_TRAINING:
            p_finger = 0.35 + 0.55 * (number - 1) / 14
            onset_sd = None
        elif phase == rs.PHASE_PROBE:
            p_finger, onset_sd = probe_finger[number - 1], probe_onset_sd[number - 1]
        else:
            p_finger, onset_sd = final_finger, final_onset_sd

        base = 1_800_000_000.0 + trial["index"] * 1000
        results, raw = [], []
        for i, note in enumerate(notes):
            played = i < limit
            key_ok = played and rng.random() < min(0.99, p_finger + 0.08)
            finger_ok = key_ok and rng.random() < 0.92
            actual_note = note.midi_note if key_ok else note.midi_note + 1

            if onset_sd is None:
                due = base + i * 2.0
                press = due + abs(rng.gauss(0.6, 0.15))
            else:
                due = base + note.note_on_time_sec
                press = due + rng.gauss(0.0, onset_sd)

            if played:
                target_duration = note.note_off_time_sec - note.note_on_time_sec
                release = press + max(0.05, target_duration + rng.gauss(0.0, duration_jitter))
                raw.append({"abs_time": press, "type": "note_on",
                            "note": actual_note, "velocity": 80})
                raw.append({"abs_time": release, "type": "note_off",
                            "note": actual_note, "velocity": 0})

            results.append({
                "index": i,
                "target_note": note.midi_note,
                "target_note_name": note.note_name,
                "target_key_id": i,
                "target_finger": note.finger,
                "cue_onset_time": due,
                "timed_out": not played,
                "actual_note": actual_note if played else None,
                "actual_key_id": i if played else None,
                "keypress_time": press if played else None,
                "timing_error_s": (press - due) if played else None,
                "note_correct": bool(key_ok),
                "actual_finger": (note.finger if finger_ok else
                                  rng.choice([f for f in FINGERS if f != note.finger]))
                                 if played else None,
                "finger_correct": bool(finger_ok) if played else None,
                "finger_probabilities": None,
                "target_finger_probability": None,
                "actual_finger_point": None,
                "validity": "valid",
                "finger_corrected": False,
                "finger_reviewed": True,
            })

        folder = self.root / "quiz" / quiz_name
        (folder / "raw").mkdir(parents=True, exist_ok=True)
        (folder / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        (folder / "raw" / "midi_raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
        (folder / "rhythm_recues.json").write_text(
            json.dumps({
                "quiz_name": quiz_name,
                "trial_index": trial["index"],
                "phase": phase,
                "notes": [{"index": r["index"],
                           "first_cue_onset_time": r["cue_onset_time"],
                           "last_cue_onset_time": r["cue_onset_time"],
                           "recue_count": 1 if phase == rs.PHASE_TRAINING else 0}
                          for r in results],
            }, indent=2),
            encoding="utf-8",
        )
        hits = sum(1 for r in results if r["note_correct"])
        QuizMeta(
            quiz_name=quiz_name, song_name=self.melody_name,
            keyboard_profile_name="test", port_name=None, created_at=base,
            timeout_s=5.0, note_count=len(results), hits=hits,
            misses=len(results) - hits, note_accuracy=hits / len(results),
            mean_timing_error_s=0.0, finger_accuracy=None,
            guidance_type={"training": "haptic", "probe": "backlight",
                           "final": "unguided"}[phase],
            analyzed=analysed,
        ).save(folder / "meta.json")


def make_study(test, participants=8, **kwargs):
    names = rs.discover_melodies()
    if not names:
        raise unittest.SkipTest("no melodies under data/rhythm_experiment/")
    study = SyntheticStudy(names[0])
    study.install(test)
    rng = random.Random(11)
    people = [f"P{i:02d}" for i in range(1, participants + 1)]
    for index, person in enumerate(people):
        offset = rng.uniform(-0.10, 0.10)
        study.add_participant(
            person, seed=index,
            probe_finger=tuple(min(0.97, max(0.05, v + offset))
                               for v in (0.55, 0.72, 0.86)),
            **kwargs,
        )
    return study, people


class TestEventExtraction(unittest.TestCase):
    def setUp(self):
        self.study, self.people = make_study(self, participants=2)
        self.events = ra.collect_participant(self.people[0])

    def test_there_is_one_row_per_target_note_not_per_key_press(self):
        n_notes = len(self.study.melody.notes)
        self.assertEqual(len(self.events), rs.TOTAL_TRIALS * n_notes)

    def test_note_offs_are_recovered_from_the_raw_midi_log(self):
        # results.json has no note-off at all, so a duration here proves
        # the raw log was read and paired.
        played = [e for e in self.events if e["played"]]
        self.assertTrue(played)
        self.assertTrue(all(e["actual_duration_s"] is not None for e in played))

    def test_the_target_duration_comes_from_the_melody_s_own_grid(self):
        beats = {e["target_duration_beats"] for e in self.events}
        self.assertEqual(beats, {1.0, 2.0, 3.0})
        for event in self.events:
            if event["target_duration_beats"] == 2.0:
                # 60 BPM: one beat is one second, and the note sounds for
                # all but the release gap.
                self.assertGreater(event["target_duration_s"], 1.0)

    def test_training_yields_a_reaction_time_and_no_onset_error(self):
        training = [e for e in self.events if e["phase"] == rs.PHASE_TRAINING]
        self.assertTrue(training)
        self.assertTrue(all(e["signed_onset_error_ms"] is None for e in training))
        self.assertTrue(all(e["rt_ms"] is not None for e in training if e["played"]))

    def test_a_performance_yields_an_onset_error_and_no_reaction_time(self):
        performances = [e for e in self.events if e["is_performance"]]
        self.assertTrue(performances)
        self.assertTrue(all(e["rt_ms"] is None for e in performances))
        self.assertTrue(
            all(e["signed_onset_error_ms"] is not None for e in performances if e["played"])
        )

    def test_absolute_error_is_the_magnitude_of_the_signed_one(self):
        for event in self.events:
            if event["signed_onset_error_ms"] is not None:
                self.assertAlmostEqual(
                    event["absolute_onset_error_ms"], abs(event["signed_onset_error_ms"])
                )

    def test_every_event_carries_the_phase_and_repetition_number(self):
        for event in self.events:
            self.assertIn(event["phase"], (rs.PHASE_TRAINING, rs.PHASE_PROBE, rs.PHASE_FINAL))
            self.assertGreaterEqual(event["phase_number"], 1)


class TestAccuracyDenominators(unittest.TestCase):
    def test_a_note_never_played_counts_against_accuracy(self):
        # The tempting bug is to average over the notes that have a
        # verdict, which would score a participant who played 5 of 15
        # notes the same as one who played all 15 equally well.
        study = SyntheticStudy(rs.discover_melodies()[0])
        study.install(self)
        study.add_participant("P01", seed=1, played_notes=5)
        events = ra.collect_participant("P01")
        trials = ra.trial_summaries(events)
        n_notes = len(study.melody.notes)
        for row in trials:
            self.assertEqual(row["n_events"], n_notes)
            self.assertEqual(row["n_played"], 5)
            self.assertLessEqual(row["key_accuracy"], 5 / n_notes)
            self.assertLessEqual(row["finger_accuracy"], 5 / n_notes)


class TestStrictFingerAnalysis(unittest.TestCase):
    def test_an_unanalysed_trial_stops_the_analysis_and_names_itself(self):
        study = SyntheticStudy(rs.discover_melodies()[0])
        study.install(self)
        study.add_participant("P01", seed=1, analysed=False)
        with self.assertRaises(ra.RhythmAnalysisError) as caught:
            ra.collect_participant("P01")
        self.assertIn("finger analysis", str(caught.exception))


class TestProbeStatistics(unittest.TestCase):
    def setUp(self):
        self.study, self.people = make_study(self, participants=10)
        self.result = ra.analyse(self.people)

    def test_it_recovers_the_planted_rise_in_finger_accuracy(self):
        test = self.result.probe_tests["finger_accuracy"]
        means = [test["per_probe"][label]["mean"] for label in ra.PROBE_LABELS]
        self.assertLess(means[0], means[1])
        self.assertLess(means[1], means[2])
        self.assertTrue(test["ran"])
        self.assertLess(test["p"], 0.05)

    def test_it_recovers_the_planted_fall_in_onset_error(self):
        test = self.result.probe_tests["mean_absolute_onset_error_ms"]
        means = [test["per_probe"][label]["mean"] for label in ra.PROBE_LABELS]
        self.assertGreater(means[0], means[1])
        self.assertGreater(means[1], means[2])
        self.assertTrue(test["ran"])
        self.assertLess(test["p"], 0.05)

    def test_the_onset_error_is_in_milliseconds(self):
        # Planted as a 260 ms SD at Probe 1; a half-normal's mean
        # magnitude is about 0.8 SD, so a value in seconds or a
        # signed-mean-instead-of-absolute bug is caught here.
        test = self.result.probe_tests["mean_absolute_onset_error_ms"]
        self.assertGreater(test["per_probe"]["Probe 1"]["mean"], 100.0)
        self.assertLess(test["per_probe"]["Probe 1"]["mean"], 400.0)

    def test_the_unit_of_inference_is_the_participant(self):
        # n must be the number of people, never the number of events.
        test = self.result.probe_tests["finger_accuracy"]
        self.assertEqual(test["n"], len(self.people))
        self.assertEqual(len(test["participants"]), len(self.people))

    def test_post_hoc_pairs_are_holm_corrected_and_never_smaller_than_raw(self):
        test = self.result.probe_tests["finger_accuracy"]
        self.assertEqual(len(test["posthoc"]), 3)
        for pair in test["posthoc"]:
            self.assertGreaterEqual(pair["p_holm"], pair["p"] - 1e-12)
            self.assertIsNotNone(pair["effect_size_rank_biserial"])
            self.assertEqual(len(pair["ci95_mean_difference"]), 2)

    def test_a_confidence_interval_brackets_its_own_point_estimate(self):
        for test in self.result.probe_tests.values():
            for pair in test.get("posthoc", []):
                low, high = pair["ci95_mean_difference"]
                if low is None:
                    continue
                self.assertLessEqual(low, pair["mean_difference"])
                self.assertLessEqual(pair["mean_difference"], high)

    def test_a_tiny_sample_is_described_but_not_tested(self):
        study = SyntheticStudy(rs.discover_melodies()[0])
        study.install(self)
        for index, person in enumerate(["P01", "P02"]):
            study.add_participant(person, seed=index)
        result = ra.analyse(["P01", "P02"])
        test = result.probe_tests["finger_accuracy"]
        self.assertFalse(test["ran"])
        self.assertIn("no test run", test["note"])
        self.assertTrue(test["per_probe"]["Probe 1"]["n"])


class TestWithdrawalCost(unittest.TestCase):
    def setUp(self):
        self.study, self.people = make_study(self, participants=6)
        self.result = ra.analyse(self.people)

    def test_it_is_the_probe_minus_the_training_trial_before_it(self):
        cost = self.result.withdrawal["finger_accuracy"]
        self.assertEqual(cost["pairs"], ["Training 5 -> Probe 1",
                                         "Training 10 -> Probe 2",
                                         "Training 15 -> Probe 3"])
        by_key = {(r["participant"], r["phase"], r["phase_number"]): r["finger_accuracy"]
                  for r in self.result.trials}
        person = self.people[0]
        expected = (by_key[(person, rs.PHASE_PROBE, 1)]
                    - by_key[(person, rs.PHASE_TRAINING, 5)])
        self.assertAlmostEqual(cost["per_participant"][person][0], expected)

    def test_timing_is_refused_because_the_two_phases_measure_different_things(self):
        with self.assertRaises(ra.RhythmAnalysisError) as caught:
            ra.withdrawal_costs(self.result.trials, "mean_absolute_onset_error_ms")
        self.assertIn("reaction time", str(caught.exception))

    def test_the_refusal_is_stated_in_the_reported_output_too(self):
        self.assertIn("reaction time", self.result.withdrawal["finger_accuracy"]["timing_note"])
        self.assertIn("No timing withdrawal cost", ra.summary_text(self.result))


class TestFinalTest(unittest.TestCase):
    def setUp(self):
        self.study, self.people = make_study(self, participants=8)
        self.result = ra.analyse(self.people)

    def test_the_final_test_is_never_labelled_a_fourth_probe(self):
        test = self.result.final_tests["finger_accuracy"]
        self.assertIn("transfer", test["comparison"].lower())
        probe = self.result.probe_tests["finger_accuracy"]
        self.assertEqual(len(probe["per_probe"]), 3)

    def test_it_compares_the_final_against_probe_three_only(self):
        test = self.result.final_tests["mean_absolute_onset_error_ms"]
        self.assertEqual(test["n"], len(self.people))
        self.assertTrue(test["probe3"]["n"])
        self.assertTrue(test["final"]["n"])

    def test_the_summary_warns_that_two_channels_were_removed(self):
        text = ra.summary_text(self.result)
        self.assertIn("not a fourth probe", text)
        self.assertIn("backlight", text)


class TestCombinedScore(unittest.TestCase):
    def test_the_onset_tolerance_is_configurable_and_actually_used(self):
        study = SyntheticStudy(rs.discover_melodies()[0])
        study.install(self)
        study.add_participant("P01", seed=3)

        strict = ra.collect_participant("P01", ra.AnalysisConfig(onset_tolerance_ms=1.0))
        loose = ra.collect_participant("P01", ra.AnalysisConfig(onset_tolerance_ms=5000.0))
        strict_rate = sum(1 for e in strict if e["complete_success"])
        loose_rate = sum(1 for e in loose if e["complete_success"])
        self.assertLess(strict_rate, loose_rate)

    def test_the_default_tolerance_is_a_starting_value_not_a_hard_coded_rule(self):
        self.assertEqual(ra.AnalysisConfig().onset_tolerance_ms, 200.0)
        self.assertEqual(ra.AnalysisConfig(onset_tolerance_ms=50.0).onset_tolerance_ms, 50.0)


class TestOutputs(unittest.TestCase):
    def setUp(self):
        self.study, self.people = make_study(self, participants=6)
        self.result = ra.analyse(self.people)
        self.out = Path(tempfile.mkdtemp())

    def test_it_writes_the_event_participant_and_statistics_files(self):
        written = ra.export(self.result, self.out)
        names = {p.name for p in written}
        self.assertIn(ra.EVENT_CSV, names)
        self.assertIn(ra.PARTICIPANT_CSV, names)
        self.assertIn(ra.STATS_JSON, names)
        self.assertIn(ra.SUMMARY_TXT, names)
        for path in written:
            self.assertGreater(path.stat().st_size, 0)

    def test_the_statistics_file_is_valid_json(self):
        ra.export(self.result, self.out)
        doc = json.loads((self.out / ra.STATS_JSON).read_text(encoding="utf-8"))
        self.assertIn("probe_tests", doc)
        self.assertIn("withdrawal_cost", doc)
        self.assertEqual(doc["config"]["onset_tolerance_ms"], 200.0)

    def test_every_figure_renders(self):
        from rhythm_study import analysis_figures

        made = analysis_figures.render_all(self.result, self.out)
        self.assertEqual(len(made), len(analysis_figures.FIGURE_FILENAMES))
        for path in made:
            self.assertGreater(path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
