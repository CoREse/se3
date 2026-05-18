"""Tests for sync_state module — SyncState dataclass, load/save, code_fingerprint."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from se3.engine.sync_state import (
    SYNC_STATE_SCHEMA_VERSION,
    SyncState,
    compute_code_fingerprint,
    compute_file_content_hash,
    detect_file_set_change,
    load,
    save,
    state_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_state_file(root: Path, data: dict) -> Path:
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# SyncState dataclass tests
# ---------------------------------------------------------------------------

class TestSyncStateDataclass:
    """Task 1: SyncState dataclass fields, to_dict/from_dict roundtrip."""

    def test_defaults(self):
        s = SyncState()
        assert s.state_version == SYNC_STATE_SCHEMA_VERSION
        assert s.converged_at is None
        assert s.code_fingerprint == ""
        assert s.discovery_converged is False
        assert s.spec_deps == {}
        assert s.obsolete_specs == []

    def test_to_dict_from_dict_roundtrip_empty(self):
        s = SyncState(
            converged_at="2026-01-01T00:00:00Z",
            code_fingerprint="abc123",
            discovery_converged=True,
        )
        data = s.to_dict()
        restored = SyncState.from_dict(data)
        assert restored.state_version == s.state_version
        assert restored.converged_at == s.converged_at
        assert restored.code_fingerprint == s.code_fingerprint
        assert restored.discovery_converged == s.discovery_converged
        assert restored.spec_deps == s.spec_deps
        assert restored.obsolete_specs == s.obsolete_specs

    def test_to_dict_from_dict_with_spec_deps(self):
        s = SyncState(
            converged_at="2026-01-01T00:00:00Z",
            code_fingerprint="def456",
            discovery_converged=True,
            spec_deps={
                "my-feature": {
                    "spec_hash": "sha_spec",
                    "deps": {
                        "src/my_feature.py": "sha1",
                        "tests/test_my_feature.py": "sha2",
                    },
                },
            },
            obsolete_specs=["old-thing"],
        )
        data = s.to_dict()
        restored = SyncState.from_dict(data)

        assert restored.spec_deps == s.spec_deps
        assert restored.obsolete_specs == ["old-thing"]

        entry = restored.spec_deps["my-feature"]
        assert entry["spec_hash"] == "sha_spec"
        assert entry["deps"]["src/my_feature.py"] == "sha1"
        assert entry["deps"]["tests/test_my_feature.py"] == "sha2"

    def test_to_dict_preserves_types(self):
        """Ensure to_dict produces JSON-serialisable types."""
        s = SyncState(
            converged_at="2026-01-01T00:00:00Z",
            code_fingerprint="fp",
            discovery_converged=True,
            spec_deps={"a": {"spec_hash": "h", "deps": {"f.py": "fh"}}},
            obsolete_specs=["obs"],
        )
        data = s.to_dict()
        # Must be serialisable without exception
        json.dumps(data)

    def test_from_dict_tolerates_missing_keys(self):
        """from_dict with empty dict returns defaults."""
        s = SyncState.from_dict({})
        assert s.state_version == SYNC_STATE_SCHEMA_VERSION
        assert s.converged_at is None
        assert s.code_fingerprint == ""
        assert s.discovery_converged is False
        assert s.spec_deps == {}
        assert s.obsolete_specs == []

    def test_spec_in_sync_all_match(self):
        s = SyncState(
            spec_deps={
                "feat": {
                    "spec_hash": "abc",
                    "deps": {"src/a.py": "h1", "src/b.py": "h2"},
                },
            }
        )
        current = {"src/a.py": "h1", "src/b.py": "h2"}
        assert s.spec_in_sync("feat", current) is True

    def test_spec_in_sync_one_mismatch(self):
        s = SyncState(
            spec_deps={
                "feat": {
                    "spec_hash": "abc",
                    "deps": {"src/a.py": "h1", "src/b.py": "h2"},
                },
            }
        )
        current = {"src/a.py": "h1", "src/b.py": "changed"}
        assert s.spec_in_sync("feat", current) is False

    def test_spec_in_sync_missing_entry(self):
        s = SyncState()
        assert s.spec_in_sync("nonexistent", {}) is False

    def test_spec_in_sync_empty_deps(self):
        s = SyncState(spec_deps={"feat": {"spec_hash": "abc", "deps": {}}})
        assert s.spec_in_sync("feat", {}) is False


# ---------------------------------------------------------------------------
# Load tests — Task 2: three self-invalidation paths
# ---------------------------------------------------------------------------

class TestLoad:
    """Task 2: load() returns None for missing/corrupt/version-mismatch."""

    def test_load_missing_file(self, tmp_path):
        assert load(tmp_path) is None

    def test_load_valid(self, tmp_path):
        s = SyncState(
            converged_at="2026-01-01T00:00:00Z",
            code_fingerprint="fp",
            discovery_converged=True,
        )
        save(s, tmp_path)
        restored = load(tmp_path)
        assert restored is not None
        assert restored.code_fingerprint == "fp"
        assert restored.discovery_converged is True

    def test_load_corrupt_json_returns_none(self, tmp_path):
        p = state_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("this is not json", encoding="utf-8")
        assert load(tmp_path) is None

    def test_load_version_mismatch_returns_none(self, tmp_path):
        _write_state_file(
            tmp_path,
            {
                "state_version": 999,
                "converged_at": "2026-01-01T00:00:00Z",
                "code_fingerprint": "fp",
                "discovery_converged": True,
                "spec_deps": {},
                "obsolete_specs": [],
            },
        )
        assert load(tmp_path) is None

    def test_load_missing_version_field_treated_as_current(self, tmp_path):
        """When state_version is absent, from_dict fills the current version."""
        _write_state_file(
            tmp_path,
            {
                "converged_at": "2026-01-01T00:00:00Z",
                "code_fingerprint": "fp",
                "discovery_converged": True,
                "spec_deps": {},
                "obsolete_specs": [],
            },
        )
        result = load(tmp_path)
        assert result is not None
        assert result.state_version == SYNC_STATE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Save tests — Task 2: atomic write
# ---------------------------------------------------------------------------

class TestSave:
    """Task 2: save() uses atomic tmp+rename pattern."""

    def test_save_creates_directory_and_file(self, tmp_path):
        s = SyncState(code_fingerprint="fp1")
        p = save(s, tmp_path)
        assert p.exists()
        loaded = json.loads(p.read_text(encoding="utf-8"))
        assert loaded["code_fingerprint"] == "fp1"

    def test_save_is_readable_by_load(self, tmp_path):
        s = SyncState(
            converged_at="2026-06-01T12:00:00Z",
            code_fingerprint="test_fp",
            discovery_converged=True,
            spec_deps={"x": {"spec_hash": "h", "deps": {"a.py": "ah"}}},
            obsolete_specs=["old"],
        )
        save(s, tmp_path)
        restored = load(tmp_path)
        assert restored is not None
        assert restored.converged_at == s.converged_at
        assert restored.code_fingerprint == s.code_fingerprint
        assert restored.discovery_converged == s.discovery_converged
        assert restored.spec_deps == s.spec_deps
        assert restored.obsolete_specs == s.obsolete_specs

    def test_save_overwrites_existing(self, tmp_path):
        s1 = SyncState(code_fingerprint="old")
        save(s1, tmp_path)
        s2 = SyncState(code_fingerprint="new")
        save(s2, tmp_path)
        restored = load(tmp_path)
        assert restored is not None
        assert restored.code_fingerprint == "new"


# ---------------------------------------------------------------------------
# compute_file_content_hash tests
# ---------------------------------------------------------------------------

class TestComputeFileContentHash:
    def test_returns_sha256(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello", encoding="utf-8")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert compute_file_content_hash(f) == expected

    def test_returns_none_for_missing(self, tmp_path):
        assert compute_file_content_hash(tmp_path / "nonexistent") is None

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello", encoding="utf-8")
        f2.write_text("world", encoding="utf-8")
        assert compute_file_content_hash(f1) != compute_file_content_hash(f2)

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "x.txt"
        f2 = tmp_path / "y.txt"
        f1.write_text("same", encoding="utf-8")
        f2.write_text("same", encoding="utf-8")
        assert compute_file_content_hash(f1) == compute_file_content_hash(f2)


# ---------------------------------------------------------------------------
# compute_code_fingerprint tests — Task 3
# ---------------------------------------------------------------------------

class TestComputeCodeFingerprint:
    """Task 3: fingerprint sensitive to add/delete/modify/rename, insensitive
    to mtime-only changes."""

    def _init_git(self, tmp_path: Path) -> None:
        import subprocess
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True,
        )

    def _commit_file(self, repo: Path, rel_path: str, content: str) -> None:
        import subprocess
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "add", rel_path],
            cwd=str(repo), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", f"Add {rel_path}"],
            cwd=str(repo), check=True,
        )

    def test_fingerprint_changes_on_content_change(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/main.py", "print('hello')")
        fp1 = compute_code_fingerprint(tmp_path)

        # Modify file content
        (tmp_path / "src" / "main.py").write_text("print('world')", encoding="utf-8")
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_changes_on_file_add(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/main.py", "hello")
        fp1 = compute_code_fingerprint(tmp_path)

        # Add new untracked non-ignored file
        (tmp_path / "src" / "new.py").write_text("new", encoding="utf-8")
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_changes_on_file_delete(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/main.py", "hello")
        fp1 = compute_code_fingerprint(tmp_path)

        (tmp_path / "src" / "main.py").unlink()
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_changes_on_rename(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/main.py", "hello")
        fp1 = compute_code_fingerprint(tmp_path)

        (tmp_path / "src" / "main.py").rename(tmp_path / "src" / "renamed.py")
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_insensitive_to_mtime(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/main.py", "hello")
        fp1 = compute_code_fingerprint(tmp_path)

        # Touch mtime only
        f = tmp_path / "src" / "main.py"
        os.utime(str(f), (f.stat().st_atime, f.stat().st_mtime + 100))
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 == fp2

    def test_fingerprint_excludes_se3_directory(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/main.py", "hello")
        fp1 = compute_code_fingerprint(tmp_path)

        # Write something under se3/
        (tmp_path / "se3" / "specs" / "base").mkdir(parents=True)
        (tmp_path / "se3" / "specs" / "base" / "spec.md").write_text(
            "<!-- spec-format: v1 -->\n# base Specification\n## Purpose\nTest.\n### Requirement: R1\n", encoding="utf-8"
        )
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 == fp2

    def test_fingerprint_is_stable(self, tmp_path):
        """Same tree → same fingerprint on repeated calls."""
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "1")
        self._commit_file(tmp_path, "b.py", "2")
        fp1 = compute_code_fingerprint(tmp_path)
        fp2 = compute_code_fingerprint(tmp_path)
        fp3 = compute_code_fingerprint(tmp_path)
        assert fp1 == fp2 == fp3
        assert len(fp1) == 64  # SHA-256 hex length

    def test_fingerprint_empty_repo(self, tmp_path):
        self._init_git(tmp_path)
        fp = compute_code_fingerprint(tmp_path)
        assert len(fp) == 64
        # Empty tree is stable
        assert compute_code_fingerprint(tmp_path) == fp

    def test_fingerprint_untracked_non_ignored_included(self, tmp_path):
        self._init_git(tmp_path)
        fp1 = compute_code_fingerprint(tmp_path)

        (tmp_path / "untracked.txt").write_text("data", encoding="utf-8")
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_untracked_ignored_excluded(self, tmp_path):
        self._init_git(tmp_path)
        (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
        fp1 = compute_code_fingerprint(tmp_path)

        (tmp_path / "debug.log").write_text("log data", encoding="utf-8")
        fp2 = compute_code_fingerprint(tmp_path)
        assert fp1 == fp2

    def test_fingerprint_deleted_tracked_file_detected(self, tmp_path):
        """When a tracked file is deleted from the working tree, the fingerprint
        changes because its blob SHA is no longer in ls-files --stage."""
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "src/a.py", "a")
        self._commit_file(tmp_path, "src/b.py", "b")
        fp_before = compute_code_fingerprint(tmp_path)

        # git rm b.py and commit
        import subprocess
        subprocess.run(
            ["git", "rm", "-q", "src/b.py"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "remove b"],
            cwd=str(tmp_path), check=True,
        )
        fp_after = compute_code_fingerprint(tmp_path)
        assert fp_before != fp_after


# ---------------------------------------------------------------------------
# detect_file_set_change tests — Task 3
# ---------------------------------------------------------------------------

class TestDetectFileSetChange:
    """Task 3: detect_file_set_change identifies added/deleted/renamed files."""

    def _init_git(self, tmp_path: Path) -> None:
        import subprocess
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True,
        )

    def _commit_file(self, repo: Path, rel_path: str, content: str) -> None:
        import subprocess
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "add", rel_path],
            cwd=str(repo), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", f"Add {rel_path}"],
            cwd=str(repo), check=True,
        )

    def test_no_change_when_file_set_matches(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "a")
        self._commit_file(tmp_path, "b.py", "b")

        state = SyncState(
            spec_deps={
                "spec1": {"spec_hash": "h1", "deps": {"a.py": "ah", "b.py": "bh"}},
            }
        )
        assert detect_file_set_change(state, tmp_path) is False

    def test_change_when_new_file_appears(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "a")

        state = SyncState(
            spec_deps={
                "spec1": {"spec_hash": "h1", "deps": {"a.py": "ah"}},
            }
        )
        # Now add a new untracked file
        (tmp_path / "new_file.py").write_text("new", encoding="utf-8")
        assert detect_file_set_change(state, tmp_path) is True

    def test_change_when_file_disappears(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "a")
        self._commit_file(tmp_path, "b.py", "b")

        state = SyncState(
            spec_deps={
                "spec1": {"spec_hash": "h1", "deps": {"a.py": "ah", "b.py": "bh"}},
            }
        )
        # Remove b.py from the working tree
        (tmp_path / "b.py").unlink()
        assert detect_file_set_change(state, tmp_path) is True

    def test_no_change_with_empty_spec_deps(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "a")

        state = SyncState()  # no spec_deps
        assert detect_file_set_change(state, tmp_path) is False

    def test_no_change_empty_recorded_paths(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "a")

        state = SyncState(
            spec_deps={"spec1": {"spec_hash": "h1", "deps": {}}}
        )
        assert detect_file_set_change(state, tmp_path) is False

    def test_se3_files_ignored_in_change_detection(self, tmp_path):
        self._init_git(tmp_path)
        self._commit_file(tmp_path, "a.py", "a")

        state = SyncState(
            spec_deps={
                "spec1": {"spec_hash": "h1", "deps": {"a.py": "ah"}},
            }
        )
        # Write under se3/ — should NOT trigger change detection
        (tmp_path / "se3" / "state").mkdir(parents=True)
        (tmp_path / "se3" / "state" / "sync_state.json").write_text("{}", encoding="utf-8")
        assert detect_file_set_change(state, tmp_path) is False
