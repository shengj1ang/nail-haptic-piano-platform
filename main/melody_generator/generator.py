"""Orchestration: build candidates, reject, score, keep the best one.

The pipeline for a single candidate is four independent stages, in order::

    melody.generate_melody_line   pitch, as scale-step indices
    fingering.assign_fingering    -> MIDI note + hand + finger
    rhythm.generate_rhythm        -> beat grid, held notes, rests
    timing.build_events           -> note-on/note-off seconds, velocity

then :mod:`validation` rejects it or :mod:`scoring` ranks it. Because every
random draw comes from one seeded :class:`random.Random`, the same seed always
produces byte-identical output.
"""

import random
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import GeneratorConfig, default_phrase_plan
from .fingering import FingeringError, FingeringPlan, assign_fingering
from .layouts import Layout, get_layout
from .melody import (
    MelodyGenerationError,
    MelodyLine,
    PitchSpace,
    build_pitch_space,
    generate_melody_line,
)
from .rhythm import RhythmGenerationError, RhythmPlan, generate_rhythm
from .scoring import Scores, score_candidate
from .theory import Key, get_key
from .timing import NoteEvent, RestEvent, build_events
from .validation import ValidationReport, validate


class GenerationFailed(RuntimeError):
    """No candidate survived the rules within the attempt budget."""


@dataclass(frozen=True)
class GenerationStats:
    attempts: int
    accepted: int
    rejections: Dict[str, int]
    #: Which of the ranked candidates was taken (0 = the highest scoring)
    #: and how many were in contention. Recorded so a melody says how it
    #: was chosen and not just what it is: the seed picks between the
    #: best few (see SELECTION_POOL), so the choice is part of what the
    #: seed decides and a reader should be able to see which it made.
    selected_rank: int = 0
    selected_from: int = 1

    def to_dict(self) -> Dict[str, object]:
        return {
            "attempts": self.attempts,
            "accepted_candidates": self.accepted,
            "selected_rank": self.selected_rank,
            "selected_from": self.selected_from,
            "rejection_counts": dict(
                sorted(self.rejections.items(), key=lambda kv: -kv[1])
            ),
        }


@dataclass(frozen=True)
class MelodySequence:
    """A finished, validated melody plus everything used to produce it."""

    name: str
    seed: int
    created_at_epoch_s: float
    config: GeneratorConfig
    key: Key
    layout: Layout
    phrase_plan: Tuple[int, ...]
    line: MelodyLine
    fingering: FingeringPlan
    rhythm: RhythmPlan
    notes: Tuple[NoteEvent, ...]
    rests: Tuple[RestEvent, ...]
    scores: Scores
    validation: ValidationReport
    stats: GenerationStats

    @property
    def total_beats(self) -> float:
        return self.rhythm.total_beats

    @property
    def beat_seconds(self) -> float:
        return 60.0 / self.config.timing.bpm

    @property
    def total_seconds(self) -> float:
        return self.total_beats * self.beat_seconds

    @property
    def selection_score(self) -> float:
        return _selection_score(self.scores)

    def fingers_used(self) -> Tuple[str, ...]:
        seen = []
        for note in self.notes:
            if note.finger not in seen:
                seen.append(note.finger)
        return tuple(seen)


def _selection_score(scores: Scores) -> float:
    """Rank candidates on musicality, with a mild penalty for difficulty."""
    return scores.musicality - 0.3 * scores.difficulty


#: How many of the best-ranked candidates the seed chooses between.
#:
#: Taking only the single highest (a plain argmax) threw away nearly all
#: the variety the search had already produced: a 40-candidate pool holds
#: about 39 DIFFERENT melodies, and keeping one meant whichever tune was
#: both easy to sample and high-scoring won over and over. Measured over
#: 300 seeds, one melody came up in 9.3% of runs and the top five in 20%,
#: so two seeds returning the same tune was ordinary rather than unlucky.
#:
#: Every candidate here has already passed validation, max_difficulty and
#: min_musicality - they are all melodies this generator calls acceptable,
#: and insisting on the highest score among them was ranking by the third
#: decimal place. Choosing between the best five instead takes the most
#: common melody from 9.3% of runs to 3.3% and the distinct count over
#: 300 seeds from 158 to 197, for 0.006 of mean musicality.
SELECTION_POOL = 5


@dataclass(frozen=True)
class _Candidate:
    line: MelodyLine
    fingering: FingeringPlan
    rhythm: RhythmPlan
    notes: Tuple[NoteEvent, ...]
    rests: Tuple[RestEvent, ...]
    scores: Scores
    validation: ValidationReport


def _resolve_plan(cfg: GeneratorConfig) -> Tuple[int, ...]:
    plan = tuple(cfg.melody.phrase_plan) or default_phrase_plan(cfg.melody.note_count)
    if sum(plan) != cfg.melody.note_count:
        raise ValueError(
            f"phrase plan {plan} sums to {sum(plan)}, but note_count is "
            f"{cfg.melody.note_count}"
        )
    if min(plan) < 2:
        raise ValueError(f"phrase plan {plan} contains a phrase shorter than two notes")
    return plan


def _phrase_start_positions(plan: Sequence[int]) -> Tuple[int, ...]:
    starts = []
    running = 0
    for length in plan:
        starts.append(running)
        running += length
    return tuple(starts)


def _build_candidate(
    space: PitchSpace,
    layout: Layout,
    plan: Sequence[int],
    cfg: GeneratorConfig,
    rng: random.Random,
) -> _Candidate:
    line = generate_melody_line(space, plan, cfg.melody, rng)
    fingering = assign_fingering(line, layout, cfg.validation, rng)
    rhythm = generate_rhythm(plan, cfg.rhythm, rng)
    degrees = [space.degree(i) for i in line.indices]
    notes, rests = build_events(
        placements=fingering.placements,
        rhythm=rhythm,
        phrase_of_note=line.phrase_of_note(),
        scale_degrees=degrees,
        phrase_start_positions=_phrase_start_positions(plan),
        cfg=cfg.timing,
    )
    report = validate(notes, rests, rhythm, line, space, layout, cfg)
    scores = score_candidate(notes, rests, rhythm, line, space, plan, cfg)
    return _Candidate(
        line=line,
        fingering=fingering,
        rhythm=rhythm,
        notes=notes,
        rests=rests,
        scores=scores,
        validation=report,
    )


def generate_sequence(
    seed: int,
    cfg: Optional[GeneratorConfig] = None,
    name: Optional[str] = None,
) -> MelodySequence:
    """Generate one validated melody. The same seed always gives the same tune."""
    cfg = cfg or GeneratorConfig()
    key = get_key(cfg.melody.key)
    layout = get_layout(cfg.melody.layout)
    space = build_pitch_space(layout, key)
    plan = _resolve_plan(cfg)

    master = random.Random(seed)
    rejections: Counter = Counter()
    accepted: List[_Candidate] = []
    attempts = 0

    for attempts in range(1, cfg.scoring.candidate_attempts + 1):
        rng = random.Random(master.getrandbits(64))
        try:
            candidate = _build_candidate(space, layout, plan, cfg, rng)
        except (MelodyGenerationError, RhythmGenerationError, FingeringError) as error:
            rejections[f"build:{error}"] += 1
            continue
        if not candidate.validation.ok:
            for code in set(candidate.validation.codes()):
                rejections[code] += 1
            continue
        if candidate.scores.difficulty > cfg.scoring.max_difficulty:
            rejections["difficulty_too_high"] += 1
            continue
        if candidate.scores.musicality < cfg.scoring.min_musicality:
            rejections["musicality_too_low"] += 1
            continue
        accepted.append(candidate)
        if len(accepted) >= cfg.scoring.candidate_pool:
            break

    if not accepted:
        top = ", ".join(
            f"{code} x{count}" for code, count in rejections.most_common(6)
        )
        raise GenerationFailed(
            f"no candidate passed in {attempts} attempts; most common "
            f"rejections: {top or 'none recorded'}"
        )

    # Rank, then let the seed choose between the best few rather than
    # always taking the top one - see SELECTION_POOL for why, and for the
    # measurements. The choice is drawn from `master`, the same stream the
    # candidates came from, so a seed still determines its melody
    # completely: the same seed gives the same tune, as it always did.
    ranked = sorted(accepted, key=lambda c: -_selection_score(c.scores))
    top_k = min(SELECTION_POOL, len(ranked))
    rank = random.Random(master.getrandbits(64)).randrange(top_k)
    best = ranked[rank]
    return MelodySequence(
        name=name or f"melody_seed{seed}",
        seed=seed,
        created_at_epoch_s=time.time(),
        config=cfg,
        key=key,
        layout=layout,
        phrase_plan=tuple(plan),
        line=best.line,
        fingering=best.fingering,
        rhythm=best.rhythm,
        notes=best.notes,
        rests=best.rests,
        scores=best.scores,
        validation=best.validation,
        stats=GenerationStats(
            attempts=attempts,
            accepted=len(accepted),
            rejections=dict(rejections),
            selected_rank=rank,
            selected_from=top_k,
        ),
    )


def generate_and_export(
    seed: int,
    out_dir: Path,
    cfg: Optional[GeneratorConfig] = None,
    name: Optional[str] = None,
    split_hand_channels: bool = False,
) -> Tuple[MelodySequence, Dict[str, Path]]:
    """Generate one melody and write the .mid / .json / .csv / .txt files."""
    from .export import export_sequence  # local import keeps the layers apart

    sequence = generate_sequence(seed, cfg=cfg, name=name)
    paths = export_sequence(
        sequence, Path(out_dir), split_hand_channels=split_hand_channels
    )
    return sequence, paths
