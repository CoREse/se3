"""SE3 Version management.

Single source of truth: pyproject.toml
"""

import re
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional


def get_framework_version() -> str:
    """Get current framework version from single source of truth (pyproject.toml)."""
    from .. import __version__
    return __version__


def get_versions_from_versions_md(project_root: Path) -> List[str]:
    """Get all versions from VERSIONS.md version history."""
    versions_file = project_root / "VERSIONS.md"
    if not versions_file.exists():
        return []

    content = versions_file.read_text()
    # Match version table entries: | 1.0.0 | 2026-02-16 | ... |
    versions = re.findall(r'\|\s*(\d+\.\d+\.\d+)\s*\|', content)
    return versions


def check_version_consistency(project_root: Path) -> Tuple[bool, List[str]]:
    """
    Check if version references are consistent.

    Returns: (is_consistent, list of issues)
    """
    issues = []

    framework_version = get_framework_version()
    versions_md_versions = get_versions_from_versions_md(project_root)

    # Check VERSIONS.md has the current version
    if versions_md_versions and framework_version not in versions_md_versions:
        issues.append(
            f"VERSIONS.md missing entry for {framework_version}"
        )

    return len(issues) == 0, issues


def get_changed_framework_files(project_root: Path, since_ref: str = "HEAD~1") -> List[str]:
    """Get list of framework files that have been modified since a git ref."""
    framework_patterns = [
        "src/se3/",
        "scripts/",
    ]

    result = subprocess.run(
        ["git", "diff", "--name-only", since_ref, "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True
    )

    changed = result.stdout.strip().split('\n') if result.returncode == 0 else []

    framework_files = []
    for f in changed:
        for pattern in framework_patterns:
            if f.startswith(pattern):
                framework_files.append(f)
                break

    return framework_files


def version_bumped(project_root: Path, current_version: str) -> bool:
    """Check if version has been bumped in the latest commit."""
    pyproject_file = project_root / "pyproject.toml"
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--", str(pyproject_file)],
        cwd=project_root,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return False

    # Check if version was modified
    return 'version' in result.stdout


def validate_version_bump(project_root: Path) -> Tuple[bool, List[str]]:
    """
    Validate that version bump follows SemVer rules.

    Returns: (is_valid, list of issues)
    """
    issues = []

    # Get changed files
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project_root,
        capture_output=True,
        text=True
    )

    changed_files = result.stdout.strip().split('\n') if result.returncode == 0 else []

    # Check for framework changes
    framework_files = [
        "src/se3/",
        "scripts/",
    ]

    has_framework_change = any(
        any(f.startswith(fw) for fw in framework_files)
        for f in changed_files
    )

    if has_framework_change:
        # Check if version was updated
        result = subprocess.run(
            ["git", "diff", "--cached", "--", "pyproject.toml"],
            cwd=project_root,
            capture_output=True,
            text=True
        )

        if "version" not in result.stdout:
            issues.append(
                "Framework files changed but version not updated. "
                "Update version in pyproject.toml following SemVer: "
                "PATCH for fixes, MINOR for features, MAJOR for breaking changes."
            )

    return len(issues) == 0, issues


def find_project_root() -> Path:
    """Find project root by looking for .git (directory or file in worktrees)."""
    current = Path.cwd()
    while current != current.parent:
        git_path = current / ".git"
        if git_path.exists():
            return current
        current = current.parent
    return Path.cwd()


if __name__ == "__main__":
    # Simple CLI for testing
    import sys

    project_root = find_project_root()
    is_valid, issues = check_version_consistency(project_root)

    if not is_valid:
        print("Version consistency issues found:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)
    else:
        print(f"Version consistent: {get_framework_version()}")
        sys.exit(0)
