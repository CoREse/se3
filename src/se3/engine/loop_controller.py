"""Loop mode controller for SE3 flow engine.

Orchestrates the loop mode lifecycle as an external wrapper around
the standard run_flow() pipeline. The internal flow is mechanically
unaware of loop context; awareness is injected only via
set_extra_prompt(persistent=True).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from .llm_caller import clear_persistent_extra_prompt, set_extra_prompt
from .worktree import (
    WorktreeContext,
    cleanup_loop,
    create_loop_branch,
    delete_branch,
    get_current_branch,
    get_diff_stat,
    has_new_commits,
    list_loop_branches,
    merge_loop_branch,
    remove_worktree,
)

logger = logging.getLogger(__name__)


class IterationResult:
    """Result of a single loop iteration."""

    def __init__(self, success: bool, task: Optional[str] = None, exit_code: int = 0):
        self.success = success
        self.task = task
        self.exit_code = exit_code


class LoopController:
    """Orchestrates the full loop mode lifecycle.

    Creates branch/worktree for isolation, discovers tasks, invokes run_flow()
    per iteration, tracks completed/failed tasks, and handles post-loop
    merge/discard/defer decisions.

    The internal run_flow() is treated as a black box — the controller only
    knows its return code. Loop awareness is injected into the flow only via
    persistent extra prompt.
    """

    def __init__(
        self,
        project_root: Path,
        max_iterations: Optional[int] = None,
        no_worktree: bool = False,
        prompt_history: Any = None,
    ) -> None:
        self.project_root = project_root
        self.max_iterations = max_iterations
        self.no_worktree = no_worktree
        self.prompt_history = prompt_history

        # State
        self.loop_branch: Optional[str] = None
        self.original_branch: Optional[str] = None
        self.worktree_path: Optional[Path] = None
        self._worktree_ctx: Optional[WorktreeContext] = None
        self._effective_root: Path = project_root
        self.iteration_count: int = 0
        self.completed_tasks: set[str] = set()
        self.failed_tasks: set[str] = set()
        self.use_worktree: bool = not no_worktree

    @property
    def effective_root(self) -> Path:
        """The root directory where flows should execute."""
        return self._effective_root

    @property
    def has_worktree(self) -> bool:
        """Whether a worktree is currently active."""
        return self.worktree_path is not None

    def start(self) -> bool:
        """Create branch and worktree for isolation.

        Returns:
            True if setup succeeded (or no worktree needed), False if setup
            failed and fell back to non-isolated mode.
        """
        if not self.use_worktree:
            return True

        try:
            self.original_branch = get_current_branch(self.project_root)
            self.loop_branch, self.original_branch = create_loop_branch(self.project_root)
            self._worktree_ctx = WorktreeContext(self.project_root, self.loop_branch)
            self.worktree_path = self._worktree_ctx.__enter__()
            self._effective_root = self.worktree_path
            return True
        except Exception as e:
            logger.error("Failed to set up worktree isolation: %s", e)
            self.use_worktree = False
            self.loop_branch = None
            self.worktree_path = None
            self._worktree_ctx = None
            self._effective_root = self.project_root
            return False

    def run_iteration(
        self,
        run_flow_fn,
        task: Optional[str] = None,
        task_type: str = "pending",
    ) -> IterationResult:
        """Run a single loop iteration.

        Args:
            run_flow_fn: The run_flow() function to call.
            task: Task description (if None, discovers next task).
            task_type: Type of task.

        Returns:
            IterationResult with success status and task info.
        """
        self.iteration_count += 1

        if not task:
            task = find_next_task(
                self.project_root,
                skip_tasks=self.completed_tasks | self.failed_tasks,
            )
            if not task:
                return IterationResult(success=True, task=None)

        # Inject loop context via persistent extra prompt
        loop_context = self._build_loop_context(task)
        set_extra_prompt(loop_context, persistent=True)

        try:
            exit_code = run_flow_fn(
                project_root=self._effective_root,
                task_description=task,
                task_type=task_type,
                is_loop_mode=True,
                prompt_history=self.prompt_history,
            )
        finally:
            # Clean up persistent prompt between iterations
            clear_persistent_extra_prompt()

        if exit_code == 0:
            self.completed_tasks.add(task)
        else:
            self.failed_tasks.add(task)

        return IterationResult(
            success=(exit_code == 0),
            task=task,
            exit_code=exit_code,
        )

    def finish(self, interrupted: bool = False) -> dict:
        """Handle post-loop cleanup and return state for merge decision.

        Args:
            interrupted: Whether the loop was interrupted by Ctrl+C.

        Returns:
            Dict with keys: 'has_commits', 'loop_branch', 'original_branch',
            'worktree_cleaned'. Caller handles user interaction.
        """
        result = {
            "has_commits": False,
            "loop_branch": self.loop_branch,
            "original_branch": self.original_branch,
            "worktree_cleaned": False,
            "interrupted": interrupted,
        }

        if not self.use_worktree or not self.loop_branch or not self.original_branch:
            return result

        # Remove worktree (but preserve branch)
        if self._worktree_ctx is not None:
            self._worktree_ctx.__exit__(None, None, None)
            self._worktree_ctx = None
            self.worktree_path = None
            result["worktree_cleaned"] = True

        # Check if there are new commits
        result["has_commits"] = has_new_commits(
            self.project_root, self.loop_branch, self.original_branch
        )

        return result

    def merge(self) -> bool:
        """Merge the loop branch into the original branch.

        Returns:
            True if merge succeeded.
        """
        if not self.loop_branch or not self.original_branch:
            return False
        success = merge_loop_branch(
            self.project_root, self.loop_branch, self.original_branch
        )
        if success:
            delete_branch(self.project_root, self.loop_branch)
        return success

    def discard(self) -> None:
        """Discard the loop branch."""
        if self.loop_branch:
            delete_branch(self.project_root, self.loop_branch)

    def merge_existing(self, branch: str) -> bool:
        """Merge an existing loop branch.

        Args:
            branch: Branch name to merge.

        Returns:
            True if merge succeeded.
        """
        target = get_current_branch(self.project_root)
        return merge_loop_branch(self.project_root, branch, target)

    def _build_loop_context(self, task: str) -> str:
        """Build loop context string for prompt injection."""
        parts = [
            f"[Loop Mode Context] You are running in loop mode, iteration "
            f"{self.iteration_count}",
        ]
        if self.max_iterations:
            parts[0] += f" of {self.max_iterations}"
        parts[0] += "."
        parts.append(f"Current task: {task}")
        if self.completed_tasks:
            parts.append(
                f"Previously completed tasks ({len(self.completed_tasks)}): "
                + ", ".join(sorted(self.completed_tasks)[:5])
            )
        return "\n".join(parts)


def find_next_task(
    project_root: Path,
    skip_tasks: Optional[set[str]] = None,
) -> Optional[str]:
    """Find the next task from backlog, roadmap, or code TODOs.

    Searches the original project_root (not worktree) for task discovery.
    Supports filtering already-attempted tasks and priority ordering.

    Args:
        project_root: Project root directory (original, not worktree).
        skip_tasks: Set of task description strings to skip.

    Returns:
        Task description or None if no tasks found.
    """
    if skip_tasks is None:
        skip_tasks = set()

    # Load custom backlog paths from se3.yaml if available
    backlog_paths = _get_backlog_paths(project_root)

    # Collect all candidate tasks with priority
    candidates: list[tuple[int, str]] = []

    # Search backlog directories
    for backlog_dir in backlog_paths:
        if not backlog_dir.exists():
            continue
        for backlog_file in sorted(backlog_dir.glob("*.md")):
            try:
                content = backlog_file.read_text()
                phase_priority = _extract_phase_priority(content)
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("- [ ]") or line.startswith("* [ ]"):
                        task_text = line[5:].strip()
                        if task_text and len(task_text) > 5:
                            task_desc = f"[{backlog_file.stem}] {task_text}"
                            if task_desc not in skip_tasks:
                                candidates.append((phase_priority, task_desc))
            except IOError:
                continue

    # Check roadmap.md
    roadmap_file = project_root / "roadmap.md"
    if roadmap_file.exists():
        try:
            content = roadmap_file.read_text()
            in_current_phase = False
            for line in content.split("\n"):
                if "Phase 1" in line or "Current" in line.lower():
                    in_current_phase = True
                elif line.startswith("## Phase") and in_current_phase:
                    in_current_phase = False

                if in_current_phase and (
                    line.strip().startswith("- [ ]")
                    or line.strip().startswith("* [ ]")
                ):
                    task_text = line.strip()[5:].strip()
                    if task_text and len(task_text) > 5:
                        task_desc = f"[roadmap] {task_text}"
                        if task_desc not in skip_tasks:
                            candidates.append((100, task_desc))
        except IOError:
            pass

    # Sort by priority (lower = higher priority) and return first
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    # Fallback: check for TODO comments in code
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "TODO", "--", "*.py", "*.md"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            for todo_line in result.stdout.strip().split("\n"):
                parts = todo_line.split(":", 2)
                if len(parts) >= 3:
                    task_desc = f"[TODO] {parts[2].strip()}"
                    if task_desc not in skip_tasks:
                        return task_desc
    except Exception:
        pass

    return None


def _get_backlog_paths(project_root: Path) -> list[Path]:
    """Get backlog directory paths, checking se3.yaml for custom paths."""
    paths = []

    # Check se3.yaml for custom backlog paths
    config_file = project_root / "se3.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            custom_paths = config.get("loop", {}).get("backlog_paths", [])
            for p in custom_paths:
                paths.append(project_root / p)
        except Exception:
            pass

    # Default backlog paths
    if not paths:
        specs_backlog = project_root / "specs" / "_backlog"
        openspec_backlog = project_root / "openspec" / "backlog"
        if specs_backlog.exists():
            paths.append(specs_backlog)
        elif openspec_backlog.exists():
            paths.append(openspec_backlog)

    return paths


def _extract_phase_priority(content: str) -> int:
    """Extract phase number from file content for priority sorting.

    Files with lower phase numbers are higher priority.
    Files without phase info get priority 50 (medium).
    """
    match = re.search(r"[Pp]hase\s+(\d+)", content)
    if match:
        return int(match.group(1))
    return 50
