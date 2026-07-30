"""Unit tests for the validation experiments' TEXT statistics: the
per-experiment summary_report() functions (validation_experiments/
report.py plus each experiment module) and the saved-run picker the
launcher's validation windows use to load them.

Re-opening a saved run used to redraw only its chart; these cover the
other half - that the numbers behind the picture are regenerated from
the run's own CSV/meta, in the same wording and alignment the live run
prints, and that selecting a run in the picker loads exactly that run.

All tests work on throwaway files in a temp directory; nothing here
reads or writes the project's own saved experiment data.

Run from main/:  python test-script/test_validation_reports.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The run-picker tests drive real Qt windows, so they need a platform
# plugin that works without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")



class TestSummaryReports(unittest.TestCase):
    """Every validation experiment can rebuild its console-style
    statistics from a saved CSV, so re-opening a run shows the numbers
    and not only the chart."""

    def _delay_csv(self, directory):
        from validation_experiments.motor_acc_delay_experiment \
            import motor_acc_delay as m
        results = []
        for i in range(1, 4):
            results.append(m.TrialResult(
                trial_id=i, status=m.STATUS_OK, delay_ms=8.0 + i,
                baseline_magnitude_counts=1000.0, peak_axis_delta_counts=250.0,
                onset_ms=5.0 + i, settling_time_from_onset_ms=120.0 + i,
                stable_latency_from_command_ms=125.0 + i,
                steady_state_envelope_counts=200.0 + i,
                vib_duration_s=2.0, actuator_type="LRA"))
        path = os.path.join(directory, "delay_trials_1700000000.csv")
        m.save_trials_csv(path, results)
        return m, path, results

    def test_delay_report_matches_the_live_run_wording(self):
        with tempfile.TemporaryDirectory() as directory:
            m, path, results = self._delay_csv(directory)
            lines = m.summary_report(path)
            text = "\n".join(lines)
            for label in ("Vibration onset latency:",
                          "Settling time from onset:",
                          "Stable latency from command:",
                          "Detection-level crossing:"):
                self.assertIn(label, text)
            self.assertIn("mean", text)
            self.assertIn("median", text)
            self.assertIn("SD", text)
            self.assertIn("range", text)
            self.assertIn("(n=3)", text)
            # The per-trial table is there too.
            self.assertIn("trial", text)
            self.assertIn("delay_trials_1700000000.csv", text)

    def test_delay_report_numbers_match_the_computed_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            m, path, results = self._delay_csv(directory)
            stats = m.delay_stats(results)
            line = [ln for ln in m.summary_report(path)
                    if ln.startswith("Vibration onset latency:")][0]
            self.assertIn(f"{stats['onset_mean_ms']:.2f}", line)
            self.assertIn(f"{stats['onset_median_ms']:.2f}", line)

    def test_delay_report_without_a_meta_file_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            m, path, _ = self._delay_csv(directory)
            text = "\n".join(m.summary_report(path))
            self.assertIn("no .meta.json", text)

    def test_report_line_format_is_the_documented_one(self):
        from validation_experiments import report
        line = report.stats_line("Vibration onset latency:", {
            "onset_mean_ms": 6.14, "onset_median_ms": 5.88,
            "onset_sd_ms": 1.20, "onset_min_ms": 4.11,
            "onset_max_ms": 7.96, "onset_n": 10}, "onset")
        self.assertEqual(
            line,
            "Vibration onset latency:     mean     6.14 ms, median     "
            "5.88 ms, SD    1.20 ms, range 4.11-7.96 ms (n=10)")

    def test_report_line_is_none_for_an_empty_series(self):
        from validation_experiments import report
        self.assertIsNone(report.stats_line("x:", {}, "onset"))

    def test_stamp_and_time_come_from_the_file_name(self):
        from validation_experiments import report
        self.assertEqual(report.stamp_from_path("a/delay_trials_1700000000.csv"),
                         1700000000)
        self.assertIsNone(report.stamp_from_path("a/no_stamp_here.csv"))
        self.assertNotEqual(report.run_time_text("delay_trials_1700000000.csv",
                                                 None), "unknown time")

    def test_amplitude_report_states_the_recommendation_and_band(self):
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_amplitude_sweep as m
        with tempfile.TemporaryDirectory() as directory:
            results = [
                m.StepResult(amp=a, n_samples=100,
                             vector_rms_counts=a * 4.0,
                             vector_rms_ms2=a * 0.04,
                             legacy_magnitude_rms_counts=a * 2.0,
                             legacy_magnitude_rms_ms2=a * 0.02)
                for a in (4, 32, 64, 96, 128)]
            path = os.path.join(directory, "amp_sweep_1700000000.csv")
            m.save_csv(path, results)
            text = "\n".join(m.summary_report(path))
            self.assertIn("Amplitude-sweep statistics", text)
            self.assertIn("amp", text)
            self.assertTrue("Recommended cue amp" in text
                            or m.UNCALIBRATED_LABEL in text)

    def test_frequency_report_states_the_resonance(self):
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_frequency_sweep as m
        with tempfile.TemporaryDirectory() as directory:
            results = [
                m.StepResult(sweep_pass="coarse", frequency_hz=f,
                             n_samples=100,
                             vector_rms_counts=100.0 - abs(f - 224),
                             vector_rms_ms2=(100.0 - abs(f - 224)) / 100,
                             legacy_magnitude_rms_counts=50.0,
                             legacy_magnitude_rms_ms2=0.5,
                             peak_magnitude_delta_counts=10.0)
                for f in (200, 224, 250)]
            path = os.path.join(directory, "sweep_1700000000.csv")
            m.save_csv(path, results)
            text = "\n".join(m.summary_report(path))
            self.assertIn("Resonant frequency: 224 Hz", text)
            self.assertIn("Coarse pass (3 steps)", text)

    def test_spectrogram_report_prints_a_matrix_for_a_small_grid(self):
        from validation_experiments.actuator_spectrogram import (
            actuator_spectrogram as m)
        with tempfile.TemporaryDirectory() as directory:
            results = [
                m.CellResult(freq_hz=f, amp=a, n_samples=50,
                             vector_rms_counts=a * 1.0,
                             vector_rms_ms2=a * 0.01,
                             legacy_magnitude_rms_counts=a * 0.5,
                             legacy_magnitude_rms_ms2=a * 0.005)
                for f in (100, 200) for a in (0, 128, 255)]
            path = os.path.join(directory, "spectrogram_1700000000.csv")
            m.save_csv(path, results)
            text = "\n".join(m.summary_report(path))
            self.assertIn("Intensity map", text)
            self.assertIn("amp 128", text)
            self.assertIn("Strongest vibration at freq=100 Hz, amp=255", text)

    def test_spectrogram_report_falls_back_for_a_large_grid(self):
        from validation_experiments.actuator_spectrogram import (
            actuator_spectrogram as m)
        with tempfile.TemporaryDirectory() as directory:
            results = [
                m.CellResult(freq_hz=f, amp=a, n_samples=50,
                             vector_rms_counts=a * 1.0,
                             vector_rms_ms2=a * 0.01)
                for f in range(50, 550, 25) for a in range(0, 256, 8)]
            self.assertGreater(len(results), m.MAX_REPORT_CELLS)
            path = os.path.join(directory, "spectrogram_1700000000.csv")
            m.save_csv(path, results)
            text = "\n".join(m.summary_report(path))
            self.assertIn("Strongest amp per frequency", text)
            self.assertNotIn("Intensity map", text)

    def test_every_experiment_module_exposes_a_report(self):
        from validation_experiments.actuator_spectrogram import (
            actuator_spectrogram)
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_amplitude_sweep, lra_frequency_sweep
        from validation_experiments.motor_acc_delay_experiment \
            import motor_acc_delay
        for module in (actuator_spectrogram, lra_amplitude_sweep,
                       lra_frequency_sweep, motor_acc_delay):
            self.assertTrue(callable(getattr(module, "summary_report", None)),
                            module.__name__)


class TestRunPicker(unittest.TestCase):
    """The saved-run list every validation window offers, and the
    statistics it prints when a run is selected."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from validation_experiments.motor_acc_delay_experiment \
            import motor_acc_delay as m
        self.module = m
        self._tmp = tempfile.TemporaryDirectory(prefix="run_picker_test_")
        self._real_dir = m.OUTPUT_DIR
        m.OUTPUT_DIR = self._tmp.name
        self.addCleanup(setattr, m, "OUTPUT_DIR", self._real_dir)
        self.addCleanup(self._tmp.cleanup)

    def _write_run(self, stamp: int, n_trials: int = 3):
        m = self.module
        results = [
            m.TrialResult(trial_id=i, status=m.STATUS_OK, delay_ms=8.0,
                          baseline_magnitude_counts=1000.0,
                          peak_axis_delta_counts=250.0, onset_ms=5.0 + i,
                          settling_time_from_onset_ms=120.0,
                          stable_latency_from_command_ms=125.0,
                          steady_state_envelope_counts=200.0,
                          vib_duration_s=2.0, actuator_type="LRA")
            for i in range(1, n_trials + 1)]
        path = os.path.join(self._tmp.name, f"delay_trials_{stamp}.csv")
        m.save_trials_csv(path, results)
        m.save_plot(os.path.join(self._tmp.name, f"delay_summary_{stamp}.png"),
                    results, "LRA", 11)
        return path

    def _window(self):
        from app.gui.validation_experiment_window import MotorAccDelayWindow
        window = MotorAccDelayWindow(None)
        self.addCleanup(window.close)
        return window

    def test_saved_runs_are_detected_and_listed_newest_first(self):
        older = self._write_run(1700000000)
        newer = self._write_run(1700009999)
        window = self._window()
        self.assertEqual(window.run_combo.count(), 2)
        self.assertEqual(window.run_combo.itemData(0), newer)
        self.assertEqual(window.run_combo.itemData(1), older)

    def test_entries_name_the_run_and_its_parameters(self):
        self._write_run(1700000000)
        window = self._window()
        label = window.run_combo.itemText(0)
        self.assertIn("delay_trials_1700000000.csv", label)
        self.assertIn("2023-", label)   # the epoch stamp, as local time

    def test_an_empty_output_folder_says_so_instead_of_failing(self):
        window = self._window()
        self.assertEqual(window.run_combo.count(), 1)
        self.assertIn("no saved runs", window.run_combo.itemText(0))
        self.assertFalse(window.run_combo.isEnabled())

    def test_opening_the_window_prints_the_latest_run_statistics(self):
        self._write_run(1700000000)
        window = self._window()
        self.assertIn("Vibration onset latency:", window.log_view.toPlainText())

    def test_selecting_a_run_loads_it_and_prints_its_statistics(self):
        self._write_run(1700000000)
        older = self._write_run(1600000000, n_trials=5)
        window = self._window()
        before = window.log_view.toPlainText()
        window.run_combo.setCurrentIndex(window.run_combo.findData(older))
        self.assertEqual(window._current_csv, older)
        added = window.log_view.toPlainText()[len(before):]
        self.assertIn("delay_trials_1600000000.csv", added)
        self.assertIn("(n=5)", added)

    def test_show_statistics_reprints_for_the_displayed_run(self):
        path = self._write_run(1700000000)
        window = self._window()
        before = len(window.log_view.toPlainText())
        window._show_statistics()
        added = window.log_view.toPlainText()[before:]
        self.assertIn(os.path.basename(path), added)
        self.assertIn("Detection-level crossing:", added)

    def test_refreshing_the_list_does_not_reload_the_chart(self):
        self._write_run(1700000000)
        window = self._window()
        current = window._current_csv
        before = len(window.log_view.toPlainText())
        self._write_run(1700005555)
        window._populate_run_list()          # what opening the popup does
        self.assertEqual(window.run_combo.count(), 2)
        self.assertEqual(window._current_csv, current)
        self.assertEqual(len(window.log_view.toPlainText()), before)

    def test_a_browsed_file_outside_the_folder_is_shown_in_the_list(self):
        self._write_run(1700000000)
        window = self._window()
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = os.path.join(elsewhere, "delay_trials_1650000000.csv")
            m = self.module
            m.save_trials_csv(outside, [
                m.TrialResult(trial_id=1, status=m.STATUS_OK, delay_ms=8.0,
                              baseline_magnitude_counts=1000.0,
                              peak_axis_delta_counts=250.0, onset_ms=5.0,
                              settling_time_from_onset_ms=120.0,
                              stable_latency_from_command_ms=125.0,
                              vib_duration_s=2.0, actuator_type="LRA")])
            window._load_run(outside)
            self.assertEqual(window._current_csv, outside)
            self.assertEqual(window.run_combo.currentData(), outside)
            self.assertIn("outside this folder", window.run_combo.currentText())

    def test_a_finished_run_is_added_to_the_list_and_selected(self):
        path = self._write_run(1700000000)
        window = self._window()
        newer = self._write_run(1700007777)
        window._on_succeeded({"png_path": os.path.join(
            self._tmp.name, "delay_summary_1700007777.png"),
            "csv_path": newer, "n_ok": 3, "n_trials": 3})
        self.assertEqual(window.run_combo.currentData(), newer)
        self.assertEqual(window.run_combo.count(), 2)


# =========================================================================
# The shared full-size chart preview
# =========================================================================

class TestPlotPreviewFit(unittest.TestCase):
    """Opening a chart must already be fitted to the window.

    The preview's scroll area has no real geometry until the window has
    been shown, so a fit computed before that scales the chart wrongly -
    which used to leave every newly opened chart needing a manual "Fit to
    window" click."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.gui.validation_experiment_window import (
            close_shared_plot_preview)
        self._tmp = tempfile.TemporaryDirectory(prefix="preview_test_")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(close_shared_plot_preview)

    def _png(self, name: str, width_in: float, height_in: float) -> str:
        import matplotlib.pyplot as plt
        path = os.path.join(self._tmp.name, name)
        figure = plt.figure(figsize=(width_in, height_in))
        figure.savefig(path, dpi=150)
        plt.close(figure)
        return path

    def _open(self, png_path: str):
        from app.gui.validation_experiment_window import (
            _PlotView, shared_plot_preview)
        view = _PlotView()
        self.addCleanup(view.deleteLater)
        view.show_png(png_path)
        self.assertTrue(view.open_full_size())
        self.app.processEvents()
        return view, shared_plot_preview()

    def test_a_chart_opens_already_fitted(self):
        _view, preview = self._open(self._png("large.png", 13, 11))
        self.assertTrue(preview._fit)
        # The applied zoom must be the fit for the REAL viewport, not for
        # the placeholder one the window had before it was shown.
        self.assertAlmostEqual(preview._zoom, preview._fit_zoom(), places=6)
        self.assertIn("(fit)", preview.info.text())

    def test_a_small_chart_is_not_blown_up_past_its_own_resolution(self):
        _view, preview = self._open(self._png("small.png", 2, 1.5))
        self.assertAlmostEqual(preview._zoom, 1.0, places=6)

    def test_switching_to_another_panels_chart_refits(self):
        self._open(self._png("large.png", 13, 11))
        _view, preview = self._open(self._png("other.png", 6, 9))
        self.assertTrue(preview._fit)
        self.assertAlmostEqual(preview._zoom, preview._fit_zoom(), places=6)

    def test_a_manual_zoom_survives_the_same_panel_re_rendering(self):
        view, preview = self._open(self._png("large.png", 13, 11))
        preview.set_zoom(1.0)
        self.assertFalse(preview._fit)
        view.show_png(self._png("large2.png", 13, 11))   # same owner
        self.app.processEvents()
        self.assertFalse(preview._fit)
        self.assertAlmostEqual(preview._zoom, 1.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
