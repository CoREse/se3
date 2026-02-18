"""Tests for the human calls archive functionality.

Tests cover:
- Archive command (with and without dry-run)
- List command (various status filters)
- Archive directory creation
- Filename collision handling
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add tools to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from se3_tools.commands.human_calls_cmd import (
    find_project_root,
    get_archive_dir,
    ensure_archive_dir,
    is_call_completed,
    get_pending_calls_count,
    get_responded_calls_count,
)
from se3_tools.human_calls import (
    HumanCall,
    HumanCallStore,
    CallStatus,
    CallType,
    CallPriority,
)


class TestArchiveDirectory:
    """Test archive directory operations."""

    def test_ensure_archive_dir_creates_directory(self):
        """Should create archive directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_dir = ensure_archive_dir(root)

            assert archive_dir.exists()
            assert archive_dir.name == "archive"
            assert archive_dir.parent.name == "human-calls"

    def test_ensure_archive_dir_existing(self):
        """Should return existing archive directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing = root / "human-calls" / "archive"
            existing.mkdir(parents=True)

            archive_dir = ensure_archive_dir(root)
            assert archive_dir == existing


class TestIsCallCompleted:
    """Test call completion detection."""

    def test_responded_status_is_completed(self):
        """Should consider RESPONDED status as completed."""
        call = HumanCall(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            status=CallStatus.RESPONDED,
        )
        assert is_call_completed(call) is True

    def test_completed_status_is_completed(self):
        """Should consider COMPLETED status as completed."""
        call = HumanCall(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            status=CallStatus.COMPLETED,
        )
        assert is_call_completed(call) is True

    def test_pending_status_not_completed(self):
        """Should not consider PENDING status as completed."""
        call = HumanCall(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            status=CallStatus.PENDING,
        )
        assert is_call_completed(call) is False

    def test_meaningful_response_considered_completed(self):
        """Should consider call with meaningful response as completed."""
        call = HumanCall(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            status=CallStatus.PENDING,  # Even with pending status
            response="This is a meaningful response with enough content.",
        )
        assert is_call_completed(call) is True

    def test_default_prompt_response_not_completed(self):
        """Should not consider default prompt marker as completed."""
        call = HumanCall(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            status=CallStatus.PENDING,
            response="<!-- Human: write your response below -->",
        )
        assert is_call_completed(call) is False

    def test_short_response_not_completed(self):
        """Should not consider very short response as completed."""
        call = HumanCall(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            status=CallStatus.PENDING,
            response="Hi",
        )
        assert is_call_completed(call) is False


class TestGetCallsCounts:
    """Test getting call counts for status display."""

    def test_get_pending_calls_count(self):
        """Should return correct count of pending calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)

            store = HumanCallStore(calls_dir)
            store.create_call("Pending 1", "Context 1")
            store.create_call("Pending 2", "Context 2")

            count = get_pending_calls_count(root)
            assert count == 2

    def test_get_pending_calls_count_empty(self):
        """Should return 0 when no pending calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)

            count = get_pending_calls_count(root)
            assert count == 0

    def test_get_pending_calls_count_no_directory(self):
        """Should return 0 when human-calls directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            count = get_pending_calls_count(root)
            assert count == 0

    def test_get_responded_calls_count(self):
        """Should return correct count of responded calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)

            store = HumanCallStore(calls_dir)

            # Create a call and simulate a response
            call = store.create_call("Test Call", "Test context")
            content = call.file_path.read_text()
            content = content.replace(
                "<!-- Human: write your response below -->",
                "This is a response with enough content to be valid."
            )
            call.file_path.write_text(content)

            count = get_responded_calls_count(root)
            assert count == 1


class TestArchiveCommandIntegration:
    """Integration tests for archive command."""

    def test_archive_moves_completed_calls(self):
        """Should move completed calls to archive directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)
            archive_dir = calls_dir / "archive"
            archive_dir.mkdir()

            store = HumanCallStore(calls_dir)

            # Create a pending call
            pending = store.create_call("Pending Call", "This is pending")

            # Create a responded call
            responded = store.create_call("Responded Call", "This has response")
            content = responded.file_path.read_text()
            content = content.replace(
                "<!-- Human: write your response below -->",
                "This is a response with enough content."
            )
            responded.file_path.write_text(content)

            # Verify initial state
            assert pending.file_path.exists()
            assert responded.file_path.exists()

            # Archive the calls
            from se3_tools.commands.human_calls_cmd import archive_calls
            import typer

            # Mock the typer echo to capture output
            outputs = []
            original_echo = typer.echo
            typer.echo = lambda x: outputs.append(x)

            try:
                archive_calls(dry_run=False, project_root=str(root))
            except typer.Exit:
                pass
            finally:
                typer.echo = original_echo

            # Verify responded call was archived
            assert not responded.file_path.exists()
            archived_files = list(archive_dir.glob("*.md"))
            assert len(archived_files) == 1
            assert "responded" in archived_files[0].name.lower()

            # Verify pending call remains
            assert pending.file_path.exists()

    def test_archive_dry_run_does_not_move(self):
        """Should not move files in dry-run mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)
            archive_dir = calls_dir / "archive"
            archive_dir.mkdir()

            store = HumanCallStore(calls_dir)

            # Create a responded call
            responded = store.create_call("Responded Call", "This has response")
            content = responded.file_path.read_text()
            content = content.replace(
                "<!-- Human: write your response below -->",
                "This is a response with enough content."
            )
            responded.file_path.write_text(content)

            # Archive in dry-run mode
            from se3_tools.commands.human_calls_cmd import archive_calls
            import typer

            outputs = []
            original_echo = typer.echo
            typer.echo = lambda x: outputs.append(x)

            try:
                archive_calls(dry_run=True, project_root=str(root))
            except typer.Exit:
                pass
            finally:
                typer.echo = original_echo

            # Verify file was NOT moved
            assert responded.file_path.exists()
            archived_files = list(archive_dir.glob("*.md"))
            assert len(archived_files) == 0

    def test_archive_handles_filename_collision(self):
        """Should handle filename collisions by appending number."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)
            archive_dir = calls_dir / "archive"
            archive_dir.mkdir()

            # Create an existing file in archive with same name pattern
            existing_file = archive_dir / "20260218-120000-test.md"
            existing_file.write_text("Existing archived file")

            store = HumanCallStore(calls_dir)

            # Create a call with same filename pattern (forced)
            call = store.create_call("Test", "Context")
            # Rename to match the existing archived file
            new_path = calls_dir / "20260218-120000-test.md"
            call.file_path.rename(new_path)
            call.file_path = new_path

            # Add response to mark as completed
            content = new_path.read_text()
            content = content.replace(
                "<!-- Human: write your response below -->",
                "This is a response with enough content."
            )
            new_path.write_text(content)

            # Archive the call
            from se3_tools.commands.human_calls_cmd import archive_calls
            import typer

            outputs = []
            original_echo = typer.echo
            typer.echo = lambda x: outputs.append(x)

            try:
                archive_calls(dry_run=False, project_root=str(root))
            except typer.Exit:
                pass
            finally:
                typer.echo = original_echo

            # Verify both files exist with different names
            archived_files = sorted(archive_dir.glob("*.md"))
            assert len(archived_files) == 2
            # One should have the suffix
            assert any("-1" in f.name for f in archived_files)


class TestListCommandIntegration:
    """Integration tests for list command."""

    def test_list_pending_calls(self):
        """Should list only pending calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)

            store = HumanCallStore(calls_dir)
            store.create_call("Pending Call", "This is pending")

            from se3_tools.commands.human_calls_cmd import list_calls
            import typer

            outputs = []
            original_echo = typer.echo
            def mock_echo(x=None):
                if x is not None:
                    outputs.append(x)
            typer.echo = mock_echo

            try:
                list_calls(status="pending", project_root=str(root), limit=20)
            except typer.Exit:
                pass
            finally:
                typer.echo = original_echo

            output_text = "\n".join(str(o) for o in outputs)
            assert "Pending" in output_text or "pending" in output_text.lower()

    def test_list_archived_calls(self):
        """Should list archived calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls_dir = root / "human-calls"
            calls_dir.mkdir(parents=True)
            archive_dir = calls_dir / "archive"
            archive_dir.mkdir()

            # Create an archived file
            archived_file = archive_dir / "20260218-120000-test.md"
            archived_file.write_text("Archived content")

            from se3_tools.commands.human_calls_cmd import list_calls
            import typer

            outputs = []
            original_echo = typer.echo
            def mock_echo2(x=None):
                if x is not None:
                    outputs.append(x)
            typer.echo = mock_echo2

            try:
                list_calls(status="archived", project_root=str(root), limit=20)
            except typer.Exit:
                pass
            finally:
                typer.echo = original_echo

            output_text = "\n".join(str(o) for o in outputs)
            assert "Archived" in output_text or "archived" in output_text.lower()
            assert "test.md" in output_text

    def test_list_no_calls_directory(self):
        """Should handle missing calls directory gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            from se3_tools.commands.human_calls_cmd import list_calls
            import typer

            outputs = []
            original_echo = typer.echo
            typer.echo = lambda x: outputs.append(x)

            try:
                list_calls(status="pending", project_root=str(root), limit=20)
            except typer.Exit:
                pass
            finally:
                typer.echo = original_echo

            output_text = "\n".join(str(o) for o in outputs)
            assert "No" in output_text or "not found" in output_text.lower()
