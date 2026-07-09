"""Participant Trial Schedule generator for the Controlled Pilot Study.

Qt wrapper around app.pilot_study (which holds all the logic, GUI-free):
fill in the participant's metadata, pick which generated stimulus batch
under data/sequence/ to lock them to, and generate the 27-trial
randomised schedule from method.tex "Trial Structure" - 3 conditions x 3
levels x 3 unique sequences per cell, interleaved by a seeded shuffle,
with the two mandatory 2-min rests after trials 9 and 18 marked inline.
The table is a preview of exactly what will run; Save writes it to
data/ControlledPilotStudy/<participant>/TrialStructure.json.

Every trial row in that file carries its own status/timestamps, so the
experiment runner (the next tool in this launcher section) updates the
same file as the session progresses and can resume from the first
non-completed trial after a crash - "Load existing" here shows any saved
participant's schedule with its live progress for exactly that reason.

Overwriting an existing participant's file regenerates the schedule and
therefore *discards their recorded progress* - the save path warns before
doing that.
"""

import random
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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
from ..pilot_study import (
    CONDITION_LABEL,
    HANDEDNESS_OPTIONS,
    PilotStudyError,
    SEX_OPTIONS,
    TOTAL_TRIALS,
    TRIAL_STATUS_COMPLETED,
    TRIAL_STATUS_PENDING,
    discover_sequence_batches,
    list_participants,
    load_trial_structure,
    new_trial_structure,
    next_pending_trial,
    save_trial_structure,
    trial_structure_path,
)
from ..sequence_generator import LEVEL_SYMBOL, LEVELS

COLUMNS = ["Trial", "Condition", "Level", "Sequence", "Rest after", "Status"]


class PilotScheduleWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Controlled Pilot Study - Participant Trial Schedule")
        self.cfg = cfg or Config.load()
        self.resize(1100, 850)

        self._batches: dict = {}
        self._doc: Optional[dict] = None  # the schedule currently shown in the table

        # ---------------------------------------------------- participant
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. P01")

        self.sex_combo = QComboBox()
        for sex in SEX_OPTIONS:
            self.sex_combo.addItem(sex.capitalize(), sex)

        self.age_spin = QSpinBox()
        self.age_spin.setRange(16, 99)
        self.age_spin.setValue(20)

        self.handedness_combo = QComboBox()
        for hand in HANDEDNESS_OPTIONS:
            self.handedness_combo.addItem(hand.capitalize(), hand)

        # Piano/keyboard experience matters for a beginner-training study,
        # so it is recorded alongside the thesis's demographics.
        self.experience_spin = QSpinBox()
        self.experience_spin.setRange(0, 50)
        self.experience_spin.setSuffix(" years")

        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("optional - anything worth recording (eligibility notes, etc.)")

        participant_box = QGroupBox("Participant metadata")
        participant_layout = QVBoxLayout(participant_box)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Name:"))
        row1.addWidget(self.name_edit, 1)
        row1.addWidget(QLabel("Biological sex:"))
        row1.addWidget(self.sex_combo)
        row1.addWidget(QLabel("Age:"))
        row1.addWidget(self.age_spin)
        row1.addWidget(QLabel("Handedness:"))
        row1.addWidget(self.handedness_combo)
        row1.addWidget(QLabel("Piano experience:"))
        row1.addWidget(self.experience_spin)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Notes:"))
        row2.addWidget(self.notes_edit, 1)
        participant_layout.addLayout(row1)
        participant_layout.addLayout(row2)

        # ---------------------------------------------------- stimulus set
        self.batch_combo = QComboBox()
        self.batch_combo.currentIndexChanged.connect(self._update_batch_hint)
        batch_refresh_btn = QPushButton("Refresh")
        batch_refresh_btn.clicked.connect(self._refresh_batches)

        self.batch_hint_label = QLabel("")
        self.batch_hint_label.setWordWrap(True)

        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("blank = draw one")
        # Prefill from config.json's stored default: reopening the window
        # keeps working with the same seed until "New seed" overwrites it.
        if self.cfg.seeds.pilot_schedule is not None:
            self.seed_edit.setText(str(self.cfg.seeds.pilot_schedule))

        seed_refresh_btn = QPushButton("New seed")
        seed_refresh_btn.setToolTip(
            "Draw a fresh random seed for the schedule shuffle and store it as config.json's new default."
        )
        seed_refresh_btn.clicked.connect(self._draw_new_seed)

        self.generate_btn = QPushButton(f"Generate {TOTAL_TRIALS}-Trial Schedule")
        self.generate_btn.clicked.connect(self._generate)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("Sequence batch:"))
        batch_row.addWidget(self.batch_combo, 1)
        batch_row.addWidget(batch_refresh_btn)
        batch_row.addWidget(QLabel("Schedule seed:"))
        batch_row.addWidget(self.seed_edit)
        batch_row.addWidget(seed_refresh_btn)
        batch_row.addWidget(self.generate_btn)

        stimulus_box = QGroupBox("Stimulus set && schedule")
        stimulus_layout = QVBoxLayout(stimulus_box)
        stimulus_layout.addLayout(batch_row)
        stimulus_layout.addWidget(self.batch_hint_label)

        # ---------------------------------------------------- schedule table
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(3, 260)  # Sequence
        self.table.verticalHeader().setVisible(False)

        # ---------------------------------------------------- save / load
        self.save_btn = QPushButton("Save TrialStructure.json")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)

        self.load_combo = QComboBox()
        load_btn = QPushButton("Load existing")
        load_btn.clicked.connect(self._load_existing)

        save_row = QHBoxLayout()
        save_row.addWidget(self.save_btn, 1)
        save_row.addWidget(QLabel("Saved participants:"))
        save_row.addWidget(self.load_combo, 1)
        save_row.addWidget(load_btn)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(participant_box)
        layout.addWidget(stimulus_box)
        layout.addWidget(self.table, 1)
        layout.addLayout(save_row)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._refresh_batches()
        self._refresh_participants()

    # ------------------------------------------------------------------
    # Stimulus batches
    # ------------------------------------------------------------------

    def _refresh_batches(self) -> None:
        self._batches = discover_sequence_batches()
        current = self.batch_combo.currentData()
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        for batch in sorted(self._batches):
            pools = self._batches[batch]
            counts = " ".join(f"{LEVEL_SYMBOL[lvl]}:{len(pools[lvl])}" for lvl in LEVELS)
            self.batch_combo.addItem(f"{batch}  ({counts})", batch)
        if current is not None:
            idx = self.batch_combo.findData(current)
            if idx >= 0:
                self.batch_combo.setCurrentIndex(idx)
        self.batch_combo.blockSignals(False)
        self._update_batch_hint()

    def _update_batch_hint(self) -> None:
        batch = self.batch_combo.currentData()
        if batch is None:
            self.batch_hint_label.setText(
                "No generator-named sequences (\"<batch>-<level>-<id>\") found under data/sequence/ - generate "
                "and save a batch with the Experiment Sequence Generator first."
            )
            self.generate_btn.setEnabled(False)
            return
        pools = self._batches[batch]
        short = [lvl for lvl in LEVELS if len(pools[lvl]) < 3]
        if short:
            missing = ", ".join(f"Level {LEVEL_SYMBOL[lvl]} has {len(pools[lvl])}" for lvl in short)
            self.batch_hint_label.setText(f"Batch unusable: every level needs >= 3 sequences ({missing}).")
            self.generate_btn.setEnabled(False)
        else:
            reuse_note = (
                "each sequence is used at most once across the whole session"
                if all(len(pools[lvl]) >= 9 for lvl in LEVELS)
                else "fewer than 9 per level: sequences stay unique within a cell but repeat across conditions"
            )
            self.batch_hint_label.setText(
                f"3 conditions x 3 levels x 3 unique sequences per cell = {TOTAL_TRIALS} trials; {reuse_note}. "
                "2-min rests are scheduled after trials 9 and 18."
            )
            self.generate_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Generating
    # ------------------------------------------------------------------

    def _participant_metadata(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "sex": self.sex_combo.currentData(),
            "age": self.age_spin.value(),
            "handedness": self.handedness_combo.currentData(),
            "piano_experience_years": self.experience_spin.value(),
            "notes": self.notes_edit.text().strip(),
        }

    def _draw_new_seed(self) -> None:
        """Draw a fresh schedule seed and store it as config.json's new
        default, so the window reopens with it until the next refresh."""
        seed = random.randrange(2**31)
        self.seed_edit.setText(str(seed))
        self.cfg.seeds.pilot_schedule = seed
        self.cfg.save()

    def _generate(self) -> None:
        batch = self.batch_combo.currentData()
        if batch is None:
            return

        seed_text = self.seed_edit.text().strip()
        if seed_text:
            try:
                seed = int(seed_text)
            except ValueError:
                QMessageBox.warning(self, "Invalid seed", "Seed must be an integer, or blank to draw a fresh one.")
                return
        else:
            seed = random.randrange(2**31)
            self.seed_edit.setText(str(seed))

        try:
            self._doc = new_trial_structure(
                participant=self._participant_metadata(),
                sequence_batch=batch,
                pools=self._batches[batch],
                seed=seed,
                keyboard_profile=self.cfg.active_keyboard_profile,
            )
        except PilotStudyError as exc:
            QMessageBox.warning(self, "Couldn't build schedule", str(exc))
            return

        self._populate_table(self._doc)
        self.save_btn.setEnabled(True)
        self.status_label.setText(
            f"Generated a {TOTAL_TRIALS}-trial schedule from batch '{batch}' with seed {seed} "
            "(same batch + seed reproduces this order). Not saved yet - review it, then Save."
        )

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _populate_table(self, doc: dict) -> None:
        self.table.setRowCount(0)
        for trial in doc["trials"]:
            row = self.table.rowCount()
            self.table.insertRow(row)
            status = trial["status"]
            if status == TRIAL_STATUS_COMPLETED:
                status_text = "completed"
            elif status == TRIAL_STATUS_PENDING:
                status_text = ""
            else:
                status_text = status  # in_progress = where a crashed session stopped
            cells = [
                str(trial["index"]),
                f"{trial['condition']} - {CONDITION_LABEL[trial['condition']]}",
                trial["level_symbol"],
                trial["sequence"],
                f"{doc.get('rest_duration_s', 120) // 60} min rest" if trial["rest_after"] else "",
                status_text,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)
        for col in range(len(COLUMNS)):
            if col != 3:
                self.table.resizeColumnToContents(col)

    # ------------------------------------------------------------------
    # Saving / loading
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self._doc is None:
            return
        # Re-read the metadata fields so edits made after Generate still land
        # in the saved file - the schedule itself is not regenerated.
        self._doc["participant"] = self._participant_metadata()
        name = self._doc["participant"]["name"]
        if not name:
            QMessageBox.warning(self, "Name needed", "Fill in the participant name before saving.")
            return

        path = trial_structure_path(name)
        if path.exists():
            reply = QMessageBox.question(
                self,
                "Overwrite existing participant?",
                f"{path} already exists. Overwriting replaces their schedule AND discards any recorded "
                "experiment progress. Overwrite?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            saved_path = save_trial_structure(self._doc)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't save", str(exc))
            return
        self._refresh_participants()
        self.status_label.setText(f"Saved to {saved_path} - the experiment runner resumes from this file.")

    def _refresh_participants(self) -> None:
        self.load_combo.clear()
        self.load_combo.addItems(list_participants())

    def _load_existing(self) -> None:
        name = self.load_combo.currentText()
        if not name:
            QMessageBox.information(self, "Nothing to load", "No saved participants under data/ControlledPilotStudy/.")
            return
        try:
            doc = load_trial_structure(name)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't load", str(exc))
            return

        self._doc = doc
        participant = doc.get("participant", {})
        self.name_edit.setText(participant.get("name", name))
        idx = self.sex_combo.findData(participant.get("sex"))
        self.sex_combo.setCurrentIndex(max(idx, 0))
        self.age_spin.setValue(int(participant.get("age", self.age_spin.minimum())))
        idx = self.handedness_combo.findData(participant.get("handedness"))
        self.handedness_combo.setCurrentIndex(max(idx, 0))
        self.experience_spin.setValue(int(participant.get("piano_experience_years", 0)))
        self.notes_edit.setText(participant.get("notes", ""))
        self.seed_edit.setText(str(doc.get("schedule_seed", "")))

        self._populate_table(doc)
        self.save_btn.setEnabled(True)

        progress = doc.get("progress", {})
        pending = next_pending_trial(doc)
        if pending is None:
            resume_note = "session finished."
        else:
            resume_note = f"resume from trial {pending['index']} ({pending['sequence']})."
        self.status_label.setText(
            f"Loaded '{name}' (batch '{doc.get('sequence_batch')}', seed {doc.get('schedule_seed')}): "
            f"{progress.get('completed', 0)}/{progress.get('total', TOTAL_TRIALS)} trials completed - {resume_note}"
        )
