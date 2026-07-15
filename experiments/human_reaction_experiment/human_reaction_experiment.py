import os
import time
import sqlite3
import random
import queue
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

from pynput import keyboard
from controller import VibratorController


# ==========================================
# Experiment configuration
# ==========================================

DB_PATH = "reaction_experiment.db"

LRA_INDEX = 11
ERM_INDEX = 10

AMP = 64
VIBRATION_DURATION_SECONDS = 0.20

INTER_TRIAL_INTERVAL_MIN = 2.0
INTER_TRIAL_INTERVAL_MAX = 5.0

VALID_RESPONSE_WINDOW_SECONDS = 2.0
MISS_WINDOW_SECONDS = 5.0

TRIALS_PER_BLOCK = 15
RESPONSE_KEY = keyboard.Key.space


# ==========================================
# Data model
# ==========================================

@dataclass
class TrialResult:
    participant_id: str
    actuator_type: str
    trial_index: int
    vibration_onset: float
    response_time: Optional[float]
    reaction_time_ms: Optional[float]
    is_valid: int
    is_miss: int
    false_start_count: int
    iti_seconds: float


# ==========================================
# Database helpers
# ==========================================

def init_database(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            participant_id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id TEXT NOT NULL,
            actuator_type TEXT NOT NULL,
            trial_index INTEGER NOT NULL,
            vibration_onset REAL NOT NULL,
            response_time REAL,
            reaction_time_ms REAL,
            is_valid INTEGER NOT NULL,
            is_miss INTEGER NOT NULL,
            false_start_count INTEGER NOT NULL,
            iti_seconds REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subjective_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id TEXT NOT NULL,
            actuator_type TEXT NOT NULL,
            clarity INTEGER NOT NULL,
            comfort INTEGER NOT NULL,
            responsiveness INTEGER NOT NULL,
            satisfaction INTEGER NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS final_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id TEXT NOT NULL,
            preferred_actuator TEXT NOT NULL,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
        )
        """
    )

    conn.commit()
    return conn


def upsert_participant(conn: sqlite3.Connection, participant_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO participants (participant_id) VALUES (?)",
        (participant_id,),
    )
    conn.commit()


def save_trial(conn: sqlite3.Connection, result: TrialResult) -> None:
    conn.execute(
        """
        INSERT INTO trials (
            participant_id,
            actuator_type,
            trial_index,
            vibration_onset,
            response_time,
            reaction_time_ms,
            is_valid,
            is_miss,
            false_start_count,
            iti_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.participant_id,
            result.actuator_type,
            result.trial_index,
            result.vibration_onset,
            result.response_time,
            result.reaction_time_ms,
            result.is_valid,
            result.is_miss,
            result.false_start_count,
            result.iti_seconds,
        ),
    )
    conn.commit()


def save_subjective_ratings(
    conn: sqlite3.Connection,
    participant_id: str,
    actuator_type: str,
    clarity: int,
    comfort: int,
    responsiveness: int,
    satisfaction: int,
    notes: str,
) -> None:
    conn.execute(
        """
        INSERT INTO subjective_ratings (
            participant_id,
            actuator_type,
            clarity,
            comfort,
            responsiveness,
            satisfaction,
            notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participant_id,
            actuator_type,
            clarity,
            comfort,
            responsiveness,
            satisfaction,
            notes,
        ),
    )
    conn.commit()


def save_final_feedback(
    conn: sqlite3.Connection,
    participant_id: str,
    preferred_actuator: str,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO final_feedback (
            participant_id,
            preferred_actuator,
            reason
        ) VALUES (?, ?, ?)
        """,
        (participant_id, preferred_actuator, reason),
    )
    conn.commit()


# ==========================================
# Response key recorder
# ==========================================

class KeyPressRecorder:
    def __init__(self, target_key=RESPONSE_KEY):
        self.target_key = target_key
        self.events: "queue.Queue[float]" = queue.Queue()
        self.listener = keyboard.Listener(on_press=self._on_press)

    def _on_press(self, key):
        if key == self.target_key:
            self.events.put(time.perf_counter())

    def start(self) -> None:
        self.listener.start()

    def stop(self) -> None:
        self.listener.stop()

    def clear(self) -> None:
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def count_false_starts_before(self, onset_time: float) -> int:
        count = 0
        future_events = []

        while not self.events.empty():
            try:
                event_time = self.events.get_nowait()
            except queue.Empty:
                break

            if event_time < onset_time:
                count += 1
            else:
                future_events.append(event_time)

        for event_time in future_events:
            self.events.put(event_time)

        return count

    def wait_for_response(self, onset_time: float, timeout_seconds: float) -> Tuple[Optional[float], int]:
        deadline = onset_time + timeout_seconds
        false_starts = 0

        while time.perf_counter() < deadline:
            remaining = max(0.0, deadline - time.perf_counter())
            try:
                event_time = self.events.get(timeout=min(0.01, remaining))
            except queue.Empty:
                continue

            if event_time < onset_time:
                false_starts += 1
                continue

            return event_time, false_starts

        return None, false_starts


# ==========================================
# Hardware helpers
# ==========================================

def actuator_mask(index: int) -> int:
    return 1 << index


def vibrate_once(vc: VibratorController, actuator_index: int, amp: int, duration: float) -> float:
    # This command style follows the user's reference file.
    mask = actuator_mask(actuator_index)
    onset = time.perf_counter()
    vc.send(f"S {mask} {amp}")
    time.sleep(duration)
    vc.stop_all()
    return onset


# ==========================================
# User input helpers
# ==========================================

def ask_participant_id() -> str:
    while True:
        participant_id = input("Please enter participant ID: ").strip()
        if participant_id:
            return participant_id
        print("Participant ID cannot be empty.")


def ask_likert(question: str) -> int:
    while True:
        value = input(f"{question} (1-5): ").strip()
        if value in {"1", "2", "3", "4", "5"}:
            return int(value)
        print("Please enter a number from 1 to 5.")


def collect_subjective_feedback(conn: sqlite3.Connection, participant_id: str, actuator_type: str) -> None:
    print(f"\nSubjective feedback for {actuator_type}")
    clarity = ask_likert("Perceived clarity of the vibration signal")
    comfort = ask_likert("Comfort of the vibration during repeated trials")
    responsiveness = ask_likert("Perceived responsiveness of the vibration feedback")
    satisfaction = ask_likert("Overall satisfaction with the vibration cue")
    notes = input("Optional notes: ").strip()

    save_subjective_ratings(
        conn,
        participant_id,
        actuator_type,
        clarity,
        comfort,
        responsiveness,
        satisfaction,
        notes,
    )


def collect_final_feedback(conn: sqlite3.Connection, participant_id: str) -> None:
    print("\nFinal comparison feedback")
    while True:
        preferred = input("Which actuator did the participant prefer? (LRA/ERM): ").strip().upper()
        if preferred in {"LRA", "ERM"}:
            break
        print("Please enter LRA or ERM.")

    reason = input("Why was this actuator preferred? ").strip()
    save_final_feedback(conn, participant_id, preferred, reason)


# ==========================================
# Experiment logic
# ==========================================

def compute_block_summary(results: List[TrialResult]) -> Dict[str, float]:
    valid_rts = [r.reaction_time_ms for r in results if r.reaction_time_ms is not None and r.is_valid == 1]
    total = len(results)
    valid_count = sum(r.is_valid for r in results)
    miss_count = sum(r.is_miss for r in results)

    return {
        "mean_reaction_time_ms": (sum(valid_rts) / len(valid_rts)) if valid_rts else float("nan"),
        "accuracy": (valid_count / total) if total else 0.0,
        "miss_rate": (miss_count / total) if total else 0.0,
    }


def run_block(
    conn: sqlite3.Connection,
    vc: VibratorController,
    key_recorder: KeyPressRecorder,
    participant_id: str,
    actuator_type: str,
    actuator_index: int,
    n_trials: int,
) -> List[TrialResult]:
    print("\n" + "=" * 60)
    print(f"Starting block: {actuator_type}")
    print(f"Actuator index: {actuator_index}")
    print(f"Trials: {n_trials}")
    print("Instruction: wait for the vibration, then press SPACE as fast as possible.")
    input("Press Enter when the participant is ready... ")

    results: List[TrialResult] = []

    for trial_index in range(1, n_trials + 1):
        iti = random.uniform(INTER_TRIAL_INTERVAL_MIN, INTER_TRIAL_INTERVAL_MAX)
        key_recorder.clear()

        print(f"\n{actuator_type} | Trial {trial_index}/{n_trials}")
        time.sleep(iti)

        onset = vibrate_once(vc, actuator_index, AMP, VIBRATION_DURATION_SECONDS)
        false_before = key_recorder.count_false_starts_before(onset)
        response_time, false_after = key_recorder.wait_for_response(onset, MISS_WINDOW_SECONDS)
        false_start_count = false_before + false_after

        if response_time is None:
            reaction_time_ms = None
            is_valid = 0
            is_miss = 1
            print("Result: MISS")
        else:
            reaction_time_ms = (response_time - onset) * 1000.0
            is_valid = int((response_time - onset) <= VALID_RESPONSE_WINDOW_SECONDS)
            is_miss = 0
            if is_valid:
                print(f"Result: VALID | RT = {reaction_time_ms:.2f} ms")
            else:
                print(f"Result: LATE | RT = {reaction_time_ms:.2f} ms")

        result = TrialResult(
            participant_id=participant_id,
            actuator_type=actuator_type,
            trial_index=trial_index,
            vibration_onset=onset,
            response_time=response_time,
            reaction_time_ms=reaction_time_ms,
            is_valid=is_valid,
            is_miss=is_miss,
            false_start_count=false_start_count,
            iti_seconds=iti,
        )
        save_trial(conn, result)
        results.append(result)

    summary = compute_block_summary(results)
    print("\nBlock summary")
    print(f"Mean reaction time (valid only): {summary['mean_reaction_time_ms']:.2f} ms")
    print(f"Accuracy: {summary['accuracy'] * 100:.1f}%")
    print(f"Miss rate: {summary['miss_rate'] * 100:.1f}%")

    return results


# ==========================================
# Main
# ==========================================

def main() -> None:
    print("User Reaction Experiment")
    print("Condition order: LRA -> ERM")
    print("Response key: SPACE")
    print(f"Database file: {os.path.abspath(DB_PATH)}")

    conn = init_database(DB_PATH)
    participant_id = ask_participant_id()
    upsert_participant(conn, participant_id)

    key_recorder = KeyPressRecorder()
    key_recorder.start()

    try:
        with VibratorController() as vc:
            print("Connected device:", vc.echo())
            vc.stop_all()
            time.sleep(0.2)

            run_block(
                conn=conn,
                vc=vc,
                key_recorder=key_recorder,
                participant_id=participant_id,
                actuator_type="LRA",
                actuator_index=LRA_INDEX,
                n_trials=TRIALS_PER_BLOCK,
            )
            collect_subjective_feedback(conn, participant_id, "LRA")

            input("\nPress Enter to continue to ERM... ")

            run_block(
                conn=conn,
                vc=vc,
                key_recorder=key_recorder,
                participant_id=participant_id,
                actuator_type="ERM",
                actuator_index=ERM_INDEX,
                n_trials=TRIALS_PER_BLOCK,
            )
            collect_subjective_feedback(conn, participant_id, "ERM")
            collect_final_feedback(conn, participant_id)

            vc.stop_all()

    except KeyboardInterrupt:
        print("\nExperiment interrupted by user.")
    finally:
        key_recorder.stop()
        conn.close()

    print("\nExperiment finished. Data have been saved to SQLite.")


if __name__ == "__main__":
    main()
