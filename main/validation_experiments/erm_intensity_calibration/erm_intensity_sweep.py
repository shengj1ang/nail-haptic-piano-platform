"""ERM drive-intensity sweep.

Companion to the LRA resonance/intensity calibration, but for the
eccentric-rotating-mass (ERM) actuator. An ERM is *not* a resonator, so
the LRA's two-part method (find a resonant frequency, then pick an
amplitude at it) does not apply: an ERM is a DC motor whose eccentric
mass produces a rotating force. It has one drive knob - the average
voltage set by the PWM duty (`amp`) at a fixed kHz-range PWM frequency
that makes the chopped drive act as smooth DC - and that one knob sets
the vibration's amplitude AND its frequency together (both rise with
rotor speed). There is nothing to "tune to"; there is only "how hard to
drive it".

This sweep therefore steps `amp` at a fixed PWM frequency (5 kHz, so the
drive behaves like DC - see the motor_acc_delay experiment for why an
ERM needs kHz PWM), measures the resulting vibration with the LIS3DH,
and:

  * recommends the `amp` whose RMS acceleration lands in the same
    perception-based comfort band the LRA amplitude sweep targets
    (0.4-0.6 m/s^2 RMS, aim 0.5) - so the ERM cue is delivered at a
    matched intensity to the LRA cue;
  * reports the ERM-specific by-products the LRA sweep has no analogue
    for: the stiction/startup amp (the lowest drive at which the rotor
    actually spins) and the dominant vibration frequency at each amp
    (estimated by FFT of the accelerometer trace), which for an ERM is
    determined by the drive, not chosen.

Per-step results (CSV), the response curve (PNG) and the run's
parameter/result record (meta.json) are saved under
data/validation_experiments/erm_intensity_calibration/.

Requires firmware >= v2.9.0 (the 'F' frequency command, the
"ACC,id,x,y,z" stream, and the 1.344 kHz LIS3DH ODR that a 1 ms stream
needs to carry fresh samples for the frequency estimate).

Runs standalone (python erm_intensity_sweep.py) or through the
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
import numpy as np

try:
    from ..rig import SweepAborted, collect_samples, open_rig, send
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from rig import SweepAborted, collect_samples, open_rig, send


# ==========================================
# Experiment configuration
# ==========================================

MOTOR_INDEX = 10         # ERM test channel (wiring convention; LRA is 11)

# ERM needs kHz-range PWM so the chopped drive acts as smooth DC and can
# overcome stiction; 224 Hz (the LRA-resonance boot default since v2.7.0)
# leaves an ERM stalled. Matches ACTUATOR_PWM_HZ["ERM"] in the
# motor_acc_delay experiment. Restored to the boot default on exit.
PWM_HZ = 5000
DEFAULT_PWM_FREQ = 224   # firmware boot default, restored when the sweep ends

# One knob to sweep: the PWM duty. Unlike the LRA (whose usable range
# caps at amp 128 because it responds to the AC fundamental,
# sin(pi*amp/255)), an ERM driven as DC responds to the average voltage,
# which is monotonic in duty right up to full scale - so the full
# 0-255 range is meaningful.
AMP_VALUES = list(range(8, 256, 8))   # 8..248 in steps of 8

ACC_SENSOR_ID = 0
ACC_INTERVAL_MS = 1      # firmware >= v2.9.0: 1.344 kHz ODR, so a 1 ms
                         # stream carries fresh samples for the FFT

BASELINE_S = 0.30        # quiet window measured before each step
SETTLE_S = 0.30          # motor-on settling before measuring (ERM spin-up
                         # to steady speed is slower than an LRA ring-up)
MEASURE_S = 0.50         # measurement window (>= ~100 cycles at ERM speeds;
                         # sets the FFT resolution, ~1/MEASURE_S = 2 Hz)
REST_S = 0.20            # motor-off rest between steps (let the rotor stop)

# LIS3DH high-resolution mode, +/-2 g: 1 count = 1 mg.
MS2_PER_COUNT = 0.001 * 9.80665

# Target cue band (RMS, m/s^2) and the aim point inside it - the SAME
# perception-based band the LRA amplitude sweep uses, so both actuators
# deliver a matched-intensity cue. Caveat: the band was derived at the
# LRA's 200-250 Hz (Pacinian peak); an ERM's frequency depends on amp
# and may sit lower, where equal m/s^2 is not exactly equal perceived
# intensity - treat the recommendation as a starting point, not a
# perceptual equivalence.
TARGET_BAND_MS2 = (0.4, 0.6)
TARGET_MS2 = 0.5

# Frequency-estimate parameters. A step counts as "spinning" only when
# the FFT finds a peak this far above the median spectral power in the
# plausible ERM band; below that the drive did not overcome stiction (or
# is pure noise) and neither the frequency nor the intensity is real.
FREQ_MIN_HZ = 20.0       # ignore DC/drift/rig-sway below this
FREQ_MAX_HZ = 400.0      # ERM rotor speeds top out well below this
SPIN_SNR = 6.0           # peak power / median power to accept a spin

# Data lives under main/data/ like every other experiment output.
OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "erm_intensity_calibration"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (steps_done, steps_total)


@dataclass
class StepResult:
    amp: int
    rms_delta_counts: float
    rms_ms2: float
    peak_delta_counts: float
    dom_freq_hz: float       # NaN when no clear rotor peak (not spinning)
    baseline_mag: float
    n_samples: int


def is_spinning(r: StepResult) -> bool:
    """True when the rotor actually turned at this amp: the FFT found a
    clear rotor peak. Used identically by the live run and render_csv so
    the startup amp and the recommendation match between them."""
    return math.isfinite(r.dom_freq_hz)


# ==========================================
# Frequency estimate
# ==========================================

def dominant_frequency(samples: List[tuple]) -> float:
    """Estimate the ERM's vibration frequency from a (t, x, y, z) series.

    The eccentric mass makes the force vector rotate, so every axis
    oscillates at the rotor frequency; summing the three demeaned power
    spectra makes the estimate independent of how the rig is oriented.
    The sample rate is taken as the mean host-arrival rate over the
    window (individual arrivals jitter, but the mean equals the firmware
    ODR). Returns the peak frequency in Hz, or NaN when no peak stands
    clearly above the spectral noise (rotor not spinning)."""
    if len(samples) < 32:
        return math.nan
    ts = [s[0] for s in samples]
    span = ts[-1] - ts[0]
    if span <= 0:
        return math.nan
    fs = (len(samples) - 1) / span          # mean sample rate (Hz)
    n = len(samples)

    power = np.zeros(n // 2 + 1)
    for axis in (1, 2, 3):
        arr = np.array([s[axis] for s in samples], dtype=float)
        arr -= arr.mean()                   # drop DC / gravity
        power += np.abs(np.fft.rfft(arr)) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)

    band = (freqs >= FREQ_MIN_HZ) & (freqs <= min(FREQ_MAX_HZ, fs / 2))
    if not band.any():
        return math.nan
    band_power = power[band]
    band_freqs = freqs[band]
    peak_idx = int(np.argmax(band_power))
    median_power = float(np.median(power[freqs >= FREQ_MIN_HZ]))
    if median_power <= 0 or band_power[peak_idx] < SPIN_SNR * median_power:
        return math.nan
    return float(band_freqs[peak_idx])


# ==========================================
# Sweep
# ==========================================

def measure_amp(ser, amp: int, log: LogFn,
                motor_index: int = MOTOR_INDEX,
                acc_sensor_id: int = ACC_SENSOR_ID) -> Optional[StepResult]:
    baseline = collect_samples(ser, BASELINE_S, acc_sensor_id)
    if not baseline:
        log(f"  amp={amp}: no baseline samples - is the stream running?")
        return None
    baseline_mag = statistics.fmean(
        math.sqrt(x * x + y * y + z * z) for _, x, y, z in baseline)

    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_samples(ser, MEASURE_S, acc_sensor_id)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        log(f"  amp={amp}: no vibration samples")
        return None

    mags = [math.sqrt(x * x + y * y + z * z) for _, x, y, z in vib]
    rms_counts = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in mags]))
    peak_counts = max(abs(m - baseline_mag) for m in mags)
    dom_freq = dominant_frequency(vib)
    result = StepResult(amp, rms_counts, rms_counts * MS2_PER_COUNT,
                        peak_counts, dom_freq, baseline_mag, len(vib))
    freq_str = f"{dom_freq:5.1f} Hz" if math.isfinite(dom_freq) else "  stalled"
    log(f"  amp={amp:3d}  rms={rms_counts:7.1f} counts = {result.rms_ms2:5.3f} m/s^2  "
        f"f={freq_str}  n={len(vib)}")
    return result


# ==========================================
# Recommendation
# ==========================================

def choose(results: List[StepResult]):
    """From the measured steps pick the headline outcomes. Shared by the
    live run and render_csv so both report the same thing.
    Returns (recommended, in_band, startup_amp)."""
    spinning = [r for r in results if is_spinning(r)]
    candidates = spinning if spinning else results
    recommended = min(candidates, key=lambda r: abs(r.rms_ms2 - TARGET_MS2))
    in_band = [r for r in spinning
               if TARGET_BAND_MS2[0] <= r.rms_ms2 <= TARGET_BAND_MS2[1]]
    startup_amp = min((r.amp for r in spinning), default=None)
    return recommended, in_band, startup_amp


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
              startup_amp: Optional[int], motor_index: int = MOTOR_INDEX,
              pwm_hz: int = PWM_HZ) -> None:
    amps = [r.amp for r in results]
    ms2 = [r.rms_ms2 for r in results]

    fig, ax = plt.subplots(figsize=(10, 5))

    # Left axis: measured intensity, the target band and the pick.
    l_int, = ax.plot(amps, ms2, "o-", markersize=4, color="C0",
                     label="Measured RMS acceleration")
    ax.axhspan(TARGET_BAND_MS2[0], TARGET_BAND_MS2[1], color="C2", alpha=0.15,
               label=f"Target cue band {TARGET_BAND_MS2[0]}-{TARGET_BAND_MS2[1]} m/s²")
    l_rec = ax.axvline(recommended.amp, linestyle="--", linewidth=1.5, color="C3",
                       label=f"Recommended: amp={recommended.amp} "
                             f"({recommended.rms_ms2:.2f} m/s²)")
    handles = [l_int, ax.patches[0], l_rec]
    if startup_amp is not None:
        l_start = ax.axvline(startup_amp, linestyle=":", linewidth=1.3, color="0.4",
                             label=f"Startup: amp={startup_amp} (rotor begins to spin)")
        handles.append(l_start)

    ax.set_xlabel("amp (PWM duty, 0-255 scale)")
    ax.set_ylabel("RMS acceleration (m/s²)")
    ax.set_ylim(bottom=0)
    ax.grid(True)

    # Right axis: the by-product frequency, only where the rotor spun -
    # the ERM's defining trait is that intensity and frequency rise
    # together with the single drive knob.
    ax2 = ax.twinx()
    f_amps = [r.amp for r in results if is_spinning(r)]
    f_hz = [r.dom_freq_hz for r in results if is_spinning(r)]
    if f_amps:
        l_freq, = ax2.plot(f_amps, f_hz, "s--", markersize=4, color="C1",
                           alpha=0.8, label="Dominant vibration frequency")
        handles.append(l_freq)
    ax2.set_ylabel("Dominant vibration frequency (Hz)")
    ax2.set_ylim(bottom=0)

    ax.set_title(f"ERM intensity & frequency vs drive "
                 f"(motor port {motor_index}, {pwm_hz} Hz PWM)")
    ax.legend(handles=handles, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def meta_path_for(csv_path: str) -> str:
    """erm_sweep_<ts>.csv -> erm_sweep_<ts>.meta.json (same folder)."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def save_meta(csv_path: str, png_path: str, stamp: str, firmware,
              motor_index: int, acc_sensor_id: int, pwm_hz: int,
              recommended: StepResult, in_band: List[StepResult],
              startup_amp: Optional[int]) -> str:
    """Write the run's parameter/result record next to its CSV/PNG."""
    meta = {
        "experiment": "erm_intensity_sweep",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": {
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "pwm_hz": pwm_hz,
            "amp_values": AMP_VALUES,
            "target_band_ms2": list(TARGET_BAND_MS2),
            "target_ms2": TARGET_MS2,
            "ms2_per_count": MS2_PER_COUNT,
            "baseline_s": BASELINE_S,
            "settle_s": SETTLE_S,
            "measure_s": MEASURE_S,
            "rest_s": REST_S,
            "acc_interval_ms": ACC_INTERVAL_MS,
            "freq_min_hz": FREQ_MIN_HZ,
            "freq_max_hz": FREQ_MAX_HZ,
            "spin_snr": SPIN_SNR,
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path),
        },
        "result": {
            "recommended_amp": recommended.amp,
            "recommended_rms_ms2": recommended.rms_ms2,
            "recommended_freq_hz": (recommended.dom_freq_hz
                                    if math.isfinite(recommended.dom_freq_hz)
                                    else None),
            "in_band_amps": [r.amp for r in in_band],
            "startup_amp": startup_amp,
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
    """Read back a saved erm_sweep_*.csv (the exact columns save_csv writes)."""
    results: List[StepResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            results.append(StepResult(
                amp=int(row["amp"]),
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
    """Re-render the response curve from a saved erm_sweep_*.csv - same
    plot and same recommendation rule as the live run. Plot labels come
    from the run's sibling .meta.json when it exists, so an old run
    re-renders with the parameters it was actually measured at.
    Returns a summary dict like run_experiment()'s."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    if motor_index is None:
        motor_index = params.get("motor_index", MOTOR_INDEX)
    pwm_hz = params.get("pwm_hz", PWM_HZ)

    results = load_results(csv_path)
    recommended, in_band, startup_amp = choose(results)
    save_plot(out_png, results, recommended, startup_amp,
              motor_index=motor_index, pwm_hz=pwm_hz)
    return {
        "recommended_amp": recommended.amp,
        "recommended_rms_ms2": recommended.rms_ms2,
        "recommended_freq_hz": (recommended.dom_freq_hz
                                if math.isfinite(recommended.dom_freq_hz)
                                else None),
        "in_band_amps": [r.amp for r in in_band],
        "startup_amp": startup_amp,
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
                   pwm_hz: int = PWM_HZ) -> dict:
    """Run the ERM intensity sweep and write the output files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between steps;
    motor_index/acc_sensor_id/pwm_hz let it point the sweep at a
    different motor port / LIS3DH sensor / PWM frequency. pwm_hz should
    stay in the kHz range for any ERM (a sub-kHz drive stalls the rotor
    and invalidates the sweep); it is restored to the boot default when
    the sweep ends. The defaults reproduce the original standalone-script
    behaviour exactly.
    Returns a summary dict (recommended amp + output paths)."""
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    log(f"ERM intensity sweep on motor port {motor_index}, PWM {pwm_hz} Hz "
        "(chopped drive acts as DC)")
    log(f"amp values: {AMP_VALUES[0]}..{AMP_VALUES[-1]} in steps of "
        f"{AMP_VALUES[1] - AMP_VALUES[0]}")
    log(f"Estimated duration: ~{estimated_duration_s():.0f} s. "
        "Keep the rig still during the sweep.\n")

    ser = open_rig(log=log, interactive=interactive)
    try:
        send(ser, "X")
        send(ser, f"F {motor_index} {pwm_hz}")   # kHz PWM so the ERM spins
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

        recommended, in_band, startup_amp = choose(results)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Unix epoch seconds, per the project-wide epoch-timestamps rule.
        stamp = str(int(time.time()))
        csv_path = os.path.join(OUTPUT_DIR, f"erm_sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"erm_response_{stamp}.png")
        save_csv(csv_path, results)
        save_plot(png_path, results, recommended, startup_amp,
                  motor_index=motor_index, pwm_hz=pwm_hz)
        meta_path = save_meta(csv_path, png_path, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, pwm_hz,
                              recommended, in_band, startup_amp)

        if not any(is_spinning(r) for r in results):
            log("\nWARNING: the rotor never registered as spinning at any amp. "
                "Check the ERM is on this port and the PWM is in the kHz range; "
                "the numbers below are noise, not vibration.")
        freq_note = (f", spinning at {recommended.dom_freq_hz:.0f} Hz"
                     if math.isfinite(recommended.dom_freq_hz) else "")
        log(f"\n=== Recommended amp: {recommended.amp} "
            f"({recommended.rms_ms2:.2f} m/s² RMS, target {TARGET_MS2}{freq_note}) ===")
        if startup_amp is not None:
            log(f"Rotor starts spinning at amp={startup_amp} "
                "(below this the drive can't overcome stiction).")
        if in_band:
            band_str = ", ".join(f"{r.amp} ({r.rms_ms2:.2f})" for r in in_band)
            log(f"All amp values inside the {TARGET_BAND_MS2[0]}-"
                f"{TARGET_BAND_MS2[1]} m/s² band: {band_str}")
        else:
            log("No amp landed inside the target band - recommended is the "
                "closest measured point; consider widening AMP_VALUES or the band.")
        log(f"Data:  {csv_path}")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")

        return {
            "recommended_amp": recommended.amp,
            "recommended_rms_ms2": recommended.rms_ms2,
            "recommended_freq_hz": (recommended.dom_freq_hz
                                    if math.isfinite(recommended.dom_freq_hz)
                                    else None),
            "in_band_amps": [r.amp for r in in_band],
            "startup_amp": startup_amp,
            "csv_path": csv_path,
            "png_path": png_path,
        }
    finally:
        # Leave the rig in a clean default state whatever happened: the
        # kHz sweep frequency must not outlive the sweep on the port.
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
