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

BAUD = 115200
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


@dataclass
class StepResult:
    amp: int
    rms_delta_counts: float
    rms_ms2: float
    peak_delta_counts: float
    baseline_mag: float
    n_samples: int


# ==========================================
# Serial helpers (same conventions as lra_frequency_sweep.py)
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
    time.sleep(2.0)
    ser.reset_input_buffer()

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
    ser.reset_input_buffer()
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


def measure_amp(ser: serial.Serial, amp: int) -> Optional[StepResult]:
    baseline = collect_magnitudes(ser, BASELINE_S)
    if not baseline:
        print(f"  amp={amp}: no baseline samples - is the stream running?")
        return None
    baseline_mag = statistics.fmean(baseline)

    send(ser, f"S {1 << MOTOR_INDEX} {amp}", wait_s=0.0)
    time.sleep(SETTLE_S)
    vib = collect_magnitudes(ser, MEASURE_S)
    send(ser, "X", wait_s=0.0)
    time.sleep(REST_S)

    if not vib:
        print(f"  amp={amp}: no vibration samples")
        return None

    rms_counts = math.sqrt(statistics.fmean([(m - baseline_mag) ** 2 for m in vib]))
    peak_counts = max(abs(m - baseline_mag) for m in vib)
    result = StepResult(amp, rms_counts, rms_counts * MS2_PER_COUNT,
                        peak_counts, baseline_mag, len(vib))
    print(f"  amp={amp:3d}  rms={rms_counts:7.1f} counts = {result.rms_ms2:5.3f} m/s^2  "
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


def save_plot(path: str, results: List[StepResult], recommended: StepResult) -> None:
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

    ax.set_title(f"LRA amplitude response at {FREQ_HZ} Hz (motor port {MOTOR_INDEX})")
    ax.set_xlabel("amp (PWM duty, 0-255 scale)")
    ax.set_ylabel("RMS acceleration (m/s²)")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ==========================================
# Main
# ==========================================

def main() -> None:
    step_s = BASELINE_S + SETTLE_S + MEASURE_S + REST_S
    print(f"LRA amplitude sweep at {FREQ_HZ} Hz on motor port {MOTOR_INDEX}")
    print(f"amp values: {AMP_VALUES[0]}..{AMP_VALUES[-1]} in steps of "
          f"{AMP_VALUES[1] - AMP_VALUES[0]}")
    print(f"Estimated duration: ~{len(AMP_VALUES) * step_s:.0f} s. "
          "Keep the rig still during the sweep.\n")

    ser = open_rig()
    try:
        send(ser, "X")
        send(ser, f"F {MOTOR_INDEX} {FREQ_HZ}")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        results: List[StepResult] = []
        for amp in AMP_VALUES:
            step = measure_amp(ser, amp)
            if step is not None:
                results.append(step)
        if not results:
            raise RuntimeError("Sweep produced no data - check wiring and stream")

        recommended = min(results, key=lambda r: abs(r.rms_ms2 - TARGET_MS2))
        in_band = [r for r in results
                   if TARGET_BAND_MS2[0] <= r.rms_ms2 <= TARGET_BAND_MS2[1]]

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"amp_sweep_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"amplitude_response_{stamp}.png")
        save_csv(csv_path, results)
        save_plot(png_path, results, recommended)

        print(f"\n=== Recommended amp: {recommended.amp} "
              f"({recommended.rms_ms2:.2f} m/s² RMS, target {TARGET_MS2}) ===")
        if in_band:
            band_str = ", ".join(f"{r.amp} ({r.rms_ms2:.2f})" for r in in_band)
            print(f"All amp values inside the {TARGET_BAND_MS2[0]}-"
                  f"{TARGET_BAND_MS2[1]} m/s² band: {band_str}")
        print(f"Data:  {csv_path}")
        print(f"Plot:  {png_path}")
    finally:
        try:
            send(ser, "X")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        print("Serial port closed.")


if __name__ == "__main__":
    main()
