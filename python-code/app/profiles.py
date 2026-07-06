"""Discover saved calibration profiles under data/keyboard-profile/<name>/."""

from pathlib import Path
from typing import List

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "keyboard-profile"


def list_profiles(data_dir: Path = DATA_DIR) -> List[str]:
    if not data_dir.exists():
        return []
    return sorted(p.name for p in data_dir.iterdir() if (p / "keyboard_template.json").exists())


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
