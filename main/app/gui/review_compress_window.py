"""Review Video Compression window (launcher section 9: Tools).

The GUI over app/review_compress.py, and the exact equivalent of running
main/tool_compress_review_videos.py in a terminal: Scan is that script's
first phase, Convert is what answering "1" at its prompt does. All of
the deciding, converting and rolling back happens in the backend module;
this window chooses nothing on its own.

What the two phases cost is why they are separate buttons. A scan probes
every data/quiz/<attempt>/review.mp4 with ffmpeg and only reads. A
conversion re-encodes, and pays for its safety twice over: the original
is fully decoded before the rename and the new file fully decoded after
it, so nothing is deleted until the replacement has been proved to hold
the same frames. That is three ffmpeg passes per file, which is minutes
per file - hence the per-file progress bar underneath the overall one.
"""

from typing import List, Optional

from .. import review_compress as compress
from ..review_compress import ReviewVideo
from ..tool_binaries import describe_tool, find_ffmpeg
from .maintenance_window import MaintenanceWindow, ToolJob

# Row states, in the words the console tool uses for the same files.
STATE_H264 = "already H.264"
STATE_PENDING = "needs conversion"
STATE_UNKNOWN = "unreadable/unknown"
STATE_CONVERTED = "converted to H.264"
STATE_FAILED = "not converted - original kept"


class ReviewCompressWindow(MaintenanceWindow):
    TITLE = "Review Video Compression"
    HEADER = (
        "Convert quiz review videos to H.264. Scans every "
        "data/quiz/<attempt>/review.mp4, then converts only the ones that are not "
        "H.264 already."
    )
    NOTE = (
        "Raw recordings are never touched: the scan looks one level deep, so "
        "data/quiz/<attempt>/raw/ is outside this tool. Each original is kept as "
        "tmp-review.mp4 and deleted only after the new file is confirmed as H.264 with "
        "the same frame count; if anything fails, or you press Stop, the original is put "
        "back and the failed output is kept as failed-review.mp4 for you to look at and "
        "delete. Identical to running tool_compress_review_videos.py in a terminal."
    )
    COLUMNS = ("Attempt", "Codec", "State")
    SCAN_LABEL = "Scan review videos"
    RUN_LABEL = "Convert"

    def __init__(self, cfg=None):
        self._videos: List[ReviewVideo] = []
        self._states: dict = {}
        super().__init__(cfg)

    # ------------------------------------------------------------------

    def find_tool(self) -> Optional[str]:
        return find_ffmpeg()

    def tool_description(self, path: Optional[str]) -> str:
        if path is None:
            return compress.ffmpeg_missing_message()
        return describe_tool("ffmpeg", path)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def start_scan(self) -> None:
        if not self.refresh_tool_status():
            return
        if not compress.QUIZ_DIR.is_dir():
            self.append_log(f"ERROR: quiz directory does not exist: {compress.QUIZ_DIR}")
            return
        self.log_view.clear()
        self._start(self._scan_work, self._scan_finished)

    def _scan_work(self, job: ToolJob) -> List[ReviewVideo]:
        paths = compress.find_review_videos()
        job.log(
            f"Scanning {len(paths)} review video(s) under "
            f"{compress.quiz_dir_label()} ..."
        )

        def scanned(index: int, total: int, video: ReviewVideo) -> None:
            codec = video.codec if video.codec is not None else f"UNKNOWN ({video.probe_error})"
            job.log(f"[scan {index:>3}/{total}] {compress.display_path(video.path)}: {codec}")
            job.report_overall(f"Scanning {index} / {total}", index, total)
            job.report("Reading codecs", index, total)

        return compress.scan_review_videos(paths, on_scanned=scanned, cancelled=job.cancelled)

    def _scan_finished(self, videos: object) -> None:
        self._videos = list(videos)  # type: ignore[arg-type]
        self._states = {}
        self._refresh_table()

        h264 = sum(video.is_h264 for video in self._videos)
        unknown = sum(video.codec is None for video in self._videos)
        pending = self._pending()

        self.append_log("")
        self.append_log("Scan complete:")
        self.append_log(f"  review.mp4 files found: {len(self._videos)}")
        self.append_log(f"  already H.264:          {h264}")
        self.append_log(f"  need conversion:        {len(pending)}")
        self.append_log(f"  unreadable/unknown:     {unknown}")

        self._update_run_button()
        if pending:
            self.status.setText(
                f"{len(pending)} file(s) need conversion. "
                "Nothing is changed until you press Convert."
            )
        else:
            self.status.setText("No review.mp4 files need conversion.")
            self.append_log("")
            self.append_log("No review.mp4 files need conversion.")
        if unknown:
            self.append_log(
                "Unreadable/unknown files will be skipped; they will not be renamed or deleted."
            )

    # ------------------------------------------------------------------
    # Convert
    # ------------------------------------------------------------------

    def start_run(self) -> None:
        if not self.refresh_tool_status():
            return
        if not self._pending():
            return
        self._start(self._convert_work, self._convert_finished)

    def _convert_work(self, job: ToolJob) -> tuple:
        pending = self._pending()

        def started(index: int, total: int, video: ReviewVideo) -> None:
            job.log("")
            job.log(f"[convert {index:>3}/{total}] {compress.display_path(video.path)}")
            job.report_overall(
                f"Converting {index} / {total}   {video.attempt}", index - 1, total
            )

        # Collected here and applied by the GUI thread in
        # _convert_finished, rather than written into the window from
        # this one: the table is the GUI thread's to change.
        outcomes: dict = {}

        def finished(video: ReviewVideo, ok: bool) -> None:
            outcomes[video.path] = STATE_CONVERTED if ok else STATE_FAILED

        converted, failed = compress.convert_all(
            pending,
            log=job.log,
            on_file=started,
            on_result=finished,
            on_progress=job.report,
            cancelled=job.cancelled,
        )
        job.report_overall(f"Finished {len(pending)} / {len(pending)}", len(pending), len(pending))
        return converted, failed, len(pending), job.cancelled(), outcomes

    def _convert_finished(self, result: object) -> None:
        converted, failed, total, stopped, outcomes = result  # type: ignore[misc]
        self._states.update(outcomes)
        self._refresh_table()

        h264 = sum(video.is_h264 for video in self._videos)
        unknown = sum(video.codec is None for video in self._videos)
        self.append_log("")
        self.append_log("Finished:")
        self.append_log(f"  converted: {converted}")
        self.append_log(f"  failed/skipped: {failed}")
        self.append_log(f"  already H.264: {h264}")
        self.append_log(f"  unreadable/unknown: {unknown}")
        if stopped:
            self.append_log(
                "  Stopped on request. Any file that was mid-conversion was put back; "
                "a failed-review.mp4 left beside it is that attempt's discarded output "
                "and is safe to delete."
            )

        # Files this run never reached stay offered, so a stopped batch
        # can be picked up again without paying for another full scan -
        # every file is re-checked from scratch as it comes up anyway.
        # The rest of the table is this run's outcome per file; only a
        # fresh scan can restate the codecs on disk.
        self._update_run_button()
        remaining = len(self._pending())
        self.status.setText(
            f"{converted} of {total} converted, {failed} failed or skipped."
            + (f" {remaining} file(s) still to do." if remaining else "")
        )

    # ------------------------------------------------------------------

    def _update_run_button(self) -> None:
        pending = self._pending()
        self.run_btn.setText(f"Convert {len(pending)} file(s)" if pending else "Convert")
        self.run_btn.setEnabled(bool(pending) and self.find_tool() is not None)

    def _pending(self) -> List[ReviewVideo]:
        """Files still to convert: not H.264, not unreadable, and not
        already dealt with by this window's last run."""
        return [
            video
            for video in self._videos
            if video.needs_conversion and video.path not in self._states
        ]

    def _state_of(self, video: ReviewVideo) -> str:
        if video.path in self._states:
            return self._states[video.path]
        if video.codec is None:
            return STATE_UNKNOWN
        return STATE_H264 if video.is_h264 else STATE_PENDING

    def _refresh_table(self) -> None:
        self.set_rows(
            [
                (
                    video.attempt,
                    video.codec if video.codec is not None else "unknown",
                    self._state_of(video),
                )
                for video in self._videos
            ]
        )
