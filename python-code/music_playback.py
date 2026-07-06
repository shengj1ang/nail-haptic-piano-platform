"""
Manual-test/demo UI: replays a song recorded with
music_recording_wizard.py (data/music/<song>/) on an on-screen piano,
highlighting each key as it's "pressed" and lighting up a dot standing in
for whichever finger (L1-L5/R1-R5) played it, per fingering.json.

No camera, MIDI device, or LED strip required - this only reads back
already-saved files. The keyboard is a full 88-key (A0-C8) piano, not just
the ~25 keys the recording keyboard physically has - unlike
test_virtual_piano_led.py's PianoWidget (which mirrors one specific wired
rig's real key count/positions for real LED output), this is a pure display,
so it shows a complete piano regardless of which keys the recording
actually used. It does reuse PianoKey (one key widget) and KeyFeedback from
test_virtual_piano_led.py rather than reimplementing those - a note is
"pressed" here the exact same way it is there for real MIDI input, via
PianoKey.set_midi_active().
"""

import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
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

from app.config import Config
from app.music_recording import PlaybackEvent, SongMeta, load_playback_events, song_dir, META_FILENAME
from app.song_library import SongEntry, find_song_entry, list_song_entries
from test_virtual_piano_led import KeyFeedback, PianoKey
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import NoteLEDMapper

# Left hand pinky-to-thumb, then right hand thumb-to-pinky, so the two
# thumbs land in the middle - the same order they'd sit in over a keyboard.
FINGER_IDS = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]

DOT_SIZE = 40
DOT_IDLE = QColor(70, 70, 70)
DOT_ACTIVE = QColor(255, 140, 0)

# Saved songs start right at the first note (see
# app.music_recording.trim_to_first_note) - a short pause here before
# playback actually starts gives a "get ready" beat instead of the first
# key firing the instant Play is clicked.
PLAYBACK_LEAD_IN_S = 2.5

# A full 88-key piano: A0 (MIDI 21) through C8 (MIDI 108). Which pitch
# classes are black keys is a fixed fact about the chromatic scale, not
# something that depends on any particular keyboard/profile.
FIRST_NOTE = 21
LAST_NOTE = 108
_BLACK_PITCH_CLASSES = {1, 3, 6, 8, 10}  # C#, D#, F#, G#, A#


def _is_black_key(note: int) -> bool:
    return (note % 12) in _BLACK_PITCH_CLASSES


class FullPianoWidget(QWidget):
    """All 88 keys of a standard piano, laid out left to right by MIDI note
    number - independent of any calibration profile, since this is just a
    display, not something wired to a real LED strip."""

    WHITE_W = 19
    WHITE_H = 130
    BLACK_W = 12
    BLACK_H = 80

    def __init__(self, feedback: KeyFeedback):
        super().__init__()
        self.keys_by_note: dict[int, PianoKey] = {}

        white_positions = []  # (note, x)
        black_positions = []  # (note, x)
        white_index = 0
        for note in range(FIRST_NOTE, LAST_NOTE + 1):
            if _is_black_key(note):
                black_positions.append((note, white_index * self.WHITE_W - self.BLACK_W // 2))
            else:
                white_positions.append((note, white_index * self.WHITE_W))
                white_index += 1

        self.setFixedSize(white_index * self.WHITE_W, self.WHITE_H)

        # White keys first, then black keys on top - Qt stacks later-created
        # siblings above earlier ones (same reasoning as PianoWidget).
        for note, x in white_positions:
            key = PianoKey(self, note, is_black=False, feedback=feedback)
            key.setFixedSize(self.WHITE_W, self.WHITE_H)
            key.move(x, 0)
            self.keys_by_note[note] = key

        for note, x in black_positions:
            key = PianoKey(self, note, is_black=True, feedback=feedback)
            key.setFixedSize(self.BLACK_W, self.BLACK_H)
            key.move(x, 0)
            key.raise_()
            self.keys_by_note[note] = key


class FingerDot(QWidget):
    """Stands in for one fingertip when full hand/finger graphics aren't
    needed - lit while that finger is "pressing" a key during playback."""

    def __init__(self, finger_id: str):
        super().__init__()
        self.finger_id = finger_id
        self.active = False
        self.setFixedSize(DOT_SIZE, DOT_SIZE)

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(DOT_ACTIVE if self.active else DOT_IDLE)
        painter.setPen(QPen(QColor(20, 20, 20)))
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.finger_id)


class HandsWidget(QWidget):
    """Two labeled groups of five dots (left hand / right hand) - the
    simplified stand-in for "show two hands" this demo uses."""

    def __init__(self):
        super().__init__()
        self.dots = {finger_id: FingerDot(finger_id) for finger_id in FINGER_IDS}

        layout = QHBoxLayout(self)
        layout.addWidget(QLabel("Left hand:"))
        for finger_id in ["L5", "L4", "L3", "L2", "L1"]:
            layout.addWidget(self.dots[finger_id])
        layout.addStretch(1)
        layout.addWidget(QLabel("Right hand:"))
        for finger_id in ["R1", "R2", "R3", "R4", "R5"]:
            layout.addWidget(self.dots[finger_id])

    def set_active(self, finger_id: Optional[str], active: bool) -> None:
        dot = self.dots.get(finger_id) if finger_id else None
        if dot is not None:
            dot.set_active(active)

    def clear_all(self) -> None:
        for dot in self.dots.values():
            dot.set_active(False)


class PlaybackWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Song Playback (keyboard + finger demo)")
        self.cfg = cfg  # unused for now, accepted so the launcher can open this like every other tool

        self.audio_player: Optional[NoteAudioPlayer] = None
        try:
            self.audio_player = NoteAudioPlayer()
            audio_status_text = "Audio: ready"
        except Exception as exc:
            audio_status_text = f"Audio: not available ({exc})"

        self.song_combo = QComboBox()
        self.song_combo.currentTextChanged.connect(self._load_song)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_songs)

        self.play_btn = QPushButton("Play")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._toggle_play)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)

        self.info_label = QLabel("No song loaded.")
        self.progress_label = QLabel("")
        self.audio_status = QLabel(audio_status_text)
        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0))
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)
        self.hands = HandsWidget()

        song_row = QHBoxLayout()
        song_row.addWidget(QLabel("Song:"))
        song_row.addWidget(self.song_combo, 1)
        song_row.addWidget(refresh_btn)

        transport_row = QHBoxLayout()
        transport_row.addWidget(self.play_btn)
        transport_row.addWidget(self.stop_btn)
        transport_row.addWidget(self.progress_label, 1)

        audio_row = QHBoxLayout()
        audio_row.addWidget(self.audio_status, 1)
        audio_row.addWidget(QLabel("Timbre:"))
        audio_row.addWidget(self.timbre_combo)

        # No LED hardware involved in playback - an empty note table makes
        # every light_key()/clear_key() call a no-op, so KeyFeedback only
        # ever drives the on-screen highlight + (optional) audio here. The
        # 88-key layout doesn't depend on any profile, so it's built once.
        feedback = KeyFeedback(NoteLEDMapper(None, {}), self.audio_player)
        self.piano = FullPianoWidget(feedback)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(song_row)
        layout.addWidget(self.info_label)
        layout.addLayout(audio_row)
        layout.addLayout(transport_row)
        layout.addWidget(self.hands)
        layout.addWidget(self.piano)
        self.setCentralWidget(central)

        self.events: list[PlaybackEvent] = []
        self.total_duration = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._playing = False
        self._play_start_wall_time = 0.0
        self._pause_offset = -PLAYBACK_LEAD_IN_S  # elapsed play time banked before the current run
        self._next_event_idx = 0
        self._active_events: list[PlaybackEvent] = []  # currently-held notes, so we know when to release them

        self._refresh_songs()

    def _on_timbre_changed(self, index: int) -> None:
        if self.audio_player is not None:
            self.audio_player.set_timbre(self.timbre_combo.itemData(index))

    # ------------------------------------------------------------------
    # Song loading
    # ------------------------------------------------------------------

    def _refresh_songs(self) -> None:
        entries = list_song_entries()
        self.song_combo.blockSignals(True)
        self.song_combo.clear()
        self.song_combo.addItems([entry.label for entry in entries])
        self.song_combo.blockSignals(False)

        if not entries:
            self.info_label.setText(
                "No songs found under data/music/ or data/sequence/. Record one with music_recording_wizard.py, "
                "or generate one with experiment_sequence_wizard.py."
            )
            return
        self._load_song(entries[0].label)

    def _load_song(self, label: str) -> None:
        if not label:
            return
        self._stop()

        entry: Optional[SongEntry] = find_song_entry(label)
        if entry is None:
            return

        try:
            meta = SongMeta.load(song_dir(entry.name, entry.data_dir) / META_FILENAME)
            self.events = load_playback_events(entry.name, data_dir=entry.data_dir)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't load song", str(exc))
            return

        self.total_duration = max((e.time + e.duration for e in self.events), default=0.0)
        self.info_label.setText(
            f"{meta.title}  |  difficulty {meta.difficulty}  |  "
            f"{meta.note_count} notes  |  {self.total_duration:.1f}s"
        )
        self.progress_label.setText(f"0.0s / {self.total_duration:.1f}s")
        self.play_btn.setEnabled(bool(self.events))

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _toggle_play(self) -> None:
        if self._playing:
            self._pause()
        else:
            self._play()

    def _play(self) -> None:
        if not self.events:
            return
        self._playing = True
        self.play_btn.setText("Pause")
        self.stop_btn.setEnabled(True)
        self._play_start_wall_time = time.time()
        self._timer.start(20)

    def _pause(self) -> None:
        self._playing = False
        self._timer.stop()
        self._pause_offset += time.time() - self._play_start_wall_time
        self.play_btn.setText("Play")

    def _stop(self) -> None:
        self._playing = False
        self._timer.stop()
        self.play_btn.setText("Play")
        self.stop_btn.setEnabled(False)
        # Saved songs now start right at the first note (see
        # app.music_recording.trim_to_first_note), so every fresh Play gets a
        # short lead-in here instead - starting at a negative "elapsed" and
        # counting up to 0 - so it doesn't jump straight into the first key.
        self._pause_offset = -PLAYBACK_LEAD_IN_S
        self._next_event_idx = 0

        if self.piano is not None:
            for key in self.piano.keys_by_note.values():
                key.set_midi_active(False)
        self.hands.clear_all()
        self._active_events = []
        if self.events:
            self.progress_label.setText(f"0.0s / {self.total_duration:.1f}s")

    def _elapsed(self) -> float:
        if self._playing:
            return self._pause_offset + (time.time() - self._play_start_wall_time)
        return self._pause_offset

    def _tick(self) -> None:
        elapsed = self._elapsed()
        if elapsed < 0:
            self.progress_label.setText(f"Starting in {-elapsed:.1f}s...")
        else:
            self.progress_label.setText(f"{elapsed:.1f}s / {self.total_duration:.1f}s")

        while self._next_event_idx < len(self.events) and self.events[self._next_event_idx].time <= elapsed:
            ev = self.events[self._next_event_idx]
            self._next_event_idx += 1
            self._active_events.append(ev)
            self._set_note_active(ev, True)

        still_active = []
        for ev in self._active_events:
            if elapsed >= ev.time + ev.duration:
                self._set_note_active(ev, False)
            else:
                still_active.append(ev)
        self._active_events = still_active

        if self._next_event_idx >= len(self.events) and not self._active_events:
            self._stop()

    def _set_note_active(self, ev: PlaybackEvent, active: bool) -> None:
        key = self.piano.keys_by_note.get(ev.note) if self.piano else None
        if key is not None:
            key.set_midi_active(active)
        self.hands.set_active(ev.finger, active)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self.audio_player is not None:
            self.audio_player.stop_all()
            self.audio_player.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = PlaybackWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
