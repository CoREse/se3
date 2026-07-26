"""Tests for the SE3 daemon package (supervisor, spawner, aggregator, daemon)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tianluo.daemon import (
    Daemon,
    DaemonAggregator,
    DaemonConfig,
    DaemonSpawner,
    DaemonSupervisor,
    daemon_status,
    start_daemon,
    stop_daemon,
)
from tianluo.daemon.daemon import (
    DaemonAlreadyRunning,
    PROJECT_ROOTS_FILENAME,
    _append_project_root,
    _read_project_roots,
    _read_project_roots_raw,
    _sanitize_project_roots,
)
from tianluo.daemon import spawner as spawner_mod
from tianluo.daemon.supervisor import _cmdline_is_se3_run


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# A benign, always-existing, NON-tempdir cwd for the ``se3 run``-shaped stubs
# these discovery tests spawn. The suite-wide ``SE3_EXTERNAL_SCAN_IGNORE`` marker
# hides such a stub from a *new* daemon's external scan, but a **pre-fix** daemon
# already running on this dev machine does not understand that marker, still
# recognises ``[.../se3, run]`` as a genuine flow, and would persist the stub's
# cwd into the real ``~/.se3/project_roots.json``. Running these stubs in a pytest
# tempdir would therefore leak that tempdir into the registry. Anchoring their cwd
# to the real repo root (a legitimate, already-known se3 project — never a pytest
# tempdir) makes that unavoidable pre-fix-daemon registration harmless: nothing a
# test does can add a ``/tmp/pytest-of-*`` root. The tests assert only pid
# membership, never the registered cwd, so the change does not weaken them.
_BENIGN_STUB_CWD = str(Path(__file__).resolve().parents[1])


def _spawn_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    """Start a long-lived child process for liveness tests."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _make_engine_json(root, *, flow_id="flow-abc", status="running", index=2):
    """Write a minimal engine.json under a fake project root."""
    state_dir = root / "tianluo" / "state"
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


def _make_resumable_snapshot(root, *, flow_id, status="running", index=2):
    """Write a per-flow resumable snapshot under tianluo/state/resumable/."""
    snap_dir = root / "tianluo" / "state" / "resumable"
    snap_dir.mkdir(parents=True, exist_ok=True)
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
    (snap_dir / f"{flow_id}.json").write_text(json.dumps(payload), encoding="utf-8")
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
# _cmdline_is_se3_run predicate
# --------------------------------------------------------------------------


def _wait_until_psutil_observes(pid, timeout: float = 10.0):
    """Block until ``psutil`` reports *pid* with a populated cmdline.

    The ``_scan_external`` regression tests must scan a process the daemon can
    actually see. Without this barrier a "not registered" assertion could pass
    merely because ``process_iter`` missed the freshly-spawned pid (or caught it
    pre-exec with an empty cmdline) — not because the predicate rejected it,
    silently voiding the regression's intent. Returns the observed cmdline.
    """
    psutil = pytest.importorskip("psutil")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cmdline = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmdline = None
        if cmdline:
            return cmdline
        time.sleep(0.02)
    raise AssertionError(f"psutil did not observe pid {pid} within {timeout}s")


def _write_fake_se3(directory, body: str):
    """Write an executable ``se3`` shebang script running *body*.

    The basename is ``se3`` so psutil observes ``[interpreter, /path/se3, ...]``
    (se3 at argv[1]) — the real shebang-rewritten console-script shape.
    """
    path = directory / "tianluo"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


class TestCmdlineIsSe3Run:
    """The predicate must match only genuine ``se3 run`` launch shapes and
    must reject inline-code stubs that carry ``se3``/``run`` as script argv."""

    def test_inline_code_form_is_not_se3_run(self):
        # ``python -c <code> se3 run``: tianluo/run are the inline script's argv,
        # not an se3 subcommand — this is the ghost-session false positive.
        assert _cmdline_is_se3_run(["python3", "-c", "<code>", "se3", "run"]) is False

    def test_direct_console_script_argv0(self):
        assert _cmdline_is_se3_run(["se3", "run", "--worktree"]) is True
        assert _cmdline_is_se3_run(["/venv/bin/se3", "run"]) is True

    def test_shebang_rewritten_console_script_argv1(self):
        # psutil's real observation of a shebang console-script: se3 at argv[1].
        assert _cmdline_is_se3_run(["python3", "/venv/bin/se3", "run"]) is True

    def test_module_form(self):
        assert _cmdline_is_se3_run([sys.executable, "-m", "se3", "run"]) is True

    def test_console_script_with_change_option(self):
        # ``se3 run -c my-change``: the CLI's own -c/--change option must NOT be
        # mistaken for the interpreter's inline-code -c and still match.
        assert _cmdline_is_se3_run(["se3", "run", "-c", "my-change", "do work"]) is True
        assert _cmdline_is_se3_run(
            ["python3", "/venv/bin/se3", "run", "-c", "my-change"]
        ) is True

    def test_module_form_with_change_option(self):
        # ``python -m se3 run -c my-change``: the trailing -c is the CLI option,
        # not interpreter inline code, so the module form still matches.
        assert _cmdline_is_se3_run(
            ["python3", "-m", "se3", "run", "-c", "my-change"]
        ) is True

    def test_inline_code_carrying_module_tokens_is_not_se3_run(self):
        # Inline -c precedes a stray ``-m se3 run`` in the script's argv: the
        # interpreter-mode -c disqualifies the module match.
        assert _cmdline_is_se3_run(
            ["python3", "-c", "<code>", "-m", "se3", "run"]
        ) is False

    def test_se3_run_as_script_arguments_only(self):
        # tianluo/run at argv[2]+ with no -m se3 and no se3 program token.
        assert _cmdline_is_se3_run(["python3", "script.py", "se3", "run"]) is False

    def test_other_se3_subcommand_with_trailing_run_is_not_se3_run(self):
        # ``run`` must be the IMMEDIATE subcommand. A different se3 command whose
        # own subcommand/argument is named ``run`` (here ``se3 migrate run``)
        # must not be mistaken for a top-level ``se3 run`` flow — otherwise it
        # re-introduces the ghost-session false positive.
        assert _cmdline_is_se3_run(["se3", "migrate", "run"]) is False
        assert _cmdline_is_se3_run(["/venv/bin/se3", "migrate", "run"]) is False
        # Same constraint via the shebang-rewritten console-script shape.
        assert _cmdline_is_se3_run(["python3", "/venv/bin/se3", "migrate", "run"]) is False
        # And via the module form: ``python -m se3 migrate run``.
        assert _cmdline_is_se3_run([sys.executable, "-m", "se3", "migrate", "run"]) is False

    def test_empty_cmdline(self):
        assert _cmdline_is_se3_run([]) is False

    def test_module_form_without_run(self):
        assert _cmdline_is_se3_run([sys.executable, "-m", "se3"]) is False


# --------------------------------------------------------------------------
# _scan_external discovery
# --------------------------------------------------------------------------


class TestScanExternalDiscovery:
    """``_scan_external`` consumes the tightened predicate: inline-code stubs
    must not be registered, genuine console-script processes still must be."""

    def test_inline_code_se3_run_not_registered(self, tmp_path):
        # Opt back into scan-ignored stubs (they inherit the suite-wide
        # SE3_EXTERNAL_SCAN_IGNORE marker) so the "not registered" result is
        # attributable solely to the cmdline-predicate rejection, not to the
        # marker-skip branch that a default supervisor would also apply.
        sup = DaemonSupervisor(include_scan_ignored=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "se3", "run"],
            cwd=str(tmp_path),
        )
        try:
            # Only meaningful if psutil actually scans this pid: synchronise
            # until it is observable, otherwise "not registered" could pass
            # because the pid was never seen, not because it was rejected.
            cmdline = _wait_until_psutil_observes(proc.pid)
            assert "-c" in cmdline and "se3" in cmdline
            flows = sup.discover_flows(scan_external=True)
            assert all(f.pid != proc.pid for f in flows)
            assert sup.get(proc.pid) is None
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_console_script_se3_run_registered(self, tmp_path):
        pytest.importorskip("psutil")
        fake_se3 = _write_fake_se3(tmp_path, "import time\ntime.sleep(30)\n")
        # The stub inherits the suite-wide SE3_EXTERNAL_SCAN_IGNORE marker so a
        # *new* daemon never registers it; opt this in-process supervisor back in
        # so the positive-registration path is still tested. cwd is the real repo
        # root (see _BENIGN_STUB_CWD), NOT the pytest tempdir, so even a pre-fix
        # daemon that ignores the marker cannot leak a tempdir into ~/.se3/.
        sup = DaemonSupervisor(include_scan_ignored=True)
        proc = subprocess.Popen([str(fake_se3), "run"], cwd=_BENIGN_STUB_CWD)
        try:
            # Wait until psutil reports the shebang-rewritten cmdline (se3 at
            # argv[1]) so the positive registration is exercised deterministically.
            _wait_until_psutil_observes(proc.pid)
            flows = sup.discover_flows(scan_external=True)
            assert any(f.pid == proc.pid for f in flows)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_scan_ignored_stub_not_registered_by_default_supervisor(self, tmp_path):
        """A default supervisor must honour the SE3_EXTERNAL_SCAN_IGNORE marker.

        This is the negative counterpart of the positive test above: it directly
        exercises the ``_proc_opted_out_of_scan`` skip branch in
        ``_scan_external``. The spawned stub inherits the suite-wide
        ``SE3_EXTERNAL_SCAN_IGNORE=1`` marker (see tests/conftest.py); a default
        ``DaemonSupervisor()`` — how a real dev-machine daemon runs — must NOT
        register it, so a regression in the env-read guard (renamed key, broken
        environ parsing) that silently reopens the ~/.se3 registry-pollution
        vector fails this test instead of passing unnoticed.
        """
        pytest.importorskip("psutil")
        fake_se3 = _write_fake_se3(tmp_path, "import time\ntime.sleep(30)\n")
        sup = DaemonSupervisor()
        # cwd is the real repo root, not the pytest tempdir (see _BENIGN_STUB_CWD):
        # a pre-fix daemon that ignores the marker cannot leak a tempdir into
        # ~/.se3/. This test asserts the marker-skip, so cwd is immaterial to it.
        proc = subprocess.Popen([str(fake_se3), "run"], cwd=_BENIGN_STUB_CWD)
        try:
            # Synchronise on observability first: "not registered" must mean the
            # marker was honoured, not that psutil merely never saw the pid.
            _wait_until_psutil_observes(proc.pid)
            flows = sup.discover_flows(scan_external=True)
            assert all(f.pid != proc.pid for f in flows)
            assert sup.get(proc.pid) is None
        finally:
            proc.terminate()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------
# DaemonSpawner
# --------------------------------------------------------------------------


class TestResolveSe3Command:
    """Same-prefix-first resolution of the CLI argv prefix.

    The daemon may be installed in a Python environment whose bin dir is
    not first on ``PATH``; relying on ``shutil.which()`` would then spawn
    ``luo run`` / ``luo init`` children from an unrelated environment,
    silently mismatching versions. ``_resolve_se3_command`` therefore
    prefers a console script (``luo`` / ``tianluo`` / legacy ``se3``)
    sitting next to ``sys.executable`` and falls back to
    ``[sys.executable, '-m', 'tianluo']``.
    """

    def test_prefers_sys_executable_prefix(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python"
        fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_luo = tmp_path / "luo"
        fake_luo.write_text("#!/bin/sh\n", encoding="utf-8")
        unrelated = tmp_path / "other" / "luo"
        unrelated.parent.mkdir()
        unrelated.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.setattr(spawner_mod.sys, "executable", str(fake_python))
        monkeypatch.setattr(
            spawner_mod.shutil, "which", lambda name: str(unrelated)
        )

        assert spawner_mod._resolve_se3_command() == [str(fake_luo)]

    def test_prefers_legacy_se3_script_over_module_form(self, tmp_path, monkeypatch):
        # An old install that only ships the `se3` console script must still
        # resolve to it (same-prefix rule beats the module-form fallback).
        fake_python = tmp_path / "python"
        fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_se3 = tmp_path / "tianluo"
        fake_se3.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.setattr(spawner_mod.sys, "executable", str(fake_python))
        monkeypatch.setattr(spawner_mod.shutil, "which", lambda name: None)

        assert spawner_mod._resolve_se3_command() == [str(fake_se3)]

    def test_falls_back_to_module_form(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python"
        fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
        # No sibling console script placed under tmp_path on purpose.
        unrelated = tmp_path / "other" / "luo"
        unrelated.parent.mkdir()
        unrelated.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.setattr(spawner_mod.sys, "executable", str(fake_python))
        monkeypatch.setattr(
            spawner_mod.shutil, "which", lambda name: str(unrelated)
        )

        assert spawner_mod._resolve_se3_command() == [
            str(fake_python),
            "-m",
            "tianluo",
        ]


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

    def test_spawn_appends_worktree_flag(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn(
            "isolate me", project_root=str(tmp_path), worktree=True
        )
        assert "--worktree" in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_spawn_omits_worktree_flag_by_default(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn("plain task", project_root=str(tmp_path))
        assert "--worktree" not in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_spawn_from_issue_appends_worktree(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn(
            "", project_root=str(tmp_path), from_issue_id="9", worktree=True
        )
        assert "--from-issue" in spawned.args
        assert "--worktree" in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_spawn_from_issue_builds_from_issue_argv(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn(
            "ignored", project_root=str(tmp_path), from_issue_id="042"
        )
        assert "--from-issue" in spawned.args
        idx = spawned.args.index("--from-issue")
        assert spawned.args[idx + 1] == "042"
        assert "--output-format" in spawned.args
        assert "json" in spawned.args
        # The request's task description must not reach the argv on this path.
        assert "ignored" not in spawned.args
        assert "--type" not in spawned.args
        spawner.wait(spawned.pid, timeout=10)
        spawner.reap()

    def test_spawn_from_issue_appends_discover(self, fake_se3, tmp_path):
        spawner = DaemonSpawner()
        spawned = spawner.spawn(
            "", project_root=str(tmp_path), from_issue_id="7", discover=True
        )
        assert "--from-issue" in spawned.args
        assert "--discover" in spawned.args
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
# DaemonSpawner.ensure_se3_project
# --------------------------------------------------------------------------


class TestEnsureSe3Project:
    """Pre-spawn auto-init hook used by the web `New Task` form."""

    def test_skips_init_when_already_se3_project(self, tmp_path, monkeypatch):
        """Directory containing tianluo/charter.md must not re-run init."""
        spec = tmp_path / "tianluo" / "charter.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# existing\n", encoding="utf-8")

        called = []

        def fake_run(*args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("subprocess.run should not be invoked")

        monkeypatch.setattr(spawner_mod.subprocess, "run", fake_run)
        spawner = DaemonSpawner(login_shell_path=None)
        result = spawner.ensure_se3_project(str(tmp_path))
        assert result.initialized is False
        assert result.error == ""
        assert called == []

    def test_runs_init_on_empty_directory(self, tmp_path, monkeypatch):
        """Empty directory triggers `se3 init -p <root>` subprocess."""
        captured = {}

        class _FakeCompleted:
            returncode = 0
            stdout = b"initialized\n"
            stderr = b""

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["cwd"] = kwargs.get("cwd")
            # Simulate init writing the marker file.
            spec = tmp_path / "tianluo" / "charter.md"
            spec.parent.mkdir(parents=True, exist_ok=True)
            spec.write_text("# initialized\n", encoding="utf-8")
            return _FakeCompleted()

        monkeypatch.setattr(spawner_mod.subprocess, "run", fake_run)
        spawner = DaemonSpawner(login_shell_path=None)
        result = spawner.ensure_se3_project(str(tmp_path))
        assert result.initialized is True
        assert result.error == ""
        assert "init" in captured["args"]
        assert "-p" in captured["args"]
        assert str(tmp_path.resolve()) in captured["args"]

    def test_returns_error_on_nonzero_exit(self, tmp_path, monkeypatch):
        """A non-zero return code surfaces as EnsureResult.error and no raise."""

        class _FakeCompleted:
            returncode = 2
            stdout = b""
            stderr = b"boom\n"

        monkeypatch.setattr(
            spawner_mod.subprocess, "run", lambda *a, **k: _FakeCompleted()
        )
        spawner = DaemonSpawner(login_shell_path=None)
        result = spawner.ensure_se3_project(str(tmp_path))
        assert result.initialized is False
        assert "exit code 2" in result.error
        assert "boom" in result.error

    def test_returns_error_when_marker_missing_after_init(
        self, tmp_path, monkeypatch
    ):
        """`se3 init` exit 0 but missing marker is treated as a failure."""

        class _FakeCompleted:
            returncode = 0
            stdout = b""
            stderr = b""

        monkeypatch.setattr(
            spawner_mod.subprocess, "run", lambda *a, **k: _FakeCompleted()
        )
        spawner = DaemonSpawner(login_shell_path=None)
        result = spawner.ensure_se3_project(str(tmp_path))
        assert "marker" in result.error

    def test_empty_project_root_returns_error(self):
        spawner = DaemonSpawner()
        result = spawner.ensure_se3_project("")
        assert result.error
        assert result.initialized is False


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
        calls_dir = tmp_path / "tianluo" / "calls"
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
        os.utime(tmp_path / "tianluo" / "state" / "engine.json", None)
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

    # -- request_resume (explicit protocol-driven resume) ------------------

    def test_request_resume_paused_flow(self, fake_se3, tmp_path):
        """request_resume resumes a PAUSED flow and registers the project root."""
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-r1", status="paused")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        spawned = daemon.request_resume("flow-r1", project_root=str(proj))
        assert "--resume" in spawned.args
        assert "flow-r1" in spawned.args
        assert proj.resolve() in daemon.aggregator.project_roots
        daemon.spawner.wait(spawned.pid, timeout=10)
        daemon.spawner.reap()

    def test_request_resume_failed_flow(self, fake_se3, tmp_path):
        """request_resume resumes a FAILED flow."""
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-f1", status="failed")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        spawned = daemon.request_resume("flow-f1", project_root=str(proj))
        assert "--resume" in spawned.args
        assert "flow-f1" in spawned.args
        daemon.spawner.wait(spawned.pid, timeout=10)
        daemon.spawner.reap()

    def test_request_resume_rejects_completed_flow(self, fake_se3, tmp_path):
        """request_resume raises ValueError for COMPLETED flows."""
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-done", status="completed")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        with pytest.raises(ValueError, match="COMPLETED"):
            daemon.request_resume("flow-done", project_root=str(proj))

    def test_request_resume_resumes_interrupted_running_flow(self, fake_se3, tmp_path):
        """An interrupted flow whose saved status is RUNNING is resumable.

        The flow was interrupted mid-step (e.g. in discovery) so its persisted
        status never advanced past ``running``; with no live process it must
        still be resumable from its breakpoint.
        """
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-run", status="running")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        spawned = daemon.request_resume("flow-run", project_root=str(proj))
        assert "--resume" in spawned.args
        assert "flow-run" in spawned.args
        daemon.spawner.wait(spawned.pid, timeout=10)
        daemon.spawner.reap()

    def test_request_resume_loads_snapshot_when_engine_overwritten(
        self, fake_se3, tmp_path
    ):
        """A flow whose engine.json slot was overwritten resumes via snapshot.

        Flow A is interrupted while running, then flow B starts and overwrites
        engine.json. Resuming A must fall back to the per-flow resumable
        snapshot rather than rejecting on a flow-id mismatch.
        """
        proj = tmp_path / "proj"
        # engine.json now describes flow B (the later run).
        _make_engine_json(proj, flow_id="flow-b", status="running")
        # flow A's resumable snapshot survives, saved while it was running.
        _make_resumable_snapshot(proj, flow_id="flow-a", status="running")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        spawned = daemon.request_resume("flow-a", project_root=str(proj))
        assert "--resume" in spawned.args
        assert "flow-a" in spawned.args
        daemon.spawner.wait(spawned.pid, timeout=10)
        daemon.spawner.reap()

    def test_request_resume_rejects_completed_snapshot(self, fake_se3, tmp_path):
        """A COMPLETED snapshot flow is never resumable."""
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-b", status="running")
        _make_resumable_snapshot(proj, flow_id="flow-a", status="completed")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        with pytest.raises(ValueError, match="COMPLETED"):
            daemon.request_resume("flow-a", project_root=str(proj))

    def test_request_resume_rejects_unknown_flow(self, fake_se3, tmp_path):
        """request_resume raises ValueError when the flow is found nowhere."""
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-real", status="paused")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        with pytest.raises(ValueError, match="not found"):
            daemon.request_resume("flow-wrong", project_root=str(proj))

    def test_request_resume_rejects_missing_state(self, fake_se3, tmp_path):
        """request_resume raises ValueError when neither engine.json nor a
        snapshot exists for the flow."""
        proj = tmp_path / "proj"
        proj.mkdir()
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        with pytest.raises(ValueError, match="not found"):
            daemon.request_resume("flow-x", project_root=str(proj))

    def test_request_resume_rejects_live_process(self, fake_se3, tmp_path):
        """request_resume raises ValueError when a live process exists."""
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-live", status="paused")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        sleeper = _spawn_sleeper(30)
        try:
            daemon.supervisor.register(sleeper.pid, str(proj))
            with pytest.raises(ValueError, match="live process"):
                daemon.request_resume("flow-live", project_root=str(proj))
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=10)

    def test_request_resume_rejects_when_other_flow_live_in_root(
        self, fake_se3, tmp_path
    ):
        """A live process for a *different* flow in the same root blocks resume.

        Flow B runs live in project root R (engine.json holds B). Flow A was
        interrupted and survives only as a resumable snapshot. Resuming A would
        race two writers on the single-slot engine.json, so request_resume must
        refuse — matching by project_root rather than flow_id.
        """
        proj = tmp_path / "proj"
        _make_engine_json(proj, flow_id="flow-b", status="running")
        _make_resumable_snapshot(proj, flow_id="flow-a", status="running")
        daemon = Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))
        sleeper = _spawn_sleeper(30)
        try:
            # Register the live process as flow B in the same root.
            daemon.supervisor.register(sleeper.pid, str(proj))
            with pytest.raises(ValueError, match="live process"):
                daemon.request_resume("flow-a", project_root=str(proj))
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
            proj / "tianluo" / "calls" / "discovery_step_123.response.json"
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
        asyncio.run(daemon._poll_once())
        assert config.status_file.exists()
        payload = json.loads(config.status_file.read_text(encoding="utf-8"))
        assert payload["snapshot"]["flows"]

    def test_poll_once_offloads_snapshot_build(self, tmp_path):
        """The heavy snapshot build must not block the event loop.

        ``get_snapshot`` can fan out into a full ``tianluo/history`` walk; running it
        synchronously on the loop stalls heartbeats and inbound SPAWN_FLOW. This
        test installs a deliberately blocking ``get_snapshot`` and asserts a
        concurrent coroutine (standing in for the heartbeat / receive loop)
        keeps making progress while the snapshot is being built — which is only
        possible if the build runs in a worker thread via ``asyncio.to_thread``.
        """
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)

        real_get_snapshot = daemon.aggregator.get_snapshot
        snapshot_started = threading.Event()
        release_snapshot = threading.Event()

        def _blocking_get_snapshot():
            # Signal the loop, then block this worker thread until the
            # concurrent coroutine has had a chance to run.
            snapshot_started.set()
            assert release_snapshot.wait(timeout=5.0)
            return real_get_snapshot()

        daemon.aggregator.get_snapshot = _blocking_get_snapshot

        async def _drive():
            ticks = 0

            async def _heartbeat():
                nonlocal ticks
                # Wait until the snapshot build is in flight, then prove the
                # loop is still live by advancing several times before letting
                # the (offloaded, blocking) snapshot build finish.
                while not snapshot_started.is_set():
                    await asyncio.sleep(0)
                for _ in range(3):
                    ticks += 1
                    await asyncio.sleep(0)
                release_snapshot.set()

            await asyncio.gather(daemon._poll_once(), _heartbeat())
            return ticks

        ticks = asyncio.run(_drive())
        # The heartbeat advanced while the snapshot build was blocked => the
        # build ran off the event loop.
        assert ticks == 3
        assert config.status_file.exists()
        payload = json.loads(config.status_file.read_text(encoding="utf-8"))
        assert payload["snapshot"]["flows"]

    def test_poll_once_offloads_flow_discovery(self, tmp_path):
        """External-flow discovery must not parse engine.json on the loop.

        ``discover_flows`` -> ``_scan_external`` -> ``register`` ->
        ``_read_flow_id`` -> ``read_engine_header`` parses an at-or-under-guard
        engine.json synchronously (issue #243 A3). Running discovery on the loop
        would block heartbeats for that parse's duration. This installs a
        blocking ``discover_flows`` and proves a concurrent coroutine keeps
        advancing while it runs — only possible if it is offloaded via
        ``asyncio.to_thread``.
        """
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)

        real_discover = daemon.supervisor.discover_flows
        discover_started = threading.Event()
        release_discover = threading.Event()

        def _blocking_discover(*args, **kwargs):
            discover_started.set()
            assert release_discover.wait(timeout=5.0)
            return real_discover(*args, **kwargs)

        daemon.supervisor.discover_flows = _blocking_discover

        async def _drive():
            ticks = 0

            async def _heartbeat():
                nonlocal ticks
                while not discover_started.is_set():
                    await asyncio.sleep(0)
                for _ in range(3):
                    ticks += 1
                    await asyncio.sleep(0)
                release_discover.set()

            await asyncio.gather(daemon._poll_once(), _heartbeat())
            return ticks

        ticks = asyncio.run(_drive())
        assert ticks == 3
        assert config.status_file.exists()

    def test_poll_once_offloads_root_registration(self, tmp_path):
        """Registering a discovered flow's root must not run on the loop.

        ``add_project_root`` for a *genuinely new* root writes through to
        ``registry_persist`` -> ``_read_project_roots`` -> ``json.loads`` of
        project_roots.json (issue #243 A3). Doing that on the event loop while
        iterating discovered flows would block heartbeats for the parse's
        duration. This installs a blocking ``add_project_root`` and proves a
        concurrent coroutine keeps advancing while it runs — only possible if the
        registration loop is offloaded via ``asyncio.to_thread``.
        """
        import types

        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)

        # A discovered flow whose root is not yet tracked, so registration takes
        # the new-root (persisting, parse-bearing) path.
        record = types.SimpleNamespace(project_root=str(proj))
        daemon.supervisor.discover_flows = lambda *a, **k: [record]

        register_started = threading.Event()
        release_register = threading.Event()

        def _blocking_add(path):
            register_started.set()
            assert release_register.wait(timeout=5.0)

        daemon.aggregator.add_project_root = _blocking_add

        async def _drive():
            ticks = 0

            async def _heartbeat():
                nonlocal ticks
                while not register_started.is_set():
                    await asyncio.sleep(0)
                for _ in range(3):
                    ticks += 1
                    await asyncio.sleep(0)
                release_register.set()

            await asyncio.gather(daemon._poll_once(), _heartbeat())
            return ticks

        ticks = asyncio.run(_drive())
        assert ticks == 3

    def test_write_status_without_client(self, tmp_path):
        """No server_url configured -> connection fields mark local-only."""
        proj = tmp_path / "proj"
        _make_engine_json(proj)
        config = DaemonConfig(pid_dir=tmp_path / "rt", project_roots=[str(proj)])
        daemon = Daemon(config)
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        assert daemon._client is None
        asyncio.run(daemon._poll_once())  # must not raise with _client is None
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
        asyncio.run(daemon._poll_once())
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
        asyncio.run(daemon._poll_once())
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
        from tianluo.daemon.daemon import DAEMON_LOG_FORMAT

        assert "%(asctime)s" in DAEMON_LOG_FORMAT

    def test_configure_installs_timestamped_handler(self):
        import logging

        from tianluo.daemon.daemon import _configure_daemon_logging

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

        from tianluo.daemon.daemon import _configure_daemon_logging

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

        from tianluo.daemon.daemon import _configure_daemon_logging

        _configure_daemon_logging()
        handler = next(
            h
            for h in logging.getLogger().handlers
            if getattr(h, "_se3_daemon_log_handler", False)
        )
        stream = io.StringIO()
        handler.setStream(stream)
        logging.getLogger("tianluo.daemon.daemon").info("hello daemon")
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
        from tianluo import cli as cli_mod

        # Force `import websockets` inside the precheck to fail.
        monkeypatch.setitem(sys.modules, "websockets", None)
        cli_mod._precheck_websockets("ws://host:8080")
        out = capsys.readouterr().out
        assert "websockets" in out
        assert "local-only" in out.lower()
        assert "pip install 'tianluo[server]'" in out

    def test_report_connection_warns_when_not_connected(self, monkeypatch, capsys):
        """A fresh status file still showing a last_error at the deadline warns."""
        from tianluo import cli as cli_mod

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
        from tianluo import cli as cli_mod

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
        from tianluo import cli as cli_mod

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
        from tianluo import cli as cli_mod

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
        from tianluo import cli as cli_mod

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


# --------------------------------------------------------------------------
# Persistent project-roots registry (pid_dir/project_roots.json)
# --------------------------------------------------------------------------


def _make_history_flow(root, *, flow_id="flow-hist", project_root=None):
    """Create a history-only flow under *root* (``tianluo/history/<flow_id>``).

    Writes a ``_meta.json`` carrying ``project_root`` and one per-step ``jsonl``
    file so the flow is enumerable by both
    :func:`enumerate_historical_project_roots` and
    :meth:`DaemonHistoryReader.build_index`.
    """
    project_root = project_root if project_root is not None else str(root)
    flow_dir = root / "tianluo" / "history" / flow_id
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "_meta.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "project_root": project_root,
                "type": "feature",
                "created_at": "2026-05-18T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    (flow_dir / "01_analyze_abc123.jsonl").write_text(
        json.dumps({"step_id": "01_analyze_abc123", "message": {"role": "user"}})
        + "\n",
        encoding="utf-8",
    )
    return flow_dir


class TestProjectRootsRegistryHelpers:
    """Unit tests for _read_project_roots / _append_project_root (all in tmp)."""

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert _read_project_roots(tmp_path / "project_roots.json") == []

    def test_read_corrupt_file_returns_empty(self, tmp_path):
        path = tmp_path / "project_roots.json"
        path.write_text("{ not valid json", encoding="utf-8")
        assert _read_project_roots(path) == []

    def test_append_then_read_roundtrip(self, tmp_path):
        path = tmp_path / "project_roots.json"
        proj = tmp_path / "proj"
        proj.mkdir()
        _append_project_root(path, str(proj))
        assert _read_project_roots(path) == [os.path.realpath(str(proj))]

    def test_append_deduplicates(self, tmp_path):
        path = tmp_path / "project_roots.json"
        proj = tmp_path / "proj"
        proj.mkdir()
        _append_project_root(path, str(proj))
        first_mtime = path.stat().st_mtime
        time.sleep(0.01)
        # A second append of the same root must be a no-op: no duplicate entry
        # and the file is left untouched (not rewritten).
        _append_project_root(path, str(proj))
        assert _read_project_roots(path) == [os.path.realpath(str(proj))]
        assert path.stat().st_mtime == first_mtime

    def test_append_normalises_realpath(self, tmp_path):
        path = tmp_path / "project_roots.json"
        proj = tmp_path / "proj"
        proj.mkdir()
        # A non-normalised spelling of the same dir collapses to one entry.
        messy = str(proj) + os.sep + "." + os.sep
        _append_project_root(path, messy)
        _append_project_root(path, str(proj))
        assert _read_project_roots(path) == [os.path.realpath(str(proj))]

    def test_append_uses_atomic_write(self, tmp_path, monkeypatch):
        """The write goes through _atomic_write_json (temp file + rename)."""
        from tianluo.daemon import daemon as daemon_mod

        calls = []
        real_atomic = daemon_mod._atomic_write_json

        def spy(p, payload):
            calls.append((p, payload))
            real_atomic(p, payload)

        monkeypatch.setattr(daemon_mod, "_atomic_write_json", spy)
        path = tmp_path / "project_roots.json"
        proj = tmp_path / "proj"
        proj.mkdir()
        _append_project_root(path, str(proj))
        assert len(calls) == 1
        assert calls[0][0] == path

    def test_config_project_roots_file_under_pid_dir(self, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path)
        assert config.project_roots_file == tmp_path / PROJECT_ROOTS_FILENAME


class TestProjectRootsSelfHeal:
    """Non-existent roots are filtered on read, healed by _sanitize, and never
    (re-)appended — so a deleted pytest tempdir disappears from the registry."""

    def _write_registry(self, path, roots):
        path.write_text(
            json.dumps({"project_roots": [str(r) for r in roots]}),
            encoding="utf-8",
        )

    def test_read_filters_nonexistent_root(self, tmp_path):
        path = tmp_path / "project_roots.json"
        live = tmp_path / "live"
        live.mkdir()
        dead = tmp_path / "gone"  # never created
        self._write_registry(path, [live, dead])
        # Read side hides the deleted root from consumers...
        assert _read_project_roots(path) == [str(live)]
        # ...but the raw view still shows both (so _sanitize can detect a change).
        assert _read_project_roots_raw(path) == [str(live), str(dead)]

    def test_sanitize_erases_nonexistent_root_from_disk(self, tmp_path):
        path = tmp_path / "project_roots.json"
        live = tmp_path / "live"
        live.mkdir()
        dead = tmp_path / "gone"
        self._write_registry(path, [live, dead])
        _sanitize_project_roots(path)
        # The deleted root is now gone from the raw on-disk document, not just
        # hidden on read — the self-heal survives the next restart.
        assert _read_project_roots_raw(path) == [str(live)]

    def test_sanitize_no_change_when_all_roots_exist(self, tmp_path):
        path = tmp_path / "project_roots.json"
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        self._write_registry(path, [a, b])
        before = path.stat().st_mtime
        time.sleep(0.01)
        _sanitize_project_roots(path)
        # No stale entries -> file left untouched (no needless rewrite).
        assert path.stat().st_mtime == before

    def test_sanitize_missing_file_is_safe(self, tmp_path):
        # A missing registry must not raise or create the file.
        path = tmp_path / "project_roots.json"
        _sanitize_project_roots(path)
        assert not path.exists()

    def test_sanitize_corrupt_file_is_safe(self, tmp_path):
        path = tmp_path / "project_roots.json"
        path.write_text("{ not valid json", encoding="utf-8")
        _sanitize_project_roots(path)  # must not raise / block startup

    def test_append_skips_nonexistent_root(self, tmp_path):
        path = tmp_path / "project_roots.json"
        dead = tmp_path / "gone"  # never created
        _append_project_root(path, str(dead))
        # A since-deleted directory is never persisted in the first place.
        assert not path.exists()
        assert _read_project_roots_raw(path) == []

    def test_append_new_root_prunes_persisted_stale_root(self, tmp_path):
        # A stale entry persisted while the daemon was up (e.g. a pytest tempdir
        # torn down after registration) must be erased from disk when a *new*
        # live root is appended, not carried forward until the next restart.
        path = tmp_path / "project_roots.json"
        dead = tmp_path / "gone"  # never created
        self._write_registry(path, [dead])
        live = tmp_path / "live"
        live.mkdir()
        _append_project_root(path, str(live))
        assert _read_project_roots_raw(path) == [os.path.realpath(str(live))]


class TestRegistryWriteThrough:
    """add_project_root / request_spawn / _poll_once persist roots to disk."""

    def test_add_project_root_writes_through(self, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        proj = tmp_path / "proj"
        proj.mkdir()
        daemon.aggregator.add_project_root(str(proj))
        roots = _read_project_roots(config.project_roots_file)
        assert os.path.realpath(str(proj)) in roots

    def test_repeated_registration_dedupes_on_disk(self, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        proj = tmp_path / "proj"
        proj.mkdir()
        daemon.aggregator.add_project_root(str(proj))
        daemon.aggregator.add_project_root(str(proj))
        roots = _read_project_roots(config.project_roots_file)
        assert roots.count(os.path.realpath(str(proj))) == 1

    def test_request_spawn_writes_through(self, fake_se3, tmp_path):
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        proj = tmp_path / "proj"
        proj.mkdir()
        spawned = daemon.request_spawn("task", project_root=str(proj))
        roots = _read_project_roots(config.project_roots_file)
        assert os.path.realpath(str(proj)) in roots
        daemon.spawner.wait(spawned.pid, timeout=10)
        daemon.spawner.reap()

    def test_poll_once_persists_discovered_root(self, tmp_path, monkeypatch):
        """A root discovered by the supervisor poll is written through."""
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        proj = tmp_path / "proj"
        _make_engine_json(proj)

        class _FakeRecord:
            def __init__(self, root):
                self.project_root = str(root)

            def to_dict(self):
                return {"project_root": self.project_root}

        monkeypatch.setattr(
            daemon.supervisor, "discover_flows", lambda: [_FakeRecord(proj)]
        )
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(daemon._poll_once())
        roots = _read_project_roots(config.project_roots_file)
        assert os.path.realpath(str(proj)) in roots

    def test_write_through_only_touches_tmp(self, tmp_path):
        """Registration writes solely under the isolated pid_dir."""
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        daemon = Daemon(config)
        proj = tmp_path / "proj"
        proj.mkdir()
        daemon.aggregator.add_project_root(str(proj))
        assert config.project_roots_file.exists()
        # The registry file lives under the isolated pid_dir, nowhere else.
        assert config.project_roots_file.parent == (tmp_path / "rt")


class TestRegistryDrivesHistoryWithNoLiveProcess:
    """Reload / zero-live-process: the registry alone repopulates both outputs.

    Equivalent to a daemon restart with no ``se3 run`` process anywhere: the
    in-memory active set is empty and only the on-disk registry remains.
    """

    def test_registry_repopulates_snapshot_and_index(self, tmp_path):
        # A project with real on-disk history but no live flow / no engine.json.
        proj = tmp_path / "proj"
        _make_history_flow(proj, flow_id="flow-hist", project_root=str(proj))
        proj_real = os.path.realpath(str(proj))

        # Pre-seed the registry file under an isolated pid_dir (as a prior
        # daemon would have left it), then build a brand-new Daemon with no
        # config project_roots and no discovered live processes.
        config = DaemonConfig(pid_dir=tmp_path / "rt")
        config.pid_dir.mkdir(parents=True, exist_ok=True)
        _append_project_root(config.project_roots_file, str(proj))

        daemon = Daemon(config)
        # No active roots registered this lifetime.
        assert daemon.aggregator.project_roots == []

        # (a) snapshot.project_roots is non-empty and contains the history root.
        snapshot = daemon.aggregator.get_snapshot()
        assert proj_real in snapshot.project_roots

        # (b) build_index() returns that historical session under no live flow.
        index = daemon.history_reader.build_index()
        flow_ids = {meta.flow_id for meta in index}
        assert "flow-hist" in flow_ids

    def test_aggregator_all_project_roots_union(self, tmp_path):
        """all_project_roots() unions active ∪ registry ∪ disk-history roots."""
        proj = tmp_path / "proj"
        _make_history_flow(proj, flow_id="flow-hist", project_root=str(proj))
        registry_file = tmp_path / "rt" / "project_roots.json"
        registry_file.parent.mkdir(parents=True, exist_ok=True)
        _append_project_root(registry_file, str(proj))

        agg = DaemonAggregator(
            registry_load=lambda: _read_project_roots(registry_file),
            registry_persist=lambda r: _append_project_root(registry_file, r),
        )
        # No active roots, yet the registry root surfaces.
        assert agg.project_roots == []
        assert os.path.realpath(str(proj)) in agg.all_project_roots()
        # _merge_project_roots delegates to all_project_roots (identical output).
        assert agg._merge_project_roots() == agg.all_project_roots()

    def test_bare_aggregator_has_no_persistence(self, tmp_path):
        """Default DaemonAggregator() keeps legacy in-memory-only behavior."""
        agg = DaemonAggregator()
        proj = tmp_path / "proj"
        proj.mkdir()
        agg.add_project_root(str(proj))
        # No registry file is created anywhere for a bare aggregator.
        assert not (tmp_path / "project_roots.json").exists()
        # all_project_roots still works, sourced purely from the active set.
        assert os.path.realpath(str(proj)) in agg.all_project_roots()
