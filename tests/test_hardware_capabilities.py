"""Machines report hardware and installed tools so agents can pick the right one."""

from branch_monkey_mcp.computer_runtime import capabilities as cap


def test_runtime_capabilities_include_hardware_and_tools():
    c = cap.get_runtime_capabilities()
    hw = c["hardware"]
    assert hw["os"] in ("macos", "linux") or isinstance(hw["os"], str)
    assert isinstance(hw["cpu_cores"], int) and hw["cpu_cores"] > 0
    assert isinstance(hw["gpus"], list)
    assert isinstance(c["tools"], list)
    assert all(t in cap.COMMON_TOOLS for t in c["tools"])


def test_nvidia_gpus_are_parsed(monkeypatch):
    monkeypatch.setattr(cap, "_run", lambda cmd, timeout=3.0: "NVIDIA A100-SXM4-80GB, 81920\nNVIDIA A100-SXM4-80GB, 81920\n" if cmd[0] == "nvidia-smi" else "")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    gpus = cap._gpus()
    assert gpus == [
        {"name": "NVIDIA A100-SXM4-80GB", "memory_gb": 80, "vendor": "nvidia"},
        {"name": "NVIDIA A100-SXM4-80GB", "memory_gb": 80, "vendor": "nvidia"},
    ]


def test_hardware_is_cached(monkeypatch):
    cap._HARDWARE_CACHE.clear()
    first = cap.get_hardware()
    monkeypatch.setattr(cap, "_gpus", lambda: [{"name": "changed"}])
    assert cap.get_hardware() is first
