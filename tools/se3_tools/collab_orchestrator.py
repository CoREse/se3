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

        # Send prompt
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError) as e:
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
        await self._ensure_worktree(task)

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
            return

        cmd_entry = runner.commands[0]
        cmd_name = cmd_entry["cmd"]

        # Check if command exists
        if not shutil.which(cmd_name):
            task.status = "failed"
            task.exit_code = -1
            self.renderer.append_worker_output(task.id, f"[Error] Claude command '{cmd_name}' not found")
            return

        # Launch worker
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

        # Check if process started successfully
        if proc.returncode is not None and proc.returncode != 0:
            # Process failed immediately
            task.status = "failed"
            task.exit_code = proc.returncode
            self.renderer.append_worker_output(task.id, f"[Error] Worker process exited immediately with code {proc.returncode}")
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
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass  # Process may already be dead
            # Update task status before re-raising
            task.status = "failed"
            task.exit_code = -1
            task.completed_at = datetime.now()
            del self.active_workers[task.id]
            raise

        # Wait for completion
        try:
            await proc.wait()
            task.exit_code = proc.returncode
            task.status = "done" if proc.returncode == 0 else "failed"
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
            stdout, stderr = await proc.communicate()

            # If git removal failed, try to unregister the worktree from git's registry
            # without deleting files (in case of permission issues or locked files)
            if task.worktree.exists() and proc.returncode != 0:
                # Try to unregister from git's worktree list without deleting files
                unregister_proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "remove", "--force", str(task.worktree),
                    cwd=self.project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await unregister_proc.communicate()

            # Fall back to direct removal if git removal failed
            if task.worktree.exists():
                try:
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
            shutil.rmtree(task.worktree, ignore_errors=True)

        # Ensure parent directory exists
        task.worktree.parent.mkdir(parents=True, exist_ok=True)

        # Create worktree
        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", str(task.worktree), "-b", task.branch, self.base_branch,
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
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
                    shutil.rmtree(task.worktree, ignore_errors=True)
                    # Also try to remove from git worktree registry
                    await asyncio.create_subprocess_exec(
                        "git", "worktree", "remove", str(task.worktree), "--force",
                        cwd=self.project_root,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except Exception:
                    pass

            raise RuntimeError(f"Failed to create worktree for {task.id}: {error_msg}")

    async def _save_task_file(self, task: Task) -> Path:
        """Save task definition to file."""
        task_file = self.collab_dir / "tasks" / f"{task.id}.json"
        task_data = {
            "id": task.id,
            "title": task.title,
            "prompt": task.prompt,
            "branch": task.branch,
            "worktree": str(task.worktree),
            "base_branch": self.base_branch,
            "status": task.status,
            "health": {
                "timeout_minutes": 60,
                "max_attempts": 3,
                "attempts": 0,
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
5. If you need help, create a human call file"""

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

        objective = f"""Review completed collaboration session.

All tasks have completed execution.

Task Summary:
{chr(10).join(tasks_summary)}

Please review the results and decide next action.
Respond with JSON action: "complete" if all tasks succeeded, "retry" if some failed,
or "escalate" if human intervention is needed."""

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
            except json.JSONDecodeError:
                # Not JSON, might be plain text output
                json_objects.append(line)

        # Combine all collected text and search for JSON
        combined_output = '\n'.join(json_objects) if json_objects else output

        # Try to extract JSON from markdown code blocks
        code_block_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', combined_output, re.DOTALL)
        if code_block_match:
            json_str = code_block_match.group(1).strip()
        else:
            # Try to find JSON object in the output
            # Use a more robust pattern that finds the outermost JSON object
            json_match = re.search(r'\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}', combined_output, re.DOTALL)
            if not json_match:
                return ManagerDecision(
                    action="escalate",
                    reason="Could not parse manager response as JSON",
                )
            json_str = json_match.group()

        try:
            data = json.loads(json_str)
            return ManagerDecision(
                action=data.get("action", "escalate"),
                tasks=data.get("tasks", []),
                target_task=data.get("target_task", ""),
                merge_branch=data.get("merge_branch", ""),
                retry_prompt=data.get("retry_prompt", ""),
                reason=data.get("reason", ""),
                summary=data.get("summary", ""),
            )
        except json.JSONDecodeError as e:
            return ManagerDecision(
                action="escalate",
                reason=f"JSON parse error: {e}",
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
