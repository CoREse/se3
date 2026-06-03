"""Tests for the pre-implement test baseline module (engine/test_baseline.py).

Covers:
- compute_baseline_key sensitivity to HEAD and working-tree content changes
- cache read/write (atomic, hit/miss, corruption-tolerant)
- BaselineCapture background capture, blocking wait, and the failure sentinel
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from se3.engine import test_baseline


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def git_repo(tmp_path):
    """A minimal initialized git repo with one commit."""
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "Tester"], tmp_path)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


def _py_print_command(body: str):
    """A command that prints *body* (via python -c) then exits 0."""
    return [sys.executable, "-c", f"print({body!r})"]


# ---------------------------------------------------------------------------
# compute_baseline_key
# ---------------------------------------------------------------------------

class TestComputeBaselineKey:
    def test_stable_for_unchanged_tree(self, git_repo):
        k1 = test_baseline.compute_baseline_key(git_repo)
        k2 = test_baseline.compute_baseline_key(git_repo)
        assert k1 == k2
        assert ":" in k1  # head:dirty form

    def test_changes_on_head_change(self, git_repo):
        before = test_baseline.compute_baseline_key(git_repo)
        (git_repo / "b.txt").write_text("more\n", encoding="utf-8")
        _git(["add", "."], git_repo)
        _git(["commit", "-m", "second"], git_repo)
        after = test_baseline.compute_baseline_key(git_repo)
        assert before != after

    def test_changes_on_tracked_modification(self, git_repo):
        before = test_baseline.compute_baseline_key(git_repo)
        # Modify a tracked file without committing → dirty working tree.
        (git_repo / "a.txt").write_text("hello world\n", encoding="utf-8")
        after = test_baseline.compute_baseline_key(git_repo)
        assert before != after

    def test_changes_on_untracked_content(self, git_repo):
        before = test_baseline.compute_baseline_key(git_repo)
        (git_repo / "untracked.py").write_text("x = 1\n", encoding="utf-8")
        after = test_baseline.compute_baseline_key(git_repo)
        assert before != after
        # Changing the untracked content again shifts the key once more.
        (git_repo / "untracked.py").write_text("x = 2\n", encoding="utf-8")
        after2 = test_baseline.compute_baseline_key(git_repo)
        assert after2 != after

    def test_non_git_dir_does_not_raise(self, tmp_path):
        key = test_baseline.compute_baseline_key(tmp_path)
        assert isinstance(key, str)
        assert key.startswith("no-head:")


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------

class TestBaselineCache:
    def test_round_trip_hit(self, tmp_path):
        key = "abc:def"
        test_baseline.save_cache(tmp_path, key, {"t/x.py::test_a", "t/x.py::test_b"})
        loaded = test_baseline.load_cached(tmp_path, key)
        assert loaded == {"t/x.py::test_a", "t/x.py::test_b"}

    def test_empty_set_is_a_hit_not_a_miss(self, tmp_path):
        key = "clean:tree"
        test_baseline.save_cache(tmp_path, key, set())
        loaded = test_baseline.load_cached(tmp_path, key)
        assert loaded == set()
        assert loaded is not None  # distinct from a miss

    def test_missing_file_is_miss(self, tmp_path):
        assert test_baseline.load_cached(tmp_path, "no:such") is None

    def test_unknown_key_is_miss(self, tmp_path):
        test_baseline.save_cache(tmp_path, "key1", {"a::b"})
        assert test_baseline.load_cached(tmp_path, "key2") is None

    def test_corrupt_json_is_miss(self, tmp_path):
        path = test_baseline.cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        assert test_baseline.load_cached(tmp_path, "any:key") is None

    def test_schema_mismatch_is_miss(self, tmp_path):
        path = test_baseline.cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version": 999, "entries": {"k": {"failures": ["a::b"]}}}',
            encoding="utf-8",
        )
        assert test_baseline.load_cached(tmp_path, "k") is None

    def test_save_preserves_other_keys(self, tmp_path):
        test_baseline.save_cache(tmp_path, "k1", {"a::b"})
        test_baseline.save_cache(tmp_path, "k2", {"c::d"})
        assert test_baseline.load_cached(tmp_path, "k1") == {"a::b"}
        assert test_baseline.load_cached(tmp_path, "k2") == {"c::d"}

    def test_cache_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(test_baseline, "MAX_CACHE_ENTRIES", 3)
        for i in range(5):
            test_baseline.save_cache(tmp_path, f"key{i}", {f"t::test_{i}"})
        # Oldest two evicted; newest three retained.
        assert test_baseline.load_cached(tmp_path, "key0") is None
        assert test_baseline.load_cached(tmp_path, "key1") is None
        assert test_baseline.load_cached(tmp_path, "key2") == {"t::test_2"}
        assert test_baseline.load_cached(tmp_path, "key4") == {"t::test_4"}

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        test_baseline.save_cache(tmp_path, "k", {"a::b"})
        state_dir = test_baseline.cache_path(tmp_path).parent
        leftovers = list(state_dir.glob(".test_baseline_cache.*.tmp"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# BaselineCapture
# ---------------------------------------------------------------------------

class TestBaselineCapture:
    def test_parses_failed_ids(self, tmp_path):
        body = "tests/foo.py::test_a PASSED\ntests/foo.py::test_b FAILED"
        cmd = [sys.executable, "-c",
               f"import sys; print({body!r}); sys.exit(1)"]
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        result = capture.wait()
        assert result == {"tests/foo.py::test_b"}

    def test_all_passed_returns_empty_set(self, tmp_path):
        body = "tests/foo.py::test_a PASSED\ntests/foo.py::test_b PASSED"
        cmd = _py_print_command(body)
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        result = capture.wait()
        assert result == set()
        assert result is not None

    def test_wait_blocks_until_ready(self, tmp_path):
        # A short sleep then emit one failing test.
        cmd = [
            sys.executable, "-c",
            "import time,sys; time.sleep(0.5); "
            "print('tests/x.py::test_slow FAILED'); sys.exit(1)",
        ]
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        # Right after launch the process should not be ready yet.
        assert capture.is_ready() is False
        result = capture.wait()
        assert capture.is_ready() is True
        assert result == {"tests/x.py::test_slow"}

    def test_infra_failure_returns_none_sentinel(self, tmp_path):
        # Non-zero exit with no parseable per-test output → fallback sentinel.
        cmd = [sys.executable, "-c", "import sys; sys.exit(2)"]
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        result = capture.wait()
        assert result is None

    def test_launch_failure_returns_none_sentinel(self, tmp_path):
        cmd = ["this-command-definitely-does-not-exist-xyz"]
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        assert capture.is_ready() is True  # nothing to wait for
        assert capture.wait() is None

    def test_sets_recursion_guard_env(self, tmp_path):
        # The child should see SE3_TEST_RUNNING=1; encode that as a pass/fail
        # line so it flows through the parser.
        cmd = [
            sys.executable, "-c",
            "import os; v=os.environ.get('SE3_TEST_RUNNING'); "
            "print('t::test_env ' + ('PASSED' if v=='1' else 'FAILED'))",
        ]
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        result = capture.wait()
        assert result == set()  # env was set → test_env PASSED

    def test_wait_is_idempotent(self, tmp_path):
        cmd = _py_print_command("tests/foo.py::test_b FAILED")
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        first = capture.wait()
        second = capture.wait()
        assert first == second

    def test_wait_timeout_raises_then_resolves(self, tmp_path):
        cmd = [
            sys.executable, "-c",
            "import time; time.sleep(0.6); print('t::test_x PASSED')",
        ]
        capture = test_baseline.BaselineCapture(tmp_path, command=cmd).launch()
        with pytest.raises(subprocess.TimeoutExpired):
            capture.wait(timeout=0.1)
        # Still resumable after a timeout.
        result = capture.wait()
        assert result == set()

    def test_resolve_command_defaults_to_pytest_verbose(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        capture = test_baseline.BaselineCapture(tmp_path)
        cmd = capture._resolve_command()
        assert "pytest" in cmd
        assert "-v" in cmd
