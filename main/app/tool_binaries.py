"""Find the external command-line programs the maintenance tools drive.

ffmpeg (review-video compression) and 7z (participant ZIP backups) are
not Python packages, so where they live differs per machine: Homebrew on
a Mac, a downloaded .exe on the Windows laptop the rig runs on. Rather
than every tool hardcoding shutil.which("ffmpeg") and then reporting
"not on PATH" on a machine where the executable sits right next to the
code, all lookups go through here and check two places, in order:

  1. main/runtime/bin - drop ffmpeg.exe / 7z.exe (or their macOS/Linux
     equivalents) in that folder and nothing else needs configuring.
     launcher.bat prepends the same folder to PATH for the bundled
     Windows runtime; this module also prepends it in-process, so it
     works just as well when the launcher is started with
     `python launcher.py` on any OS - and so does anything those tools
     start themselves.
  2. PATH, as installed system-wide.

Windows is the reason the lookup is shutil.which() rather than joining a
filename: which() applies PATHEXT, so the name "7z" finds 7z.exe without
this module needing to know the extension, and the same call works
unchanged on macOS and Linux.

7-Zip also ships under several names - 7z, the modern official 7zz, and
the standalone 7za - and any of them can create and test the ZIP
archives the backup tool asks for, so all three are accepted in that
order of preference. (7zr is deliberately not in the list: it handles
only the .7z format, and the backup tool writes ZIP.)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, Sequence

# main/ - the directory the tools' relative data paths are anchored to.
MAIN_DIR = Path(__file__).resolve().parent.parent

# Optional drop-in folder for the executables. Nothing here creates it;
# an absent folder simply means "nothing bundled, use PATH".
RUNTIME_BIN = MAIN_DIR / "runtime" / "bin"

FFMPEG_NAMES = ("ffmpeg",)
SEVEN_ZIP_NAMES = ("7z", "7zz", "7za")


def ensure_runtime_bin_on_path() -> bool:
    """Put runtime/bin at the front of this process's PATH, once.

    Only this process and the children it starts are affected - nothing
    is written to the user's shell profile or to the registry, so the
    entry disappears with the process.

    Returns True if the folder exists and is now on PATH.
    """
    if not RUNTIME_BIN.is_dir():
        return False
    entry = str(RUNTIME_BIN)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if entry not in parts:
        os.environ["PATH"] = os.pathsep.join([entry, *parts]) if parts else entry
    return True


# Done at import, so simply importing this module is enough for any
# lookup - including one made by code that never calls find_tool, such
# as a library that shells out to ffmpeg on its own.
ensure_runtime_bin_on_path()


def find_tool(names: Sequence[str]) -> Optional[str]:
    """Full path to the first of `names` that exists, or None.

    runtime/bin is searched explicitly before PATH rather than relying on
    the PATH entry above, so a bundled copy still wins if something else
    in this process has since rewritten PATH.
    """
    if RUNTIME_BIN.is_dir():
        for name in names:
            found = shutil.which(name, path=str(RUNTIME_BIN))
            if found:
                return found
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_ffmpeg() -> Optional[str]:
    return find_tool(FFMPEG_NAMES)


def find_seven_zip() -> Optional[str]:
    return find_tool(SEVEN_ZIP_NAMES)


def is_bundled(path: Optional[str]) -> bool:
    """True if this executable came from runtime/bin rather than PATH."""
    if not path:
        return False
    try:
        return Path(path).resolve().parent == RUNTIME_BIN.resolve()
    except OSError:
        return False


def describe_tool(display_name: str, path: Optional[str]) -> str:
    """One line saying which copy is in use, for a status bar or a log."""
    if path is None:
        return f"{display_name}: not found"
    where = "bundled in runtime/bin" if is_bundled(path) else "found on PATH"
    return f"{display_name}: {path}  ({where})"


def missing_tool_message(display_name: str, names: Sequence[str] = ()) -> str:
    """Why a tool could not be found, and the two ways to fix it."""
    alternatives = ""
    if len(names) > 1:
        alternatives = " (also accepted: " + ", ".join(names[1:]) + ")"
    return (
        f"{display_name} was not found{alternatives}.\n"
        f"Install it so it is on PATH, or put the executable "
        f"(on Windows {names[0] if names else display_name}.exe) in:\n"
        f"    {RUNTIME_BIN}"
    )
