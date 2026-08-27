"""Tests for app.gui.progress_task and the tab lists that feed it.

The progress dialog exists so the analysis windows do not look frozen for
several seconds, but the reason it is driven by a list of steps rather
than a hardcoded count is maintenance: whoever adds the next analysis
should get a correct progress bar without touching the dialog. These
tests pin both halves of that - the runner's behaviour, and the fact that
each window's tabs come from the one list the dialog is sized from.

Run from main/:  python test-script/test_progress_task.py
"""

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from PySide6.QtWidgets import QApplication, QProgressDialog  # noqa: E402

from app.gui import progress_task  # noqa: E402
from app.gui.progress_task import run_with_progress  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class TestRunWithProgress(unittest.TestCase):

    def test_runs_every_step_in_order(self):
        ran = []
        ok = run_with_progress(None, "t", [
            (f"step {i}", lambda i=i: ran.append(i)) for i in range(5)
        ])
        self.assertTrue(ok)
        self.assertEqual(ran, [0, 1, 2, 3, 4])

    def test_empty_step_list_is_not_an_error(self):
        self.assertTrue(run_with_progress(None, "t", []))

    def test_cancel_stops_between_steps_and_reports_it(self):
        """Cancelling must not abandon a step half-done: the check happens
        between steps, so every step either ran fully or not at all."""
        ran = []
        real = QProgressDialog.wasCanceled
        QProgressDialog.wasCanceled = lambda self: len(ran) >= 2
        try:
            ok = run_with_progress(None, "t", [
                (f"step {i}", lambda i=i: ran.append(i)) for i in range(5)
            ])
        finally:
            QProgressDialog.wasCanceled = real
        self.assertFalse(ok)
        self.assertEqual(ran, [0, 1])

    def test_labels_name_the_step_about_to_run(self):
        """The label has to be set before the work, or it always names the
        step that just finished."""
        seen = []
        real_label = QProgressDialog.setLabelText

        def spy(self, text):
            real_label(self, text)
            seen.append(text)
        QProgressDialog.setLabelText = spy
        try:
            run_with_progress(None, "t", [("alpha", lambda: seen.append("RAN alpha")),
                                          ("beta", lambda: seen.append("RAN beta"))])
        finally:
            QProgressDialog.setLabelText = real_label
        # Each label appears before its own step runs.
        self.assertLess(seen.index("1 / 2   alpha"), seen.index("RAN alpha"))
        self.assertLess(seen.index("2 / 2   beta"), seen.index("RAN beta"))
        self.assertLess(seen.index("RAN alpha"), seen.index("2 / 2   beta"))

    def test_a_raising_step_is_not_swallowed(self):
        def boom():
            raise ValueError("step failed")
        with self.assertRaises(ValueError):
            run_with_progress(None, "t", [("bad", boom)])


class TestTabListsDriveTheProgressBar(unittest.TestCase):
    """Both windows must build their tabs from _tab_builders(), because
    that list is what sizes the dialog. A tab added anywhere else would
    show a bar that finishes early and would escape the export-coverage
    contracts in test_group_export.py / test_participant_export.py."""

    def test_group_analyze_walks_the_builder_list(self):
        import test_group_export as tge
        w = tge.build_window()
        titles = [w.tabs.tabText(i) for i in range(w.tabs.count())]
        self.assertEqual(titles, [t for t, _ in w._tab_builders()])

    def test_participant_analyze_walks_the_builder_list(self):
        import test_participant_export as tpe
        w = tpe.build_window()
        titles = [w.tabs.tabText(i) for i in range(w.tabs.count())]
        self.assertEqual(titles, [t for t, _ in w._tab_builders()])

    def test_builders_return_triples_or_add_their_own_tab(self):
        """_tab_runner passes a returned value straight to _add_tab, so a
        builder must return a 2- or 3-tuple, or None if it added its own."""
        import test_group_export as tge
        w = tge.build_window()
        for title, build in w._tab_builders():
            self.assertTrue(callable(build), title)


class TestWrappingTabs(unittest.TestCase):
    """The tab strip must wrap rather than hide tabs: a dozen analyses do
    not fit one row on a laptop, and QTabWidget's answer - a scroll arrow -
    made most of the analysis invisible."""

    def _widget(self, labels):
        from PySide6.QtWidgets import QLabel
        from app.gui.wrapping_tabs import WrappingTabWidget
        tabs = WrappingTabWidget()
        for label in labels:
            tabs.addTab(QLabel(label), label)
        tabs.resize(400, 300)
        tabs.show()
        for _ in range(3):
            _APP.processEvents()
        return tabs

    def _rows(self, tabs):
        return len({b.geometry().y() for b in tabs._buttons})

    def test_narrow_strip_wraps_instead_of_hiding_tabs(self):
        tabs = self._widget([f"Analysis number {i}" for i in range(8)])
        self.assertEqual(tabs.count(), 8)
        self.assertGreater(self._rows(tabs), 1)
        # every tab is laid out inside the strip, not pushed off the end
        for b in tabs._buttons:
            self.assertLessEqual(b.geometry().right(), tabs._strip.width() + 1, b.text())

    def test_labels_are_never_truncated(self):
        """The width comes from the text, so the full label always fits -
        that is the whole point of replacing QTabWidget here."""
        tabs = self._widget(["Condition × Difficulty", "Condition A Strategy", "Quality"])
        for b in tabs._buttons:
            needed = b.fontMetrics().horizontalAdvance(b.text())
            self.assertGreaterEqual(b.width(), needed, b.text())

    def test_clear_removes_buttons_and_pages(self):
        tabs = self._widget(["a", "b", "c"])
        tabs.clear()
        self.assertEqual(tabs.count(), 0)
        self.assertEqual(len(tabs._buttons), 0)

    def test_selecting_a_tab_shows_its_page(self):
        tabs = self._widget(["a", "b", "c"])
        tabs._buttons[2].click()
        _APP.processEvents()
        self.assertEqual(tabs.currentIndex(), 2)
        self.assertEqual(tabs.tabText(2), "c")
        # exactly one tab reads as selected
        self.assertEqual(sum(b.isChecked() for b in tabs._buttons), 1)



class TestLongStepsStayResponsive(unittest.TestCase):
    """A step that runs for minutes must not look like a hung program.

    It runs on the GUI thread, so nothing repaints while it does - which
    is why the fix is the step checking in, not the bar's style. These
    pin that the check-in actually moves the bar, in both of its forms.
    """

    def _spy(self):
        values, labels = [], []
        original_value = progress_task.QProgressDialog.setValue
        original_label = progress_task.QProgressDialog.setLabelText

        def set_value(dialog, value):
            values.append(value)
            return original_value(dialog, value)

        def set_label(dialog, text):
            labels.append(text)
            return original_label(dialog, text)

        progress_task.QProgressDialog.setValue = set_value
        progress_task.QProgressDialog.setLabelText = set_label
        return values, labels, (original_value, original_label)

    def _restore(self, originals):
        progress_task.QProgressDialog.setValue = originals[0]
        progress_task.QProgressDialog.setLabelText = originals[1]

    def test_a_step_reporting_a_fraction_advances_the_bar(self):
        values, labels, originals = self._spy()
        try:
            def step(report):
                for i in range(20):
                    report(f"resample {i + 1}/20", (i + 1) / 20)
                    time.sleep(0.01)

            self.assertTrue(progress_task.run_with_progress(None, "t", [("work", step)]))
        finally:
            self._restore(originals)
        inside = [v for v in values if 0 < v < progress_task._TICKS_PER_STEP]
        self.assertGreater(len(inside), 1, "the bar never moved inside the step")
        self.assertEqual(inside, sorted(inside), "a measured fraction must not go backwards")

    def test_a_step_without_a_fraction_sweeps_back_and_forth(self):
        """The 'still working, cannot say how far' case: the bar has to
        move, and it has to be visibly different from real progress."""
        values, labels, originals = self._spy()
        try:
            def step(report):
                deadline = time.monotonic() + 2.2
                while time.monotonic() < deadline:
                    report("fitting something")
                    time.sleep(0.01)

            self.assertTrue(progress_task.run_with_progress(None, "t", [("work", step)]))
        finally:
            self._restore(originals)
        inside = [v for v in values if v < progress_task._TICKS_PER_STEP]
        self.assertGreater(len(inside), 4)
        rises = sum(1 for i in range(len(inside) - 1) if inside[i + 1] > inside[i])
        falls = sum(1 for i in range(len(inside) - 1) if inside[i + 1] < inside[i])
        self.assertGreater(rises, 0)
        self.assertGreater(falls, 0, "the indicator never swept back")

    def test_the_sweep_never_reaches_the_end_of_the_step(self):
        """A bounce that touched the far end would read as 'finished'."""
        values, _, originals = self._spy()
        try:
            def step(report):
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    report("working")
                    time.sleep(0.01)

            progress_task.run_with_progress(None, "t", [("work", step)])
        finally:
            self._restore(originals)
        inside = [v for v in values if v < progress_task._TICKS_PER_STEP]
        self.assertLess(max(inside), progress_task._TICKS_PER_STEP)

    def test_a_step_can_be_cancelled_part_way_without_returning_a_partial(self):
        completed = []

        def step(report):
            for i in range(500):
                report(f"item {i}", i / 500)
            completed.append("finished")

        original = progress_task.QProgressDialog.wasCanceled
        calls = {"n": 0}

        def cancelled_after_a_while(dialog):
            calls["n"] += 1
            return calls["n"] > 20

        progress_task.QProgressDialog.wasCanceled = cancelled_after_a_while
        try:
            result = progress_task.run_with_progress(
                None, "t", [("work", step), ("later", lambda: completed.append("later"))])
        finally:
            progress_task.QProgressDialog.wasCanceled = original
        self.assertFalse(result)
        self.assertNotIn("finished", completed)
        self.assertNotIn("later", completed)

    def test_zero_argument_steps_still_work_unchanged(self):
        """Every existing caller passes zero-argument steps."""
        ran = []
        result = progress_task.run_with_progress(
            None, "t", [("a", lambda: ran.append("a")), ("b", lambda: ran.append("b"))])
        self.assertTrue(result)
        self.assertEqual(ran, ["a", "b"])

    def test_a_step_taking_an_argument_is_given_the_reporter(self):
        received = []
        progress_task.run_with_progress(
            None, "t", [("a", lambda report: received.append(report))])
        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], progress_task.StepProgress)

if __name__ == "__main__":
    unittest.main(verbosity=2)
