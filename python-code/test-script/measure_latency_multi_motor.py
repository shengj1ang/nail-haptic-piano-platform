# pip install pyserial sounddevice numpy scipy matplotlib

import os
import time
import queue
import random
import numpy as np
import sounddevice as sd
import matplotlib.pyplot as plt
from scipy.signal import butter, sosfiltfilt
from controller import VibratorController


# =========================
# User settings
# =========================

NUM_RUNS = 20

# Choose which motors are allowed to be tested
TEST_MOTORS = [0, 2, 6]

# Audio settings
SAMPLE_RATE = 48000
CHANNELS = 1
DTYPE = "float32"

# Recording window
PRE_ROLL_S = 0.20
POST_ROLL_S = 1.20

# Motor command settings
AMP = 120
ON_MS = 150

# Detection settings
HP_CUTOFF_HZ = 80.0
LP_CUTOFF_HZ = 5000.0
THRESHOLD_SIGMA = 6.0
MIN_EVENT_DELAY_S = 0.005
SEARCH_AFTER_CMD_S = 0.80

# Optional audio device index
AUDIO_DEVICE = None

# Output folder
OUTPUT_DIR = "latency_results"


def make_bandpass_sos(fs, low_hz, high_hz, order=4):
    nyq = fs * 0.5
    low = max(1.0, low_hz) / nyq
    high = min(high_hz, nyq * 0.95) / nyq
    return butter(order, [low, high], btype="bandpass", output="sos")


def smooth_envelope(x, win_size=256):
    x = np.abs(x)
    if win_size <= 1:
        return x
    kernel = np.ones(win_size, dtype=np.float32) / win_size
    return np.convolve(x, kernel, mode="same")


def detect_onset(audio, fs, cmd_time_offset_s):
    """
    Detect the first strong acoustic onset after the command time.
    Returns onset_time_s relative to recording start, or None if not found.
    """
    sos = make_bandpass_sos(fs, HP_CUTOFF_HZ, LP_CUTOFF_HZ)
    filtered = sosfiltfilt(sos, audio)

    env = smooth_envelope(filtered, win_size=int(fs * 0.004))  # ~4 ms smoothing

    baseline_end = max(1, int((cmd_time_offset_s - 0.02) * fs))
    baseline = env[:baseline_end] if baseline_end > 10 else env[: max(10, len(env) // 10)]

    mu = float(np.mean(baseline))
    sigma = float(np.std(baseline)) + 1e-9
    threshold = mu + THRESHOLD_SIGMA * sigma

    start_idx = int((cmd_time_offset_s + MIN_EVENT_DELAY_S) * fs)
    end_idx = min(len(env), int((cmd_time_offset_s + SEARCH_AFTER_CMD_S) * fs))

    if start_idx >= end_idx:
        return None, {
            "threshold": threshold,
            "mu": mu,
            "sigma": sigma,
            "peak": float(np.max(env)) if len(env) else 0.0,
        }

    search = env[start_idx:end_idx]
    hits = np.where(search > threshold)[0]

    info = {
        "threshold": threshold,
        "mu": mu,
        "sigma": sigma,
        "peak": float(np.max(search)) if len(search) else 0.0,
    }

    if len(hits) == 0:
        return None, info

    onset_idx = start_idx + int(hits[0])
    onset_time_s = onset_idx / fs
    return onset_time_s, info


def record_and_trigger(vc, motor_idx, device=None):
    """
    Record audio, trigger one selected motor once, and return:
    audio, command_time_relative_to_record_start
    """
    q_audio = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print("Audio status:", status)
        q_audio.put(indata.copy())

    blocksize = 1024
    audio_blocks = []

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=blocksize,
        callback=callback,
        device=device,
    ):
        record_t0 = time.perf_counter()

        # Record baseline before sending the command
        time.sleep(PRE_ROLL_S)

        # Trigger the selected motor once
        cmd_t = time.perf_counter()
        vc.send(f"P {motor_idx} 1 {AMP} {ON_MS} 0")

        # Keep recording after the trigger
        time.sleep(POST_ROLL_S)

        record_t1 = time.perf_counter()

    while not q_audio.empty():
        audio_blocks.append(q_audio.get())

    if not audio_blocks:
        raise RuntimeError("No audio data captured")

    audio = np.concatenate(audio_blocks, axis=0).reshape(-1)
    expected_len = int((record_t1 - record_t0) * SAMPLE_RATE)

    if len(audio) > expected_len > 0:
        audio = audio[:expected_len]

    cmd_offset_s = cmd_t - record_t0
    return audio, cmd_offset_s


def build_motor_color_map(motors):
    """
    Assign one distinct color to each motor.
    """
    cmap = plt.get_cmap("tab10")
    unique_motors = sorted(set(motors))
    return {m: cmap(i % 10) for i, m in enumerate(unique_motors)}


def plot_run_sequence(results, output_dir):
    """
    Plot latency by run index, with different point colors for different motors,
    and a global mean line.
    """
    run_ids = np.arange(1, len(results) + 1)
    latencies = np.array([r["latency_ms"] for r in results], dtype=float)
    motors = [r["motor"] for r in results]
    mean_latency = float(np.mean(latencies))

    color_map = build_motor_color_map(motors)

    plt.figure(figsize=(11, 5))
    plt.plot(run_ids, latencies, linewidth=1.2, alpha=0.7)

    for x, y, m in zip(run_ids, latencies, motors):
        plt.scatter(x, y, s=60, color=color_map[m], label=f"Motor {m}")

    # Deduplicate legend entries
    handles, labels = plt.gca().get_legend_handles_labels()
    seen = set()
    uniq_handles = []
    uniq_labels = []
    for h, l in zip(handles, labels):
        if l not in seen:
            seen.add(l)
            uniq_handles.append(h)
            uniq_labels.append(l)

    plt.axhline(mean_latency, linestyle="--", linewidth=1.5, label=f"Global Mean = {mean_latency:.2f} ms")

    plt.xlabel("Run")
    plt.ylabel("Latency (ms)")
    plt.title("Latency by Run")
    plt.xticks(run_ids)
    plt.grid(True, alpha=0.3)
    plt.legend(uniq_handles + [plt.Line2D([], [], linestyle="--", color="gray")],
               uniq_labels + [f"Global Mean = {mean_latency:.2f} ms"])
    plt.tight_layout()

    path = os.path.join(output_dir, "latency_by_run.png")
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def plot_grouped_by_motor(results, output_dir):
    """
    Plot latency grouped by motor, with one color per motor
    and one mean line per motor.
    """
    motors = sorted(set(r["motor"] for r in results))
    color_map = build_motor_color_map(motors)

    plt.figure(figsize=(10, 5))

    for x_idx, motor in enumerate(motors, start=1):
        motor_vals = [r["latency_ms"] for r in results if r["motor"] == motor]
        if not motor_vals:
            continue

        xs = np.full(len(motor_vals), x_idx, dtype=float)

        # Small horizontal jitter
        jitter = np.linspace(-0.08, 0.08, len(motor_vals)) if len(motor_vals) > 1 else np.array([0.0])
        xs = xs + jitter

        plt.scatter(xs, motor_vals, s=70, color=color_map[motor], alpha=0.9, label=f"Motor {motor}")

        mean_val = float(np.mean(motor_vals))
        plt.hlines(mean_val, x_idx - 0.2, x_idx + 0.2, linestyles="--", linewidth=2, color=color_map[motor])

    handles, labels = plt.gca().get_legend_handles_labels()
    seen = set()
    uniq_handles = []
    uniq_labels = []
    for h, l in zip(handles, labels):
        if l not in seen:
            seen.add(l)
            uniq_handles.append(h)
            uniq_labels.append(l)

    plt.xticks(range(1, len(motors) + 1), [f"Motor {m}" for m in motors])
    plt.ylabel("Latency (ms)")
    plt.title("Latency Grouped by Motor")
    plt.grid(True, axis="y", alpha=0.3)
    plt.legend(uniq_handles, uniq_labels)
    plt.tight_layout()

    path = os.path.join(output_dir, "latency_grouped_by_motor.png")
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def plot_histogram(results, output_dir):
    """
    Plot a histogram of all latency values.
    """
    latencies = np.array([r["latency_ms"] for r in results], dtype=float)

    plt.figure(figsize=(9, 5))
    plt.hist(latencies, bins=min(10, max(5, len(latencies) // 2)), alpha=0.8)
    plt.axvline(np.mean(latencies), linestyle="--", linewidth=1.5, label=f"Mean = {np.mean(latencies):.2f} ms")

    plt.xlabel("Latency (ms)")
    plt.ylabel("Count")
    plt.title("Latency Distribution")
    plt.grid(True, axis="y", alpha=0.3)
    plt.legend()
    plt.tight_layout()

    path = os.path.join(output_dir, "latency_histogram.png")
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def save_summary(results, output_dir):
    """
    Save summary statistics to a text file.
    """
    latencies = np.array([r["latency_ms"] for r in results], dtype=float)

    summary_path = os.path.join(output_dir, "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("Latency Measurement Summary\n")
        f.write("===========================\n\n")
        f.write(f"Runs: {len(latencies)}\n")
        f.write(f"Motors tested: {sorted(set(r['motor'] for r in results))}\n")
        f.write(f"Mean: {latencies.mean():.2f} ms\n")
        f.write(f"Std:  {latencies.std():.2f} ms\n")
        f.write(f"Min:  {latencies.min():.2f} ms\n")
        f.write(f"Max:  {latencies.max():.2f} ms\n\n")

        for motor in sorted(set(r["motor"] for r in results)):
            vals = np.array([r["latency_ms"] for r in results if r["motor"] == motor], dtype=float)
            if len(vals) == 0:
                continue
            f.write(f"Motor {motor}\n")
            f.write(f"  Count: {len(vals)}\n")
            f.write(f"  Mean:  {vals.mean():.2f} ms\n")
            f.write(f"  Std:   {vals.std():.2f} ms\n")
            f.write(f"  Min:   {vals.min():.2f} ms\n")
            f.write(f"  Max:   {vals.max():.2f} ms\n\n")

    return summary_path


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not TEST_MOTORS:
        raise ValueError("TEST_MOTORS cannot be empty")

    results = []

    with VibratorController() as vc:
        print("Device:", vc.echo())
        print(f"Testing motors: {TEST_MOTORS}")

        for i in range(NUM_RUNS):
            motor_idx = random.choice(TEST_MOTORS)

            print(f"\n--- Run {i + 1}/{NUM_RUNS} | Motor {motor_idx} ---")

            vc.stop_all()
            time.sleep(0.1)

            audio, cmd_offset_s = record_and_trigger(vc, motor_idx, device=AUDIO_DEVICE)
            onset_s, info = detect_onset(audio, SAMPLE_RATE, cmd_offset_s)

            if onset_s is None:
                print("No onset detected, skipping")
                continue

            latency_s = onset_s - cmd_offset_s
            latency_ms = latency_s * 1000.0

            print(f"Latency: {latency_ms:.2f} ms")

            results.append({
                "run": i + 1,
                "motor": motor_idx,
                "latency_ms": latency_ms,
            })

        vc.stop_all()

    if not results:
        print("\nNo valid measurements.")
        return

    latencies = np.array([r["latency_ms"] for r in results], dtype=float)

    print("\n========== RESULT ==========")
    print(f"Runs: {len(latencies)}")
    print(f"Mean: {latencies.mean():.2f} ms")
    print(f"Std:  {latencies.std():.2f} ms")
    print(f"Min:  {latencies.min():.2f} ms")
    print(f"Max:  {latencies.max():.2f} ms")

    for motor in sorted(set(r["motor"] for r in results)):
        vals = np.array([r["latency_ms"] for r in results if r["motor"] == motor], dtype=float)
        print(f"\nMotor {motor}:")
        print(f"  Count: {len(vals)}")
        print(f"  Mean:  {vals.mean():.2f} ms")
        print(f"  Std:   {vals.std():.2f} ms")
        print(f"  Min:   {vals.min():.2f} ms")
        print(f"  Max:   {vals.max():.2f} ms")

    path1 = plot_run_sequence(results, OUTPUT_DIR)
    path2 = plot_grouped_by_motor(results, OUTPUT_DIR)
    path3 = plot_histogram(results, OUTPUT_DIR)
    summary_path = save_summary(results, OUTPUT_DIR)

    print("\nSaved files:")
    print(path1)
    print(path2)
    print(path3)
    print(summary_path)


if __name__ == "__main__":
    main()