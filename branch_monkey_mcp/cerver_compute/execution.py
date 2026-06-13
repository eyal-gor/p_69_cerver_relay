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
    """Pull the human-readable text out of one normalized stream event.

    Handles the event shapes the CLI providers emit after normalization:
    ``assistant`` (concatenates the ``text`` blocks of the message content),
    ``tool_result`` (the ``content`` field), and ``result`` (the ``result``
    field). Anything else falls back to a top-level ``text`` key.

    Args:
        normalized: A decoded stream event. Non-dict values yield "".

    Returns:
        The extracted text, or "" when the event carries none.
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
    """Extract assistant-visible text from a raw provider stream event.

    Only ``output`` events carry text. Their ``data`` field is a JSON string
    that is decoded and run through :func:`_extract_normalized_text`. If the
    payload is not valid JSON, the event's ``raw`` field (or the data string
    itself) is returned unchanged so nothing is silently dropped.

    Args:
        event: A single stream event from the agent manager listener queue.

    Returns:
        The text to surface to the caller, or "" for non-output events.
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
    """Route a message to an agent based on its current lifecycle status.

    Acts as the entry point for both starting and continuing a local provider
    session, dispatching on the agent's status:

    - ``prepared``  → spawns the CLI process with ``message`` ("started").
    - ``paused`` / ``completed`` / ``failed`` with a ``session_id`` → resumes
      the existing session ("resumed").
    - ``running``   → rejected with HTTP 400; the caller must wait.
    - anything else → rejected with HTTP 400 (no active session).

    Args:
        agent_manager: The agent manager owning the session registry.
        agent_id: Id of the target agent/sandbox.
        message: The prompt or follow-up text to deliver.

    Returns:
        A dict with ``success``, the ``action`` taken ("started"/"resumed"),
        and an ``images`` count.

    Raises:
        HTTPException: 404 if the agent is unknown; 400 if it is running or
            has no resumable session.
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
    """Run an agent to completion and collect its output as a single result.

    The blocking (non-streaming) counterpart to :func:`provider_stream_events`.
    Subscribes a listener, sends the input, then drains stream events until the
    session exits, pauses, or errors — accumulating assistant text along the
    way. On a clean finish it prefers the structured result extracted from the
    agent's output buffer, falling back to the concatenated stream text.

    Args:
        agent_manager: The agent manager owning the session registry.
        agent_id: Id of the target agent/sandbox.
        message: The prompt or follow-up text to deliver.
        timeout_seconds: Max seconds to wait for each successive stream event.

    Returns:
        A result dict describing the run: ``stdout``/``stderr``, ``exit_code``,
        ``command_id``/``session_id``, working directory, status, and whether
        the session ``can_resume``.

    Raises:
        HTTPException: 404/400 from :func:`send_provider_input`, or 408 if no
            event arrives within ``timeout_seconds``.
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
    """Stream an agent run to the client as Server-Sent Events.

    The streaming counterpart to :func:`collect_provider_run`. Emits a
    ``connected`` event, sends the input, then forwards each stream event as an
    ``data: {json}`` SSE frame until an ``exit`` or ``paused`` event arrives or
    the client disconnects. A ``: heartbeat`` comment is sent whenever 15s pass
    with no event so idle connections stay open.

    Args:
        agent_manager: The agent manager owning the session registry.
        request: The FastAPI request, polled for client disconnects.
        agent_id: Id of the target agent/sandbox.
        message: The prompt or follow-up text to deliver.

    Yields:
        SSE-formatted strings (``data:`` frames and heartbeat comments).

    Raises:
        HTTPException: 404 if the agent is unknown.
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
    """Build the Cerver-facing state snapshot for one sandbox.

    Looks up the agent and delegates to
    :func:`~branch_monkey_mcp.cerver_compute.provider.build_provider_state` to
    shape the response, passing the total agent count and the supplied
    workflow summary.

    Args:
        agent_manager: The agent manager owning the session registry.
        sandbox_id: Id of the sandbox/agent to describe.
        workflow_summary: Pre-computed workflow status to embed in the state.

    Returns:
        The provider-state dict expected by the Cerver gateway.

    Raises:
        HTTPException: 404 if the sandbox is unknown.
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
    """Terminate a local provider session and report it as deleted.

    Kills the underlying agent process, optionally removing its git worktree,
    and returns the terminated-session acknowledgement the Cerver gateway
    expects.

    Args:
        agent_manager: The agent manager owning the session registry.
        sandbox_id: Id of the sandbox/agent to terminate.
        cleanup_worktree: When True, also delete the agent's git worktree.

    Returns:
        A dict acknowledging termination, including ``sandbox_id``,
        ``remote_sandbox_id``, ``provider``, and ``status``.
    """
    agent_manager.kill(sandbox_id, cleanup_worktree=cleanup_worktree)
    return {
        "success": True,
        "sandbox_id": sandbox_id,
        "remote_sandbox_id": sandbox_id,
        "provider": "cerver_local_provider",
        "status": "terminated",
    }
