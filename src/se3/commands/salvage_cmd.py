"""SE3 Salvage command — rescue work from abnormally terminated sessions.

Performs a best-effort recovery: reads session state tolerantly, evaluates
git diff, commits existing changes, creates issues for unfinished work,
and archives the session. Each step is independently fault-tolerant.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()


def salvage(project_root: Optional[Path] = None) -> int:
    """Execute the salvage pipeline.

    Each step runs independently with its own error handling.

    Args:
        project_root: Project root directory. Auto-detected if None.

    Returns:
        Exit code (0 = success, 1 = partial/failure)
    """
    if project_root is None:
        project_root = _find_project_root()
        if project_root is None:
            console.print("[red]Could not find project root (no .git or se3.yaml found)[/red]")
            return 1

    project_root = Path(project_root)
    results: List[Tuple[str, str, str]] = []  # (step_name, status, detail)

    # Step 1: Read session state (tolerant)
    flow = None
    warnings: List[str] = []
    try:
        flow, warnings = _load_session(project_root)
        if flow:
            results.append(("Read session", "OK", f"Flow {flow.flow_id}"))
        else:
            results.append(("Read session", "SKIP", "No session found, using git diff"))
    except Exception as e:
        results.append(("Read session", "FAIL", str(e)[:80]))
        logger.warning(f"Step 1 (read session) failed: {e}")

    for w in warnings:
        logger.info(f"Session load warning: {w}")

    # Step 2: Assess git diff
    diff_info: Dict[str, Any] = {}
    try:
        diff_info = _assess_git_diff(project_root)
        file_count = diff_info.get("changed_file_count", 0)
        if file_count > 0:
            results.append(("Assess git diff", "OK", f"{file_count} files changed"))
        else:
            results.append(("Assess git diff", "OK", "No uncommitted changes"))
    except Exception as e:
        results.append(("Assess git diff", "FAIL", str(e)[:80]))
        logger.warning(f"Step 2 (assess git diff) failed: {e}")

    # Step 3: Commit changes
    commit_hash = None
    try:
        commit_hash = _commit_changes(project_root, flow, diff_info)
        if commit_hash:
            results.append(("Commit changes", "OK", f"Committed: {commit_hash[:8]}"))
        else:
            results.append(("Commit changes", "SKIP", "Nothing to commit"))
    except Exception as e:
        results.append(("Commit changes", "FAIL", str(e)[:80]))
        logger.warning(f"Step 3 (commit changes) failed: {e}")

    # Step 4: Create salvage issues
    created_issues: List[Any] = []
    try:
        created_issues = _create_salvage_issues(project_root, flow, diff_info)
        if created_issues:
            ids = ", ".join(i.id for i in created_issues)
            results.append(("Create issues", "OK", f"Created: {ids}"))
        else:
            results.append(("Create issues", "SKIP", "No issues to create"))
    except Exception as e:
        results.append(("Create issues", "FAIL", str(e)[:80]))
        logger.warning(f"Step 4 (create issues) failed: {e}")

    # Step 5: Archive session
    try:
        archived = _archive_session(project_root)
        if archived:
            results.append(("Archive session", "OK", "Session archived"))
        else:
            results.append(("Archive session", "SKIP", "No session to archive"))
    except Exception as e:
        results.append(("Archive session", "FAIL", str(e)[:80]))
        logger.warning(f"Step 5 (archive session) failed: {e}")

    # Display results
    _display_results(results)

    has_failure = any(status == "FAIL" for _, status, _ in results)
    return 1 if has_failure else 0


def _find_project_root() -> Optional[Path]:
    """Find project root by looking for .git or se3.yaml."""
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or (p / "se3.yaml").exists():
            return p
    return None


def _load_session(project_root: Path) -> Tuple[Optional[Any], List[str]]:
    """Tolerantly load the session state.

    Args:
        project_root: Project root

    Returns:
        Tuple of (FlowInstance or None, warnings)
    """
    from ..engine.persistence import PersistenceManager

    pm = PersistenceManager(project_root)
    return pm.load_flow_tolerant()


def _assess_git_diff(project_root: Path) -> Dict[str, Any]:
    """Assess uncommitted changes via git.

    Args:
        project_root: Project root

    Returns:
        Dict with diff info (stat, changed_file_count, diff_summary)
    """
    info: Dict[str, Any] = {}

    # git status
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=project_root,
    )
    status_lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    info["status_lines"] = status_lines
    info["changed_file_count"] = len(status_lines)

    # git diff --stat
    result = subprocess.run(
        ["git", "diff", "--stat"],
        capture_output=True, text=True, cwd=project_root,
    )
    info["diff_stat"] = result.stdout.strip()

    # git diff (truncated for issue description)
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        capture_output=True, text=True, cwd=project_root,
    )
    full_diff = result.stdout
    info["diff_summary"] = full_diff[:4000] if full_diff else ""

    # Changed file list
    info["changed_files"] = []
    for line in status_lines:
        if len(line) >= 3:
            info["changed_files"].append(line[3:].strip())

    return info


def _commit_changes(
    project_root: Path,
    flow: Optional[Any],
    diff_info: Dict[str, Any],
) -> Optional[str]:
    """Commit uncommitted changes with a salvage message.

    Args:
        project_root: Project root
        flow: FlowInstance (may be None)
        diff_info: Git diff info

    Returns:
        Commit hash if committed, None if nothing to commit
    """
    if diff_info.get("changed_file_count", 0) == 0:
        return None

    # Stage all changes
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, cwd=project_root,
    )

    # Build commit message
    task_desc = ""
    if flow:
        task_desc = flow.task_description[:80]
    else:
        task_desc = "unknown task"

    file_count = diff_info.get("changed_file_count", 0)
    commit_msg = f"[salvage] {task_desc}\n\nSalvage commit: {file_count} files from interrupted session."

    result = subprocess.run(
        ["git", "commit", "-m", commit_msg, "--no-verify"],
        capture_output=True, text=True, cwd=project_root,
    )

    if result.returncode != 0:
        # Maybe nothing to commit after all
        if "nothing to commit" in result.stdout + result.stderr:
            return None
        logger.warning(f"Git commit failed: {result.stderr}")
        return None

    # Get commit hash
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=project_root,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _create_salvage_issues(
    project_root: Path,
    flow: Optional[Any],
    diff_info: Dict[str, Any],
) -> List[Any]:
    """Create issues for unfinished work.

    Args:
        project_root: Project root
        flow: FlowInstance (may be None)
        diff_info: Git diff info

    Returns:
        List of created Issues
    """
    from ..engine.issue_manager import IssueManager

    mgr = IssueManager(project_root)
    issues = []

    # Build issue from flow state
    if flow:
        title = f"Incomplete: {flow.task_description[:80]}"
        desc_parts = [
            f"Session interrupted while working on: {flow.task_description}",
            "",
        ]

        # Completed steps
        completed_steps = []
        current_step_type = None
        for step_id in flow.state.step_history:
            step = flow.state.steps.get(step_id)
            if step:
                status_str = step.status.value if hasattr(step.status, "value") else str(step.status)
                step_type_str = step.step_type.value if hasattr(step.step_type, "value") else str(step.step_type)
                completed_steps.append(f"- {step_type_str}: {status_str}")
                if step.step_id == flow.state.current_step_id:
                    current_step_type = step_type_str

        if completed_steps:
            desc_parts.append("**Step history:**")
            desc_parts.extend(completed_steps)
            desc_parts.append("")

        if current_step_type:
            desc_parts.append(f"**Interrupted at step:** {current_step_type}")
            desc_parts.append("")

        # Diff summary
        if diff_info.get("changed_files"):
            desc_parts.append("**Changed files:**")
            for f in diff_info["changed_files"][:20]:
                desc_parts.append(f"- {f}")
            if len(diff_info["changed_files"]) > 20:
                desc_parts.append(f"- ... and {len(diff_info['changed_files']) - 20} more")

        issue = mgr.create(
            title=title,
            description="\n".join(desc_parts),
            priority="medium",
            tags=["auto-discovered", "source:salvage"],
        )
        issues.append(issue)

    elif diff_info.get("changed_file_count", 0) > 0:
        # No flow state, but there are changes
        title = "Incomplete: interrupted session (no session state)"
        desc_parts = [
            "Session interrupted with uncommitted changes but no readable session state.",
            "",
            "**Changed files:**",
        ]
        for f in diff_info.get("changed_files", [])[:20]:
            desc_parts.append(f"- {f}")

        issue = mgr.create(
            title=title,
            description="\n".join(desc_parts),
            priority="medium",
            tags=["auto-discovered", "source:salvage"],
        )
        issues.append(issue)

    return issues


def _archive_session(project_root: Path) -> bool:
    """Archive the current session state.

    Args:
        project_root: Project root

    Returns:
        True if archived, False if nothing to archive
    """
    from ..engine.persistence import PersistenceManager

    pm = PersistenceManager(project_root)
    if pm.state_file.exists():
        pm.clear_state()
        return True
    return False


def _display_results(results: List[Tuple[str, str, str]]) -> None:
    """Display salvage results as a Rich table.

    Args:
        results: List of (step_name, status, detail)
    """
    table = Table(title="Salvage Results")
    table.add_column("Step", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Detail")

    status_styles = {
        "OK": "[green]OK[/green]",
        "SKIP": "[yellow]SKIP[/yellow]",
        "FAIL": "[red]FAIL[/red]",
    }

    for step_name, status, detail in results:
        styled_status = status_styles.get(status, status)
        table.add_row(step_name, styled_status, detail)

    console.print()
    console.print(table)
    console.print()
