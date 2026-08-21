"""LRA frequency-response sweep.

Drives the LRA (wired to motor port 0 for this experiment) at a fixed
amplitude while stepping the PWM frequency across a configurable range,
measures the resulting vibration with the LIS3DH accelerometer, and
reports the resonant frequency - the frequency with the highest RMS
acceleration.

Two passes: a coarse sweep over the full range, then a fine 1 Hz sweep
around the coarse peak.

Like every accelerometer experiment in this folder tree, each step is
scored with BOTH vibration-intensity metrics from
validation_experiments/acceleration_metrics.py - the recommended
demeaned three-axis vector RMS and the legacy baseline-subtracted
magnitude RMS - and the step's full three-axis sample series is saved,
so the resonance can be re-derived offline under either metric without
re-running the hardware. The metric only decides which curve is plotted
and which peak is reported; both are always measured.

Per-step results (CSV), the raw three-axis samples (compressed NPZ), the
response curve (PNG) and the run's parameter/result record (meta.json)
are saved under
data/validation_experiments/lra_resonance_intensity_calibration/.

Requires firmware >= v2.6.0 (the 'F' frequency command and the
"ACC,id,x,y,z" stream format).

Runs standalone (python lra_frequency_sweep.py) or through the
launcher's "Validation Experiments" section, which wraps
run_experiment() in a progress-bar window; both paths write the same
output files.
"""

import csv
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, fields
from typing import Callable, List, Optional

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt

try:
    from ..acceleration_metrics import (
        DEFAULT_METRIC,
        METRIC_CSV_COLUMNS,
        METRIC_NAMES,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        AccelerationMetrics,
        RawSampleRecorder,
        compute_acceleration_metrics,
        compute_baseline_magnitude,
        get_metric_value,
        metric_axis_label,
        metric_meta_block,
        metric_spec,
        metrics_available_in,
        normalise_metric,
        raw_path_for,
        select_metric,
    )
    from ..rig import SweepAborted, collect_samples, open_rig, send
    from .. import report
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from acceleration_metrics import (
        DEFAULT_METRIC,
        METRIC_CSV_COLUMNS,
        METRIC_NAMES,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        AccelerationMetrics,
        RawSampleRecorder,
        compute_acceleration_metrics,
        compute_baseline_magnitude,
        get_metric_value,
        metric_axis_label,
        metric_meta_block,
        metric_spec,
        metrics_available_in,
        normalise_metric,
        raw_path_for,
        select_metric,
    )
    from rig import SweepAborted, collect_samples, open_rig, send
    import report

try:
    from common import haptic_config as hc
except ImportError:  # direct execution from this folder - add main/ to the path
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from common import haptic_config as hc


# ==========================================
# Experiment configuration
# ==========================================

MOTOR_INDEX = 11         # LRA is wired to motor port 11 for this experiment
AMP = 128                # 50% duty = maximum AC fundamental for an LRA

ACC_SENSOR_ID = 0
ACC_INTERVAL_MS = 3      # LIS3DH ODR is 400 Hz, so keep intervals >= 3 ms

COARSE_START_HZ = 100
COARSE_STOP_HZ = 350
COARSE_STEP_HZ = 5
FINE_SPAN_HZ = 10        # fine pass covers coarse peak +/- this span
FINE_STEP_HZ = 1

BASELINE_S = 0.30        # quiet window measured before each step
SETTLE_S = 0.15          # motor-on settling before measuring (LRA ring-up)
MEASURE_S = 0.40         # vibration measurement window
REST_S = 0.15            # motor-off rest between steps

# Restored to the pin when the sweep ends: the FIRMWARE boot default
# (motor_driver.cpp), not the configured LRA default - this puts the rig
# back the way the firmware left it. The sweep itself covers
# COARSE_START_HZ..COARSE_STOP_HZ and is never narrowed by the config:
# finding the resonance is the entire point of this experiment.
DEFAULT_PWM_FREQ = hc.FIRMWARE_BOOT_PWM_HZ

# Data lives under main/data/ like every other experiment output.
OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "lra_resonance_intensity_calibration"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (steps_done, steps_total)


@dataclass
class StepResult:
    """One frequency step: its identity plus BOTH intensity metrics and
    the per-axis statistics behind them. Field names are CSV column
    names. Metric fields are Optional because a pre-refactor CSV carries
    only the legacy columns; those rows load with the vector fields as
    None so nothing pretends to derive them."""
    sweep_pass: str          # "coarse" or "fine"
    frequency_hz: int
    n_samples: int
    mean_x_counts: Optional[float] = None
    mean_y_counts: Optional[float] = None
    mean_z_counts: Optional[float] = None
    rms_x_counts: Optional[float] = None
    rms_y_counts: Optional[float] = None
    rms_z_counts: Optional[float] = None
    legacy_magnitude_rms_counts: Optional[float] = None
    legacy_magnitude_rms_ms2: Optional[float] = None
    vector_rms_counts: Optional[float] = None
    vector_rms_ms2: Optional[float] = None
    baseline_magnitude_counts: Optional[float] = None
    peak_magnitude_delta_counts: Optional[float] = None

    @classmethod
    def from_metrics(cls, sweep_pass: str, frequency_hz: int,
                     metrics: AccelerationMetrics) -> "StepResult":
        return cls(sweep_pass=sweep_pass, frequency_hz=int(frequency_hz),
                   **{c: getattr(metrics, c) for c in METRIC_CSV_COLUMNS})


CSV_COLUMNS = tuple(f.name for f in fields(StepResult))


# ==========================================
# Sweep
# ==========================================

def measure_step(ser, sweep_pass: str, freq: int, log: LogFn,
                 motor_index: int = MOTOR_INDEX,
                 acc_sensor_id: int = ACC_SENSOR_ID,
                 amp: int = AMP,
                 metric: str = DEFAULT_METRIC,
                 recorder: Optional[RawSampleRecorder] = None,
                 cell_id: int = -1) -> Optional[StepResult]:
    """One frequency step: quiet baseline, drive, measurement window.
    Both metrics come from the same window; `metric` only picks what the
    progress line prints. When a recorder is given, the raw baseline and
    vibration samples are kept for offline re-analysis."""
    send(ser, f"F {motor_index} {freq}")

    baseline = collect_samples(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        log(f"  {freq} Hz: no baseline samples - is the stream running?")
        return None
    baseline_magnitude = compute_baseline_magnitude(baseline)
    baseline_window = -1
    if recorder is not None:
        baseline_window = recorder.add_window(
            baseline, phase=PHASE_BASELINE, cell_id=cell_id,
            sweep_pass=sweep_pass, commanded_freq_hz=freq, commanded_amp=0)

    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_samples(ser, MEASURE_S, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  {freq} Hz: no vibration samples")
        return None
    if recorder is not None:
        recorder.add_window(vib, phase=PHASE_VIBRATION, cell_id=cell_id,
                            baseline_window_id=baseline_window,
                            sweep_pass=sweep_pass, commanded_freq_hz=freq,
                            commanded_amp=amp)

    metrics = compute_acceleration_metrics(vib, baseline_magnitude,
                                           MS2_PER_COUNT)
    result = StepResult.from_metrics(sweep_pass, freq, metrics)
    log(f"  {freq:4d} Hz  vector={metrics.vector_rms_counts:8.1f}  "
        f"legacy={metrics.legacy_magnitude_rms_counts:8.1f}  "
        f"peak={metrics.peak_magnitude_delta_counts:8.1f}  n={len(vib)}")
    return result


def resonance_step(results: List[StepResult], metric: str) -> StepResult:
    """The strongest step UNDER THE CHOSEN METRIC - switching the metric
    can legitimately move the peak, which is why both are stored."""
    scored = [(get_metric_value(r, metric, unit="counts"), r) for r in results]
    scored = [(v, r) for v, r in scored if v is not None]
    if not scored:
        raise ValueError(f"No step carries the {metric!r} metric.")
    return max(scored, key=lambda pair: pair[0])[1]


# ==========================================
# Output
# ==========================================

def save_csv(path: str, results: List[StepResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def save_plot(path: str, coarse: List[StepResult], fine: List[StepResult],
              resonance: StepResult, motor_index: int = MOTOR_INDEX,
              amp: int = AMP, metric: str = DEFAULT_METRIC) -> None:
    """The response curve, in raw counts of the SELECTED metric (the peak
    location is what matters here, and counts keep the noise floor
    visible; the CSV carries the m/s² value of every point too)."""
    def series(rows):
        xs, ys = [], []
        for r in rows:
            value = get_metric_value(r, metric, unit="counts")
            if value is not None:
                xs.append(r.frequency_hz)
                ys.append(value)
        return xs, ys

    fig, ax = plt.subplots(figsize=(10, 5))

    cx, cy = series(coarse)
    ax.plot(cx, cy, "o-", markersize=4,
            label=f"Coarse sweep ({COARSE_STEP_HZ} Hz steps)")
    if fine:
        fx, fy = series(fine)
        ax.plot(fx, fy, "s-", markersize=4,
                label=f"Fine sweep ({FINE_STEP_HZ} Hz steps)")

    ax.axvline(resonance.frequency_hz, linestyle="--", linewidth=1.5,
               label=f"Resonance: {resonance.frequency_hz} Hz")

    ax.set_title(f"LRA frequency response (motor port {motor_index}, amp={amp})")
    ax.set_xlabel("PWM frequency (Hz)")
    ax.set_ylabel(metric_axis_label(metric, unit="counts"))
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    """sweep_<ts>.csv -> sweep_<ts>.meta.json (same folder)."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def png_path_for(csv_path: str) -> str:
    """sweep_<ts>.csv -> frequency_response_<ts>.png (same folder) - the
    run's saved plot, so a re-render can overwrite it in place. The PNG
    uses a different prefix than the CSV, so only the trailing stamp is
    shared."""
    stamp = os.path.splitext(os.path.basename(csv_path))[0].rsplit("_", 1)[-1]
    return os.path.join(os.path.dirname(csv_path),
                        f"frequency_response_{stamp}.png")


def save_meta(csv_path: str, png_path: str, raw_path: Optional[str],
              stamp: str, firmware, motor_index: int, acc_sensor_id: int,
              amp: int, metric: str, resonance: StepResult) -> str:
    """Write the run's parameter/result record next to its CSV/PNG."""
    meta = {
        "experiment": "lra_frequency_sweep",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "amp": amp,
            "coarse_start_hz": COARSE_START_HZ,
            "coarse_stop_hz": COARSE_STOP_HZ,
            "coarse_step_hz": COARSE_STEP_HZ,
            "fine_span_hz": FINE_SPAN_HZ,
            "fine_step_hz": FINE_STEP_HZ,
            "baseline_s": BASELINE_S,
            "settle_s": SETTLE_S,
            "measure_s": MEASURE_S,
            "rest_s": REST_S,
            "acc_interval_ms": ACC_INTERVAL_MS,
        },
        # The rig's haptic configuration at run time, for traceability.
        # This sweep's frequency points are its own (COARSE/FINE steps) -
        # the configured LRA default never restricts what is measured.
        "haptic_config": {"snapshot": hc.config_snapshot()},
        "files": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path),
            "raw_acceleration": os.path.basename(raw_path) if raw_path else None,
        },
        "metrics": metric_meta_block(metric, raw_path is not None,
                                     os.path.basename(raw_path) if raw_path
                                     else None, MS2_PER_COUNT),
        # The headline result is always stated FOR THE SELECTED METRIC.
        "result": {
            "metric": normalise_metric(metric),
            "resonance_hz": resonance.frequency_hz,
            "resonance_counts": get_metric_value(resonance, metric, "counts"),
            "resonance_ms2": get_metric_value(resonance, metric, "ms2"),
            "peak_magnitude_delta_counts":
                resonance.peak_magnitude_delta_counts,
        },
    }
    path = meta_path_for(csv_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return path


def load_meta(csv_path: str) -> Optional[dict]:
    path = meta_path_for(csv_path)
    if not os.path.exists(path):
        return None  # e.g. a CSV predating the meta files
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _opt_float(row: dict, *keys: str) -> Optional[float]:
    """First present, non-empty column among `keys`, as a float."""
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return float(value)
    return None


def load_results(csv_path: str) -> List[StepResult]:
    """Read back a saved sweep_*.csv.

    Accepts both the current two-metric columns and the pre-refactor ones
    (rms_delta / peak_delta / baseline_mag), which map onto the legacy
    metric; the vector fields of such a row stay None."""
    results: List[StepResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(StepResult(
                sweep_pass=row["sweep_pass"],
                frequency_hz=int(row["frequency_hz"]),
                n_samples=int(row["n_samples"]),
                mean_x_counts=_opt_float(row, "mean_x_counts"),
                mean_y_counts=_opt_float(row, "mean_y_counts"),
                mean_z_counts=_opt_float(row, "mean_z_counts"),
                rms_x_counts=_opt_float(row, "rms_x_counts"),
                rms_y_counts=_opt_float(row, "rms_y_counts"),
                rms_z_counts=_opt_float(row, "rms_z_counts"),
                legacy_magnitude_rms_counts=_opt_float(
                    row, "legacy_magnitude_rms_counts", "rms_delta"),
                legacy_magnitude_rms_ms2=_opt_float(
                    row, "legacy_magnitude_rms_ms2"),
                vector_rms_counts=_opt_float(row, "vector_rms_counts"),
                vector_rms_ms2=_opt_float(row, "vector_rms_ms2"),
                baseline_magnitude_counts=_opt_float(
                    row, "baseline_magnitude_counts", "baseline_mag"),
                peak_magnitude_delta_counts=_opt_float(
                    row, "peak_magnitude_delta_counts", "peak_delta"),
            ))
    if not results:
        raise ValueError(f"No sweep rows found in {csv_path}")
    return results


def raw_file_for(csv_path: str) -> Optional[str]:
    """The run's raw three-axis sample file, or None for a run saved
    before raw data was kept."""
    path = raw_path_for(csv_path)
    return path if os.path.exists(path) else None


def available_metrics_for(csv_path: str) -> List[str]:
    """Which metrics a saved run can be plotted with (legacy only for a
    pre-refactor CSV - the vector RMS needs the per-axis samples)."""
    return metrics_available_in(load_results(csv_path), METRIC_NAMES)


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None,
               metric: Optional[str] = None) -> dict:
    """Re-render the response curve from a saved sweep_*.csv - same plot
    and same resonance rule (fine pass preferred) as the live run, under
    the requested metric (default: the run's stored choice; an old
    legacy-only CSV falls back to the legacy metric). Plot labels come
    from the run's sibling .meta.json when it exists. Returns a summary
    dict like run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    metrics_meta = meta.get("metrics", {}) if meta else {}
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    amp = params.get("amp", AMP)

    results = load_results(csv_path)
    supported = metrics_available_in(results, METRIC_NAMES)
    requested = metric or metrics_meta.get("selected_plot_metric")
    chosen = select_metric(results, requested)

    coarse = [r for r in results if r.sweep_pass == "coarse"]
    fine = [r for r in results if r.sweep_pass == "fine"]
    candidates = fine if fine else coarse
    resonance = resonance_step(candidates, chosen)
    save_plot(out_png, coarse, fine, resonance, motor_index=motor_index,
              amp=amp, metric=chosen)
    return {
        "metric": chosen,
        "requested_metric": normalise_metric(requested) if requested else None,
        "available_metrics": supported,
        "resonance_hz": resonance.frequency_hz,
        "resonance_counts": get_metric_value(resonance, chosen, "counts"),
        "resonance_ms2": get_metric_value(resonance, chosen, "ms2"),
        "peak_magnitude_delta_counts": resonance.peak_magnitude_delta_counts,
        "csv_path": csv_path,
        "png_path": out_png,
    }


def summary_report(csv_path: str, summary: Optional[dict] = None) -> list:
    """The sweep's statistics as text, rebuilt from a saved sweep_*.csv
    (+ its .meta.json): the parameters, every measured step under BOTH
    metrics, and the resonance the chart marks.

    The metric that decides the resonance is the one in `summary`
    (i.e. whatever the chart on screen is showing), so the text and the
    picture always name the same peak."""
    results = load_results(csv_path)
    meta = load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    metrics_meta = (meta or {}).get("metrics", {})
    chosen = normalise_metric(
        (summary or {}).get("metric")
        or metrics_meta.get("selected_plot_metric")
        or select_metric(results, None))

    coarse = [r for r in results if r.sweep_pass == "coarse"]
    fine = [r for r in results if r.sweep_pass == "fine"]
    candidates = fine if fine else coarse
    resonance = resonance_step(candidates, chosen)

    details = [
        f"motor port {params.get('motor_index', '?')}, amp "
        f"{params.get('amp', '?')}, ACC sensor "
        f"{params.get('acc_sensor_id', '?')}",
        f"coarse {params.get('coarse_start_hz', COARSE_START_HZ)}-"
        f"{params.get('coarse_stop_hz', COARSE_STOP_HZ)} Hz in "
        f"{params.get('coarse_step_hz', COARSE_STEP_HZ)} Hz steps, then "
        f"fine {params.get('fine_step_hz', FINE_STEP_HZ)} Hz around the peak",
        f"resonance decided by: {metric_spec(chosen).short_label}",
    ]
    lines = report.header("Frequency-sweep statistics", csv_path, meta, details)

    for label, steps in (("Coarse pass", coarse), ("Fine pass", fine)):
        if not steps:
            continue
        lines.append("")
        lines.append(f"  {label} ({len(steps)} steps):")
        lines.extend(report.table(
            ("freq Hz", "vector RMS counts", "vector m/s²",
             "legacy RMS counts", "legacy m/s²", "peak delta", "n"),
            [(r.frequency_hz,
              report.number(r.vector_rms_counts, 1),
              report.number(r.vector_rms_ms2, 3),
              report.number(r.legacy_magnitude_rms_counts, 1),
              report.number(r.legacy_magnitude_rms_ms2, 3),
              report.number(r.peak_magnitude_delta_counts, 1),
              r.n_samples) for r in steps],
            indent="    "))

    lines.append("")
    lines.append(f"Resonant frequency: {resonance.frequency_hz} Hz "
                 f"({metric_spec(chosen).short_label} = "
                 f"{get_metric_value(resonance, chosen, 'counts'):.1f} counts "
                 f"= {get_metric_value(resonance, chosen, 'ms2'):.3f} m/s²)")
    for name in METRIC_NAMES:
        if name == chosen:
            continue
        try:
            alt = resonance_step(candidates, name)
        except Exception:
            continue
        lines.append(f"  (for reference, {metric_spec(name).short_label} "
                     f"peaks at {alt.frequency_hz} Hz: "
                     f"{get_metric_value(alt, name, 'counts'):.1f} counts)")
    lines.append("The sweep measures every step above; the resonance is the "
                 "strongest of them under the selected metric.")
    return lines


# ==========================================
# Experiment
# ==========================================

def estimated_duration_s() -> float:
    coarse_steps = len(range(COARSE_START_HZ, COARSE_STOP_HZ + 1, COARSE_STEP_HZ))
    step_s = BASELINE_S + SETTLE_S + MEASURE_S + REST_S
    return (coarse_steps + 2 * FINE_SPAN_HZ // FINE_STEP_HZ + 1) * step_s


def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   amp: int = AMP,
                   plot_metric: str = DEFAULT_METRIC) -> dict:
    """Run the full two-pass sweep and write the output files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between steps;
    motor_index/acc_sensor_id/amp let it point the sweep at a different
    motor port / LIS3DH sensor / drive amplitude (amp 128 = maximum AC
    fundamental for an LRA - lower it only for a rig that must not be
    driven that hard); plot_metric picks which intensity metric the
    curve is drawn with and which peak is reported as the resonance
    (both metrics are measured and saved regardless, and the raw
    three-axis samples are kept so the choice can be revisited).
    Returns a summary dict (resonance + output paths).
    """
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)
    plot_metric = normalise_metric(plot_metric)

    coarse_freqs = list(range(COARSE_START_HZ, COARSE_STOP_HZ + 1, COARSE_STEP_HZ))
    # Fine-pass length is only known after the coarse peak; assume the
    # full +/- span for the initial progress total and correct it later.
    total_steps = len(coarse_freqs) + 2 * FINE_SPAN_HZ // FINE_STEP_HZ + 1
    done_steps = 0

    log(f"LRA frequency sweep on motor port {motor_index}, amp={amp}")
    log(f"Coarse: {COARSE_START_HZ}-{COARSE_STOP_HZ} Hz in {COARSE_STEP_HZ} Hz steps, "
        f"then fine +/-{FINE_SPAN_HZ} Hz in {FINE_STEP_HZ} Hz steps")
    log(f"Resonance metric: {metric_spec(plot_metric).short_label} "
        "(both metrics are measured and saved)")
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s. "
        "Keep the rig still during the sweep.\n")

    stamp = str(int(time.time()))
    recorder = RawSampleRecorder(
        run_id=f"lra_frequency_sweep_{stamp}",
        experiment="lra_frequency_sweep",
        ms2_per_count=MS2_PER_COUNT,
        sensor_id=acc_sensor_id,
        meta={"motor_index": motor_index, "amp": amp,
              "acc_interval_ms": ACC_INTERVAL_MS, "baseline_s": BASELINE_S,
              "settle_s": SETTLE_S, "measure_s": MEASURE_S, "rest_s": REST_S},
    )

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        log("Coarse sweep:")
        coarse: List[StepResult] = []
        cell_id = 0
        for freq in coarse_freqs:
            if should_stop():
                raise SweepAborted()
            step = measure_step(ser, "coarse", freq, log,
                                motor_index=motor_index,
                                acc_sensor_id=acc_sensor_id, amp=amp,
                                metric=plot_metric, recorder=recorder,
                                cell_id=cell_id)
            cell_id += 1
            done_steps += 1
            progress(done_steps, total_steps)
            if step is not None:
                coarse.append(step)
        if not coarse:
            raise RuntimeError("Coarse sweep produced no data - check wiring and stream")

        coarse_peak = resonance_step(coarse, plot_metric)
        log(f"\nCoarse peak: {coarse_peak.frequency_hz} Hz "
            f"({get_metric_value(coarse_peak, plot_metric, 'counts'):.1f} counts)\n")

        fine_freqs = [f for f in range(coarse_peak.frequency_hz - FINE_SPAN_HZ,
                                       coarse_peak.frequency_hz + FINE_SPAN_HZ + 1,
                                       FINE_STEP_HZ)
                      if COARSE_START_HZ <= f <= COARSE_STOP_HZ]
        total_steps = len(coarse_freqs) + len(fine_freqs)
        log("Fine sweep:")
        fine: List[StepResult] = []
        for freq in fine_freqs:
            if should_stop():
                raise SweepAborted()
            step = measure_step(ser, "fine", freq, log,
                                motor_index=motor_index,
                                acc_sensor_id=acc_sensor_id, amp=amp,
                                metric=plot_metric, recorder=recorder,
                                cell_id=cell_id)
            cell_id += 1
            done_steps += 1
            progress(done_steps, total_steps)
            if step is not None:
                fine.append(step)

        candidates = fine if fine else coarse
        resonance = resonance_step(candidates, plot_metric)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        csv_path = os.path.join(OUTPUT_DIR, f"sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"frequency_response_{stamp}.png")
        raw_pth = raw_path_for(csv_path)
        save_csv(csv_path, coarse + fine)
        recorder.save(raw_pth)
        save_plot(png_path, coarse, fine, resonance, motor_index=motor_index,
                  amp=amp, metric=plot_metric)
        meta_path = save_meta(csv_path, png_path, raw_pth, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, amp, plot_metric,
                              resonance)

        log(f"\n=== Resonant frequency: {resonance.frequency_hz} Hz ===")
        log(f"    {metric_spec(plot_metric).short_label} = "
            f"{get_metric_value(resonance, plot_metric, 'counts'):.1f} counts "
            f"= {get_metric_value(resonance, plot_metric, 'ms2'):.3f} m/s²; "
            f"peak_magnitude_delta="
            f"{resonance.peak_magnitude_delta_counts:.1f} counts")
        for name in METRIC_NAMES:
            if name == plot_metric:
                continue
            alt = resonance_step(candidates, name)
            log(f"    (for reference, {metric_spec(name).short_label} peaks at "
                f"{alt.frequency_hz} Hz: "
                f"{get_metric_value(alt, name, 'counts'):.1f} counts)")
        log(f"Data:    {csv_path}")
        log(f"Raw ACC: {raw_pth} ({recorder.n_samples} samples, "
            f"{recorder.n_windows} windows)")
        log(f"Plot:    {png_path}")
        log(f"Meta:    {meta_path}")
        log(f"\nTo lock this in, have the host send: F {motor_index} "
            f"{resonance.frequency_hz} after connecting.")

        return {
            "metric": plot_metric,
            "available_metrics": list(METRIC_NAMES),
            "resonance_hz": resonance.frequency_hz,
            "resonance_counts": get_metric_value(resonance, plot_metric, "counts"),
            "resonance_ms2": get_metric_value(resonance, plot_metric, "ms2"),
            "peak_magnitude_delta_counts": resonance.peak_magnitude_delta_counts,
            "csv_path": csv_path,
            "png_path": png_path,
            "raw_path": raw_pth,
        }
    finally:
        # Leave the rig in a clean default state whatever happened.
        try:
            send(ser, "X")
            send(ser, f"F {motor_index} {DEFAULT_PWM_FREQ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        log("Serial port closed, pin frequency restored to "
            f"{DEFAULT_PWM_FREQ} Hz.")


def main() -> None:
    run_experiment()


if __name__ == "__main__":
    main()
