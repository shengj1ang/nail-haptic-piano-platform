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

from .keyboard.midi_mapping import note_name
from .music_recording import MUSIC_DATA_DIR, FINGERING_FILENAME, sanitize_song_name, song_dir

QUIZ_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "quiz"

RAW_VIDEO_FILENAME = "performance.mp4"
RAW_MIDI_FILENAME = "midi_raw.json"
RAW_NOTES_FILENAME = "notes.json"
RAW_SYNC_FILENAME = "sync.json"
RESULTS_FILENAME = "results.json"
META_FILENAME = "meta.json"

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
    with open(path) as f:
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
    cue_onset_time: float  # seconds, same relative clock as the raw MIDI log
    timed_out: bool
    actual_note: Optional[int] = None
    actual_key_id: Optional[int] = None
    keypress_time: Optional[float] = None
    timing_error_s: Optional[float] = None  # keypress_time - cue_onset_time
    note_correct: bool = False
    actual_finger: Optional[str] = None  # filled in after analyze_recording
    finger_correct: Optional[bool] = None


def save_quiz_results(results: List[QuizResult], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)


def load_quiz_results(path: Path) -> List[QuizResult]:
    with open(path) as f:
        data = json.load(f)
    return [QuizResult(**item) for item in data]


@dataclass
class QuizMeta:
    quiz_name: str
    song_name: str
    keyboard_profile_name: str
    port_name: Optional[str]
    created_at: str
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
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "QuizMeta":
        with open(path) as f:
            return cls(**json.load(f))


def summarize(results: List[QuizResult]) -> Dict[str, Optional[float]]:
    """The three headline numbers: Note Accuracy (target key vs actual
    key), mean Timing Error (keypress time - cue onset time), and Finger
    Accuracy (target finger vs detected finger) - the last one only over
    attempts where both are actually known."""
    total = len(results)
    hits = sum(1 for r in results if r.note_correct)
    misses = sum(1 for r in results if r.timed_out)

    timing_errors = [r.timing_error_s for r in results if r.timing_error_s is not None]
    mean_timing_error_s = statistics.mean(timing_errors) if timing_errors else None

    finger_checks = [r for r in results if r.target_finger is not None and r.actual_finger is not None]
    finger_accuracy = (
        sum(1 for r in finger_checks if r.finger_correct) / len(finger_checks) if finger_checks else None
    )

    return {
        "note_accuracy": hits / total if total else 0.0,
        "hits": hits,
        "misses": misses,
        "mean_timing_error_s": mean_timing_error_s,
        "finger_accuracy": finger_accuracy,
    }
