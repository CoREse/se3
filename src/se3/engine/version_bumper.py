"""Version bumping functionality for SE3 commit step.

Provides automatic version detection and bumping for various project types.
Supports package.json, pyproject.toml, version.py, and setup.py files.
"""

from __future__ import annotations

import abc
import ast
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Type, Tuple, Union

logger = logging.getLogger(__name__)


class ProjectType(Enum):
    """Enumeration of supported project types for version detection."""

    PYTHON = "python"
    NODEJS = "nodejs"
    UNKNOWN = "unknown"


class BumpType(Enum):
    """Types of version bumps following semantic versioning."""

    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"


class TaskType(Enum):
    """Task types that determine bump rules."""

    FEATURE = "feature"
    BUGFIX = "bugfix"
    BREAKING = "breaking"


@dataclass(frozen=True)
class Version:
    """Semantic Version data class following SemVer 2.0.0 spec.

    Supports format: MAJOR.MINOR.PATCH[-prerelease][+build]

    Attributes:
        major: Major version number (incompatible API changes)
        minor: Minor version number (backward-compatible functionality)
        patch: Patch version number (backward-compatible bug fixes)
        prerelease: Optional prerelease identifier (e.g., "alpha", "beta.1")
        build: Optional build metadata (e.g., "20240101", "sha.5114f85")
    """

    major: int
    minor: int
    patch: int
    prerelease: Optional[str] = None
    build: Optional[str] = None

    # Pattern for parsing SemVer strings
    # Follows Semantic Versioning 2.0.0 specification
    _SEMVER_PATTERN = re.compile(
        r'^(?:[vV])?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)'
        r'(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)'
        r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?'
        r'(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
    )

    @classmethod
    def parse(cls, version_str: str) -> "Version":
        """Parse a semantic version string following SemVer 2.0.0.

        Args:
            version_str: Version string in SemVer format

        Returns:
            Version instance

        Raises:
            ValueError: If version format is invalid
        """
        if not version_str:
            raise ValueError("Version string cannot be empty")

        version_str = version_str.strip()
        match = cls._SEMVER_PATTERN.match(version_str)
        if not match:
            raise ValueError(
                f"Invalid SemVer format: '{version_str}'. "
                "Expected format: MAJOR.MINOR.PATCH[-prerelease][+build]"
            )

        groups = match.groupdict()
        return cls(
            major=int(groups["major"]),
            minor=int(groups["minor"]),
            patch=int(groups["patch"]),
            prerelease=groups.get("prerelease"),
            build=groups.get("build"),
        )

    @classmethod
    def validate(cls, version_str: str) -> bool:
        """Validate if a string is a valid SemVer 2.0.0 version.

        Args:
            version_str: Version string to validate

        Returns:
            True if valid SemVer, False otherwise
        """
        try:
            cls.parse(version_str)
            return True
        except ValueError:
            return False

    def __str__(self) -> str:
        """Convert version to string representation."""
        version = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            version += f"-{self.prerelease}"
        if self.build:
            version += f"+{self.build}"
        return version

    def bump_major(self) -> "Version":
        """Bump major version, resetting minor, patch, and prerelease.

        Per SemVer 2.0.0, bumping any version resets prerelease and build metadata.

        Returns:
            New Version with bumped major number
        """
        return Version(
            major=self.major + 1,
            minor=0,
            patch=0,
        )

    def bump_minor(self) -> "Version":
        """Bump minor version, resetting patch and prerelease.

        Per SemVer 2.0.0, bumping any version resets prerelease and build metadata.

        Returns:
            New Version with bumped minor number
        """
        return Version(
            major=self.major,
            minor=self.minor + 1,
            patch=0,
        )

    def bump_patch(self) -> "Version":
        """Bump patch version, resetting prerelease.

        Per SemVer 2.0.0, bumping any version resets prerelease and build metadata.

        Returns:
            New Version with bumped patch number
        """
        return Version(
            major=self.major,
            minor=self.minor,
            patch=self.patch + 1,
        )

    def with_prerelease(self, prerelease: Optional[str]) -> "Version":
        """Create a new version with the specified prerelease identifier.

        Args:
            prerelease: Prerelease identifier (e.g., "alpha", "beta.1")

        Returns:
            New Version with the prerelease identifier
        """
        return Version(
            major=self.major,
            minor=self.minor,
            patch=self.patch,
            prerelease=prerelease,
            build=self.build,
        )

    def with_build(self, build: Optional[str]) -> "Version":
        """Create a new version with the specified build metadata.

        Per SemVer 2.0.0, build metadata should be ignored in precedence.

        Args:
            build: Build metadata (e.g., "20240101", "sha.5114f85")

        Returns:
            New Version with the build metadata
        """
        return Version(
            major=self.major,
            minor=self.minor,
            patch=self.patch,
            prerelease=self.prerelease,
            build=build,
        )

    def bump(self, bump_type: BumpType) -> "Version":
        """Bump version according to the specified bump type.

        Args:
            bump_type: Type of bump to apply

        Returns:
            New Version with applied bump
        """
        if bump_type == BumpType.MAJOR:
            return self.bump_major()
        elif bump_type == BumpType.MINOR:
            return self.bump_minor()
        else:  # PATCH
            return self.bump_patch()

    def __eq__(self, other: object) -> bool:
        """Check equality with another Version."""
        if not isinstance(other, Version):
            return NotImplemented
        # For comparison, ignore build metadata per SemVer spec
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.prerelease == other.prerelease
        )

    def __hash__(self) -> int:
        """Hash based on version components (excluding build metadata)."""
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __lt__(self, other: "Version") -> bool:
        """Compare versions for ordering following SemVer 2.0.0 precedence rules."""
        if not isinstance(other, Version):
            return NotImplemented

        # Compare major, minor, patch
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

        # A version without prerelease has higher precedence
        if self.prerelease is None and other.prerelease is not None:
            return False
        if self.prerelease is not None and other.prerelease is None:
            return True

        # Compare prerelease identifiers
        if self.prerelease and other.prerelease:
            self_parts = self.prerelease.split('.')
            other_parts = other.prerelease.split('.')

            for s, o in zip(self_parts, other_parts):
                # Numeric identifiers are compared as integers
                s_is_num = s.isdigit()
                o_is_num = o.isdigit()

                if s_is_num and o_is_num:
                    s_int, o_int = int(s), int(o)
                    if s_int != o_int:
                        return s_int < o_int
                elif s_is_num:
                    # Numeric identifiers have lower precedence than alphanumeric
                    return True
                elif o_is_num:
                    return False
                else:
                    # Alphanumeric comparison
                    if s != o:
                        return s < o

            # Shorter prerelease has lower precedence if all preceding equal
            return len(self_parts) < len(other_parts)

        return False

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        return not self <= other

    def __ge__(self, other: "Version") -> bool:
        return not self < other


@dataclass
class VersionInfo:
    """Structured version information for a project.

    Attributes:
        project_type: Detected project type (Python, Node.js, etc.)
        version_file: Path to the file containing the version
        current_version: The current version string
        version: Parsed Version object
    """

    project_type: ProjectType
    version_file: Path
    current_version: str
    version: Version


class VersionFileHandler(abc.ABC):
    """Abstract base class for version file handlers.

    Implementations handle reading and writing version strings for
    specific file formats like JSON, TOML, or Python files.
    """

    @abc.abstractmethod
    def can_handle(self, path: Path) -> bool:
        """Check if this handler can process the given file.

        Args:
            path: Path to the version file

        Returns:
            True if this handler supports the file format
        """
        pass

    @abc.abstractmethod
    def read_version(self, path: Path) -> str:
        """Read the version string from the file.

        Args:
            path: Path to the version file

        Returns:
            The version string

        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the version cannot be parsed
        """
        pass

    @abc.abstractmethod
    def write_version(self, path: Path, new_version: str) -> None:
        """Write the version string to the file.

        Args:
            path: Path to the version file
            new_version: The new version string to write

        Raises:
            FileNotFoundError: If the file doesn't exist
            PermissionError: If the file cannot be written
            ValueError: If the version format is invalid
        """
        pass


class JsonVersionHandler(VersionFileHandler):
    """Handler for JSON-based version files (e.g., package.json)."""

    def can_handle(self, path: Path) -> bool:
        return path.suffix == ".json" or path.name == "package.json"

    def read_version(self, path: Path) -> str:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        version = data.get("version")
        if not version:
            raise ValueError(f"No 'version' field found in {path}")
        return str(version)

    def write_version(self, path: Path, new_version: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["version"] = new_version
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")


class TomlVersionHandler(VersionFileHandler):
    """Handler for TOML-based version files (e.g., pyproject.toml)."""

    def can_handle(self, path: Path) -> bool:
        return path.suffix == ".toml" or path.name == "pyproject.toml"

    def read_version(self, path: Path) -> str:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore

        with open(path, "rb") as f:
            data = tomllib.load(f)

        # Try project.version first (PEP 621)
        version = data.get("project", {}).get("version")
        if version:
            return str(version)

        # Try tool.poetry.version
        version = data.get("tool", {}).get("poetry", {}).get("version")
        if version:
            return str(version)

        raise ValueError(f"No version field found in {path}")

    def write_version(self, path: Path, new_version: str) -> None:
        content = path.read_text(encoding="utf-8")

        # Try to replace project.version (PEP 621)
        # Match version within [project] section, preserving quotes and spacing
        pattern = r'(\[project\][^\[]*version\s*=\s*["\'])([^"\']+)(["\'])'
        match = re.search(pattern, content, re.DOTALL)

        if match:
            new_content = (
                content[: match.start(2)]
                + new_version
                + content[match.end(2) :]
            )
            path.write_text(new_content, encoding="utf-8")
            return

        # Try to replace tool.poetry.version
        pattern = r'(\[tool\.poetry\][^\[]*version\s*=\s*["\'])([^"\']+)(["\'])'
        match = re.search(pattern, content, re.DOTALL)

        if match:
            new_content = (
                content[: match.start(2)]
                + new_version
                + content[match.end(2) :]
            )
            path.write_text(new_content, encoding="utf-8")
            return

        raise ValueError(f"Could not find version field to update in {path}")


class PythonVersionHandler(VersionFileHandler):
    """Handler for Python files containing __version__ or version variables."""

    def can_handle(self, path: Path) -> bool:
        return path.suffix == ".py"

    def read_version(self, path: Path) -> str:
        content = path.read_text(encoding="utf-8")

        # Try __version__ first
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)

        # Try version = "x.y.z"
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if match:
            return match.group(1)

        # Try VERSION = "x.y.z"
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if match:
            return match.group(1)

        raise ValueError(f"No version variable found in {path}")

    def write_version(self, path: Path, new_version: str) -> None:
        content = path.read_text(encoding="utf-8")

        # Try __version__ first
        pattern = r'(__version__\s*=\s*["\'])([^"\']+)(["\'])'
        if re.search(pattern, content):
            new_content = re.sub(pattern, rf"\g<1>{new_version}\g<3>", content)
            path.write_text(new_content, encoding="utf-8")
            return

        # Try version = "x.y.z"
        pattern = r'^(version\s*=\s*["\'])([^"\']+)(["\'])'
        if re.search(pattern, content, re.MULTILINE):
            new_content = re.sub(pattern, rf"\g<1>{new_version}\g<3>", content, flags=re.MULTILINE)
            path.write_text(new_content, encoding="utf-8")
            return

        # Try VERSION = "x.y.z"
        pattern = r'^(VERSION\s*=\s*["\'])([^"\']+)(["\'])'
        if re.search(pattern, content, re.MULTILINE):
            new_content = re.sub(pattern, rf"\g<1>{new_version}\g<3>", content, flags=re.MULTILINE)
            path.write_text(new_content, encoding="utf-8")
            return

        raise ValueError(f"Could not find version variable to update in {path}")


class SetupPyHandler(VersionFileHandler):
    """Handler for setup.py files using AST-based parsing."""

    def can_handle(self, path: Path) -> bool:
        return path.name == "setup.py"

    def read_version(self, path: Path) -> str:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content)

        # Walk the AST to find setup() call
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check if this is a setup() call
                func_name = self._get_func_name(node.func)
                if func_name == "setup":
                    # Look for version keyword argument
                    for keyword in node.keywords:
                        if keyword.arg == "version":
                            if isinstance(keyword.value, ast.Constant):
                                return str(keyword.value.value)
                            elif isinstance(keyword.value, ast.Str):  # Python < 3.8
                                return keyword.value.s

        raise ValueError(f"No version argument found in setup() call in {path}")

    def _get_func_name(self, func_node) -> Optional[str]:
        """Extract function name from AST node."""
        if isinstance(func_node, ast.Name):
            return func_node.id
        elif isinstance(func_node, ast.Attribute):
            return func_node.attr
        return None

    def write_version(self, path: Path, new_version: str) -> None:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content)

        # Find the setup() call and the version keyword
        version_pos = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_func_name(node.func)
                if func_name == "setup":
                    for keyword in node.keywords:
                        if keyword.arg == "version":
                            version_pos = (keyword.value.lineno, keyword.value.col_offset, keyword.value.end_lineno, keyword.value.end_col_offset)
                            break
                    if version_pos:
                        break

        if version_pos is None:
            raise ValueError(f"Could not find version argument to update in setup() call in {path}")

        # Replace the version string in the original content
        lines = content.split('\n')
        start_line, start_col, end_line, end_col = version_pos

        # Adjust for 1-indexed line numbers
        start_line -= 1
        end_line -= 1

        # Extract the version string with its quotes
        if start_line == end_line:
            original_line = lines[start_line]
            before = original_line[:start_col]
            after = original_line[end_col:]
            # Preserve the original quote style
            quote_match = re.search(r'(["\'])', original_line[start_col:])
            if quote_match:
                quote = quote_match.group(1)
                lines[start_line] = before + quote + new_version + quote + after
            else:
                lines[start_line] = before + '"' + new_version + '"' + after
        else:
            # Multi-line string - use regex fallback
            pattern = r'(setup\([^)]*version\s*=\s*["\'])([^"\']+)(["\'])'
            new_content = re.sub(pattern, rf"\g<1>{new_version}\g<3>", content, flags=re.DOTALL)
            path.write_text(new_content, encoding="utf-8")
            return

        path.write_text('\n'.join(lines), encoding="utf-8")


class VersionDetector:
    """Detects project type and version file location.

    Automatically detects Python and Node.js projects by looking for
    characteristic files (pyproject.toml, package.json) and extracts
    version information.

    Example:
        detector = VersionDetector()
        info = detector.detect(Path("/path/to/project"))
        print(f"Project type: {info.project_type.value}")
        print(f"Version: {info.current_version}")
        print(f"Version file: {info.version_file}")
    """

    # Detection priority order for version files
    VERSION_FILES = [
        ("pyproject.toml", ProjectType.PYTHON),
        ("package.json", ProjectType.NODEJS),
        ("setup.py", ProjectType.PYTHON),
        ("version.py", ProjectType.PYTHON),
        ("src/__init__.py", ProjectType.PYTHON),
        ("src/version.py", ProjectType.PYTHON),
    ]

    def __init__(self):
        """Initialize the version detector."""
        self._handlers: List[VersionFileHandler] = [
            TomlVersionHandler(),
            JsonVersionHandler(),
            PythonVersionHandler(),
            SetupPyHandler(),
        ]

    def detect_project_type(self, project_root: Union[Path, str]) -> ProjectType:
        """Detect the project type from project files.

        Checks for characteristic files to determine if this is a
        Python or Node.js project.

        Args:
            project_root: Root directory of the project

        Returns:
            Detected ProjectType (PYTHON, NODEJS, or UNKNOWN)
        """
        if isinstance(project_root, str):
            project_root = Path(project_root)

        # Check for Python project markers
        python_markers = [
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
        ]
        for marker in python_markers:
            if (project_root / marker).exists():
                return ProjectType.PYTHON

        # Check for Node.js project markers
        nodejs_markers = [
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "node_modules",
        ]
        for marker in nodejs_markers:
            if (project_root / marker).exists():
                return ProjectType.NODEJS

        return ProjectType.UNKNOWN

    def get_version_file_path(self, project_root: Union[Path, str]) -> Optional[Path]:
        """Get the path to the version file.

        Checks common version file locations in priority order.

        Args:
            project_root: Root directory of the project

        Returns:
            Path to the version file, or None if not found
        """
        if isinstance(project_root, str):
            project_root = Path(project_root)

        for filename, _ in self.VERSION_FILES:
            path = project_root / filename
            if path.exists():
                # Verify a handler can read this file
                for handler in self._handlers:
                    if handler.can_handle(path):
                        return path

        return None

    def read_version(self, path: Path) -> str:
        """Read the version string from a file.

        Args:
            path: Path to the version file

        Returns:
            The version string

        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the version cannot be read
        """
        for handler in self._handlers:
            if handler.can_handle(path):
                return handler.read_version(path)

        raise ValueError(f"No handler available for file: {path}")

    def detect(self, project_root: Union[Path, str]) -> VersionInfo:
        """Detect project type and version information.

        This is the main entry point for version detection. It detects
        the project type, locates the version file, and reads the current
        version.

        Args:
            project_root: Root directory of the project

        Returns:
            VersionInfo containing project type, version file path, and version

        Raises:
            FileNotFoundError: If no version file is found
            ValueError: If version cannot be read or parsed
        """
        if isinstance(project_root, str):
            project_root = Path(project_root)

        # Detect project type
        project_type = self.detect_project_type(project_root)

        # Find version file
        version_file = self.get_version_file_path(project_root)
        if version_file is None:
            raise FileNotFoundError(
                f"No version file found in {project_root}. "
                "Expected one of: " + ", ".join(f for f, _ in self.VERSION_FILES)
            )

        # Read and parse version
        version_str = self.read_version(version_file)
        version = Version.parse(version_str)

        return VersionInfo(
            project_type=project_type,
            version_file=version_file,
            current_version=version_str,
            version=version,
        )


@dataclass
class VersionConfig:
    """Configuration for version bumping.

    Attributes:
        enabled: Whether automatic version bumping is enabled
        file_path: Optional explicit path to version file (None = auto-detect)
        bump_rules: Mapping of task types to bump types
        include_in_commit_message: Whether to include version in commit message
    """

    enabled: bool = True
    file_path: Optional[str] = None
    bump_rules: Dict[TaskType, BumpType] = None  # type: ignore
    include_in_commit_message: bool = True

    def __post_init__(self):
        if self.bump_rules is None:
            self.bump_rules = {
                TaskType.FEATURE: BumpType.MINOR,
                TaskType.BUGFIX: BumpType.PATCH,
                TaskType.BREAKING: BumpType.MAJOR,
            }

    def validate(self) -> List[str]:
        """Validate the configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if self.file_path:
            path = Path(self.file_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.exists():
                errors.append(f"Version file not found: {self.file_path}")

        for task_type in TaskType:
            if task_type not in self.bump_rules:
                errors.append(f"Missing bump rule for task type: {task_type.value}")

        return errors


class VersionBumper:
    """Main class for version bumping operations.

    Handles detection of version files, reading/writing versions,
    and atomic bump operations with rollback support.

    Supports Semantic Versioning 2.0.0 with major, minor, patch bumps,
    as well as pre-release and build metadata handling.
    """

    # Priority order for version file detection
    DEFAULT_VERSION_FILES = [
        "package.json",
        "pyproject.toml",
        "version.py",
        "setup.py",
        "src/__init__.py",
        "src/version.py",
    ]

    # Task type to bump type mapping
    DEFAULT_BUMP_RULES: Dict[TaskType, BumpType] = {
        TaskType.FEATURE: BumpType.MINOR,
        TaskType.BUGFIX: BumpType.PATCH,
        TaskType.BREAKING: BumpType.MAJOR,
    }

    def __init__(self, config: VersionConfig):
        """Initialize the version bumper with configuration.

        Args:
            config: Version bumping configuration
        """
        self.config = config
        self._handlers: List[VersionFileHandler] = [
            JsonVersionHandler(),
            TomlVersionHandler(),
            PythonVersionHandler(),
            SetupPyHandler(),
        ]
        self._backup_version: Optional[str] = None
        self._backup_path: Optional[Path] = None
        self._detector = VersionDetector()

    def register_handler(self, handler: VersionFileHandler) -> None:
        """Register a custom version file handler.

        Args:
            handler: Handler instance to register
        """
        self._handlers.append(handler)

    def detect_version_file(self, project_root: Optional[Path] = None) -> Optional[Path]:
        """Auto-detect the version file in the project.

        Checks for common version files in priority order.

        Args:
            project_root: Root directory of the project (default: cwd)

        Returns:
            Path to the version file, or None if not found
        """
        if project_root is None:
            project_root = Path.cwd()

        # If explicit path is configured, use it
        if self.config.file_path:
            path = Path(self.config.file_path)
            if path.is_absolute():
                return path if path.exists() else None
            return project_root / path if (project_root / path).exists() else None

        # Use detector for auto-detection
        return self._detector.get_version_file_path(project_root)

    def detect_project_type(self, project_root: Optional[Path] = None) -> ProjectType:
        """Detect the project type.

        Args:
            project_root: Root directory of the project (default: cwd)

        Returns:
            Detected project type
        """
        if project_root is None:
            project_root = Path.cwd()

        return self._detector.detect_project_type(project_root)

    def _get_handler(self, path: Path) -> VersionFileHandler:
        """Get the appropriate handler for a file.

        Args:
            path: Path to the version file

        Returns:
            Handler that can process the file

        Raises:
            ValueError: If no handler supports the file
        """
        for handler in self._handlers:
            if handler.can_handle(path):
                return handler
        raise ValueError(f"No handler available for file: {path}")

    def read_version(self, path: Optional[Path] = None) -> str:
        """Read the current version from a file.

        Args:
            path: Path to version file (None = auto-detect)

        Returns:
            Current version string

        Raises:
            FileNotFoundError: If version file not found
            ValueError: If version cannot be read
        """
        if path is None:
            path = self.detect_version_file()
            if path is None:
                raise FileNotFoundError("No version file found")

        handler = self._get_handler(path)
        return handler.read_version(path)

    @staticmethod
    def parse_version(version: str) -> Version:
        """Parse a semantic version string following SemVer 2.0.0.

        Args:
            version: Version string (e.g., "1.2.3", "1.2.3-alpha", "1.2.3+build.1")

        Returns:
            Parsed Version object

        Raises:
            ValueError: If version format is invalid
        """
        return Version.parse(version)

    @staticmethod
    def validate_semver(version: str) -> bool:
        """Validate if a string is a valid SemVer 2.0.0 version.

        Args:
            version: Version string to validate

        Returns:
            True if valid SemVer, False otherwise
        """
        return Version.validate(version)

    @staticmethod
    def bump_version_string(version: str, bump_type: BumpType) -> str:
        """Calculate the new version after bumping.

        Args:
            version: Current version string
            bump_type: Type of bump to apply

        Returns:
            New version string
        """
        v = Version.parse(version)
        new_v = v.bump(bump_type)
        return str(new_v)

    @staticmethod
    def get_bump_type_from_task(task_type: TaskType, rules: Optional[Dict[TaskType, BumpType]] = None) -> BumpType:
        """Get the bump type for a given task type.

        Args:
            task_type: The type of task being performed
            rules: Optional custom mapping rules (uses DEFAULT_BUMP_RULES if not provided)

        Returns:
            The bump type to apply
        """
        if rules is None:
            rules = VersionBumper.DEFAULT_BUMP_RULES
        return rules.get(task_type, BumpType.PATCH)

    def bump_version(
        self,
        bump_type: Optional[BumpType] = None,
        task_type: Optional[TaskType] = None,
        path: Optional[Path] = None,
    ) -> str:
        """Bump the version in the specified file.

        Creates a backup before bumping to allow rollback on failure.

        Args:
            bump_type: Type of bump to apply (overrides task_type)
            task_type: Task type to determine bump type from config
            path: Path to version file (None = auto-detect)

        Returns:
            New version string

        Raises:
            FileNotFoundError: If version file not found
            ValueError: If bump cannot be performed
            RuntimeError: If version bumping is disabled
        """
        if not self.config.enabled:
            raise RuntimeError("Version bumping is disabled")

        # Determine bump type
        if bump_type is None:
            if task_type is None:
                raise ValueError("Either bump_type or task_type must be provided")
            bump_type = self.config.bump_rules.get(task_type, BumpType.PATCH)

        # Find version file
        if path is None:
            path = self.detect_version_file()
            if path is None:
                raise FileNotFoundError("No version file found")

        # Read current version and create backup
        handler = self._get_handler(path)
        current_version = handler.read_version(path)
        self._backup_version = current_version
        self._backup_path = path

        # Calculate and write new version
        new_version = self.bump_version_string(current_version, bump_type)
        handler.write_version(path, new_version)

        logger.info(f"Bumped version: {current_version} -> {new_version}")
        return new_version

    def save_original_version(self, path: Optional[Path] = None) -> str:
        """Save the current version for potential rollback.

        Args:
            path: Path to version file (None = auto-detect)

        Returns:
            The original version string that was saved

        Raises:
            FileNotFoundError: If version file not found
        """
        if path is None:
            path = self.detect_version_file()
            if path is None:
                raise FileNotFoundError("No version file found")

        handler = self._get_handler(path)
        current_version = handler.read_version(path)
        self._backup_version = current_version
        self._backup_path = path
        return current_version

    def rollback(self) -> None:
        """Rollback the last version bump operation.

        Restores the original version if a backup exists.
        Safe to call even if no backup exists.
        """
        if self._backup_version is None or self._backup_path is None:
            logger.debug("No version backup to rollback")
            return

        try:
            handler = self._get_handler(self._backup_path)
            handler.write_version(self._backup_path, self._backup_version)
            logger.info(
                f"Rolled back version: {self._backup_path} -> {self._backup_version}"
            )
        except Exception as e:
            logger.error(f"Failed to rollback version: {e}")
            raise
        finally:
            self._backup_version = None
            self._backup_path = None

    def clear_backup(self) -> None:
        """Clear the backup without rolling back.

        Call this after a successful commit to make the bump permanent.
        """
        self._backup_version = None
        self._backup_path = None

    @contextmanager
    def atomic_bump(
        self,
        bump_type: Optional[BumpType] = None,
        task_type: Optional[TaskType] = None,
        path: Optional[Path] = None,
    ) -> Generator[str, None, None]:
        """Context manager for atomic version bumping with automatic rollback.

        On successful exit from the context, the backup is cleared.
        On exception, the version is rolled back to the original.

        Args:
            bump_type: Type of bump to apply (overrides task_type)
            task_type: Task type to determine bump type from config
            path: Path to version file (None = auto-detect)

        Yields:
            New version string

        Example:
            with bumper.atomic_bump(task_type=TaskType.FEATURE) as new_version:
                # Do something with the new version
                git_commit(f"Release {new_version}")
            # If exception raised, version is rolled back automatically
            # If no exception, backup is cleared and bump is permanent
        """
        if not self.config.enabled:
            raise RuntimeError("Version bumping is disabled")

        # Determine bump type
        if bump_type is None:
            if task_type is None:
                raise ValueError("Either bump_type or task_type must be provided")
            bump_type = self.get_bump_type_from_task(task_type, self.config.bump_rules)

        # Find version file
        if path is None:
            path = self.detect_version_file()
            if path is None:
                raise FileNotFoundError("No version file found")

        # Save original version for potential rollback
        handler = self._get_handler(path)
        original_version = handler.read_version(path)
        self._backup_version = original_version
        self._backup_path = path

        # Calculate and apply new version
        new_version = self.bump_version_string(original_version, bump_type)

        try:
            handler.write_version(path, new_version)
            logger.info(f"Bumped version: {original_version} -> {new_version}")
            yield new_version
            # Success - clear backup to make bump permanent
            self.clear_backup()
        except Exception:
            # Failure - rollback to original version
            logger.warning(f"Exception occurred, rolling back version to {original_version}")
            try:
                handler.write_version(path, original_version)
                logger.info(f"Rolled back version to {original_version}")
            except Exception as rollback_error:
                logger.error(f"Failed to rollback version: {rollback_error}")
            raise
        finally:
            if self._backup_version is not None:
                # Ensure backup is cleared on success
                self.clear_backup()
