"""
Terminal UI for the Cerver Relay.

Shows a live dashboard with connection status, heartbeat, and system
statistics. Press L to view logs, Q to quit.
"""

import curses
import os
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Any

# Reduce ESC key delay (default 1000ms is way too long)
os.environ.setdefault("ESCDELAY", "25")

from .logo import (
    LOGO, LOGO_WIDTH, LOGO_HEIGHT, GRADIENT_COLORS, get_animated_attrs,
)


class LogCapture:
    """Intercepts writes to a stream and stores them in a ring buffer."""

    def __init__(self, original, max_lines: int = 1000):
        self._original = original
        self._buffer: deque = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "strict")

    def write(self, text: str) -> int:
        if text and text.strip():
            ts = datetime.now().strftime("%H:%M:%S")
            with self._lock:
                for line in text.rstrip("\n").split("\n"):
                    if line.strip():
                        self._buffer.append(f"{ts}  {line}")
        return len(text) if text else 0

    def flush(self):
        pass

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return False

    def reconfigure(self, **kwargs):
        pass

    def get_lines(self, limit: int = 200) -> list:
        with self._lock:
            return list(self._buffer)[-limit:]

    @property
    def closed(self):
        return False


class RelayTUI:
    """Curses-based terminal UI for the Cerver Relay."""

    REFRESH_MS = 2000

    def __init__(self):
        self.state: Dict[str, Any] = {
            "version": "",
            "commit_sha": "",
            # Snapshot from agent_environment.probe(). Keys:
            #   binaries: {name -> resolved path}
            #   missing_required: [names]
            #   shell_path_captured: bool
            "agent_env": {},
            "machine_name": "",
            "machine_id": "",
            "home_dir": "",
            "project": None,
            "project_path": None,
            "port": 18081,
            "dashboard_url": "",
            "cloud_url": "",
            "cerver_only": False,
            "connection": "disconnected",
            "server_running": False,
            "last_heartbeat": None,
            "cerver_last_heartbeat": None,
            "connected_at": None,
            "reconnect_count": 0,
            "requests_handled": 0,
            "auth_state": "idle",
            "auth_url": None,
            "auth_code": None,
            "user_email": None,
            "org_name": None,
            "onboarding_needed": False,
            "registered": None,  # None=pending, True=ok, str=error
            "stream_bridge": None,  # None=not started, True=connected, False=disconnected, str=error
            "launchd": None,  # None=unknown, "running", "installed", "not_installed", "error"
            "launchd_pid": None,
            "launchd_prompt": None,  # None=don't show, "pending"=showing, "done"=answered
            "cli_providers": {},  # {name: {display_name, installed, ...}}
            "default_cli": "claude",
            "cli_prompt": None,  # None=don't show, "pending"=showing, "done"=answered
            "agent_counts": {},
            # Per-agent list — populated by the relay's stats poll from
            # agent_manager.list(). One dict per live agent: id, cli_tool,
            # status, created_at, last_activity, session_id, etc.
            # Rendered as the session list on the Runtime tab.
            "agent_rows": [],
            # Recent Cerver session summaries for this compute. These are
            # gateway records, not just in-memory local agents, so the Runtime
            # tab can show ready/idle sessions after the local agent exits.
            "cerver_session_rows": [],
            "workflow_summary": {},
            "compute": {},
            "compute_health": None,
            "cerver_status": "idle",
            "cerver_compute_id": None,
            "cerver_network_computes": [],
            "cerver_network_updated_at": None,
            "cerver_network_error": None,
        }
        self._stdout_capture = LogCapture(sys.stdout)
        self._stderr_capture = LogCapture(sys.stderr)
        # Main view tabs: "connect" (identity + transport + setup) and
        # "runtime" (workload + compute resources). [I]nstalled and
        # [L]ogs are sub-views; pressing them again returns to whichever
        # main view was last active — tracked in _last_main_view.
        self._view = "connect"
        self._last_main_view = "connect"
        self._running = True
        self._stop_callback: Optional[Callable] = None
        self._scroll_offset = 0
        self._provision_scroll_offset = 0
        self._network_scroll_offset = 0
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._anim_frame = 0
        self._editing_home = False
        self._home_input = ""
        self._home_cursor = 0
        self._editing_name = False
        self._name_input = ""
        self._name_cursor = 0
        self._on_name_set = None  # Callback when machine name is confirmed
        self._onboarding_initialized = False
        self._onboarding_input = ""
        self._onboarding_cursor = 0
        self._on_home_set = None  # Callback when home dir is confirmed
        self._on_logout = None  # Callback when user logs out
        self._on_launchd_install = None  # Callback when user chooses launchd option
        self._launchd_selected = 0  # 0=Yes, 1=No for the prompt menu
        # Quit confirmation. Default selection is "No" so a stray Enter
        # press after the modal opens doesn't kill the relay accidentally.
        self._quit_selected = 1  # 0=Yes (quit), 1=No (cancel)
        # Highlighted row in the Runtime tab's session list. ↑/↓ (or j/k)
        # while on the Runtime view shifts it. Auto-clamped when the list
        # changes size (agent finishes, new one starts) so the cursor
        # doesn't fall off the end.
        self._runtime_session_idx = 0
        self._on_cli_set = None  # Callback when user selects a CLI provider
        self._cli_selected = 0  # Index into installed providers list
        # CLI auth sub-modes within CLI prompt
        self._cli_auth_mode = None  # None, "api_key", "device_auth"
        self._cli_api_key_input = ""
        self._cli_api_key_cursor = 0
        self._cli_device_auth = None  # {url, code, message} from device auth
        self._on_cli_api_key = None  # Callback(provider_name, key)
        self._on_cli_device_auth = None  # Callback(provider_name) -> starts async auth
        self._on_cli_install = None  # Callback(provider_name) -> installs CLI async
        self._on_cli_refresh = None  # Callback() -> refreshes provider state
        self._cli_installing = None  # Name of provider being installed
        # Per-tab field cursor for Up/Down + Enter navigation. Only the
        # Connect tab has actionable rows today (machine, home, CLI,
        # startup, logout). Index resets on tab switch so cursor doesn't
        # land on a stale slot. See _connect_field_keys() for the live
        # ordered list (it's dynamic — launchd row only renders when
        # launchd state is known).
        self._field_cursor = 0
        self._verbose = False
        self._history_len = 36
        self._metric_history = {
            "cpu": deque(maxlen=self._history_len),
            "memory": deque(maxlen=self._history_len),
            "load": deque(maxlen=self._history_len),
            "disk": deque(maxlen=self._history_len),
        }

    def install_capture(self):
        """Redirect stdout/stderr to capture logs."""
        sys.stdout = self._stdout_capture
        sys.stderr = self._stderr_capture

    def restore_streams(self):
        """Restore original stdout/stderr."""
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr

    def update(self, **kwargs):
        """Update state dict (thread-safe for simple dict updates)."""
        self.state.update(kwargs)
        compute = kwargs.get("compute")
        if compute:
            self._record_compute_history(compute)
            self.state["compute_health"] = self._get_compute_health(compute)

    def run(self, stop_callback: Optional[Callable] = None):
        """Run the TUI. Blocks until user quits."""
        self._stop_callback = stop_callback
        try:
            # Force stdin to read from the terminal. curses uses C-level
            # stdin (fd 0) for getch(). If fd 0 was consumed or redirected
            # (e.g. curl|bash, process managers, uvx), input silently breaks.
            try:
                tty_fd = os.open("/dev/tty", os.O_RDONLY)
                if tty_fd != 0:
                    os.dup2(tty_fd, 0)
                    os.close(tty_fd)
            except OSError:
                pass
            curses.wrapper(self._main_loop)
        except KeyboardInterrupt:
            pass
        finally:
            self.restore_streams()
            if self._stop_callback:
                self._stop_callback()

    def stop(self):
        self._running = False

    # ── curses main loop ─────────────────────────────────────────────

    def _main_loop(self, stdscr):
        default_bg = -1
        try:
            curses.use_default_colors()
        except curses.error:
            default_bg = curses.COLOR_BLACK
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        # Short timeout so we poll keys every 100ms for responsive input
        stdscr.timeout(100)

        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_GREEN, default_bg)
            curses.init_pair(2, curses.COLOR_RED, default_bg)
            curses.init_pair(3, curses.COLOR_YELLOW, default_bg)
            curses.init_pair(4, curses.COLOR_CYAN, default_bg)
            try:
                curses.init_pair(5, 8, default_bg)  # bright black (gray)
            except curses.error:
                curses.init_pair(5, curses.COLOR_WHITE, default_bg)
            # Logo gradient: smooth indigo → white (8 levels, pairs 10–17)
            for i, color_num in enumerate(GRADIENT_COLORS):
                try:
                    curses.init_pair(10 + i, color_num, default_bg)
                except curses.error:
                    fb = curses.COLOR_BLUE if i < 3 else (curses.COLOR_CYAN if i < 6 else curses.COLOR_WHITE)
                    curses.init_pair(10 + i, fb, default_bg)

        last_draw = 0.0
        # Logo animates smoothly
        ANIM_INTERVAL = 0.1  # 10 fps — smooth shimmer

        while self._running:
            now = time.monotonic()
            # Animated logo wants 10fps redraws on tabs that show it;
            # Help is static so it falls back to the slower REFRESH_MS.
            interval = ANIM_INTERVAL if self._view in ("connect", "provision", "network", "runtime") else self.REFRESH_MS / 1000.0
            if now - last_draw >= interval:
                self._anim_frame += 1
                try:
                    stdscr.erase()
                    h, w = stdscr.getmaxyx()
                    if self._view == "connect":
                        self._draw_connect(stdscr, h, w)
                    elif self._view == "provision":
                        self._draw_provision(stdscr, h, w)
                    elif self._view == "network":
                        self._draw_network(stdscr, h, w)
                    elif self._view == "runtime":
                        self._draw_runtime(stdscr, h, w)
                    elif self._view == "help":
                        self._draw_help(stdscr, h, w)
                    else:
                        self._draw_logs(stdscr, h, w)
                    stdscr.refresh()
                except curses.error:
                    pass
                last_draw = time.monotonic()

            # getch blocks for up to 100ms (set by timeout above)
            key = stdscr.getch()
            if key != -1:
                self._handle_key(key, stdscr)
                last_draw = 0.0  # Force redraw after key press

    def _handle_key(self, key, stdscr=None):
        # Onboarding mode (text input active, only Ctrl-C quits)
        s = self.state
        if s.get("onboarding_needed") and s["auth_state"] not in ("authenticating", "waiting"):
            self._handle_onboarding_key(key, stdscr)
            return

        # CLI selection prompt mode
        if s.get("cli_prompt") == "pending":
            self._handle_cli_prompt_key(key)
            return

        # Launchd prompt mode
        if s.get("launchd_prompt") == "pending":
            self._handle_launchd_prompt_key(key)
            return

        # Quit confirmation modal — takes over keyboard until resolved.
        # Sits AFTER the wizard prompts above so it can't fire while
        # the user is mid-onboarding (those modes have their own quit
        # paths via Ctrl-C / Esc; double-confirming would just be noise).
        if s.get("quit_prompt") == "pending":
            self._handle_quit_prompt_key(key)
            return

        # Machine name editing mode
        if self._editing_name:
            if key in (curses.KEY_ENTER, 10, 13):  # Enter
                name = self._name_input.strip()
                if name:
                    self.state["machine_name"] = name
                    if self._on_name_set:
                        self._on_name_set(name)
                self._editing_name = False
                if stdscr:
                    curses.curs_set(0)
            elif key == 27:  # Escape — cancel
                self._editing_name = False
                if stdscr:
                    curses.curs_set(0)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if self._name_cursor > 0:
                    self._name_input = (
                        self._name_input[: self._name_cursor - 1]
                        + self._name_input[self._name_cursor :]
                    )
                    self._name_cursor -= 1
            elif key == curses.KEY_LEFT:
                self._name_cursor = max(0, self._name_cursor - 1)
            elif key == curses.KEY_RIGHT:
                self._name_cursor = min(len(self._name_input), self._name_cursor + 1)
            elif key == curses.KEY_HOME or key == 1:  # Ctrl-A
                self._name_cursor = 0
            elif key == curses.KEY_END or key == 5:  # Ctrl-E
                self._name_cursor = len(self._name_input)
            elif 32 <= key <= 126:
                ch = chr(key)
                self._name_input = (
                    self._name_input[: self._name_cursor]
                    + ch
                    + self._name_input[self._name_cursor :]
                )
                self._name_cursor += 1
            return

        # Home directory editing mode
        if self._editing_home:
            if key in (curses.KEY_ENTER, 10, 13):  # Enter
                path = self._home_input.strip()
                if path and os.path.isdir(os.path.expanduser(path)):
                    expanded = os.path.expanduser(path)
                    self.state["home_dir"] = expanded
                    if self._on_home_set:
                        self._on_home_set(expanded)
                self._editing_home = False
            elif key == 27:  # Escape — cancel
                self._editing_home = False
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if self._home_cursor > 0:
                    self._home_input = (
                        self._home_input[: self._home_cursor - 1]
                        + self._home_input[self._home_cursor :]
                    )
                    self._home_cursor -= 1
            elif key == curses.KEY_LEFT:
                self._home_cursor = max(0, self._home_cursor - 1)
            elif key == curses.KEY_RIGHT:
                self._home_cursor = min(len(self._home_input), self._home_cursor + 1)
            elif key == curses.KEY_HOME or key == 1:  # Ctrl-A
                self._home_cursor = 0
            elif key == curses.KEY_END or key == 5:  # Ctrl-E
                self._home_cursor = len(self._home_input)
            elif 32 <= key <= 126:
                ch = chr(key)
                self._home_input = (
                    self._home_input[: self._home_cursor]
                    + ch
                    + self._home_input[self._home_cursor :]
                )
                self._home_cursor += 1
            return

        if key == ord("q") or key == ord("Q"):
            # Defer the actual quit until the confirmation modal resolves.
            # Reset the selection to "No" each open so a long-held Enter
            # doesn't barrel through both the open and the confirm.
            self._quit_selected = 1
            self.state["quit_prompt"] = "pending"
        elif key == ord("l") or key == ord("L"):
            if self._view == "logs":
                self._view = self._last_main_view
            else:
                if self._view in ("connect", "provision", "network", "runtime", "help"):
                    self._last_main_view = self._view
                self._view = "logs"
                self._scroll_offset = 0
            self._field_cursor = 0
        elif key == ord("1"):
            # Direct nav: Connect tab. Also resets _last_main_view so
            # [L] from a sub-view returns here.
            if self._view != "connect":
                self._view = "connect"
                self._last_main_view = "connect"
                self._field_cursor = 0
        elif key == ord("2"):
            # Direct nav: Provision tab (cerver-side compute identity).
            if self._view != "provision":
                self._view = "provision"
                self._last_main_view = "provision"
                self._field_cursor = 0
        elif key == ord("3"):
            # Direct nav: Network tab.
            if self._view != "network":
                self._view = "network"
                self._last_main_view = "network"
                self._field_cursor = 0
        elif key == ord("4"):
            # Direct nav: Runtime tab.
            if self._view != "runtime":
                self._view = "runtime"
                self._last_main_view = "runtime"
                self._field_cursor = 0
        elif key == ord("5"):
            # Direct nav: Logs tab. Mirrors the [L] toggle's enter path,
            # without the back-toggle behavior — [5] always lands you on
            # logs even if you were already there.
            if self._view != "logs":
                self._view = "logs"
                self._scroll_offset = 0
            self._last_main_view = "logs"
            self._field_cursor = 0
        elif key == ord("6"):
            # Direct nav: Help tab. Static reference page — no live
            # state, so refresh interval falls back to slow.
            if self._view != "help":
                self._view = "help"
                self._last_main_view = "help"
                self._field_cursor = 0
        elif key == ord("n") or key == ord("N"):
            if self._view == "connect":
                self._action_edit_name(stdscr)
        elif key == ord("h") or key == ord("H"):
            # Home moved to Runtime — accept [H] from either tab so old
            # muscle memory still works.
            if self._view in ("connect", "runtime"):
                self._action_edit_home(stdscr)
        elif key == ord("v") or key == ord("V"):
            if self._view in ("connect", "runtime"):
                self._verbose = not self._verbose
        elif key == ord("s") or key == ord("S"):
            if self._view in ("connect", "provision"):
                self._action_toggle_launchd()
        elif key == ord("c") or key == ord("C"):
            # AI CLI moved to Runtime — accept [C] from either tab too.
            if self._view in ("connect", "runtime"):
                self._action_pick_cli()
        elif key == ord("d") or key == ord("D"):
            if self._view == "connect":
                self._action_logout()
        elif key == curses.KEY_UP and self._view == "logs":
            self._scroll_offset += 1
        elif key == curses.KEY_DOWN and self._view == "logs":
            self._scroll_offset = max(0, self._scroll_offset - 1)
        elif key == curses.KEY_UP and self._view == "network":
            self._network_scroll_offset = max(0, self._network_scroll_offset - 1)
        elif key == curses.KEY_DOWN and self._view == "network":
            self._network_scroll_offset += 1
        elif key in (getattr(curses, "KEY_SR", 337), 337) and self._view == "provision":
            self._provision_scroll_offset = max(0, self._provision_scroll_offset - 1)
        elif key in (getattr(curses, "KEY_SF", 336), 336) and self._view == "provision":
            self._provision_scroll_offset += 1
        elif key == curses.KEY_UP and self._view in ("connect", "provision", "runtime"):
            fields = self._active_field_keys()
            if fields:
                self._field_cursor = (self._field_cursor - 1) % len(fields)
        elif key == curses.KEY_DOWN and self._view in ("connect", "provision", "runtime"):
            fields = self._active_field_keys()
            if fields:
                self._field_cursor = (self._field_cursor + 1) % len(fields)
        elif key in (curses.KEY_ENTER, 10, 13) and self._view in ("connect", "provision", "runtime"):
            # Enter on the focused field dispatches to its action handler
            # — same path as the legacy letter shortcuts ([N]/[H]/[S]/[C]/[D]),
            # which we keep around for muscle-memory back-compat. Connect
            # (name/launchd/logout), Provision (update_cli), and Runtime
            # (home/cli) flow through the same handler dispatch table.
            fields = self._active_field_keys()
            if 0 <= self._field_cursor < len(fields):
                key_name = fields[self._field_cursor]
                handler = {
                    "name": lambda: self._action_edit_name(stdscr),
                    "home": lambda: self._action_edit_home(stdscr),
                    "cli": self._action_pick_cli,
                    "launchd": self._action_toggle_launchd,
                    "logout": self._action_logout,
                    "update_cli": self._action_update_cli,
                }.get(key_name)
                if handler:
                    handler()
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and self._view in ("connect", "provision", "network", "runtime", "help", "logs"):
            # ←/→ cycles forward/backward through the main tabs.
            # Provision sits between Connect (machine identity) and
            # Network (account compute mesh), then Runtime (local workload).
            order = ["connect", "provision", "network", "runtime", "logs", "help"]
            i = order.index(self._view)
            step = -1 if key == curses.KEY_LEFT else 1
            self._view = order[(i + step) % len(order)]
            # Reset the field cursor so a fresh tab lands focus on the
            # first actionable row instead of carrying a stale index
            # from a different tab's field list.
            self._field_cursor = 0
            # Logs has its own scroll state; tracking it as a "main view"
            # so toggling [L] from elsewhere remembers it as the return
            # target is fine — same semantics the other tabs already use.
            self._last_main_view = self._view

    # ── connect-tab field actions (also triggered by Enter on cursor) ──

    def _connect_field_keys(self) -> list:
        """Ordered list of focusable field keys on the Connect tab.

        Connect is identity + transport + setup. Home and AI CLI moved
        to Runtime (they describe HOW the relay runs work, not WHO it
        is). Launchd row is conditional on `self.state["launchd"]`.
        """
        fields = ["name"]
        if self.state.get("launchd") is not None:
            fields.append("launchd")
        fields.append("logout")
        return fields

    def _runtime_field_keys(self) -> list:
        """Ordered list of focusable field keys on the Runtime tab.

        Runtime covers HOW the relay runs work: where (Home) and with
        what (AI CLI default), plus a row per live agent for the
        session-list view. Up/Down cycles through everything in one
        ring, so the user can navigate from "Home" all the way down
        into "session 3" without learning a second keybinding.

        Session keys are `session_<agent_id>` — the dispatcher table
        in _handle_key doesn't (yet) have a handler for them, so Enter
        on a session row is a no-op. That's fine for now; future work
        will bind Enter to a tail / kill / peek action.
        """
        keys = ["home", "cli"]
        for row in self.state.get("agent_rows") or []:
            if not isinstance(row, dict):
                continue
            aid = row.get("id")
            if aid:
                keys.append(f"session_{aid}")
        for row in self.state.get("cerver_session_rows") or []:
            if not isinstance(row, dict):
                continue
            sid = row.get("sessionId") or row.get("session_id")
            if sid:
                keys.append(f"cerver_session_{sid}")
        return keys

    def _provision_field_keys(self) -> list:
        """Ordered list of focusable field keys on the Provision tab.

        Provision is mostly read-only telemetry, but the MAINTENANCE
        section offers an in-place CLI upgrade action.
        """
        fields = ["update_cli"]
        if self.state.get("launchd") is not None:
            fields.append("launchd")
        return fields

    def _active_field_keys(self) -> list:
        """Field list for whichever tab the cursor is currently on."""
        if self._view == "connect":
            return self._connect_field_keys()
        if self._view == "runtime":
            return self._runtime_field_keys()
        if self._view == "provision":
            return self._provision_field_keys()
        return []

    def _action_edit_name(self, stdscr=None):
        self._editing_name = True
        self._name_input = self.state.get("machine_name", "")
        self._name_cursor = len(self._name_input)
        if stdscr:
            curses.curs_set(1)

    def _action_edit_home(self, stdscr=None):
        self._editing_home = True
        self._home_input = self.state.get("home_dir", "")
        self._home_cursor = len(self._home_input)
        if stdscr:
            curses.curs_set(1)

    def _action_toggle_launchd(self):
        if not self._on_launchd_install:
            return
        ld = self.state.get("launchd")
        if ld in ("running", "installed"):
            self._on_launchd_install(False)
        else:
            self._on_launchd_install(True)

    def _action_pick_cli(self):
        providers = self.state.get("cli_providers", {})
        installed = [n for n, p in providers.items() if p.get("installed")]
        current = self.state.get("default_cli", "claude")
        self._cli_selected = installed.index(current) if current in installed else 0
        self.state["cli_prompt"] = "pending"

    def _action_logout(self):
        if not self._on_logout:
            return
        self._on_logout()
        self._running = False

    def _action_update_cli(self):
        """Spawn `cerver update` in a background thread so the TUI
        keeps redrawing while the upgrade runs. Output flows through
        the relay's stdout (captured by LogCapture, visible in the
        Logs tab); a short status surfaces inline on Provision."""
        if self.state.get("cerver_update_state") == "running":
            return  # already in flight — Enter is idempotent
        self.state["cerver_update_state"] = "running"
        self.state["cerver_update_last_msg"] = "Starting…"
        import threading
        threading.Thread(target=self._run_cerver_update, daemon=True).start()

    def _run_cerver_update(self):
        """Worker: locates the cerver binary (preferring the same path
        the user actually invokes), runs `cerver update`, and reports
        outcome via self.state so the Provision row updates on the next
        draw tick.

        Extends PATH to include common toolchain directories before
        spawning — the relay process is often started from launchd or
        a curl|bash installer with a minimal PATH, missing the dirs
        where Homebrew, asdf, or `g` install `go`. `cerver update`
        shells out to `go install`, so a missing `go` on the subprocess
        PATH is the #1 reason this verb fails from the TUI even when
        it works in the user's interactive shell.
        """
        import subprocess
        try:
            cerver_bin = os.path.expanduser("~/.cerver/bin/cerver")
            if not os.path.exists(cerver_bin):
                resolved = shutil.which("cerver")
                if resolved:
                    cerver_bin = resolved
                else:
                    self.state["cerver_update_state"] = "failed"
                    self.state["cerver_update_last_msg"] = "cerver binary not found on PATH"
                    return
            # Build an enriched PATH for the subprocess. Order: existing
            # PATH first (so user customizations win), then common dirs
            # where go/node/npm tend to live. Dedupe to keep it short.
            base_path = os.environ.get("PATH", "")
            extra_dirs = [
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/local/go/bin",
                os.path.expanduser("~/go/bin"),
                os.path.expanduser("~/.asdf/shims"),
                os.path.expanduser("~/.local/bin"),
            ]
            existing = set(base_path.split(":"))
            for d in extra_dirs:
                if d and d not in existing and os.path.isdir(d):
                    base_path = (base_path + ":" + d) if base_path else d
                    existing.add(d)
            env = {**os.environ, "PATH": base_path}
            print(f"[update] running {cerver_bin} update (PATH augmented)")
            proc = subprocess.run(
                [cerver_bin, "update"],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            # Echo captured output into the relay log stream so the
            # Logs tab shows the full transcript.
            for line in (proc.stdout or "").splitlines():
                print(f"[update] {line}")
            for line in (proc.stderr or "").splitlines():
                print(f"[update err] {line}")
            if proc.returncode == 0:
                self.state["cerver_update_state"] = "success"
                # Pull last "Modified:" / "Installed:" line as the
                # surface message so the user sees what's new.
                summary = "Updated"
                for line in reversed((proc.stdout or "").splitlines()):
                    line = line.strip()
                    if line.startswith("Installed:") or line.startswith("Modified:"):
                        summary = line
                        break
                self.state["cerver_update_last_msg"] = summary[:80]
            else:
                self.state["cerver_update_state"] = "failed"
                err = (proc.stderr or proc.stdout or "").strip().splitlines()
                tail = err[-1] if err else f"exit {proc.returncode}"
                self.state["cerver_update_last_msg"] = tail[:80]
        except subprocess.TimeoutExpired:
            self.state["cerver_update_state"] = "failed"
            self.state["cerver_update_last_msg"] = "timed out after 180s"
        except Exception as exc:  # noqa: BLE001
            self.state["cerver_update_state"] = "failed"
            self.state["cerver_update_last_msg"] = str(exc)[:80]

    def _focused_field_key(self) -> Optional[str]:
        """The symbolic key of the currently focused row on whichever
        tab supports cursor nav (Connect or Runtime), or None when the
        cursor doesn't apply to the current view (Help / Logs)."""
        fields = self._active_field_keys()
        if not fields:
            return None
        if 0 <= self._field_cursor < len(fields):
            return fields[self._field_cursor]
        return None

    # One-line title + 1–3 line description for each focusable field.
    # Surfaced below the rows in an "About" panel that updates as the
    # cursor moves — so the user can read what a row does without
    # leaving the tab. Keep entries short; the panel reserves ~4 rows.
    _FIELD_HELP = {
        "name": (
            "Machine name",
            "Display label for this compute in the cerver dashboard and in",
            "`cerver sessions` listings. Doesn't affect routing or auth.",
        ),
        "home": (
            "Home directory",
            "Default working directory the relay spawns CLI sessions in.",
            "Tasks without an explicit cwd inherit this path.",
        ),
        "cli": (
            "Default AI CLI",
            "Provider used by `cerver run` / `cerver compare` when no",
            "--cli flag is given. Switching here applies to new runs only.",
        ),
        "launchd": (
            "Startup (launchd)",
            "Whether macOS auto-launches a background relay on login.",
            "If you run a manual relay too, both can fight for port 18081.",
        ),
        "logout": (
            "Logout",
            "Signs out of this machine's cerver account and quits the relay.",
            "You'll need to run `cerver login` again to reconnect.",
        ),
        "update_cli": (
            "Update CLI",
            "Reinstalls the cerver CLI from the latest commit on main.",
            "Output streams into the Logs tab; status surfaces here.",
        ),
    }

    def _draw_field_help(self, stdscr, y, col, bar_w):
        """Render an 'About' section explaining the focused field.

        Returns the y after the section so the caller can keep stacking
        content below. No-ops when no field is focused (Help/Logs tabs).
        """
        key = self._focused_field_key()
        if not key or key not in self._FIELD_HELP:
            return y
        title, line1, line2 = self._FIELD_HELP[key]
        self._hline(stdscr, y, col, bar_w)
        y += 1
        self._put(stdscr, y, col + 2, "About  ·  " + title, self._bold() | self._cyan())
        y += 1
        self._put(stdscr, y, col + 2, line1, self._dim())
        y += 1
        self._put(stdscr, y, col + 2, line2, self._dim())
        y += 1
        return y

    def _focus_attr(self, base_attr=0):
        """Return the attr to use for a row's text when focused.
        Reverse-video on top of whatever base styling the row uses, so
        the active row reads as 'inverse highlighted' across the label
        + value — no separate cursor glyph needed."""
        return base_attr | curses.A_REVERSE

    def _draw_focus_marker(self, stdscr, y, col, is_focused):
        """No-op stub kept so old call sites still work after the
        focus-mark switched from a ▶ glyph to inverse-video text. All
        rendering of the focus state now happens via _focus_attr() on
        the row's label/value attrs."""
        return

    # ── drawing helpers ──────────────────────────────────────────────

    def _put(self, stdscr, y, x, text, attr=0):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def _hline(self, stdscr, y, x, length):
        h, w = stdscr.getmaxyx()
        actual = min(length, w - x - 1)
        if 0 <= y < h and actual > 0:
            self._put(stdscr, y, x, "\u2500" * actual, self._dim())

    def _green(self):
        return curses.color_pair(1) if curses.has_colors() else 0

    def _red(self):
        return curses.color_pair(2) if curses.has_colors() else 0

    def _yellow(self):
        return curses.color_pair(3) if curses.has_colors() else 0

    def _cyan(self):
        return curses.color_pair(4) if curses.has_colors() else 0

    def _dim(self):
        return curses.color_pair(5) if curses.has_colors() else curses.A_DIM

    def _bold(self):
        return curses.A_BOLD

    def _version_label(self, s) -> str:
        """Header version string: `v369 · 8b6e791`. The commit SHA lets
        you tell at a glance whether a restart actually loaded fresh code
        — previously two relays running different commits both showed
        the same v-number and you had to grep ps + uv cache to disambiguate.
        """
        ver = s.get("version") or ""
        sha = s.get("commit_sha") or ""
        if ver and sha:
            return f"v{ver} · {sha}"
        if ver:
            return f"v{ver}"
        return sha

    # ── connect view ─────────────────────────────────────────────────
    # Identity (who am I), transport (am I reachable?), setup (install
    # state of relay autostart + cerver CLI). Sibling view to Runtime,
    # which covers workload/resource use. Both share the animated logo
    # header. Modal overlays (auth, onboarding, launchd prompt, CLI
    # selector) all surface here because Connect is the default landing
    # view on startup and on logout.

    def _draw_connect(self, stdscr, h, w):
        s = self.state
        col = 2
        lbl_col = 4
        val_col = 20
        bar_w = min(50, w - 4)
        y = 1

        # Header — animated CERVER logo with a per-tab subtitle. The
        # tab name doubles as the screen title so the user always knows
        # which view they're on; version trails for support context.
        ver = self._version_label(s)
        subtitle_text = "Cerver Connect"
        if w >= LOGO_WIDTH + 6:
            self._draw_animated_logo(stdscr, y, col)
            y += LOGO_HEIGHT
            # Subtitle bumped from dim → bold green so the active tab
            # name pops next to the animated logo. Version stays dim
            # and trails the title as soft context.
            self._put(stdscr, y, col + LOGO_WIDTH - len(subtitle_text), subtitle_text, self._bold() | self._green())
            if ver:
                self._put(stdscr, y + 1, col + LOGO_WIDTH - len(ver), ver, self._dim())
                y += 2
            else:
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2
        else:
            self._put(stdscr, y, col, subtitle_text, self._bold() | self._green())
            y += 1
            if ver:
                self._put(stdscr, y, col, ver, self._dim())
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2

        # Auth screen (takes over dashboard while authenticating)
        if s["auth_state"] in ("authenticating", "waiting"):
            self._draw_auth(stdscr, y, col, bar_w)
            return

        # Onboarding screen (first run, after auth)
        if s.get("onboarding_needed"):
            self._draw_onboarding(stdscr, y, col, bar_w)
            return

        # Launchd install prompt (after onboarding)
        if s.get("launchd_prompt") == "pending":
            self._draw_launchd_prompt(stdscr, y, col, bar_w)
            return

        # CLI selection prompt (after launchd, or when [C] pressed)
        if s.get("cli_prompt") == "pending":
            self._draw_cli_prompt(stdscr, y, col, bar_w)
            return

        # Quit confirmation modal — last so it overlays whatever the
        # user was looking at, but never collides with the wizards.
        if s.get("quit_prompt") == "pending":
            self._draw_quit_prompt(stdscr, y, col, bar_w)
            return

        # Version — number of commits in the relay repo (bumps every commit)
        if s.get("version"):
            self._put(stdscr, y, lbl_col, "Version", self._dim())
            self._put(stdscr, y, val_col, s["version"], self._bold())
            y += 1

        # Account info
        if s.get("user_email"):
            self._put(stdscr, y, lbl_col, "User", self._dim())
            self._put(stdscr, y, val_col, s["user_email"], self._bold())
            y += 1

        # Machine info — editable field. Inverse-video on label+value
        # when focused (selected by Up/Down). Old letter shortcuts ([N])
        # still work but no longer clutter the row.
        focused_name = self._focused_field_key() == "name"
        rev_name = curses.A_REVERSE if focused_name else 0
        self._put(stdscr, y, lbl_col, "Machine", self._dim() | rev_name)
        if self._editing_name:
            field_w = max(30, bar_w - val_col + col)
            display = self._name_input[:field_w]
            self._put(stdscr, y, val_col, display, self._bold() | self._cyan())
            cursor_x = val_col + min(self._name_cursor, field_w)
            try:
                stdscr.move(y, cursor_x)
            except curses.error:
                pass
        else:
            machine_val = s.get("machine_name", "\u2014")
            self._put(stdscr, y, val_col, machine_val, self._bold() | rev_name)
        y += 1

        # Home + AI CLI moved to the Runtime tab — they describe HOW
        # the relay runs work (working directory, default model agent),
        # which fits Runtime's mandate better than Connect's identity-
        # and-transport focus.

        dashboard_url = s.get("dashboard_url", f"http://localhost:{s['port']}/")
        self._put(stdscr, y, lbl_col, "Dashboard", self._dim())
        self._put(stdscr, y, val_col, dashboard_url, self._bold())
        y += 1


        y += 1
        self._put(stdscr, y, lbl_col, "STATUS", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        # Cerver connection — single source of truth for relay presence.
        cerver_status = s.get("cerver_status", "idle")
        cerver_compute_id = s.get("cerver_compute_id")
        cerver_compute_label = s.get("cerver_compute_label")
        cerver_url = s.get("cerver_url") or "gateway.cerver.ai"

        self._put(stdscr, y, lbl_col, "Cerver", self._dim())
        if cerver_status == "connected":
            self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, f"Connected  ·  {cerver_url}")
        elif cerver_status == "connecting":
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, f"Connecting...  ·  {cerver_url}")
        elif isinstance(cerver_compute_id, str) and cerver_compute_id.startswith("error:"):
            self._put(stdscr, y, val_col, "\u25cf", self._red() | self._bold())
            self._put(stdscr, y, val_col + 2, "Registration failed")
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, f"Waiting...  ·  {cerver_url}")
        y += 1

        self._put(stdscr, y, lbl_col, "Compute", self._dim())
        if isinstance(cerver_compute_id, str) and cerver_compute_id and not cerver_compute_id.startswith("error:"):
            self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
            label = cerver_compute_label or s.get("machine_name") or "compute"
            display = f"{label}  ·  {cerver_compute_id}"
            self._put(stdscr, y, val_col + 2, display[:bar_w - val_col - 2])
        elif isinstance(cerver_compute_id, str) and cerver_compute_id.startswith("error:"):
            self._put(stdscr, y, val_col, "\u25cf", self._red() | self._bold())
            self._put(stdscr, y, val_col + 2, cerver_compute_id[:bar_w - val_col - 2])
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Registering...")
        y += 1

        # Health (derived from CPU/memory/disk + active agents)
        health = s.get("compute_health") or "\u2014"
        health_attr = self._green() | self._bold()
        if health == "Unhealthy":
            health_attr = self._red() | self._bold()
        elif health == "Stressed":
            health_attr = self._yellow() | self._bold()
        elif health == "Busy":
            health_attr = self._cyan() | self._bold()
        elif health == "\u2014":
            health_attr = self._dim()
        active_agents = (s.get("agent_counts") or {}).get("running", 0)
        active_workflows = (((s.get("workflow_summary") or {}).get("counts")) or {}).get("running", 0)
        active_total = active_agents + active_workflows
        suffix = ""
        if health == "Busy" and active_total > 0:
            unit_a = "agent" if active_agents == 1 else "agents"
            suffix = f"  ·  {active_agents} {unit_a} running"
            if active_workflows > 0:
                unit_w = "workflow" if active_workflows == 1 else "workflows"
                suffix += f", {active_workflows} {unit_w}"
        self._put(stdscr, y, lbl_col, "Health", self._dim())
        self._put(stdscr, y, val_col, "\u25cf", health_attr)
        self._put(stdscr, y, val_col + 2, f"{health}{suffix}")
        y += 1

        # Local server
        self._put(stdscr, y, lbl_col, "Local Server", self._dim())
        if s["server_running"]:
            self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, f"Running :{s['port']}")
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Starting...")
        y += 1
        if self._verbose:
            self._put(stdscr, y, val_col, "Handles agent execution requests", self._dim())
            y += 1

        # Heartbeat (from cerver connect channel)
        self._put(stdscr, y, lbl_col, "Heartbeat", self._dim())
        hb = s.get("cerver_last_heartbeat")
        if hb:
            ago = int((datetime.now(timezone.utc) - hb).total_seconds())
            if ago < 60:
                self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
                self._put(stdscr, y, val_col + 2, f"OK  {ago}s ago")
            else:
                self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
                self._put(stdscr, y, val_col + 2, f"Stale  {ago}s ago")
        elif s.get("cerver_status") == "connected":
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Waiting...")
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._dim())
            self._put(stdscr, y, val_col + 2, "\u2014")
        y += 1
        if self._verbose:
            self._put(stdscr, y, val_col, "Keeps connection alive, detects drops", self._dim())
            y += 1

        # Startup (launchd)
        ld = s.get("launchd")
        if ld is not None:
            focused_ld = self._focused_field_key() == "launchd"
            rev_ld = curses.A_REVERSE if focused_ld else 0
            self._put(stdscr, y, lbl_col, "Startup", self._dim() | rev_ld)
            if ld == "running":
                self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Enabled", rev_ld)
            elif ld == "installed":
                self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Installed (not running)", rev_ld)
            elif ld == "error":
                self._put(stdscr, y, val_col, "\u25cf", self._red() | self._bold() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Error", rev_ld)
            else:
                self._put(stdscr, y, val_col, "\u25cf", self._dim() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Not installed", rev_ld)
            y += 1
            if self._verbose:
                self._put(stdscr, y, val_col, "Auto-start on login via launchd", self._dim())
                y += 1


        # Reconnects — moved from the old WORKLOAD block. Belongs to
        # Connect because it's a transport-stability metric, not a
        # measure of how much work the relay is doing.
        rc = s.get("reconnect_count", 0)
        self._put(stdscr, y, lbl_col, "Reconnects", self._dim())
        self._put(stdscr, y, val_col, str(rc), self._green() if rc == 0 else self._yellow())
        y += 1

        # Logout — action row, no live data. Inverse-highlighted when
        # focused by Up/Down for the same affordance as the editable
        # rows. Legacy [D] keystroke still works.
        y += 1
        focused_logout = self._focused_field_key() == "logout"
        rev_logout = curses.A_REVERSE if focused_logout else 0
        self._put(stdscr, y, lbl_col, "Logout", self._dim() | rev_logout)
        self._put(stdscr, y, val_col, "Sign out of this machine", self._dim() | rev_logout)
        y += 1

        # Contextual help — explains whatever row is currently focused
        # so the user can read what each action does without leaving
        # the tab. Renders only when a row is focused (Connect always
        # has one of name/launchd/logout focused).
        y += 1
        y = self._draw_field_help(stdscr, y, col, bar_w)

        # Footer — tab nav first, then connect-specific actions.
        if not self._editing_home:
            curses.curs_set(0)

        # TRY — onboarding hints for the /cerver Claude Code skill. Renders
        # only until the first request lands; after the user is up and running
        # it auto-hides so STATUS / WORKLOAD / COMPUTE get the screen back.
        # Tight 4 rows + header so the section doesn't overflow short terminals.
        if s.get("requests_handled", 0) == 0:
            y += 1
            self._put(stdscr, y, lbl_col, "TRY", self._dim())
            self._put(
                stdscr, y, lbl_col + 4,
                "(in Claude Code — the /cerver skill is installed)",
                self._dim(),
            )
            y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 1
            # Wider command column so descriptions align cleanly and don't
            # collide with the longest verb. Right edge guarded against the
            # actual terminal width `w`, not the narrow `bar_w` hline.
            cmd_col = lbl_col + 2
            desc_col = cmd_col + 36  # 36 = len longest cmd ('/cerver move <session> <compute>') + 2 gap
            tips = (
                ('/cerver run "<prompt>"',            "send a prompt to this machine"),
                ('/cerver compare "<prompt>"',        "same prompt → claude + codex"),
                ('/cerver computes',                   "list your registered computes"),
                ('/cerver move <session> <compute>',  "move a live session"),
                ('/cerver help',                       "all verbs"),
            )
            for cmd, desc in tips:
                self._put(stdscr, y, lbl_col, "▸", self._dim())
                self._put(stdscr, y, cmd_col, cmd, self._bold())
                if desc_col + len(desc) < w - 1:
                    self._put(stdscr, y, desc_col, desc, self._dim())
                y += 1

        self._draw_tab_footer(stdscr, h, w, col, lbl_col, bar_w, current="connect")

    # ── provision view ───────────────────────────────────────────────
    # Cerver-side compute identity: the full compute_id (which is what
    # other tools — kompany, the dashboard, `cerver computes` — use to
    # reference this machine), the human label, the provider type, and
    # the gateway connection state. Read-only — provisioning state is
    # managed by the gateway, not by the user typing into this panel.

    def _draw_provision(self, stdscr, h, w):
        s = self.state
        col = 2
        lbl_col = 4
        val_col = 20
        bar_w = min(80, w - 4)
        y = 1
        self._provision_scroll_offset = min(self._provision_scroll_offset, 40)

        # Header — same animated logo treatment as Connect/Runtime.
        ver = self._version_label(s)
        subtitle_text = "Cerver Provision"
        if w >= LOGO_WIDTH + 6:
            self._draw_animated_logo(stdscr, y, col)
            y += LOGO_HEIGHT
            self._put(stdscr, y, col + LOGO_WIDTH - len(subtitle_text), subtitle_text, self._bold() | self._green())
            if ver:
                self._put(stdscr, y + 1, col + LOGO_WIDTH - len(ver), ver, self._dim())
                y += 2
            else:
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2
        else:
            self._put(stdscr, y, col, subtitle_text, self._bold() | self._green())
            y += 1
            if ver:
                self._put(stdscr, y, col, ver, self._dim())
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2

        y -= self._provision_scroll_offset
        if self._provision_scroll_offset > 0:
            self._put(stdscr, 1, max(0, w - 18), f"↑ {self._provision_scroll_offset}", self._dim())

        # Global quit confirmation. Key handling already sets
        # quit_prompt from any tab; Provision needs to render it too so
        # pressing Q does not look like a no-op.
        if s.get("quit_prompt") == "pending":
            self._draw_quit_prompt(stdscr, y, col, bar_w)
            return

        # COMPUTE section — the identity the gateway sees.
        self._put(stdscr, y, lbl_col, "COMPUTE", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        cerver_compute_id = s.get("cerver_compute_id")
        cerver_compute_label = s.get("cerver_compute_label") or s.get("machine_name")
        # Full compute_id, not truncated — that's the whole point of
        # this view. If we're still registering, surface the in-flight
        # state instead of a fake id.
        self._put(stdscr, y, lbl_col, "Compute ID", self._dim())
        if isinstance(cerver_compute_id, str) and cerver_compute_id and not cerver_compute_id.startswith("error:"):
            self._put(stdscr, y, val_col, cerver_compute_id, self._bold() | self._cyan())
        elif isinstance(cerver_compute_id, str) and cerver_compute_id.startswith("error:"):
            self._put(stdscr, y, val_col, cerver_compute_id, self._red() | self._bold())
        else:
            self._put(stdscr, y, val_col, "Registering…", self._yellow())
        y += 1

        self._put(stdscr, y, lbl_col, "Label", self._dim())
        self._put(stdscr, y, val_col, cerver_compute_label or "—", self._bold())
        y += 1

        # Provider: where this compute physically lives. For local-relay
        # mounts it's always "cerver_local_provider"; managed providers
        # (vercel / e2b / modal / cloudflare) get their own value once
        # the relay supports them as Provision targets.
        provider = s.get("cerver_compute_provider") or "cerver_local_provider"
        self._put(stdscr, y, lbl_col, "Provider", self._dim())
        self._put(stdscr, y, val_col, provider, self._bold())
        y += 1

        if s.get("user_email"):
            self._put(stdscr, y, lbl_col, "Owner", self._dim())
            self._put(stdscr, y, val_col, s["user_email"], self._bold())
            y += 1

        y += 1
        # Installed — inventory of tools available on this compute. The
        # cerver CLI is probed by filesystem; AI CLIs come from
        # cli_providers populated by relay_client. Auth state surfaces
        # alongside install state so "installed but not signed in" is
        # visible at a glance. Verbose mode appends path/auth detail.
        self._put(stdscr, y, lbl_col, "INSTALLED", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        cli_path = os.path.expanduser("~/.cerver/bin/cerver")
        self._put(stdscr, y, lbl_col, "cerver CLI", self._dim())
        if os.access(cli_path, os.X_OK):
            self._put(stdscr, y, val_col, "●", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, "Installed")
        else:
            self._put(stdscr, y, val_col, "●", self._dim())
            self._put(stdscr, y, val_col + 2, "Not installed")
        y += 1
        if self._verbose:
            self._put(stdscr, y, val_col, cli_path, self._dim())
            y += 1

        # Infisical CLI — separate from "Infisical configured" which
        # means env vars are wired up. Two distinct concerns: binary
        # present, vs. relay can fetch secrets. A yellow ● flags the
        # "configured but no binary" case so users still see the local
        # CLI is missing even when the relay itself works.
        infisical_bin = shutil.which("infisical")
        infisical_configured = bool(
            os.environ.get("INFISICAL_TOKEN") and os.environ.get("INFISICAL_PROJECT_ID")
        )
        self._put(stdscr, y, lbl_col, "Infisical CLI", self._dim())
        if infisical_bin:
            self._put(stdscr, y, val_col, "●", self._green() | self._bold())
            suffix = "  ·  configured" if infisical_configured else ""
            self._put(stdscr, y, val_col + 2, f"Installed{suffix}")
        elif infisical_configured:
            self._put(stdscr, y, val_col, "●", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Configured (CLI not installed)")
        else:
            self._put(stdscr, y, val_col, "●", self._dim())
            self._put(stdscr, y, val_col + 2, "Not installed")
        y += 1
        if self._verbose and infisical_bin:
            self._put(stdscr, y, val_col, infisical_bin, self._dim())
            y += 1

        # Go — required by `cerver update` (which shells out to
        # `go install …@latest`). Surfaces install state so a failed
        # Update CLI action has obvious context. Probe the same well-
        # known toolchain dirs the CLI's findGo() walks, since the
        # relay process's PATH may not have /opt/homebrew/bin etc.
        go_bin = shutil.which("go")
        if not go_bin:
            for cand in (
                "/opt/homebrew/bin/go",
                "/usr/local/bin/go",
                "/usr/local/go/bin/go",
                os.path.expanduser("~/go/bin/go"),
                os.path.expanduser("~/.asdf/shims/go"),
                os.path.expanduser("~/.local/bin/go"),
            ):
                if os.path.exists(cand) and os.access(cand, os.X_OK):
                    go_bin = cand
                    break
        self._put(stdscr, y, lbl_col, "Go", self._dim())
        if go_bin:
            self._put(stdscr, y, val_col, "●", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, "Installed")
        else:
            self._put(stdscr, y, val_col, "●", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Not installed (needed for Update CLI)")
        y += 1
        if self._verbose and go_bin:
            self._put(stdscr, y, val_col, go_bin, self._dim())
            y += 1

        default = s.get("default_cli", "")
        for name, p in (s.get("cli_providers") or {}).items():
            display = p.get("display_name") or name
            label = f"{display} *" if name == default else display
            self._put(stdscr, y, lbl_col, label, self._dim())
            if p.get("installed"):
                method = p.get("auth_method") or "none"
                if p.get("authenticated"):
                    suffix = f"  ·  {method}" if method != "none" else ""
                    self._put(stdscr, y, val_col, "●", self._green() | self._bold())
                    self._put(stdscr, y, val_col + 2, f"Installed{suffix}")
                else:
                    self._put(stdscr, y, val_col, "●", self._yellow() | self._bold())
                    self._put(stdscr, y, val_col + 2, "Installed · not signed in")
                if self._verbose:
                    detail = p.get("auth_detail") or p.get("path") or ""
                    if detail:
                        y += 1
                        self._put(stdscr, y, val_col, detail[: max(0, w - val_col - 2)], self._dim())
            else:
                self._put(stdscr, y, val_col, "●", self._dim())
                self._put(stdscr, y, val_col + 2, "Not installed")
            y += 1
        y += 1

        # DISCOVERED — output of the agent_environment.probe() that
        # ran at startup. Shows the user exactly which `codex`, `node`,
        # `claude`, etc. the relay will hand to agent subprocesses.
        # If any REQUIRED binary is missing, surface it loudly in red —
        # that's the failure mode the user otherwise only finds out
        # about when an agent silently exits non-zero.
        agent_env = s.get("agent_env") or {}
        bins = agent_env.get("binaries") or {}
        missing = agent_env.get("missing_required") or []
        if bins or missing:
            self._put(stdscr, y, lbl_col, "DISCOVERED", self._dim())
            y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 1

            # Show the binaries that matter most to a running session
            # first; everything else gets a one-line summary below.
            primary = ["node", "codex", "claude", "git", "bash"]
            shown: set[str] = set()
            for name in primary:
                if name in bins:
                    self._put(stdscr, y, lbl_col, name, self._dim())
                    path = bins[name]
                    self._put(stdscr, y, val_col, path[: max(0, w - val_col - 2)])
                    y += 1
                    shown.add(name)
                elif name in missing:
                    self._put(stdscr, y, lbl_col, name, self._dim())
                    self._put(stdscr, y, val_col, "● MISSING — agent spawn will fail", self._red() | self._bold())
                    y += 1

            extras = sorted(n for n in bins.keys() if n not in shown)
            if extras:
                self._put(stdscr, y, lbl_col, "also", self._dim())
                detail = ", ".join(extras)
                self._put(stdscr, y, val_col, detail[: max(0, w - val_col - 2)], self._dim())
                y += 1
            if not agent_env.get("shell_path_captured"):
                self._put(stdscr, y, lbl_col, "", self._dim())
                self._put(
                    stdscr,
                    y,
                    val_col,
                    "(login-shell PATH not captured — only well-known dirs probed)",
                    self._dim(),
                )
                y += 1
            y += 1

        # MAINTENANCE — focusable action rows for cerver-side upkeep.
        # First action: in-place upgrade of the local `cerver` CLI by
        # spawning `cerver update` as a subprocess. Output streams into
        # the relay's stdout (captured by LogCapture and visible on the
        # Logs tab); a short status surfaces inline below.
        self._put(stdscr, y, lbl_col, "MAINTENANCE", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        focused_update = self._focused_field_key() == "update_cli"
        rev_update = curses.A_REVERSE if focused_update else 0
        self._put(stdscr, y, lbl_col, "Update CLI", self._dim() | rev_update)
        upd_state = s.get("cerver_update_state", "idle")
        upd_msg = s.get("cerver_update_last_msg", "")
        if upd_state == "running":
            self._put(stdscr, y, val_col, "●", self._yellow() | self._bold() | rev_update)
            self._put(stdscr, y, val_col + 2, "Updating…  (see Logs tab)", rev_update)
        elif upd_state == "success":
            self._put(stdscr, y, val_col, "●", self._green() | self._bold() | rev_update)
            self._put(stdscr, y, val_col + 2, upd_msg or "Updated · Enter to re-run", rev_update)
        elif upd_state == "failed":
            self._put(stdscr, y, val_col, "●", self._red() | self._bold() | rev_update)
            self._put(stdscr, y, val_col + 2, (upd_msg or "Failed")[: max(0, bar_w - val_col - 4)], rev_update)
        else:
            self._put(stdscr, y, val_col, "Press Enter to upgrade in place", self._dim() | rev_update)
        y += 1

        # Startup service — the common source of "the relay starts and
        # drops" confusion is a launchd-managed background relay already
        # holding port 18081 while the user starts another foreground
        # relay. Keep the control near Provision because it affects how
        # this compute is brought online.
        ld = s.get("launchd")
        if ld is not None:
            focused_ld = self._focused_field_key() == "launchd"
            rev_ld = curses.A_REVERSE if focused_ld else 0
            pid = s.get("launchd_pid")
            self._put(stdscr, y, lbl_col, "Startup service", self._dim() | rev_ld)
            if ld == "running":
                self._put(stdscr, y, val_col, "●", self._green() | self._bold() | rev_ld)
                msg = f"launchd running" + (f" · pid {pid}" if pid else "")
                self._put(stdscr, y, val_col + 2, msg[: max(0, w - val_col - 4)], rev_ld)
                y += 1
                self._put(
                    stdscr,
                    y,
                    val_col,
                    "Enter stops/removes it before you run a manual relay.",
                    self._dim() | rev_ld,
                )
            elif ld == "installed":
                self._put(stdscr, y, val_col, "●", self._yellow() | self._bold() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Installed, not running · Enter to remove", rev_ld)
            elif ld == "error":
                self._put(stdscr, y, val_col, "●", self._red() | self._bold() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Error · check Logs", rev_ld)
            else:
                self._put(stdscr, y, val_col, "●", self._dim() | rev_ld)
                self._put(stdscr, y, val_col + 2, "Not installed · Enter to enable auto-start", rev_ld)
            y += 1

        # About panel — explains the focused row when one is selected.
        y += 1
        y = self._draw_field_help(stdscr, y, col, bar_w)

        self._draw_tab_footer(stdscr, h, w, col, lbl_col, bar_w, current="provision")

    # ── network view ────────────────────────────────────────────────

    def _draw_network(self, stdscr, h, w):
        s = self.state
        col = 2
        lbl_col = 4
        bar_w = min(110, w - 4)
        y = 1

        ver = self._version_label(s)
        subtitle_text = "Cerver Network"
        if w >= LOGO_WIDTH + 6:
            self._draw_animated_logo(stdscr, y, col)
            y += LOGO_HEIGHT
            self._put(stdscr, y, col + LOGO_WIDTH - len(subtitle_text), subtitle_text, self._bold() | self._green())
            if ver:
                self._put(stdscr, y + 1, col + LOGO_WIDTH - len(ver), ver, self._dim())
                y += 2
            else:
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2
        else:
            self._put(stdscr, y, col, subtitle_text, self._bold() | self._green())
            y += 1
            if ver:
                self._put(stdscr, y, col, ver, self._dim())
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2

        if s.get("quit_prompt") == "pending":
            self._draw_quit_prompt(stdscr, y, col, bar_w)
            return

        rows = s.get("cerver_network_computes") or []
        if not isinstance(rows, list):
            rows = []
        connected = sum(1 for r in rows if self._compute_is_connected(r))
        private_rows = [r for r in rows if str(r.get("scope") or "private").lower() == "private"]
        updated = self._format_relative_time(s.get("cerver_network_updated_at"))
        error = s.get("cerver_network_error")

        self._put(stdscr, y, lbl_col, "PRIVATE NETWORK", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1
        summary = f"{connected}/{len(rows)} connected"
        if private_rows:
            summary += f"  ·  {len(private_rows)} private"
        if updated != "—":
            summary += f"  ·  refreshed {updated}"
        self._put(stdscr, y, lbl_col, summary, self._bold())
        y += 1
        if error:
            self._put(stdscr, y, lbl_col, f"Gateway: {error}", self._red())
        else:
            self._put(stdscr, y, lbl_col, "Computes connected to this account, including offline machines.", self._dim())
        y += 2

        headers = (("STATUS", 14), ("LABEL", 24), ("PROVIDER", 24), ("SCOPE", 10), ("ID", 22))
        x = lbl_col
        for title, width in headers:
            self._put(stdscr, y, x, title, self._dim() | self._bold())
            x += width
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        visible_h = max(1, h - y - 4)
        max_offset = max(0, len(rows) - visible_h)
        self._network_scroll_offset = min(self._network_scroll_offset, max_offset)
        start = self._network_scroll_offset
        visible_rows = rows[start:start + visible_h]
        if not visible_rows:
            self._put(stdscr, y, lbl_col, "No computes returned yet.", self._dim())
        for row in visible_rows:
            connected_row = self._compute_is_connected(row)
            status = str(row.get("status") or "unknown")
            status_text = "connected" if connected_row else status
            status_attr = self._green() | self._bold() if connected_row else self._dim()
            label = str(row.get("label") or row.get("name") or "—")
            provider = str(row.get("provider") or "—")
            scope = str(row.get("scope") or "private")
            compute_id = str(row.get("compute_id") or row.get("id") or "—")
            if compute_id == s.get("cerver_compute_id"):
                label = f"{label} *"
            x = lbl_col
            for value, width, attr in (
                (status_text, 14, status_attr),
                (label, 24, self._bold()),
                (provider, 24, self._dim()),
                (scope, 10, self._dim()),
                (compute_id, 22, self._cyan() if connected_row else self._dim()),
            ):
                self._put(stdscr, y, x, self._clip(value, width - 1), attr)
                x += width
            y += 1

        if len(rows) > visible_h:
            pct = int(((start + len(visible_rows)) / max(len(rows), 1)) * 100)
            self._put(stdscr, 1, col + bar_w - 8, f"  {pct:3d}%", self._dim())

        self._draw_tab_footer(stdscr, h, w, col, lbl_col, bar_w, current="network")

    # ── runtime view ─────────────────────────────────────────────────
    # Workload (agents, workflows, requests) and the underlying compute
    # resources (CPU / memory / load / disk). Sibling of Connect; shares
    # the same animated header but skips identity/setup since those are
    # static during a session.

    def _draw_runtime(self, stdscr, h, w):
        s = self.state
        col = 2
        lbl_col = 4
        val_col = 20
        bar_w = min(50, w - 4)
        y = 1

        # Header — same logo treatment as Connect, but subtitle reads
        # "Cerver Runtime" so the active tab is unambiguous even before
        # the user looks at the bottom bar.
        ver = self._version_label(s)
        subtitle_text = "Cerver Runtime"
        if w >= LOGO_WIDTH + 6:
            self._draw_animated_logo(stdscr, y, col)
            y += LOGO_HEIGHT
            # Subtitle bumped to bold green (was dim) — keeps the active
            # tab name legible next to the logo. Version on its own dim
            # line below to free up emphasis.
            self._put(stdscr, y, col + LOGO_WIDTH - len(subtitle_text), subtitle_text, self._bold() | self._green())
            if ver:
                self._put(stdscr, y + 1, col + LOGO_WIDTH - len(ver), ver, self._dim())
                y += 2
            else:
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2
        else:
            self._put(stdscr, y, col, subtitle_text, self._bold() | self._green())
            y += 1
            if ver:
                self._put(stdscr, y, col, ver, self._dim())
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2

        # Machine identity moved to the Provision tab — Runtime is just
        # the work-side config now: Home (where it runs) and AI CLI
        # (what runs it), both focusable via Up/Down/Enter.

        # Home — editable working directory for this relay.
        focused_home = self._focused_field_key() == "home"
        rev_home = curses.A_REVERSE if focused_home else 0
        self._put(stdscr, y, lbl_col, "Home", self._dim() | rev_home)
        if self._editing_home:
            field_w = max(30, bar_w - val_col + col)
            display = self._home_input[:field_w]
            self._put(stdscr, y, val_col, display, self._bold() | self._cyan())
            cursor_x = val_col + min(self._home_cursor, field_w)
            if 0 <= cursor_x < w - 1:
                try:
                    curses.curs_set(1)
                    stdscr.move(y, cursor_x)
                except curses.error:
                    pass
            self._put(stdscr, y + 1, val_col, "Enter to save, Esc to cancel", self._dim())
            y += 2
        else:
            home_val = s.get("home_dir", "—")
            self._put(stdscr, y, val_col, home_val, self._bold() | rev_home)
            y += 1

        # AI CLI — default provider for `cerver run` / `cerver compare`.
        providers = s.get("cli_providers", {})
        default_cli = s.get("default_cli", "claude")
        default_provider = providers.get(default_cli, {})
        cli_display = default_provider.get("display_name", default_cli.title())
        cli_authed = default_provider.get("authenticated", False)
        focused_cli = self._focused_field_key() == "cli"
        rev_cli = curses.A_REVERSE if focused_cli else 0
        self._put(stdscr, y, lbl_col, "AI CLI", self._dim() | rev_cli)
        self._put(stdscr, y, val_col, cli_display, self._bold() | rev_cli)
        dot_x = val_col + len(cli_display) + 1
        if cli_authed:
            self._put(stdscr, y, dot_x, "●", self._green() | rev_cli)
        else:
            self._put(stdscr, y, dot_x, "○", self._red() | rev_cli)
        y += 1

        # Contextual help — explains whatever row is currently focused
        # (Home or AI CLI on Runtime). Same panel shape as Connect.
        y += 1
        y = self._draw_field_help(stdscr, y, col, bar_w)
        y += 1

        # Workload
        self._put(stdscr, y, lbl_col, "WORKLOAD", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        agent_counts = s.get("agent_counts", {})
        running = int(agent_counts.get("running", 0) or 0)
        paused = int(agent_counts.get("paused", 0) or 0)
        prepared = int(agent_counts.get("prepared", 0) or 0)
        total_live = running + paused + prepared
        self._put(stdscr, y, lbl_col, "Live agents", self._dim())
        # Lead with the running count in bold so the actual live load is
        # one glance — "how many CLIs are this machine driving right now?"
        # — instead of buried in a three-up split where everyone reads
        # the first number ("0 run") and concludes nothing's happening.
        running_attr = self._bold() | (self._green() if running > 0 else 0)
        self._put(stdscr, y, val_col, f"{running}", running_attr)
        self._put(
            stdscr,
            y,
            val_col + max(2, len(str(running)) + 1),
            f"running  ·  {paused} paused  ·  {prepared} ready  ·  {total_live} total",
            self._dim(),
        )
        y += 1
        # When the local HTTP server is down, the stats come from the
        # in-process manager fallback — say so, so a stale "0 running"
        # number can't be mistaken for ground truth.
        if not s.get("server_running"):
            self._put(stdscr, y, lbl_col, "", self._dim())
            self._put(
                stdscr,
                y,
                val_col,
                "(local HTTP down — counts read directly from agent manager)",
                self._dim(),
            )
            y += 1

        # Per-agent rows — one line each. ↑/↓ on the Runtime tab cycles
        # through fields ([Home, AI CLI]) and then into these rows;
        # whichever row is focused renders in inverse video so the user
        # sees what they'd act on (Enter is not wired to anything yet —
        # tail/kill/peek is the natural next step).
        #
        # session_w is the wider bound for the session list — the
        # rest of the Runtime tab uses bar_w=50 for the narrow stat
        # column, but a session row with cli/agent/status/age/detail
        # needs ~110 cols to read comfortably. We let it expand here
        # without disturbing the surrounding layout.
        session_w = min(110, max(50, w - 4))
        rows = s.get("agent_rows") or []
        if rows:
            # Header row, dimmed.
            self._put(stdscr, y, lbl_col, "Local agents", self._dim())
            y += 1
            self._put(stdscr, y, lbl_col + 2, "CLI", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 12, "AGENT", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 23, "STATUS", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 34, "AGE", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 44, "DETAIL", self._dim() | self._bold())
            y += 1
            for row in rows:
                if not isinstance(row, dict):
                    continue
                aid = str(row.get("id") or "")
                focused = self._focused_field_key() == f"session_{aid}"
                rev = curses.A_REVERSE if focused else 0
                status = str(row.get("status") or "?")
                cli = str(row.get("cli_tool") or "—")
                # Status dot color so a glance tells you who's actively
                # working vs idle vs broken.
                status_color = {
                    "running": self._green() | self._bold(),
                    "paused":  self._cyan(),
                    "ready":   self._yellow(),
                    "prepared": self._dim(),
                    "completed": self._dim(),
                    "failed":  self._red() | self._bold(),
                    "stopped": self._dim(),
                }.get(status, self._dim())
                self._put(stdscr, y, lbl_col, "●", status_color | rev)
                self._put(stdscr, y, lbl_col + 2, cli[:8], self._bold() | rev)
                self._put(stdscr, y, lbl_col + 12, aid[:10], self._dim() | rev)
                self._put(stdscr, y, lbl_col + 23, status[:10], rev)
                age = self._format_relative_time(row.get("created_at"))
                self._put(stdscr, y, lbl_col + 34, age[:10], self._dim() | rev)
                # DETAIL: session_id short form (so the user can
                # cross-reference with `cerver sessions`) plus a tiny
                # idle-since for paused/ready rows. Truncate to fit.
                sid = str(row.get("session_id") or "")
                detail_parts = []
                if sid:
                    detail_parts.append(f"sid:{sid[:8]}")
                if status in ("paused", "ready"):
                    idle = self._format_relative_time(row.get("last_activity"))
                    detail_parts.append(f"idle {idle}")
                detail = "  ·  ".join(detail_parts)
                detail_w = max(0, session_w - (lbl_col + 44))
                self._put(stdscr, y, lbl_col + 44, detail[:detail_w], self._dim() | rev)
                y += 1
            y += 1
        else:
            self._put(stdscr, y, lbl_col, "Local agents", self._dim())
            self._put(stdscr, y, val_col, "none running on this relay", self._dim())
            y += 2

        cerver_rows = s.get("cerver_session_rows") or []
        self._put(stdscr, y, lbl_col, "Cerver sessions", self._dim())
        y += 1
        if cerver_rows:
            self._put(stdscr, y, lbl_col + 2, "CLI", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 12, "SESSION", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 25, "STATUS", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 36, "UPDATED", self._dim() | self._bold())
            self._put(stdscr, y, lbl_col + 48, "NAME", self._dim() | self._bold())
            y += 1
            for row in cerver_rows:
                if y >= h - 8 or not isinstance(row, dict):
                    break
                sid = str(row.get("sessionId") or row.get("session_id") or "")
                focused = self._focused_field_key() == f"cerver_session_{sid}"
                rev = curses.A_REVERSE if focused else 0
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                cli = str(metadata.get("cli_tool") or row.get("harness") or "—")
                status = str(row.get("status") or "?")
                status_color = {
                    "running": self._green() | self._bold(),
                    "ready": self._yellow(),
                    "idle": self._yellow(),
                    "completed": self._dim(),
                    "failed": self._red() | self._bold(),
                    "terminated": self._dim(),
                }.get(status, self._dim())
                self._put(stdscr, y, lbl_col, "●", status_color | rev)
                self._put(stdscr, y, lbl_col + 2, cli[:8], self._bold() | rev)
                self._put(stdscr, y, lbl_col + 12, sid[:10], self._dim() | rev)
                self._put(stdscr, y, lbl_col + 25, status[:10], status_color | rev)
                self._put(stdscr, y, lbl_col + 36, self._format_relative_time(row.get("updatedAt"))[:10], self._dim() | rev)
                name = str(row.get("sessionName") or row.get("task") or row.get("title") or "")
                name_w = max(0, session_w - (lbl_col + 48))
                self._put(stdscr, y, lbl_col + 48, name[:name_w], self._dim() | rev)
                y += 1
            y += 1
        else:
            self._put(stdscr, y, val_col, "no recent sessions for this compute", self._dim())
            y += 2

        workflow_counts = (s.get("workflow_summary") or {}).get("counts", {})
        self._put(stdscr, y, lbl_col, "Workflows", self._dim())
        self._put(
            stdscr,
            y,
            val_col,
            f"{workflow_counts.get('running', 0)} run  {workflow_counts.get('paused', 0)} paused",
        )
        y += 1

        self._put(stdscr, y, lbl_col, "Uptime", self._dim())
        self._put(stdscr, y, val_col, self._format_uptime())
        y += 1

        self._put(stdscr, y, lbl_col, "Requests", self._dim())
        self._put(stdscr, y, val_col, str(s.get("requests_handled", 0)))
        y += 2

        # Compute
        self._put(stdscr, y, lbl_col, "COMPUTE", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        compute = s.get("compute", {})
        self._put(stdscr, y, lbl_col, "Overall", self._dim())
        health_text, health_attr = self._format_compute_health(compute)
        self._put(stdscr, y, val_col, health_text, health_attr)
        y += 1

        self._put(stdscr, y, lbl_col, "CPU", self._dim())
        self._put(stdscr, y, val_col, self._format_metric_line("cpu", compute.get("cpu_percent"), 100))
        y += 1

        self._put(stdscr, y, lbl_col, "Memory", self._dim())
        self._put(stdscr, y, val_col, self._format_metric_line("memory", (compute.get("memory", {}) or {}).get("percent"), 100))
        y += 1

        self._put(stdscr, y, lbl_col, "Load", self._dim())
        load_pct = (compute.get("load", {}) or {}).get("normalized_percent")
        self._put(stdscr, y, val_col, self._format_metric_line("load", load_pct, 100, suffix=self._format_load(compute)))
        y += 1

        self._put(stdscr, y, lbl_col, "Disk", self._dim())
        self._put(stdscr, y, val_col, self._format_metric_line("disk", (compute.get("disk", {}) or {}).get("percent"), 100, invert_label=True, suffix=self._format_disk_free(compute.get("disk", {}))))
        y += 1
        y += 1


        # Recent log lines
        self._put(stdscr, y, lbl_col, "RECENT", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        recent = self._stdout_capture.get_lines(3)
        for line in recent:
            if y >= h - 3:
                break
            self._put(stdscr, y, lbl_col, line[:bar_w], self._dim())
            y += 1

        self._draw_tab_footer(stdscr, h, w, col, lbl_col, bar_w, current="runtime")

    # ── help view ────────────────────────────────────────────────────
    # Static reference page: what cerver is wired to (Infisical),
    # what the CLI verbs do (`run`, `compare`, `computes`, …), and
    # the TUI keybindings that aren't already discoverable in the
    # footer. Aimed at users who haven't read the docs — terse and
    # action-oriented. Lives as a third main tab so it's reachable
    # in two keystrokes from anywhere.

    def _draw_help(self, stdscr, h, w):
        s = self.state
        col = 2
        lbl_col = 4
        bar_w = min(w - 4, 100)
        y = 1

        # Title row — same header treatment as Connect/Runtime so the
        # tab swap is visually consistent. No animated logo here; this
        # page is static and the logo eats vertical room help needs.
        self._put(stdscr, y, col, "Cerver Help", self._bold())
        ver = self._version_label(s)
        if ver:
            self._put(stdscr, y, col + bar_w - len(ver) - 2, ver, self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 2

        # ── Section helpers ─────────────────────────────────────
        # Keep section rendering compact since the page is content-
        # heavy and the terminal might be short. Each section: bold
        # header, hline, dim body. Bail out if we run off the bottom.
        cmd_col = lbl_col + 2
        desc_col = cmd_col + 34   # widest cmd: `cerver compare --clis a,b,c "..."`

        def section(title):
            nonlocal y
            if y >= h - 4:
                return False
            self._put(stdscr, y, lbl_col, title, self._dim() | self._bold())
            y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 1
            return True

        def row(cmd, desc):
            nonlocal y
            if y >= h - 3:
                return
            self._put(stdscr, y, cmd_col, cmd, self._bold())
            if desc_col + len(desc) < w - 1:
                self._put(stdscr, y, desc_col, desc, self._dim())
            y += 1

        def blank():
            nonlocal y
            y += 1

        # ── Infisical ───────────────────────────────────────────
        if section("INFISICAL — your secrets vault"):
            row("infisical login", "browser OAuth, stores session in ~/.infisical")
            row("infisical secrets", "list keys cerver will inject into agents")
            row("infisical run -- cmd", "run any command with vault env vars injected")
            blank()
            self._put(stdscr, y, cmd_col, "Cerver wraps the relay with `infisical run` so spawned", self._dim())
            y += 1
            self._put(stdscr, y, cmd_col, "agents (claude/codex/grok) get your API keys at runtime.", self._dim())
            y += 1
            self._put(stdscr, y, cmd_col, "Skipped Infisical? Relay reads keys from process env.", self._dim())
            y += 1
            blank()

        # ── Cerver CLI verbs ────────────────────────────────────
        if section("CERVER — agent runs on any compute"):
            row("cerver run \"<prompt>\"", "single agent on this machine")
            row("cerver run --on <compute>", "pick a registered compute by name")
            row("cerver run --cli codex", "claude (default), codex, or grok")
            row("cerver run --bill api", "bill via API keys instead of subscription")
            blank()
            row("cerver compare \"<prompt>\"", "claude + codex side-by-side")
            row("cerver compare --clis claude,codex,grok \"…\"", "three-way (needs vault)")
            blank()
            row("cerver computes", "list your registered computes")
            row("cerver sessions", "recent agent sessions")
            row("cerver move <session> <compute>", "migrate a live session")
            row("cerver login", "re-bootstrap auth (and Infisical)")
            blank()

        # ── TUI keybindings ─────────────────────────────────────
        if section("KEYBINDINGS"):
            row("[1] / [2] / [3] / [4] / [5] / [6]", "Connect / Provision / Network / Runtime / Logs / Help")
            row("← / →", "cycle tabs")
            row("[L]", "logs (toggle, same as [5])")
            row("[V]", "verbose mode on this tab")
            row("[Q]", "quit")
            blank()
            row("Connect only:", "")
            row("  [N] / [H]", "edit machine name / home dir")
            row("  [S]", "install/uninstall launchd autostart")
            row("  [C]", "pick default AI CLI")
            row("  [D]", "logout")
            blank()

        # ── Links ───────────────────────────────────────────────
        if section("LINKS"):
            row("docs", "https://cerver.ai/docs")
            row("dashboard", s.get("dashboard_url") or "http://localhost:18081/")
            row("infisical", "https://app.infisical.com")

        self._draw_tab_footer(stdscr, h, w, col, lbl_col, bar_w, current="help")

    # ── shared tab footer ────────────────────────────────────────────
    # Pulled into a helper so all three main tabs show the same
    # navigation strip with a bracket+green on the active tab.
    # Connect-specific action keys ([N]/[H]/[S]/[C]/[D]) are appended
    # only when current == "connect" since those handlers ignore other
    # views anyway.

    def _draw_tab_footer(self, stdscr, h, w, col, lbl_col, bar_w, current):
        footer_y = h - 2
        self._hline(stdscr, footer_y - 1, col, bar_w)
        x = lbl_col

        # Tab nav. Active tab uses inverse-video (A_REVERSE) so it
        # matches the row-highlight style used inside the panels —
        # consistent affordance for "this is the selected item." ←→
        # also cycles (handler routes those into the matching _view
        # assignment). Logs is a peer tab now — [L] still works as a
        # shortcut but arrow nav reaches it too.
        for key_label, view_name, display in (
            ("[1]", "connect", "Connect"),
            ("[2]", "provision", "Provision"),
            ("[3]", "network", "Network"),
            ("[4]", "runtime", "Runtime"),
            ("[5]", "logs", "Logs"),
            ("[6]", "help", "Help"),
        ):
            self._put(stdscr, footer_y, x, key_label, self._cyan() | self._bold())
            is_active = (current == view_name)
            if is_active:
                attr = self._bold() | curses.A_REVERSE
            else:
                attr = self._dim()
            label = f" {display} " if is_active else display
            self._put(stdscr, footer_y, x + 4, label, attr)
            x += 4 + len(label) + 2

    def _draw_animated_logo(self, stdscr, y, col):
        """Draw the logo as a rain-on-water surface. Random raindrops
        spawn at impact points and ripple outward; spawn rate scales with
        the count of running agents so the logo gets visibly more
        animated when the relay is doing work."""
        h, w = stdscr.getmaxyx()
        num_levels = len(GRADIENT_COLORS)
        running = (self.state.get("agent_counts") or {}).get("running", 0)
        for i, line in enumerate(LOGO):
            row_y = y + i
            if row_y >= h:
                break
            intensities = get_animated_attrs(self._anim_frame, len(line), row=i, workload=running)
            for cx, ch in enumerate(line):
                if ch == " ":
                    continue
                screen_x = col + cx
                if screen_x >= w - 1:
                    break
                val = intensities[cx] if cx < len(intensities) else 0.3
                # Map brightness to 8-level gradient (pairs 10–17)
                level = int(val * (num_levels - 1))
                level = max(0, min(num_levels - 1, level))
                attr = curses.color_pair(10 + level)
                if level >= num_levels - 2:
                    attr |= curses.A_BOLD
                try:
                    stdscr.addch(row_y, screen_x, ch, attr)
                except curses.error:
                    pass

    def _handle_onboarding_key(self, key, stdscr):
        """Handle keyboard input during onboarding."""
        if not self._onboarding_initialized:
            self._onboarding_input = self.state.get("home_dir", "")
            self._onboarding_cursor = len(self._onboarding_input)
            self._onboarding_initialized = True

        if key in (curses.KEY_ENTER, 10, 13):  # Enter — accept
            path = self._onboarding_input.strip()
            if path:
                expanded = os.path.expanduser(path)
                if os.path.isdir(expanded):
                    self.state["home_dir"] = expanded
                    self.state["onboarding_needed"] = False
                    if self._on_home_set:
                        self._on_home_set(expanded)
                    if stdscr:
                        curses.curs_set(0)
                    return
                # Directory doesn't exist — don't accept, stay in onboarding
            else:
                # Empty input — use current default
                self.state["onboarding_needed"] = False
                if self._on_home_set:
                    self._on_home_set(self.state.get("home_dir", ""))
                if stdscr:
                    curses.curs_set(0)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if self._onboarding_cursor > 0:
                self._onboarding_input = (
                    self._onboarding_input[: self._onboarding_cursor - 1]
                    + self._onboarding_input[self._onboarding_cursor :]
                )
                self._onboarding_cursor -= 1
        elif key == curses.KEY_LEFT:
            self._onboarding_cursor = max(0, self._onboarding_cursor - 1)
        elif key == curses.KEY_RIGHT:
            self._onboarding_cursor = min(len(self._onboarding_input), self._onboarding_cursor + 1)
        elif key == curses.KEY_HOME or key == 1:  # Ctrl-A
            self._onboarding_cursor = 0
        elif key == curses.KEY_END or key == 5:  # Ctrl-E
            self._onboarding_cursor = len(self._onboarding_input)
        elif 32 <= key <= 126:
            ch = chr(key)
            self._onboarding_input = (
                self._onboarding_input[: self._onboarding_cursor]
                + ch
                + self._onboarding_input[self._onboarding_cursor :]
            )
            self._onboarding_cursor += 1

    def _draw_onboarding(self, stdscr, y, col, bar_w):
        """Draw the first-run onboarding screen."""
        lbl_col = col + 2
        h, w = stdscr.getmaxyx()

        if not self._onboarding_initialized:
            self._onboarding_input = self.state.get("home_dir", "")
            self._onboarding_cursor = len(self._onboarding_input)
            self._onboarding_initialized = True

        self._put(stdscr, y, lbl_col, "Welcome! Let's set up your workspace.", self._bold())
        y += 2
        self._put(stdscr, y, lbl_col, "Where do your projects live?", self._dim())
        y += 1
        self._put(stdscr, y, lbl_col, "This is the parent folder containing your code.", self._dim())
        y += 2

        # Home dir input field
        self._put(stdscr, y, lbl_col, "Home:", self._dim())
        input_x = lbl_col + 7
        field_w = max(30, bar_w - input_x + col)
        display = self._onboarding_input[:field_w]
        self._put(stdscr, y, input_x, display, self._bold() | self._cyan())

        # Blinking cursor
        cursor_x = input_x + min(self._onboarding_cursor, field_w)
        if 0 <= cursor_x < w - 1:
            try:
                curses.curs_set(1)
                stdscr.move(y, cursor_x)
            except curses.error:
                pass
        y += 2

        # Validate and show status
        path = self._onboarding_input.strip()
        if path:
            expanded = os.path.expanduser(path)
            if os.path.isdir(expanded):
                self._put(stdscr, y, lbl_col, "Directory exists", self._green())
            else:
                self._put(stdscr, y, lbl_col, "Directory not found", self._red())
        y += 2

        # Footer
        self._hline(stdscr, y, col, bar_w)
        y += 1
        x = lbl_col
        self._put(stdscr, y, x, "[Enter]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 8, "Continue", self._dim())

    def _handle_launchd_prompt_key(self, key):
        """Handle keyboard input during launchd prompt."""
        if key in (curses.KEY_UP, ord("k")):
            self._launchd_selected = max(0, self._launchd_selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._launchd_selected = min(1, self._launchd_selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if self._on_launchd_install:
                self._on_launchd_install(self._launchd_selected == 0)
            else:
                self.state["launchd_prompt"] = "done"
        elif key == ord("1"):
            self._launchd_selected = 0
        elif key == ord("2"):
            self._launchd_selected = 1

    def _draw_launchd_prompt(self, stdscr, y, col, bar_w):
        """Draw the launchd install prompt screen."""
        lbl_col = col + 2

        self._put(stdscr, y, lbl_col, "Run on startup?", self._bold())
        y += 2
        self._put(stdscr, y, lbl_col, "The relay can auto-start when you log in", self._dim())
        y += 1
        self._put(stdscr, y, lbl_col, "and restart automatically if it crashes.", self._dim())
        y += 2

        options = ["Yes (recommended)", "No, just run manually"]
        for i, opt in enumerate(options):
            if i == self._launchd_selected:
                self._put(stdscr, y, lbl_col, f"  \u25b8 {i+1}.", self._cyan() | self._bold())
                self._put(stdscr, y, lbl_col + 7, opt, self._cyan() | self._bold())
            else:
                self._put(stdscr, y, lbl_col + 4, f"{i+1}.", self._dim())
                self._put(stdscr, y, lbl_col + 7, opt)
            y += 1
        y += 1

        self._hline(stdscr, y, col, bar_w)
        y += 1
        x = lbl_col
        self._put(stdscr, y, x, "[\u2191\u2193]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 5, "Select", self._dim())
        x += 14
        self._put(stdscr, y, x, "[Enter]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 8, "Confirm", self._dim())

    def _handle_quit_prompt_key(self, key):
        """Keyboard input while the quit confirmation modal is open.

        Enter on the current selection commits. Up/Down or 1/2 reposition
        the cursor. Y is a fast-path to confirm. N or Esc cancels. A
        second Q press also cancels — pressing Q again is more naturally
        "I changed my mind" than "yes, definitely".
        """
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            self._quit_selected = 0
        elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
            self._quit_selected = 1
        elif key == ord("1"):
            self._quit_selected = 0
        elif key == ord("2"):
            self._quit_selected = 1
        elif key in (ord("y"), ord("Y")):
            # Y is a hard yes regardless of cursor position.
            self.state["quit_prompt"] = None
            self._running = False
        elif key in (ord("n"), ord("N"), 27, ord("q"), ord("Q")):
            # N / Esc / second-Q all cancel.
            self.state["quit_prompt"] = None
        elif key in (curses.KEY_ENTER, 10, 13):
            if self._quit_selected == 0:
                self.state["quit_prompt"] = None
                self._running = False
            else:
                self.state["quit_prompt"] = None

    def _draw_quit_prompt(self, stdscr, y, col, bar_w):
        """Centered confirmation before the relay shuts down. Borrows
        the launchd-prompt layout so the visual vocabulary stays
        consistent across modals.
        """
        lbl_col = col + 2

        self._put(stdscr, y, lbl_col, "Quit the relay?", self._bold())
        y += 2
        self._put(stdscr, y, lbl_col, "Shutting down ends any in-flight agent runs", self._dim())
        y += 1
        self._put(stdscr, y, lbl_col, "and disconnects this compute from gateway.cerver.ai.", self._dim())
        y += 2

        options = ["Yes, quit", "No, keep running"]
        for i, opt in enumerate(options):
            if i == self._quit_selected:
                self._put(stdscr, y, lbl_col, f"  ▸ {i+1}.", self._cyan() | self._bold())
                self._put(stdscr, y, lbl_col + 7, opt, self._cyan() | self._bold())
            else:
                self._put(stdscr, y, lbl_col + 4, f"{i+1}.", self._dim())
                self._put(stdscr, y, lbl_col + 7, opt)
            y += 1
        y += 1

        self._hline(stdscr, y, col, bar_w)
        y += 1
        x = lbl_col
        self._put(stdscr, y, x, "[↑↓]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 5, "Select", self._dim())
        x += 14
        self._put(stdscr, y, x, "[Enter]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 8, "Confirm", self._dim())
        x += 18
        self._put(stdscr, y, x, "[Y/N]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 6, "Fast", self._dim())
        x += 12
        self._put(stdscr, y, x, "[Esc]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 6, "Cancel", self._dim())

    def _handle_cli_prompt_key(self, key):
        """Handle keyboard input during CLI selection prompt."""
        providers = self.state.get("cli_providers", {})
        installed = [n for n, p in providers.items() if p.get("installed")]
        not_installed = [n for n, p in providers.items() if not p.get("installed")]
        all_names = list(providers.keys())

        # --- API key input sub-mode ---
        if self._cli_auth_mode == "api_key":
            if key in (curses.KEY_ENTER, 10, 13):
                api_key = self._cli_api_key_input.strip()
                if api_key and self._on_cli_api_key:
                    name = (installed[self._cli_selected] if self._cli_selected < len(installed)
                            else all_names[self._cli_selected] if self._cli_selected < len(all_names)
                            else None)
                    if name:
                        self._on_cli_api_key(name, api_key)
                self._cli_auth_mode = None
                self._cli_api_key_input = ""
                self._cli_api_key_cursor = 0
                # Refresh provider status after key change
                if self._on_cli_refresh:
                    self._on_cli_refresh()
            elif key == 27:  # Escape — cancel
                self._cli_auth_mode = None
                self._cli_api_key_input = ""
                self._cli_api_key_cursor = 0
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if self._cli_api_key_cursor > 0:
                    self._cli_api_key_input = (
                        self._cli_api_key_input[: self._cli_api_key_cursor - 1]
                        + self._cli_api_key_input[self._cli_api_key_cursor :]
                    )
                    self._cli_api_key_cursor -= 1
            elif key == curses.KEY_LEFT:
                self._cli_api_key_cursor = max(0, self._cli_api_key_cursor - 1)
            elif key == curses.KEY_RIGHT:
                self._cli_api_key_cursor = min(len(self._cli_api_key_input), self._cli_api_key_cursor + 1)
            elif key == curses.KEY_HOME or key == 1:  # Ctrl-A
                self._cli_api_key_cursor = 0
            elif key == curses.KEY_END or key == 5:  # Ctrl-E
                self._cli_api_key_cursor = len(self._cli_api_key_input)
            elif 32 <= key <= 126:
                ch = chr(key)
                self._cli_api_key_input = (
                    self._cli_api_key_input[: self._cli_api_key_cursor]
                    + ch
                    + self._cli_api_key_input[self._cli_api_key_cursor :]
                )
                self._cli_api_key_cursor += 1
            return

        # --- Device auth display sub-mode ---
        if self._cli_auth_mode == "device_auth":
            if key in (curses.KEY_ENTER, 10, 13, 27):  # Enter or Escape
                self._cli_auth_mode = None
                self._cli_device_auth = None
                # Refresh provider auth status (user may have completed sign-in)
                if self._on_cli_refresh:
                    self._on_cli_refresh()
            return

        # --- Main CLI selection ---
        # Navigate across ALL providers (installed first, then not-installed)
        all_ordered = installed + not_installed
        if not all_ordered:
            self.state["cli_prompt"] = "done"
            return

        if key in (curses.KEY_UP, ord("k")):
            self._cli_selected = max(0, self._cli_selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._cli_selected = min(len(all_ordered) - 1, self._cli_selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if self._cli_selected < len(all_ordered):
                chosen = all_ordered[self._cli_selected]
                p = providers.get(chosen, {})
                if p.get("installed"):
                    self.state["default_cli"] = chosen
                    self.state["cli_prompt"] = "done"
                    if self._on_cli_set:
                        self._on_cli_set(chosen)
        elif key == 27:  # Escape — cancel
            self.state["cli_prompt"] = "done"
        elif key == ord("a") or key == ord("A"):
            # Enter API key for selected provider
            self._cli_auth_mode = "api_key"
            self._cli_api_key_input = ""
            self._cli_api_key_cursor = 0
        elif key == ord("s") or key == ord("S"):
            # Start device auth for selected provider (runs async in background thread)
            if self._cli_selected < len(all_ordered):
                name = all_ordered[self._cli_selected]
                if self._on_cli_device_auth:
                    self._on_cli_device_auth(name)
        elif key == ord("i") or key == ord("I"):
            # Install selected provider if not installed
            if self._cli_selected < len(all_ordered):
                name = all_ordered[self._cli_selected]
                p = providers.get(name, {})
                if not p.get("installed") and self._on_cli_install:
                    self._on_cli_install(name)

    def _draw_cli_prompt(self, stdscr, y, col, bar_w):
        """Draw the CLI provider selection screen with auth status."""
        lbl_col = col + 2
        h, w = stdscr.getmaxyx()
        providers = self.state.get("cli_providers", {})
        installed = [n for n, p in providers.items() if p.get("installed")]
        not_installed = [n for n, p in providers.items() if not p.get("installed")]

        # --- API key input sub-screen ---
        if self._cli_auth_mode == "api_key":
            sel_name = installed[self._cli_selected] if self._cli_selected < len(installed) else "?"
            sel_provider = providers.get(sel_name, {})
            display = sel_provider.get("display_name", sel_name)

            self._put(stdscr, y, lbl_col, f"Set API Key for {display}", self._bold())
            y += 2
            env_var = "ANTHROPIC_API_KEY" if sel_name == "claude" else "OPENAI_API_KEY"
            self._put(stdscr, y, lbl_col, f"Paste your {env_var}:", self._dim())
            y += 2

            # Key input (masked)
            self._put(stdscr, y, lbl_col, "Key:", self._dim())
            input_x = lbl_col + 6
            field_w = max(30, bar_w - input_x + col)
            visible = self._cli_api_key_input[:field_w]
            # Show first 8 chars, mask the rest
            if len(visible) > 8:
                masked = visible[:8] + "\u2022" * (len(visible) - 8)
            else:
                masked = visible
            self._put(stdscr, y, input_x, masked, self._bold() | self._cyan())

            cursor_x = input_x + min(self._cli_api_key_cursor, field_w)
            if 0 <= cursor_x < w - 1:
                try:
                    curses.curs_set(1)
                    stdscr.move(y, cursor_x)
                except curses.error:
                    pass
            y += 2

            self._hline(stdscr, y, col, bar_w)
            y += 1
            x = lbl_col
            self._put(stdscr, y, x, "[Enter]", self._cyan() | self._bold())
            self._put(stdscr, y, x + 8, "Save", self._dim())
            x += 16
            self._put(stdscr, y, x, "[Esc]", self._cyan() | self._bold())
            self._put(stdscr, y, x + 6, "Cancel", self._dim())
            return

        # --- Device auth display sub-screen ---
        if self._cli_auth_mode == "device_auth" and self._cli_device_auth:
            da = self._cli_device_auth
            sel_name = installed[self._cli_selected] if self._cli_selected < len(installed) else "?"
            sel_provider = providers.get(sel_name, {})
            display = sel_provider.get("display_name", sel_name)

            self._put(stdscr, y, lbl_col, f"Sign in to {display}", self._bold())
            y += 2

            if da.get("type") == "device_code":
                self._put(stdscr, y, lbl_col, "1. Open this link in your browser:", self._dim())
                y += 1
                url = da.get("url", "")
                self._put(stdscr, y, lbl_col + 3, url, self._cyan() | self._bold())
                y += 2
                code = da.get("code")
                if code:
                    self._put(stdscr, y, lbl_col, "2. Enter this code:", self._dim())
                    y += 1
                    self._put(stdscr, y, lbl_col + 3, code, self._green() | self._bold())
                    y += 2
                self._put(stdscr, y, lbl_col, "Waiting for approval...", self._yellow())
            elif da.get("type") == "browser":
                self._put(stdscr, y, lbl_col, da.get("message", "Opening browser..."), self._yellow())
            y += 2

            self._hline(stdscr, y, col, bar_w)
            y += 1
            self._put(stdscr, y, lbl_col, "[Enter/Esc]", self._cyan() | self._bold())
            self._put(stdscr, y, lbl_col + 12, "Back", self._dim())
            return

        # --- Main CLI selection screen ---
        curses.curs_set(0)

        self._put(stdscr, y, lbl_col, "Select AI CLI", self._bold())
        y += 2
        self._put(stdscr, y, lbl_col, "Choose the default CLI tool for running agents.", self._dim())
        y += 2

        # Show all providers — installed first, then not-installed
        all_ordered = installed + not_installed
        for i, name in enumerate(all_ordered):
            p = providers[name]
            display = p.get("display_name", name)
            is_installed = p.get("installed", False)
            authed = p.get("authenticated", False)
            auth_detail = p.get("auth_detail", "")
            is_installing = self._cli_installing == name

            # Provider name with selection cursor
            if i == self._cli_selected:
                self._put(stdscr, y, lbl_col, f"  \u25b8 {i+1}.", self._cyan() | self._bold())
                self._put(stdscr, y, lbl_col + 7, display, self._cyan() | self._bold())
            else:
                self._put(stdscr, y, lbl_col + 4, f"{i+1}.", self._dim())
                name_attr = 0 if is_installed else self._dim()
                self._put(stdscr, y, lbl_col + 7, display, name_attr)

            # Status indicator
            status_x = lbl_col + 7 + len(display) + 2
            if is_installing:
                self._put(stdscr, y, status_x, "\u25cf", self._yellow())
                self._put(stdscr, y, status_x + 2, "Installing...", self._yellow())
            elif not is_installed:
                self._put(stdscr, y, status_x, "\u25cb", self._dim())
                self._put(stdscr, y, status_x + 2, "Not installed", self._dim())
                self._put(stdscr, y, status_x + 16, "[I]", self._cyan())
            elif authed:
                self._put(stdscr, y, status_x, "\u25cf", self._green())
                detail_str = auth_detail[:bar_w - status_x - col + 2] if auth_detail else "Authenticated"
                self._put(stdscr, y, status_x + 2, detail_str, self._dim())
            else:
                self._put(stdscr, y, status_x, "\u25cb", self._red())
                self._put(stdscr, y, status_x + 2, "Not signed in", self._dim())
            y += 1

        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1
        x = lbl_col
        self._put(stdscr, y, x, "[\u2191\u2193]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 5, "Select", self._dim())
        x += 14
        self._put(stdscr, y, x, "[Enter]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 8, "Confirm", self._dim())
        x += 18
        self._put(stdscr, y, x, "[A]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 4, "API Key", self._dim())
        x += 13
        self._put(stdscr, y, x, "[S]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 4, "Sign in", self._dim())
        x += 13
        self._put(stdscr, y, x, "[I]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 4, "Install", self._dim())
        x += 13
        self._put(stdscr, y, x, "[Esc]", self._cyan() | self._bold())
        self._put(stdscr, y, x + 6, "Back", self._dim())

    def _draw_auth(self, stdscr, y, col, bar_w):
        s = self.state
        lbl_col = col + 2

        if s.get("auth_url"):
            self._put(stdscr, y, lbl_col, "Authorize this device:", self._bold())
            y += 2
            self._put(stdscr, y, lbl_col, "Visit:", self._dim())
            y += 1
            self._put(stdscr, y, lbl_col + 2, s.get("auth_url", ""), self._cyan() | self._bold())
            y += 2
            if s.get("auth_code"):
                self._put(stdscr, y, lbl_col, "Code:", self._dim())
                self._put(stdscr, y, lbl_col + 7, s["auth_code"], self._green() | self._bold())
                y += 2
            self._put(stdscr, y, lbl_col, "Waiting for approval...", self._yellow())
        else:
            self._put(stdscr, y, lbl_col, "Authenticating...", self._yellow())

        y += 2
        self._hline(stdscr, y, col, bar_w)
        y += 1
        self._put(stdscr, y, lbl_col + 2, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, y, lbl_col + 6, "Quit", self._dim())

    # ── logs view ────────────────────────────────────────────────────

    def _draw_logs(self, stdscr, h, w):
        col = 2
        bar_w = min(w - 4, 100)

        # Header
        self._put(stdscr, 1, col, "Logs", self._bold())
        self._put(stdscr, 1, col + bar_w - 10, "[L] Toggle", self._cyan())
        self._hline(stdscr, 2, col, bar_w)

        # Log lines
        all_lines = self._stdout_capture.get_lines(500)
        visible_h = h - 6
        if visible_h <= 0:
            return

        # Clamp scroll
        max_scroll = max(0, len(all_lines) - visible_h)
        self._scroll_offset = min(self._scroll_offset, max_scroll)

        end = len(all_lines) - self._scroll_offset
        start = max(0, end - visible_h)
        end = max(start, end)
        visible = all_lines[start:end]

        for i, line in enumerate(visible):
            y = 3 + i
            if y >= h - 3:
                break
            attr = 0
            if "Error" in line or "error" in line:
                attr = self._red()
            elif "Warning" in line or "warning" in line:
                attr = self._yellow()
            elif "Connected" in line or "success" in line:
                attr = self._green()
            self._put(stdscr, y, col, line[: w - 4], attr)

        # Shared tab footer (Logs highlighted as the active tab). Scroll
        # percent goes in the top-right of the panel since the footer
        # strip is now reserved for tab nav across all four views.
        if len(all_lines) > visible_h:
            pct = int((end / max(len(all_lines), 1)) * 100)
            self._put(stdscr, 1, col + bar_w - 8, f"  {pct:3d}%", self._dim())
        self._draw_tab_footer(stdscr, h, w, col, lbl_col=col + 2, bar_w=bar_w, current="logs")

    # ── helpers ──────────────────────────────────────────────────────

    def _clip(self, value, width: int) -> str:
        text = str(value)
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width <= 1:
            return text[:width]
        return text[: width - 1] + "…"

    def _compute_is_connected(self, row: Dict[str, Any]) -> bool:
        status = str(row.get("status") or "").lower()
        return status in ("online", "ready", "connected")

    def _format_relative_time(self, value) -> str:
        if not value:
            return "—"
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
        if not isinstance(value, datetime):
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        total = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
        if total < 60:
            return f"{total}s ago"
        if total < 3600:
            return f"{total // 60}m ago"
        if total < 86400:
            return f"{total // 3600}h ago"
        return f"{total // 86400}d ago"

    def _format_uptime(self) -> str:
        connected_at = self.state.get("connected_at")
        if not connected_at or self.state["connection"] != "connected":
            return "\u2014"
        total = int((datetime.now(timezone.utc) - connected_at).total_seconds())
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m {total % 60}s"
        hours = total // 3600
        minutes = (total % 3600) // 60
        return f"{hours}h {minutes}m"

    def _format_bytes(self, value) -> str:
        if value is None:
            return "\u2014"
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return "\u2014"

    def _format_load(self, compute: Dict[str, Any]) -> str:
        load = compute.get("load", {})
        one = load.get("one")
        five = load.get("five")
        fifteen = load.get("fifteen")
        if one is None or five is None or fifteen is None:
            return "\u2014"
        return f"{one:.2f}  {five:.2f}  {fifteen:.2f}"

    def _format_disk_free(self, disk: Dict[str, Any]) -> str:
        free = disk.get("free_bytes")
        total = disk.get("total_bytes")
        if free is None:
            return "\u2014"
        if total is None:
            return self._format_bytes(free)
        return f"{self._format_bytes(free)} free"

    def _record_compute_history(self, compute: Dict[str, Any]) -> None:
        samples = {
            "cpu": compute.get("cpu_percent"),
            "memory": (compute.get("memory") or {}).get("percent"),
            "load": (compute.get("load") or {}).get("normalized_percent"),
            "disk": (compute.get("disk") or {}).get("percent"),
        }
        for key, value in samples.items():
            if value is not None:
                self._metric_history[key].append(float(value))

    def _sparkline(self, values) -> str:
        if not values:
            return ""
        chars = "▁▂▃▄▅▆▇█"
        out = []
        for value in values:
            idx = int(max(0, min(len(chars) - 1, round((value / 100) * (len(chars) - 1)))))
            out.append(chars[idx])
        return "".join(out)

    def _usage_bar(self, value: Optional[float], width: int = 10) -> str:
        if value is None:
            return "[" + ("-" * width) + "]"
        filled = int(round(max(0, min(100, value)) / 100 * width))
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

    def _format_metric_line(self, metric: str, value: Optional[float], scale_max: float, invert_label: bool = False, suffix: Optional[str] = None) -> str:
        if value is None:
            return "\u2014"
        label = f"{value:.0f}%"
        if invert_label:
            free = max(0.0, 100.0 - value)
            label = f"{free:.0f}% free"
        bar = self._usage_bar((value / scale_max) * 100 if scale_max else value)
        parts = [f"{label:>8}", bar]
        if suffix:
            parts.append(suffix)
        return "  ".join(parts)

    def _get_compute_health(self, compute: Dict[str, Any]) -> str:
        cpu = compute.get("cpu_percent")
        mem = (compute.get("memory") or {}).get("percent")
        disk = (compute.get("disk") or {}).get("percent")
        load = (compute.get("load") or {}).get("normalized_percent")
        active_agents = (self.state.get("agent_counts", {}) or {}).get("running", 0)
        active_workflows = ((self.state.get("workflow_summary") or {}).get("counts") or {}).get("running", 0)

        if disk is not None and disk >= 92:
            return "Unhealthy"
        if mem is not None and mem >= 90:
            return "Unhealthy"
        if cpu is not None and cpu >= 90:
            return "Stressed"
        if load is not None and load >= 100:
            return "Stressed"
        if mem is not None and mem >= 80:
            return "Stressed"
        if (active_agents + active_workflows) > 0:
            if cpu is not None and cpu >= 25:
                return "Busy"
            return "Busy"
        if cpu is None and mem is None and disk is None:
            return "\u2014"
        return "Idle"

    def _format_compute_health(self, compute: Dict[str, Any]):
        health = self.state.get("compute_health") or self._get_compute_health(compute)
        if health == "Unhealthy":
            return health, self._red() | self._bold()
        if health == "Stressed":
            return health, self._yellow() | self._bold()
        if health == "Busy":
            return health, self._cyan() | self._bold()
        if health == "Idle":
            return health, self._green() | self._bold()
        return health, self._dim()
