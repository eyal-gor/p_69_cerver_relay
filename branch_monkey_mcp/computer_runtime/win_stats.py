"""CPU and memory on Windows, from the Windows API (no ps, vm_stat or /proc)."""

import ctypes
import sys
import time
from typing import Optional


def memory() -> Optional[dict]:
    if sys.platform != "win32":
        return None

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    total, free = int(st.ullTotalPhys), int(st.ullAvailPhys)
    used = max(total - free, 0)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "percent": round(used / total * 100, 1) if total else None,
    }


def _times():
    idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return idle.value, kernel.value + user.value  # kernel time includes idle time


def cpu_percent(sample: float = 0.25) -> Optional[float]:
    """Whole-computer CPU use over a short sample, 0–100."""
    if sys.platform != "win32":
        return None
    a = _times()
    time.sleep(sample)
    b = _times()
    if not a or not b or b[1] <= a[1]:
        return None
    busy = 1 - (b[0] - a[0]) / (b[1] - a[1])
    return round(min(max(busy * 100, 0.0), 100.0), 1)
