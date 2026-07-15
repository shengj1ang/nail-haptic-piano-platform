#include "motor_driver.h"

static const uint8_t PWM_PINS[16] = {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15};
static const uint8_t NUM_PWM = 16;
static const uint32_t DEFAULT_PWM_FREQ = 300;

// 'F' command frequency limits, clamped to a range that makes sense for
// vibration motors. All 16 pins are addressable, same as P/S, so a motor
// moved onto a spare pin stays fully controllable without re-flashing.
static const uint32_t FREQ_MIN = 50;
static const uint32_t FREQ_MAX = 20000;

// Last duty written to each pin, so a frequency change can re-apply it
// (changing the timer period invalidates the old duty registers).
static uint8_t currentAmp[NUM_PWM];

// Motor pulse task state
struct MotorTask {
  bool active;
  uint8_t amp;
  uint16_t remaining;   // pulses left
  uint32_t on_ms;
  uint32_t off_ms;

  bool state_on;
  uint32_t next_ts;
};

static MotorTask motors[NUM_PWM];

// Single write path for motor PWM so currentAmp always matches the hardware
static void writeMotor(uint8_t idx, uint8_t amp) {
  analogWrite(PWM_PINS[idx], amp);
  currentAmp[idx] = amp;
}

// On Teensy 4.1, pins on the same FlexPWM submodule always share one PWM
// frequency. Within motor pins 0-15 the coupled pairs are: 2&3 (FlexPWM4.2),
// 6&9 (FlexPWM2.2), 7&8 (FlexPWM1.3). Pins 10-15 sit on individual
// QuadTimer channels and are fully independent.
static int8_t sharedPartner(uint8_t idx) {
  switch (idx) {
    case 2: return 3;
    case 3: return 2;
    case 6: return 9;
    case 9: return 6;
    case 7: return 8;
    case 8: return 7;
    default: return -1;
  }
}

// Change one pin's PWM frequency, then re-apply the current duty to that
// pin and to any pin sharing its timer submodule.
static void setPinFrequency(uint8_t idx, uint32_t freq) {
#if defined(TEENSYDUINO)
  analogWriteFrequency(PWM_PINS[idx], (float)freq);
#endif
  writeMotor(idx, currentAmp[idx]);
  int8_t partner = sharedPartner(idx);
  if (partner >= 0) writeMotor((uint8_t)partner, currentAmp[partner]);
}

// Init all motor output pins
static void initPins() {
  for (uint8_t i = 0; i < NUM_PWM; i++) {
    pinMode(PWM_PINS[i], OUTPUT);
    digitalWrite(PWM_PINS[i], LOW);
    writeMotor(i, 0);
  }
}

// Set default PWM frequency on Teensy
static void initDefaultFrequencies() {
#if defined(TEENSYDUINO)
  for (uint8_t i = 0; i < NUM_PWM; i++) {
    analogWriteFrequency(PWM_PINS[i], (float)DEFAULT_PWM_FREQ);
  }
#endif
}

// Start one pulse task for one motor
static void startTask(uint8_t idx, uint16_t count, uint8_t amp, uint32_t on_ms, uint32_t off_ms) {
  if (idx >= NUM_PWM) return;

  MotorTask &m = motors[idx];

  m.active = true;
  m.amp = amp;
  m.remaining = count;
  m.on_ms = on_ms;
  m.off_ms = off_ms;

  m.state_on = true;
  m.next_ts = millis() + on_ms;

  writeMotor(idx, amp);
}

// Public init
void motorInit() {
  initPins();
  initDefaultFrequencies();
  stopAll();
}

// Stop all motors immediately
void stopAll() {
  for (uint8_t i = 0; i < NUM_PWM; i++) {
    motors[i].active = false;
    writeMotor(i, 0);
  }
}

// Non-blocking motor scheduler
void updateMotors() {
  uint32_t now = millis();

  for (uint8_t i = 0; i < NUM_PWM; i++) {
    MotorTask &m = motors[i];

    if (!m.active) continue;
    if (now < m.next_ts) continue;

    if (m.state_on) {
      // Turn motor off
      writeMotor(i, 0);
      m.state_on = false;
      m.next_ts = now + m.off_ms;
      m.remaining--;

      if (m.remaining == 0) {
        m.active = false;
      }
    } else {
      // Turn motor on again
      writeMotor(i, m.amp);
      m.state_on = true;
      m.next_ts = now + m.on_ms;
    }
  }
}

// Parse command: P idx count amp on_ms off_ms
void handlePulse(char* p) {
  long idx, count, amp, on_ms, off_ms;

  if (sscanf(p, "%ld %ld %ld %ld %ld",
             &idx, &count, &amp, &on_ms, &off_ms) == 5) {

    if (idx >= 0 && idx < NUM_PWM &&
        count > 0 &&
        amp >= 0 && amp <= 255) {

      startTask((uint8_t)idx,
                (uint16_t)count,
                (uint8_t)amp,
                (uint32_t)on_ms,
                (uint32_t)off_ms);
    }
  }
}

// Parse command: S mask amp
void handleImmediate(char* p) {
  long mask, amp;
  if (sscanf(p, "%ld %ld", &mask, &amp) == 2) {
    for (uint8_t i = 0; i < NUM_PWM; i++) {
      if (mask & (1 << i)) {
        writeMotor(i, amp);
      } else {
        writeMotor(i, 0);
      }
      motors[i].active = false;
    }
  }
}

// Parse command: F idx freq   (idx 0-15, or -1 for all motor pins)
void handleFrequency(char* p) {
  long idx, freq;

  if (sscanf(p, "%ld %ld", &idx, &freq) != 2) return;
  if (freq < (long)FREQ_MIN || freq > (long)FREQ_MAX) return;

  if (idx == -1) {
    for (uint8_t i = 0; i < NUM_PWM; i++) {
      setPinFrequency(i, (uint32_t)freq);
    }
  } else if (idx >= 0 && idx < NUM_PWM) {
    setPinFrequency((uint8_t)idx, (uint32_t)freq);
  }
}