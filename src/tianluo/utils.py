"""Shared utilities for SE 3.0 tools."""

import re
from pathlib import Path
from typing import Dict, List, Optional, Any
import shutil


def get_exit_code(results: List[Dict[str, Any]]) -> int:
    """
    Determine exit code from validation results.

    Args:
        results: List of result dicts with 'level' key ('error', 'warning', 'info', 'runtime')

    Returns:
        0 = success (no errors)
        1 = validation errors found
        2 = runtime/configuration error
    """
    has_error = False
    has_runtime_error = False

    for result in results:
        level = result.get("level", "")
        if level == "error":
            has_error = True
        elif level == "runtime":
            has_runtime_error = True

    if has_runtime_error:
        return 2
    if has_error:
        return 1
    return 0


def discover_outputs(path: Path) -> List[Path]:
    """Discover all output files in the given path.

    Args:
        path: Directory to scan for output files

    Returns:
        List of output file paths
    """
    if not path.exists():
        return []

    outputs = []
    for item in path.iterdir():
        if item.is_file():
            outputs.append(item)
    return outputs


def get_file_mtime(path: Path) -> Optional[float]:
    """Get the modification time of a file.

    Args:
        path: Path to the file

    Returns:
        Modification timestamp, or None if file doesn't exist
    """
    if not path.exists():
        return None
    return path.stat().st_mtime


def copy_file(src: Path, dst: Path) -> None:
    """Copy a file from source to destination.

    Args:
        src: Source file path
        dst: Destination file path
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def get_source_mappings(project_root: Path) -> dict:
    """Get mapping of source files to output files.

    With the SE3 module system, output/ only contains templates for `luo init`.
    Runtime files (CLAUDE.md, status.md, tianluo.yaml) are no longer synced.

    Args:
        project_root: Root of the SE 3.0 project

    Returns:
        Dict mapping output paths to source paths
    """
    # No runtime sync mappings — output/ now only holds templates
    return {}


def find_verification_markers(scenario_id: str, search_paths: List[str]) -> List[Dict[str, str]]:
    """Search for verification markers for a scenario ID.

    Args:
        scenario_id: The scenario ID to search for (format: spec-name/scenario-name)
        search_paths: List of paths to search in

    Returns:
        List of dicts with 'type', 'file', 'line' keys
    """
    markers = []

    # Patterns to search for
    pytest_pattern = re.compile(r'@pytest\.mark\.scenario\(["\']' + re.escape(scenario_id) + r'["\']\)')
    comment_pattern = re.compile(r'# Verify:\s*' + re.escape(scenario_id) + r'\s*$')

    for search_path in search_paths:
        path = Path(search_path)
        if not path.exists():
            continue

        # Search Python files for pytest markers
        for py_file in path.rglob("*.py"):
            try:
                content = py_file.read_text(encoding='utf-8')
                lines = content.split('\n')

                for i, line in enumerate(lines, 1):
                    if pytest_pattern.search(line):
                        markers.append({
                            'type': 'pytest',
                            'file': str(py_file),
                            'line': i
                        })
                    elif comment_pattern.search(line):
                        markers.append({
                            'type': 'comment',
                            'file': str(py_file),
                            'line': i
                        })
            except (IOError, UnicodeDecodeError):
                continue

    return markers


def get_framework_version(project_root: Path) -> Optional[str]:
    """Extract SE3 framework version from pyproject.toml.

    Args:
        project_root: Root of the project

    Returns:
        Version string (e.g., "3.3.6") or None if not found
    """
    pyproject_file = project_root / "pyproject.toml"
    if not pyproject_file.exists():
        return None

    try:
        content = pyproject_file.read_text()
        match = re.search(r'version\s*=\s*"(\d+\.\d+\.\d+)"', content)
        return match.group(1) if match else None
    except (OSError, IOError):
        return None


def check_documentation_consistency(
    project_root: Path,
    check_framework_files: bool = False
) -> tuple[bool, List[str]]:
    """Check documentation consistency: README.md and VERSIONS.md.

    This is the SHARED version used by commit, done, and handoff commands.

    Args:
        project_root: Root of the project
        check_framework_files: If True, perform strict checks for framework changes

    Returns:
        Tuple of (is_consistent, list_of_issues)
        Issues prefixed with "BLOCKING:" indicate the check should block the operation
    """
    issues = []
    readme_path = project_root / "README.md"
    versions_path = project_root / "VERSIONS.md"
    pyproject_file = project_root / "pyproject.toml"

    # Check if this is an SE3 framework project
    is_se3_framework = pyproject_file.exists()

    # Check README.md exists
    if not readme_path.exists():
        if is_se3_framework and check_framework_files:
            issues.append(
                "BLOCKING: README.md not found.\n"
                "  Required action: Create README.md with framework documentation."
            )
            return False, issues
        else:
            issues.append("README.md not found")
            return False, issues

    readme_content = readme_path.read_text()

    # Check VERSIONS.md exists and is referenced
    if versions_path.exists():
        if "VERSIONS.md" not in readme_content:
            msg = "README.md does not reference VERSIONS.md"
            if is_se3_framework:
                if check_framework_files or "Version History" in readme_content:
                    issues.append(f"BLOCKING: {msg}.\n"
                        f"  Required action: Update README.md to reference VERSIONS.md\n"
                        f"  Add: 'See [VERSIONS.md](VERSIONS.md) for the complete version history.'")
                    return False, issues
            else:
                issues.append(msg)
    else:
        if is_se3_framework:
            if check_framework_files:
                issues.append(
                    "BLOCKING: VERSIONS.md not found.\n"
                    "  Required action: Create VERSIONS.md with version history."
                )
                return False, issues
            else:
                # Non-blocking: still report for awareness
                issues.append(
                    "VERSIONS.md not found. "
                    "Recommended action: Create VERSIONS.md with version history."
                )

    # Check version consistency (only for SE3 framework projects)
    if is_se3_framework:
        current_version = get_framework_version(project_root)
        if not current_version:
            if check_framework_files:
                issues.append(
                    "BLOCKING: Could not extract version from pyproject.toml.\n"
                    "  Required action: Ensure the file contains valid version string:\n"
                    '    version = "X.Y.Z"'
                )
                return False, issues
        else:
            # Check README.md contains current version (as distinct version to avoid substring matches)
            # Use negative lookbehind/ahead to ensure "2.22" doesn't match "2.22.6"
            # Match must be preceded/followed by non-version char (not digit or dot) or string boundary
            import re
            version_pattern = rf'(?<![\d.]){re.escape(current_version)}(?![\d.])'
            if not re.search(version_pattern, readme_content):
                msg = f"Version {current_version} not found in README.md"
                if check_framework_files:
                    issues.append(
                        f"BLOCKING: {msg}.\n"
                        f"  Required action: Update README.md to reference version {current_version}\n"
                        f"  README.md should include the current version (e.g., 'Current Version: {current_version}')"
                    )
                    return False, issues
                else:
                    issues.append(msg)

            # Check VERSIONS.md contains current version (if it exists)
            if versions_path.exists():
                versions_content = versions_path.read_text()
                # Use same pattern: version must be distinct (not part of larger version)
                if not re.search(version_pattern, versions_content):
                    msg = f"Version {current_version} not found in VERSIONS.md"
                    if check_framework_files:
                        issues.append(
                            f"BLOCKING: {msg}.\n"
                            f"  Required action: Add version entry to VERSIONS.md\n"
                            f"  Entry format: | {current_version} | YYYY-MM-DD | Description of changes |"
                        )
                        return False, issues
                    else:
                        issues.append(msg)

    return len(issues) == 0, issues


def has_framework_file_changes(project_root: Path) -> tuple[bool, List[str]]:
    """Check if any framework files have been staged for commit.

    Args:
        project_root: Root of the project

    Returns:
        Tuple of (has_changes, list_of_changed_files)
    """
    import subprocess

    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project_root, capture_output=True, text=True
    )
    staged_files = result.stdout.strip().split("\n") if result.returncode == 0 else []

    framework_patterns = [
        "tools/se3_tools/__init__.py",
        "tools/se3_tools/commands/",
        "tools/se3_tools/utils.py",
        "tools/se3_tools/progress.py",
        "tools/se3_tools/human_calls.py",
        "tools/se3_tools/config.py",
        "scripts/rules-",
    ]

    changed_framework_files = []
    for f in staged_files:
        for pattern in framework_patterns:
            if f.startswith(pattern):
                changed_framework_files.append(f)
                break

    return len(changed_framework_files) > 0, changed_framework_files


def parse_status_md(filepath: str = "./status.md") -> Dict[str, Any]:
    """Parse the status.md file.

    Args:
        filepath: Path to status.md

    Returns:
        Dict with parsed status information
    """
    result = {
        'active_change': None,
        'current_task': None,
        'status': None,
        'blocked_since': None,
        'blockers': [],
        'raw': {}
    }

    path = Path(filepath)
    if not path.exists():
        return result

    content = path.read_text(encoding='utf-8')

    # Extract Active Change
    active_match = re.search(r'\*\*Active Change\*\*:\s*`?([^`\n]+)`?', content)
    if active_match:
        result['active_change'] = active_match.group(1).strip()

    # Extract Current Task
    task_match = re.search(r'\*\*Current Task\*\*:\s*`?([^`\n]+)`?', content)
    if task_match:
        result['current_task'] = task_match.group(1).strip()

    # Extract Status
    status_match = re.search(r'\*\*Status\*\*:\s*`?([^`\n]+)`?', content)
    if status_match:
        result['status'] = status_match.group(1).strip()

    # Extract Blocked Since
    blocked_match = re.search(r'\*\*Blocked Since\*\*:\s*`?([^`\n]+)`?', content)
    if blocked_match:
        result['blocked_since'] = blocked_match.group(1).strip()

    # Extract Blockers table
    blockers_section = re.search(r'## Blockers\s*\n\s*\n.*\n\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|\s*\n((?:\|[^|]+\|[^|]+\|[^|]+\|[^|]+\|\s*\n)+)', content)
    if blockers_section:
        table_rows = blockers_section.group(5)
        for row in table_rows.strip().split('\n'):
            parts = [p.strip() for p in row.split('|')[1:-1]]
            if len(parts) >= 4 and parts[0] != 'Issue' and parts[0] != 'none':
                result['blockers'].append({
                    'issue': parts[0],
                    'type': parts[1],
                    'since': parts[2],
                    'resolution': parts[3]
                })

    return result


