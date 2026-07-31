"""Unit tests for app.finger_weakest - "does each participant's OWN
weakest finger gain most from the vibrotactile cue?".

Selecting the extreme of five noisily-estimated cells and then
re-measuring it is the classic regression-to-the-mean setup, and the bias
points at the hypothesis. Two properties are therefore asserted directly:

  * when the fingers genuinely differ, the selection is stable across
    independent halves of the trials and both estimates agree;
  * when the fingers do NOT differ at all - so "the weakest finger" is
    only that half's noise - the selection agreement collapses towards
    the 1-in-5 chance rate and the naive advantage is inflated relative
    to the split-half one.

The second is the test that stops this analysis from being written up as
a finding when it is arithmetic.

Run from main/:  python test-script/test_finger_weakest.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from app import finger_common as fc  # noqa: E402
from app import finger_weakest as fw  # noqa: E402
from app.group_analysis import MIN_TEST_N  # noqa: E402
from finger_fixtures import make_events  # noqa: E402

SMALL = dict(trials_per_condition=6, events_per_trial=20)
N_SIMS = 10


def estimates(res):
    return {e["key"]: e for e in res["estimates"]}


class TestSelection(unittest.TestCase):
    def test_slowest_finger_is_picked_when_it_really_is_slowest(self):
        """The fixture makes finger 5 the slowest by construction."""
        res = fw.weakest_finger_analysis(
            make_events(n_participants=6, noise=0.0, accuracy=1.0, **SMALL), "rt")
        self.assertEqual(set(res["picks"].values()), {5})
        self.assertEqual(res["finger_counts"][5], 6)

    def test_participants_without_all_five_fingers_are_not_selected_from(self):
        """"The worst of three" is not the same quantity as "the worst of
        five", so an incomplete participant must be skipped, not ranked."""
        cells = {("P01", f): float(f) for f in (1, 2, 3)}
        cells.update({("P02", f): float(f) for f in fc.FINGER_IDS})
        picks = fw.weakest_by_participant(cells, higher_is_worse=True)
        self.assertEqual(picks, {"P02": 5})

    def test_ties_resolve_deterministically(self):
        cells = {("P01", f): 1.0 for f in fc.FINGER_IDS}
        self.assertEqual(fw.weakest_by_participant(cells, higher_is_worse=True), {"P01": 1})
        self.assertEqual(fw.weakest_by_participant(cells, higher_is_worse=False), {"P01": 1})

    def test_accuracy_criterion_uses_the_other_direction(self):
        cells = {("P01", 1): 0.80, ("P01", 2): 0.95, ("P01", 3): 0.99,
                 ("P01", 4): 0.97, ("P01", 5): 0.90}
        self.assertEqual(fw.weakest_by_participant(cells, higher_is_worse=False)["P01"], 1)
        self.assertEqual(fw.weakest_by_participant(cells, higher_is_worse=True)["P01"], 3)

    def test_unknown_criterion_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            fw.weakest_finger_analysis(make_events(**SMALL), "not_a_criterion")


class TestStabilityDiagnostic(unittest.TestCase):
    def test_stable_when_the_fingers_really_differ(self):
        res = fw.weakest_finger_analysis(
            make_events(n_participants=7, noise=0.05, **SMALL), "rt")
        s = res["stability"]
        self.assertEqual(s["n_compared"], 7)
        self.assertGreater(s["agreement_rate"], s["chance_rate"])
        self.assertIn("Stable enough", fw.stability_verdict(res))

    def test_collapses_to_chance_when_the_fingers_are_identical(self):
        """No true between-finger difference: "this participant's weakest
        finger" is a description of the half, not of the participant."""
        rates = []
        for seed in range(N_SIMS):
            res = fw.weakest_finger_analysis(
                make_events(n_participants=7, finger_offset_scale=0.0,
                            noise=0.12, seed=seed, **SMALL), "rt")
            rates.append(res["stability"]["agreement_rate"])
        mean_rate = float(np.mean(rates))
        self.assertLess(mean_rate, 0.5,
                        f"selection looks stable ({mean_rate:.2f}) with no true finger "
                        "differences — the split-half selection may be leaking")

    def test_verdict_warns_when_agreement_is_at_chance(self):
        res = {"stability": {"n_compared": 7, "n_agree": 1, "agreement_rate": 1 / 7,
                             "chance_rate": 0.2}}
        self.assertIn("descriptive noise", fw.stability_verdict(res))

    def test_verdict_survives_an_empty_comparison(self):
        res = {"stability": {"n_compared": 0, "n_agree": 0, "agreement_rate": np.nan,
                             "chance_rate": 0.2}}
        self.assertIn("could not be assessed", fw.stability_verdict(res))


class TestRegressionToTheMean(unittest.TestCase):
    """The naive estimate selects and measures on the same trials; the
    split-half estimate does not. Under a uniform benefit the second must
    be the smaller one."""

    @classmethod
    def setUpClass(cls):
        cls.naive, cls.clean = [], []
        for seed in range(N_SIMS):
            res = fw.weakest_finger_analysis(
                make_events(n_participants=7, finger_offset_scale=0.0,
                            noise=0.12, comp_slope=0.0, seed=seed, **SMALL), "rt")
            est = estimates(res)
            cls.naive.append(est["naive"]["test"]["mean"])
            cls.clean.append(est["split_half"]["test"]["mean"])
        cls.naive = np.array(cls.naive, dtype=float)
        cls.clean = np.array(cls.clean, dtype=float)

    def test_naive_advantage_is_inflated_under_the_null(self):
        """Every finger gains the same amount and no finger is truly
        weakest, so the true advantage is 0 - yet selecting on the same
        data reports a positive one."""
        self.assertGreater(self.naive.mean(), 0.005)  # > 5 ms of pure artefact
        self.assertGreater((self.naive > 0).mean(), 0.8)

    def test_split_half_removes_most_of_it(self):
        self.assertLess(self.clean.mean(), self.naive.mean())
        self.assertLess(abs(self.clean.mean()), abs(self.naive.mean()) / 2)

    def test_gap_helper_reports_the_difference(self):
        res = fw.weakest_finger_analysis(
            make_events(n_participants=7, finger_offset_scale=0.0, seed=0, **SMALL), "rt")
        gap = fw.regression_to_mean_gap(res)
        est = estimates(res)
        self.assertAlmostEqual(gap["naive"], est["naive"]["test"]["mean"])
        self.assertAlmostEqual(gap["split_half"], est["split_half"]["test"]["mean"])
        self.assertAlmostEqual(gap["gap"], gap["naive"] - gap["split_half"])


class TestAdvantageTable(unittest.TestCase):
    def setUp(self):
        self.res = fw.weakest_finger_analysis(
            make_events(n_participants=7, noise=0.0, accuracy=1.0,
                        comp_slope=1.0, cond_shift=0.0, **SMALL), "rt")

    def test_advantage_is_weakest_minus_the_other_four(self):
        table = estimates(self.res)["naive"]["table"]
        self.assertEqual(len(table), 7)
        self.assertTrue((table["n_others"] == 4).all())
        np.testing.assert_allclose(table["advantage"],
                                   table["benefit_weakest"] - table["benefit_others_mean"])

    def test_a_real_targeted_effect_is_recovered(self):
        """comp_slope = 1 gives the slowest finger the largest benefit by
        construction, so the advantage must be clearly positive in BOTH
        estimates."""
        for key in ("naive", "split_half"):
            self.assertGreater(estimates(self.res)[key]["test"]["mean"], 0.0, key)

    def test_the_unit_of_inference_is_the_participant(self):
        for e in self.res["estimates"]:
            self.assertEqual(e["test"]["n"], 7)


class TestGating(unittest.TestCase):
    def test_below_min_test_n_no_estimate_but_a_reason(self):
        res = fw.weakest_finger_analysis(
            make_events(n_participants=MIN_TEST_N - 1, **SMALL), "rt")
        self.assertEqual(res["estimates"], [])
        self.assertIn(f"N ≥ {MIN_TEST_N}", res["reason"])
        self.assertIsNone(fw.regression_to_mean_gap(res))

    def test_empty_input_does_not_raise(self):
        res = fw.weakest_finger_analysis([], "rt")
        self.assertEqual(res["n_participants"], 0)
        self.assertIsNotNone(res["reason"])

    def test_both_criteria_run(self):
        events = make_events(n_participants=7, **SMALL)
        for criterion, *_ in fw.CRITERIA:
            res = fw.weakest_finger_analysis(events, criterion)
            self.assertIsNone(res["reason"], criterion)
            self.assertEqual(len(res["estimates"]), 2, criterion)


if __name__ == "__main__":
    unittest.main(verbosity=2)
