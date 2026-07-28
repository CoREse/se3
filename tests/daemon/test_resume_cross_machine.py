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


def _write_engine(root: Path, *, flow_id: str, status: str) -> None:
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
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_run_pid(root: Path, *, pid: int, machine_id: str | None) -> None:
    """Write a run.pid marker. ``machine_id=None`` emits the legacy single line."""
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    if machine_id is None:
        # Legacy pre-upgrade record: a bare pid on one line, no machine id.
        (state_dir / "run.pid").write_text(f"{pid}\n", encoding="utf-8")
    else:
        (state_dir / "run.pid").write_text(
            encode_run_pidfile(pid, machine_id), encoding="utf-8"
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
    _write_run_pid(proj, pid=424242, machine_id=FOREIGN_MACHINE)

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

    _write_run_pid(proj, pid=1, machine_id=FOREIGN_MACHINE)
    assert run_cmd._cross_machine_resume_block(proj, "f") == FOREIGN_MACHINE

    # legacy (no machine id) and local marker → same-machine path, allowed.
    _write_run_pid(proj, pid=1, machine_id=None)
    assert run_cmd._cross_machine_resume_block(proj, "f") is None
    _write_run_pid(proj, pid=1, machine_id=stable_machine_id())
    assert run_cmd._cross_machine_resume_block(proj, "f") is None

    # COMPLETED flow skips the guard even with a (stale) foreign marker.
    _write_engine(proj, flow_id="f", status="completed")
    _write_run_pid(proj, pid=1, machine_id=FOREIGN_MACHINE)
    assert run_cmd._cross_machine_resume_block(proj, "f") is None


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
            detail = resp.json().get("detail", "")
            assert _SERVER_MACHINE in detail, detail
        finally:
            ctx.__exit__(None, None, None)
