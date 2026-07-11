"""Offline finger-accuracy analysis.

Given a recorded video and a MIDI event log (see app.midi -
MidiEvent / save_midi_log / load_midi_log), this recomputes the same
"which finger pressed this key" result that test_finger_accuracy.py produces live, without
a camera or MIDI device connected. This is the entry point for the
"record now, analyze later" workflow: a session's MIDI notes and video get
captured during the experiment, and finger accuracy is computed afterward.

The MIDI log's timestamps are relative to when the MIDI recorder started,
which is always somewhat *before* the video's first frame (the recorder is
constructed first; opening the camera/VideoWriter takes time). sync_path
points at the session's sync.json (see app.music_recording.SyncInfo),
which records both start moments so each event can be shifted onto the
video's clock before being mapped to a frame. Without it, events are
assumed to already be video-relative - and every match is read from a
frame *later* than the actual keypress, by however long the gap between
the two starts was.
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

    # MIDI-relative -> video-relative (see module docstring). midi_start
    # precedes video_start, so the shift is negative: the event happened
    # this many seconds *earlier* on the video's clock.
    time_offset_s = 0.0
    if sync_path is not None and Path(sync_path).exists():
        sync = SyncInfo.load(sync_path)
        time_offset_s = sync.midi_start_time - sync.video_start_time

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
        frame_idx = min(max(int(round((event.time + time_offset_s) * fps)), 0), len(hands_by_frame) - 1)
        hands = hands_by_frame[frame_idx] if hands_by_frame else {}
        results.append(match_note_to_finger(event.note, template, mapping, hands))

    return results
