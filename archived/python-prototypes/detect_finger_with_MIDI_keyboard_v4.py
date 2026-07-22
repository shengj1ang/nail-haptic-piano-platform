import cv2, time, threading, pickle, os
import mediapipe as mp
import numpy as np
import mido
from collections import deque
from mediapipe.tasks.python import vision

MODEL_PATH = "hand_landmarker.task"
SAVE_PATH = "finger_motion_templates.pkl"

MIDI_PORTS = ["SE25 MIDI1", "SE25 MIDI2"]

USE_MIRROR = False
SAMPLES_PER_FINGER = 15
PRE_TIME = 0.16
POST_TIME = 0.04
RESULT_HOLD = 0.8
SMOOTH_ALPHA = 0.45

ORDER = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]

FINGERS = {
    "L1": ("Left",  [1, 2, 3, 4]),
    "L2": ("Left",  [5, 6, 7, 8]),
    "L3": ("Left",  [9, 10, 11, 12]),
    "L4": ("Left",  [13, 14, 15, 16]),
    "L5": ("Left",  [17, 18, 19, 20]),
    "R1": ("Right", [1, 2, 3, 4]),
    "R2": ("Right", [5, 6, 7, 8]),
    "R3": ("Right", [9, 10, 11, 12]),
    "R4": ("Right", [13, 14, 15, 16]),
    "R5": ("Right", [17, 18, 19, 20]),
}

CONNECTIONS = [
    (0,1),(1,2),(2,3),(3,4),
    (0,5),(5,6),(6,7),(7,8),
    (5,9),(9,10),(10,11),(11,12),
    (9,13),(13,14),(14,15),(15,16),
    (13,17),(17,18),(18,19),(19,20),
    (0,17)
]

BaseOptions = mp.tasks.BaseOptions
HandLandmarker = vision.HandLandmarker
HandLandmarkerOptions = vision.HandLandmarkerOptions
VisionRunningMode = vision.RunningMode

options = HandLandmarkerOptions(
    base_options=BaseOptions(model_asset_path=MODEL_PATH),
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

smooth_state = {}
frame_buffer = deque(maxlen=120)
latest_pixels = {}

midi_lock = threading.Lock()
pending_notes = deque(maxlen=32)
recent_midi = deque(maxlen=32)

templates = {}
samples = []

mode = "train"
train_i = 0
train_n = 0

active = {
    "label": None,
    "time": 0,
    "score": 0,
}


def get_hand_label(result, i):
    if result.handedness and i < len(result.handedness) and result.handedness[i]:
        return result.handedness[i][0].category_name
    return f"Hand{i + 1}"


def smooth(label, raw):
    if label not in smooth_state:
        smooth_state[label] = {}

    pts = []

    for i, lm in enumerate(raw):
        p = np.array([lm.x, lm.y, lm.z], dtype=np.float32)

        if i not in smooth_state[label]:
            smooth_state[label][i] = p
        else:
            smooth_state[label][i] = SMOOTH_ALPHA * p + (1 - SMOOTH_ALPHA) * smooth_state[label][i]

        pts.append(smooth_state[label][i].copy())

    return np.array(pts, dtype=np.float32)


def normalize_hand(pts):
    wrist = pts[0]
    scale = np.linalg.norm(pts[5] - pts[17]) + 1e-6
    return (pts - wrist) / scale


def make_snapshot(t, hands):
    norm = {}

    for hand, pts in hands.items():
        norm[hand] = normalize_hand(pts)

    frame_buffer.append({
        "t": t,
        "hands": norm,
    })


def get_window(t0):
    a = t0 - PRE_TIME
    b = t0 + POST_TIME
    return [f for f in frame_buffer if a <= f["t"] <= b]


def resample_curve(values, n=12):
    values = np.asarray(values, dtype=np.float32)

    if len(values) < 3:
        return None

    old = np.linspace(0, 1, len(values))
    new = np.linspace(0, 1, n)

    out = []

    for d in range(values.shape[1]):
        out.append(np.interp(new, old, values[:, d]))

    return np.stack(out, axis=1)


def extract_finger_feature(label, t0):
    hand, ids = FINGERS[label]
    win = get_window(t0)

    seq = []

    for f in win:
        if hand not in f["hands"]:
            continue

        pts = f["hands"][hand]
        finger = pts[ids]

        base = finger[0]
        rel = finger - base

        one = np.concatenate([
            rel[:, 1],
            rel[:, 0],
            rel[:, 2],
        ])

        seq.append(one)

    if len(seq) < 4:
        return None

    curve = resample_curve(seq, 12)

    if curve is None:
        return None

    vel = np.diff(curve, axis=0)
    acc = np.diff(vel, axis=0)

    feat = np.concatenate([
        curve.flatten(),
        vel.flatten(),
        acc.flatten(),
    ])

    feat = feat - np.mean(feat)
    feat = feat / (np.std(feat) + 1e-6)

    return feat.astype(np.float32)


def build_templates(samples):
    data = {}

    for x, y in samples:
        data.setdefault(y, []).append(x)

    result = {}

    for y, xs in data.items():
        xs = np.array(xs, dtype=np.float32)
        result[y] = {
            "mean": xs.mean(axis=0),
            "std": xs.std(axis=0) + 1e-6,
        }

    return result


def predict(t0):
    scores = []

    for label in ORDER:
        x = extract_finger_feature(label, t0)
        if x is None or label not in templates:
            continue

        temp = templates[label]["mean"]
        dist = float(np.linalg.norm(x - temp))

        scores.append((label, dist))

    if not scores:
        return None, 999

    scores.sort(key=lambda x: x[1])
    return scores[0]


def register_note(port, note, velocity):
    now = time.time()

    with midi_lock:
        item = {
            "time": now,
            "note": int(note),
            "velocity": int(velocity),
            "port": port,
            "captured": False,
        }

        pending_notes.append(item)
        recent_midi.append(item)


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
        print("MIDI failed:", port, e)


def start_midi():
    try:
        names = mido.get_input_names()
    except Exception as e:
        print("MIDI error:", e)
        return []

    selected = [p for p in MIDI_PORTS if p in names] or names

    for p in selected:
        threading.Thread(target=midi_thread, args=(p,), daemon=True).start()

    return selected


def draw_finger(canvas, label, text, color=(0, 0, 255)):
    if label not in FINGERS:
        return

    hand, ids = FINGERS[label]

    if hand not in latest_pixels:
        return

    pts = latest_pixels[hand]
    fs = [pts[i] for i in ids]

    for p in fs:
        cv2.circle(canvas, p, 10, color, 2)

    for a, b in zip(fs[:-1], fs[1:]):
        cv2.line(canvas, a, b, color, 3)

    tip = fs[-1]
    cv2.putText(canvas, text, (tip[0] + 10, tip[1] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)


opened_ports = start_midi()

if os.path.exists(SAVE_PATH):
    try:
        with open(SAVE_PATH, "rb") as f:
            templates = pickle.load(f)
        mode = "predict"
        print("Loaded templates.")
    except Exception:
        pass


while True:
    ok, frame = cap.read()
    if not ok:
        break

    if USE_MIRROR:
        frame = cv2.flip(frame, 1)

    h, w = frame.shape[:2]
    now = time.time()

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    ts = int((now - start_time) * 1000)

    result = landmarker.detect_for_video(mp_img, ts)

    cam = frame.copy()
    skel = np.zeros_like(frame)

    hands = {}
    latest_pixels.clear()

    if result.hand_landmarks:
        for i, raw in enumerate(result.hand_landmarks):
            label = get_hand_label(result, i)
            pts = smooth(label, raw)
            hands[label] = pts

            pix = [(int(p[0] * w), int(p[1] * h)) for p in pts]
            latest_pixels[label] = pix

            for a, b in CONNECTIONS:
                cv2.line(skel, pix[a], pix[b], (0, 255, 0), 2)

            for x, y in pix:
                cv2.circle(skel, (x, y), 4, (255, 255, 255), -1)

            wx, wy = pix[0]
            cv2.putText(cam, label, (wx - 20, wy - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(skel, label, (wx - 20, wy - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

    make_snapshot(now, hands)

    with midi_lock:
        notes = list(pending_notes)

    for note in notes:
        if note["captured"]:
            continue

        if now - note["time"] < POST_TIME:
            continue

        t0 = note["time"]

        if mode == "train":
            target = ORDER[train_i]
            x = extract_finger_feature(target, t0)

            if x is not None:
                samples.append((x, target))
                train_n += 1

                active = {
                    "label": target,
                    "time": now,
                    "score": 0,
                }

                print(f"Train {target}: {train_n}/{SAMPLES_PER_FINGER}")

                if train_n >= SAMPLES_PER_FINGER:
                    train_i += 1
                    train_n = 0

                    if train_i >= len(ORDER):
                        templates = build_templates(samples)

                        with open(SAVE_PATH, "wb") as f:
                            pickle.dump(templates, f)

                        mode = "predict"
                        print("Training complete.")

        else:
            label, score = predict(t0)

            active = {
                "label": label,
                "time": now,
                "score": score,
            }

            print("Predict:", label, round(score, 2))

        with midi_lock:
            for n in pending_notes:
                if n["time"] == note["time"]:
                    n["captured"] = True

    if active["label"] and now - active["time"] <= RESULT_HOLD:
        text = active["label"]
        if mode == "predict":
            text += f" {active['score']:.1f}"

        draw_finger(cam, active["label"], text)
        draw_finger(skel, active["label"], text)

    lines = []

    if mode == "train":
        lines.append("TRAINING")
        lines.append(f"Press: {ORDER[train_i]}")
        lines.append(f"Sample: {train_n}/{SAMPLES_PER_FINGER}")
        lines.append("You can press different keys")
    else:
        lines.append("PREDICT")
        lines.append(f"Finger: {active['label'] or 'None'}")

    with midi_lock:
        recent = [x for x in recent_midi if now - x["time"] <= 1.0]

    if recent:
        lines.append("MIDI: " + ", ".join(str(x["note"]) for x in recent[-6:]))
    else:
        lines.append("MIDI: waiting")

    lines.append("Ports: " + (", ".join(opened_ports) if opened_ports else "none"))

    out = np.hstack([cam, skel])

    cv2.putText(out, "Camera", (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.putText(out, "Skeleton", (w + 20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    for i, line in enumerate(lines):
        cv2.putText(out, line, (20, 85 + i * 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 255, 255), 2)

    cv2.imshow("Finger Motion Template", out)

    key = cv2.waitKey(1) & 0xFF

    if key in [27, ord("q")]:
        break

    if key == ord("r"):
        if os.path.exists(SAVE_PATH):
            os.remove(SAVE_PATH)

        templates = {}
        samples = []
        mode = "train"
        train_i = 0
        train_n = 0
        active = {"label": None, "time": 0, "score": 0}

        print("Reset training.")

running = False
cap.release()
landmarker.close()
cv2.destroyAllWindows()