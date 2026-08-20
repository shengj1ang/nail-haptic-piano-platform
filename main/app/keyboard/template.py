"""Pixel-exact keyboard template.

Each key's shape is stored as a plain pixel map - a single-channel PNG the
same size as the camera frame, where pixel value = key_id + 1 (0 = not part
of any key) - rather than as a rectangle or polygon. Using the template
later is then a single array lookup at a pixel position, no geometry math
involved.

`note` is intentionally left unset by detection - it is filled in later by
hand, in config, once the physical key layout is known.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

KEY_MAP_FILENAME = "keyboard_key_map.png"


@dataclass
class KeyBox:
    id: int
    kind: str  # "white" or "black"
    note: Optional[str] = None


@dataclass
class KeyboardTemplate:
    frame_width: int
    frame_height: int
    keys: List[KeyBox] = field(default_factory=list)
    region: Optional[dict] = None  # informational only - not used for hit-testing
    key_map: Optional[np.ndarray] = field(default=None, repr=False, compare=False)

    def key_id_at(self, x: int, y: int) -> Optional[int]:
        """Which key (by id) occupies pixel (x, y) of the original frame, or None."""
        if self.key_map is None:
            raise RuntimeError("key_map is not loaded")

        if not (0 <= y < self.key_map.shape[0] and 0 <= x < self.key_map.shape[1]):
            return None

        value = int(self.key_map[y, x])
        return value - 1 if value > 0 else None

    def save(self, json_path: Path) -> None:
        if self.key_map is None:
            raise RuntimeError("key_map must be set before saving")

        json_path = Path(json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(json_path.parent / KEY_MAP_FILENAME), self.key_map)

        payload = {
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "region": self.region,
            "key_map": KEY_MAP_FILENAME,
            "keys": [asdict(k) for k in self.keys],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, json_path: Path) -> "KeyboardTemplate":
        json_path = Path(json_path)
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        key_map_path = json_path.parent / data["key_map"]
        key_map = cv2.imread(str(key_map_path), cv2.IMREAD_UNCHANGED)
        if key_map is None:
            raise FileNotFoundError(key_map_path)

        return cls(
            frame_width=data["frame_width"],
            frame_height=data["frame_height"],
            keys=[KeyBox(**k) for k in data["keys"]],
            region=data.get("region"),
            key_map=key_map,
        )
