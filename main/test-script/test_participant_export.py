"""Coverage tests for the Participant Analysis window's "Export figures + data".

The single-participant export used to be opt-in in the same way the group
one was: figures were registered by _add_tab, but the tidy tables behind
them were set by hand inside three of the nine _build_ methods, so six
tabs rendered charts whose numbers never reached a CSV. Registration now
happens in _add_tab for both, and these tests are what keeps it that way:

  * every tab must contribute at least one figure AND at least one tidy
    table to the export;
  * every registered table must be a non-empty DataFrame with
    filesystem-safe slugs (they become file stems);
  * _save_figures must actually write each PNG, SVG and CSV, plus a
    manifest naming the participant and the valid-event denominator;
  * confirmed carry-over events must be out of the event-level tabs -
    the trial-level statistics already excluded them, so an event tab
    that kept them would contradict the numbers printed beside it.

Run from main/:  python test-script/test_participant_export.py
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

from app.gui import participant_analysis_window as paw  # noqa: E402
from finger_fixtures import make_events  # noqa: E402

_APP = QApplication.instance() or QApplication([])

PARTICIPANT = "P00"

# Every tab and the export prefix its tables carry. A new tab must be
# added here, which is the point: the list is the contract.
TAB_TABLE_PREFIXES = {
    "Overview": "overview_",
    "Learning": "learning_",
    "Difficulty": "difficulty_",
    "Trade-off": "tradeoff_",
    "Errors": "error",
    "Confusion": "confusion_",
    "Fingers": "fingers_",
    "Finger benefit": "finger_benefit_",
    "Timing": "timing_",
    "Quality": "quality_",
}

TAB_FIGURE_PREFIXES = {
    "Overview": "overview",
    "Learning": "learning_",
    "Difficulty": "difficulty",
    "Trade-off": "tradeoff",
    "Errors": "errors_",
    "Confusion": "confusion_",
    "Fingers": "fingers_",
    "Finger benefit": "finger_benefit",
    "Timing": "timing_",
    "Quality": "quality_",
}


def trial_rows(participant=PARTICIPANT, conditions=("A", "B", "C"),
               levels=("alpha", "beta", "gamma")):
    """One participant's full 27-trial schedule, enough for every tab.

    Trial indices run condition-major (A 1-9, B 10-18, C 19-27) so they
    line up with the event fixture, which the threshold-sensitivity and
    session-position computations join on."""
    rows = []
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
                    "misses": 0, "wrong_key": 0,
                    "suspected_carryover": 1, "excluded_carryover": 1,
                    "manual_corrections": 0, "unresolved_rate": 0.02,
                    "ambiguous_rate": 0.03, "borderline_events": 1,
                    "qc_extra_presses": 2,
                })
                index += 1
    return rows


_WINDOW = None


def build_window():
    """Built once and shared: ten tabs and ~23 figures is slow enough that
    rebuilding per test class doubles this file's runtime for nothing."""
    global _WINDOW
    if _WINDOW is not None:
        return _WINDOW
    trials = trial_rows()
    events = make_events(n_participants=1, trials_per_condition=9, events_per_trial=30)
    # One manually confirmed carry-over, so the exclusion has something
    # to bite on and the counts can be checked against it.
    events[0] = {**events[0], "validity": "invalid_carryover"}

    w = paw.ParticipantAnalysisWindow()
    w._figures, w._datasets = {}, {}
    w._participant = PARTICIPANT
    w._trials = trials
    w._all_events = events
    w._events = paw.pa.valid_events(events)
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
        a stray separator lands the file somewhere else.

        A figure and a table MAY share a stem - quality_audit.png and
        quality_audit.csv are the chart and the numbers behind it, and
        naming them alike is the point. What must never repeat is a stem
        within one kind, because those do collide on disk."""
        for kind in (self.window._figures, self.window._datasets):
            self.assertEqual(len(list(kind)), len(set(kind)))
        written = ([f"{s}.png" for s in self.window._figures]
                   + [f"{s}.svg" for s in self.window._figures]
                   + [f"{s}.csv" for s in self.window._datasets])
        self.assertEqual(len(written), len(set(written)))
        for slug in list(self.window._figures) + list(self.window._datasets):
            self.assertNotIn("/", slug)
            self.assertNotIn(" ", slug)
            self.assertTrue(slug.replace("_", "").replace(".", "").replace("-", "").isalnum(),
                            slug)

    def test_the_core_tables_are_present_by_name(self):
        """The tables a results chapter actually needs — losing one of
        these to a refactor should fail loudly, not quietly."""
        for slug in ("overview_condition_metrics",
                     "overview_cell_metrics",
                     "learning_within_cell_repetition",
                     "learning_session_position",
                     "learning_difficulty_progression_trials",
                     "error_proportions",
                     "fingers_homologous_cells",
                     "finger_benefit_cells",
                     "finger_benefit_dispersion",
                     "quality_audit",
                     "quality_threshold_sensitivity"):
            self.assertIn(slug, self.window._datasets)

    def test_finger_exports_that_mirror_the_group_grid_exclude_condition_a(self):
        """A carries no target-finger cue, so it is not a per-finger
        baseline — the tables that feed a B/C comparison must not smuggle
        it in."""
        self.assertEqual(
            set(self.window._datasets["finger_benefit_cells"].columns)
            & {"baseline", "cued"}, {"baseline", "cued"})
        self.assertEqual(
            set(self.window._datasets["finger_benefit_dispersion"]["condition"]), {"B", "C"})
        self.assertEqual(
            set(self.window._datasets["learning_session_position"]["condition"]), {"B", "C"})


class TestCarryOverExclusion(unittest.TestCase):
    """The one event marked invalid_carryover must not reach an outcome."""

    @classmethod
    def setUpClass(cls):
        cls.window = build_window()

    def test_valid_events_are_one_short_of_the_raw_rows(self):
        self.assertEqual(len(self.window._events), len(self.window._all_events) - 1)

    def test_event_level_tables_use_the_valid_denominator(self):
        breakdown = self.window._datasets["error_breakdown"]
        self.assertEqual(int(breakdown["count"].sum()), len(self.window._events))

    def test_the_excluded_event_survives_in_the_quality_audit(self):
        audit = self.window._datasets["quality_audit"].iloc[0]
        self.assertEqual(int(audit["n_events"]), len(self.window._all_events))
        self.assertEqual(int(audit["n_valid_events"]), len(self.window._events))


class TestSaveFigures(unittest.TestCase):

    def test_writes_every_png_svg_csv_and_a_manifest(self):
        window = build_window()
        with tempfile.TemporaryDirectory() as tmp:
            original = paw.STUDY_DATA_DIR
            paw.STUDY_DATA_DIR = Path(tmp)
            try:
                window._save_figures()
            finally:
                paw.STUDY_DATA_DIR = original
            out_dir = Path(tmp) / PARTICIPANT / "figures"
            written = {p.name for p in out_dir.iterdir()}
            for slug in window._figures:
                self.assertIn(f"{PARTICIPANT}_{slug}.png", written)
                self.assertIn(f"{PARTICIPANT}_{slug}.svg", written)
            for slug in window._datasets:
                self.assertIn(f"{PARTICIPANT}_{slug}.csv", written)

            manifest = pd.read_csv(out_dir / f"{PARTICIPANT}__manifest.csv")
            self.assertEqual(manifest.iloc[0]["kind"], "provenance")
            self.assertEqual(manifest.iloc[0]["rows"], PARTICIPANT)
            # The denominator has to be self-identifying in the appendix.
            self.assertIn(f"{len(window._events)} valid", manifest.iloc[0]["columns"])
            self.assertEqual(len(manifest) - 1,
                             len(window._figures) + len(window._datasets))

    def test_a_table_that_cannot_be_written_does_not_abort_the_others(self):
        """One unwritable file used to take the whole export down with it,
        which is the worst moment to lose the other fifty."""
        window = build_window()
        with tempfile.TemporaryDirectory() as tmp:
            original = paw.STUDY_DATA_DIR
            paw.STUDY_DATA_DIR = Path(tmp)
            broken = pd.DataFrame({"x": [1]})
            broken.to_csv = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
            window._datasets["zz_broken_table"] = broken
            try:
                window._save_figures()
            finally:
                paw.STUDY_DATA_DIR = original
                window._datasets.pop("zz_broken_table")
            status = window.status_label.text()
            self.assertIn("FAILED", status)
            self.assertIn("zz_broken_table", status)
            out_dir = Path(tmp) / PARTICIPANT / "figures"
            self.assertIn(f"{PARTICIPANT}_overview_condition_metrics.csv",
                          {p.name for p in out_dir.iterdir()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
