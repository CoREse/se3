"""Tests for the SE3 daemon package (supervisor, spawner, aggregator, daemon)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from se3.daemon import (
    Daemon,
    DaemonAggregator,
    DaemonConfig,
    DaemonSpawner,
    DaemonSupervisor,
    daemon_status,
    start_daemon,
    stop_daemon,
)
from se3.daemon.daemon import DaemonAlreadyRunning
from se3.daemon import spawner as spawner_mod


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _spawn_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    """Start a long-lived child process for liveness tests."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _make_engine_json(root, *, flow_id="flow-abc", status="running", index=2):
    """Write a minimal engine.json under a fake project root."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "flow_id": flow_id,
        "task_description": "Implement feature X",
        "task_type": "feature",
        "status": status,
        "updated_at": "2026-05-18T12:00:00",
        "state": {
            "current_step_id": "step-3",
            "current_step_index": index,
            "selected_steps": ["analyze", "plan", "implement", "test"],
            "steps": {"step-3": {"step_type": "implement"}},
        },
    }
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


# --------------------------------------------------------------------------
# DaemonSupervisor
# --------------------------------------------------------------------------


class TestSupervisor:
    def test_register_and_get(self, tmp_path):
        sup = DaemonSupervisor()
        rec = sup.register(12345, str(tmp_path), flow_id="f1", task_description="t")
        assert rec.pid == 12345
        assert rec.flow_id == "f1"
        assert sup.get(12345) is rec
        assert sup.count == 1

    def test_register_idempotent(self, tmp_path):
        sup = DaemonSupervisor()
        sup.register(999, str(tmp_path))
        sup.register(999, str(tmp_path), flow_id="later")
        assert sup.count == 1
        assert sup.get(999).flow_id == "later"

    def test_discover_and_reap_dead_process(self, tmp_path):
        sup = DaemonSupervisor()
        proc = _spawn_sleeper()
        try:
            sup.register(proc.pid, str(tmp_path), origin="spawned")
            flows = sup.discover_flows(scan_external=False)
            assert any(f.pid == proc.pid for f in flows)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        reaped = sup.reap()
        assert any(r.pid == proc.pid for r in reaped)
        assert sup.count == 0

    def test_exit_callback_fires_on_reap(self, tmp_path):
        sup = DaemonSupervisor()
        seen = []
        sup.on_exit(lambda rec: seen.append(rec.pid))
        proc = _spawn_sleeper(0.1)
        sup.register(proc.pid, str(tmp_path))
        proc.wait(timeout=10)
        sup.reap()
        assert seen == [proc.pid]

    def test_is_alive(self):
        assert DaemonSupervisor.is_alive(os.getpid()) is True
        assert DaemonSupervisor.is_alive(-1) is False

    def test_concurrent_registration(self, tmp_path):
        import threading

        sup = DaemonSupervisor()

        def worker(base):
            for i in range(50):
                sup.register(base + i, str(tmp_path))

        threads = [threading.Thread(target=worker, args=(b,)) for b in (1000, 2000, 3000)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sup.count == 150


# --------------------------------------------------------------------------
# DaemonSpawner
# --------------------------------------------------------------------------


@pytest.fixture
def fake_se3(tmp_path, monkeypatch):
    """Replace the ``se3`` command with a fake NDJSON-emitting script."""
    script = tmp_path / "fake_se3.py"
    script.write_text(
        "import sys, json, time\n"
        "for i in range(3):\n"
        "    print(json.dumps({'type': 'step_started', 'i': i}), flush=True)\n"
        "print(json.dumps({'type': 'flow_completed'}), flush=True)\n"
        "time.sleep(float(sys.argv[-1]) if sys.argv[-1].replace('.','').isdigit() else 0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        spawner_mod, "_resolve_se3_command", lambda: [sys.executable, str(script)]
    )
    return script


class TestSpawner:
    def test_spawn_starts_child(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn("do a thing", project_root=str(tmp_path))
        assert spawned.pid > 0
        assert "--output-format" in spawned.args
        assert "json" in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_iter_events_parses_ndjson(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn("task", project_root=str(tmp_path))
        events = list(spawner.iter_events(spawned.pid))
        types = [e["type"] for e in events]
        assert "step_started" in types
        assert "flow_completed" in types
        spawner.reap()

    def test_spawn_registers_with_supervisor(self, fake_se3, tmp_path):
        sup = DaemonSupervisor()
        spawner = DaemonSpawner(supervisor=sup)
        spawned = spawner.spawn("task", project_root=str(tmp_path))
        assert sup.get(spawned.pid) is not None
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()
        assert sup.get(spawned.pid) is None

    def test_terminate_sends_sigterm(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        # fake script sleeps for the trailing numeric arg
        spawned = spawner.spawn("task", project_root=str(tmp_path), extra_args=["30"])
        assert spawned.is_running
        rc = spawner.terminate(spawned.pid, grace=10.0)
        assert rc is not None
        assert not spawned.is_running

    def test_orphans_and_reap(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn("task", project_root=str(tmp_path))
        spawner.wait(spawned.pid, timeout=10)
        # still tracked until reaped
        reaped = spawner.reap()
        assert spawned.pid in [s.pid for s in reaped]
        assert spawner.orphans() == []


# --------------------------------------------------------------------------
# DaemonAggregator
# --------------------------------------------------------------------------


class TestAggregator:
    def test_snapshot_reads_engine_json(self, tmp_path):
        _make_engine_json(tmp_path)
        agg = DaemonAggregator()
        agg.add_project_root(tmp_path)
        snap = agg.get_snapshot()
        assert len(snap.flows) == 1
        flow = snap.flows[0]
        assert flow.flow_id == "flow-abc"
        assert flow.status == "running"
        assert flow.current_step == "implement"
        assert flow.total_steps == 4
        assert flow.progress == pytest.approx(0.5)

    def test_snapshot_empty_root(self, tmp_path):
        agg = DaemonAggregator()
        agg.add_project_root(tmp_path)
        snap = agg.get_snapshot()
        assert snap.flows == []

    def test_pending_calls_detected(self, tmp_path):
        _make_engine_json(tmp_path)
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True)
        (calls_dir / "call_001.json").write_text("{}", encoding="utf-8")
        agg = DaemonAggregator()
        agg.add_project_root(tmp_path)
        snap = agg.get_snapshot()
        assert len(snap.pending_calls) == 1
        assert snap.pending_calls[0].call_id == "call_001"

    def test_has_changes_detects_mtime(self, tmp_path):
        _make_engine_json(tmp_path)
        agg = DaemonAggregator()
        agg.add_project_root(tmp_path)
        assert agg.has_changes() is True  # first observation
        assert agg.has_changes() is False  # unchanged
        time.sleep(0.01)
        _make_engine_json(tmp_path, status="completed")
        os.utime(tmp_path / "se3" / "state" / "engine.json", None)
        assert agg.has_changes() is True

    def test_set_project_roots(self, tmp_path):
        agg = DaemonAggregator()
        agg.set_project_roots([tmp_path])
        assert agg.project_roots == [tmp_path.resolve()]
        agg.remove_project_root(tmp_path)
        assert agg.project_roots == []

    def test_snapshot_serializable(self, tmp_path):
        _make_engine_json(tmp_path)
        agg = DaemonAggregator()
        agg.add_project_root(tmp_path)
        snap = agg.get_snapshot()
        # round-trips through JSON without error
        json.dumps(snap.to_dict())


# --------------------------------------------------------------------------
# Daemon lifecycle
# --------------------------------------------------------------------------


class TestDaemonLifecycle:
    def test_status_when_not_running(self, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path)
        assert daemon_status(config) == {"running": False}

    def test_pidfile_guard(self, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path)
        # write a pidfile naming a live process (ourselves)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        config.pid_file.write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        daemon = Daemon(config)
        with pytest.raises(DaemonAlreadyRunning):
            daemon._write_pidfile()

    def test_request_spawn_registers_project_root(self, fake_se3, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        proj = tmp_path / "proj"
        proj.mkdir()
        spawned = daemon.request_spawn("task", project_root=str(proj))
        assert proj.resolve() in daemon.aggregator.project_roots
        daemon.spawner.wait(spawned.pid, timeout=10)
        daemon.spawner.reap()

    def test_poll_once_writes_status(self, tmp_path):
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        daemon._poll_once()
        assert config.status_file.exists()
        payload = json.loads(config.status_file.read_text(encoding="utf-8"))
        assert payload["snapshot"]["flows"]

    def test_background_start_stop(self, tmp_path, monkeypatch):
        """End-to-end: start a detached daemon, query status, stop it."""
        monkeypatch.setenv("SE3_DAEMON_DIR", str(tmp_path))
        config = DaemonConfig(pid_dir=tmp_path)
        result = start_daemon(config)
        assert result["status"] in ("started", "starting")
        try:
            # wait for the daemon to claim the pidfile
            deadline = time.time() + 10
            while time.time() < deadline:
                status = daemon_status(config)
                if status.get("running"):
                    break
                time.sleep(0.2)
            assert daemon_status(config).get("running") is True
        finally:
            stop_result = stop_daemon(config, timeout=15)
        assert stop_result["status"] in ("stopped", "not_running")
        assert daemon_status(config).get("running") is False
