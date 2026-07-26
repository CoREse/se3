"""Tests for ensure_version() and initialize_version_system() with existing files lacking versions.

Covers:
- TomlVersionHandler.ensure_version()
- PythonVersionHandler.ensure_version()
- initialize_version_system() when files exist but have no version
- commit_handler resilience when read_version fails on detected version file
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tianluo.engine.version_bumper import (
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


# === TomlVersionHandler.ensure_version() ===


class TestTomlEnsureVersion:
    def test_adds_version_under_existing_project_section(self, temp_dir: Path):
        """pyproject.toml has [project] but no version → adds version."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[project]\nname = "myproject"\n')

        handler = TomlVersionHandler()
        handler.ensure_version(path, "0.1.0")

        assert handler.read_version(path) == "0.1.0"
        # Original content preserved
        content = path.read_text()
        assert 'name = "myproject"' in content

    def test_adds_project_section_when_missing(self, temp_dir: Path):
        """pyproject.toml has no [project] section → adds section with version."""
        path = temp_dir / "pyproject.toml"
        path.write_text('[build-system]\nrequires = ["setuptools"]\n')

        handler = TomlVersionHandler()
        handler.ensure_version(path, "1.0.0")

        assert handler.read_version(path) == "1.0.0"
        content = path.read_text()
        assert "[project]" in content
        assert "[build-system]" in content

    def test_idempotent_when_version_exists(self, temp_dir: Path):
        """File already has a version → ensure_version does nothing."""
        path = temp_dir / "pyproject.toml"
        original = '[project]\nname = "myproject"\nversion = "2.0.0"\n'
        path.write_text(original)

        handler = TomlVersionHandler()
        handler.ensure_version(path, "0.1.0")

        # Version unchanged
        assert handler.read_version(path) == "2.0.0"
        assert path.read_text() == original


# === PythonVersionHandler.ensure_version() ===


class TestPythonEnsureVersion:
    def test_adds_version_to_empty_init_py(self, temp_dir: Path):
        """__init__.py with no content → adds __version__."""
        path = temp_dir / "__init__.py"
        path.write_text("")

        handler = PythonVersionHandler()
        handler.ensure_version(path, "0.1.0")

        assert handler.read_version(path) == "0.1.0"

    def test_prepends_version_to_existing_init_py(self, temp_dir: Path):
        """__init__.py with code but no __version__ → prepends __version__."""
        path = temp_dir / "__init__.py"
        path.write_text("# My package\nimport os\n")

        handler = PythonVersionHandler()
        handler.ensure_version(path, "0.2.0")

        assert handler.read_version(path) == "0.2.0"
        content = path.read_text()
        assert content.startswith('__version__ = "0.2.0"\n')
        assert "# My package" in content
        assert "import os" in content

    def test_idempotent_when_version_exists(self, temp_dir: Path):
        """File already has __version__ → ensure_version does nothing."""
        path = temp_dir / "__init__.py"
        original = '__version__ = "3.0.0"\nimport os\n'
        path.write_text(original)

        handler = PythonVersionHandler()
        handler.ensure_version(path, "0.1.0")

        assert handler.read_version(path) == "3.0.0"
        assert path.read_text() == original


# === initialize_version_system() with existing files ===


class TestInitializeVersionSystemExistingFiles:
    def test_pyproject_exists_without_version(self, temp_dir: Path, version_config: VersionConfig):
        """pyproject.toml exists but has no version → adds version instead of crashing."""
        (temp_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')

        bumper = VersionBumper(version_config)
        version_file = bumper.initialize_version_system(temp_dir, "0.1.0")

        assert version_file.name == "pyproject.toml"
        assert bumper.read_version(version_file) == "0.1.0"

    def test_pyproject_exists_with_version_raises(self, temp_dir: Path, version_config: VersionConfig):
        """pyproject.toml with a valid version → still raises FileExistsError."""
        (temp_dir / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "1.0.0"\n')

        bumper = VersionBumper(version_config)
        with pytest.raises(FileExistsError):
            bumper.initialize_version_system(temp_dir)

    def test_init_py_exists_without_version(self, temp_dir: Path, version_config: VersionConfig):
        """src/pkg/__init__.py exists but has no __version__ → project can be initialized."""
        src_dir = temp_dir / "src" / "mypackage"
        src_dir.mkdir(parents=True)
        init_file = src_dir / "__init__.py"
        init_file.write_text("# empty package\n")

        # Add a Python marker so project type is detected
        (temp_dir / "requirements.txt").write_text("pytest\n")

        bumper = VersionBumper(version_config)
        version_file = bumper.initialize_version_system(temp_dir, "0.1.0")

        # Should succeed — either created pyproject.toml or added version to existing file
        assert version_file.exists()
        assert bumper.read_version(version_file) == "0.1.0"
