"""
The guest fence: what stops someone else's session from reading the owner's files.

A pooled machine runs sessions for people its owner approved. Those sessions
run as the owner's OS user, so without a fence a guest's ``cat ~/.ssh/id_rsa``
works. Every guest command therefore runs inside the operating system's own
sandbox, allowed into one throwaway workspace and nothing else of the owner's:

  macOS  sandbox-exec (Seatbelt), the same mechanism Claude Code and Codex use
  Linux  bubblewrap (bwrap): an empty /home, the workspace bound in, the rest
         of the system read-only

What a guest can do: run the machine's tools (python, node, git, compilers,
the agent CLIs), use the network, read and write its own workspace, and read
and write the owner's opt-in shared folder (``CERVER_POOL_SHARE_DIR``, or
``~/CerverShared`` if it exists).

What a guest cannot do: read or write anything under /Users or /home (other
than the workspace, the shared folder and the directories its tools live in,
read-only), write anywhere else on the system, use the keychain tool or
AppleScript, or signal the owner's processes.

If no fence is available (Linux without bwrap, Windows), guest sessions are
refused: ``fence_status()`` says why, and the relay reports
``guest_fence: false`` so the gateway never sends it someone else's work.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
import uuid
from typing import Dict, List, Optional, Tuple

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Agent CLIs and runtimes often live in the owner's home (~/.local/bin/claude,
# ~/.nvm, ~/.bun). Their directories are readable inside the fence (never
# writable) so a guest's agent can still start.
_TOOL_NAMES = ("claude", "codex", "node", "npm", "npx", "python3", "python", "bun", "uv", "deno", "go", "cargo", "grok", "gemini")


def _home_roots() -> List[str]:
    return ["/Users", "/Volumes"] if platform.system() == "Darwin" else ["/home", "/root"]


def _under(path: str, roots: List[str]) -> bool:
    p = os.path.realpath(path)
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def share_dir() -> Optional[str]:
    """The owner's opt-in shared folder, or None."""
    explicit = os.environ.get("CERVER_POOL_SHARE_DIR", "").strip()
    cand = os.path.expanduser(explicit) if explicit else os.path.expanduser("~/CerverShared")
    return os.path.realpath(cand) if os.path.isdir(cand) else None


def _tool_read_dirs(path_value: str) -> List[str]:
    """Directories under a home that hold tools on PATH, plus where their
    symlinks point (claude → ~/.local/share/claude/versions/…). Read-only."""
    roots = _home_roots()
    out: List[str] = []

    def add(d: str) -> None:
        d = os.path.realpath(d)
        if _under(d, roots) and d not in out and d not in roots:
            out.append(d)

    for d in (path_value or "").split(os.pathsep):
        if not d or not os.path.isdir(d) or not _under(d, roots):
            continue
        add(d)
        for name in _TOOL_NAMES:
            f = os.path.join(d, name)
            if os.path.islink(f) or os.path.isfile(f):
                target = os.path.realpath(f)
                # The package dir, not just the bin dir (node needs its lib/),
                # but never a whole top-level folder like ~/.local or ~/.bun:
                # those hold other apps' data too.
                parent = os.path.dirname(target)
                pkg = os.path.dirname(parent) if os.path.basename(parent) == "bin" else parent
                add(pkg if _depth_in_home(pkg) >= 2 else parent)
    # A PATH dir that is itself a top-level home folder (rare) stays; drop any
    # entry that would expose a whole home-level folder like ~/.local.
    return [d for d in out if _depth_in_home(d) >= 2 or os.path.basename(d) == "bin"]


def _depth_in_home(path: str) -> int:
    """Folders below the user's home: ~/.local → 1, ~/.nvm/versions → 2."""
    p = os.path.realpath(path)
    for root in _home_roots():
        if p.startswith(root.rstrip("/") + "/"):
            # /Users/<name>/a/b → parts after the user folder
            return max(0, len(p[len(root):].strip("/").split("/")) - 1)
    return 99


def new_workspace(hint: str = "") -> str:
    """A fresh, empty folder for one guest session, outside every home."""
    base = "/private/tmp/cerver-pool" if platform.system() == "Darwin" else os.path.join(tempfile.gettempdir(), "cerver-pool")
    os.makedirs(base, mode=0o755, exist_ok=True)
    safe = "".join(c for c in hint if c.isalnum() or c in "-_")[:40] or uuid.uuid4().hex[:12]
    ws = os.path.join(base, f"{safe}-{uuid.uuid4().hex[:6]}")
    os.makedirs(ws, mode=0o700)
    os.makedirs(os.path.join(ws, "tmp"), mode=0o700, exist_ok=True)
    return os.path.realpath(ws)


def fence_status() -> Tuple[bool, str]:
    """(available, how/why) for this machine."""
    system = platform.system()
    if system == "Darwin":
        if os.path.exists(SANDBOX_EXEC):
            return True, "macOS sandbox-exec"
        return False, "sandbox-exec is missing"
    if system == "Linux":
        if shutil.which("bwrap"):
            return True, "bubblewrap"
        return False, "install bubblewrap (apt install bubblewrap / dnf install bubblewrap) to run other people's sessions"
    return False, f"{system} has no guest fence yet"


def _sb_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(workspace: str, path_value: str) -> str:
    """The macOS profile. Later rules win, so: allow, then fence off homes and
    all writes, then open the workspace, shared folder and tool dirs again."""
    ws = os.path.realpath(workspace)
    share = share_dir()
    tools = _tool_read_dirs(path_value)
    lines = [
        "(version 1)",
        "(allow default)",
        # Every home and mounted volume, read and write.
        "(deny file-read* file-write* (subpath \"/Users\") (subpath \"/Volumes\") (subpath \"/private/var/root\"))",
        # Listing /Users itself is harmless and some tools stat it.
        "(allow file-read* (literal \"/Users\"))",
        # No writes anywhere…
        "(deny file-write* (subpath \"/\"))",
        # …except the workspace and devices (/dev/null, ttys).
        f"(allow file-read* file-write* (subpath {_sb_str(ws)}))",
        "(allow file-write* (subpath \"/dev\"))",
    ]
    if share:
        lines.append(f"(allow file-read* file-write* (subpath {_sb_str(share)}))")
    for d in tools:
        lines.append(f"(allow file-read* (subpath {_sb_str(d)}))")
    lines += [
        # Keychain and app scripting are ways out of the fence.
        "(deny process-exec (literal \"/usr/bin/security\") (literal \"/usr/bin/osascript\"))",
        # Only its own processes.
        "(deny signal)",
        "(allow signal (target same-sandbox))",
    ]
    return "\n".join(lines) + "\n"


def bwrap_args(workspace: str, path_value: str) -> List[str]:
    """The Linux fence: the system read-only, homes empty, the workspace bound in."""
    ws = os.path.realpath(workspace)
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for d in ("/home", "/root", "/run/user", "/var/tmp"):
        if os.path.isdir(d):
            args += ["--tmpfs", d]
    for d in _tool_read_dirs(path_value):
        args += ["--ro-bind", d, d]
    share = share_dir()
    if share:
        args += ["--bind", share, share]
    args += ["--bind", ws, ws, "--chdir", ws, "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--new-session", "--"]
    return args


def guest_env(workspace: str, path_value: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """No inherited variables: the owner's tokens stay out. Only what tools
    need, HOME and TMPDIR inside the workspace, plus what the gateway sent."""
    tmp = os.path.join(workspace, "tmp")
    env = {
        "PATH": path_value or "/usr/local/bin:/usr/bin:/bin",
        "HOME": workspace,
        "TMPDIR": tmp,
        "USER": "cerver-guest",
        "LOGNAME": "cerver-guest",
        "SHELL": "/bin/sh",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "CERVER_GUEST": "1",
    }
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def wrap(argv: List[str], workspace: str, path_value: str) -> List[str]:
    """Return argv that runs ``argv`` inside the fence. Raises if there is none:
    a guest command never runs unfenced."""
    ok, why = fence_status()
    if not ok:
        raise PermissionError(f"This machine can't run other people's sessions safely: {why}.")
    if platform.system() == "Darwin":
        profile_path = os.path.join(workspace, ".cerver-fence.sb")
        with open(profile_path, "w") as f:
            f.write(seatbelt_profile(workspace, path_value))
        # The profile lives in the workspace but the guest can't loosen it:
        # sandbox-exec reads it once, before the guest's process starts.
        return [SANDBOX_EXEC, "-f", profile_path, *argv]
    return [*bwrap_args(workspace, path_value), *argv]
