"""Convert quiz review videos to H.264 without touching raw recordings.

The script performs two distinct phases:

1. Scan every ``data/quiz/<quiz>/review.mp4`` with ffmpeg and report its
   video codec.
2. Convert only the files that are not already H.264.

For each conversion, the existing file is first renamed to
``tmp-review.mp4``.  The conversion command is intentionally exactly:

    ffmpeg -i tmp-review.mp4 review.mp4

The temporary original is deleted only after ffmpeg succeeds and the new
``review.mp4`` has been checked with ffmpeg and confirmed as H.264.  If the
conversion fails, the original file is restored.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
QUIZ_DIR = SCRIPT_DIR / "data" / "quiz"
REVIEW_FILENAME = "review.mp4"
TMP_REVIEW_FILENAME = "tmp-review.mp4"

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


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(QUIZ_DIR))
    except ValueError:
        return str(path)


def _probe_codec(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Read the first video stream's codec using ffmpeg itself."""
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


def _remove_incomplete_output_and_restore_original(review: Path, temporary: Path) -> None:
    """Roll back only files whose ownership is unambiguous in this run."""
    if review.exists():
        review.unlink()
    if temporary.exists() and not review.exists():
        temporary.rename(review)


def _convert_review(video: ReviewVideo, index: int, total: int) -> bool:
    review = video.path
    temporary = review.with_name(TMP_REVIEW_FILENAME)
    label = _display_path(review)

    print(f"\n[convert {index:>3}/{total}] {label}", flush=True)

    # Never overwrite or delete a pre-existing temporary file.  It may be an
    # original preserved by an interrupted earlier run and must be inspected
    # manually.
    if temporary.exists():
        print(
            f"  SKIPPED: {_display_path(temporary)} already exists; nothing was changed.",
            flush=True,
        )
        return False

    try:
        review.rename(temporary)
    except OSError as exc:
        print(f"  FAILED: could not rename the original review file: {exc}", flush=True)
        return False

    try:
        # Do not add options here.  This exact command was requested so that
        # ffmpeg selects the MP4 defaults, including H.264 video.
        completed = subprocess.run(
            ["ffmpeg", "-i", TMP_REVIEW_FILENAME, REVIEW_FILENAME],
            cwd=review.parent,
            check=False,
        )
    except (OSError, KeyboardInterrupt) as exc:
        print(f"  FAILED: ffmpeg was interrupted or could not start: {exc}", flush=True)
        _remove_incomplete_output_and_restore_original(review, temporary)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False

    if completed.returncode != 0 or not review.is_file():
        print(
            f"  FAILED: ffmpeg exited with status {completed.returncode}; restoring the original.",
            flush=True,
        )
        _remove_incomplete_output_and_restore_original(review, temporary)
        return False

    new_codec, probe_error = _probe_codec(review)
    if new_codec != "h264":
        detail = new_codec if new_codec is not None else probe_error
        print(
            f"  FAILED: new review.mp4 is not confirmed as H.264 ({detail}); "
            "restoring the original.",
            flush=True,
        )
        _remove_incomplete_output_and_restore_original(review, temporary)
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert data/quiz/*/review.mp4 files to H.264 safely."
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="report codecs without renaming or converting any files",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg was not found on PATH.", file=sys.stderr)
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

    if args.scan_only:
        print("\nScan-only mode: no files were changed.", flush=True)
        return 0 if not unknown else 1

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
