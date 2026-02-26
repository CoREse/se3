"""Tests for SE3 progress tracking module.

Tests cover:
- Progress entry generation and formatting
- Current Session section management
- Session finalization (handoff)
- Collab report appending
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.progress import (
    ensure_current_session_section,
    get_current_session_entries,
    get_session_number,
    append_commit_entry,
    finalize_session,
    append_collab_report,
    CURRENT_SESSION_HEADER,
    CURRENT_SESSION_MARKER,
)


# =============================================================================
# Current Session Section Tests
# =============================================================================

class TestCurrentSessionSection:
    """Test Current Session section management in progress.md."""

    def test_add_section_to_empty(self):
        """Should add Current Session section to empty content."""
        result = ensure_current_session_section("")
        assert CURRENT_SESSION_HEADER in result
        assert CURRENT_SESSION_MARKER in result

    def test_add_section_to_existing(self):
        """Should add Current Session section to existing content."""
        content = "# Progress\n\nSome history here.\n"
        result = ensure_current_session_section(content)
        assert CURRENT_SESSION_HEADER in result
        assert content.strip() in result

    def test_no_duplicate_section(self):
        """Should not add duplicate Current Session section."""
        content = f"# Progress\n\n{CURRENT_SESSION_HEADER}\n{CURRENT_SESSION_MARKER}\n\n"
        result = ensure_current_session_section(content)
        assert result.count(CURRENT_SESSION_MARKER) == 1


class TestGetCurrentSessionEntries:
    """Test extracting commit entries from Current Session."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

    def test_no_progress_file(self):
        """Should return empty list if no progress.md."""
        entries = get_current_session_entries(self.project_root)
        assert entries == []

    def test_no_marker(self):
        """Should return empty list if no Current Session marker."""
        (self.project_root / "progress.md").write_text("# Progress\n\nOld stuff.\n")
        entries = get_current_session_entries(self.project_root)
        assert entries == []

    def test_extract_entries(self):
        """Should extract commit entries from Current Session."""
        content = f"""# Progress

## 2026-02-16 Session 1 (handoff)

### Done
- Previous work

{CURRENT_SESSION_HEADER}
{CURRENT_SESSION_MARKER}

- `abc1234` Fix authentication bug (3 files)
- `def5678` Add user validation (2 files)
"""
        (self.project_root / "progress.md").write_text(content)
        entries = get_current_session_entries(self.project_root)
        assert len(entries) == 2
        assert "abc1234" in entries[0]
        assert "def5678" in entries[1]


class TestGetSessionNumber:
    """Test session number determination."""

    def test_first_session(self):
        """Should return 1 for content with no session records."""
        assert get_session_number("# Progress\n") == 1

    def test_increment_session(self):
        """Should return next number after existing sessions."""
        content = "## 2026-02-15 Session 1 (handoff)\n\n## 2026-02-16 Session 2 (handoff)\n"
        assert get_session_number(content) == 3


# =============================================================================
# Commit Entry Tests (require git)
# =============================================================================

class TestAppendCommitEntry:
    """Test appending commit entries to progress.md."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmpdir, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmpdir, capture_output=True
        )
        (self.project_root / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=self.tmpdir, capture_output=True
        )

    def test_creates_progress_file(self):
        """Should create progress.md if it doesn't exist."""
        assert not (self.project_root / "progress.md").exists()
        result = append_commit_entry(self.project_root)
        assert result is True
        assert (self.project_root / "progress.md").exists()

    def test_appends_to_existing(self):
        """Should append to existing progress.md."""
        (self.project_root / "progress.md").write_text("# Progress\n\nOld content.\n")
        result = append_commit_entry(self.project_root)
        assert result is True
        content = (self.project_root / "progress.md").read_text()
        assert "Old content" in content
        assert CURRENT_SESSION_MARKER in content

    def test_entry_format(self):
        """Entry should contain commit hash, message, and file count."""
        append_commit_entry(self.project_root)
        content = (self.project_root / "progress.md").read_text()
        # Should have a line like: - `abc1234` Initial commit (1 files)
        assert "Initial commit" in content
        assert "files)" in content

    def test_multiple_entries(self):
        """Multiple commits should append multiple entries."""
        append_commit_entry(self.project_root)

        # Make another commit
        (self.project_root / "README.md").write_text("# Updated\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Update readme"],
            cwd=self.tmpdir, capture_output=True
        )
        append_commit_entry(self.project_root)

        entries = get_current_session_entries(self.project_root)
        assert len(entries) == 2


# =============================================================================
# Session Finalization Tests (require git)
# =============================================================================

class TestFinalizeSession:
    """Test session finalization (handoff)."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmpdir, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmpdir, capture_output=True
        )
        (self.project_root / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=self.tmpdir, capture_output=True
        )

    def test_no_progress_file(self):
        """Should handle missing progress.md gracefully."""
        result = finalize_session(self.project_root)
        assert "no progress.md" in result

    def test_no_current_entries(self):
        """Should handle empty Current Session."""
        (self.project_root / "progress.md").write_text(
            f"# Progress\n\n{CURRENT_SESSION_HEADER}\n{CURRENT_SESSION_MARKER}\n\n"
        )
        result = finalize_session(self.project_root)
        assert "no commits" in result

    def test_finalize_with_entries(self):
        """Should replace Current Session with formal session record."""
        # Create a progress file with commit entries
        append_commit_entry(self.project_root)

        # Make another commit
        (self.project_root / "README.md").write_text("# Changed\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Update readme\n\nNext: add more features"],
            cwd=self.tmpdir, capture_output=True
        )
        append_commit_entry(self.project_root)

        # Finalize
        report = finalize_session(self.project_root)

        assert "Session 1" in report
        assert "### Done" in report
        assert "### Commits" in report
        assert "### Files Changed" in report

        # Check progress.md was updated
        content = (self.project_root / "progress.md").read_text()
        assert "Session 1" in content
        # Current Session section should be empty (ready for next session)
        assert CURRENT_SESSION_MARKER in content


# =============================================================================
# Collab Report Tests
# =============================================================================

class TestAppendCollabReport:
    """Test collab report appending."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

    def test_creates_progress_file(self):
        """Should create progress.md if it doesn't exist."""
        report = "## 2026-02-17 Collab Session: Test\n\n### Tasks\n- task-001: done\n"
        result = append_collab_report(self.project_root, report)
        assert result is True
        content = (self.project_root / "progress.md").read_text()
        assert "Collab Session" in content

    def test_inserts_before_current_session(self):
        """Should insert report before Current Session section."""
        (self.project_root / "progress.md").write_text(
            f"# Progress\n\n{CURRENT_SESSION_HEADER}\n{CURRENT_SESSION_MARKER}\n\n- `abc` commit (1 files)\n"
        )
        report = "## 2026-02-17 Collab Session: Test\n\n### Tasks\n- task-001: done\n"
        append_collab_report(self.project_root, report)

        content = (self.project_root / "progress.md").read_text()
        # Report should be before Current Session
        collab_pos = content.find("Collab Session")
        current_pos = content.find(CURRENT_SESSION_HEADER)
        assert collab_pos < current_pos
