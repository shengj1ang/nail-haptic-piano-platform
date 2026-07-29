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

You pick:

  * the motor port (0-11),
  * the actuator type (ERM or LRA), which sets the default amp and
    frequency ranges (ERM freq 0-1000 Hz, LRA freq 0-350 Hz; both amp
    0-255),
  * the amp and frequency ranges (adjustable, seeded from the type),
  * a scan precision (how finely both axes are stepped),
  * a Vibrate time - how long each (freq, amp) cell is driven
    continuously before its intensity is measured (default 2 s), and
  * whether to print each cell's value inside its box, as the raw RMS
    number or as a 0-1 normalised value.

Because it is a full 2-D sweep, the run length is (frequencies x amps x
Vibrate time) and can be many minutes - the estimate is printed at the
start; use Coarse precision / a short Vibrate time for a quick look.

Outputs (timestamped with Unix epoch seconds, like every other validation
experiment) under data/validation_experiments/actuator_spectrogram/: the
intensity grid (NPZ) for re-rendering, the map (PNG), a per-cell scalar
table (CSV) and the run's parameter/result record (meta.json).

Requires firmware >= v2.9.0 (the 'F' command and the "ACC,id,x,y,z"
stream). Runs standalone (python actuator_spectrogram.py) or through the
launcher's "Validation Experiments" section.
"""

import csv
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from typing import Callable, List, Optional

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt
import numpy as np

try:
    from ..rig import SweepAborted, collect_samples, open_rig, send
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from rig import SweepAborted, collect_samples, open_rig, send


# ==========================================
# Configuration
# ==========================================

MOTOR_INDEX = 10          # default motor port (ERM test channel; LRA is 11)
ACC_SENSOR_ID = 0
DEFAULT_MOTOR_TYPE = "ERM"

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
DEFAULT_PWM_FREQ = 224    # firmware boot default, restored when the sweep ends

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

# Value annotation inside each box: off, the raw RMS number, or a 0-1
# normalised value (position between the map's min and max intensity).
ANNOTATE_MODES = ("off", "rms", "normalized")
DEFAULT_ANNOTATE = "off"

ACC_INTERVAL_MS = 1       # fast stream so the RMS captures the vibration
BASELINE_S = 0.30         # quiet window, re-measured once per frequency row
SETTLE_S = 0.30           # motor-on spin-up before the measurement window
REST_S = 0.15             # motor-off rest between cells

# LIS3DH high-resolution mode, +/-2 g: 1 count = 1 mg.
MS2_PER_COUNT = 0.001 * 9.80665

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
    freq_hz: int
    amp: int
    rms_delta_counts: float
    rms_ms2: float
    peak_delta_counts: float
    baseline_mag: float
    n_samples: int


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

def _baseline_mag(ser, acc_sensor_id: int) -> Optional[float]:
    """Mean |a| over a quiet window (motor off) - the rest floor RMS is
    measured against."""
    baseline = collect_samples(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        return None
    return statistics.fmean(
        math.sqrt(x * x + y * y + z * z) for _, x, y, z in baseline)


def measure_cell(ser, freq_hz: int, amp: int, baseline_mag: float,
                 measure_s: float, log: LogFn,
                 motor_index: int = MOTOR_INDEX,
                 acc_sensor_id: int = ACC_SENSOR_ID) -> Optional[CellResult]:
    """Drive one (freq, amp) cell for measure_s and return its scalar
    vibration intensity (baseline-subtracted RMS acceleration). The PWM
    frequency is assumed already set (once per frequency row); this drives
    the amp, waits out the settle + window, then stops."""
    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_samples(ser, measure_s, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  freq={freq_hz} amp={amp}: no samples")
        return None

    mags = [math.sqrt(x * x + y * y + z * z) for _, x, y, z in vib]
    rms_counts = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in mags]))
    peak_counts = max(abs(m - baseline_mag) for m in mags)
    return CellResult(freq_hz, amp, rms_counts, rms_counts * MS2_PER_COUNT,
                      peak_counts, baseline_mag, len(vib))


# ==========================================
# Grid assembly
# ==========================================

def build_matrix(freqs: List[int], amps: List[int],
                 results: List[CellResult]) -> np.ndarray:
    """(len(freqs) x len(amps)) matrix of rms_ms2, NaN where a cell is
    missing (a dropped measurement)."""
    fi = {f: i for i, f in enumerate(freqs)}
    ai = {a: j for j, a in enumerate(amps)}
    mat = np.full((len(freqs), len(amps)), np.nan)
    for r in results:
        if r.freq_hz in fi and r.amp in ai:
            mat[fi[r.freq_hz], ai[r.amp]] = r.rms_ms2
    return mat


# ==========================================
# Output
# ==========================================

def save_csv(path: str, results: List[CellResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def save_grid(path: str, freqs: List[int], amps: List[int],
              matrix: np.ndarray) -> None:
    """Persist the intensity grid so the map can be re-rendered
    (render_csv) without re-running the hardware."""
    np.savez(path, freqs=np.asarray(freqs, dtype=int),
             amps=np.asarray(amps, dtype=int),
             intensity=np.asarray(matrix, dtype=float))


def _annotate_cells(ax, freqs: np.ndarray, amps: np.ndarray,
                    M: np.ndarray, mesh, mode: str) -> None:
    """Print each finite cell's value at its centre, in white or black
    picked per cell so the text always contrasts with the cell colour.
    mode 'rms' prints the raw m/s² value; 'normalized' prints the cell's
    0-1 position between the map's min and max. Font shrinks as the grid
    gets denser; edge cells are aligned inward so nothing clips."""
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
                 annotate_mode: str = DEFAULT_ANNOTATE) -> None:
    """The intensity map: amp x drive-frequency cells filled with the
    measured RMS acceleration (m/s²), darker = stronger. annotate_mode
    ('rms'/'normalized') prints each cell's value inside its box."""
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
    cbar.set_label("RMS acceleration (m/s²) - darker = stronger")

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
    """spectrogram_<ts>.csv -> spectrogram_<ts>.npz (same folder)."""
    d = os.path.dirname(csv_path)
    base = os.path.basename(csv_path)
    stamp = base[len("spectrogram_"):-len(".csv")] if (
        base.startswith("spectrogram_") and base.endswith(".csv")) else ""
    return os.path.join(d, f"spectrogram_{stamp}.npz")


def save_meta(csv_path: str, png_path: str, grid_pth: str, stamp: str,
              firmware, motor_index: int, acc_sensor_id: int, motor_type: str,
              amp_min: int, amp_max: int, freq_min: float, freq_max: float,
              precision: str, freq_step: int, amp_step: int, measure_s: float,
              annotate_mode: str, peak: CellResult) -> str:
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
        "files": {
            "csv": os.path.basename(csv_path),
            "heatmap": os.path.basename(png_path),
            "grid": os.path.basename(grid_pth),
        },
        "result": {
            "peak_freq_hz": peak.freq_hz,
            "peak_amp": peak.amp,
            "peak_rms_ms2": peak.rms_ms2,
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


def load_results(csv_path: str) -> List[CellResult]:
    results: List[CellResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(CellResult(
                freq_hz=int(row["freq_hz"]),
                amp=int(row["amp"]),
                rms_delta_counts=float(row["rms_delta_counts"]),
                rms_ms2=float(row["rms_ms2"]),
                peak_delta_counts=float(row["peak_delta_counts"]),
                baseline_mag=float(row["baseline_mag"]),
                n_samples=int(row["n_samples"]),
            ))
    if not results:
        raise ValueError(f"No sweep rows found in {csv_path}")
    return results


def set_annotate_mode(csv_path: str, annotate_mode: str) -> str:
    """Switch a saved run's value annotation and re-render ITS OWN heatmap
    PNG in place (the file next to the CSV), so the saved image always
    matches the chosen mode. Also records the mode in the run's meta so
    later re-renders keep it. Returns the PNG path. Never touches the
    CSV/NPZ measurement data."""
    if annotate_mode not in ANNOTATE_MODES:
        raise ValueError(f"annotate_mode must be one of {ANNOTATE_MODES}")
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    defaults = type_defaults(params.get("motor_type", DEFAULT_MOTOR_TYPE))
    motor_index = params.get("motor_index", MOTOR_INDEX)
    motor_type = params.get("motor_type", DEFAULT_MOTOR_TYPE)
    amp_min = params.get("amp_min", defaults["amp_min"])
    amp_max = params.get("amp_max", defaults["amp_max"])
    freq_min = params.get("freq_min", defaults["freq_min"])
    freq_max = params.get("freq_max", defaults["freq_max"])

    npz_path = grid_path_for(csv_path)
    if not os.path.exists(npz_path):
        raise ValueError(
            f"Grid file {os.path.basename(npz_path)} not found - the map can "
            "only be re-rendered from the run's .npz.")
    # The run's own PNG sits next to its CSV (same <ts> stem).
    png_path = os.path.splitext(csv_path)[0] + ".png"

    data = np.load(npz_path)
    save_heatmap(png_path, data["freqs"], data["amps"], data["intensity"],
                 motor_index, motor_type, amp_min, amp_max, freq_min, freq_max,
                 annotate_mode=annotate_mode)

    if meta is not None:   # keep the stored mode in sync for future renders
        meta.setdefault("parameters", {})["annotate_mode"] = annotate_mode
        with open(meta_path_for(csv_path), "w") as f:
            json.dump(meta, f, indent=2)
    return png_path


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None) -> dict:
    """Re-render the intensity map from a saved run's sibling
    spectrogram_*.npz (this experiment always writes one). Labels/ranges
    come from the run's .meta.json. Returns a summary dict like
    run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    defaults = type_defaults(params.get("motor_type", DEFAULT_MOTOR_TYPE))
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    motor_type = params.get("motor_type", DEFAULT_MOTOR_TYPE)
    amp_min = params.get("amp_min", defaults["amp_min"])
    amp_max = params.get("amp_max", defaults["amp_max"])
    freq_min = params.get("freq_min", defaults["freq_min"])
    freq_max = params.get("freq_max", defaults["freq_max"])
    annotate_mode = params.get("annotate_mode", DEFAULT_ANNOTATE)

    npz_path = grid_path_for(csv_path)
    if not os.path.exists(npz_path):
        raise ValueError(
            f"Grid file {os.path.basename(npz_path)} not found - the map can "
            "only be re-rendered from the run's .npz.")
    data = np.load(npz_path)
    save_heatmap(out_png, data["freqs"], data["amps"], data["intensity"],
                 motor_index, motor_type, amp_min, amp_max, freq_min, freq_max,
                 annotate_mode=annotate_mode)

    results = load_results(csv_path)
    peak = max(results, key=lambda r: r.rms_ms2)
    return {
        "motor_type": motor_type,
        "peak_freq_hz": peak.freq_hz,
        "peak_amp": peak.amp,
        "peak_rms_ms2": peak.rms_ms2,
        "csv_path": csv_path,
        "png_path": out_png,
    }


# ==========================================
# Experiment
# ==========================================

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
                   amp_min: Optional[int] = None,
                   amp_max: Optional[int] = None,
                   freq_min: Optional[float] = None,
                   freq_max: Optional[float] = None) -> dict:
    """Sweep drive frequency x amp and build the actuator's intensity map.

    for freq in freq_min..freq_max (step from precision):
        for amp in amp_min..amp_max (step from precision):
            drive at (freq, amp) for measure_s, measure RMS intensity

    motor_type ('ERM'/'LRA') seeds the default ranges; amp_min/amp_max/
    freq_min/freq_max override them; precision sets both steps; measure_s
    is the per-cell drive/measure time; annotate_mode prints each cell's
    value ('rms'/'normalized') or nothing ('off'). Writes the
    NPZ/PNG/CSV/meta outputs and returns a summary dict."""
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

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
    log(f"Estimated duration: ~{est:.0f} s. "
        "Keep the rig still during the sweep.\n")

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        results: List[CellResult] = []
        done = 0
        for freq in freqs:
            if should_stop():
                raise SweepAborted()
            send(ser, f"F {motor_index} {freq}")   # set the row's drive frequency
            baseline_mag = _baseline_mag(ser, acc_sensor_id)  # motor off here
            if baseline_mag is None:
                log(f"freq={freq}: no baseline samples - is the stream running?")
                baseline_mag = 0.0
            row_peak = 0.0
            for amp in amps:
                if should_stop():
                    raise SweepAborted()
                cell = measure_cell(ser, freq, amp, baseline_mag, measure_s, log,
                                    motor_index=motor_index,
                                    acc_sensor_id=acc_sensor_id)
                done += 1
                progress(done, total_cells)
                if cell is not None:
                    results.append(cell)
                    row_peak = max(row_peak, cell.rms_ms2)
            log(f"freq={freq:5d} Hz  row peak {row_peak:5.3f} m/s²")
        if not results:
            raise RuntimeError("Sweep produced no data - check wiring and stream")

        matrix = build_matrix(freqs, amps, results)
        peak = max(results, key=lambda r: r.rms_ms2)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stamp = str(int(time.time()))     # Unix epoch seconds (project rule)
        csv_path = os.path.join(OUTPUT_DIR, f"spectrogram_{stamp}.csv")
        grid_pth = os.path.join(OUTPUT_DIR, f"spectrogram_{stamp}.npz")
        png_path = os.path.join(OUTPUT_DIR, f"spectrogram_{stamp}.png")
        save_csv(csv_path, results)
        save_grid(grid_pth, freqs, amps, matrix)
        save_heatmap(png_path, freqs, amps, matrix, motor_index, motor_type,
                     amp_min, amp_max, freq_min, freq_max,
                     annotate_mode=annotate_mode)
        meta_path = save_meta(csv_path, png_path, grid_pth, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, motor_type,
                              amp_min, amp_max, freq_min, freq_max,
                              precision, freq_step, amp_step, measure_s,
                              annotate_mode, peak)

        log(f"\n=== Strongest vibration at freq={peak.freq_hz} Hz, "
            f"amp={peak.amp} ({peak.rms_ms2:.2f} m/s² RMS) ===")
        log(f"Grid:     {grid_pth}")
        log(f"Heatmap:  {png_path}")
        log(f"Data:     {csv_path}")
        log(f"Meta:     {meta_path}")

        return {
            "motor_type": motor_type,
            "peak_freq_hz": peak.freq_hz,
            "peak_amp": peak.amp,
            "peak_rms_ms2": peak.rms_ms2,
            "csv_path": csv_path,
            "png_path": png_path,
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
