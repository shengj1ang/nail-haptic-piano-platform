"""Match what was played to what was asked for, allowing slips.

A performance is scored by pairing each target note with the key press
that answered it. The obvious pairing - the n-th press answers the n-th
note - is wrong in a way that quietly ruins a trial: a participant who
misses one note and then plays the remaining fourteen perfectly is
scored as getting fourteen notes wrong, because every later press is
compared against the note before the one it was actually playing.

That is not hypothetical. In P01's first probe the participant dropped
the opening G4 and played the rest of the melody correctly; positional
pairing scored 9/15, and the 6 "errors" were an artefact of the pairing,
not of the performance.

So pairing is done by **global sequence alignment** (Needleman-Wunsch)
instead, which is allowed to leave a target unanswered (the participant
missed it) or a press unmatched (they played something extra) and to
resynchronise afterwards.

The scores below are chosen so that:

* a wrong key in the right place is a SUBSTITUTION, not a deletion
  followed by an insertion - the participant did answer that note, just
  wrongly, and `MISMATCH > 2 * GAP` is what expresses that;
* a genuine missed or extra note costs one gap and lets everything after
  it realign, which pays for itself within a couple of notes.

Pitch is the only thing aligned on. Timing is deliberately not part of
the score: a participant whose rhythm has drifted has still played the
right notes, and letting a large timing error break the pairing would
reintroduce the problem this module exists to fix.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

MATCH = 2.0
MISMATCH = -1.0
GAP = -2.0


@dataclass(frozen=True)
class Pair:
    """One step of an alignment.

    Exactly one of the two indices may be None: a target with no press is
    a note the participant never played, a press with no target is one
    they played extra.
    """

    target_index: Optional[int]
    played_index: Optional[int]

    @property
    def is_missed(self) -> bool:
        return self.played_index is None

    @property
    def is_extra(self) -> bool:
        return self.target_index is None


def align(targets: Sequence[int], played: Sequence[int]) -> List[Pair]:
    """Align two note sequences by pitch. Returns one Pair per step, in
    order, covering every target and every press exactly once."""
    n, m = len(targets), len(played)
    # score[i][j] = best score for the first i targets against the first j presses
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = score[i - 1][0] + GAP
    for j in range(1, m + 1):
        score[0][j] = score[0][j - 1] + GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diagonal = score[i - 1][j - 1] + (MATCH if targets[i - 1] == played[j - 1] else MISMATCH)
            score[i][j] = max(diagonal, score[i - 1][j] + GAP, score[i][j - 1] + GAP)

    pairs: List[Pair] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            diagonal = score[i - 1][j - 1] + (MATCH if targets[i - 1] == played[j - 1] else MISMATCH)
            if score[i][j] == diagonal:
                pairs.append(Pair(i - 1, j - 1))
                i, j = i - 1, j - 1
                continue
        if i > 0 and score[i][j] == score[i - 1][j] + GAP:
            pairs.append(Pair(i - 1, None))  # target with nothing played for it
            i -= 1
            continue
        pairs.append(Pair(None, j - 1))  # a press with no target
        j -= 1
    pairs.reverse()
    return pairs


def match_targets(targets: Sequence[int], played: Sequence[int]) -> Tuple[List[Optional[int]], List[int]]:
    """The pairing in the shape the scoring wants: for each target, the
    index of the press that answered it (or None), plus the indices of
    presses that answered no target."""
    answered: List[Optional[int]] = [None] * len(targets)
    extra: List[int] = []
    for pair in align(targets, played):
        if pair.target_index is not None and pair.played_index is not None:
            answered[pair.target_index] = pair.played_index
        elif pair.target_index is None and pair.played_index is not None:
            extra.append(pair.played_index)
    return answered, extra


def describe(targets: Sequence[int], played: Sequence[int]) -> dict:
    """Counts a reader can sanity-check an alignment against."""
    answered, extra = match_targets(targets, played)
    correct = sum(
        1 for i, j in enumerate(answered) if j is not None and targets[i] == played[j]
    )
    wrong = sum(
        1 for i, j in enumerate(answered) if j is not None and targets[i] != played[j]
    )
    return {
        "targets": len(targets),
        "presses": len(played),
        "correct": correct,
        "wrong_key": wrong,
        "missed": sum(1 for j in answered if j is None),
        "extra": len(extra),
    }
