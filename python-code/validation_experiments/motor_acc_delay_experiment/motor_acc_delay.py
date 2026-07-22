"""Motor command -> accelerometer detection delay.

Measures the latency between the host issuing a motor-on command
("S mask amp") and the LIS3DH glued to that motor registering the
vibration - the electromechanical part of the haptic-cue latency chain.

Two onset metrics are reported per trial, both standard practice for
signal-onset estimation:

  onset_ms  - MOTION ONSET: the first sustained above-noise response
              (ONSET_CONSECUTIVE consecutive samples above
              ONSET_MULT x the baseline noise p95), the command-to-
              first-motion latency.
  delay_ms  - DETECTION-LEVEL CROSSING: the response reaching the
              detection level (NOISE_MULT x baseline p95). For a
              resonantly driven LRA this includes the mechanical
              ring-up, so delay_ms - onset_ms is the rise time to
              detectable amplitude.

Both instants are refined by LINEAR INTERPOLATION between the last
sub-threshold and first supra-threshold sample, removing the sampling-
grid quantisation bias. With firmware >= v2.9.0 the LIS3DH runs at
1.344 kHz and the stream at 1 ms, giving ~6 samples per 224 Hz
vibration cycle.

The suspended-rig protocol (hang the motor+sensor pair freely, any
orientation; every trial gates on stillness first) and the per-axis
adaptive detector are documented in README.md.

Per-run outputs (Unix-epoch-seconds stamp <ts>) under
data/validation_experiments/motor_acc_delay_experiment/:
  delay_trials_<ts>.csv   per-trial summary rows
  delay_samples_<ts>.csv  per-sample delta series (long format)
  delay_summary_<ts>.png  two-panel figure (delays per trial + traces)
  delay_trials_<ts>.meta.json  parameters, firmware, files, result

Runs standalone (python motor_acc_delay.py) or through the launcher's
"Validation Experiments" section; both paths write the same files.
"""

import csv
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt

try:
    from ..rig import SweepAborted, open_rig, parse_acc_line, send
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from rig import SweepAborted, open_rig, parse_acc_line, send


# ==========================================
# Experiment configuration
# ==========================================

# The rig's two actuator test channels (see teensy_driver README wiring).
ACTUATOR_MOTORS = {"LRA": 11, "ERM": 10}

# PWM frequency per actuator type. The firmware boot default (224 Hz,
# the LRA's resonance) is right for the LRA but WRONG for an ERM: a DC
# motor chopped at 224 Hz (1.1 ms on / 3.3 ms off at amp 64) cannot
# overcome stiction and never starts. ERMs need kHz-range PWM so the
# drive behaves like smooth DC - v2.1.0's ~4.5 kHz default is why the
# historical ERM runs worked. The experiment sets the port's frequency
# before the trials and restores the boot default afterwards.
DEFAULT_PWM_FREQ = 224
ACTUATOR_PWM_HZ = {"LRA": DEFAULT_PWM_FREQ, "ERM": 5000}

MOTOR_INDEX = 11         # default: the LRA test channel
ACC_SENSOR_ID = 0

ACC_INTERVAL_MS = 1      # firmware >= v2.9.0: LIS3DH at 1.344 kHz, so a
                         # 1 ms stream carries fresh samples (older
                         # firmware streams duplicates beyond 2.5 ms)
AMP = 64                 # project-default cue intensity
VIB_DURATION_S = 0.20    # motor ON duration per trial

NUM_TRIALS = 10
BASELINE_DURATION_S = 1.0
INTER_TRIAL_REST_S = 1.0
SEARCH_TIMEOUT_S = 3.0

# Detection level (delay_ms): adaptive, derived from the quiet baseline:
#   threshold = max(MIN_THRESHOLD, NOISE_MULT * p95(baseline deviations))
# The deviation is computed PER AXIS against the per-axis baseline means
# (| |a| - baseline | is only second-order sensitive to vibration
# perpendicular to gravity and is not used).
MIN_THRESHOLD = 40       # counts; floor so noise can't set it absurdly low
NOISE_MULT = 3.0         # detection level = this many times baseline p95
CONSECUTIVE_HITS = 1     # samples above the level required for a crossing

# Motion onset (onset_ms): CUSUM change-point detection (Page 1954).
# A fixed-amplitude onset criterion is biased against gradually ringing
# actuators: an ERM's impulsive start crosses any level instantly while
# an LRA's exponential ring-up is only "admitted" once it has grown to
# the level, milliseconds after motion truly began. CUSUM instead
# accumulates each sample's departure from the baseline-noise
# distribution, S_i = max(0, S_{i-1} + (d_i - mu - k*sigma)), alarms
# when S exceeds h*sigma, and dates the onset at the sample where the
# alarmed excursion STARTED - the first instant the curve departs from
# noise, regardless of how slowly the amplitude grows.
CUSUM_SLACK_SIGMA = 1.0  # k: per-sample slack above the noise mean
CUSUM_ALARM_SIGMA = 10.0  # h: accumulated evidence needed to alarm

# After the detection-level crossing, keep reading this many samples so
# the saved trace shows the ring-up context around both onsets.
POST_CROSSING_SAMPLES = 25

# Mounting protocol: the motor+sensor pair hangs FREELY IN THE AIR
# (desk mounting damps the vibration below reliable detection). The
# starting orientation is arbitrary - the per-axis baseline absorbs
# whatever direction gravity points - but each trial first waits for
# the rig to hang still.
STILL_WINDOW_S = 0.5     # stillness is judged over windows this long
# A genuinely still hanging rig still shows a p95 deviation of ~30-45
# counts (wire-borne micro-vibration + sensor noise), so the gate sits
# above that floor; actual swinging reads well over 100.
STILL_MAX_DEV = 60.0     # p95 of in-window per-axis deviations (counts)
STILL_TIMEOUT_S = 60.0   # give up if the rig never settles

OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "motor_acc_delay_experiment"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (trials_done, trials_total)


@dataclass
class TrialResult:
    trial_id: int
    status: str                    # "ok" or "timeout"
    delay_ms: Optional[float]      # detection-level crossing (interpolated)
    baseline_mag: float
    peak_delta: float
    threshold: float = 0.0         # per-trial detection level (0 = unknown)
    onset_ms: Optional[float] = None       # motion onset (interpolated)
    onset_threshold: float = 0.0   # per-trial onset threshold (0 = unknown)
    # Per-sample trace (host-relative time vs per-axis delta); empty
    # when loaded from a summary CSV without its samples file.
    rel_times: List[float] = field(default_factory=list)
    deltas: List[float] = field(default_factory=list)
    detect_rel_time: Optional[float] = None


# ==========================================
# Trial
# ==========================================

def _collect_window(ser, duration_s: float,
                    acc_sensor_id: int) -> List[Tuple[int, int, int]]:
    samples: List[Tuple[int, int, int]] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw, acc_sensor_id)
        if sample is not None:
            samples.append(sample)
    return samples


def _p95_dev(samples: List[Tuple[int, int, int]]) -> float:
    bx = statistics.fmean(s[0] for s in samples)
    by = statistics.fmean(s[1] for s in samples)
    bz = statistics.fmean(s[2] for s in samples)
    devs = sorted(math.sqrt((x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2)
                  for x, y, z in samples)
    return devs[int(0.95 * (len(devs) - 1))]


def wait_until_still(ser, acc_sensor_id: int, log: LogFn,
                     should_stop: Callable[[], bool],
                     still_max_dev: float = STILL_MAX_DEV) -> None:
    """Block until the suspended rig hangs still (see STILL_* constants;
    still_max_dev is per-run adjustable for rigs with a different noise
    floor). Orientation is irrelevant - only in-window motion matters.
    Raises SweepAborted on a stop request, RuntimeError on timeout."""
    log("Waiting for the rig to hang still...")
    t0 = time.monotonic()
    ser.reset_input_buffer()
    min_p95 = float("inf")
    windows = 0
    hinted = False
    while True:
        if should_stop():
            raise SweepAborted()
        if time.monotonic() - t0 > STILL_TIMEOUT_S:
            raise RuntimeError(
                f"Rig did not settle within {STILL_TIMEOUT_S:.0f} s "
                f"(lowest p95 deviation seen: {min_p95:.0f} counts vs "
                f"limit {still_max_dev:.0f}) - steady the hanging assembly "
                "and start again, or raise the stillness limit if the rig "
                "is genuinely still")
        samples = _collect_window(ser, STILL_WINDOW_S, acc_sensor_id)
        if len(samples) < 10:
            continue
        p95 = _p95_dev(samples)
        min_p95 = min(min_p95, p95)
        windows += 1
        if p95 <= still_max_dev:
            log(f"Rig still (p95 deviation {p95:.0f} counts) "
                f"after {time.monotonic() - t0:.1f} s.")
            return
        log(f"  ...still moving (p95 deviation {p95:.0f} counts)")
        # If the readings plateau just above the limit, the problem is
        # this rig's noise floor, not motion - say so instead of letting
        # the operator chase a stillness that already happened.
        if not hinted and windows >= 10 and min_p95 <= still_max_dev * 1.5:
            hinted = True
            log(f"  (p95 has plateaued around {min_p95:.0f} counts - if the "
                "rig looks still, that is this rig's noise floor; consider "
                f"raising the stillness limit above {min_p95:.0f})")


def collect_baseline(ser, duration_s: float,
                     acc_sensor_id: int) -> Tuple[Tuple[float, float, float],
                                                  float, float, float, float]:
    """Quiet-window baseline: per-axis means (for the per-axis deviation
    measure), the |a| mean (reported for continuity with the old logs),
    the detection level, and the noise mean/SD the CUSUM onset uses."""
    samples = _collect_window(ser, duration_s, acc_sensor_id)
    if len(samples) < 20:
        raise RuntimeError("Not enough ACC samples for the baseline - "
                           "is the stream running?")

    bx = statistics.fmean(s[0] for s in samples)
    by = statistics.fmean(s[1] for s in samples)
    bz = statistics.fmean(s[2] for s in samples)
    baseline_mag = statistics.fmean(
        math.sqrt(x * x + y * y + z * z) for x, y, z in samples)

    devs = sorted(math.sqrt((x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2)
                  for x, y, z in samples)
    noise_p95 = devs[int(0.95 * (len(devs) - 1))]
    threshold = max(MIN_THRESHOLD, NOISE_MULT * noise_p95)
    noise_mean = statistics.fmean(devs)
    noise_sd = statistics.stdev(devs)
    return (bx, by, bz), baseline_mag, threshold, noise_mean, noise_sd


def _interp_crossing(rel_times: List[float], deltas: List[float],
                     idx: int, level: float) -> float:
    """Linearly interpolated time at which the series crossed `level`
    between sample idx-1 and idx (standard sub-sample onset estimate);
    falls back to the sample time when interpolation is ill-posed."""
    if idx == 0:
        return rel_times[0]
    t0, d0 = rel_times[idx - 1], deltas[idx - 1]
    t1, d1 = rel_times[idx], deltas[idx]
    if d1 <= d0 or d0 >= level:
        return t1
    frac = (level - d0) / (d1 - d0)
    return t0 + frac * (t1 - t0)


def _find_onset(rel_times: List[float], deltas: List[float],
                noise_mean: float, noise_sd: float) -> Optional[float]:
    """CUSUM change-point onset (Page 1954): accumulate each sample's
    departure from the baseline-noise distribution and alarm once the
    accumulated evidence exceeds CUSUM_ALARM_SIGMA x sigma; the onset
    is dated at the sample where the alarmed excursion started - the
    first instant the curve departs from noise."""
    if noise_sd <= 0:
        return None
    slack = noise_mean + CUSUM_SLACK_SIGMA * noise_sd
    alarm = CUSUM_ALARM_SIGMA * noise_sd
    s = 0.0
    excursion_start = 0
    for i, d in enumerate(deltas):
        prev = s
        s = max(0.0, s + (d - slack))
        if s > 0.0 and prev == 0.0:
            excursion_start = i
        if s > alarm:
            return rel_times[excursion_start]
    return None


def run_single_trial(ser, trial_id: int, log: LogFn,
                     motor_index: int, acc_sensor_id: int,
                     should_stop: Callable[[], bool],
                     still_max_dev: float = STILL_MAX_DEV) -> TrialResult:
    log(f"\n===== Trial {trial_id} =====")
    # The previous trial's buzz leaves the hanging rig swinging - gate
    # every trial on stillness before taking its baseline.
    wait_until_still(ser, acc_sensor_id, log, should_stop,
                     still_max_dev=still_max_dev)
    (bx, by, bz), baseline_mag, threshold, noise_mean, noise_sd = \
        collect_baseline(ser, BASELINE_DURATION_S, acc_sensor_id)
    log(f"Baseline |a|: {baseline_mag:.2f}; noise {noise_mean:.0f}±"
        f"{noise_sd:.0f} counts; detection level: {threshold:.0f} counts")

    ser.reset_input_buffer()

    rel_times: List[float] = []
    deltas: List[float] = []
    hit_count = 0
    peak_delta = 0.0
    crossing_idx: Optional[int] = None
    post_samples = 0

    t_cmd = time.perf_counter()
    send(ser, f"S {1 << motor_index} {AMP}", wait_s=0.0)
    t_off = t_cmd + VIB_DURATION_S

    while time.perf_counter() - t_cmd < SEARCH_TIMEOUT_S:
        if time.perf_counter() >= t_off:
            send(ser, "X", wait_s=0.0)
            t_off = float("inf")

        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw, acc_sensor_id)
        if sample is None:
            continue

        x, y, z = sample
        # Per-axis deviation: first-order sensitive to vibration in ANY
        # direction, unlike | |a| - baseline | which nearly cancels for
        # motion perpendicular to gravity.
        delta = math.sqrt((x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2)
        peak_delta = max(peak_delta, delta)
        rel_times.append(time.perf_counter() - t_cmd)
        deltas.append(delta)

        if crossing_idx is None:
            if delta > threshold:
                hit_count += 1
                if hit_count >= CONSECUTIVE_HITS:
                    crossing_idx = len(deltas) - 1
            else:
                hit_count = 0
        else:
            # Crossing found - keep a short tail so the trace (and the
            # onset search) has the full ring-up context.
            post_samples += 1
            if post_samples >= POST_CROSSING_SAMPLES:
                break

    send(ser, "X", wait_s=0.0)

    onset_rel = _find_onset(rel_times, deltas, noise_mean, noise_sd)
    onset_ms = onset_rel * 1000.0 if onset_rel is not None else None
    # Recorded per trial for the CSV: the CUSUM alarm level in counts.
    onset_threshold = CUSUM_ALARM_SIGMA * noise_sd

    if crossing_idx is None:
        log(f"Trial {trial_id}: no detection within {SEARCH_TIMEOUT_S:.2f}s "
            f"(peak_delta={peak_delta:.1f} vs level {threshold:.0f})")
        return TrialResult(trial_id, "timeout", None, baseline_mag,
                           peak_delta, threshold, onset_ms, onset_threshold,
                           rel_times, deltas, None)

    detect_rel_time = _interp_crossing(rel_times, deltas, crossing_idx,
                                       threshold)
    delay_ms = detect_rel_time * 1000.0
    onset_str = f"{onset_ms:.2f}" if onset_ms is not None else "-"
    log(f"Trial {trial_id}: onset {onset_str} ms, "
        f"detection-level crossing {delay_ms:.2f} ms")
    return TrialResult(trial_id, "ok", delay_ms, baseline_mag, peak_delta,
                       threshold, onset_ms, onset_threshold,
                       rel_times, deltas, detect_rel_time)


# ==========================================
# Output
# ==========================================

def samples_path_for(csv_path: str) -> str:
    """delay_trials_<ts>.csv -> delay_samples_<ts>.csv (same folder)."""
    folder, name = os.path.split(csv_path)
    return os.path.join(folder, name.replace("delay_trials_", "delay_samples_", 1))


def meta_path_for(csv_path: str) -> str:
    """delay_trials_<ts>.csv -> delay_trials_<ts>.meta.json."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def save_trials_csv(path: str, results: List[TrialResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trial_id", "status", "onset_ms", "delay_ms",
                         "baseline_mag", "peak_delta", "onset_threshold",
                         "threshold"])
        for r in results:
            writer.writerow([
                r.trial_id, r.status,
                "" if r.onset_ms is None else f"{r.onset_ms:.3f}",
                "" if r.delay_ms is None else f"{r.delay_ms:.3f}",
                f"{r.baseline_mag:.2f}", f"{r.peak_delta:.2f}",
                f"{r.onset_threshold:.1f}", f"{r.threshold:.1f}"])


def save_samples_csv(path: str, results: List[TrialResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trial_id", "rel_time_s", "delta"])
        for r in results:
            for t, d in zip(r.rel_times, r.deltas):
                writer.writerow([r.trial_id, f"{t:.5f}", f"{d:.2f}"])


def _series_stats(values: List[float]) -> dict:
    return {
        "mean_ms": statistics.mean(values) if values else None,
        "sd_ms": statistics.stdev(values) if len(values) >= 2 else None,
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
    }


def delay_stats(results: List[TrialResult]) -> dict:
    ok = [r.delay_ms for r in results if r.delay_ms is not None]
    onsets = [r.onset_ms for r in results if r.onset_ms is not None]
    crossing = _series_stats(ok)
    onset = _series_stats(onsets)
    return {
        "n_ok": len(ok),
        "n_timeout": sum(1 for r in results if r.status == "timeout"),
        "n_onset": len(onsets),
        "onset_mean_ms": onset["mean_ms"],
        "onset_sd_ms": onset["sd_ms"],
        "onset_min_ms": onset["min_ms"],
        "onset_max_ms": onset["max_ms"],
        "mean_delay_ms": crossing["mean_ms"],
        "sd_delay_ms": crossing["sd_ms"],
        "min_delay_ms": crossing["min_ms"],
        "max_delay_ms": crossing["max_ms"],
    }


def save_plot(path: str, results: List[TrialResult], actuator_type: str,
              motor_index: int, threshold_hint: Optional[float] = None) -> None:
    stats = delay_stats(results)
    thresholds = [r.threshold for r in results if r.threshold > 0]
    threshold_line = (statistics.fmean(thresholds) if thresholds
                      else threshold_hint)

    fig = plt.figure(figsize=(12, 8))

    ax1 = fig.add_subplot(2, 1, 1)
    xs = [r.trial_id for r in results]
    ax1.plot(xs, [r.delay_ms if r.delay_ms is not None else float("nan")
                  for r in results], marker="o",
             label="Detection-level crossing")
    if any(r.onset_ms is not None for r in results):
        ax1.plot(xs, [r.onset_ms if r.onset_ms is not None else float("nan")
                      for r in results], marker="s",
                 label="Motion onset")
    if stats["mean_delay_ms"] is not None:
        ax1.axhline(stats["mean_delay_ms"], linestyle="--",
                    label=f"Crossing mean = {stats['mean_delay_ms']:.2f} ms")
    if stats["onset_mean_ms"] is not None:
        ax1.axhline(stats["onset_mean_ms"], linestyle="-.",
                    label=f"Onset mean = {stats['onset_mean_ms']:.2f} ms")
    ax1.set_title(f"Motor command -> ACC latency "
                  f"({actuator_type}, motor port {motor_index}, amp={AMP})")
    ax1.set_xlabel("Trial")
    ax1.set_ylabel("Latency (ms)")
    ax1.grid(True)
    ax1.legend(fontsize=8)

    ax2 = fig.add_subplot(2, 1, 2)
    any_trace = False
    for r in results:
        if r.rel_times and r.deltas:
            any_trace = True
            ax2.plot(r.rel_times, r.deltas, label=f"Trial {r.trial_id}")
            if r.detect_rel_time is not None:
                ax2.axvline(r.detect_rel_time, linestyle="--")
    if threshold_line is not None:
        ax2.axhline(threshold_line, linestyle=":",
                    label=f"Detection level ≈ {threshold_line:.0f}")
    # (No horizontal line for the onset: CUSUM is a change-point
    # statistic, not a level test - onsets are visible in panel 1.)
    ax2.set_title("Acceleration deviation after motor command"
                  if any_trace else
                  "Acceleration deviation after motor command "
                  "(no per-sample traces for this run)")
    ax2.set_xlabel("Time since command (s)")
    ax2.set_ylabel("Per-axis deviation (counts)")
    ax2.grid(True)
    if any_trace or threshold_line is not None:
        ax2.legend(fontsize=7, ncols=2)

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def save_meta(csv_path: str, samples_path: Optional[str], png_path: str,
              stamp: str, firmware, motor_index: int, acc_sensor_id: int,
              actuator_type: str, results: List[TrialResult],
              still_max_dev: float = STILL_MAX_DEV,
              pwm_freq_hz: int = DEFAULT_PWM_FREQ) -> str:
    meta = {
        "experiment": "motor_acc_delay",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "actuator_type": actuator_type,
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "amp": AMP,
            "vib_duration_s": VIB_DURATION_S,
            "num_trials": NUM_TRIALS,
            "baseline_duration_s": BASELINE_DURATION_S,
            "inter_trial_rest_s": INTER_TRIAL_REST_S,
            "search_timeout_s": SEARCH_TIMEOUT_S,
            "pwm_freq_hz": pwm_freq_hz,
            "detection": "per_axis_delta_adaptive_threshold",
            "min_threshold_counts": MIN_THRESHOLD,
            "noise_mult": NOISE_MULT,
            "consecutive_hits": CONSECUTIVE_HITS,
            "onset_criterion": ("CUSUM change-point (Page 1954): slack "
                                f"{CUSUM_SLACK_SIGMA} sigma, alarm "
                                f"{CUSUM_ALARM_SIGMA} sigma of baseline "
                                "deviation noise; onset = start of the "
                                "alarmed excursion"),
            "cusum_slack_sigma": CUSUM_SLACK_SIGMA,
            "cusum_alarm_sigma": CUSUM_ALARM_SIGMA,
            "crossing_estimate": "linear_interpolation",
            "mounting": "suspended_free_hanging",
            "still_window_s": STILL_WINDOW_S,
            "still_max_dev": still_max_dev,
            "acc_interval_ms": ACC_INTERVAL_MS,
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "samples_csv": (os.path.basename(samples_path)
                            if samples_path else None),
            "png": os.path.basename(png_path),
        },
        "result": delay_stats(results),
    }
    path = meta_path_for(csv_path)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
    return path


def load_meta(csv_path: str) -> Optional[dict]:
    path = meta_path_for(csv_path)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_results(csv_path: str) -> List[TrialResult]:
    """Read back a delay_trials_*.csv (summary rows, no traces)."""
    results: List[TrialResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(TrialResult(
                trial_id=int(row["trial_id"]),
                status=row["status"],
                delay_ms=float(row["delay_ms"]) if row["delay_ms"] else None,
                baseline_mag=float(row["baseline_mag"]),
                peak_delta=float(row["peak_delta"]),
                # Columns below were added over time; absent in older runs.
                threshold=float(row["threshold"]) if row.get("threshold") else 0.0,
                onset_ms=float(row["onset_ms"]) if row.get("onset_ms") else None,
                onset_threshold=(float(row["onset_threshold"])
                                 if row.get("onset_threshold") else 0.0),
            ))
    if not results:
        raise ValueError(f"No trial rows found in {csv_path}")
    return results


def load_samples(samples_path: str) -> Dict[int, Tuple[List[float], List[float]]]:
    traces: Dict[int, Tuple[List[float], List[float]]] = {}
    with open(samples_path, newline="") as f:
        for row in csv.DictReader(f):
            times, deltas = traces.setdefault(int(row["trial_id"]), ([], []))
            times.append(float(row["rel_time_s"]))
            deltas.append(float(row["delta"]))
    return traces


def render_csv(csv_path: str, out_png: str) -> dict:
    """Re-render the summary figure from a saved delay_trials_*.csv.
    Labels come from the sibling .meta.json when it exists; per-sample
    traces come from the sibling delay_samples file when it exists
    (backfilled pre-refactor runs only have summary rows)."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    actuator_type = params.get("actuator_type", "LRA")
    motor_index = params.get("motor_index", MOTOR_INDEX)
    # Old fixed-threshold runs recorded "threshold"; adaptive runs carry
    # per-trial thresholds in the CSV instead.
    threshold_hint = params.get("threshold")

    results = load_results(csv_path)
    samples_path = samples_path_for(csv_path)
    if os.path.exists(samples_path):
        traces = load_samples(samples_path)
        for r in results:
            if r.trial_id in traces:
                r.rel_times, r.deltas = traces[r.trial_id]
                if r.delay_ms is not None:
                    r.detect_rel_time = r.delay_ms / 1000.0

    save_plot(out_png, results, actuator_type, motor_index,
              threshold_hint=threshold_hint)
    stats = delay_stats(results)
    stats.update({"actuator_type": actuator_type, "csv_path": csv_path,
                  "png_path": out_png})
    return stats


# ==========================================
# Experiment
# ==========================================

def estimated_duration_s() -> float:
    return NUM_TRIALS * (BASELINE_DURATION_S + VIB_DURATION_S
                         + INTER_TRIAL_REST_S)


def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   actuator_type: str = "LRA",
                   still_max_dev: float = STILL_MAX_DEV) -> dict:
    """Run the delay measurement and write the output files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between trials;
    motor_index/acc_sensor_id/actuator_type pick the actuator under
    test (LRA on port 11, ERM on port 10 by wiring convention);
    still_max_dev adjusts the stillness gate for the rig's noise floor.
    Returns a summary dict with both onset and crossing statistics.
    """
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    log(f"Motor -> ACC delay test: {actuator_type} on motor port "
        f"{motor_index}, amp={AMP}, {NUM_TRIALS} trials")
    log("Suspend the motor+sensor pair freely in the air (any orientation); "
        "each trial waits until it hangs still before measuring.")
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s plus "
        "settling time.\n")

    ser = open_rig(log=log, interactive=interactive)
    results: List[TrialResult] = []
    try:
        send(ser, "X")
        # ERM needs kHz-range PWM to start (see ACTUATOR_PWM_HZ); the LRA
        # stays on its resonant boot default.
        pwm_freq_hz = ACTUATOR_PWM_HZ.get(actuator_type, DEFAULT_PWM_FREQ)
        send(ser, f"F {motor_index} {pwm_freq_hz}")
        log(f"PWM frequency on port {motor_index}: {pwm_freq_hz} Hz "
            f"({actuator_type})")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        for trial_id in range(1, NUM_TRIALS + 1):
            if should_stop():
                raise SweepAborted()
            results.append(run_single_trial(ser, trial_id, log,
                                            motor_index, acc_sensor_id,
                                            should_stop,
                                            still_max_dev=still_max_dev))
            progress(trial_id, NUM_TRIALS)
            if trial_id < NUM_TRIALS:
                time.sleep(INTER_TRIAL_REST_S)

        stats = delay_stats(results)
        log("\n===== Summary =====")
        for r in results:
            onset_str = f"{r.onset_ms:.2f}" if r.onset_ms is not None else "-"
            if r.delay_ms is None:
                log(f"Trial {r.trial_id}: status={r.status}, "
                    f"onset={onset_str} ms, "
                    f"baseline={r.baseline_mag:.2f}, "
                    f"peak_delta={r.peak_delta:.2f}")
            else:
                log(f"Trial {r.trial_id}: onset={onset_str} ms, "
                    f"crossing={r.delay_ms:.2f} ms, "
                    f"baseline={r.baseline_mag:.2f}, "
                    f"peak_delta={r.peak_delta:.2f}")
        if stats["onset_mean_ms"] is not None:
            log(f"Motion onset:            mean {stats['onset_mean_ms']:.2f} ms"
                + (f", SD {stats['onset_sd_ms']:.2f}"
                   if stats["onset_sd_ms"] is not None else "")
                + f" (n={stats['n_onset']})")
        if stats["mean_delay_ms"] is not None:
            log(f"Detection-level crossing: mean {stats['mean_delay_ms']:.2f} ms"
                + (f", SD {stats['sd_delay_ms']:.2f}"
                   if stats["sd_delay_ms"] is not None else "")
                + f" (n={stats['n_ok']})")
        if stats["mean_delay_ms"] is None and stats["onset_mean_ms"] is None:
            log("No successful detections, so no latency was computed.")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        stamp = str(int(time.time()))
        csv_path = os.path.join(OUTPUT_DIR, f"delay_trials_{stamp}.csv")
        samples_path = os.path.join(OUTPUT_DIR, f"delay_samples_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"delay_summary_{stamp}.png")
        save_trials_csv(csv_path, results)
        save_samples_csv(samples_path, results)
        save_plot(png_path, results, actuator_type, motor_index)
        meta_path = save_meta(csv_path, samples_path, png_path, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, actuator_type,
                              results, still_max_dev=still_max_dev,
                              pwm_freq_hz=pwm_freq_hz)
        log(f"Data:  {csv_path}")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")

        stats.update({"actuator_type": actuator_type, "csv_path": csv_path,
                      "png_path": png_path})
        return stats
    finally:
        try:
            send(ser, "X")
            # A non-default PWM frequency (ERM runs) must not outlive the
            # experiment - restore the boot default.
            send(ser, f"F {motor_index} {DEFAULT_PWM_FREQ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        log("Serial port closed.")


def main() -> None:
    run_experiment()


if __name__ == "__main__":
    main()
