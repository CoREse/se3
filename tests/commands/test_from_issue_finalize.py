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

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import se3.commands.run as run
from se3.cli import app
from se3.engine.issue_manager import IssueManager, IssueStatus
from se3.engine.models import FlowInstance, FlowStatus


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
            "se3.commands.run.get_project_root", return_value=project_root
        ), patch(
            "se3.commands.run.run_flow", return_value=return_code
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

    def test_nonzero_exit_does_not_reopen_via_wrapper(self, tmp_path):
        # A non-zero return likewise must not be interpreted by the wrapper;
        # FAILED→open is decided inside run_flow (mocked here), so the issue
        # remains in-progress and the exit code is passed through faithfully.
        issue_id = IssueManager(tmp_path).create(
            description="Fix the thing", type="bug"
        ).id
        result, _ = self._invoke(tmp_path, 1)

        assert result.exit_code == 1
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )
