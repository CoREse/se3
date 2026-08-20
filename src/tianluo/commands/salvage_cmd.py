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

from ..i18n import t

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
            console.print(t("salvage.no_project_root"))
            return 1

    project_root = Path(project_root)

    # WHY: the root can sit above cwd (run from a subdirectory), and the caller
    # bound the language before auto-detection could happen — re-bind now that
    # the operating project is settled so output honours its language.language.
    from ..i18n import bind_project_root

    bind_project_root(project_root)
    results: List[Tuple[str, str, str]] = []  # (step_name, status, detail)

    # Step 1: Read session state (tolerant)
    flow = None
    warnings: List[str] = []
    try:
        flow, warnings = _load_session(project_root)
        if flow:
            results.append((t("salvage.step.read_session"), "OK", t("salvage.detail.flow", flow_id=flow.flow_id)))
        else:
            results.append((t("salvage.step.read_session"), "SKIP", t("salvage.detail.no_session_git_diff")))
    except Exception as e:
        results.append((t("salvage.step.read_session"), "FAIL", str(e)[:80]))
        logger.warning(f"Step 1 (read session) failed: {e}")

    for w in warnings:
        logger.info(f"Session load warning: {w}")

    # Step 2: Assess git diff
    diff_info: Dict[str, Any] = {}
    try:
        diff_info = _assess_git_diff(project_root)
        file_count = diff_info.get("changed_file_count", 0)
        if file_count > 0:
            results.append((t("salvage.step.assess_git_diff"), "OK", t("salvage.detail.files_changed", count=file_count)))
        else:
            results.append((t("salvage.step.assess_git_diff"), "OK", t("salvage.detail.no_uncommitted_changes")))
    except Exception as e:
        results.append((t("salvage.step.assess_git_diff"), "FAIL", str(e)[:80]))
        logger.warning(f"Step 2 (assess git diff) failed: {e}")

    # Step 3: Commit changes
    commit_hash = None
    try:
        commit_hash = _commit_changes(project_root, flow, diff_info)
        if commit_hash:
            results.append((t("salvage.step.commit_changes"), "OK", t("salvage.detail.committed", hash=commit_hash[:8])))
        else:
            results.append((t("salvage.step.commit_changes"), "SKIP", t("salvage.detail.nothing_to_commit")))
    except Exception as e:
        results.append((t("salvage.step.commit_changes"), "FAIL", str(e)[:80]))
        logger.warning(f"Step 3 (commit changes) failed: {e}")

    # Step 4: Create salvage issues
    created_issues: List[Any] = []
    try:
        created_issues = _create_salvage_issues(project_root, flow, diff_info)
        if created_issues:
            ids = ", ".join(i.id for i in created_issues)
            results.append((t("salvage.step.create_issues"), "OK", t("salvage.detail.created", ids=ids)))
        else:
            results.append((t("salvage.step.create_issues"), "SKIP", t("salvage.detail.no_issues")))
    except Exception as e:
        results.append((t("salvage.step.create_issues"), "FAIL", str(e)[:80]))
        logger.warning(f"Step 4 (create issues) failed: {e}")

    # Step 5: Archive session
    try:
        archived = _archive_session(project_root)
        if archived:
            results.append((t("salvage.step.archive_session"), "OK", t("salvage.detail.session_archived")))
        else:
            results.append((t("salvage.step.archive_session"), "SKIP", t("salvage.detail.no_session_to_archive")))
    except Exception as e:
        results.append((t("salvage.step.archive_session"), "FAIL", str(e)[:80]))
        logger.warning(f"Step 5 (archive session) failed: {e}")

    # Display results
    _display_results(results)

    has_failure = any(status == "FAIL" for _, status, _ in results)
    return 1 if has_failure else 0


def _find_project_root() -> Optional[Path]:
    """Find project root by looking for .git or an SE3 config file."""
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or is_se3_project_root(p):
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
            source="system",
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
            source="system",
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
    from ..engine.review_scope import discard_flow_snapshots

    pm = PersistenceManager(project_root)
    if pm.state_file.exists():
        # Capture the flow_id before archiving so we can also drop the per-flow
        # resumable snapshot. A salvaged flow has been dispositioned (changes
        # committed, unfinished work turned into issues, session archived) and
        # MUST NOT remain resumable; mirroring save_flow's clear-on-completion
        # keeps the daemon STATUS_UPDATE channel (which seeds its dedup set only
        # from active engine.json flow_ids) and the history-index channel
        # consistent — otherwise the archived flow would still carry a lingering
        # resumable/<flow_id>.json and surface a Resume button in one view but
        # not the other.
        flow_id = None
        try:
            flow = pm.load_flow()
            if flow is not None:
                flow_id = flow.flow_id
        except Exception:
            flow_id = None
        pm.clear_state()
        if flow_id:
            pm.clear_resumable_snapshot(flow_id)
            # A salvaged flow has been dispositioned and can never be resumed,
            # so its review baselines have no reader left; reclaim them on the
            # same signal that retires the resumable snapshot.
            discard_flow_snapshots(project_root, flow_id)
        return True
    return False


def _display_results(results: List[Tuple[str, str, str]]) -> None:
    """Display salvage results as a Rich table.

    Args:
        results: List of (step_name, status, detail)
    """
    table = Table(title=t("salvage.table.title"))
    table.add_column(t("salvage.table.col_step"), style="cyan")
    table.add_column(t("salvage.table.col_status"), style="bold")
    table.add_column(t("salvage.table.col_detail"))

    status_styles = {
        "OK": t("salvage.status.ok"),
        "SKIP": t("salvage.status.skip"),
        "FAIL": t("salvage.status.fail"),
    }

    for step_name, status, detail in results:
        styled_status = status_styles.get(status, status)
        table.add_row(step_name, styled_status, detail)

    console.print()
    console.print(table)
    console.print()
