"""Tests for MergeOrchestrator sequential merge logic."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tianluo.engine.merge.human_call import DEGRADED_CALL_TYPE
from tianluo.engine.merge.orchestrator import MergeOrchestrator, MergeReport


def _get_default_branch(path: Path) -> str:
    """Get the current branch name."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    # Ignore tianluo/ runtime directory so that merge lock files and logs
    # do not cause "untracked working tree files would be overwritten".
    # version-intents is whitelisted (matching the real init/migrate template)
    # so worktree sessions' intents actually travel with the branch — write_intent
    # now refuses to write to a gitignored path.
    (path / ".gitignore").write_text(
        "/tianluo/*\n!/tianluo/specs/\n!/tianluo/issues/\n!/tianluo/version-intents/\n"
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _add_commit(path: Path, filename: str, content: str, message: str) -> None:
    """Add a file and commit."""
    (path / filename).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True, capture_output=True,
    )


def _create_branch(path: Path, branch: str) -> None:
    """Create a new branch from current HEAD."""
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-b", branch],
        check=True, capture_output=True,
    )


def _is_working_tree_clean(path: Path) -> bool:
    """Check if working tree has no tracked uncommitted changes."""
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, check=True,
    )
    return not result.stdout.strip()


def _create_worktree(path: Path, branch: str, wt_dir: Path) -> None:
    """Create a git worktree for *branch* at *wt_dir*."""
    subprocess.run(
        ["git", "-C", str(path), "worktree", "add", str(wt_dir), branch],
        check=True, capture_output=True,
    )


def _write_pyproject(path: Path, version: str) -> None:
    """Write a minimal pyproject.toml with the given version."""
    content = (
        '[build-system]\nrequires = ["setuptools"]\n\n'
        '[project]\n'
        'name = "test-pkg"\n'
        f'version = "{version}"\n'
    )
    (path / "pyproject.toml").write_text(content)


def _read_pyproject_version(path: Path) -> str:
    """Read pyproject.toml's version from working tree."""
    content = (path / "pyproject.toml").read_text()
    import re
    match = re.search(
        r'\[project\][^\[]*?version\s*=\s*["\']([^"\']+)["\']',
        content, re.DOTALL,
    )
    assert match, "could not find version in pyproject.toml"
    return match.group(1)


def _commit(path: Path, message: str, *files: str) -> None:
    """Stage files and commit."""
    if files:
        subprocess.run(
            ["git", "-C", str(path), "add", *files],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(path), "add", "-A"],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True, capture_output=True,
    )


class TestMergeOrchestrator:
    def test_merge_single_clean_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]
        assert report.failed_branch is None
        assert report.log_file is not None
        assert report.log_file.exists()

    def test_intent_probe_ignores_consumed_historical_intents(
        self, tmp_path: Path
    ) -> None:
        """The legacy-aggregation suppression probe counts only unconsumed intents.

        Every reconcile commits the consumed intent JSON into master and never
        deletes it. Probing with include_consumed=True would see that permanent
        residue and suppress legacy per-branch aggregation for ALL later merges,
        so a pure legacy branch (direct version-file bump, no intent) would land
        verbatim with no bump. Only an as-yet-unconsumed intent — this merge's own
        contribution — should stand the legacy path down.
        """
        from tianluo.engine.version_intent import VersionIntent, mark_consumed, write_intent

        _init_repo(tmp_path)
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)

        # No intents at all → legacy aggregation stays enabled.
        assert orch._merged_tree_has_version_intents() is False

        # A historical, already-consumed intent (reconcile residue) must NOT
        # suppress aggregation for an unrelated later legacy merge.
        write_intent(
            tmp_path,
            VersionIntent(
                flow_id="flowHistoric",
                change_summary="old",
                versions_changes=["old feat"],
                bump_type="minor",
            ),
        )
        mark_consumed(tmp_path, "flowHistoric")
        assert orch._merged_tree_has_version_intents() is False

        # A fresh, unconsumed intent (this merge's own contribution) DOES.
        write_intent(
            tmp_path,
            VersionIntent(
                flow_id="flowFresh",
                change_summary="new",
                versions_changes=["new feat"],
                bump_type="minor",
            ),
        )
        assert orch._merged_tree_has_version_intents() is True

    def test_intent_probe_ignores_pre_existing_unrelated_intents(
        self, tmp_path: Path
    ) -> None:
        """An unconsumed intent already on master before THIS merge is ignored.

        Flow A finished merge_integrate and left its unconsumed intent on master
        while still awaiting version_reconcile. A separate legacy `se3 merge`
        that introduces NO intent must still run legacy aggregation — Flow A's
        leftover intent (present at the pre-merge tree) is not this merge's
        contribution, so it must not stand aggregation down.
        """
        from tianluo.engine.version_intent import VersionIntent, write_intent

        _init_repo(tmp_path)
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)

        # Flow A's unconsumed intent, committed to master before this merge.
        write_intent(
            tmp_path,
            VersionIntent(
                flow_id="flowA_pending",
                change_summary="pending",
                versions_changes=["pending feat"],
                bump_type="minor",
            ),
        )
        # tianluo/ is gitignored in this fixture; force-add so the intent lands in
        # the committed tree that intent_flow_ids_at_ref reads.
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "-f", "tianluo/version-intents"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "flow A intent (awaiting reconcile)"],
            check=True, capture_output=True,
        )
        # Simulate execute() having snapshotted the pre-merge tree's intents.
        from tianluo.engine.version_intent import intent_flow_ids_at_ref

        orch._pre_merge_intent_ids = intent_flow_ids_at_ref(tmp_path, "HEAD")
        assert orch._pre_merge_intent_ids == {"flowA_pending"}

        # The legacy branch introduces no new intent → aggregation stays enabled.
        assert orch._merged_tree_has_version_intents() is False

    def test_merge_multiple_clean_branches(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-a", "feature-b"])

        assert report.success is True
        assert report.merged_branches == ["feature-a", "feature-b"]

        # Verify both branches' content is present
        assert (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").exists()

    def test_conflict_llm_failure_escalates_to_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """default strategy + LLM resolver raises → human call file created, conflict markers preserved."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("base new content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )

        pre_merge_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Mock LLM resolver to fail — ``safe`` strategy escalates to human call.
        # (The new default is ``fast``, which does not write a human-call
        # file; this test pins ``safe`` explicitly to exercise the
        # escalation path.)
        from tianluo.engine.llm_caller import LLMCallError
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(LLMCallError("mock llm fail")),
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == "feature"
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Working tree should still have conflict markers (merge not aborted)
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_merge_head

    def test_fast_conflict_llm_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + LLM resolver raises → abort, no human call."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("base new content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )

        pre_merge_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Mock LLM resolver to fail — fast strategy should abort without human call
        from tianluo.engine.llm_caller import LLMCallError
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(LLMCallError("mock llm fail")),
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "llm_resolution_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_abort_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_abort_head == pre_merge_head

    def test_second_branch_fails_first_preserved(self, tmp_path: Path, monkeypatch) -> None:
        """If the second branch has a conflict, the first merge is preserved."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        # feature-a: clean merge
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        # feature-b: will conflict
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature-b")
        (tmp_path / "shared.txt").write_text("b content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on b"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("base new content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver to fail, triggering abort path on feature-b
        # under the ``safe`` strategy (the new default is ``fast`` which
        # does NOT escalate to human call on failure).
        from tianluo.engine.llm_caller import LLMCallError
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(LLMCallError("mock llm fail")),
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature-a", "feature-b"])

        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.pending_human is True
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None

        # Verify feature-a is merged (a.txt should exist)
        assert (tmp_path / "a.txt").exists()

        # Working tree should have conflict markers (default strategy leaves them for human)
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

    def test_log_file_created(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.log_file is not None
        assert report.log_file.exists()
        log_content = report.log_file.read_text()
        assert "Merge orchestrator starting" in log_content
        assert "feature" in log_content
        assert "merged successfully" in log_content

    def test_write_log_calls_fsync(self, tmp_path: Path) -> None:
        """B13 regression: _write_log must fsync so the log survives crash.

        After the parent-directory fsync was added, fsync is expected
        to be called twice — once on the file's fd (so the log content
        is durable) and once on the parent directory's fd (so the
        directory entry is durable, preventing the file from being
        invisible after recovery if a crash lands between content
        durable and entry durable).
        """
        from unittest.mock import patch

        _init_repo(tmp_path)
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        orch._log("test message for fsync")

        with patch("os.fsync") as mock_fsync:
            orch._write_log()

        assert orch.log_file is not None
        assert orch.log_file.exists()
        assert "test message for fsync" in orch.log_file.read_text()
        # fsync called once for the log fd, once for the parent dir fd.
        assert mock_fsync.call_count == 2, (
            f"Expected fsync to be called twice (file + parent dir), "
            f"got {mock_fsync.call_count}"
        )

    def test_already_up_to_date_merge_does_not_amend(self, tmp_path: Path) -> None:
        """Merging an already-merged branch must not create a commit or bump version."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "Add pyproject")

        # Create a feature branch and merge it into main FIRST
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        # Merge feature into main so it's already an ancestor
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        pre_version = subprocess.run(
            ["git", "-C", str(tmp_path), "show", f"{pre_head}:pyproject.toml"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert 'version = "1.0.0"' in pre_version

        # Now run orchestrator to merge the already-merged branch
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]

        # HEAD must NOT have changed — no new commit was produced
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # pyproject.toml version must still be 1.0.0 (no amend corruption)
        pyproject = tmp_path / "pyproject.toml"
        assert 'version = "1.0.0"' in pyproject.read_text()

        # No version aggregation should have occurred
        assert report.pre_merge_version == "1.0.0"
        assert report.final_version is None
        assert report.version_aggregation_skipped is True

    def test_pre_merge_rev_parse_head_failure_does_not_block_merge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed pre-merge rev-parse HEAD degrades gracefully.

        The pre-merge SHA is only diagnostic (bump inference / issue-ID
        reconciliation); losing it must not fail a merge that git
        performed successfully. The post-condition checks — which run
        git themselves — are what actually prove the merge landed.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        call_count = 0
        original_run_git = None

        def patch_run_git(project_root, *args, check=True, timeout=30):
            nonlocal call_count
            # Make the SECOND "rev-parse HEAD" call fail (the one inside
            # _merge_single_branch, lines 312-316). The first is in execute().
            if len(args) >= 2 and args[0] == "rev-parse" and args[1] == "HEAD":
                call_count += 1
                if call_count == 2:
                    class FakeResult:
                        returncode = 1
                        stdout = ""
                        stderr = "mock rev-parse failure"
                    return FakeResult()
            # Fall through to real _run_git for everything else
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # git merge itself succeeded and the post-condition checks confirm
        # the merge commit is on HEAD, so the merge is reported as a success
        # even though the pre-merge SHA could not be captured. No human call
        # is raised and nothing is rolled back.
        assert report.success is True
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert report.rollback_failed is False
        assert report.pending_human is False
        assert report.human_call_file is None
        assert "feature" in report.merged_branches

        # The merge commit is on HEAD and differs from the pre-merge HEAD.
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head
        assert report.outcomes[0].merge_commit_sha == post_head

    def test_strict_pre_merge_rev_parse_head_failure_does_not_block_merge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """strict strategy: a missing pre-merge SHA is equally non-fatal.

        Strict only diverges from safe on *conflict* handling; a clean
        merge whose pre-merge SHA read failed still succeeds.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        call_count = 0

        def patch_run_git(project_root, *args, check=True, timeout=30):
            nonlocal call_count
            if len(args) >= 2 and args[0] == "rev-parse" and args[1] == "HEAD":
                call_count += 1
                if call_count == 2:
                    class FakeResult:
                        returncode = 1
                        stdout = ""
                        stderr = "mock rev-parse failure"
                    return FakeResult()
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute(["feature"])

        # Same as safe: a missing pre_sha does not fail the merge.
        assert report.success is True
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert report.rollback_failed is False
        assert report.pending_human is False
        assert report.human_call_file is None
        assert "feature" in report.merged_branches

    def test_rev_parse_head_timeout_after_clean_merge_still_succeeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A post-merge rev-parse HEAD timeout is logged but not fatal.

        The merge-commit SHA read after a clean merge is diagnostic; the
        post-condition checks re-run git independently and are what prove
        the merge landed. The timeout must therefore surface in the merge
        log without failing an otherwise-good merge.
        """
        import subprocess as _sp

        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        call_count = 0

        def patch_run_git(project_root, *args, check=True, timeout=30):
            nonlocal call_count
            # Make the THIRD "rev-parse HEAD" call time out (the post-merge
            # one inside _merge_single_branch).  The first two are:
            #   1. execute() pre-merge SHA capture
            #   2. _merge_single_branch() pre_merge_sha capture
            if len(args) >= 2 and args[0] == "rev-parse" and args[1] == "HEAD":
                call_count += 1
                if call_count == 3:
                    raise _sp.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=15)
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # The merge succeeds: the post-condition checks verified the merge
        # commit independently of the timed-out SHA read.
        assert report.success is True
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert report.rollback_failed is False
        assert report.pending_human is False
        assert report.human_call_file is None
        assert "feature" in report.merged_branches

        # The timeout must still be visible to an operator in the merge log.
        assert report.log_file is not None
        log_text = report.log_file.read_text()
        assert "git rev-parse HEAD timed out after clean merge of 'feature'" in log_text

    def test_merge_report_defaults(self) -> None:
        report = MergeReport()
        assert report.success is False
        assert report.merged_branches == []
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert report.pending_human is False

    def test_conflict_human_call_print_instructions_failure_preserved(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If write_call succeeds but print_instructions raises in the HUMAN_CALL
        branch of _handle_conflict, report.failure_reason must be 'pending_human',
        and the call file on disk must still exist.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: requires_human_review flag -> triggers HUMAN_CALL
        # (Confidence rating is now informational under the LLM-as-editor
        # model; the safe decider gates on explicit flags only.)
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": True},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": True},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Monkeypatch print_instructions to simulate broken pipe
        def mock_print_instructions(self, call_file):
            raise BrokenPipeError("mock broken pipe")

        monkeypatch.setattr(
            "tianluo.engine.merge.human_call.HumanCallWriter.print_instructions",
            mock_print_instructions,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.pending_human is True
        assert report.failure_reason == "pending_human"
        assert report.failed_branch == "feature"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_merge_timeout_default_strategy(self, tmp_path: Path, monkeypatch) -> None:
        """git merge raises TimeoutExpired -> abort, failure_reason=merge_timed_out."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        import subprocess as _sp

        def patch_run_git(project_root, *args, check=True, timeout=30):
            # Only match the initial merge (not merge --abort)
            if len(args) >= 1 and args[0] == "merge" and "--abort" not in args:
                raise _sp.TimeoutExpired(cmd=["git", "merge"], timeout=30)
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )
        # Mock _abort_merge to succeed — real git has no merge in progress
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_timed_out"
        assert report.pending_human is False

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_merge_timeout_fast_strategy(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + git merge timeout -> fast_abort with merge_timed_out."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        import subprocess as _sp

        def patch_run_git(project_root, *args, check=True, timeout=30):
            # Only match the initial merge (not merge --abort)
            if len(args) >= 1 and args[0] == "merge" and "--abort" not in args:
                raise _sp.TimeoutExpired(cmd=["git", "merge"], timeout=30)
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )
        # Mock _abort_merge to succeed — real git has no merge in progress
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_timed_out"
        assert report.pending_human is False

        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_non_conflict_merge_failure_surfaces_stderr(self, tmp_path: Path, monkeypatch) -> None:
        """git merge returns non-zero without conflicts -> stderr logged, failure_reason set."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        def patch_run_git(project_root, *args, check=True, timeout=30):
            # Only match the initial merge (not merge --abort)
            if len(args) >= 1 and args[0] == "merge" and "--abort" not in args:
                class FakeResult:
                    returncode = 1
                    stdout = ""
                    stderr = "fatal: refusing to merge unrelated histories"
                return FakeResult()
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )
        # Mock _abort_merge to succeed — real git has no merge in progress
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        # failure_reason should include the stderr message
        assert "refusing to merge unrelated histories" in report.failure_reason
        assert report.pending_human is False

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Log file should contain the stderr
        assert report.log_file is not None
        log_text = report.log_file.read_text()
        assert "refusing to merge unrelated histories" in log_text

    def test_fast_non_conflict_merge_failure_surfaces_stderr(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy: git merge returns non-zero without conflicts -> stderr
        is preserved in failure_reason with 'fast_failure:' prefix (distinct from
        conflict-resolution 'fast_abort')."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        def patch_run_git(project_root, *args, check=True, timeout=30):
            if len(args) >= 1 and args[0] == "merge" and "--abort" not in args:
                class FakeResult:
                    returncode = 1
                    stdout = ""
                    stderr = "fatal: refusing to merge unrelated histories"
                return FakeResult()
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git,
        )
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        # failure_reason must use 'fast_failure:' prefix, not 'fast_abort:'
        assert report.failure_reason.startswith("fast_failure")
        assert "refusing to merge unrelated histories" in report.failure_reason
        assert report.pending_human is False

    def test_version_bump_postcondition_failure_flips_success_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When assert_version_bumped fails after aggregation, report.success
        must be False and failure_reason must reflect the post-condition
        violation (regression test for the critical success-flag wiring bug).
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "add pyproject")

        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock aggregate_and_apply to report success but leave the version
        # file untouched. Then the post-condition check will fail.
        original_aggregate = (
            "tianluo.engine.merge.orchestrator.aggregate_and_apply"
        )

        def mock_aggregate(project_root, branch_bumps, pre_version, amend=True):
            from dataclasses import dataclass
            from tianluo.engine.version_bumper import BumpType

            @dataclass
            class FakeAgg:
                success: bool = True
                new_version: str = "1.1.0"
                bump_type: BumpType = BumpType.MINOR
                error: str = ""

            return FakeAgg()

        monkeypatch.setattr(original_aggregate, mock_aggregate)

        # Mock assert_version_bumped to raise PostConditionViolated.
        # The function is imported locally inside the orchestrator, so we
        # patch the source module and rely on the local import resolving
        # to the patched object at test time.
        def mock_assert_version_bumped(project_root, expected_version):
            from tianluo.commands.merge.failure_reason import FailureReason
            from tianluo.commands.merge.postcondition import PostConditionViolated
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=f"expected {expected_version} but found 1.0.0",
            )

        # G3 fix: patch the orchestrator's bound reference (top-level
        # import) rather than the postcondition module's symbol.
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.assert_version_bumped",
            mock_assert_version_bumped,
        )

        # Ensure bump inference returns a value so aggregation runs.
        from tianluo.engine.version_bumper import BumpType

        def mock_infer_branch_bump(project_root, branch, merge_base_sha):
            from dataclasses import dataclass

            @dataclass
            class FakeInfer:
                bump: BumpType | None = BumpType.MINOR
                reason: str = "mocked for test"

            return FakeInfer()

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.infer_branch_bump",
            mock_infer_branch_bump,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        # The merge itself succeeded; the post-condition failed.
        assert report.success is False, (
            f"report.success must be False when version post-condition fails, "
            f"got {report.success}"
        )
        assert report.merged_branches == ["feature"]
        assert report.failed_branch is None  # git merge did not fail
        assert report.version_aggregation_error is not None
        assert "1.1.0" in report.version_aggregation_error
        # failure_reason must be the typed post-condition reason
        assert report.failure_reason == "postcond_version_not_bumped"

    def test_version_aggregation_runs_despite_runtime_sync_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """B12: a successful git merge must still get its version bump even
        when later runtime sync fails."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "add pyproject")

        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock aggregate_and_apply to verify it was called
        aggregate_called = False

        def mock_aggregate(project_root, branch_bumps, pre_version, amend=True):
            nonlocal aggregate_called
            aggregate_called = True
            from dataclasses import dataclass
            from tianluo.engine.version_bumper import BumpType

            @dataclass
            class FakeAgg:
                success: bool = True
                new_version: str = "1.1.0"
                bump_type: BumpType = BumpType.MINOR
                error: str = ""

            return FakeAgg()

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.aggregate_and_apply",
            mock_aggregate,
        )

        # Mock assert_version_bumped so the post-condition passes.
        # G3 fix: orchestrator now imports at module top, so we patch
        # the orchestrator's bound reference rather than the
        # postcondition module's symbol (which the orchestrator captured
        # at module load time and no longer re-resolves on each call).
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.assert_version_bumped",
            lambda project_root, expected_version: None,
        )

        # Ensure bump inference returns a value so aggregation would run.
        from tianluo.engine.version_bumper import BumpType

        def mock_infer_branch_bump(project_root, branch, merge_base_sha):
            from dataclasses import dataclass

            @dataclass
            class FakeInfer:
                bump: BumpType | None = BumpType.MINOR
                reason: str = "mocked for test"

            return FakeInfer()

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.infer_branch_bump",
            mock_infer_branch_bump,
        )

        # Mock _sync_runtime to return a collision after the git merge succeeds.
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._sync_runtime",
            lambda self, branch, report: "runtime_sync_collision",
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        # Runtime sync should have reported a collision
        assert report.failed_branch == "feature"
        assert report.failure_reason == "runtime_sync_collision"
        # B12 fix: version aggregation must still have run
        assert aggregate_called is True, (
            "aggregate_and_apply was NOT called despite a successful git merge — "
            "B12 regression"
        )

    def test_user_scenario_already_ancestor_plus_new_merge(
        self, tmp_path: Path,
    ) -> None:
        """E2E regression for the user-reported scenario.

        ``se3 merge scn discoverbug`` where:
        - ``scn`` is already an ancestor of HEAD (no-op for this run).
        - ``discoverbug`` requires a real merge commit.

        Pins the report bucket split so a future refactor cannot silently
        re-introduce double-counting (the original bug was the CLI
        reporting both branches as merged when only one produced a new
        commit).  Also asserts the merge commit is verifiable via
        ``git log`` afterward (the user could not see their merge in
        history) and that the version increments exactly once.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "add pyproject")

        # Branch ``scn``: merged once, then back into default so HEAD is
        # already an ancestor before the test runs.
        _create_branch(tmp_path, "scn")
        _add_commit(tmp_path, "scn.txt", "scn", "scn change")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--no-ff", "-m",
             "merge scn (pre-test)", "scn"],
            check=True, capture_output=True,
        )
        head_before_orchestrator = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Branch ``discoverbug``: not yet merged. Bumps pyproject.toml so
        # version aggregation has a real bump to apply.
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "discoverbug",
             default_branch],
            check=True, capture_output=True,
        )
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "discoverbug.txt").write_text("fix")
        _commit(tmp_path, "discoverbug change")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["scn", "discoverbug"])

        # Bucket split: scn appears only in already_ancestor_branches,
        # discoverbug only in newly_merged_branches.  This is the bug
        # the user reported: with the legacy aggregate-only model the
        # CLI rendered both as "merged" and the user could not tell
        # what actually changed.
        assert report.success is True
        assert report.already_ancestor_branches == ["scn"]
        assert report.newly_merged_branches == ["discoverbug"]
        # Aggregate matches both branches; consumers iterating
        # merged_branches should always pair with the bucket split.
        assert set(report.merged_branches) == {"scn", "discoverbug"}

        # The merge commit produced for discoverbug must be visible in
        # ``git log`` afterward — the user's primary concern was that
        # their merge was reported successful but missing from history.
        log_output = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--all", "--oneline"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert "discoverbug change" in log_output

        # HEAD must have advanced from where it was before the run
        # (a real merge commit landed for discoverbug).
        head_after = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_after != head_before_orchestrator

        # discoverbug must be an ancestor of HEAD post-run (post-condition
        # check would catch the silent merge loss, but verify directly).
        ancestor_check = subprocess.run(
            ["git", "-C", str(tmp_path), "merge-base",
             "--is-ancestor", "discoverbug", "HEAD"],
            capture_output=True, check=False,
        )
        assert ancestor_check.returncode == 0

        # Version must have bumped exactly once (discoverbug only) — scn
        # was already absorbed in the pre-test merge.
        post_version = _read_pyproject_version(tmp_path)
        assert post_version == "1.0.1"


class TestMergeOrchestratorConflictResolution:
    """Integration tests for conflict resolution via mocked LLM."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches. Returns (default_branch, feature_branch)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_default_accept_resolves_and_commits(self, tmp_path: Path, monkeypatch) -> None:
        """default strategy + high confidence → ACCEPT → write back → commit."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        call_count = 0

        def mock_resolve(self, context, strategy):
            nonlocal call_count
            call_count += 1
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        # Should succeed with one LLM call
        assert call_count == 1, f"Expected 1 LLM call, got {call_count}"
        assert report.success is True
        assert feature_branch in report.merged_branches

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

        # File should contain resolved content
        assert (tmp_path / "shared.txt").read_text() == "line1\nRESOLVED\nline3\n"

    def test_default_human_call_preserves_markers(self, tmp_path: Path, monkeypatch) -> None:
        """safe strategy + requires_human_review flag → HUMAN_CALL → conflict markers preserved.

        Under the LLM-as-editor model, the safe decider gates on
        explicit flags, not confidence rating.  The mock here sets
        ``requires_human_review=True`` to force the escalation path.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 5, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": True},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": True},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == feature_branch

        # Working tree should still have conflict markers
        assert (tmp_path / "shared.txt").exists()
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Call file should exist and be set on report
        assert report.human_call_file is not None
        calls_dir = tmp_path / "tianluo" / "calls"
        call_files = list(calls_dir.glob("merge_*.json"))
        assert len(call_files) >= 1

    def test_reject_aborts_and_restores(self, tmp_path: Path, monkeypatch) -> None:
        """Any strategy where decision is REJECT → git merge --abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="bad",
                        hunks=[HunkResolution(1, 5, Confidence.LOW, "bad")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": True},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": True},
            )

        # Force REJECT via the new ``resolve_and_decide`` entry point
        # (the orchestrator no longer routes through ``decide``).
        def mock_resolve_and_decide(self, resolver, conflict_files, context, *, max_iterations):
            from tianluo.engine.merge.strategy import DecisionAction, StrategyDecision
            return StrategyDecision(
                action=DecisionAction.REJECT,
                reason="mock reject",
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.StrategyDecider.resolve_and_decide",
            mock_resolve_and_decide,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "merge_conflict"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_strategy_conflict_markers_in_resolution_abort(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + conflict markers left in resolved_content → abort.

        Even in fast mode, resolved content that still contains git conflict
        markers must be rejected before being committed. The first-pass
        validation detects the markers and aborts the merge.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        # LLM erroneously left conflict markers in the resolution
                        resolved_content="line1\n<<<<<<< HEAD\nBASE\n=======\nFEATURE\n>>>>>>> branch\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Merge must fail — markers were detected and aborted.  Under
        # the LLM-as-editor batch path the resolver itself rejects a
        # resolution whose ``resolved_content`` still contains markers,
        # surfacing as ``resolution_rejected`` rather than the legacy
        # ``resolution_validation_failed`` (which fired one layer
        # deeper, in ``_apply_resolution``).  Either failure category
        # is acceptable so long as the merge does not commit markers.
        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason in (
            "resolution_rejected",
            "resolution_validation_failed",
        )

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # File on disk must NOT contain the marker-laden content
        content = (tmp_path / "shared.txt").read_text()
        # After abort, working tree is restored to pre-merge state (ours)
        assert "BASE" in content
        # LLM resolution with markers was NOT written
        assert "RESOLVED" not in content

    def test_strict_conflict_routes_to_human_call(self, tmp_path: Path) -> None:
        """strict strategy + any conflict → HUMAN_CALL (LLM is never invoked)."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.pending_human is True

    def test_llm_called_once_per_merge(self, tmp_path: Path, monkeypatch) -> None:
        """Verify exactly one LLM call is made per conflicting merge."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        call_count = 0

        def mock_resolve(self, context, strategy):
            nonlocal call_count
            call_count += 1
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        assert call_count == 1, f"Expected exactly 1 LLM call, got {call_count}"
        assert report.success is True

    def test_valid_llm_deletion_tracked_text_file(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns empty resolved_content for tracked text file → file deleted via git rm -f.

        To reach the deletion path the working-tree file must NOT have conflict
        markers (the safety check rejects deletion of files that still contain
        markers).  We therefore start a normal text conflict and then overwrite
        the working-tree file with marker-free content before the orchestrator
        runs, simulating a "resolve by deletion" decision.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "deleteme.txt", "delete me please\n", "Add deleteme")
        _create_branch(tmp_path, "feature")
        (tmp_path / "deleteme.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "deleteme.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on main"],
            check=True, capture_output=True,
        )

        # Start the merge so the file enters the unmerged state, then remove
        # conflict markers so the deletion path is allowed.
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # This test pre-stages an in-progress merge as a fixture shortcut, to
        # inject a specific working-tree state before conflict resolution runs.
        # The dirty pre-flight now (correctly) refuses to START a merge on top
        # of a pre-existing one — in production the orchestrator holds the lock
        # and never leaves a merge in progress, so a lingering MERGE_HEAD is an
        # anomaly to block, not to bulldoze. Bypass it here since we are
        # unit-testing the resolution path, not the pre-flight.
        monkeypatch.setattr(
            MergeOrchestrator, "_preflight_dirty_tracked_files",
            lambda self, report, branches: True,
        )
        # The merge should have produced a conflict
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr, (
            f"Expected conflict but got: {merge_result.stdout} {merge_result.stderr}"
        )
        # Remove conflict markers — simulate a human pre-resolving to "delete"
        (tmp_path / "deleteme.txt").write_text("resolved content without markers\n")

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="deleteme.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "delete it")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should succeed — file was legitimately deleted
        assert report.success is True
        assert "feature" in report.merged_branches

        # File should NOT exist anymore
        assert not (tmp_path / "deleteme.txt").exists()

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

    def test_incomplete_llm_resolution_routes_to_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """LLM omits one of two conflict files → HUMAN_CALL with report.human_call_file set."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create two files that will conflict
        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: only resolves a.txt, omits b.txt
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                    # b.txt intentionally omitted
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should fail with pending_human due to incomplete resolution
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == "feature"
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Call file should mention incomplete resolution
        data = json.loads(report.human_call_file.read_text())
        assert "incomplete" in data["decision_reason"].lower()
        assert "b.txt" in data["decision_reason"]
        # The instructions text must reference the actual call-file name
        assert report.human_call_file.name + ".response" in data["instructions"]

    def test_binary_conflict_aborts_not_deletes(self, tmp_path: Path, monkeypatch) -> None:
        """Binary file + empty LLM resolved_content → abort, do NOT delete the file."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create a file with null bytes so our heuristic treats it as binary
        binary_content = b"line1\n\x00\x01\x02\nline3\n"
        (tmp_path / "data.bin").write_bytes(binary_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Add binary file"],
            check=True, capture_output=True,
        )

        # Create feature branch with different binary content
        _create_branch(tmp_path, "feature")
        feature_content = b"line1\n\x00\xfe\xff\nfeature_line\n"
        (tmp_path / "data.bin").write_bytes(feature_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on feature"],
            check=True, capture_output=True,
        )

        # Switch back to main, change binary differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        main_content = b"line1\n\x00\xaa\xbb\nmain_line\n"
        (tmp_path / "data.bin").write_bytes(main_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: returns empty resolved_content (triggers deletion path)
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="data.bin",
                        resolved_content="",  # empty → would trigger deletion path
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "binary")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should fail — binary files cannot be auto-resolved
        assert report.success is False
        assert report.failed_branch == "feature"

        # The binary file should still exist on disk (not deleted by git rm)
        assert (tmp_path / "data.bin").exists()

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_path_validation_rejects_bogus_paths(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns path traversal → extra-files pre-check routes to HUMAN_CALL."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    # Include the legitimate conflict file …
                    FileResolution(
                        path="shared.txt",
                        resolved_content="resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                    # … and a bogus path that the extra-files pre-check catches.
                    FileResolution(
                        path="../escape.txt",
                        resolved_content="evil content",
                        hunks=[HunkResolution(1, 1, Confidence.HIGH, "bad")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        # Extra files are now caught in the pre-check and routed to HUMAN_CALL
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None

        # Bogus path must NOT have been written outside the project root
        assert not (tmp_path.parent / "escape.txt").exists()

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestAbortMergeFailureHandling:
    """Tests that when git merge --abort fails, the failure is surfaced."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches. Returns (default_branch, feature_branch)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_default_human_call_write_fails_and_abort_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """safe + write_call fails + abort fails -> failure_reason=merge_abort_failed.

        The escalation is forced by the ``requires_human_review`` flag
        (the LLM-as-editor decider gates on flags, not confidence).
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="resolved",
                        hunks=[HunkResolution(1, 5, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": True},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": True},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock write_call to fail, and _abort_merge to also fail
        monkeypatch.setattr(
            "tianluo.engine.merge.human_call.HumanCallWriter.write_call",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
        )
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: False,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.failed_branch == feature_branch
        # KEY: abort failure must be surfaced, not masked by write failure
        assert report.failure_reason == "merge_abort_failed"

        # Clean up the mid-merge state for the test runner
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--abort"],
            capture_output=True,
        )

    def test_default_incomplete_resolution_write_fails_and_abort_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """default + incomplete resolution + write_call fails + abort fails -> merge_abort_failed."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[],  # incomplete — missing shared.txt
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock write_call to fail, and _abort_merge to also fail
        monkeypatch.setattr(
            "tianluo.engine.merge.human_call.HumanCallWriter.write_call",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
        )
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: False,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "merge_abort_failed"

        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--abort"],
            capture_output=True,
        )


class TestMergeOrchestratorCleanupInteraction:
    """Tests for --delete-merged interaction with aggregation failures."""

    def test_aggregation_failure_fails_loud_and_skips_cleanup(self, tmp_path: Path, monkeypatch) -> None:
        """If aggregate_and_apply fails (write/amend error, NOT the benign
        "no bumps" path), the report fails loud (success=False) and
        cleanup is skipped so the operator can re-run after investigating.

        This is the corrected behavior after the user-incident shape:
        per-branch merges all commit, the version write silently fails,
        and the CLI used to still report success — masking the failure.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")

        # feature: clean merge with a version bump so aggregate_and_apply is invoked
        _create_branch(tmp_path, "feature")
        _write_pyproject(tmp_path, "4.4.1")
        (tmp_path / "feat.txt").write_text("feature")
        _commit(tmp_path, "Bump version on feature", "pyproject.toml", "feat.txt")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock aggregate_and_apply to fail (simulate amend failure)
        def mock_aggregate(project_root, bumps, pre_version, amend=True):
            from tianluo.engine.merge.version_aggregator import AggregateResult
            return AggregateResult(
                success=False,
                error="git commit --amend failed: mock failure",
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.aggregate_and_apply",
            mock_aggregate,
        )

        orch = MergeOrchestrator(
            project_root=tmp_path,
            strategy="safe",
            delete_merged=True,
        )
        report = orch.execute(["feature"])

        # Aggregation failure (not "no bumps") → report.success=False
        # so the operator sees the failure rather than a misleading success.
        assert report.success is False
        assert report.version_aggregation_error is not None
        assert "amend failed" in report.version_aggregation_error

        # Cleanup is skipped when report.success=False so the operator
        # can re-run after investigating the version-write failure.
        assert report.cleanup_skipped is True

        # Branch should NOT be deleted (cleanup did not run)
        branch_exists = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "feature"],
            capture_output=True, text=True, check=False,
        )
        assert branch_exists.returncode == 0

    def test_version_higher_than_target_fails_loud_and_postcondition_passes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When the on-disk version is strictly higher than the
        aggregator's computed target (anomalous state — e.g. a stale
        pre_merge_version, manual prior bump, or a branch tip whose
        pyproject.toml was already advanced), the orchestrator MUST
        mark success=False with VERSION_HIGHER_THAN_TARGET. The
        version-bump post-condition is still called against
        ``agg.new_version`` (which the aggregator resets to the
        on-disk version), so it MUST agree with the disk state and
        not spuriously override the more specific failure reason.

        This is the explicit higher-than-target sub-case of the
        version_already_at_target branch — distinct from the
        "current == target" warning case.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")

        # feature: bump pyproject version so aggregate_and_apply is invoked
        _create_branch(tmp_path, "feature")
        _write_pyproject(tmp_path, "4.4.1")
        (tmp_path / "feat.txt").write_text("feature")
        _commit(tmp_path, "Bump version on feature", "pyproject.toml", "feat.txt")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        # Now simulate the higher-than-target shape: while the merge is in
        # progress the on-disk pyproject is bumped *higher* than the
        # aggregated target.  We mock the aggregator below so the actual
        # disk read in the test is irrelevant — only the orchestrator's
        # downstream handling of the AggregateResult matters.

        # Mock aggregate to report the higher-than-target anomaly:
        # disk = 5.0.0, computed target was 4.4.1, so the aggregator
        # resets new_version to current ("5.0.0") and flags it.
        def mock_aggregate(project_root, bumps, pre_version, amend=True):
            from tianluo.engine.merge.version_aggregator import AggregateResult
            return AggregateResult(
                success=False,
                new_version="5.0.0",
                version_already_at_target=True,
                version_higher_than_target=True,
                error=(
                    "VersionNotAdvanced: current version 5.0.0 is higher "
                    "than aggregated target 4.4.1; possible manual bump "
                    "or anomalous state — aggregator did not run"
                ),
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.aggregate_and_apply",
            mock_aggregate,
        )

        orch = MergeOrchestrator(
            project_root=tmp_path,
            strategy="safe",
        )
        report = orch.execute(["feature"])

        # The higher-than-target anomaly is the *primary* failure
        # reason. The post-condition check is exercised against
        # agg.new_version (5.0.0) which matches the disk, so it does
        # NOT override with a POSTCOND_VERSION_NOT_BUMPED label.
        assert report.success is False
        assert report.version_higher_than_target is True
        assert report.failure_reason == "version_higher_than_target"
        assert report.final_version == "5.0.0"

    def test_delete_merged_with_dirty_worktree_skips_and_reports_success(self, tmp_path: Path) -> None:
        """Successful merge + delete_merged=True with dirty worktree → success=True, skipped_dirty populated."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create feature branch with a tracked file
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Create worktree for feature branch
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)

        # Dirty the worktree by modifying a tracked file
        (wt_dir / "feat.txt").write_text("modified uncommitted")

        # Run merge with delete_merged=True
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=True)
        report = orch.execute(["feature"])

        # Merge itself should succeed
        assert report.success is True
        assert report.merged_branches == ["feature"]

        # Cleanup should have run but skipped due to dirty worktree
        assert report.cleanup_skipped is False
        assert report.cleanup_report is not None
        assert len(report.cleanup_report.skipped_dirty) == 1
        assert report.cleanup_report.skipped_dirty[0][0] == "feature"

        # Branch should still exist (not deleted because worktree is dirty)
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "feature"],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0

        # Worktree should still exist
        assert wt_dir.exists()

    def test_binary_conflict_non_empty_resolution_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """Binary file conflict + LLM returns non-empty content → abort via is_binary check.

        The first-pass validation in ``_apply_resolution`` detects that the
        conflict file is binary and aborts before writing anything back.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create a file with null bytes so our heuristic treats it as binary
        binary_content = b"line1\n\x00\x01\x02\nline3\n"
        (tmp_path / "data.bin").write_bytes(binary_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Add binary file"],
            check=True, capture_output=True,
        )

        # Create feature branch with different binary content
        _create_branch(tmp_path, "feature")
        feature_content = b"line1\n\x00\xfe\xff\nfeature_line\n"
        (tmp_path / "data.bin").write_bytes(feature_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on feature"],
            check=True, capture_output=True,
        )

        # Switch back to main, change binary differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        main_content = b"line1\n\x00\xaa\xbb\nmain_line\n"
        (tmp_path / "data.bin").write_bytes(main_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: returns NON-empty resolved_content for binary file
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="data.bin",
                        # Non-empty content for a binary file — should be rejected
                        resolved_content="some text that claims to resolve binary",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "binary")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should fail — binary files cannot be auto-resolved
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "binary_file_conflict"

        # A human call must have been created for non-fast strategies
        assert report.pending_human is True
        assert report.human_call_file is not None

        # The binary file should still have the main content (not overwritten by LLM)
        assert (tmp_path / "data.bin").read_bytes() == main_content

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_git_rm_failure_in_apply_resolution_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """Second-pass ``git rm -f`` failure in _apply_resolution → abort merge.

        When LLM returns empty resolved_content for a text file (deletion),
        the second pass calls ``git rm -f``. If that fails, the merge must be
        aborted and ``resolution_write_failed`` returned.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "deleteme.txt", "delete me please\n", "Add deleteme")
        _create_branch(tmp_path, "feature")
        (tmp_path / "deleteme.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "deleteme.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on main"],
            check=True, capture_output=True,
        )

        # Start the merge so the file enters the unmerged state, then remove
        # conflict markers so the deletion path is allowed.
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # This test pre-stages an in-progress merge as a fixture shortcut, to
        # inject a specific working-tree state before conflict resolution runs.
        # The dirty pre-flight now (correctly) refuses to START a merge on top
        # of a pre-existing one — in production the orchestrator holds the lock
        # and never leaves a merge in progress, so a lingering MERGE_HEAD is an
        # anomaly to block, not to bulldoze. Bypass it here since we are
        # unit-testing the resolution path, not the pre-flight.
        monkeypatch.setattr(
            MergeOrchestrator, "_preflight_dirty_tracked_files",
            lambda self, report, branches: True,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr, (
            f"Expected conflict but got: {merge_result.stdout} {merge_result.stderr}"
        )
        # Remove conflict markers — simulate a human pre-resolving to "delete"
        (tmp_path / "deleteme.txt").write_text("resolved content without markers\n")

        # Mock LLM resolver: returns empty resolved_content → deletion path
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="deleteme.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "delete it")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Track whether abort was called
        abort_calls = []
        original_abort = MergeOrchestrator._abort_merge

        def tracked_abort(self):
            abort_calls.append(True)
            return original_abort(self)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge", tracked_abort
        )

        # Monkeypatch _run_git to fail the "rm -f" call
        original_run_git = None

        def patch_run_git(project_root, *args, check=True, timeout=30):
            if len(args) >= 2 and args[0] == "rm" and args[1] == "-f":
                class FakeResult:
                    returncode = 1
                    stdout = ""
                    stderr = "mock rm failure"
                return FakeResult()
            # Fall through to real _run_git for everything else
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should fail — git rm -f failed
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "resolution_write_failed"

        # Abort must have been called
        assert len(abort_calls) >= 1

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_rename_conflict_absent_working_tree_path_deletes_via_git_rm(self, tmp_path: Path, monkeypatch) -> None:
        """Rename/delete conflict where the path is absent from working tree but
        has unmerged index entries — empty resolved_content triggers
        ``git rm -f --ignore-unmatch`` and the merge succeeds.

        Regression test for the branch in _apply_resolution lines 661-667.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start the merge so the file enters the unmerged state
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # This test pre-stages an in-progress merge as a fixture shortcut, to
        # inject a specific working-tree state before conflict resolution runs.
        # The dirty pre-flight now (correctly) refuses to START a merge on top
        # of a pre-existing one — in production the orchestrator holds the lock
        # and never leaves a merge in progress, so a lingering MERGE_HEAD is an
        # anomaly to block, not to bulldoze. Bypass it here since we are
        # unit-testing the resolution path, not the pre-flight.
        monkeypatch.setattr(
            MergeOrchestrator, "_preflight_dirty_tracked_files",
            lambda self, report, branches: True,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr, (
            f"Expected conflict but got: {merge_result.stdout} {merge_result.stderr}"
        )

        # Remove the file from the working tree while preserving unmerged index entries.
        # This simulates a rename/delete conflict where the original path is absent
        # from the working tree but still present in the index with stages 1/2/3.
        (tmp_path / "shared.txt").unlink()

        # Verify the index still lists the file as unmerged
        diff_result = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, check=True,
        )
        assert "shared.txt" in diff_result.stdout

        # Verify the working-tree path is absent
        assert not (tmp_path / "shared.txt").exists()

        # Mock LLM resolver: returns empty resolved_content -> deletion path
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty -> deletion
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "delete it")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should succeed — git rm -f --ignore-unmatch handled the absent path
        assert report.success is True
        assert "feature" in report.merged_branches

        # File should NOT exist anymore
        assert not (tmp_path / "shared.txt").exists()

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

    def test_write_call_failure_aborts_merge(self, tmp_path: Path, monkeypatch) -> None:
        """If HumanCallWriter.write_call raises, the merge must abort and
        return 'human_call_write_failed' (not 'pending_human') so the user
        knows manual recovery is required."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: low confidence -> strategy returns HUMAN_CALL
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock HumanCallWriter.write_call to raise
        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Must abort, not return pending_human without a call file
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "human_call_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_deletion_low_confidence_rejected_for_modify_delete(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns empty resolved_content with LOW confidence for a file that
        has non-empty content in ours/theirs -> deletion is rejected and merge
        aborts."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start merge, remove conflict markers to reach deletion validation
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # This test pre-stages an in-progress merge as a fixture shortcut, to
        # inject a specific working-tree state before conflict resolution runs.
        # The dirty pre-flight now (correctly) refuses to START a merge on top
        # of a pre-existing one — in production the orchestrator holds the lock
        # and never leaves a merge in progress, so a lingering MERGE_HEAD is an
        # anomaly to block, not to bulldoze. Bypass it here since we are
        # unit-testing the resolution path, not the pre-flight.
        monkeypatch.setattr(
            MergeOrchestrator, "_preflight_dirty_tracked_files",
            lambda self, report, branches: True,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr
        (tmp_path / "shared.txt").write_text("resolved content without markers\n")

        # Mock LLM resolver: HIGH overall confidence (strategy accepts) but
        # LOW hunk confidence — the deletion gate should reject because not all
        # hunks have HIGH confidence for a file with content in ours/theirs.
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty -> deletion
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "maybe delete")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Should fail — low-confidence deletion of a file with content rejected
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "resolution_validation_failed"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_deletion_empty_hunks_high_confidence_accepted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Empty hunks + overall_confidence=HIGH → deletion accepted."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start merge, remove conflict markers to reach deletion validation
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # This test pre-stages an in-progress merge as a fixture shortcut, to
        # inject a specific working-tree state before conflict resolution runs.
        # The dirty pre-flight now (correctly) refuses to START a merge on top
        # of a pre-existing one — in production the orchestrator holds the lock
        # and never leaves a merge in progress, so a lingering MERGE_HEAD is an
        # anomaly to block, not to bulldoze. Bypass it here since we are
        # unit-testing the resolution path, not the pre-flight.
        monkeypatch.setattr(
            MergeOrchestrator, "_preflight_dirty_tracked_files",
            lambda self, report, branches: True,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr
        (tmp_path / "shared.txt").write_text("resolved content without markers\n")

        # Mock LLM: empty hunks list, HIGH overall confidence → should accept deletion
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[],  # no hunks → fall back to overall_confidence
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Should succeed — empty hunks + HIGH overall → accepted
        assert report.success is True
        assert "feature" in report.merged_branches
        assert not (tmp_path / "shared.txt").exists()
        assert _is_working_tree_clean(tmp_path) is True

    def test_deletion_empty_hunks_low_confidence_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Empty hunks + overall_confidence=LOW → deletion rejected (not HIGH)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start merge, remove conflict markers to reach deletion validation
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # This test pre-stages an in-progress merge as a fixture shortcut, to
        # inject a specific working-tree state before conflict resolution runs.
        # The dirty pre-flight now (correctly) refuses to START a merge on top
        # of a pre-existing one — in production the orchestrator holds the lock
        # and never leaves a merge in progress, so a lingering MERGE_HEAD is an
        # anomaly to block, not to bulldoze. Bypass it here since we are
        # unit-testing the resolution path, not the pre-flight.
        monkeypatch.setattr(
            MergeOrchestrator, "_preflight_dirty_tracked_files",
            lambda self, report, branches: True,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr
        (tmp_path / "shared.txt").write_text("resolved content without markers\n")

        # Mock LLM: empty hunks list, LOW overall confidence → should reject deletion
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[],  # no hunks → fall back to overall_confidence
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Should fail — empty hunks + LOW overall → validation rejects deletion
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "resolution_validation_failed"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestStrictShortCircuit:
    """Tests for strict strategy short-circuit: skip LLM, direct human call."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_strict_skips_llm_direct_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """strict mode must NOT call LLM; instead it creates a human call file."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        llm_call_count = 0

        def mock_resolve(self, context, strategy):
            nonlocal llm_call_count
            llm_call_count += 1
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # LLM should NOT have been called
        assert llm_call_count == 0, f"Expected 0 LLM calls in strict mode, got {llm_call_count}"

        # Should be pending_human
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "pending_human"

        # Call file should exist
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Working tree should still have conflict markers (not aborted)
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Log should mention strict short-circuit
        assert orch.log_file is not None
        log_text = orch.log_file.read_text()
        assert "skipping LLM resolution" in log_text.lower() or "strict" in log_text.lower()

    def test_strict_call_file_contains_placeholder_resolution(self, tmp_path: Path, monkeypatch) -> None:
        """strict mode call file must contain placeholder resolution data."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        assert report.human_call_file is not None
        call_data = json.loads(report.human_call_file.read_text())

        # Should be merge_conflict type
        assert call_data["type"] == "merge_conflict"

        # Should have file entries with working tree content as resolved_content
        assert len(call_data["files"]) == 1
        file_entry = call_data["files"][0]
        assert file_entry["path"] == "shared.txt"

        # LLM resolution should have LOW confidence (placeholder)
        assert call_data["llm_overall_confidence"] == "low"

        # Decision reason should mention strict
        assert "strict" in call_data["decision_reason"].lower()

        # Strategy marker so consumers know the proposal is a placeholder
        assert call_data.get("strategy") == "strict"

    def test_strict_write_call_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """If write_call raises in strict mode, the merge must abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # Must abort
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "human_call_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_strict_build_context_raises_routes_to_conflict(self, tmp_path: Path, monkeypatch) -> None:
        """strict + build_conflict_context raises -> write degraded call, pending_human.

        Strict's contract is 'any conflict escalates directly to human call'.
        When context building fails, a degraded call file (type
        ``merge_context_unavailable``) is written so the user still has a
        call file to respond to.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        # Mock build_conflict_context to raise
        def mock_build_context(project_root, ours, theirs, **kwargs):
            raise RuntimeError("mock context failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.build_conflict_context",
            mock_build_context,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # Strict contract: must escalate to human call even when context build fails
        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_default_build_context_raises_routes_to_conflict(self, tmp_path: Path, monkeypatch) -> None:
        """default + build_conflict_context raises -> abort, reported as conflict_context_failed."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_build_context(project_root, ours, theirs, **kwargs):
            raise RuntimeError("mock context failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.build_conflict_context",
            mock_build_context,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "conflict_context_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

        assert _is_working_tree_clean(tmp_path) is True

        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_build_context_raises_routes_to_fast_abort(self, tmp_path: Path, monkeypatch) -> None:
        """fast + build_conflict_context raises -> abort, reported as conflict_context_failed.

        Mirrors the strict/default tests and verifies fast mode returns fast_abort
        (not merge_conflict) and creates no human call file.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_build_context(project_root, ours, theirs, **kwargs):
            raise RuntimeError("mock context failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.build_conflict_context",
            mock_build_context,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "conflict_context_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

        assert _is_working_tree_clean(tmp_path) is True

        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_strict_skips_llm_and_writes_call(self, tmp_path: Path, monkeypatch) -> None:
        """strict mode: LLMCaller.call never invoked; human call file created."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        llm_call_count = 0

        def mock_llm_call(self, prompt, require_json=False):
            nonlocal llm_call_count
            llm_call_count += 1
            return ""

        monkeypatch.setattr(
            "tianluo.engine.llm_caller.LLMCaller.call",
            mock_llm_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # LLM should NOT have been called at all
        assert llm_call_count == 0, (
            f"Expected 0 LLM calls in strict mode, got {llm_call_count}"
        )

        # Should be pending_human with call file
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestFastAbortBehavior:
    """Tests for fast mode: human-escalation paths become abort+fail."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_fast_human_call_decision_aborts_no_call_file(self, tmp_path: Path, monkeypatch) -> None:
        """fast + REJECT decision -> abort, no call file, no pending_human."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": True},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": True},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        calls_before = list((tmp_path / "tianluo" / "calls").glob("merge_*.json")) if (tmp_path / "tianluo" / "calls").exists() else []

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must fail with resolution_rejected, NOT pending_human
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "resolution_rejected"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # No new call files should have been created
        calls_after = list((tmp_path / "tianluo" / "calls").glob("merge_*.json")) if (tmp_path / "tianluo" / "calls").exists() else []
        assert len(calls_after) == len(calls_before)

    def test_fast_per_file_requires_human_review_accepted(self, tmp_path: Path, monkeypatch) -> None:
        """fast + per-file requires_human_review flag → ACCEPT (merge succeeds).

        Only a *global* requires_human_review flag REJECTs under the fast
        strategy; per-file flags are accepted with a warning log.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": True},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        calls_before = list((tmp_path / "tianluo" / "calls").glob("merge_*.json")) if (tmp_path / "tianluo" / "calls").exists() else []

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must succeed — a per-file requires_human_review flag is dropped in fast mode
        assert report.success is True
        assert report.pending_human is False
        assert feature_branch in report.merged_branches
        assert report.human_call_file is None

        # File should be resolved
        assert (tmp_path / "shared.txt").read_text() == "line1\nRESOLVED\nline3\n"

        # No new call files should have been created
        calls_after = list((tmp_path / "tianluo" / "calls").glob("merge_*.json")) if (tmp_path / "tianluo" / "calls").exists() else []
        assert len(calls_after) == len(calls_before)

    def test_fast_incomplete_resolution_aborts_no_call_file(self, tmp_path: Path, monkeypatch) -> None:
        """fast + incomplete LLM resolution -> abort, no call file."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        # Mock LLM: only resolves a.txt, omits b.txt
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail with incomplete_resolution
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "incomplete_resolution"
        assert report.human_call_file is None

        # Working tree clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_resolution_commit_timeout_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast + commit timeout after resolution -> fast_abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Monkeypatch _run_git to make "commit" time out
        import subprocess as _sp

        def patch_run_git(project_root, *args, check=True, timeout=30):
            if len(args) >= 1 and args[0] == "commit":
                raise _sp.TimeoutExpired(cmd=["git", "commit"], timeout=30)
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patch_run_git
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must fail with fast_abort result code, but failure_reason is resolution_commit_timeout
        # the actual cause (commit timeout) so the CLI message is accurate.
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "resolution_commit_timeout"

        # Working tree clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_apply_resolution_validation_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast + validation failure in _apply_resolution -> resolution_validation_failed."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        # Contains conflict markers -> validation failure
                        resolved_content="line1\n<<<<<<< HEAD\nBASE\n=======\nFEATURE\n>>>>>>> branch\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must fail.  Under the LLM-as-editor batch path the resolver
        # rejects an LLMResolution whose ``resolved_content`` still
        # contains conflict markers and surfaces this as
        # ``resolution_rejected`` rather than the legacy
        # ``resolution_validation_failed`` produced one layer deeper.
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason in (
            "resolution_rejected",
            "resolution_validation_failed",
        )

        # Working tree clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_binary_file_conflict_aborts_with_specific_reason(self, tmp_path: Path, monkeypatch) -> None:
        """fast + binary file conflict -> abort with binary_file_conflict_fast_abort reason.

        The first-pass validation in ``_apply_resolution`` detects that the
        conflict file is binary and aborts before writing anything back.
        In fast mode the dedicated ``binary_file_conflict_fast_abort`` failure
        reason is used so the CLI can surface a strategy-appropriate message
        that does not promise human review (fast mode never calls human).
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create a file with null bytes so our heuristic treats it as binary
        binary_content = b"line1\n\x00\x01\x02\nline3\n"
        (tmp_path / "data.bin").write_bytes(binary_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Add binary file"],
            check=True, capture_output=True,
        )

        # Create feature branch with different binary content
        _create_branch(tmp_path, "feature")
        feature_content = b"line1\n\x00\xfe\xff\nfeature_line\n"
        (tmp_path / "data.bin").write_bytes(feature_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on feature"],
            check=True, capture_output=True,
        )

        # Switch back to main, change binary differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        main_content = b"line1\n\x00\xaa\xbb\nmain_line\n"
        (tmp_path / "data.bin").write_bytes(main_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: returns non-empty resolved_content for binary file
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="data.bin",
                        resolved_content="some text that claims to resolve binary",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "binary")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail with the fast-specific binary_file_conflict_fast_abort reason
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "binary_file_conflict_fast_abort"
        assert report.pending_human is False

        # The binary file should still have the main content (not overwritten by LLM)
        assert (tmp_path / "data.bin").read_bytes() == main_content

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_default_incomplete_resolution_still_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """default + incomplete resolution -> still writes human call (not fast_abort)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # default mode should still create human call
        assert report.success is False
        assert report.pending_human is True
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

    def test_fast_incomplete_llm_resolution_aborts_no_call(self, tmp_path: Path, monkeypatch) -> None:
        """fast + incomplete LLM resolution -> abort, no call file created."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        # Mock LLM: only resolves a.txt, omits b.txt
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        calls_before = list((tmp_path / "tianluo" / "calls").glob("merge_*.json")) if (tmp_path / "tianluo" / "calls").exists() else []

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail with incomplete_resolution
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "incomplete_resolution"
        assert report.human_call_file is None

        # Working tree clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # No new call files
        calls_after = list((tmp_path / "tianluo" / "calls").glob("merge_*.json")) if (tmp_path / "tianluo" / "calls").exists() else []
        assert len(calls_after) == len(calls_before)


class TestConflictEscalationFailurePaths:
    """Tests for conflict escalation and call-file failure handling."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches. Returns (default_branch, feature_branch)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_fast_apply_resolution_sha_read_timeout_still_succeeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """fast + conflict resolved + post-commit SHA read times out
        -> merge still succeeds (the SHA read is best-effort logging)."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Patch _run_git to timeout on the post-commit SHA read that
        # _apply_resolution performs purely to log the merge commit.
        import tianluo.engine.worktree as _wt
        rev_parse_count = 0
        original_run_git = _wt._run_git

        def patched_run_git(project_root, *args, check=True, timeout=30):
            nonlocal rev_parse_count
            if len(args) >= 2 and args[0] == "rev-parse" and args[1] == "HEAD":
                rev_parse_count += 1
                # Count: 1=execute pre-merge, 2=_merge_single_branch pre,
                # 3=_apply_resolution post-commit SHA read (logging only)
                if rev_parse_count == 3:
                    raise subprocess.TimeoutExpired(
                        cmd=["git", "rev-parse", "HEAD"], timeout=15,
                    )
            return original_run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", patched_run_git,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Merge should still succeed despite the SHA read timing out
        assert report.success is True, (
            f"Expected success, got failure_reason={report.failure_reason}"
        )
        assert feature_branch in report.merged_branches
        assert report.pending_human is False
        # Verify the conflict was actually resolved
        assert (tmp_path / "shared.txt").read_text() == "line1\nRESOLVED\nline3\n"

    def test_strict_context_build_failure_writes_degraded_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """strict + build_conflict_context fails -
        write degraded human call and return pending_human.

        Strict's contract is 'any conflict escalates directly to human call'.
        When context build fails, a degraded call file (type
        ``merge_context_unavailable``) is written so the user still has a
        call file to respond to.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock build_conflict_context to raise
        def mock_build_context(*args, **kwargs):
            raise RuntimeError("mock context build failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.build_conflict_context",
            mock_build_context,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute(["feature"])

        # Strict contract: must escalate to human call even when context build fails
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == "feature"
        assert report.failure_reason == "conflict_context_failed"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()
        # Degraded call file: minimal payload carrying the failure message
        call_data = json.loads(report.human_call_file.read_text())
        assert call_data["type"] == DEGRADED_CALL_TYPE
        assert call_data["branch"] == "feature"
        assert "mock context build failure" in call_data["message"]
        assert set(call_data["options"]) == {"accept", "abort", "manual"}

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True
        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_llm_failure_then_human_call_write_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """LLM resolve raises + write_call also raises -
        abort_merge fallback runs, failure_reason='human_call_write_failed',
        pending_human stays False.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver to raise
        from tianluo.engine.llm_caller import LLMCallError
        def mock_resolve(self, context, strategy):
            raise LLMCallError("mock LLM failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock HumanCallWriter.write_call to raise
        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Must abort, not return pending_human without a call file
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "human_call_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True
        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_human_call_decision_then_write_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Decider returns HUMAN_CALL + write_call raises -
        abort_merge fallback runs, failure_reason='human_call_write_failed',
        pending_human stays False.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: low confidence -
        # strategy decider will return HUMAN_CALL
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock HumanCallWriter.write_call to raise
        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Must abort, not return pending_human without a call file
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "human_call_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True
        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_human_call_decision_then_write_failure_and_abort_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Decider returns HUMAN_CALL + write_call raises + abort_merge also fails -
        failure_reason cascades to 'merge_abort_failed' overriding
        'human_call_write_failed'.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: low confidence -
        # strategy decider will return HUMAN_CALL
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": False},
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock HumanCallWriter.write_call to raise
        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        # Mock _abort_merge to always return False
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: False,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # abort_merge failed overrides human_call_write_failed
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_abort_failed"
        assert report.human_call_file is None

    def test_llm_failure_then_write_failure_and_abort_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """LLM resolve raises + write_call also raises + abort_merge also fails -
        failure_reason cascades to 'merge_abort_failed' overriding
        'human_call_write_failed'.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver to raise
        from tianluo.engine.llm_caller import LLMCallError
        def mock_resolve(self, context, strategy):
            raise LLMCallError("mock LLM failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock HumanCallWriter.write_call to raise
        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        # Mock _abort_merge to always return False
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._abort_merge",
            lambda self: False,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # abort_merge failed overrides human_call_write_failed
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_abort_failed"
        assert report.human_call_file is None

    def test_strict_build_context_failure_write_degraded_call_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """strict + build_conflict_context fails + write_degraded_call also fails -
        failure_reason is 'conflict_context_failed_call_file_write_failed'.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock build_conflict_context to raise
        def mock_build_context(*args, **kwargs):
            raise RuntimeError("mock context build failure")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.build_conflict_context",
            mock_build_context,
        )

        # Mock HumanCallWriter.write_degraded_call to raise
        def mock_write_degraded_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.HumanCallWriter.write_degraded_call",
            mock_write_degraded_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute(["feature"])

        # Strict mode: context build fails, degraded write also fails
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "conflict_context_failed_call_file_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True
        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestRuntimeSyncIntegration:
    """Integration tests for runtime sync in merge orchestrator."""

    def test_runtime_sync_copies_tier_a_after_merge(self, tmp_path: Path) -> None:
        """Clean merge with bound worktree copies tier A runtime files."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create feature branch, commit a non-se3 file
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")

        # Checkout back to default so feature branch is free for worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Create a bound worktree for the feature branch
        wt_dir = tmp_path / ".." / "feature-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature"],
            check=True, capture_output=True,
        )

        # Add UNCOMMITTED tier A runtime files to the worktree's tianluo/
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("wt log")
        (wt_se3 / "logs").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "logs" / "app.log").write_text("wt app log")
        (wt_se3 / "state").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "state" / "summary-abc.md").write_text("wt summary")

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]

        # Tier A files from the worktree should be copied into target tianluo/
        target_se3 = tmp_path / "tianluo"
        assert (target_se3 / "history" / "flow1.log").exists()
        assert (target_se3 / "history" / "flow1.log").read_text() == "wt log"
        assert (target_se3 / "logs" / "app.log").exists()
        assert (target_se3 / "logs" / "app.log").read_text() == "wt app log"
        assert (target_se3 / "state" / "summary-abc.md").exists()
        assert (target_se3 / "state" / "summary-abc.md").read_text() == "wt summary"

        # Cleanup worktree (force because tianluo/ files are gitignored but present)
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_collision_stops_merge(self, tmp_path: Path) -> None:
        """Tier A collision stops the merge sequence, preserving earlier merges."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will have a collision
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Set up target tianluo/ with an UNCOMMITTED file that will collide
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Create bound worktree for feature-b with a colliding file
        wt_dir = tmp_path / ".." / "feature-b-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-b log")

        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(["feature-a", "feature-b"])

        # feature-a merged successfully, feature-b collided
        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_collision"

        # feature-a's file should exist
        assert (tmp_path / "a.txt").exists()

        # Merge commit is on HEAD; branch IS recorded as merged so the
        # report matches git state. On retry, the already-merged path will
        # re-run runtime sync.
        assert (tmp_path / "b.txt").exists()
        assert "feature-b" in report.merged_branches

        # The colliding file in target should remain unchanged
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"

        # Cleanup worktree (force because tianluo/ files are gitignored but present)
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_collision_report_shape_and_log(self, tmp_path: Path) -> None:
        """Tier-A collision from inside the orchestrator: verify MergeReport shape.

        Creates two branches with bound worktrees; the first merges cleanly and
        copies its tier-A file into the target tianluo/. The second branch's merge
        succeeds git-wise but runtime_sync collides because the target now has
        the first branch's file. Verifies:
        - failure_reason='runtime_sync_collision'
        - branch recorded in merged_branches (git merge succeeded)
        - version_aggregation_skipped=True (failed_branch gates aggregation)
        - log contains retry warning
        - merge commit is on HEAD
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Set up pyproject.toml so version aggregation is attempted
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "M0: v1.0.0")

        # feature-a: clean merge with worktree containing tier-A file
        _create_branch(tmp_path, "feature-a")
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will collide at runtime_sync (do NOT change pyproject.toml
        # so the merge is clean and runtime_sync actually runs)
        _create_branch(tmp_path, "feature-b")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Create bound worktree for feature-a with tier-A file
        wt_a = (tmp_path / ".." / "feature-a-wt").resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_a), "feature-a"],
            check=True, capture_output=True,
        )
        (wt_a / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_a / "tianluo" / "history" / "h1.md").write_text("feature-a history")

        # Create bound worktree for feature-b with SAME tier-A file (collision)
        wt_b = (tmp_path / ".." / "feature-b-wt").resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_b), "feature-b"],
            check=True, capture_output=True,
        )
        (wt_b / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_b / "tianluo" / "history" / "h1.md").write_text("feature-b history")

        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(["feature-a", "feature-b"])

        # feature-a merged, feature-b collided
        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_collision"

        # The collided branch IS recorded as merged (git merge succeeded)
        assert "feature-b" in report.merged_branches

        # B12 fix: version aggregation runs even when a later branch's
        # runtime sync fails, because the earlier successful merges
        # (feature-a) still need their bumps applied.  The aggregation
        # is gated by ``branch_bumps`` (only populated for successfully
        # merged branches), not by ``report.failed_branch``.
        assert report.version_aggregation_skipped is False
        # final_version may be None when the on-disk version already
        # matches the computed target (no-op aggregation).
        assert report.effective_pre_merge_version is not None

        # Log should contain retry warning
        assert report.log_file is not None
        log_text = report.log_file.read_text()
        assert "runtime sync collision" in log_text.lower()
        assert "retry" in log_text.lower() or "again" in log_text.lower()

        # Merge commit for feature-b IS on HEAD (git merge succeeded)
        # Verify by checking the file exists and HEAD is a merge commit
        assert (tmp_path / "b.txt").exists()
        head_parents = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD^@"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        assert len(head_parents) == 2, "HEAD should be a merge commit with 2 parents"

        # Cleanup worktrees
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_a)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_b)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_skips_when_no_worktree(self, tmp_path: Path) -> None:
        """Merge succeeds without worktree — runtime sync is skipped."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]
        assert report.log_file is not None
        log_content = report.log_file.read_text()
        assert "Runtime sync skipped" in log_content

    def test_already_merged_runs_runtime_sync(self, tmp_path: Path) -> None:
        """When a branch is already merged (already_merged path), runtime sync still runs.

        The branch tip's tracked content was already on HEAD, but its gitignored
        runtime data (tier A) in the bound worktree still needs to be synced.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Merge feature first (normal path)
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report1 = orch.execute(["feature"])
        assert report1.success is True
        assert report1.merged_branches == ["feature"]

        # Create bound worktree for feature with tier A runtime data
        wt_dir = tmp_path / ".." / "feature-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True)
        (wt_se3 / "history" / "flow1.log").write_text("wt log")

        # Try merging feature again — it's already an ancestor
        # Reset the worktree so HEAD is back on main
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch2 = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report2 = orch2.execute(["feature"])

        assert report2.success is True
        assert report2.merged_branches == ["feature"]
        # Tier A files from the worktree should be synced even though
        # the branch was already merged
        target_se3 = tmp_path / "tianluo"
        assert (target_se3 / "history" / "flow1.log").exists()
        assert (target_se3 / "history" / "flow1.log").read_text() == "wt log"

        # Cleanup
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_os_error_skips_file(self, tmp_path: Path) -> None:
        """Tier A copy OSError skips the file; merge sequence continues."""
        from pathlib import Path

        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will have an OS error during copy
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Create bound worktree for feature-b with a tier A file
        wt_dir = tmp_path / ".." / "feature-b-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-b log")

        # Inject a PermissionError during os.open so the copy phase skips
        import os
        original_open = os.open
        tier_a_file = str(wt_se3 / "history" / "flow1.log")
        def _failing_open(path, flags, *args, **kwargs):
            if str(path) == tier_a_file:
                raise PermissionError(13, "Permission denied", str(path))
            return original_open(path, flags, *args, **kwargs)

        os.open = _failing_open
        try:
            orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
            report = orch.execute(["feature-a", "feature-b"])
        finally:
            os.open = original_open

        # Both branches merge successfully; the file is skipped
        assert report.success is True
        assert "feature-a" in report.merged_branches
        assert "feature-b" in report.merged_branches
        # File recorded as skipped, not as a failure
        assert report.failure_reason is None
        assert any(
            branch == "feature-b" and "history/flow1.log" in files
            for branch, files in report.runtime_sync_skipped_files
        )

        # feature-a's file should exist
        assert (tmp_path / "a.txt").exists()
        # feature-b's merge commit should still be on HEAD
        assert (tmp_path / "b.txt").exists()

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_timeout_stops_merge(self, tmp_path: Path) -> None:
        """TimeoutExpired from _get_worktree_path_for_branch stops merge gracefully."""
        import tianluo.engine.merge.runtime_sync as _rs

        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will timeout during worktree lookup
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock _get_worktree_path_for_branch to raise TimeoutExpired only for feature-b
        original = _rs._get_worktree_path_for_branch
        def _raise_timeout(project_root, branch):
            if branch == "feature-b":
                raise subprocess.TimeoutExpired(cmd=["git", "worktree", "list"], timeout=15)
            return original(project_root, branch)

        _rs._get_worktree_path_for_branch = _raise_timeout
        try:
            orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
            report = orch.execute(["feature-a", "feature-b"])
        finally:
            _rs._get_worktree_path_for_branch = original

        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_timeout"
        # feature-b IS recorded as merged (merge commit is on HEAD)
        assert "feature-b" in report.merged_branches
        assert (tmp_path / "a.txt").exists()

    def test_runtime_sync_collision_recovery_path(self, tmp_path: Path) -> None:
        """After a collision, re-running merge on the same branch succeeds
        once the collision is cleared (already_merged path re-runs sync),
        and the version from the merge commit is preserved."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Set up version metadata before branching
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "M0: v1.0.0")

        _create_branch(tmp_path, "feature-b")
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Set up target tianluo/ with a colliding file
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Create bound worktree for feature-b with a colliding file
        wt_dir = tmp_path / ".." / "feature-b-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-b log")

        # First merge attempt — should fail with collision (strict mode)
        orch1 = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report1 = orch1.execute(["feature-b"])

        assert report1.success is False
        assert report1.failure_reason == "runtime_sync_collision"
        # Merge commit IS on HEAD (git merge succeeded) — branch recorded as merged
        assert "feature-b" in report1.merged_branches
        assert (tmp_path / "b.txt").exists()
        # Version from the merge commit is already present
        assert _read_pyproject_version(tmp_path) == "1.0.1"

        # User clears the collision by removing the target file
        (target_se3 / "history" / "flow1.log").unlink()

        # Second merge attempt — should succeed via already_merged path
        orch2 = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report2 = orch2.execute(["feature-b"])

        assert report2.success is True
        assert "feature-b" in report2.merged_branches
        # The tier A file should now be synced from the worktree
        assert (target_se3 / "history" / "flow1.log").exists()
        assert (target_se3 / "history" / "flow1.log").read_text() == "feature-b log"
        # Version must remain correct (from the original merge, not double-bumped)
        assert _read_pyproject_version(tmp_path) == "1.0.1"
        # Task B2/B3/B4: already-merged branches skip bump inference, so
        # version aggregation is skipped on retry (the version was already
        # bumped in the first attempt).

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_collision_multi_branch_retry_no_over_bump(
        self, tmp_path: Path
    ) -> None:
        """Mixed-recovery: retry with one already-merged + one new branch.

        First attempt: B merges (version 1.0.0 -> 1.0.1) but runtime_sync
        collides, so C is never attempted. On retry `se3 merge B C`, B is
        already_merged and C merges. The final version must be 1.0.1
        (max(PATCH, PATCH) on original 1.0.0), NOT 1.0.2.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # M0: version 1.0.0
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "M0: v1.0.0")

        # Branch B: patch bump to 1.0.1
        _create_branch(tmp_path, "feature-b")
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "B: patch to 1.0.1")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Branch C: also patch bump to 1.0.1 (same target, different file)
        _create_branch(tmp_path, "feature-c")
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "c.txt").write_text("c")
        _commit(tmp_path, "C: patch to 1.0.1")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Set up target tianluo/ with a colliding file for B's worktree
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Create bound worktree for B with a colliding tier A file
        wt_b_dir = tmp_path / ".." / "feature-b-wt"
        wt_b_dir = wt_b_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_b_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_b_se3 = wt_b_dir / "tianluo"
        (wt_b_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_b_se3 / "history" / "flow1.log").write_text("feature-b log")

        # First attempt: merge B then C — B succeeds, runtime_sync collides
        orch1 = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report1 = orch1.execute(["feature-b", "feature-c"])

        assert report1.success is False
        assert report1.failure_reason == "runtime_sync_collision"
        # B's merge commit IS on HEAD — branch recorded as merged
        assert "feature-b" in report1.merged_branches
        assert (tmp_path / "b.txt").exists()
        assert _read_pyproject_version(tmp_path) == "1.0.1"

        # Clear the collision so retry can proceed
        (target_se3 / "history" / "flow1.log").unlink()

        # Retry: B is already_merged, C merges cleanly
        orch2 = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report2 = orch2.execute(["feature-b", "feature-c"])

        assert report2.success is True
        assert "feature-b" in report2.merged_branches
        assert "feature-c" in report2.merged_branches
        # Critical: must NOT over-bump. Both branches are PATCH on 1.0.0.
        assert report2.final_version == "1.0.1"
        assert _read_pyproject_version(tmp_path) == "1.0.1"

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_b_dir)],
            check=True, capture_output=True,
        )

    def test_conflict_resolved_then_runtime_sync(self, tmp_path: Path, monkeypatch) -> None:
        """After LLM conflict resolution and commit, runtime sync runs
        and copies tier A files from the branch's bound worktree."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "M0")

        # Create a shared file that will conflict
        (tmp_path / "shared.txt").write_text("base content")
        _commit(tmp_path, "Add shared.txt")

        # Branch modifies shared.txt
        _create_branch(tmp_path, "conflict-branch")
        (tmp_path / "shared.txt").write_text("theirs content")
        _commit(tmp_path, "Branch change")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Default branch also modifies shared.txt (creates conflict)
        (tmp_path / "shared.txt").write_text("ours content")
        _commit(tmp_path, "Ours change")

        # Set up bound worktree for the branch with tier A files
        wt_dir = tmp_path / ".." / "conflict-branch-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "conflict-branch"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow.log").write_text("branch history")

        # Mock conflict resolver to accept ours
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            files = []
            for cf in context.files:
                resolved = cf.ours_content
                files.append(
                    FileResolution(
                        path=cf.path,
                        resolved_content=resolved,
                        hunks=[
                            HunkResolution(
                                h.start_line, h.end_line,
                                Confidence.HIGH, "accept ours",
                            )
                            for h in cf.hunks
                        ],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    )
                )
            return LLMResolution(
                files=files,
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["conflict-branch"])

        assert report.success is True
        assert "conflict-branch" in report.merged_branches
        # The conflict was resolved to ours
        assert (tmp_path / "shared.txt").read_text() == "ours content"
        # Runtime sync should have copied tier A files from the worktree
        target_se3 = tmp_path / "tianluo"
        assert (target_se3 / "history" / "flow.log").exists()
        assert (target_se3 / "history" / "flow.log").read_text() == "branch history"

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_end_to_end_spec_example_4_7_0(self, tmp_path: Path, monkeypatch) -> None:
        """End-to-end: A advanced past branch-point, B PATCH, C MINOR -> 4.7.0.

        Repo state:
        - Base (M0): pyproject 4.4.0
        - Branch B: pyproject 4.4.1 (patch from base)
        - Branch C: pyproject 4.5.0 -> 4.5.1 -> 4.6.0 (minor from base, with noise)
        - A advances to 4.6.0 after branches were created

        Merge order: C first (clean -- pyproject identical to A at 4.6.0),
        then B (conflict -- B at 4.4.1 vs A at 4.6.0).
        The conflict resolver is mocked to accept keeping A's version.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")

        # Branch B: patch bump
        _create_branch(tmp_path, "B")
        _write_pyproject(tmp_path, "4.4.1")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "Bump patch on B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Branch C: minor bump with intermediate noise
        _create_branch(tmp_path, "C")
        _write_pyproject(tmp_path, "4.5.0")
        (tmp_path / "c.txt").write_text("c1")
        _commit(tmp_path, "C1: 4.5.0")
        _write_pyproject(tmp_path, "4.5.1")
        _commit(tmp_path, "C2: 4.5.1")
        _write_pyproject(tmp_path, "4.6.0")
        (tmp_path / "c.txt").write_text("c3")
        _commit(tmp_path, "C3: 4.6.0")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # A advances past branch-point to 4.6.0
        _write_pyproject(tmp_path, "4.6.0")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "M1: advance A to 4.6.0")

        # Mock conflict resolver to accept the pyproject resolution (keep ours)
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            files = []
            for cf in context.files:
                resolved = cf.ours_content
                files.append(
                    FileResolution(
                        path=cf.path,
                        resolved_content=resolved,
                        hunks=[
                            HunkResolution(
                                h.start_line, h.end_line,
                                Confidence.HIGH, "accept ours"
                            )
                            for h in cf.hunks
                        ],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    )
                )
            return LLMResolution(
                files=files,
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["C", "B"])

        assert report.success is True
        assert "C" in report.merged_branches
        assert "B" in report.merged_branches
        assert report.pre_merge_version == "4.6.0"
        # max(PATCH from B, MINOR from C) on 4.6.0 -> 4.7.0
        assert report.final_version == "4.7.0"
        assert report.bump_type == "minor"
        assert _read_pyproject_version(tmp_path) == "4.7.0"

    def test_runtime_sync_skipped_tracked_in_report(self, tmp_path: Path) -> None:
        """When a branch has no bound worktree, it is recorded in the report."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "Add pyproject")

        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert "feature" in report.merged_branches
        assert "feature" in report.runtime_sync_skipped_branches

    def test_clean_merge_merge_base_wiring(self, tmp_path: Path) -> None:
        """Orchestrator passes merge-base (not pre_merge_sha) to infer_branch_bump.

        When A advances past the branch-point and the branch merges cleanly,
        the bump is computed relative to the merge-base, giving the correct
        end-to-end diff.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")

        # Branch: minor bump (4.4.0 -> 4.6.0) with intermediate noise
        _create_branch(tmp_path, "feature")
        _write_pyproject(tmp_path, "4.5.0")
        (tmp_path / "f.txt").write_text("f1")
        _commit(tmp_path, "F1: 4.5.0")
        _write_pyproject(tmp_path, "4.6.0")
        (tmp_path / "f.txt").write_text("f2")
        _commit(tmp_path, "F2: 4.6.0")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # A advances past branch-point to 4.6.0 (same as feature tip)
        _write_pyproject(tmp_path, "4.6.0")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "M1: advance A to 4.6.0")

        # Feature merges cleanly because both A and feature have pyproject=4.6.0
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert "feature" in report.merged_branches
        assert report.pre_merge_version == "4.6.0"
        # merge-base is M0 (4.4.0), feature tip is 4.6.0 -> MINOR bump
        # 4.6.0 + MINOR = 4.7.0
        assert report.final_version == "4.7.0"
        assert report.bump_type == "minor"
        assert _read_pyproject_version(tmp_path) == "4.7.0"

    def test_merge_base_failure_skips_one_branch_aggregates_other(self, tmp_path: Path, monkeypatch) -> None:
        """When merge-base fails for one branch, it is skipped; the other still contributes.

        Creates two branches with related history. The first branch contributes a
        MINOR bump; the second branch is simulated to have no merge-base (as if
        from unrelated histories). The aggregation uses only the first branch.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")

        # Branch A: minor bump (4.4.0 -> 4.5.0)
        _create_branch(tmp_path, "branch-a")
        _write_pyproject(tmp_path, "4.5.0")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "Bump minor on A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Branch B: patch bump (4.4.0 -> 4.4.1)
        _create_branch(tmp_path, "branch-b")
        _write_pyproject(tmp_path, "4.4.1")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "Bump patch on B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock conflict resolver to avoid real LLM call for pyproject conflict
        def mock_resolve(self, context, strategy):
            from tianluo.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            files = []
            for cf in context.files:
                files.append(
                    FileResolution(
                        path=cf.path,
                        resolved_content=cf.ours_content,
                        hunks=[
                            HunkResolution(
                                h.start_line, h.end_line,
                                Confidence.HIGH, "accept ours"
                            )
                            for h in cf.hunks
                        ],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False},
                    )
                )
            return LLMResolution(
                files=files,
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False},
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Mock _run_git so merge-base fails for branch-b but succeeds for branch-a
        import tianluo.engine.merge.orchestrator as orch_mod
        orig_run_git = orch_mod._run_git

        def fake_run_git(project_root, *args, **kwargs):
            if len(args) >= 1 and args[0] == "merge-base":
                # args = ("merge-base", base_ref, branch)
                if len(args) >= 3 and args[2] == "branch-b":
                    import subprocess as sp
                    return sp.CompletedProcess(
                        args=args, returncode=1, stdout="", stderr="no merge base"
                    )
            return orig_run_git(project_root, *args, **kwargs)

        monkeypatch.setattr(orch_mod, "_run_git", fake_run_git)

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["branch-a", "branch-b"])

        # Both branches merged successfully
        assert report.success is True
        assert "branch-a" in report.merged_branches
        assert "branch-b" in report.merged_branches

        # Only branch-a contributed to aggregation (branch-b skipped).
        # When the on-disk version is already at the computed target,
        # bump_type is intentionally NOT set (bump_applied=False).
        assert report.pre_merge_version == "4.4.0"
        assert report.final_version == "4.5.0"
        assert _read_pyproject_version(tmp_path) == "4.5.0"

    def test_two_already_merged_branches_min_wins(self, tmp_path: Path) -> None:
        """Two already-merged branches: min base_version wins for effective pre-merge.

        Repo:
        - M0: 1.0.0
        - Branch A (from M0): 1.0.1 (patch) + a.txt
        - Merge A → M1 (version 1.0.1)
        - Branch B (from M1): 1.1.0 (minor) + b.txt
        - Merge B → M2 (version 1.1.0)

        Retry `se3 merge A B`:
        - A's base ref = M0, base_version = 1.0.0
        - B's base ref = M1, base_version = 1.0.1
        - min-wins: effective_pre_merge_version = 1.0.0 (not 1.0.1)
        - aggregate: max(patch, minor) on 1.0.0 = 1.1.0 (correct)

        Without min-wins, effective_pre_merge_version would be 1.0.1
        and aggregate would produce 1.2.0 (over-bump).
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # M0: version 1.0.0
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "M0: v1.0.0")

        # Branch A: patch bump to 1.0.1
        _create_branch(tmp_path, "A")
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "A: patch to 1.0.1")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Merge A → M1 (1.0.1)
        orch1 = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report1 = orch1.execute(["A"])
        assert report1.success is True
        assert report1.final_version == "1.0.1"

        # Branch B (from M1): minor bump to 1.1.0
        _create_branch(tmp_path, "B")
        _write_pyproject(tmp_path, "1.1.0")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "B: minor to 1.1.0")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Merge B → M2 (1.1.0)
        orch2 = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report2 = orch2.execute(["B"])
        assert report2.success is True
        assert report2.final_version == "1.1.0"

        # Now retry `se3 merge A B` — both are already merged
        orch3 = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report3 = orch3.execute(["A", "B"])

        assert report3.success is True
        assert "A" in report3.merged_branches
        assert "B" in report3.merged_branches
        # B2 fix: already-merged branches are excluded from bump
        # aggregation so they cannot double-count their already-applied
        # bumps. With both branches already-merged, branch_bumps is
        # empty and the aggregator is skipped (correctly — the version
        # has nothing to advance).  The version on disk stays at the
        # already-correct 1.1.0.
        assert report3.version_aggregation_skipped is True
        assert _read_pyproject_version(tmp_path) == "1.1.0"

    def test_runtime_sync_collision_lenient_bypasses_and_continues(
        self, tmp_path: Path,
    ) -> None:
        """In lenient mode, tier A collision bypasses to sidecar and the
        merge sequence continues with the next branch."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will have a collision (lenient mode)
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Set up target tianluo/ with an UNCOMMITTED file that will collide
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Create bound worktree for feature-b with a colliding file
        wt_dir = tmp_path / ".." / "feature-b-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-b log")

        # Default (lenient) mode — collision should be bypassed, sequence continues
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-a", "feature-b"])

        # Both branches should be merged successfully
        assert report.success is True
        assert "feature-a" in report.merged_branches
        assert "feature-b" in report.merged_branches
        assert report.failed_branch is None
        assert report.failure_reason is None

        # The colliding file in target should remain unchanged
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"
        # Sidecar should contain the source version
        sidecar = target_se3 / "history" / "flow1.log.from-feature-b"
        assert sidecar.exists()
        assert sidecar.read_text() == "feature-b log"

        # Collisions should be recorded in the report
        assert len(report.runtime_sync_collisions) == 1
        collision = report.runtime_sync_collisions[0]
        assert collision.branch == "feature-b"
        assert collision.original_rel_path == "history/flow1.log"
        assert collision.sidecar_rel_path == "history/flow1.log.from-feature-b"

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_collision_lenient_multi_branch_collisions(
        self, tmp_path: Path,
    ) -> None:
        """In lenient mode, multiple branches with collisions all bypass
        and the sequence completes with all collisions recorded."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Set up target tianluo/ with a file that both branches will collide with
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # feature-a: clean merge with colliding worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: also collides at the same path
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Create worktrees for both branches
        wt_a = tmp_path / ".." / "feature-a-wt"
        wt_a = wt_a.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_a), "feature-a"],
            check=True, capture_output=True,
        )
        (wt_a / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_a / "tianluo" / "history" / "flow1.log").write_text("feature-a log")

        wt_b = tmp_path / ".." / "feature-b-wt"
        wt_b = wt_b.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_b), "feature-b"],
            check=True, capture_output=True,
        )
        (wt_b / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_b / "tianluo" / "history" / "flow1.log").write_text("feature-b log")

        # Lenient mode — both collisions bypassed
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-a", "feature-b"])

        # Both branches merged successfully
        assert report.success is True
        assert "feature-a" in report.merged_branches
        assert "feature-b" in report.merged_branches

        # Target unchanged
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"

        # Both collisions recorded
        assert len(report.runtime_sync_collisions) == 2
        branches_with_collisions = {c.branch for c in report.runtime_sync_collisions}
        assert branches_with_collisions == {"feature-a", "feature-b"}

        # Sidecars exist for both
        assert (target_se3 / "history" / "flow1.log.from-feature-a").exists()
        assert (target_se3 / "history" / "flow1.log.from-feature-b").exists()
        assert (
            target_se3 / "history" / "flow1.log.from-feature-a"
        ).read_text() == "feature-a log"
        assert (
            target_se3 / "history" / "flow1.log.from-feature-b"
        ).read_text() == "feature-b log"

        # Cleanup worktrees
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_a)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_b)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_collision_lenient_logs_each_collision(
        self, tmp_path: Path,
    ) -> None:
        """In lenient mode, each bypassed collision is logged in the merge log."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature: clean merge with colliding worktree
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Set up target tianluo/ with colliding file
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        # Create worktree with colliding file
        wt_dir = tmp_path / ".." / "feature-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature"],
            check=True, capture_output=True,
        )
        (wt_dir / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_dir / "tianluo" / "history" / "flow1.log").write_text("feature log")

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.log_file is not None
        log_text = report.log_file.read_text()

        # Log should contain collision bypass info
        assert "Runtime sync collision" in log_text
        assert "history/flow1.log" in log_text
        assert "from-feature" in log_text

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_strict_mode_stops_sequence_same_as_before(
        self, tmp_path: Path,
    ) -> None:
        """Explicit strict=True preserves the old collision-stopping behavior."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will collide
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_dir = tmp_path / ".." / "feature-b-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-b log")

        # Strict mode — collision should stop the sequence
        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(["feature-a", "feature-b"])

        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_collision"
        # No collisions recorded (strict mode raises, doesn't bypass)
        assert len(report.runtime_sync_collisions) == 0
        # No sidecar created
        assert not (target_se3 / "history" / "flow1.log.from-feature-b").exists()

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_strict_mode_halts_before_subsequent_branches(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Strict mode with 3 branches: collision on middle branch halts sequence,
        does not create sidecars, and leaves subsequent branches unattempted."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-collide: will collide at runtime sync
        _create_branch(tmp_path, "feature-collide")
        _add_commit(tmp_path, "collide.txt", "collide", "Add collide")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-c: should never be attempted
        _create_branch(tmp_path, "feature-c")
        _add_commit(tmp_path, "c.txt", "c", "Add C")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_dir = tmp_path / ".." / "feature-collide-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-collide"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-collide log")

        # Sentinel side-effect setup: bind a worktree to feature-c containing
        # a unique runtime file. If the strict-mode halt logic regresses and
        # feature-c IS attempted, sync_branch_runtime would copy this sentinel
        # into the target project's tianluo/history/. Asserting the sentinel's
        # absence is independent of the monkey-patch — even if the monkeypatch
        # becomes a no-op due to a future call-site refactor, the file-system
        # check below will still detect a regression.
        wt_c_dir = tmp_path / ".." / "feature-c-wt"
        wt_c_dir = wt_c_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_c_dir), "feature-c"],
            check=True, capture_output=True,
        )
        wt_c_se3 = wt_c_dir / "tianluo"
        (wt_c_se3 / "history").mkdir(parents=True, exist_ok=True)
        sentinel_rel = "history/feature-c-sentinel.log"
        (wt_c_se3 / "history" / "feature-c-sentinel.log").write_text(
            "feature-c sentinel — must NOT be synced after strict halt"
        )

        # Track which branches `sync_branch_runtime` is called for so we can
        # assert directly that feature-c's worktree was never even attempted
        # (the loop must short-circuit after the strict-mode collision).
        import tianluo.engine.merge.orchestrator as _orch
        original_sync = _orch.sync_branch_runtime
        sync_call_branches: list[str] = []

        def _tracking_sync(project_root, branch, *, strict=False):
            sync_call_branches.append(branch)
            return original_sync(project_root, branch, strict=strict)

        monkeypatch.setattr(_orch, "sync_branch_runtime", _tracking_sync)

        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(["feature-a", "feature-collide", "feature-c"])

        # (a) feature-a merged successfully
        assert "feature-a" in report.merged_branches
        # (a.1) feature-collide git-merge succeeded; only post-merge sync
        # raised, so it must still appear in merged_branches.
        assert "feature-collide" in report.merged_branches
        # (b) Middle branch failed with runtime sync collision
        assert report.failed_branch == "feature-collide"
        assert report.failure_reason == "runtime_sync_collision"
        # (c) feature-c was never attempted
        assert "feature-c" not in report.merged_branches
        assert report.unattempted_branches == ["feature-c"]
        # (c.1) Direct evidence: sync_branch_runtime was never invoked for
        # feature-c. Together with unattempted_branches==['feature-c'], this
        # proves the orchestrator returned without entering the loop body
        # for feature-c — guards against a future regression that "attempts
        # but skips" subsequent branches after a strict halt.
        assert "feature-c" not in sync_call_branches
        # (c.2) File-system side-effect check: independent of the monkey-patch
        # above, the feature-c sentinel must not appear in the target. If a
        # future refactor breaks the monkey-patch (e.g. orchestrator imports
        # the symbol differently and `_orch.sync_branch_runtime` is no longer
        # the call-site binding), the tracking list assertion (c.1) silently
        # becomes a no-op — but this filesystem assertion still catches the
        # regression because the sentinel file is observable on disk.
        assert not (target_se3 / sentinel_rel).exists(), (
            f"feature-c sentinel file was synced to target despite strict halt; "
            f"orchestrator did not short-circuit after feature-collide failure"
        )
        # (d) No sidecar files created on target
        assert not list((target_se3 / "history").glob("*.from-feature-collide*"))
        # (e) No collision entries recorded (strict mode raises, doesn't bypass)
        assert len(report.runtime_sync_collisions) == 0
        # (f) The failed branch must not be recorded as skipped — a regression
        # that incorrectly populates these in the strict-collision halt path
        # would not be caught without this assertion.
        assert "feature-collide" not in report.runtime_sync_skipped_branches
        assert not any(
            branch == "feature-collide"
            for branch, _ in report.runtime_sync_skipped_files
        )
        # (g) Strict-mode halt must not populate idempotent-bypass tracking:
        # ``runtime_sync_idempotent_bypasses`` records ``(branch, count)``
        # tuples and ``runtime_sync_idempotent_records`` carries the per-file
        # ``BypassedCollision`` audit detail.  A regression that incorrectly
        # populated either field for a strict-halted branch (e.g. by running
        # the lenient idempotent-bypass code path before raising) would not
        # be caught by the collision/skipped assertions above, so assert
        # them empty explicitly — paralleling assertions (e)/(f).
        assert report.runtime_sync_idempotent_bypasses == []
        assert report.runtime_sync_idempotent_records == []

        # Cleanup worktrees
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_c_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_strict_mode_unattempted_branches_preserves_order(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Strict mode with 4 branches: collision on second branch leaves
        TWO unattempted branches; assert order is preserved verbatim from
        the input argument list (operator readability)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-collide: will collide at runtime sync
        _create_branch(tmp_path, "feature-collide")
        _add_commit(tmp_path, "collide.txt", "collide", "Add collide")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-c: should never be attempted
        _create_branch(tmp_path, "feature-c")
        _add_commit(tmp_path, "c.txt", "c", "Add C")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-d: should never be attempted (added to test multi-element
        # ordering in unattempted_branches; with two elements, list-equality
        # asserts a verifiable order rather than the implicit single-element
        # "ordering" of the prior test).
        _create_branch(tmp_path, "feature-d")
        _add_commit(tmp_path, "d.txt", "d", "Add D")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_dir = tmp_path / ".." / "feature-collide-mb-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-collide"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-collide log")

        # Track which branches `sync_branch_runtime` is called for so we can
        # confirm neither feature-c nor feature-d is attempted.
        import tianluo.engine.merge.orchestrator as _orch
        original_sync = _orch.sync_branch_runtime
        sync_call_branches: list[str] = []

        def _tracking_sync(project_root, branch, *, strict=False):
            sync_call_branches.append(branch)
            return original_sync(project_root, branch, strict=strict)

        monkeypatch.setattr(_orch, "sync_branch_runtime", _tracking_sync)

        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(
            ["feature-a", "feature-collide", "feature-c", "feature-d"]
        )

        # feature-a and feature-collide reached merged_branches
        assert "feature-a" in report.merged_branches
        assert "feature-collide" in report.merged_branches
        # feature-collide failed at runtime sync
        assert report.failed_branch == "feature-collide"
        assert report.failure_reason == "runtime_sync_collision"
        # Multi-element unattempted_branches must preserve input order:
        # feature-c was passed BEFORE feature-d, so the list MUST read
        # exactly that way. List-equality enforces both membership and
        # ordering. A regression that reordered, deduped, or sorted
        # alphabetically would land identical members but different order.
        assert report.unattempted_branches == ["feature-c", "feature-d"]
        # Direct evidence: neither was even attempted.
        assert "feature-c" not in sync_call_branches
        assert "feature-d" not in sync_call_branches
        # No sidecars were created (strict mode raised, did not bypass).
        assert not list((target_se3 / "history").glob("*.from-feature-collide*"))

        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_os_error_mid_bypass_orchestrator_level_lenient(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """In lenient mode, an unexpected OSError from sync_branch_runtime is
        logged and the merge sequence continues — the branch is recorded as
        skipped for runtime sync rather than halting the sequence.

        Monkey-patches the ``sync_branch_runtime`` name imported into the
        orchestrator module so the test exercises the exact call-site binding.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will trigger runtime sync OSError
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock sync_branch_runtime to raise OSError on feature-b
        # Must patch the name imported into the orchestrator module.
        import tianluo.engine.merge.orchestrator as _orch
        original_sync = _orch.sync_branch_runtime

        def _raising_sync(project_root, branch, *, strict=False):
            if branch == "feature-b":
                raise OSError(28, "No space left on device")
            return original_sync(project_root, branch, strict=strict)

        monkeypatch.setattr(_orch, "sync_branch_runtime", _raising_sync)

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-a", "feature-b"])

        # feature-a should merge successfully
        assert "feature-a" in report.merged_branches
        # In lenient mode, feature-b merge succeeds; OSError is treated as a
        # skipped branch (runtime sync unavailable) rather than a failure.
        assert report.success is True
        assert "feature-b" in report.merged_branches
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert "feature-b" in report.runtime_sync_skipped_branches
        # Merge commit for feature-b should be preserved on HEAD
        assert (tmp_path / "b.txt").exists()
        # Unattempted branches should be empty (feature-b was the last)
        assert report.unattempted_branches == []
        # No partial collision entries leaked
        assert len(report.runtime_sync_collisions) == 0

    def test_runtime_sync_os_error_mid_bypass_orchestrator_level_strict(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """In strict mode, an OSError from sync_branch_runtime still halts
        the merge sequence with 'runtime_sync_os_error' category."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        import tianluo.engine.merge.orchestrator as _orch
        original_sync = _orch.sync_branch_runtime

        def _raising_sync(project_root, branch, *, strict=False):
            if branch == "feature-b":
                raise OSError(28, "No space left on device")
            return original_sync(project_root, branch, strict=strict)

        monkeypatch.setattr(_orch, "sync_branch_runtime", _raising_sync)

        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(["feature-a", "feature-b"])

        assert "feature-a" in report.merged_branches
        assert report.success is False
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_os_error"
        assert (tmp_path / "b.txt").exists()
        assert report.unattempted_branches == []
        assert len(report.runtime_sync_collisions) == 0

    def test_runtime_sync_os_error_with_unattempted_branches_lenient(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """In lenient mode, an unexpected OSError mid-sequence does not halt;
        the branch is recorded as skipped and subsequent branches continue."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will trigger OSError
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-c: should still be attempted in lenient mode
        _create_branch(tmp_path, "feature-c")
        _add_commit(tmp_path, "c.txt", "c", "Add C")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        import tianluo.engine.merge.orchestrator as _orch
        original_sync = _orch.sync_branch_runtime

        def _raising_sync(project_root, branch, *, strict=False):
            if branch == "feature-b":
                raise OSError(28, "No space left on device")
            return original_sync(project_root, branch, strict=strict)

        monkeypatch.setattr(_orch, "sync_branch_runtime", _raising_sync)

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-a", "feature-b", "feature-c"])

        # All branches merge successfully at git level; lenient mode continues
        assert report.success is True
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert "feature-a" in report.merged_branches
        assert "feature-b" in report.merged_branches
        assert "feature-c" in report.merged_branches
        assert "feature-b" in report.runtime_sync_skipped_branches
        assert report.unattempted_branches == []
        assert (tmp_path / "b.txt").exists()

    def test_runtime_sync_timeout_monkeypatch_stops_sequence(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """TimeoutExpired from sync_branch_runtime halts sequence, preserves merge.

        Parallel to test_runtime_sync_os_error_mid_bypass_orchestrator_level
        (monkeypatch of the sync_branch_runtime name imported into the
        orchestrator module). Protects against future refactors that reorder
        the catch order in the orchestrator's runtime-sync error handling.
        """
        import tianluo.engine.merge.orchestrator as _orch

        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will trigger TimeoutExpired via monkeypatch
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-c: never attempted
        _create_branch(tmp_path, "feature-c")
        _add_commit(tmp_path, "c.txt", "c", "Add C")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        original_sync = _orch.sync_branch_runtime

        def _raising_sync(project_root, branch, *, strict=False):
            if branch == "feature-b":
                raise subprocess.TimeoutExpired(
                    cmd=["se3", "sync"], timeout=30,
                )
            return original_sync(project_root, branch, strict=strict)

        monkeypatch.setattr(_orch, "sync_branch_runtime", _raising_sync)

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-a", "feature-b", "feature-c"])

        # feature-a merges successfully
        assert "feature-a" in report.merged_branches
        # feature-b merge succeeded at git level but runtime sync timed out
        assert report.success is False
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_timeout"
        # Merge commit for feature-b IS on HEAD — branch recorded as merged
        assert "feature-b" in report.merged_branches
        assert report.unattempted_branches == ["feature-c"]
        assert (tmp_path / "b.txt").exists()
        # No partial collision entries leaked
        assert len(report.runtime_sync_collisions) == 0

    def test_runtime_sync_strict_collision_populates_unattempted(
        self, tmp_path: Path,
    ) -> None:
        """When strict-mode runtime sync collision halts the sequence,
        unattempted_branches is populated correctly."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-a: clean merge, no worktree
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: will collide
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-c: never attempted
        _create_branch(tmp_path, "feature-c")
        _add_commit(tmp_path, "c.txt", "c", "Add C")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_dir = tmp_path / ".." / "feature-b-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        (wt_se3 / "history" / "flow1.log").write_text("feature-b log")

        orch = MergeOrchestrator(
            project_root=tmp_path, strict_runtime_sync=True,
        )
        report = orch.execute(["feature-a", "feature-b", "feature-c"])

        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "runtime_sync_collision"
        # Critical: unattempted_branches must be populated
        assert report.unattempted_branches == ["feature-c"]
        # feature-c must NOT appear as merged — it was never attempted
        assert "feature-c" not in report.merged_branches
        # No collisions recorded (strict mode raises, doesn't bypass)
        assert len(report.runtime_sync_collisions) == 0

        # Filesystem-level assertion: strict mode raises before any sidecar
        # is written. A regression that wrote a sidecar in strict mode
        # would leave a `flow1.log.from-<branch>` file at the target —
        # detect that directly rather than relying solely on the report's
        # bookkeeping (which a buggy code path might also fail to update).
        assert not list((target_se3 / "history").glob("flow1.log.from-*"))
        # Recursive sweep across the entire target tree for any sidecar.
        for sidecar in target_se3.rglob("*.from-*"):
            raise AssertionError(
                f"strict mode unexpectedly wrote sidecar at {sidecar}"
            )

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_lenient_disambiguation_exhausted_skipped_files(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Lenient mode: when both ``<dest>.from-<branch>`` and
        ``<dest>.from-<branch>.<short_hash>`` already exist on the target
        with different content, the file is reported via
        ``runtime_sync_skipped_files`` (not ``runtime_sync_collisions``)
        and the merge sequence still completes successfully.
        """
        import hashlib
        import logging

        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # feature-b: will trigger lenient sync; both sidecars pre-exist
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Build worktree with the source flow1.log
        wt_dir = tmp_path / ".." / "feature-b-disambig-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        src_content = "feature-b log content"
        (wt_se3 / "history" / "flow1.log").write_text(src_content)
        src_hash = hashlib.sha256(src_content.encode()).hexdigest()
        short_hash = src_hash[:8]
        long_hash = src_hash[:16]

        # Pre-populate target with primary file AND all three sidecar paths
        # so disambiguation exhausts.
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")
        (target_se3 / "history" / "flow1.log.from-feature-b").write_text(
            "old sidecar content"
        )
        (target_se3 / "history" / f"flow1.log.from-feature-b.{short_hash}").write_text(
            "old hash-suffix sidecar content"
        )
        (target_se3 / "history" / f"flow1.log.from-feature-b.{long_hash}").write_text(
            "old long-hash sidecar content"
        )

        with caplog.at_level(
            logging.WARNING, logger="tianluo.engine.merge.runtime_sync"
        ):
            orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
            report = orch.execute(["feature-b"])

        # Merge should still succeed (lenient mode)
        assert report.success is True
        assert "feature-b" in report.merged_branches

        # The disambiguation-exhausted file is reported via skipped_files
        skipped_paths = [
            rel
            for branch, paths in report.runtime_sync_skipped_files
            for rel in paths
            if branch == "feature-b"
        ]
        assert "history/flow1.log" in skipped_paths

        # Collision is recorded for audit trail even when sidecar write
        # failed, so operators see a uniform entry in runtime_sync_collisions.
        assert any(
            c.original_rel_path == "history/flow1.log"
            for c in report.runtime_sync_collisions
        )

        # Original target and pre-existing sidecars are untouched
        assert (target_se3 / "history" / "flow1.log").read_text() == "target log"
        assert (
            target_se3 / "history" / "flow1.log.from-feature-b"
        ).read_text() == "old sidecar content"
        assert (
            target_se3 / "history" / f"flow1.log.from-feature-b.{short_hash}"
        ).read_text() == "old hash-suffix sidecar content"

        # Operator-visible warning distinguishes this skip from benign IO skips
        assert any(
            "sidecar disambiguation exhausted" in record.message
            and "history/flow1.log" in record.message
            for record in caplog.records
        )

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_runtime_sync_idempotent_records_propagated_to_merge_report(
        self, tmp_path: Path,
    ) -> None:
        """Direct test: when ``sync_branch_runtime`` records an idempotent
        sidecar match in ``SyncReport.idempotent_bypass_records``, the
        orchestrator forwards every entry to
        ``MergeReport.runtime_sync_idempotent_records``.

        This protects against a future refactor that drops the ``extend()``
        call at orchestrator.py:~1235 while still incrementing the
        ``runtime_sync_idempotent_bypasses`` counter — the silent-drop
        regression would not be caught by counter-only assertions.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Build worktree containing the source flow1.log with a known content.
        wt_dir = tmp_path / ".." / "feature-b-idempotent-wt"
        wt_dir = wt_dir.resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_dir), "feature-b"],
            check=True, capture_output=True,
        )
        wt_se3 = wt_dir / "tianluo"
        (wt_se3 / "history").mkdir(parents=True, exist_ok=True)
        src_content = "feature-b idempotent log"
        (wt_se3 / "history" / "flow1.log").write_text(src_content)

        # Pre-populate target with primary file (different content) AND a
        # sidecar that already matches source — triggers the idempotent path.
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")
        (target_se3 / "history" / "flow1.log.from-feature-b").write_text(
            src_content
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature-b"])

        # Merge succeeds in lenient mode
        assert report.success is True
        assert "feature-b" in report.merged_branches

        # Counter is incremented (existing rendered signal)
        assert report.runtime_sync_idempotent_bypasses == [("feature-b", 1)]

        # Per-file audit detail is forwarded to the orchestrator-level report:
        # this is the regression guard for the propagation extend() call.
        assert len(report.runtime_sync_idempotent_records) == 1
        record = report.runtime_sync_idempotent_records[0]
        assert record.branch == "feature-b"
        assert record.original_rel_path == "history/flow1.log"
        assert record.sidecar_rel_path == "history/flow1.log.from-feature-b"

        # Cleanup worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_dir)],
            check=True, capture_output=True,
        )

    def test_postcond_check_timeout_dispatched_to_report(self, tmp_path: Path, monkeypatch) -> None:
        """_verify_post_merge_conditions returning 'postcond_check_timeout'
        routes through the state machine to the user-visible report."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Force _verify_post_merge_conditions to return the timeout token
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator._verify_post_merge_conditions",
            lambda self, branch, *, already_ancestor, report: "postcond_check_timeout",
        )

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "postcond_check_timeout"


class TestRuntimeSyncCollisionVersionAggregation:
    """Couple the 'Version not bumped' log claim to actual final_version state.

    These tests assert what ``report.final_version`` is set to after a
    runtime_sync_collision break in lenient and strict modes, ensuring
    the orchestrator does NOT short-circuit version aggregation when
    earlier branches have contributed bumps (B12 contract).  Without
    these assertions, a regression where aggregation is incorrectly
    skipped after the break would silently pass through the existing
    "report shape" tests because they only assert ``report.success``
    and ``failure_reason`` — not the aggregated version.
    """

    def test_collision_break_does_not_skip_aggregation(
        self, tmp_path: Path,
    ) -> None:
        """B12 contract: a runtime_sync_collision break MUST still feed
        version aggregation from successful earlier branches' bumps.

        Setup:
        - M0: pyproject at 1.0.0
        - feature-a: PATCH bump to 1.0.1 (clean merge, no collision)
        - feature-b: NO version bump, but a tier-A worktree file that
          collides with the target — runtime_sync triggers
          runtime_sync_collision in strict mode
        Expected:
        - report.success is False
        - failure_reason == 'runtime_sync_collision'
        - But aggregation runs because feature-a's bump is in branch_bumps
        - final_version == '1.0.1' (PATCH applied) — NOT None
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "M0: v1.0.0")

        # feature-a: bumps version to 1.0.1, no worktree (no runtime sync)
        _create_branch(tmp_path, "feature-a")
        _write_pyproject(tmp_path, "1.0.1")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "feature-a: PATCH to 1.0.1")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # feature-b: no version change, but a worktree with a tier-A file
        # that will collide with target tianluo/.
        _create_branch(tmp_path, "feature-b")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "feature-b: add b.txt (no version change)")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Pre-place a colliding tier-A file in target tianluo/ so feature-b's
        # runtime sync hits a strict collision.
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_b = (tmp_path / ".." / "feature-b-wt").resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_b), "feature-b"],
            check=True, capture_output=True,
        )
        (wt_b / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_b / "tianluo" / "history" / "flow1.log").write_text(
            "feature-b log (collides)"
        )

        try:
            orch = MergeOrchestrator(
                project_root=tmp_path, strict_runtime_sync=True,
            )
            report = orch.execute(["feature-a", "feature-b"])

            # The sequence halts on feature-b's collision, but:
            # 1. feature-a is recorded as merged
            # 2. feature-b's git merge succeeded (recorded too)
            # 3. branch_bumps contains feature-a's PATCH bump
            # 4. version aggregation runs at the end of the loop
            assert report.success is False
            assert report.failure_reason == "runtime_sync_collision"
            assert report.failed_branch == "feature-b"
            assert "feature-a" in report.merged_branches
            assert "feature-b" in report.merged_branches

            # Aggregation IS run despite the collision break — gated
            # solely by branch_bumps non-emptiness.
            assert report.version_aggregation_skipped is False, (
                "B12 contract violated: aggregation was skipped despite "
                "feature-a contributing a PATCH bump"
            )

            # Critical: the final version reflects the aggregated bump
            # from feature-a (and any bump from feature-b's merge-base
            # diff).  Whichever branches contributed bumps, the
            # aggregation result MUST land on disk and be reflected in
            # report.final_version — NOT None.
            assert report.final_version is not None, (
                "B12 contract violated: final_version is None despite "
                "successful aggregation. The 'Version not bumped' log "
                "would be misleading if reintroduced."
            )
            assert _read_pyproject_version(tmp_path) == report.final_version

            # Specifically, since feature-a is PATCH on 1.0.0 and
            # feature-b's merge contributes no version diff, the
            # aggregated bump is PATCH, applied to 1.0.0 → 1.0.1.
            # (If feature-b's merge-base diff yielded a different bump,
            #  this assertion would need to follow the max rule; for
            #  this scenario PATCH is the correct expected outcome.)
            assert report.final_version == "1.0.1", (
                f"Expected final_version='1.0.1', got "
                f"{report.final_version!r}"
            )
        finally:
            subprocess.run(
                ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_b)],
                check=False, capture_output=True,
            )

    def test_collision_break_log_does_not_lie_about_version_state(
        self, tmp_path: Path,
    ) -> None:
        """The orchestrator's post-break log MUST NOT claim 'Version not
        bumped' when version aggregation actually ran successfully.

        This guards against the historical regression where the log
        line emitted right before the runtime_sync_* break said the
        version was not bumped, even though the post-loop aggregation
        path still applied bumps from earlier successful branches.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _write_pyproject(tmp_path, "2.0.0")
        _commit(tmp_path, "M0: v2.0.0")

        _create_branch(tmp_path, "feature-a")
        _write_pyproject(tmp_path, "2.0.1")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "feature-a: PATCH to 2.0.1")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        _create_branch(tmp_path, "feature-b")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "feature-b: add b.txt")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_b = (tmp_path / ".." / "feature-b-wt-2").resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_b), "feature-b"],
            check=True, capture_output=True,
        )
        (wt_b / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_b / "tianluo" / "history" / "flow1.log").write_text(
            "feature-b log (collides)"
        )

        try:
            orch = MergeOrchestrator(
                project_root=tmp_path, strict_runtime_sync=True,
            )
            report = orch.execute(["feature-a", "feature-b"])

            assert report.failure_reason == "runtime_sync_collision"
            assert report.final_version == "2.0.1"

            # Read the log to verify the misleading message is gone.
            assert report.log_file is not None
            log_text = report.log_file.read_text()

            # Aggregation log should be present.
            assert "Aggregating SemVer bumps" in log_text or (
                "Version aggregated" in log_text
            ) or "version_higher_than_target" in log_text.lower(), (
                "Aggregation should have run after the collision break"
            )

            # The historically-misleading message MUST NOT appear in
            # combination with the actual aggregation output.  Either:
            # (a) the message was removed entirely, or
            # (b) it was replaced with an honest description.
            # We assert that "Version not bumped despite N successful
            # merge(s)" no longer appears with the runtime_sync_*
            # halt context, because aggregation DID run.
            assert "Version not bumped despite" not in log_text, (
                "Misleading log retained: aggregation actually ran but "
                "the log claims version was not bumped"
            )
        finally:
            subprocess.run(
                ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_b)],
                check=False, capture_output=True,
            )

    def test_post_loop_success_log_only_when_no_failure(
        self, tmp_path: Path,
    ) -> None:
        """Post-loop 'All N branches merged successfully' log MUST be
        gated on report.failed_branch is None.

        Regression guard for the misleading message that previously
        printed 'All N branches merged successfully' even after a
        runtime_sync_* break (where some branches were not attempted
        and report.success is False).
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _write_pyproject(tmp_path, "3.0.0")
        _commit(tmp_path, "M0")

        _create_branch(tmp_path, "feature-a")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "feature-a")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        _create_branch(tmp_path, "feature-b")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "feature-b")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Set up collision target
        target_se3 = tmp_path / "tianluo"
        (target_se3 / "history").mkdir(parents=True, exist_ok=True)
        (target_se3 / "history" / "flow1.log").write_text("target log")

        wt_b = (tmp_path / ".." / "feature-b-wt-3").resolve()
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", str(wt_b), "feature-b"],
            check=True, capture_output=True,
        )
        (wt_b / "tianluo" / "history").mkdir(parents=True, exist_ok=True)
        (wt_b / "tianluo" / "history" / "flow1.log").write_text(
            "feature-b log (collides)"
        )

        try:
            orch = MergeOrchestrator(
                project_root=tmp_path, strict_runtime_sync=True,
            )
            report = orch.execute(["feature-a", "feature-b"])

            assert report.success is False
            assert report.failed_branch == "feature-b"

            assert report.log_file is not None
            log_text = report.log_file.read_text()

            # The misleading "All N branch(es) merged successfully" log
            # MUST NOT appear when failed_branch is set.  Operators
            # reading the log should see an honest "halted at branch X"
            # message instead.
            assert "All 2 branch(es) merged successfully" not in log_text, (
                "Misleading log: post-loop success message printed "
                "even though feature-b failed"
            )
            # The honest halted-at message should be present.
            assert (
                "halted at branch" in log_text.lower()
                or "of 2 branch(es) merged before halt" in log_text.lower()
            ), f"Expected halted-at message in log:\n{log_text}"
        finally:
            subprocess.run(
                ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt_b)],
                check=False, capture_output=True,
            )


class TestMergeOrchestratorBatchResolverIntegration:
    """End-to-end coverage that ``MergeOrchestrator.execute()`` actually
    routes conflict resolution through :meth:`ConflictResolver.resolve_batch`.

    The other conflict-resolution tests in this file monkeypatch the
    legacy ``ConflictResolver.resolve`` entry point, which bypasses the
    LLM-as-editor batch path entirely.  This class exercises the new
    path: ``resolve`` is the public wrapper, but the LLM call site
    (``ConflictResolver._call_llm``) is monkeypatched so the test can
    drive a stub "LLM" that edits files on disk — exactly how the new
    model is supposed to work.
    """

    def test_orchestrator_routes_through_resolve_batch_for_safe(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A real merge with a single conflict file is resolved via the
        LLM-as-editor loop.  The stub LLM clears the conflict markers
        on its first call; the orchestrator commits and reports
        success.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base\n", "base")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature change"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("base-updated\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "base change"],
            check=True, capture_output=True,
        )

        # Stub LLM that clears the conflict by writing a merged version
        # of shared.txt directly.  Called via ``ConflictResolver._call_llm``,
        # which the new ``resolve()`` wrapper invokes through ``resolve_batch``.
        call_count = {"n": 0}

        def fake_call_llm(self, prompt: str) -> str:
            call_count["n"] += 1
            (tmp_path / "shared.txt").write_text("base-updated\nfeature\n")
            return "edited"

        monkeypatch.setattr(
            "tianluo.engine.merge.conflict_resolver.ConflictResolver._call_llm",
            fake_call_llm,
        )

        orch = MergeOrchestrator(
            project_root=tmp_path, strategy="safe", delete_merged=False,
        )
        report = orch.execute(["feature"])

        assert report.success is True, (
            f"merge unexpectedly failed: "
            f"{report.failure_reason} / {report.failure_detail}"
        )
        assert call_count["n"] >= 1, (
            "The orchestrator never invoked the LLM — meaning the "
            "LLM-as-editor batch path was not exercised."
        )
        assert "feature" in report.merged_branches

        # The resolved file is on disk with markers cleared.
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" not in content
        assert "=======" not in content
        assert ">>>>>>>" not in content

    def test_orchestrator_fast_aborts_when_batch_loop_exhausts(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Fast strategy + an LLM that never clears markers → after the
        configured ``max_conflict_resolve_iterations`` are exhausted the
        merge aborts without writing a human MCP call file.  Exercises
        the full path through ``resolve_batch``.
        """
        _init_repo(tmp_path)
        # Use a low iteration cap so the test runs quickly.  Written
        # after _init_repo so we don't fight that helper's initial
        # commit logic.
        (tmp_path / "tianluo.yaml").write_text(
            "merge:\n  max_conflict_resolve_iterations: 2\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "tianluo.yaml"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "configure max iter"],
            check=True, capture_output=True,
        )
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base\n", "base")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("base-updated\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "base change"],
            check=True, capture_output=True,
        )

        # Stub LLM that never edits — every iteration leaves markers in place.
        call_count = {"n": 0}

        def fake_call_llm(self, prompt: str) -> str:
            call_count["n"] += 1
            return "I couldn't resolve"

        monkeypatch.setattr(
            "tianluo.engine.merge.conflict_resolver.ConflictResolver._call_llm",
            fake_call_llm,
        )

        orch = MergeOrchestrator(
            project_root=tmp_path, strategy="fast", delete_merged=False,
        )
        report = orch.execute(["feature"])

        # Cap was 2 → the resolve_batch loop should have called the LLM
        # exactly that many times (one per iteration) before declaring
        # the batch exhausted.
        assert call_count["n"] == 2, (
            f"Expected 2 LLM iterations (cap=2), got {call_count['n']}"
        )
        assert report.success is False
        # Fast strategy never writes a human call file on resolution failure.
        assert report.human_call_file is None


class TestAbortMergeNoMergeInProgress:
    """G2: ``_abort_merge`` must treat 'no merge to abort' as success.

    Otherwise every failure that happens BEFORE git merge even starts (e.g.
    a blocked dirty pre-flight, or a merge that git refused to begin) has its
    real ``failure_reason`` overwritten with a misleading
    ``merge_abort_failed`` — exactly the root-cause masking this change fixes.
    """

    def _make_orch(self, tmp_path: Path) -> MergeOrchestrator:
        _init_repo(tmp_path)
        return MergeOrchestrator(project_root=tmp_path, delete_merged=False)

    def test_merge_head_missing_treated_as_success(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        orch = self._make_orch(tmp_path)

        def fake_run_git(project_root, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=128,
                stdout="",
                stderr="fatal: There is no merge to abort (MERGE_HEAD missing).\n",
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", fake_run_git
        )
        assert orch._abort_merge() is True

    def test_real_abort_failure_returns_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        orch = self._make_orch(tmp_path)

        def fake_run_git(project_root, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=1,
                stdout="",
                stderr="fatal: unable to write new index file\n",
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", fake_run_git
        )
        assert orch._abort_merge() is False

    def test_clean_abort_returns_true(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        orch = self._make_orch(tmp_path)

        def fake_run_git(project_root, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=list(args), returncode=0, stdout="", stderr="",
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", fake_run_git
        )
        assert orch._abort_merge() is True

    def test_timeout_returns_false(self, tmp_path: Path, monkeypatch) -> None:
        orch = self._make_orch(tmp_path)

        def fake_run_git(project_root, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git merge --abort", timeout=30)

        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator._run_git", fake_run_git
        )
        assert orch._abort_merge() is False
