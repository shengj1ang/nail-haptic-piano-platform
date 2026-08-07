"""Back up complete quiz participants into separate ZIP archives.

Run this script without arguments from the directory that contains it:

    python3 tool_backup_quiz_to_zip.py

It reads participant trial directories from ``data/quiz`` and writes one
archive per complete participant to the directory ``data/quiz-zip``. Only
P01 through P20 are eligible, and a participant is archived only when all
trials T01 through T27 are present. TEST, remote, and all other directory
names are ignored. ``.DS_Store`` files are excluded at every depth.

The script never deletes or modifies source data. It also never overwrites an
existing Pxx.zip. Each new archive is first written as tmp-Pxx.zip, tested
with 7z, and only then renamed to Pxx.zip. A failed or interrupted temporary
archive is preserved for manual inspection and is never treated as a
completed backup.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


QUIZ_DIR = Path("data") / "quiz"
BACKUP_DIR = Path("data") / "quiz-zip"
EXPECTED_TRIALS = frozenset(range(1, 28))

TRIAL_DIRECTORY_RE = re.compile(
    r"^(P(?:0[1-9]|1[0-9]|20))-T(0[1-9]|1[0-9]|2[0-7])(?:-.+)?$"
)


@dataclass(frozen=True)
class ParticipantBackup:
    participant: str
    directories: tuple[Path, ...]
    trial_numbers: frozenset[int]

    @property
    def missing_trials(self) -> tuple[int, ...]:
        return tuple(sorted(EXPECTED_TRIALS - self.trial_numbers))

    @property
    def complete(self) -> bool:
        return not self.missing_trials

    @property
    def archive_path(self) -> Path:
        return BACKUP_DIR / f"{self.participant}.zip"

    @property
    def temporary_path(self) -> Path:
        return BACKUP_DIR / f"tmp-{self.participant}.zip"


def _path_is_occupied(path: Path) -> bool:
    """Treat dangling symlinks as occupied so they are never overwritten."""
    return path.exists() or path.is_symlink()


def _discover_participants() -> tuple[list[ParticipantBackup], list[Path]]:
    grouped: dict[str, list[tuple[int, Path]]] = {}
    ignored: list[Path] = []

    for path in sorted(QUIZ_DIR.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or path.is_symlink():
            continue
        match = TRIAL_DIRECTORY_RE.fullmatch(path.name)
        if match is None:
            ignored.append(path)
            continue
        participant = match.group(1)
        trial_number = int(match.group(2))
        grouped.setdefault(participant, []).append((trial_number, path))

    participants = [
        ParticipantBackup(
            participant=participant,
            directories=tuple(path for _, path in sorted(entries)),
            trial_numbers=frozenset(trial for trial, _ in entries),
        )
        for participant, entries in sorted(grouped.items())
    ]
    return participants, ignored


def _format_trials(trials: tuple[int, ...] | list[int]) -> str:
    return ", ".join(f"T{trial:02d}" for trial in trials)


def _print_preview(
    pending: list[ParticipantBackup],
    existing: list[ParticipantBackup],
    incomplete: list[ParticipantBackup],
    blocked_by_temporary: list[ParticipantBackup],
    ignored: list[Path],
) -> None:
    print("\nBackup preview", flush=True)
    print("==============", flush=True)

    if pending:
        print("\nArchives that will be created:", flush=True)
        for backup in pending:
            print(
                f"  {backup.archive_path} <- {len(backup.directories)} directorie(s)",
                flush=True,
            )
            for directory in backup.directories:
                print(f"    - {directory}", flush=True)
    else:
        print("\nNo new archives are ready to be created.", flush=True)

    if existing:
        print("\nSkipped because the completed archive already exists:", flush=True)
        for backup in existing:
            print(f"  - {backup.archive_path}", flush=True)

    if incomplete:
        print("\nSkipped because T01-T27 are not yet complete:", flush=True)
        for backup in incomplete:
            print(
                f"  - {backup.participant}: missing {_format_trials(backup.missing_trials)}",
                flush=True,
            )

    if blocked_by_temporary:
        print("\nSkipped because a temporary archive already exists:", flush=True)
        for backup in blocked_by_temporary:
            print(
                f"  - {backup.temporary_path} "
                "(preserved; inspect or remove it manually before retrying)",
                flush=True,
            )

    if ignored:
        print("\nIgnored non-participant directories:", flush=True)
        for path in ignored:
            print(f"  - {path}", flush=True)


def _archive_participant(backup: ParticipantBackup, index: int, total: int) -> bool:
    archive = backup.archive_path
    temporary = backup.temporary_path

    print(
        f"\n[archive {index:>2}/{total}] {backup.participant} -> {archive}",
        flush=True,
    )

    if platform.system() != "Darwin":
        print("  SKIPPED: this script runs only on macOS.", flush=True)
        return False
    if shutil.which("7z") is None:
        print("  SKIPPED: 7z is no longer available on PATH.", flush=True)
        return False
    if _path_is_occupied(archive):
        print("  SKIPPED: the completed archive now exists; it will not be overwritten.", flush=True)
        return False
    if _path_is_occupied(temporary):
        print("  SKIPPED: the temporary archive now exists; it will not be overwritten.", flush=True)
        return False

    for directory in backup.directories:
        if not directory.is_dir() or directory.is_symlink():
            print(
                f"  SKIPPED: source directory is missing or unsafe: {directory}",
                flush=True,
            )
            return False

    # Run from data/quiz so the ZIP contains Pxx-Txx-... at its root rather
    # than embedding the repository's parent directories. -tzip explicitly
    # requests ZIP format; no source-deletion option is ever passed to 7z.
    command = [
        "7z",
        "a",
        "-tzip",
        "-xr!.DS_Store",
        str(Path("..") / BACKUP_DIR.name / temporary.name),
        *(directory.name for directory in backup.directories),
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=QUIZ_DIR,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, KeyboardInterrupt) as exc:
        print(
            f"  FAILED: 7z was interrupted or could not start: {exc}",
            flush=True,
        )
        print("  Source directories were not changed or deleted.", flush=True)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if completed.returncode != 0 or not temporary.is_file():
        print(
            f"  FAILED: 7z exited with status {completed.returncode}. "
            "Any temporary archive is preserved; source directories were not changed.",
            flush=True,
        )
        return False

    try:
        tested = subprocess.run(
            ["7z", "t", str(temporary)],
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, KeyboardInterrupt) as exc:
        print(f"  FAILED: the archive could not be tested: {exc}", flush=True)
        print("  The temporary archive and all source directories are preserved.", flush=True)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if tested.returncode != 0:
        print(
            f"  FAILED: 7z archive test exited with status {tested.returncode}. "
            "The temporary archive and all source directories are preserved.",
            flush=True,
        )
        return False

    if _path_is_occupied(archive):
        print(
            "  FAILED: the completed archive appeared during processing. "
            "Neither archive will be overwritten or deleted.",
            flush=True,
        )
        return False

    try:
        temporary.rename(archive)
    except OSError as exc:
        print(
            f"  FAILED: the tested temporary archive could not be renamed: {exc}",
            flush=True,
        )
        print("  The temporary archive and all source directories are preserved.", flush=True)
        return False

    print("  OK: ZIP archive created and tested. Source directories were preserved.", flush=True)
    return True


def _choose_action() -> bool:
    print("\nChoose the next action:", flush=True)
    print("  1. Create every archive shown in the preview", flush=True)
    print("  2. Exit without creating any archives", flush=True)
    while True:
        try:
            choice = input("Enter 1 or 2: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting without creating any archives.", flush=True)
            return False
        if choice == "1":
            return True
        if choice == "2":
            print("Exiting without creating any archives.", flush=True)
            return False
        print("Invalid choice. Enter 1 or 2.", flush=True)


def main() -> int:
    if platform.system() != "Darwin":
        print("ERROR: this backup script runs only on macOS. No files were changed.", file=sys.stderr)
        return 2
    if shutil.which("7z") is None:
        print("ERROR: 7z was not found on PATH. No files were changed.", file=sys.stderr)
        return 2
    if not QUIZ_DIR.is_dir():
        print(f"ERROR: quiz directory does not exist: {QUIZ_DIR}", file=sys.stderr)
        return 2
    if _path_is_occupied(BACKUP_DIR) and not BACKUP_DIR.is_dir():
        print(
            f"ERROR: backup destination exists but is not a directory: {BACKUP_DIR}. "
            "No files were changed.",
            file=sys.stderr,
        )
        return 2

    participants, ignored = _discover_participants()
    existing: list[ParticipantBackup] = []
    incomplete: list[ParticipantBackup] = []
    blocked_by_temporary: list[ParticipantBackup] = []
    pending: list[ParticipantBackup] = []

    for backup in participants:
        if _path_is_occupied(backup.archive_path):
            existing.append(backup)
        elif not backup.complete:
            incomplete.append(backup)
        elif _path_is_occupied(backup.temporary_path):
            blocked_by_temporary.append(backup)
        else:
            pending.append(backup)

    _print_preview(pending, existing, incomplete, blocked_by_temporary, ignored)

    if not pending:
        print("\nNo archives need to be created.", flush=True)
        return 0
    if not _choose_action():
        return 0

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: could not create {BACKUP_DIR}: {exc}", file=sys.stderr)
        return 2

    created = 0
    failed = 0
    for index, backup in enumerate(pending, start=1):
        if _archive_participant(backup, index, len(pending)):
            created += 1
        else:
            failed += 1

    print("\nFinished:", flush=True)
    print(f"  archives created: {created}", flush=True)
    print(f"  failed/skipped:   {failed}", flush=True)
    print(f"  already existed:  {len(existing)}", flush=True)
    print(f"  incomplete:       {len(incomplete)}", flush=True)
    print("  source deleted:   0", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
