"""HTTP API Server for SE3 External Controller.

Provides REST API for session management, auto-commit control, and collaboration.
Supports both Unix socket (local) and TCP (remote) modes.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException, BackgroundTasks
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    # Mock classes for type checking
    class BaseModel:
        pass
    class FastAPI:
        pass

# Controller paths
CONTROLLER_DIR = Path.home() / ".se3" / "controller"
STATE_FILE = CONTROLLER_DIR / "state.json"
PID_FILE = CONTROLLER_DIR / "daemon.pid"
SOCKET_PATH = CONTROLLER_DIR / "daemon.sock"

# Global state (shared across requests)
controller_state = {
    "running": False,
    "sessions": {},
    "active_workers": {},
    "pending_commits": [],
}


# Pydantic models for API
class SessionStartRequest(BaseModel):
    objective: Optional[str] = None
    mode: str = "interactive"  # interactive, worker, manager
    project_root: Optional[str] = None


class SessionStopRequest(BaseModel):
    session_id: str
    force: bool = False
    skip_commit: bool = False


class CommitTriggerRequest(BaseModel):
    reason: str
    message: Optional[str] = None
    session_id: Optional[str] = None


class CommitConfigUpdate(BaseModel):
    silence_timeout: Optional[int] = None
    commit_on_test_pass: Optional[bool] = None
    commit_on_session_end: Optional[bool] = None


class CollabStartRequest(BaseModel):
    objective: str
    max_workers: int = 3
    project_root: Optional[str] = None


class SpawnRequest(BaseModel):
    role: str  # worker, manager
    task_id: Optional[str] = None
    prompt: str
    project_root: Optional[str] = None


class EventProcessRequest(BaseModel):
    event_file: str


def load_state() -> dict:
    """Load controller state from file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "version": "1.0.0",
        "sessions": {},
        "collab_sessions": {},
    }


def save_state(state: dict):
    """Save controller state to file."""
    CONTROLLER_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def find_project_root(cwd: Optional[str] = None) -> Path:
    """Find project root by looking for .claude/ directory."""
    current = Path(cwd) if cwd else Path.cwd()
    while current != current.parent:
        if (current / ".claude").is_dir():
            return current
        current = current.parent
    return Path.cwd()


class AutoCommitManager:
    """Manage auto-commit configuration and triggers."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.config = self._load_config()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _load_config(self) -> dict:
        """Load auto-commit config."""
        config_file = self.project_root / ".claude" / "auto-commit.json"
        if config_file.exists():
            try:
                return json.loads(config_file.read_text())
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "silence_timeout": 300,
            "commit_on_test_pass": True,
            "commit_on_session_end": True,
            "enabled": True,
        }

    def _save_config(self):
        """Save auto-commit config."""
        config_file = self.project_root / ".claude" / "auto-commit.json"
        config_file.write_text(json.dumps(self.config, indent=2))

    def update_config(self, **kwargs):
        """Update configuration."""
        self.config.update(kwargs)
        self._save_config()

    def start_watching(self):
        """Start file watching thread."""
        if not self.config.get("enabled", True):
            return

        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop_watching(self):
        """Stop file watching."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _watch_loop(self):
        """Main watch loop."""
        last_check = time.time()
        while not self._stop_event.is_set():
            try:
                if time.time() - last_check >= 10:  # Check every 10s
                    self._check_commit_triggers()
                    last_check = time.time()
            except Exception as e:
                print(f"[watcher] Error: {e}", file=sys.stderr)
            time.sleep(1)

    def _check_commit_triggers(self):
        """Check if commit should be triggered."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if not result.stdout.strip():
            return

        # Check silence timeout
        if self._is_silence_period_met():
            self._trigger_commit("silence_timeout")

    def _is_silence_period_met(self) -> bool:
        """Check if files have been unmodified for the timeout period."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if not result.stdout.strip():
            return False

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
        return silence_duration >= self.config.get("silence_timeout", 300)

    def _trigger_commit(self, reason: str):
        """Trigger se3 commit."""
        print(f"[watcher] Auto-commit triggered: {reason}")

        message = f"Auto-commit: {reason}"

        result = subprocess.run(
            ["se3", "commit", "-m", message],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            print(f"[watcher] Committed successfully")
            controller_state["pending_commits"] = [
                c for c in controller_state["pending_commits"]
                if c.get("project_root") != str(self.project_root)
            ]
        else:
            print(f"[watcher] Commit failed: {result.stderr}", file=sys.stderr)


class SessionManager:
    """Manage Claude sessions."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self._load_sessions()

    def _load_sessions(self):
        """Load sessions from state file."""
        state = load_state()
        self.sessions = state.get("sessions", {})

    def _save_sessions(self):
        """Save sessions to state file."""
        state = load_state()
        state["sessions"] = self.sessions
        save_state(state)

    def create_session(self, objective: Optional[str], mode: str, project_root: Path) -> str:
        """Create a new session."""
        session_id = f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        session = {
            "id": session_id,
            "objective": objective,
            "mode": mode,
            "project_root": str(project_root),
            "status": "starting",
            "created_at": datetime.now().isoformat(),
            "pid": None,
        }

        self.sessions[session_id] = session
        self._save_sessions()

        return session_id

    def start_session_process(self, session_id: str) -> int:
        """Start Claude process for session."""
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        project_root = Path(session["project_root"])

        # Build prompt
        prompt = self._build_prompt(session)

        cmd = ["claude"]
        if prompt:
            cmd.extend(["-p", prompt])

        env = {
            **os.environ,
            "SE3_SESSION_ID": session_id,
            "SE3_AGENT_ROLE": session["mode"],
            "SE3_PROJECT_ROOT": str(project_root),
        }

        proc = subprocess.Popen(
            cmd,
            cwd=project_root,
            env=env,
        )

        session["pid"] = proc.pid
        session["status"] = "running"
        self._save_sessions()

        return proc.pid

    def _build_prompt(self, session: dict) -> str:
        """Build initial prompt for session."""
        base = """You are Claude Code in an SE3 managed session.

External Controller is managing this session.
Available MCP tools:
- report_task_complete: Report task completion
- request_human_input: Ask human for input
- trigger_commit: Request immediate commit
- spawn_worker_task: Spawn a worker task (for managers)
- report_status: Report current status
- request_pause: Request session pause

Type '/commit' in chat to request immediate commit.
Type '/pause' to pause session (controller will commit).
"""

        if session.get("objective"):
            base += f"\n\nObjective: {session['objective']}"

        return base

    def stop_session(self, session_id: str, force: bool = False, skip_commit: bool = False):
        """Stop a session."""
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        pid = session.get("pid")
        project_root = Path(session["project_root"])

        if not skip_commit:
            self._commit_pending_changes(project_root, session_id)

        if pid:
            try:
                os.kill(pid, signal.SIGTERM if not force else signal.SIGKILL)
            except ProcessLookupError:
                pass

        session["status"] = "stopped"
        session["stopped_at"] = datetime.now().isoformat()
        self._save_sessions()

    def _commit_pending_changes(self, project_root: Path, session_id: str):
        """Commit pending changes."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            subprocess.run(
                ["se3", "commit", "-m", f"Session end: {session_id}"],
                cwd=project_root,
            )

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get session info."""
        return self.sessions.get(session_id)

    def list_sessions(self) -> list[dict]:
        """List all sessions."""
        return list(self.sessions.values())


class CollaborationManager:
    """Manage multi-agent collaboration."""

    def __init__(self):
        self.active_workers: dict[str, subprocess.Popen] = {}
        self._load_state()

    def _load_state(self):
        """Load collaboration state."""
        state = load_state()
        # Don't restore processes, just clean up stale state

    def start_collaboration(self, objective: str, max_workers: int, project_root: Path) -> str:
        """Start a collaboration session."""
        collab_id = f"collab-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        collab_dir = project_root / ".collab"
        collab_dir.mkdir(exist_ok=True)

        config = {
            "collab_id": collab_id,
            "objective": objective,
            "max_workers": max_workers,
            "project_root": str(project_root),
            "status": "active",
            "created_at": datetime.now().isoformat(),
        }

        (collab_dir / "config.json").write_text(json.dumps(config, indent=2))

        # Spawn manager to create plan
        self._spawn_manager("plan", objective, project_root, collab_id)

        return collab_id

    def _spawn_manager(self, event_type: str, context: str, project_root: Path, collab_id: str):
        """Spawn manager agent."""
        prompt = f"""You are SE3 Collaboration Manager.
Event: {event_type}
Context: {context}

Respond with JSON matching the action schema."""

        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", "30",
        ]

        env = {
            **os.environ,
            "SE3_AGENT_ROLE": "manager",
            "SE3_PROJECT_ROOT": str(project_root),
            "SE3_COLLAB_ID": collab_id,
        }

        def run_manager():
            proc = subprocess.Popen(
                cmd,
                cwd=project_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(timeout=900)

            if proc.returncode == 0:
                try:
                    result = json.loads(stdout.decode())
                    self._process_manager_result(result, project_root, collab_id)
                except json.JSONDecodeError:
                    print(f"[collab] Manager returned invalid JSON", file=sys.stderr)

        threading.Thread(target=run_manager, daemon=True).start()

    def _spawn_worker(self, task_id: str, prompt: str, project_root: Path, collab_id: str):
        """Spawn worker agent."""
        worktree = project_root / ".worktrees" / task_id
        branch = f"collab/{task_id}"

        if not worktree.exists():
            subprocess.run(
                ["git", "worktree", "add", str(worktree), "-b", branch, "master"],
                cwd=project_root,
                check=True,
            )

        cmd = [
            "claude",
            "-p", prompt,
            "--max-turns", "50",
        ]

        env = {
            **os.environ,
            "SE3_AGENT_ROLE": "worker",
            "SE3_PROJECT_ROOT": str(project_root),
            "SE3_COLLAB_ID": collab_id,
            "SE3_TASK_ID": task_id,
        }

        proc = subprocess.Popen(
            cmd,
            cwd=worktree,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.active_workers[task_id] = proc

        def monitor():
            stdout, stderr = proc.communicate()
            print(f"[collab] Worker {task_id} exited: {proc.returncode}")
            self._spawn_manager(
                "worker_complete",
                f"Task {task_id} completed with exit code {proc.returncode}",
                project_root,
                collab_id,
            )

        threading.Thread(target=monitor, daemon=True).start()

    def _process_manager_result(self, result: dict, project_root: Path, collab_id: str):
        """Process manager decision."""
        action = result.get("action")

        if action == "plan":
            for task in result.get("tasks", []):
                task_id = task["id"]
                task_file = project_root / ".collab" / "tasks" / f"{task_id}.json"
                task_file.parent.mkdir(exist_ok=True)
                task_file.write_text(json.dumps(task, indent=2))

        elif action == "spawn_worker":
            task_id = result.get("task_id")
            prompt = result.get("prompt")
            if task_id and prompt:
                self._spawn_worker(task_id, prompt, project_root, collab_id)

        elif action == "complete":
            print(f"[collab] Collaboration {collab_id} complete")


# Create FastAPI app
if HAS_FASTAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Manage application lifespan."""
        # Startup
        controller_state["running"] = True
        print("[api] Controller API server starting...")
        yield
        # Shutdown
        controller_state["running"] = False
        print("[api] Controller API server shutting down...")

    app = FastAPI(
        title="SE3 Controller API",
        description="External Controller for SE3 Framework",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Initialize managers
    session_manager = SessionManager()
    collab_manager = CollaborationManager()

    @app.post("/session/start")
    async def session_start(request: SessionStartRequest):
        """Start a new session."""
        try:
            project_root = find_project_root(request.project_root)
            session_id = session_manager.create_session(
                request.objective,
                request.mode,
                project_root,
            )
            pid = session_manager.start_session_process(session_id)

            return {
                "session_id": session_id,
                "pid": pid,
                "status": "started",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/session/stop")
    async def session_stop(request: SessionStopRequest):
        """Stop a session."""
        try:
            session_manager.stop_session(
                request.session_id,
                request.force,
                request.skip_commit,
            )
            return {"status": "stopped", "session_id": request.session_id}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/session/status")
    async def session_status(session_id: Optional[str] = None):
        """Get session status."""
        if session_id:
            session = session_manager.get_session(session_id)
            if not session:
                raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
            return session
        else:
            return {"sessions": session_manager.list_sessions()}

    @app.post("/commit/trigger")
    async def commit_trigger(request: CommitTriggerRequest):
        """Trigger immediate commit."""
        try:
            project_root = find_project_root()
            message = request.message or f"Manual commit: {request.reason}"

            result = subprocess.run(
                ["se3", "commit", "-m", message],
                cwd=project_root,
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                return {"status": "committed", "message": message}
            else:
                raise HTTPException(status_code=500, detail=result.stderr)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/commit/config")
    async def commit_config_get():
        """Get auto-commit configuration."""
        project_root = find_project_root()
        manager = AutoCommitManager(project_root)
        return manager.config

    @app.put("/commit/config")
    async def commit_config_update(request: CommitConfigUpdate):
        """Update auto-commit configuration."""
        project_root = find_project_root()
        manager = AutoCommitManager(project_root)

        updates = request.dict(exclude_unset=True)
        manager.update_config(**updates)

        return {"status": "updated", "config": manager.config}

    @app.post("/collab/start")
    async def collab_start(request: CollabStartRequest):
        """Start collaboration session."""
        try:
            project_root = find_project_root(request.project_root)
            collab_id = collab_manager.start_collaboration(
                request.objective,
                request.max_workers,
                project_root,
            )
            return {"collab_id": collab_id, "status": "started"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/collab/spawn")
    async def collab_spawn(request: SpawnRequest):
        """Spawn worker/manager process."""
        try:
            project_root = find_project_root(request.project_root)

            if request.role == "worker":
                if not request.task_id:
                    raise HTTPException(status_code=400, detail="task_id required for worker")
                collab_manager._spawn_worker(
                    request.task_id,
                    request.prompt,
                    project_root,
                    "manual",
                )
            elif request.role == "manager":
                collab_manager._spawn_manager(
                    "manual",
                    request.prompt,
                    project_root,
                    "manual",
                )
            else:
                raise HTTPException(status_code=400, detail=f"Invalid role: {request.role}")

            return {"status": "spawned", "role": request.role}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {
            "status": "healthy",
            "running": controller_state["running"],
            "sessions": len(session_manager.sessions),
            "active_workers": len(collab_manager.active_workers),
        }


def run_server(host: str = "127.0.0.1", port: int = 8765, socket_path: Optional[str] = None):
    """Run the API server."""
    if not HAS_FASTAPI:
        print("Error: FastAPI not installed. Run: pip install fastapi uvicorn", file=sys.stderr)
        sys.exit(1)

    import uvicorn

    if socket_path:
        # Unix socket mode
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

        uvicorn.run(app, uds=str(socket_path))
    else:
        # TCP mode
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
