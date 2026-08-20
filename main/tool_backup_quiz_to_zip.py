"""Back up complete quiz participants into separate ZIP archives.

Console frontend. Run it without arguments:

    python3 tool_backup_quiz_to_zip.py

The eligibility rules, the 7z commands and the never-overwrite/never-
delete guarantees all live in app/quiz_backup.py - the same module the
launcher's "Participant ZIP Backup" window drives - so this script and
that button behave identically. This file only prints the plan and asks
the one confirmation question.

What it does, in short: read participant trial directories from
``data/quiz`` and write one tested ``data/quiz-zip/Pxx.zip`` per
participant whose T01-T27 are all present, leaving every source
directory untouched. See app/quiz_backup.py for the full contract, and
README.md for the operator's version.

7z is found in main/runtime/bin first and on PATH second, and 7z, 7zz or
7za are all accepted, so this now runs on Windows and Linux as well as
macOS (see app/tool_binaries.py).
"""

from __future__ import annotations

import sys

from app.quiz_backup import (
    BACKUP_DIR,
    QUIZ_DIR,
    BackupPlan,
    ParticipantBackup,
    archive_all,
    create_backup_dir,
    display_path,
    format_trials,
    path_is_occupied,
    plan_backups,
    seven_zip_missing_message,
)
from app.tool_binaries import find_seven_zip


def _print_preview(plan: BackupPlan) -> None:
    print("\nBackup preview", flush=True)
    print("==============", flush=True)

    if plan.pending:
        print("\nArchives that will be created:", flush=True)
        for backup in plan.pending:
            print(
                f"  {display_path(backup.archive_path)} <- "
                f"{len(backup.directories)} directorie(s)",
                flush=True,
            )
            for directory in backup.directories:
                print(f"    - {display_path(directory)}", flush=True)
    else:
        print("\nNo new archives are ready to be created.", flush=True)

    if plan.existing:
        print("\nSkipped because the completed archive already exists:", flush=True)
        for backup in plan.existing:
            print(f"  - {display_path(backup.archive_path)}", flush=True)

    if plan.incomplete:
        print("\nSkipped because T01-T27 are not yet complete:", flush=True)
        for backup in plan.incomplete:
            print(
                f"  - {backup.participant}: missing {format_trials(backup.missing_trials)}",
                flush=True,
            )

    if plan.blocked_by_temporary:
        print("\nSkipped because a temporary archive already exists:", flush=True)
        for backup in plan.blocked_by_temporary:
            print(
                f"  - {display_path(backup.temporary_path)} "
                "(preserved; inspect or remove it manually before retrying)",
                flush=True,
            )

    if plan.ignored:
        print("\nIgnored non-participant directories:", flush=True)
        for path in plan.ignored:
            print(f"  - {display_path(path)}", flush=True)


def _archive_heading(index: int, total: int, backup: ParticipantBackup) -> None:
    print(
        f"\n[archive {index:>2}/{total}] {backup.participant} -> "
        f"{display_path(backup.archive_path)}",
        flush=True,
    )


def _confirm() -> bool:
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
    if find_seven_zip() is None:
        print(
            f"ERROR: {seven_zip_missing_message()}\nNo files were changed.",
            file=sys.stderr,
        )
        return 2
    if not QUIZ_DIR.is_dir():
        print(f"ERROR: quiz directory does not exist: {QUIZ_DIR}", file=sys.stderr)
        return 2
    if path_is_occupied(BACKUP_DIR) and not BACKUP_DIR.is_dir():
        print(
            f"ERROR: backup destination exists but is not a directory: {BACKUP_DIR}. "
            "No files were changed.",
            file=sys.stderr,
        )
        return 2

    plan = plan_backups()
    _print_preview(plan)

    if not plan.pending:
        print("\nNo archives need to be created.", flush=True)
        return 0
    if not _confirm():
        return 0

    error = create_backup_dir()
    if error is not None:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    created, failed = archive_all(
        plan.pending,
        log=lambda message: print(message, flush=True),
        on_participant=_archive_heading,
    )

    print("\nFinished:", flush=True)
    print(f"  archives created: {created}", flush=True)
    print(f"  failed/skipped:   {failed}", flush=True)
    print(f"  already existed:  {len(plan.existing)}", flush=True)
    print(f"  incomplete:       {len(plan.incomplete)}", flush=True)
    print("  source deleted:   0", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
