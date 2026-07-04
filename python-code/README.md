# python-code

Python side of a piano practice / learning experiment setup: camera+MIDI
finger-accuracy detection, a virtual piano that drives LED feedback, and the
lower-level motor/LED/serial tooling both of those (and earlier prototypes)
are built on.

## Layout

```
app/                            UI package: camera + MIDI finger-accuracy detection
launcher.py                     entry point - hub window for every tool below, grouped by stage

setup_keyboard_wizard.py        entry point - app/ calibration wizard (stage 1: initial setup)
setup_midi_mapping_wizard.py    entry point - app/ MIDI mapping wizard (stage 1: initial setup)

test_keyboard_preview.py        entry point - app/ profile preview/sanity-check (stage 2: feature testing)
test_finger_accuracy.py         entry point - live finger/key detector (stage 2: feature testing, see app/)
test_virtual_piano_led.py       manual-test UI (PySide6): on-screen piano that lights the physical LED
                                 strips (stage 2: feature testing)

music_recording_wizard.py       entry point - records a song (video+MIDI) and saves a fingering-annotated
                                 score under data/music/<song>/ (stage 3: recording & playback)
music_playback.py               manual-test UI (PySide6): replays a saved song on an on-screen 88-key
                                 piano + finger dots (stage 3: recording & playback, see app/music_recording.py)

config.json                     app/ settings (auto-created), active profile
data/keyboard-profile/<profile>/   app/ calibration profiles (see "app/" below)
data/music/<song>/               saved recordings (see app/music_recording.py)
hand_landmarker.task            MediaPipe hand landmark model, used by app/
requirements.txt                Python deps for app/ (conda env "fingercam")

profile_led_mapper.py           reusable, GUI-free: profile + MIDI note -> LED positions
note_led_map.py                 this LED rig's fixed wiring (position -> LED pixels), used by profile_led_mapper.py
note_audio.py                   reusable, GUI-free: MIDI note -> audio tone playback (no profile needed)

common/                         shared low-level modules (serial/motor/LED), used by both
                                 test_virtual_piano_led.py and test-script/
test-script/                    older, non-UI motor/LED/latency/MIDI scripts + their output
archive/                        early FingerAccuracy prototypes, kept for reference only
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
   id it belongs to (0 for background).
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
| `launcher.py` | Hub window - one button per script above, grouped into setup / testing / recording+playback; only one tool is open at a time (they share the camera). |

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

### Profile folder layout

```
data/keyboard-profile/<profile_name>/
  keyboard_template.json   # key ids, kind (white/black), note (usually null)
  keyboard_key_map.png     # pixel map: value = key_id + 1, 0 = background
  midi_mapping.json        # key_id -> MIDI note number (+ note name), from step 2
```

`data/` also holds other kinds of experiment data alongside these
calibration profiles (e.g. `data/user-log/`, `data/user-profile/`), each in
its own subfolder.

Multiple profiles can coexist (different desks, cameras, keyboards); every
tool has a dropdown or `active_profile` in `config.json` to pick between them.

### Using it as a library

Nothing here is locked to the PyQt UI - the GUIs in `app/gui/`
are thin wrappers around plain functions/classes in the top-level package.

```python
from app import (
    Config, Camera, HandTracker, MidiListener,
    KeyboardTemplate, MidiMapping, match_note_to_finger,
)

cfg = Config.load()
template = KeyboardTemplate.load(f"data/keyboard-profile/{cfg.active_profile}/keyboard_template.json")
mapping = MidiMapping.load(f"data/keyboard-profile/{cfg.active_profile}/midi_mapping.json")

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

`MidiListener` timestamps every event (seconds since the listener started -
the same convention `HandTracker` uses for its own video timestamps), so a
session's MIDI can be logged and matched against a recorded video afterward
instead of processed live:

```python
from app import MidiListener, save_midi_log, analyze_recording

midi = MidiListener()
# ... run your experiment, e.g. cv2.VideoWriter recording the same camera ...
save_midi_log(midi.pop_events(), "session1_notes.json")

# Later, with no camera or MIDI device needed:
results = analyze_recording(
    video_path="session1.mp4",
    midi_log_path="session1_notes.json",
    profile_name="desk_webcam",
)
for r in results:
    print(r)   # FingerMatch(note=..., key_id=..., finger=..., inside=...) or None
```

`video_path` and `midi_log_path` must share a time origin (start recording
the video and the `MidiListener` at the same moment) since each MIDI event
is matched to hand positions in the video by that shared timestamp.

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
  finger_matching.py           # match_note_to_finger, FingerMatch - the core matching logic
  offline.py                   # analyze_recording - recorded video + MIDI log -> matches
  profiles.py                  # list_profiles - discover data/keyboard-profile/<name>/ folders
  keyboard/
    template.py                # KeyBox, KeyboardTemplate - the pixel-exact key map
    wizard.py                  # KeyFillWizard - paint-bucket key segmentation
    midi_mapping.py            # MidiMapping - key_id <-> MIDI note
    visualize.py                # color/label helpers for drawing a key_map
    detector.py                 # Canny edge detection used by the calibration wizard
  gui/                          # PyQt windows/pages for the scripts above
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
for manual testing: pick a profile, and an on-screen 25-key piano mirrors
it. Press and hold a virtual key (or the matching real MIDI key) and the
corresponding LED(s) on the physical Teensy WS2812 strips light up; release
to turn them off.

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
| `demo_async.py` | Demonstrates asynchronous motor control - starts one motor, injects others while it's still running, to show non-blocking behavior. |
| `demo_keyboard_control.py` | Real-time keyboard control - maps keys A-`;` to motors 0-9, supports multiple simultaneous key presses. |
| `detect_live_motor.py` | Real-time microphone-based detection tool for tuning vibration-detection parameters (threshold, frequency band). |
| `measure_latency.py` | Single-motor latency measurement - triggers a motor, detects the acoustic onset, estimates end-to-end latency. |
| `measure_latency_multi_motor.py` | Multi-motor latency measurement - random motor selection, multiple runs, per-motor comparison, automatic plotting into `latency_results/`. |
| `test_led_array.py` | Manual LED test script - lights specific pixels on specific strips to sanity-check wiring/colors. |
| `midi_probe.py` | Ground-truth calibration probe - prints the MIDI note number for each physical key as you press it, left to right. |
| `MIDI.py` | Minimal listener that prints every incoming MIDI message on two ports. |
| `plot_acc_from_ACC_stream.py` | Live-plots `ACC,x,y,z` accelerometer lines streamed over serial. |
| `plot_acc_from_ACC_stream_autostart.py` | Same, but also sends the serial commands to start/stop the stream automatically. |

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
