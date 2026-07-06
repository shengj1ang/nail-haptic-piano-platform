"""Statistical validity checks for a difficulty-graded motor-sequence
stimulus set (generated sequences under data/sequence/; L1/L2/L3 =
alpha/beta/gamma): are the three levels actually statistically separable
on motor cost and H_norm, is there a consistent monotonic difficulty
trend, how much do the levels' distributions overlap, and is one of the
two features redundant with the other.

GUI-free by design - see app/gui/sequence_metrics_window.py for the
"Validate Stimulus Set" button and app/gui/stimulus_validation_dialog.py
for the report/plot window that use this.

Uses scipy.stats for the actual significance tests (Kruskal-Wallis H as
the omnibus 3-group test, pairwise Mann-Whitney U as the post-hoc test,
Spearman rank correlation for redundancy) rather than hand-rolled
p-values - these are the standard non-parametric choices for exactly this
situation (group sizes here can be as small as a handful of sequences per
level, and normality isn't guaranteed), and they double as the
"recommended tests" answer when a group is too small to actually run one.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

LEVELS = ("alpha", "beta", "gamma")
LEVEL_DISPLAY = {"alpha": "L1 (alpha)", "beta": "L2 (beta)", "gamma": "L3 (gamma)"}

SIGNIFICANCE_ALPHA = 0.05
# |Spearman r| at or above this (and significant) counts two features as
# redundant - a common rule-of-thumb cutoff for "strong" correlation, not
# a universal statistical law; adjust here if a stricter/looser standard
# is wanted for the thesis.
REDUNDANCY_THRESHOLD = 0.7
MIN_GROUP_SIZE = 2  # Mann-Whitney U / Kruskal-Wallis need at least this many points per group


@dataclass
class GroupSummary:
    level: str
    n: int
    mean: float
    median: float
    std: float
    minimum: float
    maximum: float
    q1: float
    q3: float


@dataclass
class PairwiseResult:
    level_a: str
    level_b: str
    n_a: int
    n_b: int
    u_stat: Optional[float]
    p_value: Optional[float]
    significant: Optional[bool]
    range_overlap: float  # 0..1 - fraction of the two groups' combined value range that both share
    skipped_reason: Optional[str] = None


@dataclass
class FeatureValidation:
    feature: str
    groups: Dict[str, GroupSummary]
    kruskal_h: Optional[float]
    kruskal_p: Optional[float]
    kruskal_significant: Optional[bool]
    kruskal_skipped_reason: Optional[str]
    pairwise: List[PairwiseResult]
    monotonic_mean: Optional[bool]
    monotonic_median: Optional[bool]


@dataclass
class ValidationReport:
    n_total: int
    n_by_level: Dict[str, int]
    cost: FeatureValidation
    entropy: FeatureValidation
    correlation_r: Optional[float]
    correlation_p: Optional[float]
    redundant: Optional[bool]
    conclusion: str  # "valid" | "partially valid" | "invalid" | "insufficient data"
    reasons: List[str]


def _group_summary(level: str, values: np.ndarray) -> GroupSummary:
    return GroupSummary(
        level=level,
        n=len(values),
        mean=float(np.mean(values)),
        median=float(np.median(values)),
        std=float(np.std(values, ddof=1)) if len(values) > 1 else 0.0,
        minimum=float(np.min(values)),
        maximum=float(np.max(values)),
        q1=float(np.percentile(values, 25)),
        q3=float(np.percentile(values, 75)),
    )


def _range_overlap(a: np.ndarray, b: np.ndarray) -> float:
    """Distribution-free overlap measure: how much of the two groups'
    combined value range is shared by both - 0 = fully separated ranges,
    1 = identical range. Doesn't assume normality, appropriate for the
    small samples typical here."""
    lo, hi = max(a.min(), b.min()), min(a.max(), b.max())
    combined_lo, combined_hi = min(a.min(), b.min()), max(a.max(), b.max())
    span = combined_hi - combined_lo
    if span <= 0:
        return 1.0
    return max(hi - lo, 0.0) / span


def _pairwise(level_a: str, values_a: np.ndarray, level_b: str, values_b: np.ndarray) -> PairwiseResult:
    overlap = _range_overlap(values_a, values_b)
    if len(values_a) < MIN_GROUP_SIZE or len(values_b) < MIN_GROUP_SIZE:
        return PairwiseResult(
            level_a,
            level_b,
            len(values_a),
            len(values_b),
            None,
            None,
            None,
            overlap,
            skipped_reason=f"need >= {MIN_GROUP_SIZE} sequences in both levels for Mann-Whitney U",
        )
    u_stat, p_value = stats.mannwhitneyu(values_a, values_b, alternative="two-sided")
    return PairwiseResult(
        level_a,
        level_b,
        len(values_a),
        len(values_b),
        float(u_stat),
        float(p_value),
        bool(p_value < SIGNIFICANCE_ALPHA),
        overlap,
    )


def _validate_feature(feature: str, by_level: Dict[str, np.ndarray]) -> FeatureValidation:
    groups = {level: _group_summary(level, values) for level, values in by_level.items() if len(values)}
    present = [level for level in LEVELS if level in groups]

    kruskal_h = kruskal_p = kruskal_sig = None
    kruskal_skip = None
    eligible = [level for level in present if len(by_level[level]) >= MIN_GROUP_SIZE]
    if len(eligible) == 3:
        h_stat, p_value = stats.kruskal(*(by_level[level] for level in eligible))
        kruskal_h, kruskal_p = float(h_stat), float(p_value)
        kruskal_sig = kruskal_p < SIGNIFICANCE_ALPHA
    else:
        kruskal_skip = (
            f"need all three levels represented with >= {MIN_GROUP_SIZE} sequences each for Kruskal-Wallis "
            f"(have: {', '.join(f'{LEVEL_DISPLAY[lv]}={len(by_level.get(lv, []))}' for lv in LEVELS)})"
        )

    pairwise = [_pairwise(a, by_level[a], b, by_level[b]) for a, b in combinations(present, 2)]

    monotonic_mean = monotonic_median = None
    if present == list(LEVELS):
        means = [groups[level].mean for level in LEVELS]
        medians = [groups[level].median for level in LEVELS]
        monotonic_mean = means[0] < means[1] < means[2]
        monotonic_median = medians[0] < medians[1] < medians[2]

    return FeatureValidation(
        feature, groups, kruskal_h, kruskal_p, kruskal_sig, kruskal_skip, pairwise, monotonic_mean, monotonic_median
    )


def validate_stimulus_set(records: List[Tuple[str, float, float]]) -> ValidationReport:
    """records: (level, motor_cost, h_norm) - one triple per generated
    sequence, level in {"alpha", "beta", "gamma"}."""
    by_level_cost: Dict[str, List[float]] = {level: [] for level in LEVELS}
    by_level_entropy: Dict[str, List[float]] = {level: [] for level in LEVELS}
    all_cost: List[float] = []
    all_entropy: List[float] = []

    for level, cost, entropy in records:
        if level not in LEVELS:
            continue
        by_level_cost[level].append(cost)
        by_level_entropy[level].append(entropy)
        all_cost.append(cost)
        all_entropy.append(entropy)

    n_by_level = {level: len(by_level_cost[level]) for level in LEVELS}
    n_total = sum(n_by_level.values())

    cost_arrays = {level: np.asarray(values, dtype=float) for level, values in by_level_cost.items()}
    entropy_arrays = {level: np.asarray(values, dtype=float) for level, values in by_level_entropy.items()}

    cost_validation = _validate_feature("Motor cost", cost_arrays)
    entropy_validation = _validate_feature("H_norm", entropy_arrays)

    correlation_r = correlation_p = None
    redundant = None
    if n_total >= 3:
        r_value, p_value = stats.spearmanr(all_cost, all_entropy)
        correlation_r, correlation_p = float(r_value), float(p_value)
        redundant = abs(correlation_r) >= REDUNDANCY_THRESHOLD and correlation_p < SIGNIFICANCE_ALPHA

    conclusion, reasons = _conclude(n_by_level, cost_validation, entropy_validation, redundant)
    return ValidationReport(
        n_total, n_by_level, cost_validation, entropy_validation, correlation_r, correlation_p, redundant, conclusion, reasons
    )


def _conclude(
    n_by_level: Dict[str, int], cost: FeatureValidation, entropy: FeatureValidation, redundant: Optional[bool]
) -> Tuple[str, List[str]]:
    reasons: List[str] = []

    missing = [LEVEL_DISPLAY[level] for level in LEVELS if n_by_level.get(level, 0) < MIN_GROUP_SIZE]
    if missing:
        reasons.append(f"Not enough sequences in: {', '.join(missing)} (need >= {MIN_GROUP_SIZE} each).")
        return "insufficient data", reasons

    def _all_pairs_significant(fv: FeatureValidation) -> Optional[bool]:
        known = [p.significant for p in fv.pairwise if p.significant is not None]
        return all(known) if known else None

    checks = {
        "Motor cost separable across levels (Kruskal-Wallis, p < 0.05)": cost.kruskal_significant,
        "H_norm separable across levels (Kruskal-Wallis, p < 0.05)": entropy.kruskal_significant,
        "Motor cost monotonic L1 < L2 < L3 (by median)": cost.monotonic_median,
        "H_norm monotonic L1 < L2 < L3 (by median)": entropy.monotonic_median,
        "Every adjacent level pair distinguishable on motor cost (Mann-Whitney U)": _all_pairs_significant(cost),
        "Every adjacent level pair distinguishable on H_norm (Mann-Whitney U)": _all_pairs_significant(entropy),
    }
    for name, ok in checks.items():
        reasons.append(f"{'PASS' if ok else 'FAIL' if ok is False else 'N/A'}: {name}")

    if redundant:
        reasons.append(
            "NOTE: motor cost and H_norm are strongly correlated across all sequences (redundant) - level "
            "separation may be driven by a single underlying dimension rather than two independent ones."
        )

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
    lines.append(f"Stimulus set validation - {report.n_total} generated sequence(s) ({counts})")
    lines.append("")

    for fv in (report.cost, report.entropy):
        lines.append(f"== {fv.feature} ==")
        for level in LEVELS:
            g = fv.groups.get(level)
            if g is None:
                lines.append(f"  {LEVEL_DISPLAY[level]}: no data")
                continue
            lines.append(
                f"  {LEVEL_DISPLAY[level]}: n={g.n}  mean={g.mean:.3f}  median={g.median:.3f}  std={g.std:.3f}  "
                f"range=[{g.minimum:.3f}, {g.maximum:.3f}]  IQR=[{g.q1:.3f}, {g.q3:.3f}]"
            )

        if fv.kruskal_h is not None:
            sig = "significant" if fv.kruskal_significant else "not significant"
            lines.append(f"  Kruskal-Wallis (omnibus, 3 groups): H={fv.kruskal_h:.3f}, p={fv.kruskal_p:.4f} ({sig} at α=0.05)")
        else:
            lines.append(f"  Kruskal-Wallis: skipped - {fv.kruskal_skipped_reason}")

        for p in fv.pairwise:
            label = f"{LEVEL_DISPLAY[p.level_a]} vs {LEVEL_DISPLAY[p.level_b]}"
            if p.skipped_reason:
                lines.append(f"    {label}: skipped ({p.skipped_reason}) - range overlap={p.range_overlap:.0%}")
            else:
                sig = "significant" if p.significant else "not significant"
                lines.append(
                    f"    {label}: Mann-Whitney U={p.u_stat:.1f}, p={p.p_value:.4f} ({sig}), "
                    f"range overlap={p.range_overlap:.0%}"
                )

        if fv.monotonic_median is not None:
            lines.append(f"  Monotonic trend L1<L2<L3 - by median: {'yes' if fv.monotonic_median else 'no'}, "
                         f"by mean: {'yes' if fv.monotonic_mean else 'no'}")
        lines.append("")

    lines.append("== Feature redundancy (motor cost vs H_norm, pooled across all levels) ==")
    if report.correlation_r is not None:
        verdict = "redundant" if report.redundant else "not redundant"
        lines.append(
            f"  Spearman r={report.correlation_r:.3f}, p={report.correlation_p:.4f} "
            f"({verdict} at |r|>={REDUNDANCY_THRESHOLD}, α=0.05)"
        )
    else:
        lines.append("  Skipped - need at least 3 sequences total.")
    lines.append("")

    lines.append(f"== Conclusion: {report.conclusion.upper()} ==")
    for reason in report.reasons:
        lines.append(f"  - {reason}")

    return "\n".join(lines)
