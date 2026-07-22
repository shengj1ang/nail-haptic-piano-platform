import sys
from collections import deque

import serial
from serial.tools import list_ports
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


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

        if sys.platform == "darwin":
            if "cu." in name:
                score += 3
            if "usbmodem" in name or "usbserial" in name:
                score += 4
            if "bluetooth" in desc or "edifier" in name or "edifier" in desc:
                score -= 10
        elif sys.platform.startswith("linux"):
            if "ttyacm" in name or "ttyusb" in name:
                score += 4
        elif sys.platform.startswith("win"):
            if "com" in name:
                score += 2
            if "usb" in desc:
                score += 3

        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)

    best_score = scored[0][0]
    best_ports = [p for s, p in scored if s == best_score and s > 0]

    if len(best_ports) == 1:
        p = best_ports[0]
        print(f"Auto-selected: {p.device} ({p.description})")
        return p.device

    if len(best_ports) > 1:
        print("Multiple possible serial devices detected:")
        for i, p in enumerate(best_ports):
            print(f"[{i}] {p.device} | {p.description}")

        while True:
            idx = input("Select index: ").strip()
            if idx.isdigit():
                idx = int(idx)
                if 0 <= idx < len(best_ports):
                    return best_ports[idx].device

    print("Could not auto-select device. Available ports:")
    for i, p in enumerate(ports):
        print(f"[{i}] {p.device} | {p.description}")

    while True:
        idx = input("Select index: ").strip()
        if idx.isdigit():
            idx = int(idx)
            if 0 <= idx < len(ports):
                return ports[idx].device


def main():
    baud = 115200
    port = auto_detect_port()
    ser = serial.Serial(port, baudrate=baud, timeout=0.02)
    print(f"Connected to {port}")

    max_points = 300

    xs = deque(maxlen=max_points)
    ys = deque(maxlen=max_points)
    zs = deque(maxlen=max_points)
    ts = deque(maxlen=max_points)

    sample_index = 0

    fig, ax = plt.subplots(figsize=(10, 5))
    line_x, = ax.plot([], [], label="X")
    line_y, = ax.plot([], [], label="Y")
    line_z, = ax.plot([], [], label="Z")

    ax.set_title("LIS3DH Real-Time Acceleration")
    ax.set_xlabel("Sample")
    ax.set_ylabel("Acceleration (raw)")
    ax.legend()
    ax.grid(True)

    def update(_frame):
        nonlocal sample_index

        for _ in range(50):
            try:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
            except Exception:
                break

            if not line:
                continue

            parts = line.split(",")
            if len(parts) != 3:
                continue

            try:
                x = int(parts[0])
                y = int(parts[1])
                z = int(parts[2])
            except ValueError:
                continue

            ts.append(sample_index)
            xs.append(x)
            ys.append(y)
            zs.append(z)
            sample_index += 1

        if not ts:
            return line_x, line_y, line_z

        line_x.set_data(ts, xs)
        line_y.set_data(ts, ys)
        line_z.set_data(ts, zs)

        ax.set_xlim(ts[0], ts[-1] if ts[-1] > ts[0] else ts[0] + 1)

        data_min = min(min(xs), min(ys), min(zs))
        data_max = max(max(xs), max(ys), max(zs))
        if data_min == data_max:
            data_min -= 1
            data_max += 1
        padding = max(20, int((data_max - data_min) * 0.1))
        ax.set_ylim(data_min - padding, data_max + padding)

        return line_x, line_y, line_z

    ani = FuncAnimation(fig, update, interval=50, blit=False, cache_frame_data=False)

    try:
        plt.show()
    finally:
        ser.close()
        print("Serial port closed.")


if __name__ == "__main__":
    main()
