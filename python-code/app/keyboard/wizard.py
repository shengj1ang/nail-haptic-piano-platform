"""Paint-bucket style manual key segmentation.

The automatic Hough-line approach struggles with real camera distortion, so
key boundaries are instead marked by hand: click once inside a key and the
connected region floods outward until it hits a detected edge or the outer
boundary rectangle, the same way a paint bucket tool works. Each key's exact
filled pixels are kept (see build_key_map) rather than reduced to a box.
"""

from typing import List, Optional, Tuple

import cv2
import numpy as np

from .template import KeyBox

Rect = Tuple[int, int, int, int]  # x, y, w, h


class KeyFillWizard:
    def __init__(
        self,
        frame: np.ndarray,
        boundary: Rect,
        edges: np.ndarray,
        fill_tolerance: int = 12,
        min_fill_px: int = 300,
        max_fill_radius: Optional[int] = None,
    ):
        self.frame = frame
        self.boundary = boundary
        self.fill_tolerance = fill_tolerance
        self.min_fill_px = min_fill_px
        self.max_fill_radius = max_fill_radius
        self.mode = "white"

        h, w = frame.shape[:2]
        bx, by, bw, bh = boundary

        # Barrier mask (H+2, W+2) per cv2.floodFill convention: nonzero
        # pixels are never crossed. Start by blocking everything outside the
        # boundary rect, then also block detected edge pixels inside it.
        barrier = np.ones((h + 2, w + 2), dtype=np.uint8)
        barrier[1 + by : 1 + by + bh, 1 + bx : 1 + bx + bw] = 0

        edge_mask = (edges > 0).astype(np.uint8)
        inner = barrier[1:-1, 1:-1]
        barrier[1:-1, 1:-1] = np.maximum(inner, edge_mask)

        self._base_barrier = barrier
        self.keys: List[KeyBox] = []
        self._region_masks: List[np.ndarray] = []
        self._next_id = 0

    def _current_barrier(self) -> np.ndarray:
        barrier = self._base_barrier.copy()
        for m in self._region_masks:
            barrier[1:-1, 1:-1] = np.maximum(barrier[1:-1, 1:-1], m)
        return barrier

    def try_fill(self, x: int, y: int) -> Optional[KeyBox]:
        barrier = self._current_barrier()

        if barrier[1 + y, 1 + x] != 0:
            return None  # on an edge, outside the boundary, or already filled

        if self.max_fill_radius is not None:
            h, w = self.frame.shape[:2]
            r = self.max_fill_radius
            x0, x1 = max(0, x - r), min(w, x + r)
            y0, y1 = max(0, y - r), min(h, y + r)

            # Block everything outside a window around the click too, so a
            # weak/broken edge (common around dark keys against a dark case)
            # can't let the fill run away into a huge blob.
            window_block = np.ones_like(barrier)
            window_block[1 + y0 : 1 + y1, 1 + x0 : 1 + x1] = 0
            barrier = np.maximum(barrier, window_block)

        work = self.frame.copy()
        mask = barrier.copy()
        tol = (self.fill_tolerance,) * 3

        cv2.floodFill(
            work,
            mask,
            (x, y),
            (0, 0, 0),
            loDiff=tol,
            upDiff=tol,
            flags=4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8),
        )

        region = (mask[1:-1, 1:-1] == 255).astype(np.uint8)
        count = int(region.sum())

        if count < self.min_fill_px:
            return None

        key = KeyBox(id=self._next_id, kind=self.mode)
        self.keys.append(key)
        self._region_masks.append(region)
        self._next_id += 1
        return key

    def undo(self) -> None:
        if not self.keys:
            return
        self.keys.pop()
        self._region_masks.pop()
        self._next_id = max(0, self._next_id - 1)

    def reset(self) -> None:
        self.keys.clear()
        self._region_masks.clear()
        self._next_id = 0

    def toggle_mode(self) -> None:
        self.mode = "black" if self.mode == "white" else "white"

    def build_key_map(self) -> np.ndarray:
        """Single-channel map, same size as `frame`: pixel value = key_id + 1,
        0 = not part of any key. This is the pixel-exact record that gets saved."""
        h, w = self.frame.shape[:2]
        key_map = np.zeros((h, w), dtype=np.uint8)

        for key, mask in zip(self.keys, self._region_masks):
            key_map[mask.astype(bool)] = key.id + 1

        return key_map

    def overlay(self, canvas: np.ndarray) -> None:
        # Loud, high-contrast colors - deliberately not the red used for the
        # edge overlay elsewhere, so filled keys stay easy to spot against it.
        colors = {"white": (255, 0, 255), "black": (0, 255, 255)}

        for key, mask in zip(self.keys, self._region_masks):
            color = colors.get(key.kind, (0, 140, 255))
            tint = np.zeros_like(canvas)
            tint[mask.astype(bool)] = color
            cv2.addWeighted(tint, 0.55, canvas, 1.0, 0, dst=canvas)

            # Outline the actual filled shape (keys are often not simple
            # rectangles - a black key notch can cut into a white key).
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(canvas, contours, -1, color, 2)

            ys, xs = np.where(mask)
            cx, cy = int(xs.mean()), int(ys.mean())

            label = str(key.id + 1)
            font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2
            (tw, th), _ = cv2.getTextSize(label, font, scale, thickness)

            # Black backing box so the number reads clearly over any key color.
            cv2.rectangle(
                canvas,
                (cx - tw // 2 - 4, cy - th // 2 - 4),
                (cx + tw // 2 + 4, cy + th // 2 + 4),
                (0, 0, 0),
                -1,
            )
            cv2.putText(canvas, label, (cx - tw // 2, cy + th // 2), font, scale, (255, 255, 255), thickness)
