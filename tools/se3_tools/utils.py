"""Shared utilities for SE 3.0 tools."""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
import shutil


def discover_specs(path: str) -> List[str]:
    """
    Discover all spec files in openspec/specs/ and openspec/changes/*/specs/.

    Args:
        path: Base path to search from (should contain openspec/ directory)

    Returns:
        List of absolute paths to spec.md files
    """
    base_path = Path(path).resolve()
    spec_files = []

    # Standard location: openspec/specs/*/
    standard_specs = base_path / "openspec" / "specs"
    if standard_specs.exists():
        for spec_dir in standard_specs.iterdir():
            if spec_dir.is_dir():
                spec_file = spec_dir / "spec.md"
                if spec_file.exists():
                    spec_files.append(str(spec_file))

    # Change specs: openspec/changes/*/specs/*/
    changes_dir = base_path / "openspec" / "changes"
    if changes_dir.exists():
        for change_dir in changes_dir.iterdir():
            if change_dir.is_dir():
                change_specs = change_dir / "specs"
                if change_specs.exists():
                    for spec_dir in change_specs.iterdir():
                        if spec_dir.is_dir():
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

    Args:
        project_root: Root of the SE 3.0 project

    Returns:
        Dict mapping output paths to source paths
    """
    mappings = {}

    # CLAUDE.md -> output/CLAUDE.md
    claude_md = project_root / "CLAUDE.md"
    if claude_md.exists():
        mappings[project_root / "output" / "CLAUDE.md"] = claude_md

    # Global CLAUDE.md -> output/CLAUDE.global.md
    global_claude = Path.home() / ".claude" / "CLAUDE.md"
    if global_claude.exists():
        mappings[project_root / "output" / "CLAUDE.global.md"] = global_claude

    # se3.config.yaml -> output/se3.config.yaml
    config = project_root / "se3.config.yaml"
    if config.exists():
        mappings[project_root / "output" / "se3.config.yaml"] = config

    # status.md -> output/status.md
    status = project_root / "status.md"
    if status.exists():
        mappings[project_root / "output" / "status.md"] = status

    return mappings


def discover_changes(path: str = "openspec/changes") -> List[str]:
    """Discover all change directories.

    Args:
        path: Base path to search for changes

    Returns:
        List of change directory names
    """
    changes = []
    changes_path = Path(path)

    if not changes_path.exists():
        return changes

    for item in changes_path.iterdir():
        if item.is_dir():
            changes.append(item.name)

    return sorted(changes)


def discover_specs_in_change(change_name: str, base_path: str = "openspec/changes") -> List[str]:
    """Discover all spec files within a change.

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


def discover_human_calls(path: str = "human-calls") -> List[Dict[str, Any]]:
    """Discover all human call files and their status.

    Args:
        path: Path to human-calls directory

    Returns:
        List of dicts with file info and parsed frontmatter
    """
    calls = []
    calls_path = Path(path)

    if not calls_path.exists():
        return calls

    for item in calls_path.glob("*.md"):
        try:
            content = item.read_text(encoding='utf-8')

            call_info = {
                'file': item.name,
                'path': str(item),
                'type': None,
                'priority': None,
                'status': None,
                'created': None
            }

            type_match = re.search(r'^type:\s*(\w+)', content, re.MULTILINE)
            if type_match:
                call_info['type'] = type_match.group(1)

            priority_match = re.search(r'^priority:\s*(\w+)', content, re.MULTILINE)
            if priority_match:
                call_info['priority'] = priority_match.group(1)

            status_match = re.search(r'^status:\s*(\w+)', content, re.MULTILINE)
            if status_match:
                call_info['status'] = status_match.group(1)

            created_match = re.search(r'^created:\s*(\S+)', content, re.MULTILINE)
            if created_match:
                call_info['created'] = created_match.group(1)

            calls.append(call_info)
        except (IOError, UnicodeDecodeError):
            continue

    return calls
