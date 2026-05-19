"""Tests for the SE3 daemon package (supervisor, spawner, aggregator, daemon)."""

from __future__ import annotations

import json
import os
import re
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


class _FakeClient:
    """Minimal stand-in for DaemonClient exposing connected/last_error."""

    def __init__(self, *, connected: bool, last_error):
        self.connected = connected
        self.last_error = last_error


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

    def test_spawn_appends_discover_flag(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn(
            "explore something", project_root=str(tmp_path), discover=True
        )
        assert "--discover" in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_spawn_omits_discover_flag_by_default(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn("plain task", project_root=str(tmp_path))
        assert "--discover" not in spawned.args
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

    def test_spawn_high_volume_output_does_not_deadlock(self, tmp_path, monkeypatch):
        """A child emitting far more than the OS pipe buffer must still finish.

        Regression test: with stdout/stderr piped and undrained, a child that
        writes >64 KB blocks on write() and deadlocks. Redirecting to log files
        lets it always run to completion.
        """
        script = tmp_path / "noisy_se3.py"
        # Emit ~512 KB on stdout and ~512 KB on stderr — well past the pipe buffer.
        script.write_text(
            "import sys\n"
            "for i in range(4000):\n"
            "    sys.stdout.write('x' * 128 + '\\n')\n"
            "    sys.stderr.write('e' * 128 + '\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stderr.flush()\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            spawner_mod, "_resolve_se3_command", lambda: [sys.executable, str(script)]
        )
        spawner = DaemonSpawner()
        spawned = spawner.spawn("noisy task", project_root=str(tmp_path))
        # Without continuous draining this wait() would hang forever.
        rc = spawner.wait(spawned.pid, timeout=15)
        assert rc == 0
        assert spawned.stdout_log is not None and spawned.stdout_log.exists()
        assert spawned.stderr_log is not None and spawned.stderr_log.exists()
        assert spawned.stdout_log.stat().st_size > 64 * 1024
        spawner.reap()

    def test_orphans_and_reap(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn("task", project_root=str(tmp_path))
        spawner.wait(spawned.pid, timeout=10)
        # still tracked until reaped
        reaped = spawner.reap()
        assert spawned.pid in [s.pid for s in reaped]
        assert spawner.orphans() == []

    def test_resume_builds_resume_argv(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.resume("flow-xyz", project_root=str(tmp_path))
        assert "--resume" in spawned.args
        assert "--flow-id" in spawned.args
        assert "flow-xyz" in spawned.args
        assert "--output-format" in spawned.args
        assert "json" in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_resume_registers_with_supervisor(self, fake_se3, tmp_path):
        sup = DaemonSupervisor()
        spawner = DaemonSpawner(supervisor=sup)
        spawned = spawner.resume("flow-xyz", project_root=str(tmp_path))
        assert sup.get(spawned.pid) is not None
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()


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

    def test_resume_paused_flow_spawns_resume(self, fake_se3, tmp_path):
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-paused", status="paused")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        daemon._resume_paused_flow(str(proj))
        procs = daemon.spawner.processes
        assert len(procs) == 1
        assert "--resume" in procs[0].args
        assert "flow-paused" in procs[0].args
        assert proj.resolve() in daemon.aggregator.project_roots
        daemon.spawner.wait(procs[0].pid, timeout=10)
        daemon.spawner.reap()

    def test_resume_skips_non_paused_flow(self, fake_se3, tmp_path):
        proj = tmp_path / "proj"
        _make_engine_json(proj, status="running")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        daemon._resume_paused_flow(str(proj))
        assert daemon.spawner.processes == []

    def test_resume_noop_without_engine_json(self, fake_se3, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        daemon._resume_paused_flow(str(proj))
        assert daemon.spawner.processes == []

    def test_resume_skips_when_live_process_exists(self, fake_se3, tmp_path):
        proj = tmp_path / "proj"
        _make_engine_json(proj, status="paused")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        sleeper = _spawn_sleeper(30)
        try:
            daemon.supervisor.register(sleeper.pid, str(proj))
            daemon._resume_paused_flow(str(proj))
            assert daemon.spawner.processes == []
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=10)

    def test_handle_respond_writes_response_and_resumes(self, fake_se3, tmp_path):
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-disc", status="paused")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        daemon._handle_respond_request(
            "discovery_step_123", str(proj), "1"
        )
        response_file = (
            proj / "se3" / "calls" / "discovery_step_123.response.json"
        )
        assert response_file.exists()
        procs = daemon.spawner.processes
        assert len(procs) == 1
        assert "--resume" in procs[0].args
        assert "flow-disc" in procs[0].args
        daemon.spawner.wait(procs[0].pid, timeout=10)
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

    def test_write_status_without_client(self, tmp_path):
        """No server_url configured -> connection fields mark local-only."""
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        assert daemon._client is None
        daemon._poll_once()  # must not raise with _client is None
        payload = json.loads(config.status_file.read_text(encoding="utf-8"))
        assert payload["connected"] is False
        assert payload["last_error"] is None
        assert payload["server_configured"] is False

    def test_write_status_with_connected_client(self, tmp_path):
        """A connected client surfaces connected=True / last_error=None."""
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        daemon._client = _FakeClient(connected=True, last_error=None)
        daemon._poll_once()
        payload = json.loads(config.status_file.read_text(encoding="utf-8"))
        assert payload["connected"] is True
        assert payload["last_error"] is None
        assert payload["server_configured"] is True

    def test_write_status_with_failed_client(self, tmp_path):
        """A disconnected client surfaces connected=False with its last_error."""
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        daemon._client = _FakeClient(
            connected=False, last_error="websockets not installed"
        )
        daemon._poll_once()
        payload = json.loads(config.status_file.read_text(encoding="utf-8"))
        assert payload["connected"] is False
        assert payload["last_error"] == "websockets not installed"
        assert payload["server_configured"] is True

    def test_daemon_status_exposes_connection_fields(self, tmp_path):
        """daemon_status() surfaces connected/last_error from the status file."""
        config = DaemonConfig(pid_dir=tmp_path)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        config.pid_file.write_text(
            json.dumps({"pid": os.getpid(), "server_url": "ws://host:8080"}),
            encoding="utf-8",
        )
        config.status_file.write_text(
            json.dumps(
                {
                    "updated_at": time.time(),
                    "server_configured": True,
                    "connected": False,
                    "last_error": "connection refused",
                    "tracked_flows": [],
                    "snapshot": {},
                }
            ),
            encoding="utf-8",
        )
        status = daemon_status(config)
        assert status["running"] is True
        assert status["connected"] is False
        assert status["last_error"] == "connection refused"
        assert status["server_configured"] is True

    def test_daemon_status_connection_defaults_when_absent(self, tmp_path):
        """A status file lacking connection fields yields safe defaults."""
        config = DaemonConfig(pid_dir=tmp_path)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        config.pid_file.write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        config.status_file.write_text(
            json.dumps({"updated_at": time.time(), "snapshot": {}}),
            encoding="utf-8",
        )
        status = daemon_status(config)
        assert status["connected"] is False
        assert status["last_error"] is None
        assert status["server_configured"] is False

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


# --------------------------------------------------------------------------
# Daemon logging configuration
# --------------------------------------------------------------------------


class TestDaemonLogging:
    @pytest.fixture(autouse=True)
    def _restore_root_logger(self):
        """Snapshot and restore root logger state around each test."""
        import logging

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        yield
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    def test_log_format_includes_timestamp(self):
        from se3.daemon.daemon import DAEMON_LOG_FORMAT

        assert "%(asctime)s" in DAEMON_LOG_FORMAT

    def test_configure_installs_timestamped_handler(self):
        import logging

        from se3.daemon.daemon import _configure_daemon_logging

        _configure_daemon_logging()
        tagged = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, "_se3_daemon_log_handler", False)
        ]
        assert len(tagged) == 1
        assert "%(asctime)s" in tagged[0].formatter._fmt

    def test_configure_is_idempotent(self):
        import logging

        from se3.daemon.daemon import _configure_daemon_logging

        _configure_daemon_logging()
        _configure_daemon_logging()
        _configure_daemon_logging()
        tagged = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, "_se3_daemon_log_handler", False)
        ]
        assert len(tagged) == 1

    def test_handler_emits_timestamped_line(self):
        import io
        import logging

        from se3.daemon.daemon import _configure_daemon_logging

        _configure_daemon_logging()
        handler = next(
            h
            for h in logging.getLogger().handlers
            if getattr(h, "_se3_daemon_log_handler", False)
        )
        stream = io.StringIO()
        handler.setStream(stream)
        logging.getLogger("se3.daemon.daemon").info("hello daemon")
        output = stream.getvalue()
        assert "hello daemon" in output
        # A timestamp line begins with a 4-digit year.
        assert re.match(r"^\d{4}-\d\d-\d\d ", output)


# --------------------------------------------------------------------------
# CLI start — visible degradation / connection warnings
# --------------------------------------------------------------------------


class _FakeClock:
    """A controllable clock so connection-poll tests run without real waits."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TestDaemonStartWarnings:
    def test_precheck_warns_when_websockets_missing(self, monkeypatch, capsys):
        """The CLI front-end shouts when 'websockets' is unavailable."""
        from se3 import cli as cli_mod

        # Force `import websockets` inside the precheck to fail.
        monkeypatch.setitem(sys.modules, "websockets", None)
        cli_mod._precheck_websockets("ws://host:8080")
        out = capsys.readouterr().out
        assert "websockets" in out
        assert "local-only" in out.lower()
        assert "pip install 'se3[server]'" in out

    def test_report_connection_warns_when_not_connected(self, monkeypatch, capsys):
        """A fresh status file still showing a last_error at the deadline warns."""
        from se3 import cli as cli_mod

        monkeypatch.setattr(cli_mod, "time", _FakeClock())

        def fake_status(_config):
            return {
                "started_at": 1000.0,
                "updated_at": 1000.0,
                "connected": False,
                "last_error": "connection refused",
            }

        cli_mod._report_connection_result(object(), fake_status)
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "connection refused" in out

    def test_report_connection_warns_when_pending(self, monkeypatch, capsys):
        """When no verdict lands before the timeout, say so rather than lie."""
        from se3 import cli as cli_mod

        monkeypatch.setattr(cli_mod, "time", _FakeClock())

        def fake_status(_config):
            return {
                "started_at": 1000.0,
                "updated_at": 1000.0,
                "connected": False,
                "last_error": None,
            }

        cli_mod._report_connection_result(object(), fake_status)
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "not connected" in out.lower()

    def test_report_connection_confirms_when_connected(self, monkeypatch, capsys):
        """A connected daemon gets a positive confirmation line."""
        from se3 import cli as cli_mod

        monkeypatch.setattr(cli_mod, "time", _FakeClock())

        def fake_status(_config):
            return {
                "started_at": 1000.0,
                "updated_at": 1000.0,
                "connected": True,
                "last_error": None,
            }

        cli_mod._report_connection_result(object(), fake_status)
        out = capsys.readouterr().out
        assert "connected" in out.lower()
        assert "WARNING" not in out

    def test_report_connection_treats_transient_error_as_non_final(
        self, monkeypatch, capsys
    ):
        """A first-dial error must not be reported if backoff reconnects."""
        from se3 import cli as cli_mod

        monkeypatch.setattr(cli_mod, "time", _FakeClock())

        calls = {"n": 0}

        def fake_status(_config):
            calls["n"] += 1
            # First poll: transient error; later polls: connected.
            if calls["n"] < 3:
                return {
                    "started_at": 1000.0,
                    "updated_at": 1000.0,
                    "connected": False,
                    "last_error": "connection refused",
                }
            return {
                "started_at": 1000.0,
                "updated_at": 1000.5,
                "connected": True,
                "last_error": "connection refused",
            }

        cli_mod._report_connection_result(object(), fake_status)
        out = capsys.readouterr().out
        assert "connected" in out.lower()
        assert "WARNING" not in out

    def test_report_connection_ignores_stale_status_file(self, monkeypatch, capsys):
        """A status file predating the current daemon must not be trusted."""
        from se3 import cli as cli_mod

        monkeypatch.setattr(cli_mod, "time", _FakeClock())

        def fake_status(_config):
            # updated_at < started_at: leftover from a hard-killed daemon
            # that recorded a (now meaningless) connected=True.
            return {
                "started_at": 1000.0,
                "updated_at": 500.0,
                "connected": True,
                "last_error": None,
            }

        cli_mod._report_connection_result(object(), fake_status)
        out = capsys.readouterr().out
        # The stale connected=True is ignored; we report pending instead.
        assert "WARNING" in out
        assert "not connected" in out.lower()
