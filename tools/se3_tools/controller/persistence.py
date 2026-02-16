"""Session persistence and recovery for SE3 Controller.

Handles:
- State saving to disk
- Process crash detection
- Automatic recovery of sessions
- Graceful shutdown handling
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

CONTROLLER_DIR = Path.home() / ".se3" / "controller"
STATE_FILE = CONTROLLER_DIR / "state.json"
SESSIONS_DIR = CONTROLLER_DIR / "sessions"


class SessionPersistence:
    """Manage session persistence to disk."""

    def __init__(self):
        CONTROLLER_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(exist_ok=True)

    def save_session(self, session_id: str, data: dict):
        """Save session state to disk."""
        session_file = SESSIONS_DIR / f"{session_id}.json"

        # Add metadata
        data["_saved_at"] = datetime.now().isoformat()
        data["_version"] = "1.0.0"

        session_file.write_text(json.dumps(data, indent=2))

    def load_session(self, session_id: str) -> Optional[dict]:
        """Load session state from disk."""
        session_file = SESSIONS_DIR / f"{session_id}.json"

        if not session_file.exists():
            return None

        try:
            return json.loads(session_file.read_text())
        except (json.JSONDecodeError, IOError):
            return None

    def delete_session(self, session_id: str):
        """Delete session state from disk."""
        session_file = SESSIONS_DIR / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()

    def list_sessions(self) -> list[str]:
        """List all saved session IDs."""
        sessions = []
        for f in SESSIONS_DIR.glob("*.json"):
            sessions.append(f.stem)
        return sessions

    def save_global_state(self, state: dict):
        """Save global controller state."""
        state["_saved_at"] = datetime.now().isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def load_global_state(self) -> dict:
        """Load global controller state."""
        if not STATE_FILE.exists():
            return {"version": "1.0.0", "sessions": {}}

        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return {"version": "1.0.0", "sessions": {}}


class ProcessMonitor:
    """Monitor process health and detect crashes."""

    @staticmethod
    def is_process_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def get_process_info(pid: int) -> Optional[dict]:
        """Get information about a process."""
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid,ppid,cmd", "--no-headers"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(None, 2)
                return {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "cmd": parts[2],
                }
        except Exception:
            pass
        return None


class RecoveryManager:
    """Manage recovery from crashes and unexpected shutdowns."""

    def __init__(self):
        self.persistence = SessionPersistence()
        self.monitor = ProcessMonitor()

    def check_and_recover(self) -> list[dict]:
        """Check for sessions needing recovery and recover them.

        Returns:
            List of recovered session IDs and their status.
        """
        recovered = []
        global_state = self.persistence.load_global_state()

        for session_id, session_data in global_state.get("sessions", {}).items():
            pid = session_data.get("pid")

            if pid and not self.monitor.is_process_alive(pid):
                # Process died, attempt recovery
                recovery_result = self._recover_session(session_id, session_data)
                recovered.append({
                    "session_id": session_id,
                    "status": recovery_result,
                })

        return recovered

    def _recover_session(self, session_id: str, session_data: dict) -> str:
        """Attempt to recover a crashed session.

        Returns:
            Recovery status: "restarted", "committed", "failed"
        """
        print(f"[recovery] Recovering session: {session_id}")

        # Check for pending changes
        project_root = Path(session_data.get("project_root", "."))

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            # There are pending changes - try to commit them
            print(f"[recovery] Committing pending changes...")
            commit_result = subprocess.run(
                ["se3", "commit", "-m", f"Recovery commit for {session_id}"],
                cwd=project_root,
                capture_output=True,
                text=True,
            )

            if commit_result.returncode == 0:
                print(f"[recovery] Committed successfully")
            else:
                print(f"[recovery] Commit failed: {commit_result.stderr}")

        # Update session status
        session_data["status"] = "recovered"
        session_data["recovered_at"] = datetime.now().isoformat()
        self.persistence.save_session(session_id, session_data)

        return "committed"


class GracefulShutdown:
    """Handle graceful shutdown on SIGTERM/SIGINT."""

    def __init__(self, cleanup_callback=None):
        self.cleanup_callback = cleanup_callback
        self._shutdown_requested = False
        self._handlers_installed = False

    def install_handlers(self):
        """Install signal handlers for graceful shutdown."""
        if self._handlers_installed:
            return

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._handlers_installed = True

    def _handle_signal(self, signum, frame):
        """Handle shutdown signal."""
        signame = signal.Signals(signum).name
        print(f"\n[shutdown] Received {signame}, starting graceful shutdown...")
        self._shutdown_requested = True

        if self.cleanup_callback:
            try:
                self.cleanup_callback()
            except Exception as e:
                print(f"[shutdown] Cleanup error: {e}", file=sys.stderr)

        sys.exit(0)

    def is_shutdown_requested(self) -> bool:
        """Check if shutdown has been requested."""
        return self._shutdown_requested


class Watchdog:
    """Watchdog to monitor and restart critical processes."""

    def __init__(self, max_restarts: int = 3):
        self.max_restarts = max_restarts
        self.restart_count = 0
        self._stop_event = False

    def start_monitoring(self, pid: int, restart_callback):
        """Start monitoring a process and restart if it dies.

        Args:
            pid: Process ID to monitor
            restart_callback: Function to call to restart the process
        """
        print(f"[watchdog] Starting monitoring for PID {pid}")

        while not self._stop_event:
            time.sleep(5)

            if not ProcessMonitor.is_process_alive(pid):
                self.restart_count += 1

                if self.restart_count > self.max_restarts:
                    print(f"[watchdog] Max restarts ({self.max_restarts}) exceeded")
                    break

                print(f"[watchdog] Process died, restart #{self.restart_count}")

                try:
                    new_pid = restart_callback()
                    pid = new_pid
                    print(f"[watchdog] Restarted as PID {pid}")
                except Exception as e:
                    print(f"[watchdog] Restart failed: {e}")

    def stop(self):
        """Stop watchdog."""
        self._stop_event = True


def save_checkpoint(project_root: Path, session_id: str, state: dict):
    """Save a checkpoint of current state.

    Can be used by Claude processes to save progress periodically.
    """
    checkpoint_dir = project_root / ".claude" / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_file = checkpoint_dir / f"{session_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    checkpoint = {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        "state": state,
    }

    checkpoint_file.write_text(json.dumps(checkpoint, indent=2))
    return checkpoint_file


def load_latest_checkpoint(project_root: Path, session_id: str) -> Optional[dict]:
    """Load the latest checkpoint for a session."""
    checkpoint_dir = project_root / ".claude" / "checkpoints"

    if not checkpoint_dir.exists():
        return None

    checkpoints = sorted(checkpoint_dir.glob(f"{session_id}-*.json"), reverse=True)

    if not checkpoints:
        return None

    try:
        return json.loads(checkpoints[0].read_text())
    except (json.JSONDecodeError, IOError):
        return None
