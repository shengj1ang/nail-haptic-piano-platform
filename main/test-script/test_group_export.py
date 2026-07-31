"""Coverage tests for the Group Analysis window's "Export figures + data".

The export used to be opt-in: each tab had to remember to register its
own figures and tables, and six of the ten tabs never did - so the button
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

import pandas as pd  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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
    "Errors": "errors_",
    "Fingers": "fingers_",
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
    "Errors": "group_errors",
    "Fingers": "group_fingers",
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
    w._add_tab("Overview", *w._build_overview())
    w._add_tab("Condition × Difficulty", *w._build_condition_difficulty())
    w._add_tab("Contrasts", *w._build_contrasts())
    w._add_tradeoff_tab()
    w._add_tab("Learning / Order", *w._build_learning())
    w._add_tab("Errors", *w._build_errors())
    w._add_tab("Fingers", *w._build_fingers())
    w._add_tab("RM-ANOVA", *w._build_rm_anova())
    w._add_tab("Finger Benefit", *w._build_finger_benefit())
    w._add_tab("Quality", *w._build_quality())
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
                     "errors_participant_proportions",
                     "fingers_participant_cells",
                     "quality_participant_audit",
                     "finger_benefit_cells"):
            self.assertIn(slug, self.window._datasets)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
