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
    """Strip surrounding whitespace and a trailing slash from a base URL.

    Keeps the persisted state keys (which embed ``cerver_url``) stable
    regardless of how the URL was supplied, so ``https://gateway.cerver.ai``
    and ``https://gateway.cerver.ai/`` resolve to the same auth entry.

    Args:
        url: The raw gateway URL, possibly with padding or a trailing slash.

    Returns:
        The normalized URL with no trailing slash.
    """
    return url.strip().rstrip("/")


def _load_cerver_state() -> Dict[str, Any]:
    """Read the on-disk cerver compute state file.

    The state file (``~/.kompany/cerver_compute.json``) holds persisted auth
    tokens and compute identities keyed by gateway URL and identity tuple.

    Returns:
        The parsed state dictionary, or an empty dict if the file does not
        exist or cannot be parsed.
    """
    if not CERVER_COMPUTE_STATE_FILE.exists():
        return {}

    try:
        return json.loads(CERVER_COMPUTE_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_cerver_state(state: Dict[str, Any]) -> None:
    """Write the cerver compute state to disk, creating the config dir if needed.

    Args:
        state: The full state dictionary to serialize and persist to
            ``~/.kompany/cerver_compute.json``.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CERVER_COMPUTE_STATE_FILE.write_text(json.dumps(state, indent=2))


class CerverComputeClient:
    """Registers the local runtime with Cerver as a private compute.

    A single instance owns the lifecycle of one local machine's presence in
    Cerver: it authenticates the operator (via a stored token or the device-code
    browser flow), registers the machine as a ``local`` compute, keeps it alive
    with heartbeats, and unregisters it on shutdown. It can also list the
    account's computes and recent sessions for display in the relay TUI.

    Auth tokens and the compute identity are persisted to
    ``~/.kompany/cerver_compute.json`` so the machine reconnects under the same
    identity across restarts without re-running the login flow.

    Attributes:
        cerver_url: Normalized gateway base URL.
        owner_id: Account owner id, if known up front or resolved by the server.
        local_port: Local relay port advertised in the connection block.
        machine_name: Human-readable label for this compute.
        provider: Provider key reported to the gateway.
        api_token: Bearer token for authenticated calls, if present.
        user_id: Account user id resolved during authentication.
        compute_id: Stable identifier for this compute, persisted across runs.
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

        Configuration falls back to environment variables when arguments are
        omitted: ``CERVER_GATEWAY_URL`` for the URL, ``CERVER_OWNER_ID`` for the
        owner, and ``CERVER_API_TOKEN`` for the token. Persisted auth/identity
        from a previous run is loaded immediately so a returning machine keeps
        its token and compute id.

        Args:
            cerver_url: Gateway base URL; falls back to the env var, then the
                default gateway.
            owner_id: Account owner id; falls back to ``CERVER_OWNER_ID``.
            local_port: Local relay port to advertise in the connection block.
            machine_name: Human-readable label for this compute.
            provider: Provider key reported to the gateway.
            api_token: Bearer token; falls back to ``CERVER_API_TOKEN``.
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
        """Whether the client has a gateway URL to talk to.

        Returns:
            ``True`` if a non-empty ``cerver_url`` is configured.
        """
        return bool(self.cerver_url)

    def _auth_key(self) -> str:
        """Build the state-file key under which this gateway's auth is stored.

        Returns:
            A key scoped to the gateway URL, so tokens for different gateways
            never collide in the shared state file.
        """
        return f"auth::{self.cerver_url}"

    def _identity_key(self) -> str:
        """Build the state-file key for this machine's compute identity.

        The key combines gateway URL, owner (or user, or ``"anonymous"``),
        provider, and local port so the same physical machine reuses one
        compute id per logical configuration.

        Returns:
            The identity key string used to persist and look up ``compute_id``.
        """
        owner_key = self.owner_id or self.user_id or "anonymous"
        return f"{self.cerver_url}|{owner_key}|{self.provider}|{self.local_port}"

    def _load_persisted_auth(self) -> None:
        """Load a saved access token and user id for this gateway, if any.

        Populates ``api_token`` and ``user_id`` from the state file when a
        valid auth entry exists. Missing or malformed entries are ignored.
        """
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
        """Load the saved compute id for this identity key, if any.

        Populates ``compute_id`` from the state file so the machine re-registers
        under the same identity it used on a previous run.
        """
        state = _load_cerver_state()
        compute_id = state.get(self._identity_key())
        if isinstance(compute_id, str) and compute_id:
            self.compute_id = compute_id

    def _persist_auth(self) -> None:
        """Write the current access token and user id to the state file.

        No-op when there is no token to save.
        """
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
        """Write the current compute id to the state file under the identity key.

        No-op when no compute id has been assigned yet.
        """
        if not self.compute_id:
            return

        state = _load_cerver_state()
        state[self._identity_key()] = self.compute_id
        _save_cerver_state(state)

    def ensure_compute_id(self) -> str:
        """Return this machine's compute id, generating and persisting one if absent.

        A freshly generated id has the form ``comp_<16 hex chars>`` and is
        saved immediately so it survives restarts.

        Returns:
            The stable compute id for this machine.
        """
        if not self.compute_id:
            self.compute_id = f"comp_{uuid.uuid4().hex[:16]}"
            self._persist_identity()
        return self.compute_id

    def _headers(self) -> Dict[str, str]:
        """Build request headers, adding a bearer token when authenticated.

        Returns:
            A headers dict with JSON content type, plus an ``Authorization``
            header when ``api_token`` is set.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _build_metadata(self) -> Dict[str, Any]:
        """Assemble the metadata block describing this machine to the gateway.

        Pulls live machine state (mode, working/home directories, relay info)
        so heartbeats and registrations report the current runtime context.

        Returns:
            A metadata dict sent with register and heartbeat payloads.
        """
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
        """Build the connection block telling the gateway how to reach this compute.

        Advertises the ``cerver_connect`` transport keyed by the compute id, and
        includes the local API token (``P69_API_TOKEN``) when one is set so the
        gateway can authenticate calls back into the relay.

        Returns:
            The connection dict embedded in the registration payload.
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
        """Assemble the full payload for the compute registration call.

        Combines the machine label, kind, provider, runtime capabilities,
        metadata, connection block, and compute id into the body posted to
        ``/v2/computes/register``.

        Returns:
            The registration request body.
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
        """Ensure a valid access token, running the device-code flow if needed.

        Returns immediately when a token is already present. Otherwise it starts
        the OAuth device-code flow against ``/v2/auth/device``: it prints the
        verification URL and user code, opens the browser, then polls until the
        operator approves, the request is denied, or the code expires. On
        approval the token and user id are saved and the persisted identity is
        reloaded.

        Raises:
            RuntimeError: If approval returns no access token, the login is
                denied, the code expires, or polling times out.
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
        """Register this machine as a compute with the gateway.

        Authenticates first if neither a token nor an owner id is available,
        then posts the registration payload (with one automatic re-auth on
        401). The server-assigned ``compute_id`` is persisted, and the
        server-resolved ``owner_id`` is adopted in case auth mapped it to a
        different account than the local config assumed.

        Returns:
            The gateway's registration response payload.

        Raises:
            RuntimeError: If the client has no gateway URL configured.
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
        """Send a liveness heartbeat for this compute, registering first if needed.

        Lazily registers when no compute id exists yet, then posts the current
        status, capabilities, and metadata to the compute's heartbeat endpoint
        (with one automatic re-auth on 401).

        Args:
            status: The status to report, e.g. ``"online"``.

        Returns:
            The gateway's heartbeat response payload.

        Raises:
            RuntimeError: If registration fails to yield a compute id.
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
        """Remove this compute from the gateway on shutdown.

        Issues a DELETE for the current compute id. No-op when nothing was
        registered. Errors are swallowed so a failed cleanup never blocks
        shutdown.
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
