"""
Relay Client for Kompany Cloud

This module allows a local machine to connect to Kompany Cloud
and receive relayed requests from the web UI.

VERSION: 5

The client:
1. Authenticates using device auth flow (if no cached token)
2. Gets connection config from cloud (stream bridge URL, etc.)
3. Connects to Cloudflare Durable Object via WebSocket
4. Registers as a compute node
5. Receives requests and executes them locally
6. Streams responses back through the DO WebSocket

All communication (request/response AND streaming) flows through
the Cloudflare DO WebSocket. Database access uses Supabase PostgREST.

Features:
- Auto-reconnect with exponential backoff on connection loss
- Health monitoring to detect silent disconnections
- Graceful shutdown handling

Usage:
    branch-monkey-relay
"""

import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

import httpx
import websockets

from .cerver_compute import CerverComputeClient
from .cerver_connect_transport import CerverConnectTransport, set_active_transport
from .connection_logger import connection_logger
from .kompany_local_transport.relay_forwarding import (
    build_local_url,
    execute_local_request as forward_local_request,
)
from .kompany_local_transport.relay_registration import (
    post_cloud_heartbeat,
    post_local_disconnect,
    post_local_heartbeat,
)


# Reconnection settings
INITIAL_RECONNECT_DELAY = 1  # seconds
MAX_RECONNECT_DELAY = 60  # seconds
RECONNECT_BACKOFF_MULTIPLIER = 2
MAX_RECONNECT_ATTEMPTS = None  # None = unlimited
CONNECTION_HEALTH_CHECK_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 60  # seconds - consider connection dead if no heartbeat succeeds


class ConnectionState(Enum):
    """Connection state for the relay client."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"

# Version — number of commits in the relay repo. Bakes at wheel build time
# via hatch_build.VersionWriter (reads from branch_monkey_mcp/_version.py).
# Falls back to a runtime `git rev-list` when developing from a working tree
# without going through the build (pip install -e editable, source checkout).
def _compute_version() -> str:
    try:
        from . import _version  # type: ignore
        count = getattr(_version, "COMMIT_COUNT", "")
        if count and str(count).isdigit():
            return str(count)
    except Exception:
        pass
    try:
        pkg_dir = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(pkg_dir),
            capture_output=True,
            text=True,
            timeout=2,
        )
        count = (result.stdout or "").strip()
        if result.returncode == 0 and count.isdigit():
            return count
    except Exception:
        pass
    return "0"


VERSION = _compute_version()


def _current_commit_sha() -> str:
    """Best-effort: which commit this running process was built from."""
    try:
        from . import _version  # type: ignore
        sha = getattr(_version, "COMMIT_SHA", "")
        if sha:
            return str(sha)
    except Exception:
        pass
    try:
        pkg_dir = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(pkg_dir),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return (result.stdout or "").strip()
    except Exception:
        pass
    return ""


CURRENT_COMMIT_SHA = _current_commit_sha()
# Public github repo to poll for auto-updates. Same URL the install.sh uses
# for uvx; can be overridden via env for forks.
RELAY_GITHUB_REPO = os.environ.get(
    # Canonical repo after the May 2026 rename. Old self-installed
    # relays polling gneyal/p_69_branch_monkey_mcp keep working via
    # GitHub's user+repo rename redirects (our httpx call has
    # follow_redirects=True). New installs poll the canonical URL
    # directly — one fewer redirect hop.
    "RELAY_UPDATE_REPO", "eyal-gor/p_69_cerver_relay"
)
# How often to poll GitHub for a newer commit. Default 10 min — short enough
# to roll out fixes within a coffee break, long enough to stay well under
# GitHub's anonymous 60 req/hour rate limit.
UPDATE_POLL_INTERVAL = int(os.environ.get("RELAY_UPDATE_INTERVAL_S", "600"))

# Config file location
CONFIG_DIR = Path.home() / ".kompany"
TOKEN_FILE = CONFIG_DIR / "relay_token.json"
MACHINE_ID_FILE = CONFIG_DIR / "machine_id"
PERSISTENT_CONFIG_FILE = CONFIG_DIR / "config.json"

# Cerver-side config — Infisical Universal Auth creds the relay uses to
# fetch its own secrets at runtime. Lives under ~/.cerver/ to keep the
# split clean between "kompany account state" (~/.kompany/) and "cerver
# runtime secrets" (~/.cerver/). The install script (or the relay's first
# launch, see _bootstrap_cerver_credentials) writes this file.
CERVER_DIR = Path.home() / ".cerver"
CERVER_INFISICAL_ENV = CERVER_DIR / "infisical.env"

# Cloud API URL - fallback if /api/config fetch fails
FALLBACK_CLOUD_URL = "https://kompany.dev"

# Stream bridge URL - Cloudflare Durable Object for direct streaming
DEFAULT_STREAM_BRIDGE_URL = "https://stream-bridge.gneyal.workers.dev"


def fetch_cloud_url_from_config(fallback_url: str = FALLBACK_CLOUD_URL) -> str:
    """
    Fetch the cloud URL from the /api/config endpoint.
    This makes the relay domain-agnostic by reading the configured appDomain.
    """
    try:
        import httpx
        response = httpx.get(f"{fallback_url}/api/config", timeout=5.0)
        if response.status_code == 200:
            config = response.json()
            app_domain = config.get("appDomain")
            if app_domain:
                cloud_url = f"https://{app_domain}"
                print(f"[Relay] Using domain from config: {app_domain}")
                return cloud_url
    except Exception as e:
        print(f"[Relay] Could not fetch config: {e}")
    return fallback_url


def load_persistent_config() -> Dict[str, Any]:
    """Load persistent relay settings (home_dir, etc.)."""
    if PERSISTENT_CONFIG_FILE.exists():
        try:
            with open(PERSISTENT_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_persistent_config(updates: Dict[str, Any]):
    """Save persistent relay settings (merges with existing)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = load_persistent_config()
    config.update(updates)
    with open(PERSISTENT_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# Will be resolved at runtime
DEFAULT_CLOUD_URL = FALLBACK_CLOUD_URL


class RelayClient:
    """
    Relay client that connects local machine to Kompany Cloud
    using Cloudflare Durable Object WebSocket.

    All communication (request/response relay AND streaming) flows through
    the DO WebSocket. Database access uses Supabase PostgREST (unchanged).

    Handles:
    - Device authentication flow
    - DO WebSocket connection (sole transport)
    - Request/response relay
    - Agent output streaming
    - Auto-reconnection with exponential backoff
    - Health monitoring
    - Compute node registration
    """

    def __init__(
        self,
        cloud_url: str = DEFAULT_CLOUD_URL,
        local_port: int = 18081,
        machine_name: Optional[str] = None,
        tui=None,
        cerver_url: Optional[str] = None,
        cerver_owner_id: Optional[str] = None,
        cerver_api_token: Optional[str] = None,
    ):
        self.cloud_url = cloud_url.rstrip("/")
        self.local_port = local_port
        self.machine_name = machine_name or self._get_machine_name()
        self.machine_id = self._get_stable_machine_id()

        # Seed _relay_status with our machine_id immediately. The legacy
        # stream-bridge connection used to be the only thing populating it
        # (via _send_local_heartbeat after a successful WebSocket connect),
        # so cerver-only relays with no stream-bridge ended up registering
        # under metadata.relay_machine_id=None — and kompany's compute
        # lookup couldn't match them. The id is stable across restarts, so
        # there's no harm setting it before the WS comes up.
        try:
            from .bridge_and_local_actions.config import update_relay_status
            update_relay_status(
                connected=False,
                machine_id=self.machine_id,
                machine_name=self.machine_name,
                cloud_url=self.cloud_url,
            )
            # update_relay_status only sets machine_id when connected=True;
            # set it directly so the cerver_compute metadata picks it up.
            from .bridge_and_local_actions import config as _cfg
            _cfg._relay_status["machine_id"] = self.machine_id
            _cfg._relay_status["machine_name"] = self.machine_name
        except Exception:
            pass
        self.cerver_url = cerver_url or os.environ.get("CERVER_GATEWAY_URL")
        self.cerver_owner_id = cerver_owner_id or os.environ.get("CERVER_OWNER_ID")
        self.cerver_api_token = cerver_api_token or os.environ.get("CERVER_API_TOKEN")

        # Auth data
        self.access_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.org_id: Optional[str] = None
        self.user_email: Optional[str] = None
        self.org_name: Optional[str] = None

        # Relay config (from cloud)
        self.relay_config: Optional[Dict[str, Any]] = None

        self._running = False

        # Cloudflare DO WebSocket (sole transport for all relay communication)
        self._do_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._do_ws_task: Optional[asyncio.Task] = None
        self._do_ws_reconnect = True
        self.stream_bridge_url: Optional[str] = None

        # Connection state tracking for auto-reconnect
        self.connection_state = ConnectionState.DISCONNECTED
        self.reconnect_attempts = 0
        self.last_successful_heartbeat: Optional[datetime] = None
        self.should_reconnect = True  # False when explicitly disconnected
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cerver_heartbeat_task: Optional[asyncio.Task] = None
        self._local_stats_task: Optional[asyncio.Task] = None
        self._background_tasks: set = set()
        self._do_reconnect_attempts = 0
        self._auth_refreshing = False
        self.tui = tui
        self._request_count = 0
        self._cerver_client: Optional[CerverComputeClient] = None
        self._cerver_connect_transport: Optional[CerverConnectTransport] = None
        self._cerver_connect_task: Optional[asyncio.Task] = None

    def _tui_update(self, **kwargs):
        """Update TUI state if active."""
        if self.tui:
            self.tui.update(**kwargs)

    def _create_tracked_task(self, coro):
        """Create an asyncio task that's tracked for cleanup."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _get_reconnect_delay(self) -> float:
        """Calculate reconnect delay with exponential backoff and jitter."""
        delay = min(
            INITIAL_RECONNECT_DELAY * (RECONNECT_BACKOFF_MULTIPLIER ** self.reconnect_attempts),
            MAX_RECONNECT_DELAY
        )
        # Add jitter (±20%) to prevent thundering herd
        jitter = delay * 0.2 * (random.random() * 2 - 1)
        return delay + jitter

    def _get_machine_name(self) -> str:
        """Generate a human-readable machine name. Prefers saved nickname."""
        saved = load_persistent_config().get("machine_name")
        if saved:
            return saved
        return socket.gethostname()

    def _get_stable_machine_id(self) -> str:
        """Get or create a stable machine ID that persists across restarts."""
        if MACHINE_ID_FILE.exists():
            return MACHINE_ID_FILE.read_text().strip()

        # Generate once, reuse forever
        machine_id = f"{self.machine_name}-{uuid.uuid4().hex[:8]}"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_FILE.write_text(machine_id)
        return machine_id

    def _load_token(self) -> Optional[Dict[str, Any]]:
        """Load cached token and config from file."""
        if TOKEN_FILE.exists():
            try:
                with open(TOKEN_FILE) as f:
                    data = json.load(f)
                    if data.get("access_token") and data.get("cloud_url") == self.cloud_url:
                        print(f"[Relay] Using saved token for {data.get('machine_name', 'unknown')}")
                        return data
            except Exception as e:
                print(f"[Relay] Error loading token: {e}")
        return None

    def _save_token(self, data: Dict[str, Any]):
        """Save token and config to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data["cloud_url"] = self.cloud_url
        data["machine_name"] = self.machine_name
        data["machine_id"] = self.machine_id
        data["saved_at"] = datetime.utcnow().isoformat()

        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[Relay] Token saved to {TOKEN_FILE}")

    def _clear_token(self):
        """Clear cached token."""
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            print(f"[Relay] Cleared cached token")

    async def _fetch_account_info(self):
        """Fetch user email and org name from the cloud API."""
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.org_id:
            headers["X-Org-Id"] = self.org_id

        async with httpx.AsyncClient() as client:
            # Fetch org name (and user email from org membership)
            if not self.org_name:
                try:
                    resp = await client.get(
                        f"{self.cloud_url}/api/organizations",
                        headers=headers,
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        orgs = data.get("organizations", [])

                        # Try to match by org_id, otherwise use first org
                        if self.org_id:
                            for org in orgs:
                                if str(org.get("id")) == str(self.org_id):
                                    self.org_name = org.get("name")
                                    break
                        if not self.org_name and len(orgs) >= 1:
                            self.org_name = orgs[0].get("name")
                            # Also capture org_id if we didn't have one
                            if not self.org_id:
                                self.org_id = str(orgs[0].get("id"))

                        # Some endpoints return user info alongside orgs
                        if not self.user_email:
                            self.user_email = data.get("email") or data.get("user_email")
                except Exception as e:
                    print(f"[Relay] Could not fetch org info: {e}")

            # Fetch user email from /api/me
            if not self.user_email:
                try:
                    resp = await client.get(
                        f"{self.cloud_url}/api/me",
                        headers=headers,
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self.user_email = (
                            data.get("email")
                            or data.get("user_email")
                            or data.get("user", {}).get("email")
                        )
                except Exception:
                    pass  # /api/me may not exist yet

    async def authenticate(self) -> bool:
        """
        Authenticate with the cloud using device auth flow.
        Returns True if authentication succeeds.
        """
        # Try cached token first
        cached = self._load_token()
        if cached:
            self.access_token = cached.get("access_token")
            self.user_id = cached.get("user_id")
            self.org_id = cached.get("org_id")
            self.user_email = cached.get("user_email")
            self.org_name = cached.get("org_name")
            self.relay_config = cached.get("relay_config")
            self.machine_id = cached.get("machine_id", self.machine_id)

            if self.relay_config:
                return True
            else:
                print("[Relay] Cached token missing relay config, re-authenticating...")
                self._clear_token()

        # Start device auth flow
        print("\n[Relay] Starting device authentication...")
        self._tui_update(auth_state="authenticating")
        print(f"[Relay] Connecting to {self.cloud_url}")

        async with httpx.AsyncClient() as client:
            # Request device code
            try:
                response = await client.post(
                    f"{self.cloud_url}/api/auth/device",
                    json={"machine_name": self.machine_name},
                    timeout=30
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                print(f"[Relay] Failed to start device auth: {e}")
                self._tui_update(auth_state="failed")
                return False

            device_code = data["device_code"]
            user_code = data["user_code"]
            verification_uri = data["verification_uri"]
            expires_in = data.get("expires_in", 900)
            interval = data.get("interval", 5)

            # Check if device was auto-approved (trusted device)
            auto_approved = data.get("auto_approved", False)

            if auto_approved:
                print(f"[Relay] Device auto-approved (trusted device)")
                self._tui_update(auth_state="waiting", auth_url=verification_uri, auth_code=user_code)
            else:
                self._tui_update(auth_state="waiting", auth_url=verification_uri, auth_code=user_code)

                print(f"\n{'='*50}")
                print(f"  To authorize this device, visit:")
                print(f"  {verification_uri}")
                print(f"\n  Or go to {self.cloud_url}/approve")
                print(f"  and enter code: {user_code}")
                print(f"{'='*50}\n")

                # Auto-open browser for authentication
                try:
                    webbrowser.open(verification_uri)
                    print(f"[Relay] Opening browser for authentication...")
                except Exception:
                    pass  # Browser open failed, user can manually visit URL

            print(f"[Relay] Waiting for approval (expires in {expires_in//60} minutes)...")

            # Poll for approval
            start_time = time.time()
            poll_count = 0
            while time.time() - start_time < expires_in:
                await asyncio.sleep(interval)
                poll_count += 1

                try:
                    response = await client.get(
                        f"{self.cloud_url}/api/auth/device",
                        params={"device_code": device_code},
                        timeout=30
                    )
                    data = response.json()

                    if data.get("status") == "approved":
                        self.access_token = data["access_token"]
                        self.user_id = data.get("user_id")
                        self.org_id = data.get("org_id")
                        self.user_email = data.get("user_email")
                        self.org_name = data.get("org_name")
                        self.relay_config = data.get("relay_config")

                        if not self.relay_config:
                            print("[Relay] Error: No relay config in response")
                            return False

                        # Fetch account info (user email, org name) if not in auth response
                        await self._fetch_account_info()

                        # Save everything
                        self._save_token({
                            "access_token": self.access_token,
                            "user_id": self.user_id,
                            "org_id": self.org_id,
                            "user_email": self.user_email,
                            "org_name": self.org_name,
                            "relay_config": self.relay_config
                        })

                        print("\n[Relay] Authentication successful!")
                        self._tui_update(auth_state="authenticated")
                        return True

                    elif data.get("error") == "access_denied":
                        print("[Relay] Authentication denied")
                        self._tui_update(auth_state="failed")
                        return False
                    elif data.get("error") == "expired_token":
                        print("[Relay] Device code expired")
                        self._tui_update(auth_state="failed")
                        return False

                    # Still pending
                    if poll_count % 6 == 0:
                        print("[Relay] Still waiting for approval...")

                except Exception as e:
                    print(f"[Relay] Polling error: {e}")

            print("[Relay] Authentication timed out")
            self._tui_update(auth_state="failed")
            return False

    async def _connect_do(self) -> bool:
        """
        Establish the Cloudflare DO WebSocket connection.
        This is the sole transport for all relay communication.
        Returns True if successful, False otherwise.
        """
        self.connection_state = ConnectionState.CONNECTING
        self._tui_update(connection="connecting")

        print(f"\n[Relay] Connecting to stream bridge...")
        if self.user_email:
            print(f"[Relay] User: {self.user_email}")
        if self.org_name:
            print(f"[Relay] Organization: {self.org_name}")
        print(f"[Relay] Machine: {self.machine_name} ({self.machine_id})")
        print(f"[Relay] Local port: {self.local_port}")

        try:
            await self._connect_stream_bridge()

            if not self._do_ws:
                print("[Relay] Failed to establish DO WebSocket connection")
                self.connection_state = ConnectionState.DISCONNECTED
                self._tui_update(connection="disconnected")
                return False

            # Register this machine
            await self._register_machine()

            # Send initial heartbeat to local server
            await self._send_local_heartbeat()

            # Update state
            self.connection_state = ConnectionState.CONNECTED
            self.reconnect_attempts = 0
            self.last_successful_heartbeat = datetime.utcnow()
            self._tui_update(connection="connected", connected_at=datetime.now(timezone.utc))

            connection_logger.log("connected", detail=f"DO bridge {self.stream_bridge_url}")

            print(f"\n[Relay] Connected to stream bridge!")
            print(f"[Relay] Ready to receive requests from cloud\n")

            return True

        except Exception as e:
            print(f"[Relay] Connection failed: {e}")
            self.connection_state = ConnectionState.DISCONNECTED
            self._tui_update(connection="disconnected")
            connection_logger.log("connection_failed", error=str(e))
            return False

    async def _disconnect_do(self):
        """Disconnect from the DO WebSocket."""
        self._do_ws_reconnect = False
        if self._do_ws_task and not self._do_ws_task.done():
            self._do_ws_task.cancel()
            self._do_ws_task = None
        if self._do_ws:
            try:
                await self._do_ws.close()
            except Exception:
                pass
            self._do_ws = None
            self._tui_update(stream_bridge=None)

        # Cancel tracked background tasks
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        self.connection_state = ConnectionState.DISCONNECTED
        self._tui_update(connection="disconnected")
        connection_logger.log("disconnected", detail="DO WebSocket closed")
        print("[Relay] Disconnected")

    async def _connect_stream_bridge(self):
        """Connect to Cloudflare DO stream bridge for direct streaming."""
        # Resolve stream bridge URL from config, env, or default
        url = (
            (self.relay_config or {}).get("stream_bridge_url")
            or os.environ.get("STREAM_BRIDGE_URL")
            or DEFAULT_STREAM_BRIDGE_URL
        )
        # Allow disabling with empty string
        if not url:
            print("[Relay] Stream bridge disabled (empty URL)")
            return

        self.stream_bridge_url = url
        ws_url = url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        ws_url = f"{ws_url}/relay/{self.machine_id}?token={self.access_token}"

        try:
            self._do_ws = await websockets.connect(ws_url, ping_interval=30, ping_timeout=10)
            self._do_ws_reconnect = True
            self._do_reconnect_attempts = 0  # Reset backoff on success
            print(f"[Relay] Connected to stream bridge: {self.stream_bridge_url}")
            connection_logger.log("stream_bridge_connected", detail=self.stream_bridge_url)
            self._tui_update(stream_bridge=True)

            # Start listener for incoming messages (stream_start from browsers)
            self._do_ws_task = asyncio.create_task(self._do_ws_listen())
        except Exception as e:
            print(f"[Relay] Could not connect to stream bridge: {e}")
            connection_logger.log("stream_bridge_failed", error=str(e))
            self._do_ws = None
            self._tui_update(stream_bridge=str(e)[:60])

    async def _do_ws_listen(self):
        """Listen for messages from the DO stream bridge (browser → relay, cloud → relay)."""
        try:
            async for raw in self._do_ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")
                if msg_type == "stream_start":
                    self._create_tracked_task(self._handle_stream_start(data, via_do=True))
                elif msg_type == "stream_stop":
                    print(f"[Relay] Stream stop via DO: stream_id={data.get('stream_id')}")
                elif msg_type == "request":
                    # HTTP request relayed from cloud via DO
                    self._create_tracked_task(self._handle_do_request(data))
                elif msg_type == "disconnect":
                    print(f"\n[Relay] Received disconnect command via DO bridge")
                    self._create_tracked_task(self._shutdown())
                elif msg_type == "ping":
                    try:
                        await self._do_ws.send(json.dumps({"type": "pong"}))
                    except Exception:
                        pass
        except websockets.ConnectionClosed as e:
            print(f"[Relay] Stream bridge disconnected: code={e.code} reason={e.reason}")
            connection_logger.log("stream_bridge_disconnected", error=str(e))
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[Relay] Stream bridge listener error: {e}")
            connection_logger.log("stream_bridge_error", error=str(e))
        finally:
            self._do_ws = None
            self._tui_update(stream_bridge=False, connection="disconnected")
            # DO WebSocket is the sole transport — disconnection means full reconnect
            if self._do_ws_reconnect and self._running:
                self.connection_state = ConnectionState.DISCONNECTED
                await self._trigger_reconnect()

    async def _handle_do_request(self, data: Dict[str, Any]):
        """Handle an HTTP request relayed from cloud via the DO WebSocket.

        Executes the request on the local server and sends the response
        back through the DO WebSocket so the DO can return it as an HTTP response.
        """
        request_id = data.get("id")
        method = data.get("method", "GET")
        path = data.get("path", "/")

        self._request_count += 1
        self._tui_update(requests_handled=self._request_count)
        print(f"[Relay] DO request: {method} {path} (id={request_id})")

        response = await self._execute_local_request(data)

        # Send response back via DO WebSocket
        try:
            if self._do_ws:
                await self._do_ws.send(json.dumps(response))
                print(f"[Relay] DO response sent: status={response.get('status')} (id={request_id})")
            else:
                print(f"[Relay] Cannot send DO response — WebSocket disconnected (id={request_id})")
        except Exception as e:
            print(f"[Relay] Failed to send DO response: {e}")
            connection_logger.log("do_response_failed", error=str(e), detail=f"id={request_id}")

    async def _send_stream_data(self, use_do: bool, data: dict):
        """Send stream data via DO WebSocket."""
        try:
            if self._do_ws:
                await self._do_ws.send(json.dumps(data))
            else:
                raise ConnectionError("DO WebSocket not connected")
        except Exception as e:
            connection_logger.log("stream_send_failed", error=str(e), detail="stream_event")
            raise  # Re-raise so callers can handle stream failures

    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        if not self.should_reconnect:
            return

        self.connection_state = ConnectionState.RECONNECTING

        while self.should_reconnect and self._running:
            # Check max attempts
            if MAX_RECONNECT_ATTEMPTS is not None and self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                print(f"[Relay] Max reconnect attempts ({MAX_RECONNECT_ATTEMPTS}) reached. Giving up.")
                self._running = False
                return

            delay = self._get_reconnect_delay()
            self.reconnect_attempts += 1
            self._tui_update(connection="reconnecting", reconnect_count=self.reconnect_attempts)

            connection_logger.log(
                "reconnecting",
                detail=f"Attempt {self.reconnect_attempts}",
                attempt=self.reconnect_attempts,
                delay=delay,
            )
            print(f"[Relay] Reconnecting in {delay:.1f}s (attempt {self.reconnect_attempts})...")
            await asyncio.sleep(delay)

            if not self.should_reconnect or not self._running:
                return

            try:
                # Clean up old connection
                await self._disconnect_do()

                # Attempt reconnection
                if await self._connect_do():
                    connection_logger.log(
                        "reconnected",
                        detail=f"After {self.reconnect_attempts} attempts",
                        attempt=self.reconnect_attempts,
                    )
                    print(f"[Relay] Reconnected successfully!")
                    return

            except Exception as e:
                print(f"[Relay] Reconnection attempt {self.reconnect_attempts} failed: {e}")
                continue

    async def _trigger_reconnect(self):
        """Trigger a reconnection attempt."""
        if self.connection_state == ConnectionState.RECONNECTING:
            return  # Already reconnecting

        print("[Relay] Triggering reconnection...")
        self.connection_state = ConnectionState.DISCONNECTED

        # Cancel existing reconnect task if any
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        # Start reconnection
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _check_do_alive(self) -> bool:
        """Test DO WebSocket liveness by sending a ping."""
        if not self._do_ws:
            return False
        try:
            await self._do_ws.send(json.dumps({"type": "ping"}))
            return True
        except Exception as e:
            connection_logger.log("do_ping_failed", error=str(e), detail="liveness probe")
            return False

    async def _refresh_auth(self):
        """Re-authenticate in background when token expires (401)."""
        if self._auth_refreshing:
            return
        self._auth_refreshing = True
        try:
            print("[Relay] Re-authenticating...")
            self._clear_token()
            if await self.authenticate():
                connection_logger.log("auth_refreshed", detail="Token refreshed successfully")
                print("[Relay] Re-authentication successful")
                # Reconnect stream bridge with new token (it uses the relay token)
                if self._do_ws:
                    try:
                        await self._do_ws.close()
                    except Exception:
                        pass
                    self._do_ws = None
                    self._do_reconnect_attempts = 0
                    await self._connect_stream_bridge()
            else:
                connection_logger.log("auth_expired", detail="Re-authentication failed")
                print("[Relay] Re-authentication failed")
        except Exception as e:
            print(f"[Relay] Re-authentication error: {e}")
        finally:
            self._auth_refreshing = False

    async def _health_check_loop(self):
        """Monitor DO WebSocket connection health.

        Tests the DO WebSocket liveness every 30s by sending a ping.
        The DO WebSocket's built-in ping/pong (websockets library) handles
        most detection, but this provides an application-level check.
        """
        liveness_failures = 0

        while self._running:
            try:
                await asyncio.sleep(CONNECTION_HEALTH_CHECK_INTERVAL)

                if self.connection_state != ConnectionState.CONNECTED:
                    liveness_failures = 0
                    continue

                # DO WebSocket liveness test
                alive = await self._check_do_alive()

                if alive:
                    if liveness_failures > 0:
                        connection_logger.log("do_liveness_recovered", detail=f"After {liveness_failures} failures")
                    liveness_failures = 0
                else:
                    liveness_failures += 1
                    print(f"[Relay] DO liveness probe failed ({liveness_failures}x)")

                    # 4 consecutive failures (2 min) → connection is dead, reconnect
                    # The websockets library ping/pong catches most real disconnects.
                    # This is a secondary safety net — no need to be aggressive.
                    if liveness_failures >= 4:
                        connection_logger.log(
                            "health_check_triggered_reconnect",
                            detail=f"DO liveness failed {liveness_failures}x",
                            reason="do_dead",
                        )
                        print(f"[Relay] DO connection appears dead — reconnecting")
                        liveness_failures = 0
                        await self._trigger_reconnect()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Relay] Health check error: {e}")

    async def connect(self):
        """
        Connect to the stream bridge DO and start receiving requests.
        Includes auto-reconnect on connection loss.
        """
        if not self.relay_config:
            if not await self.authenticate():
                print("[Relay] Authentication failed, cannot connect")
                return

        # Fetch account info if not cached (e.g. old token file)
        if not self.user_email or not self.org_name:
            await self._fetch_account_info()
            # Re-save token with updated info
            if self.user_email or self.org_name:
                self._save_token({
                    "access_token": self.access_token,
                    "user_id": self.user_id,
                    "org_id": self.org_id,
                    "user_email": self.user_email,
                    "org_name": self.org_name,
                    "relay_config": self.relay_config,
                })

        self._tui_update(
            user_email=self.user_email,
            org_name=self.org_name,
        )

        # Startup pull — match the cerver-only path. If the wheel uvx
        # cached is behind main, exec --refresh before doing any work.
        if await self._check_for_updates_once():
            return

        self._running = True

        # Bootstrap CERVER_API_TOKEN before attempting cerver registration.
        # Idempotent: if the env var is already set, or the token was
        # cached on a previous launch, this is a no-op. Otherwise it
        # fetches the trio from kompany.dev and the token from Infisical.
        await self._bootstrap_cerver_credentials()

        await self._register_cerver_compute()

        # Initial connection
        if not await self._connect_do():
            print("[Relay] Initial connection failed, starting reconnection loop...")
            await self._reconnect()
            if not self._running:
                return

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        self._local_stats_task = asyncio.create_task(self._local_stats_loop())
        self._cerver_heartbeat_task = asyncio.create_task(self._cerver_heartbeat_loop())
        self._update_check_task = asyncio.create_task(self._update_check_loop())
        self._infisical_refresh_task = asyncio.create_task(self._infisical_refresh_loop())

        try:
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)

                # If disconnected and should reconnect, trigger it
                if (self.connection_state == ConnectionState.DISCONNECTED
                    and self.should_reconnect
                    and (not self._reconnect_task or self._reconnect_task.done())):
                    self._reconnect_task = asyncio.create_task(self._reconnect())

        except KeyboardInterrupt:
            print("\n[Relay] Shutting down...")
        except asyncio.CancelledError:
            print("\n[Relay] Task cancelled, shutting down...")
        finally:
            self._running = False
            self.should_reconnect = False

            # Cancel background tasks
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            if self._health_check_task:
                self._health_check_task.cancel()
            if self._local_stats_task:
                self._local_stats_task.cancel()
            if self._cerver_heartbeat_task:
                self._cerver_heartbeat_task.cancel()
            if self._reconnect_task:
                self._reconnect_task.cancel()

            # Cancel all tracked fire-and-forget tasks
            for task in list(self._background_tasks):
                if not task.done():
                    task.cancel()
            self._background_tasks.clear()

            # Wait for tasks to finish
            tasks = [
                t
                for t in [
                    self._heartbeat_task,
                    self._health_check_task,
                    self._local_stats_task,
                    self._cerver_heartbeat_task,
                ]
                if t
            ]
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass

            await self._stop_cerver_connect_transport()
            await self._unregister_machine()
            await self._unregister_cerver_compute()

    async def _cloud_heartbeat(self, status: str = "online"):
        """Deprecated: kompany no longer hosts /api/relay/heartbeat. The relay
        now reports presence to cerver via the connect channel + the
        registered cerver_local_provider compute. This is a no-op kept so
        existing callers don't crash mid-migration.
        """
        return None

    async def _register_machine(self):
        """Register this machine as a compute node via the cloud API."""
        try:
            await self._cloud_heartbeat("online")
            print(f"[Relay] Registered compute node via cloud API")
            self._tui_update(registered=True)
        except Exception as e:
            print(f"[Relay] Warning: Could not register compute node: {e}")
            self._tui_update(registered=str(e)[:80])

    def _read_cerver_env_file(self) -> Dict[str, str]:
        """Parse ~/.cerver/infisical.env if present. Empty dict if missing."""
        if not CERVER_INFISICAL_ENV.exists():
            return {}
        out: Dict[str, str] = {}
        try:
            for line in CERVER_INFISICAL_ENV.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as e:
            print(f"[Relay] Warning: failed to read {CERVER_INFISICAL_ENV}: {e}")
        return out

    def _write_cerver_env_file(self, creds: Dict[str, str]) -> None:
        """Persist the Infisical Universal Auth trio to ~/.cerver/infisical.env."""
        CERVER_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CERVER_DIR.chmod(0o700)
        except Exception:
            pass
        contents = (
            f"INFISICAL_CLIENT_ID={creds.get('client_id', '')}\n"
            f"INFISICAL_TOKEN={creds.get('client_secret', '')}\n"
            f"INFISICAL_PROJECT_ID={creds.get('project_id', '')}\n"
            f"INFISICAL_ENV={creds.get('environment', 'prod')}\n"
        )
        CERVER_INFISICAL_ENV.write_text(contents)
        try:
            CERVER_INFISICAL_ENV.chmod(0o600)
        except Exception:
            pass

    async def _bootstrap_cerver_credentials(self) -> None:
        """
        Ensure self.cerver_api_token is populated, fetching from Infisical
        on the fly. Idempotent — no-ops if a value is already set.

        Sequence:
          1. If self.cerver_api_token already set (env var or constructor arg) → done.
          2. Look for Infisical Universal Auth creds in ~/.cerver/infisical.env
             (written by `install.sh` historically; written by step 3 below
             on first launch from now on).
          3. If absent, call kompany's /api/account/infisical/relay-credentials
             using the device-auth token we just obtained, save the trio.
          4. Login to Infisical with the trio, fetch CERVER_API_TOKEN, stash
             it on `self.cerver_api_token`.

        The relay's launch command never needs CERVER_API_TOKEN in env — it
        bootstraps itself. The user signs in once (device auth), everything
        else cascades.
        """
        if self.cerver_api_token:
            return

        env = self._read_cerver_env_file()
        client_id = env.get("INFISICAL_CLIENT_ID")
        client_secret = env.get("INFISICAL_TOKEN")
        project_id = env.get("INFISICAL_PROJECT_ID")
        environment = env.get("INFISICAL_ENV") or "prod"

        # Fetch the trio from kompany if we don't have it yet.
        if not (client_id and client_secret and project_id):
            if not self.access_token:
                print("[Relay] No device-auth token; cannot fetch Infisical credentials.")
                return
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{self.cloud_url}/api/account/infisical/relay-credentials",
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        timeout=20
                    )
                if resp.status_code == 404:
                    print("[Relay] No Infisical vault provisioned for this org yet — skipping bootstrap.")
                    return
                resp.raise_for_status()
                payload = resp.json()
                client_id = payload["client_id"]
                client_secret = payload["client_secret"]
                project_id = payload["project_id"]
                environment = payload.get("environment") or "prod"
                self._write_cerver_env_file({
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "project_id": project_id,
                    "environment": environment
                })
                print(f"[Relay] Fetched Infisical credentials for {self.org_name or 'org'}")
            except Exception as e:
                print(f"[Relay] Could not fetch Infisical credentials: {e}")
                return

        # Login to Infisical, then fetch CERVER_API_TOKEN.
        try:
            async with httpx.AsyncClient() as client:
                login = await client.post(
                    "https://app.infisical.com/api/v1/auth/universal-auth/login",
                    json={"clientId": client_id, "clientSecret": client_secret},
                    timeout=15
                )
                login.raise_for_status()
                inf_token = login.json()["accessToken"]
                secret_resp = await client.get(
                    "https://app.infisical.com/api/v3/secrets/raw/CERVER_API_TOKEN",
                    params={
                        "workspaceId": project_id,
                        "environment": environment,
                        "secretPath": "/"
                    },
                    headers={"Authorization": f"Bearer {inf_token}"},
                    timeout=15
                )
                if secret_resp.status_code == 404:
                    print("[Relay] CERVER_API_TOKEN not found in Infisical — set it in /account/settings.")
                    return
                secret_resp.raise_for_status()
                value = secret_resp.json().get("secret", {}).get("secretValue")
                if value:
                    self.cerver_api_token = value
                    print("[Relay] Loaded CERVER_API_TOKEN from Infisical")
        except Exception as e:
            print(f"[Relay] Failed to load CERVER_API_TOKEN from Infisical: {e}")

    def _ensure_cerver_client(self) -> Optional[CerverComputeClient]:
        if not self.cerver_url:
            return None

        owner_id = self.cerver_owner_id or self.user_id
        api_token = self.cerver_api_token or self.access_token

        if (
            self._cerver_client
            and self._cerver_client.owner_id == owner_id
            and self._cerver_client.api_token == api_token
            and self._cerver_client.cerver_url == self.cerver_url.rstrip("/")
        ):
            return self._cerver_client

        self._cerver_client = CerverComputeClient(
            cerver_url=self.cerver_url,
            owner_id=owner_id,
            local_port=self.local_port,
            machine_name=self.machine_name,
            api_token=api_token,
        )
        return self._cerver_client

    def _ensure_cerver_connect_transport(self) -> Optional[CerverConnectTransport]:
        client = self._ensure_cerver_client()
        if not client or not client.api_token or not client.compute_id:
            return None

        if (
            self._cerver_connect_transport
            and self._cerver_connect_transport.cerver_url == client.cerver_url
            and self._cerver_connect_transport.api_token == client.api_token
            and self._cerver_connect_transport.compute_id == client.compute_id
            and self._cerver_connect_transport.local_port == self.local_port
        ):
            return self._cerver_connect_transport

        self._cerver_connect_transport = CerverConnectTransport(
            cerver_url=client.cerver_url,
            api_token=client.api_token,
            compute_id=client.compute_id,
            local_port=self.local_port,
            on_status=self._handle_cerver_connect_status,
            on_connected=self._handle_cerver_connect_connected,
        )
        return self._cerver_connect_transport

    def _handle_cerver_connect_status(self, status: str):
        if status == "connected":
            self._tui_update(cerver_status="connected")
            return

        if status == "connecting":
            self._tui_update(cerver_status="connecting")

    def _handle_cerver_connect_connected(self, payload: Dict[str, Any]):
        client = self._ensure_cerver_client()
        print("[Cerver] Connect channel live")
        self._tui_update(
            cerver_status="connected",
            cerver_compute_id=payload.get("compute_id") or (client.compute_id if client else None),
            cerver_last_heartbeat=datetime.now(timezone.utc),
        )

    async def _start_cerver_connect_transport(self):
        transport = self._ensure_cerver_connect_transport()
        if not transport:
            return

        if self._cerver_connect_task and not self._cerver_connect_task.done():
            return

        self._tui_update(cerver_status="connecting")
        # Register so agent_manager can publish stream events without a direct
        # dependency on the transport instance.
        set_active_transport(transport)
        self._cerver_connect_task = asyncio.create_task(transport.run())

    async def _stop_cerver_connect_transport(self):
        if self._cerver_connect_task and not self._cerver_connect_task.done():
            self._cerver_connect_task.cancel()
            try:
                await asyncio.gather(self._cerver_connect_task, return_exceptions=True)
            except Exception:
                pass
        self._cerver_connect_task = None

        if self._cerver_connect_transport:
            try:
                await self._cerver_connect_transport.close()
            except Exception:
                pass
        self._cerver_connect_transport = None
        set_active_transport(None)

    async def _register_cerver_compute(self):
        client = self._ensure_cerver_client()
        if not client:
            return

        try:
            self._tui_update(cerver_status="connecting")
            payload = await client.register()
            compute_id = payload.get("compute_id")
            if compute_id:
                print(f"[Cerver] Registered local compute {compute_id}")
                self._tui_update(
                    cerver_status="connecting",
                    cerver_compute_id=compute_id,
                    cerver_last_heartbeat=datetime.now(timezone.utc),
                )
                await self._start_cerver_connect_transport()
        except Exception as e:
            print(f"[Cerver] Warning: Could not register compute: {e}")
            self._tui_update(
                cerver_status="error",
                cerver_compute_id=f"error: {str(e)[:60]}",
            )

    async def _cerver_heartbeat_loop(self):
        # HTTP heartbeat keeps the gateway's registration record fresh
        # and lets us re-spin the WS transport if it's missing. It does
        # NOT set cerver_status="connected" anymore: that's the WS
        # transport's job (via _handle_cerver_connect_status). The old
        # behavior here was a bug — heartbeat succeeding only tells us
        # HTTP works, not that the connect channel is live, so a
        # half-open WS would let the TUI keep showing green while
        # sessions 500'd with "no active connect channel".
        while self._running:
            try:
                await asyncio.sleep(60)
                client = self._ensure_cerver_client()
                if not client:
                    continue
                payload = await client.heartbeat("online")
                compute_id = payload.get("compute_id") or client.compute_id
                if compute_id:
                    self._tui_update(
                        cerver_compute_id=compute_id,
                        cerver_last_heartbeat=datetime.now(timezone.utc),
                    )
                    # Restart the WS transport if its task ended OR if
                    # we currently believe it's not connected. The latter
                    # catches "stuck in reconnect backoff" — task is alive
                    # but the channel is in fact down. _start_cerver_…
                    # is idempotent when the task is healthy.
                    task_done = not self._cerver_connect_task or self._cerver_connect_task.done()
                    # State lives on the TUI; in --no-tui runs we have no
                    # local mirror, so skip the not-connected branch
                    # (the task-alive check above still catches real death).
                    not_connected = (
                        self.tui is not None
                        and self.tui.state.get("cerver_status") != "connected"
                    )
                    if task_done:
                        await self._start_cerver_connect_transport()
                    elif not_connected:
                        # Task is running but WS isn't connected — likely
                        # in backoff. Leave it; transport will retry on its
                        # own schedule. Logging surfaces the asymmetry so a
                        # user can pattern-match the symptom.
                        print("[Cerver] heartbeat OK but WS not connected; transport retrying")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Cerver] Heartbeat failed: {e}")
                # Heartbeat failure doesn't necessarily mean the channel
                # is dead, but it's a useful signal — surface as
                # cerver_http_error so the TUI can show "HTTP degraded"
                # without overwriting the channel's own state.
                self._tui_update(cerver_http_error=str(e)[:80])

    async def _check_for_updates_once(self) -> bool:
        """One-shot check: is the live `main` ahead of CURRENT_COMMIT_SHA?
        If yes, exec self with --refresh so the new commit takes effect
        before any work runs. Returns True if it triggered the exec
        (caller should bail; control will not return). False if up-to-date,
        no baseline known, or check failed.
        """
        baseline = CURRENT_COMMIT_SHA
        if not baseline:
            return False
        api_url = f"https://api.github.com/repos/{RELAY_GITHUB_REPO}/commits/main"
        try:
            # follow_redirects=True is required: GitHub returns 301 when the
            # owner has been renamed (e.g. account migrated from gneyal to
            # eyal-gor). Without it the poll silently fails forever and
            # auto-update appears to work but never picks up new commits.
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                return False
            latest_sha = resp.json().get("sha", "")
            if latest_sha and latest_sha != baseline:
                print(f"[Update] startup check: new commit {latest_sha[:8]} (running {baseline[:8]}). Pulling...")
                await self._exec_self_update()
                return True
        except Exception as exc:
            print(f"[Update] startup check skipped: {exc}")
        return False

    async def _infisical_refresh_loop(self):
        """Keep the Infisical secrets cache warm.

        Skips entirely if INFISICAL_TOKEN/INFISICAL_PROJECT_ID aren't set —
        Infisical is opt-in and the relay falls back to host process env.
        Otherwise refreshes on the client's TTL (5 min by default) so a
        rotated key reaches the next CLI spawn within that window.
        """
        from .infisical_client import get_secrets, is_configured
        if not is_configured():
            return
        # Initial fetch immediately so the first agent spawn after boot
        # already has Infisical-managed secrets.
        await get_secrets(force=True)
        while self._running:
            await asyncio.sleep(300)
            try:
                await get_secrets(force=True)
            except Exception as exc:
                print(f"[Infisical] refresh error: {exc}")

    async def _update_check_loop(self):
        """Poll GitHub for a newer commit on main; if found, exec a fresh
        uvx invocation that replaces this process. Lets installed relays
        keep up with bug fixes without the user manually re-running
        install.sh.

        Uses ETag/If-None-Match conditional requests so each unchanged
        poll returns 304 and doesn't count against GitHub's anonymous
        60 req/hour rate limit. With ETag, ~tens of relays at 10-min
        intervals stay well under the cap.
        """
        # First check is delayed so we don't restart-loop right after a
        # restart that's still propagating to GitHub.
        await asyncio.sleep(60)
        baseline = CURRENT_COMMIT_SHA
        if not baseline:
            print("[Update] no baseline commit sha — auto-update disabled")
            return
        api_url = f"https://api.github.com/repos/{RELAY_GITHUB_REPO}/commits/main"
        last_etag: Optional[str] = None
        while self._running:
            try:
                headers = {"Accept": "application/vnd.github+json"}
                if last_etag:
                    headers["If-None-Match"] = last_etag
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    resp = await client.get(api_url, headers=headers)
                if resp.status_code == 304:
                    # No change since last poll — doesn't count against
                    # rate limit per GitHub's docs.
                    pass
                elif resp.status_code == 200:
                    last_etag = resp.headers.get("ETag")
                    latest_sha = resp.json().get("sha", "")
                    if latest_sha and latest_sha != baseline:
                        print(f"[Update] new commit on main ({latest_sha[:8]}); current={baseline[:8]}. Restarting...")
                        self._tui_update(connection="updating")
                        await self._exec_self_update()
                        return
                elif resp.status_code in (403, 429):
                    # Rate-limited. Honor x-ratelimit-reset and back off.
                    reset = resp.headers.get("x-ratelimit-reset")
                    if reset:
                        try:
                            wait_for = max(60, int(reset) - int(time.time()) + 5)
                            print(f"[Update] rate limited; waiting {wait_for}s")
                            await asyncio.sleep(wait_for)
                            continue
                        except Exception:
                            pass
                    print(f"[Update] rate limited; backing off {UPDATE_POLL_INTERVAL * 2}s")
                    await asyncio.sleep(UPDATE_POLL_INTERVAL * 2)
                    continue
                else:
                    print(f"[Update] github poll {resp.status_code}; will retry")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[Update] poll error: {exc}")
            await asyncio.sleep(UPDATE_POLL_INTERVAL)

    async def _exec_self_update(self):
        """Replace this process with a fresh `uvx --refresh ...` so the
        next boot pulls the latest commit. uv re-resolves the git source
        when --refresh is passed; without it the cached wheel sticks.
        Argv preserved so flags (--cerver-url, --no-tui, --dir) carry over.
        """
        try:
            uvx = shutil.which("uvx") or "uvx"
            repo_url = f"git+https://github.com/{RELAY_GITHUB_REPO}.git"
            # Drop the script name (sys.argv[0]) — uvx will produce a new one.
            extra_args = sys.argv[1:]
            cmd = [
                uvx,
                "--refresh",
                "--from",
                repo_url,
                "branch-monkey-relay",
                *extra_args,
            ]
            print(f"[Update] exec: {' '.join(cmd)}")
            # Best-effort cleanup before exec — we don't await long here
            # because os.execvp will replace the process anyway.
            try:
                self._running = False
                await self._unregister_cerver_compute()
            except Exception:
                pass
            os.execvp(cmd[0], cmd)
        except Exception as exc:
            print(f"[Update] self-update exec failed: {exc}")
            # Fallback: exit so KeepAlive (launchd) restarts us. Without
            # --refresh on launchd, the old wheel reloads — but at least
            # we don't keep checking endlessly.
            os._exit(0)

    async def _unregister_cerver_compute(self):
        client = self._ensure_cerver_client()
        if not client:
            return
        await client.unregister()

    async def connect_cerver_only(self):
        """Register this local runtime with Cerver without connecting to Kompany Cloud."""
        if not self.cerver_url:
            print("[Cerver] Missing Cerver URL. Use --cerver-url or CERVER_GATEWAY_URL.")
            return

        # Startup pull: if a newer commit exists on main, exec --refresh
        # before doing any registration. Without this, plain `uvx ...`
        # (no --refresh) loads the cached wheel forever even though
        # `--cerver-only` skips the in-process update loop below.
        if await self._check_for_updates_once():
            return  # _exec_self_update has replaced the process

        self._running = True
        self._tui_update(cerver_only=True)
        await self._register_cerver_compute()
        self._cerver_heartbeat_task = asyncio.create_task(self._cerver_heartbeat_loop())
        # Background poll: keep the running relay up to date without
        # requiring restarts. Same loop the dual-mode path uses.
        self._update_check_task = asyncio.create_task(self._update_check_loop())
        self._infisical_refresh_task = asyncio.create_task(self._infisical_refresh_loop())

        try:
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n[Cerver] Shutting down...")
        except asyncio.CancelledError:
            print("\n[Cerver] Task cancelled, shutting down...")
        finally:
            self._running = False
            if self._cerver_heartbeat_task:
                self._cerver_heartbeat_task.cancel()
                try:
                    await asyncio.gather(self._cerver_heartbeat_task, return_exceptions=True)
                except Exception:
                    pass
            await self._stop_cerver_connect_transport()
            await self._unregister_cerver_compute()

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to cloud API and local server.

        Cloud API failures do NOT trigger DO reconnection — the heartbeat
        and DO WebSocket are independent transport paths. DNS failure to
        kompany.dev says nothing about the WebSocket health.
        The _health_check_loop handles actual connection death separately.
        """
        consecutive_failures = 0

        while self._running:
            try:
                await asyncio.sleep(60)  # 60s is sufficient — frontend checks every 45s

                if self.connection_state != ConnectionState.CONNECTED:
                    continue

                try:
                    # Heartbeat to cloud API (updates compute_nodes table only)
                    await self._cloud_heartbeat("online")

                    # Success - reset failure counter
                    self.last_successful_heartbeat = datetime.utcnow()
                    if consecutive_failures > 0:
                        connection_logger.log("heartbeat_recovered", detail=f"After {consecutive_failures} failures")
                    consecutive_failures = 0
                    self._tui_update(last_heartbeat=datetime.now(timezone.utc))
                    # Don't log every success — only log failures and recoveries

                except Exception as e:
                    error_str = str(e)

                    # Handle 401 Unauthorized — trigger background re-auth
                    if "401" in error_str or "Unauthorized" in error_str:
                        connection_logger.log("auth_expired", detail="Cloud API returned 401, re-authenticating")
                        print(f"[Relay] Cloud API 401 — triggering re-authentication")
                        self._create_tracked_task(self._refresh_auth())
                        continue  # Don't count as heartbeat failure

                    consecutive_failures += 1
                    connection_logger.log(
                        "heartbeat_failed",
                        detail=f"Consecutive failure #{consecutive_failures}",
                        error=error_str,
                    )

                    if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                        print(f"[Relay] Cloud heartbeat failed ({consecutive_failures}x): {e}")

                    # NOTE: We intentionally do NOT trigger reconnect here.
                    # Cloud API failures (DNS, 500s) are independent of the
                    # DO WebSocket connection. The _health_check_loop handles
                    # actual WebSocket liveness.

                # Heartbeat to local server (so dashboard knows relay is connected)
                await self._send_local_heartbeat()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Relay] Heartbeat loop error: {e}")

    async def _send_local_heartbeat(self):
        """Send heartbeat to local server to indicate relay is connected."""
        try:
            await post_local_heartbeat(
                local_port=self.local_port,
                machine_id=self.machine_id,
                machine_name=self.machine_name,
                cloud_url=self.cloud_url,
            )
        except Exception:
            pass  # Local server might not support this yet

    async def _local_stats_loop(self):
        """Poll the local bridge for workload and compute stats."""
        interval = 5 if self.tui else 30  # Fast polling only when TUI is active
        while self._running:
            try:
                await asyncio.sleep(interval)
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"http://127.0.0.1:{self.local_port}/api/local-claude/stats",
                        timeout=5,
                    )
                    response.raise_for_status()
                    data = response.json()

                self._tui_update(
                    server_running=True,
                    agent_counts=data.get("agent_counts", {}),
                    workflow_summary=data.get("workflow_summary", {}),
                    compute=data.get("compute", {}),
                )
            except asyncio.CancelledError:
                break
            except Exception:
                # Local server down — still collect basic compute stats AND
                # read agent counts directly from the in-process manager so
                # the Runtime tab keeps showing live load even when the HTTP
                # local server failed to start (port-binding takeover lost,
                # supervisor restart in progress, etc.). Before this fallback
                # the Agents row showed "0 run 0 paused 0 ready" any time
                # the stats endpoint was unreachable — completely misleading
                # when the manager was actively handling sessions.
                fallback_counts: Dict[str, int] = {}
                try:
                    from .bridge_and_local_actions.agent_manager import agent_manager as _mgr
                    for a in _mgr.list():
                        status = a.get("status") if isinstance(a, dict) else None
                        if not status:
                            continue
                        fallback_counts[status] = fallback_counts.get(status, 0) + 1
                except Exception:
                    pass
                try:
                    import shutil, os as _os
                    cpu_count = _os.cpu_count() or 1
                    load1, _, _ = _os.getloadavg()
                    disk = shutil.disk_usage("/")
                    fallback_compute = {
                        "cpu_percent": round((load1 / cpu_count) * 100, 1),
                        "memory": {},
                        "load": {"one": round(load1, 2), "normalized_percent": round((load1 / cpu_count) * 100, 1)},
                        "disk": {"percent": round((disk.used / disk.total) * 100, 1), "free_bytes": disk.free, "total_bytes": disk.total},
                    }
                    self._tui_update(
                        server_running=False,
                        compute=fallback_compute,
                        agent_counts=fallback_counts,
                    )
                except Exception:
                    self._tui_update(server_running=False, agent_counts=fallback_counts)

    async def _unregister_machine(self):
        """Mark compute node as offline."""
        try:
            await self._cloud_heartbeat("offline")
        except Exception:
            pass
        # Notify local server of disconnection
        try:
            await post_local_disconnect(self.local_port)
        except Exception:
            pass

    async def _shutdown(self):
        """Gracefully shutdown the relay client."""
        connection_logger.log("shutdown", detail="Graceful shutdown")
        print("[Relay] Shutting down gracefully...")
        self._running = False
        self.should_reconnect = False
        await self._stop_cerver_connect_transport()
        await self._unregister_machine()
        await self._unregister_cerver_compute()
        print("[Relay] Disconnected. Goodbye!")
        sys.exit(0)

    async def _handle_stream_start(self, payload: Dict[str, Any], via_do: bool = True):
        """Handle SSE stream start request - connect to local SSE and forward events.

        Args:
            payload: The stream_start message with stream_id and agent_id.
            via_do: Always True — all streaming goes through the DO WebSocket.
        """
        stream_id = payload.get("stream_id")
        agent_id = payload.get("agent_id")

        if not stream_id or not agent_id:
            print(f"[Relay] Stream start missing stream_id or agent_id")
            return

        use_do = True
        transport = "DO bridge"

        url = build_local_url(
            self.local_port, f"/api/local-claude/agents/{agent_id}/stream"
        )
        print(f"[Relay] Starting SSE stream for agent {agent_id}, stream_id={stream_id} via {transport}")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        await self._send_stream_data(use_do, {
                            "stream_id": stream_id,
                            "type": "error",
                            "error": f"Failed to connect to local SSE: {response.status_code}"
                        })
                        return

                    print(f"[Relay] Connected to local SSE for agent {agent_id}")
                    event_count = 0

                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            # Empty line or comment (heartbeat)
                            continue

                        if line.startswith("data: "):
                            data = line[6:]  # Remove "data: " prefix
                            try:
                                event = json.loads(data)
                                event_count += 1
                                event_type = event.get("type", "unknown")
                                if event_count <= 5 or event_count % 10 == 0:
                                    print(f"[Relay] Forwarding event #{event_count} type={event_type} via {transport}")
                                # Forward the event
                                await self._send_stream_data(use_do, {
                                    "stream_id": stream_id,
                                    "event": event
                                })

                                # Check for exit event
                                if event.get("type") == "exit":
                                    print(f"[Relay] Stream ended for agent {agent_id}")
                                    break

                            except json.JSONDecodeError:
                                # Forward raw data if not JSON
                                await self._send_stream_data(use_do, {
                                    "stream_id": stream_id,
                                    "raw": data
                                })

        except Exception as e:
            connection_logger.log(
                "stream_error",
                detail=f"Agent {agent_id}, stream {stream_id}",
                error=str(e),
            )
            print(f"[Relay] Stream error for agent {agent_id}: {e}")
            try:
                await self._send_stream_data(use_do, {
                    "stream_id": stream_id,
                    "type": "error",
                    "error": str(e)
                })
            except Exception:
                pass

    async def _execute_local_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Execute request on local server and return response."""
        try:
            return await forward_local_request(self.local_port, request)

        except Exception as e:
            import traceback
            print(f"[Relay] Request handler exception: {type(e).__name__}: {e}")
            traceback.print_exc()
            return {
                "type": "response",
                "id": request_id,
                "status": 500,
                "body": {"error": str(e) or f"{type(e).__name__}: unknown error"}
            }

    def stop(self):
        """Stop the relay client."""
        self._running = False
        self.should_reconnect = False


def run_relay_client(
    cloud_url: str = DEFAULT_CLOUD_URL,
    local_port: int = 18081,
    machine_name: Optional[str] = None,
    cerver_url: Optional[str] = None,
    cerver_owner_id: Optional[str] = None,
    cerver_api_token: Optional[str] = None,
):
    """
    Run the relay client.
    This is a blocking call that runs until interrupted.
    """
    client = RelayClient(
        cloud_url=cloud_url,
        local_port=local_port,
        machine_name=machine_name,
        cerver_url=cerver_url,
        cerver_owner_id=cerver_owner_id,
        cerver_api_token=cerver_api_token,
    )

    async def main():
        await client.connect()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Relay] Shutting down...")
        client.stop()


async def start_relay_client_async(
    cloud_url: str = DEFAULT_CLOUD_URL,
    local_port: int = 18081,
    machine_name: Optional[str] = None,
    cerver_url: Optional[str] = None,
    cerver_owner_id: Optional[str] = None,
    cerver_api_token: Optional[str] = None,
) -> RelayClient:
    """
    Start the relay client as an async task.
    Returns the client instance for control.
    """
    client = RelayClient(
        cloud_url=cloud_url,
        local_port=local_port,
        machine_name=machine_name,
        cerver_url=cerver_url,
        cerver_owner_id=cerver_owner_id,
        cerver_api_token=cerver_api_token,
    )

    # Start in background task
    asyncio.create_task(client.connect())

    return client


def run_cerver_compute_client(
    cerver_url: Optional[str] = None,
    cerver_owner_id: Optional[str] = None,
    local_port: int = 18081,
    machine_name: Optional[str] = None,
    cerver_api_token: Optional[str] = None,
):
    """
    Run only the Cerver compute registration loop.
    This keeps the local machine available on Cerver without connecting to Kompany Cloud.
    """
    client = RelayClient(
        cloud_url=DEFAULT_CLOUD_URL,
        local_port=local_port,
        machine_name=machine_name,
        cerver_url=cerver_url,
        cerver_owner_id=cerver_owner_id,
        cerver_api_token=cerver_api_token,
    )

    async def main():
        await client.connect_cerver_only()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Cerver] Shutting down...")
        client.stop()


def is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def _port_holder_info(port: int) -> Optional[Dict[str, str]]:
    """Identify the process holding `port`. Returns {pid, command} or None.

    Used at startup to decide whether the port-in-use we're hitting is a
    stale relay we can recycle, or something else we should refuse to
    step on. lsof is on every macOS and most Linux distros — fail
    silently if it's missing or returns garbage.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=2,
        )
        pid = (result.stdout or "").strip().split("\n")[0]
        if not pid.isdigit():
            return None
        ps = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            capture_output=True, text=True, timeout=2,
        )
        return {"pid": pid, "command": (ps.stdout or "").strip()}
    except Exception:
        return None


def _looks_like_relay(command: str) -> bool:
    """Heuristic: is this command line another instance of *us*?

    We try to be specific so we don't auto-kill an unrelated process
    that happens to use port 18081 — match on our own entry-point names
    rather than anything generic like "python".
    """
    needles = ("branch-monkey-relay", "branch_monkey_mcp", "cerver-relay")
    return any(n in command for n in needles)


def _try_take_over_port(port: int, holder: Dict[str, str]) -> bool:
    """When a stale relay holds `port`, kill it so we can bind.

    Returns True if the port is free after the kill (the caller can
    proceed), False otherwise. SIGTERM first with a short grace window,
    then SIGKILL — the relay's signal handler should let it clean up
    cleanly when possible.
    """
    import time
    pid = holder["pid"]
    print(f"[Relay] Port {port} held by stale relay (pid {pid}). Reclaiming.")
    for sig in (15, 9):
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            break
        except Exception as e:
            print(f"[Relay] kill -{sig} {pid} failed: {e}")
            return False
        # Give the OS up to 1.5s to release the port before escalating.
        for _ in range(15):
            time.sleep(0.1)
            if not is_port_in_use(port):
                print(f"[Relay] Reclaimed port {port}.")
                return True
    return not is_port_in_use(port)


def start_server_in_background(port: int = 18081, home_dir: Optional[str] = None, working_dir: Optional[str] = None):
    """Start the local agent server in a background thread.

    If the port is held by another relay process (stale instance,
    crashed launchd job, leftover from a prior session), reclaim it
    automatically — silently running in zombie mode (the old behavior)
    hid bugs for hours during real incident response.

    If the holder can't be identified (transient socket state, lsof
    missing, foreign process), fall back to the legacy behavior:
    print what we found and skip the local server. Exiting hard here
    would leave the gateway-connected half of the relay dead too,
    which is worse than running degraded.
    """
    import threading

    if is_port_in_use(port):
        holder = _port_holder_info(port)
        if holder and _looks_like_relay(holder["command"]):
            if not _try_take_over_port(port, holder):
                print(f"[Relay] Could not reclaim port {port} from pid {holder['pid']} — skipping local server.")
                print(f"[Relay] Run manually if needed: lsof -ti:{port} | xargs kill -9")
                return None
        else:
            who = f"pid {holder['pid']} ({holder['command']})" if holder else "unknown holder (no lsof match)"
            print(f"[Relay] Port {port} is in use ({who}) — skipping local server.")
            print(f"[Relay] Gateway transport will still run. To reclaim: lsof -ti:{port} | xargs kill -9")
            return None

    def run():
        from .bridge_and_local_actions import run_server, set_default_working_dir, set_home_directory
        if home_dir:
            set_home_directory(home_dir)
        if working_dir:
            set_default_working_dir(working_dir)
        elif home_dir:
            set_default_working_dir(home_dir)
        run_server(port=port)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Poll for server readiness (up to 3 seconds)
    import time
    for _ in range(15):
        time.sleep(0.2)
        if is_port_in_use(port):
            print(f"[Relay] Local agent server started on port {port}")
            return thread

    print(f"[Relay] Warning: Server may have failed to start on port {port}")

    return thread


def install_skills() -> bool:
    """
    Install Kompany skills into ~/.claude/skills/kompany/.
    Copies bundled skill files from the package. Overwrites existing files.
    Returns True if any files were installed.
    """
    skills_src = Path(__file__).parent / "skills" / "kompany"
    skills_dst = Path.home() / ".claude" / "skills" / "kompany"

    if not skills_src.exists():
        return False

    try:
        installed = 0
        for src_file in skills_src.rglob("*.md"):
            rel = src_file.relative_to(skills_src)
            dst_file = skills_dst / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            dst_file.write_text(src_file.read_text())
            installed += 1

        if installed > 0:
            print(f"[Skills] Installed {installed} kompany skill file(s) to {skills_dst}")
        return installed > 0
    except Exception as e:
        print(f"[Skills] Warning: Could not install skills: {e}")
        return False


def setup_mcp_config(working_dir: str, cloud_url: str = DEFAULT_CLOUD_URL) -> bool:
    """
    Set up MCP config in the project's .mcp.json file.
    Returns True if config was created or updated.
    """
    mcp_file = Path(working_dir) / ".mcp.json"

    # The MCP server config to add
    mcp_server_config = {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/gneyal/p_69_branch_monkey_mcp.git", "branch-monkey-mcp"],
        "env": {
            "BRANCH_MONKEY_API_URL": cloud_url
        }
    }

    try:
        if mcp_file.exists():
            # Read existing config
            with open(mcp_file, "r") as f:
                config = json.load(f)

            # Ensure mcpServers exists
            if "mcpServers" not in config:
                config["mcpServers"] = {}

            # Check if already configured
            if "kompany-cloud" in config["mcpServers"]:
                print(f"[MCP] Config already exists in {mcp_file}")
                return False

            # Add our config
            config["mcpServers"]["kompany-cloud"] = mcp_server_config

            with open(mcp_file, "w") as f:
                json.dump(config, f, indent=2)

            print(f"[MCP] Added kompany-cloud to {mcp_file}")
            return True
        else:
            # Create new config
            config = {
                "mcpServers": {
                    "kompany-cloud": mcp_server_config
                }
            }

            with open(mcp_file, "w") as f:
                json.dump(config, f, indent=2)

            print(f"[MCP] Created {mcp_file} with kompany-cloud config")
            return True

    except Exception as e:
        print(f"[MCP] Warning: Could not set up MCP config: {e}")
        return False


def _run_with_tui(args, home_dir, current_project, onboarding_needed=False):
    """Run the relay with terminal UI."""
    import threading
    from .relay_tui import RelayTUI

    tui = RelayTUI()

    # Detect installed CLI providers early (needed by callbacks)
    persistent_cfg = load_persistent_config()
    try:
        from .bridge_and_local_actions.cli_providers import get_available_providers, get_default_cli
        cli_providers = get_available_providers()
        default_cli = get_default_cli()
        installed_clis = [n for n, p in cli_providers.items() if p.get("installed")]
        # Show CLI selection on first run (after onboarding/launchd) if not yet configured
        _cli_prompt_needed = (
            "default_cli" not in persistent_cfg
            and len(installed_clis) >= 1
        )
    except Exception:
        cli_providers = {}
        default_cli = "claude"
        _cli_prompt_needed = False

    # Callback when user sets home dir during onboarding or [H] edit
    def on_home_set(path):
        save_persistent_config({"home_dir": path})
        try:
            from .bridge_and_local_actions import set_home_directory
            set_home_directory(path)
        except Exception:
            pass

    tui._on_home_set = on_home_set

    # Callback when user renames the machine
    def on_name_set(name):
        relay_ref[0].machine_name = name
        # Update Cerver registration label
        if relay_ref[0]._cerver_client:
            relay_ref[0]._cerver_client.machine_name = name
        # Persist so it survives restarts
        save_persistent_config({"machine_name": name})
        print(f"[Relay] Machine name set to: {name}")

    tui._on_name_set = on_name_set

    # Callback when user toggles launchd service (install/uninstall)
    def on_launchd_toggle(do_install):
        import subprocess
        if do_install:
            home = tui.state.get("home_dir")
            if install_launchd_service(home):
                tui.update(launchd="running")
                print("[Relay] Launchd service installed.")
            else:
                tui.update(launchd="error")
                print("[Relay] Failed to install launchd service.")
        else:
            # Uninstall
            if LAUNCHD_PLIST_PATH.exists():
                subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST_PATH)], capture_output=True)
                LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
            tui.update(launchd="not_installed")
            print("[Relay] Launchd service removed.")
        tui.update(launchd_prompt="done")
        # Show CLI selection if needed (first run with multiple CLIs)
        if _cli_prompt_needed and tui.state.get("cli_prompt") is None:
            tui.update(cli_prompt="pending")

    tui._on_launchd_install = on_launchd_toggle

    # Callback when user logs out
    def on_logout():
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        print("[Relay] Logged out. Token cleared.")

    tui._on_logout = on_logout

    # Callback when user selects a CLI provider
    def on_cli_set(cli_name):
        try:
            from .bridge_and_local_actions.cli_providers import set_default_cli
            set_default_cli(cli_name)
            print(f"[Relay] Default CLI set to: {cli_name}")
        except Exception as e:
            print(f"[Relay] Failed to set CLI: {e}")

    tui._on_cli_set = on_cli_set

    # Callback when user sets an API key for a provider
    def on_cli_api_key(provider_name, api_key):
        try:
            from .bridge_and_local_actions.cli_providers import get_provider, get_available_providers
            provider = get_provider(provider_name)
            provider.set_api_key(api_key)
            print(f"[Relay] API key set for {provider.display_name}")
            # Refresh providers to update auth status in TUI
            tui.update(cli_providers=get_available_providers())
        except Exception as e:
            print(f"[Relay] Failed to set API key: {e}")

    tui._on_cli_api_key = on_cli_api_key

    # Callback when user starts device auth for a provider
    # Runs in a background thread to avoid freezing the TUI
    def on_cli_device_auth(provider_name):
        import threading

        def _run():
            try:
                from .bridge_and_local_actions.cli_providers import get_provider, get_available_providers
                provider = get_provider(provider_name)
                result = provider.start_device_auth()
                if result:
                    result.pop("_process", None)
                    tui._cli_device_auth = result
                    tui._cli_auth_mode = "device_auth"
                else:
                    print(f"[Relay] Device auth not available for {provider_name}")
                    tui._cli_auth_mode = None
            except Exception as e:
                print(f"[Relay] Failed to start device auth: {e}")
                tui._cli_auth_mode = None

        # Show "connecting..." immediately, result arrives async
        tui._cli_device_auth = {"type": "browser", "message": "Starting sign-in..."}
        tui._cli_auth_mode = "device_auth"
        threading.Thread(target=_run, daemon=True).start()
        return True  # Signal that auth was started (TUI switches to device_auth view)

    tui._on_cli_device_auth = on_cli_device_auth

    # Callback to install a CLI provider (runs in background thread)
    def on_cli_install(provider_name):
        import threading

        def _run():
            try:
                from .bridge_and_local_actions.cli_providers import get_provider, get_available_providers
                provider = get_provider(provider_name)
                tui._cli_installing = provider_name
                print(f"[Relay] Installing {provider.display_name}...")
                result = provider.install()
                tui._cli_installing = None
                if result["success"]:
                    print(f"[Relay] {provider.display_name} installed successfully")
                else:
                    print(f"[Relay] Install failed: {result['output']}")
                # Refresh providers
                tui.update(cli_providers=get_available_providers())
            except Exception as e:
                tui._cli_installing = None
                print(f"[Relay] Install failed: {e}")

        threading.Thread(target=_run, daemon=True).start()

    tui._on_cli_install = on_cli_install

    # Callback to refresh CLI provider status (after auth changes)
    def on_cli_refresh():
        try:
            from .bridge_and_local_actions.cli_providers import get_available_providers
            tui.update(cli_providers=get_available_providers())
        except Exception:
            pass

    tui._on_cli_refresh = on_cli_refresh

    # Detect current launchd status
    if sys.platform == "darwin":
        ld_status = check_launchd_status()
        if ld_status["running"]:
            launchd_state = "running"
        elif ld_status["installed"]:
            launchd_state = "installed"
        else:
            launchd_state = "not_installed"
    else:
        launchd_state = None

    # Pre-populate user/org info from cached token
    cached_user_email = None
    cached_org_name = None
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                cached = json.load(f)
                cached_user_email = cached.get("user_email")
                cached_org_name = cached.get("org_name")
        except Exception:
            pass

    # Snapshot of the provision probe for the Provision tab.
    _agent_env_snapshot: Dict[str, Any] = {}
    try:
        from .computer_runtime import agent_environment as _agent_env_mod
        if _agent_env_mod.current:
            _agent_env_snapshot = {
                "binaries": dict(_agent_env_mod.current.binaries),
                "missing_required": list(_agent_env_mod.current.missing_required),
                "shell_path_captured": _agent_env_mod.current.shell_path_captured,
            }
    except Exception:
        pass

    tui.update(
        version=VERSION,
        # Short SHA so "did my restart actually pick up the fix?" is a
        # one-glance check instead of grepping ps + uv cache contents.
        commit_sha=(CURRENT_COMMIT_SHA or "")[:7],
        agent_env=_agent_env_snapshot,
        machine_name=args.name or socket.gethostname(),
        home_dir=home_dir,
        project=os.path.basename(current_project) if current_project else None,
        project_path=current_project,
        port=args.port,
        dashboard_url=f"http://localhost:{args.port}/",
        cloud_url=args.cloud_url,
        user_email=cached_user_email,
        org_name=cached_org_name,
        onboarding_needed=onboarding_needed,
        launchd=launchd_state,
        cli_providers=cli_providers,
        default_cli=default_cli,
        cerver_only=args.cerver_only,
    )
    tui.install_capture()

    # Start local server
    if not args.no_server:
        start_server_in_background(
            port=args.port,
            home_dir=home_dir,
            working_dir=current_project,
        )
        tui.update(server_running=is_port_in_use(args.port))

    # Start relay in background thread
    relay_ref = [None]

    def run_relay():
        client = RelayClient(
            cloud_url=args.cloud_url,
            local_port=args.port,
            machine_name=args.name,
            tui=tui,
            cerver_url=args.cerver_url,
            cerver_owner_id=args.cerver_owner_id,
            cerver_api_token=args.cerver_api_token,
        )
        relay_ref[0] = client
        try:
            if args.cerver_only:
                asyncio.run(client.connect_cerver_only())
            else:
                asyncio.run(client.connect())
        except Exception as e:
            print(f"[Relay] Error: {e}")

    relay_thread = threading.Thread(target=run_relay, daemon=True)
    relay_thread.start()

    # TUI runs in main thread (blocks until quit)
    tui.run(stop_callback=lambda: relay_ref[0] and relay_ref[0].stop())


LAUNCHD_LABEL = "dev.kompany.relay"
LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def check_launchd_status() -> dict:
    """Check launchd service status. Returns dict with installed, running, pid."""
    import subprocess

    if sys.platform != "darwin":
        return {"installed": False, "running": False, "pid": None}

    if not LAUNCHD_PLIST_PATH.exists():
        return {"installed": False, "running": False, "pid": None}

    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"installed": True, "running": False, "pid": None}

    # Parse output — launchctl list <label> outputs lines like: "PID" = 1234;
    pid = None
    for line in result.stdout.strip().splitlines():
        cols = line.split()
        if len(cols) >= 1 and cols[-1] == LAUNCHD_LABEL:
            pid = cols[0] if cols[0] != "-" else None
            break

    return {"installed": True, "running": pid is not None, "pid": pid}


def install_launchd_service(home_dir: str = None) -> bool:
    """Install the relay as a launchd service. Returns True on success."""
    import shutil
    import subprocess

    if sys.platform != "darwin":
        return False

    binary = shutil.which("branch-monkey-relay")
    if not binary:
        return False

    # Build ProgramArguments — always --no-tui since launchd has no TTY
    program_args = [binary, "--no-tui"]

    if not home_dir:
        persistent_cfg = load_persistent_config()
        home_dir = persistent_cfg.get("home_dir")
    if home_dir:
        program_args.extend(["--dir", home_dir])

    # Build the plist XML
    args_xml = "\n".join(f"        <string>{a}</string>" for a in program_args)
    current_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{home_dir or str(Path.home() / "Code")}</string>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{CONFIG_DIR / "relay.log"}</string>
    <key>StandardErrorPath</key>
    <string>{CONFIG_DIR / "relay.err.log"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{current_path}</string>
    </dict>
</dict>
</plist>
"""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Unload first if already loaded (ignore errors)
    if LAUNCHD_PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
            capture_output=True,
        )

    LAUNCHD_PLIST_PATH.write_text(plist_content)

    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST_PATH)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _launchd_install():
    """CLI handler for 'branch-monkey-relay install'."""
    import shutil

    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    binary = shutil.which("branch-monkey-relay")
    if not binary:
        print("Error: 'branch-monkey-relay' not found in PATH.")
        print("Make sure it's installed (pip install -e . or pip install branch-monkey-mcp).")
        sys.exit(1)

    persistent_cfg = load_persistent_config()
    home_dir = persistent_cfg.get("home_dir")

    if install_launchd_service(home_dir):
        print(f"Service '{LAUNCHD_LABEL}' installed and started.")
        print(f"  Plist: {LAUNCHD_PLIST_PATH}")
        print(f"  Logs:  {CONFIG_DIR / 'relay.log'}")
        print(f"  Errors: {CONFIG_DIR / 'relay.err.log'}")
        print()
        print("The relay will auto-start on login and restart if it crashes.")
        print("Use 'branch-monkey-relay uninstall' to remove the service.")
    else:
        print("Error: Failed to install launchd service.")
        sys.exit(1)


def _launchd_uninstall():
    """Uninstall the branch-monkey-relay launchd service."""
    import subprocess

    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    if not LAUNCHD_PLIST_PATH.exists():
        print(f"Service not installed (no plist at {LAUNCHD_PLIST_PATH}).")
        return

    # Unload the service
    result = subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: launchctl unload returned: {result.stderr.strip()}")

    # Remove the plist
    LAUNCHD_PLIST_PATH.unlink()
    print(f"Service '{LAUNCHD_LABEL}' uninstalled.")
    print(f"Removed {LAUNCHD_PLIST_PATH}")


def _launchd_status():
    """CLI handler for 'branch-monkey-relay status'."""
    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    status = check_launchd_status()
    if not status["installed"]:
        print(f"Service not installed (no plist at {LAUNCHD_PLIST_PATH}).")
        return

    print(f"Service '{LAUNCHD_LABEL}':")
    print(f"  Plist:  {LAUNCHD_PLIST_PATH}")
    if status["running"]:
        print(f"  PID:    {status['pid']}")
        print(f"  Status: running")
    else:
        print(f"  Status: installed but not running")

    log_path = CONFIG_DIR / "relay.log"
    if log_path.exists():
        print(f"  Log:    {log_path}")


def main():
    """CLI entry point for branch-monkey-relay."""
    # Ensure output is not buffered (for background processes)
    sys.stdout.reconfigure(line_buffering=True)

    # Tee stdout/stderr into an in-memory ring buffer so the log-tail
    # endpoint works regardless of how the relay was started (uvx in a
    # Terminal, launchd, nohup, etc). Operator still sees live output
    # in the original stream — this just adds remote readability.
    try:
        from . import log_buffer
        log_buffer.install()
    except Exception as _exc:  # never block startup on diagnostics
        print(f"[relay] log_buffer install failed (non-fatal): {_exc}")

    # Handle subcommands before argparse
    if len(sys.argv) > 1 and sys.argv[1] in ("install", "uninstall", "status"):
        cmd = sys.argv[1]
        if cmd == "install":
            _launchd_install()
        elif cmd == "uninstall":
            _launchd_uninstall()
        elif cmd == "status":
            _launchd_status()
        return

    import argparse

    # Resolve cloud URL dynamically from /api/config
    resolved_cloud_url = fetch_cloud_url_from_config(FALLBACK_CLOUD_URL)

    parser = argparse.ArgumentParser(
        description="Connect your machine to Kompany Cloud"
    )
    parser.add_argument(
        "--cloud-url",
        default=os.environ.get("BRANCH_MONKEY_CLOUD_URL", resolved_cloud_url),
        help=f"Cloud URL (default: auto-detected from config)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BRANCH_MONKEY_LOCAL_PORT", "18081")),
        help="Local server port (default: 18081)"
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Machine name (default: hostname)"
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Skip starting local server (use if server is running separately)"
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip setting up MCP config in .mcp.json"
    )
    parser.add_argument(
        "--dir", "-d",
        default=os.getcwd(),
        help="Working directory for agent execution (default: current directory)"
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable terminal UI, show raw logs instead"
    )
    parser.add_argument(
        "--cli",
        default=None,
        choices=["claude", "codex", "grok"],
        help="Set default AI CLI provider (claude, codex, or grok)"
    )
    parser.add_argument(
        "--cerver-url",
        default=os.environ.get("CERVER_GATEWAY_URL"),
        help="Cerver base URL for compute registration"
    )
    parser.add_argument(
        "--cerver-owner-id",
        default=os.environ.get("CERVER_OWNER_ID"),
        help="Legacy fallback owner id for Cerver registration"
    )
    parser.add_argument(
        "--cerver-api-token",
        default=os.environ.get("CERVER_API_TOKEN"),
        help="Optional API token for talking to Cerver"
    )
    parser.add_argument(
        "--cerver-only",
        action="store_true",
        help="Register this machine with Cerver without connecting to Kompany Cloud"
    )

    args = parser.parse_args()

    # Probe where this machine's agent binaries actually live. Runs once
    # at startup, persists to ~/.cerver/agent_env.json, and from this
    # point on every spawned subprocess uses the resolved canonical PATH
    # via cli_runtime.build_process_env. Surfaced in the Provision tab
    # so the user can see what was discovered.
    try:
        from .computer_runtime import agent_environment as _agent_env
        _probed = _agent_env.probe()
        _agent_env.set_current(_probed)
        if _probed.missing_required:
            print(
                f"[Relay] Provision warning — missing required binaries: "
                f"{', '.join(_probed.missing_required)}. Agent spawn may fail."
            )
        else:
            found = sorted(_probed.binaries.keys())
            print(f"[Relay] Provision: discovered {len(found)} binaries ({', '.join(found)})")
    except Exception as _probe_exc:
        print(f"[Relay] Provision probe failed: {_probe_exc}. Continuing with inherited PATH.")

    # Handle --cli flag: persist default CLI choice
    if args.cli:
        try:
            from .bridge_and_local_actions.cli_providers import set_default_cli
            set_default_cli(args.cli)
        except Exception as e:
            print(f"[Relay] Warning: Could not set CLI preference: {e}")

    # Check for working directory in this order:
    # 1. --dir flag (if explicitly provided, not default)
    # 2. BRANCH_MONKEY_WORKING_DIR environment variable
    # 3. Current directory (default)
    env_working_dir = os.environ.get("BRANCH_MONKEY_WORKING_DIR")
    dir_explicitly_set = args.dir != os.getcwd()

    if dir_explicitly_set:
        # --dir was explicitly provided
        working_dir = os.path.abspath(args.dir)
    elif env_working_dir:
        # Use environment variable
        working_dir = os.path.abspath(os.path.expanduser(env_working_dir))
        print(f"[Relay] Using working directory from BRANCH_MONKEY_WORKING_DIR: {working_dir}")
    else:
        # Use current directory
        working_dir = os.getcwd()

    # Validate the directory exists
    if not os.path.isdir(working_dir):
        print(f"[Relay] Error: Directory does not exist: {working_dir}")
        sys.exit(1)

    # Set up Kompany-specific MCP config unless disabled or running in Cerver-only mode
    if not args.no_mcp and not args.cerver_only:
        setup_mcp_config(working_dir, args.cloud_url)

    # Install Kompany skills only for the Kompany-connected flow
    if not args.cerver_only:
        install_skills()

    # Determine home directory (parent of projects) vs current project
    # Home is typically the Code folder, project is a subfolder
    home_dir = working_dir
    current_project = None

    # Check if working_dir looks like a project (has .git, package.json, etc.)
    project_markers = ['.git', 'package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod', 'pom.xml']
    is_project = any(os.path.exists(os.path.join(working_dir, marker)) for marker in project_markers)

    if is_project:
        # working_dir is a project, home is its parent
        current_project = working_dir
        home_dir = os.path.dirname(working_dir)

    # Load persistent config (saved home_dir from onboarding)
    persistent_cfg = load_persistent_config()
    onboarding_needed = "home_dir" not in persistent_cfg

    # Use saved home_dir if available and no explicit --dir was set
    if not dir_explicitly_set and not env_working_dir:
        saved_home = persistent_cfg.get("home_dir")
        if saved_home and os.path.isdir(saved_home):
            home_dir = saved_home

    # Terminal UI mode (default when running in a terminal)
    use_tui = not args.no_tui and sys.stdout.isatty()
    if use_tui:
        try:
            from .relay_tui import RelayTUI  # noqa: F401
            _run_with_tui(args, home_dir, current_project, onboarding_needed=onboarding_needed)
            return
        except ImportError:
            pass  # Fall through to raw logs

    # Load cached account info for display
    cached_user_email = None
    cached_org_name = None
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                cached = json.load(f)
                cached_user_email = cached.get("user_email")
                cached_org_name = cached.get("org_name")
        except Exception:
            pass

    print(f"")
    if args.cerver_only:
        print(f"\033[1mCerver Local Compute\033[0m v{VERSION}")
        print(f"")
        print(f"  \033[38;2;107;114;128mThis registers your machine with Cerver so it can appear\033[0m")
        print(f"  \033[38;2;107;114;128mas private compute in apps like Kompany.\033[0m")
    else:
        print(f"\033[1mKompany Relay\033[0m v{VERSION}")
        print(f"")
        print(f"  \033[38;2;107;114;128mThis connects your machine to kompany.dev so you can\033[0m")
        print(f"  \033[38;2;107;114;128mrun AI agents on your local codebase from the cloud.\033[0m")
    print(f"")
    if cached_user_email and not args.cerver_only:
        print(f"  User:      \033[1m{cached_user_email}\033[0m")
    if cached_org_name and not args.cerver_only:
        print(f"  Org:       \033[1m{cached_org_name}\033[0m")
    print(f"  Home:      \033[1m{home_dir}\033[0m")
    if current_project:
        project_name = os.path.basename(current_project)
        print(f"  Project:   \033[1m{project_name}\033[0m \033[38;2;107;114;128m({current_project})\033[0m")
    else:
        print(f"  Project:   \033[38;2;107;114;128m(none selected - pick one in dashboard)\033[0m")

    # Show detected CLI providers
    try:
        from .bridge_and_local_actions.cli_providers import get_available_providers, get_default_cli
        _cli_providers = get_available_providers()
        _default_cli = get_default_cli()
        first_line = True
        for name, info in _cli_providers.items():
            if info.get("installed"):
                suffix = " (default)" if name == _default_cli else " (installed)"
                label = "  AI CLI:    " if first_line else "             "
                print(f"{label}\033[1m{info['display_name']}\033[0m\033[38;2;107;114;128m{suffix}\033[0m")
                first_line = False
        if first_line:
            print(f"  AI CLI:    \033[38;2;107;114;128m(none detected)\033[0m")
    except Exception:
        pass

    print(f"  Dashboard: \033[1mhttp://localhost:{args.port}/\033[0m")
    if args.cerver_only:
        target = args.cerver_url or os.environ.get("CERVER_GATEWAY_URL") or "https://gateway.cerver.ai"
        print(f"  Cerver:    \033[1m{target}\033[0m")
        if args.cerver_owner_id or os.environ.get("CERVER_OWNER_ID"):
            owner_hint = args.cerver_owner_id or os.environ.get("CERVER_OWNER_ID")
            print(f"  Owner:     \033[1m{owner_hint}\033[0m \033[38;2;107;114;128m(legacy fallback)\033[0m")
        else:
            print(f"  Login:     \033[38;2;107;114;128mBrowser login on first registration\033[0m")
    print(f"")

    # Start local agent server unless --no-server is specified
    if not args.no_server:
        print(f"\033[38;2;107;114;128mStarting local server...\033[0m")
        start_server_in_background(port=args.port, home_dir=home_dir, working_dir=current_project)
        time.sleep(1)
    else:
        print(f"\033[38;2;107;114;128mSkipping local server (--no-server)\033[0m")

    if args.cerver_only:
        target = args.cerver_url or os.environ.get("CERVER_GATEWAY_URL") or "https://gateway.cerver.ai"
        print(f"\033[38;2;107;114;128mRegistering local compute with {target}...\033[0m")
        run_cerver_compute_client(
            cerver_url=args.cerver_url,
            cerver_owner_id=args.cerver_owner_id,
            local_port=args.port,
            machine_name=args.name,
            cerver_api_token=args.cerver_api_token,
        )
    else:
        print(f"\033[38;2;107;114;128mConnecting to {args.cloud_url}...\033[0m")
        run_relay_client(
            cloud_url=args.cloud_url,
            local_port=args.port,
            machine_name=args.name,
            cerver_url=args.cerver_url,
            cerver_owner_id=args.cerver_owner_id,
            cerver_api_token=args.cerver_api_token,
        )


if __name__ == "__main__":
    main()
