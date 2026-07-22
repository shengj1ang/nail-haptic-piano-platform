# python-code

Python side of a piano practice / learning experiment setup: camera+MIDI
finger-accuracy detection, a virtual piano that drives LED feedback, and the
lower-level motor/LED/serial tooling both of those (and earlier prototypes)
are built on.

## Layout

```
app/                            UI package: camera + MIDI finger-accuracy detection, quiz, generator
launcher.py                     entry point - hub window for every tool below, grouped into the same
                                 numbered sections used throughout this README (1 Initial Setup ...
                                 7 Data Analysis, 9 Validation Experiments; section 8 Tele-training
                                 is an empty placeholder).
                                 Section 1 also holds the launcher-only Visual Guidance Cue Selection
                                 window (app/gui/cue_selection_window.py), which persists the quiz cue
                                 style (dot/hand) to config.json

setup_camera_wizard.py          entry point - camera selection/orientation (section 1: initial setup)
setup_keyboard_wizard.py        entry point - app/ calibration wizard (section 1: initial setup)
setup_midi_mapping_wizard.py    entry point - app/ MIDI mapping wizard (section 1: initial setup)

test_keyboard_preview.py        entry point - app/ profile preview/sanity-check (section 2: feature testing)
test_finger_accuracy.py         entry point - live finger/key detector (section 2: feature testing, see app/)
test_virtual_piano_led.py       manual-test UI (PySide6): on-screen piano that lights the physical LED
                                 strips (section 2: feature testing)
test_haptic_vibrator.py         manual-test UI: per-finger vibration motor check (section 2: feature testing)
app/gui/accelerometer_window.py manual-test UI (launcher-only): live X/Y/Z plot of one LIS3DH from the
                                 rig's ACC stream, sensor id selectable + persisted to config.json
                                 (accelerometer.sensor_id) (section 2: feature testing)

experiment_sequence_wizard.py   entry point - generates the controlled bimanual stimulus sequences for
                                 the main user study (30-event, difficulty alpha/beta/gamma, seeded, with
                                 built-in difficulty validation) and saves them under data/sequence/ -
                                 see SEQUENCE_GENERATOR_ALGORITHM.md (section 3: experiment sequence design)

music_recording_wizard.py       entry point - records a song (video+MIDI) and saves a fingering-annotated
                                 score under data/music/<song>/ (section 4: recording & playback)
music_playback.py               manual-test UI (PySide6): replays a saved song on an on-screen 88-key
                                 piano + finger dots (section 4: recording & playback, see app/music_recording.py)

student_quiz.py                 entry point - cue-response quiz with VISUAL finger cue (section 5: practice & assessment)
student_quiz_haptic.py          entry point - same quiz with HAPTIC finger cue (section 5: practice & assessment)
quiz_analysis.py                entry point - batch offline analysis of saved quiz sessions: outcome
                                 metrics table, LED sync alignment, per-event review/correction,
                                 carry-over review, participant CSV export (section 7: data analysis)

config.json                     app/ settings (auto-created): camera/MIDI, active_keyboard_profile,
                                 visual_cue_style
data/keyboard-profile/<profile>/   app/ calibration profiles (see "app/" below)
data/music/<song>/               saved teacher recordings (see app/music_recording.py)
data/sequence/<name>/            generated stimulus sequences (same meta.json/fingering.json layout as
                                 data/music/, see app/sequence_generator.py)
data/sequence_validation/<batch>/   difficulty-validation output for a generated sequence batch
                                 (app/stimulus_validation.py, viewable in the Sequence/Music Metrics window)
data/quiz/<attempt>/             saved quiz sessions (video+MIDI raw + analysis results; results.json
                                 carries per-event validity - "valid"/"invalid_carryover" - set during
                                 the manual carry-over review; raw/ also holds sync_align.json - the
                                 trial's confirmed LED sync anchor - and sync_detect.json, the audit
                                 trail of the anchor last used)
data/validation_experiments/<experiment>/   timestamped CSV/PNG/meta.json runs of the validation
                                 experiments (see validation_experiments/ below)
data/MainUserStudy/<participant>/   TrialStructure.json - the participant's randomised 27-trial
                                 schedule + live progress (see app/pilot_study.py); the Formal
                                 Experiment Session (launcher section 6, no standalone script)
                                 updates and resumes from this file after a crash. Also the export
                                 target: <P>_trials.csv / <P>_events.csv and figures/ (300 dpi
                                 PNGs + per-analysis CSVs from Participant Analysis)
hand_landmarker.task            MediaPipe hand landmark model, used by app/
requirements.txt                Python deps for app/ (conda env "fingercam")

profile_led_mapper.py           reusable, GUI-free: profile + MIDI note -> LED positions
note_led_map.py                 this LED rig's fixed wiring (position -> LED pixels), used by profile_led_mapper.py
note_audio.py                   reusable, GUI-free: MIDI note -> audio tone playback (no profile needed)

common/                         shared low-level modules (serial/motor/LED), used by both
                                 test_virtual_piano_led.py and test-script/
validation_experiments/         small hardware-validation experiments, separate from the main study
                                 (section 9: validation experiments) - one subfolder per experiment
                                 plus the shared rig.py serial helpers; GUI wrappers in
                                 app/gui/validation_experiment_window.py. Currently:
                                 lra_resonance_intensity_calibration/ (frequency + amplitude sweeps,
                                 full write-up in its README.md); runs are saved as timestamped
                                 CSV/PNG/meta.json triples under data/validation_experiments/<experiment>/
test-script/                    older, non-UI motor/LED/latency/MIDI scripts + their output
read_data_from_accelerometer/   legacy standalone LIS3DH sketch + plotters (old bare "x,y,z" serial
                                 format, pre-unified-firmware) - reference only, see its README;
                                 the live tool for the current rig is Accelerometer Live View above
archive/                        early FingerAccuracy prototypes, kept for reference only
runtime                         Python Environment in Windows, python 3.11.9, do not use or read or change this diretory when in development
venv.bat                        Do not read/write this file
launcher.bat                    Do not read/write this file
```

---

## app/ - FingerAccuracy pipeline

Camera + MIDI based detection of *which finger* pressed *which piano key*.

### Where this fits

This is the "which finger pressed this key" piece of a bigger piano
practice / learning experiment setup. Roughly, the full pipeline looks
like:

```
cue shown -> participant presses a key
  -> MIDI logs the key + a timestamp -> camera works out which finger -> analysis after the fact
```

`app/` only covers the camera part, but it's built so it can be called from
the rest of that pipeline either live during a session, or later from a
recording - see [Using it as a library](#using-it-as-a-library).

### How it works

There are three steps, and only the first two need doing once per
physical setup:

1. Calibrate: click on the camera image to mark each key's exact pixel
   range (a paint-bucket style fill, bounded by Canny edges). This gets
   saved as a *profile* - `data/keyboard-profile/<profile_name>/keyboard_template.json` plus
   `keyboard_key_map.png`, a pixel map where each pixel's value is the key
   id it belongs to plus one (0 for background).
2. Map MIDI notes to those keys: press the keys in order 1, 2, 3, ... and
   whatever MIDI note number each one actually sends gets recorded next to
   the template, as `midi_mapping.json`.
3. Live detection: MediaPipe finds fingertip pixel positions every frame.
   On each MIDI note-on, look up which key that note belongs to, then
   check which fingertip's pixel is inside that key's region (or closest
   to it, if none are) - that's the finger used.

Left hand thumb -> pinky = `L1`..`L5`, right hand thumb -> pinky =
`R1`..`R5`. Works fine with 0, 1, or 2 hands in frame - both hands showing
up isn't required.

A profile only stays valid as long as the camera and keyboard don't move
relative to each other. If they do, just re-run step 1.

### Setup

```bash
conda activate fingercam    # or: conda env create ...; pip install -r requirements.txt
```

Everything runs inside the `fingercam` conda environment (Python, OpenCV,
MediaPipe, mido, PySide6). `hand_landmarker.task` (MediaPipe's model file)
is bundled in this folder already.

### Pipeline scripts

Run these from inside `python-code/`.

| Script | Purpose |
|---|---|
| `setup_keyboard_wizard.py` | PyQt wizard - calibrate a new profile (click-based, no keyboard shortcuts). |
| `setup_midi_mapping_wizard.py` | PyQt wizard - press keys 1..N in order, records the MIDI note each sends. |
| `test_keyboard_preview.py` | Utility/demo - pick a profile from a dropdown and sanity-check it against the live camera, optionally showing MIDI note names instead of key numbers. |
| `test_finger_accuracy.py` | The live app - keyboard overlay + hand skeleton + real-time finger/key matching. |
| `test_virtual_piano_led.py` | Manual-test UI - on-screen piano that lights the physical LED strips (see below). |
| `music_recording_wizard.py` | PyQt wizard - records a song (video+MIDI), flashes the LEDs for sync, and saves a fingering-annotated score under `data/music/<song>/`. |
| `music_playback.py` | Manual-test UI - replays a saved song on an on-screen 88-key piano, lighting a dot for whichever finger played each note. |
| `experiment_sequence_wizard.py` | Stimulus generator for the main user study - matched families of 30-event bimanual sequences per difficulty level (α/β/γ), constraint-driven (D = C_m/C_s/C_c), seeded for reproducibility, with automatic difficulty validation. Algorithm: `SEQUENCE_GENERATOR_ALGORITHM.md`. Includes the read-only "Sequence/Music Metrics" viewer (`app/gui/sequence_metrics_window.py`). |
| `student_quiz.py` / `student_quiz_haptic.py` | Cue-response quiz over a saved song/sequence: LED key cue plus a visual (`student_quiz`) or nail-mounted haptic (`student_quiz_haptic`) finger cue; records the whole session (video+MIDI) under `data/quiz/<attempt>/` for offline scoring. |
| `quiz_analysis.py` | Batch offline analysis of saved quiz sessions: a checkable multi-quiz table with the report's full per-trial outcome measures, LED-anchored video/MIDI sync alignment (auto + manual), per-trial detail / per-event review-and-correction windows (including the carry-over validity review), and one-click per-participant CSV export - see [Data analysis](#data-analysis-launcher-section-7). |
| launcher section 6 (no standalone scripts) | The main user study tools: **Participant Trial Schedule** (`app/gui/pilot_schedule_window.py`) builds and saves a participant's randomised 27-trial schedule; **Formal Experiment Session** (`app/gui/experiment_session_window.py`) runs it - see [Main user study](#main-user-study-launcher-section-6). |
| launcher section 7, Participant Analysis (no standalone script) | Cross-trial single-participant analysis (`app/gui/participant_analysis_window.py`): condition/difficulty/learning charts, speed-accuracy trade-off, event-level error breakdown, finger confusion matrices, per-finger profiles - with 300 dpi figure + tidy CSV export. |
| `test_haptic_vibrator.py` | Manual test - drive each finger's vibration motor individually. |
| `setup_camera_wizard.py` | Camera selection/orientation wizard. |
| `launcher.py` | Hub window - one button per script above, grouped by stage; only one tool is open at a time (they share the camera). |

```bash
python setup_keyboard_wizard.py
python setup_midi_mapping_wizard.py
python test_finger_accuracy.py
python music_recording_wizard.py
python music_playback.py
```

Each script's docstring has more detail; `config.json` (auto-created on
first run) holds the camera index/flip settings, Canny thresholds, the
MIDI port, and which profile is "active" (used by default when a script
doesn't ask you to pick one).

### Main user study (launcher section 6)

The formal experiment tools have no standalone entry scripts - open them
from `launcher.py`, section "6. Main User Study":

1. **Participant Trial Schedule** (`app/gui/pilot_schedule_window.py`) -
   enter the participant's metadata, lock them to one generated stimulus
   batch under `data/sequence/`, and generate + save the randomised
   27-trial schedule (3 conditions x 3 levels x 3 unique sequences per
   cell, seeded shuffle, 2-min rests after trials 9 and 18) to
   `data/MainUserStudy/<participant>/TrialStructure.json`.

2. **Formal Experiment Session** (`app/gui/experiment_session_window.py`) -
   opens three windows at once:

   - **Session controller** - load a saved participant and see every
     trial's live status. Each row has a tick box plus its own Start
     button; shortcut buttons select the remaining trials of one
     condition ("Select remaining Visual (B)" etc.). "Run selected trials
     in order" walks the ticked trials smallest-index-first, pausing for
     a manual Continue between trials (with the 2-min rest countdown
     after trials 9/18). Every status change is written straight back to
     `TrialStructure.json`, so a crashed session reloads with its exact
     progress and resumes from the first non-completed trial.
   - **Trial runner** (`app/gui/experiment_runner_window.py`) - the quiz
     window re-purposed for scheduled trials: connect the LED strip and
     pick the MIDI port/timbre/timeout here once. Every trial records an
     ordinary quiz under `data/quiz/`, named
     `<participant>-T<index>-<condition><level>` (a rerun appends `-r2`,
     `-r3`, ... so no attempt's data is overwritten), which means the
     existing analysis tools (Quiz Analysis, review video) work on pilot
     trials unchanged. The per-trial analysis popup is suppressed
     mid-session; batch the analysis afterwards from launcher section 7.
   - **Participant-facing cue screen** (`app/gui/experiment_cue.py`) -
     created once per session, so it can be dragged/fullscreened onto the
     external display one time instead of reopening every trial.
     Condition B draws the visual finger cue on it; conditions A and C
     show only a status line, so no visual finger cue leaks into them.

   The three feedback conditions (`app/pilot_study.py`): **A** key-light
   only, **B** visual finger cue (dot/hand style from `config.json`'s
   `visual_cue_style`), **C** vibrotactile finger cue (per-finger motors,
   `app/haptic_cue.py`, rig connected lazily on the first C trial).

   Trials always run with `config.json`'s `active_keyboard_profile` (MIDI
   mapping + LED layout, same as every quiz); the `keyboard_profile`
   stored inside `TrialStructure.json` is *not* used at run time - it only
   records which profile limited the sequence generator's key range when
   the schedule was made.

### Data analysis (launcher section 7)

Every stored time in the codebase is an absolute Unix epoch timestamp
(`time.time()` floats - no ISO strings, no recorder-relative clocks;
`archive/migrate_to_epoch_timestamps.py` converted the pre-existing
data). The analysis workflow, all reachable from launcher section
"7. Data Analysis":

1. **Quiz Analysis** (`quiz_analysis.py` /
   `app/gui/quiz_analysis_window.py`) - the per-trial hub. Every saved
   quiz is a row in a checkable table (filter box + select
   shown/analyzed/not-analyzed shortcuts) with the report's full outcome
   measures as columns: Key Accuracy, the three finger-accuracy views
   (FA main = key∧finger over all events - the primary measure; FA |
   key ok; Note Accuracy | finger ok), stratified reaction times,
   timeouts/wrong-key counts, the carry-over review state, QC extra-press
   counts, unresolved/ambiguous detection rates, same-hand vs hand-switch
   splits, per-finger FA/TE (L1-L5, R1-R5), and FA under alternative
   thresholds θ ∈ {0.30..0.50}.

   - **Video sync**: mapping MIDI timestamps to video frames goes
     through `app/sync_led.py` - the trial's LED flash is auto-detected
     in the footage (box matched filter with the commanded pulse width +
     two self-checks) and saved to `raw/sync_align.json`; the per-row
     "Video Sync" button opens a 3-frame manual alignment window
     (`app/gui/video_sync_window.py`) for trials the detector can't
     handle. A saved (especially manual) alignment always wins over
     re-detection. Per-trial camera start-up error is real: −0.2 s to
     +0.55 s measured across the pilot data.
   - **Analyze selected (from video)** runs the MediaPipe pipeline +
     review-video render per checked quiz - behind a confirmation
     dialog, because it overwrites the detected-finger fields in
     `results.json` (manual finger corrections are lost; carry-over
     validity verdicts are kept). **Auto-align selected** (also behind a
     confirmation) persists fresh LED-flash detections as `sync_align.json`,
     skipping anything already aligned. **(data only)** is unguarded: it
     just re-summarizes straight from a (possibly hand-corrected)
     `results.json` and persists the headline numbers to `meta.json`.
   - **Double-click a quiz** for the per-trial detail window
     (`app/gui/quiz_detail_window.py`): all 30 events with verdicts,
     borderline-probability highlighting, a finger confusion matrix and
     distribution stats. **Double-click an event** there to play back
     its keypress ±5 s and correct the detected finger
     (`app/gui/event_review_window.py`) - corrections change
     `actual_finger` only (softmax kept as the audit trail) and are
     flagged in a Manual column.
   - **Carry-over review**: a matched response faster than 100 ms
     (`CARRYOVER_RT_THRESHOLD_S`) cannot be a reaction to its cue - pilot
     data shows these are the tail of the previous event's presses
     crossing the 0.4 s inter-event gap. Such events are flagged
     "review!" in the detail window's Validity column (counted as
     `suspected_carryover`), never auto-labelled anticipation.
     **Right-click the event row** to confirm one as `invalid_carryover`
     (or restore it): confirmed events are excluded from every statistic
     - RT and accuracy alike - and reported as `excluded_carryover`.
     Unmatched raw presses (near-simultaneous double-hits + inter-trial
     strays) are QC/debug info only (`qc_*` export columns, the detail
     window's QC line); they never enter the main outcome measures.
   - **Export participant data** writes `<P>_trials.csv` /
     `<P>_events.csv` (no wall-clock timestamps - RTs, verdicts and
     validity only) next to the participant's `TrialStructure.json`.

2. **Participant Analysis** (`app/gui/participant_analysis_window.py`) -
   cross-trial, within-subject: nine tabs (overview, learning
   progression incl. the within-cell trial 1→3 trend, difficulty,
   speed-accuracy trade-off, event-level error breakdown + wrong-key
   distance, per-condition finger confusion with a paper view, per-finger
   profiles + RT boxplots, RT distributions, data-quality audit), each
   with an auto-generated caption. Computations live GUI-free in
   `app/participant_analysis.py` (pandas DataFrames, participant-agnostic
   - ready for the future multi-participant aggregation) over the rows
   from `app/participant_export.py`, with unit tests in
   `test-script/test_participant_analysis.py`. "Export figures + data"
   writes 300 dpi PNGs + tidy CSVs (`<P>_<slug>.png/.csv`) under
   `data/MainUserStudy/<P>/figures/`.

   All participant-level readouts are descriptive; group-level inference
   is a separate, later step.

### Profile folder layout

```
data/keyboard-profile/<profile_name>/
  keyboard_template.json   # key ids, kind (white/black), note (usually null)
  keyboard_key_map.png     # pixel map: value = key_id + 1, 0 = background
  midi_mapping.json        # key_id -> MIDI note number (+ note name), from step 2
```

`data/` also holds the experiment data alongside these calibration
profiles, each kind in its own subfolder (`music/`, `sequence/`,
`sequence_validation/`, `quiz/`, `MainUserStudy/` - see the layout at the
top of this file).

Multiple profiles can coexist (different desks, cameras, keyboards); every
tool has a dropdown or `active_keyboard_profile` in `config.json` to pick
between them.

### Using it as a library

Nothing here is locked to the PyQt UI - the GUIs in `app/gui/`
are thin wrappers around plain functions/classes in the top-level package.

```python
from app import (
    Config, Camera, HandTracker, MidiListener,
    KeyboardTemplate, MidiMapping, match_note_to_finger,
)

cfg = Config.load()
template = KeyboardTemplate.load(f"data/keyboard-profile/{cfg.active_keyboard_profile}/keyboard_template.json")
mapping = MidiMapping.load(f"data/keyboard-profile/{cfg.active_keyboard_profile}/midi_mapping.json")

tracker = HandTracker()
midi = MidiListener()          # picks the first available port if none given

with Camera(cfg.camera) as cam:
    while True:
        frame = cam.read()
        hands = tracker.process(frame)          # {"Left": Hand(...), "Right": Hand(...)}

        for event in midi.pop_events():         # MidiEvent(time, note)
            match = match_note_to_finger(event.note, template, mapping, hands)
            if match:
                print(match.finger, "pressed key", match.key_id + 1, "note", event.note)
```

#### Record now, analyze later

`MidiListener` timestamps every event with an absolute epoch `time.time()`
(the project-wide convention - see [Data analysis](#data-analysis-launcher-section-7)),
so a session's MIDI can be logged and matched against a recorded video
afterward instead of processed live:

```python
from app import MidiListener, save_midi_log, analyze_recording

midi = MidiListener()
# ... run your experiment, e.g. cv2.VideoWriter recording the same camera ...
save_midi_log(midi.pop_events(), "session1_notes.json")

# Later, with no camera or MIDI device needed:
results = analyze_recording(
    video_path="session1.mp4",
    midi_log_path="session1_notes.json",
    keyboard_profile_name="desk_webcam",
    sync_path="session1_sync.json",   # SyncInfo - see below
)
for r in results:
    print(r)   # FingerMatch(note=..., key_id=..., finger=..., inside=...) or None
```

Epoch event times are mapped to video frames through `sync_path` - a saved
`app.music_recording.SyncInfo` (video/MIDI start times plus the sync LED
flash times), which `app/sync_led.py` anchors on the flash it detects in
the footage; this is exactly how the recording/quiz tools save their
`raw/sync.json`. Without `sync_path`, event times are assumed to already
be video-relative seconds (the legacy pre-epoch convention only).

`Camera` also accepts a video file path in place of a live camera index
(`CameraConfig.index` takes either) - useful for replaying a recording
through the same live-style code path instead of `analyze_recording`.

### app/ module layout

```
app/
  camera.py                    # Camera - cv2.VideoCapture wrapper (index or video file)
  config.py                    # Config / *Config dataclasses, JSON load/save
  hand_tracking.py             # HandTracker, Hand - MediaPipe wrapper, L1-L5/R1-R5 fingertips
  midi.py                      # MidiListener, MidiEvent, list_input_ports, save/load_midi_log
                               #   (all event times are absolute epoch timestamps)
  finger_matching.py           # match_note_to_finger, FingerMatch - the core matching logic
  offline.py                   # analyze_recording - recorded video + MIDI log -> matches,
                               #   frame mapping via the LED sync anchor (app/sync_led.py)
  sync_led.py                  # video/MIDI sync anchoring: LED flash detection, sync_align.json
                               #   persistence (manual > auto > start-times fallback)
  profiles.py                  # list_profiles - discover data/keyboard-profile/<name>/ folders
  music_recording.py           # song save/load (meta.json/fingering.json), raw capture, sync marks
  song_library.py              # combined data/music/ + data/sequence/ listing for song pickers
  sequence_generator.py        # stimulus generation: D = (C_m,C_s,C_c) metrics, level constraints,
                               #   matched families, seeded RNG (see SEQUENCE_GENERATOR_ALGORITHM.md)
  stimulus_validation.py       # difficulty validation of the alpha/beta/gamma level pools
  pilot_study.py               # main user study: 27-trial randomised schedules (3 conditions x
                               #   3 levels x 3 sequences, method.tex "Trial Structure"), saved to
                               #   data/MainUserStudy/<participant>/TrialStructure.json with
                               #   per-trial status for crash-resume
  quiz.py                      # cue-response quiz logic shared by the visual/haptic quiz tools
                               #   and the pilot study's trial runner; summarize() computes the
                               #   report's per-trial outcome measures (three FA views, RT
                               #   stratification, per-finger stats, threshold sensitivity) and
                               #   excludes human-confirmed invalid_carryover events, reporting
                               #   suspected/excluded carry-over counts; count_extra_presses()
                               #   is the QC-only unmatched-press breakdown (double-hits vs
                               #   inter-trial strays)
  participant_export.py        # one participant's trials/events as unified rows (the adapter the
                               #   analysis + CSV export share); disk-first quiz-dir resolution
                               #   (highest -rN retake wins)
  participant_analysis.py      # GUI-free cross-trial computations (pandas, participant-agnostic):
                               #   speed-accuracy trade-off, event outcome classification, finger
                               #   confusion, wrong-key distance - unit-tested in test-script/
  haptic_cue.py                # per-finger vibration cue driver used by the haptic quiz and the
                               #   pilot study's condition C trials
  keyboard/
    template.py                # KeyBox, KeyboardTemplate - the pixel-exact key map
    wizard.py                  # KeyFillWizard - paint-bucket key segmentation
    midi_mapping.py            # MidiMapping - key_id <-> MIDI note
    visualize.py                # color/label helpers for drawing a key_map
    detector.py                 # Canny edge detection used by the calibration wizard
  gui/                          # PyQt windows/pages for the scripts above (incl. the sequence
                                #   generator window, metrics viewer, validation dialog, cue window,
                                #   the pilot-study windows: participant schedule, formal-session
                                #   controller, trial runner, participant-facing experiment cue
                                #   screen; and the analysis windows: quiz_analysis_window,
                                #   quiz_detail_window, event_review_window, video_sync_window,
                                #   participant_analysis_window)
```

`archive/` holds earlier, now-superseded prototypes of this same detector
(`detect_finger_with_MIDI_keyboard_v2/v3/v4.py` plus their pickled models),
kept only for reference.

---

## profile_led_mapper.py + note_led_map.py - MIDI note -> LED, and test_virtual_piano_led.py - manual test UI

This LED rig is hardware-specific and isn't meant to generalize to other
keyboards, but the identifier it's keyed by is: `note_led_map.py` only knows
about *this strip's fixed wiring* (which pixel(s) light up for the Nth
white/black key, left to right - see `test-script/test_led_array.py`), and
its `NoteLEDMapper.light_key(note)` / `clear_key(note)` take a MIDI note
number directly, no separate key_id.

`profile_led_mapper.py` is the reusable, GUI-free glue that combines that
wiring with a calibration profile's actual `key_id -> note` mapping (from
`data/keyboard-profile/<profile>/`, produced by `setup_keyboard_wizard.py` +
`setup_midi_mapping_wizard.py`) to build a ready-to-use `NoteLEDMapper` -
`build_mapper(led, profile_name)` is the one-shot entry point. Import this
directly in any script that needs "MIDI note -> light the right LED"
without pulling in a GUI.

`test_virtual_piano_led.py` is a thin PySide6 wrapper around the above,
for manual testing: pick a profile, and an on-screen piano mirroring that
profile's calibrated keys appears. Press and hold a virtual key (or the
matching real MIDI key) and the corresponding LED(s) on the physical
Teensy WS2812 strips light up; release to turn them off.

```bash
python test_virtual_piano_led.py
```

If the keyboard's transpose/octave setting changes, only the profile needs
re-calibrating (`setup_midi_mapping_wizard.py`) - `note_led_map.py`'s wiring table
doesn't need to change.

---

## note_audio.py - MIDI note -> audio tone playback

Reusable, GUI-free playback of a continuous tone for whichever MIDI note(s)
are currently held down, meant to be imported by future teaching modules
the same way `profile_led_mapper.py` is - a note-keyed `play_key(note)` /
`stop_key(note)` pair, mirroring `NoteLEDMapper`'s `light_key`/`clear_key`.
No calibration profile needed here: frequency comes straight from the MIDI
note number via the standard equal-tempered formula (note 69 = A4 = 440Hz),
since pitch is physics, not a fact about specific hardware.

```python
from note_audio import NoteAudioPlayer

with NoteAudioPlayer() as player:
    player.play_key(60)   # starts a middle-C tone, fades in
    ...
    player.stop_key(60)   # fades it back out
```

Uses `sounddevice` (already a project dependency via `test-script/measure_latency*.py`)
to mix all currently-held notes into one output stream, with a short linear
fade on press/release to avoid clicks.

---

## common/ - shared serial/motor/LED modules

Low-level modules shared by `test_virtual_piano_led.py` above and the scripts in
`test-script/` below. Not a UI on its own.

| Module | Purpose |
|---|---|
| `serial_utils.py` | Cross-platform auto-detection and opening of the serial port (USB/Arduino/Teensy first, with manual fallback). |
| `controller.py` | `VibratorController` - high-level async interface to the multi-motor vibration firmware (connect, pulse, stop). |
| `led_controller.py` | `LEDArrayController` - high-level interface to the Teensy WS2812 LED firmware (set pixel, clear, brightness). |

---

## test-script/ - older motor/LED/latency/MIDI scripts

Non-UI scripts from earlier stages of the project - motor control demos,
latency measurement, and MIDI/LED debugging tools. All import their shared
logic from `common/`.

| Script | Purpose |
|---|---|
| `demo_send_motor_command_async.py` | Demonstrates asynchronous motor control - starts one motor, injects others while it's still running, to show non-blocking behavior. |
| `demo_keyboard_control.py` | Real-time keyboard control - maps keys A-`;` to motors 0-9, supports multiple simultaneous key presses. |
| `detect_live_motor.py` | Real-time microphone-based detection tool for tuning vibration-detection parameters (threshold, frequency band). |
| `measure_latency.py` | Single-motor latency measurement - triggers a motor, detects the acoustic onset, estimates end-to-end latency. |
| `measure_latency_multi_motor.py` | Multi-motor latency measurement - random motor selection, multiple runs, per-motor comparison, automatic plotting into `latency_results/`. |
| `test_led_array.py` | Manual LED test script - lights specific pixels on specific strips to sanity-check wiring/colors. |
| `midi_probe.py` | Ground-truth calibration probe - prints the MIDI note number for each physical key as you press it, left to right. |
| `MIDI.py` | Minimal listener that prints every incoming MIDI message on two ports. |
| `plot_acc_from_ACC_stream_autostart.py` | Live-plots `ACC,x,y,z` accelerometer lines streamed over serial, sending the start/stop stream commands automatically. |
| `test_participant_analysis.py` | Unit tests for `app/participant_analysis.py` (the GUI-free cross-trial computation layer). |

`latency_results/` holds the plots/summary generated by the latency scripts;
`video_demo/` holds recorded demo videos of the latency tests.

## Configuration

In `test-script/measure_latency_multi_motor.py`:

```python
TEST_MOTORS = [0, 2, 6]
NUM_RUNS = 20
```

You can modify which motors to test, the number of runs, and detection
parameters.

## Notes

- Using pins 0 and 1 may conflict with serial on some boards.
- Ensure the power supply is sufficient for multiple motors.
- Microphone positioning significantly affects latency-measurement accuracy.
