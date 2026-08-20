"""The Quiz Analysis condition segments (app/gui/quiz_analysis_window.py).

The three A/B/C buttons latch, combine, and drive a selection that four
other buttons, a hand-ticked checkbox, the filter box and a refresh can
all change underneath them. That is a state machine, and the failure it
can produce is quiet and expensive: a segment left down over somebody
else's selection claims a scope, and the next press of "Analyze
selected" runs MediaPipe over whatever is actually checked - hundreds of
videos - not over what the lit button says.

These tests pin the two invariants that keep it honest:
  - while a segment is down, the checked rows are exactly the visible
    quizzes of the pressed conditions, and
  - the moment that stops being true, the segments come back up without
    disturbing the selection that replaced theirs.

Run from main/:  python test-script/test_quiz_condition_filter.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import Config  # noqa: E402
from app.gui import quiz_analysis_window as qaw  # noqa: E402

_APP = QApplication.instance() or QApplication([])

# Two participants so the filter box has something to narrow to, and one
# folder with no condition segment at all (the curated/remote names the
# real data dir carries) - it must never be selected by any condition.
QUIZZES = [
    "P01-T01-Aα", "P01-T02-Bβ", "P01-T03-Cγ", "P01-T04-Bα",
    "P02-T01-Aβ", "P02-T02-Bγ", "P02-T03-Cα", "P02-T04-Cβ",
    "remote-1786710570",
]


class ConditionSegmentTest(unittest.TestCase):

    def setUp(self):
        # list_quizzes is the only thing the row list comes from; the
        # per-row meta.json load is allowed to fail (the window writes
        # "couldn't load" into the status cell and keeps the row), which
        # is all the selection logic needs.
        self._saved = qaw.list_quizzes
        qaw.list_quizzes = lambda: list(QUIZZES)
        self.win = qaw.QuizAnalysisWindow(Config.load())
        self.addCleanup(self.win.close)
        self.addCleanup(lambda: setattr(qaw, "list_quizzes", self._saved))

    # -- helpers -------------------------------------------------------

    def press(self, *letters: str) -> None:
        for letter in letters:
            btn = self.win._condition_btns[letter]
            btn.setChecked(not btn.isChecked())

    def down(self) -> str:
        return "".join(c for c in "ABC" if self.win._condition_btns[c].isChecked())

    def checked(self) -> set:
        return set(self.win._checked_names())

    def expected(self, *letters: str, prefix: str = "") -> set:
        return {
            name for name in QUIZZES
            if qaw._condition_of(name) in set(letters) and name.startswith(prefix)
        }

    # -- the segments drive the selection ------------------------------

    def test_one_segment_selects_exactly_its_condition(self):
        self.press("B")
        self.assertEqual(self.checked(), self.expected("B"))

    def test_two_segments_select_the_union(self):
        """The whole point of dropping the single-select behaviour: one
        batch covering two conditions."""
        self.press("B", "C")
        self.assertEqual(self.checked(), self.expected("B", "C"))

    def test_releasing_one_leaves_the_other(self):
        self.press("B", "C")
        self.press("C")
        self.assertEqual(self.checked(), self.expected("B"))

    def test_releasing_the_last_one_selects_nothing(self):
        """Not 'everything': an empty condition set is no claim at all,
        and the batch buttons go back to disabled."""
        self.press("B")
        self.press("B")
        self.assertEqual(self.checked(), set())
        self.assertFalse(self.win.analyze_btn.isEnabled())

    def test_a_quiz_with_no_condition_is_never_selected(self):
        self.press("A", "B", "C")
        self.assertNotIn("remote-1786710570", self.checked())

    # -- and let go of it when somebody else takes over ----------------

    def test_another_select_button_releases_the_segments(self):
        self.press("B")
        self.win._select_rows(lambda analyzed: True)  # Select shown
        self.assertEqual(self.down(), "")
        # Released, but the selection it was released *for* is intact.
        self.assertEqual(self.checked(), set(QUIZZES))

    def test_hand_ticking_a_row_releases_the_segments(self):
        self.press("B")
        row = next(r for r in range(self.win.table.rowCount())
                   if self.win._quiz_name(r) == "P01-T01-Aα")
        self.win.table.item(row, qaw.COL_QUIZ).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.down(), "")
        self.assertEqual(self.checked(), self.expected("B") | {"P01-T01-Aα"})

    def test_select_none_releases_the_segments(self):
        self.press("B", "C")
        self.win._select_rows(None)
        self.assertEqual(self.down(), "")
        self.assertEqual(self.checked(), set())

    def test_a_refresh_releases_the_segments(self):
        self.press("B")
        self.win._refresh_quizzes()
        self.assertEqual(self.down(), "")
        self.assertEqual(self.checked(), set())

    # -- the filter box is the one thing they follow instead -----------

    def test_filtering_re_derives_rather_than_releasing(self):
        """A pressed segment means "the *shown* quizzes of this
        condition", so narrowing the filter narrows it too."""
        self.press("B")
        self.win.filter_edit.setText("P01")
        self.assertEqual(self.down(), "B")
        self.assertEqual(self.checked(), self.expected("B", prefix="P01"))

    def test_filtering_stops_a_hidden_row_staying_checked(self):
        """The hazard the re-derive exists for: a checked row scrolled out
        of existence is still a row the batch buttons would run on."""
        self.press("B", "C")
        self.win.filter_edit.setText("P02")
        hidden_checked = [
            self.win._quiz_name(row)
            for row in range(self.win.table.rowCount())
            if self.win.table.isRowHidden(row)
            and self.win.table.item(row, qaw.COL_QUIZ).checkState() == Qt.CheckState.Checked
        ]
        self.assertEqual(hidden_checked, [])

    def test_clearing_the_filter_widens_the_selection_again(self):
        self.press("B")
        self.win.filter_edit.setText("P01")
        self.win.filter_edit.setText("")
        self.assertEqual(self.checked(), self.expected("B"))


if __name__ == "__main__":
    unittest.main()
