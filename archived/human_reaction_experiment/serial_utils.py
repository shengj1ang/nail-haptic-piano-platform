import serial
from serial.tools import list_ports
import sys


def auto_detect_port() -> str:
    """
    Cross-platform auto detection of serial port.

    Priority:
    1. USB / Arduino / Teensy devices
    2. Platform-specific common patterns
    3. Fallback to manual selection
    """

    ports = list(list_ports.comports())

    if not ports:
        raise RuntimeError("No serial ports found")

    scored = []

    for p in ports:
        score = 0

        name = (p.device or "").lower()
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()

        # ---------- Common USB indicators ----------
        if "usb" in name or "usb" in desc:
            score += 3

        if "serial" in desc:
            score += 1

        if "arduino" in desc:
            score += 5

        if "teensy" in desc:
            score += 5

        if "ch340" in desc or "cp210" in desc or "ftdi" in desc:
            score += 4

        # ---------- macOS ----------
        if sys.platform == "darwin":
            if "cu." in name:
                score += 3
            if "usbmodem" in name or "usbserial" in name:
                score += 3

        # ---------- Linux ----------
        elif sys.platform.startswith("linux"):
            if "ttyacm" in name or "ttyusb" in name:
                score += 3

        # ---------- Windows ----------
        elif sys.platform.startswith("win"):
            if "com" in name:
                score += 2
            if "usb" in desc:
                score += 3

        scored.append((score, p))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    best_score = scored[0][0]
    best_ports = [p for s, p in scored if s == best_score and s > 0]

    # Case 1: exactly one best match → auto select
    if len(best_ports) == 1:
        p = best_ports[0]
        print(f"Auto-selected: {p.device} ({p.description})")
        return p.device

    # Case 2: multiple good matches → ask user
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

    # Case 3: no confident match → fallback
    print("Could not auto-select device. Available ports:")
    for i, p in enumerate(ports):
        print(f"[{i}] {p.device} | {p.description}")

    while True:
        idx = input("Select index: ").strip()
        if idx.isdigit():
            idx = int(idx)
            if 0 <= idx < len(ports):
                return ports[idx].device

def open_serial(port, baud=115200, timeout=0.3):
    """
    Open serial port and wait for MCU reset.
    """
    ser = serial.Serial(port, baud, timeout=timeout)

    import time
    time.sleep(1.5)  # wait for board reset

    ser.reset_input_buffer()
    return ser