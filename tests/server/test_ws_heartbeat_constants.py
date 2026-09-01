"""Server heartbeat thresholds are widened to tolerate lossy daemon links.

These constants pair with the daemon's ping_timeout=60: a dead connection is
still reclaimed within ~90s, while presence debounce + incremental gap backfill
absorb the brief reconnect windows. Locking the values in prevents a regression
back to the tighter 15/45 that evicted daemons on transient PONG loss.
"""

from __future__ import annotations

from tianluo.server import ws


def test_heartbeat_constants_relaxed() -> None:
    assert ws.PING_INTERVAL == 20.0
    assert ws.HEARTBEAT_TIMEOUT == 90.0


def test_run_declares_the_transport_keepalive(monkeypatch):
    """uvicorn's WS ping must not be tighter than the app's own liveness rule.

    Inherited, uvicorn closes a WebSocket after 20 s without a pong — and its
    ping rides the same inbound stream the app is consuming, so a receive loop
    working through a large flow's ~150-frame history drain lost the daemon
    connection to ``1011 keepalive ping timeout`` mid-reply. Declaring the
    interval/timeout from the heartbeat constants keeps the transport from
    declaring a peer dead that the protocol above it still considers alive.
    """
    import sys
    import types

    from tianluo.server import app as app_module

    captured = {}
    fake = types.ModuleType("uvicorn")

    def _run(app, **kwargs):
        captured.update(kwargs)

    fake.run = _run
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    app_module.run(host="127.0.0.1", port=0)

    assert captured["ws_ping_interval"] == ws.PING_INTERVAL
    assert captured["ws_ping_timeout"] == ws.HEARTBEAT_TIMEOUT
    # The transport must outlive the application-level heartbeat, never the
    # other way round.
    assert captured["ws_ping_timeout"] >= ws.HEARTBEAT_TIMEOUT
