"""Synthetic exported-event fixtures shared by the finger_* test files.

One generator with knobs for the three things the analyses have to tell
apart:

  uniform benefit          every finger gains the same constant -> the
                           NULL for the compensation and weakest-finger
                           questions (a positive result there is an
                           artefact)
  compensatory benefit     the gain grows with the finger's own baseline
                           -> the effect those analyses should recover
  proportional speed-up    every RT multiplied by one constant -> the
                           NULL for the equalisation question (SD and
                           range shrink by that factor, CV does not)

Keeping the generator in one place means a test that claims "the naive
estimator is biased" and a test that claims "the split-half estimator is
not" are demonstrably talking about the same data-generating process.
"""

import random
from typing import List, Optional

FINGERS = [1, 2, 3, 4, 5]
HANDS = ["L", "R"]

# Baseline RT (s) added per finger ID: thumb fastest, little slowest, so
# "the weakest finger" is a real property and not just noise.
FINGER_OFFSET = {1: 0.00, 2: 0.02, 3: 0.05, 4: 0.09, 5: 0.14}


def make_events(n_participants: int = 7,
                trials_per_condition: int = 6,
                events_per_trial: int = 20,
                base: float = 0.95,
                cond_shift: float = -0.25,
                comp_slope: float = 0.0,
                proportional: Optional[float] = None,
                finger_offset_scale: float = 1.0,
                noise: float = 0.12,
                accuracy: float = 0.93,
                seed: int = 1,
                conditions=("A", "B", "C")) -> List[dict]:
    """Exported event rows for n_participants.

    cond_shift    constant added to C's mean (negative = C faster)
    comp_slope    extra C benefit proportional to the finger's own
                  baseline excess over `base` (0 = uniform benefit)
    proportional  if set, C's mean is base_mean * proportional instead -
                  a pure multiplicative speed-up, overriding cond_shift
                  and comp_slope
    finger_offset_scale
                  0 removes every TRUE difference between the fingers, so
                  "this participant's weakest finger" becomes pure noise -
                  the null for the weakest-finger analysis
    noise         within-cell SD of a single event's RT (0 = noiseless,
                  which makes the algebraic properties exact)
    """
    rng = random.Random(seed)
    events = []
    for i in range(n_participants):
        participant = f"P{i:02d}"
        p_offset = rng.gauss(0, 0.06) if noise else 0.02 * i
        trial_index = 1
        for condition in conditions:
            for trial in range(trials_per_condition):
                for k in range(events_per_trial):
                    hand = HANDS[k % 2]
                    finger = FINGERS[(k // 2) % len(FINGERS)]
                    mu_baseline = (base + p_offset
                                   + finger_offset_scale * FINGER_OFFSET[finger])
                    if condition == "C":
                        if proportional is not None:
                            mu = mu_baseline * proportional
                        else:
                            extra = comp_slope * (mu_baseline - base)
                            mu = mu_baseline + cond_shift - extra
                    elif condition == "A":
                        mu = mu_baseline + 0.05
                    else:
                        mu = mu_baseline
                    rt = rng.gauss(mu, noise) if noise else mu
                    correct = rng.random() < accuracy if accuracy < 1 else True
                    events.append({
                        "participant": participant,
                        "trial_index": trial_index,
                        "condition": condition,
                        "level": ["alpha", "beta", "gamma"][trial % 3],
                        "event_index": k,
                        "target_finger": f"{hand}{finger}",
                        "target_hand": hand,
                        "timed_out": False,
                        "target_note": 60,
                        "actual_note": 60,
                        "key_correct": True,
                        "actual_finger": f"{hand}{finger}",
                        "finger_correct": bool(correct),
                        "target_finger_probability": 0.9 if correct else 0.1,
                        "rt_s": max(0.05, rt),
                        "validity": "valid",
                        "manually_corrected": False,
                    })
                trial_index += 1
    return events
