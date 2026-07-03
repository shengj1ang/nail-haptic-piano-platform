import cv2, time, threading
import mediapipe as mp
import numpy as np
import mido
from collections import deque
from mediapipe.tasks.python import vision

BaseOptions = mp.tasks.BaseOptions
HandLandmarker = vision.HandLandmarker
HandLandmarkerOptions = vision.HandLandmarkerOptions
VisionRunningMode = vision.RunningMode

MODEL = "hand_landmarker.task"
MIDI_PORTS = ["SE25 MIDI1", "SE25 MIDI2"]

HIST_LEN = 10
MIDI_GROUP_WINDOW = 0.085
CAPTURE_DELAY = 0.035
RESULT_HOLD = 0.8
MIN_SCORE = 0.00035
SMOOTH_ALPHA = 0.45

FINGERS = {
    "Thumb":  [1, 2, 3, 4],
    "Index":  [5, 6, 7, 8],
    "Middle": [9, 10, 11, 12],
    "Ring":   [13, 14, 15, 16],
    "Pinky":  [17, 18, 19, 20],
}

CONNECTIONS = [
    (0,1),(1,2),(2,3),(3,4),
    (0,5),(5,6),(6,7),(7,8),
    (5,9),(9,10),(10,11),(11,12),
    (9,13),(13,14),(14,15),(15,16),
    (13,17),(17,18),(18,19),(19,20),
    (0,17)
]

options = HandLandmarkerOptions(
    base_options=BaseOptions(model_asset_path=MODEL),
    running_mode=VisionRunningMode.VIDEO,
    num_hands=2,
    min_hand_detection_confidence=0.5,
    min_hand_presence_confidence=0.5,
    min_tracking_confidence=0.6,
)

landmarker = HandLandmarker.create_from_options(options)

cap = cv2.VideoCapture(0)
start_time = time.time()
running = True

history = {}
smooth_state = {}
visible_landmarks = {}
visible_points = {}

midi_lock = threading.Lock()
pending_group = None
midi_events = deque(maxlen=64)

active_result = {
    "items": [],
    "notes": [],
    "time": 0.0,
}


def hand_label(result, i):
    if result.handedness and i < len(result.handedness) and result.handedness[i]:
        return result.handedness[i][0].category_name
    return f"Hand{i + 1}"


def smooth_points(label, landmarks):
    if label not in smooth_state:
        smooth_state[label] = {}

    out = []
    for i, lm in enumerate(landmarks):
        p = np.array([lm.x, lm.y, lm.z], dtype=np.float32)

        if i not in smooth_state[label]:
            smooth_state[label][i] = p
        else:
            smooth_state[label][i] = SMOOTH_ALPHA * p + (1 - SMOOTH_ALPHA) * smooth_state[label][i]

        out.append(smooth_state[label][i].copy())

    return np.array(out)


def push_history(label, pts, t):
    if label not in history:
        history[label] = {}

    for fname, ids in FINGERS.items():
        if fname not in history[label]:
            history[label][fname] = deque(maxlen=HIST_LEN)

        arr = pts[ids]
        history[label][fname].append({
            "t": t,
            "pts": arr.copy(),
        })


def finger_score(label, fname):
    q = history.get(label, {}).get(fname)
    if not q or len(q) < 5:
        return None

    a = q[-5]
    b = q[-3]
    c = q[-1]

    dt1 = max(b["t"] - a["t"], 1e-6)
    dt2 = max(c["t"] - b["t"], 1e-6)

    v1 = (b["pts"] - a["pts"]) / dt1
    v2 = (c["pts"] - b["pts"]) / dt2
    acc = (v2 - v1) / max(c["t"] - a["t"], 1e-6)

    y_acc = acc[:, 1]
    y_vel = v2[:, 1]

    weights = np.array([0.45, 0.75, 1.0, 1.25], dtype=np.float32)

    down_acc = float(np.sum(np.maximum(y_acc, 0) * weights))
    down_vel = float(np.sum(np.maximum(y_vel, 0) * weights))

    side_motion = float(np.mean(np.abs(v2[:, 0])))
    z_motion = float(np.mean(np.abs(v2[:, 2])))

    curl = float(c["pts"][-1, 1] - c["pts"][1, 1])

    score = (
        down_acc * 0.018
        + down_vel * 0.9
        + max(curl, 0) * 0.12
        - side_motion * 0.18
        - z_motion * 0.08
    )

    return {
        "hand": label,
        "finger": fname,
        "score": score,
        "down_acc": down_acc,
        "down_vel": down_vel,
    }


def collect_candidates():
    items = []

    for label in visible_landmarks:
        for fname in FINGERS:
            item = finger_score(label, fname)
            if item and item["score"] > MIN_SCORE:
                items.append(item)

    items.sort(key=lambda x: x["score"], reverse=True)
    return items


def select_fingers(candidates, n):
    selected = []
    used = set()

    for item in candidates:
        key = (item["hand"], item["finger"])
        if key in used:
            continue

        selected.append(item)
        used.add(key)

        if len(selected) >= n:
            break

    return selected


def register_note(port, note, velocity):
    global pending_group

    now = time.time()

    with midi_lock:
        midi_events.append({
            "time": now,
            "note": int(note),
            "velocity": int(velocity),
            "port": port,
        })

        if pending_group is None or now - pending_group["start_time"] > MIDI_GROUP_WINDOW:
            pending_group = {
                "start_time": now,
                "last_time": now,
                "notes": [int(note)],
                "captured": False,
            }
        else:
            pending_group["last_time"] = now
            pending_group["notes"].append(int(note))


def midi_thread(port):
    global running

    try:
        with mido.open_input(port) as p:
            while running:
                for msg in p.iter_pending():
                    if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
                        register_note(port, msg.note, msg.velocity)
                time.sleep(0.001)
    except Exception as e:
        print(f"MIDI port failed: {port} | {e}")


def start_midi():
    try:
        available = mido.get_input_names()
    except Exception as e:
        print("MIDI error:", e)
        return []

    selected = [p for p in MIDI_PORTS if p in available] or available

    for p in selected:
        threading.Thread(target=midi_thread, args=(p,), daemon=True).start()

    return selected


opened_ports = start_midi()

while True:
    ok, frame = cap.read()
    if not ok:
        break

    frame = cv2.flip(frame, 1)
    h, w = frame.shape[:2]
    now = time.time()

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    ts = int((now - start_time) * 1000)

    result = landmarker.detect_for_video(mp_img, ts)

    cam = frame.copy()
    skel = np.zeros_like(frame)

    visible_landmarks.clear()
    visible_points.clear()

    if result.hand_landmarks:
        for i, raw in enumerate(result.hand_landmarks):
            label = hand_label(result, i)
            pts = smooth_points(label, raw)

            visible_landmarks[label] = pts
            push_history(label, pts, now)

            pix = [(int(p[0] * w), int(p[1] * h)) for p in pts]
            visible_points[label] = pix

            for a, b in CONNECTIONS:
                cv2.line(skel, pix[a], pix[b], (0, 255, 0), 2)

            for x, y in pix:
                cv2.circle(skel, (x, y), 4, (255, 255, 255), -1)

            wx, wy = pix[0]
            cv2.putText(cam, label, (wx - 20, wy - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(skel, label, (wx - 20, wy - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

    for label in list(history.keys()):
        if label not in visible_landmarks:
            history[label].clear()
            smooth_state.pop(label, None)

    with midi_lock:
        group = pending_group.copy() if pending_group else None

    if group and not group["captured"]:
        age = now - group["start_time"]
        quiet = now - group["last_time"]

        if age >= CAPTURE_DELAY and quiet >= MIDI_GROUP_WINDOW:
            candidates = collect_candidates()
            selected = select_fingers(candidates, len(group["notes"]))

            active_result["items"] = selected
            active_result["notes"] = group["notes"][:]
            active_result["time"] = now

            with midi_lock:
                if pending_group and pending_group["start_time"] == group["start_time"]:
                    pending_group["captured"] = True

    showing = now - active_result["time"] <= RESULT_HOLD

    status = []

    if showing and active_result["items"]:
        for idx, item in enumerate(active_result["items"]):
            label = item["hand"]
            fname = item["finger"]

            if label not in visible_points:
                continue

            tip_id = FINGERS[fname][-1]
            joint_id = FINGERS[fname][-2]
            tip = visible_points[label][tip_id]
            joint = visible_points[label][joint_id]

            note = active_result["notes"][idx] if idx < len(active_result["notes"]) else ""
            text = f"{label} {fname} N{note}"

            for canvas in (cam, skel):
                cv2.circle(canvas, tip, 12, (0, 0, 255), 2)
                cv2.circle(canvas, joint, 9, (0, 0, 255), 2)
                cv2.putText(canvas, text, (tip[0] + 10, tip[1] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2)

        status.append("Finger: " + ", ".join(
            f"{x['hand']} {x['finger']}" for x in active_result["items"]
        ))
    else:
        status.append("Finger: None")

    with midi_lock:
        recent = [e for e in midi_events if now - e["time"] <= 1.0]

    if recent:
        status.append("MIDI notes: " + ", ".join(str(e["note"]) for e in recent[-6:]))
    else:
        status.append("MIDI: waiting")

    if group and not group["captured"]:
        status.append(f"Pending notes: {len(group['notes'])}")

    status.append("Ports: " + (", ".join(opened_ports) if opened_ports else "none"))

    out = np.hstack([cam, skel])

    cv2.putText(out, "Camera", (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.putText(out, "Skeleton", (w + 20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    for i, line in enumerate(status):
        cv2.putText(out, line, (20, 85 + i * 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.82, (0, 255, 255), 2)

    cv2.imshow("Hand Tracking", out)

    key = cv2.waitKey(1) & 0xFF
    if key in (27, ord("q")):
        break

running = False
cap.release()
landmarker.close()
cv2.destroyAllWindows()