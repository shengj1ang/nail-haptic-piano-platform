"""The on-screen implementation of app.quiz.CueOutput - a standalone
window meant to be dragged onto a second monitor and maximized/
fullscreened, showing only the "press this finger" cue (ten dots standing
in for two hands, same convention as music_playback.py's HandsWidget) plus
a short status line (countdown, note name, ...).

Everything is drawn from scratch in paintEvent, sized off the widget's
current width/height rather than fixed pixel constants, so it scales
cleanly at any window size - including fullscreen on an external display.

This is deliberately the only place that knows the cue is currently a
screen: swap it for a vibration-motor CueOutput later and nothing in
student_quiz.py's quiz-running logic needs to change.
"""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QMainWindow, QWidget

from ..quiz import CueOutput

# Left hand pinky-to-thumb, then right hand thumb-to-pinky, so the two
# thumbs land in the middle - the same order/convention music_playback.py
# uses for its finger dots.
FINGER_ORDER = ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"]

BG_COLOR = QColor(24, 25, 29)
IDLE_COLOR = QColor(70, 70, 78)
ACTIVE_COLOR = QColor(255, 140, 0)
TEXT_COLOR = QColor(240, 240, 245)


class FingerCueWidget(QWidget):
    """Ten labeled circles (5 per hand) plus a status line, all sized off
    self.width()/self.height() so the whole thing scales with the window."""

    def __init__(self):
        super().__init__()
        self.active_finger: Optional[str] = None
        self.message = "Waiting..."
        self.setMinimumSize(320, 160)

    def set_target(self, finger: Optional[str], message: str = "") -> None:
        self.active_finger = finger
        self.message = message
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG_COLOR)

        w, h = self.width(), self.height()

        # Status/message line across the top third.
        if self.message:
            msg_font = painter.font()
            msg_font.setPointSizeF(max(h * 0.07, 10))
            msg_font.setBold(True)
            painter.setFont(msg_font)
            painter.setPen(QPen(TEXT_COLOR))
            painter.drawText(0, 0, w, int(h * 0.32), Qt.AlignmentFlag.AlignCenter, self.message)

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

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Finger Cue")
        self.widget = FingerCueWidget()
        self.setCentralWidget(self.widget)
        self.resize(900, 380)

    def set_target(self, finger: Optional[str], message: str = "") -> None:
        self.widget.set_target(finger, message)


class ScreenCueOutput(CueOutput):
    """Qt-window-backed CueOutput - see the module docstring for why this
    is the only file that should ever import Qt for the cue itself."""

    def __init__(self):
        self.window = CueWindow()
        self.window.show()

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
