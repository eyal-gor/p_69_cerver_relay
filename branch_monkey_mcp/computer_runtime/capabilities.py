"""
Runtime capability helpers for local and hosted computer environments.

This module gathers the machine-level capabilities that higher layers
like the Cerver provider adapter can expose without depending directly
on route handlers.
"""

from typing import Any, Dict, List

from ..bridge_and_local_actions.cli_providers import (
    _invalidate_resolver_path_cache,
    get_available_providers,
)


def get_available_cli_tools() -> List[str]:
    """Return installed CLI tool names.

    Drops the cached lookup PATH first: a CLI installed after the relay
    started — into a directory that wasn't on the PATH at startup — stayed
    invisible until a relay restart, because detection probed against the
    stale cached PATH. Capabilities are collected once per heartbeat
    (every 30s, off the event loop), so rebuilding the PATH here is cheap and a freshly
    installed harness shows up on the next heartbeat.
    """
    _invalidate_resolver_path_cache()
    tools: List[str] = []
    for name, provider in get_available_providers().items():
        if provider.get("installed") and isinstance(name, str) and name:
            tools.append(name)
    return tools


def get_local_models() -> Dict[str, List[str]]:
    """Return per-harness models that exist only on *this* machine.

    Hosted harnesses are deliberately absent: their model list is the same
    everywhere and belongs in the client, not in a per-machine capability
    report. Local weights are the opposite — which models exist is a fact
    about this disk, so it can only be answered here.

    Shape: {"ollama": ["llama3.2:latest", ...]}. Absent or empty means this
    machine has no local models, which is a normal state.
    """
    from ..bridge_and_local_actions.cli_providers import _PROVIDERS

    out: Dict[str, List[str]] = {}
    for name, provider in _PROVIDERS.items():
        lister = getattr(provider, "local_models", None)
        if not callable(lister):
            continue
        try:
            models = lister()
        except Exception:  # noqa: BLE001 — a probe must never break capabilities
            continue
        if models:
            out[name] = models
    return out


# Software an agent most often needs to know is on a machine before it
# picks it. Checked by name on PATH; the version is not read.
COMMON_TOOLS = (
    "git", "docker", "ffmpeg", "node", "npm", "bun", "python3", "uv", "go",
    "cargo", "java", "nvcc", "nvidia-smi", "psql", "redis-server", "chromium",
    "google-chrome", "playwright", "kubectl", "terraform", "gh",
)

_HARDWARE_CACHE: Dict[str, Any] = {}
_HARDWARE_TTL = 600.0


def get_installed_tools() -> List[str]:
    """Which of COMMON_TOOLS are on this machine's PATH."""
    import shutil
    return [t for t in COMMON_TOOLS if shutil.which(t)]


def _run(cmd: List[str], timeout: float = 3.0) -> str:
    import subprocess
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def _ram_gb() -> Any:
    import os
    import platform
    try:
        if platform.system() == "Darwin":
            b = _run(["sysctl", "-n", "hw.memsize"]).strip()
            return round(int(b) / 2**30) if b.isdigit() else None
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return round(pages * size / 2**30, 1)
    except Exception:
        return None


def _gpus() -> List[Dict[str, Any]]:
    """GPUs on this machine: NVIDIA via nvidia-smi, Apple silicon via the chip name."""
    import platform
    import shutil
    gpus: List[Dict[str, Any]] = []
    smi = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]) if shutil.which("nvidia-smi") else ""
    for line in smi.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            try:
                mem = round(float(parts[1]) / 1024)
            except ValueError:
                mem = None
            gpus.append({"name": parts[0], "memory_gb": mem, "vendor": "nvidia"})
    if not gpus and platform.system() == "Darwin" and platform.machine() == "arm64":
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        if chip:
            # Apple GPUs share the machine's memory.
            gpus.append({"name": f"{chip} GPU", "memory_gb": None, "vendor": "apple", "shared_memory": True})
    return gpus


def get_hardware() -> Dict[str, Any]:
    """What this machine is: OS, CPU, memory, free disk and GPUs. Cached for 10 minutes."""
    import os
    import platform
    import shutil
    import time
    now = time.monotonic()
    if _HARDWARE_CACHE.get("at") and now - _HARDWARE_CACHE["at"] < _HARDWARE_TTL:
        return _HARDWARE_CACHE["value"]
    try:
        disk_free = round(shutil.disk_usage(os.path.expanduser("~")).free / 2**30)
    except Exception:
        disk_free = None
    value = {
        "os": {"Darwin": "macos"}.get(platform.system(), platform.system().lower()),
        "arch": platform.machine(),
        "cpu_cores": os.cpu_count(),
        "ram_gb": _ram_gb(),
        "disk_free_gb": disk_free,
        "gpus": _gpus(),
    }
    _HARDWARE_CACHE.update(at=now, value=value)
    return value


def get_runtime_capabilities() -> Dict[str, Any]:
    """Return a normalized capability payload for this computer runtime."""
    cli_tools = get_available_cli_tools()
    return {
        "runtimes": ["shell"],
        "streaming": True,
        "persistence": "high",
        "desktop": True,
        "public_preview": False,
        "worktrees": True,
        "dev_servers": True,
        "git": True,
        "local_computer": True,
        "cerver_provider": True,
        "cli_tools": cli_tools,
        # Per-machine model inventory. Clients build their model picker from
        # this rather than a hardcoded list, so a model pulled on one laptop
        # doesn't get offered on another that hasn't got it.
        "local_models": get_local_models(),
        # What an agent checks before choosing this machine: hardware, and
        # which common tools are installed.
        "hardware": get_hardware(),
        "tools": get_installed_tools(),
    }
