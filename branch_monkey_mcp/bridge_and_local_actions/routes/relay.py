"""
Relay status, heartbeat, and diagnostics endpoints.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import get_relay_status, update_relay_status
from ...connection_logger import connection_logger

router = APIRouter()


class RelayHeartbeat(BaseModel):
    """Relay heartbeat request."""
    machine_id: str
    machine_name: str
    cloud_url: str


@router.get("/status")
def relay_status():
    """Get current relay connection status."""
    status = get_relay_status()
    # Check if heartbeat is recent (within 60 seconds)
    if status["last_heartbeat"]:
        last_hb = datetime.fromisoformat(status["last_heartbeat"])
        age_seconds = (datetime.utcnow() - last_hb).total_seconds()
        status["connected"] = age_seconds < 60
        status["heartbeat_age_seconds"] = int(age_seconds)
    return status


@router.post("/heartbeat")
def relay_heartbeat(heartbeat: RelayHeartbeat):
    """Receive heartbeat from relay client to indicate it's connected."""
    update_relay_status(
        connected=True,
        machine_id=heartbeat.machine_id,
        machine_name=heartbeat.machine_name,
        cloud_url=heartbeat.cloud_url
    )
    return {"status": "ok", "received": datetime.utcnow().isoformat()}


@router.post("/disconnect")
def relay_disconnect():
    """Mark relay as disconnected."""
    update_relay_status(connected=False)
    return {"status": "ok", "disconnected": True}


@router.get("/diagnostics")
def relay_diagnostics():
    """
    Connection diagnostics: uptime, reconnect count, heartbeat stats, recent events.

    Returns structured data for debugging disconnection issues.
    Use this endpoint from the dashboard, curl, or any monitoring tool.

    Example: curl http://localhost:18081/api/relay/diagnostics | jq
    """
    diag = connection_logger.get_diagnostics()
    # Merge in current relay status for full picture
    diag["relay_status"] = get_relay_status()
    return diag


@router.post("/leave")
async def leave_pool():
    """The owner removed this computer from the pool (via the gateway)."""
    from ...cerver_compute.leave import leave
    return {"ok": leave(), "say": "This computer is leaving the pool."}
