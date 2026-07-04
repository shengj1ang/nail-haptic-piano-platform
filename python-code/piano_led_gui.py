"""
On-screen piano (PySide6) that mirrors the physical 25-key MIDI keyboard
used elsewhere in this project.

Press and hold a virtual key -> the matching LED(s) on the real Teensy
WS2812 strips light up (via note_led_map.py, keyed by key_id). Release the
key -> the LED(s) turn back off. Useful for eyeballing/measuring LED
response without needing the physical MIDI keyboard.

The left-to-right white/black key order below (VISUAL_SEQUENCE) is exactly
as described from the physical keyboard, not derived from MIDI note math -
key_id is the only thing that matters here.
"""

import sys

import mido
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from common.led_controller import LEDArrayController
from note_led_map import NOTE_TO_KEY_ID, NoteLEDMapper

# Physical MIDI keyboard input port (see config.json / test-script/MIDI.py).
MIDI_PORT_NAME = "SE25 MIDI1"

# ---- key sizes ----
WHITE_W = 44
WHITE_H = 180
BLACK_W = 28
BLACK_H = 110

WHITE_IDLE = QColor("white")
BLACK_IDLE = QColor(30, 30, 30)

# Mouse-click highlight (blue) vs real MIDI-keyboard highlight (orange) - kept
# visually distinct so you can tell which input triggered a key.
MOUSE_PRESSED = QColor(90, 170, 255)
MIDI_PRESSED = QColor(255, 140, 0)

# Physical left-to-right key order, as described from the real keyboard:
# W1 B1 W2 B2 W3 W4 B3 W5 B4 W6 B5 W7 W8 B6 W9 B7 W10 W11 B8 W12 B9 W13 B10 W14 W15
# White Wn -> key_id n-1 (0-14), Black Bn -> key_id n+14 (15-24).
VISUAL_SEQUENCE = [
    (False, 0),   # 白1
    (True, 15),   # 黑1
    (False, 1),   # 白2
    (True, 16),   # 黑2
    (False, 2),   # 白3
    (False, 3),   # 白4
    (True, 17),   # 黑3
    (False, 4),   # 白5
    (True, 18),   # 黑4
    (False, 5),   # 白6
    (True, 19),   # 黑5
    (False, 6),   # 白7
    (False, 7),   # 白8
    (True, 20),   # 黑6
    (False, 8),   # 白9
    (True, 21),   # 黑7
    (False, 9),   # 白10
    (False, 10),  # 白11
    (True, 22),   # 黑8
    (False, 11),  # 白12
    (True, 23),   # 黑9
    (False, 12),  # 白13
    (True, 24),   # 黑10
    (False, 13),  # 白14
    (False, 14),  # 白15
]


class PianoKey(QWidget):
    """
    One clickable piano key. Lights/clears its LED(s) on press/release.

    Tracks mouse-press and real-MIDI-press independently, so either input
    (or both at once) is reflected without one clobbering the other's state.
    """

    def __init__(self, parent: QWidget, key_id: int, is_black: bool, mapper: NoteLEDMapper):
        super().__init__(parent)
        self.key_id = key_id
        self.is_black = is_black
        self.mapper = mapper
        self.mouse_pressed = False
        self.midi_active = False
        self.setFixedSize(BLACK_W if is_black else WHITE_W, BLACK_H if is_black else WHITE_H)

    def _current_color(self):
        idle = BLACK_IDLE if self.is_black else WHITE_IDLE
        if self.midi_active:
            return MIDI_PRESSED
        if self.mouse_pressed:
            return MOUSE_PRESSED
        return idle

    def paintEvent(self, event):
        painter = QPainter(self)
        text_color = QColor(220, 220, 220) if self.is_black else QColor(130, 130, 130)
        painter.fillRect(self.rect(), self._current_color())
        painter.setPen(QPen(QColor(60, 60, 60)))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setPen(QPen(text_color))
        painter.drawText(
            self.rect().adjusted(0, 0, 0, -8),
            Qt.AlignBottom | Qt.AlignHCenter,
            str(self.key_id),
        )

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.mouse_pressed = True
        self.update()
        if not self.mapper.light_key(self.key_id):
            print(f"key_id {self.key_id} has no LED mapping")

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.mouse_pressed = False
        self.update()
        if not self.midi_active:
            self.mapper.clear_key(self.key_id)

    def set_midi_active(self, active: bool):
        """Called from the MIDI listener when the real key is pressed/released."""
        if active == self.midi_active:
            return
        self.midi_active = active
        self.update()
        if active:
            if not self.mapper.light_key(self.key_id):
                print(f"key_id {self.key_id} has no LED mapping")
        elif not self.mouse_pressed:
            self.mapper.clear_key(self.key_id)


class PianoWidget(QWidget):
    """Lays out the 15 white + 10 black keys following VISUAL_SEQUENCE."""

    def __init__(self, mapper: NoteLEDMapper):
        super().__init__()

        self.keys_by_id = {}  # key_id -> PianoKey, so external (MIDI) events can find a key

        num_white = sum(1 for is_black, _ in VISUAL_SEQUENCE if not is_black)
        self.setFixedSize(num_white * WHITE_W, WHITE_H)

        # First pass: compute where every key goes, without creating widgets yet.
        white_positions = []  # (key_id, x)
        black_positions = []  # (key_id, x)
        white_index = 0
        for is_black, key_id in VISUAL_SEQUENCE:
            if not is_black:
                white_positions.append((key_id, white_index * WHITE_W))
                white_index += 1
            else:
                # Sits on the boundary between the white key just placed and the next one.
                black_positions.append((key_id, white_index * WHITE_W - BLACK_W // 2))

        # Create all white keys first, then all black keys on top of them - Qt
        # stacks later-created siblings above earlier ones, so creating a white
        # key after a black key (as the old interleaved loop did) covered half
        # of that black key.
        for key_id, x in white_positions:
            key = PianoKey(self, key_id, is_black=False, mapper=mapper)
            key.move(x, 0)
            self.keys_by_id[key_id] = key

        for key_id, x in black_positions:
            key = PianoKey(self, key_id, is_black=True, mapper=mapper)
            key.move(x, 0)
            key.raise_()
            self.keys_by_id[key_id] = key


class MidiBridge(QObject):
    """
    Relays note on/off events from mido's background listener thread to the
    Qt main thread. mido's callback runs off-thread, and Qt widgets may only
    be touched from the thread that owns them - emitting a signal here is
    thread-safe and gets auto-queued onto the main thread's event loop.
    """

    note_event = Signal(int, bool)  # (midi_note, is_on)


class PianoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Virtual Piano -> LED")

        self.led = LEDArrayController()
        try:
            self.led.connect()
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", f"Could not connect to the LED controller:\n{exc}")
            raise SystemExit(1)

        self.mapper = NoteLEDMapper(self.led)
        self.led.off()

        self.piano = PianoWidget(self.mapper)

        self.midi_bridge = MidiBridge()
        self.midi_bridge.note_event.connect(self._on_midi_note)
        self.midi_port = None
        midi_status = self._connect_midi()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel(f"Connected: {self.led.port}"))
        layout.addWidget(QLabel(midi_status))
        layout.addWidget(QLabel(
            "Click and hold a key to light its LED (label = key_id); release to turn it off. "
            "Blue = clicked here, orange = pressed on the real keyboard."
        ))
        layout.addWidget(self.piano)
        self.setCentralWidget(central)

    def _connect_midi(self) -> str:
        """Open the physical MIDI keyboard's input port. Non-fatal if it fails -
        the virtual piano still works standalone via mouse clicks."""
        try:
            self.midi_port = mido.open_input(MIDI_PORT_NAME, callback=self._midi_callback)
            return f"MIDI: listening on {MIDI_PORT_NAME}"
        except Exception as exc:
            print(f"Could not open MIDI port {MIDI_PORT_NAME!r}: {exc}")
            print(f"Available MIDI inputs: {mido.get_input_names()}")
            return f"MIDI: not connected ({exc})"

    def _midi_callback(self, msg):
        """Runs on mido's background listener thread - do nothing here except
        hand off to the Qt main thread via the signal."""
        if msg.type == "note_on":
            self.midi_bridge.note_event.emit(msg.note, msg.velocity > 0)
        elif msg.type == "note_off":
            self.midi_bridge.note_event.emit(msg.note, False)

    def _on_midi_note(self, note: int, is_on: bool):
        key_id = NOTE_TO_KEY_ID.get(note)
        if key_id is None:
            print(f"MIDI note {note} has no key_id mapping")
            return
        key = self.piano.keys_by_id.get(key_id)
        if key is not None:
            key.set_midi_active(is_on)

    def closeEvent(self, event):
        if self.midi_port is not None:
            self.midi_port.close()
        self.mapper.clear_all()
        self.led.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = PianoWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
