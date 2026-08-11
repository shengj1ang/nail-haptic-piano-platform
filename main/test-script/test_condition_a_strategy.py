"""Tests for Condition-A free-fingering strategy and motor-demand proxies.

Run from main/:  python test-script/test_condition_a_strategy.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app import condition_a_strategy as cas  # noqa: E402
from app.gui import condition_a_strategy_tab as cas_tab  # noqa: E402


def trial(participant, trial_index, actual_fingers, target_fingers,
          *, level="alpha", invalid_last=False):
    rows = []
    notes = [60, 62, 64, 65, 67, 69]
    for index, (actual, target) in enumerate(zip(actual_fingers, target_fingers)):
        rows.append({
            "participant": participant,
            "trial_index": trial_index,
            "condition": "A",
            "level": level,
            "event_index": index,
            "target_note": notes[index],
            "target_finger": target,
            "timed_out": False,
            "actual_note": notes[index],
            "key_correct": True,
            "actual_finger": actual,
            "rt_s": 0.4 + index * 0.01,
            "validity": "invalid_carryover" if invalid_last and index == len(actual_fingers) - 1 else "valid",
        })
    return rows


class TestConditionAStrategy(unittest.TestCase):
    def setUp(self):
        self.events = (
            trial("P01", 2, ["R2"] * 4,
                  ["R1", "R5", "R1", "R5"])
            + trial("P01", 8, ["L2", "R2", "L2", "R2"],
                    ["L2", "R2", "L2", "R2"], level="beta")
            + trial("P02", 1, ["R2", "R2", "R3", "R3"],
                    ["R1", "R5", "R1", "R5"])
        )

    def test_display_order_is_spatially_symmetric_around_the_thumbs(self):
        self.assertEqual(
            cas.SPATIAL_FINGER_ORDER,
            ["L5", "L4", "L3", "L2", "L1", "R1", "R2", "R3", "R4", "R5"],
        )

    def test_twenty_participant_hand_chart_keeps_a_fixed_compact_width(self):
        hand_usage = pd.DataFrame([
            {
                "participant": f"P{participant:02d}",
                "hand": hand,
                "n": 150,
                "share_pct": 50.0,
            }
            for participant in range(1, 21)
            for hand in ("L", "R")
        ])
        figure = cas_tab._participant_hand_usage_figure(hand_usage)
        self.assertEqual(figure.get_figwidth(), 10.5)
        widths = [patch.get_width() for patch in figure.axes[0].patches]
        self.assertTrue(widths)
        self.assertTrue(all(width <= 0.52 for width in widths))
        self.assertEqual(figure.axes[0].get_xticklabels()[0].get_rotation(), 45)

    def test_one_finger_trial_has_one_effective_finger_and_no_switches(self):
        frame = cas.condition_a_trial_strategy(self.events)
        row = frame[(frame["participant"] == "P01")
                    & (frame["trial_index"] == 2)].iloc[0]
        self.assertEqual(row["a_occurrence"], 1)
        self.assertEqual(row["dominant_finger"], "R2")
        self.assertEqual(row["dominant_share_pct"], 100)
        self.assertEqual(row["unique_fingers"], 1)
        self.assertAlmostEqual(row["effective_fingers"], 1)
        self.assertEqual(row["finger_switch_rate_pct"], 0)

    def test_motor_proxy_compares_fingering_on_the_same_notes(self):
        frame = cas.condition_a_trial_strategy(self.events)
        row = frame[(frame["participant"] == "P01")
                    & (frame["trial_index"] == 2)].iloc[0]
        self.assertEqual(row["actual_finger_transition_steps"], 0)
        self.assertEqual(row["reference_finger_transition_steps"], 4)
        self.assertAlmostEqual(
            row["actual_key_travel_semitones"],
            row["reference_key_travel_semitones"],
        )

    def test_invalid_carryover_is_excluded_from_every_measure(self):
        events = trial(
            "P03", 1,
            ["R2", "R2", "R2", "L5"],
            ["R1", "R1", "R1", "L5"],
            invalid_last=True,
        )
        row = cas.condition_a_trial_strategy(events).iloc[0]
        self.assertEqual(row["n_target_events"], 3)
        self.assertEqual(row["n_resolved_responses"], 3)
        self.assertEqual(row["dominant_share_pct"], 100)
        usage = cas.condition_a_finger_usage(events).set_index("finger")
        self.assertEqual(usage.loc["L5", "n"], 0)

    def test_finger_usage_is_zero_filled_and_participant_weighted(self):
        usage = cas.condition_a_finger_usage(self.events)
        self.assertEqual(len(usage), 2 * 10)
        self.assertEqual(set(usage["finger"]), set(cas.FINGERS))
        for participant, rows in usage.groupby("participant"):
            self.assertAlmostEqual(rows["share_pct"].sum(), 100, msg=participant)
        summary = cas.finger_usage_group_summary(usage).set_index("finger")
        self.assertEqual(summary.loc["R2", "n"], 2)
        hand_usage = cas.participant_hand_usage(usage)
        self.assertEqual(len(hand_usage), 2 * 2)
        for participant, rows in hand_usage.groupby("participant"):
            self.assertAlmostEqual(rows["share_pct"].sum(), 100, msg=participant)
            expected = usage.loc[usage["participant"] == participant, "n"].sum()
            self.assertEqual(rows["n"].sum(), expected, msg=participant)

    def test_occurrence_and_effort_exports_use_participants_as_units(self):
        frame = cas.condition_a_trial_strategy(self.events)
        occurrence = cas.occurrence_summary(frame)
        first_dominance = occurrence[
            (occurrence["a_occurrence"] == 1)
            & (occurrence["metric"] == "dominant_share_pct")
        ].iloc[0]
        self.assertEqual(first_dominance["n"], 2)
        within_level = cas.level_occurrence_summary(frame)
        alpha_first = within_level[
            (within_level["level"] == "alpha")
            & (within_level["level_occurrence"] == 1)
            & (within_level["metric"] == "dominant_share_pct")
        ].iloc[0]
        self.assertEqual(alpha_first["n"], 2)
        effort = cas.participant_motor_effort(frame)
        expected_cells = frame[["participant", "level"]].drop_duplicates()
        self.assertEqual(len(effort), len(expected_cells) * 3)
        group = cas.motor_effort_group_summary(effort)
        self.assertEqual(set(group["metric"]), set(cas.EFFORT_METRICS))
        first_alpha = group[
            (group["metric"] == "finger_transition_distance")
            & (group["level"] == "alpha")
        ].iloc[0]
        self.assertEqual(first_alpha["n"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
