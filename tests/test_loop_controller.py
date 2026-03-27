"""Tests for LoopController and persistent extra prompt."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.llm_caller import (
    clear_extra_prompt,
    clear_persistent_extra_prompt,
    get_extra_prompt,
    set_extra_prompt,
)
from se3.engine.loop_controller import LoopController


# ── Persistent extra prompt tests ──


class TestExtraPromptTransient:
    """Verify transient (default) extra prompt behavior is unchanged."""

    def setup_method(self):
        clear_extra_prompt()

    def teardown_method(self):
        clear_extra_prompt()

    def test_set_and_get(self):
        set_extra_prompt("hello")
        assert get_extra_prompt() == "hello"

    def test_none_by_default(self):
        assert get_extra_prompt() is None

    def test_clear(self):
        set_extra_prompt("hello")
        clear_extra_prompt()
        assert get_extra_prompt() is None


class TestExtraPromptPersistent:
    """Verify persistent extra prompt behavior."""

    def setup_method(self):
        clear_extra_prompt()

    def teardown_method(self):
        clear_extra_prompt()

    def test_set_persistent(self):
        set_extra_prompt("loop context", persistent=True)
        assert get_extra_prompt() == "loop context"

    def test_persistent_survives_clear_transient(self):
        """Clearing transient does not affect persistent."""
        set_extra_prompt("loop context", persistent=True)
        set_extra_prompt(None, persistent=False)
        assert get_extra_prompt() == "loop context"

    def test_clear_persistent_only(self):
        set_extra_prompt("transient msg")
        set_extra_prompt("persistent msg", persistent=True)
        clear_persistent_extra_prompt()
        assert get_extra_prompt() == "transient msg"

    def test_clear_all(self):
        set_extra_prompt("transient msg")
        set_extra_prompt("persistent msg", persistent=True)
        clear_extra_prompt()
        assert get_extra_prompt() is None


class TestExtraPromptCoexistence:
    """Verify transient and persistent prompts coexist correctly."""

    def setup_method(self):
        clear_extra_prompt()

    def teardown_method(self):
        clear_extra_prompt()

    def test_both_included(self):
        set_extra_prompt("persistent", persistent=True)
        set_extra_prompt("transient")
        result = get_extra_prompt()
        assert "persistent" in result
        assert "transient" in result

    def test_persistent_first(self):
        """Persistent prompt appears before transient in combined output."""
        set_extra_prompt("PERSIST", persistent=True)
        set_extra_prompt("TRANS")
        result = get_extra_prompt()
        persist_pos = result.index("PERSIST")
        trans_pos = result.index("TRANS")
        assert persist_pos < trans_pos


# ── find_next_task removed tests ──


class TestFindNextTaskRemoved:
    """Verify find_next_task function no longer exists."""

    def test_find_next_task_not_importable(self):
        with pytest.raises(ImportError):
            from se3.engine.loop_controller import find_next_task  # noqa: F401


# ── LoopController tests ──


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
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


class TestLoopControllerLifecycle:
    def test_start_creates_branch_and_worktree(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path)
        assert controller.start(task="test task")
        assert controller.has_worktree
        assert controller.loop_branch is not None
        # New naming: loop/{slug}-{iteration}
        assert controller.loop_branch.startswith("loop/")
        assert controller.effective_root != tmp_path
        # Cleanup
        controller.finish()

    def test_start_without_task_uses_legacy_format(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path)
        assert controller.start()
        assert controller.has_worktree
        assert controller.loop_branch is not None
        # Without task, falls back to legacy format
        assert controller.loop_branch.startswith("se3-loop/")
        # Cleanup
        controller.finish()

    def test_start_no_worktree_mode(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path, no_worktree=True)
        assert controller.start()
        assert not controller.has_worktree
        assert controller.effective_root == tmp_path

    def test_run_iteration_with_task(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path, no_worktree=True)
        controller.start()

        mock_run_flow = MagicMock(return_value=0)
        result = controller.run_iteration(
            run_flow_fn=mock_run_flow,
            task="Test task",
            task_type="feature",
        )

        assert result.success
        assert result.task == "Test task"
        mock_run_flow.assert_called_once()

    def test_run_iteration_requires_task(self):
        """task is a required parameter — calling without it raises TypeError."""
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        mock_run_flow = MagicMock(return_value=0)
        with pytest.raises(TypeError):
            controller.run_iteration(run_flow_fn=mock_run_flow)

    def test_failed_task_tracked(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path, no_worktree=True)
        controller.start()

        mock_run_flow = MagicMock(return_value=1)
        result = controller.run_iteration(
            run_flow_fn=mock_run_flow,
            task="Failing task",
        )

        assert not result.success
        assert result.exit_code == 1

    def test_iteration_count_increments(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path, no_worktree=True)
        controller.start()

        mock_run_flow = MagicMock(return_value=0)
        controller.run_iteration(run_flow_fn=mock_run_flow, task="Task 1")
        controller.run_iteration(run_flow_fn=mock_run_flow, task="Task 2")
        assert controller.iteration_count == 2

    def test_finish_with_worktree(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path)
        controller.start()

        finish_state = controller.finish()
        assert finish_state["worktree_cleaned"]
        assert finish_state["loop_branch"] is not None
        assert not controller.has_worktree

    def test_finish_interrupted(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path)
        controller.start()

        finish_state = controller.finish(interrupted=True)
        assert finish_state["interrupted"]
        assert finish_state["worktree_cleaned"]

    def test_discard(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path)
        controller.start()
        branch = controller.loop_branch
        controller.finish()
        controller.discard()

        # Branch should be deleted
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch],
            capture_output=True, text=True,
        )
        assert branch not in result.stdout


class TestPreviousSummaryInjection:
    """Tests for cross-iteration summary injection."""

    def test_add_summary(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        controller.add_summary("iteration 1 summary")
        assert controller.accumulated_summaries == ["iteration 1 summary"]

    def test_accumulated_summaries_multiple(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        controller.add_summary("summary 1")
        controller.add_summary("summary 2")
        controller.add_summary("summary 3")
        assert len(controller.accumulated_summaries) == 3
        assert controller.accumulated_summaries[0] == "summary 1"
        assert controller.accumulated_summaries[2] == "summary 3"

    def test_summary_truncation(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        # Add long summaries to exceed 2000 char limit
        for i in range(30):
            controller.add_summary(f"Long summary {i}: " + "x" * 100)
        total = sum(len(s) for s in controller.accumulated_summaries)
        assert total <= 2100  # within limits (2000 + one placeholder)
        assert controller.accumulated_summaries[0] == "[...earlier iterations omitted...]"

    def test_build_loop_context_includes_summaries(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        controller.iteration_count = 2
        controller.add_summary("Did X and Y in iteration 1")
        context = controller._build_loop_context("my task")
        assert "Previous Iteration Summaries" in context
        assert "Did X and Y in iteration 1" in context
        assert "my task" in context

    def test_build_loop_context_no_summary(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        controller.iteration_count = 1
        context = controller._build_loop_context("my task")
        assert "Previous Iteration Summaries" not in context
        assert "my task" in context

    def test_build_loop_context_no_completed_tasks_list(self):
        """_build_loop_context should not contain completed tasks list."""
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        controller.iteration_count = 3
        context = controller._build_loop_context("my task")
        assert "Previously completed" not in context

    def test_iteration_summary_attribute(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        assert controller.iteration_summary is None
        controller.iteration_summary = "test summary"
        assert controller.iteration_summary == "test summary"

    def test_build_loop_context_with_max_iterations(self):
        controller = LoopController(
            project_root=Path("/tmp"), no_worktree=True, max_iterations=5
        )
        controller.iteration_count = 3
        context = controller._build_loop_context("my task")
        assert "3 of 5" in context

    def test_iteration_start_commit_initial(self):
        controller = LoopController(project_root=Path("/tmp"), no_worktree=True)
        assert controller.iteration_start_commit == ""
