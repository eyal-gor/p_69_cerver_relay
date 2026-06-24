"""Relay launchd service management (macOS).

Split out of :mod:`relay_client`: installing, uninstalling, and checking the
``dev.cerver.relay`` LaunchAgent that keeps the relay running across logins,
plus migration off the legacy ``dev.kompany.relay`` plist. ``relay_client``
re-imports these names for its CLI dispatch.
"""
import os
import shutil
import sys
from pathlib import Path

from .relay_config import CONFIG_DIR, load_persistent_config

LAUNCHD_LABEL = "dev.cerver.relay"
LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

# Legacy label from the kompany-only era. Kept here so a `branch-monkey-relay
# install` (or any reinstall) can migrate the user off the stale plist —
# the old one pointed at a uv archive path that became stale within hours
# of each push and caused the May 2026 "ghost relay zombies my fixes"
# incident. Migration: unload + delete + replace.
_LEGACY_LAUNCHD_LABEL = "dev.kompany.relay"
_LEGACY_LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LEGACY_LAUNCHD_LABEL}.plist"


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


def install_launchd_service(home_dir: str = None, machine_name: str = None) -> bool:
    """Install the relay as a launchd service. Returns True on success.

    Layout decisions baked in (and learned the hard way on 2026-05-19):

      * **`uv tool uvx --refresh` over a pinned binary path.** The prior
        plist hardcoded `/Users/<user>/.cache/uv/archive-v0/<id>/bin/
        branch-monkey-relay` — that archive id became stale within hours
        of every git push, and KeepAlive resurrected the stale binary
        every 10 seconds, silently shadowing every fix the user had
        just deployed. uvx --refresh resolves the latest commit from
        github on every spawn. Trade-off: needs network at boot.

      * **`cerver-relay --cerver-only` mode** is the install default.
        The mixed-mode (kompany.dev + gateway.cerver.ai) era kept
        two relays fighting for port 18081. Cerver-only is the single
        source of truth.

      * **ThrottleInterval 30s** (was 10s). Tight respawn loops mask
        real failures — relay dies in 4s, restarts, dies in 4s, never
        recovers. 30s lets Infisical / gateway return real errors and
        reduces the odds of two restarts racing for port 18081.

      * **Homebrew-first PATH.** The plist's PATH only needs to be rich
        enough for `uv` to find `git` and `python` during refresh;
        agent_environment.probe() expands it further per-subprocess.
    """
    import subprocess

    if sys.platform != "darwin":
        return False

    uv_bin = shutil.which("uv") or "/Users/" + os.environ.get("USER", "") + "/.local/bin/uv"
    if not Path(uv_bin).exists():
        print("Error: uv not found. Install it first: curl -LsSf https://astral.sh/uv/install.sh | sh")
        return False

    if not home_dir:
        persistent_cfg = load_persistent_config()
        home_dir = persistent_cfg.get("home_dir")
    working_dir = home_dir or str(Path.home() / "Code")
    if not machine_name:
        import socket as _socket
        machine_name = _socket.gethostname().split(".")[0]

    # Migrate off the legacy `dev.kompany.relay` plist if it's still
    # there. KeepAlive on the old one would otherwise keep respawning
    # alongside the new one, two relays fighting for 18081.
    if _LEGACY_LAUNCHD_PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(_LEGACY_LAUNCHD_PLIST_PATH)],
            capture_output=True,
        )
        try:
            _LEGACY_LAUNCHD_PLIST_PATH.unlink()
            print(f"Migrated off legacy plist: {_LEGACY_LAUNCHD_PLIST_PATH}")
        except OSError:
            pass

    program_args = [
        uv_bin, "tool", "uvx", "--refresh",
        "--from", "git+https://github.com/eyal-gor/p_69_cerver_relay.git",
        "cerver-relay",
        "--cerver-only",
        "--cerver-url", "https://gateway.cerver.ai",
        "--name", machine_name,
    ]
    args_xml = "\n".join(f"        <string>{a}</string>" for a in program_args)

    plist_path_env = (
        "/opt/homebrew/bin:/opt/homebrew/sbin:"
        f"{Path.home()}/.local/bin:{Path.home()}/.bun/bin:{Path.home()}/.cargo/bin:"
        "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )

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
    <string>{working_dir}</string>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>{CONFIG_DIR / "relay.log"}</string>
    <key>StandardErrorPath</key>
    <string>{CONFIG_DIR / "relay.err.log"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{plist_path_env}</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
    </dict>
</dict>
</plist>
"""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

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
    """CLI handler for 'branch-monkey-relay install'.

    No longer pre-checks for a `branch-monkey-relay` binary on PATH —
    the new plist invokes `uv tool uvx --refresh git+…` so the binary
    is resolved fresh from github on every spawn. The function only
    needs `uv` itself, and install_launchd_service handles that check.
    """
    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    persistent_cfg = load_persistent_config()
    home_dir = persistent_cfg.get("home_dir")
    machine_name = persistent_cfg.get("machine_name")

    if install_launchd_service(home_dir, machine_name=machine_name):
        print(f"Service '{LAUNCHD_LABEL}' installed and started.")
        print(f"  Plist: {LAUNCHD_PLIST_PATH}")
        print(f"  Logs:  {CONFIG_DIR / 'relay.log'}")
        print(f"  Errors: {CONFIG_DIR / 'relay.err.log'}")
        print()
        print("The relay will auto-start on login and restart if it crashes.")
        print("Each spawn refreshes from github HEAD via `uv tool uvx --refresh`,")
        print("so a new git push reaches you on the next ThrottleInterval cycle.")
        print()
        print("Use 'branch-monkey-relay uninstall' to remove the service.")
    else:
        print("Error: Failed to install launchd service.")
        sys.exit(1)


def _launchd_uninstall():
    """Uninstall the branch-monkey-relay launchd service.

    Also clears the legacy `dev.kompany.relay` plist if it's still
    around — otherwise an `uninstall` would leave the user thinking
    the autostart is gone while the old KeepAlive=true plist quietly
    revives the relay on next login.
    """
    import subprocess

    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    any_found = False
    for label, path in (
        (LAUNCHD_LABEL, LAUNCHD_PLIST_PATH),
        (_LEGACY_LAUNCHD_LABEL, _LEGACY_LAUNCHD_PLIST_PATH),
    ):
        if not path.exists():
            continue
        any_found = True
        result = subprocess.run(
            ["launchctl", "unload", str(path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Warning: launchctl unload {label} returned: {result.stderr.strip()}")
        try:
            path.unlink()
            print(f"Service '{label}' uninstalled — removed {path}")
        except OSError as exc:
            print(f"Warning: could not remove {path}: {exc}")

    if not any_found:
        print(f"Service not installed (no plist at {LAUNCHD_PLIST_PATH}).")


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
