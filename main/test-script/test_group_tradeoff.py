"""Unit tests for app.group_tradeoff (the GUI-free data/colour/figure
layer behind the Group Analysis Trade-off tab) plus an offscreen render
test of the tab itself.

Run from main/:  python test-script/test_group_tradeoff.py
"""

import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from app import group_tradeoff as gt  # noqa: E402
from app.group_analysis import CONDITIONS, LEVELS  # noqa: E402
from test_group_analysis import full_schedule, trial  # noqa: E402


def schedules(n_participants: int, **kw):
    """n complete 27-trial schedules P01..Pnn."""
    rows = []
    for i in range(n_participants):
        rows += full_schedule(f"P{i + 1:02d}", **kw)
    return rows


def png_size(path: Path):
    """(width, height) from the PNG IHDR header - no PIL dependency."""
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


class TestParticipantWeighting(unittest.TestCase):
    def test_group_centroid_weights_participants_not_trials(self):
        # P01: 1 included trial (400 ms, 100%); P02: 3 trials (800 ms, 0%).
        # Pooling the 4 trials would give 700 ms / 25%; the participant-
        # weighted group centroid must be 600 ms / 50%.
        trials = [trial("P01", 1, rt_correct_key_s=0.4, fa_main=1.0)]
        trials += [trial("P02", i + 1, rt_correct_key_s=0.8, fa_main=0.0)
                   for i in range(3)]
        data = gt.compute(trials, ["P01", "P02"])
        row = data.gcent[data.gcent["condition"] == "A"].iloc[0]
        self.assertAlmostEqual(float(row["rt_ms"]), 600.0)
        self.assertAlmostEqual(float(row["fa_pct"]), 50.0)
        self.assertEqual(int(row["n_participants"]), 2)  # never 4 trials

    def test_difficulty_group_centroid_also_participant_first(self):
        # Same imbalance inside one condition x level cell.
        trials = [trial("P01", 1, level="beta", rt_correct_key_s=0.4)]
        trials += [trial("P02", i + 1, level="beta", rt_correct_key_s=0.8)
                   for i in range(3)]
        data = gt.compute(trials, ["P01", "P02"])
        row = data.gcent_level[(data.gcent_level["condition"] == "A")
                               & (data.gcent_level["level"] == "beta")].iloc[0]
        self.assertAlmostEqual(float(row["rt_ms"]), 600.0)
        self.assertEqual(int(row["n_participants"]), 2)


class TestCentroids(unittest.TestCase):
    def test_participant_centroid_is_mean_of_included_trials(self):
        trials = [
            trial("P01", 1, rt_correct_key_s=0.4, fa_main=0.2),
            trial("P01", 2, rt_correct_key_s=0.6, fa_main=0.4),
            trial("P01", 3, rt_correct_key_s=None, fa_main=0.9),   # no valid RT
            trial("P01", 4, analyzed=False, rt_correct_key_s=0.1),  # no FA
        ]
        data = gt.compute(trials, ["P01"])
        self.assertEqual(int(data.points["included"].sum()), 2)
        cen = data.pcent.iloc[0]
        self.assertEqual(int(cen["n_trials"]), 2)  # excluded trials never enter
        self.assertAlmostEqual(float(cen["rt_ms"]), 500.0)
        self.assertAlmostEqual(float(cen["fa_pct"]), 30.0)

    def test_excluded_trials_are_nan_points_not_zeros(self):
        pts = gt.trial_points([trial("P01", 1, rt_correct_key_s=None)])
        self.assertFalse(bool(pts["included"].iloc[0]))
        self.assertTrue(math.isnan(float(pts["rt_ms"].iloc[0])))
        self.assertTrue(math.isnan(float(pts["fa_pct"].iloc[0])))


class TestCMinusB(unittest.TestCase):
    def test_delta_signs_and_verdict_when_c_wins(self):
        rows = schedules(2, fa_by_condition={"B": 0.5, "C": 0.8},
                         rt_by_condition={"B": 0.6, "C": 0.45})
        summary = gt.group_summary(gt.compute(rows, ["P01", "P02"]).gcent)
        d = summary["delta_c_minus_b"]
        self.assertAlmostEqual(d["fa_pct"], 30.0)   # C more accurate: positive
        self.assertAlmostEqual(d["rt_ms"], -150.0)  # C faster: negative
        self.assertEqual(summary["verdict"],
                         "C is descriptively faster and more accurate than B.")

    def test_no_verdict_when_c_is_slower(self):
        rows = schedules(2, fa_by_condition={"B": 0.5, "C": 0.8},
                         rt_by_condition={"B": 0.45, "C": 0.6})
        summary = gt.group_summary(gt.compute(rows, ["P01", "P02"]).gcent)
        self.assertGreater(summary["delta_c_minus_b"]["rt_ms"], 0)
        self.assertIsNone(summary["verdict"])


class TestDifficultyMapping(unittest.TestCase):
    def test_levels_map_to_discrete_z_and_symbols(self):
        self.assertEqual(gt.LEVEL_Z, {"alpha": 0, "beta": 1, "gamma": 2})
        data = gt.compute(schedules(2), ["P01", "P02"])
        fig = gt.build_group_tradeoff_by_difficulty_3d(data)
        ax = fig.axes[0]
        self.assertEqual([t.get_text() for t in ax.get_zticklabels()],
                         ["α", "β", "γ"])  # symbols, never 0/1/2

    def test_export_uses_symbols(self):
        data = gt.compute(schedules(1), ["P01"])
        ds = gt.export_datasets(data)
        self.assertEqual(set(ds["group_tradeoff_trial_points"]["difficulty"]),
                         {"α", "β", "γ"})


class TestParticipantOrder(unittest.TestCase):
    def test_z_axis_follows_given_order_never_resorted(self):
        order = ["P10", "P02", "P07"]  # deliberately not string-sorted
        rows = sum((full_schedule(p) for p in order), [])
        data = gt.compute(rows, order)
        self.assertEqual(data.participants, order)
        fig = gt.build_group_tradeoff_by_participant_3d(data)
        labels = [t.get_text() for t in fig.axes[0].get_zticklabels()]
        self.assertEqual(labels[:3], order)
        self.assertEqual(labels[3], "Group mean")  # own slot, no participant plane

    def test_no_group_slot_when_group_centroids_hidden(self):
        data = gt.compute(schedules(2), ["P01", "P02"])
        fig = gt.build_group_tradeoff_by_participant_3d(
            data, options=gt.TradeoffOptions(show_group_centroids=False))
        labels = [t.get_text() for t in fig.axes[0].get_zticklabels()]
        self.assertEqual(labels, ["P01", "P02"])


class TestMissingCells(unittest.TestCase):
    def test_missing_condition_absent_not_zeroed(self):
        rows = full_schedule("P01") + [t for t in full_schedule("P02")
                                       if t["condition"] != "C"]
        data = gt.compute(rows, ["P01", "P02"])
        self.assertTrue(data.pcent[(data.pcent["participant"] == "P02")
                                   & (data.pcent["condition"] == "C")].empty)
        c_row = data.gcent[data.gcent["condition"] == "C"].iloc[0]
        self.assertEqual(int(c_row["n_participants"]), 1)  # P02 skipped, not 0-filled
        missing = gt.missing_cells(data)
        self.assertIn("P02 C/α", missing)
        self.assertEqual(len(missing), 3)  # the three C levels only

    def test_summary_handles_condition_with_no_data(self):
        rows = [t for t in full_schedule("P01") if t["condition"] == "A"]
        summary = gt.group_summary(gt.compute(rows, ["P01"]).gcent)
        self.assertNotIn("B", summary["conditions"])
        self.assertIsNone(summary["delta_c_minus_b"])
        self.assertIsNone(summary["verdict"])


class TestCarryOver(unittest.TestCase):
    def test_valid_event_count_subtracts_confirmed_carryover(self):
        pts = gt.trial_points([trial(note_count=30, excluded_carryover=3)])
        self.assertEqual(int(pts["valid_event_count"].iloc[0]), 27)

    def test_rt_fa_taken_from_exported_columns_unchanged(self):
        # The exported rt_correct_key_s / fa_main already exclude the
        # confirmed carry-over events; the tab must reuse them verbatim.
        pts = gt.trial_points([trial(rt_correct_key_s=0.42, fa_main=0.7)])
        self.assertAlmostEqual(float(pts["rt_ms"].iloc[0]), 420.0)
        self.assertAlmostEqual(float(pts["fa_pct"].iloc[0]), 70.0)


class TestErrorBarModes(unittest.TestCase):
    def test_sd_bounds_narrower_than_t_ci_at_n2(self):
        # Two participants, RTs 400 / 600 ms: SD = 141.4 -> ±SD bar
        # (358.6, 641.4); the 95% t-CI uses t(0.975, 1) = 12.706 ->
        # (-770.6, 1770.6). Both computed over participant centroids.
        trials = [trial("P01", 1, rt_correct_key_s=0.4),
                  trial("P02", 1, rt_correct_key_s=0.6)]
        data = gt.compute(trials, ["P01", "P02"])
        row = data.gcent.iloc[0]
        sd_lo, sd_hi = gt.error_bounds(row, "rt", "sd")
        self.assertAlmostEqual(sd_lo, 500 - 141.4213562, places=4)
        self.assertAlmostEqual(sd_hi, 500 + 141.4213562, places=4)
        ci_lo, ci_hi = gt.error_bounds(row, "rt", "ci")
        self.assertAlmostEqual(ci_lo, 500 - 12.7062047 * 100, places=3)
        self.assertLess(ci_lo, sd_lo)  # the long bars the CI mode draws
        self.assertGreater(ci_hi, sd_hi)

    def test_no_bounds_in_either_mode_below_two_participants(self):
        data = gt.compute([trial("P01", 1)], ["P01"])
        row = data.gcent.iloc[0]
        self.assertIsNone(gt.error_bounds(row, "rt", "ci"))
        self.assertIsNone(gt.error_bounds(row, "rt", "sd"))
        self.assertIsNone(gt.error_bounds(row, "fa", "sd"))

    def test_default_mode_is_ci_and_both_render(self):
        self.assertEqual(gt.TradeoffOptions().error_bars, "ci")
        data = gt.compute(schedules(2), ["P01", "P02"])
        for mode in ("ci", "sd"):
            figs = gt.build_figures(data, options=gt.TradeoffOptions(error_bars=mode))
            legend = figs["group_tradeoff_2d"].axes[0].get_legend()
            labels = [t.get_text() for t in legend.get_texts()]
            self.assertTrue(any(gt.error_bar_label(mode) in l for l in labels))

    def test_group_centroid_csv_carries_sd_and_ci(self):
        ds = gt.export_datasets(gt.compute(schedules(2), ["P01", "P02"]))
        cols = set(ds["group_tradeoff_group_centroids"].columns)
        self.assertLessEqual({"rt_sd", "fa_sd", "rt_ci95_lo", "fa_ci95_hi"}, cols)


class TestSmallN(unittest.TestCase):
    def test_single_participant_mean_without_ci(self):
        data = gt.compute(full_schedule("P01"), ["P01"])
        row = data.gcent.iloc[0]
        self.assertEqual(int(row["n_participants"]), 1)
        self.assertFalse(np.isnan(float(row["rt_ms"])))       # mean still drawn
        self.assertTrue(np.isnan(float(row["rt_ci95_lo"])))   # CI never fabricated
        self.assertTrue(np.isnan(float(row["fa_ci95_lo"])))
        gt.build_figures(data)  # and the figures still render


class TestExportDatasets(unittest.TestCase):
    def test_row_counts_and_required_fields(self):
        data = gt.compute(schedules(2), ["P01", "P02"])
        ds = gt.export_datasets(data)
        tp = ds["group_tradeoff_trial_points"]
        pc = ds["group_tradeoff_participant_centroids"]
        gc = ds["group_tradeoff_group_centroids"]
        self.assertEqual(len(tp), 54)          # 2 x 27 trials
        self.assertEqual(len(pc), 6 + 18)      # per-condition + per-level rows
        self.assertEqual(len(gc), 3 + 9)
        for df, extra in ((tp, {"participant_id", "trial_order"}),
                          (pc, {"participant_id", "n_trials"}),
                          (gc, {"n_participants", "rt_ci95_lo", "fa_ci95_hi"})):
            self.assertLessEqual(
                {"condition", "difficulty", "mean_rt_correct_key_ms",
                 "main_finger_accuracy", "centroid_level"} | extra,
                set(df.columns))
        self.assertEqual(set(tp["centroid_level"]), {"trial"})
        self.assertEqual(set(pc["centroid_level"]), {"participant"})
        self.assertEqual(set(gc["centroid_level"]), {"group"})
        self.assertEqual(set(pc["difficulty"]), {"all", "α", "β", "γ"})

    def test_empty_selection_yields_empty_frames_not_crash(self):
        ds = gt.export_datasets(gt.compute([], []))
        for df in ds.values():
            self.assertEqual(len(df), 0)


class TestColors(unittest.TestCase):
    def test_participant_shades_stay_in_band_and_unique(self):
        for n in (1, 2, 25, 200):
            lightness = [gt.participant_lightness(i, n) for i in range(n)]
            self.assertTrue(all(0.30 <= l <= 0.76 for l in lightness))  # never white/black
        hexes = [gt.participant_color("B", i, 25) for i in range(25)]
        self.assertEqual(len(set(hexes)), 25)  # distinct at a realistic N
        # No crash and valid hex even at an absurd N.
        for i in (0, 199):
            self.assertRegex(gt.participant_color("C", i, 200), r"^#[0-9a-f]{6}$")

    def test_same_participant_same_rank_across_conditions(self):
        self.assertEqual(gt.participant_lightness(3, 10),
                         gt.participant_lightness(3, 10))
        # First participant is lightest, later ones darker.
        self.assertGreater(gt.participant_lightness(0, 5),
                           gt.participant_lightness(4, 5))

    def test_level_shades_ordered_and_visible_on_white(self):
        self.assertGreater(gt.LEVEL_LIGHTNESS["alpha"], gt.LEVEL_LIGHTNESS["beta"])
        self.assertGreater(gt.LEVEL_LIGHTNESS["beta"], gt.LEVEL_LIGHTNESS["gamma"])
        self.assertLessEqual(gt.LEVEL_LIGHTNESS["alpha"], 0.75)  # not near-white
        for c in CONDITIONS:
            for lv in LEVELS:
                self.assertRegex(gt.level_color(c, lv), r"^#[0-9a-f]{6}$")

    def test_shade_clamps_out_of_range_lightness(self):
        self.assertEqual(gt.shade("#3a76c4", 2.0), "#ffffff")
        self.assertEqual(gt.shade("#3a76c4", -1.0), "#000000")


class TestRendering(unittest.TestCase):
    def _assert_renders(self, n_participants: int):
        participants = [f"P{i + 1:02d}" for i in range(n_participants)]
        data = gt.compute(schedules(n_participants), participants)
        figs = gt.build_figures(data)
        self.assertEqual(set(figs), {"group_tradeoff_2d",
                                     "group_tradeoff_by_difficulty_3d",
                                     "group_tradeoff_by_participant_3d"})
        xlims = [fig.axes[0].get_xlim() for fig in figs.values()]
        for xlim in xlims[1:]:  # shared x-range across all three figures
            self.assertEqual(xlim, xlims[0])
        for fig in figs.values():
            self.assertEqual(fig.axes[0].get_ylim(), (0.0, 105.0))
        return figs

    def test_renders_with_2_participants(self):
        self._assert_renders(2)

    def test_renders_with_12_participants(self):
        self._assert_renders(12)

    def test_all_toggle_combinations_render(self):
        data = gt.compute(schedules(2), ["P01", "P02"])
        for bits in range(16):
            for mode in ("ci", "sd"):
                gt.build_figures(data, options=gt.TradeoffOptions(
                    show_trial_points=bool(bits & 1),
                    show_participant_centroids=bool(bits & 2),
                    show_group_centroids=bool(bits & 4),
                    connect_b_to_c=bool(bits & 8),
                    error_bars=mode))


class TestExportedFilesReadable(unittest.TestCase):
    def test_tight_export_keeps_titles_labels_legends(self):
        data = gt.compute(schedules(3), ["P01", "P02", "P03"])
        figs = gt.build_figures(
            data, cond_titles={"A": "A (Key-only practice)",
                               "B": "B (Visual finger-cue guidance)",
                               "C": "C (Vibrotactile finger guidance)"})
        titles = {"group_tradeoff_2d": "Group speed–accuracy trade-off",
                  "group_tradeoff_by_difficulty_3d": "Speed–accuracy trade-off by difficulty",
                  "group_tradeoff_by_participant_3d": "Speed–accuracy trade-off by participant"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for slug, fig in figs.items():
                ax = fig.axes[0]
                self.assertEqual(ax.get_title(), titles[slug])
                self.assertIn("Mean RT of correct-key events (ms)", ax.get_xlabel())
                self.assertIn("Main Finger Accuracy (%)", ax.get_ylabel())
                self.assertTrue(fig.legends or ax.get_legend())
                # The export path: tight bbox includes every
                # artist (titles, labels, outside legends) by definition;
                # verify the files materialise at a readable size.
                fig.savefig(out / f"{slug}.png", dpi=300, bbox_inches="tight")
                fig.savefig(out / f"{slug}.svg", bbox_inches="tight")
                w, h = png_size(out / f"{slug}.png")
                self.assertGreater(w, 900)
                self.assertGreater(h, 900)
                self.assertGreater((out / f"{slug}.svg").stat().st_size, 1000)


try:  # The tab itself needs Qt; skip cleanly on a headless CI without it.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class TestTradeoffTabGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _build_window(self, n_participants: int):
        from app import group_analysis as ga
        from app.gui.group_analysis_window import GroupAnalysisWindow
        participants = [f"P{i + 1:02d}" for i in range(n_participants)]
        window = GroupAnalysisWindow()
        window._data = ga.GroupData(included=participants,
                                    trial_rows=schedules(n_participants))
        window._pc = ga.participant_condition_metrics(window._data.trial_rows)
        window._cells = ga.participant_cell_metrics(window._data.trial_rows)
        window._cond_titles = window._condition_titles(window._data.trial_rows)
        window._figures, window._datasets = {}, {}
        window._add_tradeoff_tab()
        return window

    def test_tab_renders_with_2_and_10_participants(self):
        for n in (2, 10):
            window = self._build_window(n)
            self.assertEqual(window.tabs.tabText(0), "Trade-off")
            self.assertEqual(
                set(window._figures),
                {"group_tradeoff_2d",
                 "group_tradeoff_by_difficulty_3d",
                 "group_tradeoff_by_participant_3d"})
            self.assertEqual(set(window._datasets),
                             {"group_tradeoff_trial_points",
                              "group_tradeoff_participant_centroids",
                              "group_tradeoff_group_centroids"})

    def test_toggles_rebuild_figures(self):
        window = self._build_window(2)
        fig_before = window._figures["group_tradeoff_2d"]
        window.tr_points_check.setChecked(False)  # dense-plot escape hatch
        fig_after = window._figures["group_tradeoff_2d"]
        self.assertIsNot(fig_before, fig_after)

    def test_errorbar_combo_switches_to_sd(self):
        window = self._build_window(2)
        window.tr_errbar_combo.setCurrentIndex(1)  # ±SD
        fig = window._figures["group_tradeoff_2d"]
        labels = [t.get_text() for t in fig.axes[0].get_legend().get_texts()]
        self.assertTrue(any("± SD" in l for l in labels))
        self.assertFalse(any("t-CI" in l for l in labels))


if __name__ == "__main__":
    unittest.main(verbosity=2)
