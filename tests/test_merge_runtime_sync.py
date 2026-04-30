"""Tests for runtime_sync module — tier A/B/C sync logic.

Uses filesystem fixtures only; no real git is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.merge.runtime_sync import (
    RuntimeSyncCollision,
    SyncReport,
    sync_branch_runtime,
)


def _make_sync_call(source_dir: Path, target_dir: Path):
    """Return a callable that invokes sync_branch_runtime with a mocked worktree lookup.

    The returned callable accepts ``branch`` and returns a ``SyncReport``.
    """
    def _call(branch: str) -> SyncReport:
        import se3.engine.merge.runtime_sync as _rs
        original = _rs._get_worktree_path_for_branch
        _rs._get_worktree_path_for_branch = lambda _pr, _br: source_dir
        try:
            return sync_branch_runtime(target_dir, branch)
        finally:
            _rs._get_worktree_path_for_branch = original
    return _call


class TestTierACopy:
    """Tier A files are copied when the target does not already have them."""

    def test_tier_a_dir_file_copied(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("log content")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert report.skipped is False
        assert "history/flow1.log" in report.copied
        assert (target_se3 / "history" / "flow1.log").exists()
        assert (target_se3 / "history" / "flow1.log").read_text() == "log content"

    def test_tier_a_glob_file_copied(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "summary-abc.md").write_text("summary")
        (source_se3 / "calls").mkdir(parents=True)
        (source_se3 / "calls" / "confirm_123.json").write_text("confirm")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "state/summary-abc.md" in report.copied
        assert "calls/confirm_123.json" in report.copied
        assert (target_se3 / "state" / "summary-abc.md").exists()
        assert (target_se3 / "calls" / "confirm_123.json").exists()

    def test_tier_a_nested_file_copied(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "collab" / "tasks" / "sub").mkdir(parents=True)
        (source_se3 / "collab" / "tasks" / "sub" / "task.md").write_text("task")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "collab/tasks/sub/task.md" in report.copied
        assert (target_se3 / "collab" / "tasks" / "sub" / "task.md").exists()

    def test_tier_a_preserves_mtime_and_permissions(self, tmp_path: Path) -> None:
        import os
        import stat

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "logs").mkdir(parents=True)
        src_file = source_se3 / "logs" / "app.log"
        src_file.write_text("log")
        # Set a specific mode
        os.chmod(src_file, 0o640)
        # Set a specific mtime
        os.utime(src_file, (1234567890, 1234567890))

        call = _make_sync_call(source, target)
        report = call("feature")

        dest_file = target / "se3" / "logs" / "app.log"
        assert dest_file.exists()
        assert stat.S_IMODE(dest_file.stat().st_mode) == 0o640
        assert dest_file.stat().st_mtime == 1234567890

    def test_tier_a_all_paths_covered(self, tmp_path: Path) -> None:
        """Every tier A path constant has at least one file in the test."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "h.log").write_text("h")
        (source_se3 / "logs").mkdir(parents=True)
        (source_se3 / "logs" / "l.log").write_text("l")
        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "summary-x.md").write_text("s")
        (source_se3 / "state" / "archive").mkdir(parents=True)
        (source_se3 / "state" / "archive" / "a.md").write_text("a")
        (source_se3 / "calls").mkdir(parents=True)
        (source_se3 / "calls" / "confirm_y.json").write_text("c")
        (source_se3 / "collab" / "tasks").mkdir(parents=True)
        (source_se3 / "collab" / "tasks" / "t.md").write_text("t")

        call = _make_sync_call(source, target)
        report = call("feature")

        copied_set = set(report.copied)
        assert "history/h.log" in copied_set
        assert "logs/l.log" in copied_set
        assert "state/summary-x.md" in copied_set
        assert "state/archive/a.md" in copied_set
        assert "calls/confirm_y.json" in copied_set
        assert "collab/tasks/t.md" in copied_set


class TestTierACollision:
    """Tier A collisions raise RuntimeSyncCollision."""

    def test_same_relative_path_collision(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        call = _make_sync_call(source, target)
        with pytest.raises(RuntimeSyncCollision) as exc_info:
            call("feature")
        assert exc_info.value.rel_path == "history/flow1.log"

    def test_glob_collision(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "summary-x.md").write_text("source")
        (target_se3 / "state").mkdir(parents=True)
        (target_se3 / "state" / "summary-x.md").write_text("target")

        call = _make_sync_call(source, target)
        with pytest.raises(RuntimeSyncCollision) as exc_info:
            call("feature")
        assert exc_info.value.rel_path == "state/summary-x.md"

    def test_different_subpath_same_name_no_collision(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "same.log").write_text("source")
        (target_se3 / "logs").mkdir(parents=True)
        (target_se3 / "logs" / "same.log").write_text("target")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "history/same.log" in report.copied
        assert (target_se3 / "history" / "same.log").exists()
        # target's logs/same.log should be untouched
        assert (target_se3 / "logs" / "same.log").read_text() == "target"


class TestTierB:
    """Tier B files are recorded as discarded but not copied."""

    def test_tier_b_files_discarded(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "engine.json").write_text("{}")
        (source_se3 / "state" / "known_test_failures.json").write_text("[]")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "state/engine.json" in report.discarded
        assert "state/known_test_failures.json" in report.discarded
        assert not (target_se3 / "state" / "engine.json").exists()
        assert not (target_se3 / "state" / "known_test_failures.json").exists()

    def test_tier_b_dir_discarded(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "calls" / "active").mkdir(parents=True)
        (source_se3 / "calls" / "active" / "call1.json").write_text("c1")
        (source_se3 / "calls" / "active" / "call2.json").write_text("c2")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "calls/active/call1.json" in report.discarded
        assert "calls/active/call2.json" in report.discarded
        assert not (target_se3 / "calls" / "active").exists()


class TestTierC:
    """Tier C directories are completely skipped."""

    def test_tier_c_not_copied(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "cache").mkdir(parents=True)
        (source_se3 / "cache" / "idx.json").write_text("cached")
        (source_se3 / "tmp").mkdir(parents=True)
        (source_se3 / "tmp" / "temp.txt").write_text("tmp")
        (source_se3 / "worktrees").mkdir(parents=True)
        (source_se3 / "worktrees" / "wt1").write_text("wt")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "cache/idx.json" not in report.copied
        assert "tmp/temp.txt" not in report.copied
        assert "worktrees/wt1" not in report.copied
        assert "cache/idx.json" not in report.discarded
        assert "tmp/temp.txt" not in report.discarded
        assert "worktrees/wt1" not in report.discarded


class TestMissingWorktree:
    """When the source worktree is missing, sync is skipped."""

    def test_missing_worktree_returns_skipped(self, tmp_path: Path) -> None:
        import se3.engine.merge.runtime_sync as _rs

        original = _rs._get_worktree_path_for_branch
        _rs._get_worktree_path_for_branch = lambda _pr, _br: None
        try:
            report = sync_branch_runtime(tmp_path, "feature")
            assert report.skipped is True
            assert report.copied == []
            assert report.discarded == []
        finally:
            _rs._get_worktree_path_for_branch = original


class TestEdgeCases:
    """Miscellaneous edge cases."""

    def test_empty_source_se3(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        (source / "se3").mkdir(parents=True)

        call = _make_sync_call(source, target)
        report = call("feature")

        assert report.skipped is False
        assert report.copied == []
        assert report.discarded == []

    def test_source_se3_missing(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        # source/se3 does not exist at all

        call = _make_sync_call(source, target)
        report = call("feature")

        assert report.skipped is False
        assert report.copied == []
        assert report.discarded == []
