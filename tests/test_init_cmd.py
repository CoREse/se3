"""Tests for se3 init command.

Tests cover:
- Base spec generation from template
- tianluo.yaml creation
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

from tianluo.commands.init_cmd import (
    run_init,
    is_git_repository,
    init_repository,
    create_gitignore,
    DEFAULT_GITIGNORE_TEMPLATE,
)


class TestRunInit:
    """Tests for the run_init function."""

    def test_creates_charter(self, tmp_path):
        """se3 init creates tianluo/charter.md from template (new knowledge system).

        The retired spec corpus (tianluo/specs/base/spec.md) is no longer
        scaffolded — a fresh project bootstraps the code-index + charter +
        why-comment triad with a committable, injectable charter instead.
        """
        result = run_init(tmp_path, "TestProject")

        charter = tmp_path / "tianluo" / "charter.md"
        assert charter.exists()
        content = charter.read_text()
        assert "TestProject" in content
        assert "Charter" in content
        assert "tianluo/charter.md" in result["created"]
        # The retired base spec must NOT be created.
        assert not (tmp_path / "tianluo" / "specs" / "base" / "spec.md").exists()

    def test_creates_se3_yaml(self, tmp_path):
        """se3 init creates tianluo.yaml when it doesn't exist."""
        result = run_init(tmp_path, "TestProject")

        yaml_path = tmp_path / "tianluo.yaml"
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "TestProject" in content
        assert "tianluo.yaml" in result["created"]

    def test_creates_se3_directory(self, tmp_path):
        """se3 init creates the tianluo/ runtime directory."""
        run_init(tmp_path, "TestProject")

        se3_dir = tmp_path / "tianluo"
        assert se3_dir.is_dir()
        # The retired spec corpus directory is no longer scaffolded.
        assert not (se3_dir / "specs").exists()

    def test_no_overwrite_existing_charter(self, tmp_path):
        """se3 init does not overwrite an existing charter."""
        # Pre-create charter with custom content
        se3_dir = tmp_path / "tianluo"
        se3_dir.mkdir(parents=True)
        charter = se3_dir / "charter.md"
        charter.write_text("# Custom content - do not overwrite")

        result = run_init(tmp_path, "TestProject")

        # Verify content was not overwritten
        assert charter.read_text() == "# Custom content - do not overwrite"
        assert any("already exists" in s for s in result["skipped"])

    def test_no_overwrite_existing_se3_yaml(self, tmp_path):
        """se3 init does not overwrite existing tianluo.yaml."""
        yaml_path = tmp_path / "tianluo.yaml"
        yaml_path.write_text("custom: config")

        result = run_init(tmp_path, "TestProject")

        assert yaml_path.read_text() == "custom: config"
        assert any("tianluo.yaml" in s and "already exists" in s for s in result["skipped"])

    def test_default_project_name_placeholder(self, tmp_path):
        """Template placeholders are replaced with project name."""
        run_init(tmp_path, "My Great Project")

        charter = tmp_path / "tianluo" / "charter.md"
        content = charter.read_text()
        assert "My Great Project" in content
        assert "{project_name}" not in content

    def test_idempotent_full_run(self, tmp_path):
        """Running init twice produces no changes on second run."""
        run_init(tmp_path, "TestProject")
        result = run_init(tmp_path, "TestProject")

        assert result["created"] == []
        # tianluo.yaml + charter are skipped; .gitignore is tracked separately
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
        assert "/tianluo/*" in content
        assert "!/tianluo/code-index.md" in content
        assert "!/tianluo/charter.md" in content
        assert "!/tianluo/specs/" not in content
        assert "!/tianluo/issues/" in content
        assert "__pycache__/" in content

    def test_gitignore_has_root_default_deny(self, tmp_path):
        """Generated .gitignore opens with root default-deny `/*`.

        The bearing defense against an agent's `git add -A` staging stray
        root junk is flipping the root layer to default-deny; assert the
        `/*` line is actually present.
        """
        run_init(tmp_path, "TestProject")
        content = (tmp_path / ".gitignore").read_text()
        # `/*` must appear as its own line (a default-deny anchor), not just
        # as a substring of some other pattern.
        assert "/*" in content.splitlines()

    def test_gitignore_whitelists_all_tracked_top_level_entries(self, tmp_path):
        """Every top-level product run_init lands is un-ignored by name.

        Under root default-deny a missing `!/<name>` line would silently
        stop tracking a real project file — exactly the "silent loss" this
        whitelist exists to prevent.
        """
        run_init(tmp_path, "TestProject")
        lines = (tmp_path / ".gitignore").read_text().splitlines()

        # Files/dirs run_init actually creates at the top level, plus the
        # standard tracked project products a fresh repo carries.
        for entry in [
            "!/.gitignore",
            "!/tianluo.yaml",
            "!/VERSIONS.md",
            "!/tianluo/",
            "!/README.md",
            "!/pyproject.toml",
            "!/src/",
            "!/tests/",
            "!/docs/",
            "!/scripts/",
            "!/LICENSE",
            "!/NOTICE",
        ]:
            assert entry in lines, f"missing whitelist entry: {entry}"

    def test_gitignore_does_not_block_init_products(self, tmp_path):
        """git itself must not ignore any product run_init lands.

        Verifies the whitelist with the real ignore engine (not just string
        matching): `git check-ignore` should report none of the landed
        top-level paths as ignored.
        """
        import shutil
        import subprocess

        if shutil.which("git") is None:
            pytest.skip("git not available")

        run_init(tmp_path, "TestProject")

        # run_init initializes the repo and lands these top-level products.
        landed = ["tianluo.yaml", "VERSIONS.md", "tianluo", "tianluo/charter.md", ".gitignore"]
        for rel in landed:
            assert (tmp_path / rel).exists(), f"expected run_init to land {rel}"
            # check-ignore exits 0 when the path IS ignored, 1 when it is not.
            proc = subprocess.run(
                ["git", "check-ignore", "-q", rel],
                cwd=str(tmp_path),
                capture_output=True,
            )
            assert proc.returncode != 0, f"{rel} is unexpectedly gitignored"

    def test_init_force_overwrites_gitignore(self, tmp_path):
        """se3 init --force overwrites existing .gitignore."""
        # Create pre-existing .gitignore
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# Custom content")

        result = run_init(tmp_path, "TestProject", force=True)

        assert result["gitignore_created"] is True
        content = gitignore.read_text()
        assert "/tianluo/*" in content
        assert "!/tianluo/code-index.md" in content
        assert "!/tianluo/charter.md" in content
        assert "!/tianluo/specs/" not in content
        assert "# Custom content" not in content

    def test_init_appends_local_pattern_to_existing_gitignore(self, tmp_path):
        """When .gitignore exists without tianluo.local.yaml, init appends the pattern.

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
        assert "tianluo.local.yaml" in content  # pattern added

    def test_init_local_overrides_yaml_signal(self, tmp_path):
        """When tianluo.local.yaml already exists, run_init must surface the
        ``local_overrides_yaml`` flag so the operator sees that the
        committed tianluo.yaml will be shadowed at load time. This is the
        only signal the user gets about the override.
        """
        local = tmp_path / "tianluo.local.yaml"
        local.write_text("version:\n  enabled: false\n")

        result = run_init(tmp_path, "TestProject")

        assert result["local_overrides_yaml"] is True
        # The committed tianluo.yaml is still created — only the runtime
        # config-load picks the local file. Verify both exist.
        assert (tmp_path / "tianluo.yaml").exists()
        assert local.read_text() == "version:\n  enabled: false\n"

    def test_init_local_overrides_yaml_false_when_no_local(self, tmp_path):
        result = run_init(tmp_path, "TestProject")
        assert result["local_overrides_yaml"] is False

    def test_init_surfaces_gitignore_negated_for_explicit_negation(self, tmp_path):
        """End-to-end: when .gitignore contains ``!tianluo.local.yaml``, the
        ``run_init`` result must set ``gitignore_negated`` and leave
        ``gitignore_appended`` / ``gitignore_created`` false so the
        init_cmd warning path (rather than the "appended" or "created"
        path) fires. Without this integration test the init_cmd echo for
        the negation warning is only covered indirectly via the
        create_gitignore unit test.
        """
        gitignore = tmp_path / ".gitignore"
        original = "*.yaml\n!tianluo.local.yaml\n"
        gitignore.write_text(original)

        result = run_init(tmp_path, "TestProject")

        assert result["gitignore_negated"] is True
        assert result["gitignore_appended"] is False
        assert result["gitignore_created"] is False
        assert result.get("gitignore_error", False) is False
        # File was not mutated.
        assert gitignore.read_text() == original

    def test_init_force_with_existing_local_still_surfaces_signal(self, tmp_path):
        """``se3 init --force`` regenerates tianluo.yaml, but a pre-existing
        tianluo.local.yaml still shadows it at load time. The operator needs
        the ``local_overrides_yaml`` flag even after force re-init,
        otherwise they can silently believe the regenerated tianluo.yaml is
        the active config when in fact the local file is.
        """
        local = tmp_path / "tianluo.local.yaml"
        local.write_text("version:\n  enabled: false\n")
        yaml_path = tmp_path / "tianluo.yaml"
        yaml_path.write_text("# stale content\n")

        result = run_init(tmp_path, "TestProject", force=True)

        # tianluo.yaml was regenerated (force overwrote stale content).
        assert "tianluo.yaml" in result["created"]
        assert "TestProject" in yaml_path.read_text()
        # But the local file still shadows it — flag must be set.
        assert result["local_overrides_yaml"] is True
        # And the local file itself was not touched by --force.
        assert local.read_text() == "version:\n  enabled: false\n"


    def test_creates_versions_md(self, tmp_path):
        """se3 init creates VERSIONS.md from the template in a clean directory."""
        result = run_init(tmp_path, "TestProject")

        versions_md = tmp_path / "VERSIONS.md"
        assert versions_md.exists()
        content = versions_md.read_text()
        # First line is the canonical changelog title.
        assert content.splitlines()[0] == "# Version History"
        # Initial 0.1.0 entry is present.
        assert "0.1.0" in content
        # Placeholders were substituted.
        assert "TestProject" in content
        assert "{project_name}" not in content
        assert "{date}" not in content
        # Result flags reflect creation.
        assert result["versions_md_created"] is True
        assert result["versions_md_already_existed"] is False

    def test_no_overwrite_existing_versions_md(self, tmp_path):
        """se3 init does not overwrite an existing VERSIONS.md."""
        versions_md = tmp_path / "VERSIONS.md"
        versions_md.write_text("# Custom changelog - do not overwrite")

        result = run_init(tmp_path, "TestProject")

        # Content untouched.
        assert versions_md.read_text() == "# Custom changelog - do not overwrite"
        assert result["versions_md_created"] is False
        assert result["versions_md_already_existed"] is True

    def test_versions_md_force_overwrites(self, tmp_path):
        """se3 init --force regenerates VERSIONS.md from the template."""
        versions_md = tmp_path / "VERSIONS.md"
        versions_md.write_text("# Stale changelog")

        result = run_init(tmp_path, "TestProject", force=True)

        assert result["versions_md_created"] is True
        content = versions_md.read_text()
        assert content.splitlines()[0] == "# Version History"
        assert "0.1.0" in content
        assert "# Stale changelog" not in content

    def test_charter_mentions_version_rules(self, tmp_path):
        """The generated charter documents the tianluo/version-rules.md mechanism.

        Guards against regressing to a charter that locks in static bump
        rules and never tells new projects the custom version-rules file
        exists.
        """
        run_init(tmp_path, "TestProject")

        charter = tmp_path / "tianluo" / "charter.md"
        content = charter.read_text()
        assert "tianluo/version-rules.md" in content


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
        """create_gitignore appends tianluo.local.yaml to an existing .gitignore."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing content")

        status, message = create_gitignore(tmp_path)

        assert status == "appended"
        content = gitignore.read_text()
        assert "# existing content" in content
        assert "tianluo.local.yaml" in content

    def test_create_gitignore_idempotent_when_pattern_present(self, tmp_path):
        """When tianluo.local.yaml is already ignored, create_gitignore is a no-op."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing\ntianluo.local.yaml\n")

        status, message = create_gitignore(tmp_path)

        assert status == "unchanged"
        assert "already exists" in message
        assert gitignore.read_text() == "# existing\ntianluo.local.yaml\n"

    def test_create_gitignore_idempotent_with_trailing_slash_pattern(self, tmp_path):
        """``tianluo.local.yaml/`` (directory-only marker) still counts as intent
        to ignore — avoid appending a duplicate ``tianluo.local.yaml`` line.
        """
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing\ntianluo.local.yaml/\n")

        status, message = create_gitignore(tmp_path)

        assert status == "unchanged"
        assert gitignore.read_text() == "# existing\ntianluo.local.yaml/\n"

    def test_create_gitignore_detects_negation_and_refuses_to_append(self, tmp_path):
        """When .gitignore contains ``!tianluo.local.yaml`` (explicit
        un-ignore), appending a plain ``tianluo.local.yaml`` rule would
        create a last-line-wins conflict that could silently flip the
        file's tracked state depending on edit order. create_gitignore
        must return ``"negated"`` and leave the file untouched so the
        caller can surface a warning instead of quietly corrupting the
        ignore semantics.
        """
        gitignore = tmp_path / ".gitignore"
        original = "*.yaml\n!tianluo.local.yaml\n"
        gitignore.write_text(original)

        status, message = create_gitignore(tmp_path)

        assert status == "negated"
        # Original content preserved exactly — no silent mutation.
        assert gitignore.read_text() == original
        assert "!tianluo.local.yaml" in message or "negation" in message

    def test_create_gitignore_broad_negation_does_not_trigger_negated(self, tmp_path):
        """``!*.yaml`` / ``!se3.*`` / ``!*`` are broad un-ignores — the user
        was not explicitly targeting ``tianluo.local.yaml``, they just have a
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
            assert "tianluo.local.yaml" in content
            # Clean up between iterations so each reuses a fresh state.
            gitignore.unlink()

    def test_create_gitignore_recursive_glob_prefix_is_recognised(self, tmp_path):
        """A pattern like ``**/tianluo.local.yaml`` is a valid git rule that
        ignores the file at any depth. ``fnmatchcase`` does not model
        ``**`` itself, so without stripping the prefix the file would be
        considered not-yet-ignored and init would append a redundant
        plain rule. Verify the prefix is handled.
        """
        gitignore = tmp_path / ".gitignore"
        original = "# existing\n**/tianluo.local.yaml\n"
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
    """Tests for base spec auto-loading via ContextBuilder."""

    def test_base_spec_loaded_when_exists(self, tmp_path):
        """ContextBuilder._load_spec_content auto-loads base spec when it exists."""
        from tianluo.engine.context_builder import ContextBuilder

        # Create base spec
        specs_dir = tmp_path / "tianluo" / "specs" / "base"
        specs_dir.mkdir(parents=True)
        (specs_dir / "spec.md").write_text("# Base spec content")

        builder = ContextBuilder(tmp_path)
        content = builder._load_spec_content("base")

        assert content is not None
        assert "Base spec content" in content

    def test_base_spec_none_when_missing(self, tmp_path):
        """_load_spec_content returns None when base spec doesn't exist."""
        from tianluo.engine.context_builder import ContextBuilder

        # Create specs dir without base spec
        specs_dir = tmp_path / "tianluo" / "specs"
        specs_dir.mkdir(parents=True)

        builder = ContextBuilder(tmp_path)
        content = builder._load_spec_content("base")

        assert content is None
