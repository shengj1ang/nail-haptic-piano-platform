"""Manual single-motor haptic bench: pick a motor port, dial in a PWM
frequency and drive amp, connect the rig, then press-and-hold one button
to vibrate that motor - release to stop.

A minimal front end over common.controller.VibratorController (the same
manual connect/disconnect convention as test_haptic_vibrator.py's
HapticTestWindow), for quickly feeling one actuator at an arbitrary
frequency/amp - e.g. trying the LRA at 224 Hz vs the ERM at 5 kHz, or
sanity-checking a wiring change - without editing a script or running a
full sweep. It sends the raw firmware commands the sweeps use: `F <port>
<freq>` to set the port's PWM frequency, `S <mask> <amp>` to drive it,
`X` to stop.

Runs standalone (python test_haptic_single_motor.py) or from the
launcher's "Feature Testing" section.
"""

import sys

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from common.controller import VibratorController

# Firmware 'F' command's valid PWM-frequency range and the boot default
# (the LRA's 224 Hz resonance). An ERM needs a kHz-range value instead.
FREQ_MIN_HZ = 50
FREQ_MAX_HZ = 20000
DEFAULT_FREQ_HZ = 224
DEFAULT_AMP = 64          # project-default cue intensity
MAX_MOTOR_INDEX = 15      # firmware motor-port range


class SingleMotorHapticWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Single-Motor Haptic Bench")
        self.cfg = cfg if cfg is not None else Config.load()

        # Manual (button-triggered) connection, never opened on launch -
        # same convention as HapticTestWindow.
        self.controller = VibratorController()
        self.connected = False
        self._vibrating = False

        # --- connection row ---------------------------------------------
        self.status = QLabel("Not connected.")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)
        conn_row = QHBoxLayout()
        conn_row.addWidget(self.status, 1)
        conn_row.addWidget(self.connect_btn)

        # --- parameter row ----------------------------------------------
        self.motor_spin = QSpinBox()
        self.motor_spin.setRange(0, MAX_MOTOR_INDEX)
        self.motor_spin.setValue(0)
        self.motor_spin.setToolTip("Motor port to drive (LRA is wired to 11, "
                                   "ERM to 10; default 0)")

        self.freq_spin = QSpinBox()
        self.freq_spin.setRange(FREQ_MIN_HZ, FREQ_MAX_HZ)
        self.freq_spin.setValue(DEFAULT_FREQ_HZ)
        self.freq_spin.setSuffix(" Hz")
        self.freq_spin.setToolTip("PWM frequency for the port (F command). "
                                  "224 Hz suits the LRA; an ERM needs kHz "
                                  "(e.g. 5000) or it stalls.")

        self.amp_spin = QSpinBox()
        self.amp_spin.setRange(0, 255)
        self.amp_spin.setValue(DEFAULT_AMP)
        self.amp_spin.setToolTip("Drive amplitude (PWM duty, 0-255 scale)")

        param_row = QHBoxLayout()
        param_row.addWidget(QLabel("Motor id:"))
        param_row.addWidget(self.motor_spin)
        param_row.addSpacing(12)
        param_row.addWidget(QLabel("Frequency:"))
        param_row.addWidget(self.freq_spin)
        param_row.addSpacing(12)
        param_row.addWidget(QLabel("Amp:"))
        param_row.addWidget(self.amp_spin)
        param_row.addStretch(1)

        # --- press-and-hold button --------------------------------------
        self.vibrate_btn = QPushButton("Press and hold to vibrate")
        self.vibrate_btn.setMinimumHeight(90)
        self.vibrate_btn.setEnabled(False)  # until connected
        # pressed/released fire on mouse down/up - exactly hold-to-vibrate.
        self.vibrate_btn.pressed.connect(self._on_pressed)
        self.vibrate_btn.released.connect(self._on_released)

        instructions = QLabel(
            "Connect the rig, set the motor id / frequency / amp, then press "
            "and hold the button below to drive that motor; release to stop. "
            "The frequency and amp are applied on each press, so you can "
            "retune between buzzes.")
        instructions.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(conn_row)
        layout.addWidget(instructions)
        layout.addLayout(param_row)
        layout.addWidget(self.vibrate_btn, 1)
        self.setCentralWidget(central)
        self.resize(560, 300)

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
            self.vibrate_btn.setEnabled(False)
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
        self.vibrate_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Press-and-hold vibrate
    # ------------------------------------------------------------------

    def _on_pressed(self) -> None:
        if not self.connected:
            return
        motor = self.motor_spin.value()
        freq = self.freq_spin.value()
        amp = self.amp_spin.value()
        # Apply the frequency to the port, then drive it continuously.
        self.controller.send(f"F {motor} {freq}")
        self.controller.send(f"S {1 << motor} {amp}")
        self._vibrating = True
        self.status.setText(f"Vibrating motor {motor} at {freq} Hz, amp {amp}...")

    def _on_released(self) -> None:
        self._stop()

    def _stop(self) -> None:
        if self.connected and self._vibrating:
            self.controller.stop_all()
            self.status.setText(f"Connected on {self.controller.port}")
        self._vibrating = False

    def closeEvent(self, event) -> None:
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
