
# Teensy Driver Project Documentation

## Overview

This project is a Teensy-based real-time control system for three hardware domains:

1. **Vibration motors** driven through PWM output pins and scheduled asynchronously in firmware.
2. **WS2812 LED strips** driven from dedicated LED data pins and controlled over the same serial protocol.
3. **LIS3DH accelerometers** connected via SPI for vibration measurement and debugging.

The design goal is to keep the embedded firmware simple, deterministic, and easy to extend, while exposing a high-level Python interface for host-side control.

---

# Project Goals

- Control **10 vibration motors**
- Control **two WS2812 LED strips**
- Support **LIS3DH accelerometer measurement**
- Use **one USB serial connection**
- Maintain **non‑blocking firmware**
- Enable **Python real‑time plotting and experimentation**

---

# High-Level Architecture

## Firmware (Teensy)

Handles:

- Serial command parsing
- Motor scheduling
- LED framebuffer updates
- Accelerometer sampling
- SPI communication

Modules:

```
teensy_driver.ino
motor_driver.cpp / .h
LED_array.cpp / .h
accel_driver.cpp / .h
```

Each subsystem is updated inside the main loop.

---

## Python Host Layer

Python communicates through USB serial and can:

- Send commands
- Log data
- Plot accelerometer streams

Example tools:

```
controller.py
led_controller.py
plot_acc_from_ACC_stream_autostart.py
```

---

# Hardware Configuration

## Motors

- PWM pins: `0–9`
- PWM frequency: `300 Hz`
- Number of motors: `10`

Motor control uses asynchronous scheduling.

---

## LED Strips

Two WS2812 strips are supported, driven by the `WS2812Serial` library
(hardware UART + DMA). This library only runs on specific hardware serial
TX pins per Teensy model — see its `readme.md` for the full list. On
Teensy 4.1 the usable pins are: `1, 8, 14, 17, 20, 24, 29, 35, 47, 53`.
Pins `1/8/14` are used by the motor PWM outputs and `35` by the
accelerometer SPI (MISO), so the strips use two of the remaining pins:

| Strip | Pin |
|------|------|
| Strip 0 | 24 |
| Strip 1 | 29 |

`WS2812Serial.show()` starts the DMA transfer and returns immediately
without disabling interrupts, unlike the previous `FastLED` bit-banged
output (which blocked the CPU with interrupts disabled for the whole
strip refresh, adding latency/jitter to serial command handling).

Typical configuration:

```
NUM_LEDS_0 = 60
NUM_LEDS_1 = 60
```

Recommended electrical setup:

- 330Ω resistor on LED data line
- 1000µF capacitor on LED power
- Shared ground between LED power and Teensy

---

# Accelerometer Subsystem

The firmware supports **LIS3DH accelerometers over SPI**.

Driver files:

```
accel_driver.cpp
accel_driver.h
```

Design goals:

- Non‑blocking sampling
- Expandable to multiple sensors
- Compatible with Python real‑time plotting

---

# SPI Wiring

Shared SPI bus:

| Signal | Teensy Pin |
|------|------|
| SCK | 33 |
| MOSI | 34 |
| MISO | 35 |

Each accelerometer requires a unique CS pin.

Example:

```
ACCEL_1_CS = 36
ACCEL_2_CS = 37
ACCEL_3_CS = 38
```

Only one CS pin should be LOW at a time.

---

# Device Identification

The firmware verifies LIS3DH communication using:

```
WHO_AM_I register
```

Expected value:

```
0x33
```

---

# Serial Protocol

Commands are ASCII lines terminated with newline.

Example:

```
COMMAND arguments...
```

---

# Accelerometer Commands

| Command | Description |
|------|------|
| `A HELP` | list commands |
| `A WHOAMI` | read device ID |
| `A READ` | read single sample |
| `A START <ms>` | start streaming |
| `A STOP` | stop streaming |
| `A RATE <ms>` | change stream rate |
| `A STATUS` | print driver status |

Example:

```
A START 10
```

Streams every 10 ms.

---

# Accelerometer Output Format

Streaming format:

```
ACC,x,y,z
```

Example:

```
ACC,123,-15,1020
```

Values are raw LIS3DH readings.

---

# Python Integration

Script:

```
plot_acc_from_ACC_stream_autostart.py
```

Workflow:

1. Connect serial
2. Send

```
A STOP
A START 10
```

3. Read sensor stream
4. Plot real‑time data
5. Stop streaming when window closes

---

# Multi‑Accelerometer Expansion

SPI supports multiple sensors.

Shared:

```
SCK
MOSI
MISO
```

Unique:

```
CS1
CS2
CS3
...
```

Example read sequence:

```
CS1 LOW
read sensor
CS1 HIGH

CS2 LOW
read sensor
CS2 HIGH
```

Future format example:

```
ACC1,x,y,z
ACC2,x,y,z
```

---

# Firmware Design Principles

## Non‑Blocking Execution

Subsystems avoid `delay()` and use timer‑based scheduling.

---

## Modular Drivers

Drivers are separated:

- motor_driver
- LED_array
- accel_driver

---

## Deterministic Main Loop

Typical loop:

```
handleSerialCommands();  // dispatch any already-arrived command first
updateMotors();
updateLEDs();
updateAccelerometer();
```

Serial commands are checked and dispatched before the subsystem updates
so an already-arrived command isn't left waiting behind them.

---

# Performance

Typical accelerometer streaming loads:

| Interval | Load |
|----|----|
| 20 ms | very safe |
| 10 ms | recommended |
| 5 ms | moderate |
| 1 ms | heavy |

Recommended rate: **10–20 ms**.

---

# Project Structure

```
teensy_driver/

├── teensy_driver.ino
├── motor_driver.h
├── motor_driver.cpp
├── LED_array.h
├── LED_array.cpp
├── accel_driver.h
├── accel_driver.cpp
```

---

# Future Improvements

Possible upgrades:

- multiple accelerometer support
- timestamped sensor packets
- synchronized motor + sensor experiments
- DMA SPI acquisition
- binary streaming protocol

---

# Summary

The system supports:

- 10 vibration motors
- 2 WS2812 LED strips
- LIS3DH accelerometer sensing
- Python real‑time plotting

All modules run asynchronously and communicate through one serial interface.
