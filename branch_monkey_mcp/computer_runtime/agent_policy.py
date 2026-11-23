"""
What the owner's own agents may touch on this computer.

The owner sets it in the Cerver app (Security page); it lives in
``~/.cerver/security.json`` on the computer it applies to:

  {"agent_access": "as_you" | "own_folder",
   "blocked_folders": ["~/Documents/Taxes", "~/.ssh"]}

  as_you      today's behaviour: the agent works as you, everywhere you can.
  own_folder  the agent sees only the folder it was started in (plus its own
              settings and tools); the rest of your home folder is hidden.
  vm          the agent runs in a fresh Linux VM (Apple's container tool,
              provisioned with `cerver vm`) that sees only its folder.
  blocked_folders  never readable or writable by any agent, in either mode.
  allowed_folders  own_folder mode: folders agents may also open, besides the
              one they work in (e.g. ~/Code). Blocked folders still win.

Enforced with the system sandbox, the same one the guest fence uses
(guest_fence.py): sandbox-exec on macOS, bubblewrap on Linux. When a rule is
set and the sandbox is missing, the agent does not start: a rule the owner
set is never silently skipped.

Guests (other people's sessions) are always fenced by guest_fence; this file
is only about the owner's agents.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from typing import Dict, List, Optional

from . import guest_fence

POLICY_FILE = os.path.expanduser("~/.cerver/security.json")
MODES = ("as_you", "own_folder", "vm")

# The agents' own settings and logins: they must stay readable (and writable:
# sessions, history) in own_folder mode or the agent can't start.
_AGENT_HOME_ITEMS = (".claude", ".claude.json", ".codex", ".config/claude", ".config/codex", ".gemini", ".grok", ".cache", ".npm", ".local/state",
                    # Cerver's own programs and the local VM's data, so an agent can
                    # set up and check the VM (Provisioning → "Set up with an agent").
                    ".cerver/bin", "Library/Application Support/com.apple.container")


def load() -> Dict[str, object]:
    """The saved policy, with defaults. A broken file means the strict reading
    of what is there is impossible, so it falls back to as_you with no blocked
    folders, and says so in ``problem``."""
    out: Dict[str, object] = {"agent_access": "as_you", "blocked_folders": [], "allowed_folders": []}
    if not os.path.exists(POLICY_FILE):
        return out
    try:
        with open(POLICY_FILE) as f:
            raw = json.load(f)
    except Exception as e:  # noqa: BLE001
        out["problem"] = f"can't read {POLICY_FILE}: {e}"
        return out
    mode = raw.get("agent_access")
    if mode in MODES:
        out["agent_access"] = mode
    blocked = raw.get("blocked_folders")
    if isinstance(blocked, list):
        out["blocked_folders"] = [str(b) for b in blocked if isinstance(b, str) and b.strip()]
    allowed = raw.get("allowed_folders")
    if isinstance(allowed, list):
        out["allowed_folders"] = [str(b) for b in allowed if isinstance(b, str) and b.strip()]
    return out


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def blocked_paths(policy: Optional[Dict[str, object]] = None) -> List[str]:
    p = policy or load()
    return [_real(b) for b in p.get("blocked_folders", [])]  # type: ignore[union-attr]


def report() -> Dict[str, object]:
    """For the capabilities report: what the app shows for this computer."""
    p = load()
    ok, why = guest_fence.fence_status()
    out = {
        "mode": p["agent_access"],
        "blocked_folders": p["blocked_folders"],
        "allowed_folders": p.get("allowed_folders", []),
        "ask_folder": os.path.join(WORK_ROOT, "ask"),
        "enforced_by": why if ok else None,
    }
    if "problem" in p:
        out["problem"] = p["problem"]
    return out


def active(policy: Optional[Dict[str, object]] = None) -> bool:
    p = policy or load()
    return p["agent_access"] != "as_you" or bool(p["blocked_folders"])


def _sb(s: str) -> str:
    return guest_fence._sb_str(s)


def seatbelt_profile(cwd: str, path_value: str, policy: Dict[str, object]) -> str:
    """macOS profile. Later rules win: allow everything, hide homes (own_folder),
    open the work folder, agent settings and tools again, then hide the
    blocked folders last so nothing re-opens them."""
    home = os.path.realpath(os.path.expanduser("~"))
    lines = ["(version 1)", "(allow default)"]
    if policy["agent_access"] == "own_folder":
        lines += [
            '(deny file-read* file-write* (subpath "/Users") (subpath "/Volumes"))',
            '(allow file-read* (literal "/Users") (literal ' + _sb(home) + "))",
            f"(allow file-read* file-write* (subpath {_sb(os.path.realpath(cwd))}))",
        ]
        for a in policy.get("allowed_folders", []):  # type: ignore[union-attr]
            lines.append(f"(allow file-read* file-write* (subpath {_sb(_real(a))}))")
        for item in _AGENT_HOME_ITEMS:
            p = os.path.join(home, item)
            kind = "subpath" if not item.endswith(".json") else "literal"
            lines.append(f"(allow file-read* file-write* ({kind} {_sb(p)}))")
        # Keychain files: the login item the agent CLIs read through `security`.
        lines.append(f"(allow file-read* (subpath {_sb(os.path.join(home, 'Library/Keychains'))}))")
        for d in guest_fence._tool_read_dirs(path_value):
            lines.append(f"(allow file-read* (subpath {_sb(d)}))")
    for b in blocked_paths(policy):
        lines.append(f"(deny file-read* file-write* (subpath {_sb(b)}))")
    return "\n".join(lines) + "\n"


def bwrap_args(cwd: str, path_value: str, policy: Dict[str, object]) -> List[str]:
    home = os.path.realpath(os.path.expanduser("~"))
    args = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    if policy["agent_access"] == "own_folder":
        args += ["--tmpfs", "/home"]
        if os.path.isdir("/root"):
            args += ["--tmpfs", "/root"]
        for item in _AGENT_HOME_ITEMS:
            p = os.path.join(home, item)
            if os.path.exists(p):
                args += ["--bind", p, p]
        for d in guest_fence._tool_read_dirs(path_value):
            args += ["--ro-bind", d, d]
        c = os.path.realpath(cwd)
        args += ["--bind", c, c]
        for a in policy.get("allowed_folders", []):  # type: ignore[union-attr]
            r = _real(a)
            if os.path.isdir(r):
                args += ["--bind", r, r]
    for b in blocked_paths(policy):
        if os.path.isdir(b):
            args += ["--tmpfs", b]
        elif os.path.exists(b):
            args += ["--ro-bind", "/dev/null", b]
    return args + ["--chdir", os.path.realpath(cwd), "--die-with-parent", "--"]


def check_cwd(cwd: str, policy: Dict[str, object]) -> None:
    """Refuse a start in a blocked folder."""
    c = os.path.realpath(cwd)
    for b in blocked_paths(policy):
        if c == b or c.startswith(b.rstrip("/") + "/"):
            raise PermissionError(f"Agents can't work in {cwd}: it's a blocked folder on this computer.")


WORK_ROOT = os.path.expanduser("~/.cerver/work")


def work_folder(cwd: str, policy: Optional[Dict[str, object]] = None) -> str:
    """Where the agent works. Asked to work in the whole home folder (an
    "Ask Compute Pool" question has no project) while this computer keeps
    agents in their own folder: ~/.cerver/work/ask instead (one stable folder,
    so a follow-up finds its conversation)."""
    p = policy or load()
    if p["agent_access"] not in ("own_folder", "vm"):
        return cwd
    c = os.path.realpath(cwd)
    home = os.path.realpath(os.path.expanduser("~"))
    if c == home or c in ("/", "/Users", "/home"):
        # One stable folder, so a follow-up (--resume) finds its conversation.
        d = os.path.join(WORK_ROOT, "ask")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d
    return cwd


def wrap(argv: List[str], cwd: str, path_value: str, workspace: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> List[str]:
    """argv that runs the owner's agent under this computer's rules (unchanged
    when there are none). Raises PermissionError when a rule can't be kept."""
    policy = load()
    if not active(policy):
        return argv
    check_cwd(cwd, policy)
    if policy["agent_access"] == "vm":
        from . import vm_runtime
        return vm_runtime.wrap(argv, cwd, env or {})
    system = platform.system()
    if system == "Darwin":
        if not os.path.exists(guest_fence.SANDBOX_EXEC):
            raise PermissionError("This computer has security rules for agents, but sandbox-exec is missing.")
        d = workspace or os.path.join(os.path.expanduser("~/.cerver"), "fence")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"agent-{os.getpid()}-{abs(hash((cwd, tuple(argv)))) % 10**8}.sb")
        with open(path, "w") as f:
            f.write(seatbelt_profile(cwd, path_value, policy))
        return [guest_fence.SANDBOX_EXEC, "-f", path, *argv]
    if system == "Linux":
        if not shutil.which("bwrap"):
            raise PermissionError("This computer has security rules for agents; install bubblewrap (apt install bubblewrap) so they can be kept.")
        return [*bwrap_args(cwd, path_value, policy), *argv]
    raise PermissionError(f"This computer has security rules for agents, and {system} can't keep them yet.")
