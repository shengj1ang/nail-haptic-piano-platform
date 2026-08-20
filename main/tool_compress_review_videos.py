"""Convert quiz review videos to H.264 without touching raw recordings.

Console frontend. Run it without arguments:

    python3 tool_compress_review_videos.py

Every decision, check and rollback lives in app/review_compress.py - the
same module the launcher's "Review Video Compression" window drives - so
running this script and clicking that button do exactly the same thing to
the same files. This file only prints what that module reports and asks
the one confirmation question.

What it does, in short: scan every ``data/quiz/<attempt>/review.mp4``,
report its codec, then (after you confirm) convert only the files that
are not already H.264, with the original preserved until the new file has
been proved to have the same frames. See app/review_compress.py for the
full contract, and README.md for the operator's version.

ffmpeg is found in main/runtime/bin first and on PATH second, so a
Windows machine with ffmpeg.exe dropped into that folder needs nothing
installed system-wide (see app/tool_binaries.py).
"""

from __future__ import annotations

import sys

from app.review_compress import (
    QUIZ_DIR,
    ReviewVideo,
    convert_all,
    display_path,
    ffmpeg_missing_message,
    find_review_videos,
    quiz_dir_label,
    scan_review_videos,
)
from app.tool_binaries import find_ffmpeg


def _scan_line(index: int, total: int, video: ReviewVideo) -> None:
    codec_label = video.codec if video.codec is not None else f"UNKNOWN ({video.probe_error})"
    print(f"[scan {index:>3}/{total}] {display_path(video.path)}: {codec_label}", flush=True)


def _convert_heading(index: int, total: int, video: ReviewVideo) -> None:
    print(f"\n[convert {index:>3}/{total}] {display_path(video.path)}", flush=True)


def _confirm() -> bool:
    print("\nChoose the next action:", flush=True)
    print("  1. Convert every non-H.264 review.mp4", flush=True)
    print("  2. Exit without changing any files", flush=True)
    while True:
        try:
            choice = input("Enter 1 or 2: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting without changing any files.", flush=True)
            return False
        if choice == "1":
            return True
        if choice == "2":
            print("Exiting without changing any files.", flush=True)
            return False
        print("Invalid choice. Enter 1 or 2.", flush=True)


def main() -> int:
    if find_ffmpeg() is None:
        print(
            f"ERROR: {ffmpeg_missing_message()}\n"
            "No files were renamed, converted, or deleted.",
            file=sys.stderr,
        )
        return 2
    if not QUIZ_DIR.is_dir():
        print(f"ERROR: quiz directory does not exist: {QUIZ_DIR}", file=sys.stderr)
        return 2

    paths = find_review_videos()
    print(f"Scanning {len(paths)} review video(s) under {quiz_dir_label()} ...", flush=True)
    videos = scan_review_videos(paths, on_scanned=_scan_line)

    h264_count = sum(video.is_h264 for video in videos)
    unknown = [video for video in videos if video.codec is None]
    pending = [video for video in videos if video.needs_conversion]

    print("\nScan complete:", flush=True)
    print(f"  review.mp4 files found: {len(videos)}", flush=True)
    print(f"  already H.264:          {h264_count}", flush=True)
    print(f"  need conversion:        {len(pending)}", flush=True)
    print(f"  unreadable/unknown:     {len(unknown)}", flush=True)

    if not pending:
        print("\nNo review.mp4 files need conversion. Exiting.", flush=True)
        return 0 if not unknown else 1

    if not _confirm():
        return 0

    if unknown:
        print(
            "\nUnreadable/unknown files will be skipped; they will not be renamed or deleted.",
            flush=True,
        )

    converted, failed = convert_all(
        pending,
        log=lambda message: print(message, flush=True),
        on_file=_convert_heading,
    )

    print("\nFinished:", flush=True)
    print(f"  converted: {converted}", flush=True)
    print(f"  failed/skipped: {failed}", flush=True)
    print(f"  already H.264: {h264_count}", flush=True)
    print(f"  unreadable/unknown: {len(unknown)}", flush=True)
    return 0 if failed == 0 and not unknown else 1


if __name__ == "__main__":
    raise SystemExit(main())
