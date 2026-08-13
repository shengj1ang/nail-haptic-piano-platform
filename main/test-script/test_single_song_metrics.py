"""Tests for the GUI-free Single Song Complexity Evaluation logic.

Run from main/:  python test-script/test_single_song_metrics.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import Config
from app.music_recording import FingeringEntry, SongMeta, save_fingering
from app.profiles import DATA_DIR as PROFILE_DATA_DIR
from app.sequence_generator import (
    Action,
    COMPONENTS,
    LEVELS,
    SequenceStats,
    constraint_violations,
    evaluate_song,
    profile_note_range,
)
from app.single_song_metrics import (
    ALL_CONSTRAINT_KEYS,
    CONSTRAINT_GROUPS,
    METRIC_DESCRIPTIONS,
    SingleSongEvaluationError,
    exact_reference_matches,
    evaluate_single_song,
    group_reference_profiles,
    metric_groups,
    non_dominated_reference_levels,
    reference_constraint_comparisons,
)
from app.song_library import SongEntry, list_song_entries


class TestSingleSongEvaluation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="single_song_metrics_")
        self.addCleanup(self.tmp.cleanup)
        self.music_dir = Path(self.tmp.name) / "music"

    def _entry(self, name="example"):
        return SongEntry(label=f"music/{name}", name=name, data_dir=self.music_dir)

    def _write_song(self, name="example", *, bounds=(None, None), fingers=None):
        folder = self.music_dir / name
        folder.mkdir(parents=True)
        if fingers is None:
            fingers = ["L1", "R2", None, "L3"]
        notes = [60, 64, 62, 67]
        entries = [
            FingeringEntry(
                time=float(i),
                note=notes[i],
                note_name=str(notes[i]),
                key_id=i,
                finger=finger,
                inside=True,
            )
            for i, finger in enumerate(fingers)
        ]
        SongMeta(
            title="Example title",
            difficulty=2,
            created_at=1.0,
            duration_s=4.0,
            note_count=len(entries),
            start_note=bounds[0],
            end_note=bounds[1],
        ).save(folder / "meta.json")
        save_fingering(entries, folder / "fingering.json")
        return self._entry(name)

    def test_loader_reuses_evaluate_song_and_profile_fallback(self):
        entry = self._write_song()
        result = evaluate_single_song(entry, fallback_bounds=(48, 72))
        actions, stats, total = evaluate_song(entry.name, entry.data_dir, fallback_bounds=(48, 72))

        self.assertEqual(result.title, "Example title")
        self.assertEqual(result.saved_difficulty, 2)
        self.assertEqual(result.total_notes, total)
        self.assertEqual(len(result.actions), 3)
        self.assertEqual(result.actions, actions)
        self.assertEqual(result.stats, stats)
        self.assertEqual((result.stats.k_min, result.stats.k_max), (48, 72))
        self.assertEqual(result.note_bounds_source, "active keyboard profile fallback")
        self.assertIn("L1", result.fingers_text)
        self.assertIn("60 (C4)", result.notes_text)

    def test_saved_generation_bounds_override_profile_fallback(self):
        entry = self._write_song(bounds=(50, 70))
        result = evaluate_single_song(entry, fallback_bounds=(48, 72))
        self.assertEqual((result.stats.k_min, result.stats.k_max), (50, 70))
        self.assertEqual(result.note_bounds_source, "saved start_note/end_note in meta.json")

    def test_reference_comparison_is_exactly_generator_violations(self):
        entry = self._write_song(bounds=(48, 72), fingers=["L1", "R2", "L3", "R4"])
        result = evaluate_single_song(entry)
        comparisons = reference_constraint_comparisons(result.stats)

        self.assertEqual(tuple(item.level for item in comparisons), LEVELS)
        for item in comparisons:
            self.assertEqual(item.violations, tuple(constraint_violations(result.stats, item.level)))
            all_keys = set(item.satisfied) | {message.split("=", 1)[0] for message in item.violations}
            from app.sequence_generator import LEVEL_CONSTRAINTS
            self.assertEqual(all_keys, set(LEVEL_CONSTRAINTS[item.level]))
        self.assertFalse(hasattr(result, "score"))
        self.assertFalse(hasattr(result, "inferred_difficulty"))

    def test_formal_structure_problems_do_not_suppress_metrics(self):
        entry = self._write_song(fingers=["R1", "R2", "R3", "R4"])
        result = evaluate_single_song(entry, fallback_bounds=(48, 72))

        text = "\n".join(result.structural_problems)
        self.assertIn("formal generated sequence requires exactly 30 events", text)
        self.assertIn("does not use both hands", text)
        self.assertGreater(result.stats.d_seq_mean, 0.0)

    def test_no_parseable_fingers_reports_a_specific_error(self):
        entry = self._write_song(fingers=[None, "", "unknown", None])
        with self.assertRaisesRegex(SingleSongEvaluationError, "none has a parseable finger label"):
            evaluate_single_song(entry, fallback_bounds=(48, 72))

    def test_invalid_fingering_json_reports_the_entry_error(self):
        entry = self._write_song()
        (self.music_dir / entry.name / "fingering.json").write_text("{bad json", encoding="utf-8")
        with self.assertRaisesRegex(SingleSongEvaluationError, "Could not load music/example"):
            evaluate_single_song(entry, fallback_bounds=(48, 72))

    def test_missing_fingering_file_reports_the_exact_missing_path(self):
        entry = self._write_song()
        path = self.music_dir / entry.name / "fingering.json"
        path.unlink()
        with self.assertRaisesRegex(SingleSongEvaluationError, "Required song file is missing:.*fingering.json"):
            evaluate_single_song(entry, fallback_bounds=(48, 72))

    def test_unusable_note_bounds_report_the_specific_reason(self):
        entry = self._write_song()
        with self.assertRaisesRegex(SingleSongEvaluationError, "lower bound 72 is above upper bound 48"):
            evaluate_single_song(entry, fallback_bounds=(72, 48))

    def test_bad_metadata_is_reported_but_metrics_use_fallback(self):
        entry = self._write_song()
        (self.music_dir / entry.name / "meta.json").write_text(json.dumps({"title": "incomplete"}), encoding="utf-8")
        result = evaluate_single_song(entry, fallback_bounds=(48, 72))

        self.assertIsNotNone(result.metadata_warning)
        self.assertIn("meta.json could not be read", result.metadata_warning)
        self.assertIsNone(result.saved_difficulty)
        self.assertEqual((result.stats.k_min, result.stats.k_max), (48, 72))

    def test_every_canonical_component_is_grouped_and_described(self):
        groups = metric_groups()
        grouped = [spec for group in ("C_m", "C_s", "C_c") for spec in groups[group]]
        self.assertEqual(grouped, list(COMPONENTS))
        self.assertTrue(all(spec.key in METRIC_DESCRIPTIONS for spec in COMPONENTS))


class TestCoefficientFreeReferenceProfiles(unittest.TestCase):
    @staticmethod
    def _alpha_stats():
        """A point inside every alpha numeric interval (span S=100)."""
        return SequenceStats(
            k_min=0,
            k_max=100,
            d_seq_mean=10.0,
            d_m_mean=5.0,
            d_m_p95=10.0,
            d_f_mean=0.5,
            r_l=20.0,
            r_r=20.0,
            h_norm=0.25,
            v_trans=0.2,
            p_pred=0.8,
            a_h=0.25,
            h_hand=0.8,
            b_h=0.8,
            o_lr=0.0,
            x_f=0.0,
            x_e=0.0,
        )

    def test_exact_alpha_point_uniquely_dominates_other_references(self):
        stats = self._alpha_stats()
        self.assertEqual(non_dominated_reference_levels(stats), ("alpha",))
        comparisons = reference_constraint_comparisons(stats)
        self.assertEqual(exact_reference_matches(comparisons, ()), ("alpha",))

    def test_structural_problem_prevents_an_exact_match_but_not_numeric_profile(self):
        stats = self._alpha_stats()
        comparisons = reference_constraint_comparisons(stats)
        self.assertEqual(
            exact_reference_matches(comparisons, ("does not use both hands",)),
            (),
        )
        self.assertEqual(non_dominated_reference_levels(stats), ("alpha",))

    def test_single_hand_makes_only_coordination_profile_not_comparable(self):
        profiles = {
            profile.group: profile
            for profile in group_reference_profiles(
                self._alpha_stats(),
                [Action(hand="R", finger=1, note=60), Action(hand="R", finger=2, note=62)],
            )
        }
        self.assertTrue(profiles["C_m"].comparable)
        self.assertTrue(profiles["C_s"].comparable)
        self.assertFalse(profiles["C_c"].comparable)
        self.assertEqual(profiles["C_c"].non_dominated_levels, ())
        self.assertIn("both hands", profiles["C_c"].limitation)

    def test_every_generator_constraint_is_assigned_to_one_group(self):
        grouped = tuple(
            key for group in ("C_m", "C_s", "C_c") for key in CONSTRAINT_GROUPS[group]
        )
        self.assertEqual(grouped, ALL_CONSTRAINT_KEYS)
        self.assertEqual(len(grouped), len(set(grouped)))


class TestMusic123ProjectData(unittest.TestCase):
    """Required integration check against the saved project recording."""

    def test_music_123_is_discovered_loaded_and_compared_without_classification(self):
        matches = [entry for entry in list_song_entries() if entry.label == "music/123"]
        self.assertEqual(len(matches), 1)

        cfg = Config.load()
        bounds = profile_note_range(cfg.active_keyboard_profile, PROFILE_DATA_DIR)
        result = evaluate_single_song(matches[0], fallback_bounds=bounds)

        self.assertEqual(result.title, "123")
        self.assertEqual(result.saved_difficulty, 1)
        self.assertEqual(result.total_notes, 32)
        self.assertEqual(len(result.actions), 32)
        self.assertEqual((result.stats.k_min, result.stats.k_max), (48, 72))
        self.assertEqual(len(result.reference_comparisons), 3)
        self.assertEqual(len([result.stats.metric(spec.key) for spec in COMPONENTS]), len(COMPONENTS))

        structure = "\n".join(result.structural_problems)
        self.assertIn("requires exactly 30 events", structure)
        self.assertIn("does not use both hands", structure)
        self.assertTrue(all(comparison.violations for comparison in result.reference_comparisons))
        self.assertEqual(result.exact_constraint_matches, ())
        self.assertEqual(result.overall_non_dominated_levels, LEVELS)

        profiles = {profile.group: profile for profile in result.group_reference_profiles}
        self.assertEqual(profiles["C_m"].non_dominated_levels, ("beta", "gamma"))
        self.assertEqual(profiles["C_s"].non_dominated_levels, ("beta",))
        self.assertFalse(profiles["C_c"].comparable)


class TestSingleSongWindowIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_window_defaults_to_music_123_and_populates_all_sections(self):
        from app.gui.single_song_metrics_window import SingleSongMetricsWindow

        window = SingleSongMetricsWindow(Config.load())
        self.addCleanup(window.close)
        self.app.processEvents()

        self.assertEqual(window.song_combo.currentText(), "music/123")
        self.assertIn("32 total", window.count_label.text())
        self.assertIn("not a difficulty automatically calculated", window.difficulty_label.text())
        self.assertEqual({name: table.rowCount() for name, table in window._metric_tables.items()}, {
            "C_m": 6,
            "C_s": 3,
            "C_c": 6,
        })
        self.assertEqual(window.reference_table.rowCount(), 3)
        self.assertIn("requires exactly 30 events", window.structure_edit.toPlainText())
        self.assertIn("Unclassified", window.constraint_status_label.text())
        self.assertIn("Not comparable as a complete three-group profile", window.overall_profile_label.text())
        self.assertIn("No unique closest level", window.overall_profile_label.text())
        self.assertIn("Mixed / no unique closest", window.motor_profile_label.text())
        self.assertIn("β (beta)-like", window.sequence_profile_label.text())
        self.assertIn("Not comparable", window.coordination_profile_label.text())

    def test_launcher_registers_the_new_third_tool_without_replacing_existing_tools(self):
        import launcher
        from app.gui.sequence_generator_window import SequenceGeneratorWindow
        from app.gui.sequence_metrics_window import SequenceMetricsWindow
        from app.gui.single_song_metrics_window import SingleSongMetricsWindow

        tools = next(items for title, items in launcher.SECTIONS if title.startswith("4."))
        self.assertEqual(tools[:2], [
            ("Experiment Sequence Generator", SequenceGeneratorWindow),
            ("Sequence/Music Metrics", SequenceMetricsWindow),
        ])
        self.assertEqual(tools[2], ("Single Song Complexity Evaluation", SingleSongMetricsWindow))


if __name__ == "__main__":
    unittest.main(verbosity=2)
