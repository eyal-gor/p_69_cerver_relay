"""Direct recording: this machine writes its sessions' transcripts straight
to Neon, so big LLM output never passes through the Cerver gateway.

The gateway signs a one-hour pass for one session (POST
/v2/sessions/:id/recorder). With it, this machine calls one database
function on Neon's Data API that can only append to that session. No
database password lives here.

If the gateway has recording off (404), the pass fails, or Neon refuses,
record_direct() returns False and the caller pushes through the gateway as
before, so nothing is ever lost by trying.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


async def _pass(client: Any, target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gateway = str(target["url"]).rstrip("/")
    sid = target["session_id"]
    if _off_until.get(gateway, 0) > time.time():
        return None
    p = _passes.get(sid)
    if p and _expires(p) - 120 > time.time():
        return p
    resp = await client.post(
        f"{gateway}/v2/sessions/{sid}/recorder",
        headers={"Authorization": f"Bearer {target['token']}"},
    )
    if resp.status_code == 404 and "off" in resp.text.lower():
        _off_until[gateway] = time.time() + OFF_RETRY_SECONDS  # recording off on this gateway
        return None
    if resp.status_code != 200:
        return None
    p = resp.json()
    _passes[sid] = p
    return p


async def record_direct(target: Dict[str, Any], entries: List[Dict[str, Any]]) -> bool:
    """Append entries to the session in Neon directly. True when they landed."""
    if os.environ.get("CERVER_DIRECT_RECORDING", "1") == "0" or not entries:
        return False
    import httpx

    rows = [dict(e, at=e.get("at") or _now_iso()) for e in entries]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            p = await _pass(client, target)
            if not p:
                return False
            resp = await client.post(
                p["url"],
                json={"p_session_id": target["session_id"], "p_entries": rows},
                headers={"Authorization": f"Bearer {p['token']}", "Content-Type": "application/json"},
            )
            if resp.status_code < 300:
                return True
            if resp.status_code in (401, 403):
                _passes.pop(target["session_id"], None)  # stale pass: get a new one next time
            return False
    except Exception:
        return False


def forget(session_id: str) -> None:
    _passes.pop(session_id, None)
