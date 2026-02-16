"""SE3 Collaboration v2 - Integration with External Controller.

This module provides backward-compatible interface to the new
External Controller while maintaining the same CLI experience.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from ..controller.api_server import (
    CollaborationManager,
    SessionManager,
    find_project_root,
)
from ..controller.persistence import RecoveryManager

app = typer.Typer(invoke_without_command=True)


def is_controller_running() -> bool:
    """Check if external controller daemon is running."""
    controller_dir = Path.home() / ".se3" / "controller"
    pid_file = controller_dir / "daemon.pid"

    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def start_controller_daemon() -> bool:
    """Start the controller daemon if not running."""
    if is_controller_running():
        return True

    typer.echo("Starting SE3 Controller daemon...")

    result = subprocess.run(
        [sys.executable, "-m", "se3_tools.controller.daemon", "daemon"],
        capture_output=True,
    )

    # Give it time to start
    import time
    time.sleep(1)

    return is_controller_running()


@app.callback()
def collab_v2(
    objective: str = typer.Argument(None, help="Collaboration objective"),
    resume: bool = typer.Option(False, "--resume", help="Resume a previous session"),
    status: bool = typer.Option(False, "--status", help="Show collaboration status"),
    abort: bool = typer.Option(False, "--abort", help="Abort and cleanup"),
    daemon: bool = typer.Option(False, "--daemon", help="Start orchestrator daemon (auto mode)"),
    manual: bool = typer.Option(False, "--manual", help="Manual mode: generate plan, print commands"),
    launch_manager_flag: bool = typer.Option(False, "--launch-manager", help="Launch manager for event type (internal)"),
    launch_worker_flag: bool = typer.Option(False, "--launch-worker", help="Launch worker for task (internal)"),
    event_type: str = typer.Option("plan", "--event", help="Event type for manager"),
    task_id: str = typer.Option(None, "--task", help="Task ID for worker"),
    mock: bool = typer.Option(False, "--mock", help="Use mock for testing"),
    project_root: str = typer.Option(None, "--project-root", "-p", help="Project root directory"),
    use_v1: bool = typer.Option(False, "--v1", help="Use legacy v1 implementation"),
):
    """Manage git-worktree based multi-agent collaboration (v2 with External Controller).

    Uses External Controller for process management, auto-commit, and recovery.
    """
    root = Path(project_root) if project_root else find_project_root()

    # Check for v1 flag or fallback
    if use_v1 or not is_controller_running():
        if daemon and not use_v1:
            # Try to start v2
            if start_controller_daemon():
                typer.echo("Using External Controller v2")
                return _run_v2(root, objective, resume, daemon, manual)
            else:
                typer.echo("Warning: Could not start External Controller, falling back to v1")

        if not use_v1:
            # Fall back to v1 implementation
            from . import collab as collab_v1
            return collab_v1.collab(
                objective=objective,
                resume=resume,
                status=status,
                abort=abort,
                daemon=daemon,
                manual=manual,
                launch_manager_flag=launch_manager_flag,
                launch_worker_flag=launch_worker_flag,
                event_type=event_type,
                task_id=task_id,
                mock=mock,
                project_root=project_root,
            )

    # Run v2
    return _run_v2(root, objective, resume, daemon, manual, status, abort)


def _run_v2(
    root: Path,
    objective: Optional[str],
    resume: bool,
    daemon: bool,
    manual: bool,
    status: bool = False,
    abort: bool = False,
):
    """Run collaboration using v2 External Controller."""

    if status:
        return _show_status_v2(root)

    if abort:
        return _abort_v2(root)

    if manual:
        return _run_manual_v2(root, objective)

    if daemon or objective:
        # Check recovery first
        recovery = RecoveryManager()
        recovered = recovery.check_and_recover()
        if recovered:
            typer.echo(f"Recovered {len(recovered)} sessions")

        # Start collaboration
        collab = CollaborationManager()

        if resume:
            # Load existing config
            collab_dir = root / ".collab"
            config_file = collab_dir / "config.json"
            if config_file.exists():
                config = json.loads(config_file.read_text())
                objective = config.get("objective", "Resumed session")
                max_workers = config.get("max_parallel_workers", 3)
            else:
                typer.echo("No session to resume")
                raise typer.Exit(1)
        else:
            max_workers = 3

        collab_id = collab.start_collaboration(objective, max_workers, root)
        typer.echo(f"Collaboration started: {collab_id}")
        typer.echo(f"Objective: {objective}")

        if daemon:
            typer.echo("Running in daemon mode. Press Ctrl+C to stop.")
            try:
                # Keep main thread alive
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                typer.echo("\nStopping collaboration...")
        return


def _show_status_v2(root: Path):
    """Show collaboration status using v2."""
    collab_dir = root / ".collab"

    if not (collab_dir / "config.json").exists():
        typer.echo("No active collaboration session.")
        raise typer.Exit(0)

    config = json.loads((collab_dir / "config.json").read_text())

    typer.echo(f"\n{'=' * 60}")
    typer.echo("SE3 Collaboration Status (v2)")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  Session:    {config.get('collab_id', 'unknown')}")
    typer.echo(f"  Objective:  {config.get('objective', 'unknown')}")
    typer.echo(f"  Status:     {config.get('status', 'unknown')}")
    typer.echo(f"  Created:    {config.get('created_at', 'unknown')}")
    typer.echo(f"  Workers:    max {config.get('max_workers', 3)}")

    # List tasks
    tasks_dir = collab_dir / "tasks"
    if tasks_dir.exists():
        task_files = sorted(tasks_dir.glob("task-*.json"))
        if task_files:
            typer.echo(f"\n  Tasks:")
            for tf in task_files:
                task = json.loads(tf.read_text())
                status_icon = {
                    "pending": "○",
                    "in_progress": "◉",
                    "done": "●",
                    "failed": "✗",
                    "timeout": "⏱",
                    "blocked": "◫",
                    "escalated": "⚠",
                }.get(task.get("status", ""), "?")
                attempts = task.get("health", {}).get("attempts", 0)
                max_att = task.get("health", {}).get("max_attempts", 3)
                typer.echo(
                    f"    {status_icon} {task['id']}: [{task['status']}] "
                    f"{task.get('title', '')} "
                    f"(attempts: {attempts}/{max_att})"
                )

    # Check controller status
    if is_controller_running():
        typer.echo(f"\n  Controller: running (v2)")
    else:
        typer.echo(f"\n  Controller: not running")

    typer.echo(f"{'=' * 60}\n")


def _abort_v2(root: Path):
    """Abort collaboration session."""
    collab_dir = root / ".collab"

    if not collab_dir.exists():
        typer.echo("No active collaboration session.")
        raise typer.Exit(0)

    # Kill workers
    tasks_dir = collab_dir / "tasks"
    if tasks_dir.exists():
        for tf in tasks_dir.glob("task-*.json"):
            task = json.loads(tf.read_text())
            pid = task.get("worker_pid")
            if pid and str(pid) != "null" and str(pid) != "0":
                subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
                typer.echo(f"Killed worker {task['id']} (PID {pid})")

    # Cleanup worktrees
    worktree_dir = root / ".worktrees"
    if worktree_dir.exists():
        for wt in worktree_dir.iterdir():
            if wt.is_dir():
                subprocess.run(
                    ["git", "worktree", "remove", str(wt), "--force"],
                    cwd=root,
                    capture_output=True,
                )
                typer.echo(f"Removed worktree: {wt.name}")

    # Update config
    config_file = collab_dir / "config.json"
    if config_file.exists():
        config = json.loads(config_file.read_text())
        config["status"] = "aborted"
        config_file.write_text(json.dumps(config, indent=2))

    typer.echo("Collaboration session aborted.")


def _run_manual_v2(root: Path, objective: Optional[str]):
    """Run manual mode."""
    if not objective:
        typer.echo("Error: provide an objective")
        raise typer.Exit(1)

    collab_dir = root / ".collab"
    collab_dir.mkdir(exist_ok=True)

    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    base_branch = result.stdout.strip() or "master"

    config = {
        "collab_id": f"collab-manual",
        "objective": objective,
        "base_branch": base_branch,
        "created_at": __import__('datetime').datetime.now().isoformat(),
        "max_workers": 3,
        "status": "manual",
    }
    (collab_dir / "config.json").write_text(json.dumps(config, indent=2))

    typer.echo(f"\n{'=' * 60}")
    typer.echo("SE3 Collaboration v2 - Manual Mode")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"\nObjective: {objective}")
    typer.echo(f"Base branch: {base_branch}")
    typer.echo(f"\nStep 1: Use External Controller to spawn manager:")
    typer.echo(f"  se3 controller spawn-manager plan \"{objective}\"")
    typer.echo(f"\nStep 2: After manager creates tasks, spawn workers:")
    typer.echo(f"  se3 controller spawn-worker task-001")
    typer.echo(f"\nOr use daemon mode for automatic execution:")
    typer.echo(f"  se3 collab --daemon \"{objective}\"")
    typer.echo(f"{'=' * 60}\n")


# Additional commands for v2

@app.command("spawn-manager")
def spawn_manager(
    event_type: str = typer.Argument(...),
    context: str = typer.Argument(""),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
):
    """Spawn a manager agent (v2)."""
    root = Path(project_root) if project_root else find_project_root()

    collab = CollaborationManager()
    collab._spawn_manager(event_type, context, root, "manual")
    typer.echo(f"Manager spawned for event: {event_type}")


@app.command("spawn-worker")
def spawn_worker(
    task_id: str = typer.Argument(...),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
):
    """Spawn a worker agent for a task (v2)."""
    root = Path(project_root) if project_root else find_project_root()

    task_file = root / ".collab" / "tasks" / f"{task_id}.json"
    if not task_file.exists():
        typer.echo(f"Task not found: {task_id}")
        raise typer.Exit(1)

    task = json.loads(task_file.read_text())
    prompt = task.get("prompt", "")

    collab = CollaborationManager()
    collab._spawn_worker(task_id, prompt, root, "manual")
    typer.echo(f"Worker spawned for task: {task_id}")
