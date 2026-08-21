"""LRA amplitude-response sweep at the resonant frequency.

Companion to lra_frequency_sweep.py: with the PWM frequency fixed at the
measured resonance (the configured LRA default frequency, config.json's
haptic block), this script steps the drive amplitude
(`amp` 0-128), measures the resulting RMS acceleration with the LIS3DH,
converts it to m/s^2, and reports the RECOMMENDED CUE AMP - the amp
whose measured intensity lands closest to the target haptic-cue
intensity.

"Recommended cue amp" means EXACTLY that: the amp closest to a target
INTENSITY. It is not the amp that produces the strongest vibration (that
is simply the top of the sweep range) and it is not an
actuator-optimal or resonance-related operating point.

Both vibration-intensity metrics from
validation_experiments/acceleration_metrics.py are measured per step -
the recommended demeaned three-axis vector RMS and the legacy
baseline-subtracted magnitude RMS - and every step's full three-axis
sample series is saved, so the recommendation can be recomputed offline
under either metric. Which metric drives the curve and the cue amp is a
plot-time choice; both are always recorded.

EACH METRIC HAS ITS OWN TARGET BAND (see CUE_TARGETS below). The two
metrics measure different things and therefore live on different
numeric scales - on this rig the same drive reads ~0.5 m/s^2 under the
legacy magnitude RMS and ~2.7 m/s^2 under the demeaned vector RMS,
because the legacy formula is only second-order sensitive to vibration
perpendicular to gravity while the vector RMS captures the full AC
energy. Sharing one band across both is therefore a category error: it
used to make switching the plot metric move the recommendation from
amp~52 to amp~4, which looked like a change in the actuator's operating
point but was only a change of ruler. Every consumer - the curve, the
shaded band, the cue amp, the in-band list, the legend, the title, the
meta and the GUI status line - reads its band from `cue_target(metric)`
so the two can never drift apart again.

A metric with no calibrated band gets NO recommendation: the plot and
the meta say "Target band not calibrated" rather than borrowing the
other metric's numbers.

Requires firmware >= v2.6.0. Uses the same wiring as the frequency
sweep: LRA on motor port 0, LIS3DH sensor 0 coupled to it.

Runs standalone (python lra_amplitude_sweep.py) or through the
launcher's "Validation Experiments" section, which wraps
run_experiment() in a progress-bar window; both paths write the same
output files.
"""

import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, asdict, fields
from typing import Callable, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt

try:
    from ..acceleration_metrics import (
        DEFAULT_METRIC,
        METRIC_CSV_COLUMNS,
        METRIC_LEGACY_MAGNITUDE_RMS,
        METRIC_NAMES,
        METRIC_VECTOR_RMS,
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
        METRIC_LEGACY_MAGNITUDE_RMS,
        METRIC_NAMES,
        METRIC_VECTOR_RMS,
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

MOTOR_INDEX = 11         # LRA port (same rig as the frequency sweep)
# The frequency the amplitudes are measured at: the LRA's configured
# resonance (config.json haptic.lra.default_frequency, adopted from
# lra_frequency_sweep's result). Read at import as the command-line /
# control default - the GUI's PWM-freq box overrides it per run, and
# only that value is ever sent.
FREQ_HZ = hc.get_default_frequency(hc.LRA)
# The resonance every run saved before freq_hz was recorded used; used
# only to label such a run when re-rendering it.
HISTORICAL_FREQ_HZ = 224

AMP_VALUES = list(range(4, 129, 4))   # 4..128: the LRA's monotonic range

ACC_SENSOR_ID = 0
ACC_INTERVAL_MS = 3

BASELINE_S = 0.30
SETTLE_S = 0.15
MEASURE_S = 0.40
REST_S = 0.15

# ==========================================
# Target cue intensity - PER METRIC
# ==========================================
#
# THE single definition of every target band in this experiment. Nothing
# else in this file (or in the GUI, or in the plots) may hard-code a band
# or an aim point: they all go through cue_target(metric). The two
# metrics are different rulers, so each needs its own numbers - see the
# module docstring.

CALIBRATION_HISTORICAL = "historical"      # adopted with the original study
CALIBRATION_PROVISIONAL = "provisional"    # derived from data, not yet perceptual
CALIBRATION_NONE = "not_calibrated"        # no band exists for this metric

CALIBRATION_LABELS = {
    CALIBRATION_HISTORICAL: "historical calibration",
    CALIBRATION_PROVISIONAL: "provisional calibration",
    CALIBRATION_NONE: "not calibrated",
}

#: Shown wherever a band would be, for a metric that has none.
UNCALIBRATED_LABEL = "Target band not calibrated"


@dataclass(frozen=True)
class CueTarget:
    """The target haptic-cue intensity for ONE metric: the band, the aim
    point inside it, and where those numbers came from.

    `band_ms2`/`target_ms2` are None exactly when the metric has no
    calibrated target; `calibrated` is the only test callers should use.
    An uncalibrated metric yields NO recommended cue amp - the other
    metric's thresholds are never substituted."""
    metric: str
    band_ms2: Optional[Tuple[float, float]]
    target_ms2: Optional[float]
    calibration_status: str
    source: str

    @property
    def calibrated(self) -> bool:
        return (self.band_ms2 is not None and self.target_ms2 is not None
                and self.calibration_status != CALIBRATION_NONE)

    @property
    def status_label(self) -> str:
        return CALIBRATION_LABELS.get(self.calibration_status,
                                      self.calibration_status)

    @property
    def band_label(self) -> str:
        """"0.4-0.6 m/s²" - or the not-calibrated wording."""
        if not self.calibrated:
            return UNCALIBRATED_LABEL
        return f"{self.band_ms2[0]:g}-{self.band_ms2[1]:g} m/s²"

    def describe(self) -> str:
        """Legend / log wording: band, aim point and calibration status."""
        if not self.calibrated:
            return f"{UNCALIBRATED_LABEL} for the {self.short_label}"
        return (f"Target cue band {self.band_label} "
                f"(aim {self.target_ms2:g} m/s², {self.status_label})")

    @property
    def short_label(self) -> str:
        return metric_spec(self.metric).short_label

    def contains(self, value: Optional[float]) -> bool:
        if not self.calibrated or value is None:
            return False
        return self.band_ms2[0] <= value <= self.band_ms2[1]

    def as_meta(self) -> dict:
        """The block every .meta.json records for the selected metric."""
        return {
            "target_band_ms2": (list(self.band_ms2) if self.band_ms2
                                else None),
            "target_ms2": self.target_ms2,
            "target_calibration_status": self.calibration_status,
            "target_calibration_source": self.source,
            "target_band_calibrated": self.calibrated,
        }


CUE_TARGETS: Dict[str, CueTarget] = {
    METRIC_LEGACY_MAGNITUDE_RMS: CueTarget(
        metric=METRIC_LEGACY_MAGNITUDE_RMS,
        band_ms2=(0.4, 0.6),
        target_ms2=0.5,
        calibration_status=CALIBRATION_HISTORICAL,
        source=("historical calibration: the perception-based band the "
                "project adopted amp=64 from. Fingertip vibrotactile "
                "detection threshold around 200-250 Hz (Pacinian peak "
                "sensitivity) is ~0.1-0.4 m/s^2 RMS, and a clear but "
                "non-annoying cue sits a little above it. (ISO 2631-1 is "
                "whole-body and limited to 0.5-80 Hz; ISO 5349-1 is an "
                "occupational-exposure standard - neither prescribes a "
                "haptic-cue level, so this band is perception-based with "
                "the ISO comfort descriptors as a conservative "
                "cross-check.)"),
    ),
    METRIC_VECTOR_RMS: CueTarget(
        metric=METRIC_VECTOR_RMS,
        band_ms2=(2.4, 3.0),
        target_ms2=2.7,
        calibration_status=CALIBRATION_PROVISIONAL,
        source=("PROVISIONAL calibration: estimated from the demeaned "
                "vector RMS this rig measured at amp~48-56 (2.55-2.94 "
                "m/s^2 across the existing runs), i.e. the drive range "
                "the historical legacy-metric band already selects. It "
                "is a rescaling of the historical band onto this metric's "
                "scale, NOT an independent perceptual calibration - "
                "expect it to be re-measured."),
    ),
}


def cue_target(metric: Optional[str]) -> CueTarget:
    """The target cue intensity for `metric`. Always returns a CueTarget:
    a metric with no entry gets an UNCALIBRATED one, so callers branch on
    `.calibrated` rather than on None, and can never silently fall back
    to another metric's band."""
    name = normalise_metric(metric)
    target = CUE_TARGETS.get(name)
    if target is not None:
        return target
    return CueTarget(
        metric=name, band_ms2=None, target_ms2=None,
        calibration_status=CALIBRATION_NONE,
        source=("no target cue intensity has been calibrated for this "
                "metric, so no cue amp is recommended"),
    )


def cue_targets_meta() -> dict:
    """Every calibrated band, recorded in each run's meta so a saved run
    always states the definitions it was produced under."""
    return {name: dict(target.as_meta(), metric=name)
            for name, target in CUE_TARGETS.items()}


# Data lives under main/data/ like every other experiment output.
OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "lra_resonance_intensity_calibration"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (steps_done, steps_total)


@dataclass
class StepResult:
    """One amplitude step: its identity plus BOTH intensity metrics and
    the per-axis statistics behind them. Field names are CSV column
    names; metric fields are Optional so a pre-refactor CSV (legacy
    columns only) loads with the vector fields left as None."""
    amp: int
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
    def from_metrics(cls, amp: int,
                     metrics: AccelerationMetrics) -> "StepResult":
        return cls(amp=int(amp),
                   **{c: getattr(metrics, c) for c in METRIC_CSV_COLUMNS})


CSV_COLUMNS = tuple(f.name for f in fields(StepResult))


# ==========================================
# Sweep
# ==========================================

def measure_amp(ser, amp: int, log: LogFn,
                motor_index: int = MOTOR_INDEX,
                acc_sensor_id: int = ACC_SENSOR_ID,
                recorder: Optional[RawSampleRecorder] = None,
                cell_id: int = -1,
                freq_hz: float = FREQ_HZ) -> Optional[StepResult]:
    """One amplitude step: quiet baseline, drive, measurement window.
    Both metrics are computed from the same window; when a recorder is
    given the raw samples of both windows are kept for offline
    re-analysis."""
    baseline = collect_samples(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        log(f"  amp={amp}: no baseline samples - is the stream running?")
        return None
    baseline_magnitude = compute_baseline_magnitude(baseline)
    baseline_window = -1
    if recorder is not None:
        baseline_window = recorder.add_window(
            baseline, phase=PHASE_BASELINE, cell_id=cell_id,
            commanded_freq_hz=freq_hz, commanded_amp=0)

    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_samples(ser, MEASURE_S, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  amp={amp}: no vibration samples")
        return None
    if recorder is not None:
        recorder.add_window(vib, phase=PHASE_VIBRATION, cell_id=cell_id,
                            baseline_window_id=baseline_window,
                            commanded_freq_hz=freq_hz, commanded_amp=amp)

    metrics = compute_acceleration_metrics(vib, baseline_magnitude,
                                           MS2_PER_COUNT)
    result = StepResult.from_metrics(amp, metrics)
    log(f"  amp={amp:3d}  vector={metrics.vector_rms_ms2:5.3f} m/s^2  "
        f"legacy={metrics.legacy_magnitude_rms_ms2:5.3f} m/s^2  n={len(vib)}")
    return result


def recommended_cue_amp(results: List[StepResult],
                        metric: str) -> Optional[StepResult]:
    """The amp whose measured intensity is CLOSEST TO THE TARGET CUE
    INTENSITY of `metric` - the "amp closest to target intensity", not
    the amp that vibrates hardest.

    Each metric aims at its own target (cue_target), so the two can
    legitimately pick different amps; that is why the metric is explicit.
    Returns None when the metric has no calibrated target band - an
    uncalibrated metric gets no recommendation rather than one computed
    against somebody else's numbers.

    Raises ValueError when no step carries the metric at all (a
    legacy-only CSV asked for the vector RMS)."""
    scored = [(get_metric_value(r, metric, "ms2"), r) for r in results]
    scored = [(v, r) for v, r in scored if v is not None]
    if not scored:
        raise ValueError(f"No step carries the {metric!r} metric.")
    target = cue_target(metric)
    if not target.calibrated:
        return None
    return min(scored, key=lambda pair: abs(pair[0] - target.target_ms2))[1]


def in_band(results: List[StepResult], metric: str) -> List[StepResult]:
    """Every step inside `metric`'s OWN target band (empty when that
    metric has no calibrated band)."""
    target = cue_target(metric)
    return [r for r in results
            if target.contains(get_metric_value(r, metric, "ms2"))]


# ==========================================
# Output
# ==========================================

def save_csv(path: str, results: List[StepResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def save_plot(path: str, results: List[StepResult],
              recommended: Optional[StepResult],
              motor_index: int = MOTOR_INDEX, freq_hz: int = FREQ_HZ,
              metric: str = DEFAULT_METRIC) -> None:
    """The response curve for `metric`, with THAT METRIC'S target band.

    The shaded band, the cue-amp line, the legend and the subtitle all
    come from cue_target(metric), so switching the plot metric moves all
    of them together; a metric with no calibrated band gets the curve and
    an explicit "not calibrated" note instead of somebody else's band."""
    target = cue_target(metric)
    amps, ms2 = [], []
    for r in results:
        value = get_metric_value(r, metric, "ms2")
        if value is not None:
            amps.append(r.amp)
            ms2.append(value)
    max_ms2 = max(ms2)

    fig, ax = plt.subplots(figsize=(10, 5.6))

    # Theoretical shape for reference: intensity ~ sin(pi*amp/255),
    # scaled to the measured maximum.
    theory = [max_ms2 * math.sin(math.pi * a / 255) / math.sin(math.pi * max(amps) / 255)
              for a in amps]
    ax.plot(amps, theory, "--", linewidth=1.2, alpha=0.7,
            label="sin(π·amp/255) model (scaled)")

    ax.plot(amps, ms2, "o-", markersize=4,
            label=f"Measured {metric_spec(metric).short_label}")

    if target.calibrated:
        ax.axhspan(target.band_ms2[0], target.band_ms2[1], alpha=0.15,
                   color="tab:green", label=target.describe())
        ax.axhline(target.target_ms2, linestyle=":", linewidth=1.2,
                   color="tab:green",
                   label=f"Target intensity = {target.target_ms2:g} m/s²")
    if recommended is not None:
        recommended_ms2 = get_metric_value(recommended, metric, "ms2")
        ax.axvline(recommended.amp, linestyle="--", linewidth=1.5,
                   color="tab:red",
                   label=f"Recommended cue amp = {recommended.amp} "
                         f"({recommended_ms2:.2f} m/s²) — amp closest to "
                         "target intensity")
    else:
        ax.text(0.02, 0.95, f"{UNCALIBRATED_LABEL} for the "
                            f"{metric_spec(metric).short_label}\n"
                            "— no cue amp recommended",
                transform=ax.transAxes, va="top", fontsize=9,
                color="tab:red",
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.8))

    ax.set_title(f"LRA amplitude response at {freq_hz} Hz "
                 f"(motor port {motor_index})\n"
                 f"{metric_spec(metric).short_label} — "
                 + (f"target {target.band_label}, {target.status_label}"
                    if target.calibrated else UNCALIBRATED_LABEL),
                 fontsize=11)
    ax.set_xlabel("amp (PWM duty, 0-255 scale)")
    ax.set_ylabel(metric_axis_label(metric))
    ax.legend(fontsize=8)
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    """amp_sweep_<ts>.csv -> amp_sweep_<ts>.meta.json (same folder)."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def png_path_for(csv_path: str) -> str:
    """amp_sweep_<ts>.csv -> amplitude_response_<ts>.png (same folder) -
    the run's saved plot, so a re-render can overwrite it in place. The
    PNG uses a different prefix than the CSV, so only the trailing stamp
    is shared."""
    stamp = os.path.splitext(os.path.basename(csv_path))[0].rsplit("_", 1)[-1]
    return os.path.join(os.path.dirname(csv_path),
                        f"amplitude_response_{stamp}.png")


def result_block(results: List[StepResult], metric: str) -> dict:
    """The headline result FOR ONE METRIC: its own target band, where
    that band came from, the cue amp it selects and every in-band amp.

    Shared by save_meta(), render_csv() and run_experiment() so the meta
    file, the re-render and the GUI status line can never disagree about
    which band produced which number."""
    metric = normalise_metric(metric)
    target = cue_target(metric)
    recommended = recommended_cue_amp(results, metric)
    band = in_band(results, metric)
    recommended_ms2 = (None if recommended is None
                       else get_metric_value(recommended, metric, "ms2"))
    block = {
        "metric": metric,
        "metric_label": metric_spec(metric).short_label,
        "recommended_cue_amp": None if recommended is None else recommended.amp,
        "recommended_cue_amp_ms2": recommended_ms2,
        "in_band_amps": [r.amp for r in band],
        "recommendation_note": (
            "the amp whose measured intensity is closest to this metric's "
            "target cue intensity - NOT the amp that produces the "
            "strongest vibration"),
    }
    block.update(target.as_meta())
    if recommended is None:
        block["recommendation_unavailable_reason"] = target.source
    # Long-standing key names, kept so older readers of a meta file (and
    # the GUI's saved summaries) keep working. Same numbers.
    block["recommended_amp"] = block["recommended_cue_amp"]
    block["recommended_ms2"] = block["recommended_cue_amp_ms2"]
    return block


def save_meta(csv_path: str, png_path: str, raw_path: Optional[str],
              stamp: str, firmware, motor_index: int, acc_sensor_id: int,
              freq_hz: int, metric: str,
              results: List[StepResult]) -> str:
    """Write the run's parameter/result record next to its CSV/PNG."""
    target = cue_target(metric)
    meta = {
        "experiment": "lra_amplitude_sweep",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "freq_hz": freq_hz,
            "amp_values": AMP_VALUES,
            # Where freq_hz came from: the configured LRA default, or a
            # value typed into the window. The amp points are the
            # experiment's own sweep and never come from the config.
            "freq_source": hc.value_source(freq_hz,
                                           hc.get_default_frequency(hc.LRA)),
            # The band that actually produced this run's result, plus
            # every band defined at save time - the two metrics have
            # DIFFERENT bands and a saved run must say which it used.
            "target_band_ms2": (list(target.band_ms2) if target.band_ms2
                                else None),
            "target_ms2": target.target_ms2,
            "cue_targets_by_metric": cue_targets_meta(),
            "ms2_per_count": MS2_PER_COUNT,
            "baseline_s": BASELINE_S,
            "settle_s": SETTLE_S,
            "measure_s": MEASURE_S,
            "rest_s": REST_S,
            "acc_interval_ms": ACC_INTERVAL_MS,
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path),
            "raw_acceleration": os.path.basename(raw_path) if raw_path else None,
        },
        "metrics": metric_meta_block(metric, raw_path is not None,
                                     os.path.basename(raw_path) if raw_path
                                     else None, MS2_PER_COUNT),
        # The rig's haptic configuration at run time, for traceability.
        # The amp points swept are the experiment's own (AMP_VALUES).
        "haptic_config": {"snapshot": hc.config_snapshot()},
        # The headline result is always stated FOR THE SELECTED METRIC.
        "result": result_block(results, metric),
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
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return float(value)
    return None


def load_results(csv_path: str) -> List[StepResult]:
    """Read back a saved amp_sweep_*.csv.

    Accepts both the current two-metric columns and the pre-refactor ones
    (rms_delta_counts / rms_ms2 / peak_delta_counts / baseline_mag),
    which map onto the legacy metric; the vector fields of such a row
    stay None."""
    results: List[StepResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(StepResult(
                amp=int(row["amp"]),
                n_samples=int(row["n_samples"]),
                mean_x_counts=_opt_float(row, "mean_x_counts"),
                mean_y_counts=_opt_float(row, "mean_y_counts"),
                mean_z_counts=_opt_float(row, "mean_z_counts"),
                rms_x_counts=_opt_float(row, "rms_x_counts"),
                rms_y_counts=_opt_float(row, "rms_y_counts"),
                rms_z_counts=_opt_float(row, "rms_z_counts"),
                legacy_magnitude_rms_counts=_opt_float(
                    row, "legacy_magnitude_rms_counts", "rms_delta_counts"),
                legacy_magnitude_rms_ms2=_opt_float(
                    row, "legacy_magnitude_rms_ms2", "rms_ms2"),
                vector_rms_counts=_opt_float(row, "vector_rms_counts"),
                vector_rms_ms2=_opt_float(row, "vector_rms_ms2"),
                baseline_magnitude_counts=_opt_float(
                    row, "baseline_magnitude_counts", "baseline_mag"),
                peak_magnitude_delta_counts=_opt_float(
                    row, "peak_magnitude_delta_counts", "peak_delta_counts"),
            ))
    if not results:
        raise ValueError(f"No sweep rows found in {csv_path}")
    return results


def raw_file_for(csv_path: str) -> Optional[str]:
    path = raw_path_for(csv_path)
    return path if os.path.exists(path) else None


def available_metrics_for(csv_path: str) -> List[str]:
    """Which metrics a saved run can be plotted with (legacy only for a
    pre-refactor CSV - the vector RMS needs the per-axis samples)."""
    return metrics_available_in(load_results(csv_path), METRIC_NAMES)


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None,
               metric: Optional[str] = None) -> dict:
    """Re-render the response curve from a saved amp_sweep_*.csv - same
    plot and same recommendation rule as the live run, under the
    requested metric (default: the run's stored choice; an old
    legacy-only CSV falls back to the legacy metric). Plot labels come
    from the run's sibling .meta.json when it exists. Returns a summary
    dict like run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    metrics_meta = meta.get("metrics", {}) if meta else {}
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    freq_hz = params.get("freq_hz", HISTORICAL_FREQ_HZ)

    results = load_results(csv_path)
    supported = metrics_available_in(results, METRIC_NAMES)
    requested = metric or metrics_meta.get("selected_plot_metric")
    chosen = select_metric(results, requested)

    # Re-derived from the CHOSEN metric's own target band, never read
    # back from the meta's stored result: re-plotting an old run under
    # the other metric must move the band and the cue amp together.
    recommended = recommended_cue_amp(results, chosen)
    save_plot(out_png, results, recommended, motor_index=motor_index,
              freq_hz=freq_hz, metric=chosen)
    summary = dict(result_block(results, chosen),
                   requested_metric=(normalise_metric(requested)
                                     if requested else None),
                   available_metrics=supported,
                   csv_path=csv_path,
                   png_path=out_png)
    return summary


def summary_report(csv_path: str, summary: Optional[dict] = None) -> list:
    """The sweep's statistics as text, rebuilt from a saved
    amp_sweep_*.csv (+ its .meta.json): the parameters, every measured
    amp under BOTH metrics, and the recommended cue amp.

    Every metric-dependent number (target band, recommendation, in-band
    amps) is derived for the metric the displayed chart uses, so the two
    can never disagree - the same rule render_csv() follows."""
    results = load_results(csv_path)
    meta = load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    metrics_meta = (meta or {}).get("metrics", {})
    chosen = normalise_metric(
        (summary or {}).get("metric")
        or metrics_meta.get("selected_plot_metric")
        or select_metric(results, None))
    target = cue_target(chosen)
    recommended = recommended_cue_amp(results, chosen)
    band = in_band(results, chosen)

    details = [
        f"motor port {params.get('motor_index', '?')}, drive frequency "
        f"{params.get('freq_hz', '?')} Hz, ACC sensor "
        f"{params.get('acc_sensor_id', '?')}",
        f"{len(results)} amp steps; intensity metric: "
        f"{metric_spec(chosen).short_label}",
        f"target cue intensity: {target.band_label} "
        f"({target.status_label})",
    ]
    if params.get("freq_source"):
        details.append(f"drive frequency source: {params['freq_source']}")
    lines = report.header("Amplitude-sweep statistics", csv_path, meta, details)

    lines.append("")
    lines.extend(report.table(
        ("amp", "vector m/s²", "vector counts", "legacy m/s²",
         "legacy counts", "in band", "n"),
        [(r.amp,
          report.number(r.vector_rms_ms2, 3),
          report.number(r.vector_rms_counts, 1),
          report.number(r.legacy_magnitude_rms_ms2, 3),
          report.number(r.legacy_magnitude_rms_counts, 1),
          "yes" if r in band else "",
          r.n_samples) for r in results]))

    lines.append("")
    if recommended is None:
        lines.append(f"{UNCALIBRATED_LABEL} for the "
                     f"{metric_spec(chosen).short_label} - no cue amp is "
                     "recommended (another metric's band is never "
                     "substituted).")
        lines.append(f"  {target.source}")
    else:
        value = get_metric_value(recommended, chosen, "ms2")
        lines.append(f"Recommended cue amp: {recommended.amp} "
                     f"({value:.2f} m/s², {metric_spec(chosen).short_label}) "
                     f"- closest to the {target.band_label} target band "
                     f"[{target.status_label}]")
        lines.append("  (the amp closest to the target cue INTENSITY - not "
                     "the amp that vibrates hardest)")
    if band:
        lines.append("In band: " + ", ".join(
            f"{r.amp} ({get_metric_value(r, chosen, 'ms2'):.2f} m/s²)"
            for r in band))
    for name in METRIC_NAMES:
        if name == chosen:
            continue
        # Each metric is judged against ITS OWN band - different rulers.
        alt_target = cue_target(name)
        alt = recommended_cue_amp(results, name)
        if alt is None:
            lines.append(f"  (for reference, the {metric_spec(name).short_label} "
                         "has no calibrated target band, so it recommends "
                         "nothing)")
        else:
            lines.append(f"  (for reference, {metric_spec(name).short_label} "
                         f"aims at {alt_target.band_label} and would "
                         f"recommend amp={alt.amp} at "
                         f"{get_metric_value(alt, name, 'ms2'):.2f} m/s²)")
    return lines


# ==========================================
# Experiment
# ==========================================

def estimated_duration_s() -> float:
    step_s = BASELINE_S + SETTLE_S + MEASURE_S + REST_S
    return len(AMP_VALUES) * step_s


def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   freq_hz: int = FREQ_HZ,
                   plot_metric: str = DEFAULT_METRIC) -> dict:
    """Run the amplitude sweep and write the output files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between steps;
    motor_index/acc_sensor_id/freq_hz let it point the sweep at a
    different motor port / LIS3DH sensor / PWM frequency (change
    freq_hz only after re-measuring the resonance with the frequency
    sweep - amplitudes measured off-resonance are meaningless for an
    LRA); plot_metric picks which intensity metric the curve and the
    recommended cue amp are derived from - each metric aims at its OWN
    target band (cue_target), so switching it moves the band and the
    recommendation together (both metrics are measured and saved
    regardless, along with the raw three-axis samples).
    Returns a summary dict (the selected metric's result_block plus the
    output paths); `recommended_cue_amp` is None when that metric has no
    calibrated target band.
    """
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)
    plot_metric = normalise_metric(plot_metric)

    log(f"LRA amplitude sweep at {freq_hz} Hz on motor port {motor_index}")
    log(f"amp values: {AMP_VALUES[0]}..{AMP_VALUES[-1]} in steps of "
        f"{AMP_VALUES[1] - AMP_VALUES[0]}")
    target = cue_target(plot_metric)
    log(f"Recommendation metric: {metric_spec(plot_metric).short_label} "
        "(both metrics are measured and saved)")
    log(f"Target cue intensity:  {target.band_label}"
        + (f", aim {target.target_ms2:g} m/s² ({target.status_label})"
           if target.calibrated else " - no cue amp will be recommended"))
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s. "
        "Keep the rig still during the sweep.\n")

    stamp = str(int(time.time()))
    recorder = RawSampleRecorder(
        run_id=f"lra_amplitude_sweep_{stamp}",
        experiment="lra_amplitude_sweep",
        ms2_per_count=MS2_PER_COUNT,
        sensor_id=acc_sensor_id,
        meta={"motor_index": motor_index, "freq_hz": freq_hz,
              "acc_interval_ms": ACC_INTERVAL_MS, "baseline_s": BASELINE_S,
              "settle_s": SETTLE_S, "measure_s": MEASURE_S, "rest_s": REST_S},
    )

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, f"F {motor_index} {freq_hz}")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        results: List[StepResult] = []
        for i, amp in enumerate(AMP_VALUES):
            if should_stop():
                raise SweepAborted()
            step = measure_amp(ser, amp, log,
                               motor_index=motor_index,
                               acc_sensor_id=acc_sensor_id,
                               recorder=recorder, cell_id=i, freq_hz=freq_hz)
            progress(i + 1, len(AMP_VALUES))
            if step is not None:
                results.append(step)
        if not results:
            raise RuntimeError("Sweep produced no data - check wiring and stream")

        result = result_block(results, plot_metric)
        recommended = recommended_cue_amp(results, plot_metric)
        band = in_band(results, plot_metric)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        csv_path = os.path.join(OUTPUT_DIR, f"amp_sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"amplitude_response_{stamp}.png")
        raw_pth = raw_path_for(csv_path)
        save_csv(csv_path, results)
        recorder.save(raw_pth)
        save_plot(png_path, results, recommended, motor_index=motor_index,
                  freq_hz=freq_hz, metric=plot_metric)
        meta_path = save_meta(csv_path, png_path, raw_pth, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, freq_hz,
                              plot_metric, results)

        if recommended is None:
            log(f"\n=== {UNCALIBRATED_LABEL} for the "
                f"{metric_spec(plot_metric).short_label} - no cue amp "
                "recommended ===")
            log(f"  {target.source}")
        else:
            log(f"\n=== Recommended cue amp: {recommended.amp} "
                f"({result['recommended_cue_amp_ms2']:.2f} m/s² "
                f"{metric_spec(plot_metric).short_label}, target "
                f"{target.target_ms2:g} m/s², {target.status_label}) ===")
            log("  (the amp closest to the target cue INTENSITY - not the "
                "amp that vibrates hardest)")
        if band:
            band_str = ", ".join(
                f"{r.amp} ({get_metric_value(r, plot_metric, 'ms2'):.2f})"
                for r in band)
            log(f"All amp values inside the {target.band_label} band: "
                f"{band_str}")
        for name in METRIC_NAMES:
            if name == plot_metric:
                continue
            # Each metric is compared against ITS OWN band - the two are
            # different rulers and share no thresholds.
            alt_target = cue_target(name)
            alt = recommended_cue_amp(results, name)
            if alt is None:
                log(f"(for reference, the {metric_spec(name).short_label} "
                    f"has no calibrated target band, so it recommends "
                    "nothing)")
            else:
                log(f"(for reference, {metric_spec(name).short_label} aims "
                    f"at {alt_target.band_label} and would recommend "
                    f"amp={alt.amp} at "
                    f"{get_metric_value(alt, name, 'ms2'):.2f} m/s²)")
        log(f"Data:    {csv_path}")
        log(f"Raw ACC: {raw_pth} ({recorder.n_samples} samples, "
            f"{recorder.n_windows} windows)")
        log(f"Plot:    {png_path}")
        log(f"Meta:    {meta_path}")

        return dict(result,
                    available_metrics=list(METRIC_NAMES),
                    csv_path=csv_path,
                    png_path=png_path,
                    raw_path=raw_pth)
    finally:
        try:
            send(ser, "X")
            if freq_hz != hc.FIRMWARE_BOOT_PWM_HZ:
                # A custom sweep frequency shouldn't outlive the sweep -
                # put the pin back on the firmware's boot default.
                send(ser, f"F {motor_index} {hc.FIRMWARE_BOOT_PWM_HZ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        log("Serial port closed.")


def main() -> None:
    run_experiment()


if __name__ == "__main__":
    main()
