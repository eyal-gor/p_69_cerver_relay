"""
Workflow execution engine for ``kompany-workflow``.

Pure execution layer: locating and loading workflow YAML definitions,
resolving environment variables, and running steps as subprocesses. The
CLI command handlers in :mod:`branch_monkey_mcp.workflow_commands` and the
argparse wiring in :mod:`branch_monkey_mcp.workflow` build on top of this.
"""

import os
import subprocess
import time
from pathlib import Path

import yaml


DEFAULT_WORKFLOW_FILE = ".kompany/workflow.yml"
DEFAULT_STEP_TIMEOUT = 300  # 5 minutes


def find_workflow_file(file_path=None):
    """Find the workflow definition file."""
    if file_path:
        p = Path(file_path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Workflow file not found: {file_path}")

    # Search up from cwd
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / DEFAULT_WORKFLOW_FILE
        if candidate.exists():
            return candidate
        # Also check workflow.yml at root
        candidate = parent / "workflow.yml"
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"No workflow file found. Create {DEFAULT_WORKFLOW_FILE} or pass --file"
    )


def load_workflow(file_path=None):
    """Load and validate a workflow definition from YAML."""
    path = find_workflow_file(file_path)

    with open(path) as f:
        wf = yaml.safe_load(f)

    if not isinstance(wf, dict):
        raise ValueError(f"Invalid workflow file: expected a YAML mapping, got {type(wf).__name__}")

    if "steps" not in wf or not isinstance(wf["steps"], list):
        raise ValueError("Workflow must have a 'steps' list")

    # Validate steps
    for i, step in enumerate(wf["steps"]):
        if not isinstance(step, dict):
            raise ValueError(f"Step {i} must be a mapping")
        if "name" not in step:
            raise ValueError(f"Step {i} missing 'name'")
        if "run" not in step and step.get("approval") != "required":
            raise ValueError(f"Step '{step['name']}' missing 'run' command")

    wf.setdefault("name", path.stem)
    wf.setdefault("working_directory", str(path.parent.parent))  # parent of .kompany/
    wf["_file"] = str(path)

    return wf


def resolve_env(env_dict, parent_env=None):
    """Resolve environment variables, expanding $VAR references."""
    base = dict(os.environ)
    if parent_env:
        base.update(parent_env)

    resolved = {}
    for key, val in (env_dict or {}).items():
        val = str(val)
        if val.startswith("$"):
            var_name = val[1:]
            resolved[key] = base.get(var_name, "")
        else:
            resolved[key] = val

    return resolved


def run_step(step, global_env, working_directory, prev_results):
    """Execute a single workflow step. Returns step result dict."""
    name = step["name"]
    command = step.get("run", "")
    step_env = step.get("env", {})
    timeout = step.get("timeout", DEFAULT_STEP_TIMEOUT)

    # Build environment — inherits parent env (including $AGENT_PROMPT if set)
    env = dict(os.environ)
    env.update(resolve_env(global_env))
    env.update(resolve_env(step_env, global_env))

    # Inject previous step results as env vars
    for prev in prev_results:
        safe_name = prev["name"].upper().replace("-", "_").replace(" ", "_")
        env[f"STEP_{safe_name}_STATUS"] = prev["status"]
        env[f"STEP_{safe_name}_EXIT_CODE"] = str(prev.get("exit_code", ""))
        # Truncate stdout to avoid env var size limits
        stdout = prev.get("stdout", "")
        if len(stdout) > 4096:
            stdout = stdout[:4096] + "\n...(truncated)"
        env[f"STEP_{safe_name}_STDOUT"] = stdout

    # Resolve working directory
    cwd = step.get("working_directory", working_directory)
    cwd = os.path.expanduser(cwd)

    start = time.time()

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        duration_ms = int((time.time() - start) * 1000)

        return {
            "name": name,
            "status": "success" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "stdout": result.stdout[-8192:] if len(result.stdout) > 8192 else result.stdout,
            "stderr": result.stderr[-4096:] if len(result.stderr) > 4096 else result.stderr,
            "duration_ms": duration_ms,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "name": name,
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Step timed out after {timeout}s",
            "duration_ms": duration_ms,
        }

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "name": name,
            "status": "error",
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "duration_ms": duration_ms,
        }


def run_workflow(wf, from_step=None, single_step=None):
    """Execute a workflow. Returns structured JSON result."""
    steps = wf["steps"]
    global_env = wf.get("env", {})
    working_directory = wf.get("working_directory", os.getcwd())
    results = []
    total_start = time.time()

    # Determine which steps to run
    start_index = 0
    if from_step:
        found = False
        for i, step in enumerate(steps):
            if step["name"] == from_step:
                start_index = i
                found = True
                break
        if not found:
            return {
                "workflow": wf.get("name", "unknown"),
                "status": "error",
                "error": f"Step '{from_step}' not found",
                "steps": [],
                "duration_ms": 0,
            }

    for i, step in enumerate(steps):
        name = step["name"]

        # Skip steps before start point
        if i < start_index:
            results.append({"name": name, "status": "skipped"})
            continue

        # Single step mode
        if single_step and name != single_step:
            results.append({"name": name, "status": "skipped"})
            continue

        # Approval gate — halt before this step
        if step.get("approval") == "required":
            # If we're resuming FROM this step, skip the gate
            if from_step != name:
                results.append({
                    "name": name,
                    "status": "pending_approval",
                    "approval": {
                        "step": name,
                        "description": step.get("description", f"Step '{name}' requires approval before execution"),
                        "resume_from": name,
                    },
                })
                total_duration = int((time.time() - total_start) * 1000)
                return {
                    "workflow": wf.get("name", "unknown"),
                    "file": wf.get("_file", ""),
                    "status": "needs_approval",
                    "steps": results,
                    "duration_ms": total_duration,
                    "resume_from": name,
                    "approval": {
                        "step": name,
                        "description": step.get("description", f"Approval needed for: {name}"),
                    },
                }

        # Check condition
        condition = step.get("condition")
        if condition:
            # Simple condition: check if previous step succeeded
            if condition.startswith("step.") and condition.endswith(".success"):
                ref_name = condition[5:-8]
                ref_result = next((r for r in results if r["name"] == ref_name), None)
                if not ref_result or ref_result["status"] != "success":
                    results.append({"name": name, "status": "skipped", "reason": f"Condition not met: {condition}"})
                    continue

        # Run the step
        if step.get("run"):
            step_result = run_step(step, global_env, working_directory, results)
            results.append(step_result)

            # Stop on failure unless continue_on_error
            if step_result["status"] != "success" and not step.get("continue_on_error"):
                total_duration = int((time.time() - total_start) * 1000)
                return {
                    "workflow": wf.get("name", "unknown"),
                    "file": wf.get("_file", ""),
                    "status": "failed",
                    "failed_step": name,
                    "steps": results,
                    "duration_ms": total_duration,
                }
        else:
            results.append({"name": name, "status": "skipped", "reason": "No command"})

        if single_step:
            break

    total_duration = int((time.time() - total_start) * 1000)
    return {
        "workflow": wf.get("name", "unknown"),
        "file": wf.get("_file", ""),
        "status": "completed",
        "steps": results,
        "duration_ms": total_duration,
    }
