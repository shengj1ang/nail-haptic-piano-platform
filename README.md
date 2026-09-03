# Nail-Mounted Haptic Cues for Piano Training and Tele-training

Code repository for the MSc project of the same name. It documents the
platform implementation, operation, validation tools, and analysis workflow.
The complete project report is included as
[`shengjiang_final_report.pdf`](shengjiang_final_report.pdf).

The platform is a multimodal piano-guidance rig: a MIDI keyboard with per-key
LED backlighting, ten nail-mounted vibrotactile actuators driven by a Teensy
4.1, an overhead camera for finger-use detection, and the experiment software
that runs and analyses the studies built on top of it.

Everything is reachable from one hub window:

```bash
cd main && python launcher.py
```

---

## Documentation index

**Every document lives in [`doc/`](doc/).** This README is the index and stays
short: each subject below is written up in exactly one place, and nothing here
repeats it. Screenshots used by the documents are in [`doc/image/`](doc/image).

### Start here

| Document | What it covers |
|---|---|
| **[doc/USER_MANUAL.md](doc/USER_MANUAL.md)** | **How to use the software.** A screenshot walkthrough of every launcher button, section by section: what each window is for, the steps you take in it, and what it writes. Start here if you want to *operate* the platform. |
| **[doc/PLATFORM.md](doc/PLATFORM.md)** | **How the software is built.** Directory layout, the calibration→detection pipeline, the study runner and the analysis windows, the data-maintenance tools, and the haptic configuration. Start here if you want to *change* it. |
| [doc/firmware.md](doc/firmware.md) | The Teensy 4.1 firmware: motors, LEDs, accelerometers, and the complete serial protocol. |
| [shengjiang_final_report.pdf](shengjiang_final_report.pdf) | The complete project report: research rationale, formal methods, results, discussion, and appendices. |

### The experiments

Four strands of experimental work, in the order they were built. Each has its
own protocol and write-up.

| Study | Launcher | Document |
|---|---|---|
| **Actuator validation** — resonance, intensity, motor→ACC delay, spectra, adhesives | Validation Experiments | [doc/validation-experiments.md](doc/validation-experiments.md), plus one document per experiment below |
| **Main User Study** — which cue modality teaches a key-and-finger mapping best | Main User Study, Data Analysis | method chapter of the report; stimulus algorithm in [doc/SEQUENCE_GENERATOR_ALGORITHM.md](doc/SEQUENCE_GENERATOR_ALGORITHM.md) |
| **Tele-training** — guidance delivered over a network | Tele-training | [doc/REMOTE_GUIDANCE.md](doc/REMOTE_GUIDANCE.md) |
| **Rhythm experiment** — what survives when the cue is withdrawn | Rhythm Experiment | [doc/RHYTHM_EXPERIMENT.md](doc/RHYTHM_EXPERIMENT.md) |

The validation experiments are experiments in the same sense as the others,
and they come first in the dependency order: they are why the cue amplitude
and frequency are the values they are rather than a guess.

| Validation experiment | Document |
|---|---|
| LRA resonance and intensity calibration (frequency + amplitude sweeps) | [doc/validation-lra-resonance.md](doc/validation-lra-resonance.md) |
| Actuator spectrogram (drive-frequency × amplitude intensity map) | [doc/validation-actuator-spectrogram.md](doc/validation-actuator-spectrogram.md) |
| Motor → accelerometer delay (LRA/ERM command-to-vibration latency) | [doc/validation-motor-acc-delay.md](doc/validation-motor-acc-delay.md) |
| Adhesion vibration comparison (three mounting adhesives, LRA) | [doc/validation-adhesion-comparison.md](doc/validation-adhesion-comparison.md) |

### Algorithms

| Document | What it covers |
|---|---|
| [doc/SEQUENCE_GENERATOR_ALGORITHM.md](doc/SEQUENCE_GENERATOR_ALGORITHM.md) | The Main User Study's stimulus generator: matched bimanual sequence families per difficulty level (α/β/γ), constraint-driven, seeded, with automatic difficulty validation. |
| [doc/melody-generator.md](doc/melody-generator.md) | The rhythm experiment's melody generator: motif-and-phrase construction, melodic rules in scale steps, static fingering, whole-beat rhythm, and the scoring that selects a candidate. |

### Deployment and reference

| Document | What it covers |
|---|---|
| [doc/server.md](doc/server.md) | The tele-training relay server as an operator guide: accounts and rooms, HTTP↔HTTPS, JWT keys vs TLS certificates, deploying `main/server/` on its own, and the full REST + WebSocket reference. |
| [doc/runtime-bin.md](doc/runtime-bin.md) | The drop-in folder for ffmpeg / 7z, which the data-maintenance tools search before `PATH`. |
| [doc/archived.md](doc/archived.md) | Superseded prototypes and two completed one-off studies, kept for provenance. |
| [doc/legacy-accelerometer-reader.md](doc/legacy-accelerometer-reader.md) | Legacy standalone LIS3DH reader, pre-dating the unified firmware. Reference only. |

---

## Repository structure

```
./
├── doc/                  every document in this repository, plus doc/image/
│                         for the screenshots the user manual uses
├── main/                 the Python platform - launcher hub, calibration wizards,
│   │                     quiz and study tools, analysis, all four studies
│   ├── app/                     UI package: camera + MIDI finger-detection pipeline, GUIs
│   ├── common/                  shared low-level serial / motor / LED modules
│   ├── remote_guidance/         tele-training clients
│   ├── server/                  the relay server - imports nothing from the platform
│   ├── melody_generator/        rhythm-experiment stimulus generator (stdlib only)
│   ├── rhythm_study/            rhythm-experiment sessions and analysis
│   ├── validation_experiments/  hardware validation experiments
│   ├── data/                    calibration profiles, stimuli, and every study's data
│   └── test-script/             the test suite, plus older non-UI hardware scripts
├── teensy_driver/        Teensy 4.1 firmware: motors (async PWM), WS2812 LEDs, LIS3DH
├── 3d_model/             SolidWorks models and STLs for the actuator mounts and case
└── archived/             superseded prototypes and completed one-off studies
```

---

## Getting started

**Requirements.** Python 3.11+ and the pinned dependency set:

```bash
pip install -r main/requirements.txt
```

That file also carries the tele-training dependencies; `main/server/requirements.txt`
lists the relay's alone, for deploying that folder by itself.

**Hardware.** A Teensy 4.1 running the `haptic-piano` firmware (verify with the
`E` command — see [doc/firmware.md](doc/firmware.md)), a MIDI keyboard, two
WS2812 LED strips, ten vibrotactile actuators, and a camera positioned above
the keyboard.

**First run.** Work through the launcher's Initial Setup section in order —
camera selection, keyboard calibration, MIDI mapping — before anything else.
Every later tool reads the calibration profile those wizards produce.
[doc/USER_MANUAL.md](doc/USER_MANUAL.md) walks through each window with a
screenshot; [doc/PLATFORM.md](doc/PLATFORM.md) explains what sits behind them.

---

## Notes for reviewers

- **This repository is the implementation record.** It documents what the
  software does, how it is built, and how to operate it. The
  [project report](shengjiang_final_report.pdf) provides the full research
  rationale and formal study methods.
  Where a design decision is constrained by the study methodology, the code
  comments identify the corresponding report section.
- **Each study's data stays in its own folder** under `main/data/`, and the
  studies are isolated from one another structurally rather than by
  convention — the constraints are stated in each study's document and are
  enforced by tests.
- **The test suite** is `main/test-script/`:

  ```bash
  cd main && QT_QPA_PLATFORM=offscreen python -m pytest test-script/ -q --ignore=test-script/test_led_array.py
  ```

  `test_led_array.py` is excluded because it prompts for a serial port on
  stdin; it is a hardware tool, not an automated test.
- **The manual's screenshots are regenerated, not re-photographed.**
  `main/test-script/capture_manual_screenshots.py` renders every window
  offscreen against a disposable copy of the project, with the camera, serial
  ports and MIDI stubbed, and writes `doc/image/`.
