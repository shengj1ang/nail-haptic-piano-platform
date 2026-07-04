"""
Manual-test UI: on-screen piano (PySide6) that mirrors a calibrated
keyboard profile, for eyeballing/measuring the physical LED strip response
without needing to read code.

Press and hold a virtual key -> the matching LED(s) on the real Teensy
WS2812 strips light up and a matching tone plays; release -> both turn back
off. Pressing the real MIDI keyboard does the same, via a live MIDI
connection.

This file is just a thin PySide6 wrapper for manual testing - the actual
reusable "profile + MIDI -> light the right LED" logic lives in
profile_led_mapper.py and note_led_map.py, and "MIDI note -> tone" lives in
note_audio.py, all with no GUI dependency, so other scripts can import those
directly instead of this one.
"""

import sys

import mido
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

import profile_led_mapper
from app.config import Config
from app.keyboard.midi_mapping import MidiMapping
from app.profiles import DATA_DIR, list_profiles
from common.led_controller import LEDArrayController
from note_audio import NoteAudioPlayer
from note_led_map import NoteLEDMapper


class KeyFeedback:
    """Combines LED + audio feedback for a note press/release into one
    call, so PianoKey doesn't need to know both exist. audio_player is
    optional - if it failed to open (no output device, etc.) LED feedback
    still works on its own."""

    def __init__(self, led_mapper: NoteLEDMapper, audio_player: NoteAudioPlayer | None):
        self.led_mapper = led_mapper
        self.audio_player = audio_player

    def on(self, note: int) -> bool:
        lit = self.led_mapper.light_key(note)
        if self.audio_player is not None:
            self.audio_player.play_key(note)
        return lit

    def off(self, note: int) -> bool:
        cleared = self.led_mapper.clear_key(note)
        if self.audio_player is not None:
            self.audio_player.stop_key(note)
        return cleared

    def off_all(self) -> None:
        self.led_mapper.clear_all()
        if self.audio_player is not None:
            self.audio_player.stop_all()

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

# Physical left-to-right key shape for a standard 25-key keyboard (True =
# black key) - a fixed fact about the shape of any such keyboard, unrelated
# to which profile/notes are loaded: W1 B1 W2 B2 W3 W4 B3 W5 B4 W6 B5 W7 W8
# B6 W9 B7 W10 W11 B8 W12 B9 W13 B10 W14 W15.
KEY_SHAPE_SEQUENCE = [
    False, True, False, True, False, False, True, False, True, False, True,
    False, False, True, False, True, False, False, True, False, True, False,
    True, False, False,
]


def _build_visual_sequence(profile_name: str):
    """(is_black, note) pairs in physical left-to-right order, for the given profile."""
    white_notes, black_notes = profile_led_mapper.build_ordered_notes(profile_name)

    sequence = []
    white_i = black_i = 0
    for is_black in KEY_SHAPE_SEQUENCE:
        if is_black:
            sequence.append((True, black_notes[black_i]))
            black_i += 1
        else:
            sequence.append((False, white_notes[white_i]))
            white_i += 1
    return sequence


class PianoKey(QWidget):
    """
    One clickable piano key. Triggers LED + audio feedback on press/release.

    Tracks mouse-press and real-MIDI-press independently, so either input
    (or both at once) is reflected without one clobbering the other's state.
    """

    def __init__(self, parent: QWidget, note: int | None, is_black: bool, feedback: KeyFeedback):
        super().__init__(parent)
        self.note = note
        self.is_black = is_black
        self.feedback = feedback
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
            str(self.note) if self.note is not None else "?",
        )

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self.note is None:
            return
        self.mouse_pressed = True
        self.update()
        if not self.feedback.on(self.note):
            print(f"note {self.note} has no LED mapping")

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.note is None:
            return
        self.mouse_pressed = False
        self.update()
        if not self.midi_active:
            self.feedback.off(self.note)

    def set_midi_active(self, active: bool):
        """Called from the MIDI listener when the real key is pressed/released."""
        if self.note is None or active == self.midi_active:
            return
        self.midi_active = active
        self.update()
        if active:
            if not self.feedback.on(self.note):
                print(f"note {self.note} has no LED mapping")
        elif not self.mouse_pressed:
            self.feedback.off(self.note)


class PianoWidget(QWidget):
    """Lays out the 15 white + 10 black keys following a (is_black, note) sequence."""

    def __init__(self, feedback: KeyFeedback, visual_sequence):
        super().__init__()

        self.keys_by_note = {}  # MIDI note -> PianoKey, so external (MIDI) events can find a key

        num_white = sum(1 for is_black, _ in visual_sequence if not is_black)
        self.setFixedSize(num_white * WHITE_W, WHITE_H)

        # First pass: compute where every key goes, without creating widgets yet.
        white_positions = []  # (note, x)
        black_positions = []  # (note, x)
        white_index = 0
        for is_black, note in visual_sequence:
            if not is_black:
                white_positions.append((note, white_index * WHITE_W))
                white_index += 1
            else:
                # Sits on the boundary between the white key just placed and the next one.
                black_positions.append((note, white_index * WHITE_W - BLACK_W // 2))

        # Create all white keys first, then all black keys on top of them - Qt
        # stacks later-created siblings above earlier ones, so creating a white
        # key after a black key (as an interleaved loop would) covered half of
        # that black key.
        for note, x in white_positions:
            key = PianoKey(self, note, is_black=False, feedback=feedback)
            key.move(x, 0)
            if note is not None:
                self.keys_by_note[note] = key

        for note, x in black_positions:
            key = PianoKey(self, note, is_black=True, feedback=feedback)
            key.move(x, 0)
            key.raise_()
            if note is not None:
                self.keys_by_note[note] = key


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
        self.setWindowTitle("Virtual Piano -> LED (manual test)")

        self.cfg = Config.load()
        self.led = LEDArrayController()
        try:
            self.led.connect()
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", f"Could not connect to the LED controller:\n{exc}")
            raise SystemExit(1)
        self.led.off()

        self.audio_player: NoteAudioPlayer | None = None
        try:
            self.audio_player = NoteAudioPlayer()
            audio_status_text = "Audio: ready"
        except Exception as exc:
            print(f"Could not open an audio output device: {exc}")
            audio_status_text = f"Audio: not available ({exc})"

        self.midi_port = None
        self.piano: PianoWidget | None = None
        self.mapper: NoteLEDMapper | None = None

        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._load_profile)

        self.led_status = QLabel(f"LED: connected on {self.led.port}")
        self.audio_status = QLabel(audio_status_text)
        self.midi_status = QLabel("MIDI: not connected")
        self.instructions = QLabel(
            "Click and hold a key to light its LED and play its tone (label = MIDI note); release to "
            "turn both off. Blue = clicked here, orange = pressed on the real keyboard."
        )

        self.midi_bridge = MidiBridge()
        self.midi_bridge.note_event.connect(self._on_midi_note)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        profile_row.addWidget(self.profile_combo, 1)

        self.piano_slot = QVBoxLayout()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(profile_row)
        layout.addWidget(self.led_status)
        layout.addWidget(self.audio_status)
        layout.addWidget(self.midi_status)
        layout.addWidget(self.instructions)
        layout.addLayout(self.piano_slot)
        self.setCentralWidget(central)

        self._refresh_profiles()

    def _refresh_profiles(self) -> None:
        profiles = list_profiles(DATA_DIR)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        self.profile_combo.blockSignals(False)

        if not profiles:
            QMessageBox.warning(
                self, "No profiles found",
                "No profiles under data/keyboard-profile/. Run step1_keyboard_wizard.py and "
                "step2_midi_mapping.py first.",
            )
            return

        target = self.cfg.active_profile if self.cfg.active_profile in profiles else profiles[0]
        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, profile_name: str) -> None:
        if not profile_name:
            return

        try:
            mapper = profile_led_mapper.build_mapper(self.led, profile_name)
            visual_sequence = _build_visual_sequence(profile_name)
        except (FileNotFoundError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't load profile", str(exc))
            return

        if self.piano is not None:
            self.piano.setParent(None)
            self.piano.deleteLater()

        self.mapper = mapper
        feedback = KeyFeedback(self.mapper, self.audio_player)
        self.piano = PianoWidget(feedback, visual_sequence)
        self.piano_slot.addWidget(self.piano)

        self._connect_midi(profile_name)

    def _connect_midi(self, profile_name: str) -> None:
        if self.midi_port is not None:
            self.midi_port.close()
            self.midi_port = None

        mapping = MidiMapping.load(DATA_DIR / profile_name / "midi_mapping.json")
        port_name = mapping.port_name
        if not port_name:
            self.midi_status.setText("MIDI: not connected (profile has no port_name)")
            return

        try:
            self.midi_port = mido.open_input(port_name, callback=self._midi_callback)
            self.midi_status.setText(f"MIDI: listening on {port_name}")
        except Exception as exc:
            print(f"Could not open MIDI port {port_name!r}: {exc}")
            print(f"Available MIDI inputs: {mido.get_input_names()}")
            self.midi_status.setText(f"MIDI: not connected ({exc})")

    def _midi_callback(self, msg):
        """Runs on mido's background listener thread - do nothing here except
        hand off to the Qt main thread via the signal."""
        if msg.type == "note_on":
            self.midi_bridge.note_event.emit(msg.note, msg.velocity > 0)
        elif msg.type == "note_off":
            self.midi_bridge.note_event.emit(msg.note, False)

    def _on_midi_note(self, note: int, is_on: bool):
        if self.piano is None:
            return
        key = self.piano.keys_by_note.get(note)
        if key is None:
            print(f"MIDI note {note} has no on-screen key")
            return
        key.set_midi_active(is_on)

    def closeEvent(self, event):
        if self.midi_port is not None:
            self.midi_port.close()
        if self.mapper is not None:
            self.mapper.clear_all()
        if self.audio_player is not None:
            self.audio_player.stop_all()
            self.audio_player.close()
        self.led.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = PianoWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
