"""
Service layer for the task-management MCP tools.

Holds the implementation behind the heavier ``@mcp.tool()`` wrappers in
``tasks.py`` — the workflow-guidance text, PR creation, completion bookkeeping
and artifact assembly — so the tool functions stay thin (schema + delegation)
while the orchestration lives here, decomposed into small named helpers.

Behaviour (return strings, side effects, ordering) is preserved exactly; this
module is a structural extraction only.
"""

import subprocess
import re

from .. import state
from ..api_client import api_get, api_post, api_put


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


# --- task_work ------------------------------------------------------------

VALID_WORKFLOWS = ["ask", "plan", "execute", "workspace"]


def _work_next_steps(task_id: int, workflow: str) -> str:
    """Build the workflow-specific 'Next Steps' guidance block."""
    if workflow == "ask":
        return """**Next Steps (Ask Workflow):**
1. Research/explore to answer the question
2. Use `kompany_task_log` to record findings
3. Use `kompany_task_update` to mark done when answered"""
    elif workflow == "plan":
        return """**Next Steps (Plan Workflow):**
1. Research the codebase and requirements
2. Create a plan/design document
3. Use `kompany_task_log` to record the plan
4. Get user approval before implementing
5. If approved, switch to execute workflow or create sub-tasks"""
    elif workflow == "workspace":
        return f"""**Next Steps (Workspace Workflow):**
1. Work on the task (research, analysis, writing, etc.)
2. Use `kompany_task_log(task_id={task_id}, content="...")` to record progress
3. Save outputs using `kompany_context_create(name="...", content="...", context_type="general")`
4. Complete: `kompany_task_complete(task_id={task_id}, summary="...")`

No worktree or PR needed — results are saved as Kompany contexts."""
    else:  # execute
        return f"""**Next Steps (Execute Workflow):**

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


def start_task_work(task_id: int, workflow: str = "execute") -> str:
    """Implementation behind ``kompany_task_work``."""
    # Validate workflow
    if workflow not in VALID_WORKFLOWS:
        return f"❌ Invalid workflow '{workflow}'. Must be one of: {', '.join(VALID_WORKFLOWS)}"

    try:
        # Start working on task (workflow is guidance only, not stored)
        result = api_post(f"/api/tasks/{task_id}/work")
        task = result.get("task", {})

        state.CURRENT_TASK_ID = task_id
        state.CURRENT_TASK_TITLE = task.get('title', 'Unknown')

        auto_log_activity("task_work_start", duration=1)

        next_steps = _work_next_steps(task_id, workflow)

        return f"""# Working on Task {task_id}: {task.get('title', 'Unknown')}

**Workflow:** {workflow.upper()}
**Status:** in_progress
**Description:** {task.get('description') or '(none)'}
**Version:** {task.get('version') or 'backlog'}

{next_steps}"""
    except Exception as e:
        return f"Error: {str(e)}"


# --- task_complete --------------------------------------------------------

def _create_pr(worktree_path: str):
    """Create a GitHub PR via the gh CLI.

    Returns ``(pr_url_or_None, status_output)``.
    """
    if not worktree_path:
        # No worktree = non-code task (workspace/ask/plan) — skip PR
        return None, "No worktree — skipping PR creation"

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
        github_pr_url = pr_match.group(0) if pr_match else None
        return github_pr_url, pr_output
    except FileNotFoundError:
        return None, "gh CLI not found - skipping PR creation"
    except subprocess.TimeoutExpired:
        return None, "gh pr create timed out"
    except Exception as e:
        return None, f"PR creation failed: {str(e)}"


def _create_completion_context(task_id, task_uuid, task_title, summary, files_changed, context_name):
    """Create a context summarizing the completed task and link it.

    Returns ``(context_id_or_None, ctx_name)``. May raise — the caller wraps
    this in a try/except so failures surface as a warning line.
    """
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

    return context_id, ctx_name


def _notify_completion(task_id, summary, github_pr_url, context_id):
    """Create a (non-critical) notification for task completion."""
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


def complete_task(
    task_id: int,
    summary: str,
    worktree_path: str = None,
    files_changed: str = None,
    context_name: str = None
) -> str:
    """Implementation behind ``kompany_task_complete``."""
    try:
        github_pr_url, pr_output = _create_pr(worktree_path)

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
                context_id, ctx_name = _create_completion_context(
                    task_id, task_uuid, task_title, summary, files_changed, context_name
                )
                if context_id:
                    output += f"\n\n📎 Context created and linked: {ctx_name}"
            except Exception as ctx_err:
                output += f"\n\n⚠️ Could not create context: {str(ctx_err)}"

        _notify_completion(task_id, summary, github_pr_url, context_id)

        return output
    except Exception as e:
        return f"Error completing task: {str(e)}"


# --- task_add_artifact ----------------------------------------------------

def _build_artifact(artifact_type, body, platform, title, subject, to, url, filename, metadata):
    """Assemble the artifact dict from the supplied fields."""
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

    return artifact


def add_task_artifact(
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
    """Implementation behind ``kompany_task_add_artifact``."""
    try:
        artifact = _build_artifact(
            artifact_type, body, platform, title, subject, to, url, filename, metadata
        )

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
