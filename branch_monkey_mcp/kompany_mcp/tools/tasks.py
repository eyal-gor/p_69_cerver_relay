"""
Task management tools.
"""

import subprocess
import re

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


def auto_log_activity(tool_name: str, duration: float = 0):
    """Automatically log tool activity when a task is active."""
    if state.CURRENT_TASK_ID is None:
        return

    try:
        from datetime import datetime

        data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "provider": "mcp",
            "model": "claude-tool-call",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0,
            "duration": duration,
            "prompt_preview": f"Tool: {tool_name}",
            "response_preview": "",
            "status": "success",
            "session_id": state.CURRENT_SESSION_ID,
            "tool_name": tool_name,
            "git_email": state.GIT_USER_EMAIL,
            "task_id": state.CURRENT_TASK_ID,
            "task_title": state.CURRENT_TASK_TITLE
        }

        api_post("/api/prompt-logs", data)
    except Exception:
        pass


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
    # Validate workflow
    valid_workflows = ["ask", "plan", "execute", "workspace"]
    if workflow not in valid_workflows:
        return f"❌ Invalid workflow '{workflow}'. Must be one of: {', '.join(valid_workflows)}"

    try:
        # Start working on task (workflow is guidance only, not stored)
        result = api_post(f"/api/tasks/{task_id}/work")
        task = result.get("task", {})

        state.CURRENT_TASK_ID = task_id
        state.CURRENT_TASK_TITLE = task.get('title', 'Unknown')

        auto_log_activity("task_work_start", duration=1)

        # Workflow-specific instructions
        if workflow == "ask":
            next_steps = """**Next Steps (Ask Workflow):**
1. Research/explore to answer the question
2. Use `kompany_task_log` to record findings
3. Use `kompany_task_update` to mark done when answered"""
        elif workflow == "plan":
            next_steps = """**Next Steps (Plan Workflow):**
1. Research the codebase and requirements
2. Create a plan/design document
3. Use `kompany_task_log` to record the plan
4. Get user approval before implementing
5. If approved, switch to execute workflow or create sub-tasks"""
        elif workflow == "workspace":
            next_steps = f"""**Next Steps (Workspace Workflow):**
1. Work on the task (research, analysis, writing, etc.)
2. Use `kompany_task_log(task_id={task_id}, content="...")` to record progress
3. Save outputs using `kompany_context_create(name="...", content="...", context_type="general")`
4. Complete: `kompany_task_complete(task_id={task_id}, summary="...")`

No worktree or PR needed — results are saved as Kompany contexts."""
        else:  # execute
            next_steps = f"""**Next Steps (Execute Workflow):**

**Step 1: Create Worktree** (isolates your changes)
```bash
git worktree add .worktrees/task-{task_id} -b task/{task_id}-short-description
cd .worktrees/task-{task_id}
```

**Step 2: Implement Changes**
- Make changes in the worktree (NOT the main repo)
- Use `kompany_task_log()` to record progress

**Step 3: Commit & Push**
```bash
git add .
git commit -m "Task #{task_id}: description

Co-Authored-By: Kompany.dev via Claude Code"
git push -u origin task/{task_id}-short-description
```

**Step 4: Complete Task**
`kompany_task_complete(task_id={task_id}, summary="...", worktree_path=".worktrees/task-{task_id}")`

This creates a GitHub PR. The user reviews and merges it (NOT auto-merged)."""

        return f"""# Working on Task {task_id}: {task.get('title', 'Unknown')}

**Workflow:** {workflow.upper()}
**Status:** in_progress
**Description:** {task.get('description') or '(none)'}
**Version:** {task.get('version') or 'backlog'}

{next_steps}"""
    except Exception as e:
        return f"Error: {str(e)}"


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
    try:
        github_pr_url = None
        pr_output = ""

        if not worktree_path:
            # No worktree = non-code task (workspace/ask/plan) — skip PR
            pr_output = "No worktree — skipping PR creation"
        else:
            # Try to create PR using gh CLI
            try:
                pr_result = subprocess.run(
                    ["gh", "pr", "create", "--fill"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=worktree_path
                )
                pr_output = pr_result.stdout + pr_result.stderr

                # Extract PR URL from output (gh pr create outputs the URL)
                pr_match = re.search(r'https://github\.com/[^/]+/[^/]+/pull/\d+', pr_output)
                if pr_match:
                    github_pr_url = pr_match.group(0)
            except FileNotFoundError:
                pr_output = "gh CLI not found - skipping PR creation"
            except subprocess.TimeoutExpired:
                pr_output = "gh pr create timed out"
            except Exception as e:
                pr_output = f"PR creation failed: {str(e)}"

        payload = {"summary": summary}
        if github_pr_url:
            payload["github_pr_url"] = github_pr_url
        if files_changed:
            payload["files_changed"] = files_changed
        # Use /in_review endpoint to move task to "In Review" status for human verification
        result = api_post(f"/api/tasks/{task_id}/in_review", payload)
        task = result.get("task", {})
        task_title = task.get('title', 'Unknown')
        task_uuid = task.get('id')

        auto_log_activity("task_complete", duration=1)

        state.CURRENT_TASK_ID = None
        state.CURRENT_TASK_TITLE = None

        output = f"✅ Task {task_id} completed: {task_title}\n\nSummary: {summary}"
        if github_pr_url:
            output += f"\n\n🔗 PR created: {github_pr_url}"
        elif pr_output:
            output += f"\n\n⚠️ PR: {pr_output}"

        # Auto-create and link context if project is focused
        context_id = None
        if state.CURRENT_PROJECT_ID and task_uuid:
            try:
                # Build context content
                context_content = ""
                if files_changed:
                    context_content += f"Files changed:\n"
                    for f in files_changed.split(","):
                        context_content += f"- {f.strip()}\n"
                    context_content += "\n"
                context_content += summary

                # Create context
                ctx_name = context_name or f"Task #{task_id}: {task_title[:50]}"
                ctx_result = api_post("/api/contexts", {
                    "name": ctx_name,
                    "content": context_content,
                    "context_type": "code",
                    "project_id": state.CURRENT_PROJECT_ID
                })
                context = ctx_result.get("context", ctx_result)
                context_id = context.get("id")

                # Link context to task
                if context_id:
                    api_post(f"/api/contexts/task/{task_uuid}", {"context_id": context_id})
                    output += f"\n\n📎 Context created and linked: {ctx_name}"

            except Exception as ctx_err:
                output += f"\n\n⚠️ Could not create context: {str(ctx_err)}"

        # Create notification for task completion
        try:
            notif_title = f"Task #{task_id} completed"
            notif_message = summary[:200]
            if github_pr_url:
                notif_message += f"\nPR: {github_pr_url}"

            # Link to the generated context if available, otherwise PR
            notif_link = None
            if context_id:
                notif_link = f"/context?id={context_id}"
            elif github_pr_url:
                notif_link = github_pr_url

            api_post("/api/notifications", {
                "project_id": state.CURRENT_PROJECT_ID,
                "type": "success",
                "title": notif_title,
                "message": notif_message,
                "link": notif_link
            })
        except Exception:
            pass  # Non-critical

        return output
    except Exception as e:
        return f"Error completing task: {str(e)}"


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
    try:
        import json as _json

        artifact = {"type": artifact_type, "body": body}
        if platform:
            artifact["platform"] = platform
        if title:
            artifact["title"] = title
        if subject:
            artifact["subject"] = subject
        if to:
            artifact["to"] = to
        if url:
            artifact["url"] = url
        if filename:
            artifact["filename"] = filename
        if metadata:
            try:
                artifact["metadata"] = _json.loads(metadata)
            except _json.JSONDecodeError:
                pass

        # Fetch current artifacts
        result = api_get(f"/api/tasks/{task_id}")
        task = result.get("task", result)
        current_artifacts = task.get("artifacts") or []

        # Append new artifact
        current_artifacts.append(artifact)

        # Update task
        api_put(f"/api/tasks/{task_id}", {"artifacts": current_artifacts})

        count = len(current_artifacts)
        return f"✅ Added {artifact_type} artifact to task (total: {count}). The Decision Preparer will package this into a decision when the task moves to review."
    except Exception as e:
        return f"Error adding artifact: {str(e)}"


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
