// Minimal Vibration Driver (non-blocking, no patterns)
// Commands:
//   F f1 f2 f3    -> set PWM frequencies (Hz)
//   S mask amp    -> start vibration for selected motors
//   X             -> stop all
// Replies:
//   OK ...
//   ERR ...

#include <Arduino.h>

static const uint8_t PWM_PINS[3] = {2, 4, 6};
static const uint8_t DIR_PINS[3] = {3, 5, 255}; // motor3 no dir in your snippet

static char lineBuf[96];
static uint8_t lineLen = 0;
static bool overflow = false;

static inline void applyMask(uint8_t mask, uint8_t amp) {
  for (uint8_t i = 0; i < 3; i++) {
    if (mask & (1u << i)) analogWrite(PWM_PINS[i], amp);
    else analogWrite(PWM_PINS[i], 0);
  }
}

static inline void stopAll() {
  applyMask(0, 0);
}

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
      overflow = true; // discard until newline
    }
  }
  return false;
}

static void replyOK(const char* msg) {
  Serial.print("OK ");
  Serial.println(msg);
}

static void replyERR(const char* msg) {
  Serial.print("ERR ");
  Serial.println(msg);
}

void setup() {
  Serial.begin(115200);

  for (uint8_t i = 0; i < 3; i++) {
    pinMode(PWM_PINS[i], OUTPUT);
    analogWrite(PWM_PINS[i], 0);
  }
  if (DIR_PINS[0] != 255) { pinMode(DIR_PINS[0], OUTPUT); digitalWrite(DIR_PINS[0], HIGH); }
  if (DIR_PINS[1] != 255) { pinMode(DIR_PINS[1], OUTPUT); digitalWrite(DIR_PINS[1], HIGH); }

  stopAll();
  replyOK("boot");
}

void loop() {
  // Non-blocking: only parse when a full line arrives
  if (!readLine()) return;

  // Skip leading spaces
  char* p = lineBuf;
  while (*p == ' ' || *p == '\t') p++;
  if (!*p) return;

  // Command letter
  char cmd = *p++;

  // ---- STOP ----
  if (cmd == 'X') {
    stopAll();
    replyOK("stop");
    return;
  }

  // ---- SET FREQ ----
  if (cmd == 'F') {
    long f1, f2, f3;
    int n = sscanf(p, "%ld %ld %ld", &f1, &f2, &f3);
    if (n != 3 || f1 <= 0 || f2 <= 0 || f3 <= 0) {
      replyERR("bad F");
      return;
    }

    // set PWM frequency if supported by core
    #if defined(TEENSYDUINO)
      analogWriteFrequency(PWM_PINS[0], (float)f1);
      analogWriteFrequency(PWM_PINS[1], (float)f2);
      analogWriteFrequency(PWM_PINS[2], (float)f3);
      replyOK("freq");
    #else
      // On classic Arduino, analogWriteFrequency isn't available.
      // We accept command for protocol compatibility.
      replyOK("freq_ignored");
    #endif
    return;
  }

  // ---- START ----
  if (cmd == 'S') {
    long mask, amp;
    int n = sscanf(p, "%ld %ld", &mask, &amp);
    if (n != 2 || mask < 0 || mask > 7 || amp < 0 || amp > 255) {
      replyERR("bad S");
      return;
    }
    applyMask((uint8_t)mask, (uint8_t)amp);
    replyOK("start");
    return;
  }

  replyERR("unknown");
}