"""Coverage tests for the Group Analysis window's "Export figures + data".

The export used to be opt-in: each tab had to remember to register its
own figures and tables, and six of the tabs never did - so the button
silently wrote three tabs' worth of output and looked like it had worked.
Registration now happens in _add_tab, and these tests are what keeps it
that way:

  * every tab that renders a figure must contribute at least one figure
    AND at least one tidy table to the export;
  * every registered table must be a non-empty DataFrame whose columns
    survive a CSV round-trip;
  * _save_figures must actually write each PNG, SVG and CSV, plus a
    manifest that lists all of them and records which participants the
    export came from.

Run from main/:  python test-script/test_group_export.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import condition_a_strategy as cas  # noqa: E402
from app import group_analysis as ga  # noqa: E402
from app.gui import group_analysis_window as gaw  # noqa: E402
from finger_fixtures import make_events  # noqa: E402

_APP = QApplication.instance() or QApplication([])

# Every tab and the export prefix its tables carry. A new tab must be
# added here, which is the point: the list is the contract.
TAB_TABLE_PREFIXES = {
    "Overview": "overview_",
    "Condition × Difficulty": "condition_difficulty_",
    "Contrasts": "contrasts_",
    "Trade-off": "group_tradeoff",
    "Learning / Order": "learning_",
    "Condition A Strategy": "condition_a_",
    "Errors": "errors_",
    "Fingers": "fingers_",
    "Finger Confusion": "confusion_group_",
    "RM-ANOVA": "rm_anova_",
    "Finger Benefit": "finger_",
    "Quality": "quality_",
}

TAB_FIGURE_PREFIXES = {
    "Overview": "group_overview",
    "Condition × Difficulty": "group_condition_difficulty",
    "Contrasts": "group_contrasts",
    "Trade-off": "group_tradeoff",
    "Learning / Order": "group_learning",
    "Condition A Strategy": "group_condition_a",
    "Errors": "group_errors",
    "Fingers": "group_fingers",
    "Finger Confusion": "group_confusion",
    "RM-ANOVA": "group_rm_anova",
    "Finger Benefit": "group_finger",
    "Quality": "group_quality",
}


def trial_rows(participants, conditions=("A", "B", "C"), levels=("alpha", "beta", "gamma")):
    """A full 27-trial schedule per participant, enough for every tab."""
    rows = []
    for participant in participants:
        index = 1
        for condition in conditions:
            for level in levels:
                for rep in range(3):
                    rows.append({
                        "participant": participant, "trial_index": index,
                        "condition": condition, "condition_label": {
                            "A": "key only", "B": "visual", "C": "haptic"}[condition],
                        "level": level, "level_symbol": level[0],
                        "sequence": f"seq-{condition}-{level}-{rep}",
                        "quiz_name": "q", "guidance_type": condition,
                        "sync_method": "led", "analyzed": True, "note_count": 30,
                        "key_accuracy": 0.92,
                        "fa_main": {"A": 0.60, "B": 0.72, "C": 0.80}[condition] + 0.01 * rep,
                        "fa_given_key": 0.75,
                        "rt_correct_key_s": {"A": 1.10, "B": 0.95, "C": 0.75}[condition],
                        "rt_complete_s": {"A": 1.15, "B": 1.00, "C": 0.78}[condition],
                        "misses": 0, "suspected_carryover": 0, "excluded_carryover": 0,
                        "manual_corrections": 0, "unresolved_rate": 0.02,
                        "ambiguous_rate": 0.03, "borderline_events": 1,
                        "qc_extra_presses": 2, "fa_theta_0.30": 0.70, "fa_theta_0.50": 0.62,
                    })
                    index += 1
    return rows


_WINDOW = None


def build_window():
    """Built once and shared: a full analysis over 7 participants renders
    ~15 figures including two 3D scatters, which is slow enough that
    rebuilding it per test class doubles the file's runtime for nothing."""
    global _WINDOW
    if _WINDOW is not None:
        return _WINDOW
    participants = [f"P{i:02d}" for i in range(7)]
    events = make_events(n_participants=7, trials_per_condition=9, events_per_trial=30)
    data = ga.GroupData(included=participants,
                        trial_rows=trial_rows(participants), event_rows=events)
    w = gaw.GroupAnalysisWindow()
    w._figures, w._datasets = {}, {}
    w._data = data
    w._pc = ga.participant_condition_metrics(data.trial_rows)
    w._cells = ga.participant_cell_metrics(data.trial_rows)
    w._cond_titles = w._condition_titles(data.trial_rows)
    # Build the tabs the way the window does, by walking its own
    # _tab_builders() list. Copying the list here instead - which this
    # test used to do - meant a newly added tab was never built, so the
    # coverage contract below silently stopped applying to it, which is
    # exactly the failure these tests exist to prevent.
    for title, build in w._tab_builders():
        w._tab_runner(title, build)()
    _WINDOW = w
    return w


class TestExportCoverage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.window = build_window()

    def test_every_tab_is_rendered(self):
        titles = [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())]
        self.assertEqual(titles, list(TAB_TABLE_PREFIXES))

    def test_every_tab_contributes_at_least_one_figure(self):
        slugs = list(self.window._figures)
        for tab, prefix in TAB_FIGURE_PREFIXES.items():
            self.assertTrue(any(s.startswith(prefix) for s in slugs),
                            f"tab {tab!r} exports no figure (expected a "
                            f"{prefix!r}* slug); registration was probably skipped")

    def test_every_tab_contributes_at_least_one_table(self):
        slugs = list(self.window._datasets)
        for tab, prefix in TAB_TABLE_PREFIXES.items():
            self.assertTrue(any(s.startswith(prefix) for s in slugs),
                            f"tab {tab!r} exports no tidy table (expected a "
                            f"{prefix!r}* slug)")

    def test_registered_tables_are_non_empty_dataframes(self):
        for slug, df in self.window._datasets.items():
            self.assertIsInstance(df, pd.DataFrame, slug)
            self.assertGreater(len(df), 0, f"{slug} is an empty table")
            self.assertGreater(len(df.columns), 0, slug)

    def test_figure_and_table_slugs_are_unique_and_filesystem_safe(self):
        """Slugs become file stems, so a duplicate silently overwrites and
        a stray separator lands the file somewhere else."""
        all_slugs = list(self.window._figures) + list(self.window._datasets)
        self.assertEqual(len(all_slugs), len(set(all_slugs)))
        for slug in all_slugs:
            self.assertNotIn("/", slug)
            self.assertNotIn(" ", slug)
            self.assertTrue(slug.replace("_", "").replace(".", "").replace("-", "").isalnum(),
                            slug)

    def test_the_core_tables_are_present_by_name(self):
        """The tables a results chapter actually needs — losing one of
        these to a refactor should fail loudly, not quietly."""
        for slug in ("overview_participant_condition_metrics",
                     "condition_difficulty_participant_cells",
                     "contrasts_participant_differences",
                     "contrasts_tests",
                     "learning_session_position",
                     "learning_difficulty_progression_trials",
                     "learning_difficulty_progression_group_summary",
                     "condition_a_trial_strategy",
                     "errors_participant_proportions",
                     "fingers_participant_cells",
                     "quality_participant_audit",
                     "finger_benefit_cells"):
            self.assertIn(slug, self.window._datasets)

    def test_condition_a_has_participant_hand_and_finger_usage_views(self):
        for slug in (
                "group_condition_a_hand_usage_by_participant",
                "group_condition_a_finger_usage_by_participant"):
            self.assertIn(slug, self.window._figures)
        hand_usage = self.window._datasets["condition_a_hand_usage"]
        self.assertEqual(set(hand_usage["hand"]), {"L", "R"})
        totals = hand_usage.groupby("participant")["share_pct"].sum()
        np.testing.assert_allclose(totals.to_numpy(dtype=float), 100)
        heatmap = self.window._figures[
            "group_condition_a_finger_usage_by_participant"
        ]
        labels = [tick.get_text().split("\n")[0]
                  for tick in heatmap.axes[0].get_xticklabels()]
        self.assertEqual(labels, cas.SPATIAL_FINGER_ORDER)

    def test_inferential_performance_exports_exclude_condition_a(self):
        for slug in ("condition_difficulty_participant_cells",
                     "fingers_participant_cells"):
            self.assertEqual(set(self.window._datasets[slug]["condition"]), {"B", "C"}, slug)
        self.assertEqual(
            set(self.window._datasets["contrasts_participant_differences"]["contrast"]),
            {"C−B"},
        )

    def test_overview_retains_descriptive_a_b_c_participant_lines(self):
        """A is context, not inference, but the requested descriptive
        within-participant A-B-C trace must stay visible."""
        for slug in ("group_overview_accuracy", "group_overview_rt"):
            for ax in self.window._figures[slug].axes:
                traces = [
                    line for line in ax.lines
                    if len(line.get_xdata()) == 3
                    and np.allclose(line.get_xdata(), [0, 1, 2])
                ]
                self.assertGreaterEqual(
                    len(traces), self.window._data.n,
                    f"{slug} lost one or more A-B-C participant traces",
                )

    def test_difficulty_axes_use_alpha_beta_gamma_labels(self):
        expected = [ga.LEVEL_TICK_LABELS[level] for level in ga.LEVELS]
        figure = self.window._figures["group_condition_difficulty"]
        for ax in figure.axes:
            self.assertEqual([tick.get_text() for tick in ax.get_xticklabels()],
                             expected)
            self.assertEqual(ax.get_legend().get_title().get_text(),
                             "Feedback condition")

    def test_descriptive_tradeoff_keeps_condition_a(self):
        for slug in ("group_tradeoff_trial_points",
                     "group_tradeoff_participant_centroids",
                     "group_tradeoff_group_centroids"):
            self.assertEqual(set(self.window._datasets[slug]["condition"]),
                             {"A", "B", "C"}, slug)

    def test_threshold_curve_is_automatic_bc_and_contains_raw_040(self):
        theta = self.window._datasets["quality_threshold_sensitivity"]
        self.assertEqual(set(theta["condition"]), {"B", "C"})
        self.assertEqual(set(theta["theta"]), {0.30, 0.35, 0.40, 0.45, 0.50})
        self.assertEqual(set(theta["source"]), {"automatic_target_probability"})


class TestExportWritesFiles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.window = build_window()

    def _export(self, tmp: Path):
        saved = gaw.STUDY_DATA_DIR
        gaw.STUDY_DATA_DIR = tmp
        try:
            self.window._save_figures()
        finally:
            gaw.STUDY_DATA_DIR = saved
        return tmp / "group_figures"

    def test_every_registered_item_reaches_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._export(Path(tmp))
            for slug in self.window._figures:
                for ext in ("png", "svg"):
                    path = out / f"{slug}.{ext}"
                    self.assertTrue(path.is_file(), f"missing {path.name}")
                    self.assertGreater(path.stat().st_size, 0, path.name)
            for slug in self.window._datasets:
                path = out / f"{slug}.csv"
                self.assertTrue(path.is_file(), f"missing {path.name}")
                self.assertGreater(len(pd.read_csv(path)), 0, path.name)

    def test_manifest_lists_everything_and_names_the_participants(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._export(Path(tmp))
            manifest = pd.read_csv(out / "_manifest.csv")
            listed = set(manifest["file"].dropna())
            for slug in self.window._datasets:
                self.assertIn(f"{slug}.csv", listed)
            for slug in self.window._figures:
                self.assertIn(f"{slug}.png / {slug}.svg", listed)
            provenance = manifest[manifest["kind"] == "provenance"].iloc[0]
            self.assertIn("P00", str(provenance["columns"]))
            self.assertIn("N = 7", str(provenance["rows"]))

    def test_status_line_reports_the_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._export(Path(tmp))
            text = self.window.status_label.text()
            self.assertIn(f"{len(self.window._figures)} figures", text)
            self.assertIn(f"{len(self.window._datasets)} CSVs", text)
            self.assertNotIn("FAILED", text)

    def test_one_unwritable_item_does_not_abort_the_rest(self):
        """A single bad figure must cost one file, not the whole export."""
        class Exploding:
            @staticmethod
            def savefig(*a, **kw):
                raise RuntimeError("boom")

        self.window._figures["group_broken_figure"] = Exploding()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = self._export(Path(tmp))
                self.assertIn("FAILED", self.window.status_label.text())
                self.assertIn("group_broken_figure", self.window.status_label.text())
                self.assertTrue((out / "_manifest.csv").is_file())
                self.assertTrue((out / "fingers_participant_cells.csv").is_file())
        finally:
            self.window._figures.pop("group_broken_figure", None)


class TestAxisPolicy(unittest.TestCase):
    """Latency axes may not be cropped, and no figure plots a transformed value.

    A cropped RT baseline makes the gap between two conditions look like
    whatever the crop chooses, which is the property a reader checks when
    they ask how a figure was made - so it is asserted here rather than
    reviewed.

    Percentage axes are deliberately exempt. Accuracy in this study sits
    at the ceiling, so pinning those panels to 0-100% turns every
    condition into one flat line and hides the movement the panel exists
    to show; they autoscale, and their captions say so.
    """

    @classmethod
    def setUpClass(cls):
        cls.window = build_window()

    # Measures whose scale does not bottom out at zero. "Effective number
    # of fingers" is a diversity index over the ten fingers: one finger is
    # its floor, so 1-10 already IS the full range and a 0-10 axis would
    # reserve space for an impossible value.
    NON_ZERO_FLOORS = {"effective number of fingers": 1.0}

    @staticmethod
    def _axes(figure):
        """2D axes only - a 3D projection keeps its limits elsewhere."""
        return [ax for ax in figure.axes if not hasattr(ax, "get_zlim")]

    def test_no_axis_starts_above_zero_when_all_its_data_is_positive(self):
        """Checked on every non-percentage y axis, and on any RT x axis.

        Ordinal x axes (occurrence 1-9, repetition 1-3) are categories,
        not quantities, so their tick range is not a baseline claim, and
        percentage axes are exempt for the reason in the class docstring.
        """
        for slug, figure in self.window._figures.items():
            for index, ax in enumerate(self._axes(figure)):
                candidates = []
                if "%" not in ax.get_ylabel():
                    candidates.append(("y", ax.get_ylim, ax.dataLim.y0, ax.dataLim.y1))
                if "(ms)" in ax.get_xlabel():
                    candidates.append(("x", ax.get_xlim, ax.dataLim.x0, ax.dataLim.x1))
                for name, get_lim, low, high in candidates:
                    if not (np.isfinite(low) and np.isfinite(high)) or low < 0:
                        continue
                    lim = get_lim()
                    if lim[0] > lim[1]:       # inverted, e.g. a matrix
                        continue
                    floor = self.NON_ZERO_FLOORS.get(
                        ax.get_ylabel().lower().replace("\n", " ")
                        if name == "y" else ax.get_xlabel().lower(), 0.0)
                    self.assertLessEqual(
                        lim[0], floor + 1e-9,
                        f"{slug} axis {index} {name}-range starts at {lim[0]:.2f} "
                        f"but its data is all >= {floor:.0f} - a cropped baseline")

    def test_every_rt_axis_starts_at_zero(self):
        """The positive form of the rule above, so it cannot pass vacuously."""
        checked = 0
        for slug, figure in self.window._figures.items():
            for ax in self._axes(figure):
                label = ax.get_ylabel()
                if "(ms)" not in label and label != "ms":
                    continue
                if ax.dataLim.y0 < 0:     # a signed paired difference
                    continue
                self.assertLessEqual(ax.get_ylim()[0], 1e-9,
                                     f"{slug}: RT axis starts at {ax.get_ylim()[0]:.1f} ms")
                checked += 1
        self.assertGreater(checked, 0, "found no RT axis to check")

    def test_no_figure_plots_a_composition_adjusted_value(self):
        """Every plotted point has to exist in the exported trial table."""
        for slug in self.window._figures:
            self.assertNotIn("adjusted", slug)
            self.assertNotIn("error_log", slug)
        for figure in self.window._figures.values():
            for ax in self._axes(figure):
                self.assertNotIn("adjusted", ax.get_ylabel().lower())
                self.assertNotIn("adjusted", ax.get_title().lower())
                self.assertNotEqual(ax.get_yscale(), "symlog")


if __name__ == "__main__":
    unittest.main(verbosity=2)
