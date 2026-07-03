#ifndef LED_ARRAY_H
#define LED_ARRAY_H

#include <Arduino.h>

void ledInit();
void updateLEDs();
void handleLEDSet(char* p);
void handleLEDGlobalBrightness(char* p);
void handleLEDClear(char* p);
void handleLEDShowNow();

#endif
