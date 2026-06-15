"""
Kompany-specific forwarding helpers for cloud-to-local requests.
"""

from typing import Any, Dict

import httpx


def build_local_url(local_port: int, path: str) -> str:
    """Build the loopback URL for a cloud-forwarded request.

    Cloud-forwarded requests carry only the request path; this prefixes it
    with the relay's local HTTP server origin (always ``127.0.0.1`` so the
    forwarded traffic never leaves the machine).

    Args:
        local_port: Port the relay's local HTTP server listens on.
        path: Request path, including any query string (e.g. ``/api/...``).

    Returns:
        The fully-qualified loopback URL to send the request to.
    """
    return f"http://127.0.0.1:{local_port}{path}"


async def execute_local_request(local_port: int, request: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a cloud-forwarded request against the local server."""
    request_id = request.get("id")
    method = request.get("method", "GET")
    path = request.get("path", "/")
    body = request.get("body", {})
    headers = request.get("headers", {})
    timeout_ms = request.get("timeout_ms")

    url = build_local_url(local_port, path)

    try:
        if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
            request_timeout = max(5.0, float(timeout_ms) / 1000.0)
        else:
            request_timeout = 55 if method == "GET" else 180
        async with httpx.AsyncClient() as client:
            if method == "GET":
                response = await client.get(url, headers=headers, timeout=request_timeout)
            elif method == "POST":
                response = await client.post(url, json=body, headers=headers, timeout=request_timeout)
            elif method == "PUT":
                response = await client.put(url, json=body, headers=headers, timeout=request_timeout)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers, timeout=request_timeout)
            elif method == "PATCH":
                response = await client.patch(url, json=body, headers=headers, timeout=request_timeout)
            else:
                return {
                    "type": "response",
                    "id": request_id,
                    "status": 405,
                    "body": {"error": f"Method {method} not supported"},
                }

            try:
                response_body = response.json()
            except Exception:
                response_body = {"text": response.text}

            return {
                "type": "response",
                "id": request_id,
                "status": response.status_code,
                "body": response_body,
            }
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        # The cloud-side caller sees this string verbatim (cerver bubbles
        # it up as the session-create error), so label it clearly:
        # "All connection attempts failed" coming from httpx looks like a
        # cloud↔relay network problem, but this branch only fires when
        # the relay can't reach its own local HTTP server.
        return {
            "type": "response",
            "id": request_id,
            "status": 500,
            "body": {
                "error": (
                    f"Relay's local HTTP server at {url} did not respond "
                    f"({type(exc).__name__}: {exc}). The relay process is "
                    f"likely deadlocked — restart the relay."
                ),
            },
        }
    except Exception as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": 500,
            "body": {"error": str(exc) or f"{type(exc).__name__}: unknown error"},
        }
