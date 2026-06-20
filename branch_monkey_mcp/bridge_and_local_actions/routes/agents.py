"""
Agent management endpoints for the local server.
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import get_default_working_dir
from ..agent_manager import agent_manager

router = APIRouter()


_WORKFLOW_RETENTION = timedelta(hours=6)
_workflow_lock = threading.Lock()
_workflow_runs: dict[str, dict] = {}


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Used as the single clock source for workflow-run bookkeeping so that
    every ``started_at``/``finished_at`` stamp is comparable and serializes
    with an explicit UTC offset.
    """
    return datetime.now(timezone.utc)


def _cleanup_workflows() -> None:
    """Drop workflow runs that finished longer than the retention window ago.

    Iterates the in-memory ``_workflow_runs`` registry and evicts any run
    whose ``finished_at`` is older than ``_WORKFLOW_RETENTION`` (6 hours).
    Still-running entries (``finished_at`` is ``None``) are always kept.

    Callers must already hold ``_workflow_lock``; this function does not
    acquire it itself.
    """
    cutoff = _utc_now() - _WORKFLOW_RETENTION
    stale_ids = []
    for workflow_id, run in _workflow_runs.items():
        finished_at = run.get("finished_at")
        if finished_at and finished_at < cutoff:
            stale_ids.append(workflow_id)

    for workflow_id in stale_ids:
        _workflow_runs.pop(workflow_id, None)


def _start_workflow_run(name: str, working_dir: str) -> str:
    """Register a new workflow run and return its short id.

    Allocates an 8-character hex id, opportunistically prunes stale runs,
    and records a fresh ``running`` entry in ``_workflow_runs`` stamped with
    the current UTC start time. Acquires ``_workflow_lock`` internally.

    Args:
        name: Human-readable workflow name (e.g. the workflow file or label).
        working_dir: Directory the workflow run executes against.

    Returns:
        The generated workflow id, used later to look the run up in
        :func:`_finish_workflow_run`.
    """
    workflow_id = uuid.uuid4().hex[:8]
    with _workflow_lock:
        _cleanup_workflows()
        _workflow_runs[workflow_id] = {
            "id": workflow_id,
            "name": name,
            "working_dir": working_dir,
            "status": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "resume_from": None,
            "error": None,
        }
    return workflow_id


def _finish_workflow_run(workflow_id: str, workflow_result: dict) -> None:
    """Record the terminal outcome of a previously started workflow run.

    Looks the run up by id and stamps it with the final status, finish time,
    and any resume/error metadata pulled from ``workflow_result``. A
    ``needs_approval`` result is mapped to the ``paused`` status so the run
    surfaces as awaiting approval rather than finished. Unknown ids are
    ignored (e.g. if the run was already evicted by retention cleanup).
    Acquires ``_workflow_lock`` internally.

    Args:
        workflow_id: Id returned by :func:`_start_workflow_run`.
        workflow_result: Result mapping from the workflow CLI, optionally
            carrying ``status``, ``resume_from``, and ``error`` keys.
    """
    with _workflow_lock:
        run = _workflow_runs.get(workflow_id)
        if not run:
            return

        status = workflow_result.get("status", "error")
        run["status"] = "paused" if status == "needs_approval" else status
        run["finished_at"] = _utc_now()
        run["resume_from"] = workflow_result.get("resume_from")
        run["error"] = workflow_result.get("error")


def get_workflow_summary() -> dict:
    """Summarize tracked workflow runs for the machine-stats endpoint.

    Prunes stale runs, then returns aggregate status ``counts`` plus the 10
    most recently started runs (newest first) serialized into JSON-friendly
    dicts. Any run with an unrecognized status is folded into the ``error``
    bucket. Acquires ``_workflow_lock`` internally.

    Returns:
        A dict with two keys: ``counts`` (a status -> count mapping over
        running/paused/completed/failed/error) and ``runs`` (a list of
        serialized recent runs with ISO-8601 timestamps).
    """
    with _workflow_lock:
        _cleanup_workflows()
        counts = {
            "running": 0,
            "paused": 0,
            "completed": 0,
            "failed": 0,
            "error": 0,
        }

        for run in _workflow_runs.values():
            status = run.get("status", "error")
            if status in counts:
                counts[status] += 1
            else:
                counts["error"] += 1

        recent = sorted(
            _workflow_runs.values(),
            key=lambda run: run.get("started_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:10]

        def _serialize(run: dict) -> dict:
            """Convert an in-memory workflow run into a JSON-safe dict.

            Renders the ``started_at``/``finished_at`` datetimes as ISO-8601
            strings (or ``None``) and passes the remaining fields through.
            """
            return {
                "id": run["id"],
                "name": run["name"],
                "working_dir": run["working_dir"],
                "status": run["status"],
                "started_at": run["started_at"].isoformat() if run.get("started_at") else None,
                "finished_at": run["finished_at"].isoformat() if run.get("finished_at") else None,
                "resume_from": run.get("resume_from"),
                "error": run.get("error"),
            }

        return {
            "counts": counts,
            "runs": [_serialize(run) for run in recent],
        }


class CronCallback(BaseModel):
    """Callback info for notifying cerver when an agent finishes.

    Originally cron-only; now used by all agent-creating endpoints
    (/agents, /run-agent) so any agent can stream its transcript into a
    cerver shadow session. cerver_* fields wire the agent to its shadow:
    each event gets pushed to /v2/sessions/{id}/transcript via
    _push_event_to_cerver. Without them, no cerver writes happen.

    The non-cerver fields (url, cron_id, etc.) are vestigial for non-cron
    callers and can be omitted.
    """
    url: str = ""
    secret: str = ""
    cron_id: str = ""
    cron_name: str = ""
    agent_name: str = ""
    project_id: str = ""
    user_id: str = ""
    session_id: Optional[str] = None
    cerver_url: Optional[str] = None
    cerver_api_token: Optional[str] = None
    cerver_session_id: Optional[str] = None


class CreateAgentRequest(BaseModel):
    task_id: Optional[str] = None
    task_number: Optional[int] = None
    title: str = "Local Task"
    description: Optional[str] = None
    working_dir: Optional[str] = None
    prompt: Optional[str] = None
    workflow: str = "execute"
    skip_branch: bool = False  # Legacy: prefer workflow field
    branch: Optional[str] = None
    defer_start: bool = False
    cli_tool: Optional[str] = None  # 'claude' or 'codex'; defaults to 'claude'
    # Optional cerver wiring — when present, every stream event from this
    # agent is pushed to /v2/sessions/{cerver_session_id}/transcript via
    # _push_event_to_cerver. Lets task / chat sessions persist transcripts
    # without depending on a connected frontend, same as cron via /run-agent.
    callback: Optional[CronCallback] = None
    # Project-scoped env vars (e.g. BUFFER_API_KEY) that the spawned CLI
    # process inherits. Lets kompany pass project secrets through to the
    # agent without baking them into the relay host's shell env.
    extra_env: Optional[Dict[str, str]] = None


class RunAgentRequest(BaseModel):
    """Request to run an agent with its system prompt (e.g. from a cron)."""
    agent_name: str = "Agent"
    system_prompt: str
    instructions: str
    working_dir: Optional[str] = None
    callback: Optional[CronCallback] = None
    cli_tool: Optional[str] = None  # 'claude' or 'codex'; defaults to 'claude'
    extra_env: Optional[Dict[str, str]] = None


class TaskExecuteRequest(BaseModel):
    """Request from relay to execute a task in a specific local_path."""
    task_id: str
    task_number: int
    title: str
    description: Optional[str] = None
    local_path: Optional[str] = None
    repository_url: Optional[str] = None
    cli_tool: Optional[str] = None  # 'claude' or 'codex'; defaults to 'claude'


class ImageData(BaseModel):
    data: str  # base64 data URL (data:image/png;base64,...)
    name: str = "image"
    type: str = "image/png"


class InputRequest(BaseModel):
    input: str
    images: Optional[List[ImageData]] = None
    cli_tool: Optional[str] = None  # Override CLI provider before first message (prepared sessions only)
    # When the cerver gateway forwards a /v2/sessions/:id/input call to
    # the relay, it has ALREADY written the user message to the
    # session's transcript (step 1 of recordInput). Setting this flag
    # tells the relay to skip its own _push_user_message — otherwise
    # the user message lands twice in the transcript ~700ms apart.
    pre_logged: bool = False
    # Recovery hints (also forwarded by cerver gateway). When the local
    # agent_id isn't in the relay's in-memory _agents dict — relay
    # restart, stale cleanup, machine migration — these let the relay
    # rebuild a paused agent record and resume the conversation via the
    # CLI's own --resume / `exec resume` flag (provider-agnostic).
    cerver_session_id: Optional[str] = None
    cli_session_id: Optional[str] = None
    working_dir: Optional[str] = None


def save_images_to_temp(images: List[ImageData]) -> List[str]:
    """Save base64 images to temporary files and return file paths."""
    temp_paths = []
    for i, img in enumerate(images):
        try:
            # Parse data URL: data:image/png;base64,xxxxx
            if img.data.startswith('data:'):
                # Extract the base64 part after the comma
                header, b64_data = img.data.split(',', 1)
                # Get extension from content type
                content_type = header.split(';')[0].split(':')[1]
                ext = content_type.split('/')[-1]
                if ext == 'jpeg':
                    ext = 'jpg'
            else:
                # Assume raw base64
                b64_data = img.data
                ext = 'png'

            # Decode and save to temp file
            image_bytes = base64.b64decode(b64_data)
            fd, temp_path = tempfile.mkstemp(suffix=f'.{ext}', prefix='claude_img_')
            os.write(fd, image_bytes)
            os.close(fd)
            temp_paths.append(temp_path)
            print(f"[LocalServer] Saved image {i+1} to {temp_path}")
        except Exception as e:
            print(f"[LocalServer] Failed to save image {i+1}: {e}")
    return temp_paths


@router.post("/task-execute")
async def execute_task(request: TaskExecuteRequest):
    """
    Execute a task dispatched from the cloud.
    This endpoint is called by the relay when a user triggers a task run.
    The task is executed in the specified local_path.
    """
    # Use local_path if provided, otherwise fall back to default working dir
    print(f"[TaskExecute] Received local_path from request: {request.local_path}")
    working_dir = request.local_path or get_default_working_dir()
    print(f"[TaskExecute] Resolved working_dir: {working_dir}")

    # Verify the directory exists
    if working_dir and not os.path.isdir(working_dir):
        raise HTTPException(
            status_code=400,
            detail=f"Working directory does not exist: {working_dir}"
        )

    print(f"[TaskExecute] Starting task #{request.task_number}: {request.title}")
    print(f"[TaskExecute] Working directory: {working_dir}")

    # Build the prompt with task info
    prompt = f"Work on task #{request.task_number}: {request.title}"
    if request.description:
        prompt += f"\n\n{request.description}"

    # Create and start the agent
    result = await agent_manager.create(
        task_id=request.task_id,
        task_number=request.task_number,
        task_title=request.title,
        task_description=request.description,
        working_dir=working_dir,
        prompt=prompt,
        cli_tool=request.cli_tool
    )

    return {
        "success": True,
        "agent_id": result.get("id"),
        "task_number": request.task_number,
        "working_dir": working_dir,
        "branch": result.get("branch"),
        "worktree_path": result.get("worktree_path"),
        "is_worktree": result.get("is_worktree", False),
        "message": f"Task #{request.task_number} started in {working_dir}"
    }


@router.post("/agents")
async def create_agent(request: CreateAgentRequest):
    """Create and start a new local Claude Code agent."""
    # Derive skip_branch from workflow — non-code workflows skip git worktree
    skip_branch = request.skip_branch or request.workflow in ("ask", "plan", "workspace")

    callback_dict = request.callback.model_dump() if request.callback else None

    return await agent_manager.create(
        task_id=request.task_id,
        task_number=request.task_number,
        task_title=request.title,
        task_description=request.description,
        working_dir=request.working_dir,
        prompt=request.prompt,
        skip_branch=skip_branch,
        branch=request.branch,
        defer_start=request.defer_start,
        cli_tool=request.cli_tool,
        callback=callback_dict,
        extra_env=request.extra_env,
    )


@router.post("/run-agent")
async def run_agent(request: RunAgentRequest):
    """Run an agent with a system prompt and instructions.

    Used by cron jobs and other automated triggers.
    The agent's system_prompt is passed via --append-system-prompt,
    and the instructions are the user message via -p.
    """
    working_dir = request.working_dir or get_default_working_dir()

    if working_dir and not os.path.isdir(working_dir):
        raise HTTPException(
            status_code=400,
            detail=f"Working directory does not exist: {working_dir}"
        )

    print(f"[RunAgent] Starting agent: {request.agent_name}")
    print(f"[RunAgent] Working directory: {working_dir}")

    callback_dict = None
    if request.callback:
        callback_dict = request.callback.model_dump()

    result = await agent_manager.create(
        task_title=request.agent_name,
        working_dir=working_dir,
        prompt=request.instructions,
        system_prompt=request.system_prompt,
        skip_branch=True,
        callback=callback_dict,
        cli_tool=request.cli_tool,
        extra_env=request.extra_env,
        # /run-agent is the one-shot/cron entry point — agent should
        # complete and clean up after the prompt finishes, not wait for
        # interactive follow-ups.
        complete_on_exit=True,
    )

    return {
        "success": True,
        "agent_id": result.get("id"),
        "agent_name": request.agent_name,
        "status": result.get("status"),
        "work_dir": result.get("work_dir")
    }


class RunWorkflowRequest(BaseModel):
    """Request to run a workflow."""
    workflow_yaml: Optional[str] = None  # the workflow YAML content (from machine.command)
    working_dir: Optional[str] = None
    from_step: Optional[str] = None  # resume from this step
    step: Optional[str] = None  # run only this step
    callback: Optional[CronCallback] = None
    # For machines without a workflow — auto-generate a default
    machine_id: Optional[str] = None
    system_prompt: Optional[str] = None
    instructions: Optional[str] = None
    agent_name: Optional[str] = None
    session_id: Optional[str] = None  # agent_session to write messages to


def _build_default_yaml(machine_id: Optional[str], instructions: str, agent_name: str) -> str:
    """Build a default workflow YAML for machines without one."""
    steps = []
    if machine_id:
        steps.append(f'  - name: load-context\n    description: Load machine context (agent, memory, metrics, tasks)\n    run: "kompany-workflow load-context {machine_id}"')
        escaped = instructions.replace('"', '\\"')
        steps.append(f'  - name: run\n    description: "{agent_name}"\n    run: \'kompany-workflow llm -s "$STEP_LOAD_CONTEXT_STDOUT" -p "{escaped}"\'\n    timeout: 300')
    else:
        escaped = instructions.replace('"', '\\"')
        steps.append(f'  - name: run\n    description: "{agent_name}"\n    run: \'kompany-workflow llm -p "{escaped}"\'\n    timeout: 300')

    return f"name: {agent_name}\ndescription: Auto-generated workflow\n\nsteps:\n" + "\n\n".join(steps) + "\n"


@router.post("/run-workflow")
async def run_workflow(request: RunWorkflowRequest):
    """Run a workflow. The YAML comes from the request body (stored in machine.command).
    If no YAML provided, auto-generates a default LLM workflow.
    """
    import subprocess as sp
    import tempfile

    working_dir = request.working_dir or get_default_working_dir()

    if working_dir and not os.path.isdir(working_dir):
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {working_dir}")

    # Get the workflow YAML
    yaml_content = request.workflow_yaml
    workflow_name = request.agent_name or "workflow"
    if not yaml_content:
        yaml_content = _build_default_yaml(
            request.machine_id,
            request.instructions or "Run your default behavior.",
            request.agent_name or "default",
        )
        print(f"[RunWorkflow] Auto-generated default workflow")

    # Write YAML to temp file
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', prefix='wf-', delete=False)
    tmp.write(yaml_content)
    tmp.close()
    workflow_file = tmp.name

    # Build command
    cmd = ["kompany-workflow", "run", "-f", workflow_file]
    if request.from_step:
        cmd.extend(["--from", request.from_step])
    if request.step:
        cmd.extend(["--step", request.step])

    print(f"[RunWorkflow] Running: {' '.join(cmd)}")
    print(f"[RunWorkflow] Working dir: {working_dir}")
    workflow_id = _start_workflow_run(workflow_name, working_dir)

    try:
        result = sp.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
            cwd=working_dir,
        )

        print(f"[RunWorkflow] Exit code: {result.returncode}")

        # Parse JSON output
        try:
            workflow_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            workflow_result = {
                "status": "error",
                "error": "Failed to parse workflow output",
                "stdout": result.stdout[-2000:] if result.stdout else "",
                "stderr": result.stderr[-2000:] if result.stderr else "",
            }

        _finish_workflow_run(workflow_id, workflow_result)

        # Save run history to cloud API
        callback_dict = request.callback.model_dump() if request.callback else {}
        try:
            import httpx
            callback_url = callback_dict.get("url", "")
            api_base = callback_url.rsplit("/api/", 1)[0] if callback_url else "https://kompany.dev"

            run_payload = {
                "machine_id": request.machine_id or None,
                "project_id": callback_dict.get("project_id"),
                "user_id": callback_dict.get("user_id"),
                "status": workflow_result.get("status", "error"),
                "duration_ms": workflow_result.get("duration_ms", 0),
                "triggered_by": "cron" if request.callback else "manual",
                "cron_id": callback_dict.get("cron_id"),
                "error": workflow_result.get("error"),
                "resume_from": workflow_result.get("resume_from"),
                "steps": workflow_result.get("steps", []),
            }

            async with httpx.AsyncClient() as client:
                # Save run history
                await client.post(f"{api_base}/api/workflow-runs", json=run_payload, timeout=10)
                print(f"[RunWorkflow] Saved run history")

                # Handle cron callback
                if callback_url:
                    # Collect all step outputs for full workflow transcript
                    steps = workflow_result.get("steps", [])
                    full_output = ""
                    for step in steps:
                        step_name = step.get("name", "step")
                        step_stdout = step.get("stdout", "")
                        if step_stdout:
                            full_output += f"## {step_name}\n{step_stdout}\n\n"
                    if not full_output:
                        full_output = workflow_result.get("steps", [{}])[-1].get("stdout", "")[:4000] if steps else ""

                    await client.post(callback_url, json={
                        "cron_id": callback_dict.get("cron_id"),
                        "cron_name": callback_dict.get("cron_name"),
                        "agent_name": callback_dict.get("agent_name"),
                        "project_id": callback_dict.get("project_id"),
                        "user_id": callback_dict.get("user_id"),
                        "session_id": callback_dict.get("session_id") or (request.session_id if request.session_id else None),
                        "status": "completed" if workflow_result.get("status") == "completed" else "failed",
                        "output": full_output[:8000],
                    }, headers={"x-cron-secret": callback_dict.get("secret", "")}, timeout=10)
        except Exception as e:
            print(f"[RunWorkflow] Post-run error: {e}")

        # Clean up temp file
        if os.path.exists(workflow_file):
            os.unlink(workflow_file)

        return {
            "success": result.returncode == 0,
            "workflow": workflow_result,
        }

    except sp.TimeoutExpired:
        _finish_workflow_run(workflow_id, {"status": "error", "error": "Workflow timed out after 600s"})
        if os.path.exists(workflow_file):
            os.unlink(workflow_file)
        return {"success": False, "workflow": {"status": "error", "error": "Workflow timed out after 600s"}}
    except Exception as e:
        _finish_workflow_run(workflow_id, {"status": "error", "error": str(e)})
        if os.path.exists(workflow_file):
            os.unlink(workflow_file)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents")
def list_agents():
    """List all local agents."""
    return agent_manager.list()


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    """Get agent info by ID."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/agents/{agent_id}/input")
async def send_input(agent_id: str, request: InputRequest):
    """Send input to agent (resumes session if paused).

    If the agent isn't in the relay's in-memory map AND the caller
    supplied recovery hints (cerver does this on every /input forward),
    rebuild a paused record and resume via the CLI's own --resume flag.
    Lets conversations survive relay restarts and stale-agent reaping
    without requiring the user to start over.
    """
    agent = agent_manager.get(agent_id)
    if not agent and (request.cli_session_id or request.cerver_session_id):
        recovered = agent_manager.recover_agent(
            agent_id,
            cli_session_id=request.cli_session_id,
            cli_tool=request.cli_tool,
            working_dir=request.working_dir,
            cerver_session_id=request.cerver_session_id,
        )
        if recovered:
            agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    message = request.input.rstrip('\n')

    # Handle images: save to temp files
    image_paths = []
    if request.images:
        image_paths = save_images_to_temp(request.images)
        if image_paths:
            # Prepend image paths to message - Claude can read image files directly
            image_refs = "\n".join([f"Please read and analyze this image file: {path}" for path in image_paths])
            message = f"{image_refs}\n\n{message}" if message else image_refs
            print(f"[LocalServer] Added {len(image_paths)} image references to message")

    # Handle prepared sessions: first message spawns the CLI process
    if agent["status"] == "prepared":
        # Allow overriding CLI tool before spawning (user may have changed selection)
        if request.cli_tool:
            agent_obj = agent_manager._agents.get(agent_id)
            if agent_obj:
                agent_obj.cli_tool = request.cli_tool
        await agent_manager.spawn_cli_process(agent_id, message, image_paths, pre_logged=request.pre_logged)
        return {"success": True, "action": "started", "cli_tool": agent.get("cli_tool"), "images": len(image_paths)}

    print(
        f"[send_input] agent={agent_id} status={agent.get('status')} "
        f"session_id={agent.get('session_id')} cli_tool={agent.get('cli_tool')}"
    )

    if agent["status"] in ("paused", "completed", "failed"):
        # If we have a Claude CLI session id, resume it (cheaper, preserves
        # tool history). Otherwise — common for fresh chat sessions whose
        # initial run finished before emitting the `init` event with the
        # session_id — spawn a brand-new CLI process with the user's input
        # as the prompt. Either way the user's first message lands.
        if agent.get("session_id"):
            await agent_manager.resume_session(agent_id, message, image_paths, pre_logged=request.pre_logged)
            return {"success": True, "action": "resumed", "images": len(image_paths)}
        await agent_manager.spawn_cli_process(agent_id, message, image_paths, pre_logged=request.pre_logged)
        return {"success": True, "action": "started", "images": len(image_paths)}

    if agent["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail=f"Agent is running (status={agent['status']}, sid={agent.get('session_id')}). Wait for it to complete before sending another message."
        )

    raise HTTPException(
        status_code=400,
        detail=f"No active session (status={agent.get('status')}, sid={agent.get('session_id')}). Start a new task."
    )


@router.delete("/agents/{agent_id}")
def kill_agent(agent_id: str, cleanup_worktree: bool = False):
    """Kill an agent."""
    agent_manager.kill(agent_id, cleanup_worktree)
    return {"success": True}


@router.post("/agents/cleanup")
def cleanup_agents():
    """Clean up all stale/completed agents."""
    cleaned = agent_manager.cleanup_stale_agents()
    return {"success": True, "cleaned": cleaned, "remaining": len(agent_manager._agents)}


@router.delete("/agents")
def kill_all_agents(cleanup_worktrees: bool = False):
    """Kill all agents."""
    agent_ids = list(agent_manager._agents.keys())
    for agent_id in agent_ids:
        agent_manager.kill(agent_id, cleanup_worktrees)
    return {"success": True, "killed": len(agent_ids)}


@router.get("/agents/{agent_id}/output")
def get_output(agent_id: str):
    """Get full output buffer."""
    output = agent_manager.get_output(agent_id)
    return {"output": output}


@router.get("/agents/{agent_id}/stream")
async def stream_output(agent_id: str, request: Request):
    """Stream agent output via Server-Sent Events."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    queue = agent_manager.add_listener(agent_id)

    async def event_generator():
        """Yield Server-Sent Events for one agent output stream.

        Emits an initial ``connected`` frame plus a ``worktree_info`` frame so
        the frontend can render branch/worktree state, then a one-shot status
        frame when the agent is already ``prepared`` (deferred start) or in a
        terminal ``paused``/``completed``/``failed`` state. After that it
        drains the per-listener queue, forwarding each event and stopping on an
        ``exit`` event or client disconnect. A ``: heartbeat`` comment is sent
        every 15 seconds of idle time to keep the connection alive. The
        listener queue is always removed on exit via the ``finally`` block.
        """
        try:
            init_event = {"type": "connected", "agentId": agent_id, "status": agent['status']}
            yield f"data: {json.dumps(init_event)}\n\n"

            # Send worktree/branch info so frontend can update the UI
            worktree_event = {
                "type": "worktree_info",
                "branch": agent.get('branch'),
                "worktree_path": agent.get('worktree_path'),
                "is_worktree": agent.get('worktree_path') is not None,
                "work_dir": agent.get('work_dir')
            }
            yield f"data: {json.dumps(worktree_event)}\n\n"

            # If agent is prepared (deferred start), send status so frontend knows
            if agent['status'] == 'prepared':
                prepared_event = {
                    "type": "prepared",
                    "message": "Session ready. Send a message to start."
                }
                yield f"data: {json.dumps(prepared_event)}\n\n"

            # If agent is already paused/completed, send that status immediately
            if agent['status'] in ('paused', 'completed', 'failed'):
                paused_event = {
                    "type": "paused",
                    "exit_code": agent.get('exit_code'),
                    "session_id": agent.get('session_id'),
                    "can_resume": agent.get('can_resume', True)
                }
                yield f"data: {json.dumps(paused_event)}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"

                    if event.get("type") == "exit":
                        break

                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

        finally:
            agent_manager.remove_listener(agent_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )


@router.get("/check")
def check_cli_installed():
    """Check which AI CLI tools are installed locally."""
    from ..cli_providers import get_available_providers

    providers = get_available_providers()

    # Backwards compatible: 'installed' is True if any provider is available
    any_installed = any(p["installed"] for p in providers.values())
    # Claude path for backwards compat
    claude_info = providers.get("claude", {})

    return {
        "installed": any_installed,
        "path": claude_info.get("path"),
        "providers": providers
    }


@router.get("/stats")
def get_machine_stats():
    """
    Get combined machine stats in a single request.
    Returns agents, worktrees count, and working directory info.
    This avoids multiple relay round-trips.
    """
    from .worktrees import list_worktrees
    from .config_routes import get_working_directory

    # Get agents (fast - in memory)
    agents = agent_manager.list()
    agent_counts = {
        "running": 0,
        "paused": 0,
        "prepared": 0,
        "completed": 0,
        "failed": 0,
    }
    for agent in agents:
        status = agent.get("status")
        if status in agent_counts:
            agent_counts[status] += 1

    workflow_summary = get_workflow_summary()

    def _safe_run(cmd: list[str]) -> Optional[str]:
        """Run a short shell command and return its stripped stdout.

        Wraps :func:`subprocess.run` with a 2-second timeout and swallows all
        failures (non-zero exit, timeout, missing binary), returning ``None``
        so machine-stats collection never raises on a probe command.

        Args:
            cmd: Command and arguments to execute.

        Returns:
            The trimmed standard output on success, otherwise ``None``.
        """
        try:
            import subprocess as sp

            result = sp.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            return None
        return None

    def _get_cpu_percent() -> Optional[float]:
        """Estimate system-wide CPU utilization as a percentage.

        Sums the per-process ``%cpu`` column from ``ps`` and normalizes by the
        CPU count so the result reads as a single-core-equivalent percentage,
        clamped to the range ``[0.0, 999.0]``. Returns ``None`` if ``ps``
        produced no usable output.
        """
        output = _safe_run(["ps", "-A", "-o", "%cpu="])
        if not output:
            return None
        total = 0.0
        for line in output.splitlines():
            try:
                total += float(line.strip())
            except ValueError:
                continue
        cpu_count = os.cpu_count() or 1
        return round(min(max(total / cpu_count, 0.0), 999.0), 1)

    def _get_memory_stats() -> dict:
        """Collect physical-memory usage in a cross-platform way.

        On macOS it derives used memory from ``sysctl hw.memsize`` and the
        ``vm_stat`` page counters (free + inactive + speculative pages are
        treated as available). On Linux it reads ``MemTotal``/``MemAvailable``
        from ``/proc/meminfo``. All probe failures degrade gracefully, leaving
        the corresponding fields as ``None``.

        Returns:
            A dict with ``total_bytes``, ``used_bytes``, and ``percent`` (the
            used fraction rounded to one decimal, or ``None`` when unknown).
        """
        total_bytes = None
        used_bytes = None
        system = sys.platform

        if system == "darwin":
            total_raw = _safe_run(["sysctl", "-n", "hw.memsize"])
            page_size_raw = _safe_run(["sysctl", "-n", "hw.pagesize"])
            vm_stat_raw = _safe_run(["vm_stat"])
            try:
                total_bytes = int(total_raw) if total_raw else None
                page_size = int(page_size_raw) if page_size_raw else 4096
                if vm_stat_raw and total_bytes:
                    pages = {}
                    for line in vm_stat_raw.splitlines():
                        if ":" not in line:
                            continue
                        key, value = line.split(":", 1)
                        value = value.strip().rstrip(".")
                        value = value.replace(".", "").strip()
                        try:
                            pages[key.strip()] = int(value)
                        except ValueError:
                            continue
                    available_pages = (
                        pages.get("Pages free", 0)
                        + pages.get("Pages inactive", 0)
                        + pages.get("Pages speculative", 0)
                    )
                    used_bytes = max(total_bytes - (available_pages * page_size), 0)
            except Exception:
                pass
        elif os.path.exists("/proc/meminfo"):
            try:
                meminfo = {}
                with open("/proc/meminfo") as f:
                    for line in f:
                        key, value = line.split(":", 1)
                        meminfo[key] = int(value.strip().split()[0]) * 1024
                total_bytes = meminfo.get("MemTotal")
                available = meminfo.get("MemAvailable")
                if total_bytes and available is not None:
                    used_bytes = max(total_bytes - available, 0)
            except Exception:
                pass

        percent = None
        if total_bytes and used_bytes is not None:
            percent = round((used_bytes / total_bytes) * 100, 1)

        return {
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "percent": percent,
        }

    def _get_disk_stats(path: Optional[str]) -> dict:
        """Report disk usage for a path (or the default working dir).

        Uses :func:`shutil.disk_usage` on ``path`` when given, otherwise the
        configured default working directory. On any error it returns the same
        shape with ``None`` byte counts so the stats response stays consistent.

        Args:
            path: Filesystem path to measure; falls back to the default
                working directory when ``None``.

        Returns:
            A dict with ``path``, ``free_bytes``, ``total_bytes``,
            ``used_bytes``, and ``percent`` (used fraction, or ``None``).
        """
        try:
            import shutil

            target = path or get_default_working_dir()
            usage = shutil.disk_usage(target)
            return {
                "path": target,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "percent": round((usage.used / usage.total) * 100, 1) if usage.total else None,
            }
        except Exception:
            return {
                "path": path or get_default_working_dir(),
                "free_bytes": None,
                "total_bytes": None,
                "used_bytes": None,
                "percent": None,
            }

    cpu_count = os.cpu_count() or 1
    try:
        load1, load5, load15 = os.getloadavg()
        load = {
            "one": round(load1, 2),
            "five": round(load5, 2),
            "fifteen": round(load15, 2),
            "normalized_percent": round((load1 / cpu_count) * 100, 1),
        }
    except Exception:
        load = {
            "one": None,
            "five": None,
            "fifteen": None,
            "normalized_percent": None,
        }

    # Get worktrees (may be slower due to git commands)
    try:
        wt_result = list_worktrees()
        worktrees = wt_result.get("worktrees", [])
    except Exception as e:
        print(f"[Stats] Failed to get worktrees: {e}")
        worktrees = []

    # Get working directory config
    try:
        config = get_working_directory()
        home_dir = config.get("home_directory")
    except Exception as e:
        print(f"[Stats] Failed to get working dir: {e}")
        home_dir = None

    return {
        "agents": agents,
        "agent_counts": agent_counts,
        "workflow_summary": workflow_summary,
        "worktrees": worktrees,
        "home_directory": home_dir,
        "compute": {
            "cpu_percent": _get_cpu_percent(),
            "cpu_count": cpu_count,
            "memory": _get_memory_stats(),
            "load": load,
            "disk": _get_disk_stats(home_dir or get_default_working_dir()),
        },
    }
