"""
MCP Tools for Kompany.

This package contains all MCP tool definitions organized by domain.
"""

# Import all tool modules to register them with the MCP app
from . import status
from . import projects
from . import tasks
from . import versions
from . import teams
from . import machines
from . import metrics
from . import connections
from . import notes
from . import domains
from . import contexts
from . import agents
from . import deployments
from . import decisions
from . import crons
from . import compute
from . import jobs

__all__ = [
    "jobs",
    "status",
    "projects",
    "tasks",
    "versions",
    "teams",
    "machines",
    "metrics",
    "connections",
    "notes",
    "domains",
    "contexts",
    "agents",
    "deployments",
    "decisions",
    "crons",
    "compute",
]
