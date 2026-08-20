"""Annotated review video for a finished quiz analysis.

Re-encodes a quiz's raw performance video with everything a human needs
to audit the automatic scoring drawn on top of each frame:

  - the hand skeletons the analysis actually matched against (from the
    hands.json that app.offline.analyze_recording saved - the exact same
    landmarks, not a fresh detection pass)
  - the target key tinted blue for as long as its cue is active
  - once the student pressed, the pressed key tinted green (right key) or
    red (wrong key), and the detected fingertip circled with its label
  - a text panel per note: target vs pressed key and whether it matched,
    target vs detected finger with the target finger's softmax probability
    (see app.finger_matching) and whether it cleared the threshold. When
    the finger judgment failed, the five most probable fingers and their
    softmax values are listed so the miss can be audited at a glance;
    when it passed but was ambiguous, the runner-up finger is shown.

The finger shown and the verdict beside it answer different questions -
one is the softmax argmax, the other the threshold rule on the *cued*
finger's mass - so they disagree in ways that read as contradictions
unless the panel says why. The two automatic cases are named rather than
left to the viewer: near-tie and sub-threshold (see _annotate()). A
manually corrected event is different: the ruling is a human's and the
probabilities are not, so the panel drops them entirely and just states
the corrected verdict - a confirmed match reads as a plain OK, a
confirmed miss as a plain WRONG naming the finger actually used, with no
mention that a correction took place. Existing review videos keep
whatever overlay they were rendered with; the wording only changes on
re-render.

Runs purely from files the analysis already saved (results.json stores
each keypress's finger probabilities and fingertip pixel position,
hands.json the per-frame skeletons) - no MediaPipe pass, so it's just a
decode+encode of the video. Like app.offline, keypress timestamps are
MIDI-relative and are shifted onto the video's clock via the session's
sync.json.
"""

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from .finger_matching import FINGER_PROBABILITY_THRESHOLD
from .hand_tracking import Hand, draw_hands
from .keyboard.midi_mapping import note_name
from .keyboard.template import KeyboardTemplate
from .music_recording import SyncInfo
from .profiles import DATA_DIR
from .quiz import QuizResult, finger_manually_corrected, scored_near_tie, subthreshold_match
from .sync_led import event_epoch_time, resolve_sync_anchor

# BGR
TARGET_TINT = (255, 160, 60)  # blue-ish: "this key was cued"
GOOD = (90, 200, 90)
BAD = (70, 70, 230)
NEUTRAL = (200, 200, 200)
PANEL_BG = (20, 20, 20)

# Keep the pressed-key/fingertip markers up this long after the keypress,
# even if the next cue already started, so a quick press stays reviewable.
PRESS_MARK_HOLD_S = 1.0


def _tint_key(frame: np.ndarray, key_map: np.ndarray, key_id: int, color, alpha: float = 0.45) -> None:
    mask = key_map == key_id + 1
    if not mask.any():
        return
    tint = np.empty_like(frame)
    tint[:] = color
    blended = cv2.addWeighted(frame, 1.0 - alpha, tint, alpha, 0)
    frame[mask] = blended[mask]


def _put_line(frame: np.ndarray, text: str, org: Tuple[int, int], color, scale: float = 0.6) -> None:
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def _runner_up(result: QuizResult) -> Optional[Tuple[str, float]]:
    probs = result.finger_probabilities or {}
    others = [(p, f) for f, p in probs.items() if f != result.actual_finger]
    if not others:
        return None
    p, finger = max(others)
    return (finger, p) if p >= 0.15 else None


def _top5_text(result: QuizResult) -> Optional[str]:
    probs = result.finger_probabilities or {}
    if not probs:
        return None
    top = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return "top-5: " + "  ".join(f"{finger} {p:.2f}" for finger, p in top)


def load_hands_by_frame(hands_path: Path) -> List[Dict[str, Hand]]:
    """Rebuild the per-frame Hand objects saved by
    app.offline.analyze_recording (see its hands_out_path parameter)."""
    with open(hands_path, encoding="utf-8") as f:
        data = json.load(f)
    return [
        {label: Hand(label, [tuple(p) for p in points]) for label, points in frame.items()}
        for frame in data
    ]


def _finger_mark_label(result: QuizResult) -> str:
    """Label for the circled fingertip.

    The circle sits on the fingertip the detector reported, so that finger
    stays first - renaming it to the cued one would put a wrong label on a
    tip the viewer can see. What gets appended is why the verdict beside it
    reads the way it does: "R2 ~ R3" for a near-tie the cued finger still
    won on the threshold rule, "R4 (p<th)" for a cued finger that was the
    most probable tip and still missed theta. ASCII only - cv2.putText
    cannot draw the approx or theta glyphs the detail table uses."""
    detected = result.actual_finger or "?"
    # On a manually corrected event the finger is a human's answer, not the
    # detector's, so the automatic-scoring hints below don't apply - just
    # the finger, matching the plain verdict the panel now shows.
    if finger_manually_corrected(result):
        return detected
    if scored_near_tie(result):
        return f"{detected} ~ {result.target_finger}"
    if subthreshold_match(result):
        return f"{detected} (p<th)"
    return detected


def _draw_press_marks(frame: np.ndarray, key_map: np.ndarray, result: QuizResult) -> None:
    """The pressed key's tint and the detected fingertip's circle."""
    if result.actual_key_id is not None:
        _tint_key(frame, key_map, result.actual_key_id, GOOD if result.note_correct else BAD, alpha=0.55)
    if result.actual_finger_point is not None:
        px, py = int(result.actual_finger_point[0]), int(result.actual_finger_point[1])
        color = GOOD if result.finger_correct else BAD if result.finger_correct is not None else NEUTRAL
        cv2.circle(frame, (px, py), 16, color, 3)
        _put_line(frame, _finger_mark_label(result), (px + 20, py - 12), color, scale=0.7)


def _annotate(frame: np.ndarray, key_map: np.ndarray, result: QuizResult, t_video: float,
              keypress_v: Optional[float], note_total: int) -> None:
    # --- key regions -------------------------------------------------
    pressed_visible = keypress_v is not None and t_video >= keypress_v
    if result.target_key_id is not None:
        _tint_key(frame, key_map, result.target_key_id, TARGET_TINT)
    if pressed_visible:
        _draw_press_marks(frame, key_map, result)

    # --- text panel ---------------------------------------------------
    lines: List[Tuple[str, tuple]] = []
    lines.append((f"Note {result.index + 1}/{note_total}   target {result.target_note_name}", (255, 255, 255)))

    if result.timed_out:
        lines.append(("Key: no keypress (TIMED OUT)", BAD))
    elif not pressed_visible:
        lines.append(("Key: waiting for keypress...", NEUTRAL))
    else:
        pressed_name = note_name(result.actual_note) if result.actual_note is not None else "?"
        mark = "OK" if result.note_correct else "WRONG"
        lines.append(
            (f"Key: target {result.target_note_name} | pressed {pressed_name}  [{mark}]",
             GOOD if result.note_correct else BAD)
        )

    if result.timed_out or not pressed_visible:
        lines.append((f"Finger: target {result.target_finger or '?'}", NEUTRAL))
    elif finger_manually_corrected(result):
        # A human overturned the automatic finger verdict, so the video
        # shows the corrected truth, not the fact that a correction happened:
        # a confirmed match reads as a plain OK, a confirmed miss as a plain
        # WRONG naming the finger actually used. The detector's stored
        # probabilities no longer decide anything here, so they are dropped
        # rather than printed beside a verdict they would contradict
        # ("p=0.34 vs 0.40 ... [OK]").
        if result.finger_correct:
            mark, color = "OK", GOOD
        else:
            mark, color = "WRONG", BAD
        lines.append(
            (f"Finger: target {result.target_finger or '?'} | "
             f"actual {result.actual_finger or '?'}  [{mark}]", color)
        )
    else:
        p = result.target_finger_probability
        p_text = f"p(target)={p:.2f} vs {FINGER_PROBABILITY_THRESHOLD:.2f}" if p is not None else "p(target)=n/a"
        if result.finger_correct is None:
            mark, color = "N/A", NEUTRAL
        elif result.finger_correct:
            mark, color = "OK", GOOD
        else:
            mark, color = "WRONG", BAD
        lines.append(
            (f"Finger: target {result.target_finger or '?'} | "
             f"detected {result.actual_finger or '?'}  {p_text}  [{mark}]", color)
        )
        p_detected = (result.finger_probabilities or {}).get(result.actual_finger)

        # Why the verdict reads the way it does, whenever it and the finger
        # beside it are answering different questions. At most one applies.
        if scored_near_tie(result) and p is not None and p_detected is not None:
            lines.append(
                (f"        near-tie: {result.actual_finger} p={p_detected:.2f} edged out cued "
                 f"{result.target_finger} p={p:.2f} - scored as {result.target_finger}", GOOD)
            )
        elif subthreshold_match(result) and p is not None:
            lines.append(
                (f"        cued {result.target_finger} was the most probable but "
                 f"p={p:.2f} < {FINGER_PROBABILITY_THRESHOLD:.2f} - not credited", BAD)
            )

        # Then the evidence itself.
        if result.finger_correct is False:
            # A failed judgment gets the full picture: the five most
            # probable fingers, so "target was close but lost" and "target
            # was nowhere near" are distinguishable without re-analyzing.
            top5 = _top5_text(result)
            if top5 is not None:
                lines.append((f"        {top5}", BAD))
        elif not scored_near_tie(result):
            # Skipped where a line above already named the runner-up: on a
            # near-tie it is the cued finger.
            ru = _runner_up(result)
            if ru is not None:
                lines.append((f"        runner-up {ru[0]} p={ru[1]:.2f}", NEUTRAL))

    panel_h = 18 + 30 * len(lines)
    panel_w = 640
    overlay = frame[0:panel_h, 0:panel_w].copy()
    overlay[:] = PANEL_BG
    frame[0:panel_h, 0:panel_w] = cv2.addWeighted(frame[0:panel_h, 0:panel_w], 0.35, overlay, 0.65, 0)
    y = 30
    for text, color in lines:
        _put_line(frame, text, (12, y), color)
        y += 30


def render_review_video(
    video_path: Path,
    out_path: Path,
    results: List[QuizResult],
    keyboard_profile_name: str,
    data_dir: Path = DATA_DIR,
    sync_path: Optional[Path] = None,
    hands_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Writes the annotated copy of video_path to out_path and returns it.

    results must already be analyzed (actual_finger etc. filled in - see
    app/gui/quiz_analysis_window.py). If hands_path exists, the saved
    per-frame skeletons are drawn too. progress_callback(frames_done,
    total_frames) mirrors app.offline.analyze_recording's convention."""
    template = KeyboardTemplate.load(data_dir / keyboard_profile_name / "keyboard_template.json")
    key_map = template.key_map

    hands_by_frame: List[Dict[str, Hand]] = []
    if hands_path is not None and Path(hands_path).exists():
        hands_by_frame = load_hands_by_frame(hands_path)

    # Wall-clock -> video-relative seconds, through the same LED-anchored
    # mapping app.offline uses, so the overlay marks the exact frames the
    # scoring read.
    sync = anchor = None
    if sync_path is not None and Path(sync_path).exists():
        sync = SyncInfo.load(sync_path)
        anchor = resolve_sync_anchor(video_path, sync, keyboard_profile_name, data_dir)

    def to_video_s(t: Optional[float]) -> Optional[float]:
        if t is None:
            return None
        if anchor is None:
            return t  # legacy: no sync.json, times already video-relative
        return event_epoch_time(t, sync) - anchor.frame0_epoch

    # One annotation segment per note: from its cue onset until the next
    # note's cue onset (the quiz presents notes strictly one at a time).
    ordered = sorted(results, key=lambda r: r.cue_onset_time)
    segments = [(to_video_s(r.cue_onset_time), r, to_video_s(r.keypress_time)) for r in ordered]

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

    try:
        frame_idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t_video = frame_idx / fps

            if frame_idx < len(hands_by_frame):
                draw_hands(frame, hands_by_frame[frame_idx])

            # The active note is the last one whose cue has started.
            active_i = None
            for i, (start_v, _, _) in enumerate(segments):
                if start_v <= t_video:
                    active_i = i
            if active_i is not None:
                start_v, result, keypress_v = segments[active_i]

                # While the active note is still waiting for its keypress,
                # keep the previous note's press marks on screen briefly -
                # the ~0.4s gap between notes alone is too short to see
                # what was just scored.
                if (keypress_v is None or t_video < keypress_v) and active_i > 0:
                    _, prev_result, prev_keypress_v = segments[active_i - 1]
                    if prev_keypress_v is not None and t_video - prev_keypress_v <= PRESS_MARK_HOLD_S:
                        _draw_press_marks(frame, key_map, prev_result)

                _annotate(frame, key_map, result, t_video, keypress_v, len(results))

            writer.write(frame)
            frame_idx += 1
            if progress_callback is not None:
                progress_callback(frame_idx, total_frames)
    finally:
        cap.release()
        writer.release()

    return out_path
