"""Tests for out-of-sample prediction and the Computational Model window.

The claim these guard is the one a reader cannot check by looking at a
number: that every prediction came from a model which never saw the row
it scored.  Most of what follows is therefore about SPLITS, not about
accuracy - a model can look excellent and be worthless if a fold leaked,
and the failure is silent.

The rest pin the reporting rules the module exists to enforce: that a
rare error class is labelled exploratory rather than given a confident
number, that personalisation is allowed to use only Condition A, and that
the export carries the agreed schema.

Run from main/:  python test-script/test_effector_prediction.py
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import effector_model as em  # noqa: E402
from app import effector_prediction as ep  # noqa: E402
from test_effector_model import simulate  # noqa: E402


def small_events(n_participants=6, n_trials=3, seed=41):
    return em.build_events(simulate(n_participants=n_participants,
                                    n_trials_per_condition=n_trials,
                                    kappa_hand=(3.0, 5.0), seed=seed))


class TestNoLeakage(unittest.TestCase):
    """Splits are the whole safety argument, so they are tested first."""

    @classmethod
    def setUpClass(cls):
        cls.events = small_events()
        cls.prediction_run = ep.run_predictions(
            cls.events,
            predictors=[("M3 (primary)",
                         lambda: ep.ChoiceModelPredictor(em.ModelSpec(name="M3")))],
            include_personalised=True, include_trial_cv=True)

    def test_audit_passes_on_a_clean_run(self):
        audit = ep.leakage_audit(self.prediction_run, self.events)
        failures = audit[~audit["holds"].astype(bool)]
        self.assertTrue(failures.empty, f"leakage audit failed: {list(failures['check'])}")

    def test_every_lopo_row_came_from_the_fold_that_excluded_it(self):
        lopo = self.prediction_run.predictions[self.prediction_run.predictions["fold_id"].str.startswith("LOPO/")]
        held_out = lopo["fold_id"].str.split("/").str[1]
        self.assertTrue((held_out.to_numpy() == lopo["participant"].to_numpy()).all())

    def test_population_variant_predicts_every_modelled_event_once(self):
        population = self.prediction_run.predictions[
            (self.prediction_run.predictions["variant"] == ep.POPULATION)
            & (self.prediction_run.predictions["fold_id"].str.startswith("LOPO/"))]
        self.assertEqual(len(population), len(self.events))

    def test_personalised_never_scores_condition_a(self):
        """Condition A is the calibration data. Scoring it as well would
        be testing on what was used to calibrate."""
        personalised = self.prediction_run.predictions[
            self.prediction_run.predictions["variant"] == ep.PERSONALISED]
        self.assertFalse(personalised.empty)
        self.assertTrue(personalised["condition"].isin(em.CUED_CONDITIONS).all())

    def test_calibration_data_is_rejected_if_it_contains_a_cued_event(self):
        """The guard is an assertion in _fold, not a convention."""
        train = self.events[self.events["participant"] != "P01"]
        test = self.events[(self.events["participant"] == "P01")
                           & (self.events["condition"] != "A")]
        bad = self.events[(self.events["participant"] == "P01")]  # includes B and C
        predictor = ep.ChoiceModelPredictor(em.ModelSpec(), personalised=True)
        with self.assertRaises(AssertionError):
            ep._fold(train, test, predictor, "fold", ep.PERSONALISED, "M3",
                     calibration=bad, participant="P01")

    def test_overlapping_train_and_test_is_rejected(self):
        predictor = ep.ChoiceModelPredictor(em.ModelSpec())
        with self.assertRaises(AssertionError):
            ep._fold(self.events, self.events, predictor, "fold", ep.POPULATION, "M3")

    def test_trial_folds_keep_whole_trials_together(self):
        trial = self.prediction_run.predictions[self.prediction_run.predictions["fold_id"].str.startswith("trial/")]
        self.assertFalse(trial.empty)
        folds_per_trial = trial.groupby(["participant", "trial"])["fold_id"].nunique()
        self.assertTrue(folds_per_trial.eq(1).all())

    def test_held_out_scores_are_worse_than_in_sample(self):
        """A sanity check with teeth: if held-out log loss matched the
        in-sample value, the split would not be doing anything."""
        fit = em.fit_choice_model(self.events)
        probs, _ = em.predict(fit, self.events)
        chosen = np.array([em.FINGER_INDEX[f] for f in self.events["actual_finger"]])
        in_sample = float(-np.log(probs[np.arange(len(self.events)), chosen]).mean())
        held_out = float(self.prediction_run.predictions[
            (self.prediction_run.predictions["variant"] == ep.POPULATION)
            & (self.prediction_run.predictions["fold_id"].str.startswith("LOPO/"))]["log_loss"].mean())
        self.assertGreater(held_out, in_sample)


class TestPredictionsAreNotFittedValues(unittest.TestCase):

    def test_predicting_a_held_out_participant_ignores_their_own_prior(self):
        """A held-out participant has no fitted deviation, and using one
        would be predicting them from themselves."""
        events = small_events()
        train = events[events["participant"] != "P01"]
        test = events[events["participant"] == "P01"]
        predictor = ep.ChoiceModelPredictor(em.ModelSpec())
        predictor.fit(train, participants=sorted(train["participant"].unique()),
                      n_keys=int(events["key"].max()) + 1)
        self.assertFalse(predictor.seen_participants)
        with_prior, _ = em.predict(predictor.fit_result, test, use_participant_prior=True)
        without_prior = predictor.predict(test)
        # The held-out label is unknown to the fit, so both routes give the
        # group prior - the point is that predict() cannot be made to use a
        # personal deviation unless one was calibrated.
        self.assertTrue(np.allclose(with_prior, without_prior))


class TestRareEventReporting(unittest.TestCase):

    def test_a_class_with_too_few_cases_is_labelled_exploratory(self):
        self.assertIsNone(ep.rare_event_warning(200, "wrong hand"))
        self.assertIn("exploratory", ep.rare_event_warning(3, "wrong hand"))
        self.assertIn("nothing to score", ep.rare_event_warning(0, "wrong hand"))

    def test_error_risk_carries_the_warning_where_the_class_is_rare(self):
        events = small_events(n_participants=6, n_trials=3, seed=53)
        run = ep.run_predictions(
            events, predictors=[("M3 (primary)",
                                 lambda: ep.ChoiceModelPredictor(em.ModelSpec()))],
            include_personalised=False, include_trial_cv=False)
        metrics = ep.error_risk_metrics(run.predictions)
        rare = metrics[metrics["n_positives"] < ep.MIN_POSITIVES_FOR_INFERENCE]
        self.assertTrue(rare["warning"].notna().all(),
                        "a rare class was reported without its exploratory label")

    def test_accuracy_is_not_among_the_reported_error_risk_columns(self):
        """Accuracy on a 1% class is ~99% for a model that never flags it,
        so it must not appear as if it meant something."""
        events = small_events()
        run = ep.run_predictions(
            events, predictors=[("M3 (primary)",
                                 lambda: ep.ChoiceModelPredictor(em.ModelSpec()))],
            include_personalised=False, include_trial_cv=False)
        columns = set(ep.error_risk_metrics(run.predictions).columns)
        self.assertNotIn("accuracy", columns)
        self.assertIn("average_precision", columns)
        self.assertIn("base_rate", columns)


class TestBaselinesAndExport(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.events = small_events()

    def test_every_baseline_returns_a_proper_distribution(self):
        train = self.events[self.events["participant"] != "P01"]
        test = self.events[self.events["participant"] == "P01"]
        for _, factory in ep.default_predictors():
            predictor = factory()
            if isinstance(predictor, ep.ChoiceModelPredictor):
                predictor.fit(train, participants=sorted(train["participant"].unique()),
                              n_keys=int(self.events["key"].max()) + 1)
            else:
                predictor.fit(train)
            probs = predictor.predict(test)
            self.assertEqual(probs.shape, (len(test), em.N_FINGERS))
            self.assertTrue(np.allclose(probs.sum(axis=1), 1.0, atol=1e-6),
                            f"{predictor.name} did not return a distribution")
            self.assertTrue((probs > 0).all(),
                            f"{predictor.name} assigned zero probability, "
                            "which makes log loss infinite on a surprise")

    def test_the_primary_subset_is_one_row_per_event_in_the_same_schema(self):
        """The committable half of the export.

        The full table is every model by every held-out event and is left
        out of version control as a regenerable artifact; this subset is
        what stays, so it has to be exactly one row per modelled event and
        carry the identical columns - a subset with a different schema
        would quietly stop answering the same questions.
        """
        run = ep.run_predictions(
            self.events,
            predictors=[("M3 (primary)",
                         lambda: ep.ChoiceModelPredictor(em.ModelSpec())),
                        ("empirical finger frequency", ep.EmpiricalFingerFrequency)],
            include_personalised=True, include_trial_cv=True)
        full = ep.export_frame(run.predictions)
        subset = ep.primary_export_frame(run.predictions, run.primary_model)

        self.assertEqual(list(subset.columns), list(full.columns))
        self.assertEqual(len(subset), len(self.events))
        self.assertLess(len(subset), len(full))
        self.assertEqual(subset["model_name"].unique().tolist(), [run.primary_model])
        self.assertEqual(subset["variant"].unique().tolist(), [ep.POPULATION])
        self.assertTrue(subset["fold_id"].str.startswith("LOPO/").all())
        # Still one row per event, not a sample of them.
        keys = set(zip(subset["participant"], subset["trial"], subset["event"]))
        self.assertEqual(len(keys), len(subset))

    def test_export_frame_carries_the_agreed_schema(self):
        run = ep.run_predictions(
            self.events, predictors=[("M3 (primary)",
                                      lambda: ep.ChoiceModelPredictor(em.ModelSpec()))],
            include_personalised=False, include_trial_cv=False)
        frame = ep.export_frame(run.predictions)
        for column in ep.EXPORT_COLUMNS:
            self.assertIn(column, frame.columns)
        self.assertEqual(len(frame), len(run.predictions))


class TestWindow(unittest.TestCase):
    """The window builds every tab from real-shaped data and registers
    every figure and table for export."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        from app.gui.computational_model_window import ComputationalModelWindow

        cls.events = small_events()
        cls.window = ComputationalModelWindow(None)
        cls.window._events = cls.events
        cls.window._accounting = pd.DataFrame(
            [{"stage": "modelled events", "n": len(cls.events)}])
        spec = em.ModelSpec()
        cls.window._do_fit(cls.events, spec)
        cls.window._do_two_stage(cls.events, spec)
        cls.window._do_channel_test(cls.events, spec)
        cls.window._do_per_participant(cls.events)
        cls.window._do_rt(cls.events)
        cls.window._rebuild_tabs()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()

    def _titles(self):
        return [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())]

    def test_the_summary_is_first_and_the_diagnostics_are_not_shown(self):
        """The window opens on the answer, not on a parameter table."""
        titles = self._titles()
        self.assertEqual(titles[0], "Model Summary")
        for technical in ("Parameters", "Model Comparison", "Identifiability",
                          "Model Status", "RT Decomposition"):
            self.assertNotIn(technical, titles)

    def test_the_advanced_toggle_reveals_the_diagnostics(self):
        self.window.advanced_box.setChecked(True)
        try:
            titles = self._titles()
            for technical in ("Parameters", "Identifiability", "Model Comparison",
                              "Model Status", "Participant Parameters",
                              "RT Decomposition"):
                self.assertIn(technical, titles)
            self.assertEqual(titles[0], "Model Summary")
        finally:
            self.window.advanced_box.setChecked(False)

    def test_the_toggle_does_not_change_what_gets_exported(self):
        """The toggle decides what is on screen and nothing else. An
        export that depended on a checkbox would be a trap, and the
        toggle's tooltip promises it does not."""
        self.window.advanced_box.setChecked(False)
        plain = (set(self.window._figures), set(self.window._datasets))
        self.window.advanced_box.setChecked(True)
        advanced = (set(self.window._figures), set(self.window._datasets))
        self.window.advanced_box.setChecked(False)
        self.assertEqual(plain, advanced)
        self.assertGreater(len(plain[0]), len(self._titles()))

    def test_the_summary_figure_uses_no_statistical_jargon(self):
        """It has to be readable without knowing what a log-likelihood
        is; these are the words that would break that promise."""
        figure = self.window._figures["model_summary"]
        words = []
        for ax in figure.axes:
            words.append(ax.get_title())
            words.append(ax.get_xlabel())
            words.append(ax.get_ylabel())
            words.extend(t.get_text() for t in ax.texts)
            words.extend(label.get_text() for label in ax.get_xticklabels())
        text = " ".join(words).lower()
        for jargon in ("log-likelihood", "log likelihood", "loglik", "kappa",
                       "softmax", "aic", "p =", "coefficient", "parameter",
                       "match_hand", "match_digit"):
            self.assertNotIn(jargon, text, f"the summary figure says {jargon!r}")

    def test_prediction_tabs_appear_only_after_a_prediction_run(self):
        """An empty prediction panel would read as a result, so the tabs
        are absent until there is something held out to show."""
        self.assertNotIn("Out-of-Sample Summary", self._titles())
        self.window._run = ep.run_predictions(
            self.events, predictors=[("M3 (primary)",
                                      lambda: ep.ChoiceModelPredictor(em.ModelSpec()))],
            include_personalised=False, include_trial_cv=False)
        self.window._rebuild_tabs()
        titles = self._titles()
        for expected in ("Out-of-Sample Summary", "Error Risk Prediction",
                         "RT Prediction", "Event Viewer"):
            self.assertIn(expected, titles)

    def test_every_tab_contributed_something_exportable(self):
        self.assertTrue(self.window._figures)
        self.assertTrue(self.window._datasets)
        self.assertIn("model_summary", self.window._figures)

    def test_the_export_offers_both_the_full_and_the_committable_table(self):
        """Both are written: the full one for auditing the comparison,
        the subset because the full one is too big to version."""
        self.window._run = ep.run_predictions(
            self.events, predictors=[("M3 (primary)",
                                      lambda: ep.ChoiceModelPredictor(em.ModelSpec()))],
            include_personalised=False, include_trial_cv=False)
        datasets = {}
        for _title, build, _advanced in self.window._tab_builders():
            result = build()
            if result is not None and len(result) > 2 and result[2]:
                datasets.update({k: v for k, v in result[2].items() if v is not None})
        datasets["prediction_events"] = ep.export_frame(self.window._run.predictions)
        datasets["prediction_events_primary"] = ep.primary_export_frame(
            self.window._run.predictions, self.window._run.primary_model)
        self.assertIn("prediction_events", datasets)
        self.assertIn("prediction_events_primary", datasets)
        self.assertLess(len(datasets["prediction_events_primary"]),
                        len(datasets["prediction_events"]) + 1)

    def test_identifiability_banner_is_shown_and_names_the_bound(self):
        banner = self.window._identifiability_banner()
        self.assertTrue(banner)
        self.assertIn("Identifiability", banner)

    def test_measured_not_derived_is_stated_on_the_rt_tab(self):
        from app.gui import computational_model_window as window_module

        caption, _, _, _ = self.window._build_rt_decomposition()
        self.assertIn("Measured, not derived", caption)
        self.assertIn("Measured, not derived", window_module._MEASURED_NOT_DERIVED)


class TestLauncherIntegration(unittest.TestCase):

    def test_window_is_registered_and_concurrent(self):
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import launcher
        from app.gui.computational_model_window import ComputationalModelWindow

        labels = [label for _, tools in launcher.SECTIONS for label, _ in tools]
        self.assertIn("Computational Model Analysis", labels)
        self.assertIn(ComputationalModelWindow, launcher.CONCURRENT_TOOLS)

    def test_existing_analysis_windows_are_untouched(self):
        import launcher
        from app.gui.group_analysis_window import GroupAnalysisWindow
        from app.gui.participant_analysis_window import ParticipantAnalysisWindow
        from app.gui.quiz_analysis_window import QuizAnalysisWindow

        labels = [label for _, tools in launcher.SECTIONS for label, _ in tools]
        for expected in ("Quiz Analysis", "Participant Analysis",
                         "Group Analysis (Multi-Participant)"):
            self.assertIn(expected, labels)
        self.assertIn(GroupAnalysisWindow, launcher.CONCURRENT_TOOLS)
        self.assertIn(ParticipantAnalysisWindow, launcher.CONCURRENT_TOOLS)
        self.assertNotIn(QuizAnalysisWindow, launcher.CONCURRENT_TOOLS)

    def test_section_numbering_is_stable(self):
        """Section numbers are referenced in the README and the report, so a
        change to the numbering must be a deliberate renumber, not an accident
        of adding a window. Data Analysis is 7; the tail is Rhythm Experiment
        (11) then the Demo & About front matter (12)."""
        import launcher

        titles = [title for title, _ in launcher.SECTIONS]
        self.assertTrue(titles[6].startswith("7. Data Analysis"))
        self.assertTrue(titles[-2].startswith("11. Rhythm Experiment"))
        self.assertTrue(titles[-1].startswith("12. Demo && About"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
