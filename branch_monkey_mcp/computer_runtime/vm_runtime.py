"""
The local VM on an Apple computer: what's provisioned, and running an agent in it.

Provisioning is done by the Cerver CLI (`cerver vm install | start | login`,
p_71 cmd/vm.go), from the Provisioning section of the Cerver app. It uses
Apple's own `container` tool, where each container is a small Linux VM, and a
Cerver image with the coding agents (``cerver-agent:1``).

Here the relay only:
  status()  what's provisioned, for the capabilities report (each computer's
            Provisioning section in the app and in /v2/pool)
  wrap()    argv that runs the owner's agent in a fresh VM that sees only the
            folder it works in; used when the Security page says "In a VM"
"""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from typing import Dict, List, Optional

CONTAINER = "/usr/local/bin/container"
IMAGE = "cerver-agent:1"  # same name as cmd/vm.go VMImage
ENV_FILE = os.path.expanduser("~/.cerver/vm.env")

# Environment the agent needs inside the VM. Never the Mac's PATH, HOME or
# anything else: the VM has its own.
_PASS_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "OPENAI_", "CODEX_", "CERVER_SESSION", "GH_TOKEN", "GITHUB_TOKEN")


def _run(args: List[str], timeout: float = 8) -> Optional[str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _mac_major() -> int:
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except Exception:  # noqa: BLE001
        return 0


def status() -> Dict[str, object]:
    s: Dict[str, object] = {"supported": False, "installed": False, "running": False, "image": False, "signed_in": False}
    if platform.system() != "Darwin":
        s["why"] = "the local VM uses Apple's container tool, which runs on Macs"
    elif platform.machine() != "arm64":
        s["why"] = "needs an Apple silicon Mac"
    elif _mac_major() < 26:
        s["why"] = "needs macOS 26 or later"
    else:
        s["supported"] = True
    if os.path.exists(CONTAINER):
        s["installed"] = True
        v = _run([CONTAINER, "--version"])
        if v:
            s["version"] = v.strip()
        if _run([CONTAINER, "system", "status"]) is not None:
            s["running"] = True
            out = _run([CONTAINER, "image", "list"]) or ""
            name, tag = IMAGE.split(":")
            for line in out.splitlines():
                f = line.split()
                if len(f) >= 2 and f[0].endswith(name) and f[1] == tag:
                    s["image"] = True
    try:
        with open(ENV_FILE) as f:
            s["signed_in"] = "CLAUDE_CODE_OAUTH_TOKEN=" in f.read()
    except OSError:
        pass
    s["ready"] = bool(s["supported"] and s["installed"] and s["running"] and s["image"])
    return s


def wrap(argv: List[str], cwd: str, env: Dict[str, str]) -> List[str]:
    """argv that runs the agent CLI in a fresh VM: only ``cwd`` is mounted (at
    /work), the CLI is the VM's own copy, and only the agent's credentials
    cross over. Raises PermissionError when the VM can't be used, so the agent
    never silently runs on the Mac instead."""
    st = status()
    if not st["ready"]:
        raise PermissionError(
            "This computer runs agents in a VM, but the VM isn't ready. Open the Cerver app → this computer → Provisioning."
        )
    if not st["signed_in"] and not env.get("ANTHROPIC_API_KEY"):
        raise PermissionError("Agents in the VM need your Claude login: Cerver app → Provisioning → Sign in the VM.")
    args = [CONTAINER, "run", "--rm", "--volume", f"{os.path.realpath(cwd)}:/work", "--workdir", "/work"]
    if os.path.exists(ENV_FILE):
        args += ["--env-file", ENV_FILE]  # the token never appears in a process list
    # Other credentials (e.g. a project's keys) go through a private env file.
    extra = {k: v for k, v in env.items() if k.startswith(_PASS_PREFIXES)}
    if extra:
        fd, path = tempfile.mkstemp(prefix="cerver-vm-env-", dir=os.path.expanduser("~/.cerver"))
        with os.fdopen(fd, "w") as f:
            for k, v in extra.items():
                f.write(f"{k}={v}\n")
        os.chmod(path, 0o600)
        args += ["--env-file", path]
        # The CLI reads it before the VM starts; removed a minute later.
        subprocess.Popen(["/bin/sh", "-c", f"sleep 60; rm -f '{path}'"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The agent CLI inside the VM: its own copy, by name (claude, codex).
    cli = os.path.basename(argv[0]) if argv else "claude"
    return [*args, IMAGE, cli, *argv[1:]]
