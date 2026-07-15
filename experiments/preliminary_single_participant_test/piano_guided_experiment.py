
import time
import random
import mido
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

from controller import VibratorController
from led_controller import LEDArrayController


# ============================================================
# 1) MIDI / key definitions
# ============================================================
ALLOWED_NOTES = [ 50, 52, 54, 55, 57, 59, 61, 62, 64, 66, 67, 69, 71, 73, 74]

LED_MAP: Dict[int, Tuple[int, int]] = {
    note: (0, i) for i, note in enumerate(ALLOWED_NOTES)
}

# 用 5 个震动口对应右手五指。这里采用“相对位置”而不是“绝对音高 -> 手指”。
# 也就是每一步先决定一个 5 键窗口，再把窗口里的第 0..4 个键映射给震动 0..4。
FINGER_PORTS = [0, 1, 2, 3, 4]
AMP = 64

MIDI_PORT_CANDIDATES = ["SE25 MIDI1", "SE25 MIDI2"]


# ============================================================
# 2) sequence generation
# ============================================================

@dataclass
class Prompt:
    note: int
    finger_idx: int          # 0~4
    window_notes: List[int]  # 当前五指窗口


def generate_random_sequence(
    total_steps: int = 20,
    span: int = 5,
    seed: Optional[int] = None,
) -> List[Prompt]:
    """
    随机生成练习序列。
    - span=5 表示每一步都在一个连续的 5 个白键窗口内选一个目标音
    - finger_idx 直接对应窗口内位置 => 右手五指
    """
    rng = random.Random(seed)
    prompts: List[Prompt] = []

    # 在 15 个白键中选一个 5 键窗口
    max_start = len(ALLOWED_NOTES) - span
    current_start = rng.randint(0, max_start)

    prev_note = None
    for _ in range(total_steps):
        # 窗口小范围游走，避免跨度太大
        current_start += rng.choice([-1, 0, 1])
        current_start = max(0, min(max_start, current_start))

        window = ALLOWED_NOTES[current_start: current_start + span]

        # 尽量避免连续重复同一个音
        candidates = [n for n in window if n != prev_note] or window
        note = rng.choice(candidates)
        finger_idx = window.index(note)

        prompts.append(Prompt(note=note, finger_idx=finger_idx, window_notes=window))
        prev_note = note

    return prompts


def generate_scale_sequence() -> List[Prompt]:
    """
    一个非常容易实现、也很适合最初实验的固定序列：
    5 个白键上行再下行。
    """
    window = [60, 62, 64, 65, 67]  # C D E F G
    up = [Prompt(n, i, window) for i, n in enumerate(window)]
    down = [Prompt(n, i, window) for i, n in list(enumerate(window))[::-1][1:-1]]
    return up + down


# ============================================================
# 3) hardware helpers
# ============================================================

def find_midi_port() -> str:
    names = mido.get_input_names()
    for cand in MIDI_PORT_CANDIDATES:
        if cand in names:
            return cand
    if names:
        return names[0]
    raise RuntimeError("No MIDI input port found.")


class Vibro:
    def __init__(self, controller: VibratorController):
        self.vc = controller

    def set_finger(self, finger_idx: int):
        mask = 1 << FINGER_PORTS[finger_idx]
        self.vc.send(f"S {mask} {AMP}")

    def off(self):
        self.vc.stop_all()


class LEDs:
    def __init__(self, controller: LEDArrayController):
        self.led = controller

    def show_note(self, note: int):
        self.led.off()
        if note not in LED_MAP:
            return
        strip, pixel = LED_MAP[note]
        self.led.set_pixel(strip, pixel, 0, 255, 0, 120)
        self.led.set_global_brightness(100)

    def off(self):
        self.led.off()


# ============================================================
# 4) experiment loop
# ============================================================

def wait_for_target_note(inport, target_note: int):
    """
    只把 velocity>0 的 note_on 当作“按下”。
    velocity=0 的 note_on 在很多设备里其实等价于 note_off。
    """
    t0 = time.perf_counter()
    while True:
        msg = inport.receive()
        if msg.type == "note_on":
            if getattr(msg, "velocity", 0) <= 0:
                continue
            note = getattr(msg, "note", None)
            if note == target_note:
                rt = time.perf_counter() - t0
                return {
                    "correct": True,
                    "rt_sec": rt,
                    "pressed_note": note,
                }
            elif note in ALLOWED_NOTES:
                rt = time.perf_counter() - t0
                return {
                    "correct": False,
                    "rt_sec": rt,
                    "pressed_note": note,
                }


def run_experiment(
    mode: str = "random",      # "random" or "scale"
    total_steps: int = 20,
    cue_mode: str = "both",    # "led", "vib", "both"
    seed: int = 7,
):
    if mode == "scale":
        sequence = generate_scale_sequence()
    else:
        sequence = generate_random_sequence(total_steps=total_steps, seed=seed)

    midi_port = find_midi_port()
    print(f"Using MIDI input: {midi_port}")

    with mido.open_input(midi_port) as inport, \
         VibratorController() as vc, \
         LEDArrayController() as led_ctrl:

        vib = Vibro(vc)
        leds = LEDs(led_ctrl)

        print("Vibrator device:", vc.echo())
        vc.stop_all()
        led_ctrl.off()

        input("Press Enter to start...")

        results = []

        for i, prompt in enumerate(sequence, start=1):
            print(f"\nStep {i}/{len(sequence)}")
            print(f"Target note = {prompt.note}, finger = {prompt.finger_idx}, window = {prompt.window_notes}")

            if cue_mode in ("led", "both"):
                leds.show_note(prompt.note)
            if cue_mode in ("vib", "both"):
                vib.set_finger(prompt.finger_idx)

            result = wait_for_target_note(inport, prompt.note)

            leds.off()
            vib.off()

            result["target_note"] = prompt.note
            result["finger_idx"] = prompt.finger_idx
            result["window_notes"] = prompt.window_notes
            results.append(result)

            print(
                f"Pressed={result['pressed_note']} | "
                f"correct={result['correct']} | "
                f"RT={result['rt_sec']:.3f}s"
            )

            time.sleep(0.3)

        # summary
        n = len(results)
        correct_n = sum(r["correct"] for r in results)
        mean_rt = sum(r["rt_sec"] for r in results) / n if n else 0.0

        print("\n================ SUMMARY ================")
        print(f"Trials: {n}")
        print(f"Accuracy: {correct_n}/{n} = {correct_n / n:.1%}")
        print(f"Mean RT: {mean_rt:.3f}s")
        print("========================================")


if __name__ == "__main__":
    run_experiment(
        mode="random",
        total_steps=20,
        cue_mode="both",   # "led", "vib", "both"
        seed=7,
    )
