"""Unit tests for app.participant_analysis (the computation layer behind
the Participant Analysis window's Trade-off / Errors / Confusion tabs)
plus the FA-main denominator invariant in app.quiz.summarize.

Run from main/:  python test-script/test_participant_analysis.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import group_analysis as ga  # noqa: E402
from app import participant_analysis as pa  # noqa: E402
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


# ---------------------------------------------------------------------------
# The participant-level computations shared with the Group Analysis window.
#
# These used to live in app.group_analysis, which meant the
# single-participant window could not use them without importing a group
# module - so it did not, and drifted. They are exercised here, on ONE
# participant, because that is the case the move exists to serve.


def trial(**kw) -> dict:
    base = {
        "participant": "P01", "trial_index": 1, "condition": "B", "level": "alpha",
        "sequence": "s1", "analyzed": True, "key_accuracy": 0.9, "fa_main": 0.8,
        "fa_given_key": 0.85, "rt_correct_key_s": 0.7, "rt_complete_s": 0.72,
        "misses": 0, "wrong_key": 0, "suspected_carryover": 0, "excluded_carryover": 0,
        "manual_corrections": 0, "unresolved_rate": 0.0, "ambiguous_rate": 0.0,
        "borderline_events": 0, "qc_extra_presses": 0, "sync_method": "led",
    }
    base.update(kw)
    return base


def pevent(**kw) -> dict:
    base = event()
    base.update({"participant": "P01", "trial_index": 1, "level": "alpha",
                 "validity": "valid", "target_finger_probability": 0.9})
    base.update(kw)
    return base


class TestValidEvents(unittest.TestCase):
    def test_only_confirmed_carryover_is_removed(self):
        events = [
            pevent(),
            pevent(validity="invalid_carryover"),
            pevent(timed_out=True, actual_note=None),      # a real outcome
            pevent(actual_finger=None, finger_correct=None),  # unresolved, a real outcome
        ]
        kept = pa.valid_events(events)
        self.assertEqual(len(kept), 3)
        self.assertNotIn("invalid_carryover", [e["validity"] for e in kept])

    def test_rows_without_a_validity_field_are_kept(self):
        """Older exports predate the column; absence is not invalidity."""
        self.assertEqual(len(pa.valid_events([event()])), 1)


class TestParticipantCellMetrics(unittest.TestCase):
    def test_fa_averages_analyzed_trials_only_and_missing_stays_nan(self):
        rows = [trial(trial_index=1, fa_main=0.8),
                trial(trial_index=2, fa_main=None, analyzed=False),
                trial(trial_index=3, fa_main=0.6)]
        df = pa.participant_condition_metrics(rows)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df.iloc[0]["fa_main"]), 0.7)
        self.assertEqual(int(df.iloc[0]["n_trials"]), 3)
        self.assertEqual(int(df.iloc[0]["n_analyzed"]), 2)

    def test_a_missing_rt_never_enters_the_mean_as_zero(self):
        rows = [trial(trial_index=1, rt_correct_key_s=0.8),
                trial(trial_index=2, rt_correct_key_s=None)]
        df = pa.participant_condition_metrics(rows)
        self.assertAlmostEqual(float(df.iloc[0]["rt_correct_key_s"]), 0.8)

    def test_cells_a_participant_never_ran_are_absent_not_zero(self):
        df = pa.participant_cell_metrics([trial(condition="B", level="alpha")])
        self.assertEqual(len(df), 1)
        self.assertEqual(list(df["condition"]), ["B"])


class TestWithinCellRepetition(unittest.TestCase):
    def test_repetition_follows_the_participants_own_trial_order(self):
        rows = [trial(trial_index=20, fa_main=0.9),
                trial(trial_index=4, fa_main=0.5),
                trial(trial_index=11, fa_main=0.7)]
        df = pa.within_cell_repetition(rows).sort_values("repetition")
        self.assertEqual(list(df["repetition"]), [1, 2, 3])
        self.assertEqual([round(v, 2) for v in df["fa_main"]], [0.5, 0.7, 0.9])

    def test_unanalyzed_trials_contribute_nan_fa_not_zero(self):
        df = pa.within_cell_repetition([trial(analyzed=False, fa_main=0.9)])
        self.assertTrue(np.isnan(df.iloc[0]["fa_main"]))


class TestSessionPositionMetrics(unittest.TestCase):
    def test_condition_a_is_excluded_from_the_bc_progression(self):
        rows = [trial(trial_index=1, condition="A"),
                trial(trial_index=2, condition="B"),
                trial(trial_index=3, condition="C")]
        df = pa.session_position_metrics(rows)
        self.assertEqual(set(df["condition"]), {"B", "C"})

    def test_adjustment_removes_the_cell_mean_and_restores_the_grand_mean(self):
        # Two cells with different means: after adjustment both are
        # centred on the participant's own B/C grand mean, so the
        # position curve no longer carries the schedule's composition.
        rows = [trial(trial_index=1, condition="B", level="alpha", rt_correct_key_s=1.0),
                trial(trial_index=2, condition="B", level="alpha", rt_correct_key_s=1.2),
                trial(trial_index=3, condition="C", level="gamma", rt_correct_key_s=0.4),
                trial(trial_index=4, condition="C", level="gamma", rt_correct_key_s=0.6)]
        df = pa.session_position_metrics(rows).set_index("position")
        grand = (1.0 + 1.2 + 0.4 + 0.6) / 4
        self.assertAlmostEqual(df.loc[1, "rt_correct_key_s_adjusted"], grand - 0.1)
        self.assertAlmostEqual(df.loc[3, "rt_correct_key_s_adjusted"], grand - 0.1)
        # ... and the raw values are kept so the transform is auditable.
        self.assertAlmostEqual(df.loc[1, "rt_correct_key_s_raw"], 1.0)


class TestPerFingerMetrics(unittest.TestCase):
    def test_left_and_right_merge_by_id_with_the_counts_kept(self):
        events = [pevent(target_finger="L4", actual_finger="L4"),
                  pevent(target_finger="R4", actual_finger="R4"),
                  pevent(target_finger="R4", actual_finger="R3", finger_correct=False)]
        row = pa.per_finger_metrics(events).set_index("finger_id").loc[4]
        self.assertEqual(int(row["n"]), 3)
        self.assertEqual(int(row["n_left"]), 1)
        self.assertEqual(int(row["n_right"]), 2)
        self.assertAlmostEqual(float(row["fa"]), 2 / 3)

    def test_rt_complete_restricts_to_key_and_finger_correct_events(self):
        events = [pevent(target_finger="R2", rt_s=0.5),
                  pevent(target_finger="R2", rt_s=1.5, finger_correct=False,
                         actual_finger="R3")]
        row = pa.per_finger_metrics(events).set_index("finger_id").loc[2]
        self.assertAlmostEqual(float(row["rt_s"]), 1.0)        # all responded
        self.assertAlmostEqual(float(row["rt_complete_s"]), 0.5)
        self.assertEqual(int(row["n_rt_complete"]), 1)

    def test_carryover_events_never_reach_a_finger_cell(self):
        events = [pevent(target_finger="R2", rt_s=0.5),
                  pevent(target_finger="R2", rt_s=9.0, validity="invalid_carryover")]
        row = pa.per_finger_metrics(events).set_index("finger_id").loc[2]
        self.assertEqual(int(row["n"]), 1)
        self.assertAlmostEqual(float(row["rt_s"]), 0.5)

    def test_a_finger_with_no_events_is_nan_not_zero(self):
        row = pa.per_finger_metrics([pevent(target_finger="R2")]).set_index("finger_id").loc[5]
        self.assertEqual(int(row["n"]), 0)
        self.assertTrue(np.isnan(row["fa"]))


class TestOutcomeProportions(unittest.TestCase):
    def test_proportions_sum_to_one_over_the_valid_events(self):
        events = [pevent(), pevent(finger_correct=False, actual_finger="R3"),
                  pevent(validity="invalid_carryover")]
        df = pa.participant_outcome_proportions(events)
        self.assertAlmostEqual(float(df["proportion"].sum()), 1.0)
        self.assertEqual(set(df["n_events"]), {2})


class TestQualitySummary(unittest.TestCase):
    def test_counts_total_and_valid_events_separately(self):
        trials = [trial(excluded_carryover=1, suspected_carryover=2)]
        events = [pevent(), pevent(validity="invalid_carryover")]
        row = pa.quality_summary(trials, events).iloc[0]
        self.assertEqual(int(row["n_events"]), 2)
        self.assertEqual(int(row["n_valid_events"]), 1)
        self.assertEqual(int(row["excluded_carryover"]), 1)
        self.assertEqual(int(row["suspected_carryover"]), 2)


class TestThresholdSensitivity(unittest.TestCase):
    def test_every_theta_is_recomputed_from_the_stored_probabilities(self):
        trials = [trial(trial_index=1, condition="B")]
        events = [pevent(trial_index=1, target_finger_probability=0.45),
                  pevent(trial_index=1, target_finger_probability=0.33)]
        df = pa.threshold_sensitivity(trials, events).set_index("theta")
        self.assertAlmostEqual(float(df.loc[0.30, "fa"]), 1.0)   # both clear 0.30
        self.assertAlmostEqual(float(df.loc[0.40, "fa"]), 0.5)   # only the 0.45 one
        self.assertAlmostEqual(float(df.loc[0.50, "fa"]), 0.0)
        self.assertEqual(set(df["source"]), {"automatic_target_probability"})

    def test_unanalyzed_and_condition_a_trials_are_out(self):
        trials = [trial(trial_index=1, condition="A"),
                  trial(trial_index=2, condition="B", analyzed=False)]
        events = [pevent(trial_index=1, condition="A"), pevent(trial_index=2)]
        self.assertTrue(pa.threshold_sensitivity(trials, events).empty)


class TestGroupAnalysisStillReExports(unittest.TestCase):
    """The move must be invisible to app.group_analysis's callers: the
    group window, three finger_* modules and session_progression all
    reach these through `ga.`, and a rename there is a silent breakage."""

    def test_the_moved_names_resolve_to_the_same_objects(self):
        for name in ("valid_events", "participant_condition_metrics",
                     "participant_cell_metrics", "within_cell_repetition",
                     "participant_repetition_metrics", "session_position_metrics",
                     "participant_outcome_proportions", "per_finger_metrics",
                     "quality_summary", "threshold_sensitivity"):
            self.assertIs(getattr(ga, name), getattr(pa, name), name)

    def test_the_shared_constants_are_the_same_values(self):
        self.assertEqual(ga.CONDITIONS, pa.CONDITIONS)
        self.assertEqual(ga.LEVELS, pa.LEVELS)
        self.assertEqual(ga.GUIDANCE_CONDITIONS, pa.GUIDANCE_CONDITIONS)
        self.assertEqual(ga.FINGER_IDS, pa.FINGER_IDS)
        self.assertEqual(ga.VALIDITY_INVALID_CARRYOVER, pa.VALIDITY_INVALID_CARRYOVER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
