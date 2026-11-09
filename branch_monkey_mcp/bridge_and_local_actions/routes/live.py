"""
Live screen over WebRTC.

A viewer (the Cerver dashboard, or anyone the owner lets watch) sends a WebRTC
offer; this machine answers with a video track of its screen. The offer and
answer travel through the gateway's existing owner-only forward
(POST /v2/computes/:id/exec → POST /api/live/offer here), so no port is opened
on this machine. Video then flows peer to peer, with STUN to find a path.

The viewer can also drive the machine: mouse and keyboard events come over a
WebRTC data channel named "input" and are replayed with xdotool. Linux (X11)
only for now — the e2b desktop and any Xvfb machine; elsewhere status reports
control: false and the viewer stays view-only.

Needs the optional ``live`` extra (aiortc, mss, numpy). Without it the routes
answer 501 and say how to install it, and the rest of the relay is unaffected.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fractions
import threading
import logging
import json
import os
import shutil
import sys
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


def _screen_permission() -> Optional[bool]:
    """macOS only: whether this process may record the screen (None elsewhere)."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def _ask_screen_permission() -> None:
    """macOS: put this process in the Screen Recording list (shows the prompt once)."""
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess()
    except Exception:
        pass


def _capability() -> Dict[str, Any]:
    """Whether this machine can stream its screen, and if not, why.

    reason: ok | missing_deps | needs_permission | no_display
    """
    try:
        _deps()
    except HTTPException as e:
        return {"available": False, "reason": "missing_deps", "detail": e.detail}
    if sys.platform == "darwin":
        if _screen_permission() is False:
            return {
                "available": False,
                "reason": "needs_permission",
                "detail": "macOS needs Screen Recording allowed for this relay "
                "(System Settings > Privacy & Security > Screen & System Audio Recording), "
                f"then a relay restart. Process: {sys.executable}",
            }
    elif sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return {
            "available": False,
            "reason": "no_display",
            "detail": "This machine has no screen (no DISPLAY). Run the agent's apps on a virtual display (Xvfb) to watch them.",
        }
    return {"available": True, "reason": "ok", "control": _control_available()}


def _control_available() -> bool:
    """Whether a viewer's clicks and keys can be replayed here (X11 + xdotool)."""
    return sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY")) and shutil.which("xdotool") is not None


# Keys the browser names differently from X keysyms.
_KEYSYMS = {
    "Enter": "Return", "Backspace": "BackSpace", "Tab": "Tab", "Escape": "Escape",
    "Delete": "Delete", "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down",
    "Insert": "Insert", " ": "space",
    **{f"F{i}": f"F{i}" for i in range(1, 13)},
}
_MODS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "meta": "super"}


def _xdotool_args(ev: Dict[str, Any], rect: Dict[str, int]) -> Optional[list]:
    """One viewer event as xdotool arguments, or None to ignore it.

    Events: move/down/up {x, y in 0..1, b: 1|2|3}, wheel {dy}, key {k, mods},
    text {s}. Coordinates are fractions of the streamed monitor, so they hold
    whatever size the viewer draws the video at.
    """
    t = ev.get("t")

    def xy() -> list:
        x = min(max(float(ev.get("x", 0)), 0.0), 1.0)
        y = min(max(float(ev.get("y", 0)), 0.0), 1.0)
        return ["mousemove", str(rect["left"] + round(x * (rect["width"] - 1))), str(rect["top"] + round(y * (rect["height"] - 1)))]

    button = str(ev.get("b", 1)) if ev.get("b") in (1, 2, 3) else "1"
    if t == "move":
        return xy()
    if t in ("down", "up"):
        return xy() + ["mousedown" if t == "down" else "mouseup", button]
    if t == "wheel":
        dy = float(ev.get("dy", 0))
        if not dy:
            return None
        return ["click", "--repeat", str(min(5, max(1, round(abs(dy) / 60) or 1))), "5" if dy > 0 else "4"]
    if t == "text":
        text = str(ev.get("s", ""))[:500]
        return ["type", "--delay", "8", "--", text] if text else None
    if t == "key":
        k = str(ev.get("k", ""))
        sym = _KEYSYMS.get(k) or (k if len(k) == 1 else None)
        if not sym:
            return None
        mods = [_MODS[m] for m in (ev.get("mods") or []) if m in _MODS]
        return ["key", "--", "+".join(mods + [sym])]
    return None


def _monitor_rect(monitor: int) -> Dict[str, int]:
    import mss

    with mss.mss() as sct:
        mons = sct.monitors
        m = mons[monitor] if 0 < monitor < len(mons) else mons[1]
        return {k: int(m[k]) for k in ("left", "top", "width", "height")}


def _attach_input(pc: Any, monitor: int) -> None:
    """Replay the viewer's input channel on this machine, in order.

    A run of mouse moves collapses to the latest one, so a slow xdotool never
    leaves the pointer trailing behind the viewer's hand.
    """
    if not _control_available():
        return
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=500)
    rect: Dict[str, int] = {}

    async def _replay() -> None:
        while True:
            ev = await queue.get()
            if ev is None:
                return
            if ev.get("t") == "move" and not queue.empty():
                continue
            if not rect:
                try:
                    rect.update(await asyncio.get_running_loop().run_in_executor(None, _monitor_rect, monitor))
                except Exception as e:
                    log.warning("live input: no monitor geometry: %s", e)
                    continue
            args = _xdotool_args(ev, rect)
            if not args:
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception as e:
                log.warning("live input: xdotool failed: %s", e)

    task = asyncio.ensure_future(_replay())

    @pc.on("datachannel")
    def _on_channel(channel: Any) -> None:
        if channel.label != "input":
            return

        @channel.on("message")
        def _on_message(msg: Any) -> None:
            try:
                ev = json.loads(msg)
            except Exception:
                return
            if isinstance(ev, dict):
                try:
                    queue.put_nowait(ev)
                except asyncio.QueueFull:
                    pass

    @pc.on("connectionstatechange")
    async def _stop_input() -> None:
        if pc.connectionState in ("failed", "closed", "disconnected"):
            task.cancel()


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
    cap = _capability()
    if not cap["available"]:
        if cap["reason"] == "needs_permission":
            _ask_screen_permission()
        # Refuse up front: without permission a grab blocks and the viewer waits on nothing.
        raise HTTPException(status_code=501 if cap["reason"] == "missing_deps" else 409, detail=cap)
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
        _attach_input(pc, offer.monitor)
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
    return {**_capability(), "platform": sys.platform, "viewers": len(_peers), "max_viewers": MAX_VIEWERS}
