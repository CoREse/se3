"""Tests for se3 init command.

Tests cover:
- Base spec generation from template
- se3.yaml creation
- Idempotency (no overwrite of existing files)
- Project name handling
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.commands.init_cmd import run_init


class TestRunInit:
    """Tests for the run_init function."""

    def test_creates_base_spec(self, tmp_path):
        """se3 init creates se3/specs/base/spec.md from template."""
        result = run_init(tmp_path, "TestProject")

        base_spec = tmp_path / "se3" / "specs" / "base" / "spec.md"
        assert base_spec.exists()
        content = base_spec.read_text()
        assert "TestProject" in content
        assert "Base Specification" in content
        assert "se3/specs/base/spec.md" in result["created"]

    def test_creates_se3_yaml(self, tmp_path):
        """se3 init creates se3.yaml when it doesn't exist."""
        result = run_init(tmp_path, "TestProject")

        yaml_path = tmp_path / "se3.yaml"
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "TestProject" in content
        assert "se3.yaml" in result["created"]

    def test_creates_specs_directory(self, tmp_path):
        """se3 init creates se3/specs/ directory."""
        run_init(tmp_path, "TestProject")

        specs_dir = tmp_path / "se3" / "specs"
        assert specs_dir.is_dir()

    def test_no_overwrite_existing_base_spec(self, tmp_path):
        """se3 init does not overwrite existing base spec."""
        # Pre-create base spec with custom content
        base_dir = tmp_path / "se3" / "specs" / "base"
        base_dir.mkdir(parents=True)
        base_spec = base_dir / "spec.md"
        base_spec.write_text("# Custom content - do not overwrite")

        result = run_init(tmp_path, "TestProject")

        # Verify content was not overwritten
        assert base_spec.read_text() == "# Custom content - do not overwrite"
        assert any("already exists" in s for s in result["skipped"])

    def test_no_overwrite_existing_se3_yaml(self, tmp_path):
        """se3 init does not overwrite existing se3.yaml."""
        yaml_path = tmp_path / "se3.yaml"
        yaml_path.write_text("custom: config")

        result = run_init(tmp_path, "TestProject")

        assert yaml_path.read_text() == "custom: config"
        assert any("se3.yaml" in s and "already exists" in s for s in result["skipped"])

    def test_default_project_name_placeholder(self, tmp_path):
        """Template placeholders are replaced with project name."""
        run_init(tmp_path, "My Great Project")

        base_spec = tmp_path / "se3" / "specs" / "base" / "spec.md"
        content = base_spec.read_text()
        assert "My Great Project" in content
        assert "{project_name}" not in content

    def test_idempotent_full_run(self, tmp_path):
        """Running init twice produces no changes on second run."""
        run_init(tmp_path, "TestProject")
        result = run_init(tmp_path, "TestProject")

        assert result["created"] == []
        assert len(result["skipped"]) == 2  # se3.yaml + base spec


class TestReadSpecBaseLoading:
    """Tests for base spec auto-loading in read_spec step."""

    def test_base_spec_loaded_when_exists(self, tmp_path):
        """read_spec auto-loads base spec when it exists."""
        from se3.engine.context_builder import ContextBuilder

        # Create base spec
        specs_dir = tmp_path / "se3" / "specs" / "base"
        specs_dir.mkdir(parents=True)
        (specs_dir / "spec.md").write_text("# Base spec content")

        builder = ContextBuilder(tmp_path)
        content = builder._load_spec_content("base")

        assert content is not None
        assert "Base spec content" in content

    def test_base_spec_none_when_missing(self, tmp_path):
        """_load_spec_content returns None when base spec doesn't exist."""
        from se3.engine.context_builder import ContextBuilder

        # Create specs dir without base spec
        specs_dir = tmp_path / "se3" / "specs"
        specs_dir.mkdir(parents=True)

        builder = ContextBuilder(tmp_path)
        content = builder._load_spec_content("base")

        assert content is None
