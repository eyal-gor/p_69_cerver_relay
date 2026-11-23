"""A journal of the jobs this relay ran, for the Agents page of the Cerver app.

Each run Cerver hands this computer is one line in ~/.cerver/relay-jobs.jsonl:
when it came, what it asked (first words only), who sent it, how it ended and
how long it took. The last 300 are kept. Nothing secret is written: no
environment, no output.
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

FILE = os.path.expanduser("~/.cerver/relay-jobs.jsonl")
KEEP = 300
_lock = threading.Lock()
_sessions: Dict[str, Dict[str, Any]] = {}  # sandbox id → who/what, from its creation
_running: Dict[str, Dict[str, Any]] = {}


def note_session(sandbox_id: str, engine: str, metadata: Optional[Dict[str, Any]]) -> None:
    m = metadata or {}
    pool = bool(m.get("pool_session") or m.get("guest") or m.get("external_user_id"))
    origin = m.get("source") or m.get("surface") or m.get("run_kind") or ""
    _sessions[sandbox_id] = {
        "engine": engine,
        "cli": m.get("cli_tool") or m.get("harness") or engine,
        "from": "someone you invited" if pool else (m.get("from_machine") or m.get("origin_machine") or origin or "you"),
        "project": m.get("project_slug") or "",
        "session": m.get("session_id") or m.get("cerver_session_id") or "",
    }


def start(sandbox_id: str, task: str, info: Optional[Dict[str, Any]] = None) -> str:
    jid = uuid.uuid4().hex[:10]
    s = {"engine": "", "cli": "", "from": "", "project": "", "session": "", **_sessions.get(sandbox_id, {}), **(info or {})}
    words = " ".join((task or "").split())
    _running[jid] = {"id": jid, "at": time.time(), "task": words[:160], **s, "status": "running"}
    return jid


def end(jid: str, ok: bool, error: str = "") -> None:
    j = _running.pop(jid, None)
    if not j:
        return
    j["status"] = "done" if ok else "failed"
    j["seconds"] = round(time.time() - j["at"], 1)
    if error:
        j["error"] = error[:200]
    with _lock:
        try:
            os.makedirs(os.path.dirname(FILE), exist_ok=True)
            lines: List[str] = []
            if os.path.exists(FILE):
                with open(FILE) as f:
                    lines = f.read().splitlines()[-(KEEP - 1):]
            lines.append(json.dumps(j))
            tmp = FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, FILE)
        except Exception:  # noqa: BLE001
            pass


def recent(n: int = 50) -> Dict[str, Any]:
    done: List[Dict[str, Any]] = []
    try:
        with open(FILE) as f:
            for line in f.read().splitlines():
                try:
                    done.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    except FileNotFoundError:
        pass
    day = time.time() - 86400
    today = [j for j in done if j.get("at", 0) >= day]
    return {
        "running": list(_running.values()),
        "jobs": list(reversed(done[-n:])),
        "today": {
            "jobs": len(today),
            "done": sum(1 for j in today if j.get("status") == "done"),
            "failed": sum(1 for j in today if j.get("status") == "failed"),
            "seconds": round(sum(j.get("seconds", 0) for j in today)),
        },
        "total": len(done),
    }
