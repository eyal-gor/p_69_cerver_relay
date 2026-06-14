"""
Kompany-specific relay registration and heartbeat helpers.
"""

from typing import Any, Dict, Optional

import httpx


def build_cloud_heartbeat_payload(
    machine_id: str,
    machine_name: str,
    local_port: int,
    status: str = "online",
) -> Dict[str, Any]:
    """Build the JSON body for a cloud relay heartbeat.

    Args:
        machine_id: Stable identifier for this relay machine.
        machine_name: Human-readable machine name shown in the cloud UI.
        local_port: Port the relay's local HTTP server listens on, advertised
            so the cloud knows where to forward requests back to.
        status: Liveness status to report (defaults to ``"online"``).

    Returns:
        The heartbeat payload as a plain dict, ready to JSON-encode.
    """
    return {
        "machine_id": machine_id,
        "machine_name": machine_name,
        "status": status,
        "local_port": local_port,
    }


async def post_cloud_heartbeat(
    cloud_url: str,
    access_token: Optional[str],
    machine_id: str,
    machine_name: str,
    local_port: int,
    status: str = "online",
) -> Dict[str, Any]:
    """Send a heartbeat to the cloud relay registry.

    Registers (or refreshes) this machine with the cloud so it stays
    routable for cloud-to-local request forwarding. Called periodically by
    the relay.

    Args:
        cloud_url: Base URL of the cloud relay service.
        access_token: Bearer token for authenticating the relay; when falsy,
            the request is sent without an ``Authorization`` header.
        machine_id: Stable identifier for this relay machine.
        machine_name: Human-readable machine name shown in the cloud UI.
        local_port: Port the relay's local HTTP server listens on.
        status: Liveness status to report (defaults to ``"online"``).

    Returns:
        The decoded JSON response from the cloud.

    Raises:
        httpx.HTTPStatusError: If the cloud returns a non-2xx status.
    """
    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{cloud_url}/api/relay/heartbeat",
            headers=headers,
            json=build_cloud_heartbeat_payload(
                machine_id=machine_id,
                machine_name=machine_name,
                local_port=local_port,
                status=status,
            ),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()


async def post_local_heartbeat(
    local_port: int,
    machine_id: str,
    machine_name: str,
    cloud_url: str,
) -> None:
    """Notify the relay's own local server that the cloud link is up.

    Posts to the loopback ``/api/relay/heartbeat`` endpoint so the local
    server knows which cloud it is paired with and reflects the connected
    state (e.g. in the relay UI). Fire-and-forget — the response is ignored.

    Args:
        local_port: Port the relay's local HTTP server listens on.
        machine_id: Stable identifier for this relay machine.
        machine_name: Human-readable machine name.
        cloud_url: Base URL of the cloud this relay is connected to.
    """
    async with httpx.AsyncClient() as client:
        await client.post(
            f"http://127.0.0.1:{local_port}/api/relay/heartbeat",
            json={
                "machine_id": machine_id,
                "machine_name": machine_name,
                "cloud_url": cloud_url,
            },
            timeout=5,
        )


async def post_local_disconnect(local_port: int) -> None:
    """Tell the relay's own local server the cloud link has gone down.

    Posts to the loopback ``/api/relay/disconnect`` endpoint so the local
    server can clear its connected state during relay shutdown or teardown.
    Fire-and-forget — the response is ignored.

    Args:
        local_port: Port the relay's local HTTP server listens on.
    """
    async with httpx.AsyncClient() as client:
        await client.post(
            f"http://127.0.0.1:{local_port}/api/relay/disconnect",
            timeout=5,
        )
