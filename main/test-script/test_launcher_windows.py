"""Launcher window lifecycle: what may coexist, and what closing means.

Three rules are pinned here.

Most tools claim the camera or the MIDI keyboard, so the launcher opens
one at a time. The analysis windows are the exception - they read exported
CSVs and nothing else, and reading a participant's numbers against the
group's is exactly a two-window job - so they coexist, with each other and
with whatever exclusive tool is open.

Each button's hover text names which of those two it is, or that it is a
detached tele-training process instead. That text is derived rather than
written per button, and TestLifetimeTooltips holds the derivation against
what the launcher really does, so a tool changing category cannot leave a
button promising the old one.

Closing the launcher ends the session. That has to include windows the
tools opened themselves (per-event review, the cue screen), because each
of those is top-level and keeps the process alive on its own, and it has
to survive a window that refuses to close - which is why the last word is
exit(0) rather than quit().

Run from main/:  python test-script/test_launcher_windows.py
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton  # noqa: E402

import launcher  # noqa: E402
from app.config import Config  # noqa: E402
from app.gui.group_analysis_window import GroupAnalysisWindow  # noqa: E402
from app.gui.participant_analysis_window import ParticipantAnalysisWindow  # noqa: E402
from app.gui.quiz_analysis_window import QuizAnalysisWindow  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class _Stubborn(QMainWindow):
    """Stands in for a window with an unsaved-changes prompt."""

    def closeEvent(self, event):  # noqa: N802
        event.ignore()


class TestConcurrentTools(unittest.TestCase):

    def setUp(self):
        self.win = launcher.LauncherWindow(Config.load())
        self.win.show()

    def tearDown(self):
        self.win.close()

    def test_the_two_analysis_windows_open_together(self):
        self.win._open(ParticipantAnalysisWindow)
        self.win._open(GroupAnalysisWindow)
        self.assertEqual(len(self.win._concurrent), 2)
        self.assertTrue(all(w.isVisible() for w in self.win._concurrent.values()))

    def test_reopening_raises_the_same_window_rather_than_duplicating(self):
        self.win._open(ParticipantAnalysisWindow)
        first = self.win._concurrent[ParticipantAnalysisWindow]
        self.win._open(ParticipantAnalysisWindow)
        self.assertIs(self.win._concurrent[ParticipantAnalysisWindow], first)
        self.assertEqual(len(self.win._concurrent), 1)

    def test_an_exclusive_tool_does_not_close_the_analysis_windows(self):
        """Losing a built analysis to opening an unrelated tool would cost
        the seconds it took to build, for no reason - they share nothing."""
        self.win._open(ParticipantAnalysisWindow)
        self.win._open(GroupAnalysisWindow)
        self.win._open(QuizAnalysisWindow)
        self.assertIsInstance(self.win._current, QuizAnalysisWindow)
        self.assertTrue(all(w.isVisible() for w in self.win._concurrent.values()))

    def test_exclusive_tools_still_replace_each_other(self):
        self.win._open(QuizAnalysisWindow)
        first = self.win._current
        self.win._open(QuizAnalysisWindow)
        self.assertIsNot(self.win._current, first)
        self.assertFalse(first.isVisible())


class TestLifetimeTooltips(unittest.TestCase):
    """The hover text promises a lifetime; these check it is the one the
    launcher actually gives that tool.

    Hover text about coexistence is the kind that rots silently - a tool
    moves in or out of CONCURRENT_TOOLS and its tooltip keeps describing
    the old behaviour, which is worse than no tooltip because it is
    believed. lifetime_tooltip derives the text from the two facts that
    implement the rule, and these tests pin that derivation to what
    _open() and closeEvent() do.
    """

    def test_every_button_says_what_it_will_do(self):
        win = launcher.LauncherWindow(Config.load())
        self.addCleanup(win.close)
        buttons = win.findChildren(QPushButton)
        self.assertTrue(buttons)
        self.assertTrue(all(btn.toolTip() for btn in buttons))

    def test_each_entry_gets_the_tooltip_matching_how_it_opens(self):
        known = {
            launcher.EXCLUSIVE_TOOLTIP,
            launcher.CONCURRENT_TOOLTIP,
            launcher.PROCESS_TOOLTIP,
            launcher.DEMO_TOOLTIP,
        }
        for _title, tools in launcher.SECTIONS:
            for label, entry in tools:
                tip = launcher.lifetime_tooltip(entry)
                self.assertIn(tip, known, label)
                if isinstance(entry, launcher.ActionEntry):
                    expected = entry.tooltip
                elif isinstance(entry, launcher.ProcessEntry):
                    expected = launcher.PROCESS_TOOLTIP
                elif entry in launcher.CONCURRENT_TOOLS:
                    expected = launcher.CONCURRENT_TOOLTIP
                else:
                    expected = launcher.EXCLUSIVE_TOOLTIP
                self.assertEqual(tip, expected, label)

    def test_only_the_process_tooltip_promises_to_outlive_the_launcher(self):
        """closeEvent() closes every sub-window and deliberately spares the
        detached processes, so exactly one of the three may say so."""
        outlives = "Keeps running after the launcher closes"
        self.assertIn(outlives, launcher.PROCESS_TOOLTIP)
        self.assertNotIn(outlives, launcher.EXCLUSIVE_TOOLTIP)
        self.assertNotIn(outlives, launcher.CONCURRENT_TOOLTIP)
        for tip in (launcher.EXCLUSIVE_TOOLTIP, launcher.CONCURRENT_TOOLTIP):
            self.assertIn("Closes when the launcher closes", tip)

    def test_the_concurrent_tooltip_is_worn_by_exactly_the_concurrent_tools(self):
        wearing = {
            entry
            for _title, tools in launcher.SECTIONS
            for _label, entry in tools
            if launcher.lifetime_tooltip(entry) == launcher.CONCURRENT_TOOLTIP
        }
        self.assertEqual(wearing, launcher.CONCURRENT_TOOLS)


class TestClosingTheLauncherEndsTheSession(unittest.TestCase):

    def _run_until_exit(self, build) -> int:
        """Build a window set, close the launcher, and return the event
        loop's exit code. The watchdog turns "never exits" into a failing
        exit code instead of a hung test run."""
        win = launcher.LauncherWindow(Config.load())
        win.show()
        build(win)
        QTimer.singleShot(20, win.close)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: _APP.exit(99))
        watchdog.start(3000)
        code = _APP.exec()
        watchdog.stop()
        return code

    def test_closes_tool_windows_and_exits(self):
        def build(win):
            win._open(ParticipantAnalysisWindow)
            win._open(GroupAnalysisWindow)
            win._open(QuizAnalysisWindow)
        self.assertEqual(self._run_until_exit(build), 0)

    def test_closes_sub_windows_the_tools_opened_themselves(self):
        """A tool's own child window is top-level too, so it would keep the
        process alive after the launcher is gone."""
        orphan = {}

        def build(win):
            win._open(ParticipantAnalysisWindow)
            orphan["w"] = QMainWindow()
            orphan["w"].show()
        self.assertEqual(self._run_until_exit(build), 0)
        self.assertFalse(orphan["w"].isVisible())

    def test_exits_even_when_a_window_refuses_to_close(self):
        """Qt 6's quit() abandons the shutdown if any window ignores the
        close request; exit(0) is what makes this not depend on goodwill."""
        def build(win):
            win._open(GroupAnalysisWindow)
            win._stubborn = _Stubborn()
            win._stubborn.show()
        self.assertEqual(self._run_until_exit(build), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
