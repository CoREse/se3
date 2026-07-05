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

import se3.commands.run as run
from se3.cli import app
from se3.engine.issue_manager import IssueManager, IssueStatus
from se3.engine.models import (
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


# ==========================================================================
# End-to-end integration (G4)
#
# The unit/integration tests above (and G3's) stop at the run_merge boundary —
# they mock run_merge to a bare return code, so the "merge succeeded → source
# issue resolved" step (which lives INSIDE run_merge) never actually fires. G4
# strings the WHOLE chain together with the REAL run_merge (only its git
# machinery — MergeOrchestrator / branch-exists — is mocked, exactly as the
# merge-backfill tests do), so the resolve is driven by production code end to
# end:
#
#     worktree from-issue first run PAUSES (issue held in-progress)
#         → a FRESH process resumes it (_resume_worktree_run, decoupled from the
#           original wrapper — the source_issue_id comes only from the persisted
#           worktree engine.json)
#         → flow reaches COMPLETED
#         → the REAL trailing merge (_finalize_worktree_merge → run_merge) lands
#         → run_merge's backfill resolves the source issue.
#
# This is the single test that proves the three groups compose: the persisted
# terminal status decouples finalization from the wrapper process (G3), and the
# resolve choke point in run_merge (G2) actually closes the issue out.
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


def _write_wt_engine(
    wt_path: Path, *, status, source_issue_id, branch, flow_id="wt-1", original="master"
):
    """Persist a worktree engine.json the way a real ``--worktree`` from-issue
    flow does — carrying ``source_issue_id`` so finalization is process-decoupled."""
    state_dir = wt_path / "se3" / "state"
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
        "se3.commands.merge_cmd.render_text", capture_render_text
    )

    class MockOrchestrator:
        def __init__(self, **kwargs):
            pass

        def execute(self, branches):
            return report

    monkeypatch.setattr(
        "se3.engine.merge.orchestrator.MergeOrchestrator", MockOrchestrator
    )
    monkeypatch.setattr(
        "se3.commands.merge_cmd._branch_exists", lambda _root, _branch: True
    )


def _success_report(branch):
    from se3.engine.merge.orchestrator import MergeReport

    return MergeReport(
        success=True,
        merged_branches=[branch],
        newly_merged_branches=[branch],
        already_ancestor_branches=[],
    )


def _failure_report(branch):
    from se3.engine.merge.orchestrator import MergeReport

    return MergeReport(success=False, failed_branch=branch)


class TestWorktreeFromIssuePauseResumeMergeResolvedE2E:
    """The headline end-to-end chain: pause → (new process) resume → COMPLETED
    → real merge-back → source issue resolved. Nothing here mocks run_merge or
    the finalize helpers — only the flow body (run_flow) and the low-level git
    merge machinery are stubbed."""

    def test_pause_then_fresh_resume_merge_success_resolves_issue(
        self, tmp_path, monkeypatch
    ):
        _init_git_repo(tmp_path)
        # A from-issue run advances the issue OPEN→IN_PROGRESS before dispatch.
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/fix-the-thing-1"
        wt_path = tmp_path / "se3" / "worktrees" / "worktree-fix-1"

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

        # --- Resume in a FRESH process: run_flow (the flow body) is stubbed to
        # drive the worktree flow to COMPLETED, exactly as a real resumed flow
        # would persist it. Everything downstream (_finalize_worktree_merge →
        # run_merge → backfill) is the real production code path.
        def fake_run_flow(*_a, **kwargs):
            _write_wt_engine(
                Path(kwargs["project_root"]),
                status="completed",
                source_issue_id=issue_id,
                branch=branch,
            )
            return 0

        captured: list[dict] = []
        _patch_real_merge(monkeypatch, _success_report(branch), captured)
        monkeypatch.setattr("se3.commands.run.run_flow", fake_run_flow)

        # resume_run rediscovers the paused worktree run and re-dispatches it —
        # this models the daemon spawning `se3 run --resume` as a new process.
        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")

        assert rc == 0
        # The merge landed and run_merge's backfill resolved the source issue —
        # the whole point of the chain. It surfaces the resolve in the output.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )
        merge_body = "\n".join(
            e["content"] for e in captured if e["title"] == "Merge Complete"
        )
        assert f"Resolved source issue #{issue_id}" in merge_body

    def test_pause_then_resume_merge_failure_keeps_in_progress(
        self, tmp_path, monkeypatch
    ):
        # req 1 merge-failure half, exercised through the real resume→merge path:
        # a COMPLETED flow whose trailing merge FAILS must leave the issue
        # in-progress (so the retry-merge path can still resolve it) and tell the
        # operator the branch is preserved and how to retry.
        _init_git_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/fix-the-thing-2"
        wt_path = tmp_path / "se3" / "worktrees" / "worktree-fix-2"
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

        errors: list[str] = []

        def capture_error(msg, *_a, **_kw):
            errors.append(str(msg))

        captured: list[dict] = []
        _patch_real_merge(monkeypatch, _failure_report(branch), captured)
        monkeypatch.setattr("se3.commands.run.run_flow", fake_run_flow)
        monkeypatch.setattr("se3.commands.run.display_error", capture_error)

        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")

        assert rc != 0
        # Merge failed → issue stays in-progress (retry-merge can resolve later).
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.IN_PROGRESS
        )
        msg = "\n".join(errors)
        assert "in-progress" in msg
        assert "preserved" in msg
        assert f"se3 merge {branch}" in msg
        assert f"#{issue_id}" in msg

    def test_resume_to_failed_reopens_issue(self, tmp_path, monkeypatch):
        # req 5 on the resume path: a resumed worktree flow that ends FAILED
        # returns the source issue to OPEN, no merge attempted.
        _init_git_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path)
        branch = "worktree/fix-the-thing-3"
        wt_path = tmp_path / "se3" / "worktrees" / "worktree-fix-3"
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

        monkeypatch.setattr("se3.commands.run.run_flow", fake_run_flow)
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator", guard_orchestrator
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

        # No se3/worktrees under tmp_path → resume_run routes straight to the
        # synchronous run_flow path (models the daemon's `se3 run --resume`).
        with patch("se3.commands.run.PersistenceManager", return_value=mock_pm), \
             patch("se3.commands.run.StateMachine", return_value=mock_sm), \
             patch("se3.commands.run.STEP_HANDLERS", {}), \
             patch("se3.engine.step_renderers.render_step_output"), \
             patch("se3.commands.run.render_full"):
            rc = run.resume_run(tmp_path, "sync-1", output_format="cli")

        assert rc == 0
        # The fresh resume process finalized the issue off the persisted
        # source_issue_id — the wrapper never ran.
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )
