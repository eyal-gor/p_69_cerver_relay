"""
Cerver-facing compute helpers for the p69 local computer runtime.

This module is the beginning of the thin adapter layer that presents the
local runtime as a Cerver-compatible compute provider.
"""

from typing import Any, Dict

from fastapi import HTTPException

from ..computer_runtime.capabilities import get_runtime_capabilities
from ..computer_runtime.machine_state import get_machine_state


def infer_provider_workflow(metadata: Dict[str, Any]) -> str:
    """Infer the local execution workflow from provider metadata."""
    explicit = metadata.get("workflow")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    if metadata.get("public_preview") is True:
        return "workspace"

    return "execute"


def build_provider_agent_payload(
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Map provider metadata into the local agent/session creation payload."""
    task = metadata.get("task")
    title = metadata.get("session_name") or metadata.get("title") or task or "Cerver local session"
    workload = metadata.get("workload")

    description_parts = []
    if isinstance(task, str) and task.strip():
        description_parts.append(task.strip())
    if isinstance(workload, str) and workload.strip():
        description_parts.append(f"workload: {workload.strip()}")

    bootstrap_prompt = metadata.get("bootstrap_prompt")
    # Passive shadows (kompany's transcript receivers) must NEVER auto-spawn
    # a CLI — kompany already spawned the real agent via /local-claude/agents
    # and a second CLI here doubles Anthropic API calls and triggers
    # api_retry storms. metadata.passive=True forces defer_start regardless.
    is_passive = bool(metadata.get("passive") or metadata.get("shadow"))
    has_bootstrap = bool(isinstance(bootstrap_prompt, str) and bootstrap_prompt.strip())
    # If cerver handed us a bootstrap prompt (e.g. from a session resume
    # synthesizing context out of a prior transcript), the agent has work
    # to do *now* — defer_start would leave it waiting forever for a /run
    # call that never comes. Defer only when the caller has nothing for
    # the agent to do yet, or when the session is explicitly passive.
    return {
        "title": title,
        "description": " | ".join(description_parts) if description_parts else None,
        "working_dir": metadata.get("working_dir"),
        "workflow": infer_provider_workflow(metadata),
        "branch": metadata.get("branch"),
        "defer_start": is_passive or not has_bootstrap,
        "cli_tool": metadata.get("cli_tool"),
        "cli_model": metadata.get("cli_model"),
        "prompt": None if is_passive else bootstrap_prompt,
    }


def get_provider_info() -> Dict[str, Any]:
    """Return provider metadata for the local computer."""
    machine_state = get_machine_state()
    return {
        "provider": "cerver_local_provider",
        "label": "Local Computer",
        "mode": machine_state["mode"],
        "status": machine_state["status"],
        "machine_id": machine_state.get("machine_id"),
        "machine_name": machine_state.get("machine_name"),
        "last_heartbeat": machine_state.get("last_heartbeat"),
        "working_directory": machine_state.get("working_directory"),
        "capabilities": get_runtime_capabilities(),
    }


def build_provider_session_response(
    agent_id: str,
    agent: Dict[str, Any],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the normalized provider create-session response."""
    return {
        "sandbox_id": agent_id,
        "remote_sandbox_id": agent_id,
        "provider": "cerver_local_provider",
        "engine": metadata.get("engine", "shell"),
        "status": "ready",
        "created_at": agent.get("created_at"),
        "metadata": {
            **metadata,
            "cwd": agent.get("work_dir"),
            "branch": agent.get("branch"),
            "worktree_path": agent.get("worktree_path"),
            "cli_tool": agent.get("cli_tool"),
        },
        "capabilities": [
            "shell",
            "streaming",
            "local-computer",
            "resume",
            "worktree",
        ],
    }


async def create_provider_session(
    agent_manager: Any,
    metadata: Dict[str, Any],
    engine: str = "shell",
    timeout_ms: Any = None,
) -> Dict[str, Any]:
    """Create a normalized provider session through the local runtime."""
    payload = build_provider_agent_payload(metadata or {})
    # cerver passes project-scoped env vars (BUFFER_API_KEY, etc.) on
    # metadata.env when provisioning. Forward them to the spawned CLI so
    # the agent inherits them — the same hook secret_fetch reads from on
    # the env backend.
    extra_env = metadata.get("env") if isinstance(metadata, dict) else None
    if extra_env is not None and not isinstance(extra_env, dict):
        extra_env = None
    # When cerver hands us a session_id at provision time, attach it as the
    # agent's callback so _publish_stream_to_cerver can tag stream events
    # with this session — i.e. so subscribers to /v2/sessions/<id>/stream/ws
    # actually receive anything. The cerver_url + cerver_api_token aren't
    # known here but agent_manager falls back to the active connect-transport
    # for those when only cerver_session_id is set.
    cerver_session_id = (
        metadata.get("cerver_session_id") if isinstance(metadata, dict) else None
    )
    callback = {"cerver_session_id": cerver_session_id} if cerver_session_id else None
    # Pool sessions also carry cerver_url + an ephemeral token so the relay can
    # push status/transcript for a session it doesn't own (the gateway sets
    # these). Without them the relay falls back to its connect transport, which
    # has no api_token in --cerver-only mode -> "missing cerver fields", and the
    # session never reports back. Normal sessions omit them and use the fallback.
    if callback and isinstance(metadata, dict):
        if metadata.get("cerver_url"):
            callback["cerver_url"] = metadata["cerver_url"]
        if metadata.get("cerver_api_token"):
            callback["cerver_api_token"] = metadata["cerver_api_token"]
    # `complete_on_exit=True` tells the agent_manager to clean up the
    # agent record as soon as the CLI process ends, instead of parking
    # it in `paused` for a follow-up `--resume`. One-shot callers
    # (cerver run / cerver compare) opt in by setting the flag in
    # session metadata — the gateway threads it through; chat sessions
    # leave it unset so the resume path still works.
    complete_on_exit = bool(metadata.get("complete_on_exit", False)) if isinstance(metadata, dict) else False
    # Pooled (borrowed-machine) session: the gateway sets this so the relay runs
    # clean-room and the harness talks to the gateway proxy. See POOLS.md.
    pool_session = bool(metadata.get("pool_session", False)) if isinstance(metadata, dict) else False
    created = await agent_manager.create(
        task_title=payload["title"],
        task_description=payload["description"],
        working_dir=payload["working_dir"],
        prompt=payload["prompt"],
        skip_branch=payload["workflow"] in ("ask", "plan", "workspace"),
        branch=payload["branch"],
        defer_start=payload["defer_start"],
        cli_tool=payload["cli_tool"],
        cli_model=payload["cli_model"],
        extra_env=extra_env,
        callback=callback,
        complete_on_exit=complete_on_exit,
        pool_session=pool_session,
    )

    agent_id = created.get("id")
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=500, detail="Failed to create provider session")

    return build_provider_session_response(
        agent_id,
        agent,
        {
            **(metadata or {}),
            "engine": engine or "shell",
            "timeout_ms": timeout_ms,
        },
    )


def build_provider_state(
    sandbox_id: str,
    agent: Dict[str, Any],
    agent_total: int,
    workflow_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Return normalized provider state."""
    return {
        "provider": "cerver_local_provider",
        "sandbox_id": sandbox_id,
        "agent": agent,
        "agent_counts": {
            "total": agent_total,
        },
        "workflow_summary": workflow_summary,
    }
