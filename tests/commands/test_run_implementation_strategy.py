"""CLI and run-orchestration coverage for implementation strategy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import tianluo.commands.run as run
from tianluo.cli import app
from tianluo.engine.issue_manager import IssueManager


runner = CliRunner()


def _invoke(args, project_root: Path):
    run_flow = MagicMock(return_value=0)
    run_worktree = MagicMock(return_value=0)
    resume = MagicMock(return_value=0)
    with patch(
        "tianluo.commands.run.get_project_root", return_value=project_root
    ), patch("tianluo.commands.run.run_flow", run_flow), patch(
        "tianluo.commands.run.run_worktree_mode", run_worktree
    ), patch("tianluo.commands.run.resume_run", resume):
        result = runner.invoke(app, ["run", *args])
    return result, run_flow, run_worktree, resume


@pytest.mark.parametrize("strategy", ["auto", "direct", "planned"])
def test_cli_accepts_and_forwards_strategy(tmp_path: Path, strategy: str):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--implementation-strategy", strategy],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["implementation_strategy"] == strategy


def test_cli_rejects_unknown_strategy(tmp_path: Path):
    result, run_flow, run_worktree, resume = _invoke(
        ["task", "--implementation-strategy", "automatic"],
        tmp_path,
    )

    assert result.exit_code == 1
    assert "automatic" in result.output
    run_flow.assert_not_called()
    run_worktree.assert_not_called()
    resume.assert_not_called()


def test_omitted_strategy_defers_to_project_config(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(["task"], tmp_path)

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["implementation_strategy"] is None


def test_preset_does_not_consume_independent_strategy_option(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(
        ["--preset", "doc-sync", "--implementation-strategy", "direct"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["task_type"] == "feature"
    assert run_flow.call_args.kwargs["implementation_strategy"] == "direct"


def test_worktree_cli_forwards_strategy(tmp_path: Path):
    result, _run_flow, worktree, _resume = _invoke(
        ["task", "--worktree", "--implementation-strategy", "auto"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert worktree.call_args.kwargs["implementation_strategy"] == "auto"


def test_from_issue_worktree_forwards_strategy(tmp_path: Path):
    issue = IssueManager(tmp_path).create(description="Fix it", type="bug")
    result, _run_flow, worktree, _resume = _invoke(
        [
            "--from-issue",
            issue.id,
            "--worktree",
            "--implementation-strategy",
            "planned",
        ],
        tmp_path,
    )

    assert result.exit_code == 0
    assert worktree.call_args.kwargs["source_issue_id"] == issue.id
    assert worktree.call_args.kwargs["implementation_strategy"] == "planned"


def test_from_issue_sync_forwards_strategy(tmp_path: Path):
    issue = IssueManager(tmp_path).create(description="Fix it", type="bug")
    result, run_flow, _worktree, _resume = _invoke(
        ["--from-issue", issue.id, "--implementation-strategy", "direct"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["source_issue_id"] == issue.id
    assert run_flow.call_args.kwargs["implementation_strategy"] == "direct"


def test_resume_does_not_forward_a_new_strategy(tmp_path: Path):
    result, run_flow, worktree, resume = _invoke(
        ["--flow-id", "flow-1", "--implementation-strategy", "direct"],
        tmp_path,
    )

    assert result.exit_code == 0
    resume.assert_called_once()
    assert "implementation_strategy" not in resume.call_args.kwargs
    run_flow.assert_not_called()
    worktree.assert_not_called()


def test_worktree_wrapper_forwards_strategy_to_new_flow(tmp_path: Path):
    worktree_path = tmp_path / "tianluo" / "worktrees" / "wt"
    (worktree_path / "tianluo" / "state").mkdir(parents=True)
    with patch(
        "tianluo.engine.worktree.get_current_branch", return_value="main"
    ), patch(
        "tianluo.engine.worktree.fork_worktree", return_value=worktree_path
    ), patch("tianluo.commands.run.run_flow", return_value=2) as run_flow:
        result = run.run_worktree_mode(
            project_root=tmp_path,
            task="task",
            implementation_strategy="direct",
        )

    assert result == 2
    assert run_flow.call_args.kwargs["implementation_strategy"] == "direct"


@patch("tianluo.commands.run.PersistenceManager")
@patch("tianluo.commands.run.StateMachine")
@patch("tianluo.commands.run.STEP_HANDLERS", {})
def test_run_resume_keeps_persisted_strategy_and_never_recreates_flow(
    mock_sm_class, mock_pm_class, tmp_path: Path
):
    """A resume request carrying a different strategy must not re-decide.

    The fresh-process resume path loads the persisted flow and only
    ``create_flow`` (which is where a strategy request takes effect) runs for
    brand-new flows — so a hot config change or a stray explicit flag can
    never rewrite the strategy a flow was already executing under.
    """
    from tianluo.commands.run import run_flow
    from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType

    flow = FlowInstance(
        flow_id="resume-strategy",
        task_description="persisted direct flow",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.context["requested_implementation_strategy"] = "direct"
    flow.state.context["effective_implementation_strategy"] = "direct"
    flow.state.context["strategy_reason"] = "persisted before restart"
    flow.state.selected_steps = [
        StepType.ANALYZE,
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
    ]
    flow.state.current_step_index = 1
    step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.RUNNING)
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id

    mock_pm = MagicMock()
    mock_pm_class.return_value = mock_pm
    mock_pm._peek_active_flow_id.return_value = "resume-strategy"
    mock_pm.load_flow_by_id.return_value = flow

    mock_sm = MagicMock()
    mock_sm_class.return_value = mock_sm
    mock_sm.run_step.return_value = StepStatus.COMPLETED
    mock_sm.transition_to_next.side_effect = (
        lambda current: setattr(current, "status", FlowStatus.COMPLETED)
    )

    with patch("tianluo.engine.step_renderers.render_step_output"), patch(
        "tianluo.commands.run.render_full"
    ):
        run_flow(
            project_root=tmp_path,
            flow_id="resume-strategy",
            implementation_strategy="planned",
        )

    mock_sm.create_flow.assert_not_called()
    assert flow.state.context["effective_implementation_strategy"] == "direct"
    assert flow.state.context["strategy_reason"] == "persisted before restart"
