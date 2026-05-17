"""
Unit tests for the transcript-push dedup pipeline in agent_manager.

Covers the bugs that produced empty / duplicated cerver transcripts:

  - Bug A: Claude CLI emits both an `assistant` event and a `result`
    event with the same text. The old message.id-only dedup let the
    `result` slip through as a duplicate. Fix: signature-based dedup
    (role + kind + tool_id + sha1(content)) catches them as identical.
  - Bug B: `result` events get mapped to transcript entries even
    though the `assistant` event already pushed the same content. Fix:
    `_event_to_cerver_entries` no longer returns entries for `result`.
  - Bug C: tool_use events with the same tool_name but different inputs
    must not collide. Fix: signature folds tool_input into the hash.

Run with: bun test tests/test_transcript_dedup.py
(or: python -m pytest tests/test_transcript_dedup.py)
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the package importable when running from repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from branch_monkey_mcp.bridge_and_local_actions.agent_manager import (  # noqa: E402
    LocalAgentManager,
)
from branch_monkey_mcp.computer_runtime.execution import (  # noqa: E402
    extract_result_from_output_buffer,
)


# ---------------------------------------------------------------------------
# Bug A: signature dedup catches result-vs-assistant collisions
# ---------------------------------------------------------------------------

def test_signature_collapses_assistant_and_result_with_same_text():
    """Streaming `assistant` event and the matching `result` event must
    produce the same signature when their text is identical, so the
    second push is dedup'd."""
    assistant_entry = {"role": "assistant", "kind": "text", "content": "HELLO"}
    result_entry = {"role": "assistant", "kind": "text", "content": "HELLO"}
    sig_a = LocalAgentManager._entry_signature(assistant_entry)
    sig_b = LocalAgentManager._entry_signature(result_entry)
    assert sig_a == sig_b


def test_signature_distinguishes_different_text():
    """Two assistant entries with different text must have different
    signatures (regression: trivial hash collision would hide content)."""
    a = {"role": "assistant", "kind": "text", "content": "HELLO"}
    b = {"role": "assistant", "kind": "text", "content": "WORLD"}
    assert LocalAgentManager._entry_signature(a) != LocalAgentManager._entry_signature(b)


def test_signature_handles_unicode_and_long_content():
    """SHA1 path must not blow up on non-ASCII or large inputs."""
    a = {"role": "assistant", "kind": "text", "content": "שלום עולם 🚀"}
    b = {"role": "assistant", "kind": "text", "content": "שלום עולם 🚀"}
    c = {"role": "assistant", "kind": "text", "content": "x" * 50_000}
    assert LocalAgentManager._entry_signature(a) == LocalAgentManager._entry_signature(b)
    assert LocalAgentManager._entry_signature(a) != LocalAgentManager._entry_signature(c)


# ---------------------------------------------------------------------------
# Bug C: tool_use signatures fold input shape, not just tool name
# ---------------------------------------------------------------------------

def test_signature_for_tool_use_distinguishes_inputs():
    """Two `Bash` tool_use entries with different commands must NOT be
    treated as duplicates of each other."""
    a = {
        "role": "assistant",
        "kind": "tool_use",
        "content": "",
        "tool_id": "toolu_aaa",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    b = {
        "role": "assistant",
        "kind": "tool_use",
        "content": "",
        "tool_id": "toolu_bbb",  # different id
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},  # different input
    }
    assert LocalAgentManager._entry_signature(a) != LocalAgentManager._entry_signature(b)


def test_signature_for_tool_use_collapses_identical_inputs():
    """Same tool, same id, same input → same signature (so a re-emitted
    streaming chunk for the same tool_use is dedup'd)."""
    a = {
        "role": "assistant",
        "kind": "tool_use",
        "content": "",
        "tool_id": "toolu_xyz",
        "tool_name": "Read",
        "tool_input": {"path": "/x"},
    }
    b = dict(a)  # exact copy
    assert LocalAgentManager._entry_signature(a) == LocalAgentManager._entry_signature(b)


def test_signature_for_tool_use_input_order_invariant():
    """tool_input is JSON-encoded with sort_keys=True so dict ordering
    doesn't produce a false-distinct signature."""
    a = {
        "role": "assistant",
        "kind": "tool_use",
        "content": "",
        "tool_id": "toolu_xyz",
        "tool_name": "Read",
        "tool_input": {"path": "/x", "limit": 100},
    }
    b = {
        "role": "assistant",
        "kind": "tool_use",
        "content": "",
        "tool_id": "toolu_xyz",
        "tool_name": "Read",
        "tool_input": {"limit": 100, "path": "/x"},  # reversed order
    }
    assert LocalAgentManager._entry_signature(a) == LocalAgentManager._entry_signature(b)


# ---------------------------------------------------------------------------
# Bug B: result events no longer become transcript entries
# ---------------------------------------------------------------------------

def test_result_event_yields_no_transcript_entries():
    """`result` events must not be mapped — the matching `assistant`
    event already covers the text, so emitting an entry for `result`
    produced visible duplicates on cerver."""
    mgr = LocalAgentManager()
    inner = {"type": "result", "result": "HELLO", "subtype": "success"}
    event = {"type": "output", "data": json.dumps(inner)}
    entries = mgr._event_to_cerver_entries(event)
    assert entries == [], (
        "result events must not produce transcript entries — "
        "the streaming assistant event already pushed the text"
    )


def test_assistant_text_event_yields_one_entry():
    """Sanity check: a normal assistant text event still produces an entry."""
    mgr = LocalAgentManager()
    inner = {
        "type": "assistant",
        "message": {
            "id": "msg_abc",
            "content": [{"type": "text", "text": "HELLO"}],
        },
    }
    event = {"type": "output", "data": json.dumps(inner)}
    entries = mgr._event_to_cerver_entries(event)
    assert len(entries) == 1
    assert entries[0]["role"] == "assistant"
    assert entries[0]["kind"] == "text"
    assert entries[0]["content"] == "HELLO"


def test_assistant_tool_use_event_yields_tool_use_entry():
    """tool_use blocks within an assistant event become tool_use entries."""
    mgr = LocalAgentManager()
    inner = {
        "type": "assistant",
        "message": {
            "id": "msg_abc",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
        },
    }
    event = {"type": "output", "data": json.dumps(inner)}
    entries = mgr._event_to_cerver_entries(event)
    assert len(entries) == 1
    assert entries[0]["kind"] == "tool_use"
    assert entries[0]["tool_id"] == "toolu_1"
    assert entries[0]["tool_input"] == {"command": "ls"}


# ---------------------------------------------------------------------------
# Integration: _push_event_to_cerver dedup behaviour against the agent
# ---------------------------------------------------------------------------

def _make_fake_agent():
    """Minimal agent stand-in that mimics the dataclass surface used by
    _push_event_to_cerver / _post_transcript_entries."""
    agent = MagicMock()
    agent.id = "test-agent"
    agent.task_title = "test"
    agent.callback = None  # no cerver wiring → entries get tracked in stats but not POSTed
    agent._pushed_signatures = set()
    agent._push_stats = {
        "pushed": 0,
        "dedup_skipped": 0,
        "transport_waits": 0,
        "drops": 0,
    }
    return agent


def test_push_event_dedups_repeated_assistant_text():
    """Two `assistant` events with identical text produce one push, not two."""
    mgr = LocalAgentManager()
    agent = _make_fake_agent()
    inner = {
        "type": "assistant",
        "message": {
            "id": "msg_abc",
            "content": [{"type": "text", "text": "HELLO"}],
        },
    }
    event = {"type": "output", "data": json.dumps(inner)}

    mgr._push_event_to_cerver(agent, event)
    mgr._push_event_to_cerver(agent, event)

    # First call adds 1 signature, second one is dedup_skipped.
    assert len(agent._pushed_signatures) == 1
    assert agent._push_stats["dedup_skipped"] == 1


def test_push_event_assistant_and_result_collapse():
    """The exact bug from the diagnostic probe: assistant says HELLO,
    then result reports the same HELLO, total signatures must be 1."""
    mgr = LocalAgentManager()
    agent = _make_fake_agent()

    assistant_inner = {
        "type": "assistant",
        "message": {
            "id": "msg_abc",
            "content": [{"type": "text", "text": "HELLO"}],
        },
    }
    result_inner = {"type": "result", "result": "HELLO", "subtype": "success"}

    mgr._push_event_to_cerver(agent, {"type": "output", "data": json.dumps(assistant_inner)})
    mgr._push_event_to_cerver(agent, {"type": "output", "data": json.dumps(result_inner)})

    # Result event produces no entries at all (dropped in _event_to_cerver_entries).
    # Assistant event produces one. Final signature count == 1.
    assert len(agent._pushed_signatures) == 1
    # No dedup_skipped here because the result event yielded no entries to dedup.
    assert agent._push_stats["dedup_skipped"] == 0


def test_post_transcript_dedups_across_all_callers():
    """Regression: the v317 probe revealed dedup was only checked inside
    _push_event_to_cerver. _push_user_message and the post-loop final
    flush bypass that path and call _post_transcript_entries directly,
    which produced duplicates on cerver. The fix moved dedup INTO
    _post_transcript_entries so every push path benefits.
    """
    mgr = LocalAgentManager()
    agent = _make_fake_agent()

    user_entry = {"role": "user", "kind": "text", "content": "Say HELLO"}
    assistant_entry = {"role": "assistant", "kind": "text", "content": "HELLO"}

    # Caller A: simulate _push_user_message routing through
    # _post_transcript_entries directly
    mgr._post_transcript_entries(agent, [user_entry])
    # Caller B: simulate streaming assistant event through the entry pipeline
    mgr._post_transcript_entries(agent, [assistant_entry])
    # Caller C: simulate the post-loop final flush re-pushing the same
    # assistant text. Without the fix this would slip through and
    # appear as a duplicate on cerver.
    mgr._post_transcript_entries(agent, [{"role": "assistant", "kind": "text", "content": "HELLO"}])
    # Caller D: simulate _push_user_message firing again with the same
    # prompt — also must dedup.
    mgr._post_transcript_entries(agent, [user_entry])

    # Two unique signatures: the user message and the assistant reply.
    assert len(agent._pushed_signatures) == 2
    # Two duplicate attempts skipped (caller C and D).
    assert agent._push_stats["dedup_skipped"] == 2


def test_codex_empty_turn_completed_falls_back_to_assistant_text():
    """Codex normalizes turn.completed to type=result with result="".
    That empty result must not mask the real assistant text emitted earlier,
    or final flush appends a bogus "[cli_exit] no assistant message" entry.
    """
    output_buffer = [
        {
            "parsed": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "real answer"}]},
            }
        },
        {"parsed": {"type": "result", "result": "", "usage": {"turns": 1}}},
    ]

    assert extract_result_from_output_buffer(output_buffer) == "real answer"
