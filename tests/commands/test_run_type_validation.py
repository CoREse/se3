"""CLI entry-check tests for ``luo run --type``.

Two behaviours are locked down here:

- An unknown explicit ``--type`` is rejected at the CLI, not silently absorbed
  by ``get_default_step_sequence``'s fallback-to-feature lookup (which must
  stay lenient for retired types persisted in old flows).
- Omitting ``--type`` leaves the type *pending* so analyze's classification
  wins, instead of pinning every unflagged run to a concrete default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tianluo.cli import EXPLICIT_TASK_TYPES, app

runner = CliRunner()


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


def _invoke(args, project_root):
    rf = MagicMock(return_value=0)
    wt = MagicMock(return_value=0)
    with patch("tianluo.commands.run.get_project_root", return_value=project_root), patch(
        "tianluo.commands.run.run_flow", rf
    ), patch("tianluo.commands.run.run_worktree_mode", wt):
        result = runner.invoke(app, ["run"] + args)
    return result, rf, wt


def test_unknown_type_is_rejected(project_root):
    result, rf, _ = _invoke(["do a thing", "--type", "nonsense"], project_root)

    assert result.exit_code == 1
    assert "nonsense" in result.output
    rf.assert_not_called()


def test_discovery_is_not_an_explicit_type(project_root):
    """``discovery`` is a run mode reached via --discover, never via --type."""
    result, rf, _ = _invoke(["do a thing", "--type", "discovery"], project_root)

    assert result.exit_code == 1
    rf.assert_not_called()


@pytest.mark.parametrize("task_type", EXPLICIT_TASK_TYPES)
def test_valid_types_reach_run_flow(project_root, task_type):
    result, rf, _ = _invoke(["do a thing", "--type", task_type], project_root)

    assert result.exit_code == 0
    assert rf.call_args.kwargs["task_type"] == task_type


def test_omitted_type_is_pending(project_root):
    """Without --type the flow starts pending so analyze's answer is honoured."""
    result, rf, _ = _invoke(["do a thing"], project_root)

    assert result.exit_code == 0
    assert rf.call_args.kwargs["task_type"] == "pending"


def test_explicit_pending_sentinel_is_accepted(project_root):
    """The daemon/WebUI "auto" path always passes ``--type pending`` verbatim.

    The spawner appends ``--type <task_type>`` unconditionally and the New Task
    form's default option carries the ``pending`` sentinel, so rejecting it as
    "not one of the five types" would break every WebUI-published auto task.
    """
    result, rf, _ = _invoke(["do a thing", "--type", "pending"], project_root)

    assert result.exit_code == 0
    assert rf.call_args.kwargs["task_type"] == "pending"


def test_explicit_pending_does_not_conflict_with_preset(project_root):
    """``--type pending`` means "no type", so it is not a --preset conflict."""
    prompts_dir = project_root / "tianluo" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "doc-sync.md").write_text("Sync the docs.", encoding="utf-8")
    (project_root / "tianluo.yaml").write_text(
        "presets:\n"
        "  doc-sync:\n"
        "    type: review\n"
        "    prompt_file: tianluo/prompts/doc-sync.md\n",
        encoding="utf-8",
    )

    result, rf, _ = _invoke(
        ["--preset", "doc-sync", "--type", "pending"], project_root
    )

    assert result.exit_code == 0
    assert rf.call_args.kwargs["task_type"] == "review"


def test_discover_flag_bypasses_the_explicit_type_check(project_root):
    result, rf, _ = _invoke(["--discover", "explore something"], project_root)

    assert result.exit_code == 0
    assert rf.call_args.kwargs["task_type"] == "discovery"


def test_preset_type_bypasses_the_explicit_type_check(project_root, tmp_path):
    """A preset supplies its own type from disk; it is not a user-typed value."""
    prompts_dir = project_root / "tianluo" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "doc-sync.md").write_text("Sync the docs.", encoding="utf-8")
    (project_root / "tianluo.yaml").write_text(
        "presets:\n"
        "  doc-sync:\n"
        "    type: review\n"
        "    prompt_file: tianluo/prompts/doc-sync.md\n",
        encoding="utf-8",
    )

    result, rf, _ = _invoke(["--preset", "doc-sync"], project_root)

    assert result.exit_code == 0
    assert rf.call_args.kwargs["task_type"] == "review"
