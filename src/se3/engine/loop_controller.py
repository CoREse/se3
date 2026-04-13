"""Loop mode controller for SE3 flow engine.

Orchestrates the loop mode lifecycle as an external wrapper around
the standard run_flow() pipeline. The internal flow is mechanically
unaware of loop context; awareness is injected only via
set_extra_prompt(persistent=True).

In Ralph Loop mode, the controller accepts a user prompt as a required
parameter and repeats it across iterations, injecting cross-iteration
summaries via persistent extra prompt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .llm_caller import clear_persistent_extra_prompt, set_extra_prompt
from .worktree import (
    WorktreeContext,
    _slugify_task_id,
    create_loop_branch,
    delete_branch,
    get_current_branch,
    has_new_commits,
    merge_loop_branch,
)

logger = logging.getLogger(__name__)


class IterationResult:
    """Result of a single loop iteration."""

    def __init__(self, success: bool, task: Optional[str] = None, exit_code: int = 0):
        self.success = success
        self.task = task
        self.exit_code = exit_code


class LoopController:
    """Orchestrates the full loop mode lifecycle (Ralph Loop).

    Creates branch/worktree for isolation, accepts a user prompt as the
    required task, invokes run_flow() per iteration, and supports
    cross-iteration summary injection. Task discovery is not performed —
    the user prompt is repeated each iteration.

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
        self.use_worktree: bool = not no_worktree

        # Cross-iteration summary (accumulated across iterations)
        self.accumulated_summaries: list[str] = []
        self.iteration_summary: Optional[str] = None
        self.iteration_start_commit: str = ""

    @property
    def effective_root(self) -> Path:
        """The root directory where flows should execute."""
        return self._effective_root

    @property
    def has_worktree(self) -> bool:
        """Whether a worktree is currently active."""
        return self.worktree_path is not None

    def start(self, task: str = "") -> bool:
        """Create branch and worktree for isolation.

        Args:
            task: Task description, used to derive the branch name slug.

        Returns:
            True if setup succeeded (or no worktree needed), False if setup
            failed and fell back to non-isolated mode.
        """
        if not self.use_worktree:
            return True

        try:
            self.original_branch = get_current_branch(self.project_root)
            task_id = _slugify_task_id(task) if task else None
            iteration = self.iteration_count + 1
            self.loop_branch, self.original_branch = create_loop_branch(
                self.project_root,
                task_id=task_id,
                iteration=iteration if task_id else None,
            )
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

    def add_summary(self, summary: str) -> None:
        """Append an iteration summary to the accumulated list.

        Enforces a total length cap of 8000 characters.  When exceeded,
        early entries are replaced with a placeholder.
        """
        self.accumulated_summaries.append(summary)
        self._truncate_summaries()

    def _truncate_summaries(self) -> None:
        """Ensure total accumulated summary length stays under 8000 chars."""
        max_len = 8000
        placeholder = "[...earlier iterations omitted...]"
        total = sum(len(s) for s in self.accumulated_summaries)
        if total <= max_len:
            return
        # Remove oldest (non-placeholder) entries until under limit
        while total > max_len and len(self.accumulated_summaries) > 1:
            first = self.accumulated_summaries[0]
            if first == placeholder:
                # Skip the placeholder, remove the next real entry
                if len(self.accumulated_summaries) > 2:
                    removed = self.accumulated_summaries.pop(1)
                    total -= len(removed)
                else:
                    break
            else:
                removed = self.accumulated_summaries.pop(0)
                total -= len(removed)
        # Ensure placeholder is at the front
        if self.accumulated_summaries and self.accumulated_summaries[0] != placeholder:
            self.accumulated_summaries.insert(0, placeholder)
            total += len(placeholder)

    def run_iteration(
        self,
        run_flow_fn,
        task: str,
        task_type: str = "pending",
    ) -> IterationResult:
        """Run a single loop iteration.

        Args:
            run_flow_fn: The run_flow() function to call.
            task: Task description (required).
            task_type: Type of task.

        Returns:
            IterationResult with success status and task info.
        """
        self.iteration_count += 1
        self.iteration_summary = None

        # Record HEAD commit at start of iteration for diff calculation
        try:
            import subprocess
            head_result = subprocess.run(
                ["git", "-C", str(self._effective_root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=False,
            )
            self.iteration_start_commit = head_result.stdout.strip() if head_result.returncode == 0 else ""
        except Exception:
            self.iteration_start_commit = ""

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
        if self.accumulated_summaries:
            summary_lines = []
            for idx, s in enumerate(self.accumulated_summaries, 1):
                if s == "[...earlier iterations omitted...]":
                    summary_lines.append(s)
                else:
                    summary_lines.append(f"Iteration {idx}: {s}")
            parts.append(
                "\n[Previous Iteration Summaries]\n" + "\n".join(summary_lines)
            )
        return "\n".join(parts)
