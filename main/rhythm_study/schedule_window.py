"""Rhythm Trial Schedule generator (launcher section 11).

Qt wrapper around :mod:`rhythm_study.schedule` (which holds all the
logic, GUI-free): fill in the participant's metadata, pick which melody
under ``data/rhythm_experiment/`` to lock them to, and generate the
19-trial schedule. The table is a preview of exactly what will run; Save
writes it to data/RhythmStudy/<participant>/TrialStructure.json.

Simpler than the Main User Study's equivalent
(``app/gui/pilot_schedule_window.py``, which this was copied from) in
one important way: the trial order here is **fixed by the design**, not
sampled, so there is no batch to choose, no seed to draw and nothing to
reproduce. The only real choice is which melody the participant learns -
and because they play the same one 19 times, that choice is made once,
here, and frozen into their file.

Every trial row in that file carries its own status/timestamps, so the
session controller (the next tool in this launcher section) updates the
same file as the session progresses and can resume from the first
non-completed trial after a crash - "Load existing" here shows any saved
participant's schedule with its live progress for exactly that reason.

Overwriting an existing participant's file regenerates the schedule and
therefore *discards their recorded progress* - the save path warns
before doing that.
"""

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

from app.config import Config

from .schedule import (
    HANDEDNESS_OPTIONS,
    PROBE_COUNT,
    SEX_OPTIONS,
    TOTAL_TRIALS,
    TRAINING_BLOCK_SIZE,
    TRAINING_TRIALS,
    TRIAL_STATUS_COMPLETED,
    TRIAL_STATUS_PENDING,
    RhythmStudyError,
    discover_melodies,
    list_participants,
    load_trial_structure,
    melody_summary,
    new_trial_structure,
    next_pending_trial,
    save_trial_structure,
    trial_structure_path,
)

COLUMNS = ["Trial", "Phase", "Backlight", "Haptic", "Melody", "Rest after", "Status"]

DESIGN_LINE = (
    f"Training x{TRAINING_BLOCK_SIZE} -> Probe 1 -> Training x{TRAINING_BLOCK_SIZE} -> Probe 2 -> "
    f"Training x{TRAINING_BLOCK_SIZE} -> Probe 3 -> Final test"
)


class RhythmScheduleWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Rhythm Experiment - Participant Trial Schedule")
        self.cfg = cfg or Config.load()
        self.resize(1100, 850)

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

        # ---------------------------------------------------- melody
        self.melody_combo = QComboBox()
        self.melody_combo.currentIndexChanged.connect(self._update_melody_hint)
        melody_refresh_btn = QPushButton("Refresh")
        melody_refresh_btn.clicked.connect(self._refresh_melodies)

        self.melody_hint_label = QLabel("")
        self.melody_hint_label.setWordWrap(True)

        self.generate_btn = QPushButton(f"Generate {TOTAL_TRIALS}-Trial Schedule")
        self.generate_btn.clicked.connect(self._generate)

        melody_row = QHBoxLayout()
        melody_row.addWidget(QLabel("Melody:"))
        melody_row.addWidget(self.melody_combo, 1)
        melody_row.addWidget(melody_refresh_btn)
        melody_row.addWidget(self.generate_btn)

        stimulus_box = QGroupBox("Melody && schedule")
        stimulus_layout = QVBoxLayout(stimulus_box)
        stimulus_layout.addLayout(melody_row)
        stimulus_layout.addWidget(self.melody_hint_label)

        # ---------------------------------------------------- schedule table
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(1, 220)  # Phase
        self.table.setColumnWidth(4, 220)  # Melody
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

        self._refresh_melodies()
        self._refresh_participants()

    # ------------------------------------------------------------------
    # Melodies
    # ------------------------------------------------------------------

    def _refresh_melodies(self) -> None:
        current = self.melody_combo.currentData()
        self.melody_combo.blockSignals(True)
        self.melody_combo.clear()
        for name in discover_melodies():
            self.melody_combo.addItem(name, name)
        if current is not None:
            idx = self.melody_combo.findData(current)
            if idx >= 0:
                self.melody_combo.setCurrentIndex(idx)
        self.melody_combo.blockSignals(False)
        self._update_melody_hint()

    def _update_melody_hint(self) -> None:
        name = self.melody_combo.currentData()
        if name is None:
            self.melody_hint_label.setText(
                "No melodies found under data/rhythm_experiment/ - generate and save one with the "
                "Rhythm Melody Generator first."
            )
            self.generate_btn.setEnabled(False)
            return
        try:
            summary = melody_summary(name)
        except RhythmStudyError as exc:
            self.melody_hint_label.setText(str(exc))
            self.generate_btn.setEnabled(False)
            return

        fingers = ", ".join(summary["fingers_used"]) or "no fingering"
        self.melody_hint_label.setText(
            f"{summary['note_count']} notes  |  {summary['key_display']}  |  {summary['layout']}  |  "
            f"{summary['bpm']:.0f} BPM  |  {summary['duration_s']:.1f} s  |  fingers: {fingers}\n"
            f"{DESIGN_LINE}  =  {TOTAL_TRIALS} trials "
            f"({TRAINING_TRIALS} training, {PROBE_COUNT} probes, 1 final test), all on this one melody. "
            "The order is fixed by the design - there is nothing random to seed."
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

    def _generate(self) -> None:
        melody = self.melody_combo.currentData()
        if melody is None:
            return
        try:
            self._doc = new_trial_structure(
                participant=self._participant_metadata(),
                melody=melody,
                keyboard_profile=self.cfg.active_keyboard_profile,
            )
        except RhythmStudyError as exc:
            QMessageBox.warning(self, "Couldn't build schedule", str(exc))
            return

        self._populate_table(self._doc)
        self.save_btn.setEnabled(True)
        self.status_label.setText(
            f"Generated the {TOTAL_TRIALS}-trial schedule on melody '{melody}'. "
            "Not saved yet - review it, then Save."
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
                f"{trial['phase_label']}  #{trial['phase_number']}",
                "on" if trial["backlight"] else "OFF",
                "on" if trial["haptic"] else "OFF",
                trial["melody"],
                "rest" if trial["rest_after"] else "",
                status_text,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)
        for col in range(len(COLUMNS)):
            if col not in (1, 4):
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
        self.status_label.setText(f"Saved to {saved_path} - the session window resumes from this file.")

    def _refresh_participants(self) -> None:
        self.load_combo.clear()
        self.load_combo.addItems(list_participants())

    def _load_existing(self) -> None:
        name = self.load_combo.currentText()
        if not name:
            QMessageBox.information(self, "Nothing to load", "No saved participants under data/RhythmStudy/.")
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
        idx = self.melody_combo.findData(doc.get("melody"))
        if idx >= 0:
            self.melody_combo.setCurrentIndex(idx)

        self._populate_table(doc)
        self.save_btn.setEnabled(True)

        progress = doc.get("progress", {})
        pending = next_pending_trial(doc)
        if pending is None:
            resume_note = "session finished."
        else:
            resume_note = f"resume from trial {pending['index']} ({pending['phase_label']})."
        self.status_label.setText(
            f"Loaded '{name}' (melody '{doc.get('melody')}'): "
            f"{progress.get('completed', 0)}/{progress.get('total', TOTAL_TRIALS)} trials completed - {resume_note}"
        )
