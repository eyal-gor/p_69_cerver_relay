"""
A cerver chat whose first message reaches a relay that has no agent for it.

cerver opens chat sessions with engine "shell" (the gateway's default), so the
relay creates a shell sandbox, not an agent. When the first message arrives at
/agents/<id>/input there is nothing to resume: recover_agent must still
register the agent so send_input spawns a fresh CLI, rather than answering
404, which cerver swallows and the chat then waits on forever.

Run with: python -m pytest tests/test_recover_first_turn.py
"""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from branch_monkey_mcp.bridge_and_local_actions.agent_manager import (  # noqa: E402
    LocalAgentManager,
)
from branch_monkey_mcp.cerver_compute.shell_sandbox import (  # noqa: E402
    create_shell_sandbox,
    delete_shell_sandbox,
)


def test_first_turn_of_a_cerver_chat_is_recovered_without_a_resume_id(tmp_path):
    m = LocalAgentManager()
    shell = create_shell_sandbox({"working_dir": str(tmp_path)}, None)
    sid = shell["sandbox_id"]
    try:
        got = m.recover_agent(sid, cli_session_id=None, cli_tool="claude",
                              working_dir=None, cerver_session_id="sess-1")
        assert got is not None
        assert got["status"] == "paused"
        assert got["session_id"] is None      # send_input spawns fresh
        assert got["can_resume"] is False
        assert got["work_dir"] == str(tmp_path)  # the sandbox's folder
        assert m.get(sid) is not None
    finally:
        delete_shell_sandbox(sid)


def test_an_id_nobody_vouches_for_is_still_refused():
    m = LocalAgentManager()
    assert m.recover_agent("nobody", cli_session_id=None, cli_tool=None,
                           working_dir=None, cerver_session_id=None) is None


def test_a_resume_id_still_resumes():
    m = LocalAgentManager()
    got = m.recover_agent("a1", cli_session_id="abcdef123456", cli_tool="codex",
                          working_dir="/tmp", cerver_session_id="sess-2")
    assert got["session_id"] == "abcdef123456"
    assert got["can_resume"] is True
    assert got["cli_tool"] == "codex"
