"""Trial runner - the live window of the formal Main User Study
session.

student_quiz.QuizWindow re-purposed for scheduled trials: the sequence,
condition and participant are injected per trial by the session
controller (app/gui/experiment_session_window.py) instead of picked here,
and all three feedback conditions run through the session's ONE shared
ExperimentCue (app/gui/experiment_cue.py) - visual and haptic guidance
differ only in how that cue conveys the finger, exactly the CueOutput
seam app.quiz was designed around. What stays to do in this window is the
hardware setup the quizzes always needed: connect the LED strip, pick the
MIDI port/timbre/timeout, watch the camera view.

Each trial is recorded as a completely ordinary quiz under data/quiz/
(named "<participant>-T<index>-<condition><level>", "-r2"/"-r3" appended
on a rerun so no attempt's data is ever overwritten), so every existing
analysis tool - Quiz Analysis, review video - works on pilot trials
unchanged. The per-trial finger-matching popup is suppressed though:
mid-session it would steal focus between trials; analysis is batched
afterwards from the launcher's "7. Data Analysis".
"""

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QMessageBox

from student_quiz import QuizWindow

from ..config import Config
from ..quiz import quiz_dir, sanitize_quiz_name
from ..sequence_generator import LEVEL_DISPLAY
from ..song_library import SEQUENCE_LABEL_PREFIX
from .experiment_cue import ExperimentCue

# What each pilot condition (app.pilot_study.CONDITIONS) means for
# QuizMeta.guidance_type - B/C reuse the standalone quiz tools' values,
# "key-only" is new with the formal experiment.
CONDITION_GUIDANCE = {"A": "key-only", "B": "visual", "C": "haptic"}


class ExperimentRunnerWindow(QuizWindow):
    trial_finished = Signal(int)  # trial index - recorded, saved, quiz on disk
    trial_aborted = Signal(int)  # trial index - cancelled, nothing saved

    def __init__(self, cfg: Config, cue: ExperimentCue):
        # Stashed before super().__init__ because that's when _make_cue runs.
        self._shared_cue = cue
        super().__init__(cfg, guidance_type="visual")
        self.setWindowTitle("Main User Study - Trial Runner")
        self._trial_index: Optional[int] = None
        self._allow_close = False

        # Song + quiz name are driven by the schedule window per trial; they
        # stay visible as a readout of what's loaded but can't be edited.
        self.song_combo.setEnabled(False)
        self.quiz_name_edit.setEnabled(False)
        # Trials start from the schedule window (a row's Start button or
        # "Run selected"), never from here - Cancel stays available.
        self.start_btn.hide()

        self.trial_label = QLabel("No trial running - load a participant in the schedule window and start one there.")
        self.trial_label.setWordWrap(True)
        self.centralWidget().layout().insertWidget(0, self.trial_label)

    def _make_cue(self, guidance_type: str, cue_style: Optional[str]):
        # One persistent cue for the whole session, owned by the session
        # controller - NOT a fresh window/rig connection per trial.
        return self._shared_cue

    # ------------------------------------------------------------------
    # Driving from the session controller
    # ------------------------------------------------------------------

    def start_trial(self, doc: dict, trial: dict) -> Optional[str]:
        """Start one scheduled trial. Returns the quiz name it records
        under, or None if it couldn't start (a message box said why -
        LED not connected, no MIDI port, missing sequence, rig offline)."""
        if self.phase != "idle":
            QMessageBox.warning(self, "Trial already running", "Finish or cancel the current trial first.")
            return None

        label = f"{SEQUENCE_LABEL_PREFIX}/{trial['sequence']}"
        idx = self.song_combo.findText(label)
        if idx < 0:
            super()._refresh_songs()
            idx = self.song_combo.findText(label)
        if idx < 0:
            QMessageBox.warning(
                self, "Sequence missing", f"'{trial['sequence']}' was not found under data/sequence/."
            )
            return None
        self.song_combo.setCurrentIndex(idx)
        if not self.targets:
            return None  # _load_song already explained what failed

        quiz_name = self._unique_quiz_name(doc["participant"]["name"], trial)
        self.quiz_name_edit.setText(quiz_name)

        status = (
            f"Trial {trial['index']}/{len(doc['trials'])}  -  "
            f"Difficulty {LEVEL_DISPLAY[trial['level']]}  -  Condition {trial['condition']}"
        )
        try:
            self._shared_cue.set_condition(trial["condition"], status)
        except Exception as exc:
            QMessageBox.warning(self, "Vibration rig not available", str(exc))
            return None

        # Recorded into the trial's QuizMeta so analysis can group by how
        # the cue was actually delivered.
        self.guidance_type = CONDITION_GUIDANCE[trial["condition"]]
        self._trial_index = trial["index"]
        self.trial_label.setText(f"Running {status}  -  sequence '{trial['sequence']}'  -  quiz '{quiz_name}'")

        self._start_quiz()
        if self.phase == "idle":  # a _start_quiz guard refused (it said why)
            self._trial_index = None
            self.trial_label.setText("Trial did not start - fix the problem above and start it again.")
            self._shared_cue.set_idle("One moment please...")
            return None
        return quiz_name

    def _unique_quiz_name(self, participant: str, trial: dict) -> str:
        base = sanitize_quiz_name(f"{participant}-T{trial['index']:02d}-{trial['condition']}{trial['level_symbol']}")
        name, attempt = base, 1
        while quiz_dir(name).exists():
            attempt += 1
            name = f"{base}-r{attempt}"  # rerun after a crash/cancel keeps every attempt's data
        return name

    # ------------------------------------------------------------------
    # QuizWindow hook overrides
    # ------------------------------------------------------------------

    def _refresh_songs(self) -> None:
        # The base picks + loads the first list entry, which would swap the
        # targets out from under a running trial if the Refresh button were
        # clicked mid-run.
        if self.phase != "idle":
            return
        super()._refresh_songs()

    def _unlock_inputs(self) -> None:
        super()._unlock_inputs()
        self.song_combo.setEnabled(False)
        self.quiz_name_edit.setEnabled(False)

    def _open_analysis_window(self) -> None:
        # No per-trial analysis popup mid-session (see module docstring).
        index, self._trial_index = self._trial_index, None
        self._shared_cue.set_idle("Trial complete.\nPlease wait for the experimenter.")
        if index is not None:
            self.trial_label.setText(f"Trial {index} finished and saved.")
            self.trial_finished.emit(index)

    def _cancel_quiz(self) -> None:
        super()._cancel_quiz()
        index, self._trial_index = self._trial_index, None
        self._shared_cue.set_idle("One moment please...")
        if index is not None:
            self.trial_label.setText(f"Trial {index} cancelled - nothing was saved; it can be rerun.")
            self.trial_aborted.emit(index)

    def closeEvent(self, event) -> None:
        # Closing this window mid-session would tear down the camera AND the
        # shared cue window on the participant's screen. Its lifetime belongs
        # to the session controller, so a stray X-click just minimizes.
        if self._allow_close:
            super().closeEvent(event)
        else:
            event.ignore()
            self.showMinimized()
