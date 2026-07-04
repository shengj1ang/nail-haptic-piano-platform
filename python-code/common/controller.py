import time
from serial_utils import auto_detect_port, open_serial


class VibratorController:
    """
    High-level controller for async vibration system (P command).
    """

    def __init__(self, port=None, num_motors=10):
        self.port = port
        self.num_motors = num_motors
        self.ser = None

    def connect(self):
        if self.port is None:
            self.port = auto_detect_port()

        self.ser = open_serial(self.port)

        # safety stop
        self.send("X")
        time.sleep(0.05)
        self.send("X")

    def close(self):
        if self.ser:
            self.ser.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, cmd: str):
        """Send raw command"""
        self.ser.write((cmd.strip() + "\n").encode())
        self.ser.flush()

    def echo(self):
        """Get firmware version"""
        self.send("E")
        line = self.ser.readline().decode(errors="ignore").strip()
        return line

    def stop_all(self):
        self.send("X")

    def pulse(self, idx, count=1, amp=80, on_ms=120, off_ms=120):
        """
        Send async pulse task to one motor.

        P idx count amp on off
        """
        if not (0 <= idx < self.num_motors):
            raise ValueError("invalid motor index")

        cmd = f"P {idx} {count} {amp} {on_ms} {off_ms}"
        self.send(cmd)

    def pulse_many(self, motors, count=1, amp=80, on_ms=120, off_ms=120):
        """
        Trigger multiple motors at once (async).
        """
        for m in motors:
            self.pulse(m, count, amp, on_ms, off_ms)