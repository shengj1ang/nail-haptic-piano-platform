"""LRA frequency-response sweep.

Drives the LRA (wired to motor port 0 for this experiment) at a fixed
amplitude while stepping the PWM frequency across a configurable range,
measures the resulting vibration with the LIS3DH accelerometer, and
reports the resonant frequency - the frequency with the highest RMS
acceleration.

Two passes: a coarse sweep over the full range, then a fine 1 Hz sweep
around the coarse peak. Per-step results (CSV) and the response curve
(PNG) are saved under output/.

Requires firmware >= v2.6.0 (the 'F' frequency command and the
"ACC,id,x,y,z" stream format).
"""

import csv
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional, Tuple

import serial
from serial.tools import list_ports

import matplotlib

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt


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
BAUD = 115200

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


@dataclass
class StepResult:
    sweep_pass: str          # "coarse" or "fine"
    frequency_hz: int
    rms_delta: float
    peak_delta: float
    baseline_mag: float
    n_samples: int


# ==========================================
# Serial helpers
# ==========================================

def auto_detect_port() -> str:
    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found")

    scored = []
    for p in ports:
        score = 0
        name = (p.device or "").lower()
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()

        # Teensy's USB VID:PID is the strongest signal and needs no I/O.
        if p.vid == 0x16C0 and p.pid == 0x0483:
            score += 10
        if "teensy" in desc:
            score += 6
        if "usb" in name or "usb" in desc or "usb" in hwid:
            score += 3
        if sys.platform == "darwin" and ("usbmodem" in name or "cu." in name):
            score += 3
        if "bluetooth" in name or "bluetooth" in desc:
            score -= 10

        scored.append((score, p))

    scored.sort(key=lambda s: s[0], reverse=True)
    best_score, best = scored[0]
    if best_score > 0:
        print(f"Auto-selected: {best.device} ({best.description})")
        return best.device

    print("Could not auto-select device. Available ports:")
    for i, p in enumerate(ports):
        print(f"[{i}] {p.device} | {p.description}")
    while True:
        idx = input("Select index: ").strip()
        if idx.isdigit() and 0 <= int(idx) < len(ports):
            return ports[int(idx)].device


def send(ser: serial.Serial, cmd: str, wait_s: float = 0.05) -> None:
    ser.write((cmd.strip() + "\n").encode("utf-8"))
    ser.flush()
    if wait_s > 0:
        time.sleep(wait_s)


def open_rig() -> serial.Serial:
    port = auto_detect_port()
    ser = serial.Serial(port, BAUD, timeout=0.05)
    time.sleep(2.0)  # let the board settle after the port opens
    ser.reset_input_buffer()

    # Identity handshake: expect "E haptic-piano vX.Y.Z".
    send(ser, "E", wait_s=0.0)
    deadline = time.monotonic() + 1.0
    identity = ""
    while time.monotonic() < deadline:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if line.startswith("E "):
            identity = line
            break
    if "haptic-piano" not in identity:
        print(f"WARNING: unexpected identity reply {identity!r} - "
              "is this the right device / firmware >= v2.6.0?")
    else:
        print(f"Connected: {identity}")
    return ser


# ==========================================
# Sampling
# ==========================================

def parse_acc_line(raw: str, sensor_id: int = ACC_SENSOR_ID) -> Optional[Tuple[int, int, int]]:
    # Firmware >= v2.5.0 streams "ACC,<id>,x,y,z"; keep only the requested sensor.
    parts = raw.split(",")
    if len(parts) != 5 or parts[0] != "ACC":
        return None
    try:
        if int(parts[1]) != sensor_id:
            return None
        return int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


def collect_magnitudes(ser: serial.Serial, duration_s: float) -> List[float]:
    """Read the ACC stream for duration_s and return |a| magnitudes."""
    ser.reset_input_buffer()  # drop samples from before this window
    mags: List[float] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw)
        if sample is None:
            continue
        x, y, z = sample
        mags.append(math.sqrt(x * x + y * y + z * z))
    return mags


# ==========================================
# Sweep
# ==========================================

def measure_step(ser: serial.Serial, sweep_pass: str, freq: int) -> Optional[StepResult]:
    send(ser, f"F {MOTOR_INDEX} {freq}")

    baseline = collect_magnitudes(ser, BASELINE_S)
    if not baseline:
        print(f"  {freq} Hz: no baseline samples - is the stream running?")
        return None
    baseline_mag = statistics.fmean(baseline)

    send(ser, f"S {1 << MOTOR_INDEX} {AMP}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_magnitudes(ser, MEASURE_S)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        print(f"  {freq} Hz: no vibration samples")
        return None

    rms_delta = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in vib]))
    peak_delta = max(abs(m - baseline_mag) for m in vib)
    result = StepResult(sweep_pass, freq, rms_delta, peak_delta, baseline_mag, len(vib))
    print(f"  {freq:4d} Hz  rms={rms_delta:8.1f}  peak={peak_delta:8.1f}  n={len(vib)}")
    return result


def run_sweep(ser: serial.Serial, sweep_pass: str, freqs: List[int]) -> List[StepResult]:
    results: List[StepResult] = []
    for freq in freqs:
        step = measure_step(ser, sweep_pass, freq)
        if step is not None:
            results.append(step)
    return results


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
              resonance: StepResult) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))

    ax.plot([r.frequency_hz for r in coarse], [r.rms_delta for r in coarse],
            "o-", markersize=4, label=f"Coarse sweep ({COARSE_STEP_HZ} Hz steps)")
    if fine:
        ax.plot([r.frequency_hz for r in fine], [r.rms_delta for r in fine],
                "s-", markersize=4, label=f"Fine sweep ({FINE_STEP_HZ} Hz steps)")

    ax.axvline(resonance.frequency_hz, linestyle="--", linewidth=1.5,
               label=f"Resonance: {resonance.frequency_hz} Hz")

    ax.set_title(f"LRA frequency response (motor port {MOTOR_INDEX}, amp={AMP})")
    ax.set_xlabel("PWM frequency (Hz)")
    ax.set_ylabel("RMS acceleration delta (raw LIS3DH counts)")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ==========================================
# Main
# ==========================================

def main() -> None:
    coarse_freqs = list(range(COARSE_START_HZ, COARSE_STOP_HZ + 1, COARSE_STEP_HZ))
    step_s = BASELINE_S + SETTLE_S + MEASURE_S + REST_S
    est_s = (len(coarse_freqs) + 2 * FINE_SPAN_HZ // FINE_STEP_HZ + 1) * step_s
    print(f"LRA frequency sweep on motor port {MOTOR_INDEX}, amp={AMP}")
    print(f"Coarse: {COARSE_START_HZ}-{COARSE_STOP_HZ} Hz in {COARSE_STEP_HZ} Hz steps, "
          f"then fine +/-{FINE_SPAN_HZ} Hz in {FINE_STEP_HZ} Hz steps")
    print(f"Estimated duration: ~{est_s:.0f} s. Keep the rig still during the sweep.\n")

    ser = open_rig()
    try:
        send(ser, "X")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        print("Coarse sweep:")
        coarse = run_sweep(ser, "coarse", coarse_freqs)
        if not coarse:
            raise RuntimeError("Coarse sweep produced no data - check wiring and stream")

        coarse_peak = max(coarse, key=lambda r: r.rms_delta)
        print(f"\nCoarse peak: {coarse_peak.frequency_hz} Hz "
              f"(rms={coarse_peak.rms_delta:.1f})\n")

        fine_freqs = [f for f in range(coarse_peak.frequency_hz - FINE_SPAN_HZ,
                                       coarse_peak.frequency_hz + FINE_SPAN_HZ + 1,
                                       FINE_STEP_HZ)
                      if COARSE_START_HZ <= f <= COARSE_STOP_HZ]
        print("Fine sweep:")
        fine = run_sweep(ser, "fine", fine_freqs)

        candidates = fine if fine else coarse
        resonance = max(candidates, key=lambda r: r.rms_delta)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"frequency_response_{stamp}.png")
        save_csv(csv_path, coarse + fine)
        save_plot(png_path, coarse, fine, resonance)

        print(f"\n=== Resonant frequency: {resonance.frequency_hz} Hz ===")
        print(f"    rms_delta={resonance.rms_delta:.1f}, "
              f"peak_delta={resonance.peak_delta:.1f} (raw counts)")
        print(f"Data:  {csv_path}")
        print(f"Plot:  {png_path}")
        print(f"\nTo lock this in, have the host send: F {MOTOR_INDEX} "
              f"{resonance.frequency_hz} after connecting.")
    finally:
        # Leave the rig in a clean default state whatever happened.
        try:
            send(ser, "X")
            send(ser, f"F {MOTOR_INDEX} {DEFAULT_PWM_FREQ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        print("Serial port closed, pin frequency restored to "
              f"{DEFAULT_PWM_FREQ} Hz.")


if __name__ == "__main__":
    main()
