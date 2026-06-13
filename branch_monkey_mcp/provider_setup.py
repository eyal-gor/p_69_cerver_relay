"""
Interactive setup for adding compute / AI provider credentials to the
user's Infisical project. Walks through each provider's required keys,
verifies them with a test API call, and pushes them to Infisical.

Cerver's position: your secrets stay in your Infisical, not ours. This
tool just removes the find-the-right-page → copy → paste → hope-it-works
friction. Same credentials, fewer steps, with verification in the loop.

Usage:
    cerver-add-provider           # interactive picker
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    print("httpx is required. Install with: pip install httpx")
    sys.exit(1)


# ─── Colors (matches install.sh palette) ─────────────────────────
BOLD   = "\033[1m"
GREEN  = "\033[38;2;34;197;94m"
ACCENT = "\033[38;2;99;102;241m"
ERROR  = "\033[38;2;239;68;68m"
MUTED  = "\033[38;2;107;114;128m"
NC     = "\033[0m"


# ─── Provider catalog ────────────────────────────────────────────
# Each provider declares: display name, tagline, where to grab the
# credentials, what fields are required, and a verify() callable that
# returns (ok, message). Verification is a cheap authenticated GET
# returning 200 on a valid key. Providers without a clean HTTP-auth
# verify (Modal, gRPC-only) get a skip-with-warning.

def _verify_anthropic(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": creds["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        timeout=10,
    )
    return (r.status_code == 200, f"HTTP {r.status_code}")


def _verify_openai(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {creds['OPENAI_API_KEY']}"},
        timeout=10,
    )
    return (r.status_code == 200, f"HTTP {r.status_code}")


def _verify_xai(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://api.x.ai/v1/models",
        headers={"Authorization": f"Bearer {creds['XAI_API_KEY']}"},
        timeout=10,
    )
    return (r.status_code == 200, f"HTTP {r.status_code}")


def _verify_google(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        headers={"Authorization": f"Bearer {creds['GEMINI_API_KEY']}"},
        timeout=10,
    )
    return (r.status_code == 200, f"HTTP {r.status_code}")


def _verify_vercel(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://api.vercel.com/v2/user",
        headers={"Authorization": f"Bearer {creds['VERCEL_TOKEN']}"},
        timeout=10,
    )
    return (r.status_code == 200, f"HTTP {r.status_code}")


def _verify_e2b(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://api.e2b.dev/sandboxes",
        headers={"X-API-KEY": creds["E2B_API_KEY"]},
        timeout=10,
    )
    # E2B returns 200 for empty list, 401 for bad key.
    return (r.status_code == 200, f"HTTP {r.status_code}")


def _verify_daytona(creds: Dict[str, str]) -> Tuple[bool, str]:
    r = httpx.get(
        "https://app.daytona.io/api/workspaces",
        headers={"Authorization": f"Bearer {creds['DAYTONA_API_KEY']}"},
        timeout=10,
    )
    return (r.status_code == 200, f"HTTP {r.status_code}")


# ─── Local model setup (Ollama) ──────────────────────────────────
# Unlike the hosted providers, Ollama isn't a key you paste into
# Infisical — it's a binary + model weights on THIS machine. So its
# "setup" installs the binary (with consent), makes sure the server is
# up, and pulls a model. Nothing leaves the box; nothing is pushed.

import platform
import shutil
import subprocess
import time


def _ollama_server_up(base: str = "http://localhost:11434") -> bool:
    try:
        r = httpx.get(f"{base}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _install_ollama_binary() -> bool:
    """Install the ollama binary, asking consent first. Returns True if
    it's available afterward."""
    if shutil.which("ollama"):
        return True

    system = platform.system()
    if system == "Darwin":
        cmd = (
            ["brew", "install", "ollama"]
            if shutil.which("brew")
            else ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
        )
    elif system == "Linux":
        cmd = ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
    else:
        print(f"  {ERROR}Automatic install isn't supported on {system}.{NC}")
        print(f"  {MUTED}Download it from {ACCENT}https://ollama.com/download{NC}{MUTED}, then re-run this.{NC}")
        return False

    print(f"  {MUTED}Will run:{NC} {ACCENT}{' '.join(cmd)}{NC}")
    if input(f"  {BOLD}Install Ollama now? [y/N]:{NC} ").strip().lower() != "y":
        print(f"  {MUTED}Skipped install.{NC}")
        return False

    print(f"  {MUTED}Installing Ollama…{NC}")
    try:
        # No capture — let the installer stream its own progress.
        result = subprocess.run(cmd, timeout=600)
    except Exception as e:
        print(f"  {ERROR}Install failed: {e}{NC}")
        return False
    if result.returncode != 0 or not shutil.which("ollama"):
        print(f"  {ERROR}Install did not complete. See output above.{NC}")
        return False
    print(f"  {GREEN}✓ ollama binary installed{NC}")
    return True


def _ensure_ollama_server() -> bool:
    """Make sure the Ollama server is listening. Start it detached if not."""
    if _ollama_server_up():
        return True
    if not shutil.which("ollama"):
        return False
    print(f"  {MUTED}Starting the Ollama server…{NC}")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"  {ERROR}Could not start `ollama serve`: {e}{NC}")
        return False
    # Give it a moment to bind the port.
    for _ in range(10):
        if _ollama_server_up():
            print(f"  {GREEN}✓ server up at localhost:11434{NC}")
            return True
        time.sleep(0.6)
    print(f"  {ERROR}Server didn't come up. Try `ollama serve` in another terminal.{NC}")
    return False


def _setup_ollama(cfg: Optional[Dict[str, str]] = None) -> bool:
    """Install Ollama (if needed), ensure it's running, and pull a model.

    `cfg` is accepted for signature parity with the credential flow but
    unused — Ollama is local, so nothing is pushed to Infisical.
    """
    print()
    print(f"{BOLD}─── Ollama (local open models) ─────────────{NC}")
    print(f"  {MUTED}Runs models on THIS machine. No API key, no per-token cost.{NC}")
    print(f"  {MUTED}cerver reaches it because the relay runs on the same box.{NC}")
    print()

    if not _install_ollama_binary():
        return True
    if not _ensure_ollama_server():
        return True

    # Pick a model to pull. Keep the suggestions small/fast — this is the
    # cheap tier. Users can pull anything from ollama.com/library later.
    print()
    print(f"  {MUTED}Popular small models:{NC}")
    print(f"    {ACCENT}llama3.2{NC}      {MUTED}— Meta, 3B, fast, great default{NC}")
    print(f"    {ACCENT}qwen2.5:7b{NC}    {MUTED}— strong general/coding{NC}")
    print(f"    {ACCENT}gemma2:2b{NC}     {MUTED}— tiny, laptop-friendly{NC}")
    print(f"    {ACCENT}mistral{NC}       {MUTED}— 7B, solid all-rounder{NC}")
    model = input(f"\n  {BOLD}Model to pull [llama3.2]:{NC} ").strip() or "llama3.2"

    print(f"\n  {MUTED}Pulling {model} (first pull downloads weights — can be GBs)…{NC}")
    try:
        result = subprocess.run(["ollama", "pull", model], timeout=3600)
    except Exception as e:
        print(f"  {ERROR}Pull failed: {e}{NC}")
        return True
    if result.returncode != 0:
        print(f"  {ERROR}Pull failed — check the model name at ollama.com/library{NC}")
        return True

    print()
    print(f"  {GREEN}{BOLD}✓ Ollama is ready with {model}.{NC}")
    print(f"  {MUTED}Run it:{NC} {ACCENT}cerver run --cli ollama --model {model} \"explain this repo\"{NC}")
    print(f"  {MUTED}Or route the cheap tier to it in a policy: {{ \"route\": {{ \"harness\": \"ollama\", \"model\": \"{model}\" }} }}{NC}")
    return True


PROVIDERS: Dict[str, Dict] = {
    # ─── Model providers — supply the LLM that writes the session ─
    "anthropic": {
        "kind": "model",
        "display_name": "Anthropic",
        "tagline": "Claude models",
        "credentials_url": "https://console.anthropic.com/settings/keys",
        "secrets": [
            {"key": "ANTHROPIC_API_KEY", "label": "API Key (sk-ant-...)", "hidden": True},
        ],
        "verify": _verify_anthropic,
    },
    "openai": {
        "kind": "model",
        "display_name": "OpenAI",
        "tagline": "GPT models, Codex CLI",
        "credentials_url": "https://platform.openai.com/api-keys",
        "secrets": [
            {"key": "OPENAI_API_KEY", "label": "API Key (sk-proj-...)", "hidden": True},
        ],
        "verify": _verify_openai,
    },
    "xai": {
        "kind": "model",
        "display_name": "xAI",
        "tagline": "Grok models",
        "credentials_url": "https://console.x.ai/team/default/api-keys",
        "secrets": [
            {"key": "XAI_API_KEY", "label": "API Key", "hidden": True},
        ],
        "verify": _verify_xai,
    },
    "gemma": {
        "kind": "model",
        "display_name": "Gemma (Google)",
        "tagline": "Gemma open models — free",
        "credentials_url": "https://aistudio.google.com/apikey",
        "secrets": [
            {"key": "GEMINI_API_KEY", "label": "API Key (AIza...)", "hidden": True},
        ],
        "verify": _verify_google,
    },

    # ─── Local models — run on THIS machine, free (no key, no Infisical) ─
    "ollama": {
        "kind": "local",
        "display_name": "Ollama",
        "tagline": "open models on this machine — free, no key",
        "credentials_url": "https://ollama.com",
        "secrets": [],
        "verify": None,
        "setup": _setup_ollama,
    },

    # ─── Compute providers — supply the sandbox the session runs in ─
    "vercel": {
        "kind": "compute",
        "display_name": "Vercel Sandbox",
        "tagline": "Cloud sandbox",
        "credentials_url": "https://vercel.com/account/tokens",
        "secrets": [
            {"key": "VERCEL_TOKEN",      "label": "Token", "hidden": True},
            {"key": "VERCEL_TEAM_ID",    "label": "Team ID (optional)", "hidden": False, "optional": True},
            {"key": "VERCEL_PROJECT_ID", "label": "Project ID (optional)", "hidden": False, "optional": True},
        ],
        "verify": _verify_vercel,
    },
    "e2b": {
        "kind": "compute",
        "display_name": "E2B",
        "tagline": "Cloud sandbox",
        "credentials_url": "https://e2b.dev/dashboard/keys",
        "secrets": [
            {"key": "E2B_API_KEY", "label": "API Key", "hidden": True},
        ],
        "verify": _verify_e2b,
    },
    "modal": {
        "kind": "compute",
        "display_name": "Modal",
        "tagline": "GPU + CPU compute",
        "credentials_url": "https://modal.com/settings/tokens",
        "secrets": [
            {"key": "MODAL_TOKEN_ID",     "label": "Token ID (ak-...)", "hidden": False},
            {"key": "MODAL_TOKEN_SECRET", "label": "Token Secret (as-...)", "hidden": True},
        ],
        "verify": None,  # Modal's auth is gRPC; no clean HTTP verify.
    },
    "daytona": {
        "kind": "compute",
        "display_name": "Daytona",
        "tagline": "Cloud workspace",
        "credentials_url": "https://app.daytona.io/dashboard/keys",
        "secrets": [
            {"key": "DAYTONA_API_KEY", "label": "API Key", "hidden": True},
        ],
        "verify": _verify_daytona,
    },
}


# ─── Infisical config + write API ────────────────────────────────

def load_infisical_config() -> Optional[Dict[str, str]]:
    """Read Infisical credentials from env or ~/.cerver/infisical.env.

    Env var takes precedence so a user can override the saved config
    without editing the file.
    """
    keys = ("INFISICAL_TOKEN", "INFISICAL_PROJECT_ID", "INFISICAL_CLIENT_ID")
    if all(os.environ.get(k) for k in keys):
        return {
            "INFISICAL_TOKEN":      os.environ["INFISICAL_TOKEN"],
            "INFISICAL_PROJECT_ID": os.environ["INFISICAL_PROJECT_ID"],
            "INFISICAL_CLIENT_ID":  os.environ["INFISICAL_CLIENT_ID"],
            "INFISICAL_ENV":        os.environ.get("INFISICAL_ENV", "prod"),
        }

    env_file = Path.home() / ".cerver" / "infisical.env"
    if not env_file.exists():
        return None

    parsed: Dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        parsed[k.strip()] = v.strip()

    if not all(parsed.get(k) for k in keys):
        return None
    parsed.setdefault("INFISICAL_ENV", "prod")
    return parsed


def infisical_login(cfg: Dict[str, str]) -> Optional[str]:
    """Exchange Universal Auth credentials for a short-lived access token."""
    try:
        r = httpx.post(
            "https://app.infisical.com/api/v1/auth/universal-auth/login",
            json={
                "clientId":     cfg["INFISICAL_CLIENT_ID"],
                "clientSecret": cfg["INFISICAL_TOKEN"],
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  {ERROR}Infisical login failed: HTTP {r.status_code}{NC}")
            return None
        return r.json().get("accessToken")
    except Exception as e:
        print(f"  {ERROR}Infisical login error: {e}{NC}")
        return None


def infisical_set_secret(access: str, cfg: Dict[str, str], name: str, value: str) -> bool:
    """Upsert a secret. Tries CREATE first; on conflict, falls back to UPDATE."""
    base = "https://app.infisical.com"
    headers = {"Authorization": f"Bearer {access}"}
    payload = {
        "workspaceId": cfg["INFISICAL_PROJECT_ID"],
        "environment": cfg["INFISICAL_ENV"],
        "secretValue": value,
        "secretPath":  "/",
        "type":        "shared",
    }
    # Create (POST). 200/201 = created, 400/409 = already exists → update.
    try:
        r = httpx.post(
            f"{base}/api/v3/secrets/raw/{name}",
            json=payload, headers=headers, timeout=10,
        )
        if r.status_code in (200, 201):
            return True
        # Update (PATCH) if it already exists.
        r = httpx.patch(
            f"{base}/api/v3/secrets/raw/{name}",
            json=payload, headers=headers, timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"  {ERROR}set_secret({name}) error: {e}{NC}")
        return False


# ─── Interactive flow ────────────────────────────────────────────

def prompt_for_secrets(provider_key: str) -> Dict[str, str]:
    """Ask the user for each required secret. Hidden inputs use getpass."""
    p = PROVIDERS[provider_key]
    creds: Dict[str, str] = {}
    for s in p["secrets"]:
        prompt = f"  {s['key']} ({s['label']}): "
        if s.get("hidden"):
            v = getpass.getpass(prompt)
        else:
            v = input(prompt).strip()
        if v or not s.get("optional"):
            creds[s["key"]] = v
    return creds


def add_one_provider(cfg: Dict[str, str]) -> bool:
    """Run a single provider-add cycle. Returns True if user wants to continue."""
    # Group by kind so the menu makes the conceptual split visible —
    # model providers (who's writing) vs compute providers (where it runs).
    # Numbering stays sequential across groups so a single number picks any.
    keys = list(PROVIDERS.keys())
    model_keys   = [k for k in keys if PROVIDERS[k]["kind"] == "model"]
    local_keys   = [k for k in keys if PROVIDERS[k]["kind"] == "local"]
    compute_keys = [k for k in keys if PROVIDERS[k]["kind"] == "compute"]
    print()
    print(f"{BOLD}Pick a provider to add:{NC}")
    n = 1
    if model_keys:
        print(f"\n  {MUTED}MODEL PROVIDERS{NC}  {MUTED}— who writes the session{NC}")
        for k in model_keys:
            p = PROVIDERS[k]
            print(f"  {n}) {p['display_name']:<15} {MUTED}— {p['tagline']}{NC}")
            n += 1
    if local_keys:
        print(f"\n  {MUTED}LOCAL MODELS{NC}     {MUTED}— run on this machine, free{NC}")
        for k in local_keys:
            p = PROVIDERS[k]
            print(f"  {n}) {p['display_name']:<15} {MUTED}— {p['tagline']}{NC}")
            n += 1
    if compute_keys:
        print(f"\n  {MUTED}COMPUTE PROVIDERS{NC} {MUTED}— where the session runs{NC}")
        for k in compute_keys:
            p = PROVIDERS[k]
            print(f"  {n}) {p['display_name']:<15} {MUTED}— {p['tagline']}{NC}")
            n += 1
    print(f"\n  q) Quit")

    raw = input(f"\n{BOLD}Choice:{NC} ").strip().lower()
    if raw == "q" or raw == "":
        return False

    # The ordered key list matches the displayed numbering (models, then
    # local, then compute).
    ordered_keys = model_keys + local_keys + compute_keys
    try:
        idx = int(raw) - 1
        if idx < 0 or idx >= len(ordered_keys):
            raise ValueError
        provider_key = ordered_keys[idx]
    except ValueError:
        print(f"  {ERROR}Not a valid choice — pick a number 1-{len(ordered_keys)} or q to quit.{NC}")
        return True

    p = PROVIDERS[provider_key]

    # Local providers (Ollama) install + pull on this machine — no
    # credentials, no Infisical push. Hand off to their setup() callable.
    if p.get("setup"):
        p["setup"](cfg)
        again = input(f"\n{BOLD}Add another provider? [y/N]:{NC} ").strip().lower()
        return again == "y"

    print()
    print(f"{BOLD}─── {p['display_name']} ─────────────────────────{NC}")
    print(f"  Get credentials at: {ACCENT}{p['credentials_url']}{NC}")
    print(f"  {MUTED}Paste them below — hidden fields won't echo.{NC}")
    print()

    creds = {k: v for k, v in prompt_for_secrets(provider_key).items() if v}
    required_keys = [s["key"] for s in p["secrets"] if not s.get("optional")]
    missing = [k for k in required_keys if not creds.get(k)]
    if missing:
        print(f"\n  {ERROR}Missing required: {', '.join(missing)}. Try again.{NC}")
        return True

    # Verify (best-effort — providers without HTTP verify are skipped).
    print()
    if p.get("verify"):
        print(f"  {MUTED}Verifying with {p['display_name']}…{NC}")
        try:
            ok, msg = p["verify"](creds)
            if not ok:
                print(f"  {ERROR}✗ Verification failed ({msg}){NC}")
                print(f"  {MUTED}Not pushing to Infisical. Double-check the credentials and retry.{NC}")
                return True
            print(f"  {GREEN}✓ {p['display_name']} accepted the credentials{NC}")
        except Exception as e:
            print(f"  {ERROR}✗ Could not verify: {e}{NC}")
            return True
    else:
        print(f"  {MUTED}({p['display_name']} has no HTTP-auth verify path — pushing without test){NC}")

    # Push to Infisical.
    print()
    print(f"  {MUTED}Pushing to your Infisical project [{cfg['INFISICAL_ENV']}]…{NC}")
    access = infisical_login(cfg)
    if not access:
        return True

    all_ok = True
    for name, value in creds.items():
        if not value:
            continue
        if infisical_set_secret(access, cfg, name, value):
            print(f"  {GREEN}✓{NC} {name}")
        else:
            print(f"  {ERROR}✗{NC} {name} (push failed)")
            all_ok = False

    print()
    if all_ok:
        kind = p["kind"]
        usage = (
            f"a model harness ({provider_key})"
            if kind == "model"
            else f"compute: {{ provider: '{provider_key}' }}"
        )
        print(f"  {GREEN}{BOLD}✓ {p['display_name']} is ready.{NC}")
        print(f"  {MUTED}Cerver sessions can now use {usage}.{NC}")
    else:
        print(f"  {ERROR}Some secrets didn't push. Check your Infisical permissions.{NC}")

    again = input(f"\n{BOLD}Add another provider? [y/N]:{NC} ").strip().lower()
    return again == "y"


def main() -> None:
    print()
    print(f"{ACCENT}{BOLD}cerver · add a provider{NC}")
    print(f"{MUTED}Pushes credentials to your Infisical. Cerver never sees them.{NC}")

    cfg = load_infisical_config()
    if not cfg:
        print()
        print(f"{ERROR}Infisical isn't configured yet.{NC}")
        print(f"{MUTED}Run the installer first — it sets up Infisical:{NC}")
        print(f"  {ACCENT}curl -fsSL https://cerver.ai/install.sh | bash{NC}")
        sys.exit(1)

    print(f"{MUTED}Project: {cfg['INFISICAL_PROJECT_ID']} · env: {cfg['INFISICAL_ENV']}{NC}")

    while add_one_provider(cfg):
        pass

    print()
    print(f"{GREEN}Done.{NC} {MUTED}Run cerver-add-provider again anytime to add more.{NC}")
    print()


if __name__ == "__main__":
    main()
