"""Tests for runtime_sync module — tier A/B/C sync logic.

Uses filesystem fixtures only; no real git is required.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from se3.engine.merge.runtime_sync import (
    RuntimeSyncCollision,
    SyncReport,
    sync_branch_runtime,
)


def _make_sync_call(source_dir: Path, target_dir: Path, *, strict: bool = False):
    """Return a callable that invokes sync_branch_runtime with a mocked worktree lookup.

    The returned callable accepts ``branch`` and returns a ``SyncReport``.
    """
    def _call(branch: str) -> SyncReport:
        import se3.engine.merge.runtime_sync as _rs
        original = _rs._get_worktree_path_for_branch
        _rs._get_worktree_path_for_branch = lambda _pr, _br: source_dir
        try:
            return sync_branch_runtime(target_dir, branch, strict=strict)
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

        (source_se3 / "state" / "archive" / "sub").mkdir(parents=True)
        (source_se3 / "state" / "archive" / "sub" / "snapshot.md").write_text("snapshot")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "state/archive/sub/snapshot.md" in report.copied
        assert (target_se3 / "state" / "archive" / "sub" / "snapshot.md").exists()

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

        call = _make_sync_call(source, target)
        report = call("feature")

        copied_set = set(report.copied)
        assert "history/h.log" in copied_set
        assert "logs/l.log" in copied_set
        assert "state/summary-x.md" in copied_set
        assert "state/archive/a.md" in copied_set
        assert "calls/confirm_y.json" in copied_set

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

    def test_symlink_into_tier_c_skipped(self, tmp_path: Path) -> None:
        """A tier-A symlink that resolves into a tier-C directory is skipped.

        This prevents tier-C content (e.g. cache files) from leaking across
        via tier-A traversal with a tier-A relative path.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Create a tier-C directory with a file
        (source_se3 / "cache").mkdir(parents=True)
        (source_se3 / "cache" / "index.db").write_text("cached data")

        # Create a tier-A directory with a symlink pointing to tier-C
        (source_se3 / "history").mkdir(parents=True)
        symlink_to_cache = source_se3 / "history" / "cached"
        os.symlink(source_se3 / "cache", symlink_to_cache)

        call = _make_sync_call(source, target)
        report = call("feature")

        # Nothing from cache/ should leak through the symlink
        assert "cache/index.db" not in report.copied
        assert "history/cached/index.db" not in report.copied
        assert not (target_se3 / "history" / "cached").exists()
        assert not (target_se3 / "cache" / "index.db").exists()

    def test_symlink_to_tier_c_file_skipped(self, tmp_path: Path) -> None:
        """A tier-A symlink that resolves to a tier-C file is skipped."""
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Create a tier-C file
        (source_se3 / "cache").mkdir(parents=True)
        (source_se3 / "cache" / "index.db").write_text("cached data")

        # Create a symlink in tier-A pointing to the tier-C file
        (source_se3 / "history").mkdir(parents=True)
        symlink_file = source_se3 / "history" / "cached.db"
        os.symlink(source_se3 / "cache" / "index.db", symlink_file)

        call = _make_sync_call(source, target)
        report = call("feature")

        # The symlink-to-tier-C file should not be copied
        assert "history/cached.db" not in report.copied
        assert not (target_se3 / "history" / "cached.db").exists()

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
    """Tier A collisions raise RuntimeSyncCollision in strict mode."""

    def test_same_relative_path_collision(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        call = _make_sync_call(source, target, strict=True)
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

        call = _make_sync_call(source, target, strict=True)
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


class TestLenientCollision:
    """Tier A collisions are bypassed via sidecar files in lenient (default) mode."""

    def test_collision_bypassed_to_sidecar(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Target should remain unchanged
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"
        # Sidecar should contain source version
        sidecar = target_se3 / "history" / "flow1.log.from-feature"
        assert sidecar.exists()
        assert sidecar.read_text() == "source log"
        # Report should record the collision
        assert len(report.collisions) == 1
        assert report.collisions[0].branch == "feature"
        assert report.collisions[0].original_rel_path == "history/flow1.log"
        assert report.collisions[0].sidecar_rel_path == "history/flow1.log.from-feature"

    def test_glob_collision_bypassed(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "summary-x.md").write_text("source")
        (target_se3 / "state").mkdir(parents=True)
        (target_se3 / "state" / "summary-x.md").write_text("target")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        assert (target_se3 / "state" / "summary-x.md").read_text() == "target"
        sidecar = target_se3 / "state" / "summary-x.md.from-feature"
        assert sidecar.exists()
        assert sidecar.read_text() == "source"
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "state/summary-x.md"

    def test_glob_dir_collision_graceful_skip(self, tmp_path: Path) -> None:
        """Glob path directory collision in lenient mode is skipped, not aborted.

        Audit-trail uniformity: directory-at-dest also records an audit-only
        ``BypassedCollision`` row (``written=False``), matching the bypass
        loop's ``sidecar_is_directory`` branch.
        """
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "summary-x.md").write_text("source")
        # Target has a DIRECTORY at the same relative path
        (target_se3 / "state").mkdir(parents=True)
        (target_se3 / "state" / "summary-x.md").mkdir()

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should not abort — directory collision is skipped gracefully
        assert "state/summary-x.md" in report.skipped_files
        # Audit-only collision recorded for trail uniformity
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "state/summary-x.md"
        assert report.collisions[0].written is False
        assert report.collisions[0].dest_hash == "unavailable"

    def test_branch_name_with_slash_safe(self, tmp_path: Path) -> None:
        """Branch names containing '/' are safely transformed in sidecar filenames."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        call = _make_sync_call(source, target, strict=False)
        report = call("feat/branch")

        sidecar = target_se3 / "history" / "flow1.log.from-feat__branch"
        assert sidecar.exists()
        assert len(report.collisions) == 1
        assert report.collisions[0].sidecar_rel_path == "history/flow1.log.from-feat__branch"

    def test_sidecar_idempotent(self, tmp_path: Path) -> None:
        """If sidecar already exists with identical content, treat as idempotent."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("same content")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")
        # Pre-existing sidecar with same content as source
        (target_se3 / "history" / "flow1.log.from-feature").write_text("same content")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Idempotent matches are NOT recorded in collisions so re-runs do
        # not surface spurious warnings.
        assert len(report.collisions) == 0
        # Sidecar content unchanged
        sidecar = target_se3 / "history" / "flow1.log.from-feature"
        assert sidecar.read_text() == "same content"
        # Weak signal: idempotent_bypasses counter is incremented so
        # operators inheriting a worktree with stale sidecar leftovers can
        # detect that prior runs preserved divergent source data.
        assert report.idempotent_bypasses == 1
        # Per-file audit detail is recorded in a parallel list so operators
        # investigating stale sidecars have exact names without rerunning.
        assert len(report.idempotent_bypass_records) == 1
        assert report.idempotent_bypass_records[0].sidecar_rel_path == "history/flow1.log.from-feature"

    def test_sidecar_hash_suffix_disambiguation(self, tmp_path: Path) -> None:
        """If sidecar exists with different content, use hash-suffix path."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")
        # Pre-existing sidecar with DIFFERENT content
        (target_se3 / "history" / "flow1.log.from-feature").write_text("old sidecar")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Original sidecar should remain
        assert (target_se3 / "history" / "flow1.log.from-feature").read_text() == "old sidecar"
        # Hash-suffix sidecar should be created
        hash_suffix_files = list((target_se3 / "history").glob("flow1.log.from-feature.*"))
        assert len(hash_suffix_files) == 1
        assert hash_suffix_files[0].read_text() == "source log"
        assert len(report.collisions) == 1
        assert report.collisions[0].sidecar_rel_path.startswith("history/flow1.log.from-feature.")

    def test_collision_uses_long_hash_suffix_when_short_exhausted(
        self, tmp_path: Path,
    ) -> None:
        """When primary sidecar and 8-char hash suffix both exist with different
        content, the 16-char hash suffix is used as a fallback."""
        import hashlib

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Pre-populate primary sidecar and 8-char hash suffix with DIFFERENT content
        (target_se3 / "history" / "flow1.log.from-feature").write_text("old sidecar")
        src_hash = hashlib.sha256(b"source log").hexdigest()
        short_hash = src_hash[:8]
        (target_se3 / "history" / f"flow1.log.from-feature.{short_hash}").write_text(
            "old hash-suffix content"
        )

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Original sidecar and 8-char suffix should remain
        assert (
            target_se3 / "history" / "flow1.log.from-feature"
        ).read_text() == "old sidecar"
        assert (
            target_se3 / "history" / f"flow1.log.from-feature.{short_hash}"
        ).read_text() == "old hash-suffix content"
        # 16-char hash-suffix sidecar should be created
        long_hash = src_hash[:16]
        long_sidecar = target_se3 / "history" / f"flow1.log.from-feature.{long_hash}"
        assert long_sidecar.exists()
        assert long_sidecar.read_text() == "source log"
        assert len(report.collisions) == 1
        assert report.collisions[0].sidecar_rel_path == (
            f"history/flow1.log.from-feature.{long_hash}"
        )

    def test_lenient_mode_sequence_continues(self, tmp_path: Path) -> None:
        """In lenient mode, a collision on one file does not block copying other files."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source")
        (source_se3 / "history" / "newfile.log").write_text("new content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Non-colliding file should still be copied
        assert "history/newfile.log" in report.copied
        assert (target_se3 / "history" / "newfile.log").read_text() == "new content"
        # Colliding file should be bypassed
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "history/collides.log"

    def test_lenient_mode_preserves_metadata(self, tmp_path: Path) -> None:
        """Sidecar files preserve source mtime and mode."""
        import os
        import stat

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        src_file = source_se3 / "history" / "flow1.log"
        src_file.write_text("source log")
        os.chmod(src_file, 0o640)
        os.utime(src_file, (1234567890, 1234567890))

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        sidecar = target_se3 / "history" / "flow1.log.from-feature"
        assert sidecar.exists()
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o640
        assert sidecar.stat().st_mtime == 1234567890

    def test_two_pass_no_partial_on_collision(self, tmp_path: Path) -> None:
        """In lenient mode with multiple collisions, all sidecars are written or none."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "second.log").write_text("second")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "first.log").write_text("target first")
        (target_se3 / "history" / "second.log").write_text("target second")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Both should be bypassed
        assert len(report.collisions) == 2
        assert (target_se3 / "history" / "first.log.from-feature").exists()
        assert (target_se3 / "history" / "second.log.from-feature").exists()

    def test_sidecar_disambiguation_exhausted_skipped(self, tmp_path: Path) -> None:
        """When primary, 8-char, and 16-char sidecar paths all exist with
        different content, the file is skipped rather than halting the sync."""
        import hashlib

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        src_file = source_se3 / "history" / "flow1.log"
        src_file.write_text("source log from branch")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Compute the hash of source content to determine what the hash-suffix
        # sidecar paths would be.
        src_content = b"source log from branch"
        src_hash = hashlib.sha256(src_content).hexdigest()
        short_hash = src_hash[:8]
        long_hash = src_hash[:16]

        # Pre-populate ALL three sidecar paths with different content
        (target_se3 / "history" / "flow1.log.from-feature").write_text("old sidecar content")
        (target_se3 / "history" / f"flow1.log.from-feature.{short_hash}").write_text("old hash-suffix content")
        (target_se3 / "history" / f"flow1.log.from-feature.{long_hash}").write_text("old long-hash content")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should be recorded as skipped (not copied)
        assert "history/flow1.log" in report.skipped_files
        # Collision is recorded for audit trail even when sidecar write
        # failed, so operators see a uniform entry in runtime_sync_collisions.
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "history/flow1.log"
        assert report.collisions[0].branch == "feature"
        # Non-colliding files (if present) should still be copied
        # Destination unchanged
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"

    def test_source_contains_sidecar_named_file(self, tmp_path: Path) -> None:
        """Source-side files matching the sidecar filename pattern are
        SKIPPED (Task 32 / E3): never propagate forward as if they were
        real runtime data, otherwise re-runs accumulate
        ``foo.from-A.from-B`` chains.  The ordinary tier-A file in the
        same directory still runs through the normal collision path.
        """
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Source has both a regular file and a sidecar-named file.  The
        # sidecar-named file represents output left over by a prior
        # ``se3 merge`` invocation that ran into a collision and wrote
        # to a sidecar slot.  Subsequent merges from this branch must
        # not pick that sidecar back up as input data.
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "x.log").write_text("main file content")
        (source_se3 / "history" / "x.log.from-feature").write_text("sidecar-named source file")

        # Target already has x.log
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "x.log").write_text("target x.log content")

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # x.log.from-feature must NOT be copied — it matches the sidecar
        # pattern and is filtered at collection time.
        assert "history/x.log.from-feature" not in report.copied
        # And it must NOT show up as a skipped file either: collection-
        # time filtering removes it from the candidate set entirely.
        assert "history/x.log.from-feature" not in report.skipped_files

        # x.log should still have a collision recorded with sidecar (this
        # is unchanged by the E3 filter — only the sidecar-named source
        # file is filtered, the ordinary file still runs through).
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "history/x.log"
        sidecar_rel = report.collisions[0].sidecar_rel_path
        assert sidecar_rel == "history/x.log.from-feature"
        sidecar_path = target_se3 / sidecar_rel
        # The sidecar at the target must contain x.log's source content
        # (created by the bypass loop), NOT the source-side
        # x.log.from-feature content (which was filtered out).
        assert sidecar_path.exists(), f"sidecar {sidecar_path} does not exist"
        assert sidecar_path.read_text() == "main file content"
        assert sidecar_path.read_text() != "sidecar-named source file"
        # Target x.log must remain unchanged
        assert (target_se3 / "history" / "x.log").read_text() == "target x.log content"

    def test_oserror_during_bypass_skips_not_aborts(
        self, tmp_path: Path,
    ) -> None:
        """When an OSError occurs mid-bypass (e.g. on the second collision),
        the file is skipped and the sync continues — already-copied tier-A
        files and earlier sidecars are preserved."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Tier A files in source:
        #   a.log — no collision (will be copied)
        #   b.log — collision (first bypass, succeeds)
        #   c.log — collision (second bypass, triggers OSError)
        for name in ("a.log", "b.log", "c.log"):
            (source_se3 / "history").mkdir(parents=True, exist_ok=True)
            (source_se3 / "history" / name).write_text(f"source {name}")

        # Target has b.log and c.log (collisions), but NOT a.log
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "b.log").write_text("target b")
        (target_se3 / "history" / "c.log").write_text("target c")

        call = _make_sync_call(source, target, strict=False)

        # Mock _write_sidecar: first call passes through, second raises OSError
        original_write_sidecar = _rs._write_sidecar
        write_count = [0]

        def flaky_write_sidecar(*args, **kwargs):
            write_count[0] += 1
            if write_count[0] >= 2:
                raise OSError(28, "No space left on device")
            return original_write_sidecar(*args, **kwargs)

        _rs._write_sidecar = flaky_write_sidecar
        try:
            report = call("feature")
        finally:
            _rs._write_sidecar = original_write_sidecar

        # Sync should complete (not raise)
        #   - Copied tier-A file (a.log) is preserved
        assert (target_se3 / "history" / "a.log").exists()
        assert (target_se3 / "history" / "a.log").read_text() == "source a.log"
        #   - Sidecar from first bypass (b.log.from-feature) is preserved
        assert (target_se3 / "history" / "b.log.from-feature").exists()
        assert (target_se3 / "history" / "b.log.from-feature").read_text() == "source b.log"
        #   - c.log is skipped (OSError during bypass)
        assert "history/c.log" in report.skipped_files
        #   - Target original files must be intact
        assert (target_se3 / "history" / "b.log").read_text() == "target b"
        assert (target_se3 / "history" / "c.log").read_text() == "target c"


class TestTierB:
    """Tier B files are recorded as discarded but not copied."""

    def test_tier_b_files_discarded(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "engine.json").write_text("{}")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "state/engine.json" in report.discarded
        assert not (target_se3 / "state" / "engine.json").exists()

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

        call = _make_sync_call(source, target, strict=True)
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
    """OS-level errors during copy are skipped rather than aborting the sync."""

    def test_copy_oserror_skips_file(self, tmp_path: Path) -> None:
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
            report = call("feature")
        finally:
            os.open = original_open

        # Sync succeeds; file is skipped rather than aborting
        assert "history/flow1.log" in report.skipped_files
        assert not (target / "se3" / "history" / "flow1.log").exists()

    def test_partial_copy_earlier_files_remain(self, tmp_path: Path) -> None:
        """If copy fails mid-loop, earlier files remain; later files are skipped."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "second.log").write_text("second")

        call_count = 0
        original_open = os.open
        # E1/E5 TOCTOU hardening: ``_atomic_write_bytes`` now opens the
        # parent directory with ``O_DIRECTORY|O_NOFOLLOW`` so the rename
        # is pinned to a specific dirfd. This adds one extra ``os.open``
        # call per write. Per-file os.open count: 1 (read source) +
        # 1 (mkstemp) + 1 (parent dir_fd) = 3.  Fail from the 4th call
        # onward so the first file's read+write completes (3 calls),
        # then the second file's first os.open trips.
        def _intermittent_open(path, flags, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                raise PermissionError(13, "Permission denied", str(path))
            return original_open(path, flags, *args, **kwargs)

        os.open = _intermittent_open
        try:
            call = _make_sync_call(source, target)
            report = call("feature")
        finally:
            os.open = original_open

        # first.log was copied successfully and remains
        assert (target_se3 / "history" / "first.log").exists()
        assert "history/first.log" in report.copied
        # second.log was skipped (not rolled back)
        assert not (target_se3 / "history" / "second.log").exists()
        assert "history/second.log" in report.skipped_files

    def test_copy_oserror_skips_without_rollback(self, tmp_path: Path) -> None:
        """OSError during copy skips the file; no rollback occurs."""
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
            report = call("feature")
        finally:
            os.open = original_open

        # Sync succeeds; file is skipped
        assert "history/sub/deep.log" in report.skipped_files
        # The file should not exist (read failed before mkdir)
        assert not (target_se3 / "history" / "sub" / "deep.log").exists()

    def test_copy_phase_enospc_logs_warning(
        self, tmp_path: Path, monkeypatch, caplog,
    ) -> None:
        """When _atomic_write_bytes raises ENOSPC in the copy phase,
        a WARNING is logged (symmetric with the bypass-phase handler)."""
        import logging
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("log content")

        def _failing_atomic(dest_path: Path, content: bytes, **kwargs) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(_rs, "_atomic_write_bytes", _failing_atomic)

        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            call = _make_sync_call(source, target)
            report = call("feature")

        assert "history/flow1.log" in report.skipped_files
        # WARNING should be logged for ENOSPC in copy phase
        assert "source data is not represented on disk" in caplog.text


class TestTOCTOU:
    """TOCTOU (Time-of-Check-Time-of-Use) defenses in the copy phase."""

    def test_toctou_dest_appears_different_content_lenient_bypassed(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If dest_file appears between validation and copy with different
        content, the lenient-mode TOCTOU re-check routes to the bypass loop
        so the source version is preserved as a sidecar (symmetric with the
        pre-validation phase)."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source content")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            # Create dest_file with DIFFERENT content after read
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text("different content")
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should be bypassed to sidecar (not skipped, not copied)
        assert "history/flow1.log" not in report.skipped_files
        assert "history/flow1.log" not in report.copied
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "history/flow1.log"
        # Sidecar should contain source version
        sidecar = target_se3 / "history" / "flow1.log.from-feature"
        assert sidecar.exists()
        assert sidecar.read_text() == "source content"
        # The file at dest should have the "different content" (not overwritten)
        assert (target_se3 / "history" / "flow1.log").read_text() == "different content"

    def test_toctou_dest_appears_same_content_idempotent_skip(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If dest_file appears between validation and copy with identical
        content, the TOCTOU re-check treats it as idempotent and skips."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("same content")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            # Create dest_file with SAME content after read
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text("same content")
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should be idempotent — not copied, not skipped, not collision
        assert "history/flow1.log" not in report.copied
        assert "history/flow1.log" not in report.skipped_files
        assert len(report.collisions) == 0
        # File exists at dest with the same content
        assert (target_se3 / "history" / "flow1.log").read_text() == "same content"

    def test_toctou_dest_appears_different_content_strict_raises(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If dest_file appears between validation and copy with different
        content, the strict-mode TOCTOU re-check raises RuntimeSyncCollision."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source content")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text("different content")
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        call = _make_sync_call(source, target, strict=True)
        with pytest.raises(RuntimeSyncCollision):
            call("feature")

    def test_toctou_recheck_file_hash_oserror_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If _file_hash raises OSError during the TOCTOU re-check,
        the file is skipped rather than aborting the sync.

        Audit-trail uniformity: when the bypass loop encounters an OSError
        during the dest-hash re-read inside _write_sidecar (transient I/O,
        permission error, etc.), the BypassedCollision is recorded with a
        placeholder dest_hash so operators reading ``report.collisions``
        see a uniform row for every collided file.
        """
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source content")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            # Create dest_file AFTER read (simulating concurrent writer)
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text("concurrent content")
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        # Make _file_hash raise OSError during the re-check
        original_file_hash = _rs._file_hash

        def _failing_file_hash(path: Path) -> str:
            # Only raise for the target file during re-check
            if str(path).startswith(str(target_se3)) and "flow1.log" in str(path):
                raise OSError(5, "Input/output error", str(path))
            return original_file_hash(path)

        monkeypatch.setattr(_rs, "_file_hash", _failing_file_hash)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Skipped (not copied)
        assert "history/flow1.log" in report.skipped_files
        assert "history/flow1.log" not in report.copied
        # Audit row recorded with placeholder dest_hash so operators see a
        # uniform entry for every collided file.
        assert len(report.collisions) == 1
        assert report.collisions[0].original_rel_path == "history/flow1.log"
        assert report.collisions[0].dest_hash == "unavailable"
        # Dest file should retain the concurrent content
        assert (target_se3 / "history" / "flow1.log").read_text() == "concurrent content"

    def test_toctou_recheck_oserror_in_copy_phase_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If dest_file appears between validation and copy with same-size
        different content and _file_hash raises OSError during the copy-phase
        TOCTOU re-check, the file is skipped (not copied, not bypassed).

        This is distinct from test_toctou_recheck_file_hash_oserror_skipped:
        here the dest and source have the SAME byte length, so the size fast-
        path in _check_collision does not short-circuit. The OSError from
        _file_hash(dest_file) propagates to the copy-phase ``except OSError``
        rather than routing through the bypass loop.
        """
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Same-size, different content (12 bytes each)
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("src content!")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            # Create dest_file AFTER read with same-size different content
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text("dst content!")
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        # Make _file_hash raise OSError ONLY for the target dest file
        original_file_hash = _rs._file_hash

        def _failing_file_hash(path: Path) -> str:
            if str(path).startswith(str(target_se3)) and "flow1.log" in str(path):
                raise OSError(5, "Input/output error", str(path))
            return original_file_hash(path)

        monkeypatch.setattr(_rs, "_file_hash", _failing_file_hash)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Skipped via copy-phase ``except OSError`` — not copied, not bypassed
        assert "history/flow1.log" in report.skipped_files
        assert "history/flow1.log" not in report.copied
        # No collision recorded (OSError path does not route to bypass loop)
        assert len(report.collisions) == 0
        # Dest file retains the concurrent content
        assert (target_se3 / "history" / "flow1.log").read_text() == "dst content!"
        # No sidecar should be created
        assert not (target_se3 / "history" / "flow1.log.from-feature").exists()


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

        call = _make_sync_call(source, target, strict=True)
        with pytest.raises(RuntimeSyncCollision) as exc_info:
            call("feature")
        assert exc_info.value.rel_path == "history/flow1.log"

    def test_dest_directory_lenient_skipped(self, tmp_path: Path) -> None:
        """In lenient mode, a directory at the destination path is skipped
        rather than aborting the entire sync."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").mkdir(parents=True)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Directory collision should be skipped, not aborting
        assert "history/flow1.log" in report.skipped_files
        # Other non-colliding files should still be processed (none in this case)

    def test_dest_directory_lenient_records_audit_only_collision(
        self, tmp_path: Path,
    ) -> None:
        """A directory at the destination path in lenient mode produces BOTH
        a ``skipped_files`` entry AND an audit-only ``BypassedCollision``
        record (``written=False``).

        This locks in the contract that every lenient-mode 'lost data' path
        is recorded uniformly in ``collisions``, mirroring the bypass loop's
        ``sidecar_is_directory`` branch.  A regression that recorded the
        directory-at-dest case only in ``skipped_files`` (asymmetric with the
        sidecar-is-dir path) would force operators to cross-reference both
        lists and break the documented audit-trail uniformity.
        """
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").mkdir(parents=True)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # New contract: dest-is-directory → both skipped_files and audit-only collision
        assert "history/flow1.log" in report.skipped_files
        assert len(report.collisions) == 1
        collision = report.collisions[0]
        assert collision.original_rel_path == "history/flow1.log"
        assert collision.branch == "feature"
        assert collision.written is False
        assert collision.dest_hash == "unavailable"
        assert collision.sidecar_rel_path == "history/flow1.log.from-feature"
        # Idempotent counter is unaffected by directory collisions
        assert report.idempotent_bypasses == 0

    def test_dest_directory_lenient_with_other_files_still_copies(self, tmp_path: Path) -> None:
        """In lenient mode, a directory collision on one file does not block
        copying other non-colliding files."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")
        (source_se3 / "history" / "flow2.log").write_text("other log")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").mkdir(parents=True)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Directory collision skipped
        assert "history/flow1.log" in report.skipped_files
        # Other file still copied
        assert "history/flow2.log" in report.copied
        assert (target_se3 / "history" / "flow2.log").read_text() == "other log"


    def test_sidecar_directory_collision_lenient_skipped(self, tmp_path: Path, caplog) -> None:
        """When the sidecar path is a directory, lenient mode skips the file
        rather than aborting the sync, and logs a distinct warning."""
        import logging

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")
        # Sidecar path is a directory
        (target_se3 / "history" / "flow1.log.from-feature").mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            call = _make_sync_call(source, target, strict=False)
            report = call("feature")

        # Should be skipped, not aborting
        assert "history/flow1.log" in report.skipped_files
        # Original file unchanged
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"
        # Warning must identify the real cause (directory), not "disambiguation exhausted"
        assert any(
            "sidecar path is a directory" in record.message
            and "history/flow1.log" in record.message
            for record in caplog.records
        )
        assert not any(
            "sidecar disambiguation exhausted" in record.message
            for record in caplog.records
        )

    def test_hash_suffix_sidecar_directory_lenient_skipped(self, tmp_path: Path, caplog) -> None:
        """When the hash-suffix sidecar path is a directory, lenient mode
        skips the file rather than aborting, and logs a distinct warning."""
        import hashlib
        import logging

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")
        # Plain sidecar exists with different content
        (target_se3 / "history" / "flow1.log.from-feature").write_text("old sidecar")
        # Hash-suffix sidecar path is a directory
        src_hash = hashlib.sha256(b"source log").hexdigest()
        short_hash = src_hash[:8]
        (target_se3 / "history" / f"flow1.log.from-feature.{short_hash}").mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            call = _make_sync_call(source, target, strict=False)
            report = call("feature")

        # Should be skipped
        assert "history/flow1.log" in report.skipped_files
        # Warning must identify the real cause (directory), not "disambiguation exhausted"
        assert any(
            "sidecar path is a directory" in record.message
            and "history/flow1.log" in record.message
            for record in caplog.records
        )
        assert not any(
            "sidecar disambiguation exhausted" in record.message
            for record in caplog.records
        )

    def test_fifo_at_dest_lenient_skipped(self, tmp_path: Path) -> None:
        """A FIFO at the destination path in lenient mode is skipped,
        not routed through the bypass loop, and recorded in collisions
        as an audit-only entry for audit-trail uniformity."""
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source log")

        (target_se3 / "history").mkdir(parents=True)
        # Create a FIFO at the destination path
        dest_fifo = target_se3 / "history" / "flow1.log"
        os.mkfifo(str(dest_fifo))

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should be skipped directly (not aborting, not bypassed)
        assert "history/flow1.log" in report.skipped_files
        # Audit-trail uniformity: non-regular dest is recorded as an
        # audit-only collision (written=False), matching the directory case.
        assert len(report.collisions) == 1
        collision = report.collisions[0]
        assert collision.original_rel_path == "history/flow1.log"
        assert collision.written is False
        assert collision.dest_hash == "unavailable"
        # No sidecar should be created
        assert not (target_se3 / "history" / "flow1.log.from-feature").exists()
        # FIFO must remain intact
        assert dest_fifo.exists()
        assert stat.S_ISFIFO(dest_fifo.stat().st_mode)


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


class TestValidationPhaseOSError:
    """Transient IO errors during validation are skipped, not aborted."""

    def test_file_hash_raises_oserror_during_validation_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If _file_hash raises OSError on a source file during validation,
        the file is added to skipped_files and the sync continues."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "unreadable.log").write_text("unreadable")

        # Make unreadable.log collide so _file_hash is called during validation
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "unreadable.log").write_text("target")

        original_file_hash = _rs._file_hash
        call_count = 0

        def _failing_file_hash(path: Path, source_se3: Path | None = None) -> str:
            nonlocal call_count
            call_count += 1
            if "unreadable.log" in str(path) and str(path).startswith(str(source_se3 or "")):
                raise OSError(5, "Input/output error", str(path))
            return original_file_hash(path, source_se3)

        monkeypatch.setattr(_rs, "_file_hash", _failing_file_hash)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # The readable file should be copied
        assert "history/first.log" in report.copied
        # The unreadable file should be skipped
        assert "history/unreadable.log" in report.skipped_files

    def test_dest_file_hash_raises_oserror_during_validation_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If _file_hash raises OSError on an existing destination file during
        collision check, the file is added to skipped_files and the sync
        continues."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "first.log").write_text("first")
        (source_se3 / "history" / "collides.log").write_text("source")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target")

        original_file_hash = _rs._file_hash
        call_count = 0

        def _failing_file_hash(path: Path, source_se3: Path | None = None) -> str:
            nonlocal call_count
            call_count += 1
            if "collides.log" in str(path) and str(path).startswith(str(target_se3)):
                raise OSError(5, "Input/output error", str(path))
            return original_file_hash(path, source_se3)

        monkeypatch.setattr(_rs, "_file_hash", _failing_file_hash)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # The non-colliding file should be copied
        assert "history/first.log" in report.copied
        # The colliding file whose dest can't be hashed should be skipped
        assert "history/collides.log" in report.skipped_files


class TestBypassOSError:
    """OSError during bypass-phase sidecar write is handled gracefully."""

    def test_bypass_oserror_skipped_not_aborted(self, tmp_path: Path, monkeypatch) -> None:
        """When _atomic_write_bytes raises OSError during sidecar write,
        the file is skipped and the sync continues without full rollback."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source")
        (source_se3 / "history" / "newfile.log").write_text("new content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target")

        original_atomic = _rs._atomic_write_bytes
        call_count = 0

        def _failing_atomic(dest_path: Path, content: bytes, **kwargs) -> None:
            nonlocal call_count
            call_count += 1
            if "collides.log.from-feature" in str(dest_path):
                raise OSError(28, "No space left on device")
            return original_atomic(dest_path, content)

        monkeypatch.setattr(_rs, "_atomic_write_bytes", _failing_atomic)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # The colliding file should be skipped (not rolled back)
        assert "history/collides.log" in report.skipped_files
        # The non-colliding file should still be copied
        assert "history/newfile.log" in report.copied
        assert (target_se3 / "history" / "newfile.log").exists()
        # No sidecar should exist for the failed write
        assert not (target_se3 / "history" / "collides.log.from-feature").exists()

    def test_bypass_oserror_records_bypassed_collision_audit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When sidecar write fails with OSError, the audit trail records a
        BypassedCollision entry alongside the skipped_files entry, so
        operators reading ``report.collisions`` see a uniform row for every
        collided file rather than having to cross-reference logs."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        def _failing_atomic(dest_path: Path, content: bytes, **kwargs) -> None:
            if "collides.log.from-feature" in str(dest_path):
                raise OSError(36, "File name too long", str(dest_path))
            return None  # Other writes treated as no-op (shouldn't be hit)

        monkeypatch.setattr(_rs, "_atomic_write_bytes", _failing_atomic)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Skipped_files records the data-loss path
        assert "history/collides.log" in report.skipped_files
        # Audit trail uniformity: collisions also records the failed bypass
        assert len(report.collisions) == 1
        recorded = report.collisions[0]
        assert recorded.branch == "feature"
        assert recorded.original_rel_path == "history/collides.log"
        # sidecar_rel_path identifies the would-be target so operators can
        # locate where the data should have landed
        assert recorded.sidecar_rel_path == "history/collides.log.from-feature"

    def test_bypass_dest_file_hash_oserror_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When _file_hash(dest_file) raises OSError inside _write_sidecar,
        the bypass loop's ``except OSError`` catches it and records the
        collision with ``dest_hash='unavailable'`` rather than rolling back
        already-copied tier-A files."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("src")
        (source_se3 / "history" / "newfile.log").write_text("new content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        original_file_hash = _rs._file_hash

        def _failing_file_hash(path: Path, source_se3: Path | None = None) -> str:
            # Raise OSError when called on the destination file inside
            # _write_sidecar.  Different sizes (3 vs 14 bytes) ensure the
            # size fast-path in _check_collision triggers RuntimeSyncCollision
            # *before* _file_hash(dest_file) is called there, so this failure
            # only fires inside _write_sidecar.
            if str(path) == str(target_se3 / "history" / "collides.log"):
                raise OSError(13, "Permission denied", str(path))
            return original_file_hash(path, source_se3)

        monkeypatch.setattr(_rs, "_file_hash", _failing_file_hash)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should be skipped, not rolled back
        assert "history/collides.log" in report.skipped_files
        assert len(report.collisions) == 1
        assert report.collisions[0].dest_hash == "unavailable"
        assert report.collisions[0].written is False
        # Non-colliding file should still be copied
        assert "history/newfile.log" in report.copied
        assert (target_se3 / "history" / "newfile.log").exists()
        # Target file should remain untouched
        assert (target_se3 / "history" / "collides.log").read_text() == "target content"

    def test_bypass_preflight_enametoolong_skipped(
        self, tmp_path: Path, monkeypatch, caplog,
    ) -> None:
        """When the sidecar filename exceeds NAME_MAX, the preflight check
        catches it and records a clear ENAMETOOLONG warning rather than
        relying on the OS error at write time."""
        import logging
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        # Monkey-patch the preflight to trigger unconditionally for this test
        original_write_sidecar = _rs._write_sidecar

        def _preflight_failing_sidecar(*args, **kwargs):
            # Inject a RuntimeSyncCollision with ENAMETOOLONG to test the
            # bypass-loop handling path without needing a 255-char filename.
            sidecar_path = target_se3 / "history" / "collides.log.from-feature"
            raise _rs.RuntimeSyncCollision(
                "history/collides.log",
                reason="sidecar_write_os_error",
                sidecar_path=str(sidecar_path),
                errno_code=_rs.errno.ENAMETOOLONG,
            )

        monkeypatch.setattr(_rs, "_write_sidecar", _preflight_failing_sidecar)

        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            call = _make_sync_call(source, target, strict=False)
            report = call("feature")

        assert "history/collides.log" in report.skipped_files
        assert len(report.collisions) == 1
        assert report.collisions[0].written is False
        # The ENAMETOOLONG warning should be logged
        assert "too long" in caplog.text.lower()

    def test_bypass_phase_enospc_logs_warning(
        self, tmp_path: Path, monkeypatch, caplog,
    ) -> None:
        """When _atomic_write_bytes raises ENOSPC during sidecar write,
        a WARNING is logged (symmetric with the copy-phase handler)."""
        import logging
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target")

        original_atomic = _rs._atomic_write_bytes

        def _failing_atomic(dest_path: Path, content: bytes, **kwargs) -> None:
            if "collides.log.from-feature" in str(dest_path):
                raise OSError(28, "No space left on device")
            return original_atomic(dest_path, content)

        monkeypatch.setattr(_rs, "_atomic_write_bytes", _failing_atomic)

        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            call = _make_sync_call(source, target, strict=False)
            report = call("feature")

        assert "history/collides.log" in report.skipped_files
        assert len(report.collisions) == 1
        # WARNING should be logged for ENOSPC in bypass phase
        assert "source data is not represented on disk" in caplog.text


class TestSafeBranchLabel:
    """_safe_branch_label produces filesystem-safe labels."""

    def test_truncation_logs_debug(self, caplog) -> None:
        """When a branch name exceeds 64 chars after safe transformation,
        logger.debug records the truncation."""
        import logging
        import se3.engine.merge.runtime_sync as _rs

        long_branch = "feature/" + "a" * 80
        with caplog.at_level(logging.DEBUG, logger="se3.engine.merge.runtime_sync"):
            result = _rs._safe_branch_label(long_branch)

        assert len(result) == 64
        assert result.endswith("_")
        assert "truncated" in caplog.text
        assert long_branch in caplog.text

    def test_truncated_label_idempotent_sidecar_emits_warning(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Full sync_branch_runtime path: when a >64-char branch name produces
        a truncated label AND a pre-existing sidecar matches the source content,
        the WARNING-level log surfaces the entropy-loss caveat through normal
        control flow (not just the isolated _safe_branch_label test)."""
        import logging
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Source has a file that will collide with the target
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source content")

        # Target has a different file at the same relative path (collision)
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        # Long branch name that triggers truncation
        long_branch = "feature/" + "a" * 80
        truncated_label = _rs._safe_branch_label(long_branch)
        assert len(truncated_label) == 64
        assert truncated_label.endswith("_")

        # Pre-create a sidecar at the truncated label path with content
        # matching the source — simulates a stale sidecar from a prior run.
        sidecar_path = (
            target_se3 / "history" / f"collides.log.from-{truncated_label}"
        )
        sidecar_path.write_text("source content")

        call = _make_sync_call(source, target, strict=False)
        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            report = call(long_branch)

        # The idempotent sidecar match should be recorded as an idempotent
        # bypass (not added to collisions) so re-runs do not generate noise.
        assert report.idempotent_bypasses == 1
        assert len(report.collisions) == 0
        # The audit detail list still records the per-file bypass for operators
        # investigating the warning.
        assert len(report.idempotent_bypass_records) == 1
        record = report.idempotent_bypass_records[0]
        assert record.branch == long_branch
        assert record.original_rel_path == "history/collides.log"

        # The WARNING-level entropy-loss message must fire end-to-end.
        warning_messages = [
            rec.message for rec in caplog.records
            if rec.levelno == logging.WARNING
        ]
        warning_text = " ".join(warning_messages)
        assert "idempotent sidecar match" in warning_text
        assert "truncated label" in warning_text
        # The new wording identifies the audit record as "current call"
        # rather than "authoritative" — ensure the corrected text fires.
        assert "current call" in warning_text

    def test_truncated_label_write_time_emits_info_log(
        self, tmp_path: Path, caplog,
    ) -> None:
        """When a long branch name (>64 chars) produces a truncated label and
        a sidecar is written for the first time (not idempotent), the INFO-level
        log at _write_sidecar time surfaces the entropy-loss warning so
        operators see it in normal-operation logs, not just on idempotent
        retries or debug logs."""
        import logging
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Source has a file that will collide with the target
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source content")

        # Target has a different file at the same relative path (collision)
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        # Long branch name that triggers truncation
        long_branch = "feature/" + "a" * 80
        truncated_label = _rs._safe_branch_label(long_branch)
        assert len(truncated_label) == 64
        assert truncated_label.endswith("_")

        # NO pre-existing sidecar — the write path (not idempotent) must fire.
        call = _make_sync_call(source, target, strict=False)
        with caplog.at_level(logging.INFO, logger="se3.engine.merge.runtime_sync"):
            report = call(long_branch)

        # A sidecar was written
        assert len(report.collisions) == 1
        assert report.collisions[0].written is True
        assert report.collisions[0].branch == long_branch
        sidecar_path = target_se3 / "history" / f"collides.log.from-{truncated_label}"
        assert sidecar_path.exists()
        assert sidecar_path.read_text() == "source content"

        # The INFO-level truncation-at-write-time message must fire.
        info_messages = [
            rec.message for rec in caplog.records
            if rec.levelno == logging.INFO
        ]
        info_text = " ".join(info_messages)
        assert "sidecar label truncated at write time" in info_text
        assert truncated_label in info_text
        assert "future writes from a different long branch" in info_text

    def test_safe_branch_label_with_truncation_short_branch(self) -> None:
        """Short branch names are preserved unchanged and truncated is False."""
        import se3.engine.merge.runtime_sync as _rs

        label, truncated = _rs._safe_branch_label_with_truncation("feature/foo")
        assert label == "feature__foo"
        assert truncated is False

    def test_safe_branch_label_with_truncation_long_branch(self) -> None:
        """Long branch names (>64 chars after sanitization) are truncated and
        truncated flag is True."""
        import se3.engine.merge.runtime_sync as _rs

        long_branch = "feature/" + "a" * 80
        label, truncated = _rs._safe_branch_label_with_truncation(long_branch)
        assert len(label) == 64
        assert label.endswith("_")
        assert truncated is True

    def test_safe_branch_label_with_truncation_exactly_64_chars(self) -> None:
        """A branch name that is exactly 64 characters after sanitization is
        NOT truncated — the boundary is strictly >64."""
        import se3.engine.merge.runtime_sync as _rs

        # 64 alphanumeric chars — no replacement needed
        exact = "a" * 64
        label, truncated = _rs._safe_branch_label_with_truncation(exact)
        assert label == exact
        assert len(label) == 64
        assert truncated is False

    def test_safe_branch_label_with_truncation_empty_branch(self) -> None:
        """Empty branch returns 'unnamed' with truncated=False."""
        import se3.engine.merge.runtime_sync as _rs

        label, truncated = _rs._safe_branch_label_with_truncation("")
        assert label == "unnamed"
        assert truncated is False


class TestWriteSidecarGuards:
    """Invariant guards on the internal ``_write_sidecar`` helper."""

    def test_write_sidecar_empty_branch_raises_value_error(
        self, tmp_path: Path,
    ) -> None:
        """``_write_sidecar(branch="")`` raises ValueError before touching disk.

        Defense-in-depth: the public entry point ``sync_branch_runtime``
        already rejects empty branch names, so this guard only matters if a
        future refactor calls ``_write_sidecar`` directly (e.g. for an
        unbound worktree).  Without the guard, an empty branch would
        collapse onto the shared sidecar suffix ``.from-unnamed`` for every
        caller, silently merging audit identity across branches.
        """
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"
        (source_se3 / "history").mkdir(parents=True)
        (target_se3 / "history").mkdir(parents=True)

        src_file = source_se3 / "history" / "x.log"
        src_file.write_text("source")
        dest_file = target_se3 / "history" / "x.log"
        dest_file.write_text("target")

        with pytest.raises(ValueError, match="branch must not be empty"):
            _rs._write_sidecar(
                src_file=src_file,
                dest_file=dest_file,
                rel_str="history/x.log",
                source_se3=source_se3,
                target_se3=target_se3,
                branch="",
                src_hash="0" * 64,
            )
        # No sidecar was written.
        assert not any(target_se3.glob("history/x.log.from-*"))


class TestTOCTOUDirectoryDest:
    """TOCTOU: dest_file becomes a directory between validation and copy."""

    def test_toctou_dest_becomes_directory_lenient_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If dest_file becomes a directory between validation and copy,
        lenient mode skips the file and records an audit-only collision row
        (mirrors the pre-validation path's audit-trail uniformity)."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source content")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            # Create dest_file as a DIRECTORY after read
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            if dest_file.exists() and not dest_file.is_dir():
                dest_file.unlink()
            dest_file.mkdir(parents=True, exist_ok=True)
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # Should be skipped, not bypassed to sidecar
        assert "history/flow1.log" in report.skipped_files
        assert "history/flow1.log" not in report.copied
        # Audit-trail uniformity: TOCTOU directory swap is recorded as an
        # audit-only collision row (written=False), matching the pre-
        # validation path's behavior.
        assert len(report.collisions) == 1
        collision = report.collisions[0]
        assert collision.original_rel_path == "history/flow1.log"
        assert collision.branch == "feature"
        assert collision.written is False
        assert collision.dest_hash == "unavailable"
        assert collision.sidecar_rel_path == "history/flow1.log.from-feature"
        # The directory at dest should remain
        assert (target_se3 / "history" / "flow1.log").is_dir()

    def test_toctou_dest_becomes_directory_strict_raises(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If dest_file becomes a directory between validation and copy,
        strict mode raises RuntimeSyncCollision."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("source content")

        original_read = _rs._safe_read_and_stat

        def _toctou_read(src_file: Path, source_se3: Path):
            content, st = original_read(src_file, source_se3)
            dest_file = target_se3 / "history" / "flow1.log"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            if dest_file.exists() and not dest_file.is_dir():
                dest_file.unlink()
            dest_file.mkdir(parents=True, exist_ok=True)
            return content, st

        monkeypatch.setattr(_rs, "_safe_read_and_stat", _toctou_read)

        call = _make_sync_call(source, target, strict=True)
        with pytest.raises(RuntimeSyncCollision) as exc_info:
            call("feature")
        assert exc_info.value.rel_path == "history/flow1.log"


class TestSafeReadAndStatParentSymlinkBoundary:
    """Document the defense-in-depth gap in ``_safe_read_and_stat``'s
    symlink fallback boundary check.

    The fallback validates resolved paths via ``os.path.normpath``, which
    is purely lexical: it collapses ``..`` and ``.`` segments but does NOT
    resolve symlinked intermediate parent components.  A symlinked parent
    pointing outside ``source_se3`` therefore evades the lexical check;
    the downstream ``os.open(O_NOFOLLOW)`` only refuses to follow the
    final path component, so a parent-symlink swap is followed at open
    time.  These tests pin the current behavior so a future stricter
    check (fd-based traversal via ``openat``) is detectable, AND verify
    that the higher-level ``_collect_files_under`` defense catches the
    case before ``_safe_read_and_stat`` is reached for normal flows.
    """

    def test_collect_files_filters_symlink_to_parent_outside(
        self, tmp_path: Path,
    ) -> None:
        """Higher-level defense: ``_collect_files_under`` fully resolves
        each candidate symlink and rejects any whose resolved target
        lands outside ``source_se3``.  This is the primary defense; the
        ``_safe_read_and_stat`` lexical check is a secondary
        defense-in-depth signal that the source is not normally reached
        through the public sync_branch_runtime entry point.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"
        external_dir = tmp_path / "external"

        (source_se3 / "history").mkdir(parents=True)
        external_dir.mkdir()
        (external_dir / "leak.txt").write_text("LEAKED")

        # parent_sym is a symlink to an external directory outside source_se3
        parent_sym = source_se3 / "history" / "parent_sym"
        os.symlink(str(external_dir), str(parent_sym))

        # sym is a symlink whose target ("parent_sym/leak.txt") lexically
        # resolves to /source/se3/history/parent_sym/leak.txt — a path that
        # passes a string-only normpath check yet actually escapes
        # source_se3 because parent_sym is itself a symlink.
        sym = source_se3 / "history" / "sym.log"
        os.symlink("parent_sym/leak.txt", str(sym))

        call = _make_sync_call(source, target)
        report = call("feature")

        # The end-to-end flow does NOT leak the external content because
        # _collect_files_under fully resolves the symlink chain and
        # rejects the entry. sym.log is filtered out at collection time.
        assert "history/sym.log" not in report.copied
        # If a regression bypassed the boundary check at collection,
        # leaked content would land at target/se3/history/sym.log — assert
        # absence so the regression is caught at a separate layer than
        # the report-list inspection.
        assert not (target_se3 / "history" / "sym.log").exists()

    def test_collect_files_filters_symlink_to_parent_inside_target(
        self, tmp_path: Path,
    ) -> None:
        """Self-check Fix #6 fixture: a malicious source worktree with a
        parent-component symlink pointing INTO the target_se3 tree must
        not produce a write under target via the read-then-copy flow.

        ``_is_outside_source_symlink`` only catches the case where a
        symlink's resolved target leaves source_se3.  A symlink whose
        intermediate parent points into target_se3 (so reading the
        resolved file would actually be reading from target itself)
        could either confuse the copy semantics or, after a careless
        change, ricochet target content back into the runtime sync
        report.  This test pins the documented behavior: such entries
        are rejected at collection time and never produce a target-side
        write.
        """
        import os

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "preexisting.log").write_text(
            "TARGET_PREEXISTING"
        )

        # parent_sym in source's history points INTO target_se3/history
        parent_sym = source_se3 / "history" / "into_target"
        os.symlink(str(target_se3 / "history"), str(parent_sym))

        # sym whose lexical target lands inside source_se3 but whose
        # parent (into_target) is actually target_se3/history.
        sym = source_se3 / "history" / "sneaky.log"
        os.symlink("into_target/preexisting.log", str(sym))

        call = _make_sync_call(source, target)
        report = call("feature")

        # The malicious entry must NOT appear in the copied set; the
        # target's original file MUST remain untouched.
        assert "history/sneaky.log" not in report.copied
        # The target's existing file is preserved verbatim.
        assert (
            (target_se3 / "history" / "preexisting.log").read_text()
            == "TARGET_PREEXISTING"
        )
        # No spurious sneaky.log at target.
        assert not (target_se3 / "history" / "sneaky.log").exists()

    def test_safe_read_and_stat_parent_symlink_blocked_by_realpath(
        self, tmp_path: Path,
    ) -> None:
        """With ``os.path.realpath`` (replaced from ``os.path.normpath``),
        a symlink whose target lexically lands inside ``source_se3`` but
        whose actual parent is a symlink pointing outside is now caught
        by the boundary check and rejected with OSError.

        Previously ``os.path.normpath`` was purely lexical and did not
        resolve symlinks in parent components, allowing external content
        to leak through.  The switch to ``realpath`` closes this gap.
        """
        import os

        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        source_se3 = source / "se3"
        external_dir = tmp_path / "external"

        (source_se3 / "history").mkdir(parents=True)
        external_dir.mkdir()
        (external_dir / "leak.txt").write_text("LEAKED")

        # parent_sym → external_dir (outside source_se3)
        parent_sym = source_se3 / "history" / "parent_sym"
        os.symlink(str(external_dir), str(parent_sym))

        # sym → "parent_sym/leak.txt" (relative target that LEXICALLY
        # lands inside source_se3 because the string starts with the
        # source_se3 prefix)
        sym = source_se3 / "history" / "sym.log"
        os.symlink("parent_sym/leak.txt", str(sym))

        # Realpath resolves the parent symlink, so the resolved path is
        # outside source_se3 and the boundary check raises OSError.
        with pytest.raises(OSError):
            _rs._safe_read_and_stat(sym, source_se3)


class TestAtomicWriteBytesDestinationSymlinkSwap:
    """Verify ``_atomic_write_bytes`` refuses to overwrite a symlinked
    destination (Task 30 / E1+E5).

    Although ``os.rename(2)`` does not follow destination symlinks (it
    atomically replaces the path entry, leaving the link's target file
    untouched), a symlink at the destination is itself suspicious — it
    implies someone planted a path-takeover gadget between collision
    validation and the atomic write.  ``_atomic_write_bytes`` now
    short-circuits with ``OSError(errno=ELOOP)`` so the attempt surfaces
    loudly rather than silently consuming a tier-A or sidecar slot.
    """

    def test_atomic_write_bytes_rejects_symlink_destination(
        self, tmp_path: Path,
    ) -> None:
        """When dest_path is a symlink at write time, ``_atomic_write_bytes``
        raises ``OSError(ELOOP)`` and leaves the external target untouched.
        """
        import errno
        import os

        import se3.engine.merge.runtime_sync as _rs

        target = tmp_path / "target"
        target_se3 = target / "se3" / "history"
        target_se3.mkdir(parents=True)

        # External file outside the destination tree; the symlink points
        # here.  The previous behavior would have happily replaced the
        # symlink and gone on; the O_NOFOLLOW guard now refuses.
        external_target = tmp_path / "external_target.txt"
        external_target.write_text("ORIGINAL EXTERNAL")

        # dest_path is a symlink pointing to the external file.
        dest_path = target_se3 / "victim.log"
        os.symlink(str(external_target), str(dest_path))

        # Sanity: dest_path is a symlink and reads through to the external.
        assert dest_path.is_symlink()
        assert dest_path.read_text() == "ORIGINAL EXTERNAL"

        with pytest.raises(OSError) as exc_info:
            _rs._atomic_write_bytes(dest_path, b"NEW CONTENT")
        assert exc_info.value.errno == errno.ELOOP

        # The symlink remains intact — the external file is untouched
        # and dest_path still resolves through it.
        assert dest_path.is_symlink()
        assert external_target.read_text() == "ORIGINAL EXTERNAL"


class TestSafeBranchLabelTruncationPaths:
    """Coverage for the truncation path in
    ``_safe_branch_label_with_truncation`` end-to-end via
    ``sync_branch_runtime``.

    The truncated-label paths exercised here:
    - hash-suffix idempotent match warning (lines 669-678)
    - long-hash idempotent match warning (lines 700-709)
    - NAME_MAX preflight when a truncated label still produces a too-long
      sidecar filename due to a long source filename
    """

    def test_truncated_label_hash_suffix_idempotent_emits_warning(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Truncated branch label + hash-suffix sidecar idempotent match
        fires the lines 669-678 warning.  Setup: pre-create a primary
        sidecar with DIFFERENT content (forcing disambiguation) and a
        hash-suffix sidecar with SOURCE-MATCHING content (idempotent).
        """
        import logging

        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        long_branch = "feature/" + "b" * 80
        truncated_label = _rs._safe_branch_label(long_branch)
        assert len(truncated_label) == 64

        # Primary sidecar already exists with DIFFERENT content (force
        # disambiguation to the hash-suffix slot).
        primary_sidecar = (
            target_se3 / "history" / f"collides.log.from-{truncated_label}"
        )
        primary_sidecar.write_text("ANOTHER long-branch's content")

        # Hash-suffix sidecar already exists with SOURCE-MATCHING content
        # (forces the idempotent hash-suffix branch to fire).
        import hashlib
        src_hash = hashlib.sha256(b"source content").hexdigest()
        short_hash = src_hash[:8]
        hash_sidecar = (
            target_se3 / "history"
            / f"collides.log.from-{truncated_label}.{short_hash}"
        )
        hash_sidecar.write_text("source content")

        call = _make_sync_call(source, target, strict=False)
        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            report = call(long_branch)

        # Idempotent hash-suffix match recorded (no new collision row,
        # idempotent counter incremented).
        assert report.idempotent_bypasses == 1
        assert len(report.collisions) == 0
        assert len(report.idempotent_bypass_records) == 1
        record = report.idempotent_bypass_records[0]
        assert record.branch == long_branch
        # Sidecar path should reference the hash-suffix slot.
        assert short_hash in record.sidecar_rel_path

        # The hash-suffix-specific warning must fire.
        warning_text = " ".join(
            r.message for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "hash-suffix sidecar match" in warning_text
        assert "truncated label" in warning_text
        assert "current call" in warning_text

    def test_truncated_label_long_hash_idempotent_emits_warning(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Truncated branch label + long-hash (16-char) sidecar idempotent
        match fires the lines 700-709 warning.  Setup: pre-create primary
        + 8-char-hash sidecars with DIFFERENT content (forcing both rounds
        of disambiguation) and the 16-char-hash sidecar with SOURCE-
        MATCHING content (idempotent at the long-hash slot).
        """
        import hashlib
        import logging

        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "collides.log").write_text("source content")

        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "collides.log").write_text("target content")

        long_branch = "feature/" + "c" * 80
        truncated_label = _rs._safe_branch_label(long_branch)

        # Primary sidecar — different content
        primary = target_se3 / "history" / f"collides.log.from-{truncated_label}"
        primary.write_text("alpha different content")

        # 8-char-hash sidecar — different content
        src_hash = hashlib.sha256(b"source content").hexdigest()
        short_hash = src_hash[:8]
        long_hash = src_hash[:16]
        short_sidecar = (
            target_se3 / "history"
            / f"collides.log.from-{truncated_label}.{short_hash}"
        )
        short_sidecar.write_text("beta different content")

        # 16-char-hash sidecar — matches source (idempotent)
        long_sidecar = (
            target_se3 / "history"
            / f"collides.log.from-{truncated_label}.{long_hash}"
        )
        long_sidecar.write_text("source content")

        call = _make_sync_call(source, target, strict=False)
        with caplog.at_level(logging.WARNING, logger="se3.engine.merge.runtime_sync"):
            report = call(long_branch)

        # Idempotent long-hash match recorded
        assert report.idempotent_bypasses == 1
        assert len(report.collisions) == 0
        assert len(report.idempotent_bypass_records) == 1
        record = report.idempotent_bypass_records[0]
        assert record.branch == long_branch
        assert long_hash in record.sidecar_rel_path

        # The long-hash-specific warning must fire.
        warning_text = " ".join(
            r.message for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "long-hash sidecar match" in warning_text
        assert "truncated label" in warning_text

    def test_truncated_label_name_max_preflight_records_audit(
        self, tmp_path: Path,
    ) -> None:
        """When the source filename is long enough that the truncated-
        label sidecar filename still exceeds NAME_MAX (255 bytes), the
        preflight at the sidecar write point raises ENAMETOOLONG-flavored
        RuntimeSyncCollision and the lenient bypass loop records an
        audit-only row.
        """
        import errno

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Use a long source filename: 200 chars of base name. Combined
        # with ".from-<truncated 64-char label>" the total exceeds the
        # 255-byte NAME_MAX preflight check.
        long_basename = "x" * 200 + ".log"
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / long_basename).write_text("source content")

        # Force a collision so the bypass path is taken.
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / long_basename).write_text("target content")

        # Branch name long enough to trigger label truncation.
        long_branch = "feature/" + "d" * 80

        call = _make_sync_call(source, target, strict=False)
        report = call(long_branch)

        # The file is skipped in lenient mode (sidecar cannot be written).
        rel_path = f"history/{long_basename}"
        assert rel_path in report.skipped_files
        # An audit-only collision row is recorded for traceability.
        # (When the dest_file is unreadable we expect dest_hash to be
        # the DEST_HASH_UNAVAILABLE sentinel; here the dest is a normal
        # file so the dest_hash is a real hash. The key invariant is the
        # row exists with written=False.)
        audit_rows = [
            c for c in report.collisions
            if c.original_rel_path == rel_path and not c.written
        ]
        assert len(audit_rows) == 1
        # The recorded sidecar path uses the truncated label even though
        # the actual file was never written (preflight rejected it).
        assert ".from-" in audit_rows[0].sidecar_rel_path


# =====================================================================
# Lenient-mode exception propagation: already-synced files preserved
# =====================================================================


class TestLenientModePreservesSyncedFilesOnUnexpectedException:
    """When an unexpected exception escapes the per-file handlers in lenient
    mode, already-synced files MUST be preserved (not rolled back)."""

    def test_lenient_preserves_already_copied_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """RuntimeSyncCollision escaping the copy loop preserves prior copies."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Two tier A files in source
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("log1")
        (source_se3 / "history" / "flow2.log").write_text("log2")

        call = _make_sync_call(source, target, strict=False)

        original_atomic_write = _rs._atomic_write_bytes
        call_count = 0

        def fake_atomic_write(dest: Path, content: bytes) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call succeeds
                return original_atomic_write(dest, content)
            # Second call raises RuntimeSyncCollision, which escapes the
            # inner OSError-only handler and hits the outer handler.
            raise _rs.RuntimeSyncCollision(
                rel_path=str(dest.name),
                reason="injected_test_exception",
                sidecar_path=str(dest),
            )

        monkeypatch.setattr(_rs, "_atomic_write_bytes", fake_atomic_write)

        with pytest.raises(_rs.RuntimeSyncCollision):
            call("feature")

        # First file must be preserved despite the second file failing
        assert (target_se3 / "history" / "flow1.log").exists()
        assert (
            target_se3 / "history" / "flow1.log"
        ).read_text() == "log1"
        # Second file should not exist (write was aborted)
        assert not (target_se3 / "history" / "flow2.log").exists()

    def test_strict_rolls_back_on_exception(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """In strict mode, the same exception triggers rollback of all copies."""
        import se3.engine.merge.runtime_sync as _rs

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("log1")
        (source_se3 / "history" / "flow2.log").write_text("log2")

        call = _make_sync_call(source, target, strict=True)

        original_atomic_write = _rs._atomic_write_bytes
        call_count = 0

        def fake_atomic_write(dest: Path, content: bytes) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return original_atomic_write(dest, content)
            raise _rs.RuntimeSyncCollision(
                rel_path=str(dest.name),
                reason="injected_test_exception",
                sidecar_path=str(dest),
            )

        monkeypatch.setattr(_rs, "_atomic_write_bytes", fake_atomic_write)

        with pytest.raises(_rs.RuntimeSyncCollision):
            call("feature")

        # In strict mode, the first file is rolled back
        assert not (target_se3 / "history" / "flow1.log").exists()
        assert not (target_se3 / "history" / "flow2.log").exists()


# =====================================================================
# Idempotent bypass records separation correctness
# =====================================================================


class TestIdempotentBypassRecordsSeparation:
    """Idempotent sidecar matches MUST go to idempotent_bypass_records
    and MUST NOT appear in collisions.  A bug that conflates the two
    lists would produce spurious warnings on every re-run."""

    def test_idempotent_match_never_appears_in_collisions(
        self, tmp_path: Path
    ) -> None:
        """Exact sidecar content match is recorded only in idempotent lists."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "flow1.log").write_text("same content")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "flow1.log").write_text("target content")
        # Pre-existing sidecar with IDENTICAL content to source
        (target_se3 / "history" / "flow1.log.from-feature").write_text(
            "same content"
        )

        call = _make_sync_call(source, target, strict=False)
        report = call("feature")

        # The critical invariant: idempotent matches do NOT pollute collisions
        assert len(report.collisions) == 0, (
            "idempotent match was incorrectly recorded in collisions — "
            "this would cause spurious warnings on every re-run"
        )
        assert report.idempotent_bypasses == 1
        assert len(report.idempotent_bypass_records) == 1
        record = report.idempotent_bypass_records[0]
        assert record.original_rel_path == "history/flow1.log"
        assert record.sidecar_rel_path == "history/flow1.log.from-feature"
