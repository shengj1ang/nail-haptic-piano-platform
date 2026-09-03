"""Unit tests for the binomial GLMM layer of app.group_analysis
(accuracy_cell_counts / glmm_accuracy and the fitting machinery under it).

This model is the accuracy result the report leans on where the ANOVA
cannot go, and it is fitted by code in this repository rather than by a
library, so the tests have to earn that: they check the estimator against
things whose answer is known independently, not against its own output.

  * the counts it is fitted on reproduce, cell by cell, the `fa` the
    per-finger table and the ceiling diagnostics already publish;
  * fitting the aggregated binomial cells gives the same answer as
    fitting every Bernoulli event, which is the identity that lets the
    model run on 200 rows instead of ten thousand;
  * the starting-value IRLS really is at the logistic maximum
    (score = X'(y - n p) = 0, checked algebraically);
  * the quadrature is converged: 1 node (Laplace) and 31 nodes agree;
  * on simulated data with a KNOWN truth the fixed effect comes back
    unbiased, the likelihood-ratio test holds its nominal size, and the
    cluster-robust standard error - the one the tab reports - is larger
    than the model-based one exactly when the model is misspecified in
    the way a random intercept alone can be misspecified;
  * a saturated fit reproduces the observed cell proportions;
  * and no data situation makes the tab raise: gated N, empty input, a
    completely separated cell.

Run from main/:  python test-script/test_group_glmm.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as sstats  # noqa: E402

from app import group_analysis as ga  # noqa: E402
from finger_fixtures import make_events  # noqa: E402

CONDITIONS = list(ga.ANOVA_CONDITIONS)


# ---------------------------------------------------------------------------
# Fixtures

def simulate_counts(n_participants=20, trials=27, b0=3.5, b_cond=0.75,
                    b_finger=-0.20, sigma=0.45, slope_sd=0.0, seed=0):
    """Binomial cells from a KNOWN random-intercept (optionally
    random-slope) model - the only data in this file whose right answer is
    available for comparison.

    slope_sd > 0 adds a by-participant condition slope, i.e. the exact
    misspecification the cluster-robust standard error exists to survive.
    """
    rng = np.random.default_rng(seed)
    u = rng.normal(0, sigma, n_participants)
    v = rng.normal(0, slope_sd, n_participants) if slope_sd else np.zeros(n_participants)
    rows = []
    for i in range(n_participants):
        for c in CONDITIONS:
            for f in ga.FINGER_IDS:
                eta = b0 + b_finger * (f - 1) + u[i]
                if c == CONDITIONS[1]:
                    eta += b_cond + v[i]
                k = int(rng.binomial(trials, 1.0 / (1.0 + np.exp(-eta))))
                rows.append({"participant": f"P{i:02d}", "condition": c, "finger_id": f,
                             "successes": k, "trials": trials, "fa": k / trials})
    return pd.DataFrame(rows)


def fit_counts(counts, cols=None, **kw):
    """Fit one counts frame, optionally on a column subset of the full
    Condition x Finger design."""
    y, n, X, names, term_cols, gi, participants = ga._glmm_design(counts, CONDITIONS)
    if cols is not None:
        X = X[:, cols]
        names = [names[j] for j in cols]
    fit = ga._fit_binomial_glmm(y, n, X, gi, len(participants), **kw)
    return fit, names, term_cols


def additive_cols(term_cols):
    return [0] + term_cols["condition"] + term_cols["finger_id"]


def expand_to_events(counts):
    """The aggregated cells written back out as one row per event, in the
    exported event schema."""
    events = []
    for _, r in counts.iterrows():
        for i in range(int(r["trials"])):
            correct = i < int(r["successes"])
            events.append({
                "participant": r["participant"], "trial_index": 1,
                "condition": r["condition"], "level": "alpha", "event_index": i,
                "target_finger": f"R{int(r['finger_id'])}", "target_hand": "R",
                "timed_out": False, "target_note": 60, "actual_note": 60,
                "key_correct": True, "actual_finger": f"R{int(r['finger_id'])}",
                "finger_correct": bool(correct),
                "target_finger_probability": 0.9 if correct else 0.1,
                "rt_s": 0.9, "validity": "valid", "manually_corrected": False,
            })
    return events


# ---------------------------------------------------------------------------
# The events the model is fitted on

class TestAccuracyCellCounts(unittest.TestCase):

    def setUp(self):
        self.events = make_events(n_participants=6, trials_per_condition=4,
                                  events_per_trial=20, accuracy=0.9, seed=3)

    def test_counts_reproduce_the_published_per_finger_accuracy(self):
        """successes / trials IS the `fa` of the per-finger cells.

        The whole argument for this tab is that it models the same cells
        the RM-ANOVA tab describes; if the denominators drifted apart, the
        model and the descriptive table would be about different data
        while claiming to be about one.
        """
        counts = ga.accuracy_cell_counts(self.events, CONDITIONS)
        pf = ga.per_finger_metrics(self.events).set_index(
            ["participant", "condition", "finger_id"])
        self.assertTrue(len(counts))
        for _, row in counts.iterrows():
            key = (row["participant"], row["condition"], row["finger_id"])
            self.assertAlmostEqual(row["successes"] / row["trials"],
                                   float(pf.loc[key, "fa"]), places=12,
                                   msg=f"cell {key} disagrees with per_finger_metrics")
            self.assertEqual(int(row["trials"]), int(pf.loc[key, "n_judged"]))

    def test_only_the_modelled_conditions_are_counted(self):
        counts = ga.accuracy_cell_counts(self.events, CONDITIONS)
        self.assertEqual(sorted(counts["condition"].unique()), sorted(CONDITIONS))

    def test_timeouts_and_unjudged_events_are_excluded_not_scored_zero(self):
        """A timeout has no finger verdict, so it cannot be a binomial
        failure without inventing one. It leaves the denominator instead -
        the same rule per_finger_metrics uses."""
        events = list(self.events)
        base = ga.accuracy_cell_counts(events, CONDITIONS)
        n_before = int(base["trials"].sum())
        extra = dict(events[0])
        extra.update(timed_out=True, finger_correct=None, actual_finger=None, rt_s=None)
        after = ga.accuracy_cell_counts(events + [extra], CONDITIONS)
        self.assertEqual(int(after["trials"].sum()), n_before)

    def test_empty_input_gives_an_empty_frame_not_an_error(self):
        self.assertTrue(ga.accuracy_cell_counts([], CONDITIONS).empty)


# ---------------------------------------------------------------------------
# The estimator, against answers obtained some other way

class TestEstimator(unittest.TestCase):

    def test_irls_start_is_at_the_logistic_maximum(self):
        """Checked by the score equation X'(y - n p) = 0 rather than
        against another implementation, so the test needs no second
        library to be true."""
        counts = simulate_counts(n_participants=8, seed=11)
        y, n, X, *_ = ga._glmm_design(counts, CONDITIONS)
        beta = ga._irls_logistic(y, n, X)
        score = X.T @ (y - n * ga.expit(X @ beta))
        self.assertLess(float(np.max(np.abs(score))), 1e-8)

    def test_aggregated_cells_and_raw_events_give_the_same_fit(self):
        """The identity the module claims when it aggregates: every event
        in a cell shares one covariate row, so a Bernoulli GLMM over
        events and a binomial GLMM over the cells maximise the same
        function."""
        counts = simulate_counts(n_participants=7, trials=15, seed=5)
        from_counts, _, _ = fit_counts(counts)
        events = expand_to_events(counts)
        rebuilt = ga.accuracy_cell_counts(events, CONDITIONS)
        from_events, _, _ = fit_counts(rebuilt)
        self.assertLess(float(np.max(np.abs(from_counts["beta"] - from_events["beta"]))), 1e-6)
        self.assertAlmostEqual(from_counts["sigma"], from_events["sigma"], places=6)

    def test_quadrature_is_converged_at_the_default_node_count(self):
        """One node is the Laplace approximation; if 1, 15 and 31 nodes
        disagree, the default is not integrating the random effect
        accurately enough to report."""
        counts = simulate_counts(n_participants=12, seed=2)
        fits = {k: fit_counts(counts, nodes=k, robust=False)[0] for k in (1, 15, 31)}
        for k in (1, 15):
            self.assertLess(float(np.max(np.abs(fits[k]["beta"] - fits[31]["beta"]))), 5e-3,
                            f"{k}-node fit differs from the 31-node fit")

    def test_fixed_effect_is_recovered_from_a_known_truth(self):
        """Bias of the condition coefficient over repeated simulated
        datasets, against the value the data was generated with."""
        truth = 0.75
        estimates = []
        for seed in range(30):
            counts = simulate_counts(b_cond=truth, seed=100 + seed)
            _, _, term_cols = fit_counts(counts)
            fit, names, _ = fit_counts(counts, cols=additive_cols(term_cols), robust=False)
            estimates.append(fit["beta"][names.index(f"condition[{CONDITIONS[1]}]")])
        estimates = np.array(estimates)
        se = estimates.std(ddof=1) / np.sqrt(len(estimates))
        self.assertLess(abs(estimates.mean() - truth), max(3 * se, 0.05),
                        f"mean estimate {estimates.mean():.4f} vs truth {truth}")

    def test_variance_component_is_recovered_from_a_known_truth(self):
        """Maximum likelihood is mildly downward-biased for a variance
        component, which the tab says out loud; this pins how mild."""
        sigmas = [fit_counts(simulate_counts(sigma=0.45, seed=200 + s), robust=False)[0]["sigma"]
                  for s in range(20)]
        self.assertAlmostEqual(float(np.mean(sigmas)), 0.45, delta=0.10)

    def test_saturated_model_reproduces_the_observed_cell_means(self):
        """The Condition x Finger fixed effects saturate the 2 x 5 cell
        table, so a correct fit has to land on the observed proportions.
        A wrong link, a wrong design column or a broken quadrature all
        show up here."""
        events = make_events(n_participants=9, trials_per_condition=5,
                             events_per_trial=20, accuracy=0.92, seed=8)
        res = ga.glmm_accuracy(events, CONDITIONS)
        self.assertIsNone(res["reason"])
        gap = (res["fitted"]["p_fitted"] - res["fitted"]["observed"]).abs().max()
        self.assertLess(float(gap), 0.01, "saturated fit does not reproduce the cell means")

    def test_robust_standard_error_grows_when_the_random_structure_is_wrong(self):
        """The stated reason the tab headlines the sandwich: with a
        by-participant condition slope in the truth and only a random
        intercept in the model, the model-based SE of the condition effect
        is too small and the cluster-robust one is not."""
        ratios = []
        for seed in range(12):
            counts = simulate_counts(slope_sd=0.9, seed=300 + seed)
            _, _, term_cols = fit_counts(counts)
            fit, names, _ = fit_counts(counts, cols=additive_cols(term_cols))
            j = names.index(f"condition[{CONDITIONS[1]}]")
            ratios.append(np.sqrt(fit["cov_robust"][j, j]) / np.sqrt(fit["cov"][j, j]))
        self.assertGreater(float(np.mean(ratios)), 1.3,
                           "the sandwich is not widening under a misspecified slope")

    def test_condition_likelihood_ratio_test_holds_its_size(self):
        """Type-I error of the headline test on data generated with NO
        condition effect. A test whose nominal p is not its actual size
        would make every claim in the tab a different claim."""
        rejected = 0
        reps = 60
        for seed in range(reps):
            counts = simulate_counts(b_cond=0.0, seed=400 + seed)
            _, _, term_cols = fit_counts(counts)
            full, _, _ = fit_counts(counts, cols=additive_cols(term_cols), robust=False)
            null, _, _ = fit_counts(counts, cols=[0] + term_cols["finger_id"], robust=False)
            chi2 = 2 * (full["loglik"] - null["loglik"])
            rejected += sstats.chi2.sf(max(chi2, 0.0), 1) < 0.05
        self.assertLessEqual(rejected / reps, 0.15,
                             f"rejected {rejected}/{reps} null datasets at alpha = .05")


# ---------------------------------------------------------------------------
# The reported result

class TestGlmmAccuracy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.events = make_events(n_participants=10, trials_per_condition=6,
                                 events_per_trial=20, accuracy=0.9, seed=4)
        cls.res = ga.glmm_accuracy(cls.events, CONDITIONS)

    def test_it_fits_and_says_so(self):
        self.assertIsNone(self.res["reason"])
        self.assertTrue(self.res["converged"])
        self.assertTrue(self.res["robust"])
        self.assertEqual(self.res["n_participants"], 10)

    def test_effects_are_the_three_the_anova_would_have_had(self):
        sources = [e["source"] for e in self.res["effects"]]
        self.assertEqual(sources, ["condition", "finger_id", "condition * finger_id"])
        df = {e["source"]: e["df"] for e in self.res["effects"]}
        self.assertEqual(df["condition"], 1)
        self.assertEqual(df["finger_id"], len(ga.FINGER_IDS) - 1)
        self.assertEqual(df["condition * finger_id"], len(ga.FINGER_IDS) - 1)
        for e in self.res["effects"]:
            self.assertGreaterEqual(e["chi2"], 0.0)
            self.assertTrue(0.0 <= e["p"] <= 1.0)

    def test_simple_effect_at_the_reference_finger_is_the_condition_coefficient(self):
        """The per-digit contrasts are built from the coefficient vector,
        so the reference digit's contrast has to come back as the
        coefficient itself - the check that the contrast vectors are
        indexed correctly."""
        coef = {c["term"]: c for c in self.res["coefficients"]}
        first = self.res["simple_effects"][0]
        self.assertEqual(first["finger_id"], ga.FINGER_IDS[0])
        self.assertAlmostEqual(first["log_or"],
                               coef[f"condition[{CONDITIONS[1]}]"]["estimate"], places=10)
        for s in self.res["simple_effects"]:
            self.assertAlmostEqual(s["odds_ratio"], float(np.exp(s["log_or"])), places=10)
            self.assertLess(s["or_lo"], s["or_hi"])

    def test_overall_condition_effect_comes_from_the_additive_model(self):
        """Not from the interaction model, where the condition coefficient
        is the REFERENCE DIGIT's effect and quoting it as the overall one
        would be wrong."""
        overall = self.res["condition_effect"]
        self.assertIsNotNone(overall)
        interaction_term = {c["term"]: c for c in self.res["coefficients"]}[
            f"condition[{CONDITIONS[1]}]"]
        self.assertNotAlmostEqual(overall["estimate"], interaction_term["estimate"], places=9)

    def test_a_uniform_fixture_shows_no_condition_effect(self):
        """finger_fixtures draws every event at one constant accuracy, so
        a model that reports a condition effect on it is reporting noise
        as signal."""
        overall = self.res["condition_effect"]
        self.assertGreater(overall["p"], 0.05)
        self.assertLess(abs(np.log(overall["odds_ratio"])), 0.6)

    def test_export_table_carries_every_reported_number(self):
        table = ga.glmm_effect_table(self.res)
        self.assertEqual(sorted(table["part"].unique()),
                         ["condition_effect", "fixed_effect", "lrt", "simple_effect"])
        self.assertEqual(int((table["part"] == "lrt").sum()), 3)
        self.assertEqual(int((table["part"] == "simple_effect").sum()), len(ga.FINGER_IDS))
        self.assertTrue((table["n_participants"] == 10).all())

    def test_icc_is_the_latent_scale_share(self):
        sigma = self.res["sigma"]
        self.assertAlmostEqual(self.res["icc"],
                               sigma ** 2 / (sigma ** 2 + np.pi ** 2 / 3), places=12)


# ---------------------------------------------------------------------------
# Nothing here may take the window down

class TestDegenerateData(unittest.TestCase):

    def test_below_the_minimum_n_it_reports_instead_of_fitting(self):
        events = make_events(n_participants=ga.MIN_TEST_N - 1, trials_per_condition=3,
                             events_per_trial=20, accuracy=0.9, seed=6)
        res = ga.glmm_accuracy(events, CONDITIONS)
        self.assertIsNotNone(res["reason"])
        self.assertIn(str(ga.MIN_TEST_N), res["reason"])
        self.assertEqual(res["effects"], [])

    def test_no_events_at_all(self):
        res = ga.glmm_accuracy([], CONDITIONS)
        self.assertIsNotNone(res["reason"])
        self.assertEqual(res["n_participants"], 0)

    def test_a_completely_separated_cell_is_named_not_fitted(self):
        """A condition x finger cell with no errors anywhere sends its
        coefficient to infinity. Reporting the cell beats printing a
        number produced by a diverging fit."""
        events = [e for e in make_events(n_participants=8, trials_per_condition=4,
                                         events_per_trial=20, accuracy=0.9, seed=7)]
        for e in events:
            if e["condition"] == CONDITIONS[1] and e["target_finger"][1] == "3":
                e["finger_correct"] = True
        res = ga.glmm_accuracy(events, CONDITIONS)
        self.assertIsNotNone(res["reason"])
        self.assertIn("separated", res["reason"])
        self.assertTrue(any("F3" in s for s in res["separation"]))

    def test_a_single_condition_still_returns_a_renderable_result(self):
        events = make_events(n_participants=8, trials_per_condition=4,
                             events_per_trial=20, accuracy=0.9, seed=9,
                             conditions=("B",))
        res = ga.glmm_accuracy(events, ["B"])
        self.assertIsInstance(res, dict)
        self.assertIn("reason", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
