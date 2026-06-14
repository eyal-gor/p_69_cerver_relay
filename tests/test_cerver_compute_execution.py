"""
Unit tests for the Cerver-facing execution helpers.

These helpers are the per-sandbox connection layer: they route a message to a
local agent based on its lifecycle, drain the agent's event stream into a single
result, and shape the terminate/state responses the gateway expects. The
reliability behaviours covered here:

  - Lifecycle dispatch (`send_provider_input`): prepared -> start, a resumable
    session -> resume, a running agent -> HTTP 400 (don't double-drive it), and
    unknown / non-resumable -> the right 404 / 400 so a flaky client gets a
    clear error instead of a hang.
  - Run collection (`collect_provider_run`): clean exit, mid-run error, pause,
    and — critically for connection reliability — a stalled stream surfacing as
    HTTP 408 rather than blocking forever. The event listener is always removed
    in `finally`, so a failed run never leaks a subscription.
  - Stream-text extraction: the assistant / tool_result / result event shapes,
    and the malformed-JSON fallback that keeps output from being dropped.

All tests are offline and drive a small in-memory fake agent manager. Async
helpers are exercised with ``asyncio.run`` (no pytest-asyncio dependency).

Run with: uv run --with pytest python -m pytest tests/test_cerver_compute_execution.py
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

# Make the package importable when running from repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from branch_monkey_mcp.cerver_compute import execution as ex  # noqa: E402
from branch_monkey_mcp.cerver_compute.execution import (  # noqa: E402
    collect_provider_run,
    delete_provider_session,
    extract_stream_text,
    get_provider_state_response,
    send_provider_input,
)


def _run(coro):
    return asyncio.run(coro)


class FakeAgentManager:
    """In-memory agent manager for exercising the execution helpers.

    Holds a single agent dict (or None for the not-found case), a listener
    queue tests can pre-fill with stream events, and records the spawn / resume
    / kill / remove-listener calls the helpers make.
    """

    def __init__(self, agent=None):
        self._agent = agent
        self.queue = asyncio.Queue()
        self.spawned = []
        self.resumed = []
        self.killed = []
        self.removed_listeners = []
        self._agents = {}

    def get(self, agent_id):
        return self._agent

    def list(self):
        return [self._agent] if self._agent else []

    def add_listener(self, agent_id):
        return self.queue

    def remove_listener(self, agent_id, queue):
        self.removed_listeners.append((agent_id, queue))

    async def spawn_cli_process(self, agent_id, message):
        self.spawned.append((agent_id, message))
        if self._agent is not None:
            self._agent["status"] = "running"
            self._agent.setdefault("session_id", "sess-1")

    async def resume_session(self, agent_id, message):
        self.resumed.append((agent_id, message))

    def kill(self, agent_id, cleanup_worktree=False):
        self.killed.append((agent_id, cleanup_worktree))


# ---------------------------------------------------------------------------
# extract_stream_text / _extract_normalized_text
# ---------------------------------------------------------------------------


def _assistant_event(text):
    data = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return {"type": "output", "data": json.dumps(data)}


def test_extract_stream_text_assistant_concatenates_text_blocks():
    data = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}]},
    }
    assert extract_stream_text({"type": "output", "data": json.dumps(data)}) == "Hello world"


def test_extract_stream_text_tool_result_and_result_shapes():
    tool = {"type": "tool_result", "content": "tool output"}
    assert extract_stream_text({"type": "output", "data": json.dumps(tool)}) == "tool output"
    res = {"type": "result", "result": "final answer"}
    assert extract_stream_text({"type": "output", "data": json.dumps(res)}) == "final answer"


def test_extract_stream_text_ignores_non_output_events():
    assert extract_stream_text({"type": "exit", "exit_code": 0}) == ""
    assert extract_stream_text({"type": "output", "data": None}) == ""


def test_extract_stream_text_falls_back_to_raw_on_bad_json():
    # Malformed JSON must not be silently dropped: raw wins, else the data string.
    assert extract_stream_text({"type": "output", "data": "{bad", "raw": "RAW"}) == "RAW"
    assert extract_stream_text({"type": "output", "data": "plain text"}) == "plain text"


def test_extract_normalized_text_handles_non_dict_and_missing_text():
    assert ex._extract_normalized_text(["not", "a", "dict"]) == ""
    assert ex._extract_normalized_text({"type": "other"}) == ""
    assert ex._extract_normalized_text({"text": "top-level"}) == "top-level"


# ---------------------------------------------------------------------------
# send_provider_input — lifecycle dispatch
# ---------------------------------------------------------------------------


def test_send_input_prepared_starts_session():
    mgr = FakeAgentManager({"status": "prepared", "cli_tool": "claude"})
    out = _run(send_provider_input(mgr, "a1", "do it"))
    assert out["action"] == "started"
    assert out["cli_tool"] == "claude"
    assert mgr.spawned == [("a1", "do it")]


@pytest.mark.parametrize("status", ["paused", "completed", "failed"])
def test_send_input_resumes_when_session_exists(status):
    mgr = FakeAgentManager({"status": status, "session_id": "s9"})
    out = _run(send_provider_input(mgr, "a1", "again"))
    assert out["action"] == "resumed"
    assert mgr.resumed == [("a1", "again")]
    assert mgr.spawned == []


def test_send_input_rejects_running_agent_with_400():
    mgr = FakeAgentManager({"status": "running"})
    with pytest.raises(HTTPException) as exc:
        _run(send_provider_input(mgr, "a1", "x"))
    assert exc.value.status_code == 400
    assert "running" in exc.value.detail.lower()


def test_send_input_unknown_agent_is_404():
    mgr = FakeAgentManager(None)
    with pytest.raises(HTTPException) as exc:
        _run(send_provider_input(mgr, "ghost", "x"))
    assert exc.value.status_code == 404


def test_send_input_no_resumable_session_is_400():
    # Terminal-ish status but no session_id -> nothing to resume.
    mgr = FakeAgentManager({"status": "paused"})
    with pytest.raises(HTTPException) as exc:
        _run(send_provider_input(mgr, "a1", "x"))
    assert exc.value.status_code == 400
    assert "no active session" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# collect_provider_run — draining the stream into one result
# ---------------------------------------------------------------------------


def test_collect_run_completes_on_exit_event():
    async def scenario():
        mgr = FakeAgentManager({"status": "prepared"})
        await mgr.queue.put(_assistant_event("hello"))
        await mgr.queue.put({"type": "exit", "exit_code": 0})
        return mgr, await collect_provider_run(mgr, "a1", "hi", timeout_seconds=2)

    mgr, result = _run(scenario())
    assert result["success"] is True
    assert result["stdout"] == "hello"
    assert result["exit_code"] == 0
    assert result["can_resume"] is True  # spawn set a session_id
    # Listener subscription was cleaned up.
    assert mgr.removed_listeners and mgr.removed_listeners[0][0] == "a1"


def test_collect_run_reports_error_event():
    async def scenario():
        mgr = FakeAgentManager({"status": "prepared"})
        await mgr.queue.put({"type": "error", "error": "boom"})
        return await collect_provider_run(mgr, "a1", "hi", timeout_seconds=2)

    result = _run(scenario())
    assert result["exit_code"] == 1
    assert result["stderr"] == "boom"
    assert result["stdout"] == ""


def test_collect_run_handles_pause_with_exit_code():
    async def scenario():
        mgr = FakeAgentManager({"status": "prepared"})
        await mgr.queue.put({"type": "paused", "exit_code": 7})
        return await collect_provider_run(mgr, "a1", "hi", timeout_seconds=2)

    result = _run(scenario())
    assert result["exit_code"] == 7
    assert result["can_resume"] is True


def test_collect_run_times_out_with_408_and_cleans_up():
    async def scenario():
        mgr = FakeAgentManager({"status": "prepared"})  # queue left empty -> stalls
        try:
            await collect_provider_run(mgr, "a1", "hi", timeout_seconds=0.05)
            return mgr, None
        except HTTPException as exc:
            return mgr, exc

    mgr, exc = _run(scenario())
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 408
    # Even on timeout the listener must be removed.
    assert mgr.removed_listeners and mgr.removed_listeners[0][0] == "a1"


# ---------------------------------------------------------------------------
# delete / state responses
# ---------------------------------------------------------------------------


def test_delete_provider_session_terminates_and_acks():
    mgr = FakeAgentManager({"status": "running"})
    out = delete_provider_session(mgr, "a1", cleanup_worktree=True)
    assert out["status"] == "terminated"
    assert out["sandbox_id"] == "a1"
    assert out["provider"] == "cerver_local_provider"
    assert mgr.killed == [("a1", True)]


def test_get_provider_state_response_unknown_sandbox_is_404():
    mgr = FakeAgentManager(None)
    with pytest.raises(HTTPException) as exc:
        get_provider_state_response(mgr, "ghost", {})
    assert exc.value.status_code == 404
