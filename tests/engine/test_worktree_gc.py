"""Unit tests for the leaked-worktree garbage collector (``worktree_gc``).

Covers the six branches called out in the design: a merged stale run (archived
+ branch deleted), an unmerged stale run (archived + branch ref retained), a
not-over-age run (skipped), a non-terminal run (skipped), a dry run (no disk
mutation), and an archive failure (worktree + branch preserved).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from se3.engine.merge import worktree_gc
from se3.engine.merge.worktree_gc import (
    WorktreeGCReport,
    find_stale_worktree_runs,
    gc_worktree_runs,
    _dir_size,
)


# --------------------------------------------------------------------------- #
# git / fixture helpers
# --------------------------------------------------------------------------- #

def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, text=True,
    )


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# Test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _branch_exists(path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", branch],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def _make_worktree_run(
    project_root: Path,
    *,
    name: str,
    branch: str,
    base: str,
    status: str = "completed",
    is_worktree_mode: bool = True,
    merged: bool = False,
    age_seconds: float = 0.0,
    engine_json_text: str | None = None,
) -> Path:
    """Create a real git worktree under ``se3/worktrees/<name>`` with a run state.

    Returns the worktree directory path. ``merged`` merges the branch back into
    ``base`` so it becomes an ancestor of HEAD; ``age_seconds`` backdates the
    engine.json mtime so the over-age filter can be exercised.
    """
    # Create the branch with a distinct commit, then return to base.
    _git(project_root, "checkout", "-b", branch)
    (project_root / f"{name}.txt").write_text(f"work for {name}\n")
    _git(project_root, "add", f"{name}.txt")
    _git(project_root, "commit", "-m", f"work on {branch}")
    _git(project_root, "checkout", base)

    wt_path = project_root / "se3" / "worktrees" / name
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git(project_root, "worktree", "add", str(wt_path), branch)

    if merged:
        _git(project_root, "merge", "--no-edit", branch)

    state_dir = wt_path / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    engine_json = state_dir / "engine.json"
    if engine_json_text is not None:
        engine_json.write_text(engine_json_text)
    else:
        header = {
            "flow_id": f"flow-{name}",
            "status": status,
            "task_description": f"task {name}",
            "task_type": "feature",
            "is_worktree_mode": is_worktree_mode,
            "worktree_branch": branch,
            "worktree_path": str(wt_path),
            "worktree_original_branch": base,
        }
        engine_json.write_text(json.dumps(header, indent=2) + "\n")

    if age_seconds:
        old = time.time() - age_seconds
        os.utime(engine_json, (old, old))
    return wt_path


# --------------------------------------------------------------------------- #
# find_stale_worktree_runs
# --------------------------------------------------------------------------- #

class TestFindStaleWorktreeRuns:
    def test_returns_only_terminal_over_age_worktree_runs(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        _make_worktree_run(
            tmp_path, name="stale-done", branch="feat-done", base=base,
            status="completed", age_seconds=48 * 3600,
        )
        # Excluded: fresh (not over age)
        _make_worktree_run(
            tmp_path, name="fresh", branch="feat-fresh", base=base,
            status="completed", age_seconds=0,
        )
        # Excluded: non-terminal
        _make_worktree_run(
            tmp_path, name="running", branch="feat-run", base=base,
            status="running", age_seconds=48 * 3600,
        )
        # Excluded: not a worktree-mode run
        _make_worktree_run(
            tmp_path, name="nonwt", branch="feat-nonwt", base=base,
            status="completed", is_worktree_mode=False, age_seconds=48 * 3600,
        )

        stale = find_stale_worktree_runs(tmp_path, max_age_seconds=24 * 3600)
        names = {r["name"] for r in stale}
        assert names == {"stale-done"}
        rec = stale[0]
        assert rec["worktree_branch"] == "feat-done"
        assert rec["worktree_original_branch"] == base
        assert rec["status"] == "completed"
        assert rec["flow_id"] == "flow-stale-done"

    def test_failed_status_is_terminal(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        _make_worktree_run(
            tmp_path, name="failed-run", branch="feat-failed", base=base,
            status="failed", age_seconds=48 * 3600,
        )
        stale = find_stale_worktree_runs(tmp_path, max_age_seconds=24 * 3600)
        assert {r["name"] for r in stale} == {"failed-run"}

    def test_corrupt_json_excluded(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        _make_worktree_run(
            tmp_path, name="corrupt", branch="feat-corrupt", base=base,
            age_seconds=48 * 3600, engine_json_text="{not valid json",
        )
        stale = find_stale_worktree_runs(tmp_path, max_age_seconds=24 * 3600)
        assert stale == []

    def test_archive_dir_not_scanned(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        archive = tmp_path / "se3" / "worktrees" / ".archive" / "x" / "se3" / "state"
        archive.mkdir(parents=True)
        (archive / "engine.json").write_text(
            json.dumps({"status": "completed", "is_worktree_mode": True,
                        "worktree_branch": "x", "flow_id": "f"}) + "\n"
        )
        old = time.time() - 48 * 3600
        os.utime(archive / "engine.json", (old, old))
        assert find_stale_worktree_runs(tmp_path, max_age_seconds=24 * 3600) == []

    def test_missing_worktrees_root(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert find_stale_worktree_runs(tmp_path) == []


# --------------------------------------------------------------------------- #
# _dir_size
# --------------------------------------------------------------------------- #

class TestDirSize:
    def test_sums_file_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_bytes(b"12345")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_bytes(b"678")
        assert _dir_size(tmp_path) == 8

    def test_missing_dir_is_zero(self, tmp_path: Path) -> None:
        assert _dir_size(tmp_path / "nope") == 0


# --------------------------------------------------------------------------- #
# gc_worktree_runs
# --------------------------------------------------------------------------- #

class TestGcMergedBranch:
    def test_archives_and_deletes_merged_branch(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        wt = _make_worktree_run(
            tmp_path, name="merged", branch="feat-merged", base=base,
            merged=True, age_seconds=48 * 3600,
        )
        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        assert [n for n, _p, _b in report.archived] == ["merged"]
        assert report.reclaimed_bytes > 0
        assert report.retained_unmerged == []
        # Branch deleted, worktree removed.
        assert not _branch_exists(tmp_path, "feat-merged")
        assert not wt.exists()
        # Archive landed under se3/worktrees/.archive/worktree_<name>-<epoch>
        # (named from the RUN name, not the branch slug).
        _name, archive_path, _bytes = report.archived[0]
        assert archive_path is not None
        assert archive_path.parent == tmp_path / "se3" / "worktrees" / ".archive"
        assert re.fullmatch(r"worktree_merged-\d+", archive_path.name)
        assert archive_path.is_dir()
        # Terminal state promoted into the main archive.
        assert (tmp_path / "se3" / "state" / "archive"
                / "engine_flow-merged.json").exists()

    def test_deletes_merged_branch_when_head_on_unrelated_branch(
        self, tmp_path: Path
    ) -> None:
        # Regression: the branch is provably merged into its RECORDED original
        # branch (base), but GC runs while the main checkout sits on an
        # unrelated branch that does NOT contain it. ``git branch -d`` evaluates
        # its fully-merged gate against the current checkout/upstream (not the
        # recorded base), so it would refuse the delete and the merged branch
        # would be wrongly retained+warned. GC must still delete it because our
        # own ancestor check against the correct base already proved the merge.
        base = _init_repo(tmp_path)
        # Capture the initial commit BEFORE feat-merged exists so ``release`` can
        # branch from it — a release that merely branched off ``base`` after the
        # helper's fast-forward merge would itself contain feat-merged and thus
        # not reproduce the ``git branch -d`` refusal.
        initial = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        wt = _make_worktree_run(
            tmp_path, name="merged", branch="feat-merged", base=base,
            merged=True, age_seconds=48 * 3600,
        )
        # Move HEAD onto a divergent branch that does NOT contain feat-merged.
        _git(tmp_path, "checkout", "-b", "release", initial)
        (tmp_path / "release.txt").write_text("release only\n")
        _git(tmp_path, "add", "release.txt")
        _git(tmp_path, "commit", "-m", "release-only work")

        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        assert [n for n, _p, _b in report.archived] == ["merged"]
        assert report.retained_unmerged == []
        assert not _branch_exists(tmp_path, "feat-merged")
        assert not wt.exists()


class TestGcUnmergedBranch:
    def test_retains_unmerged_branch_ref(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        wt = _make_worktree_run(
            tmp_path, name="unmerged", branch="feat-unmerged", base=base,
            merged=False, age_seconds=48 * 3600,
        )
        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        # Archived and worktree removed, but branch ref MUST survive.
        assert [n for n, _p, _b in report.archived] == ["unmerged"]
        assert not wt.exists()
        assert _branch_exists(tmp_path, "feat-unmerged")
        assert len(report.retained_unmerged) == 1
        branch, original, _reason = report.retained_unmerged[0]
        assert branch == "feat-unmerged"
        assert original == base
        assert report.reclaimed_bytes > 0


class TestGcUnresolvableBase:
    """Merge must be proven against the recorded original branch only — never a
    HEAD fallback. A run whose original branch is gone (or was never recorded)
    is undecidable and MUST retain its branch even if that branch happens to be
    an ancestor of the current HEAD.
    """

    def _rewrite_original_branch(self, wt: Path, value) -> None:
        engine_json = wt / "se3" / "state" / "engine.json"
        header = json.loads(engine_json.read_text())
        if value is None:
            header.pop("worktree_original_branch", None)
        else:
            header["worktree_original_branch"] = value
        engine_json.write_text(json.dumps(header, indent=2) + "\n")
        old = time.time() - 48 * 3600
        os.utime(engine_json, (old, old))

    def test_retains_branch_when_original_branch_missing_ref(
        self, tmp_path: Path
    ) -> None:
        base = _init_repo(tmp_path)
        # merged=True makes feat-ghost an ancestor of HEAD, so a (removed) HEAD
        # fallback would wrongly classify it merged. The recorded original branch
        # no longer resolves, so merge into the INTENDED base is unproven.
        wt = _make_worktree_run(
            tmp_path, name="ghostbase", branch="feat-ghost", base=base,
            merged=True, age_seconds=48 * 3600,
        )
        self._rewrite_original_branch(wt, "deleted-original-branch")

        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        assert [n for n, _p, _b in report.archived] == ["ghostbase"]
        assert not wt.exists()
        # Branch ref MUST survive despite being an ancestor of HEAD.
        assert _branch_exists(tmp_path, "feat-ghost")
        assert len(report.retained_unmerged) == 1
        branch, original, reason = report.retained_unmerged[0]
        assert branch == "feat-ghost"
        assert original == "deleted-original-branch"
        assert "no longer resolvable" in reason

    def test_retains_branch_when_no_original_branch_recorded(
        self, tmp_path: Path
    ) -> None:
        base = _init_repo(tmp_path)
        wt = _make_worktree_run(
            tmp_path, name="nobase", branch="feat-nobase", base=base,
            merged=True, age_seconds=48 * 3600,
        )
        self._rewrite_original_branch(wt, None)

        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        assert [n for n, _p, _b in report.archived] == ["nobase"]
        assert not wt.exists()
        assert _branch_exists(tmp_path, "feat-nobase")
        assert len(report.retained_unmerged) == 1
        branch, _original, reason = report.retained_unmerged[0]
        assert branch == "feat-nobase"
        assert "no recorded original branch" in reason


class TestGcDryRun:
    def test_dry_run_touches_nothing(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        wt = _make_worktree_run(
            tmp_path, name="dry", branch="feat-dry", base=base,
            merged=True, age_seconds=48 * 3600,
        )
        report = gc_worktree_runs(
            tmp_path, max_age_seconds=24 * 3600, dry_run=True,
        )

        # Accounting is populated...
        assert [n for n, _p, _b in report.archived] == ["dry"]
        assert report.reclaimed_bytes > 0
        # ...but the disk is untouched: worktree, branch, and no archive dir.
        assert wt.exists()
        assert _branch_exists(tmp_path, "feat-dry")
        assert not (tmp_path / "se3" / "worktrees" / ".archive").exists()
        # Dry-run archive path is None (nothing written).
        assert report.archived[0][1] is None


class TestGcArchiveFailure:
    def test_archive_failure_preserves_worktree_and_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _init_repo(tmp_path)
        wt = _make_worktree_run(
            tmp_path, name="archfail", branch="feat-archfail", base=base,
            merged=True, age_seconds=48 * 3600,
        )

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(worktree_gc, "_archive_worktree", _boom)
        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        # Destructive steps skipped: worktree + branch preserved, error recorded.
        assert wt.exists()
        assert _branch_exists(tmp_path, "feat-archfail")
        assert report.archived == []
        assert report.reclaimed_bytes == 0
        assert len(report.errors) == 1
        assert report.errors[0][0] == "archfail"
        assert "archive" in report.errors[0][1]

    def test_removal_failure_not_reported_as_reclaimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``remove_worktree`` swallows git failures (locked / permission) and
        # only logs; simulate a removal that leaves the directory in place and
        # assert the GC records an error rather than a phantom reclamation.
        base = _init_repo(tmp_path)
        wt = _make_worktree_run(
            tmp_path, name="stuck", branch="feat-stuck", base=base,
            merged=True, age_seconds=48 * 3600,
        )

        def _noop_remove(project_root, wt_path):
            # Directory intentionally left in place — the leak persists.
            return None

        monkeypatch.setattr(worktree_gc, "remove_worktree", _noop_remove)
        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        # Nothing reclaimed, error recorded, branch kept intact for a re-run.
        assert wt.exists()
        assert _branch_exists(tmp_path, "feat-stuck")
        assert report.archived == []
        assert report.reclaimed_bytes == 0
        assert len(report.errors) == 1
        assert report.errors[0][0] == "stuck"
        assert "removal failed" in report.errors[0][1]


class TestGcRunIsolation:
    def test_one_failure_does_not_abort_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = _init_repo(tmp_path)
        _make_worktree_run(
            tmp_path, name="aaa-bad", branch="feat-bad", base=base,
            merged=True, age_seconds=48 * 3600,
        )
        good_wt = _make_worktree_run(
            tmp_path, name="zzz-good", branch="feat-good", base=base,
            merged=True, age_seconds=48 * 3600,
        )

        real_archive = worktree_gc._archive_worktree

        def _selective(project_root, branch, wt_path, **kwargs):
            if branch == "feat-bad":
                raise OSError("boom")
            return real_archive(project_root, branch, wt_path, **kwargs)

        monkeypatch.setattr(worktree_gc, "_archive_worktree", _selective)
        report = gc_worktree_runs(tmp_path, max_age_seconds=24 * 3600)

        # The good run after the failing one still processed.
        assert [n for n, _p, _b in report.archived] == ["zzz-good"]
        assert not good_wt.exists()
        assert len(report.errors) == 1
        assert report.errors[0][0] == "aaa-bad"
