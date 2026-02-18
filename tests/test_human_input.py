"""Tests for the human_input module.

Tests cover:
- HumanInput dataclass serialization
- HumanInputStore file operations
- Input parsing and section extraction
- Response writing
- Archival functionality
"""

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

# Add tools to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from se3_tools.human_input import (
    HumanInput,
    HumanInputStore,
    InputStatus,
    discover_human_inputs,
)


class TestHumanInput:
    """Test HumanInput dataclass."""

    def test_input_creation(self):
        """Should create a HumanInput with default values."""
        inp = HumanInput(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            title="Test Input",
        )

        assert inp.id == "test-001"
        assert inp.title == "Test Input"
        assert inp.status == InputStatus.PENDING
        assert inp.context == ""
        assert inp.request == ""
        assert inp.response == ""

    def test_input_to_dict(self):
        """Should serialize to dictionary."""
        inp = HumanInput(
            id="test-001",
            file_path=Path("/tmp/test.md"),
            title="Test Input",
            context="Test context",
            request="Test request",
        )

        data = inp.to_dict()

        assert data["id"] == "test-001"
        assert data["title"] == "Test Input"
        assert data["status"] == "pending"
        assert data["context"] == "Test context"
        assert data["request"] == "Test request"


class TestHumanInputStore:
    """Test HumanInputStore operations."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = HumanInputStore(self.tmpdir)

    def test_create_input_template(self):
        """Should create an input file template."""
        filepath = self.store.create_input_template(
            title="Test Request",
            context="Some context",
            request="Do something"
        )

        assert filepath.exists()
        content = filepath.read_text()
        assert "# Test Request" in content
        assert "## Context" in content
        assert "Some context" in content
        assert "## Request" in content
        assert "Do something" in content
        assert "## Response" in content

    def test_parse_input_file(self):
        """Should parse an input file correctly."""
        # Create a test file
        filepath = Path(self.tmpdir) / "20260218-120000-test.md"
        content = """# Test Input Title

## Context
This is the context.

## Request
This is the request.

## Response
<!-- Agent will fill this in -->
"""
        filepath.write_text(content)

        inp = self.store.parse_input_file(filepath)

        assert inp is not None
        assert inp.id == "20260218-120000-test"
        assert inp.title == "Test Input Title"
        assert inp.context == "This is the context."
        assert inp.request == "This is the request."
        assert inp.status == InputStatus.PENDING

    def test_parse_input_file_with_response(self):
        """Should detect completed status when response is present."""
        filepath = Path(self.tmpdir) / "test-completed.md"
        content = """# Completed Input

## Context
Context here.

## Request
Request here.

## Response
This is the agent response.
"""
        filepath.write_text(content)

        inp = self.store.parse_input_file(filepath)

        assert inp is not None
        assert inp.status == InputStatus.COMPLETED
        assert inp.response == "This is the agent response."

    def test_get_all_inputs(self):
        """Should get all inputs excluding archived."""
        # Create two input files
        self.store.create_input_template("Input 1")
        self.store.create_input_template("Input 2")

        inputs = self.store.get_all_inputs()

        assert len(inputs) == 2
        assert all(isinstance(inp, HumanInput) for inp in inputs)

    def test_get_pending_inputs(self):
        """Should get only pending inputs."""
        # Create pending input
        self.store.create_input_template("Pending Input")

        # Create completed input
        filepath = Path(self.tmpdir) / "completed.md"
        content = """# Completed

## Context
Context.

## Request
Request.

## Response
Done.
"""
        filepath.write_text(content)

        pending = self.store.get_pending_inputs()

        assert len(pending) == 1
        assert pending[0].title == "Pending Input"

    def test_get_input(self):
        """Should get a specific input by ID."""
        self.store.create_input_template("Specific Input")

        # Find the created file
        inputs = self.store.get_all_inputs()
        input_id = inputs[0].id

        inp = self.store.get_input(input_id)

        assert inp is not None
        assert inp.title == "Specific Input"

    def test_get_input_not_found(self):
        """Should return None for non-existent input."""
        inp = self.store.get_input("non-existent-id")
        assert inp is None

    def test_read_input_file_external(self):
        """Should read input file from any path."""
        # Create file outside store directory
        external_dir = tempfile.mkdtemp()
        filepath = Path(external_dir) / "external.md"
        content = """# External Input

## Context
External context.

## Request
External request.
"""
        filepath.write_text(content)

        inp = self.store.read_input_file(filepath)

        assert inp is not None
        assert inp.title == "External Input"
        assert inp.context == "External context."

    def test_write_response(self):
        """Should write response to input file."""
        self.store.create_input_template("Test Input")
        inputs = self.store.get_all_inputs()
        input_id = inputs[0].id

        success = self.store.write_response(input_id, "This is my response.")

        assert success is True

        # Verify response was written
        inp = self.store.get_input(input_id)
        assert inp.response == "This is my response."
        assert inp.status == InputStatus.COMPLETED

    def test_write_response_not_found(self):
        """Should return False for non-existent input."""
        success = self.store.write_response("non-existent", "response")
        assert success is False

    def test_archive_input(self):
        """Should archive an input file."""
        self.store.create_input_template("To Archive")
        inputs = self.store.get_all_inputs()
        input_id = inputs[0].id

        success = self.store.archive_input(input_id)

        assert success is True
        assert self.store.get_input(input_id) is None
        assert (Path(self.tmpdir) / "archive" / f"{input_id}.md").exists()

    def test_archive_input_not_found(self):
        """Should return False for non-existent input."""
        success = self.store.archive_input("non-existent")
        assert success is False

    def test_process_input(self):
        """Should process input with processor function."""
        self.store.create_input_template(
            title="Process Me",
            context="Some context",
            request="Do something"
        )
        inputs = self.store.get_all_inputs()
        input_id = inputs[0].id

        def processor(context, request):
            return f"Processed: {request}"

        success, message = self.store.process_input(input_id, processor)

        assert success is True
        assert "Successfully processed" in message
        assert self.store.get_input(input_id) is None  # Archived

        # Check archived file has response
        archived_path = Path(self.tmpdir) / "archive" / f"{input_id}.md"
        assert archived_path.exists()
        content = archived_path.read_text()
        assert "Processed: Do something" in content

    def test_process_input_not_pending(self):
        """Should fail if input is not pending."""
        # Create input with response (completed status)
        filepath = Path(self.tmpdir) / "completed.md"
        content = """# Completed

## Context
Context.

## Request
Request.

## Response
Already done.
"""
        filepath.write_text(content)

        def processor(context, request):
            return "New response"

        success, message = self.store.process_input("completed", processor)

        assert success is False
        assert "not pending" in message


class TestSectionExtraction:
    """Test section extraction with different formats."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = HumanInputStore(self.tmpdir)

    def test_extract_context_section(self):
        """Should extract context section."""
        filepath = Path(self.tmpdir) / "test.md"
        content = """# Title

## Context
Line 1
Line 2

## Request
Request text
"""
        filepath.write_text(content)
        inp = self.store.parse_input_file(filepath)
        assert inp.context == "Line 1\nLine 2"

    def test_extract_request_section(self):
        """Should extract request section."""
        filepath = Path(self.tmpdir) / "test.md"
        content = """# Title

## Context
Context text

## Request
Request line 1
Request line 2

## Response
Response text
"""
        filepath.write_text(content)
        inp = self.store.parse_input_file(filepath)
        assert inp.request == "Request line 1\nRequest line 2"

    def test_extract_response_section(self):
        """Should extract response section."""
        filepath = Path(self.tmpdir) / "test.md"
        content = """# Title

## Context
Context

## Request
Request

## Response
Response line 1
Response line 2
"""
        filepath.write_text(content)
        inp = self.store.parse_input_file(filepath)
        assert inp.response == "Response line 1\nResponse line 2"

    def test_chinese_headers(self):
        """Should handle Chinese section headers."""
        filepath = Path(self.tmpdir) / "test.md"
        content = """# 标题

## 上下文
中文上下文

## 请求
中文请求
"""
        filepath.write_text(content)
        inp = self.store.parse_input_file(filepath)
        assert inp.context == "中文上下文"
        assert inp.request == "中文请求"


class TestDiscoverHumanInputs:
    """Test discover_human_inputs compatibility function."""

    def test_discover_human_inputs(self):
        """Should discover all human inputs."""
        tmpdir = tempfile.mkdtemp()
        store = HumanInputStore(tmpdir)
        store.create_input_template("Input 1")
        store.create_input_template("Input 2")

        result = discover_human_inputs(tmpdir)

        assert len(result) == 2
        assert all("file" in item for item in result)
        assert all("status" in item for item in result)
