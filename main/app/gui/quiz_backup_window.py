"""Participant ZIP Backup window (launcher section 9: Tools).

The GUI over app/quiz_backup.py, and the exact equivalent of running
main/tool_backup_quiz_to_zip.py in a terminal: Scan is that script's
preview, Create is what answering "1" at its prompt does. Which
participants are eligible, how the archive is built and tested, and the
refusal to overwrite or delete anything all live in the backend module.

The table is the preview, one row per participant, and its State column
is the reason a participant is or is not being archived - "incomplete"
with the missing trials named, "archive exists", "temporary archive
exists", or "ready". That is the question this tool exists to answer
between sessions, and it is worth more than the log for it.
"""

from typing import List, Optional

from .. import quiz_backup as backup_backend
from ..quiz_backup import BackupPlan, ParticipantBackup
from ..tool_binaries import describe_tool, find_seven_zip
from .maintenance_window import MaintenanceWindow, ToolJob

STATE_CREATED = "archive created"
STATE_FAILED = "not archived - nothing deleted"

# A participant early in the study is missing twenty-odd trials, and
# spelling all of them out in a table cell makes the column wider than
# the window. The cell answers "how far off is this one"; the log keeps
# the full list for when the answer is "nearly there".
MISSING_TRIALS_SHOWN = 5


def _missing_summary(backup: ParticipantBackup) -> str:
    missing = backup.missing_trials
    if len(missing) <= MISSING_TRIALS_SHOWN:
        return f"missing {backup_backend.format_trials(missing)}"
    shown = backup_backend.format_trials(missing[:MISSING_TRIALS_SHOWN])
    return f"missing {len(missing)} trials: {shown}, ..."


class QuizBackupWindow(MaintenanceWindow):
    TITLE = "Participant ZIP Backup"
    HEADER = (
        "Back up complete quiz participants into one tested ZIP each. Reads "
        "data/quiz and writes data/quiz-zip/Pxx.zip for every participant whose "
        "T01-T27 are all present."
    )
    NOTE = (
        "Source data is never deleted or modified, and an existing Pxx.zip is never "
        "overwritten. Each archive is written as tmp-Pxx.zip, tested with 7z, and only "
        "then renamed - a failed, stopped or interrupted temporary archive is kept for "
        "you to inspect and must be removed by hand before that participant can be "
        "retried. Incomplete participants are skipped on purpose: an early Pxx.zip would "
        "be treated as done by every later run. Identical to running "
        "tool_backup_quiz_to_zip.py in a terminal."
    )
    COLUMNS = ("Participant", "Trials", "State", "Detail")
    SCAN_LABEL = "Scan participants"
    RUN_LABEL = "Create archives"

    def __init__(self, cfg=None):
        self._plan: Optional[BackupPlan] = None
        self._states: dict = {}
        super().__init__(cfg)

    # ------------------------------------------------------------------

    def find_tool(self) -> Optional[str]:
        return find_seven_zip()

    def tool_description(self, path: Optional[str]) -> str:
        if path is None:
            return backup_backend.seven_zip_missing_message()
        return describe_tool("7z", path)

    def refresh_tool_status(self) -> bool:
        """Scanning is only a directory listing, so it stays available
        without 7z - seeing who is complete is useful on a machine that
        cannot make the archives."""
        found = super().refresh_tool_status()
        self.scan_btn.setEnabled(self._job is None)
        return found

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def start_scan(self) -> None:
        if not backup_backend.QUIZ_DIR.is_dir():
            self.append_log(f"ERROR: quiz directory does not exist: {backup_backend.QUIZ_DIR}")
            return
        destination = backup_backend.BACKUP_DIR
        if backup_backend.path_is_occupied(destination) and not destination.is_dir():
            self.append_log(
                f"ERROR: backup destination exists but is not a directory: {destination}"
            )
            return
        self.log_view.clear()
        self._start(self._scan_work, self._scan_finished)

    def _scan_work(self, job: ToolJob) -> BackupPlan:
        job.report_overall("Reading data/quiz ...", 0, 1)
        job.report("Reading participant directories", 0, 0)
        plan = backup_backend.plan_backups()
        job.report_overall("Scan complete", 1, 1)
        return plan

    def _scan_finished(self, plan: object) -> None:
        self._plan = plan  # type: ignore[assignment]
        self._states = {}
        self._refresh_table()
        assert self._plan is not None

        self.append_log("Backup preview")
        self.append_log("==============")
        if self._plan.pending:
            self.append_log("")
            self.append_log("Archives that will be created:")
            for backup in self._plan.pending:
                self.append_log(
                    f"  {backup_backend.display_path(backup.archive_path)} <- "
                    f"{len(backup.directories)} directorie(s)"
                )
        else:
            self.append_log("")
            self.append_log("No new archives are ready to be created.")
        if self._plan.existing:
            self.append_log("")
            self.append_log("Skipped because the completed archive already exists:")
            for backup in self._plan.existing:
                self.append_log(f"  - {backup_backend.display_path(backup.archive_path)}")
        if self._plan.incomplete:
            self.append_log("")
            self.append_log("Skipped because T01-T27 are not yet complete:")
            for backup in self._plan.incomplete:
                missing = backup_backend.format_trials(backup.missing_trials)
                self.append_log(f"  - {backup.participant}: missing {missing}")
        if self._plan.blocked_by_temporary:
            self.append_log("")
            self.append_log("Skipped because a temporary archive already exists:")
            for backup in self._plan.blocked_by_temporary:
                self.append_log(
                    f"  - {backup_backend.display_path(backup.temporary_path)} "
                    "(preserved; inspect or remove it manually before retrying)"
                )
        if self._plan.ignored:
            self.append_log("")
            self.append_log("Ignored non-participant directories:")
            for path in self._plan.ignored:
                self.append_log(f"  - {backup_backend.display_path(path)}")

        pending = self._remaining()
        have_tool = self.find_tool() is not None
        self._update_run_button()
        if not pending:
            self.status.setText("No archives need to be created.")
        elif not have_tool:
            self.status.setText(
                f"{len(pending)} participant(s) are ready, but 7z was not found."
            )
        else:
            self.status.setText(
                f"{len(pending)} participant(s) ready. "
                "Nothing is written until you press Create."
            )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def start_run(self) -> None:
        if not self.refresh_tool_status() or not self._remaining():
            return
        error = backup_backend.create_backup_dir()
        if error is not None:
            self.append_log(f"ERROR: {error}")
            self.status.setText(f"Could not start: {error}")
            return
        self._start(self._archive_work, self._archive_finished)

    def _archive_work(self, job: ToolJob) -> tuple:
        pending = self._remaining()

        def started(index: int, total: int, backup: ParticipantBackup) -> None:
            job.log("")
            job.log(
                f"[archive {index:>2}/{total}] {backup.participant} -> "
                f"{backup_backend.display_path(backup.archive_path)}"
            )
            job.report_overall(
                f"Archiving {index} / {total}   {backup.participant}", index - 1, total
            )

        # Collected here and applied by the GUI thread in
        # _archive_finished, rather than written into the window from
        # this one: the table is the GUI thread's to change.
        outcomes: dict = {}

        def finished(backup: ParticipantBackup, ok: bool) -> None:
            outcomes[backup.participant] = STATE_CREATED if ok else STATE_FAILED

        created, failed = backup_backend.archive_all(
            pending,
            log=job.log,
            on_participant=started,
            on_result=finished,
            on_progress=job.report,
            cancelled=job.cancelled,
        )
        job.report_overall(f"Finished {len(pending)} / {len(pending)}", len(pending), len(pending))
        return created, failed, len(pending), job.cancelled(), outcomes

    def _archive_finished(self, result: object) -> None:
        created, failed, total, stopped, outcomes = result  # type: ignore[misc]
        self._states.update(outcomes)
        self._refresh_table()
        assert self._plan is not None

        self.append_log("")
        self.append_log("Finished:")
        self.append_log(f"  archives created: {created}")
        self.append_log(f"  failed/skipped:   {failed}")
        self.append_log(f"  already existed:  {len(self._plan.existing)}")
        self.append_log(f"  incomplete:       {len(self._plan.incomplete)}")
        self.append_log("  source deleted:   0")
        if stopped:
            self.append_log(
                "  Stopped on request. Any half-written tmp-Pxx.zip is kept and must be "
                "removed by hand before that participant is retried."
            )

        # Participants this run never reached stay offered, so a stopped
        # run can be picked up again; the one it stopped inside is not,
        # because its half-written tmp-Pxx.zip has to be dealt with by
        # hand first. Scan again to re-read data/quiz from disk.
        self._update_run_button()
        remaining = len(self._remaining())
        self.status.setText(
            f"{created} of {total} archived, {failed} failed or skipped."
            + (f" {remaining} participant(s) still to do." if remaining else "")
        )

    # ------------------------------------------------------------------

    def _remaining(self) -> List[ParticipantBackup]:
        """Participants still to archive: ready, and not already attempted
        by this window's last run."""
        if self._plan is None:
            return []
        return [
            backup
            for backup in self._plan.pending
            if backup.participant not in self._states
        ]

    def _update_run_button(self) -> None:
        remaining = self._remaining()
        self.run_btn.setText(
            f"Create {len(remaining)} archive(s)" if remaining else "Create archives"
        )
        self.run_btn.setEnabled(bool(remaining) and self.find_tool() is not None)

    def _refresh_table(self) -> None:
        if self._plan is None:
            self.set_rows([])
            return
        rows: List[tuple] = []
        for backup in self._plan.participants:
            state = self._states.get(backup.participant, self._plan.state_of(backup))
            if state == "incomplete":
                detail = _missing_summary(backup)
            elif state == "archive exists":
                detail = backup_backend.display_path(backup.archive_path)
            elif state == "temporary archive exists":
                detail = f"remove {backup_backend.display_path(backup.temporary_path)} first"
            elif state == STATE_CREATED:
                detail = backup_backend.display_path(backup.archive_path)
            elif state == STATE_FAILED:
                detail = "see the log above"
            else:
                detail = f"-> {backup_backend.display_path(backup.archive_path)}"
            rows.append(
                (backup.participant, f"{len(backup.trial_numbers)} / 27", state, detail)
            )
        self.set_rows(rows)
