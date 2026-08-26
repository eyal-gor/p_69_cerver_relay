"""
Logic for project folder management endpoints.

Pure (non-HTTP) helpers that back the thin route handlers in ``projects.py``.
These functions raise ``fastapi.HTTPException`` directly so the HTTP status
codes and detail messages stay byte-identical to the original inline route
bodies; the routes themselves become one-line delegations.
"""

import os
import re
import subprocess

from fastapi import HTTPException

from ..config import get_home_directory, find_dev_dir


def expand_path(path: str) -> str:
    """Expand ~ and environment variables in path."""
    return os.path.expanduser(os.path.expandvars(path))


def sanitize_project_name(name: str) -> str:
    """
    Sanitize project name for folder creation.
    Converts to lowercase, replaces spaces with hyphens, removes special chars.
    """
    # Convert to lowercase
    name = name.lower()
    # Replace spaces and underscores with hyphens
    name = re.sub(r'[\s_]+', '-', name)
    # Remove any characters that aren't alphanumeric or hyphens
    name = re.sub(r'[^a-z0-9-]', '', name)
    # Remove consecutive hyphens
    name = re.sub(r'-+', '-', name)
    # Remove leading/trailing hyphens
    name = name.strip('-')
    return name


def create_project_folder(base_path: str, project_name: str, init_git: bool):
    """
    Create a new project folder, optionally initializing a git repo.

    Returns:
        {
            path: Full path to created folder,
            folder_name: Just the folder name,
            git_initialized: Whether git was initialized
        }
    """
    base_path = expand_path(base_path)

    # Validate base path exists
    if not os.path.isdir(base_path):
        raise HTTPException(
            status_code=400,
            detail=f"Base path does not exist: {base_path}"
        )

    # Sanitize project name
    folder_name = sanitize_project_name(project_name)
    if not folder_name:
        raise HTTPException(
            status_code=400,
            detail="Project name results in empty folder name after sanitization"
        )

    full_path = os.path.join(base_path, folder_name)

    # Check if folder already exists
    if os.path.exists(full_path):
        raise HTTPException(
            status_code=409,
            detail=f"Folder already exists: {folder_name}"
        )

    try:
        # Create the folder
        os.makedirs(full_path)

        git_initialized = False

        # Initialize git if requested
        if init_git:
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=full_path,
                    capture_output=True,
                    check=True
                )
                git_initialized = True
            except subprocess.CalledProcessError:
                # Git init failed, but folder was created
                pass

        return {
            "path": full_path,
            "folder_name": folder_name,
            "git_initialized": git_initialized
        }

    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied creating folder: {full_path}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create folder: {str(e)}"
        )


def _detect_git_remote(path: str):
    """Return the origin remote URL for ``path``, or None if not detectable."""
    git_config_path = os.path.join(path, ".git", "config")
    if not os.path.exists(git_config_path):
        return None
    try:
        git_result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=path,
            capture_output=True,
            text=True
        )
        if git_result.returncode == 0:
            return git_result.stdout.strip()
    except Exception:
        pass
    return None


def _detect_framework(deps: dict):
    """Map a merged dependency dict to a framework display name, or None."""
    if "@sveltejs/kit" in deps:
        return "SvelteKit"
    elif "next" in deps:
        return "Next.js"
    elif "nuxt" in deps:
        return "Nuxt"
    elif "astro" in deps:
        return "Astro"
    elif "gatsby" in deps:
        return "Gatsby"
    elif "svelte" in deps:
        return "Svelte"
    elif "react" in deps:
        return "React"
    elif "vue" in deps:
        return "Vue"
    elif "express" in deps:
        return "Express"
    elif "fastify" in deps:
        return "Fastify"
    return None


def _detect_dev_server(package_json: dict, framework):
    """Detect the dev-server command/port from package.json scripts, or None."""
    scripts = package_json.get("scripts", {})
    dev_command = scripts.get("dev") or scripts.get("start")
    if not dev_command:
        return None

    # Try to detect port from command
    port_match = re.search(r'(?:--port|PORT=|:)(\d{4,5})', dev_command)
    port = int(port_match.group(1)) if port_match else 3000

    # Adjust default port based on framework
    if not port_match:
        if framework == "SvelteKit":
            port = 5173
        elif framework in ["Next.js", "Nuxt", "Gatsby"]:
            port = 3000
        elif framework == "Astro":
            port = 4321

    return {
        "command": "dev" if "dev" in scripts else "start",
        "port": port
    }


# (config_file, platform) pairs, checked in order; first match wins.
DEPLOYMENT_CONFIGS = [
    ("wrangler.toml", "Cloudflare Pages"),
    ("wrangler.json", "Cloudflare Pages"),
    ("vercel.json", "Vercel"),
    ("netlify.toml", "Netlify"),
    ("railway.json", "Railway"),
    ("railway.toml", "Railway"),
    ("fly.toml", "Fly.io"),
    ("render.yaml", "Render"),
]


def _detect_deployment(path: str, result: dict):
    """Detect deployment platform from config files and record raw config."""
    for config_file, platform in DEPLOYMENT_CONFIGS:
        config_path = os.path.join(path, config_file)
        if os.path.exists(config_path):
            result["deployment_platform"] = platform
            try:
                with open(config_path, 'r') as f:
                    content = f.read()
                    # Store first 2000 chars of config for reference
                    result["raw_config"][config_file] = content[:2000]
            except Exception:
                pass
            break  # Use first match


def scan_project(path: str):
    """
    Scan a folder for project configuration.

    Detects git remote, framework, deployment platform, and dev server command
    and port.

    Returns:
        {
            git_remote, framework, deployment_platform, dev_server, raw_config
        }
    """
    path = expand_path(path)

    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail=f"Directory does not exist: {path}"
        )

    result = {
        "git_remote": None,
        "framework": None,
        "deployment_platform": None,
        "dev_server": None,
        "raw_config": {}
    }

    # Detect git remote
    result["git_remote"] = _detect_git_remote(path)

    # Detect framework and dev server from package.json
    dev_dir, package_json = find_dev_dir(path)

    if package_json:
        result["raw_config"]["package_json"] = package_json
        # Store relative working_dir if dev scripts live in a subdirectory
        if os.path.abspath(dev_dir) != os.path.abspath(path):
            result["working_dir"] = os.path.relpath(dev_dir, path)

        # Detect framework from dependencies
        deps = {
            **package_json.get("dependencies", {}),
            **package_json.get("devDependencies", {})
        }
        result["framework"] = _detect_framework(deps)

        # Detect dev server
        result["dev_server"] = _detect_dev_server(package_json, result["framework"])

    # Detect deployment platform from config files
    _detect_deployment(path, result)

    return result


def list_folders(path: str):
    """
    List folders in a directory for the folder browser.

    Returns:
        {
            path: The requested path (expanded),
            parent: Parent directory path,
            folders: List of { name, path, is_git_repo }
        }
    """
    path = expand_path(path)

    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail=f"Directory does not exist: {path}"
        )

    folders = []

    try:
        entries = sorted(os.listdir(path))
        for entry in entries:
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path) and not entry.startswith('.'):
                folders.append({
                    "name": entry,
                    "path": full_path,
                    "is_git_repo": os.path.exists(os.path.join(full_path, ".git"))
                })
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied reading directory: {path}"
        )

    return {
        "path": path,
        "parent": os.path.dirname(path),
        "folders": folders
    }


def get_home_dir():
    """
    Get the home directory configured for this relay.

    Returns:
        {
            home_directory: The configured home directory,
            default_code_path: Suggested default code path (~/Code)
        }
    """
    home_dir = get_home_directory()
    default_code = expand_path("~/Code")

    return {
        "home_directory": home_dir,
        "default_code_path": default_code if os.path.isdir(default_code) else home_dir
    }
