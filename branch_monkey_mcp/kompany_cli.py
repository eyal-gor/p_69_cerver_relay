"""
kompany — a thin CLI over the kompany.dev HTTP API.

Companion to the MCP server, not a replacement. Both wrap the SAME
`api_client` and the SAME stored auth (`~/.branch-monkey/token.json`).
The MCP is for AI-driven interactive use; the CLI is for shell pipes,
xargs, cron, and CI:

    kompany status
    kompany project focus <id>
    kompany task search "Cron Diagnostics Run" --json | jq -r '.[].id' | xargs -n 50 kompany task delete -q
    kompany task create "Ship the audio meter" --version 2026-05-28

Project focus is persistent across CLI invocations — kept in
`~/.branch-monkey/cli-focus.json`. This is separate from the MCP
server's in-memory focus on purpose; the two surfaces have independent
ergonomics and shouldn't surprise each other.

When a verb only fits the AI/MCP shape (decision queues, agent
configuration, anything that needs LLM reasoning), it lives on the MCP
and stays off the CLI. When in doubt, add it to the MCP first; promote
to the CLI when a real shell workflow asks for it.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Optional

import click

# Importing the MCP package as a side-effect loads `state.API_URL` and
# `state.API_KEY` from `~/.branch-monkey/token.json`. It also prints a
# "Connecting to: …" line to stderr — fine for the MCP server, noisy for
# a CLI that's about to be xargs'd 50× a second. Silence it for the
# common (token-already-stored) path; if the import actually needs to
# prompt (no token, device flow), the captured output is replayed.
_import_stderr = io.StringIO()
try:
    with contextlib.redirect_stderr(_import_stderr):
        import branch_monkey_mcp.kompany_mcp  # noqa: F401
except Exception:
    sys.stderr.write(_import_stderr.getvalue())
    raise

from branch_monkey_mcp.kompany_mcp import state  # noqa: E402
from branch_monkey_mcp.kompany_mcp.api_client import (  # noqa: E402
    api_delete,
    api_get,
    api_post,
    api_put,
)


# ── Persistent focus (CLI-side only; MCP keeps its own in memory) ────────

FOCUS_FILE = Path.home() / ".branch-monkey" / "cli-focus.json"


def _read_focus() -> Optional[dict]:
    if not FOCUS_FILE.exists():
        return None
    try:
        return json.loads(FOCUS_FILE.read_text())
    except Exception:
        return None


def _write_focus(project_id: str, project_name: Optional[str] = None) -> None:
    FOCUS_FILE.parent.mkdir(exist_ok=True)
    payload: dict = {"project_id": project_id}
    if project_name:
        payload["project_name"] = project_name
    FOCUS_FILE.write_text(json.dumps(payload, indent=2))
    FOCUS_FILE.chmod(0o600)


def _clear_focus() -> None:
    if FOCUS_FILE.exists():
        FOCUS_FILE.unlink()


def _resolve_project(project: Optional[str]) -> str:
    if project:
        return project
    focus = _read_focus()
    if focus and focus.get("project_id"):
        return focus["project_id"]
    click.echo(
        "no project focused. set one with `kompany project focus <id>` "
        "or pass --project <id>.",
        err=True,
    )
    sys.exit(2)


# ── Output helpers ───────────────────────────────────────────────────────

STATUS_ICON = {
    "todo": "⬜",
    "in_progress": "🔄",
    "in_review": "👀",
    "done": "✅",
    "completed": "✅",
    "complete": "✅",
}


def _print_tasks_table(tasks: list[dict]) -> None:
    for t in tasks:
        icon = STATUS_ICON.get(t.get("status"), "⬜")
        num = t.get("task_number") or "?"
        uid = t.get("id") or "?"
        title = (t.get("title") or "").splitlines()[0]
        click.echo(f"{icon}  #{num:<6} {uid}  {title[:80]}")


def _print_versions_table(versions: list[dict]) -> None:
    for v in versions:
        locked = " 🔒" if v.get("locked") else ""
        click.echo(f"{v.get('key', '?'):<24}  {v.get('label', '')}{locked}")


def _print_projects_table(projects: list[dict]) -> None:
    for p in projects:
        click.echo(f"{p.get('id', '?')}  {p.get('name', '?')}")


# ── Top-level group ──────────────────────────────────────────────────────

@click.group(
    help=(
        "kompany — bulk/shell companion to the kompany MCP. "
        "Same auth (`~/.branch-monkey/token.json`), same endpoints; "
        "designed for pipes, xargs, cron, CI."
    )
)
def cli() -> None:
    pass


# ── status ───────────────────────────────────────────────────────────────

@cli.command(help="Show auth + focus state.")
def status() -> None:
    click.echo(f"api url      {state.API_URL}")
    click.echo(f"auth         {'token loaded' if state.API_KEY else 'NOT AUTHENTICATED'}")
    focus = _read_focus()
    if focus:
        name = focus.get("project_name", "")
        click.echo(f"focus        {focus.get('project_id')}  {name}")
    else:
        click.echo("focus        (none — `kompany project focus <id>` to set)")


# ── project ──────────────────────────────────────────────────────────────

@cli.group(help="Project operations.")
def project() -> None:
    pass


@project.command("list", help="List available projects.")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON.")
def project_list(json_out: bool) -> None:
    result = api_get("/api/projects")
    projects = result.get("projects", result if isinstance(result, list) else [])
    if json_out:
        click.echo(json.dumps(projects, indent=2))
        return
    _print_projects_table(projects)


@project.command("focus", help="Set this CLI's persistent project focus.")
@click.argument("project_id")
def project_focus(project_id: str) -> None:
    name: Optional[str] = None
    try:
        result = api_get(f"/api/projects/{project_id}")
        proj = result.get("project", result if isinstance(result, dict) else {})
        if isinstance(proj, dict):
            name = proj.get("name")
    except Exception:
        pass
    _write_focus(project_id, name)
    suffix = f"  ({name})" if name else ""
    click.echo(f"focused  {project_id}{suffix}")


@project.command("clear", help="Clear CLI focus.")
def project_clear() -> None:
    _clear_focus()
    click.echo("focus cleared")


@project.command("current", help="Show current CLI focus.")
def project_current() -> None:
    focus = _read_focus()
    if not focus:
        click.echo("(none)")
        return
    click.echo(f"{focus.get('project_id')}  {focus.get('project_name', '')}")


# ── task ─────────────────────────────────────────────────────────────────

@cli.group(help="Task operations.")
def task() -> None:
    pass


@task.command("list", help="List tasks in the focused (or --project) project.")
@click.option("--project", help="Override the focused project_id.")
@click.option("--machine", help="Filter by machine_id.")
@click.option("--json", "json_out", is_flag=True)
def task_list(project: Optional[str], machine: Optional[str], json_out: bool) -> None:
    pid = _resolve_project(project)
    params: dict = {"project_id": pid}
    if machine:
        params["machine_id"] = machine
    result = api_get("/api/tasks", params=params)
    tasks = result.get("tasks", result if isinstance(result, list) else [])
    if json_out:
        click.echo(json.dumps(tasks, indent=2))
        return
    _print_tasks_table(tasks)


@task.command("search", help="Search tasks by title or description.")
@click.argument("query")
@click.option("--status", help="Filter by status (todo|in_progress|in_review|done).")
@click.option("--version", help="Filter by version key.")
@click.option("--project", help="Override the focused project_id.")
@click.option("--json", "json_out", is_flag=True)
def task_search(
    query: str,
    status: Optional[str],
    version: Optional[str],
    project: Optional[str],
    json_out: bool,
) -> None:
    params: dict = {"query": query}
    if status:
        params["status"] = status
    if version:
        params["version"] = version
    pid = project or (_read_focus() or {}).get("project_id")
    if pid:
        params["project_id"] = pid
    result = api_get("/api/tasks/search", params=params)
    tasks = result.get("tasks", [])
    if json_out:
        click.echo(json.dumps(tasks, indent=2))
        return
    _print_tasks_table(tasks)


@task.command("get", help="Fetch one task as JSON.")
@click.argument("task_id")
def task_get(task_id: str) -> None:
    try:
        data = api_get(f"/api/tasks/{task_id}")
        click.echo(json.dumps(data, indent=2))
    except Exception as exc:
        click.echo(f"FAIL  {task_id}  {exc}", err=True)
        sys.exit(1)


@task.command("create", help="Create a task in the focused (or --project) project.")
@click.argument("title")
@click.option("--description", default="")
@click.option("--status", default="todo")
@click.option("--priority", type=int, default=0)
@click.option(
    "--version",
    help=(
        "Version key. Omit to let the API route the task into today's "
        "daily version (PR #290 behaviour)."
    ),
)
@click.option("--machine", help="Machine id to assign.")
@click.option("--project", help="Override focused project_id.")
@click.option("--json", "json_out", is_flag=True)
def task_create(
    title: str,
    description: str,
    status: str,
    priority: int,
    version: Optional[str],
    machine: Optional[str],
    project: Optional[str],
    json_out: bool,
) -> None:
    pid = _resolve_project(project)
    payload: dict = {
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "project_id": pid,
    }
    if version:
        payload["version"] = version
    if machine:
        payload["machine_id"] = machine
    data = api_post("/api/tasks", payload)
    if json_out:
        click.echo(json.dumps(data, indent=2))
        return
    t = data.get("task", data) if isinstance(data, dict) else {}
    click.echo(f"created  #{t.get('task_number', '?')}  {t.get('id', '?')}")


@task.command("update", help="Update fields on a task by UUID or task_number.")
@click.argument("task_id")
@click.option("--title")
@click.option("--description")
@click.option("--status")
@click.option("--priority", type=int)
@click.option("--version", help="Set the task's version (sprint).")
@click.option("--machine", help="Set machine_id.")
@click.option("--json", "json_out", is_flag=True)
def task_update(
    task_id: str,
    title: Optional[str],
    description: Optional[str],
    status: Optional[str],
    priority: Optional[int],
    version: Optional[str],
    machine: Optional[str],
    json_out: bool,
) -> None:
    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if description is not None:
        updates["description"] = description
    if status is not None:
        updates["status"] = status
    if priority is not None:
        updates["priority"] = priority
    if version is not None:
        updates["version"] = version
    if machine is not None:
        updates["machine_id"] = machine
    if not updates:
        click.echo("no fields to update — pass at least one --option", err=True)
        sys.exit(2)
    data = api_put(f"/api/tasks/{task_id}", updates)
    if json_out:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"updated  {task_id}")


@task.command("delete", help="Delete one or more tasks by UUID or task_number.")
@click.argument("task_ids", nargs=-1, required=True)
@click.option("--quiet", "-q", is_flag=True, help="Suppress per-task success output.")
def task_delete(task_ids: tuple[str, ...], quiet: bool) -> None:
    failures: list[str] = []
    for tid in task_ids:
        try:
            api_delete(f"/api/tasks/{tid}")
            if not quiet:
                click.echo(f"ok    {tid}")
        except Exception as exc:
            click.echo(f"FAIL  {tid}  {exc}", err=True)
            failures.append(tid)
    if failures:
        click.echo(f"{len(failures)} delete(s) failed", err=True)
        sys.exit(1)


@task.command("log", help="Append an LLM progress note to a task.")
@click.argument("task_id")
@click.argument("content")
@click.option("--update-type", default="progress", help="Tag for the log entry.")
def task_log(task_id: str, content: str, update_type: str) -> None:
    api_post(
        f"/api/tasks/{task_id}/log",
        {"content": content, "update_type": update_type},
    )
    click.echo(f"logged  {task_id}")


# ── version ──────────────────────────────────────────────────────────────

@cli.group(help="Version (sprint/daily) operations.")
def version() -> None:
    pass


@version.command("list", help="List versions in the focused project.")
@click.option("--project", help="Override focused project_id.")
@click.option("--json", "json_out", is_flag=True)
def version_list(project: Optional[str], json_out: bool) -> None:
    pid = _resolve_project(project)
    result = api_get(f"/api/versions?project_id={pid}")
    versions = result.get("versions", [])
    if json_out:
        click.echo(json.dumps(versions, indent=2))
        return
    _print_versions_table(versions)


@version.command("create", help="Create a new version in the focused project.")
@click.argument("key")
@click.option("--label", required=True, help="Display label (e.g. 'Thu, May 28').")
@click.option("--description", default="")
@click.option("--sort-order", type=int, default=0)
@click.option("--project", help="Override focused project_id.")
def version_create(
    key: str,
    label: str,
    description: str,
    sort_order: int,
    project: Optional[str],
) -> None:
    pid = _resolve_project(project)
    api_post(
        "/api/versions",
        {
            "key": key,
            "label": label,
            "description": description,
            "sort_order": sort_order,
            "project_id": pid,
        },
    )
    click.echo(f"created  {key}  {label}")


@version.command("delete", help="Delete a version by key (project-scoped).")
@click.argument("key")
@click.option("--project", help="Override focused project_id.")
def version_delete(key: str, project: Optional[str]) -> None:
    pid = _resolve_project(project)
    api_delete(f"/api/versions/{key}", params={"project_id": pid})
    click.echo(f"deleted  {key}")


# ── entry point ──────────────────────────────────────────────────────────


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
