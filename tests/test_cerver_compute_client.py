"""
Unit tests for the Cerver compute registration client.

`CerverComputeClient` is the relay's reliability surface for keeping a local
machine connected to the Cerver gateway as a private compute: it authenticates,
registers, heartbeats, and reconnects across restarts. The reliability-critical
behaviours covered here are:

  - State persistence & reconnect: auth token + per-identity ``compute_id`` are
    written to / restored from ``~/.kompany/cerver_compute.json`` so a restart
    reuses the same compute (the identity key keeps distinct configs separate).
  - Auth retry on 401: a stale saved token is cleared and the call retried once
    after a fresh login, rather than hard-failing the connection.
  - Lazy register-on-heartbeat: a heartbeat with no compute_id registers first.
  - Best-effort unregister: a dead gateway never blocks shutdown.

All tests are offline. ``httpx.AsyncClient`` is monkeypatched with a scripted
fake that returns real ``httpx.Response`` objects, so ``raise_for_status`` and
status-code branching exercise the genuine httpx behaviour without a network.

Run with: uv run --with pytest python -m pytest tests/test_cerver_compute_client.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

# Make the package importable when running from repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from branch_monkey_mcp.cerver_compute import client as client_mod  # noqa: E402
from branch_monkey_mcp.cerver_compute.client import CerverComputeClient  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles: a scripted async HTTP client returning real httpx.Response objs
# ---------------------------------------------------------------------------


class FakeAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` that replays scripted responses.

    A single shared ``script`` (list of ``httpx.Response``) is consumed in order
    across every instance, and each request is recorded on ``calls`` so tests can
    assert on method, url, headers, and body.
    """

    script = []
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _next(self, method, url, headers, json_body, params):
        FakeAsyncClient.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": json_body,
                "params": params,
            }
        )
        if not FakeAsyncClient.script:
            raise AssertionError(f"unexpected {method} {url}: script exhausted")
        response = FakeAsyncClient.script.pop(0)
        # Attach a request so .raise_for_status() can build an HTTPStatusError.
        response.request = httpx.Request(method, url)
        return response

    async def post(self, url, headers=None, json=None, params=None):
        return self._next("POST", url, headers, json, params)

    async def get(self, url, headers=None, json=None, params=None):
        return self._next("GET", url, headers, json, params)

    async def delete(self, url, headers=None, json=None, params=None):
        return self._next("DELETE", url, headers, json, params)


def _resp(status_code, payload=None):
    """Build a real httpx.Response with a JSON body."""
    return httpx.Response(status_code, json=payload if payload is not None else {})


@pytest.fixture
def fake_http(monkeypatch):
    """Install the scripted fake AsyncClient and reset its shared state."""
    FakeAsyncClient.script = []
    FakeAsyncClient.calls = []
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    """Point the persisted-state file at a temp dir and clear cerver env vars.

    Yields the state-file Path so tests can read what the client wrote.
    """
    state_file = tmp_path / "cerver_compute.json"
    monkeypatch.setattr(client_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(client_mod, "CERVER_COMPUTE_STATE_FILE", state_file)
    for var in ("CERVER_GATEWAY_URL", "CERVER_OWNER_ID", "CERVER_API_TOKEN", "P69_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return state_file


def _make_client(**overrides):
    """Construct a client with sensible test defaults."""
    kwargs = dict(
        cerver_url="https://gw.example",
        owner_id="owner-1",
        local_port=9999,
        machine_name="test-machine",
    )
    kwargs.update(overrides)
    return CerverComputeClient(**kwargs)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helpers: URL normalization, keys, headers, identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://gw.example/", "https://gw.example"),
        ("  https://gw.example  ", "https://gw.example"),
        ("https://gw.example///", "https://gw.example"),  # all trailing slashes stripped
        ("https://gw.example", "https://gw.example"),
        ("   ", ""),  # whitespace-only collapses to empty
    ],
)
def test_normalize_base_url(raw, expected):
    assert client_mod._normalize_base_url(raw) == expected


def test_enabled_reflects_configured_url(tmp_state):
    assert _make_client().enabled is True
    # A whitespace-only url is truthy (so it skips the env/default fallback) but
    # normalizes to "" -> the client reports itself as not-enabled.
    assert _make_client(cerver_url="   ").enabled is False
    # A None url falls back to the default gateway, so the client is enabled.
    assert _make_client(cerver_url=None).enabled is True


def test_headers_include_bearer_only_when_token_present(tmp_state):
    c = _make_client(api_token=None)
    assert c._headers() == {"Content-Type": "application/json"}
    c.api_token = "tok-123"
    assert c._headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer tok-123",
    }


def test_identity_key_distinguishes_port_and_owner(tmp_state):
    a = _make_client(local_port=1111)
    b = _make_client(local_port=2222)
    assert a._identity_key() != b._identity_key()
    assert a._identity_key().endswith("|cerver_local_provider|1111")

    anon = _make_client(owner_id=None)
    anon.user_id = None
    assert "|anonymous|" in anon._identity_key()


def test_build_connection_includes_local_api_token_from_env(tmp_state, monkeypatch):
    c = _make_client()
    conn = c._build_connection()
    assert conn["transport"] == "cerver_connect"
    assert conn["connect_id"].startswith("comp_")
    assert "api_token" not in conn

    monkeypatch.setenv("P69_API_TOKEN", "local-secret")
    assert c._build_connection()["api_token"] == "local-secret"


def test_ensure_compute_id_is_stable_and_persisted(tmp_state):
    c = _make_client()
    first = c.ensure_compute_id()
    assert first.startswith("comp_")
    assert c.ensure_compute_id() == first  # stable across calls

    saved = json.loads(tmp_state.read_text())
    assert saved[c._identity_key()] == first


# ---------------------------------------------------------------------------
# State persistence & reconnect-across-restart
# ---------------------------------------------------------------------------


def test_load_cerver_state_tolerates_missing_and_corrupt_file(tmp_state):
    assert client_mod._load_cerver_state() == {}  # missing
    tmp_state.write_text("{not valid json")
    assert client_mod._load_cerver_state() == {}  # corrupt -> {}


def test_persisted_identity_is_restored_by_a_fresh_client(tmp_state):
    first = _make_client()
    compute_id = first.ensure_compute_id()

    # A brand-new client for the same identity reconnects to the same compute.
    second = _make_client()
    assert second.compute_id == compute_id


def test_persisted_auth_is_restored_and_clearable(tmp_state):
    c = _make_client(api_token="initial-tok")
    c.user_id = "user-9"
    c._persist_auth()

    restored = _make_client(api_token=None)
    assert restored.api_token == "initial-tok"
    assert restored.user_id == "user-9"

    restored._clear_persisted_auth()
    assert restored.api_token is None
    assert restored.user_id is None
    # And the next fresh client no longer sees a token.
    assert _make_client(api_token=None).api_token is None


# ---------------------------------------------------------------------------
# Auth retry on 401 (the reliability behaviour for stale tokens)
# ---------------------------------------------------------------------------


def test_post_with_auth_retry_succeeds_first_try(tmp_state, fake_http):
    fake_http.script = [_resp(200, {"ok": True})]
    c = _make_client(api_token="tok")
    out = _run(c._post_with_auth_retry("/v2/x", {"a": 1}))
    assert out == {"ok": True}
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_post_with_auth_retry_reauths_once_on_401(tmp_state, fake_http, monkeypatch):
    fake_http.script = [_resp(401, {"error": "expired"}), _resp(200, {"ok": True})]
    c = _make_client(api_token="stale")

    reauth_calls = {"n": 0}

    async def fake_ensure():
        reauth_calls["n"] += 1
        c.api_token = "fresh"

    monkeypatch.setattr(c, "ensure_authenticated", fake_ensure)

    out = _run(c._post_with_auth_retry("/v2/x", {"a": 1}))
    assert out == {"ok": True}
    assert reauth_calls["n"] == 1
    # Second call carried the refreshed token.
    assert fake_http.calls[1]["headers"]["Authorization"] == "Bearer fresh"


def test_post_with_auth_retry_does_not_reauth_without_token(tmp_state, fake_http):
    fake_http.script = [_resp(401, {"error": "nope"})]
    c = _make_client(api_token=None)
    with pytest.raises(httpx.HTTPStatusError):
        _run(c._post_with_auth_retry("/v2/x", {"a": 1}))
    assert len(fake_http.calls) == 1  # no retry attempted


def test_post_with_auth_retry_raises_when_second_attempt_still_401(tmp_state, fake_http, monkeypatch):
    fake_http.script = [_resp(401), _resp(401)]
    c = _make_client(api_token="stale")
    monkeypatch.setattr(c, "ensure_authenticated", _noop_reauth(c))
    with pytest.raises(httpx.HTTPStatusError):
        _run(c._post_with_auth_retry("/v2/x", {"a": 1}))
    assert len(fake_http.calls) == 2


def _noop_reauth(c):
    async def fake_ensure():
        c.api_token = "fresh"

    return fake_ensure


# ---------------------------------------------------------------------------
# register / heartbeat / unregister
# ---------------------------------------------------------------------------


def test_register_adopts_server_ids_and_persists(tmp_state, fake_http):
    fake_http.script = [_resp(200, {"compute_id": "comp_server", "owner_id": "owner-server"})]
    c = _make_client(api_token="tok")

    payload = _run(c.register())
    assert payload["compute_id"] == "comp_server"
    assert c.compute_id == "comp_server"
    assert c.owner_id == "owner-server"  # server-resolved owner wins

    saved = json.loads(tmp_state.read_text())
    assert c.compute_id in saved.values()

    # Sanity check the request shape the gateway receives.
    body = fake_http.calls[0]["json"]
    assert body["kind"] == "local"
    assert body["label"] == "test-machine"
    assert body["connection"]["transport"] == "cerver_connect"
    assert body["compute_id"].startswith("comp_")


def test_register_requires_cerver_url(tmp_state, fake_http, monkeypatch):
    c = _make_client()
    monkeypatch.setattr(c, "cerver_url", "")  # force disabled
    with pytest.raises(RuntimeError, match="missing cerver_url"):
        _run(c.register())


def test_heartbeat_registers_first_when_no_compute_id(tmp_state, fake_http, monkeypatch):
    c = _make_client(api_token="tok")
    assert c.compute_id is None

    registered = {"n": 0}

    async def fake_register():
        registered["n"] += 1
        c.compute_id = "comp_after_register"

    monkeypatch.setattr(c, "register", fake_register)
    fake_http.script = [_resp(200, {"status": "online"})]

    out = _run(c.heartbeat())
    assert registered["n"] == 1
    assert out == {"status": "online"}
    # Heartbeat hit the freshly-registered compute's endpoint.
    assert "/v2/computes/comp_after_register/heartbeat" in fake_http.calls[0]["url"]


def test_heartbeat_raises_if_registration_yields_no_id(tmp_state, fake_http, monkeypatch):
    c = _make_client(api_token="tok")

    async def fake_register():
        return {}  # leaves compute_id unset

    monkeypatch.setattr(c, "register", fake_register)
    with pytest.raises(RuntimeError, match="did not return a compute_id"):
        _run(c.heartbeat())


def test_unregister_is_noop_without_compute_id(tmp_state, fake_http):
    c = _make_client(api_token="tok")
    _run(c.unregister())  # must not touch the network
    assert fake_http.calls == []


def test_unregister_swallows_gateway_errors(tmp_state, fake_http):
    fake_http.script = [_resp(500)]
    c = _make_client(api_token="tok")
    c.compute_id = "comp_x"
    # A 500 from the gateway must not raise out of shutdown.
    _run(c.unregister())
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["method"] == "DELETE"


def test_list_computes_reauths_on_401(tmp_state, fake_http, monkeypatch):
    fake_http.script = [_resp(401), _resp(200, {"computes": []})]
    c = _make_client(api_token="stale")
    monkeypatch.setattr(c, "ensure_authenticated", _noop_reauth(c))

    out = _run(c.list_computes())
    assert out == {"computes": []}
    assert len(fake_http.calls) == 2
    assert fake_http.calls[1]["headers"]["Authorization"] == "Bearer fresh"


def test_list_sessions_clamps_limit(tmp_state, fake_http):
    fake_http.script = [_resp(200, {"sessions": []})]
    c = _make_client(api_token="tok")
    _run(c.list_sessions(limit=999))
    # limit is clamped into [1, 50].
    assert fake_http.calls[0]["params"] == {"limit": "50"}
