"""Experiment Sequence Generator.

Builds the constrained motor-sequence stimuli described in
final_report_2026/method/method.tex ("Sequence Design and Difficulty
Levels"): click Generate and get a fresh matched group of sequences for
*every* difficulty level (alpha, beta, gamma) at once - there is no level
picker, since a stimulus set always needs all three. Each level's group
size is set by the Count field (default 9); every sequence in a level's
group is named "<level symbol>-<id>" (id = 1..count), e.g. "α-1".."α-9".
Save any row as a "song" under data/sequence/<name>/{meta.json,
fingering.json} - the same layout a real recording produces under
data/music/ (app.music_recording), just kept in its own top-level folder
so the two are easy to tell apart. That means a saved sequence shows up -
labelled "sequence/<name>", next to real recordings labelled "music/<name>"
- in music_playback.py's and student_quiz(_haptic).py's song pickers via
app.song_library, without either tool needing to know or care which
produced any given entry.

There's no profile picker either - this always generates against
config.json's active_keyboard_profile (set by the Keyboard Calibration
Wizard), so there's no way to accidentally generate a sequence for the
wrong physical keyboard. START_NOTE/END_NOTE are actual MIDI note numbers,
bounded by whatever that profile's own midi_mapping.json covers (every
note that appears anywhere in that file is valid; the min/max of those is
the profile's range - see app.sequence_generator.profile_note_range).

An optional Batch name prefixes every row's default name - "<batch>-<level
symbol>-<id>" instead of just "<level symbol>-<id>" - so a whole
generation run can be told apart from another one later. "Save All" saves
every row in one go, but first checks every target name for conflicts
(against data/sequence/ and against each other) and saves nothing at all
if even one conflict is found, rather than saving some and not others.
"""

from typing import Dict, Optional, Tuple

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
    DEFAULT_N_ACTIONS,
    LEVEL_LABEL,
    LEVEL_SYMBOL,
    LEVELS,
    MAX_FAMILY_COUNT,
    MAX_N_ACTIONS,
    MIN_FAMILY_COUNT,
    MIN_N_ACTIONS,
    SEQUENCE_DATA_DIR,
    Sequence,
    SequenceStats,
    format_sequence_for_display,
    generate_all_matched_families,
    profile_note_range,
    save_sequence_as_song,
)

COLUMNS = ["Name", "Level", "Fingers", "Notes", "H_norm", "Mean cost", "Hand-switch", ""]


class SequenceGeneratorWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Experiment Sequence Generator")
        self.cfg = cfg or Config.load()
        self.resize(1500, 900)  # the results table has up to 3 levels x Count rows - needs real room

        self._rows: Dict[int, Tuple[Sequence, str]] = {}  # table row -> (actions, level)

        # ------------------------------------------------------------ setup
        self.profile_label = QLabel("")
        self.profile_label.setWordWrap(True)
        profile_refresh_btn = QPushButton("Refresh")
        profile_refresh_btn.clicked.connect(self._refresh_profile)

        self.start_spin = QSpinBox()
        self.end_spin = QSpinBox()

        self.n_actions_spin = QSpinBox()
        self.n_actions_spin.setRange(MIN_N_ACTIONS, MAX_N_ACTIONS)
        self.n_actions_spin.setValue(DEFAULT_N_ACTIONS)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(MIN_FAMILY_COUNT, MAX_FAMILY_COUNT)
        self.count_spin.setValue(DEFAULT_FAMILY_COUNT)

        self.batch_name_edit = QLineEdit()
        self.batch_name_edit.setPlaceholderText("optional - blank means <level>-<id>")

        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_label, 1)
        profile_row.addWidget(profile_refresh_btn)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("START_NOTE:"))
        range_row.addWidget(self.start_spin)
        range_row.addWidget(QLabel("END_NOTE:"))
        range_row.addWidget(self.end_spin)
        range_row.addWidget(QLabel("Actions:"))
        range_row.addWidget(self.n_actions_spin)
        range_row.addWidget(QLabel("Count (per level):"))
        range_row.addWidget(self.count_spin)

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
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)

        self.save_all_btn = QPushButton("Save All")
        self.save_all_btn.clicked.connect(self._save_all)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(setup_box)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.save_all_btn)
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
                "must fall within this range; only the white-key notes in between are used."
            )
        else:
            self.range_hint_label.setText("")

    # ------------------------------------------------------------------
    # Generating
    # ------------------------------------------------------------------

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
        n_actions = self.n_actions_spin.value()
        count = self.count_spin.value()

        if start_note > end_note:
            QMessageBox.warning(self, "Invalid range", "START_NOTE must be <= END_NOTE.")
            return

        self.status_label.setText("Generating matched families for all three levels - this may take a moment...")
        self.generate_btn.setEnabled(False)
        try:
            families, errors = generate_all_matched_families(
                keyboard_profile_name, start_note=start_note, end_note=end_note, n_actions=n_actions, count=count
            )
        finally:
            self.generate_btn.setEnabled(True)

        self._populate_table(families)

        ok_levels = ", ".join(LEVEL_LABEL[level] for level in LEVELS if level in families)
        if errors:
            failed = "; ".join(f"{LEVEL_LABEL[level]}: {msg}" for level, msg in errors.items())
            QMessageBox.warning(
                self,
                "Some levels couldn't be generated",
                f"Generated: {ok_levels or 'none'}.\n\nFailed:\n{failed}",
            )
            self.status_label.setText(
                f"Generated {ok_levels or 'no levels'} over notes {start_note}-{end_note} on profile "
                f"'{keyboard_profile_name}' - see the warning dialog for what failed."
            )
        else:
            self.status_label.setText(
                f"Generated all three levels ({ok_levels}) over notes {start_note}-{end_note} on profile "
                f"'{keyboard_profile_name}'."
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

                for col, value in zip((4, 5, 6), (stats.h_norm, stats.mean_motor_cost, stats.hand_switch_prob)):
                    item = QTableWidgetItem(f"{value:.3f}")
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, col, item)

                save_btn = QPushButton("Save as song")
                save_btn.clicked.connect(lambda _checked=False, r=row: self._save_row(r))
                self.table.setCellWidget(row, len(COLUMNS) - 1, save_btn)

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _row_name(self, row: int) -> str:
        name_edit = self.table.cellWidget(row, 0)
        return name_edit.text().strip() if isinstance(name_edit, QLineEdit) else ""

    def _save_row(self, row: int) -> None:
        if row not in self._rows:
            return
        actions, level = self._rows[row]
        name = self._row_name(row)
        if not name:
            QMessageBox.warning(self, "Name needed", "Give this sequence a name before saving.")
            return

        keyboard_profile_name = self.cfg.active_keyboard_profile
        if name in set(list_songs(SEQUENCE_DATA_DIR)):
            reply = QMessageBox.question(
                self,
                "Overwrite existing sequence?",
                f"A sequence named '{name}' already exists under data/sequence/. Overwrite it?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            saved_dir = save_sequence_as_song(name, actions, level, keyboard_profile_name)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't save sequence", str(exc))
            return

        self.status_label.setText(f"Saved '{name}' to {saved_dir} - it will now show up as a song in every tool.")

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
        conflicts = []
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

        keyboard_profile_name = self.cfg.active_keyboard_profile
        saved = []
        for row in rows:
            name = self._row_name(row)
            actions, level = self._rows[row]
            try:
                save_sequence_as_song(name, actions, level, keyboard_profile_name)
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
