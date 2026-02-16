"""SE3 External Controller Daemon.

Manages Claude sessions, auto-commit, and multi-agent collaboration.
Runs as a background daemon process.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer()

# Controller state directory
CONTROLLER_DIR = Path.home() / ".se3" / "controller"
PID_FILE = CONTROLLER_DIR / "daemon.pid"
SOCKET_PATH = CONTROLLER_DIR / "daemon.sock"


class AutoCommitWatcher:
    """Watch for file changes and trigger commits based on heuristics."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.last_modified = {}
        self.silence_timeout = 300  # 5 minutes
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start watching in background thread."""
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop watching."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _watch_loop(self):
        """Main watch loop."""
        while not self._stop_event.is_set():
            try:
                self._check_for_commit_triggers()
            except Exception as e:
                print(f"[watcher] Error: {e}", file=sys.stderr)
            time.sleep(10)  # Check every 10 seconds

    def _check_for_commit_triggers(self):
        """Check if any commit triggers are met."""
        # Check for modified files
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if not result.stdout.strip():
            return  # No changes

        # Check silence timeout
        if self._is_silence_period_met():
            self._trigger_commit("silence_timeout")

    def _is_silence_period_met(self) -> bool:
        """Check if enough time has passed since last modification."""
        # Get most recent modification time of any tracked file
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if not result.stdout.strip():
            return False

        # Get the most recent mtime of modified files
        most_recent = 0
        for line in result.stdout.strip().split("\n"):
            if len(line) < 3:
                continue
            filepath = line[3:].strip()
            full_path = self.project_root / filepath
            if full_path.exists():
                mtime = full_path.stat().st_mtime
                most_recent = max(most_recent, mtime)

        if most_recent == 0:
            return False

        silence_duration = time.time() - most_recent
        return silence_duration >= self.silence_timeout

    def _trigger_commit(self, reason: str):
        """Trigger se3 commit."""
        print(f"[watcher] Triggering commit: {reason}")

        # Generate commit message based on changes
        message = self._generate_commit_message()

        result = subprocess.run(
            ["se3", "commit", "-m", message],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            print(f"[watcher] Committed: {message}")
        else:
            print(f"[watcher] Commit failed: {result.stderr}", file=sys.stderr)

    def _generate_commit_message(self) -> str:
        """Generate commit message from changes."""
        result = subprocess.run(
            ["git", "diff", "--stat", "--cached"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        # Simple message generation
        files_changed = len(result.stdout.strip().split("\n")) - 1
        return f"Auto-commit: {files_changed} files changed (silence timeout)"


class SessionController:
    """Manage a single Claude session."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.claude_process: Optional[subprocess.Popen] = None
        self.watcher = AutoCommitWatcher(project_root)
        self.session_id: Optional[str] = None

    def start_interactive(self, objective: Optional[str] = None):
        """Start an interactive Claude session."""
        self.session_id = f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        # Build initial prompt
        prompt = self._build_prompt(objective)

        # Start Claude in interactive mode
        cmd = ["claude"]
        if prompt:
            cmd.extend(["-p", prompt])

        print(f"[controller] Starting session: {self.session_id}")

        # Start auto-commit watcher
        self.watcher.start()

        try:
            # Run Claude interactively
            self.claude_process = subprocess.Popen(
                cmd,
                cwd=self.project_root,
            )
            self.claude_process.wait()
        except KeyboardInterrupt:
            print("\n[controller] Interrupted")
        finally:
            self.stop()

    def _build_prompt(self, objective: Optional[str]) -> str:
        """Build initial prompt for Claude."""
        base_prompt = """You are Claude Code in SE3 managed session.

External Controller is managing this session. Key behaviors:
1. Use MCP tools to communicate with controller when needed
2. Focus on the given objective
3. Controller handles auto-commit based on file changes
4. Type '/commit' to request immediate commit
5. Type '/pause' to pause session (controller will commit)
"""

        if objective:
            base_prompt += f"\n\nObjective: {objective}"

        return base_prompt

    def stop(self, force_commit: bool = True):
        """Stop the session."""
        print("[controller] Stopping session...")

        # Stop watcher
        self.watcher.stop()

        # Commit if requested
        if force_commit:
            self._force_commit()

        # Terminate Claude if still running
        if self.claude_process and self.claude_process.poll() is None:
            self.claude_process.terminate()
            try:
                self.claude_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.claude_process.kill()

        print("[controller] Session stopped")

    def _force_commit(self):
        """Force a commit before stopping."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            print("[controller] Committing pending changes...")
            subprocess.run(
                ["se3", "commit", "-m", f"Session end: {self.session_id}"],
                cwd=self.project_root,
            )


class CollaborationController:
    """Manage multi-agent collaboration (replaces collab --daemon)."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.active_workers: dict[str, subprocess.Popen] = {}
        self._stop_event = threading.Event()

    def start(self, objective: str, max_workers: int = 3):
        """Start collaboration session."""
        print(f"[collab] Starting collaboration: {objective}")

        # Create collab structure
        collab_dir = self.project_root / ".collab"
        collab_dir.mkdir(exist_ok=True)

        # Save session config
        config = {
            "session_id": f"collab-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            "objective": objective,
            "max_workers": max_workers,
            "status": "active",
        }
        (collab_dir / "config.json").write_text(json.dumps(config, indent=2))

        # Spawn manager to create plan
        self._spawn_manager("plan", objective)

        # Start event loop
        self._event_loop()

    def _spawn_manager(self, event_type: str, context: str):
        """Spawn manager agent as separate Claude process."""
        print(f"[collab] Spawning manager for {event_type}")

        cmd = [
            "claude",
            "-p", f"You are SE3 Collaboration Manager. Event: {event_type}\nContext: {context}",
            "--output-format", "json",
            "--max-turns", "30",
        ]

        proc = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for completion and process result
        stdout, stderr = proc.communicate(timeout=900)  # 15 min timeout

        if proc.returncode == 0:
            try:
                result = json.loads(stdout.decode())
                self._process_manager_result(result)
            except json.JSONDecodeError:
                print(f"[collab] Manager returned invalid JSON: {stdout.decode()[:200]}")

    def _spawn_worker(self, task_id: str, prompt: str):
        """Spawn worker agent as separate Claude process."""
        print(f"[collab] Spawning worker for {task_id}")

        # Create worktree
        worktree = self.project_root / ".worktrees" / task_id
        branch = f"collab/{task_id}"

        if not worktree.exists():
            subprocess.run(
                ["git", "worktree", "add", str(worktree), "-b", branch, "master"],
                cwd=self.project_root,
                check=True,
            )

        cmd = [
            "claude",
            "-p", prompt,
            "--max-turns", "50",
        ]

        proc = subprocess.Popen(
            cmd,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.active_workers[task_id] = proc

        # Start monitoring thread
        threading.Thread(
            target=self._monitor_worker,
            args=(task_id, proc),
            daemon=True,
        ).start()

    def _monitor_worker(self, task_id: str, proc: subprocess.Popen):
        """Monitor worker process completion."""
        stdout, stderr = proc.communicate()

        print(f"[collab] Worker {task_id} exited with code {proc.returncode}")

        # Notify manager
        self._spawn_manager(
            "worker_complete",
            f"Task {task_id} completed with exit code {proc.returncode}"
        )

    def _process_manager_result(self, result: dict):
        """Process manager decision."""
        action = result.get("action")

        if action == "plan":
            # Create tasks
            for task in result.get("tasks", []):
                task_id = task["id"]
                task_file = self.project_root / ".collab" / "tasks" / f"{task_id}.json"
                task_file.parent.mkdir(exist_ok=True)
                task_file.write_text(json.dumps(task, indent=2))
                print(f"[collab] Created task: {task_id}")

        elif action == "spawn_worker":
            task_id = result.get("task_id")
            prompt = result.get("prompt")
            if task_id and prompt:
                self._spawn_worker(task_id, prompt)

        elif action == "complete":
            print("[collab] Collaboration complete")
            self._stop_event.set()

    def _event_loop(self):
        """Main event loop."""
        while not self._stop_event.is_set():
            # Check for pending tasks
            tasks_dir = self.project_root / ".collab" / "tasks"
            if tasks_dir.exists():
                for task_file in tasks_dir.glob("task-*.json"):
                    task = json.loads(task_file.read_text())
                    if task.get("status") == "pending":
                        # Update status and spawn
                        task["status"] = "in_progress"
                        task_file.write_text(json.dumps(task, indent=2))
                        self._spawn_worker(task["id"], task["prompt"])

            time.sleep(5)


# CLI Commands

@app.command()
def daemon(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
    stop: bool = typer.Option(False, "--stop", help="Stop running daemon"),
    status: bool = typer.Option(False, "--status", help="Show daemon status"),
):
    """Start/stop the SE3 controller daemon."""
    root = Path(project_root) if project_root else Path.cwd()

    if status:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(pid, 0)
                print(f"Daemon running (PID {pid})")
            except ProcessLookupError:
                print("Daemon not running (stale PID file)")
        else:
            print("Daemon not running")
        return

    if stop:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                PID_FILE.unlink()
                print(f"Daemon stopped (PID {pid})")
            except ProcessLookupError:
                PID_FILE.unlink()
                print("Daemon not running")
        return

    # Start daemon
    CONTROLLER_DIR.mkdir(parents=True, exist_ok=True)

    if PID_FILE.exists():
        old_pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(old_pid, 0)
            print(f"Daemon already running (PID {old_pid})")
            return
        except ProcessLookupError:
            pass

    # Daemonize
    pid = os.fork()
    if pid > 0:
        PID_FILE.write_text(str(pid))
        print(f"Daemon started (PID {pid})")
        return

    # Child process
    os.setsid()
    sys.stdout = open(CONTROLLER_DIR / "daemon.log", "a")
    sys.stderr = sys.stdout

    # Run daemon loop
    while True:
        time.sleep(60)


@app.command()
def session(
    objective: Optional[str] = typer.Argument(None),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
):
    """Start an interactive Claude session managed by external controller."""
    root = Path(project_root) if project_root else Path.cwd()

    controller = SessionController(root)

    def signal_handler(signum, frame):
        print("\n[signal] Received termination signal")
        controller.stop(force_commit=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    controller.start_interactive(objective)


@app.command()
def collab(
    objective: str = typer.Argument(...),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
    max_workers: int = typer.Option(3, "--max-workers", "-w"),
):
    """Start multi-agent collaboration managed by external controller."""
    root = Path(project_root) if project_root else Path.cwd()

    controller = CollaborationController(root)

    def signal_handler(signum, frame):
        print("\n[signal] Stopping collaboration...")
        controller._stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    controller.start(objective, max_workers)


if __name__ == "__main__":
    app()
