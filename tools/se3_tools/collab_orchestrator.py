"""Foreground Orchestrator for SE3 Collab - asyncio-based implementation.

Provides real-time collaboration with Manager planning and concurrent Worker execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .claude_runner import ClaudeRunner
from .collab_render import CollabRenderer
from .collab_human_handler import InteractiveHumanHandler


@dataclass
class Task:
    """Represents a single task in the collaboration."""
    id: str
    title: str
    prompt: str
    branch: str
    worktree: Path
    status: str = "pending"  # pending, running, done, failed, blocked
    progress: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    output_log: list[str] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 2


@dataclass
class WorkerSession:
    """Represents an active worker session."""
    task: Task
    process: asyncio.subprocess.Process
    output_buffer: list[str] = field(default_factory=list)
    task_file: Path | None = None


@dataclass
class ManagerDecision:
    """Represents a decision from the Manager agent."""
    action: str  # plan, merge, reject, retry, split, escalate, complete
    tasks: list[dict[str, Any]] = field(default_factory=list)
    target_task: str = ""
    merge_branch: str = ""
    retry_prompt: str = ""
    reason: str = ""
    summary: str = ""


class ForegroundOrchestrator:
    """Asyncio-based foreground orchestrator for SE3 Collab.

    Runs Manager and Workers as subprocesses with real-time output capture
    and rich terminal UI.
    """

    def __init__(
        self,
        project_root: Path,
        renderer: CollabRenderer,
        max_parallel: int = 3,
        mock: bool = False,
    ):
        self.project_root = project_root
        self.renderer = renderer
        self.worktrees_dir = project_root / ".worktrees"
        self.collab_dir = project_root / ".collab"
        self.tasks: dict[str, Task] = {}
        self.active_workers: dict[str, WorkerSession] = {}
        self.max_parallel = max_parallel
        self.mock = mock
        self.session_id = f"collab-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.base_branch = "master"
        self.human_handler = InteractiveHumanHandler(project_root, renderer)

    async def run(self, objective: str) -> bool:
        """Run the complete collaboration session.

        Args:
            objective: The high-level objective for the collaboration

        Returns:
            True if successful, False otherwise
        """
        # Setup
        self._ensure_directories()
        self._load_base_branch()
        self._save_session_config(objective)

        try:
            # Phase 1: Manager planning
            self.renderer.update_manager("Initializing Manager for task planning...")
            decision = await self._run_manager_plan(objective)

            if decision.action == "escalate":
                self.renderer.print_message(
                    f"Manager escalated: {decision.reason}", "yellow"
                )
                return False

            if decision.action != "plan":
                self.renderer.print_message(
                    f"Unexpected manager action: {decision.action}", "red"
                )
                return False

            # Create tasks from manager decision
            self.renderer.update_manager(f"Creating {len(decision.tasks)} tasks...")
            for task_data in decision.tasks:
                task = self._create_task(task_data)
                self.tasks[task.id] = task
                self.renderer.add_worker(task.id, task.title)

            # Check if any tasks were created
            if not self.tasks:
                self.renderer.print_message(
                    "Manager returned no tasks. Nothing to do.", "yellow"
                )
                return False

            # Phase 2: Execute all workers concurrently
            self.renderer.update_manager(f"Launching {len(self.tasks)} workers...")
            await self._run_all_workers()

            # Phase 2.5: Check for human calls if all tasks are blocked
            can_continue = await self.human_handler.check_and_handle(list(self.tasks.values()))
            if not can_continue:
                self.renderer.print_message(
                    "Collaboration paused - waiting for human response", "yellow"
                )
                return False

            # Phase 3: Manager review
            self.renderer.update_manager("Running final review...")
            review = await self._run_manager_review()

            # Summary
            completed = sum(1 for t in self.tasks.values() if t.status == "done")
            failed = sum(1 for t in self.tasks.values() if t.status == "failed")

            self.renderer.print_final_summary(
                success=review.action == "complete",
                completed=completed,
                failed=failed,
            )

            return review.action == "complete"

        except asyncio.CancelledError:
            # Handle graceful shutdown on cancellation
            self.renderer.print_message("\nShutting down collaboration...", "yellow")
            await self.cleanup()
            raise
        except Exception as e:
            self.renderer.print_message(f"\nError during collaboration: {e}", "red")
            await self.cleanup()
            raise

    async def cleanup(self):
        """Clean up all active resources.

        Terminates any running worker processes and saves task states.
        Called on shutdown or error.
        """
        if not self.active_workers:
            return

        self.renderer.print_message(
            f"Cleaning up {len(self.active_workers)} active worker(s)...", "yellow"
        )

        # Create cleanup tasks for all active workers
        cleanup_tasks = []
        for session in list(self.active_workers.values()):
            cleanup_tasks.append(self._cleanup_worker(session))

        # Wait for all cleanups with a timeout
        if cleanup_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*cleanup_tasks, return_exceptions=True),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                self.renderer.print_message(
                    "Warning: Some workers did not terminate in time", "red"
                )

        self.active_workers.clear()

    async def _cleanup_worker(self, session: WorkerSession):
        """Clean up a single worker session.

        Terminates the process and updates task status.
        """
        task = session.task
        proc = session.process

        # Update task status
        if task.status == "running":
            task.status = "failed"
            task.exit_code = -1
            task.completed_at = datetime.now()

        # Terminate process if still running
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass  # Ignore errors during force kill
            except Exception:
                pass  # Ignore other termination errors

        # Save final task state
        try:
            await self._save_task_file(task)
        except Exception:
            pass  # Ignore save errors during cleanup

    def _ensure_directories(self):
        """Ensure required directories exist."""
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.collab_dir.mkdir(parents=True, exist_ok=True)
        (self.collab_dir / "tasks").mkdir(exist_ok=True)
        (self.collab_dir / "logs").mkdir(exist_ok=True)

    def _load_base_branch(self):
        """Load the current git branch as base."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )
        self.base_branch = result.stdout.strip() or "master"

    def _save_session_config(self, objective: str):
        """Save session configuration."""
        config = {
            "session_id": self.session_id,
            "objective": objective,
            "base_branch": self.base_branch,
            "created_at": datetime.now().isoformat(),
            "max_parallel_workers": self.max_parallel,
            "status": "active",
            "mode": "foreground",
        }
        config_file = self.collab_dir / "config.json"
        config_file.write_text(json.dumps(config, indent=2))

    def _create_task(self, task_data: dict[str, Any]) -> Task:
        """Create a Task from manager decision data.

        Validates required fields and provides sensible defaults.
        """
        # Validate task_data is a dict
        if not isinstance(task_data, dict):
            raise ValueError(f"Task data must be a dictionary, got {type(task_data).__name__}")

        # Get or generate task ID
        task_id = task_data.get("id", f"task-{len(self.tasks)+1:03d}")

        # Validate ID format (basic check)
        if not task_id or not isinstance(task_id, str):
            task_id = f"task-{len(self.tasks)+1:03d}"

        # Sanitize ID for filesystem safety
        task_id = re.sub(r'[^a-zA-Z0-9_-]', '-', task_id)

        # Get title with fallback
        title = task_data.get("title", "")
        if not title or not isinstance(title, str):
            title = f"Task {task_id}"

        # Get prompt with fallback
        prompt = task_data.get("prompt", "")
        if not isinstance(prompt, str):
            prompt = str(prompt) if prompt else ""

        return Task(
            id=task_id,
            title=title,
            prompt=prompt,
            branch=f"collab/{task_id}",
            worktree=self.worktrees_dir / task_id,
        )

    async def _run_manager_plan(self, objective: str, timeout: int = 300) -> ManagerDecision:
        """Run Manager to create initial plan.

        Args:
            objective: The objective to plan for
            timeout: Maximum time to wait for planning (seconds)
        """
        if self.mock:
            # Return a mock plan for testing
            return ManagerDecision(
                action="plan",
                tasks=[
                    {
                        "id": "task-001",
                        "title": f"Mock task for: {objective[:50]}",
                        "prompt": f"This is a mock task. Objective: {objective}",
                    }
                ],
                reason="Mock mode - returning test plan",
                summary="Mock plan generated for testing",
            )

        prompt = self._build_manager_prompt(objective)

        # Use ClaudeRunner for command fallback support
        runner = ClaudeRunner(self.project_root)

        # Get the first available command
        if not runner.commands:
            return ManagerDecision(
                action="escalate",
                reason="No Claude commands configured",
            )

        cmd_entry = runner.commands[0]
        cmd_name = cmd_entry["cmd"]

        # Check if command exists
        if not shutil.which(cmd_name):
            return ManagerDecision(
                action="escalate",
                reason=f"Claude command '{cmd_name}' not found in PATH",
            )

        proc = await asyncio.create_subprocess_exec(
            cmd_name,
            "--dangerously-skip-permissions",
            "--print",
            "--output-format", "stream-json",
            "--max-turns", "30",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._get_clean_env(),
        )

        # Send prompt via stdin
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            else:
                # stdin is None - process may have failed to start
                self.renderer.update_manager("Warning: Manager process stdin is None, process may have failed to start")
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # Handle case where process exits before we finish writing
            self.renderer.update_manager(f"Warning: Manager process closed stdin early: {e}")

        # Read output with timeout
        output_lines = []
        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                # Check timeout
                if asyncio.get_event_loop().time() - start_time > timeout:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                    return ManagerDecision(
                        action="escalate",
                        reason=f"Manager planning timed out after {timeout}s",
                    )

                # Use wait_for to implement read timeout
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # Check if process is still running
                    if proc.returncode is not None:
                        break
                    continue

                if not line:
                    break

                line_str = line.decode()
                output_lines.append(line_str)

                # Update manager panel with recent output
                self.renderer.update_manager("".join(output_lines[-20:]))

                # Also render stream-json output for tool calls
                self.renderer.render_stream_json(line_str)

        except asyncio.CancelledError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            raise

        await proc.wait()

        # Parse decision
        full_output = "".join(output_lines)
        return self._parse_manager_decision(full_output)

    async def _run_all_workers(self):
        """Run all workers with concurrency limit and retry support."""
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def run_with_limit(task: Task):
            async with semaphore:
                await self._run_worker_with_retry(task)

        # Create tasks for all pending workers
        pending = [t for t in self.tasks.values() if t.status == "pending"]
        if not pending:
            return

        # Start all workers
        coroutines = [run_with_limit(task) for task in pending]
        await asyncio.gather(*coroutines, return_exceptions=True)

    async def _run_worker_with_retry(self, task: Task):
        """Run a worker with automatic retry on failure."""
        while task.retry_count <= task.max_retries:
            await self._run_worker(task)

            if task.status == "done":
                return  # Success, no retry needed

            # Task failed - check if we should retry
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = "pending"  # Reset to pending for retry
                task.progress = 0
                # Update renderer to show retry status
                self.renderer.update_worker_status(
                    task.id,
                    status="pending",
                    eta=f"retry {task.retry_count}/{task.max_retries}"
                )
                self.renderer.append_worker_output(
                    task.id,
                    f"\n[Retry {task.retry_count}/{task.max_retries}] Retrying task...\n"
                )
                # Small delay before retry
                await asyncio.sleep(1)
            else:
                # Max retries reached
                self.renderer.append_worker_output(
                    task.id,
                    f"\n[Failed] Max retries ({task.max_retries}) reached.\n"
                )
                break

    async def _run_worker(self, task: Task):
        """Run a single worker task."""
        # Create worktree
        try:
            await self._ensure_worktree(task)
        except Exception as e:
            task.status = "failed"
            task.exit_code = -1
            self.renderer.append_worker_output(task.id, f"[Error] Failed to create worktree: {e}")
            await self._save_task_file(task)
            return

        # Create task file
        task_file = await self._save_task_file(task)

        if self.mock:
            # Mock mode: simulate worker execution
            await self._run_mock_worker(task)
            return

        # Create worker prompt file
        prompt_file = self._create_worker_prompt_file(task)

        # Use ClaudeRunner for command fallback support
        runner = ClaudeRunner(self.project_root)

        # Get the first available command
        if not runner.commands:
            task.status = "failed"
            task.exit_code = -1
            self.renderer.append_worker_output(task.id, "[Error] No Claude commands configured")
            await self._save_task_file(task)
            return

        cmd_entry = runner.commands[0]
        cmd_name = cmd_entry["cmd"]

        # Check if command exists
        if not shutil.which(cmd_name):
            task.status = "failed"
            task.exit_code = -1
            self.renderer.append_worker_output(task.id, f"[Error] Claude command '{cmd_name}' not found")
            await self._save_task_file(task)
            return

        # Launch worker
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd_name,
                "--dangerously-skip-permissions",
                "--print",
                "--output-format", "stream-json",
                f"@{prompt_file}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=task.worktree,
                env=self._get_clean_env(),
            )
        except Exception as e:
            task.status = "failed"
            task.exit_code = -1
            self.renderer.append_worker_output(task.id, f"[Error] Failed to launch worker process: {e}")
            await self._save_task_file(task)
            return

        # Update status
        task.status = "running"
        task.started_at = datetime.now()
        self.renderer.update_worker_status(task.id, status="running")

        session = WorkerSession(
            task=task,
            process=proc,
            task_file=task_file,
        )
        self.active_workers[task.id] = session

        # Read output in real-time
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break

                line_str = line.decode()
                session.output_buffer.append(line_str)
                task.output_log.append(line_str)

                # Update UI
                self.renderer.append_worker_output(task.id, line_str)
                self.renderer.render_stream_json(line_str)

                # Update progress based on output patterns
                self._update_progress_from_output(task, line_str)

        except asyncio.CancelledError:
            self.renderer.append_worker_output(task.id, "[Cancelled by user]")
            await self._cleanup_worker_process(proc, task, timeout=5.0)
            # Update task status before re-raising
            task.status = "failed"
            task.exit_code = -1
            task.completed_at = datetime.now()
            del self.active_workers[task.id]
            # Save final state
            await self._save_task_file(task)
            raise
        except Exception as e:
            # Handle unexpected errors during output reading
            self.renderer.append_worker_output(task.id, f"[Error] Failed to read worker output: {e}")
            await self._cleanup_worker_process(proc, task, timeout=5.0)
            task.status = "failed"
            task.exit_code = -1
            task.completed_at = datetime.now()
            del self.active_workers[task.id]
            await self._save_task_file(task)
            return

        # Wait for completion with timeout to prevent indefinite hanging
        try:
            await asyncio.wait_for(proc.wait(), timeout=3600)  # 1 hour timeout
            task.exit_code = proc.returncode
            task.status = "done" if proc.returncode == 0 else "failed"
        except asyncio.TimeoutError:
            await self._cleanup_worker_process(proc, task, timeout=5.0)
            task.exit_code = -1
            task.status = "failed"
            self.renderer.append_worker_output(task.id, "[Error] Worker timed out after 1 hour")
        except Exception as e:
            task.exit_code = -1
            task.status = "failed"
            self.renderer.append_worker_output(task.id, f"Error: {e}")

        task.completed_at = datetime.now()

        # Update UI
        self.renderer.update_worker_status(
            task.id,
            status=task.status,
            progress=100 if task.status == "done" else task.progress,
        )

        del self.active_workers[task.id]

        # Update task file with final status
        await self._save_task_file(task)

        # Clean up prompt file
        if prompt_file.exists():
            try:
                prompt_file.unlink()
            except Exception:
                pass  # Ignore cleanup errors

    async def _cleanup_worker_process(self, proc: asyncio.subprocess.Process, task: Task, timeout: float = 5.0):
        """Clean up a worker process gracefully, then forcefully if needed.

        Args:
            proc: The subprocess process to clean up
            task: The task being run (for logging purposes)
            timeout: Timeout in seconds to wait for graceful termination
        """
        if proc.returncode is not None:
            # Process already exited
            return

        # Try graceful termination first
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Force kill if graceful termination failed
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                # Ignore errors during force kill - process may already be dead
                pass
        except Exception:
            # Ignore other errors during termination
            pass

    async def _run_mock_worker(self, task: Task):
        """Run a mock worker for testing (simulates success)."""
        # Update status
        task.status = "running"
        task.started_at = datetime.now()
        self.renderer.update_worker_status(task.id, status="running")

        # Create a mock session
        class MockProcess:
            def __init__(self):
                self.returncode = 0

        session = WorkerSession(
            task=task,
            process=MockProcess(),  # type: ignore
        )
        self.active_workers[task.id] = session

        # Simulate progress
        for progress in [25, 50, 75, 100]:
            await asyncio.sleep(0.5)  # Short delay for visual effect
            task.progress = progress
            self.renderer.update_worker_status(task.id, progress=progress)
            self.renderer.append_worker_output(task.id, f"Mock progress: {progress}%\n")

        # Complete
        task.exit_code = 0
        task.status = "done"
        task.completed_at = datetime.now()

        # Update UI
        self.renderer.update_worker_status(
            task.id,
            status=task.status,
            progress=100,
        )

        del self.active_workers[task.id]

        # Save final task state
        await self._save_task_file(task)

    async def _cleanup_and_recreate_worktree(self, task: Task):
        """Clean up existing worktree/branch and recreate fresh."""
        # First, try to prune any stale worktrees
        prune_proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "prune",
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await prune_proc.communicate()

        # Remove worktree if it exists
        if task.worktree.exists():
            # Try to remove via git first
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", str(task.worktree), "--force",
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Fall back to direct removal if git removal failed
            if task.worktree.exists():
                try:
                    # Remove .git file first (it's a file, not a directory in worktrees)
                    # This avoids permission issues on some systems
                    git_file = task.worktree / ".git"
                    if git_file.exists():
                        git_file.unlink()
                    shutil.rmtree(task.worktree, ignore_errors=False)
                except Exception as e:
                    # If we can't remove it, log but continue - git might still work
                    self.renderer.append_worker_output(
                        task.id, f"[Warning] Could not remove worktree directory: {e}"
                    )

        # Try to delete branch if it exists (ignore errors)
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "-D", task.branch,
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()  # Ignore error (branch may not exist)

    async def _ensure_worktree(self, task: Task):
        """Ensure the worktree exists for a task."""
        if task.worktree.exists():
            # Verify it's a valid git worktree
            git_dir = task.worktree / ".git"
            if git_dir.exists():
                return
            # Invalid worktree, remove and recreate
            # Remove .git file first to avoid permission issues
            git_file = task.worktree / ".git"
            if git_file.exists():
                try:
                    git_file.unlink(missing_ok=True)
                except Exception:
                    pass  # Continue even if unlink fails
            try:
                shutil.rmtree(task.worktree, ignore_errors=True)
            except Exception:
                pass  # Continue even if rmtree fails

        # Ensure parent directory exists
        task.worktree.parent.mkdir(parents=True, exist_ok=True)

        # Create worktree with retry logic for locked worktrees
        max_retries = 2
        proc = None
        stdout = b""
        stderr = b""
        for attempt in range(max_retries):
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(task.worktree), "-b", task.branch, self.base_branch,
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                return  # Success

            error_msg = stderr.decode() if stderr else "Unknown error"
            error_lower = error_msg.lower()

            # If worktree is locked, prune and retry
            if "locked" in error_lower and attempt < max_retries - 1:
                self.renderer.append_worker_output(
                    task.id, f"[Warning] Worktree locked, pruning and retrying..."
                )
                prune_proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "prune",
                    cwd=self.project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await prune_proc.communicate()
                await asyncio.sleep(0.5)  # Brief delay before retry
                continue

            # For other errors, break and let the error handling below deal with it
            break

        if proc is None or proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            error_lower = error_msg.lower()

            # Handle branch already exists - clean up and retry
            if "already exists" in error_lower:
                await self._cleanup_and_recreate_worktree(task)
                # Retry creating worktree
                proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "add", str(task.worktree), "-b", task.branch, self.base_branch,
                    cwd=self.project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return
                error_msg = stderr.decode() if stderr else error_msg
                error_lower = error_msg.lower()

            # Handle worktree already registered - prune and retry
            if "is already registered" in error_lower or "already registered" in error_lower:
                # Prune stale worktrees
                prune_proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "prune",
                    cwd=self.project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await prune_proc.communicate()

                # Try to remove the existing worktree directory if it exists
                if task.worktree.exists():
                    # Remove .git file first to avoid permission issues
                    git_file = task.worktree / ".git"
                    if git_file.exists():
                        git_file.unlink(missing_ok=True)
                    shutil.rmtree(task.worktree, ignore_errors=True)

                # Retry creating worktree
                proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "add", str(task.worktree), "-b", task.branch, self.base_branch,
                    cwd=self.project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return
                error_msg = stderr.decode() if stderr else error_msg

            # Clean up on failure to avoid leaving partial state
            if task.worktree.exists():
                try:
                    # Remove .git file first to avoid permission issues
                    git_file = task.worktree / ".git"
                    if git_file.exists():
                        git_file.unlink(missing_ok=True)
                    shutil.rmtree(task.worktree, ignore_errors=True)
                except Exception:
                    pass

            raise RuntimeError(f"Failed to create worktree for {task.id}: {error_msg}")

    async def _save_task_file(self, task: Task) -> Path:
        """Save task definition to file."""
        task_file = self.collab_dir / "tasks" / f"{task.id}.json"
        # Ensure parent directory exists (in case it was deleted)
        task_file.parent.mkdir(parents=True, exist_ok=True)

        # Calculate attempts from retry_count
        attempts = task.retry_count + 1 if task.status != "pending" else 0

        task_data = {
            "id": task.id,
            "title": task.title,
            "prompt": task.prompt,
            "branch": task.branch,
            "worktree": str(task.worktree),
            "base_branch": self.base_branch,
            "status": task.status,
            "exit_code": task.exit_code,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "health": {
                "timeout_minutes": 60,
                "max_attempts": task.max_retries + 1,
                "attempts": attempts,
                "last_activity": datetime.now().isoformat(),
            },
        }
        task_file.write_text(json.dumps(task_data, indent=2))
        return task_file

    def _create_worker_prompt_file(self, task: Task) -> Path:
        """Create the worker prompt file."""
        prompt_file = task.worktree / ".worker-prompt.txt"

        # Load worker rules if available
        rules_file = self.project_root / "scripts" / "rules-worker.md"
        if rules_file.exists():
            rules = rules_file.read_text()
        else:
            rules = """You are a worker agent. Your job is to implement the assigned task.

Rules:
1. Work in the current directory (worktree)
2. Make commits as you progress
3. Run tests to verify your work
4. Exit with code 0 on success, non-zero on failure
5. If you need help, create a human call file in human-calls/ with your task ID as prefix (e.g., human-calls/{task_id}-question.md)"""

        prompt = f"""{rules}

---

## Your Task (ID: {task.id})

**Title:** {task.title}

**Instructions:**
{task.prompt}

## Important

- Work in: {task.worktree}
- Branch: {task.branch}
- Base branch: {self.base_branch}

When complete, exit with code 0.
"""

        prompt_file.write_text(prompt)
        return prompt_file

    def _update_progress_from_output(self, task: Task, line: str):
        """Update task progress based on output patterns.

        Recognizes various progress indicators from common tools and frameworks.
        """
        # Look for progress indicators in output
        patterns = [
            # Standard progress patterns
            (r"progress[:\s]+(\d+)%", 1),
            (r"(\d+)%\s+complete", 1),
            (r"(\d+)%\s+done", 1),
            (r"completed[:\s]+(\d+)/(\d+)", lambda m: int(m.group(1)) / int(m.group(2)) * 100),
            # pytest patterns
            (r"(\d+) passed.*in\s+[\d.]+s", lambda m: 100),  # All tests passed
            (r"passed.*?(\d+)%", 1),
            (r"(\d+) failed", lambda m: 50),  # Partial progress on failures
            # Build/compilation patterns
            (r"\[(\d+)/(\d+)\]", lambda m: int(m.group(1)) / int(m.group(2)) * 100),
            (r"Compiling.*?(\d+)%", 1),
            (r"Building.*?(\d+)%", 1),
            # Git patterns
            (r"Receiving objects:\s+(\d+)%", 1),
            (r"Resolving deltas:\s+(\d+)%", 1),
            # Package manager patterns
            (r"Installing.*?(\d+)%", 1),
            (r"Downloading.*?(\d+)%", 1),
            # Task completion patterns
            (r"Task (\d+)/(\d+) complete", lambda m: int(m.group(1)) / int(m.group(2)) * 100),
            (r"Step (\d+)/(\d+)", lambda m: int(m.group(1)) / int(m.group(2)) * 100),
            # Claude Code specific patterns
            (r"✓\s+\w+.*\((\d+)%\)", 1),
            (r"Running tests.*?\[(\d+)%\]", 1),
            # SE3 workflow patterns
            (r"Iteration (\d+)/(\d+)", lambda m: int(m.group(1)) / int(m.group(2)) * 100),
            (r"se3:work.*iteration (\d+)/(\d+)", lambda m: int(m.group(1)) / int(m.group(2)) * 100),
        ]

        for pattern, group in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                try:
                    if callable(group):
                        progress = int(group(match))
                    else:
                        progress = int(match.group(group))
                    task.progress = min(100, max(0, progress))
                    self.renderer.update_worker_status(task.id, progress=task.progress)
                    break
                except (ValueError, ZeroDivisionError):
                    # Skip if conversion fails
                    continue

    async def _run_manager_review(self) -> ManagerDecision:
        """Run Manager to review completed tasks."""
        # Build context from completed tasks
        tasks_summary = []
        for task in self.tasks.values():
            status_icon = "✓" if task.status == "done" else "✗"
            tasks_summary.append(
                f"{status_icon} {task.id}: [{task.status}] {task.title}"
            )

        # Calculate success/failure stats
        completed_count = sum(1 for t in self.tasks.values() if t.status == "done")
        failed_count = sum(1 for t in self.tasks.values() if t.status == "failed")
        total_count = len(self.tasks)

        objective = f"""Review completed collaboration session.

All tasks have completed execution.

Task Summary ({completed_count}/{total_count} completed, {failed_count} failed):
{chr(10).join(tasks_summary)}

Please review the results and decide next action.

IMPORTANT: Respond with a JSON object in this exact format:
{{
  "action": "complete",
  "reason": "explanation of your decision",
  "summary": "human-readable summary"
}}

Valid actions are:
- "complete": All tasks succeeded or acceptable level of success achieved
- "retry": Some tasks failed and should be retried with adjusted approach
- "escalate": Human intervention is needed to resolve issues
- "split": Failed tasks should be broken down into smaller sub-tasks"""

        # Use same planning method with review context
        return await self._run_manager_plan(objective)

    def _build_manager_prompt(self, objective: str) -> str:
        """Build the prompt for the Manager agent."""
        # Load manager rules if available
        rules_file = self.project_root / "scripts" / "rules-manager.md"
        if rules_file.exists():
            rules = rules_file.read_text()
        else:
            rules = """You are a Manager agent. Your job is to plan and coordinate work.

Respond with valid JSON matching the expected schema."""

        return f"""{rules}

---

## Objective

{objective}

## Instructions

Analyze the objective and create a task plan. Break down complex work into
manageable, independent tasks that can be executed in parallel.

Respond ONLY with valid JSON in this format:

{{
  "action": "plan",
  "tasks": [
    {{
      "id": "task-001",
      "title": "Short description of the task",
      "prompt": "Detailed instructions for the worker"
    }}
  ],
  "reason": "Why you chose this plan",
  "summary": "Human-readable summary"
}}

Rules:
- Create 2-5 tasks for parallel execution
- Each task should be independent and self-contained
- Tasks should not depend on each other unless necessary
- Use descriptive IDs like "task-001", "task-002"
- Provide clear, detailed prompts for workers
"""

    def _parse_manager_decision(self, output: str) -> ManagerDecision:
        """Parse the Manager's JSON decision from output.

        Handles both plain JSON output and stream-json format with multiple messages.
        Uses a robust JSON extraction method that handles nested braces correctly.
        """
        # First, try to parse as stream-json (multiple JSON lines)
        json_objects = []
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # Look for assistant messages with content
                if obj.get("type") == "assistant":
                    message = obj.get("message", {})
                    content = message.get("content", [])
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text = item.get("text", "")
                            # Try to find JSON in the text
                            json_objects.append(text)
                # Also collect raw result messages
                elif obj.get("type") == "result":
                    json_objects.append(obj.get("result", ""))
                elif "action" in obj:
                    # Plain JSON response (manager decision directly)
                    json_objects.append(line)
            except json.JSONDecodeError:
                # Not JSON, might be plain text output
                json_objects.append(line)

        # Combine all collected text and search for JSON
        combined_output = '\n'.join(json_objects) if json_objects else output

        # Try to extract JSON from markdown code blocks first
        code_block_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', combined_output, re.DOTALL)
        if code_block_match:
            json_str = code_block_match.group(1).strip()
            try:
                data = json.loads(json_str)
                return self._create_manager_decision(data)
            except json.JSONDecodeError:
                # Continue to try other methods
                pass

        # Use robust brace matching to find JSON objects
        json_str = self._extract_json_with_brace_matching(combined_output)
        if json_str:
            try:
                data = json.loads(json_str)
                return self._create_manager_decision(data)
            except json.JSONDecodeError as e:
                return ManagerDecision(
                    action="escalate",
                    reason=f"JSON parse error: {e}",
                )

        return ManagerDecision(
            action="escalate",
            reason="Could not parse manager response as JSON",
        )

    def _extract_json_with_brace_matching(self, text: str) -> str | None:
        """Extract a JSON object from text using proper brace matching.

        This handles nested braces correctly by counting open/close braces,
        ignoring braces inside strings.
        """
        in_string = False
        escape_next = False
        brace_depth = 0
        start_idx = -1

        for i, char in enumerate(text):
            if escape_next:
                escape_next = False
                continue

            if char == '\\' and in_string:
                escape_next = True
                continue

            if char == '"' and not in_string:
                in_string = True
                continue
            elif char == '"' and in_string:
                in_string = False
                continue

            # Only count braces when not inside a string
            if not in_string:
                if char == '{':
                    if brace_depth == 0:
                        start_idx = i
                    brace_depth += 1
                elif char == '}':
                    brace_depth -= 1
                    if brace_depth == 0 and start_idx >= 0:
                        # Found a complete JSON object
                        return text[start_idx:i+1]
                    elif brace_depth < 0:
                        # Mismatched braces, reset
                        brace_depth = 0
                        start_idx = -1

        return None

    def _create_manager_decision(self, data: dict) -> ManagerDecision:
        """Create a ManagerDecision from parsed JSON data."""
        return ManagerDecision(
            action=data.get("action", "escalate"),
            tasks=data.get("tasks", []),
            target_task=data.get("target_task", ""),
            merge_branch=data.get("merge_branch", ""),
            retry_prompt=data.get("retry_prompt", ""),
            reason=data.get("reason", ""),
            summary=data.get("summary", ""),
        )

    def _get_clean_env(self) -> dict[str, str]:
        """Get clean environment for subprocesses."""
        env = dict(os.environ)
        # Remove CLAUDECODE to avoid nested session detection
        env.pop("CLAUDECODE", None)
        env["SE3_AGENT_ROLE"] = "collab"
        env["SE3_PROJECT_ROOT"] = str(self.project_root)
        return env

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of the collaboration session."""
        return {
            "session_id": self.session_id,
            "total_tasks": len(self.tasks),
            "completed": sum(1 for t in self.tasks.values() if t.status == "done"),
            "failed": sum(1 for t in self.tasks.values() if t.status == "failed"),
            "pending": sum(1 for t in self.tasks.values() if t.status == "pending"),
            "running": sum(1 for t in self.tasks.values() if t.status == "running"),
            "tasks": [
                {
                    "id": t.id,
                    "status": t.status,
                    "title": t.title,
                }
                for t in self.tasks.values()
            ],
        }
