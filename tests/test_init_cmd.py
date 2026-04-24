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
        assert "/se3/*" in content
        assert "!/se3/specs/" in content
        assert "!/se3/issues/" in content
        assert "__pycache__/" in content

    def test_init_force_overwrites_gitignore(self, tmp_path):
        """se3 init --force overwrites existing .gitignore."""
        # Create pre-existing .gitignore
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# Custom content")

        result = run_init(tmp_path, "TestProject", force=True)

        assert result["gitignore_created"] is True
        content = gitignore.read_text()
        assert "/se3/*" in content
        assert "!/se3/specs/" in content
        assert "# Custom content" not in content

    def test_init_appends_local_pattern_to_existing_gitignore(self, tmp_path):
        """When .gitignore exists without se3.local.yaml, init appends the pattern.

        This is the user's only protection against accidentally committing a
        local config file in a project whose .gitignore predates this feature.
        """
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# legacy gitignore\n*.pyc\n")

        result = run_init(tmp_path, "TestProject")

        assert result["gitignore_appended"] is True
        assert result["gitignore_created"] is False
        content = gitignore.read_text()
        assert "*.pyc" in content  # original content preserved
        assert "se3.local.yaml" in content  # pattern added

    def test_init_local_overrides_yaml_signal(self, tmp_path):
        """When se3.local.yaml already exists, run_init must surface the
        ``local_overrides_yaml`` flag so the operator sees that the
        committed se3.yaml will be shadowed at load time. This is the
        only signal the user gets about the override.
        """
        local = tmp_path / "se3.local.yaml"
        local.write_text("version:\n  enabled: false\n")

        result = run_init(tmp_path, "TestProject")

        assert result["local_overrides_yaml"] is True
        # The committed se3.yaml is still created — only the runtime
        # config-load picks the local file. Verify both exist.
        assert (tmp_path / "se3.yaml").exists()
        assert local.read_text() == "version:\n  enabled: false\n"

    def test_init_local_overrides_yaml_false_when_no_local(self, tmp_path):
        result = run_init(tmp_path, "TestProject")
        assert result["local_overrides_yaml"] is False

    def test_init_surfaces_gitignore_negated_for_explicit_negation(self, tmp_path):
        """End-to-end: when .gitignore contains ``!se3.local.yaml``, the
        ``run_init`` result must set ``gitignore_negated`` and leave
        ``gitignore_appended`` / ``gitignore_created`` false so the
        init_cmd warning path (rather than the "appended" or "created"
        path) fires. Without this integration test the init_cmd echo for
        the negation warning is only covered indirectly via the
        create_gitignore unit test.
        """
        gitignore = tmp_path / ".gitignore"
        original = "*.yaml\n!se3.local.yaml\n"
        gitignore.write_text(original)

        result = run_init(tmp_path, "TestProject")

        assert result["gitignore_negated"] is True
        assert result["gitignore_appended"] is False
        assert result["gitignore_created"] is False
        assert result.get("gitignore_error", False) is False
        # File was not mutated.
        assert gitignore.read_text() == original

    def test_init_force_with_existing_local_still_surfaces_signal(self, tmp_path):
        """``se3 init --force`` regenerates se3.yaml, but a pre-existing
        se3.local.yaml still shadows it at load time. The operator needs
        the ``local_overrides_yaml`` flag even after force re-init,
        otherwise they can silently believe the regenerated se3.yaml is
        the active config when in fact the local file is.
        """
        local = tmp_path / "se3.local.yaml"
        local.write_text("version:\n  enabled: false\n")
        yaml_path = tmp_path / "se3.yaml"
        yaml_path.write_text("# stale content\n")

        result = run_init(tmp_path, "TestProject", force=True)

        # se3.yaml was regenerated (force overwrote stale content).
        assert "se3.yaml" in result["created"]
        assert "TestProject" in yaml_path.read_text()
        # But the local file still shadows it — flag must be set.
        assert result["local_overrides_yaml"] is True
        # And the local file itself was not touched by --force.
        assert local.read_text() == "version:\n  enabled: false\n"


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
        status, message = create_gitignore(tmp_path)

        assert status == "created"
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text() == DEFAULT_GITIGNORE_TEMPLATE

    def test_create_gitignore_appends_local_pattern_to_existing(self, tmp_path):
        """create_gitignore appends se3.local.yaml to an existing .gitignore."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing content")

        status, message = create_gitignore(tmp_path)

        assert status == "appended"
        content = gitignore.read_text()
        assert "# existing content" in content
        assert "se3.local.yaml" in content

    def test_create_gitignore_idempotent_when_pattern_present(self, tmp_path):
        """When se3.local.yaml is already ignored, create_gitignore is a no-op."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing\nse3.local.yaml\n")

        status, message = create_gitignore(tmp_path)

        assert status == "unchanged"
        assert "already exists" in message
        assert gitignore.read_text() == "# existing\nse3.local.yaml\n"

    def test_create_gitignore_idempotent_with_trailing_slash_pattern(self, tmp_path):
        """``se3.local.yaml/`` (directory-only marker) still counts as intent
        to ignore — avoid appending a duplicate ``se3.local.yaml`` line.
        """
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing\nse3.local.yaml/\n")

        status, message = create_gitignore(tmp_path)

        assert status == "unchanged"
        assert gitignore.read_text() == "# existing\nse3.local.yaml/\n"

    def test_create_gitignore_detects_negation_and_refuses_to_append(self, tmp_path):
        """When .gitignore contains ``!se3.local.yaml`` (explicit
        un-ignore), appending a plain ``se3.local.yaml`` rule would
        create a last-line-wins conflict that could silently flip the
        file's tracked state depending on edit order. create_gitignore
        must return ``"negated"`` and leave the file untouched so the
        caller can surface a warning instead of quietly corrupting the
        ignore semantics.
        """
        gitignore = tmp_path / ".gitignore"
        original = "*.yaml\n!se3.local.yaml\n"
        gitignore.write_text(original)

        status, message = create_gitignore(tmp_path)

        assert status == "negated"
        # Original content preserved exactly — no silent mutation.
        assert gitignore.read_text() == original
        assert "!se3.local.yaml" in message or "negation" in message

    def test_create_gitignore_broad_negation_does_not_trigger_negated(self, tmp_path):
        """``!*.yaml`` / ``!se3.*`` / ``!*`` are broad un-ignores — the user
        was not explicitly targeting ``se3.local.yaml``, they just have a
        wide rule that happens to cover it. In that case we should still
        append our ignore block rather than return ``"negated"`` with a
        misleading "explicit negation" warning.
        """
        for broad in ("!*.yaml\n", "!se3.*\n", "!*\n"):
            gitignore = tmp_path / ".gitignore"
            gitignore.write_text(f"# existing\n{broad}")

            status, message = create_gitignore(tmp_path)

            assert status == "appended", (
                f"broad negation {broad!r} must NOT trigger 'negated' status; "
                f"got {status!r} ({message!r})"
            )
            content = gitignore.read_text()
            assert "se3.local.yaml" in content
            # Clean up between iterations so each reuses a fresh state.
            gitignore.unlink()

    def test_create_gitignore_recursive_glob_prefix_is_recognised(self, tmp_path):
        """A pattern like ``**/se3.local.yaml`` is a valid git rule that
        ignores the file at any depth. ``fnmatchcase`` does not model
        ``**`` itself, so without stripping the prefix the file would be
        considered not-yet-ignored and init would append a redundant
        plain rule. Verify the prefix is handled.
        """
        gitignore = tmp_path / ".gitignore"
        original = "# existing\n**/se3.local.yaml\n"
        gitignore.write_text(original)

        status, message = create_gitignore(tmp_path)

        assert status == "unchanged"
        assert gitignore.read_text() == original

    def test_create_gitignore_overwrites_with_force(self, tmp_path):
        """create_gitignore overwrites existing .gitignore with force=True."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing content")

        status, message = create_gitignore(tmp_path, force=True)

        assert status == "created"
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
