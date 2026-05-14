"""
CLI Provider abstraction for supporting multiple AI coding CLI tools.

Supports Claude Code CLI and OpenAI Codex CLI with a unified interface
for command building, output normalization, and availability checking.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class CliCommand:
    """A CLI command ready to execute."""
    args: List[str]
    env_overrides: Dict[str, None]  # Keys to remove from env
    env_inject: Dict[str, str] = None  # Keys to add/set in env

    def __post_init__(self):
        if self.env_inject is None:
            self.env_inject = {}


class CliProvider:
    """Base class for CLI tool providers."""

    name: str = ""
    display_name: str = ""
    install_hint: str = ""
    install_cmd: List[str] = []  # e.g. ["npm", "install", "-g", "package-name"]
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
            result = subprocess.run(
                self.install_cmd,
                capture_output=True, text=True, timeout=120,
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr
            return {"success": success, "output": output.strip()}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "Install timed out"}
        except Exception as e:
            return {"success": False, "output": str(e)}

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

        Checks: 1) API key in ~/.kompany/config.json, 2) API key in env.
        Returns dict of env vars to set when spawning the CLI process.
        """
        if self.api_key_config:
            config = _load_config()
            stored_key = config.get(self.api_key_config)
            if stored_key:
                return {self.api_key_env: stored_key}
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

        Common format follows Claude Code's stream-json structure:
        - {"type": "system", "subtype": "init", "session_id": "..."}
        - {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
        - {"type": "result", "result": "..."}

        Returns None to skip/filter the event.
        """
        return raw_json

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
    api_key_env = "ANTHROPIC_API_KEY"
    api_key_config = "anthropic_api_key"

    def is_available(self) -> Optional[str]:
        return shutil.which("claude")

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
    api_key_env = "OPENAI_API_KEY"
    api_key_config = "openai_api_key"

    def is_available(self) -> Optional[str]:
        return shutil.which("codex")

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
        return CliCommand(
            args=[
                "bash", "-c",
                f"cat '{prompt_file}' | codex exec - --dangerously-bypass-approvals-and-sandbox -o '{out_file}' > /dev/null 2>&1; cat '{out_file}'; rm -f '{prompt_file}' '{out_file}'"
            ],
            env_overrides={},
            env_inject=self.get_auth_env(),
        )

    def build_run_command(self, prompt, system_prompt=None, model=None):
        prompt_file = self._write_prompt_file(prompt, system_prompt)
        # codex supports -c key=value overrides on its global config —
        # putting `-c model="<name>"` ahead of `exec` is the same as
        # editing ~/.codex/config.toml's `model =` line for this call only.
        model_arg = f' -c model="{model}"' if model else ""
        return CliCommand(
            args=[
                "bash", "-c",
                f"cat '{prompt_file}' | codex{model_arg} exec - --dangerously-bypass-approvals-and-sandbox --json; rm -f '{prompt_file}'"
            ],
            env_overrides={},
            env_inject=self.get_auth_env(),
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
            env_overrides={},
            env_inject=self.get_auth_env(),
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "codex",
                "exec", prompt,
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            ],
            env_overrides={},
            env_inject=self.get_auth_env(),
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
    api_key_env = "XAI_API_KEY"
    api_key_config = "xai_api_key"

    def is_available(self) -> Optional[str]:
        return shutil.which("grok")

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
        args = [
            "claude",
            "-p", prompt,
            "--output-format", output_format,
            "--dangerously-skip-permissions"
        ]
        if output_format == "stream-json":
            args.append("--verbose")
        if model:
            args.extend(["--model", model])
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])
        return CliCommand(
            args=args,
            env_overrides={"CLAUDECODE": None},
            env_inject={
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **(auth_env if auth_env else {}),
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

    def build_resume_command(self, prompt, session_id):
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--resume", session_id,
                "--dangerously-skip-permissions"
            ],
            env_overrides={"CLAUDECODE": None},
            env_inject={
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

    def build_oneshot_command(self, prompt):
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--dangerously-skip-permissions"
            ],
            env_overrides={"CLAUDECODE": None},
            env_inject={
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

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
