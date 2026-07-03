# FingerAccuracy

Camera + MIDI based detection of *which finger* pressed *which piano key*.

[中文说明 (README.zh.md)](README.zh.md)

## Where this fits

This is the "which finger pressed this key" piece of a bigger piano
practice / learning experiment setup. Roughly, the full pipeline looks
like:

```
cue shown -> participant presses a key
  -> MIDI logs the key + a timestamp -> camera works out which finger -> analysis after the fact
```

This repo only covers the camera part, but it's built so it can be called
from the rest of that pipeline either live during a session, or later from
a recording - see [Using it as a library](#using-it-as-a-library).

## How it works

There are three steps, and only the first two need doing once per
physical setup:

1. Calibrate: click on the camera image to mark each key's exact pixel
   range (a paint-bucket style fill, bounded by Canny edges). This gets
   saved as a *profile* - `data/<profile_name>/keyboard_template.json` plus
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

## Setup

```bash
conda activate fingercam    # or: conda env create ...; pip install -r requirements.txt
```

Everything runs inside the `fingercam` conda environment (Python, OpenCV,
MediaPipe, mido, PySide6). `hand_landmarker.task` (MediaPipe's model file)
is bundled in this folder already.

## Pipeline scripts

Run these from inside `python-code/FingerAccuracy/`.

| Script | Purpose |
|---|---|
| `step1_keyboard_wizard.py` | PyQt wizard - calibrate a new profile (click-based, no keyboard shortcuts). |
| `step2_midi_mapping.py` | PyQt wizard - press keys 1..N in order, records the MIDI note each sends. |
| `main.py` | The live app - keyboard overlay + hand skeleton + real-time finger/key matching. |
| `demo_keyboard_preview.py` | Utility/demo - pick a profile from a dropdown and sanity-check it against the live camera, optionally showing MIDI note names instead of key numbers. |

```bash
python step1_keyboard_wizard.py
python step2_midi_mapping.py
python main.py
```

Each script's docstring has more detail; `config.json` (auto-created on
first run) holds the camera index/flip settings, Canny thresholds, the
MIDI port, and which profile is "active" (used by default when a script
doesn't ask you to pick one).

## Profile folder layout

```
data/<profile_name>/
  keyboard_template.json   # key ids, kind (white/black), note (usually null)
  keyboard_key_map.png     # pixel map: value = key_id + 1, 0 = background
  midi_mapping.json        # key_id -> MIDI note number (+ note name), from step 2
```

Multiple profiles can coexist (different desks, cameras, keyboards); every
tool has a dropdown or `active_profile` in `config.json` to pick between them.

## Using it as a library

Nothing here is locked to the PyQt UI - the GUIs in `fingeraccuracy/gui/`
are thin wrappers around plain functions/classes in the top-level package.

```python
from fingeraccuracy import (
    Config, Camera, HandTracker, MidiListener,
    KeyboardTemplate, MidiMapping, match_note_to_finger,
)

cfg = Config.load()
template = KeyboardTemplate.load(f"data/{cfg.active_profile}/keyboard_template.json")
mapping = MidiMapping.load(f"data/{cfg.active_profile}/midi_mapping.json")

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

### Record now, analyze later

`MidiListener` timestamps every event (seconds since the listener started -
the same convention `HandTracker` uses for its own video timestamps), so a
session's MIDI can be logged and matched against a recorded video afterward
instead of processed live:

```python
from fingeraccuracy import MidiListener, save_midi_log, analyze_recording

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

## Project layout

```
main.py                        # STEP 3-ish: live finger-accuracy detector (entry point)
step1_keyboard_wizard.py       # STEP 1: calibration wizard (entry point)
step2_midi_mapping.py          # STEP 2: MIDI mapping wizard (entry point)
demo_keyboard_preview.py       # utility: profile viewer (entry point)
config.json                    # camera/detection/wizard/MIDI settings + active_profile
hand_landmarker.task           # MediaPipe hand landmark model
data/<profile>/...             # calibration profiles (see above)
archive/                       # earlier prototype scripts, kept for reference only

fingeraccuracy/
  camera.py                    # Camera - cv2.VideoCapture wrapper (index or video file)
  config.py                    # Config / *Config dataclasses, JSON load/save
  hand_tracking.py             # HandTracker, Hand - MediaPipe wrapper, L1-L5/R1-R5 fingertips
  midi.py                      # MidiListener, MidiEvent, list_input_ports, save/load_midi_log
  finger_matching.py           # match_note_to_finger, FingerMatch - the core matching logic
  offline.py                   # analyze_recording - recorded video + MIDI log -> matches
  profiles.py                  # list_profiles - discover data/<name>/ folders
  keyboard/
    template.py                # KeyBox, KeyboardTemplate - the pixel-exact key map
    wizard.py                  # KeyFillWizard - paint-bucket key segmentation
    midi_mapping.py            # MidiMapping - key_id <-> MIDI note
    visualize.py                # color/label helpers for drawing a key_map
    detector.py                 # Canny edge detection used by the calibration wizard
  gui/                          # PyQt windows/pages for the four scripts above
```
