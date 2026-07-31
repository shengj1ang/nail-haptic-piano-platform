"""Unit tests for app.finger_common - the shared cell/split-half
preparation behind the three finger-benefit analyses.

The split-half machinery is the load-bearing part: every unbiased
estimate in this family assumes the two halves have INDEPENDENT sampling
noise and cover the session evenly. Those two properties are what is
tested here, not just that the function returns something.

Run from main/:  python test-script/test_finger_common.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from app import finger_common as fc  # noqa: E402
from app.group_analysis import per_finger_metrics  # noqa: E402
from finger_fixtures import make_events  # noqa: E402


class TestBenefitSign(unittest.TestCase):
    def test_lower_is_better_metrics_keep_baseline_minus_cued(self):
        self.assertEqual(fc.benefit_sign("rt_complete_s"), 1.0)
        self.assertEqual(fc.benefit_sign("rt_s"), 1.0)

    def test_higher_is_better_metric_flips(self):
        """A positive benefit must always mean "the cued condition is
        better", whichever direction the metric runs."""
        self.assertEqual(fc.benefit_sign("fa"), -1.0)


class TestPairedFingerCells(unittest.TestCase):
    def setUp(self):
        self.events = make_events(n_participants=6, noise=0.0)
        self.pf = per_finger_metrics(self.events)

    def test_shape_and_derived_columns(self):
        pairs, dropped = fc.paired_finger_cells(self.pf, "rt_complete_s")
        self.assertEqual(dropped, {})
        self.assertEqual(len(pairs), 6 * len(fc.FINGER_IDS))
        self.assertEqual(sorted(pairs["finger_id"].unique()), fc.FINGER_IDS)
        np.testing.assert_allclose(pairs["benefit"], pairs["baseline"] - pairs["cued"])
        np.testing.assert_allclose(pairs["average"],
                                   (pairs["baseline"] + pairs["cued"]) / 2)

    def test_fa_benefit_is_signed_so_positive_means_better(self):
        pairs, _ = fc.paired_finger_cells(self.pf, "fa")
        row = pairs.iloc[0]
        self.assertAlmostEqual(row["benefit"], row["cued"] - row["baseline"])

    def test_incomplete_participant_is_dropped_and_named(self):
        pf = self.pf[~((self.pf["participant"] == "P02")
                       & (self.pf["condition"] == "C")
                       & (self.pf["finger_id"] == 3))]
        pairs, dropped = fc.paired_finger_cells(pf, "rt_complete_s")
        self.assertIn("P02", dropped)
        self.assertNotIn("P02", set(pairs["participant"]))
        self.assertEqual(len(pairs), 5 * len(fc.FINGER_IDS))

    def test_condition_a_is_never_a_baseline(self):
        pairs, _ = fc.paired_finger_cells(self.pf, "rt_complete_s")
        self.assertEqual(len(pairs), 6 * len(fc.FINGER_IDS))  # B/C only, not 3 conditions

    def test_empty_input(self):
        pairs, dropped = fc.paired_finger_cells(per_finger_metrics([]), "rt_complete_s")
        self.assertTrue(pairs.empty)
        self.assertEqual(dropped, {})


class TestSplitHalf(unittest.TestCase):
    def setUp(self):
        self.events = make_events(n_participants=4, trials_per_condition=6)

    def test_partition_is_exact_and_lossless(self):
        a, b = fc.split_half_events(self.events)
        self.assertEqual(len(a) + len(b), len(self.events))
        ids = lambda evs: {(e["participant"], e["trial_index"], e["event_index"]) for e in evs}
        self.assertEqual(ids(a) | ids(b), ids(self.events))
        self.assertEqual(ids(a) & ids(b), set())

    def test_no_trial_is_split_across_halves(self):
        """Events inside one trial share a sequence and a moment, so
        their errors are correlated; if a trial straddled the two halves
        the "independent noise" assumption behind every split-half
        estimate in this family would be false."""
        a, b = fc.split_half_events(self.events)
        trials_a = {(e["participant"], e["condition"], e["trial_index"]) for e in a}
        trials_b = {(e["participant"], e["condition"], e["trial_index"]) for e in b}
        self.assertEqual(trials_a & trials_b, set())

    def test_halves_are_balanced_within_each_condition(self):
        a, b = fc.split_half_events(self.events)
        for condition in ("B", "C"):
            for participant in ("P00", "P03"):
                na = sum(1 for e in a if e["participant"] == participant
                         and e["condition"] == condition)
                nb = sum(1 for e in b if e["participant"] == participant
                         and e["condition"] == condition)
                self.assertLessEqual(abs(na - nb), max(na, nb) * 0.25 + 1)

    def test_split_is_deterministic(self):
        """A reported split-half number has to be reproducible from the
        exported CSVs alone - no RNG anywhere in the path."""
        first = fc.split_half_events(self.events)[0]
        for _ in range(3):
            again = fc.split_half_events(self.events)[0]
            self.assertEqual([e["event_index"] for e in first],
                             [e["event_index"] for e in again])

    def test_split_half_cells_cover_the_same_grid(self):
        pf_a, pf_b = fc.split_half_cells(self.events)
        for pf in (pf_a, pf_b):
            grid = pf[pf["condition"].isin(["B", "C"])]
            self.assertEqual(len(grid), 4 * 2 * len(fc.FINGER_IDS))
            self.assertTrue(grid["rt_complete_s"].notna().all())

    def test_empty_input_is_survivable(self):
        a, b = fc.split_half_events([])
        self.assertEqual((a, b), ([], []))


class TestPairedSummary(unittest.TestCase):
    def test_values_match_the_textbook_definitions(self):
        vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        s = fc.paired_summary(vals)
        self.assertEqual(s["n"], 5)
        self.assertAlmostEqual(s["mean"], 3.0)
        self.assertAlmostEqual(s["sd"], float(np.std(vals, ddof=1)))
        self.assertAlmostEqual(s["dz"], 3.0 / float(np.std(vals, ddof=1)))
        self.assertLess(s["ci95_lo"], 3.0)
        self.assertGreater(s["ci95_hi"], 3.0)

    def test_nan_values_are_dropped_not_counted(self):
        s = fc.paired_summary(np.array([1.0, np.nan, 3.0]))
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["mean"], 2.0)

    def test_single_value_gives_no_fabricated_interval(self):
        s = fc.paired_summary(np.array([2.0]))
        self.assertEqual(s["n"], 1)
        self.assertAlmostEqual(s["mean"], 2.0)
        for key in ("sd", "sem", "ci95_lo", "ci95_hi", "dz"):
            self.assertTrue(np.isnan(s[key]), key)

    def test_zero_spread_gives_no_infinite_effect_size(self):
        s = fc.paired_summary(np.array([2.0, 2.0, 2.0]))
        self.assertAlmostEqual(s["sd"], 0.0)
        self.assertTrue(np.isnan(s["dz"]))


class TestDescribeDropped(unittest.TestCase):
    def test_none_when_complete(self):
        self.assertIsNone(fc.describe_dropped({}))

    def test_names_every_excluded_participant(self):
        note = fc.describe_dropped({"P02": "missing C/F3", "P05": "missing B/F1"})
        self.assertIn("P02", note)
        self.assertIn("P05", note)
        self.assertIn("never imputed", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
