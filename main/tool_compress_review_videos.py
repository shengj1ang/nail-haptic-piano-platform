"""Convert quiz review videos to H.264 without touching raw recordings.

Run this script without arguments from the directory that contains it:

    python3 tool_compress_review_videos.py

The script performs two distinct phases:

1. Scan every ``data/quiz/<quiz>/review.mp4`` with ffmpeg and report its
   video codec.
2. Convert only the files that are not already H.264.

For each conversion, the existing file is first renamed to
``tmp-review.mp4``.  The conversion command is intentionally exactly:

    ffmpeg -i tmp-review.mp4 review.mp4

The temporary original is deleted only after ffmpeg succeeds and the new
``review.mp4`` has been checked with ffmpeg, confirmed as H.264, fully
decoded, and found to contain the same number of frames. If ffmpeg is not
installed or any step fails, the original file is not deleted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Run this script from the directory that contains it (main/).  No path or
# command-line argument is required from the user.
QUIZ_DIR = Path("data") / "quiz"
REVIEW_FILENAME = "review.mp4"
TMP_REVIEW_FILENAME = "tmp-review.mp4"
FAILED_REVIEW_FILENAME = "failed-review.mp4"

# Example ffmpeg stream line:
#   Stream #0:0: Video: mpeg4 (Simple Profile) (...)
VIDEO_CODEC_RE = re.compile(r"\bVideo:\s*([^,\s(]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ReviewVideo:
    path: Path
    codec: Optional[str]
    probe_error: Optional[str] = None

    @property
    def is_h264(self) -> bool:
        return self.codec is not None and self.codec.lower() == "h264"


def _path_is_occupied(path: Path) -> bool:
    """Treat dangling symlinks as occupied so they are never overwritten."""
    return path.exists() or path.is_symlink()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(QUIZ_DIR))
    except ValueError:
        return str(path)


def _probe_codec(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Read the first video stream's codec using ffmpeg itself."""
    if shutil.which("ffmpeg") is None:
        return None, "ffmpeg is not available on PATH"
    try:
        completed = subprocess.run(
            ["ffmpeg", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
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


def _decoded_frame_count(path: Path) -> tuple[Optional[int], Optional[str]]:
    """Fully decode one video stream with ffmpeg and count its frames."""
    if shutil.which("ffmpeg") is None:
        return None, "ffmpeg is not available on PATH"

    try:
        completed = subprocess.run(
            [
                "ffmpeg",
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
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "full ffmpeg decode timed out"
    except OSError as exc:
        return None, f"could not run ffmpeg: {exc}"

    if completed.returncode != 0:
        error_lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        detail = error_lines[-1] if error_lines else f"ffmpeg exited with status {completed.returncode}"
        return None, detail

    frame_count = sum(
        1
        for line in completed.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if frame_count == 0:
        return None, "ffmpeg decoded zero video frames"
    return frame_count, None


def _scan_review_videos() -> list[ReviewVideo]:
    review_paths = sorted(
        path
        for path in QUIZ_DIR.glob(f"*/{REVIEW_FILENAME}")
        if path.is_file() and not path.is_symlink()
    )

    print(f"Scanning {len(review_paths)} review video(s) under {QUIZ_DIR} ...", flush=True)
    videos: list[ReviewVideo] = []
    for index, path in enumerate(review_paths, start=1):
        codec, error = _probe_codec(path)
        videos.append(ReviewVideo(path=path, codec=codec, probe_error=error))
        codec_label = codec if codec is not None else f"UNKNOWN ({error})"
        print(
            f"[scan {index:>3}/{len(review_paths)}] {_display_path(path)}: {codec_label}",
            flush=True,
        )
    return videos


def _unused_failed_output_path(directory: Path) -> Path:
    candidate = directory / FAILED_REVIEW_FILENAME
    counter = 1
    while _path_is_occupied(candidate):
        candidate = directory / f"failed-review-{counter}.mp4"
        counter += 1
    return candidate


def _restore_original(review: Path, temporary: Path) -> tuple[bool, Optional[Path], Optional[str]]:
    """Restore tmp-review.mp4 without deleting any file during rollback."""
    if not temporary.is_file() or temporary.is_symlink():
        return False, None, "the temporary original is missing or is not a regular file"

    failed_output: Optional[Path] = None
    try:
        if _path_is_occupied(review):
            if not review.is_file() or review.is_symlink():
                return False, None, "review.mp4 exists but is not a regular file"
            failed_output = _unused_failed_output_path(review.parent)
            review.rename(failed_output)
        temporary.rename(review)
    except OSError as exc:
        return False, failed_output, str(exc)
    return True, failed_output, None


def _report_rollback(review: Path, temporary: Path) -> None:
    restored, failed_output, error = _restore_original(review, temporary)
    if restored:
        print("  The original review.mp4 was restored.", flush=True)
        if failed_output is not None:
            print(
                f"  The failed output was preserved as {_display_path(failed_output)}.",
                flush=True,
            )
        return

    print(
        "  WARNING: automatic restoration could not finish. "
        f"The original remains preserved at {_display_path(temporary)}. Error: {error}",
        flush=True,
    )


def _convert_review(video: ReviewVideo, index: int, total: int) -> bool:
    review = video.path
    temporary = review.with_name(TMP_REVIEW_FILENAME)
    label = _display_path(review)

    print(f"\n[convert {index:>3}/{total}] {label}", flush=True)

    # Check again for every file. On Windows this also covers ffmpeg.exe
    # being removed, renamed, or becoming unavailable after the first scan.
    if shutil.which("ffmpeg") is None:
        print("  SKIPPED: ffmpeg is no longer available; nothing was changed.", flush=True)
        return False

    if not review.is_file() or review.is_symlink():
        print("  SKIPPED: review.mp4 is missing or is not a regular file.", flush=True)
        return False

    # Never overwrite or delete a pre-existing temporary file.  It may be an
    # original preserved by an interrupted earlier run and must be inspected
    # manually.
    if _path_is_occupied(temporary):
        print(
            f"  SKIPPED: {_display_path(temporary)} already exists; nothing was changed.",
            flush=True,
        )
        return False

    source_frame_count, source_error = _decoded_frame_count(review)
    if source_frame_count is None:
        print(
            f"  SKIPPED: the original could not be fully decoded ({source_error}); "
            "nothing was changed.",
            flush=True,
        )
        return False

    if not review.is_file() or review.is_symlink():
        print("  SKIPPED: review.mp4 changed during validation; nothing was changed.", flush=True)
        return False

    try:
        review.rename(temporary)
    except OSError as exc:
        print(f"  FAILED: could not rename the original review file: {exc}", flush=True)
        return False

    if shutil.which("ffmpeg") is None:
        print("  FAILED: ffmpeg became unavailable after the rename.", flush=True)
        _report_rollback(review, temporary)
        return False

    try:
        # Do not add options here.  This exact command was requested so that
        # ffmpeg selects the MP4 defaults, including H.264 video.
        completed = subprocess.run(
            ["ffmpeg", "-i", TMP_REVIEW_FILENAME, REVIEW_FILENAME],
            cwd=review.parent,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, KeyboardInterrupt) as exc:
        print(f"  FAILED: ffmpeg was interrupted or could not start: {exc}", flush=True)
        _report_rollback(review, temporary)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if completed.returncode != 0 or not review.is_file() or review.is_symlink():
        print(
            f"  FAILED: ffmpeg exited with status {completed.returncode}; restoring the original.",
            flush=True,
        )
        _report_rollback(review, temporary)
        return False

    try:
        output_size = review.stat().st_size
    except OSError as exc:
        print(
            f"  FAILED: the new review.mp4 could not be inspected ({exc}); "
            "restoring the original.",
            flush=True,
        )
        _report_rollback(review, temporary)
        return False
    if output_size <= 0:
        print(
            "  FAILED: ffmpeg produced an empty review.mp4; restoring the original.",
            flush=True,
        )
        _report_rollback(review, temporary)
        return False

    new_codec, probe_error = _probe_codec(review)
    if new_codec != "h264":
        detail = new_codec if new_codec is not None else probe_error
        print(
            f"  FAILED: new review.mp4 is not confirmed as H.264 ({detail}); "
            "restoring the original.",
            flush=True,
        )
        _report_rollback(review, temporary)
        return False

    output_frame_count, output_error = _decoded_frame_count(review)
    if output_frame_count is None:
        print(
            f"  FAILED: the new review.mp4 could not be fully decoded ({output_error}); "
            "restoring the original.",
            flush=True,
        )
        _report_rollback(review, temporary)
        return False
    if output_frame_count != source_frame_count:
        print(
            f"  FAILED: frame count changed from {source_frame_count} to "
            f"{output_frame_count}; restoring the original.",
            flush=True,
        )
        _report_rollback(review, temporary)
        return False

    try:
        temporary.unlink()
    except OSError as exc:
        # Both files are valid at this point.  Keeping the temporary original
        # is safer than treating cleanup failure as permission to remove more.
        print(
            f"  CONVERTED, but the temporary original could not be deleted: {exc}",
            flush=True,
        )
        return False

    print("  OK: converted to H.264; temporary original deleted.", flush=True)
    return True


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print(
            "ERROR: ffmpeg was not found on PATH. No files were renamed, "
            "converted, or deleted.",
            file=sys.stderr,
        )
        return 2
    if not QUIZ_DIR.is_dir():
        print(f"ERROR: quiz directory does not exist: {QUIZ_DIR}", file=sys.stderr)
        return 2

    videos = _scan_review_videos()
    h264_count = sum(video.is_h264 for video in videos)
    unknown = [video for video in videos if video.codec is None]
    pending = [video for video in videos if video.codec is not None and not video.is_h264]

    print("\nScan complete:", flush=True)
    print(f"  review.mp4 files found: {len(videos)}", flush=True)
    print(f"  already H.264:          {h264_count}", flush=True)
    print(f"  need conversion:        {len(pending)}", flush=True)
    print(f"  unreadable/unknown:     {len(unknown)}", flush=True)

    if not pending:
        print("\nNo review.mp4 files need conversion. Exiting.", flush=True)
        return 0 if not unknown else 1

    print("\nChoose the next action:", flush=True)
    print("  1. Convert every non-H.264 review.mp4", flush=True)
    print("  2. Exit without changing any files", flush=True)
    while True:
        try:
            choice = input("Enter 1 or 2: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting without changing any files.", flush=True)
            return 0
        if choice == "1":
            break
        if choice == "2":
            print("Exiting without changing any files.", flush=True)
            return 0
        print("Invalid choice. Enter 1 or 2.", flush=True)

    if unknown:
        print(
            "\nUnreadable/unknown files will be skipped; they will not be renamed or deleted.",
            flush=True,
        )

    converted = 0
    failed = 0
    for index, video in enumerate(pending, start=1):
        if _convert_review(video, index, len(pending)):
            converted += 1
        else:
            failed += 1

    print("\nFinished:", flush=True)
    print(f"  converted: {converted}", flush=True)
    print(f"  failed/skipped: {failed}", flush=True)
    print(f"  already H.264: {h264_count}", flush=True)
    print(f"  unreadable/unknown: {len(unknown)}", flush=True)
    return 0 if failed == 0 and not unknown else 1


if __name__ == "__main__":
    raise SystemExit(main())
