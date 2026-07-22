"""LRA frequency-response sweep.

Drives the LRA (wired to motor port 0 for this experiment) at a fixed
amplitude while stepping the PWM frequency across a configurable range,
measures the resulting vibration with the LIS3DH accelerometer, and
reports the resonant frequency - the frequency with the highest RMS
acceleration.

Two passes: a coarse sweep over the full range, then a fine 1 Hz sweep
around the coarse peak. Per-step results (CSV), the response curve
(PNG) and the run's parameter/result record (meta.json) are saved under
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

MOTOR_INDEX = 0          # LRA is wired to motor port 0 for this experiment
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

DEFAULT_PWM_FREQ = 224   # restored to the pin when the sweep ends (firmware boot default)

# Data lives under main/data/ like every other experiment output.
OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "lra_resonance_intensity_calibration"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (steps_done, steps_total)


@dataclass
class StepResult:
    sweep_pass: str          # "coarse" or "fine"
    frequency_hz: int
    rms_delta: float
    peak_delta: float
    baseline_mag: float
    n_samples: int


# ==========================================
# Sweep
# ==========================================

def measure_step(ser, sweep_pass: str, freq: int, log: LogFn,
                 motor_index: int = MOTOR_INDEX,
                 acc_sensor_id: int = ACC_SENSOR_ID,
                 amp: int = AMP) -> Optional[StepResult]:
    send(ser, f"F {motor_index} {freq}")

    baseline = collect_magnitudes(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        log(f"  {freq} Hz: no baseline samples - is the stream running?")
        return None
    baseline_mag = statistics.fmean(baseline)

    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_magnitudes(ser, MEASURE_S, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  {freq} Hz: no vibration samples")
        return None

    rms_delta = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in vib]))
    peak_delta = max(abs(m - baseline_mag) for m in vib)
    result = StepResult(sweep_pass, freq, rms_delta, peak_delta, baseline_mag, len(vib))
    log(f"  {freq:4d} Hz  rms={rms_delta:8.1f}  peak={peak_delta:8.1f}  n={len(vib)}")
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


def save_plot(path: str, coarse: List[StepResult], fine: List[StepResult],
              resonance: StepResult, motor_index: int = MOTOR_INDEX,
              amp: int = AMP) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))

    ax.plot([r.frequency_hz for r in coarse], [r.rms_delta for r in coarse],
            "o-", markersize=4, label=f"Coarse sweep ({COARSE_STEP_HZ} Hz steps)")
    if fine:
        ax.plot([r.frequency_hz for r in fine], [r.rms_delta for r in fine],
                "s-", markersize=4, label=f"Fine sweep ({FINE_STEP_HZ} Hz steps)")

    ax.axvline(resonance.frequency_hz, linestyle="--", linewidth=1.5,
               label=f"Resonance: {resonance.frequency_hz} Hz")

    ax.set_title(f"LRA frequency response (motor port {motor_index}, amp={amp})")
    ax.set_xlabel("PWM frequency (Hz)")
    ax.set_ylabel("RMS acceleration delta (raw LIS3DH counts)")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    """sweep_<ts>.csv -> sweep_<ts>.meta.json (same folder)."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def save_meta(csv_path: str, png_path: str, stamp: str, firmware,
              motor_index: int, acc_sensor_id: int, amp: int,
              resonance: StepResult) -> str:
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
        "files": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path),
        },
        "result": {
            "resonance_hz": resonance.frequency_hz,
            "rms_delta": resonance.rms_delta,
            "peak_delta": resonance.peak_delta,
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
    """Read back a saved sweep_*.csv (the exact columns save_csv writes)."""
    results: List[StepResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(StepResult(
                sweep_pass=row["sweep_pass"],
                frequency_hz=int(row["frequency_hz"]),
                rms_delta=float(row["rms_delta"]),
                peak_delta=float(row["peak_delta"]),
                baseline_mag=float(row["baseline_mag"]),
                n_samples=int(row["n_samples"]),
            ))
    if not results:
        raise ValueError(f"No sweep rows found in {csv_path}")
    return results


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None) -> dict:
    """Re-render the response curve from a saved sweep_*.csv - same plot
    and same resonance rule (fine pass preferred) as the live run. Plot
    labels come from the run's sibling .meta.json when it exists, so an
    old run re-renders with the parameters it was actually measured at.
    Returns a summary dict like run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    amp = params.get("amp", AMP)

    results = load_results(csv_path)
    coarse = [r for r in results if r.sweep_pass == "coarse"]
    fine = [r for r in results if r.sweep_pass == "fine"]
    candidates = fine if fine else coarse
    resonance = max(candidates, key=lambda r: r.rms_delta)
    save_plot(out_png, coarse, fine, resonance, motor_index=motor_index,
              amp=amp)
    return {
        "resonance_hz": resonance.frequency_hz,
        "rms_delta": resonance.rms_delta,
        "peak_delta": resonance.peak_delta,
        "csv_path": csv_path,
        "png_path": out_png,
    }


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
                   amp: int = AMP) -> dict:
    """Run the full two-pass sweep and write the output/ files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between steps;
    motor_index/acc_sensor_id/amp let it point the sweep at a different
    motor port / LIS3DH sensor / drive amplitude (amp 128 = maximum AC
    fundamental for an LRA - lower it only for a rig that must not be
    driven that hard). The defaults reproduce the original
    standalone-script behaviour exactly.
    Returns a summary dict (resonance + output paths).
    """
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    coarse_freqs = list(range(COARSE_START_HZ, COARSE_STOP_HZ + 1, COARSE_STEP_HZ))
    # Fine-pass length is only known after the coarse peak; assume the
    # full +/- span for the initial progress total and correct it later.
    total_steps = len(coarse_freqs) + 2 * FINE_SPAN_HZ // FINE_STEP_HZ + 1
    done_steps = 0

    log(f"LRA frequency sweep on motor port {motor_index}, amp={amp}")
    log(f"Coarse: {COARSE_START_HZ}-{COARSE_STOP_HZ} Hz in {COARSE_STEP_HZ} Hz steps, "
        f"then fine +/-{FINE_SPAN_HZ} Hz in {FINE_STEP_HZ} Hz steps")
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s. "
        "Keep the rig still during the sweep.\n")

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        log("Coarse sweep:")
        coarse: List[StepResult] = []
        for freq in coarse_freqs:
            if should_stop():
                raise SweepAborted()
            step = measure_step(ser, "coarse", freq, log,
                                motor_index=motor_index,
                                acc_sensor_id=acc_sensor_id, amp=amp)
            done_steps += 1
            progress(done_steps, total_steps)
            if step is not None:
                coarse.append(step)
        if not coarse:
            raise RuntimeError("Coarse sweep produced no data - check wiring and stream")

        coarse_peak = max(coarse, key=lambda r: r.rms_delta)
        log(f"\nCoarse peak: {coarse_peak.frequency_hz} Hz "
            f"(rms={coarse_peak.rms_delta:.1f})\n")

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
                                acc_sensor_id=acc_sensor_id, amp=amp)
            done_steps += 1
            progress(done_steps, total_steps)
            if step is not None:
                fine.append(step)

        candidates = fine if fine else coarse
        resonance = max(candidates, key=lambda r: r.rms_delta)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        stamp = str(int(time.time()))
        csv_path = os.path.join(OUTPUT_DIR, f"sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"frequency_response_{stamp}.png")
        save_csv(csv_path, coarse + fine)
        save_plot(png_path, coarse, fine, resonance, motor_index=motor_index,
                  amp=amp)
        meta_path = save_meta(csv_path, png_path, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, amp, resonance)

        log(f"\n=== Resonant frequency: {resonance.frequency_hz} Hz ===")
        log(f"    rms_delta={resonance.rms_delta:.1f}, "
            f"peak_delta={resonance.peak_delta:.1f} (raw counts)")
        log(f"Data:  {csv_path}")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")
        log(f"\nTo lock this in, have the host send: F {motor_index} "
            f"{resonance.frequency_hz} after connecting.")

        return {
            "resonance_hz": resonance.frequency_hz,
            "rms_delta": resonance.rms_delta,
            "peak_delta": resonance.peak_delta,
            "csv_path": csv_path,
            "png_path": png_path,
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
