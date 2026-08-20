"""Back up complete quiz participants into separate ZIP archives.

As with app/review_compress.py, the logic lives here without printing or
prompting so that the console entry point
(main/tool_backup_quiz_to_zip.py) and the launcher's Tools window
(app/gui/quiz_backup_window.py) share one implementation. Frontends pass
`log`, `on_progress` and `cancelled` callbacks and decide how to show
them.

Participant trial directories are read from ``data/quiz`` and one
archive per complete participant is written to ``data/quiz-zip``. Only
P01 through P20 are eligible, and a participant is archived only when
all trials T01 through T27 are present - archiving a participant early
would leave a Pxx.zip that every later run skips as "already backed up",
silently freezing the backup at whatever was finished that day. TEST,
remote, and all other directory names are ignored. ``.DS_Store`` files
are excluded at every depth.

Nothing here deletes or modifies source data, and no existing Pxx.zip is
ever overwritten. Each new archive is first written as tmp-Pxx.zip,
tested with 7z, and only then renamed to Pxx.zip. A failed, cancelled or
interrupted temporary archive is preserved for manual inspection and is
never treated as a completed backup.

This used to refuse to run anywhere but macOS. It no longer does: the
work is one 7z invocation over relative paths, which is as true on the
Windows machine the rig runs on as on the analysis Mac, and which 7z
executable to use now comes from app/tool_binaries.py (runtime/bin
first, then PATH) instead of assuming a Homebrew install.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .tool_binaries import SEVEN_ZIP_NAMES, find_seven_zip, missing_tool_message

MAIN_DIR = Path(__file__).resolve().parent.parent
QUIZ_DIR = MAIN_DIR / "data" / "quiz"
BACKUP_DIR = MAIN_DIR / "data" / "quiz-zip"
EXPECTED_TRIALS = frozenset(range(1, 28))

TRIAL_DIRECTORY_RE = re.compile(
    r"^(P(?:0[1-9]|1[0-9]|20))-T(0[1-9]|1[0-9]|2[0-7])(?:-.+)?$"
)

# 7z's own progress line, produced by -bsp1:  " 42% 13 + P01-T05-.../review.mp4"
PERCENT_RE = re.compile(r"(\d{1,3})%")

STAGE_ARCHIVE = "Compressing"
STAGE_TEST = "Testing the archive"

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]
CancelFn = Callable[[], bool]


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


@dataclass
class BackupPlan:
    """What a run would do, worked out before anything is written.

    Every participant lands in exactly one bucket, and the buckets are
    the reasons themselves - so a frontend can show why a participant is
    not being archived without re-deriving the rule.
    """

    pending: list[ParticipantBackup] = field(default_factory=list)
    existing: list[ParticipantBackup] = field(default_factory=list)
    incomplete: list[ParticipantBackup] = field(default_factory=list)
    blocked_by_temporary: list[ParticipantBackup] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)

    def state_of(self, backup: ParticipantBackup) -> str:
        for bucket, state in (
            (self.pending, "ready"),
            (self.existing, "archive exists"),
            (self.incomplete, "incomplete"),
            (self.blocked_by_temporary, "temporary archive exists"),
        ):
            if backup in bucket:
                return state
        return "unknown"

    @property
    def participants(self) -> list[ParticipantBackup]:
        """Every participant found, in Pxx order, whatever their state."""
        return sorted(
            [*self.pending, *self.existing, *self.incomplete, *self.blocked_by_temporary],
            key=lambda backup: backup.participant,
        )


def seven_zip_missing_message() -> str:
    return missing_tool_message("7z", SEVEN_ZIP_NAMES)


def path_is_occupied(path: Path) -> bool:
    """Treat dangling symlinks as occupied so they are never overwritten."""
    return path.exists() or path.is_symlink()


def display_path(path: Path) -> str:
    """Name a path relative to main/ where possible - the tool's own data
    lives there, and an absolute path per line reads as noise."""
    try:
        return str(path.relative_to(MAIN_DIR))
    except ValueError:
        return str(path)


def format_trials(trials: Iterable[int]) -> str:
    return ", ".join(f"T{trial:02d}" for trial in trials)


def _noop_log(_message: str) -> None:
    pass


def discover_participants() -> tuple[list[ParticipantBackup], list[Path]]:
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


def plan_backups() -> BackupPlan:
    """Sort every participant into the one reason that applies to it.

    Order matters: an existing Pxx.zip means the participant is done and
    nothing else about them needs saying, and a participant who is not
    complete yet cannot be blocked by a temporary archive they were
    never eligible for.
    """
    participants, ignored = discover_participants()
    plan = BackupPlan(ignored=ignored)
    for backup in participants:
        if path_is_occupied(backup.archive_path):
            plan.existing.append(backup)
        elif not backup.complete:
            plan.incomplete.append(backup)
        elif path_is_occupied(backup.temporary_path):
            plan.blocked_by_temporary.append(backup)
        else:
            plan.pending.append(backup)
    return plan


def _run_seven_zip(
    command: list[str],
    stage: str,
    cwd: Optional[Path],
    on_progress: Optional[ProgressFn],
    cancelled: Optional[CancelFn],
) -> int:
    """Run one 7z command and return its exit code.

    Without a progress callback or a cancellation to honour, 7z keeps the
    console's stdout - what someone running the script from a terminal
    expects. With either, its output is read instead and -bsp1 is added
    so the percentage arrives on stdout; -bsp1 changes only where 7z
    reports progress, never what it writes to the archive.

    That output is read in raw chunks rather than by lines because 7z
    rewrites its progress in place with backspaces, not carriage
    returns: to readline() the entire run is a single unterminated line,
    and every percentage would arrive at once, after the work is over.

    A cancelled run is a failed one: 7z is terminated, exits non-zero,
    and the caller preserves the temporary archive exactly as it does
    for any other failure.
    """
    if on_progress is None and cancelled is None:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode

    process = subprocess.Popen(
        [command[0], "-bsp1", *command[1:]],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        fd = process.stdout.fileno()  # type: ignore[union-attr]
        tail = b""
        while True:
            # os.read returns as soon as anything is there, unlike a
            # sized read on the text wrapper, which would wait for a full
            # buffer - and 7z's progress is a trickle of a few bytes.
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer = tail + chunk
            matches = PERCENT_RE.findall(buffer.decode("utf-8", "replace"))
            if matches and on_progress is not None:
                on_progress(stage, min(int(matches[-1]), 100), 100)
            # Keep enough of the tail that a percentage split across two
            # chunks is still seen whole by the next pass.
            tail = buffer[-8:]
            if cancelled is not None and cancelled():
                process.terminate()
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    return process.returncode


def archive_participant(
    backup: ParticipantBackup,
    log: LogFn = _noop_log,
    on_progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> bool:
    """Write, test and publish one participant's archive, or change nothing.

    Source directories are never passed to a deleting option and are
    never touched here; the only file this creates is the temporary
    archive, and the only rename is that temporary onto its final name
    once 7z has tested it.
    """
    archive = backup.archive_path
    temporary = backup.temporary_path

    seven_zip = find_seven_zip()
    if seven_zip is None:
        log("  SKIPPED: 7z is no longer available.")
        return False
    if path_is_occupied(archive):
        log("  SKIPPED: the completed archive now exists; it will not be overwritten.")
        return False
    if path_is_occupied(temporary):
        log("  SKIPPED: the temporary archive now exists; it will not be overwritten.")
        return False

    for directory in backup.directories:
        if not directory.is_dir() or directory.is_symlink():
            log(f"  SKIPPED: source directory is missing or unsafe: {display_path(directory)}")
            return False

    if cancelled is not None and cancelled():
        log("  SKIPPED: stopped before this participant; nothing was created.")
        return False

    # Run from data/quiz so the ZIP contains Pxx-Txx-... at its root rather
    # than embedding the repository's parent directories. -tzip explicitly
    # requests ZIP format; no source-deletion option is ever passed to 7z.
    command = [
        seven_zip,
        "a",
        "-tzip",
        "-xr!.DS_Store",
        str(Path("..") / BACKUP_DIR.name / temporary.name),
        *(directory.name for directory in backup.directories),
    ]

    if on_progress is not None:
        on_progress(STAGE_ARCHIVE, 0, 100)
    try:
        returncode = _run_seven_zip(command, STAGE_ARCHIVE, QUIZ_DIR, on_progress, cancelled)
    except (OSError, KeyboardInterrupt) as exc:
        log(f"  FAILED: 7z was interrupted or could not start: {exc}")
        log("  Source directories were not changed or deleted.")
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if returncode != 0 or not temporary.is_file():
        if cancelled is not None and cancelled():
            log("  STOPPED: compression was cancelled before the archive was finished.")
        else:
            log(f"  FAILED: 7z exited with status {returncode}.")
        if temporary.is_file():
            log(
                f"  The unfinished {display_path(temporary)} is preserved; "
                "inspect or remove it manually before retrying this participant."
            )
        log("  Source directories were not changed.")
        return False

    if on_progress is not None:
        on_progress(STAGE_TEST, 0, 100)
    try:
        # Deliberately not cancellable: the archive exists at this point
        # and testing is what decides whether it may be published.
        tested = _run_seven_zip(
            [seven_zip, "t", str(temporary)], STAGE_TEST, None, on_progress, None
        )
    except (OSError, KeyboardInterrupt) as exc:
        log(f"  FAILED: the archive could not be tested: {exc}")
        log("  The temporary archive and all source directories are preserved.")
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if tested != 0:
        log(
            f"  FAILED: 7z archive test exited with status {tested}. "
            "The temporary archive and all source directories are preserved."
        )
        return False

    if path_is_occupied(archive):
        log(
            "  FAILED: the completed archive appeared during processing. "
            "Neither archive will be overwritten or deleted."
        )
        return False

    try:
        temporary.rename(archive)
    except OSError as exc:
        log(f"  FAILED: the tested temporary archive could not be renamed: {exc}")
        log("  The temporary archive and all source directories are preserved.")
        return False

    log("  OK: ZIP archive created and tested. Source directories were preserved.")
    return True


def create_backup_dir() -> Optional[str]:
    """Create data/quiz-zip, returning an error message if that failed."""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"could not create {display_path(BACKUP_DIR)}: {exc}"
    return None


def archive_all(
    pending: Iterable[ParticipantBackup],
    log: LogFn = _noop_log,
    on_participant: Optional[Callable[[int, int, ParticipantBackup], None]] = None,
    on_result: Optional[Callable[[ParticipantBackup, bool], None]] = None,
    on_progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> tuple[int, int]:
    """Archive every participant given, returning (created, failed_or_skipped).

    Stopping counts the participants never reached as skipped, so the
    totals still add up to the list that was accepted.
    """
    pending = list(pending)
    created = 0
    failed = 0
    for index, backup in enumerate(pending, start=1):
        if cancelled is not None and cancelled():
            failed += len(pending) - index + 1
            break
        if on_participant is not None:
            on_participant(index, len(pending), backup)
        ok = archive_participant(backup, log=log, on_progress=on_progress, cancelled=cancelled)
        if on_result is not None:
            on_result(backup, ok)
        if ok:
            created += 1
        else:
            failed += 1
    return created, failed
