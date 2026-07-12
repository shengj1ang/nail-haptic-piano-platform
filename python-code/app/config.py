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
    # of a pre-recorded session possible (see app/offline.py).
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
class FingerMatchingConfig:
    """Softmax scoring of "which fingertip pressed this key" (see
    app.finger_matching, where these are read at import time). One shared
    setting for every consumer - the live detector, the offline quiz
    analysis and the review video all show/judge the same numbers."""

    # How many pixels of extra distance-to-key it takes for a fingertip's
    # softmax weight to drop by a factor of e. Smaller = the distribution
    # concentrates harder on the closest fingertip.
    softmax_temperature_px: float = 25.0
    # A note's finger judgment passes if the *target* finger holds at least
    # this share of the softmax mass (not "argmax must equal target").
    probability_threshold: float = 0.4


@dataclass
class SeedConfig:
    """Default RNG seeds, one per tool. Each tool's window prefills its
    seed field from here on open (so reopening a tool keeps working with
    the same seed), and its "New seed" button overwrites the stored value -
    drawing a fresh seed is the only action that changes the default."""

    sequence_generator: Optional[int] = None  # Experiment Sequence Generator
    pilot_schedule: Optional[int] = None  # Controlled Pilot Study schedule


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    keyboard_detection: KeyboardDetectionConfig = field(default_factory=KeyboardDetectionConfig)
    wizard: WizardConfig = field(default_factory=WizardConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    finger_matching: FingerMatchingConfig = field(default_factory=FingerMatchingConfig)
    seeds: SeedConfig = field(default_factory=SeedConfig)
    # Name of the calibration profile (data/keyboard-profile/<active_keyboard_profile>/)
    # that tools load by default - set automatically each time setup_keyboard_wizard.py saves.
    active_keyboard_profile: str = "default"
    # Which cue style Quiz - Visual Guidance shows on the cue screen:
    # "circles" (dot view) or "images" (hand view) - see app.gui.cue_window's
    # CUE_STYLES. Set by the launcher's Visual Guidance Cue Selection window.
    visual_cue_style: str = "circles"

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
            finger_matching=FingerMatchingConfig(**data.get("finger_matching", {})),
            seeds=SeedConfig(**data.get("seeds", {})),
            active_keyboard_profile=data.get("active_keyboard_profile", "default"),
            visual_cue_style=data.get("visual_cue_style", "circles"),
        )

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        payload = {
            "camera": asdict(self.camera),
            "keyboard_detection": asdict(self.keyboard_detection),
            "wizard": asdict(self.wizard),
            "midi": asdict(self.midi),
            "finger_matching": asdict(self.finger_matching),
            "seeds": asdict(self.seeds),
            "active_keyboard_profile": self.active_keyboard_profile,
            "visual_cue_style": self.visual_cue_style,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
