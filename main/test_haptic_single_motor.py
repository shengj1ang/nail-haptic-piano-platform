"""Manual haptic motor bench: tick one or more motor ports, dial in a PWM
frequency and drive amp with draggable sliders, choose a run duration and
a vibration pattern, then start/stop the selected motors.

A minimal front end over common.controller.VibratorController (the same
manual connect/disconnect convention as test_haptic_vibrator.py's
HapticTestWindow), for quickly feeling one or several actuators at an
arbitrary frequency/amp - e.g. trying the LRA at its resonance vs the ERM
at a kHz carrier, or sanity-checking a wiring change - without editing a
script or running a full sweep. The sliders OPEN on the configured
default drive of the actuator in use (config.json's haptic block, set in
Initial Setup -> Haptic Actuator Defaults); dragging them from there is
the whole point of this window, so nothing re-applies the config
afterwards. It sends the raw firmware commands the sweeps use:
`F <port> <freq>` to set each port's PWM frequency, `S <mask> <amp>` to
drive the whole selected set at once, `X` to stop.

The motor selection, frequency and amp are pushed to the device live:
while motors are energised, dragging a slider or ticking/unticking a port
re-tunes the running set immediately, so you can feel the change without
stopping. On disconnect/exit every retuned port's frequency is reset to
the boot default (the only setting the firmware keeps between runs).

Runs standalone (python test_haptic_single_motor.py) or from the
launcher's "Feature Testing" section.
"""

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.gui.accelerometer_window import AccelerometerWindow
from common import haptic_config as hc
from common.controller import VibratorController

# Firmware 'F' command PWM-frequency slider ranges. "High precision"
# trades reach for a finer step by mapping the full slider travel onto
# the LRA band (the same upper bound the config validator allows for an
# LRA); the normal band reaches the kHz values an ERM needs.
FREQ_MAX_HIGH = hc.FREQUENCY_LIMITS[hc.LRA][1]   # high-precision slider max (Hz)
FREQ_MAX_NORMAL = 5000    # normal slider max (Hz)
AMP_MAX = hc.AMP_MAX
MAX_MOTOR_INDEX = 11      # motor-port range exposed here (LRA=11, ERM=10)


def default_freq_hz() -> int:
    """Configured default drive frequency of the actuator in use."""
    return hc.get_active_haptic_defaults().default_frequency


def default_amp() -> int:
    """Configured default drive amp of the actuator in use."""
    return hc.get_active_haptic_defaults().default_amp


def freq_slider_max() -> int:
    """Upper end of the normal (non-high-precision) frequency slider.

    Widened when a configured default sits above the usual band (an ERM
    carrier may legitimately be set well past 5 kHz), so the slider can
    always SHOW the configured value instead of silently clamping it to
    a different one.
    """
    return max(FREQ_MAX_NORMAL, hc.get_haptic_config().lra.default_frequency,
               hc.get_haptic_config().erm.default_frequency)

# Vibrate + pause loop defaults.
DEFAULT_ON_MS = 500
DEFAULT_OFF_MS = 500


class SingleMotorHapticWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Haptic Motor Bench")
        self.cfg = cfg if cfg is not None else Config.load()

        # Manual (button-triggered) connection, never opened on launch -
        # same convention as HapticTestWindow.
        self.controller = VibratorController()
        self.connected = False

        # Run state.
        self._running = False   # a start/stop session is active
        self._on_now = False    # the motor(s) are energised right now
        # Ports we've retuned with 'F'; their PWM frequency otherwise
        # persists on the firmware, so we reset these on disconnect/exit.
        self._touched_ports: set[int] = set()

        # Accelerometer Live View. The rig has ONE serial port, so the two
        # cannot each open it: instead, while the live view is streaming it
        # owns the port and we drive motors by WRITING over its connection
        # (the drive then shows up in its plot) - the same single-port
        # arbitration the validation sweeps use for their Test Buzz.
        self._acc_view = None
        self._acc_ready_prev = False
        # Poll the live view's stream state (it opens the port on a worker
        # thread, and the user can connect/disconnect it from its own
        # window) to keep our Start button and status in step.
        self._acc_sync_timer = QTimer(self)
        self._acc_sync_timer.setInterval(300)
        self._acc_sync_timer.timeout.connect(self._acc_sync_tick)

        # Loop-phase timer (toggles on/off) and total-duration timer.
        self._loop_timer = QTimer(self)
        self._loop_timer.setSingleShot(True)
        self._loop_timer.timeout.connect(self._loop_tick)
        self._duration_timer = QTimer(self)
        self._duration_timer.setSingleShot(True)
        self._duration_timer.timeout.connect(self._stop)

        # --- connection row ---------------------------------------------
        self.status = QLabel("Not connected.")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.open_acc_btn = QPushButton("Open Accelerometer Live View")
        self.open_acc_btn.setToolTip(
            "Open the live X/Y/Z plot and drive at the same time. The rig has "
            "one serial port, so the live view takes it over and the bench's "
            "motor commands ride on its stream - you feel the motor and watch "
            "it in the plot together.")
        self.open_acc_btn.clicked.connect(self._open_acc_view)
        conn_row = QHBoxLayout()
        conn_row.addWidget(self.status, 1)
        conn_row.addWidget(self.open_acc_btn)
        conn_row.addWidget(self.connect_btn)

        # --- parameters --------------------------------------------------
        # Motors: a checkbox per exposed port - tick one or more to drive
        # them together (they share the frequency/amp and are driven with a
        # single masked 'S' command).
        self.motor_checks: list[QCheckBox] = []
        motor_row = QHBoxLayout()
        motor_row.setContentsMargins(0, 0, 0, 0)
        for i in range(MAX_MOTOR_INDEX + 1):
            cb = QCheckBox(str(i))
            cb.toggled.connect(self._on_motor_changed)
            self.motor_checks.append(cb)
            motor_row.addWidget(cb)
        motor_row.addStretch(1)
        self.motor_box = QWidget()
        self.motor_box.setLayout(motor_row)
        self.motor_box.setToolTip("Motor ports to drive - tick one or more "
                                  "(LRA is wired to 11, ERM to 10).")

        # Frequency: draggable slider + a high-precision toggle that swaps
        # the slider's range.
        self.freq_hi_check = QCheckBox("High precision (0-1000 Hz)")
        self.freq_hi_check.setToolTip(
            "Map the whole slider onto 0-1000 Hz for a finer step near the "
            "LRA resonance. Unchecked reaches 0-5000 Hz for ERM use.")
        self.freq_hi_check.toggled.connect(self._on_freq_range_toggled)

        self.freq_slider = QSlider(Qt.Horizontal)
        self.freq_slider.setRange(0, freq_slider_max())
        self.freq_slider.setValue(default_freq_hz())
        self.freq_slider.setToolTip(
            "PWM frequency for the port (F command). Opens on the configured "
            f"default for the actuator in use ({hc.summary_line()}); an LRA "
            "wants its resonance, an ERM needs kHz (e.g. 5000) or it stalls.")
        self.freq_value = QLabel()
        self.freq_slider.valueChanged.connect(self._on_freq_changed)

        # Amp: draggable slider (PWM duty, 0-255 scale).
        self.amp_slider = QSlider(Qt.Horizontal)
        self.amp_slider.setRange(0, AMP_MAX)
        self.amp_slider.setValue(default_amp())
        self.amp_slider.setToolTip("Drive amplitude (PWM duty, 0-255 scale)")
        self.amp_value = QLabel()
        self.amp_slider.valueChanged.connect(self._on_amp_changed)

        # Duration: continuous for a fixed time, or until manually stopped.
        self.duration_combo = QComboBox()
        self.duration_combo.addItem("Until manual stop", None)
        for s in range(1, 21):
            self.duration_combo.addItem(f"{s} s", s * 1000)
        self.duration_combo.setToolTip(
            "How long the session runs before stopping on its own.")

        # Pattern: steady drive, or a vibrate/pause loop.
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Continuous", "continuous")
        self.mode_combo.addItem("Vibrate + pause loop", "loop")
        self.mode_combo.setToolTip(
            "Continuous drives the motor steadily; the loop pattern "
            "alternates a vibrate phase and a pause phase.")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        # Loop on/off durations (only meaningful in loop mode).
        self.on_ms_spin = QSpinBox()
        self.on_ms_spin.setRange(10, 20000)
        self.on_ms_spin.setValue(DEFAULT_ON_MS)
        self.on_ms_spin.setSuffix(" ms")
        self.on_ms_spin.setToolTip("Vibrate phase length in the loop pattern.")
        self.off_ms_spin = QSpinBox()
        self.off_ms_spin.setRange(10, 20000)
        self.off_ms_spin.setValue(DEFAULT_OFF_MS)
        self.off_ms_spin.setSuffix(" ms")
        self.off_ms_spin.setToolTip("Pause phase length in the loop pattern.")

        self.loop_widgets = QWidget()
        loop_row = QHBoxLayout(self.loop_widgets)
        loop_row.setContentsMargins(0, 0, 0, 0)
        loop_row.addWidget(QLabel("Vibrate:"))
        loop_row.addWidget(self.on_ms_spin)
        loop_row.addSpacing(12)
        loop_row.addWidget(QLabel("Pause:"))
        loop_row.addWidget(self.off_ms_spin)
        loop_row.addStretch(1)

        # Lay the parameters out on a grid (label | control).
        grid = QGridLayout()
        grid.addWidget(QLabel("Motors:"), 0, 0)
        grid.addWidget(self.motor_box, 0, 1)

        freq_row = QHBoxLayout()
        freq_row.addWidget(self.freq_slider, 1)
        freq_row.addWidget(self.freq_value)
        freq_row.addSpacing(12)
        freq_row.addWidget(self.freq_hi_check)
        grid.addWidget(QLabel("Frequency:"), 1, 0)
        grid.addLayout(freq_row, 1, 1)

        amp_row = QHBoxLayout()
        amp_row.addWidget(self.amp_slider, 1)
        amp_row.addWidget(self.amp_value)
        grid.addWidget(QLabel("Amp:"), 2, 0)
        grid.addLayout(amp_row, 2, 1)

        grid.addWidget(QLabel("Duration:"), 3, 0)
        grid.addWidget(self.duration_combo, 3, 1)
        grid.addWidget(QLabel("Pattern:"), 4, 0)
        grid.addWidget(self.mode_combo, 4, 1)
        grid.addWidget(self.loop_widgets, 5, 1)
        grid.setColumnStretch(1, 1)

        # --- start/stop button ------------------------------------------
        self.run_btn = QPushButton("Start")
        self.run_btn.setMinimumHeight(70)
        self.run_btn.setEnabled(False)  # until connected
        self.run_btn.clicked.connect(self._toggle_run)

        # One-click return of just the frequency + amp controls to their
        # defaults; works live while a motor is running.
        self.reset_btn = QPushButton("Restore default amp/frequency")
        self.reset_btn.setToolTip(
            "Reset frequency and amp to the configured default for the "
            f"actuator in use ({hc.summary_line()}, from config.json) - "
            "applied live if a motor is running.")
        self.reset_btn.clicked.connect(self._reset_params)

        instructions = QLabel(
            "Connect the rig, tick one or more motors and drag the frequency / "
            "amp sliders, choose a duration and pattern, then press Start. The "
            "motor selection, frequency and amp are pushed live, so you can "
            "re-tune while it runs; press Stop (or wait out the duration) to "
            "end.")
        instructions.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(conn_row)
        layout.addWidget(instructions)
        layout.addLayout(grid)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.run_btn, 1)
        btn_row.addWidget(self.reset_btn)
        layout.addLayout(btn_row)
        layout.addStretch(1)
        self.setCentralWidget(central)
        self.resize(620, 380)

        # Initial label / selection / enabled-state sync.
        self._set_default_motors()
        self._refresh_freq_label()
        self._refresh_amp_label()
        self._on_mode_changed()

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _selected_motors(self) -> list[int]:
        return [i for i, cb in enumerate(self.motor_checks) if cb.isChecked()]

    @staticmethod
    def _mask(motors) -> int:
        m = 0
        for i in motors:
            m |= 1 << i
        return m

    def _set_default_motors(self) -> None:
        """Default selection: motor 0 only (used at launch and on reset)."""
        for i, cb in enumerate(self.motor_checks):
            cb.setChecked(i == 0)

    def _refresh_freq_label(self) -> None:
        self.freq_value.setText(f"{self.freq_slider.value()} Hz")

    def _refresh_amp_label(self) -> None:
        self.amp_value.setText(str(self.amp_slider.value()))

    def _reset_params(self) -> None:
        """Put just the frequency and amp controls back to their defaults.

        Works live: if a motor is running, resetting the sliders pushes the
        default frequency/amp to it immediately (via the valueChanged
        handlers), so you can one-click back to the default feel mid-test.
        Unlike _restore_defaults it leaves the run going and the ports as-is.
        """
        self.freq_hi_check.setChecked(False)
        self.freq_slider.setMaximum(freq_slider_max())
        self.freq_slider.setValue(default_freq_hz())
        self.amp_slider.setValue(default_amp())

    # ------------------------------------------------------------------
    # Serial transport (own port, or the live view's shared stream)
    # ------------------------------------------------------------------

    def _acc_serial(self):
        """The Accelerometer Live View's streaming connection while it is
        open and streaming, else None. When present it is the single port's
        owner and we write motor commands over it (never read)."""
        if self._acc_view is not None and self._acc_view.isVisible():
            return self._acc_view.streaming_serial()
        return None

    def _has_device(self) -> bool:
        """True when we can send motor commands right now - either the live
        view's stream is up, or we hold our own controller connection."""
        return self._acc_serial() is not None or self.connected

    def _send(self, cmd: str) -> None:
        """Send a raw firmware command over whichever transport is live,
        preferring the live view's shared stream so both work at once."""
        ser = self._acc_serial()
        if ser is not None:
            try:
                ser.write((cmd.strip() + "\n").encode())
                ser.flush()
            except Exception:
                pass
            return
        if self.connected:
            self.controller.send(cmd)

    def _conn_label(self) -> str:
        if self._acc_serial() is not None:
            return "via Accelerometer Live View"
        if self.connected:
            return f"on {self.controller.port}"
        return "not connected"

    def _sync_run_enabled(self) -> None:
        self.run_btn.setEnabled(self._has_device())

    # ------------------------------------------------------------------
    # Connection (manual toggle)
    # ------------------------------------------------------------------

    def _toggle_connect(self) -> None:
        if self.connected:
            # Stop, put the device's frequency back to default, reset controls.
            self._restore_defaults()
            self.controller.close()
            self.connected = False
            self.status.setText("Not connected.")
            self.connect_btn.setText("Connect")
            self._sync_run_enabled()
            return

        # The live view owns the single serial port while it's open - don't
        # double-open; drive directly with Start over its stream.
        if self._acc_view is not None and self._acc_view.isVisible():
            QMessageBox.information(
                self, "Live View owns the serial port",
                "The Accelerometer Live View is using the rig's serial port. "
                "Just press Start - the bench drives motors over its stream, "
                "and you'll see them in the plot.")
            return

        try:
            self.controller.connect()
        except Exception as exc:
            QMessageBox.warning(self, "Connection failed", str(exc))
            self.status.setText(f"Not connected ({exc})")
            return

        self.connected = True
        self.status.setText(f"Connected on {self.controller.port}")
        self.connect_btn.setText("Disconnect")
        self._sync_run_enabled()

    # ------------------------------------------------------------------
    # Accelerometer Live View (shared single serial port)
    # ------------------------------------------------------------------

    def _open_acc_view(self) -> None:
        if self._acc_view is not None and self._acc_view.isVisible():
            self._acc_view.raise_()
            self._acc_view.activateWindow()
            return
        # Hand the single serial port to the live view: release our own
        # connection first (a double-open fails on Windows and interleaves
        # reads on macOS), then drive over the view's stream from now on.
        self._release_controller()
        self._acc_view = AccelerometerWindow(self.cfg)
        self._acc_view.closed.connect(self._on_acc_view_closed)
        self._acc_view.show()
        self._acc_view.ensure_streaming()   # opens the port + starts streaming
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Connect")
        self.open_acc_btn.setText("Show Accelerometer Live View")
        self._acc_ready_prev = False
        self._acc_sync_timer.start()
        self.status.setText(
            "Accelerometer Live View opening - motor commands will ride on "
            "its stream and show in the plot. Press Start once it is streaming.")
        self._sync_run_enabled()

    def _on_acc_view_closed(self) -> None:
        self._acc_sync_timer.stop()
        if self._running:
            self._stop()            # the port it drove over is gone
        self._acc_view = None
        self.connect_btn.setEnabled(True)
        self.open_acc_btn.setText("Open Accelerometer Live View")
        self.status.setText(
            "Accelerometer Live View closed. Press Connect to drive on the "
            "bench's own serial port.")
        self._sync_run_enabled()

    def _acc_sync_tick(self) -> None:
        """Keep Start + status in step with the live view's stream, which
        comes up on a worker thread and can be toggled from its own window."""
        ready = self._acc_serial() is not None
        if ready != self._acc_ready_prev:
            self._acc_ready_prev = ready
            if ready:
                self.status.setText(
                    "Accelerometer Live View streaming - press Start to drive; "
                    "the motor appears in the plot.")
            elif not self._running:
                self.status.setText(
                    "Accelerometer Live View not streaming - press Connect in "
                    "its window to drive over it.")
        # Transport lost mid-run (view disconnected from its own window).
        if self._running and not self._has_device():
            self._stop()
        self._sync_run_enabled()

    def _release_controller(self) -> None:
        """Stop any drive and close our own controller connection (if held)
        so another owner - the live view's stream - can take the port. Keeps
        the frequency/amp/motor controls as they are (unlike _restore_defaults)
        so the current settings carry over to driving via the stream."""
        self._stop()
        if self.connected:
            for port in sorted(self._touched_ports):
                try:
                    self.controller.send(f"F {port} {hc.FIRMWARE_BOOT_PWM_HZ}")
                except Exception:
                    pass
            self._touched_ports.clear()
            try:
                self.controller.close()
            except Exception:
                pass
            self.connected = False

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def _toggle_run(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if not self._has_device() or self._running:
            return
        self._running = True
        self.run_btn.setText("Stop")

        # Total-duration guard (None = until manual stop).
        total_ms = self.duration_combo.currentData()
        if total_ms is not None:
            self._duration_timer.start(total_ms)

        if self.mode_combo.currentData() == "loop":
            # Begin on the vibrate phase; _loop_tick alternates thereafter.
            self._phase_on = True
            self._enter_loop_phase()
        else:
            self._drive_on()
        self._refresh_status()

    def _stop(self) -> None:
        self._loop_timer.stop()
        self._duration_timer.stop()
        if self._on_now:
            self._send("X")
        self._on_now = False
        self._running = False
        self.run_btn.setText("Start")
        if self._has_device():
            self.status.setText(f"Ready - {self._conn_label()}.")

    def _restore_defaults(self) -> None:
        """Return the device and controls to their boot defaults.

        Stops any drive (X), resets every port we retuned back to the LRA
        boot frequency (the only setting the firmware keeps between runs),
        then puts the frequency/amp controls back to their defaults. Amp
        has no standalone device state - X already cleared the drive - so
        for amp this is just the control reset.
        """
        self._stop()  # clears running/on_now and sends X if still driving
        if self._has_device():
            for port in sorted(self._touched_ports):
                self._send(f"F {port} {hc.FIRMWARE_BOOT_PWM_HZ}")
        self._touched_ports.clear()
        self.freq_hi_check.setChecked(False)
        self.freq_slider.setMaximum(freq_slider_max())
        self.freq_slider.setValue(default_freq_hz())
        self.amp_slider.setValue(default_amp())
        self._set_default_motors()

    # ------------------------------------------------------------------
    # Driving
    # ------------------------------------------------------------------

    def _drive_on(self) -> None:
        """Energise every selected motor at the current freq / amp."""
        motors = self._selected_motors()
        if not motors:
            self._on_now = False
            return
        freq = self.freq_slider.value()
        amp = self.amp_slider.value()
        for m in motors:
            self._send(f"F {m} {freq}")
            self._touched_ports.add(m)
        self._send(f"S {self._mask(motors)} {amp}")
        self._on_now = True

    def _drive_off(self) -> None:
        self._send("X")
        self._on_now = False

    def _enter_loop_phase(self) -> None:
        """Run the current loop phase and schedule the next flip."""
        if self._phase_on:
            self._drive_on()
            self._loop_timer.start(self.on_ms_spin.value())
        else:
            self._drive_off()
            self._loop_timer.start(self.off_ms_spin.value())

    def _loop_tick(self) -> None:
        if not self._running:
            return
        self._phase_on = not self._phase_on
        self._enter_loop_phase()
        self._refresh_status()

    def _refresh_status(self) -> None:
        if not self._running:
            return
        motors = self._selected_motors()
        if not motors:
            self.status.setText("No motor selected - tick at least one.")
            return
        label = ", ".join(str(m) for m in motors)
        freq = self.freq_slider.value()
        amp = self.amp_slider.value()
        if self._on_now:
            self.status.setText(
                f"Vibrating motor(s) {label} at {freq} Hz, amp {amp}...")
        else:
            self.status.setText(f"Motor(s) {label} paused (loop)...")

    # ------------------------------------------------------------------
    # Live parameter changes (pushed while running)
    # ------------------------------------------------------------------

    def _on_motor_changed(self) -> None:
        if self._running and self._on_now:
            # Selection changed while driving: stop everything, then drive
            # the new set (X clears any now-deselected port).
            self._send("X")
            self._drive_on()
        self._refresh_status()

    def _on_freq_changed(self) -> None:
        self._refresh_freq_label()
        if self._running and self._on_now:
            motors = self._selected_motors()
            freq = self.freq_slider.value()
            for m in motors:
                self._send(f"F {m} {freq}")
            self._send(f"S {self._mask(motors)} {self.amp_slider.value()}")
            self._refresh_status()

    def _on_amp_changed(self) -> None:
        self._refresh_amp_label()
        if self._running and self._on_now:
            motors = self._selected_motors()
            self._send(f"S {self._mask(motors)} {self.amp_slider.value()}")
            self._refresh_status()

    def _on_freq_range_toggled(self, high: bool) -> None:
        new_max = FREQ_MAX_HIGH if high else freq_slider_max()
        # Preserve the value where possible (clamped into the new band).
        current = min(self.freq_slider.value(), new_max)
        self.freq_slider.setMaximum(new_max)
        self.freq_slider.setValue(current)
        self._refresh_freq_label()

    def _on_mode_changed(self) -> None:
        self.loop_widgets.setVisible(self.mode_combo.currentData() == "loop")

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._loop_timer.stop()
        self._duration_timer.stop()
        self._acc_sync_timer.stop()
        # Don't let a late 'closed' callback fire into this dying window.
        if self._acc_view is not None:
            try:
                self._acc_view.closed.disconnect(self._on_acc_view_closed)
            except Exception:
                pass
        if self.connected:
            self._restore_defaults()  # stop, reset frequency + controls
            self.controller.close()
        else:
            self._stop()  # driving over the live view's stream: just stop
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = SingleMotorHapticWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
