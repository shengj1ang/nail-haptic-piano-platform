import time
from controller import VibratorController


def main():
    with VibratorController() as vc:
        # Query firmware version
        print("Device:", vc.echo())

        print("\n--- Async Demo Start ---\n")

        # Step 1:
        # Start a long triple pulse on motor 0
        # This will run independently inside the Arduino
        print("Motor 0: triple pulse (long)")
        vc.pulse(0, count=3, amp=80, on_ms=200, off_ms=200)

        # Do NOT wait for it to finish
        time.sleep(0.1)

        # Step 2:
        # Inject another motor while motor 0 is still running
        print("Motor 3: injected single pulse")
        vc.pulse(3, count=1, amp=80, on_ms=120, off_ms=0)

        # Short delay before next injection
        time.sleep(0.1)

        # Step 3:
        # Start another independent pattern
        print("Motor 5: double pulse")
        vc.pulse(5, count=2, amp=80, on_ms=150, off_ms=150)

        # At this point:
        # All motors are running independently on the MCU
        print("\nAll tasks sent. Motors running independently...\n")

        # Let everything finish
        time.sleep(3)

        # Safety stop
        vc.stop_all()
        print("Done.")


if __name__ == "__main__":
    main()