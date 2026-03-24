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
from se3.engine.loop_controller import LoopController, find_next_task


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


# ── find_next_task tests ──


class TestFindNextTask:
    def test_finds_backlog_task(self, tmp_path: Path):
        backlog = tmp_path / "specs" / "_backlog"
        backlog.mkdir(parents=True)
        (backlog / "tasks.md").write_text(
            "# Tasks\n\n- [x] Done task\n- [ ] Implement feature X\n- [ ] Another task\n"
        )
        result = find_next_task(tmp_path)
        assert result == "[tasks] Implement feature X"

    def test_skips_tasks_in_skip_set(self, tmp_path: Path):
        backlog = tmp_path / "specs" / "_backlog"
        backlog.mkdir(parents=True)
        (backlog / "tasks.md").write_text(
            "# Tasks\n\n- [ ] Implement feature X\n- [ ] Another task here\n"
        )
        result = find_next_task(tmp_path, skip_tasks={"[tasks] Implement feature X"})
        assert result == "[tasks] Another task here"

    def test_returns_none_when_all_skipped(self, tmp_path: Path):
        backlog = tmp_path / "specs" / "_backlog"
        backlog.mkdir(parents=True)
        (backlog / "tasks.md").write_text("# Tasks\n\n- [ ] Implement feature X\n")
        result = find_next_task(
            tmp_path,
            skip_tasks={"[tasks] Implement feature X"},
        )
        assert result is None

    def test_returns_none_when_no_tasks(self, tmp_path: Path):
        result = find_next_task(tmp_path)
        assert result is None

    def test_skips_short_tasks(self, tmp_path: Path):
        backlog = tmp_path / "specs" / "_backlog"
        backlog.mkdir(parents=True)
        (backlog / "tasks.md").write_text("# Tasks\n\n- [ ] Hi\n- [ ] Do something useful\n")
        result = find_next_task(tmp_path)
        assert result == "[tasks] Do something useful"

    def test_phase_priority_ordering(self, tmp_path: Path):
        backlog = tmp_path / "specs" / "_backlog"
        backlog.mkdir(parents=True)
        # File with phase 2
        (backlog / "a_phase2.md").write_text(
            "# Phase 2 tasks\n\n- [ ] Second priority task\n"
        )
        # File with phase 1 (should come first despite filename)
        (backlog / "b_phase1.md").write_text(
            "# Phase 1 tasks\n\n- [ ] First priority task\n"
        )
        result = find_next_task(tmp_path)
        assert result == "[b_phase1] First priority task"

    def test_roadmap_task(self, tmp_path: Path):
        (tmp_path / "roadmap.md").write_text(
            "## Phase 1 - Current\n\n- [ ] Roadmap task here\n\n## Phase 2\n\n- [ ] Future task\n"
        )
        result = find_next_task(tmp_path)
        assert result == "[roadmap] Roadmap task here"


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
        assert controller.start()
        assert controller.has_worktree
        assert controller.loop_branch is not None
        assert controller.loop_branch.startswith("se3-loop/")
        assert controller.effective_root != tmp_path
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
        assert "Test task" in controller.completed_tasks
        mock_run_flow.assert_called_once()

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
        assert "Failing task" in controller.failed_tasks
        assert "Failing task" not in controller.completed_tasks

    def test_iteration_count_increments(self, tmp_path: Path):
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path, no_worktree=True)
        controller.start()

        mock_run_flow = MagicMock(return_value=0)
        controller.run_iteration(run_flow_fn=mock_run_flow, task="Task 1")
        controller.run_iteration(run_flow_fn=mock_run_flow, task="Task 2")
        assert controller.iteration_count == 2

    def test_no_task_returns_none(self, tmp_path: Path):
        """When no task provided and none found, result.task is None."""
        _init_repo(tmp_path)
        controller = LoopController(project_root=tmp_path, no_worktree=True)
        controller.start()

        mock_run_flow = MagicMock(return_value=0)
        result = controller.run_iteration(run_flow_fn=mock_run_flow)
        assert result.task is None
        mock_run_flow.assert_not_called()

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
