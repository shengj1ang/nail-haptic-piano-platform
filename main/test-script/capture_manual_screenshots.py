"""Render every launcher window offscreen and save the PNGs for doc/USER_MANUAL.md.

A documentation tool, not a test: it exists so the manual's screenshots can be
regenerated after a UI change instead of being re-photographed by hand.

WHY IT RUNS AGAINST A COPY
--------------------------
Photographing these windows means constructing them, and several of them do
real work on construction: the quiz and detector windows open the camera on a
33 ms timer, the haptic windows auto-detect and open a serial port, and a
failing haptic path can write back to config.json. So the capture never runs in
`main/` itself. `--build-sandbox` makes a copy of the code plus a slimmed copy
of `data/` somewhere disposable, points its `config.json` camera at a recorded
clip, and every window is built there. On top of that, `_install_stubs()`
replaces the camera, the serial ports, the MIDI enumeration and the audio
output with fakes, so even inside the sandbox nothing can claim a device.

The stand-in camera is a recorded study trial (see DEMO_CLIP) rather than a
synthetic frame, so the previews look like the tool in use. It must be a
recording made through the camera position the ACTIVE PROFILE was calibrated
against: every window that draws the profile over the image - Region Preview,
Live Finger Detection, the MIDI Mapping Wizard - photographs visibly
misaligned otherwise, which is exactly what a reader would read as a broken
calibration.

USAGE
-----
    # 1. build a disposable copy of the project (a few hundred MB)
    python test-script/capture_manual_screenshots.py --build-sandbox /tmp/manual-sandbox

    # 2. capture every window into doc/image/
    python test-script/capture_manual_screenshots.py --sandbox /tmp/manual-sandbox --all

    # ... or just the ones that changed
    python test-script/capture_manual_screenshots.py --sandbox /tmp/manual-sandbox \
        --only launcher quiz_visual group_analysis

    python test-script/capture_manual_screenshots.py --list     # every key

`--all` runs one subprocess per window with a timeout, so a window that hangs
costs that window and not the run. `--window KEY` is that worker, and is what
you reach for when debugging one capture; it must be run with the sandbox as
the working directory, which the driver does for you.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent
REPO = MAIN.parent
DEFAULT_OUT = REPO / "doc" / "image"
# The stand-in camera must be a recording made through the SAME camera
# position the active profile was calibrated against, or every window that
# draws the profile over the image photographs visibly misaligned. A
# participant recording satisfies that by construction: it was recorded in the
# session the profile belongs to.
DEMO_CLIP = "data/quiz/P18-T25-C\u03b3/raw/performance.mp4"
# Seek in before grabbing - the opening seconds of a trial are an empty
# keyboard, and a manual wants hands in frame.
DEMO_START_FRAME = 700

# Accelerometer Live View plots whatever the rig streams, so with a fake
# serial port it photographs as an empty pair of axes. Replay a real saved
# run instead: this slice straddles a baseline -> vibration transition at the
# project's own LRA default (224 Hz, amp 64), so the picture shows the quiet
# floor, the onset, and the driven signal - which is what the window is for.
DEMO_ACC = ("data/validation_experiments/motor_acc_delay_experiment/"
            "delay_trials_1785425851.raw_acc.npz")
DEMO_ACC_SLICE = (883, 1283)   # 400 samples = the window's rolling buffer

# The offscreen platform reports a ~750x800 screen. Several windows size
# themselves to the screen they are on - the launcher most of all, which picks
# its column count from it - so a desktop-sized one is reported instead.
VIRTUAL_SCREEN = (2400, 1400)


# --------------------------------------------------------------- sandbox
def build_sandbox(dest: Path) -> None:
    """Copy the project to `dest`: all of the code, and the parts of data/
    that windows read but no video.

    Videos are what makes data/ tens of gigabytes, and no window in the manual
    needs one - the analysis windows read results.json/meta.json and the CSV
    exports. `hands.json` is skipped for the same reason at ~700 KB x every
    quiz."""
    dest.mkdir(parents=True, exist_ok=True)
    code = [
        "rsync", "-a",
        # Anchored: a bare "data/" would also exclude server/data/, which is
        # the relay store Remote Latency Analysis reads.
        "--exclude=/data/", "--exclude=/runtime/", "--exclude=__pycache__/",
        "--exclude=*.pyc", "--exclude=.git/", "--exclude=.pytest_cache/",
        "--exclude=.ruff_cache/",
        f"{MAIN}/", f"{dest}/",
    ]
    print("copying code ...")
    subprocess.run(code, check=True)

    print("copying data (no video) ...")
    (dest / "data").mkdir(exist_ok=True)
    subprocess.run([
        "rsync", "-a",
        "--exclude=*.mp4", "--exclude=*.avi", "--exclude=*.mov",
        "--exclude=quiz-zip/", "--exclude=hands.json",
        "--exclude=prediction_events.csv", "--exclude=.DS_Store",
        f"{MAIN}/data/", f"{dest}/data/",
    ], check=True)

    clip = MAIN / DEMO_CLIP
    if clip.exists():
        shutil.copy2(clip, dest / "demo_camera.mp4")
        print(f"stand-in camera: {dest / 'demo_camera.mp4'}")
    else:
        print(f"WARNING: {clip} is missing; camera previews will be blank")

    _sanitise_config(dest)
    print(f"\nsandbox ready: {dest}")


def _sanitise_config(dest: Path) -> None:
    """Blank the tele-training credentials in the sandbox's config.

    The real config carries a relay URL, a room id and a join code, and the
    tele-training windows put them on screen - so they would end up in a
    committed screenshot. The camera index is left at 0: `_install_stubs`
    resolves an integer index to the demo clip, so the windows that print the
    configured index show what a real setup shows."""
    import json

    path = dest / "config.json"
    if not path.exists():
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))
    net = cfg.get("remote_guidance", {}).get("network")
    if net:
        net.update({
            "server_url": "https://relay.example.org",
            "username": "demostudent",
            "room_id": "00000000-0000-0000-0000-000000000000",
            "join_code": "ABC123",
        })
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ----------------------------------------------------------------- stubs
def _demo_acc_lines(sandbox: Path):
    """The saved accelerometer run, as the lines the firmware would stream.

    Returns [] if the store is missing, which just leaves the live view empty
    rather than failing the capture."""
    try:
        import numpy as np

        store = np.load(sandbox / DEMO_ACC)
        lo, hi = DEMO_ACC_SLICE
        return [f"ACC,0,{x},{y},{z}\n".encode()
                for x, y, z in zip(store["x"][lo:hi],
                                   store["y"][lo:hi],
                                   store["z"][lo:hi])]
    except Exception as e:
        print(f"  (no demo ACC stream: {e})")
        return []


def _install_stubs(sandbox: Path) -> None:
    """Replace every device the windows might open with a fake.

    Belt and braces on top of the sandbox: a window that auto-connects on
    construction still must not reach the rig, the keyboard or the camera of
    whoever is regenerating the manual."""
    import serial
    from serial.tools import list_ports

    acc_lines = _demo_acc_lines(sandbox)

    class FakeSerial:
        def __init__(self, port=None, baudrate=9600, timeout=None, **kw):
            self.port, self.baudrate, self.timeout = port, baudrate, timeout
            self.is_open = True
            self.in_waiting = 0
            self._reply = b""
            self._streaming = False
            self._at = 0

        def write(self, data):
            # Enough of the firmware protocol to get the ACC stream running:
            # rig.open_rig() will not proceed without the identity reply, and
            # the live view reads nothing until "A START".
            cmd = data.decode("utf-8", errors="ignore").strip()
            if cmd == "E":
                self._reply = b"E haptic-piano v2.10.0\n"
            elif cmd.startswith("A START"):
                self._streaming = True
            elif cmd.startswith("A STOP"):
                self._streaming = False
            return len(data)

        def read(self, n=1):
            return b""

        def readline(self):
            if self._reply:
                reply, self._reply = self._reply, b""
                return reply
            if self._streaming and acc_lines:
                line = acc_lines[self._at % len(acc_lines)]
                self._at += 1
                time.sleep(0.001)   # pace it so the GUI thread keeps up
                return line
            return b""

        def reset_input_buffer(self):
            pass

        def reset_output_buffer(self):
            pass

        def flush(self):
            pass

        def close(self):
            self.is_open = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    serial.Serial = FakeSerial

    class FakePort:
        def __init__(self, device, description):
            self.device = self.name = device
            self.description = self.product = description
            self.hwid = "USB VID:PID=1A86:7523"
            self.manufacturer = "wch.cn"
            self.vid, self.pid = 0x1A86, 0x7523
            self.serial_number = "0001"
            self.location = self.interface = None

        def __getitem__(self, i):
            return (self.device, self.description, self.hwid)[i]

        def __iter__(self):
            return iter((self.device, self.description, self.hwid))

    # One port must score strictly highest in serial_utils.auto_detect_port:
    # on a tie it falls through to an interactive input() prompt, and a
    # headless run dies there on EOF.
    list_ports.comports = lambda *a, **k: [
        FakePort("/dev/cu.usbmodem-RIG1", "Teensy USB Serial (rig controller)"),
        FakePort("/dev/cu.Bluetooth-Incoming-Port", "n/a"),
    ]

    # app/midi.py talks to python-rtmidi directly rather than through mido, so
    # it can tell two identical keyboards apart - stub the same layer, or the
    # port pickers photograph empty.
    try:
        import rtmidi

        class FakeMidiIn:
            def __init__(self, *a, **k):
                self._cb = None

            def get_ports(self):
                return ["SE25 MIDI1", "SE25 MIDI1"]

            def get_port_count(self):
                return 2

            def get_port_name(self, i):
                return "SE25 MIDI1"

            def open_port(self, *a, **k):
                return self

            def close_port(self):
                pass

            def set_callback(self, cb, data=None):
                self._cb = cb

            def cancel_callback(self):
                self._cb = None

            def ignore_types(self, *a, **k):
                pass

            def is_port_open(self):
                return True

            def get_message(self):
                return None

            def delete(self):
                pass

        rtmidi.MidiIn = rtmidi.MidiOut = FakeMidiIn
    except Exception:
        pass

    # An integer camera index becomes the demo clip, so the windows that
    # enumerate cameras find two and every preview shows a real frame; any
    # other index fails to open, which is what ends a scan.
    try:
        import cv2

        real_capture = cv2.VideoCapture
        clip = str(sandbox / "demo_camera.mp4")

        def capture(src=None, *a, **k):
            seek = isinstance(src, int) or str(src).endswith("demo_camera.mp4")
            if isinstance(src, int):
                src = clip if src in (0, 1) else "/nonexistent-camera"
            if src is None:
                return real_capture()
            cap = real_capture(src, *a, **k)
            if seek and cap.isOpened():
                cap.set(cv2.CAP_PROP_POS_FRAMES, DEMO_START_FRAME)
            return cap

        cv2.VideoCapture = capture
    except Exception:
        pass

    try:
        import sounddevice as sd

        sd.play = sd.stop = sd.wait = lambda *a, **k: None
    except Exception:
        pass


# ---------------------------------------------------------- window table
# key -> (module, class, launcher section, title, width, height)
WINDOWS = [
    ("launcher",             None, None, "0", "Launcher hub", 0, 0),

    ("wiring_guide",         "app.gui.wiring_guide_window", "WiringGuideWindow", "1", "Wiring Guide", 1150, 820),
    ("camera_selection",     "app.gui.camera_selection_window", "CameraSelectionWindow", "1", "Camera Selection Wizard", 1080, 800),
    ("calibration_wizard",   "app.gui.calibration_wizard", "KeyboardCalibrationWizard", "1", "Keyboard Calibration Wizard", 1180, 850),
    ("midi_mapping",         "app.gui.midi_mapping_wizard", "MidiMappingWizard", "1", "MIDI Mapping Wizard", 1180, 850),
    ("key_preview",          "app.gui.key_preview", "KeyPreviewWindow", "1", "Profile Selection / Region Preview", 1100, 820),
    ("cue_selection",        "app.gui.cue_selection_window", "CueSelectionWindow", "1", "Visual Guidance Cue Selection", 1000, 700),
    ("haptic_config",        "app.gui.haptic_config_window", "HapticConfigWindow", "1", "Haptic Actuator Defaults", 900, 720),

    ("finger_detector",      "app.gui.finger_detector_window", "FingerDetectorWindow", "2", "Live Finger Detection", 1150, 900),
    ("virtual_piano",        "test_virtual_piano_led", "PianoWindow", "2", "Virtual Piano + LED Test", 1400, 720),
    ("haptic_vibrator",      "test_haptic_vibrator", "HapticTestWindow", "2", "Haptic Vibrator Test", 1000, 720),
    ("haptic_motor_bench",   "test_haptic_single_motor", "SingleMotorHapticWindow", "2", "Haptic Motor Bench", 1150, 850),
    ("accelerometer",        "app.gui.accelerometer_window", "AccelerometerWindow", "2", "Accelerometer Live View", 1100, 760),

    ("recording_wizard",     "app.gui.recording_wizard", "RecordingWizard", "3", "Song Recording Wizard", 1180, 850),
    ("song_playback",        "music_playback", "PlaybackWindow", "3", "Song Playback", 1400, 800),

    ("sequence_generator",   "app.gui.sequence_generator_window", "SequenceGeneratorWindow", "4", "Experiment Sequence Generator", 1200, 880),
    ("sequence_metrics",     "app.gui.sequence_metrics_window", "SequenceMetricsWindow", "4", "Sequence / Music Metrics", 1250, 880),
    ("single_song_metrics",  "app.gui.single_song_metrics_window", "SingleSongMetricsWindow", "4", "Single Song Complexity", 1200, 850),

    ("quiz_visual",          "student_quiz", "QuizWindow", "5", "Quiz - Visual Guidance", 1200, 880),
    ("quiz_haptic",          "student_quiz_haptic", "HapticQuizWindow", "5", "Quiz - Haptic Guidance", 1200, 880),

    ("pilot_schedule",       "app.gui.pilot_schedule_window", "PilotScheduleWindow", "6", "Participant Trial Schedule", 1150, 850),
    ("experiment_session",   "app.gui.experiment_session_window", "ExperimentSessionWindow", "6", "Formal Experiment Session", 1150, 850),

    ("quiz_analysis",        "app.gui.quiz_analysis_window", "QuizAnalysisWindow", "7", "Quiz Analysis", 1400, 900),
    ("participant_analysis", "app.gui.participant_analysis_window", "ParticipantAnalysisWindow", "7", "Participant Analysis", 1400, 900),
    ("group_analysis",       "app.gui.group_analysis_window", "GroupAnalysisWindow", "7", "Group Analysis", 1400, 900),
    ("computational_model",  "app.gui.computational_model_window", "ComputationalModelWindow", "7", "Computational Model Analysis", 1400, 900),

    ("remote_setup",         "remote_guidance.setup_wizard", "RemoteSetupWizard", "8", "Tele-training Setup Wizard", 1100, 820),
    ("remote_latency",       "app.gui.remote_latency_analysis_window", "RemoteLatencyAnalysisWindow", "8", "Remote Latency Analysis", 1300, 880),

    ("review_compress",      "app.gui.review_compress_window", "ReviewCompressWindow", "9", "Review Video Compression", 1100, 800),
    ("quiz_backup",          "app.gui.quiz_backup_window", "QuizBackupWindow", "9", "Participant ZIP Backup", 1100, 800),

    ("val_spectrogram",      "app.gui.validation_experiment_window", "SpectrogramWindow", "10", "Actuator Spectrogram", 1200, 880),
    ("val_frequency",        "app.gui.validation_experiment_window", "FrequencySweepWindow", "10", "LRA Frequency Sweep", 1200, 880),
    ("val_amplitude",        "app.gui.validation_experiment_window", "AmplitudeSweepWindow", "10", "LRA Amplitude Sweep", 1200, 880),
    ("val_motor_delay",      "app.gui.validation_experiment_window", "MotorAccDelayWindow", "10", "Motor to ACC Delay", 1200, 880),
    ("val_adhesion",         "app.gui.validation_experiment_window", "AdhesionComparisonWindow", "10", "Adhesion Comparison", 1200, 880),

    ("rhythm_melody_gen",    "app.gui.rhythm_melody_window", "RhythmMelodyWindow", "11", "Rhythm Melody Generator", 1200, 880),
    ("rhythm_melody_play",   "app.gui.rhythm_melody_player_window", "RhythmMelodyPlayerWindow", "11", "Playback Rhythm Melody", 1250, 820),
    ("rhythm_schedule",      "rhythm_study.schedule_window", "RhythmScheduleWindow", "11", "Rhythm Trial Schedule", 1150, 850),
    ("rhythm_session",       "rhythm_study.session_window", "RhythmSessionWindow", "11", "Rhythm Experiment Session", 1150, 850),
    ("rhythm_group",         "rhythm_study.group_analysis_window", "RhythmGroupAnalysisWindow", "11", "Rhythm Group Analysis", 1400, 900),

    ("demo_mode",            "app.gui.demo_mode_window", "DemoModeDialog", "12", "Demo Mode", 900, 700),
    ("about",                "app.gui.about_window", "AboutWindow", "12", "About", 1000, 760),
]

BY_KEY = {w[0]: w for w in WINDOWS}

# Windows that open empty and only show anything once told to load or compute
# something: (button label - matched as a substring, seconds to allow).
RECIPES = {
    "participant_analysis": [("Analyze", 90)],
    "group_analysis": [("Select all", 2), ("Analyse selected participants", 240)],
    "rhythm_group": [("Select all", 2), ("Run analysis", 120)],
    "computational_model": [("Select all", 2), ("Fit model", 600)],
    "sequence_generator": [("Generate All Difficulty Levels", 240)],
    "pilot_schedule": [("Load existing", 15)],
    "experiment_session": [("Load participant", 15)],
    "rhythm_schedule": [("Load existing", 15)],
    "rhythm_session": [("Load participant", 15)],
    "rhythm_melody_gen": [("Preview (writes nothing)", 20)],
    "accelerometer": [("Connect", 12)],
}

# QWizards worth walking, and how many Next presses to photograph.
WIZARD_PAGES = {
    "calibration_wizard": 5,
    "midi_mapping": 1,
    "recording_wizard": 2,
}

# Windows whose tabs are each worth a frame. The analysis windows' tabs do not
# exist until their recipe has run, which is why they are all in here.
TABBED = set(RECIPES) | {
    "quiz_analysis", "sequence_metrics", "single_song_metrics",
    "rhythm_melody_gen", "wiring_guide", "about", "haptic_config",
}


# ---------------------------------------------------------------- worker
def capture_one(key: str, out: Path) -> int:
    """Build one window offscreen and save its frame(s). Run with the sandbox
    as the working directory."""
    sandbox = Path.cwd()
    # This file lives in test-script/, so sys.path[0] is that folder rather
    # than the sandbox root the windows import from.
    sys.path.insert(0, str(sandbox))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _install_stubs(sandbox)

    _, modpath, clsname, _section, _title, want_w, want_h = BY_KEY[key]

    from PySide6.QtCore import QEventLoop, QRect, QTimer
    from PySide6.QtGui import QScreen
    from PySide6.QtWidgets import (
        QApplication, QGroupBox, QMessageBox, QPushButton, QTabWidget,
    )

    app = QApplication(sys.argv[:1])

    w, h = VIRTUAL_SCREEN
    QScreen.availableGeometry = lambda self: QRect(0, 0, w, h)
    QScreen.geometry = lambda self: QRect(0, 0, w, h)
    QScreen.availableSize = lambda self: QRect(0, 0, w, h).size()
    QScreen.size = lambda self: QRect(0, 0, w, h).size()

    # A modal would block a headless run forever.
    for name in ("information", "warning", "critical", "question", "about"):
        setattr(QMessageBox, name,
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))

    from app.config import Config
    from app.gui.wrapping_tabs import WrappingTabWidget

    cfg = Config.load()

    if key == "launcher":
        import launcher as L

        win = L.LauncherWindow(cfg)
        win.resize(*win.preferred_size())
    else:
        cls = getattr(__import__(modpath, fromlist=[clsname]), clsname)
        try:
            win = cls(cfg)
        except TypeError:
            win = cls()
        if want_w and want_h:
            win.resize(want_w, want_h)

    win.show()

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def shot(name):
        pix = win.grab()
        pix.save(str(out / f"{name}.png"))
        print(f"saved {out / name}.png {pix.width()}x{pix.height()}")

    out.mkdir(parents=True, exist_ok=True)
    pump(2500)                       # camera previews and plots need frames
    shot(key)

    def find_tabs():
        """The analysis windows carry a dozen tabs, so they use the project's
        own WrappingTabWidget rather than QTabWidget - it implements the same
        addTab/count/tabText/setCurrentIndex slice. Accept either."""
        found = win.findChildren(QTabWidget) + win.findChildren(WrappingTabWidget)
        return [t for t in found if t.count()]

    def click(text, seconds):
        for b in win.findChildren(QPushButton):
            if text in b.text() and b.isEnabled():
                b.click()
                waited = 0
                while waited < seconds * 1000:   # poll: a fast fit shouldn't
                    pump(1000)                   # cost the whole budget
                    waited += 1000
                    if find_tabs() and waited > 3000:
                        break
                return
        print(f"  (no enabled button matching {text!r})")

    for text, seconds in RECIPES.get(key, []):
        click(text, seconds)
    if key in RECIPES:
        pump(2000)
        shot(f"{key}_loaded")

    if key in TABBED:
        tabs = find_tabs()
        if tabs:
            tw = max(tabs, key=lambda t: t.count())
            for i in range(tw.count()):
                tw.setCurrentIndex(i)
                pump(1200)
                shot(f"{key}_tab{i + 1}-{_slug(tw.tabText(i))}")

    if key == "demo_mode":
        # This window exists to open the participant-facing screens, so tick
        # everything and photograph what it builds - the Teacher and Student
        # clients are otherwise separate processes and out of reach here.
        for box in win.findChildren(QGroupBox):
            if box.isCheckable():
                box.setChecked(True)
        pump(500)
        shot("demo_mode_all_selected")
        for made in win.build_pending(set()):
            made.show()
            pump(4000)
            pix = made.grab()
            name = type(made).__name__.replace("Window", "").replace("App", "").lower()
            pix.save(str(out / f"demo_{name}.png"))
            print(f"saved {out / 'demo_'}{name}.png ({made.windowTitle()})")

    for i in range(WIZARD_PAGES.get(key, 0)):
        # Step 1 of the calibration and recording wizards is "point the camera
        # and press Capture"; skip it and every later page has no frame to
        # draw on and photographs blank.
        for b in win.findChildren(QPushButton):
            if b.text().startswith("Capture") and b.isEnabled():
                b.click()
                pump(1200)
        try:
            win.next()
        except Exception as e:
            print(f"  (not a QWizard, or no page {i + 2}: {e})")
            break
        pump(2000)
        shot(f"{key}_page{i + 2}")

    # Windows that open more than one top-level window (the two session
    # controllers open a trial runner, and one opens a cue screen too).
    for i, extra in enumerate(
        [w for w in app.topLevelWidgets()
         if w is not win and w.isVisible() and w.width() > 200 and w.height() > 120],
        start=1,
    ):
        pix = extra.grab()
        pix.save(str(out / f"{key}_extra{i}.png"))
        print(f"saved {out / key}_extra{i}.png ({extra.windowTitle()})")

    return 0


def _slug(text: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


# ---------------------------------------------------------------- driver
def capture_all(sandbox: Path, out: Path, only) -> int:
    keys = [k for k, *_ in WINDOWS]
    if only:
        unknown = set(only) - set(keys)
        if unknown:
            sys.exit(f"unknown window key(s): {', '.join(sorted(unknown))}")
        keys = [k for k in keys if k in only]

    script = sandbox / "test-script" / Path(__file__).name
    if not script.exists():
        sys.exit(f"{script} is missing - rebuild the sandbox with --build-sandbox")

    ok, bad = [], []
    for key in keys:
        try:
            r = subprocess.run(
                [sys.executable, str(script), "--window", key, "--out", str(out)],
                cwd=sandbox, capture_output=True, text=True, timeout=900,
            )
        except subprocess.TimeoutExpired:
            bad.append((key, "TIMEOUT"))
            print(f"  TIMEOUT  {key}")
            continue
        saved = [ln for ln in r.stdout.splitlines() if ln.startswith("saved")]
        if r.returncode == 0 and saved:
            ok.append(key)
            print(f"  ok       {key}  ({len(saved)} image(s))")
        else:
            tail = (r.stderr or r.stdout).strip().splitlines()
            bad.append((key, tail[-1] if tail else f"exit {r.returncode}"))
            print(f"  FAIL     {key}: {bad[-1][1]}")

    print(f"\n{len(ok)} captured, {len(bad)} failed")
    for key, err in bad:
        print(f"  {key}: {err}")
    return 1 if bad else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--build-sandbox", metavar="DIR", type=Path,
                   help="make a disposable copy of the project to capture from")
    p.add_argument("--sandbox", metavar="DIR", type=Path,
                   help="the sandbox built above")
    p.add_argument("--out", metavar="DIR", type=Path, default=DEFAULT_OUT,
                   help=f"where the PNGs go (default: {DEFAULT_OUT})")
    p.add_argument("--all", action="store_true", help="capture every window")
    p.add_argument("--only", nargs="+", metavar="KEY", help="capture just these")
    p.add_argument("--window", metavar="KEY",
                   help="worker: capture one window, with the sandbox as cwd")
    p.add_argument("--list", action="store_true", help="print every window key")
    args = p.parse_args()

    if args.list:
        for key, _m, _c, section, title, *_ in WINDOWS:
            print(f"{key:24s} section {section:>2s}  {title}")
        return 0

    if args.window:
        return capture_one(args.window, args.out.resolve())

    if args.build_sandbox:
        build_sandbox(args.build_sandbox.resolve())
        if not (args.all or args.only):
            return 0
        args.sandbox = args.sandbox or args.build_sandbox

    if args.all or args.only:
        if not args.sandbox:
            sys.exit("--all/--only needs --sandbox DIR (build one with --build-sandbox)")
        return capture_all(args.sandbox.resolve(), args.out.resolve(), args.only)

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
