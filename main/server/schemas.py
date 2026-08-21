"""Request/response bodies and the WebSocket envelope, server side.

The envelope defined here is the server half of a matched pair with
remote_guidance/protocol.py on the client side. They are duplicated on
purpose: this folder has to stay deployable on its own, so it cannot
import the piano platform. PROTOCOL_VERSION and MESSAGE_TYPES must be
kept identical in both files - a client whose "v" does not match is
rejected with a clear error rather than being half-understood.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1

# --- message types ---------------------------------------------------------
# Grouped by who normally sends them; the server enforces the teacher-only
# set (see websocket.TEACHER_ONLY_TYPES).

TYPE_AUTH = "auth"
TYPE_PRESENCE = "presence"
TYPE_HEARTBEAT = "heartbeat"
TYPE_ERROR = "error"

TYPE_GUIDANCE_LIVE = "guidance.live"
TYPE_GUIDANCE_RECEIVED = "guidance.received"
TYPE_GUIDANCE_PRESENTED = "guidance.presented"
TYPE_PERFORMANCE_RESPONSE = "performance.response"

TYPE_SESSION_START = "session.start"
TYPE_SESSION_PAUSE = "session.pause"
TYPE_SESSION_RESUME = "session.resume"
TYPE_SESSION_STOP = "session.stop"
TYPE_SESSION_FINISHED = "session.finished"

TYPE_RECORDING_READY = "recording.ready"
TYPE_RECORDING_START = "recording.start"
TYPE_RECORDING_PAUSE = "recording.pause"
TYPE_RECORDING_STOP = "recording.stop"

TYPE_LATENCY_PROBE = "latency.probe"
TYPE_LATENCY_RECEIVED = "latency.received"
TYPE_LATENCY_PRESENTED = "latency.presented"
TYPE_LATENCY_ACK = "latency.ack"

MESSAGE_TYPES = frozenset(
    {
        TYPE_AUTH,
        TYPE_PRESENCE,
        TYPE_HEARTBEAT,
        TYPE_ERROR,
        TYPE_GUIDANCE_LIVE,
        TYPE_GUIDANCE_RECEIVED,
        TYPE_GUIDANCE_PRESENTED,
        TYPE_PERFORMANCE_RESPONSE,
        TYPE_SESSION_START,
        TYPE_SESSION_PAUSE,
        TYPE_SESSION_RESUME,
        TYPE_SESSION_STOP,
        TYPE_SESSION_FINISHED,
        TYPE_RECORDING_READY,
        TYPE_RECORDING_START,
        TYPE_RECORDING_PAUSE,
        TYPE_RECORDING_STOP,
        TYPE_LATENCY_PROBE,
        TYPE_LATENCY_RECEIVED,
        TYPE_LATENCY_PRESENTED,
        TYPE_LATENCY_ACK,
    }
)


class Envelope(BaseModel):
    """One WebSocket frame.

    sent_at_unix_ns is the *sender's* wall clock and is never rewritten -
    the server only ever adds its own stamps under `server`, so a reader
    can always tell the two apart. Monotonic values, when a client
    includes them in a payload, are opaque here: they are meaningful only
    on the machine that produced them and are simply carried back."""

    v: int = PROTOCOL_VERSION
    type: str
    message_id: str
    room_id: Optional[str] = None
    session_id: Optional[str] = None
    seq: int = 0
    sent_at_unix_ns: int = 0
    payload: Dict[str, Any] = Field(default_factory=dict)
    # Filled in by the relay: receive/send wall-clock ns, plus flags such
    # as duplicate/out-of-order. Clients must not set it.
    server: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


def make_envelope(
    type_: str,
    message_id: str,
    room_id: Optional[str] = None,
    session_id: Optional[str] = None,
    seq: int = 0,
    payload: Optional[Dict[str, Any]] = None,
    server: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": type_,
        "message_id": message_id,
        "room_id": room_id,
        "session_id": session_id,
        "seq": seq,
        "sent_at_unix_ns": time.time_ns(),
        "payload": payload or {},
        "server": server,
    }


# ---------------------------------------------------------------------------
# REST bodies
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(pattern="^(teacher|student)$")
    display_name: Optional[str] = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: int
    refresh_expires_at: int
    user_id: str
    username: str
    role: str


class UserResponse(BaseModel):
    user_id: str
    username: str
    role: str
    display_name: Optional[str] = None
    created_at: float


class CreateRoomRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class JoinRoomRequest(BaseModel):
    join_code: str = Field(min_length=4, max_length=16)


class RoomResponse(BaseModel):
    room_id: str
    name: str
    join_code: Optional[str] = None  # only returned to members
    owner_id: str
    created_at: float
    closed_at: Optional[float] = None
    my_role: Optional[str] = None


class MemberResponse(BaseModel):
    user_id: str
    username: str
    display_name: Optional[str] = None
    role: str
    joined_at: float
    online: bool = False


class RecordingEventModel(BaseModel):
    event_order: int
    rel_time_s: float
    duration_s: float = 0.4
    note: int
    note_name: Optional[str] = None
    key_id: Optional[int] = None
    finger: Optional[str] = None


class CreateRecordingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(default="music", max_length=32)
    meta: Dict[str, Any] = Field(default_factory=dict)
    events: List[RecordingEventModel]


class RecordingSummary(BaseModel):
    recording_id: str
    room_id: str
    name: str
    source: str
    event_count: int
    duration_s: float
    created_at: float
    uploaded_by: str


class RecordingDetail(RecordingSummary):
    meta: Dict[str, Any] = Field(default_factory=dict)
    events: List[RecordingEventModel] = Field(default_factory=list)


class CreateSessionRequest(BaseModel):
    # "live" = teacher plays in real time; "recording" = a stored sequence
    # is scheduled locally on the student's machine.
    mode: str = Field(default="live", pattern="^(live|recording)$")
    # The teacher opens the session but does not choose how guidance is
    # rendered. The neutral value is replaced by the student's own
    # visual/haptic/both choice when its recording.ready frame arrives.
    # The concrete values remain accepted for older clients.
    guidance_mode: str = Field(default="student_choice", pattern="^(student_choice|visual|haptic|both)$")
    playback_mode: Optional[str] = Field(default=None, pattern="^(paced|original_timing)$")
    recording_id: Optional[str] = None


class SessionResponse(BaseModel):
    session_id: str
    room_id: str
    created_by: str
    mode: str
    guidance_mode: str
    playback_mode: Optional[str] = None
    recording_id: Optional[str] = None
    state: str
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class SessionListEntry(SessionResponse):
    """A session row plus the two numbers that decide whether it is worth
    exporting: how many relayed frames and how many performance rows it
    actually produced."""

    room_name: Optional[str] = None
    guidance_event_count: int = 0
    performance_event_count: int = 0


class SessionListResponse(BaseModel):
    count: int
    limit: int
    # True when more sessions matched than `limit` allowed through, so a
    # caller knows to narrow `since`/`room_id` rather than assuming it has
    # the whole set.
    truncated: bool = False
    sessions: List[SessionListEntry] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    protocol_version: int = PROTOCOL_VERSION
    server_version: str
    schema_version: int
    server_time_unix_ns: int
    rooms_online: int
    connections: int
