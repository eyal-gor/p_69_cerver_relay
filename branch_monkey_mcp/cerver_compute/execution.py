"""
Cerver-facing execution helpers for the p69 local computer runtime.

These helpers keep the HTTP route thin while the underlying local execution
still flows through the legacy agent manager.
"""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict

from fastapi import HTTPException, Request

from ..computer_runtime.execution import extract_result_from_output_buffer
from .provider import build_provider_state


def _extract_normalized_text(normalized: Any) -> str:
    """Pull the human-readable text out of a normalized CLI stream event.

    Handles the event shapes the local providers emit: ``assistant`` messages
    (concatenating their ``text`` content blocks), ``tool_result`` and
    ``result`` events (their respective payload fields), and a generic
    fallback to a top-level ``text`` field.

    Args:
        normalized: A parsed stream event. Anything that is not a dict (or that
            lacks a recognized shape) yields an empty string.

    Returns:
        The extracted text, or an empty string when nothing matches.
    """
    if not isinstance(normalized, dict):
        return ""

    if normalized.get("type") == "assistant":
        content = normalized.get("message", {}).get("content", [])
        text_blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(text_blocks)

    if normalized.get("type") == "tool_result":
        return normalized.get("content", "") or ""

    if normalized.get("type") == "result":
        return normalized.get("result", "") or ""

    text = normalized.get("text")
    return text if isinstance(text, str) else ""


def extract_stream_text(event: Dict[str, Any]) -> str:
    """Extract displayable text from a raw agent-manager stream event.

    Only ``output`` events carry text; everything else yields an empty string.
    The event's ``data`` field is expected to be a JSON-encoded normalized
    event, which is parsed and routed through :func:`_extract_normalized_text`.
    If parsing fails, the event's ``raw`` field (or the raw ``data`` string) is
    returned unchanged.

    Args:
        event: A stream event from the agent manager's listener queue.

    Returns:
        The displayable text for the event, or an empty string.
    """
    if event.get("type") != "output":
        return ""

    data = event.get("data")
    if not isinstance(data, str):
        return ""

    try:
        normalized = json.loads(data)
        return _extract_normalized_text(normalized)
    except Exception:
        return event.get("raw") or data


async def send_provider_input(agent_manager: Any, agent_id: str, message: str) -> Dict[str, Any]:
    """Route an input message to an agent based on its current lifecycle state.

    Dispatches by the agent's status:

    - ``prepared``: spawns the CLI process with ``message`` as the first prompt
      (``action="started"``).
    - ``paused``/``completed``/``failed`` with a saved ``session_id``: resumes
      the existing session (``action="resumed"``).
    - ``running``: rejected, since the agent is busy with a prior message.

    Args:
        agent_manager: The legacy agent manager owning the agent's lifecycle.
        agent_id: Identifier of the target agent.
        message: The user input/prompt to deliver.

    Returns:
        A result dict describing the action taken (``started`` or ``resumed``).

    Raises:
        HTTPException: 404 if the agent does not exist; 400 if the agent is
            running or has no resumable session.
    """
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent["status"] == "prepared":
        await agent_manager.spawn_cli_process(agent_id, message)
        refreshed = agent_manager.get(agent_id) or {}
        return {
            "success": True,
            "action": "started",
            "cli_tool": refreshed.get("cli_tool"),
            "images": 0,
        }

    if agent["status"] in ("paused", "completed", "failed") and agent.get("session_id"):
        await agent_manager.resume_session(agent_id, message)
        return {
            "success": True,
            "action": "resumed",
            "images": 0,
        }

    if agent["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is running. Wait for it to complete before sending another message.",
        )

    raise HTTPException(status_code=400, detail="No active session. Start a new task.")


async def collect_provider_run(
    agent_manager: Any,
    agent_id: str,
    message: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Run a single provider turn to completion and return an aggregated result.

    Registers a listener, sends ``message`` via :func:`send_provider_input`,
    then drains the event queue until the run ends (``exit``/``paused``) or an
    ``error`` event arrives, accumulating stdout text and stderr. Each ``await``
    on the queue is bounded by ``timeout_seconds``. The final stdout prefers the
    result extracted from the agent's output buffer, falling back to the
    concatenated stream text. The listener is always removed on exit.

    Args:
        agent_manager: The legacy agent manager owning the agent's lifecycle.
        agent_id: Identifier of the target agent.
        message: The user input/prompt to deliver.
        timeout_seconds: Maximum time to wait for each successive event.

    Returns:
        A shell-style result dict (``stdout``, ``stderr``, ``exit_code``,
        ``command_id``, ``session_id``, resumability, etc.).

    Raises:
        HTTPException: 408 if no event arrives within ``timeout_seconds``, plus
            any error propagated from :func:`send_provider_input`.
    """
    queue = agent_manager.add_listener(agent_id)

    try:
        input_result = await send_provider_input(agent_manager, agent_id, message)
        stdout_parts = []
        stderr_parts = []
        exit_code = 0
        final_status = "running"

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise HTTPException(
                    status_code=408,
                    detail=f"Timed out waiting for provider output after {timeout_seconds}s",
                ) from exc

            if event.get("type") == "error":
                stderr_parts.append(event.get("error") or "Unknown local provider error")
                final_status = "failed"
                exit_code = 1
                break

            text = extract_stream_text(event)
            if text:
                stdout_parts.append(text)

            if event.get("type") == "paused":
                final_status = "paused"
                exit_code = event.get("exit_code") if isinstance(event.get("exit_code"), int) else 0
                break

            if event.get("type") == "exit":
                final_status = "completed"
                exit_code = event.get("exit_code") if isinstance(event.get("exit_code"), int) else 0
                break

        agent = agent_manager.get(agent_id) or {}
        raw_agent = getattr(agent_manager, "_agents", {}).get(agent_id)
        extracted_result = (
            extract_result_from_output_buffer(getattr(raw_agent, "output_buffer", []))
            if raw_agent
            else ""
        )
        return {
            "success": True,
            "action": input_result.get("action"),
            "command_id": agent.get("session_id") or agent_id,
            "execution_runtime": "shell",
            "exit_code": agent.get("exit_code") if isinstance(agent.get("exit_code"), int) else exit_code,
            "stdout": (extracted_result or "".join(stdout_parts)).strip(),
            "stderr": "".join(stderr_parts).strip(),
            "cwd": agent.get("work_dir"),
            "started_at": agent.get("created_at"),
            "provider_session_status": agent.get("status") or final_status,
            "can_resume": bool(agent.get("session_id")),
            "session_id": agent.get("session_id"),
        }
    finally:
        agent_manager.remove_listener(agent_id, queue)


async def provider_stream_events(
    agent_manager: Any,
    request: Request,
    agent_id: str,
    message: str,
) -> AsyncGenerator[str, None]:
    """Stream a provider turn to the client as Server-Sent Events.

    Emits an initial ``connected`` event, sends ``message`` via
    :func:`send_provider_input`, then relays each agent-manager event as an
    ``data:`` SSE frame until the run finishes (``exit``/``paused``) or the
    client disconnects. A ``: heartbeat`` comment is sent whenever 15 seconds
    pass with no event, keeping the connection alive. The listener is always
    removed on exit.

    Args:
        agent_manager: The legacy agent manager owning the agent's lifecycle.
        request: The incoming request, polled to detect client disconnects.
        agent_id: Identifier of the target agent.
        message: The user input/prompt to deliver.

    Yields:
        SSE-formatted strings (``data:`` frames and heartbeat comments).

    Raises:
        HTTPException: 404 if the agent does not exist.
    """
    queue = agent_manager.add_listener(agent_id)

    try:
        agent = agent_manager.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        init_event = {"type": "connected", "agentId": agent_id, "status": agent["status"]}
        yield f"data: {json.dumps(init_event)}\n\n"

        await send_provider_input(agent_manager, agent_id, message)

        while True:
            if await request.is_disconnected():
                break

            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("exit", "paused"):
                    break
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        agent_manager.remove_listener(agent_id, queue)


def get_provider_state_response(
    agent_manager: Any,
    sandbox_id: str,
    workflow_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the provider state response for a sandbox (agent).

    Looks up the agent by ``sandbox_id`` and delegates to
    :func:`~.provider.build_provider_state`, passing along the total agent
    count and the supplied workflow summary.

    Args:
        agent_manager: The legacy agent manager owning the agent's lifecycle.
        sandbox_id: Identifier of the sandbox/agent to report on.
        workflow_summary: Workflow metadata to fold into the state response.

    Returns:
        The provider state dict for the sandbox.

    Raises:
        HTTPException: 404 if the agent does not exist.
    """
    agent = agent_manager.get(sandbox_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return build_provider_state(
        sandbox_id=sandbox_id,
        agent=agent,
        agent_total=len(agent_manager.list()),
        workflow_summary=workflow_summary,
    )


def delete_provider_session(
    agent_manager: Any,
    sandbox_id: str,
    cleanup_worktree: bool = False,
) -> Dict[str, Any]:
    """Terminate a provider session (sandbox) and report the result.

    Kills the agent via the agent manager, optionally cleaning up its git
    worktree, and returns a terminated-status payload in the shape Cerver
    expects from the local provider.

    Args:
        agent_manager: The legacy agent manager owning the agent's lifecycle.
        sandbox_id: Identifier of the sandbox/agent to terminate.
        cleanup_worktree: When True, also remove the agent's git worktree.

    Returns:
        A dict confirming termination, echoing the sandbox id and provider.
    """
    agent_manager.kill(sandbox_id, cleanup_worktree=cleanup_worktree)
    return {
        "success": True,
        "sandbox_id": sandbox_id,
        "remote_sandbox_id": sandbox_id,
        "provider": "cerver_local_provider",
        "status": "terminated",
    }
