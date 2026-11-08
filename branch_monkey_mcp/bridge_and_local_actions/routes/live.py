"""
Live screen over WebRTC.

A viewer (the Cerver dashboard, or anyone the owner lets watch) sends a WebRTC
offer; this machine answers with a video track of its screen. The offer and
answer travel through the gateway's existing owner-only forward
(POST /v2/computes/:id/exec → POST /api/live/offer here), so no port is opened
on this machine. Video then flows peer to peer, with STUN to find a path.

View only for now: no mouse or keyboard input is accepted.

Needs the optional ``live`` extra (aiortc, mss, numpy). Without it the routes
answer 501 and say how to install it, and the rest of the relay is unaffected.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fractions
import threading
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
log = logging.getLogger(__name__)

MAX_VIEWERS = 3
MAX_SECONDS = 30 * 60  # a viewer is cut off after 30 minutes
MAX_WIDTH = 1440
ICE_SERVERS = ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"]

_peers: Dict[str, Any] = {}


class Offer(BaseModel):
    sdp: str
    type: str = "offer"
    fps: int = 12
    monitor: int = 1


def _deps():
    try:
        import mss  # noqa: F401
        import numpy  # noqa: F401
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription  # noqa: F401
        from aiortc.mediastreams import VideoStreamTrack  # noqa: F401
        from av import VideoFrame  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on the install
        raise HTTPException(
            status_code=501,
            detail=f"Live screen needs the 'live' extra: pip install 'cerver-relay[live]' ({e.name} missing)",
        )


def _screen_track(fps: int, monitor: int):
    import mss
    import numpy as np
    from aiortc.mediastreams import VideoStreamTrack
    from av import VideoFrame

    class ScreenTrack(VideoStreamTrack):
        """One frame of the chosen monitor per tick, scaled to MAX_WIDTH."""

        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            # Capture runs on one worker thread: a grab can block (on macOS it
            # waits while the Screen Recording prompt is open), and it must
            # never stall the event loop that serves the rest of the relay.
            # mss objects belong to the thread that made them, so it lives there.
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-capture")
            self._local = threading.local()
            self._monitor = monitor
            self._period = 1.0 / max(1, min(fps, 30))
            self._start: Optional[float] = None
            self._n = 0

        async def recv(self):
            if self._start is None:
                self._start = time.time()
            # pace to the requested frame rate
            wait = self._start + self._n * self._period - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._n += 1
            img = await asyncio.get_running_loop().run_in_executor(self._pool, self._grab)
            frame = VideoFrame.from_ndarray(img, format="bgr24")
            w = min(MAX_WIDTH, img.shape[1])
            h = int(img.shape[0] * w / img.shape[1])
            frame = frame.reformat(width=w & ~1, height=h & ~1)  # scale; encoders want even sizes
            frame.pts = int(self._n * self._period * 90000)
            frame.time_base = fractions.Fraction(1, 90000)
            return frame

        def _grab(self):
            sct = getattr(self._local, "sct", None)
            if sct is None:
                sct = self._local.sct = mss.mss()
                mons = sct.monitors
                self._local.mon = mons[self._monitor] if 0 < self._monitor < len(mons) else mons[1]
            shot = sct.grab(self._local.mon)
            return np.ascontiguousarray(np.asarray(shot)[:, :, :3])  # BGRA → BGR

        def stop(self) -> None:
            super().stop()
            self._pool.shutdown(wait=False, cancel_futures=True)

    return ScreenTrack()


async def _close(peer_id: str) -> None:
    pc = _peers.pop(peer_id, None)
    if pc is not None:
        try:
            await pc.close()
        except Exception:
            pass


@router.post("/offer")
async def live_offer(offer: Offer):
    """Answer a viewer's WebRTC offer with this machine's screen."""
    _deps()
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    if offer.type != "offer":
        raise HTTPException(status_code=400, detail="type must be 'offer'")
    if len(_peers) >= MAX_VIEWERS:
        raise HTTPException(status_code=429, detail=f"At most {MAX_VIEWERS} viewers at once")

    peer_id = uuid.uuid4().hex[:12]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=ICE_SERVERS)]))
    _peers[peer_id] = pc

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        log.info("live %s: %s", peer_id, pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await _close(peer_id)

    async def _expire() -> None:
        await asyncio.sleep(MAX_SECONDS)
        await _close(peer_id)

    try:
        pc.addTrack(_screen_track(offer.fps, offer.monitor))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())  # waits for ICE gathering
    except Exception as e:
        await _close(peer_id)
        raise HTTPException(status_code=400, detail=f"Could not answer the offer: {e}")

    asyncio.ensure_future(_expire())
    return {"peer_id": peer_id, "sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


@router.post("/close/{peer_id}")
async def live_close(peer_id: str):
    """Stop one viewer's stream."""
    await _close(peer_id)
    return {"closed": peer_id}


@router.get("/status")
async def live_status():
    """Whether live screen is available here, and how many viewers are watching."""
    try:
        _deps()
        available = True
    except HTTPException:
        available = False
    return {"available": available, "viewers": len(_peers), "max_viewers": MAX_VIEWERS}
