#pip install pynput



import time
from pynput import keyboard
from controller import VibratorController

# Key mapping
KEY_MAP = {
    'a': 0,
    's': 1,
    'd': 2,
    'f': 3,
    'g': 4,
    'h': 5,
    'j': 6,
    'k': 7,
    'l': 8,
    ';': 9,
    'x':10,
    'v':11,
}

AMP = 80

current_mask = 0
vc = None


def update_output():
    """
    Send current mask to Arduino.
    """
    global current_mask
    vc.send(f"S {current_mask} {AMP}")


def on_press(key):
    global current_mask

    try:
        k = key.char.lower()
    except:
        return

    if k in KEY_MAP:
        idx = KEY_MAP[k]
        current_mask |= (1 << idx)
        update_output()


def on_release(key):
    global current_mask

    try:
        k = key.char.lower()
    except:
        # ESC to exit
        if key == keyboard.Key.esc:
            return False
        return

    if k in KEY_MAP:
        idx = KEY_MAP[k]
        current_mask &= ~(1 << idx)
        update_output()


def main():
    global vc

    with VibratorController() as controller:
        vc = controller

        print("Device:", vc.echo())
        print("Press ASDFGHJKL; to control motors")
        print("Press ESC to exit")

        # Ensure everything is off initially
        vc.stop_all()

        listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release
        )

        listener.start()
        listener.join()

        vc.stop_all()
        print("Exit.")


if __name__ == "__main__":
    main()