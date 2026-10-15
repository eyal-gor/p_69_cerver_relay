"""Codex's MCP tool calls reach the transcript like any other tool call."""
from branch_monkey_mcp.bridge_and_local_actions.cli_providers import CodexProvider


def provider():
    return CodexProvider.__new__(CodexProvider)


def test_started_call_becomes_tool_use():
    ev = provider().normalize_event({"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "kompany", "tool": "propose_changes",
        "arguments": {"summary": "s"}, "status": "in_progress"}})
    block = ev["message"]["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "mcp__kompany__propose_changes"
    assert block["input"] == {"summary": "s"}


def test_completed_call_becomes_tool_result_with_its_text():
    ev = provider().normalize_event({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "kompany", "tool": "propose_changes", "status": "completed",
        "result": {"content": [{"type": "text", "text": '{"ok":true,"action_plan":{"actions":[1]}}'}]}}})
    assert ev == {"type": "tool_result", "content": '{"ok":true,"action_plan":{"actions":[1]}}', "is_error": False}


def test_failed_call_says_why():
    ev = provider().normalize_event({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "kompany", "tool": "x", "status": "failed",
        "error": {"message": "tool provider failed"}}})
    assert ev["is_error"] is True
    assert ev["content"] == "tool provider failed"
