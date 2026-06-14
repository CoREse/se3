"""Tests for the daemon-side WebSocket client and its server integration."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from se3.daemon import protocol
from se3.daemon.client import (
    DaemonClient,
    _default_respond_handler,
    _format_exc,
    _normalize_ws_url,
)
from se3.daemon.daemon import Daemon, DaemonConfig


# --------------------------------------------------------------------------
# URL normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        # Host already carries an explicit port -> kept verbatim.
        ("localhost:8080", "ws://localhost:8080/ws"),
        ("ws://host:9", "ws://host:9/ws"),
        ("http://host:8080", "ws://host:8080/ws"),
        ("ws://host:8080/ws", "ws://host:8080/ws"),
        ("host:8080/", "ws://host:8080/ws"),
        # A non-default explicit port is never overwritten.
        ("ws://host:9000", "ws://host:9000/ws"),
        ("ws://host:9000/ws", "ws://host:9000/ws"),
        # No port -> a scheme-aware default is filled in: ws/http -> 8080,
        # wss/https -> 443 (TLS terminates at the reverse proxy's HTTPS port).
        ("ws://host", "ws://host:8080/ws"),
        ("wss://host", "wss://host:443/ws"),
        ("https://host", "wss://host:443/ws"),
        ("http://host", "ws://host:8080/ws"),
        ("host", "ws://host:8080/ws"),
        ("host/ws", "ws://host:8080/ws"),
        # An explicit port is always preserved, even when it equals a default
        # for the *other* scheme (no scheme-aware overwrite of explicit ports).
        ("wss://host:443", "wss://host:443/ws"),
        ("wss://host:8080", "wss://host:8080/ws"),
        ("wss://host:9000", "wss://host:9000/ws"),
        # A custom path is preserved alongside the filled-in port.
        ("ws://host/daemon", "ws://host:8080/daemon"),
        ("ws://host:9000/daemon", "ws://host:9000/daemon"),
        ("wss://host/daemon", "wss://host:443/daemon"),
        ("wss://host/ws", "wss://host:443/ws"),
        # IPv6 literals: brackets are not mistaken for a port separator, and
        # the scheme-aware default still applies.
        ("ws://[::1]", "ws://[::1]:8080/ws"),
        ("wss://[::1]", "wss://[::1]:443/ws"),
        ("ws://[::1]:9000", "ws://[::1]:9000/ws"),
        ("ws://[::1]:9000/ws", "ws://[::1]:9000/ws"),
    ],
)
def test_normalize_ws_url(given, expected):
    assert _normalize_ws_url(given) == expected


def test_normalize_ws_url_default_port_is_shared_constant():
    """The filled-in port comes from protocol.DEFAULT_SERVER_PORT."""
    assert _normalize_ws_url("ws://host") == f"ws://host:{protocol.DEFAULT_SERVER_PORT}/ws"


def test_normalize_ws_url_tls_default_port_is_shared_constant():
    """The wss fill-in port comes from protocol.DEFAULT_SERVER_TLS_PORT."""
    assert protocol.DEFAULT_SERVER_PORT == 8080
    assert protocol.DEFAULT_SERVER_TLS_PORT == 443
    assert (
        _normalize_ws_url("wss://host")
        == f"wss://host:{protocol.DEFAULT_SERVER_TLS_PORT}/ws"
    )


# --------------------------------------------------------------------------
# connection-failure reason formatting (last_error diagnostics)
# --------------------------------------------------------------------------


def test_format_exc_falls_back_to_type_name_when_str_is_empty():
    """asyncio.TimeoutError stringifies to '' — the empty-parens root cause.

    The open_timeout firing (e.g. a wss:// URL dialed at the wrong port) raises
    a TimeoutError whose str() is empty; the formatter must still yield a
    non-empty, readable reason so ``se3 daemon status`` never shows
    ``not connected ()``.
    """
    assert str(asyncio.TimeoutError()) == ""  # documents the root cause
    assert _format_exc(asyncio.TimeoutError()) == "TimeoutError"


def test_format_exc_includes_message_when_present():
    assert _format_exc(ValueError("boom")) == "ValueError: boom"


def test_format_exc_is_always_nonempty_and_readable():
    for exc in (
        asyncio.TimeoutError(),
        ConnectionRefusedError(),
        OSError(),
        ValueError("x"),
    ):
        reason = _format_exc(exc)
        assert reason and reason.strip()
        # Never the bare, information-free literal that produced empty parens.
        assert reason != "not connected"


def test_run_records_nonempty_last_error_on_connection_failure():
    """A failed dial records a non-empty, readable reason in ``last_error``.

    Connecting to a port with nothing listening makes ``websockets.connect``
    raise; ``run`` must format that into a non-empty reason (not the old bare
    ``str(exc)`` that could be empty), which ``se3 daemon status`` then surfaces.
    """
    pytest.importorskip("websockets")
    port = _free_port()  # nothing is listening here

    client = DaemonClient(
        f"ws://127.0.0.1:{port}",
        machine_id="m-fail",
        hostname="fail-host",
        se3_version="6.4.0",
        snapshot_provider=lambda: {},
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        for _ in range(200):
            if client.last_error:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return client.last_error

    err = asyncio.run(scenario())
    assert err and err.strip()
    assert err != "not connected"


def test_welcome_rejection_records_reason_in_last_error():
    """The WELCOME(accepted=false) failure path also writes a readable reason.

    Beyond a failed dial / handshake timeout (covered above) and the empty
    ``str()`` of :class:`asyncio.TimeoutError` (``_format_exc`` tests), an
    authentication rejection — the server resolving the daemon key to no owner —
    is another failure path that must leave ``last_error`` non-empty so
    ``se3 daemon status`` surfaces *why* the daemon is ``not connected``. The
    full behavior (the reject also halts the reconnect storm and never leaks the
    key) lives in ``tests/test_daemon_key_hello.py``
    (``test_handle_welcome_rejected_flags_and_signals`` and friends); this is a
    focused diagnostics assertion that the rejection reason reaches the same
    ``last_error`` channel as the connection-failure reasons above.
    """
    client = DaemonClient(
        "ws://server",
        machine_id="m-reject",
        hostname="h",
        se3_version="6.4.0",
        snapshot_provider=lambda: {},
        daemon_key="k",
    )
    client._auth_rejected_event = asyncio.Event()
    client._handle_welcome({"accepted": False, "reason": "unknown daemon key"})
    assert client.last_error == "unknown daemon key"
    assert client.last_error.strip()
    assert client.last_error != "not connected"


# --------------------------------------------------------------------------
# response-file writer
# --------------------------------------------------------------------------


def test_default_respond_handler_writes_file(tmp_path):
    _default_respond_handler("call-7", str(tmp_path), {"answer": "go"})
    target = tmp_path / "se3" / "calls" / "call-7.response.json"
    assert target.is_file()
    payload = json.loads(target.read_text())
    assert payload["call_id"] == "call-7"
    assert payload["response"] == {"answer": "go"}
    assert payload["source"] == "daemon-client"


# --------------------------------------------------------------------------
# message dispatch (no real socket)
# --------------------------------------------------------------------------


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


def _make_client(**kw):
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


def test_dispatch_ping_replies_pong():
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(ws, protocol.make_ping(seq=11))

    asyncio.run(scenario())
    assert len(ws.sent) == 1
    assert ws.sent[0].type == protocol.MSG_PONG
    assert ws.sent[0].seq == 11


def test_dispatch_spawn_flow_routes_to_handler():
    received = []
    client = _make_client(
        spawn_handler=lambda t, p, ty, d: received.append((t, p, ty, d))
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Implement Y", project_root="/p", task_type="bugfix"),
        )

    asyncio.run(scenario())
    assert received == [("Implement Y", "/p", "bugfix", False)]


def test_dispatch_spawn_flow_threads_discover_flag():
    received = []
    client = _make_client(
        spawn_handler=lambda t, p, ty, d: received.append((t, p, ty, d))
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Explore Z", discover=True),
        )

    asyncio.run(scenario())
    assert received == [("Explore Z", "", "feature", True)]


def test_dispatch_spawn_flow_threads_worktree_flag():
    """A worktree:true payload reaches the handler as a worktree= keyword."""
    received = []
    client = _make_client(
        spawn_handler=lambda t, p, ty, d, *, worktree=False: received.append(
            (t, p, ty, d, worktree)
        )
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Isolate W", worktree=True),
        )

    asyncio.run(scenario())
    assert received == [("Isolate W", "", "feature", False, True)]


def test_dispatch_spawn_flow_omits_worktree_keyword_when_false():
    """A non-isolated spawn keeps the legacy 4-positional handler call shape."""
    received = []
    # A handler that does NOT accept a worktree keyword must still be callable
    # for the default (worktree=False) path — proving backward compatibility.
    client = _make_client(
        spawn_handler=lambda t, p, ty, d: received.append((t, p, ty, d))
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Plain", project_root="/p"),
        )

    asyncio.run(scenario())
    assert received == [("Plain", "/p", "feature", False)]


def test_make_spawn_flow_omits_worktree_key_when_false():
    """The wire payload stays backward compatible when worktree is false."""
    msg = protocol.make_spawn_flow("t", project_root="/p")
    assert "worktree" not in msg.payload
    msg2 = protocol.make_spawn_flow("t", project_root="/p", worktree=True)
    assert msg2.payload["worktree"] is True


def test_dispatch_spawn_flow_runs_ensure_handler_first():
    """ensure_handler is called before the spawn_handler with project_root."""
    ensure_calls = []
    spawn_calls = []

    class _Ensure:
        error = ""

    client = _make_client(
        ensure_handler=lambda root: (ensure_calls.append(root) or _Ensure()),
        spawn_handler=lambda t, p, ty, d: spawn_calls.append((t, p, ty, d)),
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("T", project_root="/some/path"),
        )

    asyncio.run(scenario())
    assert ensure_calls == ["/some/path"]
    assert spawn_calls == [("T", "/some/path", "feature", False)]


def test_dispatch_spawn_flow_aborts_when_ensure_returns_error():
    """A truthy ensure.error short-circuits — spawn_handler is not called."""
    spawn_calls = []

    class _Ensure:
        error = "se3 init exited non-zero"

    client = _make_client(
        ensure_handler=lambda root: _Ensure(),
        spawn_handler=lambda *a: spawn_calls.append(a),
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("T", project_root="/bad/path"),
        )

    asyncio.run(scenario())
    assert spawn_calls == []


def test_dispatch_spawn_flow_skips_ensure_when_no_project_root():
    """No project_root in payload means no ensure call — spawn still runs."""
    ensure_calls = []
    spawn_calls = []
    client = _make_client(
        ensure_handler=lambda root: ensure_calls.append(root),
        spawn_handler=lambda *a: spawn_calls.append(a),
    )

    async def scenario():
        await client._dispatch(_FakeWS(), protocol.make_spawn_flow("T"))

    asyncio.run(scenario())
    assert ensure_calls == []
    assert spawn_calls == [("T", "", "feature", False)]


def test_dispatch_spawn_flow_ignores_empty_task():
    received = []
    client = _make_client(spawn_handler=lambda *a: received.append(a))

    async def scenario():
        await client._dispatch(_FakeWS(), protocol.make_spawn_flow("   "))

    asyncio.run(scenario())
    assert received == []


# --------------------------------------------------------------------------
# dispatch: SPAWN_FLOW with resume_flow_id routes to resume_handler
# --------------------------------------------------------------------------


def test_dispatch_spawn_flow_with_resume_flow_id_routes_to_resume_handler():
    """When resume_flow_id is present, _handle_spawn calls resume_handler."""
    resume_calls = []
    spawn_calls = []
    client = _make_client(
        resume_handler=lambda fid, root: resume_calls.append((fid, root)),
        spawn_handler=lambda t, p, ty, d: spawn_calls.append((t, p, ty, d)),
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow(
                "",  # task_description unused for resume
                project_root="/proj",
                resume_flow_id="flow-abc",
            ),
        )

    asyncio.run(scenario())
    assert resume_calls == [("flow-abc", "/proj")]
    assert spawn_calls == []  # spawn handler must NOT be called


def test_dispatch_spawn_flow_without_resume_flow_id_routes_to_spawn_handler():
    """Normal spawn (no resume_flow_id) still goes to spawn_handler."""
    resume_calls = []
    spawn_calls = []
    client = _make_client(
        resume_handler=lambda fid, root: resume_calls.append((fid, root)),
        spawn_handler=lambda t, p, ty, d: spawn_calls.append((t, p, ty, d)),
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Build X", project_root="/proj"),
        )

    asyncio.run(scenario())
    assert resume_calls == []
    assert spawn_calls == [("Build X", "/proj", "feature", False)]


def test_dispatch_spawn_flow_resume_without_handler_logs_warning():
    """resume_flow_id present but no resume_handler -> warning, no crash."""
    spawn_calls = []
    client = _make_client(
        resume_handler=None,
        spawn_handler=lambda t, p, ty, d: spawn_calls.append((t, p, ty, d)),
    )

    async def scenario():
        # Must not raise.
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow(
                "", project_root="/proj", resume_flow_id="flow-x"
            ),
        )

    asyncio.run(scenario())
    assert spawn_calls == []  # neither handler was called


def test_dispatch_respond_call_routes_to_handler(tmp_path):
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(
            ws,
            protocol.make_respond_call("c9", "the-answer", project_root=str(tmp_path)),
        )

    asyncio.run(scenario())
    target = tmp_path / "se3" / "calls" / "c9.response.json"
    assert target.is_file()


# --------------------------------------------------------------------------
# MSG_ISSUE_COMMAND dispatch
# --------------------------------------------------------------------------


def _make_issue_client(tmp_path, *, project_roots=None):
    """Build a DaemonClient wired with a project root for issue command tests."""
    roots = project_roots or [str(tmp_path)]
    return _make_client(
        snapshot_provider=lambda: {
            "machine_id": "m1",
            "flows": [],
            "project_roots": roots,
        }
    )


def test_dispatch_issue_command_create(tmp_path):
    """ISSUE_COMMAND create creates an issue via IssueManager."""
    import yaml

    client = _make_issue_client(tmp_path)

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(tmp_path),
            description="Something is broken",
            title="Fix it",
            priority="high",
            type="bug",
            tags=["auto"],
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    issues_dir = tmp_path / "se3" / "issues" / "open"
    assert issues_dir.is_dir()
    files = list(issues_dir.glob("*.yaml"))
    assert len(files) == 1
    data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert data["description"] == "Something is broken"
    assert data["title"] == "Fix it"
    assert data["priority"] == "high"
    assert data["type"] == "bug"
    assert data["source"] == "human"  # web-initiated


def test_dispatch_issue_command_edit(tmp_path):
    """ISSUE_COMMAND edit updates an existing issue."""
    import yaml

    from se3.engine.issue_manager import IssueManager

    mgr = IssueManager(tmp_path)
    issue = mgr.create(description="Old description", title="Old title")

    client = _make_issue_client(tmp_path)

    async def scenario():
        msg = protocol.make_issue_command(
            "edit",
            project_root=str(tmp_path),
            issue_id=issue.id,
            description="New description",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    loaded = mgr.load(issue.id)
    assert loaded is not None
    assert loaded.description == "New description"


def test_dispatch_issue_command_close(tmp_path):
    """ISSUE_COMMAND close closes an issue."""
    from se3.engine.issue_manager import IssueManager

    mgr = IssueManager(tmp_path)
    issue = mgr.create(description="To be closed")

    client = _make_issue_client(tmp_path)

    async def scenario():
        msg = protocol.make_issue_command(
            "close",
            project_root=str(tmp_path),
            issue_id=issue.id,
            reason="Fixed",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    loaded = mgr.load(issue.id)
    assert loaded is not None
    assert loaded.status.value in ("closed", "resolved")


def test_dispatch_issue_command_reopen(tmp_path):
    """ISSUE_COMMAND reopen reopens a closed issue."""
    from se3.engine.issue_manager import IssueManager

    mgr = IssueManager(tmp_path)
    issue = mgr.create(description="To reopen")
    mgr.close_issue(issue.id)

    client = _make_issue_client(tmp_path)

    async def scenario():
        msg = protocol.make_issue_command(
            "reopen",
            project_root=str(tmp_path),
            issue_id=issue.id,
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    loaded = mgr.load(issue.id)
    assert loaded is not None
    assert loaded.status.value == "open"


def test_dispatch_issue_command_rejects_unregistered_root(tmp_path):
    """ISSUE_COMMAND rejects project_root not in known roots."""
    from se3.engine.issue_manager import IssueManager

    other = tmp_path / "other"
    other.mkdir()
    client = _make_issue_client(tmp_path, project_roots=["/some/other/path"])

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(other),
            description="Should fail",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())
    # No issue should have been created
    assert not (other / "se3" / "issues").exists()


def test_dispatch_issue_command_rejects_relative_path():
    """ISSUE_COMMAND rejects non-absolute project_root."""
    client = _make_client()

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root="relative/path",
            description="Should fail",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())


def test_dispatch_issue_command_ignores_empty_operation():
    """ISSUE_COMMAND with empty operation is a no-op."""
    client = _make_client()

    async def scenario():
        msg = protocol.Message(
            type=protocol.MSG_ISSUE_COMMAND,
            payload={"operation": "", "project_root": "/p"},
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())  # must not raise


def test_issue_command_reuses_cached_project_roots_without_snapshot(tmp_path):
    """With a warm cache, the issue command must not rebuild the snapshot.

    The heavy snapshot provider (which walks se3/history) is the cause of the
    ack delay; a populated ``_last_known_project_roots`` cache means the
    project_root validation reuses it and never invokes the provider.
    """
    calls = {"n": 0}

    def _counting_snapshot():
        calls["n"] += 1
        return {"machine_id": "m1", "flows": [], "project_roots": [str(tmp_path)]}

    client = _make_client(snapshot_provider=_counting_snapshot)
    # Warm the cache exactly as a prior STATUS_UPDATE would have.
    client._last_known_project_roots = {str(Path(tmp_path).resolve())}

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(tmp_path),
            description="Cached path",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    # Snapshot provider was never called on the issue hot path.
    assert calls["n"] == 0
    # The issue still landed on disk.
    files = list((tmp_path / "se3" / "issues" / "open").glob("*.yaml"))
    assert len(files) == 1


def test_issue_command_falls_back_to_snapshot_when_cache_cold(tmp_path):
    """With no cache yet, the handler builds one snapshot and validates."""
    calls = {"n": 0}

    def _counting_snapshot():
        calls["n"] += 1
        return {"machine_id": "m1", "flows": [], "project_roots": [str(tmp_path)]}

    client = _make_client(snapshot_provider=_counting_snapshot)
    assert client._last_known_project_roots is None

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(tmp_path),
            description="Cold path",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    # Snapshot built exactly once for validation, then cached for next time.
    assert calls["n"] == 1
    assert client._last_known_project_roots == {str(Path(tmp_path).resolve())}
    files = list((tmp_path / "se3" / "issues" / "open").glob("*.yaml"))
    assert len(files) == 1


def test_issue_command_replies_failure_when_cold_snapshot_raises(tmp_path):
    """A cold-cache snapshot that raises must fail closed, not write disk.

    When ``_last_known_project_roots`` is None the handler builds one snapshot
    to validate ``project_root``. If that provider raises (filesystem error,
    corrupt state, permission issue), the handler must send a failure ack and
    never reach the IssueManager write.
    """

    def _failing_snapshot():
        raise OSError("disk gone")

    client = _make_client(snapshot_provider=_failing_snapshot)
    assert client._last_known_project_roots is None
    ws = _FakeWS()

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(tmp_path),
            description="Should not persist",
            request_id="req-cold-fail",
        )
        await client._dispatch(ws, msg)

    asyncio.run(scenario())

    # A failure ack was sent, echoing the request_id.
    acks = [m for m in ws.sent if m.type == protocol.MSG_ISSUE_RESULT]
    assert len(acks) == 1
    ack = acks[0]
    assert ack.payload["request_id"] == "req-cold-fail"
    assert ack.payload["ok"] is False
    assert ack.payload["error"] == "snapshot lookup failed"
    # The cache stays cold so a later snapshot can still populate it.
    assert client._last_known_project_roots is None
    # No issue was written to disk.
    issues_dir = tmp_path / "se3" / "issues" / "open"
    assert not issues_dir.exists() or not list(issues_dir.glob("*.yaml"))


def test_issue_command_replies_before_fast_push(tmp_path):
    """The MSG_ISSUE_RESULT ack must be sent before _trigger_fast_push().

    The ack is the frame the server blocks on within ISSUE_COMMAND_TIMEOUT, so
    it must precede the heavier fast-push that only schedules a follow-up
    STATUS_UPDATE.
    """
    client = _make_issue_client(tmp_path)
    order = []

    real_send = client._send

    async def _recording_send(ws, message):
        if message.type == protocol.MSG_ISSUE_RESULT:
            order.append("ack")
        return await real_send(ws, message)

    def _recording_fast_push():
        order.append("fast_push")

    client._send = _recording_send
    client._trigger_fast_push = _recording_fast_push

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(tmp_path),
            description="Ordering",
            request_id="req-order",
        )
        await client._dispatch(_FakeWS(), msg)

    asyncio.run(scenario())

    assert order == ["ack", "fast_push"]


def test_issue_command_ack_echoes_request_id_and_issue_id(tmp_path):
    """The ack echoes request_id, ok=True and the created issue_id."""
    client = _make_issue_client(tmp_path)
    ws = _FakeWS()

    async def scenario():
        msg = protocol.make_issue_command(
            "create",
            project_root=str(tmp_path),
            description="Echo fields",
            request_id="req-123",
        )
        await client._dispatch(ws, msg)

    asyncio.run(scenario())

    acks = [m for m in ws.sent if m.type == protocol.MSG_ISSUE_RESULT]
    assert len(acks) == 1
    ack = acks[0]
    assert ack.payload["request_id"] == "req-123"
    assert ack.payload["ok"] is True
    # issue_id is the freshly assigned id (non-empty).
    assert ack.payload["issue_id"]


def test_push_status_refreshes_project_roots_cache(tmp_path):
    """A successful _push_status populates the project_roots cache."""
    client = _make_client(
        snapshot_provider=lambda: {
            "machine_id": "m1",
            "flows": [],
            "project_roots": [str(tmp_path)],
        }
    )
    assert client._last_known_project_roots is None

    async def scenario():
        await client._push_status(_FakeWS())

    asyncio.run(scenario())
    assert client._last_known_project_roots == {str(tmp_path)}


def test_push_status_sends_status_update():
    client = _make_client(snapshot_provider=lambda: {"machine_id": "m1", "flows": []})
    ws = _FakeWS()

    async def scenario():
        await client._push_status(ws)

    asyncio.run(scenario())
    assert ws.sent[0].type == protocol.MSG_STATUS_UPDATE
    assert ws.sent[0].payload["snapshot"]["machine_id"] == "m1"


def test_push_status_does_not_block_event_loop():
    """A slow synchronous snapshot provider must be offloaded to a thread.

    The snapshot build walks ``se3/history`` and can take seconds on a large
    history; if it ran inline on the event loop it would starve the heartbeat
    and SPAWN_FLOW handling (the bug this fixes). We model the heavy build with
    a blocking ``time.sleep`` and assert a concurrently-scheduled coroutine
    (standing in for the heartbeat) keeps making progress while it runs.
    """

    def slow_provider():
        time.sleep(0.3)
        return {"machine_id": "m1", "flows": []}

    client = _make_client(snapshot_provider=slow_provider)
    ws = _FakeWS()

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        beat = asyncio.ensure_future(heartbeat())
        await client._push_status(ws)
        beat.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    # If the snapshot ran inline on the loop, the heartbeat would have been
    # frozen for the whole 0.3s and ticked at most ~once. Offloaded, it keeps
    # ticking throughout.
    assert ticks >= 5
    assert ws.sent[0].type == protocol.MSG_STATUS_UPDATE


def test_resolve_interject_root_offloaded_and_matches_flow():
    """``_resolve_interject_root`` is async and finds the flow's project root."""
    snapshot = {
        "machine_id": "m1",
        "flows": [
            {"flow_id": "F1", "project_root": "/p/one"},
            {"flow_id": "F2", "project_root": "/p/two"},
        ],
    }
    client = _make_client(snapshot_provider=lambda: snapshot)

    async def scenario():
        return (
            await client._resolve_interject_root("F2"),
            await client._resolve_interject_root("missing"),
        )

    found, missing = asyncio.run(scenario())
    assert found == "/p/two"
    assert missing == ""


# --------------------------------------------------------------------------
# history push: signature-gated trigger + cursor pruning
# --------------------------------------------------------------------------


class _FakeHistoryProvider:
    """Minimal history provider for exercising the client's push debounce."""

    def __init__(self):
        self.signature: dict = {}
        self.reads: list = []  # FlowRead-likes returned by read_active_flows

    def build_index(self):
        return []

    def active_flow_signature(self):
        return dict(self.signature)

    def read_active_flows(self, cursors):
        return list(self.reads)


def test_history_changed_detects_signature_delta():
    """_history_changed reports a change only when the signature actually moves."""
    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)

    provider.signature = {"f1": (1, 10)}
    assert client._history_changed() is True  # changed from the initial {}
    assert client._history_changed() is False  # unchanged -> debounced

    provider.signature = {"f1": (2, 20)}
    assert client._history_changed() is True  # engine.json / jsonl advanced


def test_history_changed_without_provider_is_false():
    client = _make_client()  # no history_provider
    assert client._history_changed() is False


def test_push_history_prunes_drained_terminal_flow_cursor():
    """The cursor map keeps active/flushed flows and drops drained ones."""
    from se3.daemon.history import FlowRead

    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)
    ws = _FakeWS()

    provider.reads = [
        FlowRead(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step_id": "s", "message": {"role": "user", "content": "x"}}],
            {"s.jsonl": 1},
        )
    ]
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 1}}
    data_frames = [m for m in ws.sent if m.type == protocol.MSG_HISTORY_DATA]
    assert len(data_frames) == 1

    # Next round: f1 is drained/terminal and no longer returned -> pruned.
    provider.reads = []
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {}


def test_push_history_keeps_empty_active_flow_cursor():
    """An active flow with an empty delta keeps its cursor (no re-snapshot)."""
    from se3.daemon.history import FlowRead

    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)
    ws = _FakeWS()

    provider.reads = [
        FlowRead("f1", protocol.HISTORY_MODE_APPEND, [], {"s.jsonl": 3})
    ]
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 3}}
    # Empty delta -> no HISTORY_DATA frame emitted.
    assert not [m for m in ws.sent if m.type == protocol.MSG_HISTORY_DATA]


# --------------------------------------------------------------------------
# index-refresh request handling (server -> daemon forced re-push)
# --------------------------------------------------------------------------


def test_dispatch_history_index_request_forces_index_push():
    """An index-refresh request re-pushes the index even when it is unchanged."""
    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)
    ws = _FakeWS()
    # Simulate an index already pushed and unchanged since (build_index() -> []).
    client._last_index = []

    async def scenario():
        await client._dispatch(ws, protocol.make_history_index_request())

    asyncio.run(scenario())
    index_frames = [m for m in ws.sent if m.type == protocol.MSG_HISTORY_INDEX]
    # Forced re-push: a HISTORY_INDEX is emitted despite the unchanged index.
    assert len(index_frames) == 1


def test_dispatch_history_index_request_without_provider_is_noop():
    """No history provider -> the request is safely ignored (no frames)."""
    client = _make_client()  # no history_provider
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(ws, protocol.make_history_index_request())

    asyncio.run(scenario())
    assert ws.sent == []


def test_dispatch_history_index_request_swallows_provider_errors():
    """A failing provider is logged and swallowed; the connection survives."""

    class _BoomProvider(_FakeHistoryProvider):
        def build_index(self):
            raise RuntimeError("boom")

    client = _make_client(history_provider=_BoomProvider())
    ws = _FakeWS()

    async def scenario():
        # Must not raise.
        await client._dispatch(ws, protocol.make_history_index_request())

    asyncio.run(scenario())
    assert not [m for m in ws.sent if m.type == protocol.MSG_HISTORY_INDEX]


def test_dispatch_history_index_request_invalidates_cache(tmp_path):
    """Force-index invalidates the build_index TTL cache so new flows are visible.

    Regression guard: without the invalidate call, a flow added within the
    TTL window would be invisible until the cache expires, contradicting the
    MSG_HISTORY_INDEX_REQUEST contract of "rebuild from disk immediately".
    """
    from se3.daemon.history import DaemonHistoryReader

    root = tmp_path / "proj"
    state = root / "se3" / "state"
    state.mkdir(parents=True)
    (state / "engine.json").write_text(
        json.dumps({"flow_id": "f1", "status": "RUNNING"}), encoding="utf-8"
    )
    hist = root / "se3" / "history" / "f1"
    hist.mkdir(parents=True)
    (hist / "01_analyze.jsonl").write_text(
        json.dumps({"role": "user", "content": "hello"}) + "\n", encoding="utf-8"
    )

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    # Prime the cache.
    r1 = reader.build_index()
    assert [m.flow_id for m in r1] == ["f1"]

    # Add a new flow; the cache is still warm so build_index would miss it.
    hist2 = root / "se3" / "history" / "f2"
    hist2.mkdir(parents=True)
    (hist2 / "01_analyze.jsonl").write_text(
        json.dumps({"role": "user", "content": "world"}) + "\n", encoding="utf-8"
    )

    # Wire up the client with the real reader.
    client = _make_client(history_provider=reader)
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(ws, protocol.make_history_index_request())

    asyncio.run(scenario())

    index_frames = [m for m in ws.sent if m.type == protocol.MSG_HISTORY_INDEX]
    assert len(index_frames) == 1
    pushed_ids = {meta["flow_id"] for meta in index_frames[0].payload["sessions"]}
    # After invalidation the new flow must be visible.
    assert "f2" in pushed_ids, (
        f"force-index did not rebuild from disk; got {pushed_ids}"
    )


# --------------------------------------------------------------------------
# Daemon integration
# --------------------------------------------------------------------------


def test_daemon_without_server_url_has_no_client():
    daemon = Daemon(DaemonConfig(server_url=None))
    assert daemon._start_server_client() is None


# --------------------------------------------------------------------------
# end-to-end: DaemonClient <-> live FastAPI server
# --------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int, seed_key: str | None = None):
    """Start a fresh uvicorn-hosted FastAPI server on *port* in a thread.

    When *seed_key* is given, that daemon key is also accepted by the new
    server (in addition to the freshly issued one). This lets a reconnecting
    client keep using its original key against a fresh server on the same port.
    """
    import uvicorn

    import se3.server.crypto as crypto
    from _authsrv import authed_app

    app, daemon_key = authed_app()
    if seed_key is not None:
        app.state.store.issue_daemon_key(
            app.state.test_owner_id, crypto.token_hash(seed_key)
        )
        daemon_key = seed_key
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 8
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn server did not start"
    return server, thread, daemon_key


def _stop_server(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=8)


async def _wait_connected(client: DaemonClient, *, want: bool = True, tries: int = 300):
    for _ in range(tries):
        if client.connected is want:
            return
        await asyncio.sleep(0.05)


def test_client_connects_reports_and_receives():
    """End-to-end: a DaemonClient connects to a live server, reports state,
    and the server's REST API can route a SPAWN_FLOW back to it."""
    import httpx

    port = _free_port()
    server, thread, daemon_key = _start_server(port)
    snapshot = {
        "machine_id": "m-e2e",
        "hostname": "e2e-host",
        "flows": [{"flow_id": "f-e2e", "status": "running"}],
    }
    spawned = []
    client = DaemonClient(
        f"ws://127.0.0.1:{port}",
        machine_id="m-e2e",
        hostname="e2e-host",
        se3_version="6.4.0",
        snapshot_provider=lambda: snapshot,
        spawn_handler=lambda t, p, ty, d: spawned.append((t, p, ty, d)),
        status_interval=0.2,
        daemon_key=daemon_key,
    )
    base = f"http://127.0.0.1:{port}"

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await _wait_connected(client)
        assert client.connected, f"client never connected: {client.last_error}"

        async with httpx.AsyncClient(base_url=base) as http:
            # Authenticate the REST client (cookie persists across requests).
            login_resp = await http.post(
                "/api/auth/login", json={"username": "admin", "password": "pw"}
            )
            assert login_resp.status_code == 200, login_resp.text
            # The server should learn our machine + flow from the pushed snapshot.
            for _ in range(300):
                resp = await http.get("/api/flows/f-e2e")
                if resp.status_code == 200:
                    break
                await asyncio.sleep(0.05)
            assert resp.status_code == 200
            assert resp.json()["machine_id"] == "m-e2e"

            # Downlink: publish a task -> server -> daemon SPAWN_FLOW.
            pub = await http.post(
                "/api/flows",
                json={
                    "machine_id": "m-e2e",
                    "task": "e2e task",
                    "task_type": "bugfix",
                    "project_root": "/p-e2e",
                },
            )
            assert pub.status_code == 202

        for _ in range(300):
            if spawned:
                break
            await asyncio.sleep(0.05)
        assert spawned == [("e2e task", "/p-e2e", "bugfix", False)]

        stop.set()
        await asyncio.wait_for(task, timeout=8)

    try:
        asyncio.run(scenario())
    finally:
        _stop_server(server, thread)


# --------------------------------------------------------------------------
# blocking disk I/O must not stall the event loop
# --------------------------------------------------------------------------


class _BlockingHistoryProvider:
    """Provider whose disk-read methods sleep synchronously to mimic a big session.

    A correct client offloads these calls with ``asyncio.to_thread``; a regression
    that calls them directly would block the event loop for ``block_seconds``.
    """

    def __init__(self, block_seconds: float = 0.6):
        self.block_seconds = block_seconds

    def build_index(self):
        time.sleep(self.block_seconds)
        return []

    def active_flow_signature(self):
        return {}

    def read_active_flows(self, cursors):
        time.sleep(self.block_seconds)
        return []

    def read_flow(self, flow_id, *, project_root=None, cursor=None):
        from se3.daemon.history import FlowRead

        time.sleep(self.block_seconds)
        return FlowRead(flow_id, protocol.HISTORY_MODE_FULL, [], {})


def _loop_responsiveness(stop_event: asyncio.Event, samples: list) -> asyncio.Task:
    """Run a probe coroutine that records max gap between scheduled wakeups.

    A blocked event loop will show a gap close to the block duration; a
    healthy loop stays near the sleep interval.
    """

    async def probe():
        last = time.monotonic()
        while not stop_event.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            samples.append(now - last)
            last = now

    return asyncio.create_task(probe())


def test_handle_history_request_does_not_block_event_loop():
    """A slow read_flow must not stall the event loop (offloaded to a thread)."""
    provider = _BlockingHistoryProvider(block_seconds=0.6)
    client = _make_client(history_provider=provider)
    ws = _FakeWS()

    async def scenario():
        stop = asyncio.Event()
        samples: list = []
        probe = _loop_responsiveness(stop, samples)
        # Yield once so the probe records a baseline before the slow read starts.
        await asyncio.sleep(0.05)
        await client._handle_history_request(
            ws, {"flow_id": "f-big", "project_root": None}
        )
        stop.set()
        await probe
        # The probe's worst single-gap must stay well under the synchronous
        # block window — a regression that ran read_flow inline on the loop
        # would show a gap close to 0.6 s.
        assert samples, "probe never ran"
        assert max(samples) < 0.25, (
            f"event loop was blocked: max gap {max(samples):.3f}s "
            f"with block_seconds={provider.block_seconds}"
        )
        # Sanity: the request still produced its HISTORY_DATA reply.
        assert any(m.type == protocol.MSG_HISTORY_DATA for m in ws.sent)

    asyncio.run(scenario())


def test_push_history_force_index_does_not_block_event_loop():
    """A slow build_index / read_active_flows must not stall the event loop."""
    provider = _BlockingHistoryProvider(block_seconds=0.6)
    client = _make_client(history_provider=provider)
    ws = _FakeWS()

    async def scenario():
        stop = asyncio.Event()
        samples: list = []
        probe = _loop_responsiveness(stop, samples)
        await asyncio.sleep(0.05)
        await client._push_history(ws, force_index=True)
        stop.set()
        await probe
        # Both build_index and read_active_flows block for 0.6 s synchronously;
        # without to_thread the loop would stall for ~1.2 s total.
        assert samples, "probe never ran"
        assert max(samples) < 0.25, (
            f"event loop was blocked: max gap {max(samples):.3f}s "
            f"with block_seconds={provider.block_seconds}"
        )

    asyncio.run(scenario())


def test_client_reconnects_after_server_drop():
    """The client must re-establish the connection (with backoff) after the
    server goes away and comes back."""
    port = _free_port()
    server, thread, daemon_key = _start_server(port)
    client = DaemonClient(
        f"ws://127.0.0.1:{port}",
        machine_id="m-recon",
        hostname="h",
        se3_version="6.4.0",
        snapshot_provider=lambda: {"machine_id": "m-recon", "flows": []},
        status_interval=0.2,
        daemon_key=daemon_key,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await _wait_connected(client)
        assert client.connected

        # Drop the server; the client should notice and start retrying.
        await asyncio.get_event_loop().run_in_executor(
            None, _stop_server, server, thread
        )
        await _wait_connected(client, want=False)
        assert not client.connected

        # Bring a fresh server back up on the same port; reuse the original
        # daemon key so the reconnecting client still authenticates.
        server2, thread2, _key2 = await asyncio.get_event_loop().run_in_executor(
            None, _start_server, port, daemon_key
        )
        try:
            await _wait_connected(client, tries=400)
            assert client.connected, f"client did not reconnect: {client.last_error}"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=8)
            _stop_server(server2, thread2)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# `se3 daemon status` connection-line rendering
# --------------------------------------------------------------------------


def _invoke_daemon_status(monkeypatch, status_payload):
    """Run ``se3 daemon status`` with a stubbed ``daemon_status`` and return stdout."""
    from typer.testing import CliRunner

    import se3.daemon as daemon_pkg
    from se3.cli import app

    monkeypatch.setattr(daemon_pkg, "daemon_status", lambda config: status_payload)
    result = CliRunner().invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.output
    return result.output


def test_status_shows_reason_when_last_error_present(monkeypatch):
    out = _invoke_daemon_status(
        monkeypatch,
        {
            "running": True,
            "pid": 123,
            "machine_id": "m",
            "server_url": "wss://se3.example",
            "connected": False,
            "last_error": "TimeoutError",
            "tracked_flows": [],
        },
    )
    assert "not connected (TimeoutError)" in out


def test_status_no_empty_parens_when_last_error_blank(monkeypatch):
    out = _invoke_daemon_status(
        monkeypatch,
        {
            "running": True,
            "pid": 123,
            "machine_id": "m",
            "server_url": "wss://se3.example",
            "connected": False,
            "last_error": "",
            "tracked_flows": [],
        },
    )
    # No empty parens, and no information-free "(not connected)" repetition.
    assert "not connected ()" not in out
    assert "not connected (not connected)" not in out
    assert "reason unavailable" in out


def test_status_connected_branch_unchanged(monkeypatch):
    out = _invoke_daemon_status(
        monkeypatch,
        {
            "running": True,
            "pid": 1,
            "machine_id": "m",
            "server_url": "wss://se3.example",
            "connected": True,
            "tracked_flows": [],
        },
    )
    assert "Connection: connected" in out


def test_status_local_only_branch_unchanged(monkeypatch):
    out = _invoke_daemon_status(
        monkeypatch,
        {
            "running": True,
            "pid": 1,
            "machine_id": "m",
            "server_url": None,
            "connected": False,
            "tracked_flows": [],
        },
    )
    assert "local-only" in out


# --------------------------------------------------------------------------
# WebSocket max-frame size (large MSG_HISTORY_DATA frames)
# --------------------------------------------------------------------------


def test_max_ws_message_bytes_is_large_enough():
    """The shared inbound frame cap must comfortably exceed today's ~39MB
    history frames; guard it against being lowered back near a library default.
    """
    assert hasattr(protocol, "MAX_WS_MESSAGE_BYTES")
    assert protocol.MAX_WS_MESSAGE_BYTES >= 64 * 1024 * 1024


def test_session_connects_with_raised_ws_max_size():
    """``_session`` must dial ``websockets.connect`` with ``max_size`` set to
    the shared protocol cap so the daemon's inbound 1 MiB default is removed
    and large ``MSG_HISTORY_DATA`` frames are no longer dropped.

    A fake ``websockets`` module records the ``connect`` kwargs, then its fake
    connection bails out of the ``async with`` immediately so the heavy
    HELLO / push session machinery never runs (no real network involved).
    """
    captured = {}

    class _BailOut(RuntimeError):
        pass

    class _FakeConn:
        async def __aenter__(self):
            # Connect kwargs are already recorded; abort before the session
            # body so we exercise only the connect call.
            raise _BailOut()

        async def __aexit__(self, *exc):
            return False

    class _FakeWebsockets:
        @staticmethod
        def connect(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeConn()

    client = _make_client()

    async def scenario():
        stop_event = asyncio.Event()
        with pytest.raises(_BailOut):
            await client._session(stop_event, _FakeWebsockets())

    asyncio.run(scenario())
    assert captured["kwargs"]["max_size"] == protocol.MAX_WS_MESSAGE_BYTES
    # Pre-existing connect parameters are untouched.
    assert captured["kwargs"]["open_timeout"] == 10


# --------------------------------------------------------------------------
# Group G2: worktree project_root resolution for respond / interject
# --------------------------------------------------------------------------


def _make_reader_root(tmp_path, flow_id, status="RUNNING"):
    """Write a minimal active engine.json so build_index lists *flow_id*."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": flow_id, "status": status}), encoding="utf-8"
    )


def test_resolve_flow_root_from_index_returns_attributed_root(tmp_path):
    """``_resolve_flow_root_from_index`` returns the history reader's root.

    This is the SAME ``project_root`` the history reader scopes ``read_flow`` /
    ``read_active_flows`` to, so respond / interject writes line up with the
    history-read path for a ``--worktree`` / discovery session.
    """
    from se3.daemon.history import DaemonHistoryReader

    _make_reader_root(tmp_path, "wt-flow")
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
    client = _make_client(history_provider=reader)

    assert client._resolve_flow_root_from_index("wt-flow") == str(tmp_path)
    # Unknown flow yields empty string (not a crash).
    assert client._resolve_flow_root_from_index("nope") == ""


def test_resolve_flow_root_from_index_without_provider_is_empty():
    """No history provider -> empty string, never an exception."""
    client = _make_client()
    assert client._resolve_flow_root_from_index("any") == ""


def test_interject_falls_back_to_history_index_root(tmp_path):
    """An interjection with no payload root and a snapshot that omits the flow
    resolves the root from the history index and writes the call file there."""
    from se3.daemon.history import DaemonHistoryReader

    _make_reader_root(tmp_path, "wt-flow")
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
    # Snapshot deliberately does NOT list wt-flow (e.g. a just-started flow the
    # per-flow poll set has not picked up yet), forcing the index fallback.
    client = _make_client(
        snapshot_provider=lambda: {"machine_id": "m1", "flows": []},
        history_provider=reader,
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_interject_flow("wt-flow", "please stop"),
        )

    asyncio.run(scenario())

    calls_dir = tmp_path / "se3" / "calls"
    assert calls_dir.is_dir()
    files = list(calls_dir.glob("*.json"))
    assert files, "interjection call file must be written under the index root"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload.get("kind") == "interjection"
    assert (payload.get("context") or {}).get("flow_id") == "wt-flow"
