import time
import queue
import numpy as np
import sounddevice as sd
from scipy.signal import butter, sosfilt
from controller import VibratorController


# =========================
# User-tunable parameters
# =========================

SAMPLE_RATE = 48000
CHANNELS = 1
DTYPE = "float32"
BLOCKSIZE = 1024

AMP = 100
MOTOR_INDEX = 0

# Detection band
BANDPASS_LOW_HZ = 80.0
BANDPASS_HIGH_HZ = 5000.0

# Envelope smoothing
ENVELOPE_SMOOTH_MS = 8.0

# Threshold rule:
# detected if envelope > baseline_mean + THRESHOLD_SIGMA * baseline_std
THRESHOLD_SIGMA = 6.0

# Baseline warmup time before starting motor
BASELINE_SECONDS = 2.0

# Detection state holding
DETECT_HOLD_SECONDS = 0.20

# Console refresh
PRINT_INTERVAL_SECONDS = 0.10

# Optional: set to a device index if default microphone is wrong
AUDIO_DEVICE = None


def make_bandpass_sos(fs, low_hz, high_hz, order=4):
    nyq = fs * 0.5
    low = max(1.0, low_hz) / nyq
    high = min(high_hz, nyq * 0.95) / nyq
    return butter(order, [low, high], btype="bandpass", output="sos")


class LiveDetector:
    def __init__(self, fs):
        self.fs = fs
        self.sos = make_bandpass_sos(fs, BANDPASS_LOW_HZ, BANDPASS_HIGH_HZ)
        self.smooth_len = max(1, int(fs * ENVELOPE_SMOOTH_MS / 1000.0))

        self.baseline_values = []
        self.baseline_mean = 0.0
        self.baseline_std = 1e-9
        self.threshold = 0.0

        self.last_detect_time = 0.0

    def _smooth_envelope(self, x):
        x = np.abs(x)
        if self.smooth_len <= 1:
            return x
        kernel = np.ones(self.smooth_len, dtype=np.float32) / self.smooth_len
        return np.convolve(x, kernel, mode="same")

    def process_block(self, block):
        filtered = sosfilt(self.sos, block)
        env = self._smooth_envelope(filtered)
        level = float(np.max(env)) if len(env) else 0.0
        return level

    def update_baseline(self, level):
        self.baseline_values.append(level)

    def finalize_baseline(self):
        arr = np.array(self.baseline_values, dtype=np.float32)
        if len(arr) == 0:
            self.baseline_mean = 0.0
            self.baseline_std = 1e-9
        else:
            self.baseline_mean = float(arr.mean())
            self.baseline_std = float(arr.std()) + 1e-9

        self.threshold = self.baseline_mean + THRESHOLD_SIGMA * self.baseline_std

    def update_detection(self, level):
        now = time.perf_counter()

        if level > self.threshold:
            self.last_detect_time = now

        detected = (now - self.last_detect_time) <= DETECT_HOLD_SECONDS
        return detected


def main():
    q_audio = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print("Audio status:", status)
        q_audio.put(indata[:, 0].copy())

    detector = LiveDetector(SAMPLE_RATE)

    with VibratorController() as vc:
        print("Device:", vc.echo())
        vc.stop_all()
        time.sleep(0.2)

        print(f"Collecting baseline for {BASELINE_SECONDS:.1f} seconds...")
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCKSIZE,
            callback=callback,
            device=AUDIO_DEVICE,
        ):
            t0 = time.perf_counter()

            while time.perf_counter() - t0 < BASELINE_SECONDS:
                try:
                    block = q_audio.get(timeout=1.0)
                except queue.Empty:
                    continue
                level = detector.process_block(block)
                detector.update_baseline(level)

            detector.finalize_baseline()

            print("Baseline ready.")
            print(f"baseline_mean = {detector.baseline_mean:.6f}")
            print(f"baseline_std  = {detector.baseline_std:.6f}")
            print(f"threshold     = {detector.threshold:.6f}")
            print()
            print("Starting motor 0 continuous vibration...")
            print("Move the motor close to / away from the microphone.")
            print("Press Ctrl+C to stop.")
            print()

            # Continuous vibration on motor 0 using immediate mode
            vc.send(f"S {1 << MOTOR_INDEX} {AMP}")

            last_print = 0.0

            try:
                while True:
                    try:
                        block = q_audio.get(timeout=1.0)
                    except queue.Empty:
                        continue

                    level = detector.process_block(block)
                    detected = detector.update_detection(level)

                    now = time.perf_counter()
                    if now - last_print >= PRINT_INTERVAL_SECONDS:
                        state = "DETECTED" if detected else "quiet"
                        ratio = level / detector.threshold if detector.threshold > 0 else 0.0

                        print(
                            f"level={level:.6f} | "
                            f"threshold={detector.threshold:.6f} | "
                            f"ratio={ratio:.2f} | "
                            f"{state}"
                        )
                        last_print = now

            except KeyboardInterrupt:
                print("\nStopping...")

            finally:
                vc.stop_all()


if __name__ == "__main__":
    main()