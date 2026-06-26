"""
DevServerManager — single owner of dev-server process lifecycle.

Responsibilities:
  - Spawn dev-server subprocesses with proper health checking
  - Port allocation and conflict detection
  - Ngrok tunnel management (only after server is confirmed healthy)
  - Proxy routing (which server is the active target)
  - State persistence to SQLite for relay-restart recovery
  - Structured logging with stderr capture on failure
"""

import asyncio
import os
import signal
import socket
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

def _log(msg):
    """Print to current stdout (captured by TUI if running)."""
    print(f"[DevServerManager] {msg}", flush=True)

from pydantic import BaseModel

from .database import (
    init_dev_servers_db,
    save_dev_server_to_db,
    delete_dev_server_from_db,
    load_dev_servers_from_db,
    _is_port_in_use,
)
from .config import find_dev_dir
from .dev_proxy import start_dev_proxy, set_proxy_target, get_proxy_status, _proxy_state
from .worktree import find_worktree_path


_SPAWN_DEFAULTS = dict(
    # stdin must be DEVNULL: with start_new_session the inherited /dev/tty
    # becomes invalid, causing Node.js PlatformInit to dup2 /dev/null onto
    # fd 0. A dup2 bug in Node v18-v20 asserts the return is 0, but dup2
    # returns the target fd — which crashes when fd > 0. Using DEVNULL
    # gives a valid fd 0 so the dup2 path is never entered.
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
)


# Optional ngrok support
try:
    from pyngrok import ngrok
    NGROK_AVAILABLE = True
except ImportError:
    NGROK_AVAILABLE = False
    ngrok = None


BASE_DEV_PORT = 6000
_READY_POLL_INTERVAL = 1       # seconds between port checks
_READY_POLL_MAX_ATTEMPTS = 20  # total wait ≈ 20s
_EARLY_EXIT_GRACE = 0.4        # seconds to wait before first poll() check


class DevServerRequest(BaseModel):
    """Incoming request to start a dev server."""
    task_id: Optional[str] = None
    task_number: int
    run_id: Optional[str] = None
    dev_script: Optional[str] = None
    working_dir: Optional[str] = None  # Subdirectory to run dev script in (e.g. "frontend")
    tunnel: Optional[bool] = False
    worktree_path: Optional[str] = None
    project_path: Optional[str] = None


class DevServerManager:
    """Manages the full lifecycle of local dev-server processes."""

    def __init__(self):
        """Initialise the manager and recover state from a prior relay run.

        Creates the in-memory ``_servers`` (run_id -> status dict) and
        ``_tunnels`` (run_id -> ngrok tunnel) registries, ensures the
        ``dev_servers`` SQLite table exists, then rehydrates ``_servers``
        from that table so servers spawned before a relay restart remain
        tracked. A module-level singleton (:data:`manager`) is instantiated
        once at import time.
        """
        self._servers: Dict[str, dict] = {}
        self._tunnels: Dict[str, object] = {}
        # Initialise DB table and recover servers that survived a relay restart
        init_dev_servers_db()
        load_dev_servers_from_db(self._servers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(
        self,
        task_number: int,
        run_id: str,
        task_id: Optional[str] = None,
        dev_script: Optional[str] = None,
        working_dir: Optional[str] = None,
        tunnel: bool = False,
        worktree_path: Optional[str] = None,
        project_path: Optional[str] = None,
    ) -> dict:
        """Start a dev server. Returns status dict for the frontend."""
        from fastapi import HTTPException

        self._ensure_proxy()

        # --- Already running? ------------------------------------------------
        if run_id in self._servers:
            result = self._handle_existing(run_id, tunnel)
            if result is not None:
                return result
            # Stale entry was cleaned up — fall through to start fresh

        # --- Resolve worktree ------------------------------------------------
        worktree_path = self._resolve_worktree(task_number, worktree_path, project_path)

        # --- Pick a port -----------------------------------------------------
        port = self._find_available_port(BASE_DEV_PORT + task_number)

        # --- Spawn the process -----------------------------------------------
        process, command = self._spawn(dev_script, port, worktree_path, run_id, task_number, working_dir, tunnel)

        # --- Register immediately (before readiness check) -------------------
        self._servers[run_id] = {
            "process": process,
            "port": port,
            "task_id": task_id,
            "task_number": task_number,
            "run_id": run_id,
            "worktree_path": str(worktree_path),
            "started_at": datetime.now().isoformat(),
            "tunnel_url": None,
        }
        save_dev_server_to_db(run_id, self._servers[run_id])

        # --- Wait for healthy or detect early death --------------------------
        ready, error_msg = await self._wait_until_ready(process, port, run_id)

        if not ready:
            # Clean up the dead process
            self._cleanup(run_id)
            detail = f"Dev server failed to start"
            if error_msg:
                detail += f": {error_msg}"
            raise HTTPException(status_code=500, detail=detail)

        # --- Tunnel (only after confirmed healthy) ---------------------------
        tunnel_url = None
        if tunnel:
            tunnel_url = self._start_tunnel(port, run_id)
            self._servers[run_id]["tunnel_url"] = tunnel_url

        # --- Point proxy at this server --------------------------------------
        set_proxy_target(port, run_id)
        proxy_status = get_proxy_status()

        return {
            "port": port,
            "url": f"http://localhost:{port}",
            "proxyUrl": proxy_status["proxyUrl"],
            "tunnelUrl": tunnel_url,
            "runId": run_id,
            "status": "started",
        }

    def stop(self, run_id: str) -> dict:
        """Stop a running dev server."""
        from fastapi import HTTPException

        if run_id not in self._servers:
            raise HTTPException(status_code=404, detail="Server not found")

        self._cleanup(run_id)
        return {"status": "stopped", "runId": run_id}

    def list(self) -> dict:
        """List running dev servers, pruning any that have died."""
        servers = []
        dead = []
        proxy_status = get_proxy_status()

        for run_id, info in self._servers.items():
            if not _is_port_in_use(info["port"]):
                # Don't prune servers still in startup grace period (30s)
                started = info.get("started_at")
                if started:
                    try:
                        age = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
                        if age < 30:
                            _log(f" Server {run_id} port not ready yet ({age:.0f}s old), skipping prune")
                            continue
                    except Exception:
                        pass
                dead.append(run_id)
                continue

            is_active = proxy_status["targetRunId"] == run_id
            servers.append({
                "runId": run_id,
                "taskNumber": info.get("task_number"),
                "port": info["port"],
                "url": f"http://localhost:{info['port']}",
                "proxyUrl": proxy_status["proxyUrl"] if is_active else None,
                "tunnelUrl": info.get("tunnel_url"),
                "isActive": is_active,
                "startedAt": info["started_at"],
                "worktreePath": info.get("worktree_path"),
            })

        for run_id in dead:
            _log(f" Pruning dead server {run_id}")
            self._cleanup(run_id)

        return {"servers": servers, "proxy": proxy_status}

    def get_servers(self) -> Dict[str, dict]:
        """Raw dict of tracked servers (used by proxy route)."""
        return self._servers

    # ------------------------------------------------------------------
    # Tunnel helpers (exposed for advanced.py time-machine reuse)
    # ------------------------------------------------------------------

    def start_tunnel(self, port: int, run_id: str) -> Optional[str]:
        """Open an ngrok tunnel to ``port``, keyed by ``run_id``.

        Public wrapper over :meth:`_start_tunnel` exposed so advanced.py's
        time-machine flow can reuse tunnel management.

        Args:
            port: Local port the dev server is listening on.
            run_id: Identifier the tunnel is tracked under in ``_tunnels``.

        Returns:
            The public ngrok URL, or ``None`` if pyngrok is unavailable or
            the connection fails.
        """
        return self._start_tunnel(port, run_id)

    def stop_tunnel(self, run_id: str):
        """Tear down the ngrok tunnel tracked under ``run_id``, if any.

        Public wrapper over :meth:`_stop_tunnel`. A no-op when no tunnel is
        tracked for ``run_id``.
        """
        self._stop_tunnel(run_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_proxy(self):
        """Lazily start the shared dev proxy if it isn't already running.

        The proxy is the single front door that routes incoming requests to
        whichever dev server is the active target, so it must be up before
        any server is registered. Idempotent.
        """
        if not _proxy_state["running"]:
            start_dev_proxy()

    def _handle_existing(self, run_id: str, tunnel: bool) -> dict:
        """Return status for an already-tracked server, re-validate health."""
        info = self._servers[run_id]

        if _is_port_in_use(info["port"]):
            set_proxy_target(info["port"], run_id)
            proxy_status = get_proxy_status()

            tunnel_url = info.get("tunnel_url")
            if tunnel_url and run_id not in self._tunnels:
                _log(f" Stale tunnel for {run_id}, recreating")
                tunnel_url = self._start_tunnel(info["port"], run_id)
                info["tunnel_url"] = tunnel_url
            elif not tunnel_url and tunnel:
                tunnel_url = self._start_tunnel(info["port"], run_id)
                info["tunnel_url"] = tunnel_url

            return {
                "port": info["port"],
                "url": f"http://localhost:{info['port']}",
                "proxyUrl": proxy_status["proxyUrl"],
                "tunnelUrl": tunnel_url,
                "runId": run_id,
                "status": "already_running",
            }

        # Process died — clean up stale entry and return None so start() retries
        _log(f" Stale entry for {run_id} (port {info['port']} dead), cleaning up")
        self._cleanup(run_id)
        return None

    def _resolve_worktree(self, task_number: int, worktree_path: Optional[str], project_path: Optional[str]) -> str:
        """Resolve the directory the dev server should run in.

        Resolution order: an explicit ``worktree_path`` (validated to exist),
        then a worktree discovered for ``task_number`` via
        :func:`find_worktree_path`, then ``project_path`` itself as a fallback
        for cases with no worktree (e.g. main-chat).

        Args:
            task_number: Task whose worktree should be located.
            worktree_path: Caller-supplied worktree path, if known.
            project_path: Project root used both for worktree discovery and as
                the last-resort run directory.

        Returns:
            An existing directory path to spawn the dev server in.

        Raises:
            HTTPException: 404 if an explicit ``worktree_path`` does not exist,
                or if no worktree (and no usable ``project_path``) is found.
        """
        from fastapi import HTTPException

        if worktree_path:
            if not Path(worktree_path).exists():
                raise HTTPException(status_code=404, detail=f"Worktree path not found: {worktree_path}")
            _log(f" Using provided worktree: {worktree_path}")
            return worktree_path

        resolved = find_worktree_path(task_number, project_path)
        if resolved:
            return resolved

        # Fall back to project_path itself (e.g. main-chat with no worktree)
        if project_path and Path(project_path).exists():
            _log(f" No worktree found, using project path: {project_path}")
            return project_path

        detail = f"No worktree found for task {task_number}"
        if project_path:
            detail += f" in {project_path}"
        raise HTTPException(status_code=404, detail=detail)

    @staticmethod
    def _find_available_port(base: int) -> int:
        """Return the first free TCP port at or above ``base`` on localhost.

        Probes ports sequentially with a non-blocking connect; a port that
        refuses the connection is considered free.

        Args:
            base: Lowest port number to try.

        Returns:
            An available port in ``[base, base + 100]``.

        Raises:
            RuntimeError: If every port in that range is in use.
        """
        port = base
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", port)) != 0:
                    return port
            port += 1
            if port > base + 100:
                raise RuntimeError("No available port in range")

    def _spawn(self, dev_script: Optional[str], port: int, cwd: str, run_id: str, task_number: int, working_dir: Optional[str] = None, tunnel: bool = False):
        """Spawn the subprocess. Returns (process, command_str)."""
        from fastapi import HTTPException

        # Resolve run directory: explicit working_dir > scan for package.json with dev scripts > cwd
        if working_dir:
            run_cwd = str(Path(cwd) / working_dir)
        else:
            run_cwd, _ = find_dev_dir(cwd)

        # Validate working directory exists
        if not Path(run_cwd).exists():
            raise HTTPException(
                status_code=404,
                detail=f"Working directory not found: {run_cwd}. Configure working_dir in project settings.",
            )

        # Ensure node_modules exists (applies to both custom and default scripts)
        node_modules = Path(run_cwd) / "node_modules"
        if not node_modules.exists():
            _log(f" Installing deps for task {task_number} in {run_cwd}...")
            try:
                subprocess.run(
                    "npm install",
                    shell=True,
                    cwd=run_cwd,
                    timeout=180,
                    check=True,
                    **_SPAWN_DEFAULTS,
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=500, detail="npm install timed out")
            except subprocess.CalledProcessError as e:
                raise HTTPException(status_code=500, detail=f"npm install failed: {e.stderr}")

        # When tunneling (ngrok), configure env for external host access
        env = {**os.environ}
        if tunnel:
            env["DANGEROUSLY_DISABLE_HOST_CHECK"] = "true"  # CRA
            env["WATCHPACK_POLLING"] = "true"
            # Vite 6.3+ reads this env var to allow tunnel hostnames
            # Leading dot enables wildcard suffix matching (*.ngrok-free.dev)
            env["__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"] = ".ngrok-free.dev"

        spawn_kwargs = {**_SPAWN_DEFAULTS, "env": env} if tunnel else _SPAWN_DEFAULTS

        # When tunneling, add --host so the server binds to 0.0.0.0
        tunnel_flags = " --host" if tunnel else ""

        if dev_script:
            command = dev_script.replace("{port}", str(port)) + tunnel_flags
            _log(f" Custom script for {run_id}: {command} (cwd: {run_cwd})")
            process = subprocess.Popen(
                command, shell=True, cwd=run_cwd, **spawn_kwargs,
            )
            return process, command

        # Default: npm run dev
        cmd = f"npm run dev -- --port {port}{tunnel_flags}"
        _log(f" Starting default server for {run_id} on port {port} (cwd: {run_cwd}) tunnel={tunnel}")
        process = subprocess.Popen(
            cmd, shell=True, cwd=run_cwd, **spawn_kwargs,
        )
        _log(f" Spawned PID={process.pid} PGID={os.getpgid(process.pid)}")
        return process, cmd

    async def _wait_until_ready(self, process: subprocess.Popen, port: int, run_id: str):
        """Poll until the server is healthy or confirmed dead.

        Returns (ready: bool, error_message: str | None).
        """
        # Brief grace period before first health check
        await asyncio.sleep(_EARLY_EXIT_GRACE)

        for attempt in range(_READY_POLL_MAX_ATTEMPTS):
            # --- Check if process died ---
            exit_code = process.poll()
            if exit_code is not None:
                stderr_out = ""
                try:
                    stderr_out = process.stderr.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
                _log(f" Process PID={process.pid} for {run_id} exited with code {exit_code} at attempt {attempt}")
                if stderr_out:
                    # Limit log noise
                    truncated = stderr_out[:1000]
                    _log(f" stderr: {truncated}")
                return False, stderr_out[:500] or f"Process exited with code {exit_code}"

            # --- Check if port is listening ---
            if _is_port_in_use(port):
                try:
                    req = urllib.request.Request(f"http://localhost:{port}/", method="HEAD")
                    urllib.request.urlopen(req, timeout=2)
                    _log(f" Server {run_id} ready on port {port} (attempt {attempt + 1})")
                    return True, None
                except Exception:
                    # Port open but HTTP not ready yet — accept after a few tries
                    if attempt >= 3:
                        _log(f" Server {run_id} port {port} open (accepting)")
                        return True, None

            await asyncio.sleep(_READY_POLL_INTERVAL)

        # Timed out but process is still alive — it might just be slow
        _log(f" Server {run_id} not ready after {_READY_POLL_MAX_ATTEMPTS}s, but process alive — accepting")
        return True, None

    def _cleanup(self, run_id: str):
        """Kill process, stop tunnel, remove from registry and DB."""
        info = self._servers.get(run_id)
        if not info:
            return

        # Kill process
        proc = info.get("process")
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        # Stop tunnel
        self._stop_tunnel(run_id)

        # Clear proxy if this was the active target
        if _proxy_state["target_run_id"] == run_id:
            _proxy_state["target_port"] = None
            _proxy_state["target_run_id"] = None

        delete_dev_server_from_db(run_id)
        self._servers.pop(run_id, None)

    # --- Tunnel wrappers ---

    def _start_tunnel(self, port: int, run_id: str) -> Optional[str]:
        """Open an ngrok HTTP tunnel to ``port`` and record it under ``run_id``.

        Args:
            port: Local port to expose.
            run_id: Key the resulting tunnel is stored under in ``_tunnels``.

        Returns:
            The public tunnel URL, or ``None`` if pyngrok is not installed or
            the connection raises (the error is logged, not propagated).
        """
        if not NGROK_AVAILABLE:
            print("[DevServerManager] pyngrok not installed — tunnel unavailable")
            return None
        try:
            tun = ngrok.connect(port, "http")
            self._tunnels[run_id] = tun
            _log(f" Tunnel for port {port}: {tun.public_url}")
            return tun.public_url
        except Exception as e:
            _log(f" Tunnel failed: {e}")
            return None

    def _stop_tunnel(self, run_id: str):
        """Disconnect and forget the ngrok tunnel tracked under ``run_id``.

        Pops the tunnel from ``_tunnels`` and disconnects it via ngrok.
        A no-op if no tunnel is tracked; disconnect errors are logged, not
        raised.
        """
        tun = self._tunnels.pop(run_id, None)
        if tun:
            try:
                ngrok.disconnect(tun.public_url)
                _log(f" Tunnel stopped for {run_id}")
            except Exception as e:
                _log(f" Tunnel stop failed: {e}")


# Singleton instance — used by routes
manager = DevServerManager()
