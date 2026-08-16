"""One look for the three windows of the quiz audit loop - Quiz Analysis
(app/gui/quiz_analysis_window.py), Quiz Detail
(app/gui/quiz_detail_window.py) and the per-event review
(app/gui/event_review_window.py) - which are used one after another and
so should not look like three different programs.

Applied per window (setStyleSheet on the central widget), never on the
QApplication: the launcher owns a dozen other windows that nobody asked
to restyle.

Written to look the same on macOS, Windows and Linux: greys are rgba
over whatever the system palette paints and text colours are left to the
palette (so a dark desktop theme still works), no font sizes are set
(the three platforms ship different default UI fonts), and no subcontrol
that needs a bitmap - combo-box arrows, checkbox indicators - is
restyled, so those keep their native look everywhere. The accents are
the only fixed colours, one meaning each:

  #primaryBtn  blue    - runs the video pipeline: the main action
  #syncBtn     teal    - video/MIDI alignment (solid), #cellBtn is its
                         in-table outline twin
  #dataBtn     indigo  - recomputes or exports from stored data, no
                         video pass (#exportBtn is the outline twin)
  #saveBtn     amber   - writes a new value into results.json
  #confirmBtn  green   - records a verdict without changing any value
                         (#playBtn is the outline twin: playback "go")
  #stopBtn     red     - interrupts a run in progress

Everything else stays neutral on purpose. The plain buttons are the
ones you press without thinking (Refresh, Select shown, Select none);
colour is reserved for the buttons that start something or write
something, so a colour in this UI always means "this one acts".

Object names the sheet also styles: "header" (the bold summary line at
the top of a window), "note" (muted explanatory small print) and
"cellBtn" (a button living inside a table cell, kept short so it doesn't
stretch the row).
"""

from PySide6.QtGui import QColor

ACCENT = "#3b7ddd"

# Foreground for any cell painted with one of the verdict tints. Those
# tints (app/gui/quiz_detail_window.py) are fixed light colours, while
# the text colour otherwise comes from the palette - so on a dark
# desktop theme, common on Linux and Windows, a tinted cell was drawing
# near-white text on near-white paint and the value vanished.
TINT_TEXT = QColor(28, 28, 30)


def tint_item(item, color) -> None:
    """Paint a table cell with a verdict tint, foreground included."""
    item.setBackground(color)
    item.setForeground(TINT_TEXT)

STYLE_SHEET = """
QGroupBox {
    border: 1px solid rgba(128, 128, 128, 0.35);
    border-radius: 6px;
    margin-top: 9px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #3b7ddd;
}

/* No font sizes anywhere in this sheet: the three platforms ship
   different default UI fonts (13pt on macOS, 9pt Segoe UI on Windows,
   whatever the desktop sets on Linux) and a px size that looks right on
   one clips or floats on the others. Weight and colour are safe. */
QLabel#note { color: #8a8c93; }
/* The summary line at the top of a window, as a card with an accent
   spine down its left edge. */
QLabel#header {
    padding: 8px 10px;
    border: 1px solid rgba(59, 125, 221, 0.30);
    border-left: 3px solid #3b7ddd;
    border-radius: 6px;
    background: rgba(59, 125, 221, 0.07);
}

QTableWidget, QTableView {
    border: 1px solid rgba(128, 128, 128, 0.30);
    border-radius: 6px;
    gridline-color: rgba(128, 128, 128, 0.22);
    alternate-background-color: rgba(128, 128, 128, 0.06);
}
QTableWidget::item, QTableView::item { padding: 3px 6px; }
/* The finger confusion matrix: 11 columns of one- or two-digit counts,
   sized in quiz_detail_window to fit whole. Cell padding would push it
   back out of view. */
QTableWidget#confusion::item { padding: 0px; }
/* Selected rows are deliberately left to the platform (solid highlight,
   white text): a translucent overlay meant to keep the verdict tint
   visible under the selection ended up unreadable in a dark palette, and
   the selection only ever covers the one row being clicked. */
/* The per-row tick in a table cell. Left to the platform, Fusion paints
   it palette-Base on palette-Base, so under a dark Linux or Windows
   theme the control this whole window is built around is invisible. An
   explicit box - outlined when off, filled accent when on - reads on any
   theme. No checkmark glyph: a QSS indicator image needs a bitmap on
   disk, and standalone QCheckBoxes (which draw one natively and stay
   legible) are deliberately left alone. */
QTableWidget::indicator, QTableView::indicator {
    width: 13px;
    height: 13px;
    border-radius: 3px;
    border: 1px solid rgba(128, 128, 128, 0.85);
    background: transparent;
}
QTableWidget::indicator:checked, QTableView::indicator:checked {
    background: #3b7ddd;
    border-color: #3b7ddd;
}
QHeaderView::section {
    background: rgba(59, 125, 221, 0.10);
    padding: 5px 8px;
    border: none;
    border-right: 1px solid rgba(128, 128, 128, 0.20);
    border-bottom: 1px solid rgba(128, 128, 128, 0.28);
    font-weight: 600;
}

QPushButton {
    padding: 6px 13px;
    border-radius: 6px;
    border: 1px solid rgba(128, 128, 128, 0.38);
    background: rgba(128, 128, 128, 0.09);
    font-weight: 500;
}
QPushButton:hover { background: rgba(128, 128, 128, 0.18); }
QPushButton:pressed { background: rgba(128, 128, 128, 0.28); }
QPushButton:disabled {
    color: rgba(128, 128, 128, 0.75);
    background: rgba(128, 128, 128, 0.05);
    border-color: rgba(128, 128, 128, 0.20);
}
/* Solid accents: the button starts or writes something. */
QPushButton#primaryBtn, QPushButton#syncBtn, QPushButton#dataBtn,
QPushButton#saveBtn, QPushButton#confirmBtn, QPushButton#stopBtn {
    border: none;
    font-weight: 600;
    padding: 7px 15px;
}
QPushButton#primaryBtn { background: #3b7ddd; color: #ffffff; }
QPushButton#primaryBtn:hover { background: #316cc2; }
QPushButton#primaryBtn:pressed { background: #295ba5; }
QPushButton#syncBtn { background: #2a9d8f; color: #ffffff; }
QPushButton#syncBtn:hover { background: #23867a; }
QPushButton#syncBtn:pressed { background: #1c6d64; }
QPushButton#dataBtn { background: #6b5bd2; color: #ffffff; }
QPushButton#dataBtn:hover { background: #5c4cbb; }
QPushButton#dataBtn:pressed { background: #4c3fa0; }
QPushButton#saveBtn { background: #f0b429; color: #3a2b00; }
QPushButton#saveBtn:hover { background: #d99e14; }
QPushButton#saveBtn:pressed { background: #c08c0f; }
QPushButton#confirmBtn { background: #2e9e5b; color: #ffffff; }
QPushButton#confirmBtn:hover { background: #268a4f; }
QPushButton#confirmBtn:pressed { background: #1f7541; }
QPushButton#stopBtn { background: #d05353; color: #ffffff; }
QPushButton#stopBtn:hover { background: #b84545; }
QPushButton#stopBtn:pressed { background: #9c3838; }

/* Outline twins: same meaning, lighter weight on the page. Tinted text
   on a transparent ground, so they read on a light or dark palette. */
QPushButton#cellBtn, QPushButton#exportBtn, QPushButton#playBtn {
    background: transparent;
    font-weight: 600;
}
QPushButton#cellBtn { padding: 2px 9px; border-color: #2a9d8f; color: #2a9d8f; }
QPushButton#cellBtn:hover { background: rgba(42, 157, 143, 0.16); }
QPushButton#cellBtn:pressed { background: rgba(42, 157, 143, 0.28); }
QPushButton#exportBtn { border-color: #6b5bd2; color: #6b5bd2; }
QPushButton#exportBtn:hover { background: rgba(107, 91, 210, 0.16); }
QPushButton#exportBtn:pressed { background: rgba(107, 91, 210, 0.28); }
QPushButton#playBtn { border-color: #2e9e5b; color: #2e9e5b; }
QPushButton#playBtn:hover { background: rgba(46, 158, 91, 0.16); }
QPushButton#playBtn:pressed { background: rgba(46, 158, 91, 0.28); }

QPushButton#primaryBtn:disabled, QPushButton#syncBtn:disabled,
QPushButton#dataBtn:disabled, QPushButton#saveBtn:disabled,
QPushButton#confirmBtn:disabled, QPushButton#stopBtn:disabled {
    background: rgba(128, 128, 128, 0.28);
    color: #8a8c93;
}
QPushButton#cellBtn:disabled, QPushButton#exportBtn:disabled,
QPushButton#playBtn:disabled {
    background: transparent;
    border-color: rgba(128, 128, 128, 0.35);
    color: #8a8c93;
}

QLineEdit {
    padding: 5px 9px;
    border-radius: 6px;
    border: 1px solid rgba(128, 128, 128, 0.38);
}
QLineEdit:focus { border-color: #3b7ddd; }

QProgressBar {
    border: 1px solid rgba(128, 128, 128, 0.30);
    border-radius: 6px;
    text-align: center;
    height: 16px;
}
QProgressBar::chunk { background: #3b7ddd; border-radius: 5px; }

QCheckBox { spacing: 6px; }
QSlider::groove:horizontal {
    height: 5px;
    border-radius: 3px;
    background: rgba(128, 128, 128, 0.30);
}
QSlider::sub-page:horizontal { background: #3b7ddd; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 13px;
    margin: -5px 0;
    border-radius: 7px;
    background: #ffffff;
    border: 1px solid rgba(128, 128, 128, 0.55);
}
QSlider::handle:horizontal:hover { border-color: #3b7ddd; }
"""
