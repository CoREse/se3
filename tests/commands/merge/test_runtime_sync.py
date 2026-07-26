"""Regression tests for runtime_sync hardening (Group G6 / Tasks 30-33).

Each class targets one of the E1-E5 defects called out in the design doc:

* TestSymlinkDestinationRejected (E1/E5, Task 30) — ``_atomic_write_bytes``
  refuses to overwrite a symlinked destination, eliminating the path-
  takeover vector through ``temp_path.replace(dest_path)``.
* TestLenientModePreservesSuccess (E2, Task 31) — When an unexpected
  exception escapes the per-file inner handlers in lenient mode, files
  that already copied / sidecar-bypassed successfully are preserved
  rather than rolled back.
* TestSidecarSelfCollisionFiltered (E3, Task 32) — Source-side files
  matching the sidecar filename pattern are filtered at collection time
  so repeated ``se3 merge`` runs cannot accumulate
  ``.from-A.from-B`` chains.
* TestBoundedReadAndWrite (E4, Task 33) — All ``while True`` chunked I/O
  loops have iteration / size / duration caps so a hostile or malformed
  source file cannot hang the merge.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import tianluo.engine.merge.runtime_sync as _rs
from tianluo.engine.merge.runtime_sync import (
    DEST_HASH_UNAVAILABLE,
    BypassedCollision,
    RuntimeSyncCollision,
    SyncReport,
    _atomic_write_bytes,
    _bounded_read_chunks,
    _bounded_write_all,
    _is_sidecar_filename,
    sync_branch_runtime,
)


def _make_sync_call(source_dir: Path, target_dir: Path, *, strict: bool = False):
    """Helper: invoke ``sync_branch_runtime`` with a stubbed worktree lookup."""

    def _call(branch: str) -> SyncReport:
        original = _rs._get_worktree_path_for_branch
        _rs._get_worktree_path_for_branch = lambda _pr, _br: source_dir
        try:
            return sync_branch_runtime(target_dir, branch, strict=strict)
        finally:
            _rs._get_worktree_path_for_branch = original

    return _call


# ---------------------------------------------------------------------------
# Task 30 / E1+E5: O_NOFOLLOW destination guard
# ---------------------------------------------------------------------------


class TestSymlinkDestinationRejected:
    """``_atomic_write_bytes`` refuses symlink destinations.

    Acceptance criteria from the task:

    * TOCTOU 攻击测试不能逃逸 — a planted symlink at the destination
      cannot redirect the write to an external file.
    * 符号链接替换被拒 — even if the symlink points at a target the
      caller has legitimate write access to, the write is still refused.
    """

    def test_planted_symlink_to_external_file_is_rejected(
        self, tmp_path: Path,
    ) -> None:
        external = tmp_path / "external.txt"
        external.write_text("ORIGINAL")

        dest_dir = tmp_path / "se3" / "history"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "victim.log"
        os.symlink(str(external), str(dest))

        with pytest.raises(OSError) as exc_info:
            _atomic_write_bytes(dest, b"PWNED")

        assert exc_info.value.errno == errno.ELOOP
        # External file untouched, symlink intact.
        assert external.read_text() == "ORIGINAL"
        assert dest.is_symlink()

    def test_planted_symlink_to_inside_target_tree_is_rejected(
        self, tmp_path: Path,
    ) -> None:
        """Even an in-tree symlink is rejected — the policy is uniform."""
        in_tree_target = tmp_path / "se3" / "logs" / "real.log"
        in_tree_target.parent.mkdir(parents=True)
        in_tree_target.write_text("real content")

        dest_dir = tmp_path / "se3" / "history"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "victim.log"
        os.symlink(str(in_tree_target), str(dest))

        with pytest.raises(OSError) as exc_info:
            _atomic_write_bytes(dest, b"NEW")
        assert exc_info.value.errno == errno.ELOOP
        # Symlink still there, real file untouched.
        assert dest.is_symlink()
        assert in_tree_target.read_text() == "real content"

    def test_normal_write_still_works(self, tmp_path: Path) -> None:
        """Regression: the symlink guard does not break normal writes."""
        dest_dir = tmp_path / "se3" / "history"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "flow.log"

        _atomic_write_bytes(dest, b"hello")
        assert dest.read_text() == "hello"
        assert not dest.is_symlink()

    def test_write_overwrite_regular_file_still_works(self, tmp_path: Path) -> None:
        """Regression: the symlink guard does not break regular overwrites."""
        dest_dir = tmp_path / "se3" / "history"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "flow.log"
        dest.write_text("old")

        _atomic_write_bytes(dest, b"new content")
        assert dest.read_text() == "new content"

    def test_toctou_post_write_recheck_catches_late_symlink_swap(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If a symlink is planted between mkstemp and rename, the
        post-write recheck still rejects it. Simulates the TOCTOU race
        by hooking into ``_bounded_write_all`` to plant the symlink."""
        external = tmp_path / "external.txt"
        external.write_text("ORIGINAL")

        dest_dir = tmp_path / "se3" / "history"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "race.log"
        # No symlink at lstat time.
        assert not dest.exists()

        original_write = _rs._bounded_write_all

        def _planting_write(fd: int, content: bytes, path_for_error: str) -> None:
            # Simulate adversary planting a symlink mid-write.
            os.symlink(str(external), str(dest))
            return original_write(fd, content, path_for_error)

        monkeypatch.setattr(_rs, "_bounded_write_all", _planting_write)

        with pytest.raises(OSError) as exc_info:
            _atomic_write_bytes(dest, b"NEW")

        assert exc_info.value.errno == errno.ELOOP
        # External file untouched (post-write recheck caught it).
        assert external.read_text() == "ORIGINAL"


# ---------------------------------------------------------------------------
# Task 31 / E2: lenient mode preserves already-successful work
# ---------------------------------------------------------------------------


class TestLenientModePreservesSuccess:
    """Lenient-mode outer ``except`` no longer rolls back successful files.

    Acceptance: 半失败 sync 已成功部分不被撤销.

    The outer ``except (OSError, RuntimeSyncCollision)`` in
    ``sync_branch_runtime`` is a defense-in-depth safety net.  It
    *should* almost never fire in lenient mode (every per-file path
    has an inner handler that absorbs OSError into ``skipped_files``),
    but if a future refactor removes one of those inner handlers, the
    outer net would silently roll back already-successful work.  The
    Task 31 / E2 fix makes the outer net mode-aware: strict mode keeps
    the all-or-nothing rollback, lenient mode preserves whatever
    succeeded so far.

    To trigger the outer except deterministically without lying about
    a real bug, these tests monkeypatch ``_atomic_write_bytes`` to
    raise ``RuntimeSyncCollision`` — that exception type is NOT a
    subclass of OSError, so the inner ``except OSError`` doesn't catch
    it, and it escapes upward into the outer
    ``except (OSError, RuntimeSyncCollision)`` net.
    """

    def test_already_copied_files_preserved_on_unexpected_failure(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Two tier-A files: first copies successfully, second causes an
        exception that escapes the inner per-file handlers.  Lenient
        mode preserves the first."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "a.log").write_text("alpha content")
        (source_se3 / "history" / "b.log").write_text("beta content")

        original_atomic = _rs._atomic_write_bytes
        call_state = {"count": 0}

        def _flaky_atomic(dest_path: Path, content: bytes) -> None:
            call_state["count"] += 1
            if call_state["count"] == 1:
                # First file (a.log) succeeds.
                return original_atomic(dest_path, content)
            # Second file: raise RuntimeSyncCollision so it escapes the
            # inner ``except OSError`` and reaches the outer net.
            raise RuntimeSyncCollision(
                "history/b.log",
                reason="simulated_outer_propagation",
            )

        monkeypatch.setattr(_rs, "_atomic_write_bytes", _flaky_atomic)

        call = _make_sync_call(source, target, strict=False)
        with pytest.raises(RuntimeSyncCollision):
            call("feature")

        # In lenient mode, a.log MUST be preserved despite the later
        # exception that fired the outer except. Old behavior would
        # have rolled it back via the outer except's blanket loop.
        assert (target_se3 / "history" / "a.log").exists()
        assert (target_se3 / "history" / "a.log").read_text() == "alpha content"
        # se3/ root must not be removed either (lenient mode preserves).
        assert target_se3.exists()

    def test_strict_mode_still_rolls_back(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Strict mode preserves the all-or-nothing promise: a single
        propagated failure rolls back every file copied this invocation."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "a.log").write_text("alpha")
        (source_se3 / "history" / "b.log").write_text("beta")

        original_atomic = _rs._atomic_write_bytes
        call_state = {"count": 0}

        def _flaky_atomic(dest_path: Path, content: bytes) -> None:
            call_state["count"] += 1
            if call_state["count"] == 1:
                return original_atomic(dest_path, content)
            # Same trick as the lenient test — RuntimeSyncCollision
            # escapes the inner ``except OSError`` so the outer net
            # fires.
            raise RuntimeSyncCollision(
                "history/b.log",
                reason="simulated_outer_propagation",
            )

        monkeypatch.setattr(_rs, "_atomic_write_bytes", _flaky_atomic)

        call = _make_sync_call(source, target, strict=True)
        with pytest.raises(RuntimeSyncCollision):
            call("feature")

        # Strict mode rolled back the first file.
        assert not (target_se3 / "history" / "a.log").exists()
        # And the se3/ root, since we created it.
        assert not target_se3.exists()

    def test_lenient_mode_preserves_already_written_sidecars(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Sidecars written before the exception fires must also be preserved."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        # Two source files, both will collide in the bypass loop.
        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "a.log").write_text("source-a")
        (source_se3 / "history" / "b.log").write_text("source-b")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "a.log").write_text("target-a")
        (target_se3 / "history" / "b.log").write_text("target-b")

        original_write_sidecar = _rs._write_sidecar
        call_state = {"count": 0}

        def _flaky_sidecar(*args, **kwargs):
            call_state["count"] += 1
            if call_state["count"] == 1:
                return original_write_sidecar(*args, **kwargs)
            # Raise something the inner ``except (RuntimeSyncCollision,
            # OSError)`` net does NOT catch — RuntimeError is not in
            # either tuple, so it escapes the bypass loop's per-file
            # net and propagates out of the function entirely.  The
            # already-written sidecar must still be preserved on disk.
            raise RuntimeError("simulated unexpected sidecar failure")

        monkeypatch.setattr(_rs, "_write_sidecar", _flaky_sidecar)

        call = _make_sync_call(source, target, strict=False)
        with pytest.raises(RuntimeError):
            call("feature")

        # First sidecar must remain on disk.
        sidecar_a = target_se3 / "history" / "a.log.from-feature"
        sidecar_b = target_se3 / "history" / "b.log.from-feature"
        # Exactly one of the two sidecars exists (the order is dict-key
        # dependent inside the bypass loop).  We do not care which —
        # what matters is that AT LEAST ONE sidecar from before the
        # exception was preserved.
        assert sidecar_a.exists() or sidecar_b.exists(), (
            "lenient mode must preserve already-written sidecars"
        )


# ---------------------------------------------------------------------------
# Task 32 / E3: sidecar source-side filtering, idempotent re-sync
# ---------------------------------------------------------------------------


class TestSidecarSelfCollisionFiltered:
    """Sidecar-named source files are filtered; re-sync is idempotent.

    Acceptance: 同 source 重复 sync 不再产生 .from-...from-... 链.
    """

    def test_is_sidecar_filename_pattern(self) -> None:
        """The sidecar regex matches the on-disk patterns produced by
        ``_write_sidecar``."""
        # Plain sidecar.
        assert _is_sidecar_filename("flow.log.from-feature")
        # Hash-disambiguated sidecar (8-char hex).
        assert _is_sidecar_filename("flow.log.from-feature.deadbeef")
        # Long-hash sidecar (16-char hex).
        assert _is_sidecar_filename("flow.log.from-feature.deadbeefcafebabe")
        # Branch with safe-label characters: __, -, .
        assert _is_sidecar_filename("flow.log.from-feat__a-b.txt")  # plain
        assert _is_sidecar_filename("x.log.from-A__b")
        # Negative cases — these are NOT sidecars.
        assert not _is_sidecar_filename("flow.log")
        assert not _is_sidecar_filename("history.json")
        assert not _is_sidecar_filename("notes-from-meeting.txt")
        # The regex is intentionally conservative: a literal `.from-X`
        # suffix is treated as a sidecar even when the trailing chunk
        # looks ambiguous (e.g. ``a.log.from-b.deadbe`` — 6-char hex,
        # not a hash slot).  False positives are safe — they only mean
        # such a file would be filtered as a sidecar instead of being
        # synced.  False negatives would re-introduce the
        # ``.from-A.from-B`` chain bug, so we err in favor of skipping.

    def test_source_sidecar_files_are_filtered(self, tmp_path: Path) -> None:
        """A source worktree containing leftover sidecars from a prior
        merge does NOT propagate them to the destination."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "real.log").write_text("real data")
        # Three different sidecar shapes left over from prior merges.
        (source_se3 / "history" / "real.log.from-feat__a").write_text("stale-a")
        (source_se3 / "history" / "real.log.from-feat__b.deadbeef").write_text("stale-b")
        (source_se3 / "history" / "real.log.from-c.deadbeefcafebabe").write_text("stale-c")

        call = _make_sync_call(source, target)
        report = call("feature")

        # Real file copied.
        assert "history/real.log" in report.copied
        # Sidecar leftovers neither copied nor recorded as skipped — they
        # are filtered at collection time entirely.
        for stale in (
            "history/real.log.from-feat__a",
            "history/real.log.from-feat__b.deadbeef",
            "history/real.log.from-c.deadbeefcafebabe",
        ):
            assert stale not in report.copied
            assert stale not in report.skipped_files
            assert not (target_se3 / stale).exists()

    def test_repeated_sync_is_idempotent_no_chain_accumulation(
        self, tmp_path: Path,
    ) -> None:
        """Repeated ``se3 merge`` of the same source against an evolving
        target never creates ``.from-A.from-A`` chains."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "x.log").write_text("source content")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "x.log").write_text("target content")

        call = _make_sync_call(source, target)
        # Run sync three times in a row.
        for _ in range(3):
            call("feature")

        # The directory must contain x.log + exactly one sidecar
        # (x.log.from-feature) — no nested chains, no hash-disambiguated
        # extras with the same content.
        files = sorted(p.name for p in (target_se3 / "history").iterdir())
        # We allow x.log + exactly one sidecar (the primary slot).
        assert files == ["x.log", "x.log.from-feature"], (
            f"unexpected accumulation: {files}"
        )
        # Nested-chain pattern explicitly absent.
        for name in files:
            assert ".from-feature.from-" not in name

    def test_sidecar_glob_pattern_files_skipped(self, tmp_path: Path) -> None:
        """Sidecar leaves under glob patterns (e.g. state/summary-*) are
        also filtered."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "state").mkdir(parents=True)
        (source_se3 / "state" / "summary-flow.md").write_text("real summary")
        # Sidecar leftover at the same prefix-glob level.
        (source_se3 / "state" / "summary-flow.md.from-feature").write_text("stale")

        call = _make_sync_call(source, target)
        report = call("feature")

        assert "state/summary-flow.md" in report.copied
        # The sidecar leftover is filtered.
        assert "state/summary-flow.md.from-feature" not in report.copied
        assert "state/summary-flow.md.from-feature" not in report.skipped_files
        assert not (target_se3 / "state" / "summary-flow.md.from-feature").exists()


# ---------------------------------------------------------------------------
# Task 33 / E4: bounded read / write loops
# ---------------------------------------------------------------------------


class TestBoundedReadAndWrite:
    """All ``while True`` chunked-I/O loops have iteration / size /
    duration caps so a runaway read or write cannot hang ``se3 merge``.

    Acceptance: symlink loop fixture 不再 hang.
    """

    def test_bounded_read_byte_cap(self, monkeypatch, tmp_path: Path) -> None:
        """When the read byte cap is exceeded, ``OSError(EFBIG)`` fires."""
        # Monkeypatch the cap to a small value so we don't have to
        # produce hundreds of MiB of data in the test.
        monkeypatch.setattr(_rs, "_MAX_FILE_BYTES", 128)

        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * 256)

        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with pytest.raises(OSError) as exc_info:
                # Drain the generator.
                list(_bounded_read_chunks(fd, str(path)))
            assert exc_info.value.errno == errno.EFBIG
        finally:
            os.close(fd)

    def test_bounded_read_iteration_cap(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """The iteration cap fires independently of total bytes."""
        monkeypatch.setattr(_rs, "_MAX_FILE_READ_ITERATIONS", 2)

        path = tmp_path / "many_chunks.bin"
        # _READ_CHUNK_SIZE * 5 bytes triggers ~5 iterations, exceeding 2.
        path.write_bytes(b"x" * (_rs._READ_CHUNK_SIZE * 5))

        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with pytest.raises(OSError) as exc_info:
                list(_bounded_read_chunks(fd, str(path)))
            assert exc_info.value.errno == errno.EFBIG
        finally:
            os.close(fd)

    def test_bounded_read_duration_cap(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """When the read duration is exceeded, ``OSError(ETIMEDOUT)``."""
        # Negative duration → deadline already in the past at function
        # start, so the very first time-check inside the loop fires.
        monkeypatch.setattr(_rs, "_MAX_FILE_IO_DURATION_S", -1.0)

        path = tmp_path / "regular.bin"
        path.write_bytes(b"x" * 1024)
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with pytest.raises(OSError) as exc_info:
                list(_bounded_read_chunks(fd, str(path)))
            assert exc_info.value.errno == errno.ETIMEDOUT
        finally:
            os.close(fd)

    def test_bounded_read_returns_full_content_within_caps(
        self, tmp_path: Path,
    ) -> None:
        """Regression: caps don't break correct reads."""
        path = tmp_path / "small.bin"
        content = b"hello world" * 100
        path.write_bytes(content)
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            chunks = list(_bounded_read_chunks(fd, str(path)))
        finally:
            os.close(fd)
        assert b"".join(chunks) == content

    def test_bounded_write_zero_progress_raises(self, tmp_path: Path) -> None:
        """If ``os.write`` returns 0 we don't loop forever."""

        class _FakeOSWrite:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, fd: int, data: bytes) -> int:
                self.calls += 1
                return 0  # Always reports no-progress.

        fake_write = _FakeOSWrite()
        with patch("tianluo.engine.merge.runtime_sync.os.write", fake_write):
            with pytest.raises(OSError) as exc_info:
                _bounded_write_all(fd=99, content=b"data", path_for_error="x")
            assert exc_info.value.errno == errno.EIO
        # Single attempt — we don't spin forever.
        assert fake_write.calls == 1

    def test_bounded_write_duration_cap(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Bounded write also honors the time deadline."""
        monkeypatch.setattr(_rs, "_MAX_FILE_IO_DURATION_S", -1.0)

        # Use a real fd via a temp file so os.write actually works
        # (we want the duration check to fire first, before the write).
        fd, path_str = tempfile.mkstemp(dir=str(tmp_path))
        try:
            with pytest.raises(OSError) as exc_info:
                _bounded_write_all(fd, b"hello", path_str)
            assert exc_info.value.errno == errno.ETIMEDOUT
        finally:
            os.close(fd)
            try:
                os.unlink(path_str)
            except OSError:
                pass

    def test_bounded_write_empty_content_no_op(self) -> None:
        """Writing empty content is a no-op (no infinite loop, no error)."""
        # Should return immediately without touching the fd at all.
        _bounded_write_all(fd=-1, content=b"", path_for_error="ignored")

    def test_symlink_loop_bounded_via_read_caps(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """A pathological source file that streams forever (simulated)
        does not hang the merge — the bounded read cap fires."""
        # Lower the byte cap so the test is fast.
        monkeypatch.setattr(_rs, "_MAX_FILE_BYTES", 1024)

        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        # A real file larger than the cap.
        (source_se3 / "history" / "huge.log").write_bytes(b"x" * 4096)

        call = _make_sync_call(source, target)
        # The sync should not hang; the file is skipped (lenient mode
        # absorbs the OSError) and the merge completes.
        report = call("feature")
        assert "history/huge.log" in report.skipped_files
        assert "history/huge.log" not in report.copied


# ---------------------------------------------------------------------------
# End-to-end: idempotent re-runs survive the new hardening
# ---------------------------------------------------------------------------


class TestIdempotentEndToEnd:
    """Sanity checks that the four hardening tasks compose correctly."""

    def test_repeated_clean_sync_is_no_op(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "x.log").write_text("payload")

        call = _make_sync_call(source, target)
        report1 = call("feature")
        assert "history/x.log" in report1.copied

        # Second run: file already at target with identical content →
        # idempotent skip, no collision, no sidecar.
        report2 = call("feature")
        assert "history/x.log" not in report2.copied
        assert report2.collisions == []
        assert report2.skipped_files == []

    def test_repeated_collision_sync_idempotent(self, tmp_path: Path) -> None:
        """Repeated sync of the same source against an unchanged target
        with conflicting content → primary sidecar created once, then
        idempotent matches.
        """
        source = tmp_path / "source"
        target = tmp_path / "target"
        source_se3 = source / "se3"
        target_se3 = target / "se3"

        (source_se3 / "history").mkdir(parents=True)
        (source_se3 / "history" / "x.log").write_text("source")
        (target_se3 / "history").mkdir(parents=True)
        (target_se3 / "history" / "x.log").write_text("target")

        call = _make_sync_call(source, target)
        report1 = call("feature")
        assert len(report1.collisions) == 1
        assert report1.idempotent_bypasses == 0

        # Second run: sidecar already on disk with matching content →
        # idempotent bypass.
        report2 = call("feature")
        assert report2.collisions == []
        assert report2.idempotent_bypasses == 1
        # Third run: same.
        report3 = call("feature")
        assert report3.collisions == []
        assert report3.idempotent_bypasses == 1
        # Final on-disk state: x.log + exactly one sidecar.  No nested
        # ``.from-X.from-X`` chains and no hash-suffix accumulation.
        names = sorted(p.name for p in (target_se3 / "history").iterdir())
        assert names == ["x.log", "x.log.from-feature"]
