#ifndef MOTOR_DRIVER_H
#define MOTOR_DRIVER_H

#include <Arduino.h>

void motorInit();
void updateMotors();
void stopAll();
void handlePulse(char* p);
void handleImmediate(char* p);
void handleFrequency(char* p);

#endif