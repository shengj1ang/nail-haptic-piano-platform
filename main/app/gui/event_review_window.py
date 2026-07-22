"""Per-event video review and finger correction - opened by
double-clicking an event row in the Quiz Detail window.

Plays the recording around one keypress (±5 s), mapped to frames through
the trial's sync anchor, with a frame slider and 0.5/0.75/1/1.25×
playback. Below the player, the event's target finger, detected finger,
and probability evidence are shown, and the Actual Finger can be
corrected from the physical finger list.

A correction changes ONLY actual_finger (and re-judges finger_correct as
an exact match against the target finger). The stored softmax
probabilities and target_finger_probability are deliberately left
untouched: the automatic pipeline always sets actual_finger to the
softmax argmax, so actual_finger disagreeing with the argmax is the
audit trail that marks the event as manually corrected (see
app.quiz.finger_manually_corrected).
"""

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..music_recording import SyncInfo
from ..quiz import (
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    load_quiz_results,
    quiz_dir,
    quiz_raw_dir,
    save_quiz_results,
)
from ..sync_led import resolve_sync_anchor

WINDOW_SECONDS = 5.0  # played on each side of the keypress
DISPLAY_WIDTH = 720
SPEEDS = [0.5, 0.75, 1.0, 1.25]
# Physical keyboard order, plus explicitly-no-finger.
FINGER_CHOICES = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]
UNRESOLVED_LABEL = "Unresolved (no finger visible)"


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
        self.fps, self.first_frame, self.frames = self._load_clip(
            raw / RAW_VIDEO_FILENAME, anchor.frame_for(center_t)
        )
        if not self.frames:
            raise RuntimeError("could not read any video frames around this event")
        self.keypress_frame = anchor.frame_for(center_t) - self.first_frame

        # --- player --------------------------------------------------
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(self.frames) - 1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._show_frame)

        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._toggle_play)
        self.speed_combo = QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f"{s:g}×", s)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.currentIndexChanged.connect(self._apply_speed)
        jump_btn = QPushButton("Jump to keypress")
        jump_btn.clicked.connect(lambda: self.slider.setValue(self.keypress_frame))
        self.time_label = QLabel()

        player_row = QHBoxLayout()
        player_row.addWidget(self.play_btn)
        player_row.addWidget(QLabel("Speed:"))
        player_row.addWidget(self.speed_combo)
        player_row.addWidget(jump_btn)
        player_row.addWidget(self.time_label, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        # --- correction ----------------------------------------------
        timed_out = r.timed_out
        p = r.target_finger_probability
        detected = None
        if r.finger_probabilities:
            detected = max(r.finger_probabilities, key=r.finger_probabilities.get)
        evidence = QLabel(
            f"Target finger: <b>{r.target_finger or '—'}</b>  |  "
            f"detected (softmax argmax): {detected or 'unresolved'}  |  "
            f"current Actual Finger: <b>{r.actual_finger or 'unresolved'}</b>  |  "
            f"p(target) = {f'{p:.2f}' if p is not None else 'n/a'}"
        )
        evidence.setWordWrap(True)

        self.finger_combo = QComboBox()
        for f in FINGER_CHOICES:
            self.finger_combo.addItem(f, f)
        self.finger_combo.addItem(UNRESOLVED_LABEL, None)
        current_idx = self.finger_combo.findData(r.actual_finger)
        self.finger_combo.setCurrentIndex(current_idx if current_idx >= 0 else self.finger_combo.count() - 1)

        save_btn = QPushButton("Save correction")
        save_btn.clicked.connect(self._save_correction)
        self.save_note = QLabel(
            "Correction changes Actual Finger only - the stored probabilities stay untouched, "
            "which is what marks the event as manually corrected."
        )
        self.save_note.setWordWrap(True)

        correction_row = QHBoxLayout()
        correction_row.addWidget(QLabel("Actual finger was:"))
        correction_row.addWidget(self.finger_combo, 1)
        correction_row.addWidget(save_btn)

        box = QGroupBox("Correct Actual Finger")
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(evidence)
        box_layout.addLayout(correction_row)
        box_layout.addWidget(self.save_note)
        if timed_out:
            box.setEnabled(False)
            box.setTitle("Correct Actual Finger (disabled - this event timed out, there is no keypress)")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.image_label, 1)
        layout.addWidget(self.slider)
        layout.addLayout(player_row)
        layout.addWidget(box)
        self.setCentralWidget(central)

        self.slider.setValue(self.keypress_frame)
        self._show_frame(self.keypress_frame)

    # ------------------------------------------------------------------

    def _load_clip(self, video_path, center_frame: int):
        """±WINDOW_SECONDS of frames around center_frame, stored as JPEG
        bytes (a 10 s clip decoded raw would be hundreds of MB)."""
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        half = int(WINDOW_SECONDS * fps)
        first = max(center_frame - half, 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        frames = []
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
        return fps, first, frames

    def _show_frame(self, idx: int) -> None:
        frame = cv2.imdecode(np.frombuffer(self.frames[idx], np.uint8), cv2.IMREAD_COLOR)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = frame.shape
        self.image_label.setPixmap(QPixmap.fromImage(QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888)))
        rel = (idx - self.keypress_frame) / self.fps
        marker = "  ◉ KEYPRESS FRAME" if idx == self.keypress_frame else ""
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

    def _save_correction(self) -> None:
        new_finger = self.finger_combo.currentData()
        # Re-read from disk so a correction saved from another event's
        # window in the meantime isn't clobbered.
        results = load_quiz_results(quiz_dir(self.quiz_name) / RESULTS_FILENAME)
        r = results[self.event_index]
        r.actual_finger = new_finger
        # Manual ground truth replaces the probability-threshold rule for
        # this event: correct means exactly the target finger.
        r.finger_correct = (new_finger == r.target_finger) if r.target_finger is not None else None
        save_quiz_results(results, quiz_dir(self.quiz_name) / RESULTS_FILENAME)
        self.result = r
        self.save_note.setText(
            f"✔ Saved: Actual Finger = {new_finger or 'unresolved'}, finger_correct = {r.finger_correct}. "
            "Run Analyze selected (data only) in Quiz Analysis to refresh the stored summary metrics."
        )
        self.saved.emit(self.quiz_name)
