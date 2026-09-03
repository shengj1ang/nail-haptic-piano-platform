"""Rhythm Experiment - melody generator (launcher section 11).

A GUI over the standalone ``melody_generator`` package: pick a seed, a key and
a hand position, watch and hear the melody play on an on-screen piano, and
write the .mid/.json/.csv/.txt stimulus files for the rhythm experiment.

The preview half is :class:`~app.gui.rhythm_playback.MelodyPlaybackPanel` - the
Song Playback UI (88-key piano, ten finger dots, Play/Pause/Stop/seek), shared
with the Playback Rhythm Melody window so a melody looks and sounds the same
whether it has just been generated or is being replayed off disk.

ISOLATION - this window cannot affect any earlier experiment
=============================================================
The rhythm experiment is a separate study from the main haptic user study, and
nothing here is allowed to disturb data or settings that study already depends
on. That is enforced by what this window is wired to, not by convention:

* It generates through ``melody_generator`` only. That package is
  self-contained - it does not import, call or share code with
  ``app.sequence_generator`` (the bimanual pilot-study stimulus generator), and
  it has no third-party dependencies.
* It writes **only** into ``data/rhythm_experiment/``, a folder no other tool
  in this project reads or writes. It never touches ``data/sequence/``,
  ``data/music/``, ``data/quiz/`` or ``data/MainUserStudy/``.
* It never writes ``config.json`` and never touches a keyboard profile. The
  ``cfg`` handed over by the launcher is accepted for interface compatibility
  and deliberately never saved; the melody's note range is fixed by the chosen
  hand position (always inside MIDI 48-72), not by the active profile.
* Its output is not registered with ``app.song_library``, so a rhythm melody
  can never turn up in the song pickers used by ``music_playback``,
  ``student_quiz`` or ``student_quiz_haptic``.

The only things it borrows are display and playback parts that write nothing -
see ``rhythm_playback.py`` for those and why they are copied rather than
imported.

What it generates
=================
Fifteen note-on events by default, one voice, white keys only, on a whole-beat
grid at 60 BPM (1 beat = 1 s). Every phrase ends on a held note and some phrase
endings are followed by a one-beat rest; every note carries a fixed target
finger that never changes between repetitions. See
``doc/melody-generator.md`` for the generation rules, the rejection rules
and the scores.
"""

from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
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
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from melody_generator import GeneratorConfig, LAYOUTS, generate_sequence
from melody_generator.export import export_sequence, summary_text
from melody_generator.generator import GenerationFailed
from melody_generator.theory import KEYS, note_name

from ..config import Config
from .rhythm_playback import MelodyPlaybackPanel

#: Everything this window writes lives here and nowhere else - see the
#: isolation note in the module docstring. app/gui/ -> app/ -> main/.
RHYTHM_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "rhythm_experiment"

ISOLATION_NOTE = (
    "Separate from the main user study: this tool writes only to "
    "data/rhythm_experiment/, never to config.json, keyboard profiles, "
    "data/sequence/, data/music/, data/quiz/ or data/MainUserStudy/, and its "
    "melodies never appear in the study's song pickers."
)


class RhythmMelodyWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        # Accepted so the launcher can construct every tool the same way.
        # Deliberately never saved: see the isolation note above.
        self.cfg = cfg
        self.setWindowTitle("Rhythm Experiment - Melody Generator")
        self.resize(1400, 880)

        self._sequence = None
        self._out_dir = RHYTHM_DATA_DIR

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
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.info_label = QLabel("No melody yet - press Preview.")
        self.info_label.setWordWrap(True)
        column.addWidget(self.info_label)

        self.playback = MelodyPlaybackPanel()
        column.addWidget(self.playback)

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
        keys = "  ".join(f"{slot.label}={note_name(slot.midi)}" for slot in layout.slots)
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
        self._sequence = sequence
        left = sum(1 for note in sequence.notes if note.hand == "L")
        state = "PASS" if sequence.validation.ok else "FAIL"
        self.info_label.setText(
            f"seed {sequence.seed}  |  {sequence.key.display}  |  "
            f"{sequence.layout.name}  |  {len(sequence.notes)} notes, "
            f"{len(sequence.rests)} rests, {sequence.total_seconds:.1f} s  |  "
            f"L{left}/R{len(sequence.notes) - left}  |  validation {state}  |  "
            f"musicality {sequence.scores.musicality:.2f}, "
            f"difficulty {sequence.scores.difficulty:.2f}"
        )
        self.report.setPlainText(summary_text(sequence))
        self.playback.load(sequence.notes, sequence.total_seconds)

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

    def closeEvent(self, event) -> None:
        self.playback.shutdown()
        super().closeEvent(event)


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    row.addStretch(1)
    row.addWidget(widget)
    return row
