"""Tests for the start command."""

import json
import pytest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from .start import (
    create_session_branch,
    compute_git_status,
    run_session_start,
)


class TestCreateSessionBranch:
    """Test the create_session_branch function."""

    def test_creates_branch_with_se3_session_prefix(self, tmp_path):
        """Should create a branch with se3-session/ prefix."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        # Create initial commit
        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Create session branch
        branch_name = create_session_branch(tmp_path)

        # Verify branch name format
        assert branch_name.startswith("se3-session/")
        # Verify timestamp format (YYYYMMDD-HHMMSS)
        timestamp_part = branch_name.split("/")[1]
        assert len(timestamp_part) == 15  # YYYYMMDD-HHMMSS
        assert timestamp_part[8] == "-"

    def test_checks_out_new_branch(self, tmp_path):
        """Should checkout the newly created branch."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        # Create initial commit
        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Create session branch
        branch_name = create_session_branch(tmp_path)

        # Verify we're on the new branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_path, capture_output=True, text=True
        )
        assert result.stdout.strip() == branch_name


class TestComputeGitStatus:
    """Test the compute_git_status function."""

    def test_creates_new_branch_when_not_on_se3_session(self, tmp_path):
        """Should create new branch when not on a se3-session branch."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        # Create initial commit
        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Compute git status with branch creation enabled
        result = compute_git_status(tmp_path, create_branch=True)

        # Verify branch was created
        assert result["branch"].startswith("se3-session/")
        assert result["branch_created"] is True
        assert result.get("branch_reused") is None or result.get("branch_reused") is False

    def test_reuses_existing_se3_session_branch(self, tmp_path):
        """Should reuse branch when already on a se3-session branch."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        # Create initial commit
        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Create and checkout a se3-session branch
        subprocess.run(["git", "checkout", "-b", "se3-session/20260220-000000"], cwd=tmp_path, check=True, capture_output=True)

        # Compute git status with branch creation enabled
        result = compute_git_status(tmp_path, create_branch=True)

        # Verify branch was reused, not changed
        assert result["branch"] == "se3-session/20260220-000000"
        assert result.get("branch_reused") is True
        assert result.get("branch_created") is False

    def test_does_not_create_branch_when_create_branch_false(self, tmp_path):
        """Should not create new branch when create_branch=False."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        # Create initial commit
        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Compute git status with branch creation disabled
        result = compute_git_status(tmp_path, create_branch=False)

        # Verify branch was not changed
        assert result["branch"] == "master" or result["branch"] == "main"
        assert "branch_created" not in result or result.get("branch_created") is False


class TestRunSessionStart:
    """Test the run_session_start function."""

    def test_includes_branch_info_in_state(self, tmp_path):
        """Should include branch creation info in state."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        # Create initial commit
        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Run session start
        result = run_session_start(str(tmp_path))

        # Verify git info includes branch creation flags
        git_info = result.get("git", {})
        assert "branch" in git_info
        assert "branch_created" in git_info
        assert git_info["branch"].startswith("se3-session/")
        assert git_info["branch_created"] is True
