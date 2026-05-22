"""
Local Agent Manager for AI CLI execution.

This module manages the lifecycle of local AI agent instances (Claude Code, Codex, etc.),
including creation, execution, session resumption, and cleanup.
"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import HTTPException

from ..computer_runtime.cli_runtime import (
    build_resume_cli_command,
    build_run_cli_command,
    resolve_cli_provider,
    spawn_cli_subprocess,
)
from ..computer_runtime.execution import (
    broadcast_to_agent_listeners,
    build_agent_prompt,
    extract_result_from_output_buffer,
    process_provider_output_text,
)
from ..cerver_connect_transport import (
    get_active_transport,
    publish_stream_event_nowait,
)
from .cli_providers import CliProvider
from .config import get_default_working_dir
from .git_utils import is_git_repo, get_current_branch, generate_branch_name
from .worktree import create_worktree, find_worktree_for_branch, remove_worktree


def _model_provider_for_cli(cli_tool: str) -> str:
    """Map a cli_tool name → the upstream vendor for that CLI's models.
    Used when stamping observed model info on session metadata so the
    cerver UI can format "claude-sonnet-4-6 (anthropic)" without having
    to infer the vendor from the model string."""
    return {
        "claude": "anthropic",
        "codex": "openai",
        "grok": "xai",
    }.get((cli_tool or "").lower(), "")


@dataclass
class LocalAgent:
    """Represents a running local AI CLI agent."""
    id: str
    task_id: Optional[str]
    task_number: Optional[int]
    task_title: str
    task_description: Optional[str]
    repo_dir: str
    work_dir: str
    worktree_path: Optional[str]
    branch: Optional[str]
    branch_created: bool
    status: str  # prepared, starting, running, paused, completed, failed, stopped
    cli_tool: str = ""  # Which CLI provider to use (resolved at creation time)
    cli_model: str = ""  # Per-call model override forwarded to the CLI on spawn. Empty = use the CLI's local default (claude → Claude Code default, codex → ~/.codex/config.toml's `model`).
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    output_buffer: List[str] = field(default_factory=list)
    output_listeners: List[asyncio.Queue] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    exit_code: Optional[int] = None
    session_id: Optional[str] = None
    callback: Optional[Dict] = None  # Cron completion callback info
    extra_env: Optional[Dict[str, str]] = None  # Project-scoped env vars (e.g. BUFFER_API_KEY) passed by kompany/cerver and inherited by the spawned CLI process.
    # Cron-style runs complete on exit; chat-style runs pause and wait for
    # follow-up input. Both can carry a callback (chat needs one to publish
    # stream events to cerver), so we can't use callback presence as the
    # signal. Defaulting to False keeps interactive sessions alive — only
    # explicit one-shot triggers (run-agent, etc.) flip this on.
    complete_on_exit: bool = False
    # Set when the stall watchdog terminates the CLI subprocess. Lets the
    # post-loop diagnostic distinguish "CLI hung silently" from a normal
    # non-zero exit, and surfaces a clear reason to the user instead of
    # a bare SIGTERM exit code.
    watchdog_killed: bool = False
    # Dedup transcript pushes by content signature (role + kind + tool_id +
    # sha1(content)). Replaces the older message.id-only dedup, which let
    # `result` events through as silent duplicates of their matching
    # `assistant` events (no msg.id on result events → bypass).
    _pushed_signatures: set = field(default_factory=set)
    # Push pipeline observability — counters that the /agents/:id endpoint
    # surfaces so we can see whether transcript pushes are healthy without
    # tailing logs.
    _push_stats: Dict[str, int] = field(default_factory=lambda: {
        "pushed": 0,        # entries the relay attempted to POST (incremented before HTTP)
        "http_2xx": 0,      # entries acknowledged by cerver with 2xx
        "http_4xx": 0,      # cerver rejected (auth / shape error)
        "http_5xx": 0,      # cerver server error
        "http_exc": 0,      # network / timeout / unhandled exception
        "dedup_skipped": 0, # entries skipped because their signature was already sent
        "transport_waits": 0,  # times we blocked waiting for the connect transport to be ready
        "drops": 0,         # entries dropped because transport never became ready (last resort)
    })
    # Diagnostics: last few non-2xx error tails (status, body preview).
    # Capped so a misconfigured cerver can't balloon agent memory.
    _push_errors: List[str] = field(default_factory=list)
    # Last URL the relay actually POSTed to. Reveals stale / wrong
    # cerver_session_id without log scraping.
    _last_push_url: Optional[str] = None


class LocalAgentManager:
    """Manages local Claude Code agent instances."""

    MAX_AGENTS = 50  # Max concurrent agents; was 10. Lifted because a single
                    # `cerver compare` with 3 CLIs uses 3 slots, so 3 parallel
                    # compares already burned past the old cap. Real bound
                    # should be resource-based (CPU/memory) — see suggestion
                    # sg_b703c2a2 — but 50 is enough headroom for now.
    STALE_TIMEOUT = 3600  # Agents idle for 1 hour are considered stale

    # Window (seconds) that completed/failed one-shot agents linger in
    # the recent-history buffer so the relay TUI's Runtime tab can
    # show them. The active `_agents` dict is freed immediately on
    # complete_on_exit (so MAX_AGENTS slots reopen for the next burst);
    # this separate buffer is observation-only — looked at by `list()`
    # for the TUI / stats endpoint, not by the active-management code.
    # 90s is long enough to catch a typical `cerver run` cycle (cold
    # boot + run + display) without piling up forever in long-running
    # relays.
    RECENT_HISTORY_TTL_SECONDS = 90
    RECENT_HISTORY_MAX = 30

    def __init__(self):
        self._agents: Dict[str, LocalAgent] = {}
        self._output_tasks: Dict[str, asyncio.Task] = {}
        # Recently-completed one-shot agents, kept around so the
        # Runtime tab in the relay TUI shows recent activity instead
        # of "0 agents" between cerver run invocations. Each entry is
        # a dict in the same shape `list()` returns for active agents,
        # plus an `_evicted_at` timestamp the trimming logic uses.
        from collections import deque
        self._recent_completed: deque = deque(maxlen=self.RECENT_HISTORY_MAX)

    def cleanup_stale_agents(self) -> int:
        """Remove agents that are completed, failed, or stale. Returns count removed."""
        now = datetime.now()
        stale_ids = []

        for agent_id, agent in self._agents.items():
            # Remove failed/stopped agents
            if agent.status in ("failed", "stopped"):
                stale_ids.append(agent_id)
                continue

            # Check if process is still running
            process_exited = False
            if agent.process:
                poll = agent.process.poll()
                if poll is not None:
                    process_exited = True

            # Remove completed/paused agents whose process has exited and are past stale timeout
            if agent.status in ("completed", "paused") or process_exited:
                if agent.created_at:
                    try:
                        age = (now - agent.created_at).total_seconds()
                        if age > self.STALE_TIMEOUT:
                            print(f"[LocalAgent] Agent {agent_id} is stale ({agent.status}, age={int(age)}s)")
                            stale_ids.append(agent_id)
                            continue
                    except Exception:
                        pass
                # No session_id = no resumption value, clean immediately
                if not agent.session_id:
                    stale_ids.append(agent_id)
                    continue

            # Check for stale agents (no activity for a while) regardless of status
            if agent.created_at:
                try:
                    if (now - agent.created_at).total_seconds() > self.STALE_TIMEOUT:
                        print(f"[LocalAgent] Agent {agent_id} is stale (created {agent.created_at})")
                        stale_ids.append(agent_id)
                except Exception:
                    pass

        for agent_id in stale_ids:
            print(f"[LocalAgent] Cleaning up agent {agent_id}")
            self.kill(agent_id)

        return len(stale_ids)

    async def create(
        self,
        task_id: Optional[str] = None,
        task_number: Optional[int] = None,
        task_title: str = "",
        task_description: Optional[str] = None,
        working_dir: Optional[str] = None,
        prompt: Optional[str] = None,
        system_prompt: Optional[str] = None,
        skip_branch: bool = False,
        branch: Optional[str] = None,
        defer_start: bool = False,
        callback: Optional[Dict] = None,
        cli_tool: Optional[str] = None,
        cli_model: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        complete_on_exit: bool = False,
    ) -> dict:
        """Create and optionally start a new local AI agent.

        If defer_start=True, sets up worktree/branch/tracking but does NOT spawn
        the CLI process. The session enters "prepared" status and waits for the
        first message via send_input, which calls spawn_cli_process().

        Args:
            cli_tool: Which CLI to use ('claude', 'codex'). Defaults to 'claude'.
        """

        # Clean up stale agents first
        cleaned = self.cleanup_stale_agents()
        if cleaned > 0:
            print(f"[LocalAgent] Cleaned up {cleaned} stale agents")

        # Check max agent limit
        if len(self._agents) >= self.MAX_AGENTS:
            raise HTTPException(
                status_code=429,
                detail=f"Maximum number of agents ({self.MAX_AGENTS}) reached. Kill some agents first."
            )

        # Resolve CLI provider
        provider = resolve_cli_provider(cli_tool)
        cli_path = provider.is_available()
        if not cli_path:
            raise HTTPException(
                status_code=400,
                detail=f"{provider.display_name} CLI not found. Install with: {provider.install_hint}"
            )

        agent_id = str(uuid.uuid4())[:8]
        repo_dir = working_dir or get_default_working_dir()
        work_dir = repo_dir
        target_branch = branch  # Explicit branch from caller (e.g. 'staging')
        branch_created = False
        worktree_path = None

        # Handle git worktree if in a git repo
        print(f"[LocalAgent] Worktree check: task_number={task_number}, branch={target_branch}, is_git={is_git_repo(repo_dir)}, skip_branch={skip_branch}")
        if is_git_repo(repo_dir):
            if task_number and not skip_branch:
                # Task mode: generate branch name from task number
                target_branch = generate_branch_name(task_number, task_title, agent_id)
                print(f"[LocalAgent] Creating worktree for task branch: {target_branch}")
                result = create_worktree(repo_dir, target_branch, task_number, agent_id)
                print(f"[LocalAgent] Worktree result: {result}")

                if result["success"]:
                    worktree_path = result["worktree_path"]
                    work_dir = worktree_path
                    branch_created = result["branch_created"]
                else:
                    target_branch = get_current_branch(repo_dir)
            elif target_branch:
                # If the requested branch is already checked out at repo_dir,
                # just use it directly — no point creating a duplicate worktree
                # (and `git worktree add` would fail for an already-checked-out
                # branch anyway).
                current = get_current_branch(repo_dir)
                if current == target_branch:
                    print(f"[LocalAgent] Branch '{target_branch}' already checked out at {repo_dir}, using it directly")
                    work_dir = repo_dir
                else:
                    print(f"[LocalAgent] Creating worktree for explicit branch: {target_branch}")
                    result = create_worktree(repo_dir, target_branch, 0, f"{target_branch}-{agent_id}")
                    print(f"[LocalAgent] Worktree result: {result}")

                    if result["success"]:
                        worktree_path = result["worktree_path"]
                        work_dir = worktree_path
                        branch_created = result["branch_created"]
                    else:
                        # Worktree creation failed — usually because the branch
                        # is already checked out in another worktree. Find that
                        # worktree and use it, so we don't end up running on
                        # whatever stale branch repo_dir happens to be on.
                        existing = find_worktree_for_branch(repo_dir, target_branch)
                        if existing:
                            print(f"[LocalAgent] Reusing existing worktree for '{target_branch}': {existing}")
                            worktree_path = existing
                            work_dir = existing
                        else:
                            print(f"[LocalAgent] Worktree create failed and no existing worktree for '{target_branch}'; falling back to repo_dir on '{current}'")
                            work_dir = repo_dir
                            target_branch = current
            else:
                # No task, no explicit branch: work in current directory
                target_branch = get_current_branch(repo_dir)

        # If deferring start, create the agent record in "prepared" status and return
        if defer_start:
            agent = LocalAgent(
                id=agent_id,
                task_id=task_id,
                task_number=task_number,
                task_title=task_title,
                task_description=task_description,
                repo_dir=repo_dir,
                work_dir=work_dir,
                worktree_path=worktree_path,
                branch=target_branch,
                branch_created=branch_created,
                status="prepared",
                cli_tool=provider.name,
                cli_model=(cli_model or ""),
                callback=callback,
                extra_env=extra_env,
                complete_on_exit=complete_on_exit,
            )
            self._agents[agent_id] = agent
            print(f"[LocalAgent] Session prepared (deferred start): {agent_id}")

            return {
                "id": agent_id,
                "task_id": task_id,
                "task_number": task_number,
                "task_title": task_title,
                "status": "prepared",
                "type": "local",
                "work_dir": work_dir,
                "worktree_path": worktree_path,
                "branch": target_branch,
                "branch_created": branch_created,
                "is_worktree": worktree_path is not None
            }

        # Build prompt and spawn CLI process immediately
        final_prompt = self._build_prompt(prompt, task_id, task_number, task_title, task_description, target_branch, worktree_path, work_dir)

        agent = LocalAgent(
            id=agent_id,
            task_id=task_id,
            task_number=task_number,
            task_title=task_title,
            task_description=task_description,
            repo_dir=repo_dir,
            work_dir=work_dir,
            worktree_path=worktree_path,
            branch=target_branch,
            branch_created=branch_created,
            status="starting",
            cli_tool=provider.name,
            callback=callback,
            extra_env=extra_env,
            complete_on_exit=complete_on_exit,
        )

        self._agents[agent_id] = agent

        # Push the initial user prompt to cerver so the transcript starts
        # with the user's question, not the agent's first response. Uses the
        # raw `prompt` arg (not the worktree-augmented final_prompt) — that's
        # what the user actually asked.
        if prompt:
            self._push_user_message(agent, prompt)

        try:
            self._start_cli_process(agent, final_prompt, system_prompt=system_prompt)

            return {
                "id": agent_id,
                "task_id": task_id,
                "task_number": task_number,
                "task_title": task_title,
                "status": agent.status,
                "type": "local",
                "cli_tool": agent.cli_tool,
                "work_dir": work_dir,
                "worktree_path": worktree_path,
                "branch": target_branch,
                "branch_created": branch_created,
                "is_worktree": worktree_path is not None
            }

        except Exception as e:
            agent.status = "failed"
            raise HTTPException(status_code=500, detail=f"Failed to start {provider.display_name}: {str(e)}")

    def _build_prompt(
        self,
        prompt: Optional[str],
        task_id: Optional[str],
        task_number: Optional[int],
        task_title: str,
        task_description: Optional[str],
        target_branch: Optional[str],
        worktree_path: Optional[str],
        work_dir: Optional[str] = None
    ) -> str:
        """Build the final prompt, prepending worktree/workspace info if applicable."""
        return build_agent_prompt(
            prompt=prompt,
            task_id=task_id,
            task_number=task_number,
            task_title=task_title,
            task_description=task_description,
            target_branch=target_branch,
            worktree_path=worktree_path,
        )

    def _get_provider(self, agent: LocalAgent) -> CliProvider:
        """Get the CLI provider for an agent."""
        return resolve_cli_provider(agent.cli_tool)

    def _start_cli_process(self, agent: LocalAgent, final_prompt: str, system_prompt: Optional[str] = None) -> None:
        """Spawn the CLI process and start reading output."""
        provider = self._get_provider(agent)
        cli_cmd = build_run_cli_command(
            provider, final_prompt, system_prompt=system_prompt,
            model=(agent.cli_model or None),
        )
        process = spawn_cli_subprocess(cli_cmd, agent.work_dir, extra_env=agent.extra_env)

        agent.pid = process.pid
        agent.process = process
        agent.status = "running"

        print(f"[LocalAgent] Started {provider.display_name}, PID: {process.pid}")

        self._output_tasks[agent.id] = asyncio.create_task(
            self._read_json_output(agent)
        )

    async def spawn_cli_process(self, agent_id: str, message: str, image_paths: List[str] = None, pre_logged: bool = False) -> None:
        """Spawn a CLI process from a quiescent agent.

        `pre_logged=True` means the user message is already in cerver's
        transcript (the gateway's /v2/sessions/:id/input wrote it before
        forwarding to us). Skip _push_user_message in that case — otherwise
        we duplicate the entry ~700ms after the gateway did.

        Called by send_input when:
          - status == "prepared" (first message of a deferred session)
          - status in (completed, failed, paused) and no session_id
            (fresh chat whose initial run finished before Claude emitted
             session_id, so we can't /resume — start over instead)

        Refuses only when an agent is *currently* running, since spawning
        a second CLI on top of a live one would race for the worktree.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        if agent.status == "running":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot spawn — agent is already running (status: {agent.status})"
            )

        # Build the final prompt with worktree/workspace context + user message
        final_prompt = self._build_prompt(
            message, agent.task_id, agent.task_number,
            agent.task_title, agent.task_description,
            agent.branch, agent.worktree_path, agent.work_dir
        )

        print(f"[LocalAgent] Spawning CLI for prepared session {agent_id}")

        # Mirror the user's first message into cerver before the CLI starts
        # producing assistant output — but only if the gateway didn't
        # already write it via recordInput (pre_logged=True case).
        if not pre_logged:
            self._push_user_message(agent, message)

        try:
            self._start_cli_process(agent, final_prompt)
        except Exception as e:
            agent.status = "failed"
            provider = self._get_provider(agent)
            raise HTTPException(status_code=500, detail=f"Failed to start {provider.display_name}: {str(e)}")

    # Seconds of silence on a running CLI's stdout before the watchdog
    # gives up and SIGTERMs the subprocess. Tuned to be longer than a
    # typical slow first response (claude on a busy box) but shorter
    # than the cerver-CLI client's 180s WaitForReply ceiling — so the
    # relay surfaces "stalled, killed" before the gateway gives up.
    STALL_TIMEOUT_SEC = 150

    async def _read_json_output(self, agent: LocalAgent) -> None:
        """Read JSON output from subprocess and broadcast to listeners.

        A watchdog task runs alongside the read loop. If the CLI emits
        no output for STALL_TIMEOUT_SEC seconds, the watchdog terminates
        the subprocess — converts silent hangs (auth re-check stalls,
        MCP server enumeration timeouts, model-side queueing pauses) into
        a visible exit with `agent.watchdog_killed=True`, instead of
        letting the cerver-CLI client time out at 3 minutes while the
        relay still thinks the agent is running.
        """
        loop = asyncio.get_event_loop()
        provider = self._get_provider(agent)

        async def watchdog():
            # Check every 15s. last_activity is bumped to now() on every
            # output line read below, so an alive CLI never trips this.
            try:
                while agent.status == "running" and agent.process and agent.process.poll() is None:
                    await asyncio.sleep(15)
                    if agent.status != "running":
                        return
                    idle = (datetime.now() - agent.last_activity).total_seconds()
                    if idle > self.STALL_TIMEOUT_SEC:
                        print(
                            f"[LocalAgent] watchdog: agent {agent.id} silent {int(idle)}s "
                            f"(threshold {self.STALL_TIMEOUT_SEC}s); terminating CLI"
                        )
                        agent.watchdog_killed = True
                        try:
                            agent.process.terminate()
                        except Exception:
                            pass
                        return
            except asyncio.CancelledError:
                pass

        watchdog_task = asyncio.create_task(watchdog())

        def read_line():
            try:
                if agent.process and agent.process.stdout:
                    line = agent.process.stdout.readline()
                    return line
                return b''
            except Exception:
                return b''

        while agent.status == "running":
            try:
                line = await loop.run_in_executor(None, read_line)

                if not line:
                    break

                text = line.decode('utf-8', errors='replace').strip()
                if not text:
                    continue

                agent.last_activity = datetime.now()
                before_session_id = agent.session_id
                event = process_provider_output_text(agent, provider, text)
                if not event:
                    continue
                if agent.session_id and agent.session_id != before_session_id:
                    print(f"[LocalAgent] Got session_id: {agent.session_id}")
                    # Push to cerver so the gateway can hand it back to a
                    # future /input call as a recovery hint. Without this,
                    # a relay restart loses the only copy of the CLI's
                    # resume id and the session can never be resumed
                    # cleanly. Fire-and-forget — transcript pushes carry
                    # most of the value even if this PATCH drops.
                    asyncio.create_task(self._push_cli_state_to_cerver(agent))
                await broadcast_to_agent_listeners(agent, event)

                # Live: publish to cerver-connect for browser/CLI subscribers.
                self._publish_stream_to_cerver(agent, event)

                # Durable: push every event to cerver's transcript (HTTP).
                # Lets headless cron / workflow runs persist their full
                # transcript instead of just the final output.
                self._push_event_to_cerver(agent, event)

                # Extract token usage from `result` events (claude) and
                # `turn.completed` events (codex, already normalized to
                # type=result by CodexProvider.normalize_event). These
                # are the only events that carry real usage counts; the
                # data exists in the CLI stream for ~2ms today and is
                # otherwise dropped on the floor. Persist it to cerver
                # session metadata so /cerver compare can print actual
                # tokens + cost instead of the chars/4 estimate.
                self._extract_and_push_usage(agent, event)

            except Exception as e:
                print(f"[LocalAgent] Read error: {e}")
                break

        # Read loop ended (EOF, exception, or watchdog terminate). Stop
        # the watchdog before it possibly fires a second SIGTERM on an
        # already-exiting process.
        watchdog_task.cancel()

        if agent.process:
            agent.exit_code = agent.process.wait()

        # Belt-and-suspenders: when the read loop exits, flush a final
        # transcript entry to cerver. Three cases:
        #
        #   1. Happy path — exit_code == 0 AND we have assistant text.
        #      Push the text as an assistant entry. Same as before.
        #   2. Hard failure — exit_code != 0 (CLI crashed / API rejected).
        #      Push a `[cli_exit code=N]` diagnostic, with any partial
        #      response appended and the last 500B of raw stream output
        #      (which is where stderr lines and non-JSON noise live).
        #   3. Silent failure — exit_code == 0 but no assistant text was
        #      produced (CLI completed without ever emitting an
        #      agent_message). Same diagnostic shape as (2).
        #
        # In true failure cases the diagnostic is pushed as a role=assistant
        # entry so the cerver CLI's WaitForReply terminates immediately with
        # a visible error. Codex emits an empty `turn.completed`/result event;
        # extract_result_from_output_buffer intentionally ignores that empty
        # result and falls back to prior assistant text so we don't append a
        # bogus "[cli_exit] ... no assistant message" after a valid answer.
        try:
            final_text = self._extract_result(agent)
            exit_failed = bool(agent.exit_code and agent.exit_code != 0)

            if final_text and not exit_failed:
                self._post_transcript_entries(
                    agent,
                    [{"role": "assistant", "kind": "text", "content": final_text}],
                )
            else:
                # Collect the last ~500 bytes of raw (non-structured) output
                # — that's where stderr lines and CLI error messages live in
                # the merged stdout/stderr stream after `is_noise` filtering.
                raw_chunks = []
                total = 0
                for item in reversed(agent.output_buffer):
                    if not isinstance(item, dict):
                        continue
                    # Skip events that already became structured transcript
                    # entries — we want the unstructured residue.
                    if item.get("parsed"):
                        continue
                    data = item.get("data")
                    if isinstance(data, str) and data.strip():
                        raw_chunks.append(data)
                        total += len(data)
                        if total > 800:
                            break
                raw_tail = "\n".join(reversed(raw_chunks))[-500:].strip()

                diag_parts = [f"[cli_exit] cli={provider.name} code={agent.exit_code}"]
                if agent.watchdog_killed:
                    diag_parts.append(
                        f"terminated by watchdog after {self.STALL_TIMEOUT_SEC}s of silence"
                    )
                if exit_failed:
                    diag_parts.append("process exited with non-zero status")
                if not final_text:
                    diag_parts.append("no assistant message was produced")
                else:
                    diag_parts.append("partial response received:")
                    diag_parts.append(final_text)
                if raw_tail:
                    diag_parts.append(f"last raw output:\n{raw_tail}")

                self._post_transcript_entries(
                    agent,
                    [{
                        "role": "assistant",
                        "kind": "text",
                        "content": "\n\n".join(diag_parts),
                    }],
                )
        except Exception as exc:
            print(f"[LocalAgent] final flush to cerver failed: {exc}")

        # One-shot agents (cron / run-agent) complete on exit and clear their
        # session so they don't linger in the pool. Interactive agents (chat
        # via /agents) pause instead so the user's next message resumes them.
        # The signal is `complete_on_exit`, not callback presence — chats now
        # also carry callbacks (for stream publishing) but must NOT complete.
        if agent.complete_on_exit:
            agent.status = "completed" if agent.exit_code == 0 else "failed"
            agent.session_id = None  # Don't keep session — allows cleanup
            print(f"[LocalAgent] One-shot agent {agent.id} {agent.status} (exit={agent.exit_code})")

            exit_event = {"type": "exit", "exit_code": agent.exit_code}
            await broadcast_to_agent_listeners(agent, exit_event)
            self._publish_stream_to_cerver(agent, exit_event)

            await self._fire_callback(agent)

            # Immediately remove the agent from the pool. Previously this
            # waited for the timer-based cleanup_stale_agents pass (which
            # only fires at the start of the next create()), so 3 parallel
            # `cerver compare` runs (9 spawns) would pile up before any
            # cleanup ran — and at MAX_AGENTS=10 the next create() 500'd
            # with "Maximum number of agents (10) reached" before its own
            # cleanup pass could free the slot. One-shot agents have no
            # reason to linger after exit; their session_id is already
            # nulled above.
            #
            # BUT — before popping, snapshot the agent into the recent-
            # history buffer so the Runtime tab in the relay TUI can show
            # "you just ran codex 12s ago" instead of "0 agents". The
            # buffer is observation-only, separate from `_agents`, so the
            # MAX_AGENTS slot is still freed immediately for the next
            # provisioning request.
            self._recent_completed.append({
                "id": agent.id,
                "task_id": agent.task_id,
                "task_number": agent.task_number,
                "task_title": agent.task_title,
                "status": agent.status,
                "type": "local",
                "cli_tool": agent.cli_tool,
                "branch": agent.branch,
                "worktree_path": agent.worktree_path,
                "created_at": agent.created_at.isoformat(),
                "last_activity": datetime.now().isoformat(),
                "session_id": None,
                "can_resume": False,
                "_evicted_at": datetime.now(),
            })
            self._agents.pop(agent.id, None)
            output_task = self._output_tasks.pop(agent.id, None)
            if output_task is not None and not output_task.done():
                output_task.cancel()
        elif agent.session_id:
            agent.status = "paused"
            print(f"[LocalAgent] Agent {agent.id} paused, session can be resumed")

            paused_event = {
                "type": "paused",
                "exit_code": agent.exit_code,
                "session_id": agent.session_id,
                "can_resume": True,
            }
            await broadcast_to_agent_listeners(agent, paused_event)
            self._publish_stream_to_cerver(agent, paused_event)
            asyncio.create_task(
                self._push_cerver_status(
                    agent,
                    "idle" if agent.exit_code == 0 else "failed",
                    f"agent paused after exit={agent.exit_code}",
                )
            )
        else:
            agent.status = "completed" if agent.exit_code == 0 else "failed"

            exit_event = {"type": "exit", "exit_code": agent.exit_code}
            await broadcast_to_agent_listeners(agent, exit_event)
            self._publish_stream_to_cerver(agent, exit_event)

    def _extract_result(self, agent: LocalAgent) -> str:
        """Extract the final result text from the agent's output buffer.

        Looks for the 'result' type message in the stream-json output.
        Falls back to collecting assistant message text content.
        """
        return extract_result_from_output_buffer(agent.output_buffer)

    def _event_to_cerver_entries(self, event: Dict) -> list:
        """Convert a normalized agent stream event into cerver
        SessionTranscriptEntry objects (one per content block).

        process_provider_output_text wraps every line as
        {"type": "output", "data": <inner-json-string>, "raw": <line>},
        so we have to parse the inner event before mapping. Inner events
        follow Claude Code's stream-json shape: assistant/user/result/system.
        """
        # Unwrap the outer "output" envelope to get the actual stream event.
        if event.get("type") == "output" and isinstance(event.get("data"), str):
            try:
                inner = json.loads(event["data"])
            except (json.JSONDecodeError, TypeError):
                return []
        else:
            inner = event

        etype = (inner or {}).get("type")
        entries = []
        if etype == "assistant":
            blocks = ((inner.get("message") or {}).get("content")) or []
            for b in blocks:
                btype = b.get("type")
                if btype == "text":
                    entries.append({"role": "assistant", "kind": "text", "content": b.get("text", "")})
                elif btype == "tool_use":
                    entries.append({
                        "role": "assistant",
                        "kind": "tool_use",
                        "content": "",
                        "tool_id": b.get("id"),
                        "tool_name": b.get("name"),
                        "tool_input": b.get("input"),
                    })
        elif etype == "user":
            blocks = ((inner.get("message") or {}).get("content")) or []
            for b in blocks:
                if b.get("type") == "tool_result":
                    raw = b.get("content")
                    if isinstance(raw, list):
                        text = "".join(c.get("text", "") for c in raw if isinstance(c, dict) and c.get("type") == "text")
                    else:
                        text = str(raw or "")
                    entries.append({
                        "role": "tool",
                        "kind": "tool_result",
                        "content": text,
                        "tool_id": b.get("tool_use_id"),
                        "is_error": bool(b.get("is_error")),
                    })
                elif b.get("type") == "text":
                    entries.append({"role": "user", "kind": "text", "content": b.get("text", "")})
        # NOTE: `result` events are intentionally NOT mapped to transcript
        # entries here. Claude CLI emits both an `assistant` event (with the
        # text in a content block) and a `result` event (with the same text
        # as `result.result`) — mapping both produced visible duplicates on
        # cerver. The streaming `assistant` event already covers the text.
        # The post-loop final flush (in the read loop) is now also redundant
        # but harmless because the signature dedup in _push_event_to_cerver
        # would skip it anyway.
        return entries

    @staticmethod
    def _entry_signature(entry: dict) -> str:
        """Stable signature for transcript-entry dedup.

        Composed of (role, kind, tool_id, sha1(content)). Two events that
        produce the same logical transcript entry — e.g. a streaming
        `assistant` event and the matching `result` event with identical
        text — collapse to the same signature and the second one is
        skipped, even though they share no message.id.
        """
        role = entry.get("role") or ""
        kind = entry.get("kind") or ""
        tool_id = entry.get("tool_id") or ""
        # tool_use entries have empty content but unique tool_input — fold
        # tool_input shape into the signature so two distinct tool_use
        # events with the same tool_id but different inputs don't collide.
        content = entry.get("content") or ""
        if kind == "tool_use":
            try:
                content = json.dumps(entry.get("tool_input") or {}, sort_keys=True)
            except (TypeError, ValueError):
                content = str(entry.get("tool_input") or "")
        digest = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()
        return f"{role}|{kind}|{tool_id}|{digest}"

    async def _resolve_cerver_target(
        self,
        agent: LocalAgent,
        wait_for_transport: bool = True,
        timeout_seconds: float = 3.0,
    ) -> Optional[Dict[str, str]]:
        """Resolve the cerver_url + token + session_id for transcript pushes.

        Falls back to the active connect transport when the callback only
        carries cerver_session_id (the cerver_compute provider path). When
        the transport hasn't finished registering yet — typical for the
        very first push right after agent.create() — block briefly with
        polled retries instead of silently dropping the entry. Counts each
        wait via _push_stats so health is observable.

        Returns None if we genuinely can't resolve a target (no callback,
        or transport never came up). The caller must handle that.
        """
        callback = agent.callback or {}
        cerver_url = callback.get("cerver_url")
        cerver_token = callback.get("cerver_api_token")
        cerver_session_id = callback.get("cerver_session_id")
        if not cerver_session_id:
            return None
        if cerver_url and cerver_token:
            return {"url": cerver_url, "token": cerver_token, "session_id": cerver_session_id}

        # Need to fall back to transport. Poll until ready or timeout.
        deadline = asyncio.get_event_loop().time() + max(0.0, timeout_seconds)
        first_wait = True
        while True:
            transport = get_active_transport()
            if transport is not None and transport.cerver_url and transport.api_token:
                if not first_wait:
                    agent._push_stats["transport_waits"] += 1
                return {
                    "url": cerver_url or transport.cerver_url,
                    "token": cerver_token or transport.api_token,
                    "session_id": cerver_session_id,
                }
            if not wait_for_transport or asyncio.get_event_loop().time() >= deadline:
                return None
            first_wait = False
            await asyncio.sleep(0.1)

    def recover_agent(
        self,
        agent_id: str,
        *,
        cli_session_id: Optional[str],
        cli_tool: Optional[str],
        working_dir: Optional[str],
        cerver_session_id: Optional[str],
    ) -> Optional[dict]:
        """Rebuild a relay-side agent record from cerver-forwarded hints.

        Used by `send_input` when the local agent_id isn't in `_agents`
        (relay restart, stale cleanup, etc.). We register a "paused"
        agent with the cerver-supplied resume id on `agent.session_id`
        so the very next `resume_session()` call uses
        `provider.build_resume_command(prompt, cli_session_id)` — the
        CLI's own resume mechanism (provider-agnostic; works for claude
        `--resume`, codex `exec resume`, grok `--resume`, etc.).

        Returns the new agent's dict shape on success, None when we
        can't recover (no cli_session_id → no resume hint, caller should
        fall back to fresh-spawn).
        """
        if agent_id in self._agents:
            return None  # already alive — nothing to recover

        if not cli_session_id:
            # No resume id captured server-side. Could still spawn fresh
            # against the user's input but we'd lose conversation history
            # entirely — caller decides whether that's acceptable.
            return None

        resolved_dir = working_dir or os.path.expanduser("~")
        agent = LocalAgent(
            id=agent_id,
            task_id=None,
            task_number=None,
            task_title="recovered",
            task_description=None,
            repo_dir=resolved_dir,
            work_dir=resolved_dir,
            worktree_path=None,
            branch=None,
            branch_created=False,
            status="paused",  # resume_session uses --resume when status in (paused, completed, failed)
            cli_tool=(cli_tool or "claude"),
            callback=(
                {"cerver_session_id": cerver_session_id}
                if cerver_session_id
                else None
            ),
            session_id=cli_session_id,
        )
        self._agents[agent_id] = agent
        print(
            f"[LocalAgent] Recovered agent {agent_id} "
            f"(cli_tool={agent.cli_tool}, cli_session_id={cli_session_id[:8]}…, dir={resolved_dir})"
        )
        return {
            "id": agent_id,
            "task_title": "recovered",
            "status": "paused",
            "cli_tool": agent.cli_tool,
            "session_id": cli_session_id,
            "work_dir": resolved_dir,
            "type": "local",
            "can_resume": True,
        }

    async def _push_cli_state_to_cerver(self, agent: LocalAgent) -> None:
        """PATCH the cerver session's metadata with the CLI's resume id,
        the CLI tool, and the working dir. These are the three pieces
        the relay needs to recover the agent if its in-memory record is
        gone — cerver hands them back on every /input forward.

        Best effort. A failure here doesn't break the run; transcript
        pushes still carry the conversation. Worst case is a future
        recovery can't use --resume and has to fall back to fresh-spawn
        (still works, just loses CLI-internal todos/tool history).

        Provider-agnostic: `cli_session_id` is an opaque string the
        relevant CLI provider knows how to consume (claude --resume,
        codex exec resume, grok --resume).
        """
        # Wait briefly for the cerver-connect transport to be ready. The
        # init event that triggers this push often fires within the first
        # ~1s of an agent's life, which can race transport registration.
        # Previously we used wait_for_transport=False with a 0s deadline,
        # so a transport not-yet-ready meant the push silently dropped
        # and cerver permanently lacked the recovery hint.
        target = await self._resolve_cerver_target(
            agent, wait_for_transport=True, timeout_seconds=5.0
        )
        if target is None or not agent.session_id:
            return
        url = f"{target['url'].rstrip('/')}/v2/sessions/{target['session_id']}"
        body = {
            "metadata": {
                "cli_session_id": agent.session_id,
                "cli_tool": agent.cli_tool,
                "working_dir": agent.work_dir,
            }
        }

        # Retry on 404 / 5xx / network errors — same shape as
        # _post_transcript_entries. Without a successful PATCH, the
        # session can never be recovered after a relay restart, so a
        # transient failure here is much more costly than for transcript
        # entries (which retry inside cerver too).
        backoffs = [0.2, 0.4, 0.8, 1.5, 2.5]  # ~5.4s total
        attempts = 0
        last_status: Optional[int] = None
        last_exc: Optional[Exception] = None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                while True:
                    attempts += 1
                    try:
                        resp = await client.patch(
                            url,
                            json=body,
                            headers={
                                "Authorization": f"Bearer {target['token']}",
                                "Content-Type": "application/json",
                            },
                        )
                        last_status = resp.status_code
                        if resp.status_code < 300:
                            return
                        retryable = resp.status_code == 404 or resp.status_code >= 500
                    except (httpx.RequestError, httpx.TimeoutException) as exc:
                        last_exc = exc
                        retryable = True
                    if not retryable or attempts > len(backoffs):
                        break
                    await asyncio.sleep(backoffs[attempts - 1])
        except Exception as exc:
            last_exc = exc

        if last_exc is not None:
            print(
                f"[LocalAgent] cli-state PATCH failed after {attempts} attempts: "
                f"{type(last_exc).__name__}: {last_exc}"
            )
        else:
            print(
                f"[LocalAgent] cli-state PATCH failed after {attempts} attempts: "
                f"HTTP {last_status}"
            )

    def _extract_and_push_usage(self, agent: LocalAgent, event: Dict) -> None:
        """Pull token-usage off a `result`-shaped event and PATCH it to the
        cerver session's metadata. Both Claude Code (`type=result`) and
        Codex (`type=turn.completed`, normalized to `type=result` by
        CodexProvider.normalize_event) emit this once per turn. The
        underlying field names differ — claude uses cache_creation /
        cache_read, codex uses cached_input / reasoning_output — so we
        keep the raw object and let the consumer interpret.

        Accumulates across turns on `agent.usage_cumulative` so an
        interactive session reports total spend, not just the last turn.
        Fire-and-forget; the run keeps moving if cerver is slow.
        """
        # Unwrap process_provider_output_text's `{type:output, data:json}`
        # envelope to get the actual stream event.
        inner = event
        if event.get("type") == "output" and isinstance(event.get("data"), str):
            try:
                inner = json.loads(event["data"])
            except (json.JSONDecodeError, TypeError):
                return
        if not isinstance(inner, dict) or inner.get("type") != "result":
            return
        usage = inner.get("usage")
        if not isinstance(usage, dict) or not usage:
            return

        # Accumulate input + output across turns. Vendor-specific fields
        # (cache_*, reasoning_*) we pass through from the latest turn only;
        # summing them cleanly would require knowing each field's semantics.
        if not hasattr(agent, "usage_cumulative") or agent.usage_cumulative is None:
            agent.usage_cumulative = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
        agent.usage_cumulative["input_tokens"] += int(usage.get("input_tokens") or 0)
        agent.usage_cumulative["output_tokens"] += int(usage.get("output_tokens") or 0)
        agent.usage_cumulative["turns"] += 1

        body_metadata = {
            "usage_total": agent.usage_cumulative,
            "usage_last": usage,
            "usage_cli": agent.cli_tool,
        }
        # Record the actual model the CLI ran. Surfacing it in
        # metadata.cli_model lets cerver sessions / cerver show display
        # "what actually ran" instead of forcing the user to grep
        # transcripts. The user's requested model (from session-create
        # metadata.cli_model) gets clobbered here on purpose — observed
        # beats requested when they disagree.
        #
        # Sources differ by CLI:
        # - Claude Code's `result` event has no top-level `model` field;
        #   the model is the *key* inside `modelUsage` (e.g.
        #   "claude-haiku-4-5-20251001"). Multiple keys appear if a
        #   sub-agent ran a different model, but the agent's own model
        #   shows up as the largest-token entry.
        # - Codex's result (after CodexProvider.normalize_event) has a
        #   top-level `model` string.
        # - Grok proxies through claude, so it follows claude's shape.
        observed_model = inner.get("model")
        if not (isinstance(observed_model, str) and observed_model):
            model_usage = inner.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage:
                # Pick the entry with the most output tokens — that's the
                # main agent. Sub-agent spawns (e.g. Task tool) show up
                # smaller. Falls back to first key if no usage info.
                best_key = None
                best_out = -1
                for k, v in model_usage.items():
                    if not isinstance(v, dict):
                        continue
                    out = v.get("outputTokens") or v.get("output_tokens") or 0
                    if out > best_out:
                        best_out = out
                        best_key = k
                observed_model = best_key or next(iter(model_usage.keys()))
        if isinstance(observed_model, str) and observed_model:
            body_metadata["cli_model"] = observed_model
            body_metadata["cli_model_provider"] = _model_provider_for_cli(agent.cli_tool)
        body = {"metadata": body_metadata}
        asyncio.create_task(self._patch_session_metadata(agent, body))

    async def _patch_session_metadata(self, agent: LocalAgent, body: Dict) -> None:
        """Fire-and-forget PATCH to /v2/sessions/<id> with arbitrary
        metadata merge. Shared by the usage push and any other small
        per-turn updates that don't warrant their own endpoint. Same
        retry budget as transcript pushes."""
        target = await self._resolve_cerver_target(
            agent, wait_for_transport=True, timeout_seconds=5.0
        )
        if target is None:
            return
        url = f"{target['url'].rstrip('/')}/v2/sessions/{target['session_id']}"
        backoffs = [0.2, 0.4, 0.8, 1.5, 2.5]
        attempts = 0
        last_status = None
        last_exc = None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                while True:
                    attempts += 1
                    try:
                        resp = await client.patch(
                            url, json=body,
                            headers={
                                "Authorization": f"Bearer {target['token']}",
                                "Content-Type": "application/json",
                            },
                        )
                        last_status = resp.status_code
                        if resp.status_code < 300:
                            return
                        retryable = resp.status_code == 404 or resp.status_code >= 500
                    except (httpx.RequestError, httpx.TimeoutException) as exc:
                        last_exc = exc
                        retryable = True
                    if not retryable or attempts > len(backoffs):
                        break
                    await asyncio.sleep(backoffs[attempts - 1])
        except Exception as exc:
            last_exc = exc

        if last_exc is not None:
            print(
                f"[LocalAgent] metadata PATCH failed after {attempts}x: "
                f"{type(last_exc).__name__}: {last_exc}"
            )
        elif last_status is not None and last_status >= 300:
            print(
                f"[LocalAgent] metadata PATCH failed after {attempts}x: "
                f"HTTP {last_status}"
            )

    def _post_transcript_entries(self, agent: LocalAgent, entries: list) -> None:
        """Fire-and-forget POST of transcript entries to cerver.

        Signature dedup runs HERE (not just in _push_event_to_cerver) so
        every caller benefits — including _push_user_message and the
        post-loop final flush, which previously bypassed dedup and
        produced duplicates on cerver. Entries with already-seen
        signatures are silently skipped; counters reflect the skip.
        """
        if not entries:
            return

        new_entries = []
        for entry in entries:
            # Mirror cerver's rule (sessions/service.ts:334) — every entry
            # needs non-empty content unless kind=="tool_use" (those carry
            # their payload in tool_input/tool_name). Claude occasionally
            # emits empty text blocks (esp. on the very first event of a
            # fresh session); pushing them gets a 400 that doesn't retry
            # and pollutes the log without losing any real content.
            if not entry.get("content") and entry.get("kind") != "tool_use":
                agent._push_stats["empty_skipped"] = agent._push_stats.get("empty_skipped", 0) + 1
                continue
            sig = self._entry_signature(entry)
            if sig in agent._pushed_signatures:
                agent._push_stats["dedup_skipped"] += 1
                continue
            agent._pushed_signatures.add(sig)
            new_entries.append(entry)
        if not new_entries:
            return
        entries = new_entries

        async def _push():
            target = await self._resolve_cerver_target(agent)
            if target is None:
                # Last-resort drop. Log once per agent so future runs surface
                # the actual cause (missing cerver fields or transport never
                # came up) instead of a mysteriously empty transcript.
                agent._push_stats["drops"] += len(entries)
                if not getattr(agent, "_logged_skip", False):
                    callback = agent.callback or {}
                    missing = [
                        name for name, val in (
                            ("cerver_url", callback.get("cerver_url")),
                            ("cerver_api_token", callback.get("cerver_api_token")),
                            ("cerver_session_id", callback.get("cerver_session_id")),
                            ("active_transport", get_active_transport() is not None),
                        ) if not val
                    ]
                    print(
                        f"[LocalAgent] transcript push dropped for agent={agent.id} "
                        f"task={agent.task_title}: missing {missing}"
                    )
                    agent._logged_skip = True
                return

            import httpx
            agent._push_stats["pushed"] += len(entries)
            url = f"{target['url'].rstrip('/')}/v2/sessions/{target['session_id']}/transcript"
            agent._last_push_url = url

            # Retry budget for 404 / 5xx / network errors. Concretely
            # protects against the v319-confirmed race where
            # _push_user_message fires inside the cerver_compute provider
            # callback while cerver's session-creation write hasn't yet
            # committed in Postgres. Cerver becomes consistent within ~1-2s.
            # 404s on this endpoint are ALWAYS retryable: either the
            # session truly doesn't exist (in which case all pushes will
            # 404 forever and we give up after the budget) or it does and
            # the read replica caught up.
            backoffs = [0.2, 0.4, 0.8, 1.5, 2.5]  # ~5.4s total
            attempts = 0
            last_status: Optional[int] = None
            last_body = ""
            last_exc: Optional[Exception] = None
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    while True:
                        attempts += 1
                        try:
                            resp = await client.post(
                                url,
                                json={"entries": entries},
                                headers={
                                    "Authorization": f"Bearer {target['token']}",
                                    "Content-Type": "application/json",
                                },
                            )
                            last_status = resp.status_code
                            last_body = resp.text[:300] if resp.text else ""
                            if resp.status_code < 300:
                                if attempts > 1:
                                    agent._push_stats["http_retried_ok"] = (
                                        agent._push_stats.get("http_retried_ok", 0) + 1
                                    )
                                agent._push_stats["http_2xx"] += len(entries)
                                return
                            # Retry only on 404 / 5xx. 401/403/4xx-other
                            # are auth/shape problems that won't fix
                            # themselves with time.
                            retryable = (
                                resp.status_code == 404 or resp.status_code >= 500
                            )
                        except (httpx.RequestError, httpx.TimeoutException) as exc:
                            last_exc = exc
                            retryable = True
                        if not retryable or attempts > len(backoffs):
                            break
                        await asyncio.sleep(backoffs[attempts - 1])
            except Exception as exc:
                last_exc = exc

            # Out of budget. Categorise the final outcome.
            if last_exc is not None:
                agent._push_stats["http_exc"] += len(entries)
                err = f"exc({attempts}x) {url}: {type(last_exc).__name__}: {last_exc}"
            elif last_status is not None and last_status < 500:
                agent._push_stats["http_4xx"] += len(entries)
                err = f"{last_status}({attempts}x) {url}: {last_body}"
            else:
                agent._push_stats["http_5xx"] += len(entries)
                err = f"{last_status}({attempts}x) {url}: {last_body}"
            agent._push_errors.append(err)
            agent._push_errors[:] = agent._push_errors[-5:]
            print(f"[LocalAgent] transcript push {err}")

        try:
            asyncio.create_task(_push())
        except Exception:
            pass

    def _push_event_to_cerver(self, agent: LocalAgent, event: Dict) -> None:
        """Push one CLI stream event to cerver as transcript entries.

        Signature dedup lives in _post_transcript_entries so every push
        path (this one, _push_user_message, the final flush) gets the
        same protection from result-vs-assistant duplicates.
        """
        self._post_transcript_entries(agent, self._event_to_cerver_entries(event))

    def _extract_message_id(self, event: Dict) -> Optional[str]:
        """Pull `message.id` out of a CLI stream event, unwrapping the outer
        `{type: "output", data: <json>}` envelope when present.
        """
        try:
            inner = event
            if event.get("type") == "output" and isinstance(event.get("data"), str):
                inner = json.loads(event["data"])
            if not isinstance(inner, dict):
                return None
            msg = inner.get("message")
            if isinstance(msg, dict):
                mid = msg.get("id")
                if isinstance(mid, str) and mid:
                    return mid
            return None
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None

    def _publish_stream_to_cerver(self, agent: LocalAgent, event: Dict) -> None:
        """Push one CLI stream event over the live cerver-connect WebSocket.

        Cerver fans the event out to every subscriber of this session's
        /v2/sessions/<id>/stream/ws — i.e. all open browser tabs and CLI
        clients. This is the live-streaming side; the durable copy is the
        HTTP transcript push in _push_event_to_cerver.
        """
        callback = agent.callback or {}
        cerver_session_id = callback.get("cerver_session_id")
        if not cerver_session_id:
            return
        publish_stream_event_nowait(cerver_session_id, event)

    def _push_user_message(self, agent: LocalAgent, content: str) -> None:
        """Push a user-side message (initial prompt or follow-up input) so the
        cerver transcript captures the full conversation, not just assistant
        output. Without this, cron / workflow sessions start at the agent's
        first response with no context for what was asked.
        """
        if not content:
            return
        self._post_transcript_entries(
            agent, [{"role": "user", "kind": "text", "content": content}]
        )

    async def _push_cerver_status(self, agent: LocalAgent, status: str, end_reason: str) -> None:
        """Push a lifecycle status to the linked cerver session.

        Callback config is expected to include cerver_url + cerver_api_token
        + cerver_session_id. Transcript entries are pushed separately; this
        method is only the run-state edge (`running` -> `idle/completed/failed`).
        """
        import httpx

        callback = agent.callback
        if not callback:
            return

        cerver_url = callback.get("cerver_url")
        cerver_token = callback.get("cerver_api_token")
        cerver_session_id = callback.get("cerver_session_id")
        if not (cerver_url and cerver_token and cerver_session_id):
            print(f"[LocalAgent] _push_cerver_status: missing cerver fields for {agent.task_title}; skipping")
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                status_resp = await client.post(
                    f"{cerver_url.rstrip('/')}/v2/sessions/{cerver_session_id}/status",
                    json={"status": status, "end_reason": end_reason},
                    headers={
                        "Authorization": f"Bearer {cerver_token}",
                        "Content-Type": "application/json",
                    },
                )
                print(f"[LocalAgent] cerver status for {agent.task_title}: {status_resp.status_code}")
        except Exception as e:
            print(f"[LocalAgent] cerver status push failed for {agent.task_title}: {e}")

    async def _fire_callback(self, agent: LocalAgent) -> None:
        """Push transcript + status to cerver for cron-triggered agents.

        Callback config is expected to include cerver_url + cerver_api_token
        + cerver_session_id (Kompany sets these when scheduling the run).
        Falls back to nothing — no more Kompany /api/crons/callback hop.
        """
        status = agent.status
        cerver_status = "completed" if status in ("completed", "paused") else "failed"
        await self._push_cerver_status(agent, cerver_status, f"agent {status}")

    async def _run_with_resume(self, agent: LocalAgent, message: str, image_paths: List[str] = None) -> None:
        """Run a follow-up message using session resume.

        Args:
            agent: The agent to resume
            message: The follow-up message
            image_paths: Optional list of image file paths (already included in message text)
        """
        if not agent.session_id:
            return

        provider = self._get_provider(agent)
        cli_cmd = build_resume_cli_command(provider, message, agent.session_id)

        if image_paths:
            print(f"[LocalAgent] Message includes {len(image_paths)} image paths for CLI to read")

        print(f"[LocalAgent] Resuming session {agent.session_id} with {provider.display_name}")

        process = spawn_cli_subprocess(cli_cmd, agent.work_dir, extra_env=agent.extra_env)

        agent.process = process
        agent.pid = process.pid
        agent.status = "running"

        await self._read_json_output(agent)

    def get(self, agent_id: str) -> Optional[dict]:
        """Get agent info by ID."""
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        return {
            "id": agent.id,
            "task_id": agent.task_id,
            "task_number": agent.task_number,
            "task_title": agent.task_title,
            "status": agent.status,
            "type": "local",
            "cli_tool": agent.cli_tool,
            "work_dir": agent.work_dir,
            "worktree_path": agent.worktree_path,
            "branch": agent.branch,
            "branch_created": agent.branch_created,
            "is_worktree": agent.worktree_path is not None,
            "created_at": agent.created_at.isoformat(),
            "last_activity": agent.last_activity.isoformat(),
            "exit_code": agent.exit_code,
            "session_id": agent.session_id,
            "can_resume": agent.session_id is not None,
            # Push pipeline counters — zero values are fine, the keys are
            # always present so callers (and tests) don't have to special-case
            # newly-created agents.
            "transcript_push": dict(agent._push_stats),
            "transcript_signature_count": len(agent._pushed_signatures),
            "transcript_last_url": agent._last_push_url,
            "transcript_recent_errors": list(agent._push_errors),
        }

    def list(self) -> List[dict]:
        """List all agents — active (running/paused/prepared) plus recently
        completed/failed one-shots that haven't aged out of the TTL window.
        The TUI's Runtime tab shows both so a relay handling lots of short
        cerver-run calls doesn't look idle between bursts.
        """
        active = [
            {
                "id": a.id,
                "task_id": a.task_id,
                "task_number": a.task_number,
                "task_title": a.task_title,
                "status": a.status,
                "type": "local",
                "cli_tool": a.cli_tool,
                "branch": a.branch,
                "worktree_path": a.worktree_path,
                "created_at": a.created_at.isoformat(),
                "last_activity": a.last_activity.isoformat(),
                "session_id": a.session_id,
                "can_resume": a.session_id is not None
            }
            for a in self._agents.values()
        ]
        # Trim aged-out entries from the recent-completed buffer. Done
        # lazily on read instead of via a background task — the buffer
        # is bounded anyway (RECENT_HISTORY_MAX) so unbounded growth
        # isn't a risk; this just keeps the rendered list short.
        now = datetime.now()
        ttl_cutoff = now - timedelta(seconds=self.RECENT_HISTORY_TTL_SECONDS)
        while self._recent_completed and self._recent_completed[0].get("_evicted_at") < ttl_cutoff:
            self._recent_completed.popleft()
        # Strip the internal `_evicted_at` field before returning so the
        # TUI / stats consumers see the same shape they did before this
        # change — RECENT_HISTORY is an internal mechanism, not a new
        # API surface.
        recent = [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in self._recent_completed]
        return active + recent

    async def resume_session(self, agent_id: str, message: str, image_paths: List[str] = None, pre_logged: bool = False) -> bool:
        """Resume an agent session with a follow-up message.

        Args:
            agent_id: The agent to resume
            message: The follow-up message (may already contain image references)
            image_paths: Optional list of image file paths to include
            pre_logged: If True, skip pushing the user message to cerver's
                transcript — the gateway already wrote it via recordInput
                before forwarding the input to this relay. Without this gate
                we end up with two identical user entries ~700ms apart.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        if not agent.session_id:
            raise HTTPException(
                status_code=400,
                detail="No session ID available. Cannot resume session."
            )

        if agent.status == "running":
            raise HTTPException(
                status_code=400,
                detail="Agent is already running. Wait for it to complete."
            )

        if image_paths:
            print(f"[LocalAgent] Resuming with {len(image_paths)} images: {image_paths}")

        # Push the follow-up user message before the resumed agent starts
        # streaming its response, so the cerver transcript reads in order —
        # unless the gateway already wrote it (pre_logged=True), in which
        # case skip to avoid the double-entry bug.
        if not pre_logged:
            self._push_user_message(agent, message)

        try:
            if agent_id in self._output_tasks:
                self._output_tasks[agent_id].cancel()

            self._output_tasks[agent_id] = asyncio.create_task(
                self._run_with_resume(agent, message, image_paths)
            )

            return True

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to resume session: {str(e)}")

    def kill(self, agent_id: str, cleanup_worktree: bool = False) -> None:
        """Kill an agent and optionally cleanup worktree."""
        agent = self._agents.get(agent_id)
        if not agent:
            return

        print(f"[LocalAgent] Killing agent {agent_id}")

        # Terminate the child process FIRST. _read_json_output offloads
        # stdout.readline() to the default ThreadPoolExecutor; that worker
        # thread holds Python's BufferedReader lock for the duration of
        # the read. If we close stdout while a readline() is in flight,
        # close() blocks on the same lock and deadlocks the whole asyncio
        # loop — which on the uvicorn thread also stops the local HTTP
        # server from accepting new connections. Terminating the child
        # closes the pipe from the writer side, so readline() returns
        # b'' and the worker releases the lock; close() below is then
        # instant.
        if agent.process:
            try:
                agent.process.terminate()
                try:
                    agent.process.wait(timeout=2)
                except Exception:
                    agent.process.kill()
                    agent.process.wait(timeout=1)
            except Exception:
                pass
        elif agent.pid:
            try:
                os.kill(agent.pid, signal.SIGTERM)
                try:
                    os.waitpid(agent.pid, os.WNOHANG)
                except Exception:
                    pass
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(agent.pid, signal.SIGKILL)
                except Exception:
                    pass

        # Cancel the asyncio Task. Note: task.cancel() does NOT cancel
        # the executor thread running read_line — but the child is dead
        # now, so that thread is already unwinding on its own.
        if agent_id in self._output_tasks:
            self._output_tasks[agent_id].cancel()
            del self._output_tasks[agent_id]

        # Close stdout pipe to release the file descriptor. Safe now
        # that the reader has stopped.
        if agent.process and agent.process.stdout:
            try:
                agent.process.stdout.close()
            except Exception:
                pass

        agent.status = "stopped"

        if cleanup_worktree and agent.worktree_path and agent.repo_dir:
            remove_worktree(agent.repo_dir, agent.worktree_path)

        del self._agents[agent_id]
        print(f"[LocalAgent] Agent {agent_id} killed, {len(self._agents)} agents remaining")

    def add_listener(self, agent_id: str) -> asyncio.Queue:
        """Add an output listener for streaming."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        queue = asyncio.Queue()

        for item in agent.output_buffer:
            if isinstance(item, dict):
                queue.put_nowait({"type": "output", **item})
            else:
                queue.put_nowait({"type": "output", "data": item})

        agent.output_listeners.append(queue)
        return queue

    def remove_listener(self, agent_id: str, queue: asyncio.Queue) -> None:
        """Remove an output listener."""
        agent = self._agents.get(agent_id)
        if agent and queue in agent.output_listeners:
            agent.output_listeners.remove(queue)

    def get_output(self, agent_id: str) -> str:
        """Get full output buffer."""
        agent = self._agents.get(agent_id)
        if not agent:
            return ""
        parts = []
        for item in agent.output_buffer:
            if isinstance(item, dict):
                parts.append(item.get("data", ""))
            else:
                parts.append(item)
        return "".join(parts)


# Singleton instance
agent_manager = LocalAgentManager()
