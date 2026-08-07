"""The relay itself: /ws/v1/rooms/{room_id}.

Connection lifecycle
--------------------
1. The socket is accepted, then the client's FIRST frame must be an
   `auth` envelope carrying the access token in its payload. The token
   deliberately does not travel in the URL query string, where it would
   end up in proxy logs and browser history.
2. Membership of the room named in the path is checked against the
   database. Only then is the connection registered, so an
   unauthenticated socket can never receive another room's traffic.
3. Frames are relayed to the *other* members of the same room and
   nowhere else.

What the relay does and does not touch
--------------------------------------
It adds its own receive/send wall-clock stamps under `server` and never
rewrites the sender's `sent_at_unix_ns`, so a reader can always tell
which machine produced which number. It does not interpret monotonic
values carried inside payloads: those are meaningful only on the machine
that made them and are relayed back untouched.

Ordering, duplicates and persistence
------------------------------------
Every frame carries a UUID `message_id` and a per-connection increasing
`seq`. A repeated message_id is dropped rather than forwarded twice (a
reconnecting client replaying its outbox must not double-cue the
student); a seq that goes backwards is flagged `out_of_order` in the
`server` block and still delivered, because dropping a late cue silently
would be worse than showing it late. Forwarding happens first and the
database write is queued behind it, so persistence never sits in the
latency path this module exists to keep small.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from . import database as dbm
from .auth import AuthError
from .config import MAX_MESSAGE_BYTES
from .schemas import (
    PROTOCOL_VERSION,
    TYPE_AUTH,
    TYPE_ERROR,
    TYPE_GUIDANCE_LIVE,
    TYPE_GUIDANCE_PRESENTED,
    TYPE_GUIDANCE_RECEIVED,
    TYPE_HEARTBEAT,
    TYPE_LATENCY_ACK,
    TYPE_LATENCY_PROBE,
    TYPE_PERFORMANCE_RESPONSE,
    TYPE_PRESENCE,
    TYPE_RECORDING_PAUSE,
    TYPE_RECORDING_READY,
    TYPE_RECORDING_START,
    TYPE_RECORDING_STOP,
    TYPE_SESSION_FINISHED,
    TYPE_SESSION_PAUSE,
    TYPE_SESSION_RESUME,
    TYPE_SESSION_START,
    TYPE_SESSION_STOP,
    MESSAGE_TYPES,
    make_envelope,
)

router = APIRouter()
log = logging.getLogger("remote_guidance.server.ws")

# Controls only the room's own teacher may send. A student issuing one is
# refused with an `error` frame and the message is not relayed.
TEACHER_ONLY_TYPES = frozenset(
    {
        TYPE_GUIDANCE_LIVE,
        TYPE_SESSION_START,
        TYPE_SESSION_PAUSE,
        TYPE_SESSION_RESUME,
        TYPE_SESSION_STOP,
        TYPE_RECORDING_START,
        TYPE_RECORDING_PAUSE,
        TYPE_RECORDING_STOP,
        TYPE_LATENCY_PROBE,
    }
)

# Persisted as guidance (teacher-side intent / session lifecycle) versus
# performance (student-side outcome). Anything else - heartbeat, presence,
# latency probes - is transport bookkeeping and is not stored per frame.
GUIDANCE_PERSISTED_TYPES = frozenset(
    {
        TYPE_GUIDANCE_LIVE,
        TYPE_SESSION_START,
        TYPE_SESSION_PAUSE,
        TYPE_SESSION_RESUME,
        TYPE_SESSION_STOP,
        TYPE_SESSION_FINISHED,
        TYPE_RECORDING_START,
        TYPE_RECORDING_PAUSE,
        TYPE_RECORDING_STOP,
    }
)
PERFORMANCE_PERSISTED_TYPES = frozenset(
    {TYPE_PERFORMANCE_RESPONSE, TYPE_GUIDANCE_RECEIVED, TYPE_GUIDANCE_PRESENTED, TYPE_RECORDING_READY}
)

# How many recent message_ids to remember per room for duplicate
# suppression. Comfortably longer than any realistic reconnect replay at
# the protocol's event rates, and bounded so a long session cannot grow it
# without limit.
DEDUP_WINDOW = 4096


@dataclass
class Connection:
    websocket: WebSocket
    user_id: str
    username: str
    role: str
    room_id: str
    connection_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    last_seq: int = -1
    connected_at: float = field(default_factory=time.time)

    async def send(self, message: Dict[str, Any]) -> bool:
        if self.websocket.client_state != WebSocketState.CONNECTED:
            return False
        try:
            await self.websocket.send_text(json.dumps(message))
            return True
        except (RuntimeError, WebSocketDisconnect, ConnectionError):
            return False


class RoomRegistry:
    """Who is currently connected, per room. Purely in-memory: durable
    membership lives in the room_members table."""

    def __init__(self) -> None:
        self._rooms: Dict[str, List[Connection]] = {}
        self._seen: Dict[str, "OrderedDict[str, None]"] = {}
        self._lock = asyncio.Lock()

    async def add(self, conn: Connection) -> None:
        async with self._lock:
            self._rooms.setdefault(conn.room_id, []).append(conn)
            self._seen.setdefault(conn.room_id, OrderedDict())

    async def remove(self, conn: Connection) -> None:
        async with self._lock:
            peers = self._rooms.get(conn.room_id, [])
            self._rooms[conn.room_id] = [c for c in peers if c.connection_id != conn.connection_id]
            if not self._rooms[conn.room_id]:
                self._rooms.pop(conn.room_id, None)
                self._seen.pop(conn.room_id, None)

    def peers(self, room_id: str, exclude: Optional[str] = None) -> List[Connection]:
        return [c for c in self._rooms.get(room_id, []) if c.connection_id != exclude]

    def members(self, room_id: str) -> List[Connection]:
        return list(self._rooms.get(room_id, []))

    def online_user_ids(self, room_id: str) -> Set[str]:
        return {c.user_id for c in self._rooms.get(room_id, [])}

    def room_count(self) -> int:
        return len(self._rooms)

    def connection_count(self) -> int:
        return sum(len(v) for v in self._rooms.values())

    def snapshot(self) -> List[Dict[str, Any]]:
        return [
            {
                "room_id": room_id,
                "connections": [
                    {"username": c.username, "role": c.role, "connected_at": c.connected_at} for c in conns
                ],
            }
            for room_id, conns in sorted(self._rooms.items())
        ]

    def is_duplicate(self, room_id: str, message_id: str) -> bool:
        seen = self._seen.setdefault(room_id, OrderedDict())
        if message_id in seen:
            return True
        seen[message_id] = None
        while len(seen) > DEDUP_WINDOW:
            seen.popitem(last=False)
        return False


# ---------------------------------------------------------------------------


async def _send_error(websocket: WebSocket, code: str, detail: str, room_id: Optional[str] = None) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.send_text(
            json.dumps(
                make_envelope(
                    TYPE_ERROR,
                    message_id=str(uuid.uuid4()),
                    room_id=room_id,
                    payload={"code": code, "detail": detail},
                    server={"receive_wall_ns": time.time_ns()},
                )
            )
        )
    except (RuntimeError, WebSocketDisconnect, ConnectionError):
        pass


async def _read_json(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """One frame, or None if it was too large or not valid JSON. The size
    cap is applied to the raw text before parsing so an oversized frame can
    never be turned into objects."""
    raw = await websocket.receive_text()
    if len(raw.encode("utf-8", errors="ignore")) > MAX_MESSAGE_BYTES:
        await _send_error(websocket, "message_too_large", f"frames must be <= {MAX_MESSAGE_BYTES} bytes")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await _send_error(websocket, "bad_json", "frame was not valid JSON")
        return None
    return data if isinstance(data, dict) else None


@router.websocket("/ws/v1/rooms/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str) -> None:
    ctx = websocket.app.state.ctx
    await websocket.accept()

    conn = await _authenticate(websocket, ctx, room_id)
    if conn is None:
        return

    await ctx.rooms.add(conn)
    log.info("ws connect: %s (%s) -> room %s", conn.username, conn.role, room_id)
    await _broadcast_presence(ctx, room_id)

    try:
        while True:
            data = await _read_json(websocket)
            if data is None:
                continue
            await _handle_frame(ctx, conn, data)
    except WebSocketDisconnect:
        pass
    except (RuntimeError, ConnectionError) as exc:  # transport died mid-send
        log.debug("ws transport error for %s: %s", conn.username, exc)
    finally:
        await ctx.rooms.remove(conn)
        log.info("ws disconnect: %s from room %s", conn.username, room_id)
        await _broadcast_presence(ctx, room_id)


async def _authenticate(websocket: WebSocket, ctx, room_id: str) -> Optional[Connection]:
    try:
        data = await asyncio.wait_for(_read_json(websocket), timeout=15.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await _send_error(websocket, "auth_timeout", "no auth frame received")
        await websocket.close(code=4401)
        return None

    if not data or data.get("type") != TYPE_AUTH:
        await _send_error(websocket, "auth_required", "first frame must be an auth envelope")
        await websocket.close(code=4401)
        return None

    if int(data.get("v", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
        await _send_error(
            websocket, "protocol_version", f"server speaks protocol v{PROTOCOL_VERSION}, client sent v{data.get('v')}"
        )
        await websocket.close(code=4400)
        return None

    token = (data.get("payload") or {}).get("access_token", "")
    try:
        claims = ctx.tokens.verify(token)
    except AuthError as exc:
        # Note what failed, never the token itself.
        log.warning("ws auth rejected for room %s: %s", room_id, exc)
        await _send_error(websocket, "auth_failed", str(exc), room_id)
        await websocket.close(code=4401)
        return None

    user = dbm.get_user(ctx.db, claims.sub)
    room = dbm.get_room(ctx.db, room_id)
    if user is None or room is None:
        await _send_error(websocket, "not_found", "unknown user or room", room_id)
        await websocket.close(code=4404)
        return None
    if room["closed_at"] is not None:
        await _send_error(websocket, "room_closed", "that room has been closed", room_id)
        await websocket.close(code=4403)
        return None

    membership = dbm.get_membership(ctx.db, room_id, user["id"])
    if membership is None:
        log.warning("ws auth: %s is not a member of room %s", user["username"], room_id)
        await _send_error(websocket, "forbidden", "not a member of this room", room_id)
        await websocket.close(code=4403)
        return None

    conn = Connection(
        websocket=websocket,
        user_id=user["id"],
        username=user["username"],
        role=membership["role"],
        room_id=room_id,
    )
    await conn.send(
        make_envelope(
            TYPE_AUTH,
            message_id=str(uuid.uuid4()),
            room_id=room_id,
            payload={
                "status": "ok",
                "user_id": user["id"],
                "username": user["username"],
                "role": membership["role"],
                "is_owner": room["owner_id"] == user["id"],
                "connection_id": conn.connection_id,
            },
            server={"receive_wall_ns": time.time_ns(), "send_wall_ns": time.time_ns()},
        )
    )
    return conn


async def _broadcast_presence(ctx, room_id: str) -> None:
    members = ctx.rooms.members(room_id)
    payload = {
        "members": [
            {"username": c.username, "role": c.role, "user_id": c.user_id, "connected_at": c.connected_at}
            for c in members
        ]
    }
    envelope = make_envelope(
        TYPE_PRESENCE,
        message_id=str(uuid.uuid4()),
        room_id=room_id,
        payload=payload,
        server={"send_wall_ns": time.time_ns()},
    )
    for c in members:
        await c.send(envelope)


async def _handle_frame(ctx, conn: Connection, data: Dict[str, Any]) -> None:
    receive_ns = time.time_ns()

    msg_type = data.get("type")
    if msg_type not in MESSAGE_TYPES:
        await _send_error(conn.websocket, "unknown_type", f"unsupported message type {msg_type!r}", conn.room_id)
        return
    if int(data.get("v", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
        await _send_error(conn.websocket, "protocol_version", "protocol version mismatch", conn.room_id)
        return

    message_id = data.get("message_id") or str(uuid.uuid4())
    seq = int(data.get("seq", 0) or 0)

    # A frame may only ever be addressed to the room its socket belongs to.
    claimed_room = data.get("room_id")
    if claimed_room and claimed_room != conn.room_id:
        await _send_error(
            conn.websocket, "room_mismatch", "room_id does not match this connection's room", conn.room_id
        )
        return

    if msg_type == TYPE_HEARTBEAT:
        await conn.send(
            make_envelope(
                TYPE_HEARTBEAT,
                message_id=str(uuid.uuid4()),
                room_id=conn.room_id,
                session_id=data.get("session_id"),
                seq=seq,
                payload={"echo": (data.get("payload") or {}).get("token"), "client_sent_at_unix_ns": data.get("sent_at_unix_ns")},
                server={"receive_wall_ns": receive_ns, "send_wall_ns": time.time_ns()},
            )
        )
        return

    if msg_type in TEACHER_ONLY_TYPES and conn.role != dbm.ROLE_TEACHER:
        log.warning("refused %s from non-teacher %s in room %s", msg_type, conn.username, conn.room_id)
        await _send_error(conn.websocket, "forbidden", f"{msg_type} may only be sent by the teacher", conn.room_id)
        return

    duplicate = ctx.rooms.is_duplicate(conn.room_id, message_id)
    out_of_order = seq <= conn.last_seq and seq != 0
    if not out_of_order:
        conn.last_seq = max(conn.last_seq, seq)

    if duplicate:
        # Already relayed once. Tell the sender so it stops retrying, but
        # do not deliver the cue a second time. A latency probe still gets
        # its normal ack, since that is what the benchmark waits on.
        log.debug("duplicate message %s in room %s dropped", message_id, conn.room_id)
        ack_type = TYPE_LATENCY_ACK if msg_type == TYPE_LATENCY_PROBE else TYPE_ERROR
        payload: Dict[str, Any] = {"duplicate_of": message_id}
        if ack_type == TYPE_ERROR:
            payload |= {"code": "duplicate_message", "detail": "already relayed - not delivered again", "fatal": False}
        else:
            payload |= {"probe_message_id": message_id, "probe_id": (data.get("payload") or {}).get("probe_id")}
        await conn.send(
            make_envelope(
                ack_type,
                message_id=str(uuid.uuid4()),
                room_id=conn.room_id,
                session_id=data.get("session_id"),
                seq=seq,
                payload=payload,
                server={"receive_wall_ns": receive_ns, "duplicate": True},
            )
        )
        return

    send_ns = time.time_ns()
    server_block = {
        "receive_wall_ns": receive_ns,
        "send_wall_ns": send_ns,
        "sender_user_id": conn.user_id,
        "sender_username": conn.username,
        "sender_role": conn.role,
        "out_of_order": out_of_order,
    }

    outgoing = dict(data)
    outgoing["v"] = PROTOCOL_VERSION
    outgoing["message_id"] = message_id
    outgoing["room_id"] = conn.room_id
    outgoing["seq"] = seq
    outgoing["server"] = server_block
    # sent_at_unix_ns is left exactly as the client wrote it.

    for peer in ctx.rooms.peers(conn.room_id, exclude=conn.connection_id):
        await peer.send(outgoing)

    if msg_type == TYPE_LATENCY_PROBE:
        # Server-hop acknowledgement: lets a benchmark separate
        # teacher->server from server->student when a run looks slow.
        await conn.send(
            make_envelope(
                TYPE_LATENCY_ACK,
                message_id=str(uuid.uuid4()),
                room_id=conn.room_id,
                session_id=data.get("session_id"),
                seq=seq,
                payload={"probe_message_id": message_id, "probe_id": (data.get("payload") or {}).get("probe_id")},
                server={"receive_wall_ns": receive_ns, "send_wall_ns": time.time_ns()},
            )
        )

    ctx.enqueue_persist(_persist_row(conn, outgoing, msg_type, receive_ns, send_ns))


def _persist_row(conn: Connection, envelope: Dict[str, Any], msg_type: str, receive_ns: int, send_ns: int):
    """A (kind, row) pair for the background writer, or None for frames
    that are transport bookkeeping rather than session data."""
    # A student can become ready before the teacher has created the relay
    # session. Relay that announcement live, but persist only the repeat
    # carrying a session id so it can be attached unambiguously.
    if msg_type == TYPE_RECORDING_READY and not envelope.get("session_id"):
        return None
    if msg_type in GUIDANCE_PERSISTED_TYPES:
        kind = "guidance"
    elif msg_type in PERFORMANCE_PERSISTED_TYPES:
        kind = "performance"
    else:
        return None

    payload = envelope.get("payload") or {}
    row = {
        "id": envelope["message_id"],
        "session_id": envelope.get("session_id"),
        "room_id": conn.room_id,
        "sender_user_id": conn.user_id,
        "sender_role": conn.role,
        "seq": envelope.get("seq", 0),
        "type": msg_type,
        "sender_send_wall_ns": envelope.get("sent_at_unix_ns"),
        "server_receive_wall_ns": receive_ns,
        "server_send_wall_ns": send_ns,
        "payload": payload,
    }
    if kind == "performance":
        row["stage"] = payload.get("stage", "provisional")
        row["guidance_message_id"] = payload.get("guidance_message_id")
    return kind, row
