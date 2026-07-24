"""Server heartbeat thresholds are widened to tolerate lossy daemon links.

These constants pair with the daemon's ping_timeout=60: a dead connection is
still reclaimed within ~90s, while presence debounce + incremental gap backfill
absorb the brief reconnect windows. Locking the values in prevents a regression
back to the tighter 15/45 that evicted daemons on transient PONG loss.
"""

from __future__ import annotations

from se3.server import ws


def test_heartbeat_constants_relaxed() -> None:
    assert ws.PING_INTERVAL == 20.0
    assert ws.HEARTBEAT_TIMEOUT == 90.0
