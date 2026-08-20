"""Convert quiz review videos to H.264 without touching raw recordings.

The tool's whole logic lives here, with no printing and no prompting, so
that the console entry point (main/tool_compress_review_videos.py) and
the launcher's Tools window (app/gui/review_compress_window.py) run the
same code and cannot drift apart. Both frontends supply two callbacks -
`log` for the running commentary and `on_progress` for a progress bar -
plus an optional `cancelled` predicate; nothing here decides how any of
that is shown.

Two distinct phases, in this order:

1. Scan every ``data/quiz/<attempt>/review.mp4`` with ffmpeg and report
   its video codec.
2. Convert only the files that are not already H.264.

For each conversion the existing file is first renamed to
``tmp-review.mp4``. The conversion command is intentionally exactly:

    ffmpeg -i tmp-review.mp4 review.mp4

No options are added to it - the MP4 defaults, including H.264 video,
are what is wanted. (Progress is read from the stderr ffmpeg writes
anyway, so watching a conversion needs no extra flag either.)

The temporary original is deleted only after ffmpeg succeeds and the new
``review.mp4`` has been checked with ffmpeg, confirmed as H.264, fully
decoded, and found to contain the same number of frames. If ffmpeg is
not installed or any step fails, the original file is not deleted.

Paths are anchored to main/ rather than to the current directory, so the
GUI works no matter where the launcher was started from; messages still
name files relative to data/quiz, which is how a person refers to them.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .tool_binaries import FFMPEG_NAMES, find_ffmpeg, missing_tool_message

MAIN_DIR = Path(__file__).resolve().parent.parent
QUIZ_DIR = MAIN_DIR / "data" / "quiz"
REVIEW_FILENAME = "review.mp4"
TMP_REVIEW_FILENAME = "tmp-review.mp4"
FAILED_REVIEW_FILENAME = "failed-review.mp4"

# Example ffmpeg stream line:
#   Stream #0:0: Video: mpeg4 (Simple Profile) (...)
VIDEO_CODEC_RE = re.compile(r"\bVideo:\s*([^,\s(]+)", re.IGNORECASE)
# Example ffmpeg progress line (stderr, rewritten in place with \r):
#   frame=  432 fps=120 q=28.0 size=    1024kB time=00:00:14.40 ...
FRAME_RE = re.compile(r"\bframe=\s*(\d+)")

PROBE_TIMEOUT = 60
DECODE_TIMEOUT = 300

# The three slow parts of one conversion, as progress-bar labels. A
# conversion spends most of its time outside ffmpeg's own encode - the
# two full decodes that make deleting the original safe cost about as
# much again - so a bar that only moved during the encode would sit at
# nothing for two thirds of the wait.
STAGE_CHECK = "Checking the original"
STAGE_ENCODE = "Converting"
STAGE_VERIFY = "Checking the result"

# Callback types. `log` gets one finished line at a time (already
# indented the way the console tool prints it); `on_progress` gets a
# stage label plus done/total, where total 0 means "unknown, show a busy
# indicator"; `cancelled` is polled between units of work.
LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]
CancelFn = Callable[[], bool]


@dataclass(frozen=True)
class ReviewVideo:
    path: Path
    codec: Optional[str]
    probe_error: Optional[str] = None

    @property
    def is_h264(self) -> bool:
        return self.codec is not None and self.codec.lower() == "h264"

    @property
    def needs_conversion(self) -> bool:
        return self.codec is not None and not self.is_h264

    @property
    def attempt(self) -> str:
        """The data/quiz/<attempt> directory name this review belongs to."""
        return self.path.parent.name


def ffmpeg_missing_message() -> str:
    return missing_tool_message("ffmpeg", FFMPEG_NAMES)


def path_is_occupied(path: Path) -> bool:
    """Treat dangling symlinks as occupied so they are never overwritten."""
    return path.exists() or path.is_symlink()


def quiz_dir_label() -> str:
    """The quiz directory as it is worth printing: "data/quiz" normally,
    and the full path if it has been pointed somewhere else (a test's
    scratch copy), where relative_to would raise rather than shorten."""
    try:
        return str(QUIZ_DIR.relative_to(MAIN_DIR))
    except ValueError:
        return str(QUIZ_DIR)


def display_path(path: Path) -> str:
    """Name a file the way a person refers to it: relative to data/quiz
    where possible, then to main/, and only otherwise in full."""
    for base in (QUIZ_DIR, MAIN_DIR):
        try:
            return str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def _noop_log(_message: str) -> None:
    pass


def probe_codec(path: Path, ffmpeg: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Read the first video stream's codec using ffmpeg itself."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if ffmpeg is None:
        return None, "ffmpeg is not available"
    try:
        completed = subprocess.run(
            [ffmpeg, "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "ffmpeg inspection timed out"
    except OSError as exc:
        return None, f"could not run ffmpeg: {exc}"

    output = f"{completed.stdout}\n{completed.stderr}"
    for line in output.splitlines():
        match = VIDEO_CODEC_RE.search(line)
        if match:
            return match.group(1).lower(), None

    error_lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = error_lines[-1] if error_lines else "ffmpeg reported no video stream"
    return None, detail


def decoded_frame_count(
    path: Path,
    ffmpeg: Optional[str] = None,
    on_count: Optional[Callable[[int], None]] = None,
    cancelled: Optional[CancelFn] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Fully decode one video stream with ffmpeg and count its frames.

    The frame hashes are read as they arrive rather than collected at the
    end, which is what lets a caller show the count climbing and stop a
    decode that is no longer wanted. stderr goes to a temporary file, not
    to a second pipe: with both on pipes, a decode that filled the stderr
    buffer while nothing was draining it would deadlock.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    if ffmpeg is None:
        return None, "ffmpeg is not available"

    command = [
        ffmpeg,
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-f",
        "framemd5",
        "-",
    ]

    stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            return None, f"could not run ffmpeg: {exc}"

        deadline = time.monotonic() + DECODE_TIMEOUT
        frame_count = 0
        interrupted: Optional[str] = None
        try:
            for line in process.stdout:  # type: ignore[union-attr]
                if line.strip() and not line.lstrip().startswith("#"):
                    frame_count += 1
                    # A per-frame callback across a thread boundary is
                    # pure overhead on a 30-minute recording; a few
                    # updates a second is all a bar can show.
                    if on_count is not None and frame_count % 25 == 0:
                        on_count(frame_count)
                if cancelled is not None and cancelled():
                    interrupted = "cancelled"
                    break
                if time.monotonic() > deadline:
                    interrupted = "full ffmpeg decode timed out"
                    break
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()

        if interrupted is not None:
            return None, interrupted

        if process.returncode != 0:
            stderr_file.seek(0)
            error_lines = [
                line.strip() for line in stderr_file.read().splitlines() if line.strip()
            ]
            detail = (
                error_lines[-1]
                if error_lines
                else f"ffmpeg exited with status {process.returncode}"
            )
            return None, detail

        if on_count is not None:
            on_count(frame_count)
        if frame_count == 0:
            return None, "ffmpeg decoded zero video frames"
        return frame_count, None
    finally:
        stderr_file.close()


def find_review_videos(quiz_dir: Optional[Path] = None) -> list[Path]:
    """Every data/quiz/<attempt>/review.mp4, in directory order.

    The glob is one level deep on purpose: it cannot reach
    data/quiz/<attempt>/raw/, so the original recordings are outside
    this tool's scope no matter what it is asked to do.

    QUIZ_DIR is read now rather than baked into the signature, so a test
    can point the whole module at a scratch copy of data/quiz.
    """
    quiz_dir = QUIZ_DIR if quiz_dir is None else quiz_dir
    if not quiz_dir.is_dir():
        return []
    return sorted(
        path
        for path in quiz_dir.glob(f"*/{REVIEW_FILENAME}")
        if path.is_file() and not path.is_symlink()
    )


def scan_review_videos(
    paths: Optional[Sequence[Path]] = None,
    on_scanned: Optional[Callable[[int, int, ReviewVideo], None]] = None,
    cancelled: Optional[CancelFn] = None,
) -> list[ReviewVideo]:
    """Probe each review video's codec, reporting one result at a time.

    Stops early and returns what it has if `cancelled` starts returning
    True: a scan only reads, so a partial one costs nothing.
    """
    review_paths = list(find_review_videos()) if paths is None else list(paths)
    ffmpeg = find_ffmpeg()
    videos: list[ReviewVideo] = []
    for index, path in enumerate(review_paths, start=1):
        if cancelled is not None and cancelled():
            break
        codec, error = probe_codec(path, ffmpeg)
        video = ReviewVideo(path=path, codec=codec, probe_error=error)
        videos.append(video)
        if on_scanned is not None:
            on_scanned(index, len(review_paths), video)
    return videos


def _unused_failed_output_path(directory: Path) -> Path:
    candidate = directory / FAILED_REVIEW_FILENAME
    counter = 1
    while path_is_occupied(candidate):
        candidate = directory / f"failed-review-{counter}.mp4"
        counter += 1
    return candidate


def _restore_original(review: Path, temporary: Path) -> tuple[bool, Optional[Path], Optional[str]]:
    """Restore tmp-review.mp4 without deleting any file during rollback."""
    if not temporary.is_file() or temporary.is_symlink():
        return False, None, "the temporary original is missing or is not a regular file"

    failed_output: Optional[Path] = None
    try:
        if path_is_occupied(review):
            if not review.is_file() or review.is_symlink():
                return False, None, "review.mp4 exists but is not a regular file"
            failed_output = _unused_failed_output_path(review.parent)
            review.rename(failed_output)
        temporary.rename(review)
    except OSError as exc:
        return False, failed_output, str(exc)
    return True, failed_output, None


def _report_rollback(review: Path, temporary: Path, log: LogFn) -> None:
    restored, failed_output, error = _restore_original(review, temporary)
    if restored:
        log("  The original review.mp4 was restored.")
        if failed_output is not None:
            log(f"  The failed output was preserved as {display_path(failed_output)}.")
        return

    log(
        "  WARNING: automatic restoration could not finish. "
        f"The original remains preserved at {display_path(temporary)}. Error: {error}"
    )


def _run_conversion(
    review: Path,
    ffmpeg: str,
    total_frames: int,
    on_progress: Optional[ProgressFn],
    cancelled: Optional[CancelFn],
) -> int:
    """Run the conversion in the attempt directory and return ffmpeg's exit code.

    With no progress callback and no cancellation to honour, ffmpeg keeps
    the console's own stdout/stderr - that is what a person running the
    script from a terminal expects to see. A caller that wants either
    reads stderr instead, which ffmpeg writes with or without being
    asked, so the command itself is identical in both cases.

    A cancelled conversion is simply a failed one: ffmpeg is terminated,
    returns non-zero, and the caller's usual restore-the-original path
    takes it from there.
    """
    command = [ffmpeg, "-i", TMP_REVIEW_FILENAME, REVIEW_FILENAME]
    if on_progress is None and cancelled is None:
        completed = subprocess.run(
            command,
            cwd=review.parent,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode

    process = subprocess.Popen(
        command,
        cwd=review.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        for line in process.stderr:  # type: ignore[union-attr]
            match = FRAME_RE.search(line)
            if match is not None and on_progress is not None:
                on_progress(STAGE_ENCODE, min(int(match.group(1)), total_frames), total_frames)
            if cancelled is not None and cancelled():
                process.terminate()
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stderr is not None:
            process.stderr.close()
    return process.returncode


def convert_review(
    video: ReviewVideo,
    log: LogFn = _noop_log,
    on_progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> bool:
    """Convert one review.mp4 to H.264, or change nothing at all.

    Every early return leaves the attempt directory exactly as it was
    found. Past the rename, every failure path restores the original
    before returning - the temporary copy is only unlinked once the new
    file has been proved equivalent.
    """
    review = video.path
    temporary = review.with_name(TMP_REVIEW_FILENAME)

    # Check again for every file. On Windows this also covers ffmpeg.exe
    # being removed, renamed, or becoming unavailable after the first scan.
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        log("  SKIPPED: ffmpeg is no longer available; nothing was changed.")
        return False

    if not review.is_file() or review.is_symlink():
        log("  SKIPPED: review.mp4 is missing or is not a regular file.")
        return False

    # Never overwrite or delete a pre-existing temporary file.  It may be an
    # original preserved by an interrupted earlier run and must be inspected
    # manually.
    if path_is_occupied(temporary):
        log(f"  SKIPPED: {display_path(temporary)} already exists; nothing was changed.")
        return False

    if cancelled is not None and cancelled():
        log("  SKIPPED: stopped before this file; nothing was changed.")
        return False

    if on_progress is not None:
        on_progress(STAGE_CHECK, 0, 0)
    source_frame_count, source_error = decoded_frame_count(
        review,
        ffmpeg,
        on_count=(lambda done: on_progress(STAGE_CHECK, done, 0)) if on_progress else None,
        cancelled=cancelled,
    )
    if source_frame_count is None:
        log(
            f"  SKIPPED: the original could not be fully decoded ({source_error}); "
            "nothing was changed."
        )
        return False

    if not review.is_file() or review.is_symlink():
        log("  SKIPPED: review.mp4 changed during validation; nothing was changed.")
        return False

    if cancelled is not None and cancelled():
        log("  SKIPPED: stopped before this file; nothing was changed.")
        return False

    try:
        review.rename(temporary)
    except OSError as exc:
        log(f"  FAILED: could not rename the original review file: {exc}")
        return False

    if find_ffmpeg() is None:
        log("  FAILED: ffmpeg became unavailable after the rename.")
        _report_rollback(review, temporary, log)
        return False

    if on_progress is not None:
        on_progress(STAGE_ENCODE, 0, source_frame_count)
    try:
        # Do not add options here.  This exact command was requested so that
        # ffmpeg selects the MP4 defaults, including H.264 video.
        returncode = _run_conversion(review, ffmpeg, source_frame_count, on_progress, cancelled)
    except (OSError, KeyboardInterrupt) as exc:
        log(f"  FAILED: ffmpeg was interrupted or could not start: {exc}")
        _report_rollback(review, temporary, log)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if returncode != 0 or not review.is_file() or review.is_symlink():
        if cancelled is not None and cancelled():
            log("  STOPPED: the conversion was cancelled; restoring the original.")
        else:
            log(f"  FAILED: ffmpeg exited with status {returncode}; restoring the original.")
        _report_rollback(review, temporary, log)
        return False

    try:
        output_size = review.stat().st_size
    except OSError as exc:
        log(
            f"  FAILED: the new review.mp4 could not be inspected ({exc}); "
            "restoring the original."
        )
        _report_rollback(review, temporary, log)
        return False
    if output_size <= 0:
        log("  FAILED: ffmpeg produced an empty review.mp4; restoring the original.")
        _report_rollback(review, temporary, log)
        return False

    if on_progress is not None:
        on_progress(STAGE_VERIFY, 0, source_frame_count)
    new_codec, probe_error = probe_codec(review, ffmpeg)
    if new_codec != "h264":
        detail = new_codec if new_codec is not None else probe_error
        log(
            f"  FAILED: new review.mp4 is not confirmed as H.264 ({detail}); "
            "restoring the original."
        )
        _report_rollback(review, temporary, log)
        return False

    output_frame_count, output_error = decoded_frame_count(
        review,
        ffmpeg,
        on_count=(
            (lambda done: on_progress(STAGE_VERIFY, done, source_frame_count))
            if on_progress
            else None
        ),
        # Not cancellable: the new file has to be proved equivalent before
        # the original may go, and stopping here would only mean rolling
        # back work that has already been paid for.
    )
    if output_frame_count is None:
        log(
            f"  FAILED: the new review.mp4 could not be fully decoded ({output_error}); "
            "restoring the original."
        )
        _report_rollback(review, temporary, log)
        return False
    if output_frame_count != source_frame_count:
        log(
            f"  FAILED: frame count changed from {source_frame_count} to "
            f"{output_frame_count}; restoring the original."
        )
        _report_rollback(review, temporary, log)
        return False

    try:
        temporary.unlink()
    except OSError as exc:
        # Both files are valid at this point.  Keeping the temporary original
        # is safer than treating cleanup failure as permission to remove more.
        log(f"  CONVERTED, but the temporary original could not be deleted: {exc}")
        return False

    log("  OK: converted to H.264; temporary original deleted.")
    return True


def convert_all(
    pending: Iterable[ReviewVideo],
    log: LogFn = _noop_log,
    on_file: Optional[Callable[[int, int, ReviewVideo], None]] = None,
    on_result: Optional[Callable[[ReviewVideo, bool], None]] = None,
    on_progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> tuple[int, int]:
    """Convert every video given, returning (converted, failed_or_skipped).

    Stopping counts the files never reached as skipped, so the totals
    still add up to the list that was accepted - a run that stopped
    early should not read as if it had less to do.
    """
    pending = list(pending)
    converted = 0
    failed = 0
    for index, video in enumerate(pending, start=1):
        if cancelled is not None and cancelled():
            failed += len(pending) - index + 1
            break
        if on_file is not None:
            on_file(index, len(pending), video)
        ok = convert_review(video, log=log, on_progress=on_progress, cancelled=cancelled)
        if on_result is not None:
            on_result(video, ok)
        if ok:
            converted += 1
        else:
            failed += 1
    return converted, failed
