"""Reusable, GUI-free pieces of the student practice quiz (see
app/gui/cue_window.py, student_quiz.py).

A quiz steps a student through a previously recorded song's fingering
sequence (data/music/<song>/fingering.json) one note at a time: light the
target key's LED (if connected) and show a "press this finger" cue - see
CueOutput below. The on-screen implementation lives in
app.gui.cue_window.ScreenCueOutput; swapping the cue medium for a
vibration-motor one later only means writing a new CueOutput subclass, not
touching quiz-running logic. The runner then waits for either a MIDI
note-on or a timeout before moving to the next note.

Like a teacher's recording (see app.music_recording), the whole session's
video + MIDI are captured throughout and saved under
data/quiz/<quiz_name>/raw/, and afterward app.offline.analyze_recording is
run once over the confirmed keypresses to work out which finger the
student actually used. Each note's target vs actual key/finger and timing
error ends up in results.json; the three headline numbers (note accuracy,
mean timing error, finger accuracy) are computed by summarize() and saved
to meta.json.
"""

import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .finger_matching import FINGER_PROBABILITY_THRESHOLD
from .keyboard.midi_mapping import note_name
from .music_recording import MUSIC_DATA_DIR, FINGERING_FILENAME, sanitize_song_name, song_dir

QUIZ_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "quiz"

RAW_VIDEO_FILENAME = "performance.mp4"
RAW_MIDI_FILENAME = "midi_raw.json"
RAW_NOTES_FILENAME = "notes.json"
RAW_SYNC_FILENAME = "sync.json"
RAW_HANDS_FILENAME = "hands.json"  # per-frame hand landmarks, saved by analyze_recording
RESULTS_FILENAME = "results.json"
META_FILENAME = "meta.json"
REVIEW_VIDEO_FILENAME = "review.mp4"

# Same filesystem-safety rules as a recorded song's title - different
# context, identical requirement (turn free text into a safe folder name).
sanitize_quiz_name = sanitize_song_name


def quiz_dir(quiz_name: str, data_dir: Path = QUIZ_DATA_DIR) -> Path:
    return data_dir / quiz_name


def quiz_raw_dir(quiz_name: str, data_dir: Path = QUIZ_DATA_DIR) -> Path:
    return quiz_dir(quiz_name, data_dir) / "raw"


def list_quizzes(data_dir: Path = QUIZ_DATA_DIR) -> List[str]:
    if not data_dir.exists():
        return []
    return sorted(p.name for p in data_dir.iterdir() if (p / META_FILENAME).exists())


@dataclass
class QuizTarget:
    index: int
    note: int
    note_name: str
    key_id: Optional[int]
    finger: Optional[str]


def load_quiz_targets(song_name: str, data_dir: Path = MUSIC_DATA_DIR) -> List[QuizTarget]:
    """The note/finger sequence to practice, straight from a song's
    fingering.json (see app.music_recording) - a real recording
    (data/music/) or a generated experimental sequence (data/sequence/,
    see app.sequence_generator), depending on data_dir."""
    path = song_dir(song_name, data_dir) / FINGERING_FILENAME
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    return [
        QuizTarget(
            index=i,
            note=int(item["note"]),
            note_name=item.get("note_name") or note_name(int(item["note"])),
            key_id=item.get("key_id"),
            finger=item.get("finger"),
        )
        for i, item in enumerate(entries)
    ]


class CueOutput:
    """Abstract "tell the student what to do right now" cue. The quiz
    runner only ever calls these methods - never touches Qt widgets or
    motors directly - so a future vibration-motor cue is a drop-in
    replacement for ScreenCueOutput, nothing else in the quiz has to
    change."""

    def show_target(self, note: int, finger: Optional[str]) -> None:
        raise NotImplementedError

    def show_message(self, text: str) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


@dataclass
class QuizResult:
    index: int
    target_note: int
    target_note_name: str
    target_key_id: Optional[int]
    target_finger: Optional[str]
    cue_onset_time: float  # absolute wall-clock time.time(), same clock as the raw MIDI log
    timed_out: bool
    actual_note: Optional[int] = None
    actual_key_id: Optional[int] = None
    keypress_time: Optional[float] = None
    timing_error_s: Optional[float] = None  # keypress_time - cue_onset_time
    note_correct: bool = False
    actual_finger: Optional[str] = None  # most probable finger, filled in after analyze_recording
    finger_correct: Optional[bool] = None  # target finger's probability cleared the threshold (see app.finger_matching.is_finger_correct)
    # Full softmax distribution over every visible fingertip, and the target
    # finger's share of it - kept so a past quiz can be re-judged under a
    # different FINGER_PROBABILITY_THRESHOLD without re-running the video.
    finger_probabilities: Optional[Dict[str, float]] = None
    target_finger_probability: Optional[float] = None
    # Pixel position of the detected fingertip at the keypress moment, so the
    # review video (app.review_video) can mark it without re-running MediaPipe.
    actual_finger_point: Optional[List[int]] = None
    # "valid" | "invalid_carryover". Only ever set by a human (quiz detail
    # window) after reviewing a suspected carry-over response - see
    # suspected_carryover(). Invalid events are excluded from summarize().
    validity: str = "valid"


def save_quiz_results(results: List[QuizResult], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, indent=2)


def load_quiz_results(path: Path) -> List[QuizResult]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [QuizResult(**item) for item in data]


@dataclass
class QuizMeta:
    quiz_name: str
    song_name: str
    keyboard_profile_name: str
    port_name: Optional[str]
    created_at: float  # absolute wall-clock time.time() timestamp
    timeout_s: float
    note_count: int
    hits: int
    misses: int
    note_accuracy: float
    mean_timing_error_s: Optional[float]
    finger_accuracy: Optional[float]
    # How the target/finger was cued - "visual" (see student_quiz.py) today,
    # a vibration-motor guidance_type could be added later without changing
    # this schema. analyzed tracks whether the (separate, reusable -
    # app/gui/quiz_analysis_window.py) finger-matching pass has run yet;
    # note_accuracy/timing error don't need it, only finger_accuracy does.
    guidance_type: str = "visual"
    analyzed: bool = True

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "QuizMeta":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))


# Every finger label a quiz target/detection can carry, table order.
FINGER_LABELS = ["L1", "L2", "L3", "L4", "L5", "R1", "R2", "R3", "R4", "R5"]

# Alternative finger-probability thresholds for the report's sensitivity
# analysis (method.tex "Finger-Matching Validation and Sensitivity
# Analysis") - the primary theta = 0.40 lives in app.finger_matching.
SENSITIVITY_THRESHOLDS = [0.30, 0.35, 0.45, 0.50]

# An event whose target-finger probability lands within this margin of the
# decision threshold is "borderline" - the finger verdict could flip under
# a slightly different theta, so these are the events most worth checking
# in the review video during the manual audit.
BORDERLINE_MARGIN = 0.05

# A matched response this soon after cue onset is faster than a planned
# reaction - the movement almost certainly started before the cue (typically
# the tail of the previous event's presses carrying over the inter-trial
# gap). Such events are flagged suspected_carryover for manual review; they
# are never auto-labelled anticipation, and only a human can invalidate one
# (QuizResult.validity = VALIDITY_INVALID_CARRYOVER).
CARRYOVER_RT_THRESHOLD_S = 0.1

# QuizResult.validity values. Invalid events were manually confirmed as
# carry-over from the previous event and are excluded from every summary
# statistic (RT and accuracy alike); they stay in results.json for audit.
VALIDITY_VALID = "valid"
VALIDITY_INVALID_CARRYOVER = "invalid_carryover"

# Unmatched raw presses within this window of a matched keypress are
# near-simultaneous multi-key presses (the scorer keeps only the earliest);
# anything farther is an inter-trial press in the gap between events.
DOUBLE_HIT_WINDOW_S = 0.06


def _mean(values: List[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


def suspected_carryover(r: QuizResult) -> bool:
    """This event's matched response was implausibly fast (RT below
    CARRYOVER_RT_THRESHOLD_S) and hasn't been ruled on yet - it needs a
    manual look (review video) to decide whether it was really the tail of
    the previous event's presses. Confirmed ones get validity =
    VALIDITY_INVALID_CARRYOVER and stop being "suspected"."""
    return (
        r.validity == VALIDITY_VALID
        and r.timing_error_s is not None
        and r.timing_error_s < CARRYOVER_RT_THRESHOLD_S
    )


def summarize(results: List[QuizResult]) -> Dict[str, object]:
    """Per-trial outcome measures, following the report's "Outcome
    Measures and Pilot Analysis Plan" (final_report_2026/method/method.tex).

    Per event t: K_t = key correct, F_t = finger correct under the
    theta = 0.40 threshold rule (see app.finger_matching.is_finger_correct;
    unresolved counts as incorrect), A_t = K_t and F_t in the same event.

    The three finger-accuracy views:
      - fa_main   = sum(A) / total          (the report's primary measure)
      - fa_key    = sum(A) / sum(K)         (finger correct, given correct key)
      - fa_finger = sum(A) / sum(F)         (key correct, given correct finger)

    finger_accuracy is kept as an alias of fa_main - it's what
    QuizMeta.finger_accuracy stores.

    Cross-trial aggregation (per participant / condition / all data) is
    deliberately not here - single-trial numbers only.

    Events manually invalidated as carry-over (validity =
    VALIDITY_INVALID_CARRYOVER) are excluded from every statistic below -
    RT and accuracy alike - and only reported as excluded_carryover.
    """
    excluded_carryover = sum(1 for r in results if r.validity == VALIDITY_INVALID_CARRYOVER)
    results = [r for r in results if r.validity != VALIDITY_INVALID_CARRYOVER]
    total = len(results)
    hits = sum(1 for r in results if r.note_correct)
    misses = sum(1 for r in results if r.timed_out)
    responded = [r for r in results if not r.timed_out]

    def a(r: QuizResult) -> bool:  # A_t: complete action correct
        return r.note_correct and bool(r.finger_correct)

    a_count = sum(1 for r in results if a(r))
    f_count = sum(1 for r in results if r.finger_correct)

    timing_errors = [r.timing_error_s for r in results if r.timing_error_s is not None]

    # Error breakdown (counts; false starts need the raw MIDI log and are
    # left to the offline scripts).
    wrong_key = sum(1 for r in responded if not r.note_correct)
    key_ok_wrong_finger = sum(1 for r in results if r.note_correct and not r.finger_correct)

    # Detection-quality rates, over responded events only (a timeout has
    # no keypress moment to detect a finger at). Unresolved = no hand/
    # fingertip visible; ambiguous = resolved but no single fingertip held
    # a majority of the softmax mass.
    unresolved = sum(1 for r in responded if r.actual_finger is None)
    with_probs = [r for r in responded if r.finger_probabilities]
    ambiguous = sum(1 for r in with_probs if max(r.finger_probabilities.values()) < 0.5)

    # Hand-transition effects: event t is a "switch" when its target hand
    # differs from event t-1's. First event has no predecessor.
    same_hand: List[QuizResult] = []
    hand_switch: List[QuizResult] = []
    for prev, cur in zip(results, results[1:]):
        if prev.target_finger and cur.target_finger:
            (hand_switch if cur.target_finger[0] != prev.target_finger[0] else same_hand).append(cur)

    def transition_stats(events: List[QuizResult]) -> Dict[str, Optional[float]]:
        return {
            "n": len(events),
            "fa_main": (sum(1 for r in events if a(r)) / len(events)) if events else None,
            "mean_timing_error_s": _mean([r.timing_error_s for r in events if r.timing_error_s is not None]),
        }

    # Per-finger profiles, keyed by the event's *target* finger.
    finger_stats: Dict[str, Dict[str, Optional[float]]] = {}
    for finger in FINGER_LABELS:
        events = [r for r in results if r.target_finger == finger]
        finger_stats[finger] = {
            "n": len(events),
            "fa_main": (sum(1 for r in events if a(r)) / len(events)) if events else None,
            "mean_timing_error_s": _mean([r.timing_error_s for r in events if r.timing_error_s is not None]),
        }

    # Threshold sensitivity: re-judge F_t from the stored target-finger
    # probability under alternative thetas - no video pass needed.
    fa_theta: Dict[str, Optional[float]] = {}
    for theta in SENSITIVITY_THRESHOLDS:
        a_theta = sum(
            1
            for r in results
            if r.note_correct and r.target_finger_probability is not None and r.target_finger_probability >= theta
        )
        fa_theta[f"{theta:.2f}"] = a_theta / total if total else None

    # Timing distribution beyond the mean - spread and tail.
    sorted_te = sorted(timing_errors)
    timing_stats = {
        "median_s": statistics.median(sorted_te) if sorted_te else None,
        "sd_s": statistics.stdev(sorted_te) if len(sorted_te) >= 2 else None,
        "min_s": sorted_te[0] if sorted_te else None,
        "max_s": sorted_te[-1] if sorted_te else None,
        "p95_s": sorted_te[min(len(sorted_te) - 1, int(0.95 * len(sorted_te)))] if sorted_te else None,
    }

    # Within-trial trend: least-squares slope of RT over event index
    # (learning/fatigue inside one trial), plus a first-half vs
    # second-half split of FA and RT.
    xy = [(r.index, r.timing_error_s) for r in results if r.timing_error_s is not None]
    rt_slope = None
    if len(xy) >= 2:
        mean_x = _mean([x for x, _ in xy])
        mean_y = _mean([y for _, y in xy])
        denom = sum((x - mean_x) ** 2 for x, _ in xy)
        rt_slope = sum((x - mean_x) * (y - mean_y) for x, y in xy) / denom if denom else None

    def half_stats(events: List[QuizResult]) -> Dict[str, Optional[float]]:
        return {
            "fa_main": (sum(1 for r in events if a(r)) / len(events)) if events else None,
            "mean_timing_error_s": _mean([r.timing_error_s for r in events if r.timing_error_s is not None]),
        }

    first_half = half_stats(results[: total // 2])
    second_half = half_stats(results[total // 2:])

    # Wrong-key spatial profile: how far off (in semitones) and to which
    # side - near-misses and wild presses are different kinds of error.
    wrong_events = [r for r in responded if not r.note_correct and r.actual_note is not None]
    wrong_key_stats = {
        "mean_abs_semitones": _mean([abs(r.actual_note - r.target_note) for r in wrong_events]),
        "below": sum(1 for r in wrong_events if r.actual_note < r.target_note),
        "above": sum(1 for r in wrong_events if r.actual_note > r.target_note),
    }

    # Detection-confidence profile. Borderline events are the manual-audit
    # priority: their finger verdict sits within BORDERLINE_MARGIN of the
    # threshold and could flip under a slightly different theta.
    target_probs = [r.target_finger_probability for r in results if r.target_finger_probability is not None]
    margins = [
        sorted(r.finger_probabilities.values(), reverse=True)
        for r in with_probs
        if len(r.finger_probabilities) >= 2
    ]
    confidence = {
        "mean_target_prob": _mean(target_probs),
        "mean_top_margin": _mean([m[0] - m[1] for m in margins]),
        "borderline": sum(1 for p in target_probs if abs(p - FINGER_PROBABILITY_THRESHOLD) <= BORDERLINE_MARGIN),
    }

    # Implausibly fast matched responses still awaiting a manual verdict -
    # see suspected_carryover(). Never auto-counted as anticipation.
    suspected = sum(1 for r in results if suspected_carryover(r))

    # Target-vs-detected finger confusion counts (None = unresolved), for
    # the per-trial detail view's confusion matrix.
    confusion: Dict[str, Dict[Optional[str], int]] = {}
    for r in responded:
        if r.target_finger is None:
            continue
        row = confusion.setdefault(r.target_finger, {})
        row[r.actual_finger] = row.get(r.actual_finger, 0) + 1

    fa_main = a_count / total if total else None
    return {
        # Headline (QuizMeta) fields
        "note_accuracy": hits / total if total else 0.0,
        "hits": hits,
        "misses": misses,
        "mean_timing_error_s": _mean(timing_errors),
        "finger_accuracy": fa_main,
        # The three finger-accuracy views
        "fa_main": fa_main,
        "fa_key": a_count / hits if hits else None,
        "fa_finger": a_count / f_count if f_count else None,
        # Reaction time stratified by correctness
        "rt_correct_key_s": _mean([r.timing_error_s for r in results if r.note_correct and r.timing_error_s is not None]),
        "rt_complete_s": _mean([r.timing_error_s for r in results if a(r) and r.timing_error_s is not None]),
        # Error breakdown
        "timeout_rate": misses / total if total else None,
        "wrong_key": wrong_key,
        "key_ok_wrong_finger": key_ok_wrong_finger,
        # Finger-detection quality
        "unresolved_rate": unresolved / len(responded) if responded else None,
        "ambiguous_rate": ambiguous / len(with_probs) if with_probs else None,
        # Hand transitions / per-finger profiles / threshold sensitivity
        "same_hand": transition_stats(same_hand),
        "hand_switch": transition_stats(hand_switch),
        "finger_stats": finger_stats,
        "fa_theta": fa_theta,
        # Distribution / trend / audit extras (mainly for the detail view)
        "timing_stats": timing_stats,
        "rt_slope_s_per_event": rt_slope,
        "first_half": first_half,
        "second_half": second_half,
        "wrong_key_stats": wrong_key_stats,
        "confidence": confidence,
        "suspected_carryover": suspected,
        "excluded_carryover": excluded_carryover,
        "confusion": confusion,
        "manual_corrections": sum(1 for r in responded if finger_manually_corrected(r)),
    }


def count_extra_presses(results: List[QuizResult], midi_raw_path: Path) -> Optional[Dict[str, int]]:
    """QC/debug info only - never part of the main outcome measures.

    Extra presses: note_on events in the raw MIDI log (midi_raw.json) that
    were never matched to a cue event. The quiz runner only records the
    press it matched to each cue, so these are only visible here. They are
    deliberately NOT called false starts or anticipation: pilot data shows
    almost all of them are either near-simultaneous multi-key presses
    (the scorer keeps the earliest key - "double_hits", within
    DOUBLE_HIT_WINDOW_S of a matched keypress) or stray presses in the gap
    between events ("inter_trial_presses"), i.e. the tail of the previous
    response, not an early reaction to the next cue. None if the raw log
    is missing."""
    try:
        with open(midi_raw_path, encoding="utf-8") as f:
            events = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    note_ons = [e for e in events if e.get("type") == "note_on"]
    # Consume the note_on nearest each matched keypress (same note, within
    # 5 ms - both timestamps come from the same MIDI clock, so they agree
    # to float precision; the tolerance just absorbs rounding).
    unmatched = list(note_ons)
    matched_times: List[float] = []
    for r in results:
        if r.keypress_time is None or r.actual_note is None:
            continue
        matched_times.append(r.keypress_time)
        best = None
        for e in unmatched:
            if e["note"] == r.actual_note and abs(e["abs_time"] - r.keypress_time) <= 0.005:
                if best is None or abs(e["abs_time"] - r.keypress_time) < abs(best["abs_time"] - r.keypress_time):
                    best = e
        if best is not None:
            unmatched.remove(best)

    double_hits = sum(
        1
        for e in unmatched
        if any(abs(e["abs_time"] - t) <= DOUBLE_HIT_WINDOW_S for t in matched_times)
    )
    return {
        "note_on_total": len(note_ons),
        "extra_presses": len(unmatched),
        "double_hits": double_hits,
        "inter_trial_presses": len(unmatched) - double_hits,
    }


def finger_manually_corrected(r: QuizResult) -> bool:
    """Was this event's actual_finger hand-corrected (in the per-event
    review window)? The automatic pipeline always sets actual_finger to
    the softmax argmax, and manual correction deliberately leaves the
    stored probabilities untouched - so a mismatch between the two is the
    audit trail. An originally-unresolved event (no distribution) that now
    carries a finger is likewise a manual edit."""
    if r.finger_probabilities:
        detected = max(r.finger_probabilities, key=r.finger_probabilities.get)
        return r.actual_finger != detected
    return r.actual_finger is not None


def full_summary(quiz_name: str, results: List[QuizResult]) -> Dict[str, object]:
    """summarize() plus the raw-MIDI QC extras (unmatched presses, under
    the "extra" key; None when midi_raw.json is missing) - everything the
    analysis table or the per-trial detail view needs, computed fresh
    from the disk-backed per-event data."""
    s = summarize(results)
    s["extra"] = count_extra_presses(results, quiz_raw_dir(quiz_name) / RAW_MIDI_FILENAME)
    return s
