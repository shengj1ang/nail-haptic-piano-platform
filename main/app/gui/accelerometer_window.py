"""Accelerometer Live View - launcher section 2 (feature testing).

Live X/Y/Z plot of one LIS3DH on the vibration rig, the GUI successor
to the standalone plotters in read_data_from_accelerometer/ (which
predate the unified firmware and parse its old bare "x,y,z" lines).
This window talks to the current firmware instead: "A START <ms>"
starts the stream, every sensor arrives interleaved as
"ACC,<id>,x,y,z" (>= v2.5.0), and the selector picks which <id> to
follow - switchable live, since the stream always carries all sensors.

Changing the selector only retargets the live view; nothing is written
until the user presses "Save as Default", which stores the id in
config.json (accelerometer.sensor_id) - the value the validation sweep
windows preselect as their ACC sensor. The window shows the currently
saved id next to the selector so the two states can't be confused.

Serial I/O runs on a QThread (reusing validation_experiments/rig.py's
port detection + ACC parsing); samples arrive in batches and a QTimer
redraws the rolling window, so the GUI stays responsive at 100 Hz.
"""

import sys
import time
from collections import deque

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from validation_experiments.rig import open_rig, parse_acc_line, send

ACC_INTERVAL_MS = 10     # "A START 10" = 100 Hz, plenty for a live view
MAX_POINTS = 400         # rolling window length (samples)
REDRAW_MS = 50           # plot refresh period
BATCH_EMIT_S = 0.05      # worker flushes its sample batch at least this often


class _AccStreamWorker(QThread):
    """Owns the serial port: opens the rig, starts the ACC stream, and
    relays the selected sensor's samples in small batches. The GUI may
    retarget `sensor_id` live - the stream itself carries every sensor,
    so switching is just a filter change, no serial round-trip."""

    samples = Signal(list)  # list[(x, y, z)] for the selected sensor
    log = Signal(str)
    failed = Signal(str)

    def __init__(self, sensor_id: int):
        super().__init__()
        self.sensor_id = sensor_id  # written from the GUI thread on switch
        # Exposed while streaming so a host window (the validation sweeps'
        # Test Buzz) can write motor commands over the same connection
        # instead of double-opening the port. Reads stay worker-only.
        self.ser = None
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            ser = open_rig(log=self.log.emit, interactive=False)
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.ser = ser
        try:
            send(ser, "A STOP", wait_s=0.2)
            ser.reset_input_buffer()
            send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)
            self.log.emit(f"Streaming started (A START {ACC_INTERVAL_MS}).")

            batch = []
            last_emit = time.monotonic()
            while not self._stop_requested:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
                if raw:
                    sample = parse_acc_line(raw, self.sensor_id)
                    if sample is not None:
                        batch.append(sample)
                now = time.monotonic()
                if batch and (len(batch) >= 20 or now - last_emit >= BATCH_EMIT_S):
                    self.samples.emit(batch)
                    batch = []
                    last_emit = now
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self.ser = None  # withdraw shared access before closing
            try:
                send(ser, "A STOP", wait_s=0.1)
            except Exception:
                pass
            ser.close()
            self.log.emit("Serial port closed.")


class AccelerometerWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Accelerometer Live View")
        self.cfg = cfg if cfg is not None else Config.load()
        self._worker = None
        self._sample_index = 0
        self._dirty = False
        self._ts = deque(maxlen=MAX_POINTS)
        self._xs = deque(maxlen=MAX_POINTS)
        self._ys = deque(maxlen=MAX_POINTS)
        self._zs = deque(maxlen=MAX_POINTS)

        self.status = QLabel("Accelerometer: not connected")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)

        self.sensor_spin = QSpinBox()
        self.sensor_spin.setRange(0, 7)
        self.sensor_spin.setValue(self.cfg.accelerometer.sensor_id)
        self.sensor_spin.setToolTip(
            "LIS3DH sensor id to follow in the ACC stream - switchable "
            "while streaming; only saved to config.json via Save as Default")
        self.sensor_spin.valueChanged.connect(self._on_sensor_changed)

        self.saved_label = QLabel()
        self.save_btn = QPushButton("Save as Default")
        self.save_btn.setToolTip(
            "Write the selected sensor id to config.json "
            "(accelerometer.sensor_id)")
        self.save_btn.clicked.connect(self._save_default)
        self._refresh_saved_state()

        hint = QLabel(
            "Changing the sensor id only retargets the live view - nothing "
            "is saved automatically. Press \"Save as Default\" to store it in "
            "config.json (accelerometer.sensor_id); the saved id is what the "
            "Validation Experiments windows (launcher section 9) preselect as "
            "their ACC sensor."
        )
        hint.setWordWrap(True)

        top_row = QHBoxLayout()
        top_row.addWidget(self.status, 1)
        top_row.addWidget(QLabel("ACC sensor id:"))
        top_row.addWidget(self.sensor_spin)
        top_row.addWidget(self.saved_label)
        top_row.addWidget(self.save_btn)
        top_row.addWidget(self.connect_btn)

        figure = Figure(figsize=(10, 5))
        self.canvas = FigureCanvas(figure)
        self.ax = figure.add_subplot(111)
        (self.line_x,) = self.ax.plot([], [], label="X")
        (self.line_y,) = self.ax.plot([], [], label="Y")
        (self.line_z,) = self.ax.plot([], [], label="Z")
        self._style_axes()

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(90)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top_row)
        layout.addWidget(hint)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.log_view)
        self.setCentralWidget(central)
        self.resize(860, 560)

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(REDRAW_MS)
        self._redraw_timer.timeout.connect(self._redraw)

    def _style_axes(self) -> None:
        self.ax.set_title(f"LIS3DH sensor {self.sensor_spin.value()} - "
                          "real-time acceleration")
        self.ax.set_xlabel("Sample")
        self.ax.set_ylabel("Acceleration (raw counts)")
        self.ax.legend(loc="upper right")
        self.ax.grid(True)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _toggle_connect(self) -> None:
        if self._worker is not None:
            self._disconnect()
            return

        self._clear_plot()
        self.connect_btn.setEnabled(False)
        self.status.setText("Accelerometer: connecting...")

        self._worker = _AccStreamWorker(self.sensor_spin.value())
        self._worker.samples.connect(self._on_samples)
        self._worker.log.connect(self.log_view.appendPlainText)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()
        self._redraw_timer.start()

        self.connect_btn.setText("Disconnect")
        self.connect_btn.setEnabled(True)
        self.status.setText(f"Accelerometer: streaming sensor "
                            f"{self.sensor_spin.value()}")

    def _disconnect(self) -> None:
        if self._worker is None:
            return
        self.connect_btn.setEnabled(False)
        self._worker.request_stop()
        self._worker.wait(5000)
        # _on_finished restores the buttons/status.

    # ------------------------------------------------------------------
    # Single-port arbitration hooks for host windows (validation sweeps)
    # ------------------------------------------------------------------

    def streaming_serial(self):
        """The live serial connection while streaming, else None. Hosts
        may write motor commands on it (e.g. a test buzz, which then
        shows up in the plot) but must never read from it."""
        worker = self._worker
        return worker.ser if worker is not None else None

    def disconnect_stream(self) -> None:
        """Release the serial port (no-op when not streaming) - called by
        a sweep window before its worker claims the port."""
        self._disconnect()

    def set_connect_allowed(self, allowed: bool) -> None:
        """Host windows disable Connect while their sweep owns the port."""
        if allowed:
            self.connect_btn.setEnabled(True)
        elif self._worker is None:
            self.connect_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Sensor selection (saved to config.json only via Save as Default)
    # ------------------------------------------------------------------

    def _refresh_saved_state(self) -> None:
        saved = self.cfg.accelerometer.sensor_id
        self.saved_label.setText(f"Saved default: {saved}")
        # Nothing to save while the selection matches config.json.
        self.save_btn.setEnabled(self.sensor_spin.value() != saved)

    def _on_sensor_changed(self, sensor_id: int) -> None:
        if self._worker is not None:
            self._worker.sensor_id = sensor_id
            self._clear_plot()
            self.status.setText(f"Accelerometer: streaming sensor {sensor_id}")
        self.ax.set_title(f"LIS3DH sensor {sensor_id} - real-time acceleration")
        self._dirty = True
        self._refresh_saved_state()

    def _save_default(self) -> None:
        sensor_id = self.sensor_spin.value()
        self.cfg.accelerometer.sensor_id = sensor_id
        self.cfg.save()
        self._refresh_saved_state()
        self.log_view.appendPlainText(
            f"Saved sensor id {sensor_id} to config.json - now the default "
            "for the validation experiments.")

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _clear_plot(self) -> None:
        self._sample_index = 0
        for buf in (self._ts, self._xs, self._ys, self._zs):
            buf.clear()
        self._dirty = True

    def _on_samples(self, batch: list) -> None:
        for x, y, z in batch:
            self._ts.append(self._sample_index)
            self._xs.append(x)
            self._ys.append(y)
            self._zs.append(z)
            self._sample_index += 1
        self._dirty = True

    def _redraw(self) -> None:
        if not self._dirty:
            return
        self._dirty = False

        self.line_x.set_data(self._ts, self._xs)
        self.line_y.set_data(self._ts, self._ys)
        self.line_z.set_data(self._ts, self._zs)

        if self._ts:
            left = self._ts[0]
            right = self._ts[-1] if self._ts[-1] > self._ts[0] else self._ts[0] + 1
            self.ax.set_xlim(left, right)

            data_min = min(min(self._xs), min(self._ys), min(self._zs))
            data_max = max(max(self._xs), max(self._ys), max(self._zs))
            if data_min == data_max:
                data_min -= 1
                data_max += 1
            padding = max(20, int((data_max - data_min) * 0.1))
            self.ax.set_ylim(data_min - padding, data_max + padding)

        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    # Worker teardown
    # ------------------------------------------------------------------

    def _on_failed(self, message: str) -> None:
        self.log_view.appendPlainText(f"ERROR: {message}")
        self.status.setText(f"Accelerometer: failed ({message})")

    def _on_finished(self) -> None:
        self._worker = None
        self._redraw_timer.stop()
        self.connect_btn.setText("Connect")
        self.connect_btn.setEnabled(True)
        if not self.status.text().startswith("Accelerometer: failed"):
            self.status.setText("Accelerometer: not connected")

    def closeEvent(self, event) -> None:
        self._disconnect()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = AccelerometerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
