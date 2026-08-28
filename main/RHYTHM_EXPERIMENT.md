# The Rhythm Experiment

The most recent of this platform's studies, and the last to be built. Where
the Main User Study asks which *cue modality* teaches a key-and-finger mapping
best, this one asks what survives when a cue is **taken away**.

It is not the platform's second experiment - see
[Where this sits](#where-this-sits-among-the-platforms-experiments).

Launcher **section 11**. Code lives in two packages: `melody_generator/`
(the stimulus) and `rhythm_study/` (the session and the analysis).

- [Introduction](#introduction)
  - [Where this sits](#where-this-sits-among-the-platforms-experiments)
- [Method](#method)
  - [Stimulus: the melody generator](#stimulus-the-melody-generator)
  - [Apparatus](#apparatus)
  - [Design](#design)
  - [Procedure](#procedure)
  - [Measures](#measures)
  - [Analysis plan](#analysis-plan)
  - [Data, files and how to run it](#data-files-and-how-to-run-it)
- [Results](#results)
- [Discussion](#discussion)

---

## Introduction

Vibrotactile finger guidance can tell a learner *which finger to use* while
they play. The Main User Study establishes that it works while it is switched
on. That leaves the question a training aid actually has to answer:

> **Does repeated haptic-guided practice let participants keep the intended
> fingering and timing once the haptic cue is removed?**

An aid that only works while attached has taught nothing; it has substituted
for learning. So the design here is not a comparison of cue types. It is a
**cue-withdrawal** design: the same participant plays the same melody many
times with haptic guidance, and is periodically measured **without it**.

Three things follow from that framing, and they shape everything below.

1. **Improvement during guided practice is not the result.** Performance rises
   across training trials because the cue is present, not necessarily because
   anything was retained. The evidence for retention is what happens in the
   unguided measurements, and how the gap between guided and unguided changes
   with practice.
2. **The measurement has to be repeated, not final.** One post-test cannot
   distinguish "learned it by repetition 5" from "learned it by repetition 15".
   Three probes, after 5, 10 and 15 repetitions, can.
3. **Fingering and timing are separate claims.** A participant can hit every
   right key with the wrong fingers, or the right fingers at the wrong moment.
   Both are reported, and the primary analysis carries one of each.

### Where this sits among the platform's experiments

This is one strand of several, and the latest of them. Calling it "the second
study" would be wrong twice over: two other lines of experimental work came
first, and the hardware characterisation came before any of the human studies
because the others depend on its numbers.

| strand | launcher | what it measures | first built |
|---|---|---|---|
| **Validation experiments** | section 9 | the actuators themselves - LRA resonance and intensity calibration, motor-to-accelerometer delay, ERM/LRA spectra, adhesive comparison | 2026-07-22 |
| **Main User Study** | section 6 | which cue modality teaches a key-and-finger mapping best - key-only / visual / vibrotactile x 3 difficulty levels | 2026-07-22 |
| **Tele-training** | section 8 | guidance delivered over a network: a teacher, a student and a relay running at once, with its own latency budget and analysis | 2026-08-06 |
| **Rhythm experiment** | section 11 | *this document* - what survives when the cue is withdrawn | 2026-08-25 |

The validation experiments are experiments in the same sense as the rest: each
has a protocol, recorded runs and its own write-up (see
`validation_experiments/README.md`). They are the reason the cue amplitude and
frequency used here are the values they are rather than a guess, which puts
them upstream of every human study on the platform, this one included.

### Relationship to the Main User Study

Deliberately isolated from all of the above: no shared design code, no shared
stimulus pool. See
[Data, files and how to run it](#data-files-and-how-to-run-it) for the exact
boundary and why it is enforced structurally rather than by convention.

| | Main User Study | Rhythm experiment |
|---|---|---|
| question | which cue modality teaches best | what survives cue removal |
| factors | 3 cue conditions x 3 difficulty levels | 1 melody, guidance withdrawn over time |
| stimuli | 27 generated sequences, randomised | **one** melody, 19 repetitions |
| participant sees | on-screen finger cue on an external display | **nothing** - keyboard only |
| trials | 27, interleaved by a seeded shuffle | 19, **fixed order** |

---

## Method

### Stimulus: the melody generator

Full detail in [`melody_generator/README.md`](melody_generator/README.md); this
is what matters for the experiment and why.

The stimulus is **one 15-note melody**, generated once and then frozen for the
whole study. It has to satisfy two competing requirements: it must be
*learnable* in 15 repetitions by a beginner, and it must be *a tune* rather
than a legal-but-random note sequence — a random sequence would be memorised as
15 unrelated facts, which is not what a melody is and not what piano practice
trains.

Four design decisions do that work.

**1. Structure first, notes second.** The 15 notes are laid out as four phrases
(`4 + 4 + 4 + 3`, from `default_phrase_plan(15)`) with musical roles, rather
than sampled note by note:

| phrase | role | construction |
|---|---|---|
| 1 | statement `A` | a motif grown under the melodic rules, ending on an open tone |
| 2 | answer `A'` | `A` repeated, transposed by a scale step, re-tailed, or mirrored |
| 3 | contrast `B` | a new idea, biased towards the other hand's register |
| 4 | cadence `A''` | `A`'s opening, then a step onto the tonic |

The repetition between phrases 1, 2 and 4 is not decoration: it is what makes
15 notes memorable, and therefore what makes the melody learnable in a fixed
and affordable number of repetitions.

**2. Pitch is generated in scale-step space.** `+1` is always the next white
key, so no accidental can be produced. Interval weights favour steps and
thirds; a fourth is the widest leap allowed (`max_leap_steps = 3`); a leap is
usually followed by a step back the other way (`recover_after_leap_prob =
0.85`, the standard voice-leading rule); two leaps never occur back to back; a
phrase stays inside a five-key window but must span at least a third. The seam
between two phrases obeys the same rules as the inside of one.

**3. Fingering is a static map, not a search.** Each hand holds one
five-white-key position for the entire melody — the default `middle_c` layout
puts both thumbs on C4, giving nine contiguous white keys from F3 to G4 — and
a key's finger follows from which block it falls in. (The layouts are tabulated
in [`melody_generator/README.md`](melody_generator/README.md).)

**The same key is therefore always played by the same finger, in every one of
the 19 trials** — no thumb-under, no hand shift, no finger substitution.

This is the single most important property for this study. "Did they retain the
intended fingering?" is only a well-posed question if there *is* one intended
fingering; if the correct finger for a key changed with context, a wrong finger
would be ambiguous between a memory failure and a legitimate alternative.
Awkward fingering is not filtered out after generation — it cannot be
generated.

**4. Rhythm: one rule, no counting.** Every onset falls on a whole beat. Every
phrase ends on a held note, and some phrase endings are followed by a one-beat
rest. Nothing else varies: no eighth notes, no syncopation, no ties. The
closing note is three beats, and about a quarter of the time one interior note
is held for two. At the default **60 BPM one beat is one second**, so a
participant never has to subdivide, and the experimenter can read the grid off
the score directly.

The held notes matter to the analysis: a melody of nothing but one-beat notes
would make duration error meaningless. The frozen stimulus
(`melody_seed42`) has 11 one-beat, 3 two-beat and 1 three-beat notes.

**Articulation.** A key is released before the next is pressed, so exactly one
note ever sounds. The default `fixed_gap` mode releases every note a constant
**0.25 beat** before its slot ends — a constant *gap* rather than a constant
*ratio*, because scaling a 3-beat note by 0.75 would clip 0.75 s off it and it
would read as a shortened note rather than a held one.

**Selection.** A candidate is discarded unless it passes every rule in five
groups — playability, fingering, melodic motion, rhythm and structure (the full
code list is in the generator README). The structure group is what rejects a
sequence that is technically legal but reads as random digits
(`no_phrase_structure`, `pitch_entropy_high`, `weak_cadence`). The generator
then builds up to `candidate_attempts = 400` candidates, keeps a pool of
`candidate_pool = 40`, and selects the best on

```
musicality - 0.3 x difficulty
```

Both scores are plain weighted means of features in `[0, 1]`, and every feature
is written into the melody's JSON so a selection can be audited rather than
trusted. The frozen stimulus scores **0.946 musicality / 0.262 difficulty**.

**Reproducibility.** Generation is seeded, and the seed, the full config, the
scores and the rejection counts are all written into the melody's `.json`. The
melody is generated once, saved to `data/rhythm_experiment/`, and locked to a
participant when their schedule is created — the schedule file stores both the
melody's name and a snapshot of its properties, so the record still says what
was played even if the folder is later regenerated.

### Apparatus

- **Keyboard** — MIDI keyboard with a per-key addressable **LED strip**
  ("backlight"). Key positions come from the active calibration profile.
- **Vibrotactile rig** — one motor per finger, ten in total
  (`app.haptic_cue.FINGER_TO_MOTOR`). Drive frequency and amplitude are read
  once at connection from the shared haptic config, so a mid-session edit
  cannot change the cue between two trials.
- **Camera** — overhead view; fingering is recovered offline by the existing
  finger-matching pass, not judged live.
- **No participant-facing display.** The Main User Study puts a finger cue on
  an external monitor; this study has none. The participant looks only at the
  keyboard.

### Design

Within-participant, single factor: **guidance availability**, manipulated by
withdrawing cues at scheduled points. One melody, 19 trials, fixed order:

```
Training x5 -> Probe 1 -> Training x5 -> Probe 2 -> Training x5 -> Probe 3 -> Final test
```

| Phase | Backlight | Haptic | Task | n |
|---|---|---|---|---|
| **Training** | on | on | cue/response | 15 |
| **Probe** | on | off | performance | 3 |
| **Final test** | off | off | performance | 1 |

Probes fall at trials 6, 12 and 18, after 5, 10 and 15 training repetitions.
The final test is trial 19.

**There is no randomisation and no seed.** The order is fixed by the design —
the probes have to fall at known amounts of practice for "after 5 / 10 / 15
repetitions" to mean anything — so there is nothing to shuffle and nothing to
reproduce.

A rest is offered after each probe. That is the natural seam: it is the only
point at which the participant is not part-way through a training block. Unlike
the Main User Study's fixed two-minute rests, these are untimed — the session
simply waits for the experimenter.

### Procedure

**The task changes between phases, and this is not incidental.**

**Training trials are a cue/response task.** One note at a time: the target
key lights, its finger's motor buzzes, and the next note is not cued until the
current one is answered. If the participant does not press within the timeout,
**the cue is re-issued and the note keeps waiting** — a timeout never scores a
miss and never advances. Any key press advances, including a wrong one, which
is scored wrong exactly as in the standalone quizzes. The number of re-cues per
note is recorded.

This shape is right for teaching, and wrong for measuring rhythm. The next cue
arrives a fixed 0.4 s after the previous key press, so a participant *cannot*
play ahead of the apparatus, and their note timing is its timing, not theirs.
Training therefore yields a **reaction time** and no onset error at all.

**Probes and the final test are performances.** The melody's own time grid
runs, and the participant plays along with it, so every note has a moment it
was **due** — which is what makes onset and duration error real measurements
rather than reaction times. The two differ only in what the participant has to
go on:

- **Probe** — the backlight walks the grid: the lit key both names the note and
  shows when it falls. The haptic cue is off. The trial ends itself when the
  melody finishes.
- **Final test** — nothing is shown. The grid still runs, invisibly, purely as
  the reference the performance is scored against. The experimenter ends the
  trial.

**Anchoring differs between the two, deliberately.** A probe is scored against
the grid's own zero: the backlight showed the participant when every note was
due, so lagging the whole melody *is* a timing error. The final test is
re-anchored on the participant's own first note: nothing told them when to
start, so their overall start time is not a rhythm error and scoring it as one
would penalise the final test for something it does not measure. Both
anchorings are recoverable from the saved data.

### Measures

Per note event, derived where the data allows:

| | source |
|---|---|
| target / actual MIDI note | `results.json` |
| target finger | `results.json` (from the melody's static fingering) |
| detected finger | `results.json`, filled in by the offline video pass |
| target note-on / note-off | the melody's own JSON — the grid |
| actual note-on | `results.json` |
| **actual note-off** | **`raw/midi_raw.json` only** — `results.json` has no note-off field, so every duration is reconstructed by pairing note-on/note-off per pitch |
| trial number, phase, repetition number | the participant's `TrialStructure.json` |

#### Primary outcomes

1. **Finger accuracy** — correct target finger **and** correct key, over all
   target notes. Also reported conditional on correct key presses.
2. **Absolute onset error** — `|actual_note_on - target_note_on|`, in
   milliseconds; the main timing measure. **Signed** onset error is retained
   alongside it, to show whether participants run early or late rather than
   just how far off they are.

#### Secondary outcomes

3. **Key accuracy** — correct MIDI key over all target notes.
4. **Duration error** — `actual_duration - target_duration`, and its
   magnitude. Most informative on the 2- and 3-beat held notes.
5. **Complete-performance rate** — an event counts only if the key, the finger
   **and** the onset (within a configurable tolerance) are all right. The
   tolerance is a parameter, not a constant; **±200 ms** is a starting value
   for a first look, not a finding.

Accuracy denominators are **target notes, never key presses**. A note the
participant never played is a note they did not get right; averaging over the
notes that happen to have a verdict would score someone who played 5 of 15
notes the same as someone who played all 15 equally well.

#### Two measures that must not be mixed

Training yields a reaction time; a performance yields an onset error. They are
different quantities and are kept in different columns throughout. One
consequence is worth stating plainly:

> **The timing half of the "haptic withdrawal cost" cannot be computed.**
> `Training 5 -> Probe 1` is a valid comparison for finger and key accuracy,
> which are the same measure in both phases. It is not one for timing, because
> subtracting an onset error from a reaction time is arithmetic on two
> different things. The analysis refuses it rather than reporting it.

### Analysis plan

**The participant is the unit of inference throughout.** Note events within a
trial are not independent observations, and treating them as such would
multiply the apparent sample size by 15.

**Main comparison — Probe 1 vs Probe 2 vs Probe 3.** Per participant, per
probe: finger accuracy, key accuracy, mean and median absolute onset error,
signed onset error, duration error.

The tests are **non-parametric by default**: finger accuracy is a bounded
proportion that may sit near ceiling, which is exactly the situation in which a
repeated-measures ANOVA's assumptions fail and its p-value stops meaning
anything.

- **Friedman** across the three probes, with **Kendall's W** as the effect size.
- **Paired Wilcoxon** post-hoc for Probe 1 vs 2, 2 vs 3 and 1 vs 3, corrected
  with **Holm** — three chances at the same claim on the same participants.
- Effect sizes as **matched-pairs rank-biserial correlation**, and **percentile
  bootstrap** confidence intervals for the mean paired difference. Bootstrapped
  for the same reason the tests are non-parametric: on small samples of a
  bounded measure a normal interval can run past 100% accuracy.
- Below **n = 5** complete participants nothing is tested; descriptives are
  reported and the absence of a test is stated.

**Training (trials 1–15) is descriptive.** Learning curves for finger accuracy,
key accuracy, response time and duration error, with the guidance-withdrawal
points marked so the curve is never read as one continuous series with the
probes. Improvement here is **not** evidence of retention — the haptic cue is
present throughout — and the figures and the written summary both say so.

**Haptic withdrawal cost.** `probe - preceding training`, for accuracy only,
at each of the three withdrawal points:

```
Training 5  -> Probe 1
Training 10 -> Probe 2
Training 15 -> Probe 3
```

A cost that shrinks with practice is the retention signature: the participant
is depending less on the cue.

**Final test, separately.** Reported on its own and compared with Probe 3
descriptively and as a paired comparison — but labelled a **transfer /
unguided-performance** comparison, never as a fourth probe. The probe removes
one channel; the final test removes two, so a drop there has two possible
causes and cannot be attributed to haptic withdrawal alone.

**Secondary, if the data supports it:** error rate and timing error by finger,
1- vs 2- vs 3-beat duration accuracy, errors around hand switches, and sequence
positions where errors recur. The event-level CSV carries the fields these
need (`target_duration_beats`, `target_hand`, `target_finger`, `event_index`).
These are kept subordinate to the Probe 1–3 result.

**Finger data is required, not optional.** `actual_finger` is filled in by the
offline video pass, so a trial that has not been through it has no finger data
at all. The pipeline **stops and names the trials** rather than averaging over
a partly-analysed set, which would silently mean something different per
participant.

### Data, files and how to run it

**In the launcher, section 11, in order:**

| | |
|---|---|
| Rhythm Melody Generator | generate and audition a melody; writes `data/rhythm_experiment/` |
| Playback Rhythm Melody | replay one off disk |
| Rhythm Trial Schedule | participant metadata, pick the melody, generate and save the 19-trial schedule |
| Rhythm Experiment Session | run the session (opens the session controller and the trial runner) |
| Rhythm Group Analysis | the whole analysis stage |

**Where the data goes:**

```
data/rhythm_experiment/<melody>.{json,mid,csv,txt}   the stimulus
data/RhythmStudy/<participant>/TrialStructure.json   schedule + live progress
data/quiz/rhythm-<participant>-T<NN>/                one trial, an ordinary quiz
    results.json          per-event record (no note-off - see Measures)
    meta.json             includes guidance_type: haptic | backlight | unguided
    rhythm_recues.json    sidecar: first/last cue times and re-cue count
    raw/midi_raw.json     the complete MIDI log
data/RhythmStudy/group_figures/                      analysis output
```

Every status change is written straight back to `TrialStructure.json`, so a
session can crash at any point and resume from the first non-completed trial.

**Isolation from the Main User Study.** That study's data collection is
complete and its stimulus pools are frozen, so this one is prevented from
reaching them structurally, not by convention:

- no shared design code — `rhythm_study` never imports `app.pilot_study` or
  `app.sequence_generator`, and a test parses the imports to enforce it;
- it never writes `config.json`, a keyboard profile, `data/sequence/`,
  `data/music/` or `data/MainUserStudy/`;
- its melodies are not registered with `app.song_library`, so one cannot appear
  in the main study's song pickers.

The two studies meet in exactly one place: the shared `data/quiz/` folder,
where the `rhythm-` **prefix** is what separates them. Trial records are kept
byte-compatible with an ordinary quiz so every existing analysis tool reads
them unchanged — `app.quiz.load_quiz_results` builds `QuizResult(**item)`, so
one extra key would raise `TypeError` in every main-study analysis window that
opened one. That is why the re-cue record is a sidecar file. Note that
`list_quizzes()` has no filter, so `rhythm-*` folders do appear in the main
study's Quiz Analysis list; this is cosmetic.

**Tests.**

```bash
QT_QPA_PLATFORM=offscreen python -m pytest test-script/test_rhythm_study.py test-script/test_rhythm_analysis.py -q
```

There is no recorded data yet, so the analysis tests build **synthetic
sessions with known planted effects** and check the pipeline recovers them — a
metric that cannot recover an effect deliberately put into its input should not
be trusted to find a real one.

**Module map.**

| module | holds |
|---|---|
| `melody_generator/` | the stimulus generator, stdlib only, its own MIDI writer |
| `rhythm_study/schedule.py` | the 19-trial plan and `TrialStructure.json` I/O, GUI-free |
| `rhythm_study/cue.py` | the per-phase cue output (a switch, not a screen) |
| `rhythm_study/runner_window.py` | the trial runner: re-cue loop and performances |
| `rhythm_study/session_window.py` | the session controller |
| `rhythm_study/analysis.py` | event extraction, metrics, statistics, export |
| `rhythm_study/analysis_figures.py` | the figures |
| `rhythm_study/group_analysis_window.py` | Rhythm Group Analysis |

---

## Results

*Not yet collected.*

---

## Discussion

*To be written once results are in.*
