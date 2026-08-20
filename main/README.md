# main (python code)

Python side of a piano practice / learning experiment setup: camera+MIDI
finger-accuracy detection, a virtual piano that drives LED feedback, and the
lower-level motor/LED/serial tooling both of those (and earlier prototypes)
are built on.

## Layout

```
app/                            UI package: camera + MIDI finger-accuracy detection, quiz, generator
launcher.py                     entry point - hub window for every tool below, grouped into the same
                                 numbered sections used throughout this README (1 Initial Setup ...
                                 10 Tools). Section 8 Tele-training is the one
                                 section whose buttons start SEPARATE processes rather than a
                                 sub-window, because a student, a teacher and a relay have to run at
                                 the same time - see "Tele-training" below.
                                 Section 10 Tools is data housekeeping, not part of running or
                                 analysing a session - see "Quiz data maintenance tools" below.
                                 Section 1 also holds two launcher-only settings windows: Visual
                                 Guidance Cue Selection (app/gui/cue_selection_window.py), which
                                 persists the quiz cue style (dot/hand) to config.json, and Haptic
                                 Actuator Defaults (app/gui/haptic_config_window.py), which persists
                                 the actuator in use and each actuator's default frequency/amp to
                                 config.json's "haptic" block (see "Haptic actuator configuration")

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

student_remote_guidance.py      entry point - Student Client: receives a remote teacher's key/finger
                                 guidance and cues it locally (section 8: tele-training)
teacher_remote_guidance.py      entry point - Teacher Client: turns what the teacher plays into
                                 guidance, and uploads/triggers pre-recorded sequences (section 8)
remote_latency_benchmark.py     entry point - relayed-path latency benchmark, CLI or --gui (section 8)
remote_guidance/                the tele-training client package (config, protocol, timing, network
                                 client, composite cue, student/teacher apps, benchmark) - see
                                 "Tele-training" below
server/                         the relay server: FastAPI REST + WebSocket, SQLite, JWT auth. Copyable
                                 on its own to a remote host; imports nothing from this platform.
                                 Full operator documentation in server/README.md
REMOTE_GUIDANCE.md              design + handover notes for the whole tele-training module: file map,
                                 the invariants that must not be broken (reaction-time origin, clock
                                 rules, what the server may not do), traps, and the list of known gaps.
                                 START HERE before changing anything under remote_guidance/ or server/
quiz_analysis.py                entry point - batch offline analysis of saved quiz sessions: outcome
                                 metrics table, LED sync alignment, per-event review/correction,
                                 carry-over review, participant CSV export (section 7: data analysis)
tool_compress_review_videos.py  entry point (console) - scans data/quiz/*/review.mp4 and converts only
                                 non-H.264 review copies with ffmpeg; never enters raw/. A thin
                                 frontend over app/review_compress.py, which the launcher's
                                 "Review Video Compression" window (section 10) also drives
tool_backup_quiz_to_zip.py      entry point (console) - participant backup: creates one tested
                                 data/quiz-zip/Pxx.zip for each complete P01-P20 participant.
                                 A thin frontend over app/quiz_backup.py, which the launcher's
                                 "Participant ZIP Backup" window (section 10) also drives

config.json                     app/ settings (auto-created): camera/MIDI, active_keyboard_profile,
                                 visual_cue_style, haptic (actuator in use + per-actuator default
                                 frequency/amp - see "Haptic actuator configuration"), and
                                 remote_guidance (the tele-training block: relay URL plus the
                                 student's and teacher's own camera/MIDI/profile/serial ports - it
                                 never redefines the shared camera/midi/active_keyboard_profile
                                 above; see "Tele-training")
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
data/quiz-zip/Pxx.zip            optional participant-level backups created by
                                 tool_backup_quiz_to_zip.py; never used as live experiment input
data/validation_experiments/<experiment>/   timestamped CSV/PNG/raw_acc.npz/meta.json runs of the
                                 validation experiments (see validation_experiments/ below); the
                                 raw_acc.npz holds the run's full three-axis accelerometer samples,
                                 so both intensity metrics and every chart can be regenerated
                                 offline
data/remote_guidance/latency/<run>/   latency benchmark output: samples.csv, summary.json,
                                 latency.png (see "Tele-training")
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
                                 plus three shared modules: rig.py (serial helpers + the ACC sample
                                 collector), acceleration_metrics.py (the two vibration-intensity
                                 metrics, the counts -> m/s^2 conversion and the raw three-axis
                                 sample store) and report.py (the console-style text statistics each
                                 experiment can rebuild from a saved run, printed by the GUI when a
                                 run is loaded). GUI wrappers in
                                 app/gui/validation_experiment_window.py. Currently:
                                 lra_resonance_intensity_calibration/ (frequency + amplitude sweeps),
                                 actuator_spectrogram/ (2-D drive-frequency x amp intensity map),
                                 motor_acc_delay_experiment/ (LRA/ERM command-to-vibration latency)
                                 and adhesion_vibration_comparison/ (relative vibration transfer of
                                 three mounting adhesives, LRA-only, with its own three-run
                                 comparison analysis),
                                 each with a full write-up in its README.md; validation_experiments/
                                 README.md documents the shared metric/raw-data layer. Runs are saved
                                 as timestamped CSV/PNG/raw_acc.npz/meta.json sets under
                                 data/validation_experiments/<experiment>/
test-script/                    older, non-UI motor/LED/latency/MIDI scripts + their output
read_data_from_accelerometer/   legacy standalone LIS3DH sketch + plotters (old bare "x,y,z" serial
                                 format, pre-unified-firmware) - reference only, see its README;
                                 the live tool for the current rig is Accelerometer Live View above
../archived/python-prototypes/  the former archive/ folder: early FingerAccuracy prototypes,
                                 one-off utilities, and old bug reports - reference only
runtime                         Python Environment in Windows, python 3.11.9, do not use or read or change this diretory when in development
runtime/bin/                    optional drop-in folder for ffmpeg / 7z executables (see
                                 "Where ffmpeg and 7z come from" below). Searched before PATH;
                                 the executables themselves are not committed
venv.bat                        Do not read/write this file
launcher.bat                    Do not read/write this file
```

---

## Quiz data maintenance tools (launcher section 10)

Two tools for looking after data that has already been collected. Neither
is part of running a session or analysing one, and neither reads anything
the experiment reads.

Each has two frontends over one implementation:

| | window (launcher section 10) | console | shared logic |
| --- | --- | --- | --- |
| review videos -> H.264 | Review Video Compression | `python3 tool_compress_review_videos.py` | `app/review_compress.py` |
| participant backups | Participant ZIP Backup | `python3 tool_backup_quiz_to_zip.py` | `app/quiz_backup.py` |

The window and the script do the same thing to the same files, because
neither decides anything: **Scan** is the script's first phase, and
**Convert** / **Create archives** is what answering `1` at its prompt
does. Everything below describes both. The windows add only what a
terminal cannot: a table of what was found, an overall and a per-file
progress bar, a running log, and a **Stop** button.

Paths are anchored to this `main/` directory, so the scripts no longer
have to be run from here (though they still can be), and the windows work
wherever the launcher was started from.

Both windows keep running when you open another launcher tool - a
conversion pass is minutes of work and should not be thrown away by a
button press - and both are closed by closing the launcher.

### Where ffmpeg and 7z come from

Neither program is a Python dependency, so `app/tool_binaries.py` looks
for them in two places, in this order:

1. **`main/runtime/bin/`** - drop `ffmpeg.exe` / `7z.exe` (or their
   macOS/Linux equivalents) in that folder and nothing has to be
   installed on the machine. `launcher.bat` prepends the folder to `PATH`
   for the window it starts, and `app/tool_binaries.py` prepends it
   in-process as well, so anything the tools start themselves finds the
   same copy. Both are temporary: nothing is written to the system `PATH`
   or the registry.
2. **`PATH`**, as installed system-wide.

The lookup uses `shutil.which`, so on Windows the name `7z` finds
`7z.exe` without the extension being spelled out. 7-Zip is accepted under
any of the names `7z`, `7zz` or `7za` - all three can write and test the
ZIP archives the backup tool asks for.

Each window shows which copy it is using, or a warning naming
`runtime/bin` if it found none, and has a **Re-check** button so a
program dropped in there is picked up without restarting the launcher.
The console tools print the same message and exit with status 2 rather
than starting work they cannot finish.

### Compress generated review videos to H.264

```bash
python3 tool_compress_review_videos.py
```

Scans every direct `data/quiz/<attempt>/review.mp4`, reports its codec,
and asks before making changes. Files already encoded as H.264 and
unreadable or unknown files are skipped. The path pattern cannot enter
`data/quiz/<attempt>/raw/`, so original `raw/performance.mp4` recordings
are outside the tool's scope.

For each non-H.264 review, the tool renames the source to
`tmp-review.mp4` and runs this exact conversion command in that attempt
directory:

```bash
ffmpeg -i tmp-review.mp4 review.mp4
```

No options are added to it - the MP4 defaults, including H.264 video, are
the point. (The window's per-file progress bar reads the frame counter
from the stderr ffmpeg writes anyway, so watching a conversion needs no
extra flag either.)

The temporary original is deleted only after the output is non-empty,
confirmed as H.264, fully decodable by ffmpeg, and has exactly the same
frame count as the source. If ffmpeg is absent, disappears, fails,
produces an empty file, produces a non-H.264 file, or changes the frame
count, the source is not deleted. The tool restores the original
`review.mp4` and preserves any failed output as `failed-review.mp4` (or
the next unused numbered name). An existing `tmp-review.mp4`, output
file, or symbolic link is never overwritten.

Pressing **Stop** (or Ctrl-C) mid-conversion is handled as one more way
for that file to fail: ffmpeg is terminated, the original is put back,
and the discarded output is left as `failed-review.mp4` for you to look
at and delete. Files not reached yet are simply not touched.

Because a conversion is only safe if the result can be proved equivalent,
each file costs three ffmpeg passes - decode the original, encode, decode
the result - which is why the window has a per-file bar as well as an
overall one, and why a full pass over a study's worth of reviews is an
hour-scale job.

### Back up complete participants as ZIP files

```bash
python3 tool_backup_quiz_to_zip.py
```

Previews every archive it is ready to create and asks for confirmation
before creating `data/quiz-zip/`. Only participant directories matching
P01 through P20 and trial T01 through T27 are eligible. A participant is
archived only after all 27 trial numbers are present, preventing an
incomplete `Pxx.zip` from being permanently skipped as an existing backup
on a later run. `TEST-*`, `remote-*`, other unexpected directory names,
symbolic links used in place of trial directories, and `.DS_Store` files
at every depth are excluded.

Each participant is written first as `data/quiz-zip/tmp-Pxx.zip` with
`7z a -tzip`, so the archive format is ZIP rather than 7z. The temporary
archive must pass `7z t` before it is renamed to `Pxx.zip`. Existing
`Pxx.zip` and temporary archives are never overwritten. A failed,
stopped or interrupted temporary archive is preserved for inspection -
and must be removed by hand before that participant can be retried - and
no source directory or source file is ever deleted or modified.

This tool used to refuse to run anywhere but macOS. It no longer does:
the work is one 7z invocation over relative paths, which is as true on
Windows as it is on macOS, and which 7z to use now comes from the lookup
described above rather than from an assumed Homebrew install.

The window's table is the preview, one row per participant, and its
State column is the reason that participant is or is not being archived
("ready", "incomplete" with the missing trials named, "archive exists",
"temporary archive exists") - which between sessions is usually the
question being asked.

### Tests

```bash
python3 test-script/test_maintenance_tools.py
```

Runs against a temporary `data/quiz` built for each test; the project's
own data is never read or written. The conversion and archiving tests
skip themselves if ffmpeg or 7z is not available.

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

Run these from inside `main/`.

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
| `student_quiz.py` / `student_quiz_haptic.py` | Cue-response quiz over a saved song/sequence: LED key cue plus a visual (`student_quiz`) or nail-mounted haptic (`student_quiz_haptic`) finger cue; records the whole session (video+MIDI) under `data/quiz/<attempt>/` for offline scoring. **Preview keyboard profile** draws the active calibration profile over the live camera image before recording - see [Checking the camera against the profile](#checking-the-camera-against-the-profile). |
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

**These wizards configure the machine the experiment runs on.** They
write the top-level `camera`, `midi.port_name` and
`active_keyboard_profile`, and the calibration wizard saves over
`data/keyboard-profile/<name>/`. That is what they are for - and it means
they are the wrong tool for onboarding a tele-training partner, because
each of those keys is what the quiz and the Formal Experiment Session
read, and a recorded session's scoring is only reproducible while its
profile folder stays as it was. For a remote student or teacher, use
**Tele-training Setup Wizard** (section 8) instead: the same calibration
work, saved into its own new profile folder and touching no config key.

### Checking the camera against the profile

The finger judgement is only ever as good as the camera still seeing the
keyboard the way it was calibrated - nudge the tripod and every key id
shifts, silently, and the first sign of it is a trial's finger accuracy.

**Preview keyboard profile** answers that in one click. It draws the
active profile's colored key masks and key ids over the camera image and
opens the result in its own window; nothing is saved. It is on:

- **Quiz - Visual Guidance** and **Quiz - Haptic Guidance** (section 5),
- the main study's **Trial runner** (section 6), which inherits it,
- both remote clients' Settings dialogs (section 8), where it opens the
  camera for one snapshot because those windows hold no camera yet.

In the three quiz windows it annotates the frame already on screen rather
than opening a second capture, since those windows hold the camera for
their whole lifetime. It uses `active_keyboard_profile` - the same
profile that trial is about to be scored with, not a separate choice -
and it is disabled while a quiz is recording. If the camera resolution
and the profile's mask disagree, it says so instead of stretching the
mask, because a stretched mask makes a wrong calibration look aligned.

The shared implementation is `app/gui/profile_preview.py`.

**Two keyboards of the same model report the same MIDI port name.** Every
port picker here lists them as `SE25 MIDI1 #1` and `SE25 MIDI1 #2`,
numbered in the order this machine enumerates them; a name that is
reported only once keeps exactly the name the driver gives it, so a
single-keyboard setup and any older `config.json` are unaffected. The
numbering is not a device identity - it can change when something is
replugged. `python test-script/MIDI.py` listens on every port at once and
prints which label a key press arrived on, which is the quickest way to
tell two identical instruments apart; `python test-script/midi_probe.py
"SE25 MIDI1 #2"` does the same for one port. On macOS, renaming the
instruments in Audio MIDI Setup → MIDI Studio makes the raw names differ
and the suffixes disappear.

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
     pick the MIDI port/timbre/timeout here once, and use **Preview
     keyboard profile** to confirm the camera still matches the
     calibration before the first trial. Every trial records an
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
`../archived/python-prototypes/migrate_to_epoch_timestamps.py` converted the pre-existing
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
     skipping anything already aligned.
   - **Double-click a quiz** for the per-trial detail window
     (`app/gui/quiz_detail_window.py`): all 30 events with verdicts,
     borderline-probability highlighting, a finger confusion matrix and
     distribution stats. **Double-click an event** there to play back
     its keypress ±5 s and correct the detected finger
     (`app/gui/event_review_window.py`) - corrections change
     `actual_finger` only (softmax kept as the audit trail) and are
     flagged in a Manual column. Saving rewrites `results.json` and
     re-derives the headline numbers cached in `meta.json` in the same
     step (`app.quiz.save_quiz_summary`), so an edited quiz is never left
     quoting pre-correction figures and there is no follow-up pass to
     remember. Nothing reads that cache anyway - the table, the export
     and Group Analysis all recompute from the per-event data - it exists
     so a quiz folder is readable on its own.
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
     These files are snapshots and Group Analysis reads them rather than
     `results.json`, so this is the one step to re-run after a round of
     finger corrections or carry-over verdicts.

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
midi = MidiListener()          # picks the first available port if none given;
                               # pass a name from list_input_ports() to choose one

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
  midi.py                      # MidiListener, MidiEvent, save/load_midi_log, and the port
                               #   layer every tool enumerates/opens through:
                               #   list_input_ports / resolve_input_port / MidiInputReader
                               #   (rtmidi by index - mido cannot address two identical keyboards)
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
  tool_binaries.py             # finds ffmpeg / 7z: runtime/bin first, then PATH (see "Quiz data
                               #   maintenance tools"); imports nothing from the rest of app/
  review_compress.py           # GUI-free: scan + convert data/quiz/*/review.mp4 to H.264, with the
                               #   rollback rules that make deleting the original safe. Driven by
                               #   tool_compress_review_videos.py and the section 10 window
  quiz_backup.py               # GUI-free: eligibility + one tested data/quiz-zip/Pxx.zip per
                               #   complete participant. Driven by tool_backup_quiz_to_zip.py and
                               #   the section 10 window
  keyboard/
    template.py                # KeyBox, KeyboardTemplate - the pixel-exact key map
    wizard.py                  # KeyFillWizard - paint-bucket key segmentation
    midi_mapping.py            # MidiMapping - key_id <-> MIDI note
    visualize.py                # color/label helpers for drawing a key_map
    detector.py                 # Canny edge detection used by the calibration wizard
  gui/                          # PyQt windows/pages for the scripts above (incl. the sequence
                                #   generator window, metrics viewer, validation dialog, cue window,
                                #   profile_preview.py - the shared "camera vs calibration" check
                                #   used by both quizzes, the trial runner and the remote clients;
                                #   the pilot-study windows: participant schedule, formal-session
                                #   controller, trial runner, participant-facing experiment cue
                                #   screen; and the analysis windows: quiz_analysis_window,
                                #   quiz_detail_window, event_review_window, video_sync_window,
                                #   participant_analysis_window; and the section 10 maintenance
                                #   windows: maintenance_window.py - the shared shell (worker
                                #   thread, progress bars, log, tool status) - with
                                #   review_compress_window and quiz_backup_window over it)
```

`../archived/python-prototypes/` holds earlier, now-superseded prototypes of this same detector
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
| `haptic_config.py` | The project's single source of truth for haptic actuator defaults: loads/validates/saves `config.json`'s `haptic` block, and hands every tool the actuator in use and its default frequency/amp (see "Haptic actuator configuration"). |

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
| `midi_probe.py` | Ground-truth calibration probe - prints the MIDI note number for each physical key as you press it, left to right. Takes an optional port name, so it can also check one specific instrument out of several. |
| `MIDI.py` | Minimal listener that prints every incoming note with the port label it arrived on, listening on **every** input at once - the quickest way to tell two identically named keyboards apart. |
| `plot_acc_from_ACC_stream_autostart.py` | Live-plots `ACC,x,y,z` accelerometer lines streamed over serial, sending the start/stop stream commands automatically. |
| `test_participant_analysis.py` | Unit tests for `app/participant_analysis.py` (the GUI-free cross-trial computation layer). |
| `test_acceleration_metrics.py` | Unit tests for `validation_experiments/acceleration_metrics.py` (both vibration-intensity metrics, unit conversion, the raw-sample round trip) and for the metric switching / old-CSV compatibility of the accelerometer experiments. |
| `test_haptic_config.py` | Unit tests for `common/haptic_config.py` and everything that reads it: old/partial `config.json` compatibility, range validation, atomic non-destructive saving, the Initial Setup window, the validation windows' defaults and prose, "manual value wins over the config", historical-run rendering, and the haptic quiz cue. |
| `test_validation_reports.py` | Unit tests for the validation experiments' text statistics (`validation_experiments/report.py` + each experiment's `summary_report()`) and for the saved-run picker in the validation windows. |
| `test_remote_setup_wizard.py` | Unit tests for the Tele-training Setup Wizard: opening it performs no camera scan/open; scan results require an explicit selection; calibration has Initial Setup's five pages and crop/full-frame coordinate handling; MIDI mapping loads the Middle C reference image and displays the live camera with next/mapped/unmapped key overlays; and neither flow can write config or a non-Tele-training profile. Cameras/MIDI are faked. |
| `test_chord_cue.py` | Unit tests for cueing a chord (remote guidance only): the haptic bitmask, the dot view's multi-highlight, the hand view's photo cycling, the LED's single flush, and the two guarantees that scope it - scoring stays single-note, and the single-finger path the local quiz and the main user study use is untouched. |
| `test_profile_preview.py` | Unit tests for `app/gui/profile_preview.py` and the **Preview keyboard profile** button the quiz windows inherit: the mask is drawn without touching the caller's frame, a resolution mismatch is refused rather than resized, the frame already on screen is used instead of a second capture, and every failure is reported rather than raised. No camera or profile on disk is needed. |
| `test_midi_ports.py` | Unit tests for `app/midi.py`'s port identity: two identically named keyboards both stay visible and are opened by index, older bare port names still resolve, and `MidiListener`/`RawMidiRecorder` end up on the port that was asked for. rtmidi is faked, so no keyboard is needed. |
| `test_quiz_summary_cache.py` | Unit tests for the `meta.json` summary cache (`app.quiz.apply_summary` / `save_quiz_summary`): a corrected finger and a confirmed carry-over both leave the cached headline numbers equal to what the analysis table and the participant export recompute live, `results.json` is never touched, and the identity fields (`guidance_type`, `note_count`, `analyzed`) survive a re-cache. Quiz folders are temporary; no video, MIDI or MediaPipe pass is involved. |
| `test_music_recording.py` | Unit tests for the saved-song lead-in - the gap between "the recording started" and "the playing started", which is the one place an absolute `time.time()` stamp and an elapsed duration can be mixed up silently - and for the two Song Recording Wizards writing a `meta.json` whose `duration_s` is the length of the piece rather than of the capture. Songs go to a temporary directory; no camera, MIDI device or MediaPipe pass is involved. |

`latency_results/` holds the plots/summary generated by the latency scripts;
`video_demo/` holds recorded demo videos of the latency tests.

## Tele-training (launcher section 8)

Remote guidance: a teacher on one machine, a student on another, and a
relay in between. Implements the two modes the report describes
(method.tex, "Tele-training Guidance Modes") - **synchronous live
cueing**, where the teacher plays a key and the student's key LED and
finger cue fire; and the **asynchronous teacher-recorded sequence**,
where a fingering plan recorded once is triggered remotely and scheduled
locally on the student's machine.

Three independent processes:

| Process | Entry point | Devices it owns |
|---|---|---|
| Student Client | `student_remote_guidance.py` | camera 1, MIDI keyboard 1, key LED strip, nail actuators, local key audio while ready |
| Teacher Client | `teacher_remote_guidance.py` | camera 2, MIDI keyboard 2, local key audio while MIDI is connected |
| Relay Server | `python -m server` | none - see `server/README.md` |

**Every device call happens on the client that owns it.** The server
imports no camera, MIDI, MediaPipe, LED or haptic module; it routes small
JSON events and stores them. No video is ever sent over the WebSocket or
into the relay's database - "the teacher sees the student's performance
live" means events and metrics, not a video feed.

A fourth entry, **Tele-training Setup Wizard**, is a normal window rather
than a process: it builds a keyboard profile (camera → calibration → MIDI
mapping) and writes no config.json key at all. Run it once per
new participant - see [Setting up a new remote student or
teacher](#setting-up-a-new-remote-student-or-teacher).

### Getting started on one machine

```bash
python -m server --init
```

The relay listens on **port 18765** by default, and both clients default
to `http://127.0.0.1:18765`. The three defaults (client URL, launcher's
local-server port, relay bind port) live in separate files - `server/`
deliberately imports nothing from the platform - so a test keeps them in
step.

Then start the three endpoints from the launcher's **8. Tele-training**
section - *Relay Server* first (it refuses to open a second one if a
relay is already answering), then *Teacher Client* and *Student Client*.
Each opens as its own process, so all three run side by side. That
section has five entries in this order: Relay Server, Teacher Client,
Student Client, Tele-training Setup Wizard and Network Latency Benchmark.
Every one of the three running programs carries its own settings.

The relay control panel deliberately opens as a compact 440 × 360 window.
All server details, controls, connected rooms/clients and log output are
still present; scroll the dashboard to reach the lower sections when the
window is small.

Or do it by hand in three terminals:

```bash
python -m server --gui
```

```bash
python teacher_remote_guidance.py
```

```bash
python student_remote_guidance.py
```

Both clients walk through three steps, one screen at a time - **sign in**,
then **choose a room**, then the session itself:

1. *Teacher*: sign in (or create an account), type a room name, read off
   the join code the relay gives back.
2. *Student*: sign in, type that code.
3. The teacher presses **Get ready**, the student presses
   **Ready for guidance**, and any key the teacher plays cues the student.
   The order of those first two presses does not matter. Each person's
   own physical key presses also play locally in their client.

The client windows use the launcher's dark card-style Qt CSS but have
different identities: Teacher is warm amber and Student is cool cyan. A
compact command bar puts role, stage, link state and Settings on one line;
the detailed status is one slim line below it, so the header no longer
takes several mostly-empty rows. Long status text is available as a
tooltip instead of forcing the window wider. Checked boxes in either
role now contain a real, high-contrast tick instead of relying on a fill
colour alone. Settings' camera resolution/FPS fields still accept typed
numbers and also expose large role-coloured up/down buttons.

The Session stage is divided into workspaces instead of showing the
camera, every control and the results table at the same time. Teacher has
**Live studio**, **Recording library** and **Student results** tabs.
Student has **Practice studio** and **Session results** tabs. On both live
pages the setup/control card and a bounded 560 × 360 camera preview sit
side by side; the preview remains complete without becoming the whole
interface. Finishing a session opens the relevant results workspace.

The **Guidance** choice (`Visual`, `Haptic` or `Both`) appears only on
the student Session page. The teacher does not choose how cues are
rendered; their live row contains **Connect MIDI**, **Chord detection**,
**Timbre**, **Record this lesson to data/music** and the session
controls. Both clients have their own Timbre
choice (`Piano`, `Electric Piano`, `Sine`, `Mute`), copied from Student
Quiz; changing it affects that client's new notes immediately. When the
student becomes ready, their client reports its guidance choice to the
teacher and the relay stores that student-owned value on the session.

**The join code is the only room identifier anyone types.** Step 2 is one
field with no mode to pick, the same on both clients: a code joins (for
the teacher, reopens), and anything else is the name of a new room - so
the next launch, with the last code already filled in, is a single click
back into the same room. The room's uuid stays in the relay's database
and in `config.json`; it is never shown.

The relay address and the two demo accounts (`demoteacher` /
`demostudent`) come pre-filled in the sign-in form, so a run on one
machine is three clicks rather than six fields of typing. They are a
constant in `remote_guidance/gui_common.py` (`DEMO_ACCOUNTS`), not
config - `config.json` still holds no password. Overtype the username
and the pre-filled password clears itself, so a demo secret is never
offered to a real account.

**No camera, MIDI port or audio output is opened until an explicit
hardware action is pressed.** Signing in, choosing a room and sitting on
the session page touch no hardware; the devices are claimed by **Get
ready** / **Ready for guidance** (or the teacher's manual
**Connect MIDI**) and handed straight back when the session/MIDI check
stops, on **Change room** and on close. `Mute` does not open audio at all.
The student's LED strip is the exception - it has its own Connect button,
because the sync flash is worth checking before a session rather than
during one.

Running Teacher and Student on the same computer does not open extra MIDI
connections for sound. Teacher audio consumes note-on/off messages from
the detector's existing reader; student audio consumes the recorder's
existing messages. Each process has at most one normal shared-mode output
stream, which the OS mixes, and closes it as soon as that role stops. If
audio output cannot be opened, the client warns and continues without
sound rather than blocking the session.

The two camera/MediaPipe pipelines also run outside the GUI threads in a
Tele-training-only latest-frame worker. A slow vision pass can reduce the
preview frame rate, but it cannot hold a ready MIDI/network event or cue
behind it, and old preview frames are dropped instead of queued. Each
worker is capped at its configured camera FPS to avoid wasting CPU. On the
student, the screen cue is painted before LED/haptic serial writes, so a
slow serial device cannot postpone the visible instruction; reaction time
still begins after the last enabled channel completes. This ordering is
especially important when both clients share one busy computer.

If a run still looks slow, the teacher event table separates the next
places to look: a large **Net RTT** means the receive acknowledgement was
slow, **Queue** means an earlier cue was still active, and **Dispatch**
means the student's enabled output channels were slow. The relay itself
has no batching interval: it forwards first and persists asynchronously.

The teacher's **Recording library** workspace contains the existing
song/sequence upload and remote playback controls plus **Record a new
song...**, which opens Tele-training's private Teacher Recording Wizard.
It copies section 3's three-page capture/fingering/save flow and writes
the same `data/music/<song>/` format, but does not import or subclass
section 3's UI. Its camera, MIDI keyboard and keyboard profile are fixed,
read-only values from Teacher Settings, and its three pages use the same
warm amber Teacher theme. Opening that wizard is the other explicit
action that claims teacher hardware; closing it releases camera, MIDI,
LED, audio and video output and refreshes the song picker. A recorded run
creates its own `recording` session, so it is not necessary to start Live
studio first.
If the teacher triggers before the student presses **Ready for
guidance**, the student holds that one trigger and starts the download as
soon as it becomes ready.

**A live lesson is recorded on both machines, under one name.** The
student's side is unchanged: results, video and MIDI go to
`data/quiz/<session name>/` exactly like a local quiz. The teacher's own
camera and keyboard - already open, already producing what the Song
Recording Wizard saves - now also write the lesson to
`data/music/remote-<epoch>/` in that wizard's format: video, raw MIDI,
`sync.json`, `score.mid`, `fingering.json` and `meta.json`, with the same
offline finger pass at the end. The lesson therefore appears in the
Recording library afterwards and can be uploaded and replayed like any
other song. Untick **Record this lesson to data/music** before starting to
skip it (the default comes from `remote_guidance.teacher.record_video`).

The `remote-<epoch>` name is generated by the teacher and travels with the
session, so `data/music/remote-1770000000/` and
`data/quiz/remote-1770000000/` are the two halves of the same lesson. The
student uses it whenever its **Session name** box is empty; typing a name
there keeps that name instead, and the client says on its status line that
the two folders will differ. Two things the teacher's copy is not: an
independent timing measurement (its note stamps come from the 33 ms
guidance timer, not a separate MIDI thread), and a copy of the annotated
preview - it records the camera image without the key overlay, because the
offline finger pass has to be able to track hands in it.

Account creation, rooms, HTTPS/wss, deployment, SQLite backup and the
full REST/WebSocket reference are in **[server/README.md](server/README.md)**.

The module's design notes - file map, the invariants the implementation
rests on, the traps found while building it, and what is still missing or
unverified - are in **[REMOTE_GUIDANCE.md](REMOTE_GUIDANCE.md)**. Read
that before changing anything here.

### Setting up a new remote student or teacher

**Tele-training Setup Wizard** (section 8) builds a **keyboard profile**
for this machine: the camera, the calibration of that camera's view of the
keyboard, and that keyboard's key→note mapping. Use it - not section 1's
Initial Setup - whenever someone new joins a tele-training session.

Three steps, and the last two can be re-entered from the step bar so one
part can be redone (remapping MIDI without recalibrating, most of all):

1. **Camera** - opening the Tele-training wizard does not scan or open a
   camera. Click **Scan for cameras**; the scan runs in the background;
   then explicitly choose one of the detected indices to start its live
   preview and set its resolution/flips.
2. **Calibration** - the same five-page flow as Initial Setup: name the
   new profile, capture a photo, click the keyboard's two corners, tune
   edge detection on that cropped keyboard, then mark white/black keys
   with the same paint-bucket controls. **Finish** creates the profile.
3. **MIDI mapping** - the same two-page visual flow as Initial Setup.
   First connect the port and use the displayed Middle C reference image
   plus live note readout to verify note 60/C4. Then pick the profile and
   use the live camera image: the next key is highlighted yellow, mapped
   keys are green and unreached keys grey. Press each highlighted
   physical key in turn, then save the mapping.

The camera comes first because the calibration is a pixel mask of *that
camera's* frame: step 2 captures through step 1's settings rather than
asking again, so the two cannot disagree.

**Its only output is the profile folder.** It writes no `config.json` key
at all - not the top-level `camera`, `midi.port_name` or
`active_keyboard_profile`, and not the `remote_guidance` block either - so
running it cannot change what any tool on this machine does. To use the
new profile, select it in the Student or Teacher Client's own **Settings**,
which is where choosing devices already lives.

There is also **no student/teacher choice** in it. A calibration describes
a camera looking at a keyboard, not a person; student and teacher normally
need one each only because they normally sit at different setups, and if
they share a setup they can share the profile.

Two more things it will not do:

- it saves only into profile folders **it created**, each marked with a
  `remote_setup.json` file, and refuses to write into any folder without
  that marker - so section 1's profiles, and any profile a recorded
  session was scored against, are unreachable from it;
- it never overwrites an existing folder at all, not even one of its own:
  a new calibration always means a new name.

The wizard is a normal launcher window, so the usual one-tool-at-a-time
rule applies - it will not run beside a client and fight it for the
camera. The Initial Setup calibration and MIDI mapping flows were copied
into `remote_guidance/calibration_wizard.py` and
`remote_guidance/midi_mapping_wizard.py` rather than changing section 1:
their images and interactions stay the same, while their save actions go
through the Tele-training-only store. Those write rules live in
`remote_guidance/setup_store.py`, kept separate from the UI so they can
be (and are) tested without hardware.

### Configuration

Everything lives in `config.json` under a new `remote_guidance` key.

**Each program has one Settings button and shows only its own settings.**
The student client's opens the student's camera, MIDI port and
calibration profile; the teacher client's opens the teacher's; the relay
server's opens its bind address, port, registration and token lifetimes.
Nothing in the launcher edits any of them, and neither client can see the
other's devices. The block in full:

Client Settings keeps the role's main-window identity: warm amber for
Teacher, cool cyan for Student. Its compact header is followed by a
two-column Camera / Keyboard+MIDI workspace, a short device-ownership
card, a one-line status strip and the action row. The MIDI key readout is
an accented monitor card. Long messages use a tooltip rather than
widening the dialog, so the full form remains visible at 1040 × 650.

The student and teacher Settings dialogs also have **Capture camera +
profile preview**. It takes one photograph using the camera values
currently in the form, draws the selected calibration profile's colored
key masks and key-id labels over it, then opens the result in a separate
preview window. This does not save the settings or the photograph. If
the actual camera frame and profile map have different resolutions, the
preview explains the mismatch instead of stretching the mask.

The MIDI port is selected only here. The two Session pages do not repeat
the port dropdown or its Refresh button: the student opens its saved port
when **Ready for guidance** is pressed, while the teacher's **Connect
MIDI** action opens the teacher port saved in Settings. On the teacher
page, **Connect MIDI** and **Chord detection** share the first row of a
compact Live controls card, with Timbre and session actions below. The
student Session page has
its own Timbre selector. These selectors change local sound only; that
teacher row deliberately has no student-guidance selector: `Visual` /
`Haptic` / `Both` is chosen on the student Session page.

Beside the MIDI picker, **Connect and test** temporarily opens the currently
selected input. Press any key on the physical keyboard and the Settings
dialog shows its note number/name and the exact port label it came from;
this is the quick way to distinguish `#1` from `#2` before saving. Changing
the selection or refreshing the list releases the old test connection,
and Save, Cancel and closing the Settings window all release it as well,
so the client can claim the selected keyboard later when a session starts.

The MIDI picker is a real, non-editable dropdown. **Refresh** lists the
currently detected inputs. If none are connected, the role's saved port
remains as the sole option rather than being cleared; if there is no saved
port either, the empty control says `No MIDI inputs detected` and its test
button is disabled. The colored down-arrow is deliberately explicit so
the dropdown cannot be mistaken for a text box.

**Give the two roles different MIDI ports.** Teacher and student normally
use the same model of keyboard, which reports the same port name twice,
so both dialogs list `... #1` and `... #2` and warn that the numbering
follows this machine's enumeration order. Setting both roles to the same
name is not an error anyone gets told about at runtime - it just points
both clients at one keyboard, and the teacher starts receiving the
student's notes.

```json
"remote_guidance": {
  "schema_version": 1,
  "network": {"server_url": "http://127.0.0.1:18765", "verify_tls": true},
  "student": {
    "camera": {"index": 0, "width": 1280, "height": 720, "fps": 30,
               "flip_vertical": true, "flip_horizontal": true},
    "midi": {"port_name": "SE25 MIDI1 #1"},
    "keyboard_profile": "white-city-lab-20260717",
    "led": {"port": "/dev/tty.usbmodem1101"},
    "haptic": {"port": "/dev/tty.usbmodem2201"},
    "default_guidance_mode": "both",
    "record_video": true
  },
  "teacher": {
    "camera": {"index": 1},
    "midi": {"port_name": "SE25 MIDI1 #2"},
    "keyboard_profile": "teacher-desk-20260801"
  },
  "local_server": {"host": "127.0.0.1", "port": 18765, "use_gui": true}
}
```

Three things this block deliberately does **not** do:

- It does not replace or reinterpret the top-level `camera`, `midi` and
  `active_keyboard_profile` that every ordinary tool reads. Student and
  teacher each carry their own, and saving remote settings rewrites only
  the `remote_guidance` key - the local quiz's camera never moves.
- It does not store credentials. Passwords, tokens and TLS private keys
  are never written to `config.json`; you sign in from the client. (The
  demo password the form starts with is a constant in the client's own
  source, and `save()` has no way to write it here.)
- It does not assume a config file has this block at all. A
  `config.json` written before this module existed loads with defaults.

**The two student serial ports are not asked for anywhere.** The key LED
strip and the nail actuators are found and connected by the student
client when a session starts, and a strip that is not found is a warning
rather than a refusal - the finger cue and the scoring still work without
the key backlight.

The reason they used to be explicit still stands, though: the strip and
the vibration rig are two separate boards, and
`common.serial_utils.auto_detect_port` scores them almost identically -
with both plugged in it can pick the wrong one, and "wrong" here means
sending motor commands to the LED controller. If that ever happens, pin
`student.led.port` and `student.haptic.port` by hand in `config.json`.
Setting both to the same port is still refused with a clear error.

### Guidance modes

The student picks one before the session starts:

| Mode | Key LED | Finger cue |
|---|---|---|
| `visual` | yes | on-screen cue window (`app/gui/cue_window.py`) |
| `haptic` | yes | nail actuator (`app/haptic_cue.py`) |
| `both` | yes | both together |

**Chords are cued in full.** With **Chord detection** on, the teacher's
matcher returns every finger of a simultaneous press and the student cues
all of them: every key lit, every motor buzzing (the rig's command is a
bitmask, so it is one write), and every dot on the cue window. The
hand-photo style is the one channel that cannot show a set - there is one
photo per finger and no combined assets - so it cycles through the
chord's photos instead, ~5 per second, with the full chord named in the
text line. It stays **one cue event with one cue-ready moment**, so a
reaction time still has a single origin.

Scoring deliberately does not follow: a chord is judged on its primary
note, so pressing a different note of the same chord counts as wrong.
Widening the cue is free; widening the verdict would mean a second
definition of "correct" in a pipeline shared with the local quiz and an
already-run study. See [REMOTE_GUIDANCE.md](REMOTE_GUIDANCE.md) §4.11.

**None of this reaches the local quiz or the main user study.** They call
the single-finger cue API, which is unchanged - in particular a one-finger
cue never starts the photo-cycling timer, so condition B's stimulus is
exactly what it always was. There are tests for that.

The **key LED is present in all three** - the modes name only how the
*finger* is conveyed. All three are driven through one
`CompositeCueOutput` (`remote_guidance/cue_outputs.py`), which is an
ordinary `app.quiz.CueOutput`: the existing screen and haptic cue classes
are composed, not reimplemented, and `QuizWindow` is untouched.

### What is reused

Nothing about detection, scoring or storage is written twice:

| Concern | Module |
|---|---|
| Teacher's key→finger detection | `app.camera`, `app.hand_tracking`, `app.midi`, `app.finger_matching` (incl. `match_notes_to_fingers` for chords) |
| Student's cue channels | `app.gui.cue_window.ScreenCueOutput`, `app.haptic_cue.HapticCueOutput`, `profile_led_mapper` + `note_led_map` |
| Recording + sync mark | `app.music_recording` (`RawMidiRecorder`, `SyncInfo`) |
| Correctness and summary | `app.finger_matching.is_finger_correct`, `app.quiz.summarize` |
| Storage format | `app.quiz.QuizResult` / `save_quiz_results` / `QuizMeta` |
| Offline finger pass | `app.offline.analyze_recording` via `app.gui.analyze_worker.AnalyzeWorker` |
| Post-session review | `app.gui.quiz_analysis_window.QuizAnalysisWindow` |

A remote session therefore lands in `data/quiz/<session name>/` in the
standard layout (`raw/performance.mp4`, `midi_raw.json`, `notes.json`,
`sync.json`, plus `results.json` and `meta.json`), and every existing
analysis tool reads it unchanged. `QuizMeta.guidance_type` records it as
`remote-visual` / `remote-haptic` / `remote-both`.

The teacher's pre-recorded uploads come from the same libraries the local
tools use - `data/music/<song>/` and `data/sequence/<name>/`, read through
`app.music_recording.load_playback_events`. Only the note/finger event
list is uploaded; the video stays on the teacher's machine.

Running teacher and student on the same computer does not overwrite that
source folder. Every Upload creates a new relay database recording with
a new id, and the student downloads its event JSON into memory rather
than writing it back under `data/music/` or `data/sequence/`. Student
performance output is separate under `data/quiz/<session name>/`; choose
a new session name if an existing quiz result with that name must be
preserved.

### Provisional vs final results

The student scores every response twice:

1. **Provisional**, live, from the current hand landmarks, sent to the
   teacher as each event completes.
2. **Final**, after the session, when `analyze_recording` re-runs finger
   matching over the recorded video with the LED sync anchor - the same
   pass a local quiz does.

Both stages are labelled `stage: "provisional" | "final"` on the wire and
in the relay's database, and the teacher's table shows which it is
looking at. The server stores them and never recomputes either: the
session summary endpoint returns the student's own numbers under
`student_reported_summary` with `summary_source: "student"`.

### Timing: what is measured on which clock

Each event records:

```
teacher_send_wall_ns              server_receive_wall_ns
student_receive_wall_ns           queue_enter_monotonic_ns
cue_dispatch_start_monotonic_ns   led_command_complete_monotonic_ns
visual_painted_monotonic_ns       haptic_command_complete_monotonic_ns
cue_ready_monotonic_ns            cue_ready_wall_ns
student_response_monotonic_ns     student_response_wall_ns
```

`cue_ready` is the **last** enabled channel to become ready. In `both`
mode that is
`max(LED command complete, visual paint complete, haptic serial flush complete)`;
with one finger channel it still includes the LED, because the LED is in
every mode.

Student reaction time is always, and only:

```
reaction_time_ns = student_response_monotonic_ns - cue_ready_monotonic_ns
```

Both terms from the student's own monotonic clock. **Never** from
`teacher_send`, `server_receive` or `student_receive`: those would fold
network delay and queueing into a number describing a person, so a slow
link would read as a slow learner. They are all still recorded, in their
own fields, and the time an event spent waiting behind another is
recorded separately again as `queue_wait_ns`.

For `QuizResult` compatibility, wall-clock `cue_onset_time` /
`keypress_time` are saved as well - but the accurate figure is the
monotonic one, and that is what `timing_error_s` carries.

These are **software dispatch and render timings**: the moment a serial
write flushed or a frame finished painting. They are not the moment an
LED emitted light or a motor began to move. Measuring that needs a
photodiode and an accelerometer on one acquisition clock (the report's
"System Transmission Performance Benchmarking"); the CSV keeps columns
free for it and nothing here claims it.

A cue that arrives while another is still live goes into a bounded queue.
It is never silently dropped and never overwrites the live one; if the
queue is full the event is refused explicitly and the teacher is told the
student is behind.

### Latency benchmark

```bash
python remote_latency_benchmark.py --server http://127.0.0.1:18765 \
  --username teacher1 --student-username student1
```

or **8. Tele-training → Network Latency Benchmark** for the GUI wrapper.
The default is self-contained: it logs in as both roles, creates a
temporary benchmark room, connects a Teacher WebSocket and a built-in
simulated Student WebSocket, runs the probes, then closes the room without
deleting its database record. You therefore start only **Relay Server +
Network Latency Benchmark**; neither full client is needed. Both accounts
must already exist on the relay, and the GUI pre-fills the demo pair.

Built-in mode measures the real network/relay route and client WebSocket
code, but deliberately owns no Student Qt UI, LED or haptic device. Enable
**Use an external Student Client** and provide its room id only when the
real Student software/hardware dispatch path is the object of the test.
Defaults follow the report's benchmark: 1000 probes at 500 ms intervals
over one persistent WebSocket per endpoint, warm-up samples recorded
separately and excluded from the statistics, unique sequence number and
message id per probe. Output goes to
`data/remote_guidance/latency/<run id>/` as `samples.csv`, `summary.json`
and `latency.png`, with n, lost, loss rate, min, mean, sd, median, p95,
p99 and max for each metric.

- **Transport RTT is the primary metric** - both stamps come from one
  monotonic clock on the teacher's machine, so no clock synchronisation
  is needed.
- In built-in mode the two simulated endpoints share one host clock, so
  teacher-send → built-in-student-receive is a directly measured one-way
  duration through the relay.
- With an external Student, **one-way latency** is reported as a measurement only when
  `--clocks-synced` asserts both hosts are NTP-synchronised *and* the
  estimated offset uncertainty is recorded with it. Otherwise the output
  shows `RTT/2`, labelled a symmetry-based estimate. Two independent
  computers' wall clocks differ by an unknown offset that is often larger
  than the delay being measured, so subtracting one machine's timestamp
  from another's is not a latency - it can even come out negative. Check
  both hosts (`time.is`, `chronyc tracking`) before a run; the built-in
  four-timestamp offset estimator quantifies the assumption but is not
  NTP and is not offered as a substitute.
- Monotonic timestamps are never subtracted across machines. They travel
  as opaque values and are only used where they were produced.

The relay also answers each probe itself (`latency.ack`), so a slow run
can be attributed to the teacher→server hop or the server→student one.

### Tests

```bash
python -m pytest test-script/test_remote_guidance_config.py test-script/test_remote_guidance_server.py test-script/test_remote_guidance_e2e.py test-script/test_midi_ports.py
```

Covers old-config compatibility, remote saves not touching shared
settings, student/teacher config isolation, the LED/haptic port clash,
two identically named keyboards staying separately addressable,
the composite cue in all three modes, JWT issue/expiry/refresh/revoke,
room ownership and membership authorization, cross-room isolation,
duplicate-message idempotency, sequence ordering, survival of a relay
restart, reaction time originating at the local cue-ready moment, RTT and
one-way never being conflated, percentile/loss statistics, a full
server + fake teacher + fake student integration run, and the launcher's
process actions. No hardware or real network is involved.

---

## Haptic actuator configuration

Which vibration actuator the rig drives — and how hard — is **one setting
for the whole project**, stored in `config.json` and edited in the
launcher's **1. Initial Setup → Haptic Actuator Defaults**. No script,
window or experiment carries its own copy of "224 Hz / amp 64".

### Schema

```json
"haptic": {
  "using": "lra",
  "lra": { "default_frequency": 224, "default_amp": 64 },
  "erm": { "default_frequency": 1000, "default_amp": 80 }
}
```

| Field | Meaning | Default |
|---|---|---|
| `haptic.using` | Which actuator the platform drives by default — `"lra"` or `"erm"` (case-insensitive on load, stored lower case). | `"lra"` |
| `haptic.lra.default_frequency` | LRA drive frequency in Hz. | `224` |
| `haptic.lra.default_amp` | LRA drive amp (firmware PWM duty byte). | `64` |
| `haptic.erm.default_frequency` | ERM PWM **carrier** frequency in Hz. | `1000` |
| `haptic.erm.default_amp` | ERM drive amp. | `80` |

**The two frequencies do not mean the same thing.** An LRA's frequency is
a real mechanical drive frequency: the actuator only vibrates properly at
(or very near) its resonance, which is why
`validation_experiments/lra_resonance_intensity_calibration/` exists to
measure it. An ERM's frequency is the **PWM carrier** that chops its
drive rail — it is *not* the rotor's mechanical vibration frequency,
which follows the motor's speed and therefore the amp. The carrier only
has to be fast enough that the chopped drive behaves like smooth DC; too
low and the rotor cannot overcome stiction and never starts at all.

### Valid ranges

Enforced in one place (`common/haptic_config.py`) and used unchanged by
the Initial Setup spin boxes, so a control can never offer a value the
config would reject.

| Setting | Range | Where it comes from |
|---|---|---|
| amp (both actuators) | 0–255 | The firmware's PWM duty byte: `handlePulse()` rejects anything outside 0–255 (`teensy_driver/motor_driver.cpp`). |
| LRA frequency | 50–1000 Hz | Firmware `FREQ_MIN` = 50 Hz; the upper bound is the project's own LRA band (the Haptic Motor Bench's high-precision slider), and the resonance sweep only covers 100–350 Hz. |
| ERM frequency | 50–20000 Hz | Firmware `FREQ_MIN`/`FREQ_MAX` — the `F` command silently drops anything outside them. The project drives ERMs at 1–5 kHz. |

A frequency that is legal but unusual for that actuator (an LRA far off
resonance, an ERM carrier below ~1 kHz) is still accepted — this is a
research rig — but the Initial Setup window says so in an advisory note.

### What changing it affects

* the haptic quiz cue (`app/haptic_cue.py`) — amp **and** the PWM
  frequency it tunes each finger port to on connect;
* Haptic Vibrator Test and Haptic Motor Bench (their opening
  frequency/amp and "restore defaults");
* **Test Buzz** in every validation window, and the frequency pushed to
  the port before the pulse;
* Motor → ACC Delay: the actuator it opens on, and its motor port, PWM
  frequency and drive amp;
* Actuator Spectrogram: the type it opens on and its motor port;
* LRA Amplitude Sweep: the PWM frequency the amps are measured at;
* Adhesion Vibration Comparison: the frequency **and** amp it drives —
  always read from `haptic.lra` (even when `using` is `erm`) and shown
  read-only, so all three adhesives get an identical drive;
* the descriptions, tooltips and status lines of section 9 — they quote
  the configured numbers, not literals;
* the Wiring Guide's pin table (which actuator is in use, on which port).

### Priority: config defaults vs. what you type

The config supplies **initial values**; the operator always wins.

* A window reads the config when it **opens** and fills its controls.
* Anything typed afterwards is used for that run — the config is **never**
  re-applied at Start.
* Sweeps are unaffected in principle: the LRA resonance sweep still
  covers its whole 100–350 Hz range (finding the resonance is its
  purpose), the amplitude sweep still steps its own amp values, and the
  spectrogram still drives the ranges shown in its controls.
* Saved runs are immutable: a run's `meta.json` records what was actually
  sent to the hardware, and re-rendering an old chart uses **that**
  meta's parameters, never today's config. Runs saved before a field
  existed fall back to fixed historical constants, not to the config.
* New runs additionally record a `haptic_config` block: the config
  snapshot at run time, plus whether the amp / frequency / motor port
  used was the configured default or a manual override.

### Editing it

Initial Setup → **Haptic Actuator Defaults**: a radio pair for the
actuator in use plus a frequency and amp box for each actuator. Every
change is written to `config.json` **as you type** — no Save button and
no restart. Windows opened afterwards read the new values; validation
windows already open refresh their descriptive text (their controls are
left as the user set them).

Saving is read-modify-write and atomic (temp file + `os.replace`), so
every other key in `config.json` is preserved byte for byte and an
interrupted write can never leave unparseable JSON behind.

### Old config files

* No `haptic` key at all → the built-in defaults above are used; nothing
  is written until something saves.
* Some fields missing → only the missing ones fall back; the present ones
  are kept exactly.
* A malformed or out-of-range value → clamped into range (or, if it is
  not a number at all, replaced by the built-in default), with the
  correction reported in the log and shown in the Initial Setup window.
  Nothing out of range is ever sent to the hardware, and no correction is
  silent.

### What is deliberately *not* in this config

* **Motor port.** Which port an actuator is wired to (LRA → 11, ERM → 10)
  is a fact about the board, not a preference. The mapping is centralised
  in `common/haptic_config.py` (`ACTUATOR_MOTOR_PORTS`) so every window
  and experiment agrees on it, it is displayed in the Wiring Guide and in
  the Initial Setup summary, and every experiment window still has its
  own motor-port control that overrides it.
* **The firmware's boot PWM frequency** (224 Hz,
  `motor_driver.cpp DEFAULT_PWM_FREQ`). Experiments restore it on the
  port when they finish, so it must keep matching the firmware even if
  the configured LRA default changes.

---

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
