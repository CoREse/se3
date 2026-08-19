"""Regression tests for the merge post-condition invariants.

Historically this module also covered the spec-guardrail repair
machinery (amend / fix-up / rollback).  That chain has been removed
from production, so only the invariants that are independent of it
remain:

- A stray non-merge commit on top of HEAD trips ``silent_merge_loss``
  (HEAD must itself be the merge commit on the per-branch path).
- A post-condition check timeout is fail-closed, never a soft warning.
- An unreadable post-merge SHA (empty ``post_merge_sha``) never
  short-circuits the post-condition checks into a silent success.
- ``execute`` populates ``report.outcomes`` on both the success and the
  failure path, and keeps the merged branch reachable from HEAD.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


# --------- helpers ---------


def _init_repo(path: Path) -> str:
    """Init a git repo and return the initial commit SHA."""
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
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _add_feature_branch(path: Path, branch: str = "feature") -> None:
    """Create *branch* off the current HEAD with one commit, then return
    to the default branch."""
    default = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    _git(path, "checkout", "-b", branch)
    (path / f"{branch.replace('/', '_')}.txt").write_text(f"{branch} content\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"{branch} commit")
    _git(path, "checkout", default)


def _mask_post_merge_rev_parse(monkeypatch) -> dict:
    """Make the ``rev-parse HEAD`` that follows a ``git merge`` fail.

    Mirrors the production guard: when that read fails the orchestrator
    sets ``post_merge_sha = ""``, which means the no-op (already
    up-to-date) short-circuit cannot fire and the post-condition checks
    must run unconditionally.

    Returns the mutable state dict so callers can assert the mask
    actually fired (``state["masked"] == 1``) instead of passing
    vacuously.
    """
    import tianluo.engine.merge.orchestrator as orch_mod

    original_run_git = orch_mod._run_git
    state = {"after_merge": False, "masked": 0}

    def patched_run_git(project_root, *args, check=True, timeout=30, **kwargs):
        if args and args[0] == "merge":
            result = original_run_git(
                project_root, *args, check=check, timeout=timeout, **kwargs
            )
            state["after_merge"] = True
            return result
        if state["after_merge"] and args == ("rev-parse", "HEAD"):
            state["after_merge"] = False
            state["masked"] += 1
            return subprocess.CompletedProcess(
                args=list(args), returncode=128, stdout="", stderr="fatal: bad object",
            )
        return original_run_git(
            project_root, *args, check=check, timeout=timeout, **kwargs
        )

    monkeypatch.setattr(orch_mod, "_run_git", patched_run_git)
    return state


# --------- stray commit on top of the merge commit ---------


class TestStrayCommitTripsSilentMergeLoss:
    """A stray non-merge commit on top of HEAD must trip silent_merge_loss.

    Nothing on the per-branch merge path stacks a commit on top of the
    merge commit before ``_verify_post_merge_conditions`` runs, so HEAD
    must itself BE the merge commit there.  A stray commit (a hook, a
    future code path) must fail the post-condition rather than being
    tolerated as a fix-up parent.
    """

    def test_stray_commit_trips_silent_merge_loss(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        _add_feature_branch(tmp_path)

        # Monkeypatch _verify_post_merge_conditions to inject a stray commit
        # BEFORE the real check runs. This simulates a hook or side effect
        # that appended a non-merge commit on top of the merge commit.
        original_verify = MergeOrchestrator._verify_post_merge_conditions

        def patched_verify(self, branch, *, already_ancestor, report):
            # Inject a stray single-parent commit on top of HEAD
            (tmp_path / "stray.txt").write_text("stray\n")
            _git(tmp_path, "add", ".")
            _git(tmp_path, "commit", "-m", "stray commit")
            return original_verify(
                self, branch, already_ancestor=already_ancestor, report=report,
            )

        monkeypatch.setattr(
            MergeOrchestrator, "_verify_post_merge_conditions", patched_verify,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # The stray commit means HEAD is no longer the merge commit;
        # post-condition should fire silent_merge_loss.
        assert report.success is False
        assert report.failure_reason == "silent_merge_loss"
        assert report.failed_branch == "feature"

    def test_verify_post_merge_conditions_has_no_fixup_tolerance(self) -> None:
        """The per-branch post-condition takes no ``allow_fixup_parent`` knob.

        Guards the refactor: re-introducing a fix-up tolerance on this
        code path would silently accept the stray-commit layout that the
        test above proves must fail.
        """
        import inspect

        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        params = inspect.signature(
            MergeOrchestrator._verify_post_merge_conditions
        ).parameters
        assert "allow_fixup_parent" not in params
        assert set(params) == {"self", "branch", "already_ancestor", "report"}


class TestTimeoutFailClosed:
    """Post-condition timeout must be treated as fail-closed, not soft warning."""

    def test_postcond_check_timeout_returns_failure(self, tmp_path: Path, monkeypatch) -> None:
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        _add_feature_branch(tmp_path)

        # Mock the post-condition to raise TimeoutExpired
        def mock_postcond(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git merge-base", timeout=15)

        # G3 fix: orchestrator imports postcondition helpers at module
        # top, so patch the orchestrator's bound reference rather than
        # the postcondition module's symbol.
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.assert_branch_merged",
            mock_postcond,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        # Must fail closed, not silently succeed
        assert report.success is False
        assert report.failure_reason == "postcond_check_timeout"
        assert report.failed_branch == "feature"


# --------- empty post-merge SHA must not short-circuit into success ---------


class TestEmptyPostShaFallback:
    """The orchestrator must refuse to declare success when the post-merge
    SHA is unreadable AND the merge commit is not on HEAD.

    ``git rev-parse HEAD`` failing right after a clean merge leaves
    ``post_merge_sha == ""``.  That empty value must never be treated as
    "nothing changed" (the no-op short-circuit) nor as proof of success:
    the post-condition checks run regardless and fail closed when HEAD is
    not the merge commit.
    """

    def test_empty_post_sha_with_lost_merge_refuses_success(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Empty post_sha + non-merge HEAD must NOT be reported as success.

        Scenario: the branch was already fast-forwarded into the default
        branch, so ``git merge`` reports "Already up to date." and HEAD
        is a plain single-parent commit — NOT a merge commit.  With the
        post-merge ``rev-parse HEAD`` masked, the
        ``post_merge_sha == pre_merge_sha`` no-op short-circuit cannot
        fire, so the full post-condition (HEAD is the merge commit) runs
        and must refuse to declare success.
        """
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        _add_feature_branch(tmp_path)
        # Fast-forward the branch in: HEAD becomes the feature tip, a
        # plain commit that is NOT a merge commit.
        _git(tmp_path, "merge", "--ff-only", "feature")
        head_before = _git(tmp_path, "rev-parse", "HEAD")
        assert _git(tmp_path, "rev-list", "--parents", "-n", "1", "HEAD").count(" ") == 1, (
            "fixture must leave HEAD as a single-parent (non-merge) commit"
        )

        mask_state = _mask_post_merge_rev_parse(monkeypatch)

        orch = MergeOrchestrator(
            project_root=tmp_path, strategy="safe", delete_merged=False,
        )
        report = orch.execute(["feature"])

        assert mask_state["masked"] == 1, "the post-merge SHA read was never masked"
        assert report.success is False, (
            "An unreadable post-merge SHA must not be reported as success "
            "when HEAD is not the merge commit."
        )
        assert report.failure_reason == "silent_merge_loss"
        assert report.failed_branch == "feature"
        # Fail-closed means HEAD is left exactly where it was.
        assert _git(tmp_path, "rev-parse", "HEAD") == head_before

    def test_empty_post_sha_with_real_merge_succeeds(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Empty post_sha is OK when HEAD is genuinely a merge commit.

        Counterpoint to the previous test: when the merge really did
        produce a merge commit, an unreadable SHA must not make the
        post-condition over-zealous — the checks pass and the merge is
        reported as successful.
        """
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        # Diverge the default branch so the merge cannot fast-forward.
        _add_feature_branch(tmp_path)
        (tmp_path / "base.txt").write_text("base content\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "base commit")

        mask_state = _mask_post_merge_rev_parse(monkeypatch)

        orch = MergeOrchestrator(
            project_root=tmp_path, strategy="safe", delete_merged=False,
        )
        report = orch.execute(["feature"])

        assert mask_state["masked"] == 1, "the post-merge SHA read was never masked"
        assert report.success is True, (
            f"merge unexpectedly failed: {report.failure_reason} / "
            f"{report.failure_detail}"
        )
        assert "feature" in report.merged_branches
        # HEAD really is the merge commit despite the unreadable SHA.
        parents = _git(tmp_path, "rev-list", "--parents", "-n", "1", "HEAD").split()
        assert len(parents) == 3, f"expected a merge commit, got parents: {parents}"


class TestEndToEndMergeInvariants:
    """End-to-end coverage of ``MergeOrchestrator.execute`` on a real repo.

    The unit-level tests above cover the post-condition helpers in
    isolation; this class covers the full sequence under realistic git
    state — the merged branch stays reachable from HEAD and the typed
    per-branch outcomes are populated on both the success and the
    failure path.
    """

    def test_full_merge_keeps_branch_merged(self, tmp_path: Path) -> None:
        """Real ``execute``: the branch must remain an ancestor of HEAD.

        If a future regression caused a later step to drop the merge
        commit, ``assert_branch_merged`` would fail and the
        post-condition path would surface ``silent_merge_loss``.
        Asserting that the branch is still an ancestor of HEAD is a
        stronger end-to-end contract than the unit tests, because it
        depends on every step of ``execute`` preserving the merge commit.
        """
        from tianluo.commands.merge.postcondition import assert_branch_merged
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        _add_feature_branch(tmp_path)

        orch = MergeOrchestrator(
            project_root=tmp_path, strategy="safe", delete_merged=False,
        )
        report = orch.execute(["feature"])

        assert report.success is True, (
            f"merge unexpectedly failed: {report.failure_reason} / {report.failure_detail}"
        )
        # The literal post-condition that would catch a silent merge loss.
        assert_branch_merged(tmp_path, "feature")

    def test_outcomes_populated_for_successful_merge(self, tmp_path: Path) -> None:
        """G1[2]: report.outcomes carries one MergeOutcome per branch.

        Validates that the typed per-branch outcome list is populated
        alongside the legacy ``merged_branches`` list, so consumers
        that prefer the typed model can iterate ``report.outcomes``
        without scraping strings from ``merged_branches`` /
        ``failed_branch``.
        """
        from tianluo.commands.merge.result_model import MergeOutcome
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        _add_feature_branch(tmp_path)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is True
        # Exactly one outcome per branch processed.
        assert len(report.outcomes) == 1
        outcome = report.outcomes[0]
        assert isinstance(outcome, MergeOutcome)
        assert outcome.branch == "feature"
        assert outcome.success is True
        assert outcome.failure_reason is None
        # Successful merge → SHA captured (HEAD points to merge commit).
        assert outcome.merge_commit_sha is not None
        assert len(outcome.merge_commit_sha) >= 8

    def test_outcomes_populated_for_failed_merge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """G1[2]: failure paths still produce a typed MergeOutcome."""
        from tianluo.commands.merge.failure_reason import FailureReason
        from tianluo.commands.merge.result_model import MergeOutcome
        from tianluo.engine.merge.orchestrator import MergeOrchestrator

        _init_repo(tmp_path)
        # Mock _merge_single_branch to simulate a failure outcome without
        # needing a real conflict — the test focuses on outcome recording,
        # not conflict resolution mechanics.
        monkeypatch.setattr(
            MergeOrchestrator, "_merge_single_branch",
            lambda self, branch, report: "merge_conflict",
        )

        _add_feature_branch(tmp_path)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="safe")
        report = orch.execute(["feature"])

        assert report.success is False
        # Outcome populated even on failure.
        assert len(report.outcomes) == 1
        outcome = report.outcomes[0]
        assert isinstance(outcome, MergeOutcome)
        assert outcome.branch == "feature"
        assert outcome.success is False
        # Typed FailureReason rather than scraped string.
        assert isinstance(outcome.failure_reason, FailureReason)
        assert outcome.failure_reason is FailureReason.MERGE_CONFLICT
