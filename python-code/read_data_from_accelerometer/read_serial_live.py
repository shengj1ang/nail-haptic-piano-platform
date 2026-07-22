import sys
import serial
from serial.tools import list_ports


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
        manufacturer = (getattr(p, "manufacturer", "") or "").lower()

        # Common USB indicators
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

        # macOS
        if sys.platform == "darwin":
            if "cu." in name:
                score += 3
            if "usbmodem" in name or "usbserial" in name:
                score += 4
            if "bluetooth" in desc or "edifier" in name or "edifier" in desc:
                score -= 10

        # Linux
        elif sys.platform.startswith("linux"):
            if "ttyacm" in name or "ttyusb" in name:
                score += 4

        # Windows
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

    print(f"Connecting to {port} at {baud} baud...")

    try:
        ser = serial.Serial(port, baudrate=baud, timeout=1)
        print("Connected. Reading serial output...\n")

        while True:
            try:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    print(line)
            except KeyboardInterrupt:
                print("\nStopped by user.")
                break

    except serial.SerialException as e:
        print(f"Serial error: {e}")

    finally:
        try:
            ser.close()
            print("Serial port closed.")
        except:
            pass


if __name__ == "__main__":
    main()