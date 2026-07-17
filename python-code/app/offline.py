"""Offline finger-accuracy analysis.

Given a recorded video and a MIDI event log (see app.midi -
MidiEvent / save_midi_log / load_midi_log), this recomputes the same
"which finger pressed this key" result that test_finger_accuracy.py produces live, without
a camera or MIDI device connected. This is the entry point for the
"record now, analyze later" workflow: a session's MIDI notes and video get
captured during the experiment, and finger accuracy is computed afterward.

The MIDI log's timestamps are absolute wall-clock time.time() values.
sync_path points at the session's sync.json (see
app.music_recording.SyncInfo); from it and the video, app.sync_led builds
the frame mapping - anchored on the recorded LED flash when it can be
found in the footage (which absorbs the camera pipeline's unrecorded
latency), falling back to the software start-time stamps otherwise. The
anchor actually used is saved next to sync.json as sync_detect.json for
audit. Without sync.json at all, event times are assumed to already be
video-relative (legacy recordings only).
"""

import json
from pathlib import Path
from typing import Callable, List, Optional

import cv2

from .finger_matching import FingerMatch, match_note_to_finger
from .hand_tracking import HandTracker
from .keyboard.midi_mapping import MidiMapping
from .keyboard.template import KeyboardTemplate
from .midi import load_midi_log
from .music_recording import SyncInfo
from .profiles import DATA_DIR
from .sync_led import DETECTION_FILENAME, event_epoch_time, resolve_sync_anchor


def analyze_recording(
    video_path: Path,
    midi_log_path: Path,
    keyboard_profile_name: str,
    data_dir: Path = DATA_DIR,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    sync_path: Optional[Path] = None,
    hands_out_path: Optional[Path] = None,
) -> List[Optional[FingerMatch]]:
    """Returns one FingerMatch (or None, if nothing could be resolved) per
    event in the MIDI log, in the same order.

    If given, progress_callback(frames_done, total_frames) is called after
    each frame is processed - this is the slow part (one MediaPipe pass per
    frame) - so a caller can drive a progress bar. total_frames is 0 if the
    video container doesn't report a frame count.

    If hands_out_path is given, the per-frame hand landmarks are also saved
    there as JSON (one entry per frame: {hand label: 21 [x, y] pixel
    points}), so the review video (app.review_video) can draw exactly the
    skeletons this analysis matched against without re-running MediaPipe."""
    template = KeyboardTemplate.load(data_dir / keyboard_profile_name / "keyboard_template.json")
    mapping = MidiMapping.load(data_dir / keyboard_profile_name / "midi_mapping.json")
    events = load_midi_log(midi_log_path)

    # Build the wall-clock -> frame mapping (see module docstring): a saved
    # (auto/manual) alignment wins, else the LED flash is detected now,
    # else the start-time stamps. The anchor used is saved for audit.
    sync = anchor = None
    if sync_path is not None and Path(sync_path).exists():
        sync = SyncInfo.load(sync_path)
        anchor = resolve_sync_anchor(video_path, sync, keyboard_profile_name, data_dir)
        anchor.save(Path(sync_path).parent / DETECTION_FILENAME)

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    tracker = HandTracker()
    hands_by_frame = []

    try:
        frames_done = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            hands_by_frame.append(tracker.process(frame))
            frames_done += 1
            if progress_callback is not None:
                progress_callback(frames_done, total_frames)
    finally:
        cap.release()
        tracker.close()

    if hands_out_path is not None:
        serializable = [
            {label: hand.landmarks for label, hand in hands.items()}
            for hands in hands_by_frame
        ]
        hands_out_path = Path(hands_out_path)
        hands_out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(hands_out_path, "w") as f:
            json.dump(serializable, f)

    results: List[Optional[FingerMatch]] = []
    for event in events:
        if anchor is not None:
            frame_idx = anchor.frame_for(event_epoch_time(event.time, sync))
        else:
            frame_idx = int(round(event.time * fps))  # legacy: no sync.json, video-relative times
        frame_idx = min(max(frame_idx, 0), len(hands_by_frame) - 1)
        hands = hands_by_frame[frame_idx] if hands_by_frame else {}
        results.append(match_note_to_finger(event.note, template, mapping, hands))

    return results
