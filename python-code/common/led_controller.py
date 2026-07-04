import threading
import time
from contextlib import contextmanager
from typing import Iterable, List, Sequence, Tuple

from serial_utils import auto_detect_port, open_serial


Color = Tuple[int, int, int]
PixelSpec = Tuple[int, int, int, int, int, int]  # strip, idx, r, g, b, brightness


class LEDArrayController:
    """
    High-level controller for the Teensy WS2812 LED firmware.

    This class is intentionally kept separate from the existing motor controller.
    It uses the same serial_utils helpers, but does not modify any motor-side code.

    Supported firmware commands:
        E                     -> echo firmware version
        L strip idx r g b br  -> set one pixel
        B brightness          -> set global brightness (0..255)
        C strip               -> clear strip 0 / 1 / -1(all)
        U                     -> show now
    """

    def __init__(
        self,
        port: str | None = None,
        num_strips: int = 2,
        strip_lengths: Sequence[int] = (60, 60),
        auto_show: bool = True,
        write_delay_s: float = 0.0,
    ):
        self.port = port
        self.num_strips = int(num_strips)
        self.strip_lengths = tuple(int(x) for x in strip_lengths)
        self.auto_show = bool(auto_show)
        self.write_delay_s = float(write_delay_s)

        if self.num_strips != len(self.strip_lengths):
            raise ValueError("num_strips must match length of strip_lengths")
        if self.num_strips <= 0:
            raise ValueError("num_strips must be positive")
        if any(n <= 0 for n in self.strip_lengths):
            raise ValueError("all strip lengths must be positive")

        self.ser = None
        self._lock = threading.RLock()
        self._batch_depth = 0

    # ---------- connection ----------
    def connect(self):
        """Open serial connection to the LED controller board."""
        with self._lock:
            if self.ser is not None:
                return self

            if self.port is None:
                self.port = auto_detect_port()

            self.ser = open_serial(self.port)

            # Small settle delay helps some boards after USB serial opens.
            time.sleep(0.05)
            return self

    def close(self):
        """Close serial connection."""
        with self._lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                finally:
                    self.ser = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    # ---------- low-level serial ----------
    def _require_connection(self):
        if self.ser is None:
            raise RuntimeError("Serial port is not connected. Call connect() first.")

    def send(self, cmd: str):
        """Send one raw command line to the firmware."""
        with self._lock:
            self._require_connection()
            self.ser.write((cmd.strip() + "\n").encode("utf-8"))
            self.ser.flush()
            if self.write_delay_s > 0:
                time.sleep(self.write_delay_s)

    def echo(self) -> str:
        """Read firmware echo string, useful for connection check."""
        with self._lock:
            self._require_connection()
            self.send("E")
            return self.ser.readline().decode(errors="ignore").strip()

    # ---------- validation helpers ----------
    def _validate_strip(self, strip: int):
        if not (0 <= int(strip) < self.num_strips):
            raise ValueError(f"invalid strip index: {strip}")

    def _validate_pixel(self, strip: int, idx: int):
        self._validate_strip(strip)
        if not (0 <= int(idx) < self.strip_lengths[int(strip)]):
            raise ValueError(f"invalid pixel index {idx} for strip {strip}")

    @staticmethod
    def _validate_u8(name: str, value: int):
        if not (0 <= int(value) <= 255):
            raise ValueError(f"{name} must be in range 0..255")

    # ---------- batching ----------
    @contextmanager
    def batch(self, show: bool = True):
        """
        Batch multiple pixel writes and optionally flush once at the end.

        This is the safest way to update many LEDs without excessive refreshes.
        """
        with self._lock:
            self._batch_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._batch_depth -= 1
                should_show = show and self._batch_depth == 0
            if should_show:
                self.show()

    def _maybe_show(self):
        if self.auto_show and self._batch_depth == 0:
            self.show()

    # ---------- LED commands ----------
    def show(self):
        """Force LED output immediately."""
        self.send("U")

    def set_global_brightness(self, brightness: int):
        """Set global brightness for all strips, 0..255."""
        self._validate_u8("brightness", brightness)
        self.send(f"B {int(brightness)}")
        self._maybe_show()

    def clear(self, strip: int = -1):
        """
        Clear one strip or all strips.

        strip:
            0   -> clear strip 0
            1   -> clear strip 1
            -1  -> clear all strips
        """
        if strip != -1:
            self._validate_strip(strip)
        self.send(f"C {int(strip)}")
        self._maybe_show()

    def set_pixel(
        self,
        strip: int,
        idx: int,
        r: int,
        g: int,
        b: int,
        brightness: int = 255,
    ):
        """Set one pixel with RGB + per-pixel brightness."""
        self._validate_pixel(strip, idx)
        self._validate_u8("r", r)
        self._validate_u8("g", g)
        self._validate_u8("b", b)
        self._validate_u8("brightness", brightness)

        self.send(f"L {int(strip)} {int(idx)} {int(r)} {int(g)} {int(b)} {int(brightness)}")
        self._maybe_show()

    def set_pixels(self, pixels: Iterable[PixelSpec], show: bool | None = None):
        """
        Set many pixels in one call.

        Each item must be:
            (strip, idx, r, g, b, brightness)
        """
        show = self.auto_show if show is None else bool(show)
        with self.batch(show=show):
            for item in pixels:
                if len(item) != 6:
                    raise ValueError("each pixel spec must have 6 values")
                self.set_pixel(*item)

    def fill_strip(
        self,
        strip: int,
        color: Color,
        brightness: int = 255,
        start: int = 0,
        end: int | None = None,
        show: bool | None = None,
    ):
        """
        Fill one strip or one range on a strip.

        start is inclusive, end is exclusive.
        """
        self._validate_strip(strip)
        r, g, b = color
        self._validate_u8("r", r)
        self._validate_u8("g", g)
        self._validate_u8("b", b)
        self._validate_u8("brightness", brightness)

        strip_len = self.strip_lengths[strip]
        if end is None:
            end = strip_len
        if not (0 <= start <= end <= strip_len):
            raise ValueError("invalid fill range")

        show = self.auto_show if show is None else bool(show)
        with self.batch(show=show):
            for idx in range(start, end):
                self.set_pixel(strip, idx, r, g, b, brightness)

    def off(self, strip: int = -1):
        """Alias of clear()."""
        self.clear(strip=strip)

    # ---------- convenience helpers ----------
    def red(self, strip: int, idx: int, brightness: int = 255):
        self.set_pixel(strip, idx, 255, 0, 0, brightness)

    def green(self, strip: int, idx: int, brightness: int = 255):
        self.set_pixel(strip, idx, 0, 255, 0, brightness)

    def blue(self, strip: int, idx: int, brightness: int = 255):
        self.set_pixel(strip, idx, 0, 0, 255, brightness)

    def white(self, strip: int, idx: int, brightness: int = 255):
        self.set_pixel(strip, idx, 255, 255, 255, brightness)

    def test_pattern(self, brightness: int = 64):
        """Simple startup test for both strips."""
        with self.batch(show=True):
            self.clear(-1)
            if self.strip_lengths[0] > 0:
                self.red(0, 0, brightness)
            if self.num_strips > 1 and self.strip_lengths[1] > 0:
                self.green(1, 0, brightness)


__all__ = ["LEDArrayController"]
