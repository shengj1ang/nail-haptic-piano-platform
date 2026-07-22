"""Unit tests for app.group_analysis (the GUI-free aggregation and
statistics layer behind the Group Analysis window).

Run from main/:  python test-script/test_group_analysis.py
"""

import csv
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app.group_analysis import (  # noqa: E402
    CONDITIONS,
    LEVELS,
    MIN_TEST_N,
    GroupData,
    cell_availability,
    condition_inference,
    condition_pivot,
    group_center,
    load_group,
    load_participant_rows,
    paired_differences,
    participant_cell_metrics,
    participant_condition_metrics,
    participant_outcome_proportions,
    participant_repetition_metrics,
    per_finger_metrics,
    quality_summary,
    scan_participants,
    session_position_metrics,
    valid_events,
    within_cell_repetition,
)
from app.participant_analysis import CAT_CK_CF, CAT_CK_WF, CAT_NO_RESPONSE  # noqa: E402


# ---------------------------------------------------------------------------
# Row factories (mirror the exported CSV schema fields the module reads)

def trial(participant="P01", trial_index=1, condition="A", level="alpha",
          analyzed=True, **kw) -> dict:
    base = {
        "participant": participant,
        "trial_index": trial_index,
        "condition": condition,
        "level": level,
        "sequence": f"seq-{condition}-{level}",
        "analyzed": analyzed,
        "note_count": 30,
        "key_accuracy": 0.9,
        "fa_main": 0.5,
        "fa_given_key": 0.55,
        "rt_correct_key_s": 0.5,
        "rt_complete_s": 0.45,
        "misses": 0,
        "suspected_carryover": 0,
        "excluded_carryover": 0,
        "manual_corrections": 0,
        "unresolved_rate": 0.0,
        "ambiguous_rate": 0.0,
        "borderline_events": 0,
        "qc_extra_presses": 0,
        "sync_method": "manual",
    }
    base.update(kw)
    return base


def event(participant="P01", condition="A", **kw) -> dict:
    base = {
        "participant": participant,
        "trial_index": 1,
        "condition": condition,
        "level": "alpha",
        "event_index": 0,
        "target_finger": "R2",
        "target_hand": "R",
        "timed_out": False,
        "target_note": 60,
        "actual_note": 60,
        "key_correct": True,
        "actual_finger": "R2",
        "finger_correct": True,
        "rt_s": 0.5,
        "validity": "valid",
        "manually_corrected": False,
        "target_key_id": 5,
        "actual_key_id": 5,
    }
    base.update(kw)
    return base


def full_schedule(participant: str, fa_by_condition=None, rt_by_condition=None):
    """27 trials (3 conditions x 3 levels x 3 repetitions) with a
    participant-specific presentation order."""
    fa_by_condition = fa_by_condition or {}
    rt_by_condition = rt_by_condition or {}
    rows = []
    index = 1
    for c in CONDITIONS:
        for lv in LEVELS:
            for _rep in range(3):
                rows.append(trial(participant=participant, trial_index=index,
                                  condition=c, level=lv,
                                  fa_main=fa_by_condition.get(c, 0.5),
                                  rt_correct_key_s=rt_by_condition.get(c, 0.5)))
                index += 1
    return rows


# ---------------------------------------------------------------------------


class TestLoader(unittest.TestCase):
    def _write_participant(self, root: Path, participant: str,
                           with_trials=True, with_events=True) -> None:
        pdir = root / participant
        pdir.mkdir(parents=True)
        (pdir / "TrialStructure.json").write_text("{}")
        if with_trials:
            rows = [trial(participant=participant, fa_main=None)]  # None -> ""
            with open(pdir / f"{participant}_trials.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows([{k: ("" if v is None else v) for k, v in r.items()}
                             for r in rows])
        if with_events:
            rows = [event(participant=participant),
                    event(participant=participant, event_index=1, timed_out=True,
                          actual_note=None, key_correct=False, actual_finger=None,
                          finger_correct=None, rt_s=None)]
            with open(pdir / f"{participant}_events.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows([{k: ("" if v is None else v) for k, v in r.items()}
                             for r in rows])

    def test_round_trip_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_participant(root, "P01")
            trials, events = load_participant_rows("P01", root)
            t = trials[0]
            self.assertIs(t["analyzed"], True)
            self.assertIsNone(t["fa_main"])          # "" -> None, not 0
            self.assertEqual(t["trial_index"], 1)     # int
            self.assertEqual(t["key_accuracy"], 0.9)  # float
            self.assertEqual(t["condition"], "A")     # string kept
            e_ok, e_timeout = events
            self.assertIs(e_ok["key_correct"], True)
            self.assertIsNone(e_timeout["rt_s"])
            self.assertIsNone(e_timeout["finger_correct"])
            self.assertIs(e_timeout["timed_out"], True)

    def test_missing_files_reported_not_raised_by_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_participant(root, "P01")
            self._write_participant(root, "P02", with_trials=False, with_events=False)
            statuses = {s.participant: s for s in scan_participants(root)}
            self.assertTrue(statuses["P01"].ready)
            self.assertFalse(statuses["P02"].exported)
            self.assertFalse(statuses["P02"].ready)
            self.assertIn("not exported", statuses["P02"].summary())

    def test_load_group_subset_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for p in ("P01", "P02", "P03"):
                self._write_participant(root, p)
            self._write_participant(root, "P04", with_events=False)
            data = load_group(["P01", "P03", "P04"], root)
            self.assertEqual(data.included, ["P01", "P03"])  # subset honoured, P02 untouched
            self.assertEqual(data.n, 2)
            self.assertIn("P04", data.errors)
            self.assertEqual({t["participant"] for t in data.trial_rows}, {"P01", "P03"})

    def test_empty_selection_and_no_valid_participants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = load_group([], root)
            self.assertEqual(data.n, 0)
            self.assertEqual(data.errors, {})
            data2 = load_group(["P99"], root)
            self.assertEqual(data2.n, 0)
            self.assertIn("P99", data2.errors)
            # Aggregations over the empty result must not raise.
            self.assertTrue(participant_condition_metrics(data2.trial_rows).empty)
            self.assertTrue(participant_outcome_proportions(data2.event_rows).empty)
            self.assertTrue(group_center(participant_condition_metrics([]), "fa_main",
                                         ["condition"]).empty)
            self.assertTrue(per_finger_metrics([]).empty)

    def test_reanalysis_reflects_new_selection_only(self):
        """Selection change = a fresh load_group call; nothing from the
        previous selection may leak into the new result."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for p in ("P01", "P02"):
                self._write_participant(root, p)
            first = load_group(["P01", "P02"], root)
            second = load_group(["P02"], root)
            self.assertEqual(first.included, ["P01", "P02"])
            self.assertEqual(second.included, ["P02"])
            self.assertEqual({t["participant"] for t in second.trial_rows}, {"P02"})
            pc = participant_condition_metrics(second.trial_rows)
            self.assertEqual(set(pc["participant"]), {"P02"})


class TestParticipantAggregation(unittest.TestCase):
    def test_n1_descriptives_work(self):
        trials = full_schedule("P01", fa_by_condition={"A": 0.2, "B": 0.5, "C": 0.8})
        pc = participant_condition_metrics(trials)
        self.assertEqual(len(pc), 3)
        center = group_center(pc, "fa_main", ["condition"])
        self.assertEqual(list(center["n"]), [1, 1, 1])
        self.assertTrue(center["ci95_lo"].isna().all())  # no fabricated interval at N=1
        self.assertAlmostEqual(
            float(center.set_index("condition").loc["C", "mean"]), 0.8)

    def test_group_center_unit_is_participant_not_trials_or_events(self):
        trials = full_schedule("P01") + full_schedule("P02")
        self.assertEqual(len(trials), 54)
        pc = participant_condition_metrics(trials)
        center = group_center(pc, "fa_main", ["condition"])
        self.assertEqual(list(center["n"]), [2, 2, 2])  # participants, not 18 trials

    def test_unanalyzed_trials_excluded_from_fa_not_zeroed(self):
        trials = [trial(fa_main=0.6), trial(trial_index=2, analyzed=False, fa_main=0.0)]
        pc = participant_condition_metrics(trials)
        self.assertAlmostEqual(float(pc["fa_main"].iloc[0]), 0.6)
        self.assertEqual(int(pc["n_analyzed"].iloc[0]), 1)

    def test_missing_cells_are_absent_not_zero(self):
        trials = [trial(condition="A", level="alpha"), trial(condition="B", level="beta",
                                                             trial_index=2)]
        cells = participant_cell_metrics(trials)
        self.assertEqual(len(cells), 2)  # no zero-filled rows for the other 7 cells
        avail = cell_availability(trials).set_index(["condition", "level"])
        self.assertEqual(int(avail.loc[("A", "alpha"), "n_participants"]), 1)
        self.assertEqual(int(avail.loc[("C", "gamma"), "n_participants"]), 0)


class TestPairedContrasts(unittest.TestCase):
    def test_pairing_and_missing_pairs(self):
        # P01 complete; P02 has no C data -> only B-A pair for P02.
        trials = full_schedule("P01", fa_by_condition={"A": 0.2, "B": 0.4, "C": 0.9})
        trials += [t for t in full_schedule("P02", fa_by_condition={"A": 0.5, "B": 0.5})
                   if t["condition"] != "C"]
        pc = participant_condition_metrics(trials)
        diffs = paired_differences(pc, "fa_main")
        by = diffs.groupby("contrast")["diff"]
        self.assertEqual(by.count()["B−A"], 2)
        self.assertEqual(by.count()["C−A"], 1)  # P02 dropped, not imputed
        self.assertEqual(by.count()["C−B"], 1)
        p1 = diffs[(diffs["participant"] == "P01") & (diffs["contrast"] == "C−B")]
        self.assertAlmostEqual(float(p1["diff"].iloc[0]), 0.5)

    def test_condition_pivot_shape(self):
        pivot = condition_pivot(participant_condition_metrics(full_schedule("P01")),
                                "fa_main")
        self.assertEqual(list(pivot.columns), CONDITIONS)
        self.assertEqual(list(pivot.index), ["P01"])


class TestLearningAlignment(unittest.TestCase):
    def test_repetition_aligns_by_within_cell_order_across_different_schedules(self):
        # Same cell, opposite presentation order for the two participants.
        trials = [
            trial("P01", trial_index=3, condition="B", level="beta", fa_main=0.3),
            trial("P01", trial_index=10, condition="B", level="beta", fa_main=0.6),
            trial("P01", trial_index=25, condition="B", level="beta", fa_main=0.9),
            trial("P02", trial_index=20, condition="B", level="beta", fa_main=0.1),
            trial("P02", trial_index=7, condition="B", level="beta", fa_main=0.5),
            trial("P02", trial_index=1, condition="B", level="beta", fa_main=0.8),
        ]
        rep = within_cell_repetition(trials).set_index(["participant", "repetition"])
        self.assertAlmostEqual(float(rep.loc[("P01", 1), "fa_main"]), 0.3)
        self.assertAlmostEqual(float(rep.loc[("P01", 3), "fa_main"]), 0.9)
        # P02's repetition 1 is trial_index 1 (fa 0.8), not the list order.
        self.assertAlmostEqual(float(rep.loc[("P02", 1), "fa_main"]), 0.8)
        self.assertAlmostEqual(float(rep.loc[("P02", 3), "fa_main"]), 0.1)
        agg = participant_repetition_metrics(trials)
        self.assertEqual(set(agg["repetition"]), {1, 2, 3})

    def test_session_position_keeps_actual_order(self):
        trials = full_schedule("P01")
        pos = session_position_metrics(trials)
        self.assertEqual(list(pos["position"]), list(range(1, 28)))


class TestOutcomeProportions(unittest.TestCase):
    def test_invalid_carryover_excluded_from_outcomes(self):
        events = [
            event(),
            event(event_index=1, validity="invalid_carryover", key_correct=False),
            event(event_index=2, timed_out=True, actual_note=None, key_correct=False,
                  actual_finger=None, finger_correct=None, rt_s=None),
        ]
        self.assertEqual(len(valid_events(events)), 2)
        props = participant_outcome_proportions(events)
        row = props.set_index("category")
        self.assertEqual(int(row.loc[CAT_CK_CF, "count"]), 1)
        self.assertEqual(int(row.loc[CAT_NO_RESPONSE, "count"]), 1)
        self.assertEqual(int(row.loc[CAT_CK_CF, "n_events"]), 2)  # denominator excludes carry-over
        self.assertAlmostEqual(float(props["proportion"].sum()), 1.0)  # exclusive + exhaustive

    def test_proportions_are_per_participant_not_pooled(self):
        # P01: 1/1 wrong-finger; P02: 0/3. Pooled would be 1/4 = 0.25;
        # the participant-level mean must be (1.0 + 0.0) / 2 = 0.5.
        events = [event("P01", condition="B", finger_correct=False, actual_finger="R3")]
        events += [event("P02", condition="B", event_index=i) for i in range(3)]
        props = participant_outcome_proportions(events)
        ckwf = props[props["category"] == CAT_CK_WF].set_index("participant")
        self.assertAlmostEqual(float(ckwf.loc["P01", "proportion"]), 1.0)
        self.assertAlmostEqual(float(ckwf.loc["P02", "proportion"]), 0.0)
        center = group_center(props[props["category"] == CAT_CK_WF], "proportion",
                              ["condition"])
        self.assertEqual(int(center["n"].iloc[0]), 2)  # participants, not 4 events
        self.assertAlmostEqual(float(center["mean"].iloc[0]), 0.5)


class TestPerFinger(unittest.TestCase):
    def test_homologous_merge_keeps_lr_counts(self):
        events = [
            event(target_finger="L2", actual_finger="L2"),
            event(target_finger="R2", actual_finger="R2", event_index=1),
            event(target_finger="R2", actual_finger="R3", finger_correct=False,
                  event_index=2),
            event(target_finger="R2", event_index=3, validity="invalid_carryover"),
            event(target_finger="R5", event_index=4, timed_out=True, actual_note=None,
                  key_correct=False, actual_finger=None, finger_correct=None, rt_s=None),
        ]
        pf = per_finger_metrics(events).set_index("finger_id")
        self.assertEqual(int(pf.loc[2, "n"]), 3)  # carry-over excluded
        self.assertEqual(int(pf.loc[2, "n_left"]), 1)
        self.assertEqual(int(pf.loc[2, "n_right"]), 2)
        self.assertAlmostEqual(float(pf.loc[2, "fa"]), 2 / 3)
        self.assertEqual(int(pf.loc[5, "n"]), 0)  # timeout not counted as responded
        self.assertTrue(math.isnan(float(pf.loc[5, "fa"])))


class TestQuality(unittest.TestCase):
    def test_quality_counts(self):
        trials = [trial(excluded_carryover=1, suspected_carryover=2,
                        manual_corrections=3, qc_extra_presses=4)]
        events = [event(), event(event_index=1, validity="invalid_carryover")]
        q = quality_summary(trials, events)
        row = q.iloc[0]
        self.assertEqual(int(row["excluded_carryover"]), 1)
        self.assertEqual(int(row["suspected_carryover"]), 2)
        self.assertEqual(int(row["manual_corrections"]), 3)
        self.assertEqual(int(row["qc_extra_presses"]), 4)
        self.assertEqual(int(row["n_events"]), 2)
        self.assertEqual(int(row["n_valid_events"]), 1)


class TestThresholdSensitivity(unittest.TestCase):
    def test_theta_table_includes_main_040(self):
        from app.group_analysis import threshold_sensitivity
        trials = [trial(fa_main=0.5, **{"fa_theta_0.30": 0.6, "fa_theta_0.50": 0.4})]
        theta = threshold_sensitivity(trials)
        by = theta.set_index("theta")["fa"]
        self.assertAlmostEqual(float(by["0.30"]), 0.6)
        self.assertAlmostEqual(float(by["0.40"]), 0.5)  # fa_main is the θ=0.40 analysis
        self.assertAlmostEqual(float(by["0.50"]), 0.4)
        # No fa_theta columns -> empty, not a crash.
        self.assertTrue(threshold_sensitivity([trial()]).empty)
        self.assertTrue(threshold_sensitivity([]).empty)


class TestInference(unittest.TestCase):
    def _pc(self, n_participants, fa_by_condition):
        trials = []
        for i in range(n_participants):
            jitter = i * 0.01  # break ties so Wilcoxon has nonzero diffs
            trials += full_schedule(
                f"P{i + 1:02d}",
                fa_by_condition={c: v + jitter for c, v in fa_by_condition.items()})
        return participant_condition_metrics(trials)

    def test_no_inference_at_n1(self):
        res = condition_inference(self._pc(1, {"A": 0.2, "B": 0.5, "C": 0.8}), "fa_main")
        self.assertIsNone(res["friedman"])
        self.assertEqual(res["pairwise"], [])
        self.assertIn(f"N ≥ {MIN_TEST_N}", res["reason"])
        self.assertEqual(res["n_complete"], 1)

    def test_inference_runs_with_enough_complete_cases(self):
        res = condition_inference(self._pc(6, {"A": 0.2, "B": 0.5, "C": 0.8}), "fa_main")
        self.assertIsNone(res["reason"])
        self.assertEqual(res["n_complete"], 6)
        self.assertTrue(res["exploratory"])  # small N stays flagged
        fried = res["friedman"]
        self.assertEqual(fried["n"], 6)
        self.assertLess(fried["p"], 0.05)
        self.assertGreater(fried["effect_size"], 0.9)  # perfectly ordered
        for entry in res["pairwise"]:
            self.assertEqual(entry["n_pairs"], 6)
            self.assertFalse(np.isnan(entry["p"]))
            self.assertGreaterEqual(entry["p_holm"], entry["p"])
            self.assertEqual(abs(entry["effect_size"]), 1.0)
        by = {e["contrast"]: e for e in res["pairwise"]}
        self.assertAlmostEqual(by["C−A"]["mean_diff"], 0.6, places=6)
        self.assertGreater(by["B−A"]["effect_size"], 0)  # direction preserved

    def test_missing_pairs_counted_and_events_never_inflate_n(self):
        pc = self._pc(6, {"A": 0.2, "B": 0.5, "C": 0.8})
        pc = pc[~((pc["participant"] == "P06") & (pc["condition"] == "C"))]
        res = condition_inference(pc, "fa_main")
        self.assertEqual(res["n_complete"], 5)
        self.assertEqual(res["n_missing_pairs"], 1)
        by = {e["contrast"]: e for e in res["pairwise"]}
        self.assertEqual(by["B−A"]["n_pairs"], 6)  # both sides exist for P06
        self.assertEqual(by["C−A"]["n_pairs"], 5)

    def test_holm_correction_values(self):
        from app.group_analysis import _holm
        # Sorted: 0.01*3=0.03, then max(0.03, 0.03*2)=0.06, then step-down
        # monotonicity lifts 0.04*1 to 0.06 as well.
        self.assertEqual(_holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])
        self.assertEqual(_holm([0.5]), [0.5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
