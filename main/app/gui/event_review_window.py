"""Per-event video review and finger correction - opened by
double-clicking an event row in the Quiz Detail window.

Plays the recording around one keypress (±5 s), mapped to frames through
the trial's sync anchor, with a frame slider and 0.5/0.75/1/1.25×
playback. Two overlays, both on by default and independently switchable
(they hide whatever pixels the reviewer needs to see for themselves):

  - "Highlight target key": the cued key's exact pixel region, from the
    trial's keyboard profile - the same key map the scoring used - tinted
    blue on every frame.
  - "Show fingertips & pressed key": the ten fingertip positions the
    analysis actually read (from the hands.json it saved, not a fresh
    detection), and from the keypress frame on, the key that was really
    pressed tinted green/red for right/wrong key, with the pressing
    fingertip ringed in the same green/red for right/wrong finger.

Below the player, the event's target finger, detected finger, and
probability evidence are shown - Actual Finger written exactly as the
detail table writes it, "R3 (≈R2)" and all (app/gui/finger_cell.py) -
and the Actual Finger can be corrected from the physical finger list.
Both verdict buttons close the window afterwards unless "Close after
saving" is unticked.

A correction changes actual_finger, re-judges finger_correct as an exact
match against the target finger, and sets finger_corrected as the audit
trail. The stored softmax probabilities and target_finger_probability
are deliberately left untouched, so the event can still be re-judged
under a different threshold (and corrections made before
finger_corrected existed remain recognisable - see
app.quiz.finger_manually_corrected).
"""

import html

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..keyboard.template import KeyboardTemplate
from ..music_recording import SyncInfo
from ..profiles import DATA_DIR
from ..quiz import (
    RAW_HANDS_FILENAME,
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    load_quiz_results,
    quiz_dir,
    quiz_raw_dir,
    save_quiz_results,
    save_quiz_summary,
)
from ..review_video import BAD, GOOD, NEUTRAL, TARGET_TINT, load_hands_by_frame
from ..sync_led import resolve_sync_anchor
from .finger_cell import finger_cell
from .quiz_style import STYLE_SHEET

WINDOW_SECONDS = 5.0  # played on each side of the keypress
# How long the last frame is held when the recording stops inside that
# window (the trial's last event) - purely so the player doesn't end on
# the keypress.
TAIL_PAD_SECONDS = 2.0
DISPLAY_WIDTH = 720
SPEEDS = [0.5, 0.75, 1.0, 1.25]
# Key tints: the same colours as the review video (blue = cued, green =
# right, red = wrong), kept light enough that a fingertip on the key stays
# visible through them.
MASK_ALPHA = 0.40
# Fingertip markers, in displayed pixels: every visible tip gets a small
# neutral dot, the finger credited with the keypress a larger ring.
TIP_RADIUS = 5
PRESS_TIP_RADIUS = 11
# Physical keyboard order, plus explicitly-no-finger.
FINGER_CHOICES = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]
UNRESOLVED_LABEL = "Unresolved (no finger visible)"


def _label(frame, text: str, org, color, scale: float = 0.42) -> None:
    """Fingertip label, drawn twice so it stays readable over skin, keys
    and tints alike."""
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


class EventReviewWindow(QMainWindow):
    saved = Signal(str)  # quiz name, emitted after the correction is written

    def __init__(self, quiz_name: str, event_index: int, keyboard_profile_name: str):
        super().__init__()
        self.quiz_name = quiz_name
        self.event_index = event_index

        results = load_quiz_results(quiz_dir(quiz_name) / RESULTS_FILENAME)
        self.result = results[event_index]
        r = self.result
        self.setWindowTitle(f"Event Review - {quiz_name} - event {r.index} ({r.target_note_name})")

        raw = quiz_raw_dir(quiz_name)
        sync_path = raw / RAW_SYNC_FILENAME
        if not sync_path.exists():
            raise RuntimeError("no sync.json - can't map the keypress time onto video frames")
        sync = SyncInfo.load(sync_path)
        anchor = resolve_sync_anchor(raw / RAW_VIDEO_FILENAME, sync, keyboard_profile_name)

        center_t = r.keypress_time if r.keypress_time is not None else r.cue_onset_time
        self.fps, self.first_frame, self.frames, self._scale, self._real_frames = self._load_clip(
            raw / RAW_VIDEO_FILENAME, anchor.frame_for(center_t)
        )
        if not self.frames:
            raise RuntimeError("could not read any video frames around this event")
        self.keypress_frame = anchor.frame_for(center_t) - self.first_frame
        self._key_map = self._load_key_map(keyboard_profile_name)
        self._target_mask = self._key_mask(r.target_key_id)
        self._pressed_mask = self._key_mask(r.actual_key_id)
        self._hands_by_frame = self._load_hands(raw / RAW_HANDS_FILENAME)

        # --- player --------------------------------------------------
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(self.frames) - 1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._show_frame)

        self.play_btn = QPushButton("▶ Play")
        self.play_btn.setObjectName("playBtn")
        self.play_btn.clicked.connect(self._toggle_play)
        self.speed_combo = QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f"{s:g}×", s)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.currentIndexChanged.connect(self._apply_speed)
        jump_btn = QPushButton("Jump to keypress")
        jump_btn.clicked.connect(lambda: self.slider.setValue(self.keypress_frame))
        self.mask_check = QCheckBox(f"Highlight target key ({r.target_note_name})")
        self.mask_check.setChecked(True)
        self.mask_check.setToolTip(
            "Tint the cued key's pixel region, taken from the trial's keyboard profile - the same "
            "key map the finger matching used. Uncheck it when the tint covers the fingertip you "
            "are trying to read."
        )
        if self._target_mask is None:
            self.mask_check.setChecked(False)
            self.mask_check.setEnabled(False)
            self.mask_check.setToolTip(
                "No key region to show: this event has no target key, or the keyboard profile's "
                "template couldn't be loaded."
            )
        self.mask_check.toggled.connect(lambda: self._show_frame(self.slider.value()))

        self.fingers_check = QCheckBox("Show fingertips && pressed key")  # && = a literal & in a Qt label
        self.fingers_check.setChecked(True)
        self.fingers_check.setToolTip(
            "Draw the fingertip positions the analysis read from this video, and from the keypress "
            "frame on, the key actually pressed (green = right key, red = wrong key) with the "
            "pressing fingertip ringed green (right finger) or red (wrong finger)."
        )
        if not self._hands_by_frame and self._pressed_mask is None:
            self.fingers_check.setChecked(False)
            self.fingers_check.setEnabled(False)
            self.fingers_check.setToolTip(
                "Nothing to draw: this trial has no hands.json (re-run the analysis to save one) "
                "and no pressed key for this event."
            )
        self.fingers_check.toggled.connect(lambda: self._show_frame(self.slider.value()))

        self.labels_check = QCheckBox("Label pressing finger")
        self.labels_check.setChecked(True)
        self.labels_check.setToolTip(
            "Write the finger name next to the fingertip credited with the keypress. The other "
            "tips are always plain dots - ten labels at once just cover the hands."
        )
        self.labels_check.setEnabled(self.fingers_check.isChecked())
        self.labels_check.toggled.connect(lambda: self._show_frame(self.slider.value()))
        self.fingers_check.toggled.connect(self.labels_check.setEnabled)

        self.close_check = QCheckBox("Close after saving")
        self.close_check.setChecked(True)
        self.close_check.setToolTip(
            "Close this window as soon as Save correction or Confirm as is has written the "
            "verdict. Uncheck it to stay here after saving."
        )
        self.time_label = QLabel()
        self.time_label.setObjectName("note")
        # Its text changes length as you scrub (keypress marker, held-tail
        # note); an Ignored width policy keeps that from widening the whole
        # window - and with it the video area - mid-playback.
        self.time_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        player_row = QHBoxLayout()
        player_row.addWidget(self.play_btn)
        player_row.addWidget(QLabel("Speed:"))
        player_row.addWidget(self.speed_combo)
        player_row.addWidget(jump_btn)
        player_row.addWidget(self.time_label, 1)

        # Own row, away from the transport controls - it's an overlay
        # toggle, not playback.
        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self.mask_check)
        overlay_row.addSpacing(24)
        overlay_row.addWidget(self.fingers_check)
        overlay_row.addSpacing(24)
        overlay_row.addWidget(self.labels_check)
        overlay_row.addSpacing(24)
        overlay_row.addWidget(self.close_check)
        overlay_row.addStretch(1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        # --- correction ----------------------------------------------
        timed_out = r.timed_out
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        self.evidence.setObjectName("evidence")
        self._refresh_evidence()

        self.finger_combo = QComboBox()
        for f in FINGER_CHOICES:
            self.finger_combo.addItem(f, f)
        self.finger_combo.addItem(UNRESOLVED_LABEL, None)
        current_idx = self.finger_combo.findData(r.actual_finger)
        self.finger_combo.setCurrentIndex(current_idx if current_idx >= 0 else self.finger_combo.count() - 1)

        # Amber changes the stored label, green leaves it alone: the two
        # buttons are one keystroke apart in a queue you work through fast,
        # so the colour carries which one writes a new finger.
        save_btn = QPushButton("Save correction")
        save_btn.setObjectName("saveBtn")
        save_btn.clicked.connect(self._save_correction)
        confirm_btn = QPushButton("Confirm as is")
        confirm_btn.setObjectName("confirmBtn")
        confirm_btn.setToolTip(
            "The automatic verdict was right - change nothing, just record that this event has "
            "been watched so it leaves the review queue."
        )
        confirm_btn.clicked.connect(self._confirm_as_is)
        self.save_note = QLabel(
            "Correction changes Actual Finger only - the stored probabilities stay untouched. "
            "Either button records that a human has ruled on this event, which takes it off the "
            "review queue."
        )
        self.save_note.setObjectName("note")
        self.save_note.setWordWrap(True)

        correction_row = QHBoxLayout()
        correction_row.setSpacing(10)
        correction_row.addWidget(QLabel("Actual finger was:"))
        correction_row.addWidget(self.finger_combo, 1)
        correction_row.addWidget(save_btn)
        correction_row.addWidget(confirm_btn)

        box = QGroupBox("Correct Actual Finger")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(14, 16, 14, 12)
        box_layout.setSpacing(10)
        box_layout.addWidget(self.evidence)
        box_layout.addLayout(correction_row)
        box_layout.addWidget(self.save_note)
        if timed_out:
            box.setEnabled(False)
            box.setTitle("Correct Actual Finger (disabled - this event timed out, there is no keypress)")

        central = QWidget()
        central.setStyleSheet(STYLE_SHEET)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.image_label, 1)
        layout.addWidget(self.slider)
        layout.addLayout(player_row)
        layout.addLayout(overlay_row)
        layout.addWidget(box)
        self.setCentralWidget(central)

        self.slider.setValue(self.keypress_frame)
        self._show_frame(self.keypress_frame)

    # ------------------------------------------------------------------

    def _refresh_evidence(self) -> None:
        """The evidence line, rebuilt from self.result so it also reflects
        a correction just saved from this window. Actual Finger is spelled
        the way the Quiz Detail table spells it - "R3 (≈R2)" when the cued
        finger was scored despite a neighbour winning the argmax, "R4
        (p<θ)" for the mirror case - so one event never reads two ways
        across the two windows (app/gui/finger_cell.py)."""
        r = self.result
        p = r.target_finger_probability
        detected = None
        if r.finger_probabilities:
            detected = max(r.finger_probabilities, key=r.finger_probabilities.get)
        text, tip, _tint = finger_cell(r)
        self.evidence.setText(
            f"Target finger: <b>{r.target_finger or '—'}</b>  |  "
            f"detected (softmax argmax): {detected or 'unresolved'}  |  "
            # escaped: "(p<θ)" in a rich-text label would be read as a tag
            f"current Actual Finger: <b>{html.escape(text)}</b>  |  "
            f"p(target) = {f'{p:.2f}' if p is not None else 'n/a'}"
        )
        self.evidence.setToolTip(tip or "")

    def _load_clip(self, video_path, center_frame: int):
        """±WINDOW_SECONDS of frames around center_frame, stored as JPEG
        bytes (a 10 s clip decoded raw would be hundreds of MB).

        When the recording ends before the window does - the trial's last
        event, where the camera stops right after the keypress - the last
        frame is repeated for TAIL_PAD_SECONDS, so playback doesn't run
        out the moment the press happens and there is still room to scrub
        back or hit Jump to keypress. It's a display-only freeze frame,
        same size as every other frame; nothing about the recording or the
        scoring changes. Returns (fps, first frame index, frames, display
        scale, how many of the frames are distinct video)."""
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        half = int(WINDOW_SECONDS * fps)
        first = max(center_frame - half, 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        frames = []
        scale = 1.0  # displayed pixels per source pixel - overlays are stored in source pixels
        for _ in range(2 * half + 1):
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            scale = DISPLAY_WIDTH / w
            frame = cv2.resize(frame, (DISPLAY_WIDTH, int(h * scale)))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                frames.append(buf.tobytes())
        cap.release()

        real_count = len(frames)
        after_press = real_count - 1 - (center_frame - first)
        if frames and after_press < half:
            frames.extend([frames[-1]] * int(TAIL_PAD_SECONDS * fps))
        return fps, first, frames, scale, real_count

    def _load_key_map(self, keyboard_profile_name: str):
        """The profile's key map, nearest-resized onto the displayed
        (DISPLAY_WIDTH-wide) frame. It is the very map app.finger_matching
        scored this event against, so what gets tinted is exactly what the
        analysis treated as each key. None when the profile has no usable
        template; the key overlays are then simply unavailable."""
        try:
            template = KeyboardTemplate.load(DATA_DIR / keyboard_profile_name / "keyboard_template.json")
        except Exception:
            return None
        probe = cv2.imdecode(np.frombuffer(self.frames[0], np.uint8), cv2.IMREAD_COLOR)
        h, w = probe.shape[:2]
        # Nearest-neighbour: key ids are labels, not intensities - and it
        # also absorbs a profile calibrated at a different frame size.
        return cv2.resize(template.key_map, (w, h), interpolation=cv2.INTER_NEAREST)

    def _key_mask(self, key_id):
        """Boolean mask of one key over the displayed frame, or None when
        there is no such key (no target/no keypress, or it isn't in the
        template)."""
        if key_id is None or self._key_map is None:
            return None
        mask = self._key_map == key_id + 1
        return mask if mask.any() else None

    def _load_hands(self, hands_path):
        """The per-frame hand landmarks app.offline.analyze_recording saved
        for this trial - the exact skeletons the scoring read, indexed by
        absolute video frame. Empty when the trial predates hands.json or
        it can't be read; the fingertip overlay is then unavailable."""
        try:
            return load_hands_by_frame(hands_path)
        except Exception:
            return []

    def _tint(self, frame, mask, color) -> None:
        tint = np.empty_like(frame)
        tint[:] = color
        blended = cv2.addWeighted(frame, 1.0 - MASK_ALPHA, tint, MASK_ALPHA, 0)
        frame[mask] = blended[mask]

    def _draw_fingertips(self, frame, idx: int) -> None:
        """Every fingertip the analysis saw in this frame, plus - from the
        keypress frame on - the pressed key and the fingertip credited with
        the press, colour-coded right (green) / wrong (red). Before the
        keypress there is no verdict yet, so every tip stays neutral. Only
        the pressing tip is ever named, and only while "Label pressing
        finger" is on: ten labels at once bury the hands."""
        r = self.result
        pressed_shown = not r.timed_out and idx >= self.keypress_frame
        if pressed_shown and self._pressed_mask is not None:
            self._tint(frame, self._pressed_mask, GOOD if r.note_correct else BAD)

        frame_idx = self.first_frame + idx
        if not (0 <= frame_idx < len(self._hands_by_frame)):
            return
        press_color = GOOD if r.finger_correct else BAD if r.finger_correct is not None else NEUTRAL
        for hand in self._hands_by_frame[frame_idx].values():
            for finger, (fx, fy) in hand.fingertips.items():
                x, y = int(fx * self._scale), int(fy * self._scale)
                if pressed_shown and finger == r.actual_finger:
                    cv2.circle(frame, (x, y), PRESS_TIP_RADIUS, press_color, 2)
                    if self.labels_check.isChecked():
                        _label(frame, finger, (x + PRESS_TIP_RADIUS + 4, y - 6), press_color)
                else:
                    cv2.circle(frame, (x, y), TIP_RADIUS, NEUTRAL, -1)

    def _show_frame(self, idx: int) -> None:
        frame = cv2.imdecode(np.frombuffer(self.frames[idx], np.uint8), cv2.IMREAD_COLOR)
        # Held tail: annotate it exactly like the last real frame it repeats.
        held = idx >= self._real_frames
        overlay_idx = min(idx, self._real_frames - 1)
        if self.mask_check.isChecked() and self._target_mask is not None:
            self._tint(frame, self._target_mask, TARGET_TINT)
        if self.fingers_check.isChecked():
            self._draw_fingertips(frame, overlay_idx)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = frame.shape
        self.image_label.setPixmap(QPixmap.fromImage(QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888)))
        rel = (idx - self.keypress_frame) / self.fps
        marker = "  ◉ KEYPRESS FRAME" if idx == self.keypress_frame else ""
        if held:
            marker = "  (held - recording ended)"
        self.time_label.setText(f"t = {rel:+.2f} s relative to keypress{marker}")

    # ------------------------------------------------------------------

    def _toggle_play(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.play_btn.setText("▶ Play")
        else:
            if self.slider.value() >= len(self.frames) - 1:
                self.slider.setValue(0)
            self._apply_speed()
            self._timer.start()
            self.play_btn.setText("⏸ Pause")

    def _apply_speed(self) -> None:
        speed = self.speed_combo.currentData()
        self._timer.setInterval(max(int(1000 / (self.fps * speed)), 1))

    def _tick(self) -> None:
        nxt = self.slider.value() + 1
        if nxt >= len(self.frames):
            self._timer.stop()
            self.play_btn.setText("▶ Play")
            return
        self.slider.setValue(nxt)

    # ------------------------------------------------------------------

    def _write_verdict(self, corrected: bool):
        """Persist this event's human verdict. Re-reads from disk first so
        a correction saved from another event's window in the meantime
        isn't clobbered."""
        results = load_quiz_results(quiz_dir(self.quiz_name) / RESULTS_FILENAME)
        r = results[self.event_index]
        if corrected:
            new_finger = self.finger_combo.currentData()
            r.actual_finger = new_finger
            # Manual ground truth replaces the probability-threshold rule
            # for this event: correct means exactly the target finger.
            r.finger_correct = (new_finger == r.target_finger) if r.target_finger is not None else None
            r.finger_corrected = True  # explicit audit trail, never inferred
        r.finger_reviewed = True  # watched by a human either way
        save_quiz_results(results, quiz_dir(self.quiz_name) / RESULTS_FILENAME)
        # meta.json caches the headline numbers this event feeds into, so
        # it is re-derived here rather than left to whoever opened this
        # window - the two files are never out of step on disk.
        save_quiz_summary(self.quiz_name)
        self.result = r
        return r

    def _after_verdict(self) -> None:
        """Either button saves; whether it also closes is the reviewer's
        choice. Closing is the default because the queue is worked through
        one double-click at a time - untick it to keep watching the same
        keypress after saving, and the evidence line then shows the new
        verdict in place."""
        self._refresh_evidence()
        if self.close_check.isChecked():
            self.close()

    def _save_correction(self) -> None:
        r = self._write_verdict(corrected=True)
        self.save_note.setText(
            f"✔ Saved: Actual Finger = {r.actual_finger or 'unresolved'}, "
            f"finger_correct = {r.finger_correct}. The summary metrics behind it have "
            "already been recomputed."
        )
        self.saved.emit(self.quiz_name)
        self._after_verdict()

    def _confirm_as_is(self) -> None:
        r = self._write_verdict(corrected=False)
        self.save_note.setText(
            f"✔ Reviewed, nothing changed: Actual Finger stays {r.actual_finger or 'unresolved'}, "
            f"finger_correct = {r.finger_correct}. The event is off the review queue."
        )
        self.saved.emit(self.quiz_name)
        self._after_verdict()

    def closeEvent(self, event) -> None:
        """The window can outlive its close (the detail window holds a
        reference until it next prunes) - stop the playback timer so a
        closed player isn't still scrubbing."""
        self._timer.stop()
        super().closeEvent(event)
