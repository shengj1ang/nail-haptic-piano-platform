"""Manual-test UI: click a labeled circle (L5..L1, R1..R5, same layout as
the haptic quiz's on-screen finger cue) to buzz that finger's motor on the
vibration rig; release to stop it. A "Run Full Test" button sweeps every
motor once, left hand pinky-to-thumb then right hand thumb-to-pinky, so a
dead motor can be caught without ten separate clicks.

This is a thin manual-test wrapper, the same role test_virtual_piano_led.py
plays for the LED strip - the finger<->motor wiring lives in
app/haptic_cue.py and the drive amp/frequency come from config.json's
haptic block (both shared with the haptic quiz), so this test exercises
the exact same mapping AND the exact same cue strength guidance mode
delivers. Change them in Initial Setup -> Haptic Actuator Defaults.
"""

import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.gui.cue_window import FINGER_ORDER
from app.haptic_cue import FINGER_TO_MOTOR
from common import haptic_config as hc
from common.controller import VibratorController

BG_COLOR = QColor(24, 25, 29)
IDLE_COLOR = QColor(70, 70, 78)
ACTIVE_COLOR = QColor(255, 140, 0)
TEXT_COLOR = QColor(240, 240, 245)

# Full-test sweep timing: how long each motor stays on, and the silent gap
# after it so consecutive buzzes feel distinct rather than one long blur.
SWEEP_ON_MS = 300
SWEEP_GAP_MS = 150


class HapticButtonsWidget(QWidget):
    """Ten labeled circles (5 per hand), same layout math as
    FingerCueWidget, but clickable: press one to buzz its motor for as
    long as the mouse stays down, release to stop it."""

    finger_pressed = Signal(str)
    finger_released = Signal()

    def __init__(self):
        super().__init__()
        self.active_finger = None
        self._pressed_finger = None
        self.setMinimumSize(320, 160)

    def set_active(self, finger) -> None:
        """Lets the automated sweep highlight the current motor without going through mouse events."""
        self.active_finger = finger
        self.update()

    def _dot_geometry(self):
        """finger_id -> (x, y, diameter), kept in one place so hit-testing
        in mousePressEvent and drawing in paintEvent never drift apart."""
        w, h = self.width(), self.height()
        margin = w * 0.04
        usable_w = w - 2 * margin
        dot_d = min(usable_w / 13.0, h * 0.55)
        gap = (usable_w - 10 * dot_d) / 12.0
        hand_gap = gap * 2.0
        y = h * 0.62 - dot_d / 2.0

        geometry = {}
        x = margin + gap
        for finger_id in FINGER_ORDER:
            geometry[finger_id] = (x, y, dot_d)
            x += dot_d + (hand_gap if finger_id == "L1" else gap)
        return geometry

    def _finger_at(self, pos):
        for finger_id, (x, y, d) in self._dot_geometry().items():
            if x <= pos.x() <= x + d and y <= pos.y() <= y + d:
                return finger_id
        return None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG_COLOR)

        geometry = self._dot_geometry()
        if geometry:
            dot_d = next(iter(geometry.values()))[2]
            dot_font = painter.font()
            dot_font.setPointSizeF(max(dot_d * 0.34, 8))
            dot_font.setBold(True)
            painter.setFont(dot_font)

        for finger_id, (x, y, d) in geometry.items():
            active = finger_id == self.active_finger
            painter.setBrush(ACTIVE_COLOR if active else IDLE_COLOR)
            painter.setPen(QPen(QColor(10, 10, 10), max(d * 0.02, 1.0)))
            painter.drawEllipse(int(x), int(y), int(d), int(d))
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.drawText(int(x), int(y), int(d), int(d), Qt.AlignmentFlag.AlignCenter, finger_id)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        finger = self._finger_at(event.position())
        if finger is None:
            return
        self._pressed_finger = finger
        self.set_active(finger)
        self.finger_pressed.emit(finger)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._pressed_finger is None:
            return
        self._pressed_finger = None
        self.set_active(None)
        self.finger_released.emit()


class HapticTestWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Haptic Vibrator Test")
        self.cfg = cfg if cfg is not None else Config.load()

        # Manual (button-)triggered connection, same convention as the LED
        # controller in test_virtual_piano_led.py - never opened
        # automatically on launch.
        self.controller = VibratorController()
        self.connected = False
        # Refreshed from config.json on every connect (see _toggle_connect).
        self._amp = self.cfg.haptic.active.default_amp

        self.status = QLabel("Vibrator: not connected")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)

        self.buttons = HapticButtonsWidget()
        self.buttons.finger_pressed.connect(self._on_finger_pressed)
        self.buttons.finger_released.connect(self._on_finger_released)

        self.sweep_btn = QPushButton("Run Full Test (L5 → L1, R1 → R5)")
        self.sweep_btn.clicked.connect(self._start_sweep)

        instructions = QLabel(
            "Click and hold a circle to buzz that finger's motor; release to stop.\n"
            "\"Run Full Test\" sweeps every motor once, in order, to catch a dead motor.\n"
            # The cue strength is not a constant of this window: it is
            # whatever Initial Setup -> Haptic Actuator Defaults says, so
            # what you feel here is what the haptic quiz delivers.
            + hc.describe_active()
            + "  (from config.json; change it in Initial Setup → Haptic "
              "Actuator Defaults)"
        )

        top_row = QHBoxLayout()
        top_row.addWidget(self.status, 1)
        top_row.addWidget(self.connect_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top_row)
        layout.addWidget(instructions)
        layout.addWidget(self.buttons, 1)
        layout.addWidget(self.sweep_btn)
        self.setCentralWidget(central)
        self.resize(760, 340)

        self._sweep_timer = QTimer(self)
        self._sweep_timer.setSingleShot(True)
        self._sweep_timer.timeout.connect(self._sweep_step)
        self._sweep_index = 0
        self._sweep_phase = "gap"
        self._sweeping = False

    # ------------------------------------------------------------------
    # Vibrator connection (manual)
    # ------------------------------------------------------------------

    def _toggle_connect(self) -> None:
        if self.connected:
            self._stop_sweep()
            self.controller.stop_all()
            self.controller.close()
            self.connected = False
            self.status.setText("Vibrator: not connected")
            self.connect_btn.setText("Connect")
            return

        try:
            self.controller.connect()
        except Exception as exc:
            QMessageBox.warning(self, "Vibrator connection failed", str(exc))
            self.status.setText(f"Vibrator: not connected ({exc})")
            return

        # Read the configured drive once per connection, and retune each
        # finger port to it - the firmware boots every pin at the LRA's
        # resonance, which an ERM cannot start from.
        config = hc.get_haptic_config()
        self._amp = config.active.default_amp
        for motor in sorted(set(FINGER_TO_MOTOR.values())):
            self.controller.send(f"F {motor} {config.active.default_frequency}")

        self.connected = True
        self.status.setText(
            f"Vibrator: connected on {self.controller.port} - driving "
            f"{hc.summary_line()}")
        self.connect_btn.setText("Disconnect")

    # ------------------------------------------------------------------
    # Single-motor test (click and hold a circle)
    # ------------------------------------------------------------------

    def _on_finger_pressed(self, finger: str) -> None:
        if not self.connected or self._sweeping:
            return
        motor = FINGER_TO_MOTOR.get(finger)
        if motor is None:
            return
        self.controller.send(f"S {1 << motor} {self._amp}")

    def _on_finger_released(self) -> None:
        if not self.connected or self._sweeping:
            return
        self.controller.stop_all()

    # ------------------------------------------------------------------
    # Full test: sweep every motor once, L5 -> L1 then R1 -> R5
    # ------------------------------------------------------------------

    def _start_sweep(self) -> None:
        if not self.connected or self._sweeping:
            return
        self._sweeping = True
        self.sweep_btn.setEnabled(False)
        self.buttons.setEnabled(False)
        self._sweep_index = 0
        self._sweep_phase = "gap"  # so the first step turns a motor on rather than off
        self._sweep_step()

    def _stop_sweep(self) -> None:
        self._sweep_timer.stop()
        if self._sweeping:
            self._sweeping = False
            self.sweep_btn.setEnabled(True)
            self.buttons.setEnabled(True)
            self.buttons.set_active(None)

    def _sweep_step(self) -> None:
        if self._sweep_phase == "on":
            self.controller.stop_all()
            self.buttons.set_active(None)
            self._sweep_phase = "gap"
            self._sweep_timer.start(SWEEP_GAP_MS)
            return

        # FINGER_ORDER is already L5..L1 (left pinky to thumb) then R1..R5
        # (right thumb to pinky) - exactly the sweep order asked for.
        if self._sweep_index >= len(FINGER_ORDER):
            self._sweeping = False
            self.sweep_btn.setEnabled(True)
            self.buttons.setEnabled(True)
            return

        finger = FINGER_ORDER[self._sweep_index]
        self._sweep_index += 1
        motor = FINGER_TO_MOTOR.get(finger)
        self.buttons.set_active(finger)
        if motor is not None:
            self.controller.send(f"S {1 << motor} {self._amp}")
        self._sweep_phase = "on"
        self._sweep_timer.start(SWEEP_ON_MS)

    def closeEvent(self, event) -> None:
        self._stop_sweep()
        if self.connected:
            self.controller.stop_all()
            self.controller.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = HapticTestWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
