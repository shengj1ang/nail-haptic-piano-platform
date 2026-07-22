# Teensy Driver Project Documentation

Firmware: **haptic-piano** (check with the `E` command). Target board: **Teensy 4.1**.

## Overview

This project is a Teensy-based real-time control system for three hardware domains, all driven over **one USB serial connection**:

1. **Vibration motors** (10 finger motors + test/spare channels) driven through PWM output pins and scheduled asynchronously in firmware.
2. **Two WS2812 LED strips** driven from dedicated LED data pins.
3. **LIS3DH accelerometers** (one or more, on a shared SPI bus) for vibration measurement and debugging.

The design goal is to keep the embedded firmware simple, deterministic and non-blocking, while exposing a high-level Python interface for host-side control, logging and real-time plotting.

---

## Current Wiring (Quick Start)

Everything connected today, in one table:

| Teensy pin | Function | Notes |
|------|------|------|
| 0–9 | Finger vibration motors (PWM) | Host-side default mapping: L5→0, L4→1, L3→2, L2→3, L1→4, R1→5, R2→6, R3→7, R4→8, R5→9 (`app/haptic_cue.py`) |
| 10 | ERM test channel + spare motor channel (PWM) | used in the actuator-comparison experiments; doubles as the spare if a finger port fails |
| 11 | LRA test channel + spare motor channel (PWM) | used in the actuator-comparison experiments; doubles as the spare if a finger port fails |
| 12 | Reserved for SPI | not initialized by the firmware (pin 12 is SPI0 MISO) |
| 13 | Free | not initialized by the firmware; carries the onboard orange LED (and SPI0 SCK) |
| 24 | WS2812 LED strip 0 data | 60 LEDs (`NUM_LEDS_0`) |
| 29 | WS2812 LED strip 1 data | 60 LEDs (`NUM_LEDS_1`) |
| 33 | Accel SPI SCK | shared bus |
| 34 | Accel SPI MOSI | shared bus |
| 35 | Accel SPI MISO | shared bus |
| 36 | Accel CS, sensor 0 | one CS per sensor; `CS_PINS[]` in `accel_driver.cpp` |
| 37 | Accel CS, sensor 1 | in `CS_PINS[]` since v2.8.0 - probed at boot, skipped if no sensor attached |
| 38 | Accel CS, sensor 2 | in `CS_PINS[]` since v2.8.0 - probed at boot, skipped if no sensor attached |
| USB | Serial link to host | 115200 nominal (native USB — the baud value is ignored; actual throughput is USB speed) |

All 12 motor pins (0–11) are initialized and fully addressable by every motor command (`P`/`S`/`F`). The two channels beyond the ten fingers (10/11) are a **deliberate redundancy design**: the firmware is not hard-limited to ten fingers (room for secondary development), and if a finger port is damaged the motor can be moved to 10 or 11 and remapped host-side — no firmware re-flash needed. Pins 12–15 are intentionally left out of the motor set (12 reserved for SPI, 13 carries the onboard LED).

### Power

| Rail | Powers | Source | Notes |
|------|------|------|------|
| 10 V | Motor power (drive side of the driver board) | External supply through a boost module | this is the rail the PWM chops, so effective motor voltage = `amp/255 × 10 V`; full duty (`amp = 255`) puts the full 10 V on a motor — keep `amp` within values validated for your motors. Check the boost module's current rating against all motors running at once |
| 5 V | WS2812 LED strips | External 5 V supply | 2×60 LEDs can draw up to ~7 A at full white — never from USB; put a 1000 µF capacitor across the strip power input and a 330 Ω resistor in each data line |
| 3.3 V | Motor driver board logic (chip supply) + LIS3DH accelerometer(s) | Teensy `3.3V` pin | matches the Teensy's 3.3 V PWM/SPI signal levels; the LIS3DH is a 3.3 V part and Teensy 4.1 pins are **not 5 V tolerant** |
| GND | everything | common | tie **all** grounds together (Teensy, boost module output, LED supply, driver board, sensors); without a shared ground the data signals have no return path and nothing works reliably |
| — | Teensy itself | USB | peripherals take their power directly from the external supplies and never back-feed the Teensy. If you do want to power the Teensy from VIN externally while USB is also plugged in, cut the VIN–VUSB pad first (standard Teensy practice) |

Wire count per accelerometer: 6 (3.3V, GND, SCK, MOSI, MISO, CS) — each additional sensor adds only its own CS wire, the other five are taps onto the shared bus/rails.

First contact checklist:

1. Flash `teensy_driver.ino`, open the serial port (any baud).
2. Send `E` → expect `E haptic-piano v2.9.0`.
3. Send `A WHOAMI` → expect one line per CS slot; a fitted sensor answers `0x33`, e.g. `ACC WHOAMI 0 0x33` with empty slots reading something else (typically `0xFF`).
4. Send `S 1 64` → motor 0 vibrates at the project-default intensity; `X` stops everything.
5. Send `L 0 0 255 0 0 128` then `U` → first pixel of strip 0 lights red.

---

## Architecture

### Firmware (Teensy)

One module per subsystem, each updated from the main loop:

```
teensy_driver/
├── teensy_driver.ino     serial line reader + command dispatch
├── motor_driver.cpp/.h   PWM motors, async pulse scheduler
├── LED_array.cpp/.h      WS2812 strips (framebuffer + DMA output)
├── accel_driver.cpp/.h   LIS3DH sampling + streaming
```

Main loop:

```
handleSerialCommands();  // dispatch any already-arrived command first
updateMotors();
updateLEDs();
updateAccelerometer();
```

Design principles:

- **Non-blocking execution**: subsystems avoid `delay()` and use timer-based scheduling.
- **Deterministic dispatch**: serial commands are checked and dispatched before the subsystem updates, so an already-arrived command isn't left waiting behind them.
- **Modular drivers**: motor / LED / accel code never touch each other's pins or state.

### Python Host Layer

Python communicates through USB serial and can send commands, log data and plot accelerometer streams. Example tools (`python-code/`):

```
common/controller.py                        motor control (VibratorController)
common/led_controller.py                    LED control
test-script/plot_acc_from_ACC_stream_autostart.py   real-time accel plot
```

Typical accelerometer workflow (what the autostart plot script does):

1. Connect serial
2. Send `A STOP` then `A START 10`
3. Read the `ACC,id,x,y,z` stream and plot in real time
4. Send `A STOP` when the window closes

---

## Serial Protocol

Commands are ASCII lines terminated with newline:

```
COMMAND arguments...
```

### Motor commands

| Command | Description |
|------|------|
| `X` | stop all motors immediately |
| `E` | echo firmware identity, e.g. `E haptic-piano v2.9.0` (name + version) |
| `P idx count amp on_ms off_ms` | pulse motor `idx`: `count` cycles of `on_ms` on / `off_ms` off at amplitude `amp` (0–255) |
| `S mask amp` | set motors by bitmask (bits 0–11): every pin whose bit is set in `mask` runs at amplitude `amp` (0–255), all others stop. Persists until the next command |
| `F idx freq` | set the PWM frequency (Hz) of motor pin `idx` (0–11); `idx = -1` sets all 12 pins at once. Valid range 50–20000 Hz. Persists until reboot (boot default: **224 Hz** — the LRA's measured resonance, see `experiments/lra_frequency_sweep`) |

Notes:

- `S` is a *full-state* command, not incremental: to keep two motors running, set both bits in one command — a second `S` with only one bit would stop the other motor.
- `S` never times out. If the host crashes without sending `X`, the motor keeps running — host code should always stop motors in its cleanup path.
- `P` is for finite pulse patterns; `S` is for "hold until told otherwise".

#### Amplitude vs frequency — which knob does what

The PWM signal has two independent parameters, and they play very different roles:

- **Amplitude (`amp`, the duty cycle)** is the *intensity knob*. The supply voltage is fixed; the duty cycle sets the effective drive voltage the motor sees. It is a field of every `P`/`S` command, so the host can change it per command at runtime — no firmware change needed.
- **Frequency (`F` command)** is a *calibration setting*, not an intensity control. It is set once to match the actuator (see below) and then left alone.

**ERM (eccentric rotating mass — pin 10, and the finger motors).**
An ERM is a DC motor: its inertia low-pass-filters the PWM, so it only responds to the *average* voltage (`duty × supply`). Intensity rises monotonically with `amp` over the full 0–255 range. The PWM frequency barely matters — anything from ~100 Hz up to the low kHz works, so the 224 Hz boot default (chosen for the LRA) is equally fine for ERMs. Do not try to control ERM intensity with `F`.

**LRA (linear resonant actuator).**
An LRA is a spring–mass resonator and behaves completely differently:

- **It ignores DC.** With this unipolar (single-transistor) drive, the LRA responds to the *AC fundamental* of the PWM square wave, whose amplitude is proportional to `sin(π · amp/255)`. So intensity is **not** linear in `amp`: it peaks at `amp ≈ 128` (50% duty) and falls back to zero at `amp = 255` (pure DC). Measurement further showed **stroke saturation above `amp ≈ 88`** — output plateaus, so the useful intensity range is roughly **0–90**.
- **The PWM frequency must sit at the LRA's resonant frequency f₀.** For this project's LRA the mounted resonance was **measured at 224 Hz** (the boot default since v2.7.0); coin LRAs in general sit somewhere in 150–250 Hz. The resonance is narrow (high Q, measured usable region ~221–231 Hz): a few Hz off the peak costs a large fraction of the output, no matter how high `amp` is. Frequency is therefore useless as an intensity knob — detuning is unrepeatable (f₀ drifts with load and temperature) and changes the perceived pitch of the vibration, which confounds intensity experiments.

**Calibrated values (see `experiments/lra_frequency_sweep/README.md` for the full method, data and standards rationale):**

| Quantity | Value | Where it lives |
|------|------|------|
| Resonant frequency f₀ | **224 Hz** (~2.8× the output of the old 300 Hz default) | firmware boot default; runtime override via `F <port> <freq>` |
| Default cue intensity | **`amp = 64`** → 0.49 m/s² RMS, centre of the 0.4–0.6 m/s² "clearly perceptible, not annoying" band | `HAPTIC_AMPLITUDE` in `app/haptic_cue.py`, `pulse()` defaults in `common/controller.py`, experiment `AMP` constants |

**Re-calibrating after a hardware change.**
f₀ is mount-specific, so re-run the calibration whenever the LRA is remounted or swapped — the dedicated scripts in `experiments/lra_frequency_sweep/` automate the whole procedure (frequency sweep at `amp = 128`, then amplitude sweep at f₀, both measured with the accelerometer subsystem and saved as CSV + plot). The manual equivalent, if ever needed: sweep `F <port> <freq>` at `amp = 128` while recording RMS acceleration (`A START`), lock the peak frequency, then sweep `amp` to map intensity.

#### PWM timer sharing (Teensy 4.1)

Pins on the same FlexPWM submodule always share one PWM frequency: changing one pin's frequency with `F` also changes its partners'. The firmware automatically re-applies the current duty to the target pin and its partner after a frequency change (a new timer period would otherwise invalidate the old duty registers).

| Motor pin | Timer | Shares frequency with |
|------|------|------|
| 0 | FlexPWM1.1 | 42, 43 (unused) |
| 1 | FlexPWM1.0 | 44, 45 (unused) |
| 2, 3 | FlexPWM4.2 | **each other** |
| 4 | FlexPWM2.0 | 33 (accel SPI SCK — muxed to SPI, unaffected) |
| 5 | FlexPWM2.1 | — |
| 6, 9 | FlexPWM2.2 | **each other** |
| 7, 8 | FlexPWM1.3 | each other (and 25, unused) |
| 10 | QuadTimer1.0 | — (independent) |
| 11 | QuadTimer1.2 | — (independent) |

Practical consequences:

- The ERM (pin 10) and LRA (pin 11) are on independent timers, so retuning the LRA never affects any other motor.
- The finger-motor pairs 2/3, 6/9 and 7/8 change frequency together. This is harmless in practice: all finger motors are the same actuator type and run at the shared 224 Hz default.

### LED commands

| Command | Description |
|------|------|
| `L strip idx r g b brightness` | set one pixel: `strip` 0/1, `idx` within the strip, `r g b` 0–255, per-pixel `brightness` 0–255 |
| `B brightness` | global brightness 0–255, applied to both strips |
| `C strip` | clear strip 0 or 1; `C -1` clears both |
| `U` | push the framebuffer to the strips immediately |

Pixel changes are buffered and pushed automatically on the next loop pass once something changed; `U` forces an immediate push.

### Accelerometer commands

All commands operate on every sensor in `CS_PINS`:

| Command | Description |
|------|------|
| `A HELP` | list commands |
| `A WHOAMI` | probe each sensor, one `ACC WHOAMI <i> 0x..` line per sensor (`0x33` = found; the LIS3DH `WHO_AM_I` register value) |
| `A READ` | read one sample from every detected sensor |
| `A START <ms>` | start streaming all detected sensors (needs ≥ 1 detected), e.g. `A START 10` streams every 10 ms |
| `A STOP` | stop streaming |
| `A RATE <ms>` | change stream rate |
| `A STATUS` | print driver status, incl. `detected=<found>/<total>` |

#### Output format

Streaming format (since v2.5.0) — one line per sensor per tick, with a uniform id field for every sensor:

```
ACC,id,x,y,z
```

`id` is the sensor's index in `CS_PINS` (0, 1, 2, ...). Example with two sensors:

```
ACC,0,123,-15,1020
ACC,1,98,-2,1015
```

Values are raw LIS3DH readings.

Host-side parsers must expect 5 fields and select on the id (`parts[1]`); all bundled Python scripts were updated accordingly and default to sensor 0. Firmware < v2.5.0 streamed 4-field `ACC,x,y,z` lines — old scripts and old firmware are mutually incompatible, so flash and update together.

### Identifying the device from the host

On Windows the rig enumerates as a generic "USB Serial Device (COMn)", so the port name alone does not say what the device is. Two-step auto-detection that works everywhere:

1. Filter candidate ports by USB ID — Teensy is `VID:PID = 16C0:0483` (pyserial: `port.vid` / `port.pid`). This needs no communication.
2. Confirm by probing: open the port, send `E\n`, and expect a reply starting with `E haptic-piano` within a short timeout. The firmware name in the reply is the rig's unique handshake — a bare version string could false-match some other echoing device.

Probe only ports that passed step 1: sending bytes to an unknown serial device can trigger unintended behavior on unrelated equipment.

---

## Accelerometer Subsystem

The firmware supports **multiple LIS3DH accelerometers on one shared SPI bus** — SCK (33) / MOSI (34) / MISO (35) are common to all sensors, and each sensor has its own chip-select (CS) pin. Only one CS is low at a time. The CS pins live in one array in `accel_driver.cpp`:

```cpp
static const uint8_t CS_PINS[] = {36, 37, 38};   // sensor id = array index
```

### Adding a sensor

Since v2.8.0 three CS slots are pre-declared (36 → id 0, 37 → id 1,
38 → id 2), so up to three sensors need **no firmware change**: wire the
new sensor's SCK/MOSI/MISO onto the shared bus and its CS to 37 or 38,
and it appears as sensor 1 or 2 on the next boot (or after `A WHOAMI` /
`A START`, both of which re-probe). Absent slots simply stay undetected
and are skipped by streaming — `ACC ERROR NOT_FOUND` only appears when
*no* slot answers. `A WHOAMI` lists one line per slot; a fitted sensor
answers `0x33`.

This also means a single sensor can be moved between CS pins to test
the host-side sensor-id path (e.g. moving CS from 36 to 37 makes the
same physical sensor stream as `ACC,1,...`).

To go beyond three sensors, append another free GPIO to `CS_PINS[]` in
`accel_driver.cpp` and re-flash — detection, streaming and every `A`
command pick up the extra slots automatically; nothing else changes.

Sensors are read back-to-back each streaming tick, one CS low at a time:

```
CS0 LOW  -> read sensor 0 -> CS0 HIGH   -> print ACC,0,...
CS1 LOW  -> read sensor 1 -> CS1 HIGH   -> print ACC,1,...
```

Consecutive sensors in one tick are read ~25 µs apart — a few percent of the sensor's ~0.74 ms sample period, so the per-tick lines can be treated as simultaneous samples.

Wiring note: keep the shared bus lines short and star-shaped rather than one long daisy-chain; if readings get flaky with several sensors attached, increase `spiDelayShort()` slightly (a slower clock costs almost nothing at these sample rates).

### SPI implementation and performance

The LIS3DH is read over a **bit-banged SPI bus** (mode 3) on ordinary GPIO pins, using `digitalWriteFast`/`digitalReadFast` with ~100 ns half-phase delays (~2.5 MHz effective clock, comfortably inside the LIS3DH's 10 MHz limit). One XYZ read (7 bytes) blocks the main loop for roughly **25 µs** per sensor — negligible against motor scheduling and serial handling even at the fastest 1 ms streaming rate:

| Interval | Loop time spent sampling (per sensor) |
|----|----|
| 20 ms | ~0.1% |
| 10 ms | ~0.25% |
| 5 ms | ~0.5% |
| 1 ms | ~2.5% |

(Historical note: the original implementation used `digitalWrite` with 3 µs delays, which made every sample block the loop for ~0.8 ms and visibly jittered motor pulse timing while streaming.)

The practical rate floor is the sensor itself, not the bus: since v2.9.0 the LIS3DH is configured for a **1.344 kHz output data rate** (`CTRL1 = 0x97`, ~0.74 ms per sample; v2.8.0 and earlier used 400 Hz / `CTRL1 = 0x77`), so `A START 1` streams fresh samples. Recommended rate: **1–10 ms**; the motor→ACC delay experiment uses 1 ms for onset resolution, the calibration sweeps 3 ms.

Two further upgrade paths, deliberately not taken yet:

- **Hardware SPI peripheral**: rewire the sensor onto hardware SPI pins and use `SPI.transfer()` at 8 MHz for a few-µs sample read. Pin 12 (SPI0 MISO) and pin 13 (SPI0 SCK) are kept free for this, but SPI0's MOSI is pin 11 — currently the LRA channel — so the conflict-free option is the SPI1 set (MOSI1 = 26, SCK1 = 27, MISO1 = 39).
- **DMA acquisition**: zero CPU cost, but overkill at the current sample rates — only worth it for multi-sensor kHz-range streaming.

---

## LED Subsystem

Two WS2812 strips are driven by the `WS2812Serial` library (hardware UART + DMA). `WS2812Serial.show()` starts the DMA transfer and returns immediately without disabling interrupts, unlike the previous `FastLED` bit-banged output (which blocked the CPU with interrupts disabled for the whole strip refresh, adding latency/jitter to serial command handling).

The library only runs on specific hardware serial TX pins per Teensy model — see its `readme.md` for the full list. On Teensy 4.1 the usable pins are: `1, 8, 14, 17, 20, 24, 29, 35, 47, 53`. Pins `1/8/14` are used by the motor PWM outputs and `35` by the accelerometer SPI (MISO), so the strips use 24 (strip 0) and 29 (strip 1).

If you need to rewire onto different pins (e.g. pin 24/29 is inconvenient for your layout), any two of the remaining usable pins work — just update `LED_PIN_0`/`LED_PIN_1` in `LED_array.cpp` to match:

| Pin | Location |
|------|------|
| 17 | main header (top, pins 0-39 row) |
| 20 | main header (top, pins 0-39 row) |
| 24 | main header (top, pins 0-39 row) — currently strip 0 |
| 29 | main header (top, pins 0-39 row) — currently strip 1 |
| 47 | extra pads on the back of the board (Teensy 4.1 only, not on the main header) |
| 53 | extra pads on the back of the board (Teensy 4.1 only, not on the main header) |

Prefer 17/20/24/29 unless you specifically want to use the back pads — they sit on the same two rows as everything else on the board, so no extra soldering is needed.

Strip lengths are set in `LED_array.cpp` (`NUM_LEDS_0 = 60`, `NUM_LEDS_1 = 60`). For the electrical setup (series resistor, bulk capacitor, common ground) see *Current Wiring* above.

---

## Future Improvements

Possible upgrades:

- timestamped sensor packets
- synchronized motor + sensor experiments
- hardware SPI / DMA acquisition (see *SPI implementation and performance*)
- binary streaming protocol
