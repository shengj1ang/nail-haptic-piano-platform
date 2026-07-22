"""Discover saved calibration profiles under data/keyboard-profile/<name>/."""

import shutil
from pathlib import Path
from typing import List

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "keyboard-profile"


def list_profiles(data_dir: Path = DATA_DIR) -> List[str]:
    if not data_dir.exists():
        return []
    return sorted(p.name for p in data_dir.iterdir() if (p / "keyboard_template.json").exists())


def snapshot_profile(profile_name: str, dest_dir: Path, data_dir: Path = DATA_DIR) -> None:
    """Copy data/keyboard-profile/<profile_name>/ (keyboard_template.json,
    midi_mapping.json, ...) into dest_dir - a point-in-time record of the
    calibration in effect right now, kept alongside a recording's raw/ files
    so it can still be checked against later even if the live profile is
    since recalibrated, renamed, or deleted."""
    shutil.copytree(data_dir / profile_name, dest_dir, dirs_exist_ok=True)


def list_profiles_with_midi_mapping(data_dir: Path = DATA_DIR) -> List[str]:
    """Calibrated profiles that also have a MIDI mapping - i.e. ready to
    translate a MIDI note to a key_id, which anything cueing by note
    (quiz guidance, LED cueing) needs, not just camera-based key regions."""
    if not data_dir.exists():
        return []
    return sorted(
        p.name
        for p in data_dir.iterdir()
        if (p / "keyboard_template.json").exists() and (p / "midi_mapping.json").exists()
    )
