"""Shared playback UI for the rhythm experiment: piano, finger dots, transport.

This is the Song Playback (``music_playback.py``) preview, packaged as a widget
so both rhythm-experiment tools show the identical thing - the generator window
auditioning a melody it just made, and the player window replaying one loaded
back off disk.

``FullPianoWidget``, ``FingerDot`` and ``HandsWidget`` are copied from
``music_playback`` rather than imported: importing that module would pull in
``app.song_library`` and through it ``app.sequence_generator``, which the
rhythm experiment must not depend on. The pieces those widgets are built from -
``PianoKey`` and ``KeyFeedback`` - are reused directly, exactly as Song
Playback reuses them, so a note is "pressed" here the same way it is there.

The panel plays any sequence of objects carrying four attributes::

    midi_note, finger, note_on_time_sec, note_off_time_sec

which both ``melody_generator.timing.NoteEvent`` (freshly generated) and
``melody_generator.load.LoadedNote`` (read back from a .json) satisfy.
"""

import time
from bisect import bisect_left
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import NoteLEDMapper
from test_virtual_piano_led import KeyFeedback, PianoKey

# Left hand pinky-to-thumb, then right hand thumb-to-pinky, so the two thumbs
# land in the middle - the same order they would sit in over a keyboard.
FINGER_IDS = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]

DOT_SIZE = 40
DOT_IDLE = QColor(70, 70, 70)
DOT_ACTIVE = QColor(255, 140, 0)

# A short "get ready" beat before the first key fires, so pressing Play does
# not drop the listener straight into note 1.
PLAYBACK_LEAD_IN_S = 2.5

# A full 88-key piano: A0 (MIDI 21) through C8 (MIDI 108). Which pitch classes
# are black keys is a fixed fact about the chromatic scale, not something that
# depends on any particular keyboard or profile.
FIRST_NOTE = 21
LAST_NOTE = 108
_BLACK_PITCH_CLASSES = {1, 3, 6, 8, 10}  # C#, D#, F#, G#, A#


def is_black_key(note: int) -> bool:
    return (note % 12) in _BLACK_PITCH_CLASSES


class DisplayFeedback(KeyFeedback):
    """KeyFeedback for a tool that has no LED strip.

    PianoKey prints "note N has no LED mapping" whenever ``on()`` returns
    False, which an empty LED table does for every single note. These windows
    are on-screen previews by design, so a missing mapping is not news:
    ``on()`` still drives the audio, then reports success.
    """

    def on(self, note: int) -> bool:
        super().on(note)
        return True


class FullPianoWidget(QWidget):
    """All 88 keys of a standard piano, laid out left to right by MIDI note
    number - independent of any calibration profile, since this is a display,
    not something wired to a real LED strip."""

    WHITE_W = 19
    WHITE_H = 130
    BLACK_W = 12
    BLACK_H = 80

    def __init__(self, feedback: KeyFeedback):
        super().__init__()
        self.keys_by_note: dict[int, PianoKey] = {}
        # note -> (x, width), so the falling-notes strip above can drop each
        # block onto the exact key it plays.
        self.note_rects: dict[int, tuple[int, int]] = {}

        white_positions = []  # (note, x)
        black_positions = []  # (note, x)
        white_index = 0
        for note in range(FIRST_NOTE, LAST_NOTE + 1):
            if is_black_key(note):
                black_positions.append(
                    (note, white_index * self.WHITE_W - self.BLACK_W // 2)
                )
            else:
                white_positions.append((note, white_index * self.WHITE_W))
                white_index += 1

        self.setFixedSize(white_index * self.WHITE_W, self.WHITE_H)

        for note, x in white_positions:
            self.note_rects[note] = (x, self.WHITE_W)
        for note, x in black_positions:
            self.note_rects[note] = (x, self.BLACK_W)

        # White keys first, then black keys on top - Qt stacks later-created
        # siblings above earlier ones.
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
    """One fingertip stand-in, lit while that finger is pressing a key."""

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
    """Two labelled groups of five dots - the left hand and the right hand."""

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


class FallingNotesWidget(QWidget):
    """Synthesia-style "piano roll" above the keyboard: each note is a coloured
    block that falls down a dark lane and lands on its key at the moment the
    note sounds.

    Time flows bottom-up: the strike line (y = height) is *now* and the top of
    the widget is ``WINDOW_S`` seconds into the future. A note occupies the band
    between ``note_on_time_sec`` (its bottom, when the key is struck) and
    ``note_off_time_sec`` (its top, when it is released). The per-note x/width
    match the keyboard below, so blocks line up with their keys, and it sits
    inside the same horizontal scroll area so the two scroll together.
    Right-hand notes are warm coral, left-hand notes amber."""

    WINDOW_S = 3.0  # seconds of upcoming music visible, strike line to top.
    HEIGHT = 260

    BG = QColor(38, 38, 40)
    GRID = QColor(58, 58, 62)
    RIGHT_HAND = QColor(240, 130, 110)
    LEFT_HAND = QColor(245, 175, 95)
    NOTE_BORDER = QColor(20, 20, 20)

    def __init__(self, note_rects: dict[int, tuple[int, int]], width: int):
        super().__init__()
        self.note_rects = note_rects
        self.setFixedSize(width, self.HEIGHT)
        self.events: List[object] = []
        self._time = 0.0

    def set_events(self, events: List[object]) -> None:
        self.events = events
        self.update()

    def set_time(self, elapsed: float) -> None:
        self._time = elapsed
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        painter.fillRect(self.rect(), self.BG)

        # Vertical guide lines at each octave boundary (every C).
        painter.setPen(QPen(self.GRID))
        for note, (x, _width) in self.note_rects.items():
            if note % 12 == 0:  # C
                painter.drawLine(x, 0, x, h)

        px_per_s = h / self.WINDOW_S
        painter.setPen(QPen(self.NOTE_BORDER, 1))
        for ev in self.events:
            start = ev.note_on_time_sec - self._time  # seconds until struck
            end = max(ev.note_off_time_sec, ev.note_on_time_sec + 0.05) - self._time
            if end < 0 or start > self.WINDOW_S:
                continue  # already passed, or still too far ahead
            rect = self.note_rects.get(ev.midi_note)
            if rect is None:
                continue
            x, width = rect
            y_bottom = h - start * px_per_s
            y_top = h - end * px_per_s
            finger = getattr(ev, "finger", "") or ""
            hand = self.LEFT_HAND if finger.startswith("L") else self.RIGHT_HAND
            painter.setBrush(hand)
            painter.drawRoundedRect(
                x + 1, int(y_top), max(width - 2, 2), int(y_bottom - y_top), 3, 3
            )


class MelodyPlaybackPanel(QWidget):
    """Audio row, transport, finger dots and piano, driven by one note list.

    Owns its :class:`NoteAudioPlayer`; the host window must call
    :meth:`shutdown` from its ``closeEvent``.
    """

    def __init__(self):
        super().__init__()
        self.audio_player: Optional[NoteAudioPlayer] = None
        try:
            self.audio_player = NoteAudioPlayer()
            audio_status_text = "Audio: ready"
        except Exception as exc:  # no output device, device busy, ...
            audio_status_text = f"Audio: not available ({exc})"

        self.events: List[object] = []
        self._event_times: List[float] = []
        self.total_duration = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._playing = False
        self._play_start_wall_time = 0.0
        self._pause_offset = -PLAYBACK_LEAD_IN_S
        self._next_event_idx = 0
        self._active_events: List[object] = []
        self._slider_down = False
        self._slider_updating = False

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.audio_status = QLabel(audio_status_text)
        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(
            max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0)
        )
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)
        audio_row = QHBoxLayout()
        audio_row.addWidget(self.audio_status, 1)
        audio_row.addWidget(QLabel("Timbre:"))
        audio_row.addWidget(self.timbre_combo)
        column.addLayout(audio_row)

        self.play_btn = QPushButton("Play")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self.toggle_play)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)

        # Seek bar in milliseconds. Dragging seeks on release; clicking the
        # groove page-steps and seeks immediately. While the user holds the
        # handle, _tick stops writing to it so the two do not fight.
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setSingleStep(100)   # arrow keys: 0.1 s
        self.seek_slider.setPageStep(1000)    # groove click: 1 s
        self.seek_slider.sliderPressed.connect(self._on_slider_pressed)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)
        self.seek_slider.sliderReleased.connect(self._on_slider_released)
        self.seek_slider.valueChanged.connect(self._on_slider_value_changed)

        self.progress_label = QLabel("")
        transport_row = QHBoxLayout()
        transport_row.addWidget(self.play_btn)
        transport_row.addWidget(self.stop_btn)
        transport_row.addWidget(self.seek_slider, 1)
        transport_row.addWidget(self.progress_label)
        column.addLayout(transport_row)

        self.hands = HandsWidget()
        column.addWidget(self.hands)

        # No LED hardware is involved: an empty note table makes every
        # light_key()/clear_key() call a no-op, so KeyFeedback only ever
        # drives the on-screen highlight and the audio here.
        feedback = DisplayFeedback(NoteLEDMapper(None, {}), self.audio_player)
        self.piano = FullPianoWidget(feedback)
        # Falling-notes strip, exactly as wide as the keyboard and lined up on
        # the same per-note x positions. Both live in one container inside the
        # scroll area, so a horizontal scroll moves them together and the blocks
        # stay directly above their keys.
        self.falling = FallingNotesWidget(self.piano.note_rects, self.piano.width())
        roll = QWidget()
        roll.setFixedSize(
            self.piano.width(), FallingNotesWidget.HEIGHT + FullPianoWidget.WHITE_H
        )
        roll_layout = QVBoxLayout(roll)
        roll_layout.setContentsMargins(0, 0, 0, 0)
        roll_layout.setSpacing(0)
        roll_layout.addWidget(self.falling)
        roll_layout.addWidget(self.piano)

        self.piano_scroll = QScrollArea()
        self.piano_scroll.setWidget(roll)
        self.piano_scroll.setWidgetResizable(False)
        self.piano_scroll.setFixedHeight(
            FallingNotesWidget.HEIGHT + FullPianoWidget.WHITE_H + 18
        )
        self.piano_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        column.addWidget(self.piano_scroll)

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def load(self, notes: Sequence[object], total_seconds: float) -> None:
        """Arm the transport for a melody and scroll the piano onto its keys."""
        self.stop()
        self.events = list(notes)
        self._event_times = [note.note_on_time_sec for note in self.events]
        self.total_duration = float(total_seconds)
        self.progress_label.setText(f"0.0s / {self.total_duration:.1f}s")
        self.play_btn.setEnabled(bool(self.events))
        self.seek_slider.setRange(0, int(self.total_duration * 1000))
        self.seek_slider.setEnabled(bool(self.events))
        self._sync_slider(0)
        self.falling.set_events(self.events)
        self.falling.set_time(0.0)
        self._centre_piano()

    def clear(self) -> None:
        self.stop()
        self.events = []
        self._event_times = []
        self.total_duration = 0.0
        self.play_btn.setEnabled(False)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.progress_label.setText("")
        self.falling.set_events([])
        self.falling.set_time(0.0)

    def _centre_piano(self) -> None:
        if not self.events:
            return
        lowest = self.piano.keys_by_note.get(min(n.midi_note for n in self.events))
        highest = self.piano.keys_by_note.get(max(n.midi_note for n in self.events))
        if highest is not None:
            self.piano_scroll.ensureWidgetVisible(highest, 80, 0)
        if lowest is not None:
            self.piano_scroll.ensureWidgetVisible(lowest, 80, 0)

    # ------------------------------------------------------------------
    # transport - the same engine as music_playback.PlaybackWindow
    # ------------------------------------------------------------------

    def _on_timbre_changed(self, index: int) -> None:
        if self.audio_player is not None:
            self.audio_player.set_timbre(self.timbre_combo.itemData(index))

    def toggle_play(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        if not self.events:
            return
        self._playing = True
        self.play_btn.setText("Pause")
        self.stop_btn.setEnabled(True)
        self._play_start_wall_time = time.time()
        self._timer.start(20)

    def pause(self) -> None:
        self._playing = False
        self._timer.stop()
        self._pause_offset += time.time() - self._play_start_wall_time
        self.play_btn.setText("Play")

    def stop(self) -> None:
        self._playing = False
        self._timer.stop()
        self.play_btn.setText("Play")
        self.stop_btn.setEnabled(False)
        # Every fresh Play gets the lead-in: start at a negative "elapsed" and
        # count up to 0, so it does not jump straight into the first key.
        self._pause_offset = -PLAYBACK_LEAD_IN_S
        self._next_event_idx = 0

        for key in self.piano.keys_by_note.values():
            key.set_midi_active(False)
        self.hands.clear_all()
        self._active_events = []
        self._sync_slider(0)
        self.falling.set_time(0.0)
        if self.events:
            self.progress_label.setText(f"0.0s / {self.total_duration:.1f}s")

    def elapsed(self) -> float:
        if self._playing:
            return self._pause_offset + (time.time() - self._play_start_wall_time)
        return self._pause_offset

    def _sync_slider(self, ms: int) -> None:
        """Programmatic slider update - flagged so _on_slider_value_changed
        does not mistake it for a user seek."""
        self._slider_updating = True
        self.seek_slider.setValue(ms)
        self._slider_updating = False

    def _on_slider_pressed(self) -> None:
        self._slider_down = True

    def _on_slider_moved(self, value: int) -> None:
        self.progress_label.setText(
            f"{value / 1000.0:.1f}s / {self.total_duration:.1f}s"
        )

    def _on_slider_released(self) -> None:
        self._slider_down = False
        self.seek(self.seek_slider.value() / 1000.0)

    def _on_slider_value_changed(self, value: int) -> None:
        # A groove click (page step) or an arrow key changes the value with no
        # press/release pair - seek immediately in that case.
        if self._slider_updating or self._slider_down:
            return
        self.seek(value / 1000.0)

    def seek(self, target_s: float) -> None:
        """Jump to target_s seconds, playing or paused. Notes whose onset lies
        before the target are skipped rather than re-triggered mid-note."""
        if not self.events:
            return
        target_s = min(max(target_s, 0.0), self.total_duration)

        for event in self._active_events:
            self._set_note_active(event, False)
        self._active_events = []

        self._next_event_idx = bisect_left(self._event_times, target_s)
        self._pause_offset = target_s
        self._play_start_wall_time = time.time()
        self.stop_btn.setEnabled(True)

        self.progress_label.setText(f"{target_s:.1f}s / {self.total_duration:.1f}s")
        self._sync_slider(int(target_s * 1000))
        self.falling.set_time(target_s)

    def _tick(self) -> None:
        elapsed = self.elapsed()
        if elapsed < 0:
            self.progress_label.setText(f"Starting in {-elapsed:.1f}s...")
        else:
            self.progress_label.setText(
                f"{elapsed:.1f}s / {self.total_duration:.1f}s"
            )
        if not self._slider_down:
            self._sync_slider(int(max(elapsed, 0.0) * 1000))
        self.falling.set_time(elapsed)

        while (
            self._next_event_idx < len(self.events)
            and self.events[self._next_event_idx].note_on_time_sec <= elapsed
        ):
            event = self.events[self._next_event_idx]
            self._next_event_idx += 1
            self._active_events.append(event)
            self._set_note_active(event, True)

        still_active = []
        for event in self._active_events:
            if elapsed >= event.note_off_time_sec:
                self._set_note_active(event, False)
            else:
                still_active.append(event)
        self._active_events = still_active

        if (
            self._next_event_idx >= len(self.events)
            and not self._active_events
            and elapsed >= self.total_duration
        ):
            self.stop()

    def _set_note_active(self, event, active: bool) -> None:
        key = self.piano.keys_by_note.get(event.midi_note)
        if key is not None:
            key.set_midi_active(active)
        self.hands.set_active(getattr(event, "finger", ""), active)

    def shutdown(self) -> None:
        """Release the audio device. Call from the host window's closeEvent."""
        self._timer.stop()
        if self.audio_player is not None:
            self.audio_player.stop_all()
            self.audio_player.close()
            self.audio_player = None
