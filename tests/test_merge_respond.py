"""Tests for se3 merge-respond command (merge_respond.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tianluo.commands.merge_respond import process_merge_response
from tianluo.engine.merge.human_call import DEGRADED_CALL_TYPE


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit on branch 'main'."""
    subprocess.run(
        ["git", "init", str(path), "--initial-branch=main"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _start_merge_conflict(path: Path) -> None:
    """Create a real in-progress merge with a conflict.

    Sets up a branch 'feature' that modifies README.md differently
    from 'main', then starts ``git merge feature`` so the repo is
    in a mid-merge state with conflict markers in the working tree.
    """
    # Create feature branch from the initial commit (before diverging changes)
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-b", "feature"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("feature content\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "feature change"],
        check=True, capture_output=True,
    )

    # Switch back to main and make a CONFLICTING change
    subprocess.run(
        ["git", "-C", str(path), "checkout", "main"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("main content\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "main change"],
        check=True, capture_output=True,
    )

    # Now merge feature — this will conflict because both branches changed README.md
    result = subprocess.run(
        ["git", "-C", str(path), "merge", "feature"],
        capture_output=True, text=True, check=False,
    )
    # merge should fail with conflicts
    assert result.returncode != 0, "Expected merge conflict but merge succeeded"
    assert (
        "CONFLICT" in result.stdout
        or "CONFLICT" in result.stderr
        or "conflict" in result.stderr.lower()
    ), f"Expected conflict markers but got: {result.stdout} {result.stderr}"


def _create_merge_call_file(
    path: Path,
    files: list[dict],
    call_type: str = "merge_conflict",
) -> Path:
    """Create a merge call file with the given files."""
    call_data = {
        "type": call_type,
        "ours_branch": "main",
        "theirs_branch": "feature",
        "merge_base": "abc123",
        "files": files,
    }
    call_file = path / "merge_call.json"
    call_file.write_text(json.dumps(call_data), encoding="utf-8")
    return call_file


def _create_response_file(call_file: Path, choice: str, feedback: str = "") -> Path:
    """Create a response file next to the call file."""
    response_path = Path(str(call_file) + ".response")
    response_data = {"choice": choice}
    if feedback:
        response_data["feedback"] = feedback
    response_path.write_text(json.dumps(response_data), encoding="utf-8")
    return response_path


class TestCallFileNotFound:
    def test_returns_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"
        exit_code = process_merge_response(missing, project_root=tmp_path)
        assert exit_code == 1


class TestResponseFileNotFound:
    def test_returns_error(self, tmp_path: Path) -> None:
        call_file = tmp_path / "merge_call.json"
        call_file.write_text('{"type": "merge_conflict"}', encoding="utf-8")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestInvalidJson:
    def test_invalid_call_file(self, tmp_path: Path) -> None:
        call_file = tmp_path / "merge_call.json"
        call_file.write_text("not json", encoding="utf-8")
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_invalid_response_file(self, tmp_path: Path) -> None:
        call_file = tmp_path / "merge_call.json"
        call_file.write_text('{"type": "merge_conflict"}', encoding="utf-8")
        response_file = Path(str(call_file) + ".response")
        response_file.write_text("not json", encoding="utf-8")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestInvalidChoice:
    def test_unknown_choice(self, tmp_path: Path) -> None:
        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "unknown_choice")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestStrictSentinelDetection:
    """Critical safety: strict-mode placeholder must NEVER be written to disk."""

    def test_refuses_strict_placeholder(self, tmp_path: Path) -> None:
        """A call file containing the strict sentinel must be rejected."""
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "foo.py",
                    "llm_resolution": {
                        "resolved_content": (
                            "[__SE3_STRICT_PLACEHOLDER__: LLM resolution was skipped. "
                            "Please resolve conflicts manually.]"
                        ),
                    },
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_refuses_mixed_files_with_sentinel(self, tmp_path: Path) -> None:
        """If ANY file has the sentinel, the entire accept is refused."""
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "good.py",
                    "llm_resolution": {"resolved_content": "print('ok')"},
                },
                {
                    "path": "bad.py",
                    "llm_resolution": {
                        "resolved_content": (
                            "[__SE3_STRICT_PLACEHOLDER__: placeholder]"
                        ),
                    },
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_allows_accept_without_sentinel(self, tmp_path: Path) -> None:
        """Normal call files without the sentinel are accepted."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        # Resolve the conflict by writing the resolved file
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved content\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert (
            (tmp_path / "README.md").read_text(encoding="utf-8")
            == "resolved content\n"
        )


class TestAcceptChoice:
    def test_writes_files_and_stages(self, tmp_path: Path) -> None:
        """Accept writes resolved content back and commits the merge."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
                {
                    "path": "src/module.py",
                    "llm_resolution": {"resolved_content": "def foo(): pass\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert (
            (tmp_path / "src" / "module.py").read_text(encoding="utf-8")
            == "def foo(): pass\n"
        )
        # Verify the merge was committed (no MERGE_HEAD left)
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not (tmp_path / git_dir / "MERGE_HEAD").exists()

    def test_skips_empty_resolved_content(self, tmp_path: Path) -> None:
        """Files with empty resolved content are skipped during write-back."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
                {
                    "path": "skip.py",
                    "llm_resolution": {"resolved_content": ""},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert (tmp_path / "README.md").read_text(encoding="utf-8") == "resolved README\n"
        # skip.py was skipped because resolved_content is empty
        assert not (tmp_path / "skip.py").exists()

    def test_commits_merge(self, tmp_path: Path) -> None:
        """Accept commits the merge after writing files."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "merged\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, check=True,
        )
        # After commit, the log should show a merge commit (not "initial")
        assert "initial" not in result.stdout.strip()

    @pytest.mark.parametrize(
        "call_type",
        [
            DEGRADED_CALL_TYPE,
            # Legacy spellings written by the retired spec-guardrails chain.
            # An old call file left on disk must still answer cleanly.
            "guardrail_violation",
            "guardrail_repair_stalled",
            "guardrail_repair_exhausted",
            "some_future_unknown_type",
        ],
    )
    def test_non_merge_conflict_type_accepts_as_manual_fix(
        self, tmp_path: Path, capsys, call_type: str,
    ) -> None:
        """Accept on a non-merge_conflict call type never auto-writes files.

        There is no per-file resolution to write back, so acceptance is
        only an acknowledgement and the operator fixes by hand.
        """
        _init_repo(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[{"path": "README.md",
                    "llm_resolution": {"resolved_content": "MUST NOT BE WRITTEN\n"}}],
            call_type=call_type,
        )
        _create_response_file(call_file, "accept", "fixed manually")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        # The resolution payload was NOT applied to the working tree.
        assert (tmp_path / "README.md").read_text() == "# Test\n"
        out = capsys.readouterr().out
        assert "no per-file resolution to write back" in out
        assert "fixed manually" in out

    def test_legacy_guardrail_call_file_fields_tolerated(
        self, tmp_path: Path, capsys,
    ) -> None:
        """An OLD call file carrying retired guardrails payload keys still
        answers cleanly instead of erroring.

        ``type: "guardrail_violation"`` plus ``violations`` /
        ``orphan_guardrails_violations`` were written by the removed
        spec-guardrails chain. Operators may still have such files on
        disk, so ``luo merge respond`` must accept them without raising.
        """
        _init_repo(tmp_path)
        call_file = tmp_path / "merge_call.json"
        call_file.write_text(
            json.dumps({
                "type": "guardrail_violation",
                "branch": "feature",
                "pre_merge_sha": "abc123",
                "violations": [
                    {
                        "file_path": "tianluo/specs/base/spec.md",
                        "violation_type": "WEAKENING",
                        "message": "SHALL weakened to SHOULD",
                    },
                ],
                "orphan_guardrails_violations": [
                    {
                        "file_path": "tianluo/specs/other/spec.md",
                        "violation_type": "DELETION",
                        "message": "requirement removed",
                    },
                ],
                "files": [],
            }),
            encoding="utf-8",
        )
        _create_response_file(call_file, "accept", "legacy file")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert "no per-file resolution to write back" in capsys.readouterr().out


class TestAbortChoice:
    def test_aborts_merge(self, tmp_path: Path) -> None:
        """Abort runs git merge --abort."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "abort")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        # Verify merge was aborted: no MERGE_HEAD
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_head_path = (
            tmp_path / git_dir / "MERGE_HEAD"
            if not Path(git_dir).is_absolute()
            else Path(git_dir) / "MERGE_HEAD"
        )
        assert not merge_head_path.exists()

    @pytest.mark.parametrize(
        "call_type",
        [
            DEGRADED_CALL_TYPE,
            # Legacy spellings kept so an old guardrails call file left on
            # disk still answers cleanly instead of failing on
            # `git merge --abort` with "no merge to abort".
            "guardrail_violation",
            "guardrail_repair_stalled",
            "guardrail_repair_exhausted",
        ],
    )
    def test_no_active_merge_type_abort_skips_git_merge_abort(
        self, tmp_path: Path, capsys, call_type: str,
    ) -> None:
        """Abort on a settled call type reports clean success without
        running ``git merge --abort`` (there is no merge in progress)."""
        _init_repo(tmp_path)
        # No in-progress merge — the merge was already aborted/rolled back.
        call_file = _create_merge_call_file(
            tmp_path,
            files=[],
            call_type=call_type,
        )
        _create_response_file(call_file, "abort", "ack")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "rollback to pre-merge state is already complete" in out
        assert "ack" in out

    def test_abort_unknown_type_still_runs_git_merge_abort(
        self, tmp_path: Path,
    ) -> None:
        """A type outside the no-active-merge list still aborts a real merge."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path, files=[], call_type="some_future_unknown_type",
        )
        _create_response_file(call_file, "abort")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not (Path(git_dir) / "MERGE_HEAD").exists()


class TestManualChoice:
    def test_returns_success(self, tmp_path: Path, capsys) -> None:
        """Manual choice returns success with the manual-resolve instructions."""
        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "manual", "will fix later")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "resolve the conflicts manually" in out
        assert "will fix later" in out

    def test_manual_parks_nothing_on_disk(self, tmp_path: Path) -> None:
        """Manual never leaves a marker file beside the call file.

        The retired ``.pending_guardrails`` parking mechanism used to
        write a sidecar here; nothing may be written now.
        """
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved\n"},
                },
            ],
        )
        _create_response_file(call_file, "manual")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0

        # No sidecar beside the call file other than the response itself
        # (the retired mechanism wrote ``<call>.pending_guardrails``).
        sidecars = sorted(
            q.name for q in tmp_path.iterdir()
            if q.name.startswith(call_file.name) and q.name != call_file.name
        )
        assert sidecars == [f"{call_file.name}.response"]
        # The proposed resolution was NOT written to the working tree.
        assert "<<<<<<<" in (tmp_path / "README.md").read_text()
        # The in-progress merge is untouched — the human finishes it by hand.
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert (Path(git_dir) / "MERGE_HEAD").exists()


class TestMainWorktreeLockRoot:
    def test_lock_targets_resolved_main_repo_root(self, tmp_path: Path) -> None:
        """process_merge_response must acquire the main-worktree mutex on the
        *main* repository root (resolved from a possibly-worktree
        project_root), not on the bare project_root — so a merge-respond
        launched from inside a linked worktree contends on the single
        project-wide lock file shared with se3 run / se3 merge.
        """
        from unittest.mock import MagicMock, patch

        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "manual", "will fix later")

        sentinel_root = Path("/resolved/main/repo")
        with patch(
            "tianluo.commands.run._resolve_main_lock_root",
            return_value=sentinel_root,
        ) as mock_resolve, patch(
            "tianluo.commands.merge.merge_lock.MergeLock"
        ) as MockLock:
            MockLock.return_value = MagicMock()
            exit_code = process_merge_response(call_file, project_root=tmp_path)

        assert exit_code == 0
        mock_resolve.assert_called_once_with(tmp_path)
        MockLock.assert_called_once_with(sentinel_root, blocking=True)


class TestEdgeCases:
    def test_missing_llm_resolution_key(self, tmp_path: Path) -> None:
        """Files missing llm_resolution key are handled gracefully."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "ok\n"},
                },
                {
                    "path": "file.py",
                    # no "llm_resolution" key
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        # Should succeed: README.md resolved, file.py skipped (empty content)
        assert exit_code == 0

    def test_none_llm_resolution(self, tmp_path: Path) -> None:
        """Files with null llm_resolution are handled gracefully."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = tmp_path / "merge_call.json"
        call_data = {
            "type": "merge_conflict",
            "files": [
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "ok\n"},
                },
                {
                    "path": "file.py",
                    "llm_resolution": None,
                },
            ],
        }
        call_file.write_text(json.dumps(call_data), encoding="utf-8")
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0


# =====================================================================
# G8 — git add returncode check, octopus first-parent
# =====================================================================


class TestGitAddReturncode:
    """G8 task 43 (G3): git add failures must NOT be silently swallowed."""

    def test_git_add_failure_returns_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When git add fails, process_merge_response returns 1."""
        import subprocess as subprocess_mod
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")

        original_run = subprocess_mod.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 4 and cmd[3] == "add":
                # Simulate git add failure
                from subprocess import CompletedProcess
                return CompletedProcess(
                    args=cmd, returncode=128,
                    stdout="", stderr="fatal: simulated git-add failure",
                )
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", fake_run)
        # The merge_respond module imported subprocess at the top, so we
        # also need to patch it via the imported module's namespace.
        from tianluo.commands import merge_respond as merge_respond_mod
        monkeypatch.setattr(
            merge_respond_mod.subprocess, "run", fake_run
        )

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestFirstParentSha:
    """G8 task 42 (G1): _first_parent_sha helper for octopus-safe parent walk."""

    def test_two_parent_merge_first_parent(self, tmp_path: Path) -> None:
        from tianluo.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        # Create a feature branch and merge it
        first_parent_expected = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feat"],
            check=True, capture_output=True,
        )
        (tmp_path / "feat.py").write_text("x")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feat"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--no-ff", "feat",
             "-m", "Merge feat"],
            check=True, capture_output=True,
        )

        first_parent = _first_parent_sha(tmp_path)
        assert first_parent == first_parent_expected

    def test_root_commit_raises_runtime_error(self, tmp_path: Path) -> None:
        from tianluo.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        # _init_repo only created the initial commit; HEAD has no parents
        with pytest.raises(RuntimeError, match="no parents"):
            _first_parent_sha(tmp_path)


# =====================================================================
# Merge-respond: version bump verification on accept
# =====================================================================


class TestVersionBumpAfterAccept:
    """The accept path verifies version advanced when a version file was resolved."""

    def _setup_repo_with_pyproject_and_conflict(self, tmp_path: Path) -> tuple[str, str]:
        """Set up a repo with pyproject.toml and a merge conflict in README.md.

        Returns (pre_sha, feature_branch_name).
        """
        _init_repo(tmp_path)

        # Add pyproject.toml on main
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "pyproject.toml"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add pyproject"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Create feature branch from current main
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        # Change pyproject version on feature
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.1.0"\n'
        )
        # Also change README on feature
        (tmp_path / "README.md").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature changes"],
            check=True, capture_output=True,
        )

        # Switch to main and make a CONFLICTING change in README
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )
        (tmp_path / "README.md").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "README.md"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "main change"],
            check=True, capture_output=True,
        )

        # Start merge — README.md will conflict, pyproject.toml merges cleanly
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature"],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0, "Expected merge conflict"
        assert "CONFLICT" in result.stdout or "CONFLICT" in result.stderr

        return pre_sha, "feature"

    def test_accepts_when_version_advanced(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When pyproject.toml version changed from pre-merge, accept passes."""
        pre_sha, _ = self._setup_repo_with_pyproject_and_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "pyproject.toml",
                    "llm_resolution": {
                        "resolved_content": (
                            '[project]\nname = "test"\nversion = "1.1.0"\n'
                        ),
                    },
                },
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        call_file.write_text(
            json.dumps({
                **json.loads(call_file.read_text(encoding="utf-8")),
                "ours_head_sha": pre_sha,
            }),
            encoding="utf-8",
        )
        _create_response_file(call_file, "accept")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0

    def test_rejects_when_version_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When pyproject.toml version did not change, accept fails."""
        pre_sha, _ = self._setup_repo_with_pyproject_and_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "pyproject.toml",
                    "llm_resolution": {
                        "resolved_content": (
                            '[project]\nname = "test"\nversion = "1.0.0"\n'
                        ),
                    },
                },
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        call_file.write_text(
            json.dumps({
                **json.loads(call_file.read_text(encoding="utf-8")),
                "ours_head_sha": pre_sha,
            }),
            encoding="utf-8",
        )
        _create_response_file(call_file, "accept")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_no_version_file_skips_check(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When no version file is in the resolution, version check is skipped."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0

