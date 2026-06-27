"""
Git merge/diff/editor operations for the merge routes.

Pure logic extracted from ``routes/merge.py`` so the route handlers stay thin.
These functions raise ``fastapi.HTTPException`` with the same status codes and
details the routes previously raised, so HTTP behavior is preserved exactly.
"""

import os
import platform
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException

from ..config import get_default_working_dir
from ..git_utils import get_git_root, get_current_branch
from ..worktree import find_worktree_path, find_actual_branch


def _parse_commit_lines(stdout: str) -> List[dict]:
    """Parse ``git log --pretty=format:%H|%s|%an|%ar`` output into commit dicts."""
    commits = []
    for line in stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('|', 3)
        if len(parts) >= 4:
            commits.append({
                "hash": parts[0][:7],
                "message": parts[1][:50] + ('...' if len(parts[1]) > 50 else ''),
                "author": parts[2],
                "date": parts[3]
            })
    return commits


def get_commits(git_root: str, ref: str, limit: int = 5) -> List[dict]:
    """Get commits from a ref with details."""
    try:
        result = subprocess.run(
            ["git", "log", ref, f"-{limit}", "--pretty=format:%H|%s|%an|%ar"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return []
        return _parse_commit_lines(result.stdout)
    except Exception:
        return []


def get_unique_commits(git_root: str, feature_branch: str, base_branch: str) -> List[dict]:
    """Get commits that are in feature but not in base."""
    try:
        result = subprocess.run(
            ["git", "log", f"{base_branch}..{feature_branch}", "--pretty=format:%H|%s|%an|%ar"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return []
        return _parse_commit_lines(result.stdout)
    except Exception:
        return []


def build_merge_preview(task_number: int, branch: str, path: Optional[str] = None) -> dict:
    """Get commit info for merge preview visualization.

    Args:
        path: Optional project path to use instead of default working directory.
    """
    work_dir = path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    current_branch = get_current_branch(git_root)

    feature_commits = get_unique_commits(git_root, branch, current_branch)
    main_commits = get_commits(git_root, current_branch, 3)

    return {
        "target_branch": current_branch,
        "source_branch": branch,
        "feature_commits": feature_commits,
        "main_commits": main_commits
    }


def compute_branch_diff(
    branch: str,
    task_number: Optional[int] = None,
    worktree_path: Optional[str] = None,
) -> dict:
    """Get diff between a branch and main."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    # Use provided worktree_path if available (most reliable)
    diff_cwd = git_root
    if worktree_path and os.path.isdir(worktree_path):
        diff_cwd = worktree_path
        print(f"[Diff] Using provided worktree path: {worktree_path}")
    else:
        # Try to extract task number from branch name if not provided
        # Branch format: task/353-some-title or task-353-some-id
        if not task_number and branch:
            match = re.search(r'task[/-](\d+)', branch)
            if match:
                task_number = int(match.group(1))

        # Try to find worktree for this task - that's where the actual changes are
        if task_number:
            found_worktree = find_worktree_path(task_number)
            if found_worktree:
                diff_cwd = found_worktree
                print(f"[Diff] Using found worktree path: {found_worktree}")

    try:
        # Get diff between main and the branch
        # When in worktree, compare HEAD (current changes) against main
        if diff_cwd != git_root:
            # In worktree: diff main against current HEAD
            result = subprocess.run(
                ["git", "diff", "main...HEAD"],
                cwd=diff_cwd,
                capture_output=True,
                text=True
            )
            if result.returncode != 0 or not result.stdout.strip():
                # Fallback: diff main against working directory
                result = subprocess.run(
                    ["git", "diff", "main"],
                    cwd=diff_cwd,
                    capture_output=True,
                    text=True
                )
        else:
            # In main repo: diff main against branch
            result = subprocess.run(
                ["git", "diff", "main..." + branch],
                cwd=git_root,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                # Try without the ... syntax
                result = subprocess.run(
                    ["git", "diff", "main", branch],
                    cwd=git_root,
                    capture_output=True,
                    text=True
                )

        return {"diff": result.stdout or "No changes", "branch": branch}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def open_path_in_editor(
    task_number: Optional[int] = None,
    path: Optional[str] = None,
    local_path: Optional[str] = None,
) -> dict:
    """Open a worktree or path in VS Code."""
    print(f"[OpenInEditor] task_number={task_number}, path={path}, local_path={local_path}")
    target_path = None

    if path:
        target_path = Path(path)
    elif task_number:
        # If local_path provided, search there; otherwise use default
        if local_path:
            worktrees_dir = Path(local_path) / ".worktrees"
            print(f"[OpenInEditor] Searching in {worktrees_dir}, exists={worktrees_dir.exists()}")
            if worktrees_dir.exists():
                prefix = f"task-{task_number}-"
                matching_dirs = []
                for d in worktrees_dir.iterdir():
                    if d.is_dir() and d.name.startswith(prefix):
                        matching_dirs.append(d)
                        print(f"[OpenInEditor] Found matching dir: {d.name}")
                if matching_dirs:
                    # Get most recently modified
                    matching_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                    target_path = matching_dirs[0]
                    print(f"[OpenInEditor] Selected: {target_path}")

        # Fallback to default search
        if not target_path:
            target_path = find_worktree_path(task_number)

        if not target_path:
            raise HTTPException(status_code=404, detail=f"No worktree found for task {task_number}")
    else:
        raise HTTPException(status_code=400, detail="Either task_number or path required")

    if isinstance(target_path, str):
        target_path = Path(target_path)

    if not target_path.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {target_path}")

    try:
        # Try to open in VS Code
        result = subprocess.run(
            ["code", str(target_path)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # VS Code not found, try 'open' on macOS
            if platform.system() == "Darwin":
                subprocess.run(["open", str(target_path)])
            else:
                raise HTTPException(status_code=500, detail="Could not open editor")

        return {"success": True, "path": str(target_path)}
    except FileNotFoundError:
        # VS Code not in PATH
        if platform.system() == "Darwin":
            subprocess.run(["open", str(target_path)])
            return {"success": True, "path": str(target_path)}
        raise HTTPException(status_code=500, detail="VS Code not found in PATH")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_merge_source(
    git_root: str,
    work_dir: str,
    task_number: int,
    requested_source: Optional[str],
) -> Optional[str]:
    """Resolve the source branch to merge, deriving from the worktree if needed.

    Mirrors the original inline resolution: verify the requested branch exists
    (fetching from remote / falling back to ``origin/<branch>`` if not), then
    fall back to the worktree's actual branch and finally its HEAD.
    """
    # Get source branch - either from request or derive from worktree
    source = requested_source

    # Helper: check if a branch ref resolves in git_root
    def branch_resolvable(branch_name):
        v = subprocess.run(
            ["git", "rev-parse", "--verify", branch_name],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        return v.returncode == 0

    # Verify the branch actually exists in git
    if source and not branch_resolvable(source):
        print(f"[Merge] Branch '{source}' not found locally, trying to fetch from remote...")
        # Try fetching the branch from remote
        fetch = subprocess.run(
            ["git", "fetch", "origin", f"{source}:{source}"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if fetch.returncode == 0 and branch_resolvable(source):
            print(f"[Merge] Successfully fetched '{source}' from remote")
        else:
            # Try origin/<branch> as a fallback ref
            if branch_resolvable(f"origin/{source}"):
                print(f"[Merge] Using remote-tracking ref 'origin/{source}'")
                source = f"origin/{source}"
            else:
                print(f"[Merge] Branch '{source}' not found, trying to derive from worktree for task {task_number}")
                source = None

    if not source:
        # Try to find the actual branch from worktree (pass project path for correct repo)
        actual = find_actual_branch(task_number)
        if actual:
            # Verify the found branch resolves in our git_root
            if branch_resolvable(actual):
                source = actual
            elif branch_resolvable(f"origin/{actual}"):
                source = f"origin/{actual}"
        if not source:
            # Last resort: check worktree HEAD directly
            worktree_path = find_worktree_path(task_number, project_path=work_dir)
            if worktree_path:
                try:
                    result = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=worktree_path,
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0:
                        candidate = result.stdout.strip()
                        if candidate and candidate != "HEAD" and branch_resolvable(candidate):
                            source = candidate
                except Exception:
                    pass

    return source


def merge_branch(
    task_number: int,
    branch: Optional[str] = None,
    target_branch: Optional[str] = None,
    path: Optional[str] = None,
) -> dict:
    """Merge a worktree branch into the target branch.

    Args:
        branch: Optional source branch. If not provided, will be derived from worktree.
        path: Optional project path to use instead of default working directory.
    """
    work_dir = path or get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    target = target_branch or get_current_branch(git_root)

    source = _resolve_merge_source(git_root, work_dir, task_number, branch)

    if not source:
        raise HTTPException(status_code=400, detail="Branch name required - could not derive from worktree")

    try:
        # Clean up any leftover merge/conflict state before starting
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        # Reset index in case merge --abort wasn't enough (e.g. unresolved index entries)
        subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        # Checkout target branch
        result = subprocess.run(
            ["git", "checkout", target],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to checkout {target}: {result.stderr}")

        # Merge source branch (auto-resolve conflicts favoring incoming branch)
        result = subprocess.run(
            ["git", "merge", source, "--no-edit", "-X", "theirs"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # If branch can't be resolved, try fetching from remote and retry
            if "not something we can merge" in result.stderr:
                print(f"[Merge] Branch '{source}' not mergeable, attempting fetch from origin...")
                # Abort any partial merge state
                subprocess.run(["git", "merge", "--abort"], cwd=git_root, capture_output=True, text=True)
                # Fetch and retry
                clean_source = source.replace("origin/", "")
                subprocess.run(
                    ["git", "fetch", "origin", f"{clean_source}:{clean_source}"],
                    cwd=git_root,
                    capture_output=True,
                    text=True
                )
                result = subprocess.run(
                    ["git", "merge", clean_source, "--no-edit", "-X", "theirs"],
                    cwd=git_root,
                    capture_output=True,
                    text=True
                )
            if result.returncode != 0:
                raise HTTPException(status_code=500, detail=f"Merge failed: {result.stderr}")

        # Check if conflicts were auto-resolved
        auto_resolved = "Auto-merging" in result.stdout and "CONFLICT" not in result.stdout

        return {
            "success": True,
            "status": "merged",
            "message": f"Successfully merged {source} into {target}",
            "source_branch": source,
            "target_branch": target,
            "auto_resolved_conflicts": not auto_resolved if "Auto-merging" in result.stdout else False,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
