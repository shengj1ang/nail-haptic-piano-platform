"""Unit tests for app.finger_equalisation - "does the vibrotactile cue
even out the five fingers, or does it only make everything faster?".

The tests that matter are the algebraic ones. A purely multiplicative
speed-up (every RT times a constant k, no equalisation whatsoever)
shrinks SD and range by exactly k while leaving the coefficient of
variation untouched. On noiseless synthetic data those are exact
identities, so they can be asserted rather than merely hoped for - which
is what makes the CV row, and not the SD row, the one that can support
an equalisation claim.

Run from main/:  python test-script/test_finger_equalisation.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from app import finger_equalisation as fe  # noqa: E402
from app.group_analysis import MIN_TEST_N  # noqa: E402
from finger_fixtures import FINGER_OFFSET, make_events  # noqa: E402

SMALL = dict(trials_per_condition=6, events_per_trial=20)


def measures(res):
    return {m["key"]: m for m in res["measures"]}


class TestDispersionMath(unittest.TestCase):
    """Noiseless data: the dispersion columns must equal the textbook
    quantities computed straight from the five cell means."""

    def setUp(self):
        self.events = make_events(n_participants=6, noise=0.0, accuracy=1.0, **SMALL)
        self.table = fe.dispersion_table(self.events)

    def test_one_row_per_participant_and_condition(self):
        self.assertEqual(len(self.table), 6 * 2)
        self.assertEqual(set(self.table["condition"]), {"B", "C"})
        self.assertTrue((self.table["n_fingers"] == 5).all())

    def test_sd_range_and_cv_match_direct_computation(self):
        row = self.table[(self.table["participant"] == "P00")
                         & (self.table["condition"] == "B")].iloc[0]
        # Noiseless: the cell means are base + p_offset + FINGER_OFFSET.
        offsets = np.array(sorted(FINGER_OFFSET.values()), dtype=float)
        self.assertAlmostEqual(row["sd_raw"], float(np.std(offsets, ddof=1)), places=9)
        self.assertAlmostEqual(row["range"], float(offsets.max() - offsets.min()), places=9)
        self.assertAlmostEqual(row["cv_raw"], row["sd_raw"] / row["mean"], places=12)

    def test_noiseless_cells_need_no_correction(self):
        """With zero within-cell variance the per-cell SEM is 0, so the
        corrected SD must equal the raw one - the correction may never
        eat real signal."""
        np.testing.assert_allclose(self.table["sd_corrected"], self.table["sd_raw"],
                                   atol=1e-9)
        np.testing.assert_allclose(self.table["mean_cell_sem"], 0.0, atol=1e-12)


class TestProportionalNullIsNotEqualisation(unittest.TestCase):
    """The single most important property of this module: a uniform
    proportional speed-up must NOT be reported as equalisation."""

    def setUp(self):
        self.k = 0.7
        self.res = fe.equalisation_analysis(
            make_events(n_participants=6, noise=0.0, accuracy=1.0,
                        proportional=self.k, **SMALL))

    def test_sd_and_range_shrink_by_exactly_the_speed_up_factor(self):
        m = measures(self.res)
        self.assertAlmostEqual(m["sd_raw"]["ratio"], self.k, places=9)
        self.assertAlmostEqual(m["range"]["ratio"], self.k, places=9)

    def test_the_scale_free_measure_does_not_move_at_all(self):
        m = measures(self.res)
        self.assertAlmostEqual(m["cv_raw"]["ratio"], 1.0, places=9)
        self.assertAlmostEqual(m["cv_raw"]["test"]["mean"], 0.0, places=12)

    def test_the_reported_null_reference_equals_that_factor(self):
        self.assertAlmostEqual(self.res["proportional_null_ratio"], self.k, places=9)

    def test_verdict_refuses_to_call_this_equalisation(self):
        verdict = fe.verdict(self.res)
        self.assertIn("proportional speed-up", verdict)
        self.assertNotIn("not explained by the overall speed-up", verdict)


class TestRealEqualisation(unittest.TestCase):
    """Compress the between-finger differences without touching the
    overall speed: both the corrected SD and the CV must fall."""

    def setUp(self):
        # comp_slope = 1 removes the finger offsets entirely under C.
        self.res = fe.equalisation_analysis(
            make_events(n_participants=6, noise=0.0, accuracy=1.0,
                        cond_shift=0.0, comp_slope=1.0, **SMALL))

    def test_dispersion_collapses(self):
        m = measures(self.res)
        self.assertLess(m["sd_raw"]["cued_mean"], 1e-9)
        self.assertLess(m["cv_raw"]["cued_mean"], 1e-9)
        self.assertEqual(m["sd_raw"]["n_shrunk"], m["sd_raw"]["n_pairs"])

    def test_verdict_accepts_it(self):
        self.assertIn("not explained by the overall speed-up", fe.verdict(self.res))


class TestNoiseCorrection(unittest.TestCase):
    def setUp(self):
        self.events = make_events(n_participants=6, noise=0.12, **SMALL)
        self.table = fe.dispersion_table(self.events)

    def test_correction_removes_sampling_noise_from_the_spread(self):
        """Var_observed = Var_true + E[SEM^2], so the corrected SD is
        strictly below the raw one whenever the cells carry noise."""
        self.assertTrue((self.table["mean_cell_sem"] > 0).all())
        self.assertTrue((self.table["sd_corrected"] <= self.table["sd_raw"] + 1e-12).all())

    def test_correction_matches_its_own_formula(self):
        row = self.table.iloc[0]
        expected = np.sqrt(max(row["sd_raw"] ** 2 - row["mean_cell_sem"] ** 2, 0.0))
        # mean_cell_sem is the mean SEM, the formula uses the mean SQUARED
        # SEM, so allow the small Jensen gap but require the right scale.
        self.assertLess(abs(row["sd_corrected"] - expected), 0.02)

    def test_correction_never_returns_a_negative_variance(self):
        """When the noise exceeds the observed spread the true spread is
        0, not an imaginary number."""
        noisy = fe.dispersion_table(
            make_events(n_participants=6, noise=0.5, finger_offset_scale=0.0, **SMALL))
        self.assertTrue((noisy["sd_corrected"] >= 0).all())
        self.assertTrue(noisy["sd_corrected"].notna().all())

    def test_cell_standard_errors_are_per_cell(self):
        sems = fe.cell_standard_errors(self.events)
        self.assertIn(("P00", "B", 1), sems)
        self.assertTrue(all(np.isfinite(v) and v > 0 for v in sems.values()))


class TestInferenceAndGating(unittest.TestCase):
    def test_both_tests_are_reported(self):
        res = fe.equalisation_analysis(make_events(**SMALL))
        for m in res["measures"]:
            self.assertIn("p_t", m["test"])
            self.assertIn("p_wilcoxon", m["test"])
            self.assertEqual(m["test"]["n"], 7)

    def test_below_min_test_n_no_result_but_a_reason(self):
        res = fe.equalisation_analysis(
            make_events(n_participants=MIN_TEST_N - 1, **SMALL))
        self.assertEqual(res["measures"], [])
        self.assertIn(f"N ≥ {MIN_TEST_N}", res["reason"])
        self.assertIsNone(fe.verdict(res))

    def test_empty_input_does_not_raise(self):
        res = fe.equalisation_analysis([])
        self.assertEqual(res["n_participants"], 0)
        self.assertIsNotNone(res["reason"])
        self.assertTrue(fe.dispersion_table([]).empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
