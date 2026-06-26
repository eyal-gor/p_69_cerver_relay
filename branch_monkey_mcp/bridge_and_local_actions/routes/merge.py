"""
Merge and diff endpoints for the local server.
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .merge_ops import (
    build_merge_preview,
    compute_branch_diff,
    merge_branch,
    open_path_in_editor,
)

router = APIRouter()


class MergeRequest(BaseModel):
    task_number: int
    branch: Optional[str] = None  # Optional - will be derived from worktree if not provided
    target_branch: Optional[str] = None
    path: Optional[str] = None  # Project path to use instead of default


class OpenInEditorRequest(BaseModel):
    task_number: Optional[int] = None
    path: Optional[str] = None
    local_path: Optional[str] = None  # Base project path for finding worktrees


@router.get("/merge-preview")
def merge_preview(task_number: int, branch: str, path: Optional[str] = None):
    """Get commit info for merge preview visualization.

    Args:
        path: Optional project path to use instead of default working directory.
    """
    return build_merge_preview(task_number, branch, path)


@router.get("/diff")
def get_branch_diff(branch: str, task_number: Optional[int] = None, worktree_path: Optional[str] = None):
    """Get diff between a branch and main."""
    return compute_branch_diff(branch, task_number, worktree_path)


@router.post("/open-in-editor")
def open_in_editor(request: OpenInEditorRequest):
    """Open a worktree or path in VS Code."""
    return open_path_in_editor(request.task_number, request.path, request.local_path)


@router.post("/merge")
def merge_worktree_branch(request: MergeRequest):
    """Merge a worktree branch into the target branch.

    Args:
        request.branch: Optional source branch. If not provided, will be derived from worktree.
        request.path: Optional project path to use instead of default working directory.
    """
    return merge_branch(
        request.task_number,
        request.branch,
        request.target_branch,
        request.path,
    )
