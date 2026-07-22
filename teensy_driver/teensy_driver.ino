#include <Arduino.h>
#include "motor_driver.h"
#include "LED_array.h"
#include "accel_driver.h"

#define FW_NAME "haptic-piano"
#define FW_VERSION "v2.9.0"

// ===== serial buffer =====
static char lineBuf[128];
static uint8_t lineLen = 0;
static bool overflow = false;

// Skip spaces/tabs at the beginning
static char* skipSpaces(char* p) {
  while (*p == ' ' || *p == '\t') p++;
  return p;
}

// Read one full command line from Serial
static bool readLine() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\r') continue;

    if (c == '\n') {
      if (overflow) {
        overflow = false;
        lineLen = 0;
        return false;
      }
      lineBuf[lineLen] = '\0';
      lineLen = 0;
      return true;
    }

    if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      overflow = true;
    }
  }
  return false;
}

// Return firmware identity: "E <name> <version>". The device name makes
// the reply distinctive enough to double as a host-side auto-detect probe.
static void handleEcho() {
  Serial.print("E ");
  Serial.print(FW_NAME);
  Serial.print(' ');
  Serial.println(FW_VERSION);
}

void setup() {
  Serial.begin(115200);

  motorInit();
  ledInit();
  accelInit();
}

void loop() {
  // Check and dispatch any already-arrived command first, so it isn't
  // stuck behind the (potentially blocking) subsystem updates below.
  if (readLine()) {
    char* p = skipSpaces(lineBuf);

    if (*p) {
      char cmd = *p++;
      p = skipSpaces(p);

      // ===== motor =====
      if (cmd == 'X') {
        stopAll();
      } else if (cmd == 'E') {
        handleEcho();
      } else if (cmd == 'P') {
        handlePulse(p);
      } else if (cmd == 'S') {
        handleImmediate(p);

      // F idx freq (set PWM frequency on motor pin idx 0-11, -1 = all)
      } else if (cmd == 'F') {
        handleFrequency(p);

      // ===== accelerometer =====
      // A HELP | A WHOAMI | A READ | A START [interval_ms] | A STOP | A RATE interval_ms | A STATUS
      } else if (cmd == 'A') {
        handleAccelCommand(p);

      // ===== LED =====
      // L strip idx r g b brightness
      } else if (cmd == 'L') {
        handleLEDSet(p);

      // B brightness
      } else if (cmd == 'B') {
        handleLEDGlobalBrightness(p);

      // C strip (strip = 0 / 1 / -1(all))
      } else if (cmd == 'C') {
        handleLEDClear(p);

      // U (force LED output immediately)
      } else if (cmd == 'U') {
        handleLEDShowNow();
      }
    }
  }

  // Non-blocking updates (accel SPI read still briefly blocks, so these run
  // after command dispatch, not before it).
  updateMotors();
  updateLEDs();
  updateAccelerometer();
}