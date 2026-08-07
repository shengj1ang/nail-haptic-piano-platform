"""FastAPI application factory and the shared server context.

The context holds the four things every handler needs (config, database,
token service, live room registry) plus the background writer that keeps
SQLite out of the relay's latency path: the WebSocket handler forwards a
frame and then hands the row to `enqueue_persist`, which a single worker
task drains into the database off the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional, Tuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__, api, database as dbm, websocket as ws
from .auth import TokenService
from .config import ServerConfig
from .database import Database

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT)


class ServerContext:
    def __init__(self, config: ServerConfig, db: Optional[Database] = None):
        self.config = config
        self.db = db or Database(config.database_file)
        self.tokens = TokenService(config, self.db)
        self.tokens.ensure_keys()
        self.rooms = ws.RoomRegistry()
        self.log = logging.getLogger("remote_guidance.server")
        self._persist_queue: "asyncio.Queue[Tuple[str, dict]]" = asyncio.Queue(maxsize=10000)
        self._persist_task: Optional[asyncio.Task] = None

    # -- background persistence ---------------------------------------

    def enqueue_persist(self, item: Optional[Tuple[str, dict]]) -> None:
        if item is None:
            return
        try:
            self._persist_queue.put_nowait(item)
        except asyncio.QueueFull:
            # Losing an audit row is bad; blocking the relay is worse.
            # Say so loudly rather than silently dropping.
            self.log.error("persist queue full - dropping a %s row", item[0])

    async def start(self) -> None:
        if self._persist_task is None:
            self._persist_task = asyncio.create_task(self._persist_worker())

    async def stop(self) -> None:
        if self._persist_task is not None:
            self._persist_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._persist_task
            self._persist_task = None
        await self.drain()

    async def drain(self) -> None:
        """Flush anything still queued - used on shutdown and by tests
        that assert on what reached the database."""
        while not self._persist_queue.empty():
            kind, row = self._persist_queue.get_nowait()
            await asyncio.to_thread(self._write_row, kind, row)

    async def _persist_worker(self) -> None:
        while True:
            kind, row = await self._persist_queue.get()
            try:
                await asyncio.to_thread(self._write_row, kind, row)
            except Exception:  # never let one bad row kill the writer
                self.log.exception("failed to persist a %s row", kind)

    def _write_row(self, kind: str, row: dict) -> None:
        if kind == "guidance":
            dbm.insert_guidance_event(self.db, row)
            self._apply_session_state(row)
        elif kind == "performance":
            dbm.insert_performance_event(self.db, row)
            self._apply_student_guidance_mode(row)

    def _apply_session_state(self, row: dict) -> None:
        session_id = row.get("session_id")
        if not session_id:
            return
        state = {
            ws.TYPE_SESSION_START: "running",
            ws.TYPE_RECORDING_START: "running",
            ws.TYPE_SESSION_RESUME: "running",
            ws.TYPE_SESSION_PAUSE: "paused",
            ws.TYPE_RECORDING_PAUSE: "paused",
            ws.TYPE_SESSION_STOP: "stopped",
            ws.TYPE_RECORDING_STOP: "stopped",
            ws.TYPE_SESSION_FINISHED: "finished",
        }.get(row.get("type"))
        if state and dbm.get_session(self.db, session_id) is not None:
            dbm.set_session_state(self.db, session_id, state)

    def _apply_student_guidance_mode(self, row: dict) -> None:
        """Store the modality reported by the student, never a teacher default."""
        if row.get("type") != ws.TYPE_RECORDING_READY or row.get("sender_role") != dbm.ROLE_STUDENT:
            return
        guidance_mode = (row.get("payload") or {}).get("guidance_mode")
        if guidance_mode not in {"visual", "haptic", "both"}:
            return
        session_id = row.get("session_id")
        session = dbm.get_session(self.db, session_id) if session_id else None
        if session is not None and session["room_id"] == row.get("room_id"):
            dbm.set_session_guidance_mode(self.db, session_id, guidance_mode)


def create_app(config: Optional[ServerConfig] = None, db: Optional[Database] = None) -> FastAPI:
    config = config or ServerConfig.load()
    ctx = ServerContext(config, db=db)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        await ctx.start()
        dbm.purge_expired_revocations(ctx.db)
        ctx.log.info(
            "relay ready on %s (db=%s, schema v%d, tls=%s)",
            config.http_base_url(),
            config.database_file,
            ctx.db.schema_version,
            "on" if config.tls.enabled else "off",
        )
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(
        title="Remote Guidance Relay",
        version=__version__,
        description=(
            "Relay server for the tele-training module: room routing, guidance/performance "
            "event storage and latency probes. No camera, MIDI or actuator code runs here - "
            "every device stays on a client machine."
        ),
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.include_router(api.router)
    app.include_router(ws.router)

    @app.get("/", include_in_schema=False)
    def _root() -> JSONResponse:
        return JSONResponse(
            {
                "service": "remote-guidance-relay",
                "version": __version__,
                "health": "/api/v1/health",
                "docs": "/docs",
            }
        )

    return app


def build_uvicorn_kwargs(config: ServerConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": config.bind_host,
        "port": config.port,
        # wsproto rather than the websockets package: it is the WebSocket
        # implementation this project's environment ships with.
        "ws": "wsproto",
        "log_level": "info",
    }
    if config.tls.enabled:
        certfile, keyfile = config.tls_certfile, config.tls_keyfile
        if not certfile or not keyfile:
            raise SystemExit("tls.enabled is true but tls.certfile/tls.keyfile are not both set")
        if not certfile.exists() or not keyfile.exists():
            raise SystemExit(f"TLS certificate or key not found: {certfile}, {keyfile}")
        kwargs["ssl_certfile"] = str(certfile)
        kwargs["ssl_keyfile"] = str(keyfile)
    return kwargs
