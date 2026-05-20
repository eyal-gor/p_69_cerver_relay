"""
Cerver-owned websocket transport for private local compute.

This opens an outbound websocket from the local runtime to Cerver so the
hosted gateway can forward provider requests back to the local machine.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import websockets

from ..kompany_local_transport.relay_forwarding import execute_local_request

INITIAL_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0
RECONNECT_BACKOFF_MULTIPLIER = 2.0

StatusCallback = Callable[[str], None]
ConnectedCallback = Callable[[Dict[str, Any]], None]


def build_cerver_connect_ws_url(cerver_url: str, compute_id: str, api_token: str = "") -> str:
    base = cerver_url.strip().rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = base

    return f"{ws_base}/v2/connect/ws?compute_id={quote(compute_id)}&token={quote(api_token)}"


class CerverConnectTransport:
    def __init__(
        self,
        *,
        cerver_url: str,
        api_token: str,
        compute_id: str,
        local_port: int,
        on_status: Optional[StatusCallback] = None,
        on_connected: Optional[ConnectedCallback] = None,
    ):
        self.cerver_url = cerver_url
        self.api_token = api_token
        self.compute_id = compute_id
        self.local_port = local_port
        self.on_status = on_status
        self.on_connected = on_connected

        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

    def _emit_status(self, status: str) -> None:
        if self.on_status:
            self.on_status(status)

    async def run(self) -> None:
        self._running = True
        reconnect_attempts = 0

        while self._running:
            self._emit_status("connecting")
            try:
                await self._connect_once()
                reconnect_attempts = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                reconnect_attempts += 1
                print(f"[Cerver] Connect channel reconnecting: {exc}")
                self._emit_status("connecting")
                delay = min(
                    INITIAL_RECONNECT_DELAY
                    * (RECONNECT_BACKOFF_MULTIPLIER ** max(0, reconnect_attempts - 1)),
                    MAX_RECONNECT_DELAY,
                )
                await asyncio.sleep(delay)

        await self.close()

    async def close(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _connect_once(self) -> None:
        ws_url = build_cerver_connect_ws_url(self.cerver_url, self.compute_id, self.api_token)
        async with websockets.connect(
            ws_url,
            additional_headers={
                "Authorization": f"Bearer {self.api_token}",
            },
            ping_interval=30,
            ping_timeout=10,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            self._emit_status("connected")

            # Dispatch each gateway request as its own task so a long-
            # running /run (e.g. claude taking 15s to finish) doesn't
            # block the WS read loop. With `await _handle_message(raw)`
            # in the loop, three concurrent /v2/sessions/<id>/input calls
            # were processed serially — the second + third CLIs sat in
            # the WS receive buffer until the first finished, and the
            # upstream gateway/client timed out at 180s before they
            # were dequeued (the "third CLI never produces output"
            # symptom from 853eb43). Each handler owns its own
            # send-response; the websockets library serializes sends on
            # the underlying connection, so concurrent _send_json calls
            # are safe.
            async for raw in ws:
                asyncio.create_task(self._handle_message_safely(raw))

        self._ws = None

    async def _handle_message_safely(self, raw: str) -> None:
        try:
            await self._handle_message(raw)
        except Exception as exc:
            # Swallow + log: an exception escaping a fire-and-forget
            # task becomes a noisy "Task exception was never retrieved"
            # but doesn't kill the WS loop. Log so we still notice.
            print(f"[Cerver connect] request handler crashed: {exc}")

    async def _handle_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = payload.get("type")
        if msg_type == "connected":
            if self.on_connected:
                self.on_connected(payload)
            return

        if msg_type == "ping":
            await self._send_json({"type": "pong"})
            return

        if msg_type != "request":
            return

        response = await self._execute_request(payload)
        await self._send_json(response)

    async def _execute_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request_id = payload.get("request_id")
        request = {
            "id": request_id,
            "method": payload.get("method", "GET"),
            "path": payload.get("path", "/"),
            "headers": payload.get("headers", {}),
            "body": payload.get("body"),
        }

        response = await execute_local_request(self.local_port, request)
        return {
            "type": "response",
            "request_id": request_id,
            "status": response.get("status", 500),
            "headers": response.get("headers", {}),
            "body": response.get("body"),
        }

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("Cerver connect websocket is not connected")
        await self._ws.send(json.dumps(payload))

    async def publish_stream_event(self, session_id: str, event: Dict[str, Any]) -> None:
        """Forward one CLI stream event to cerver for live fan-out.

        Cerver routes the event to every subscriber of the session's
        /v2/sessions/<id>/stream/ws WebSocket. Fire-and-forget: any send
        failure is dropped silently — the durable copy goes via the HTTP
        transcript push, this WS path is for low-latency live updates only.
        """
        if self._ws is None or not session_id:
            return
        try:
            await self._send_json(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": event,
                }
            )
        except Exception:
            # WS may be mid-reconnect — drop the live event, the HTTP
            # transcript push still preserves it for refresh-load.
            pass


# ── Module-level active-transport registry ─────────────────────────────────
# The relay only ever runs one CerverConnectTransport at a time. Modules that
# need to publish stream events (e.g. agent_manager) shouldn't have to thread
# the transport through their constructors — they read the active one here.

_active_transport: Optional["CerverConnectTransport"] = None


def set_active_transport(transport: Optional["CerverConnectTransport"]) -> None:
    global _active_transport
    _active_transport = transport


def get_active_transport() -> Optional["CerverConnectTransport"]:
    return _active_transport


def publish_stream_event_nowait(session_id: str, event: Dict[str, Any]) -> None:
    """Fire-and-forget convenience: schedules a publish on the active transport
    if one exists. Safe to call from sync contexts — schedules a task on the
    running loop, swallows errors. Returns immediately.
    """
    transport = _active_transport
    if transport is None or not session_id:
        return
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(transport.publish_stream_event(session_id, event))
    except RuntimeError:
        # No running loop — drop silently.
        pass
