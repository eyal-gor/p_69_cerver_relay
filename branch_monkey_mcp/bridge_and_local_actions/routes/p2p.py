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
from typing import Any, Dict

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


@router.post("/close/{peer_id}")
async def p2p_close(peer_id: str):
    await _close(peer_id)
    return {"closed": peer_id}


@router.get("/status")
async def p2p_status():
    return {"available": available(), "peers": len(_peers), "max_peers": MAX_PEERS}
