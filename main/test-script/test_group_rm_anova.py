"""Unit tests for the Condition x Finger repeated-measures ANOVA layer of
app.group_analysis (anova_cell_frame / rm_anova_finger /
ceiling_diagnostics / finger_cell_descriptives) and for the per-finger
columns it is fitted on.

The tests never assert "pingouin returns what pingouin returns". They
check the things the report depends on and that a wrong wiring would
break: which participants enter a repeated-measures fit, the degrees of
freedom implied by the design, the algebraic identities that tie F,
partial eta squared and the Greenhouse-Geisser correction together, the
independent paired-t cross-check of the two-level Condition effect, and
the guarantee that no data situation makes the tab raise.

Run from main/:  python test-script/test_group_rm_anova.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as sstats  # noqa: E402

from app import group_analysis as ga  # noqa: E402
from app.group_analysis import (  # noqa: E402
    ANOVA_CONDITIONS,
    FINGER_IDS,
    MIN_TEST_N,
    anova_cell_frame,
    ceiling_diagnostics,
    finger_cell_descriptives,
    per_finger_metrics,
    rm_anova_finger,
)

HAVE_PINGOUIN = ga.load_pingouin()[0] is not None
NEEDS_PINGOUIN = unittest.skipUnless(
    HAVE_PINGOUIN,
    "pingouin is not installed in this interpreter — `pip install -r requirements.txt`")


# ---------------------------------------------------------------------------
# Fixtures

def event(participant="P01", condition="B", target_finger="R2", **kw) -> dict:
    """One exported event row, same schema the group loader produces."""
    base = {
        "participant": participant,
        "trial_index": 1,
        "condition": condition,
        "level": "alpha",
        "event_index": 0,
        "target_finger": target_finger,
        "target_hand": target_finger[0],
        "timed_out": False,
        "target_note": 60,
        "actual_note": 60,
        "key_correct": True,
        "actual_finger": target_finger,
        "finger_correct": True,
        "rt_s": 0.5,
        "validity": "valid",
        "manually_corrected": False,
    }
    base.update(kw)
    return base


def cells(values, conditions=("B", "C"), fingers=FINGER_IDS, metric="rt_complete_s",
          extra_columns=True) -> pd.DataFrame:
    """A per_finger_metrics-shaped frame from values[participant][condition][finger].

    Only the columns the ANOVA layer reads are guaranteed; the count
    columns are filled with a plausible constant so caption/audit code
    paths can be exercised too."""
    rows = []
    for participant in sorted(values):
        for c in conditions:
            for f in fingers:
                v = values[participant].get(c, {}).get(f, np.nan)
                row = {"participant": participant, "condition": c, "finger_id": f, metric: v}
                if extra_columns:
                    row.update(n=60, n_left=30, n_right=30, n_judged=60, n_rt_complete=50)
                rows.append(row)
    return pd.DataFrame(rows)


def linear_cells(n_participants=7, cond_shift=-0.04, finger_slope=0.015,
                 interaction=0.0, noise=0.0, seed=3, metric="rt_complete_s"):
    """A clean additive design plus optional interaction and noise:

        y = 0.55 + participant offset + cond_shift*[C] + finger_slope*finger
            + interaction*[C]*finger + noise
    """
    rng = np.random.default_rng(seed)
    values = {}
    for i in range(n_participants):
        offset = rng.normal(0, 0.05)
        p = f"P{i:02d}"
        values[p] = {}
        for c in ("B", "C"):
            values[p][c] = {}
            for f in FINGER_IDS:
                y = 0.55 + offset + finger_slope * f
                if c == "C":
                    y += cond_shift + interaction * f
                if noise:
                    y += rng.normal(0, noise)
                values[p][c][f] = y
    return cells(values, metric=metric)


# ---------------------------------------------------------------------------


class TestPerFingerRtColumns(unittest.TestCase):
    """rt_complete_s must be the correct-complete-action RT, i.e. a
    strict subset of the events rt_s averages, and must never silently
    fall back to it."""

    def test_complete_rt_uses_only_key_and_finger_correct_events(self):
        events = [
            event(rt_s=0.40),                                             # complete
            event(event_index=1, rt_s=0.60, finger_correct=False,
                  actual_finger="R3"),                                    # right key, wrong finger
            event(event_index=2, rt_s=0.80, key_correct=False,
                  actual_note=61, finger_correct=True),                   # wrong key
            event(event_index=3, rt_s=1.00, finger_correct=None,
                  actual_finger=None),                                    # unresolved finger
        ]
        pf = per_finger_metrics(events)
        row = pf[pf["finger_id"] == 2].iloc[0]
        self.assertEqual(row["n"], 4)
        self.assertEqual(row["n_judged"], 3)          # unresolved has no verdict
        self.assertEqual(row["n_rt_complete"], 1)
        self.assertAlmostEqual(row["rt_complete_s"], 0.40)
        self.assertAlmostEqual(row["rt_s"], (0.40 + 0.60 + 0.80 + 1.00) / 4)
        self.assertAlmostEqual(row["fa"], 1 / 3)

    def test_no_complete_events_gives_nan_not_zero(self):
        events = [event(rt_s=0.5, finger_correct=False, actual_finger="R3")]
        row = per_finger_metrics(events).set_index("finger_id").loc[2]
        self.assertEqual(row["n_rt_complete"], 0)
        self.assertTrue(np.isnan(row["rt_complete_s"]))

    def test_timed_out_and_carryover_events_stay_out(self):
        events = [
            event(rt_s=0.5),
            event(event_index=1, rt_s=None, timed_out=True, actual_note=None,
                  key_correct=False, actual_finger=None, finger_correct=None),
            event(event_index=2, rt_s=0.1, validity="invalid_carryover"),
        ]
        row = per_finger_metrics(events).set_index("finger_id").loc[2]
        self.assertEqual(row["n"], 1)
        self.assertEqual(row["n_rt_complete"], 1)
        self.assertAlmostEqual(row["rt_complete_s"], 0.5)

    def test_empty_input_keeps_the_new_columns(self):
        pf = per_finger_metrics([])
        self.assertTrue(pf.empty)
        for col in ("rt_complete_s", "n_rt_complete"):
            self.assertIn(col, pf.columns)


class TestDesignFrame(unittest.TestCase):
    """Listwise completeness: a repeated-measures fit is only defined on
    participants who supply the whole grid, and who was dropped has to be
    reportable by name."""

    def test_balanced_grid_kept_and_ordered(self):
        pf = linear_cells(n_participants=6)
        frame, dropped = anova_cell_frame(pf, "rt_complete_s")
        self.assertEqual(dropped, {})
        self.assertEqual(len(frame), 6 * 2 * 5)
        self.assertEqual(frame["participant"].nunique(), 6)
        first = frame.head(10)
        self.assertEqual(list(first["condition"]), ["B"] * 5 + ["C"] * 5)
        self.assertEqual(list(first["finger_id"]), FINGER_IDS * 2)

    def test_condition_a_never_enters_the_model(self):
        pf = linear_cells(n_participants=6)
        pf_with_a = pd.concat([pf, pf.assign(condition="A")], ignore_index=True)
        frame, _ = anova_cell_frame(pf_with_a, "rt_complete_s")
        self.assertEqual(set(frame["condition"]), set(ANOVA_CONDITIONS))
        self.assertNotIn("A", set(frame["condition"]))

    def test_missing_row_drops_the_whole_participant_and_names_the_cell(self):
        pf = linear_cells(n_participants=6)
        pf = pf[~((pf["participant"] == "P03") & (pf["condition"] == "C")
                  & (pf["finger_id"] == 4))]
        frame, dropped = anova_cell_frame(pf, "rt_complete_s")
        self.assertEqual(list(dropped), ["P03"])
        self.assertIn("C/F4", dropped["P03"])
        self.assertNotIn("P03", set(frame["participant"]))
        self.assertEqual(len(frame), 5 * 2 * 5)  # still balanced

    def test_nan_cell_counts_as_missing_and_is_never_imputed(self):
        pf = linear_cells(n_participants=6)
        mask = ((pf["participant"] == "P05") & (pf["condition"] == "B")
                & (pf["finger_id"] == 2))
        pf.loc[mask, "rt_complete_s"] = np.nan
        frame, dropped = anova_cell_frame(pf, "rt_complete_s")
        self.assertIn("P05", dropped)
        self.assertFalse(frame["rt_complete_s"].isna().any())
        self.assertEqual(frame["participant"].nunique(), 5)

    def test_empty_and_unknown_metric_are_survivable(self):
        frame, dropped = anova_cell_frame(per_finger_metrics([]), "rt_complete_s")
        self.assertTrue(frame.empty)
        self.assertEqual(dropped, {})
        frame2, dropped2 = anova_cell_frame(linear_cells(), "not_a_column")
        self.assertTrue(frame2.empty)
        self.assertEqual(dropped2, {})


class TestGating(unittest.TestCase):
    """No fit below the inferential-N floor, and a missing optional
    dependency degrades this tab only."""

    def test_below_min_test_n_no_fit_but_a_reason(self):
        res = rm_anova_finger(linear_cells(n_participants=MIN_TEST_N - 1))
        self.assertEqual(res["effects"], [])
        self.assertIsNotNone(res["reason"])
        self.assertIn(f"N ≥ {MIN_TEST_N}", res["reason"])
        self.assertEqual(res["n_participants"], MIN_TEST_N - 1)

    def test_dropped_participants_are_named_in_the_gate_reason(self):
        pf = linear_cells(n_participants=MIN_TEST_N)
        pf = pf[~((pf["participant"] == "P00") & (pf["finger_id"] == 1))]
        res = rm_anova_finger(pf)
        self.assertIsNotNone(res["reason"])
        self.assertIn("P00", res["reason"])

    def test_missing_pingouin_reports_instead_of_raising(self):
        saved = ga._pingouin
        try:
            ga._pingouin = "pingouin is not installed (ImportError: test)"
            res = rm_anova_finger(linear_cells(n_participants=7))
            self.assertEqual(res["effects"], [])
            self.assertIn("pingouin", res["reason"])
            self.assertEqual(res["n_participants"], 7)   # design still described
            self.assertEqual(res["n_cells"], 70)
        finally:
            ga._pingouin = saved

    def test_empty_input_does_not_raise(self):
        res = rm_anova_finger(per_finger_metrics([]))
        self.assertEqual(res["n_participants"], 0)
        self.assertIsNotNone(res["reason"])


@NEEDS_PINGOUIN
class TestAnovaModel(unittest.TestCase):

    def setUp(self):
        self.pf = linear_cells(n_participants=7, noise=0.02, interaction=0.004, seed=11)
        self.res = rm_anova_finger(self.pf, "rt_complete_s")

    def test_design_and_degrees_of_freedom(self):
        """a=2, b=5, n=7 -> (1, 6) for Condition and (4, 24) for Finger
        and the interaction. Wrong error terms would show up here first."""
        self.assertIsNone(self.res["reason"])
        self.assertEqual(self.res["n_participants"], 7)
        self.assertEqual(self.res["n_cells"], 70)
        self.assertEqual(self.res["cells_per_participant"], 10)
        by = {e["source"]: e for e in self.res["effects"]}
        self.assertEqual(set(by), {"condition", "finger_id", "condition * finger_id"})
        self.assertEqual((by["condition"]["df1"], by["condition"]["df2"]), (1, 6))
        self.assertEqual((by["finger_id"]["df1"], by["finger_id"]["df2"]), (4, 24))
        self.assertEqual((by["condition * finger_id"]["df1"],
                          by["condition * finger_id"]["df2"]), (4, 24))

    def test_partial_eta_squared_matches_f_and_df(self):
        """eta_p^2 = SS_eff/(SS_eff+SS_err) = F*df1 / (F*df1 + df2)."""
        for e in self.res["effects"]:
            expected = e["F"] * e["df1"] / (e["F"] * e["df1"] + e["df2"])
            self.assertAlmostEqual(e["np2"], expected, places=8, msg=e["label"])
            self.assertGreaterEqual(e["np2"], 0.0)
            self.assertLessEqual(e["np2"], 1.0)

    def test_condition_effect_equals_paired_t_squared(self):
        """A two-level within factor is a paired t-test on the
        participant means over the other factor: F = t^2, same p. This is
        an independent check of the Condition row."""
        frame = self.res["frame"]
        marginal = (frame.groupby(["participant", "condition"])["rt_complete_s"]
                    .mean().unstack("condition"))
        t, p = sstats.ttest_rel(marginal["C"], marginal["B"])
        cond = next(e for e in self.res["effects"] if e["source"] == "condition")
        self.assertAlmostEqual(cond["F"], float(t) ** 2, places=6)
        self.assertAlmostEqual(cond["p_unc"], float(p), places=10)

    def test_condition_is_never_greenhouse_geisser_corrected(self):
        """Two levels = one contrast: sphericity cannot be violated, so
        epsilon is fixed at 1 and the uncorrected p is the reported one."""
        cond = next(e for e in self.res["effects"] if e["source"] == "condition")
        self.assertFalse(cond["mauchly"]["applicable"])
        self.assertFalse(cond["gg_applicable"])
        self.assertFalse(cond["sphericity_violated"])
        self.assertEqual(cond["correction"], "none")
        self.assertEqual(cond["p_reported"], cond["p_unc"])
        self.assertAlmostEqual(cond["eps"], 1.0, places=6)
        self.assertTrue(np.isnan(cond["p_gg"]))

    def test_finger_and_interaction_carry_mauchly_and_epsilon(self):
        for source in ("finger_id", "condition * finger_id"):
            e = next(x for x in self.res["effects"] if x["source"] == source)
            self.assertTrue(e["mauchly"]["applicable"], source)
            self.assertFalse(np.isnan(e["mauchly"]["W"]), source)
            self.assertFalse(np.isnan(e["mauchly"]["p"]), source)
            self.assertTrue(e["gg_applicable"], source)
            # Greenhouse-Geisser epsilon is bounded by 1/(k-1) .. 1 with
            # k = 5 levels of the finger contrast set.
            self.assertGreaterEqual(e["eps"], 1 / (len(FINGER_IDS) - 1) - 1e-9, source)
            self.assertLessEqual(e["eps"], 1.0 + 1e-9, source)
            # Rescaled df, and a correction that can only be conservative.
            self.assertAlmostEqual(e["df1_gg"], e["df1"] * e["eps"], places=9)
            self.assertAlmostEqual(e["df2_gg"], e["df2"] * e["eps"], places=9)
            self.assertGreaterEqual(e["p_gg"], e["p_unc"] - 1e-12, source)

    def test_reported_p_uses_gg_for_every_multi_contrast_effect(self):
        """At pilot N, Mauchly is diagnostic only: every effect with
        more than one contrast must headline GG-rescaled df and p."""
        for e in self.res["effects"]:
            if e["gg_applicable"]:
                self.assertEqual(e["correction"], "Greenhouse–Geisser", e["label"])
                self.assertEqual(e["p_reported"], e["p_gg"], e["label"])
            else:
                self.assertEqual(e["correction"], "none", e["label"])
                self.assertEqual(e["p_reported"], e["p_unc"], e["label"])

    def test_additive_data_has_no_interaction_and_a_real_main_effect(self):
        """Data built with a constant B->C shift and a finger gradient but
        no interaction must show both main effects and a null
        interaction; this is the sanity check that the factors are not
        swapped or mislabelled. Small noise keeps the interaction error
        term away from an exactly-zero 0/0."""
        res = rm_anova_finger(linear_cells(n_participants=7, cond_shift=-0.05,
                                           finger_slope=0.02, interaction=0.0,
                                           noise=0.004, seed=5))
        by = {e["source"]: e for e in res["effects"]}
        self.assertLess(by["condition"]["p_unc"], 0.001)
        self.assertGreater(by["condition"]["np2"], 0.9)
        self.assertLess(by["finger_id"]["p_unc"], 0.001)
        self.assertGreater(by["condition * finger_id"]["p_unc"], 0.05)

    def test_interaction_is_detected_when_it_is_built_in(self):
        """Same design, but the B->C shift now grows with the finger ID:
        the interaction term has to pick that up."""
        res = rm_anova_finger(linear_cells(n_participants=7, cond_shift=0.0,
                                           finger_slope=0.02, interaction=0.03,
                                           noise=0.005, seed=9))
        inter = next(e for e in res["effects"] if e["source"] == "condition * finger_id")
        self.assertLess(inter["p_unc"], 0.001)
        self.assertGreater(inter["np2"], 0.9)

    def test_sensitivity_metric_runs_on_the_same_design(self):
        pf = linear_cells(n_participants=7, noise=0.02, seed=4, metric="rt_s")
        res = rm_anova_finger(pf, "rt_s")
        self.assertIsNone(res["reason"])
        self.assertEqual(res["metric"], "rt_s")
        self.assertEqual(len(res["effects"]), 3)

    def test_zero_variance_data_is_reported_not_raised(self):
        """Every cell of every participant identical: the F ratios are
        0/0. Whatever pingouin makes of that, the tab must come back with
        a renderable result object rather than an exception."""
        flat = cells({f"P{i:02d}": {c: {f: 0.5 for f in FINGER_IDS} for c in ("B", "C")}
                      for i in range(7)})
        with np.errstate(all="ignore"):
            res = rm_anova_finger(flat, "rt_complete_s")
        if res["reason"] is not None:
            self.assertIsInstance(res["reason"], str)
            return
        for e in res["effects"]:
            self.assertFalse(np.isfinite(e["F"]) and e["F"] > 1e6, e["label"])
            self.assertIsInstance(e["p_reported"], float, e["label"])


class TestFingerAccuracyDescriptives(unittest.TestCase):
    """FA is deliberately kept out of the ANOVA; the descriptive table
    and the ceiling diagnostics that justify that must be exact."""

    def _fa_pf(self):
        values = {}
        for i in range(6):
            p = f"P{i:02d}"
            values[p] = {"B": {f: 0.90 for f in FINGER_IDS},
                         "C": {f: 1.00 for f in FINGER_IDS}}
        # One participant off the ceiling in a single B cell.
        values["P00"]["B"][1] = 0.80
        return cells(values, metric="fa")

    def test_descriptives_are_over_participants_on_the_anova_grid(self):
        desc = finger_cell_descriptives(self._fa_pf(), "fa").set_index(
            ["condition", "finger_id"])
        b1 = desc.loc[("B", 1)]
        self.assertEqual(int(b1["n"]), 6)                    # participants, not cells
        self.assertAlmostEqual(b1["mean"], (0.80 + 5 * 0.90) / 6)
        self.assertAlmostEqual(b1["sd"], float(np.std([0.80] + [0.90] * 5, ddof=1)))
        self.assertLess(b1["ci95_lo"], b1["mean"])
        self.assertGreater(b1["ci95_hi"], b1["mean"])
        # A cell where every participant is identical has zero spread but
        # still a defined interval.
        c1 = desc.loc[("C", 1)]
        self.assertAlmostEqual(c1["mean"], 1.0)
        self.assertAlmostEqual(c1["sd"], 0.0)

    def test_descriptives_use_the_same_complete_cases_as_the_model(self):
        pf = self._fa_pf()
        pf = pf[~((pf["participant"] == "P02") & (pf["condition"] == "C")
                  & (pf["finger_id"] == 3))]
        desc = finger_cell_descriptives(pf, "fa")
        self.assertTrue((desc["n"] == 5).all())  # P02 dropped from every cell

    def test_ceiling_diagnostics_count_exact_ones(self):
        diag = ceiling_diagnostics(self._fa_pf(), "fa")
        self.assertEqual(diag["n_participants"], 6)
        self.assertEqual(diag["n_cells"], 60)
        self.assertEqual(diag["n_at_ceiling"], 30)          # all of condition C
        self.assertAlmostEqual(diag["prop_at_ceiling"], 0.5)
        self.assertAlmostEqual(diag["min"], 0.80)
        self.assertAlmostEqual(diag["max"], 1.00)

    def test_ceiling_diagnostics_on_empty_input(self):
        diag = ceiling_diagnostics(per_finger_metrics([]), "fa")
        self.assertEqual(diag["n_cells"], 0)
        self.assertEqual(diag["n_at_ceiling"], 0)
        self.assertTrue(np.isnan(diag["prop_at_ceiling"]))
        self.assertTrue(finger_cell_descriptives(per_finger_metrics([]), "fa").empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
