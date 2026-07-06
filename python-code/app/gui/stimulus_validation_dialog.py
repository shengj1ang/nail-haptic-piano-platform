"""Report/plot window for app.stimulus_validation.validate_stimulus_set()
- see app/gui/sequence_metrics_window.py's "Validate Stimulus Set" button.

_BoxPlotWidget draws a simple min/Q1-median-Q3/max box-and-whisker row per
difficulty level with plain QPainter calls (no charting dependency), so
the level separation the text report describes can be seen directly
rather than only read as numbers.
"""

from typing import Dict, Optional

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget

from ..stimulus_validation import LEVEL_DISPLAY, LEVELS, GroupSummary, ValidationReport, format_report

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
    def __init__(self, report: ValidationReport, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Stimulus Set Validation")
        self.resize(950, 820)

        conclusion_label = QLabel(f"Conclusion: {report.conclusion.upper()}")
        conclusion_font = conclusion_label.font()
        conclusion_font.setPointSize(conclusion_font.pointSize() + 3)
        conclusion_font.setBold(True)
        conclusion_label.setFont(conclusion_font)

        cost_plot = _BoxPlotWidget("Motor cost by level", report.cost.groups)
        entropy_plot = _BoxPlotWidget("H_norm by level", report.entropy.groups)

        report_text = QTextEdit()
        report_text.setReadOnly(True)
        report_text.setFont(QFont("Menlo, Consolas, monospace"))
        report_text.setPlainText(format_report(report))

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(conclusion_label)
        layout.addWidget(cost_plot)
        layout.addWidget(entropy_plot)
        layout.addWidget(report_text, 1)
        layout.addWidget(close_btn)
