"""REST surface, all under /api/v1.

Authorisation is checked in one place per resource rather than per
handler body: `require_member` resolves the caller's membership row for a
room and raises 403 if there isn't one, and `require_teacher_of` on top
of it refuses anyone who is not that room's owning teacher. A teacher can
therefore never drive another teacher's room, and a student can never
issue teacher-only controls, whether they come in over REST or over the
WebSocket (see websocket.TEACHER_ONLY_TYPES, which applies the same
rules to frames).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__, database as dbm
from .auth import AuthError, TOKEN_TYPE_REFRESH, hash_password, verify_password
from .schemas import (
    CreateRecordingRequest,
    CreateRoomRequest,
    CreateSessionRequest,
    HealthResponse,
    JoinRoomRequest,
    LoginRequest,
    LogoutRequest,
    MemberResponse,
    RecordingDetail,
    RecordingEventModel,
    RecordingSummary,
    RefreshRequest,
    RegisterRequest,
    RoomResponse,
    SessionListEntry,
    SessionListResponse,
    SessionResponse,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/api/v1")
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


def get_ctx(request: Request):
    return request.app.state.ctx


def current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> sqlite3.Row:
    ctx = get_ctx(request)
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = ctx.tokens.verify(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    user = dbm.get_user(ctx.db, claims.sub)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    return user


def _room_or_404(ctx, room_id: str) -> sqlite3.Row:
    room = dbm.get_room(ctx.db, room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "room not found")
    return room


def require_member(ctx, room_id: str, user: sqlite3.Row) -> sqlite3.Row:
    room = _room_or_404(ctx, room_id)
    membership = dbm.get_membership(ctx.db, room_id, user["id"])
    if membership is None:
        # Deliberately 403 and not 404: the caller proved who they are,
        # and hiding existence buys nothing once join codes are unguessable.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this room")
    return room


def require_teacher_of(ctx, room_id: str, user: sqlite3.Row) -> sqlite3.Row:
    room = require_member(ctx, room_id, user)
    if room["owner_id"] != user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the room's own teacher may do this")
    return room


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    ctx = get_ctx(request)
    return HealthResponse(
        status="ok",
        server_version=__version__,
        schema_version=ctx.db.schema_version,
        server_time_unix_ns=time.time_ns(),
        rooms_online=ctx.rooms.room_count(),
        connections=ctx.rooms.connection_count(),
    )


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def _token_response(ctx, user: sqlite3.Row) -> TokenResponse:
    pair = ctx.tokens.issue_pair(user["id"], user["role"])
    return TokenResponse(
        access_token=pair["access_token"],
        refresh_token=pair["refresh_token"],
        expires_at=pair["expires_at"],
        refresh_expires_at=pair["refresh_expires_at"],
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
    )


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(request: Request, body: RegisterRequest) -> TokenResponse:
    ctx = get_ctx(request)
    # A closed server still lets the very first account through, otherwise
    # allow_registration=false would lock an operator out of a fresh database.
    if not ctx.config.allow_registration and dbm.user_count(ctx.db) > 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is disabled on this server")
    if dbm.get_user_by_username(ctx.db, body.username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    try:
        password_hash = hash_password(body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    user_id = dbm.create_user(ctx.db, body.username, password_hash, body.role, body.display_name)
    ctx.log.info("registered user %s (%s)", body.username, body.role)
    return _token_response(ctx, dbm.get_user(ctx.db, user_id))


@router.post("/auth/login", response_model=TokenResponse)
def login(request: Request, body: LoginRequest) -> TokenResponse:
    ctx = get_ctx(request)
    user = dbm.get_user_by_username(ctx.db, body.username)
    # Same message for "no such user" and "wrong password", so the endpoint
    # can't be used to enumerate accounts.
    if user is None or not verify_password(user["password_hash"], body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    ctx.log.info("login: %s", body.username)
    return _token_response(ctx, user)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh(request: Request, body: RefreshRequest) -> TokenResponse:
    ctx = get_ctx(request)
    try:
        claims = ctx.tokens.verify(body.refresh_token, expected_type=TOKEN_TYPE_REFRESH)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    user = dbm.get_user(ctx.db, claims.sub)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    # Rotate: the presented refresh token is spent, so a stolen copy is
    # useless once the legitimate client has refreshed once.
    dbm.revoke_token(ctx.db, claims.jti, claims.sub, claims.exp)
    return _token_response(ctx, user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    body: LogoutRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    ctx = get_ctx(request)
    if credentials is not None and credentials.credentials:
        try:
            ctx.tokens.revoke(credentials.credentials)
        except AuthError:
            pass  # already invalid - nothing left to revoke
    if body.refresh_token:
        try:
            ctx.tokens.revoke(body.refresh_token, expected_type=TOKEN_TYPE_REFRESH)
        except AuthError:
            pass
    ctx.log.info("logout")


@router.get("/auth/me", response_model=UserResponse)
def me(user: sqlite3.Row = Depends(current_user)) -> UserResponse:
    return UserResponse(
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        display_name=user["display_name"],
        created_at=user["created_at"],
    )


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------


def _room_response(room: sqlite3.Row, my_role: Optional[str], include_code: bool) -> RoomResponse:
    return RoomResponse(
        room_id=room["id"],
        name=room["name"],
        join_code=room["join_code"] if include_code else None,
        owner_id=room["owner_id"],
        created_at=room["created_at"],
        closed_at=room["closed_at"],
        my_role=my_role,
    )


@router.post("/rooms", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
def create_room(request: Request, body: CreateRoomRequest, user: sqlite3.Row = Depends(current_user)) -> RoomResponse:
    ctx = get_ctx(request)
    if user["role"] != dbm.ROLE_TEACHER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only a teacher can create a room")
    room = dbm.create_room(ctx.db, body.name, user["id"])
    ctx.log.info("room %s created by %s", room["id"], user["username"])
    return _room_response(room, dbm.ROLE_TEACHER, include_code=True)


@router.post("/rooms/join", response_model=RoomResponse)
def join_room(request: Request, body: JoinRoomRequest, user: sqlite3.Row = Depends(current_user)) -> RoomResponse:
    ctx = get_ctx(request)
    room = dbm.get_room_by_code(ctx.db, body.join_code)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no room with that join code")
    if room["closed_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that room has been closed")
    if room["owner_id"] == user["id"]:
        return _room_response(room, dbm.ROLE_TEACHER, include_code=True)
    dbm.add_room_member(ctx.db, room["id"], user["id"], user["role"])
    ctx.log.info("%s joined room %s as %s", user["username"], room["id"], user["role"])
    return _room_response(room, user["role"], include_code=False)


@router.get("/rooms/{room_id}", response_model=RoomResponse)
def get_room(request: Request, room_id: str, user: sqlite3.Row = Depends(current_user)) -> RoomResponse:
    ctx = get_ctx(request)
    room = require_member(ctx, room_id, user)
    membership = dbm.get_membership(ctx.db, room_id, user["id"])
    return _room_response(room, membership["role"], include_code=room["owner_id"] == user["id"])


@router.post("/rooms/{room_id}/close", response_model=RoomResponse)
def close_room(request: Request, room_id: str, user: sqlite3.Row = Depends(current_user)) -> RoomResponse:
    ctx = get_ctx(request)
    require_teacher_of(ctx, room_id, user)
    dbm.close_room(ctx.db, room_id)
    ctx.log.info("room %s closed", room_id)
    return _room_response(dbm.get_room(ctx.db, room_id), dbm.ROLE_TEACHER, include_code=True)


@router.get("/rooms/{room_id}/members", response_model=List[MemberResponse])
def room_members(request: Request, room_id: str, user: sqlite3.Row = Depends(current_user)) -> List[MemberResponse]:
    ctx = get_ctx(request)
    require_member(ctx, room_id, user)
    online = ctx.rooms.online_user_ids(room_id)
    return [
        MemberResponse(
            user_id=row["user_id"],
            username=row["username"],
            display_name=row["display_name"],
            role=row["role"],
            joined_at=row["joined_at"],
            online=row["user_id"] in online,
        )
        for row in dbm.list_room_members(ctx.db, room_id)
    ]


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------


@router.post("/rooms/{room_id}/recordings", response_model=RecordingSummary, status_code=status.HTTP_201_CREATED)
def create_recording(
    request: Request,
    room_id: str,
    body: CreateRecordingRequest,
    user: sqlite3.Row = Depends(current_user),
) -> RecordingSummary:
    ctx = get_ctx(request)
    require_teacher_of(ctx, room_id, user)
    if not body.events:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a recording needs at least one event")
    recording_id = dbm.create_recording(
        ctx.db,
        room_id,
        user["id"],
        body.name,
        body.source,
        [e.model_dump() for e in body.events],
        body.meta,
    )
    ctx.log.info("recording %s (%d events) uploaded to room %s", recording_id, len(body.events), room_id)
    return _recording_summary(dbm.get_recording(ctx.db, recording_id))


def _recording_summary(row: sqlite3.Row) -> RecordingSummary:
    return RecordingSummary(
        recording_id=row["id"],
        room_id=row["room_id"],
        name=row["name"],
        source=row["source"],
        event_count=row["event_count"],
        duration_s=row["duration_s"],
        created_at=row["created_at"],
        uploaded_by=row["uploaded_by"],
    )


@router.get("/rooms/{room_id}/recordings", response_model=List[RecordingSummary])
def list_recordings(request: Request, room_id: str, user: sqlite3.Row = Depends(current_user)) -> List[RecordingSummary]:
    ctx = get_ctx(request)
    require_member(ctx, room_id, user)
    return [_recording_summary(row) for row in dbm.list_recordings(ctx.db, room_id)]


@router.get("/recordings/{recording_id}", response_model=RecordingDetail)
def get_recording(request: Request, recording_id: str, user: sqlite3.Row = Depends(current_user)) -> RecordingDetail:
    """The student downloads the whole event list here *before* playback
    starts, so every cue is scheduled from local storage and no single
    note ever waits on the network."""
    ctx = get_ctx(request)
    row = dbm.get_recording(ctx.db, recording_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found")
    require_member(ctx, row["room_id"], user)
    events = [
        RecordingEventModel(
            event_order=e["event_order"],
            rel_time_s=e["rel_time_s"],
            duration_s=e["duration_s"],
            note=e["note"],
            note_name=e["note_name"],
            key_id=e["key_id"],
            finger=e["finger"],
        )
        for e in dbm.get_recording_events(ctx.db, recording_id)
    ]
    summary = _recording_summary(row)
    return RecordingDetail(**summary.model_dump(), meta=json.loads(row["meta_json"] or "{}"), events=events)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def _session_response(row: sqlite3.Row) -> SessionResponse:
    return SessionResponse(
        session_id=row["id"],
        room_id=row["room_id"],
        created_by=row["created_by"],
        mode=row["mode"],
        guidance_mode=row["guidance_mode"],
        playback_mode=row["playback_mode"],
        recording_id=row["recording_id"],
        state=row["state"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


@router.post("/rooms/{room_id}/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    request: Request,
    room_id: str,
    body: CreateSessionRequest,
    user: sqlite3.Row = Depends(current_user),
) -> SessionResponse:
    ctx = get_ctx(request)
    require_teacher_of(ctx, room_id, user)
    if body.mode == "recording":
        if not body.recording_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "a recording session needs recording_id")
        recording = dbm.get_recording(ctx.db, body.recording_id)
        if recording is None or recording["room_id"] != room_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found in this room")
    session_id = dbm.create_session(
        ctx.db, room_id, user["id"], body.mode, body.guidance_mode, body.playback_mode, body.recording_id
    )
    ctx.log.info("session %s created in room %s (%s/%s)", session_id, room_id, body.mode, body.guidance_mode)
    return _session_response(dbm.get_session(ctx.db, session_id))


def _session_or_403(ctx, session_id: str, user: sqlite3.Row) -> sqlite3.Row:
    session = dbm.get_session(ctx.db, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    require_member(ctx, session["room_id"], user)
    return session


# --- listing and bulk export ----------------------------------------------
# Everything above can only be reached by a caller that already knows a
# session id. That is enough for a client driving its own lesson and not
# enough to get a finished study's timing data off a deployed server: the
# ids live in whatever the clients happened to write locally, and a lost
# folder means the rows on the server are unreachable without a shell on
# the box. These two endpoints close that gap and nothing more - both are
# read-only, and both are scoped by room membership exactly like the
# single-session routes.

SESSION_LIST_DEFAULT = 200
SESSION_LIST_MAX = 1000
SESSION_EXPORT_DEFAULT = 100
SESSION_EXPORT_MAX = 500


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    room_id: Optional[str] = Query(default=None, description="restrict to one room the caller belongs to"),
    since: Optional[float] = Query(default=None, description="unix seconds; sessions created at or after this"),
    state: Optional[str] = Query(default=None, description="created, running, paused or finished"),
    limit: int = Query(default=SESSION_LIST_DEFAULT, ge=1, le=SESSION_LIST_MAX),
    user: sqlite3.Row = Depends(current_user),
) -> SessionListResponse:
    """Sessions the caller can see, newest first, with event counts."""
    ctx = get_ctx(request)
    if room_id is not None:
        # Explicit 403 for a room the caller is not in, rather than the
        # empty list the membership join would otherwise return.
        require_member(ctx, room_id, user)
    # One extra row is what distinguishes "exactly limit matched" from
    # "more matched and were cut off".
    rows = dbm.list_sessions_for_user(
        ctx.db, user["id"], room_id=room_id, since=since, state=state, limit=limit + 1
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    return SessionListResponse(
        count=len(rows),
        limit=limit,
        truncated=truncated,
        sessions=[
            SessionListEntry(
                **_session_response(row).model_dump(),
                room_name=row["room_name"],
                guidance_event_count=row["guidance_event_count"],
                performance_event_count=row["performance_event_count"],
            )
            for row in rows
        ],
    )


# Declared before /sessions/{session_id} on purpose: FastAPI matches in
# registration order, so the path parameter would otherwise swallow the
# literal "export" and every call here would 404 as a missing session.
@router.get("/sessions/export")
def export_sessions(
    request: Request,
    session_ids: Optional[str] = Query(
        default=None, description="comma-separated session ids; when given, the filters below are ignored"
    ),
    room_id: Optional[str] = Query(default=None),
    since: Optional[float] = Query(default=None, description="unix seconds; sessions created at or after this"),
    state: Optional[str] = Query(default=None),
    limit: int = Query(default=SESSION_EXPORT_DEFAULT, ge=1, le=SESSION_EXPORT_MAX),
    user: sqlite3.Row = Depends(current_user),
) -> Dict[str, Any]:
    """Every guidance and performance event of many sessions in one reply.

    The payloads are returned verbatim, exactly as `/sessions/{id}/events`
    returns them one session at a time - in particular the per-cue
    `timings` dictionary the student sends with each performance row. The
    server does not summarise or re-derive anything here.
    """
    ctx = get_ctx(request)
    truncated = False

    if session_ids is not None:
        wanted: List[str] = []
        for raw in session_ids.split(","):
            sid = raw.strip()
            # Dropping duplicates keeps the reply's shape predictable and
            # stops a repeated id from being counted twice against limit.
            if sid and sid not in wanted:
                wanted.append(sid)
        if len(wanted) > limit:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"asked for {len(wanted)} sessions; limit is {limit}"
            )
        # Authorise each one exactly as the single-session routes do, so an
        # id from another teacher's room is a 403 and not a silent omission.
        sessions = [_session_or_403(ctx, sid, user) for sid in wanted]
    else:
        if room_id is not None:
            require_member(ctx, room_id, user)
        rows = dbm.list_sessions_for_user(
            ctx.db, user["id"], room_id=room_id, since=since, state=state, limit=limit + 1
        )
        truncated = len(rows) > limit
        sessions = rows[:limit]

    events = dbm.list_events_for_sessions(ctx.db, [s["id"] for s in sessions])
    return {
        "exported_at": time.time(),
        "server_version": __version__,
        "schema_version": ctx.db.schema_version,
        "count": len(sessions),
        "limit": limit,
        "truncated": truncated,
        "sessions": [
            {
                "session": _session_response(session).model_dump(),
                "guidance": [_event_row(r) for r in events[session["id"]]["guidance"]],
                "performance": [_event_row(r) for r in events[session["id"]]["performance"]],
            }
            for session in sessions
        ],
    }


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(request: Request, session_id: str, user: sqlite3.Row = Depends(current_user)) -> SessionResponse:
    return _session_response(_session_or_403(get_ctx(request), session_id, user))


@router.get("/sessions/{session_id}/events")
def session_events(request: Request, session_id: str, user: sqlite3.Row = Depends(current_user)) -> Dict[str, Any]:
    ctx = get_ctx(request)
    _session_or_403(ctx, session_id, user)
    rows = dbm.list_session_events(ctx.db, session_id)
    return {
        "session_id": session_id,
        "guidance": [_event_row(r) for r in rows["guidance"]],
        "performance": [_event_row(r) for r in rows["performance"]],
    }


def _event_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return data


@router.get("/sessions/{session_id}/summary")
def session_summary(request: Request, session_id: str, user: sqlite3.Row = Depends(current_user)) -> Dict[str, Any]:
    """Whatever the student's own scoring last reported, plus plain counts
    of what the relay saw.

    The server never re-derives accuracy. Finger matching and the
    accuracy formulas live in app.finger_matching / app.quiz.summarize on
    the client, and a summary here is the student's final (or, until then,
    provisional) result stored verbatim - so there can only ever be one
    definition of correctness in the system."""
    ctx = get_ctx(request)
    session = _session_or_403(ctx, session_id, user)
    rows = dbm.list_session_events(ctx.db, session_id)

    guidance = rows["guidance"]
    performance = [_event_row(r) for r in rows["performance"]]
    finals = [p for p in performance if p.get("stage") == "final"]
    provisionals = [p for p in performance if p.get("stage") == "provisional"]

    # The student sends its own summarize() output on session.finished;
    # take the most recent one verbatim.
    reported: Optional[Dict[str, Any]] = None
    for row in reversed(guidance):
        if row["type"] == "session.finished":
            reported = json.loads(row["payload_json"] or "{}").get("summary")
            break

    return {
        "session": _session_response(session).model_dump(),
        "counts": {
            "guidance_events": len(guidance),
            "performance_events": len(performance),
            "provisional": len(provisionals),
            "final": len(finals),
        },
        # Named to make its provenance unmistakable in any consumer.
        "student_reported_summary": reported,
        "summary_source": "student" if reported else None,
    }
