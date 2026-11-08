"""
API routes for the local server.

This package contains all FastAPI route handlers organized by domain.
"""

from fastapi import APIRouter

from .health import router as health_router
from .relay import router as relay_router
from .agents import router as agents_router
from .git import router as git_router
from .worktrees import router as worktrees_router
from .dev_servers import router as dev_servers_router
from .proxy import router as proxy_router
from .merge import router as merge_router
from .config_routes import router as config_router
from .advanced import router as advanced_router
from .projects import router as projects_router
from .cerver_provider import router as cerver_provider_router
from .logs import router as logs_router
from .live import router as live_router

# Create main router that includes all sub-routers
main_router = APIRouter()

# Include all routers
main_router.include_router(health_router, tags=["health"])
main_router.include_router(relay_router, prefix="/api/relay", tags=["relay"])
main_router.include_router(live_router, prefix="/api/live", tags=["live"])
main_router.include_router(agents_router, prefix="/api/local-claude", tags=["agents"])
main_router.include_router(git_router, prefix="/api", tags=["git"])
main_router.include_router(worktrees_router, prefix="/api/local-claude", tags=["worktrees"])
main_router.include_router(dev_servers_router, prefix="/api/local-claude", tags=["dev-servers"])
main_router.include_router(proxy_router, prefix="/api/local-claude", tags=["proxy"])
main_router.include_router(merge_router, prefix="/api/local-claude", tags=["merge"])
main_router.include_router(config_router, prefix="/api", tags=["config"])
main_router.include_router(advanced_router, prefix="/api/local-claude", tags=["advanced"])
main_router.include_router(projects_router, prefix="/api/local-claude/projects", tags=["projects"])
main_router.include_router(cerver_provider_router, prefix="/api/cerver", tags=["cerver-provider"])
main_router.include_router(logs_router, prefix="/api/local-claude", tags=["logs"])
