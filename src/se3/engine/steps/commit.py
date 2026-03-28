"""Commit step handler.

Commits the changes using git.
Integrates with VersionBumper for automatic version bumping.
Uses version analysis from the version_analyze step when available.
Generates descriptive commit messages using LLM when proposal summary is not available.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..version_bumper import BumpType, TaskType, VersionBumper, VersionConfig

logger = logging.getLogger(__name__)

# Prompt for generating concise commit message when proposal summary is not available
COMMIT_MESSAGE_PROMPT = """You are an expert at writing clear, concise git commit messages.

Task Type: {task_type}
Task Description: {task_description}

Changes Made:
{changes_text}

Instructions:
1. Write a concise commit message summary (first line only, max 72 characters)
2. Use imperative mood (e.g., "Add feature" not "Added feature")
3. Start with a verb describing the action
4. Be specific but brief
5. Do not include the task type prefix (like "feat:" or "fix:")
6. Output ONLY the commit message summary, no explanation

Examples:
- "Add user authentication with JWT tokens"
- "Fix memory leak in connection pool"
- "Refactor database query optimization"
- "Update API documentation for v2 endpoints"

Write a concise commit message summary:"""


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
    if not _has_changes(project_root):
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

                # Save original version for potential rollback
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
    
    # Check if we should use the LLM analysis result
    # Use it if confidence is sufficient or if auto_confirm is enabled
    if bump_type_str:
        # Get configuration for confirmation behavior
        auto_confirm = getattr(version_config, 'auto_bump', True)
        confidence_threshold = getattr(version_config, 'confidence_threshold', None)
        
        # Determine if we need human confirmation
        need_confirmation = False
        if not auto_confirm:
            need_confirmation = True
        elif confidence_threshold:
            confidence_levels = {"high": 3, "medium": 2, "low": 1}
            threshold_level = confidence_levels.get(confidence_threshold, 0)
            current_level = confidence_levels.get(confidence, 0)
            if current_level < threshold_level:
                need_confirmation = True
        
        if not need_confirmation:
            logger.info(f"Using version_analyze result: bump_type={bump_type_str}, confidence={confidence}")
            try:
                # Always return a valid BumpType - never None
                return BumpType(bump_type_str) if bump_type_str != "none" else BumpType.PATCH
            except ValueError:
                logger.warning(f"Invalid bump_type from version_analyze: {bump_type_str}, falling back")
        else:
            logger.info(f"Version analysis confidence ({confidence}) below threshold, may need confirmation")
            try:
                # Always return a valid BumpType - never None
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


def _format_changes_for_prompt(changes_made: dict) -> str:
    """Format changes for the commit message prompt."""
    files_changed = changes_made.get("files_changed", [])
    if not files_changed:
        return "No files changed"
    
    lines = []
    for f in files_changed[:10]:  # Limit to 10 files
        path = f.get("path", "")
        action = f.get("action", "modified")
        explanation = f.get("explanation", "")
        if explanation:
            lines.append(f"- {path} ({action}): {explanation}")
        else:
            lines.append(f"- {path} ({action})")
    
    if len(files_changed) > 10:
        lines.append(f"- ... and {len(files_changed) - 10} more files")
    
    return "\n".join(lines)


def _generate_commit_message(
    flow: FlowInstance,
    step: Step,
    new_version: str | None = None,
    version_config: VersionConfig | None = None
) -> str:
    """Generate a commit message based on the flow context.

    Uses proposal summary if available, otherwise uses LLM to generate
    a concise commit message from the task description and changes.

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
    restricted_edits_applied = step.inputs.get("restricted_edits_applied", [])

    # Use proposal summary if available, then implement_summary as fallback
    summary = proposal.get("summary", "") or implement_summary
    if summary:
        # Use first sentence or first 50 chars
        first_line = summary.split(".")[0]
        if len(first_line) > 72:
            first_line = first_line[:69] + "..."
        message = f"{task_type}: {first_line}"
    else:
        # Use LLM to generate commit message from changes and task description
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        try:
            changes_text = _format_changes_for_prompt(changes_made)
            if restricted_edits_applied:
                edits_desc = [f"  - {e.get('file_path', '?')}" for e in restricted_edits_applied]
                changes_text += "\n\nRestricted edits applied by engine:\n" + "\n".join(edits_desc)
            prompt = COMMIT_MESSAGE_PROMPT.format(
                task_type=task_type,
                task_description=task_description,
                changes_text=changes_text
            )
            
            # Append issue discovery injection if applicable
            from ..context_builder import get_issue_discovery_injection
            injection = get_issue_discovery_injection("commit", project_root)
            if injection:
                prompt += injection

            caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id="commit_summary")
            response = caller.call(prompt=prompt, require_json=False)
            
            # Clean up the response
            generated_summary = response.strip().strip('"\'')
            # Remove any "Commit message:" or similar prefixes
            for prefix in ["Commit message:", "Summary:", "Message:", "Commit:"]:
                if generated_summary.startswith(prefix):
                    generated_summary = generated_summary[len(prefix):].strip()
            
            # Truncate if too long
            if len(generated_summary) > 72:
                generated_summary = generated_summary[:69] + "..."
            
            if generated_summary:
                message = f"{task_type}: {generated_summary}"
            else:
                # Fallback to truncated task description
                desc = task_description[:60] if len(task_description) > 60 else task_description
                message = f"{task_type}: {desc}"
        except Exception as e:
            logger.warning(f"Failed to generate commit message with LLM: {e}. Using fallback.")
            # Fallback to truncated task description
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
