"""Talking to the relay: a REST client and a threaded WebSocket client.

Both are plain Python with no Qt import, for two reasons. It keeps them
unit-testable without a display, and it enforces the rule that matters
most for the GUIs: **a Qt window must never run a blocking network loop**.
The receive loop, the heartbeat and the reconnect backoff all live on
worker threads here; the windows attach through qt_bridge.py, which turns
these callbacks into Qt signals delivered on the GUI thread.

The WebSocket half is deliberately synchronous (websocket-client rather
than asyncio). A blocking recv on its own thread is the simplest thing
that can be reasoned about next to a Qt event loop - no second event loop
to keep alive, no cross-loop scheduling.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .protocol import (
    PROTOCOL_VERSION,
    TYPE_AUTH,
    TYPE_ERROR,
    TYPE_HEARTBEAT,
    make_envelope,
)

log = logging.getLogger("remote_guidance.network")

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_AUTHENTICATING = "authenticating"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"


class RemoteApiError(RuntimeError):
    """A REST call the relay refused. `status` is the HTTP code, or None
    if the server could not be reached at all."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """What a logged-in client holds. The password is not among it - it is
    used once, at login, and never stored or written to config.json."""

    access_token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    username: str = ""
    role: str = ""
    expires_at: float = 0.0

    @property
    def authenticated(self) -> bool:
        return bool(self.access_token)


class RemoteApiClient:
    """Thin stdlib HTTP client for /api/v1. urllib rather than a third
    party library so the client side adds no dependency the platform does
    not already need."""

    def __init__(self, base_url: str, verify_tls: bool = True, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.session = Session()

    # -- plumbing ------------------------------------------------------

    @property
    def api_base(self) -> str:
        return self.base_url if self.base_url.endswith("/api/v1") else f"{self.base_url}/api/v1"

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.verify_tls:
            return None  # urllib's default verification
        # Only ever reached when the operator has explicitly turned
        # verification off for a self-signed development certificate.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Any:
        url = f"{self.api_base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if authenticated and self.session.access_token:
            req.add_header("Authorization", f"Bearer {self.session.access_token}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context()) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            raise RemoteApiError(detail, status=exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteApiError(f"could not reach {url}: {exc}", status=None) from exc
        except json.JSONDecodeError as exc:
            raise RemoteApiError(f"malformed response from {url}: {exc}") from exc

    # -- auth ----------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return self.request("GET", "/health", authenticated=False)

    def _adopt(self, data: Dict[str, Any]) -> Session:
        self.session = Session(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            user_id=data["user_id"],
            username=data["username"],
            role=data["role"],
            expires_at=float(data.get("expires_at", 0)),
        )
        return self.session

    def register(self, username: str, password: str, role: str, display_name: Optional[str] = None) -> Session:
        return self._adopt(
            self.request(
                "POST",
                "/auth/register",
                {"username": username, "password": password, "role": role, "display_name": display_name},
                authenticated=False,
            )
        )

    def login(self, username: str, password: str) -> Session:
        return self._adopt(
            self.request("POST", "/auth/login", {"username": username, "password": password}, authenticated=False)
        )

    def refresh(self) -> Session:
        return self._adopt(
            self.request("POST", "/auth/refresh", {"refresh_token": self.session.refresh_token}, authenticated=False)
        )

    def logout(self) -> None:
        try:
            self.request("POST", "/auth/logout", {"refresh_token": self.session.refresh_token})
        except RemoteApiError as exc:
            log.debug("logout call failed (%s) - clearing the local session anyway", exc)
        self.session = Session()

    def me(self) -> Dict[str, Any]:
        return self.request("GET", "/auth/me")

    # -- rooms / recordings / sessions ---------------------------------

    def create_room(self, name: str) -> Dict[str, Any]:
        return self.request("POST", "/rooms", {"name": name})

    def join_room(self, join_code: str) -> Dict[str, Any]:
        return self.request("POST", "/rooms/join", {"join_code": join_code})

    def get_room(self, room_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/rooms/{room_id}")

    def close_room(self, room_id: str) -> Dict[str, Any]:
        return self.request("POST", f"/rooms/{room_id}/close")

    def room_members(self, room_id: str) -> List[Dict[str, Any]]:
        return self.request("GET", f"/rooms/{room_id}/members")

    def upload_recording(self, room_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", f"/rooms/{room_id}/recordings", payload)

    def list_recordings(self, room_id: str) -> List[Dict[str, Any]]:
        return self.request("GET", f"/rooms/{room_id}/recordings")

    def get_recording(self, recording_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/recordings/{recording_id}")

    def create_session(self, room_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", f"/rooms/{room_id}/sessions", payload)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/sessions/{session_id}")

    def session_events(self, session_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/sessions/{session_id}/events")

    def session_summary(self, session_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/sessions/{session_id}/summary")


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8"))
        detail = body.get("detail", body)
        if isinstance(detail, list) and detail:  # pydantic validation errors
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        return f"{exc.code} {exc.reason}: {detail}"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, AttributeError):
        return f"{exc.code} {exc.reason}"


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@dataclass
class ClientCallbacks:
    """Called from the client's worker threads, never from a GUI thread.
    Anything that touches widgets must hop threads first - qt_bridge.py
    does that with signals."""

    on_message: Optional[Callable[[Dict[str, Any]], None]] = None
    on_state: Optional[Callable[[str, str], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class RemoteWebSocketClient:
    """One persistent authenticated connection to a room.

    Owns three threads at most: the receive loop, a heartbeat timer, and
    (briefly) the reconnect backoff. `send()` is safe to call from any
    thread; frames are serialised behind a lock.

    Reconnection is automatic with exponential backoff up to
    reconnect_max_delay_s. On reconnect the auth handshake is repeated -
    the sequence counter keeps rising, so the relay can tell a genuine
    retransmission (same message_id, dropped) from a new event."""

    def __init__(
        self,
        ws_url: str,
        access_token: str,
        room_id: str,
        callbacks: Optional[ClientCallbacks] = None,
        verify_tls: bool = True,
        heartbeat_interval_s: float = 10.0,
        reconnect_initial_delay_s: float = 1.0,
        reconnect_max_delay_s: float = 30.0,
        auto_reconnect: bool = True,
    ):
        self.ws_url = ws_url
        self.access_token = access_token
        self.room_id = room_id
        self.callbacks = callbacks or ClientCallbacks()
        self.verify_tls = verify_tls
        self.heartbeat_interval_s = heartbeat_interval_s
        self.reconnect_initial_delay_s = reconnect_initial_delay_s
        self.reconnect_max_delay_s = reconnect_max_delay_s
        self.auto_reconnect = auto_reconnect

        self.state = STATE_DISCONNECTED
        self.session_id: Optional[str] = None

        self._ws = None
        self._send_lock = threading.Lock()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        self._authed = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._rx_thread is not None and self._rx_thread.is_alive():
            return
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._run, name="rg-ws-rx", daemon=True)
        self._rx_thread.start()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="rg-ws-hb", daemon=True)
        self._hb_thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Idempotent, and safe to call from a window's closeEvent."""
        self._stop.set()
        self._authed.clear()
        with self._send_lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - the socket is going away regardless
                pass
        for thread in (self._rx_thread, self._hb_thread):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=timeout)
        self._rx_thread = None
        self._hb_thread = None
        self._set_state(STATE_DISCONNECTED, "stopped")

    def wait_until_connected(self, timeout: float = 15.0) -> bool:
        return self._authed.wait(timeout=timeout)

    @property
    def connected(self) -> bool:
        return self._authed.is_set()

    # -- sending -------------------------------------------------------

    def next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def send(
        self,
        type_: str,
        payload: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build, send and return the envelope (so the caller can record
        the message_id and its own send-time). None if the socket is not
        up - callers decide whether that is fatal; nothing is queued
        silently behind the scenes."""
        envelope = make_envelope(
            type_,
            room_id=self.room_id,
            session_id=session_id if session_id is not None else self.session_id,
            seq=seq if seq is not None else self.next_seq(),
            payload=payload,
            message_id=message_id,
        )
        return envelope if self.send_envelope(envelope) else None

    def send_envelope(self, envelope: Dict[str, Any]) -> bool:
        with self._send_lock:
            ws = self._ws
            if ws is None:
                return False
            try:
                ws.send(json.dumps(envelope, ensure_ascii=False))
                return True
            except Exception as exc:  # noqa: BLE001 - any transport failure
                log.debug("send failed: %s", exc)
                return False

    # -- internals -----------------------------------------------------

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if self.callbacks.on_state:
            try:
                self.callbacks.on_state(state, detail)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the loop
                log.exception("on_state callback raised")

    def _emit_error(self, detail: str) -> None:
        log.warning("%s", detail)
        if self.callbacks.on_error:
            try:
                self.callbacks.on_error(detail)
            except Exception:  # noqa: BLE001
                log.exception("on_error callback raised")

    def _sslopt(self) -> Dict[str, Any]:
        if self.verify_tls:
            return {}
        return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}

    def _run(self) -> None:
        delay = self.reconnect_initial_delay_s
        while not self._stop.is_set():
            try:
                self._connect_once()
                delay = self.reconnect_initial_delay_s  # a clean run resets backoff
            except Exception as exc:  # noqa: BLE001 - keep retrying whatever broke
                # stop() closes the socket specifically to wake recv().
                # Some backends report that expected wake-up as EBADF;
                # it is normal shutdown, not a connection failure.
                if not self._stop.is_set():
                    self._emit_error(f"connection failed: {exc}")
                    self._set_state(STATE_ERROR, str(exc))
            finally:
                self._authed.clear()
                with self._send_lock:
                    self._ws = None

            if self._stop.is_set() or not self.auto_reconnect:
                break
            self._set_state(STATE_DISCONNECTED, f"reconnecting in {delay:.0f}s")
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.reconnect_max_delay_s)

        self._set_state(STATE_DISCONNECTED, "closed")

    def _connect_once(self) -> None:
        import websocket  # websocket-client; imported here so tests can stub it

        self._set_state(STATE_CONNECTING, self.ws_url)
        ws = websocket.create_connection(
            self.ws_url,
            sslopt=self._sslopt(),
            timeout=30,
            enable_multithread=True,
        )
        with self._send_lock:
            self._ws = ws

        # The access token goes in the first frame's payload, never in the
        # URL - a query string would be recorded by proxies and history.
        self._set_state(STATE_AUTHENTICATING, "sending auth frame")
        ws.send(json.dumps(make_envelope(TYPE_AUTH, room_id=self.room_id, payload={"access_token": self.access_token}), ensure_ascii=False))

        try:
            while not self._stop.is_set():
                raw = ws.recv()
                if raw is None or raw == "":
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    self._emit_error("received a frame that was not valid JSON")
                    continue
                self._on_envelope(envelope)
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _on_envelope(self, envelope: Dict[str, Any]) -> None:
        msg_type = envelope.get("type")

        if int(envelope.get("v", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
            self._emit_error(
                f"protocol mismatch: this client speaks v{PROTOCOL_VERSION}, the server sent v{envelope.get('v')}"
            )

        if msg_type == TYPE_AUTH:
            payload = envelope.get("payload") or {}
            if payload.get("status") == "ok":
                self._authed.set()
                self._set_state(STATE_CONNECTED, f"as {payload.get('username')} ({payload.get('role')})")
            else:
                self._emit_error(f"authentication refused: {payload}")
        elif msg_type == TYPE_ERROR:
            payload = envelope.get("payload") or {}
            self._emit_error(f"server error [{payload.get('code')}]: {payload.get('detail')}")

        if self.callbacks.on_message:
            try:
                self.callbacks.on_message(envelope)
            except Exception:  # noqa: BLE001 - one bad handler must not drop the connection
                log.exception("on_message callback raised")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            if self._authed.is_set():
                self.send(TYPE_HEARTBEAT, {"token": str(time.time_ns())})
