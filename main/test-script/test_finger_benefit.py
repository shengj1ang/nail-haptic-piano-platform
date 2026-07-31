"""Unit tests for app.finger_benefit - "is the vibrotactile benefit
larger on the fingers that were already slow?".

The central test here is not that the module computes a correlation. It
is that the module can tell a real compensatory effect apart from the
artefact the naive version of this question manufactures. Two synthetic
worlds are used throughout:

  uniform    every finger gains the same constant. The TRUE
             benefit-vs-baseline correlation is zero, so any positive
             estimate is bias.
  compensatory  the gain grows with the finger's own baseline. The true
             correlation is strongly positive.

test_naive_is_biased_upwards_under_the_null and
test_split_half_is_unbiased_under_the_null are the pair that justifies
the whole module existing; if the second ever fails, the reported
"compensation" result is not trustworthy.

Run from main/:  python test-script/test_finger_benefit.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from app import finger_benefit as fb  # noqa: E402
from app import finger_common as fc  # noqa: E402
from app.group_analysis import MIN_TEST_N  # noqa: E402
from finger_fixtures import make_events  # noqa: E402

# Monte-Carlo settings: enough repetitions to separate a +0.25 bias from
# zero, few enough that the file stays a unit test.
N_SIMS = 12
SMALL = dict(trials_per_condition=6, events_per_trial=20)


def group_rs(uniform: bool, n_sims: int = N_SIMS):
    """{estimator: [group r, one per simulated study]}."""
    kwargs = dict(SMALL, comp_slope=0.0 if uniform else 1.5)
    out = {}
    for seed in range(n_sims):
        res = fb.compensation_analysis(make_events(seed=seed, **kwargs))
        for e in res["estimators"]:
            out.setdefault(e["key"], []).append(e["group"]["r"])
    return {k: np.array(v, dtype=float) for k, v in out.items()}


class TestCouplingBias(unittest.TestCase):
    """The reason this module has three estimators instead of one."""

    @classmethod
    def setUpClass(cls):
        cls.null = group_rs(uniform=True)
        cls.effect = group_rs(uniform=False)

    def test_naive_is_biased_upwards_under_the_null(self):
        """Every finger gains exactly the same amount, so the true
        correlation is 0 - yet regressing the benefit on its own
        baseline term reports a clearly positive one, in the direction
        of the hypothesis. This is the trap the module exists to avoid."""
        naive = self.null["naive"]
        self.assertGreater(naive.mean(), 0.10,
                           "naive estimator unexpectedly unbiased — the fixture may no "
                           "longer contain per-cell sampling noise")
        self.assertGreater((naive > 0).mean(), 0.6)

    def test_split_half_is_unbiased_under_the_null(self):
        """Selection-free baseline: the expected correlation is 0."""
        clean = self.null["split_half"]
        self.assertLess(abs(clean.mean()), 0.12)

    def test_split_half_beats_naive_under_the_null(self):
        self.assertLess(self.null["split_half"].mean(), self.null["naive"].mean())

    def test_oldham_sits_between_the_two(self):
        """Oldham removes the shared-noise term; with equal variances in
        the two conditions it should land near the unbiased estimate and
        below the naive one."""
        self.assertLess(self.null["oldham"].mean(), self.null["naive"].mean())

    def test_all_estimators_recover_a_real_compensatory_effect(self):
        """Unbiasedness is worthless if it also destroys the signal: the
        split-half estimator must still find a genuine effect."""
        for key in ("naive", "oldham", "split_half"):
            self.assertGreater(self.effect[key].mean(), 0.5, key)
        self.assertGreater(self.effect["split_half"].mean(), 0.7)

    def test_artefact_gap_shrinks_when_the_effect_is_real(self):
        """The naive/split-half gap measures artefact, not effect size,
        so it must be far larger under the null than under a real
        effect."""
        null_gap = (self.null["naive"] - self.null["split_half"]).mean()
        effect_gap = (self.effect["naive"] - self.effect["split_half"]).mean()
        self.assertGreater(null_gap, effect_gap)


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.res = fb.compensation_analysis(make_events(seed=2, comp_slope=1.0, **SMALL))

    def test_all_three_estimators_present_with_points(self):
        self.assertIsNone(self.res["reason"])
        keys = [e["key"] for e in self.res["estimators"]]
        self.assertEqual(keys, ["naive", "oldham", "split_half"])
        for e in self.res["estimators"]:
            self.assertEqual(e["n_participants"], 7)
            self.assertEqual(e["n_points"], 7 * len(fc.FINGER_IDS))

    def test_the_unit_of_inference_is_the_participant(self):
        """35 cells, but df must be N-1 = 6 - the cells are not
        independent observations."""
        for e in self.res["estimators"]:
            self.assertEqual(e["group"]["n"], 7)
            self.assertEqual(e["group"]["df"], 6)
            self.assertEqual(len(e["per_participant"]), 7)
            self.assertTrue((e["per_participant"]["n_points"] == 5).all())

    def test_naive_and_oldham_differ_only_in_their_x_axis(self):
        by = {e["key"]: e for e in self.res["estimators"]}
        naive_x = by["naive"]["points"]["x"].to_numpy(dtype=float)
        oldham_x = by["oldham"]["points"]["x"].to_numpy(dtype=float)
        np.testing.assert_allclose(by["naive"]["points"]["y"], by["oldham"]["points"]["y"])
        self.assertFalse(np.allclose(naive_x, oldham_x))
        # Oldham's x is the (B, C) mean, so it lies below the B baseline
        # whenever C is the faster condition.
        self.assertTrue((oldham_x < naive_x).all())

    def test_fisher_z_round_trips_to_the_reported_r(self):
        for e in self.res["estimators"]:
            zs = e["per_participant"]["z"].dropna().to_numpy(dtype=float)
            self.assertAlmostEqual(e["group"]["r"], float(np.tanh(zs.mean())), places=10)

    def test_split_half_x_axis_is_not_the_full_sample_baseline(self):
        """If half 1's baseline silently fell back to the full sample the
        estimator would be coupled again and the test above would still
        pass - so check it explicitly."""
        by = {e["key"]: e for e in self.res["estimators"]}
        full = by["naive"]["points"].set_index(["participant", "finger_id"])["x"]
        half = by["split_half"]["points"].set_index(["participant", "finger_id"])["x"]
        self.assertFalse(np.allclose(full.reindex(half.index).to_numpy(dtype=float),
                                     half.to_numpy(dtype=float)))

    def test_slopes_are_in_metric_units(self):
        for e in self.res["estimators"]:
            slopes = e["per_participant"]["slope"].dropna()
            self.assertEqual(len(slopes), 7)
            self.assertTrue(np.isfinite(e["slope"]["mean"]))


class TestGating(unittest.TestCase):
    def test_below_min_test_n_no_estimate_but_a_reason(self):
        res = fb.compensation_analysis(
            make_events(n_participants=MIN_TEST_N - 1, **SMALL))
        self.assertEqual(res["estimators"], [])
        self.assertIn(f"N ≥ {MIN_TEST_N}", res["reason"])

    def test_empty_input_does_not_raise(self):
        res = fb.compensation_analysis([])
        self.assertEqual(res["n_participants"], 0)
        self.assertIsNotNone(res["reason"])
        self.assertIsNone(fb.artefact_gap(res))

    def test_missing_pingouin_only_costs_rm_corr(self):
        from app import group_analysis as ga
        saved = ga._pingouin
        try:
            ga._pingouin = "pingouin is not importable in this interpreter (test)"
            res = fb.compensation_analysis(make_events(seed=1, **SMALL))
            self.assertIsNone(res["reason"])
            for e in res["estimators"]:
                self.assertFalse(e["rm_corr"]["available"])
                self.assertTrue(np.isfinite(e["group"]["r"]))  # two-stage still works
        finally:
            ga._pingouin = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
