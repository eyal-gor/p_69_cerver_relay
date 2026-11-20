"""Direct recording: this machine writes its sessions' transcripts straight
to Neon, so big LLM output never passes through the Cerver gateway.

The gateway signs a one-hour pass for one session (POST
/v2/sessions/:id/recorder). With it, this machine calls one database
function on Neon's Data API that can only append to that session. No
database password lives here.

The pool's master records; the gateway never carries transcripts. The one
exception is a gateway that has recording switched off (404 "off", i.e.
before Neon is set up): record_direct() returns OFF and the caller uses the
old gateway push. When recording is on and a write fails, it returns FAILED
and the caller keeps the lines and retries Neon. It never falls back to the
gateway.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LANDED = "landed"
OFF = "off"        # gateway has recording switched off: use the gateway push
FAILED = "failed"  # recording is on but this write failed: keep and retry Neon

_passes: Dict[str, Dict[str, Any]] = {}
_off_until: Dict[str, float] = {}
OFF_RETRY_SECONDS = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _expires(p: Dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(p["expires_at"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class _Off(Exception):
    pass


async def _pass(client: Any, target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gateway = str(target["url"]).rstrip("/")
    sid = target["session_id"]
    if _off_until.get(gateway, 0) > time.time():
        raise _Off()
    p = _passes.get(sid)
    if p and _expires(p) - 120 > time.time():
        return p
    resp = await client.post(
        f"{gateway}/v2/sessions/{sid}/recorder",
        headers={"Authorization": f"Bearer {target['token']}"},
    )
    if resp.status_code == 404 and "off" in resp.text.lower():
        _off_until[gateway] = time.time() + OFF_RETRY_SECONDS  # recording off on this gateway
        raise _Off()
    if resp.status_code != 200:
        return None
    p = resp.json()
    _passes[sid] = p
    return p


async def record_direct(target: Dict[str, Any], entries: List[Dict[str, Any]]) -> str:
    """Append entries to the session in Neon directly. Returns LANDED, OFF or FAILED."""
    if os.environ.get("CERVER_DIRECT_RECORDING", "1") == "0":
        return OFF
    if not entries:
        return LANDED
    import httpx

    rows = [dict(e, at=e.get("at") or _now_iso()) for e in entries]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            p = await _pass(client, target)
            if not p:
                return FAILED
            resp = await client.post(
                p["url"],
                json={"p_session_id": target["session_id"], "p_entries": rows},
                headers={"Authorization": f"Bearer {p['token']}", "Content-Type": "application/json"},
            )
            if resp.status_code < 300:
                return LANDED
            if resp.status_code in (401, 403):
                _passes.pop(target["session_id"], None)  # stale pass: get a new one next time
            return FAILED
    except _Off:
        return OFF
    except Exception:
        return FAILED


def forget(session_id: str) -> None:
    _passes.pop(session_id, None)
