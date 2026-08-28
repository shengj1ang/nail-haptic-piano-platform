# Nail-Mounted Haptic Cues for Piano Training and Tele-training

Code repository for the MSc project of the same name. The thesis is a separate
repository; its methodology chapter (`../final_report_2026/method/method.tex`)
defines the protocols this software implements, and the two are meant to be
read together.

The platform is a multimodal piano-guidance rig: a MIDI keyboard with per-key
LED backlighting, ten nail-mounted vibrotactile actuators driven by a Teensy
4.1, an overhead camera for finger-use detection, and the experiment software
that runs and analyses the studies built on top of it.

Everything is reachable from one hub window:

```bash
cd main && python launcher.py
```

---

## Where to read what

This README is the front door and stays short. Every subject below has its own
document, and each is the single place that subject is written up — nothing
here repeats them.

### Start here

| Document | What it covers |
|---|---|
| **[main/README.md](main/README.md)** | The platform: directory layout, every launcher section, the calibration→detection pipeline, and how to run each tool. **The main entry point for the software.** |
| [teensy_driver/README.md](teensy_driver/README.md) | The firmware: motors, LEDs, accelerometers, and the complete serial protocol. |

### The experiments

Four strands of experimental work, in the order they were built. Each has its
own protocol and write-up.

| Study | Launcher | Document |
|---|---|---|
| **Actuator validation** — resonance, intensity, motor→ACC delay, spectra, adhesives | section 10 | [main/validation_experiments/README.md](main/validation_experiments/README.md) and one README per experiment |
| **Main User Study** — which cue modality teaches a key-and-finger mapping best | sections 6–7 | method chapter; stimulus algorithm in [main/SEQUENCE_GENERATOR_ALGORITHM.md](main/SEQUENCE_GENERATOR_ALGORITHM.md) |
| **Tele-training** — guidance delivered over a network | section 8 | [main/REMOTE_GUIDANCE.md](main/REMOTE_GUIDANCE.md) |
| **Rhythm experiment** — what survives when the cue is withdrawn | section 11 | [main/RHYTHM_EXPERIMENT.md](main/RHYTHM_EXPERIMENT.md) |

The validation experiments are experiments in the same sense as the others,
and they come first in the dependency order: they are why the cue amplitude
and frequency are the values they are rather than a guess.

### Algorithms

| Document | What it covers |
|---|---|
| [main/SEQUENCE_GENERATOR_ALGORITHM.md](main/SEQUENCE_GENERATOR_ALGORITHM.md) | The Main User Study's stimulus generator: matched bimanual sequence families per difficulty level (α/β/γ), constraint-driven, seeded, with automatic difficulty validation. |
| [main/melody_generator/README.md](main/melody_generator/README.md) | The rhythm experiment's melody generator: motif-and-phrase construction, melodic rules in scale steps, static fingering, whole-beat rhythm, and the scoring that selects a candidate. |

### Deployment and reference

| Document | What it covers |
|---|---|
| [main/server/README.md](main/server/README.md) | The tele-training relay server as an operator guide: accounts and rooms, HTTP↔HTTPS, JWT keys vs TLS certificates, deploying `server/` on its own, and the full REST + WebSocket reference. |
| [archived/README.md](archived/README.md) | Superseded prototypes and two completed one-off studies, kept for provenance. |
| [main/read_data_from_accelerometer/README.md](main/read_data_from_accelerometer/README.md) | Legacy standalone LIS3DH reader, pre-dating the unified firmware. Reference only. |

---

## Repository structure

```
individual_project_2026/
├── main/                 the Python platform - launcher hub, calibration wizards,
│   │                     quiz and study tools, analysis, all four studies
│   ├── app/                     UI package: camera + MIDI finger-detection pipeline, GUIs
│   ├── common/                  shared low-level serial / motor / LED modules
│   ├── remote_guidance/         tele-training clients (section 8)
│   ├── server/                  the relay server - imports nothing from the platform
│   ├── melody_generator/        rhythm-experiment stimulus generator (stdlib only)
│   ├── rhythm_study/            rhythm-experiment sessions and analysis
│   ├── validation_experiments/  hardware validation experiments (section 10)
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
`E` command — see [teensy_driver/README.md](teensy_driver/README.md)), a MIDI
keyboard, two WS2812 LED strips, ten vibrotactile actuators, and a camera
positioned above the keyboard.

**First run.** Work through launcher section 1 (Initial Setup) in order —
camera selection, keyboard calibration, MIDI mapping — before anything else.
Every later tool reads the calibration profile those wizards produce.
[main/README.md](main/README.md) walks through each section.

---

## Notes for reviewers

- **The protocols live in the thesis**, not here. This repository documents
  what the software does and how it is built; `method.tex` defines what the
  studies are and why. Where a design decision is forced by the methodology,
  the code comments say so and name the section.
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
