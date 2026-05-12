"""Regression tests for implement_handler re-entry preservation.

These tests pin the fix for the double-bump bug in session
``20260512-225655_1b987f13``: when ``implement_handler`` is re-entered
(either as a fix iteration or as a DAG resume) after a worktree-DAG run
has already merged a version-bump commit onto the main branch, the
handler MUST NOT clobber the originally-captured ``pre_session_version``
or the previously-collected ``session_commits``. Otherwise
``version_analyze`` sees the post-bump disk version as a pristine
baseline and produces a second bump.

The two pinned invariants:

1. ``step.outputs["pre_session_version"]`` is captured exactly once
   (first entry) and preserved on every subsequent entry.
2. ``step.outputs["session_commits"]`` is computed against the
   flow-wide ``flow.baseline_commit`` (captured at flow init), not the
   per-entry HEAD, so commits merged by earlier entries remain visible
   to version_analyze.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from se3.engine.models import (
    FlowInstance,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.steps.implement import implement_handler


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        check=False,
    )


def _git_ok(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = _git(repo, *args)
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result


@pytest.fixture
def reentry_repo(tmp_path):
    """Git repo seeded with pyproject.toml at 5.1.0 and one baseline commit.

    Returns a tuple (project_root, baseline_sha) representing the state
    of the repo at flow init time — the version_analyze baseline.
    """
    repo = tmp_path / "project"
    repo.mkdir()
    if _git(repo, "--version").returncode != 0:
        pytest.skip("git not available")
    _git_ok(repo, "init")
    _git_ok(repo, "config", "user.email", "test@example.com")
    _git_ok(repo, "config", "user.name", "Test User")
    _git_ok(repo, "config", "commit.gpgsign", "false")
    _git_ok(repo, "config", "init.defaultBranch", "master")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "5.1.0"\n'
    )
    (repo / "README.md").write_text("hello\n")
    _git_ok(repo, "add", "pyproject.toml", "README.md")
    _git_ok(repo, "commit", "-m", "baseline at 5.1.0")
    baseline = _git_ok(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, baseline


def _make_flow(project_root: Path, baseline_sha: str) -> FlowInstance:
    """Build a FlowInstance pinned to ``project_root`` with the flow-wide
    baseline pre-recorded (matching ``state_machine._record_baseline_commit``).
    """
    flow = FlowInstance(
        task_description="add feature X",
        baseline_commit=baseline_sha,
    )
    # implement_handler reads project_root via flow.change_path.parent,
    # so synthesize a change_path that points inside the repo.
    flow.change_path = project_root / ".se3-change-marker"
    return flow


def _make_implement_step() -> Step:
    return Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "add feature X",
            "task_type": "feature",
            "task_groups": [],
            "design_doc": {},
            "spec_content": {},
        },
    )


class TestReentryPreservesPreSessionVersion:
    """When a worktree-DAG entry merged a version bump on first entry and
    the handler is re-entered (fix iteration / DAG resume) with disk now
    at the bumped version, ``pre_session_version`` MUST stay pinned to
    the original pre-implement value (5.1.0), not be overwritten to the
    post-bump disk value (5.2.0).
    """

    def test_fix_iteration_preserves_pre_session_version(self, reentry_repo):
        project_root, baseline_sha = reentry_repo
        flow = _make_flow(project_root, baseline_sha)
        step = _make_implement_step()

        # --- First entry: simulate the body of implement_handler running
        # without actually calling out to the LLM. We only need the
        # bookkeeping at the top of the handler to fire, so short-circuit
        # the real work by mocking out the inner machinery.
        with patch(
            "se3.engine.steps.implement._run_single_llm_call",
            return_value=StepStatus.COMPLETED,
        ), patch(
            "se3.engine.steps.implement._resolve_files_changed",
        ):
            # Treat the first call as a fix-iteration to keep it
            # short-circuited (no group execution required).
            step.inputs["is_fix_iteration"] = True
            step.inputs["fix_instructions"] = "first pass"
            step.inputs["fix_iteration"] = 1
            implement_handler(step, flow)

        # After first entry: pre_session_version should reflect disk @5.1.0
        assert step.outputs["pre_session_version"] == "5.1.0"

        # --- Simulate a worktree-DAG group bumping the version on main
        (project_root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "5.2.0"\n'
        )
        _git_ok(project_root, "add", "pyproject.toml")
        _git_ok(project_root, "commit", "-m", "bump version to 5.2.0")

        # --- Re-entry as a fix iteration after the bump commit
        with patch(
            "se3.engine.steps.implement._run_single_llm_call",
            return_value=StepStatus.COMPLETED,
        ), patch(
            "se3.engine.steps.implement._resolve_files_changed",
        ):
            step.inputs["fix_iteration"] = 2
            step.inputs["fix_instructions"] = "second pass"
            implement_handler(step, flow)

        # The pinned invariant: pre_session_version is still 5.1.0, NOT
        # overwritten to disk's current 5.2.0.
        assert step.outputs["pre_session_version"] == "5.1.0", (
            "implement_handler must capture pre_session_version exactly "
            "once; re-entry overwrote it with the post-bump disk version, "
            "which would let version_analyze double-bump."
        )

    def test_session_commits_use_flow_baseline_not_per_entry_head(
        self, reentry_repo,
    ):
        """The fix-iteration re-entry must surface commits merged by an
        earlier entry. Concretely, after a worktree merge-back has added a
        version-bump commit on main, _collect_session_commits called from
        the fix-iteration return path must still include that commit —
        proving it diffed against ``flow.baseline_commit`` rather than the
        post-bump HEAD.
        """
        project_root, baseline_sha = reentry_repo
        flow = _make_flow(project_root, baseline_sha)
        step = _make_implement_step()

        # First entry – pre_session_version captured at 5.1.0.
        with patch(
            "se3.engine.steps.implement._run_single_llm_call",
            return_value=StepStatus.COMPLETED,
        ), patch(
            "se3.engine.steps.implement._resolve_files_changed",
        ):
            step.inputs["is_fix_iteration"] = True
            step.inputs["fix_instructions"] = "first pass"
            step.inputs["fix_iteration"] = 1
            implement_handler(step, flow)

        # Simulate worktree group merging a version bump commit onto main.
        (project_root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "5.2.0"\n'
        )
        _git_ok(project_root, "add", "pyproject.toml")
        _git_ok(project_root, "commit", "-m", "bump version to 5.2.0")

        # Re-entry: the fix-iteration path must recompute session_commits
        # against flow.baseline_commit (the original pre-implement HEAD).
        with patch(
            "se3.engine.steps.implement._run_single_llm_call",
            return_value=StepStatus.COMPLETED,
        ), patch(
            "se3.engine.steps.implement._resolve_files_changed",
        ):
            step.inputs["fix_iteration"] = 2
            step.inputs["fix_instructions"] = "second pass"
            implement_handler(step, flow)

        subjects = [c["subject"] for c in step.outputs["session_commits"]]
        assert "bump version to 5.2.0" in subjects, (
            "session_commits must include the bump commit merged between "
            "entries; the handler must diff against flow.baseline_commit, "
            "not the post-bump per-entry HEAD."
        )
        # Verify the commit was actually attributed to pyproject.toml.
        for commit in step.outputs["session_commits"]:
            if commit["subject"] == "bump version to 5.2.0":
                assert "pyproject.toml" in commit["files"]
                break
