"""
Tiny Infisical client for the relay — pulls project secrets and merges
them into spawned-CLI env vars.

Why baked into the relay (not a `infisical run` wrapper):
  - No extra CLI to install per machine
  - Updates flow via `uvx --refresh` like the rest of the relay code
  - Per-spawn fetch lets rotated keys reach the next agent without a
    relay restart

Config (env vars on the relay process):
  INFISICAL_TOKEN        Machine Identity universal-auth client secret OR
                         a legacy service token starting with `st.`
  INFISICAL_CLIENT_ID    Required when INFISICAL_TOKEN is a universal-auth
                         secret (paired login). Omit for `st.` tokens.
  INFISICAL_PROJECT_ID   UUID from the Infisical URL
  INFISICAL_ENV          dev | prod | staging   (default: dev)
  INFISICAL_BASE_URL     Override (default: https://app.infisical.com)

The fetch is best-effort — if Infisical is unreachable or the token is
invalid, the relay logs a warning and falls back to whatever's already in
the process env. Never crashes the relay.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Optional

try:
    import httpx
except ImportError:  # pragma: no cover — relay always installs httpx
    httpx = None  # type: ignore


# Cache lives at module scope so all spawn calls share it. Refreshed on a
# TTL — secrets that rotate in Infisical reach new agents within REFRESH_S.
REFRESH_S = 300  # 5 minutes
_cache: Dict[str, str] = {}
_cache_at: float = 0.0
_access_token: Optional[str] = None
_access_token_exp: float = 0.0
_lock = asyncio.Lock()


def _config() -> Optional[Dict[str, str]]:
    """Read Infisical config from env. Returns None if not configured."""
    token = os.environ.get("INFISICAL_TOKEN", "").strip()
    project = os.environ.get("INFISICAL_PROJECT_ID", "").strip()
    if not token or not project:
        return None
    return {
        "token": token,
        "client_id": os.environ.get("INFISICAL_CLIENT_ID", "").strip(),
        "project_id": project,
        "env": os.environ.get("INFISICAL_ENV", "dev").strip() or "dev",
        "base_url": (os.environ.get("INFISICAL_BASE_URL") or "https://app.infisical.com").rstrip("/"),
    }


async def _login(cfg: Dict[str, str]) -> Optional[str]:
    """Exchange the configured credentials for a short-lived access token.

    Two auth paths:
      - `st.` prefix → legacy Service Token, used directly as Bearer
      - Otherwise → Machine Identity Universal Auth (clientId + secret)
    """
    if cfg["token"].startswith("st."):
        return cfg["token"]
    if not cfg["client_id"]:
        print("[infisical] INFISICAL_CLIENT_ID required for universal-auth tokens; skipping fetch")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{cfg['base_url']}/api/v1/auth/universal-auth/login",
                json={"clientId": cfg["client_id"], "clientSecret": cfg["token"]},
            )
            if r.status_code != 200:
                print(f"[infisical] login failed: {r.status_code} {r.text[:200]}")
                return None
            data = r.json()
            return data.get("accessToken")
    except Exception as exc:
        print(f"[infisical] login error: {exc}")
        return None


async def _ensure_access_token(cfg: Dict[str, str]) -> Optional[str]:
    global _access_token, _access_token_exp
    if _access_token and time.time() < _access_token_exp - 30:
        return _access_token
    token = await _login(cfg)
    if token:
        _access_token = token
        # Universal auth tokens default to ~10min; service tokens don't expire.
        _access_token_exp = time.time() + (570 if not cfg["token"].startswith("st.") else 86400 * 365)
    return token


async def _fetch_secrets(cfg: Dict[str, str]) -> Dict[str, str]:
    access = await _ensure_access_token(cfg)
    if not access:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{cfg['base_url']}/api/v3/secrets/raw",
                params={"workspaceId": cfg["project_id"], "environment": cfg["env"]},
                headers={"Authorization": f"Bearer {access}"},
            )
            if r.status_code != 200:
                print(f"[infisical] fetch failed: {r.status_code} {r.text[:200]}")
                return {}
            data = r.json()
            secrets = data.get("secrets") or []
            return {
                s["secretKey"]: s.get("secretValue", "")
                for s in secrets
                if isinstance(s, dict) and s.get("secretKey")
            }
    except Exception as exc:
        print(f"[infisical] fetch error: {exc}")
        return {}


async def get_secrets(force: bool = False) -> Dict[str, str]:
    """Return a fresh-enough copy of the project's Infisical secrets.

    Cached for REFRESH_S to keep spawn paths fast. Pass `force=True` to
    bypass the cache (e.g. on demand from a debug command).
    """
    global _cache, _cache_at
    cfg = _config()
    if not cfg or httpx is None:
        return {}

    async with _lock:
        if not force and _cache and (time.time() - _cache_at) < REFRESH_S:
            return dict(_cache)
        fetched = await _fetch_secrets(cfg)
        if fetched:
            _cache = fetched
            _cache_at = time.time()
            print(f"[infisical] cached {len(_cache)} secret(s) from {cfg['env']}")
        return dict(_cache)


def get_secrets_sync() -> Dict[str, str]:
    """Sync wrapper for spawn paths that aren't async-ready.

    Returns the cache when warm. When the cache is EMPTY but Infisical
    is configured, does a blocking fetch instead of returning {} — a
    fresh sandbox relay can receive its first agent spawn within ~3s
    of boot, before the async refresh loop's initial fetch lands. On a
    user's Mac that race is masked by CLI OAuth; in a sandbox there is
    no OAuth, so an empty cache means the agent spawns keyless and
    dies with "Not logged in". Cold-start attempts are rate-limited so
    a vaultless project doesn't pay a fetch per spawn.
    """
    global _cache, _cache_at, _last_sync_attempt
    if _cache:
        return dict(_cache)
    cfg = _config()
    if not cfg or httpx is None:
        return {}
    now = time.time()
    if now - _last_sync_attempt < 30:
        return {}
    _last_sync_attempt = now
    fetched = _fetch_secrets_blocking(cfg)
    if fetched:
        _cache = fetched
        _cache_at = time.time()
        print(f"[infisical] cold-start sync fetch cached {len(_cache)} secret(s)")
    return dict(_cache)


_last_sync_attempt = 0.0


def _fetch_secrets_blocking(cfg: Dict[str, str]) -> Dict[str, str]:
    """Synchronous mirror of _login + _fetch_secrets for spawn paths.

    Uses httpx's sync client so it's safe to call from non-async code
    (and from threads with no running loop). 10s ceiling total.
    """
    try:
        with httpx.Client(timeout=10) as client:
            token = cfg["token"]
            if not token.startswith("st."):
                if not cfg["client_id"]:
                    return {}
                r = client.post(
                    f"{cfg['base_url']}/api/v1/auth/universal-auth/login",
                    json={"clientId": cfg["client_id"], "clientSecret": cfg["token"]},
                )
                if r.status_code != 200:
                    print(f"[infisical] sync login failed: {r.status_code} {r.text[:200]}")
                    return {}
                token = r.json().get("accessToken") or ""
                if not token:
                    return {}
            r = client.get(
                f"{cfg['base_url']}/api/v3/secrets/raw",
                params={"workspaceId": cfg["project_id"], "environment": cfg["env"]},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                print(f"[infisical] sync fetch failed: {r.status_code} {r.text[:200]}")
                return {}
            secrets = r.json().get("secrets") or []
            return {
                s["secretKey"]: s.get("secretValue", "")
                for s in secrets
                if isinstance(s, dict) and s.get("secretKey")
            }
    except Exception as exc:
        print(f"[infisical] sync fetch error: {exc}")
        return {}


def is_configured() -> bool:
    return _config() is not None
