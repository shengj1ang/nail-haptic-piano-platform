"""The one place the network layer and Qt meet.

A window never runs a receive loop, never blocks on a socket and never
calls into websocket-client directly. It owns a RemoteClientBridge, calls
its plain methods to send, and connects to its signals to be told what
arrived.

Why this is safe: Qt signals emitted from a non-GUI thread to a receiver
living in the GUI thread are delivered as *queued* connections - the slot
runs on the receiver's thread, on the next pass of its event loop. That
is the supported way to hand data from a worker thread to widgets, so all
the callbacks fired on the network client's threads become ordinary
GUI-thread slot calls here and nothing touches a widget off-thread.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal

from .network_client import ClientCallbacks, RemoteWebSocketClient


class RemoteClientBridge(QObject):
    """Owns a RemoteWebSocketClient and re-emits its callbacks as signals.

    messageReceived carries the whole envelope (dict). Windows filter on
    `type` rather than getting one signal per message type, so adding a
    protocol message does not mean adding a signal here."""

    messageReceived = Signal(dict)
    stateChanged = Signal(str, str)  # state, detail
    errorOccurred = Signal(str)

    def __init__(
        self,
        ws_url: str,
        access_token: str,
        room_id: str,
        verify_tls: bool = True,
        heartbeat_interval_s: float = 10.0,
        reconnect_initial_delay_s: float = 1.0,
        reconnect_max_delay_s: float = 30.0,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.client = RemoteWebSocketClient(
            ws_url=ws_url,
            access_token=access_token,
            room_id=room_id,
            callbacks=ClientCallbacks(
                on_message=self._on_message,
                on_state=self._on_state,
                on_error=self._on_error,
            ),
            verify_tls=verify_tls,
            heartbeat_interval_s=heartbeat_interval_s,
            reconnect_initial_delay_s=reconnect_initial_delay_s,
            reconnect_max_delay_s=reconnect_max_delay_s,
        )

    # -- worker-thread callbacks: emit only, never touch widgets -------

    def _on_message(self, envelope: Dict[str, Any]) -> None:
        self.messageReceived.emit(envelope)

    def _on_state(self, state: str, detail: str) -> None:
        self.stateChanged.emit(state, detail)

    def _on_error(self, detail: str) -> None:
        self.errorOccurred.emit(detail)

    # -- GUI-thread API ------------------------------------------------

    def start(self) -> None:
        self.client.start()

    def stop(self) -> None:
        self.client.stop()

    @property
    def connected(self) -> bool:
        return self.client.connected

    @property
    def session_id(self) -> Optional[str]:
        return self.client.session_id

    @session_id.setter
    def session_id(self, value: Optional[str]) -> None:
        self.client.session_id = value

    def send(
        self,
        type_: str,
        payload: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Non-blocking: serialises one frame onto the already-open socket
        and returns the envelope that was sent (or None if the connection
        is currently down)."""
        return self.client.send(type_, payload, session_id=session_id, message_id=message_id)
