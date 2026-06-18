"""Discover where this machine's agent binaries actually live.

Background
==========

The relay can be started from many contexts:
  - User's interactive shell (rich PATH)
  - install.sh shim (rich PATH inherited from the user shell)
  - launchd (sparse PATH — usually `/usr/bin:/bin:/usr/sbin:/sbin`)
  - A cron job (sparse PATH)
  - A nested subprocess of another tool

Whichever way the relay starts, it then spawns agent CLIs (`codex`,
`claude`, `grok`) via `bash -c "... | <cli> exec ..."`. Bash needs to
find the CLI on its PATH; the CLI's `#!/usr/bin/env node` shebang
needs to find node; and require.resolve inside the CLI walks
node_modules from there. If any link in that chain is missing, the
agent fails with an opaque ENOENT — the symptom that ate most of
2026-05-19 here.

Strategy
========

Probe once at relay startup. Build a canonical PATH from three sources,
priority-ordered, deduplicated, existence-checked:

  1. The relay's inherited PATH (whatever launchd / shell gave us).
  2. The user's interactive-shell PATH — captured by running
     `$SHELL -ilc 'echo $PATH'` so we get whatever they've configured
     in .zshrc / .bashrc (nvm, mise, asdf, brew, …). This is the
     reason `which codex` works in their terminal but the relay can't
     find it.
  3. A static set of well-known macOS / Linux install dirs. Catches
     fresh installs where the user hasn't sourced their rc yet.

Then resolve each binary we care about against that PATH, persist
the result to ~/.cerver/agent_env.json, and surface it through the
TUI's Provision tab. Subprocesses get the canonical PATH via
build_process_env in cli_runtime.py.

Re-probe on startup; the cache file is mostly there for the
Provision tab to read between probes.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

# Binaries the relay genuinely needs to spawn an agent at all.
# Missing any of these is a hard error worth surfacing.
REQUIRED_BINARIES = ("bash", "git", "node")

# Useful but not strictly required. The relay still functions without
# them; we just report what's available so the user knows what runs.
OPTIONAL_BINARIES = (
    "codex",
    "claude",
    "grok",
    "ollama",
    "npm",
    "go",
    "python3",
    "infisical",
    "uv",
    "bun",
    "cargo",
)

# Static fallback locations. Globs handle versioned dirs (nvm/fnm/asdf)
# since shutil.which doesn't recurse — the parent of `<version>/bin`
# is useless on PATH, only the leaf works.
_STATIC_DIRS = [
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/opt/homebrew/opt/node/bin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/snap/bin",
    "/usr/lib/go/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
    os.path.expanduser("~/.bun/bin"),
    os.path.expanduser("~/.cargo/bin"),
    os.path.expanduser("~/.asdf/shims"),
    os.path.expanduser("~/.volta/bin"),
    os.path.expanduser("~/go/bin"),
    os.path.expanduser("~/.cerver/bin"),
]

_GLOB_PATTERNS = [
    os.path.expanduser("~/.nvm/versions/node/*/bin"),
    os.path.expanduser("~/.fnm/node-versions/*/installation/bin"),
    os.path.expanduser("~/Library/Application Support/fnm/node-versions/*/installation/bin"),
    os.path.expanduser("~/.asdf/installs/nodejs/*/bin"),
    os.path.expanduser("~/.mise/installs/node/*/bin"),
]


@dataclass
class AgentEnv:
    """Snapshot of where the relay can find agent binaries."""
    path: str = ""
    binaries: Dict[str, str] = field(default_factory=dict)
    missing_required: List[str] = field(default_factory=list)
    probed_at: float = 0.0
    shell_path_captured: bool = False

    def get(self, name: str) -> Optional[str]:
        return self.binaries.get(name)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


_CACHE_PATH = Path(os.path.expanduser("~/.cerver/agent_env.json"))


def _capture_shell_path(timeout: float = 8.0) -> Optional[str]:
    """Run the user's login shell to pick up their interactive PATH.

    Why: the relay process inherits whatever launchd handed it, which
    typically excludes /opt/homebrew/bin and any version-manager
    shims (nvm/mise/asdf). The user's shell rc — sourced via `-il` —
    knows the truth.

    Risks:
      - Slow if .zshrc does heavy work; capped at 8s.
      - Some rc files print spurious stdout. We take the LAST line so
        a banner from `motd` or `figlet` in .zshrc doesn't poison us.
      - We pass a *minimal* env so the shell doesn't echo our own
        sparse PATH back as the answer.

    Returns the PATH string or None.
    """
    shell = os.environ.get("SHELL") or "/bin/zsh"
    if not os.path.exists(shell):
        return None
    home = os.path.expanduser("~")
    try:
        # `-i -l -c` together = "load my full env then run this".
        # Some shells warn on -i without a tty; we discard stderr.
        result = subprocess.run(
            [shell, "-ilc", "printf '\\n__CERVER_PATH__:%s\\n' \"$PATH\""],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"HOME": home, "SHELL": shell, "TERM": "dumb"},
        )
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            if line.startswith("__CERVER_PATH__:"):
                return line[len("__CERVER_PATH__:"):].strip()
    except Exception:
        pass
    return None


def _candidate_dirs(initial_path: str, shell_path: Optional[str]) -> List[str]:
    """Order: inherited PATH, then shell-rc PATH, then static fallbacks,
    then version-manager globs. Deduplicated, existence-checked.
    """
    seen: set[str] = set()
    out: List[str] = []

    def add(d: str) -> None:
        if not d or d in seen:
            return
        try:
            if os.path.isdir(d):
                out.append(d)
                seen.add(d)
        except OSError:
            pass

    for p in (initial_path or "").split(os.pathsep):
        add(p)
    if shell_path:
        for p in shell_path.split(os.pathsep):
            add(p)
    for p in _STATIC_DIRS:
        add(p)
    for pattern in _GLOB_PATTERNS:
        try:
            for p in sorted(glob.glob(pattern)):
                add(p)
        except OSError:
            pass
    return out


def probe(initial_path: Optional[str] = None) -> AgentEnv:
    """Discover where this machine's agent binaries live.

    Run once at relay startup, again on operator demand (touch
    ~/.cerver/agent_env.refresh between sessions). The returned
    snapshot drives both subprocess PATH (cli_runtime.build_process_env)
    and the Provision tab's "Discovered" section.
    """
    src_path = initial_path if initial_path is not None else os.environ.get("PATH", "")
    shell_path = _capture_shell_path()
    dirs = _candidate_dirs(src_path, shell_path)
    canonical = os.pathsep.join(dirs)

    binaries: Dict[str, str] = {}
    missing_required: List[str] = []
    for name in (*REQUIRED_BINARIES, *OPTIONAL_BINARIES):
        resolved = shutil.which(name, path=canonical)
        if resolved:
            binaries[name] = resolved
        elif name in REQUIRED_BINARIES:
            missing_required.append(name)

    env = AgentEnv(
        path=canonical,
        binaries=binaries,
        missing_required=missing_required,
        probed_at=time.time(),
        shell_path_captured=bool(shell_path),
    )

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(env.to_json() + "\n")
    except Exception:
        # Best-effort cache; never block startup on disk weirdness.
        pass

    return env


def load_cached() -> Optional[AgentEnv]:
    """Read the last probe result from disk. Used by code paths that
    can't (or shouldn't) re-probe — e.g. the Provision tab refresh
    loop. Returns None if the cache is missing or unreadable.
    """
    if not _CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(_CACHE_PATH.read_text())
        return AgentEnv(
            path=raw.get("path", ""),
            binaries=raw.get("binaries", {}) or {},
            missing_required=list(raw.get("missing_required", []) or []),
            probed_at=float(raw.get("probed_at", 0.0) or 0.0),
            shell_path_captured=bool(raw.get("shell_path_captured", False)),
        )
    except Exception:
        return None


# Module-level singleton populated by `probe()` at startup. cli_runtime
# reads `current.path` for every subprocess spawn; the Provision tab
# reads `current.binaries` for display. Both gracefully fall back to
# os.environ["PATH"] when current is None (probe hasn't run yet).
current: Optional[AgentEnv] = None


def set_current(env: AgentEnv) -> None:
    """Install a probed env as the active one. Called once at startup
    after `probe()` returns.
    """
    global current
    current = env


def get_path_for_subprocess() -> str:
    """The PATH to inject into spawned agent subprocesses.

    Falls back to os.environ['PATH'] if probe hasn't run — better
    than nothing, and avoids a hard dependency on the probe order.
    """
    if current and current.path:
        return current.path
    return os.environ.get("PATH", "")
