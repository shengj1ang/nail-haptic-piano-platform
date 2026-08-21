"""Latency analysis of real, recorded tele-training sessions.

This reads the relay's own store of what actually happened in a lesson -
the per-cue `timings` dictionary the Student Client sends with every
performance row - and turns it into the transport and cue-delivery
numbers for that session. It is the counterpart to the latency benchmark:
the benchmark fires synthetic probes to characterise the path, this reads
the delays that real guidance cues met.

What can and cannot be measured from this data is decided by which clock
a pair of timestamps came from, and the split is the whole point:

  * Same-clock differences are latencies. The relay stamps both its
    receive and its send on one machine, so server_receive -> server_send
    is real relay processing time. The Student Client's monotonic marks
    (queue, dispatch, LED/haptic/paint) are all one clock, so the cue
    pipeline and the visual-haptic skew are real.

  * Cross-machine wall-clock differences are NOT latencies on their own.
    Teacher, server and student clocks are not synchronised, and their
    offsets are large and drift between sessions (seen directly as the
    per-session minimum jumping from tens of milliseconds to seconds). An
    absolute one-way delay therefore cannot be read from them. What
    survives is the variation *within* a session: subtracting a session's
    own minimum cancels the constant offset and leaves the excess delay -
    the delivery jitter - above that session's floor. That, and only
    that, is reported for the cross-machine leg.

No accuracy is recomputed here: note/finger correctness is taken verbatim
from the row the student already scored, exactly as the server does.
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# The relay's SQLite file. This is a read-only consumer of it; it never
# writes, and opens with mode=ro so it cannot interfere with a running
# server.
DEFAULT_DB = Path(__file__).resolve().parent.parent / "server" / "data" / "remote_guidance.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def summarize(values: Sequence[float]) -> Optional[Dict[str, float]]:
    """min / median / mean / sd / p95 / p99 / max over a sample, or None
    when there is nothing to summarise. Percentiles are nearest-rank, as
    in the benchmark, so a small n degrades gracefully instead of
    interpolating between two points that are not there."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)

    def pct(q: float) -> float:
        return vals[min(n - 1, int(q * n))]

    return {
        "n": n,
        "min": round(vals[0], 3),
        "median": round(st.median(vals), 3),
        "mean": round(st.mean(vals), 3),
        "sd": round(st.pstdev(vals), 3),
        "p95": round(pct(0.95), 3),
        "p99": round(pct(0.99), 3),
        "max": round(vals[-1], 3),
    }


@dataclass
class SessionInfo:
    session_id: str
    room_id: Optional[str]
    guidance_mode: Optional[str]
    created_at: Optional[float]
    state: Optional[str]
    cue_rows: int          # performance rows for this session
    timed_rows: int        # of those, how many carry a full timings dict

    def label(self) -> str:
        import datetime

        when = (
            datetime.datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M")
            if self.created_at
            else "?"
        )
        mode = self.guidance_mode or "?"
        return f"{when}  ·  {mode}  ·  {self.timed_rows} cues  ·  {self.session_id[:8]}"


def list_sessions(db_path: Path = DEFAULT_DB) -> List[SessionInfo]:
    """Every session that recorded at least one performance row, newest
    first. Sessions with no rows are skipped: there is nothing to analyse.

    The performance table is queried directly, so a session whose room row
    was lost is still analysable here - this is an offline reading of the
    data, not the authorised REST surface."""
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.session_id AS sid,
                   COUNT(*) AS cue_rows,
                   MIN(p.created_at) AS created_at,
                   s.room_id AS room_id,
                   s.guidance_mode AS guidance_mode,
                   s.state AS state
            FROM performance_events p
            LEFT JOIN guidance_sessions s ON s.id = p.session_id
            GROUP BY p.session_id
            ORDER BY created_at DESC
            """
        ).fetchall()

        out: List[SessionInfo] = []
        for r in rows:
            timed = conn.execute(
                "SELECT COUNT(*) FROM performance_events "
                "WHERE session_id = ? AND json_extract(payload_json, '$.timings') IS NOT NULL",
                (r["sid"],),
            ).fetchone()[0]
            out.append(
                SessionInfo(
                    session_id=r["sid"],
                    room_id=r["room_id"],
                    guidance_mode=r["guidance_mode"],
                    created_at=r["created_at"],
                    state=r["state"],
                    cue_rows=r["cue_rows"],
                    timed_rows=timed,
                )
            )
        return out
    finally:
        conn.close()


@dataclass
class SessionLatency:
    info: SessionInfo
    # same-clock latencies (trustworthy)
    relay_processing_ms: Optional[Dict[str, float]] = None
    queue_wait_ms: Optional[Dict[str, float]] = None
    local_dispatch_ms: Optional[Dict[str, float]] = None
    receive_to_cue_ready_ms: Optional[Dict[str, float]] = None
    abs_visual_haptic_skew_ms: Optional[Dict[str, float]] = None
    # cross-machine, offset-robust (variation only)
    delivery_excess_ms: Optional[Dict[str, float]] = None
    session_floor_ms: Optional[float] = None  # the per-session minimum that was subtracted
    # behavioural, taken verbatim from the student's own scoring
    reaction_time_ms: Optional[Dict[str, float]] = None
    note_accuracy: Optional[float] = None
    finger_accuracy: Optional[float] = None
    timeouts: int = 0
    # raw arrays kept for the figures
    raw: Dict[str, List[float]] = field(default_factory=dict)


def _timings(conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM performance_events WHERE session_id = ? ORDER BY seq, created_at",
        (session_id,),
    ).fetchall()
    out = []
    for (pj,) in rows:
        try:
            out.append(json.loads(pj))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def analyse_session(session_id: str, db_path: Path = DEFAULT_DB) -> SessionLatency:
    conn = _connect(db_path)
    try:
        info_rows = list_sessions(db_path)
        info = next((s for s in info_rows if s.session_id == session_id), None)
        if info is None:
            raise KeyError(f"no session {session_id} in {db_path}")

        payloads = _timings(conn, session_id)
        T = [p["timings"] for p in payloads if isinstance(p.get("timings"), dict)]

        def same_clock(a: str, b: str) -> List[float]:
            out = []
            for t in T:
                x, y = t.get(a), t.get(b)
                if x is not None and y is not None:
                    out.append((y - x) / 1e6)
            return out

        # -- relay processing: server's own two stamps, from guidance_events --
        relay = [
            (r["server_send_wall_ns"] - r["server_receive_wall_ns"]) / 1e6
            for r in conn.execute(
                "SELECT server_receive_wall_ns, server_send_wall_ns FROM guidance_events "
                "WHERE session_id = ? AND server_receive_wall_ns IS NOT NULL "
                "AND server_send_wall_ns IS NOT NULL",
                (session_id,),
            ).fetchall()
        ]

        # -- student cue pipeline (all one monotonic clock) --
        queue = [t["queue_wait_ns"] / 1e6 for t in T if t.get("queue_wait_ns") is not None]
        dispatch = [t["local_dispatch_ns"] / 1e6 for t in T if t.get("local_dispatch_ns") is not None]
        cue_ready = same_clock("student_receive_monotonic_ns", "cue_ready_monotonic_ns")
        skew = [
            abs(t["visual_painted_monotonic_ns"] - t["haptic_command_complete_monotonic_ns"]) / 1e6
            for t in T
            if t.get("visual_painted_monotonic_ns") and t.get("haptic_command_complete_monotonic_ns")
        ]

        # -- cross-machine delivery, offset removed by subtracting the floor --
        span = same_clock("teacher_send_wall_ns", "student_receive_wall_ns")
        floor = min(span) if span else None
        excess = [x - floor for x in span] if floor is not None else []

        # -- behavioural, verbatim from the student's final rows --
        finals = [p for p in payloads if p.get("stage") == "final"]
        rt = [p["reaction_time_ns"] / 1e6 for p in finals if p.get("reaction_time_ns") is not None]
        note_ok = [p.get("note_correct") for p in finals if p.get("note_correct") is not None]
        finger_ok = [p.get("finger_correct") for p in finals if p.get("finger_correct") is not None]
        timeouts = sum(1 for p in payloads if p.get("timed_out"))

        return SessionLatency(
            info=info,
            relay_processing_ms=summarize(relay),
            queue_wait_ms=summarize(queue),
            local_dispatch_ms=summarize(dispatch),
            receive_to_cue_ready_ms=summarize(cue_ready),
            abs_visual_haptic_skew_ms=summarize(skew),
            delivery_excess_ms=summarize(excess),
            session_floor_ms=round(floor, 3) if floor is not None else None,
            reaction_time_ms=summarize(rt),
            note_accuracy=(sum(note_ok) / len(note_ok)) if note_ok else None,
            finger_accuracy=(sum(finger_ok) / len(finger_ok)) if finger_ok else None,
            timeouts=timeouts,
            raw={
                "delivery_excess_ms": excess,
                "local_dispatch_ms": dispatch,
                "abs_visual_haptic_skew_ms": skew,
                "queue_wait_ms": queue,
            },
        )
    finally:
        conn.close()
