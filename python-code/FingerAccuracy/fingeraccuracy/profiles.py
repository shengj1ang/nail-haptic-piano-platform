"""Discover saved calibration profiles under data/<name>/."""

from pathlib import Path
from typing import List

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def list_profiles(data_dir: Path = DATA_DIR) -> List[str]:
    if not data_dir.exists():
        return []
    return sorted(p.name for p in data_dir.iterdir() if (p / "keyboard_template.json").exists())
