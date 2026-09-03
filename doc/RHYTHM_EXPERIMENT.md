# The Rhythm Experiment

> **This is an exploratory pilot, it is not part of the thesis, and it
> stopped after two testers.** No result here is reported in the
> [final report](../shengjiang_final_report.pdf). It was built to find out whether the
> cue-withdrawal question is worth asking properly and whether this
> apparatus can ask it. The answer to the second half is "not yet, and
> here is the list" — see [Results](#results) and
> [What would have to change](#what-would-have-to-change). Time ran out
> before that list could be worked through, so the study is left where it
> is rather than half-fixed. Read it as a lab notebook, not a report.

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
  - [Refreshing already-recorded data](#refreshing-already-recorded-data)
  - [Data, files and how to run it](#data-files-and-how-to-run-it)
- [Testers](#testers)
- [Results](#results)
- [Discussion](#discussion)
  - [What would have to change](#what-would-have-to-change)

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
| **Validation experiments** | section 10 | the actuators themselves - LRA resonance and intensity calibration, motor-to-accelerometer delay, ERM/LRA spectra, adhesive comparison | 2026-07-22 |
| **Main User Study** | section 6 | which cue modality teaches a key-and-finger mapping best - key-only / visual / vibrotactile x 3 difficulty levels | 2026-07-22 |
| **Tele-training** | section 8 | guidance delivered over a network: a teacher, a student and a relay running at once, with its own latency budget and analysis | 2026-08-06 |
| **Rhythm experiment** | section 11 | *this document* - what survives when the cue is withdrawn | 2026-08-25 |

The validation experiments are experiments in the same sense as the rest: each
has a protocol, recorded runs and its own write-up (see
`doc/validation-experiments.md`). They are the reason the cue amplitude and
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

Full detail in [`doc/melody-generator.md`](melody-generator.md); this
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
in [`doc/melody-generator.md`](melody-generator.md).)

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
`candidate_pool = 40`, ranks them on

```
musicality - 0.3 x difficulty
```

and lets the seed choose between the best five. Choosing between them rather
than always taking the single highest is what keeps two seeds from returning
the same tune — an argmax discarded the ~39 distinct melodies each pool holds,
and one of them then came up in 9.3% of runs. See
[`doc/melody-generator.md`](melody-generator.md) for the
measurements. Which candidate was taken is recorded in the melody's JSON.

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

**The cue lasts the note, not the key press.** Both channels continue after
the key goes down and stop when the beat ends: the participant holds the key
while they can feel the buzz, and lets go when it stops. Every other quiz on
this platform clears the cue the instant a response is recorded, which teaches
the onset and nothing else — under that rule a 3-beat note and a 1-beat note
are the same experience, and the durations built into the melody could not be
learned at all. Since the melody's notes are 1, 2 and 3 beats long and
duration is one of the measured outcomes, the cue has to carry it.

The hold is measured **from the key press**, not from the cue. A training
trial is paced entirely by the participant — the next note is not cued until
the last is answered — so the note's slot effectively begins when they play
it. Anchoring on the cue instead would mean the slower a participant was, the
less of the note they would feel, and a participant slower than the note is
long would feel none of it. The anchor and the per-note hold are both written
into the trial's sidecar (`sustain_anchor`, `sustain_s`), so a later change of
mind about it is visible in the data rather than inferred from whichever code
was running at the time.

Probes and the final test need none of this: they are performances against
the melody's real grid, where the backlight already runs on the melody's own
note-on and note-off times.

This shape is right for teaching, and wrong for measuring rhythm. The next cue
arrives a fixed 0.4 s after the previous key press, so a participant *cannot*
play ahead of the apparatus, and their note timing is its timing, not theirs.
Training therefore yields a **reaction time** and no onset error at all.

**A probe gives the pitch and withholds the time.** It is note-by-note like
training, and what separates it is what it does *not* supply. The backlight
lights the target key and waits; it goes out when the key goes down. It never
runs on a clock and it never shows how long a note lasts, so **when to press
and when to release are entirely the participant's**. The haptic cue is off,
and there is no held cue.

The participant therefore has to remember the rhythm and the fingering, and is
only spared having to remember which note comes next. Their timing is compared
with the melody as **intervals** rather than against an absolute grid — see
[Measures](#measures).

> **This replaced an earlier probe form, and the reason is worth recording.**
> Probes were first run as performances with the backlight walking the
> melody's grid, so that every note had a time it was *due* and onset error
> was an absolute number. Tester 1's data showed what that actually measured:
> their onset error in the first probe began 1.6 s behind the light and shrank
> to a few hundred milliseconds by the end of the trial — the signature of
> someone **tracking a light**, not of someone recalling a rhythm. The same
> light had already told them which note came next, so the probe was left
> measuring fingering alone. Giving the pitch and withholding the time is what
> the design was trying to do all along.

**The final test is the only whole performance**, and it is unguided: nothing
is shown, and the melody's grid runs invisibly as the reference the
performance is scored against. The experimenter ends the trial. It is
re-anchored on the participant's own first note — nothing told them when to
start, so their overall start time is not a rhythm error.

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
2. **Absolute inter-onset-interval (IOI) error** — the gap from one note to
   the next, compared with the melody's own, in milliseconds; the main timing
   measure. **Signed** IOI error is kept alongside it, to show whether
   intervals are stretched or compressed rather than just how far off they are.

   Intervals rather than absolute onsets, because a probe cues each note and
   waits: there is no external clock for the participant to be early or late
   against, so an absolute onset error would only measure how long the
   apparatus took to ask. What a remembered rhythm *is* is the shape — this
   note twice as far from the last as the one before it — and that is what an
   interval captures. It also forgives a performance played uniformly faster
   or slower, which is a tempo choice rather than a failure of recall.

   **Absolute onset error against the grid is still computed, but only for
   the final test**, the one trial played straight through.

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

### Refreshing already-recorded data

`raw/midi_raw.json` is the complete record of a trial and is never rewritten;
`results.json` is derived from it, so a scoring bug found later can simply be
redone:

```bash
cd main && python -m rhythm_study.rescore P01
```

It rewrites `results.json` (keeping the original beside it as
`results_before_realign.json`) and re-syncs the cached counts in `meta.json`.

**Only performances are re-paired.** A probe or the final test is one
performance, so its pairing is redone by sequence alignment. Training is left
exactly as recorded, and that is deliberate: training is cue/response, each
note had its own response window, and the press the loop accepted really was
that note's answer. A press swallowed in the gap cost that note's *reaction
time* — the participant played it twice — but not its key or finger.

Re-pairing training on a widened window was tried and made the data worse.
Nothing distinguishes "the right note, played in the gap and ignored" from "a
slip, immediately corrected" except the note itself, and taking the first
press in the window credits the slip: in Tester 1's seventh training trial it
took an A#3 played 0.4 s before the cue as the answer to a B3 the participant
went on to play correctly, and every note after it shifted by one. Training
trials are only annotated (`swallowed_presses`, `timing_reliable` in the
sidecar), so the analysis can drop their timing while keeping their accuracy.

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
    rhythm_recues.json    sidecar: first/last cue times, re-cue count, cue hold
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

## Testers

A pilot changes as it runs, so who ran which protocol is part of the record.

Two testers ran, under **different probe forms** — the form changed because
Tester 1's data showed the first one was measuring the wrong thing — and on
different melodies. Neither is a participant in any reported study.

### Tester 1 (`P01`) — 2026-08-28

Full 19-trial schedule on `melody_seed415411`, but under **the earlier probe
form**: probes were performances with the backlight walking the melody's grid.
Their data is therefore **not comparable with later testers'** on any timing
measure, and only partly comparable on accuracy.

What it was nevertheless good for — and it changed the design three times:

| What it showed | What changed |
|---|---|
| Onset error in probe 1 ran from +1.6 s to −0.4 s within the trial | The probe was measuring light-tracking, not recall. Probes became pitch-only, with the timing left to the participant. |
| Probe key accuracy scored 9/15, 9/15, 5/15 | The participant had in fact played 14, 15 and 15 of the 15 notes. Positional pairing was scoring one dropped note as fourteen errors; scoring moved to sequence alignment. |
| 26 key presses in the raw MIDI log had no matching response | The runner only accepted presses from the cue onwards, so a note played in the gap before the cue was discarded and had to be played again. The response window now opens when the previous note stops sounding. |

Their trials were re-scored in place with the alignment fix
(`python -m rhythm_study.rescore P01`); the original scoring is kept beside
each one as `results_before_realign.json`, and the raw MIDI was never touched.
Training trials were deliberately **not** re-paired — see
[Refreshing already-recorded data](#refreshing-already-recorded-data).

### Tester 2 (`P02`) — 2026-08-28

Full 19-trial schedule on `melody_seed42`, under **the current probe form**
(the backlight names the key and waits) and with all three apparatus fixes in
place. The first clean run.

The fixes are visible in the data: their probes recorded **no extra presses at
all**, where Tester 1's had one to two per probe from presses swallowed in the
gap, and positional pairing and alignment agree on every probe (15/15 either
way) because there was nothing left to resynchronise.

## Results

**Two testers, fifteen notes per probe, and different probe forms between
them.** Nothing below is a finding about learning. It is a description of what
two people did and, mostly, of what the apparatus turned out to be able and
unable to measure. No statistical test was run: the analysis refuses to test
below five complete participants, which is the right call here.

### What the numbers were

| | Probe 1 | Probe 2 | Probe 3 | Final |
|---|---|---|---|---|
| **Finger accuracy** — Tester 1 | 27% | 60% | 33% | 47% |
| **Finger accuracy** — Tester 2 | 87% | 93% | 60% | 33% |
| **Key accuracy** — Tester 1 | 93% | 100% | 100% | 67% |
| **Key accuracy** — Tester 2 | 100% | 100% | 100% | 93% |
| **\|IOI error\|** — Tester 2 | 405 ms | 267 ms | 408 ms | 304 ms |

Withdrawal cost (probe minus the training trial before it), in percentage
points of finger accuracy: **−73, −33, −60** for Tester 1 and **−13, +53, −27**
for Tester 2.

Tester 1's timing is omitted from the table above: their probes ran under the
old form, where the backlight paced the melody, so their onset numbers measure
how well they tracked a light.

### 1. The bottleneck is the fingering, not the notes

Key accuracy sits at or near 100% in every probe for both testers, and averages
95–99% in training. Whatever they were failing to retain, it was not *which
notes to play* — the melody's fifteen notes over seven or fewer keys are simply
easy to remember.

Finger accuracy is where everything happens, and where the haptic cue is the
only thing carrying the information. That is the design working as intended:
the manipulation lands on exactly the channel the cue owns.

### 2. Removing the cue costs a lot, immediately

Five of the six probe points are below the training trial that preceded them,
several by a wide margin. Tester 1's first probe drops 73 percentage points.
The substitutions are systematic rather than random — Tester 1 repeatedly
answered a left-hand target with the right thumb or index:

```
target L2 -> played R1   x5
target L4 -> played L2   x3
target L2 -> played R2   x3
```

which is what falling back on a comfortable default looks like, not what
forgetting looks like.

### 3. There is no sign of that cost shrinking

The retention question is whether the withdrawal cost gets smaller with more
practice. It does not. **Both testers peak at probe 2 and fall back at probe
3** — the probe that follows the most practice is the worst of the three for
both of them:

```
Tester 1   27% -> 60% -> 33%
Tester 2   87% -> 93% -> 60%
```

Two people at fifteen notes a probe cannot establish that shape, but it is the
opposite of what improvement would look like, and it is the same shape twice.

### 4. Training cannot show learning at all

Tester 1's finger accuracy across the fifteen training repetitions:

```
100 100  93 100 100  87 100  93  87  93  93 100  87  93  93
```

At ceiling from the first repetition and very slightly *down* by the last. That
is not a participant who failed to learn; it is a measurement that cannot
move. **The haptic cue tells them which finger to use, so a training trial
scores near 100% whether they have learned anything or not.** Repetition 1 and
repetition 15 are indistinguishable by construction.

So the design has only three informative measurement points per person, of
fifteen notes each, and no way to see the learning it is supposedly measuring
in between them.

### 5. The finger measurement is not solid enough to lean on

`target_finger_probability` is the camera's confidence in the finger the
melody asked for; the decision threshold is 0.40.

| | mean p(target) | events below 0.40 | manually reviewed |
|---|---|---|---|
| Tester 1 | 0.67 | 41 / 282 | **0** |
| Tester 2 | 0.53 | 49 / 284 | **0** |

Tester 2 spent the session **hovering at the threshold**, so a large share of
their finger verdicts were decided by a margin that noise could flip. Not one
event was put through the manual review queue that `app.finger_matching` exists
to feed.

That is enough to explain the one number above that makes no sense. Tester 2's
withdrawal cost at probe 2 is **+53 pp** — the probe scored better than the
training trial before it, which a "cost" cannot do. The training trial in
question is repetition 10, scored 40%, and its mean p(target) is 0.38: the
camera lost the hand, not the participant. The same is true of their
repetitions 12–15 (0.42, 0.37, 0.49, 0.59).

**Until those events are reviewed, no finger number here should be quoted.**

## Discussion

### What this was worth

The apparatus, not the answer. Three faults were found, all of which silently
corrupted data and none of which would have been visible without running real
people through it:

- a **press played in the gap** before the cue was discarded, so the
  participant pressed, nothing happened, and they pressed again — 26 times in
  Tester 1's session alone;
- **positional pairing** scored one dropped note as fourteen errors, turning a
  15/15 performance into 5/15;
- the **probe was measuring light-tracking**: Tester 1's onset error began 1.6 s
  behind the backlight and shrank through the trial, which is what following a
  metronome looks like, and the same light had already given away which note
  came next.

All three are fixed, and Tester 2's session shows the fixes holding. That is a
real result for a pilot, and it is the reason to run one.

### The obvious reading of probe 3, and why the design cannot test it

Both testers were worse at the third probe than the second. The experimenter's
reading is disengagement: **fifteen repetitions of the same fifteen-note melody
is boring**, and a bored participant stops trying to use the fingering they
were taught and reaches for whatever is comfortable — which is exactly the
substitution pattern the errors show.

That is plausible and it fits. It is also **untestable in this design**,
because practice, time-on-task and boredom all increase together and the
probes are the only measurement points. Probe 3 is simultaneously the
best-practised, the most fatigued and the most bored point in the session, and
nothing separates the three. A design that wanted to tell them apart would have
to break the confound — vary the practice amount between participants, or
interleave the probes differently, or measure engagement directly.

Note also that the same reading fits the *other* interpretation: a participant
who has genuinely learned the fingering has less reason to attend to it, and
looks identical to one who has stopped caring. The data cannot separate
"learned it and relaxed" from "gave up".

### What would have to change

Roughly in order of how much they cost:

1. **Review the finger verdicts.** The queue exists and was never used. Nothing
   about fingering can be concluded before this, and it is the cheapest item on
   the list.
2. **Make training measurable.** It is at ceiling because the cue gives the
   answer. Withholding the haptic on a few scattered training notes, or scoring
   training on reaction time and duration rather than accuracy, would restore a
   learning curve.
3. **More notes per probe.** Fifteen gives a binomial standard error of about
   13 pp, which is wider than most of the differences discussed above. Playing
   each probe twice would cost two minutes and cut that to about 9 pp.
4. **Break the practice/boredom confound.** See above; this is a design change,
   not a parameter.
5. **A control condition.** There is nothing to compare retention *against*.
   A group that trains without the haptic cue would say whether any of this is
   about the cue at all.
6. **More than two people.** The analysis will not run a test below five, and
   it is right not to.

### Why it stopped here

Time. The list above is a redesign rather than a fix, and the thesis it would
have supported is finished without it. What exists is a working apparatus, a
protocol that has already been corrected three times against real data, an
analysis pipeline whose measures were chosen *after* seeing what the apparatus
could actually measure, and two sessions' worth of recordings kept in full —
raw MIDI, video and all — so that anything here can be re-derived by whoever
picks it up.
