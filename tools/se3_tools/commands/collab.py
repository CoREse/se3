"""Git Worktree Collaboration command for SE 3.0.

Independent Entry Mode Architecture:
- Orchestrator runs as daemon (pure bash, no AI)
- Manager and workers are launched as independent Claude sessions
- Communication via file system (.collab/) and MCP

Usage modes:
1. --daemon: Start orchestrator daemon (runs in background)
2. --manual: Generate task files, print commands for manual execution
3. --launch-manager: Launch manager for a task (internal use)
4. --launch-worker: Launch worker for a task (internal use)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List

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


def load_config(project_root: Path) -> dict:
    """Load se3.config.yaml if exists."""
    config_file = project_root / "se3.config.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def ensure_collab_structure(project_root: Path):
    """Ensure .collab directory structure exists."""
    collab_dir = get_collab_dir(project_root)
    for subdir in ["tasks", "logs", "events", "pending", "completed"]:
        (collab_dir / subdir).mkdir(parents=True, exist_ok=True)


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


def generate_manager_prompt(project_root: Path, event_type: str, context: str) -> str:
    """Generate prompt for manager agent."""
    collab_dir = get_collab_dir(project_root)

    # Load manager rules
    rules_file = project_root / "scripts" / "rules-manager.md"
    if rules_file.exists():
        rules = rules_file.read_text()
    else:
        rules = "You are a manager agent. Respond with valid JSON."

    # Load current tasks summary
    tasks_summary = []
    tasks_dir = collab_dir / "tasks"
    if tasks_dir.exists():
        for tf in sorted(tasks_dir.glob("task-*.json")):
            task = json.loads(tf.read_text())
            tasks_summary.append(
                f"- {task['id']}: [{task['status']}] {task.get('title', '')}"
            )

    config_file = collab_dir / "config.json"
    base_branch = "master"
    if config_file.exists():
        config = json.loads(config_file.read_text())
        base_branch = config.get("base_branch", "master")

    return f"""{rules}

---

## Current State
Project root: {project_root}
Base branch: {base_branch}

## All Tasks
{chr(10).join(tasks_summary) if tasks_summary else "(no tasks yet)"}

## Event
Type: {event_type}
Context:
{context}

## Instructions
Analyze the event and decide the next action. Respond ONLY with valid JSON matching this schema:
{{
  "action": "plan|merge|reject|retry|split|escalate|complete",
  "tasks": [...],
  "target_task": "task-id",
  "merge_branch": "branch-name",
  "retry_prompt": "adjusted prompt for retry",
  "reason": "explanation",
  "summary": "human-readable summary of decision"
}}

Rules:
- For 'plan': include full task definitions in 'tasks' array
- For 'merge': set target_task and merge_branch
- For 'reject': set target_task and reason (becomes feedback for worker retry)
- For 'retry': set target_task and retry_prompt
- For 'split': set target_task and new sub-tasks in 'tasks'
- For 'escalate': set reason (will be sent to human)
- For 'complete': when all tasks are merged and done
- If unsure, use 'escalate' rather than guessing
"""


def generate_worker_prompt(project_root: Path, task_id: str) -> str:
    """Generate prompt for worker agent."""
    collab_dir = get_collab_dir(project_root)
    task_file = collab_dir / "tasks" / f"{task_id}.json"

    # Load worker rules
    rules_file = project_root / "scripts" / "rules-worker.md"
    if rules_file.exists():
        rules = rules_file.read_text()
    else:
        rules = "You are a worker agent. Implement the task, run tests, commit."

    task = json.loads(task_file.read_text())
    task_prompt = task.get("prompt", "")

    return f"""{rules}

---

## Your Task (ID: {task_id})

{task_prompt}

## Important
- Work in the provided worktree directory
- Commit your changes when done
- Exit with code 0 on success, non-zero on failure
"""


def launch_manager(project_root: Path, event_type: str, context: str) -> subprocess.Popen:
    """Launch manager as independent Claude process.

    Uses ClaudeRunner for priority-based command selection.
    """
    from ..claude_runner import ClaudeRunner

    prompt = generate_manager_prompt(project_root, event_type, context)

    # Write prompt to file for reference and use @file syntax to avoid CLI parsing issues
    collab_dir = get_collab_dir(project_root)
    prompt_file = collab_dir / "logs" / f"manager-{event_type}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.prompt"
    prompt_file.write_text(prompt)

    args = ["--dangerously-skip-permissions", "--print", "--output-format", "text", "--max-turns", "0", f"@{prompt_file}"]
    env = {**dict(os.environ), "SE3_AGENT_ROLE": "manager", "SE3_PROJECT_ROOT": str(project_root)}
    env.pop("CLAUDECODE", None)  # Avoid nested session detection

    runner = ClaudeRunner(project_root)
    proc, _ = runner.popen(
        args=args,
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )
    return proc


def launch_worker(project_root: Path, task_id: str) -> subprocess.Popen:
    """Launch worker as independent Claude process in worktree.

    Uses ClaudeRunner for priority-based command selection with activity monitoring.

    Returns a Popen-like object with wait() and returncode for compatibility.
    """
    from ..claude_runner import ClaudeRunner, MonitoredResult

    collab_dir = get_collab_dir(project_root)
    task_file = collab_dir / "tasks" / f"{task_id}.json"
    task = json.loads(task_file.read_text())

    worktree = task.get("worktree", f".worktrees/{task_id}")
    if not worktree.startswith("/"):
        worktree = str(project_root / worktree)

    # Ensure worktree exists
    if not Path(worktree).exists():
        branch = task.get("branch", f"collab/{task_id}")
        base_branch = task.get("base_branch", "master")
        subprocess.run(
            ["git", "worktree", "add", worktree, "-b", branch, base_branch],
            cwd=project_root,
            capture_output=True
        )

    prompt = generate_worker_prompt(project_root, task_id)

    # Write prompt to file and use @file syntax to avoid CLI parsing issues
    collab_dir = get_collab_dir(project_root)
    prompt_file = collab_dir / "logs" / f"worker-{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.prompt"
    prompt_file.write_text(prompt)

    args = ["--dangerously-skip-permissions", "--print", "--max-turns", "0", f"@{prompt_file}"]
    env = {
        **dict(os.environ),
        "SE3_TASK_ID": task_id,
        "SE3_AGENT_ROLE": "worker",
        "SE3_PROJECT_ROOT": str(project_root)
    }
    env.pop("CLAUDECODE", None)  # Avoid nested session detection

    log_file = collab_dir / "logs" / f"worker-{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    timeout_min = task.get("health", {}).get("timeout_minutes", 60)

    # Initialize last_activity if not present
    if "last_activity" not in task.get("health", {}):
        task["health"]["last_activity"] = datetime.now().isoformat()
        task_file.write_text(json.dumps(task, indent=2))

    # Activity callback: update task's last_activity timestamp whenever output is received
    def on_activity():
        try:
            with open(task_file, "r", encoding="utf-8") as f:
                current_task = json.load(f)
            current_task["health"]["last_activity"] = datetime.now().isoformat()
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(current_task, f, indent=2)
        except Exception as e:
            print(f"[collab] Failed to update last_activity: {e}", file=sys.stderr)

    runner = ClaudeRunner(project_root)

    # Use run_with_monitor for activity-based monitoring and command fallback
    result = runner.run_with_monitor(
        args=args,
        log_file=log_file,
        wall_timeout=timeout_min * 60,
        inactivity_timeout=300,  # 5 minutes without output = stuck
        cwd=Path(worktree),
        env=env,
        on_activity=on_activity,
    )

    # Create a simple wrapper object for compatibility
    class WorkerResult:
        def __init__(self, returncode: int):
            self.returncode = returncode

        def wait(self) -> int:
            return self.returncode

        def communicate(self, timeout=None) -> tuple:
            return (b"", b"")

        def poll(self) -> int:
            return self.returncode

    return WorkerResult(result.returncode)


def start_daemon(project_root: Path, objective: str, resume: bool = False):
    """Start orchestrator daemon mode."""
    ensure_collab_structure(project_root)
    collab_dir = get_collab_dir(project_root)

    if not resume:
        # Clean up old tasks from previous sessions
        tasks_dir = collab_dir / "tasks"
        if tasks_dir.exists():
            for f in tasks_dir.glob("task-*.json"):
                f.unlink()
            for f in tasks_dir.glob(".exitcode-*"):
                f.unlink()

        # Create session config
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_root,
            capture_output=True,
            text=True
        )
        base_branch = result.stdout.strip() or "master"

        config = {
            "session_id": f"collab-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            "objective": objective,
            "base_branch": base_branch,
            "created_at": datetime.now().isoformat(),
            "max_parallel_workers": 3,
            "status": "active"
        }
        (collab_dir / "config.json").write_text(json.dumps(config, indent=2))

        typer.echo(f"Created collaboration session: {config['session_id']}")
        typer.echo(f"Objective: {objective}")

    # Start the orchestrator daemon
    script = project_root / "scripts" / "collab-orchestrator.sh"
    if not script.exists():
        typer.echo(f"Error: orchestrator script not found at {script}")
        raise typer.Exit(1)

    cmd = ["bash", str(script), "--daemon"]
    if resume:
        cmd.append("--resume")
    elif objective:
        cmd.append(objective)

    # Start in background - MUST clear CLAUDECODE to avoid nested session detection
    env = {**dict(os.environ), "PROJECT_ROOT": str(project_root)}
    env.pop("CLAUDECODE", None)  # Clear to allow manager/worker to run Claude

    subprocess.Popen(
        cmd,
        stdout=open(collab_dir / "logs" / "orchestrator.log", "w"),
        stderr=subprocess.STDOUT,
        env=env,
        cwd=project_root,
        start_new_session=True  # Detach from terminal
    )

    typer.echo(f"Orchestrator daemon started.")
    typer.echo(f"Use 'se3 collab --status' to check progress")
    typer.echo(f"Use 'se3 collab --abort' to stop")


def run_manual_mode(project_root: Path, objective: str):
    """Manual mode: generate initial plan, print commands for user to execute."""
    ensure_collab_structure(project_root)
    collab_dir = get_collab_dir(project_root)

    # Get base branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root,
        capture_output=True,
        text=True
    )
    base_branch = result.stdout.strip() or "master"

    # Create session config
    config = {
        "session_id": f"collab-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "objective": objective,
        "base_branch": base_branch,
        "created_at": datetime.now().isoformat(),
        "max_parallel_workers": 3,
        "status": "manual"
    }
    (collab_dir / "config.json").write_text(json.dumps(config, indent=2))

    typer.echo(f"\n{'=' * 60}")
    typer.echo("SE3 Collaboration - Manual Mode")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"\nObjective: {objective}")
    typer.echo(f"Base branch: {base_branch}")
    typer.echo(f"\nStep 1: Launch Manager to create plan")
    typer.echo(f"  se3 collab --launch-manager plan")
    typer.echo(f"\nStep 2: After manager creates tasks, launch workers:")
    typer.echo(f"  se3 collab --launch-worker task-001")
    typer.echo(f"\nStep 3: When worker completes, review with manager:")
    typer.echo(f"  se3 collab --launch-manager review")
    typer.echo(f"\nOr use daemon mode for automatic execution:")
    typer.echo(f"  se3 collab --daemon \"{objective}\"")
    typer.echo(f"{'=' * 60}\n")


@app.callback()
def collab(
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
):
    """Manage git-worktree based multi-agent collaboration.

    Independent Entry Mode:
    - Use --daemon for automatic execution (orchestrator manages everything)
    - Use --manual to generate plan and execute manually
    """
    root = Path(project_root) if project_root else find_project_root()

    if status:
        print_status(root)
        raise typer.Exit(0)

    if abort:
        abort_session(root)
        raise typer.Exit(0)

    if launch_manager_flag:
        # Internal: launch manager process
        context = objective if objective else ""
        proc = launch_manager(root, event_type, context)
        stdout, _ = proc.communicate(timeout=900)  # 15 min timeout
        if proc.returncode == 0 and stdout:
            print(stdout.decode() if isinstance(stdout, bytes) else stdout)
        raise typer.Exit(proc.returncode)

    if launch_worker_flag:
        # Internal: launch worker process with activity monitoring
        if not task_id:
            typer.echo("Error: --task required for --launch-worker")
            raise typer.Exit(1)
        result = launch_worker(root, task_id)
        raise typer.Exit(result.returncode)

    if daemon:
        if not resume and not objective:
            typer.echo("Error: provide an objective or use --resume")
            raise typer.Exit(1)
        start_daemon(root, objective or "", resume)
        raise typer.Exit(0)

    if manual:
        if not objective:
            typer.echo("Error: provide an objective")
            raise typer.Exit(1)
        run_manual_mode(root, objective)
        raise typer.Exit(0)

    # Default: run orchestrator directly (legacy mode, for testing)
    if not resume and not objective:
        typer.echo("Error: provide an objective, use --daemon, --manual, or --resume")
        raise typer.Exit(1)

    script = root / "scripts" / "collab-orchestrator.sh"
    if not script.exists():
        typer.echo(f"Error: orchestrator script not found at {script}")
        raise typer.Exit(1)

    cmd = ["bash", str(script)]
    if resume:
        cmd.append("--resume")
    if mock:
        cmd.append("--mock")
    if objective:
        cmd.append(objective)

    env = {**dict(os.environ), "PROJECT_ROOT": str(root)}

    try:
        result = subprocess.run(cmd, env=env, cwd=root)
        raise typer.Exit(result.returncode)
    except KeyboardInterrupt:
        typer.echo("\nInterrupted. Use 'se3 collab --abort' to cleanup.")
        raise typer.Exit(130)
