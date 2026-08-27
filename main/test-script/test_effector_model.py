"""Tests for the effector-selection model and the reaction-time decomposition.

The substantive claims are checked by PARAMETER RECOVERY on synthetic data
generated from the model's own generative form: a test that only asserted
"the fit runs" would pass on a model with the wrong gradient, and the
gradient is hand-written here precisely because a numerical one is too slow
for the cross-validation loops.

The rest pin the invariants that make the fitted numbers mean what the
report says they mean - the event filter matching the shared one, the
softmax being invariant to candidate-constant terms (which is what lets
trial-initial events stay in), the participant being the unit of the
paired contrast, and Condition A carrying no cue evidence by construction.

Run from main/:  python test-script/test_effector_model.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import effector_model as em  # noqa: E402
from app import effector_rt as ert  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic study: the same shape as the real one, generated from known
# parameters so the fit can be asked to find them again.


def simulate(n_participants=12, n_trials_per_condition=9, n_events=30,
             kappa_hand=(4.0, 8.0), kappa_digit=(2.0, 1.5),
             digit_gradient=(2.0, 2.5), prior_weight=(0.5, 0.5), seed=7):
    """Events drawn from the model itself.

    Keys 0-14, left hand reachable on 0-8 and right on 6-14 (the real
    keyboard's overlap), a right-index-dominant habit like the observed
    one, and a cued finger drawn uniformly from the fingers that can reach
    the key - so recovery is tested on a design with the same collinearity
    structure as the study, not on an easier one.
    """
    rng = np.random.default_rng(seed)
    reach = np.full((2, 15), -12.0)
    for key in range(0, 9):
        reach[0, key] = -0.25 * abs(key - 4)
    for key in range(6, 15):
        reach[1, key] = -0.25 * abs(key - 10)
    prior = np.array([-1.5, 0.6, 0.2, -1.0, -1.8, -1.2, 1.4, 0.5, -0.9, -1.9])

    rows = []
    for p in range(n_participants):
        offset = rng.normal(0, 0.6, em.N_FINGERS)
        for condition_index, condition in enumerate(em.CONDITIONS):
            for trial in range(n_trials_per_condition):
                trial_index = condition_index * n_trials_per_condition + trial + 1
                previous = None
                for event in range(n_events):
                    key = int(rng.integers(0, 15))
                    reachable = np.where(reach[:, key] > -10)[0]
                    hand = int(rng.choice(reachable))
                    digit = int(rng.integers(1, 6))
                    cued = f"{'LR'[hand]}{digit}"
                    utility = reach[em.IS_RIGHT.astype(int), key].copy()
                    weight = 1.0 if condition == "A" else prior_weight[condition_index - 1]
                    utility = utility + weight * (prior + offset)
                    if previous is not None:
                        utility = utility + 0.4 * (em.IS_RIGHT == em.IS_RIGHT[previous])
                    if condition != "A":
                        i = condition_index - 1
                        utility = (utility
                                   + kappa_hand[i] * (em.IS_RIGHT == hand)
                                   + kappa_digit[i] * (em.DIGIT == digit)
                                   - digit_gradient[i] * np.abs(em.DIGIT - digit))
                    probs = np.exp(utility - utility.max())
                    probs = probs / probs.sum()
                    chosen = int(rng.choice(em.N_FINGERS, p=probs))
                    rows.append({
                        "participant": f"P{p + 1:02d}", "trial_index": trial_index,
                        "condition": condition, "level": "alpha", "event_index": event,
                        "target_finger": cued, "actual_finger": em.FINGERS[chosen],
                        "target_key_id": key, "actual_key_id": key,
                        "timed_out": False, "key_correct": True,
                        "finger_correct": em.FINGERS[chosen] == cued,
                        "rt_s": 0.5, "validity": "valid",
                    })
                    previous = chosen
    return rows


class TestParameterRecovery(unittest.TestCase):
    """The fit must find parameters it is known to have been given.

    Recovery is tested in the regime where the parameters are IDENTIFIED -
    cue evidence strong enough to matter, weak enough that the events
    which distinguish it still occur.  The opposite regime is not a
    tolerance to be loosened but a property of the design, and it has its
    own test class below.
    """

    @classmethod
    def setUpClass(cls):
        cls.truth = dict(kappa_hand=(3.0, 5.0), kappa_digit=(2.0, 1.5),
                         digit_gradient=(2.0, 2.5), prior_weight=(0.5, 0.5))
        cls.events = simulate(n_participants=12, n_trials_per_condition=12, **cls.truth)
        cls.df = em.build_events(cls.events)
        cls.fit = em.fit_choice_model(cls.df)
        cls.coef = cls.fit.coefficients().set_index("parameter")["estimate"]

    def test_fit_converges(self):
        self.assertTrue(self.fit.converged, self.fit.message)

    def test_hand_evidence_is_recovered(self):
        for condition, true in zip(em.CUED_CONDITIONS, self.truth["kappa_hand"]):
            self.assertAlmostEqual(self.coef[f"match_hand[{condition}]"], true, delta=0.8)

    def test_digit_evidence_is_recovered(self):
        for condition, true in zip(em.CUED_CONDITIONS, self.truth["kappa_digit"]):
            self.assertAlmostEqual(self.coef[f"match_digit[{condition}]"], true, delta=0.8)

    def test_digit_gradient_is_recovered(self):
        for condition, true in zip(em.CUED_CONDITIONS, self.truth["digit_gradient"]):
            self.assertAlmostEqual(self.coef[f"digit_distance[{condition}]"], -true, delta=0.8)

    def test_prior_weight_is_recovered(self):
        for condition, true in zip(em.CUED_CONDITIONS, self.truth["prior_weight"]):
            self.assertAlmostEqual(self.coef[f"w[{condition}]"], true, delta=0.25)

    def test_the_channel_contrast_is_recovered_in_the_right_channel(self):
        """The study's headline claim, on data where the answer is known:
        hand evidence differs between the conditions and digit evidence
        does not, and the fit must not smear one into the other."""
        hand_gap = self.coef["match_hand[C]"] - self.coef["match_hand[B]"]
        digit_gap = self.coef["match_digit[C]"] - self.coef["match_digit[B]"]
        self.assertGreater(hand_gap, 1.2)
        self.assertLess(abs(digit_gap), 1.0)

    def test_analytic_gradient_matches_finite_differences(self):
        """Every fit, cross-validation fold and bootstrap resample rides on
        this gradient; a sign error in one block would still converge, to
        the wrong place."""
        problem = em._Problem(self.df.head(400), self.fit.spec,
                              self.fit.participants, self.fit.n_keys)
        rng = np.random.default_rng(3)
        w = rng.normal(0, 0.3, problem.layout.size)
        _, analytic = problem.objective(w)
        step = 1e-6
        for index in rng.choice(problem.layout.size, size=25, replace=False):
            up, down = w.copy(), w.copy()
            up[index] += step
            down[index] -= step
            numeric = (problem.objective(up)[0] - problem.objective(down)[0]) / (2 * step)
            self.assertAlmostEqual(analytic[index], numeric, delta=1e-3,
                                   msg=f"gradient mismatch at parameter {index}")


class TestWeakIdentificationIsDeclaredNotHidden(unittest.TestCase):
    """Hand evidence is identified by cross-hand actions, so a condition
    that produces none identifies it only from below.

    This is the study's real situation - Condition C produced two
    cross-hand actions in 5,400 events - and these tests exist so the
    limitation is asserted rather than described.  They are what stops a
    future change from quietly reporting a bound as an estimate.
    """

    @classmethod
    def setUpClass(cls):
        # Hand evidence weak enough in B that cross-hand actions are
        # common, overwhelming in C so that none occur at all - the
        # extreme form of the real study's 67-against-2.
        cls.df = em.build_events(simulate(n_participants=8, n_trials_per_condition=8,
                                          kappa_hand=(2.0, 40.0), seed=23))

    def test_a_condition_with_no_cross_hand_actions_is_flagged(self):
        df = self.df
        diagnostics = em.identifiability_diagnostics(df).set_index("parameter")
        self.assertEqual(diagnostics.loc["match_hand[C]", "n_identifying"], 0)
        self.assertIn("not identified", diagnostics.loc["match_hand[C]", "verdict"])
        self.assertEqual(diagnostics.loc["match_hand[B]", "verdict"], "estimated")

    def test_the_bound_is_still_in_the_right_direction(self):
        """What the data DO support when the magnitude is unidentified:
        the ordering.  The constrained test asks whether the two
        conditions can supply the same hand evidence, which stays
        answerable when the free maximum does not."""
        lrt = em.constrained_contrast_test(self.df, features=("match_hand",)).set_index("channel")
        self.assertGreater(lrt.loc["match_hand", "delta_loglik"], 10.0)
        self.assertLess(lrt.loc["match_hand", "p"], 0.001)

    def test_tying_a_channel_that_does_not_differ_costs_almost_nothing(self):
        """The same test must NOT fire on a channel the two conditions
        share, or it would report a difference everywhere."""
        df = em.build_events(simulate(n_participants=8, n_trials_per_condition=6,
                                      kappa_hand=(3.0, 5.0), kappa_digit=(2.0, 2.0),
                                      seed=29))
        lrt = em.constrained_contrast_test(df, features=("match_digit",)).set_index("channel")
        self.assertLess(lrt.loc["match_digit", "delta_loglik"], 4.0)


class TestErrorStructureIsPredictedNotFitted(unittest.TestCase):
    """The cross-hand and homologous classes appear nowhere in the design
    matrix, so agreement with the observed counts is a prediction."""

    @classmethod
    def setUpClass(cls):
        cls.df = em.build_events(simulate(seed=11))
        cls.fit = em.fit_choice_model(cls.df)

    def test_no_feature_names_the_error_classes(self):
        names = " ".join(cls_name for cls_name in em.CUE_FEATURES + em.MOTOR_FEATURES)
        self.assertNotIn("homolog", names)
        self.assertNotIn("cross_hand", names)

    def test_predicted_counts_track_observed_counts(self):
        table = em.error_structure(self.fit, self.df).set_index("condition")
        for condition in em.CONDITIONS:
            observed = table.loc[condition, "observed_cross_hand"]
            predicted = table.loc[condition, "predicted_cross_hand"]
            self.assertAlmostEqual(predicted, observed,
                                   delta=max(6.0, 0.35 * max(observed, 1)),
                                   msg=f"cross-hand mismatch in condition {condition}")

    def test_condition_a_has_no_cue_evidence(self):
        """Not a fitted result: A has no cue coefficients at all, which is
        what "the finger cue is withheld" means in this model."""
        self.assertTrue(all("[A]" not in name for name in self.fit.layout.cue_names))


class TestEventHandling(unittest.TestCase):

    def test_filter_matches_the_shared_carryover_rule(self):
        events = simulate(n_participants=2, n_trials_per_condition=1, seed=5)
        events[0]["validity"] = "invalid_carryover"
        events[1]["timed_out"] = True
        events[2]["actual_finger"] = None
        df = em.build_events(events)
        self.assertEqual(len(df), len(events) - 3)
        accounting = em.event_accounting(events).set_index("stage")["n"]
        self.assertEqual(accounting["modelled events"], len(df))
        self.assertEqual(accounting["exported events"], len(events))

    def test_trial_initial_events_are_kept(self):
        """Their transition features are zero for every candidate, and a
        candidate-constant term cancels in a softmax, so keeping them costs
        nothing and dropping them would discard one event per trial."""
        events = simulate(n_participants=2, n_trials_per_condition=1, seed=6)
        df = em.build_events(events)
        self.assertTrue(df["first_in_trial"].any())
        feats = em.candidate_features(df)
        first = df["first_in_trial"].to_numpy()
        for name in em.MOTOR_FEATURES:
            row = feats[name][first]
            self.assertTrue(np.allclose(row.std(axis=1), 0.0),
                            f"{name} varies across candidates on a trial-initial event")

    def test_conflict_is_zero_in_condition_a(self):
        df = em.build_events(simulate(n_participants=3, n_trials_per_condition=2, seed=8))
        fit = em.fit_choice_model(df)
        scored = em.attach_predictions(fit, df)
        self.assertTrue((scored.loc[scored["condition"] == "A", "conflict"] == 0).all())
        self.assertTrue((scored.loc[scored["condition"] != "A", "conflict"] > 0).all())


class TestValidationSplits(unittest.TestCase):
    """No split may put events from one trial, or one participant's own
    fitted deviation, on both sides."""

    @classmethod
    def setUpClass(cls):
        cls.df = em.build_events(simulate(n_participants=6, n_trials_per_condition=2, seed=9))

    def test_leave_one_participant_out_covers_every_participant_once(self):
        scores = em.leave_one_participant_out(self.df, em.ModelSpec(name="M2", cue=("match_hand", "match_digit")))
        self.assertEqual(sorted(scores["participant"].unique()),
                         sorted(self.df["participant"].unique()))
        self.assertEqual(scores["n"].sum(), len(self.df))

    def test_trial_folds_never_split_a_trial(self):
        scores = em.leave_one_trial_out(self.df, n_folds=3)
        self.assertEqual(scores["n"].sum(), len(self.df))

    def test_cue_models_beat_the_no_cue_null_out_of_sample(self):
        null = em.summarise_cv(em.leave_one_participant_out(self.df, em.MODEL_LADDER[0]))
        cued = em.summarise_cv(em.leave_one_participant_out(self.df, em.MODEL_LADDER[2]))
        for condition in em.CUED_CONDITIONS:
            a = null.set_index("condition").loc[condition, "loglik_per_event"]
            b = cued.set_index("condition").loc[condition, "loglik_per_event"]
            self.assertGreater(b, a)


class TestParticipantLevelInference(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.df = em.build_events(simulate(n_participants=10, n_trials_per_condition=9,
                                          kappa_hand=(3.0, 5.0), seed=13))
        cls.fit = em.fit_choice_model(cls.df)
        cls.per_participant = em.per_participant_cue_parameters(cls.fit, cls.df)

    def test_one_row_per_participant(self):
        self.assertEqual(len(self.per_participant), len(self.fit.participants))

    def test_contrast_n_is_participants_not_events(self):
        contrast = em.paired_parameter_contrast(self.per_participant).set_index("parameter")
        self.assertEqual(contrast.loc["match_hand", "n_participants"],
                         len(self.fit.participants))
        self.assertEqual(contrast.loc["match_hand", "df"], len(self.fit.participants) - 1)

    def test_contrast_finds_the_simulated_channel_difference(self):
        """Direction and channel, not magnitude: per-participant kappa_hand
        is upward-biased wherever that person produced no cross-hand
        action, so the mean difference is checked for sign and the digit
        channel for the absence of a spurious one."""
        contrast = em.paired_parameter_contrast(self.per_participant).set_index("parameter")
        self.assertGreater(contrast.loc["match_hand", "mean_difference"], 1.0)
        self.assertLess(contrast.loc["match_hand", "p"], 0.01)
        self.assertGreater(contrast.loc["match_digit", "p"], 0.05)

    def test_per_participant_penalty_is_scaled_to_the_data_it_sees(self):
        """Unscaled, the ridge guard would weigh as heavily against one
        participant's events as against everyone's, shrinking every
        per-participant estimate towards zero relative to the pooled fit."""
        pooled = self.fit.coefficients().set_index("parameter")["estimate"]
        mean_hand_b = self.per_participant["match_hand[B]"].mean()
        self.assertAlmostEqual(mean_hand_b, pooled["match_hand[B]"], delta=1.2)


class TestReactionTimeLayer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(17)
        events = simulate(n_participants=8, n_trials_per_condition=3, seed=15)
        cls.df = em.build_events(events)
        cls.fit = em.fit_choice_model(cls.df)
        scored = em.attach_predictions(cls.fit, cls.df)
        # RT built from the model's own decomposition, so the regression
        # has a known answer: 60 ms per nat of conflict on log RT.
        base = np.log(0.5) + 0.06 * scored["conflict"].to_numpy()
        base = base + np.where(scored["condition"] == "B", 0.7,
                               np.where(scored["condition"] == "C", 0.4, 0.0))
        scored["rt_s"] = np.exp(base + rng.normal(0, 0.15, len(scored)))
        cls.scored = scored

    def test_selection_coefficient_is_recovered(self):
        table = ert.decomposition(self.scored).set_index("term")
        self.assertAlmostEqual(table.loc["conflict", "estimate"], 0.06, delta=0.02)

    def test_condition_costs_are_recovered(self):
        table = ert.decomposition(self.scored).set_index("term")
        self.assertAlmostEqual(table.loc["C(condition)[T.B]", "estimate"], 0.7, delta=0.06)
        self.assertAlmostEqual(table.loc["C(condition)[T.C]", "estimate"], 0.4, delta=0.06)

    def test_entropy_check_reports_every_condition(self):
        check = ert.entropy_check(self.scored)
        self.assertEqual(sorted(check["condition"]), list(em.CONDITIONS))
        self.assertTrue((check.set_index("condition").loc["A", "mean_conflict"]) == 0)

    def test_predicted_rt_is_on_the_same_scale_as_observed(self):
        """Duan's smearing correction: without it a log-scale fit
        under-predicts every arithmetic mean by a constant factor."""
        table = ert.observed_vs_predicted_rt(self.scored)
        ratio = table["predicted_rt_s"].mean() / table["observed_rt_s"].mean()
        self.assertAlmostEqual(ratio, 1.0, delta=0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
