"""LRA amplitude-response sweep at the resonant frequency.

Companion to lra_frequency_sweep.py: with the PWM frequency fixed at the
measured resonance (224 Hz), this script steps the drive amplitude
(`amp` 0-128), measures the resulting RMS acceleration with the LIS3DH,
converts it to m/s^2, and recommends the amp value whose output lands in
the target "clearly perceptible, comfortable" band.

Target band rationale: fingertip vibrotactile detection threshold around
200-250 Hz (Pacinian peak sensitivity) is ~0.1-0.4 m/s^2 RMS; a clear
but non-annoying cue is typically set a bit above that, ~0.4-0.6 m/s^2
RMS. (ISO 2631-1 is a whole-body standard limited to 0.5-80 Hz and
ISO 5349-1 is an occupational-exposure standard - neither prescribes a
haptic-cue level, so the band below is perception-based with the ISO
comfort descriptors as a conservative cross-check.)

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
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from typing import Callable, List, Optional

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt

try:
    from ..rig import SweepAborted, collect_magnitudes, open_rig, send
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from rig import SweepAborted, collect_magnitudes, open_rig, send


# ==========================================
# Experiment configuration
# ==========================================

MOTOR_INDEX = 0          # LRA port (same rig as the frequency sweep)
FREQ_HZ = 224            # measured resonance, see lra_frequency_sweep results

AMP_VALUES = list(range(4, 129, 4))   # 4..128: the LRA's monotonic range

ACC_SENSOR_ID = 0
ACC_INTERVAL_MS = 3

BASELINE_S = 0.30
SETTLE_S = 0.15
MEASURE_S = 0.40
REST_S = 0.15

# LIS3DH high-resolution mode, +/-2 g: 1 count = 1 mg.
MS2_PER_COUNT = 0.001 * 9.80665

# Target cue band (RMS, m/s^2) and the point picked inside it.
TARGET_BAND_MS2 = (0.4, 0.6)
TARGET_MS2 = 0.5

# Data lives under main/data/ like every other experiment output.
OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "lra_resonance_intensity_calibration"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (steps_done, steps_total)


@dataclass
class StepResult:
    amp: int
    rms_delta_counts: float
    rms_ms2: float
    peak_delta_counts: float
    baseline_mag: float
    n_samples: int


# ==========================================
# Sweep
# ==========================================

def measure_amp(ser, amp: int, log: LogFn,
                motor_index: int = MOTOR_INDEX,
                acc_sensor_id: int = ACC_SENSOR_ID) -> Optional[StepResult]:
    baseline = collect_magnitudes(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        log(f"  amp={amp}: no baseline samples - is the stream running?")
        return None
    baseline_mag = statistics.fmean(baseline)

    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_magnitudes(ser, MEASURE_S, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  amp={amp}: no vibration samples")
        return None

    rms_counts = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in vib]))
    peak_counts = max(abs(m - baseline_mag) for m in vib)
    result = StepResult(amp, rms_counts, rms_counts * MS2_PER_COUNT,
                        peak_counts, baseline_mag, len(vib))
    log(f"  amp={amp:3d}  rms={rms_counts:7.1f} counts = {result.rms_ms2:5.3f} m/s^2  "
        f"n={len(vib)}")
    return result


# ==========================================
# Output
# ==========================================

def save_csv(path: str, results: List[StepResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def save_plot(path: str, results: List[StepResult], recommended: StepResult,
              motor_index: int = MOTOR_INDEX, freq_hz: int = FREQ_HZ) -> None:
    amps = [r.amp for r in results]
    ms2 = [r.rms_ms2 for r in results]
    max_ms2 = max(ms2)

    fig, ax = plt.subplots(figsize=(10, 5))

    # Theoretical shape for reference: intensity ~ sin(pi*amp/255),
    # scaled to the measured maximum.
    theory = [max_ms2 * math.sin(math.pi * a / 255) / math.sin(math.pi * max(amps) / 255)
              for a in amps]
    ax.plot(amps, theory, "--", linewidth=1.2, alpha=0.7,
            label="sin(π·amp/255) model (scaled)")

    ax.plot(amps, ms2, "o-", markersize=4, label="Measured RMS acceleration")

    ax.axhspan(TARGET_BAND_MS2[0], TARGET_BAND_MS2[1], alpha=0.15,
               label=f"Target cue band {TARGET_BAND_MS2[0]}-{TARGET_BAND_MS2[1]} m/s²")
    ax.axvline(recommended.amp, linestyle="--", linewidth=1.5,
               label=f"Recommended: amp={recommended.amp} "
                     f"({recommended.rms_ms2:.2f} m/s²)")

    ax.set_title(f"LRA amplitude response at {freq_hz} Hz (motor port {motor_index})")
    ax.set_xlabel("amp (PWM duty, 0-255 scale)")
    ax.set_ylabel("RMS acceleration (m/s²)")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    """amp_sweep_<ts>.csv -> amp_sweep_<ts>.meta.json (same folder)."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def save_meta(csv_path: str, png_path: str, stamp: str, firmware,
              motor_index: int, acc_sensor_id: int, freq_hz: int,
              recommended: StepResult, in_band: List[StepResult]) -> str:
    """Write the run's parameter/result record next to its CSV/PNG."""
    meta = {
        "experiment": "lra_amplitude_sweep",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "freq_hz": freq_hz,
            "amp_values": AMP_VALUES,
            "target_band_ms2": list(TARGET_BAND_MS2),
            "target_ms2": TARGET_MS2,
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
        },
        "result": {
            "recommended_amp": recommended.amp,
            "recommended_rms_ms2": recommended.rms_ms2,
            "in_band_amps": [r.amp for r in in_band],
        },
    }
    path = meta_path_for(csv_path)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
    return path


def load_meta(csv_path: str) -> Optional[dict]:
    path = meta_path_for(csv_path)
    if not os.path.exists(path):
        return None  # e.g. a CSV predating the meta files
    with open(path) as f:
        return json.load(f)


def load_results(csv_path: str) -> List[StepResult]:
    """Read back a saved amp_sweep_*.csv (the exact columns save_csv writes)."""
    results: List[StepResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(StepResult(
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


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None) -> dict:
    """Re-render the response curve from a saved amp_sweep_*.csv - same
    plot and same recommendation rule as the live run. Plot labels come
    from the run's sibling .meta.json when it exists, so an old run
    re-renders with the parameters it was actually measured at.
    Returns a summary dict like run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    freq_hz = params.get("freq_hz", FREQ_HZ)

    results = load_results(csv_path)
    recommended = min(results, key=lambda r: abs(r.rms_ms2 - TARGET_MS2))
    in_band = [r for r in results
               if TARGET_BAND_MS2[0] <= r.rms_ms2 <= TARGET_BAND_MS2[1]]
    save_plot(out_png, results, recommended, motor_index=motor_index,
              freq_hz=freq_hz)
    return {
        "recommended_amp": recommended.amp,
        "recommended_rms_ms2": recommended.rms_ms2,
        "in_band_amps": [r.amp for r in in_band],
        "csv_path": csv_path,
        "png_path": out_png,
    }


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
                   freq_hz: int = FREQ_HZ) -> dict:
    """Run the amplitude sweep and write the output/ files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between steps;
    motor_index/acc_sensor_id/freq_hz let it point the sweep at a
    different motor port / LIS3DH sensor / PWM frequency (change
    freq_hz only after re-measuring the resonance with the frequency
    sweep - amplitudes measured off-resonance are meaningless for an
    LRA). The defaults reproduce the original standalone-script
    behaviour exactly.
    Returns a summary dict (recommended amp + output paths).
    """
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    log(f"LRA amplitude sweep at {freq_hz} Hz on motor port {motor_index}")
    log(f"amp values: {AMP_VALUES[0]}..{AMP_VALUES[-1]} in steps of "
        f"{AMP_VALUES[1] - AMP_VALUES[0]}")
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s. "
        "Keep the rig still during the sweep.\n")

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
                               acc_sensor_id=acc_sensor_id)
            progress(i + 1, len(AMP_VALUES))
            if step is not None:
                results.append(step)
        if not results:
            raise RuntimeError("Sweep produced no data - check wiring and stream")

        recommended = min(results, key=lambda r: abs(r.rms_ms2 - TARGET_MS2))
        in_band = [r for r in results
                   if TARGET_BAND_MS2[0] <= r.rms_ms2 <= TARGET_BAND_MS2[1]]

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        stamp = str(int(time.time()))
        csv_path = os.path.join(OUTPUT_DIR, f"amp_sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"amplitude_response_{stamp}.png")
        save_csv(csv_path, results)
        save_plot(png_path, results, recommended, motor_index=motor_index,
                  freq_hz=freq_hz)
        meta_path = save_meta(csv_path, png_path, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, freq_hz,
                              recommended, in_band)

        log(f"\n=== Recommended amp: {recommended.amp} "
            f"({recommended.rms_ms2:.2f} m/s² RMS, target {TARGET_MS2}) ===")
        if in_band:
            band_str = ", ".join(f"{r.amp} ({r.rms_ms2:.2f})" for r in in_band)
            log(f"All amp values inside the {TARGET_BAND_MS2[0]}-"
                f"{TARGET_BAND_MS2[1]} m/s² band: {band_str}")
        log(f"Data:  {csv_path}")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")

        return {
            "recommended_amp": recommended.amp,
            "recommended_rms_ms2": recommended.rms_ms2,
            "in_band_amps": [r.amp for r in in_band],
            "csv_path": csv_path,
            "png_path": png_path,
        }
    finally:
        try:
            send(ser, "X")
            if freq_hz != FREQ_HZ:
                # A custom sweep frequency shouldn't outlive the sweep -
                # put the pin back on the boot-default resonance.
                send(ser, f"F {motor_index} {FREQ_HZ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        log("Serial port closed.")


def main() -> None:
    run_experiment()


if __name__ == "__main__":
    main()
