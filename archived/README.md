# archived - superseded prototypes and completed one-off studies

Everything in this folder is **kept for reference only**: either an
early prototype whose functionality now lives in `teensy_driver/` /
`main/`, or a completed validation study whose data and
analysis are cited by the final report and must stay unchanged. Do not
build new work on top of these.

Repeatable calibration/validation experiments live in
`main/validation_experiments/` (with GUI entries in launcher
section 9).

## Early firmware / bring-up sketches

| Folder | What it was |
|---|---|
| `hello_world/` | First board bring-up (blink + serial). |
| `serial-test/` | USB serial link echo/LED test. |
| `init_virb_run/` | First motor spin-up test - fixed PWM pins, no serial protocol. |
| `sketch_feb1a/` | Early motor timing experiment on the same fixed-pin setup. |
| `driver-vib/` | First standalone vibration-driver firmware (F/S/X serial commands) plus host test scripts - the direct ancestor of `teensy_driver/motor_driver.cpp`. |
| `LED-array/` | First standalone WS2812 strip sketch - superseded by `teensy_driver/LED_array.cpp`. |
| `find_right_accelerometer_port/` | Brute-force pin search that established the LIS3DH SPI wiring (SCK 33 / MOSI 34 / MISO 35 / CS 36) now hard-coded in `teensy_driver/accel_driver.cpp`. |

The related `read_data_from_accelerometer/` (old standalone LIS3DH
sketch + plotters) was moved to
`main/read_data_from_accelerometer/` as a documented legacy
reference - see its README.

## Early host-side tooling

| Folder | What it was |
|---|---|
| `statistics/` | Early vibration logging (`test_log.db`) and Jupyter plotting of serial data. |
| `bug-report/` | Old bug reports from the python-code era. |
| `python-prototypes/` | The old `python-code/archive/`: superseded FingerAccuracy detector prototypes (`detect_finger_with_MIDI_keyboard_v2/v3/v4.py` with their pickled models and MediaPipe task file), one-off utilities (`migrate_to_epoch_timestamps.py`, `select_camera.py`, `test_frame_rate.py`). |

## Superseded experiment prototypes and completed studies

| Folder | What it was |
|---|---|
| `preliminary_single_participant_test/` | Earliest guided cue-response prototype (LED window + five finger motors + MIDI, single participant). Fully superseded by the quiz pipeline (`main/student_quiz.py`, `student_quiz_haptic.py`) and the Formal Experiment Session runner. |
| `human_reaction_experiment/` | **Completed** LRA-vs-ERM human validation study (five participants: reaction time + subjective ratings). Its outcome - the LRA selection - is locked and reported in the final report; `reaction_experiment.db` and `analysis_output/analysis_report.md` are the archival record and must not be modified or regenerated. |
| `attachment_verification_experiement/` | **Completed** nail-attachment validation (double-sided tape vs Blu Tack vs eyelash glue, on both actuators): vibration transmission (peak/RMS), onset delay, and a fall-off reliability test. Its outcome - Blu Tack as the nail coupler, with its lower transmission treated as beneficial mild damping - is locked and reported in the final report; the `.db` and `attachment_analysis_output/` are the archival record. |
