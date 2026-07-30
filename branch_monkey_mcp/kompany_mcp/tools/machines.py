"""
Machine (automated business process) management tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def kompany_machine_list() -> str:
    """List all machines for the current project.

    Each machine shows its canvas badge number (#N) — users reference
    machines as "system #20", so keep the number with the name.
    Requires a project to be focused first using kompany_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first.\n\nUse `kompany_project_list` to see available projects."

    try:
        endpoint = f"/api/machines?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        machines = result.get("machines", [])

        if not machines:
            return f"No machines found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Machines (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for m in machines:
            status_icon = {"active": "🟢", "paused": "⏸️", "draft": "📝"}.get(m.get("status"), "⚪")
            num = m.get("machine_number")
            num_tag = f" #{num}" if num is not None else ""
            output += f"{status_icon} **{m.get('name')}**{num_tag} (ID: `{m.get('id')}`)\n"
            if m.get("description"):
                output += f"   {m.get('description')[:80]}...\n"

        return output
    except Exception as e:
        return f"Error fetching machines: {str(e)}"


def _build_default_workflow(name, goal, machine_id=None):
    """Build a default workflow YAML for a new machine."""
    safe_name = name.lower().replace(' ', '-').replace('–', '-').replace('—', '-')
    steps = []
    if machine_id:
        steps.append(f'  - name: load-context\n    description: Load machine context (agent, memory, metrics, tasks)\n    run: "kompany-workflow load-context {machine_id}"')
    steps.append(f'  - name: run\n    description: "{goal or name}"\n    run: \'kompany-workflow llm -s "$STEP_LOAD_CONTEXT_STDOUT" -p "Execute your goal: {(goal or name).replace(chr(39), "")}"\'\n    timeout: 300')
    return f'name: {safe_name}\ndescription: "{goal or name}"\n\nsteps:\n' + '\n\n'.join(steps) + '\n'


@mcp.tool()
def kompany_machine_create(
    name: str,
    description: str = "",
    goal: str = "",
    status: str = "active",
    position_x: float = 0,
    position_y: float = 0,
    metric_unit: str = "",
    leading_metric_name: str = "",
    machine_type: str = "processor",
    domain_id: str = None,
    workflow_yaml: str = None
) -> str:
    """Create a new machine (automated business process) in the current project.

    A default workflow is auto-generated if none provided. The workflow defines
    what this machine does when triggered by a cron.

    Args:
        name: Display name for the machine
        description: Short description of what this machine does
        goal: The machine's goal or objective
        status: active, paused, or draft (default: active)
        position_x: X position on canvas (default: 0)
        position_y: Y position on canvas (default: 0)
        metric_unit: Output metric name (e.g. "leads", "signups", "revenue")
        leading_metric_name: Input/leading metric name (e.g. "calls made", "emails sent")
        machine_type: generator, processor, funnel, monitor, router, aggregator, syncer, or nurture (default: processor)
        domain_id: UUID of the domain to place this machine in (optional). Use kompany_domain_list to find domain IDs.
        workflow_yaml: Workflow YAML definition (optional). Auto-generated if not provided.

    Requires a project to be focused first using kompany_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first."

    try:
        payload = {
            "name": name,
            "description": description,
            "goal": goal,
            "status": status,
            "position_x": position_x,
            "position_y": position_y,
            "metric_unit": metric_unit,
            "leading_metric_name": leading_metric_name,
            "machine_type": machine_type,
            "project_id": state.CURRENT_PROJECT_ID
        }
        if domain_id is not None:
            payload["domain_id"] = domain_id
        result = api_post("/api/machines", payload)
        machine = result.get("machine", result)
        machine_id = machine.get("id")

        # Set workflow — provided or auto-generated
        wf = workflow_yaml or _build_default_workflow(name, goal, machine_id)
        try:
            api_put(f"/api/machines/{machine_id}", {"command": wf})
        except Exception:
            pass

        # Seed metrics if provided
        metrics_seeded = []
        if metric_unit:
            try:
                api_post(f"/api/machines/{machine_id}/metrics/add", {
                    "metric_name": metric_unit,
                    "value": 0,
                    "period": "weekly"
                })
                metrics_seeded.append(f"output: {metric_unit}")
            except Exception:
                pass
        if leading_metric_name:
            try:
                api_post(f"/api/machines/{machine_id}/metrics/add", {
                    "metric_name": leading_metric_name,
                    "value": 0,
                    "period": "weekly"
                })
                metrics_seeded.append(f"leading: {leading_metric_name}")
            except Exception:
                pass

        output = f"✅ Created machine: {name} (ID: {machine_id}) in project {state.CURRENT_PROJECT_NAME}"
        if metrics_seeded:
            output += f"\n   Metrics seeded: {', '.join(metrics_seeded)}"
        return output
    except Exception as e:
        return f"Error creating machine: {str(e)}"


@mcp.tool()
def kompany_machine_get(machine_id: str) -> str:
    """Get a specific machine by ID.

    Args:
        machine_id: The UUID of the machine to retrieve
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/machines/{machine_id}")
        machine = result.get("machine", result)

        output = f"# Machine: {machine.get('name')}\n\n"
        output += f"**ID:** `{machine.get('id')}`\n"
        output += f"**Status:** {machine.get('status', 'unknown')}\n"
        if machine.get('description'):
            output += f"**Description:** {machine.get('description')}\n"
        if machine.get('goal'):
            output += f"**Goal:** {machine.get('goal')}\n"
        output += f"**Position:** ({machine.get('position_x', 0)}, {machine.get('position_y', 0)})\n"

        return output
    except Exception as e:
        return f"Error fetching machine: {str(e)}"


@mcp.tool()
def kompany_machine_update(
    machine_id: str,
    name: str = None,
    description: str = None,
    goal: str = None,
    status: str = None,
    position_x: float = None,
    position_y: float = None,
    agent_id: str = None,
    domain_id: str = None
) -> str:
    """Update an existing machine.

    Args:
        machine_id: The UUID of the machine to update
        name: New name (optional)
        description: New description (optional)
        goal: New goal (optional)
        status: New status - active, paused, or draft (optional)
        position_x: New X position (optional)
        position_y: New Y position (optional)
        agent_id: UUID of the agent to assign (optional)
        domain_id: UUID of the domain to move this machine to (optional). Use kompany_domain_list to find domain IDs.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first."

    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if goal is not None:
            updates["goal"] = goal
        if status is not None:
            updates["status"] = status
        if position_x is not None:
            updates["position_x"] = position_x
        if position_y is not None:
            updates["position_y"] = position_y
        if agent_id is not None:
            updates["agent_id"] = agent_id
        if domain_id is not None:
            updates["domain_id"] = domain_id

        if not updates:
            return "⚠️ No updates provided. Specify at least one field to update."

        result = api_put(f"/api/machines/{machine_id}", updates)
        machine = result.get("machine", result)
        return f"✅ Updated machine: {machine.get('name')} (ID: {machine_id})"
    except Exception as e:
        return f"Error updating machine: {str(e)}"


@mcp.tool()
def kompany_machine_delete(machine_id: str) -> str:
    """Delete a machine by ID.

    Args:
        machine_id: The UUID of the machine to delete
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `kompany_project_focus <project_id>` first."

    try:
        api_delete(f"/api/machines/{machine_id}")
        return f"✅ Deleted machine (ID: {machine_id})"
    except Exception as e:
        return f"Error deleting machine: {str(e)}"
