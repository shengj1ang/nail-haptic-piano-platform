"""Actuator spectrogram - drive-frequency x amp vibration-intensity map.

A 2-D parameter sweep for either actuator type on the rig. It drives the
motor at every combination of PWM drive frequency and amp, and fills a
grid cell with the vibration intensity it measures there:

    for freq in freq_min .. freq_max:   # PWM drive frequency (F command)
        for amp in amp_min .. amp_max:   # PWM duty
            drive at (freq, amp), measure the accelerometer RMS
            grid[freq][amp] = that intensity

    x-axis = amp
    y-axis = drive frequency (the value set with the F command)
    colour = measured RMS acceleration at that (amp, frequency),
             DARKER = STRONGER

Each cell is drawn as a discrete filled box, so the picture reads as the
grid of measured intensities the boxes were "filled" with. The cell value
is the broadband RMS acceleration the drive produced; the y-axis is the
frequency the motor is DRIVEN at, not a frequency read back from the
accelerometer.

Two intensity metrics are measured for EVERY cell (see
validation_experiments/acceleration_metrics.py):

  * demeaned three-axis vector RMS - the recommended default, immune to
    gravity direction, sensor bias and mounting orientation;
  * legacy magnitude RMS - the original baseline-subtracted |a| formula,
    kept so new runs stay comparable with historical ones.

The plot metric is chosen at plot time, not at measurement time: the run
also saves every cell's FULL three-axis sample series, so a saved run can
be re-analysed and re-plotted with either metric offline.

You pick:

  * the motor port (0-11),
  * the actuator type (ERM or LRA), which sets the default amp and
    frequency ranges (ERM freq 0-1000 Hz, LRA freq 0-350 Hz; both amp
    0-255),
  * the amp and frequency ranges (adjustable, seeded from the type),
  * a scan precision (how finely both axes are stepped),
  * a Vibrate time - how long each (freq, amp) cell is driven
    continuously before its intensity is measured (default 2 s),
  * which intensity metric the map is coloured by, and
  * whether to print each cell's value inside its box, as the raw metric
    number or as a 0-1 normalised value.

Because it is a full 2-D sweep, the run length is (frequencies x amps x
Vibrate time) and can be many minutes - the estimate is printed at the
start; use Coarse precision / a short Vibrate time for a quick look.

Outputs (timestamped with Unix epoch seconds, like every other validation
experiment) under data/validation_experiments/actuator_spectrogram/: the
raw three-axis samples (compressed NPZ), the intensity grid (NPZ) for
re-rendering, the map (PNG), a per-cell summary table (CSV) and the run's
parameter/result record (meta.json).

Requires firmware >= v2.9.0 (the 'F' command and the "ACC,id,x,y,z"
stream). Runs standalone (python actuator_spectrogram.py) or through the
launcher's "Validation Experiments" section.
"""

import csv
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, fields
from typing import Callable, Dict, List, Optional

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt
import numpy as np

try:
    from ..acceleration_metrics import (
        DEFAULT_METRIC,
        METRIC_CSV_COLUMNS,
        METRIC_LEGACY_MAGNITUDE_RMS,
        METRIC_NAMES,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        AccelerationMetrics,
        RawSampleRecorder,
        compute_acceleration_metrics,
        compute_baseline_magnitude,
        get_metric_value,
        load_raw_acceleration_samples,
        metric_axis_label,
        metric_meta_block,
        metric_spec,
        metrics_available_in,
        normalise_metric,
        raw_path_for,
        recompute_window_metrics,
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
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        AccelerationMetrics,
        RawSampleRecorder,
        compute_acceleration_metrics,
        compute_baseline_magnitude,
        get_metric_value,
        load_raw_acceleration_samples,
        metric_axis_label,
        metric_meta_block,
        metric_spec,
        metrics_available_in,
        normalise_metric,
        raw_path_for,
        recompute_window_metrics,
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
# Configuration
# ==========================================

# Which actuator this experiment opens on, and the port it is wired to:
# both follow config.json's haptic block (the actuator in use), so the
# window opens on the rig's actual actuator. These are module-level
# fallbacks for command-line runs - the GUI re-reads the config when the
# window opens and its Type / Motor port controls override both.
DEFAULT_MOTOR_TYPE = hc.actuator_label(hc.get_active_haptic_type())
MOTOR_INDEX = hc.get_actuator_motor_port()
ACC_SENSOR_ID = 0

# What a run saved before its meta recorded these is assumed to have
# used. Re-rendering an old chart must reproduce THAT run, so these stay
# fixed and are never taken from today's config.
HISTORICAL_MOTOR_TYPE = "ERM"
HISTORICAL_MOTOR_INDEX = 10

# Per-actuator default ranges (all user-adjustable in the launcher; these
# are what "select this type" seeds the range controls with).
TYPE_CONFIG = {
    "ERM": {"amp_min": 0, "amp_max": 255, "freq_min": 0, "freq_max": 1000},
    "LRA": {"amp_min": 0, "amp_max": 255, "freq_min": 0, "freq_max": 350},
}
MOTOR_TYPES = list(TYPE_CONFIG.keys())

# Absolute bounds the range controls allow, and the firmware limits.
AMP_MIN, AMP_MAX = 0, 255
FREQ_MAX_LIMIT = 20000    # firmware 'F' command upper bound (Hz)
FREQ_DRIVE_MIN = 50       # firmware 'F' command minimum (Hz)
# Firmware boot default (motor_driver.cpp DEFAULT_PWM_FREQ), restored on
# the port when the sweep ends - a firmware fact, not a preference.
DEFAULT_PWM_FREQ = hc.FIRMWARE_BOOT_PWM_HZ

# Scan precision -> (frequency step, amp step). A 2-D sweep's cell count is
# frequencies x amps, so finer precision multiplies the run time fast.
PRECISION_STEPS = {
    "Coarse": {"freq": 100, "amp": 32},
    "Medium": {"freq": 50,  "amp": 16},
    "Fine":   {"freq": 25,  "amp": 8},
}
DEFAULT_PRECISION = "Coarse"   # 2-D sweeps are large; Coarse is a sane default

# Each (freq, amp) cell is driven CONTINUOUSLY for the sustained MEASURE_S
# window and its intensity computed from that whole window. Default; the
# launcher exposes it as a "Vibrate" control so it can be changed per run.
MEASURE_S = 2.00
MEASURE_S_MIN, MEASURE_S_MAX = 0.5, 30.0

# Value annotation inside each box: off, the raw metric number, or a 0-1
# normalised value (position between the map's min and max intensity).
# "rms" is kept as the mode name for backward compatibility with runs
# whose meta already records it; it prints the SELECTED metric's m/s².
ANNOTATE_MODES = ("off", "rms", "normalized")
DEFAULT_ANNOTATE = "off"

ACC_INTERVAL_MS = 1       # fast stream so the RMS captures the vibration
BASELINE_S = 0.30         # quiet window, re-measured once per frequency row
SETTLE_S = 0.30           # motor-on spin-up before the measurement window
REST_S = 0.15             # motor-off rest between cells

# High intensity -> dark: magma reversed runs pale (low) to near-black
# (high), matching "darker = stronger".
COLORMAP = "magma_r"

OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "actuator_spectrogram"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


@dataclass
class CellResult:
    """One (freq, amp) cell: its identity plus BOTH intensity metrics and
    the per-axis statistics they were derived from. The field names are
    the CSV column names. Metric fields are Optional because a CSV
    written before the two-metric refactor carries only the legacy
    columns - those rows load with the vector fields left as None."""
    freq_hz: int
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
    def from_metrics(cls, freq_hz: int, amp: int,
                     metrics: AccelerationMetrics) -> "CellResult":
        return cls(freq_hz=int(freq_hz), amp=int(amp),
                   **{c: getattr(metrics, c) for c in METRIC_CSV_COLUMNS})


CSV_COLUMNS = tuple(f.name for f in fields(CellResult))


# ==========================================
# Parameter helpers
# ==========================================

def type_defaults(motor_type: str) -> dict:
    """Default amp/frequency ranges for a type name; ERM if unknown."""
    return TYPE_CONFIG.get(motor_type, TYPE_CONFIG[DEFAULT_MOTOR_TYPE])


def amp_values(step: int, amp_min: int, amp_max: int) -> List[int]:
    """Amp sweep points across [amp_min, amp_max], always ending at amp_max."""
    vals = list(range(int(amp_min), int(amp_max) + 1, step))
    if not vals:
        vals = [int(amp_min)]
    if vals[-1] != int(amp_max):
        vals.append(int(amp_max))
    return vals


def freq_values(step: int, freq_min: float, freq_max: float) -> List[int]:
    """PWM drive frequencies swept: multiples of `step` within
    [max(freq_min, FREQ_DRIVE_MIN), freq_max], always ending at freq_max
    (the firmware can't drive below FREQ_DRIVE_MIN, so the sweep starts
    there even if freq_min is lower)."""
    lo = max(int(round(freq_min)), FREQ_DRIVE_MIN)
    top = int(round(freq_max))
    vals = [f for f in range(step, top + 1, step) if f >= lo]
    if not vals:
        vals = [min(max(lo, step), top)]
    if vals[-1] != top and top >= lo:
        vals.append(top)
    return vals


def steps_for(precision: str):
    p = PRECISION_STEPS.get(precision, PRECISION_STEPS[DEFAULT_PRECISION])
    return p["freq"], p["amp"]


# ==========================================
# Measurement
# ==========================================

def measure_cell(ser, freq_hz: int, amp: int, baseline_magnitude: float,
                 measure_s: float, log: LogFn,
                 motor_index: int = MOTOR_INDEX,
                 acc_sensor_id: int = ACC_SENSOR_ID):
    """Drive one (freq, amp) cell for measure_s and return
    (CellResult, raw samples). The PWM frequency is assumed already set
    (once per frequency row); this drives the amp, waits out the settle +
    window, then stops. Both intensity metrics come from the same window,
    so the two are always directly comparable."""
    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_samples(ser, measure_s, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  freq={freq_hz} amp={amp}: no samples")
        return None, []

    metrics = compute_acceleration_metrics(vib, baseline_magnitude,
                                           MS2_PER_COUNT)
    return CellResult.from_metrics(freq_hz, amp, metrics), vib


# ==========================================
# Grid assembly
# ==========================================

def build_matrix(freqs: List[int], amps: List[int], results: List[CellResult],
                 metric: str = DEFAULT_METRIC) -> np.ndarray:
    """(len(freqs) x len(amps)) matrix of the chosen metric in m/s², NaN
    where a cell is missing (a dropped measurement, or a metric the row
    does not carry)."""
    fi = {f: i for i, f in enumerate(freqs)}
    ai = {a: j for j, a in enumerate(amps)}
    mat = np.full((len(freqs), len(amps)), np.nan)
    for r in results:
        if r.freq_hz in fi and r.amp in ai:
            value = get_metric_value(r, metric, unit="ms2")
            mat[fi[r.freq_hz], ai[r.amp]] = np.nan if value is None else value
    return mat


def grid_axes(results: List[CellResult]):
    """The sweep's (freqs, amps) axes, recovered from the cell rows."""
    freqs = sorted({r.freq_hz for r in results})
    amps = sorted({r.amp for r in results})
    return freqs, amps


def peak_cell(results: List[CellResult], metric: str) -> CellResult:
    """The strongest cell UNDER THE CHOSEN METRIC - switching the metric
    can legitimately move the peak, which is the point of offering both."""
    scored = [(get_metric_value(r, metric, unit="ms2"), r) for r in results]
    scored = [(v, r) for v, r in scored if v is not None]
    if not scored:
        raise ValueError(f"No cell carries the {metric!r} metric.")
    return max(scored, key=lambda pair: pair[0])[1]


# ==========================================
# Output
# ==========================================

def save_csv(path: str, results: List[CellResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def save_grid(path: str, freqs: List[int], amps: List[int],
              matrices: Dict[str, np.ndarray]) -> None:
    """Persist the intensity grid, one matrix per metric, so the map can
    be re-rendered without re-running the hardware.

    `intensity` is also written as the legacy magnitude RMS grid, which
    is exactly what that key meant in pre-refactor files - so an old
    reader keeps working and never silently reads a different metric."""
    arrays = {
        "freqs": np.asarray(freqs, dtype=int),
        "amps": np.asarray(amps, dtype=int),
    }
    for name, matrix in matrices.items():
        arrays[f"intensity_{name}_ms2"] = np.asarray(matrix, dtype=float)
    legacy = matrices.get(METRIC_LEGACY_MAGNITUDE_RMS)
    if legacy is not None:
        arrays["intensity"] = np.asarray(legacy, dtype=float)
    np.savez(path, **arrays)


def load_grid(path: str, metric: str = DEFAULT_METRIC):
    """(freqs, amps, matrix) for one metric from a saved grid NPZ - the
    reader for the documented grid output. Re-rendering no longer needs
    it (render_csv rebuilds the matrix from the CSV, which carries both
    metrics); this is for anyone consuming the grid file directly.
    Raises when that metric is not in the file (an old grid only holds
    the legacy magnitude RMS)."""
    data = np.load(path)
    key = f"intensity_{normalise_metric(metric)}_ms2"
    if key in data:
        return data["freqs"], data["amps"], data[key]
    if normalise_metric(metric) == METRIC_LEGACY_MAGNITUDE_RMS and "intensity" in data:
        return data["freqs"], data["amps"], data["intensity"]
    raise ValueError(
        f"{os.path.basename(path)} does not contain the "
        f"{metric_spec(metric).short_label} grid - it predates the "
        "two-metric refactor.")


def _annotate_cells(ax, freqs: np.ndarray, amps: np.ndarray,
                    M: np.ndarray, mesh, mode: str) -> None:
    """Print each finite cell's value at its centre, in white or black
    picked per cell so the text always contrasts with the cell colour.
    mode 'rms' prints the selected metric's raw m/s² value; 'normalized'
    prints the cell's 0-1 position between the map's min and max. Font
    shrinks as the grid gets denser; edge cells are aligned inward so
    nothing clips."""
    n = max(len(amps), len(freqs))
    fontsize = 7 if n <= 12 else 6 if n <= 20 else 5 if n <= 32 else 4
    cmap = mesh.cmap
    norm = mesh.norm
    last_i, last_j = M.shape[0] - 1, M.shape[1] - 1
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            val = M[i, j]
            if not np.isfinite(val):
                continue
            frac = float(norm(val))               # 0-1 position in the scale
            r, g, b, _ = cmap(frac)
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            label = f"{frac:.2f}" if mode == "normalized" else f"{val:.2f}"
            ha = "left" if j == 0 else "right" if j == last_j else "center"
            va = "top" if i == last_i else "center"
            ax.text(amps[j], freqs[i], label, ha=ha, va=va, fontsize=fontsize,
                    color="white" if luminance < 0.5 else "black")


def save_heatmap(path: str, freqs, amps, matrix, motor_index: int,
                 motor_type: str, amp_min: int, amp_max: int,
                 freq_min: float, freq_max: float,
                 annotate_mode: str = DEFAULT_ANNOTATE,
                 metric: str = DEFAULT_METRIC) -> None:
    """The intensity map: amp x drive-frequency cells filled with the
    measured RMS acceleration (m/s²), darker = stronger. The colour-bar
    names the metric the matrix was built from, so a legacy map and a
    vector-RMS map can never be confused. annotate_mode ('rms'/
    'normalized') prints each cell's value inside its box."""
    amps = np.asarray(amps, dtype=float)
    freqs = np.asarray(freqs, dtype=float)
    M = np.asarray(matrix, dtype=float)               # (n_freq, n_amp)

    fig, ax = plt.subplots(figsize=(11, 6))
    masked = np.ma.masked_invalid(M)
    cmap = plt.get_cmap(COLORMAP).copy()
    cmap.set_bad(color="0.85")                        # missing cells: light grey
    # "nearest" shading draws each measurement as a discrete filled cell
    # (the "boxes" of the grid), centred on its amp / drive-frequency.
    mesh = ax.pcolormesh(amps, freqs, masked, shading="nearest", cmap=cmap)
    cbar = fig.colorbar(mesh, ax=ax, pad=0.02)
    cbar.set_label(f"{metric_axis_label(metric)} - darker = stronger")

    if annotate_mode in ("rms", "normalized"):
        _annotate_cells(ax, freqs, amps, M, mesh, annotate_mode)

    ax.set_xlabel("amp (PWM duty)")
    ax.set_ylabel("Drive frequency (Hz, F command)")
    ax.set_xlim(amp_min, amp_max)
    ax.set_ylim(freq_min, freq_max)
    ax.set_title(f"{motor_type} vibration intensity vs drive frequency & amp "
                 f"(motor port {motor_index})")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    return os.path.splitext(csv_path)[0] + ".meta.json"


def grid_path_for(csv_path: str) -> str:
    """spectrogram_<ts>.csv -> spectrogram_<ts>.npz (same folder) - where
    a run's grid file lives, for load_grid()."""
    d = os.path.dirname(csv_path)
    base = os.path.basename(csv_path)
    stamp = base[len("spectrogram_"):-len(".csv")] if (
        base.startswith("spectrogram_") and base.endswith(".csv")) else ""
    return os.path.join(d, f"spectrogram_{stamp}.npz")


def save_meta(csv_path: str, png_path: str, grid_pth: str, raw_path: str,
              stamp: str, firmware, motor_index: int, acc_sensor_id: int,
              motor_type: str, amp_min: int, amp_max: int, freq_min: float,
              freq_max: float, precision: str, freq_step: int, amp_step: int,
              measure_s: float, annotate_mode: str, metric: str,
              peak: CellResult) -> str:
    meta = {
        "experiment": "actuator_spectrogram",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "motor_type": motor_type,
            "amp_min": amp_min,
            "amp_max": amp_max,
            "freq_min": freq_min,
            "freq_max": freq_max,
            "freq_drive_min_hz": FREQ_DRIVE_MIN,
            "precision": precision,
            "freq_step": freq_step,
            "amp_step": amp_step,
            "ms2_per_count": MS2_PER_COUNT,
            "baseline_s": BASELINE_S,
            "settle_s": SETTLE_S,
            "measure_s": measure_s,
            "rest_s": REST_S,
            "acc_interval_ms": ACC_INTERVAL_MS,
            "annotate_mode": annotate_mode,
        },
        # The haptic config as it stood at run time. This experiment
        # SWEEPS frequency and amp, so the config never sets what was
        # driven - the swept ranges above are the record of that. It is
        # kept so a run can be traced back to the rig's configuration.
        "haptic_config": {
            "snapshot": hc.config_snapshot(),
            "note": ("informational: every cell's drive comes from the "
                     "swept ranges in 'parameters', not from this config"),
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "heatmap": os.path.basename(png_path),
            "grid": os.path.basename(grid_pth),
            "raw_acceleration": os.path.basename(raw_path) if raw_path else None,
        },
        # metric_version / available_metrics / selected_plot_metric /
        # raw_acceleration_file / raw_data_format / ms2_per_count
        "metrics": metric_meta_block(metric, raw_path is not None,
                                     os.path.basename(raw_path) if raw_path
                                     else None, MS2_PER_COUNT),
        # The headline result is always stated FOR THE SELECTED METRIC.
        "result": {
            "metric": normalise_metric(metric),
            "peak_freq_hz": peak.freq_hz,
            "peak_amp": peak.amp,
            "peak_ms2": get_metric_value(peak, metric, unit="ms2"),
            "peak_counts": get_metric_value(peak, metric, unit="counts"),
            # Kept under its historical name so old readers still find it.
            "peak_rms_ms2": get_metric_value(peak, metric, unit="ms2"),
        },
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


def _opt_float(row: dict, key: str) -> Optional[float]:
    value = row.get(key)
    if value is None or value == "":
        return None
    return float(value)


def load_results(csv_path: str) -> List[CellResult]:
    """Read back a saved spectrogram_*.csv.

    Accepts both the current two-metric columns and the pre-refactor
    single-metric ones (rms_delta_counts / rms_ms2 / peak_delta_counts /
    baseline_mag), which map onto the legacy metric; the vector fields of
    such a row stay None so nothing can pretend to derive them."""
    results: List[CellResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            legacy_counts = _opt_float(row, "legacy_magnitude_rms_counts")
            if legacy_counts is None:
                legacy_counts = _opt_float(row, "rms_delta_counts")
            legacy_ms2 = _opt_float(row, "legacy_magnitude_rms_ms2")
            if legacy_ms2 is None:
                legacy_ms2 = _opt_float(row, "rms_ms2")
            peak_counts = _opt_float(row, "peak_magnitude_delta_counts")
            if peak_counts is None:
                peak_counts = _opt_float(row, "peak_delta_counts")
            baseline = _opt_float(row, "baseline_magnitude_counts")
            if baseline is None:
                baseline = _opt_float(row, "baseline_mag")
            results.append(CellResult(
                freq_hz=int(row["freq_hz"]),
                amp=int(row["amp"]),
                n_samples=int(row["n_samples"]),
                mean_x_counts=_opt_float(row, "mean_x_counts"),
                mean_y_counts=_opt_float(row, "mean_y_counts"),
                mean_z_counts=_opt_float(row, "mean_z_counts"),
                rms_x_counts=_opt_float(row, "rms_x_counts"),
                rms_y_counts=_opt_float(row, "rms_y_counts"),
                rms_z_counts=_opt_float(row, "rms_z_counts"),
                legacy_magnitude_rms_counts=legacy_counts,
                legacy_magnitude_rms_ms2=legacy_ms2,
                vector_rms_counts=_opt_float(row, "vector_rms_counts"),
                vector_rms_ms2=_opt_float(row, "vector_rms_ms2"),
                baseline_magnitude_counts=baseline,
                peak_magnitude_delta_counts=peak_counts,
            ))
    if not results:
        raise ValueError(f"No sweep rows found in {csv_path}")
    return results


def raw_file_for(csv_path: str) -> Optional[str]:
    """The run's raw three-axis sample file, or None for a run saved
    before raw data was kept (an old CSV)."""
    path = raw_path_for(csv_path)
    return path if os.path.exists(path) else None


def available_metrics_for(csv_path: str) -> List[str]:
    """Which metrics a saved run can be plotted with. Old runs offer the
    legacy metric only - the vector RMS needs per-axis samples and is
    NOT derivable from a stored scalar magnitude RMS."""
    return metrics_available_in(load_results(csv_path), METRIC_NAMES)


def recompute_from_raw(csv_path: str) -> List[CellResult]:
    """Rebuild every cell's metrics from the run's raw sample file - the
    proof that the saved raw data alone reproduces the summary table.
    Raises when the run has no raw file."""
    raw_path = raw_file_for(csv_path)
    if raw_path is None:
        raise ValueError(f"{os.path.basename(csv_path)} has no raw "
                         "acceleration file - it predates raw-sample saving.")
    raw = load_raw_acceleration_samples(raw_path)
    return [CellResult.from_metrics(int(w.commanded_freq_hz),
                                    int(w.commanded_amp), w.metrics)
            for w in recompute_window_metrics(raw, phase=PHASE_VIBRATION)]


def set_display_options(csv_path: str, annotate_mode: Optional[str] = None,
                        metric: Optional[str] = None) -> str:
    """Switch a saved run's plot metric and/or value annotation and
    re-render ITS OWN heatmap PNG in place (the file next to the CSV), so
    the saved image always matches the chosen options. Both choices are
    recorded in the run's meta so later re-renders keep them. Returns the
    PNG path. Never touches the CSV/NPZ/raw measurement data."""
    if annotate_mode is not None and annotate_mode not in ANNOTATE_MODES:
        raise ValueError(f"annotate_mode must be one of {ANNOTATE_MODES}")
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    metrics_meta = meta.get("metrics", {}) if meta else {}
    defaults = type_defaults(params.get("motor_type", HISTORICAL_MOTOR_TYPE))
    motor_index = params.get("motor_index", HISTORICAL_MOTOR_INDEX)
    motor_type = params.get("motor_type", HISTORICAL_MOTOR_TYPE)
    amp_min = params.get("amp_min", defaults["amp_min"])
    amp_max = params.get("amp_max", defaults["amp_max"])
    freq_min = params.get("freq_min", defaults["freq_min"])
    freq_max = params.get("freq_max", defaults["freq_max"])
    if annotate_mode is None:
        annotate_mode = params.get("annotate_mode", DEFAULT_ANNOTATE)

    results = load_results(csv_path)
    chosen = select_metric(results, metric
                           or metrics_meta.get("selected_plot_metric"))
    freqs, amps = grid_axes(results)
    matrix = build_matrix(freqs, amps, results, chosen)

    # The run's own PNG sits next to its CSV (same <ts> stem).
    png_path = os.path.splitext(csv_path)[0] + ".png"
    save_heatmap(png_path, freqs, amps, matrix, motor_index, motor_type,
                 amp_min, amp_max, freq_min, freq_max,
                 annotate_mode=annotate_mode, metric=chosen)

    if meta is not None:   # keep the stored options in sync for future renders
        meta.setdefault("parameters", {})["annotate_mode"] = annotate_mode
        meta.setdefault("metrics", {})["selected_plot_metric"] = chosen
        peak = peak_cell(results, chosen)
        meta["result"] = {
            "metric": chosen,
            "peak_freq_hz": peak.freq_hz,
            "peak_amp": peak.amp,
            "peak_ms2": get_metric_value(peak, chosen, unit="ms2"),
            "peak_counts": get_metric_value(peak, chosen, unit="counts"),
            "peak_rms_ms2": get_metric_value(peak, chosen, unit="ms2"),
        }
        with open(meta_path_for(csv_path), "w") as f:
            json.dump(meta, f, indent=2)
    return png_path


def set_annotate_mode(csv_path: str, annotate_mode: str) -> str:
    """Backwards-compatible alias for set_display_options(annotate_mode=...)."""
    return set_display_options(csv_path, annotate_mode=annotate_mode)


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None,
               metric: Optional[str] = None) -> dict:
    """Re-render the intensity map from a saved run's CSV, with the
    chosen metric (default: the metric stored in the run's meta, falling
    back to the recommended one; an old legacy-only CSV falls back to the
    legacy metric rather than failing). Labels/ranges come from the run's
    .meta.json. Returns a summary dict like run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    metrics_meta = meta.get("metrics", {}) if meta else {}
    defaults = type_defaults(params.get("motor_type", HISTORICAL_MOTOR_TYPE))
    if motor_index is None:
        motor_index = params.get("motor_index", HISTORICAL_MOTOR_INDEX)
    motor_type = params.get("motor_type", HISTORICAL_MOTOR_TYPE)
    amp_min = params.get("amp_min", defaults["amp_min"])
    amp_max = params.get("amp_max", defaults["amp_max"])
    freq_min = params.get("freq_min", defaults["freq_min"])
    freq_max = params.get("freq_max", defaults["freq_max"])
    annotate_mode = params.get("annotate_mode", DEFAULT_ANNOTATE)

    results = load_results(csv_path)
    supported = metrics_available_in(results, METRIC_NAMES)
    requested = metric or metrics_meta.get("selected_plot_metric")
    chosen = select_metric(results, requested)
    freqs, amps = grid_axes(results)
    matrix = build_matrix(freqs, amps, results, chosen)
    save_heatmap(out_png, freqs, amps, matrix, motor_index, motor_type,
                 amp_min, amp_max, freq_min, freq_max,
                 annotate_mode=annotate_mode, metric=chosen)

    peak = peak_cell(results, chosen)
    return {
        "motor_type": motor_type,
        "metric": chosen,
        "requested_metric": normalise_metric(requested) if requested else None,
        "available_metrics": supported,
        "peak_freq_hz": peak.freq_hz,
        "peak_amp": peak.amp,
        "peak_ms2": get_metric_value(peak, chosen, unit="ms2"),
        "peak_rms_ms2": get_metric_value(peak, chosen, unit="ms2"),
        "csv_path": csv_path,
        "png_path": out_png,
    }


# ==========================================
# Experiment
# ==========================================

#: Above this many cells the full matrix is replaced by a per-frequency
#: best-cell listing: a Fine sweep can be hundreds of cells wide, and a
#: wrapped 200-column table is less readable than no table at all.
MAX_REPORT_CELLS = 400


def summary_report(csv_path: str, summary: Optional[dict] = None) -> list:
    """The map's statistics as text, rebuilt from a saved
    spectrogram_*.csv (+ its .meta.json): the parameters, the measured
    intensity of every cell (or, for a large grid, the strongest amp per
    frequency), and the peak the heat map marks.

    Values are shown for the metric the displayed map uses, so the text
    and the colours always describe the same numbers."""
    results = load_results(csv_path)
    meta = load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    metrics_meta = (meta or {}).get("metrics", {})
    chosen = normalise_metric(
        (summary or {}).get("metric")
        or metrics_meta.get("selected_plot_metric")
        or select_metric(results, None))
    freqs, amps = grid_axes(results)
    peak = peak_cell(results, chosen)

    details = [
        f"{params.get('motor_type', '?')} on motor port "
        f"{params.get('motor_index', '?')}, ACC sensor "
        f"{params.get('acc_sensor_id', '?')}",
        f"amp {params.get('amp_min', '?')}-{params.get('amp_max', '?')} "
        f"step {params.get('amp_step', '?')}, freq "
        f"{params.get('freq_min', '?')}-{params.get('freq_max', '?')} Hz "
        f"step {params.get('freq_step', '?')} "
        f"({params.get('precision', '?')} precision)",
        f"{len(freqs)} frequencies x {len(amps)} amps = {len(results)} cells, "
        f"{params.get('measure_s', '?')} s per cell",
        f"values shown: {metric_spec(chosen).short_label} in m/s²",
    ]
    lines = report.header("Spectrogram statistics", csv_path, meta, details)
    lines.append("")

    by_freq = {}
    for cell in results:
        by_freq.setdefault(cell.freq_hz, []).append(cell)

    if len(results) <= MAX_REPORT_CELLS and amps:
        # The full grid, as a frequency x amp matrix - the text form of
        # the picture, with each cell's own measured value.
        lines.append(f"  Intensity map (m/s², rows = drive frequency, "
                     f"columns = amp):")
        rows = []
        for freq in freqs:
            cells = {c.amp: c for c in by_freq.get(freq, [])}
            rows.append([freq] + [
                report.number(
                    get_metric_value(cells[a], chosen, unit="ms2")
                    if a in cells else None, 2)
                for a in amps])
        lines.extend(report.table(["freq Hz"] + [f"amp {a}" for a in amps],
                                  rows, indent="    "))
    else:
        lines.append(f"  Strongest amp per frequency ({len(results)} cells - "
                     f"too many for a full matrix, over "
                     f"{MAX_REPORT_CELLS}):")
        rows = []
        for freq in freqs:
            cells = by_freq.get(freq, [])
            if not cells:
                continue
            best = peak_cell(cells, chosen)
            rows.append((freq, best.amp,
                         report.number(get_metric_value(best, chosen,
                                                        unit="ms2"), 3),
                         len(cells)))
        lines.extend(report.table(
            ("freq Hz", "best amp", "m/s²", "cells"), rows, indent="    "))

    lines.append("")
    lines.append(f"Strongest vibration at freq={peak.freq_hz} Hz, "
                 f"amp={peak.amp} "
                 f"({get_metric_value(peak, chosen, unit='ms2'):.2f} m/s², "
                 f"{metric_spec(chosen).short_label})")
    for name in METRIC_NAMES:
        if name == chosen:
            continue
        try:
            alt = peak_cell(results, name)
        except Exception:
            continue
        lines.append(f"  (for reference, {metric_spec(name).short_label} "
                     f"peaks at freq={alt.freq_hz} Hz, amp={alt.amp}: "
                     f"{get_metric_value(alt, name, unit='ms2'):.2f} m/s²)")
    lines.append("Every cell above was driven at its own (frequency, amp) - "
                 "the map is the sweep's own grid, not a configured default.")
    return lines


def estimated_duration_s(precision: str = DEFAULT_PRECISION,
                         measure_s: float = MEASURE_S,
                         motor_type: str = DEFAULT_MOTOR_TYPE) -> float:
    freq_step, amp_step = steps_for(precision)
    d = type_defaults(motor_type)
    freqs = freq_values(freq_step, d["freq_min"], d["freq_max"])
    amps = amp_values(amp_step, d["amp_min"], d["amp_max"])
    per_cell = SETTLE_S + measure_s + REST_S
    return len(freqs) * (BASELINE_S + len(amps) * per_cell)


def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   motor_type: str = DEFAULT_MOTOR_TYPE,
                   precision: str = DEFAULT_PRECISION,
                   measure_s: float = MEASURE_S,
                   annotate_mode: str = DEFAULT_ANNOTATE,
                   plot_metric: str = DEFAULT_METRIC,
                   amp_min: Optional[int] = None,
                   amp_max: Optional[int] = None,
                   freq_min: Optional[float] = None,
                   freq_max: Optional[float] = None) -> dict:
    """Sweep drive frequency x amp and build the actuator's intensity map.

    for freq in freq_min..freq_max (step from precision):
        for amp in amp_min..amp_max (step from precision):
            drive at (freq, amp) for measure_s, measure BOTH metrics

    motor_type ('ERM'/'LRA') seeds the default ranges; amp_min/amp_max/
    freq_min/freq_max override them; precision sets both steps; measure_s
    is the per-cell drive/measure time; annotate_mode prints each cell's
    value ('rms'/'normalized') or nothing ('off'); plot_metric picks
    which intensity metric colours the map and defines the reported peak
    (both are measured and saved regardless). Writes the raw-sample NPZ,
    the grid NPZ, the PNG, the CSV and the meta, and returns a summary
    dict."""
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    plot_metric = normalise_metric(plot_metric)
    d = type_defaults(motor_type)
    amp_min = d["amp_min"] if amp_min is None else int(amp_min)
    amp_max = d["amp_max"] if amp_max is None else int(amp_max)
    freq_min = d["freq_min"] if freq_min is None else int(freq_min)
    freq_max = d["freq_max"] if freq_max is None else int(freq_max)
    freq_step, amp_step = steps_for(precision)
    measure_s = max(MEASURE_S_MIN, min(MEASURE_S_MAX, measure_s))
    freqs = freq_values(freq_step, freq_min, freq_max)
    amps = amp_values(amp_step, amp_min, amp_max)
    total_cells = len(freqs) * len(amps)
    per_cell = SETTLE_S + measure_s + REST_S
    est = len(freqs) * (BASELINE_S + len(amps) * per_cell)

    log(f"{motor_type} intensity map on motor port {motor_index}")
    log(f"drive frequency {freqs[0]}..{freqs[-1]} Hz step {freq_step} "
        f"({len(freqs)} rows) x amp {amps[0]}..{amps[-1]} step {amp_step} "
        f"({len(amps)} cols) = {total_cells} cells, {measure_s:.1f} s each")
    log(f"Plot metric: {metric_spec(plot_metric).short_label} "
        "(both metrics are measured and saved; the map can be re-plotted "
        "with either afterwards)")
    log(f"Estimated duration: ~{est:.0f} s. "
        "Keep the rig still during the sweep.\n")

    stamp = str(int(time.time()))     # Unix epoch seconds (project rule)
    recorder = RawSampleRecorder(
        run_id=f"actuator_spectrogram_{stamp}",
        experiment="actuator_spectrogram",
        ms2_per_count=MS2_PER_COUNT,
        sensor_id=acc_sensor_id,
        meta={"motor_index": motor_index, "motor_type": motor_type,
              "acc_interval_ms": ACC_INTERVAL_MS, "measure_s": measure_s,
              "baseline_s": BASELINE_S, "settle_s": SETTLE_S,
              "rest_s": REST_S, "precision": precision},
    )

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        results: List[CellResult] = []
        done = 0
        cell_id = 0
        for freq in freqs:
            if should_stop():
                raise SweepAborted()
            send(ser, f"F {motor_index} {freq}")   # set the row's drive frequency
            # Motor off here: this window is the row's quiet baseline, and
            # its mean |a| is what the LEGACY metric subtracts.
            baseline_samples = collect_samples(ser, BASELINE_S, acc_sensor_id)
            if baseline_samples:
                baseline_magnitude = compute_baseline_magnitude(baseline_samples)
                baseline_window = recorder.add_window(
                    baseline_samples, phase=PHASE_BASELINE, cell_id=-1,
                    commanded_freq_hz=freq, commanded_amp=0)
            else:
                log(f"freq={freq}: no baseline samples - is the stream running?")
                baseline_magnitude = 0.0
                baseline_window = -1
            row_peak = 0.0
            for amp in amps:
                if should_stop():
                    raise SweepAborted()
                cell, samples = measure_cell(
                    ser, freq, amp, baseline_magnitude, measure_s, log,
                    motor_index=motor_index, acc_sensor_id=acc_sensor_id)
                done += 1
                progress(done, total_cells)
                if cell is not None:
                    results.append(cell)
                    recorder.add_window(
                        samples, phase=PHASE_VIBRATION, cell_id=cell_id,
                        baseline_window_id=baseline_window,
                        commanded_freq_hz=freq, commanded_amp=amp)
                    cell_id += 1
                    value = get_metric_value(cell, plot_metric, unit="ms2")
                    row_peak = max(row_peak, value if value is not None else 0.0)
            log(f"freq={freq:5d} Hz  row peak {row_peak:5.3f} m/s²")
        if not results:
            raise RuntimeError("Sweep produced no data - check wiring and stream")

        matrices = {name: build_matrix(freqs, amps, results, name)
                    for name in METRIC_NAMES}
        peak = peak_cell(results, plot_metric)
        peak_ms2 = get_metric_value(peak, plot_metric, unit="ms2")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        csv_path = os.path.join(OUTPUT_DIR, f"spectrogram_{stamp}.csv")
        grid_pth = os.path.join(OUTPUT_DIR, f"spectrogram_{stamp}.npz")
        png_path = os.path.join(OUTPUT_DIR, f"spectrogram_{stamp}.png")
        raw_pth = raw_path_for(csv_path)
        save_csv(csv_path, results)
        save_grid(grid_pth, freqs, amps, matrices)
        recorder.save(raw_pth)
        save_heatmap(png_path, freqs, amps, matrices[plot_metric], motor_index,
                     motor_type, amp_min, amp_max, freq_min, freq_max,
                     annotate_mode=annotate_mode, metric=plot_metric)
        meta_path = save_meta(csv_path, png_path, grid_pth, raw_pth, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, motor_type,
                              amp_min, amp_max, freq_min, freq_max,
                              precision, freq_step, amp_step, measure_s,
                              annotate_mode, plot_metric, peak)

        log(f"\n=== Strongest vibration at freq={peak.freq_hz} Hz, "
            f"amp={peak.amp} ({peak_ms2:.2f} m/s² "
            f"{metric_spec(plot_metric).short_label}) ===")
        other = [n for n in METRIC_NAMES if n != plot_metric]
        for name in other:
            alt = peak_cell(results, name)
            alt_ms2 = get_metric_value(alt, name, unit="ms2")
            log(f"    (for reference, {metric_spec(name).short_label} peaks at "
                f"freq={alt.freq_hz} Hz, amp={alt.amp}: {alt_ms2:.2f} m/s²)")
        log(f"Grid:     {grid_pth}")
        log(f"Raw ACC:  {raw_pth} ({recorder.n_samples} samples, "
            f"{recorder.n_windows} windows)")
        log(f"Heatmap:  {png_path}")
        log(f"Data:     {csv_path}")
        log(f"Meta:     {meta_path}")

        return {
            "motor_type": motor_type,
            "metric": plot_metric,
            "available_metrics": list(METRIC_NAMES),
            "peak_freq_hz": peak.freq_hz,
            "peak_amp": peak.amp,
            "peak_ms2": peak_ms2,
            "peak_rms_ms2": peak_ms2,
            "csv_path": csv_path,
            "png_path": png_path,
            "raw_path": raw_pth,
        }
    finally:
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
