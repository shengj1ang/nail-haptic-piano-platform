"""Rhythm experiment: participant schedules and the formal session.

The run-time half of the rhythm experiment - the platform's most recently
built study, not its second: the actuator validation experiments (section
9) and the tele-training work (section 8) both came earlier, and the Main
User Study earlier still. Its stimulus half is the standalone
``melody_generator`` package (launcher section 11's first two buttons);
this package is what actually puts a participant through the melody.

The design, in one line::

    Training x5 -> Probe 1 -> Training x5 -> Probe 2 -> Training x5 -> Probe 3 -> Final test

19 trials, one melody, fixed order. What changes across them is not the
stimulus but which guidance channels are available:

===========  =========  =======  ============  ==============================
Phase        Backlight  Haptic   Task          What the participant goes on
===========  =========  =======  ============  ==============================
Training     on         on       cue/response  both channels - 15 trials
Probe        on         off      performance   the backlight, playing the
                                               melody on its own clock
Final test   off        off      performance   memory only - one trial, last
===========  =========  =======  ============  ==============================

The task column matters as much as the cue columns. **Training is a
cue/response task** - the next note is not cued until the last one is
answered - which is right for teaching but makes rhythm unmeasurable:
the participant cannot play ahead of an apparatus that waits for them.
**A probe and the final test are performances** against the melody's own
time grid, which is what gives them a real onset and duration error
rather than a reaction time. See :mod:`rhythm_study.analysis`, which
keeps the two kinds of timing in separate columns and never subtracts
one from the other.

Structure mirrors the Main User Study's section 6, which this was copied
from:

* :mod:`rhythm_study.schedule` - GUI-free schedule + TrialStructure.json
  I/O (from ``app/pilot_study.py``);
* :mod:`rhythm_study.cue` - the per-phase cue output (replaces
  ``app/gui/experiment_cue.py``);
* :mod:`rhythm_study.schedule_window` - "Rhythm Trial Schedule";
* :mod:`rhythm_study.session_window` - "Rhythm Experiment Session";
* :mod:`rhythm_study.runner_window` - the trial runner;
* :mod:`rhythm_study.analysis` - event extraction, metrics, statistics;
* :mod:`rhythm_study.analysis_figures` - the report's figures;
* :mod:`rhythm_study.group_analysis_window` - "Rhythm Group Analysis".

WHAT IS DELIBERATELY DIFFERENT FROM THE MAIN USER STUDY
=======================================================
Copied, not shared - nothing here is imported by section 6, so this
study's design can move without touching a locked one:

* **No visual guidance, and no participant-facing screen at all.** The
  main study's ``ExperimentCue``/``CueWindow`` (finger dot / hand photo)
  has no counterpart here. "Backlight" means the keyboard's own LED
  strip; the participant looks at the keyboard, never at a monitor.
* **One analysis window, not three.** The main study splits participant
  / group / model because it has a 3x3 factorial and several questions.
  This study asks one - does the fingering and timing survive the haptic
  cue being removed - and answers it from three probes.
* **A timeout re-cues instead of advancing.** The standalone quizzes
  score a note as timed out and move on; here the cue is re-issued and
  the note waits (see :mod:`rhythm_study.runner_window`).
* **The final test is a free performance**, ended by the experimenter,
  not a note-by-note cue/response loop.
* **No randomisation and no seed.** The 19-trial order is fixed by the
  design, so there is nothing to shuffle and nothing to reproduce - and
  therefore no reason to touch ``config.json``'s seeds.
* **Stimuli come from ``data/rhythm_experiment/``**, read through
  ``melody_generator.load``, not from ``data/sequence/`` and not through
  ``app.song_library``.

WHERE THE DATA GOES
===================
* ``data/RhythmStudy/<participant>/TrialStructure.json`` - the schedule
  and its live progress.
* ``data/quiz/rhythm-<participant>-T<NN>`` - each trial, recorded as an
  ordinary quiz so every existing analysis tool reads it unchanged. The
  ``rhythm-`` prefix is the only thing separating these from the main
  study's quizzes in that shared folder.

* ``data/RhythmStudy/group_figures/`` - the analysis output: the
  event-level and participant-level CSVs, the statistics, the figures
  and a written summary.

The re-cue counts and first-cue timestamps that the ordinary quiz record
has no field for are written to a **sidecar** file inside the quiz
folder (``rhythm_recues.json``), never as extra keys in ``results.json``:
``app.quiz.load_quiz_results`` builds ``QuizResult(**item)``, so one
extra key would raise TypeError in every main-study analysis window that
happened to open one of these quizzes. Note-offs are not in
``results.json`` at all - the analysis reconstructs every duration from
``raw/midi_raw.json``, which is the only place they exist.
"""
