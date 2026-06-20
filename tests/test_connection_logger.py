"""
Unit tests for the relay connection-event logger.

``connection_logger.ConnectionLogger`` is the relay's reliability-observability
surface: it records every connection lifecycle event (connect / disconnect /
reconnect / heartbeat / failure) into a bounded in-memory ring buffer plus a
best-effort JSON-lines file, and rolls those events up into the snapshot served
by ``/api/relay/diagnostics``. When a sandbox's connection flaps, this is the
record operators read to find out what happened, so its bookkeeping needs to be
correct. The behaviours covered here:

  - Event entries: ``ts`` + ``event`` are always present; optional fields
    (detail / reason / error / attempt / delay) are included only when supplied,
    ``attempt=0`` is kept (it is not None), and ``delay`` is rounded to 0.1s.
  - Running stats: connect / disconnect / reconnect / heartbeat counters and the
    ``last_connected_at`` / ``last_disconnected_at`` timestamps update per event.
  - Ring buffer: most-recent ordering, ``limit`` slicing, and ``maxlen`` eviction
    of the oldest events.
  - Diagnostics: heartbeat success-rate, last-failure selection, and the three
    uptime cases (currently up / currently down / never connected).
  - File backend: events are appended as JSON lines and the log rotates once it
    exceeds ``MAX_LOG_SIZE``.
  - Thread safety: concurrent logging never drops events.

All tests are hermetic: the module-level ``LOG_DIR`` / ``LOG_FILE`` are
redirected to a ``tmp_path`` so nothing touches the real ``~/.kompany`` and the
process-wide singleton is never used.

Run with: uv run --with pytest python -m pytest tests/test_connection_logger.py
"""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make the package importable when running from repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from branch_monkey_mcp import connection_logger as cl  # noqa: E402
from branch_monkey_mcp.connection_logger import ConnectionLogger  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def logger(tmp_path, monkeypatch):
    """A fresh ConnectionLogger writing into an isolated temp log file.

    The module references ``LOG_DIR`` / ``LOG_FILE`` as globals at call time, so
    redirecting them before construction keeps every read and write inside
    ``tmp_path`` and away from the real ``~/.kompany`` and the singleton.
    """
    log_dir = tmp_path / ".kompany"
    monkeypatch.setattr(cl, "LOG_DIR", log_dir)
    monkeypatch.setattr(cl, "LOG_FILE", log_dir / "connection_events.log")
    return ConnectionLogger()


def _read_log_lines(path: Path) -> list:
    """Parse a JSON-lines log file into a list of dicts."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Event entry shape
# ---------------------------------------------------------------------------


def test_minimal_event_has_ts_and_event_only(logger):
    logger.log("connected")
    entry = logger.get_recent_events()[-1]

    assert entry["event"] == "connected"
    assert "ts" in entry
    # ISO-8601 parse must succeed.
    datetime.fromisoformat(entry["ts"])
    # No optional fields leak in when not supplied.
    assert set(entry.keys()) == {"ts", "event"}


def test_optional_fields_included_only_when_supplied(logger):
    logger.log(
        "reconnecting",
        detail="retrying",
        reason="heartbeat_timeout",
        error="ReadTimeout",
        attempt=3,
        delay=2.5,
    )
    entry = logger.get_recent_events()[-1]

    assert entry["detail"] == "retrying"
    assert entry["reason"] == "heartbeat_timeout"
    assert entry["error"] == "ReadTimeout"
    assert entry["attempt"] == 3
    assert entry["delay"] == 2.5


def test_attempt_zero_is_kept(logger):
    # attempt is gated on "is not None", so a first attempt of 0 must survive.
    logger.log("reconnecting", attempt=0)
    entry = logger.get_recent_events()[-1]
    assert entry["attempt"] == 0


def test_delay_is_rounded_to_one_decimal(logger):
    logger.log("reconnecting", delay=1.23456)
    entry = logger.get_recent_events()[-1]
    assert entry["delay"] == 1.2


# ---------------------------------------------------------------------------
# Running stats
# ---------------------------------------------------------------------------


def test_connect_disconnect_reconnect_counters(logger):
    logger.log("connected")
    logger.log("disconnected", reason="heartbeat_timeout")
    logger.log("reconnected")
    logger.log("connected")

    diag = logger.get_diagnostics()
    assert diag["stats"]["total_connects"] == 2
    assert diag["stats"]["total_disconnects"] == 1
    assert diag["stats"]["total_reconnects"] == 1


def test_connected_and_reconnected_update_last_connected_at(logger):
    logger.log("connected")
    first = logger.get_diagnostics()["connection"]["last_connected_at"]
    assert first is not None

    logger.log("reconnected")
    second = logger.get_diagnostics()["connection"]["last_connected_at"]
    # reconnected refreshes the "currently connected since" marker.
    assert second is not None
    assert second == logger.get_recent_events()[-1]["ts"]


def test_disconnect_records_last_disconnected_at(logger):
    logger.log("disconnected", reason="auth_expired")
    conn = logger.get_diagnostics()["connection"]
    assert conn["last_disconnected_at"] is not None


def test_heartbeat_counters_and_success_rate(logger):
    for _ in range(3):
        logger.log("heartbeat_ok")
    logger.log("heartbeat_failed", error="Timeout")

    diag = logger.get_diagnostics()
    assert diag["heartbeat"]["total_ok"] == 3
    assert diag["heartbeat"]["total_failed"] == 1
    # 3 / 4 = 75.0%
    assert diag["heartbeat"]["success_rate_pct"] == 75.0


def test_heartbeat_success_rate_none_when_no_heartbeats(logger):
    logger.log("connected")
    assert logger.get_diagnostics()["heartbeat"]["success_rate_pct"] is None


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


def test_recent_events_are_in_chronological_order(logger):
    for i in range(5):
        logger.log("heartbeat_ok", detail=f"beat-{i}")
    events = logger.get_recent_events()
    details = [e["detail"] for e in events]
    assert details == [f"beat-{i}" for i in range(5)]


def test_recent_events_limit_returns_only_the_tail(logger):
    for i in range(10):
        logger.log("heartbeat_ok", detail=f"beat-{i}")
    tail = logger.get_recent_events(limit=3)
    assert [e["detail"] for e in tail] == ["beat-7", "beat-8", "beat-9"]


def test_ring_buffer_evicts_oldest_beyond_maxlen(tmp_path, monkeypatch):
    # Shrink the buffer so the test stays small; __init__ reads the global.
    log_dir = tmp_path / ".kompany"
    monkeypatch.setattr(cl, "LOG_DIR", log_dir)
    monkeypatch.setattr(cl, "LOG_FILE", log_dir / "connection_events.log")
    monkeypatch.setattr(cl, "RING_BUFFER_SIZE", 5)

    logger = ConnectionLogger()
    for i in range(8):
        logger.log("heartbeat_ok", detail=f"beat-{i}")

    events = logger.get_recent_events(limit=100)
    # Only the last 5 survive; beat-0..beat-2 were evicted.
    assert len(events) == 5
    assert [e["detail"] for e in events] == [f"beat-{i}" for i in range(3, 8)]


# ---------------------------------------------------------------------------
# Diagnostics: last failure + uptime
# ---------------------------------------------------------------------------


def test_last_failure_picks_most_recent_failure_event(logger):
    logger.log("connected")
    logger.log("heartbeat_failed", error="Timeout")
    logger.log("disconnected", reason="heartbeat_timeout")
    logger.log("reconnected")  # a non-failure event after the failures

    last_failure = logger.get_diagnostics()["last_failure"]
    # The most recent failure-class event is the disconnect, not the heartbeat.
    assert last_failure is not None
    assert last_failure["event"] == "disconnected"


def test_last_failure_none_when_only_healthy_events(logger):
    logger.log("connected")
    logger.log("heartbeat_ok")
    assert logger.get_diagnostics()["last_failure"] is None


def test_uptime_none_when_never_connected(logger):
    logger.log("connecting")
    assert logger.get_diagnostics()["connection"]["uptime_seconds"] is None


def test_uptime_positive_while_connected(logger):
    # Backdate the connect so uptime is a stable, clearly-positive value.
    past = datetime.now(timezone.utc) - timedelta(seconds=100)
    logger._stats["last_connected_at"] = past.isoformat()

    uptime = logger.get_diagnostics()["connection"]["uptime_seconds"]
    assert uptime is not None
    assert 95 <= uptime <= 120


def test_uptime_zero_when_disconnect_after_connect(logger):
    now = datetime.now(timezone.utc)
    connected = now - timedelta(seconds=100)
    disconnected = now - timedelta(seconds=10)  # later than the connect
    logger._stats["last_connected_at"] = connected.isoformat()
    logger._stats["last_disconnected_at"] = disconnected.isoformat()

    # Currently down: a disconnect newer than the last connect means uptime 0.
    assert logger.get_diagnostics()["connection"]["uptime_seconds"] == 0


# ---------------------------------------------------------------------------
# File backend
# ---------------------------------------------------------------------------


def test_events_are_appended_as_json_lines(logger):
    logger.log("connected", detail="initial")
    logger.log("heartbeat_ok")

    lines = _read_log_lines(cl.LOG_FILE)
    assert len(lines) == 2
    assert lines[0]["event"] == "connected"
    assert lines[0]["detail"] == "initial"
    assert lines[1]["event"] == "heartbeat_ok"


def test_log_rotates_when_exceeding_max_size(tmp_path, monkeypatch):
    log_dir = tmp_path / ".kompany"
    log_file = log_dir / "connection_events.log"
    monkeypatch.setattr(cl, "LOG_DIR", log_dir)
    monkeypatch.setattr(cl, "LOG_FILE", log_file)
    # Tiny cap: a single entry already exceeds it, forcing a rotation next write.
    monkeypatch.setattr(cl, "MAX_LOG_SIZE", 10)

    logger = ConnectionLogger()
    logger.log("connected")  # file now > 10 bytes
    logger.log("heartbeat_ok")  # rotation triggers before this write

    rotated = log_file.with_suffix(".log.1")
    assert rotated.exists()
    # The live file holds the post-rotation entry only.
    live_lines = _read_log_lines(log_file)
    assert len(live_lines) == 1
    assert live_lines[0]["event"] == "heartbeat_ok"


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_logging_records_every_event(logger):
    threads = []

    def worker(start):
        for i in range(20):
            logger.log("heartbeat_ok", detail=f"t{start}-{i}")

    for t_id in range(5):
        threads.append(threading.Thread(target=worker, args=(t_id,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    diag = logger.get_diagnostics()
    # 5 threads x 20 heartbeats, none lost to a race on the shared buffer/stats.
    assert diag["heartbeat"]["total_ok"] == 100
    assert len(_read_log_lines(cl.LOG_FILE)) == 100
