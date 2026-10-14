"""
A session's tools, served to the CLI as an MCP server (stdio).

Cerver keeps the tools a caller gave a session (e.g. Kompany's chat tools).
When the relay runs Claude Code or Codex for that session it starts this
bridge beside it; the bridge lists the tools and forwards each call through
the relay's own local server (which adds the relay's cerver credentials), so
no key is written into the CLI's config.

    python -m branch_monkey_mcp.tools_bridge
    env: CERVER_SESSION_ID, RELAY_LOCAL_PORT
"""

import asyncio
import os
import sys

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

SESSION_ID = os.environ.get("CERVER_SESSION_ID", "")
BASE = f"http://127.0.0.1:{os.environ.get('RELAY_LOCAL_PORT', '18081')}/api/local-claude/session-tools/{SESSION_ID}"

server = Server("kompany")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(BASE)
        res.raise_for_status()
        tools = res.json().get("tools", [])
    return [
        types.Tool(name=t["name"], description=t.get("description") or "", inputSchema=t.get("input_schema") or {"type": "object", "properties": {}})
        for t in tools
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    async with httpx.AsyncClient(timeout=300) as client:
        res = await client.post(f"{BASE}/call", json={"tool_name": name, "tool_input": arguments or {}})
        try:
            body = res.json()
        except ValueError:
            body = {"content": res.text, "is_error": True}
    text = body.get("content")
    if not isinstance(text, str):
        text = str(text)
    if body.get("is_error") or not res.is_success:
        raise RuntimeError(text or f"tool call failed ({res.status_code})")
    return [types.TextContent(type="text", text=text)]


async def _main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    if not SESSION_ID:
        print("tools_bridge: CERVER_SESSION_ID is not set", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
