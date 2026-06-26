"""
Task management tools.

The thin ``@mcp.tool()`` wrappers below define the MCP schema; the heavier
implementations (workflow guidance, PR creation, completion bookkeeping,
artifact assembly) live in ``task_services.py``.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp
from . import task_services
from .task_services import auto_log_activity


@mcp.tool()
def kompany_task_list(machine_id: str = None) -> str:
    """List all tasks for the current project.

    Args:
        machine_id: Optional machine UUID to filter tasks by a specific machine

    Requires a project to be focused first using kompany_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first.\n\nUse `kompany_project_list` to see available projects."

    try:
        params = {"project_id": state.CURRENT_PROJECT_ID}
        if machine_id:
            params["machine_id"] = machine_id
        result = api_get("/api/tasks", params=params)
        tasks = result.get("tasks", [])

        if not tasks:
            return f"No tasks found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Tasks (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for task in tasks:
            status_icon = {"todo": "⬜", "in_progress": "🔄", "done": "✅"}.get(task.get("status"), "⬜")
            task_num = task.get('task_number', 'N/A')
            output += f"{status_icon} **#{task_num}**: {task.get('title')}\n"
            if task.get("description"):
                output += f"   {task.get('description')[:100]}...\n"

        return output
    except Exception as e:
        return f"Error fetching tasks: {str(e)}"


@mcp.tool()
def kompany_task_create(
    title: str,
    description: str = "",
    status: str = "todo",
    priority: int = 0,
    version: str = None,
    machine_id: str = None
) -> str:
    """Create a new task in the current project.

    If `version` is omitted, the task is filed under the project's latest
    version (i.e. today's daily plan), not the backlog. Pass `version`
    explicitly only to override that default.

    Requires a project to be focused first using kompany_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first."

    try:
        data = {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "project_id": state.CURRENT_PROJECT_ID
        }
        # Only send an explicit version. When omitted, let the backend default
        # the task to the project's latest version (today's daily plan) rather
        # than forcing "backlog" and stranding every agent/MCP-created task.
        if version:
            data["version"] = version
        if machine_id:
            data["machine_id"] = machine_id

        result = api_post("/api/tasks", data)
        task = result.get("task", result)

        return f"✅ Created task #{task.get('task_number', task.get('id'))}: {title} (Project: {state.CURRENT_PROJECT_NAME})"
    except Exception as e:
        return f"Error creating task: {str(e)}"


@mcp.tool()
def kompany_task_update(
    task_id: str,
    title: str = None,
    description: str = None,
    status: str = None,
    priority: int = None,
    version: str = None,
    machine_id: str = None
) -> str:
    """Update an existing task.

    Args:
        task_id: Task number (e.g., "123") or UUID (e.g., "abc-def-...")
    """
    try:
        updates = {}
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
        if machine_id is not None:
            updates["machine_id"] = machine_id if machine_id else None

        api_put(f"/api/tasks/{task_id}", updates)
        return f"✅ Updated task {task_id}"
    except Exception as e:
        return f"Error updating task: {str(e)}"


@mcp.tool()
def kompany_task_delete(task_id: str) -> str:
    """Delete a task by UUID."""
    try:
        api_delete(f"/api/tasks/{task_id}")
        return f"✅ Deleted task {task_id}"
    except Exception as e:
        return f"Error deleting task: {str(e)}"


@mcp.tool()
def kompany_task_work(task_id: int, workflow: str = "execute") -> str:
    """Start working on a task. Sets status to in_progress and logs start.

    Args:
        task_id: The task number to work on
        workflow: Required workflow type:
            - "ask": Quick question/research - answer directly, no code changes
            - "plan": Design/architecture - create plan, get approval before implementing
            - "execute": Implementation - create worktree, code, PR, complete with context
            - "workspace": Non-code task - runs in project dir, saves outputs as contexts
    """
    return task_services.start_task_work(task_id, workflow)


@mcp.tool()
def kompany_task_log(task_id: int, content: str, update_type: str = "progress") -> str:
    """Log LLM work on a task."""
    try:
        api_post(f"/api/tasks/{task_id}/log", {
            "content": content,
            "update_type": update_type
        })
        auto_log_activity("task_log")
        return f"✓ Logged update to task #{task_id}"
    except Exception as e:
        return f"Error logging: {str(e)}"


@mcp.tool()
def kompany_task_complete(
    task_id: int,
    summary: str,
    worktree_path: str = None,
    files_changed: str = None,
    context_name: str = None
) -> str:
    """Mark a task as complete, create a PR using gh CLI, and link everything.

    This will:
    1. Run 'gh pr create --fill' to create a GitHub PR (from worktree directory)
    2. Mark the task as complete with the PR URL
    3. Create a linked context with the summary

    Args:
        task_id: The task number to complete
        summary: Summary of what was done
        worktree_path: Path to the worktree directory (required for PR creation)
        files_changed: Comma-separated list of files that were modified (e.g., "src/foo.ts, src/bar.ts")
        context_name: Optional name for the context (defaults to task title)
    """
    return task_services.complete_task(
        task_id, summary, worktree_path, files_changed, context_name
    )


@mcp.tool()
def kompany_task_add_artifact(
    task_id: str,
    artifact_type: str,
    body: str,
    platform: str = None,
    title: str = None,
    subject: str = None,
    to: str = None,
    url: str = None,
    filename: str = None,
    metadata: str = None
) -> str:
    """Add a structured artifact to a task's output.

    Artifacts are typed outputs that the Decision Preparer will package into decisions.
    Call this once per output item (e.g., once per social post, once per email draft).

    Args:
        task_id: The UUID of the task
        artifact_type: Type of artifact. One of:
            - social_post: Social media post (use platform + body)
            - email: Email draft (use to + subject + body)
            - code_pr: Pull request (use title + url + body for summary)
            - report: Analysis/report (use title + body)
            - message: Chat/Slack message (use to + body)
            - file: Generated file/asset (use filename + url)
            - generic: Anything else (use title + body)
        body: The main content (post text, email body, PR summary, report content, etc.)
        platform: For social_post: LinkedIn, X, Instagram, Facebook, etc.
        title: Title or headline
        subject: For email: email subject line
        to: For email/message: recipient(s)
        url: Link to external resource (PR url, file url, etc.)
        filename: For file artifacts: the filename
        metadata: Optional JSON string of extra key-value pairs
    """
    return task_services.add_task_artifact(
        task_id, artifact_type, body, platform, title, subject, to, url, filename, metadata
    )


@mcp.tool()
def kompany_task_search(query: str, status: str = None, version: str = None) -> str:
    """Search tasks by title or description."""
    try:
        params = {"query": query}
        if status:
            params["status"] = status
        if version:
            params["version"] = version
        # Include project_id if a project is focused (enables team task search)
        if state.CURRENT_PROJECT_ID:
            params["project_id"] = state.CURRENT_PROJECT_ID

        result = api_get("/api/tasks/search", params=params)
        tasks = result.get("tasks", [])

        if not tasks:
            return f"No tasks matching '{query}'"

        output = f"# Tasks matching '{query}'\n\n"
        for task in tasks:
            status_icon = {"todo": "⬜", "in_progress": "🔄", "done": "✅", "in_review": "👀"}.get(task.get("status"), "⬜")
            task_num = task.get('task_number', 'None')
            task_uuid = task.get('id', 'N/A')
            output += f"{status_icon} **#{task_num}** `{task_uuid}`: {task.get('title')}\n"
            if task.get("description"):
                desc = task.get('description', '')
                # Show full description, truncate if very long
                if len(desc) > 500:
                    desc = desc[:500] + "..."
                output += f"   📝 {desc}\n"

        return output
    except Exception as e:
        return f"Error searching: {str(e)}"
