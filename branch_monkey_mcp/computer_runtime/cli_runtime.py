"""
CLI execution primitives for the reusable computer runtime.

This module intentionally stops short of owning session lifecycle.
It only handles:
- provider resolution
- command construction
- environment shaping
- subprocess spawning
"""

import os
import subprocess
from typing import Optional

from ..bridge_and_local_actions.cli_providers import CliProvider, get_provider
from ..infisical_client import get_secrets_sync, is_configured as infisical_configured


def resolve_cli_provider(cli_tool: str) -> CliProvider:
    """Resolve a CLI provider by name."""
    return get_provider(cli_tool)


def build_process_env(cli_cmd, extra_env: Optional[dict] = None) -> dict:
    """Build the environment for a CLI command.

    Layering, lowest → highest precedence:
      1. host process env (os.environ)
      2. cli_cmd.env_inject (provider-specific overrides)
      3. Infisical-fetched secrets (cached by infisical_client)
      4. extra_env (per-call caller intent — wins on conflict)

    extra_env: caller-supplied env vars (e.g. project-scoped secrets passed
    through from kompany or cerver session metadata).
    """
    env = os.environ.copy()
    for key in cli_cmd.env_overrides:
        env.pop(key, None)

    # Always remove CLAUDECODE to allow nested launches.
    env.pop("CLAUDECODE", None)

    if cli_cmd.env_inject:
        env.update(cli_cmd.env_inject)

    # Layer Infisical-fetched secrets on top of host env so a rotated
    # ANTHROPIC_API_KEY in Infisical reaches the next CLI spawn without a
    # relay restart. extra_env still takes precedence so the caller's
    # explicit per-session intent wins.
    if infisical_configured():
        infisical_env = get_secrets_sync()
        if infisical_env:
            env.update(infisical_env)

    if extra_env:
        env.update(extra_env)

    return env


def build_run_cli_command(
    provider: CliProvider,
    prompt: str,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
):
    """Build a new-run CLI command for a provider. `model` is the
    per-call override coming from cerver session metadata.cli_model
    (set by `cerver run --model X`); each provider injects it into
    its own native flag (claude/grok: --model; codex: -c model=...)."""
    return provider.build_run_command(prompt, system_prompt=system_prompt, model=model)


def build_resume_cli_command(
    provider: CliProvider,
    message: str,
    session_id: str,
):
    """Build a resume CLI command for a provider."""
    return provider.build_resume_command(message, session_id)


def spawn_cli_subprocess(cli_cmd, cwd: str, extra_env: Optional[dict] = None) -> subprocess.Popen:
    """Spawn a CLI subprocess for the given command.

    extra_env: project-scoped env vars (secrets, config) to layer on top of
    the host's process env so the agent inherits them.
    """
    env = build_process_env(cli_cmd, extra_env=extra_env)
    return subprocess.Popen(
        cli_cmd.args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env,
        bufsize=1,
        universal_newlines=False,
    )


__all__ = [
    "resolve_cli_provider",
    "build_process_env",
    "build_run_cli_command",
    "build_resume_cli_command",
    "spawn_cli_subprocess",
]
