"""CLI and run-orchestration coverage for the PLAN grouping options.

Replaces ``test_run_implementation_strategy.py``: the implementation-strategy
routing axis was retired in favour of ``--plan-decomposition`` /
``--plan-granularity``, with the old option kept for one version as a mapped
alias.
"""

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


# ---------------------------------------------------------------------------
# --plan-decomposition / --plan-granularity acceptance + forwarding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decomposition", ["capability", "granular"])
def test_cli_accepts_and_forwards_decomposition(tmp_path: Path, decomposition: str):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--plan-decomposition", decomposition],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["plan_decomposition"] == decomposition
    assert run_flow.call_args.kwargs["plan_granularity"] is None


@pytest.mark.parametrize("granularity", ["auto", "single", "conservative"])
def test_cli_accepts_and_forwards_granularity(tmp_path: Path, granularity: str):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--plan-granularity", granularity],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["plan_granularity"] == granularity
    assert run_flow.call_args.kwargs["plan_decomposition"] is None


def test_cli_forwards_both_options_together(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--plan-decomposition", "capability", "--plan-granularity", "single"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["plan_decomposition"] == "capability"
    assert run_flow.call_args.kwargs["plan_granularity"] == "single"


def test_cli_rejects_unknown_decomposition(tmp_path: Path):
    result, run_flow, run_worktree, resume = _invoke(
        ["task", "--plan-decomposition", "coarse"],
        tmp_path,
    )

    assert result.exit_code == 1
    assert "coarse" in result.output
    assert "capability" in result.output
    assert "granular" in result.output
    run_flow.assert_not_called()
    run_worktree.assert_not_called()
    resume.assert_not_called()


def test_cli_rejects_unknown_granularity(tmp_path: Path):
    result, run_flow, run_worktree, resume = _invoke(
        ["task", "--plan-granularity", "coarsest"],
        tmp_path,
    )

    assert result.exit_code == 1
    assert "coarsest" in result.output
    assert "conservative" in result.output
    run_flow.assert_not_called()
    run_worktree.assert_not_called()
    resume.assert_not_called()


def test_omitted_options_defer_to_project_config(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(["task"], tmp_path)

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["plan_decomposition"] is None
    assert run_flow.call_args.kwargs["plan_granularity"] is None


def test_preset_does_not_consume_independent_plan_mode_options(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(
        ["--preset", "doc-sync", "--plan-granularity", "single"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["task_type"] == "feature"
    assert run_flow.call_args.kwargs["plan_granularity"] == "single"


def test_worktree_cli_forwards_plan_mode(tmp_path: Path):
    result, _run_flow, worktree, _resume = _invoke(
        ["task", "--worktree", "--plan-decomposition", "granular"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert worktree.call_args.kwargs["plan_decomposition"] == "granular"
    assert worktree.call_args.kwargs["plan_granularity"] is None


def test_from_issue_worktree_forwards_plan_mode(tmp_path: Path):
    issue = IssueManager(tmp_path).create(description="Fix it", type="bug")
    result, _run_flow, worktree, _resume = _invoke(
        [
            "--from-issue",
            issue.id,
            "--worktree",
            "--plan-granularity",
            "conservative",
        ],
        tmp_path,
    )

    assert result.exit_code == 0
    assert worktree.call_args.kwargs["source_issue_id"] == issue.id
    assert worktree.call_args.kwargs["plan_granularity"] == "conservative"


def test_from_issue_sync_forwards_plan_mode(tmp_path: Path):
    issue = IssueManager(tmp_path).create(description="Fix it", type="bug")
    result, run_flow, _worktree, _resume = _invoke(
        ["--from-issue", issue.id, "--plan-decomposition", "granular"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["source_issue_id"] == issue.id
    assert run_flow.call_args.kwargs["plan_decomposition"] == "granular"


def test_resume_does_not_forward_plan_mode(tmp_path: Path):
    """A resumed flow keeps the grouping it already entered."""
    result, run_flow, worktree, resume = _invoke(
        ["--flow-id", "flow-1", "--plan-granularity", "single"],
        tmp_path,
    )

    assert result.exit_code == 0
    resume.assert_called_once()
    assert "plan_decomposition" not in resume.call_args.kwargs
    assert "plan_granularity" not in resume.call_args.kwargs
    run_flow.assert_not_called()
    worktree.assert_not_called()


def test_worktree_wrapper_forwards_plan_mode_to_new_flow(tmp_path: Path):
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
            plan_decomposition="granular",
            plan_granularity="single",
        )

    assert result == 2
    assert run_flow.call_args.kwargs["plan_decomposition"] == "granular"
    assert run_flow.call_args.kwargs["plan_granularity"] == "single"


def test_run_module_no_longer_speaks_the_retired_vocabulary():
    source = Path(run.__file__).read_text(encoding="utf-8")
    assert "implementation_strategy" not in source


# ---------------------------------------------------------------------------
# --implementation-strategy: retired, mapped for one version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strategy,expected_decomposition,expected_granularity",
    [
        ("direct", None, "single"),
        ("planned", "granular", None),
        ("auto", None, None),
    ],
)
def test_legacy_strategy_maps_onto_plan_mode(
    tmp_path: Path, strategy, expected_decomposition, expected_granularity
):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--implementation-strategy", strategy],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["plan_decomposition"] == expected_decomposition
    assert run_flow.call_args.kwargs["plan_granularity"] == expected_granularity


def test_legacy_strategy_prints_deprecation_notice(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--implementation-strategy", "direct"],
        tmp_path,
    )

    assert result.exit_code == 0
    # Rich wraps the panel body, so match on tokens rather than a sentence.
    assert "--plan-decomposition" in result.output
    assert "deprecated" in result.output.lower()
    run_flow.assert_called_once()


def test_explicit_new_option_wins_over_legacy_strategy(tmp_path: Path):
    result, run_flow, _worktree, _resume = _invoke(
        ["task", "--implementation-strategy", "direct", "--plan-granularity", "auto"],
        tmp_path,
    )

    assert result.exit_code == 0
    assert run_flow.call_args.kwargs["plan_granularity"] == "auto"


def test_cli_still_rejects_unknown_legacy_strategy(tmp_path: Path):
    result, run_flow, run_worktree, resume = _invoke(
        ["task", "--implementation-strategy", "automatic"],
        tmp_path,
    )

    assert result.exit_code == 1
    assert "automatic" in result.output
    run_flow.assert_not_called()
    run_worktree.assert_not_called()
    resume.assert_not_called()
