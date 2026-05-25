"""
Project management tools.
"""

from .. import state
from ..api_client import api_get, api_post
from ..mcp_app import mcp


@mcp.tool()
def kompany_project_list() -> str:
    """List all projects available to you."""
    try:
        result = api_get("/api/projects")
        projects = result.get("projects", [])

        if not projects:
            return "No projects found."

        output = "# Projects\n\n"
        for p in projects:
            focus_marker = " 👈 **FOCUSED**" if str(p.get("id")) == str(state.CURRENT_PROJECT_ID) else ""
            output += f"- **{p.get('name')}** (ID: `{p.get('id')}`){focus_marker}\n"
            if p.get("description"):
                output += f"   {p.get('description')[:80]}\n"

        if state.CURRENT_PROJECT_ID:
            output += f"\n---\n**Current focus:** {state.CURRENT_PROJECT_NAME}\n"
        else:
            output += f"\n---\n⚠️ No project focused. Use `kompany_project_focus <id>` to set one.\n"

        return output
    except Exception as e:
        return f"Error fetching projects: {str(e)}"


@mcp.tool()
def kompany_project_focus(project_id: str) -> str:
    """Set the project in focus. All operations will be scoped to this project.

    Args:
        project_id: The UUID of the project to focus on
    """
    try:
        # Fetch the project to validate and get its name
        result = api_get(f"/api/projects/{project_id}")
        project = result.get("project", {})

        if not project:
            return f"❌ Project not found: {project_id}"

        state.CURRENT_PROJECT_ID = str(project_id)
        state.CURRENT_PROJECT_NAME = project.get("name", "Unknown")

        return f"""# 🎯 Project Focused

**Project:** {state.CURRENT_PROJECT_NAME}
**ID:** {state.CURRENT_PROJECT_ID}

All operations are now scoped to this project:
- Tasks you create will be in this project
- Task lists will show only this project's tasks
- Same for machines, versions, team members, and domains

Use `kompany_project_clear` to remove focus."""
    except Exception as e:
        return f"Error focusing project: {str(e)}"


@mcp.tool()
def kompany_project_clear() -> str:
    """Clear the current project focus."""
    state.CURRENT_PROJECT_ID = None
    state.CURRENT_PROJECT_NAME = None
    return "✅ Project focus cleared. Use `kompany_project_focus <id>` to set a new project."


@mcp.tool()
def kompany_org_list() -> str:
    """List all organizations."""
    try:
        result = api_get("/api/organizations")
        orgs = result.get("organizations", [])

        if not orgs:
            return "No organizations found."

        output = "# Organizations\n\n"
        for o in orgs:
            output += f"- **{o.get('name')}** (ID: `{o.get('id')}`)\n"
            if o.get("description"):
                output += f"   {o.get('description')[:80]}\n"

        return output
    except Exception as e:
        return f"Error fetching organizations: {str(e)}"


@mcp.tool()
def kompany_project_create(
    name: str,
    description: str = "",
    organization_id: str = "",
    git_remote_url: str = "",
    focus: bool = True,
) -> str:
    """Create a new cloud project and (by default) focus it.

    This creates the project record in the Kompany cloud (kompany.dev) so it
    immediately shows up in `kompany_project_list` and can hold domains,
    machines, metrics, tasks, etc. It does NOT create a local folder — use
    `kompany_project_create_folder` for that, then link via git_remote_url.

    Args:
        name: Project display name (e.g. "p_91_leave_safe_zone").
        description: Optional short description.
        organization_id: Org UUID to create the project under. Defaults to the
            authenticated org. Use `kompany_org_list` to see options.
        git_remote_url: Optional git remote to associate with the project.
        focus: Whether to focus the new project after creating it (default: True).

    Returns:
        The created project's name and ID.
    """
    try:
        org_id = organization_id or state.ORG_ID
        if not org_id:
            return (
                "❌ No organization_id available. Pass one explicitly or run "
                "`kompany_org_list` to find your org ID."
            )

        payload = {
            "name": name,
            "description": description,
            "organization_id": org_id,
        }
        if git_remote_url:
            payload["git_remote_url"] = git_remote_url

        result = api_post("/api/projects", payload)
        project = result.get("project", {})
        project_id = project.get("id")

        if not project_id:
            return f"❌ Project creation returned no ID. Response: {result}"

        focus_note = ""
        if focus:
            state.CURRENT_PROJECT_ID = str(project_id)
            state.CURRENT_PROJECT_NAME = project.get("name", name)
            focus_note = "\n\n🎯 This project is now focused — all operations are scoped to it."

        return f"""# ✅ Project Created

**Project:** {project.get('name', name)}
**ID:** `{project_id}`
**Org:** `{org_id}`{focus_note}"""

    except Exception as e:
        return f"Error creating project: {str(e)}"


@mcp.tool()
def kompany_project_create_folder(
    base_path: str,
    project_name: str,
    machine_id: str,
    init_git: bool = True
) -> str:
    """Create a new project folder on a local machine.

    Args:
        base_path: The base directory to create the project in (e.g., ~/Code)
        project_name: The project name (will be sanitized for folder name)
        machine_id: The machine ID of the connected relay node
        init_git: Whether to initialize a git repository (default: True)

    Returns:
        Information about the created folder including path.

    Example:
        kompany_project_create_folder("~/Code", "my-saas-app", "machine-abc123")
        → Creates ~/Code/my-saas-app/ with git initialized
    """
    try:
        # Call the relay endpoint which forwards to the local server
        result = api_post(
            f"/api/relay/{machine_id}/local-claude/projects/create-project-folder",
            {
                "base_path": base_path,
                "project_name": project_name,
                "init_git": init_git
            }
        )

        path = result.get("path", "unknown")
        folder_name = result.get("folder_name", "unknown")
        git_initialized = result.get("git_initialized", False)

        output = f"""# Project Folder Created

**Folder:** `{folder_name}`
**Full Path:** `{path}`
**Git Initialized:** {"Yes" if git_initialized else "No"}

The folder is ready for your project files."""

        return output

    except Exception as e:
        return f"Error creating project folder: {str(e)}"


@mcp.tool()
def kompany_project_scan(path: str, machine_id: str) -> str:
    """Scan a folder for project configuration on a local machine.

    Detects git remote, framework, deployment platform, and dev server settings
    from configuration files in the folder.

    Args:
        path: The folder path to scan
        machine_id: The machine ID of the connected relay node

    Returns:
        Detected configuration including git remote, framework, deployment platform,
        and dev server settings.
    """
    try:
        result = api_post(
            f"/api/relay/{machine_id}/local-claude/projects/scan-project",
            {"path": path}
        )

        git_remote = result.get("git_remote")
        framework = result.get("framework")
        deployment_platform = result.get("deployment_platform")
        dev_server = result.get("dev_server")

        output = "# Project Configuration Detected\n\n"

        if git_remote:
            output += f"**Git Remote:** `{git_remote}`\n"
        else:
            output += "**Git Remote:** Not detected\n"

        if framework:
            output += f"**Framework:** {framework}\n"
        else:
            output += "**Framework:** Not detected\n"

        if deployment_platform:
            output += f"**Deployment Platform:** {deployment_platform}\n"
        else:
            output += "**Deployment Platform:** Not detected\n"

        if dev_server:
            command = dev_server.get("command", "unknown")
            port = dev_server.get("port", "unknown")
            output += f"**Dev Server:** `npm run {command}` on port {port}\n"
        else:
            output += "**Dev Server:** Not detected\n"

        return output

    except Exception as e:
        return f"Error scanning project: {str(e)}"


@mcp.tool()
def kompany_project_list_folders(path: str, machine_id: str) -> str:
    """List folders in a directory on a local machine.

    Useful for browsing the file system to find project folders.

    Args:
        path: The directory path to list
        machine_id: The machine ID of the connected relay node

    Returns:
        List of folders with their names, paths, and whether they are projects or git repos.
    """
    try:
        result = api_post(
            f"/api/relay/{machine_id}/local-claude/projects/list-folders",
            {"path": path}
        )

        current_path = result.get("path", path)
        parent = result.get("parent", "")
        folders = result.get("folders", [])

        output = f"# Folders in `{current_path}`\n\n"
        output += f"**Parent:** `{parent}`\n\n"

        if not folders:
            output += "_No folders found_\n"
        else:
            for folder in folders:
                name = folder.get("name", "")
                is_project = folder.get("is_project", False)
                is_git = folder.get("is_git_repo", False)

                icons = []
                if is_project:
                    icons.append("P")
                if is_git:
                    icons.append("git")

                icon_str = f" [{', '.join(icons)}]" if icons else ""
                output += f"- `{name}`{icon_str}\n"

        return output

    except Exception as e:
        return f"Error listing folders: {str(e)}"
