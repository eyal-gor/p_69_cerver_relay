"""
Project folder management endpoints for the local server.

Handles creating project folders with auto-numbering (p_X_name format),
scanning folders for configuration, and browsing the file system.

The HTTP handlers below are thin delegations; the filesystem/git/scan logic
lives in the sibling ``projects_ops`` module.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from . import projects_ops

router = APIRouter()


class CreateProjectFolderRequest(BaseModel):
    """Request to create a new project folder."""
    base_path: str  # e.g., ~/Code
    project_name: str  # e.g., my-saas-app
    init_git: bool = True  # Initialize git repo


class ScanProjectRequest(BaseModel):
    """Request to scan a folder for project configuration."""
    path: str


class ListFoldersRequest(BaseModel):
    """Request to list folders in a directory."""
    path: str


@router.post("/create-project-folder")
def create_project_folder(request: CreateProjectFolderRequest):
    """
    Create a new project folder.

    Returns:
        {
            path: Full path to created folder,
            folder_name: Just the folder name,
            git_initialized: Whether git was initialized
        }
    """
    return projects_ops.create_project_folder(
        request.base_path, request.project_name, request.init_git
    )


@router.post("/scan-project")
def scan_project(request: ScanProjectRequest):
    """
    Scan a folder for project configuration.

    Detects:
    - Git remote URL from .git/config
    - Framework from package.json
    - Deployment platform from config files (wrangler.toml, vercel.json, etc.)
    - Dev server command and port from package.json

    Returns:
        {
            git_remote: URL of git remote (if any),
            framework: Detected framework name,
            deployment_platform: Detected deployment platform,
            dev_server: { command, port } if detected,
            raw_config: Object with detected config file contents
        }
    """
    return projects_ops.scan_project(request.path)


@router.post("/list-folders")
def list_folders(request: ListFoldersRequest):
    """
    List folders in a directory for the folder browser.

    Returns:
        {
            path: The requested path (expanded),
            parent: Parent directory path,
            folders: List of { name, path, is_git_repo }
        }
    """
    return projects_ops.list_folders(request.path)


@router.get("/home-directory")
def get_home_dir():
    """
    Get the home directory configured for this relay.

    Returns:
        {
            home_directory: The configured home directory,
            default_code_path: Suggested default code path (~/Code)
        }
    """
    return projects_ops.get_home_dir()
