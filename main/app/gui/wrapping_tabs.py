"""A tab widget whose tab strip wraps onto as many rows as it needs.

The analysis windows carry a dozen tabs with names like "Condition ×
Difficulty" and "Condition A Strategy", which want about 1270px of strip.
QTabWidget cannot wrap - it is a single row by construction - so below
that width it hides the overflow behind a scroll arrow (or elides the
labels, depending on the platform style), and half the analysis becomes
invisible on a laptop screen.

This keeps every label readable at any width by laying the tab buttons
out in a flow that wraps, and drives a QStackedWidget with them. It
implements the small slice of the QTabWidget API the windows and their
tests use - addTab, count, clear, tabText, widget, currentIndex,
setCurrentIndex, currentChanged - so it drops in where QTabWidget was.

Tab shapes are drawn by the real style through QStyleOptionTab, so they
still look like this platform's tabs rather than like buttons; only the
label placement is this widget's own (see _TabButton.paintEvent).
"""

from typing import List, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton,
    QLayout,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QVBoxLayout,
    QWidget,
)


# Padding around a tab label. Owned here rather than taken from the
# style, because paintEvent draws the label itself - see _TabButton.
_H_PADDING = 14
_V_PADDING = 7


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to a new row when it runs out of
    width (the layout Qt's own Flow Layout example describes). Reports
    height for a given width so the container can grow a row instead of
    clipping."""

    def __init__(self, parent=None, spacing: int = 2):
        super().__init__(parent)
        self._items: List = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802 (Qt override)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        """Place items row by row; return the total height used."""
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        spacing = self.spacing()
        row_height = 0

        for item in self._items:
            hint = item.sizeHint()
            if row_height and x + hint.width() - 1 > right:
                x = rect.x() + margins.left()
                y += row_height + spacing
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + spacing
            row_height = max(row_height, hint.height())

        return y + row_height - rect.y() + margins.bottom()


class _TabButton(QAbstractButton):
    """One tab, painted by the current style so it matches native tabs.

    Position matters to the style (a first/middle/last tab is drawn with
    different rounding and separators), and in a wrapping strip every row
    is visually its own run of tabs, so the owner sets `position` after
    each relayout rather than it being fixed at construction.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.position = QStyleOptionTab.TabPosition.OnlyOneTab

    def _option(self) -> QStyleOptionTab:
        option = QStyleOptionTab()
        option.initFrom(self)
        option.text = self.text()
        option.shape = QTabBar.Shape.RoundedNorth
        option.position = self.position
        option.state |= QStyle.StateFlag.State_Active
        if self.isChecked():
            option.state |= QStyle.StateFlag.State_Selected
        else:
            option.state &= ~QStyle.StateFlag.State_Selected
        return option

    def sizeHint(self) -> QSize:  # noqa: N802
        content = self.fontMetrics().size(Qt.TextFlag.TextShowMnemonic, self.text())
        return QSize(content.width() + 2 * _H_PADDING,
                     content.height() + 2 * _V_PADDING)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """Shape from the style, text drawn here.

        Drawing the whole tab with CE_TabBarTab would let the style pick
        the label rect, and several styles - the macOS one included - inset
        it for the rounded ends and then clip the text to what is left,
        which is how a wrapped strip ends up showing "Dverview". Painting
        the shape and the label separately keeps the padding this widget's
        own decision, which is what sizeHint() was sized against."""
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, self._option())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect().adjusted(_H_PADDING, 0, -_H_PADDING, 0),
                         int(Qt.AlignmentFlag.AlignCenter), self.text())


class WrappingTabWidget(QWidget):
    """QTabWidget-alike whose tab strip wraps instead of overflowing."""

    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: List[_TabButton] = []
        self._strip = QWidget(self)
        self._strip_layout = FlowLayout(self._strip)
        # A height-for-width layout only actually drives the widget's
        # height if the size policy says so - without this the strip keeps
        # a one-row height and the wrapped rows are simply clipped.
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self._strip.setSizePolicy(policy)
        self._stack = QStackedWidget(self)
        self._stack.currentChanged.connect(self.currentChanged)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._strip)
        layout.addWidget(self._stack, 1)

    # -- the QTabWidget slice the analysis windows and tests use --------

    def addTab(self, widget: QWidget, label: str) -> int:  # noqa: N802
        button = _TabButton(label, self._strip)
        button.clicked.connect(lambda _checked, w=widget: self._stack.setCurrentWidget(w))
        self._strip_layout.addWidget(button)
        self._buttons.append(button)
        index = self._stack.addWidget(widget)
        if index == 0:
            button.setChecked(True)
        self._update_positions()
        return index

    def count(self) -> int:
        return self._stack.count()

    def clear(self) -> None:
        for button in self._buttons:
            self._strip_layout.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._buttons.clear()
        while self._stack.count():
            page = self._stack.widget(0)
            self._stack.removeWidget(page)
            page.setParent(None)
            page.deleteLater()

    def tabText(self, index: int) -> str:  # noqa: N802
        return self._buttons[index].text() if 0 <= index < len(self._buttons) else ""

    def widget(self, index: int) -> Optional[QWidget]:
        return self._stack.widget(index)

    def currentIndex(self) -> int:  # noqa: N802
        return self._stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)
            self._stack.setCurrentIndex(index)

    def setTabToolTip(self, index: int, tip: str) -> None:  # noqa: N802
        if 0 <= index < len(self._buttons):
            self._buttons[index].setToolTip(tip)

    # ------------------------------------------------------------------

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_positions()

    def _update_positions(self) -> None:
        """Tell each button where it sits in its own row, so the style
        rounds the ends of every row rather than only the whole strip."""
        if not self._buttons:
            return
        rows: List[List[_TabButton]] = []
        last_y = None
        for button in self._buttons:
            y = button.geometry().y()
            if last_y is None or y != last_y:
                rows.append([])
                last_y = y
            rows[-1].append(button)
        P = QStyleOptionTab.TabPosition
        for row in rows:
            for i, button in enumerate(row):
                if len(row) == 1:
                    button.position = P.OnlyOneTab
                elif i == 0:
                    button.position = P.Beginning
                elif i == len(row) - 1:
                    button.position = P.End
                else:
                    button.position = P.Middle
            for button in row:
                button.update()
