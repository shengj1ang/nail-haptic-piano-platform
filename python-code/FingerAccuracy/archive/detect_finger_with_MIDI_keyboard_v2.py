import cv2
import mediapipe as mp
import numpy as np
import time
import threading
from collections import deque
import mido
from mediapipe.tasks.python import vision

BaseOptions = mp.tasks.BaseOptions
HandLandmarker = vision.HandLandmarker
HandLandmarkerOptions = vision.HandLandmarkerOptions
VisionRunningMode = vision.RunningMode

options = HandLandmarkerOptions(
    base_options=BaseOptions(model_asset_path="hand_landmarker.task"),
    running_mode=VisionRunningMode.VIDEO,
    num_hands=2,
    min_hand_detection_confidence=0.5,
    min_hand_presence_confidence=0.5,
    min_tracking_confidence=0.6,
)

landmarker = vision.HandLandmarker.create_from_options(options)

connections = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17)
]

finger_pairs = {
    "Thumb": (4, 3),
    "Index": (8, 7),
    "Middle": (12, 11),
    "Ring": (16, 15),
    "Pinky": (20, 19),
}

MIDI_PORTS = ['SE25 MIDI1', 'SE25 MIDI2']
MIDI_GROUP_WINDOW = 0.085
CAPTURE_DELAY = 0.035
RESULT_HOLD_TIME = 0.8
HISTORY_LEN = 8
MIN_SCORE = 0.007
SMOOTH_ALPHA = 0.34
FREEZE_THRESHOLD = 0.0028
FREEZE_FRAMES = 4
UNFREEZE_BOOST_TIME = 0.18

cap = cv2.VideoCapture(0)
start_time = time.time()
running = True

hand_history = {"Left": {}, "Right": {}}
point_filter_state = {}
hand_points_map = {}
hand_landmarks_map = {}
last_seen_hands = set()

midi_lock = threading.Lock()
midi_events = deque(maxlen=64)
pending_group = None
last_midi_trigger_time = 0.0
active_result = {
    "items": [],
    "notes": [],
    "time": 0.0,
}


def get_hand_label(result, hand_index):
    if result.handedness and hand_index < len(result.handedness) and result.handedness[hand_index]:
        return result.handedness[hand_index][0].category_name
    return f"Hand {hand_index + 1}"


def ensure_history(hand_label):
    if hand_label not in hand_history:
        hand_history[hand_label] = {}
    for finger_name in finger_pairs:
        if finger_name not in hand_history[hand_label]:
            hand_history[hand_label][finger_name] = {
                "tip_y": deque(maxlen=HISTORY_LEN),
                "joint_y": deque(maxlen=HISTORY_LEN),
                "tip_x": deque(maxlen=HISTORY_LEN),
                "joint_x": deque(maxlen=HISTORY_LEN),
            }


def clear_hand_history(hand_label):
    if hand_label in hand_history:
        for finger_name in hand_history[hand_label]:
            for key in hand_history[hand_label][finger_name]:
                hand_history[hand_label][finger_name][key].clear()


def clear_filter_state(hand_label):
    if hand_label in point_filter_state:
        del point_filter_state[hand_label]


def update_history(hand_label, landmarks):
    ensure_history(hand_label)
    for finger_name, (tip_i, joint_i) in finger_pairs.items():
        tip = landmarks[tip_i]
        joint = landmarks[joint_i]
        hand_history[hand_label][finger_name]["tip_y"].append(tip.y)
        hand_history[hand_label][finger_name]["joint_y"].append(joint.y)
        hand_history[hand_label][finger_name]["tip_x"].append(tip.x)
        hand_history[hand_label][finger_name]["joint_x"].append(joint.x)


def smooth_landmarks(hand_label, landmarks, boost_motion=False):
    if hand_label not in point_filter_state:
        point_filter_state[hand_label] = {}

    smoothed = []
    alpha = 0.62 if boost_motion else SMOOTH_ALPHA
    freeze_threshold = FREEZE_THRESHOLD * (1.8 if boost_motion else 1.0)

    for idx, lm in enumerate(landmarks):
        x = float(lm.x)
        y = float(lm.y)
        z = float(lm.z)

        if idx not in point_filter_state[hand_label]:
            point_filter_state[hand_label][idx] = {
                "x": x,
                "y": y,
                "z": z,
                "freeze_count": 0,
            }
        else:
            state = point_filter_state[hand_label][idx]
            dx = x - state["x"]
            dy = y - state["y"]
            dz = z - state["z"]
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5

            if dist < freeze_threshold:
                state["freeze_count"] += 1
            else:
                state["freeze_count"] = 0

            if state["freeze_count"] >= FREEZE_FRAMES:
                x = state["x"]
                y = state["y"]
                z = state["z"]
            else:
                x = alpha * x + (1.0 - alpha) * state["x"]
                y = alpha * y + (1.0 - alpha) * state["y"]
                z = alpha * z + (1.0 - alpha) * state["z"]
                state["x"] = x
                state["y"] = y
                state["z"] = z

        state = point_filter_state[hand_label][idx]
        smoothed.append(type("Point", (), {"x": state["x"], "y": state["y"], "z": state["z"]})())

    return smoothed


def finger_motion_score(hand_label, finger_name):
    hist = hand_history[hand_label][finger_name]
    if len(hist["tip_y"]) < 5:
        return None

    tip_y = list(hist["tip_y"])
    joint_y = list(hist["joint_y"])
    tip_x = list(hist["tip_x"])
    joint_x = list(hist["joint_x"])

    tip_now_y = tip_y[-1]
    tip_prev_y = np.mean(tip_y[-5:-1])
    joint_now_y = joint_y[-1]
    joint_prev_y = np.mean(joint_y[-5:-1])
    tip_now_x = tip_x[-1]
    tip_prev_x = np.mean(tip_x[-5:-1])
    joint_now_x = joint_x[-1]
    joint_prev_x = np.mean(joint_x[-5:-1])

    tip_move_y = tip_now_y - tip_prev_y
    joint_move_y = joint_now_y - joint_prev_y
    tip_move_x = abs(tip_now_x - tip_prev_x)
    joint_move_x = abs(joint_now_x - joint_prev_x)
    relative_gap = tip_now_y - joint_now_y

    score = (
        tip_move_y * 2.4 +
        joint_move_y * 1.15 +
        max(0.0, relative_gap) * 0.25 -
        tip_move_x * 0.35 -
        joint_move_x * 0.2
    )

    tip_i, joint_i = finger_pairs[finger_name]
    tip_lm = hand_landmarks_map[hand_label][tip_i]
    joint_lm = hand_landmarks_map[hand_label][joint_i]

    return {
        "hand": hand_label,
        "finger": finger_name,
        "score": float(score),
        "tip_move_y": float(tip_move_y),
        "joint_move_y": float(joint_move_y),
        "relative_gap": float(relative_gap),
        "tip_z": float(tip_lm.z),
        "joint_z": float(joint_lm.z),
    }


def collect_candidates():
    candidates = []
    for hand_label in last_seen_hands:
        for finger_name in finger_pairs:
            item = finger_motion_score(hand_label, finger_name)
            if item is not None and item["score"] > MIN_SCORE:
                candidates.append(item)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def select_fingers_for_notes(candidates, note_count):
    if note_count <= 0 or not candidates:
        return []

    selected = []
    used = set()

    for item in candidates:
        key = (item["hand"], item["finger"])
        if key in used:
            continue
        selected.append(item)
        used.add(key)
        if len(selected) >= note_count:
            break

    return selected


def register_note_on(port_name, note, velocity):
    global pending_group, last_midi_trigger_time
    now = time.time()
    with midi_lock:
        midi_events.append({
            "time": now,
            "note": int(note),
            "velocity": int(velocity),
            "port": port_name,
        })
        last_midi_trigger_time = now
        if pending_group is None or (now - pending_group["start_time"]) > MIDI_GROUP_WINDOW:
            pending_group = {
                "start_time": now,
                "last_time": now,
                "notes": [int(note)],
                "velocities": [int(velocity)],
                "ports": [port_name],
                "captured": False,
            }
        else:
            pending_group["last_time"] = now
            pending_group["notes"].append(int(note))
            pending_group["velocities"].append(int(velocity))
            pending_group["ports"].append(port_name)


def midi_listener(port_name):
    global running
    try:
        with mido.open_input(port_name) as inport:
            while running:
                for msg in inport.iter_pending():
                    if msg.type == 'note_on' and getattr(msg, 'velocity', 0) > 0:
                        register_note_on(port_name, msg.note, msg.velocity)
                time.sleep(0.001)
    except Exception:
        pass


def start_midi_threads():
    available = set(mido.get_input_names())
    selected = [p for p in MIDI_PORTS if p in available]
    if not selected:
        selected = list(available)
    threads = []
    for port_name in selected:
        t = threading.Thread(target=midi_listener, args=(port_name,), daemon=True)
        t.start()
        threads.append(t)
    return selected, threads


opened_ports, midi_threads = start_midi_threads()

while True:
    ret, frame = cap.read()
    if not ret:
        break

    frame = cv2.flip(frame, 1)
    h, w, _ = frame.shape
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    timestamp_ms = int((time.time() - start_time) * 1000)
    result = landmarker.detect_for_video(mp_image, timestamp_ms)

    camera_view = frame.copy()
    skeleton = np.zeros((h, w, 3), dtype=np.uint8)
    status_lines = []
    now = time.time()

    visible_hands = set()
    hand_points_map = {}
    hand_landmarks_map = {}

    boost_motion = (now - last_midi_trigger_time) <= UNFREEZE_BOOST_TIME

    if result.hand_landmarks:
        for hand_index, raw_landmarks in enumerate(result.hand_landmarks):
            hand_label = get_hand_label(result, hand_index)
            visible_hands.add(hand_label)

            smoothed_landmarks = smooth_landmarks(hand_label, raw_landmarks, boost_motion=boost_motion)
            hand_landmarks_map[hand_label] = smoothed_landmarks
            update_history(hand_label, smoothed_landmarks)

            points = []
            for lm in smoothed_landmarks:
                x = int(lm.x * w)
                y = int(lm.y * h)
                points.append((x, y))
            hand_points_map[hand_label] = points

            for a, b in connections:
                cv2.line(skeleton, points[a], points[b], (0, 255, 0), 2)
            for x, y in points:
                cv2.circle(skeleton, (x, y), 4, (255, 255, 255), -1)

            wrist_point = points[0]
            cv2.putText(camera_view, hand_label, (wrist_point[0] - 20, wrist_point[1] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(skeleton, hand_label, (wrist_point[0] - 20, wrist_point[1] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

    for hand_label in list(hand_history.keys()):
        if hand_label not in visible_hands:
            clear_hand_history(hand_label)
            clear_filter_state(hand_label)

    last_seen_hands = visible_hands.copy()

    with midi_lock:
        current_group = pending_group.copy() if pending_group is not None else None

    if current_group is not None and not current_group["captured"]:
        group_age = now - current_group["start_time"]
        quiet_time = now - current_group["last_time"]
        should_capture = group_age >= CAPTURE_DELAY and quiet_time >= MIDI_GROUP_WINDOW

        if should_capture:
            note_count = len(current_group["notes"])
            candidates = collect_candidates()
            selected = select_fingers_for_notes(candidates, note_count)
            active_result["items"] = selected
            active_result["notes"] = current_group["notes"][:]
            active_result["time"] = now
            with midi_lock:
                if pending_group is not None and pending_group["start_time"] == current_group["start_time"]:
                    pending_group["captured"] = True

    showing_result = (now - active_result["time"]) <= RESULT_HOLD_TIME

    if showing_result and active_result["items"]:
        for idx, item in enumerate(active_result["items"]):
            hand_label = item["hand"]
            finger_name = item["finger"]
            if hand_label not in hand_points_map:
                continue
            points = hand_points_map[hand_label]
            tip_i, joint_i = finger_pairs[finger_name]
            tip_point = points[tip_i]
            joint_point = points[joint_i]
            note_text = ""
            if idx < len(active_result["notes"]):
                note_text = f" N{active_result['notes'][idx]}"
            label_text = f"{hand_label} {finger_name}{note_text}"
            cv2.circle(camera_view, tip_point, 11, (0, 0, 255), 2)
            cv2.circle(camera_view, joint_point, 9, (0, 0, 255), 2)
            cv2.circle(skeleton, tip_point, 11, (0, 0, 255), 2)
            cv2.circle(skeleton, joint_point, 9, (0, 0, 255), 2)
            cv2.putText(camera_view, label_text, (tip_point[0] + 10, tip_point[1] - 10 + idx * 2), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (0, 0, 255), 2)
            cv2.putText(skeleton, label_text, (tip_point[0] + 10, tip_point[1] - 10 + idx * 2), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (0, 0, 255), 2)

    if showing_result and active_result["items"]:
        if len(active_result["items"]) == 1:
            item = active_result["items"][0]
            status_lines.append(f"Finger: {item['hand']} {item['finger']}")
        else:
            status_lines.append(f"Fingers: {len(active_result['items'])}")
            for item in active_result["items"]:
                status_lines.append(f"- {item['hand']} {item['finger']}")
    else:
        status_lines.append("Finger: None")

    with midi_lock:
        recent_events = [e for e in midi_events if now - e["time"] <= 1.0]

    if recent_events:
        notes_text = ", ".join(str(e["note"]) for e in recent_events[-6:])
        status_lines.append(f"MIDI notes: {notes_text}")
    else:
        status_lines.append("MIDI: waiting")

    if current_group is not None and not current_group["captured"]:
        status_lines.append(f"Pending notes: {len(current_group['notes'])}")

    if boost_motion:
        status_lines.append("Stabilizer: active-release")
    else:
        status_lines.append("Stabilizer: frozen-when-still")

    if opened_ports:
        status_lines.append("Ports: " + ", ".join(opened_ports))
    else:
        status_lines.append("Ports: none")

    combined = np.hstack((camera_view, skeleton))
    cv2.putText(combined, 'Camera', (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.putText(combined, 'Skeleton', (w + 20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    panel_y = 80
    for i, line in enumerate(status_lines):
        cv2.putText(combined, line, (20, panel_y + i * 34), cv2.FONT_HERSHEY_SIMPLEX, 0.83, (0, 255, 255), 2)

    cv2.imshow('Hand Tracking', combined)

    key = cv2.waitKey(1) & 0xFF
    if key == 27 or key == ord('q'):
        break

running = False
cap.release()
landmarker.close()
cv2.destroyAllWindows()