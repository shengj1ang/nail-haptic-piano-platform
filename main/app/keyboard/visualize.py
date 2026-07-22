"""Color a key_map onto a live frame with high-contrast, distinct colors."""

from typing import Dict, Optional

import cv2
import numpy as np

# Consecutive key ids are physically adjacent, so hues are spread with the
# golden angle rather than evenly divided - that keeps neighbours from ever
# landing on similar colors, regardless of how many keys there are.
GOLDEN_ANGLE = 137.508


def generate_colors(n: int):
    colors = []
    for i in range(n):
        hue = (i * GOLDEN_ANGLE) % 180  # OpenCV hue range is 0-179
        hsv = np.uint8([[[hue, 230, 255]]])
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
        colors.append(tuple(int(c) for c in bgr))
    return colors


def build_color_luts(num_keys: int):
    colors = generate_colors(num_keys)
    lut_b = np.zeros(256, dtype=np.uint8)
    lut_g = np.zeros(256, dtype=np.uint8)
    lut_r = np.zeros(256, dtype=np.uint8)

    for kid, (b, g, r) in enumerate(colors, start=1):
        lut_b[kid] = b
        lut_g[kid] = g
        lut_r[kid] = r

    return lut_b, lut_g, lut_r


def overlay_keys(frame: np.ndarray, key_map: np.ndarray, luts, alpha: float = 0.55) -> None:
    lut_b, lut_g, lut_r = luts
    color_map = cv2.merge([cv2.LUT(key_map, lut_b), cv2.LUT(key_map, lut_g), cv2.LUT(key_map, lut_r)])

    mask = key_map > 0
    blended = cv2.addWeighted(frame, 1 - alpha, color_map, alpha, 0)
    frame[mask] = blended[mask]


def draw_labels(
    frame: np.ndarray,
    key_map: np.ndarray,
    num_keys: int,
    label_map: Optional[Dict[int, str]] = None,
) -> None:
    """label_map, if given, maps a key_map pixel value (1-indexed) to the
    text to draw - e.g. a MIDI note name instead of the plain key number."""
    font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2

    for kid in range(1, num_keys + 1):
        ys, xs = np.where(key_map == kid)
        if len(xs) == 0:
            continue

        cx, cy = int(xs.mean()), int(ys.mean())
        label = label_map.get(kid, str(kid)) if label_map else str(kid)
        (tw, th), _ = cv2.getTextSize(label, font, scale, thickness)

        cv2.rectangle(
            frame,
            (cx - tw // 2 - 3, cy - th // 2 - 3),
            (cx + tw // 2 + 3, cy + th // 2 + 3),
            (0, 0, 0),
            -1,
        )
        cv2.putText(frame, label, (cx - tw // 2, cy + th // 2), font, scale, (255, 255, 255), thickness)
