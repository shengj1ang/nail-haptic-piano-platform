import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import matplotlib.pyplot as plt
import serial
from serial.tools import list_ports

from controller import VibratorController


# =========================
# User-adjustable parameters
# =========================
ACC_BAUD = 115200
ACC_START_CMD = "A START 10"
ACC_STOP_CMD = "A STOP"

MOTOR_IDX = 11          # You confirmed key 'v' -> motor 11
AMP = 80                # Vibration amplitude
VIB_DURATION_S = 0.20   # Motor ON duration for each trial

NUM_TRIALS = 3          # Adjustable: how many trials to measure
BASELINE_DURATION_S = 1.0
INTER_TRIAL_REST_S = 1.0
SEARCH_TIMEOUT_S = 3.0

THRESHOLD = 300         # Adjustable detection threshold
CONSECUTIVE_HITS = 3    # Require this many consecutive samples above threshold

SAVE_FIG_PATH = "motor_acc_delay_summary.png"
SHOW_FIG = True


NUM_TRIALS = 10
THRESHOLD = 150
CONSECUTIVE_HITS = 1
ACC_START_CMD = "A START 2"   # 如果固件支持

@dataclass
class TrialResult:
    trial_id: int
    command_time: float
    detected_time: Optional[float]
    delay_ms: Optional[float]
    baseline_mag: float
    peak_delta: float
    detected_sample_index: Optional[int]
    status: str
    rel_times: List[float]
    deltas: List[float]
    detect_rel_time: Optional[float]


# =========================
# Serial helpers
# =========================
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
        manufacturer = (getattr(p, "manufacturer", "") or "").lower()

        if "usb" in name or "usb" in desc or "usb" in hwid:
            score += 3
        if "serial" in desc:
            score += 1
        if "arduino" in desc or "arduino" in manufacturer:
            score += 5
        if "teensy" in desc or "teensy" in manufacturer:
            score += 6
        if "ch340" in desc or "cp210" in desc or "ftdi" in desc:
            score += 4
        if "ttyacm" in name or "ttyusb" in name:
            score += 4

        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]
    best_ports = [p for s, p in scored if s == best_score and s > 0]

    if len(best_ports) == 1:
        p = best_ports[0]
        print(f"Auto-selected ACC port: {p.device} ({p.description})")
        return p.device

    print("Available serial ports:")
    for i, p in enumerate(ports):
        print(f"[{i}] {p.device} | {p.description}")

    while True:
        idx = input("Select ACC serial index: ").strip()
        if idx.isdigit() and 0 <= int(idx) < len(ports):
            return ports[int(idx)].device



def send_serial_command(ser: serial.Serial, cmd: str, wait_s: float = 0.15) -> None:
    ser.write((cmd.strip() + "\n").encode("utf-8"))
    ser.flush()
    time.sleep(wait_s)



def parse_acc_line(raw: str, sensor_id: int = 0) -> Optional[Tuple[int, int, int]]:
    # Firmware >= v2.5.0 streams "ACC,<id>,x,y,z"; keep only the requested sensor.
    parts = raw.split(",")
    if len(parts) != 5 or parts[0] != "ACC":
        return None
    try:
        if int(parts[1]) != sensor_id:
            return None
        x = int(parts[2])
        y = int(parts[3])
        z = int(parts[4])
        return x, y, z
    except ValueError:
        return None



def magnitude(x: int, y: int, z: int) -> float:
    return math.sqrt(x * x + y * y + z * z)


# =========================
# Experiment logic
# =========================
def collect_baseline(ser: serial.Serial, duration_s: float) -> float:
    values = []
    t0 = time.perf_counter()

    while time.perf_counter() - t0 < duration_s:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        parsed = parse_acc_line(raw)
        if parsed is None:
            continue
        values.append(magnitude(*parsed))

    if len(values) < 10:
        raise RuntimeError("Not enough ACC samples collected for baseline")

    return sum(values) / len(values)



def run_single_trial(
    ser: serial.Serial,
    vc: VibratorController,
    trial_id: int,
    motor_idx: int,
    amp: int,
    vib_duration_s: float,
    baseline_duration_s: float,
    threshold: float,
    consecutive_hits: int,
    search_timeout_s: float,
) -> TrialResult:
    print(f"\n===== Trial {trial_id} =====")
    print("Collecting baseline...")
    baseline_mag = collect_baseline(ser, baseline_duration_s)
    print(f"Baseline magnitude: {baseline_mag:.2f}")

    ser.reset_input_buffer()

    mask = 1 << motor_idx
    rel_times: List[float] = []
    deltas: List[float] = []

    hit_count = 0
    peak_delta = 0.0
    detected_time = None
    detected_sample_index = None
    detect_rel_time = None

    t_cmd = time.perf_counter()
    vc.send(f"S {mask} {amp}")
    t_off = t_cmd + vib_duration_s

    start_search = time.perf_counter()
    sample_index = 0

    while time.perf_counter() - start_search < search_timeout_s:
        now = time.perf_counter()
        if now >= t_off:
            vc.send(f"S 0 {amp}")
            t_off = float("inf")

        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue

        parsed = parse_acc_line(raw)
        if parsed is None:
            continue

        x, y, z = parsed
        mag = magnitude(x, y, z)
        delta = abs(mag - baseline_mag)
        peak_delta = max(peak_delta, delta)

        rel_t = time.perf_counter() - t_cmd
        rel_times.append(rel_t)
        deltas.append(delta)

        if delta > threshold:
            hit_count += 1
            if hit_count >= consecutive_hits:
                detected_time = time.perf_counter()
                detected_sample_index = sample_index
                detect_rel_time = detected_time - t_cmd
                break
        else:
            hit_count = 0

        sample_index += 1

    vc.stop_all()

    if detected_time is None:
        print(f"Trial {trial_id}: no detection within {search_timeout_s:.2f}s")
        return TrialResult(
            trial_id=trial_id,
            command_time=t_cmd,
            detected_time=None,
            delay_ms=None,
            baseline_mag=baseline_mag,
            peak_delta=peak_delta,
            detected_sample_index=None,
            status="timeout",
            rel_times=rel_times,
            deltas=deltas,
            detect_rel_time=None,
        )

    delay_ms = (detected_time - t_cmd) * 1000.0
    print(f"Trial {trial_id}: detected after {delay_ms:.2f} ms")

    return TrialResult(
        trial_id=trial_id,
        command_time=t_cmd,
        detected_time=detected_time,
        delay_ms=delay_ms,
        baseline_mag=baseline_mag,
        peak_delta=peak_delta,
        detected_sample_index=detected_sample_index,
        status="ok",
        rel_times=rel_times,
        deltas=deltas,
        detect_rel_time=detect_rel_time,
    )



def summarize_results(results: List[TrialResult]) -> None:
    ok_delays = [r.delay_ms for r in results if r.delay_ms is not None]

    print("\n===== Summary =====")
    for r in results:
        if r.delay_ms is None:
            print(
                f"Trial {r.trial_id}: status={r.status}, "
                f"baseline={r.baseline_mag:.2f}, peak_delta={r.peak_delta:.2f}"
            )
        else:
            print(
                f"Trial {r.trial_id}: delay={r.delay_ms:.2f} ms, "
                f"baseline={r.baseline_mag:.2f}, peak_delta={r.peak_delta:.2f}"
            )

    if ok_delays:
        avg = statistics.mean(ok_delays)
        print(f"Average delay: {avg:.2f} ms")
        if len(ok_delays) >= 2:
            print(f"Std dev: {statistics.stdev(ok_delays):.2f} ms")
            print(f"Min / Max: {min(ok_delays):.2f} / {max(ok_delays):.2f} ms")
    else:
        print("No successful detections, so no average delay was computed.")


# =========================
# Plotting
# =========================
def plot_results(results: List[TrialResult], save_path: str, show_fig: bool = True) -> None:
    ok_results = [r for r in results if r.delay_ms is not None]

    fig = plt.figure(figsize=(12, 8))

    ax1 = fig.add_subplot(2, 1, 1)
    xs = [r.trial_id for r in results]
    ys = [r.delay_ms if r.delay_ms is not None else float("nan") for r in results]
    ax1.plot(xs, ys, marker="o")
    ax1.set_title("Motor command -> accelerometer detection delay")
    ax1.set_xlabel("Trial")
    ax1.set_ylabel("Delay (ms)")
    ax1.grid(True)

    if ok_results:
        avg = statistics.mean(r.delay_ms for r in ok_results if r.delay_ms is not None)
        ax1.axhline(avg, linestyle="--", label=f"Average = {avg:.2f} ms")
        ax1.legend()

    ax2 = fig.add_subplot(2, 1, 2)
    for r in results:
        if r.rel_times and r.deltas:
            ax2.plot(r.rel_times, r.deltas, label=f"Trial {r.trial_id}")
            if r.detect_rel_time is not None:
                ax2.axvline(r.detect_rel_time, linestyle="--")
    ax2.axhline(THRESHOLD, linestyle=":", label=f"Threshold = {THRESHOLD}")
    ax2.set_title("Acceleration change after motor command")
    ax2.set_xlabel("Time since command (s)")
    ax2.set_ylabel("|magnitude - baseline|")
    ax2.grid(True)
    ax2.legend()

    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"Saved figure to: {Path(save_path).resolve()}")

    if show_fig:
        plt.show()
    else:
        plt.close(fig)


# =========================
# Main entry
# =========================
def main() -> None:
    port = auto_detect_port()
    ser = serial.Serial(port, baudrate=ACC_BAUD, timeout=0.02)
    print(f"Connected to ACC serial: {port}")

    time.sleep(2.0)
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    print(f"Sending: {ACC_STOP_CMD}")
    send_serial_command(ser, ACC_STOP_CMD, wait_s=0.2)
    print(f"Sending: {ACC_START_CMD}")
    send_serial_command(ser, ACC_START_CMD, wait_s=0.2)

    results: List[TrialResult] = []

    try:
        with VibratorController() as vc:
            print("Motor controller:", vc.echo())
            vc.stop_all()
            time.sleep(0.2)

            for trial_id in range(1, NUM_TRIALS + 1):
                result = run_single_trial(
                    ser=ser,
                    vc=vc,
                    trial_id=trial_id,
                    motor_idx=MOTOR_IDX,
                    amp=AMP,
                    vib_duration_s=VIB_DURATION_S,
                    baseline_duration_s=BASELINE_DURATION_S,
                    threshold=THRESHOLD,
                    consecutive_hits=CONSECUTIVE_HITS,
                    search_timeout_s=SEARCH_TIMEOUT_S,
                )
                results.append(result)

                if trial_id < NUM_TRIALS:
                    print(f"Resting for {INTER_TRIAL_REST_S:.2f}s before next trial...")
                    time.sleep(INTER_TRIAL_REST_S)

            vc.stop_all()

    finally:
        try:
            print(f"Sending: {ACC_STOP_CMD}")
            send_serial_command(ser, ACC_STOP_CMD, wait_s=0.1)
        except Exception:
            pass
        try:
            if ser.is_open:
                ser.close()
        except Exception:
            pass

    summarize_results(results)
    plot_results(results, SAVE_FIG_PATH, SHOW_FIG)


if __name__ == "__main__":
    main()
