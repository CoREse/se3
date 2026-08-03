"""Tests for the END_SESSION control-plane path (protocol + daemon + client).

Covers group G2 of the end-session feature:

* protocol: :data:`~tianluo.daemon.protocol.MSG_END_SESSION` registration and the
  :func:`~tianluo.daemon.protocol.make_end_session` constructor (round-trip +
  optional-field omission).
* spawner: :meth:`~tianluo.daemon.spawner.DaemonSpawner.end_session` argv assembly.
* daemon: :meth:`~tianluo.daemon.daemon.Daemon.request_end_session` flow lookup and
  the ValueError on an unknown flow.
* client: :meth:`~tianluo.daemon.client.DaemonClient._handle_end_session` routing,
  empty-flow_id ignore, project_root reverse-resolution, and handler-exception
  containment.
"""

from __future__ import annotations

import asyncio

import pytest

from tianluo.daemon import protocol
from tianluo.daemon import spawner as spawner_mod
from tianluo.daemon.client import DaemonClient
from tianluo.daemon.daemon import Daemon, DaemonConfig
from tianluo.daemon.spawner import DaemonSpawner


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


def test_msg_end_session_is_server_to_daemon():
    """END_SESSION is a server→daemon message, registered in the right sets."""
    assert protocol.MSG_END_SESSION == "end_session"
    assert protocol.MSG_END_SESSION in protocol.SERVER_TO_DAEMON
    assert protocol.MSG_END_SESSION in protocol.ALL_MESSAGE_TYPES
    assert protocol.MSG_END_SESSION not in protocol.DAEMON_TO_SERVER


def test_protocol_version_current():
    """END_SESSION was additive and did not itself bump PROTOCOL_VERSION; the
    version later advanced to "3" for the (unrelated) traffic-reduction
    messages, to "4" for the (also unrelated) presence signalling, to "5" for
    the (likewise unrelated) upload channel and to "6" for its fetch
    counterpart, so simply pin the current revision here."""
    assert protocol.PROTOCOL_VERSION == "6"


def test_make_end_session_roundtrip():
    """A full make_end_session payload survives a JSON round-trip intact."""
    msg = protocol.make_end_session(
        "flow-123", project_root="/proj", reason="cleanup"
    )
    assert msg.type == protocol.MSG_END_SESSION
    decoded = protocol.decode(msg.to_json())
    assert decoded.type == protocol.MSG_END_SESSION
    assert decoded.payload["flow_id"] == "flow-123"
    assert decoded.payload["project_root"] == "/proj"
    assert decoded.payload["reason"] == "cleanup"


def test_make_end_session_omits_empty_optionals():
    """Empty project_root is dropped from the wire; default reason kept."""
    msg = protocol.make_end_session("flow-1")
    assert msg.payload == {"flow_id": "flow-1", "reason": "user terminated"}
    assert "project_root" not in msg.payload

    msg2 = protocol.make_end_session("flow-1", reason="")
    assert msg2.payload == {"flow_id": "flow-1"}
    assert "reason" not in msg2.payload


# --------------------------------------------------------------------------
# spawner.end_session
# --------------------------------------------------------------------------


def _capturing_spawner(monkeypatch):
    """Return a spawner whose ``_launch`` records its argv instead of spawning."""
    monkeypatch.setattr(spawner_mod, "_resolve_se3_command", lambda: ["se3"])
    spawner = DaemonSpawner(login_shell_path=None)
    captured = {}

    def _fake_launch(args, cwd, task_description, env):
        captured["args"] = args
        captured["cwd"] = cwd
        captured["label"] = task_description
        return object()

    spawner._launch = _fake_launch  # type: ignore[assignment]
    return spawner, captured


def test_end_session_builds_argv(tmp_path, monkeypatch):
    spawner, captured = _capturing_spawner(monkeypatch)
    spawner.end_session("flow-xyz", project_root=str(tmp_path), pid=4242)
    args = captured["args"]
    assert args[0] == "se3"
    assert args[1] == "end-session"
    # flow_id is passed positionally (matching the CLI signature).
    assert args[2] == "flow-xyz"
    assert "-p" in args
    assert "--pid" in args
    assert args[args.index("--pid") + 1] == "4242"
    assert captured["label"] == "[end-session flow-xyz]"


def test_end_session_omits_pid_when_none(tmp_path, monkeypatch):
    spawner, captured = _capturing_spawner(monkeypatch)
    spawner.end_session("flow-xyz", project_root=str(tmp_path), pid=None)
    assert "--pid" not in captured["args"]


def test_end_session_uses_resolved_se3_command(tmp_path, monkeypatch):
    """argv is built from _resolve_se3_command(), never a hardcoded literal."""
    monkeypatch.setattr(
        spawner_mod, "_resolve_se3_command", lambda: ["python", "-m", "se3"]
    )
    spawner = DaemonSpawner(login_shell_path=None)
    captured = {}
    spawner._launch = lambda args, cwd, td, env: captured.setdefault("args", args)  # type: ignore[assignment]
    spawner.end_session("f1", project_root=str(tmp_path))
    assert captured["args"][:4] == ["python", "-m", "se3", "end-session"]


# --------------------------------------------------------------------------
# daemon.request_end_session
# --------------------------------------------------------------------------


class _StubSpawner:
    """Records end_session calls without launching a real subprocess."""

    def __init__(self):
        self.calls = []

    def end_session(self, flow_id, *, project_root=None, pid=None):
        self.calls.append((flow_id, project_root, pid))
        return object()


def test_request_end_session_locates_flow_and_spawns(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    daemon.spawner = _StubSpawner()  # type: ignore[assignment]
    daemon.supervisor.register(99999, str(proj), flow_id="flow-end")

    daemon.request_end_session("flow-end")

    assert len(daemon.spawner.calls) == 1
    flow_id, root, pid = daemon.spawner.calls[0]
    assert flow_id == "flow-end"
    assert root == str(proj.resolve())
    assert pid == 99999


def test_request_end_session_prefers_supplied_root(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    other = tmp_path / "main"
    other.mkdir()
    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    daemon.spawner = _StubSpawner()  # type: ignore[assignment]
    daemon.supervisor.register(12321, str(proj), flow_id="flow-end")

    daemon.request_end_session("flow-end", project_root=str(other))

    _, root, _ = daemon.spawner.calls[0]
    assert root == str(other.resolve())


def test_request_end_session_folds_worktree_root_to_main(tmp_path):
    """A server-supplied *worktree* project_root is normalized to its <main>.

    The server reports a worktree session's ``project_root`` as the
    ``<main>/tianluo/worktrees/<name>`` sandbox itself. ``request_end_session`` must
    fold it back to ``<main>`` so ``se3 end-session -p`` runs against the main
    repo and can locate/archive the worktree.
    """
    main = tmp_path / "main"
    (main / "tianluo").mkdir(parents=True)
    wt = main / "tianluo" / "worktrees" / "wt_x"
    wt.mkdir(parents=True)
    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    daemon.spawner = _StubSpawner()  # type: ignore[assignment]

    daemon.request_end_session("dangling-wt", project_root=str(wt))

    assert len(daemon.spawner.calls) == 1
    _, root, pid = daemon.spawner.calls[0]
    assert root == str(main.resolve())
    assert pid is None


def test_request_end_session_unknown_flow_raises(tmp_path):
    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    daemon.spawner = _StubSpawner()  # type: ignore[assignment]
    with pytest.raises(ValueError):
        daemon.request_end_session("flow-missing")
    assert daemon.spawner.calls == []


def test_request_end_session_unsupervised_flow_with_root_dispatches(tmp_path):
    """A dangling (non-live) flow is still endable when the server supplies a root.

    A PAUSED / FAILED / interrupted worktree session leaves no live ``se3 run``
    process, so it is absent from ``supervisor.flows``; the CLI can still archive
    it, so we must dispatch with no pid hint rather than reject the request.
    """
    main = tmp_path / "main"
    main.mkdir()
    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    daemon.spawner = _StubSpawner()  # type: ignore[assignment]
    # No supervised flow registered for this id.

    daemon.request_end_session("dangling-flow", project_root=str(main))

    assert len(daemon.spawner.calls) == 1
    flow_id, root, pid = daemon.spawner.calls[0]
    assert flow_id == "dangling-flow"
    assert root == str(main.resolve())
    assert pid is None


def test_request_end_session_empty_flow_raises(tmp_path):
    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    daemon.spawner = _StubSpawner()  # type: ignore[assignment]
    with pytest.raises(ValueError):
        daemon.request_end_session("")


# --------------------------------------------------------------------------
# client._handle_end_session
# --------------------------------------------------------------------------


def _make_client(**kw):
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


def test_dispatch_end_session_routes_to_handler():
    received = []
    client = _make_client(
        end_session_handler=lambda f, p, r: received.append((f, p, r))
    )

    async def scenario():
        await client._dispatch(
            object(),
            protocol.make_end_session(
                "flow-7", project_root="/p", reason="bye"
            ),
        )

    asyncio.run(scenario())
    assert received == [("flow-7", "/p", "bye")]


def test_handle_end_session_ignores_empty_flow_id():
    received = []
    client = _make_client(end_session_handler=lambda f, p, r: received.append(f))
    asyncio.run(client._handle_end_session({"flow_id": "", "project_root": "/p"}))
    assert received == []


def test_handle_end_session_resolves_root_from_index():
    """A missing project_root is reverse-resolved from the history index."""

    class _Meta:
        flow_id = "flow-9"
        project_root = "/resolved/root"

    class _Provider:
        def build_index(self):
            return [_Meta()]

    received = []
    client = _make_client(
        end_session_handler=lambda f, p, r: received.append((f, p, r)),
        history_provider=_Provider(),
    )
    asyncio.run(client._handle_end_session({"flow_id": "flow-9"}))
    assert received == [("flow-9", "/resolved/root", "user terminated")]


def test_handle_end_session_without_handler_is_noop():
    client = _make_client()  # no end_session_handler
    # Must not raise even though no handler is configured.
    asyncio.run(client._handle_end_session({"flow_id": "flow-1", "project_root": "/p"}))


def test_handle_end_session_swallows_handler_exception():
    def _boom(flow_id, project_root, reason):
        raise RuntimeError("kaboom")

    client = _make_client(end_session_handler=_boom)
    # The exception is caught and logged; the connection must survive.
    asyncio.run(client._handle_end_session({"flow_id": "flow-1", "project_root": "/p"}))
