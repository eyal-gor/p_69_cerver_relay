"""
Terminal UI for the Cerver Relay.

Shows a live dashboard with connection status, heartbeat, and system
statistics. Press L to view logs, Q to quit.
"""

import curses
import os
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
            "launchd_prompt": None,  # None=don't show, "pending"=showing, "done"=answered
            "cli_providers": {},  # {name: {display_name, installed, ...}}
            "default_cli": "claude",
            "cli_prompt": None,  # None=don't show, "pending"=showing, "done"=answered
            "agent_counts": {},
            "workflow_summary": {},
            "compute": {},
            "compute_health": None,
            "cerver_status": "idle",
            "cerver_compute_id": None,
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
        curses.use_default_colors()
        curses.curs_set(0)
        # Short timeout so we poll keys every 100ms for responsive input
        stdscr.timeout(100)

        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            try:
                curses.init_pair(5, 8, -1)  # bright black (gray)
            except curses.error:
                curses.init_pair(5, curses.COLOR_WHITE, -1)
            # Logo gradient: smooth indigo → white (8 levels, pairs 10–17)
            for i, color_num in enumerate(GRADIENT_COLORS):
                try:
                    curses.init_pair(10 + i, color_num, -1)
                except curses.error:
                    fb = curses.COLOR_BLUE if i < 3 else (curses.COLOR_CYAN if i < 6 else curses.COLOR_WHITE)
                    curses.init_pair(10 + i, fb, -1)

        last_draw = 0.0
        # Logo animates smoothly
        ANIM_INTERVAL = 0.1  # 10 fps — smooth shimmer

        while self._running:
            now = time.monotonic()
            # Dashboard with animation needs faster redraws; other views use REFRESH_MS
            interval = ANIM_INTERVAL if self._view in ("connect", "runtime") else self.REFRESH_MS / 1000.0
            if now - last_draw >= interval:
                self._anim_frame += 1
                try:
                    stdscr.erase()
                    h, w = stdscr.getmaxyx()
                    if self._view == "connect":
                        self._draw_connect(stdscr, h, w)
                    elif self._view == "runtime":
                        self._draw_runtime(stdscr, h, w)
                    elif self._view == "installed":
                        self._draw_installed(stdscr, h, w)
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
            self._running = False
        elif key == ord("l") or key == ord("L"):
            if self._view == "logs":
                self._view = self._last_main_view
            else:
                if self._view in ("connect", "runtime"):
                    self._last_main_view = self._view
                self._view = "logs"
                self._scroll_offset = 0
        elif key == ord("i") or key == ord("I"):
            # Toggle the installed-tools screen. Tracks the originating
            # main view so [I] returns the user where they came from
            # instead of always defaulting to Connect.
            if self._view == "installed":
                self._view = self._last_main_view
            elif self._view in ("connect", "runtime"):
                self._last_main_view = self._view
                self._view = "installed"
        elif key == ord("1"):
            # Direct nav: Connect tab. Also resets _last_main_view so
            # [I]/[L] from a sub-view return here.
            if self._view != "connect":
                self._view = "connect"
                self._last_main_view = "connect"
        elif key == ord("2"):
            # Direct nav: Runtime tab.
            if self._view != "runtime":
                self._view = "runtime"
                self._last_main_view = "runtime"
        elif key == ord("n") or key == ord("N"):
            if self._view == "connect":
                self._editing_name = True
                self._name_input = self.state.get("machine_name", "")
                self._name_cursor = len(self._name_input)
                if stdscr:
                    curses.curs_set(1)
        elif key == ord("h") or key == ord("H"):
            if self._view == "connect":
                self._editing_home = True
                self._home_input = self.state.get("home_dir", "")
                self._home_cursor = len(self._home_input)
                if stdscr:
                    curses.curs_set(1)
        elif key == ord("v") or key == ord("V"):
            if self._view in ("connect", "runtime", "installed"):
                self._verbose = not self._verbose
        elif key == ord("s") or key == ord("S"):
            if self._view == "connect" and self._on_launchd_install:
                ld = self.state.get("launchd")
                if ld in ("running", "installed"):
                    self._on_launchd_install(False)  # uninstall
                else:
                    self._on_launchd_install(True)  # install
        elif key == ord("c") or key == ord("C"):
            if self._view == "connect":
                # Open CLI selection prompt
                providers = self.state.get("cli_providers", {})
                installed = [n for n, p in providers.items() if p.get("installed")]
                current = self.state.get("default_cli", "claude")
                self._cli_selected = installed.index(current) if current in installed else 0
                self.state["cli_prompt"] = "pending"
        elif key == ord("d") or key == ord("D"):
            if self._view == "connect" and self._on_logout:
                self._on_logout()
                self._running = False
        elif key == curses.KEY_UP and self._view == "logs":
            self._scroll_offset += 1
        elif key == curses.KEY_DOWN and self._view == "logs":
            self._scroll_offset = max(0, self._scroll_offset - 1)

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

        # Header — animated CERVER logo with kompany-relay subtitle.
        # The big logo is the platform brand; the subtitle pins down which
        # variant of the runtime is actually executing.
        ver = f"v{s['version']}" if s["version"] else ""
        if w >= LOGO_WIDTH + 6:
            self._draw_animated_logo(stdscr, y, col)
            y += LOGO_HEIGHT
            subtitle = f"cerver relay {ver}".rstrip()
            self._put(stdscr, y, col + LOGO_WIDTH - len(subtitle), subtitle, self._dim())
            y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2
        else:
            self._put(stdscr, y, col, "cerver", self._bold() | self._green())
            self._put(stdscr, y, col + 7, "·", self._dim())
            self._put(stdscr, y, col + 9, "relay", self._dim())
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

        # Machine info — editable field
        self._put(stdscr, y, lbl_col, "Machine", self._dim())
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
            self._put(stdscr, y, val_col, machine_val, self._bold())
            self._put(stdscr, y, val_col + len(machine_val) + 1, "[N]", self._dim())
        y += 1

        # Home — editable field
        self._put(stdscr, y, lbl_col, "Home", self._dim())
        if self._editing_home:
            # Show input field with cursor
            field_w = max(30, bar_w - val_col + col)
            display = self._home_input[:field_w]
            self._put(stdscr, y, val_col, display, self._bold() | self._cyan())
            # Blinking cursor
            cursor_x = val_col + min(self._home_cursor, field_w)
            if 0 <= cursor_x < w - 1:
                try:
                    curses.curs_set(1)
                    stdscr.move(y, cursor_x)
                except curses.error:
                    pass
            # Hint
            self._put(stdscr, y + 1, val_col, "Enter to save, Esc to cancel", self._dim())
            y += 2
        else:
            home_val = s.get("home_dir", "\u2014")
            self._put(stdscr, y, val_col, home_val, self._bold())
            self._put(stdscr, y, val_col + len(home_val) + 1, "[H]", self._dim())
            y += 1

        if s.get("project"):
            self._put(stdscr, y, lbl_col, "Project", self._dim())
            self._put(stdscr, y, val_col, s["project"], self._bold())
            y += 1

        # AI CLI provider
        providers = s.get("cli_providers", {})
        default_cli = s.get("default_cli", "claude")
        default_provider = providers.get(default_cli, {})
        cli_display = default_provider.get("display_name", default_cli.title())
        cli_authed = default_provider.get("authenticated", False)
        self._put(stdscr, y, lbl_col, "AI CLI", self._dim())
        self._put(stdscr, y, val_col, cli_display, self._bold())
        # Auth status dot
        dot_x = val_col + len(cli_display) + 1
        if cli_authed:
            self._put(stdscr, y, dot_x, "\u25cf", self._green())
        else:
            self._put(stdscr, y, dot_x, "\u25cb", self._red())
        self._put(stdscr, y, dot_x + 2, "[C]", self._dim())
        y += 1

        dashboard_url = s.get("dashboard_url", f"http://localhost:{s['port']}/")
        self._put(stdscr, y, lbl_col, "Dashboard", self._dim())
        self._put(stdscr, y, val_col, dashboard_url, self._bold())
        y += 1

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
            self._put(stdscr, y, lbl_col, "Startup", self._dim())
            if ld == "running":
                self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
                self._put(stdscr, y, val_col + 2, "Enabled")
            elif ld == "installed":
                self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
                self._put(stdscr, y, val_col + 2, "Installed (not running)")
            elif ld == "error":
                self._put(stdscr, y, val_col, "\u25cf", self._red() | self._bold())
                self._put(stdscr, y, val_col + 2, "Error")
            else:
                self._put(stdscr, y, val_col, "\u25cf", self._dim())
                self._put(stdscr, y, val_col + 2, "Not installed")
            y += 1
            if self._verbose:
                self._put(stdscr, y, val_col, "Auto-start on login via launchd", self._dim())
                y += 1

        # cerver CLI binary (~/.cerver/bin/cerver). Labelled "cerver CLI"
        # to disambiguate from the AI CLI row above (claude/codex/grok).
        cli_path = os.path.expanduser("~/.cerver/bin/cerver")
        self._put(stdscr, y, lbl_col, "cerver CLI", self._dim())
        if os.access(cli_path, os.X_OK):
            self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, "Installed")
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._dim())
            self._put(stdscr, y, val_col + 2, "Not installed")
        y += 1
        if self._verbose:
            self._put(stdscr, y, val_col, "cerver run / compare / login (~/.cerver/bin/cerver)", self._dim())
            y += 1

        # Reconnects — moved from the old WORKLOAD block. Belongs to
        # Connect because it's a transport-stability metric, not a
        # measure of how much work the relay is doing.
        rc = s.get("reconnect_count", 0)
        self._put(stdscr, y, lbl_col, "Reconnects", self._dim())
        self._put(stdscr, y, val_col, str(rc), self._green() if rc == 0 else self._yellow())
        y += 1

        # Footer — tab nav first, then connect-specific actions.
        if not self._editing_home:
            curses.curs_set(0)
        self._draw_tab_footer(stdscr, h, w, col, lbl_col, bar_w, current="connect")

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

        # Header — same logo treatment as Connect so the user always
        # knows they're inside the relay TUI, regardless of which tab.
        ver = f"v{s['version']}" if s["version"] else ""
        if w >= LOGO_WIDTH + 6:
            self._draw_animated_logo(stdscr, y, col)
            y += LOGO_HEIGHT
            subtitle = f"cerver relay {ver}".rstrip()
            self._put(stdscr, y, col + LOGO_WIDTH - len(subtitle), subtitle, self._dim())
            y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2
        else:
            self._put(stdscr, y, col, "cerver", self._bold() | self._green())
            self._put(stdscr, y, col + 7, "·", self._dim())
            self._put(stdscr, y, col + 9, "relay", self._dim())
            y += 1
            if ver:
                self._put(stdscr, y, col, ver, self._dim())
                y += 1
            self._hline(stdscr, y, col, bar_w)
            y += 2

        # Identity-lite — just enough to confirm which compute you're
        # looking at without duplicating the full block from Connect.
        if s.get("machine_name"):
            self._put(stdscr, y, lbl_col, "Machine", self._dim())
            self._put(stdscr, y, val_col, s["machine_name"], self._bold())
            y += 1
        providers = s.get("cli_providers", {})
        default_cli = s.get("default_cli", "claude")
        default_provider = providers.get(default_cli, {})
        cli_display = default_provider.get("display_name", default_cli.title())
        self._put(stdscr, y, lbl_col, "AI CLI", self._dim())
        self._put(stdscr, y, val_col, cli_display, self._bold())
        y += 2

        # Workload
        self._put(stdscr, y, lbl_col, "WORKLOAD", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        agent_counts = s.get("agent_counts", {})
        self._put(stdscr, y, lbl_col, "Agents", self._dim())
        self._put(
            stdscr,
            y,
            val_col,
            f"{agent_counts.get('running', 0)} run  {agent_counts.get('paused', 0)} paused  {agent_counts.get('prepared', 0)} ready",
        )
        y += 1

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

    # ── shared tab footer ────────────────────────────────────────────
    # Pulled into a helper so both Connect and Runtime show the same
    # navigation strip with a ● on the active tab. Connect-specific
    # action keys ([N]/[H]/[S]/[C]/[D]) are appended only when current
    # == "connect" since those handlers ignore other views anyway.

    def _draw_tab_footer(self, stdscr, h, w, col, lbl_col, bar_w, current):
        footer_y = h - 2
        self._hline(stdscr, footer_y - 1, col, bar_w)
        x = lbl_col

        # Tab nav: [1] Connect, [2] Runtime — ● marks the active one.
        active_dot = "●"
        self._put(stdscr, footer_y, x, "[1]", self._cyan() | self._bold())
        label = f"{active_dot} Connect" if current == "connect" else "Connect"
        attr = self._bold() if current == "connect" else self._dim()
        self._put(stdscr, footer_y, x + 4, label, attr)
        x += 4 + len(label) + 2

        self._put(stdscr, footer_y, x, "[2]", self._cyan() | self._bold())
        label = f"{active_dot} Runtime" if current == "runtime" else "Runtime"
        attr = self._bold() if current == "runtime" else self._dim()
        self._put(stdscr, footer_y, x + 4, label, attr)
        x += 4 + len(label) + 2

        self._put(stdscr, footer_y, x, "[I]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Installed", self._dim())
        x += 15

        self._put(stdscr, footer_y, x, "[L]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Logs", self._dim())
        x += 10

        self._put(stdscr, footer_y, x, "[V]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Verbose", self._dim())
        x += 13

        if current == "connect":
            # Identity edits + setup actions only apply on the Connect
            # tab — their handlers gate on _view == "connect".
            self._put(stdscr, footer_y, x, "[N]", self._cyan() | self._bold())
            self._put(stdscr, footer_y, x + 4, "Name", self._dim())
            x += 10
            self._put(stdscr, footer_y, x, "[H]", self._cyan() | self._bold())
            self._put(stdscr, footer_y, x + 4, "Home", self._dim())
            x += 10
            self._put(stdscr, footer_y, x, "[S]", self._cyan() | self._bold())
            ld = self.state.get("launchd")
            s_label = "Uninstall" if ld in ("running", "installed") else "Startup"
            self._put(stdscr, footer_y, x + 4, s_label, self._dim())
            x += 4 + len(s_label) + 2
            self._put(stdscr, footer_y, x, "[C]", self._cyan() | self._bold())
            self._put(stdscr, footer_y, x + 4, "CLI", self._dim())
            x += 9
            self._put(stdscr, footer_y, x, "[D]", self._cyan() | self._bold())
            self._put(stdscr, footer_y, x + 4, "Logout", self._dim())
            x += 12

        self._put(stdscr, footer_y, x, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Quit", self._dim())

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

        # Footer
        footer_y = h - 2
        self._hline(stdscr, footer_y - 1, col, bar_w)
        x = col + 2
        self._put(stdscr, footer_y, x, "[L]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Back", self._dim())
        x += 12
        self._put(stdscr, footer_y, x, "[\u2191\u2193]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 5, "Scroll", self._dim())
        x += 14
        self._put(stdscr, footer_y, x, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Quit", self._dim())

        # Scroll indicator
        if len(all_lines) > visible_h:
            pct = int((end / max(len(all_lines), 1)) * 100)
            self._put(stdscr, footer_y, col + bar_w - 6, f"{pct:3d}%", self._dim())

    # ── installed-tools view ─────────────────────────────────────────

    def _draw_installed(self, stdscr, h, w):
        """Per-tool installation status for the compute this relay runs on.

        Two groups:
          1. cerver CLI — the standalone `cerver` Go binary at
             ~/.cerver/bin/cerver. Checked directly (filesystem probe)
             because it doesn't live in cli_providers.
          2. AI CLIs — claude / codex / grok / etc, populated by
             relay_client via cli_providers.get_available_providers().
             Status includes auth (api_key / oauth / none) so the user
             can spot "installed but not signed in" cases.

        No keys to act on here — it's a read-only inspector. [I] toggles
        back to dashboard; [Q] still quits globally.
        """
        col = 2
        lbl_col = 4
        val_col = 22
        path_col = 42
        bar_w = min(w - 4, 100)

        # Header
        self._put(stdscr, 1, col, "Installed on this compute", self._bold())
        self._put(stdscr, 1, col + bar_w - 10, "[I] Back", self._cyan())
        self._hline(stdscr, 2, col, bar_w)

        y = 4

        # ── cerver CLI ───────────────────────────────────────────────
        self._put(stdscr, y, lbl_col, "CERVER", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        cli_path = os.path.expanduser("~/.cerver/bin/cerver")
        installed = os.access(cli_path, os.X_OK)
        self._put(stdscr, y, lbl_col, "cerver CLI", self._dim())
        if installed:
            self._put(stdscr, y, val_col, "●", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, "Installed")
            self._put(stdscr, y, path_col, cli_path, self._dim())
        else:
            self._put(stdscr, y, val_col, "●", self._dim())
            self._put(stdscr, y, val_col + 2, "Not installed")
            self._put(stdscr, y, path_col, "go install github.com/eyal-gor/p_71_cerver_cli/cmd/cerver@latest", self._dim())
        y += 1
        y += 1

        # ── AI CLIs ──────────────────────────────────────────────────
        self._put(stdscr, y, lbl_col, "AI CLIs", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        providers = self.state.get("cli_providers") or {}
        if not providers:
            self._put(stdscr, y, lbl_col, "No providers reported yet — relay still initializing.", self._dim())
            y += 1
        else:
            default = self.state.get("default_cli", "")
            for name, p in providers.items():
                display = p.get("display_name") or name
                is_default = (name == default)
                label = f"{display} (default)" if is_default else display
                self._put(stdscr, y, lbl_col, label, self._dim())
                if p.get("installed"):
                    self._put(stdscr, y, val_col, "●", self._green() | self._bold())
                    # Tighter status: "Installed · signed in" or
                    # "Installed · not signed in" so the user can act on
                    # an auth gap without opening a separate screen.
                    if p.get("authenticated"):
                        method = p.get("auth_method") or ""
                        suffix = f" · {method}" if method and method != "none" else ""
                        detail = p.get("auth_detail") or ""
                        self._put(stdscr, y, val_col + 2, f"Installed{suffix}")
                        if detail and self._verbose:
                            self._put(stdscr, y, path_col, detail[: max(0, w - path_col - 2)], self._dim())
                        elif p.get("path"):
                            self._put(stdscr, y, path_col, p["path"][: max(0, w - path_col - 2)], self._dim())
                    else:
                        self._put(stdscr, y, val_col + 2, "Installed · not signed in", self._yellow())
                        if p.get("path"):
                            self._put(stdscr, y, path_col, p["path"][: max(0, w - path_col - 2)], self._dim())
                else:
                    self._put(stdscr, y, val_col, "●", self._dim())
                    self._put(stdscr, y, val_col + 2, "Not installed")
                    hint = p.get("install_hint") or ""
                    if hint:
                        self._put(stdscr, y, path_col, hint[: max(0, w - path_col - 2)], self._dim())
                y += 1

        # Footer — direct nav back to either main tab so the user
        # doesn't have to remember which one they came from.
        footer_y = h - 2
        self._hline(stdscr, footer_y - 1, col, bar_w)
        x = lbl_col
        self._put(stdscr, footer_y, x, "[1]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Connect", self._dim())
        x += 13
        self._put(stdscr, footer_y, x, "[2]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Runtime", self._dim())
        x += 13
        self._put(stdscr, footer_y, x, "[I]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Back", self._dim())
        x += 11
        self._put(stdscr, footer_y, x, "[V]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Verbose", self._dim())
        x += 13
        self._put(stdscr, footer_y, x, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Quit", self._dim())

    # ── helpers ──────────────────────────────────────────────────────

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
