"""Shared utilities for SE 3.0 tools."""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
import shutil

# Re-export human_calls functions for backwards compatibility
from .human_calls import discover_human_calls, HumanCallStore


def _resolve_specs_dir(base_path: Path) -> Path:
    """Resolve specs directory: se3/specs/ preferred, specs/ fallback, openspec/specs/ legacy."""
    primary = base_path / "se3" / "specs"
    fallback = base_path / "specs"
    legacy = base_path / "openspec" / "specs"
    if primary.exists():
        return primary
    if fallback.exists():
        return fallback
    return legacy


def discover_specs(path: str) -> List[str]:
    """
    Discover all spec files in specs/ (or openspec/specs/ fallback).

    Args:
        path: Base path to search from

    Returns:
        List of absolute paths to spec.md files
    """
    base_path = Path(path).resolve()
    spec_files = []

    # Standard location: specs/*/ (with openspec/specs/ fallback)
    standard_specs = _resolve_specs_dir(base_path)
    if standard_specs.exists():
        for spec_dir in standard_specs.iterdir():
            if spec_dir.is_dir() and not spec_dir.name.startswith("_"):
                spec_file = spec_dir / "spec.md"
                if spec_file.exists():
                    spec_files.append(str(spec_file))

    return sorted(spec_files)


def parse_spec(filepath: str) -> Dict[str, Any]:
    """
    Parse a spec file and extract key sections.

    Args:
        filepath: Path to the spec.md file

    Returns:
        Dict with:
        - title: str (the # header)
        - purpose: str (content under ## Purpose)
        - requirements: List[Dict] with keys: title, level, content, scenarios, line
        - scenarios: List[Dict] with keys: title, when, then, line
    """
    result = {
        "title": None,
        "purpose": None,
        "requirements": [],
        "scenarios": [],
    }

    content = Path(filepath).read_text(encoding="utf-8")
    lines = content.split("\n")

    current_section = None
    current_requirement = None
    current_scenario = None
    i = 0

    while i < len(lines):
        line = lines[i]

        # Title (# header)
        if line.startswith("# ") and "Specification" in line:
            result["title"] = line[2:].strip()

        # Purpose section
        elif line.startswith("## Purpose"):
            current_section = "purpose"
            purpose_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("#"):
                if lines[i].strip():
                    purpose_lines.append(lines[i].strip())
                i += 1
            result["purpose"] = " ".join(purpose_lines) if purpose_lines else ""
            continue

        # Requirements section
        elif line.startswith("## Requirements"):
            current_section = "requirements"

        # Requirement (### or #### with "Requirement:")
        elif re.match(r"^###+\s+Requirement:", line):
            current_requirement = {
                "title": line.split(":", 1)[1].strip() if ":" in line else "",
                "level": line.count("#"),
                "content": [],
                "scenarios": [],
                "line": i + 1,
            }
            result["requirements"].append(current_requirement)
            current_scenario = None

        # Scenario
        elif re.match(r"^####+\s+Scenario:", line):
            scenario_title = line.split(":", 1)[1].strip() if ":" in line else ""
            current_scenario = {
                "title": scenario_title,
                "when": None,
                "then": None,
                "line": i + 1,
            }
            result["scenarios"].append(current_scenario)
            if current_requirement:
                current_requirement["scenarios"].append(current_scenario)

        # WHEN clause
        elif current_scenario and re.match(r"^-\s+\*\*WHEN\*\*", line, re.IGNORECASE):
            match = re.match(r"^-\s+\*\*WHEN\*\*\s*(.+)", line, re.IGNORECASE)
            if match:
                current_scenario["when"] = match.group(1).strip()

        # THEN clause
        elif current_scenario and re.match(r"^-\s+\*\*THEN\*\*", line, re.IGNORECASE):
            match = re.match(r"^-\s+\*\*THEN\*\*\s*(.+)", line, re.IGNORECASE)
            if match:
                current_scenario["then"] = match.group(1).strip()

        # Collect requirement content (for checking SHALL/SHOULD/MAY)
        elif current_requirement and current_section == "requirements":
            if line.strip() and not line.startswith("#"):
                current_requirement["content"].append(line.strip())

        i += 1

    return result


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

    With the SE3 module system, output/ only contains templates for `se3 init`.
    Runtime files (CLAUDE.md, status.md, se3.yaml) are no longer synced.

    Args:
        project_root: Root of the SE 3.0 project

    Returns:
        Dict mapping output paths to source paths
    """
    # No runtime sync mappings — output/ now only holds templates
    return {}


def discover_changes(path: str = "se3/specs/_changelog") -> List[str]:
    """Discover all changelog entries.

    Args:
        path: Base path to search for changelog entries (falls back to specs/, openspec/changes)

    Returns:
        List of changelog entry names
    """
    changes = []
    changes_path = Path(path)

    # Fallback to specs/_changelog (legacy)
    if not changes_path.exists():
        changes_path = Path("specs/_changelog")

    # Fallback to legacy openspec/changes
    if not changes_path.exists():
        changes_path = Path("openspec/changes")

    if not changes_path.exists():
        return changes

    for item in changes_path.iterdir():
        if item.is_dir() or (item.is_file() and item.suffix == ".md"):
            changes.append(item.stem if item.is_file() else item.name)

    return sorted(changes)


def discover_specs_in_change(change_name: str, base_path: str = "openspec/changes") -> List[str]:
    """Discover all spec files within a change (legacy support).

    Args:
        change_name: Name of the change
        base_path: Base path to changes directory

    Returns:
        List of spec file paths
    """
    specs = []
    specs_path = Path(base_path) / change_name / "specs"

    if not specs_path.exists():
        return specs

    for item in specs_path.rglob("spec.md"):
        specs.append(str(item))

    return sorted(specs)


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
    """Extract SE3 framework version from tools/se3_tools/__init__.py.

    Args:
        project_root: Root of the project

    Returns:
        Version string (e.g., "2.22.4") or None if not found
    """
    init_file = project_root / "tools" / "se3_tools" / "__init__.py"
    if not init_file.exists():
        return None

    try:
        content = init_file.read_text()
        match = re.search(r'SE3_FRAMEWORK_VERSION = "(\d+\.\d+\.\d+)"', content)
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
    init_file = project_root / "tools" / "se3_tools" / "__init__.py"

    # Check if this is an SE3 framework project
    is_se3_framework = init_file.exists()

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
                    "BLOCKING: Could not extract SE3_FRAMEWORK_VERSION from tools/se3_tools/__init__.py.\n"
                    "  Required action: Ensure the file contains valid version string:\n"
                    '    SE3_FRAMEWORK_VERSION = "X.Y.Z"'
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
        "scripts/collab-",
        "scripts/rules-",
        "scripts/mcp-",
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


