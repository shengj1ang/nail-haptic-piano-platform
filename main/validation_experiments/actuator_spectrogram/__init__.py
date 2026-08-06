"""Actuator spectrogram: a 2-D drive-frequency x amp vibration-intensity
map for either an ERM or an LRA. For every PWM drive frequency and every
amp (both over user-adjustable ranges, seeded from the type - ERM freq
50-5000 Hz, LRA freq 0-350 Hz, both amp 0-255) it drives the motor at that
combination, measures the accelerometer RMS intensity and fills a grid
box with it, coloured darker where stronger. See README.md and
actuator_spectrogram.py.
"""
