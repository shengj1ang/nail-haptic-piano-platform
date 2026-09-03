"""The WebSocket message envelope, client side.

This is the client half of a matched pair with server/schemas.py. The two
are duplicated on purpose - server/ has to stay deployable on its own, so
it cannot import this package - and must be changed together.
PROTOCOL_VERSION exists so a mismatch is refused loudly at connect time
instead of being half-understood at runtime.

Payload shapes are documented here rather than enforced with a schema
library, because the client half also has to tolerate a *newer* server
adding fields it does not know about.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1

# --- message types (keep in sync with server/schemas.py) -------------------

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

# --- join codes (keep in sync with server/database.py) ---------------------
#
# The relay's own copy of these is _JOIN_CODE_ALPHABET / JOIN_CODE_LENGTH
# in server/database.py; the duplication is the same matched-pair rule as
# the message types above, and a test asserts the two agree.
#
# The alphabet leaves out I, L, O, 0 and 1 so a code read aloud or copied
# off a screen cannot be mistyped into a different valid code. That also
# makes looks_like_join_code() safe enough to be the *only* thing telling
# a room name apart from a code in the teacher's single room field: an
# ordinary word of exactly six letters almost always contains one of the
# excluded characters.
JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 6


def looks_like_join_code(value: str) -> bool:
    """Is this text a room join code rather than a room name?

    Deliberately strict: the length must match exactly and every
    character must be in the relay's alphabet. Anything else is treated
    as a name, so a wrong guess creates a room instead of failing to
    find one - which is visible immediately and costs one click."""
    text = (value or "").strip().upper()
    return len(text) == JOIN_CODE_LENGTH and all(character in JOIN_CODE_ALPHABET for character in text)


# Guidance modes a student session can run in. The LED key cue is present
# in all three - these name only how the *finger* is conveyed.
GUIDANCE_VISUAL = "visual"
GUIDANCE_HAPTIC = "haptic"
GUIDANCE_BOTH = "both"
GUIDANCE_MODES = (GUIDANCE_VISUAL, GUIDANCE_HAPTIC, GUIDANCE_BOTH)

# Pre-recorded playback modes.
PLAYBACK_PACED = "paced"  # each event waits for a response or times out
PLAYBACK_ORIGINAL = "original_timing"  # replay the teacher's own relative timing
PLAYBACK_MODES = (PLAYBACK_PACED, PLAYBACK_ORIGINAL)

STAGE_PROVISIONAL = "provisional"
STAGE_FINAL = "final"

DEFAULT_TIMEOUT_S = 5.0


@dataclass
class GuidanceAction:
    """One "press this key with this finger" instruction.

    guidance.live carries a *list* of these even when there is only one,
    so the protocol has room for a chord from day one - the teacher side
    already resolves simultaneous note-ons through
    app.finger_matching.match_notes_to_fingers, which assigns one
    fingertip per note."""

    note: int
    velocity: int = 0
    finger: Optional[str] = None
    key_id: Optional[int] = None
    note_name: Optional[str] = None
    finger_probability: Optional[float] = None
    finger_probabilities: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "note": int(self.note),
            "velocity": int(self.velocity),
            "finger": self.finger,
            "key_id": self.key_id,
            "note_name": self.note_name,
            "finger_probability": self.finger_probability,
            "finger_probabilities": dict(self.finger_probabilities),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuidanceAction":
        return cls(
            note=int(data["note"]),
            velocity=int(data.get("velocity", 0) or 0),
            finger=data.get("finger"),
            key_id=data.get("key_id"),
            note_name=data.get("note_name"),
            finger_probability=data.get("finger_probability"),
            finger_probabilities=dict(data.get("finger_probabilities") or {}),
        )


def guidance_payload(actions: List[GuidanceAction], timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    return {"actions": [a.to_dict() for a in actions], "timeout_s": float(timeout_s)}


def parse_actions(payload: Dict[str, Any]) -> List[GuidanceAction]:
    return [GuidanceAction.from_dict(a) for a in (payload.get("actions") or [])]


def new_message_id() -> str:
    return str(uuid.uuid4())


def make_envelope(
    type_: str,
    room_id: Optional[str] = None,
    session_id: Optional[str] = None,
    seq: int = 0,
    payload: Optional[Dict[str, Any]] = None,
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """sent_at_unix_ns is this machine's wall clock. It is a *label*, not
    something another machine may subtract from its own clock - see
    timing.py and the note in doc/server.md."""
    return {
        "v": PROTOCOL_VERSION,
        "type": type_,
        "message_id": message_id or new_message_id(),
        "room_id": room_id,
        "session_id": session_id,
        "seq": int(seq),
        "sent_at_unix_ns": time.time_ns(),
        "payload": payload or {},
    }


def server_block(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return envelope.get("server") or {}


def server_receive_wall_ns(envelope: Dict[str, Any]) -> Optional[int]:
    value = server_block(envelope).get("receive_wall_ns")
    return int(value) if value is not None else None


def sender_send_wall_ns(envelope: Dict[str, Any]) -> Optional[int]:
    value = envelope.get("sent_at_unix_ns")
    return int(value) if value else None
