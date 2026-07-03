"""Load and save FingerAccuracy settings from config.json."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class CameraConfig:
    # A live camera index (0, 1, ...), or a path to a recorded video file -
    # cv2.VideoCapture accepts either, which is what makes offline analysis
    # of a pre-recorded session possible (see fingeraccuracy/offline.py).
    index: Union[int, str] = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    flip_vertical: bool = False
    flip_horizontal: bool = False


@dataclass
class KeyboardDetectionConfig:
    canny_low: int = 50
    canny_high: int = 150
    hough_threshold: int = 80
    hough_min_line_length: int = 60
    hough_max_line_gap: int = 10
    min_region_area_ratio: float = 0.05
    min_key_width_px: int = 10


@dataclass
class WizardConfig:
    fill_tolerance: int = 12
    min_fill_px: int = 300
    # A single fill is blocked from growing past this fraction of the
    # boundary's larger side, regardless of edges/tolerance - catches a black
    # key (weak contrast against its shadow/case) leaking into a huge blob.
    max_fill_size_ratio: float = 0.35


@dataclass
class MidiConfig:
    port_name: Optional[str] = None  # None = pick the first available port


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    keyboard_detection: KeyboardDetectionConfig = field(default_factory=KeyboardDetectionConfig)
    wizard: WizardConfig = field(default_factory=WizardConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    # Name of the calibration profile (data/<active_profile>/) that tools
    # load by default - set automatically each time step1_keyboard_wizard.py saves.
    active_profile: str = "default"

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg

        with open(path) as f:
            data = json.load(f)

        return cls(
            camera=CameraConfig(**data.get("camera", {})),
            keyboard_detection=KeyboardDetectionConfig(**data.get("keyboard_detection", {})),
            wizard=WizardConfig(**data.get("wizard", {})),
            midi=MidiConfig(**data.get("midi", {})),
            active_profile=data.get("active_profile", "default"),
        )

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        payload = {
            "camera": asdict(self.camera),
            "keyboard_detection": asdict(self.keyboard_detection),
            "wizard": asdict(self.wizard),
            "midi": asdict(self.midi),
            "active_profile": self.active_profile,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
