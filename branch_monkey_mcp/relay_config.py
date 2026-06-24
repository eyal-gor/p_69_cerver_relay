"""Relay configuration: file paths, cloud URLs, version metadata, and the
helpers that read and write the persistent relay config.

Split out of :mod:`relay_client` so the configuration concern — where the
relay stores kompany account state (``~/.kompany``) and cerver runtime
secrets (``~/.cerver``), and how it resolves its version and cloud URL —
lives in one small module instead of being threaded through the client.
``relay_client`` re-imports every name defined here, so existing references
keep working unchanged.
"""
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

# Version — number of commits in the relay repo. Bakes at wheel build time
# via hatch_build.VersionWriter (reads from branch_monkey_mcp/_version.py).
# Falls back to a runtime `git rev-list` when developing from a working tree
# without going through the build (pip install -e editable, source checkout).
def _compute_version() -> str:
    """Resolve the relay version string (the repo's commit count).

    Prefers the baked-in ``COMMIT_COUNT`` written into ``_version.py`` at
    wheel build time. When running from a working tree that skipped the
    build (editable install, source checkout), falls back to a runtime
    ``git rev-list --count HEAD``.

    Returns:
        The commit count as a string, or ``"0"`` if neither source is
        available.
    """
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
# install.sh writes the user's account token here as CERVER_API_KEY.
# Launchd's plist doesn't source env files, so the relay has to read it
# directly — otherwise `--cerver-only` starts a fresh device-auth flow
# (pops /approve?code=… in the browser) on every respawn.
CERVER_ACCOUNT_ENV = CERVER_DIR / "cerver.env"


def _load_cerver_api_token() -> Optional[str]:
    """Read the cerver account token from ~/.cerver/cerver.env.

    Accepts both CERVER_API_TOKEN (historical name the relay reads from
    os.environ) and CERVER_API_KEY (what install.sh writes today). Returns
    None if the file is absent, unreadable, or contains neither key.
    """
    if not CERVER_ACCOUNT_ENV.exists():
        return None
    try:
        for line in CERVER_ACCOUNT_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("CERVER_API_TOKEN", "CERVER_API_KEY"):
                val = v.strip().strip('"').strip("'")
                if val:
                    return val
    except Exception as e:
        print(f"[Relay] Warning: failed to read {CERVER_ACCOUNT_ENV}: {e}")
    return None

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
