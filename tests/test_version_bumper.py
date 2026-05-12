"""Tests for VersionBumper version detection and initialization edge cases.

Covers:
- detect_version_file() returns None for files without version
- ensure_version() correctly adds version to existing files
- initialize_version_system() handles existing-but-versionless files
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from se3.engine.version_bumper import (
    PythonVersionHandler,
    TomlVersionHandler,
    VersionBumper,
    VersionConfig,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def version_config() -> VersionConfig:
    config = VersionConfig(enabled=True)
    config.auto_generate_script = False
    return config


# === VersionBumper.detect_version_file() edge cases ===


class TestDetectVersionFileVersionless:
    """detect_version_file() must return None when files exist but lack a version."""

    def test_returns_none_for_pyproject_without_version(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """pyproject.toml without [project].version -> detect_version_file returns None."""
        (temp_dir / "pyproject.toml").write_text('[project]\nname = "myproject"\n')

        bumper = VersionBumper(version_config)
        assert bumper.detect_version_file(temp_dir) is None

    def test_returns_none_for_init_py_without_version(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """__init__.py without __version__ -> detect_version_file returns None."""
        src_dir = temp_dir / "src" / "mypkg"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("# empty package\n")

        bumper = VersionBumper(version_config)
        assert bumper.detect_version_file(temp_dir) is None

    def test_returns_none_for_empty_directory(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """Empty directory -> detect_version_file returns None."""
        bumper = VersionBumper(version_config)
        assert bumper.detect_version_file(temp_dir) is None

    def test_returns_path_when_version_present(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """pyproject.toml with version -> detect_version_file returns the path."""
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "myproject"\nversion = "1.0.0"\n'
        )

        bumper = VersionBumper(version_config)
        result = bumper.detect_version_file(temp_dir)
        assert result is not None
        assert result.name == "pyproject.toml"


# === ensure_version() edge cases ===


class TestEnsureVersionEdgeCases:
    """ensure_version() adds a version to files that lack one."""

    def test_toml_adds_version_to_empty_project_section(self, temp_dir: Path):
        """pyproject.toml with empty [project] section -> version added."""
        path = temp_dir / "pyproject.toml"
        path.write_text("[project]\n")

        handler = TomlVersionHandler()
        handler.ensure_version(path, "0.1.0")

        assert handler.read_version(path) == "0.1.0"

    def test_toml_preserves_other_fields(self, temp_dir: Path):
        """ensure_version preserves existing fields in pyproject.toml."""
        path = temp_dir / "pyproject.toml"
        path.write_text(
            '[project]\nname = "demo"\ndescription = "A demo"\n'
        )

        handler = TomlVersionHandler()
        handler.ensure_version(path, "0.2.0")

        content = path.read_text()
        assert 'name = "demo"' in content
        assert 'description = "A demo"' in content
        assert handler.read_version(path) == "0.2.0"

    def test_python_adds_version_to_file_with_imports(self, temp_dir: Path):
        """__init__.py with imports but no __version__ -> version prepended."""
        path = temp_dir / "__init__.py"
        path.write_text("from .core import main\nfrom .utils import helper\n")

        handler = PythonVersionHandler()
        handler.ensure_version(path, "1.0.0")

        content = path.read_text()
        assert content.startswith('__version__ = "1.0.0"\n')
        assert "from .core import main" in content

    def test_python_idempotent_does_not_change_existing(self, temp_dir: Path):
        """Calling ensure_version twice does not duplicate or change version."""
        path = temp_dir / "__init__.py"
        path.write_text("")

        handler = PythonVersionHandler()
        handler.ensure_version(path, "0.1.0")
        first_content = path.read_text()
        handler.ensure_version(path, "9.9.9")
        second_content = path.read_text()

        assert first_content == second_content
        assert handler.read_version(path) == "0.1.0"

    def test_toml_idempotent_does_not_change_existing(self, temp_dir: Path):
        """Calling ensure_version twice does not duplicate or change version."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "x"\nversion = "2.0.0"\n')

        handler = TomlVersionHandler()
        handler.ensure_version(path, "0.1.0")

        assert handler.read_version(path) == "2.0.0"


# === initialize_version_system() edge cases ===


class TestInitializeVersionSystemEdgeCases:
    """initialize_version_system() handles existing files without versions."""

    def test_pyproject_exists_no_version_adds_version(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """pyproject.toml exists without version -> adds version, returns path."""
        (temp_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')

        bumper = VersionBumper(version_config)
        version_file = bumper.initialize_version_system(temp_dir, "0.1.0")

        assert version_file.name == "pyproject.toml"
        assert bumper.read_version(version_file) == "0.1.0"

    def test_init_py_exists_no_version_project_initializes(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """src/pkg/__init__.py exists without __version__ -> project initializable."""
        src_dir = temp_dir / "src" / "mypkg"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("# empty\n")
        # Python marker so project type is detected
        (temp_dir / "requirements.txt").write_text("pytest\n")

        bumper = VersionBumper(version_config)
        version_file = bumper.initialize_version_system(temp_dir, "0.1.0")

        assert version_file.exists()
        assert bumper.read_version(version_file) == "0.1.0"

    def test_raises_file_exists_when_version_present(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """pyproject.toml with version -> FileExistsError (not silently overwritten)."""
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        bumper = VersionBumper(version_config)
        with pytest.raises(FileExistsError):
            bumper.initialize_version_system(temp_dir)


# === VersionBumper.set_version() ===


class TestSetVersionToml:
    """set_version writes a fully-formed version verbatim to a TOML version file."""

    def test_set_version_writes_explicit_version_to_pyproject(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """set_version('2.5.7') on pyproject.toml writes exactly '2.5.7'."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "demo"\nversion = "1.0.0"\n')

        bumper = VersionBumper(version_config)
        result = bumper.set_version("2.5.7", path=path)

        assert result == "2.5.7"
        # Reading the file back yields the exact version (no bump computation)
        assert bumper.read_version(path) == "2.5.7"

    def test_set_version_supports_prerelease_and_build(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """set_version preserves valid prerelease + build metadata strings."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "demo"\nversion = "1.0.0"\n')

        bumper = VersionBumper(version_config)
        result = bumper.set_version("1.2.3-alpha.1+build.42", path=path)

        assert result == "1.2.3-alpha.1+build.42"
        assert bumper.read_version(path) == "1.2.3-alpha.1+build.42"


class TestSetVersionJson:
    """set_version writes a fully-formed version verbatim to a JSON version file."""

    def test_set_version_writes_explicit_version_to_package_json(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """set_version('3.0.0') on package.json writes exactly '3.0.0'."""
        path = temp_dir / "package.json"
        path.write_text('{"name": "demo", "version": "1.2.3"}\n')

        bumper = VersionBumper(version_config)
        result = bumper.set_version("3.0.0", path=path)

        assert result == "3.0.0"
        assert bumper.read_version(path) == "3.0.0"


class TestSetVersionValidation:
    """set_version rejects invalid SemVer strings before writing."""

    def test_set_version_rejects_invalid_semver(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """A non-SemVer string raises ValueError without touching the file."""
        path = temp_dir / "pyproject.toml"
        original = '[project]\nname = "demo"\nversion = "1.0.0"\n'
        path.write_text(original)

        bumper = VersionBumper(version_config)
        with pytest.raises(ValueError, match="Invalid SemVer"):
            bumper.set_version("not-a-version", path=path)

        # File on disk is untouched
        assert path.read_text() == original

    def test_set_version_rejects_empty_string(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """Empty string raises ValueError."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "demo"\nversion = "1.0.0"\n')

        bumper = VersionBumper(version_config)
        with pytest.raises(ValueError):
            bumper.set_version("", path=path)

    def test_set_version_disabled_raises_runtime_error(
        self, temp_dir: Path
    ):
        """When config.enabled is False, set_version raises RuntimeError."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "demo"\nversion = "1.0.0"\n')

        cfg = VersionConfig(enabled=False)
        cfg.auto_generate_script = False
        bumper = VersionBumper(cfg)
        with pytest.raises(RuntimeError, match="disabled"):
            bumper.set_version("2.0.0", path=path)


class TestSetVersionRollback:
    """set_version captures a backup of the previous version for rollback."""

    def test_set_version_rollback_restores_previous_version(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """After set_version, calling rollback restores the original value."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "demo"\nversion = "1.0.0"\n')

        bumper = VersionBumper(version_config)
        bumper.set_version("2.0.0", path=path)
        assert bumper.read_version(path) == "2.0.0"

        bumper.rollback()
        assert bumper.read_version(path) == "1.0.0"

    def test_set_version_rollback_works_for_json(
        self, temp_dir: Path, version_config: VersionConfig
    ):
        """Rollback after set_version on a JSON handler restores the original."""
        path = temp_dir / "package.json"
        path.write_text('{"name": "demo", "version": "0.9.0"}\n')

        bumper = VersionBumper(version_config)
        bumper.set_version("1.0.0", path=path)
        assert bumper.read_version(path) == "1.0.0"

        bumper.rollback()
        assert bumper.read_version(path) == "0.9.0"
