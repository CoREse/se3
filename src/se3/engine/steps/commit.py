"""Commit step handler.

Commits the changes using git.
Integrates with VersionBumper for automatic version bumping.
Uses version analysis and commit message from the version_analyze step.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..models import FlowInstance, Step, StepStatus
from ..version_bumper import BumpType, TaskType, VersionBumper, VersionConfig

logger = logging.getLogger(__name__)


def commit_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the commit step.

    Commits changes using git commands. If version bumping is enabled,
    bumps the version before committing and includes the new version
    in the commit message.
    
    Uses the bump_type from version_analyze step if available, otherwise
    falls back to task type based bump rules.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Check if there are changes to commit
    baseline_commit = getattr(flow, "baseline_commit", None)
    if not _has_changes(project_root, baseline_commit=baseline_commit):
        logger.info("No changes to commit")
        step.outputs["commit_hash"] = "no-changes"
        step.outputs["committed"] = False
        return StepStatus.COMPLETED

    # Load version bumping configuration
    version_config = _load_version_config(project_root)

    # Initialize version bumping state
    version_bumper: VersionBumper | None = None
    version_file: Path | None = None
    original_version: str | None = None
    new_version: str | None = None
    version_bumped = False

    try:
        # Attempt version bumping if enabled
        if version_config.enabled:
            version_bumper = VersionBumper(version_config)
            version_file = version_bumper.detect_version_file(project_root)

            if version_file:
                # Get bump type from version_analyze step or fallback to task type
                bump_type = _get_bump_type(step, flow, version_config)

                try:
                    # Save original version for potential rollback
                    original_version = version_bumper.read_version(version_file)
                except (ValueError, KeyError, RuntimeError):
                    # File exists but has no readable version — auto-repair
                    logger.warning(
                        f"Version file {version_file} exists but has no readable version. "
                        "Attempting auto-repair."
                    )
                    if version_bumper._use_script_mode and version_bumper._script_runner:
                        # Script mode: regenerate version script
                        logger.info("Script mode detected, regenerating version script.")
                        from ..version_script_interface import generate_version_script
                        generate_version_script(project_root)
                    else:
                        # File mode: reinitialize version system
                        logger.info("File mode detected, reinitializing version system.")
                        version_file = version_bumper.initialize_version_system(
                            project_root=project_root,
                            initial_version="0.1.0"
                        )
                    # Retry — let any exception propagate normally
                    original_version = version_bumper.read_version(version_file)

                # Bump the version
                new_version = version_bumper.bump_version(
                    path=version_file,
                    bump_type=bump_type
                )
                version_bumped = True
                logger.info(f"Bumped version: {original_version} -> {new_version}")

                # Stage the version file
                _stage_file(project_root, version_file)
            else:
                # No version file exists - initialize version system
                logger.info("No version file detected, initializing version system")
                try:
                    version_file = version_bumper.initialize_version_system(
                        project_root=project_root,
                        initial_version="0.1.0"
                    )
                    logger.info(f"Created version file: {version_file}")

                    # Now get the bump type and bump the version
                    bump_type = _get_bump_type(step, flow, version_config)

                    # Save original version for potential rollback
                    try:
                        original_version = version_bumper.read_version(version_file)
                    except (ValueError, KeyError, RuntimeError):
                        logger.warning(
                            f"Freshly created version file {version_file} is not readable. "
                            "Attempting auto-repair."
                        )
                        if version_bumper._use_script_mode and version_bumper._script_runner:
                            logger.info("Script mode detected, regenerating version script.")
                            from ..version_script_interface import generate_version_script
                            generate_version_script(project_root)
                        else:
                            logger.info("File mode detected, reinitializing version system.")
                            version_file = version_bumper.initialize_version_system(
                                project_root=project_root,
                                initial_version="0.1.0"
                            )
                        # Retry — let any exception propagate normally
                        original_version = version_bumper.read_version(version_file)

                    # Bump the version
                    new_version = version_bumper.bump_version(
                        path=version_file,
                        bump_type=bump_type
                    )
                    version_bumped = True
                    logger.info(f"Bumped version: {original_version} -> {new_version}")

                    # Stage the new version file
                    _stage_file(project_root, version_file)
                except Exception as e:
                    logger.error(f"Failed to initialize version system: {e}")
                    raise

        # Generate commit message (including version if bumped)
        commit_message = _generate_commit_message(flow, step, new_version, version_config)

        logger.info(f"Committing changes with message: {commit_message[:60]}...")

        # Add all changes
        result = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        if result.returncode != 0:
            # Rollback version if staging failed
            if version_bumped and version_bumper and version_file and original_version:
                _rollback_version(version_bumper, version_file, original_version)
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
            # Rollback version if commit failed
            if version_bumped and version_bumper and version_file and original_version:
                _rollback_version(version_bumper, version_file, original_version)
            step.error_message = f"Failed to commit: {result.stderr}"
            return StepStatus.FAILED

        # Get commit hash
        commit_hash = _get_commit_hash(project_root)

        # Clear version backup on successful commit (make bump permanent)
        if version_bumper:
            version_bumper.clear_backup()

        # Store outputs
        step.outputs["commit_hash"] = commit_hash
        step.outputs["committed"] = True
        step.outputs["commit_message"] = commit_message
        if new_version:
            step.outputs["version"] = new_version
            step.outputs["version_bumped"] = True

        logger.info(f"Changes committed: {commit_hash[:8]}")

        return StepStatus.COMPLETED

    except Exception as e:
        # Rollback version on any exception
        if version_bumped and version_bumper and version_file and original_version:
            try:
                _rollback_version(version_bumper, version_file, original_version)
            except Exception as rollback_error:
                logger.error(f"Failed to rollback version: {rollback_error}")

        logger.exception("Commit step failed")
        step.error_message = f"Failed to commit: {str(e)}"
        return StepStatus.FAILED


def _load_version_config(project_root: Path) -> VersionConfig:
    """Load version bumping configuration.

    Args:
        project_root: Project root directory

    Returns:
        VersionConfig instance
    """
    # Import here to avoid circular imports
    from ...config import load_version_config as load_cfg
    return load_cfg(project_root)


def _get_bump_type(step: Step, flow: FlowInstance, version_config: VersionConfig) -> BumpType:
    """Determine bump type from version_analyze step or fallback to task type.
    
    First checks if version_analyze step provided a bump_type, then falls back
    to the task type based bump rules from configuration.

    Args:
        step: The current step (may have bump_type in inputs)
        flow: The flow instance
        version_config: Version configuration with bump rules

    Returns:
        BumpType enum value - always returns a valid BumpType, never None
    """
    # First, try to get bump_type from version_analyze step input
    bump_type_str = step.inputs.get("bump_type")
    confidence = step.inputs.get("confidence", "low")

    if bump_type_str:
        logger.info(f"Using version_analyze result: bump_type={bump_type_str}, confidence={confidence}")
        try:
            return BumpType(bump_type_str) if bump_type_str != "none" else BumpType.PATCH
        except ValueError:
            logger.warning(f"Invalid bump_type from version_analyze: {bump_type_str}, falling back")
    
    # Fallback to task type based bump rules
    task_type = flow.task_type or "feature"
    bump_type_str = version_config.bump_rules.get(task_type, "patch")

    # Always return a valid BumpType - never None or skip
    try:
        return BumpType(bump_type_str)
    except ValueError:
        return BumpType.PATCH


def _get_task_type(flow: FlowInstance) -> str:
    """Determine task type from flow context.

    Args:
        flow: The flow instance

    Returns:
        Task type string (feature, bugfix, small, etc.)
    """
    return flow.task_type or "feature"


def _stage_file(project_root: Path, file_path: Path) -> None:
    """Stage a specific file for commit.

    Args:
        project_root: Project root directory
        file_path: Path to the file to stage
    """
    # Get relative path if file is within project root
    try:
        rel_path = file_path.relative_to(project_root)
    except ValueError:
        rel_path = file_path

    result = subprocess.run(
        ["git", "add", str(rel_path)],
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to stage {rel_path}: {result.stderr}")


def _rollback_version(
    version_bumper: VersionBumper,
    version_file: Path,
    original_version: str
) -> None:
    """Rollback version to original value.

    Args:
        version_bumper: VersionBumper instance
        version_file: Path to version file
        original_version: Original version string to restore
    """
    logger.warning(f"Rolling back version to {original_version}")
    try:
        version_bumper.rollback()
        logger.info(f"Version rolled back to {original_version}")
    except Exception as e:
        logger.error(f"Version rollback failed: {e}")
        raise


def _has_changes(project_root: Path, baseline_commit: str | None = None) -> bool:
    """Check if there are code changes to commit.

    When a baseline_commit is provided, uses ``git diff`` to compare the
    baseline against HEAD.  This correctly detects changes in multi-worktree
    scenarios where commits have been merged but the working tree is clean.

    Falls back to ``git status --porcelain`` when no baseline is available
    (backward compatibility).

    Args:
        project_root: Project root directory
        baseline_commit: Optional baseline commit hash to diff against HEAD

    Returns:
        True if there are changes to commit
    """
    # When a baseline commit is available, compare it against HEAD.
    if baseline_commit:
        try:
            result = subprocess.run(
                ["git", "diff", baseline_commit, "HEAD", "--quiet"],
                capture_output=True,
                text=True,
                cwd=project_root,
            )
            # --quiet: exit code 0 means no diff, 1 means there are diffs
            if result.returncode == 1:
                return True
            if result.returncode == 0:
                # No diff between baseline and HEAD; still check working tree
                # in case there are unstaged/uncommitted changes on top.
                pass
            # returncode > 1 indicates an error (e.g. bad commit ref) — fall through
        except Exception:
            pass

    # Fallback: check working tree for uncommitted changes
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



def _generate_commit_message(
    flow: FlowInstance,
    step: Step,
    new_version: str | None = None,
    version_config: VersionConfig | None = None
) -> str:
    """Generate a commit message based on the flow context.

    Priority chain for the subject line:
    1. commit_message from version_analyze step (via step.inputs)
    2. proposal summary from plan step
    3. implement_summary from implement step
    4. Template fallback from task description

    Args:
        flow: The flow instance
        step: The current step
        new_version: Optional new version string if version was bumped
        version_config: Version configuration

    Returns:
        Commit message string
    """
    task_type = flow.task_type or "feature"
    task_description = step.inputs.get("task_description", flow.task_description) or ""

    # Get inputs from previous steps
    changes_made = step.inputs.get("changes_made") or {}
    proposal = step.inputs.get("proposal") or {}

    # Get completion status from implement step (defaults for backward compatibility)
    completion_status = step.inputs.get("completion_status", "complete")
    incomplete_tasks = step.inputs.get("incomplete_tasks", [])
    implement_summary = step.inputs.get("implement_summary", "")

    # Priority 1: commit_message from version_analyze
    commit_msg_from_va = step.inputs.get("commit_message", "")
    if commit_msg_from_va:
        first_line = commit_msg_from_va.strip()
        if len(first_line) > 72:
            first_line = first_line[:69] + "..."
        message = f"{task_type}: {first_line}"
    else:
        # Priority 2: proposal summary, Priority 3: implement_summary
        summary = proposal.get("summary", "") or implement_summary
        if summary:
            first_line = summary.split(".")[0]
            if len(first_line) > 72:
                first_line = first_line[:69] + "..."
            message = f"{task_type}: {first_line}"
        else:
            # Priority 4: template fallback from task description
            desc = task_description[:60] if len(task_description) > 60 else task_description
            message = f"{task_type}: {desc}"

    # Add context about the change
    files_changed = changes_made.get("files_changed", [])
    if files_changed:
        file_paths = []
        for f in files_changed[:3]:
            if isinstance(f, str):
                file_paths.append(f)
            elif isinstance(f, dict):
                file_paths.append(f.get("path", "?"))
            else:
                file_paths.append(str(f))
        file_list = ", ".join(file_paths)
        if len(files_changed) > 3:
            file_list += f" and {len(files_changed) - 3} more"

        message += f"\n\nFiles: {file_list}"

    # Add incomplete tasks section when partial completion
    if completion_status == "partial" and incomplete_tasks:
        message += "\n\nIncomplete tasks (partial completion):"
        for task in incomplete_tasks:
            if isinstance(task, str):
                message += f"\n- {task}"
            elif isinstance(task, dict):
                desc = task.get("description", task.get("task", str(task)))
                reason = task.get("reason", "")
                message += f"\n- {desc}"
                if reason:
                    message += f" ({reason})"

    # Add version information if bumping occurred and is configured to include
    include_version = (
        version_config is None or
        version_config.include_in_commit_message
    )
    if new_version and include_version:
        message += f"\n\nVersion: {new_version}"

    # Add flow reference
    message += f"\n\nFlow: {flow.flow_id}"

    return message


def _get_commit_hash(project_root: Path) -> str:
    """Get the current commit hash.

    Returns ``'unknown'`` on repos with no commits or on any git failure.

    Args:
        project_root: Project root directory

    Returns:
        Commit hash string, or ``'unknown'`` if unavailable
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"
