"""Commit step handler.

Commits the changes using git.
This is a non-LLM step that executes git commands.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def commit_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the commit step.

    Commits changes using git commands.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Check if there are changes to commit
    if not _has_changes(project_root):
        logger.info("No changes to commit")
        step.outputs["commit_hash"] = "no-changes"
        step.outputs["committed"] = False
        return StepStatus.COMPLETED

    # Generate commit message
    commit_message = _generate_commit_message(flow, step)

    logger.info(f"Committing changes with message: {commit_message[:60]}...")

    try:
        # Add all changes
        result = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        if result.returncode != 0:
            step.error_message = f"Failed to stage changes: {result.stderr}"
            return StepStatus.FAILED

        # Commit
        result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        if result.returncode != 0:
            step.error_message = f"Failed to commit: {result.stderr}"
            return StepStatus.FAILED

        # Get commit hash
        commit_hash = _get_commit_hash(project_root)

        # Store outputs
        step.outputs["commit_hash"] = commit_hash
        step.outputs["committed"] = True
        step.outputs["commit_message"] = commit_message

        logger.info(f"Changes committed: {commit_hash[:8]}")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Commit step failed")
        step.error_message = f"Failed to commit: {str(e)}"
        return StepStatus.FAILED


def _has_changes(project_root: Path) -> bool:
    """Check if there are uncommitted changes.

    Args:
        project_root: Project root directory

    Returns:
        True if there are changes to commit
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        return len(result.stdout.strip()) > 0
    except Exception:
        return False


def _generate_commit_message(flow: FlowInstance, step: Step) -> str:
    """Generate a commit message based on the flow context.

    Args:
        flow: The flow instance
        step: The current step

    Returns:
        Commit message string
    """
    task_type = flow.task_type or "feature"
    task_description = flow.task_description or ""

    # Get inputs from previous steps
    changes_made = step.inputs.get("changes_made") or {}
    proposal = step.inputs.get("proposal") or {}

    # Use proposal summary if available
    summary = proposal.get("summary", "")
    if summary:
        # Use first sentence or first 50 chars
        first_line = summary.split(".")[0]
        if len(first_line) > 72:
            first_line = first_line[:69] + "..."
        message = f"{task_type}: {first_line}"
    else:
        # Use task description
        desc = task_description[:60] if len(task_description) > 60 else task_description
        message = f"{task_type}: {desc}"

    # Add context about the change
    files_changed = changes_made.get("files_changed", [])
    if files_changed:
        file_list = ", ".join(f.get("path", "") for f in files_changed[:3])
        if len(files_changed) > 3:
            file_list += f" and {len(files_changed) - 3} more"

        message += f"\n\nFiles: {file_list}"

    # Add flow reference
    message += f"\n\nFlow: {flow.flow_id}"

    return message


def _get_commit_hash(project_root: Path) -> str:
    """Get the current commit hash.

    Args:
        project_root: Project root directory

    Returns:
        Commit hash string
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"
