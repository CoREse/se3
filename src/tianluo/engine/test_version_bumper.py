"""Tests for version bumper functionality.

Unit tests for all version file handlers and VersionBumper class.
"""

import ast
import json
import tempfile
from pathlib import Path

import pytest

from .version_bumper import (
    BumpType,
    JsonVersionHandler,
    PythonVersionHandler,
    SetupPyHandler,
    TomlVersionHandler,
    VersionBumper,
    VersionConfig,
    VersionFileHandler,
)


class TestJsonVersionHandler:
    """Tests for JsonVersionHandler."""

    def test_can_handle_json_file(self):
        """Test handler recognizes JSON files."""
        handler = JsonVersionHandler()
        assert handler.can_handle(Path("package.json"))
        assert handler.can_handle(Path("version.json"))
        assert handler.can_handle(Path("/path/to/file.json"))

    def test_cannot_handle_non_json_file(self):
        """Test handler rejects non-JSON files."""
        handler = JsonVersionHandler()
        assert not handler.can_handle(Path("version.py"))
        assert not handler.can_handle(Path("pyproject.toml"))
        assert not handler.can_handle(Path("setup.py"))

    def test_read_version_from_package_json(self):
        """Test reading version from package.json."""
        handler = JsonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "package.json"
            path.write_text(json.dumps({"name": "test", "version": "1.2.3"}))
            assert handler.read_version(path) == "1.2.3"

    def test_read_version_missing_field(self):
        """Test error when version field is missing."""
        handler = JsonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "package.json"
            path.write_text(json.dumps({"name": "test"}))
            with pytest.raises(ValueError, match="No 'version' field"):
                handler.read_version(path)

    def test_write_version_preserves_structure(self):
        """Test writing version preserves JSON structure."""
        handler = JsonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "package.json"
            original = {
                "name": "test-package",
                "version": "1.0.0",
                "dependencies": {"lodash": "^4.0.0"},
            }
            path.write_text(json.dumps(original, indent=2))

            handler.write_version(path, "1.1.0")

            result = json.loads(path.read_text())
            assert result["version"] == "1.1.0"
            assert result["name"] == "test-package"
            assert result["dependencies"]["lodash"] == "^4.0.0"

    def test_write_version_adds_newline(self):
        """Test that write_version adds trailing newline."""
        handler = JsonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "package.json"
            path.write_text(json.dumps({"version": "1.0.0"}))

            handler.write_version(path, "1.1.0")

            content = path.read_text()
            assert content.endswith("\n")


class TestTomlVersionHandler:
    """Tests for TomlVersionHandler."""

    def test_can_handle_toml_file(self):
        """Test handler recognizes TOML files."""
        handler = TomlVersionHandler()
        assert handler.can_handle(Path("pyproject.toml"))
        assert handler.can_handle(Path("config.toml"))
        assert handler.can_handle(Path("/path/to/file.toml"))

    def test_cannot_handle_non_toml_file(self):
        """Test handler rejects non-TOML files."""
        handler = TomlVersionHandler()
        assert not handler.can_handle(Path("package.json"))
        assert not handler.can_handle(Path("version.py"))

    def test_read_version_pep621(self):
        """Test reading version from [project] section (PEP 621)."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[project]
name = "test"
version = "2.0.0"
description = "A test package"
"""
            path.write_text(content)
            assert handler.read_version(path) == "2.0.0"

    def test_read_version_poetry(self):
        """Test reading version from [tool.poetry] section."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[tool.poetry]
name = "test"
version = "3.1.4"
description = "A test package"
"""
            path.write_text(content)
            assert handler.read_version(path) == "3.1.4"

    def test_read_version_pep621_priority_over_poetry(self):
        """Test that [project] takes priority over [tool.poetry]."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[project]
version = "1.0.0"

[tool.poetry]
version = "2.0.0"
"""
            path.write_text(content)
            # Should return project.version, not tool.poetry.version
            assert handler.read_version(path) == "1.0.0"

    def test_read_version_missing(self):
        """Test error when no version found in TOML."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[build-system]
requires = ["setuptools"]
"""
            path.write_text(content)
            with pytest.raises(ValueError, match="No version field found"):
                handler.read_version(path)

    def test_write_version_pep621(self):
        """Test updating version in [project] section."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[project]
name = "test"
version = "1.0.0"
description = "A test package"

[build-system]
requires = ["hatchling"]
"""
            path.write_text(content)

            handler.write_version(path, "1.1.0")

            result = path.read_text()
            assert 'version = "1.1.0"' in result
            assert "name = \"test\"" in result
            assert "[build-system]" in result

    def test_write_version_poetry(self):
        """Test updating version in [tool.poetry] section."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[tool.poetry]
name = "test"
version = "1.0.0"
description = "A test package"
"""
            path.write_text(content)

            handler.write_version(path, "2.0.0")

            result = path.read_text()
            assert 'version = "2.0.0"' in result
            assert "name = \"test\"" in result

    def test_write_version_preserves_comments(self):
        """Test that TOML comments are preserved."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """# This is a comment
[project]
name = "test"
version = "1.0.0"  # inline comment
description = "A test package"

# Another comment
[build-system]
requires = ["hatchling"]
"""
            path.write_text(content)

            handler.write_version(path, "1.1.0")

            result = path.read_text()
            assert "# This is a comment" in result
            assert "# inline comment" in result
            assert "# Another comment" in result

    def test_write_version_not_found(self):
        """Test error when version field cannot be found for update."""
        handler = TomlVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pyproject.toml"
            content = """[project]
name = "test"
"""
            path.write_text(content)
            with pytest.raises(ValueError, match="Could not find version field"):
                handler.write_version(path, "1.0.0")


class TestPythonVersionHandler:
    """Tests for PythonVersionHandler."""

    def test_can_handle_python_file(self):
        """Test handler recognizes Python files."""
        handler = PythonVersionHandler()
        assert handler.can_handle(Path("version.py"))
        assert handler.can_handle(Path("__init__.py"))
        assert handler.can_handle(Path("/path/to/file.py"))

    def test_cannot_handle_non_python_file(self):
        """Test handler rejects non-Python files."""
        handler = PythonVersionHandler()
        assert not handler.can_handle(Path("package.json"))
        assert not handler.can_handle(Path("pyproject.toml"))

    def test_read_version_double_quoted(self):
        """Test reading __version__ with double quotes."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text('__version__ = "1.2.3"\n')
            assert handler.read_version(path) == "1.2.3"

    def test_read_version_single_quoted(self):
        """Test reading __version__ with single quotes."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text("__version__ = '1.2.3'\n")
            assert handler.read_version(path) == "1.2.3"

    def test_read_version_variable(self):
        """Test reading version variable."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text('version = "2.0.0"\n')
            assert handler.read_version(path) == "2.0.0"

    def test_read_version_uppercase(self):
        """Test reading VERSION variable."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text('VERSION = "3.0.0"\n')
            assert handler.read_version(path) == "3.0.0"

    def test_read_version_preference_order(self):
        """Test that __version__ is preferred over version."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text('__version__ = "1.0.0"\nversion = "2.0.0"\n')
            # Should return __version__, not version
            assert handler.read_version(path) == "1.0.0"

    def test_read_version_missing(self):
        """Test error when no version variable found."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text('SOME_VAR = "value"\n')
            with pytest.raises(ValueError, match="No version variable found"):
                handler.read_version(path)

    def test_write_version_double_quoted(self):
        """Test updating double-quoted version."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text('__version__ = "1.0.0"\n')

            handler.write_version(path, "1.1.0")

            result = path.read_text()
            assert '__version__ = "1.1.0"' in result

    def test_write_version_single_quoted(self):
        """Test updating single-quoted version."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            path.write_text("__version__ = '1.0.0'\n")

            handler.write_version(path, "1.1.0")

            result = path.read_text()
            assert "__version__ = '1.1.0'" in result or '__version__ = "1.1.0"' in result

    def test_write_version_preserves_structure(self):
        """Test that write_version preserves file structure."""
        handler = PythonVersionHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "version.py"
            content = '''"""Version module."""

__version__ = "1.0.0"

SOME_OTHER_VAR = "value"

def get_version():
    return __version__
'''
            path.write_text(content)

            handler.write_version(path, "2.0.0")

            result = path.read_text()
            assert '__version__ = "2.0.0"' in result
            assert '"""Version module."""' in result
            assert 'SOME_OTHER_VAR = "value"' in result
            assert "def get_version():" in result


class TestSetupPyHandler:
    """Tests for SetupPyHandler."""

    def test_can_handle_setup_py(self):
        """Test handler recognizes setup.py."""
        handler = SetupPyHandler()
        assert handler.can_handle(Path("setup.py"))
        assert handler.can_handle(Path("/path/to/setup.py"))

    def test_cannot_handle_non_setup_py(self):
        """Test handler rejects non-setup.py files."""
        handler = SetupPyHandler()
        assert not handler.can_handle(Path("version.py"))
        assert not handler.can_handle(Path("package.json"))
        assert not handler.can_handle(Path("setup.cfg"))

    def test_read_version_simple(self):
        """Test reading version from simple setup.py."""
        handler = SetupPyHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setup.py"
            path.write_text('from setuptools import setup\n\nsetup(version="1.0.0")\n')
            assert handler.read_version(path) == "1.0.0"

    def test_read_version_with_other_args(self):
        """Test reading version when setup has other arguments."""
        handler = SetupPyHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setup.py"
            content = '''from setuptools import setup

setup(
    name="test",
    version="2.3.4",
    description="A test package",
)
'''
            path.write_text(content)
            assert handler.read_version(path) == "2.3.4"

    def test_read_version_multiline(self):
        """Test reading version in multiline setup call."""
        handler = SetupPyHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setup.py"
            content = '''from setuptools import setup

setup(
    name="test",
    version="1.2.3",
    packages=["test"],
)
'''
            path.write_text(content)
            assert handler.read_version(path) == "1.2.3"

    def test_read_version_missing(self):
        """Test error when no version in setup.py."""
        handler = SetupPyHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setup.py"
            path.write_text('from setuptools import setup\n\nsetup(name="test")\n')
            with pytest.raises(ValueError, match="No version argument found"):
                handler.read_version(path)

    def test_write_version_simple(self):
        """Test updating version in simple setup.py."""
        handler = SetupPyHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setup.py"
            path.write_text('from setuptools import setup\n\nsetup(version="1.0.0")\n')

            handler.write_version(path, "1.1.0")

            result = path.read_text()
            assert 'version="1.1.0"' in result

    def test_write_version_preserves_structure(self):
        """Test that write_version preserves setup.py structure."""
        handler = SetupPyHandler()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "setup.py"
            content = '''"""Setup script."""
from setuptools import setup, find_packages

with open("README.md") as f:
    readme = f.read()

setup(
    name="test-package",
    version="1.0.0",
    description="A test package",
    author="Test Author",
    packages=find_packages(),
)
'''
            path.write_text(content)

            handler.write_version(path, "2.0.0")

            result = path.read_text()
            assert 'version="2.0.0"' in result or 'version = "2.0.0"' in result
            assert 'name="test-package"' in result or 'name = "test-package"' in result
            assert "README.md" in result
            assert "find_packages" in result


class TestVersionBumper:
    """Tests for VersionBumper class."""

    def test_parse_version_simple(self):
        """Test parsing simple version string."""
        assert VersionBumper.parse_version("1.2.3") == (1, 2, 3)
        assert VersionBumper.parse_version("0.0.1") == (0, 0, 1)
        assert VersionBumper.parse_version("10.20.30") == (10, 20, 30)

    def test_parse_version_with_v_prefix(self):
        """Test parsing version with v/V prefix."""
        assert VersionBumper.parse_version("v1.2.3") == (1, 2, 3)
        assert VersionBumper.parse_version("V1.0.0") == (1, 0, 0)

    def test_parse_version_with_prerelease(self):
        """Test parsing version with prerelease suffix."""
        assert VersionBumper.parse_version("1.0.0-alpha") == (1, 0, 0)
        assert VersionBumper.parse_version("2.0.0-beta.1") == (2, 0, 0)

    def test_parse_version_two_components(self):
        """Test parsing version with only major.minor."""
        assert VersionBumper.parse_version("1.2") == (1, 2, 0)

    def test_parse_version_invalid(self):
        """Test error on invalid version format."""
        with pytest.raises(ValueError):
            VersionBumper.parse_version("invalid")
        with pytest.raises(ValueError):
            VersionBumper.parse_version("1")

    def test_bump_version_string_major(self):
        """Test bumping major version."""
        assert VersionBumper.bump_version_string("1.2.3", BumpType.MAJOR) == "2.0.0"
        assert VersionBumper.bump_version_string("0.1.0", BumpType.MAJOR) == "1.0.0"

    def test_bump_version_string_minor(self):
        """Test bumping minor version."""
        assert VersionBumper.bump_version_string("1.2.3", BumpType.MINOR) == "1.3.0"
        assert VersionBumper.bump_version_string("0.0.1", BumpType.MINOR) == "0.1.0"

    def test_bump_version_string_patch(self):
        """Test bumping patch version."""
        assert VersionBumper.bump_version_string("1.2.3", BumpType.PATCH) == "1.2.4"
        assert VersionBumper.bump_version_string("0.0.0", BumpType.PATCH) == "0.0.1"

    def test_detect_version_file_priority(self):
        """Test version file detection priority."""
        config = VersionConfig()
        bumper = VersionBumper(config)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Create package.json - should be detected first
            (root / "package.json").write_text('{"version": "1.0.0"}')
            assert bumper.detect_version_file(root).name == "package.json"

    def test_detect_version_file_pyproject(self):
        """Test detection of pyproject.toml."""
        config = VersionConfig()
        bumper = VersionBumper(config)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            (root / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
            detected = bumper.detect_version_file(root)
            assert detected is not None
            assert detected.name == "pyproject.toml"

    def test_detect_version_file_configured_path(self):
        """Test detection with configured file path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Create custom version file
            version_file = root / "custom_version.py"
            version_file.write_text('__version__ = "3.0.0"\n')

            config = VersionConfig(file_path=str(version_file))
            bumper = VersionBumper(config)

            detected = bumper.detect_version_file(root)
            assert detected == version_file

    def test_bump_version_with_bump_type(self):
        """Test bumping with explicit bump type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            package_json = root / "package.json"
            package_json.write_text('{"name": "test", "version": "1.2.3"}')

            config = VersionConfig()
            bumper = VersionBumper(config)

            new_version = bumper.bump_version(
                path=package_json, bump_type=BumpType.MAJOR
            )
            assert new_version == "2.0.0"

    def test_bump_version_creates_backup(self):
        """Test that bump_version creates backup for rollback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            package_json = root / "package.json"
            package_json.write_text('{"name": "test", "version": "1.0.0"}')

            config = VersionConfig()
            bumper = VersionBumper(config)

            bumper.bump_version(path=package_json, bump_type=BumpType.PATCH)

            # After bump, backup should exist
            assert bumper._backup_path == package_json
            assert bumper._backup_version == "1.0.0"

    def test_rollback_restores_version(self):
        """Test rollback restores original version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            package_json = root / "package.json"
            package_json.write_text('{"name": "test", "version": "1.0.0"}')

            config = VersionConfig()
            bumper = VersionBumper(config)

            bumper.bump_version(path=package_json, bump_type=BumpType.PATCH)
            assert json.loads(package_json.read_text())["version"] == "1.0.1"

            bumper.rollback()
            assert json.loads(package_json.read_text())["version"] == "1.0.0"

    def test_rollback_without_backup(self):
        """Test rollback is safe when no backup exists."""
        config = VersionConfig()
        bumper = VersionBumper(config)

        # Should not raise
        bumper.rollback()

    def test_clear_backup(self):
        """Test clearing backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            package_json = root / "package.json"
            package_json.write_text('{"name": "test", "version": "1.0.0"}')

            config = VersionConfig()
            bumper = VersionBumper(config)

            bumper.bump_version(path=package_json, bump_type=BumpType.PATCH)
            assert bumper._backup_version is not None

            bumper.clear_backup()
            assert bumper._backup_version is None
            assert bumper._backup_path is None

    def test_bump_version_disabled(self):
        """Test error when bumping is disabled."""
        config = VersionConfig(enabled=False)
        bumper = VersionBumper(config)

        with pytest.raises(RuntimeError, match="Version bumping is disabled"):
            bumper.bump_version(bump_type=BumpType.PATCH)

class TestVersionConfig:
    """Tests for VersionConfig."""

    def test_validate_nonexistent_file(self):
        """Test validation catches nonexistent file."""
        config = VersionConfig(file_path="/nonexistent/path/version.py")
        errors = config.validate()
        assert any("Version file not found" in e for e in errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
