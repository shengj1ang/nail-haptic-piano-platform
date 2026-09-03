"""Video/MIDI sync anchoring via the recorded LED flash.

The software start-time stamps in sync.json (video_start_time,
midi_start_time) are not enough to line MIDI events up with video frames:
the camera pipeline delivers frames with an unrecorded latency (tens to
hundreds of ms, different every session), so "frame 0 was captured at
video_start_time" is systematically wrong. Measured across the P01/P02
pilot data, the real per-trial error ranges from -0.2 s to +0.55 s -
easily a dozen frames, enough to read the wrong moment for every
keypress.

The sync LED flash fixes this because it lives on both clocks at once:
student_quiz.py records the wall-clock moment it switched the LED on
(sync.json's led_on_time), and the flash itself is visible in the video.
Detecting the flash frame gives one exact (frame index, wall time) pair,
from which every absolute event timestamp maps to its true frame:

    frame(t) = flash_on_frame + (t - led_on_time) * fps

Detection: the flash lights the first five white keys for visibility, but
the first key (student_quiz.SYNC_LED_PIXELS[0] = key_id 0) is switched
first and is the only one detection looks at, so its pixel region comes
straight from the keyboard profile's key map. A box matched filter with
the known flash duration (led_off_time - led_on_time) slides over that
region's per-frame brightness, searching only within ±0.5 s of the
software expectation (so the later cue LEDs can't be mistaken for the
flash), and the edges are refined on the brightness derivative. Two
self-checks decide whether the detection is trusted: the detected pulse
must match the software duration, and the offsets implied by the on and
off edges must agree.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .keyboard.template import KeyboardTemplate
from .music_recording import SyncInfo
from .profiles import DATA_DIR

SYNC_KEY_ID = 0  # the sync LED sits on the first white key (see student_quiz.py)
SEARCH_WINDOW_S = 0.5  # how far from the software-expected flash time to search
PULSE_LENGTH_TOLERANCE_S = 0.15  # detected vs software flash duration
EDGE_AGREEMENT_TOLERANCE_S = 0.15  # on-edge vs off-edge implied offsets
DETECTION_FILENAME = "sync_detect.json"  # audit trail of the anchor a video analysis actually used
# The confirmed alignment for a recording: written by the batch auto-align
# button ("auto"), by the manual frame-picking window ("manual" - see
# app/gui/video_sync_window.py), or by analyze_recording itself when its
# fresh detection passes the self-checks. Once present, every later
# analysis reuses it instead of re-detecting, so a manual correction is
# never silently overwritten.
ALIGN_FILENAME = "sync_align.json"
# How far a saved alignment's frame 0 may sit from the recording's own
# start time, on top of the recording's length, before it is treated as
# belonging to a different recording. Generous on purpose: rejecting a
# real manual correction would be worse than the drift it guards against.
ALIGNMENT_DRIFT_MARGIN_S = 5.0

log = logging.getLogger("app.sync_led")


@dataclass
class SyncAnchor:
    """How to convert an absolute wall-clock timestamp into a video frame
    index, plus the audit trail of how that conversion was obtained."""

    # "led": flash freshly detected and trusted; "auto"/"manual": loaded
    # from a saved sync_align.json (auto-align button / manual picking
    # window); "start-times": software-timestamp fallback, no alignment.
    method: str
    fps: float
    frame0_epoch: float  # wall-clock time.time() of frame 0's *content*
    flash_on_frame: Optional[int] = None
    flash_off_frame: Optional[int] = None
    detected_pulse_s: Optional[float] = None
    expected_pulse_s: Optional[float] = None
    # How far the detected flash sits from the software-clock expectation -
    # i.e. the systematic error the fallback method would have made.
    led_vs_start_times_offset_s: Optional[float] = None
    reason: str = ""  # why the fallback was used, empty when method == "led"

    def frame_for(self, abs_time: float) -> int:
        return int(round((abs_time - self.frame0_epoch) * self.fps))

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> "SyncAnchor":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))


def anchor_from_flash_frame(
    flash_on_frame: int, fps: float, sync: SyncInfo, method: str
) -> SyncAnchor:
    """Build an anchor from a known (picked or detected) flash frame: that
    frame's content was captured at the wall-clock moment sync.led_on_time."""
    return SyncAnchor(
        method=method,
        fps=fps,
        frame0_epoch=sync.led_on_time - flash_on_frame / fps,
        flash_on_frame=flash_on_frame,
        led_vs_start_times_offset_s=flash_on_frame / fps - (sync.led_on_time - sync.video_start_time),
    )


def load_sync_alignment(raw_dir: Path) -> Optional[SyncAnchor]:
    """The saved (auto or manual) alignment for a recording, if any."""
    path = Path(raw_dir) / ALIGN_FILENAME
    if not path.exists():
        return None
    try:
        return SyncAnchor.load(path)
    except Exception:
        return None


def alignment_belongs_to(anchor: SyncAnchor, sync: SyncInfo, video_path: Path) -> bool:
    """Could this saved alignment have been made for this recording?

    A saved alignment outranks everything, which is right for a manual
    correction and catastrophic for one left behind by a *different*
    recording: every event maps to a frame index far outside the video,
    no hands are found there, and the pass returns "no finger" for the
    whole session without erroring. That happened - see doc/REMOTE_GUIDANCE.md
    trap 11 - when a session folder was reused and kept the previous
    session's sync_align.json, 145 seconds adrift of a 40-second video.

    The test is deliberately loose. Frame 0's content can legitimately
    predate video_start_time (the LED flash resolves a real offset of up
    to a second or so), so only a gap larger than the recording itself,
    plus a margin, is treated as proof that the two do not belong
    together. A genuine alignment can never be that far out."""
    if not sync.video_start_time or not anchor.frame0_epoch:
        return True
    drift_s = abs(anchor.frame0_epoch - sync.video_start_time)
    duration_s = _video_duration_s(video_path)
    if duration_s is None:
        return drift_s <= ALIGNMENT_DRIFT_MARGIN_S
    return drift_s <= duration_s + ALIGNMENT_DRIFT_MARGIN_S


def _video_duration_s(video_path: Path) -> Optional[float]:
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    finally:
        cap.release()
    if fps <= 0 or frames <= 0:
        return None
    return frames / fps


def resolve_sync_anchor(
    video_path: Path,
    sync: SyncInfo,
    keyboard_profile_name: str,
    data_dir: Path = DATA_DIR,
) -> SyncAnchor:
    """The anchor an analysis should use: a saved sync_align.json wins
    (manual corrections must never be overridden by re-detection);
    otherwise detect the flash now and - when the detection passes its
    self-checks - persist it as an "auto" alignment for next time.

    A saved alignment that cannot belong to this recording is the one
    exception - it is ignored rather than obeyed, and says so."""
    raw_dir = Path(video_path).parent
    saved = load_sync_alignment(raw_dir)
    if saved is not None:
        if alignment_belongs_to(saved, sync, video_path):
            return saved
        log.warning(
            "%s holds an alignment for a different recording (frame 0 at %.3f, this video starts at "
            "%.3f) - ignoring it and re-detecting",
            raw_dir / ALIGN_FILENAME,
            saved.frame0_epoch,
            sync.video_start_time,
        )
    anchor = compute_sync_anchor(video_path, sync, keyboard_profile_name, data_dir)
    if anchor.method == "led":
        auto = SyncAnchor(**{**asdict(anchor), "method": "auto"})
        auto.save(raw_dir / ALIGN_FILENAME)
        return auto
    return anchor


def _key_region_mask(keyboard_profile_name: str, data_dir: Path) -> Optional[np.ndarray]:
    try:
        template = KeyboardTemplate.load(data_dir / keyboard_profile_name / "keyboard_template.json")
    except Exception:
        return None
    if template.key_map is None:
        return None
    mask = (template.key_map == SYNC_KEY_ID + 1).astype(np.uint8)  # key_map stores id+1
    if not mask.any():
        return None
    # Grow the region upward/side so the LED strip's glow above the key is
    # included, not just the key surface it spills onto.
    return cv2.dilate(mask, np.ones((25, 9), np.uint8)) > 0


def compute_sync_anchor(
    video_path: Path,
    sync: SyncInfo,
    keyboard_profile_name: str,
    data_dir: Path = DATA_DIR,
) -> SyncAnchor:
    """Always returns a usable anchor: LED-based when the flash can be
    found and passes the self-checks, otherwise the start-times fallback
    (frame0_epoch = video_start_time) with reason set."""

    def fallback(reason: str) -> SyncAnchor:
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        return SyncAnchor(
            method="start-times", fps=fps, frame0_epoch=sync.video_start_time, reason=reason
        )

    if not sync.led_on_time or not sync.led_off_time:
        return fallback("no LED flash times in sync.json")
    expected_pulse_s = sync.led_off_time - sync.led_on_time
    if not (0.1 <= expected_pulse_s <= 3.0):
        return fallback(f"implausible software flash duration {expected_pulse_s:.3f}s")

    mask = _key_region_mask(keyboard_profile_name, data_dir)
    if mask is None:
        return fallback(f"no key map for profile {keyboard_profile_name}")

    expected_on_s = sync.led_on_time - sync.video_start_time

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    series = []
    n_frames = int((max(expected_on_s, 0) + expected_pulse_s + SEARCH_WINDOW_S + 1.0) * fps)
    for _ in range(n_frames):
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        series.append(float(gray[mask].mean()))
    cap.release()

    s = np.array(series)
    w = int(round(expected_pulse_s * fps))
    margin = 6
    if len(s) < w + 2 * margin:
        return fallback("video too short to search for the flash")

    # Box matched filter: the flash is the window of the known width whose
    # inside is brightest relative to just before and just after it.
    k_lo = max(int((expected_on_s - SEARCH_WINDOW_S) * fps), 1)
    k_hi = min(int((expected_on_s + SEARCH_WINDOW_S) * fps), len(s) - w - margin)
    if k_hi <= k_lo:
        return fallback("search window falls outside the video")
    best_k, best_score = None, -np.inf
    for k in range(k_lo, k_hi):
        inside = s[k:k + w].mean()
        before = s[max(k - margin, 0):k]
        after = s[k + w:k + w + margin]
        outside = (before.mean() + after.mean()) / 2 if len(before) else after.mean()
        score = inside - outside
        if score > best_score:
            best_score, best_k = score, k

    # Refine both edges on the frame-to-frame brightness derivative.
    d = np.diff(s)
    lo = max(best_k - 4, 1)
    on_frame = lo + int(np.argmax(d[lo - 1:best_k + 4 - 1]))
    lo2 = best_k + w - 4
    off_frame = lo2 + int(np.argmin(d[lo2 - 1:min(best_k + w + 4, len(d) + 1) - 1]))

    detected_pulse_s = (off_frame - on_frame) / fps
    on_offset = on_frame / fps - (sync.led_on_time - sync.video_start_time)
    off_offset = off_frame / fps - (sync.led_off_time - sync.video_start_time)

    if abs(detected_pulse_s - expected_pulse_s) > PULSE_LENGTH_TOLERANCE_S:
        return fallback(
            f"detected pulse {detected_pulse_s:.3f}s doesn't match software {expected_pulse_s:.3f}s"
        )
    if abs(on_offset - off_offset) > EDGE_AGREEMENT_TOLERANCE_S:
        return fallback(
            f"on/off edges disagree ({on_offset:+.3f}s vs {off_offset:+.3f}s)"
        )

    return SyncAnchor(
        method="led",
        fps=fps,
        frame0_epoch=sync.led_on_time - on_frame / fps,
        flash_on_frame=on_frame,
        flash_off_frame=off_frame,
        detected_pulse_s=detected_pulse_s,
        expected_pulse_s=expected_pulse_s,
        led_vs_start_times_offset_s=on_offset,
    )


def event_epoch_time(t: float, sync: Optional[SyncInfo]) -> float:
    """Absolute wall-clock time of a logged event timestamp. All logs
    written since the epoch-timestamp migration store absolute times
    already; a small value (clearly not an epoch) is a legacy
    recorder-relative timestamp and is shifted by the MIDI clock's start."""
    if t >= 1e6 or sync is None:
        return t
    return sync.midi_start_time + t
