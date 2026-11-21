"""
Direct mode: commands and their output go straight between a person's device
and this machine, peer to peer. The Cerver gateway never sees them.

How a connection starts (the only part that passes through the gateway):
  1. The device makes a WebRTC offer with a data channel named "cerver".
  2. It sends the offer to the gateway (POST /v2/sessions/:id/p2p/offer). The
     gateway checks that the caller owns that session, then hands the offer to
     this machine (POST /api/p2p/offer here) with the session's sandbox id.
  3. This machine answers. The two sides then find a direct route (ICE, with
     public STUN servers to learn their outside addresses) and encrypt
     everything with DTLS. From here on the gateway is out of the path.

On the channel, each message is one JSON object:
  device  → machine  {"id": "1", "code": "ls", "timeout": 60}
  machine → device   {"id": "1", "stdout": "…", "stderr": "", "exit_code": 0, "duration_ms": 12}
  machine → device   {"id": "1", "error": "…"}             (when it could not run)

A peer is bound to one sandbox (the session's), chosen by the gateway at
offer time, so it can only run where that session runs. A guest's sandbox is
fenced (computer_runtime/guest_fence.py) no matter how its commands arrive.

Needs aiortc (the relay's "live" extra). Without it the offer answers 501.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...cerver_compute.execution import collect_provider_run
from ...cerver_compute.shell_sandbox import get_shell_sandbox, run_in_shell_sandbox
from ..agent_manager import agent_manager

log = logging.getLogger(__name__)
router = APIRouter()

ICE_SERVERS = ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"]
MAX_PEERS = 8
MAX_SECONDS = 2 * 60 * 60  # a peer is cut off after 2 hours
MAX_MESSAGE = 256 * 1024  # bytes of one command message
DEFAULT_TIMEOUT = 120

_peers: Dict[str, Any] = {}


class P2POffer(BaseModel):
    sdp: str
    type: str = "offer"
    sandbox_id: str
    # Set by the gateway when the caller is not this machine's owner.
    guest: bool = False


def available() -> bool:
    try:
        import aiortc  # noqa: F401
        return True
    except ImportError:
        return False


async def _close(peer_id: str) -> None:
    pc = _peers.pop(peer_id, None)
    if pc is not None:
        try:
            await pc.close()
        except Exception:
            pass


async def _run(sandbox_id: str, code: str, timeout: int) -> Dict[str, Any]:
    started = time.monotonic()
    if get_shell_sandbox(sandbox_id):
        result = await run_in_shell_sandbox(sandbox_id, code, timeout)
    else:
        result = await collect_provider_run(agent_manager, sandbox_id, code, timeout)
    out = dict(result) if isinstance(result, dict) else {"result": result}
    out["duration_ms"] = round((time.monotonic() - started) * 1000)
    return out


@router.post("/offer")
async def p2p_offer(offer: P2POffer):
    """Answer a device's offer with a direct command channel to one sandbox."""
    if not available():
        raise HTTPException(status_code=501, detail="Direct mode needs the relay's live extra: pip install 'cerver-relay[live]'")
    if offer.type != "offer":
        raise HTTPException(status_code=400, detail="type must be 'offer'")
    shell = get_shell_sandbox(offer.sandbox_id)
    if shell is None and agent_manager.get(offer.sandbox_id) is None:
        raise HTTPException(status_code=404, detail="That session isn't running on this machine")
    # Belt and braces: a guest only ever reaches a fenced sandbox.
    if offer.guest and not (shell and shell.get("guest")):
        raise HTTPException(status_code=403, detail="Guests can only use fenced sessions")
    if len(_peers) >= MAX_PEERS:
        raise HTTPException(status_code=429, detail=f"At most {MAX_PEERS} direct connections at once")

    from ._ice import speed_up_gathering
    speed_up_gathering()
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    peer_id = uuid.uuid4().hex[:12]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=ICE_SERVERS)]))
    _peers[peer_id] = pc
    sandbox_id = offer.sandbox_id

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        log.info("p2p %s: %s", peer_id, pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await _close(peer_id)

    @pc.on("datachannel")
    def _on_channel(channel: Any) -> None:
        if channel.label != "cerver":
            return

        async def _handle(raw: Any) -> None:
            msg_id = None
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if len(raw) > MAX_MESSAGE:
                    raise ValueError("message too large")
                msg = json.loads(raw)
                msg_id = msg.get("id")
                code = msg.get("code")
                if not isinstance(code, str) or not code.strip():
                    raise ValueError("code is required")
                timeout = max(5, min(int(msg.get("timeout") or DEFAULT_TIMEOUT), 3600))
                reply = await _run(sandbox_id, code, timeout)
            except Exception as e:  # report, never crash the channel
                reply = {"error": str(e)}
            reply["id"] = msg_id
            if channel.readyState == "open":
                channel.send(json.dumps(reply))

        @channel.on("message")
        def _on_message(raw: Any) -> None:
            asyncio.ensure_future(_handle(raw))

    async def _expire() -> None:
        await asyncio.sleep(MAX_SECONDS)
        await _close(peer_id)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())  # waits for ICE gathering
    except Exception as e:
        await _close(peer_id)
        raise HTTPException(status_code=400, detail=f"Could not answer the offer: {e}")

    asyncio.ensure_future(_expire())
    return {"peer_id": peer_id, "sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


# ── Forward: reach an app on this machine's port, peer to peer ──
#
# `cerver forward <machine> <port>` on another device makes this machine's
# app (say a web app on port 3456) show up there at localhost. Each TCP
# connection on that device opens one data channel named "tcp:<port>"; this
# side connects to 127.0.0.1:<port> and passes bytes both ways. The gateway
# only introduces the two (owner of this machine only); the bytes never pass
# through it.

FORWARD_MAX_SECONDS = 12 * 60 * 60  # a forward lasts up to 12 hours
CHANNEL_HIGH_WATER = 1 << 20  # pause reading the app while 1 MB is unsent
CHUNK = 16 * 1024  # SCTP messages stay small


class ForwardOffer(BaseModel):
    sdp: str
    type: str = "offer"


def forward_port(label: str) -> Optional[int]:
    """The port a channel label asks for ("tcp:3456" → 3456), or None."""
    if not label.startswith("tcp:"):
        return None
    try:
        port = int(label[4:])
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _pipe(channel: Any, port: int) -> None:
    """One data channel ⇄ one TCP connection to 127.0.0.1:port."""
    inbox: asyncio.Queue = asyncio.Queue()
    state: Dict[str, Any] = {"writer": None}

    def _finish() -> None:
        w = state.get("writer")
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        if channel.readyState in ("connecting", "open"):
            channel.close()

    async def _app_to_peer(reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            while channel.bufferedAmount > CHANNEL_HIGH_WATER and channel.readyState == "open":
                await asyncio.sleep(0.01)
            if channel.readyState != "open":
                break
            channel.send(data)

    async def _peer_to_app(writer: asyncio.StreamWriter) -> None:
        while True:
            data = await inbox.get()
            if data is None:
                break
            writer.write(data)
            await writer.drain()

    async def _run() -> None:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError as e:
            log.info("forward: nothing listening on %s (%s)", port, e)
            _finish()
            return
        state["writer"] = writer
        tasks = [asyncio.ensure_future(_app_to_peer(reader)), asyncio.ensure_future(_peer_to_app(writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception as e:
            log.info("forward %s ended: %s", port, e)
        finally:
            for t in tasks:
                t.cancel()
            _finish()

    @channel.on("message")
    def _on_message(raw: Any) -> None:
        inbox.put_nowait(raw.encode() if isinstance(raw, str) else raw)

    @channel.on("close")
    def _on_close() -> None:
        inbox.put_nowait(None)

    asyncio.ensure_future(_run())


@router.post("/forward")
async def p2p_forward(offer: ForwardOffer):
    """Answer an offer whose channels each reach one local port of this machine.

    Only the machine's owner gets here: the gateway checks that before it
    passes the offer on. Guests never get port forwarding.
    """
    if not available():
        raise HTTPException(status_code=501, detail="Forwarding needs the relay's live extra: pip install 'cerver-relay[live]'")
    if offer.type != "offer":
        raise HTTPException(status_code=400, detail="type must be 'offer'")
    if len(_peers) >= MAX_PEERS:
        raise HTTPException(status_code=429, detail=f"At most {MAX_PEERS} direct connections at once")

    from ._ice import speed_up_gathering
    speed_up_gathering()
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    peer_id = uuid.uuid4().hex[:12]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=ICE_SERVERS)]))
    _peers[peer_id] = pc

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        log.info("forward %s: %s", peer_id, pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await _close(peer_id)

    @pc.on("datachannel")
    def _on_channel(channel: Any) -> None:
        port = forward_port(channel.label)
        if port is None:
            channel.close()
            return
        _pipe(channel, port)

    async def _expire() -> None:
        await asyncio.sleep(FORWARD_MAX_SECONDS)
        await _close(peer_id)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
    except Exception as e:
        await _close(peer_id)
        raise HTTPException(status_code=400, detail=f"Could not answer the offer: {e}")

    asyncio.ensure_future(_expire())
    return {"peer_id": peer_id, "sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


@router.post("/close/{peer_id}")
async def p2p_close(peer_id: str):
    await _close(peer_id)
    return {"closed": peer_id}


@router.get("/status")
async def p2p_status():
    return {"available": available(), "peers": len(_peers), "max_peers": MAX_PEERS}
