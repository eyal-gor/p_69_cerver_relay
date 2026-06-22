"""
kompany-workflow — Deterministic workflow CLI for Kompany agents.

A standalone CLI that agents invoke via bash to run multi-step pipelines.
Reads workflow definitions from YAML files, executes steps sequentially,
and returns structured JSON results. Approval gates pause execution and
return context for the agent to create a Kompany Decision.

Usage:
    kompany-workflow run [--file workflow.yml] [--step step-name] [--from step-name]
    kompany-workflow validate [--file workflow.yml]
    kompany-workflow list [--file workflow.yml]

Workflow definition (YAML):
    name: my-pipeline
    working_directory: /path/to/project  # optional, defaults to cwd
    env:                                  # optional, global env vars
      API_KEY: $API_KEY
    steps:
      - name: fetch-data
        run: python fetch.py
        timeout: 60                       # optional, seconds
      - name: process
        run: python process.py
        env:
          MODE: production
      - name: deploy
        run: ./deploy.sh
        approval: required                # halts here, agent creates Decision

This module wires up the argparse CLI. The execution engine lives in
:mod:`branch_monkey_mcp.workflow_engine` and the subcommand handlers in
:mod:`branch_monkey_mcp.workflow_commands`; both are re-exported here so
existing ``from branch_monkey_mcp.workflow import ...`` imports keep working.
"""

import argparse
import sys

from .workflow_engine import (
    DEFAULT_STEP_TIMEOUT,
    DEFAULT_WORKFLOW_FILE,
    find_workflow_file,
    load_workflow,
    resolve_env,
    run_step,
    run_workflow,
)
from .workflow_commands import (
    _get_api_client,
    cmd_agent_prompt,
    cmd_list,
    cmd_llm,
    cmd_load_context,
    cmd_log,
    cmd_run,
    cmd_save_output,
    cmd_update_memory,
    cmd_update_metric,
    cmd_validate,
)

__all__ = [
    "DEFAULT_STEP_TIMEOUT",
    "DEFAULT_WORKFLOW_FILE",
    "find_workflow_file",
    "load_workflow",
    "resolve_env",
    "run_step",
    "run_workflow",
    "cmd_run",
    "cmd_validate",
    "cmd_list",
    "cmd_llm",
    "cmd_agent_prompt",
    "cmd_save_output",
    "cmd_update_memory",
    "cmd_update_metric",
    "cmd_log",
    "cmd_load_context",
    "main",
]


def main():
    parser = argparse.ArgumentParser(
        prog="kompany-workflow",
        description="Deterministic workflow runner for Kompany agents",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run
    run_parser = subparsers.add_parser("run", help="Execute a workflow")
    run_parser.add_argument("--file", "-f", help="Path to workflow YAML file")
    run_parser.add_argument("--step", "-s", help="Run only this specific step")
    run_parser.add_argument("--from", dest="resume_from", help="Resume from this step (skip prior steps)")
    run_parser.set_defaults(func=cmd_run)

    # validate
    val_parser = subparsers.add_parser("validate", help="Validate a workflow file")
    val_parser.add_argument("--file", "-f", help="Path to workflow YAML file")
    val_parser.set_defaults(func=cmd_validate)

    # list
    list_parser = subparsers.add_parser("list", help="List workflow steps")
    list_parser.add_argument("--file", "-f", help="Path to workflow YAML file")
    list_parser.set_defaults(func=cmd_list)

    # agent-prompt
    ap_parser = subparsers.add_parser("agent-prompt", help="Fetch the agent prompt for a machine")
    ap_parser.add_argument("machine_id", help="Machine UUID")
    ap_parser.set_defaults(func=cmd_agent_prompt)

    # llm
    llm_parser = subparsers.add_parser("llm", help="Run a prompt through the configured LLM")
    llm_parser.add_argument("--prompt", "-p", help="The prompt (or pipe via stdin)")
    llm_parser.add_argument("--system-prompt", "-s", help="System prompt")
    llm_parser.add_argument("--cli", help="CLI provider: claude, codex, grok (default: from config)")
    llm_parser.add_argument("--cwd", help="Working directory")
    llm_parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default: 300)")
    llm_parser.add_argument("--mcp", action="store_true", help="Load MCP tools (slower startup, enables tool use)")
    llm_parser.set_defaults(func=cmd_llm)

    # save-output
    so_parser = subparsers.add_parser("save-output", help="Save output as a Kompany context")
    so_parser.add_argument("--content", "-c", help="Content to save (or pipe via stdin)")
    so_parser.add_argument("--name", "-n", help="Context name (default: Workflow Output)")
    so_parser.add_argument("--type", "-t", default="general", help="Context type (default: general)")
    so_parser.add_argument("--project-id", help="Project UUID (uses focused project if omitted)")
    so_parser.set_defaults(func=cmd_save_output)

    # update-memory
    mem_parser = subparsers.add_parser("update-memory", help="Update a machine's memory context")
    mem_parser.add_argument("--machine-id", "-m", help="Machine UUID (looks up its memory context)")
    mem_parser.add_argument("--context-id", help="Direct context UUID to update")
    mem_parser.add_argument("--content", "-c", help="Content (or pipe via stdin)")
    mem_parser.add_argument("--append", "-a", action="store_true", help="Append to existing content instead of replacing")
    mem_parser.set_defaults(func=cmd_update_memory)

    # update-metric
    met_parser = subparsers.add_parser("update-metric", help="Update a metric on a machine")
    met_parser.add_argument("machine_id", help="Machine UUID")
    met_parser.add_argument("metric_name", help="Metric name")
    met_parser.add_argument("--value", type=float, help="Set to this value")
    met_parser.add_argument("--increment", type=float, help="Increment by this amount")
    met_parser.set_defaults(func=cmd_update_metric)

    # log
    log_parser = subparsers.add_parser("log", help="Log workflow activity")
    log_parser.add_argument("--content", "-c", help="Log content (or pipe via stdin)")
    log_parser.add_argument("--machine-id", "-m", help="Machine UUID")
    log_parser.add_argument("--task-id", help="Task ID to log against")
    log_parser.add_argument("--title", help="Log title")
    log_parser.set_defaults(func=cmd_log)

    # load-context
    ctx_parser = subparsers.add_parser("load-context", help="Load full machine context (agent, memory, metrics, tasks)")
    ctx_parser.add_argument("machine_id", help="Machine UUID")
    ctx_parser.set_defaults(func=cmd_load_context)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
