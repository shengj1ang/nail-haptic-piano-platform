"""Quick manual motor check: motor port 0, PWM frequency 300 Hz, AMP=0,
driven steadily for 10 seconds so you can watch whether the motor moves.

Sends the raw rig commands over the shared serial protocol:
    F 0 300     set motor port 0's PWM carrier to 300 Hz
    S 1 0       drive the motors in bitmask 1 (= 1 << 0, port 0) at amp 0
    X           stop all motors

Note: AMP=0 means zero drive (0% PWM duty), so under normal firmware the
motor gets no voltage and should NOT move - this run mainly confirms that.
Change AMP below if you want to see it actually spin.

On exit (including Ctrl+C) it stops the motor and restores the firmware's
224 Hz boot-default PWM frequency on the port.

Run:  python test_motor_port0_freq300_amp0.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.controller import VibratorController

# ---- parameters -------------------------------------------------------
MOTOR_PORT = 0        # motor port to drive
PWM_HZ = 300          # PWM carrier frequency (Hz)
AMP = 0               # drive amplitude, 0-255 (0 = no drive)
DURATION_S = 10       # how long to hold the drive
DEFAULT_PWM_HZ = 224  # firmware boot default, restored on exit
# -----------------------------------------------------------------------


def main() -> None:
    mask = 1 << MOTOR_PORT
    with VibratorController() as vc:
        print("Device:", vc.echo())
        vc.send("X")                          # safety stop
        time.sleep(0.05)

        print(f"Set port {MOTOR_PORT} PWM frequency -> {PWM_HZ} Hz")
        vc.send(f"F {MOTOR_PORT} {PWM_HZ}")
        time.sleep(0.05)

        try:
            print(f"Drive port {MOTOR_PORT} at amp={AMP} for {DURATION_S} s "
                  "- watch the motor...")
            vc.send(f"S {mask} {AMP}")
            time.sleep(DURATION_S)
        finally:
            vc.send("X")                      # stop
            vc.send(f"F {MOTOR_PORT} {DEFAULT_PWM_HZ}")  # restore default freq
            print(f"Stopped. PWM frequency on port {MOTOR_PORT} restored to "
                  f"{DEFAULT_PWM_HZ} Hz.")


if __name__ == "__main__":
    main()
