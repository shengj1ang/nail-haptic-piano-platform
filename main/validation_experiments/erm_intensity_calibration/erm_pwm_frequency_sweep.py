"""ERM PWM-frequency adequacy sweep.

Companion to erm_intensity_sweep.py. Where the intensity sweep fixes the
PWM frequency and steps the drive `amp`, this one fixes `amp` and steps
the **PWM carrier frequency** - but note what it is and is NOT:

  * It is NOT a resonance sweep. An ERM is not a resonator; the 'F'
    command sets the PWM carrier, not a vibration frequency, so there is
    no resonant peak to find (that is the whole difference from the LRA
    frequency sweep).
  * It IS a check of how high the PWM carrier must be for the chopped
    drive to behave like smooth DC. Below that the motor's rotor tries
    to follow the chop (it pulses / stalls, and the dominant vibration
    frequency tracks the PWM frequency); above it the rotor spins
    steadily (RMS plateaus and the dominant frequency settles at the
    rotor speed, independent of the PWM frequency). The crossover is the
    minimum adequate PWM frequency - the number that justifies driving
    the ERM at 5 kHz.

`amp` is fixed at **128 (50% duty)** by default: that is the point of
maximum chopping, where the PWM frequency has the most leverage. It is
NOT 255 - at 100% duty the pin is constant-on DC and the PWM frequency
has no effect at all, so the sweep would be flat. (This mirrors the LRA
frequency sweep fixing amp at 128 for the maximum AC fundamental.)

Per-step results (CSV), the response curve (PNG) and the run's
parameter/result record (meta.json) are saved under
data/validation_experiments/erm_intensity_calibration/, alongside the
intensity sweep's output.

Requires firmware >= v2.9.0 (the 'F' command, the "ACC,id,x,y,z" stream,
and the 1.344 kHz LIS3DH ODR the frequency FFT needs).

Runs standalone (python erm_pwm_frequency_sweep.py) or through the
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
    from .erm_intensity_sweep import MS2_PER_COUNT, dominant_frequency
except ImportError:  # direct execution rather than package import
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.append(os.path.dirname(_here))   # validation_experiments/ (for rig)
    sys.path.append(_here)                     # this folder (for the sibling module)
    from rig import SweepAborted, collect_samples, open_rig, send
    from erm_intensity_sweep import MS2_PER_COUNT, dominant_frequency


# ==========================================
# Experiment configuration
# ==========================================

MOTOR_INDEX = 10         # ERM test channel (wiring convention; LRA is 11)

# Fixed drive amplitude. 128 = 50% duty = maximum chopping, so the PWM
# frequency has the most leverage on whether the drive acts as DC. NOT
# 255: at 100% duty the pin is constant-on DC and the PWM frequency does
# nothing, making the sweep flat. (Same reasoning as the LRA frequency
# sweep's amp=128.)
AMP = 128

# The PWM carrier frequencies to step, log-spaced so the interesting
# low-kHz transition is well resolved without wasting steps up high.
# Kept inside the firmware 'F' command's 50-20000 Hz range.
FREQ_START_HZ = 100
FREQ_STOP_HZ = 16000
N_FREQS = 25
ADOPTED_PWM_HZ = 5000    # the value this sweep validates (drawn for reference)
DEFAULT_PWM_FREQ = 224   # firmware boot default, restored when the sweep ends

ACC_SENSOR_ID = 0
ACC_INTERVAL_MS = 1      # firmware >= v2.9.0: 1.344 kHz ODR

BASELINE_S = 0.30
SETTLE_S = 0.30
MEASURE_S = 0.50
REST_S = 0.20

# Adequacy criteria: a PWM-frequency step counts as "acting like DC" when
# both its RMS and its rotor frequency have settled to the high-frequency
# plateau (the top PLATEAU_N steps, which are safely in the DC regime).
PLATEAU_N = 3
RMS_TOL = 0.15           # within +/-15% of the plateau RMS
FREQ_TOL_HZ = 25.0       # rotor freq within this of the plateau rotor freq

# Data lives in the ERM calibration folder, next to the intensity sweep.
OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "erm_intensity_calibration"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (steps_done, steps_total)


@dataclass
class StepResult:
    pwm_freq_hz: int
    rms_delta_counts: float
    rms_ms2: float
    peak_delta_counts: float
    dom_freq_hz: float       # NaN when no clear vibration peak
    baseline_mag: float
    n_samples: int


def frequency_list() -> List[int]:
    """Log-spaced PWM carrier frequencies, de-duplicated and sorted."""
    raw = np.geomspace(FREQ_START_HZ, FREQ_STOP_HZ, N_FREQS)
    return sorted({int(round(f)) for f in raw})


# ==========================================
# Sweep
# ==========================================

def measure_step(ser, pwm_freq: int, log: LogFn,
                 motor_index: int = MOTOR_INDEX,
                 acc_sensor_id: int = ACC_SENSOR_ID,
                 amp: int = AMP) -> Optional[StepResult]:
    send(ser, f"F {motor_index} {pwm_freq}")

    baseline = collect_samples(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        log(f"  {pwm_freq} Hz: no baseline samples - is the stream running?")
        return None
    baseline_mag = statistics.fmean(
        math.sqrt(x * x + y * y + z * z) for _, x, y, z in baseline)

    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_samples(ser, MEASURE_S, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  {pwm_freq} Hz: no vibration samples")
        return None

    mags = [math.sqrt(x * x + y * y + z * z) for _, x, y, z in vib]
    rms_counts = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in mags]))
    peak_counts = max(abs(m - baseline_mag) for m in mags)
    dom_freq = dominant_frequency(vib)
    result = StepResult(pwm_freq, rms_counts, rms_counts * MS2_PER_COUNT,
                        peak_counts, dom_freq, baseline_mag, len(vib))
    freq_str = f"{dom_freq:5.1f} Hz" if math.isfinite(dom_freq) else "  none"
    log(f"  PWM={pwm_freq:6d} Hz  rms={result.rms_ms2:5.3f} m/s^2  "
        f"vib_f={freq_str}  n={len(vib)}")
    return result


# ==========================================
# Recommendation
# ==========================================

def choose(results: List[StepResult]):
    """Find the minimum adequate PWM frequency: the lowest carrier from
    which every higher carrier drives the rotor as smooth DC (RMS and
    rotor frequency both settled to the high-frequency plateau). Shared
    by the live run and render_csv so both report the same thing.
    Returns (recommended, rms_ref, frot_ref, adequate) - recommended is
    None if the rotor never moved."""
    moving = [r for r in results if math.isfinite(r.dom_freq_hz)]
    if not moving:
        return None, None, None, []

    by_freq = sorted(moving, key=lambda r: r.pwm_freq_hz)
    plateau = by_freq[-PLATEAU_N:] if len(by_freq) >= PLATEAU_N else by_freq
    rms_ref = statistics.median(r.rms_ms2 for r in plateau)
    frot_ref = statistics.median(r.dom_freq_hz for r in plateau)

    def adequate(r: StepResult) -> bool:
        return (abs(r.rms_ms2 - rms_ref) <= RMS_TOL * rms_ref
                and abs(r.dom_freq_hz - frot_ref) <= FREQ_TOL_HZ)

    adequate_list = [r for r in by_freq if adequate(r)]

    # Walk down from the top while every step stays adequate: the lowest
    # such carrier is the threshold (a lone adequate outlier down low,
    # with an inadequate step above it, does not count).
    recommended = None
    for r in reversed(by_freq):
        if adequate(r):
            recommended = r
        else:
            break
    return recommended, rms_ref, frot_ref, adequate_list


# ==========================================
# Output
# ==========================================

def save_csv(path: str, results: List[StepResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def save_plot(path: str, results: List[StepResult],
              recommended: Optional[StepResult], rms_ref: Optional[float],
              motor_index: int = MOTOR_INDEX, amp: int = AMP) -> None:
    freqs = [r.pwm_freq_hz for r in results]
    ms2 = [r.rms_ms2 for r in results]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.set_xscale("log")

    l_int, = ax.plot(freqs, ms2, "o-", markersize=4, color="C0",
                     label="Measured RMS acceleration")
    handles = [l_int]
    if rms_ref is not None:
        band = ax.axhspan(rms_ref * (1 - RMS_TOL), rms_ref * (1 + RMS_TOL),
                          color="C2", alpha=0.15,
                          label=f"DC-regime plateau ±{int(RMS_TOL*100)}%")
        handles.append(band)
    if recommended is not None:
        l_rec = ax.axvline(recommended.pwm_freq_hz, linestyle="--", linewidth=1.5,
                           color="C3",
                           label=f"Min adequate PWM: {recommended.pwm_freq_hz} Hz")
        handles.append(l_rec)
    l_adopt = ax.axvline(ADOPTED_PWM_HZ, linestyle=":", linewidth=1.3, color="0.4",
                        label=f"Adopted drive: {ADOPTED_PWM_HZ} Hz")
    handles.append(l_adopt)

    ax.set_xlabel("PWM carrier frequency (Hz, log scale)")
    ax.set_ylabel("RMS acceleration (m/s²)")
    ax.set_ylim(bottom=0)
    ax.grid(True, which="both", alpha=0.4)

    # Right axis: dominant vibration frequency. The dashed y=x line marks
    # where the rotor is merely following the PWM chop (pulsing); once the
    # points drop below it and flatten, the drive is acting as DC and the
    # value is the steady rotor speed.
    ax2 = ax.twinx()
    f_freqs = [r.pwm_freq_hz for r in results if math.isfinite(r.dom_freq_hz)]
    f_hz = [r.dom_freq_hz for r in results if math.isfinite(r.dom_freq_hz)]
    if f_freqs:
        l_vib, = ax2.plot(f_freqs, f_hz, "s--", markersize=4, color="C1",
                          alpha=0.8, label="Dominant vibration frequency")
        handles.append(l_vib)
        l_follow, = ax2.plot(freqs, freqs, ":", color="C1", alpha=0.4,
                             label="rotor follows chop (vib = PWM)")
        handles.append(l_follow)
    ax2.set_ylabel("Dominant vibration frequency (Hz)")
    ax2.set_ylim(bottom=0)

    ax.set_title(f"ERM PWM-frequency adequacy at fixed amp={amp} "
                 f"(motor port {motor_index})")
    ax.legend(handles=handles, loc="center right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    """erm_pwm_sweep_<ts>.csv -> erm_pwm_sweep_<ts>.meta.json (same folder)."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def save_meta(csv_path: str, png_path: str, stamp: str, firmware,
              motor_index: int, acc_sensor_id: int, amp: int,
              recommended: Optional[StepResult], rms_ref: Optional[float],
              frot_ref: Optional[float]) -> str:
    """Write the run's parameter/result record next to its CSV/PNG."""
    meta = {
        "experiment": "erm_pwm_frequency_sweep",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "amp": amp,
            "freq_start_hz": FREQ_START_HZ,
            "freq_stop_hz": FREQ_STOP_HZ,
            "n_freqs": N_FREQS,
            "adopted_pwm_hz": ADOPTED_PWM_HZ,
            "ms2_per_count": MS2_PER_COUNT,
            "baseline_s": BASELINE_S,
            "settle_s": SETTLE_S,
            "measure_s": MEASURE_S,
            "rest_s": REST_S,
            "acc_interval_ms": ACC_INTERVAL_MS,
            "plateau_n": PLATEAU_N,
            "rms_tol": RMS_TOL,
            "freq_tol_hz": FREQ_TOL_HZ,
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path),
        },
        "result": {
            "min_adequate_pwm_hz": (recommended.pwm_freq_hz
                                    if recommended is not None else None),
            "plateau_rms_ms2": rms_ref,
            "plateau_rotor_freq_hz": frot_ref,
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


def load_results(csv_path: str) -> List[StepResult]:
    """Read back a saved erm_pwm_sweep_*.csv (the columns save_csv writes)."""
    results: List[StepResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(StepResult(
                pwm_freq_hz=int(row["pwm_freq_hz"]),
                rms_delta_counts=float(row["rms_delta_counts"]),
                rms_ms2=float(row["rms_ms2"]),
                peak_delta_counts=float(row["peak_delta_counts"]),
                dom_freq_hz=float(row["dom_freq_hz"]),  # "nan" -> NaN
                baseline_mag=float(row["baseline_mag"]),
                n_samples=int(row["n_samples"]),
            ))
    if not results:
        raise ValueError(f"No sweep rows found in {csv_path}")
    return results


def render_csv(csv_path: str, out_png: str,
               motor_index: Optional[int] = None) -> dict:
    """Re-render the response curve from a saved erm_pwm_sweep_*.csv - same
    plot and same recommendation rule as the live run. Labels come from
    the sibling .meta.json when present. Returns a summary dict like
    run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    amp = params.get("amp", AMP)

    results = load_results(csv_path)
    recommended, rms_ref, frot_ref, _ = choose(results)
    save_plot(out_png, results, recommended, rms_ref,
              motor_index=motor_index, amp=amp)
    return {
        "min_adequate_pwm_hz": (recommended.pwm_freq_hz
                                if recommended is not None else None),
        "plateau_rms_ms2": rms_ref,
        "plateau_rotor_freq_hz": frot_ref,
        "csv_path": csv_path,
        "png_path": out_png,
    }


# ==========================================
# Experiment
# ==========================================

def estimated_duration_s() -> float:
    step_s = BASELINE_S + SETTLE_S + MEASURE_S + REST_S
    return len(frequency_list()) * step_s


def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   amp: int = AMP) -> dict:
    """Step the PWM carrier at a fixed drive amp and report the minimum
    adequate PWM frequency. `amp` should stay below 255 (at 100% duty the
    PWM frequency has no effect - the sweep would be flat); 128 (50% duty)
    is the maximum-leverage point. The port's PWM frequency is restored to
    the boot default when the sweep ends. The defaults reproduce the
    standalone-script behaviour exactly.
    Returns a summary dict (min adequate PWM freq + output paths)."""
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    if amp >= 255:
        log("WARNING: amp=255 is 100% duty (constant-on DC) - the PWM "
            "frequency has no effect and the sweep will be flat. Use amp "
            "< 255 (128 = 50% duty is the maximum-leverage point).")

    freqs = frequency_list()
    log(f"ERM PWM-frequency sweep on motor port {motor_index}, fixed amp={amp}")
    log(f"PWM carrier: {freqs[0]}..{freqs[-1]} Hz, {len(freqs)} log-spaced steps")
    log("NOTE: an ERM has no resonance - this finds the PWM frequency above "
        "which the drive acts as DC, not a 'best' vibration frequency.")
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s. "
        "Keep the rig still during the sweep.\n")

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        results: List[StepResult] = []
        for i, pwm_freq in enumerate(freqs):
            if should_stop():
                raise SweepAborted()
            step = measure_step(ser, pwm_freq, log,
                                motor_index=motor_index,
                                acc_sensor_id=acc_sensor_id, amp=amp)
            progress(i + 1, len(freqs))
            if step is not None:
                results.append(step)
        if not results:
            raise RuntimeError("Sweep produced no data - check wiring and stream")

        recommended, rms_ref, frot_ref, adequate = choose(results)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        stamp = str(int(time.time()))
        csv_path = os.path.join(OUTPUT_DIR, f"erm_pwm_sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"erm_pwm_response_{stamp}.png")
        save_csv(csv_path, results)
        save_plot(png_path, results, recommended, rms_ref,
                  motor_index=motor_index, amp=amp)
        meta_path = save_meta(csv_path, png_path, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, amp,
                              recommended, rms_ref, frot_ref)

        if recommended is None:
            log("\nWARNING: the rotor never registered as moving at any PWM "
                "frequency. Check the ERM is on this port and the amp is high "
                "enough to spin it.")
        else:
            log(f"\n=== Minimum adequate PWM frequency: "
                f"{recommended.pwm_freq_hz} Hz ===")
            log(f"    Above this the drive acts as DC: RMS ~{rms_ref:.2f} m/s², "
                f"rotor ~{frot_ref:.0f} Hz (independent of the PWM frequency).")
            if recommended.pwm_freq_hz <= ADOPTED_PWM_HZ:
                log(f"    The adopted {ADOPTED_PWM_HZ} Hz drive is comfortably "
                    "above the threshold - confirmed adequate.")
            else:
                log(f"    NOTE: the threshold is ABOVE the adopted "
                    f"{ADOPTED_PWM_HZ} Hz - consider raising the ERM drive "
                    "frequency.")
        log(f"Data:  {csv_path}")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")

        return {
            "min_adequate_pwm_hz": (recommended.pwm_freq_hz
                                    if recommended is not None else None),
            "plateau_rms_ms2": rms_ref,
            "plateau_rotor_freq_hz": frot_ref,
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
