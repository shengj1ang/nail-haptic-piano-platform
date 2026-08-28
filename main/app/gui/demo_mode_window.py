"""Demo Mode picker - launcher section 12.

Demo Mode is pure presentation: it opens the platform's participant- and
tele-training-facing windows so they can be photographed for slides, a
report or a poster, without a rig, a relay or a second person. It adds no
behaviour of its own - each window is the real one, shown the way it looks
in use.

This dialog is only the chooser. It lets you pick any combination of:

  - Visual Cue - the real finger-cue screen (app.gui.cue_window.CueWindow),
    with a finger you pick already highlighted, in either the dot or the
    hand style.
  - Teacher Client and Student Client - the tele-training windows
    (remote_guidance.*), each opened in DEMO MODE: they jump straight to
    their session page with no server, no login and no peer (see the
    windows' `demo` flag), and their camera / MIDI / LED still open locally
    from the normal in-window buttons, so a photo shows the live view.

The launcher (launcher.py) opens the windows this panel hands it and keeps
them: they are ordinary top-level windows, so unlike the one-at-a-time tools
they all stay open together, which is the whole point - a teacher screen and
a student screen side by side.

The panel stays open after Open, as a live control surface rather than a
one-shot chooser: Open only opens what is ticked and not already open (the
launcher reads that from the windows themselves), so more windows can be
added at any time. Closing a demo window changes nothing here on purpose -
its box stays as it was, and ticking it and pressing Open again re-opens it.

Two windows cannot hold the SAME physical camera at once, so each client
gets its own camera picker here; point them at different indices (or leave
one without a camera) to photograph both live at the same time.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.gui.cue_window import CUE_STYLES, DEFAULT_CUE_STYLE, FINGER_ORDER, CueWindow
from remote_guidance.config import RemoteGuidanceConfig
from remote_guidance.student.window import StudentRemoteWindow
from remote_guidance.teacher.window import TeacherRemoteWindow

# Camera indices offered per client. Not probed (probing opens and releases
# each device and takes a noticeable moment) - the operator knows which
# index is which, and "Configured" covers the common case of just using
# what the client is already set to.
CAMERA_CHOICES = list(range(6))

STYLE_SHEET = """
QDialog { background: #1e1f24; }
QLabel { color: #d8d9e0; }
QLabel#hint { color: #9a9ba5; font-size: 12px; }
QGroupBox {
    color: #d8d9e0;
    font-weight: 600;
    border: 1px solid #35363e;
    border-radius: 10px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    background: #26272e;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: #7fb2ff;
}
QGroupBox::indicator, QCheckBox::indicator { width: 16px; height: 16px; }
QCheckBox { color: #eceef2; }
QComboBox {
    background: #2f303a;
    color: #eceef2;
    border: 1px solid #3a3b44;
    border-radius: 5px;
    padding: 4px 8px;
}
QComboBox:disabled { color: #7d7e88; }
QPushButton {
    padding: 7px 16px;
    border-radius: 6px;
    border: 1px solid #3a3b44;
    background: #2f303a;
    color: #eceef2;
}
QPushButton:hover { background: #3a3c48; border-color: #7fb2ff; }
QPushButton:default { border-color: #7fb2ff; }
"""


class DemoModeDialog(QDialog):
    """A live panel for opening demo windows for screenshots.

    Stays open after Open (it is non-modal), so windows can be added over
    time. `openRequested` fires when Open is pressed; the launcher answers
    by calling `build_pending()` with the kinds already on screen, so an
    already-open window is never re-opened."""

    openRequested = Signal()

    def __init__(self, cfg: Config, parent: Optional[QWidget] = None,
                 initially_open: Optional[set] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.remote = RemoteGuidanceConfig.load()
        # Which kinds are already open, so re-opening the panel reflects
        # reality; empty (the usual first open) falls back to a tick on the
        # cue as a friendly default.
        self._initially_open = set(initially_open or ())
        self.setWindowTitle("Demo Mode")
        self.setStyleSheet(STYLE_SHEET)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        intro = QLabel(
            "Open the real windows for screenshots — no rig, relay or second "
            "person needed. Tick what you want, then Open. This panel stays "
            "open, so you can add more windows or re-open one you closed."
        )
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        root.addWidget(self._build_cue_group())
        root.addWidget(self._build_teacher_group())
        root.addWidget(self._build_student_group())

        cam_note = QLabel(
            "Two windows can’t share one camera — give the Teacher and Student "
            "different camera indices to photograph both live at once."
        )
        cam_note.setObjectName("hint")
        cam_note.setWordWrap(True)
        root.addWidget(cam_note)

        # "Open selected" is an ActionRole button: it does NOT close the
        # dialog (that is the whole point of the panel). Close just dismisses
        # the panel; the windows it opened stay open.
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        open_btn = buttons.addButton("Open selected", QDialogButtonBox.ButtonRole.ActionRole)
        open_btn.clicked.connect(self.openRequested)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def _checked_default(self, kind: str) -> bool:
        """Tick a box if its window is already open (so re-opening the panel
        mirrors reality); with nothing open, tick only the cue as a friendly
        first-time default."""
        if self._initially_open:
            return kind in self._initially_open
        return kind == "cue"

    def _build_cue_group(self) -> QGroupBox:
        box = QGroupBox("Visual Cue screen")
        box.setCheckable(True)
        box.setChecked(self._checked_default("cue"))
        lay = QVBoxLayout(box)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style:"))
        self.cue_style = QComboBox()
        for key, label in CUE_STYLES.items():
            self.cue_style.addItem(label, key)
        configured_style = getattr(self.cfg, "visual_cue_style", DEFAULT_CUE_STYLE)
        idx = self.cue_style.findData(configured_style)
        self.cue_style.setCurrentIndex(idx if idx >= 0 else 0)
        style_row.addWidget(self.cue_style, 1)
        lay.addLayout(style_row)

        finger_row = QHBoxLayout()
        finger_row.addWidget(QLabel("Highlight finger:"))
        self.cue_finger = QComboBox()
        self.cue_finger.addItem("None (idle screen)", None)
        for finger in FINGER_ORDER:
            self.cue_finger.addItem(finger, finger)
        # A sensible default that is obviously "a finger is lit" in a photo.
        default_finger = self.cue_finger.findData("R2")
        self.cue_finger.setCurrentIndex(default_finger if default_finger >= 0 else 0)
        finger_row.addWidget(self.cue_finger, 1)
        lay.addLayout(finger_row)

        self.cue_group = box
        return box

    def _client_group(self, kind: str, title: str, configured_index) -> tuple[QGroupBox, QComboBox, QCheckBox]:
        box = QGroupBox(title)
        box.setCheckable(True)
        box.setChecked(self._checked_default(kind))
        lay = QVBoxLayout(box)

        cam_row = QHBoxLayout()
        cam_row.addWidget(QLabel("Camera:"))
        cam = QComboBox()
        cam.addItem(f"Configured (index {configured_index})", None)
        for index in CAMERA_CHOICES:
            cam.addItem(f"Camera {index}", index)
        cam_row.addWidget(cam, 1)
        lay.addLayout(cam_row)

        open_cam = QCheckBox("Open camera on launch")
        open_cam.setChecked(True)
        lay.addWidget(open_cam)

        return box, cam, open_cam

    def _build_teacher_group(self) -> QGroupBox:
        box, cam, open_cam = self._client_group(
            "teacher", "Teacher Client (offline)", self.remote.teacher.camera.index
        )
        self.teacher_group, self.teacher_camera, self.teacher_open_cam = box, cam, open_cam
        return box

    def _build_student_group(self) -> QGroupBox:
        box, cam, open_cam = self._client_group(
            "student", "Student Client (offline)", self.remote.student.camera.index
        )
        self.student_group, self.student_camera, self.student_open_cam = box, cam, open_cam
        return box

    # ------------------------------------------------------------------
    # Panel <-> launcher
    # ------------------------------------------------------------------

    def build_pending(self, already_open: set) -> List[QWidget]:
        """Construct the ticked windows that are NOT already open, tagged and
        configured but not shown.

        `already_open` is the set of kinds ("cue"/"teacher"/"student") the
        launcher currently has open, so Open only ever *adds* windows - it
        never re-opens or duplicates one that is already on screen.

        Each window carries `demo_kind` (so the launcher can key it and untick
        the right box when it closes) and `demo_autostart` - a callable the
        launcher runs once the window is on screen to open its camera (opening
        it before show would just photograph a blank frame), or None."""
        windows: List[QWidget] = []

        if self.cue_group.isChecked() and "cue" not in already_open:
            windows.append(self._make_cue_window())

        if self.teacher_group.isChecked() and "teacher" not in already_open:
            windows.append(self._make_client_window(
                "teacher",
                TeacherRemoteWindow,
                self.teacher_camera.currentData(),
                self.teacher_open_cam.isChecked(),
                autostart_method="_start_live_session",
            ))

        if self.student_group.isChecked() and "student" not in already_open:
            windows.append(self._make_client_window(
                "student",
                StudentRemoteWindow,
                self.student_camera.currentData(),
                self.student_open_cam.isChecked(),
                autostart_method="_start_session",
            ))

        return windows

    def _make_cue_window(self) -> CueWindow:
        style = self.cue_style.currentData() or DEFAULT_CUE_STYLE
        finger = self.cue_finger.currentData()
        window = CueWindow(style)
        window.setWindowTitle("Demo — Visual Cue")
        if finger:
            window.set_target(finger, f"finger {finger}")
        else:
            window.set_target(None, "Visual guidance cue")
        window.demo_kind = "cue"
        window.demo_autostart = None
        return window

    def _make_client_window(self, kind: str, window_cls, camera_index,
                            open_camera: bool, autostart_method: str):
        window = window_cls(self.cfg, self.remote, demo=True)
        # Override the camera only when a specific index was picked; None
        # means "leave the client on its configured camera".
        if camera_index is not None:
            window.cfg.camera.index = camera_index
        window.demo_kind = kind
        window.demo_autostart = getattr(window, autostart_method) if open_camera else None
        return window
