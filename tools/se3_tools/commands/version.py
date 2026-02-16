"""SE3 Version management and enforcement.

Single source of truth: tools/se3_tools/__init__.py:SE3_FRAMEWORK_VERSION
"""

import re
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional


def get_framework_version() -> str:
    """Get current framework version from single source of truth (direct file read for git worktree compatibility)."""
    import os
    from pathlib import Path

    # Get the path to __init__.py in the current working tree
    init_file = Path(__file__).parent.parent / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"Cannot find se3_tools __init__.py at {init_file}")

    with open(init_file, "r", encoding="utf-8") as f:
        content = f.read()

    import re
    match = re.search(r'SE3_FRAMEWORK_VERSION\s*=\s*"([\d]+\.[\d]+\.[\d]+)"', content)
    if not match:
        raise ValueError("Cannot find SE3_FRAMEWORK_VERSION definition in __init__.py")

    return match.group(1)


def get_template_version(project_root: Path) -> Optional[str]:
    """Get version from SE3.md.template."""
    template_file = project_root / "output" / "SE3.md.template"
    if not template_file.exists():
        return None

    content = template_file.read_text()
    match = re.search(r'<!-- SE3 Version: (\d+\.\d+\.\d+) -->', content)
    return match.group(1) if match else None


def get_readme_versions(project_root: Path) -> List[str]:
    """Get all versions from README.md Version History."""
    readme_file = project_root / "README.md"
    if not readme_file.exists():
        return []

    content = readme_file.read_text()
    # Match version table entries: | 1.0.0 | 2026-02-16 | ... |
    versions = re.findall(r'\|\s*(\d+\.\d+\.\d+)\s*\|', content)
    return versions


def check_version_consistency(project_root: Path) -> Tuple[bool, List[str]]:
    """
    Check if all version references are consistent.

    Returns: (is_consistent, list of issues)
    """
    issues = []

    # Get versions from all sources
    framework_version = get_framework_version()
    template_version = get_template_version(project_root)
    readme_versions = get_readme_versions(project_root)

    # Check template matches framework
    if template_version and template_version != framework_version:
        issues.append(
            f"Version mismatch: output/SE3.md.template ({template_version}) "
            f"!= SE3_FRAMEWORK_VERSION ({framework_version})"
        )

    # Check README has the current version
    if readme_versions and framework_version not in readme_versions:
        issues.append(
            f"README.md Version History missing entry for {framework_version}"
        )

    # Check if framework files changed without version bump
    changed_files = get_changed_framework_files(project_root)
    if changed_files:
        if not version_bumped(project_root, framework_version):
            issues.append(
                f"Framework files changed but version not bumped: {', '.join(changed_files)}"
            )

    return len(issues) == 0, issues


def get_changed_framework_files(project_root: Path, since_ref: str = "HEAD~1") -> List[str]:
    """Get list of framework files that have been modified since a git ref.

    Args:
        project_root: Path to the project root
        since_ref: Git ref to compare against (default: HEAD~1 for last commit)
    """
    framework_patterns = [
        "output/SE3.md.template",
        "tools/se3_tools/__init__.py",
        "tools/se3_tools/commands/",
        "scripts/collab-",
        "scripts/rules-",
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
    init_file = project_root / "tools" / "se3_tools" / "__init__.py"
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--", str(init_file)],
        cwd=project_root,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return False

    # Check if SE3_FRAMEWORK_VERSION was modified
    return "SE3_FRAMEWORK_VERSION" in result.stdout


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
        "output/SE3.md.template",
        "tools/se3_tools/__init__.py",
        "tools/se3_tools/commands/",
        "scripts/collab-",
        "scripts/rules-",
    ]

    has_framework_change = any(
        any(f.startswith(fw) for fw in framework_files)
        for f in changed_files
    )

    if has_framework_change:
        # Check if version was updated
        result = subprocess.run(
            ["git", "diff", "--cached", "--", "tools/se3_tools/__init__.py"],
            cwd=project_root,
            capture_output=True,
            text=True
        )

        if "SE3_FRAMEWORK_VERSION" not in result.stdout:
            issues.append(
                "Framework files changed but SE3_FRAMEWORK_VERSION not updated. "
                "Update version following SemVer: PATCH for fixes, MINOR for features, MAJOR for breaking changes."
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
    from pathlib import Path

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
