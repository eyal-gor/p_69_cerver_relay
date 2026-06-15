"""
Cerver provider endpoints for the local server.

This exposes a provider-shaped HTTP surface so p69 can appear inside Cerver
as a first-class execution provider, just like a hosted sandbox backend.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...cerver_compute.execution import (
    collect_provider_run,
    delete_provider_session as delete_provider_session_response,
    get_provider_state_response,
    provider_stream_events,
)
from ...cerver_compute.provider import (
    create_provider_session as create_provider_session_response,
    get_provider_info as build_provider_info,
)
from ..agent_manager import agent_manager
from .agents import get_workflow_summary

router = APIRouter()


DEFAULT_TIMEOUT_SECONDS = 300


class CreateProviderSessionRequest(BaseModel):
    """Request body for creating a provider sandbox session.

    Attributes:
        engine: Local execution engine to run the session under. Defaults to
            ``"shell"``.
        timeout_ms: Optional per-session timeout in milliseconds. ``None``
            lets the local agent manager apply its own default.
        metadata: Free-form provider metadata used to derive the local
            workflow and agent payload (see ``infer_provider_workflow``).
    """

    engine: Optional[str] = "shell"
    timeout_ms: Optional[int] = None
    metadata: Dict[str, Any] = {}


class ProviderRunRequest(BaseModel):
    """Request body for executing code in a provider sandbox.

    Attributes:
        code: The command or instruction to execute in the sandbox.
        timeout: Wall-clock timeout in seconds. Defaults to 30; the route
            clamps it to a minimum of 5 seconds.
        envs: Optional environment variables to expose to the execution.
    """

    code: str
    timeout: Optional[int] = 30
    envs: Optional[Dict[str, str]] = None


class ProviderInstallRequest(BaseModel):
    """Request body for installing a package in a provider sandbox.

    Attributes:
        package: Name of the package to install. The route tries npm, pnpm,
            yarn, and pip in turn.
    """

    package: str


class ProviderStateRequest(BaseModel):
    """Request body for writing arbitrary sandbox state.

    Attributes:
        state: Opaque key/value state to persist. The p69 provider does not
            support state writes, so this is only used to shape the rejected
            request.
    """

    state: Dict[str, Any]


def _unsupported(detail: str) -> HTTPException:
    """Build a 501 Not Implemented error for unsupported provider operations.

    Args:
        detail: Human-readable explanation of why the operation is unsupported.

    Returns:
        An ``HTTPException`` with status code 501 and the given detail.
    """
    return HTTPException(status_code=501, detail=detail)


@router.get("/provider")
def get_provider_info():
    """Return provider metadata describing this local computer.

    This is the discovery endpoint Cerver calls to learn the provider's
    identity and capabilities before scheduling sandbox sessions.

    Returns:
        A provider-info dict (id, capabilities, machine state, etc.).
    """
    return build_provider_info()


@router.post("/provider/sandboxes")
async def create_provider_session(request: CreateProviderSessionRequest):
    """Create a new provider sandbox session backed by a local agent.

    Maps the provider metadata onto a local agent/session creation payload
    and starts it under the requested engine.

    Args:
        request: Engine, timeout, and metadata for the new session.

    Returns:
        The created session descriptor (sandbox id and status).
    """
    return await create_provider_session_response(
        agent_manager=agent_manager,
        metadata=request.metadata or {},
        engine=request.engine or "shell",
        timeout_ms=request.timeout_ms,
    )


@router.post("/provider/sandboxes/{sandbox_id}/run")
async def run_provider_session(sandbox_id: str, request: ProviderRunRequest):
    """Execute code in a sandbox and return the full result once complete.

    Blocks until the run finishes (or times out), unlike the streaming
    variant. The timeout is clamped to a minimum of 5 seconds.

    Args:
        sandbox_id: Identifier of the target sandbox session.
        request: Code to run and its timeout.

    Returns:
        The collected run result (output and exit status).
    """
    timeout_seconds = max(5, int(request.timeout or DEFAULT_TIMEOUT_SECONDS))
    return await collect_provider_run(agent_manager, sandbox_id, request.code, timeout_seconds)


@router.post("/provider/sandboxes/{sandbox_id}/run/stream")
async def stream_provider_session(sandbox_id: str, request: ProviderRunRequest, raw_request: Request):
    """Execute code in a sandbox and stream events as Server-Sent Events.

    Streams incremental run events to the caller and stops early if the
    client disconnects (observed via ``raw_request``).

    Args:
        sandbox_id: Identifier of the target sandbox session.
        request: Code to run.
        raw_request: Underlying request, used to detect client disconnects.

    Returns:
        A ``StreamingResponse`` emitting ``text/event-stream`` events.
    """
    return StreamingResponse(
        provider_stream_events(agent_manager, raw_request, sandbox_id, request.code),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.post("/provider/sandboxes/{sandbox_id}/install")
async def install_in_provider_session(sandbox_id: str, request: ProviderInstallRequest):
    """Install a package in the sandbox via the available package manager.

    Issues a single shell command that tries npm, pnpm, yarn, and pip in
    turn, returning the first that succeeds.

    Args:
        sandbox_id: Identifier of the target sandbox session.
        request: The package to install.

    Returns:
        The collected run result of the install command.
    """
    return await collect_provider_run(
        agent_manager,
        sandbox_id,
        f"Run this exact shell command and return the result only: npm install {request.package} || pnpm add {request.package} || yarn add {request.package} || pip install {request.package}",
        DEFAULT_TIMEOUT_SECONDS,
    )


@router.get("/provider/sandboxes/{sandbox_id}/state")
def get_provider_state(sandbox_id: str):
    """Return the current state of a sandbox session.

    Combines the agent manager's view of the sandbox with the latest local
    workflow summary so Cerver can render session status.

    Args:
        sandbox_id: Identifier of the target sandbox session.

    Returns:
        A state dict describing the session and its workflow summary.
    """
    return get_provider_state_response(
        agent_manager=agent_manager,
        sandbox_id=sandbox_id,
        workflow_summary=get_workflow_summary(),
    )


@router.put("/provider/sandboxes/{sandbox_id}/state")
def set_provider_state(sandbox_id: str, request: ProviderStateRequest):
    """Reject arbitrary sandbox state writes (unsupported by p69).

    Args:
        sandbox_id: Identifier of the target sandbox session.
        request: The state the caller attempted to write.

    Raises:
        HTTPException: Always raised with status 501; p69 does not support
            arbitrary state writes.
    """
    raise _unsupported("p69 provider does not support arbitrary state writes")


@router.get("/provider/sandboxes/{sandbox_id}/files")
def read_provider_file(sandbox_id: str, path: Optional[str] = None, encoding: str = "utf-8"):
    """Reject direct sandbox file reads (unsupported by p69).

    Args:
        sandbox_id: Identifier of the target sandbox session.
        path: Requested file path (ignored).
        encoding: Requested text encoding (ignored).

    Raises:
        HTTPException: Always raised with status 501; p69 does not expose
            direct file reads through the provider interface.
    """
    raise _unsupported("p69 provider does not support direct file reads through the provider interface")


@router.put("/provider/sandboxes/{sandbox_id}/files")
def write_provider_file(sandbox_id: str):
    """Reject direct sandbox file writes (unsupported by p69).

    Args:
        sandbox_id: Identifier of the target sandbox session.

    Raises:
        HTTPException: Always raised with status 501; p69 does not expose
            direct file writes through the provider interface.
    """
    raise _unsupported("p69 provider does not support direct file writes through the provider interface")


@router.delete("/provider/sandboxes/{sandbox_id}")
def delete_provider_session(sandbox_id: str, cleanup_worktree: bool = False):
    """Tear down a sandbox session and release its resources.

    Args:
        sandbox_id: Identifier of the sandbox session to delete.
        cleanup_worktree: When ``True``, also remove the session's git
            worktree from disk.

    Returns:
        The deletion result for the session.
    """
    return delete_provider_session_response(
        agent_manager,
        sandbox_id,
        cleanup_worktree=cleanup_worktree,
    )
