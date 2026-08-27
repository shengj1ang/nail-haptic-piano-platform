"""Computational Model Analysis - launcher section 7.

A model of WHICH FINGER acted, fitted to the Main User Study events and
then made to predict events it never saw.  The other two analysis windows
describe what happened; this one asks whether one mechanism accounts for
it, and whether that mechanism generalises to a participant the model has
not met.

The window has two halves, and the tab strip keeps them apart on purpose:

  Model      - the fit itself.  Parameters, model comparison, error
               structure, the Condition-A habitual prior, and the
               reaction-time decomposition.  Everything here is in
               sample except where a tab says otherwise.

  Prediction - held-out only.  Every number under these tabs comes from a
               model that did not see the row it is scoring, produced by
               app.effector_prediction, whose folds are the single place
               a train/test split is made.  In-sample fitted values never
               appear on this side of the strip.

Two limitations are shown in the window rather than left to a caption,
because both are the kind that a reader will otherwise assume away:

  - Condition C produced two cross-hand actions in 5,400 events, so the
    hand-evidence parameter is identified only from below.  The C - B
    difference is a LOWER BOUND.  identifiability_diagnostics() recomputes
    that from whatever data is loaded, and the banner is red when a
    parameter on screen is a bound rather than an estimate.
  - The reaction-time model's per-condition term is MEASURED, NOT
    DERIVED.  Predicting reaction time well does not explain the 247 ms
    difference between the modalities; the model says what that cost is,
    not why it exists.

Structure follows app/gui/group_analysis_window.py exactly - participant
list on the left, WrappingTabWidget on the right, _tab_builders() as the
single list that IS the analysis, _add_tab registering figures and tables
so a tab cannot ship without its export.  Fitting and prediction run
through app.gui.progress_task because a full prediction run is a few
hundred model fits.
"""

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import effector_model as em
from .. import effector_prediction as ep
from .. import effector_rt as ert
from .. import group_analysis as ga
from ..pilot_study import DATA_DIR as STUDY_DATA_DIR
from .analysis_export import export_analysis
from .progress_task import run_with_progress
from .participant_analysis_window import CONDITION_COLORS, ScrollFriendlyCanvas
from .stats_format import fmt
from .wrapping_tabs import WrappingTabWidget

MODEL_FIGURE_DIR = STUDY_DATA_DIR / "model_figures"
MODEL_FIT_DIR = STUDY_DATA_DIR / "model_fits"

# Colours reused from the other analysis windows so a condition means the
# same thing across the whole application.
FINGER_ORDER = em.FINGERS
OUTCOME_COLORS = {
    ep.OUTCOME_CORRECT: "#4a9a5a",
    ep.OUTCOME_WITHIN_HAND: "#d9a03d",
    ep.OUTCOME_HOMOLOGOUS: "#c4453a",
    ep.OUTCOME_CROSS_OTHER: "#8a4a8a",
}

_IDENTIFIABILITY_BANNER = (
    "<b style='color:#b03a2e'>Identifiability:</b> {detail} "
    "The C &minus; B hand-evidence difference is therefore a <b>lower bound</b>, "
    "not a point estimate: its sign rests on {b_events} cross-hand actions under B "
    "against {c_events} under C, which is robust, while its size does not. "
    "Report the likelihood-ratio channel test, not the parameter value."
)

_MEASURED_NOT_DERIVED = (
    "<b style='color:#b03a2e'>Measured, not derived:</b> the per-condition reaction-time "
    "term is a fitted constant for each cue modality. The model states what the visual "
    "cue costs relative to the haptic one; it does not derive that cost from the choice "
    "parameters. Adding the selection-conflict term leaves the B &minus; C gap essentially "
    "unchanged, so accurate reaction-time prediction is <b>not</b> evidence that the "
    "mechanism behind the difference has been explained."
)


class ComputationalModelWindow(QMainWindow):
    def __init__(self, cfg=None):
        super().__init__()
        self.setWindowTitle("Computational Model Analysis")
        self.resize(1420, 920)
        self._figures: Dict[str, Figure] = {}
        self._datasets: Dict[str, object] = {}
        self._fit: Optional[em.ChoiceFit] = None
        self._events: Optional[pd.DataFrame] = None
        self._scored: Optional[pd.DataFrame] = None
        self._run: Optional[ep.PredictionRun] = None
        self._log_lines: List[str] = []

        self._build_controls()
        self._build_layout()
        self._refresh_participants()

    # ------------------------------------------------------------------
    # Controls

    def _build_controls(self) -> None:
        self.participant_list = QListWidget()
        self.participant_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)

        # No model picker. There is one primary model and the comparison
        # against the simpler alternatives is part of what fitting MEANS
        # here, not a choice to be made in the interface: a reader asking
        # "why these parameters" is owed the ladder, and a reader who is
        # not asking should never have to pick a model to get an answer.
        self.advanced_box = QCheckBox("Show advanced diagnostics")
        self.advanced_box.setChecked(False)
        self.advanced_box.setToolTip(
            "Parameter tables, identifiability, the model ladder and ablations, "
            "calibration and the per-fold audit.\n\n"
            "Everything is computed either way and everything is exported either way; "
            "this only decides how much of it is on screen.")
        self.advanced_box.toggled.connect(self._advanced_toggled)

        self.two_stage_box = QCheckBox("Estimate the habitual prior from Condition A alone")
        self.two_stage_box.setChecked(True)
        self.two_stage_box.setToolTip(
            "Two-stage fit. The prior is estimated on the key-only trials, then frozen, so "
            "the weight w_c measures how much of the FREE-CHOICE habit still acts when a "
            "finger is named. Unticked, the prior is estimated from all three conditions at "
            "once, which is more efficient but lets the cued trials help decide what the "
            "habit is.")

        self.cv_box = QCheckBox("Cross-validate (leave one participant out)")
        self.cv_box.setChecked(True)
        self.cv_box.setToolTip(
            "On by default, and it is what makes the model comparison meaningful: the "
            "simpler alternatives and the ablations are all refitted twenty times, once "
            "per held-out participant, so they are ranked on data none of them saw.\n\n"
            "Nothing here has to be run model by model — one Fit does the whole "
            "comparison. Untick it only to get a quick fit while exploring; the results "
            "quoted in a report need it on.")
        self.bootstrap_spin = QSpinBox()
        self.bootstrap_spin.setRange(0, 2000)
        self.bootstrap_spin.setValue(300)
        self.bootstrap_spin.setSingleStep(50)
        self.bootstrap_spin.setToolTip(
            "Bootstrap resamples for the parameter intervals, resampling PARTICIPANTS with "
            "replacement. 0 skips it. Events within a person are not independent, so an "
            "event-level bootstrap would give intervals several times too narrow.\n\n"
            "Each resample is a full refit — roughly two seconds on twenty participants, so "
            "300 takes about ten minutes. The progress dialog shows the count and a time "
            "estimate while it runs and can be cancelled. Lower it while exploring; the "
            "intervals quoted in a report should come from a full run.")

        self.personalised_box = QCheckBox("Also predict personalised (own Condition A)")
        self.personalised_box.setChecked(True)
        self.personalised_box.setToolTip(
            "Adds a second prediction pass in which a held-out participant's own key-only "
            "trials calibrate their habitual prior before their cued trials are predicted. "
            "Their B and C events are never used. This is set up as a comparison with a "
            "genuine possible null, not as an improvement.")

        self.trial_cv_box = QCheckBox("Also run the within-participant trial split")
        self.trial_cv_box.setChecked(True)

        self.fit_btn = QPushButton("Fit model")
        self.fit_btn.clicked.connect(self._fit_model)
        self.predict_btn = QPushButton("Run out-of-sample prediction")
        self.predict_btn.clicked.connect(self._run_prediction)
        self.predict_btn.setEnabled(False)
        self.predict_btn.setToolTip(
            "Leave-one-participant-out over every model in the comparison set, then the "
            "within-participant trial split for the primary model. A few hundred fits - "
            "minutes, not seconds.")
        self.load_btn = QPushButton("Load fitted model")
        self.load_btn.clicked.connect(self._load_fit)
        self.save_btn = QPushButton("Save fitted model")
        self.save_btn.clicked.connect(self._save_fit)
        self.save_btn.setEnabled(False)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.clicked.connect(self._reset)
        self.export_btn = QPushButton("Export figures + data")
        self.export_btn.clicked.connect(self._export)
        self.export_btn.setEnabled(False)
        self.export_summary_btn = QPushButton("Export summary figure (for the report)")
        self.export_summary_btn.setToolTip(
            "Writes computational_model_summary.png and .svg — the three-panel summary on "
            "its own, at report resolution, with no table, log or interface around it.\n\n"
            "The same figure the Model Summary tab shows, re-rendered at the size a figure "
            "is placed at rather than the size a window happens to be.")
        self.export_summary_btn.clicked.connect(self._export_summary_figure)
        self.export_summary_btn.setEnabled(False)

    def _build_layout(self) -> None:
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_participants)
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._set_all_checks(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._set_all_checks(False))

        left = QWidget()
        left.setFixedWidth(330)
        layout = QVBoxLayout(left)
        layout.addWidget(QLabel("Participants (data/MainUserStudy):"))
        layout.addWidget(self.participant_list, 1)
        row = QHBoxLayout()
        row.addWidget(refresh_btn)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        layout.addLayout(row)

        layout.addWidget(self.two_stage_box)
        layout.addWidget(self.cv_box)
        boot_row = QHBoxLayout()
        boot_row.addWidget(QLabel("Bootstrap resamples:"))
        boot_row.addWidget(self.bootstrap_spin)
        layout.addLayout(boot_row)
        layout.addWidget(self.fit_btn)
        layout.addWidget(self.advanced_box)

        layout.addSpacing(8)
        layout.addWidget(QLabel("Out-of-sample prediction:"))
        layout.addWidget(self.personalised_box)
        layout.addWidget(self.trial_cv_box)
        layout.addWidget(self.predict_btn)

        layout.addSpacing(8)
        for button in (self.load_btn, self.save_btn, self.export_summary_btn,
                       self.export_btn, self.reset_btn):
            layout.addWidget(button)

        self.header_label = QLabel("No model fitted yet.")
        header_font = self.header_label.font()
        header_font.setBold(True)
        self.header_label.setFont(header_font)
        self.status_label = QLabel(
            "Tick participants, choose a model, then press “Fit model”. "
            "Prediction becomes available once a model has been fitted.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.RichText)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setPlaceholderText("Fitting log")

        self.tabs = WrappingTabWidget()

        right_top = QWidget()
        right_layout = QVBoxLayout(right_top)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.header_label)
        right_layout.addWidget(self.status_label)
        right_layout.addWidget(self.tabs, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(right_top)
        splitter.addWidget(self.log_view)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.addWidget(left)
        outer.addWidget(splitter, 1)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # Participant selection (mirrors the group window)

    def _refresh_participants(self) -> None:
        checked = {
            self.participant_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.participant_list.count())
            if self.participant_list.item(i).checkState() == Qt.CheckState.Checked
        }
        first_load = self.participant_list.count() == 0
        self.participant_list.clear()
        for status in ga.scan_participants():
            item = QListWidgetItem(f"{status.participant} — {status.summary()}")
            item.setData(Qt.ItemDataRole.UserRole, status.participant)
            item.setToolTip(status.summary())
            if status.ready:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # Default to every ready participant: the model needs the
                # whole group, and a partial selection is the exception.
                item.setCheckState(Qt.CheckState.Checked
                                   if (first_load or status.participant in checked)
                                   else Qt.CheckState.Unchecked)
            else:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.participant_list.addItem(item)

    def _set_all_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.participant_list.count()):
            item = self.participant_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(state)

    def _checked_participants(self) -> List[str]:
        return [
            self.participant_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.participant_list.count())
            if self.participant_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    # ------------------------------------------------------------------
    # Logging

    @staticmethod
    def _reporter(report, prefix: str = ""):
        """Adapt a StepProgress to the (message) callbacks the analysis
        modules already take.

        These callers do not know their own totals, so no fraction is
        passed and the bar sweeps back and forth inside the step - which
        says "working, cannot say how far" rather than "frozen"."""
        if report is None:
            return None

        def progress(message: str) -> None:
            report(f"{prefix}{message}")

        return progress

    def _log(self, message: str) -> None:
        self._log_lines.append(message)
        self.log_view.appendPlainText(message)

    def _advanced_toggled(self, _checked: bool) -> None:
        """Re-render with or without the diagnostic tabs.

        Nothing is recomputed - the toggle only decides what is shown, so
        it is instant and cannot change a number."""
        if self._fit is not None:
            self._rebuild_tabs()

    # ------------------------------------------------------------------
    # Fitting

    def _fit_model(self) -> None:
        selection = self._checked_participants()
        if not selection:
            self.status_label.setText("No participants selected — tick at least one.")
            return
        self._reset_results()
        spec = em.PRIMARY_SPEC
        self._log(f"Loading {len(selection)} participant(s)…")
        data = ga.load_group(selection)
        if not data.included:
            self.status_label.setText(
                "None of the selected participants could be loaded: "
                + "; ".join(f"{p}: {msg}" for p, msg in data.errors.items()))
            return

        try:
            events = em.build_events(data.event_rows)
        except em.EffectorModelError as error:
            QMessageBox.warning(self, "Nothing to model", str(error))
            return
        self._events = events
        self._accounting = em.event_accounting(data.event_rows)
        self._log(f"{len(events)} modelled events from {events['participant'].nunique()} "
                  f"participant(s); keys 0–{int(events['key'].max())}.")

        # Steps that can run for minutes take the progress reporter (see
        # app.gui.progress_task): they call it as they go, which is what
        # keeps the bar moving and Cancel reachable. The quick ones stay
        # zero-argument.
        steps = [("Fitting the choice model", lambda: self._do_fit(events, spec))]
        if self.two_stage_box.isChecked():
            steps.append(("Two-stage fit (prior from Condition A)",
                          lambda: self._do_two_stage(events, spec)))
        steps.append(("Channel likelihood-ratio test",
                      lambda report: self._do_channel_test(events, spec, report)))
        steps.append(("Per-participant cue parameters",
                      lambda report: self._do_per_participant(events, report)))
        if self.bootstrap_spin.value():
            steps.append((f"Bootstrap ({self.bootstrap_spin.value()} resamples)",
                          lambda report: self._do_bootstrap(events, spec, report)))
        if self.cv_box.isChecked():
            steps.append(("Model comparison (leave one participant out)",
                          lambda report: self._do_model_comparison(events, report)))
        steps.append(("Reaction-time decomposition", lambda: self._do_rt(events)))
        steps.append(("Building tabs", self._rebuild_tabs))

        if not run_with_progress(self, "Fitting the effector-selection model", steps):
            self._reset_results()
            self.status_label.setText("Fit cancelled — nothing to show or export.")
            return

        self.predict_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.export_summary_btn.setEnabled(True)

    def _do_fit(self, events: pd.DataFrame, spec: em.ModelSpec) -> None:
        self._fit = em.fit_choice_model(events, spec)
        self._scored = em.attach_predictions(self._fit, events)
        self._log(f"{spec.name}: log-likelihood {self._fit.loglik:.1f}, "
                  f"{self._fit.n_params} parameters, AIC {self._fit.aic:.1f}, "
                  f"converged={self._fit.converged}.")
        if not self._fit.converged:
            self._log(f"  optimiser message: {self._fit.message}")

    def _do_two_stage(self, events: pd.DataFrame, spec: em.ModelSpec) -> None:
        self._two_stage = em.fit_two_stage(events, spec)
        self._prior_bounds = em.prior_weight_bounds(events, spec)
        weights = self._two_stage.cue_stage.layout.block(
            self._two_stage.cue_stage.params, "prior_weight")
        self._log("Two-stage prior weight: "
                  + ", ".join(f"w[{c}] = {w:.3f}"
                              for c, w in zip(em.CUED_CONDITIONS, weights)))

    def _do_channel_test(self, events: pd.DataFrame, spec: em.ModelSpec,
                         report=None) -> None:
        self._channel_test = em.constrained_contrast_test(
            events, spec, progress=self._reporter(report, "tying "))
        self._identifiability = em.identifiability_diagnostics(events)
        self._cross_hand = em.cross_hand_rates(events)
        for row in self._channel_test.itertuples():
            self._log(f"Channel {row.channel}: tying B and C costs "
                      f"{row.delta_loglik:.2f} log-likelihood "
                      f"(chi2({row.df}) = {row.chi2:.1f}).")

    def _do_per_participant(self, events: pd.DataFrame, report=None) -> None:
        self._per_participant = em.per_participant_cue_parameters(
            self._fit, events, progress=self._reporter(report))
        self._contrast = em.paired_parameter_contrast(self._per_participant)

    def _do_bootstrap(self, events: pd.DataFrame, spec: em.ModelSpec,
                      report=None) -> None:
        # The bootstrap is the slowest thing in the window by a wide
        # margin - several hundred model fits - so it reports every
        # resample, with a fraction, which is what gives the bar a real
        # position and the label a time estimate.
        total = self.bootstrap_spin.value()

        def progress(message: str, done: int = 0) -> None:
            if report is not None:
                report(message, done / total if total else None)
            if done in (1, total) or done % 50 == 0:
                self._log(f"  bootstrap {message}")

        self._bootstrap = em.bootstrap_parameters(
            events, spec, n_resamples=total, progress=progress)
        unidentified = self._bootstrap[self._bootstrap["n_resamples_unidentified"] > 0]
        for row in unidentified.itertuples():
            self._log(f"  {row.parameter}: {row.n_resamples_unidentified} resample(s) "
                      f"carried no event able to identify it — upper interval end is a bound.")

    def _do_model_comparison(self, events: pd.DataFrame, report=None) -> None:
        # Every model in the ladder is cross-validated over every
        # participant, so the total is known and the bar can be honest
        # about how far through the whole comparison it is.
        ladder = list(em.MODEL_LADDER)
        ablations = list(em.ABLATIONS)
        participants = max(events["participant"].nunique(), 1)
        total = (len(ladder) + len(ablations)) * (participants + 1)
        self._comparison_done = 0

        def progress(message: str) -> None:
            self._comparison_done += 1
            if report is not None:
                report(message, min(self._comparison_done / total, 1.0))
            self._log(f"  {message}")

        self._ladder = em.compare_models(events, ladder, progress=progress)
        self._ablations = em.compare_models(events, ablations, progress=progress)

    def _do_rt(self, events: pd.DataFrame) -> None:
        scored = self._scored
        self._rt_table = ert.decomposition(scored)
        self._rt_components = ert.component_comparison(scored)
        self._rt_split = ert.selection_vs_execution(scored)
        self._rt_interaction = ert.condition_by_digit(scored)
        self._rt_entropy = ert.entropy_check(scored)
        self._rt_link = ert.evidence_time_link(self._per_participant, scored)
        self._rt_observed = ert.observed_vs_predicted_rt(scored)
        self._rt_residuals = ert.residual_diagnostics(scored)

    # ------------------------------------------------------------------
    # Prediction

    def _run_prediction(self) -> None:
        if self._events is None:
            return
        events = self._events
        self._log("Starting out-of-sample prediction — this is a few hundred model fits.")
        predictors = ep.default_predictors()

        # One progress call per participant per model - the personalised
        # pass shares the fold, so it does not double the count - plus the
        # trial-split folds. Known in advance, so the bar shows a real
        # position through what is otherwise several hundred silent fits.
        participants = max(events["participant"].nunique(), 1)
        total = len(predictors) * participants
        if self.trial_cv_box.isChecked():
            total += ep.DEFAULT_TRIAL_FOLDS
        self._prediction_done = 0

        def do_run(report) -> None:
            def progress(message: str) -> None:
                self._prediction_done += 1
                report(message, min(self._prediction_done / total, 1.0))
                self._log(f"  {message}")

            self._run = ep.run_predictions(
                events, predictors,
                include_personalised=self.personalised_box.isChecked(),
                include_trial_cv=self.trial_cv_box.isChecked(),
                progress=progress)

        if not run_with_progress(self, "Out-of-sample prediction",
                                 [("Predicting held-out events", do_run),
                                  ("Building prediction tabs", self._rebuild_tabs)]):
            self.status_label.setText(
                "Prediction cancelled — the fitted model is still available.")
            return
        audit = ep.leakage_audit(self._run, events)
        failed = audit[~audit["holds"].astype(bool)]
        if len(failed):
            self._log("LEAKAGE AUDIT FAILED: " + "; ".join(failed["check"]))
        else:
            self._log(f"Leakage audit passed ({len(audit)} checks). "
                      f"{len(self._run.predictions)} held-out prediction rows.")
        self.export_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # State

    def _reset_results(self) -> None:
        self.tabs.clear()
        self._figures = {}
        self._datasets = {}
        self._fit = None
        self._scored = None
        self._run = None
        for name in ("_two_stage", "_prior_bounds", "_channel_test", "_identifiability",
                     "_cross_hand", "_per_participant", "_contrast", "_bootstrap",
                     "_ladder", "_ablations", "_rt_table", "_rt_components", "_rt_split",
                     "_rt_interaction", "_rt_entropy", "_rt_link", "_rt_observed",
                     "_rt_residuals", "_accounting"):
            if hasattr(self, name):
                delattr(self, name)
        self.predict_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.export_summary_btn.setEnabled(False)

    def _reset(self) -> None:
        self._reset_results()
        self._events = None
        self._log_lines = []
        self.log_view.clear()
        self.header_label.setText("No model fitted yet.")
        self.status_label.setText(
            "Reset. Tick participants, choose a model, then press “Fit model”.")

    # ------------------------------------------------------------------
    # Tab assembly
    #
    # _tab_builders() IS the analysis: _rebuild_tabs walks it and the
    # progress dialog sizes itself from it, so adding a tab is one line
    # here and nothing else. Prediction tabs appear only once a prediction
    # run exists, rather than showing empty panels that look like a result.

    def _tab_builders(self):
        """Every tab in display order, as (title, builder, advanced).

        This list IS the analysis: _rebuild_tabs walks it, and everything
        on it is built and registered for export whatever the Advanced
        toggle says. The flag decides only what is put on screen - an
        export that changed depending on which checkbox was ticked would
        be a trap, and the toggle's own tooltip promises it does not.
        """
        has_run = self._run is not None
        builders = [("Model Summary", self._build_summary, False)]
        if has_run:
            builders += [
                ("Out-of-Sample Summary", self._build_prediction_summary, False),
                ("Error Risk Prediction", self._build_error_risk, False),
                ("RT Prediction", self._build_rt_prediction, False),
            ]
        builders += [
            ("Error Structure", self._build_error_structure, False),
            ("Habitual Fingering", self._build_prior, False),
            ("Selection vs Execution", self._build_selection_execution, False),
        ]
        if has_run:
            builders.append(("Event Viewer", self._build_event_viewer, False))
        builders += [
            ("Model Status", self._build_status, True),
            ("Parameters", self._build_parameters, True),
            ("Identifiability", self._build_identifiability, True),
            ("Model Comparison", self._build_comparison, True),
            ("Participant Parameters", self._build_participant_parameters, True),
            ("RT Decomposition", self._build_rt_decomposition, True),
        ]
        if has_run:
            builders += [
                ("Finger Choice Prediction", self._build_choice_prediction, True),
                ("Calibration", self._build_calibration, True),
                ("Population vs Personalised", self._build_population_personalised, True),
                ("Prediction by Condition", self._build_prediction_by_condition, True),
                ("Prediction by Finger", self._build_prediction_by_finger, True),
                ("Prediction Diagnostics", self._build_prediction_diagnostics, True),
            ]
        return builders

    def _rebuild_tabs(self) -> None:
        self.tabs.clear()
        self._figures = {}
        self._datasets = {}
        show_advanced = self.advanced_box.isChecked()
        for title, build, advanced in self._tab_builders():
            result = build()
            if result is None:
                continue
            # Registered either way; shown only if it belongs on screen.
            self._add_tab(title, *result, visible=show_advanced or not advanced)
        self._update_header()

    def _update_header(self) -> None:
        if self._fit is None:
            return
        fit = self._fit
        self.header_label.setText(
            f"{fit.spec.name} — {len(fit.participants)} participants, "
            f"{fit.n_events} events, {fit.n_params} parameters")
        parts = [f"Log-likelihood {fit.loglik:.1f} ({fit.loglik / fit.n_events:.4f} per "
                 f"event; chance is {np.log(1 / em.N_FINGERS):.4f}). AIC {fit.aic:.1f}."]
        if self._run is not None:
            summary = ep.prediction_summary(self._run)
            best = summary.iloc[0]
            parts.append(f"Held-out prediction: best model is <b>{best['model_name']}</b> "
                         f"at log loss {best['log_loss']:.4f}.")
        parts.append(self._identifiability_banner())
        self.status_label.setText(" ".join(parts))

    def _identifiability_banner(self) -> str:
        table = getattr(self, "_identifiability", None)
        if table is None or table.empty:
            return ""
        bounded = table[table["verdict"] != "estimated"]
        if bounded.empty:
            return ("<b style='color:#2e7d32'>Identifiability:</b> every cue parameter on "
                    "screen is supported by enough informative events to be read as an "
                    "estimate.")
        counts = table.set_index("parameter")["n_identifying"]
        detail = ("; ".join(f"{row.parameter} is a {row.verdict} "
                            f"({row.n_identifying} informative event"
                            f"{'s' if row.n_identifying != 1 else ''})"
                            for row in bounded.itertuples()) + ".")
        return _IDENTIFIABILITY_BANNER.format(
            detail=detail,
            b_events=int(counts.get("match_hand[B]", 0)),
            c_events=int(counts.get("match_hand[C]", 0)))

    def _add_tab(self, title: str, caption_html: str,
                 figures: Dict[str, Figure],
                 datasets: Optional[Dict[str, object]] = None,
                 widgets: Optional[List[QWidget]] = None,
                 visible: bool = True) -> None:
        """Render one tab AND register its figures and tables for export.

        Registration lives here rather than in each builder so a tab
        cannot ship without its output - the same rule the group window
        enforces."""
        self._figures.update(figures)
        for slug, table in (datasets or {}).items():
            if table is not None and len(table):
                self._datasets[slug] = table
        if not visible:
            return

        content = QWidget()
        layout = QVBoxLayout(content)
        caption = QLabel(caption_html)
        caption.setWordWrap(True)
        caption.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(caption)
        for widget in (widgets or []):
            layout.addWidget(widget)
        for figure in figures.values():
            canvas = ScrollFriendlyCanvas(figure)
            canvas.setFixedHeight(int(figure.get_figheight() * 100))
            canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(canvas)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, title)

    @staticmethod
    def _table_widget(frame: pd.DataFrame, max_rows: int = 400,
                      decimals: int = 4) -> QTableWidget:
        """A DataFrame as a read-only grid.

        Numbers are rendered to a fixed number of decimals so columns line
        up; the exported CSV keeps full precision."""
        shown = frame.head(max_rows)
        table = QTableWidget(len(shown), len(shown.columns))
        table.setHorizontalHeaderLabels([str(c) for c in shown.columns])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        for r, (_, row) in enumerate(shown.iterrows()):
            for c, value in enumerate(row):
                if isinstance(value, float) and np.isfinite(value):
                    text = f"{value:.{decimals}f}"
                elif value is None or (isinstance(value, float) and not np.isfinite(value)):
                    text = "n/a"
                else:
                    text = str(value)
                table.setItem(r, c, QTableWidgetItem(text))
        table.resizeColumnsToContents()
        height = min(28 * (len(shown) + 1) + 26, 460)
        table.setMinimumHeight(height)
        table.setMaximumHeight(height)
        return table

    # ------------------------------------------------------------------
    # The summary
    #
    # One figure that answers, without any statistics: what the model
    # does, what it found, and what it can predict. Everything on it is
    # in counts and odds - no log-likelihood, no parameter names, no
    # coefficient anybody has to exponentiate in their head.

    def _build_summary(self):
        headline = em.headline_numbers(self._fit, self._events)
        concentration = within_hand = {}
        if self._run is not None:
            concentration = ep.risk_concentration(
                self._run.predictions, model_name=self._run.primary_model)
            # The class the model canNOT predict, shown beside the one it
            # can: a panel that reported only the success would be
            # advertising, not a result.
            within_hand = ep.risk_concentration(
                self._run.predictions, outcome=ep.OUTCOME_WITHIN_HAND,
                column="p_within_hand", model_name=self._run.primary_model)
        figure = self._plot_summary(headline, concentration, within_hand)

        hand_b = headline.get("hand advantage|B|odds")
        hand_c = headline.get("hand advantage|C|odds")
        digit_b = headline.get("digit sharpness|B|odds")
        digit_c = headline.get("digit sharpness|C|odds")
        lines = ["<h3>What the model does, what it found, and what it predicts</h3>",
                 "<p>Every keypress in this study is a choice of one finger out of ten. "
                 "The model treats it that way: three things push on the ten candidates — "
                 "what this person tends to do anyway, what the hand can physically reach, "
                 "and what the cue says — and the finger with the most support acts. The "
                 "cue is allowed to say two separate things: <b>which hand</b> and "
                 "<b>which finger of that hand</b>.</p>"]
        if hand_b and hand_c and digit_b and digit_c:
            lines.append(
                f"<p><b>The finding.</b> The two cues are about equally clear on "
                f"<i>which finger</i> of a hand to use. They are not equally clear on "
                f"<i>which hand</i>: that is where almost the whole benefit of putting the "
                f"cue on the body sits. The plainest form of it needs no model at all — "
                f"{headline.get('cross_hand_events|B', 0)} actions used the wrong hand "
                f"under the screen cue against "
                f"{headline.get('cross_hand_events|C', 0)} under the finger cue, out of "
                f"5,400 events each.</p>"
                "<p>That is also where the errors come from. A picture says “second "
                "finger” in a way that fits the left hand as well as the right, so when the "
                "habit pulls towards the other hand the mistake that comes out is the "
                "matching finger on the wrong hand. A buzz on the finger cannot make that "
                "mistake, because which hand it is on is not something it has to say.</p>"
                "<p style='color:#666666'>The panel deliberately shows a direction and the "
                "raw counts rather than a ratio. Under the finger cue the wrong hand was "
                "used twice in 5,400 events, which fixes the strength of that channel only "
                "from below — a “so many times stronger” figure computed from it would "
                "read as a measurement when it is a floor. The size is in the Parameters "
                "tab, labelled as the bound it is.</p>")
        if concentration:
            lines.append(
                f"<p><b>And it predicts them.</b> Held out one participant at a time — the "
                f"model never saw the person it was scoring — "
                f"{concentration['n_errors_in_top']} of "
                f"{concentration['n_errors']} of those wrong-hand mistakes happened in the "
                f"{int(concentration['top_fraction'] * 100)}% of events it had flagged as "
                f"riskiest, before the key was pressed. If the flag meant nothing, about "
                f"{concentration['expected_if_uninformative']:.0f} would have.</p>")
            if within_hand:
                lines.append(
                    f"<p><b>And it fails on the other kind.</b> The same warning finds only "
                    f"{within_hand['n_errors_in_top']} of {within_hand['n_errors']} "
                    f"within-hand slips in that same riskiest slice — below the "
                    f"{within_hand['expected_if_uninformative']:.0f} that picking at random "
                    f"would give. Which digit slips to its neighbour is not something this "
                    f"model can anticipate, and the panel says so rather than reporting "
                    f"only the half that worked. That fits the mechanism: a slip to the "
                    f"next finger of the correct hand is a matter of execution and of "
                    f"where the camera drew the line, not of choosing the wrong "
                    f"effector.</p>")
        else:
            lines.append("<p><b>Prediction</b> has not been run yet — press “Run "
                         "out-of-sample prediction” to fill in the third panel.</p>")
        if headline.get("hand_is_bound|C"):
            lines.append(
                f"<p style='color:#7a5c00'><b>One honest caveat.</b> Under vibration only "
                f"{headline.get('cross_hand_events|C', 0)} actions in 5,400 used the wrong "
                f"hand at all, against {headline.get('cross_hand_events|B', 0)} on the "
                f"screen. With so few, the vibration figure is a floor rather than a "
                f"measurement: the true separation is at least that large and cannot be "
                f"pinned down more precisely from this data. The comparison between the "
                f"two cues is safe; the exact size of the haptic number is not.</p>")
        lines.append("<p style='color:#666666'>Tick <i>Show advanced diagnostics</i> for "
                     "the parameter estimates, the model comparison against simpler "
                     "alternatives, the identifiability audit and the per-fold "
                     "cross-validation behind all of this.</p>")
        return ("".join(lines), {"model_summary": figure},
                {"model_headline": pd.DataFrame([headline]),
                 "model_risk_concentration": (pd.DataFrame([concentration])
                                              if concentration else None)}, [])

    def _plot_summary(self, headline, concentration, within_hand=None) -> Figure:
        figure = Figure(figsize=(13.0, 5.2))
        axes = figure.subplots(1, 3, width_ratios=[1.15, 1.0, 1.05])
        self._panel_mechanism(axes[0])
        self._panel_channels(axes[1], headline)
        self._panel_prediction(axes[2], concentration, within_hand)
        figure.tight_layout(pad=1.6, w_pad=2.6, rect=(0, 0.09, 1, 1))
        return figure

    @staticmethod
    def _panel_mechanism(ax) -> None:
        """A) What the model does. A diagram, not a chart."""
        from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.axis("off")
        ax.set_title("A.  How the model chooses a finger", fontsize=11, loc="left",
                     fontweight="bold", pad=12)

        hub = (5.75, 5.4)
        boxes = [
            (8.7, "what this person\nusually does", "#8fa8c8"),
            (7.0, "what the hand\ncan reach", "#8fa8c8"),
            (4.4, "the cue says\nWHICH HAND", "#d98f3d"),
            (2.7, "the cue says\nWHICH FINGER", "#d98f3d"),
        ]
        for y, text, colour in boxes:
            ax.add_patch(FancyBboxPatch((0.15, y - 0.62), 3.75, 1.24,
                                        boxstyle="round,pad=0.06", linewidth=0,
                                        facecolor=colour, alpha=0.9))
            ax.text(2.02, y, text, ha="center", va="center", fontsize=7.6,
                    color="white", fontweight="bold", linespacing=1.35)
            ax.add_patch(FancyArrowPatch((4.0, y), (hub[0] - 0.85, hub[1]),
                                         arrowstyle="-|>", mutation_scale=10,
                                         linewidth=1.0, color="#9aa6b2", alpha=0.85,
                                         connectionstyle="arc3,rad=0.1"))

        ax.add_patch(FancyBboxPatch((hub[0] - 0.82, 4.0), 1.7, 2.9,
                                    boxstyle="round,pad=0.08", linewidth=1.2,
                                    edgecolor="#3a4a5a", facecolor="#eef2f6"))
        ax.text(hub[0] + 0.03, hub[1], "the ten\nfingers\ncompete", ha="center",
                va="center", fontsize=8.6, color="#26313d", fontweight="bold",
                linespacing=1.35)
        ax.add_patch(FancyArrowPatch((hub[0] + 0.92, hub[1]), (7.55, hub[1]),
                                     arrowstyle="-|>", mutation_scale=12,
                                     linewidth=1.3, color="#3a4a5a"))

        # A miniature probability profile: the point is the SHAPE - one
        # finger clearly ahead, a neighbour close behind - not the values.
        heights = np.array([0.05, 0.08, 0.55, 1.0, 0.22, 0.10, 0.06, 0.04, 0.03, 0.02])
        x = np.linspace(7.85, 9.75, len(heights))
        colours = ["#c4453a" if h == heights.max() else "#a8b8c8" for h in heights]
        ax.bar(x, heights * 2.3, width=0.15, bottom=4.35, color=colours)
        ax.text(8.8, 7.15, "one finger\nacts", ha="center", fontsize=8.4,
                color="#26313d", fontweight="bold", linespacing=1.35)
        ax.text(8.8, 3.75, "chance of each\nof the ten fingers", ha="center", va="top",
                fontsize=7.4, color="#777777", linespacing=1.3)
        ax.text(0.15, 1.15, "Nothing in the model mentions “wrong hand”. It knows only\n"
                            "whether a candidate matches the cued hand, and whether it\n"
                            "matches the cued finger — the mistakes follow from that.",
                fontsize=7.6, color="#666666", style="italic", va="top", linespacing=1.5)

    def _panel_channels(self, ax, headline) -> None:
        """B) What the haptic cue actually strengthens.

        Deliberately NOT an odds ratio. The hand-evidence parameter under
        the haptic cue is identified by two cross-hand actions in 5,400,
        so it is a lower bound and any multiple computed from it - "39
        times stronger" - would read as a measurement when it is a floor.
        What this panel shows instead is the direction, which is solid,
        and the raw counts behind it, which are not model quantities at
        all and can be checked by counting.
        """
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.axis("off")
        ax.set_title("B.  What the haptic cue strengthens", fontsize=11, loc="left",
                     fontweight="bold", pad=12)

        rows = [
            (8.3, "WHICH HAND acts", "Haptic  \u226b  Visual", "#b03a2e", "#fbeae8"),
            (5.7, "WHICH FINGER of that hand", "about the same", "#3a6a44", "#e9f2eb"),
        ]
        from matplotlib.patches import FancyBboxPatch

        # Stacked, not side by side. Putting the label and the verdict on
        # one line means the two texts have to be kept apart by guessing
        # their rendered widths, and the panel is narrow enough that any
        # font substitution slides them into each other. One above the
        # other cannot collide however wide either turns out to be.
        for y, channel, verdict, colour, fill in rows:
            ax.add_patch(FancyBboxPatch((0.2, y - 1.15), 9.6, 2.3,
                                        boxstyle="round,pad=0.06", linewidth=1.0,
                                        edgecolor=colour, facecolor=fill, alpha=0.95))
            ax.text(5.0, y + 0.62, channel, ha="center", va="center", fontsize=8.8,
                    color="#26313d", fontweight="bold")
            ax.text(5.0, y - 0.48, verdict, ha="center", va="center", fontsize=13,
                    color=colour, fontweight="bold")

        counts = (headline.get("cross_hand_events|B"), headline.get("cross_hand_events|C"))
        if all(c is not None for c in counts):
            ax.text(5.0, 3.75, "Actions that used the wrong hand, out of 5,400 each",
                    ha="center", va="center", fontsize=8.6, color="#3a4a5a")
            for x_position, count, condition, caption in (
                    (3.0, counts[0], "B", "screen cue"),
                    (7.0, counts[1], "C", "finger cue")):
                ax.text(x_position, 2.45, f"{count}", ha="center", va="center",
                        fontsize=26, fontweight="bold", color=CONDITION_COLORS[condition])
                ax.text(x_position, 1.35, caption, ha="center", va="center",
                        fontsize=8.4, color="#5a6a7a")
            ax.text(5.0, 2.45, "vs", ha="center", va="center", fontsize=11,
                    color="#8a96a2")

        ax.text(5.0, 0.25, "Haptic mainly strengthens hand identity.",
                ha="center", va="center", fontsize=11.5, fontweight="bold",
                color="#26313d")

    @staticmethod
    def _panel_prediction(ax, concentration, within_hand=None) -> None:
        """C) What it can predict, on people it never saw - and what it cannot."""
        ax.set_title("C.  Predicting mistakes before they happen", fontsize=11,
                     loc="left", fontweight="bold", pad=12)
        if not concentration:
            ax.axis("off")
            ax.text(0.5, 0.55, "Run the out-of-sample prediction\nto fill in this panel",
                    ha="center", va="center", fontsize=10, color="#999999",
                    linespacing=1.5)
            return

        classes = [("wrong HAND", concentration, "#c4453a")]
        if within_hand:
            classes.append(("wrong FINGER", within_hand, "#b8c2cc"))
        share_of_events = concentration["top_fraction"]

        x = np.arange(len(classes))
        shares = [100.0 * c["share_captured"] for _, c, _ in classes]
        ax.bar(x, shares, width=0.5, color=[colour for _, _, colour in classes], zorder=3)
        ax.axhline(100.0 * share_of_events, color="#3a4a5a", linestyle="--",
                   linewidth=1.3, zorder=2)
        ax.annotate(f"chance: {share_of_events * 100:.0f}%",
                    (len(classes) - 0.36, 100.0 * share_of_events), xytext=(0, 4),
                    textcoords="offset points", ha="right", va="bottom", fontsize=7.8,
                    color="#3a4a5a")
        for position, (_, entry, _) in enumerate(classes):
            ax.annotate(f"{entry['n_errors_in_top']} of {entry['n_errors']}",
                        (position, 100.0 * entry["share_captured"]), xytext=(0, 9),
                        textcoords="offset points", ha="center", fontsize=11,
                        fontweight="bold", color="#26313d")
        ax.set_xticks(x, [label for label, _, _ in classes], fontsize=10)
        ax.set_xlim(-0.78, len(classes) - 0.22)
        ax.set_ylim(0, 122)
        ax.set_ylabel(f"share caught in the {share_of_events * 100:.0f}% of events\n"
                      f"the model flagged riskiest", fontsize=8.6)
        ax.grid(axis="y", alpha=0.25, zorder=0)
        ax.set_axisbelow(True)
        ax.text(0.5, -0.24, "The model predicts wrong-hand risk,\nbut not within-hand slips.",
                transform=ax.transAxes, ha="center", va="top", fontsize=10.5,
                fontweight="bold", color="#26313d", linespacing=1.4)
        ax.text(0.5, -0.45, "Held out one participant at a time: the model had never seen "
                            "the person it was scoring,\nand the warning is issued before "
                            "the key is pressed.",
                transform=ax.transAxes, ha="center", fontsize=7.6, color="#666666",
                style="italic", va="top", linespacing=1.5)

    # ------------------------------------------------------------------
    # Model tabs

    def _build_status(self):
        fit = self._fit
        summary = em.fit_summary(fit, self._events)
        lines = [f"<h3>{fit.spec.name}</h3>",
                 "<p>Every keypress is modelled as a choice among the ten fingers: a "
                 "habitual prior and a reachability term set what the hand would do "
                 "anyway, the cue adds evidence for the named hand and the named digit, "
                 "and a softmax turns the total into a probability for each finger. "
                 "Condition A has no cue term at all, which is what withholding the "
                 "finger cue means in the model rather than a separate model for A.</p>",
                 "<p><b>Where the events went</b> — the modelled count reconciles with "
                 "the export by inspection, so nothing is dropped silently.</p>"]
        widgets = [self._table_widget(self._accounting, decimals=0),
                   self._table_widget(summary, decimals=4)]
        if hasattr(self, "_prior_bounds"):
            lines.append("<p><b>Does naming a finger abolish the standing preference?</b> "
                         "Both extremes are refitted with the prior weight pinned, so the "
                         "answer is a difference in log-likelihood rather than a claim "
                         "about an interval clearing a threshold.</p>")
            widgets.append(self._table_widget(self._prior_bounds, decimals=3))
        return ("".join(lines), {}, {"model_fit_summary": summary,
                                     "model_event_accounting": self._accounting,
                                     "model_prior_weight_bounds":
                                         getattr(self, "_prior_bounds", None)}, widgets)

    def _build_parameters(self):
        fit = self._fit
        coefficients = fit.coefficients()
        interpretable = coefficients[coefficients["block"] != "habitual prior"]
        contrasts = em.evidence_contrasts(fit)
        figures = {"model_cue_parameters": self._plot_cue_parameters(contrasts)}

        lines = ["<h3>Fitted parameters</h3>",
                 "<p>All values are log-odds. A cue parameter says how much evidence that "
                 "cue supplied for the named property of the effector; the prior weight "
                 "says how much of the free-choice habit still acts once a finger is "
                 "named (1 = untouched, 0 = abolished).</p>",
                 "<p><b>Report the contrasts, not the raw coefficients.</b> Writing each "
                 "candidate's utility relative to the far cross-hand alternative, the cued "
                 "finger gets <i>hand + digit</i>, the homologous finger on the other hand "
                 "gets <i>digit</i> alone, and the neighbouring digit on the same hand gets "
                 "<i>hand − gradient</i>. So the two identified quantities are the "
                 "<b>hand advantage</b> (cued vs homologous, identified by cross-hand "
                 "actions) and the <b>digit sharpness</b> (cued vs same-hand neighbour, "
                 "identified by within-hand actions). The raw digit coefficient on its own "
                 "is separated from the gradient only by cross-hand actions, of which "
                 "Condition C has one.</p>"]
        widgets = [self._table_widget(contrasts, decimals=3),
                   self._table_widget(interpretable, decimals=4)]
        datasets = {"model_coefficients": coefficients,
                    "model_evidence_contrasts": contrasts}
        if hasattr(self, "_bootstrap"):
            lines.append(
                f"<p><b>Intervals</b> from {int(self._bootstrap['n_resamples'].iloc[0])} "
                "bootstrap resamples of the PARTICIPANTS, not of the events: events within "
                "one person are not independent and an event-level resample would give "
                "intervals several times too narrow. The <i>reading</i> column marks any "
                "parameter whose interval is built partly from resamples that contained no "
                "event able to identify it — for those the upper end is a bound.</p>")
            widgets.append(self._table_widget(self._bootstrap, decimals=4))
            datasets["model_bootstrap"] = self._bootstrap
        if hasattr(self, "_two_stage"):
            two_stage = self._two_stage.coefficients()
            lines.append("<p><b>Two-stage fit.</b> The prior is estimated on Condition A "
                         "alone and then frozen, so the weight below rescales a habit the "
                         "cued trials had no hand in shaping. The joint fit above is more "
                         "efficient; the two agree on sign and ordering and differ in "
                         "magnitude, the joint prior having already been pulled towards "
                         "the cued trials.</p>")
            widgets.append(self._table_widget(two_stage, decimals=4))
            datasets["model_two_stage_coefficients"] = two_stage
        return ("".join(lines), figures, datasets, widgets)

    def _plot_cue_parameters(self, contrasts: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 4.0))
        axes = figure.subplots(1, 2)
        bootstrap = getattr(self, "_bootstrap", None)
        for ax, contrast in zip(axes, ["hand advantage", "digit sharpness"]):
            subset = contrasts[contrasts["contrast"] == contrast].set_index("condition")
            conditions = [c for c in em.CUED_CONDITIONS if c in subset.index]
            values = [subset.loc[c, "log_odds"] for c in conditions]
            ax.bar(range(len(conditions)), values,
                   color=[CONDITION_COLORS[c] for c in conditions], width=0.55)
            if bootstrap is not None and contrast == "hand advantage":
                for i, condition in enumerate(conditions):
                    row = bootstrap[bootstrap["parameter"] == f"match_hand[{condition}]"]
                    if len(row):
                        row = row.iloc[0]
                        ax.errorbar([i], [row["estimate"]],
                                    yerr=[[row["estimate"] - row["ci95_lo"]],
                                          [row["ci95_hi"] - row["estimate"]]],
                                    color="black", capsize=5, linewidth=1.3)
                        if row["n_resamples_unidentified"] > 0:
                            ax.annotate("bound", (i, row["ci95_hi"]),
                                        textcoords="offset points", xytext=(0, 6),
                                        ha="center", fontsize=8, color="#b03a2e")
            ax.set_xticks(range(len(conditions)),
                          [f"{c}\n{'visual' if c == 'B' else 'haptic'}" for c in conditions])
            ax.set_ylabel("log-odds")
            ax.set_ylim(bottom=0)
            ax.set_title(contrast, fontsize=10)
        axes[0].set_title("Hand advantage\n(cued vs homologous, other hand)", fontsize=10)
        axes[1].set_title("Digit sharpness\n(cued vs adjacent digit, same hand)", fontsize=10)
        figure.suptitle("Evidence each cue supplied, by channel", fontsize=11)
        figure.tight_layout()
        return figure

    def _build_identifiability(self):
        table = getattr(self, "_identifiability", None)
        if table is None:
            return None
        lines = ["<h3>What these data can and cannot pin down</h3>",
                 "<p>A coefficient is only as determined as the events that distinguish "
                 "it. Hand evidence is identified by <b>cross-hand actions</b>: if a "
                 "condition produced none, the likelihood rises without a maximum and only "
                 "the stated bound stops it; if it produced two, the maximum exists but "
                 "the upper tail is nearly flat.</p>",
                 f"<p>{self._identifiability_banner()}</p>",
                 "<p>Recovery simulations on synthetic data with a known value "
                 "(test-script/test_effector_model.py) recover it accurately while "
                 "cross-hand actions stay in the handful, and scatter from 6.9 to 10.7 for "
                 "the same true value of 8 once they fall to zero or one. That is why the "
                 "primary test of the channel claim is the likelihood ratio on the "
                 "Model Comparison tab, which compares models rather than parameter "
                 "values and stays valid when the maximum does not.</p>"]
        widgets = [self._table_widget(table, decimals=0)]
        datasets = {"model_identifiability": table}
        figures = {}
        if hasattr(self, "_cross_hand"):
            widgets.append(self._table_widget(
                self._cross_hand.pivot(index="participant", columns="condition",
                                       values="n_cross_hand").reset_index(), decimals=0))
            datasets["model_cross_hand_rates"] = self._cross_hand
            figures["model_cross_hand"] = self._plot_cross_hand(self._cross_hand)
            lines.insert(3, "<p>The raw observable behind the whole channel claim, per "
                            "participant — countable without any model.</p>")
        return ("".join(lines), figures, datasets, widgets)

    def _plot_cross_hand(self, cross_hand: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.6))
        ax = figure.subplots()
        pivot = cross_hand.pivot(index="participant", columns="condition",
                                 values="n_cross_hand").fillna(0)
        x = np.arange(len(pivot))
        width = 0.38
        for offset, condition in zip((-width / 2, width / 2), em.CUED_CONDITIONS):
            if condition in pivot.columns:
                ax.bar(x + offset, pivot[condition], width,
                       label=f"{condition} ({'visual' if condition == 'B' else 'haptic'})",
                       color=CONDITION_COLORS[condition])
        ax.set_xticks(x, pivot.index, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("cross-hand actions")
        ax.set_ylim(bottom=0)
        ax.legend(fontsize=8)
        ax.set_title("Actions using the hand that was not cued — the events that identify "
                     "hand evidence", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_comparison(self):
        lines = ["<h3>Model comparison</h3>"]
        widgets, datasets, figures = [], {}, {}
        if hasattr(self, "_channel_test"):
            lines.append(
                "<p><b>The primary test of the channel claim.</b> Each row refits the "
                "whole model with one channel's B and C coefficients tied together and "
                "compares against the free fit. This asks whether the channels differ, "
                "which is the claim, and unlike a test on the parameter values it does "
                "not need the size of the difference to be identified.</p>")
            widgets.append(self._table_widget(self._channel_test, decimals=3))
            datasets["model_channel_test"] = self._channel_test
            figures["model_channel_test"] = self._plot_channel_test(self._channel_test)
        if hasattr(self, "_ladder"):
            lines.append(
                "<p><b>The ladder.</b> Each step is a hypothesis about what the cue "
                "supplies — nothing (M0), one undifferentiated “this finger” signal (M1), "
                "hand and digit identity separably (M2), a graded digit distance (M3), "
                "and strength varying by cued digit (M4). Cross-validated scores are "
                "leave-one-participant-out.</p>"
                "<p><b>Read the log-likelihood column, not the accuracy column.</b> "
                "Top-1 accuracy saturates: once any cue term is present the cued finger "
                "is usually first, so several structurally different models score "
                "identically. Log-likelihood scores the whole ten-way distribution, "
                "including where the mass for the errors went, and that is where the "
                "models differ.</p>")
            widgets.append(self._table_widget(self._ladder, decimals=4))
            datasets["model_ladder"] = self._ladder
        if hasattr(self, "_ablations"):
            lines.append(
                "<p><b>Ablations.</b> Each row removes one NON-cue component, to show the "
                "cue parameters are not quietly absorbing habit, biomechanics or movement "
                "cost.</p>")
            widgets.append(self._table_widget(self._ablations, decimals=4))
            datasets["model_ablations"] = self._ablations
        return ("".join(lines), figures, datasets, widgets)

    def _plot_channel_test(self, test: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(8.0, 3.6))
        ax = figure.subplots()
        labels = {"match_hand": "hand identity", "match_digit": "digit identity",
                  "digit_distance": "digit gradient"}
        rows = test.set_index("channel")
        channels = [c for c in ("match_hand", "match_digit", "digit_distance")
                    if c in rows.index]
        values = [rows.loc[c, "delta_loglik"] for c in channels]
        colors = ["#c4453a" if v == max(values) else "#7a8898" for v in values]
        ax.barh(range(len(channels)), values, color=colors, height=0.5)
        for i, value in enumerate(values):
            ax.annotate(f"{value:.1f}", (value, i), xytext=(4, 0),
                        textcoords="offset points", va="center", fontsize=9)
        ax.set_yticks(range(len(channels)), [labels[c] for c in channels])
        ax.invert_yaxis()
        ax.set_xlabel("log-likelihood lost when B and C are forced to supply the same "
                      "evidence on this channel")
        ax.set_xlim(left=0)
        ax.set_title("Which channel carries the difference between the two cues?", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_error_structure(self):
        structure = em.error_structure(self._fit, self._events)
        genuine = em.genuine_error_structure(self._events)
        figures = {"model_error_structure": self._plot_error_structure(structure)}
        for condition in em.CUED_CONDITIONS:
            observed, predicted = em.confusion_matrix(self._fit, self._events, condition)
            figures[f"model_confusion_{condition}"] = self._plot_confusion(
                observed, predicted, condition)
        lines = ["<h3>Error structure — predicted, not fitted</h3>",
                 "<p>No feature in the design matrix mentions “cross-hand” or "
                 "“homologous”. The model knows only whether a candidate matches the cued "
                 "hand and whether it matches the cued digit; the classes below are read "
                 "off the fitted distribution by summing probability mass. Agreement with "
                 "the observed counts is therefore a test of the factorisation, not a "
                 "quantity that was fitted to.</p>",
                 "<p>Digit evidence carries across the body midline — “digit 2” fits L2 as "
                 "well as R2 — while hand evidence does not. When the hand advantage is "
                 "not large enough to overcome the habitual prior, the best remaining "
                 "competitor is the same digit on the wrong hand. That is where the "
                 "homologous error comes from.</p>",
                 "<p>Observed counts use <i>actual_finger</i>, which is what the model "
                 "predicts. The θ = 0.40 rule forgives some adjacent-digit events as "
                 "detector near-ties and so counts fewer substitutions; that view is the "
                 "second table, for reconciliation with the Group Analysis confusion "
                 "tab.</p>"]
        widgets = [self._table_widget(structure, decimals=1),
                   self._table_widget(genuine, decimals=0)]
        return ("".join(lines), figures,
                {"model_error_structure": structure,
                 "model_genuine_error_structure": genuine}, widgets)

    def _plot_error_structure(self, structure: pd.DataFrame) -> Figure:
        """Observed against model-expected substitutions, one panel per
        error class so each keeps its own scale.

        Sharing one axis would defeat the figure: within-hand slips are
        five times more common than wrong-hand actions under the screen
        cue and a hundred and sixty times more common under the finger
        cue, so on a common scale the class the whole argument rests on
        is two invisible bars. Each class therefore gets its own panel.

        Condition A is not here at all. It produced 4,431 substitutions
        against 426 and 323, because under free choice almost every event
        counts as a departure from a fingering nobody was shown - a
        different task, not a worse one. Its numbers stay in the table
        below the figure.
        """
        figure = Figure(figsize=(10.5, 4.3))
        axes = figure.subplots(1, 3)
        rows = structure.set_index("condition")
        classes = [("cross_hand", "Wrong HAND"),
                   ("homologous", "Wrong hand,\nsame finger number"),
                   ("within_hand", "Wrong finger,\nsame hand")]
        conditions = [c for c in em.CUED_CONDITIONS if c in rows.index]

        for ax, (key, title) in zip(axes, classes):
            x = np.arange(len(conditions))
            observed = [rows.loc[c, f"observed_{key}"] for c in conditions]
            predicted = [rows.loc[c, f"predicted_{key}"] for c in conditions]
            ax.bar(x, observed, 0.52, color=[CONDITION_COLORS[c] for c in conditions],
                   zorder=3, label="observed")
            # The prediction as a rule laid across the bar: "the model said
            # it would come to here" is read faster from one mark than from
            # a second bar to compare against.
            for position, value in enumerate(predicted):
                ax.hlines(value, position - 0.33, position + 0.33, color="#26313d",
                          linewidth=2.2, zorder=5)
            for position, (obs, pred) in enumerate(zip(observed, predicted)):
                ax.annotate(f"{obs:.0f} observed\n{pred:.0f} predicted",
                            (position, max(obs, pred)), xytext=(0, 6),
                            textcoords="offset points", ha="center", fontsize=8.4,
                            linespacing=1.4, color="#26313d")
            ax.set_xticks(x, [f"{c}\n{'screen cue' if c == 'B' else 'finger cue'}"
                              for c in conditions], fontsize=9)
            ax.set_xlim(-0.62, len(conditions) - 0.38)
            ax.set_ylim(0, max(max(observed), max(predicted)) * 1.45)
            ax.grid(axis="y", alpha=0.25, zorder=0)
            ax.set_axisbelow(True)
            ax.set_title(title, fontsize=10, fontweight="bold", color="#26313d")
        axes[0].set_ylabel("actions", fontsize=9)
        axes[0].hlines([], [], [], color="#26313d", linewidth=2.2,
                       label="model prediction")
        axes[0].legend(fontsize=8.2, frameon=False, loc="upper right")

        figure.suptitle("Observed vs model-predicted error structure", fontsize=12.5,
                        fontweight="bold", x=0.012, ha="left", y=0.985)
        figure.text(0.012, 0.015,
                    "None of these classes is fitted. The model is told only whether a "
                    "candidate finger matches the cued hand and whether it matches the cued "
                    "digit; “wrong hand” and “same finger\nnumber” are read off the "
                    "resulting distribution, so the agreement between the bar and the rule "
                    "is a prediction, not a quantity the fit was asked to reproduce.",
                    fontsize=7.8, color="#666666", style="italic", va="bottom",
                    linespacing=1.6)
        figure.subplots_adjust(left=0.075, right=0.985, top=0.80, bottom=0.24, wspace=0.28)
        return figure

    def _plot_confusion(self, observed: pd.DataFrame, predicted: pd.DataFrame,
                        condition: str) -> Figure:
        figure = Figure(figsize=(9.5, 4.4))
        axes = figure.subplots(1, 2)
        for ax, table, title in ((axes[0], observed, "observed"),
                                 (axes[1], predicted, "model-expected")):
            # Off-diagonal detail is the point, and the diagonal is two
            # orders of magnitude larger, so the colour scale is set from
            # the off-diagonal cells and the diagonal simply saturates.
            values = table.to_numpy(float)
            off_diagonal = values.copy()
            np.fill_diagonal(off_diagonal, 0.0)
            top = max(off_diagonal.max(), 1.0)
            ax.imshow(values, cmap="magma_r", vmin=0, vmax=top)
            ax.set_xticks(range(len(FINGER_ORDER)), FINGER_ORDER, fontsize=7, rotation=90)
            ax.set_yticks(range(len(FINGER_ORDER)), FINGER_ORDER, fontsize=7)
            ax.set_xlabel("finger used")
            ax.set_title(title, fontsize=10)
        axes[0].set_ylabel("finger cued")
        figure.suptitle(f"Condition {condition} — colour scaled to the off-diagonal, "
                        "so the diagonal saturates by design", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_prior(self):
        priors = self._fit.participant_priors()
        usage = em.free_choice_prior(self._events)
        figures = {"model_prior": self._plot_prior(priors, usage)}
        lines = ["<h3>The habitual prior, and how much of it survives cueing</h3>",
                 "<p>Condition A withholds the finger cue, so what participants did there "
                 "is what they do when nothing names a digit. That is not a baseline to "
                 "score the cued conditions against — free choice is a different task — "
                 "but it is the strategy a finger cue has to displace, and the model "
                 "estimates it as a per-finger prior.</p>"]
        widgets = []
        datasets = {"model_participant_priors": priors, "model_free_choice_usage": usage}
        if hasattr(self, "_prior_bounds"):
            weights = self._prior_bounds
            fitted = weights[weights["variant"] == "fitted w"]
            if len(fitted):
                row = fitted.iloc[0]
                lines.append(
                    f"<p>The fitted weight is <b>w[B] = {row['w_B']:.3f}</b> and "
                    f"<b>w[C] = {row['w_C']:.3f}</b>: roughly a third of the free-choice "
                    "habit still acts once a finger is named. Both extremes are rejected — "
                    "pinning w at 0 (the cue abolishes the habit) and at 1 (the habit is "
                    "untouched) each cost a large amount of log-likelihood, shown below. "
                    "The two conditions are close, which is the substantive point: the "
                    "haptic cue does not suppress the habit more, it supplies more hand "
                    "evidence so the same residual habit produces fewer errors.</p>")
            widgets.append(self._table_widget(self._prior_bounds, decimals=3))
        lines.append(
            "<p><b>Caveat on w.</b> Its absolute value is less robust than the hand "
            "contrast: the prior weight trades off against digit evidence, and under a "
            "strict detector-confidence filter the two conditions separate. The claim "
            "that the habit is strongly attenuated but not abolished survives; the claim "
            "that B and C attenuate it equally should be reported as an observation under "
            "the primary fit, not as a conclusion.</p>")
        return ("".join(lines), figures, datasets, widgets)

    def _plot_prior(self, priors: pd.DataFrame, usage: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 4.0))
        axes = figure.subplots(1, 2)
        spatial = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]
        observed = usage.groupby("finger")["share"].mean().reindex(spatial)
        fitted = priors.groupby("finger")["probability"].mean().reindex(spatial)
        x = np.arange(len(spatial))
        axes[0].bar(x - 0.2, observed.to_numpy(), 0.4, label="observed (Condition A)",
                    color="#3a4a5a")
        axes[0].bar(x + 0.2, fitted.to_numpy(), 0.4, label="fitted prior", color="#8fb0d0")
        axes[0].set_xticks(x, spatial, fontsize=8)
        axes[0].set_ylabel("share of presses")
        axes[0].set_ylim(bottom=0)
        axes[0].legend(fontsize=8)
        axes[0].set_title("Free-choice fingering: observed against fitted", fontsize=10)

        for participant, group in usage.groupby("participant"):
            axes[1].plot(x, group.set_index("finger")["share"].reindex(spatial).to_numpy(),
                         color="#9a9a9a", linewidth=0.8, alpha=0.6)
        axes[1].plot(x, observed.to_numpy(), color="#c4453a", linewidth=2.0, marker="o",
                     label="group mean")
        axes[1].set_xticks(x, spatial, fontsize=8)
        axes[1].set_ylabel("share of presses")
        axes[1].set_ylim(bottom=0)
        axes[1].legend(fontsize=8)
        axes[1].set_title("One line per participant", fontsize=10)
        figure.suptitle("The habitual fingering a finger cue has to displace", fontsize=11)
        figure.tight_layout()
        return figure

    def _build_participant_parameters(self):
        table = getattr(self, "_per_participant", None)
        if table is None or table.empty:
            return None
        contrast = self._contrast
        figures = {"model_participant_parameters": self._plot_participant_parameters(table)}
        lines = ["<h3>Per-participant cue parameters</h3>",
                 "<p>The cue and prior-weight parameters refitted for each participant, "
                 "with the reach table and the group prior held at the pooled estimate. "
                 "Holding reach fixed is what makes this usable: one participant supplies "
                 "about 780 events over 30 hand-by-key cells, so a personal reach table is "
                 "mostly noise and that noise leaks straight into the cue coefficients. "
                 "The ridge guard is rescaled by each participant's share of the events, "
                 "so these are comparable with the pooled values rather than shrunk "
                 "towards zero.</p>",
                 "<p><b>Secondary, not primary.</b> Most participants produced no "
                 "cross-hand action under Condition C, so their individual hand-evidence "
                 "value is a bound whose number comes from the guard rather than from "
                 "their behaviour — averaging bounds is not an estimate. This table is a "
                 "consistency check on the direction; the inferential claim belongs to the "
                 "likelihood-ratio test on the Model Comparison tab.</p>"]
        widgets = [self._table_widget(contrast, decimals=4),
                   self._table_widget(table, decimals=3)]
        return ("".join(lines), figures,
                {"model_participant_cue_parameters": table,
                 "model_paired_parameter_contrast": contrast}, widgets)

    def _plot_participant_parameters(self, table: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 4.0))
        axes = figure.subplots(1, 2)
        pairs = [("match_hand", "Hand evidence"), ("match_digit", "Digit evidence")]
        for ax, (parameter, title) in zip(axes, pairs):
            b_col, c_col = f"{parameter}[B]", f"{parameter}[C]"
            if b_col not in table.columns or c_col not in table.columns:
                continue
            for _, row in table.iterrows():
                ax.plot([0, 1], [row[b_col], row[c_col]], color="#9a9a9a",
                        linewidth=0.9, marker="o", markersize=3.5, alpha=0.8)
            ax.plot([0, 1], [table[b_col].mean(), table[c_col].mean()],
                    color="#c4453a", linewidth=2.4, marker="D", markersize=8,
                    label="mean")
            ax.set_xticks([0, 1], ["B (visual)", "C (haptic)"])
            ax.set_ylabel("log-odds")
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=8)
        figure.suptitle("Each participant's own cue parameters "
                        "(hand evidence under C is a bound for most of them)", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_selection_execution(self):
        split = getattr(self, "_rt_split", None)
        if split is None:
            return None
        figures = {"model_selection_execution": self._plot_selection_execution(split)}
        lines = ["<h3>Selection cost against execution cost, per digit</h3>",
                 "<p>Each digit's reaction time is compared with and without the "
                 "habit-override term. What the term removes is the cost of being an "
                 "unusual choice; what survives is the cost of making the movement.</p>",
                 "<p>The index finger is the clear case in one direction — almost all of "
                 "its speed advantage is selection, because it is the habitual choice, and "
                 "once that is removed it is no faster than the thumb. The ring and little "
                 "fingers are the case in the other direction: their slowness survives the "
                 "adjustment, so it is execution. This is the dissociation that lets the "
                 "cue be described as relieving selection rather than execution.</p>"]
        widgets = [self._table_widget(split, decimals=4)]
        return ("".join(lines), figures, {"model_selection_vs_execution": split}, widgets)

    def _plot_selection_execution(self, split: pd.DataFrame) -> Figure:
        """In milliseconds, because that is the unit the finding lives in.

        The adjusted bar is the model's fitted reaction time with the
        habit-override term held at its average for every event: each
        digit scored as though it had been an equally usual choice. What
        is left is the cost of making the movement.
        """
        figure = Figure(figsize=(9.6, 4.3))
        ax = figure.subplots()
        if "vs_thumb_raw_ms" not in split.columns:
            ax.axis("off")
            return figure
        x = np.arange(len(split))
        raw = split["vs_thumb_raw_ms"].to_numpy(float)
        adjusted = split["vs_thumb_adjusted_ms"].to_numpy(float)
        ax.bar(x - 0.2, raw, 0.4, label="as measured", color="#3a4a5a", zorder=3)
        ax.bar(x + 0.2, adjusted, 0.4,
               label="with every digit an equally usual choice", color="#8fb0d0", zorder=3)
        for position, (a, b) in enumerate(zip(raw, adjusted)):
            ax.annotate(f"{a:+.0f}", (position - 0.2, a),
                        xytext=(0, 4 if a >= 0 else -12), textcoords="offset points",
                        ha="center", fontsize=8.4, fontweight="bold", color="#26313d")
            ax.annotate(f"{b:+.0f}", (position + 0.2, b),
                        xytext=(0, 4 if b >= 0 else -12), textcoords="offset points",
                        ha="center", fontsize=8.4, color="#4a6a86")
        ax.axhline(0, color="#26313d", linewidth=1.0, zorder=4)
        ax.set_xticks(x, [f"{row.digit_name}" for row in split.itertuples()], fontsize=9.5)
        ax.set_ylabel("reaction time relative to the thumb (ms)", fontsize=9)
        ax.legend(fontsize=8.6, frameon=False, loc="upper left")
        ax.grid(axis="y", alpha=0.25, zorder=0)
        ax.set_axisbelow(True)
        ax.margins(y=0.28)
        ax.set_title("Which digit differences are about choosing, and which about moving",
                     fontsize=11, fontweight="bold", loc="left")

        # The two conclusions, written on the panel rather than left to be
        # read out of eight bars.
        index_row = split[split["digit"] == 2]
        little_row = split[split["digit"] == 5]
        notes = []
        if len(index_row):
            raw_ms = float(index_row["vs_thumb_raw_ms"].iloc[0])
            adjusted_ms = float(index_row["vs_thumb_adjusted_ms"].iloc[0])
            notes.append(f"The index finger's {abs(raw_ms):.0f} ms advantage is mostly "
                         f"the advantage of being the habitual choice: {abs(adjusted_ms):.0f} ms "
                         f"of it survives.")
        if len(little_row):
            adjusted_ms = float(little_row["vs_thumb_adjusted_ms"].iloc[0])
            notes.append(f"The ring and little fingers stay slow either way "
                         f"(+{adjusted_ms:.0f} ms for the little finger) — that part is "
                         f"execution, and no cue addresses it.")
        if notes:
            ax.text(0.0, -0.20, "  ".join(notes), transform=ax.transAxes, fontsize=8.6,
                    color="#26313d", va="top", wrap=True)
        figure.tight_layout(rect=(0, 0.10, 1, 1))
        return figure

    def _build_rt_decomposition(self):
        table = getattr(self, "_rt_table", None)
        if table is None:
            return None
        figures = {"model_rt_components": self._plot_rt_components(self._rt_observed)}
        lines = ["<h3>Reaction-time decomposition</h3>",
                 f"<p>{_MEASURED_NOT_DERIVED}</p>",
                 "<p>Log reaction time is modelled as a participant baseline plus three "
                 "components: the cost of overriding the habit (selection), a per-condition "
                 "constant (cue transformation), and a per-digit constant (execution), with "
                 "movement covariates. Standard errors are clustered on the participant, "
                 "because events within one person share that person's speed and their "
                 "fitted prior.</p>",
                 f"<p>R² = {fmt(table.attrs.get('r_squared'), 4)} over "
                 f"{fmt(table.attrs.get('n'), 0)} events.</p>",
                 "<p><b>Why entropy is not used as the selection cost.</b> Mean entropy is "
                 "highest in Condition A, and Condition A is the fastest condition, so "
                 "entropy alone predicts the ordering backwards. It conflates two "
                 "situations a softmax cannot tell apart: many options being acceptable "
                 "(free choice, fast) and evidence fighting the prior (cued, slow). "
                 "Conflict separates them because it is measured against the cued finger "
                 "and is zero when nothing is cued.</p>"]
        widgets = [self._table_widget(
            table[["component", "term", "estimate", "se", "t", "p", "ci95_lo", "ci95_hi"]],
            decimals=4)]
        datasets = {"model_rt_decomposition": table,
                    "model_rt_components": self._rt_components,
                    "model_rt_entropy_check": self._rt_entropy,
                    "model_rt_condition_by_digit": self._rt_interaction,
                    "model_rt_evidence_time_link": self._rt_link,
                    "model_rt_observed_vs_predicted": self._rt_observed,
                    "model_rt_residuals": self._rt_residuals}
        lines.append("<p><b>What the selection term does and does not absorb.</b> Adding "
                     "it takes a real bite out of the rise from Condition A to the cued "
                     "conditions, and almost none out of the gap between B and C — which "
                     "is why that gap is reported as measured rather than explained.</p>")
        widgets.append(self._table_widget(self._rt_components, decimals=4))
        widgets.append(self._table_widget(self._rt_entropy, decimals=3))
        lines.append("<p><b>The accumulator prediction, tested and not supported.</b> If "
                     "time to a decision criterion were set by the evidence rate, a "
                     "participant with weaker fitted cue evidence should pay a larger "
                     "reaction-time cost. Correlating each participant's own value against "
                     "their own cost, within condition, does not show it. That is why no "
                     "drift-diffusion or race model is fitted anywhere in this analysis: "
                     "the study has 233 and 73 genuine substitutions under B and C, which "
                     "cannot identify drift, boundary and non-decision time separately, "
                     "and the one prediction that is testable here fails.</p>")
        widgets.append(self._table_widget(self._rt_link, decimals=4))
        widgets.append(self._table_widget(self._rt_residuals, decimals=4))
        return ("".join(lines), figures, datasets, widgets)

    def _plot_rt_components(self, observed: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.8))
        axes = figure.subplots(1, 2)
        for condition in em.CONDITIONS:
            subset = observed[observed["condition"] == condition]
            axes[0].scatter(subset["observed_rt_s"], subset["predicted_rt_s"],
                            s=34, color=CONDITION_COLORS[condition], label=condition,
                            edgecolor="white", linewidth=0.5)
        limit = float(np.nanmax([observed["observed_rt_s"].max(),
                                 observed["predicted_rt_s"].max()])) * 1.08
        axes[0].plot([0, limit], [0, limit], color="#888888", linewidth=0.9, linestyle="--")
        axes[0].set_xlim(0, limit)
        axes[0].set_ylim(0, limit)
        axes[0].set_xlabel("observed mean RT (s)")
        axes[0].set_ylabel("fitted mean RT (s)")
        axes[0].legend(fontsize=8)
        axes[0].set_title("Participant × condition (in sample)", fontsize=10)

        table = self._rt_table.set_index("term")
        terms = [t for t in ("conflict", "C(condition)[T.B]", "C(condition)[T.C]",
                             "C(digit)[T.4]", "C(digit)[T.5]") if t in table.index]
        labels = {"conflict": "selection\n(per nat)", "C(condition)[T.B]": "visual cue\ncost",
                  "C(condition)[T.C]": "haptic cue\ncost", "C(digit)[T.4]": "ring\n(execution)",
                  "C(digit)[T.5]": "little\n(execution)"}
        values = [table.loc[t, "estimate"] for t in terms]
        errors = [[table.loc[t, "estimate"] - table.loc[t, "ci95_lo"] for t in terms],
                  [table.loc[t, "ci95_hi"] - table.loc[t, "estimate"] for t in terms]]
        axes[1].bar(range(len(terms)), values, color="#6a7f95", width=0.55)
        axes[1].errorbar(range(len(terms)), values, yerr=errors, fmt="none",
                         color="black", capsize=4, linewidth=1.1)
        axes[1].axhline(0, color="black", linewidth=0.8)
        axes[1].set_xticks(range(len(terms)), [labels[t] for t in terms], fontsize=8)
        axes[1].set_ylabel("log reaction time")
        axes[1].set_title("Components (condition terms are measured, not derived)",
                          fontsize=10)
        figure.tight_layout()
        return figure

    # ------------------------------------------------------------------
    # Prediction tabs
    #
    # Everything below scores rows the fitting model never saw. Nothing on
    # this side of the tab strip is an in-sample fitted value.

    _PREDICTION_PREAMBLE = (
        "<p><b>Held-out only.</b> Every number on this tab comes from a model fitted "
        "without the row it is scoring. Under leave-one-participant-out the held-out "
        "person contributes nothing to the fit; under the within-participant split the "
        "held-out <i>trials</i> do not. The fold that produced each row is carried in its "
        "<code>fold_id</code>, and the Prediction Diagnostics tab audits the split.</p>")

    def _primary(self, variant: str = ep.POPULATION) -> pd.DataFrame:
        return self._run.for_model(self._run.primary_model, variant)

    def _build_prediction_summary(self):
        summary = ep.prediction_summary(self._run)
        by_participant = ep.choice_metrics_by_participant(self._run.predictions)
        paired = ep.compare_models_paired(by_participant, reference=self._run.primary_model)
        figures = {"prediction_summary": self._plot_prediction_summary(summary)}
        lines = ["<h3>Out-of-sample prediction summary</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>The question is not which model fits the collected data best but "
                 "which predicts data it has not seen. Models are ranked by held-out log "
                 "loss over the cued conditions; the accuracy columns are shown beside it "
                 "and should not be used for the ranking, because they saturate.</p>",
                 "<p>The comparison set runs from base rates through a memorising lookup "
                 "table and an off-the-shelf multinomial logistic classifier to the "
                 "structured models. A structured model earns its parameters only by "
                 "beating those on the same held-out events.</p>",
                 "<p>The paired table below is over PARTICIPANTS — each model against the "
                 "primary model on the same twenty held-out people — so the unit of the "
                 "test is the same one the rest of the study uses.</p>"]
        widgets = [self._table_widget(summary, decimals=4)]
        if len(paired):
            widgets.append(self._table_widget(paired, decimals=4))
        return ("".join(lines), figures,
                {"prediction_summary": summary,
                 "prediction_by_participant": by_participant,
                 "prediction_model_contrasts": paired}, widgets)

    def _plot_prediction_summary(self, summary: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 4.2))
        axes = figure.subplots(1, 2)
        order = summary.sort_values("log_loss", ascending=False)
        y = np.arange(len(order))
        colors = ["#c4453a" if name == self._run.primary_model else "#7a8898"
                  for name in order["model_name"]]
        axes[0].barh(y, order["log_loss"], color=colors, height=0.6)
        axes[0].set_yticks(y, order["model_name"], fontsize=8)
        axes[0].set_xlabel("held-out log loss (lower is better)")
        axes[0].set_xlim(left=0)
        axes[0].set_title("What ranks the models", fontsize=10)

        axes[1].barh(y, order["top1_accuracy"], color=colors, height=0.6)
        axes[1].set_yticks(y, ["" for _ in y])
        axes[1].set_xlabel("held-out top-1 accuracy")
        axes[1].set_xlim(left=0)
        axes[1].set_title("What does not (saturates)", fontsize=10)
        figure.suptitle("Held-out prediction on the cued conditions, "
                        "leave-one-participant-out", fontsize=11)
        figure.tight_layout()
        return figure

    def _build_choice_prediction(self):
        metrics = ep.choice_metrics(self._run.predictions)
        primary = self._primary()
        observed, predicted = ep.prediction_confusion(
            self._run.predictions, "B", self._run.primary_model)
        figures = {"prediction_choice_metrics": self._plot_choice_metrics(metrics)}
        for condition in em.CUED_CONDITIONS:
            obs, pred = ep.prediction_confusion(self._run.predictions, condition,
                                                self._run.primary_model)
            figures[f"prediction_confusion_{condition}"] = self._plot_confusion(
                obs, pred, f"{condition} (held out)")
        lines = ["<h3>Finger choice prediction</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>For each held-out event the model is given the pressed key, the cued "
                 "finger, the condition, the previous action and the reach and motor "
                 "features, plus the prior and cue parameters learned from the other "
                 "participants, and returns a probability for each of the ten fingers. "
                 "Top-1 is the finger it ranks first; top-2 allows the second.</p>",
                 "<p>The confusion matrices are built from held-out probability mass, so "
                 "the predicted panel is the model's full expectation over a participant "
                 "it never saw — not a fitted reconstruction of them.</p>"]
        widgets = [self._table_widget(metrics, decimals=4)]
        del observed, predicted, primary
        return ("".join(lines), figures, {"prediction_choice_metrics": metrics}, widgets)

    def _plot_choice_metrics(self, metrics: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 4.0))
        axes = figure.subplots(1, 2)
        frame = metrics[metrics["variant"] == ep.POPULATION]
        models = list(dict.fromkeys(frame["model_name"]))
        width = 0.8 / max(len(em.CONDITIONS), 1)
        for index, condition in enumerate(em.CONDITIONS):
            subset = frame[frame["condition"] == condition].set_index("model_name")
            values = [subset["log_loss"].get(m, np.nan) for m in models]
            axes[0].bar(np.arange(len(models)) + index * width - 0.4 + width / 2,
                        values, width, label=condition, color=CONDITION_COLORS[condition])
            accuracy = [subset["top1_accuracy"].get(m, np.nan) for m in models]
            axes[1].bar(np.arange(len(models)) + index * width - 0.4 + width / 2,
                        accuracy, width, color=CONDITION_COLORS[condition])
        for ax, label in ((axes[0], "held-out log loss"), (axes[1], "held-out top-1")):
            ax.set_xticks(range(len(models)),
                          [m.replace(" ", "\n", 1) for m in models], fontsize=7)
            ax.set_ylabel(label)
            ax.set_ylim(bottom=0)
        axes[0].legend(fontsize=8, title="condition")
        figure.suptitle("Held-out choice prediction by model and condition", fontsize=11)
        figure.tight_layout()
        return figure

    def _build_error_risk(self):
        metrics = ep.error_risk_metrics(self._run.predictions)
        deciles = ep.error_risk_deciles(self._run.predictions)
        risky = ep.highest_risk_events(self._run.predictions, condition="B",
                                       model_name=self._run.primary_model)
        figures = {"prediction_error_risk": self._plot_error_risk(deciles)}
        warnings = metrics["warning"].dropna().unique().tolist()
        lines = ["<h3>Error risk prediction</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>Before the keypress, the model's ten-way distribution is folded into "
                 "four mutually exclusive outcomes: the cued finger, a within-hand "
                 "substitution, the homologous digit on the wrong hand, and any other "
                 "cross-hand action. The question is whether the events it calls risky "
                 "are the ones that actually fail.</p>",
                 "<p><b>Accuracy is not reported for these classes and should not be.</b> "
                 "Predicting “correct” on every event scores about 98.7% under B and "
                 "99.96% on the wrong-hand class under C. Average precision — the area "
                 "under the precision-recall curve — is the summary, because unlike ROC "
                 "area it does not flatter a classifier on a class that is a fraction of a "
                 "percent of the data. The <i>lift over base rate</i> column is the honest "
                 "read: 1.0 means no better than guessing at the base rate.</p>"]
        if warnings:
            lines.append("<p><b style='color:#b03a2e'>Rare-event warnings:</b> "
                         + "; ".join(warnings) + ". Treat these rows as exploratory.</p>")
        lines.append("<p>The decile table bins events by predicted risk and shows the "
                     "observed failure rate in each bin — the most direct form of “are the "
                     "risky ones actually risky”. Below it are the individual held-out "
                     "events the model flagged hardest under visual guidance, with what "
                     "each one actually did.</p>")
        widgets = [self._table_widget(metrics, decimals=4),
                   self._table_widget(deciles, decimals=4),
                   self._table_widget(risky, decimals=3)]
        return ("".join(lines), figures,
                {"prediction_error_risk_metrics": metrics,
                 "prediction_error_risk_deciles": deciles,
                 "prediction_highest_risk_events": risky}, widgets)

    def _plot_error_risk(self, deciles: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.8))
        axes = figure.subplots(1, 2)
        frame = deciles[(deciles["variant"] == ep.POPULATION)
                        & (deciles["model_name"] == self._run.primary_model)]
        for ax, condition in zip(axes, em.CUED_CONDITIONS):
            subset = frame[frame["condition"] == condition]
            if subset.empty:
                continue
            ax.plot(subset["risk_bin"], subset["observed_rate"], marker="o",
                    color=CONDITION_COLORS[condition], label="observed")
            ax.plot(subset["risk_bin"], subset["mean_predicted"], marker="s",
                    linestyle="--", color="#6a7f95", label="predicted")
            ax.set_xlabel("predicted-risk bin (low → high)")
            ax.set_ylabel("homologous-error rate")
            ax.set_ylim(bottom=0)
            ax.set_title(f"Condition {condition}", fontsize=10)
            ax.legend(fontsize=8)
        figure.suptitle("Predicted homologous-error risk against what actually happened "
                        "(held out)", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_rt_prediction(self):
        metrics = ep.rt_metrics(self._run.predictions)
        by_participant = ep.rt_by_participant(self._run.predictions,
                                              self._run.primary_model)
        residuals = ep.rt_residual_diagnostics(self._run.predictions,
                                               self._run.primary_model)
        figures = {"prediction_rt": self._plot_rt_prediction(by_participant)}
        lines = ["<h3>Reaction-time prediction</h3>",
                 self._PREDICTION_PREAMBLE,
                 f"<p>{_MEASURED_NOT_DERIVED}</p>",
                 "<p>The reaction-time model is refitted inside every fold on the training "
                 "events only, with no participant intercepts — a held-out person has no "
                 "fitted intercept and inventing one would be the leak this whole module "
                 "exists to avoid. The personalised variant adds an intercept offset taken "
                 "from that person's own Condition A trials, which is calibration data "
                 "rather than a fitted effect.</p>",
                 "<p>R² is computed against the mean of the held-out reaction times, so a "
                 "negative value means the model did worse than that constant. Such a "
                 "value is left as it is rather than clipped.</p>"]
        widgets = [self._table_widget(metrics, decimals=4),
                   self._table_widget(residuals, decimals=4)]
        return ("".join(lines), figures,
                {"prediction_rt_metrics": metrics,
                 "prediction_rt_by_participant": by_participant,
                 "prediction_rt_residuals": residuals}, widgets)

    def _plot_rt_prediction(self, by_participant: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.8))
        axes = figure.subplots(1, 2)
        for condition in em.CONDITIONS:
            subset = by_participant[by_participant["condition"] == condition]
            axes[0].scatter(subset["observed_rt_s"], subset["predicted_rt_s"], s=34,
                            color=CONDITION_COLORS[condition], label=condition,
                            edgecolor="white", linewidth=0.5)
        finite = by_participant[["observed_rt_s", "predicted_rt_s"]].to_numpy(float)
        limit = float(np.nanmax(finite)) * 1.08 if np.isfinite(finite).any() else 1.0
        axes[0].plot([0, limit], [0, limit], color="#888888", linestyle="--", linewidth=0.9)
        axes[0].set_xlim(0, limit)
        axes[0].set_ylim(0, limit)
        axes[0].set_xlabel("observed mean RT (s)")
        axes[0].set_ylabel("predicted mean RT (s)")
        axes[0].legend(fontsize=8)
        axes[0].set_title("Held-out participant × condition", fontsize=10)

        primary = self._primary()
        for condition in em.CONDITIONS:
            subset = primary[primary["condition"] == condition]
            axes[1].hist(subset["rt_residual"].dropna(), bins=50, histtype="step",
                         color=CONDITION_COLORS[condition], label=condition)
        axes[1].axvline(0, color="black", linewidth=0.8)
        axes[1].set_xlabel("observed − predicted RT (s)")
        axes[1].set_ylabel("held-out events")
        axes[1].legend(fontsize=8)
        axes[1].set_title("Residuals", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_calibration(self):
        calibration = ep.calibration_table(self._run.predictions)
        error = ep.expected_calibration_error(calibration)
        homologous = ep.calibration_table(self._run.predictions, column="p_homologous",
                                          outcome=ep.OUTCOME_HOMOLOGOUS)
        figures = {"prediction_calibration": self._plot_calibration(calibration)}
        lines = ["<h3>Calibration</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>Ranking and calibration are different properties. A model can order "
                 "events perfectly and still say 0.9 where the truth is 0.5, and a "
                 "probability that is not calibrated cannot be used as a risk. Bins are "
                 "equal-width on the predicted probability, so regions with no events stay "
                 "visibly empty rather than being smoothed away by equal-count bins.</p>",
                 "<p>Expected calibration error is the bin-count-weighted mean absolute "
                 "gap between predicted and observed — one number per model and condition, "
                 "so calibration can be compared alongside log loss.</p>"]
        widgets = [self._table_widget(error, decimals=4),
                   self._table_widget(calibration, decimals=4)]
        return ("".join(lines), figures,
                {"prediction_calibration": calibration,
                 "prediction_calibration_error": error,
                 "prediction_calibration_homologous": homologous}, widgets)

    def _plot_calibration(self, calibration: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.8))
        axes = figure.subplots(1, 2)
        frame = calibration[(calibration["variant"] == ep.POPULATION)
                            & (calibration["model_name"] == self._run.primary_model)]
        for ax, condition in zip(axes, em.CUED_CONDITIONS):
            subset = frame[frame["condition"] == condition]
            if subset.empty:
                continue
            ax.plot([0, 1], [0, 1], color="#888888", linestyle="--", linewidth=0.9,
                    label="perfect calibration")
            sizes = 20 + 180 * subset["n"] / max(subset["n"].max(), 1)
            ax.scatter(subset["predicted"], subset["observed"], s=sizes,
                       color=CONDITION_COLORS[condition], alpha=0.85,
                       edgecolor="white", linewidth=0.6)
            ax.plot(subset["predicted"], subset["observed"],
                    color=CONDITION_COLORS[condition], linewidth=1.0)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.set_xlabel("predicted P(cued finger)")
            ax.set_ylabel("observed rate")
            ax.set_title(f"Condition {condition}", fontsize=10)
            ax.legend(fontsize=8, loc="upper left")
        figure.suptitle("Calibration of the held-out probability "
                        "(marker area is the number of events in the bin)", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_population_personalised(self):
        comparison = ep.population_vs_personalised(self._run.predictions)
        if comparison.empty:
            return ("<h3>Population vs personalised</h3>"
                    "<p>The personalised pass was not run, so there is nothing to "
                    "compare.</p>", {}, {}, [])
        figures = {"prediction_population_vs_personalised":
                   self._plot_population_personalised(comparison)}
        lines = ["<h3>Population against personalised prediction</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p><b>Population</b> means a participant nobody has seen: only the "
                 "group-level prior and the event's own features are available. "
                 "<b>Personalised</b> means the same held-out participant, except that "
                 "their own Condition A trials may calibrate their habitual prior and "
                 "their baseline speed. Condition A carries no finger cue, so using it "
                 "leaks nothing about the cued events being predicted, and their B and C "
                 "events are never touched.</p>",
                 "<p><b>This is a comparison, not an improvement.</b> Earlier work on this "
                 "dataset already found no cross-participant gain from a personal habitual "
                 "prior, so a null here is an expected outcome and is reported as one. The "
                 "verdict column separates a tight null — the personal prior genuinely "
                 "adds nothing detectable — from a wide one, which would mean the "
                 "comparison is simply inconclusive.</p>"]
        widgets = [self._table_widget(comparison, decimals=4)]
        return ("".join(lines), figures,
                {"prediction_population_vs_personalised": comparison}, widgets)

    def _plot_population_personalised(self, comparison: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.6))
        metrics = list(dict.fromkeys(comparison["metric"]))
        axes = figure.subplots(1, max(len(metrics), 1))
        axes = np.atleast_1d(axes)
        labels = {"log_loss": "log loss (lower better)", "top1": "top-1 accuracy",
                  "mae": "RT MAE, s (lower better)"}
        for ax, metric in zip(axes, metrics):
            subset = comparison[comparison["metric"] == metric]
            y = np.arange(len(subset))
            ax.barh(y - 0.18, subset["population"], 0.34, label="population",
                    color="#6a7f95")
            ax.barh(y + 0.18, subset["personalised"], 0.34, label="personalised",
                    color="#c48f3a")
            ax.set_yticks(y, subset["model"], fontsize=8)
            ax.set_xlabel(labels.get(metric, metric))
            ax.set_xlim(left=0)
            ax.set_title(metric, fontsize=10)
        axes[0].legend(fontsize=8)
        figure.suptitle("Does a held-out participant's own key-only data help predict "
                        "their cued trials?", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_prediction_by_condition(self):
        metrics = ep.choice_metrics(self._run.predictions)
        rt = ep.rt_metrics(self._run.predictions)
        primary_choice = metrics[(metrics["model_name"] == self._run.primary_model)]
        primary_rt = rt[rt["model_name"] == self._run.primary_model]
        figures = {"prediction_by_condition": self._plot_by_condition(primary_choice)}
        lines = ["<h3>Prediction by condition</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>The primary model's held-out performance split by condition. "
                 "Condition A is the hardest to predict and should be: with no finger cue "
                 "the choice is genuinely free, so the ceiling is set by how stereotyped "
                 "the person's habit is, not by how good the model is. The cued conditions "
                 "are easier because the cue constrains the answer — and C is easier than "
                 "B, which is the same channel difference showing up as predictability.</p>"]
        widgets = [self._table_widget(primary_choice, decimals=4),
                   self._table_widget(primary_rt, decimals=4)]
        return ("".join(lines), figures,
                {"prediction_by_condition_choice": primary_choice,
                 "prediction_by_condition_rt": primary_rt}, widgets)

    def _plot_by_condition(self, metrics: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.6))
        axes = figure.subplots(1, 3)
        frame = metrics[metrics["variant"] == ep.POPULATION].set_index("condition")
        panels = [("log_loss", "held-out log loss"), ("top1_accuracy", "top-1 accuracy"),
                  ("top2_accuracy", "top-2 accuracy")]
        for ax, (column, label) in zip(axes, panels):
            conditions = [c for c in em.CONDITIONS if c in frame.index]
            values = [frame.loc[c, column] for c in conditions]
            ax.bar(range(len(conditions)), values,
                   color=[CONDITION_COLORS[c] for c in conditions], width=0.55)
            ax.set_xticks(range(len(conditions)), conditions)
            ax.set_ylabel(label)
            ax.set_ylim(bottom=0)
        figure.suptitle("Primary model, held out, by condition", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_prediction_by_finger(self):
        primary = self._primary()
        by_finger = (primary.assign(digit=primary["target_finger"].str[1].astype(int))
                     .groupby(["condition", "target_finger"])
                     .agg(n=("log_loss", "size"), log_loss=("log_loss", "mean"),
                          top1=("top1_hit", "mean"),
                          predicted_correct=("p_correct", "mean"),
                          observed_correct=("correct", "mean")).reset_index())
        by_digit = (primary.assign(digit=primary["target_finger"].str[1].astype(int))
                    .groupby(["condition", "digit"])
                    .agg(n=("log_loss", "size"), log_loss=("log_loss", "mean"),
                         top1=("top1_hit", "mean"),
                         predicted_correct=("p_correct", "mean"),
                         observed_correct=("correct", "mean")).reset_index())
        by_digit["digit_name"] = by_digit["digit"].map(em.DIGIT_NAMES)
        figures = {"prediction_by_finger": self._plot_by_finger(by_digit)}
        lines = ["<h3>Prediction by cued finger</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>Held-out predicted probability of using the cued finger against the "
                 "observed rate, per digit. This is the per-digit calibration check: a gap "
                 "between the two lines in one digit means the model is systematically "
                 "wrong about that finger for participants it has not seen, which a "
                 "pooled score would hide.</p>"]
        widgets = [self._table_widget(by_digit, decimals=4),
                   self._table_widget(by_finger, decimals=4)]
        return ("".join(lines), figures,
                {"prediction_by_digit": by_digit, "prediction_by_finger": by_finger},
                widgets)

    def _plot_by_finger(self, by_digit: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(9.5, 3.6))
        axes = figure.subplots(1, 2)
        for ax, condition in zip(axes, em.CUED_CONDITIONS):
            subset = by_digit[by_digit["condition"] == condition].sort_values("digit")
            if subset.empty:
                continue
            x = subset["digit"].to_numpy()
            ax.plot(x, subset["observed_correct"], marker="o",
                    color=CONDITION_COLORS[condition], label="observed")
            ax.plot(x, subset["predicted_correct"], marker="s", linestyle="--",
                    color="#6a7f95", label="predicted (held out)")
            ax.set_xticks(x, [f"{d}\n{em.DIGIT_NAMES[d]}" for d in x], fontsize=8)
            ax.set_ylabel("P(cued finger used)")
            ax.set_title(f"Condition {condition}", fontsize=10)
            ax.legend(fontsize=8)
        figure.suptitle("Per-digit held-out calibration", fontsize=10)
        figure.tight_layout()
        return figure

    def _build_prediction_diagnostics(self):
        audit = ep.leakage_audit(self._run, self._events)
        folds = (self._run.predictions.groupby(["model_name", "variant", "fold_id"])
                 .size().reset_index(name="n_events"))
        failed = audit[~audit["holds"].astype(bool)]
        lines = ["<h3>Prediction diagnostics</h3>",
                 "<p>“These are predictions, not fitted values” is the one claim on the "
                 "prediction tabs that cannot be checked by looking at a number, so it is "
                 "audited instead. Each row below states a property that would be false if "
                 "a split had leaked.</p>"]
        if len(failed):
            lines.append("<p><b style='color:#b03a2e'>Audit FAILED:</b> "
                         + "; ".join(failed["check"]) + ". Treat every prediction number "
                         "in this window as unsafe until this is resolved.</p>")
        else:
            lines.append("<p><b style='color:#2e7d32'>All checks pass.</b> Every scored "
                         "row was produced by a fold that excluded it.</p>")
        lines.append("<p>The fold table shows how many events each fold contributed, so an "
                     "unbalanced or missing fold is visible rather than averaged away.</p>")
        widgets = [self._table_widget(audit, decimals=0),
                   self._table_widget(folds, decimals=0)]
        return ("".join(lines), {}, {"prediction_leakage_audit": audit,
                                     "prediction_folds": folds}, widgets)

    # ------------------------------------------------------------------
    # Event-level prediction viewer
    #
    # The aggregate tables say the model ranks risk well; this is where a
    # single event can be opened and the claim checked on it.

    def _build_event_viewer(self):
        frame = self._primary()
        self._viewer_frame = frame
        self.viewer_participant = QComboBox()
        self.viewer_participant.addItems(sorted(frame["participant"].unique()))
        self.viewer_trial = QComboBox()
        self.viewer_event = QComboBox()
        self.viewer_participant.currentTextChanged.connect(self._viewer_participant_changed)
        self.viewer_trial.currentTextChanged.connect(self._viewer_trial_changed)
        self.viewer_event.currentTextChanged.connect(self._viewer_event_changed)

        self.viewer_detail = QLabel("Select an event.")
        self.viewer_detail.setWordWrap(True)
        self.viewer_detail.setTextFormat(Qt.TextFormat.RichText)
        self.viewer_distribution = QTableWidget(0, 0)

        selector = QWidget()
        row = QHBoxLayout(selector)
        row.setContentsMargins(0, 0, 0, 0)
        for label, box in (("Participant", self.viewer_participant),
                           ("Trial", self.viewer_trial), ("Event", self.viewer_event)):
            row.addWidget(QLabel(label + ":"))
            row.addWidget(box)
        risky_btn = QPushButton("Jump to the riskiest held-out event")
        risky_btn.setToolTip("Selects the event the model gave the highest predicted "
                             "homologous-error probability under visual guidance.")
        risky_btn.clicked.connect(self._viewer_jump_to_risky)
        row.addWidget(risky_btn)
        row.addStretch(1)

        self._viewer_participant_changed(self.viewer_participant.currentText())

        lines = ["<h3>Event viewer</h3>",
                 self._PREDICTION_PREAMBLE,
                 "<p>One held-out event at a time: what was cued, what the model expected "
                 "before the keypress, and what actually happened. The distribution below "
                 "is the model's full ten-way answer for that event, produced by the fold "
                 "that excluded this participant.</p>"]
        return ("".join(lines), {}, {}, [selector, self.viewer_detail,
                                         self.viewer_distribution])

    def _viewer_participant_changed(self, participant: str) -> None:
        if not participant:
            return
        frame = self._viewer_frame
        trials = sorted(frame[frame["participant"] == participant]["trial"].unique())
        self.viewer_trial.blockSignals(True)
        self.viewer_trial.clear()
        self.viewer_trial.addItems([str(t) for t in trials])
        self.viewer_trial.blockSignals(False)
        self._viewer_trial_changed(self.viewer_trial.currentText())

    def _viewer_trial_changed(self, trial: str) -> None:
        if not trial:
            return
        frame = self._viewer_frame
        participant = self.viewer_participant.currentText()
        events = sorted(frame[(frame["participant"] == participant)
                              & (frame["trial"] == int(trial))]["event"].unique())
        self.viewer_event.blockSignals(True)
        self.viewer_event.clear()
        self.viewer_event.addItems([str(e) for e in events])
        self.viewer_event.blockSignals(False)
        self._viewer_event_changed(self.viewer_event.currentText())

    def _viewer_event_changed(self, event: str) -> None:
        if not event:
            return
        frame = self._viewer_frame
        match = frame[(frame["participant"] == self.viewer_participant.currentText())
                      & (frame["trial"] == int(self.viewer_trial.currentText()))
                      & (frame["event"] == int(event))]
        if match.empty:
            return
        self._show_event(match.iloc[0])

    def _viewer_jump_to_risky(self) -> None:
        frame = self._viewer_frame
        visual = frame[frame["condition"] == "B"]
        if visual.empty:
            return
        row = visual.loc[visual["p_homologous"].idxmax()]
        self.viewer_participant.setCurrentText(str(row["participant"]))
        self.viewer_trial.setCurrentText(str(row["trial"]))
        self.viewer_event.setCurrentText(str(row["event"]))

    def _show_event(self, row: pd.Series) -> None:
        correct = row["actual_finger"] == row["target_finger"]
        verdict = ("<b style='color:#2e7d32'>cued finger used</b>" if correct
                   else f"<b style='color:#b03a2e'>{row['observed_outcome']}</b>")
        predicted_hit = row["predicted_finger"] == row["actual_finger"]
        rt_line = ("n/a" if not np.isfinite(row.get("predicted_rt", np.nan))
                   else f"{row['predicted_rt']:.3f} s predicted vs "
                        f"{row['actual_rt']:.3f} s actual "
                        f"(residual {row['rt_residual']:+.3f} s)")
        self.viewer_detail.setText(
            f"<table cellpadding='4'>"
            f"<tr><td><b>Fold</b></td><td>{row['fold_id']}</td>"
            f"<td><b>Condition</b></td><td>{row['condition']}</td>"
            f"<td><b>Target key</b></td><td>{int(row['target_key'])}</td></tr>"
            f"<tr><td><b>Cued finger</b></td><td>{row['target_finger']}</td>"
            f"<td><b>Finger used</b></td><td>{row['actual_finger']}</td>"
            f"<td><b>Outcome</b></td><td>{verdict}</td></tr>"
            f"<tr><td><b>Model's first choice</b></td>"
            f"<td>{row['predicted_finger']} "
            f"{'✓' if predicted_hit else '✗'}</td>"
            f"<td><b>Second choice</b></td><td>{row.get('second_choice', 'n/a')}</td>"
            f"<td><b>Log loss</b></td><td>{row['log_loss']:.3f}</td></tr>"
            f"<tr><td><b>P(cued finger)</b></td><td>{row['p_correct']:.4f}</td>"
            f"<td><b>P(wrong hand)</b></td><td>{row['p_wrong_hand']:.4f}</td>"
            f"<td><b>P(homologous)</b></td><td>{row['p_homologous']:.4f}</td></tr>"
            f"<tr><td><b>P(within-hand slip)</b></td>"
            f"<td>{row.get('p_within_hand', float('nan')):.4f}</td>"
            f"<td><b>Reaction time</b></td><td colspan='3'>{rt_line}</td></tr>"
            f"</table>")

        spatial = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]
        table = self.viewer_distribution
        table.clear()
        table.setRowCount(3)
        table.setColumnCount(len(spatial))
        table.setHorizontalHeaderLabels(spatial)
        table.setVerticalHeaderLabels(["P(finger)", "cued", "used"])
        for column, finger in enumerate(spatial):
            probability = float(row[f"p_{finger}"])
            item = QTableWidgetItem(f"{probability:.4f}")
            # Shade by probability so the shape of the distribution reads
            # at a glance rather than having to be compared digit by digit.
            from PySide6.QtGui import QColor
            shade = int(255 - min(probability, 1.0) * 150)
            item.setBackground(QColor(shade, 255 - (255 - shade) // 3, shade))
            table.setItem(0, column, item)
            table.setItem(1, column, QTableWidgetItem(
                "●" if finger == row["target_finger"] else ""))
            table.setItem(2, column, QTableWidgetItem(
                "●" if finger == row["actual_finger"] else ""))
        table.resizeColumnsToContents()
        table.setMaximumHeight(140)

    # ------------------------------------------------------------------
    # Save / load / export

    def _save_fit(self) -> None:
        if self._fit is None:
            return
        MODEL_FIT_DIR.mkdir(parents=True, exist_ok=True)
        default = MODEL_FIT_DIR / f"{self._fit.spec.name.split()[0]}_fit.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save fitted model", str(default), "Model fit (*.json)")
        if not path:
            return
        fit = self._fit
        document = {
            "schema_version": 1,
            "model": fit.spec.name,
            "spec": {"cue": list(fit.spec.cue), "motor": list(fit.spec.motor),
                     "reach": fit.spec.reach, "prior": fit.spec.prior,
                     "prior_by_participant": fit.spec.prior_by_participant,
                     "prior_weight": fit.spec.prior_weight,
                     "cue_by_digit": fit.spec.cue_by_digit,
                     "l2": fit.spec.l2, "l2_prior_dev": fit.spec.l2_prior_dev,
                     "cue_bound": fit.spec.cue_bound},
            "participants": list(fit.participants),
            "n_keys": fit.n_keys,
            "n_events": fit.n_events,
            "n_params": fit.n_params,
            "loglik": fit.loglik,
            "converged": fit.converged,
            "params": [float(v) for v in fit.params],
            "coefficients": fit.coefficients().to_dict(orient="records"),
            "evidence_contrasts": em.evidence_contrasts(fit).to_dict(orient="records"),
        }
        try:
            # ensure_ascii=False with an explicit encoding, matching every
            # other JSON this project writes.
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
        except OSError as error:
            QMessageBox.warning(self, "Could not save", str(error))
            return
        self._log(f"Saved fitted model to {path}")

    def _load_fit(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load fitted model", str(MODEL_FIT_DIR), "Model fit (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not load", str(error))
            return
        selection = document.get("participants") or []
        self._log(f"Loading fitted model from {path} "
                  f"({document.get('model')}, {len(selection)} participants).")
        data = ga.load_group(selection)
        if not data.included:
            QMessageBox.warning(
                self, "Participants unavailable",
                "The saved fit refers to participants that are not exported here:\n"
                + ", ".join(selection))
            return
        try:
            events = em.build_events(data.event_rows)
        except em.EffectorModelError as error:
            QMessageBox.warning(self, "Nothing to model", str(error))
            return

        stored = document.get("spec", {})
        spec = em.ModelSpec(
            name=document.get("model", "loaded model"),
            cue=tuple(stored.get("cue", em.ModelSpec().cue)),
            motor=tuple(stored.get("motor", em.MOTOR_FEATURES)),
            reach=stored.get("reach", True), prior=stored.get("prior", True),
            prior_by_participant=stored.get("prior_by_participant", True),
            prior_weight=stored.get("prior_weight", True),
            cue_by_digit=stored.get("cue_by_digit", False),
            l2=stored.get("l2", 1e-2), l2_prior_dev=stored.get("l2_prior_dev", 1.0),
            cue_bound=stored.get("cue_bound", 20.0))
        layout = em._layout(spec, list(document["participants"]), int(document["n_keys"]))
        params = np.asarray(document["params"], dtype=float)
        if params.size != layout.size:
            QMessageBox.warning(
                self, "Fit does not match the data",
                f"The saved fit has {params.size} parameters but this model and data need "
                f"{layout.size}. The export or the participant set has changed since it "
                "was written; refit rather than loading it.")
            return

        self._reset_results()
        self._events = events
        self._accounting = em.event_accounting(data.event_rows)
        self._fit = em.ChoiceFit(
            spec=spec, params=params, layout=layout, loglik=float(document["loglik"]),
            n_events=int(document["n_events"]), n_params=int(document["n_params"]),
            converged=bool(document.get("converged", True)), message="loaded from file",
            participants=list(document["participants"]), n_keys=int(document["n_keys"]))
        self._scored = em.attach_predictions(self._fit, events)
        # The derived tables are cheap next to the fit itself, and
        # recomputing them keeps a loaded model from showing stale
        # diagnostics beside freshly loaded data.
        self._do_channel_test(events, spec)
        self._do_per_participant(events)
        self._do_rt(events)
        self._rebuild_tabs()
        self.predict_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.export_summary_btn.setEnabled(True)
        self._log("Loaded. Cross-validation, ablations and bootstrap are not stored in the "
                  "fit file - refit if those tabs are needed.")

    # Report figures are wider and shorter than the on-screen one: a
    # figure sits in a column at a fixed width, and the panels have to
    # stay legible when it is scaled to fit rather than filling a window.
    SUMMARY_FIGURE_SIZE = (13.0, 4.9)
    SUMMARY_FIGURE_DPI = 400

    def _export_summary_figure(self) -> None:
        """The three-panel summary on its own, at report resolution.

        Re-rendered rather than saved from the screen: the on-screen
        figure is sized to a window, and a figure that is placed at a
        fixed column width needs its own proportions or the text comes
        out at whatever size the window happened to be."""
        if self._fit is None:
            return
        MODEL_FIGURE_DIR.mkdir(parents=True, exist_ok=True)
        headline = em.headline_numbers(self._fit, self._events)
        concentration = within_hand = {}
        if self._run is not None:
            concentration = ep.risk_concentration(
                self._run.predictions, model_name=self._run.primary_model)
            within_hand = ep.risk_concentration(
                self._run.predictions, outcome=ep.OUTCOME_WITHIN_HAND,
                column="p_within_hand", model_name=self._run.primary_model)
        figure = self._plot_summary(headline, concentration, within_hand)
        figure.set_size_inches(*self.SUMMARY_FIGURE_SIZE)
        figure.tight_layout(pad=1.5, w_pad=2.2)

        written, failures = [], []
        for suffix, options in ((".png", {"dpi": self.SUMMARY_FIGURE_DPI}), (".svg", {})):
            path = MODEL_FIGURE_DIR / f"computational_model_summary{suffix}"
            try:
                figure.savefig(path, bbox_inches="tight", facecolor="white", **options)
                written.append(path.name)
            except Exception as error:
                failures.append(f"{path.name} ({type(error).__name__}: {error})")
        status = (f"Wrote {' and '.join(written)} to {MODEL_FIGURE_DIR}"
                  if written else "Nothing was written.")
        if failures:
            status += "  Failed: " + "; ".join(failures)
        if not self._run:
            status += ("  Panel C is empty — run the out-of-sample prediction first if the "
                       "figure is for the report.")
        self._log(status)
        self.status_label.setText(status)

    def _export(self) -> None:
        if not self._figures and not self._datasets:
            return
        MODEL_FIGURE_DIR.mkdir(parents=True, exist_ok=True)
        datasets = dict(self._datasets)
        if self._run is not None:
            # Two per-event tables, deliberately.
            #
            # prediction_events is every model in the comparison set and
            # both variants - what makes the model comparison auditable,
            # and about eighty megabytes.
            #
            # prediction_events_primary is the primary model's population
            # predictions alone: one row per modelled event, a few
            # megabytes, and the one that answers "why was this event
            # flagged". The big file is regenerable from the tracked
            # inputs under a fixed seed, so it is the one to leave out of
            # version control (see the repository .gitignore); the small
            # one is meant to be kept.
            datasets["prediction_events"] = ep.export_frame(self._run.predictions)
            datasets["prediction_events_primary"] = ep.primary_export_frame(
                self._run.predictions, self._run.primary_model)
        provenance = {
            "participants": ", ".join(self._fit.participants) if self._fit else "",
            "model": self._fit.spec.name if self._fit else "",
            "events": self._fit.n_events if self._fit else 0,
        }
        # No filename prefix: every slug already begins with "model_" or
        # "prediction_", and adding one on top produced model_model_*.
        # The manifest is then "_manifest.csv", which sorts to the top of
        # the folder exactly as the group export's does.
        status = export_analysis(self, MODEL_FIGURE_DIR, self._figures, datasets,
                                 provenance)
        self._log(status)
        self.status_label.setText(status)
