"""Run a sequence of named steps behind a modal progress dialog.

Both analysis windows have two operations slow enough to look like a
freeze - building every tab, and writing every figure and table to disk -
and both are naturally a list of named units of work. This runs such a
list, showing which unit is in flight.

The point of taking the work as a list rather than as a hardcoded count is
that the caller's step list IS the work: a window that builds its tabs by
walking the same list the dialog was sized from cannot drift out of sync
with its own progress bar when someone adds an analysis later. Adding a
step is a one-line change in one place, and both the total and the labels
follow from it.
"""

from typing import Callable, Iterable, List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog

Step = Tuple[str, Callable[[], None]]


def run_with_progress(parent, title: str, steps: Iterable[Step]) -> bool:
    """Run every (label, callable) step in order, returning True if all of
    them ran and False if the user cancelled.

    Cancellation is checked between steps, never inside one, so a step
    always either runs completely or not at all - the caller decides what a
    partial result means and cleans up accordingly. A step that raises is
    the caller's business too: nothing is caught here.
    """
    steps: List[Step] = list(steps)
    total = len(steps)
    dialog = QProgressDialog("Starting...", "Cancel", 0, total, parent)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)  # these are always seconds long
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    try:
        for i, (label, run) in enumerate(steps):
            if dialog.wasCanceled():
                return False
            # setValue() processes events while the dialog is modal, so
            # this is what repaints the new label and makes Cancel
            # reachable. Label first, so it names the step about to run.
            dialog.setLabelText(f"{i + 1} / {total}   {label}")
            dialog.setValue(i)
            run()
        dialog.setValue(total)
        return True
    finally:
        dialog.close()
