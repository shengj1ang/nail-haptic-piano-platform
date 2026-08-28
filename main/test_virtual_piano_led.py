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
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
from app.midi import MidiInputReader, list_input_ports
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


class LatchMode:
    """The 'keep keys lit on click' toggle, shared by reference.

    One instance lives on the window and is handed to every PianoKey, so
    flipping it takes effect immediately on all keys and, because the same
    object is passed in again each time, survives the piano being rebuilt on
    a profile change (which discards and recreates every key)."""

    def __init__(self):
        self.enabled = False


class PianoKey(QWidget):
    """
    One clickable piano key. Triggers LED + audio feedback on press/release.

    Tracks mouse-press and real-MIDI-press independently, so either input
    (or both at once) is reflected without one clobbering the other's state.

    With the shared LatchMode enabled, a mouse click instead *toggles* the
    key: the first click lights it and leaves it lit (latched), the next
    click turns it off. A real MIDI press is always momentary - latching is
    a mouse-only convenience for inspecting the LED strip hands-free.
    """

    def __init__(self, parent: QWidget, note: int | None, is_black: bool,
                 feedback: KeyFeedback, latch: "LatchMode | None" = None):
        super().__init__(parent)
        self.note = note
        self.is_black = is_black
        self.feedback = feedback
        # Optional so the other consumers of this key (music_playback,
        # rhythm_playback) need no change: with no shared latch they get a
        # private, always-off one, i.e. the original momentary behaviour.
        self.latch = latch if latch is not None else LatchMode()
        self.mouse_pressed = False
        self.midi_active = False
        # Stays lit after a click while LatchMode is enabled, until clicked
        # again or the latch is turned off.
        self.latched = False
        # Held on continuously by the "backlight always on" control - lit with
        # no click at all until it is turned off. Independent of the click,
        # latch and MIDI states, so it never fights them: the key's LED is on
        # if ANY of the four want it on.
        self.always_on = False
        self.setFixedSize(BLACK_W if is_black else WHITE_W, BLACK_H if is_black else WHITE_H)

    def _current_color(self):
        idle = BLACK_IDLE if self.is_black else WHITE_IDLE
        if self.midi_active:
            return MIDI_PRESSED
        # A latched key is a mouse-driven state, so it wears the same blue as
        # a held click; an always-on key is lit the same way.
        if self.mouse_pressed or self.latched or self.always_on:
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
        if self.latch.enabled:
            # Toggle: click on -> stays lit, click again -> off. The
            # feedback is only turned off if a real MIDI press is not also
            # holding this key.
            self.latched = not self.latched
            self.update()
            if self.latched:
                if not self.feedback.on(self.note):
                    print(f"note {self.note} has no LED mapping")
            elif not self.midi_active and not self.always_on:
                self.feedback.off(self.note)
            return
        self.mouse_pressed = True
        self.update()
        if not self.feedback.on(self.note):
            print(f"note {self.note} has no LED mapping")

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.note is None:
            return
        # In latch mode the state was decided on press and the key holds it;
        # releasing the mouse must not turn it back off.
        if self.latch.enabled:
            return
        self.mouse_pressed = False
        self.update()
        if not self.midi_active and not self.always_on:
            self.feedback.off(self.note)

    def clear_latch(self):
        """Drop a latched key back to idle - called when the latch toggle is
        switched off, so no key is left stuck lit with no way to release it in
        momentary mode."""
        if not self.latched:
            return
        self.latched = False
        self.update()
        if not self.midi_active and not self.always_on:
            self.feedback.off(self.note)

    def set_always_on(self, on: bool):
        """Light this key's LED continuously (or stop), no click needed - drives
        the 'backlight always on' control. Independent of the click/latch/MIDI
        states: turning it off only clears the LED if none of those are still
        holding the key lit."""
        if self.note is None or on == self.always_on:
            return
        self.always_on = on
        self.update()
        if on:
            if not self.feedback.on(self.note):
                print(f"note {self.note} has no LED mapping")
        elif not self.mouse_pressed and not self.latched and not self.midi_active:
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
        elif not self.mouse_pressed and not self.latched and not self.always_on:
            # A latched / always-on key stays lit after the real key released.
            self.feedback.off(self.note)


class PianoWidget(QWidget):
    """Lays out the 15 white + 10 black keys following a (is_black, note) sequence."""

    def __init__(self, feedback: KeyFeedback, visual_sequence, latch: "LatchMode | None" = None):
        super().__init__()

        self.latch = latch
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
            key = PianoKey(self, note, is_black=False, feedback=feedback, latch=latch)
            key.move(x, 0)
            if note is not None:
                self.keys_by_note[note] = key

        for note, x in black_positions:
            key = PianoKey(self, note, is_black=True, feedback=feedback, latch=latch)
            key.move(x, 0)
            key.raise_()
            if note is not None:
                self.keys_by_note[note] = key

    def clear_latched(self):
        """Release every latched key - used when the latch toggle is turned
        off so nothing is left stuck lit."""
        for key in self.keys_by_note.values():
            key.clear_latch()


class MidiBridge(QObject):
    """
    Relays note on/off events from the MIDI polling thread to the Qt main
    thread. That loop runs off-thread, and Qt widgets may only be touched
    from the thread that owns them - emitting a signal here is thread-safe
    and gets auto-queued onto the main thread's event loop.
    """

    note_event = Signal(int, bool)  # (midi_note, is_on)


class PianoWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None, *, demo_always_on: bool = False):
        super().__init__()
        self.setWindowTitle("Virtual Piano -> LED (manual test)")

        self.cfg = cfg if cfg is not None else Config.load()

        # Demo Mode opens this window to photograph one backlight LED lit
        # steadily: with this set, the first profile load ticks "backlight
        # always on" and pre-selects a key so a light shows with no interaction.
        self._demo_always_on = demo_always_on
        self._demo_default_applied = False
        # MIDI note currently held on by the "backlight always on" control, or
        # None. Kept so a profile change / LED (re)connect can re-light it.
        self._always_on_note: int | None = None

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

        self.midi_reader: MidiInputReader | None = None
        self._midi_thread: threading.Thread | None = None
        self._midi_running = False
        self.piano: PianoWidget | None = None
        self.mapper: NoteLEDMapper | None = None
        # Shared with every key: off by default (momentary click, as before);
        # on means a click latches the key lit until clicked again.
        self.latch = LatchMode()

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
            "Tick \"Latch keys lit\" to make a click keep a key lit until you click it again.\n"
            "Tick \"Backlight always on\" and pick a key to light one LED steadily with no click at all.\n"
            "LED and MIDI are both optional - connect them with the buttons below; the on-screen "
            "piano and audio work fine with neither connected."
        )

        self.latch_check = QCheckBox("Latch keys lit (click toggles on/off)")
        self.latch_check.setToolTip(
            "On: clicking a key toggles it and it stays lit until you click it again.\n"
            "Off: a key lights only while you press and hold it (the default)."
        )
        self.latch_check.toggled.connect(self._on_latch_toggled)

        # "Backlight always on": light one chosen key's LED steadily with no
        # click at all - for photographing / inspecting a single backlight. The
        # key combo is filled per profile (physical left-to-right) in _load_profile.
        self.always_on_check = QCheckBox("Backlight always on (light a key with no click)")
        self.always_on_check.setToolTip(
            "On: the key picked on the right is lit continuously, no click needed.\n"
            "Connect the LED to light the real keyboard; the on-screen key lights either way."
        )
        self.always_on_check.toggled.connect(self._apply_always_on)
        self.always_on_combo = QComboBox()
        self.always_on_combo.currentIndexChanged.connect(self._apply_always_on)

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

        backlight_row = QHBoxLayout()
        backlight_row.addWidget(self.always_on_check)
        backlight_row.addWidget(QLabel("Key:"))
        backlight_row.addWidget(self.always_on_combo, 1)

        self.piano_slot = QVBoxLayout()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(profile_row)
        layout.addLayout(led_row)
        layout.addLayout(audio_row)
        layout.addLayout(midi_row)
        layout.addWidget(self.midi_status)
        layout.addWidget(self.instructions)
        layout.addWidget(self.latch_check)
        layout.addLayout(backlight_row)
        layout.addLayout(self.piano_slot)
        self.setCentralWidget(central)

        self._refresh_midi_ports()
        self._refresh_profiles()

    def _on_timbre_changed(self, index: int) -> None:
        if self.audio_player is not None:
            self.audio_player.set_timbre(self.timbre_combo.itemData(index))

    def _on_latch_toggled(self, checked: bool) -> None:
        """Turn the latch on/off. Turning it off releases every key that was
        latched lit, so switching back to momentary mode never leaves a key
        stuck on."""
        self.latch.enabled = checked
        if not checked and self.piano is not None:
            self.piano.clear_latched()

    # ------------------------------------------------------------------
    # Backlight always on (light one key steadily, no click)
    # ------------------------------------------------------------------

    def _populate_backlight_keys(self, visual_sequence) -> None:
        """Refill the key picker for the loaded profile: one entry per playable
        key, labelled by physical left-to-right position, keeping the previous
        pick if that key still exists. Then (re-)apply the always-on light."""
        prev_note = self.always_on_combo.currentData()

        self.always_on_combo.blockSignals(True)
        self.always_on_combo.clear()
        self.always_on_combo.addItem("None", None)
        white_i = black_i = 0
        for is_black, note in visual_sequence:
            if is_black:
                black_i += 1
                label = f"Black {black_i}"
            else:
                white_i += 1
                label = f"White {white_i}"
            if note is not None:
                self.always_on_combo.addItem(f"{label}  (note {note})", note)

        # Restore the previous key if it survived the profile change; otherwise,
        # the first time a Demo Mode window loads, pick a middle key so a light
        # shows with no interaction.
        target_idx = self.always_on_combo.findData(prev_note) if prev_note is not None else -1
        if target_idx < 0 and self._demo_always_on and not self._demo_default_applied:
            self._demo_default_applied = True
            self.always_on_check.setChecked(True)
            target_idx = self.always_on_combo.findText("White 8", Qt.MatchStartsWith)
            if target_idx < 0:
                target_idx = 1 if self.always_on_combo.count() > 1 else 0
        self.always_on_combo.setCurrentIndex(max(target_idx, 0))
        self.always_on_combo.blockSignals(False)

        self._apply_always_on()

    def _apply_always_on(self) -> None:
        """Make exactly the picked key (or none) held on by the always-on
        control - moving the steady light off any previous key first."""
        self.always_on_combo.setEnabled(self.always_on_check.isChecked())
        if self.piano is None:
            return

        note = self.always_on_combo.currentData() if self.always_on_check.isChecked() else None
        if note == self._always_on_note:
            return

        if self._always_on_note is not None:
            prev_key = self.piano.keys_by_note.get(self._always_on_note)
            if prev_key is not None:
                prev_key.set_always_on(False)

        self._always_on_note = note
        if note is not None:
            key = self.piano.keys_by_note.get(note)
            if key is not None:
                key.set_always_on(True)

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

        error = self._open_led_connection()
        if error is not None:
            QMessageBox.warning(self, "LED connection failed", error)

    def _open_led_connection(self) -> str | None:
        """Open the LED strip. Returns None on success, or an error string (no
        pop-up - the caller decides how to report it). On success it re-lights a
        key already held by "backlight always on", whose light_key was a no-op
        while the strip was disconnected."""
        try:
            self.led.connect()
            self.led.off()
        except Exception as exc:
            self.led_status.setText(f"LED: not connected ({exc})")
            return str(exc)

        self.led_connected = True
        self.led_status.setText(f"LED: connected on {self.led.port}")
        self.led_connect_btn.setText("Disconnect LED")
        if self.mapper is not None and self._always_on_note is not None:
            try:
                self.mapper.light_key(self._always_on_note)
            except RuntimeError:
                pass
        return None

    def connect_led_for_demo(self) -> None:
        """Demo Mode autostart hook: connect the strip so an always-on backlight
        lights the real keyboard for a photo. Never raises and never pops a
        dialog - if no strip is wired up, the on-screen key still shows lit."""
        if self.led_connected:
            return
        error = self._open_led_connection()
        if error is not None:
            print(f"Demo backlight: LED not connected ({error})")

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
        if self.midi_reader is not None:
            self._stop_midi()
            self.midi_status.setText("MIDI: not connected")
            self.midi_connect_btn.setText("Connect MIDI")
            return

        port_name = self.midi_port_combo.currentText()
        if not port_name:
            QMessageBox.warning(self, "No MIDI port selected", "Pick a MIDI port first.")
            return

        try:
            self.midi_reader = MidiInputReader(port_name)
        except Exception as exc:
            print(f"Could not open MIDI port {port_name!r}: {exc}")
            print(f"Available MIDI inputs: {list_input_ports()}")
            self.midi_status.setText(f"MIDI: not connected ({exc})")
            return

        self._midi_running = True
        self._midi_thread = threading.Thread(target=self._midi_loop, daemon=True)
        self._midi_thread.start()
        self.midi_status.setText(f"MIDI: listening on {self.midi_reader.port_name}")
        self.midi_connect_btn.setText("Disconnect MIDI")

    def _stop_midi(self) -> None:
        self._midi_running = False
        if self._midi_thread is not None:
            self._midi_thread.join(timeout=0.5)
            self._midi_thread = None
        if self.midi_reader is not None:
            self.midi_reader.close()
            self.midi_reader = None

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
        self.piano = PianoWidget(feedback, visual_sequence, self.latch)
        self.piano_slot.addWidget(self.piano)

        # The old keys (and their always-on state) were just destroyed; refill
        # the backlight picker for this profile and re-light on the new keys.
        self._always_on_note = None
        self._populate_backlight_keys(visual_sequence)

        # Just preselect this profile's usual MIDI port in the combo, if it's
        # currently available - connecting is still a manual step (see
        # _toggle_midi()), independent of which profile is loaded.
        mapping = MidiMapping.load(DATA_DIR / keyboard_profile_name / "midi_mapping.json")
        available_ports = [self.midi_port_combo.itemText(i) for i in range(self.midi_port_combo.count())]
        if mapping.port_name and mapping.port_name in available_ports:
            self.midi_port_combo.setCurrentText(mapping.port_name)

    def _midi_loop(self) -> None:
        """Runs on its own thread - do nothing here except hand off to the
        Qt main thread via the signal."""
        while self._midi_running and self.midi_reader is not None:
            for msg in self.midi_reader.poll():
                if msg.type == "note_on":
                    self.midi_bridge.note_event.emit(msg.note, msg.velocity > 0)
                else:
                    self.midi_bridge.note_event.emit(msg.note, False)
            time.sleep(0.001)

    def _on_midi_note(self, note: int, is_on: bool):
        if self.piano is None:
            return
        key = self.piano.keys_by_note.get(note)
        if key is None:
            print(f"MIDI note {note} has no on-screen key")
            return
        key.set_midi_active(is_on)

    def closeEvent(self, event):
        self._stop_midi()
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
