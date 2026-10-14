"""
Background job tools: how a worker running on this computer (Claude Code or
Codex, started by cerver for a Kompany job) reads the company and hands its
change back.

Each call carries the job id and the job's token from the task the worker was
given; Kompany checks the token and runs the same tools its own chat uses, so
nothing here needs `kompany_login`.
"""

import json

import requests

from .. import state
from ..mcp_app import mcp


def _call(job_id: str, token: str, tool: str, payload: dict) -> str:
    url = f"{state.API_URL.rstrip('/')}/api/jobs/{job_id}/tool"
    try:
        res = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-Job-Token": token},
            data=json.dumps({"tool": tool, "input": payload}),
            timeout=240,
        )
    except requests.RequestException as e:
        return f"Error: could not reach Kompany ({e})"
    try:
        body = res.json()
    except ValueError:
        return f"Error: Kompany answered {res.status_code}: {res.text[:300]}"
    if not res.ok:
        return f"Error: {body.get('error') or res.status_code}"
    return body.get("content", "")


@mcp.tool()
def kompany_job_read(job_id: str, token: str, what: str, system: str = "", query: str = "") -> str:
    """Read the company for a Kompany job.

    what:
      review_systems — every system: its steps, schedule, last run, what is wrong
      workflow       — one system's steps (pass system)
      runs           — one system's recent runs and errors (pass system)
      metrics        — one system's numbers (pass system)
      changes        — what changed in one system lately (pass system)
      documents      — search the company's documents and story (pass query)
      decisions      — the decisions waiting on the owner
    """
    return _call(job_id, token, "read", {"what": what, "system": system, "query": query})


@mcp.tool()
def kompany_job_propose(job_id: str, token: str, summary: str, actions_json: str) -> str:
    """Hand the job's change back to Kompany: {summary, actions} as in the chat's
    propose_changes. Kompany checks it; if it passes, it waits for the owner to
    apply with one click. If it sends back reasons, fix them and call again.

    actions_json: a JSON array of actions, e.g. [{"type": "create_machine", ...}]
    """
    try:
        actions = json.loads(actions_json)
    except ValueError as e:
        return f"Error: actions_json is not JSON ({e})"
    return _call(job_id, token, "propose", {"summary": summary, "actions": actions})


@mcp.tool()
def kompany_job_progress(job_id: str, token: str, line: str) -> str:
    """Tell the owner what you are doing now, in a few plain words
    (e.g. "Reading Email Outreach's steps"). Shown live on the Jobs page."""
    return _call(job_id, token, "progress", {"line": line})


@mcp.tool()
def kompany_job_done(job_id: str, token: str, ok: bool, summary: str) -> str:
    """End the job. ok=true after kompany_job_propose passed; ok=false when
    something blocked you. summary: one or two sentences for the owner on what
    you changed, or what blocked you and what they must do."""
    return _call(job_id, token, "done", {"ok": ok, "summary": summary})
