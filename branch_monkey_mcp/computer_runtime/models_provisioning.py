"""Provisioning: local models (Ollama) on this computer.

What the Provisioning section shows for each computer, and what its buttons
do. The owner clicks; the gateway forwards to this relay (routes/models.py).

  status()          Ollama installed / running, models here, what fits
  install_ollama()  get Ollama (Homebrew if present, else the official app)
  start_ollama()    start it, and keep it starting at login
  pull(name)        download a model from the catalog, in the background
  remove(name)      delete a model

Only models in CATALOG can be pulled, and only when this computer has the
memory to run it and the free disk to hold it (plus room to spare), so a
click can't fill a disk or install something that would crawl.
"""

import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

from ..bridge_and_local_actions.ollama_runner import _base_url, list_local_models

# Sizes are Ollama's download sizes; memory is what runs it comfortably.
CATALOG: List[Dict[str, Any]] = [
    {"name": "qwen3:4b", "size_gb": 2.5, "min_ram_gb": 8, "label": "Qwen3 4B", "good_for": "summaries, sorting, short replies"},
    {"name": "qwen3:8b", "size_gb": 5.2, "min_ram_gb": 16, "label": "Qwen3 8B", "good_for": "everyday chat, drafts, simple tool use"},
    {"name": "qwen3:14b", "size_gb": 9.3, "min_ram_gb": 24, "label": "Qwen3 14B", "good_for": "better reasoning and tool use"},
    {"name": "gpt-oss:20b", "size_gb": 14.0, "min_ram_gb": 32, "label": "gpt-oss 20B", "good_for": "OpenAI's open model, strong tool use"},
]
# Keep 30 GB free after a download: the OS, apps and agents need room too.
SPARE_DISK_GB = 30

_pull: Dict[str, Any] = {}
_lock = threading.Lock()


def _ram_gb() -> Optional[float]:
    try:
        from .capabilities import get_hardware
        return get_hardware().get("ram_gb")
    except Exception:  # noqa: BLE001
        return None


def _disk_free_gb() -> Optional[float]:
    try:
        return round(shutil.disk_usage(os.path.expanduser("~")).free / 2**30, 1)
    except Exception:  # noqa: BLE001
        return None


def _ollama_bin() -> Optional[str]:
    for p in (shutil.which("ollama"), "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
              "/Applications/Ollama.app/Contents/Resources/ollama",
              os.path.expanduser("~/Applications/Ollama.app/Contents/Resources/ollama")):
        if p and os.path.exists(p):
            return p
    return None


def _running() -> bool:
    try:
        with urllib.request.urlopen(f"{_base_url()}/api/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _sizes() -> Dict[str, float]:
    try:
        with urllib.request.urlopen(f"{_base_url()}/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return {}
    return {m.get("name"): round((m.get("size") or 0) / 2**30, 1) for m in data.get("models") or [] if m.get("name")}


def fits(entry: Dict[str, Any], ram: Optional[float], disk: Optional[float]) -> Optional[str]:
    """None when the model fits this computer, else why not (plain words)."""
    if ram is not None and ram < entry["min_ram_gb"]:
        return f"Needs {entry['min_ram_gb']} GB of memory; this computer has {ram:g} GB"
    need = entry["size_gb"] + SPARE_DISK_GB
    if disk is not None and disk < need:
        return f"Not enough free disk: needs about {need:g} GB free, {disk:g} GB is"
    return None


def status() -> Dict[str, Any]:
    ram, disk = _ram_gb(), _disk_free_gb()
    installed = _ollama_bin() is not None
    running = installed and _running()
    here = _sizes() if running else {}
    have = set(here)
    catalog = []
    for e in CATALOG:
        has = e["name"] in have
        catalog.append({**e, "installed": has, "why_not": None if has else fits(e, ram, disk)})
    fitting = [c for c in catalog if not c["installed"] and not c["why_not"]]
    with _lock:
        pulling = dict(_pull) if _pull else None
    return {
        "supported": platform.system() in ("Darwin", "Linux"),
        "ollama_installed": installed,
        "ollama_running": running,
        "models": [{"name": n, "size_gb": s} for n, s in here.items()],
        "ram_gb": ram,
        "disk_free_gb": disk,
        "catalog": catalog,
        # The biggest model that fits is the one to suggest.
        "recommended": fitting[-1]["name"] if fitting else None,
        "pulling": pulling,
    }


def summary() -> Dict[str, Any]:
    """Light version for the capability report: no catalog."""
    installed = _ollama_bin() is not None
    running = installed and _running()
    return {"ollama_installed": installed, "ollama_running": running, "models": list_local_models() if running else []}


def _run(args: List[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def start_ollama() -> Dict[str, Any]:
    """Start Ollama and keep it starting at login."""
    if _running():
        return {"ok": True, "how": "already running"}
    app = next((p for p in ("/Applications/Ollama.app", os.path.expanduser("~/Applications/Ollama.app")) if os.path.exists(p)), None)
    brew = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(p)), None)
    binp = _ollama_bin()
    if platform.system() == "Darwin" and app:
        _run(["open", "-g", "-a", app], 30)  # the app adds itself to login items
        how = "Ollama app"
    elif brew and binp and binp.startswith(os.path.dirname(os.path.dirname(brew))):
        r = _run([brew, "services", "start", "ollama"], 120)  # starts now and at login
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}
        how = "Homebrew service"
    elif binp:
        subprocess.Popen([binp, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        how = "ollama serve (until restart)"
    else:
        return {"ok": False, "error": "Ollama isn't installed"}
    for _ in range(30):
        if _running():
            return {"ok": True, "how": how}
        time.sleep(1)
    return {"ok": False, "error": "Ollama didn't start within 30 seconds"}


def install_ollama() -> Dict[str, Any]:
    if _ollama_bin():
        return start_ollama()
    system = platform.system()
    brew = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(p)), None)
    if system == "Darwin" and brew:
        r = _run([brew, "install", "ollama"], 900)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}
    elif system == "Darwin":
        # The official app, into ~/Applications (no admin password needed).
        import tempfile
        dest = os.path.expanduser("~/Applications")
        os.makedirs(dest, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            z = os.path.join(tmp, "Ollama-darwin.zip")
            urllib.request.urlretrieve("https://ollama.com/download/Ollama-darwin.zip", z)
            r = _run(["ditto", "-x", "-k", z, dest], 300)  # keeps the app's signature intact
            if r.returncode != 0:
                return {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}
    else:
        return {"ok": False, "error": "On Linux, install Ollama with: curl -fsSL https://ollama.com/install.sh | sh"}
    return start_ollama()


def _pull_worker(name: str) -> None:
    try:
        req = urllib.request.Request(f"{_base_url()}/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                try:
                    ev = json.loads(line.decode())
                except ValueError:
                    continue
                with _lock:
                    if ev.get("error"):
                        _pull.update(error=ev["error"])
                        return
                    if ev.get("total"):
                        _pull.update(total=ev["total"], completed=ev.get("completed") or 0)
                    _pull.update(step=ev.get("status"))
        with _lock:
            _pull.update(done=True)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _pull.update(error=str(e))
    finally:
        with _lock:
            _pull.update(finished_at=time.time())


def pull(name: str) -> Dict[str, Any]:
    entry = next((e for e in CATALOG if e["name"] == name), None)
    if not entry:
        return {"ok": False, "error": f"{name} isn't in the list of models Cerver installs"}
    if not _running():
        return {"ok": False, "error": "Start Ollama first"}
    why = fits(entry, _ram_gb(), _disk_free_gb())
    if why:
        return {"ok": False, "error": why}
    with _lock:
        if _pull and not _pull.get("finished_at"):
            return {"ok": False, "error": f"Already downloading {_pull.get('name')}"}
        _pull.clear()
        _pull.update(name=name, started_at=time.time(), completed=0, total=int(entry["size_gb"] * 2**30))
    threading.Thread(target=_pull_worker, args=(name,), daemon=True).start()
    return {"ok": True, "pulling": name}


def remove(name: str) -> Dict[str, Any]:
    if name not in _sizes():
        return {"ok": False, "error": f"{name} isn't on this computer"}
    req = urllib.request.Request(f"{_base_url()}/api/delete", data=json.dumps({"model": name}).encode(),
                                 headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True}
