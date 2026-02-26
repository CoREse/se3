"""Tests for se3 commit command.

Tests cover:
- Test detection (pytest, npm, config-based)
- Sensitive file blocking
- File staging logic
- Commit message generation
- Dry run mode
- Test failure blocking
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from se3_tools.commands.commit import (
    detect_test_command,
    check_sensitive_files,
    get_changed_files,
    generate_message_fallback,
    validate_message,
    SENSITIVE_PATTERNS,
    MIN_MESSAGE_LENGTH,
)


# =============================================================================
# Test Detection
# =============================================================================

class TestDetectTestCommand:
    """Test automatic test command detection."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_detect_pytest_from_tests_dir(self):
        """Should detect pytest when tests/ directory exists."""
        (Path(self.tmpdir) / "tests").mkdir()
        cmd = detect_test_command(Path(self.tmpdir))
        assert cmd is not None
        assert "pytest" in " ".join(cmd)

    def test_detect_pytest_from_pytest_ini(self):
        """Should detect pytest when pytest.ini exists."""
        (Path(self.tmpdir) / "pytest.ini").write_text("[pytest]\n")
        cmd = detect_test_command(Path(self.tmpdir))
        assert cmd is not None
        assert "pytest" in " ".join(cmd)

    def test_detect_npm_test(self):
        """Should detect npm test when package.json has test script."""
        (Path(self.tmpdir) / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}})
        )
        cmd = detect_test_command(Path(self.tmpdir))
        assert cmd is not None
        assert "npm" in cmd[0]

    def test_no_test_framework(self):
        """Should return None when no test framework detected."""
        cmd = detect_test_command(Path(self.tmpdir))
        assert cmd is None

    def test_config_override(self):
        """Should use se3.yaml test_command if present."""
        config = {"commit": {"test_command": "make test"}}
        (Path(self.tmpdir) / "se3.yaml").write_text(
            "commit:\n  test_command: make test\n"
        )
        cmd = detect_test_command(Path(self.tmpdir))
        assert cmd == ["make", "test"]

    def test_npm_without_test_script(self):
        """Should not detect npm test if no test script in package.json."""
        (Path(self.tmpdir) / "package.json").write_text(
            json.dumps({"scripts": {"build": "webpack"}})
        )
        cmd = detect_test_command(Path(self.tmpdir))
        assert cmd is None


# =============================================================================
# Sensitive File Blocking
# =============================================================================

class TestSensitiveFiles:
    """Test sensitive file detection."""

    def test_block_env_file(self):
        blocked = check_sensitive_files([".env"])
        assert ".env" in blocked

    def test_block_env_variants(self):
        blocked = check_sensitive_files([".env.local", ".env.production"])
        assert len(blocked) == 2

    def test_block_key_files(self):
        blocked = check_sensitive_files(["server.key", "cert.pem", "auth.p12"])
        assert len(blocked) == 3

    def test_block_credentials(self):
        blocked = check_sensitive_files(["credentials.json", "secrets.yaml"])
        assert len(blocked) == 2

    def test_allow_normal_files(self):
        blocked = check_sensitive_files([
            "src/auth.py",
            "tests/test_auth.py",
            "README.md",
            "package.json",
        ])
        assert len(blocked) == 0

    def test_block_mixed(self):
        """Should block only sensitive files from a mixed list."""
        files = ["src/main.py", ".env", "tests/test.py", "secrets.yaml"]
        blocked = check_sensitive_files(files)
        assert set(blocked) == {".env", "secrets.yaml"}

    def test_path_with_directory(self):
        """Should check only the basename, not the full path."""
        blocked = check_sensitive_files(["config/.env", "deploy/server.key"])
        assert len(blocked) == 2

    def test_token_json(self):
        blocked = check_sensitive_files(["token.json"])
        assert "token.json" in blocked


# =============================================================================
# Git Integration Tests (require git)
# =============================================================================

class TestGitIntegration:
    """Test git-dependent functionality. Requires git."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Initialize a git repo
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmpdir, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmpdir, capture_output=True
        )
        # Initial commit
        (Path(self.tmpdir) / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmpdir, capture_output=True
        )

    def test_get_changed_files_modified(self):
        """Should detect modified files."""
        (Path(self.tmpdir) / "README.md").write_text("# Updated\n")
        changes = get_changed_files(Path(self.tmpdir))
        assert "README.md" in changes["modified"]

    def test_get_changed_files_untracked(self):
        """Should detect untracked files."""
        (Path(self.tmpdir) / "new_file.py").write_text("print('hi')\n")
        changes = get_changed_files(Path(self.tmpdir))
        assert "new_file.py" in changes["untracked"]

    def test_get_changed_files_staged(self):
        """Should detect staged files."""
        (Path(self.tmpdir) / "README.md").write_text("# Staged\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        changes = get_changed_files(Path(self.tmpdir))
        assert "README.md" in changes["staged"]

    def test_get_changed_files_clean(self):
        """Should return empty lists for clean repo."""
        changes = get_changed_files(Path(self.tmpdir))
        assert len(changes["staged"]) == 0
        assert len(changes["modified"]) == 0
        assert len(changes["untracked"]) == 0

    def test_generate_message_single_file(self):
        """Should generate message with single file name."""
        (Path(self.tmpdir) / "README.md").write_text("# Changed\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        msg = generate_message_fallback(Path(self.tmpdir))
        assert "README.md" in msg

    def test_generate_message_no_staged(self):
        """Should return default message when nothing staged."""
        msg = generate_message_fallback(Path(self.tmpdir))
        assert "Update" in msg


# =============================================================================
# CLI Integration Tests
# =============================================================================

class TestCommitCli:
    """Test the commit CLI command behavior."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Init git repo
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmpdir, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmpdir, capture_output=True
        )
        (Path(self.tmpdir) / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmpdir, capture_output=True
        )

    def test_dry_run_no_commit(self):
        """--dry-run should not actually create a commit."""
        (Path(self.tmpdir) / "README.md").write_text("# Changed\n")

        result = subprocess.run(
            ["se3", "commit", "--dry-run", "-m", "test", "-p", self.tmpdir, "--skip-tests"],
            capture_output=True, text=True
        )

        # Check that no new commit was created
        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=self.tmpdir, capture_output=True, text=True
        )
        assert log.stdout.strip().count("\n") == 0  # Only initial commit

    def test_no_changes_exits_clean(self):
        """Should exit cleanly when there are no changes."""
        result = subprocess.run(
            ["se3", "commit", "-m", "nothing", "-p", self.tmpdir, "--skip-tests"],
            capture_output=True, text=True
        )
        assert "No changes to commit" in result.stdout

    def test_commit_with_message(self):
        """Should commit with provided message."""
        (Path(self.tmpdir) / "README.md").write_text("# Changed\n")

        result = subprocess.run(
            ["se3", "commit", "-m", "Update readme", "-p", self.tmpdir, "--skip-tests"],
            capture_output=True, text=True
        )

        # Verify commit was made
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=self.tmpdir, capture_output=True, text=True
        )
        assert "Update readme" in log.stdout

    def test_sensitive_file_blocked(self):
        """Should block commits that include sensitive files."""
        (Path(self.tmpdir) / ".env").write_text("SECRET=bad\n")
        subprocess.run(["git", "add", ".env"], cwd=self.tmpdir, capture_output=True)

        result = subprocess.run(
            ["se3", "commit", "-m", "oops", "-f", ".env", "-p", self.tmpdir, "--skip-tests"],
            capture_output=True, text=True
        )
        assert "Sensitive files detected" in result.stdout or "BLOCKED" in result.stdout

    def test_test_failure_blocks_commit(self):
        """Should block commit when tests fail."""
        (Path(self.tmpdir) / "README.md").write_text("# Changed\n")
        # Create a tests/ dir with a failing test
        (Path(self.tmpdir) / "tests").mkdir()
        (Path(self.tmpdir) / "tests" / "test_fail.py").write_text(
            "def test_fail():\n    assert False\n"
        )

        result = subprocess.run(
            ["se3", "commit", "-m", "should fail", "-p", self.tmpdir],
            capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "FAILED" in result.stdout or "Tests FAILED" in result.stdout


# =============================================================================
# Message Validation Tests
# =============================================================================

class TestMessageValidation:
    """Test commit message quality validation."""

    def test_good_message_no_warnings(self):
        """A well-formed message should produce no warnings."""
        msg = """Add user authentication with JWT tokens

Implemented login/logout endpoints, token refresh, and middleware.

Status: auth module complete, all 15 tests passing
Next: integrate with frontend login form"""
        warnings = validate_message(msg)
        assert len(warnings) == 0

    def test_short_message_warns(self):
        """Very short messages should trigger a warning."""
        warnings = validate_message("fix bug")
        assert any("too short" in w.lower() for w in warnings)

    def test_missing_status_warns(self):
        """Missing Status: line should trigger a suggestion."""
        msg = "Add authentication module\n\nNext: integrate with frontend"
        warnings = validate_message(msg)
        assert any("Status:" in w for w in warnings)

    def test_missing_next_warns(self):
        """Missing Next: line should trigger a suggestion."""
        msg = "Add authentication module\n\nStatus: complete"
        warnings = validate_message(msg)
        assert any("Next:" in w for w in warnings)

    def test_long_first_line_warns(self):
        """Very long first lines should trigger a warning."""
        msg = "A" * 130 + "\n\nStatus: done\nNext: nothing"
        warnings = validate_message(msg)
        assert any("first line" in w.lower() for w in warnings)

    def test_adequate_short_message(self):
        """A short but adequate message should only get context suggestions."""
        msg = "Fix off-by-one in pagination logic"
        warnings = validate_message(msg)
        # Should warn about missing Status/Next, but not about length
        assert not any("too short" in w.lower() for w in warnings)

    def test_complete_message_format(self):
        """The ideal SE3 message format should pass all checks."""
        msg = """Refactor database connection pooling for better concurrency

Replaced single connection with pool of 10, added health checks.

Status: pool working in dev, load test shows 3x throughput improvement
Next: add pool size to se3.config.yaml, test under production load"""
        warnings = validate_message(msg)
        assert len(warnings) == 0

    def test_validation_is_warnings_not_errors(self):
        """Validation should always return a list (never raise)."""
        # Even empty string should return warnings, not crash
        warnings = validate_message("")
        assert isinstance(warnings, list)
        assert len(warnings) > 0
