"""The on-screen implementation of app.quiz.CueOutput - a standalone
window meant to be dragged onto a second monitor and maximized/
fullscreened, showing only the "press this finger" cue plus a short status
line (countdown, note name, ...).

Two cue styles are supported (see CUE_STYLES):

- "dot": ten dots standing in for two hands, same convention as
  music_playback.py's HandsWidget.
- "hand": one of the eleven hand-photo assets under app/assets/image/
  (HAND.jpg for idle, L1-L5/R1-R5 with that finger highlighted).

Which style to use is chosen ahead of time in the launcher's Visual
Guidance Cue Selection window (app.gui.cue_selection_window) and stored
in config.json as Config.visual_cue_style - the quiz reads it from there
on launch, so there's no per-session prompt and no reason to support
switching styles mid-quiz.

Everything is drawn from scratch in paintEvent, sized off the widget's
current width/height rather than fixed pixel constants, so it scales
cleanly at any window size - including fullscreen on an external display.

This is deliberately the only place that knows the cue is currently a
screen: swap it for a vibration-motor CueOutput later and nothing in
student_quiz.py's quiz-running logic needs to change.
"""

from pathlib import Path
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
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

# The "hand" style's photos are white-background line art, so the dark
# dot-style background/text would clash - use a white/black pair just
# for that style instead.
IMAGE_BG_COLOR = QColor(255, 255, 255)
IMAGE_TEXT_COLOR = QColor(0, 0, 0)

# key -> human-readable label, in the order they should be offered to pick
# from (see app.gui.cue_selection_window, which presents these).
CUE_STYLES = {
    "dot": "Dot view (circles)",
    "hand": "Hand view (photos)",
}
DEFAULT_CUE_STYLE = "dot"

# How long each finger's photo is held when the "hand" style is cueing a
# chord. That style has one photo per finger and no combined assets, so
# the fingers are shown in turn; ~5 Hz reads as "all of these" without
# tipping into flicker. Only ever reached through set_targets() with more
# than one finger - a single-finger cue never starts the timer.
HAND_CYCLE_MS = 200

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
        # The full set, for a chord. Single-finger cues leave this at
        # [finger] and behave exactly as they always have - see
        # set_targets() for why that guarantee matters.
        self.active_fingers: List[str] = []
        self.message = "Waiting..."
        self.style = style
        self.status_only = False
        self._cycle_timer: Optional[QTimer] = None
        self._cycle_index = 0
        self.setMinimumSize(320, 160)

    def set_target(self, finger: Optional[str], message: str = "") -> None:
        """One finger. Unchanged: this is what the local quiz and the main
        study's cue screen call, and neither can reach the chord path."""
        self.active_finger = finger
        self.active_fingers = [finger] if finger else []
        self.message = message
        self.status_only = False
        self._stop_cycle()
        self.update()

    def set_targets(self, fingers: Sequence[str], message: str = "") -> None:
        """Several fingers at once - a chord, from remote guidance only.

        The dot style simply highlights all of them. The hand style has
        one photo per finger and no combined assets, so it cycles through
        them instead: at HAND_CYCLE_MS each, the fingers of a chord read
        as a set rather than as one instruction.

        With one finger this is identical to set_target() - no timer is
        started and the paint is the same - so nothing that cues single
        notes can be changed by this method existing."""
        fingers = [f for f in fingers if f]
        self.active_fingers = list(fingers)
        self.active_finger = fingers[0] if fingers else None
        self.message = message
        self.status_only = False
        self._cycle_index = 0
        if self.style == "hand" and len(fingers) > 1:
            self._start_cycle()
        else:
            self._stop_cycle()
        self.update()

    # -- hand-photo cycling ---------------------------------------------

    def _start_cycle(self) -> None:
        if self._cycle_timer is None:
            self._cycle_timer = QTimer(self)
            self._cycle_timer.timeout.connect(self._advance_cycle)
        if not self._cycle_timer.isActive():
            self._cycle_timer.start(HAND_CYCLE_MS)

    def _stop_cycle(self) -> None:
        if self._cycle_timer is not None and self._cycle_timer.isActive():
            self._cycle_timer.stop()
        self._cycle_index = 0

    def _advance_cycle(self) -> None:
        if len(self.active_fingers) < 2:
            self._stop_cycle()
            return
        self._cycle_index = (self._cycle_index + 1) % len(self.active_fingers)
        self.update()

    @property
    def cycling(self) -> bool:
        return self._cycle_timer is not None and self._cycle_timer.isActive()

    def set_status(self, message: str) -> None:
        """Status-only mode: the whole surface becomes one centered
        (multi-line) message, with no finger graphics at all. The pilot
        study's persistent cue screen uses this during key-only and haptic
        trials, where drawing the dots/hand photos would leak a visual cue
        into a condition that must not have one."""
        self.active_finger = None
        self.active_fingers = []
        self.message = message
        self.status_only = True
        self._stop_cycle()
        self.update()

    def set_style(self, style: str) -> None:
        if style not in CUE_STYLES:
            raise ValueError(f"Unknown cue style {style!r}, expected one of {list(CUE_STYLES)}")
        self.style = style
        # Cycling only exists for the hand style; switching away from it
        # mid-chord must not leave a timer repainting nothing.
        if style == "hand" and len(self.active_fingers) > 1 and not self.status_only:
            self._start_cycle()
        else:
            self._stop_cycle()
        self.update()

    def paintEvent(self, event) -> None:
        is_hand = self.style == "hand"
        bg_color = IMAGE_BG_COLOR if is_hand else BG_COLOR
        text_color = IMAGE_TEXT_COLOR if is_hand else TEXT_COLOR

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), bg_color)

        w, h = self.width(), self.height()

        if self.status_only:
            msg_font = painter.font()
            msg_font.setPointSizeF(max(h * 0.08, 12))
            msg_font.setBold(True)
            painter.setFont(msg_font)
            painter.setPen(QPen(text_color))
            # Word-wrap inside a margin - status lines can be full sentences
            # ("Drag this window onto the participant-facing display...")
            # and must never run off the window edge.
            margin_x, margin_y = int(w * 0.05), int(h * 0.05)
            painter.drawText(
                self.rect().adjusted(margin_x, margin_y, -margin_x, -margin_y),
                int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
                self.message,
            )
            return

        # Status/message line across the top third.
        if self.message:
            msg_font = painter.font()
            msg_font.setPointSizeF(max(h * 0.07, 10))
            msg_font.setBold(True)
            painter.setFont(msg_font)
            painter.setPen(QPen(text_color))
            painter.drawText(0, 0, w, int(h * 0.32), Qt.AlignmentFlag.AlignCenter, self.message)

        if self.style == "hand":
            self._paint_image(painter, w, h)
        else:
            self._paint_circles(painter, w, h)

    def _paint_image(self, painter: QPainter, w: int, h: int) -> None:
        # For a chord this is whichever finger the cycle is currently on;
        # for one finger (every local quiz and main-study trial) the list
        # holds exactly that finger and the index stays 0.
        shown = None
        if self.active_fingers:
            shown = self.active_fingers[self._cycle_index % len(self.active_fingers)]

        if shown in FINGER_IMAGE_FILENAMES:
            pixmap = _load_pixmap(shown, FINGER_IMAGE_FILENAMES[shown])
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
            # Every finger of a chord lights at once - ten dots can show a
            # set, unlike the one-photo-per-finger hand style.
            active = finger_id in self.active_fingers
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

    def set_targets(self, fingers: Sequence[str], message: str = "") -> None:
        self.widget.set_targets(fingers, message)

    def set_status(self, message: str) -> None:
        self.widget.set_status(message)

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

    def show_targets(self, targets) -> None:
        """A chord: every note in the label, every finger on the canvas.

        One target falls straight through to show_target(), so this cannot
        change what a single-note cue looks like."""
        from ..keyboard.midi_mapping import note_name

        if len(targets) <= 1:
            super().show_targets(targets)
            return

        notes = " + ".join(note_name(note) for note, _ in targets)
        fingers = [finger for _, finger in targets if finger]
        # Named in full even on the hand style, where the photos can only
        # be shown one at a time - the text is what makes the chord
        # unambiguous while the pictures cycle.
        label = f"Press: {notes}"
        label += f"  -  fingers {' + '.join(fingers)}" if fingers else "  -  fingers unknown"
        self.window.set_targets(fingers, label)

    def show_message(self, text: str) -> None:
        self.window.set_target(None, text)

    def clear(self) -> None:
        self.window.set_target(None, "")

    def flush(self) -> None:
        """Repaint now instead of at the next event-loop pass.

        set_target() only schedules an update(), so the moment
        show_target() returns is *before* the cue is on screen. The quiz
        doesn't care (its timing error is measured against the same
        convention throughout), but the remote module has to timestamp
        the instant the visual cue actually became visible, so it calls
        this and stamps afterwards - see remote_guidance/cue_outputs.py.
        Optional: nothing else calls it, and behaviour is unchanged for
        callers that don't."""
        self.window.widget.repaint()

    def close(self) -> None:
        self.window.close()
