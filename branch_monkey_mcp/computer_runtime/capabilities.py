"""
Runtime capability helpers for local and hosted computer environments.

This module gathers the machine-level capabilities that higher layers
like the Cerver provider adapter can expose without depending directly
on route handlers.
"""

from typing import Any, Dict, List

from ..bridge_and_local_actions.cli_providers import get_available_providers


def get_available_cli_tools() -> List[str]:
    """Return installed CLI tool names."""
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
    }
