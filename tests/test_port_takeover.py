"""A relay must never kill another person's relay that holds the local port."""

import os
import socket

import pytest

from branch_monkey_mcp import relay_client as rc


@pytest.fixture
def held_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(128)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture(autouse=True)
def pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CERVER_DIR", tmp_path)
    monkeypatch.setattr(rc, "RELAY_PID_FILE", tmp_path / "relay.pid")
    for k in ("RELAY_LOCAL_PORT", "BRANCH_MONKEY_LOCAL_PORT"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "relay.pid"


def test_free_port_is_used_and_pid_recorded(pidfile):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    assert rc.resolve_local_port(port) == port
    assert pidfile.read_text() == str(os.getpid())
    assert os.environ["RELAY_LOCAL_PORT"] == str(port)


def test_other_homes_relay_is_left_running(held_port, monkeypatch):
    monkeypatch.setattr(rc, "_port_holder_info", lambda p: {"pid": "999999", "command": "uvx cerver-relay --cerver-only"})
    monkeypatch.setattr(rc, "_process_home", lambda pid: "/home/someone-else")
    killed = []
    monkeypatch.setattr(rc, "_try_take_over_port", lambda p, h: killed.append(h) or True)
    port = rc.resolve_local_port(held_port)
    assert port != held_port
    assert not killed
    # And the server start does not try to take it over either.
    assert rc.start_server_in_background(port=held_port) is None
    assert not killed


def test_stale_copy_of_us_is_reclaimed(held_port, monkeypatch, pidfile):
    pidfile.write_text("424242")
    monkeypatch.setattr(rc, "_port_holder_info", lambda p: {"pid": "424242", "command": "python -m branch_monkey_mcp.relay"})
    assert rc.resolve_local_port(held_port) == held_port


def test_same_home_relay_counts_as_ours(monkeypatch):
    monkeypatch.setattr(rc, "_process_home", lambda pid: os.path.expanduser("~"))
    assert rc._is_own_relay({"pid": "1", "command": "cerver-relay"})


def test_unknown_home_is_not_ours(monkeypatch):
    monkeypatch.setattr(rc, "_process_home", lambda pid: None)
    assert not rc._is_own_relay({"pid": "1", "command": "cerver-relay"})


def test_non_relay_is_never_ours():
    assert not rc._is_own_relay({"pid": str(os.getpid()), "command": "nginx"})
