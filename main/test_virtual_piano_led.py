"""
Manual-test UI: on-screen piano (PySide6) that mirrors a calibrated
keyboard profile, for eyeballing/measuring the physical LED strip response
without needing to read code.

Press and hold a virtual key -> the matching LED(s) on the real Teensy
WS2812 strips light up and a matching tone plays; release -> both turn back
off. Pressing the real MIDI keyboard does the same, via a live MIDI
connection.

LED and MIDI are both optional, manually-triggered connections (buttons,
not auto-connect-on-launch/on-profile-change) - this is meant to run
against a second piano rig that has no LED strip wired up at all, so the
on-screen piano, audio feedback, and MIDI-driven highlighting all need to
keep working with no LED controller attached.

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
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import profile_led_mapper
from app.config import Config
from app.keyboard.midi_mapping import MidiMapping
from app.midi import list_input_ports
from app.profiles import DATA_DIR, list_profiles
from common.led_controller import LEDArrayController
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import NoteLEDMapper


class KeyFeedback:
    """Combines LED + audio feedback for a note press/release into one
    call, so PianoKey doesn't need to know both exist. audio_player is
    optional - if it failed to open (no output device, etc.) LED feedback
    still works on its own. Likewise, the LED controller may simply not be
    connected (no strip wired up on this rig, or not connected yet) - every
    led_mapper call is guarded, so on-screen + audio feedback keep working
    with no LED hardware present at all."""

    def __init__(self, led_mapper: NoteLEDMapper, audio_player: NoteAudioPlayer | None):
        self.led_mapper = led_mapper
        self.audio_player = audio_player

    def on(self, note: int) -> bool:
        lit = False
        try:
            lit = self.led_mapper.light_key(note)
        except RuntimeError:
            pass  # LED controller not connected
        if self.audio_player is not None:
            self.audio_player.play_key(note)
        return lit

    def off(self, note: int) -> bool:
        cleared = False
        try:
            cleared = self.led_mapper.clear_key(note)
        except RuntimeError:
            pass
        if self.audio_player is not None:
            self.audio_player.stop_key(note)
        return cleared

    def off_all(self) -> None:
        try:
            self.led_mapper.clear_all()
        except RuntimeError:
            pass
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


def _build_visual_sequence(keyboard_profile_name: str):
    """(is_black, note) pairs in physical left-to-right order, for the given profile."""
    white_notes, black_notes = profile_led_mapper.build_ordered_notes(keyboard_profile_name)

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
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Virtual Piano -> LED (manual test)")

        self.cfg = cfg if cfg is not None else Config.load()

        # Both the LED controller and the MIDI port are optional, manually
        # (button-)triggered connections - never opened automatically on
        # launch or on profile change - so this still runs against a piano
        # with no LED strip at all, or before a MIDI cable is plugged in.
        self.led = LEDArrayController()
        self.led_connected = False

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

        self.led_status = QLabel("LED: not connected")
        self.led_connect_btn = QPushButton("Connect LED")
        self.led_connect_btn.clicked.connect(self._toggle_led)

        self.audio_status = QLabel(audio_status_text)
        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0))
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)

        self.midi_port_combo = QComboBox()
        self.midi_refresh_btn = QPushButton("Refresh")
        self.midi_refresh_btn.clicked.connect(self._refresh_midi_ports)
        self.midi_connect_btn = QPushButton("Connect MIDI")
        self.midi_connect_btn.clicked.connect(self._toggle_midi)
        self.midi_status = QLabel("MIDI: not connected")

        self.instructions = QLabel(
            "Click and hold a key to light its LED and play its tone (label = MIDI note); release to "
            "turn both off. Blue = clicked here, orange = pressed on the real keyboard.\n"
            "LED and MIDI are both optional - connect them with the buttons below; the on-screen "
            "piano and audio work fine with neither connected."
        )

        self.midi_bridge = MidiBridge()
        self.midi_bridge.note_event.connect(self._on_midi_note)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        profile_row.addWidget(self.profile_combo, 1)

        led_row = QHBoxLayout()
        led_row.addWidget(self.led_status, 1)
        led_row.addWidget(self.led_connect_btn)

        midi_row = QHBoxLayout()
        midi_row.addWidget(QLabel("MIDI port:"))
        midi_row.addWidget(self.midi_port_combo, 1)
        midi_row.addWidget(self.midi_refresh_btn)
        midi_row.addWidget(self.midi_connect_btn)

        audio_row = QHBoxLayout()
        audio_row.addWidget(self.audio_status, 1)
        audio_row.addWidget(QLabel("Timbre:"))
        audio_row.addWidget(self.timbre_combo)

        self.piano_slot = QVBoxLayout()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(profile_row)
        layout.addLayout(led_row)
        layout.addLayout(audio_row)
        layout.addLayout(midi_row)
        layout.addWidget(self.midi_status)
        layout.addWidget(self.instructions)
        layout.addLayout(self.piano_slot)
        self.setCentralWidget(central)

        self._refresh_midi_ports()
        self._refresh_profiles()

    def _on_timbre_changed(self, index: int) -> None:
        if self.audio_player is not None:
            self.audio_player.set_timbre(self.timbre_combo.itemData(index))

    # ------------------------------------------------------------------
    # LED connection (manual)
    # ------------------------------------------------------------------

    def _toggle_led(self) -> None:
        if self.led_connected:
            if self.mapper is not None:
                try:
                    self.mapper.clear_all()
                except RuntimeError:
                    pass
            self.led.close()
            self.led_connected = False
            self.led_status.setText("LED: not connected")
            self.led_connect_btn.setText("Connect LED")
            return

        try:
            self.led.connect()
            self.led.off()
        except Exception as exc:
            QMessageBox.warning(self, "LED connection failed", str(exc))
            self.led_status.setText(f"LED: not connected ({exc})")
            return

        self.led_connected = True
        self.led_status.setText(f"LED: connected on {self.led.port}")
        self.led_connect_btn.setText("Disconnect LED")

    # ------------------------------------------------------------------
    # MIDI connection (manual)
    # ------------------------------------------------------------------

    def _refresh_midi_ports(self) -> None:
        ports = list_input_ports()
        self.midi_port_combo.blockSignals(True)
        self.midi_port_combo.clear()
        self.midi_port_combo.addItems(ports)
        if self.cfg.midi.port_name in ports:
            self.midi_port_combo.setCurrentText(self.cfg.midi.port_name)
        self.midi_port_combo.blockSignals(False)

    def _toggle_midi(self) -> None:
        if self.midi_port is not None:
            self.midi_port.close()
            self.midi_port = None
            self.midi_status.setText("MIDI: not connected")
            self.midi_connect_btn.setText("Connect MIDI")
            return

        port_name = self.midi_port_combo.currentText()
        if not port_name:
            QMessageBox.warning(self, "No MIDI port selected", "Pick a MIDI port first.")
            return

        try:
            self.midi_port = mido.open_input(port_name, callback=self._midi_callback)
        except Exception as exc:
            print(f"Could not open MIDI port {port_name!r}: {exc}")
            print(f"Available MIDI inputs: {mido.get_input_names()}")
            self.midi_status.setText(f"MIDI: not connected ({exc})")
            return

        self.midi_status.setText(f"MIDI: listening on {port_name}")
        self.midi_connect_btn.setText("Disconnect MIDI")

    def _refresh_profiles(self) -> None:
        profiles = list_profiles(DATA_DIR)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        self.profile_combo.blockSignals(False)

        if not profiles:
            QMessageBox.warning(
                self, "No profiles found",
                "No profiles under data/keyboard-profile/. Run setup_keyboard_wizard.py and "
                "setup_midi_mapping_wizard.py first.",
            )
            return

        target = self.cfg.active_keyboard_profile if self.cfg.active_keyboard_profile in profiles else profiles[0]
        self.profile_combo.setCurrentText(target)
        self._load_profile(target)

    def _load_profile(self, keyboard_profile_name: str) -> None:
        if not keyboard_profile_name:
            return

        try:
            mapper = profile_led_mapper.build_mapper(self.led, keyboard_profile_name)
            visual_sequence = _build_visual_sequence(keyboard_profile_name)
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

        # Just preselect this profile's usual MIDI port in the combo, if it's
        # currently available - connecting is still a manual step (see
        # _toggle_midi()), independent of which profile is loaded.
        mapping = MidiMapping.load(DATA_DIR / keyboard_profile_name / "midi_mapping.json")
        available_ports = [self.midi_port_combo.itemText(i) for i in range(self.midi_port_combo.count())]
        if mapping.port_name and mapping.port_name in available_ports:
            self.midi_port_combo.setCurrentText(mapping.port_name)

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
            try:
                self.mapper.clear_all()
            except RuntimeError:
                pass
        if self.audio_player is not None:
            self.audio_player.stop_all()
            self.audio_player.close()
        self.led.close()  # a no-op if it was never connected
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = PianoWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
