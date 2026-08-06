"""Unit tests for validation_experiments/acceleration_metrics.py (the
shared vibration-intensity layer behind every accelerometer experiment)
and for the metric plumbing of the experiments that use it.

Covers the properties the two metrics are supposed to have - gravity
rejection, rotation invariance, exact agreement with the project's
historical legacy formula, correct sinusoid amplitudes, a single
counts -> m/s^2 conversion - plus the round trip through the raw-sample
store and the old-CSV compatibility rules.

Run from main/:  python test-script/test_acceleration_metrics.py
"""

import csv
import json
import math
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from validation_experiments.acceleration_metrics import (  # noqa: E402
    DEFAULT_METRIC,
    METRIC_LEGACY_MAGNITUDE_RMS,
    METRIC_VECTOR_RMS,
    MS2_PER_COUNT,
    PHASE_BASELINE,
    PHASE_VIBRATION,
    RawSampleRecorder,
    available_metrics,
    compute_acceleration_metrics,
    compute_axis_statistics,
    compute_baseline_magnitude,
    compute_demeaned_vector_rms,
    compute_legacy_magnitude_rms,
    counts_to_ms2,
    get_metric_value,
    load_raw_acceleration_samples,
    metric_axis_label,
    metrics_available_in,
    recompute_window_metrics,
    select_metric,
)
from validation_experiments.actuator_spectrogram import (  # noqa: E402
    actuator_spectrogram as spectro,
)
from validation_experiments.lra_resonance_intensity_calibration import (  # noqa: E402
    lra_amplitude_sweep,
    lra_frequency_sweep,
)
from validation_experiments.motor_acc_delay_experiment import (  # noqa: E402
    motor_acc_delay,
)

G_COUNTS = 1000.0     # 1 g at the LIS3DH's +/-2 g HR scale (1 count = 1 mg)


# ==========================================
# Signal helpers
# ==========================================

def still_with_gravity(n=400, gravity_axis=2, noise=0.0, seed=0,
                       quantize=False):
    """A motionless sensor: constant 1 g on one axis, zero on the others
    (plus optional Gaussian noise). quantize=True rounds to whole counts,
    the way the LIS3DH actually reports."""
    rng = np.random.default_rng(seed)
    samples = np.zeros((n, 3))
    samples[:, gravity_axis] = G_COUNTS
    if noise:
        samples += rng.normal(0.0, noise, samples.shape)
    if quantize:
        samples = np.rint(samples)
    return [tuple(row) for row in samples]


def vibration(n=1000, fs=1000.0, freq=224.0, amps=(30.0, 0.0, 0.0),
              offsets=(0.0, 0.0, G_COUNTS), phases=(0.0, 0.0, 0.0),
              quantize=False):
    """A sinusoidal vibration on each axis, on top of a static offset."""
    t = np.arange(n) / fs
    cols = [off + amp * np.sin(2 * math.pi * freq * t + ph)
            for amp, off, ph in zip(amps, offsets, phases)]
    samples = np.column_stack(cols)
    if quantize:
        samples = np.rint(samples)
    return [tuple(row) for row in samples]


def with_times(samples, fs=1000.0, t0=1_700_000_000.0):
    """Turn (x, y, z) samples into the (t, x, y, z) form the collectors
    produce, with epoch-second timestamps."""
    return [(t0 + i / fs, x, y, z) for i, (x, y, z) in enumerate(samples)]


def rotate(samples, axis="z", degrees=37.0):
    """Rigidly rotate a whole sample set - a different mounting pose of
    the same physical vibration."""
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    if axis == "z":
        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    elif axis == "y":
        R = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    else:
        R = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    return [tuple(row) for row in np.asarray(samples, dtype=float) @ R.T]


def legacy_reference(samples, baseline_magnitude):
    """The project's ORIGINAL formula, transcribed straight from the
    pre-refactor sweep scripts (math/statistics, no numpy) - the
    reference the shared implementation must reproduce exactly."""
    import statistics
    mags = [math.sqrt(x * x + y * y + z * z) for x, y, z in
            [(s[-3], s[-2], s[-1]) for s in samples]]
    return math.sqrt(statistics.fmean(
        [(m - baseline_magnitude) ** 2 for m in mags]))


# ==========================================
# The two metrics
# ==========================================

class TestVectorRms(unittest.TestCase):

    def test_still_sensor_under_gravity_reads_zero(self):
        """A motionless sensor holding 1 g must read ~0 vibration, no
        matter which way gravity points."""
        for axis in (0, 1, 2):
            samples = still_with_gravity(gravity_axis=axis)
            self.assertAlmostEqual(compute_demeaned_vector_rms(samples), 0.0,
                                   places=9)
            # ... and the raw magnitude is the full 1 g, so the metric is
            # genuinely removing the static component, not the signal.
            self.assertAlmostEqual(compute_baseline_magnitude(samples),
                                   G_COUNTS, places=6)

    def test_still_sensor_with_noise_reads_only_the_noise(self):
        noise_sd = 2.0
        samples = still_with_gravity(n=20000, noise=noise_sd, seed=7)
        value = compute_demeaned_vector_rms(samples)
        # Three independent axes of SD-2 noise -> sqrt(3) * 2 counts.
        self.assertAlmostEqual(value, math.sqrt(3) * noise_sd, delta=0.1)

    def test_rotation_invariance(self):
        """The same physical vibration measured in a rotated mounting
        pose must give the same vector RMS."""
        samples = vibration(amps=(30.0, 12.0, 5.0), offsets=(0.0, 0.0, G_COUNTS),
                            phases=(0.0, 1.1, 2.3))
        reference = compute_demeaned_vector_rms(samples)
        for axis, degrees in (("z", 37.0), ("y", 90.0), ("x", 12.5),
                              ("z", 180.0)):
            rotated = rotate(samples, axis=axis, degrees=degrees)
            self.assertAlmostEqual(compute_demeaned_vector_rms(rotated),
                                   reference, places=6,
                                   msg=f"rotation {degrees}° about {axis}")

    def test_legacy_metric_misses_vibration_perpendicular_to_gravity(self):
        """The blind spot that motivates the new metric. Two vibrations of
        IDENTICAL energy, one along gravity and one across it: the vector
        RMS scores them equal, the legacy |a|-based metric barely sees the
        perpendicular one (|a| = sqrt(g² + v²) ≈ g + v²/2g)."""
        along = vibration(amps=(0.0, 0.0, 40.0), offsets=(0.0, 0.0, G_COUNTS))
        across = vibration(amps=(40.0, 0.0, 0.0), offsets=(0.0, 0.0, G_COUNTS))

        self.assertAlmostEqual(compute_demeaned_vector_rms(along),
                               compute_demeaned_vector_rms(across), places=6)

        legacy_along = compute_legacy_magnitude_rms(
            along, compute_baseline_magnitude(along))
        legacy_across = compute_legacy_magnitude_rms(
            across, compute_baseline_magnitude(across))
        self.assertGreater(legacy_along, 10 * legacy_across)

    def test_three_axis_sinusoid_value(self):
        """A sinusoid of amplitude A has RMS A/sqrt(2) per axis, so three
        axes give sqrt((Ax^2 + Ay^2 + Az^2)/2)."""
        ax, ay, az = 30.0, 12.0, 5.0
        # An exact whole number of cycles makes the closed form exact.
        samples = vibration(n=1000, fs=1000.0, freq=100.0,
                            amps=(ax, ay, az), offsets=(0.0, 0.0, G_COUNTS),
                            phases=(0.0, 1.1, 2.3))
        expected = math.sqrt((ax ** 2 + ay ** 2 + az ** 2) / 2.0)
        self.assertAlmostEqual(compute_demeaned_vector_rms(samples), expected,
                               places=6)

    def test_equals_quadrature_sum_of_axis_rms(self):
        samples = vibration(amps=(30.0, 12.0, 5.0), phases=(0.0, 1.1, 2.3))
        axes = compute_axis_statistics(samples)
        self.assertAlmostEqual(axes.vector_rms_counts,
                               compute_demeaned_vector_rms(samples), places=9)
        # The per-axis means are the static offsets that were removed.
        self.assertAlmostEqual(axes.mean_z_counts, G_COUNTS, delta=0.5)


class TestLegacyMetric(unittest.TestCase):

    def test_matches_the_projects_original_formula(self):
        """Bit-for-bit agreement with the pre-refactor implementation, so
        old and new results stay comparable."""
        for amps in ((40.0, 0.0, 0.0), (0.0, 0.0, 25.0), (10.0, 8.0, 6.0)):
            samples = vibration(amps=amps, offsets=(0.0, 0.0, G_COUNTS))
            baseline = still_with_gravity(n=300)
            baseline_magnitude = compute_baseline_magnitude(baseline)
            self.assertAlmostEqual(
                compute_legacy_magnitude_rms(samples, baseline_magnitude),
                legacy_reference(samples, baseline_magnitude), places=9,
                msg=f"amps={amps}")

    def test_accepts_timestamped_samples_identically(self):
        samples = vibration(amps=(40.0, 3.0, 2.0))
        stamped = with_times(samples)
        self.assertAlmostEqual(compute_demeaned_vector_rms(samples),
                               compute_demeaned_vector_rms(stamped), places=9)
        self.assertAlmostEqual(compute_legacy_magnitude_rms(samples, 1000.0),
                               compute_legacy_magnitude_rms(stamped, 1000.0),
                               places=9)


class TestUnits(unittest.TestCase):

    def test_counts_to_ms2(self):
        # LIS3DH HR +/-2 g: 1 count = 1 mg = 0.00980665 m/s^2.
        self.assertAlmostEqual(MS2_PER_COUNT, 0.00980665, places=12)
        self.assertAlmostEqual(counts_to_ms2(1000.0), 9.80665, places=9)
        self.assertAlmostEqual(counts_to_ms2(0.0), 0.0, places=12)
        self.assertAlmostEqual(counts_to_ms2(50.0), 50.0 * MS2_PER_COUNT,
                               places=12)

    def test_metrics_convert_exactly_once(self):
        samples = vibration(amps=(40.0, 0.0, 0.0))
        m = compute_acceleration_metrics(samples, G_COUNTS)
        self.assertAlmostEqual(m.vector_rms_ms2,
                               m.vector_rms_counts * MS2_PER_COUNT, places=12)
        self.assertAlmostEqual(m.legacy_magnitude_rms_ms2,
                               m.legacy_magnitude_rms_counts * MS2_PER_COUNT,
                               places=12)

    def test_custom_scale_is_honoured(self):
        samples = vibration(amps=(40.0, 0.0, 0.0))
        m = compute_acceleration_metrics(samples, G_COUNTS, ms2_per_count=0.5)
        self.assertAlmostEqual(m.vector_rms_ms2, m.vector_rms_counts * 0.5,
                               places=12)


class TestMetricSelection(unittest.TestCase):

    def test_default_is_the_demeaned_vector_rms(self):
        self.assertEqual(DEFAULT_METRIC, METRIC_VECTOR_RMS)

    def test_get_metric_value_reads_both_units(self):
        samples = vibration(amps=(40.0, 0.0, 0.0))
        m = compute_acceleration_metrics(samples, G_COUNTS)
        self.assertAlmostEqual(get_metric_value(m, METRIC_VECTOR_RMS, "counts"),
                               m.vector_rms_counts, places=12)
        self.assertAlmostEqual(
            get_metric_value(m, METRIC_LEGACY_MAGNITUDE_RMS, "ms2"),
            m.legacy_magnitude_rms_ms2, places=12)

    def test_old_column_names_map_onto_the_legacy_metric_only(self):
        """A pre-refactor CSV row: the legacy metric is readable under its
        old column names, the vector RMS is reported as unavailable
        rather than invented."""
        old_row = {"freq_hz": "100", "amp": "64", "rms_delta_counts": "50.0",
                   "rms_ms2": "0.49", "peak_delta_counts": "120",
                   "baseline_mag": "1000", "n_samples": "100"}
        self.assertAlmostEqual(
            get_metric_value(old_row, METRIC_LEGACY_MAGNITUDE_RMS, "counts"),
            50.0, places=9)
        self.assertIsNone(get_metric_value(old_row, METRIC_VECTOR_RMS))
        self.assertEqual(metrics_available_in([old_row]),
                         [METRIC_LEGACY_MAGNITUDE_RMS])
        # Asking for the vector RMS on such data falls back, explicitly.
        self.assertEqual(select_metric([old_row], METRIC_VECTOR_RMS),
                         METRIC_LEGACY_MAGNITUDE_RMS)

    def test_available_metrics_depends_on_raw_samples(self):
        self.assertEqual(available_metrics(has_raw_samples=False),
                         [METRIC_LEGACY_MAGNITUDE_RMS])
        self.assertEqual(sorted(available_metrics(has_raw_samples=True)),
                         sorted([METRIC_VECTOR_RMS,
                                 METRIC_LEGACY_MAGNITUDE_RMS]))

    def test_axis_labels_name_the_metric_and_the_unit(self):
        self.assertEqual(metric_axis_label(METRIC_VECTOR_RMS),
                         "Demeaned 3-axis vector RMS acceleration (m/s²)")
        self.assertEqual(metric_axis_label(METRIC_LEGACY_MAGNITUDE_RMS),
                         "Legacy magnitude RMS acceleration (m/s²)")
        self.assertIn("counts",
                      metric_axis_label(METRIC_VECTOR_RMS, unit="counts"))


# ==========================================
# Raw-sample store
# ==========================================

class TestRawSampleRoundTrip(unittest.TestCase):

    def _record(self, path):
        recorder = RawSampleRecorder(run_id="test_run", experiment="unit_test",
                                     sensor_id=3)
        expected = []
        for cell_id, (freq, amp, ax) in enumerate([(224, 64, 30.0),
                                                   (224, 128, 55.0),
                                                   (300, 64, 8.0)]):
            # Whole counts, as the LIS3DH reports them - the raw store is
            # lossless for integer counts, which is what real runs write.
            baseline = with_times(still_with_gravity(n=120, noise=2.0,
                                                     seed=cell_id,
                                                     quantize=True))
            base_id = recorder.add_window(baseline, phase=PHASE_BASELINE,
                                          cell_id=cell_id,
                                          commanded_freq_hz=freq,
                                          commanded_amp=0, timestamps="epoch")
            vib = with_times(vibration(n=500, freq=float(freq),
                                       amps=(ax, ax / 3, 0.0), quantize=True))
            recorder.add_window(vib, phase=PHASE_VIBRATION, cell_id=cell_id,
                                baseline_window_id=base_id,
                                commanded_freq_hz=freq, commanded_amp=amp,
                                sweep_pass="coarse", timestamps="epoch")
            expected.append((freq, amp, vib,
                             compute_baseline_magnitude(baseline)))
        recorder.save(path)
        return expected

    def test_save_load_preserves_samples_and_both_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.raw_acc.npz")
            expected = self._record(path)

            raw = load_raw_acceleration_samples(path)
            self.assertEqual(raw.run_id, "test_run")
            self.assertEqual(raw.ms2_per_count, MS2_PER_COUNT)
            self.assertEqual(len(raw), 3 * (120 + 500))

            recomputed = recompute_window_metrics(raw)
            self.assertEqual(len(recomputed), 3)
            for window, (freq, amp, vib, baseline) in zip(recomputed, expected):
                # Identity survived the round trip.
                self.assertEqual(window.commanded_freq_hz, float(freq))
                self.assertEqual(window.commanded_amp, float(amp))
                self.assertEqual(window.sweep_pass, "coarse")
                self.assertEqual(window.sensor_id, 3)
                # ... and so did the numbers: both metrics recomputed from
                # the file match the ones computed live.
                live = compute_acceleration_metrics(vib, baseline)
                self.assertAlmostEqual(window.metrics.vector_rms_counts,
                                       live.vector_rms_counts, places=6)
                self.assertAlmostEqual(
                    window.metrics.legacy_magnitude_rms_counts,
                    live.legacy_magnitude_rms_counts, places=6)
                self.assertAlmostEqual(window.metrics.vector_rms_ms2,
                                       live.vector_rms_ms2, places=9)

    def test_raw_samples_are_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.raw_acc.npz")
            recorder = RawSampleRecorder(run_id="r", experiment="unit_test")
            samples = with_times(vibration(n=64, amps=(40.0, 9.0, 3.0),
                                           quantize=True))
            recorder.add_window(samples, phase=PHASE_VIBRATION, cell_id=0,
                                timestamps="epoch")
            recorder.save(path)

            raw = load_raw_acceleration_samples(path)
            back = raw.samples_for_window(0)
            np.testing.assert_array_equal(back[:, 1], [s[1] for s in samples])
            np.testing.assert_array_equal(back[:, 2], [s[2] for s in samples])
            np.testing.assert_array_equal(back[:, 3], [s[3] for s in samples])
            np.testing.assert_allclose(back[:, 0], [s[0] for s in samples])

    def test_baseline_linkage_reproduces_the_legacy_metric(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.raw_acc.npz")
            expected = self._record(path)
            raw = load_raw_acceleration_samples(path)
            for window, (_, _, _, baseline) in zip(
                    recompute_window_metrics(raw), expected):
                self.assertAlmostEqual(
                    window.metrics.baseline_magnitude_counts, baseline,
                    places=6)


# ==========================================
# Experiment plumbing (metric switching end to end)
# ==========================================

def synthetic_cells():
    """Spectrogram cells over a small (freq, amp) grid whose two metrics
    peak at DIFFERENT cells, so a metric switch is observable.

    The trick is orientation: gravity sits on z, and the strong cell
    vibrates purely across x (perpendicular to gravity), where the legacy
    |a|-based metric is only second-order sensitive; the other cell
    vibrates weakly but ALONG gravity, which the legacy metric flatters.
    """
    freqs, amps = [100, 200], [64, 128]
    strong = {(200, 128): (60.0, 0.0, 0.0),   # big, perpendicular to gravity
              (100, 64): (0.0, 0.0, 25.0),    # small, along gravity
              (100, 128): (0.0, 0.0, 12.0),
              (200, 64): (6.0, 0.0, 0.0)}
    results = []
    for freq in freqs:
        for amp in amps:
            samples = vibration(n=800, fs=1000.0, freq=float(freq),
                                amps=strong[(freq, amp)],
                                offsets=(0.0, 0.0, G_COUNTS))
            baseline = still_with_gravity(n=200)
            metrics = compute_acceleration_metrics(
                samples, compute_baseline_magnitude(baseline))
            results.append(spectro.CellResult.from_metrics(freq, amp, metrics))
    return freqs, amps, results


class TestSpectrogramMetricSwitching(unittest.TestCase):

    def test_the_two_metrics_pick_different_peaks(self):
        _, _, results = synthetic_cells()
        vector_peak = spectro.peak_cell(results, METRIC_VECTOR_RMS)
        legacy_peak = spectro.peak_cell(results, METRIC_LEGACY_MAGNITUDE_RMS)
        self.assertEqual((vector_peak.freq_hz, vector_peak.amp), (200, 128))
        self.assertEqual((legacy_peak.freq_hz, legacy_peak.amp), (100, 64))

    def test_matrix_follows_the_selected_metric(self):
        freqs, amps, results = synthetic_cells()
        vector = spectro.build_matrix(freqs, amps, results, METRIC_VECTOR_RMS)
        legacy = spectro.build_matrix(freqs, amps, results,
                                      METRIC_LEGACY_MAGNITUDE_RMS)
        self.assertEqual(np.unravel_index(np.nanargmax(vector), vector.shape),
                         (1, 1))
        self.assertEqual(np.unravel_index(np.nanargmax(legacy), legacy.shape),
                         (0, 0))

    def test_render_switches_peak_and_colorbar_label(self):
        """The closed loop: save a run's CSV, re-render it under each
        metric, and check the reported peak and the colour-bar label both
        follow the choice."""
        freqs, amps, results = synthetic_cells()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "spectrogram_1700000000.csv")
            spectro.save_csv(csv_path, results)

            summaries = {}
            for metric in (METRIC_VECTOR_RMS, METRIC_LEGACY_MAGNITUDE_RMS):
                png = str(Path(tmp) / f"{metric}.png")
                summaries[metric] = spectro.render_csv(csv_path, png,
                                                       metric=metric)
                self.assertTrue(Path(png).exists())

            self.assertEqual(summaries[METRIC_VECTOR_RMS]["peak_freq_hz"], 200)
            self.assertEqual(summaries[METRIC_VECTOR_RMS]["peak_amp"], 128)
            self.assertEqual(
                summaries[METRIC_LEGACY_MAGNITUDE_RMS]["peak_freq_hz"], 100)
            self.assertEqual(
                summaries[METRIC_LEGACY_MAGNITUDE_RMS]["peak_amp"], 64)

            # The colour-bar wording is derived from the same metric name
            # the peak was taken with, so a saved map can never mislabel
            # which metric it shows.
            self.assertNotEqual(metric_axis_label(METRIC_VECTOR_RMS),
                                metric_axis_label(METRIC_LEGACY_MAGNITUDE_RMS))

    def test_csv_round_trip_keeps_both_metrics(self):
        _, _, results = synthetic_cells()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "spectrogram_1700000000.csv")
            spectro.save_csv(csv_path, results)
            loaded = spectro.load_results(csv_path)
            self.assertEqual(len(loaded), len(results))
            for original, back in zip(results, loaded):
                self.assertAlmostEqual(back.vector_rms_counts,
                                       original.vector_rms_counts, places=6)
                self.assertAlmostEqual(back.legacy_magnitude_rms_counts,
                                       original.legacy_magnitude_rms_counts,
                                       places=6)
            self.assertEqual(sorted(spectro.available_metrics_for(csv_path)),
                             sorted([METRIC_VECTOR_RMS,
                                     METRIC_LEGACY_MAGNITUDE_RMS]))

    def test_erm_and_lra_have_independent_ranges_and_rest_times(self):
        erm = spectro.type_defaults("ERM")
        lra = spectro.type_defaults("LRA")
        self.assertEqual((erm["freq_min"], erm["freq_max"]), (50, 5000))
        self.assertEqual((lra["freq_min"], lra["freq_max"]), (0, 350))
        self.assertEqual(spectro.rest_s_for("ERM"), 2.0)
        self.assertEqual(spectro.rest_s_for("LRA"), 0.15)

    def test_duration_estimate_uses_the_selected_actuator_rest(self):
        measure_s = 2.0
        erm = spectro.estimated_duration_s(
            "Coarse", measure_s=measure_s, motor_type="ERM")
        lra = spectro.estimated_duration_s(
            "Coarse", measure_s=measure_s, motor_type="LRA")

        def expected(motor_type):
            defaults = spectro.type_defaults(motor_type)
            freq_step, amp_step = spectro.steps_for("Coarse")
            freqs = spectro.freq_values(
                freq_step, defaults["freq_min"], defaults["freq_max"])
            amps = spectro.amp_values(
                amp_step, defaults["amp_min"], defaults["amp_max"])
            per_cell = (spectro.SETTLE_S + measure_s
                        + spectro.rest_s_for(motor_type))
            return len(freqs) * (spectro.BASELINE_S + len(amps) * per_cell)

        self.assertAlmostEqual(erm, expected("ERM"))
        self.assertAlmostEqual(lra, expected("LRA"))


class TestOldCsvCompatibility(unittest.TestCase):
    """A pre-refactor CSV must still plot - under the legacy metric only,
    with the vector RMS explicitly reported as unavailable."""

    OLD_SPECTROGRAM_ROWS = [
        {"freq_hz": 100, "amp": 64, "rms_delta_counts": 20.0,
         "rms_ms2": 20.0 * MS2_PER_COUNT, "peak_delta_counts": 60.0,
         "baseline_mag": 1000.0, "n_samples": 400},
        {"freq_hz": 100, "amp": 128, "rms_delta_counts": 55.0,
         "rms_ms2": 55.0 * MS2_PER_COUNT, "peak_delta_counts": 150.0,
         "baseline_mag": 1000.0, "n_samples": 400},
        {"freq_hz": 200, "amp": 64, "rms_delta_counts": 33.0,
         "rms_ms2": 33.0 * MS2_PER_COUNT, "peak_delta_counts": 90.0,
         "baseline_mag": 1000.0, "n_samples": 400},
        {"freq_hz": 200, "amp": 128, "rms_delta_counts": 41.0,
         "rms_ms2": 41.0 * MS2_PER_COUNT, "peak_delta_counts": 110.0,
         "baseline_mag": 1000.0, "n_samples": 400},
    ]

    def _write_old_csv(self, path):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=list(self.OLD_SPECTROGRAM_ROWS[0].keys()))
            writer.writeheader()
            writer.writerows(self.OLD_SPECTROGRAM_ROWS)

    def test_old_csv_offers_legacy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "spectrogram_1600000000.csv")
            self._write_old_csv(csv_path)
            self.assertEqual(spectro.available_metrics_for(csv_path),
                             [METRIC_LEGACY_MAGNITUDE_RMS])
            loaded = spectro.load_results(csv_path)
            self.assertIsNone(loaded[0].vector_rms_counts)
            self.assertAlmostEqual(loaded[0].legacy_magnitude_rms_counts, 20.0)

    def test_old_csv_still_renders_under_the_legacy_metric(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "spectrogram_1600000000.csv")
            self._write_old_csv(csv_path)
            png = str(Path(tmp) / "old.png")
            summary = spectro.render_csv(csv_path, png,
                                         metric=METRIC_LEGACY_MAGNITUDE_RMS)
            self.assertTrue(Path(png).exists())
            self.assertEqual(summary["peak_freq_hz"], 100)
            self.assertEqual(summary["peak_amp"], 128)

    def test_requesting_the_vector_metric_falls_back_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "spectrogram_1600000000.csv")
            self._write_old_csv(csv_path)
            png = str(Path(tmp) / "old_vector.png")
            summary = spectro.render_csv(csv_path, png,
                                         metric=METRIC_VECTOR_RMS)
            # It renders (the legacy data is real), but the summary states
            # which metric was actually used and which were possible.
            self.assertEqual(summary["metric"], METRIC_LEGACY_MAGNITUDE_RMS)
            self.assertEqual(summary["requested_metric"], METRIC_VECTOR_RMS)
            self.assertEqual(summary["available_metrics"],
                             [METRIC_LEGACY_MAGNITUDE_RMS])

    def test_old_lra_sweep_csvs_load_under_their_old_column_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            freq_csv = str(Path(tmp) / "sweep_1600000000.csv")
            with open(freq_csv, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "sweep_pass", "frequency_hz", "rms_delta", "peak_delta",
                    "baseline_mag", "n_samples"])
                writer.writeheader()
                writer.writerow({"sweep_pass": "coarse", "frequency_hz": 220,
                                 "rms_delta": 90.0, "peak_delta": 250.0,
                                 "baseline_mag": 1000.0, "n_samples": 130})
                writer.writerow({"sweep_pass": "coarse", "frequency_hz": 224,
                                 "rms_delta": 111.1, "peak_delta": 298.0,
                                 "baseline_mag": 1000.0, "n_samples": 130})
            rows = lra_frequency_sweep.load_results(freq_csv)
            self.assertAlmostEqual(rows[1].legacy_magnitude_rms_counts, 111.1)
            self.assertIsNone(rows[1].vector_rms_counts)
            self.assertEqual(lra_frequency_sweep.available_metrics_for(freq_csv),
                             [METRIC_LEGACY_MAGNITUDE_RMS])
            png = str(Path(tmp) / "freq.png")
            summary = lra_frequency_sweep.render_csv(
                freq_csv, png, metric=METRIC_VECTOR_RMS)
            self.assertEqual(summary["resonance_hz"], 224)
            self.assertEqual(summary["metric"], METRIC_LEGACY_MAGNITUDE_RMS)

            amp_csv = str(Path(tmp) / "amp_sweep_1600000000.csv")
            with open(amp_csv, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "amp", "rms_delta_counts", "rms_ms2", "peak_delta_counts",
                    "baseline_mag", "n_samples"])
                writer.writeheader()
                for amp, counts in ((60, 40.0), (64, 50.0), (68, 58.0)):
                    writer.writerow({"amp": amp, "rms_delta_counts": counts,
                                     "rms_ms2": counts * MS2_PER_COUNT,
                                     "peak_delta_counts": counts * 3,
                                     "baseline_mag": 1000.0, "n_samples": 130})
            png = str(Path(tmp) / "amp.png")
            summary = lra_amplitude_sweep.render_csv(amp_csv, png)
            self.assertEqual(summary["metric"], METRIC_LEGACY_MAGNITUDE_RMS)
            # 64 counts * 0.00980665 = 0.49 m/s^2, closest to the 0.5 target.
            self.assertEqual(summary["recommended_amp"], 64)


class TestLraSweepMetricSwitching(unittest.TestCase):

    def _amp_rows(self):
        """Amplitude steps whose two metrics disagree about the amp that
        lands on the 0.5 m/s² target: one step vibrates across gravity
        (the legacy metric under-reads it), another along it."""
        rows = []
        for amp, amps_xyz in ((32, (0.0, 0.0, 51.0)),
                              (64, (51.0, 0.0, 0.0)),
                              (96, (80.0, 0.0, 0.0))):
            samples = vibration(n=800, fs=1000.0, freq=224.0, amps=amps_xyz,
                                offsets=(0.0, 0.0, G_COUNTS))
            metrics = compute_acceleration_metrics(samples, G_COUNTS)
            rows.append(lra_amplitude_sweep.StepResult.from_metrics(amp,
                                                                    metrics))
        return rows

    def test_recommendation_follows_the_metric(self):
        rows = self._amp_rows()
        vector = lra_amplitude_sweep.recommended_cue_amp(rows,
                                                         METRIC_VECTOR_RMS)
        legacy = lra_amplitude_sweep.recommended_cue_amp(
            rows, METRIC_LEGACY_MAGNITUDE_RMS)
        self.assertNotEqual(vector.amp, legacy.amp)
        # The vector RMS sees the two 51-count sinusoids as equal energy
        # and picks one of them; the legacy metric only "sees" the one
        # aligned with gravity.
        self.assertEqual(legacy.amp, 32)

    def test_plot_and_round_trip_under_both_metrics(self):
        rows = self._amp_rows()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "amp_sweep_1700000000.csv")
            lra_amplitude_sweep.save_csv(csv_path, rows)
            for metric in (METRIC_VECTOR_RMS, METRIC_LEGACY_MAGNITUDE_RMS):
                png = str(Path(tmp) / f"amp_{metric}.png")
                summary = lra_amplitude_sweep.render_csv(csv_path, png,
                                                         metric=metric)
                self.assertTrue(Path(png).exists())
                self.assertEqual(summary["metric"], metric)

    def test_frequency_sweep_resonance_and_plot_under_both_metrics(self):
        rows = []
        for freq, amps_xyz in ((220, (0.0, 0.0, 40.0)),
                               (224, (70.0, 0.0, 0.0)),
                               (228, (0.0, 0.0, 20.0))):
            samples = vibration(n=800, fs=1000.0, freq=float(freq),
                                amps=amps_xyz, offsets=(0.0, 0.0, G_COUNTS))
            metrics = compute_acceleration_metrics(samples, G_COUNTS)
            rows.append(lra_frequency_sweep.StepResult.from_metrics(
                "coarse", freq, metrics))
        self.assertEqual(
            lra_frequency_sweep.resonance_step(rows,
                                               METRIC_VECTOR_RMS).frequency_hz,
            224)
        self.assertEqual(
            lra_frequency_sweep.resonance_step(
                rows, METRIC_LEGACY_MAGNITUDE_RMS).frequency_hz, 220)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "sweep_1700000000.csv")
            lra_frequency_sweep.save_csv(csv_path, rows)
            for metric, expected_hz in ((METRIC_VECTOR_RMS, 224),
                                        (METRIC_LEGACY_MAGNITUDE_RMS, 220)):
                png = str(Path(tmp) / f"freq_{metric}.png")
                summary = lra_frequency_sweep.render_csv(csv_path, png,
                                                         metric=metric)
                self.assertTrue(Path(png).exists())
                self.assertEqual(summary["resonance_hz"], expected_hz)
                self.assertEqual(summary["metric"], metric)


class TestCueTargetBands(unittest.TestCase):
    """Each intensity metric has its OWN target cue band.

    The two metrics are different rulers - on this rig the same drive
    reads ~0.5 m/s² under the legacy magnitude RMS and ~2.7 m/s² under
    the demeaned vector RMS - so sharing one band made switching the plot
    metric move the recommendation from amp~52 to amp~4. These tests pin
    down that the band, the recommendation, the plot and the meta all
    switch together, and that an uncalibrated metric borrows nothing."""

    #: Measured on the rig (data/.../amp_sweep_*.csv): the amp -> (legacy,
    #: vector) response, which is what the two bands are calibrated
    #: against. Both metrics should select an amp in the low 50s.
    RESPONSE = {
        40: (0.360, 2.222), 44: (0.407, 2.329), 48: (0.448, 2.547),
        52: (0.502, 2.631), 56: (0.553, 2.875), 60: (0.621, 3.073),
        64: (0.690, 3.224), 68: (0.967, 3.857),
        # the low end, where a mis-scaled band used to send the vector
        # metric's recommendation
        4: (0.030, 0.510), 8: (0.045, 0.670), 12: (0.060, 0.780),
    }

    def _measured_rows(self):
        rows = []
        for amp in sorted(self.RESPONSE):
            legacy_ms2, vector_ms2 = self.RESPONSE[amp]
            rows.append(lra_amplitude_sweep.StepResult(
                amp=amp, n_samples=130,
                legacy_magnitude_rms_counts=legacy_ms2 / MS2_PER_COUNT,
                legacy_magnitude_rms_ms2=legacy_ms2,
                vector_rms_counts=vector_ms2 / MS2_PER_COUNT,
                vector_rms_ms2=vector_ms2,
                baseline_magnitude_counts=G_COUNTS,
                peak_magnitude_delta_counts=200.0))
        return rows

    @contextmanager
    def _captured_figure(self):
        """Keep save_plot's figure alive so its legend can be read."""
        figures = []
        with mock.patch.object(lra_amplitude_sweep.plt, "close",
                               figures.append):
            yield figures
        for fig in figures:
            plt.close(fig)

    def _legend_labels(self, rows, metric):
        with tempfile.TemporaryDirectory() as tmp:
            png = str(Path(tmp) / "plot.png")
            with self._captured_figure() as figures:
                lra_amplitude_sweep.save_plot(
                    png, rows,
                    lra_amplitude_sweep.recommended_cue_amp(rows, metric),
                    metric=metric)
            ax = figures[0].axes[0]
            return (list(ax.get_legend_handles_labels()[1]),
                    ax.get_title())

    # -- the definitions themselves --------------------------------------

    def test_each_metric_has_its_own_band(self):
        legacy = lra_amplitude_sweep.cue_target(METRIC_LEGACY_MAGNITUDE_RMS)
        vector = lra_amplitude_sweep.cue_target(METRIC_VECTOR_RMS)
        self.assertEqual(legacy.band_ms2, (0.4, 0.6))
        self.assertEqual(legacy.target_ms2, 0.5)
        self.assertEqual(vector.band_ms2, (2.4, 3.0))
        self.assertEqual(vector.target_ms2, 2.7)
        self.assertNotEqual(legacy.band_ms2, vector.band_ms2)
        for target in (legacy, vector):
            self.assertTrue(target.calibrated)
            self.assertTrue(target.source)

    def test_calibration_status_is_recorded_per_metric(self):
        self.assertEqual(
            lra_amplitude_sweep.cue_target(
                METRIC_LEGACY_MAGNITUDE_RMS).calibration_status,
            lra_amplitude_sweep.CALIBRATION_HISTORICAL)
        self.assertEqual(
            lra_amplitude_sweep.cue_target(
                METRIC_VECTOR_RMS).calibration_status,
            lra_amplitude_sweep.CALIBRATION_PROVISIONAL)

    def test_bands_are_defined_in_exactly_one_place(self):
        """No module-level band constant survives for code to reach past
        cue_target() and grab the 'wrong metric's' numbers."""
        for stale in ("TARGET_BAND_MS2", "TARGET_MS2"):
            self.assertFalse(hasattr(lra_amplitude_sweep, stale),
                             f"{stale} is back - bands must live only in "
                             "CUE_TARGETS/cue_target()")

    # -- the reported bug -------------------------------------------------

    def test_both_metrics_select_the_same_drive_on_measured_data(self):
        """The regression: with each metric on its own band, the two agree
        about the physical operating point (amp ~52). Sharing the legacy
        band sent the vector metric to the bottom of the sweep."""
        rows = self._measured_rows()
        legacy = lra_amplitude_sweep.recommended_cue_amp(
            rows, METRIC_LEGACY_MAGNITUDE_RMS)
        vector = lra_amplitude_sweep.recommended_cue_amp(rows,
                                                         METRIC_VECTOR_RMS)
        self.assertEqual(legacy.amp, 52)
        self.assertEqual(vector.amp, 52)
        # ...and specifically NOT the smallest amp in the sweep.
        self.assertNotEqual(vector.amp, min(self.RESPONSE))

    def test_in_band_amps_use_the_metrics_own_band(self):
        rows = self._measured_rows()
        legacy = [r.amp for r in lra_amplitude_sweep.in_band(
            rows, METRIC_LEGACY_MAGNITUDE_RMS)]
        vector = [r.amp for r in lra_amplitude_sweep.in_band(
            rows, METRIC_VECTOR_RMS)]
        self.assertEqual(legacy, [44, 48, 52, 56])
        self.assertEqual(vector, [48, 52, 56])

    # -- switching the metric switches everything -------------------------

    def test_switching_metric_switches_curve_band_amp_legend_and_meta(self):
        rows = self._measured_rows()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "amp_sweep_1700000000.csv")
            lra_amplitude_sweep.save_csv(csv_path, rows)

            seen = {}
            for metric in (METRIC_LEGACY_MAGNITUDE_RMS, METRIC_VECTOR_RMS):
                png = str(Path(tmp) / f"amp_{metric}.png")
                summary = lra_amplitude_sweep.render_csv(csv_path, png,
                                                         metric=metric)
                meta_path = lra_amplitude_sweep.save_meta(
                    csv_path, png, None, "1700000000", "fw", 0, 0, 224,
                    metric, rows)
                with open(meta_path) as f:
                    meta = json.load(f)
                labels, title = self._legend_labels(rows, metric)
                seen[metric] = (summary, meta, labels, title)

            legacy_summary, legacy_meta, legacy_labels, legacy_title = \
                seen[METRIC_LEGACY_MAGNITUDE_RMS]
            vector_summary, vector_meta, vector_labels, vector_title = \
                seen[METRIC_VECTOR_RMS]

            # the curve / selected metric
            self.assertEqual(legacy_summary["metric"],
                             METRIC_LEGACY_MAGNITUDE_RMS)
            self.assertEqual(vector_summary["metric"], METRIC_VECTOR_RMS)
            # the target band, in the summary AND in the meta
            self.assertEqual(legacy_summary["target_band_ms2"], [0.4, 0.6])
            self.assertEqual(vector_summary["target_band_ms2"], [2.4, 3.0])
            self.assertEqual(legacy_meta["result"]["target_band_ms2"],
                             [0.4, 0.6])
            self.assertEqual(vector_meta["result"]["target_band_ms2"],
                             [2.4, 3.0])
            self.assertEqual(legacy_meta["parameters"]["target_band_ms2"],
                             [0.4, 0.6])
            self.assertEqual(vector_meta["parameters"]["target_band_ms2"],
                             [2.4, 3.0])
            # the target value
            self.assertEqual(legacy_summary["target_ms2"], 0.5)
            self.assertEqual(vector_summary["target_ms2"], 2.7)
            # the calibration status
            self.assertEqual(legacy_meta["result"]["target_calibration_status"],
                             "historical")
            self.assertEqual(vector_meta["result"]["target_calibration_status"],
                             "provisional")
            # the recommended cue amp and the in-band list
            self.assertEqual(legacy_summary["recommended_cue_amp"], 52)
            self.assertEqual(vector_summary["recommended_cue_amp"], 52)
            self.assertNotAlmostEqual(
                legacy_summary["recommended_cue_amp_ms2"],
                vector_summary["recommended_cue_amp_ms2"], places=2)
            self.assertEqual(legacy_summary["in_band_amps"], [44, 48, 52, 56])
            self.assertEqual(vector_summary["in_band_amps"], [48, 52, 56])
            # the legend and the title
            self.assertTrue(any("0.4-0.6 m/s²" in s for s in legacy_labels),
                            legacy_labels)
            self.assertTrue(any("2.4-3 m/s²" in s for s in vector_labels),
                            vector_labels)
            self.assertFalse(any("2.4-3 m/s²" in s for s in legacy_labels))
            self.assertFalse(any("0.4-0.6 m/s²" in s for s in vector_labels))
            self.assertTrue(any("historical calibration" in s
                                for s in legacy_labels))
            self.assertTrue(any("provisional calibration" in s
                                for s in vector_labels))
            self.assertTrue(any("Recommended cue amp = 52" in s
                                for s in vector_labels), vector_labels)
            self.assertIn("0.4-0.6 m/s²", legacy_title)
            self.assertIn("2.4-3 m/s²", vector_title)

    def test_meta_records_every_required_field(self):
        rows = self._measured_rows()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "amp_sweep_1700000000.csv")
            png = str(Path(tmp) / "amp.png")
            lra_amplitude_sweep.save_csv(csv_path, rows)
            lra_amplitude_sweep.save_plot(
                png, rows, lra_amplitude_sweep.recommended_cue_amp(
                    rows, METRIC_VECTOR_RMS), metric=METRIC_VECTOR_RMS)
            meta_path = lra_amplitude_sweep.save_meta(
                csv_path, png, None, "1700000000", "fw", 0, 0, 224,
                METRIC_VECTOR_RMS, rows)
            with open(meta_path) as f:
                meta = json.load(f)
            result = meta["result"]
            for key in ("metric", "target_band_ms2", "target_ms2",
                        "target_calibration_status",
                        "target_calibration_source",
                        "target_band_calibrated", "recommended_cue_amp",
                        "recommended_cue_amp_ms2", "in_band_amps"):
                self.assertIn(key, result)
            self.assertEqual(result["metric"], METRIC_VECTOR_RMS)
            self.assertAlmostEqual(result["recommended_cue_amp_ms2"], 2.631)
            # Old key names still resolve to the same numbers.
            self.assertEqual(result["recommended_amp"],
                             result["recommended_cue_amp"])
            self.assertEqual(result["recommended_ms2"],
                             result["recommended_cue_amp_ms2"])
            # Every band definition is recorded, not just the one used.
            targets = meta["parameters"]["cue_targets_by_metric"]
            self.assertEqual(targets[METRIC_LEGACY_MAGNITUDE_RMS]
                             ["target_band_ms2"], [0.4, 0.6])
            self.assertEqual(targets[METRIC_VECTOR_RMS]["target_band_ms2"],
                             [2.4, 3.0])

    # -- uncalibrated metrics --------------------------------------------

    def test_uncalibrated_metric_gets_no_recommendation(self):
        """A metric with no band must borrow nothing: no cue amp, no
        in-band list, and an explicit not-calibrated status."""
        rows = self._measured_rows()
        with mock.patch.dict(lra_amplitude_sweep.CUE_TARGETS, clear=True):
            target = lra_amplitude_sweep.cue_target(METRIC_VECTOR_RMS)
            self.assertFalse(target.calibrated)
            self.assertIsNone(target.band_ms2)
            self.assertEqual(target.calibration_status,
                             lra_amplitude_sweep.CALIBRATION_NONE)
            self.assertEqual(target.band_label,
                             lra_amplitude_sweep.UNCALIBRATED_LABEL)
            self.assertIsNone(lra_amplitude_sweep.recommended_cue_amp(
                rows, METRIC_VECTOR_RMS))
            self.assertEqual(lra_amplitude_sweep.in_band(rows,
                                                         METRIC_VECTOR_RMS), [])
            block = lra_amplitude_sweep.result_block(rows, METRIC_VECTOR_RMS)
            self.assertIsNone(block["recommended_cue_amp"])
            self.assertIsNone(block["recommended_cue_amp_ms2"])
            self.assertFalse(block["target_band_calibrated"])
            self.assertIn("recommendation_unavailable_reason", block)

    def test_uncalibrated_metric_still_plots_the_curve(self):
        rows = self._measured_rows()
        with mock.patch.dict(lra_amplitude_sweep.CUE_TARGETS, clear=True):
            labels, title = self._legend_labels(rows, METRIC_VECTOR_RMS)
            self.assertIn(lra_amplitude_sweep.UNCALIBRATED_LABEL, title)
            # The measured curve is still drawn...
            self.assertTrue(any("Measured" in s for s in labels), labels)
            # ...but nothing claims a band or a recommendation.
            self.assertFalse(any("Target cue band" in s for s in labels))
            self.assertFalse(any("Recommended cue amp" in s for s in labels))

    # -- compatibility ----------------------------------------------------

    def test_legacy_only_csv_uses_the_legacy_band(self):
        """An old CSV carries only the legacy metric; re-rendering it must
        use the LEGACY band even when the vector RMS is requested."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "amp_sweep_1600000000.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "amp", "rms_delta_counts", "rms_ms2", "peak_delta_counts",
                    "baseline_mag", "n_samples"])
                writer.writeheader()
                for amp, counts in ((48, 45.0), (52, 51.0), (56, 58.0)):
                    writer.writerow({"amp": amp, "rms_delta_counts": counts,
                                     "rms_ms2": counts * MS2_PER_COUNT,
                                     "peak_delta_counts": counts * 3,
                                     "baseline_mag": 1000.0, "n_samples": 130})
            png = str(Path(tmp) / "amp.png")
            summary = lra_amplitude_sweep.render_csv(
                csv_path, png, metric=METRIC_VECTOR_RMS)
            self.assertEqual(summary["metric"], METRIC_LEGACY_MAGNITUDE_RMS)
            self.assertEqual(summary["target_band_ms2"], [0.4, 0.6])
            self.assertEqual(summary["target_calibration_status"],
                             "historical")
            # 51 counts * 0.00980665 = 0.50 m/s², the legacy aim point.
            self.assertEqual(summary["recommended_cue_amp"], 52)
            self.assertEqual(summary["recommended_amp"], 52)


class TestMotorAccDelayIntegration(unittest.TestCase):
    """The delay experiment shares the raw store and the offline metric
    block, but keeps its own onset detector - check both halves of that."""

    def _trials(self):
        rows = []
        for trial_id in (1, 2):
            samples = vibration(n=300, fs=1000.0, freq=224.0,
                                amps=(45.0, 0.0, 10.0),
                                offsets=(0.0, 0.0, G_COUNTS), quantize=True)
            rows.append(motor_acc_delay.TrialResult(
                trial_id=trial_id, status="ok", delay_ms=6.0 + trial_id,
                baseline_magnitude_counts=G_COUNTS,
                peak_axis_delta_counts=120.0 + trial_id,
                threshold_counts=45.0, onset_ms=5.0 + trial_id,
                onset_threshold_counts=30.0,
                intensity=compute_acceleration_metrics(samples, G_COUNTS)))
        return rows

    def test_trials_csv_round_trip_keeps_both_metrics(self):
        rows = self._trials()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "delay_trials_1700000000.csv")
            motor_acc_delay.save_trials_csv(csv_path, rows)
            loaded = motor_acc_delay.load_results(csv_path)
            for original, back in zip(rows, loaded):
                self.assertEqual(back.onset_ms, original.onset_ms)
                self.assertEqual(back.delay_ms, original.delay_ms)
                self.assertAlmostEqual(back.intensity.vector_rms_ms2,
                                       original.intensity.vector_rms_ms2,
                                       places=9)
                self.assertAlmostEqual(
                    back.intensity.legacy_magnitude_rms_ms2,
                    original.intensity.legacy_magnitude_rms_ms2, places=9)
            # The figure still re-renders (it is a latency plot; there is
            # no metric to choose).
            png = str(Path(tmp) / "delay.png")
            stats = motor_acc_delay.render_csv(csv_path, png)
            self.assertTrue(Path(png).exists())
            self.assertEqual(stats["n_ok"], 2)

    def test_pre_refactor_trials_csv_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "delay_trials_1600000000.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "trial_id", "status", "onset_ms", "delay_ms",
                    "baseline_mag", "peak_delta", "onset_threshold",
                    "threshold"])
                writer.writeheader()
                writer.writerow({"trial_id": 1, "status": "ok",
                                 "onset_ms": 5.9, "delay_ms": 6.3,
                                 "baseline_mag": 1001.2, "peak_delta": 310.0,
                                 "onset_threshold": 28.0, "threshold": 44.0})
            rows = motor_acc_delay.load_results(csv_path)
            self.assertEqual(rows[0].delay_ms, 6.3)
            self.assertAlmostEqual(rows[0].baseline_magnitude_counts, 1001.2)
            self.assertAlmostEqual(rows[0].peak_axis_delta_counts, 310.0)
            self.assertAlmostEqual(rows[0].threshold_counts, 44.0)
            # No offline intensity block existed back then.
            self.assertIsNone(rows[0].intensity)
            png = str(Path(tmp) / "old_delay.png")
            motor_acc_delay.render_csv(csv_path, png)
            self.assertTrue(Path(png).exists())

    def test_detector_statistic_is_the_per_sample_form_of_the_vector_rms(self):
        """The documented reason the detector was left alone: its
        per-sample per-axis deviation, RMS'd over a window, IS the
        demeaned vector RMS - so nothing would be gained by swapping."""
        samples = vibration(n=512, fs=1000.0, freq=100.0,
                            amps=(45.0, 12.0, 10.0),
                            offsets=(0.0, 0.0, G_COUNTS))
        arr = np.asarray(samples, dtype=float)
        means = arr.mean(axis=0)
        per_sample_deviation = np.sqrt(((arr - means) ** 2).sum(axis=1))
        self.assertAlmostEqual(
            math.sqrt(float(np.mean(per_sample_deviation ** 2))),
            compute_demeaned_vector_rms(samples), places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
