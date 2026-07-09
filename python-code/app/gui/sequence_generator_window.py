"""Experiment Sequence Generator.

Builds the constrained bimanual motor-sequence stimuli described in
final_report_2026/method/method.tex ("Sequence Design and Difficulty
Levels"): click Generate and get a fresh matched family of 30-event
sequences for *every* difficulty level (alpha, beta, gamma) at once -
there is no level picker, since a stimulus set always needs all three.
Each level's family size is set by the Count field (default 9); every
sequence in a level's family is named "<level symbol>-<id>" (id =
1..count), e.g. "α-1".."α-9".

The table shows the five family-matching statistics from method.tex's
"Pairwise matching tolerances" table (H_norm, d̄m/S, A_h, B_h, O_LR) plus
H_hand as a descriptive diagnostic; the full component set of
D = (C_m, C_s, C_c) for saved sequences lives in the Sequence Metrics
window, which recomputes them with the exact same functions. After every generation run the difficulty
validation from method.tex (per-component median/IQR/range, monotonic
medians with <10% adjacent-pair violations, separate cross-region and
structural checks) runs automatically over the fresh pools; the "View
validation report" button opens the full report, which should be checked
before the pools are locked for the pilot study.

There's no profile picker - this always generates against config.json's
active_keyboard_profile (set by the Keyboard Calibration Wizard), so
there's no way to accidentally generate a sequence for the wrong physical
keyboard. START_NOTE/END_NOTE are actual MIDI note numbers, bounded by
whatever that profile's own midi_mapping.json covers (method.tex:
START_NOTE = min(V), END_NOTE = max(V), optionally narrowed).

Generation is seeded for reproducibility: the Seed field fixes the random
stream, so the same profile + note range + Count + seed + software
version regenerates the identical batch. A blank Seed draws a fresh one
and writes it back into the field, and every saved sequence's meta.json
records the seed and Count it came from (generation_seed /
generation_count), alongside the effective note bounds. The "New seed"
button draws a fresh seed up front and mirrors it into both the Seed and
the Batch name fields, so the batch's row names carry their own seed.

An optional Batch name prefixes every row's default name -
"<batch>-<level symbol>-<id>" instead of just "<level symbol>-<id>" - so a
whole generation run can be told apart from another one later. "Save All"
saves every row in one go, but first checks every target name for
conflicts (against data/sequence/ and against each other) and saves
nothing at all if even one conflict is found, rather than saving some and
not others. Saved rows land under data/sequence/<name>/{meta.json,
fingering.json} - the same layout a real recording produces under
data/music/ (app.music_recording) - so they show up, labelled
"sequence/<name>", in music_playback.py's and student_quiz(_haptic).py's
song pickers via app.song_library.
"""

import random
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..keyboard.midi_mapping import note_name as midi_note_name
from ..music_recording import list_songs
from ..profiles import DATA_DIR as PROFILE_DATA_DIR
from ..profiles import list_profiles_with_midi_mapping
from ..sequence_generator import (
    DEFAULT_FAMILY_COUNT,
    LEVEL_LABEL,
    LEVEL_SYMBOL,
    LEVELS,
    MAX_FAMILY_COUNT,
    MIN_FAMILY_COUNT,
    SEQUENCE_DATA_DIR,
    SEQUENCE_LENGTH,
    Sequence,
    SequenceStats,
    format_sequence_for_display,
    generate_all_matched_families,
    profile_note_range,
    save_sequence_as_song,
)
from ..stimulus_validation import ValidationReport, validate_level_pools
from .stimulus_validation_dialog import StimulusValidationDialog

# The five family-matching statistics from method.tex Table "Pairwise
# matching tolerances", plus H_hand - shown as a descriptive diagnostic
# only (it is neither a level constraint nor a matching tolerance; see
# app.sequence_generator.LEVEL_CONSTRAINTS) - as (header,
# SequenceStats.metric key) pairs.
DISPLAY_STATS = (
    ("H_norm", "h_norm"),
    ("d̄m/S", "d_m_mean_s"),
    ("A_h", "a_h"),
    ("B_h", "b_h"),
    ("O_LR", "o_lr"),
    ("H_hand (diag)", "h_hand"),
)

COLUMNS = ["Name", "Level", "Fingers", "Notes"] + [header for header, _ in DISPLAY_STATS] + [""]


class SequenceGeneratorWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Experiment Sequence Generator")
        self.cfg = cfg or Config.load()
        self.resize(1500, 900)  # the results table has up to 3 levels x Count rows - needs real room

        self._rows: Dict[int, Tuple[Sequence, str]] = {}  # table row -> (actions, level)
        self._generated_bounds: Optional[Tuple[int, int]] = None  # START/END notes of the last run
        self._generated_seed: Optional[int] = None  # RNG seed of the last run
        self._generated_count: Optional[int] = None  # per-level Count of the last run
        self._validation_report: Optional[ValidationReport] = None

        # ------------------------------------------------------------ setup
        self.profile_label = QLabel("")
        self.profile_label.setWordWrap(True)
        profile_refresh_btn = QPushButton("Refresh")
        profile_refresh_btn.clicked.connect(self._refresh_profile)

        self.start_spin = QSpinBox()
        self.end_spin = QSpinBox()

        self.count_spin = QSpinBox()
        self.count_spin.setRange(MIN_FAMILY_COUNT, MAX_FAMILY_COUNT)
        self.count_spin.setValue(DEFAULT_FAMILY_COUNT)

        # Reproducibility: the same seed with the same profile, note
        # range, Count, and software version regenerates the exact same
        # batch. Left blank, a fresh seed is drawn and written back into
        # this field, so every run is reproducible after the fact.
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("blank = draw one")

        seed_refresh_btn = QPushButton("New seed")
        seed_refresh_btn.setToolTip(
            "Draw a fresh random seed and fill it into both Seed and Batch name, "
            "so the saved rows' names carry the seed that generated them."
        )
        seed_refresh_btn.clicked.connect(self._draw_new_seed)

        self.batch_name_edit = QLineEdit()
        self.batch_name_edit.setPlaceholderText("optional - blank means <level>-<id>")

        # Prefill from config.json's stored default: reopening the window
        # keeps working with the same seed (and the matching batch name)
        # until "New seed" is pressed, which overwrites the stored default.
        if self.cfg.seeds.sequence_generator is not None:
            self.seed_edit.setText(str(self.cfg.seeds.sequence_generator))
            self.batch_name_edit.setText(str(self.cfg.seeds.sequence_generator))

        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_label, 1)
        profile_row.addWidget(profile_refresh_btn)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("START_NOTE:"))
        range_row.addWidget(self.start_spin)
        range_row.addWidget(QLabel("END_NOTE:"))
        range_row.addWidget(self.end_spin)
        range_row.addWidget(QLabel(f"Events per sequence: {SEQUENCE_LENGTH} (fixed)"))
        range_row.addWidget(QLabel("Count (per level):"))
        range_row.addWidget(self.count_spin)
        range_row.addWidget(QLabel("Seed:"))
        range_row.addWidget(self.seed_edit)
        range_row.addWidget(seed_refresh_btn)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("Batch name:"))
        batch_row.addWidget(self.batch_name_edit, 1)

        self.range_hint_label = QLabel("")
        self.range_hint_label.setWordWrap(True)

        self.generate_btn = QPushButton("Generate All Levels (α / β / γ)")
        self.generate_btn.clicked.connect(self._generate_all_levels)

        setup_box = QGroupBox("Stimulus setup")
        setup_layout = QVBoxLayout(setup_box)
        setup_layout.addLayout(profile_row)
        setup_layout.addLayout(range_row)
        setup_layout.addLayout(batch_row)
        setup_layout.addWidget(self.range_hint_label)
        setup_layout.addWidget(self.generate_btn)

        # ------------------------------------------------------------ table
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        # Excel-like columns: every border is user-draggable (Interactive).
        # The long Fingers/Notes columns get a wide starting width and the
        # table scrolls horizontally - Stretch mode would instead squeeze
        # them into whatever width the window leaves over, cutting them off
        # with no way to widen.
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(0, 180)  # Name
        self.table.setColumnWidth(2, 480)  # Fingers
        self.table.setColumnWidth(3, 640)  # Notes
        self.table.setColumnWidth(len(COLUMNS) - 1, 110)  # Save button
        self.table.verticalHeader().setVisible(False)

        self.validation_btn = QPushButton("View validation report")
        self.validation_btn.setEnabled(False)
        self.validation_btn.clicked.connect(self._show_validation_report)

        self.save_all_btn = QPushButton("Save All")
        self.save_all_btn.clicked.connect(self._save_all)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.validation_btn)
        buttons_row.addWidget(self.save_all_btn, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(setup_box)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons_row)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._refresh_profile()

    # ------------------------------------------------------------------
    # Profile handling
    # ------------------------------------------------------------------

    def _refresh_profile(self) -> None:
        """Always config.json's active_keyboard_profile - re-read here
        (rather than cached) so re-running the Keyboard Calibration/MIDI
        Mapping wizards earlier in the same launcher session is picked up
        without having to reopen this window."""
        profile = self.cfg.active_keyboard_profile
        valid = profile in list_profiles_with_midi_mapping(PROFILE_DATA_DIR)

        if valid:
            self.profile_label.setText(f"Keyboard profile: {profile} (config.json's active_keyboard_profile)")
        else:
            self.profile_label.setText(
                f"Keyboard profile {profile!r} (config.json's active_keyboard_profile) isn't calibrated and "
                "MIDI-mapped yet - run the Keyboard Calibration Wizard and the MIDI Mapping Wizard first."
            )
        self._apply_note_range(profile if valid else "")

    def _apply_note_range(self, keyboard_profile_name: str) -> None:
        """START_NOTE/END_NOTE are bounded by whatever notes actually
        appear in the active profile's midi_mapping.json."""
        lo, hi = 0, 0
        if keyboard_profile_name:
            try:
                lo, hi = profile_note_range(keyboard_profile_name, PROFILE_DATA_DIR)
            except Exception as exc:
                self.range_hint_label.setText(f"Couldn't read this profile's MIDI mapping: {exc}")

        has_range = hi > lo
        self.generate_btn.setEnabled(has_range)
        for spin in (self.start_spin, self.end_spin):
            spin.blockSignals(True)
            spin.setRange(lo, max(hi, lo))
            spin.blockSignals(False)
        self.start_spin.setValue(lo)
        self.end_spin.setValue(max(hi, lo))

        if has_range:
            self.range_hint_label.setText(
                f"Covers MIDI notes {lo} ({midi_note_name(lo)}) - {hi} ({midi_note_name(hi)}). START_NOTE/END_NOTE "
                "must fall within this range; only the white-key notes in between are used. The hand regions and "
                "span-normalised constraints are computed from the selected range."
            )
        else:
            self.range_hint_label.setText("")

    # ------------------------------------------------------------------
    # Generating
    # ------------------------------------------------------------------

    def _draw_new_seed(self) -> None:
        """Draw a fresh random seed and mirror it into both the Seed and
        the Batch name fields, so the next run's rows are named
        "<seed>-<level symbol>-<id>" and a saved batch carries the seed
        that generated it in its own name. The new seed also becomes
        config.json's stored default, so the window reopens with it."""
        seed = random.randrange(2**31)
        self.seed_edit.setText(str(seed))
        self.batch_name_edit.setText(str(seed))
        self.cfg.seeds.sequence_generator = seed
        self.cfg.save()

    def _generate_all_levels(self) -> None:
        keyboard_profile_name = self.cfg.active_keyboard_profile
        if keyboard_profile_name not in list_profiles_with_midi_mapping(PROFILE_DATA_DIR):
            QMessageBox.warning(
                self,
                "No usable keyboard profile",
                "config.json's active_keyboard_profile isn't calibrated and MIDI-mapped yet. Run the Keyboard "
                "Calibration Wizard and the MIDI Mapping Wizard first.",
            )
            return

        start_note = self.start_spin.value()
        end_note = self.end_spin.value()
        count = self.count_spin.value()

        if start_note > end_note:
            QMessageBox.warning(self, "Invalid range", "START_NOTE must be <= END_NOTE.")
            return

        seed_text = self.seed_edit.text().strip()
        if seed_text:
            try:
                seed = int(seed_text)
            except ValueError:
                QMessageBox.warning(self, "Invalid seed", "Seed must be an integer, or blank to draw a fresh one.")
                return
        else:
            # Draw a fresh seed and write it back into the field, so a run
            # started without an explicit seed is still reproducible.
            seed = random.randrange(2**31)
            self.seed_edit.setText(str(seed))

        self.status_label.setText(
            "Generating matched families for all three levels - Level α needs the most attempts, this can take "
            "tens of seconds..."
        )
        self.generate_btn.setEnabled(False)
        self.generate_btn.repaint()
        try:
            families, errors = generate_all_matched_families(
                keyboard_profile_name,
                start_note=start_note,
                end_note=end_note,
                count=count,
                rng=random.Random(seed),
            )
        finally:
            self.generate_btn.setEnabled(True)

        self._generated_bounds = (start_note, end_note)
        self._generated_seed = seed
        self._generated_count = count
        self._populate_table(families)

        # method.tex step 4: validate the freshly generated pools before
        # they can be locked for the pilot study.
        pools = {level: list(family.values()) for level, family in families.items()}
        self._validation_report = validate_level_pools(pools) if pools else None
        self.validation_btn.setEnabled(self._validation_report is not None)
        validation_note = ""
        if self._validation_report is not None:
            validation_note = f" Difficulty validation: {self._validation_report.conclusion.upper()} (see report)."

        ok_levels = ", ".join(LEVEL_LABEL[level] for level in LEVELS if level in families)
        settings_note = (
            f" over notes {start_note}-{end_note}, count {count}, seed {seed} on profile "
            f"'{keyboard_profile_name}' (same profile + range + count + seed reproduces this batch)"
        )
        if errors:
            failed = "; ".join(f"{LEVEL_LABEL[level]}: {msg}" for level, msg in errors.items())
            QMessageBox.warning(
                self,
                "Some levels couldn't be generated",
                f"Generated: {ok_levels or 'none'}.\n\nFailed:\n{failed}",
            )
            self.status_label.setText(
                f"Generated {ok_levels or 'no levels'}{settings_note} - see the warning dialog for what "
                f"failed.{validation_note}"
            )
        else:
            self.status_label.setText(
                f"Generated all three levels ({ok_levels}){settings_note}.{validation_note}"
            )

    def _populate_table(self, families: Dict[str, Dict[int, Tuple[Sequence, SequenceStats]]]) -> None:
        self.table.setRowCount(0)
        self._rows = {}

        batch_name = self.batch_name_edit.text().strip()

        for level in LEVELS:
            family = families.get(level)
            if family is None:
                continue
            for seq_id in sorted(family):
                actions, stats = family[seq_id]
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._rows[row] = (actions, level)

                base_name = f"{LEVEL_SYMBOL[level]}-{seq_id}"
                default_name = f"{batch_name}-{base_name}" if batch_name else base_name
                name_edit = QLineEdit(default_name)
                self.table.setCellWidget(row, 0, name_edit)

                level_item = QTableWidgetItem(LEVEL_SYMBOL[level])
                level_item.setFlags(level_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, 1, level_item)

                fingers, notes = format_sequence_for_display(actions)
                for col, text in enumerate([fingers, notes], start=2):
                    item = QTableWidgetItem(text)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, col, item)

                for offset, (_header, key) in enumerate(DISPLAY_STATS):
                    item = QTableWidgetItem(f"{stats.metric(key):.3f}")
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, 4 + offset, item)

                save_btn = QPushButton("Save as song")
                save_btn.clicked.connect(lambda _checked=False, r=row: self._save_row(r))
                self.table.setCellWidget(row, len(COLUMNS) - 1, save_btn)

        # Fit the short item columns (Level + stats) to their content;
        # Name/Fingers/Notes/button keep their user-adjustable widths.
        for col in range(1, len(COLUMNS) - 1):
            if col not in (2, 3):
                self.table.resizeColumnToContents(col)

    def _show_validation_report(self) -> None:
        if self._validation_report is None:
            return
        dialog = StimulusValidationDialog(self._validation_report, self)
        dialog.exec()

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _row_name(self, row: int) -> str:
        name_edit = self.table.cellWidget(row, 0)
        return name_edit.text().strip() if isinstance(name_edit, QLineEdit) else ""

    def _save_row_to_disk(self, row: int, name: str) -> None:
        actions, level = self._rows[row]
        start_note, end_note = self._generated_bounds if self._generated_bounds else (None, None)
        save_sequence_as_song(
            name,
            actions,
            level,
            self.cfg.active_keyboard_profile,
            start_note=start_note,
            end_note=end_note,
            generation_seed=self._generated_seed,
            generation_count=self._generated_count,
        )

    def _save_row(self, row: int) -> None:
        if row not in self._rows:
            return
        name = self._row_name(row)
        if not name:
            QMessageBox.warning(self, "Name needed", "Give this sequence a name before saving.")
            return

        if name in set(list_songs(SEQUENCE_DATA_DIR)):
            reply = QMessageBox.question(
                self,
                "Overwrite existing sequence?",
                f"A sequence named '{name}' already exists under data/sequence/. Overwrite it?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            self._save_row_to_disk(row, name)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't save sequence", str(exc))
            return

        self.status_label.setText(f"Saved '{name}' to data/sequence/ - it will now show up as a song in every tool.")

    def _save_all(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "Nothing to save", "Generate a batch first.")
            return

        rows = sorted(self._rows)
        missing = [row + 1 for row in rows if not self._row_name(row)]
        if missing:
            QMessageBox.warning(
                self,
                "Names needed",
                f"Row(s) {', '.join(map(str, missing))} have no name - give every row a name before saving all.",
            )
            return

        # A single conflict - either with an existing file, or between two
        # rows of this batch - cancels the whole save, per the "check
        # first, all-or-nothing" requirement, rather than saving some rows
        # and silently skipping/overwriting others.
        existing = set(list_songs(SEQUENCE_DATA_DIR))
        seen: Dict[str, int] = {}
        conflicts: List[str] = []
        for row in rows:
            name = self._row_name(row)
            if name in existing:
                conflicts.append(f"row {row + 1} ('{name}') already exists under data/sequence/")
            elif name in seen:
                conflicts.append(f"row {row + 1} ('{name}') duplicates row {seen[name] + 1} in this batch")
            seen.setdefault(name, row)

        if conflicts:
            QMessageBox.warning(
                self,
                "Name conflicts - nothing saved",
                "Rename these rows and try again:\n" + "\n".join(conflicts),
            )
            return

        saved = []
        for row in rows:
            name = self._row_name(row)
            try:
                self._save_row_to_disk(row, name)
                saved.append(name)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Couldn't save sequence",
                    f"Failed while saving '{name}': {exc}\n\n{len(saved)} sequence(s) were already saved before this.",
                )
                self.status_label.setText(f"Saved {len(saved)}/{len(rows)} before hitting an error on '{name}'.")
                return

        self.status_label.setText(
            f"Saved all {len(saved)} sequence(s) to data/sequence/ - they'll show up as songs in every tool."
        )
