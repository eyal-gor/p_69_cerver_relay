"""
Git status and commit endpoints for the local server.
"""

import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...computer_runtime.git_ops import (
    get_commit_diff as runtime_get_commit_diff,
    get_git_status_summary,
    list_recent_commits,
    resolve_git_root,
)
from ..config import get_default_working_dir
from ..git_utils import is_git_repo, get_git_root, get_current_branch
from .git_graph import build_branch_graph

router = APIRouter()


class PushRequest(BaseModel):
    path: Optional[str] = None
    branch: Optional[str] = None


class PullRequest(BaseModel):
    path: Optional[str] = None


class TagRequest(BaseModel):
    path: Optional[str] = None
    tag_name: str
    commit_sha: str


@router.get("/git-status")
def get_git_status(path: str = None):
    """Get git status for a directory.

    Args:
        path: Directory path to check. Defaults to working directory.

    Returns:
        {
            is_clean: bool - True if working tree is clean,
            changes_count: int - Number of changed files,
            branch: str - Current branch name,
            staged: int - Number of staged files,
            unstaged: int - Number of unstaged files,
            untracked: int - Number of untracked files
        }
    """
    return get_git_status_summary(path)


@router.get("/local-claude/commits")
def list_commits(limit: int = 10, branch: Optional[str] = None, all_branches: bool = False):
    """List recent commits."""
    try:
        return list_recent_commits(limit=limit, branch=branch, all_branches=all_branches)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/local-claude/commit-diff/{sha}")
def get_commit_diff(sha: str):
    """Get diff for a specific commit."""
    try:
        return runtime_get_commit_diff(sha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/local-claude/branch-graph")
def get_branch_graph(limit: int = 50, path: Optional[str] = None):
    """Get commits with branch graph data for visualization.

    Returns commits with parent relationships and branch info for
    rendering a visual branch graph like GitHub/GitKraken.

    Args:
        limit: Maximum number of commits to return
        path: Optional directory path to use. Defaults to working directory.
    """
    work_dir = path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        return build_branch_graph(git_root, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/checkout/{sha}")
def checkout_commit(sha: str, auto_stash: bool = True, path: Optional[str] = None):
    """Checkout to a specific commit.

    This will checkout the working directory to the specified commit.
    If auto_stash is True (default), uncommitted changes will be automatically
    stashed before checkout and can be restored later.

    Args:
        sha: The commit hash to checkout
        auto_stash: Whether to auto-stash uncommitted changes (default True)
        path: Optional directory path to use. Defaults to working directory.
    """
    work_dir = path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    stashed = False

    try:
        # Check if there are uncommitted changes
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        has_changes = bool(status_result.stdout.strip())

        if has_changes:
            if auto_stash:
                # Auto-stash changes with a descriptive message
                stash_result = subprocess.run(
                    ["git", "stash", "push", "-m", f"Auto-stash before checkout to {sha[:7]}"],
                    cwd=git_root,
                    capture_output=True,
                    text=True
                )
                if stash_result.returncode == 0:
                    stashed = True
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to stash changes: {stash_result.stderr}"
                    )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot checkout: you have uncommitted changes. Please commit or stash them first."
                )

        # Checkout to the commit
        result = subprocess.run(
            ["git", "checkout", sha],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # If checkout failed and we stashed, restore the stash
            if stashed:
                subprocess.run(["git", "stash", "pop"], cwd=git_root, capture_output=True)
            raise HTTPException(status_code=400, detail=f"Checkout failed: {result.stderr}")

        # Get current branch/commit info after checkout
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        current_ref = branch_result.stdout.strip()

        message = f"Restored to version {sha[:7]}"
        if stashed:
            message += " (your work-in-progress was saved)"

        return {
            "success": True,
            "sha": sha,
            "current_ref": current_ref,
            "detached": current_ref == "HEAD",
            "stashed": stashed,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        # If something failed and we stashed, try to restore
        if stashed:
            subprocess.run(["git", "stash", "pop"], cwd=git_root, capture_output=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/stash/pop")
def pop_stash():
    """Restore the most recent stashed changes.

    Use this to get back work-in-progress after restoring a version.
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        # Check if there's anything in the stash
        list_result = subprocess.run(
            ["git", "stash", "list"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if not list_result.stdout.strip():
            return {"success": True, "message": "No saved work to restore"}

        # Pop the stash
        result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to restore work: {result.stderr}"
            )

        return {
            "success": True,
            "message": "Your work-in-progress has been restored"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/local-claude/stash/list")
def list_stash():
    """List all stashed changes."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        result = subprocess.run(
            ["git", "stash", "list", "--pretty=format:%gd|%s|%ar"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        stashes = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('|', 2)
            if len(parts) >= 3:
                stashes.append({
                    "ref": parts[0],
                    "message": parts[1],
                    "relative_date": parts[2]
                })

        return {"stashes": stashes, "count": len(stashes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/push")
def git_push(req: PushRequest):
    """Push commits to remote."""
    work_dir = req.path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        cmd = ["git", "push"]
        if req.branch:
            cmd += ["origin", req.branch]

        result = subprocess.run(
            cmd,
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=result.stderr.strip())

        return {
            "success": True,
            "message": result.stderr.strip() or "Pushed successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/pull")
def git_pull(req: PullRequest):
    """Pull commits from remote."""
    work_dir = req.path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=result.stderr.strip())

        return {
            "success": True,
            "message": result.stdout.strip() or "Pulled successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/tag")
def git_tag(req: TagRequest):
    """Create a tag on a specific commit and push it."""
    work_dir = req.path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        # Create the tag
        result = subprocess.run(
            ["git", "tag", req.tag_name, req.commit_sha],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=result.stderr.strip())

        # Push the tag to remote
        push_result = subprocess.run(
            ["git", "push", "origin", req.tag_name],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if push_result.returncode != 0:
            # Tag was created locally but push failed — report both
            return {
                "success": True,
                "message": f"Tag '{req.tag_name}' created locally but push failed: {push_result.stderr.strip()}",
                "pushed": False
            }

        return {
            "success": True,
            "message": f"Tag '{req.tag_name}' created and pushed",
            "pushed": True
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
