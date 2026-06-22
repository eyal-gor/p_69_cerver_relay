"""
CLI command handlers for ``kompany-workflow``.

Each ``cmd_*`` function implements one argparse subcommand (wired up in
:mod:`branch_monkey_mcp.workflow`). Commands fall into three groups:

* workflow execution — ``cmd_run``, ``cmd_validate``, ``cmd_list`` build on
  :mod:`branch_monkey_mcp.workflow_engine`;
* LLM passthrough — ``cmd_llm`` runs a prompt through the configured CLI
  provider;
* Kompany API helpers — ``cmd_agent_prompt``, ``cmd_save_output``,
  ``cmd_update_memory``, ``cmd_update_metric``, ``cmd_log`` and
  ``cmd_load_context`` talk to the kompany.dev REST API via
  :func:`_get_api_client`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from .workflow_engine import load_workflow, run_workflow


def cmd_run(args):
    """Run a workflow."""
    try:
        wf = load_workflow(args.file)
    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

    result = run_workflow(wf, from_step=args.resume_from, single_step=args.step)
    print(json.dumps(result, indent=2))

    if result["status"] == "failed":
        sys.exit(1)
    elif result["status"] == "error":
        sys.exit(2)


def cmd_validate(args):
    """Validate a workflow file."""
    try:
        wf = load_workflow(args.file)
        steps = wf["steps"]
        approval_gates = [s["name"] for s in steps if s.get("approval") == "required"]

        result = {
            "valid": True,
            "workflow": wf.get("name", "unknown"),
            "file": wf.get("_file", ""),
            "step_count": len(steps),
            "steps": [s["name"] for s in steps],
            "approval_gates": approval_gates,
        }
        print(json.dumps(result, indent=2))

    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"valid": False, "error": str(e)}))
        sys.exit(1)


def cmd_list(args):
    """List steps in a workflow."""
    try:
        wf = load_workflow(args.file)
    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

    steps = []
    for s in wf["steps"]:
        info = {"name": s["name"]}
        if s.get("run"):
            info["command"] = s["run"]
        if s.get("approval"):
            info["approval"] = s["approval"]
        if s.get("description"):
            info["description"] = s["description"]
        if s.get("timeout"):
            info["timeout"] = s["timeout"]
        if s.get("condition"):
            info["condition"] = s["condition"]
        steps.append(info)

    print(json.dumps({
        "workflow": wf.get("name", "unknown"),
        "file": wf.get("_file", ""),
        "steps": steps,
    }, indent=2))


def cmd_llm(args):
    """Run a prompt through the configured LLM CLI and print the result."""
    from .bridge_and_local_actions.cli_providers import get_provider, get_default_cli

    # Read prompt from --prompt arg or stdin
    prompt = args.prompt
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("Error: provide a prompt via --prompt or stdin", file=sys.stderr)
            sys.exit(1)

    # Resolve provider
    cli_name = args.cli or get_default_cli()

    try:
        provider = get_provider(cli_name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not provider.is_available():
        print(f"Error: {provider.display_name} is not installed. {provider.install_hint}", file=sys.stderr)
        sys.exit(1)

    system_prompt = args.system_prompt or None
    use_mcp = getattr(args, 'mcp', False)
    cli_cmd = provider.build_text_command(prompt, system_prompt=system_prompt, use_mcp=use_mcp)

    env = os.environ.copy()
    for key in cli_cmd.env_overrides:
        env.pop(key, None)
    env.pop("CLAUDECODE", None)
    if cli_cmd.env_inject:
        env.update(cli_cmd.env_inject)

    cwd = args.cwd or os.getcwd()

    try:
        result = subprocess.run(
            cli_cmd.args,
            capture_output=True,
            text=True,
            timeout=args.timeout or 300,
            cwd=cwd,
            env=env,
        )
        # Print stdout (the LLM response)
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0 and result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    except subprocess.TimeoutExpired:
        print(f"Error: LLM call timed out after {args.timeout or 300}s", file=sys.stderr)
        sys.exit(1)


def _get_api_client():
    """Load auth and return (api_url, headers) for Kompany API calls."""
    import requests as req
    token_path = Path.home() / ".branch-monkey" / "token.json"
    if not token_path.exists():
        print("Error: not authenticated. Run branch-monkey-mcp first.", file=sys.stderr)
        sys.exit(1)

    with open(token_path) as f:
        token_data = json.load(f)

    api_url = token_data.get("api_url", "https://kompany.dev")
    headers = {
        "Authorization": f"Bearer {token_data.get('access_token', '')}",
        "X-Org-Id": token_data.get("org_id", ""),
        "Content-Type": "application/json",
    }
    return api_url, headers, req


def cmd_agent_prompt(args):
    """Fetch the agent's system_prompt for a machine and print to stdout."""
    api_url, headers, req = _get_api_client()

    resp = req.get(f"{api_url}/api/machines/{args.machine_id}", headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"Error fetching machine: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    machine = resp.json().get("machine", {})
    agent_id = machine.get("agent_id")
    if not agent_id:
        print("Error: machine has no agent assigned", file=sys.stderr)
        sys.exit(1)

    resp = req.get(f"{api_url}/api/agent-definitions/{agent_id}", headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"Error fetching agent: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    system_prompt = resp.json().get("agent", {}).get("system_prompt", "")
    if not system_prompt:
        print("Error: agent has no system_prompt", file=sys.stderr)
        sys.exit(1)

    print(system_prompt)


def cmd_save_output(args):
    """Save workflow output as a Kompany context."""
    api_url, headers, req = _get_api_client()

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        if not content:
            print("Error: provide content via --content or stdin", file=sys.stderr)
            sys.exit(1)

    payload = {
        "name": args.name or "Workflow Output",
        "content": content,
        "context_type": args.type or "general",
    }
    if args.project_id:
        payload["project_id"] = args.project_id

    resp = req.post(f"{api_url}/api/contexts", headers=headers, json=payload, timeout=15)
    if resp.status_code not in (200, 201):
        print(f"Error saving context: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    ctx = resp.json().get("context", resp.json())
    print(json.dumps({"saved": True, "context_id": ctx.get("id"), "name": payload["name"]}))


def cmd_update_memory(args):
    """Update or append to a machine's memory context."""
    api_url, headers, req = _get_api_client()

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        if not content:
            print("Error: provide content via --content or stdin", file=sys.stderr)
            sys.exit(1)

    if args.context_id:
        # Direct update
        context_id = args.context_id
    elif args.machine_id:
        # Find memory context for this machine
        resp = req.get(f"{api_url}/api/machines/{args.machine_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"Error fetching machine: {resp.status_code}", file=sys.stderr)
            sys.exit(1)
        machine_name = resp.json().get("machine", {}).get("name", "")
        memory_name = f"Machine Memory: {machine_name}"

        # Search for existing memory context
        resp = req.get(f"{api_url}/api/contexts?search={memory_name}", headers=headers, timeout=15)
        contexts = resp.json().get("contexts", [])
        memory = next((c for c in contexts if c.get("context_type") == "memory" and memory_name.lower() in c.get("name", "").lower()), None)

        if not memory:
            print(f"Error: no memory context found for machine {args.machine_id}", file=sys.stderr)
            sys.exit(1)
        context_id = memory["id"]
    else:
        print("Error: provide --machine-id or --context-id", file=sys.stderr)
        sys.exit(1)

    # Append or replace
    if args.append:
        resp = req.get(f"{api_url}/api/contexts/{context_id}", headers=headers, timeout=15)
        existing = resp.json().get("context", {}).get("content", "")
        content = existing.rstrip() + "\n\n" + content

    resp = req.put(f"{api_url}/api/contexts/{context_id}", headers=headers, json={"content": content}, timeout=15)
    if resp.status_code != 200:
        print(f"Error updating memory: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"updated": True, "context_id": context_id}))


def cmd_update_metric(args):
    """Update a metric value on a machine."""
    api_url, headers, req = _get_api_client()

    payload = {"metric_name": args.metric_name}
    if args.value is not None:
        payload["value"] = args.value
    if args.increment is not None:
        # Fetch current value and add
        resp = req.get(f"{api_url}/api/machines/{args.machine_id}/metrics", headers=headers, timeout=15)
        metrics = resp.json().get("metrics", [])
        current = next((m for m in metrics if m.get("metric_name") == args.metric_name), None)
        current_val = current.get("value", 0) if current else 0
        payload["value"] = current_val + args.increment

    resp = req.put(f"{api_url}/api/machines/{args.machine_id}/metrics", headers=headers, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Error updating metric: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"updated": True, "machine_id": args.machine_id, "metric": args.metric_name, "value": payload.get("value")}))


def cmd_log(args):
    """Log a workflow run to the machine's run history."""
    api_url, headers, req = _get_api_client()

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        if not content:
            print("Error: provide content via --content or stdin", file=sys.stderr)
            sys.exit(1)

    payload = {"content": content}
    if args.task_id:
        payload["task_id"] = args.task_id

    resp = req.post(f"{api_url}/api/task-logs", headers=headers, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        print(json.dumps({"logged": True}))
    else:
        # Fallback: save as notification
        notif_payload = {
            "title": args.title or "Workflow Log",
            "body": content[:500],
            "type": "info",
        }
        if args.machine_id:
            notif_payload["machine_id"] = args.machine_id
        req.post(f"{api_url}/api/notifications", headers=headers, json=notif_payload, timeout=15)
        print(json.dumps({"logged": True, "method": "notification"}))


def cmd_load_context(args):
    """Load full machine context: agent prompt, memory, metrics, tasks. Prints enriched prompt."""
    api_url, headers, req = _get_api_client()
    machine_id = args.machine_id
    parts = []

    # 1. Machine info
    resp = req.get(f"{api_url}/api/machines/{machine_id}", headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"Error fetching machine: {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    machine = resp.json().get("machine", {})
    parts.append(f"## Your Machine: {machine.get('name')} (ID: {machine_id})")
    if machine.get("description"):
        parts.append(machine["description"])
    if machine.get("goal"):
        parts.append(f"Goal: {machine['goal']}")
    parts.append("")

    # 2. Agent prompt
    agent_id = machine.get("agent_id")
    if agent_id:
        resp = req.get(f"{api_url}/api/agent-definitions/{agent_id}", headers=headers, timeout=15)
        if resp.status_code == 200:
            prompt = resp.json().get("agent", {}).get("system_prompt", "")
            if prompt:
                parts.append("## Agent Instructions")
                parts.append(prompt)
                parts.append("")

    # 3. Metrics
    resp = req.get(f"{api_url}/api/machines/{machine_id}/metrics", headers=headers, timeout=15)
    if resp.status_code == 200:
        metrics = resp.json().get("metrics", [])
        if metrics:
            parts.append("## Current Metrics")
            seen = set()
            for m in metrics:
                name = m.get("metric_name", "")
                if name in seen:
                    continue
                seen.add(name)
                target = f" / target: {m['target']}" if m.get("target") else ""
                period = m.get("period", "weekly")
                parts.append(f"- {name}: {m.get('value', 0)}{target} ({period})")
            parts.append("")

    # 4. Memory
    project_id = machine.get("project_id", "")
    memory_name = f"Machine Memory: {machine.get('name', '')}"
    resp = req.get(f"{api_url}/api/contexts?search={memory_name}", headers=headers, timeout=15)
    if resp.status_code == 200:
        contexts = resp.json().get("contexts", [])
        memory = next((c for c in contexts if c.get("context_type") == "memory" and memory_name.lower() in c.get("name", "").lower()), None)
        if memory and memory.get("content"):
            parts.append("## Memory (from previous runs)")
            content = memory["content"]
            if len(content) > 3000:
                content = content[-3000:]
            parts.append(content)
            parts.append("")

    # 5. Tasks
    task_url = f"{api_url}/api/tasks?project_id={project_id}&machine_id={machine_id}" if project_id else f"{api_url}/api/tasks?machine_id={machine_id}"
    resp = req.get(task_url, headers=headers, timeout=15)
    if resp.status_code == 200:
        tasks = resp.json().get("tasks", [])
        if tasks:
            active = [t for t in tasks if t.get("status") in ("todo", "in_progress", "in_review")]
            if active:
                parts.append("## Active Tasks")
                for t in active[:20]:
                    parts.append(f"- #{t.get('task_number', '?')} [{t.get('status')}] {t.get('title', '')}")
                parts.append("")

    print("\n".join(parts))
