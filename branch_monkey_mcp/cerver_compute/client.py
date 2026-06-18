"""
Cerver compute registration client for the local runtime.

This lets a p69-managed local machine register itself with Cerver as a
private compute, keep that compute alive with heartbeats, and unregister
on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
import webbrowser
import uuid

import httpx

from ..computer_runtime.capabilities import get_runtime_capabilities
from ..computer_runtime.machine_state import get_machine_state


DEFAULT_CERVER_URL = "https://gateway.cerver.ai"
CONFIG_DIR = Path.home() / ".kompany"
CERVER_COMPUTE_STATE_FILE = CONFIG_DIR / "cerver_compute.json"


def _normalize_base_url(url: str) -> str:
    """Strip surrounding whitespace and any trailing slash from a base URL."""
    return url.strip().rstrip("/")


def _load_cerver_state() -> Dict[str, Any]:
    """Load the persisted compute state, returning {} if missing or corrupt.

    The state file (``~/.kompany/cerver_compute.json``) holds saved auth tokens
    and per-identity compute ids keyed by URL. Any read or JSON error is
    swallowed and treated as "no state" so a bad file never blocks startup.
    """
    if not CERVER_COMPUTE_STATE_FILE.exists():
        return {}

    try:
        return json.loads(CERVER_COMPUTE_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_cerver_state(state: Dict[str, Any]) -> None:
    """Persist the compute state dict to ``~/.kompany/cerver_compute.json``.

    Creates the config directory if needed and overwrites the file with
    pretty-printed JSON.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CERVER_COMPUTE_STATE_FILE.write_text(json.dumps(state, indent=2))


class CerverComputeClient:
    """Registers a local machine with Cerver as a private compute.

    Wraps the Cerver gateway's ``/v2/computes`` API: it authenticates (browser
    device-code flow), registers the machine, keeps it alive with heartbeats,
    and unregisters on shutdown. Auth tokens and the resolved ``compute_id`` are
    persisted to ``~/.kompany/cerver_compute.json`` (keyed by gateway URL and a
    derived identity) so restarts reuse the same compute without re-login.

    Configuration falls back to the ``CERVER_GATEWAY_URL``, ``CERVER_OWNER_ID``,
    and ``CERVER_API_TOKEN`` environment variables when arguments are omitted.
    """

    def __init__(
        self,
        *,
        cerver_url: Optional[str],
        owner_id: Optional[str],
        local_port: int,
        machine_name: str,
        provider: str = "cerver_local_provider",
        api_token: Optional[str] = None,
    ):
        """Initialize the client and load any persisted auth and identity.

        Args:
            cerver_url: Gateway base URL; falls back to ``CERVER_GATEWAY_URL``
                then :data:`DEFAULT_CERVER_URL`.
            owner_id: Account owner id; falls back to ``CERVER_OWNER_ID``.
            local_port: Port this machine's local runtime listens on.
            machine_name: Human-readable label for the registered compute.
            provider: Provider key reported to Cerver.
            api_token: Bearer token; falls back to ``CERVER_API_TOKEN`` or a
                previously persisted token.
        """
        self.cerver_url = _normalize_base_url(
            cerver_url or os.environ.get("CERVER_GATEWAY_URL") or DEFAULT_CERVER_URL
        )
        self.owner_id = owner_id or os.environ.get("CERVER_OWNER_ID")
        self.local_port = local_port
        self.machine_name = machine_name
        self.provider = provider
        self.api_token = api_token or os.environ.get("CERVER_API_TOKEN")
        self.user_id: Optional[str] = None
        self.compute_id: Optional[str] = None

        self._load_persisted_auth()
        self._load_persisted_identity()

    @property
    def enabled(self) -> bool:
        """True when a gateway URL is configured (the client can register)."""
        return bool(self.cerver_url)

    def _auth_key(self) -> str:
        """State-file key under which this gateway's auth token is stored."""
        return f"auth::{self.cerver_url}"

    def _identity_key(self) -> str:
        """State-file key identifying this compute within the gateway.

        Combines gateway URL, owner (or authenticated user, else "anonymous"),
        provider, and local port so the same machine reuses one ``compute_id``
        per distinct configuration.
        """
        owner_key = self.owner_id or self.user_id or "anonymous"
        return f"{self.cerver_url}|{owner_key}|{self.provider}|{self.local_port}"

    def _load_persisted_auth(self) -> None:
        """Restore a saved access token and user id from the state file."""
        state = _load_cerver_state()
        auth_state = state.get(self._auth_key())
        if not isinstance(auth_state, dict):
            return

        access_token = auth_state.get("access_token")
        user_id = auth_state.get("user_id")
        if isinstance(access_token, str) and access_token:
            self.api_token = access_token
        if isinstance(user_id, str) and user_id:
            self.user_id = user_id

    def _load_persisted_identity(self) -> None:
        """Restore the saved ``compute_id`` for this identity, if present."""
        state = _load_cerver_state()
        compute_id = state.get(self._identity_key())
        if isinstance(compute_id, str) and compute_id:
            self.compute_id = compute_id

    def _persist_auth(self) -> None:
        """Save the current access token and user id to the state file."""
        if not self.api_token:
            return

        state = _load_cerver_state()
        state[self._auth_key()] = {
            "access_token": self.api_token,
            "user_id": self.user_id,
        }
        _save_cerver_state(state)

    def _clear_persisted_auth(self) -> None:
        """Wipe the saved cerver token so the next ensure_authenticated call
        re-runs the device-code flow. Used when cerver rejects our token
        with 401 (rotated, revoked, or never matched the account)."""
        self.api_token = None
        self.user_id = None
        state = _load_cerver_state()
        if self._auth_key() in state:
            state.pop(self._auth_key(), None)
            _save_cerver_state(state)

    def _persist_identity(self) -> None:
        """Save the current ``compute_id`` to the state file under this identity."""
        if not self.compute_id:
            return

        state = _load_cerver_state()
        state[self._identity_key()] = self.compute_id
        _save_cerver_state(state)

    def ensure_compute_id(self) -> str:
        """Return this compute's id, generating and persisting one if needed.

        Allocates a stable ``comp_<hex>`` id on first use so the local side can
        reference the compute before the gateway has confirmed registration.
        """
        if not self.compute_id:
            self.compute_id = f"comp_{uuid.uuid4().hex[:16]}"
            self._persist_identity()
        return self.compute_id

    def _headers(self) -> Dict[str, str]:
        """Build request headers, adding a bearer token when authenticated."""
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _build_metadata(self) -> Dict[str, Any]:
        """Assemble the machine metadata block sent on register/heartbeat."""
        machine_state = get_machine_state()
        return {
            "machine_name": self.machine_name,
            "mode": machine_state.get("mode"),
            "working_directory": machine_state.get("working_directory"),
            "home_directory": machine_state.get("home_directory"),
            "relay": machine_state.get("relay"),
            "relay_machine_id": machine_state.get("machine_id"),
        }

    def _build_connection(self) -> Dict[str, Any]:
        """Describe how the gateway reaches this compute (cerver_connect).

        Includes the ``connect_id`` and, when ``P69_API_TOKEN`` is set, the
        local API token the gateway must present to call back into this runtime.
        """
        connection: Dict[str, Any] = {
            "transport": "cerver_connect",
            "connect_id": self.ensure_compute_id(),
        }

        local_api_token = os.environ.get("P69_API_TOKEN")
        if local_api_token:
            connection["api_token"] = local_api_token

        return connection

    def _build_register_payload(self) -> Dict[str, Any]:
        """Build the full ``/v2/computes/register`` request body.

        Bundles the label, kind, provider, runtime capabilities, machine
        metadata, connection descriptor, and the ensured ``compute_id``.
        """
        payload: Dict[str, Any] = {
            "label": self.machine_name,
            "kind": "local",
            "provider": self.provider,
            "capabilities": get_runtime_capabilities(),
            "metadata": self._build_metadata(),
            "connection": self._build_connection(),
        }
        payload["compute_id"] = self.ensure_compute_id()
        return payload

    async def ensure_authenticated(self) -> None:
        """Obtain an access token via the browser device-code flow, if needed.

        No-op when a token is already present. Otherwise starts a device-code
        session against ``/v2/auth/device``, prints (and tries to open) the
        verification URL and user code, then polls until the user approves,
        denies, or the request expires. On approval the token and user id are
        stored and persisted.

        Raises:
            RuntimeError: If approval returns no token, the user denies the
                request, or it times out / expires before approval.
        """
        if self.api_token:
            return

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.cerver_url}/v2/auth/device",
                headers={"Content-Type": "application/json"},
                json={"machine_name": self.machine_name},
            )
            response.raise_for_status()
            start_payload = response.json()

        device_code = start_payload["device_code"]
        user_code = start_payload["user_code"]
        verification_uri = start_payload["verification_uri"]
        expires_in = int(start_payload.get("expires_in", 900))
        interval = int(start_payload.get("interval", 5))

        print("\n[Cerver] Starting browser login...")
        print(f"[Cerver] Visit: {verification_uri}")
        print(f"[Cerver] Code:  {user_code}")

        try:
            webbrowser.open(verification_uri)
        except Exception:
            pass

        deadline = asyncio.get_running_loop().time() + expires_in
        async with httpx.AsyncClient(timeout=30.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(interval)
                poll_response = await client.get(
                    f"{self.cerver_url}/v2/auth/device",
                    params={"device_code": device_code},
                )
                poll_payload = poll_response.json()

                if poll_payload.get("status") == "approved":
                    access_token = poll_payload.get("access_token")
                    user_id = poll_payload.get("user_id")
                    if not isinstance(access_token, str) or not access_token:
                        raise RuntimeError("Cerver auth approval did not return an access token")

                    self.api_token = access_token
                    self.user_id = user_id if isinstance(user_id, str) else None
                    self._persist_auth()
                    self._load_persisted_identity()
                    print("[Cerver] Login successful")
                    return

                error = poll_payload.get("error")
                if error == "access_denied":
                    raise RuntimeError("Cerver login was denied")
                if error == "expired_token":
                    raise RuntimeError("Cerver login expired before approval")

        raise RuntimeError("Timed out waiting for Cerver login approval")

    async def _post_with_auth_retry(self, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
        """POST with one automatic re-auth on 401. The saved cerver token can
        go stale (rotated, revoked, account migrated). Rather than asking the
        user to edit ~/.kompany/cerver_compute.json by hand, clear the token
        and trigger the device-code flow once, then retry the call."""
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.cerver_url}{path}",
                    headers=self._headers(),
                    json=json_body,
                )
            if response.status_code == 401 and attempt == 0 and self.api_token:
                print("[Cerver] Saved token rejected (401) — re-running browser login")
                self._clear_persisted_auth()
                await self.ensure_authenticated()
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Cerver auth retry exhausted")

    async def register(self) -> Dict[str, Any]:
        """Register this machine as a compute with the Cerver gateway.

        Authenticates first when neither a token nor an owner id is available,
        then POSTs the register payload (with one automatic re-auth on 401).
        Adopts the server-resolved ``compute_id`` and ``owner_id`` from the
        response and persists the identity.

        Returns:
            The gateway's registration response payload.

        Raises:
            RuntimeError: If the client has no configured ``cerver_url``.
        """
        if not self.enabled:
            raise RuntimeError("Cerver compute client is missing cerver_url")

        if not self.api_token and not self.owner_id:
            await self.ensure_authenticated()

        payload = await self._post_with_auth_retry(
            "/v2/computes/register", self._build_register_payload()
        )

        compute_id = payload.get("compute_id")
        if isinstance(compute_id, str) and compute_id:
            self.compute_id = compute_id
            self._persist_identity()

        # Use the server-resolved owner_id (may differ from local if auth mapped it)
        server_owner = payload.get("owner_id")
        if isinstance(server_owner, str) and server_owner:
            self.owner_id = server_owner

        return payload

    async def heartbeat(self, status: str = "online") -> Dict[str, Any]:
        """Send a liveness heartbeat, registering first if not yet registered.

        Reports the given status along with the current runtime capabilities and
        machine metadata so the gateway keeps the compute marked available.

        Args:
            status: Liveness state to report (default ``"online"``).

        Returns:
            The gateway's heartbeat response payload.

        Raises:
            RuntimeError: If registration still yields no ``compute_id``.
        """
        if not self.compute_id:
            await self.register()

        if not self.compute_id:
            raise RuntimeError("Cerver compute registration did not return a compute_id")

        return await self._post_with_auth_retry(
            f"/v2/computes/{self.compute_id}/heartbeat",
            {
                "status": status,
                "capabilities": get_runtime_capabilities(),
                "metadata": self._build_metadata(),
            },
        )

    async def list_computes(self) -> Dict[str, Any]:
        """Return every compute the authenticated account can use."""
        if not self.api_token and not self.owner_id:
            await self.ensure_authenticated()

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{self.cerver_url}/v2/computes",
                headers=self._headers(),
            )
        if response.status_code == 401 and self.api_token:
            print("[Cerver] Saved token rejected (401) — re-running browser login")
            self._clear_persisted_auth()
            await self.ensure_authenticated()
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.cerver_url}/v2/computes",
                    headers=self._headers(),
                )
        response.raise_for_status()
        return response.json()

    async def list_sessions(self, limit: int = 20) -> Dict[str, Any]:
        """Return recent Cerver sessions for the authenticated account.

        The gateway returns transcript-light summaries, so this is safe to
        poll from the relay TUI. Runtime filters these rows to this compute.
        """
        if not self.api_token and not self.owner_id:
            await self.ensure_authenticated()

        params = {"limit": str(max(1, min(limit, 50)))}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{self.cerver_url}/v2/sessions",
                headers=self._headers(),
                params=params,
            )
        if response.status_code == 401 and self.api_token:
            print("[Cerver] Saved token rejected (401) — re-running browser login")
            self._clear_persisted_auth()
            await self.ensure_authenticated()
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.cerver_url}/v2/sessions",
                    headers=self._headers(),
                    params=params,
                )
        response.raise_for_status()
        return response.json()

    async def unregister(self) -> None:
        """Best-effort removal of this compute from the gateway on shutdown.

        No-op when nothing was registered. Any error during the DELETE is
        swallowed so shutdown is never blocked by an unreachable gateway.
        """
        if not self.compute_id:
            return

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.delete(
                    f"{self.cerver_url}/v2/computes/{self.compute_id}",
                    headers=self._headers(),
                )
                response.raise_for_status()
        except Exception:
            return
