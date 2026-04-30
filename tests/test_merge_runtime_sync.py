"""Tests for runtime_sync module — tier A/B/C sync logic.

Uses filesystem fixtures only; no real git is required.
"""

from __future__ import annotations

import os
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

    def test_entry_path_symlink_outside_source_skipped(self, tmp_path: Path) -> None:
        """When a TIER_A_DIRS entry itself is a symlink to outside source_se3,
        the sync skips it entirely without copying anything into target/se3/history.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Create a directory outside source_se3 with files
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir(parents=True)
        (outside_dir / "file1.log").write_text("outside content")
        (outside_dir / "subdir").mkdir()
        (outside_dir / "subdir" / "file2.log").write_text("nested outside")

        # Create source/se3 and make history a symlink to outside_dir
        source_se3.mkdir(parents=True)
        history_link = source_se3 / "history"
        os.symlink(outside_dir, history_link)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Nothing from outside_dir should be copied into target
        assert "history/file1.log" not in report.copied
        assert "history/subdir/file2.log" not in report.copied
        assert not (target_se3 / "history").exists()

    def test_intermediate_dir_symlink_outside_source_skipped(self, tmp_path: Path) -> None:
        """When an intermediate directory (not the TIER_A entry itself) is a
        symlink to outside source_se3, the sync skips files reached through it.
        Example: source_se3/state -> outside, TIER_A entry is state/archive.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Create an outside directory with a nested structure
        outside_dir = tmp_path / "outside"
        (outside_dir / "archive").mkdir(parents=True)
        (outside_dir / "archive" / "foo.log").write_text("outside archive")

        # Create source/se3 and make "state" a symlink to outside_dir
        source_se3.mkdir(parents=True)
        state_link = source_se3 / "state"
        os.symlink(outside_dir, state_link)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Files under state/archive/ reached through the symlink must NOT be copied
        assert "state/archive/foo.log" not in report.copied
        assert not (target_se3 / "state" / "archive" / "foo.log").exists()

    def test_intermediate_dir_symlink_glob_skipped(self, tmp_path: Path) -> None:
        """When an intermediate directory on a glob path is a symlink to outside
        source_se3, glob matches reached through it are skipped.
        Example: source_se3/state -> outside, TIER_A glob is state/summary-*.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Create an outside directory with summary files
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir(parents=True)
        (outside_dir / "summary-abc.md").write_text("outside summary")

        # Create source/se3 and make "state" a symlink to outside_dir
        source_se3.mkdir(parents=True)
        state_link = source_se3 / "state"
        os.symlink(outside_dir, state_link)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Glob matches reached through the intermediate symlink must NOT be copied
        assert "state/summary-abc.md" not in report.copied
        assert not (target_se3 / "state" / "summary-abc.md").exists()

    def test_tier_a_symlink_resolved_to_content(self, tmp_path: Path) -> None:
        """Symlinks in tier A are resolved and their target content is copied.

        Copying the raw link target would create dangling or cross-worktree
        symlinks in the target tree, so we resolve and copy the actual file.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        real_file = source_se3 / "history" / "real.log"
        real_file.write_text("real content")
        symlink_file = source_se3 / "history" / "link.log"
        os.symlink(real_file, symlink_file)

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "history/link.log" in report.copied
        dest_file = target_se3 / "history" / "link.log"
        # Symlink is resolved — destination is a regular file with the content
        assert not dest_file.is_symlink()
        assert dest_file.read_text() == "real content"

    def test_tier_a_relative_symlink_resolved_to_content(self, tmp_path: Path) -> None:
        """Relative symlinks are resolved and the actual content is copied.

        The raw relative link target would be dangling in the target tree,
        so the symlink is dereferenced and the resolved file content is copied.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        real_file = source_se3 / "history" / "real.log"
        real_file.write_text("real content")
        symlink_file = source_se3 / "history" / "link.log"
        # Create a relative symlink: link.log -> ./real.log
        os.symlink("./real.log", symlink_file)

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "history/link.log" in report.copied
        assert "history/real.log" in report.copied
        dest_file = target_se3 / "history" / "link.log"
        # Symlink is resolved — destination is a regular file
        assert not dest_file.is_symlink()
        assert dest_file.read_text() == "real content"

    def test_tier_a_symlink_preserves_target_mode_not_link_mode(self, tmp_path: Path) -> None:
        """When source is a symlink, destination inherits the *target* mode.

        read_bytes() follows the symlink and copies the target's content.
        The matching metadata source must also be the target (via stat()),
        not the symlink itself (via lstat(), which typically reports 0o777).
        """
        import os
        import stat

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        real_file = source_se3 / "history" / "real.log"
        real_file.write_text("real content")
        os.chmod(real_file, 0o600)
        symlink_file = source_se3 / "history" / "link.log"
        os.symlink(real_file, symlink_file)

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "history/link.log" in report.copied
        dest_file = target_se3 / "history" / "link.log"
        assert not dest_file.is_symlink()
        # Destination must have the target's 0o600 mode, not the symlink's 0o777
        dest_mode = stat.S_IMODE(dest_file.stat().st_mode)
        assert dest_mode == 0o600, f"Expected 0o600, got 0o{dest_mode:o}"


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

    def test_worktree_directory_removed_externally_returns_skipped(self, tmp_path: Path) -> None:
        """When git metadata points to a worktree path that was force-removed externally."""
        import se3.engine.merge.runtime_sync as _rs

        nonexistent_path = tmp_path / "ghost-worktree"
        original = _rs._get_worktree_path_for_branch
        _rs._get_worktree_path_for_branch = lambda _pr, _br: nonexistent_path
        try:
            report = sync_branch_runtime(tmp_path, "feature")
            assert report.skipped is True
            assert report.copied == []
            assert report.discarded == []
        finally:
            _rs._get_worktree_path_for_branch = original


class TestTwoPassValidation:
    """Two-pass approach: validate all paths before copying."""

    def test_no_partial_copy_on_second_file_collision(self, tmp_path: Path) -> None:
        """First file is non-colliding, second file collides — nothing is copied."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Set up source with two tier A files
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "second.log").write_text("second")

        # Set up target so only the second file collides
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "second.log").write_text("target second")

        call = _make_sync_call(source, target)
        with pytest.raises(RuntimeSyncCollision) as exc_info:
            call("feature")

        # The first file should NOT have been copied (two-pass validation)
        assert not (target_se3 / "history" / "first.log").exists()
        assert exc_info.value.rel_path == "history/second.log"


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
        # source/se3 does not exist at all, but source/ itself does
        source.mkdir(parents=True)

        call = _make_sync_call(source, target)
        report = call("feature")

        assert report.skipped is False
        assert report.copied == []
        assert report.discarded == []

    def test_source_same_as_target_skipped(self, tmp_path: Path) -> None:
        """When source worktree equals project root, sync is skipped to avoid
        spurious RuntimeSyncCollision on every tier A file."""
        import se3.engine.merge.runtime_sync as _rs

        # Simulate _get_worktree_path_for_branch returning the same path
        original = _rs._get_worktree_path_for_branch
        _rs._get_worktree_path_for_branch = lambda _pr, _br: _pr
        try:
            # Set up a se3 dir with tier A files
            se3 = tmp_path / "se3"
            (se3 / "history").mkdir(parents=True)
            (se3 / "history" / "flow1.log").write_text("log content")

            report = sync_branch_runtime(tmp_path, "feature")
            assert report.skipped is True
            assert report.copied == []
            assert report.discarded == []
        finally:
            _rs._get_worktree_path_for_branch = original


class TestOSErrorPropagation:
    """OS-level errors during copy propagate as OSError, not RuntimeSyncCollision."""

    def test_copy_oserror_propagates(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("log content")

        original_open = os.open
        def _failing_open(path, flags, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        os.open = _failing_open
        try:
            call = _make_sync_call(source, target)
            with pytest.raises(PermissionError):
                call("feature")
        finally:
            os.open = original_open

        # Destination file should NOT have been created
        assert not (target / "se3" / "history" / "flow1.log").exists()

    def test_partial_copy_rolled_back(self, tmp_path: Path) -> None:
        """If copy fails mid-loop, files copied earlier are removed."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "second.log").write_text("second")

        call_count = 0
        original_open = os.open
        def _intermittent_open(path, flags, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise PermissionError(13, "Permission denied", str(path))
            return original_open(path, flags, *args, **kwargs)

        os.open = _intermittent_open
        try:
            call = _make_sync_call(source, target)
            with pytest.raises(PermissionError):
                call("feature")
        finally:
            os.open = original_open

        # first.log should have been copied then rolled back
        assert not (target_se3 / "history" / "first.log").exists()
        # second.log never made it
        assert not (target_se3 / "history" / "second.log").exists()

    def test_rollback_removes_empty_directories(self, tmp_path: Path) -> None:
        """After OSError rollback, directories created by mkdir are removed."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history" / "sub").mkdir(parents=True)
        (source_se3 / "history" / "sub" / "deep.log").write_text("deep")

        original_open = os.open
        def _failing_open(path, flags, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        os.open = _failing_open
        try:
            call = _make_sync_call(source, target)
            with pytest.raises(PermissionError):
                call("feature")
        finally:
            os.open = original_open

        # The file should not exist
        assert not (target_se3 / "history" / "sub" / "deep.log").exists()
        # The intermediate directories created by mkdir should have been removed
        assert not (target_se3 / "history" / "sub").exists()
        assert not (target_se3 / "history").exists()


class TestDirectoryCollision:
    """A directory at the destination path is treated as a collision."""

    def test_dest_directory_raises_collision(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Source has a file
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        # Target has a DIRECTORY at the same relative path
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").mkdir(parents=True)

        call = _make_sync_call(source, target)
        with pytest.raises(RuntimeSyncCollision) as exc_info:
            call("feature")
        assert exc_info.value.rel_path == "history/flow1.log"


class TestSkippedFiles:
    """TOCTOU races (file deleted or becomes a dir after collection) are tracked."""

    def test_file_deleted_after_collection_tracked_as_skipped(self, tmp_path: Path) -> None:
        """Simulate a TOCTOU race where the source file is deleted after collection."""
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "vanishes.log").write_text("will be deleted")

        original_open = os.open
        call_count = 0

        def _deleting_open(path, flags, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "vanishes.log" in str(path):
                # Delete the file to simulate TOCTOU
                os.unlink(str(path))
                raise FileNotFoundError(2, "No such file", str(path))
            return original_open(path, flags, *args, **kwargs)

        os.open = _deleting_open
        try:
            call = _make_sync_call(source, target)
            report = call("feature")
        finally:
            os.open = original_open

        # The first file should have been copied
        assert "history/first.log" in report.copied
        # The vanished file should be tracked as skipped
        assert "history/vanishes.log" in report.skipped_files
        # No empty directories should be left behind
        target_hist = target / "se3" / "history"
        assert target_hist.exists()
        assert (target_hist / "first.log").exists()
        # Only first.log should exist; no stray directories for vanishes.log
        assert not (target_hist / "vanishes.log").exists()


class TestIdempotentModeSync:
    """Idempotent skip (identical content) still syncs file mode."""

    def test_idempotent_skip_syncs_mode(self, tmp_path: Path) -> None:
        import os
        import stat

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        src_file = source_se3 / "history" / "flow1.log"
        src_file.write_text("same content")
        os.chmod(src_file, 0o755)

        (target_se3 / "history").mkdir(parents=True)
        dest_file = target_se3 / "history" / "flow1.log"
        dest_file.write_text("same content")
        os.chmod(dest_file, 0o644)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Should be skipped (idempotent) but mode should be synced
        assert "history/flow1.log" not in report.copied
        assert stat.S_IMODE(dest_file.stat().st_mode) == 0o755

    def test_idempotent_skip_symlink_syncs_target_mode(self, tmp_path: Path) -> None:
        """Idempotent skip with a symlink source syncs the *target* mode.

        When the source is a symlink and the destination already has identical
        content, the metadata convergence must use stat() (target mode) not
        lstat() (symlink mode, typically 0o777).
        """
        import os
        import stat

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        real_file = source_se3 / "history" / "real.log"
        real_file.write_text("same content")
        os.chmod(real_file, 0o600)
        symlink_file = source_se3 / "history" / "link.log"
        os.symlink(real_file, symlink_file)

        (target_se3 / "history").mkdir(parents=True)
        dest_file = target_se3 / "history" / "link.log"
        dest_file.write_text("same content")
        os.chmod(dest_file, 0o644)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Should be skipped (idempotent) but mode should be synced from target
        assert "history/link.log" not in report.copied
        dest_mode = stat.S_IMODE(dest_file.stat().st_mode)
        assert dest_mode == 0o600, f"Expected 0o600, got 0o{dest_mode:o}"


class TestBrokenSymlinks:
    """Broken symlinks inside source_se3 are skipped consistently."""

    def test_broken_symlink_skipped_not_raised(self, tmp_path: Path) -> None:
        """A broken symlink inside source_se3 is skipped rather than raising
        FileNotFoundError during validation or copy."""
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "real.log").write_text("real content")
        broken_link = source_se3 / "history" / "broken.log"
        os.symlink(source_se3 / "history" / "nonexistent.log", broken_link)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Broken symlink should be in skipped_files, not cause an error
        assert "history/broken.log" in report.skipped_files
        # Other valid files should still be copied
        assert "history/real.log" in report.copied

    def test_broken_symlink_does_not_abort_sync(self, tmp_path: Path) -> None:
        """A broken symlink alone should not abort the entire sync."""
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        broken_link = source_se3 / "history" / "broken.log"
        os.symlink(source_se3 / "history" / "missing.log", broken_link)

        call = _make_sync_call(source, target)
        report = call("feature")

        assert report.skipped is False
        assert "history/broken.log" in report.skipped_files
        assert report.copied == []
