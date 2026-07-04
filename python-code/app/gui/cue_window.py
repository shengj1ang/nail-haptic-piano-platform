"""The on-screen implementation of app.quiz.CueOutput - a standalone
window meant to be dragged onto a second monitor and maximized/
fullscreened, showing only the "press this finger" cue plus a short status
line (countdown, note name, ...).

Two cue styles are supported (see CUE_STYLES):

- "circles": ten dots standing in for two hands, same convention as
  music_playback.py's HandsWidget.
- "images": one of the eleven hand-photo assets under app/assets/image/
  (HAND.jpg for idle, L1-L5/R1-R5 with that finger highlighted).

choose_cue_style() is a blocking dialog that asks which one to use - call
it once, before building the quiz window (and the cue window it opens),
since the cue window may get dragged onto a second monitor and there's no
good reason to support switching styles mid-quiz.

Everything is drawn from scratch in paintEvent, sized off the widget's
current width/height rather than fixed pixel constants, so it scales
cleanly at any window size - including fullscreen on an external display.

This is deliberately the only place that knows the cue is currently a
screen: swap it for a vibration-motor CueOutput later and nothing in
student_quiz.py's quiz-running logic needs to change.
"""

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from ..quiz import CueOutput

# Left hand pinky-to-thumb, then right hand thumb-to-pinky, so the two
# thumbs land in the middle - the same order/convention music_playback.py
# uses for its finger dots.
FINGER_ORDER = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]

BG_COLOR = QColor(24, 25, 29)
IDLE_COLOR = QColor(70, 70, 78)
ACTIVE_COLOR = QColor(255, 140, 0)
TEXT_COLOR = QColor(240, 240, 245)

# The "images" style's hand photos are white-background line art, so the
# dark circles-style background/text would clash - use a white/black pair
# just for that style instead.
IMAGE_BG_COLOR = QColor(255, 255, 255)
IMAGE_TEXT_COLOR = QColor(0, 0, 0)

# key -> human-readable label, in the order they should be offered to pick
# from (see choose_cue_style() below).
CUE_STYLES = {
    "circles": "Circles (dots)",
    "images": "Hand images",
}
DEFAULT_CUE_STYLE = "circles"


class CueStyleSelectionCancelled(Exception):
    """Raised by choose_cue_style() when the dialog is closed (the window's
    close button, Escape, ...) instead of confirmed with Ok - callers must
    not proceed to build a quiz/cue window in that case."""


def choose_cue_style(parent: Optional[QWidget] = None) -> str:
    """Blocking "which cue style?" dialog - call this once, before
    constructing the quiz window, to pick which of CUE_STYLES the cue
    window should use for the whole session.

    Raises CueStyleSelectionCancelled if the dialog is dismissed without
    picking Ok, since there's no sane default to silently fall back to."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Visual Cue Style")
    dialog.setModal(True)

    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("How should the target finger be shown on the cue screen?"))

    combo = QComboBox()
    for key, name in CUE_STYLES.items():
        combo.addItem(name, key)
    combo.setCurrentIndex(max(combo.findData(DEFAULT_CUE_STYLE), 0))
    layout.addWidget(combo)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)

    result = dialog.exec()
    if result != QDialog.DialogCode.Accepted:
        raise CueStyleSelectionCancelled()
    return combo.currentData()


IMAGE_DIR = Path(__file__).resolve().parent.parent / "assets" / "image"
# One highlighted-hand photo per finger, plus a no-finger idle photo -
# filenames match FINGER_ORDER's finger ids exactly.
IDLE_IMAGE_FILENAME = "HAND.jpg"
FINGER_IMAGE_FILENAMES = {finger_id: f"{finger_id}.png" for finger_id in FINGER_ORDER}

# Loaded lazily (QPixmap needs a QApplication) and cached, since these are
# large fixed assets reloaded from disk on every set_target() otherwise.
_PIXMAP_CACHE: dict[str, QPixmap] = {}


def _load_pixmap(cache_key: str, filename: str) -> QPixmap:
    if cache_key not in _PIXMAP_CACHE:
        _PIXMAP_CACHE[cache_key] = QPixmap(str(IMAGE_DIR / filename))
    return _PIXMAP_CACHE[cache_key]


class FingerCueWidget(QWidget):
    """The finger cue itself - either ten labeled circles (5 per hand) or a
    highlighted-hand photo, plus a status line, all sized off
    self.width()/self.height() so the whole thing scales with the window."""

    def __init__(self, style: str = DEFAULT_CUE_STYLE):
        super().__init__()
        self.active_finger: Optional[str] = None
        self.message = "Waiting..."
        self.style = style
        self.setMinimumSize(320, 160)

    def set_target(self, finger: Optional[str], message: str = "") -> None:
        self.active_finger = finger
        self.message = message
        self.update()

    def set_style(self, style: str) -> None:
        if style not in CUE_STYLES:
            raise ValueError(f"Unknown cue style {style!r}, expected one of {list(CUE_STYLES)}")
        self.style = style
        self.update()

    def paintEvent(self, event) -> None:
        is_images = self.style == "images"
        bg_color = IMAGE_BG_COLOR if is_images else BG_COLOR
        text_color = IMAGE_TEXT_COLOR if is_images else TEXT_COLOR

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), bg_color)

        w, h = self.width(), self.height()

        # Status/message line across the top third.
        if self.message:
            msg_font = painter.font()
            msg_font.setPointSizeF(max(h * 0.07, 10))
            msg_font.setBold(True)
            painter.setFont(msg_font)
            painter.setPen(QPen(text_color))
            painter.drawText(0, 0, w, int(h * 0.32), Qt.AlignmentFlag.AlignCenter, self.message)

        if self.style == "images":
            self._paint_image(painter, w, h)
        else:
            self._paint_circles(painter, w, h)

    def _paint_image(self, painter: QPainter, w: int, h: int) -> None:
        if self.active_finger in FINGER_IMAGE_FILENAMES:
            pixmap = _load_pixmap(self.active_finger, FINGER_IMAGE_FILENAMES[self.active_finger])
        else:
            pixmap = _load_pixmap("_idle", IDLE_IMAGE_FILENAME)
        if pixmap.isNull():
            return

        # Same top third reserved for the message line as the circles style,
        # the rest is the image area.
        area_y = int(h * 0.32)
        area_h = h - area_y
        scaled = pixmap.scaled(w, area_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        x = (w - scaled.width()) // 2
        y = area_y + (area_h - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

    def _paint_circles(self, painter: QPainter, w: int, h: int) -> None:
        # Ten dots across the bottom two-thirds. All gaps are equal except
        # the one between L1 and R1 (the hand split), which is doubled so
        # it's visually obvious which five dots belong to which hand.
        margin = w * 0.04
        usable_w = w - 2 * margin
        # 10 dots + 12 gap "units" (11 normal-sized gaps around/between the
        # dots, with the hand-split gap counting as 2 of those units).
        dot_d = min(usable_w / 13.0, h * 0.55)
        gap = (usable_w - 10 * dot_d) / 12.0
        hand_gap = gap * 2.0
        y = h * 0.62 - dot_d / 2.0

        dot_font = painter.font()
        dot_font.setPointSizeF(max(dot_d * 0.34, 8))
        dot_font.setBold(True)
        painter.setFont(dot_font)

        x = margin + gap
        for finger_id in FINGER_ORDER:
            active = finger_id == self.active_finger
            painter.setBrush(ACTIVE_COLOR if active else IDLE_COLOR)
            painter.setPen(QPen(QColor(10, 10, 10), max(dot_d * 0.02, 1.0)))
            painter.drawEllipse(int(x), int(y), int(dot_d), int(dot_d))
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.drawText(int(x), int(y), int(dot_d), int(dot_d), Qt.AlignmentFlag.AlignCenter, finger_id)
            x += dot_d + (hand_gap if finger_id == "L1" else gap)


class CueWindow(QMainWindow):
    """Standalone top-level window - drag it to a second monitor and
    maximize/fullscreen it (F11 or the OS's own fullscreen control), the
    content keeps scaling to fill whatever space it's given."""

    def __init__(self, style: str = DEFAULT_CUE_STYLE):
        super().__init__()
        self.setWindowTitle("Finger Cue")
        self.widget = FingerCueWidget(style)
        self.setCentralWidget(self.widget)
        self.resize(900, 380)

    def set_target(self, finger: Optional[str], message: str = "") -> None:
        self.widget.set_target(finger, message)

    def set_style(self, style: str) -> None:
        self.widget.set_style(style)


class ScreenCueOutput(CueOutput):
    """Qt-window-backed CueOutput - see the module docstring for why this
    is the only file that should ever import Qt for the cue itself."""

    def __init__(self, style: str = DEFAULT_CUE_STYLE):
        self.window = CueWindow(style)
        self.window.show()

    def set_style(self, style: str) -> None:
        self.window.set_style(style)

    def show_target(self, note: int, finger: Optional[str]) -> None:
        from ..keyboard.midi_mapping import note_name

        label = f"Press: {note_name(note)}"
        if finger:
            label += f"  -  finger {finger}"
        else:
            label += "  -  finger unknown"
        self.window.set_target(finger, label)

    def show_message(self, text: str) -> None:
        self.window.set_target(None, text)

    def clear(self) -> None:
        self.window.set_target(None, "")

    def close(self) -> None:
        self.window.close()
