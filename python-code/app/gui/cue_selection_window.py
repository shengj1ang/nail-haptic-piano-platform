"""Visual Guidance Cue Selection window.

Quiz - Visual Guidance can show its finger cue as either ten dots
("circles", demonstrated by DOT.gif) or highlighted hand photos
("images", demonstrated by HAND.gif) - see app.gui.cue_window.CUE_STYLES.

This window is the one place that choice is made: it plays both demo GIFs
stacked vertically, and clicking either a GIF or its radio button selects
that style and saves it straight into config.json (Config.visual_cue_style).
The quiz then just reads the stored value on launch instead of asking
every time, so the choice only needs making once - it lives in the
launcher's "Initial Setup" section alongside the other run-once wizards.
"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImageReader, QMovie
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QLabel,
    QMainWindow,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from .cue_window import CUE_STYLES, IMAGE_DIR

# Style key -> (demo GIF filename, preview width px, short description under
# the radio label). Heights follow each GIF's own aspect ratio. DOT.gif is a
# short wide strip so it can afford to be wide; HAND.gif is nearly square, so
# at the same width it would tower over everything - it gets a smaller width
# to keep the two options at a similar visual weight.
STYLE_PREVIEWS = {
    "circles": ("DOT.gif", 560, "Ten dots, one per finger - the target finger's dot lights up."),
    "images": ("HAND.gif", 360, "Hand photos - the target finger is highlighted on the photo."),
}

WINDOW_WIDTH = 640


class _CueStyleOption(QFrame):
    """One selectable option: a radio button + description + looping demo
    GIF. The whole frame is click-to-select, not just the radio button,
    since the GIF is the thing the user is actually judging."""

    def __init__(self, style_key: str, on_selected):
        super().__init__()
        self.style_key = style_key
        self._on_selected = on_selected
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        gif_name, preview_width, description = STYLE_PREVIEWS[style_key]
        gif_path = IMAGE_DIR / gif_name

        self.radio = QRadioButton(CUE_STYLES[style_key])
        radio_font = self.radio.font()
        radio_font.setBold(True)
        self.radio.setFont(radio_font)
        # toggled fires for both the button being checked and unchecked -
        # only react to the newly-checked one.
        self.radio.toggled.connect(lambda checked: checked and self._on_selected(self.style_key))

        gif_label = QLabel()
        gif_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        source_size = QImageReader(str(gif_path)).size()
        movie = QMovie(str(gif_path))
        if source_size.isValid():
            scaled_h = round(preview_width * source_size.height() / source_size.width())
            movie.setScaledSize(QSize(preview_width, scaled_h))
        gif_label.setMovie(movie)
        movie.start()
        self._movie = movie  # keep alive - QLabel doesn't own its movie

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.addWidget(self.radio)
        layout.addWidget(QLabel(description))
        layout.addWidget(gif_label)

    def mousePressEvent(self, event) -> None:
        self.radio.setChecked(True)
        super().mousePressEvent(event)


class CueSelectionWindow(QMainWindow):
    """Pick the visual guidance cue style once and persist it to
    config.json - Quiz - Visual Guidance reads it from there on launch."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Visual Guidance Cue Selection")
        self.cfg = cfg

        heading = QLabel("How should Quiz - Visual Guidance show the target finger?")
        heading_font = heading.font()
        heading_font.setPointSize(heading_font.pointSize() + 2)
        heading_font.setBold(True)
        heading.setFont(heading_font)

        self.status = QLabel("")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(QLabel("Click an option below - the choice is saved to config.json immediately."))

        # Set before any option exists so a toggled signal fired during
        # setup can never reach _select_style ahead of this attribute.
        self._loading = True

        self._options = [_CueStyleOption(key, self._select_style) for key in STYLE_PREVIEWS]

        # The two radio buttons live in separate option frames, so Qt's
        # per-parent-widget auto-exclusivity does NOT cover them - without a
        # shared QButtonGroup both could end up checked at once. The group
        # is exclusive by default: checking one unchecks the other.
        self._radio_group = QButtonGroup(self)
        for option in self._options:
            self._radio_group.addButton(option.radio)
            layout.addWidget(option)

        layout.addWidget(self.status)
        layout.addStretch(1)

        # The two GIF previews plus text can outgrow a laptop screen -
        # scroll rather than clip.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)
        self.resize(WINDOW_WIDTH, 720)

        # Reflect the currently-stored choice on open. _loading suppresses
        # the save inside _select_style (rewriting the value we just read
        # would be pointless), without blocking the radio's signals - the
        # QButtonGroup needs those to keep exclusivity working.
        stored = cfg.visual_cue_style if cfg.visual_cue_style in STYLE_PREVIEWS else "circles"
        for option in self._options:
            if option.style_key == stored:
                option.radio.setChecked(True)
        self._loading = False
        self.status.setText(f"Current choice: {CUE_STYLES[stored]}")

    def _select_style(self, style_key: str) -> None:
        """Persist every selection to config.json the moment it's made -
        there is no separate Save button, closing the window keeps whatever
        was picked last."""
        if self._loading:
            return
        self.cfg.visual_cue_style = style_key
        self.cfg.save()
        self.status.setText(f"Saved: {CUE_STYLES[style_key]} (config.json updated)")
