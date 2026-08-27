"""Run a sequence of named steps behind a modal progress dialog.

The analysis windows have several operations slow enough to look like a
freeze - building every tab, writing every figure to disk, fitting a model
a few hundred times for a bootstrap - and each is naturally a list of
named units of work. This runs such a list, showing which unit is in
flight.

The point of taking the work as a list rather than as a hardcoded count is
that the caller's step list IS the work: a window that builds its tabs by
walking the same list the dialog was sized from cannot drift out of sync
with its own progress bar when someone adds an analysis later. Adding a
step is a one-line change in one place, and both the total and the labels
follow from it.

Steps that take minutes
-----------------------
A step runs synchronously on the GUI thread, so while it runs nothing
repaints and nothing responds - and a bar that has not moved for eight
minutes is indistinguishable from a hung program, whatever style it is
drawn in. An animated "busy" bar does not fix that, because the animation
also needs the event loop.

The fix is for the long step itself to check in. A step may declare that
by taking one argument, and it is then handed a reporter:

    def bootstrap(report):
        for i in range(n):
            report(f"resample {i + 1}/{n}", (i + 1) / n)   # advances
            ...

    def sweep(report):
        for name in models:
            report(f"fitting {name}")                       # bounces

Calling it repaints the dialog, keeps Cancel clickable, and moves the bar
inside that step's own segment: to the given fraction when the caller
knows one, and back and forth across the segment when it does not, so
"still working, no idea how long" reads differently from "three quarters
done" instead of both reading as frozen.

Steps that take one argument get the reporter; steps that take none are
left exactly as they were, so nothing that already worked has to change.

Cancellation
------------
Between steps, cancelling returns False before the next one starts, so a
step always either runs completely or not at all and the caller's cleanup
sees a clean boundary. A step that reports progress can also be cancelled
part way: the reporter raises StepCancelled, which unwinds that step
without letting it return a half-finished result, and run_with_progress
turns it into the same False. Either way the caller learns the work did
not finish and never receives a partial one.
"""

import time
from typing import Callable, Iterable, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProgressDialog

Step = Tuple[str, Callable]

# Sub-divisions per step, so a step reporting a fraction moves the bar
# smoothly instead of jumping between whole steps.
_TICKS_PER_STEP = 100

# How far into a step's own segment the bouncing indicator travels, as a
# fraction. Kept short of the far end so a bounce is never mistaken for
# the step having finished.
_BOUNCE_SPAN = 0.85

# Seconds for one there-and-back sweep. Driven by the CLOCK rather than
# by how many times the step happened to report: a step that calls twice
# a second and one that calls fifty times a second should both look like
# the same steady "working" motion, and tying the sweep to call count
# makes the first crawl and the second flicker.
_BOUNCE_PERIOD_S = 1.6

# Minimum seconds between repaints. Reporting per resample is the right
# granularity for the caller but repainting 300 times a second is wasted
# work, so calls in between are dropped - except the first and any that
# change the fraction materially.
_REPAINT_INTERVAL_S = 0.08


class StepCancelled(Exception):
    """Raised inside a step when the user cancels, to unwind it without
    letting it return a partial result."""


class StepProgress:
    """What a long step calls to say it is still working.

    `report(message)` bounces the bar inside this step's segment;
    `report(message, fraction)` advances it to that fraction. Both repaint
    the dialog and raise StepCancelled if the user has pressed Cancel.
    """

    def __init__(self, dialog: QProgressDialog, index: int, total: int, label: str):
        self._dialog = dialog
        self._index = index
        self._total = total
        self._label = label
        self._base = index * _TICKS_PER_STEP
        self._last_paint = 0.0
        self._started = time.monotonic()
        self._calls = 0

    def __call__(self, message: str = "", fraction: Optional[float] = None) -> None:
        if self._dialog.wasCanceled():
            raise StepCancelled(self._label)
        now = time.monotonic()
        self._calls += 1
        # Repaint at a fixed rate rather than on every call. A bootstrap
        # reports three hundred times and a fold loop twenty; repainting
        # on each would be wasted work in the first case, but the first
        # call must always show so the label never lags a step behind.
        if self._calls > 1 and now - self._last_paint < _REPAINT_INTERVAL_S:
            return
        self._last_paint = now
        if fraction is None:
            # No idea how far along: sweep back and forth so the bar shows
            # activity without claiming progress it cannot measure.
            span = _TICKS_PER_STEP * _BOUNCE_SPAN
            phase = ((now - self._started) % _BOUNCE_PERIOD_S) / _BOUNCE_PERIOD_S
            offset = int(span * (2 * phase if phase < 0.5 else 2 * (1 - phase)))
        else:
            offset = int(max(0.0, min(1.0, float(fraction))) * _TICKS_PER_STEP)
        detail = message
        if fraction is not None:
            remaining = self._estimate_remaining(fraction, now)
            if remaining:
                detail = f"{message}   ·   {remaining}" if message else remaining
        self._dialog.setLabelText(
            f"{self._index + 1} / {self._total}   {self._label}"
            + (f"\n{detail}" if detail else ""))
        self._dialog.setValue(self._base + offset)
        QApplication.processEvents()
        if self._dialog.wasCanceled():
            raise StepCancelled(self._label)

    def _estimate_remaining(self, fraction: float, now: float) -> str:
        """A rough time-to-go, from how long the step has taken so far.

        Only shown once enough of the step has run for the estimate to
        mean anything - an extrapolation from the first two percent is
        worse than no number at all."""
        if fraction < 0.02:
            return ""
        elapsed = now - self._started
        remaining = elapsed / fraction - elapsed
        if remaining < 45:
            return f"about {max(int(remaining), 1)}s left"
        return f"about {int(round(remaining / 60))} min left"


def _wants_reporter(run: Callable) -> bool:
    """Whether this step asked for a progress reporter, by taking one
    argument. Anything whose signature cannot be read is treated as a
    plain zero-argument step, which is what every existing caller is."""
    import inspect

    try:
        signature = inspect.signature(run)
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in signature.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        and p.default is p.empty
    ]
    return len(positional) == 1


def run_with_progress(parent, title: str, steps: Iterable[Step]) -> bool:
    """Run every (label, callable) step in order, returning True if all of
    them ran and False if the user cancelled.

    A step callable takes either no arguments, or one - a StepProgress it
    should call periodically if it may run for more than a second or two.
    A step that raises anything other than StepCancelled is the caller's
    business: nothing else is caught here.
    """
    steps: List[Step] = list(steps)
    total = len(steps)
    dialog = QProgressDialog("Starting...", "Cancel", 0, total * _TICKS_PER_STEP, parent)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)  # these are always seconds long
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    # Wide enough for a two-line label, so the detail line does not resize
    # the dialog on every repaint.
    dialog.setMinimumWidth(460)
    try:
        for i, (label, run) in enumerate(steps):
            if dialog.wasCanceled():
                return False
            # setValue() processes events while the dialog is modal, so
            # this is what repaints the new label and makes Cancel
            # reachable. Label first, so it names the step about to run.
            dialog.setLabelText(f"{i + 1} / {total}   {label}")
            dialog.setValue(i * _TICKS_PER_STEP)
            QApplication.processEvents()
            try:
                if _wants_reporter(run):
                    run(StepProgress(dialog, i, total, label))
                else:
                    run()
            except StepCancelled:
                return False
        dialog.setValue(total * _TICKS_PER_STEP)
        return True
    finally:
        dialog.close()
