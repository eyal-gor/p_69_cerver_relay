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
      2. Infisical-fetched secrets (cached vault defaults)
      3. cli_cmd.env_inject (provider-specific overrides — intentional)
      4. extra_env (per-call caller intent — wins on conflict)

    Why env_inject beats Infisical: providers occasionally repurpose a
    standard env var (e.g. GrokProvider sets ANTHROPIC_API_KEY to the
    xAI key so the claude CLI talks to api.x.ai). Letting Infisical's
    real ANTHROPIC_API_KEY clobber that override sends an Anthropic
    key to xAI's endpoint, which 400s as "Incorrect API key". The
    provider knows what it's doing; Infisical does not.

    extra_env: caller-supplied env vars (e.g. project-scoped secrets passed
    through from kompany or cerver session metadata).
    """
    env = os.environ.copy()

    # Always remove CLAUDECODE to allow nested launches.
    env.pop("CLAUDECODE", None)

    # Layer Infisical-fetched secrets first so a rotated key in the vault
    # reaches the next CLI spawn without a relay restart — but only when
    # the provider hasn't explicitly overridden the same var.
    if infisical_configured():
        infisical_env = get_secrets_sync()
        if infisical_env:
            env.update(infisical_env)

    if cli_cmd.env_inject:
        env.update(cli_cmd.env_inject)

    if extra_env:
        env.update(extra_env)

    # Apply env_overrides LAST. Popping these early (before Infisical) let
    # Infisical's vault silently re-add a key the provider had explicitly
    # said "remove" — most painfully, CodexProvider strips OPENAI_API_KEY
    # for subscription mode, but Infisical's OPENAI_API_KEY would re-land
    # and force _finalize_codex_env to materialize a tmp api-mode
    # CODEX_HOME with mismatched auth, making codex exit 1 silently.
    #
    # extra_env (per-call caller intent) still wins: if the caller
    # explicitly set a key that the provider wants stripped, respect the
    # caller — they know what they're doing.
    if cli_cmd.env_overrides:
        for key in cli_cmd.env_overrides:
            if extra_env and key in extra_env:
                continue
            env.pop(key, None)

    # Final-pass hook: providers that need to react to the *merged* env
    # (e.g. CodexProvider materializing a temp CODEX_HOME when the
    # extra_env layer just added OPENAI_API_KEY but codex's auth.json
    # still has OAuth tokens) wire it through cli_cmd.env_finalize.
    finalize = getattr(cli_cmd, "env_finalize", None)
    if callable(finalize):
        try:
            env = finalize(env) or env
        except Exception as exc:
            print(f"[cli_runtime] env_finalize failed: {exc}")

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
