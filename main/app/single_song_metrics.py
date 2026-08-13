"""Read-only analysis helpers for one saved music/sequence entry.

The GUI in :mod:`app.gui.single_song_metrics_window` deliberately keeps
all loading and comparison work here so it can be tested without Qt.  No
difficulty formula lives in this module: metric values come from
``sequence_generator.evaluate_song`` and the alpha/beta/gamma reference
checks come from ``constraint_violations`` and ``structural_violations``.

The saved ``meta.json`` difficulty is returned only as metadata.  A
``SingleSongEvaluation`` never contains an inferred or weighted difficulty
score because the generator defines difficulty as D = (C_m, C_s, C_c), not
as a scalar classifier for arbitrary recordings.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from .music_recording import META_FILENAME, SongMeta, song_dir
from .sequence_generator import (
    COMPONENTS,
    LEVEL_CONSTRAINTS,
    LEVEL_DIFFICULTY,
    LEVEL_DISPLAY,
    LEVELS,
    SEQUENCE_LENGTH,
    STRICT_LOWER,
    ComponentSpec,
    Sequence,
    SequenceStats,
    constraint_violations,
    evaluate_song,
    format_sequence_for_display,
    structural_violations,
)
from .song_library import SongEntry


METRIC_DESCRIPTIONS: Dict[str, str] = {
    "d_seq_mean": "Mean note displacement between consecutive cue events (semitones).",
    "d_m_mean": "Mean note displacement between consecutive uses of the same hand (semitones).",
    "d_m_p95": "95th-percentile same-hand note displacement (semitones).",
    "d_f_mean": "Mean finger-number change between consecutive uses of the same hand.",
    "r_l": "Range of MIDI notes used by the left hand (semitones).",
    "r_r": "Range of MIDI notes used by the right hand (semitones).",
    "h_norm": "Normalised entropy of the observed transition classes.",
    "v_trans": "Fraction of transitions represented by distinct transition classes.",
    "p_pred": "Best first-order next-move prediction rate; lower means less predictable.",
    "a_h": "Fraction of adjacent events that alternate hands.",
    "h_hand": "Normalised entropy of LL/LR/RL/RR hand transitions (descriptive only).",
    "b_h": "Left/right event-count balance; 1.0 is perfectly balanced.",
    "o_lr": "Overlap between the note regions used by the two hands.",
    "x_f": "Fraction of events where a hand crosses the keyboard midpoint.",
    "x_e": "Largest distance beyond the midpoint for a cross-region event (semitones).",
}


CONSTRAINT_LABELS: Dict[str, str] = {
    "d_seq_mean_s": "d̄_seq/S",
    "d_m_mean_s": "d̄_m/S",
    "d_m_p95_s": "d_m95/S",
    "d_f_mean": "d̄_f",
    "r_h_s": "max(R_L, R_R)/S",
    "h_norm": "H_norm",
    "a_h": "A_h",
    "b_h": "B_h",
    "o_lr": "O_LR",
    "x_f": "X_f",
    "x_e_s": "X_e/S",
}

# Only metrics with an existing LEVEL_CONSTRAINTS threshold can contribute
# to an alpha/beta/gamma reference profile.  V_trans, P_pred and H_hand stay
# visible as descriptive metrics, exactly as in the generator design, but
# are deliberately absent here because inventing thresholds for them would
# be another new difficulty model.
CONSTRAINT_GROUPS: Dict[str, Tuple[str, ...]] = {
    "C_m": ("d_seq_mean_s", "d_m_mean_s", "d_m_p95_s", "d_f_mean", "r_h_s"),
    "C_s": ("h_norm",),
    "C_c": ("a_h", "b_h", "o_lr", "x_f", "x_e_s"),
}
ALL_CONSTRAINT_KEYS: Tuple[str, ...] = tuple(
    key for group in ("C_m", "C_s", "C_c") for key in CONSTRAINT_GROUPS[group]
)


class SingleSongEvaluationError(Exception):
    """A saved entry cannot be analysed safely enough to show metrics."""


@dataclass(frozen=True)
class ReferenceComparison:
    """One level's generator constraints used as a descriptive reference.

    ``violations`` is the unmodified output of ``constraint_violations``.
    The structure intentionally has no inferred-level or classification
    field: even an empty violations tuple is only a threshold comparison.
    """

    level: str
    satisfied: Tuple[str, ...]
    violations: Tuple[str, ...]


@dataclass(frozen=True)
class GroupReferenceProfile:
    """Coefficient-free, possibly set-valued reference result for one C-group.

    More than one level is an intentional ``mixed`` result: without a
    coefficient or priority rule there is no justified way to force a tie
    between non-dominated levels.  ``comparable`` is false for C_c when a
    recording has no genuine bimanual sequence to compare.
    """

    group: str
    non_dominated_levels: Tuple[str, ...]
    comparable: bool
    limitation: Optional[str] = None


@dataclass(frozen=True)
class SingleSongEvaluation:
    entry: SongEntry
    title: str
    saved_difficulty: Optional[int]
    actions: Sequence
    stats: SequenceStats
    total_notes: int
    fingers_text: str
    notes_text: str
    note_bounds_source: str
    metadata_warning: Optional[str]
    warnings: Tuple[str, ...]
    structural_problems: Tuple[str, ...]
    reference_comparisons: Tuple[ReferenceComparison, ...]
    exact_constraint_matches: Tuple[str, ...]
    overall_non_dominated_levels: Tuple[str, ...]
    group_reference_profiles: Tuple[GroupReferenceProfile, ...]


def saved_difficulty_text(value: Optional[int]) -> str:
    """Human-readable metadata value, explicitly not an inferred result."""
    if value is None:
        return "Unavailable"
    level = next((name for name, stored in LEVEL_DIFFICULTY.items() if stored == value), None)
    if level is None:
        return str(value)
    return f"{LEVEL_DISPLAY[level]} (stored value {value})"


def constraint_bound_text(level: str, key: str) -> str:
    """Format the generator's inclusive/strict interval for display."""
    lo, hi = LEVEL_CONSTRAINTS[level][key]
    left = "(" if key in STRICT_LOWER[level] else "["
    return f"{left}{lo:.3f}, {hi:.3f}]"


def reference_constraint_comparisons(stats: SequenceStats) -> Tuple[ReferenceComparison, ...]:
    """Compare metrics with every generator level, without classifying.

    The pass list is derived from the exact violation list returned by the
    generator, ensuring this view cannot drift from generation-time
    acceptance semantics.
    """
    comparisons = []
    for level in LEVELS:
        violations = tuple(constraint_violations(stats, level))
        violated_keys = {message.split("=", 1)[0] for message in violations}
        satisfied = tuple(key for key in LEVEL_CONSTRAINTS[level] if key not in violated_keys)
        comparisons.append(ReferenceComparison(level, satisfied, violations))
    return tuple(comparisons)


def _violation_costs(
    stats: SequenceStats,
    level: str,
    constraint_keys: Tuple[str, ...],
) -> Dict[str, Tuple[int, float]]:
    """Per-key costs used only for component-wise dominance.

    The first tuple item distinguishes a pass (0) from a violation (1),
    including a strict-lower-bound equality whose numerical gap is zero.
    The second is the distance to the nearest interval boundary in that
    *same metric's units*.  Costs from different metrics are never added,
    averaged, or compared with each other, so no cross-metric coefficient
    or scale is introduced.
    """
    violations = constraint_violations(stats, level)
    violated_keys = {message.split("=", 1)[0] for message in violations}
    costs: Dict[str, Tuple[int, float]] = {}
    for key in constraint_keys:
        if key not in violated_keys:
            costs[key] = (0, 0.0)
            continue
        value = stats.metric(key)
        lo, hi = LEVEL_CONSTRAINTS[level][key]
        costs[key] = (1, max(lo - value, value - hi, 0.0))
    return costs


def non_dominated_reference_levels(
    stats: SequenceStats,
    constraint_keys: Tuple[str, ...] = ALL_CONSTRAINT_KEYS,
) -> Tuple[str, ...]:
    """Return alpha/beta/gamma levels not Pareto-dominated by another.

    Level A dominates B only when A's violation cost is no worse for
    *every one of the same constraints* and strictly better for at least
    one.  There is no sum, mean, norm, rank count, or feature weight.  The
    mathematically honest result can therefore contain two or three
    levels when their advantages occur on different metrics.
    """
    if not constraint_keys:
        return ()
    unknown = set(constraint_keys) - set(ALL_CONSTRAINT_KEYS)
    if unknown:
        raise ValueError(f"Unknown reference constraint key(s): {', '.join(sorted(unknown))}")

    costs = {
        level: _violation_costs(stats, level, constraint_keys)
        for level in LEVELS
    }

    def dominates(first: str, second: str) -> bool:
        no_worse = all(costs[first][key] <= costs[second][key] for key in constraint_keys)
        strictly_better = any(costs[first][key] < costs[second][key] for key in constraint_keys)
        return no_worse and strictly_better

    return tuple(
        level
        for level in LEVELS
        if not any(other != level and dominates(other, level) for other in LEVELS)
    )


def group_reference_profiles(
    stats: SequenceStats,
    actions: Sequence,
) -> Tuple[GroupReferenceProfile, ...]:
    """C_m/C_s/C_c Pareto profiles without collapsing them to a score."""
    hands = {action.hand for action in actions}
    profiles = []
    for group in ("C_m", "C_s", "C_c"):
        if group == "C_c" and hands != {"L", "R"}:
            profiles.append(
                GroupReferenceProfile(
                    group=group,
                    non_dominated_levels=(),
                    comparable=False,
                    limitation="bimanual coordination requires parsed events from both hands",
                )
            )
            continue
        profiles.append(
            GroupReferenceProfile(
                group=group,
                non_dominated_levels=non_dominated_reference_levels(stats, CONSTRAINT_GROUPS[group]),
                comparable=True,
            )
        )
    return tuple(profiles)


def exact_reference_matches(
    comparisons: Tuple[ReferenceComparison, ...],
    structural_problems: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Levels passing every displayed numeric and structural check.

    For a real recording this remains reference compatibility, not proof
    that the algorithm has established a true musical difficulty level.
    """
    if structural_problems:
        return ()
    return tuple(comparison.level for comparison in comparisons if not comparison.violations)


def formal_structure_problems(actions: Sequence) -> Tuple[str, ...]:
    """Length plus the generator's reusable structural rejection rules."""
    problems = []
    if len(actions) != SEQUENCE_LENGTH:
        problems.append(
            f"has {len(actions)} parsed events; a formal generated sequence requires exactly "
            f"{SEQUENCE_LENGTH} events"
        )
    problems.extend(structural_violations(actions))
    return tuple(problems)


def _metadata(entry: SongEntry) -> Tuple[Optional[SongMeta], Optional[str]]:
    path = song_dir(entry.name, entry.data_dir) / META_FILENAME
    try:
        return SongMeta.load(path), None
    except FileNotFoundError:
        return None, f"meta.json is missing: {path}"
    except Exception as exc:
        return None, f"meta.json could not be read ({path}): {exc}"


def evaluate_single_song(
    entry: SongEntry,
    fallback_bounds: Optional[Tuple[int, int]] = None,
    fallback_bounds_error: Optional[str] = None,
) -> SingleSongEvaluation:
    """Load and analyse exactly one saved entry without writing anything.

    Note-bound precedence is the same as Sequence/Music Metrics through
    ``evaluate_song``: complete saved generation bounds, then the active
    profile fallback supplied by the caller, then parsed-note min/max.
    Metadata errors are reported but do not hide otherwise computable
    fingering metrics.
    """
    meta, metadata_warning = _metadata(entry)
    try:
        actions, stats, total_notes = evaluate_song(
            entry.name,
            entry.data_dir,
            fallback_bounds=fallback_bounds,
        )
    except FileNotFoundError as exc:
        missing = Path(exc.filename) if exc.filename else song_dir(entry.name, entry.data_dir)
        raise SingleSongEvaluationError(f"Required song file is missing: {missing}") from exc
    except Exception as exc:
        raise SingleSongEvaluationError(f"Could not load {entry.label}: {exc}") from exc

    if not actions:
        raise SingleSongEvaluationError(
            f"{entry.label} contains {total_notes} saved note(s), but none has a parseable "
            "finger label (expected L1-L5 or R1-R5), so complexity metrics cannot be computed."
        )
    if stats.k_min > stats.k_max:
        raise SingleSongEvaluationError(
            f"Usable note bounds could not be determined: lower bound {stats.k_min} is above "
            f"upper bound {stats.k_max}."
        )

    warnings = []
    has_saved_bounds = (
        meta is not None and meta.start_note is not None and meta.end_note is not None
    )
    if has_saved_bounds:
        bounds_source = "saved start_note/end_note in meta.json"
    elif fallback_bounds is not None:
        bounds_source = "active keyboard profile fallback"
    else:
        bounds_source = "parsed note minimum/maximum fallback"
        if fallback_bounds_error:
            warnings.append(
                f"Active-profile note bounds were unavailable ({fallback_bounds_error}); "
                "parsed note minimum/maximum were used instead."
            )

    if stats.k_min == stats.k_max:
        warnings.append(
            "The usable note range has zero span; existing metric code uses a span of 1 for "
            "normalised reference comparisons."
        )
    if meta is not None and meta.note_count != total_notes:
        warnings.append(
            f"meta.json records {meta.note_count} note(s), while fingering.json contains {total_notes}."
        )

    fingers_text, notes_text = format_sequence_for_display(actions)
    structural_problems = formal_structure_problems(actions)
    comparisons = reference_constraint_comparisons(stats)
    return SingleSongEvaluation(
        entry=entry,
        title=meta.title if meta is not None else entry.name,
        saved_difficulty=meta.difficulty if meta is not None else None,
        actions=actions,
        stats=stats,
        total_notes=total_notes,
        fingers_text=fingers_text,
        notes_text=notes_text,
        note_bounds_source=bounds_source,
        metadata_warning=metadata_warning,
        warnings=tuple(warnings),
        structural_problems=structural_problems,
        reference_comparisons=comparisons,
        exact_constraint_matches=exact_reference_matches(comparisons, structural_problems),
        overall_non_dominated_levels=non_dominated_reference_levels(stats),
        group_reference_profiles=group_reference_profiles(stats, actions),
    )


def metric_groups() -> Dict[str, Tuple[ComponentSpec, ...]]:
    """The canonical COMPONENTS ordering, grouped for the single-song UI."""
    return {
        group: tuple(spec for spec in COMPONENTS if spec.group == group)
        for group in ("C_m", "C_s", "C_c")
    }
