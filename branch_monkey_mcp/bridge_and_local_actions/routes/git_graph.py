"""Branch-graph construction for the local git endpoints.

Pure logic extracted from ``routes/git.py``: builds the commit/branch graph
data used by the visual branch-graph view. Split out of the route handler so
each step is small, named and independently testable. The HTTP layer in
``git.py`` resolves the git root and translates failures to ``HTTPException``;
everything here is plain subprocess/data work that raises normal exceptions.
"""

import os
import subprocess
import time
from typing import List, Optional

# Lane/branch colors. main/master always get green; the rest cycle.
MAIN_COLOR = "#22c55e"
BRANCH_COLORS = ["#22c55e", "#3b82f6", "#f97316", "#a855f7", "#ec4899", "#14b8a6"]

# Task branches older than this (and not on main) are treated as stale and
# dropped — this catches squash-merged branches that `--merged` misses.
STALE_DAYS = 14


def _collect_branches(git_root: str) -> dict:
    """Map every relevant branch to its head commit and a display color."""
    branches_result = subprocess.run(
        ["git", "branch", "-a", "--format=%(refname:short)|%(objectname)|%(upstream:short)"],
        cwd=git_root,
        capture_output=True,
        text=True
    )

    branches = {}
    color_idx = 0

    for line in branches_result.stdout.strip().split('\n'):
        if not line or line.startswith('origin/HEAD'):
            continue
        parts = line.split('|')
        if len(parts) >= 2:
            branch_name = parts[0]
            commit_sha = parts[1]
            # Skip remote tracking refs that duplicate local branches
            if branch_name.startswith('origin/'):
                local_name = branch_name.replace('origin/', '')
                if local_name in branches:
                    continue

            # Assign color (main/master get green)
            if branch_name in ['main', 'master']:
                color = MAIN_COLOR  # Green for main
            else:
                color = BRANCH_COLORS[color_idx % len(BRANCH_COLORS)]
                color_idx += 1

            branches[branch_name] = {
                "name": branch_name,
                "head": commit_sha,
                "color": color
            }

    return branches


def _drop_merged_branches(git_root: str, branches: dict) -> set:
    """Remove branches already merged into main/master; return the merged set."""
    main_ref = "main" if "main" in branches else ("master" if "master" in branches else None)
    merged_branches = set()
    if main_ref:
        merged_result = subprocess.run(
            ["git", "branch", "-a", "--merged", main_ref, "--format=%(refname:short)"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if merged_result.returncode == 0:
            for line in merged_result.stdout.strip().split('\n'):
                name = line.strip()
                if name and name not in (main_ref, f"origin/{main_ref}", "origin/HEAD"):
                    if name.startswith('origin/'):
                        merged_branches.add(name.replace('origin/', '', 1))
                    merged_branches.add(name)

        # Remove merged branches from the dict
        for name in list(branches.keys()):
            if name in merged_branches or name.replace('origin/', '', 1) in merged_branches:
                if name not in ('main', 'master'):
                    del branches[name]

    return merged_branches


def _drop_stale_task_branches(git_root: str, branches: dict, merged_branches: set) -> None:
    """Drop stale task branches (catches squash-merged branches).

    Gets the committer date for each branch tip in a single git call and
    removes ``task/`` / ``task-`` branches whose tip is older than STALE_DAYS.
    """
    stale_cutoff = int(time.time()) - (STALE_DAYS * 86400)

    dates_result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)|%(committerdate:unix)",
         "refs/heads/", "refs/remotes/"],
        cwd=git_root,
        capture_output=True,
        text=True
    )
    branch_dates = {}
    for line in dates_result.stdout.strip().split('\n'):
        if '|' in line:
            parts = line.split('|', 1)
            try:
                branch_dates[parts[0]] = int(parts[1])
            except (ValueError, IndexError):
                pass

    for name in list(branches.keys()):
        if name in ('main', 'master'):
            continue
        # Only apply staleness filter to task branches
        if not (name.startswith('task/') or name.startswith('task-')):
            continue
        commit_date = branch_dates.get(name, 0)
        if commit_date < stale_cutoff:
            merged_branches.add(name)
            del branches[name]


def _load_commits(git_root: str, branches: dict, limit: int, merged_branches: set) -> List[dict]:
    """Load commits for the kept branches and parse them into graph nodes."""
    log_cmd = ["git", "log", f"-{limit}", "--pretty=format:%H|%P|%D|%s|%an|%ar|%ai", "--topo-order"]
    for b in branches:
        log_cmd.append(b)
    commits_result = subprocess.run(
        log_cmd,
        cwd=git_root,
        capture_output=True,
        text=True
    )

    commits = []

    for line in commits_result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('|', 6)
        if len(parts) >= 7:
            sha = parts[0]
            parents = parts[1].split() if parts[1] else []
            refs = parts[2]
            message = parts[3]
            author = parts[4]
            relative_date = parts[5]
            date = parts[6]

            # Parse refs to find branch names and tags
            branch_refs = []
            tag_refs = []
            is_head = False
            is_main = False
            if refs:
                for ref in refs.split(', '):
                    ref = ref.strip()
                    if ref == 'HEAD':
                        is_head = True
                    elif ref.startswith('HEAD -> '):
                        is_head = True
                        branch_refs.append(ref.replace('HEAD -> ', ''))
                    elif ref.startswith('tag: '):
                        tag_refs.append(ref.replace('tag: ', ''))
                    elif not ref.startswith('origin/'):
                        branch_refs.append(ref)

                    if 'main' in ref or 'master' in ref:
                        is_main = True

            # Filter out merged branches from commit refs
            branch_refs = [b for b in branch_refs if b not in merged_branches]

            commits.append({
                "hash": sha,
                "short_hash": sha[:7],
                "parents": parents,
                "parent_short": [p[:7] for p in parents],
                "branches": branch_refs,
                "tags": tag_refs,
                "is_head": is_head,
                "is_main": is_main,
                "message": message,
                "author": author,
                "relative_date": relative_date,
                "date": date,
                "refs": refs
            })

    return commits


def _assign_lanes(branches: dict, commits: List[dict]) -> tuple:
    """Assign each branch and commit a lane/color for visualization.

    main/master gets lane 0; other branches get the next free lane. Mutates
    each commit in ``commits`` with its ``lane`` and ``color``. Returns
    ``(lanes, main_branch)``.
    """
    lanes = {}

    # First pass: assign main/master to lane 0
    main_branch = None
    for branch_name in branches:
        if branch_name in ['main', 'master']:
            main_branch = branch_name
            lanes[branch_name] = 0
            break

    # Assign other branches to lanes
    next_lane = 1
    for branch_name in branches:
        if branch_name not in lanes:
            lanes[branch_name] = next_lane
            next_lane += 1

    # For each commit, determine its lane based on first branch ref
    for commit in commits:
        if commit['branches']:
            # Use first branch as the commit's lane
            for branch in commit['branches']:
                if branch in lanes:
                    commit['lane'] = lanes[branch]
                    commit['color'] = branches.get(branch, {}).get('color', '#6366f1')
                    break

        if 'lane' not in commit:
            # Commits not on a branch tip - try to find their branch
            # by looking at which branch head is an ancestor
            commit['lane'] = 0  # Default to main lane
            commit['color'] = '#6b7280'  # Gray for non-tip commits

    return lanes, main_branch


def _repo_identity(git_root: str) -> tuple:
    """Resolve ``(repo_name, remote_url)`` for repo identification."""
    remote_url = None
    repo_name = os.path.basename(git_root)
    try:
        remote_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if remote_result.returncode == 0:
            remote_url = remote_result.stdout.strip()
            # Extract repo name from URL (e.g. "org/repo" from github.com/org/repo.git)
            if remote_url:
                clean = remote_url.rstrip('/').removesuffix('.git')
                parts = clean.split('/')
                if len(parts) >= 2:
                    repo_name = '/'.join(parts[-2:])
    except Exception:
        pass

    return repo_name, remote_url


def build_branch_graph(git_root: str, limit: int = 50) -> dict:
    """Build commit + branch graph data for visualization.

    Returns commits with parent relationships, branch info and lane
    assignments for rendering a visual branch graph like GitHub/GitKraken.
    """
    # Get all branches with their current commits
    branches = _collect_branches(git_root)

    # Filter out branches already merged into main/master
    merged_branches = _drop_merged_branches(git_root, branches)

    # Also filter stale task branches (catches squash-merged branches).
    _drop_stale_task_branches(git_root, branches, merged_branches)

    # Get commits only for the branches we kept (not --all)
    commits = _load_commits(git_root, branches, limit, merged_branches)

    # Calculate lane assignments for visualization
    lanes, main_branch = _assign_lanes(branches, commits)

    # Get remote URL for repo identification
    repo_name, remote_url = _repo_identity(git_root)

    return {
        "branches": list(branches.values()),
        "commits": commits,
        "lanes": lanes,
        "main_branch": main_branch or "main",
        "repo_name": repo_name,
        "remote_url": remote_url,
        "git_root": git_root
    }
