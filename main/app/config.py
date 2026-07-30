"""Load and save FingerAccuracy settings from config.json.

The "haptic" block (which actuator is in use and each actuator's default
frequency/amp) is owned by common.haptic_config - the shared module every
tool, experiment and window reads its defaults from. It is exposed here
as Config.haptic so a window that already holds a Config can reach it
without a second import, but the validation, the range constants and the
live-saving path all live in that one module.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union

from common.haptic_config import (
    HapticConfig,
    atomic_write_json,
    default_haptic_config,
    read_config_file,
    validate_haptic_config,
)

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
class AccelerometerConfig:
    """Which LIS3DH on the vibration rig the accelerometer tools follow.

    The firmware streams every sensor interleaved ("ACC,<id>,x,y,z"); this
    is the <id> the Accelerometer Live View filters on, set from that
    window's selector and used as the default sensor in the validation
    sweep windows."""

    sensor_id: int = 0


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
    pilot_schedule: Optional[int] = None  # Main User Study schedule


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    keyboard_detection: KeyboardDetectionConfig = field(default_factory=KeyboardDetectionConfig)
    wizard: WizardConfig = field(default_factory=WizardConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    accelerometer: AccelerometerConfig = field(default_factory=AccelerometerConfig)
    finger_matching: FingerMatchingConfig = field(default_factory=FingerMatchingConfig)
    seeds: SeedConfig = field(default_factory=SeedConfig)
    # Which actuator the platform drives and each actuator's default
    # frequency/amp - see common/haptic_config.py, which validates this
    # block, applies the built-in defaults to an older config.json that
    # doesn't have it, and saves changes on its own (Initial Setup ->
    # Haptic Actuator Defaults writes through that module, not here).
    haptic: HapticConfig = field(default_factory=default_haptic_config)
    # Name of the calibration profile (data/keyboard-profile/<active_keyboard_profile>/)
    # that tools load by default - set automatically each time setup_keyboard_wizard.py saves.
    active_keyboard_profile: str = "default"
    # Which cue style Quiz - Visual Guidance shows on the cue screen:
    # "dot" (ten circles) or "hand" (highlighted hand photos) - see
    # app.gui.cue_window's CUE_STYLES. Set by the launcher's Visual
    # Guidance Cue Selection window.
    visual_cue_style: str = "dot"

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        # A config.json written before the haptic block existed (or one
        # missing individual fields) must not break startup: the
        # validator fills in whatever is absent and clamps whatever is
        # out of range, reporting both through common.haptic_config.
        haptic, _warnings = validate_haptic_config(data.get("haptic"))

        return cls(
            camera=CameraConfig(**data.get("camera", {})),
            keyboard_detection=KeyboardDetectionConfig(**data.get("keyboard_detection", {})),
            wizard=WizardConfig(**data.get("wizard", {})),
            midi=MidiConfig(**data.get("midi", {})),
            accelerometer=AccelerometerConfig(**data.get("accelerometer", {})),
            finger_matching=FingerMatchingConfig(**data.get("finger_matching", {})),
            seeds=SeedConfig(**data.get("seeds", {})),
            haptic=haptic,
            active_keyboard_profile=data.get("active_keyboard_profile", "default"),
            visual_cue_style=data.get("visual_cue_style", "dot"),
        )

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        # Merged into whatever the file already holds (rather than
        # replacing it) so a key this dataclass doesn't model - one
        # written by a newer version, or by hand - survives a save, and
        # written atomically so an interrupted write can't leave a
        # corrupt config.json behind.
        payload = read_config_file(path)
        payload.update({
            "camera": asdict(self.camera),
            "keyboard_detection": asdict(self.keyboard_detection),
            "wizard": asdict(self.wizard),
            "midi": asdict(self.midi),
            "accelerometer": asdict(self.accelerometer),
            "finger_matching": asdict(self.finger_matching),
            "seeds": asdict(self.seeds),
            "haptic": self.haptic.to_dict(),
            "active_keyboard_profile": self.active_keyboard_profile,
            "visual_cue_style": self.visual_cue_style,
        })
        atomic_write_json(path, payload)
