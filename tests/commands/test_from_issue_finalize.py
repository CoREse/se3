"""Tests for synchronous (non-worktree) ``--from-issue`` finalization.

Regression coverage for the bug where the source issue lifecycle was closed
out in the cli.py wrapper based on the process exit code (cli.py:318-325):

  * In json output mode a *pause* also returns exit 0, so the very first pause
    prematurely resolved the issue (req 4, sync side).
  * A daemon/`se3 run --resume` continuation re-enters via run_flow in a NEW
    process without the wrapper, so the wrapper's finalization never fired and
    the issue was stranded in-progress (req 6, sync side).

The fix moves finalization to run_flow's true terminal branches, keyed off the
persisted ``flow.source_issue_id`` and the terminal FlowStatus (never the exit
code), via ``run._finalize_sync_source_issue``. These tests exercise that
helper directly and assert the cli.py wrapper no longer resolves on exit code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import tianluo.commands.run as run
from tianluo.cli import app
from tianluo.engine.issue_manager import IssueManager, IssueStatus
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


def _make_in_progress_issue(project_root: Path) -> str:
    """Create an issue on disk and advance it to IN_PROGRESS (as a from-issue
    run does before dispatching the flow). Returns the issue id."""
    mgr = IssueManager(project_root)
    issue = mgr.create(description="Fix the thing", type="bug")
    mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
    return issue.id


def _flow_with_source_issue(
    project_root: Path,
    issue_id: str,
    status: FlowStatus,
    is_worktree_mode: bool = False,
) -> FlowInstance:
    """Build a flow the way resume does — via a persisted-dict round-trip — so
    ``source_issue_id`` is recovered from serialized state rather than set by
    the live wrapper process. This mirrors the process-decoupled resume path."""
    src = FlowInstance(
        task_description="Fix the thing",
        status=status,
        source_issue_id=issue_id,
        is_worktree_mode=is_worktree_mode,
    )
    # Round-trip through to_dict/from_dict to prove source_issue_id survives
    # persistence — the guarantee that makes finalization process-independent.
    return FlowInstance.from_dict(src.to_dict())


# --------------------------------------------------------------------------
# _finalize_sync_source_issue — the terminal-branch helper
# --------------------------------------------------------------------------
class TestFinalizeSyncSourceIssue:
    def test_completed_sync_resolves_issue(self, tmp_path):
        issue_id = _make_in_progress_issue(tmp_path)
        flow = _flow_with_source_issue(tmp_path, issue_id, FlowStatus.COMPLETED)

        run._finalize_sync_source_issue(
            tmp_path, flow, is_worktree_mode=False, resolved=True
        )

        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED

    def test_failed_sync_reopens_issue(self, tmp_path):
        issue_id = _make_in_progress_issue(tmp_path)
        flow = _flow_with_source_issue(tmp_path, issue_id, FlowStatus.FAILED)

        run._finalize_sync_source_issue(
            tmp_path, flow, is_worktree_mode=False, resolved=False
        )

        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN

    def test_worktree_flow_is_skipped(self, tmp_path):
        # Worktree flows defer resolve to the trailing se3 merge; a COMPLETED
        # worktree flow must NOT resolve here.
        issue_id = _make_in_progress_issue(tmp_path)
        flow = _flow_with_source_issue(
            tmp_path, issue_id, FlowStatus.COMPLETED, is_worktree_mode=True
        )

        run._finalize_sync_source_issue(
            tmp_path, flow, is_worktree_mode=True, resolved=True
        )

        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS

    def test_no_source_issue_is_noop(self, tmp_path):
        flow = FlowInstance(status=FlowStatus.COMPLETED, source_issue_id=None)
        # No source issue → nothing to do, no crash.
        run._finalize_sync_source_issue(
            tmp_path, flow, is_worktree_mode=False, resolved=True
        )

    def test_non_in_progress_issue_is_not_touched(self, tmp_path):
        # Only a run that holds the issue in-progress may finalize it. An issue
        # already resolved (e.g. a second terminal pass, or manual close) must
        # not be reopened/re-transitioned.
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="Already done", type="bug")
        mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
        mgr.update_status(issue.id, IssueStatus.RESOLVED)
        flow = _flow_with_source_issue(tmp_path, issue.id, FlowStatus.FAILED)

        run._finalize_sync_source_issue(
            tmp_path, flow, is_worktree_mode=False, resolved=False
        )

        assert IssueManager(tmp_path).load(issue.id).status == IssueStatus.RESOLVED

    def test_best_effort_swallows_update_errors(self, tmp_path):
        # A raising IssueManager must never propagate out of finalization.
        issue_id = _make_in_progress_issue(tmp_path)
        flow = _flow_with_source_issue(tmp_path, issue_id, FlowStatus.COMPLETED)

        with patch.object(
            IssueManager, "update_status", side_effect=RuntimeError("boom")
        ):
            run._finalize_sync_source_issue(
                tmp_path, flow, is_worktree_mode=False, resolved=True
            )

        # Issue untouched, but crucially no exception escaped.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )

    def test_missing_issue_is_noop(self, tmp_path):
        # source_issue_id points at an issue that does not exist on disk.
        flow = FlowInstance(status=FlowStatus.FAILED, source_issue_id="999")
        run._finalize_sync_source_issue(
            tmp_path, flow, is_worktree_mode=False, resolved=False
        )


# --------------------------------------------------------------------------
# cli.py wrapper no longer finalizes on exit code (pause returns 0)
# --------------------------------------------------------------------------
class TestFromIssueWrapperNoExitCodeFinalize:
    """The wrapper must NOT resolve/reopen based on run_flow's return code —
    finalization is owned by run_flow's terminal branches. In json mode a pause
    returns 0; that must leave the issue in-progress (req 4)."""

    def _invoke(self, project_root, return_code):
        runner = CliRunner()
        with patch(
            "tianluo.commands.run.get_project_root", return_value=project_root
        ), patch(
            "tianluo.commands.run.run_flow", return_value=return_code
        ) as mock_run_flow:
            result = runner.invoke(
                app, ["run", "--from-issue", "1"], catch_exceptions=False
            )
        return result, mock_run_flow

    def test_pause_exit_zero_does_not_resolve(self, tmp_path):
        # run_flow returning 0 models BOTH a pause (json mode) and completion.
        # The wrapper transitions the OPEN issue to in-progress and must then
        # NOT resolve it — the issue stays in-progress; a real completion is
        # resolved inside run_flow, which is mocked out here.
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id
        result, mock_run_flow = self._invoke(tmp_path, 0)

        assert result.exit_code == 0
        mock_run_flow.assert_called_once()
        # The wrapper threads source_issue_id through so run_flow can finalize.
        assert mock_run_flow.call_args.kwargs["source_issue_id"] == issue_id
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )

    def test_nonzero_exit_with_persisted_flow_does_not_reopen_via_wrapper(
        self, tmp_path
    ):
        # A non-zero return from a run that DID persist flow state must not be
        # interpreted by the wrapper: FAILED→open is decided inside run_flow's
        # terminal branch (mocked out here). Because a persisted engine.json
        # carries this source_issue_id, the wrapper's early-failure self-recovery
        # does not fire, so the issue is left exactly as run_flow left it
        # (in-progress here, since the real finalize is stubbed).
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id

        def fake_run_flow(*_a, **kwargs):
            # Model a real run that reached a persisted terminal state carrying
            # the source issue — this is what makes the wrapper defer to
            # run_flow's own finalize rather than reverting.
            state_dir = tmp_path / "tianluo" / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "engine.json").write_text(
                json.dumps(
                    {
                        "flow_id": "sync-1",
                        "status": "failed",
                        "source_issue_id": kwargs.get("source_issue_id"),
                        "is_worktree_mode": False,
                    }
                )
            )
            return 1

        runner = CliRunner()
        with patch(
            "tianluo.commands.run.get_project_root", return_value=tmp_path
        ), patch("tianluo.commands.run.run_flow", side_effect=fake_run_flow):
            result = runner.invoke(
                app, ["run", "--from-issue", "1"], catch_exceptions=False
            )

        assert result.exit_code == 1
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )


class TestFromIssueEarlyDispatchFailureReverts:
    """A dispatch that fails BEFORE any flow state is persisted strands the
    source issue IN_PROGRESS with no resume/merge path to finalize it. The
    wrapper must self-recover by reverting the OPEN→IN_PROGRESS transition it
    just made — but only when no persisted flow carries the issue."""

    def test_sync_pre_flow_failure_reverts_issue_to_open(self, tmp_path):
        # run_flow returns non-zero WITHOUT persisting any engine.json (models a
        # pre-flow ConfigError / flow-load failure). No flow carries the issue,
        # so the wrapper reverts it to OPEN — otherwise re-running --from-issue
        # would be blocked forever by the in-progress gate.
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id

        runner = CliRunner()
        with patch(
            "tianluo.commands.run.get_project_root", return_value=tmp_path
        ), patch("tianluo.commands.run.run_flow", return_value=2):
            result = runner.invoke(
                app, ["run", "--from-issue", "1"], catch_exceptions=False
            )

        assert result.exit_code == 2
        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN

    def test_sync_pre_flow_failure_reverts_despite_stale_prior_engine_json(
        self, tmp_path
    ):
        # Regression: the main-repo engine.json is a single reused slot. An
        # EARLIER `--from-issue A` run of the same issue completed and left a
        # stale engine.json still carrying source_issue_id=A (overwritten only by
        # the NEXT run's first save_flow). A re-run of `--from-issue A` (allowed
        # once A is resolved/open) that fails BEFORE persisting any new flow state
        # must still revert A to OPEN — the stale slot must NOT be mistaken for
        # this dispatch's own persisted flow and suppress the revert (else A is
        # stranded IN_PROGRESS forever, blocking future --from-issue A).
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id
        state_dir = tmp_path / "tianluo" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "engine.json").write_text(
            json.dumps(
                {
                    "flow_id": "stale-prior-run",
                    "status": "completed",
                    "source_issue_id": issue_id,
                    "is_worktree_mode": False,
                }
            )
        )

        runner = CliRunner()
        with patch(
            "tianluo.commands.run.get_project_root", return_value=tmp_path
        ), patch("tianluo.commands.run.run_flow", return_value=2):
            result = runner.invoke(
                app, ["run", "--from-issue", "1"], catch_exceptions=False
            )

        assert result.exit_code == 2
        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN

    def test_worktree_fork_failure_reverts_issue_to_open(self, tmp_path):
        # run_worktree_mode returns 1 before any worktree/engine.json exists
        # (e.g. fork_worktree raised). No persisted flow carries the issue → the
        # wrapper reverts it to OPEN.
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id

        runner = CliRunner()
        with patch(
            "tianluo.commands.run.get_project_root", return_value=tmp_path
        ), patch("tianluo.commands.run.run_worktree_mode", return_value=1):
            result = runner.invoke(
                app,
                ["run", "--from-issue", "1", "--worktree"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN

    def test_worktree_completed_merge_failure_is_not_reverted(self, tmp_path):
        # A COMPLETED worktree flow whose trailing merge FAILED returns non-zero,
        # but its worktree engine.json persists the source_issue_id and the issue
        # is deliberately kept IN_PROGRESS for a retry-merge. The wrapper must NOT
        # revert it — the retry-merge path owns its resolve.
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id
        wt_path = tmp_path / "tianluo" / "worktrees" / "worktree-fix-1"

        # The current dispatch itself persists the worktree engine.json (COMPLETED)
        # and then its trailing merge fails → rc 1. Writing it inside the mocked
        # run_worktree_mode (rather than before the wrapper runs) mirrors reality:
        # this dispatch's own flow_id is NOT in the pre-dispatch snapshot, so it is
        # correctly recognized as state owned by this run and left in-progress.
        def fake_worktree(*_a, **_kw):
            _write_wt_engine(
                wt_path,
                status="completed",
                source_issue_id=issue_id,
                branch="worktree/fix-1",
            )
            return 1

        runner = CliRunner()
        with patch(
            "tianluo.commands.run.get_project_root", return_value=tmp_path
        ), patch("tianluo.commands.run.run_worktree_mode", side_effect=fake_worktree):
            result = runner.invoke(
                app,
                ["run", "--from-issue", "1", "--worktree"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        # Persisted worktree flow carries the issue → not reverted; stays
        # in-progress so `se3 merge <branch>` can still resolve it.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )


# ==========================================================================
# End-to-end integration (G4)
#
# The merge is no longer a trailing wrapper step: a worktree flow now merges
# itself back in-flow (its final merge_integrate + version_reconcile steps run
# in the main checkout under the merge lock), so "flow COMPLETED" already means
# "landed on master". The wrapper's remaining job is best-effort housekeeping +
# resolving the source issue through the same idempotent backfill choke point:
#
#     worktree from-issue first run PAUSES (issue held in-progress)
#         → a FRESH process resumes it (_resume_worktree_run, decoupled from the
#           original wrapper — the source_issue_id comes only from the persisted
#           worktree engine.json)
#         → flow reaches COMPLETED (its in-flow merge steps already landed it)
#         → _finalize_worktree_cleanup archives the worktree + resolves the issue.
#
# This proves the persisted terminal status decouples finalization from the
# wrapper process and the backfill choke point closes the issue out. A merge
# that cannot land now fails the FLOW (not the wrapper), which reopens the issue
# — covered by test_resume_to_failed_reopens_issue below.
# ==========================================================================
def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with one commit — the real repo run_merge needs
    (it inspects HEAD/current-branch and takes the merge lock). Mirrors the
    merge-backfill test fixture."""
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
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _create_ancestor_branch(root: Path, branch: str) -> None:
    """Create *branch* at HEAD so it is an ancestor of the default branch.

    Models the real post-in-flow-merge state: merge_integrate lands the branch on
    master with ``delete_merged=False``, so at finalize time the branch ref still
    exists and IS an ancestor of master — the precondition
    ``_finalize_worktree_cleanup`` now verifies before resolving the source issue
    and reporting a merge (a flow that reached COMPLETED without landing must not
    be reported as merged)."""
    subprocess.run(
        ["git", "-C", str(root), "branch", branch],
        check=True, capture_output=True,
    )


def _write_wt_engine(
    wt_path: Path, *, status, source_issue_id, branch, flow_id="wt-1", original="master"
):
    """Persist a worktree engine.json the way a real ``--worktree`` from-issue
    flow does — carrying ``source_issue_id`` so finalization is process-decoupled."""
    state_dir = wt_path / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "isolated task",
        "is_worktree_mode": True,
        "worktree_branch": branch,
        "worktree_original_branch": original,
        "worktree_path": str(wt_path),
        "source_issue_id": source_issue_id,
        "state": {"current_step_id": "implement"},
    }
    (state_dir / "engine.json").write_text(json.dumps(data))


def _patch_real_merge(monkeypatch, report, captured):
    """Let the REAL run_merge run, mocking only its git machinery.

    Mirrors ``tests/commands/test_merge_backfill_issue._patch_merge``: the
    orchestrator (which does the actual git merge / worktree cleanup) and the
    branch-existence probe are stubbed, and ``render_text`` is captured — but
    run_merge's own control flow, including the source-issue backfill, executes
    for real.
    """
    def capture_render_text(content, title=None, style=None):
        captured.append({"content": content, "title": title})

    monkeypatch.setattr(
        "tianluo.commands.merge_cmd.render_text", capture_render_text
    )

    class MockOrchestrator:
        def __init__(self, **kwargs):
            pass

        def execute(self, branches):
            return report

    monkeypatch.setattr(
        "tianluo.engine.merge.orchestrator.MergeOrchestrator", MockOrchestrator
    )
    monkeypatch.setattr(
        "tianluo.commands.merge_cmd._branch_exists", lambda _root, _branch: True
    )


def _success_report(branch):
    from tianluo.engine.merge.orchestrator import MergeReport

    return MergeReport(
        success=True,
        merged_branches=[branch],
        newly_merged_branches=[branch],
        already_ancestor_branches=[],
    )


def _failure_report(branch):
    from tianluo.engine.merge.orchestrator import MergeReport

    return MergeReport(success=False, failed_branch=branch)


class TestWorktreeFromIssuePauseResumeMergeResolvedE2E:
    """The headline end-to-end chain: pause → (new process) resume → COMPLETED
    (in-flow merge already landed) → source issue resolved by the wrapper's
    post-merge cleanup. Only the flow body (run_flow) is stubbed; the finalize
    + backfill path is real production code."""

    def test_pause_then_fresh_resume_completed_resolves_issue(
        self, tmp_path, monkeypatch
    ):
        _init_git_repo(tmp_path)
        # A from-issue run advances the issue OPEN→IN_PROGRESS before dispatch.
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/fix-the-thing-1"
        # The in-flow merge landed the branch on master (delete_merged=False), so
        # its ref exists and is an ancestor — the state finalize now verifies.
        _create_ancestor_branch(tmp_path, branch)
        wt_path = tmp_path / "tianluo" / "worktrees" / "worktree-fix-1"

        # --- First run PAUSED: only the persisted worktree engine.json survives
        # (the original wrapper process is gone). It carries the source_issue_id.
        _write_wt_engine(
            wt_path, status="paused", source_issue_id=issue_id, branch=branch
        )
        # Pause phase: the issue must NOT have been prematurely resolved — a
        # json-mode pause returns exit 0, but resolve keys off terminal status.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )

        # --- Resume in a FRESH process: run_flow (the flow body, including its
        # in-flow merge steps) is stubbed to drive the worktree flow to COMPLETED,
        # exactly as a real resumed flow would persist it. The downstream
        # _finalize_worktree_cleanup → backfill is the real production path.
        def fake_run_flow(*_a, **kwargs):
            _write_wt_engine(
                Path(kwargs["project_root"]),
                status="completed",
                source_issue_id=issue_id,
                branch=branch,
            )
            return 0

        monkeypatch.setattr("tianluo.commands.run.run_flow", fake_run_flow)

        # resume_run rediscovers the paused worktree run and re-dispatches it —
        # this models the daemon spawning `se3 run --resume` as a new process.
        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")

        assert rc == 0
        # The flow completed (its in-flow merge landed the branch) and the
        # wrapper's cleanup backfill resolved the source issue — the whole point.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )

    def test_resume_completed_resolves_even_when_branch_cleanup_skips(
        self, tmp_path, monkeypatch
    ):
        # The post-merge cleanup is best-effort: even when CleanupManager fails to
        # delete the (genuinely landed) branch, the source-issue resolve is
        # independent and still fires, and the resume returns success (the merge
        # already landed in-flow — a cleanup hiccup is never reported as a merge
        # failure). The branch DID land (it is a real ancestor of master, so the
        # finalize ancestry guard passes); only the deletion step is made to fail.
        _init_git_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/fix-the-thing-2"
        _create_ancestor_branch(tmp_path, branch)
        wt_path = tmp_path / "tianluo" / "worktrees" / "worktree-fix-2"
        _write_wt_engine(
            wt_path, status="paused", source_issue_id=issue_id, branch=branch
        )

        def fake_run_flow(*_a, **kwargs):
            _write_wt_engine(
                Path(kwargs["project_root"]),
                status="completed",
                source_issue_id=issue_id,
                branch=branch,
            )
            return 0

        def boom_cleanup(self, branches):
            raise RuntimeError("simulated branch-deletion failure")

        monkeypatch.setattr("tianluo.commands.run.run_flow", fake_run_flow)
        monkeypatch.setattr(
            "tianluo.engine.merge.cleanup.CleanupManager.delete_merged_branches",
            boom_cleanup,
        )

        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")

        assert rc == 0
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )

    def test_resume_completed_but_branch_not_landed_does_not_resolve(
        self, tmp_path, monkeypatch
    ):
        # Issue #1 regression: a worktree flow PERSISTED BEFORE the in-flow merge
        # steps existed carries a step sequence without them, so a resume after
        # the upgrade can reach COMPLETED having never merged. "COMPLETED" must
        # NOT be trusted as "landed": the branch is not an ancestor of master, so
        # finalize must NOT resolve the source issue nor report a merge, and must
        # signal failure so the operator knows the work is stranded.
        _init_git_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/never-landed-9"
        # Deliberately DO NOT create the branch as an ancestor — it never merged.
        wt_path = tmp_path / "tianluo" / "worktrees" / "worktree-never-9"
        _write_wt_engine(
            wt_path, status="paused", source_issue_id=issue_id, branch=branch
        )

        def fake_run_flow(*_a, **kwargs):
            _write_wt_engine(
                Path(kwargs["project_root"]),
                status="completed",
                source_issue_id=issue_id,
                branch=branch,
            )
            return 0

        monkeypatch.setattr("tianluo.commands.run.run_flow", fake_run_flow)

        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")

        # Signalled as not-done, and the source issue is left IN_PROGRESS (never
        # silently resolved for work still stranded in the worktree).
        assert rc != 0
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )

    def test_resume_to_failed_reopens_issue(self, tmp_path, monkeypatch):
        # req 5 on the resume path: a resumed worktree flow that ends FAILED
        # returns the source issue to OPEN, no merge attempted.
        _init_git_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/fix-the-thing-3"
        wt_path = tmp_path / "tianluo" / "worktrees" / "worktree-fix-3"
        _write_wt_engine(
            wt_path, status="paused", source_issue_id=issue_id, branch=branch
        )

        def fake_run_flow(*_a, **kwargs):
            _write_wt_engine(
                Path(kwargs["project_root"]),
                status="failed",
                source_issue_id=issue_id,
                branch=branch,
            )
            return 1

        # run_merge must never be reached on a failed flow; use a real merge
        # patch anyway so an accidental call would still be observable/harmless.
        called = {"merge": False}

        def guard_orchestrator(**_kw):
            called["merge"] = True
            raise AssertionError("run_merge must not run for a FAILED flow")

        monkeypatch.setattr("tianluo.commands.run.run_flow", fake_run_flow)
        monkeypatch.setattr(
            "tianluo.engine.merge.orchestrator.MergeOrchestrator", guard_orchestrator
        )

        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")

        assert rc == 1
        assert called["merge"] is False
        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN


class TestSyncFromIssueResumeFinalizeE2E:
    """req 6, end to end: a synchronous (non-worktree) from-issue run that was
    paused and later resumed by a FRESH process must still finalize the source
    issue. The resume re-enters via resume_run→run_flow (no wrapper); the
    terminal branch reads ``flow.source_issue_id`` — recovered purely from
    persisted state — and resolves. This drives the REAL run_flow terminal
    finalize (only the step machine is stubbed to reach COMPLETED)."""

    def _persisted_running_flow(self, issue_id):
        """Build a resumable synchronous flow carrying source_issue_id, then
        round-trip it through to_dict/from_dict so the id is recovered from
        serialized state — proving the finalize does not depend on the original
        process that set it."""
        flow = FlowInstance(
            flow_id="sync-1",
            task_description="Fix the thing",
            task_type="feature",
            status=FlowStatus.RUNNING,
            source_issue_id=issue_id,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT]
        flow.state.current_step_index = 0
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-1",
        )
        flow.state.add_step(step)
        flow.state.current_step_id = "impl-1"
        return FlowInstance.from_dict(flow.to_dict())

    def _persisted_exhausted_flow(self, issue_id):
        """Like ``_persisted_running_flow`` but the current step has already
        reached max retries, so the next run_step→FAILED drives run_flow into
        the mid-loop auto-fail branch (an early ``return 1`` that never reaches
        the bottom terminal branch).

        The step is left PENDING (not RUNNING/FAILED): the resume prologue resets
        retry_count to 0 for a RUNNING/FAILED step, which would defeat the
        max-retries branch — a PENDING step keeps its persisted retry budget."""
        flow = FlowInstance(
            flow_id="sync-fail-1",
            task_description="Fix the thing",
            task_type="feature",
            status=FlowStatus.RUNNING,
            source_issue_id=issue_id,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT]
        flow.state.current_step_index = 0
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-1",
            retry_count=3,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = "impl-1"
        return FlowInstance.from_dict(flow.to_dict())

    def test_resume_completed_resolves_source_issue(self, tmp_path):
        issue_id = _make_in_progress_issue(tmp_path)
        flow = self._persisted_running_flow(issue_id)

        mock_pm = MagicMock()
        mock_pm.load_flow.return_value = flow
        mock_pm.load_flow_by_id.return_value = flow

        mock_sm = MagicMock()
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = (
            lambda f: setattr(f, "status", FlowStatus.COMPLETED)
        )

        # No tianluo/worktrees under tmp_path → resume_run routes straight to the
        # synchronous run_flow path (models the daemon's `se3 run --resume`).
        with patch("tianluo.commands.run.PersistenceManager", return_value=mock_pm), \
             patch("tianluo.commands.run.StateMachine", return_value=mock_sm), \
             patch("tianluo.commands.run.STEP_HANDLERS", {}), \
             patch("tianluo.engine.step_renderers.render_step_output"), \
             patch("tianluo.commands.run.render_full"):
            rc = run.resume_run(tmp_path, "sync-1", output_format="cli")

        assert rc == 0
        # The fresh resume process finalized the issue off the persisted
        # source_issue_id — the wrapper never ran.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )

    def test_resume_mid_loop_max_retries_reopens_issue(self, tmp_path):
        # req 5 on the sync path, exercised through a REAL mid-loop FAILED exit
        # rather than the helper: when a step exhausts its retries run_flow sets
        # FAILED and does an early `return 1` from inside the step loop, never
        # reaching the bottom terminal branch. That early exit must still return
        # the source issue to OPEN (else it is stranded IN_PROGRESS forever and
        # re-running --from-issue is blocked by the in-progress gate).
        issue_id = _make_in_progress_issue(tmp_path)
        flow = self._persisted_exhausted_flow(issue_id)

        mock_pm = MagicMock()
        mock_pm.load_flow.return_value = flow
        mock_pm.load_flow_by_id.return_value = flow

        mock_sm = MagicMock()
        # The step fails again with retries already exhausted → auto-fail branch.
        mock_sm.run_step.return_value = StepStatus.FAILED

        with patch("tianluo.commands.run.PersistenceManager", return_value=mock_pm), \
             patch("tianluo.commands.run.StateMachine", return_value=mock_sm), \
             patch("tianluo.commands.run.STEP_HANDLERS", {}), \
             patch("tianluo.engine.step_renderers.render_step_output"), \
             patch("tianluo.commands.run.render_full"):
            rc = run.resume_run(tmp_path, "sync-fail-1", output_format="cli")

        assert rc == 1
        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN

    def test_resume_mid_loop_abort_reopens_issue(self, tmp_path):
        # The 'Abort flow' decision is the other mid-loop FAILED exit that
        # returns early from inside the step loop; it too must reopen the source
        # issue. Retries are NOT exhausted here (retry_count 0), so the failure
        # routes through the decision resolver, which we drive to 'abort'.
        issue_id = _make_in_progress_issue(tmp_path)
        flow = self._persisted_running_flow(issue_id)
        # _persisted_running_flow leaves the step RUNNING; the resume prologue
        # flips it to PENDING and zeroes retry_count — exactly the state that
        # routes a subsequent failure through the abort decision path.

        mock_pm = MagicMock()
        mock_pm.load_flow.return_value = flow
        mock_pm.load_flow_by_id.return_value = flow

        mock_sm = MagicMock()
        mock_sm.run_step.return_value = StepStatus.FAILED

        with patch("tianluo.commands.run.PersistenceManager", return_value=mock_pm), \
             patch("tianluo.commands.run.StateMachine", return_value=mock_sm), \
             patch("tianluo.commands.run.STEP_HANDLERS", {}), \
             patch(
                 "tianluo.commands.run._resolve_step_failure_action",
                 return_value=("decision", "abort"),
             ), \
             patch(
                 "tianluo.commands.run._failure_decision_to_choice", return_value=2
             ), \
             patch("tianluo.engine.step_renderers.render_step_output"), \
             patch("tianluo.commands.run.render_full"):
            rc = run.resume_run(tmp_path, "sync-1", output_format="cli")

        assert rc == 1
        assert IssueManager(tmp_path).load(issue_id).status == IssueStatus.OPEN
