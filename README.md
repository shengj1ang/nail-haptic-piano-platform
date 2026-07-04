# Multi-Channel Vibration Control System

This project implements a complete system for controlling multiple vibration motors using an Arduino-compatible board (e.g. Teensy) and Python.

It includes:
- asynchronous multi-motor control firmware
- Python control interface
- real-time keyboard control
- audio-based latency measurement
- automatic data analysis and visualization

---

## Project Structure
```

root/
├── teensy_driver/       # Arduino / Teensy firmware
├── python-code/         # Python control and analysis tools
│   ├── app/             # UI app package: calibration + MIDI finger-detection pipeline
│   ├── test_finger_accuracy.py, launcher.py, setup_*.py, music_*.py  # entry points for app/
│   ├── test_virtual_piano_led.py # UI app: on-screen piano / LED preview (manual test)
│   ├── profile_led_mapper.py # reusable, GUI-free: profile + MIDI note -> LED positions
│   ├── note_led_map.py  # this LED rig's fixed wiring (position -> LED pixels)
│   ├── test-script/     # Older, non-UI control/testing/analysis scripts
│   └── common/          # Shared low-level modules (controller, led_controller, serial_utils)
├── README.md


```

---

## Arduino Firmware (teensy_driver)

The firmware runs on the microcontroller and handles:

- PWM output for up to 10 motors  
- non-blocking scheduling (each motor runs independently)  
- serial command parsing  

### Key Design

- Each motor has its own state machine  
- No blocking delay is used  
- Multiple motors can run different patterns simultaneously  

### Supported Commands
```
X
→ stop all motors

E
→ return firmware version

P    <on_ms> <off_ms>
→ run pulse task on one motor

S  
→ immediate override (debug / manual control)
```
---

## Python Code (python-code)

UI-based tools live directly under `python-code/`: the [app](python-code/app) package (calibration + MIDI detection pipeline, see [python-code/README.md](python-code/README.md)) and [test_virtual_piano_led.py](python-code/test_virtual_piano_led.py) (on-screen piano / LED preview).

Two subfolders hold everything else:
- `test-script/` — older, non-UI control, testing, and analysis scripts
- `common/` — shared low-level modules used by both `app/` and `test-script/`

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

### test-script/demo_async.py

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
