"""Manual single-motor haptic bench: pick a motor port, dial in a PWM
frequency and drive amp with draggable sliders, choose a run duration and
a vibration pattern, then start/stop the motor.

A minimal front end over common.controller.VibratorController (the same
manual connect/disconnect convention as test_haptic_vibrator.py's
HapticTestWindow), for quickly feeling one actuator at an arbitrary
frequency/amp - e.g. trying the LRA at 224 Hz vs the ERM at 5 kHz, or
sanity-checking a wiring change - without editing a script or running a
full sweep. It sends the raw firmware commands the sweeps use: `F <port>
<freq>` to set the port's PWM frequency, `S <mask> <amp>` to drive it,
`X` to stop.

The motor id, frequency and amp are pushed to the device live: while a
motor is energised, dragging a slider or changing the port re-tunes the
running motor immediately, so you can feel the change without stopping.

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
from common.controller import VibratorController

# Firmware 'F' command PWM-frequency slider ranges and the boot default
# (the LRA's 224 Hz resonance). "High precision" trades reach for a finer
# step by mapping the full slider travel onto the low band; the normal
# band reaches the kHz values an ERM needs.
FREQ_MAX_HIGH = 1000      # high-precision slider max (Hz)
FREQ_MAX_NORMAL = 5000    # normal slider max (Hz)
DEFAULT_FREQ_HZ = 224
DEFAULT_AMP = 64          # project-default cue intensity
AMP_MAX = 255
MAX_MOTOR_INDEX = 11      # motor-port range exposed here (LRA=11, ERM=10)

# Vibrate + pause loop defaults.
DEFAULT_ON_MS = 500
DEFAULT_OFF_MS = 500


class SingleMotorHapticWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Single-Motor Haptic Bench")
        self.cfg = cfg if cfg is not None else Config.load()

        # Manual (button-triggered) connection, never opened on launch -
        # same convention as HapticTestWindow.
        self.controller = VibratorController()
        self.connected = False

        # Run state.
        self._running = False   # a start/stop session is active
        self._on_now = False    # the motor is energised right now
        self._driven_motor = 0  # motor currently energised (to stop on switch)

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
        conn_row = QHBoxLayout()
        conn_row.addWidget(self.status, 1)
        conn_row.addWidget(self.connect_btn)

        # --- parameters --------------------------------------------------
        # Motor id: a selection box over the exposed port range.
        self.motor_combo = QComboBox()
        for i in range(MAX_MOTOR_INDEX + 1):
            self.motor_combo.addItem(str(i), i)
        self.motor_combo.setToolTip("Motor port to drive (LRA is wired to 11, "
                                    "ERM to 10; default 0)")
        self.motor_combo.currentIndexChanged.connect(self._on_motor_changed)

        # Frequency: draggable slider + a high-precision toggle that swaps
        # the slider's range.
        self.freq_hi_check = QCheckBox("High precision (0-1000 Hz)")
        self.freq_hi_check.setToolTip(
            "Map the whole slider onto 0-1000 Hz for a finer step near the "
            "LRA resonance. Unchecked reaches 0-5000 Hz for ERM use.")
        self.freq_hi_check.toggled.connect(self._on_freq_range_toggled)

        self.freq_slider = QSlider(Qt.Horizontal)
        self.freq_slider.setRange(0, FREQ_MAX_NORMAL)
        self.freq_slider.setValue(DEFAULT_FREQ_HZ)
        self.freq_slider.setToolTip("PWM frequency for the port (F command). "
                                    "224 Hz suits the LRA; an ERM needs kHz "
                                    "(e.g. 5000) or it stalls.")
        self.freq_value = QLabel()
        self.freq_slider.valueChanged.connect(self._on_freq_changed)

        # Amp: draggable slider (PWM duty, 0-255 scale).
        self.amp_slider = QSlider(Qt.Horizontal)
        self.amp_slider.setRange(0, AMP_MAX)
        self.amp_slider.setValue(DEFAULT_AMP)
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
        grid.addWidget(QLabel("Motor id:"), 0, 0)
        grid.addWidget(self.motor_combo, 0, 1)

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

        instructions = QLabel(
            "Connect the rig, pick the motor id and drag the frequency / amp "
            "sliders, choose a duration and pattern, then press Start. The "
            "motor id, frequency and amp are pushed live, so you can re-tune "
            "while it runs; press Stop (or wait out the duration) to end.")
        instructions.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(conn_row)
        layout.addWidget(instructions)
        layout.addLayout(grid)
        layout.addWidget(self.run_btn)
        layout.addStretch(1)
        self.setCentralWidget(central)
        self.resize(620, 380)

        # Initial label / enabled-state sync.
        self._refresh_freq_label()
        self._refresh_amp_label()
        self._on_mode_changed()

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _motor(self) -> int:
        return self.motor_combo.currentData()

    def _refresh_freq_label(self) -> None:
        self.freq_value.setText(f"{self.freq_slider.value()} Hz")

    def _refresh_amp_label(self) -> None:
        self.amp_value.setText(str(self.amp_slider.value()))

    # ------------------------------------------------------------------
    # Connection (manual toggle)
    # ------------------------------------------------------------------

    def _toggle_connect(self) -> None:
        if self.connected:
            self._stop()  # never leave a motor running across a disconnect
            self.controller.stop_all()
            self.controller.close()
            self.connected = False
            self.status.setText("Not connected.")
            self.connect_btn.setText("Connect")
            self.run_btn.setEnabled(False)
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
        self.run_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def _toggle_run(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if not self.connected or self._running:
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
        if self.connected and self._on_now:
            self.controller.stop_all()
        self._on_now = False
        self._running = False
        self.run_btn.setText("Start")
        if self.connected:
            self.status.setText(f"Connected on {self.controller.port}")

    # ------------------------------------------------------------------
    # Driving
    # ------------------------------------------------------------------

    def _drive_on(self) -> None:
        """Energise the current motor at the current freq / amp."""
        motor = self._motor()
        freq = self.freq_slider.value()
        amp = self.amp_slider.value()
        self.controller.send(f"F {motor} {freq}")
        self.controller.send(f"S {1 << motor} {amp}")
        self._driven_motor = motor
        self._on_now = True

    def _drive_off(self) -> None:
        self.controller.stop_all()
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
        motor = self._motor()
        freq = self.freq_slider.value()
        amp = self.amp_slider.value()
        if self._on_now:
            self.status.setText(
                f"Vibrating motor {motor} at {freq} Hz, amp {amp}...")
        else:
            self.status.setText(f"Motor {motor} paused (loop)...")

    # ------------------------------------------------------------------
    # Live parameter changes (pushed while running)
    # ------------------------------------------------------------------

    def _on_motor_changed(self) -> None:
        if self._running and self._on_now:
            # Stop whatever port is energised, then drive the new one.
            self.controller.stop_all()
            self._drive_on()
            self._refresh_status()

    def _on_freq_changed(self) -> None:
        self._refresh_freq_label()
        if self._running and self._on_now:
            motor = self._motor()
            self.controller.send(f"F {motor} {self.freq_slider.value()}")
            self.controller.send(f"S {1 << motor} {self.amp_slider.value()}")
            self._refresh_status()

    def _on_amp_changed(self) -> None:
        self._refresh_amp_label()
        if self._running and self._on_now:
            self.controller.send(
                f"S {1 << self._motor()} {self.amp_slider.value()}")
            self._refresh_status()

    def _on_freq_range_toggled(self, high: bool) -> None:
        new_max = FREQ_MAX_HIGH if high else FREQ_MAX_NORMAL
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
        if self.connected:
            self.controller.stop_all()
            self.controller.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = SingleMotorHapticWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
