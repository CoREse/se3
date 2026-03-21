"""Tests for se3 init command.

Tests cover:
- Base spec generation from template
- se3.yaml creation
- Idempotency (no overwrite of existing files)
- Project name handling
- Git repository initialization
- .gitignore creation
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.commands.init_cmd import (
    run_init,
    is_git_repository,
    init_repository,
    create_gitignore,
    DEFAULT_GITIGNORE_TEMPLATE,
)


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
        # se3.yaml + base spec are skipped; .gitignore is tracked separately
        assert len(result["skipped"]) == 2
        # .gitignore should be marked as already existed
        assert result["gitignore_already_existed"] is True

    def test_init_creates_git_repo(self, tmp_path):
        """se3 init creates a git repository in non-git directory."""
        result = run_init(tmp_path, "TestProject")

        assert result["git_initialized"] is True
        assert result["git_already_existed"] is False
        assert (tmp_path / ".git").is_dir()

    def test_init_respects_existing_git(self, tmp_path):
        """se3 init does not reinitialize when already in a git repo."""
        # Pre-initialize git
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

        result = run_init(tmp_path, "TestProject")

        assert result["git_initialized"] is False
        assert result["git_already_existed"] is True

    def test_init_creates_gitignore(self, tmp_path):
        """se3 init creates .gitignore file."""
        result = run_init(tmp_path, "TestProject")

        assert result["gitignore_created"] is True
        assert result["gitignore_already_existed"] is False

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "se3/state/" in content
        assert "__pycache__/" in content

    def test_init_force_overwrites_gitignore(self, tmp_path):
        """se3 init --force overwrites existing .gitignore."""
        # Create pre-existing .gitignore
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# Custom content")

        result = run_init(tmp_path, "TestProject", force=True)

        assert result["gitignore_created"] is True
        content = gitignore.read_text()
        assert "se3/state/" in content
        assert "# Custom content" not in content


class TestGitHelpers:
    """Tests for git helper functions."""

    def test_is_git_repository_returns_true_in_git_repo(self, tmp_path):
        """is_git_repository returns True when inside a git repo."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

        result = is_git_repository(tmp_path)

        assert result is True

    def test_is_git_repository_returns_false_outside_git_repo(self, tmp_path):
        """is_git_repository returns False when not inside a git repo."""
        result = is_git_repository(tmp_path)

        assert result is False

    def test_is_git_repository_finds_parent_git_repo(self, tmp_path):
        """is_git_repository finds .git in parent directory."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

        subdir = tmp_path / "subdir" / "nested"
        subdir.mkdir(parents=True)

        result = is_git_repository(subdir)

        assert result is True

    def test_init_repository_creates_git_repo(self, tmp_path):
        """init_repository creates a git repository."""
        success, message = init_repository(tmp_path)

        assert success is True
        assert (tmp_path / ".git").is_dir()
        assert "initialized" in message.lower() or "empty" in message.lower()

    def test_create_gitignore_creates_file(self, tmp_path):
        """create_gitignore creates .gitignore with template."""
        created, message = create_gitignore(tmp_path)

        assert created is True
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text() == DEFAULT_GITIGNORE_TEMPLATE

    def test_create_gitignore_skips_existing(self, tmp_path):
        """create_gitignore skips existing .gitignore without force."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing content")

        created, message = create_gitignore(tmp_path)

        assert created is False
        assert "already exists" in message
        assert gitignore.read_text() == "# existing content"

    def test_create_gitignore_overwrites_with_force(self, tmp_path):
        """create_gitignore overwrites existing .gitignore with force=True."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing content")

        created, message = create_gitignore(tmp_path, force=True)

        assert created is True
        assert gitignore.read_text() == DEFAULT_GITIGNORE_TEMPLATE


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
