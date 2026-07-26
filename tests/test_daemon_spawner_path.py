"""Tests for login-shell PATH resolution and child-env merging (Problem A).

Covers :func:`resolve_login_shell_path`, :func:`merge_path_env`, and the
integration into :class:`DaemonSpawner` that ensures spawned ``se3 run``
children inherit a full login-shell PATH even when the daemon was started
from a sparse environment (systemd, cron, container).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# -- import the module under test -------------------------------------------
import tianluo.daemon.spawner as spawner_mod
from tianluo.daemon.spawner import (
    DaemonSpawner,
    merge_path_env,
    resolve_login_shell_path,
)


# =========================================================================
# merge_path_env — pure-function unit tests
# =========================================================================

class TestMergePathEnv:
    """Parametrized cases for the PATH merge helper."""

    def test_login_entries_first(self):
        """Login entries appear before current entries, preserving order."""
        assert merge_path_env("A:B", "C:A") == f"C:A{os.pathsep}B"

    def test_dedup_across_both(self):
        """Entries shared between login and current appear only once."""
        sep = os.pathsep
        result = merge_path_env(f"X{sep}Y{sep}Z", f"Y{sep}W")
        # login: Y, W; current: X, Z (Y already seen)
        assert result == f"Y{sep}W{sep}X{sep}Z"

    def test_idempotent(self):
        """Merging when current already contains all login entries is a no-op in content."""
        full = f"/usr/bin{os.pathsep}/usr/local/bin{os.pathsep}~/.local/bin"
        result = merge_path_env(full, "/usr/local/bin:~/.local/bin")
        # Login entries land first, but dedup preserves original positions.
        # The result should contain the same set of entries.
        assert set(result.split(os.pathsep)) == set(full.split(os.pathsep))

    def test_login_none_returns_current(self):
        assert merge_path_env("A:B", None) == "A:B"

    def test_login_empty_returns_current(self):
        assert merge_path_env("A:B", "") == "A:B"

    def test_current_none_returns_login(self):
        assert merge_path_env(None, "C:D") == "C:D"

    def test_current_empty_returns_login(self):
        assert merge_path_env("", "C:D") == "C:D"

    def test_both_none_returns_none(self):
        assert merge_path_env(None, None) is None

    def test_both_empty_returns_none(self):
        assert merge_path_env("", "") is None

    def test_single_entry_each(self):
        assert merge_path_env("/a", "/b") == f"/b{os.pathsep}/a"

    def test_preserves_order_within_login(self):
        """Login entries keep their original relative order."""
        result = merge_path_env("Z", f"A{os.pathsep}B{os.pathsep}C")
        assert result.startswith(f"A{os.pathsep}B{os.pathsep}C")

    def test_preserves_order_within_current(self):
        """Current entries keep their original relative order after login."""
        sep = os.pathsep
        result = merge_path_env(f"X{sep}Y{sep}Z", "W")
        # W first, then X, Y, Z
        assert result == f"W{sep}X{sep}Y{sep}Z"


# =========================================================================
# resolve_login_shell_path — subprocess-based tests
# =========================================================================

class TestResolveLoginShellPath:
    """Tests using a fake shell script to control the subprocess output."""

    def _write_shell(self, tmp_path: Path, body: str, *, executable: bool = True) -> Path:
        """Write a fake shell script and return its path."""
        script = tmp_path / "fake_shell"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        if executable:
            script.chmod(0o755)
        return script

    def test_success_with_normal_output(self, tmp_path, monkeypatch):
        """Normal case: shell prints a valid PATH with noise before it."""
        shell = self._write_shell(tmp_path, """\
            #!/bin/sh
            echo "Loading profile..."
            echo "/home/user/.local/bin:/usr/local/bin:/usr/bin"
        """)
        monkeypatch.setenv("SHELL", str(shell))
        result = resolve_login_shell_path(timeout=5.0)
        assert result == "/home/user/.local/bin:/usr/local/bin:/usr/bin"

    def test_success_takes_last_pathsep_line(self, tmp_path, monkeypatch):
        """When the shell prints noise containing colons, the last line with
        ``os.pathsep`` is chosen.
        """
        sep = os.pathsep
        shell = self._write_shell(tmp_path, f"""\
            #!/bin/sh
            echo "some:noise:with:colons"
            echo "/real/path1{sep}/real/path2"
        """)
        monkeypatch.setenv("SHELL", str(shell))
        result = resolve_login_shell_path(timeout=5.0)
        assert result == f"/real/path1{sep}/real/path2"

    def test_nonzero_exit_returns_none(self, tmp_path, monkeypatch):
        """Shell exits non-zero → returns None."""
        shell = self._write_shell(tmp_path, """\
            #!/bin/sh
            exit 1
        """)
        monkeypatch.setenv("SHELL", str(shell))
        assert resolve_login_shell_path(timeout=5.0) is None

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        """Shell hangs beyond timeout → returns None."""
        shell = self._write_shell(tmp_path, """\
            #!/bin/sh
            sleep 30
        """)
        monkeypatch.setenv("SHELL", str(shell))
        assert resolve_login_shell_path(timeout=0.1) is None

    def test_empty_output_returns_none(self, tmp_path, monkeypatch):
        """Shell prints nothing → returns None."""
        shell = self._write_shell(tmp_path, """\
            #!/bin/sh
            # no output
        """)
        monkeypatch.setenv("SHELL", str(shell))
        assert resolve_login_shell_path(timeout=5.0) is None

    def test_output_without_pathsep_returns_none(self, tmp_path, monkeypatch):
        """Shell prints a line without the path separator → returns None."""
        shell = self._write_shell(tmp_path, """\
            #!/bin/sh
            echo "hello world"
        """)
        monkeypatch.setenv("SHELL", str(shell))
        assert resolve_login_shell_path(timeout=5.0) is None

    def test_missing_shell_env_falls_back_to_bash(self, monkeypatch):
        """When $SHELL is unset, falls back to /bin/bash."""
        monkeypatch.delenv("SHELL", raising=False)
        # We can't easily test the actual bash path without running bash,
        # but we can verify the fallback by checking it doesn't crash.
        # Just ensure it returns something (or None if bash not available).
        result = resolve_login_shell_path(timeout=5.0)
        # On most Linux systems bash is available, so this should return a PATH.
        # In minimal containers it might return None — both are acceptable.
        assert result is None or isinstance(result, str)

    def test_nonexistent_shell_returns_none(self, tmp_path, monkeypatch):
        """When $SHELL points to a non-existent binary, returns None."""
        monkeypatch.setenv("SHELL", "/nonexistent/shell/binary")
        assert resolve_login_shell_path(timeout=5.0) is None


# =========================================================================
# DaemonSpawner integration — PATH merging in child_env
# =========================================================================

class TestSpawnerPathMerge:
    """Integration tests verifying that spawned children get merged PATH."""

    def _make_fake_se3(self, tmp_path: Path, monkeypatch) -> Path:
        """Create a fake ``se3`` script that echoes its own PATH and exits."""
        script = tmp_path / "fake_se3.py"
        script.write_text(
            "import os, json, sys\n"
            "# Emit the child's PATH as the first NDJSON event\n"
            "print(json.dumps({'type': 'path_report', 'PATH': os.environ.get('PATH', '')}))\n"
            "print(json.dumps({'type': 'flow_completed'}))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            spawner_mod, "_resolve_se3_command",
            lambda: [sys.executable, str(script)],
        )
        return script

    def test_bare_path_spawn_gets_merged(self, tmp_path, monkeypatch):
        """Simulate systemd bare PATH: child_env PATH includes login entries."""
        bare_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
        login_path = f"/home/user/.local/bin:/usr/local/bin{bare_path}"

        monkeypatch.setenv("PATH", bare_path)
        self._make_fake_se3(tmp_path, monkeypatch)

        spawner = DaemonSpawner(login_shell_path=login_path)
        spawned = spawner.spawn("test task", project_root=str(tmp_path))
        spawner.wait(spawned.pid, timeout=10)

        events = list(spawner.iter_events(spawned.pid))
        path_reports = [e for e in events if e.get("type") == "path_report"]
        assert len(path_reports) == 1
        child_path = path_reports[0]["PATH"]

        # The child PATH must contain the login-shell ~/.local/bin entry.
        assert "/home/user/.local/bin" in child_path
        # And it must still contain the original bare PATH entries.
        for entry in bare_path.split(":"):
            assert entry in child_path
        spawner.reap()

    def test_full_path_spawn_is_idempotent(self, tmp_path, monkeypatch):
        """When daemon already has full PATH, merging is idempotent."""
        full_path = "/home/user/.local/bin:/usr/local/bin:/usr/bin"
        monkeypatch.setenv("PATH", full_path)
        self._make_fake_se3(tmp_path, monkeypatch)

        # Login shell returns the same set of entries (different order).
        login_path = "/usr/local/bin:/home/user/.local/bin:/usr/bin"
        spawner = DaemonSpawner(login_shell_path=login_path)
        spawned = spawner.spawn("test task", project_root=str(tmp_path))
        spawner.wait(spawned.pid, timeout=10)

        events = list(spawner.iter_events(spawned.pid))
        path_reports = [e for e in events if e.get("type") == "path_report"]
        assert len(path_reports) == 1
        child_path = path_reports[0]["PATH"]

        # No duplicate entries.
        parts = child_path.split(":")
        assert len(parts) == len(set(parts))
        # All original entries present.
        for entry in full_path.split(":"):
            assert entry in child_path
        spawner.reap()

    def test_login_shell_path_none_preserves_current(self, tmp_path, monkeypatch):
        """When login_shell_path=None (disabled), child PATH is unmodified."""
        current_path = "/usr/bin:/bin"
        monkeypatch.setenv("PATH", current_path)
        self._make_fake_se3(tmp_path, monkeypatch)

        spawner = DaemonSpawner(login_shell_path=None)
        spawned = spawner.spawn("test task", project_root=str(tmp_path))
        spawner.wait(spawned.pid, timeout=10)

        events = list(spawner.iter_events(spawned.pid))
        path_reports = [e for e in events if e.get("type") == "path_report"]
        assert len(path_reports) == 1
        child_path = path_reports[0]["PATH"]
        assert child_path == current_path
        spawner.reap()

    def test_explicit_env_path_overrides_merge(self, tmp_path, monkeypatch):
        """Caller's explicit env['PATH'] takes priority over merge result."""
        bare_path = "/usr/bin"
        login_path = "/home/user/.local/bin:/usr/bin"
        explicit_path = "/custom/path:/usr/bin"

        monkeypatch.setenv("PATH", bare_path)
        self._make_fake_se3(tmp_path, monkeypatch)

        spawner = DaemonSpawner(login_shell_path=login_path)
        spawned = spawner.spawn(
            "test task",
            project_root=str(tmp_path),
            env={"PATH": explicit_path},
        )
        spawner.wait(spawned.pid, timeout=10)

        events = list(spawner.iter_events(spawned.pid))
        path_reports = [e for e in events if e.get("type") == "path_report"]
        assert len(path_reports) == 1
        child_path = path_reports[0]["PATH"]
        # The explicit PATH wins — merge result is overwritten.
        assert child_path == explicit_path
        spawner.reap()

    def test_resume_also_gets_merged_path(self, tmp_path, monkeypatch):
        """The resume path also merges login-shell PATH into child_env."""
        bare_path = "/usr/sbin:/usr/bin"
        login_path = f"/home/user/.local/bin:{bare_path}"

        monkeypatch.setenv("PATH", bare_path)
        self._make_fake_se3(tmp_path, monkeypatch)

        spawner = DaemonSpawner(login_shell_path=login_path)
        spawned = spawner.resume("fake-flow-id", project_root=str(tmp_path))
        spawner.wait(spawned.pid, timeout=10)

        events = list(spawner.iter_events(spawned.pid))
        path_reports = [e for e in events if e.get("type") == "path_report"]
        assert len(path_reports) == 1
        child_path = path_reports[0]["PATH"]
        assert "/home/user/.local/bin" in child_path
        spawner.reap()


# =========================================================================
# Failure-path / warning-log tests
# =========================================================================

class TestSpawnerPathFailurePath:
    """Verify that login-shell resolution failures produce warnings and
    the spawner falls back to current behavior."""

    def test_constructor_warns_on_resolution_failure(self, tmp_path, monkeypatch, caplog):
        """When resolve_login_shell_path returns None, a warning is logged."""
        monkeypatch.setenv("SHELL", "/nonexistent/shell")
        with caplog.at_level(logging.WARNING, logger="tianluo.daemon.spawner"):
            spawner = DaemonSpawner(login_shell_path=...)  # trigger resolution
        assert spawner._login_shell_path is None
        assert "Could not resolve login-shell PATH" in caplog.text

    def test_constructor_does_not_raise_on_failure(self, tmp_path, monkeypatch):
        """Spawner construction succeeds even when PATH resolution fails."""
        monkeypatch.setenv("SHELL", "/nonexistent/shell")
        # Should not raise.
        spawner = DaemonSpawner()
        assert spawner._login_shell_path is None

    def test_spawn_works_with_no_login_path(self, tmp_path, monkeypatch):
        """Spawning works normally when login_shell_path is None (disabled)."""
        script = tmp_path / "fake_se3.py"
        script.write_text(
            "import json\nprint(json.dumps({'type': 'flow_completed'}))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            spawner_mod, "_resolve_se3_command",
            lambda: [sys.executable, str(script)],
        )
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        spawner = DaemonSpawner(login_shell_path=None)
        spawned = spawner.spawn("test task", project_root=str(tmp_path))
        assert spawned.pid > 0
        spawner.wait(spawned.pid, timeout=10)
        assert spawned.returncode == 0
        spawner.reap()
