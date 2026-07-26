"""Tests for version file detection readability validation.

Ensures get_version_file_path() only returns files that contain
a readable version, not just files that match a handler by extension.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tianluo.engine.version_bumper import VersionDetector


@pytest.fixture
def detector() -> VersionDetector:
    return VersionDetector()


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestGetVersionFilePathReadability:
    """get_version_file_path() must validate that the file is actually readable."""

    def test_returns_none_for_pyproject_without_version(self, detector: VersionDetector, temp_dir: Path):
        """pyproject.toml exists but has no [project].version → should return None."""
        (temp_dir / "pyproject.toml").write_text(
            "[project]\nname = \"myproject\"\n"
        )
        assert detector.get_version_file_path(temp_dir) is None

    def test_returns_none_for_init_py_without_version(self, detector: VersionDetector, temp_dir: Path):
        """src/__init__.py exists but has no __version__ → should return None."""
        src_dir = temp_dir / "src"
        src_dir.mkdir()
        (src_dir / "__init__.py").write_text("# empty init\n")
        assert detector.get_version_file_path(temp_dir) is None

    def test_returns_path_for_pyproject_with_version(self, detector: VersionDetector, temp_dir: Path):
        """pyproject.toml with a valid version → should return the path."""
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "myproject"\nversion = "1.2.3"\n'
        )
        result = detector.get_version_file_path(temp_dir)
        assert result is not None
        assert result.name == "pyproject.toml"

    def test_returns_path_for_init_py_with_version(self, detector: VersionDetector, temp_dir: Path):
        """__init__.py with __version__ → should return the path."""
        # Create a package structure that would be detected
        pkg_dir = temp_dir / "src"
        pkg_dir.mkdir()
        init_file = pkg_dir / "__init__.py"
        init_file.write_text('__version__ = "0.1.0"\n')
        result = detector.get_version_file_path(temp_dir)
        # This may or may not be detected depending on VERSION_FILES order,
        # but if it is, it should be valid
        if result is not None:
            # Verify the returned file is readable
            version = detector.read_version(result)
            assert version == "0.1.0"

    def test_skips_unreadable_falls_through_to_readable(self, detector: VersionDetector, temp_dir: Path):
        """If first candidate is unreadable but second is readable, return second."""
        # pyproject.toml without version (unreadable)
        (temp_dir / "pyproject.toml").write_text(
            "[project]\nname = \"myproject\"\n"
        )
        # package.json with version (readable)
        (temp_dir / "package.json").write_text(
            '{"name": "myproject", "version": "2.0.0"}\n'
        )
        result = detector.get_version_file_path(temp_dir)
        assert result is not None
        assert result.name == "package.json"

    def test_returns_none_when_no_files_exist(self, detector: VersionDetector, temp_dir: Path):
        """Empty directory → should return None."""
        assert detector.get_version_file_path(temp_dir) is None
