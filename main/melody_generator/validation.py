"""Automatic rejection rules.

A candidate melody is only accepted if it passes every check below. The checks
are grouped exactly as they are reasoned about:

  playability   range, white keys, layout membership, one note at a time
  fingering     assigned, consistent, no crossed hands, no finger hammering
  melody        leap size, consecutive leaps, stepwise share, overall span
  rhythm        not monotonous, not complex, at least one held note
  structure     motif repetition, phrase parallelism, non-random pitch use,
                a real cadence on the tonic

Each rule returns an issue with a stable ``code``, so a rejection can be
counted, reported and reproduced rather than just being "no good".
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from . import features
from .config import GeneratorConfig
from .layouts import Layout
from .melody import MelodyLine, PitchSpace
from .rhythm import RhythmPlan
from .timing import NoteEvent, RestEvent


@dataclass(frozen=True)
class Issue:
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    issues: Tuple[Issue, ...]

    def codes(self) -> Tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [
                {"code": issue.code, "message": issue.message}
                for issue in self.issues
            ],
        }


# --------------------------------------------------------------------------
# individual rule groups
# --------------------------------------------------------------------------

def _check_playability(
    notes: Sequence[NoteEvent], layout: Layout, cfg: GeneratorConfig
) -> List[Issue]:
    issues: List[Issue] = []
    vc = cfg.validation
    if len(notes) != cfg.melody.note_count:
        issues.append(
            Issue(
                "note_count",
                f"{len(notes)} note-on events, expected {cfg.melody.note_count}",
            )
        )
    playable = set(layout.keys())
    for note in notes:
        if not vc.midi_min <= note.midi_note <= vc.midi_max:
            issues.append(
                Issue(
                    "range_out_of_bounds",
                    f"event {note.event_index}: {note.note_name} "
                    f"({note.midi_note}) outside {vc.midi_min}-{vc.midi_max}",
                )
            )
        if vc.white_keys_only and note.midi_note % 12 not in (0, 2, 4, 5, 7, 9, 11):
            issues.append(
                Issue(
                    "non_white_key",
                    f"event {note.event_index}: {note.note_name} is a black key",
                )
            )
        if note.midi_note not in playable:
            issues.append(
                Issue(
                    "unplayable_key",
                    f"event {note.event_index}: {note.note_name} is outside "
                    f"the {layout.name} hand position",
                )
            )
    # Single voice: a key must be released before the next one is pressed.
    for first, second in zip(notes, notes[1:]):
        if first.note_off_time_sec > second.note_on_time_sec + 1e-9:
            issues.append(
                Issue(
                    "overlapping_notes",
                    f"event {first.event_index} is still held when event "
                    f"{second.event_index} starts",
                )
            )
        if second.note_on_time_sec <= first.note_on_time_sec:
            issues.append(
                Issue(
                    "non_monotonic_time",
                    f"event {second.event_index} does not start after "
                    f"event {first.event_index}",
                )
            )
    for note in notes:
        if note.note_off_time_sec <= note.note_on_time_sec:
            issues.append(
                Issue(
                    "non_positive_duration",
                    f"event {note.event_index} is never held",
                )
            )
    return issues


def _check_fingering(
    notes: Sequence[NoteEvent], layout: Layout, cfg: GeneratorConfig
) -> List[Issue]:
    issues: List[Issue] = []
    vc = cfg.validation
    valid_labels = {(slot.midi, slot.label) for slot in layout.slots}
    for note in notes:
        if not note.finger or note.finger[0] not in ("L", "R"):
            issues.append(
                Issue("fingering_missing", f"event {note.event_index} has no finger")
            )
            continue
        if (note.midi_note, note.finger) not in valid_labels:
            issues.append(
                Issue(
                    "fingering_impossible",
                    f"event {note.event_index}: {note.finger} cannot reach "
                    f"{note.note_name} in the {layout.name} position",
                )
            )
        if note.finger[0] != note.hand:
            issues.append(
                Issue(
                    "fingering_hand_mismatch",
                    f"event {note.event_index}: hand {note.hand} but finger "
                    f"{note.finger}",
                )
            )

    if vc.unique_finger_per_key:
        seen: Dict[int, str] = {}
        for note in notes:
            previous = seen.setdefault(note.midi_note, note.finger)
            if previous != note.finger:
                issues.append(
                    Issue(
                        "fingering_inconsistent",
                        f"{note.note_name} is played by both {previous} and "
                        f"{note.finger}",
                    )
                )
                break

    left = [n.midi_note for n in notes if n.hand == "L"]
    right = [n.midi_note for n in notes if n.hand == "R"]
    if left and right and max(left) > min(right):
        issues.append(
            Issue(
                "fingering_hand_crossing",
                "the left hand plays above the right hand",
            )
        )

    distinct_keys = len({n.midi_note for n in notes})
    if distinct_keys < vc.min_distinct_keys:
        issues.append(
            Issue(
                "too_few_distinct_keys",
                f"the melody only uses {distinct_keys} different keys "
                f"(minimum {vc.min_distinct_keys})",
            )
        )
    if vc.min_notes_per_hand > 0 and len(layout.hands()) == 2:
        for hand in layout.hands():
            played = sum(1 for n in notes if n.hand == hand)
            if played < vc.min_notes_per_hand:
                issues.append(
                    Issue(
                        "unused_hand",
                        f"the {layout.name} position has two hands but the "
                        f"{hand} hand plays {played} note(s), minimum "
                        f"{vc.min_notes_per_hand}",
                    )
                )

    same_finger = features.max_same_finger_run(notes)
    if same_finger > vc.max_consecutive_same_finger:
        issues.append(
            Issue(
                "finger_repetition_excessive",
                f"the same finger plays {same_finger} notes in a row "
                f"(limit {vc.max_consecutive_same_finger})",
            )
        )
    switches = features.hand_switch_count(notes)
    if switches > vc.max_hand_switches:
        issues.append(
            Issue(
                "hand_switch_excessive",
                f"{switches} hand changes (limit {vc.max_hand_switches})",
            )
        )
    alternations = features.max_alternation_run(notes)
    if alternations > vc.max_consecutive_alternations:
        issues.append(
            Issue(
                "alternation_excessive",
                f"{alternations} hand changes in a row "
                f"(limit {vc.max_consecutive_alternations})",
            )
        )
    return issues


def _check_melodic_motion(
    line: MelodyLine, cfg: GeneratorConfig
) -> List[Issue]:
    issues: List[Issue] = []
    vc = cfg.validation
    indices = line.indices
    deltas = features.step_intervals(indices)
    for position, delta in enumerate(deltas):
        if abs(delta) > vc.max_leap_steps:
            issues.append(
                Issue(
                    "leap_too_large",
                    f"interval {position}: {abs(delta)} scale steps "
                    f"(limit {vc.max_leap_steps})",
                )
            )
    run = features.max_consecutive_leaps(indices, cfg.melody.leap_threshold_steps)
    if run > vc.max_consecutive_leaps:
        issues.append(
            Issue(
                "consecutive_leaps",
                f"{run} leaps in a row (limit {vc.max_consecutive_leaps})",
            )
        )
    ratio = features.stepwise_ratio(indices)
    if ratio < vc.min_stepwise_ratio:
        issues.append(
            Issue(
                "leap_ratio_high",
                f"only {ratio:.0%} of moves are steps or repeats "
                f"(minimum {vc.min_stepwise_ratio:.0%})",
            )
        )
    span = max(indices) - min(indices)
    if span > cfg.melody.max_total_span_steps:
        issues.append(
            Issue(
                "range_span_excessive",
                f"melody spans {span} scale steps "
                f"(limit {cfg.melody.max_total_span_steps})",
            )
        )
    for phrase in line.phrases:
        phrase_span = max(phrase.indices) - min(phrase.indices)
        if phrase_span > cfg.melody.max_phrase_span_steps:
            issues.append(
                Issue(
                    "phrase_span_excessive",
                    f"phrase {phrase.position} spans {phrase_span} steps "
                    f"(limit {cfg.melody.max_phrase_span_steps})",
                )
            )
    if features.repeated_note_ratio(indices) > 0.35:
        issues.append(
            Issue(
                "repeated_note_excessive",
                "more than a third of the melody stands still",
            )
        )
    return issues


def _check_rhythm(
    notes: Sequence[NoteEvent],
    rests: Sequence[RestEvent],
    rhythm: RhythmPlan,
    cfg: GeneratorConfig,
) -> List[Issue]:
    issues: List[Issue] = []
    rc, vc = cfg.rhythm, cfg.validation
    durations = [n.duration_beats for n in notes]

    if any(d not in [float(x) for x in rc.note_beats] for d in durations):
        issues.append(
            Issue(
                "rhythm_illegal_duration",
                f"durations {sorted(set(durations))} are not all in "
                f"{list(rc.note_beats)}",
            )
        )
    if any(abs(n.onset_beat - round(n.onset_beat)) > 1e-9 for n in notes):
        issues.append(
            Issue("rhythm_off_grid", "an onset does not land on a whole beat")
        )

    distinct = len(set(durations))
    if distinct < vc.min_distinct_durations:
        issues.append(
            Issue(
                "rhythm_monotonous",
                f"only {distinct} distinct note length(s)",
            )
        )
    long_notes = [d for d in durations if d >= 2]
    if not long_notes:
        issues.append(
            Issue("no_long_note", "no 2-beat or 3-beat held note in the sequence")
        )
    elif len(long_notes) < rc.min_long_notes:
        issues.append(
            Issue(
                "too_few_long_notes",
                f"{len(long_notes)} held notes, minimum {rc.min_long_notes}",
            )
        )
    if features.long_note_ratio(notes) > rc.max_long_note_ratio:
        issues.append(
            Issue(
                "rhythm_too_complex",
                f"{features.long_note_ratio(notes):.0%} of notes are held "
                f"(limit {rc.max_long_note_ratio:.0%})",
            )
        )

    if len(rests) > rc.max_rests:
        issues.append(
            Issue("rhythm_too_complex", f"{len(rests)} rests (limit {rc.max_rests})")
        )
    if len(rests) < rc.min_rests:
        issues.append(
            Issue(
                "rhythm_monotonous",
                f"{len(rests)} rests (minimum {rc.min_rests})",
            )
        )
    for first, second in zip(rests, rests[1:]):
        if abs(first.end_time_sec - second.start_time_sec) < 1e-9:
            issues.append(
                Issue("rhythm_adjacent_rests", "two rests back to back")
            )
    for rest in rests:
        if rest.onset_beat <= 0:
            issues.append(Issue("rhythm_leading_rest", "the sequence starts on a rest"))
        if rest.onset_beat + rest.duration_beats >= rhythm.total_beats - 1e-9:
            issues.append(Issue("rhythm_trailing_rest", "the sequence ends on a rest"))
        if rest.duration_beats != rc.rest_beats:
            issues.append(
                Issue(
                    "rhythm_illegal_rest",
                    f"rest of {rest.duration_beats} beats, expected "
                    f"{rc.rest_beats}",
                )
            )
    return issues


def _check_structure(
    notes: Sequence[NoteEvent],
    line: MelodyLine,
    space: PitchSpace,
    cfg: GeneratorConfig,
) -> List[Issue]:
    """The "does this sound composed rather than random" group."""
    issues: List[Issue] = []
    vc = cfg.validation
    indices = line.indices

    repeats = features.motif_repeat_count(indices, length=2)
    parallels = features.parallel_phrase_pairs(line.phrases)
    if repeats < vc.min_motif_repeats and parallels == 0:
        issues.append(
            Issue(
                "no_phrase_structure",
                "no motif is ever repeated and no two phrases share a shape",
            )
        )

    available = len(set(space.pitch_classes))
    entropy = features.pitch_class_entropy([n.midi_note for n in notes], available)
    if entropy > vc.max_pitch_class_entropy:
        issues.append(
            Issue(
                "pitch_entropy_high",
                f"pitch use is {entropy:.2f} of maximum entropy - the melody "
                f"reads as random digits rather than a tune",
            )
        )

    if vc.require_tonic_ending:
        final = notes[-1]
        if final.midi_note % 12 != space.key.tonic_pc:
            issues.append(
                Issue(
                    "weak_cadence",
                    f"the melody ends on {final.note_name}, not on the "
                    f"{space.key.display} tonic",
                )
            )
        if len(indices) >= 2:
            approach = abs(indices[-1] - indices[-2])
            if approach > vc.max_cadence_approach_steps or approach == 0:
                issues.append(
                    Issue(
                        "weak_cadence",
                        f"the tonic is approached by {approach} scale steps; "
                        f"1-{vc.max_cadence_approach_steps} is expected",
                    )
                )
    return issues


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def validate(
    notes: Sequence[NoteEvent],
    rests: Sequence[RestEvent],
    rhythm: RhythmPlan,
    line: MelodyLine,
    space: PitchSpace,
    layout: Layout,
    cfg: GeneratorConfig,
) -> ValidationReport:
    issues: List[Issue] = []
    issues += _check_playability(notes, layout, cfg)
    issues += _check_fingering(notes, layout, cfg)
    issues += _check_melodic_motion(line, cfg)
    issues += _check_rhythm(notes, rests, rhythm, cfg)
    issues += _check_structure(notes, line, space, cfg)
    # De-duplicate while keeping order, so one root cause is reported once.
    unique: List[Issue] = []
    seen = set()
    for issue in issues:
        signature = (issue.code, issue.message)
        if signature not in seen:
            seen.add(signature)
            unique.append(issue)
    return ValidationReport(ok=not unique, issues=tuple(unique))
