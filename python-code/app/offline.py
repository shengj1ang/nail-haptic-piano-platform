"""Offline finger-accuracy analysis.

Given a recorded video and a MIDI event log (see app.midi -
MidiEvent / save_midi_log / load_midi_log), this recomputes the same
"which finger pressed this key" result that test_finger_accuracy.py produces live, without
a camera or MIDI device connected. This is the entry point for the
"record now, analyze later" workflow: a session's MIDI notes and video get
captured during the experiment, and finger accuracy is computed afterward.

video_path and midi_log_path must share the same time origin - i.e. video
recording and the MidiListener were started at the same moment - since
each MIDI event is matched to hand positions by that shared timestamp.
"""

from pathlib import Path
from typing import Callable, List, Optional

import cv2

from .finger_matching import FingerMatch, match_note_to_finger
from .hand_tracking import HandTracker
from .keyboard.midi_mapping import MidiMapping
from .keyboard.template import KeyboardTemplate
from .midi import load_midi_log
from .profiles import DATA_DIR


def analyze_recording(
    video_path: Path,
    midi_log_path: Path,
    keyboard_profile_name: str,
    data_dir: Path = DATA_DIR,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> List[Optional[FingerMatch]]:
    """Returns one FingerMatch (or None, if nothing could be resolved) per
    event in the MIDI log, in the same order.

    If given, progress_callback(frames_done, total_frames) is called after
    each frame is processed - this is the slow part (one MediaPipe pass per
    frame) - so a caller can drive a progress bar. total_frames is 0 if the
    video container doesn't report a frame count."""
    template = KeyboardTemplate.load(data_dir / keyboard_profile_name / "keyboard_template.json")
    mapping = MidiMapping.load(data_dir / keyboard_profile_name / "midi_mapping.json")
    events = load_midi_log(midi_log_path)

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

    results: List[Optional[FingerMatch]] = []
    for event in events:
        frame_idx = min(max(int(round(event.time * fps)), 0), len(hands_by_frame) - 1)
        hands = hands_by_frame[frame_idx] if hands_by_frame else {}
        results.append(match_note_to_finger(event.note, template, mapping, hands))

    return results
