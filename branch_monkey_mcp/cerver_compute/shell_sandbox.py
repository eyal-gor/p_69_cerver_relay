"""
A shell sandbox: run a command in a directory, return what it printed.

Cerver's workflow engine runs a `shell` step by asking this relay for a
sandbox with ``engine: "shell"`` and then POSTing the command to ``/run``.
Every such request used to open a Claude Code session and hand it the
command as a prompt — the agent parked itself as "paused", the command never
ran, and the step hung on cerver until it was swept. One kompany system
failed every night for two months that way.

A shell sandbox here is a working directory and nothing else. ``run`` executes
the command with the shell, bounded by the timeout, and answers with stdout,
stderr and the exit code — the shape cerver already reads.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Dict, Optional

from ..computer_runtime import guest_fence

_SANDBOXES: Dict[str, Dict[str, Any]] = {}


def create_shell_sandbox(metadata: Dict[str, Any], timeout_ms: Optional[int]) -> Dict[str, Any]:
    """Register a shell sandbox and return the provider-shaped descriptor.

    Someone else's session (``pool_session``, set by the gateway when the
    caller isn't this machine's owner) never gets the folder it asks for: it
    gets a fresh workspace, and every command runs inside the guest fence.
    """
    sandbox_id = f"shell_{uuid.uuid4().hex[:12]}"
    guest = bool(metadata.get("pool_session"))
    if guest:
        ok, why = guest_fence.fence_status()
        if not ok:
            raise PermissionError(f"This machine can't run other people's sessions safely: {why}.")
        working_dir = guest_fence.new_workspace(sandbox_id)
    else:
        working_dir = str(metadata.get("working_dir") or metadata.get("cwd") or os.getcwd())
        if not os.path.isdir(working_dir):
            working_dir = os.getcwd()
    env = metadata.get("env") if isinstance(metadata.get("env"), dict) else {}
    record = {
        "sandbox_id": sandbox_id,
        "working_dir": working_dir,
        "env": {str(k): str(v) for k, v in env.items()},
        "created_at": time.time(),
        "timeout_ms": timeout_ms,
        "runs": 0,
        "guest": guest,
    }
    _SANDBOXES[sandbox_id] = record
    return {
        "sandbox_id": sandbox_id,
        "remote_sandbox_id": sandbox_id,
        "provider": "cerver_local_provider",
        "engine": "shell",
        "status": "ready",
        "created_at": record["created_at"],
        "metadata": {**metadata, "cwd": working_dir},
        "capabilities": ["shell", "local-computer"],
    }


def _guest_path() -> str:
    """The PATH the relay probed at startup (finds node/claude/python), else the host's."""
    try:
        from ..computer_runtime import agent_environment
        return agent_environment.get_path_for_subprocess() or os.environ.get("PATH", "")
    except Exception:
        return os.environ.get("PATH", "")


def get_shell_sandbox(sandbox_id: str) -> Optional[Dict[str, Any]]:
    return _SANDBOXES.get(sandbox_id)


def delete_shell_sandbox(sandbox_id: str) -> bool:
    record = _SANDBOXES.pop(sandbox_id, None)
    if record and record.get("guest"):
        # A guest's workspace goes with its session.
        import shutil
        shutil.rmtree(record["working_dir"], ignore_errors=True)
    return record is not None


async def run_in_shell_sandbox(
    sandbox_id: str,
    command: str,
    timeout_seconds: int,
    envs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run ``command`` in the sandbox's directory; never raise for a failing command."""
    record = _SANDBOXES[sandbox_id]
    extra = {**record["env"], **({str(k): str(v) for k, v in (envs or {}).items()})}
    started = time.time()
    if record.get("guest"):
        # Someone else's command: no inherited env, fenced to its workspace.
        path_value = _guest_path()
        env = guest_fence.guest_env(record["working_dir"], path_value, extra)
        argv = guest_fence.wrap(["/bin/sh", "-c", command], record["working_dir"], path_value)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=record["working_dir"],
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        env = {**os.environ, **extra}
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=record["working_dir"],
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(1, timeout_seconds))
        exit_code = proc.returncode if proc.returncode is not None else 0
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        exit_code = 124
        timed_out = True
    record["runs"] += 1
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    if timed_out:
        stderr = (stderr + f"\n[shell sandbox] killed after {timeout_seconds}s").strip()
    return {
        "stdout": stdout[-200_000:],
        "stderr": stderr[-50_000:],
        "exit_code": exit_code,
        "duration_ms": int((time.time() - started) * 1000),
        "cwd": record["working_dir"],
        "sandbox_id": sandbox_id,
        "engine": "shell",
    }
