"""MediaPipe hand tracking, reporting fingertip pixel positions labeled the
way this project names fingers: left hand thumb..pinky = L1..L5, right hand
thumb..pinky = R1..R5.
"""

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import vision

MODEL_PATH = Path(__file__).resolve().parent.parent / "hand_landmarker.task"

# MediaPipe's 21-point hand model: tip landmark id for thumb, index, middle,
# ring, pinky, in that order - matches our *1..*5 numbering.
FINGERTIP_IDS = [4, 8, 12, 16, 20]

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]

Point = Tuple[int, int]


class Hand:
    def __init__(self, label: str, landmarks: List[Point]):
        self.label = label  # "Left" or "Right"
        self.landmarks = landmarks  # 21 pixel points
        prefix = "L" if label == "Left" else "R"
        self.fingertips: Dict[str, Point] = {
            f"{prefix}{finger_idx + 1}": landmarks[tip_id] for finger_idx, tip_id in enumerate(FINGERTIP_IDS)
        }


class HandTracker:
    def __init__(self, model_path: Path = MODEL_PATH, smoothing_alpha: float = 0.45):
        base_options = mp.tasks.BaseOptions(model_asset_path=str(model_path))
        options = vision.HandLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.6,
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._start_time = time.time()
        self._smoothing_alpha = smoothing_alpha
        self._smooth_state: Dict[str, Dict[int, np.ndarray]] = {}

    def _smooth(self, label: str, raw_landmarks) -> List[np.ndarray]:
        state = self._smooth_state.setdefault(label, {})
        alpha = self._smoothing_alpha
        pts = []

        for i, lm in enumerate(raw_landmarks):
            p = np.array([lm.x, lm.y, lm.z], dtype=np.float32)
            state[i] = p if i not in state else alpha * p + (1 - alpha) * state[i]
            pts.append(state[i])

        return pts

    @staticmethod
    def _hand_label(result, i: int) -> str:
        if result.handedness and i < len(result.handedness) and result.handedness[i]:
            return result.handedness[i][0].category_name
        return f"Hand{i + 1}"

    def process(self, frame_bgr: np.ndarray) -> Dict[str, Hand]:
        """Runs detection on one frame. Naturally returns 0, 1, or 2 hands -
        there is no requirement that both be present."""
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int((time.time() - self._start_time) * 1000)
        result = self._landmarker.detect_for_video(mp_image, timestamp_ms)

        hands: Dict[str, Hand] = {}
        seen_labels = set()

        if result.hand_landmarks:
            for i, raw in enumerate(result.hand_landmarks):
                label = self._hand_label(result, i)
                seen_labels.add(label)

                smoothed = self._smooth(label, raw)
                pixels = [(int(p[0] * w), int(p[1] * h)) for p in smoothed]
                hands[label] = Hand(label, pixels)

        # Drop smoothing state for hands that left the frame, so a hand
        # coming back later doesn't jump-start from stale positions.
        for label in list(self._smooth_state.keys()):
            if label not in seen_labels:
                self._smooth_state.pop(label, None)

        return hands

    def close(self) -> None:
        self._landmarker.close()


def draw_hands(canvas: np.ndarray, hands: Dict[str, Hand], color=(0, 255, 0)) -> None:
    for hand in hands.values():
        for a, b in HAND_CONNECTIONS:
            cv2.line(canvas, hand.landmarks[a], hand.landmarks[b], color, 2)
        for x, y in hand.landmarks:
            cv2.circle(canvas, (x, y), 4, (255, 255, 255), -1)

        wx, wy = hand.landmarks[0]
        cv2.putText(canvas, hand.label, (wx - 20, wy - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        for finger_label, (fx, fy) in hand.fingertips.items():
            cv2.putText(canvas, finger_label, (fx + 6, fy - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
