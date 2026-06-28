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
import tempfile
from typing import Optional

from ..bridge_and_local_actions.cli_providers import CliProvider, get_provider
from ..infisical_client import get_secrets_sync, is_configured as infisical_configured


def resolve_cli_provider(cli_tool: str) -> CliProvider:
    """Resolve a CLI provider by name."""
    return get_provider(cli_tool)


from . import agent_environment


# Env vars that point Node / npm / version-managers at a specific
# install directory. When stale (an old NVM_DIR for a node version
# that's since been removed, an NPM_CONFIG_PREFIX pointing to a path
# that no longer exists), Node's module resolution silently picks
# them up and tries to load packages from the dead path — producing
# the opaque ENOENT-against-a-ghost-nvm-path error that ate hours
# of debugging time. We're better off forcing Node back to defaults
# (resolve from __dirname and PATH) than honoring a stale pointer.
_NODE_REDIRECT_VARS = (
    "NODE_PATH",
    "NODE_OPTIONS",       # could carry --preserve-symlinks etc.
    "NPM_CONFIG_PREFIX",
    "npm_config_prefix",
    "NPM_PREFIX",
    "NVM_DIR",
    "NVM_BIN",
    "NVM_INC",
    "NVM_CD_FLAGS",
    "PNPM_HOME",
    "VOLTA_HOME",
    "FNM_DIR",
    "FNM_MULTISHELL_PATH",
    "ASDF_DATA_DIR",      # only for the agent; doesn't disable asdf for the user
)


def _clean_room_base_env(clean_home: Optional[str]) -> dict:
    """Minimal env for a POOLED session running on a borrowed machine. Carries
    no host secrets and no inherited tokens — just enough to find and run the
    harness binary, with HOME isolated to a throwaway dir so the agent can't
    read the machine owner's ~/.claude login, ~/.codex/auth.json, etc. The only
    credential path is the gateway-injected extra_env (base-URL + ephemeral
    token). See POOLS.md §relay clean-room."""
    home = clean_home or tempfile.mkdtemp(prefix="cerver-pool-")
    tmp = os.path.join(home, "tmp")
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        tmp = tempfile.gettempdir()
    return {
        # PATH is a search-path list, not a secret; the probe below overwrites
        # it, but seed it so the binary is findable even if the probe is cold.
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "TMPDIR": tmp,
        "USER": "cerver-pool",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "en_US.UTF-8")),
        "TERM": os.environ.get("TERM", "xterm-256color"),
    }


def build_process_env(
    cli_cmd,
    extra_env: Optional[dict] = None,
    pool_session: bool = False,
    clean_home: Optional[str] = None,
) -> dict:
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
    # Clean-room for pooled (borrowed-machine) sessions: do NOT inherit the
    # host env or (below) the contributor's Infisical vault. Start from a
    # minimal base with an isolated HOME. Otherwise: the normal full host env.
    env = _clean_room_base_env(clean_home) if pool_session else os.environ.copy()

    # Always remove CLAUDECODE to allow nested launches.
    env.pop("CLAUDECODE", None)

    # Scrub stale Node / version-manager pointers BEFORE Infisical
    # layers on — otherwise a key in the vault literally named
    # `NPM_CONFIG_PREFIX` would re-poison the agent's resolution.
    # The agent_environment probe already handed us the right PATH;
    # let Node walk that for binaries and resolve modules from the
    # script's __dirname like it would in the user's shell.
    for var in _NODE_REDIRECT_VARS:
        env.pop(var, None)

    # Use the PATH that the agent_environment probe stitched together
    # at relay startup — host PATH + login-shell PATH + well-known
    # macOS/Linux install dirs. That probe knows where THIS machine's
    # codex/claude/node actually live, including version-manager dirs
    # like ~/.nvm/versions/node/<active>/bin which a static list can't
    # cover. Falls back to env["PATH"] when the probe hasn't run yet.
    probed = agent_environment.get_path_for_subprocess()
    if probed:
        env["PATH"] = probed

    # Layer Infisical-fetched secrets first so a rotated key in the vault
    # reaches the next CLI spawn without a relay restart — but only when
    # the provider hasn't explicitly overridden the same var.
    # Vault secrets are the CONTRIBUTOR's — never layer them into a pooled
    # session running someone else's job.
    if not pool_session and infisical_configured():
        infisical_env = get_secrets_sync()
        if infisical_env:
            env.update(infisical_env)

    # Second scrub pass — Infisical may carry these as named secrets
    # (one user had a stale `NPM_CONFIG_PREFIX=~/.nvm/versions/node/v20.19.0`
    # in their vault that survived the node uninstall). The env_inject /
    # extra_env layers below can still set them deliberately if a caller
    # really needs to point Node somewhere specific.
    for var in _NODE_REDIRECT_VARS:
        env.pop(var, None)

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
    system_prompt: Optional[str] = None,
):
    """Build a resume CLI command for a provider. `system_prompt` (a saved
    agent's instructions) is re-applied on resume — native session resume drops
    a previously-passed system prompt, so without this the agent persona is lost
    on follow-up turns."""
    return provider.build_resume_command(message, session_id, system_prompt=system_prompt)


def spawn_cli_subprocess(
    cli_cmd,
    cwd: str,
    extra_env: Optional[dict] = None,
    pool_session: bool = False,
) -> subprocess.Popen:
    """Spawn a CLI subprocess for the given command.

    extra_env: project-scoped env vars (secrets, config) to layer on top of
    the host's process env so the agent inherits them.

    Buffering note: `bufsize=1` (line-buffered) only takes effect when
    streams are opened in text mode. With binary stdout (which we want
    so the JSON-line reader can handle UTF-8 surrogate edge-cases without
    decoder errors), Python 3.10+ emits a RuntimeWarning and silently
    falls back to the default block size — making CLI replies arrive in
    chunks instead of line-by-line. That's been a hidden contributor to
    "no reply within 1m30s" timeouts for low-volume outputs.
    `bufsize=0` (unbuffered) is the right knob in binary mode: stdout is
    flushed on every write, the JSON-line reader gets events as the CLI
    emits them.
    """
    # For a pooled session, run clean-room with HOME isolated to the session's
    # own workspace (cwd) so the borrowed machine's owner creds stay invisible.
    env = build_process_env(
        cli_cmd,
        extra_env=extra_env,
        pool_session=pool_session,
        clean_home=cwd if pool_session else None,
    )
    return subprocess.Popen(
        cli_cmd.args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env,
        bufsize=0,
        universal_newlines=False,
    )


__all__ = [
    "resolve_cli_provider",
    "build_process_env",
    "build_run_cli_command",
    "build_resume_cli_command",
    "spawn_cli_subprocess",
]
