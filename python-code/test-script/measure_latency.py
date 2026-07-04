# pip install pyserial sounddevice numpy scipy matplotlib

import time
import queue
import numpy as np
import sounddevice as sd
import matplotlib.pyplot as plt
from scipy.signal import butter, sosfiltfilt
from controller import VibratorController


# =========================
# User settings
# =========================

NUM_RUNS = 20

# Audio settings
SAMPLE_RATE = 48000
CHANNELS = 1
DTYPE = "float32"

# Recording window
PRE_ROLL_S = 0.20
POST_ROLL_S = 1.20
TOTAL_S = PRE_ROLL_S + POST_ROLL_S

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

# Plot output
PLOT_FILENAME = "latency_plot.png"


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


def record_and_trigger(vc, device=None):
    """
    Record audio, trigger motor 0 once, and return:
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

        # Record a short baseline before sending the command
        time.sleep(PRE_ROLL_S)

        # Trigger motor 0 once
        cmd_t = time.perf_counter()
        vc.send(f"P 0 1 {AMP} {ON_MS} 0")

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


def plot_results(latencies_ms):
    run_ids = np.arange(1, len(latencies_ms) + 1)
    mean_latency = float(np.mean(latencies_ms))

    plt.figure(figsize=(10, 5))
    plt.plot(run_ids, latencies_ms, marker="o")
    plt.axhline(mean_latency, linestyle="--", label=f"Mean = {mean_latency:.2f} ms")

    plt.xlabel("Run")
    plt.ylabel("Latency (ms)")
    plt.title("Measured Motor Latency Across Runs")
    plt.xticks(run_ids)
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()

    plt.savefig(PLOT_FILENAME, dpi=150)
    plt.show()


def main():
    results = []

    with VibratorController() as vc:
        print("Device:", vc.echo())

        for i in range(NUM_RUNS):
            print(f"\n--- Run {i + 1}/{NUM_RUNS} ---")

            vc.stop_all()
            time.sleep(0.1)

            audio, cmd_offset_s = record_and_trigger(vc, device=AUDIO_DEVICE)
            onset_s, info = detect_onset(audio, SAMPLE_RATE, cmd_offset_s)

            if onset_s is None:
                print("No onset detected, skipping")
                continue

            latency_s = onset_s - cmd_offset_s
            latency_ms = latency_s * 1000.0

            print(f"Latency: {latency_ms:.2f} ms")
            results.append(latency_ms)

        vc.stop_all()

    if not results:
        print("\nNo valid measurements.")
        return

    arr = np.array(results, dtype=np.float64)

    print("\n========== RESULT ==========")
    print(f"Runs: {len(arr)}")
    print(f"Mean: {arr.mean():.2f} ms")
    print(f"Std:  {arr.std():.2f} ms")
    print(f"Min:  {arr.min():.2f} ms")
    print(f"Max:  {arr.max():.2f} ms")

    plot_results(arr)
    print(f"\nPlot saved to: {PLOT_FILENAME}")


if __name__ == "__main__":
    main()