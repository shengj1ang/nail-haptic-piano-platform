"""Rhythm Experiment - play back a generated melody (launcher section 11).

The replay half of the rhythm experiment: pick one of the melodies already
written into ``data/rhythm_experiment/`` and play it on the same on-screen
piano the generator previews on, with the target finger for each note lit as
it is pressed.

What it loads
=============
A melody is a set of four files sharing one stem. Playback needs two of them,
and reads both:

* the ``.json`` carries the fingering, which a MIDI file cannot express, and
  the exact note-on/note-off seconds - so it is what the transport is driven
  from;
* the sibling ``.mid`` is parsed as well and cross-checked against it, note
  for note and onset for onset. The check is shown in the header, so a ``.mid``
  edited or replaced independently of its ``.json`` is visible rather than
  silently ignored.

A ``.mid`` with no ``.json`` beside it still loads and plays; it simply has no
fingering, and the finger dots stay dark. All of that lives in
``melody_generator.load``, so the file format has exactly one reader.

ISOLATION - this window cannot affect any earlier experiment
=============================================================
It is strictly read-only: it opens files under ``data/rhythm_experiment/``
(or whichever folder is chosen) and writes nothing at all - not the melodies
it plays, not ``config.json``, not a keyboard profile, and nothing under
``data/sequence/``, ``data/music/``, ``data/quiz/`` or
``data/MainUserStudy/``. It shares no code with ``app.sequence_generator``,
and the melodies it plays are not registered with ``app.song_library``, so
they cannot appear in the study's song pickers. See ``rhythm_playback.py``
for the display parts it borrows and why they are copied rather than imported.
"""

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from melody_generator.load import LoadedMelody, MelodyLoadError, list_melodies, load_melody
from melody_generator.theory import note_name

from ..config import Config
from .rhythm_melody_window import ISOLATION_NOTE, RHYTHM_DATA_DIR
from .rhythm_playback import MelodyPlaybackPanel

READ_ONLY_NOTE = (
    "Read-only: this window only opens melody files and plays them. "
    + ISOLATION_NOTE.split(": ", 1)[1]
)


class RhythmMelodyPlayerWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        # Accepted so the launcher can construct every tool the same way, and
        # never used: this window reads melody files and nothing else.
        self.cfg = cfg
        self.setWindowTitle("Rhythm Experiment - Playback")
        self.resize(1400, 880)

        self._folder = RHYTHM_DATA_DIR
        self._melody: Optional[LoadedMelody] = None
        self._paths: Dict[int, Path] = {}

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        banner = QLabel(READ_ONLY_NOTE)
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "color:#7fb2ff; border:1px solid #35363e; border-radius:6px;"
            "padding:7px 10px; background:rgba(127,178,255,0.08);"
        )
        outer.addWidget(banner)

        body = QHBoxLayout()
        body.setSpacing(12)
        outer.addLayout(body)
        body.addWidget(self._build_browser(), 0)
        body.addWidget(self._build_player(), 1)

        self._refresh()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_browser(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(340)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)

        box = QGroupBox("Generated melodies")
        inner = QVBoxLayout(box)
        inner.setSpacing(6)

        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)
        self.folder_label.setStyleSheet("color:#9a9ba5;")
        inner.addWidget(self.folder_label)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        inner.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        change_btn = QPushButton("Change folder...")
        change_btn.clicked.connect(self._choose_folder)
        buttons.addWidget(refresh_btn)
        buttons.addWidget(change_btn)
        inner.addLayout(buttons)

        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        inner.addWidget(open_btn)
        column.addWidget(box, 1)

        files = QGroupBox("Files")
        self.files_label = QLabel("Nothing loaded.")
        self.files_label.setWordWrap(True)
        self.files_label.setStyleSheet("color:#9a9ba5;")
        files_layout = QVBoxLayout(files)
        files_layout.addWidget(self.files_label)
        column.addWidget(files)
        return panel

    def _build_player(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.info_label = QLabel("Pick a melody on the left.")
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
    # browsing
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self.folder_label.setText(str(self._folder))
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self._paths.clear()
        for row, path in enumerate(list_melodies(self._folder)):
            suffix = "" if path.suffix.lower() == ".json" else "  (.mid only)"
            item = QListWidgetItem(f"{path.stem}{suffix}")
            item.setToolTip(str(path))
            self.list_widget.addItem(item)
            self._paths[row] = path
        self.list_widget.blockSignals(False)
        if not self._paths:
            self.playback.clear()
            self.report.clear()
            self.files_label.setText("Nothing loaded.")
            self.info_label.setText(
                f"No melodies in {self._folder} yet - generate some with "
                f'"Rhythm Melody Generator", or pick another folder.'
            )
            return
        self.list_widget.setCurrentRow(0)

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Folder holding generated melodies", str(self._folder)
        )
        if chosen:
            self._folder = Path(chosen)
            self._refresh()

    def _open_folder(self) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._folder)))

    def _on_selection_changed(self, row: int) -> None:
        path = self._paths.get(row)
        if path is None:
            return
        self._load(path)

    def _on_double_click(self, _item: QListWidgetItem) -> None:
        if self._melody is not None:
            self.playback.play()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def _load(self, path: Path) -> None:
        try:
            melody = load_melody(path)
        except MelodyLoadError as error:
            self.playback.clear()
            self.report.clear()
            self.info_label.setText(f"Couldn't load {path.name}: {error}")
            return
        self._melody = melody

        left = sum(1 for note in melody.notes if note.hand == "L")
        # The list is browsed by filename, so lead with that; a melody whose
        # recorded name differs from its file (a renamed or copied file) shows
        # both rather than quietly displaying the other one.
        title = (
            path.stem
            if melody.name == path.stem
            else f"{path.stem}  (recorded as {melody.name})"
        )
        bits = [title]
        if melody.key_display:
            bits.append(melody.key_display)
        if melody.layout:
            bits.append(melody.layout)
        bits.append(
            f"{len(melody.notes)} notes, {len(melody.rests)} rests, "
            f"{melody.total_seconds:.1f} s"
        )
        if melody.has_fingering:
            bits.append(f"L{left}/R{len(melody.notes) - left}")
        else:
            bits.append("no fingering in this file")
        if melody.validation_ok is not None:
            bits.append(f"validation {'PASS' if melody.validation_ok else 'FAIL'}")
        if melody.musicality is not None and melody.difficulty is not None:
            bits.append(
                f"musicality {melody.musicality:.2f}, "
                f"difficulty {melody.difficulty:.2f}"
            )
        if melody.midi_agrees is False:
            bits.append("WARNING: the .mid does not match the .json")
        self.info_label.setText("  |  ".join(bits))

        self.files_label.setText(self._describe_files(melody))
        self.report.setPlainText(self._describe_melody(melody))
        self.playback.load(melody.notes, melody.total_seconds)

    def _describe_files(self, melody: LoadedMelody) -> str:
        lines = []
        if melody.json_path is not None:
            lines.append(f"json  {melody.json_path.name}  (notes + fingering)")
        if melody.midi_path is not None:
            if melody.midi_agrees is True:
                check = f"matches the json ({melody.midi_note_count} notes)"
            elif melody.midi_agrees is False:
                check = "DOES NOT match the json"
            else:
                check = "played directly"
            lines.append(f"mid   {melody.midi_path.name}  ({check})")
        else:
            lines.append("mid   missing")
        for extra in (".csv", ".txt"):
            base = melody.json_path or melody.midi_path
            if base is not None and base.with_suffix(extra).is_file():
                lines.append(f"{extra[1:]}   {base.with_suffix(extra).name}")
        return "\n".join(lines)

    def _describe_melody(self, melody: LoadedMelody) -> str:
        """The .txt summary if it was saved, else a table built from the json."""
        base = melody.json_path or melody.midi_path
        if base is not None:
            summary = base.with_suffix(".txt")
            if summary.is_file():
                try:
                    return summary.read_text(encoding="utf-8")
                except OSError:
                    pass

        lines: List[str] = [melody.summary_line(), ""]
        if melody.key_to_finger:
            lines.append(
                "fingering map   "
                + "  ".join(
                    f"{note_name(midi)}={label}"
                    for midi, label in sorted(melody.key_to_finger.items())
                )
            )
            lines.append("")
        header = (
            f"  {'#':>2s} {'hand':>4s} {'fin':>4s} {'note':>5s} {'midi':>5s} "
            f"{'onset_b':>8s} {'dur_b':>6s} {'on_s':>8s} {'off_s':>8s} {'vel':>4s}"
        )
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for note in melody.notes:
            lines.append(
                f"  {note.event_index:>2d} {note.hand or '-':>4s} "
                f"{note.finger or '-':>4s} {note.note_name:>5s} "
                f"{note.midi_note:>5d} {note.onset_beat:>8.2f} "
                f"{note.duration_beats:>6.2f} {note.note_on_time_sec:>8.3f} "
                f"{note.note_off_time_sec:>8.3f} {note.velocity:>4d}"
            )
        if melody.rests:
            lines.append("")
            lines.append("rests")
            for rest in melody.rests:
                lines.append(
                    f"  {rest.rest_index:>2d} onset {rest.onset_beat:>6.2f} b  "
                    f"{rest.duration_beats:g} beat  "
                    f"{rest.start_time_sec:.3f}-{rest.end_time_sec:.3f} s"
                )
        return "\n".join(lines)

    def closeEvent(self, event) -> None:
        self.playback.shutdown()
        super().closeEvent(event)
