#include <FastLED.h>

#define LED_PIN 23
#define NUM_LEDS 60

CRGB leds[NUM_LEDS];

void setup() {
  FastLED.addLeds<WS2812, LED_PIN, GRB>(leds, NUM_LEDS);

  FastLED.setBrightness(10);   // 调低亮度
}

void loop() {

  for(int i=0;i<NUM_LEDS;i++){
    leds[i] = CRGB::White;
  }

  FastLED.show();
}