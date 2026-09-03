"""Difficulty validation for the alpha/beta/gamma level pools, as
specified in the final report's implementation appendix § "Family Matching Tolerances" and
reported in its stimulus-register appendix:

  - For each scalar component of D = (C_m, C_s, C_c), report the median,
    interquartile range, and full range separately per level.
  - A component is *monotonic* when the level medians follow the intended
    ordering AND fewer than 10% of adjacent-level pairwise comparisons
    (every alpha-beta pair plus every beta-gamma pair) violate that
    ordering. Components expected to decrease with difficulty (P_pred)
    are checked in the opposite direction.
  - B_h is a matching constraint, not a difficulty dimension: it is
    checked against the level-specific balance threshold rather than for
    a monotonic trend. H_hand is a descriptive diagnostic only - reported,
    but excluded from monotonic validation (see the decision documented
    on app.sequence_generator.LEVEL_CONSTRAINTS).
  - Cross-region metrics are validated separately: X_f = X_e = 0 must
    hold for every alpha and beta sequence, while every gamma sequence
    must stay inside the gamma cross-region limits.
  - Structurally, every sequence must contain both hands and pass the
    report's rejection rules (app.sequence_generator.structural_violations);
    the one-key-one-finger-per-event rule is guaranteed by the Action
    event type itself.

The same validation runs in two places: the Experiment Sequence Generator
right after generating fresh pools (before they are locked for the pilot
study), and the Sequence Metrics window's "Validate Stimulus Set" button
over saved sequences that were re-evaluated with the exact same
compute_stats() functions. GUI-free by design - see
app/gui/stimulus_validation_dialog.py for the report/plot window.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .sequence_generator import (
    COMPONENTS,
    LEVEL_CONSTRAINTS,
    LEVEL_DISPLAY,
    STRICT_LOWER,
    Sequence,
    SequenceStats,
    structural_violations,
)

LEVELS = ("alpha", "beta", "gamma")

# Where the validation dialog's "Export" button saves a report: one folder
# per validated batch, data/sequence_validation/<batch id>/, holding the
# plain-text report and the box-plot image (see
# app/gui/stimulus_validation_dialog.py). The batch id is normally the
# generation seed, which is what identifies a batch everywhere else too.
VALIDATION_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "sequence_validation"

# the final report's Methods chapter §"Controlled Stimulus Sequence Generator": level medians
# must follow the intended ordering "with fewer than 10% adjacent-level
# violations".
MONOTONIC_VIOLATION_LIMIT = 0.10


@dataclass
class GroupSummary:
    """Median, IQR, and full range of one component within one level's
    pool - exactly the statistics the final report's stimulus-register appendix reports."""

    level: str
    n: int
    median: float
    q1: float
    q3: float
    minimum: float
    maximum: float


@dataclass
class ComponentValidation:
    key: str  # SequenceStats attribute
    label: str  # display label (d̄_seq, H_norm, ...)
    group: str  # "C_m" | "C_s" | "C_c"
    direction: str  # "increase" | "decrease" | "matching" | "cross"
    groups: Dict[str, GroupSummary]
    # Monotonicity results; None for matching/cross components or when a
    # level pool is missing.
    medians_ordered: Optional[bool] = None
    violation_rate: Optional[float] = None
    monotonic: Optional[bool] = None


@dataclass
class CrossRegionCheck:
    level: str
    passed: bool
    problems: List[str]


@dataclass
class BalanceCheck:
    """Per the report, B_h is checked against the level-specific balance
    threshold rather than interpreted as a monotonic difficulty score."""

    level: str
    passed: bool
    problems: List[str]


@dataclass
class ValidationReport:
    n_total: int
    n_by_level: Dict[str, int]
    components: List[ComponentValidation]
    cross_checks: List[CrossRegionCheck]
    balance_checks: List[BalanceCheck]
    structural_problems: List[str]  # one line per offending sequence
    conclusion: str  # "valid" | "partially valid" | "invalid" | "insufficient data"
    reasons: List[str]


def _group_summary(level: str, values: np.ndarray) -> GroupSummary:
    return GroupSummary(
        level=level,
        n=len(values),
        median=float(np.median(values)),
        q1=float(np.percentile(values, 25)),
        q3=float(np.percentile(values, 75)),
        minimum=float(np.min(values)),
        maximum=float(np.max(values)),
    )


def _adjacent_violation_rate(by_level: Dict[str, np.ndarray], increasing: bool) -> Optional[float]:
    """Fraction of all adjacent-level pairwise comparisons (alpha x beta
    plus beta x gamma) where the lower level's value strictly violates the
    intended ordering against the higher level's value."""
    violations = 0
    total = 0
    for lower, higher in (("alpha", "beta"), ("beta", "gamma")):
        a, b = by_level.get(lower), by_level.get(higher)
        if a is None or b is None or not len(a) or not len(b):
            return None
        for x in a:
            for y in b:
                total += 1
                if (x > y) if increasing else (x < y):
                    violations += 1
    return violations / total if total else None


def _validate_component(
    spec, by_level: Dict[str, np.ndarray]
) -> ComponentValidation:
    groups = {level: _group_summary(level, values) for level, values in by_level.items() if len(values)}
    result = ComponentValidation(
        key=spec.key, label=spec.label, group=spec.group, direction=spec.direction, groups=groups
    )

    if spec.direction not in ("increase", "decrease"):
        return result
    if any(level not in groups for level in LEVELS):
        return result

    medians = [groups[level].median for level in LEVELS]
    increasing = spec.direction == "increase"
    result.medians_ordered = (
        medians[0] <= medians[1] <= medians[2] if increasing else medians[0] >= medians[1] >= medians[2]
    )
    result.violation_rate = _adjacent_violation_rate(by_level, increasing)
    if result.violation_rate is not None:
        result.monotonic = result.medians_ordered and result.violation_rate < MONOTONIC_VIOLATION_LIMIT
    return result


def _check_cross_region(level: str, stats_list: List[SequenceStats]) -> CrossRegionCheck:
    """Per the constraint table, X_f = X_e = 0 must hold for alpha and
    beta; gamma must
    remain within the gamma cross-region limits of the constraint table."""
    problems = []
    if level in ("alpha", "beta"):
        for i, s in enumerate(stats_list, start=1):
            if s.x_f != 0.0 or s.x_e != 0.0:
                problems.append(f"{LEVEL_DISPLAY[level]} sequence {i}: X_f={s.x_f:.3f}, X_e={s.x_e:.1f} (must be 0)")
    else:
        x_f_lo, x_f_hi = LEVEL_CONSTRAINTS["gamma"]["x_f"]
        x_e_hi = LEVEL_CONSTRAINTS["gamma"]["x_e_s"][1]
        strict = "x_f" in STRICT_LOWER["gamma"]
        for i, s in enumerate(stats_list, start=1):
            x_f_ok = (s.x_f > x_f_lo if strict else s.x_f >= x_f_lo) and s.x_f <= x_f_hi
            x_e_ok = s.metric("x_e_s") <= x_e_hi
            if not (x_f_ok and x_e_ok):
                problems.append(
                    f"{LEVEL_DISPLAY[level]} sequence {i}: X_f={s.x_f:.3f} (need ({x_f_lo}, {x_f_hi}]), "
                    f"X_e/S={s.metric('x_e_s'):.3f} (need <= {x_e_hi})"
                )
    return CrossRegionCheck(level=level, passed=not problems, problems=problems)


def _check_balance(level: str, stats_list: List[SequenceStats]) -> BalanceCheck:
    """Every sequence's B_h must sit at or above its level's balance
    floor from the constraint table."""
    b_h_min = LEVEL_CONSTRAINTS[level]["b_h"][0]
    problems = [
        f"{LEVEL_DISPLAY[level]} sequence {i}: B_h={s.b_h:.3f} below the level threshold {b_h_min}"
        for i, s in enumerate(stats_list, start=1)
        if s.b_h < b_h_min
    ]
    return BalanceCheck(level=level, passed=not problems, problems=problems)


def validate_level_pools(
    pools: Dict[str, List[Tuple[Sequence, SequenceStats]]],
) -> ValidationReport:
    """pools: level -> [(actions, stats), ...] - freshly generated
    families, or saved sequences re-evaluated by
    app.sequence_generator.evaluate_song()."""
    n_by_level = {level: len(pools.get(level, [])) for level in LEVELS}
    n_total = sum(n_by_level.values())

    by_level_values: Dict[str, Dict[str, np.ndarray]] = {}
    for spec in COMPONENTS:
        by_level_values[spec.key] = {
            level: np.asarray([stats.metric(spec.key) for _, stats in pools.get(level, [])], dtype=float)
            for level in LEVELS
        }

    components = [_validate_component(spec, by_level_values[spec.key]) for spec in COMPONENTS]
    cross_checks = [
        _check_cross_region(level, [stats for _, stats in pools.get(level, [])])
        for level in LEVELS
        if pools.get(level)
    ]
    balance_checks = [
        _check_balance(level, [stats for _, stats in pools.get(level, [])])
        for level in LEVELS
        if pools.get(level)
    ]

    structural_problems: List[str] = []
    for level in LEVELS:
        for i, (actions, _stats) in enumerate(pools.get(level, []), start=1):
            for problem in structural_violations(actions):
                structural_problems.append(f"{LEVEL_DISPLAY[level]} sequence {i}: {problem}")

    conclusion, reasons = _conclude(n_by_level, components, cross_checks, balance_checks, structural_problems)
    return ValidationReport(
        n_total=n_total,
        n_by_level=n_by_level,
        components=components,
        cross_checks=cross_checks,
        balance_checks=balance_checks,
        structural_problems=structural_problems,
        conclusion=conclusion,
        reasons=reasons,
    )


def _conclude(
    n_by_level: Dict[str, int],
    components: List[ComponentValidation],
    cross_checks: List[CrossRegionCheck],
    balance_checks: List[BalanceCheck],
    structural_problems: List[str],
) -> Tuple[str, List[str]]:
    reasons: List[str] = []

    missing = [LEVEL_DISPLAY[level] for level in LEVELS if n_by_level.get(level, 0) == 0]
    if missing:
        reasons.append(f"No sequences in: {', '.join(missing)}.")
        return "insufficient data", reasons

    checks: Dict[str, Optional[bool]] = {}
    for c in components:
        if c.direction in ("increase", "decrease"):
            arrow = "rising" if c.direction == "increase" else "falling"
            rate = f", adjacent-pair violations {c.violation_rate:.1%}" if c.violation_rate is not None else ""
            checks[f"{c.label} monotonic ({arrow} medians{rate})"] = c.monotonic

    cross_ok = all(check.passed for check in cross_checks)
    checks["Cross-region limits (X_f = X_e = 0 for α/β; γ within table limits)"] = cross_ok
    checks["B_h at or above each level's balance threshold"] = all(check.passed for check in balance_checks)
    checks["Structural rules (both hands, trigrams, 60% share, finger runs)"] = not structural_problems

    for name, ok in checks.items():
        reasons.append(f"{'PASS' if ok else 'FAIL' if ok is False else 'N/A'}: {name}")

    known = [ok for ok in checks.values() if ok is not None]
    if not known:
        return "insufficient data", reasons
    passed = sum(1 for ok in known if ok)
    if passed == len(known):
        return "valid", reasons
    if passed >= len(known) / 2:
        return "partially valid", reasons
    return "invalid", reasons


def format_report(report: ValidationReport) -> str:
    lines: List[str] = []
    counts = ", ".join(f"{LEVEL_DISPLAY[level]}={report.n_by_level.get(level, 0)}" for level in LEVELS)
    lines.append(f"Difficulty validation - {report.n_total} sequence(s) ({counts})")
    lines.append("")

    group_names = {"C_m": "C_m (motor movement cost)", "C_s": "C_s (sequence complexity)",
                   "C_c": "C_c (bimanual coordination)"}
    current_group = None
    for c in report.components:
        if c.group != current_group:
            current_group = c.group
            lines.append(f"== {group_names[c.group]} ==")
        role = {"increase": "expected to rise", "decrease": "expected to fall",
                "matching": "matching constraint (no ordering required)",
                "diagnostic": "descriptive diagnostic (reported only)",
                "cross": "validated separately (cross-region limits)"}[c.direction]
        lines.append(f"  {c.label} - {role}")
        for level in LEVELS:
            g = c.groups.get(level)
            if g is None:
                lines.append(f"    {LEVEL_DISPLAY[level]}: no data")
                continue
            lines.append(
                f"    {LEVEL_DISPLAY[level]}: n={g.n}  median={g.median:.3f}  "
                f"IQR=[{g.q1:.3f}, {g.q3:.3f}]  range=[{g.minimum:.3f}, {g.maximum:.3f}]"
            )
        if c.direction in ("increase", "decrease") and c.monotonic is not None:
            rate = f"{c.violation_rate:.1%}" if c.violation_rate is not None else "n/a"
            lines.append(
                f"    medians ordered: {'yes' if c.medians_ordered else 'no'}; adjacent-pair violation rate: "
                f"{rate} (limit {MONOTONIC_VIOLATION_LIMIT:.0%}) -> {'MONOTONIC' if c.monotonic else 'NOT MONOTONIC'}"
            )
        lines.append("")

    lines.append("== Cross-region checks ==")
    for check in report.cross_checks:
        lines.append(f"  {LEVEL_DISPLAY[check.level]}: {'PASS' if check.passed else 'FAIL'}")
        for problem in check.problems:
            lines.append(f"    - {problem}")
    lines.append("")

    lines.append("== Balance threshold checks (B_h vs level floor) ==")
    for check in report.balance_checks:
        lines.append(f"  {LEVEL_DISPLAY[check.level]}: {'PASS' if check.passed else 'FAIL'}")
        for problem in check.problems:
            lines.append(f"    - {problem}")
    lines.append("")

    lines.append("== Structural checks ==")
    if report.structural_problems:
        for problem in report.structural_problems:
            lines.append(f"  - {problem}")
    else:
        lines.append("  All sequences use both hands and pass the rejection rules.")
    lines.append("")

    lines.append(f"== Conclusion: {report.conclusion.upper()} ==")
    for reason in report.reasons:
        lines.append(f"  - {reason}")

    return "\n".join(lines)
