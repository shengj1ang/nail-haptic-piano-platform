"""Tests for the adhesion vibration comparison experiment
(validation_experiments/adhesion_vibration_comparison/).

The per-trial analysis is a pure function over recorded samples and the
comparison is a pure function over saved runs, so every case below runs
on synthetic signals with KNOWN amplitudes, onsets, harmonics and
spectra - no hardware, and never the real config.json or the real output
folder. Covered:

  * only the three legal adhesion methods are accepted, in any spelling
  * the drive always comes from haptic.lra, even when using=erm
  * one run saves one method (file naming + per-trial rows)
  * raw NPZ save -> reload reproduces the live run's numbers and traces
  * steady-state RMS / peaks / FFT fundamental / harmonics / THD are
    numerically correct on a known signal
  * the shared spectral helpers (amplitude scaling, THD, tolerance)
  * a not_settled trial keeps an EMPTY settling time, gets window-tail
    steady numbers, and is excluded from the steady-state statistics
  * waveform alignment (command vs onset) on trials with known onsets
  * several runs per method: run-level (per-mount) averaging, and the
    between-run / within-run standard deviations kept apart
  * the comparison refuses too few runs, a single method and mismatched
    parameters (named, with each run's value) - but ALLOWS any number of
    runs per method and any two of the three methods
  * relative vibration transfer: ratio / percent / dB formulas
  * the comparison's PNGs, CSV and meta are written and well-formed
  * the GUI window and its analysis dialog: fixed method choices,
    read-only LRA drive, run kwargs, the run list excluding comparison
    outputs, and the selection rules

Run from main/:  python test-script/test_adhesion_vibration.py
"""

import csv
import json
import math
import os
import statistics
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

from common import haptic_config as hc  # noqa: E402
from validation_experiments.acceleration_metrics import (  # noqa: E402
    PHASE_BASELINE,
    PHASE_VIBRATION,
    RawSampleRecorder,
    compute_baseline_magnitude,
    compute_vibration_spectrum,
    counts_to_ms2,
    harmonic_amplitudes,
    raw_path_for,
    total_harmonic_distortion,
)
from validation_experiments.adhesion_vibration_comparison import (  # noqa: E402
    adhesion_comparison as ac,
    adhesion_vibration as av,
)
from validation_experiments.motor_acc_delay_experiment import (  # noqa: E402
    motor_acc_delay as mad,
)

FS = 1344.0          # the rig's LIS3DH ODR (firmware >= v2.9.0)
G_COUNTS = 1000.0    # 1 g at +/-2 g, 1 count = 1 mg
NOISE_SD = 6.0       # per-axis rest noise (counts)


# ==========================================
# Synthetic signals
# ==========================================

def baseline_samples(n=1344, seed=0, t0=0.0):
    """A quiet (t, x, y, z) window: noise around rest, gravity on Z."""
    rng = np.random.default_rng(seed)
    t = t0 + np.arange(n) / FS
    return list(zip(t, rng.normal(0, NOISE_SD, n), rng.normal(0, NOISE_SD, n),
                    G_COUNTS + rng.normal(0, NOISE_SD, n)))


def drive_samples(n=2688, seed=1, amp=300.0, f0=224.0, onset_s=0.006,
                  tau=0.030, t0=2.0, harmonic2=0.10, decay=0.0):
    """A driven (t, x, y, z) window with a KNOWN onset, ring-up time
    constant, carrier amplitude and second-harmonic fraction. `decay` > 0
    makes the vibration keep decaying (a never-settling trial).
    Returns (samples, rel_times)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    env = np.where(t >= onset_s, 1.0 - np.exp(-(t - onset_s) / tau), 0.0)
    if decay:
        env = env * np.exp(-decay * t)
    wave = amp * env * (np.sin(2 * np.pi * f0 * t)
                        + harmonic2 * np.sin(2 * np.pi * 2 * f0 * t))
    x = wave + rng.normal(0, NOISE_SD, n)
    y = 0.4 * wave + rng.normal(0, NOISE_SD, n)
    z = G_COUNTS + rng.normal(0, NOISE_SD, n)
    samples = list(zip(t0 + t, x, y, z))
    return samples, t


def make_baseline(samples):
    """A mad.BaselineWindow from a quiet window, as collect_baseline
    would have measured it."""
    xs = np.array([s[1] for s in samples])
    ys = np.array([s[2] for s in samples])
    zs = np.array([s[3] for s in samples])
    means = (float(xs.mean()), float(ys.mean()), float(zs.mean()))
    deltas = mad.per_axis_deltas(samples, means)
    noise = mad.noise_stats(deltas, FS)
    return mad.BaselineWindow(
        samples=samples, means=means,
        magnitude_counts=compute_baseline_magnitude(samples),
        threshold_counts=max(mad.MIN_THRESHOLD,
                             mad.NOISE_MULT * noise.p95_counts),
        noise=noise)


def analyse(trial_id=1, method="Blu Tack", seed=1, amp=300.0, tau=0.030,
            harmonic2=0.10, decay=0.0, freq=224, drive_amp=64, notes=""):
    base = baseline_samples(seed=seed)
    drive, rel = drive_samples(seed=seed + 100, amp=amp, tau=tau,
                               harmonic2=harmonic2, decay=decay)
    return av.analyse_trial(trial_id, method, make_baseline(base), drive, rel,
                            command_time_s=2.0, vib_duration_s=2.0,
                            frequency_hz=freq, amp=drive_amp, motor_index=11,
                            acc_sensor_id=0, notes=notes)


def write_run(folder, method, stamp, seed0=0, amp=300.0, tau=0.030,
              harmonic2=0.10, freq=224, drive_amp=64, n_trials=3,
              vib_duration_s=2.0, notes=""):
    """A complete saved run - CSV + raw NPZ + meta - as run_experiment
    would have written it, from synthetic trials."""
    results = []
    recorder = RawSampleRecorder(run_id=av.run_stem(method, stamp),
                                 experiment="adhesion_vibration_comparison",
                                 sensor_id=0)
    for trial in range(1, n_trials + 1):
        base = baseline_samples(seed=seed0 + trial, t0=trial * 10.0)
        drive, rel = drive_samples(seed=seed0 + 100 + trial, amp=amp, tau=tau,
                                   harmonic2=harmonic2,
                                   t0=trial * 10.0 + 1.0)
        base_window = recorder.add_window(
            base, phase=PHASE_BASELINE, cell_id=trial, trial_id=trial,
            commanded_freq_hz=freq, commanded_amp=0, timestamps="epoch")
        recorder.add_window(drive, phase=PHASE_VIBRATION, cell_id=trial,
                            trial_id=trial, baseline_window_id=base_window,
                            commanded_freq_hz=freq, commanded_amp=drive_amp,
                            timestamps="epoch")
        results.append(av.analyse_trial(
            trial, method, make_baseline(base), drive, rel,
            command_time_s=drive[0][0], vib_duration_s=vib_duration_s,
            frequency_hz=freq, amp=drive_amp, motor_index=11,
            acc_sensor_id=0, notes=notes))
    csv_path = os.path.join(folder, f"{av.run_stem(method, stamp)}.csv")
    av.save_trials_csv(csv_path, results)
    recorder.save(raw_path_for(csv_path))
    av.save_meta(csv_path, av.png_path_for(csv_path), stamp, "test-fw",
                 method, results, freq, drive_amp, 11, 0, vib_duration_s,
                 1.0, n_trials, notes, raw_path=raw_path_for(csv_path))
    return csv_path, results


# =========================================================================
# 1. Only the three legal adhesion methods
# =========================================================================

class TestAdhesionMethods(unittest.TestCase):

    def test_the_three_methods_and_their_ids(self):
        self.assertEqual(av.ADHESION_METHODS,
                         ("Blu Tack", "Double-sided tape", "Cosmetic adhesive"))
        self.assertEqual(av.method_id("Blu Tack"), "blu_tack")
        self.assertEqual(av.method_id("Double-sided tape"),
                         "double_sided_tape")
        self.assertEqual(av.method_id("Cosmetic adhesive"), "cosmetic_adhesive")

    def test_spelling_variants_normalise_to_the_canonical_label(self):
        for variant in ("blu tack", "BLU_TACK", " Blu Tack "):
            self.assertEqual(av.normalise_method(variant), "Blu Tack")
        self.assertEqual(av.normalise_method("double-sided tape"),
                         "Double-sided tape")
        self.assertEqual(av.normalise_method("cosmetic_adhesive"),
                         "Cosmetic adhesive")

    def test_anything_else_is_rejected_not_added(self):
        for bad in ("Super glue", "", "blu", None, 3):
            with self.assertRaises(ValueError):
                av.normalise_method(bad)

    def test_superseded_names_still_load_as_the_same_method(self):
        """Runs recorded before the rename must come back as the current
        method, not as an unknown fourth one."""
        for old in ("Eyelash glue", "eyelash_glue", "eyelash adhesive",
                    "cosmetic eyelash adhesive"):
            self.assertEqual(av.normalise_method(old), "Cosmetic adhesive")
        # ...and they are not offered as choices any more.
        self.assertNotIn("Eyelash glue", av.ADHESION_METHODS)

    def test_a_legacy_run_csv_loads_under_the_current_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "adhesion_eyelash_glue_1700000000.csv")
            rows = [dict(trial_id=1, status="ok", adhesion_method="Eyelash glue",
                         adhesion_method_id="eyelash_glue", frequency_hz=224,
                         amp=64, motor_index=11, acc_sensor_id=0)]
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            loaded = av.load_results(path)
            self.assertEqual(loaded[0].adhesion_method, "Cosmetic adhesive")

    def test_run_experiment_rejects_an_unknown_method_before_hardware(self):
        with self.assertRaises(ValueError):
            av.run_experiment(adhesion_method="hot glue", interactive=False)


# =========================================================================
# 2. The drive always comes from haptic.lra
# =========================================================================

class TestFixedLraConfig(unittest.TestCase):
    """The experiment must read haptic.lra even when using=erm - the
    whole comparison depends on one identical drive."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="adhesion_cfg_")
        self.config_path = Path(self._tmp.name) / "config.json"
        self._real_path = hc.DEFAULT_CONFIG_PATH
        hc.DEFAULT_CONFIG_PATH = self.config_path
        hc.invalidate_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        hc.DEFAULT_CONFIG_PATH = self._real_path
        hc.invalidate_cache()
        self._tmp.cleanup()

    def test_reads_lra_block_even_when_using_erm(self):
        with open(self.config_path, "w") as f:
            json.dump({"haptic": {
                "using": "erm",
                "lra": {"default_frequency": 231, "default_amp": 70},
                "erm": {"default_frequency": 1500, "default_amp": 90},
            }}, f)
        hc.invalidate_cache()
        self.assertEqual(av.lra_frequency_hz(), 231)
        self.assertEqual(av.lra_amp(), 70)
        # ... and never the ERM's numbers.
        self.assertNotEqual(av.lra_frequency_hz(), 1500)
        self.assertNotEqual(av.lra_amp(), 90)

    def test_the_experiment_is_lra_only(self):
        self.assertEqual(av.ACTUATOR_TYPE, "LRA")
        self.assertEqual(av.MOTOR_INDEX, hc.ACTUATOR_MOTOR_PORTS[hc.LRA])


# =========================================================================
# 3-4. One run = one method; raw data save -> reload consistency
# =========================================================================

class TestRunOutputs(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="adhesion_run_")
        self.folder = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_file_names_carry_the_method_id_and_the_epoch_stamp(self):
        self.assertEqual(av.run_stem("Double-sided tape", 1700000000),
                         "adhesion_double_sided_tape_1700000000")

    def test_a_run_saves_exactly_one_method(self):
        csv_path, _results = write_run(self.folder, "Cosmetic adhesive",
                                       1700000000)
        self.assertIn("cosmetic_adhesive", os.path.basename(csv_path))
        with open(csv_path, newline="") as f:
            methods = {row["adhesion_method"] for row in csv.DictReader(f)}
        self.assertEqual(methods, {"Cosmetic adhesive"})
        meta = av.load_meta(csv_path)
        self.assertEqual(meta["adhesion_method"], "Cosmetic adhesive")

    def test_reloaded_summary_matches_the_live_run(self):
        csv_path, live = write_run(self.folder, "Blu Tack", 1700000000)
        reloaded = av.load_results(csv_path)
        self.assertEqual(len(reloaded), len(live))
        for a, b in zip(live, reloaded):
            self.assertEqual(a.trial_id, b.trial_id)
            self.assertEqual(a.status, b.status)
            self.assertEqual(a.steady_region_source, b.steady_region_source)
            # CSV cells round to a fixed number of places; compare there.
            self.assertAlmostEqual(a.steady_vector_rms_counts,
                                   b.steady_vector_rms_counts, places=2)
            self.assertAlmostEqual(a.peak_abs_counts, b.peak_abs_counts,
                                   places=2)
            self.assertAlmostEqual(a.onset_latency_from_command_ms,
                                   b.onset_latency_from_command_ms, places=3)
            self.assertAlmostEqual(a.spectrum.thd_ratio, b.spectrum.thd_ratio,
                                   places=4)

    def test_raw_npz_reload_restores_the_per_sample_traces(self):
        csv_path, live = write_run(self.folder, "Blu Tack", 1700000000)
        reloaded = av.load_results(csv_path)
        attached = av.attach_traces(csv_path, reloaded)
        self.assertEqual(attached, len(live))
        for a, b in zip(live, reloaded):
            self.assertEqual(len(a.trace), len(b.trace))
            # x/y/z are stored as integer counts (lossless for real data);
            # the synthetic floats round to the nearest count.
            np.testing.assert_allclose(a.trace.axes, b.trace.axes, atol=0.51)
            np.testing.assert_allclose(a.trace.rel_times, b.trace.rel_times,
                                       atol=1e-6)

    def test_render_csv_rebuilds_figure_and_stats_from_saved_files(self):
        csv_path, live = write_run(self.folder, "Blu Tack", 1700000000)
        out_png = os.path.join(self.folder, "rerender.png")
        stats = av.render_csv(csv_path, out_png)
        self.assertTrue(os.path.getsize(out_png) > 0)
        self.assertEqual(stats["adhesion_method"], "Blu Tack")
        self.assertAlmostEqual(stats["steady_rms_mean_ms2"],
                               av.run_stats(live)["steady_rms_mean_ms2"],
                               places=4)

    def test_summary_report_reads_back_the_run(self):
        csv_path, _live = write_run(self.folder, "Blu Tack", 1700000000,
                                    notes="thin layer")
        text = "\n".join(av.summary_report(csv_path))
        self.assertIn("Blu Tack", text)
        self.assertIn("Steady vector RMS", text)
        self.assertIn("THD", text)
        self.assertIn("thin layer", text)
        self.assertIn("ONE adhesion method", text)


# =========================================================================
# 5. Numerical correctness on a known signal
# =========================================================================

class TestTrialAnalysis(unittest.TestCase):
    """amp=300 on X plus 0.4*amp on Y, harmonic2=0.10. Expected steady
    figures (counts): per-axis sinusoid RMS = A/sqrt(2), quadrature over
    axes and harmonics."""

    @classmethod
    def setUpClass(cls):
        cls.r = analyse(amp=300.0, harmonic2=0.10)

    def test_trial_is_ok_and_settled(self):
        self.assertEqual(self.r.status, mad.STATUS_OK)
        self.assertEqual(self.r.steady_region_source, av.STEADY_FROM_SETTLING)

    def test_steady_vector_rms(self):
        # sqrt(rms_x^2 + rms_y^2 + rms_z^2); carrier+harmonic on X is
        # 300*sqrt(1+0.01)/sqrt(2), Y is 0.4 of that, Z is noise only.
        wave_rms = 300.0 * math.sqrt(1 + 0.10 ** 2) / math.sqrt(2)
        expected = math.sqrt(wave_rms ** 2 + (0.4 * wave_rms) ** 2
                             + 3 * NOISE_SD ** 2)
        self.assertAlmostEqual(self.r.steady_vector_rms_counts, expected,
                               delta=0.03 * expected)

    def test_peaks_absolute_robust_and_peak_to_peak(self):
        # |dev| peaks near the joint amplitude of X and Y.
        joint = 300.0 * 1.10 * math.sqrt(1 + 0.4 ** 2)
        self.assertLess(abs(self.r.peak_abs_counts - joint), 0.15 * joint)
        # The robust peak can never exceed the absolute one.
        self.assertLessEqual(self.r.peak_robust_p99_counts,
                             self.r.peak_abs_counts)
        self.assertGreater(self.r.peak_robust_p99_counts, 0.8 * joint)
        # X peak-to-peak ~ 2 * max|x|; dominated by the carrier.
        self.assertAlmostEqual(self.r.peak_to_peak_x_counts, 2 * 300.0 * 1.1,
                               delta=0.15 * 2 * 300.0)
        self.assertEqual(self.r.peak_to_peak_max_counts,
                         max(self.r.peak_to_peak_x_counts,
                             self.r.peak_to_peak_y_counts,
                             self.r.peak_to_peak_z_counts))

    def test_fundamental_dominant_and_harmonics(self):
        s = self.r.spectrum
        self.assertAlmostEqual(s.dominant_frequency_hz, 224.0, delta=1.5)
        # Quadrature amplitude over X and Y at f0: 300*sqrt(1+0.16).
        expected_a1 = 300.0 * math.sqrt(1 + 0.4 ** 2)
        self.assertAlmostEqual(s.drive_freq_amplitude_counts, expected_a1,
                               delta=0.05 * expected_a1)
        # Second harmonic at 10 % of the fundamental; higher ones absent.
        self.assertAlmostEqual(s.harmonic_amplitudes_counts[0],
                               0.10 * expected_a1,
                               delta=0.02 * expected_a1)

    def test_thd_matches_the_injected_harmonic_fraction(self):
        # THD = A2/A1 = 0.10 (higher harmonics are noise-level).
        self.assertAlmostEqual(self.r.spectrum.thd_ratio, 0.10, delta=0.015)
        self.assertAlmostEqual(self.r.spectrum.thd_percent, 10.0, delta=1.5)

    def test_onset_and_rise_are_measured(self):
        self.assertAlmostEqual(self.r.onset_latency_from_command_ms, 6.0,
                               delta=4.0)
        self.assertIsNotNone(self.r.rise_time_10_90_ms)
        # 10-90 % of 1-exp(-t/30ms) is ln(9)*tau ~ 66 ms.
        self.assertAlmostEqual(self.r.rise_time_10_90_ms,
                               math.log(9) * 30.0, delta=15.0)

    def test_whole_window_intensity_block_is_present(self):
        self.assertIsNotNone(self.r.intensity)
        # The whole window includes the quiet pre-onset part, so its RMS
        # is below the steady-state RMS.
        self.assertLess(self.r.intensity.vector_rms_counts,
                        self.r.steady_vector_rms_counts)


class TestSpectralHelpers(unittest.TestCase):
    """The shared spectral functions in acceleration_metrics."""

    def test_pure_sine_amplitude_is_recovered(self):
        t = np.arange(4096) / FS
        x = 100.0 * np.sin(2 * np.pi * 200.0 * t)
        samples = np.column_stack((x, np.zeros_like(x), np.zeros_like(x)))
        spectrum = compute_vibration_spectrum(samples, FS)
        found, amplitude = spectrum.amplitude_at(200.0)
        self.assertAlmostEqual(found, 200.0, delta=spectrum.resolution_hz)
        self.assertAlmostEqual(amplitude, 100.0, delta=2.0)

    def test_amplitude_outside_the_spectrum_is_none_not_zero(self):
        t = np.arange(1024) / FS
        samples = np.column_stack((np.sin(2 * np.pi * 100 * t),
                                   np.zeros(1024), np.zeros(1024)))
        spectrum = compute_vibration_spectrum(samples, FS)
        self.assertEqual(spectrum.amplitude_at(FS), (None, None))

    def test_thd_of_a_clean_sine_is_near_zero_and_none_without_fundamental(self):
        t = np.arange(4096) / FS
        clean = np.column_stack((100.0 * np.sin(2 * np.pi * 200.0 * t),
                                 np.zeros(4096), np.zeros(4096)))
        spectrum = compute_vibration_spectrum(clean, FS)
        self.assertLess(total_harmonic_distortion(spectrum, 200.0), 0.01)
        # No energy near 500 Hz -> the "fundamental" is noise-level, but
        # crucially a silent signal must give None, never 0 ("perfectly
        # clean").
        silent = compute_vibration_spectrum(np.zeros((4096, 3)), FS)
        self.assertIsNone(total_harmonic_distortion(silent, 200.0))

    def test_harmonics_report_where_they_looked_and_what_they_found(self):
        t = np.arange(4096) / FS
        x = (100.0 * np.sin(2 * np.pi * 200.0 * t)
             + 20.0 * np.sin(2 * np.pi * 400.0 * t))
        spectrum = compute_vibration_spectrum(
            np.column_stack((x, np.zeros(4096), np.zeros(4096))), FS)
        harmonics = harmonic_amplitudes(spectrum, 200.0, 3)
        self.assertEqual([h.order for h in harmonics], [1, 2, 3])
        self.assertAlmostEqual(harmonics[0].amplitude_counts, 100.0, delta=2)
        self.assertAlmostEqual(harmonics[1].amplitude_counts, 20.0, delta=2)
        self.assertAlmostEqual(total_harmonic_distortion(spectrum, 200.0),
                               0.20, delta=0.02)


# =========================================================================
# 6. not_settled trials are handled safely
# =========================================================================

class TestNotSettled(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # A vibration that keeps decaying fast never holds the steady
        # band: over the 200 ms hold window the envelope falls ~45 %,
        # far outside the ±20 % band.
        cls.r = analyse(decay=3.0, tau=0.010)

    def test_status_and_empty_settling_time(self):
        self.assertEqual(self.r.status, mad.STATUS_NOT_SETTLED)
        self.assertIsNone(self.r.settling_time_from_onset_ms)
        self.assertIsNone(self.r.stable_time_s)

    def test_steady_numbers_are_flagged_as_window_tail_fallback(self):
        self.assertEqual(self.r.steady_region_source, av.STEADY_FALLBACK)
        self.assertFalse(self.r.settled)
        # The fallback still yields numbers - flagged, not faked.
        self.assertIsNotNone(self.r.steady_vector_rms_counts)

    def test_excluded_from_steady_state_statistics(self):
        settled = analyse(trial_id=2, seed=7)
        stats = av.run_stats([self.r, settled])
        self.assertEqual(stats["n_not_settled"], 1)
        self.assertEqual(stats["n_fallback_steady_state"], 1)
        self.assertEqual(stats["n_settled_steady_state"], 1)
        # Steady series count only the settled trial...
        self.assertEqual(stats["steady_rms_n"], 1)
        self.assertAlmostEqual(stats["steady_rms_mean_ms2"],
                               settled.steady_vector_rms_ms2, places=9)
        # ...while the onset series keeps both trials.
        self.assertEqual(stats["onset_n"], 2)

    def test_csv_round_trip_keeps_the_empty_settling_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "adhesion_blu_tack_1700000000.csv")
            av.save_trials_csv(path, [self.r])
            reloaded = av.load_results(path)[0]
            self.assertIsNone(reloaded.settling_time_from_onset_ms)
            self.assertEqual(reloaded.steady_region_source,
                             av.STEADY_FALLBACK)

    def test_plot_and_comparison_survive_not_settled_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "plot.png")
            av.save_plot(png, [self.r], "Blu Tack", 224, 64, 11)
            self.assertTrue(os.path.getsize(png) > 0)


# =========================================================================
# 7. Waveform alignment
# =========================================================================

class TestAlignment(unittest.TestCase):

    @staticmethod
    def _method_data(onsets_s, folder, stamp=1700000000):
        results = []
        for trial, onset in enumerate(onsets_s, start=1):
            base = baseline_samples(seed=trial)
            drive, rel = drive_samples(seed=200 + trial, onset_s=onset,
                                       tau=0.020)
            results.append(av.analyse_trial(
                trial, "Blu Tack", make_baseline(base), drive, rel,
                command_time_s=2.0, vib_duration_s=2.0, frequency_hz=224,
                amp=64, motor_index=11, acc_sensor_id=0))
        csv_path = os.path.join(folder, f"{av.run_stem('Blu Tack', stamp)}.csv")
        av.save_trials_csv(csv_path, results)
        run = ac.RunData(info=ac.RunInfo(
            csv_path=csv_path, adhesion_method="Blu Tack", saved_at=stamp,
            n_trials=len(results), frequency_hz=224, amp=64, motor_index=11,
            acc_sensor_id=0, acc_interval_ms=1, vib_duration_s=2.0),
            results=results, stats=av.run_stats(results))
        group = ac.MethodGroup(method="Blu Tack", runs=[run])
        group.stats = ac.group_stats(group)
        return group

    def test_command_alignment_starts_at_zero_and_shows_the_late_rise(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._method_data([0.030, 0.030], tmp)
            envelope = ac.mean_envelope(data, ac.ALIGN_COMMAND)
            self.assertAlmostEqual(float(envelope.times_s[0]), 0.0, places=6)
            # At 15 ms (before the 30 ms onset) the envelope is at noise
            # level; well after it, it is near the plateau.
            early = np.interp(0.015, envelope.times_s, envelope.mean_counts)
            late = np.interp(0.300, envelope.times_s, envelope.mean_counts)
            self.assertLess(early, 0.25 * late)

    def test_onset_alignment_superimposes_trials_with_different_onsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._method_data([0.010, 0.060], tmp)
            aligned = ac.mean_envelope(data, ac.ALIGN_ONSET)
            self.assertEqual(aligned.n_trials, 2)
            # Aligned at each trial's own onset, both rises coincide: the
            # across-trial SD during the rise stays small relative to the
            # plateau. Command alignment must show a much larger spread.
            command = ac.mean_envelope(data, ac.ALIGN_COMMAND)
            rise = (aligned.times_s > 0.0) & (aligned.times_s < 0.05)
            rise_cmd = (command.times_s > 0.01) & (command.times_s < 0.06)
            self.assertLess(float(np.max(aligned.sd_counts[rise])),
                            0.5 * float(np.max(command.sd_counts[rise_cmd])))

    def test_unknown_alignment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._method_data([0.010], tmp)
            with self.assertRaises(ValueError):
                ac.mean_envelope(data, "resampled")


# =========================================================================
# 8. The comparison: selection rules and compatibility
# =========================================================================

class _ThreeRunCase(unittest.TestCase):
    """Three compatible synthetic runs, one per method, in a temp dir."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="adhesion_three_")
        cls.folder = cls._tmp.name
        cls.blu, _ = write_run(cls.folder, "Blu Tack", 1700000000,
                               seed0=0, amp=300.0)
        cls.tape, _ = write_run(cls.folder, "Double-sided tape", 1700000100,
                                seed0=500, amp=240.0, tau=0.045,
                                harmonic2=0.15)
        cls.glue, _ = write_run(cls.folder, "Cosmetic adhesive", 1700000200,
                                seed0=900, amp=330.0, tau=0.020,
                                harmonic2=0.05)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class TestSelectionRules(_ThreeRunCase):

    def test_a_valid_selection_passes(self):
        infos = [ac.run_info(p) for p in (self.blu, self.tape, self.glue)]
        self.assertEqual(ac.check_selection(infos), [])

    def test_two_methods_are_enough(self):
        """A comparison does not need all three methods - an adhesive
        that could not be mounted simply has no run."""
        infos = [ac.run_info(p) for p in (self.blu, self.tape)]
        self.assertEqual(ac.check_selection(infos), [])

    def test_several_runs_of_one_method_are_allowed(self):
        """Repeated runs of one adhesive are the point (each is a new
        mount), so duplicates must NOT be rejected."""
        blu2, _ = write_run(self.folder, "Blu Tack", 1700000300, seed0=1300)
        infos = [ac.run_info(p) for p in (self.blu, blu2, self.tape)]
        self.assertEqual(ac.check_selection(infos), [])

    def test_a_single_run_is_rejected(self):
        problems = ac.check_selection([ac.run_info(self.blu)])
        self.assertTrue(any("at least 2 runs" in p for p in problems))

    def test_one_method_only_is_rejected(self):
        blu2, _ = write_run(self.folder, "Blu Tack", 1700000600, seed0=2500)
        infos = [ac.run_info(p) for p in (self.blu, blu2)]
        problems = ac.check_selection(infos)
        self.assertTrue(any("2 different adhesion methods" in p
                            for p in problems))
        self.assertTrue(any("Blu Tack" in p for p in problems))
        with self.assertRaises(ValueError):
            ac.compare([self.blu, blu2], output_dir=self.folder)

    def test_mismatched_frequency_is_rejected_with_values(self):
        odd, _ = write_run(self.folder, "Cosmetic adhesive", 1700000400,
                           seed0=1700, freq=250)
        infos = [ac.run_info(p) for p in (self.blu, self.tape, odd)]
        problems = ac.check_selection(infos)
        message = "\n".join(problems)
        self.assertIn("frequency", message)
        self.assertIn("250", message)
        self.assertIn("224", message)
        with self.assertRaises(ValueError):
            ac.compare([self.blu, self.tape, odd], output_dir=self.folder)

    def test_mismatched_duration_is_rejected(self):
        odd, _ = write_run(self.folder, "Cosmetic adhesive", 1700000500,
                           seed0=2100, vib_duration_s=4.0)
        infos = [ac.run_info(p) for p in (self.blu, self.tape, odd)]
        self.assertTrue(any("duration" in p for p in
                            ac.check_selection(infos)))

    def test_nothing_is_written_when_the_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(ValueError):
                ac.compare([self.blu], output_dir=out)
            self.assertEqual(os.listdir(out), [])


# =========================================================================
# 9-10. The comparison's numbers and outputs
# =========================================================================

class TestComparison(_ThreeRunCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = tempfile.TemporaryDirectory(prefix="adhesion_out_")
        cls.summary = ac.compare([cls.blu, cls.tape, cls.glue],
                                 output_dir=cls.out.name)

    @classmethod
    def tearDownClass(cls):
        cls.out.cleanup()
        super().tearDownClass()

    def test_relative_transfer_formulas(self):
        groups = ac.load_groups([self.blu, self.tape, self.glue])
        relative = ac.relative_figures(groups, "Blu Tack")
        reference = groups["Blu Tack"].stats["steady_rms_mean_ms2"]
        for method in av.ADHESION_METHODS:
            figure = relative["steady_rms"][method]
            expected = groups[method].stats["steady_rms_mean_ms2"] / reference
            self.assertAlmostEqual(figure.ratio, expected, places=9)
            self.assertAlmostEqual(figure.percent_change,
                                   (expected - 1) * 100, places=6)
            self.assertAlmostEqual(figure.db, 20 * math.log10(expected),
                                   places=6)
        # The reference against itself is exactly 1 / 0 % / 0 dB.
        self.assertAlmostEqual(relative["steady_rms"]["Blu Tack"].ratio, 1.0)
        self.assertAlmostEqual(relative["steady_rms"]["Blu Tack"].db, 0.0)

    def test_relative_ratios_reflect_the_injected_amplitudes(self):
        # tape amp 240/300 -> ratio ~0.8; glue 330/300 -> ~1.1.
        rel = self.summary["relative"]["steady_rms"]
        self.assertAlmostEqual(rel["Double-sided tape"]["ratio"], 0.80,
                               delta=0.03)
        self.assertAlmostEqual(rel["Cosmetic adhesive"]["ratio"], 1.10,
                               delta=0.03)

    def test_the_naming_is_relative_transfer_not_transmissibility(self):
        with open(self.summary["meta_path"]) as f:
            meta = json.load(f)
        naming = meta["relative_transfer_definition"]["naming"]
        self.assertIn("NOT transmissibility", naming)
        report_text = "\n".join(self.summary["report"])
        self.assertIn("not absolute transmissibility", report_text)

    def test_all_output_files_are_written(self):
        for key in ("waveform_png", "spectrum_png", "metrics_png",
                    "csv_path", "meta_path"):
            path = self.summary[key]
            self.assertTrue(os.path.exists(path), key)
            self.assertGreater(os.path.getsize(path), 0, key)
        stem = f"adhesion_comparison_{self.summary['stamp']}"
        self.assertTrue(os.path.basename(
            self.summary["csv_path"]).startswith(stem))

    def test_comparison_csv_carries_trials_runs_and_summaries(self):
        with open(self.summary["csv_path"], newline="") as f:
            rows = list(csv.DictReader(f))
        scopes = {row["scope"] for row in rows}
        self.assertEqual(scopes, {"trial", "run", "summary"})
        methods = {row["adhesion_method"] for row in rows}
        self.assertEqual(methods, set(av.ADHESION_METHODS))
        summary_rows = [r for r in rows if r["scope"] == "summary"
                        and r["metric"] == "steady_rms"
                        and r["adhesion_method"] == "Double-sided tape"]
        self.assertEqual(len(summary_rows), 1)
        self.assertAlmostEqual(
            float(summary_rows[0]["ratio_to_reference"]),
            self.summary["relative"]["steady_rms"]["Double-sided tape"]["ratio"],
            places=5)

    def test_meta_records_inputs_drive_and_parameters(self):
        with open(self.summary["meta_path"]) as f:
            meta = json.load(f)
        self.assertEqual(len(meta["inputs"]), 3)
        self.assertEqual({i["adhesion_method"] for i in meta["inputs"]},
                         set(av.ADHESION_METHODS))
        self.assertEqual(meta["drive"]["frequency_hz"], 224)
        self.assertEqual(meta["reference_method"], "Blu Tack")
        self.assertIn("alignment", meta["analysis_parameters"])
        self.assertIn("steady_state_definition", meta["analysis_parameters"])
        self.assertEqual(meta["analysis_version"], ac.ANALYSIS_VERSION)

    def test_input_runs_are_not_modified(self):
        before = {p: (os.path.getsize(p), os.path.getmtime(p))
                  for p in (self.blu, self.tape, self.glue)}
        ac.compare([self.blu, self.tape, self.glue],
                   output_dir=self.out.name)
        for path, stamp in before.items():
            self.assertEqual((os.path.getsize(path), os.path.getmtime(path)),
                             stamp, path)

    def test_run_listing_excludes_comparison_outputs(self):
        # The comparison wrote its outputs into out.name; a comparison
        # CSV in the RUN folder must also never appear as a "run".
        comparison_csv = os.path.join(
            self.folder, "adhesion_comparison_1700009999.csv")
        with open(comparison_csv, "w") as f:
            f.write("scope\n")
        try:
            paths = av.run_csv_paths(self.folder)
            self.assertNotIn(comparison_csv, paths)
            self.assertTrue(all("adhesion_comparison_" not in p
                                for p in paths))
        finally:
            os.remove(comparison_csv)


# =========================================================================
# 10b. Several runs per method: run-level averaging and the two SDs
# =========================================================================

class TestMultipleRunsPerMethod(unittest.TestCase):
    """A method characterised by more than one mount. Two Blu Tack runs
    with deliberately different amplitudes (300 and 200 counts) stand for
    the same adhesive applied two different ways."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="adhesion_multi_")
        cls.folder = cls._tmp.name
        # 5 trials in the first mount, 3 in the second, so trial-pooling
        # and run-level averaging give visibly different answers.
        cls.blu_a, _ = write_run(cls.folder, "Blu Tack", 1700000000,
                                 seed0=0, amp=300.0, n_trials=5)
        cls.blu_b, _ = write_run(cls.folder, "Blu Tack", 1700000100,
                                 seed0=400, amp=200.0, n_trials=3)
        cls.tape, _ = write_run(cls.folder, "Double-sided tape", 1700000200,
                                seed0=800, amp=240.0, n_trials=4)
        cls.groups = ac.load_groups([cls.blu_a, cls.blu_b, cls.tape])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_group_holds_every_run_of_the_method(self):
        group = self.groups["Blu Tack"]
        self.assertEqual(group.n_runs, 2)
        self.assertEqual(group.stats["n_trials"], 8)
        self.assertEqual(sorted(group.stats["runs"]),
                         sorted(os.path.basename(p)
                                for p in (self.blu_a, self.blu_b)))

    def test_group_mean_averages_run_means_not_trials(self):
        """Each mount carries one weight: the 5-trial run must not
        outvote the 3-trial one."""
        series = av.SERIES_BY_PREFIX["steady_rms"]
        group = self.groups["Blu Tack"]
        run_means = group.run_means(series)
        self.assertEqual(len(run_means), 2)
        self.assertAlmostEqual(group.stats["steady_rms_mean_ms2"],
                               statistics.fmean(run_means), places=9)
        # A trial-pooled mean would sit closer to the 5-trial run; the
        # run-level mean must differ from it measurably here.
        trials = [v for _run, _trial, v in group.trial_values(series)]
        self.assertNotAlmostEqual(group.stats["steady_rms_mean_ms2"],
                                  statistics.fmean(trials), places=4)

    def test_between_and_within_run_sds_are_separate(self):
        group = self.groups["Blu Tack"]
        between = group.stats["steady_rms_sd_between_runs_ms2"]
        within = group.stats["steady_rms_sd_within_run_ms2"]
        self.assertIsNotNone(between)
        self.assertIsNotNone(within)
        # The two mounts differ by design (300 vs 200 counts), far more
        # than the trials inside either mount do.
        self.assertGreater(between, 5 * within)
        # The headline SD is the mount-to-mount one, and says so.
        self.assertEqual(group.stats["steady_rms_sd_basis"],
                         ac.SD_BETWEEN_RUNS)
        self.assertAlmostEqual(group.stats["steady_rms_sd_ms2"], between,
                               places=9)
        self.assertAlmostEqual(
            group.stats["steady_rms_cv_percent"],
            100 * between / group.stats["steady_rms_mean_ms2"], places=6)

    def test_a_single_run_group_falls_back_to_within_run_and_says_so(self):
        group = self.groups["Double-sided tape"]
        self.assertEqual(group.n_runs, 1)
        self.assertIsNone(group.stats["steady_rms_sd_between_runs_ms2"])
        self.assertEqual(group.stats["steady_rms_sd_basis"], ac.SD_WITHIN_RUN)
        self.assertAlmostEqual(group.stats["steady_rms_sd_ms2"],
                               group.stats["steady_rms_sd_within_run_ms2"],
                               places=9)

    def test_ratios_use_the_run_level_group_means(self):
        relative = ac.relative_figures(self.groups, "Double-sided tape")
        figure = relative["steady_rms"]["Blu Tack"]
        expected = (self.groups["Blu Tack"].stats["steady_rms_mean_ms2"]
                    / self.groups["Double-sided tape"].stats["steady_rms_mean_ms2"])
        self.assertAlmostEqual(figure.ratio, expected, places=9)

    def test_envelope_and_spectrum_pool_every_run(self):
        group = self.groups["Blu Tack"]
        envelope = ac.mean_envelope(group, ac.ALIGN_COMMAND)
        self.assertEqual(envelope.n_runs, 2)
        self.assertEqual(envelope.n_trials, 8)
        self.assertIsNotNone(ac.mean_spectrum(group))

    def test_outputs_record_the_runs_behind_every_mean(self):
        with tempfile.TemporaryDirectory() as out:
            summary = ac.compare([self.blu_a, self.blu_b, self.tape],
                                 reference_method="Double-sided tape",
                                 output_dir=out)
            self.assertEqual(summary["n_runs"],
                             {"Blu Tack": 2, "Double-sided tape": 1})
            with open(summary["meta_path"]) as f:
                meta = json.load(f)
            self.assertEqual(len(meta["inputs"]), 3)
            blu = meta["result"]["methods"]["Blu Tack"]
            self.assertEqual(blu["n_runs"], 2)
            self.assertEqual(len(blu["runs"]), 2)
            self.assertEqual(blu["series"]["steady_rms"]["sd_basis"],
                             ac.SD_BETWEEN_RUNS)
            # The per-run rows in the CSV back the summary rows up.
            with open(summary["csv_path"], newline="") as f:
                rows = list(csv.DictReader(f))
            run_rows = [r for r in rows if r["scope"] == "run"
                        and r["metric"] == "steady_rms"
                        and r["adhesion_method"] == "Blu Tack"]
            self.assertEqual(len(run_rows), 2)
            summary_row = [r for r in rows if r["scope"] == "summary"
                           and r["metric"] == "steady_rms"
                           and r["adhesion_method"] == "Blu Tack"][0]
            self.assertAlmostEqual(
                float(summary_row["mean"]),
                statistics.fmean(float(r["mean"]) for r in run_rows),
                places=6)
            self.assertEqual(summary_row["n_runs"], "2")

    def test_missing_reference_method_falls_back_and_says_so(self):
        with tempfile.TemporaryDirectory() as out:
            messages = []
            summary = ac.compare([self.blu_a, self.blu_b, self.tape],
                                 reference_method="Cosmetic adhesive",
                                 output_dir=out, log=messages.append)
            self.assertEqual(summary["reference_method"], "Blu Tack")
            self.assertTrue(any("No Cosmetic adhesive run selected" in m
                                for m in messages))


# =========================================================================
# 11. The GUI window
# =========================================================================

class TestAdhesionWindow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="adhesion_gui_")
        self._real_dir = av.OUTPUT_DIR
        av.OUTPUT_DIR = self._tmp.name
        self.addCleanup(setattr, av, "OUTPUT_DIR", self._real_dir)
        self.addCleanup(self._tmp.cleanup)

    def _window(self):
        from app.gui.validation_experiment_window import (
            AdhesionComparisonWindow)
        window = AdhesionComparisonWindow(None)
        self.addCleanup(window.close)
        return window

    def test_method_choices_are_exactly_the_three(self):
        window = self._window()
        options = [window.method_combo.itemText(i)
                   for i in range(window.method_combo.count())]
        self.assertEqual(tuple(options), av.ADHESION_METHODS)

    def test_defaults_and_ranges(self):
        window = self._window()
        self.assertEqual(window.trials_spin.value(), 5)
        self.assertEqual((window.trials_spin.minimum(),
                          window.trials_spin.maximum()), (1, 20))
        self.assertEqual(window.duration_spin.value(), 2.0)
        self.assertEqual((window.duration_spin.minimum(),
                          window.duration_spin.maximum()), (0.5, 10.0))
        self.assertEqual(window.motor_spin.value(),
                         hc.get_actuator_motor_port(hc.LRA))

    def test_drive_is_displayed_read_only_not_editable(self):
        window = self._window()
        # No frequency/amp spin boxes exist - the drive is a label.
        self.assertFalse(hasattr(window, "freq_spin"))
        self.assertFalse(hasattr(window, "amp_spin"))
        self.assertIn(str(av.lra_frequency_hz()), window.drive_label.text())
        self.assertIn(str(av.lra_amp()), window.drive_label.text())

    def test_run_kwargs_carry_the_selected_method_and_parameters(self):
        window = self._window()
        window.method_combo.setCurrentIndex(2)
        window.trials_spin.setValue(7)
        window.duration_spin.setValue(3.0)
        window.rest_spin.setValue(2.0)
        window.notes_edit.setText("  two layers  ")
        kwargs = window._extra_run_kwargs()
        self.assertEqual(kwargs, {"adhesion_method": "Cosmetic adhesive",
                                  "num_trials": 7, "vib_duration_s": 3.0,
                                  "rest_duration_s": 2.0,
                                  "notes": "two layers"})

    def test_gui_mentions_the_validity_protocol(self):
        window = self._window()
        text = window.SETUP_HINT + window._description_text()
        self.assertIn("same LRA", text)
        self.assertIn("curing time", text)

    def test_run_list_excludes_comparison_files(self):
        write_run(self._tmp.name, "Blu Tack", 1700000000)
        with open(os.path.join(self._tmp.name,
                               "adhesion_comparison_1700000001.csv"),
                  "w") as f:
            f.write("scope\n")
        window = self._window()
        listed = [window.run_combo.itemData(i)
                  for i in range(window.run_combo.count())]
        self.assertTrue(any(p and "blu_tack" in p for p in listed))
        self.assertFalse(any(p and "comparison" in p for p in listed))

    def test_runs_are_listed_newest_first_across_methods(self):
        """The file names put the METHOD before the stamp, so a plain
        string sort would order the picker by adhesive rather than by
        time - the newest run must still come first."""
        write_run(self._tmp.name, "Blu Tack", 1700000300, seed0=0)
        write_run(self._tmp.name, "Double-sided tape", 1700000100, seed0=500)
        write_run(self._tmp.name, "Cosmetic adhesive", 1700000200, seed0=900)
        window = self._window()
        listed = [os.path.basename(window.run_combo.itemData(i))
                  for i in range(window.run_combo.count())]
        self.assertEqual(listed, ["adhesion_blu_tack_1700000300.csv",
                                  "adhesion_cosmetic_adhesive_1700000200.csv",
                                  "adhesion_double_sided_tape_1700000100.csv"])
        # ...and the same order (reversed) underneath, for list_runs.
        self.assertEqual([r.saved_at
                          for r in ac.list_runs(self._tmp.name)],
                         [1700000300, 1700000200, 1700000100])

    def _dialog_with_four_runs(self):
        """Two Blu Tack mounts, one tape, one cosmetic adhesive. Rows are
        listed newest first, so: 0/1 = Blu Tack, 2 = tape, 3 = cosmetic."""
        from app.gui.validation_experiment_window import (
            AdhesionAnalysisDialog)
        write_run(self._tmp.name, "Blu Tack", 1700000400, seed0=0)
        write_run(self._tmp.name, "Blu Tack", 1700000300, seed0=300)
        write_run(self._tmp.name, "Double-sided tape", 1700000200, seed0=600)
        write_run(self._tmp.name, "Cosmetic adhesive", 1700000100, seed0=900)
        dialog = AdhesionAnalysisDialog()
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.table.rowCount(), 4)
        return dialog

    @staticmethod
    def _tick(dialog, *rows):
        from PySide6.QtCore import Qt
        for row in range(dialog.table.rowCount()):
            dialog.table.item(row, 0).setCheckState(
                Qt.CheckState.Checked if row in rows
                else Qt.CheckState.Unchecked)

    def test_analysis_dialog_needs_two_runs_over_two_methods(self):
        dialog = self._dialog_with_four_runs()
        self.assertFalse(dialog.analyse_btn.isEnabled())     # nothing ticked
        self._tick(dialog, 0)
        self.assertFalse(dialog.analyse_btn.isEnabled())     # one run
        self._tick(dialog, 0, 1)
        self.assertFalse(dialog.analyse_btn.isEnabled())     # one method only
        self.assertIn("2 different adhesion methods", dialog.status.text())
        self._tick(dialog, 0, 2)
        self.assertTrue(dialog.analyse_btn.isEnabled())      # two methods

    def test_analysis_dialog_accepts_several_runs_of_one_method(self):
        dialog = self._dialog_with_four_runs()
        self._tick(dialog, 0, 1, 2, 3)
        self.assertTrue(dialog.analyse_btn.isEnabled())
        # The status line says how many mounts each method contributes.
        self.assertIn("Blu Tack: 2 runs", dialog.status.text())
        self.assertIn("Double-sided tape: 1 run", dialog.status.text())
        # ...and warns that the single-mount methods have no spread.
        self.assertIn("single mount", dialog.status.text())

    def test_reference_combo_only_offers_selected_methods(self):
        dialog = self._dialog_with_four_runs()
        self._tick(dialog, 0, 2)          # Blu Tack + tape, no cosmetic
        model = dialog.reference_combo.model()
        enabled = {dialog.reference_combo.itemData(i)
                   for i in range(dialog.reference_combo.count())
                   if model.item(i).isEnabled()}
        self.assertEqual(enabled, {"Blu Tack", "Double-sided tape"})
        self.assertIn(dialog.reference_combo.currentData(), enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
