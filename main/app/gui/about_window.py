"""About window - launcher section 12.

A single "About this platform" panel, laid out like the classic pre-Big Sur
"About This Mac" window: the product image and name at the top, then a row of
tabs whose content changes below (Overview, Abstract, Acknowledgements,
Resources). It is pure reference - it claims no hardware and writes
nothing, so the launcher keeps it in CONCURRENT_TOOLS (stays open alongside a
tool, and re-clicking the button raises it rather than opening a second copy).

The report title, abstract and acknowledgements are copied verbatim from the
final report: the abstract and the
acknowledgement paragraph are the exact text of the submitted report, only
with LaTeX escapes (\\,, \\%, en-dashes) rendered as their plain characters.
Keep them in sync if the report is revised.
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

ICON_IMAGE = (
    Path(__file__).resolve().parent.parent / "assets" / "image" / "icon.png"
)

# --- facts from the final report -----------------------------------------

DISSERTATION_TITLE = (
    "Nail-Mounted Haptic Cues for Piano Training and Tele-training"
)
AUTHOR = "Sheng Jiang"
COURSE = "MSc Individual Project"
INSTITUTION = "Department of Bioengineering, Imperial College London"
SUPERVISOR = "Dr. Alexis W.M. Devillard"
CO_SUPERVISOR = "Dr. Etienne Burdet"

# Project source and the report PDF included at the repository root.
CODE_REPO_URL = "https://github.com/shengj1ang/nail-haptic-piano-platform"
REPORT_PDF = Path(__file__).resolve().parents[3] / "shengjiang_final_report.pdf"
REPORT_PDF_URL = REPORT_PDF.as_uri()

# Verbatim from the final report abstract, with LaTeX
# escapes rendered: "247\,ms" -> "247 ms", "\%" -> "%", "key--finger" ->
# "key-finger".
ABSTRACT = (
    "Skilled hand movement depends on choosing the correct finger as well as "
    "the correct target, yet guidance technology poorly supports that choice. "
    "Screens and sounds show which key to press and when, but neither directly "
    "specifies the acting digit. MIDI likewise records the key and timing, not "
    "the finger used, so compliance is not routinely measurable. This work "
    "addresses both gaps. A platform delivered vibration to the nail of the "
    "target finger while leaving the fingertip free to sense the key, with "
    "per-key illumination and camera-based finger identification aligned to "
    "MIDI. Bench tests selected the actuator, mounting, and drive settings "
    "before recruitment. A constraint-driven generator then produced sequences "
    "of measured, ordered difficulty matched across conditions. Twenty "
    "participants performed the task with key-only, visual-finger, and "
    "vibrotactile-finger guidance. Relative to matched visual cues, "
    "vibrotactile cues reduced reaction time by 247 ms and improved finger "
    "accuracy by 3.65 percentage points. Benefits varied across digits, and "
    "wrong-hand errors fell from 28.8% to 2.7% of substitutions, while key "
    "accuracy remained near ceiling in both conditions. The digit and error "
    "patterns suggest that the cue primarily aided selection of which finger "
    "should act rather than its movement. The common key–finger "
    "representation also drove a working remote-teaching deployment supporting "
    "live and recorded guidance. The evidence establishes an immediate "
    "cue-guided performance advantage within one session; it does not "
    "establish retention after cue withdrawal or behavioural benefit during "
    "remote use."
)

# Verbatim from the final report acknowledgements.
ACKNOWLEDGEMENTS = (
    "I would like to thank Dr Alexis W.M. Devillard and Dr Etienne Burdet for "
    "their guidance throughout this project. Their advice shaped both the "
    "technical direction of the work and the development of the experimental "
    "platform, and their feedback during implementation and during the "
    "preparation of this report improved both."
)

# Dark theme, matching the launcher and the other sub-windows.
STYLE_SHEET = """
QWidget#aboutRoot { background: #1e1f24; }
QLabel { color: #d8d9e0; }
QLabel#appName { color: #f2f2f5; font-size: 22px; font-weight: 700; }
QLabel#appTagline { color: #c8c9d2; font-size: 13px; }
QLabel#appMeta { color: #9a9ba5; font-size: 12px; }
QLabel#pageTitle { color: #7fb2ff; font-size: 15px; font-weight: 700; }
QLabel#body { color: #d8d9e0; font-size: 13px; }
QLabel#body a { color: #7fb2ff; }
QTabWidget::pane {
    border: 1px solid #35363e;
    border-radius: 8px;
    top: -1px;
    background: #26272e;
}
QTabBar::tab {
    background: #2a2b33;
    color: #c8c9d2;
    border: 1px solid #35363e;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #26272e; color: #7fb2ff; font-weight: 700; }
QTabBar::tab:hover { color: #eceef2; }
QScrollArea { border: none; background: #26272e; }
QScrollArea > QWidget > QWidget { background: #26272e; }
"""


class AboutWindow(QWidget):
    """Reference-only About panel. Accepts the shared Config for a uniform
    constructor signature (``window_cls(cfg)`` in the launcher) but does not
    use it - nothing here reads or writes any configuration."""

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = cfg
        self.setObjectName("aboutRoot")
        self.setStyleSheet(STYLE_SHEET)
        self.setWindowTitle("About Multi-Modal Platform")
        if ICON_IMAGE.exists():
            self.setWindowIcon(QIcon(str(ICON_IMAGE)))

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        root.addLayout(self._build_header())

        tabs = QTabWidget()
        tabs.addTab(self._overview_page(), "Overview")
        tabs.addTab(self._text_page(
            "Abstract",
            f"<p>{ABSTRACT}</p>"
            f"<p style='color:#9a9ba5'>— From the dissertation abstract, "
            f"<i>{DISSERTATION_TITLE}</i>.</p>",
        ), "Abstract")
        tabs.addTab(self._text_page(
            "Acknowledgements",
            f"<p>{ACKNOWLEDGEMENTS}</p>",
        ), "Acknowledgements")
        tabs.addTab(self._repositories_page(), "Resources")
        root.addWidget(tabs, 1)

        self.resize(560, 560)
        self.setMinimumSize(480, 480)

    # ------------------------------------------------------------------
    # Header: product image + name + tagline (like "About This Mac")
    # ------------------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(18)

        icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        pix = QPixmap(str(ICON_IMAGE))
        if not pix.isNull():
            icon.setPixmap(pix.scaled(
                96, 96,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(4)

        name = QLabel("Multi-Modal Platform")
        name.setObjectName("appName")
        text.addWidget(name)

        tagline = QLabel(DISSERTATION_TITLE)
        tagline.setObjectName("appTagline")
        tagline.setWordWrap(True)
        text.addWidget(tagline)

        meta = QLabel(f"{COURSE} · {INSTITUTION}")
        meta.setObjectName("appMeta")
        meta.setWordWrap(True)
        text.addWidget(meta)

        text.addStretch(1)
        header.addLayout(text, 1)
        return header

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _overview_page(self) -> QWidget:
        html = (
            "<p>A multi-modal learning-assistant platform built on the ideas "
            "in the dissertation. It delivers vibrotactile cues to the nail of "
            "the target finger, per-key keyboard illumination, and camera-based "
            "finger identification aligned to MIDI, and it bundles the tools "
            "used to design, run and analyse the studies around them.</p>"
            "<table cellspacing='0' cellpadding='4'>"
            f"<tr><td style='color:#9a9ba5'>Author</td>"
            f"<td><b>{AUTHOR}</b></td></tr>"
            f"<tr><td style='color:#9a9ba5'>Supervisor</td>"
            f"<td>{SUPERVISOR}</td></tr>"
            f"<tr><td style='color:#9a9ba5'>Co-Supervisor</td>"
            f"<td>{CO_SUPERVISOR}</td></tr>"
            f"<tr><td style='color:#9a9ba5'>Institution</td>"
            f"<td>{INSTITUTION}</td></tr>"
            f"<tr><td style='color:#9a9ba5'>Programme</td>"
            f"<td>{COURSE}</td></tr>"
            "</table>"
        )
        return self._text_page("Overview", html)

    def _repositories_page(self) -> QWidget:
        html = (
            "<p>Project source repository:</p>"
            "<p><b>Source code</b><br>"
            f"<a href='{CODE_REPO_URL}'>{CODE_REPO_URL}</a><br>"
            "<span style='color:#9a9ba5'>The platform itself — this launcher "
            "and every tool it opens, the firmware and the analysis "
            "pipeline.</span></p>"
            "<p><span style='color:#9a9ba5'>The final report is included in "
            "this repository as a PDF.</span></p>"
            "<p><b>Final report</b><br>"
            f"<a href='{REPORT_PDF_URL}'>shengjiang_final_report.pdf</a></p>"
        )
        return self._text_page("Resources", html)

    def _text_page(self, title: str, body_html: str) -> QWidget:
        """A page with a coloured heading and a scrollable rich-text body.
        Links in the body open in the system browser."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        lay.addWidget(heading)

        body = QLabel(body_html)
        body.setObjectName("body")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(scroll, 1)
        return page


def main() -> None:
    app = QApplication(sys.argv)
    window = AboutWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
