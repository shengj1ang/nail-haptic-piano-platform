"""SQLite storage for rooms, sessions, guidance/performance events and
latency runs.

Three deliberate choices:

  - WAL journalling plus a busy timeout, because the WebSocket relay
    writes while REST handlers read, from different threads.
  - foreign_keys ON, so a room's members/sessions/events cannot outlive
    the room row (every table cascades from its parent).
  - a PRAGMA user_version migration ladder rather than an ORM: each step
    is a plain function that moves the schema from version n to n+1, run
    inside one transaction, so an existing database upgrades in place and
    a restart never loses data.

No video is ever stored here. Recordings keep only the note/finger event
list needed to drive cues; the media stays on the machine that captured
it.

Every query is parameterised - no SQL is ever built by string formatting
from user input.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA_VERSION = 1

# Join codes are typed by a human, so the alphabet drops the characters
# that get misread out loud or on a screen (0/O, 1/I/L).
_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 6

ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
VALID_ROLES = (ROLE_TEACHER, ROLE_STUDENT)


def new_id() -> str:
    return str(uuid.uuid4())


def generate_join_code(length: int = JOIN_CODE_LENGTH) -> str:
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(length))


class Database:
    """Thread-local connections over one SQLite file.

    FastAPI runs sync endpoints in a worker threadpool and the relay
    persists from its own task thread, so a single shared connection
    would need external locking on every call; one connection per thread
    (all pointing at the same WAL-mode file) is simpler and lets readers
    and the writer proceed concurrently."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.migrate()

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # ------------------------------------------------------------------
    # migrations
    # ------------------------------------------------------------------

    def migrate(self) -> int:
        """Bring the file up to SCHEMA_VERSION, one step at a time.

        Returns the version now in effect. Safe to call on every start:
        an already-current database does nothing."""
        conn = self.conn
        with self._write_lock:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            while version < SCHEMA_VERSION:
                step = _MIGRATIONS[version]
                conn.executescript(step)
                version += 1
                # PRAGMA does not accept a bound parameter; version is an
                # int we control, never user input.
                conn.execute(f"PRAGMA user_version={int(version)}")
                conn.commit()
        return version

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])


_MIGRATION_0_TO_1 = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    display_name  TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    join_code  TEXT NOT NULL UNIQUE,
    owner_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    closed_at  REAL
);

-- One row per (room, user). A first-version room is one teacher plus one
-- active student, but nothing in this table says so: extra students are
-- extra rows, which is what makes a multi-student room a routing change
-- later rather than a schema migration.
CREATE TABLE IF NOT EXISTS room_members (
    room_id   TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role      TEXT NOT NULL,
    joined_at REAL NOT NULL,
    PRIMARY KEY (room_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_room_members_user ON room_members(user_id);

-- Metadata + event list of a pre-recorded fingering sequence. No media:
-- the video stays on the machine that recorded it.
CREATE TABLE IF NOT EXISTS recordings (
    id           TEXT PRIMARY KEY,
    room_id      TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    uploaded_by  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    source       TEXT NOT NULL,
    event_count  INTEGER NOT NULL,
    duration_s   REAL NOT NULL,
    meta_json    TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recordings_room ON recordings(room_id);

CREATE TABLE IF NOT EXISTS recording_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id  TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    event_order   INTEGER NOT NULL,
    rel_time_s    REAL NOT NULL,
    duration_s    REAL NOT NULL,
    note          INTEGER NOT NULL,
    note_name     TEXT,
    key_id        INTEGER,
    finger        TEXT,
    UNIQUE (recording_id, event_order)
);

CREATE TABLE IF NOT EXISTS guidance_sessions (
    id            TEXT PRIMARY KEY,
    room_id       TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    created_by    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mode          TEXT NOT NULL,
    guidance_mode TEXT NOT NULL,
    playback_mode TEXT,
    recording_id  TEXT REFERENCES recordings(id) ON DELETE SET NULL,
    state         TEXT NOT NULL,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_room ON guidance_sessions(room_id);

-- id IS the client's message_id: re-delivering the same event after a
-- reconnect collides on the primary key instead of double-counting.
CREATE TABLE IF NOT EXISTS guidance_events (
    id                     TEXT PRIMARY KEY,
    session_id             TEXT REFERENCES guidance_sessions(id) ON DELETE CASCADE,
    room_id                TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    sender_user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    seq                    INTEGER NOT NULL,
    type                   TEXT NOT NULL,
    sender_send_wall_ns    INTEGER,
    server_receive_wall_ns INTEGER,
    server_send_wall_ns    INTEGER,
    payload_json           TEXT NOT NULL,
    created_at             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guidance_events_session ON guidance_events(session_id, seq);

CREATE TABLE IF NOT EXISTS performance_events (
    id                     TEXT PRIMARY KEY,
    session_id             TEXT REFERENCES guidance_sessions(id) ON DELETE CASCADE,
    room_id                TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    sender_user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    guidance_message_id    TEXT,
    seq                    INTEGER NOT NULL,
    stage                  TEXT NOT NULL,
    sender_send_wall_ns    INTEGER,
    server_receive_wall_ns INTEGER,
    payload_json           TEXT NOT NULL,
    created_at             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_performance_events_session ON performance_events(session_id, seq);

CREATE TABLE IF NOT EXISTS latency_runs (
    id          TEXT PRIMARY KEY,
    room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    session_id  TEXT REFERENCES guidance_sessions(id) ON DELETE SET NULL,
    created_by  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    config_json TEXT NOT NULL,
    summary_json TEXT,
    started_at  REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS latency_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES latency_runs(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    warmup        INTEGER NOT NULL DEFAULT 0,
    rtt_ns        INTEGER,
    presented_rtt_ns INTEGER,
    dispatch_ns   INTEGER,
    lost          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, seq)
);

-- Logout / revocation. Rows can be pruned once expires_at has passed:
-- an expired token is already rejected by its own exp claim.
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti        TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    revoked_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

_MIGRATIONS = [_MIGRATION_0_TO_1]


# ---------------------------------------------------------------------------
# Small typed helpers over the tables above. Kept as free functions taking a
# Database so the API layer and the WebSocket relay share exactly one copy of
# every query.
# ---------------------------------------------------------------------------


def create_user(db: Database, username: str, password_hash: str, role: str, display_name: Optional[str] = None) -> str:
    user_id = new_id()
    db.execute(
        "INSERT INTO users (id, username, password_hash, role, display_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, password_hash, role, display_name, time.time()),
    )
    return user_id


def get_user_by_username(db: Database, username: str) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))


def get_user(db: Database, user_id: str) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def user_count(db: Database) -> int:
    return int(db.query_one("SELECT COUNT(*) AS n FROM users")["n"])


def create_room(db: Database, name: str, owner_id: str) -> sqlite3.Row:
    """Retries on the (astronomically unlikely) join-code collision rather
    than trusting randomness - the UNIQUE index is the real guarantee."""
    for _ in range(10):
        room_id = new_id()
        code = generate_join_code()
        try:
            db.execute(
                "INSERT INTO rooms (id, name, join_code, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (room_id, name, code, owner_id, time.time()),
            )
        except sqlite3.IntegrityError:
            continue
        add_room_member(db, room_id, owner_id, ROLE_TEACHER)
        return get_room(db, room_id)
    raise RuntimeError("could not allocate a unique join code")


def get_room(db: Database, room_id: str) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM rooms WHERE id = ?", (room_id,))


def get_room_by_code(db: Database, join_code: str) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM rooms WHERE join_code = ?", (join_code.strip().upper(),))


def close_room(db: Database, room_id: str) -> None:
    db.execute("UPDATE rooms SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (time.time(), room_id))


def add_room_member(db: Database, room_id: str, user_id: str, role: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO room_members (room_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
        (room_id, user_id, role, time.time()),
    )


def get_membership(db: Database, room_id: str, user_id: str) -> Optional[sqlite3.Row]:
    return db.query_one(
        "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
        (room_id, user_id),
    )


def list_room_members(db: Database, room_id: str) -> List[sqlite3.Row]:
    return db.query(
        "SELECT m.room_id, m.user_id, m.role, m.joined_at, u.username, u.display_name "
        "FROM room_members m JOIN users u ON u.id = m.user_id WHERE m.room_id = ? "
        "ORDER BY m.joined_at",
        (room_id,),
    )


def create_recording(
    db: Database,
    room_id: str,
    uploaded_by: str,
    name: str,
    source: str,
    events: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> str:
    recording_id = new_id()
    duration = max((float(e.get("rel_time_s", 0.0)) + float(e.get("duration_s", 0.0)) for e in events), default=0.0)
    db.execute(
        "INSERT INTO recordings (id, room_id, uploaded_by, name, source, event_count, duration_s, meta_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (recording_id, room_id, uploaded_by, name, source, len(events), duration, json.dumps(meta, ensure_ascii=False), time.time()),
    )
    db.executemany(
        "INSERT INTO recording_events "
        "(recording_id, event_order, rel_time_s, duration_s, note, note_name, key_id, finger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                recording_id,
                int(e.get("event_order", i)),
                float(e.get("rel_time_s", 0.0)),
                float(e.get("duration_s", 0.0)),
                int(e["note"]),
                e.get("note_name"),
                e.get("key_id"),
                e.get("finger"),
            )
            for i, e in enumerate(events)
        ],
    )
    return recording_id


def get_recording(db: Database, recording_id: str) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM recordings WHERE id = ?", (recording_id,))


def list_recordings(db: Database, room_id: str) -> List[sqlite3.Row]:
    return db.query("SELECT * FROM recordings WHERE room_id = ? ORDER BY created_at DESC", (room_id,))


def get_recording_events(db: Database, recording_id: str) -> List[sqlite3.Row]:
    return db.query(
        "SELECT * FROM recording_events WHERE recording_id = ? ORDER BY event_order",
        (recording_id,),
    )


def create_session(
    db: Database,
    room_id: str,
    created_by: str,
    mode: str,
    guidance_mode: str,
    playback_mode: Optional[str] = None,
    recording_id: Optional[str] = None,
) -> str:
    session_id = new_id()
    db.execute(
        "INSERT INTO guidance_sessions "
        "(id, room_id, created_by, mode, guidance_mode, playback_mode, recording_id, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?)",
        (session_id, room_id, created_by, mode, guidance_mode, playback_mode, recording_id, time.time()),
    )
    return session_id


def get_session(db: Database, session_id: str) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM guidance_sessions WHERE id = ?", (session_id,))


def set_session_state(db: Database, session_id: str, state: str) -> None:
    now = time.time()
    if state == "running":
        db.execute(
            "UPDATE guidance_sessions SET state = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
            (state, now, session_id),
        )
    elif state == "finished":
        db.execute(
            "UPDATE guidance_sessions SET state = ?, finished_at = ? WHERE id = ?",
            (state, now, session_id),
        )
    else:
        db.execute("UPDATE guidance_sessions SET state = ? WHERE id = ?", (state, session_id))


def set_session_guidance_mode(db: Database, session_id: str, guidance_mode: str) -> None:
    db.execute(
        "UPDATE guidance_sessions SET guidance_mode = ? WHERE id = ?",
        (guidance_mode, session_id),
    )


def insert_guidance_event(db: Database, row: Dict[str, Any]) -> bool:
    """False if this message_id was already stored (duplicate delivery)."""
    cur = db.execute(
        "INSERT OR IGNORE INTO guidance_events "
        "(id, session_id, room_id, sender_user_id, seq, type, sender_send_wall_ns, "
        " server_receive_wall_ns, server_send_wall_ns, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row.get("session_id"),
            row["room_id"],
            row["sender_user_id"],
            int(row.get("seq", 0)),
            row["type"],
            row.get("sender_send_wall_ns"),
            row.get("server_receive_wall_ns"),
            row.get("server_send_wall_ns"),
            json.dumps(row.get("payload", {}), ensure_ascii=False),
            time.time(),
        ),
    )
    return cur.rowcount > 0


def insert_performance_event(db: Database, row: Dict[str, Any]) -> bool:
    cur = db.execute(
        "INSERT OR REPLACE INTO performance_events "
        "(id, session_id, room_id, sender_user_id, guidance_message_id, seq, stage, "
        " sender_send_wall_ns, server_receive_wall_ns, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row.get("session_id"),
            row["room_id"],
            row["sender_user_id"],
            row.get("guidance_message_id"),
            int(row.get("seq", 0)),
            row.get("stage", "provisional"),
            row.get("sender_send_wall_ns"),
            row.get("server_receive_wall_ns"),
            json.dumps(row.get("payload", {}), ensure_ascii=False),
            time.time(),
        ),
    )
    return cur.rowcount > 0


def list_session_events(db: Database, session_id: str) -> Dict[str, List[sqlite3.Row]]:
    return {
        "guidance": db.query(
            "SELECT * FROM guidance_events WHERE session_id = ? ORDER BY seq, created_at", (session_id,)
        ),
        "performance": db.query(
            "SELECT * FROM performance_events WHERE session_id = ? ORDER BY seq, created_at", (session_id,)
        ),
    }


def revoke_token(db: Database, jti: str, user_id: str, expires_at: float) -> None:
    db.execute(
        "INSERT OR REPLACE INTO revoked_tokens (jti, user_id, revoked_at, expires_at) VALUES (?, ?, ?, ?)",
        (jti, user_id, time.time(), expires_at),
    )


def is_token_revoked(db: Database, jti: str) -> bool:
    return db.query_one("SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)) is not None


def purge_expired_revocations(db: Database) -> int:
    cur = db.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (time.time(),))
    return cur.rowcount


def create_latency_run(db: Database, room_id: str, created_by: str, config: Dict[str, Any],
                       session_id: Optional[str] = None) -> str:
    run_id = new_id()
    db.execute(
        "INSERT INTO latency_runs (id, room_id, session_id, created_by, config_json, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, room_id, session_id, created_by, json.dumps(config, ensure_ascii=False), time.time()),
    )
    return run_id


def finish_latency_run(db: Database, run_id: str, summary: Dict[str, Any]) -> None:
    db.execute(
        "UPDATE latency_runs SET summary_json = ?, finished_at = ? WHERE id = ?",
        (json.dumps(summary, ensure_ascii=False), time.time(), run_id),
    )


def insert_latency_samples(db: Database, run_id: str, samples: Iterable[Dict[str, Any]]) -> None:
    db.executemany(
        "INSERT OR REPLACE INTO latency_samples "
        "(run_id, seq, warmup, rtt_ns, presented_rtt_ns, dispatch_ns, lost) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                int(s["seq"]),
                1 if s.get("warmup") else 0,
                s.get("rtt_ns"),
                s.get("presented_rtt_ns"),
                s.get("dispatch_ns"),
                1 if s.get("lost") else 0,
            )
            for s in samples
        ],
    )


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None
