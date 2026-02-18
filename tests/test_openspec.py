"""Tests for the openspec CLI commands.

Tests cover:
- openspec init: Initialize the openspec/ directory structure
- openspec list --specs: List all available specs
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add tools to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from se3_tools.commands.openspec import initialize_openspec, list_specs


# =============================================================================
# OpenSpec Init Tests
# =============================================================================

class TestOpenSpecInit:
    """Test openspec init command."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

    def test_initialize_openspec_creates_directories(self):
        """openspec init should create openspec/, specs/, changes/, changes/archive/."""
        result = initialize_openspec(self.project_root)

        assert result["success"] is True
        assert len(result["created"]) == 3
        assert len(result["existing"]) == 0

        # Verify directories exist
        assert (self.project_root / "openspec" / "specs").exists()
        assert (self.project_root / "openspec" / "changes").exists()
        assert (self.project_root / "openspec" / "changes" / "archive").exists()

    def test_initialize_openspec_idempotent(self):
        """openspec init should be idempotent - not fail if run twice."""
        # First init
        result1 = initialize_openspec(self.project_root)
        assert result1["success"] is True
        assert len(result1["created"]) == 3

        # Second init without force
        result2 = initialize_openspec(self.project_root)
        assert result2["success"] is True
        assert len(result2["existing"]) == 3
        assert "already initialized" in result2["message"].lower()

    def test_initialize_openspec_with_force(self):
        """openspec init --force should reinitialize even if exists."""
        # First init
        initialize_openspec(self.project_root)

        # Force reinit
        result = initialize_openspec(self.project_root, force=True)
        assert result["success"] is True
        # With force, it still reports existing since directories already exist
        assert len(result["existing"]) == 3


# =============================================================================
# OpenSpec List Tests
# =============================================================================

class TestOpenSpecList:
    """Test openspec list --specs command."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create openspec structure
        self.specs_dir = self.project_root / "openspec" / "specs"
        self.specs_dir.mkdir(parents=True)

    def _create_spec(self, name: str, purpose: str = None):
        """Helper to create a spec file."""
        spec_dir = self.specs_dir / name
        spec_dir.mkdir()

        content = f"# {name} Specification\n\n"
        content += "## Purpose\n\n"
        if purpose:
            content += f"{purpose}\n"
        content += "\n## Requirements\n\n"
        content += "### Requirement: Test\n\n"
        content += "The system SHALL test.\n"

        spec_file = spec_dir / "spec.md"
        spec_file.write_text(content)
        return spec_file

    def test_list_specs_empty(self):
        """openspec list --specs should return empty list when no specs."""
        specs = list_specs(self.project_root)
        assert specs == []

    def test_list_specs_single(self):
        """openspec list --specs should list a single spec."""
        self._create_spec("test-spec", "Test purpose description")

        specs = list_specs(self.project_root)
        assert len(specs) == 1
        assert specs[0]["name"] == "test-spec"
        assert "openspec/specs/test-spec/spec.md" in specs[0]["path"]
        assert "Test purpose" in specs[0]["purpose"]

    def test_list_specs_multiple(self):
        """openspec list --specs should list multiple specs sorted by name."""
        self._create_spec("zebra-spec", "Zebra spec purpose")
        self._create_spec("alpha-spec", "Alpha spec purpose")
        self._create_spec("beta-spec", "Beta spec purpose")

        specs = list_specs(self.project_root)
        assert len(specs) == 3

        # Should be sorted alphabetically
        assert specs[0]["name"] == "alpha-spec"
        assert specs[1]["name"] == "beta-spec"
        assert specs[2]["name"] == "zebra-spec"

    def test_list_specs_ignores_non_spec_dirs(self):
        """openspec list --specs should ignore directories without spec.md."""
        # Create a spec
        self._create_spec("valid-spec", "Valid spec")

        # Create a directory without spec.md
        (self.specs_dir / "empty-dir").mkdir()

        specs = list_specs(self.project_root)
        assert len(specs) == 1
        assert specs[0]["name"] == "valid-spec"

    def test_list_specs_no_openspec_dir(self):
        """openspec list --specs should handle missing openspec directory."""
        # Remove openspec directory
        import shutil
        shutil.rmtree(self.project_root / "openspec")

        specs = list_specs(self.project_root)
        assert specs == []


# =============================================================================
# CLI Integration Tests
# =============================================================================

class TestOpenSpecCLI:
    """Test openspec CLI commands via typer."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

    def test_cli_init_command(self):
        """Test the CLI init command directly."""
        from typer.testing import CliRunner
        from se3_tools.commands.openspec import app

        runner = CliRunner()
        result = runner.invoke(app, ["init", "--project-root", str(self.project_root)])

        assert result.exit_code == 0
        assert "Created directories" in result.output

        # Verify directories exist
        assert (self.project_root / "openspec" / "specs").exists()

    def test_cli_list_specs_command(self):
        """Test the CLI list --specs command."""
        from typer.testing import CliRunner
        from se3_tools.commands.openspec import app

        # Create a spec
        specs_dir = self.project_root / "openspec" / "specs"
        specs_dir.mkdir(parents=True)
        spec_dir = specs_dir / "test-spec"
        spec_dir.mkdir()
        spec_file = spec_dir / "spec.md"
        spec_file.write_text("# Test Spec\n\n## Purpose\n\nTest purpose.\n")

        runner = CliRunner()
        result = runner.invoke(app, ["list", "--specs", "--project-root", str(self.project_root)])

        assert result.exit_code == 0
        assert "test-spec" in result.output
        assert "Test purpose" in result.output

    def test_cli_list_specs_json_format(self):
        """Test the CLI list --specs --format json command."""
        from typer.testing import CliRunner
        from se3_tools.commands.openspec import app

        # Create a spec
        specs_dir = self.project_root / "openspec" / "specs"
        specs_dir.mkdir(parents=True)
        spec_dir = specs_dir / "test-spec"
        spec_dir.mkdir()
        spec_file = spec_dir / "spec.md"
        spec_file.write_text("# Test Spec\n\n## Purpose\n\nTest purpose.\n")

        runner = CliRunner()
        result = runner.invoke(app, ["list", "--specs", "--project-root", str(self.project_root), "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "test-spec"

    def test_cli_init_json_format(self):
        """Test the CLI init --format json command."""
        from typer.testing import CliRunner
        from se3_tools.commands.openspec import app

        runner = CliRunner()
        result = runner.invoke(app, ["init", "--project-root", str(self.project_root), "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True
        assert len(data["created"]) == 3
