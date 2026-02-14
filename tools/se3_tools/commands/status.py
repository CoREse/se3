"""Status diagnostics command for SE 3.0 tools.

Implements the status-diagnostics spec:
- Parses status.md
- Checks consistency against project state
- Checks human-calls directory
- Provides diagnostic output in text or JSON format
"""

# Verify: status-diagnostics/Find status file
# Verify: status-diagnostics/Mismatched active change
# Verify: status-diagnostics/Stale blockers
# Verify: status-diagnostics/Unprocessed response
# Verify: status-diagnostics/Long-pending call
# Verify: status-diagnostics/Healthy project
# Verify: status-diagnostics/Issues found

import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from ..utils import parse_status_md, discover_human_calls, discover_changes


def check_active_change(status: Dict[str, Any], project_root: Path) -> Optional[Dict[str, Any]]:
    """Check if active change exists in openspec/changes/.

    Args:
        status: Parsed status dict
        project_root: Project root path

    Returns:
        Issue dict if problem found, None otherwise
    """
    active_change = status.get('active_change')

    if not active_change or active_change == '-':
        return None

    changes = discover_changes(str(project_root / "openspec" / "changes"))

    if active_change not in changes:
        return {
            'severity': 'error',
            'check': 'active_change',
            'message': f"Active change '{active_change}' does not exist in openspec/changes/",
            'suggestion': f"Available changes: {', '.join(changes) if changes else 'None'}"
        }

    return None


def check_blockers_consistency(status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Check if status field matches blockers table.

    Args:
        status: Parsed status dict

    Returns:
        Issue dict if problem found, None otherwise
    """
    status_value = status.get('status', '').lower()
    blockers = status.get('blockers', [])

    if status_value == 'ready' and blockers:
        return {
            'severity': 'warning',
            'check': 'blockers_consistency',
            'message': "Status is 'ready' but blockers table is not empty",
            'suggestion': "Clear blockers table or change status to 'blocked'"
        }

    if status_value == 'blocked' and not blockers:
        return {
            'severity': 'warning',
            'check': 'blockers_consistency',
            'message': "Status is 'blocked' but no blockers are listed",
            'suggestion': "Add blockers to table or change status to 'ready'"
        }

    return None


def check_git_status(project_root: Path) -> Optional[Dict[str, Any]]:
    """Check git status for uncommitted work.

    Args:
        project_root: Project root path

    Returns:
        Issue dict if problem found, None otherwise
    """
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=project_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return {
                'severity': 'warning',
                'check': 'git_status',
                'message': "Could not check git status",
                'suggestion': "Ensure this is a git repository"
            }

        uncommitted = result.stdout.strip()
        if uncommitted:
            lines = uncommitted.split('\n')
            return {
                'severity': 'info',
                'check': 'git_status',
                'message': f"{len(lines)} uncommitted change(s) in working directory",
                'suggestion': "Run 'git status' to see details"
            }

    except FileNotFoundError:
        return {
            'severity': 'warning',
            'check': 'git_status',
            'message': "Git not found",
            'suggestion': "Install git to enable git status checks"
        }

    return None


def check_human_calls(project_root: Path, timeout_days: int = 7) -> List[Dict[str, Any]]:
    """Check human-calls directory for pending/responded files.

    Args:
        project_root: Project root path
        timeout_days: Days after which a pending call is considered stale

    Returns:
        List of issue dicts
    """
    issues = []
    calls = discover_human_calls(str(project_root / "human-calls"))

    for call in calls:
        status = call.get('status')

        if status == 'pending':
            created = call.get('created')
            if created:
                try:
                    created_date = datetime.strptime(created, '%Y-%m-%d')
                    if datetime.now() - created_date > timedelta(days=timeout_days):
                        issues.append({
                            'severity': 'warning',
                            'check': 'human_calls',
                            'message': f"Long-pending human call: {call['file']} ({created})",
                            'suggestion': "Follow up on the pending request or close it"
                        })
                    else:
                        issues.append({
                            'severity': 'info',
                            'check': 'human_calls',
                            'message': f"Pending human call: {call['file']}",
                            'suggestion': "Awaiting human response"
                        })
                except ValueError:
                    issues.append({
                        'severity': 'info',
                        'check': 'human_calls',
                        'message': f"Pending human call: {call['file']}",
                        'suggestion': "Awaiting human response"
                    })

        elif status == 'responded':
            issues.append({
                'severity': 'warning',
                'check': 'human_calls',
                'message': f"Unprocessed response: {call['file']}",
                'suggestion': "Process the response and update the call status"
            })

    return issues


def run_diagnostics(project_root: str = ".") -> Dict[str, Any]:
    """Run all diagnostics checks.

    Args:
        project_root: Root directory of the project

    Returns:
        Dict with diagnostic results
    """
    root = Path(project_root).resolve()

    # Parse status.md
    status = parse_status_md(str(root / "status.md"))

    issues = []

    # Check active change
    issue = check_active_change(status, root)
    if issue:
        issues.append(issue)

    # Check blockers consistency
    issue = check_blockers_consistency(status)
    if issue:
        issues.append(issue)

    # Check git status
    issue = check_git_status(root)
    if issue:
        issues.append(issue)

    # Check human calls
    issues.extend(check_human_calls(root))

    # Count by severity
    errors = [i for i in issues if i['severity'] == 'error']
    warnings = [i for i in issues if i['severity'] == 'warning']
    infos = [i for i in issues if i['severity'] == 'info']

    return {
        'healthy': len(errors) == 0 and len(warnings) == 0,
        'status': status,
        'issues': issues,
        'summary': {
            'errors': len(errors),
            'warnings': len(warnings),
            'info': len(infos)
        }
    }


def print_text_report(results: Dict[str, Any]) -> None:
    """Print a human-readable diagnostic report.

    Args:
        results: Diagnostic results dict
    """
    print(f"\n{'=' * 60}")
    print("SE 3.0 Status Diagnostics")
    print(f"{'=' * 60}")

    status = results['status']

    print(f"\nCurrent Status:")
    print(f"  Active Change: {status.get('active_change', 'N/A')}")
    print(f"  Current Task: {status.get('current_task', 'N/A')}")
    print(f"  Status: {status.get('status', 'N/A')}")
    if status.get('blocked_since') and status['blocked_since'] != '-':
        print(f"  Blocked Since: {status['blocked_since']}")

    if status.get('blockers'):
        print(f"\n  Blockers:")
        for blocker in status['blockers']:
            print(f"    - {blocker['issue']} ({blocker['type']})")

    print(f"\n{'-' * 60}")
    print("Diagnostic Results:")
    print(f"{'-' * 60}")

    if results['healthy']:
        print("\n  ✓ All diagnostics passed")
    else:
        for issue in results['issues']:
            icon = '✗' if issue['severity'] == 'error' else '⚠' if issue['severity'] == 'warning' else 'ℹ'
            print(f"\n  {icon} [{issue['severity'].upper()}] {issue['check']}")
            print(f"     {issue['message']}")
            print(f"     Suggestion: {issue['suggestion']}")

    summary = results['summary']
    print(f"\n{'-' * 60}")
    print(f"Summary: {summary['errors']} errors, {summary['warnings']} warnings, {summary['info']} info")
    print(f"{'=' * 60}\n")


def print_json_report(results: Dict[str, Any]) -> None:
    """Print JSON diagnostic report.

    Args:
        results: Diagnostic results dict
    """
    import json
    print(json.dumps(results, indent=2, default=str))


def main(format: str = "text", project_root: str = ".") -> int:
    """Main entry point for status command.

    Args:
        format: Output format (text or json)
        project_root: Root directory of the project

    Returns:
        Exit code (0 = healthy, 1 = issues found)
    """
    results = run_diagnostics(project_root)

    if format == "json":
        print_json_report(results)
    else:
        print_text_report(results)

    return 0 if results['healthy'] else 1
