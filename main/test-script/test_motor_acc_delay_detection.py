"""Synthetic-signal tests for the Motor -> ACC onset/settling detector
(validation_experiments/motor_acc_delay_experiment/motor_acc_delay.py).

The detector is a pure function over a per-sample deviation series, so
every case below is a signal built here with a KNOWN onset and a KNOWN
settling time - no hardware, no saved run. Covered:

  * a clean step: sharp rising edge and a flat plateau
  * a realistic noisy start (LRA-like resonant ring-up)
  * a lone spike, which must NOT be mistaken for the onset
  * onset detected but never settled (the drive dies / keeps drifting)
  * no onset at all (pure rest noise)
  * an LRA-like fast start
  * an ERM-like slow spin-up ramp
  * several vibration durations, 0.5 s to 10 s
  * the CSV / plot / status plumbing around all of it

Run from main/:  python test-script/test_motor_acc_delay_detection.py
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import numpy as np  # noqa: E402

from validation_experiments.acceleration_metrics import (  # noqa: E402
    compute_acceleration_metrics,
)
from validation_experiments.motor_acc_delay_experiment import (  # noqa: E402
    motor_acc_delay as mad,
)

FS = 1344.0          # the rig's LIS3DH ODR (firmware >= v2.9.0)
G_COUNTS = 1000.0    # 1 g at +/-2 g, 1 count = 1 mg
NOISE_SD = 8.0       # per-axis rest noise (counts)


# ==========================================
# Synthetic signal helpers
# ==========================================

def rest_deltas(n: int, seed: int = 0, sd: float = NOISE_SD) -> np.ndarray:
    """A rest-noise per-axis deviation series: the Euclidean norm of
    three independent zero-mean axes, which is what the real detector
    sees when the rig hangs still."""
    rng = np.random.default_rng(seed)
    axes = rng.normal(0.0, sd, size=(n, 3))
    return np.sqrt((axes ** 2).sum(axis=1))


def carrier(n: int, freq_hz: float, fs: float = FS,
            phase: float = 0.0) -> np.ndarray:
    """|sin| carrier, normalised to unit RMS - the shape a rectified
    per-axis deviation has when the actuator drives a tone."""
    t = np.arange(n) / fs
    return np.abs(np.sin(2 * np.pi * freq_hz * t + phase)) * np.sqrt(2.0)


def build_signal(duration_s: float, amplitude_profile, freq_hz: float = 224.0,
                 fs: float = FS, seed: int = 1, noise_sd: float = NOISE_SD):
    """(rel_times, deltas) for one drive window.

    `amplitude_profile(t)` returns the vibration RMS in counts at each
    second since the command; the returned series is that profile times a
    carrier, plus rest noise."""
    n = int(round(duration_s * fs))
    t = np.arange(n) / fs
    amplitude = np.asarray(amplitude_profile(t), dtype=float)
    vibration = amplitude * carrier(n, freq_hz, fs)
    return t, np.sqrt(vibration ** 2 + rest_deltas(n, seed, noise_sd) ** 2)


def step_profile(onset_s: float, level: float):
    """Instant jump to `level` at `onset_s` (a clean rising edge)."""
    return lambda t: np.where(t >= onset_s, level, 0.0)


def ramp_profile(onset_s: float, rise_s: float, level: float):
    """Linear ramp from zero at `onset_s` to `level` after `rise_s`."""
    def profile(t):
        frac = np.clip((t - onset_s) / rise_s, 0.0, 1.0)
        return np.where(t >= onset_s, frac * level, 0.0)
    return profile


def runaway_profile(onset_s: float, start: float = 60.0,
                    doubling_s: float = 0.4 * np.log(2)):
    """An amplitude that keeps growing exponentially and so never holds
    inside any +/-20% band for a whole hold window - the "onset detected
    but never settled" case."""
    def profile(t):
        rel = np.clip(t - onset_s, 0.0, None)
        return np.where(t >= onset_s,
                        start * np.exp(rel * np.log(2) / doubling_s), 0.0)
    return profile


def exponential_profile(onset_s: float, tau_s: float, level: float):
    """1 - exp(-t/tau) ring-up: the LRA's resonant build-up."""
    def profile(t):
        rel = np.clip(t - onset_s, 0.0, None)
        return np.where(t >= onset_s, level * (1.0 - np.exp(-rel / tau_s)), 0.0)
    return profile


def noise_for(seed: int = 99, duration_s: float = 1.0,
              sd: float = NOISE_SD) -> mad.NoiseStats:
    """Baseline statistics from a synthetic quiet window."""
    return mad.noise_stats(rest_deltas(int(duration_s * FS), seed, sd), FS)


def detect(rel_times, deltas, duration_s, noise=None):
    return mad.detect_onset_and_settling(
        rel_times, deltas, noise if noise is not None else noise_for(),
        duration_s)


# ==========================================
# Onset detection
# ==========================================

class TestOnsetDetection(unittest.TestCase):

    def test_clean_step_dates_the_rising_edge(self):
        """A sharp edge with a flat plateau: onset within a couple of ms
        of the true edge, and a stable state found right after it."""
        onset = 0.050
        times, deltas = build_signal(2.0, step_profile(onset, 300.0))
        result = detect(times, deltas, 2.0)

        self.assertEqual(result.status, mad.STATUS_OK)
        self.assertAlmostEqual(result.onset_rel_s, onset, delta=0.005)
        # A step is already steady; the envelope needs at most its own
        # window to catch up.
        self.assertLess(result.settling_s, 0.040)
        self.assertGreater(result.stable_rel_s, result.onset_rel_s - 1e-9)

    def test_noisy_normal_start_is_detected(self):
        """A realistic resonant ring-up buried in rest noise."""
        onset = 0.030
        times, deltas = build_signal(
            2.0, exponential_profile(onset, 0.020, 250.0), seed=7)
        result = detect(times, deltas, 2.0)

        self.assertEqual(result.status, mad.STATUS_OK)
        self.assertAlmostEqual(result.onset_rel_s, onset, delta=0.010)
        self.assertIsNotNone(result.settling_s)
        # ~5 time constants to be inside a +/-20% band of the plateau.
        self.assertLess(result.settling_s, 0.150)

    def test_transient_spike_is_not_reported_as_onset(self):
        """A lone knock before the real start must be stepped over."""
        onset = 0.400
        times, deltas = build_signal(2.0, step_profile(onset, 300.0), seed=11)
        spike_idx = int(0.100 * FS)
        deltas = deltas.copy()
        deltas[spike_idx] += 1200.0          # one enormous sample
        deltas[spike_idx + 1] += 400.0       # ...and its immediate ring

        result = detect(times, deltas, 2.0)
        self.assertEqual(result.status, mad.STATUS_OK)
        self.assertGreater(result.onset_rel_s, 0.150,
                           "the spike at 100 ms was dated as the onset")
        self.assertAlmostEqual(result.onset_rel_s, onset, delta=0.005)

    def test_a_spike_on_its_own_yields_no_onset(self):
        """Nothing but rest noise and one knock: no onset at all."""
        times, deltas = build_signal(2.0, lambda t: np.zeros_like(t), seed=3)
        deltas = deltas.copy()
        deltas[int(0.5 * FS)] += 1500.0

        result = detect(times, deltas, 2.0)
        self.assertEqual(result.status, mad.STATUS_NO_ONSET)
        self.assertIsNone(result.onset_rel_s)
        self.assertIsNone(result.stable_rel_s)

    def test_pure_rest_noise_yields_no_onset(self):
        times, deltas = build_signal(2.0, lambda t: np.zeros_like(t), seed=5)
        result = detect(times, deltas, 2.0)
        self.assertEqual(result.status, mad.STATUS_NO_ONSET)
        self.assertIsNone(result.onset_rel_s)

    def test_lra_style_fast_start(self):
        """LRA: near-fixed frequency, ring-up in a few ms."""
        onset = 0.006
        times, deltas = build_signal(
            2.0, exponential_profile(onset, 0.004, 200.0), freq_hz=224.0,
            seed=21)
        result = detect(times, deltas, 2.0)

        self.assertEqual(result.status, mad.STATUS_OK)
        self.assertAlmostEqual(result.onset_rel_s, onset, delta=0.006)
        self.assertLess(result.settling_s, 0.060)

    def test_erm_style_slow_spin_up(self):
        """ERM: the rotor takes ~300 ms to reach speed, and the drive
        frequency climbs with it. The onset must still be dated at the
        START of the ramp, not where it becomes obvious."""
        onset = 0.010
        rise = 0.300

        def sweeping(t):
            # frequency ramps 40 -> 180 Hz as the rotor spins up
            frac = np.clip((t - onset) / rise, 0.0, 1.0)
            phase = 2 * np.pi * (40.0 * t + 0.5 * (140.0 / rise)
                                 * np.clip(t - onset, 0, rise) ** 2)
            return np.abs(np.sin(phase)) * np.sqrt(2.0) * frac * 260.0

        n = int(2.0 * FS)
        t = np.arange(n) / FS
        deltas = np.sqrt(sweeping(t) ** 2 + rest_deltas(n, 31) ** 2)
        result = detect(t, deltas, 2.0)

        self.assertEqual(result.status, mad.STATUS_OK)
        # Dated near the true start of the ramp, well before the ramp is
        # anywhere near its plateau.
        self.assertLess(result.onset_rel_s, 0.060)
        self.assertGreater(result.onset_rel_s, 0.0)
        # And the settling time reflects the slow spin-up.
        self.assertGreater(result.settling_s, 0.150)

    def test_onset_is_earlier_for_a_fast_start_than_a_slow_one(self):
        """The same detector, both actuator shapes: the settling time -
        not the onset - is what separates them."""
        fast = detect(*build_signal(2.0, exponential_profile(0.010, 0.004,
                                                             240.0), seed=41),
                      duration_s=2.0)
        slow = detect(*build_signal(2.0, ramp_profile(0.010, 0.400, 240.0),
                                    seed=41), duration_s=2.0)
        self.assertEqual(fast.status, mad.STATUS_OK)
        self.assertEqual(slow.status, mad.STATUS_OK)
        self.assertLess(fast.settling_s, slow.settling_s)


# ==========================================
# Settling / failure modes
# ==========================================

class TestSettlingDetection(unittest.TestCase):

    def test_onset_without_settling_keeps_the_onset_and_no_stable_time(self):
        """A drive whose amplitude keeps growing: onset kept, settling
        empty, status not_settled - and nothing invented."""
        times, deltas = build_signal(2.0, runaway_profile(0.020), seed=13)
        result = detect(times, deltas, 2.0)

        self.assertEqual(result.status, mad.STATUS_NOT_SETTLED)
        self.assertIsNotNone(result.onset_rel_s)
        self.assertIsNone(result.stable_rel_s)
        self.assertIsNone(result.settling_s)

    def test_vibration_that_dies_immediately_is_not_settled(self):
        """A 30 ms burst then silence: an onset, but no steady state."""
        def burst(t):
            return np.where((t >= 0.050) & (t < 0.080), 300.0, 0.0)

        times, deltas = build_signal(2.0, burst, seed=17)
        result = detect(times, deltas, 2.0)

        self.assertEqual(result.status, mad.STATUS_NOT_SETTLED)
        self.assertIsNotNone(result.onset_rel_s)
        self.assertIsNone(result.stable_rel_s)

    def test_too_short_a_window_is_insufficient_data(self):
        times, deltas = build_signal(0.02, step_profile(0.005, 300.0))
        result = detect(times, deltas, 0.5)
        self.assertEqual(result.status, mad.STATUS_INSUFFICIENT_DATA)
        self.assertIsNone(result.onset_rel_s)

    def test_empty_window_is_insufficient_data(self):
        result = detect([], [], 2.0)
        self.assertEqual(result.status, mad.STATUS_INSUFFICIENT_DATA)

    def test_steady_level_matches_the_driven_amplitude(self):
        times, deltas = build_signal(2.0, step_profile(0.030, 320.0), seed=23)
        result = detect(times, deltas, 2.0)
        self.assertEqual(result.status, mad.STATUS_OK)
        # The envelope is the RMS of the deviation, i.e. the profile's
        # own RMS level, plus the (small) noise in quadrature.
        self.assertAlmostEqual(result.steady_level_counts, 320.0, delta=25.0)
        self.assertAlmostEqual(result.steady_low_counts,
                               result.steady_level_counts * 0.8, places=6)
        self.assertAlmostEqual(result.steady_high_counts,
                               result.steady_level_counts * 1.2, places=6)


# ==========================================
# Vibration duration
# ==========================================

class TestVibrationDuration(unittest.TestCase):

    DURATIONS = (0.5, 1.0, 2.0, 5.0, 10.0)

    def test_every_supported_duration_detects_the_same_onset(self):
        """Nothing in the detector assumes a 2 s window: the same start
        must be found at 0.5 s and at 10 s."""
        onset = 0.040
        for duration in self.DURATIONS:
            with self.subTest(duration_s=duration):
                times, deltas = build_signal(
                    duration, exponential_profile(onset, 0.015, 260.0),
                    seed=int(duration * 10))
                result = detect(times, deltas, duration)
                self.assertEqual(result.status, mad.STATUS_OK)
                self.assertAlmostEqual(result.onset_rel_s, onset, delta=0.012)
                self.assertIsNotNone(result.settling_s)
                self.assertLess(result.settling_s, 0.200)

    def test_hold_window_scales_with_the_duration_and_is_bounded(self):
        self.assertEqual(mad.steady_hold_s(0.5), mad.STEADY_HOLD_MIN_S)
        self.assertAlmostEqual(mad.steady_hold_s(2.0), 0.2)
        self.assertEqual(mad.steady_hold_s(10.0), mad.STEADY_HOLD_MAX_S)
        for duration in self.DURATIONS:
            hold = mad.steady_hold_s(duration)
            self.assertGreaterEqual(hold, mad.STEADY_HOLD_MIN_S)
            self.assertLessEqual(hold, mad.STEADY_HOLD_MAX_S)
            # The hold must always fit inside the steady reference tail.
            self.assertLess(hold, duration * mad.STEADY_REF_FRACTION + 1e-9)

    def test_the_supported_range_is_the_documented_one(self):
        self.assertEqual(mad.VIB_DURATION_S, 2.0)
        self.assertEqual(mad.VIB_DURATION_MIN_S, 0.5)
        self.assertEqual(mad.VIB_DURATION_MAX_S, 10.0)
        self.assertEqual(mad.VIB_DURATION_STEP_S, 0.5)

    def test_duration_drives_the_run_time_estimate(self):
        short = mad.estimated_duration_s(0.5)
        long = mad.estimated_duration_s(10.0)
        self.assertAlmostEqual(long - short, mad.NUM_TRIALS * 9.5)
        self.assertAlmostEqual(mad.estimated_duration_s(),
                               mad.estimated_duration_s(mad.VIB_DURATION_S))


# ==========================================
# Envelope / helper properties
# ==========================================

class TestEnvelope(unittest.TestCase):

    def test_moving_rms_of_a_constant_is_that_constant(self):
        values = np.full(500, 7.0)
        envelope = mad.moving_rms(values, 27)
        np.testing.assert_allclose(envelope, 7.0, rtol=1e-12)

    def test_moving_rms_has_no_lag_on_a_step(self):
        """A centred window puts the half-height point AT the step, so
        settling times carry no window-length bias."""
        values = np.concatenate((np.zeros(500), np.full(500, 100.0)))
        envelope = mad.moving_rms(values, 41)
        # Half energy => 1/sqrt(2) of the level, at the step itself.
        self.assertAlmostEqual(envelope[500], 100.0 / np.sqrt(2), delta=6.0)
        self.assertLess(envelope[460], 40.0)
        self.assertGreater(envelope[540], 95.0)

    def test_moving_rms_is_the_windowed_form_of_the_detector_statistic(self):
        """The envelope is the shared demeaned vector RMS over a sliding
        window - the reason the detector and the metric never disagree."""
        rng = np.random.default_rng(2)
        axes = rng.normal(0.0, 40.0, size=(256, 3))
        deltas = np.sqrt((axes ** 2).sum(axis=1))
        whole_window = mad.moving_rms(deltas, len(deltas))[0]
        self.assertAlmostEqual(whole_window,
                               float(np.sqrt(np.mean(deltas ** 2))), places=9)

    def test_sample_rate_is_estimated_from_the_arrival_times(self):
        times = np.arange(1000) / 1344.0
        self.assertAlmostEqual(mad.estimate_sample_rate_hz(times), 1344.0,
                               places=6)


# ==========================================
# Result plumbing (status, CSV, figure)
# ==========================================

def _baseline_window(seed: int = 77) -> mad.BaselineWindow:
    """A synthetic quiet window in the shape collect_baseline returns."""
    n = int(1.0 * FS)
    rng = np.random.default_rng(seed)
    axes = rng.normal(0.0, NOISE_SD, size=(n, 3)) + np.array([0.0, 0.0,
                                                              G_COUNTS])
    samples = [(i / FS, float(x), float(y), float(z))
               for i, (x, y, z) in enumerate(axes)]
    means = (float(axes[:, 0].mean()), float(axes[:, 1].mean()),
             float(axes[:, 2].mean()))
    deltas = mad.per_axis_deltas(samples, means)
    noise = mad.noise_stats(deltas, FS)
    return mad.BaselineWindow(
        samples=samples, means=means,
        magnitude_counts=G_COUNTS,
        threshold_counts=max(mad.MIN_THRESHOLD,
                             mad.NOISE_MULT * noise.p95_counts),
        noise=noise)


def _drive_window(duration_s: float, profile, seed: int = 5):
    """Synthetic (t, x, y, z) samples for a drive window: the vibration
    is put on x and y so |a| barely moves - the case the per-axis
    detector exists for."""
    n = int(round(duration_s * FS))
    t = np.arange(n) / FS
    amplitude = np.asarray(profile(t), dtype=float)
    wave = np.sin(2 * np.pi * 224.0 * t) * np.sqrt(2.0)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, NOISE_SD, size=(n, 3))
    x = amplitude * wave * 0.8 + noise[:, 0]
    y = amplitude * wave * 0.6 + noise[:, 1]
    z = G_COUNTS + noise[:, 2]
    samples = [(float(ti), float(xi), float(yi), float(zi))
               for ti, xi, yi, zi in zip(t, x, y, z)]
    return samples, list(t)


class TestTrialAnalysis(unittest.TestCase):

    def _analyse(self, duration_s, profile, trial_id=1, seed=5):
        baseline = _baseline_window()
        samples, rel_times = _drive_window(duration_s, profile, seed)
        return mad.analyse_trial(
            trial_id=trial_id, log=lambda _msg: None, baseline=baseline,
            raw_samples=samples, rel_times=rel_times,
            command_time_s=1_700_000_000.0, vib_duration_s=duration_s,
            actuator_type="LRA")

    def test_ok_trial_carries_all_three_times_and_both_settling_forms(self):
        result = self._analyse(2.0, exponential_profile(0.025, 0.015, 300.0))
        self.assertEqual(result.status, mad.STATUS_OK)
        self.assertEqual(result.actuator_type, "LRA")

        # The three instants, in Unix epoch seconds.
        self.assertAlmostEqual(result.command_time_s, 1_700_000_000.0)
        self.assertGreater(result.onset_time_s, result.command_time_s)
        self.assertGreater(result.stable_time_s, result.onset_time_s)

        # ...and the three latencies derived from them, consistent. (The
        # latencies themselves are exact; differencing two epoch stamps
        # costs float64 resolution at ~1e-4 ms, hence the tolerance.)
        self.assertAlmostEqual(
            result.onset_ms,
            (result.onset_time_s - result.command_time_s) * 1000.0, places=3)
        self.assertAlmostEqual(
            result.settling_time_from_onset_ms,
            (result.stable_time_s - result.onset_time_s) * 1000.0, places=3)
        self.assertAlmostEqual(
            result.stable_latency_from_command_ms,
            (result.stable_time_s - result.command_time_s) * 1000.0, places=3)
        self.assertAlmostEqual(
            result.stable_latency_from_command_ms,
            result.onset_ms + result.settling_time_from_onset_ms, places=6)

        # The shared offline intensity block is still computed.
        self.assertIsNotNone(result.intensity)
        self.assertGreater(result.intensity.vector_rms_ms2, 0.0)
        self.assertGreater(result.steady_state_envelope_ms2, 0.0)
        # ...and the full window was kept, not just the ring-up.
        self.assertAlmostEqual(len(result.deltas) / FS, 2.0, delta=0.01)

    def test_not_settled_trial_keeps_onset_and_leaves_settling_empty(self):
        def burst(t):
            return np.where((t >= 0.040) & (t < 0.070), 320.0, 0.0)

        result = self._analyse(2.0, burst)
        self.assertEqual(result.status, mad.STATUS_NOT_SETTLED)
        self.assertIsNotNone(result.onset_ms)
        self.assertIsNone(result.settling_time_from_onset_ms)
        self.assertIsNone(result.stable_latency_from_command_ms)
        self.assertIsNone(result.stable_time_s)

    def test_no_onset_trial(self):
        result = self._analyse(2.0, lambda t: np.zeros_like(t))
        self.assertEqual(result.status, mad.STATUS_NO_ONSET)
        self.assertIsNone(result.onset_ms)
        self.assertIsNone(result.settling_time_from_onset_ms)

    def test_empty_drive_window_is_insufficient_data(self):
        baseline = _baseline_window()
        result = mad.analyse_trial(
            trial_id=1, log=lambda _m: None, baseline=baseline,
            raw_samples=[], rel_times=[], command_time_s=1.0,
            vib_duration_s=2.0)
        self.assertEqual(result.status, mad.STATUS_INSUFFICIENT_DATA)
        self.assertIsNone(result.onset_ms)

    def test_summary_reports_the_two_latencies_separately(self):
        results = [
            self._analyse(2.0, exponential_profile(0.02, 0.01, 300.0),
                          trial_id=1, seed=1),
            self._analyse(2.0, exponential_profile(0.03, 0.02, 280.0),
                          trial_id=2, seed=2),
            self._analyse(2.0, lambda t: np.zeros_like(t), trial_id=3, seed=3),
        ]
        stats = mad.delay_stats(results)
        self.assertEqual(stats["n_trials"], 3)
        self.assertEqual(stats["n_ok"], 2)
        self.assertEqual(stats["n_no_onset"], 1)
        self.assertEqual(stats["n_not_settled"], 0)
        for prefix in ("onset", "settling"):
            for key in ("mean_ms", "median_ms", "sd_ms", "min_ms", "max_ms"):
                self.assertIsNotNone(stats[f"{prefix}_{key}"],
                                     f"{prefix}_{key} missing")
        # The two are never the same number.
        self.assertNotAlmostEqual(stats["onset_mean_ms"],
                                  stats["settling_mean_ms"], places=3)
        self.assertEqual(stats["onset_n"], 2)
        self.assertEqual(stats["settling_n"], 2)

    def test_csv_round_trip_and_figure(self):
        results = [
            self._analyse(2.0, exponential_profile(0.02, 0.012, 300.0),
                          trial_id=1, seed=1),
            self._analyse(2.0, runaway_profile(0.040), trial_id=2, seed=2),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = str(Path(tmp) / "delay_trials_1700000000.csv")
            samples_path = mad.samples_path_for(csv_path)
            mad.save_trials_csv(csv_path, results)
            mad.save_samples_csv(samples_path, results)
            mad.save_meta(csv_path, samples_path,
                          str(Path(tmp) / "delay_summary_1700000000.png"),
                          "1700000000", "haptic-piano v2.9.0", 11, 0, "LRA",
                          results, vib_duration_s=2.0)

            with open(csv_path, newline="") as f:
                rows = list(csv.DictReader(f))
            # A not-settled trial stores an EMPTY settling time, never 0.
            not_settled = [r for r in rows
                           if r["status"] == mad.STATUS_NOT_SETTLED]
            self.assertTrue(not_settled)
            for row in not_settled:
                self.assertEqual(row["settling_time_from_onset_ms"], "")
                self.assertEqual(row["stable_time_s"], "")
                self.assertNotEqual(row["onset_ms"], "")
            for row in rows:
                self.assertEqual(row["actuator_type"], "LRA")
                self.assertEqual(float(row["vib_duration_s"]), 2.0)
                self.assertEqual(row["onset_ms"],
                                 row["onset_latency_from_command_ms"])
                # The shared intensity block is still there.
                self.assertNotEqual(row["vector_rms_ms2"], "")
                self.assertNotEqual(row["legacy_magnitude_rms_ms2"], "")

            loaded = mad.load_results(csv_path)
            for original, back in zip(results, loaded):
                self.assertEqual(back.status, original.status)
                self.assertAlmostEqual(back.onset_ms, original.onset_ms,
                                       places=3)
                self.assertEqual(back.settling_time_from_onset_ms is None,
                                 original.settling_time_from_onset_ms is None)
                self.assertAlmostEqual(back.intensity.vector_rms_ms2,
                                       original.intensity.vector_rms_ms2,
                                       places=9)

            png = str(Path(tmp) / "render.png")
            stats = mad.render_csv(csv_path, png)
            self.assertTrue(Path(png).exists())
            self.assertEqual(stats["n_ok"], 1)
            self.assertEqual(stats["n_not_settled"], 1)
            self.assertEqual(stats["vib_duration_s"], 2.0)

    def test_samples_csv_without_an_envelope_column_still_renders(self):
        """Compatibility: runs saved before the settling metrics carry
        deltas only, and their envelope is recomputed on load."""
        results = [self._analyse(2.0, exponential_profile(0.02, 0.012, 300.0))]
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "delay_samples_1600000000.csv")
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["trial_id", "rel_time_s", "delta"])
                for t, d in zip(results[0].rel_times, results[0].deltas):
                    writer.writerow([1, f"{t:.5f}", f"{d:.2f}"])
            traces = mad.load_samples(path)
            self.assertEqual(len(traces[1].envelopes), len(traces[1].deltas))
            self.assertGreater(max(traces[1].envelopes), 0.0)


class TestBackwardCompatibility(unittest.TestCase):

    def test_pre_settling_csv_loads_and_renders(self):
        """An onset/crossing-era CSV (no settling columns at all)."""
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
                writer.writerow({"trial_id": 2, "status": "timeout",
                                 "onset_ms": "", "delay_ms": "",
                                 "baseline_mag": 1000.4, "peak_delta": 42.0,
                                 "onset_threshold": 26.0, "threshold": 41.0})
            rows = mad.load_results(csv_path)
            self.assertEqual(rows[0].delay_ms, 6.3)
            self.assertIsNone(rows[0].settling_time_from_onset_ms)
            self.assertIsNone(rows[0].stable_time_s)
            self.assertIsNone(rows[0].intensity)
            stats = mad.delay_stats(rows)
            self.assertEqual(stats["n_timeout"], 1)
            self.assertEqual(stats["n_settling"], 0)
            self.assertIsNone(stats["settling_mean_ms"])
            png = str(Path(tmp) / "old.png")
            mad.render_csv(csv_path, png)
            self.assertTrue(Path(png).exists())

    def test_trial_result_still_accepts_the_historical_constructor(self):
        """The pre-settling keyword set still builds a TrialResult."""
        samples = [(i / FS, 10.0, 20.0, G_COUNTS) for i in range(64)]
        row = mad.TrialResult(
            trial_id=1, status="ok", delay_ms=6.0,
            baseline_magnitude_counts=G_COUNTS,
            peak_axis_delta_counts=120.0, threshold_counts=45.0,
            onset_ms=5.0, onset_threshold_counts=30.0,
            intensity=compute_acceleration_metrics(samples, G_COUNTS))
        self.assertEqual(row.status, "ok")
        self.assertIsNone(row.settling_time_from_onset_ms)
        self.assertEqual(row.vib_duration_s, mad.VIB_DURATION_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
