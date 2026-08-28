"""Trial runner - the live window of the rhythm experiment session.

``student_quiz.QuizWindow`` re-purposed for this study's trials, the same
way ``app/gui/experiment_runner_window.py`` re-purposes it for the Main
User Study: the melody, the phase and the participant are injected per
trial by the session controller (``rhythm_study/session_window.py``)
instead of picked here. What stays in this window is the hardware setup
the quizzes always needed - connect the LED strip, pick the MIDI
port/timbre, watch the camera view.

Four things behave differently from every other quiz in this project,
and they are the whole reason this class exists.

1. STIMULI COME FROM A MELODY, NOT FROM THE SONG LIBRARY
   The quizzes load targets through ``app.song_library`` out of
   ``data/music/`` or ``data/sequence/``. A rhythm melody is in neither -
   it is deliberately not registered with the song library, so it can
   never turn up in the main study's song pickers. `start_trial` builds
   the QuizTarget list straight from the melody's own .json via
   ``melody_generator.load`` instead.

2. A TIMEOUT RE-CUES INSTEAD OF SCORING A MISS
   The standalone quizzes record ``timed_out=True`` after `timeout_s` and
   move to the next note. Here the note does not advance until a key is
   pressed: on a timeout the cue is simply issued again (LED relit,
   motor re-buzzed) and the wait restarts. Any key press advances -
   pressing the *wrong* key is still an answer, exactly as in the
   standalone quizzes; only "no press at all" triggers a re-cue.

   The re-cue count and the first cue's timestamp go into a **sidecar**
   file, not into results.json - see `_save_recue_sidecar`.

3. A TRAINING CUE LASTS THE NOTE, NOT THE KEY PRESS
   Every other quiz clears the cue the instant a response is recorded.
   That teaches the onset and nothing else: a 3-beat note and a 1-beat
   note feel identical. Here the buzz and the lit key continue past the
   press and stop when the beat ends, so the participant feels how long
   a note is. The hold is measured from the key press - see
   `_begin_sustain` for why, and for what stops it.

4. ONLY TRAINING IS A CUE/RESPONSE TASK
   A probe and the final test are played as whole performances against
   the melody's own time grid, which is what makes their onset and
   duration errors real measurements rather than reaction times. During
   a probe the backlight walks that grid; in the final test the grid
   runs invisibly, purely as the reference the performance is scored
   against. See the "played as whole performances" section below.

Each trial is recorded as a completely ordinary quiz under ``data/quiz/``
named "rhythm-<participant>-T<NN>" ("-r2"/"-r3" appended on a rerun so no
attempt's data is ever overwritten), so every existing analysis tool -
Quiz Analysis, review video - works on it unchanged. The per-trial
finger-matching popup is suppressed: mid-session it would steal focus
between trials; analysis is batched afterwards.
"""

import json
import time
from typing import List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton

from student_quiz import COUNTDOWN_S, QuizWindow

from app.config import Config
from app.keyboard.midi_mapping import MidiMapping
from app.profiles import DATA_DIR as PROFILE_DATA_DIR
from app.quiz import QuizResult, QuizTarget, quiz_dir, sanitize_quiz_name

from .cue import RhythmCue
from .schedule import (
    PHASE_TRAINING,
    RhythmStudyError,
    load_trial_melody,
    trial_quiz_base_name,
)

# Written into QuizMeta.guidance_type so analysis can group trials by the
# guidance that was actually delivered. As in the main study the label
# names the channel added on top of the ever-present key LED, so training
# is "haptic". A probe is deliberately NOT called "key-only": the main
# study's key-only condition lights the next key and waits, whereas here
# the backlight plays the melody on its own clock and the participant
# follows it - the same hardware doing a different job, and one the
# analysis must not silently pool with the other.
PHASE_GUIDANCE_TYPE = {
    "training": "haptic",
    "probe": "backlight",
    "final": "unguided",
}

# Sidecar holding what an ordinary QuizResult has no field for. It lives
# beside results.json rather than inside it because
# app.quiz.load_quiz_results does QuizResult(**item) - one extra key
# there would raise TypeError in every main-study analysis window that
# opened one of these quizzes.
RECUE_SIDECAR_FILENAME = "rhythm_recues.json"

# How long a performance keeps recording after the melody's last note-off
# before it ends itself. Only guided performances end on their own - the
# grid tells them when the melody is over; an unguided one has no grid,
# so the experimenter ends it.
PERFORM_TAIL_S = 1.5


class RhythmRunnerWindow(QuizWindow):
    trial_finished = Signal(int)  # trial index - recorded, saved, quiz on disk
    trial_aborted = Signal(int)  # trial index - cancelled, nothing saved

    def __init__(self, cfg: Config, cue: RhythmCue):
        # Stashed before super().__init__ because that's when _make_cue runs.
        self._shared_cue = cue
        super().__init__(cfg, guidance_type="haptic")
        self.setWindowTitle("Rhythm Experiment - Trial Runner")
        self._trial_index: Optional[int] = None
        self._trial: Optional[dict] = None
        self._allow_close = False

        # Per-note re-cue bookkeeping for the current trial, parallel to
        # self.results: index i describes the i-th note.
        self._first_cue_onsets: List[float] = []
        self._recue_counts: List[int] = []
        self._note_first_cue: Optional[float] = None
        self._note_recues = 0
        # A note is not over when the key goes down - it is over when the
        # beat is. These hold the cue open across the response; see
        # _record_result.
        self._sustaining = False
        self._sustain_until: Optional[float] = None
        self._sustain_lengths: List[float] = []

        # Performance state. Probes and the final test are played as
        # whole performances rather than note by note; only training runs
        # the cue/response loop.
        self._perform_mode = False
        self._perform_guided = False  # backlight walks the melody's grid
        self._perform_start: Optional[float] = None
        self._perform_lit: Optional[int] = None  # melody index currently lit
        # Each target's scheduled on/off within the melody, in seconds
        # from the melody's start: the grid a performance is played
        # against, and what the backlight follows during a probe.
        self._target_onsets: List[float] = []
        self._target_offsets: List[float] = []
        self._target_durations: List[float] = []

        # The song picker and the quiz-name box are dead controls here -
        # both are decided by the participant's schedule - so they come
        # out of the window entirely. See _strip_unused_controls().
        self._strip_unused_controls()
        # Trials start from the session window (a row's Start button or
        # "Run selected"), never from here - Cancel stays available.
        self.start_btn.hide()

        self.trial_label = QLabel(
            "No trial running - load a participant in the session window and start one there."
        )
        self.trial_label.setWordWrap(True)
        # The base's opening line is "Pick a song to begin", which names a
        # picker this window no longer has.
        self.status_label.setText(
            "Connect the LED strip and pick the MIDI port, then start trials "
            "from the session window."
        )

        # Only shown for the final test, which has no note-by-note loop to
        # end itself.
        self.stop_perform_btn = QPushButton("Stop && save performance")
        self.stop_perform_btn.clicked.connect(self._finish_performance)
        self.stop_perform_btn.hide()

        header = QHBoxLayout()
        header.addWidget(self.trial_label, 1)
        header.addWidget(self.stop_perform_btn)
        self.centralWidget().layout().insertLayout(0, header)

    # ------------------------------------------------------------------
    # Trimming the inherited quiz UI
    # ------------------------------------------------------------------

    def _strip_unused_controls(self) -> None:
        """Take the controls this study cannot use out of the window.

        QuizWindow's UI is built for someone choosing what to practise:
        a song picker and a quiz-name box. In a scheduled session both
        are already decided - the melody is fixed for all 19 trials by
        the participant's TrialStructure.json, and the quiz name is
        derived from participant + trial index - so neither can be
        edited. Showing them greyed out is worse than not showing them:
        a disabled box the experimenter can never fill in still invites
        a try mid-session.

        The widget objects stay alive, because QuizWindow's own methods
        still read and write them (`_start_quiz` takes the quiz name
        from `quiz_name_edit`, `_unlock_inputs` re-enables both). Only
        their place in the layout goes, and nothing is lost from view:
        `trial_label` names the melody and the quiz folder of the
        running trial, and `status_label` repeats the melody on load.

        The timeout spin box stays - it is now the most important
        control in the window - but it is relabelled. A timeout no
        longer ends a note here, it re-issues the cue (see
        `_record_result`), so "Timeout" would be actively misleading to
        an experimenter reading it mid-session.
        """
        layout = self.centralWidget().layout()

        # The whole "Song: [picker] [Refresh]" row.
        song_row = self._row_containing(layout, self.song_combo)
        if song_row is not None:
            layout.removeItem(song_row)
            self._detach_all(song_row)
            song_row.deleteLater()

        # "Quiz name: [box]" shares its row with the timeout, so only the
        # box and the label immediately before it come out.
        name_row = self._row_containing(layout, self.quiz_name_edit)
        if name_row is not None:
            index = self._index_of(name_row, self.quiz_name_edit)
            # Highest index first: taking the box does not shift its label.
            for i in (index, index - 1):
                if i >= 0:
                    self._detach_at(name_row, i)
            self._relabel(name_row, "Timeout:", "Re-cue after:")

    @staticmethod
    def _index_of(row, widget) -> int:
        for i in range(row.count()):
            item = row.itemAt(i)
            if item is not None and item.widget() is widget:
                return i
        return -1

    @classmethod
    def _row_containing(cls, layout, widget):
        """The sub-layout of `layout` holding `widget`, or None if the
        base class has since moved it somewhere else."""
        for i in range(layout.count()):
            row = layout.itemAt(i).layout()
            if row is not None and cls._index_of(row, widget) >= 0:
                return row
        return None

    @staticmethod
    def _detach_at(row, index) -> None:
        item = row.takeAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None:
            widget.setParent(None)
            # Un-parented widgets are top-level, so an unhidden one would
            # appear as its own little window.
            widget.hide()

    @classmethod
    def _detach_all(cls, row) -> None:
        """Empty a row. Widgets the runner still holds a reference to
        (song_combo) survive; the rest - labels, Refresh buttons - have
        no other owner and go."""
        while row.count():
            cls._detach_at(row, 0)

    @staticmethod
    def _relabel(row, old: str, new: str) -> None:
        for i in range(row.count()):
            item = row.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QLabel) and widget.text() == old:
                widget.setText(new)
                return

    def _make_cue(self, guidance_type: str, cue_style: Optional[str]):
        # One persistent cue for the whole session, owned by the session
        # controller - NOT a fresh rig connection per trial.
        return self._shared_cue

    # ------------------------------------------------------------------
    # Driving from the session controller
    # ------------------------------------------------------------------

    def start_trial(self, doc: dict, trial: dict) -> Optional[str]:
        """Start one scheduled trial. Returns the quiz name it records
        under, or None if it couldn't start (a message box said why -
        LED not connected, no MIDI port, melody missing, rig offline)."""
        if self.phase != "idle":
            QMessageBox.warning(self, "Trial already running", "Finish or cancel the current trial first.")
            return None

        if not self._load_melody_targets(trial["melody"]):
            return None

        quiz_name = self._unique_quiz_name(doc["participant"]["name"], trial)
        self.quiz_name_edit.setText(quiz_name)

        status = (
            f"Trial {trial['index']}/{len(doc['trials'])}  -  "
            f"{trial['phase_label']} #{trial['phase_number']}"
        )
        try:
            self._shared_cue.set_phase(haptic=trial["haptic"])
        except Exception as exc:
            QMessageBox.warning(self, "Vibration rig not available", str(exc))
            return None

        self.guidance_type = PHASE_GUIDANCE_TYPE[trial["phase"]]
        self._trial_index = trial["index"]
        self._trial = trial
        # Training is the only note-by-note phase; a probe and the final
        # test are both played straight through as performances, and
        # differ only in whether the backlight shows the grid.
        self._perform_mode = trial["phase"] != PHASE_TRAINING
        self._perform_guided = self._perform_mode and trial["backlight"]
        self._first_cue_onsets = []
        self._recue_counts = []
        self._note_first_cue = None
        self._note_recues = 0
        self._sustain_lengths = []
        self._end_sustain()
        self._perform_start = None
        self._perform_lit = None

        guidance = (
            f"backlight {'on' if trial['backlight'] else 'OFF'}, "
            f"haptic {'on' if trial['haptic'] else 'OFF'}"
        )
        self.trial_label.setText(
            f"Running {status}  -  {guidance}  -  melody '{trial['melody']}'  -  quiz '{quiz_name}'"
        )

        self._start_quiz()
        if self.phase == "idle":  # a _start_quiz guard refused (it said why)
            self._trial_index = None
            self._trial = None
            self._perform_mode = False
            self.trial_label.setText("Trial did not start - fix the problem above and start it again.")
            return None
        self.stop_perform_btn.setVisible(self._perform_mode)
        return quiz_name

    def _load_melody_targets(self, melody_name: str) -> bool:
        """Populate self.targets/mapping/led_mapper from a melody under
        data/rhythm_experiment/, bypassing app.song_library entirely."""
        import profile_led_mapper

        keyboard_profile_name = self.cfg.active_keyboard_profile
        try:
            melody = load_trial_melody(melody_name)
            mapping = MidiMapping.load(PROFILE_DATA_DIR / keyboard_profile_name / "midi_mapping.json")
        except RhythmStudyError as exc:
            QMessageBox.warning(self, "Melody missing", str(exc))
            return False
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't load melody", str(exc))
            return False

        if not melody.notes:
            QMessageBox.warning(self, "Empty melody", f"'{melody_name}' has no notes.")
            return False

        self.mapping = mapping
        self.targets = [
            QuizTarget(
                index=i,
                note=note.midi_note,
                note_name=note.note_name,
                key_id=mapping.key_for_note(note.midi_note),
                finger=note.finger or None,
            )
            for i, note in enumerate(melody.notes)
        ]
        # The melody's own time grid. A performance is scored against it,
        # and during a probe the backlight walks it.
        self._target_onsets = [note.note_on_time_sec for note in melody.notes]
        self._target_offsets = [note.note_off_time_sec for note in melody.notes]
        # How long each note SOUNDS - the melody's gate time, one beat
        # minus the release gap for a 1-beat note. This is what a
        # training cue has to last for the participant to feel the
        # difference between a 1-, 2- and 3-beat note.
        self._target_durations = [
            note.note_off_time_sec - note.note_on_time_sec for note in melody.notes
        ]
        self.current_song_name = melody_name

        try:
            self.led_mapper = profile_led_mapper.build_mapper(self.led, keyboard_profile_name)
        except Exception:
            self.led_mapper = None  # LED cueing just won't be available for this profile

        self.status_label.setText(
            f"{melody_name}: {len(self.targets)} notes  |  profile {keyboard_profile_name}"
        )
        return True

    def _unique_quiz_name(self, participant: str, trial: dict) -> str:
        base = sanitize_quiz_name(trial_quiz_base_name(participant, trial["index"]))
        name, attempt = base, 1
        while quiz_dir(name).exists():
            attempt += 1
            name = f"{base}-r{attempt}"  # rerun after a crash/cancel keeps every attempt's data
        return name

    # ------------------------------------------------------------------
    # QuizWindow hook overrides - the re-cue loop
    # ------------------------------------------------------------------

    def _show_current_target(self) -> None:
        """Same as the base, except the backlight obeys the trial's phase.

        The base lights the target key whenever the strip is connected.
        The LED is gated here rather than by disconnecting hardware
        between trials, because the strip has to stay connected all
        session for the per-trial sync flash.

        Of the three phases only the final test has backlight off, and it
        never reaches this method (it records as a free performance
        instead) - so the dark branch is currently unexercised. It is
        written out anyway so `backlight` means the same thing here as it
        does in schedule.PHASE_GUIDANCE, rather than being decorative on
        a row the runner ignores.
        """
        if self._trial is not None and not self._trial["backlight"]:
            target = self.targets[self.current_index]
            self.cue.show_target(target.note, target.finger)
            self.cue_onset_time = time.time()
            self.phase = "presenting"
            self.status_label.setText(
                f"Note {self.current_index + 1}/{len(self.targets)}: no guidance"
            )
        else:
            super()._show_current_target()

        # Anchor the first presentation of this note; a re-cue keeps it.
        # Deliberately copied from cue_onset_time rather than taken from
        # a time.time() of its own: the two have to share one anchor, or
        # "RT from the first cue" and "RT from the last cue" would be
        # measured from different points in the cue and stop being
        # comparable. (Lighting the key is a serial write, so a second
        # reading either side of it is millisecond-different, not
        # microsecond-different.)
        if self._note_first_cue is None:
            self._note_first_cue = self.cue_onset_time
            self._note_recues = 0

    def _record_result(
        self,
        timed_out: bool,
        actual_note: Optional[int] = None,
        keypress_time: Optional[float] = None,
    ) -> None:
        """A timeout re-cues; only a key press records the note.

        This is the study's core behavioural change. The base scores a
        timed-out note as a miss and advances - here the participant is
        being trained on one melody, so the note is simply cued again and
        keeps waiting. The trial can therefore only end by being played
        to the end, or by the experimenter cancelling it.
        """
        if timed_out:
            self._note_recues += 1
            self._clear_current_led()
            self.cue.clear()
            self._show_current_target()  # re-issues the cue, resets cue_onset_time
            self.status_label.setText(
                f"Note {self.current_index + 1}/{len(self.targets)}: "
                f"re-cued ({self._note_recues}x) - waiting for a key press"
            )
            return

        self._first_cue_onsets.append(self._note_first_cue if self._note_first_cue is not None else self.cue_onset_time)
        self._recue_counts.append(self._note_recues)
        self._note_first_cue = None
        self._note_recues = 0

        # The cue outlasts the key press by the note's own length, so the
        # participant feels how long the note is rather than only when it
        # starts - see _begin_sustain().
        sustain = self._sustain_length()
        self._sustain_lengths.append(sustain or 0.0)
        if sustain:
            self._begin_sustain(sustain)

        super()._record_result(timed_out=False, actual_note=actual_note, keypress_time=keypress_time)

        if sustain:
            # The base put us in "gap", which would cue the next note
            # 0.4 s from now. The gap belongs *after* this note finishes
            # sounding, not on top of it.
            self.phase = "sustain"
            self.phase_start_wall = time.time()

    # ------------------------------------------------------------------
    # Holding a cue for the length of the note
    # ------------------------------------------------------------------
    #
    # The melody's notes are 1, 2 or 3 beats long, and that is a property
    # the participant has to learn, not just which key to press. Every
    # other quiz in this project clears the cue the moment a response is
    # recorded, which teaches only the onset: a 3-beat note and a 1-beat
    # note feel identical.
    #
    # So in training the LED stays lit and the motor stays buzzing after
    # the key goes down, and both stop when the note's beat ends. The
    # instruction to the participant is "hold the key while you can feel
    # it, and let go when the buzzing stops".
    #
    # The length is measured from the KEY PRESS, not from the cue. A
    # training trial is paced entirely by the participant - the next note
    # is not cued until the last is answered - so the note's slot
    # effectively begins when they play it. Anchoring on the cue instead
    # would mean that the slower a participant was, the less of the note
    # they would feel, and a participant slower than the note is long
    # would feel none of it, which is the opposite of what training is
    # for.
    #
    # Probes and the final test need none of this: they are performances
    # against the melody's real grid, where the backlight already runs on
    # the melody's own on/off times (see _drive_backlight).

    def _sustain_length(self) -> Optional[float]:
        """How long the current note's cue should outlast the key press,
        or None when this phase does not sustain."""
        if self._trial is None or self._trial["phase"] != PHASE_TRAINING:
            return None
        if not 0 <= self.current_index < len(self._target_durations):
            return None
        return self._target_durations[self.current_index] or None

    def _begin_sustain(self, seconds: float) -> None:
        self._sustaining = True
        self._sustain_until = time.time() + seconds
        # Holds the buzz through the base's clear(), which runs inside
        # _record_result a moment from now.
        self._shared_cue.begin_hold()

    def _end_sustain(self) -> None:
        """Stop a held cue. Safe to call at any time, so every path that
        ends a note or a trial can call it without first working out
        whether a note was in progress."""
        self._sustaining = False
        self._sustain_until = None
        self._shared_cue.end_hold()
        super()._clear_current_led()

    def _clear_current_led(self) -> None:
        # The base clears the lit key inside _record_result. During a
        # sustain the key is still sounding, so the LED stays on and is
        # cleared by _end_sustain instead.
        if self._sustaining:
            return
        super()._clear_current_led()

    # ------------------------------------------------------------------
    # Probes and the final test: played as whole performances
    # ------------------------------------------------------------------
    #
    # Training is a cue/response task - one note at a time, the next cue
    # withheld until the last one is answered. That is the right shape
    # for teaching, but it makes rhythm unmeasurable: the participant
    # physically cannot play ahead of a cue that is only issued 0.4 s
    # after their previous key press, so their note timing is the
    # apparatus's, not theirs.
    #
    # So a probe and the final test are not cue/response at all. The
    # melody's own time grid runs, and the participant plays along with
    # it; every note therefore has a time it was DUE, which is what
    # makes onset and duration error real numbers rather than reaction
    # times. The two differ only in what the participant has to go on:
    #
    #   probe        backlight walks the grid - the lit key both names
    #                the note and shows when it falls
    #   final test   nothing at all; the grid still runs, invisibly, as
    #                the reference the performance is scored against

    def _begin_countdown(self) -> None:
        if not self._perform_mode:
            super()._begin_countdown()
            return
        # A performance needs a start signal the participant can act on,
        # so the countdown stays - but it counts into the grid, not into
        # a response window.
        self.phase = "perform_countdown"
        self.phase_start_wall = time.time()

    def _start_performance(self) -> None:
        self.phase = "perform"
        self.phase_start_wall = time.time()
        self._perform_start = time.time()
        self._perform_lit = None
        if self._perform_guided:
            self.status_label.setText(
                "Probe recording - the backlight is playing the melody; the participant plays along. "
                "It ends on its own when the melody finishes."
            )
        else:
            self.status_label.setText(
                "Final test recording - no backlight, no haptic. "
                'Press "Stop & save" when the participant has finished the melody.'
            )

    def _tick(self) -> None:
        # The base handles camera + MIDI capture at the top of its own
        # _tick and then branches on phase; "perform_countdown" and
        # "perform" are not among its phases, so those branches fall
        # through and the performance is driven from here instead.
        super()._tick()
        if self.phase == "sustain":
            if time.time() >= (self._sustain_until or 0.0):
                self._end_sustain()
                # Hand back to the base's own between-notes gap, timed
                # from now so it is a gap after the note rather than
                # inside it.
                self.phase = "gap"
                self.phase_start_wall = time.time()
        elif self.phase == "perform_countdown":
            remaining = COUNTDOWN_S - (time.time() - self.phase_start_wall)
            if remaining <= 0:
                self._start_performance()
            else:
                self.status_label.setText(f"Get ready: {remaining:.0f}")
        elif self.phase == "perform":
            self._performance_tick()

    def _performance_tick(self) -> None:
        elapsed = time.time() - (self._perform_start or time.time())
        if self._perform_guided:
            self._drive_backlight(elapsed)
        # Only a guided performance can end itself: the grid is what
        # tells the participant the melody is over, so without it there
        # is nothing to say they have finished except the experimenter.
        if self._perform_guided and self._target_offsets:
            if elapsed >= self._target_offsets[-1] + PERFORM_TAIL_S:
                self._finish_performance()

    def _drive_backlight(self, elapsed: float) -> None:
        """Light whichever melody note is sounding at `elapsed`.

        The melody is one voice, so at most one note is due at a time and
        a plain scan over its 15 notes is cheaper than the bookkeeping
        needed to avoid it.
        """
        if not self.led_connected or self.led_mapper is None:
            return
        due = None
        for i, (on, off) in enumerate(zip(self._target_onsets, self._target_offsets)):
            if on <= elapsed < off:
                due = i
                break
        if due == self._perform_lit:
            return
        if self._perform_lit is not None:
            self.led_mapper.clear_key(self.targets[self._perform_lit].note)
        if due is not None:
            self.led_mapper.light_key(self.targets[due].note)
        self._perform_lit = due
        self.lit_note = self.targets[due].note if due is not None else None

    def _finish_performance(self) -> None:
        """End a probe or the final test and score it against the grid.

        Every note the participant played is in the raw MIDI log either
        way, so nothing is lost here; what this does is fill in the
        ordinary results.json so the existing analysis tools have
        something to read. Notes are matched to the melody
        POSITIONALLY - the n-th key press is judged against the n-th
        note of the melody - and each note's `cue_onset_time` is the
        moment that note was *due*, so `timing_error_s` reads as "how
        far off the beat this note was".

        The anchor that "due" is measured from differs by condition, and
        the sidecar records which was used so the analysis can re-anchor:

        * a **probe** is anchored on the grid, because the backlight
          started the melody at a known moment - so the error includes
          how far the participant lagged the cue overall;
        * the **final test** has no external clock, so it is anchored on
          the participant's own first note. Their overall start time is
          not a rhythm error, and scoring it as one would make the final
          test look bad for a reason that has nothing to do with what it
          measures.
        """
        if self.phase != "perform":
            return
        self._clear_current_led()
        self._perform_lit = None

        grid_start = self._perform_start or self.video_start_time
        presses = [e for e in self.raw_events if e.type == "note_on" and e.abs_time >= grid_start]

        # See the docstring: a probe keeps the grid's own zero, the final
        # test is re-zeroed on the first thing the participant played.
        if self._perform_guided or not presses:
            anchor = grid_start
        else:
            anchor = presses[0].abs_time - self._target_onsets[0]

        self.results = []
        for i, target in enumerate(self.targets):
            due = anchor + self._target_onsets[i]
            press = presses[i] if i < len(presses) else None
            self.results.append(
                QuizResult(
                    index=target.index,
                    target_note=target.note,
                    target_note_name=target.note_name,
                    target_key_id=target.key_id,
                    target_finger=target.finger,
                    cue_onset_time=due,
                    timed_out=press is None,  # they stopped before this note
                    actual_note=press.note if press else None,
                    actual_key_id=self.mapping.key_for_note(press.note) if (press and self.mapping) else None,
                    keypress_time=press.abs_time if press else None,
                    timing_error_s=(press.abs_time - due) if press else None,
                    note_correct=(press.note == target.note) if press else False,
                )
            )
        # Every note is presented once, so the sidecar stays the same
        # shape as a training trial's.
        self._first_cue_onsets = [r.cue_onset_time for r in self.results]
        self._recue_counts = [0] * len(self.results)

        extra = len(presses) - len(self.targets)
        self.current_index = len(self.targets)
        self._finish_quiz()
        if extra > 0:
            self.trial_label.setText(
                f"{self.trial_label.text()}  -  note: {extra} extra key press(es) beyond the melody's "
                f"{len(self.targets)} notes are in the raw MIDI log but not in results.json."
            )

    # ------------------------------------------------------------------
    # QuizWindow hook overrides - session integration
    # ------------------------------------------------------------------

    def _refresh_songs(self) -> None:
        # The base picks + loads the first list entry, which would swap the
        # targets out from under a running trial. It would also be wrong
        # even when idle: this study's stimuli are never in the song list.
        return

    def _unlock_inputs(self) -> None:
        # The base re-enables song_combo and quiz_name_edit here. Both are
        # out of the layout (see _strip_unused_controls), so that is a
        # no-op on something invisible and needs no undoing.
        super()._unlock_inputs()
        self.stop_perform_btn.hide()

    def _save_recue_sidecar(self) -> None:
        """Write the per-note re-cue record beside results.json.

        results.json stays a byte-for-byte ordinary quiz (see the module
        docstring); this is where the two things it has no field for go:
        `first_cue_onset_time`, and how many times the note had to be
        re-cued. `QuizResult.cue_onset_time` is always the LAST cue, so
        RT from either baseline is recoverable:

            RT (from last cue)  = keypress_time - cue_onset_time
            RT (from first cue) = keypress_time - first_cue_onset_time
        """
        if not self.results:
            return
        # quiz_dir("") is data/quiz ITSELF, not a folder inside it, so a
        # trial that never got as far as being named would drop this file
        # straight into the folder the main study's quizzes live in - and
        # lose its own re-cue record in the process. QuizWindow starts
        # quiz_name empty, so this is reachable whenever a trial ends
        # before _start_quiz has named it.
        if not self.quiz_name:
            return
        payload = {
            "quiz_name": self.quiz_name,
            "trial_index": self._trial_index,
            "phase": self._trial["phase"] if self._trial else None,
            "melody": self._trial["melody"] if self._trial else None,
            "backlight": self._trial["backlight"] if self._trial else None,
            "haptic": self._trial["haptic"] if self._trial else None,
            "timeout_s": self.timeout_s,
            # Anchor the cue sustain was measured from, so a later change
            # of mind about it is visible in the data rather than
            # inferred from the code that happened to be running.
            "sustain_anchor": "keypress",
            "notes": [
                {
                    "index": result.index,
                    "first_cue_onset_time": first,
                    "last_cue_onset_time": result.cue_onset_time,
                    "recue_count": recues,
                    # How long the cue was held past the key press, so the
                    # note's beat could be felt rather than only its onset.
                    "sustain_s": sustain,
                }
                for result, first, recues, sustain in zip(
                    self.results,
                    self._first_cue_onsets,
                    self._recue_counts,
                    self._sustain_lengths or [0.0] * len(self.results),
                )
            ],
        }
        path = quiz_dir(self.quiz_name) / RECUE_SIDECAR_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _open_analysis_window(self) -> None:
        # No per-trial analysis popup mid-session (see module docstring).
        # The sidecar is written here, before the session controller is
        # told the trial is done, so the trial's folder is complete the
        # moment it is marked completed.
        # A trial that finished on its last note leaves that note's cue
        # held; nothing else would stop the motor.
        self._end_sustain()
        try:
            self._save_recue_sidecar()
        except Exception as exc:  # never lose a recorded trial over a sidecar
            QMessageBox.warning(self, "Couldn't save re-cue record", str(exc))

        index, self._trial_index = self._trial_index, None
        self._trial = None
        self._perform_mode = False
        self.stop_perform_btn.hide()
        if index is not None:
            self.trial_label.setText(f"Trial {index} finished and saved.")
            self.trial_finished.emit(index)

    def _cancel_quiz(self) -> None:
        # Cancelling mid-note must not leave the motor running.
        self._end_sustain()
        super()._cancel_quiz()
        index, self._trial_index = self._trial_index, None
        self._trial = None
        self._perform_mode = False
        self.stop_perform_btn.hide()
        if index is not None:
            self.trial_label.setText(f"Trial {index} cancelled - nothing was saved; it can be rerun.")
            self.trial_aborted.emit(index)

    def closeEvent(self, event) -> None:
        # Closing this window mid-session would tear down the camera AND
        # the shared vibration-rig connection. Its lifetime belongs to the
        # session controller, so a stray X-click just minimizes.
        if self._allow_close:
            super().closeEvent(event)
        else:
            event.ignore()
            self.showMinimized()
