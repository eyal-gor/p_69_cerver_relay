"""Leave the pool: the owner removed this computer.

Forget this computer's id (so adding it back later, on purpose, starts
fresh) and stop the relay service, the same as `cerver relay uninstall`.
Runs the uninstall a moment later in its own session, so the answer to the
gateway goes out before the service stops this process.
"""

import os
import platform
import shutil
import subprocess
from typing import Optional

from .client import _load_cerver_state, _save_cerver_state


def forget_identity(compute_id: Optional[str] = None) -> int:
    """Drop saved compute ids (just this one when given). Returns how many."""
    state = _load_cerver_state()
    gone = [k for k, v in state.items() if isinstance(v, str) and v.startswith("comp_") and (compute_id is None or v == compute_id)]
    for k in gone:
        del state[k]
    if gone:
        _save_cerver_state(state)
    return len(gone)


def _cerver_bin() -> Optional[str]:
    for p in (shutil.which("cerver"), os.path.expanduser("~/.cerver/bin/cerver"), "/usr/local/bin/cerver",
              "/Applications/Cerver.app/Contents/Resources/cerver"):
        if p and os.path.exists(p):
            return p
    return None


def leave(compute_id: Optional[str] = None) -> bool:
    forget_identity(compute_id)
    if platform.system() not in ("Darwin", "Linux"):
        return False
    binp = _cerver_bin()
    if binp:
        cmd = f"sleep 2; '{binp}' relay uninstall"
    elif platform.system() == "Darwin":
        cmd = 'sleep 2; launchctl bootout gui/$(id -u)/ai.cerver.relay; rm -f ~/Library/LaunchAgents/ai.cerver.relay.plist'
    else:
        cmd = "sleep 2; systemctl --user disable --now cerver-relay.service"
    print("[Cerver] This computer was removed from your pool. Stopping the relay.")
    subprocess.Popen(["/bin/sh", "-c", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
