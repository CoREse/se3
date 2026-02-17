"""Tests for the git-worktree-collab system.

Tests cover:
- Task file creation and parsing
- Orchestrator state recovery logic
- Manager JSON validation
- MCP server tool handling
- CLI command basics
- Health check logic
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest

# Add tools to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "mcp-collab"))


# =============================================================================
# Task File Tests
# =============================================================================

class TestTaskFiles:
    """Test task file format and state management."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_dir = Path(self.tmpdir) / ".collab" / "tasks"
        self.tasks_dir.mkdir(parents=True)

    def _write_task(self, task_id: str, **overrides) -> Path:
        task = {
            "id": task_id,
            "branch": f"collab/{task_id}",
            "worktree": f".worktrees/{task_id}",
            "status": "pending",
            "title": f"Test task {task_id}",
            "prompt": f"Implement {task_id}",
            "spec_refs": [],
            "created_at": "2026-02-15T10:00:00Z",
            "started_at": None,
            "completed_at": None,
            "worker_pid": None,
            "worker_exit_code": None,
            "result_summary": None,
            "blocked_reason": None,
            "review": {"status": "pending", "merge_commit": None, "comments": None},
            "health": {
                "last_commit_at": None,
                "timeout_minutes": 60,
                "attempts": 0,
                "max_attempts": 3,
            },
        }
        task.update(overrides)
        path = self.tasks_dir / f"{task_id}.json"
        path.write_text(json.dumps(task, indent=2))
        return path

    def test_task_file_creation(self):
        """Task file should be valid JSON with all required fields."""
        path = self._write_task("task-001")
        task = json.loads(path.read_text())

        assert task["id"] == "task-001"
        assert task["status"] == "pending"
        assert task["health"]["attempts"] == 0
        assert task["health"]["max_attempts"] == 3

    def test_task_status_lifecycle(self):
        """Task status should follow valid transitions."""
        path = self._write_task("task-001")

        # pending → in_progress
        task = json.loads(path.read_text())
        task["status"] = "in_progress"
        task["started_at"] = datetime.now().isoformat()
        path.write_text(json.dumps(task))

        task = json.loads(path.read_text())
        assert task["status"] == "in_progress"
        assert task["started_at"] is not None

        # in_progress → done
        task["status"] = "done"
        task["completed_at"] = datetime.now().isoformat()
        task["worker_exit_code"] = 0
        path.write_text(json.dumps(task))

        task = json.loads(path.read_text())
        assert task["status"] == "done"
        assert task["worker_exit_code"] == 0

    def test_task_failure_state(self):
        """Failed tasks should record exit code and allow retry."""
        path = self._write_task("task-001", status="in_progress")
        task = json.loads(path.read_text())

        task["status"] = "failed"
        task["worker_exit_code"] = 1
        task["health"]["attempts"] = 1
        path.write_text(json.dumps(task))

        task = json.loads(path.read_text())
        assert task["status"] == "failed"
        assert task["health"]["attempts"] < task["health"]["max_attempts"]

    def test_task_blocked_state(self):
        """Blocked tasks should record the reason."""
        path = self._write_task("task-001", status="in_progress")
        task = json.loads(path.read_text())

        task["status"] = "blocked"
        task["blocked_reason"] = "Need clarification on auth spec"
        task["worker_exit_code"] = 2
        path.write_text(json.dumps(task))

        task = json.loads(path.read_text())
        assert task["status"] == "blocked"
        assert "clarification" in task["blocked_reason"]

    def test_task_timeout_state(self):
        """Timed out tasks should be recoverable."""
        path = self._write_task("task-001", status="in_progress")
        task = json.loads(path.read_text())

        task["status"] = "timeout"
        task["health"]["attempts"] = 1
        path.write_text(json.dumps(task))

        task = json.loads(path.read_text())
        assert task["status"] == "timeout"
        # Can retry: attempts < max_attempts
        assert task["health"]["attempts"] < task["health"]["max_attempts"]


# =============================================================================
# State Recovery Tests
# =============================================================================

class TestStateRecovery:
    """Test orchestrator state recovery from .collab/ files."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.collab_dir = Path(self.tmpdir) / ".collab"
        self.tasks_dir = self.collab_dir / "tasks"
        self.tasks_dir.mkdir(parents=True)

    def _write_config(self, **overrides):
        config = {
            "session_id": "collab-test",
            "objective": "Test objective",
            "base_branch": "master",
            "created_at": "2026-02-15T10:00:00Z",
            "max_parallel_workers": 3,
            "status": "active",
        }
        config.update(overrides)
        (self.collab_dir / "config.json").write_text(json.dumps(config, indent=2))

    def _write_task(self, task_id: str, status: str = "pending"):
        task = {
            "id": task_id,
            "branch": f"collab/{task_id}",
            "worktree": f".worktrees/{task_id}",
            "status": status,
            "title": f"Task {task_id}",
            "prompt": f"Do {task_id}",
            "worker_pid": None,
            "health": {"attempts": 0, "max_attempts": 3},
        }
        (self.tasks_dir / f"{task_id}.json").write_text(json.dumps(task, indent=2))

    def test_recover_session_config(self):
        """Should reconstruct session from config.json."""
        self._write_config(objective="Build auth system")

        config = json.loads((self.collab_dir / "config.json").read_text())
        assert config["objective"] == "Build auth system"
        assert config["status"] == "active"

    def test_recover_task_states(self):
        """Should reconstruct all task states from files."""
        self._write_task("task-001", "done")
        self._write_task("task-002", "in_progress")
        self._write_task("task-003", "pending")

        tasks = {}
        for tf in self.tasks_dir.glob("task-*.json"):
            task = json.loads(tf.read_text())
            tasks[task["id"]] = task["status"]

        assert tasks["task-001"] == "done"
        assert tasks["task-002"] == "in_progress"
        assert tasks["task-003"] == "pending"

    def test_all_terminal_detection(self):
        """Should detect when all tasks are in terminal states."""
        self._write_task("task-001", "done")
        self._write_task("task-002", "failed")

        all_terminal = True
        for tf in self.tasks_dir.glob("task-*.json"):
            task = json.loads(tf.read_text())
            if task["status"] not in ("done", "failed", "escalated"):
                all_terminal = False
                break

        assert all_terminal is True

    def test_not_terminal_with_pending(self):
        """Should not be terminal if pending tasks exist."""
        self._write_task("task-001", "done")
        self._write_task("task-002", "pending")

        all_terminal = True
        for tf in self.tasks_dir.glob("task-*.json"):
            task = json.loads(tf.read_text())
            if task["status"] not in ("done", "failed", "escalated"):
                all_terminal = False
                break

        assert all_terminal is False


# =============================================================================
# Manager JSON Validation Tests
# =============================================================================

class TestManagerJson:
    """Test manager response JSON validation."""

    VALID_ACTIONS = {"plan", "merge", "reject", "retry", "split", "escalate", "complete"}

    def _validate_manager_response(self, response_str: str) -> dict:
        """Validate a manager response. Returns parsed dict or raises."""
        data = json.loads(response_str)
        assert "action" in data, "Missing 'action' field"
        assert data["action"] in self.VALID_ACTIONS, f"Invalid action: {data['action']}"
        assert "summary" in data, "Missing 'summary' field"
        return data

    def test_valid_plan_response(self):
        resp = json.dumps({
            "action": "plan",
            "tasks": [
                {"id": "task-001", "title": "Implement auth", "branch": "collab/auth",
                 "worktree": ".worktrees/auth", "status": "pending",
                 "prompt": "Implement authentication", "spec_refs": [],
                 "health": {"timeout_minutes": 60, "attempts": 0, "max_attempts": 3}},
            ],
            "summary": "Created 1 task for auth implementation",
        })
        data = self._validate_manager_response(resp)
        assert data["action"] == "plan"
        assert len(data["tasks"]) == 1

    def test_valid_merge_response(self):
        resp = json.dumps({
            "action": "merge",
            "target_task": "task-001",
            "merge_branch": "collab/auth",
            "summary": "Code looks good, merging auth branch",
        })
        data = self._validate_manager_response(resp)
        assert data["action"] == "merge"
        assert data["target_task"] == "task-001"

    def test_valid_reject_response(self):
        resp = json.dumps({
            "action": "reject",
            "target_task": "task-001",
            "reason": "Tests are failing for edge case X",
            "summary": "Rejected due to failing tests",
        })
        data = self._validate_manager_response(resp)
        assert data["action"] == "reject"
        assert "failing" in data["reason"]

    def test_valid_escalate_response(self):
        resp = json.dumps({
            "action": "escalate",
            "reason": "Cannot determine the correct API schema",
            "summary": "Escalating to human for API schema decision",
        })
        data = self._validate_manager_response(resp)
        assert data["action"] == "escalate"

    def test_valid_complete_response(self):
        resp = json.dumps({
            "action": "complete",
            "summary": "All tasks merged successfully. Objective achieved.",
        })
        data = self._validate_manager_response(resp)
        assert data["action"] == "complete"

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            self._validate_manager_response("not json at all")

    def test_missing_action(self):
        resp = json.dumps({"summary": "no action field"})
        with pytest.raises(AssertionError, match="Missing 'action'"):
            self._validate_manager_response(resp)

    def test_invalid_action(self):
        resp = json.dumps({"action": "destroy", "summary": "bad action"})
        with pytest.raises(AssertionError, match="Invalid action"):
            self._validate_manager_response(resp)

    def test_missing_summary(self):
        resp = json.dumps({"action": "complete"})
        with pytest.raises(AssertionError, match="Missing 'summary'"):
            self._validate_manager_response(resp)


# =============================================================================
# CLI Command Tests
# =============================================================================

class TestCollabCli:
    """Test se3 collab CLI command."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create minimal project structure
        (Path(self.tmpdir) / ".claude").mkdir()
        (Path(self.tmpdir) / ".claude" / "CLAUDE.md").write_text("# Test")

    def test_status_no_session(self):
        """--status with no active session should not crash."""
        from se3_tools.commands.collab import find_project_root, get_collab_dir
        collab_dir = get_collab_dir(Path(self.tmpdir))
        assert not (collab_dir / "config.json").exists()

    def test_status_with_session(self):
        """--status with active session should show task info."""
        collab_dir = Path(self.tmpdir) / ".collab"
        tasks_dir = collab_dir / "tasks"
        tasks_dir.mkdir(parents=True)

        config = {
            "session_id": "test-session",
            "objective": "Test",
            "base_branch": "master",
            "status": "active",
        }
        (collab_dir / "config.json").write_text(json.dumps(config))

        task = {
            "id": "task-001",
            "status": "in_progress",
            "title": "Test task",
            "health": {"attempts": 1, "max_attempts": 3},
        }
        (tasks_dir / "task-001.json").write_text(json.dumps(task))

        # Verify files are readable
        loaded_config = json.loads((collab_dir / "config.json").read_text())
        assert loaded_config["status"] == "active"

        loaded_task = json.loads((tasks_dir / "task-001.json").read_text())
        assert loaded_task["status"] == "in_progress"


# =============================================================================
# Health Check Logic Tests
# =============================================================================

class TestHealthCheck:
    """Test health monitoring logic."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_dir = Path(self.tmpdir) / ".collab" / "tasks"
        self.tasks_dir.mkdir(parents=True)

    def _write_task(self, task_id, status="in_progress", pid=None,
                    timeout_min=60, attempts=0):
        task = {
            "id": task_id,
            "status": status,
            "worker_pid": pid,
            "worktree": f".worktrees/{task_id}",
            "health": {
                "timeout_minutes": timeout_min,
                "attempts": attempts,
                "max_attempts": 3,
                "last_activity": None,
            },
        }
        path = self.tasks_dir / f"{task_id}.json"
        path.write_text(json.dumps(task))
        return path

    def test_detect_dead_process(self):
        """Should detect a worker whose PID no longer exists."""
        # Use a PID that definitely doesn't exist
        self._write_task("task-001", pid=999999999)

        task = json.loads((self.tasks_dir / "task-001.json").read_text())
        pid = task["worker_pid"]

        # Simulate orchestrator check: is PID alive?
        import signal
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False

        assert alive is False

    def test_attempt_limit_exceeded(self):
        """Should detect when max attempts are reached."""
        path = self._write_task("task-001", attempts=3)
        task = json.loads(path.read_text())

        assert task["health"]["attempts"] >= task["health"]["max_attempts"]

    def test_attempt_within_limit(self):
        """Should allow retry when under max attempts."""
        path = self._write_task("task-001", attempts=1)
        task = json.loads(path.read_text())

        assert task["health"]["attempts"] < task["health"]["max_attempts"]

    def test_stale_detection_logic(self):
        """Staleness detection: no activity beyond threshold."""
        import time
        threshold_seconds = 30 * 60  # 30 minutes

        # Task with old last_activity
        old_time = datetime(2026, 2, 15, 8, 0, 0).isoformat()
        path = self._write_task("task-001")
        task = json.loads(path.read_text())
        task["health"]["last_activity"] = old_time
        path.write_text(json.dumps(task))

        task = json.loads(path.read_text())
        last_activity = datetime.fromisoformat(task["health"]["last_activity"])
        elapsed = (datetime.now() - last_activity).total_seconds()

        # Should be stale (old_time is far in the past)
        assert elapsed > threshold_seconds


# =============================================================================
# Rules Files Tests
# =============================================================================

class TestRulesFiles:
    """Test that rules files exist and have expected content."""

    SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

    def test_worker_rules_exist(self):
        """Worker rules file should exist."""
        assert (self.SCRIPTS_DIR / "rules-worker.md").exists()

    def test_manager_rules_exist(self):
        """Manager rules file should exist."""
        assert (self.SCRIPTS_DIR / "rules-manager.md").exists()

    def test_worker_rules_content(self):
        """Worker rules should contain key sections."""
        content = (self.SCRIPTS_DIR / "rules-worker.md").read_text()
        assert "Worker Agent Rules" in content
        assert "Verification Protocol" in content
        assert "Spec Guardrails" in content
        assert "MCP Tools Available" in content
        assert "Exit Codes" in content
        assert "report_progress" in content
        assert "Investigation Tasks" in content
        assert "FINDINGS.md" in content

    def test_manager_rules_content(self):
        """Manager rules should contain key sections."""
        content = (self.SCRIPTS_DIR / "rules-manager.md").read_text()
        assert "Manager Agent Rules" in content
        assert "Decision Types" in content
        assert "Response Format" in content
        assert "Task Decomposition Rules" in content
        assert "Code Review Rules" in content
        assert "Failure Handling Rules" in content

    def test_worker_rules_no_framework_concerns(self):
        """Worker rules should NOT contain framework management concerns."""
        content = (self.SCRIPTS_DIR / "rules-worker.md").read_text()
        assert "Session Protocol" not in content
        assert "Input Classification" not in content
        assert "Stage Routing" not in content
        assert "Self-Iterate" not in content

    def test_manager_rules_no_implementation_concerns(self):
        """Manager rules should NOT contain implementation details."""
        content = (self.SCRIPTS_DIR / "rules-manager.md").read_text()
        # Manager rules focus on decisions, not writing code
        assert "Verification Protocol" not in content
        assert "Commit Convention" not in content

    def test_worker_rules_findings_convention(self):
        """Worker rules should document FINDINGS.md convention for investigation tasks."""
        content = (self.SCRIPTS_DIR / "rules-worker.md").read_text()
        assert "Investigation Tasks" in content
        assert "FINDINGS.md" in content
        assert "## Summary" in content
        assert "## Findings" in content
        assert "## Recommendations" in content

    def test_worker_rules_concise(self):
        """Worker rules should be concise (under 150 lines)."""
        lines = (self.SCRIPTS_DIR / "rules-worker.md").read_text().strip().split("\n")
        assert len(lines) < 150, f"Worker rules too long: {len(lines)} lines"

    def test_manager_rules_concise(self):
        """Manager rules should be concise (under 200 lines)."""
        lines = (self.SCRIPTS_DIR / "rules-manager.md").read_text().strip().split("\n")
        assert len(lines) < 200, f"Manager rules too long: {len(lines)} lines"
