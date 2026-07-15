import math
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import serial
from serial.tools import list_ports


EXPERIMENT_NAME = "attachment_verification_experiment"
DB_PATH = "attachment_verification_experiment.db"

ACC_BAUD = 115200
ACC_START_CMD = "A START 2"
ACC_STOP_CMD = "A STOP"

ACTUATORS = {
    "LRA": 11,
    "ERM": 10,
}

ATTACHMENT_METHODS = [
    "tape",
    "putty",
    "eyelash_glue",
]

TRIALS_PER_CONDITION = 5
AMP = 80
MEASUREMENT_VIBRATION_S = 0.30
INTER_TRIAL_MIN_S = 1.5
INTER_TRIAL_MAX_S = 3.0

BASELINE_DURATION_S = 1.0
SEARCH_TIMEOUT_S = 3.0
THRESHOLD = 150
CONSECUTIVE_HITS = 1

RELIABILITY_CYCLES = 10
RELIABILITY_VIBRATION_S = 1.0
RELIABILITY_REST_S = 0.5


@dataclass
class TrialResult:
    peak_delta: float
    rms_delta: float
    onset_delay_ms: Optional[float]
    baseline_mag: float
    sample_count: int


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
        if "ttyacm" in name or "ttyusb" in name or "usbmodem" in name or "usbserial" in name:
            score += 4

        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]
    best_ports = [p for s, p in scored if s == best_score and s > 0]

    if len(best_ports) == 1:
        p = best_ports[0]
        print(f"Auto-selected device port: {p.device} ({p.description})")
        return p.device

    print("Available serial ports:")
    for i, p in enumerate(ports):
        print(f"[{i}] {p.device} | {p.description}")

    while True:
        idx = input("Select device serial index: ").strip()
        if idx.isdigit() and 0 <= int(idx) < len(ports):
            return ports[int(idx)].device
        print("Invalid selection.")


def send_serial_command(ser: serial.Serial, cmd: str, wait_s: float = 0.12) -> None:
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
        return int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


def magnitude(x: int, y: int, z: int) -> float:
    return math.sqrt(x * x + y * y + z * z)


def motor_on(ser: serial.Serial, motor_idx: int, amp: int) -> None:
    mask = 1 << motor_idx
    send_serial_command(ser, f"S {mask} {amp}", wait_s=0.01)


def motor_off(ser: serial.Serial, amp: int) -> None:
    send_serial_command(ser, f"S 0 {amp}", wait_s=0.01)


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


def run_single_trial(ser: serial.Serial, motor_idx: int) -> TrialResult:
    print("Collecting baseline...")
    baseline_mag = collect_baseline(ser, BASELINE_DURATION_S)
    print(f"Baseline magnitude: {baseline_mag:.2f}")

    ser.reset_input_buffer()

    deltas: List[float] = []
    hit_count = 0
    peak_delta = 0.0
    detected_time = None

    t_cmd = time.perf_counter()
    motor_on(ser, motor_idx, AMP)
    t_off = t_cmd + MEASUREMENT_VIBRATION_S

    start_search = time.perf_counter()

    while time.perf_counter() - start_search < SEARCH_TIMEOUT_S:
        now = time.perf_counter()
        if now >= t_off:
            motor_off(ser, AMP)
            t_off = float("inf")

        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue

        parsed = parse_acc_line(raw)
        if parsed is None:
            continue

        mag = magnitude(*parsed)
        delta = abs(mag - baseline_mag)
        deltas.append(delta)
        peak_delta = max(peak_delta, delta)

        if delta > THRESHOLD:
            hit_count += 1
            if hit_count >= CONSECUTIVE_HITS:
                detected_time = time.perf_counter()
                break
        else:
            hit_count = 0

    motor_off(ser, AMP)

    if not deltas:
        raise RuntimeError("No accelerometer samples received during trial")

    rms_delta = math.sqrt(sum(v * v for v in deltas) / len(deltas))
    onset_delay_ms = None if detected_time is None else (detected_time - t_cmd) * 1000.0

    return TrialResult(
        peak_delta=peak_delta,
        rms_delta=rms_delta,
        onset_delay_ms=onset_delay_ms,
        baseline_mag=baseline_mag,
        sample_count=len(deltas),
    )


def run_reliability_test(ser: serial.Serial, motor_idx: int) -> int:
    print(f"\nReliability test: {RELIABILITY_CYCLES} cycles")
    print(f"Each cycle: {RELIABILITY_VIBRATION_S:.1f}s vibration + {RELIABILITY_REST_S:.1f}s rest")

    for i in range(1, RELIABILITY_CYCLES + 1):
        print(f"Cycle {i}/{RELIABILITY_CYCLES}")
        motor_on(ser, motor_idx, AMP)
        time.sleep(RELIABILITY_VIBRATION_S)
        motor_off(ser, AMP)
        time.sleep(RELIABILITY_REST_S)

    return prompt_int(
        "Failure cycle (0 if it never fell off): ",
        minimum=0,
        maximum=RELIABILITY_CYCLES,
    )


def create_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trials (
            participant_id TEXT NOT NULL,
            actuator_type TEXT NOT NULL,
            attachment_method TEXT NOT NULL,
            trial_index INTEGER NOT NULL,
            peak_delta REAL NOT NULL,
            rms_delta REAL NOT NULL,
            onset_delay_ms REAL,
            baseline_mag REAL NOT NULL,
            sample_count INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conditions (
            participant_id TEXT NOT NULL,
            actuator_type TEXT NOT NULL,
            attachment_method TEXT NOT NULL,
            reliability_cycles INTEGER NOT NULL,
            failure_cycle INTEGER NOT NULL,
            comfort_rating INTEGER NOT NULL,
            PRIMARY KEY (participant_id, actuator_type, attachment_method)
        )
        """
    )

    conn.commit()


def prompt_nonempty(message: str) -> str:
    while True:
        value = input(message).strip()
        if value:
            return value
        print("Input cannot be empty.")


def prompt_int(message: str, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    while True:
        raw = input(message).strip()
        try:
            value = int(raw)
        except ValueError:
            print("Please enter an integer.")
            continue

        if minimum is not None and value < minimum:
            print(f"Value must be >= {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"Value must be <= {maximum}.")
            continue
        return value


def main() -> None:
    participant_id = prompt_nonempty("Participant ID: ")
    port = auto_detect_port()

    ser = serial.Serial(port, ACC_BAUD, timeout=1)
    time.sleep(2)

    print(f"Connected to: {port}")
    send_serial_command(ser, ACC_START_CMD, wait_s=0.3)
    ser.reset_input_buffer()

    conn = sqlite3.connect(DB_PATH)
    create_db(conn)
    cur = conn.cursor()

    conditions = [(a, m) for a in ACTUATORS for m in ATTACHMENT_METHODS]
    random.shuffle(conditions)

    try:
        for condition_index, (actuator_type, attachment_method) in enumerate(conditions, start=1):
            print("\n" + "=" * 72)
            print(f"Condition {condition_index}: {actuator_type} + {attachment_method}")
            print("Mount the actuator and place the accelerometer next to it.")
            input("Press Enter to start...")

            motor_idx = ACTUATORS[actuator_type]
            trial_results: List[TrialResult] = []

            for trial_idx in range(1, TRIALS_PER_CONDITION + 1):
                print(f"\nTrial {trial_idx}/{TRIALS_PER_CONDITION}")
                iti = random.uniform(INTER_TRIAL_MIN_S, INTER_TRIAL_MAX_S)
                print(f"Waiting {iti:.2f}s before trigger...")
                time.sleep(iti)

                result = run_single_trial(ser, motor_idx)
                trial_results.append(result)

                print(
                    f"Peak delta={result.peak_delta:.2f} | "
                    f"RMS delta={result.rms_delta:.2f} | "
                    f"Onset delay={'not detected' if result.onset_delay_ms is None else f'{result.onset_delay_ms:.2f} ms'}"
                )

                cur.execute(
                    """
                    INSERT INTO trials (
                        participant_id,
                        actuator_type,
                        attachment_method,
                        trial_index,
                        peak_delta,
                        rms_delta,
                        onset_delay_ms,
                        baseline_mag,
                        sample_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        participant_id,
                        actuator_type,
                        attachment_method,
                        trial_idx,
                        result.peak_delta,
                        result.rms_delta,
                        result.onset_delay_ms,
                        result.baseline_mag,
                        result.sample_count,
                    ),
                )
                conn.commit()

            failure_cycle = run_reliability_test(ser, motor_idx)
            comfort_rating = prompt_int("Comfort rating (1-5): ", minimum=1, maximum=5)

            cur.execute(
                """
                INSERT OR REPLACE INTO conditions (
                    participant_id,
                    actuator_type,
                    attachment_method,
                    reliability_cycles,
                    failure_cycle,
                    comfort_rating
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    participant_id,
                    actuator_type,
                    attachment_method,
                    RELIABILITY_CYCLES,
                    failure_cycle,
                    comfort_rating,
                ),
            )
            conn.commit()

            print("\nCondition summary")
            print(f"Mean peak delta: {statistics.mean([r.peak_delta for r in trial_results]):.2f}")
            print(f"Mean RMS delta: {statistics.mean([r.rms_delta for r in trial_results]):.2f}")
            valid_onsets = [r.onset_delay_ms for r in trial_results if r.onset_delay_ms is not None]
            if valid_onsets:
                print(f"Mean onset delay: {statistics.mean(valid_onsets):.2f} ms")
            else:
                print("Mean onset delay: not detected")
            print(f"Failure cycle: {failure_cycle}")

    finally:
        try:
            motor_off(ser, AMP)
        except Exception:
            pass
        try:
            send_serial_command(ser, ACC_STOP_CMD, wait_s=0.1)
        except Exception:
            pass
        ser.close()
        conn.close()


if __name__ == "__main__":
    main()