"""Cross-machine single-writer guards on the resume path (group G3).

Forensics showed one flow's step executed by two engines on two machines sharing
a filesystem, because the resume double-spawn guard only consulted the LOCAL
process table — it can never see a live ``luo run`` on another host. The fix
stamps ``tianluo/state/run.pid`` with a stable machine id and refuses a resume
when the marker is held by another machine.

Covered here (design requirement 5):

* (b) ``run.pid`` records a *foreign* machine id and the flow is not COMPLETED →
  ``daemon.request_resume`` refuses with the machine id in the message and never
  spawns a second engine;
* (c-resume) a *legacy* single-line ``run.pid`` (no machine id) — and a marker
  stamped with *this* machine — are treated as local, so the existing
  supervisor/psutil path still resumes;
* the server ``POST /api/flows/{id}/resume`` 409 names the holding machine so the
  WebUI "继续" button surfaces "该 flow 正在机器 X 上运行".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tianluo.core.machine_id import stable_machine_id
from tianluo.core.run_pidfile import encode_run_pidfile
from tianluo.daemon import spawner as spawner_mod
from tianluo.daemon.daemon import Daemon, DaemonConfig


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

FOREIGN_MACHINE = "node-elsewhere-deadbeef"


def _state_dir(root: Path) -> Path:
    return root / "tianluo" / "state"


def _write_engine(root: Path, *, flow_id: str, status: str, **extra) -> None:
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "flow_id": flow_id,
        "task_description": "shared-fs session",
        "task_type": "feature",
        "status": status,
        "updated_at": "2026-07-24T10:00:00",
        "state": {
            "current_step_id": "step-3",
            "current_step_index": 2,
            "selected_steps": ["analyze", "plan", "implement", "test"],
            "steps": {"step-3": {"step_type": "implement"}},
        },
    }
    payload.update(extra)
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")


def _make_worktree_flow(main_root: Path, *, flow_id: str, status: str = "paused") -> Path:
    """Create ``<main>/tianluo/worktrees/wt-1`` holding a ``--worktree`` flow.

    Mirrors what ``run_worktree_mode`` leaves on disk: the flow's engine.json
    lives in the WORKTREE's state dir (the main root's state dir never learns
    about it), which is exactly why the resume guard has to look there.
    """
    worktree = main_root / "tianluo" / "worktrees" / "wt-1"
    _write_engine(
        worktree,
        flow_id=flow_id,
        status=status,
        is_worktree_mode=True,
        worktree_path=str(worktree),
        worktree_branch="se3/wt-1",
        worktree_original_branch="master",
    )
    return worktree


def _write_run_pid(
    root: Path, *, pid: int, machine_id: str | None, flow_id: str | None = None
) -> None:
    """Write a run.pid marker. ``machine_id=None`` emits the legacy single line.

    ``flow_id=None`` reproduces a record written before the engine minted the
    flow id (and every pre-upgrade record), i.e. a marker that names an owner
    but not what it is running.
    """
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    if machine_id is None:
        # Legacy pre-upgrade record: a bare pid on one line, no machine id.
        (state_dir / "run.pid").write_text(f"{pid}\n", encoding="utf-8")
    else:
        (state_dir / "run.pid").write_text(
            encode_run_pidfile(pid, machine_id, flow_id), encoding="utf-8"
        )


@pytest.fixture
def fake_se3(tmp_path, monkeypatch):
    """Replace the ``luo`` command with a fast NDJSON-emitting fake."""
    script = tmp_path / "fake_se3.py"
    script.write_text(
        "import sys, json\n"
        "print(json.dumps({'type': 'flow_completed'}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        spawner_mod, "_resolve_se3_command", lambda: [sys.executable, str(script)]
    )
    return script


# --------------------------------------------------------------------------
# (b) foreign holder → refuse, never spawn
# --------------------------------------------------------------------------


def test_request_resume_rejects_foreign_active_holder(fake_se3, tmp_path):
    """A run.pid held by another machine (flow not COMPLETED) blocks resume."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-shared", status="running")
    # A foreign machine's live run owns the marker. The pid value is irrelevant
    # — it names a process on ANOTHER host that our process table cannot probe.
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE)

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError, match=FOREIGN_MACHINE):
        daemon.request_resume("flow-shared", project_root=str(proj))
    # The refusal must happen BEFORE any spawn: no second engine started.
    assert daemon.spawner.processes == []


def test_request_resume_foreign_holder_paused_flow_rejected(fake_se3, tmp_path):
    """PAUSED is resumable normally, but a foreign live holder still blocks it."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-paused", status="paused")
    _write_run_pid(
        proj, pid=424242, machine_id=FOREIGN_MACHINE, flow_id="flow-paused"
    )

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError, match="running on machine"):
        daemon.request_resume("flow-paused", project_root=str(proj))
    assert daemon.spawner.processes == []


# --------------------------------------------------------------------------
# (c-resume) legacy / local holder → same-machine path still resumes
# --------------------------------------------------------------------------


def test_request_resume_legacy_marker_resumes_via_local_path(fake_se3, tmp_path):
    """A legacy single-line run.pid (no machine id) is treated as local."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-legacy", status="paused")
    # Pre-upgrade marker: bare pid, no machine id. Must NOT be judged foreign.
    _write_run_pid(proj, pid=4242, machine_id=None)

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    spawned = daemon.request_resume("flow-legacy", project_root=str(proj))
    assert "--resume" in spawned.args
    assert "flow-legacy" in spawned.args
    daemon.spawner.wait(spawned.pid, timeout=10)
    daemon.spawner.reap()


def test_request_resume_local_machine_marker_resumes(fake_se3, tmp_path):
    """A marker stamped with THIS machine is local — the existing path resumes."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-local", status="failed")
    _write_run_pid(proj, pid=4242, machine_id=stable_machine_id())

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    spawned = daemon.request_resume("flow-local", project_root=str(proj))
    assert "--resume" in spawned.args
    assert "flow-local" in spawned.args
    daemon.spawner.wait(spawned.pid, timeout=10)
    daemon.spawner.reap()


def test_request_resume_no_marker_resumes(fake_se3, tmp_path):
    """No run.pid at all → nothing foreign to block; existing path resumes."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-nomarker", status="paused")

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    spawned = daemon.request_resume("flow-nomarker", project_root=str(proj))
    assert "--resume" in spawned.args
    daemon.spawner.wait(spawned.pid, timeout=10)
    daemon.spawner.reap()


# --------------------------------------------------------------------------
# CLI `luo run --resume` mirrors the same rejection
# --------------------------------------------------------------------------


def test_cli_resume_run_refuses_foreign_holder(tmp_path):
    """resume_run returns non-zero (never reaches run_flow) for a foreign holder."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-cli", status="paused")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE)

    rc = run_cmd.resume_run(project_root=proj, flow_id="flow-cli")
    assert rc == 1


def test_cli_cross_machine_block_truth_table(tmp_path):
    """The shared preflight blocks only a foreign holder of a non-COMPLETED flow."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="f", status="paused")

    _write_run_pid(proj, pid=1, machine_id=FOREIGN_MACHINE, flow_id="f")
    blocked = run_cmd._cross_machine_resume_block(proj, "f")
    assert blocked is not None and FOREIGN_MACHINE in blocked

    # legacy (no machine id) and local marker → same-machine path, allowed.
    _write_run_pid(proj, pid=1, machine_id=None)
    assert run_cmd._cross_machine_resume_block(proj, "f") is None
    _write_run_pid(proj, pid=1, machine_id=stable_machine_id())
    assert run_cmd._cross_machine_resume_block(proj, "f") is None

    # COMPLETED flow skips the guard even with a (stale) foreign marker.
    _write_engine(proj, flow_id="f", status="completed")
    _write_run_pid(proj, pid=1, machine_id=FOREIGN_MACHINE, flow_id="f")
    assert run_cmd._cross_machine_resume_block(proj, "f") is None


# --------------------------------------------------------------------------
# (b-worktree) a --worktree flow's marker lives in the WORKTREE, not the main root
# --------------------------------------------------------------------------


def test_cross_machine_block_reads_worktree_marker(tmp_path):
    """A foreign holder of a worktree flow blocks the resume from the main root.

    ``luo run --worktree`` stamps ``run.pid`` only inside its own worktree, so a
    guard that reads just the main root would find nothing and let a second
    engine attach to the worktree's engine.json.
    """
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    # Main root: a DIFFERENT, already-finished flow and no marker at all.
    _write_engine(proj, flow_id="other-main-flow", status="completed")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    _write_run_pid(worktree, pid=999999, machine_id=FOREIGN_MACHINE)
    assert not (_state_dir(proj) / "run.pid").exists()

    blocked = run_cmd._cross_machine_resume_block(proj, "flow-wt")
    assert blocked is not None and FOREIGN_MACHINE in blocked


def test_cli_resume_run_refuses_foreign_worktree_holder(tmp_path, monkeypatch):
    """resume_run never re-enters the worktree run whose marker is foreign."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="other-main-flow", status="completed")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    _write_run_pid(worktree, pid=999999, machine_id=FOREIGN_MACHINE)

    # Any attempt to actually resume would overwrite the remote machine's marker.
    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("_resume_worktree_run must not run for a foreign holder")

    monkeypatch.setattr(run_cmd, "_resume_worktree_run", _boom)

    assert run_cmd.resume_run(project_root=proj, flow_id="flow-wt") == 1
    # The remote machine's marker is untouched.
    assert (
        (_state_dir(worktree) / "run.pid").read_text(encoding="utf-8")
        == encode_run_pidfile(999999, FOREIGN_MACHINE)
    )


def test_worktree_local_and_legacy_markers_do_not_block(tmp_path):
    """(c) A local / legacy worktree marker keeps the existing same-machine path."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="other-main-flow", status="completed")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")

    _write_run_pid(worktree, pid=4242, machine_id=stable_machine_id())
    assert run_cmd._cross_machine_resume_block(proj, "flow-wt") is None
    _write_run_pid(worktree, pid=4242, machine_id=None)
    assert run_cmd._cross_machine_resume_block(proj, "flow-wt") is None


def test_completed_worktree_flow_skips_the_guard(tmp_path):
    """A finished worktree run's leftover foreign marker must not wedge resume."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="other-main-flow", status="completed")
    # COMPLETED worktree flows are not discoverable as resumable runs, so the
    # only marker in play is the main root's — and it is absent here.
    worktree = _make_worktree_flow(proj, flow_id="flow-wt", status="completed")
    _write_run_pid(worktree, pid=999999, machine_id=FOREIGN_MACHINE)

    assert run_cmd._cross_machine_resume_block(proj, "flow-wt") is None


# --------------------------------------------------------------------------
# (e) a foreign MAIN-ROOT marker must not block an unrelated --worktree flow
# --------------------------------------------------------------------------
#
# A ``--worktree`` flow's body never writes the main root's state files, so a
# main-root run on another machine is a legitimate concurrent peer, not a
# conflict. Judging the worktree flow by the main root's marker would wedge it
# behind an unrelated remote run it can never collide with.


@pytest.mark.parametrize(
    "worktree_marker",
    ["absent", "local", "legacy"],
    ids=["no-marker", "local-marker", "legacy-marker"],
)
def test_foreign_main_root_marker_does_not_block_worktree_flow(
    tmp_path, worktree_marker
):
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    # Machine X is running a DIFFERENT flow in the main root right now.
    _write_engine(proj, flow_id="flow-b", status="running")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-b")

    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    if worktree_marker == "local":
        _write_run_pid(worktree, pid=4242, machine_id=stable_machine_id())
    elif worktree_marker == "legacy":
        _write_run_pid(worktree, pid=4242, machine_id=None)

    assert run_cmd._cross_machine_resume_block(proj, "flow-wt") is None


def test_foreign_main_root_marker_still_blocks_a_main_root_flow(tmp_path):
    """The main root's record keeps blocking a flow that IS a main-root flow."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-a", status="paused")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-b")
    # A worktree exists but hosts an unrelated flow, so flow-a is judged by the
    # main root — where the foreign record lives.
    _make_worktree_flow(proj, flow_id="flow-other")

    blocked = run_cmd._cross_machine_resume_block(proj, "flow-a")
    assert blocked is not None and FOREIGN_MACHINE in blocked


def test_daemon_foreign_main_root_marker_does_not_block_worktree_flow(
    fake_se3, tmp_path
):
    """``request_resume`` judges a worktree flow by its OWN worktree marker."""
    proj = tmp_path / "proj"
    # The main root's engine.json is how the daemon locates the flow...
    _write_engine(proj, flow_id="flow-wt", status="paused")
    # ...while its run.pid belongs to another machine's unrelated live run.
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-b")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    _write_run_pid(worktree, pid=4242, machine_id=stable_machine_id())

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    spawned = daemon.request_resume("flow-wt", project_root=str(proj))
    assert "--resume" in spawned.args
    daemon.spawner.wait(spawned.pid, timeout=10)
    daemon.spawner.reap()


def test_daemon_request_resume_reads_worktree_marker(fake_se3, tmp_path):
    """The daemon guard consults the worktree marker when resumed by main root."""
    proj = tmp_path / "proj"
    # The main root's engine.json describes the flow (this is how the daemon
    # locates it), but the live run's marker only exists in the worktree.
    _write_engine(proj, flow_id="flow-wt", status="paused")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    _write_run_pid(worktree, pid=999999, machine_id=FOREIGN_MACHINE)

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError, match=FOREIGN_MACHINE):
        daemon.request_resume("flow-wt", project_root=str(proj))
    assert daemon.spawner.processes == []


def test_daemon_request_resume_worktree_root_still_resumes_locally(fake_se3, tmp_path):
    """(d) A local worktree marker leaves the existing resume path untouched."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-wt-local", status="paused")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt-local")
    _write_run_pid(worktree, pid=4242, machine_id=stable_machine_id())

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    spawned = daemon.request_resume("flow-wt-local", project_root=str(proj))
    assert "--resume" in spawned.args
    daemon.spawner.wait(spawned.pid, timeout=10)
    daemon.spawner.reap()


# --------------------------------------------------------------------------
# the refusal must not blame the requested flow for a CO-TENANT's marker
# --------------------------------------------------------------------------
#
# A project root's marker is per state dir, not per flow: machine X may hold it
# because it is running a *different* flow B in the same root. The resume of
# flow A is still refused (both engines would write one engine.json), but a
# message asserting "flow A is running on X — end-session it there" would send
# the operator to kill X's live, unrelated flow B.


def test_daemon_refusal_names_the_co_tenant_flow_not_the_requested_one(
    fake_se3, tmp_path
):
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-a", status="paused")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-b")

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError) as excinfo:
        daemon.request_resume("flow-a", project_root=str(proj))
    message = str(excinfo.value)
    # Still refused, still names where the live run is...
    assert FOREIGN_MACHINE in message
    assert "flow-b" in message
    # ...but never claims flow-a runs there, nor sends the operator to end a
    # session that is doing unrelated work.
    assert "Flow flow-a is running" not in message
    assert "end-session" not in message
    assert daemon.spawner.processes == []


def test_daemon_refusal_blames_the_flow_when_the_marker_is_its_own(fake_se3, tmp_path):
    """A marker stamped with the requested flow keeps the end-session recovery."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-a", status="paused")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-a")

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError) as excinfo:
        daemon.request_resume("flow-a", project_root=str(proj))
    message = str(excinfo.value)
    assert "Flow flow-a is running on machine" in message
    assert "end-session" in message


def test_daemon_unstamped_root_marker_is_not_attributed_to_the_flow(
    fake_se3, tmp_path
):
    """A foreign marker with no flow id is ambiguous — refuse without blaming."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-a", status="paused")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE)

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError) as excinfo:
        daemon.request_resume("flow-a", project_root=str(proj))
    message = str(excinfo.value)
    assert FOREIGN_MACHINE in message
    assert "end-session" not in message
    assert daemon.spawner.processes == []


def test_daemon_unstamped_worktree_marker_still_belongs_to_the_flow(
    fake_se3, tmp_path
):
    """A worktree state dir is resolved BY flow id, so it can host no other flow."""
    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-wt", status="paused")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    _write_run_pid(worktree, pid=999999, machine_id=FOREIGN_MACHINE)

    daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
    with pytest.raises(ValueError) as excinfo:
        daemon.request_resume("flow-wt", project_root=str(proj))
    assert "Flow flow-wt is running on machine" in str(excinfo.value)


def test_cli_block_message_for_a_co_tenant_marker(tmp_path):
    """The CLI preflight mirrors the daemon's attribution (locale-independent)."""
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="flow-a", status="paused")
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-b")

    blocked = run_cmd._cross_machine_resume_block(proj, "flow-a")
    assert blocked is not None
    assert FOREIGN_MACHINE in blocked and "flow-b" in blocked
    # ``end-session`` appears only in the flow-specific wording, in every locale.
    assert "end-session" not in blocked

    # The same marker stamped with the requested flow does point at end-session.
    _write_run_pid(proj, pid=999999, machine_id=FOREIGN_MACHINE, flow_id="flow-a")
    owned = run_cmd._cross_machine_resume_block(proj, "flow-a")
    assert owned is not None and "end-session" in owned


def test_cli_worktree_unstamped_marker_is_the_flows_own(tmp_path):
    from tianluo.commands import run as run_cmd

    proj = tmp_path / "proj"
    _write_engine(proj, flow_id="other-main-flow", status="completed")
    worktree = _make_worktree_flow(proj, flow_id="flow-wt")
    _write_run_pid(worktree, pid=999999, machine_id=FOREIGN_MACHINE)

    blocked = run_cmd._cross_machine_resume_block(proj, "flow-wt")
    assert blocked is not None and "end-session" in blocked


# --------------------------------------------------------------------------
# server resume endpoint names the holding machine
# --------------------------------------------------------------------------

_SERVER_FLOW = "flow-server-shared"
_SERVER_MACHINE = "node-holder-01"
_SERVER_ROOT = "/shared/jobs/proj"


def _server_flow_payload() -> dict:
    """A RUNNING flow the reporting node holds live (resumable=False)."""
    return {
        "flow_id": _SERVER_FLOW,
        "project_root": _SERVER_ROOT,
        "task_description": "shared filesystem session",
        "status": "running",
        "resumable": False,
        "updated_at": "2026-07-24T10:00:00",
    }


def test_server_resume_409_names_holding_machine(monkeypatch):
    """POST /resume on a live-held flow returns 409 with the machine id in detail."""
    pytest.importorskip("fastapi")
    import time

    from fastapi.testclient import TestClient

    from _authsrv import authed_app, authed_hello, login
    from tianluo.daemon import protocol

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        ctx = client.websocket_connect("/ws")
        sock = ctx.__enter__()
        try:
            sock.send_text(
                authed_hello(app, _SERVER_MACHINE, _SERVER_MACHINE, "6.4.0")
            )
            protocol.decode(sock.receive_text())  # WELCOME
            sock.send_text(
                protocol.make_status_update(
                    {
                        "hostname": _SERVER_MACHINE,
                        "flows": [_server_flow_payload()],
                    }
                ).to_json()
            )

            # Poll until the server has ingested the flow, then assert the 409.
            deadline = time.monotonic() + 10.0
            resp = None
            while time.monotonic() < deadline:
                resp = client.post(f"/api/flows/{_SERVER_FLOW}/resume")
                if resp.status_code == 409:
                    break
                time.sleep(0.02)
            assert resp is not None and resp.status_code == 409, (
                resp.text if resp is not None else "no response"
            )
            body = resp.json()
            detail = body.get("detail", "")
            assert _SERVER_MACHINE in detail, detail
            # The machine id must ALSO travel as a machine-readable field: the
            # WebUI renders the refusal from its own language pack
            # (toast.resumeHeldByMachine), so an en-US console never sees the
            # Chinese ``detail`` fallback.
            assert body.get("holder_machine") == _SERVER_MACHINE, body
        finally:
            ctx.__exit__(None, None, None)


def test_server_resume_409_names_no_machine_when_several_report_the_flow(
    monkeypatch,
):
    """Two daemons sharing one filesystem report the same flow → name neither.

    ``get_flow`` resolves reachable-first, so the machine it returns is merely
    the first connected *reporter* of the aggregated ``engine.json`` — not
    necessarily the host whose ``run.pid`` holds the run. Blaming it would send
    the operator to run ``luo end-session`` on a machine that holds nothing, so
    the refusal must stay machine-agnostic when the holder is ambiguous.
    """
    pytest.importorskip("fastapi")
    import time

    from fastapi.testclient import TestClient

    from _authsrv import authed_app, authed_hello, login
    from tianluo.daemon import protocol

    peer = "node-peer-02"
    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        ctx_a = client.websocket_connect("/ws")
        sock_a = ctx_a.__enter__()
        ctx_b = client.websocket_connect("/ws")
        sock_b = ctx_b.__enter__()
        try:
            for sock, machine in (
                (sock_a, _SERVER_MACHINE),
                (sock_b, peer),
            ):
                sock.send_text(authed_hello(app, machine, machine, "6.4.0"))
                protocol.decode(sock.receive_text())  # WELCOME
                sock.send_text(
                    protocol.make_status_update(
                        {"hostname": machine, "flows": [_server_flow_payload()]}
                    ).to_json()
                )

            deadline = time.monotonic() + 10.0
            body = None
            while time.monotonic() < deadline:
                resp = client.post(f"/api/flows/{_SERVER_FLOW}/resume")
                if resp.status_code == 409:
                    body = resp.json()
                    # Both reporters ingested? Then the holder is ambiguous.
                    if "holder_machine" not in body:
                        break
                time.sleep(0.02)
            assert body is not None, "no 409 response"
            assert "holder_machine" not in body, body
            assert _SERVER_MACHINE not in body.get("detail", ""), body
            assert peer not in body.get("detail", ""), body
            # The reason code still lets the WebUI localize the refusal.
            assert body.get("reason") == "still_running", body
        finally:
            ctx_b.__exit__(None, None, None)
            ctx_a.__exit__(None, None, None)
