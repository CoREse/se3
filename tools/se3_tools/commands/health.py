"""SE3 Health command - spec system integrity diagnostics.

Provides comprehensive health checks for the SE3 spec system:
- Zombie changes detection
- Old format change detection
- Completed but unarchived changes
- Stale changes (>30 days no activity)
- Directory structure drift
- Spec-change association validation

Usage:
    se3 health [--format json] [--fix]
"""

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict

import typer

app = typer.Typer(invoke_without_command=True)


@dataclass
class HealthIssue:
    """Represents a health issue found in the OpenSpec system."""
    severity: str  # 'error', 'warning', 'info'
    category: str  # 'zombie', 'old_format', 'unarchived', 'stale', 'structure', 'naming'
    message: str
    change_name: Optional[str] = None
    suggestion: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChangeInfo:
    """Information about a single change."""
    name: str
    path: Path
    is_archived: bool = False
    has_openspec_yaml: bool = False
    has_legacy_files: bool = False
    workflow: str = "unknown"
    current_step: str = "unknown"
    complete: bool = False
    tasks_total: int = 0
    tasks_done: int = 0
    last_activity: Optional[datetime] = None
    created_at: Optional[datetime] = None
    specs: List[str] = field(default_factory=list)


def parse_change_name(name: str) -> Dict[str, Any]:
    """Parse a change name to extract metadata and assess naming quality.

    Returns dict with:
        - is_valid: bool - whether name follows conventions
        - has_intent: bool - whether name describes intent
        - is_auto_generated: bool - whether name looks auto-generated
        - parts: List[str] - name parts
        - suggestion: str - suggested better name if applicable
    """
    result = {
        "is_valid": True,
        "has_intent": False,
        "is_auto_generated": False,
        "parts": name.split("-"),
        "suggestion": "",
        "issues": []
    }

    # Check for auto-generated patterns
    auto_patterns = [
        r"^se3\d+[a-z]+",  # se31xse3md...
        r"^t\d+-\d+x",     # t1-1xopenspec...
        r"^[a-z]+\d+$",    # ending with numbers only
        r"\d+-\d+-\d+",    # multiple number sequences
    ]

    for pattern in auto_patterns:
        if re.search(pattern, name, re.IGNORECASE):
            result["is_auto_generated"] = True
            result["issues"].append("Appears auto-generated (not human-readable)")
            break

    # Check for intent (should have descriptive words)
    descriptive_words = [
        "fix", "add", "update", "remove", "refactor", "implement",
        "create", "delete", "improve", "optimize", "migrate",
        "upgrade", "support", "enable", "disable", "configure"
    ]

    name_lower = name.lower()
    has_descriptive = any(word in name_lower for word in descriptive_words)

    # Check for meaningful words (longer than 2 chars, not just numbers)
    meaningful_words = [p for p in result["parts"] if len(p) > 2 and not p.isdigit()]

    if has_descriptive and len(meaningful_words) >= 2:
        result["has_intent"] = True
    elif len(meaningful_words) < 2:
        result["issues"].append("Name lacks descriptive words")
        result["is_valid"] = False

    # Generate suggestion if name is poor
    if result["is_auto_generated"] or not result["has_intent"]:
        result["suggestion"] = "Use format: <type>-<descriptive-name> (e.g., 'fix-login-validation', 'add-user-profile')"

    return result


def get_change_activity_time(change_path: Path) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Get the last activity and creation time for a change.

    Returns:
        Tuple of (last_activity, created_at)
    """
    last_activity = None
    created_at = None

    # Check various files for timestamps
    files_to_check = [
        ".se3-state.json",
        ".openspec.yaml",
        "tasks.md",
        "proposal.md",
        "design.md",
    ]

    for filename in files_to_check:
        filepath = change_path / filename
        if filepath.exists():
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                if last_activity is None or mtime > last_activity:
                    last_activity = mtime

                # Use earliest file as creation time approximation
                if created_at is None or mtime < created_at:
                    created_at = mtime
            except (OSError, PermissionError):
                pass

    # Also check git log for the directory
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(change_path)],
            capture_output=True, text=True, cwd=change_path.parent.parent.parent
        )
        if result.returncode == 0 and result.stdout.strip():
            git_time = datetime.fromtimestamp(int(result.stdout.strip()))
            if last_activity is None or git_time > last_activity:
                last_activity = git_time
    except (subprocess.SubprocessError, ValueError):
        pass

    return last_activity, created_at


def discover_all_changes(project_root: Path) -> List[ChangeInfo]:
    """Discover all changes in the openspec/changes directory (legacy)."""
    changes = []
    changes_dir = project_root / "openspec" / "changes"

    if not changes_dir.exists():
        return changes

    # Recursively find all directories with .se3-state.json or .openspec.yaml
    for marker_file in changes_dir.rglob(".se3-state.json"):
        change_path = marker_file.parent
        rel_path = change_path.relative_to(changes_dir)
        change_name = str(rel_path)

        is_archived = str(rel_path).startswith("archive/")

        info = ChangeInfo(
            name=change_name,
            path=change_path,
            is_archived=is_archived
        )

        # Check for .openspec.yaml (new format)
        info.has_openspec_yaml = (change_path / ".openspec.yaml").exists()

        # Check for legacy files (old format)
        legacy_files = ["proposal.md", "spec.md", "status.md"]
        info.has_legacy_files = any((change_path / f).exists() for f in legacy_files)

        # Read state file
        try:
            state = json.loads(marker_file.read_text())
            info.workflow = state.get("workflow", "unknown")
            info.current_step = state.get("current_step", "unknown")
            info.complete = state.get("complete", False)
        except (json.JSONDecodeError, OSError):
            pass

        # Read tasks
        tasks_file = change_path / "tasks.md"
        if tasks_file.exists():
            try:
                content = tasks_file.read_text()
                info.tasks_total = content.count("- [")
                info.tasks_done = content.count("- [x]")
            except OSError:
                pass

        # Get activity times
        info.last_activity, info.created_at = get_change_activity_time(change_path)

        # Discover associated specs
        specs_dir = change_path / "specs"
        if specs_dir.exists():
            for spec_file in specs_dir.rglob("spec.md"):
                info.specs.append(str(spec_file.relative_to(change_path)))

        changes.append(info)

    return changes


def check_zombie_changes(changes: List[ChangeInfo], stale_days: int = 30) -> List[HealthIssue]:
    """Check for zombie changes (old, inactive, no progress)."""
    issues = []
    now = datetime.now()

    for change in changes:
        if change.is_archived:
            continue

        # Skip recently created changes
        if change.created_at and (now - change.created_at).days < 7:
            continue

        # Check for no activity
        is_zombie = False
        reasons = []

        if change.last_activity:
            days_inactive = (now - change.last_activity).days
            if days_inactive > stale_days:
                is_zombie = True
                reasons.append(f"no activity for {days_inactive} days")

        # Check for no progress
        if change.tasks_total > 0 and change.tasks_done == 0:
            is_zombie = True
            reasons.append("no tasks completed")

        # Check for stuck in early steps
        if change.current_step in ("propose", "spec", "design") and change.tasks_done == 0:
            if change.created_at and (now - change.created_at).days > 14:
                is_zombie = True
                reasons.append(f"stuck in {change.current_step} for {(now - change.created_at).days} days")

        if is_zombie:
            issues.append(HealthIssue(
                severity="warning",
                category="zombie",
                message=f"Zombie change: '{change.name}' ({', '.join(reasons)})",
                change_name=change.name,
                suggestion="Archive the change if abandoned, or resume work on it",
                details={
                    "days_inactive": (now - change.last_activity).days if change.last_activity else None,
                    "current_step": change.current_step,
                    "tasks_done": change.tasks_done,
                    "tasks_total": change.tasks_total
                }
            ))

    return issues


def check_old_format_changes(changes: List[ChangeInfo]) -> List[HealthIssue]:
    """Check for changes using old format (without .openspec.yaml)."""
    issues = []

    for change in changes:
        if change.is_archived:
            continue

        if not change.has_openspec_yaml:
            # This is an old format change
            has_state = (change.path / ".se3-state.json").exists()

            if has_state:
                # Has state file but no openspec.yaml - partial migration
                issues.append(HealthIssue(
                    severity="warning",
                    category="old_format",
                    message=f"Old format change: '{change.name}' (missing .openspec.yaml)",
                    change_name=change.name,
                    suggestion="Run 'openspec migrate-change <name>' to update format, or archive if complete"
                ))
            elif change.has_legacy_files:
                # Has old files, no state - very old format
                issues.append(HealthIssue(
                    severity="warning",
                    category="old_format",
                    message=f"Legacy format change: '{change.name}' (has proposal.md/spec.md/status.md)",
                    change_name=change.name,
                    suggestion="Migrate to new format or archive the change"
                ))

    return issues


def check_unarchived_completed(changes: List[ChangeInfo]) -> List[HealthIssue]:
    """Check for completed changes that should be archived."""
    issues = []

    for change in changes:
        if change.is_archived:
            continue

        # Check if all tasks are done
        all_tasks_done = change.tasks_total > 0 and change.tasks_done == change.tasks_total

        # Check if marked complete
        is_complete = change.complete

        if all_tasks_done or is_complete:
            issues.append(HealthIssue(
                severity="info",
                category="unarchived",
                message=f"Completed change ready to archive: '{change.name}'",
                change_name=change.name,
                suggestion=f"Run 'openspec archive {change.name}' to archive this change",
                details={
                    "tasks_done": change.tasks_done,
                    "tasks_total": change.tasks_total,
                    "marked_complete": change.complete
                }
            ))

    return issues


def check_stale_changes(changes: List[ChangeInfo], stale_days: int = 30) -> List[HealthIssue]:
    """Check for stale changes (no activity for specified days)."""
    issues = []
    now = datetime.now()

    for change in changes:
        if change.is_archived or change.complete:
            continue

        if change.last_activity:
            days_inactive = (now - change.last_activity).days
            if days_inactive > stale_days:
                issues.append(HealthIssue(
                    severity="info",
                    category="stale",
                    message=f"Stale change: '{change.name}' (no activity for {days_inactive} days)",
                    change_name=change.name,
                    suggestion="Resume work on this change or archive it if no longer needed",
                    details={"days_inactive": days_inactive}
                ))

    return issues


def check_naming_conventions(changes: List[ChangeInfo]) -> List[HealthIssue]:
    """Check change names for convention compliance."""
    issues = []

    for change in changes:
        if change.is_archived:
            continue

        name_analysis = parse_change_name(change.name)

        if name_analysis["is_auto_generated"]:
            issues.append(HealthIssue(
                severity="warning",
                category="naming",
                message=f"Auto-generated change name: '{change.name}'",
                change_name=change.name,
                suggestion=name_analysis.get("suggestion", "Use a descriptive, human-readable name"),
                details={"issues": name_analysis.get("issues", [])}
            ))
        elif not name_analysis["has_intent"]:
            issues.append(HealthIssue(
                severity="info",
                category="naming",
                message=f"Poor change name: '{change.name}' (lacks descriptive intent)",
                change_name=change.name,
                suggestion=name_analysis.get("suggestion", "Include action words like 'fix', 'add', 'update'"),
                details={"issues": name_analysis.get("issues", [])}
            ))

    return issues


def check_directory_structure(project_root: Path) -> List[HealthIssue]:
    """Check for directory structure drift."""
    issues = []

    expected_dirs = [
        ("se3/specs", True),  # (path, required)
        ("se3/specs/_changelog", False),
    ]

    for dir_path, required in expected_dirs:
        full_path = project_root / dir_path
        if not full_path.exists():
            if required:
                issues.append(HealthIssue(
                    severity="error",
                    category="structure",
                    message=f"Missing required directory: {dir_path}",
                    suggestion=f"Create the directory: mkdir -p {dir_path}"
                ))
            else:
                issues.append(HealthIssue(
                    severity="info",
                    category="structure",
                    message=f"Missing optional directory: {dir_path}",
                    suggestion=f"Create the directory if needed: mkdir -p {dir_path}"
                ))

    # Check for legacy directory locations
    legacy_dirs = [
        ("human-calls", "se3/calls"),
        (".collab", "se3/collab"),
        (".se3", "se3"),
    ]

    for legacy, modern in legacy_dirs:
        legacy_path = project_root / legacy
        modern_path = project_root / modern

        if legacy_path.exists() and not modern_path.exists():
            issues.append(HealthIssue(
                severity="warning",
                category="structure",
                message=f"Legacy directory detected: {legacy}/ (should be {modern}/)",
                suggestion=f"Run 'se3 migrate' to migrate to new directory structure"
            ))

    return issues


def check_spec_change_association(project_root: Path, changes: List[ChangeInfo]) -> List[HealthIssue]:
    """Check for weak spec-change associations."""
    issues = []

    # Find specs that don't reference any change
    specs_dir = project_root / "specs"
    if not specs_dir.exists():
        specs_dir = project_root / "openspec" / "specs"
    if specs_dir.exists():
        for spec_dir in specs_dir.iterdir():
            if not spec_dir.is_dir():
                continue

            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                continue

            try:
                content = spec_file.read_text()
                # Check for change references
                has_change_ref = "change" in content.lower() or "implemented-by" in content.lower()

                if not has_change_ref:
                    # Check if any change modifies this spec
                    related_changes = [
                        c for c in changes
                        if spec_dir.name in str(c.specs) or spec_dir.name in c.name
                    ]

                    if not related_changes:
                        issues.append(HealthIssue(
                            severity="info",
                            category="spec_association",
                            message=f"Spec '{spec_dir.name}' has no associated changes",
                            suggestion="When implementing this spec, create a change and reference it"
                        ))
            except OSError:
                pass

    return issues


def run_health_check(
    project_root: str = ".",
    stale_days: int = 30,
    include_archived: bool = False,
    skip_test_changes: bool = True
) -> Dict[str, Any]:
    """Run all health checks and return results.

    Args:
        project_root: Root directory of the project
        stale_days: Number of days before a change is considered stale
        include_archived: Whether to include archived changes in checks

    Returns:
        Dict with health check results
    """
    root = Path(project_root).resolve()

    # Discover all changes
    changes = discover_all_changes(root)

    # Filter out archived changes unless requested
    all_active_changes = [c for c in changes if not c.is_archived]
    changes_to_check = changes if include_archived else all_active_changes

    # Filter out test changes if requested
    if skip_test_changes:
        changes_to_check = [c for c in changes_to_check if not is_test_change(c.name)]
        active_changes = [c for c in all_active_changes if not is_test_change(c.name)]
    else:
        active_changes = all_active_changes

    # Run all checks
    all_issues = []

    all_issues.extend(check_directory_structure(root))
    all_issues.extend(check_zombie_changes(changes_to_check, stale_days))
    all_issues.extend(check_old_format_changes(changes_to_check))
    all_issues.extend(check_unarchived_completed(changes_to_check))
    all_issues.extend(check_stale_changes(changes_to_check, stale_days))
    all_issues.extend(check_naming_conventions(changes_to_check))
    all_issues.extend(check_spec_change_association(root, changes_to_check))

    # Categorize issues
    errors = [i for i in all_issues if i.severity == "error"]
    warnings = [i for i in all_issues if i.severity == "warning"]
    infos = [i for i in all_issues if i.severity == "info"]

    # Compute summary statistics
    stats = {
        "total_changes": len(changes),
        "active_changes": len(active_changes),
        "archived_changes": len(changes) - len(active_changes),
        "completed_changes": len([c for c in active_changes if c.complete]),
        "zombie_changes": len([i for i in all_issues if i.category == "zombie"]),
        "old_format_changes": len([i for i in all_issues if i.category == "old_format"]),
        "unarchived_completed": len([i for i in all_issues if i.category == "unarchived"]),
        "stale_changes": len([i for i in all_issues if i.category == "stale"]),
        "naming_issues": len([i for i in all_issues if i.category == "naming"]),
    }

    return {
        "healthy": len(errors) == 0 and len(warnings) == 0,
        "project_root": str(root),
        "checks_run": [
            "directory_structure",
            "zombie_changes",
            "old_format",
            "unarchived_completed",
            "stale_changes",
            "naming_conventions",
            "spec_association"
        ],
        "stats": stats,
        "issues": [asdict(i) for i in all_issues],
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
            "info": len(infos)
        }
    }


def print_text_report(results: Dict[str, Any]) -> None:
    """Print a human-readable health report."""
    print(f"\n{'=' * 70}")
    print("SE 3.0 Spec System Health Check")
    print(f"{'=' * 70}")

    print(f"\nProject: {results['project_root']}")

    # Statistics
    stats = results.get("stats", {})
    print(f"\n{'-' * 70}")
    print("Statistics:")
    print(f"{'-' * 70}")
    print(f"  Total changes:     {stats.get('total_changes', 0)}")
    print(f"  Active changes:    {stats.get('active_changes', 0)}")
    print(f"  Archived changes:  {stats.get('archived_changes', 0)}")
    print(f"  Completed:         {stats.get('completed_changes', 0)}")
    print(f"  Zombie changes:    {stats.get('zombie_changes', 0)}")
    print(f"  Old format:        {stats.get('old_format_changes', 0)}")
    print(f"  Stale (>30 days):  {stats.get('stale_changes', 0)}")
    print(f"  Naming issues:     {stats.get('naming_issues', 0)}")

    # Issues
    issues = results.get("issues", [])
    if issues:
        print(f"\n{'-' * 70}")
        print("Issues Found:")
        print(f"{'-' * 70}")

        # Group by severity
        errors = [i for i in issues if i.get("severity") == "error"]
        warnings = [i for i in issues if i.get("severity") == "warning"]
        infos = [i for i in issues if i.get("severity") == "info"]

        if errors:
            print("\n  Errors:")
            for issue in errors:
                print(f"    [E] [{issue.get('category', 'unknown')}] {issue.get('message', '')}")
                if issue.get('suggestion'):
                    print(f"        -> {issue.get('suggestion')}")

        if warnings:
            print("\n  Warnings:")
            for issue in warnings:
                print(f"    [!] [{issue.get('category', 'unknown')}] {issue.get('message', '')}")
                if issue.get('suggestion'):
                    print(f"        -> {issue.get('suggestion')}")

        if infos:
            print("\n  Info:")
            for issue in infos:
                print(f"    [i] [{issue.get('category', 'unknown')}] {issue.get('message', '')}")
                if issue.get('suggestion'):
                    print(f"        -> {issue.get('suggestion')}")
    else:
        print(f"\n{'-' * 70}")
        print("  All checks passed - spec system is healthy!")

    # Summary
    summary = results.get("summary", {})
    print(f"\n{'-' * 70}")
    print(f"Summary: {summary.get('errors', 0)} errors, {summary.get('warnings', 0)} warnings, {summary.get('info', 0)} info")

    if results.get("healthy"):
        print("Status: HEALTHY")
    else:
        print("Status: NEEDS ATTENTION")

    print(f"{'=' * 70}\n")


def print_json_report(results: Dict[str, Any]) -> None:
    """Print JSON health report."""
    print(json.dumps(results, indent=2, default=str))


def is_test_change(change_name: str) -> bool:
    """Check if a change name indicates a test/experimental change.

    Test changes are identified by:
    - Starting with 'test' or 'tmp'
    - Containing only generic words like 'test', 'tmp', 'temp', 'fix'
    - Being very short (< 8 characters)
    """
    name_lower = change_name.lower()

    # Explicit test prefixes
    if name_lower.startswith(("test", "tmp", "temp-")):
        return True

    # Generic-only names (no meaningful descriptor)
    generic_words = {"test", "tmp", "temp", "fix", "update", "change", "work"}
    parts = name_lower.replace("-", "_").split("_")
    meaningful_parts = [p for p in parts if len(p) > 2 and p not in generic_words]

    if not meaningful_parts and len(change_name) < 15:
        return True

    return False


@app.callback()
def health(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    stale_days: int = typer.Option(30, "--stale-days", "-s", help="Days before a change is considered stale"),
    include_archived: bool = typer.Option(False, "--include-archived", "-a", help="Include archived changes in checks"),
    strict: bool = typer.Option(False, "--strict", help="Treat naming/info issues as warnings (for CI)"),
    fail_on_warning: bool = typer.Option(False, "--fail-on-warning", "-w", help="Exit with error if any warnings found"),
    skip_test_changes: bool = typer.Option(True, "--skip-test-changes/--include-test-changes", help="Skip test/experimental changes in checks"),
):
    """Check SE3 spec system health and integrity.

    Detects common issues in the spec system:
    - Zombie changes (inactive, no progress)
    - Old format changes (missing .openspec.yaml)
    - Completed but unarchived changes
    - Stale changes (no activity for specified days)
    - Naming convention violations
    - Directory structure drift

    Examples:
        se3 health
        se3 health --format json
        se3 health --stale-days 14
        se3 health --include-archived
        se3 health --strict              # CI mode: fail on naming issues
        se3 health --fail-on-warning     # Exit error if any warnings
    """
    results = run_health_check(project_root, stale_days, include_archived, skip_test_changes)

    # Apply strict mode: upgrade naming issues from info to warning
    if strict:
        for issue in results.get("issues", []):
            if issue.get("category") == "naming" and issue.get("severity") == "info":
                issue["severity"] = "warning"

        # Recalculate health status
        warnings = [i for i in results.get("issues", []) if i.get("severity") == "warning"]
        errors = [i for i in results.get("issues", []) if i.get("severity") == "error"]
        results["healthy"] = len(errors) == 0 and len(warnings) == 0
        results["summary"]["warnings"] = len(warnings)
        results["summary"]["info"] = len([i for i in results.get("issues", []) if i.get("severity") == "info"])

    if format == "json":
        print_json_report(results)
    else:
        print_text_report(results)

    # Exit code: 0 = healthy, 1 = has warnings/issues
    is_healthy = results["healthy"]
    if fail_on_warning and results["summary"].get("warnings", 0) > 0:
        is_healthy = False

    raise typer.Exit(code=0 if is_healthy else 1)


def main():
    """Main entry point for the health command."""
    app()


if __name__ == "__main__":
    main()
