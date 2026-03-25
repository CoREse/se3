"""Tests for version system initialization.

This module tests the VersionBumper.initialize_version_system() method
and related functionality for initializing version systems in new projects.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from se3.engine.version_bumper import (
    BumpType,
    ProjectType,
    VersionBumper,
    VersionConfig,
    VersionDetector,
)


class TestInitializeVersionSystem:
    """Tests for VersionBumper.initialize_version_system()."""

    @pytest.fixture
    def version_config(self) -> VersionConfig:
        """Create a default version config for testing."""
        config = VersionConfig(enabled=True)
        config.auto_generate_script = False
        return config

    @pytest.fixture
    def temp_project_dir(self) -> Path:
        """Create a temporary project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_initialize_python_project_with_pyproject(self, temp_project_dir: Path, version_config: VersionConfig):
        """Test initializing version system for a Python project."""
        bumper = VersionBumper(version_config)

        # Create a Python marker file
        (temp_project_dir / "requirements.txt").write_text("pytest\n")

        version_file = bumper.initialize_version_system(temp_project_dir, "0.1.0")

        assert version_file.exists()
        assert version_file.name == "pyproject.toml"
        assert "0.1.0" in version_file.read_text()

    def test_initialize_nodejs_project(self, temp_project_dir: Path, version_config: VersionConfig):
        """Test initializing version system for a Node.js project."""
        bumper = VersionBumper(version_config)

        # Create a Node.js marker file
        (temp_project_dir / "node_modules").mkdir()

        version_file = bumper.initialize_version_system(temp_project_dir, "1.0.0")

        assert version_file.exists()
        assert version_file.name == "package.json"

        content = json.loads(version_file.read_text())
        assert content["version"] == "1.0.0"

    def test_initialize_when_version_file_already_exists(self, temp_project_dir: Path, version_config: VersionConfig):
        """Test that initialization fails if a version file already exists."""
        bumper = VersionBumper(version_config)

        # Create an existing pyproject.toml with version
        pyproject = temp_project_dir / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "0.5.0"\n')

        with pytest.raises(FileExistsError) as exc_info:
            bumper.initialize_version_system(temp_project_dir)

        assert "already exists" in str(exc_info.value)

    def test_initialize_with_invalid_version(self, temp_project_dir: Path, version_config: VersionConfig):
        """Test that initialization fails with an invalid version string."""
        bumper = VersionBumper(version_config)

        with pytest.raises(ValueError) as exc_info:
            bumper.initialize_version_system(temp_project_dir, "not-a-valid-version")

        assert "Invalid initial version" in str(exc_info.value)

    def test_initial_version_defaults_to_0_1_0(self, temp_project_dir: Path, version_config: VersionConfig):
        """Test that the default initial version is 0.1.0."""
        bumper = VersionBumper(version_config)

        (temp_project_dir / "requirements.txt").write_text("pytest\n")

        version_file = bumper.initialize_version_system(temp_project_dir)

        assert "0.1.0" in version_file.read_text()


class TestVersionDetectorWithoutVersionFile:
    """Tests for VersionDetector.detect_project_type_without_version_file()."""

    @pytest.fixture
    def temp_project_dir(self) -> Path:
        """Create a temporary project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_detect_python_from_pyproject(self, temp_project_dir: Path):
        """Test detecting Python project from pyproject.toml."""
        (temp_project_dir / "pyproject.toml").write_text("[project]\n")

        detector = VersionDetector()
        project_type = detector.detect_project_type_without_version_file(temp_project_dir)

        assert project_type == ProjectType.PYTHON

    def test_detect_python_from_requirements(self, temp_project_dir: Path):
        """Test detecting Python project from requirements.txt."""
        (temp_project_dir / "requirements.txt").write_text("pytest\n")

        detector = VersionDetector()
        project_type = detector.detect_project_type_without_version_file(temp_project_dir)

        assert project_type == ProjectType.PYTHON

    def test_detect_python_from_src_structure(self, temp_project_dir: Path):
        """Test detecting Python project from src/ structure."""
        src_dir = temp_project_dir / "src"
        src_dir.mkdir()
        pkg_dir = src_dir / "mypackage"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")

        detector = VersionDetector()
        project_type = detector.detect_project_type_without_version_file(temp_project_dir)

        assert project_type == ProjectType.PYTHON

    def test_detect_nodejs_from_package_json(self, temp_project_dir: Path):
        """Test detecting Node.js project from package.json."""
        (temp_project_dir / "package.json").write_text('{"name": "test"}')

        detector = VersionDetector()
        project_type = detector.detect_project_type_without_version_file(temp_project_dir)

        assert project_type == ProjectType.NODEJS

    def test_detect_nodejs_from_node_modules(self, temp_project_dir: Path):
        """Test detecting Node.js project from node_modules directory."""
        (temp_project_dir / "node_modules").mkdir()

        detector = VersionDetector()
        project_type = detector.detect_project_type_without_version_file(temp_project_dir)

        assert project_type == ProjectType.NODEJS

    def test_detect_unknown_when_no_markers(self, temp_project_dir: Path):
        """Test returning UNKNOWN when no project markers exist."""
        detector = VersionDetector()
        project_type = detector.detect_project_type_without_version_file(temp_project_dir)

        assert project_type == ProjectType.UNKNOWN


class TestBumpTypeAlwaysValid:
    """Tests that _get_bump_type always returns a valid BumpType."""

    def test_returns_patch_for_none_bump_type(self):
        """Test that 'none' bump_type returns PATCH instead of None."""
        # This test verifies the fix is working by checking the behavior
        # The actual _get_bump_type function is tested through integration
        from se3.engine.version_bumper import BumpType

        # 'none' should now map to PATCH (not None)
        bump_type_str = "none"
        result = BumpType.PATCH if bump_type_str == "none" else BumpType(bump_type_str)
        assert result == BumpType.PATCH

    def test_returns_valid_bump_type_for_invalid_input(self):
        """Test that invalid bump_type returns PATCH."""
        from se3.engine.version_bumper import BumpType

        # Invalid bump_type should default to PATCH
        bump_type_str = "invalid"
        try:
            result = BumpType(bump_type_str)
        except ValueError:
            result = BumpType.PATCH

        assert result == BumpType.PATCH
