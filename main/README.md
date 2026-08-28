# The platform

The Python side of the project: camera + MIDI finger-use detection, the
per-key LED backlight, the vibrotactile cue path, and the tools that run and
analyse every study built on them. The repository front door and its
documentation map are one level up, in [`../README.md`](../README.md).

Everything is reachable from one hub window, which groups the tools into the
twelve numbered sections used throughout this file:

```bash
python launcher.py
```

Every tool also runs standalone from its own entry-point script.

## Where to read what

This file documents the platform and the tools that have no document of their
own. Anything with a dedicated write-up is **linked, not repeated** — if a
subject appears below in more than a paragraph or two, this is the only place
it is written up.

| Subject | Document |
|---|---|
| Repository front door, install, structure | [`../README.md`](../README.md) |
| Firmware and the serial protocol | [`../teensy_driver/README.md`](../teensy_driver/README.md) |
| **Main User Study** stimulus algorithm | [`SEQUENCE_GENERATOR_ALGORITHM.md`](SEQUENCE_GENERATOR_ALGORITHM.md) |
| **Tele-training** (section 8) | [`REMOTE_GUIDANCE.md`](REMOTE_GUIDANCE.md), and [`server/README.md`](server/README.md) for the relay |
| **Validation experiments** (section 10) | [`validation_experiments/README.md`](validation_experiments/README.md), plus one README per experiment |
| **Rhythm experiment** (section 11) | [`RHYTHM_EXPERIMENT.md`](RHYTHM_EXPERIMENT.md), and [`melody_generator/README.md`](melody_generator/README.md) for the generator |
| Study protocols and rationale | `../../final_report_2026/method/method.tex` |

What is written up **here** and nowhere else: the directory layout, the
FingerAccuracy pipeline and calibration profiles (sections 1–3), the Main
User Study runner and the analysis windows (sections 6–7), the quiz data
maintenance tools (section 9), the haptic actuator configuration, and the
LED/audio helper modules.

## Layout

```
app/                            UI package: camera + MIDI finger-accuracy detection, quiz, generator
launcher.py                     entry point - hub window for every tool below, grouped into the same
                                 numbered sections used throughout this README (1 Initial Setup ...
                                 12 Demo & About). Section 8 Tele-training is the one
                                 section whose buttons start SEPARATE processes rather than a
                                 sub-window, because a student, a teacher and a relay have to run at
                                 the same time - see "Tele-training" below.
                                 Section 9 Tools is data housekeeping, not part of running or
                                 analysing a session - see "Quiz data maintenance tools" below.
                                 Section 11 Rhythm Experiment is a separate study, deliberately
                                 isolated so it cannot affect any of the others - see "Rhythm
                                 experiment" below.
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
RHYTHM_EXPERIMENT.md            the rhythm experiment written up as Introduction / Method / Results /
                                 Discussion: the melody generation algorithm, the 19-trial
                                 cue-withdrawal design, the measures and the analysis plan.
                                 START HERE before changing anything under rhythm_study/
quiz_analysis.py                entry point - batch offline analysis of saved quiz sessions: outcome
                                 metrics table, LED sync alignment, per-event review/correction,
                                 carry-over review, participant CSV export (section 7: data analysis)
tool_compress_review_videos.py  entry point (console) - scans data/quiz/*/review.mp4 and converts only
                                 non-H.264 review copies with ffmpeg; never enters raw/. A thin
                                 frontend over app/review_compress.py, which the launcher's
                                 "Review Video Compression" window (section 9) also drives
tool_backup_quiz_to_zip.py      entry point (console) - participant backup: creates one tested
                                 data/quiz-zip/Pxx.zip for each complete P01-P20 participant.
                                 A thin frontend over app/quiz_backup.py, which the launcher's
                                 "Participant ZIP Backup" window (section 9) also drives

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
                                 (section 10: validation experiments) - one subfolder per experiment
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
melody_generator/               SEPARATE STUDY (launcher section 11): standalone generator of short,
                                 simple, fixed-fingering practice melodies for the rhythm experiment.
                                 Stdlib only, shares no code with app/sequence_generator.py, writes
                                 only data/rhythm_experiment/ - see RHYTHM_EXPERIMENT.md
rhythm_study/                   SEPARATE STUDY (launcher section 11): the run-time and analysis half -
                                 the 19-trial schedule, the session controller and trial runner, and
                                 the whole analysis stage. Writes data/RhythmStudy/ and the
                                 rhythm- prefixed folders under data/quiz/ - see RHYTHM_EXPERIMENT.md
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

## Quiz data maintenance tools (launcher section 9)

Two tools for looking after data that has already been collected. Neither
is part of running a session or analysing one, and neither reads anything
the experiment reads.

Each has two frontends over one implementation:

| | window (launcher section 9) | console | shared logic |
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

## Rhythm experiment (launcher section 11)

A study of its own, separate from the Main User Study. That one asks which
cue modality teaches best; this one asks what survives when a cue is taken
away. One 15-note melody, played 19 times, with guidance withdrawn on a
schedule:

    Training x5 -> Probe 1 -> Training x5 -> Probe 2 -> Training x5 -> Probe 3 -> Final test

| phase | backlight | haptic | task | n |
|---|---|---|---|---|
| Training | on | on | cue/response | 15 |
| Probe | on | off | performance | 3 |
| Final test | off | off | performance | 1 |

Five buttons, in the order a session uses them: **Rhythm Melody Generator
(15-note)** and **Playback Rhythm Melody** make and audition the stimulus;
**Rhythm Trial Schedule** locks a participant to one melody and writes their
19-trial schedule; **Rhythm Experiment Session** runs it (two windows - the
session controller and the trial runner - and no participant-facing cue
screen, because this study has no visual guidance); **Rhythm Group Analysis**
is the whole analysis stage.

**It cannot disturb any earlier experiment.** That is structural, not a
convention: `melody_generator/` shares no code with `app/sequence_generator.py`
and has no third-party dependencies, `rhythm_study/` never imports
`app/pilot_study.py` or `app/sequence_generator.py` (a test parses the imports
to enforce it), nothing here writes `config.json`, a keyboard profile,
`data/sequence/`, `data/music/` or `data/MainUserStudy/`, and its melodies are
not registered with `app/song_library.py` so one can never appear in the song
pickers. The two studies meet only in the shared `data/quiz/` folder, where
the `rhythm-` prefix separates them.

### Where the rhythm-experiment documentation is

This section is orientation only. Nothing below is repeated here:

| Document | What it covers |
|---|---|
| **[RHYTHM_EXPERIMENT.md](RHYTHM_EXPERIMENT.md)** | The study, written up as Introduction / Method / Results / Discussion: the research question, where it sits among the platform's four experiments, the melody generation algorithm, the 19-trial design, why training and the probes are different *tasks* and what that means for the timing measures, every derived measure, and the analysis plan. |
| [melody_generator/README.md](melody_generator/README.md) | The generator as a tool: CLI, config, the four generation ideas in detail, the hand layouts, the full validation-code list, the scoring, the output file formats, and using it as a library. |

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
                               #   tool_compress_review_videos.py and the section 9 window
  quiz_backup.py               # GUI-free: eligibility + one tested data/quiz-zip/Pxx.zip per
                               #   complete participant. Driven by tool_backup_quiz_to_zip.py and
                               #   the section 9 window
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
                                #   participant_analysis_window; and the section 9 maintenance
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

Three things to know before trusting a latency number from these: motor pins
0 and 1 can conflict with serial on some boards, the supply has to be able to
drive every motor under test at once, and microphone placement dominates the
measurement. They estimate the *acoustic* end-to-end path - serial, MCU,
motor spin-up, sound propagation and audio capture together. The
instrumented, repeatable version of this measurement is the motor→ACC delay
experiment in launcher section 10, which uses an accelerometer instead of a
microphone: see
[validation_experiments/motor_acc_delay_experiment/README.md](validation_experiments/motor_acc_delay_experiment/README.md).

## Tele-training (launcher section 8)

Guidance delivered over a network: a teacher plays, a relay forwards, and
the student's LED strip and finger actuators fire from what the teacher
did. The three run as **separate processes** — a teacher, a student and a
relay have to be up at the same time, on different machines and holding
different devices — which is why this is the one launcher section whose
buttons start processes rather than sub-windows.

Six entries: **Relay Server**, **Teacher Client**, **Student Client**,
**Tele-training Setup Wizard**, **Network Latency Benchmark** and
**Remote Latency Analysis**. There is deliberately no shared settings
window: each of the three programs owns its own settings, because one
window showing all three roles' devices meant everyone was mostly looking
at settings that were not theirs.

To try it on one machine:

```bash
python -m server --init          # once: create the SQLite database
```

then start *Relay Server*, *Teacher Client* and *Student Client* from
section 8, in that order. Both clients arrive pre-filled with
`http://127.0.0.1:18765` and their demo account, so it is: sign in →
create/join a room → **Get ready** → **Ready for guidance** → **Start
teaching**.

### Where the tele-training documentation is

This section is orientation only. The module has two documents of its
own, and nothing here is repeated in them:

| Document | What it covers |
|---|---|
| **[REMOTE_GUIDANCE.md](REMOTE_GUIDANCE.md)** | The whole module. § 1–7 are the architecture: the three processes, the file map, the invariants that must not be broken (reaction-time origin, clock rules, what the relay may not do), the data flow of one live cue, the UI structure and the config schema. § 8–11 are running it, traps found the hard way, and known gaps. **§ 12 is the user-facing reference** — the setup wizard for a new machine, the guidance modes, what is reused from the local platform, provisional vs final results, which clock each timing measure is taken on, and the latency benchmark. |
| [server/README.md](server/README.md) | The relay as an operator guide: accounts and rooms, HTTP↔HTTPS and ws↔wss, JWT keys vs TLS certificates, deploying `server/` on its own, SQLite backup, and the full REST + WebSocket reference. |

Read REMOTE_GUIDANCE.md before changing anything under `remote_guidance/`
or `server/`.

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
* the descriptions, tooltips and status lines of section 10 — they quote
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
