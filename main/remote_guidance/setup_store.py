"""What the tele-training setup wizard is allowed to write, and where.

The launcher's Initial Setup wizards calibrate the machine the *experiment*
runs on: they write `config.json`'s top-level `camera`, `midi.port_name`
and `active_keyboard_profile`, and they save the calibration into
`data/keyboard-profile/<name>/`, overwriting whatever was there. That is
correct for their job and fatal for this one. A new student joining a
tele-training session needs their own camera, their own calibration and
their own MIDI mapping, and running the existing wizards to get them
repoints the formal experiment's devices and can destroy the very profile
its recorded sessions were scored against.

So this module is the whole of the "may I write that?" decision, kept
GUI-free so it can be tested without a camera, a keyboard or Qt. Two rules,
and they are the point of the module:

1. **`config.json` is never written at all.** The wizard's only output is
   a profile folder. It does not set `active_keyboard_profile`, it does
   not move the camera or the MIDI port, and it does not even write the
   `remote_guidance` block - which is why running it can have no effect on
   any tool on this machine until someone deliberately selects the new
   profile in a client's own Settings dialog.

2. **The wizard may only write into profiles it made itself.** Every
   profile it creates gets a `remote_setup.json` marker, and any write to
   an existing profile directory is refused unless that marker is there.
   Creating one refuses outright if the directory already exists. So no
   sequence of clicks in this wizard can modify a profile that the
   experiment - or any recorded session - depends on.

Profiles still live in the shared `data/keyboard-profile/`, deliberately.
A remote session records its profile by *name* in `QuizMeta`, and the
offline finger pass, the quiz analysis window, the video sync window and
`profile_led_mapper` all resolve that name against that one directory. A
separate remote directory would isolate the files and break every one of
those readers; the marker gives the same protection without moving
anything.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List

from app.profiles import DATA_DIR as PROFILE_DATA_DIR

MARKER_FILENAME = "remote_setup.json"
TEMPLATE_FILENAME = "keyboard_template.json"
MIDI_MAPPING_FILENAME = "midi_mapping.json"

# Anything that would make a messy directory name. Deliberately strict:
# this is a folder name that ends up inside saved session metadata.
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class SetupError(Exception):
    """A refusal meant to be shown to the user."""


@dataclass
class RemoteProfileMarker:
    """Proof that this profile was made by the tele-training wizard, and
    is therefore safe for it to overwrite.

    Deliberately says nothing about a *role*. A calibration profile is a
    pixel mask of one camera's view of one keyboard plus that keyboard's
    note mapping - it belongs to a rig, not to a person. Student and
    teacher normally need one each because they normally sit at two
    different rigs, and when they share a rig they can share the profile.
    An earlier version recorded which role created it, which enforced
    nothing and only implied a constraint that does not exist."""

    profile_name: str
    created_at: float  # absolute wall-clock time.time()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "RemoteProfileMarker":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            profile_name=data["profile_name"],
            created_at=float(data.get("created_at") or 0.0),
        )


def sanitize_profile_name(name: str) -> str:
    """A filesystem-safe profile name, or "" if nothing usable is left."""
    cleaned = _INVALID_NAME_CHARS.sub("-", (name or "").strip())
    return cleaned.strip("-. ")


def profile_dir(profile_name: str, data_dir: Path = PROFILE_DATA_DIR) -> Path:
    return Path(data_dir) / profile_name


def marker_path(profile_name: str, data_dir: Path = PROFILE_DATA_DIR) -> Path:
    return profile_dir(profile_name, data_dir) / MARKER_FILENAME


def is_remote_profile(profile_name: str, data_dir: Path = PROFILE_DATA_DIR) -> bool:
    """Whether this profile was created by the tele-training wizard."""
    return marker_path(profile_name, data_dir).exists()


def list_remote_profiles(data_dir: Path = PROFILE_DATA_DIR) -> List[str]:
    """Only the wizard's own profiles - the ones it may reopen and change."""
    root = Path(data_dir)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / MARKER_FILENAME).exists())


def profile_status(profile_name: str, data_dir: Path = PROFILE_DATA_DIR) -> dict:
    """What a profile already has, for a wizard step that offers to redo
    only part of it."""
    directory = profile_dir(profile_name, data_dir)
    return {
        "exists": directory.exists(),
        "remote": is_remote_profile(profile_name, data_dir),
        "calibrated": (directory / TEMPLATE_FILENAME).exists(),
        "mapped": (directory / MIDI_MAPPING_FILENAME).exists(),
    }


def create_profile(profile_name: str, data_dir: Path = PROFILE_DATA_DIR) -> Path:
    """Claim a brand-new profile directory for the wizard.

    Takes no role: what a profile describes is a camera and a keyboard,
    and either client can be pointed at it (see RemoteProfileMarker).

    Refuses if anything is already there, whoever made it. "Overwrite the
    existing one" is not offered on purpose: the profile a recorded
    session was scored against has to keep meaning what it meant, and the
    cost of a new name is nothing next to silently invalidating it."""
    name = sanitize_profile_name(profile_name)
    if not name:
        raise SetupError("Give the profile a name (letters, digits, dot, dash or underscore).")

    directory = profile_dir(name, data_dir)
    if directory.exists():
        raise SetupError(
            f"A profile called {name!r} already exists. Choose a different name - this wizard never "
            "overwrites an existing calibration, because a recorded session may have been scored "
            "against it."
        )

    directory.mkdir(parents=True)
    RemoteProfileMarker(profile_name=name, created_at=time.time()).save(marker_path(name, data_dir))
    return directory


def writable_profile_dir(profile_name: str, data_dir: Path = PROFILE_DATA_DIR) -> Path:
    """The directory to write into when redoing part of an existing
    profile, or a refusal.

    This is the guard that makes "redo just the MIDI mapping" safe: it
    only ever succeeds for a directory carrying the wizard's own marker,
    so the experiment's profiles - and every profile made by the launcher's
    Initial Setup - are unreachable from here."""
    name = sanitize_profile_name(profile_name)
    directory = profile_dir(name, data_dir)
    if not directory.exists():
        raise SetupError(f"Profile {name!r} does not exist yet.")
    if not is_remote_profile(name, data_dir):
        raise SetupError(
            f"Profile {name!r} was not created by the tele-training setup, so this wizard will not "
            "change it. It may be the profile the formal experiment is calibrated against. Create a "
            "new tele-training profile instead."
        )
    return directory
