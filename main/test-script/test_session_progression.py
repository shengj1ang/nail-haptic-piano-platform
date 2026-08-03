"""Tests for the difficulty-aligned session progression data and figures.

Run from main/:  python test-script/test_session_progression.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import session_progression as sp  # noqa: E402
from app import session_progression_figures as sp_figures  # noqa: E402
from app.group_analysis import CONDITIONS, LEVELS  # noqa: E402


def row(participant, trial_index, level, condition,
        rt_s=1.0, key_accuracy=0.9):
    return {
        "participant": participant,
        "trial_index": trial_index,
        "level": level,
        "condition": condition,
        "rt_correct_key_s": rt_s,
        "key_accuracy": key_accuracy,
    }


def schedule(participant, offset=0.0):
    """Interleaved 27-trial schedule: nine occurrences per difficulty."""
    rows = []
    trial_index = 1
    for occurrence in range(1, 10):
        for level_index, level in enumerate(LEVELS):
            condition = CONDITIONS[(occurrence + level_index) % len(CONDITIONS)]
            rows.append(row(
                participant,
                trial_index,
                level,
                condition,
                rt_s=0.6 + offset + 0.08 * level_index + 0.01 * occurrence,
                key_accuracy=0.80 + 0.04 * level_index + 0.005 * occurrence,
            ))
            trial_index += 1
    return rows


class TestDifficultyOccurrence(unittest.TestCase):
    def test_complete_schedule_has_nine_occurrences_for_every_level(self):
        frame = sp.difficulty_progression_metrics(schedule("P01"))
        self.assertEqual(len(frame), 27)
        self.assertEqual(set(frame["condition"]), {"A", "B", "C"})
        for level in LEVELS:
            sub = frame[frame["level"] == level]
            self.assertEqual(list(sub["difficulty_occurrence"]), list(range(1, 10)))
            self.assertTrue(sub["session_position"].is_monotonic_increasing)

    def test_condition_adjustment_removes_condition_level_composition(self):
        trials = [
            row("P01", 1, "alpha", "A", rt_s=1.0, key_accuracy=0.10),
            row("P01", 2, "alpha", "B", rt_s=11.0, key_accuracy=0.50),
            row("P01", 3, "alpha", "C", rt_s=21.0, key_accuracy=0.90),
            row("P01", 4, "alpha", "A", rt_s=3.0, key_accuracy=0.20),
            row("P01", 5, "alpha", "B", rt_s=13.0, key_accuracy=0.60),
            row("P01", 6, "alpha", "C", rt_s=23.0, key_accuracy=1.00),
        ]
        frame = sp.difficulty_progression_metrics(trials)
        np.testing.assert_allclose(
            frame["rt_correct_key_s_adjusted"], [11, 11, 11, 13, 13, 13])
        # The same calculation is generic rather than RT-specific.
        np.testing.assert_allclose(
            frame["key_accuracy_adjusted"], [0.50, 0.50, 0.50, 0.60, 0.60, 0.60])

    def test_missing_values_remain_missing_not_zero(self):
        frame = sp.difficulty_progression_metrics([
            row("P01", 1, "alpha", "A", rt_s=None, key_accuracy=None),
            row("P01", 2, "alpha", "B", rt_s=1.0, key_accuracy=0.8),
        ])
        self.assertTrue(np.isnan(frame.loc[0, "rt_correct_key_s_raw"]))
        self.assertTrue(np.isnan(frame.loc[0, "rt_correct_key_s_adjusted"]))
        self.assertTrue(np.isnan(frame.loc[0, "key_accuracy_adjusted"]))


class TestSummaryAndFigures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame = sp.difficulty_progression_metrics(
            schedule("P01", 0.0) + schedule("P02", 0.2))
        cls.summary = sp.difficulty_progression_summary(cls.frame)

    def test_group_summary_uses_one_value_per_participant(self):
        cell = self.summary[
            (self.summary["metric"] == "rt_correct_key_s_raw")
            & (self.summary["level"] == "alpha")
            & (self.summary["difficulty_occurrence"] == 1)
        ].iloc[0]
        self.assertEqual(int(cell["n"]), 2)
        expected = self.frame[
            (self.frame["level"] == "alpha")
            & (self.frame["difficulty_occurrence"] == 1)
        ]["rt_correct_key_s_raw"].mean()
        self.assertAlmostEqual(float(cell["mean"]), float(expected))

    def test_rt_and_key_accuracy_figures_show_individuals_and_means(self):
        figures = sp_figures.build_difficulty_progression_figures(
            self.frame, self.summary)
        self.assertEqual(set(figures), {
            "group_learning_difficulty_progression_rt",
            "group_learning_difficulty_progression_key_accuracy",
        })
        for figure in figures.values():
            self.assertEqual(len(figure.axes), 2)
            self.assertTrue(figure.legends)
            self.assertEqual(figure.legends[0].get_title().get_text(),
                             "Difficulty")
            self.assertEqual(
                [ax.get_title() for ax in figure.axes],
                ["Observed progression by difficulty",
                 "Composition-adjusted progression by difficulty"],
            )
            visible_text = " ".join(
                [ax.get_title() for ax in figure.axes]
                + [text.get_text()
                   for legend in figure.legends
                   for text in legend.get_texts()]
            )
            self.assertNotIn("A/B/C", visible_text)
            for level in LEVELS:
                self.assertIn(
                    sp_figures.LEVEL_DISPLAY_LABELS[level],
                    visible_text,
                )
            for ax in figure.axes:
                individual = [line for line in ax.lines if line.get_linewidth() < 1.0]
                group_means = [line for line in ax.lines if line.get_linewidth() > 2.0]
                self.assertEqual(len(individual), 6)  # 2 participants x 3 levels
                self.assertEqual(len(group_means), 3)
                for line in group_means:
                    np.testing.assert_array_equal(line.get_xdata(), np.arange(1, 10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
