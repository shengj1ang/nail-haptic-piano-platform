import os
import sys
import time
import random
import mido
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

# ============================================================
# FIX: ensure local modules can be imported
# ============================================================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.append(CURRENT_DIR)

# Now safe to import
from controller import VibratorController
from led_controller import LEDArrayController


# ============================================================
# 1) MIDI / key definitions
# ============================================================
ALLOWED_NOTES = [50, 52, 54, 55, 57, 59, 61, 62, 64, 66, 67, 69, 71, 73, 74]

LED_MAP: Dict[int, Tuple[int, int]] = {
    note: (0, i) for i, note in enumerate(ALLOWED_NOTES)
}

FINGER_PORTS = [0, 1, 2, 3, 4]
AMP = 64

MIDI_PORT_CANDIDATES = ["SE25 MIDI1", "SE25 MIDI2"]


# ============================================================
# 2) sequence generation
# ============================================================

@dataclass
class Prompt:
    note: int
    finger_idx: int
    window_notes: List[int]


def generate_random_sequence(total_steps=20, span=5, seed=None):
    rng = random.Random(seed)
    prompts = []

    max_start = len(ALLOWED_NOTES) - span
    current_start = rng.randint(0, max_start)

    prev_note = None
    for _ in range(total_steps):
        current_start += rng.choice([-1, 0, 1])
        current_start = max(0, min(max_start, current_start))

        window = ALLOWED_NOTES[current_start: current_start + span]
        candidates = [n for n in window if n != prev_note] or window

        note = rng.choice(candidates)
        finger_idx = window.index(note)

        prompts.append(Prompt(note, finger_idx, window))
        prev_note = note

    return prompts


# ============================================================
# 3) hardware helpers
# ============================================================

def find_midi_port():
    names = mido.get_input_names()
    for cand in MIDI_PORT_CANDIDATES:
        if cand in names:
            return cand
    if names:
        return names[0]
    raise RuntimeError("No MIDI input port found.")


class Vibro:
    def __init__(self, controller):
        self.vc = controller

    def set_finger(self, finger_idx):
        mask = 1 << FINGER_PORTS[finger_idx]
        self.vc.send(f"S {mask} {AMP}")

    def off(self):
        self.vc.stop_all()


class LEDs:
    def __init__(self, controller):
        self.led = controller

    def show_note(self, note):
        self.led.off()
        if note not in LED_MAP:
            return
        strip, pixel = LED_MAP[note]
        self.led.set_pixel(strip, pixel, 0, 255, 0, 120)

    def off(self):
        self.led.off()


# ============================================================
# 4) input handling
# ============================================================

def wait_for_target_note(inport, target_note):
    t0 = time.perf_counter()

    while True:
        msg = inport.receive()

        if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
            note = msg.note
            rt = time.perf_counter() - t0

            return {
                "correct": note == target_note,
                "rt_sec": rt,
                "pressed_note": note,
            }


# ============================================================
# 5) experiment loop
# ============================================================

def run_experiment(
    total_steps=20,
    cue_mode="both",   # "led", "vib", "both", "visual"
    seed=7,
):
    sequence = generate_random_sequence(total_steps=total_steps, seed=seed)

    midi_port = find_midi_port()
    print(f"Using MIDI input: {midi_port}")

    with mido.open_input(midi_port) as inport:

        # Optional hardware
        vc = None
        led_ctrl = None

        if cue_mode in ("vib", "both"):
            vc = VibratorController()
            vc.__enter__()

        if cue_mode in ("led", "both", "visual"):
            led_ctrl = LEDArrayController()
            led_ctrl.__enter__()

        vib = Vibro(vc) if vc else None
        leds = LEDs(led_ctrl) if led_ctrl else None

        input("Press Enter to start...")

        results = []

        for i, prompt in enumerate(sequence, start=1):
            print(f"\nStep {i}/{len(sequence)}")

            # 👉 关键：visual 模式（你要的）
            if cue_mode == "visual":
                print(f">>> Finger: {prompt.finger_idx+1}")

            else:
                print(f"Target note = {prompt.note}, finger = {prompt.finger_idx}")

            # LED
            if leds:
                leds.show_note(prompt.note)

            # Vib
            if vib:
                vib.set_finger(prompt.finger_idx)

            result = wait_for_target_note(inport, prompt.note)

            if leds:
                leds.off()
            if vib:
                vib.off()

            results.append(result)

            print(
                f"Pressed={result['pressed_note']} | "
                f"correct={result['correct']} | "
                f"RT={result['rt_sec']:.3f}s"
            )

            time.sleep(0.3)

        # cleanup
        if vc:
            vc.__exit__(None, None, None)
        if led_ctrl:
            led_ctrl.__exit__(None, None, None)

        # summary
        n = len(results)
        acc = sum(r["correct"] for r in results) / n
        rt = sum(r["rt_sec"] for r in results) / n

        print("\n================ SUMMARY ================")
        print(f"Trials: {n}")
        print(f"Accuracy: {acc:.1%}")
        print(f"Mean RT: {rt:.3f}s")
        print("========================================")


if __name__ == "__main__":
    run_experiment(
        total_steps=20,
        cue_mode="visual",   # ⭐ 你要的模式
        seed=7,
    )