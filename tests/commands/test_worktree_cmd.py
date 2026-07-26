"""CLI tests for `se3 worktree gc` (the manual GC trigger surface).

These cover the thin render/exit-code shell around the engine GC core
(``gc_worktree_runs``), which is itself exercised against real git fixtures in
``tests/engine/test_worktree_gc.py``. Here the core is patched to return crafted
reports so we can assert precisely on:

- the dry-run vs real-delete plumbing (dry_run flag + --max-age-hours → seconds);
- the three-part report rendering (archived + reclaimed space / retained unmerged
  branches with a loud warning / skipped+errors);
- exit-code mapping (non-zero iff any run errored).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tianluo.cli import app
from tianluo.engine.merge.worktree_gc import WorktreeGCReport

runner = CliRunner()


@pytest.fixture
def project_root(tmp_path):
    # A bare directory is enough; get_project_root is patched to return it.
    return tmp_path


def _invoke(args, project_root, report):
    """Invoke `worktree gc` with the engine core + project root patched.

    The module-level Rich console is swapped for a wide one so table columns do
    not wrap mid-token and break substring assertions. ``gc_worktree_runs`` is
    replaced by a mock returning *report*; the mock is returned for call-arg
    assertions.
    """
    gc_mock = MagicMock(return_value=report)
    wide_console = Console(width=200)
    with patch(
        "tianluo.commands.run.get_project_root", return_value=project_root
    ), patch("tianluo.commands.worktree_cmd.gc_worktree_runs", gc_mock), patch(
        "tianluo.commands.worktree_cmd.console", wide_console
    ):
        result = runner.invoke(app, ["worktree", "gc"] + args)
    return result, gc_mock


def test_help_lists_gc(project_root):
    """`se3 worktree gc --help` renders without import errors."""
    result = runner.invoke(app, ["worktree", "gc", "--help"])
    assert result.exit_code == 0
    assert "--max-age-hours" in result.output
    assert "--dry-run" in result.output


def test_dry_run_passes_flag_and_default_age(project_root):
    """--dry-run threads dry_run=True and the default 24h age to the core."""
    report = WorktreeGCReport(
        archived=[("wt-a", None, 50 * 1024 * 1024)],
        reclaimed_bytes=50 * 1024 * 1024,
    )
    result, gc_mock = _invoke(["--dry-run"], project_root, report)

    assert result.exit_code == 0
    gc_mock.assert_called_once()
    _, kwargs = gc_mock.call_args
    assert kwargs["dry_run"] is True
    assert kwargs["max_age_seconds"] == pytest.approx(24 * 3600.0)
    # Dry run must not claim to have written archives.
    assert "DRY RUN" in result.output
    assert "50.0 MB" in result.output
    assert "Reclaimed space" in result.output


def test_max_age_hours_converted_to_seconds(project_root):
    """--max-age-hours N reaches the core as N*3600 seconds."""
    result, gc_mock = _invoke(
        ["--max-age-hours", "48"], project_root, WorktreeGCReport()
    )
    assert result.exit_code == 0
    _, kwargs = gc_mock.call_args
    assert kwargs["max_age_seconds"] == pytest.approx(48 * 3600.0)
    assert kwargs["dry_run"] is False


def test_report_three_sections_and_unmerged_warning(project_root):
    """The rendered report shows archived, retained-unmerged (with a loud
    warning), and skipped sections — all three."""
    report = WorktreeGCReport(
        archived=[("wt-merged", Path("/p/se3/worktrees/.archive/wt-merged-1"), 1024)],
        retained_unmerged=[
            ("feat-x", "master", "branch has commits not in HEAD (unmerged)")
        ],
        reclaimed_bytes=1024,
        skipped=[("wt-nobranch", "no worktree_branch recorded in engine.json")],
    )
    result, _ = _invoke([], project_root, report)

    assert result.exit_code == 0
    # Section 1: archived + reclaimed space.
    assert "Archived" in result.output
    assert "wt-merged" in result.output
    assert "Reclaimed space" in result.output
    # Section 2: unmerged retention with a prominent warning.
    assert "WARNING" in result.output
    assert "Retained unmerged branches" in result.output
    assert "feat-x" in result.output
    # Section 3: skipped.
    assert "Skipped" in result.output
    assert "wt-nobranch" in result.output


def test_errors_map_to_nonzero_exit(project_root):
    """Any errored run makes the command exit non-zero."""
    report = WorktreeGCReport(
        errors=[("wt-bad", "archive to se3/worktrees/.archive/ failed: OSError")],
    )
    result, _ = _invoke([], project_root, report)

    assert result.exit_code == 1
    assert "Errors" in result.output
    assert "wt-bad" in result.output


def test_empty_report_reports_nothing_and_exits_zero(project_root):
    """An empty sweep exits 0 and says nothing matched."""
    result, _ = _invoke([], project_root, WorktreeGCReport())
    assert result.exit_code == 0
    assert "No worktree runs matched" in result.output
