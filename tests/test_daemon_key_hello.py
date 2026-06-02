"""Tests for the daemon-side HELLO ``key`` credential (group G5).

Covers the three layers of the daemon-side change:

* ``protocol.make_hello`` gains an optional, additive ``key`` field;
* ``DaemonClient`` carries the configured key in HELLO, stops the reconnect
  storm on ``WELCOME(accepted=false)``, and never logs the key;
* ``DaemonConfig.daemon_key`` / ``se3 daemon start --daemon-key`` thread the
  credential through to the client, and ``se3 daemon status`` never echoes it.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from se3.daemon import protocol
from se3.daemon.client import DaemonClient
from se3.daemon.daemon import Daemon, DaemonConfig, daemon_status


# --------------------------------------------------------------------------
# protocol.make_hello — optional, additive key field
# --------------------------------------------------------------------------


def test_make_hello_includes_key_when_set():
    hello = protocol.make_hello("m1", "host", "6.4.0", "secret-key")
    assert hello.type == protocol.MSG_HELLO
    assert hello.payload["machine_id"] == "m1"
    assert hello.payload["key"] == "secret-key"


def test_make_hello_omits_key_when_empty():
    """An empty key leaves the legacy payload structure byte-identical."""
    with_default = protocol.make_hello("m1", "host", "6.4.0")
    with_empty = protocol.make_hello("m1", "host", "6.4.0", "")
    assert "key" not in with_default.payload
    assert "key" not in with_empty.payload
    # Exactly the pre-existing four fields, nothing more.
    assert set(with_default.payload) == {
        "machine_id",
        "hostname",
        "se3_version",
        "protocol_version",
    }
    assert with_default.payload["protocol_version"] == protocol.PROTOCOL_VERSION


def test_make_hello_does_not_bump_protocol_version():
    """The additive field must not bump the protocol revision."""
    assert protocol.make_hello("m", "h", "v", "k").payload[
        "protocol_version"
    ] == protocol.PROTOCOL_VERSION


def test_hello_with_key_decodes_roundtrip():
    """An old server decoding a HELLO with a key does not crash; it ignores it.

    ``decode`` preserves the unknown-to-older-servers ``key`` in the payload,
    and a single-tenant server simply never reads it via ``.get``.
    """
    msg = protocol.make_hello("m1", "host", "6.4.0", "the-key")
    decoded = protocol.decode(msg.to_json())
    assert decoded.type == protocol.MSG_HELLO
    assert decoded.payload["machine_id"] == "m1"
    assert decoded.payload["key"] == "the-key"


# --------------------------------------------------------------------------
# DaemonClient — HELLO carries the key, WELCOME reject stops reconnects
# --------------------------------------------------------------------------


def _make_client(**kw) -> DaemonClient:
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


class _SessionWS:
    """A WebSocket stand-in that captures sends and blocks on receive."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Block forever; the receive task is cancelled when the session unwinds.
        await asyncio.Event().wait()
        raise StopAsyncIteration  # pragma: no cover - never reached


class _FakeConn:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


class _FakeWebsockets:
    """Minimal ``websockets`` module stand-in for :meth:`DaemonClient._session`."""

    def __init__(self, ws):
        self._ws = ws

    def connect(self, url, **kw):
        return _FakeConn(self._ws)


def _run_one_session(client: DaemonClient, ws: _SessionWS) -> None:
    """Run a single ``_session`` that unwinds immediately via a set stop event."""

    async def scenario():
        stop = asyncio.Event()
        stop.set()  # stop_task wins the race right after HELLO is sent
        await client._session(stop, _FakeWebsockets(ws))

    asyncio.run(scenario())


def test_session_hello_carries_configured_key():
    client = _make_client(daemon_key="secret-key-123")
    ws = _SessionWS()
    _run_one_session(client, ws)
    hellos = [m for m in ws.sent if m.type == protocol.MSG_HELLO]
    assert len(hellos) == 1
    assert hellos[0].payload.get("key") == "secret-key-123"


def test_session_hello_omits_key_when_unconfigured():
    client = _make_client()  # no daemon_key
    ws = _SessionWS()
    _run_one_session(client, ws)
    hellos = [m for m in ws.sent if m.type == protocol.MSG_HELLO]
    assert len(hellos) == 1
    assert "key" not in hellos[0].payload


def test_daemon_key_is_not_logged(caplog):
    client = _make_client(daemon_key="TOP-SECRET-KEY")
    ws = _SessionWS()
    with caplog.at_level(logging.DEBUG, logger="se3.daemon.client"):
        _run_one_session(client, ws)
    for record in caplog.records:
        assert "TOP-SECRET-KEY" not in record.getMessage()


def test_handle_welcome_accepted_is_noop():
    client = _make_client(daemon_key="k")
    client._auth_rejected_event = asyncio.Event()
    client._handle_welcome({"accepted": True})
    assert client._auth_rejected is False
    assert client._auth_rejected_event.is_set() is False


def test_handle_welcome_rejected_flags_and_signals():
    client = _make_client(daemon_key="k")
    client._auth_rejected_event = asyncio.Event()
    client._handle_welcome({"accepted": False, "reason": "unknown daemon key"})
    assert client._auth_rejected is True
    assert client.last_error == "unknown daemon key"
    assert client._auth_rejected_event.is_set() is True


def test_handle_welcome_rejected_without_reason_has_fallback():
    client = _make_client()
    client._auth_rejected_event = asyncio.Event()
    client._handle_welcome({"accepted": False})
    assert client._auth_rejected is True
    assert client.last_error  # a non-empty fallback reason


def test_dispatch_welcome_reject_does_not_leak_key(caplog):
    """The rejection log carries the server reason but never the daemon key."""
    client = _make_client(daemon_key="MY-DAEMON-KEY")
    client._auth_rejected_event = asyncio.Event()

    async def scenario():
        await client._dispatch(
            _SessionWS(),
            protocol.make_welcome("srv", accepted=False, reason="bad credential"),
        )

    with caplog.at_level(logging.DEBUG, logger="se3.daemon.client"):
        asyncio.run(scenario())
    assert client._auth_rejected is True
    for record in caplog.records:
        assert "MY-DAEMON-KEY" not in record.getMessage()


def test_run_loop_stops_after_auth_rejection():
    """A rejected credential halts the run loop instead of reconnecting forever."""
    client = _make_client(daemon_key="bad")
    sessions = []

    async def fake_session(stop_event, websockets):
        sessions.append(1)
        # Simulate the server answering HELLO with accepted=false.
        client._auth_rejected = True
        client._last_error = "unknown daemon key"

    client._session = fake_session  # type: ignore[assignment]

    async def scenario():
        # run() must return on its own — we never set the stop event. If the
        # auth-stop branch were missing, run() would reconnect forever and the
        # wait_for would time out.
        await asyncio.wait_for(client.run(asyncio.Event()), timeout=2.0)

    asyncio.run(scenario())
    assert sessions == [1]  # exactly one session: no reconnect storm
    assert client.last_error == "unknown daemon key"


def test_run_loop_keeps_reconnecting_on_transient_failure():
    """A transient failure (not a rejection) does not set the auth-stop flag."""
    client = _make_client()
    sessions = []

    async def fake_session(stop_event, websockets):
        sessions.append(1)
        stop_event.set()  # end the loop deterministically after this session
        raise RuntimeError("network blip")

    client._session = fake_session  # type: ignore[assignment]

    async def scenario():
        await asyncio.wait_for(client.run(asyncio.Event()), timeout=2.0)

    asyncio.run(scenario())
    assert sessions == [1]
    assert client._auth_rejected is False
    assert client.last_error == "network blip"


# --------------------------------------------------------------------------
# DaemonConfig / Daemon wiring + status never echoes the key
# --------------------------------------------------------------------------


def test_daemon_config_daemon_key_defaults_to_none():
    assert DaemonConfig().daemon_key is None
    assert DaemonConfig(daemon_key="k").daemon_key == "k"


def test_daemon_passes_key_to_client():
    daemon = Daemon(DaemonConfig(server_url="ws://server", daemon_key="abc"))

    async def scenario():
        daemon._stop_event = asyncio.Event()
        daemon._stop_event.set()  # client.run() exits immediately, no dial
        task = daemon._start_server_client()
        assert task is not None
        await task

    asyncio.run(scenario())
    assert daemon._client is not None
    assert daemon._client._daemon_key == "abc"


def test_daemon_without_key_passes_empty_string_to_client():
    daemon = Daemon(DaemonConfig(server_url="ws://server"))  # no key

    async def scenario():
        daemon._stop_event = asyncio.Event()
        daemon._stop_event.set()
        await daemon._start_server_client()

    asyncio.run(scenario())
    assert daemon._client._daemon_key == ""


def test_daemon_status_does_not_echo_key(tmp_path):
    cfg = DaemonConfig(server_url="ws://s", daemon_key="SECRET-KEY", pid_dir=tmp_path)
    daemon = Daemon(cfg)
    daemon._write_pidfile()
    snapshot = daemon.aggregator.get_snapshot()
    daemon._write_status(snapshot, [])

    status = daemon_status(cfg)
    assert "SECRET-KEY" not in json.dumps(status, default=str)
    # The on-disk pidfile and status file must not carry the key either.
    assert "SECRET-KEY" not in cfg.pid_file.read_text(encoding="utf-8")
    assert "SECRET-KEY" not in cfg.status_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# CLI: --daemon-key flag and SE3_DAEMON_KEY env fallback
# --------------------------------------------------------------------------


@pytest.fixture()
def cli_capture(monkeypatch):
    """Patch start_daemon to capture the resolved DaemonConfig and not fork."""
    from se3 import daemon as daemon_pkg

    captured = {}

    def fake_start(config, foreground=False):
        captured["config"] = config
        return {"status": "started", "pid": 4321}

    monkeypatch.setattr(daemon_pkg, "start_daemon", fake_start)
    return captured


def _invoke_daemon_start(args):
    from typer.testing import CliRunner

    from se3 import cli

    return CliRunner().invoke(cli.app, ["daemon", "start", *args])


def test_cli_daemon_start_passes_explicit_key(cli_capture, monkeypatch):
    monkeypatch.delenv("SE3_DAEMON_KEY", raising=False)
    result = _invoke_daemon_start(["--daemon-key", "key-from-cli"])
    assert result.exit_code == 0
    assert cli_capture["config"].daemon_key == "key-from-cli"


def test_cli_daemon_start_reads_key_from_env(cli_capture, monkeypatch):
    monkeypatch.setenv("SE3_DAEMON_KEY", "key-from-env")
    result = _invoke_daemon_start([])
    assert result.exit_code == 0
    assert cli_capture["config"].daemon_key == "key-from-env"


def test_cli_daemon_start_flag_overrides_env(cli_capture, monkeypatch):
    monkeypatch.setenv("SE3_DAEMON_KEY", "env-key")
    result = _invoke_daemon_start(["--daemon-key", "flag-key"])
    assert result.exit_code == 0
    assert cli_capture["config"].daemon_key == "flag-key"


def test_cli_daemon_start_no_key_is_none(cli_capture, monkeypatch):
    monkeypatch.delenv("SE3_DAEMON_KEY", raising=False)
    result = _invoke_daemon_start([])
    assert result.exit_code == 0
    assert cli_capture["config"].daemon_key is None
