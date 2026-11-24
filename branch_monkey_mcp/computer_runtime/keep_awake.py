"""Keep this computer awake while the relay runs, so agents can reach it.

The setting lives in ~/.cerver/awake.json ({"keep_awake": true}), written by
the Cerver app or `cerver awake on|off`. With no file, desktops and servers
default to on and laptops to off (a laptop should still sleep in a bag).

It only stops *idle* sleep, and on a Mac and on Windows only while plugged in:
closing a laptop's lid or choosing Sleep still works. The same setting tells
Cerver to email the owner if this computer goes offline.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from functools import lru_cache
from typing import Optional

SETTINGS = Path.home() / ".cerver" / "awake.json"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@lru_cache(maxsize=1)
def has_battery() -> bool:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
            return "InternalBattery" in out
        if sys.platform == "win32":
            st = _win_power_status()
            return st is not None and st[1] != 128  # 128 = no system battery
        return bool(glob.glob("/sys/class/power_supply/BAT*"))
    except Exception:
        return False


def wanted() -> bool:
    try:
        data = json.loads(SETTINGS.read_text())
        if isinstance(data.get("keep_awake"), bool):
            return data["keep_awake"]
    except Exception:
        pass
    return not has_battery()


def _win_power_status() -> Optional[tuple]:
    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):  # type: ignore[attr-defined]
        return None
    return (s.ACLineStatus, s.BatteryFlag)


class KeepAwake:
    """Call sync() every minute from the relay's event loop (its main thread)."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._win_held = False
        self.on = False

    def sync(self) -> bool:
        self.on = wanted()
        if self.on:
            self._hold()
        else:
            self.release()
        return self.on

    def _hold(self) -> None:
        if sys.platform == "win32":
            st = _win_power_status()
            plugged = st is None or st[0] != 0  # 0 = on battery
            if plugged:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
                self._win_held = True
            elif self._win_held:
                self.release()
            return
        if self._proc and self._proc.poll() is None:
            return
        if sys.platform == "darwin":
            # -s: no system sleep, and only while on AC power. -w: ends with the relay.
            cmd = ["caffeinate", "-s", "-w", str(os.getpid())]
        elif shutil.which("systemd-inhibit"):
            cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=Cerver",
                   "--why=Agents run on this computer", "--mode=block", "sleep", "infinity"]
        else:
            return
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[Relay] Keeping this computer awake (Cerver setting: keep awake)")
        except Exception as e:
            print(f"[Relay] Couldn't keep this computer awake: {e}")

    def release(self) -> None:
        if sys.platform == "win32":
            if self._win_held:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
                self._win_held = False
            return
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            print("[Relay] This computer may sleep again")
        self._proc = None
