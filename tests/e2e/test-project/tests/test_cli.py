"""Test CLI commands."""

import json
import os
import tempfile
from pathlib import Path

from click.testing import CliRunner

from task_cli.cli import cli


class TestCLI:
    """Test the CLI commands."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.runner = CliRunner()
        self.temp_dir = tempfile.mkdtemp()
        self.tasks_file = Path(self.temp_dir) / "tasks.json"
        os.environ["TASKS_FILE"] = str(self.tasks_file)
    
    def teardown_method(self):
        """Clean up after tests."""
        if "TASKS_FILE" in os.environ:
            del os.environ["TASKS_FILE"]
    
    def test_add_task(self):
        """Test adding a task."""
        result = self.runner.invoke(cli, ["add", "Test task"])
        assert result.exit_code == 0
        assert "Task added" in result.output
        
        # Verify task was saved
        with open(self.tasks_file) as f:
            tasks = json.load(f)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Test task"
    
    def test_add_task_with_priority(self):
        """Test adding a task with priority."""
        result = self.runner.invoke(cli, ["add", "High priority task", "-p", "high"])
        assert result.exit_code == 0
        
        with open(self.tasks_file) as f:
            tasks = json.load(f)
        assert tasks[0]["priority"] == "high"
    
    def test_list_empty(self):
        """Test listing when no tasks exist."""
        result = self.runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No tasks found" in result.output
    
    def test_list_tasks(self):
        """Test listing tasks."""
        # Add some tasks first
        self.runner.invoke(cli, ["add", "Task 1"])
        self.runner.invoke(cli, ["add", "Task 2"])
        
        result = self.runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "Task 1" in result.output
        assert "Task 2" in result.output
    
    def test_mark_done(self):
        """Test marking a task as done."""
        self.runner.invoke(cli, ["add", "Task to complete"])
        
        result = self.runner.invoke(cli, ["done", "1"])
        assert result.exit_code == 0
        assert "marked as done" in result.output
        
        with open(self.tasks_file) as f:
            tasks = json.load(f)
        assert tasks[0]["done"] is True
    
    def test_mark_done_not_found(self):
        """Test marking a non-existent task as done."""
        result = self.runner.invoke(cli, ["done", "999"])
        assert result.exit_code == 0
        assert "not found" in result.output
    
    def test_delete_task(self):
        """Test deleting a task."""
        self.runner.invoke(cli, ["add", "Task to delete"])
        
        result = self.runner.invoke(cli, ["delete", "1"])
        assert result.exit_code == 0
        assert "deleted" in result.output
        
        with open(self.tasks_file) as f:
            tasks = json.load(f)
        assert len(tasks) == 0
    
    def test_delete_not_found(self):
        """Test deleting a non-existent task."""
        result = self.runner.invoke(cli, ["delete", "999"])
        assert result.exit_code == 0
        assert "not found" in result.output
