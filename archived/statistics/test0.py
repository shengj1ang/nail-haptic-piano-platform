import sys
import time
import serial
from serial.tools import list_ports

BAUD = 115200
READ_TIMEOUT = 0.3

# Tunable parameters
AMP = 80
ON_MS = 120
OFF_MS = 200                 # each motor test gap
STARTUP_WAIT_S = 1.5
INIT_RETRY_GAP_S = 0.05

# Delay between different motors
WAIT_BETWEEN_MOTORS_S = 1.5

# 10 PWM outputs: 0..9
NUM_MOTORS = 10

# PWM frequency settings (Hz) for 10 channels
FREQS = [300] * NUM_MOTORS


def auto_detect_port() -> str:
    """
    Auto-detect a likely serial port.
    If exactly one suitable port is found, use it directly.
    If multiple are found, ask user to choose.
    """
    ports = list(list_ports.comports())

    if not ports:
        raise RuntimeError("No serial ports found.")

    preferred = []
    others = []

    for p in ports:
        desc = (p.description or "").lower()
        device = (p.device or "").lower()
        hwid = (p.hwid or "").lower()

        score = 0
        keywords = [
            "usb", "acm", "cdc", "serial", "uart", "cp210", "ch340", "ch341",
            "ftdi", "ttyusb", "ttyacm", "wch", "silicon labs", "arduino", "teensy"
        ]
        for kw in keywords:
            if kw in desc or kw in device or kw in hwid:
                score += 1

        if score > 0:
            preferred.append(p)
        else:
            others.append(p)

    candidates = preferred if preferred else ports

    if len(candidates) == 1:
        p = candidates[0]
        print(f"Auto detected serial port: {p.device} ({p.description})")
        return p.device

    print("Multiple serial ports found:")
    for i, p in enumerate(candidates):
        print(f"[{i}] {p.device}  |  {p.description}  |  {p.hwid}")

    while True:
        s = input("Select port index: ").strip()
        if s.isdigit():
            idx = int(s)
            if 0 <= idx < len(candidates):
                return candidates[idx].device
        print("Invalid selection, try again.")


def open_serial(port: str) -> serial.Serial:
    """
    Open serial port and wait for MCU reset.
    """
    ser = serial.Serial(port, BAUD, timeout=READ_TIMEOUT)
    time.sleep(STARTUP_WAIT_S)
    ser.reset_input_buffer()
    return ser


def send_line(ser: serial.Serial, line: str) -> None:
    ser.write((line.strip() + "\n").encode("utf-8"))
    ser.flush()


def motor_mask(motor_index_0based: int) -> int:
    """
    Convert motor index (0..9) into a bitmask:
      0 -> 1
      1 -> 2
      2 -> 4
      ...
      9 -> 512
    """
    return 1 << motor_index_0based


def vibrate_once(ser: serial.Serial, mask: int, amp: int, on_ms: int) -> None:
    send_line(ser, f"S {mask} {amp}")
    time.sleep(on_ms / 1000.0)
    send_line(ser, "X")


def set_all_freqs(ser: serial.Serial, freqs) -> None:
    """
    Send 10 frequency values:
      F f0 f1 f2 f3 f4 f5 f6 f7 f8 f9
    """
    if len(freqs) != NUM_MOTORS:
        raise ValueError(f"freqs must contain exactly {NUM_MOTORS} values")

    cmd = "F " + " ".join(str(int(f)) for f in freqs)
    send_line(ser, cmd)
    time.sleep(INIT_RETRY_GAP_S)
    send_line(ser, cmd)


def test_motor_one_by_one(ser: serial.Serial) -> None:
    """
    Test motor 0..9 one by one.
    """
    for motor_idx in range(NUM_MOTORS):
        mask = motor_mask(motor_idx)
        print(f"Testing motor {motor_idx}  | mask={mask}")
        vibrate_once(ser, mask, AMP, ON_MS)

        if motor_idx != NUM_MOTORS - 1:
            time.sleep(WAIT_BETWEEN_MOTORS_S)


def main():
    if len(sys.argv) >= 2:
        port = sys.argv[1]
    else:
        port = auto_detect_port()

    ser = open_serial(port)

    try:
        # Safety stop twice
        send_line(ser, "X")
        time.sleep(INIT_RETRY_GAP_S)
        send_line(ser, "X")

        # Set PWM frequencies for all 10 outputs
        set_all_freqs(ser, FREQS)

        # Test motor 0..9 one by one
        test_motor_one_by_one(ser)

        # Final safety stop
        send_line(ser, "X")
        print("Done.")

    finally:
        ser.close()


if __name__ == "__main__":
    main()