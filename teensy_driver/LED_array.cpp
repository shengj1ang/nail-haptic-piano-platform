#include "LED_array.h"
#include <FastLED.h>       // only used for CRGB storage + color scaling math
#include <WS2812Serial.h>  // non-blocking driver: hardware UART + DMA, no cli()/sei()

// ===== LED config =====
// WS2812Serial only works on specific hardware serial TX pins (Teensy 4.1:
// 1, 8, 14, 17, 20, 24, 29, 35, 47, 53). Pins 1/8/14 are taken by the motor
// PWM outputs and pin 35 by the accelerometer SPI (MISO), so the strips were
// moved off the old FastLED pins (22/23) onto 24/29.
#define LED_PIN_0 24  // was pin 23 under FastLED
#define LED_PIN_1 29  // was pin 22 under FastLED

#define NUM_LEDS_0 60
#define NUM_LEDS_1 60

#define COLOR_ORDER WS2812_GRB

// Logical colors, already including per-pixel brightness (set via 'L').
// Global brightness ('B') is applied by the WS2812Serial driver itself
// (via setBrightness()) when it copies these into the DMA frame buffer.
static CRGB leds0[NUM_LEDS_0];
static CRGB leds1[NUM_LEDS_1];

static byte drawMem0[NUM_LEDS_0 * 3];
static byte drawMem1[NUM_LEDS_1 * 3];
DMAMEM static byte dispMem0[NUM_LEDS_0 * 12];
DMAMEM static byte dispMem1[NUM_LEDS_1 * 12];

static WS2812Serial strip0(NUM_LEDS_0, dispMem0, drawMem0, LED_PIN_0, COLOR_ORDER);
static WS2812Serial strip1(NUM_LEDS_1, dispMem1, drawMem1, LED_PIN_1, COLOR_ORDER);

// Global brightness for both strips
static uint8_t g_ledBrightness = 255;

// Only push to hardware when data changed
static bool ledDirty = false;

// Return strip pointer by strip ID
static CRGB* getStrip(uint8_t stripId, uint16_t &stripLen) {
  if (stripId == 0) {
    stripLen = NUM_LEDS_0;
    return leds0;
  }

  if (stripId == 1) {
    stripLen = NUM_LEDS_1;
    return leds1;
  }

  stripLen = 0;
  return nullptr;
}

// Clear one strip or all strips
static void clearStrip(int stripId) {
  if (stripId == 0 || stripId == -1) {
    for (int i = 0; i < NUM_LEDS_0; i++) {
      leds0[i] = CRGB::Black;
    }
  }

  if (stripId == 1 || stripId == -1) {
    for (int i = 0; i < NUM_LEDS_1; i++) {
      leds1[i] = CRGB::Black;
    }
  }

  ledDirty = true;
}

// Push logical colors to one strip. show() itself waits (via yield(), not
// cli()/sei()) for any prior DMA transfer on that strip to finish before
// touching the buffer, so it's always safe to call directly - no external
// busy check needed (and this library build has no busy() to call anyway).
static void pushStrip(WS2812Serial &out, CRGB* src, uint16_t len) {
  for (uint16_t i = 0; i < len; i++) {
    CRGB c = src[i];
    out.setPixel(i, c.r, c.g, c.b);
  }
  out.show();
}

// Init LED strips
void ledInit() {
  strip0.begin();
  strip1.begin();
  strip0.setBrightness(g_ledBrightness);
  strip1.setBrightness(g_ledBrightness);

  for (int i = 0; i < NUM_LEDS_0; i++) leds0[i] = CRGB::Black;
  for (int i = 0; i < NUM_LEDS_1; i++) leds1[i] = CRGB::Black;

  pushStrip(strip0, leds0, NUM_LEDS_0);
  pushStrip(strip1, leds1, NUM_LEDS_1);
  ledDirty = false;
}

// Update LEDs only when needed
void updateLEDs() {
  if (!ledDirty) return;

  pushStrip(strip0, leds0, NUM_LEDS_0);
  pushStrip(strip1, leds1, NUM_LEDS_1);
  ledDirty = false;
}

// Parse command: L strip idx r g b brightness
void handleLEDSet(char* p) {
  long strip, idx, r, g, b, brightness;

  if (sscanf(p, "%ld %ld %ld %ld %ld %ld",
             &strip, &idx, &r, &g, &b, &brightness) == 6) {

    if (strip < 0 || strip > 1) return;
    if (r < 0 || r > 255) return;
    if (g < 0 || g > 255) return;
    if (b < 0 || b > 255) return;
    if (brightness < 0 || brightness > 255) return;

    uint16_t stripLen = 0;
    CRGB* target = getStrip((uint8_t)strip, stripLen);
    if (target == nullptr) return;
    if (idx < 0 || idx >= stripLen) return;

    CRGB color((uint8_t)r, (uint8_t)g, (uint8_t)b);
    color.nscale8_video((uint8_t)brightness);  // per-pixel brightness

    target[idx] = color;
    ledDirty = true;
  }
}

// Parse command: B brightness
void handleLEDGlobalBrightness(char* p) {
  long b;
  if (sscanf(p, "%ld", &b) == 1) {
    if (b >= 0 && b <= 255) {
      g_ledBrightness = (uint8_t)b;
      strip0.setBrightness(g_ledBrightness);
      strip1.setBrightness(g_ledBrightness);
      ledDirty = true;
    }
  }
}

// Parse command: C strip
void handleLEDClear(char* p) {
  long strip;
  if (sscanf(p, "%ld", &strip) == 1) {
    if (strip == 0 || strip == 1 || strip == -1) {
      clearStrip((int)strip);
    }
  }
}

// Force output immediately
void handleLEDShowNow() {
  pushStrip(strip0, leds0, NUM_LEDS_0);
  pushStrip(strip1, leds1, NUM_LEDS_1);
  ledDirty = false;
}
