"""Formal experiment session for the Controlled Pilot Study.

The launcher's "Formal Experiment Session" button opens THREE windows at
once:

1. THIS window - the session controller. Load a saved participant's
   TrialStructure.json (made in the Participant Trial Schedule window);
   the table shows all trials with their live status. Every row has a
   tick box and a Start button: Start runs just that row's trial; "Run
   selected trials in order" walks every ticked trial
   smallest-index-first, pausing between trials until the experimenter
   presses Continue - with the method.tex 2-minute rest countdown shown
   after trials 9 and 18.

2. The trial runner (app/gui/experiment_runner_window.py) - camera view,
   LED/MIDI/timbre/timeout setup and the per-note cue/response loop.
   Connect the LED strip and pick the MIDI port there once; every trial
   records an ordinary quiz under data/quiz/.

3. The participant-facing cue screen (app/gui/experiment_cue.py) -
   deliberately created once for the whole session so it can be dragged
   onto the external display and fullscreened one time, instead of
   re-opening (on the wrong screen) every trial. Visual trials draw the
   finger cue on it; key-only and haptic trials show only a status line,
   so no visual finger cue leaks into those conditions.

Every status change is written straight back to the participant's
TrialStructure.json (app.pilot_study), so after a crash the same
participant reloads with the exact progress and the session resumes from
the first non-completed trial.
"""

from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..pilot_study import (
    CONDITION_LABEL,
    TRIAL_STATUS_COMPLETED,
    TRIAL_STATUS_IN_PROGRESS,
    list_participants,
    load_trial_structure,
    mark_trial_completed,
    mark_trial_started,
    save_trial_structure,
)
from .cue_window import CUE_STYLES, DEFAULT_CUE_STYLE
from .experiment_cue import ExperimentCue
from .experiment_runner_window import ExperimentRunnerWindow

COLUMNS = ["Run", "Trial", "Condition", "Level", "Sequence", "Rest after", "Status", "Action"]
CHECK_COL = 0
STATUS_COL = 6
ACTION_COL = 7


class ExperimentSessionWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Controlled Pilot Study - Formal Experiment Session")
        self.cfg = cfg or Config.load()
        self.resize(1200, 840)

        self._doc: Optional[dict] = None
        self._active_index: Optional[int] = None  # trial currently in the runner
        self._queue: List[int] = []  # remaining trial indices of a "Run selected" sequence
        self._last_finished: Optional[int] = None
        self._rest_remaining = 0

        # The other two session windows, up-front (see module docstring).
        cue_style = self.cfg.visual_cue_style if self.cfg.visual_cue_style in CUE_STYLES else DEFAULT_CUE_STYLE
        self.cue = ExperimentCue(cue_style)
        self.runner = ExperimentRunnerWindow(self.cfg, self.cue)
        self.runner.trial_finished.connect(self._on_trial_finished)
        self.runner.trial_aborted.connect(self._on_trial_aborted)
        self.runner.show()
        # The runner's constructor put its generic idle text on the cue
        # window; both windows exist now, so show placement instructions.
        self.cue.set_idle(
            "Cue screen\n\nDrag this window onto the participant-facing display\nand fullscreen it before starting trials."
        )

        self._rest_timer = QTimer(self)
        self._rest_timer.setInterval(1000)
        self._rest_timer.timeout.connect(self._rest_tick)

        # ---------------------------------------------------- participant
        self.participant_combo = QComboBox()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_participants)
        self.load_btn = QPushButton("Load participant")
        self.load_btn.clicked.connect(self._load_participant)

        participant_row = QHBoxLayout()
        participant_row.addWidget(QLabel("Saved participants:"))
        participant_row.addWidget(self.participant_combo, 1)
        participant_row.addWidget(refresh_btn)
        participant_row.addWidget(self.load_btn)

        self.info_label = QLabel(
            "Load a participant to see their schedule. (Schedules are created in the "
            "Participant Trial Schedule window and saved under data/ControlledPilotStudy/.)"
        )
        self.info_label.setWordWrap(True)

        participant_box = QGroupBox("Participant")
        participant_layout = QVBoxLayout(participant_box)
        participant_layout.addLayout(participant_row)
        participant_layout.addWidget(self.info_label)

        # ---------------------------------------------------- trial table
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(4, 260)  # Sequence
        self.table.verticalHeader().setVisible(False)

        # ---------------------------------------------------- run controls
        select_remaining_btn = QPushButton("Select remaining")
        select_remaining_btn.clicked.connect(lambda: self._set_all_checks(remaining_only=True))
        clear_btn = QPushButton("Clear selection")
        clear_btn.clicked.connect(lambda: self._set_all_checks(remaining_only=False))

        # One shortcut per feedback condition: tick exactly the trials of
        # that condition that haven't been completed yet.
        select_visual_btn = QPushButton("Select remaining Visual (B)")
        select_visual_btn.clicked.connect(lambda: self._select_condition("B"))
        select_haptic_btn = QPushButton("Select remaining Haptic (C)")
        select_haptic_btn.clicked.connect(lambda: self._select_condition("C"))
        select_key_only_btn = QPushButton("Select remaining Key-only (A)")
        select_key_only_btn.clicked.connect(lambda: self._select_condition("A"))

        select_row = QHBoxLayout()
        select_row.addWidget(select_remaining_btn)
        select_row.addWidget(select_visual_btn)
        select_row.addWidget(select_haptic_btn)
        select_row.addWidget(select_key_only_btn)
        select_row.addWidget(clear_btn)
        select_row.addStretch(1)

        self.run_selected_btn = QPushButton("Run selected trials in order")
        self.run_selected_btn.setEnabled(False)
        self.run_selected_btn.clicked.connect(self._run_selected)

        run_row = QHBoxLayout()
        run_row.addStretch(1)
        run_row.addWidget(self.run_selected_btn)

        # ------------------------------------------- between-trials panel
        self.between_label = QLabel("")
        self.between_label.setWordWrap(True)
        self.continue_btn = QPushButton("Continue to next trial")
        self.continue_btn.clicked.connect(self._continue_sequence)
        self.stop_btn = QPushButton("Stop sequence")
        self.stop_btn.clicked.connect(self._stop_sequence)

        between_buttons = QHBoxLayout()
        between_buttons.addWidget(self.continue_btn, 1)
        between_buttons.addWidget(self.stop_btn)

        self.between_box = QGroupBox("Between trials - manual confirmation")
        between_layout = QVBoxLayout(self.between_box)
        between_layout.addWidget(self.between_label)
        between_layout.addLayout(between_buttons)
        self.between_box.hide()

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(participant_box)
        layout.addWidget(self.table, 1)
        layout.addLayout(select_row)
        layout.addLayout(run_row)
        layout.addWidget(self.between_box)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._refresh_participants()

    # ------------------------------------------------------------------
    # Participant loading
    # ------------------------------------------------------------------

    def _refresh_participants(self) -> None:
        current = self.participant_combo.currentText()
        self.participant_combo.clear()
        self.participant_combo.addItems(list_participants())
        idx = self.participant_combo.findText(current)
        if idx >= 0:
            self.participant_combo.setCurrentIndex(idx)

    def _load_participant(self) -> None:
        if self._busy():
            QMessageBox.warning(self, "Session running", "Finish or stop the current trial/sequence first.")
            return
        name = self.participant_combo.currentText()
        if not name:
            QMessageBox.information(
                self, "Nothing to load", "No saved participants under data/ControlledPilotStudy/."
            )
            return
        try:
            doc = load_trial_structure(name)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't load", str(exc))
            return

        self._doc = doc
        self._queue = []
        self._populate_table()
        self._update_info()
        self._update_controls()
        pending = next((t for t in doc["trials"] if t["status"] != TRIAL_STATUS_COMPLETED), None)
        if pending is None:
            self.status_label.setText("All trials completed for this participant.")
        else:
            self.status_label.setText(
                f"Ready. Next non-completed trial is {pending['index']} ({pending['sequence']}). "
                "Connect the LED strip and pick the MIDI port in the Trial Runner window before starting."
            )

    def _update_info(self) -> None:
        doc = self._doc
        participant = doc.get("participant", {})
        progress = doc.get("progress", {})
        # Trials always run with config.json's active_keyboard_profile (the
        # trial runner loads MIDI mapping / LED layout from it, same as every
        # quiz). TrialStructure.json's stored keyboard_profile is deliberately
        # NOT used here - it only records which profile the sequence
        # generator's key range was limited to when the schedule was made.
        parts = [
            f"{participant.get('name', '?')} - {participant.get('sex', '?')}, age {participant.get('age', '?')}, "
            f"{participant.get('handedness', '?')}-handed, {participant.get('piano_experience_years', 0)} yr piano",
            f"batch '{doc.get('sequence_batch')}', seed {doc.get('schedule_seed')}",
            f"{progress.get('completed', 0)}/{progress.get('total', 0)} trials completed",
            f"trials run with keyboard profile '{self.cfg.active_keyboard_profile}' (config.json's active profile)",
        ]
        self.info_label.setText("  |  ".join(parts))

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _populate_table(self) -> None:
        doc = self._doc
        self.table.setRowCount(0)
        for trial in doc["trials"]:
            row = self.table.rowCount()
            self.table.insertRow(row)

            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            done = trial["status"] == TRIAL_STATUS_COMPLETED
            check.setCheckState(Qt.CheckState.Unchecked if done else Qt.CheckState.Checked)
            self.table.setItem(row, CHECK_COL, check)

            cells = [
                str(trial["index"]),
                f"{trial['condition']} - {CONDITION_LABEL[trial['condition']]}",
                trial["level_symbol"],
                trial["sequence"],
                f"{doc.get('rest_duration_s', 120) // 60} min rest" if trial["rest_after"] else "",
            ]
            for col, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)

            status_item = QTableWidgetItem("")
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, STATUS_COL, status_item)

            start_btn = QPushButton("Start")
            start_btn.clicked.connect(lambda _checked=False, idx=trial["index"]: self._start_single(idx))
            self.table.setCellWidget(row, ACTION_COL, start_btn)

            self._update_row(trial)
        for col in range(len(COLUMNS)):
            if col != 4:
                self.table.resizeColumnToContents(col)

    def _update_row(self, trial: dict) -> None:
        row = trial["index"] - 1  # trials are stored and shown in index order
        if trial["index"] == self._active_index:
            text = "▶ running..."
        elif trial["status"] == TRIAL_STATUS_COMPLETED:
            text = "completed"
        elif trial["status"] == TRIAL_STATUS_IN_PROGRESS:
            text = "in progress (rerun)"  # a crashed/cancelled attempt - run it again
        else:
            text = ""
        self.table.item(row, STATUS_COL).setText(text)

    def _set_all_checks(self, remaining_only: bool) -> None:
        if self._doc is None:
            return
        for trial in self._doc["trials"]:
            item = self.table.item(trial["index"] - 1, CHECK_COL)
            checked = remaining_only and trial["status"] != TRIAL_STATUS_COMPLETED
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _select_condition(self, condition: str) -> None:
        """Tick exactly the not-yet-completed trials of one feedback
        condition (A key-only / B visual / C haptic), unticking the rest."""
        if self._doc is None:
            return
        for trial in self._doc["trials"]:
            item = self.table.item(trial["index"] - 1, CHECK_COL)
            checked = trial["condition"] == condition and trial["status"] != TRIAL_STATUS_COMPLETED
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _selected_indices(self) -> List[int]:
        indices = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, CHECK_COL).checkState() == Qt.CheckState.Checked:
                indices.append(row + 1)
        return indices  # already ascending - rows are in trial-index order

    # ------------------------------------------------------------------
    # Starting trials
    # ------------------------------------------------------------------

    def _busy(self) -> bool:
        return self._active_index is not None or bool(self._queue) or self.between_box.isVisible()

    def _trial(self, index: int) -> dict:
        return next(t for t in self._doc["trials"] if t["index"] == index)

    def _start_single(self, index: int) -> None:
        if self._doc is None or self._busy():
            return
        trial = self._trial(index)
        if trial["status"] == TRIAL_STATUS_COMPLETED:
            reply = QMessageBox.question(
                self,
                "Trial already completed",
                f"Trial {index} is already completed. Run it again? (Earlier recordings are kept; "
                "the extra run is saved as a new quiz.)",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._launch_trial(index)

    def _run_selected(self) -> None:
        if self._doc is None or self._busy():
            return
        indices = self._selected_indices()
        if not indices:
            QMessageBox.information(self, "Nothing selected", "Tick the trials to run first.")
            return
        self._queue = indices
        if self._launch_trial(self._queue[0]):
            self._queue.pop(0)
        else:
            self._show_retry_panel(self._queue[0])

    def _launch_trial(self, index: int) -> bool:
        trial = self._trial(index)
        self.runner.showNormal()
        self.runner.raise_()
        quiz_name = self.runner.start_trial(self._doc, trial)
        if quiz_name is None:
            return False

        # Which quiz folder(s) hold this trial's recordings - the latest
        # one plus the full rerun history - for the analysis stage.
        trial["quiz_name"] = quiz_name
        trial.setdefault("quiz_attempts", []).append(quiz_name)
        mark_trial_started(self._doc, index)
        save_trial_structure(self._doc)

        self._active_index = index
        self._update_row(trial)
        self._update_info()
        self._update_controls()
        remaining = f" ({len(self._queue)} more queued)" if self._queue else ""
        self.status_label.setText(f"Trial {index} running as quiz '{quiz_name}'{remaining}.")
        return True

    def _update_controls(self) -> None:
        busy = self._busy()
        loaded = self._doc is not None
        self.run_selected_btn.setEnabled(loaded and not busy)
        self.load_btn.setEnabled(not busy)
        self.participant_combo.setEnabled(not busy)
        for row in range(self.table.rowCount()):
            btn = self.table.cellWidget(row, ACTION_COL)
            if btn is not None:
                btn.setEnabled(not busy)

    # ------------------------------------------------------------------
    # Runner callbacks + sequence flow
    # ------------------------------------------------------------------

    def _on_trial_finished(self, index: int) -> None:
        mark_trial_completed(self._doc, index)
        save_trial_structure(self._doc)
        trial = self._trial(index)
        self._active_index = None
        self._last_finished = index
        self._update_row(trial)
        self._update_info()

        if self._queue:
            self._show_between_panel(trial)
        else:
            done = self._doc["progress"]["finished"]
            note = "Session finished - every trial is completed." if done else f"Trial {index} completed."
            if trial["rest_after"] and not done:
                note += f" Scheduled {self._doc.get('rest_duration_s', 120) // 60}-min rest is due now."
            self.status_label.setText(note)
            self.cue.set_idle("Well done!\nPlease wait for the experimenter." if done else "Short break.")
        self._update_controls()

    def _on_trial_aborted(self, index: int) -> None:
        # start_trial marked it in_progress; leave that so the file shows a
        # rerun is needed, and drop any queued sequence - cancelling is the
        # experimenter saying "stop, something's wrong".
        save_trial_structure(self._doc)
        trial = self._trial(index)
        self._active_index = None
        self._queue = []
        self._rest_timer.stop()
        self.between_box.hide()
        self._update_row(trial)
        self._update_info()
        self._update_controls()
        self.status_label.setText(
            f"Trial {index} cancelled - it stays marked 'in progress (rerun)' and can be started again."
        )

    def _show_between_panel(self, finished_trial: dict) -> None:
        if finished_trial["rest_after"]:
            self._rest_remaining = int(self._doc.get("rest_duration_s", 120))
            self._rest_timer.start()
            self.cue.set_idle("Rest break.\nPlease relax - the next trial starts in a couple of minutes.")
        else:
            self._rest_remaining = 0
            self.cue.set_idle("Short break.\nThe next trial starts soon.")
        self.continue_btn.setText(f"Continue to trial {self._queue[0]}")
        self._refresh_between_label()
        self.between_box.show()
        self._update_controls()

    def _refresh_between_label(self) -> None:
        next_trial = self._trial(self._queue[0])
        parts = []
        if self._last_finished is not None:
            parts.append(f"Trial {self._last_finished} completed.")
        if self._rest_remaining > 0:
            minutes, seconds = divmod(self._rest_remaining, 60)
            parts.append(f"Scheduled rest: {minutes}:{seconds:02d} remaining.")
        elif self._last_finished is not None and self._trial(self._last_finished)["rest_after"]:
            parts.append("Rest finished.")
        parts.append(
            f"Next: trial {next_trial['index']} - Condition {next_trial['condition']} "
            f"({CONDITION_LABEL[next_trial['condition']]}), Level {next_trial['level_symbol']}, "
            f"sequence '{next_trial['sequence']}'."
        )
        parts.append("Press Continue when the participant is ready.")
        self.between_label.setText("\n".join(parts))

    def _show_retry_panel(self, index: int) -> None:
        """A queued trial refused to start (LED/MIDI/rig problem - the
        runner said which). Keep the queue so fixing the problem and
        pressing Continue retries instead of abandoning the sequence."""
        self._rest_remaining = 0
        self._rest_timer.stop()
        trial = self._trial(index)
        self.continue_btn.setText(f"Retry trial {index}")
        self.between_label.setText(
            f"Trial {index} (Condition {trial['condition']}, sequence '{trial['sequence']}') could not start.\n"
            "Fix the problem in the Trial Runner window (LED / MIDI port / vibration rig), then press "
            "Retry - or Stop sequence to give up."
        )
        self.between_box.show()
        self._update_controls()

    def _continue_sequence(self) -> None:
        if not self._queue:
            self.between_box.hide()
            self._update_controls()
            return
        self._rest_timer.stop()
        index = self._queue[0]
        if self._launch_trial(index):
            self._queue.pop(0)
            self.between_box.hide()
        else:
            self._show_retry_panel(index)

    def _stop_sequence(self) -> None:
        self._rest_timer.stop()
        self._queue = []
        self.between_box.hide()
        self._update_controls()
        self.cue.set_idle("One moment please...")
        self.status_label.setText("Sequence stopped - remaining trials stay as they are.")

    def _rest_tick(self) -> None:
        if self._rest_remaining > 0:
            self._rest_remaining -= 1
            if self._queue:
                self._refresh_between_label()
        if self._rest_remaining <= 0:
            self._rest_timer.stop()

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._rest_timer.stop()
        # The runner refuses close while the session owns it - lift that,
        # then close it (its closeEvent also closes the shared cue).
        self.runner._allow_close = True
        self.runner.close()
        self.cue.close()
        super().closeEvent(event)
