"""Git Worktree Collaboration command for SE 3.0.

Implements the git-worktree-collab spec:
- Launches the orchestrator to manage multi-agent collaboration
- Manager and workers run as independent claude -p processes
- Each worker gets its own git worktree with full context window
- Health monitoring at all layers with automatic recovery
"""

import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime

import typer

app = typer.Typer(invoke_without_command=True)


def find_project_root() -> Path:
    """Find the project root by looking for .claude/ directory."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".claude").is_dir():
            return current
        current = current.parent
    return Path.cwd()


def get_collab_dir(project_root: Path) -> Path:
    return project_root / ".collab"


def print_status(project_root: Path):
    """Print current collaboration status."""
    collab_dir = get_collab_dir(project_root)

    config_file = collab_dir / "config.json"
    if not config_file.exists():
        typer.echo("No active collaboration session.")
        raise typer.Exit(0)

    config = json.loads(config_file.read_text())
    typer.echo(f"\n{'=' * 60}")
    typer.echo("SE3 Collaboration Status")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  Session:    {config.get('session_id', 'unknown')}")
    typer.echo(f"  Objective:  {config.get('objective', 'unknown')}")
    typer.echo(f"  Base:       {config.get('base_branch', 'unknown')}")
    typer.echo(f"  Status:     {config.get('status', 'unknown')}")
    typer.echo(f"  Created:    {config.get('created_at', 'unknown')}")
    typer.echo(f"  Workers:    max {config.get('max_parallel_workers', 3)}")

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

    # Check orchestrator
    pid_file = collab_dir / "orchestrator.pid"
    if pid_file.exists():
        pid = pid_file.read_text().strip()
        try:
            # Check if process exists
            subprocess.run(
                ["kill", "-0", pid],
                capture_output=True,
                check=True,
            )
            typer.echo(f"\n  Orchestrator: running (PID {pid})")
        except subprocess.CalledProcessError:
            typer.echo(f"\n  Orchestrator: dead (stale PID {pid})")
    else:
        typer.echo(f"\n  Orchestrator: not running")

    typer.echo(f"{'=' * 60}\n")


def abort_session(project_root: Path):
    """Abort the current collaboration session."""
    collab_dir = get_collab_dir(project_root)

    if not collab_dir.exists():
        typer.echo("No active collaboration session.")
        raise typer.Exit(0)

    # Kill orchestrator
    pid_file = collab_dir / "orchestrator.pid"
    if pid_file.exists():
        pid = pid_file.read_text().strip()
        subprocess.run(["kill", "-TERM", pid], capture_output=True)
        typer.echo(f"Killed orchestrator (PID {pid})")

    # Kill workers
    tasks_dir = collab_dir / "tasks"
    if tasks_dir.exists():
        for tf in tasks_dir.glob("task-*.json"):
            task = json.loads(tf.read_text())
            wpid = task.get("worker_pid")
            if wpid and str(wpid) != "null" and str(wpid) != "0":
                subprocess.run(
                    ["kill", "-TERM", str(wpid)], capture_output=True
                )
                typer.echo(f"Killed worker {task['id']} (PID {wpid})")

    # Cleanup worktrees
    worktree_dir = project_root / ".worktrees"
    if worktree_dir.exists():
        for wt in worktree_dir.iterdir():
            if wt.is_dir():
                subprocess.run(
                    ["git", "worktree", "remove", str(wt), "--force"],
                    cwd=project_root,
                    capture_output=True,
                )
                typer.echo(f"Removed worktree: {wt.name}")

    # Update config
    config_file = collab_dir / "config.json"
    if config_file.exists():
        config = json.loads(config_file.read_text())
        config["status"] = "aborted"
        config_file.write_text(json.dumps(config, indent=2))

    pid_file.unlink(missing_ok=True)
    typer.echo("Collaboration session aborted.")


@app.callback()
def collab(
    objective: str = typer.Argument(None, help="Collaboration objective"),
    resume: bool = typer.Option(False, "--resume", help="Resume a previous session"),
    status: bool = typer.Option(False, "--status", help="Show collaboration status"),
    abort: bool = typer.Option(False, "--abort", help="Abort and cleanup"),
    no_watchdog: bool = typer.Option(
        False, "--no-watchdog", help="Run without watchdog (testing)"
    ),
    project_root: str = typer.Option(
        None, "--project-root", "-p", help="Project root directory"
    ),
):
    """Manage git-worktree based multi-agent collaboration."""
    root = Path(project_root) if project_root else find_project_root()

    if status:
        print_status(root)
        raise typer.Exit(0)

    if abort:
        abort_session(root)
        raise typer.Exit(0)

    if not resume and not objective:
        typer.echo("Error: provide an objective or use --resume")
        raise typer.Exit(1)

    # Find orchestrator script
    script = root / "scripts" / "collab-orchestrator.sh"
    if not script.exists():
        typer.echo(f"Error: orchestrator script not found at {script}")
        raise typer.Exit(1)

    # Build command
    cmd = ["bash", str(script)]
    if resume:
        cmd.append("--resume")
    if no_watchdog:
        cmd.append("--no-watchdog")
    if objective:
        cmd.append(objective)

    # Launch orchestrator
    typer.echo(f"Launching collaboration orchestrator...")
    typer.echo(f"  Project: {root}")
    if objective:
        typer.echo(f"  Objective: {objective}")
    typer.echo(f"  Script: {script}")
    typer.echo()

    env = {
        **dict(subprocess.os.environ),
        "PROJECT_ROOT": str(root),
    }

    try:
        result = subprocess.run(cmd, env=env, cwd=root)
        raise typer.Exit(result.returncode)
    except KeyboardInterrupt:
        typer.echo("\nInterrupted. Use 'se3 collab --abort' to cleanup.")
        raise typer.Exit(130)
