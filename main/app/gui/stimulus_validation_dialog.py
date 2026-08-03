"""Report/plot window for app.stimulus_validation.validate_level_pools()
- opened by the Experiment Sequence Generator right after generating
fresh level pools, and by the Sequence Metrics window's "Validate
Stimulus Set" button for saved sequences.

_BoxPlotWidget draws a simple min/Q1-median-Q3/max box-and-whisker row per
difficulty level with plain QPainter calls (no charting dependency), one
widget per scalar component of D = (C_m, C_s, C_c), inside a scroll area -
so the monotonic (or deliberately non-monotonic) level separation the text
report describes can be seen directly rather than only read as numbers.

"Export report" saves the dialog's contents to
data/sequence_validation/<batch id>/ - report.txt (conclusion + the full
text report shown in the lower pane) and boxplots.png (the complete
box-plot stack rendered in one image). The batch id is passed in by the
caller: the Sequence Generator passes its generation seed, the Sequence
Metrics window passes the batch name(s) of the validated sequences.
"""

from typing import Dict, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..music_recording import sanitize_song_name
from ..stimulus_validation import (
    LEVEL_DISPLAY,
    LEVELS,
    VALIDATION_DATA_DIR,
    GroupSummary,
    ValidationReport,
    format_report,
)

# Chosen for contrast against the fixed dark background this widget always
# paints (see paintEvent) - independent of whatever light/dark theme the
# rest of the app is running under, so the text and lines here are never
# accidentally near-invisible (e.g. light gray text on a light system theme).
_BACKGROUND_COLOR = QColor(32, 33, 38)
_TEXT_COLOR = QColor(235, 235, 235)
_LEVEL_COLOR = {
    "alpha": QColor(140, 190, 255),
    "beta": QColor(255, 200, 110),
    "gamma": QColor(255, 140, 140),
}

_DIRECTION_NOTE = {
    "increase": "expected to rise α (alpha) → γ (gamma)",
    "decrease": "expected to fall α (alpha) → γ (gamma)",
    "matching": "matching constraint - no ordering required",
    "diagnostic": "descriptive diagnostic - reported only",
    "cross": "cross-region metric - validated against level limits",
}


class _BoxPlotWidget(QWidget):
    def __init__(self, title: str, groups: Dict[str, GroupSummary], parent=None):
        super().__init__(parent)
        self.title = title
        self.groups = groups
        self.setMinimumHeight(150)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), _BACKGROUND_COLOR)

        painter.setPen(QPen(_TEXT_COLOR))
        title_font = QFont(painter.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(8, 16, self.title)

        levels = [level for level in LEVELS if level in self.groups]
        if not levels:
            painter.drawText(8, 40, "No data")
            return

        body_font = QFont(painter.font())
        body_font.setBold(False)
        painter.setFont(body_font)

        margin_left, margin_right, margin_top, margin_bottom = 100, 70, 28, 8
        w, h = self.width(), self.height()
        plot_w = max(w - margin_left - margin_right, 10)
        row_h = (h - margin_top - margin_bottom) / len(levels)

        lo = min(self.groups[level].minimum for level in levels)
        hi = max(self.groups[level].maximum for level in levels)
        span = (hi - lo) or 1.0

        def x_of(value: float) -> float:
            return margin_left + (value - lo) / span * plot_w

        for i, level in enumerate(levels):
            g = self.groups[level]
            y = margin_top + i * row_h + row_h / 2
            color = _LEVEL_COLOR.get(level, QColor(200, 200, 200))

            painter.setPen(QPen(_TEXT_COLOR))
            painter.drawText(6, int(y + 4), LEVEL_DISPLAY[level])

            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(int(x_of(g.minimum)), int(y), int(x_of(g.maximum)), int(y))

            box_h = row_h * 0.42
            box_color = QColor(color)
            box_color.setAlpha(90)
            painter.setBrush(box_color)
            painter.setPen(pen)
            box_left, box_right = x_of(g.q1), x_of(g.q3)
            painter.drawRect(QRectF(box_left, y - box_h / 2, max(box_right - box_left, 1.0), box_h))

            pen.setWidth(3)
            painter.setPen(pen)
            median_x = x_of(g.median)
            painter.drawLine(int(median_x), int(y - box_h / 2), int(median_x), int(y + box_h / 2))

            painter.setPen(QPen(_TEXT_COLOR))
            painter.drawText(int(margin_left + plot_w + 8), int(y + 4), f"med={g.median:.2f}")


class StimulusValidationDialog(QDialog):
    def __init__(
        self,
        report: ValidationReport,
        parent: Optional[QWidget] = None,
        batch_id: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Difficulty Validation (D = C_m, C_s, C_c)")
        self.resize(1000, 860)
        self._report = report
        self._batch_id = batch_id

        conclusion_label = QLabel(f"Conclusion: {report.conclusion.upper()}")
        conclusion_font = conclusion_label.font()
        conclusion_font.setPointSize(conclusion_font.pointSize() + 3)
        conclusion_font.setBold(True)
        conclusion_label.setFont(conclusion_font)

        # One box plot per scalar component, grouped C_m / C_s / C_c, in a
        # scroll area - the component list comes from the report itself so
        # this stays in lockstep with app.sequence_generator.COMPONENTS.
        plots_host = QWidget()
        plots_layout = QVBoxLayout(plots_host)
        current_group = None
        for c in report.components:
            if c.group != current_group:
                current_group = c.group
                header = QLabel(c.group)
                header_font = header.font()
                header_font.setBold(True)
                header.setFont(header_font)
                plots_layout.addWidget(header)
            status = ""
            if c.monotonic is not None:
                status = " - MONOTONIC" if c.monotonic else " - NOT MONOTONIC"
            title = f"{c.label} ({_DIRECTION_NOTE[c.direction]}){status}"
            plots_layout.addWidget(_BoxPlotWidget(title, c.groups))
        plots_layout.addStretch(1)
        self._plots_host = plots_host  # grabbed whole for the exported image

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(plots_host)

        report_text = QTextEdit()
        report_text.setReadOnly(True)
        report_text.setFont(QFont("Menlo, Consolas, monospace"))
        report_text.setPlainText(format_report(report))

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(scroll)
        splitter.addWidget(report_text)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        export_btn = QPushButton("Export report (text + plots)")
        export_btn.setToolTip(
            "Save report.txt and boxplots.png under data/sequence_validation/<batch id>/."
        )
        export_btn.clicked.connect(self._export)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(export_btn)
        buttons_row.addWidget(close_btn, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(conclusion_label)
        layout.addWidget(splitter, 1)
        layout.addLayout(buttons_row)

    def _export(self) -> None:
        """report.txt + boxplots.png into data/sequence_validation/<batch id>/.
        The image is the whole box-plot stack grabbed in one piece (the
        plots host's full height, not just the visible scroll viewport)."""
        folder = VALIDATION_DATA_DIR / sanitize_song_name(self._batch_id or "unnamed")
        txt_path = folder / "report.txt"
        png_path = folder / "boxplots.png"

        if txt_path.exists() or png_path.exists():
            reply = QMessageBox.question(
                self,
                "Overwrite existing export?",
                f"'{folder.name}' already has an exported report under data/sequence_validation/. Overwrite it?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            folder.mkdir(parents=True, exist_ok=True)
            txt_path.write_text(
                f"Conclusion: {self._report.conclusion.upper()}\n\n{format_report(self._report)}",
                encoding="utf-8",
            )
            if not self._plots_host.grab().save(str(png_path), "PNG"):
                raise RuntimeError(f"Couldn't write {png_path.name}")
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't export report", str(exc))
            return

        QMessageBox.information(
            self,
            "Report exported",
            f"Saved report.txt and boxplots.png to data/sequence_validation/{folder.name}/.",
        )
