"""Shared serial-rig helpers for the validation experiments.

Both LRA sweeps talk to the same haptic-piano Teensy rig: identity
handshake over the 'E' command, motor drive via 'S'/'X'/'F', and the
LIS3DH accelerometer stream ("ACC,<id>,x,y,z", firmware >= v2.6.0).
This module holds the code that used to be duplicated at the top of
each sweep script.

Deliberately self-contained rather than built on common.serial_utils:
the sweeps need the Teensy VID:PID scoring, the firmware identity
check, raw access to the ACC stream, and a non-interactive mode so a
GUI worker thread fails with an error instead of blocking on input().
"""

import math
import sys
import time
from typing import Callable, List, Optional, Tuple

import serial
from serial.tools import list_ports

BAUD = 115200


class SweepAborted(Exception):
    """Raised inside a sweep when the caller's should_stop() turns true
    (GUI Stop button); the sweep's finally-block still restores the rig."""


def auto_detect_port(log: Callable[[str], None] = print,
                     interactive: bool = True) -> str:
    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found")

    scored = []
    for p in ports:
        score = 0
        name = (p.device or "").lower()
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()

        # Teensy's USB VID:PID is the strongest signal and needs no I/O.
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
        log(f"Auto-selected: {best.device} ({best.description})")
        return best.device

    listing = "\n".join(f"[{i}] {p.device} | {p.description}"
                        for i, p in enumerate(ports))
    if not interactive:
        raise RuntimeError(
            "Could not auto-select the rig's serial device. "
            f"Available ports:\n{listing}"
        )

    print("Could not auto-select device. Available ports:")
    print(listing)
    while True:
        idx = input("Select index: ").strip()
        if idx.isdigit() and 0 <= int(idx) < len(ports):
            return ports[int(idx)].device


def send(ser: serial.Serial, cmd: str, wait_s: float = 0.05) -> None:
    ser.write((cmd.strip() + "\n").encode("utf-8"))
    ser.flush()
    if wait_s > 0:
        time.sleep(wait_s)


def open_rig(log: Callable[[str], None] = print,
             interactive: bool = True) -> serial.Serial:
    port = auto_detect_port(log=log, interactive=interactive)
    ser = serial.Serial(port, BAUD, timeout=0.05)
    time.sleep(2.0)  # let the board settle after the port opens
    ser.reset_input_buffer()

    # Identity handshake: expect "E haptic-piano vX.Y.Z".
    send(ser, "E", wait_s=0.0)
    deadline = time.monotonic() + 1.0
    identity = ""
    while time.monotonic() < deadline:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if line.startswith("E "):
            identity = line
            break
    if "haptic-piano" not in identity:
        log(f"WARNING: unexpected identity reply {identity!r} - "
            "is this the right device / firmware >= v2.6.0?")
    else:
        log(f"Connected: {identity}")
    # Stash the identity on the connection so experiments can record it
    # in their run metadata ("E haptic-piano vX.Y.Z", minus the "E ").
    ser.rig_identity = identity[2:] if identity.startswith("E ") else identity
    return ser


def parse_acc_line(raw: str, sensor_id: int) -> Optional[Tuple[int, int, int]]:
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


def collect_magnitudes(ser: serial.Serial, duration_s: float,
                       sensor_id: int) -> List[float]:
    """Read the ACC stream for duration_s and return |a| magnitudes."""
    ser.reset_input_buffer()  # drop samples from before this window
    mags: List[float] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw, sensor_id)
        if sample is None:
            continue
        x, y, z = sample
        mags.append(math.sqrt(x * x + y * y + z * z))
    return mags


def collect_samples(ser: serial.Serial, duration_s: float,
                    sensor_id: int) -> List[Tuple[float, int, int, int]]:
    """Read the ACC stream for duration_s and return (t, x, y, z) tuples.

    t is the host-arrival time (time.monotonic seconds); x/y/z are raw
    LIS3DH counts. Like collect_magnitudes but keeps the per-axis values
    and a timestamp per sample, so a caller can both compute the |a|
    magnitude (sqrt(x^2+y^2+z^2)) and estimate a vibration frequency from
    the sample series. Host timestamps jitter per sample, but their mean
    rate over the window equals the firmware ODR, which is what a
    dominant-frequency estimate needs."""
    ser.reset_input_buffer()  # drop samples from before this window
    samples: List[Tuple[float, int, int, int]] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw, sensor_id)
        if sample is None:
            continue
        x, y, z = sample
        samples.append((time.monotonic(), x, y, z))
    return samples
