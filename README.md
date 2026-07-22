# Nail-Mounted Haptic Cues for Piano Training and Tele-training — Code Repository 

Implementation side of the MSc project "Nail-Mounted Haptic Cues for Piano
Training and Tele-training": a multimodal piano-guidance platform combining a
MIDI keyboard, per-key LED backlighting, nail-mounted vibrotactile actuators
(Teensy-driven), camera-based finger-use detection, and the experiment
software for the controlled pilot study defined in
`../final_report_2026/method/method.tex`.

It includes:
- asynchronous multi-motor control firmware (Teensy) + WS2812 LED control
- Python calibration/detection pipeline (camera + MIDI + MediaPipe) — see
  [main/README.md](main/README.md)
- constrained bimanual stimulus-sequence generator with difficulty
  validation and seeded reproducibility — see
  [main/SEQUENCE_GENERATOR_ALGORITHM.md](main/SEQUENCE_GENERATOR_ALGORITHM.md)
- cue-response quiz tools (visual / haptic finger cues) with full-session
  recording and offline finger-matching analysis, plus the main-user-study
  runner and analysis windows (launcher sections 6-7)
- repeatable hardware validation/calibration experiments (LRA resonance +
  intensity sweeps, motor→ACC delay) with GUI wrappers in launcher section 9
  — see [main/validation_experiments/](main/validation_experiments/)
- audio-based latency measurement and analysis utilities
- the pilot-study protocol is defined in `../final_report_2026/method/method.tex`

---

## Project Structure
```
root/
├── teensy_driver/   # Teensy 4.1 firmware: motors (PWM, async), WS2812 LEDs,
│                    #   LIS3DH accelerometers — full serial protocol in its README
├── main/            # Python platform (renamed from python-code): launcher hub,
│   │                #   calibration wizards, quiz + main-study tools, analysis
│   ├── app/                     # UI package: camera+MIDI finger-detection pipeline, GUIs
│   ├── launcher.py, setup_*.py, student_quiz*.py, quiz_analysis.py  # entry points
│   ├── common/                  # shared low-level serial/motor/LED modules
│   ├── validation_experiments/  # repeatable calibration experiments + shared rig helpers
│   ├── data/                    # calibration profiles, sequences, quiz/study/validation data
│   ├── test-script/             # older non-UI control/testing/latency scripts
│   └── read_data_from_accelerometer/  # legacy LIS3DH reader (reference, see its README)
├── archived/        # superseded prototypes + completed one-off studies (see its README):
│                    #   early firmware sketches, FingerAccuracy prototypes,
│                    #   human_reaction / attachment_verification study records
├── 3d_model/        # SolidWorks models for the hardware case
└── README.md
```

---

## Arduino Firmware (teensy_driver)

The firmware (identity `haptic-piano`, check with `E`) runs on a Teensy 4.1
and handles three subsystems over one USB serial connection:

- PWM output for 12 motor pins (10 finger motors + LRA/ERM test channels),
  non-blocking async scheduling, per-pin PWM frequency (boot default 224 Hz,
  the LRA's measured resonance)
- two WS2812 LED strips (framebuffer + DMA output)
- up to three LIS3DH accelerometers on a shared SPI bus (1.344 kHz ODR,
  streamed as `ACC,<id>,x,y,z`)

### Command summary (full protocol: [teensy_driver/README.md](teensy_driver/README.md))
```
X                          stop all motors
E                          firmware identity/version
P idx count amp on off     async pulse pattern on one motor
S mask amp                 set motors by bitmask (full-state, persists)
F idx freq                 set PWM frequency of a motor pin
L / B / C / U              LED set-pixel / brightness / clear / show
A START|STOP|RATE|WHOAMI   accelerometer streaming and probing
```
---

## Python Code (main)

Everything is reachable from the hub window (`python launcher.py`, nine
sections from initial setup through data analysis and validation
experiments); every tool also works standalone. See
[main/README.md](main/README.md) for the complete layout and per-tool
documentation.

- [app/](main/app) — the UI package: camera + MIDI finger-detection
  pipeline, quiz/pilot-study/analysis windows
- `validation_experiments/` — repeatable calibration experiments (LRA
  sweeps, motor→ACC delay) with per-run CSV/PNG/meta outputs under
  `data/validation_experiments/`
- `test-script/` — older, non-UI control, testing, and analysis scripts
- `common/` — shared low-level modules used across the platform

---

## File Overview

### common/controller.py

High-level interface for communicating with the device.

Provides:
- connection handling
- command sending
- pulse control
- async motor triggering

---

### common/serial_utils.py

Handles serial port detection and initialization.

Features:
- automatic port selection (macOS / Linux / Windows)
- fallback to manual selection when needed

---

### test-script/demo_send_motor_command_async.py

Demonstrates asynchronous motor control.

- starts one motor
- injects additional motors while it is still running
- shows non-blocking behavior

---

### test-script/demo_keyboard_control.py

Real-time keyboard control.

- maps keys (A–;) to motors (0–9)
- supports multiple simultaneous key presses
- uses `S mask` for real-time control

---

### test-script/detect_live_motor.py

Real-time microphone-based detection tool.

- continuously monitors audio input
- detects vibration presence
- helps tune detection parameters (threshold, frequency band)

---

### test-script/measure_latency.py

Single-motor latency measurement.

- triggers motor once
- detects acoustic onset
- estimates end-to-end latency

---

### test-script/measure_latency_multi_motor.py

Advanced latency measurement (multi-motor version).

Features:
- random motor selection (configurable)
- multiple runs (statistical analysis)
- per-motor latency comparison
- automatic plotting and saving

---

## Latency Measurement

Latency is estimated using:

1. Send command via serial  
2. Motor starts vibrating  
3. Microphone detects vibration sound  
4. Compute time difference  

This includes:
- serial transmission delay  
- MCU execution time  
- motor response time  
- acoustic propagation  
- audio capture latency  

---

## Output (latency_results)

Running the measurement script generates (under `test-script/`):
```
latency_results/
├── latency_by_run.png
├── latency_grouped_by_motor.png
├── latency_histogram.png
├── summary.txt
```
---

## Configuration

In `test-script/measure_latency_multi_motor.py`:

```python
TEST_MOTORS = [0, 2, 6]
NUM_RUNS = 20

You can modify:
	•	which motors to test
	•	number of runs
	•	detection parameters

⸻

Notes
	•	Using pins 0 and 1 may conflict with serial on some boards
	•	Ensure power supply is sufficient for multiple motors
	•	Microphone positioning significantly affects measurement accuracy
Version

Current version includes:
	•	async firmware
	•	Python control layer
	•	measurement + visualization pipeline
