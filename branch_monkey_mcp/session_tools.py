"""
Hand a cerver session's tools to the CLI the relay runs for it.

Adds the tools bridge (tools_bridge.py) as an MCP server on the CLI's command
line: `--mcp-config <file>` for Claude Code, `-c mcp_servers.kompany.*` for
Codex. Other CLIs run unchanged.
"""

import json
import os
import shlex
import sys
import tempfile

SERVER_NAME = "kompany"


def _bridge(session_id: str, local_port: int):
    return {
        "command": sys.executable,
        "args": ["-m", "branch_monkey_mcp.tools_bridge"],
        "env": {"CERVER_SESSION_ID": session_id, "RELAY_LOCAL_PORT": str(local_port)},
    }


def _claude_config_file(session_id: str, local_port: int) -> str:
    folder = os.path.join(tempfile.gettempdir(), "cerver-session-tools")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{session_id}.json")
    with open(path, "w") as f:
        json.dump({"mcpServers": {SERVER_NAME: _bridge(session_id, local_port)}}, f)
    return path


def _codex_overrides(session_id: str, local_port: int):
    b = _bridge(session_id, local_port)
    env = ",".join(f'{k}="{v}"' for k, v in b["env"].items())
    return [
        "-c", f'mcp_servers.{SERVER_NAME}.command="{b["command"]}"',
        "-c", f'mcp_servers.{SERVER_NAME}.args={json.dumps(b["args"])}',
        "-c", f"mcp_servers.{SERVER_NAME}.env={{{env}}}",
    ]


def with_session_tools(cli_cmd, cli_tool: str, session_id: str, local_port: int = 18081):
    """Return cli_cmd with the session's tools added (in place)."""
    if not session_id:
        return cli_cmd
    args = list(cli_cmd.args)
    tool = (cli_tool or "").lower()
    try:
        if tool == "claude" and args and args[0] == "claude":
            args[1:1] = ["--mcp-config", _claude_config_file(session_id, local_port)]
        elif tool == "codex":
            flags = _codex_overrides(session_id, local_port)
            if args and args[0] == "codex":
                args[1:1] = flags
            elif len(args) == 3 and args[0] == "bash" and "| codex" in args[2]:
                quoted = " ".join(shlex.quote(f) for f in flags)
                args[2] = args[2].replace("| codex", f"| codex {quoted}", 1)
        cli_cmd.args = args
    except Exception as exc:  # the session still runs, just without the tools
        print(f"[session_tools] could not add the session's tools: {exc}")
    return cli_cmd
