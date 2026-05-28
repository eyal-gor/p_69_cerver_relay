"""
CLI Provider abstraction for supporting multiple AI coding CLI tools.

Supports Claude Code CLI and OpenAI Codex CLI with a unified interface
for command building, output normalization, and availability checking.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


# Cache the augmented PATH once per process — globbing nvm/fnm/asdf
# version dirs hits the filesystem and a relay can call _resolve_cli
# several times per second during compute polling.
_resolver_path_cache: Optional[str] = None


def _build_resolver_path() -> str:
    """Build an augmented PATH for binary lookup without mutating the
    process environment. Used by `_resolve_cli` below.

    Why this isn't `os.environ["PATH"] = …` at import time:
    that mutation leaks into every subprocess the relay spawns,
    including the agent child processes themselves — making PATH
    order load-bearing on import order, and silently changing what
    a user-spawned tool sees. Keep the augmentation local to *lookup*.

    Why we glob version-manager dirs:
    `~/.nvm/versions/node` is a parent of `vNN.N.N/bin` subdirs;
    `shutil.which` won't recurse, so adding the parent does nothing.
    Same for fnm and asdf. We resolve the actual `<version>/bin`
    paths once and append them.
    """
    static_dirs = [
        "/opt/homebrew/bin",            # Homebrew on Apple Silicon
        "/opt/homebrew/sbin",
        "/usr/local/bin",               # Homebrew on Intel + generic
        "/usr/local/sbin",
        "/opt/homebrew/opt/node/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.npm-global/bin"),
        os.path.expanduser("~/.asdf/shims"),
        os.path.expanduser("~/.volta/bin"),
        os.path.expanduser("~/.bun/bin"),
        os.path.expanduser("~/.cargo/bin"),
    ]
    pnpm_home = os.environ.get("PNPM_HOME")
    if pnpm_home:
        static_dirs.append(pnpm_home)

    # nvm, fnm, asdf: each <version>/bin needs to be a leaf PATH entry.
    glob_patterns = [
        os.path.expanduser("~/.nvm/versions/node/*/bin"),
        os.path.expanduser("~/.fnm/node-versions/*/installation/bin"),
        os.path.expanduser("~/Library/Application Support/fnm/node-versions/*/installation/bin"),
        os.path.expanduser("~/.asdf/installs/nodejs/*/bin"),
    ]
    versioned_dirs: List[str] = []
    for pat in glob_patterns:
        try:
            versioned_dirs.extend(sorted(glob.glob(pat)))
        except OSError:
            # Permission errors / odd filesystems — silently skip.
            pass

    current = (os.environ.get("PATH", "") or "").split(os.pathsep)
    seen = set(current)
    extras: List[str] = []
    for p in static_dirs + versioned_dirs:
        if not p or p in seen:
            continue
        try:
            if os.path.isdir(p):
                extras.append(p)
                seen.add(p)
        except OSError:
            pass
    return os.pathsep.join(current + extras) if extras else (os.environ.get("PATH", "") or "")


def _resolve_cli(name: str) -> Optional[str]:
    """`shutil.which` with our augmented PATH. Returns None if missing.

    Per-call resolver — the augmented PATH lives in a module cache,
    not in `os.environ`, so we don't accidentally hand a modified
    environment to subprocesses. Subprocesses that need to find these
    binaries themselves should compose their own PATH (see, e.g.,
    relay_tui's update-CLI subprocess augmentation).
    """
    global _resolver_path_cache
    # Standard PATH lookup first — covers the common case where the
    # user's interactive shell has launched us and PATH is rich.
    found = shutil.which(name)
    if found:
        return found
    if _resolver_path_cache is None:
        _resolver_path_cache = _build_resolver_path()
    return shutil.which(name, path=_resolver_path_cache)


def _invalidate_resolver_path_cache() -> None:
    """Drop the cached augmented PATH. Call after install/upgrade flows
    that add a new binary so `is_available()` re-discovers it on the
    next probe without a relay restart.
    """
    global _resolver_path_cache
    _resolver_path_cache = None


@dataclass
class CliCommand:
    """A CLI command ready to execute."""
    args: List[str]
    env_overrides: Dict[str, None]  # Keys to remove from env
    env_inject: Dict[str, str] = None  # Keys to add/set in env
    # Optional post-merge env hook. Runs AFTER os.environ + Infisical +
    # env_inject + extra_env have been merged, so the callable sees the
    # final env that's about to be handed to the subprocess. Returns a
    # (possibly new) dict. Used by CodexProvider to materialize a temp
    # CODEX_HOME when OPENAI_API_KEY is present but auth.json has OAuth.
    env_finalize: Optional[Callable[[Dict[str, str]], Dict[str, str]]] = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self):
        if self.env_inject is None:
            self.env_inject = {}


class CliProvider:
    """Base class for CLI tool providers."""

    name: str = ""
    display_name: str = ""
    install_hint: str = ""
    install_cmd: List[str] = []  # e.g. ["npm", "install", "-g", "package-name"]
    health_cmd: List[str] = []   # e.g. ["codex", "--version"]
    api_key_env: str = ""       # Env var name for API key (e.g. ANTHROPIC_API_KEY)
    api_key_config: str = ""    # Config key in ~/.kompany/config.json

    def is_available(self) -> Optional[str]:
        """Return path if CLI is installed, None otherwise."""
        raise NotImplementedError

    def install(self) -> dict:
        """Install the CLI. Returns {success: bool, output: str}."""
        if not self.install_cmd:
            return {"success": False, "output": "No install command configured"}
        try:
            env = os.environ.copy()
            resolver_path = _build_resolver_path()
            if resolver_path:
                env["PATH"] = resolver_path
            result = subprocess.run(
                self.install_cmd,
                capture_output=True, text=True, timeout=120,
                env=env,
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr
            if success:
                _invalidate_resolver_path_cache()
            return {"success": success, "output": output.strip()}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "Install timed out"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def health_check(self) -> dict:
        """Verify the installed CLI can actually start.

        `is_available()` only proves a wrapper exists on PATH. It does not
        catch the failure mode that broke the Mac mini: the wrapper was
        present, but pointed at a missing/wrong-architecture vendor binary.
        """
        path = self.is_available()
        if not path:
            return {"ok": False, "path": None, "detail": "not installed"}

        cmd = self.health_cmd or [self.name, "--version"]
        try:
            env = os.environ.copy()
            resolver_path = _build_resolver_path()
            if resolver_path:
                env["PATH"] = resolver_path
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=15,
                env=env,
            )
            output = (result.stdout + result.stderr).strip()
            return {
                "ok": result.returncode == 0,
                "path": path,
                "detail": output[-500:] if output else f"exit {result.returncode}",
            }
        except Exception as e:
            return {"ok": False, "path": path, "detail": str(e)}

    def get_auth_status(self) -> dict:
        """Check authentication status.

        Returns dict with:
          - authenticated: bool
          - method: str - 'api_key', 'oauth', 'none'
          - detail: str - email, key hint, or error message
        """
        return {"authenticated": False, "method": "none", "detail": "Not implemented"}

    def get_auth_env(self) -> Dict[str, str]:
        """Return env vars to inject for authentication.

        Resolution order for the API key:
          1. `~/.kompany/config.json` (TUI-stored key, machine-local)
          2. Infisical cache (synced from the user's own vault at relay
             startup — same source the spawned CLI would see in its env)
          3. Host process env (`os.environ`)

        Returning {} means env_inject leaves the env's existing
        `<api_key_env>` untouched. That used to be fine but causes a
        cross-vendor leak for proxy providers (grok runs claude → xAI
        with ANTHROPIC_API_KEY=<xai-key>; without a key here, claude
        ships Infisical's real Anthropic key to xAI and gets a 400).
        Checking Infisical + env recovers the right key when only the
        TUI hasn't been used on this machine.
        """
        if not self.api_key_config:
            return {}
        config = _load_config()
        stored_key = config.get(self.api_key_config)
        if stored_key:
            return {self.api_key_env: stored_key}

        try:
            from ..infisical_client import get_secrets_sync
            secrets = get_secrets_sync()
            vault_key = secrets.get(self.api_key_env)
            if vault_key:
                return {self.api_key_env: vault_key}
        except Exception:
            pass

        env_key = os.environ.get(self.api_key_env)
        if env_key:
            return {self.api_key_env: env_key}
        return {}

    def set_api_key(self, key: str):
        """Store an API key in persistent config."""
        if not self.api_key_config:
            raise ValueError(f"{self.display_name} does not support API key auth")
        _save_config({self.api_key_config: key})

    def clear_api_key(self):
        """Remove stored API key from config."""
        if self.api_key_config:
            config = _load_config()
            config.pop(self.api_key_config, None)
            _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)

    def start_device_auth(self) -> Optional[dict]:
        """Start device auth flow. Returns {url, code} or None if not supported."""
        return None

    def build_text_command(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        use_mcp: bool = False,
    ) -> CliCommand:
        """Build command for one-shot text output (no streaming JSON).
        Used by kompany-workflow llm for clean text responses."""
        raise NotImplementedError

    def build_run_command(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> CliCommand:
        """Build command to run a new prompt. `model`, when set, overrides
        the CLI's local default — passed in from cerver's session metadata
        (metadata.cli_model) when the user runs `cerver run --model X`."""
        raise NotImplementedError

    def build_resume_command(
        self,
        prompt: str,
        session_id: str,
    ) -> CliCommand:
        """Build command to resume a session."""
        raise NotImplementedError

    def build_oneshot_command(
        self,
        prompt: str,
    ) -> CliCommand:
        """Build command for a one-shot (non-streaming) invocation."""
        raise NotImplementedError

    def normalize_event(self, raw_json: dict) -> Optional[dict]:
        """Normalize a JSON output event to the common format.

        Each provider subclass parses its CLI's wire format and emits
        events in this shared vocabulary so downstream consumers (the
        gateway transcript, cerver-cli's WaitForReply loop, the dashboard
        renderer) never need a CLI-specific code path.

        Common format follows Claude Code's stream-json structure:

        - {"type": "system", "subtype": "init", "session_id": "..."}
            Once per session, on CLI start. session_id is the native
            CLI session id (used for native --resume).

        - {"type": "assistant",
           "message": {"content": [{"type": "text", "text": "..."}]}}
            Assistant text turn. Tool-using agents emit several of these
            interleaved with tool_use / tool_result.

        - {"type": "assistant",
           "message": {"content": [{"type": "tool_use",
                                    "name": "...", "input": {...}}]}}
            Tool invocation by the model.

        - {"type": "tool_result", "content": "..."}
            Tool's response back to the model.

        - {"type": "result", "result": "...", "usage": {...}}
            END OF ONE TURN (not the session). The CLI may keep running
            and accept more input. Codex's `turn.completed` maps here.

        - {"type": "session_completed",
           "exit_code": int, "duration_ms": int,
           "total_usage": {input_tokens, output_tokens, turns}}
            END OF THE CLI PROCESS. The single deterministic "this
            session is fully over, no more events" signal. Synthesized
            by the supervisor on `subprocess.wait()` return — none of
            the CLIs emit this themselves; process exit IS the signal.
            See `make_session_completed_event()` for the constructor.
            Downstream consumers should prefer this over transcript
            quiescence heuristics. exit_code 0 = success.

        Returns None to skip/filter the event.
        """
        return raw_json

    # Synthetic event constructors -----------------------------------
    # These are produced by the supervisor, not by parsing CLI stdout.
    # Centralized here so the wire shape stays identical across CLIs
    # and across emission sites (one supervisor today, possibly several
    # tomorrow). Adapters that need to vary any field can override.

    @staticmethod
    def make_session_completed_event(
        exit_code: int,
        duration_ms: int,
        total_usage: Optional[dict] = None,
    ) -> dict:
        """Construct the canonical session_completed event.

        Called by the process supervisor when the CLI subprocess exits.
        Push the returned dict into the transcript the same way per-turn
        events flow. This is the signal cerver-cli watches for to know
        the run is truly over — replacing the quiescence-based timeout
        heuristic that was tuned per-CLI and still missed slow codex runs.

        Args:
            exit_code: Process exit code. 0 = success, non-zero = error.
            duration_ms: Wall-clock CLI lifetime in milliseconds, from
                spawn to exit.
            total_usage: Aggregated token counts across all turns. Shape:
                {"input_tokens": int, "output_tokens": int, "turns": int}.
                Pass None when the supervisor didn't track per-turn
                usage; consumers must treat absence as "unknown," not
                zero.
        """
        event: dict = {
            "type": "session_completed",
            "exit_code": int(exit_code),
            "duration_ms": int(duration_ms),
        }
        if total_usage is not None:
            event["total_usage"] = total_usage
        return event

    def extract_session_id(self, event: dict) -> Optional[str]:
        """Extract session ID from an init event, if present."""
        return None

    def is_noise(self, text: str) -> bool:
        """Return True if this stderr/non-JSON line should be filtered."""
        return False


class ClaudeCodeProvider(CliProvider):
    """Claude Code CLI provider."""

    name = "claude"
    display_name = "Claude Code"
    install_hint = "npm install -g @anthropic-ai/claude-code"
    install_cmd = ["npm", "install", "-g", "@anthropic-ai/claude-code"]
    health_cmd = ["claude", "--version"]
    api_key_env = "ANTHROPIC_API_KEY"
    api_key_config = "anthropic_api_key"

    def is_available(self) -> Optional[str]:
        return _resolve_cli("claude")

    def get_auth_status(self) -> dict:
        """Check Claude Code auth: try `claude auth status` (JSON output)."""
        # 1. Check for stored API key in our config
        config = _load_config()
        if config.get(self.api_key_config):
            key = config[self.api_key_config]
            hint = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key ({hint})"}

        # 2. Check for API key in environment
        env_key = os.environ.get(self.api_key_env)
        if env_key:
            hint = env_key[:8] + "..." + env_key[-4:] if len(env_key) > 12 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key from env ({hint})"}

        # 3. Check CLI's own auth via `claude auth status`
        path = self.is_available()
        if not path:
            return {"authenticated": False, "method": "none", "detail": "CLI not installed"}

        try:
            result = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get("loggedIn"):
                    email = data.get("email", "")
                    method = data.get("authMethod", "oauth")
                    sub = data.get("subscriptionType", "")
                    detail = f"{email}" + (f" ({sub})" if sub else "")
                    return {"authenticated": True, "method": method, "detail": detail}
            return {"authenticated": False, "method": "none", "detail": "Not signed in"}
        except Exception as e:
            return {"authenticated": False, "method": "none", "detail": str(e)}

    def start_device_auth(self) -> Optional[dict]:
        """Start Claude device auth — opens browser via `claude auth login`."""
        import webbrowser

        path = self.is_available()
        if not path:
            return None

        try:
            # claude auth login is interactive — spawn it detached.
            # It opens the browser automatically for Anthropic OAuth.
            subprocess.Popen(
                ["claude", "auth", "login"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            return {
                "type": "browser",
                "message": "Opening browser for Anthropic sign-in...",
                "url": "https://console.anthropic.com",
            }
        except Exception:
            return None

    def _build_env_overrides(self) -> Dict[str, None]:
        """Determine which env vars to strip.

        If user has a stored API key in our config, DON'T strip it — we'll inject it.
        If no stored key, strip ANTHROPIC_API_KEY so Claude uses its own OAuth.
        """
        config = _load_config()
        if config.get(self.api_key_config):
            # User has stored an API key — don't strip, we'll inject it
            return {"CLAUDECODE": None}
        return {"ANTHROPIC_API_KEY": None, "CLAUDECODE": None}

    def _build_env_inject(self) -> Dict[str, str]:
        """Return env vars to inject (stored API key)."""
        return self.get_auth_env()

    def build_text_command(self, prompt, system_prompt=None, use_mcp=False):
        args = [
            "claude",
            "-p", prompt,
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        if use_mcp:
            for candidate in [
                Path.cwd() / ".mcp.json",
                Path.home() / ".mcp.json",
                Path.home() / "Code" / "p_63_branch_monkey" / ".mcp.json",
            ]:
                if candidate.exists():
                    args.extend(["--mcp-config", str(candidate)])
                    break
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        return CliCommand(
            args=args,
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
        )

    def build_run_command(self, prompt, system_prompt=None, model=None):
        args = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions"
        ]
        if model:
            args.extend(["--model", model])
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        return CliCommand(
            args=args,
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
        )

    def build_resume_command(self, prompt, session_id):
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--resume", session_id,
                "--dangerously-skip-permissions"
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--dangerously-skip-permissions"
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
        )

    def extract_session_id(self, event):
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
        return None

    def is_noise(self, text):
        noise_prefixes = ("warn:", "Warning:", "DeprecationWarning", "[DEP")
        noise_substrings = ("oven-sh/bun", "baseline.zip", "baseline build")
        return text.startswith(noise_prefixes) or any(s in text for s in noise_substrings)


class CodexProvider(CliProvider):
    """OpenAI Codex CLI provider."""

    name = "codex"
    display_name = "Codex CLI"
    install_hint = "npm install -g @openai/codex"
    install_cmd = ["npm", "install", "-g", "@openai/codex"]
    health_cmd = ["codex", "--version"]
    api_key_env = "OPENAI_API_KEY"
    api_key_config = "openai_api_key"

    # Cached `codex login status` result for the relay's lifetime — the
    # check shells out and adds ~150ms to every spawn otherwise.
    _subscription_cache: Optional[bool] = None

    def is_available(self) -> Optional[str]:
        return _resolve_cli("codex")

    def _has_subscription(self) -> bool:
        """True iff `codex login status` reports a signed-in OAuth session.

        Cached for the relay's lifetime — re-login is rare and the worst
        case (stale True) lets codex itself surface the auth error.
        """
        if CodexProvider._subscription_cache is not None:
            return CodexProvider._subscription_cache
        if not self.is_available():
            CodexProvider._subscription_cache = False
            return False
        try:
            result = subprocess.run(
                ["codex", "login", "status"],
                capture_output=True, text=True, timeout=4,
            )
            CodexProvider._subscription_cache = result.returncode == 0
        except Exception:
            CodexProvider._subscription_cache = False
        return CodexProvider._subscription_cache

    def get_auth_env(self) -> Dict[str, str]:
        """Resolve codex auth env.

        Subscription wins over a stored api key — injecting OPENAI_API_KEY
        forces codex into per-token billing even when the user has
        ChatGPT Plus/Pro signed in. That used to silently fail (and bill
        the api key) on `cerver compare` runs where the user expected
        subscription mode. If the user explicitly wants api mode they
        can pass `--bill api`, which flows the key in via `extra_env`
        (highest precedence) and overrides codex's OAuth resolution.
        """
        if self._has_subscription():
            return {}
        return super().get_auth_env()

    def _build_env_overrides(self) -> Dict[str, None]:
        """Strip an inherited OPENAI_API_KEY when subscription is active.

        Without this, a key sitting in Infisical or the host shell would
        force codex into api mode despite an active `codex login`. Strip
        leaves codex to resolve its own OAuth. `cerver run --bill api`
        re-injects the user-provided key via `extra_env` (highest
        precedence), and `_finalize_codex_env` swaps in a synthetic
        CODEX_HOME for that key so the api path actually fires.
        """
        if self._has_subscription():
            return {"OPENAI_API_KEY": None}
        return {}

    def _finalize_codex_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """Force codex into api-key mode when a key is present.

        Codex CLI's `~/.codex/auth.json` holds both an OAuth pair and an
        api key, and `auth_mode = "chatgpt"` makes it always prefer OAuth
        — `OPENAI_API_KEY` env var is silently ignored. The only way to
        flip auth_mode for a single spawn is to redirect `CODEX_HOME` at
        a directory whose auth.json has just the api key. We materialize
        that dir on demand, symlinking session/cache state out of the
        user's real codex home so prior context survives.
        """
        api_key = env.get("OPENAI_API_KEY")
        if not api_key:
            return env
        # Already overridden — caller knows what they're doing.
        if env.get("CODEX_HOME"):
            return env

        real_home = Path(os.path.expanduser("~/.codex"))
        # Subscription user with no api key intent: respect OAuth.
        if not real_home.exists():
            return env

        try:
            import tempfile
            tmp_home = Path(tempfile.mkdtemp(prefix="codex-api-"))
            # Symlink the bulky / stateful subdirs so we don't lose
            # sessions, memories, plugins, etc.
            for sub in ("sessions", "memories", "plugins", "cache",
                        "rules", "ambient-suggestions", "browser",
                        "computer-use"):
                src = real_home / sub
                if src.exists():
                    try:
                        os.symlink(src, tmp_home / sub)
                    except FileExistsError:
                        pass
            # Copy config.toml as-is (the projects.<dir>.trust_level
            # entries are what avoid an interactive trust prompt).
            real_cfg = real_home / "config.toml"
            if real_cfg.exists():
                import shutil as _shutil
                _shutil.copyfile(real_cfg, tmp_home / "config.toml")
            # API-only auth.json — no `tokens` block means OAuth path
            # is skipped and codex reports "Logged in using an API key".
            (tmp_home / "auth.json").write_text(json.dumps({
                "OPENAI_API_KEY": api_key,
            }))
            env = {**env, "CODEX_HOME": str(tmp_home)}
        except Exception as exc:
            print(f"[CodexProvider] failed to materialize api-mode CODEX_HOME: {exc}")
        return env

    def set_api_key(self, key: str):
        """Store API key in our config AND register with `codex login --with-api-key`."""
        super().set_api_key(key)
        # Also register with Codex's own auth system
        if self.is_available():
            try:
                subprocess.run(
                    ["codex", "login", "--with-api-key"],
                    input=key, text=True, capture_output=True, timeout=10,
                )
            except Exception:
                pass  # Best effort — env var injection still works as fallback

    def get_auth_status(self) -> dict:
        """Check Codex auth: stored API key or `codex login status`."""
        # 1. Check for stored API key in our config
        config = _load_config()
        if config.get(self.api_key_config):
            key = config[self.api_key_config]
            hint = key[:7] + "..." + key[-4:] if len(key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key ({hint})"}

        # 2. Check for API key in environment
        env_key = os.environ.get(self.api_key_env)
        if env_key:
            hint = env_key[:7] + "..." + env_key[-4:] if len(env_key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key from env ({hint})"}

        # 3. Check CLI's own auth via `codex login status`
        path = self.is_available()
        if not path:
            return {"authenticated": False, "method": "none", "detail": "CLI not installed"}

        try:
            result = subprocess.run(
                ["codex", "login", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                return {"authenticated": True, "method": "oauth", "detail": output or "Signed in"}
            return {"authenticated": False, "method": "none", "detail": "Not signed in"}
        except Exception as e:
            return {"authenticated": False, "method": "none", "detail": str(e)}

    def start_device_auth(self) -> Optional[dict]:
        """Start Codex device auth — runs `codex login --device-auth` and captures URL+code."""
        import re
        import time

        path = self.is_available()
        if not path:
            return None

        try:
            # Use Popen so the process stays alive while user completes auth in browser.
            # Read lines until we capture the URL and code, then return.
            proc = subprocess.Popen(
                ["codex", "login", "--device-auth"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )

            output = ""
            end_time = time.time() + 8
            while time.time() < end_time:
                line = proc.stdout.readline()
                if not line:
                    break
                output += line
                # Stop once we see the device code pattern
                if re.search(r'[A-Z0-9]{4,5}-[A-Z0-9]{4,5}', line):
                    break

            # Strip ANSI escape codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', output)

            url_match = re.search(r'(https://\S+)', clean)
            code_match = re.search(r'([A-Z0-9]{4,5}-[A-Z0-9]{4,5})', clean)

            if url_match:
                # Don't kill the process — it needs to stay alive to complete
                # the auth flow when the user approves in the browser.
                return {
                    "type": "device_code",
                    "url": url_match.group(1),
                    "code": code_match.group(1) if code_match else None,
                    "message": "Visit the URL and enter the code to sign in",
                }

            proc.kill()
            return None
        except Exception:
            return None

    def _write_prompt_file(self, prompt, system_prompt=None):
        """Codex has no --system-prompt flag. Write merged prompt to a temp file."""
        import tempfile
        full = f"{system_prompt}\n\n---\n\n{prompt}" if system_prompt else prompt
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.md', prefix='codex-prompt-', delete=False)
        f.write(full)
        f.close()
        return f.name

    def build_text_command(self, prompt, system_prompt=None, use_mcp=False):
        prompt_file = self._write_prompt_file(prompt, system_prompt)
        import tempfile
        out_file = tempfile.mktemp(suffix='.txt', prefix='codex-out-')
        # Discard codex's verbose stdout (it duplicates the -o file) but
        # keep stderr flowing — when codex fails (bad model, expired auth,
        # rate limit), the only useful diagnostic lives in stderr.
        #
        # Capture codex's exit code via ${PIPESTATUS[1]} (the second pipe
        # element) so the trailing `rm` doesn't overwrite it. Without
        # this, a wrong-arch codex binary or auth failure exited 0 here
        # because rm always succeeds — silently turning a hard failure
        # into "no assistant message produced" with no diagnostic trail.
        return CliCommand(
            args=[
                "bash", "-c",
                f"cat '{prompt_file}' | codex exec - --dangerously-bypass-approvals-and-sandbox -o '{out_file}' > /dev/null; code=${{PIPESTATUS[1]}}; cat '{out_file}' 2>/dev/null; rm -f '{prompt_file}' '{out_file}'; exit $code"
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self.get_auth_env(),
            env_finalize=self._finalize_codex_env,
        )

    def build_run_command(self, prompt, system_prompt=None, model=None):
        prompt_file = self._write_prompt_file(prompt, system_prompt)
        # codex supports -c key=value overrides on its global config —
        # putting `-c model="<name>"` ahead of `exec` is the same as
        # editing ~/.codex/config.toml's `model =` line for this call only.
        model_arg = f' -c model="{model}"' if model else ""
        # ${PIPESTATUS[1]} preserves codex's real exit code through the
        # trailing rm; see build_text_command above for the rationale.
        return CliCommand(
            args=[
                "bash", "-c",
                f"cat '{prompt_file}' | codex{model_arg} exec - --dangerously-bypass-approvals-and-sandbox --json; code=${{PIPESTATUS[1]}}; rm -f '{prompt_file}'; exit $code"
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self.get_auth_env(),
            env_finalize=self._finalize_codex_env,
        )

    def build_resume_command(self, prompt, session_id):
        # Codex syntax: codex exec resume <session_id> <prompt> --dangerously-bypass-approvals-and-sandbox --json
        return CliCommand(
            args=[
                "codex",
                "exec", "resume", session_id, prompt,
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self.get_auth_env(),
            env_finalize=self._finalize_codex_env,
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "codex",
                "exec", prompt,
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self.get_auth_env(),
            env_finalize=self._finalize_codex_env,
        )

    def normalize_event(self, raw_json):
        """Normalize Codex JSON output to Claude stream-json format.

        Codex v0.115+ emits:
          {"type":"thread.started","thread_id":"..."}
          {"type":"turn.started"}
          {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
          {"type":"item.started","item":{"type":"command_execution","command":"...","status":"in_progress"}}
          {"type":"item.completed","item":{"type":"command_execution","command":"...","exit_code":0,"aggregated_output":"..."}}
          {"type":"turn.completed","usage":{...}}

        We normalize to Claude's stream-json format.
        """
        event_type = raw_json.get("type", "")

        # Thread start → system init
        if event_type == "thread.started":
            return {
                "type": "system",
                "subtype": "init",
                "session_id": raw_json.get("thread_id", ""),
                "provider": "codex"
            }

        # Turn started → skip (no equivalent needed)
        if event_type == "turn.started":
            return None

        # Item events — the main content
        if event_type in ("item.completed", "item.started"):
            item = raw_json.get("item", {})
            item_type = item.get("type", "")

            # Agent message → assistant text
            if item_type == "agent_message":
                text = item.get("text", "")
                if not text:
                    return None
                return {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": text}]
                    }
                }

            # Command execution → tool use / tool result
            if item_type == "command_execution":
                command = item.get("command", "")
                status = item.get("status", "")

                if event_type == "item.started" or status == "in_progress":
                    # Tool invocation
                    return {
                        "type": "assistant",
                        "message": {
                            "content": [{
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": command}
                            }]
                        }
                    }
                else:
                    # Tool result (completed)
                    output = item.get("aggregated_output", "")
                    exit_code = item.get("exit_code", None)
                    result_text = output
                    if exit_code is not None and exit_code != 0:
                        result_text += f"\n(exit code: {exit_code})"
                    return {
                        "type": "tool_result",
                        "content": result_text
                    }

            # File operations or other item types
            if item_type in ("file_read", "file_write", "file_edit"):
                fname = item.get("file", item.get("path", ""))
                if event_type == "item.started":
                    return {
                        "type": "assistant",
                        "message": {
                            "content": [{
                                "type": "tool_use",
                                "name": item_type.replace("_", " ").title().replace(" ", ""),
                                "input": {"file": fname}
                            }]
                        }
                    }
                else:
                    return {
                        "type": "tool_result",
                        "content": item.get("output", item.get("text", f"Completed: {fname}"))
                    }

        # Turn completed → result
        if event_type == "turn.completed":
            return {
                "type": "result",
                "result": "",
                "usage": raw_json.get("usage", {})
            }

        # Failures — surface as a visible assistant message instead of
        # dropping silently. Without this, an API rejection (e.g. invalid
        # model name) lets the codex process exit cleanly but produces no
        # transcript, so the CLI's WaitForReply hangs for 3 minutes and
        # the user sees only "no reply within 3m0s" — no clue what broke.
        if event_type in ("error", "turn.failed"):
            err_msg = raw_json.get("message")
            if not err_msg and isinstance(raw_json.get("error"), dict):
                err_msg = raw_json["error"].get("message", "")
            # OpenAI sometimes nests a JSON error blob inside `message`;
            # unwrap the inner human-readable string when present.
            if isinstance(err_msg, str) and err_msg.startswith("{"):
                try:
                    import json as _json
                    parsed = _json.loads(err_msg)
                    inner = parsed.get("error", {}).get("message")
                    if inner:
                        err_msg = inner
                except Exception:
                    pass
            return {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "text",
                        "text": f"[codex error] {err_msg or 'unknown failure'}"
                    }]
                }
            }

        # Pass through unknown events
        return raw_json

    def extract_session_id(self, event):
        # Check Codex thread.started format
        if event.get("type") == "thread.started":
            return event.get("thread_id")
        # Check normalized format
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
        return None

    def is_noise(self, text):
        noise_prefixes = ("warn:", "Warning:", "DeprecationWarning", "[DEP", "npm warn")
        noise_substrings = ("ERROR codex_core::skills", "ERROR codex_core::codex", "failed to load skill", "failed to stat skills")
        return text.startswith(noise_prefixes) or any(s in text for s in noise_substrings)


class GrokProvider(CliProvider):
    """xAI Grok CLI provider (runs Claude Code with Grok models via proxy)."""

    name = "grok"
    display_name = "Grok"
    install_hint = "npm install -g grok-cli"
    install_cmd = ["npm", "install", "-g", "grok-cli"]
    health_cmd = ["grok", "--version"]
    api_key_env = "XAI_API_KEY"
    api_key_config = "xai_api_key"

    def is_available(self) -> Optional[str]:
        return _resolve_cli("grok")

    def get_auth_status(self) -> dict:
        """Check Grok auth: stored API key or XAI_API_KEY env var."""
        # 1. Check stored key
        config = _load_config()
        if config.get(self.api_key_config):
            key = config[self.api_key_config]
            hint = key[:7] + "..." + key[-4:] if len(key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key ({hint})"}

        # 2. Check env
        env_key = os.environ.get(self.api_key_env)
        if env_key:
            hint = env_key[:7] + "..." + env_key[-4:] if len(env_key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key from env ({hint})"}

        path = self.is_available()
        if not path:
            return {"authenticated": False, "method": "none", "detail": "CLI not installed"}

        return {"authenticated": False, "method": "none", "detail": "No API key — get one at console.x.ai"}

    def build_run_command(self, prompt, system_prompt=None, model=None):
        # grok-cli runs claude code under a proxy, so output is claude stream-json format.
        # We pass the API key via -k flag if stored, otherwise grok uses its keychain.
        args = ["grok"]
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        if api_key:
            args.extend(["-k", api_key])

        # grok starts the proxy and then spawns claude, which reads from stdin.
        # For non-interactive use, we need to pass claude args after --.
        # Actually grok-cli spawns claude -p automatically — we need to set
        # CLAUDE_CODE_ARGS or similar. Let's check...
        # grok-cli runs: claude -p <prompt> with proxy env vars.
        # We'll set the prompt via env vars that grok passes to claude.

        # grok-cli doesn't support passing prompts directly — it spawns
        # interactive claude. For our use case we run claude directly with
        # the proxy env vars that grok would set.
        return self._grok_cli_command(prompt, "stream-json", auth_env, api_key, model=model)

    def build_text_command(self, prompt, system_prompt=None, use_mcp=False):
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return self._grok_cli_command(prompt, "text", auth_env, api_key, system_prompt)

    def _grok_cli_command(self, prompt, output_format, auth_env, api_key, system_prompt=None, model=None):
        # Direct path to xAI — bypass the `claude` binary entirely. Routing
        # grok through `claude -p` made grok inherit claude's own
        # subscription rate limit: when the Anthropic Max quota was
        # exhausted, the claude binary 400'd ("You have reached your
        # specified API usage limits") before the HTTP call to xAI was
        # even made. xai_runner.py speaks the same stream-json output
        # the relay parser already consumes, but hits xAI directly so
        # claude's quota state is irrelevant.
        args = [
            sys.executable, "-m",
            "branch_monkey_mcp.bridge_and_local_actions.xai_runner",
            "-p", prompt,
            "--output-format", output_format,
        ]
        if output_format == "stream-json":
            args.append("--verbose")
        if model:
            args.extend(["--model", model])
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])
        # Drop ANTHROPIC_API_KEY from inherited env so a stray Anthropic
        # key can't land in the xai_runner's auth path. The runner picks
        # ANTHROPIC_API_KEY (env_inject) first, then XAI_API_KEY.
        return CliCommand(
            args=args,
            env_overrides={"CLAUDECODE": None, "ANTHROPIC_API_KEY": None},
            env_inject={
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **(auth_env if auth_env else {}),
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

    def build_resume_command(self, prompt, session_id):
        # xai_runner is stateless w.r.t. session_id — chat-style resume is a
        # cerver-level concern (the relay re-feeds prior transcript on each
        # turn). Behave the same as a fresh run for now; the model only sees
        # the current prompt, which mirrors the pre-existing claude-binary
        # behavior since `--resume` doesn't restore conversational state at
        # xAI either (xAI is stateless).
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return self._grok_cli_command(prompt, "stream-json", auth_env, api_key)

    def build_oneshot_command(self, prompt):
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return self._grok_cli_command(prompt, "json", auth_env, api_key)

    def extract_session_id(self, event):
        # Same as Claude — grok uses claude under the hood
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
        return None

    def is_noise(self, text):
        noise_prefixes = ("warn:", "Warning:", "DeprecationWarning", "[DEP")
        noise_substrings = ("oven-sh/bun", "baseline.zip", "baseline build", "proxy")
        return text.startswith(noise_prefixes) or any(s in text for s in noise_substrings)


# --- Provider Registry ---

_PROVIDERS: Dict[str, CliProvider] = {
    "claude": ClaudeCodeProvider(),
    "codex": CodexProvider(),
    "grok": GrokProvider(),
}

# Fallback default when no persistent config exists
_FALLBACK_CLI = "claude"

# Persistent config path
_CONFIG_FILE = Path.home() / ".kompany" / "config.json"


def _load_config() -> dict:
    """Load ~/.kompany/config.json."""
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(updates: dict):
    """Merge and save to ~/.kompany/config.json."""
    config = _load_config()
    config.update(updates)
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_default_cli() -> str:
    """Get the default CLI provider from persistent config, falling back to 'claude'."""
    saved = _load_config().get("default_cli")
    if saved and saved in _PROVIDERS:
        return saved
    return _FALLBACK_CLI


def set_default_cli(name: str):
    """Set the default CLI provider and persist to config."""
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown CLI provider: {name}. Available: {list(_PROVIDERS.keys())}")
    _save_config({"default_cli": name})


def get_provider(name: Optional[str] = None) -> CliProvider:
    """Get a CLI provider by name. Falls back to default, then to claude."""
    name = name or get_default_cli()
    provider = _PROVIDERS.get(name)
    if not provider:
        raise ValueError(f"Unknown CLI provider: {name}. Available: {list(_PROVIDERS.keys())}")
    # If the resolved provider isn't installed, fall back to claude
    if not provider.is_available() and name != _FALLBACK_CLI:
        fallback = _PROVIDERS.get(_FALLBACK_CLI)
        if fallback and fallback.is_available():
            print(f"[CliProviders] {provider.display_name} not installed, falling back to {fallback.display_name}")
            return fallback
    return provider


def get_available_providers() -> Dict[str, dict]:
    """Return info about all registered providers and their availability + auth status."""
    default = get_default_cli()
    result = {}
    for name, provider in _PROVIDERS.items():
        path = provider.is_available()
        auth = provider.get_auth_status()
        result[name] = {
            "name": name,
            "display_name": provider.display_name,
            "installed": path is not None,
            "path": path,
            "install_hint": provider.install_hint,
            "is_default": name == default,
            "authenticated": auth.get("authenticated", False),
            "auth_method": auth.get("method", "none"),
            "auth_detail": auth.get("detail", ""),
        }
    return result


def provision_cli_providers(names: Optional[List[str]] = None) -> Dict[str, dict]:
    """Install or repair CLI providers needed by the local relay.

    This runs during relay startup so a registered Cerver compute is not
    "ready" while its local CLI wrappers are broken. Missing auth is not a
    provisioning failure; auth is handled separately by the TUI / vault.
    """
    selected = names or list(_PROVIDERS.keys())
    results: Dict[str, dict] = {}
    for name in selected:
        provider = _PROVIDERS.get(name)
        if not provider:
            results[name] = {"ok": False, "action": "skip", "detail": "unknown provider"}
            continue

        before = provider.health_check()
        if before.get("ok"):
            results[name] = {
                "ok": True,
                "action": "verified",
                "path": before.get("path"),
                "detail": before.get("detail", ""),
            }
            continue

        if not provider.install_cmd:
            results[name] = {
                "ok": False,
                "action": "missing",
                "path": before.get("path"),
                "detail": before.get("detail", "no install command configured"),
            }
            continue

        install = provider.install()
        after = provider.health_check()
        results[name] = {
            "ok": bool(after.get("ok")),
            "action": "installed" if install.get("success") else "install_failed",
            "path": after.get("path") or before.get("path"),
            "detail": after.get("detail") if after.get("ok") else install.get("output") or after.get("detail"),
        }
    return results
