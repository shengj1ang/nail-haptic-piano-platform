"""Rhythm Experiment - melody generator (launcher section 11).

A GUI over the standalone ``melody_generator`` package: pick a seed, a key
and a hand position, watch and hear the melody play on an on-screen piano,
and write the .mid/.json/.csv/.txt stimulus files for the rhythm experiment.

The preview half of this window is deliberately the same UI as Song Playback
(``music_playback.py``): the 88-key piano, the ten finger dots, and the
Play/Pause/Stop/seek transport, driven the same way - a key lights up while
it is held and the dot for its target finger (L1-L5 / R1-R5) lights with it.
``FullPianoWidget``, ``FingerDot`` and ``HandsWidget`` are copied from there
rather than imported, because importing ``music_playback`` would drag in
``app.song_library`` and through it ``app.sequence_generator``, which this
window must not depend on (see below). The pieces those widgets are built
from - ``PianoKey`` and ``KeyFeedback`` - are reused directly, exactly as
Song Playback reuses them, so a note is "pressed" here in the same way.

ISOLATION - this window cannot affect any earlier experiment
=============================================================
The rhythm experiment is a separate study from the main haptic user study,
and nothing here is allowed to disturb data or settings that study already
depends on. That is enforced by what this window is wired to, not by
convention:

* It generates through ``melody_generator`` only. That package is
  self-contained - it does not import, call or share code with
  ``app.sequence_generator`` (the bimanual pilot-study stimulus generator),
  and it has no third-party dependencies.
* It writes **only** into ``data/rhythm_experiment/``, a folder no other
  tool in this project reads or writes. It never touches ``data/sequence/``,
  ``data/music/``, ``data/quiz/`` or ``data/MainUserStudy/``.
* It never writes ``config.json`` and never touches a keyboard profile. The
  ``cfg`` handed over by the launcher is accepted for interface
  compatibility and deliberately never saved; the melody's note range is
  fixed by the chosen hand position (always inside MIDI 48-72), not by the
  active profile.
* Its output is not registered with ``app.song_library``, so a rhythm
  melody can never turn up in the song pickers used by ``music_playback``,
  ``student_quiz`` or ``student_quiz_haptic``.

The only things it borrows are display and playback parts that write
nothing: ``note_audio.NoteAudioPlayer`` (the tone synthesiser, which claims
the audio output), and ``PianoKey``/``KeyFeedback`` from
``test_virtual_piano_led``. The LED side of ``KeyFeedback`` is fed an empty
``NoteLEDMapper`` table, so every LED call is a no-op and no strip is
touched even if one is wired up.

What it generates
=================
Fifteen note-on events by default, one voice, white keys only, on a whole-beat
grid at 60 BPM (1 beat = 1 s). Every phrase ends on a held note and some
phrase endings are followed by a one-beat rest; every note carries a fixed
target finger that never changes between repetitions. See
``melody_generator/README.md`` for the generation rules, the rejection rules
and the scores.
"""

import time
from bisect import bisect_left
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from melody_generator import GeneratorConfig, LAYOUTS, generate_sequence
from melody_generator.export import export_sequence, summary_text
from melody_generator.generator import GenerationFailed
from melody_generator.theory import KEYS
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import NoteLEDMapper
from test_virtual_piano_led import KeyFeedback, PianoKey

from ..config import Config

#: Everything this window writes lives here and nowhere else - see the
#: isolation note in the module docstring. app/gui/ -> app/ -> main/.
RHYTHM_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "rhythm_experiment"

ISOLATION_NOTE = (
    "Separate from the main user study: this tool writes only to "
    "data/rhythm_experiment/, never to config.json, keyboard profiles, "
    "data/sequence/, data/music/, data/quiz/ or data/MainUserStudy/, and its "
    "melodies never appear in the study's song pickers."
)

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


def _is_black_key(note: int) -> bool:
    return (note % 12) in _BLACK_PITCH_CLASSES


class _DisplayFeedback(KeyFeedback):
    """KeyFeedback for a tool that has no LED strip.

    PianoKey prints "note N has no LED mapping" whenever ``on()`` returns
    False, which an empty LED table does for every single note. This window
    is an on-screen preview by design, so a missing mapping is not news:
    ``on()`` still drives the audio, then reports success.
    """

    def on(self, note: int) -> bool:
        super().on(note)
        return True


class FullPianoWidget(QWidget):
    """All 88 keys of a standard piano, laid out left to right by MIDI note
    number - independent of any calibration profile, since this is just a
    display, not something wired to a real LED strip.

    Copied from music_playback.FullPianoWidget; see the module docstring for
    why it is copied rather than imported.
    """

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
                black_positions.append(
                    (note, white_index * self.WHITE_W - self.BLACK_W // 2)
                )
            else:
                white_positions.append((note, white_index * self.WHITE_W))
                white_index += 1

        self.setFixedSize(white_index * self.WHITE_W, self.WHITE_H)

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


class RhythmMelodyWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        # Accepted so the launcher can construct every tool the same way.
        # Deliberately never saved: see the isolation note above.
        self.cfg = cfg
        self.setWindowTitle("Rhythm Experiment - Melody Generator")
        self.resize(1400, 880)

        self.audio_player: Optional[NoteAudioPlayer] = None
        try:
            self.audio_player = NoteAudioPlayer()
            audio_status_text = "Audio: ready"
        except Exception as exc:
            audio_status_text = f"Audio: not available ({exc})"
        self._audio_status_text = audio_status_text

        self._sequence = None
        self.events: List[object] = []      # the generated NoteEvents, in onset order
        self._event_times: List[float] = []  # their onsets, for bisecting a seek target
        self.total_duration = 0.0
        self._out_dir = RHYTHM_DATA_DIR

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._playing = False
        self._play_start_wall_time = 0.0
        self._pause_offset = -PLAYBACK_LEAD_IN_S
        self._next_event_idx = 0
        self._active_events: List[object] = []
        self._slider_down = False
        self._slider_updating = False

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        banner = QLabel(ISOLATION_NOTE)
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "color:#7fb2ff; border:1px solid #35363e; border-radius:6px;"
            "padding:7px 10px; background:rgba(127,178,255,0.08);"
        )
        outer.addWidget(banner)

        body = QHBoxLayout()
        body.setSpacing(12)
        outer.addLayout(body)
        body.addWidget(self._build_controls(), 0)
        body.addWidget(self._build_preview(), 1)

        self._apply_gate_mode()
        self._refresh_layout_hint()
        self._refresh_out_label()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(340)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)

        music = QGroupBox("Melody")
        form = QVBoxLayout(music)
        form.setSpacing(6)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_000_000_000)
        self.seed_spin.setValue(42)
        self.seed_spin.setToolTip(
            "The same seed always regenerates the identical melody."
        )
        form.addLayout(_row("Seed", self.seed_spin))

        self.key_combo = QComboBox()
        for name in sorted(KEYS):
            self.key_combo.addItem(KEYS[name].display, name)
        # C major is the default because every hand position can play it;
        # a minor key needs its tonic to be inside the position (see
        # _sync_keys_to_layout).
        default_key = self.key_combo.findData("c_major")
        if default_key >= 0:
            self.key_combo.setCurrentIndex(default_key)
        form.addLayout(_row("Key", self.key_combo))

        self.layout_combo = QComboBox()
        for name in sorted(LAYOUTS):
            self.layout_combo.addItem(name, name)
        self.layout_combo.setCurrentText("middle_c")
        self.layout_combo.currentIndexChanged.connect(self._refresh_layout_hint)
        form.addLayout(_row("Hand position", self.layout_combo))

        self.layout_hint = QLabel()
        self.layout_hint.setWordWrap(True)
        self.layout_hint.setStyleSheet("color:#9a9ba5;")
        form.addWidget(self.layout_hint)

        self.notes_spin = QSpinBox()
        self.notes_spin.setRange(6, 40)
        self.notes_spin.setValue(15)
        self.notes_spin.setToolTip("Number of note-on events (key presses).")
        form.addLayout(_row("Note-on events", self.notes_spin))

        self.hand_spin = QSpinBox()
        self.hand_spin.setRange(0, 7)
        self.hand_spin.setValue(1)
        self.hand_spin.setToolTip(
            "On a two-hand position, the fewest notes each hand must play.\n"
            "1 only guarantees both hands are used; raise it for a more evenly\n"
            "bimanual melody; 0 allows a one-handed result."
        )
        form.addLayout(_row("Min notes per hand", self.hand_spin))
        column.addWidget(music)

        rhythm = QGroupBox("Rhythm && articulation")
        rform = QVBoxLayout(rhythm)
        rform.setSpacing(6)

        self.bpm_spin = QSpinBox()
        self.bpm_spin.setRange(30, 180)
        self.bpm_spin.setValue(60)
        self.bpm_spin.setToolTip("At 60 BPM one beat is exactly one second.")
        rform.addLayout(_row("Tempo (BPM)", self.bpm_spin))

        self.rest_min_spin = QSpinBox()
        self.rest_min_spin.setRange(0, 4)
        self.rest_min_spin.setValue(1)
        self.rest_max_spin = QSpinBox()
        self.rest_max_spin.setRange(0, 4)
        self.rest_max_spin.setValue(3)
        rests = QHBoxLayout()
        rests.addWidget(QLabel("1-beat rests"))
        rests.addStretch(1)
        rests.addWidget(self.rest_min_spin)
        rests.addWidget(QLabel("to"))
        rests.addWidget(self.rest_max_spin)
        rform.addLayout(rests)

        self.gate_combo = QComboBox()
        self.gate_combo.addItem("fixed gap (recommended)", "fixed_gap")
        self.gate_combo.addItem("fixed ratio", "ratio")
        self.gate_combo.currentIndexChanged.connect(self._apply_gate_mode)
        rform.addLayout(_row("Gate mode", self.gate_combo))

        self.gap_spin = QDoubleSpinBox()
        self.gap_spin.setRange(0.05, 0.9)
        self.gap_spin.setSingleStep(0.05)
        self.gap_spin.setDecimals(2)
        self.gap_spin.setValue(0.25)
        self.gap_spin.valueChanged.connect(self._refresh_gate_hint)
        rform.addLayout(_row("Release gap (beats)", self.gap_spin))

        self.ratio_spin = QDoubleSpinBox()
        self.ratio_spin.setRange(0.30, 0.99)
        self.ratio_spin.setSingleStep(0.05)
        self.ratio_spin.setDecimals(2)
        self.ratio_spin.setValue(0.75)
        self.ratio_spin.valueChanged.connect(self._refresh_gate_hint)
        rform.addLayout(_row("Gate ratio", self.ratio_spin))

        self.gate_hint = QLabel()
        self.gate_hint.setWordWrap(True)
        self.gate_hint.setStyleSheet("color:#9a9ba5;")
        rform.addWidget(self.gate_hint)

        self.velocity_spin = QSpinBox()
        self.velocity_spin.setRange(1, 127)
        self.velocity_spin.setValue(80)
        rform.addLayout(_row("Velocity", self.velocity_spin))
        column.addWidget(rhythm)

        actions = QGroupBox("Generate")
        acts = QVBoxLayout(actions)
        acts.setSpacing(6)

        self.preview_btn = QPushButton("Preview (writes nothing)")
        self.preview_btn.clicked.connect(self._preview)
        acts.addWidget(self.preview_btn)

        self.save_btn = QPushButton("Generate && Save files")
        self.save_btn.clicked.connect(self._generate_and_save)
        acts.addWidget(self.save_btn)

        self.out_label = QLabel()
        self.out_label.setWordWrap(True)
        self.out_label.setStyleSheet("color:#9a9ba5;")
        acts.addWidget(self.out_label)

        folder_row = QHBoxLayout()
        change_btn = QPushButton("Change folder...")
        change_btn.clicked.connect(self._choose_folder)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        folder_row.addWidget(change_btn)
        folder_row.addWidget(open_btn)
        acts.addLayout(folder_row)
        column.addWidget(actions)

        column.addStretch(1)
        return panel

    def _build_preview(self) -> QWidget:
        """The Song Playback half: piano, finger dots and a transport."""
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.info_label = QLabel("No melody yet - press Preview.")
        self.info_label.setWordWrap(True)
        column.addWidget(self.info_label)

        self.audio_status = QLabel(self._audio_status_text)
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
        self.play_btn.clicked.connect(self._toggle_play)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)

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
        feedback = _DisplayFeedback(NoteLEDMapper(None, {}), self.audio_player)
        self.piano = FullPianoWidget(feedback)
        self.piano_scroll = QScrollArea()
        self.piano_scroll.setWidget(self.piano)
        self.piano_scroll.setWidgetResizable(False)
        self.piano_scroll.setFixedHeight(FullPianoWidget.WHITE_H + 18)
        self.piano_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        column.addWidget(self.piano_scroll)

        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.report.setFont(QFont("Menlo", 11))

        column.addWidget(self.report, 1)
        return panel

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    def _config(self) -> GeneratorConfig:
        """Build a melody_generator config from the form. Nothing on disk."""
        base = GeneratorConfig()
        return base.with_overrides(
            melody=replace(
                base.melody,
                key=self.key_combo.currentData(),
                layout=self.layout_combo.currentData(),
                note_count=self.notes_spin.value(),
            ),
            rhythm=replace(
                base.rhythm,
                min_rests=min(self.rest_min_spin.value(), self.rest_max_spin.value()),
                max_rests=max(self.rest_min_spin.value(), self.rest_max_spin.value()),
            ),
            timing=replace(
                base.timing,
                bpm=float(self.bpm_spin.value()),
                gate_mode=self.gate_combo.currentData(),
                release_gap_beats=self.gap_spin.value(),
                gate_ratio=self.ratio_spin.value(),
                base_velocity=self.velocity_spin.value(),
            ),
            validation=replace(
                base.validation, min_notes_per_hand=self.hand_spin.value()
            ),
        )

    def _apply_gate_mode(self) -> None:
        fixed = self.gate_combo.currentData() == "fixed_gap"
        self.gap_spin.setEnabled(fixed)
        self.ratio_spin.setEnabled(not fixed)
        self._refresh_gate_hint()

    def _refresh_gate_hint(self) -> None:
        from melody_generator.timing import sounding_beats

        timing = self._config().timing
        held = [
            f"{beats}-beat note sounds {sounding_beats(float(beats), timing):g}"
            for beats in (1, 2, 3)
        ]
        self.gate_hint.setText("; ".join(held) + " beat(s).")

    def _refresh_layout_hint(self) -> None:
        layout = LAYOUTS[self.layout_combo.currentData()]
        keys = "  ".join(
            f"{slot.label}={_note_name(slot.midi)}" for slot in layout.slots
        )
        self.layout_hint.setText(f"{layout.description}\n{keys}")
        self._sync_keys_to_layout()
        self._refresh_gate_hint()

    def _sync_keys_to_layout(self) -> None:
        """Grey out keys the current hand position cannot finish in.

        A melody has to be able to end on its tonic, so a key whose tonic is
        not one of the position's keys - A minor in a C-C position, say - is
        not offered at all, rather than being offered and then failing at
        generation time.
        """
        layout = LAYOUTS[self.layout_combo.currentData()]
        available = {midi % 12 for midi in layout.keys()}
        model = self.key_combo.model()
        first_supported = -1
        for index in range(self.key_combo.count()):
            supported = KEYS[self.key_combo.itemData(index)].tonic_pc in available
            item = model.item(index)
            if item is not None:
                item.setEnabled(supported)
            if supported and first_supported < 0:
                first_supported = index
        current = self.key_combo.currentIndex()
        if (
            current >= 0
            and KEYS[self.key_combo.itemData(current)].tonic_pc not in available
            and first_supported >= 0
        ):
            self.key_combo.setCurrentIndex(first_supported)
            self.info_label.setText(
                f"{layout.name} cannot end on that tonic - switched to "
                f"{self.key_combo.currentText()}."
            )

    def _refresh_out_label(self) -> None:
        self.out_label.setText(f"Output folder: {self._out_dir}")

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

    def _generate(self, seed: int):
        try:
            return generate_sequence(seed, cfg=self._config())
        except (GenerationFailed, ValueError) as error:
            QMessageBox.warning(self, "Couldn't generate a melody", str(error))
            return None

    def _preview(self) -> None:
        sequence = self._generate(self.seed_spin.value())
        if sequence is None:
            return
        self._load_sequence(sequence)
        self.info_label.setText(
            f"{self.info_label.text()}   |   nothing written - press "
            f'"Generate & Save files" for the file set'
        )

    def _generate_and_save(self) -> None:
        sequence = self._generate(self.seed_spin.value())
        if sequence is None:
            return
        try:
            export_sequence(sequence, self._out_dir)
        except OSError as error:
            QMessageBox.warning(self, "Couldn't write the files", str(error))
            return
        self._load_sequence(sequence)
        self.info_label.setText(
            f"{self.info_label.text()}   |   wrote {sequence.name}"
            f".mid/.json/.csv/.txt to {self._out_dir}"
        )

    def _load_sequence(self, sequence) -> None:
        """Show a freshly generated melody and arm the transport for it."""
        self._stop()
        self._sequence = sequence
        self.events = list(sequence.notes)
        self._event_times = [note.note_on_time_sec for note in self.events]
        self.total_duration = sequence.total_seconds

        left = sum(1 for note in self.events if note.hand == "L")
        state = "PASS" if sequence.validation.ok else "FAIL"
        self.info_label.setText(
            f"seed {sequence.seed}  |  {sequence.key.display}  |  "
            f"{sequence.layout.name}  |  {len(self.events)} notes, "
            f"{len(sequence.rests)} rests, {self.total_duration:.1f} s  |  "
            f"L{left}/R{len(self.events) - left}  |  validation {state}  |  "
            f"musicality {sequence.scores.musicality:.2f}, "
            f"difficulty {sequence.scores.difficulty:.2f}"
        )
        self.report.setPlainText(summary_text(sequence))
        self.progress_label.setText(f"0.0s / {self.total_duration:.1f}s")
        self.play_btn.setEnabled(bool(self.events))
        self.seek_slider.setRange(0, int(self.total_duration * 1000))
        self.seek_slider.setEnabled(bool(self.events))
        self._sync_slider(0)
        self._centre_piano()

    def _centre_piano(self) -> None:
        """Scroll the 88-key display onto the keys this melody actually uses."""
        if not self.events:
            return
        lowest = self.piano.keys_by_note.get(min(n.midi_note for n in self.events))
        highest = self.piano.keys_by_note.get(max(n.midi_note for n in self.events))
        if highest is not None:
            self.piano_scroll.ensureWidgetVisible(highest, 80, 0)
        if lowest is not None:
            self.piano_scroll.ensureWidgetVisible(lowest, 80, 0)

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Output folder for rhythm-experiment melodies", str(self._out_dir)
        )
        if chosen:
            self._out_dir = Path(chosen)
            self._refresh_out_label()

    def _open_folder(self) -> None:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._out_dir)))

    # ------------------------------------------------------------------
    # transport - the same engine as music_playback.PlaybackWindow
    # ------------------------------------------------------------------

    def _on_timbre_changed(self, index: int) -> None:
        if self.audio_player is not None:
            self.audio_player.set_timbre(self.timbre_combo.itemData(index))

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
        # Every fresh Play gets the short lead-in: start at a negative
        # "elapsed" and count up to 0, so it does not jump straight into the
        # first key.
        self._pause_offset = -PLAYBACK_LEAD_IN_S
        self._next_event_idx = 0

        for key in self.piano.keys_by_note.values():
            key.set_midi_active(False)
        self.hands.clear_all()
        self._active_events = []
        self._sync_slider(0)
        if self.events:
            self.progress_label.setText(f"0.0s / {self.total_duration:.1f}s")

    def _elapsed(self) -> float:
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
        self._seek(self.seek_slider.value() / 1000.0)

    def _on_slider_value_changed(self, value: int) -> None:
        # A groove click (page step) or an arrow key changes the value with no
        # press/release pair - seek immediately in that case.
        if self._slider_updating or self._slider_down:
            return
        self._seek(value / 1000.0)

    def _seek(self, target_s: float) -> None:
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

    def _tick(self) -> None:
        elapsed = self._elapsed()
        if elapsed < 0:
            self.progress_label.setText(f"Starting in {-elapsed:.1f}s...")
        else:
            self.progress_label.setText(
                f"{elapsed:.1f}s / {self.total_duration:.1f}s"
            )
        if not self._slider_down:
            self._sync_slider(int(max(elapsed, 0.0) * 1000))

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

        if self._next_event_idx >= len(self.events) and not self._active_events:
            if elapsed >= self.total_duration:
                self._stop()

    def _set_note_active(self, event, active: bool) -> None:
        key = self.piano.keys_by_note.get(event.midi_note)
        if key is not None:
            key.set_midi_active(active)
        self.hands.set_active(event.finger, active)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self.audio_player is not None:
            self.audio_player.stop_all()
            self.audio_player.close()
            self.audio_player = None
        super().closeEvent(event)


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    row.addStretch(1)
    row.addWidget(widget)
    return row


def _note_name(midi: int) -> str:
    from melody_generator.theory import note_name

    return note_name(midi)
