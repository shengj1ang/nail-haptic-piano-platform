"""Unit tests for app.participant_analysis (the computation layer behind
the Participant Analysis window's Trade-off / Errors / Confusion tabs)
plus the FA-main denominator invariant in app.quiz.summarize.

Run from python-code/:  python test-script/test_participant_analysis.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.participant_analysis import (  # noqa: E402
    CAT_CK_CF,
    CAT_CK_WF,
    CAT_NO_RESPONSE,
    CAT_UNRESOLVED,
    CAT_WK_CF,
    CAT_WK_WF,
    classify_event_outcome,
    compute_error_breakdown,
    compute_finger_confusion,
    compute_trial_speed_accuracy,
    compute_wrong_key_distance,
    confusion_matrix,
    confusion_pair_count,
    relative_reduction,
    top_confusion,
    wrong_key_stats,
)
from app.quiz import QuizResult, summarize  # noqa: E402


def event(**kw) -> dict:
    base = {
        "condition": "B",
        "timed_out": False,
        "key_correct": True,
        "finger_correct": True,
        "target_finger": "R2",
        "actual_finger": "R2",
        "target_note": 60,
        "actual_note": 60,
        "target_key_id": 5,
        "actual_key_id": 5,
        "rt_s": 0.5,
    }
    base.update(kw)
    return base


class TestClassification(unittest.TestCase):
    def test_correct_key_correct_finger(self):
        self.assertEqual(classify_event_outcome(event()), CAT_CK_CF)

    def test_correct_key_wrong_finger(self):
        self.assertEqual(classify_event_outcome(event(finger_correct=False, actual_finger="R3")), CAT_CK_WF)

    def test_wrong_key_correct_finger(self):
        self.assertEqual(classify_event_outcome(event(key_correct=False)), CAT_WK_CF)

    def test_wrong_key_wrong_finger(self):
        self.assertEqual(
            classify_event_outcome(event(key_correct=False, finger_correct=False, actual_finger="L1")),
            CAT_WK_WF,
        )

    def test_unresolved_finger(self):
        self.assertEqual(
            classify_event_outcome(event(actual_finger=None, finger_correct=False)), CAT_UNRESOLVED
        )
        # A missing verdict is unscorable too, never silently "wrong finger".
        self.assertEqual(classify_event_outcome(event(finger_correct=None)), CAT_UNRESOLVED)

    def test_timeout_is_its_own_category(self):
        self.assertEqual(
            classify_event_outcome(event(timed_out=True, actual_finger=None, finger_correct=None)),
            CAT_NO_RESPONSE,
        )

    def test_breakdown_counts_are_exclusive_and_complete(self):
        events = [
            event(),
            event(finger_correct=False, actual_finger="R3"),
            event(key_correct=False),
            event(timed_out=True, actual_finger=None, finger_correct=None),
        ]
        df = compute_error_breakdown(events)
        counts = df[df["condition"] == "B"].set_index("category")["count"]
        self.assertEqual(counts.sum(), len(events))
        self.assertEqual(counts[CAT_CK_CF], 1)
        self.assertEqual(counts[CAT_CK_WF], 1)
        self.assertEqual(counts[CAT_WK_CF], 1)
        self.assertEqual(counts[CAT_NO_RESPONSE], 1)


class TestTradeoff(unittest.TestCase):
    def _trial(self, **kw):
        base = {
            "condition": "B",
            "trial_index": 1,
            "level": "alpha",
            "sequence": "s",
            "analyzed": True,
            "fa_main": 0.5,
            "rt_correct_key_s": 0.5,
        }
        base.update(kw)
        return base

    def test_trial_without_valid_rt_is_excluded_not_zero(self):
        df = compute_trial_speed_accuracy([self._trial(), self._trial(rt_correct_key_s=None)])
        self.assertEqual(len(df), 2)  # the excluded trial is still a (flagged) row
        included = df[df["included"]]
        self.assertEqual(len(included), 1)
        self.assertTrue((included["rt_ms"] > 0).all())
        self.assertTrue(df[~df["included"]]["rt_ms"].isna().all())  # counted, never plotted as 0

    def test_relative_reduction_guard_when_b_is_zero(self):
        self.assertIsNone(relative_reduction(0, 5))
        self.assertAlmostEqual(relative_reduction(10, 4), 0.6)


class TestFaMainDenominator(unittest.TestCase):
    def test_fa_main_denominator_is_all_target_events(self):
        def result(i, timed_out=False, note_ok=True, finger_ok=True):
            return QuizResult(
                index=i, target_note=60, target_note_name="C4", target_key_id=0,
                target_finger="R1", cue_onset_time=0.0, timed_out=timed_out,
                actual_note=None if timed_out else 60,
                note_correct=(not timed_out) and note_ok,
                actual_finger=None if timed_out else "R1",
                finger_correct=None if timed_out else finger_ok,
                timing_error_s=None if timed_out else 0.5,
            )

        # 3 target events: 1 complete-correct, 1 timeout, 1 wrong finger.
        s = summarize([result(0), result(1, timed_out=True), result(2, finger_ok=False)])
        self.assertAlmostEqual(s["fa_main"], 1 / 3)  # NOT 1/2 over responded


class TestConfusion(unittest.TestCase):
    def test_cross_hand_and_unresolved(self):
        events = [
            event(target_finger="R4", actual_finger="R5", finger_correct=False),
            event(target_finger="R4", actual_finger="R5", finger_correct=False),
            event(target_finger="L2", actual_finger="R2", finger_correct=False),  # cross-hand
            event(target_finger="R4", actual_finger=None, finger_correct=False),
            event(condition="C"),  # other condition, ignored
        ]
        data = confusion_matrix(compute_finger_confusion(events), "B")
        self.assertEqual(data["total"], 4)
        self.assertEqual(top_confusion(data["matrix"]), {"target": "R4", "actual": "R5", "n": 2})
        self.assertEqual(data["unresolved"]["R4"], 1)
        self.assertEqual(confusion_pair_count(data["matrix"], "R4", "R5"), 2)
        self.assertEqual(confusion_pair_count(data["matrix"], "L2", "R2"), 1)


class TestWrongKeyDistance(unittest.TestCase):
    def test_uses_calibrated_key_order_not_semitones(self):
        # Keys 2 and 5 in profile order but 5 semitones apart in MIDI.
        e = event(key_correct=False, target_note=60, actual_note=65, target_key_id=2, actual_key_id=5)
        df = compute_wrong_key_distance([e])
        self.assertEqual(list(df["distance"]), [3])
        self.assertEqual(wrong_key_stats(df).iloc[0]["metric"], "key-index")

    def test_falls_back_to_semitones_without_key_ids(self):
        e = event(key_correct=False, target_note=60, actual_note=65, target_key_id=None, actual_key_id=None)
        df = compute_wrong_key_distance([e])
        self.assertEqual(list(df["distance"]), [5])
        self.assertEqual(wrong_key_stats(df).iloc[0]["metric"], "semitones")

    def test_correct_and_timeout_events_are_ignored(self):
        self.assertTrue(compute_wrong_key_distance([event(), event(timed_out=True, actual_note=None)]).empty)

    def test_participant_column_passes_through_for_group_use(self):
        df = compute_wrong_key_distance([event(key_correct=False, participant="P07")])
        self.assertEqual(df.iloc[0]["participant"], "P07")


if __name__ == "__main__":
    unittest.main(verbosity=2)
