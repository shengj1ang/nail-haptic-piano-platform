"""Unit tests for the meta.json summary cache (app.quiz.apply_summary /
save_quiz_summary).

The five headline fields in QuizMeta mirror what summarize() derives from
results.json. Nothing reads them back - the analysis table, the
participant export and Group Analysis all recompute from the per-event
data - but every path that edits an event after the analysis pass is
expected to re-derive them, so a quiz folder never carries numbers that
contradict its own events. These tests pin that invariant, which used to
depend on remembering to press "Analyze selected (data only)".

Run from main/:  python test-script/test_quiz_summary_cache.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import quiz as qz  # noqa: E402
from app.quiz import (  # noqa: E402
    META_FILENAME,
    RESULTS_FILENAME,
    VALIDITY_INVALID_CARRYOVER,
    QuizMeta,
    QuizResult,
    apply_summary,
    full_summary,
    save_quiz_results,
    save_quiz_summary,
)

QUIZ_NAME = "P99_test_quiz"


def result(index: int, **kw) -> QuizResult:
    """One responded, fully correct event; override to make it miss."""
    base = dict(
        index=index,
        target_note=60 + index,
        target_note_name="C4",
        target_key_id=index,
        target_finger="R2",
        cue_onset_time=1000.0 + index,
        timed_out=False,
        actual_note=60 + index,
        actual_key_id=index,
        keypress_time=1000.4 + index,
        timing_error_s=0.4,
        note_correct=True,
        actual_finger="R2",
        finger_correct=True,
        finger_probabilities={"R2": 0.9, "R3": 0.1},
        target_finger_probability=0.9,
    )
    base.update(kw)
    return QuizResult(**base)


def meta(**kw) -> QuizMeta:
    base = dict(
        quiz_name=QUIZ_NAME,
        song_name="test_song",
        keyboard_profile_name="test_profile",
        port_name=None,
        created_at=1000.0,
        timeout_s=2.0,
        note_count=4,
        hits=0,
        misses=0,
        note_accuracy=0.0,
        mean_timing_error_s=None,
        finger_accuracy=None,
        guidance_type="visual",
        analyzed=True,
    )
    base.update(kw)
    return QuizMeta(**base)


class TestApplySummary(unittest.TestCase):
    """apply_summary copies exactly the five cached fields, nothing else."""

    def test_copies_the_headline_numbers(self):
        results = [result(0), result(1), result(2, timed_out=True, actual_note=None,
                                              keypress_time=None, timing_error_s=None,
                                              note_correct=False, actual_finger=None,
                                              finger_correct=None, finger_probabilities=None,
                                              target_finger_probability=None)]
        m = meta()
        s = qz.summarize(results)
        apply_summary(m, s)
        self.assertEqual(m.hits, 2)
        self.assertEqual(m.misses, 1)
        self.assertAlmostEqual(m.note_accuracy, 2 / 3)
        self.assertAlmostEqual(m.mean_timing_error_s, 0.4)
        self.assertAlmostEqual(m.finger_accuracy, 2 / 3)

    def test_leaves_the_identity_fields_alone(self):
        """guidance_type/note_count/analyzed are the fields the participant
        export actually reads out of meta.json - a re-cache must not move
        them."""
        m = meta(guidance_type="haptic", note_count=4, analyzed=True)
        apply_summary(m, qz.summarize([result(0)]))
        self.assertEqual(m.guidance_type, "haptic")
        self.assertEqual(m.note_count, 4)
        self.assertTrue(m.analyzed)
        self.assertEqual(m.quiz_name, QUIZ_NAME)


class TestSaveQuizSummary(unittest.TestCase):
    """save_quiz_summary re-derives meta.json from results.json on disk -
    the call every results.json writer in the audit path makes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.dir = root / QUIZ_NAME
        (self.dir / "raw").mkdir(parents=True)
        # quiz_dir/quiz_raw_dir bind QUIZ_DATA_DIR as a default argument at
        # import time, so redirecting the module constant wouldn't take -
        # patch the two lookups save_quiz_summary goes through instead.
        self._saved = (qz.quiz_dir, qz.quiz_raw_dir)
        qz.quiz_dir = lambda name, data_dir=None: root / name
        qz.quiz_raw_dir = lambda name, data_dir=None: root / name / "raw"
        self.addCleanup(self._restore)

    def _restore(self):
        qz.quiz_dir, qz.quiz_raw_dir = self._saved
        self._tmp.cleanup()

    def _write(self, results, m=None):
        save_quiz_results(results, self.dir / RESULTS_FILENAME)
        (m or meta()).save(self.dir / META_FILENAME)

    def _stored(self) -> dict:
        with open(self.dir / META_FILENAME, encoding="utf-8") as f:
            return json.load(f)

    def test_stale_cache_catches_up_with_a_corrected_finger(self):
        """The regression this replaces: a finger corrected in the review
        window used to leave meta.json quoting the pre-correction FA."""
        results = [result(i) for i in range(4)]
        results[0].finger_correct = False
        results[0].actual_finger = "R3"
        self._write(results)
        save_quiz_summary(QUIZ_NAME)
        self.assertAlmostEqual(self._stored()["finger_accuracy"], 0.75)

        results[0].actual_finger = "R2"  # human overrules the detector
        results[0].finger_correct = True
        results[0].finger_corrected = True
        save_quiz_results(results, self.dir / RESULTS_FILENAME)
        save_quiz_summary(QUIZ_NAME)
        self.assertAlmostEqual(self._stored()["finger_accuracy"], 1.0)

    def test_confirmed_carryover_moves_every_cached_number(self):
        """An excluded event leaves summarize()'s denominator, so the cache
        has to be re-derived for the carry-over verdict too."""
        results = [result(i) for i in range(4)]
        results[3].validity = VALIDITY_INVALID_CARRYOVER
        self._write(results)
        save_quiz_summary(QUIZ_NAME)
        stored = self._stored()
        self.assertEqual(stored["hits"], 3)
        self.assertAlmostEqual(stored["note_accuracy"], 1.0)  # 3/3, not 3/4

    def test_matches_what_the_table_and_the_export_recompute(self):
        """The cache must never disagree with the live computation those
        two read - same numbers, same source."""
        results = [result(i) for i in range(4)]
        results[2].note_correct = False
        results[2].actual_note = 71
        results[2].finger_correct = False
        self._write(results)
        save_quiz_summary(QUIZ_NAME)
        stored = self._stored()
        live = full_summary(QUIZ_NAME, results)
        for field in ("hits", "misses", "note_accuracy", "mean_timing_error_s", "finger_accuracy"):
            self.assertEqual(stored[field], live[field], field)

    def test_does_not_touch_results_json(self):
        results = [result(i) for i in range(4)]
        self._write(results)
        before = (self.dir / RESULTS_FILENAME).read_bytes()
        save_quiz_summary(QUIZ_NAME)
        self.assertEqual((self.dir / RESULTS_FILENAME).read_bytes(), before)

    def test_keeps_the_not_analyzed_flag(self):
        """A quiz whose video pass never ran still has real key/timing
        numbers to cache; re-caching them must not claim it was analyzed."""
        self._write([result(i) for i in range(4)], meta(analyzed=False, finger_accuracy=None))
        save_quiz_summary(QUIZ_NAME)
        self.assertFalse(self._stored()["analyzed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
