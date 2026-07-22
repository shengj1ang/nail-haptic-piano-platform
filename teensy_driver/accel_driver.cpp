#include "accel_driver.h"

// Confirmed working bus mapping from brute-force search:
// SCK=33, MOSI=34, MISO=35 (shared), CS=36 (per sensor)
static const uint8_t PIN_SCK  = 33;
static const uint8_t PIN_MOSI = 34;
static const uint8_t PIN_MISO = 35;

// Chip-select pins, one per LIS3DH on the shared SPI bus. Sensor id =
// index in this array (36 -> id 0, 37 -> id 1, 38 -> id 2). All three
// slots are probed at boot / A WHOAMI / A START; absent sensors simply
// stay undetected and are skipped by streaming, so a sensor can be
// wired to any of these CS pins without re-flashing. To go beyond
// three, append another free GPIO here and re-flash.
static const uint8_t CS_PINS[] = {36, 37, 38};
static const uint8_t NUM_ACCEL = sizeof(CS_PINS) / sizeof(CS_PINS[0]);

// LIS3DH registers
static const uint8_t REG_WHO_AM_I   = 0x0F;
static const uint8_t REG_CTRL1      = 0x20;
static const uint8_t REG_CTRL4      = 0x23;
static const uint8_t REG_OUT_X_L    = 0x28;
static const uint8_t LIS3DH_ID      = 0x33;

struct AccelState {
  bool detected[NUM_ACCEL] = {};
  bool streamEnabled = false;
  uint32_t intervalMs = 10;
  uint32_t nextStreamMs = 0;
  uint32_t droppedFrames = 0;
};

static AccelState g_accel;

// Half-phase delay for the bit-banged bus. 100 ns per phase (~2.5 MHz
// clock) stays well inside the LIS3DH's 10 MHz SPI limit while leaving
// margin for jumper wiring. The old delayMicroseconds(3) here made one
// XYZ read block the main loop for ~0.8 ms.
static inline void spiDelayShort() {
  delayNanoseconds(100);
}

// Bit-banged SPI MODE3 (CPOL=1, CPHA=1)
static uint8_t spiTransfer(uint8_t data) {
  uint8_t rx = 0;

  for (int i = 7; i >= 0; --i) {
    digitalWriteFast(PIN_SCK, HIGH);
    spiDelayShort();

    digitalWriteFast(PIN_MOSI, (data >> i) & 0x01);
    spiDelayShort();

    digitalWriteFast(PIN_SCK, LOW);
    spiDelayShort();

    rx <<= 1;
    if (digitalReadFast(PIN_MISO)) {
      rx |= 1;
    }

    spiDelayShort();
  }

  digitalWriteFast(PIN_SCK, HIGH);
  spiDelayShort();
  return rx;
}

static void writeReg(uint8_t sensor, uint8_t reg, uint8_t value) {
  digitalWriteFast(CS_PINS[sensor], LOW);
  spiTransfer(reg & 0x7F);
  spiTransfer(value);
  digitalWriteFast(CS_PINS[sensor], HIGH);
}

static uint8_t readReg(uint8_t sensor, uint8_t reg) {
  digitalWriteFast(CS_PINS[sensor], LOW);
  spiTransfer(0x80 | reg);
  uint8_t value = spiTransfer(0x00);
  digitalWriteFast(CS_PINS[sensor], HIGH);
  return value;
}

static void readAccelRaw(uint8_t sensor, int16_t &x, int16_t &y, int16_t &z) {
  digitalWriteFast(CS_PINS[sensor], LOW);
  spiTransfer(0xC0 | REG_OUT_X_L); // read + auto-increment

  uint8_t xL = spiTransfer(0x00);
  uint8_t xH = spiTransfer(0x00);
  uint8_t yL = spiTransfer(0x00);
  uint8_t yH = spiTransfer(0x00);
  uint8_t zL = spiTransfer(0x00);
  uint8_t zH = spiTransfer(0x00);

  digitalWriteFast(CS_PINS[sensor], HIGH);

  x = (int16_t)((xH << 8) | xL) >> 4;
  y = (int16_t)((yH << 8) | yL) >> 4;
  z = (int16_t)((zH << 8) | zL) >> 4;
}

static bool canWriteLine(size_t n) {
  return Serial.availableForWrite() >= (int)n;
}

// Unified stream format "ACC,<id>,x,y,z" for every sensor, where <id> is
// the sensor's index in CS_PINS. Host-side parsers key on the id field.
static void printSampleLine(uint8_t sensor, int16_t x, int16_t y, int16_t z) {
  if (!canWriteLine(40)) {
    g_accel.droppedFrames++;
    return;
  }

  Serial.print("ACC,");
  Serial.print(sensor);
  Serial.print(',');
  Serial.print(x);
  Serial.print(',');
  Serial.print(y);
  Serial.print(',');
  Serial.println(z);
}

static uint8_t detectedCount() {
  uint8_t n = 0;
  for (uint8_t s = 0; s < NUM_ACCEL; s++) {
    if (g_accel.detected[s]) n++;
  }
  return n;
}

static void printStatusLine() {
  if (!canWriteLine(80)) return;

  Serial.print("ACC STATUS detected=");
  Serial.print(detectedCount());
  Serial.print('/');
  Serial.print(NUM_ACCEL);
  Serial.print(" stream=");
  Serial.print(g_accel.streamEnabled ? 1 : 0);
  Serial.print(" interval_ms=");
  Serial.print(g_accel.intervalMs);
  Serial.print(" dropped=");
  Serial.println(g_accel.droppedFrames);
}

static void configureLIS3DH(uint8_t sensor) {
  // 1.344 kHz data rate (HR/normal mode) + XYZ enable - raised from
  // 400 Hz (0x77) in v2.9.0 so the delay experiment can stream at 1 ms
  // without duplicate samples (~0.74 ms sensor period).
  writeReg(sensor, REG_CTRL1, 0x97);
  // High-resolution mode, +/-2g, BDU on (0x88 sets bit7: the X/Y/Z output
  // registers only update between reads, preventing high/low byte tearing)
  writeReg(sensor, REG_CTRL4, 0x88);
}

// Probe one sensor; (re)configure it if it answers.
static bool detectSensor(uint8_t sensor) {
  uint8_t whoami = readReg(sensor, REG_WHO_AM_I);
  g_accel.detected[sensor] = (whoami == LIS3DH_ID);
  if (g_accel.detected[sensor]) configureLIS3DH(sensor);
  return g_accel.detected[sensor];
}

void accelInit() {
  pinMode(PIN_SCK, OUTPUT);
  pinMode(PIN_MOSI, OUTPUT);
  pinMode(PIN_MISO, INPUT_PULLUP);

  digitalWrite(PIN_SCK, HIGH);
  digitalWrite(PIN_MOSI, LOW);

  // All CS lines high (deselected) before any bus traffic, so a probe of
  // one sensor can't be answered by another.
  for (uint8_t s = 0; s < NUM_ACCEL; s++) {
    pinMode(CS_PINS[s], OUTPUT);
    digitalWrite(CS_PINS[s], HIGH);
  }

  delay(10);

  for (uint8_t s = 0; s < NUM_ACCEL; s++) {
    uint8_t whoami = readReg(s, REG_WHO_AM_I);
    g_accel.detected[s] = (whoami == LIS3DH_ID);
    if (g_accel.detected[s]) configureLIS3DH(s);

    if (canWriteLine(48)) {
      Serial.print("ACC INIT ");
      Serial.print(s);
      Serial.print(" WHOAMI=0x");
      Serial.println(whoami, HEX);
    }
  }

  g_accel.streamEnabled = false;
  g_accel.intervalMs = 10;
  g_accel.nextStreamMs = millis() + g_accel.intervalMs;
  g_accel.droppedFrames = 0;
}

void updateAccelerometer() {
  if (!g_accel.streamEnabled) return;

  uint32_t now = millis();
  if ((int32_t)(now - g_accel.nextStreamMs) < 0) return;

  // Catch up without drifting too much if loop jitter occurs.
  do {
    g_accel.nextStreamMs += g_accel.intervalMs;
  } while ((int32_t)(now - g_accel.nextStreamMs) >= 0);

  for (uint8_t s = 0; s < NUM_ACCEL; s++) {
    if (!g_accel.detected[s]) continue;

    int16_t x, y, z;
    readAccelRaw(s, x, y, z);
    printSampleLine(s, x, y, z);
  }
}

static char* skipSpacesLocal(char* p) {
  while (*p == ' ' || *p == '\t') p++;
  return p;
}

static bool startsWithToken(const char* s, const char* token) {
  while (*token) {
    char c1 = *s;
    char c2 = *token;
    if (c1 >= 'a' && c1 <= 'z') c1 = (char)(c1 - 'a' + 'A');
    if (c2 >= 'a' && c2 <= 'z') c2 = (char)(c2 - 'a' + 'A');
    if (c1 != c2) return false;
    s++;
    token++;
  }
  return (*s == '\0' || *s == ' ' || *s == '\t');
}

void handleAccelCommand(char* p) {
  p = skipSpacesLocal(p);

  if (!*p || startsWithToken(p, "HELP")) {
    if (canWriteLine(128)) {
      Serial.println("ACC CMDS: A HELP | A WHOAMI | A READ | A START [ms] | A STOP | A RATE ms | A STATUS");
    }
    return;
  }

  if (startsWithToken(p, "WHOAMI")) {
    for (uint8_t s = 0; s < NUM_ACCEL; s++) {
      uint8_t whoami = readReg(s, REG_WHO_AM_I);
      g_accel.detected[s] = (whoami == LIS3DH_ID);
      if (g_accel.detected[s]) configureLIS3DH(s);

      if (canWriteLine(48)) {
        Serial.print("ACC WHOAMI ");
        Serial.print(s);
        Serial.print(" 0x");
        Serial.println(whoami, HEX);
      }
    }
    return;
  }

  if (startsWithToken(p, "READ")) {
    bool any = false;

    for (uint8_t s = 0; s < NUM_ACCEL; s++) {
      if (!g_accel.detected[s] && !detectSensor(s)) continue;

      any = true;
      int16_t x, y, z;
      readAccelRaw(s, x, y, z);
      printSampleLine(s, x, y, z);
    }

    if (!any && canWriteLine(24)) Serial.println("ACC ERROR NOT_FOUND");
    return;
  }

  if (startsWithToken(p, "START")) {
    uint32_t interval = g_accel.intervalMs;
    char* arg = p + 5;
    arg = skipSpacesLocal(arg);
    if (*arg) {
      long tmp = -1;
      if (sscanf(arg, "%ld", &tmp) == 1 && tmp >= 1 && tmp <= 1000) {
        interval = (uint32_t)tmp;
      }
    }

    for (uint8_t s = 0; s < NUM_ACCEL; s++) {
      if (!g_accel.detected[s]) detectSensor(s);
    }

    if (detectedCount() == 0) {
      if (canWriteLine(24)) Serial.println("ACC ERROR NOT_FOUND");
      return;
    }

    g_accel.intervalMs = interval;
    g_accel.streamEnabled = true;
    g_accel.nextStreamMs = millis() + g_accel.intervalMs;

    if (canWriteLine(40)) {
      Serial.print("ACC START ");
      Serial.println(g_accel.intervalMs);
    }
    return;
  }

  if (startsWithToken(p, "STOP")) {
    g_accel.streamEnabled = false;
    if (canWriteLine(16)) Serial.println("ACC STOP");
    return;
  }

  if (startsWithToken(p, "RATE")) {
    long interval = -1;
    char* arg = p + 4;
    arg = skipSpacesLocal(arg);
    if (sscanf(arg, "%ld", &interval) == 1 && interval >= 1 && interval <= 1000) {
      g_accel.intervalMs = (uint32_t)interval;
      g_accel.nextStreamMs = millis() + g_accel.intervalMs;
      if (canWriteLine(40)) {
        Serial.print("ACC RATE ");
        Serial.println(g_accel.intervalMs);
      }
    } else {
      if (canWriteLine(24)) Serial.println("ACC ERROR BAD_RATE");
    }
    return;
  }

  if (startsWithToken(p, "STATUS")) {
    printStatusLine();
    return;
  }

  if (canWriteLine(24)) Serial.println("ACC ERROR BAD_CMD");
}
