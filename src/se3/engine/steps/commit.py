"""Commit step handler.

Commits the changes using git.
Integrates with VersionBumper for automatic version bumping.
Consumes the authoritative ``suggested_version`` from the version_analyze
step and writes it verbatim to the project version file. ``bump_type`` is
read only for commit-message decoration and template summary display.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..models import FlowInstance, Step, StepStatus, StepType
from ..version_bumper import VersionBumper, VersionConfig

logger = logging.getLogger(__name__)


def commit_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the commit step.

    Commits changes using git commands. If version bumping is enabled,
    writes the authoritative ``suggested_version`` produced by the
    preceding version_analyze step to the project version file before
    committing.

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

            # Resolve target version up front — this is the authoritative
            # value from version_analyze. Raises RuntimeError if missing or
            # if version_analyze failed, halting the commit.
            target_version = _resolve_target_version(step, flow)

            if version_file:
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

                # Write the authoritative target version directly
                new_version = version_bumper.set_version(
                    version=target_version,
                    path=version_file,
                )
                version_bumped = True
                logger.info(f"Set version: {original_version} -> {new_version}")

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

                    # Write the authoritative target version directly
                    new_version = version_bumper.set_version(
                        version=target_version,
                        path=version_file,
                    )
                    version_bumped = True
                    logger.info(f"Set version: {original_version} -> {new_version}")

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

        # Generate template summary when summarize step is not in the flow
        if StepType.SUMMARIZE not in flow.state.selected_steps:
            try:
                _generate_template_summary(flow, step)
            except Exception as e:
                logger.warning(f"Failed to generate template summary: {e}")

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


def _resolve_target_version(step: Step, flow: FlowInstance) -> str:
    """Resolve the authoritative target version from the version_analyze step.

    The version_analyze step's ``suggested_version`` is the sole authority on
    the new version number — this function reads it from ``step.inputs``
    (forwarded by the state machine) with a fallback to the most recent
    version_analyze step's outputs. If the version_analyze step is FAILED, or
    no ``suggested_version`` is available, a ``RuntimeError`` is raised so
    the commit step halts instead of inventing a version.

    Args:
        step: The commit step (its ``inputs`` carry forwarded version_analyze
            outputs)
        flow: The flow instance — used to locate the version_analyze step for
            status and current_version

    Returns:
        The authoritative version string to write.

    Raises:
        RuntimeError: When the version_analyze step failed or did not produce
            a ``suggested_version``. The message names the current version
            (when known) and directs the user toward human intervention.
    """
    # Locate the most recent version_analyze step (if any) for status and
    # current_version context.
    va_step: Step | None = None
    for step_id in reversed(flow.state.step_history):
        s = flow.state.steps.get(step_id)
        if s and s.step_type == StepType.VERSION_ANALYZE:
            va_step = s
            break

    suggested = step.inputs.get("suggested_version")
    if not suggested and va_step is not None:
        suggested = va_step.outputs.get("suggested_version")

    current_version = (
        (va_step.outputs.get("current_version") if va_step else None)
        or step.inputs.get("current_version")
        or "<unknown>"
    )

    if va_step is not None and va_step.status == StepStatus.FAILED:
        raise RuntimeError(
            f"version_analyze step failed; cannot determine target version "
            f"(current_version='{current_version}'). "
            "Provide a version via human intervention: rerun the version_analyze "
            "step, or create a human call under se3/calls/ to supply the version "
            "manually."
        )

    if not isinstance(suggested, str) or not suggested.strip():
        raise RuntimeError(
            "version_analyze did not produce a suggested_version "
            f"(current_version='{current_version}'). "
            "The commit step requires an explicit target version. "
            "Provide one via human intervention: rerun the version_analyze "
            "step, or create a human call under se3/calls/ to supply the "
            "version manually."
        )

    return suggested.strip()


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
    restricted_edits_applied = step.inputs.get("restricted_edits_applied", [])

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

    # Decorate the subject line with the version_analyze bump_type when
    # available. bump_type is auxiliary — it never determines the new version
    # number, but it provides useful context in the commit message.
    bump_type = step.inputs.get("bump_type")
    if isinstance(bump_type, str):
        bump_type = bump_type.strip().lower()
        if bump_type and bump_type != "none":
            message += f" ({bump_type} bump)"

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


def _generate_template_summary(flow: FlowInstance, step: Step) -> None:
    """Generate a template-based summary document when the summarize step is not in the flow.

    Uses commit message as the primary info, combined with structured data
    from the flow state (changed files, test results, version) to produce
    a summary without an LLM call.

    Args:
        flow: The flow instance
        step: The commit step (with outputs populated)
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    commit_message = step.outputs.get("commit_message", "")
    commit_hash = step.outputs.get("commit_hash", "unknown")
    version = step.outputs.get("version")
    version_bumped = step.outputs.get("version_bumped", False)

    changes = _collect_changes_from_flow(flow)
    test_results = _collect_test_results_from_flow(flow)

    task_description = flow.task_description or ""
    task_type = flow.task_type or "task"

    # Build summary document
    lines = [f"## Work Summary\n"]
    lines.append(f"**Task:** {task_description[:200]}\n")
    lines.append(f"**Type:** {task_type}\n")

    if commit_hash and commit_hash != "unknown":
        lines.append(f"**Commit:** `{commit_hash[:8]}`\n")

    if version_bumped and version:
        lines.append(f"**Version:** {version}\n")

    # Version analysis reasoning (from version_analyze step via inputs)
    reasoning = step.inputs.get("reasoning", "")
    if reasoning and reasoning.strip():
        lines.append(f"\n### Version Analysis\n")
        lines.append(f"{reasoning.strip()}\n")

    # Commit message section
    if commit_message:
        lines.append(f"\n### Commit Message\n")
        lines.append(f"{commit_message}\n")

    # Files changed section
    if changes:
        lines.append(f"\n### Files Changed ({len(changes)})\n")
        for f in changes[:20]:
            lines.append(f"- {f}")
        if len(changes) > 20:
            lines.append(f"- ... and {len(changes) - 20} more")

    # Test results section
    if test_results:
        passed = test_results.get("passed", False)
        status = "Passed" if passed else "Failed"
        lines.append(f"\n### Test Results\n")
        lines.append(f"- Status: **{status}**")

    lines.append(f"\n---\n*Generated by commit step (template mode)*\n")

    summary_text = "\n".join(lines)

    # Save to se3/state/summary-{flow_id}.md
    summary_dir = project_root / "se3" / "state"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_file = summary_dir / f"summary-{flow.flow_id}.md"

    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary_text)
        logger.info(f"Template summary saved to {summary_file}")
    except OSError as e:
        logger.warning(f"Failed to save template summary: {e}")


def _collect_changes_from_flow(flow: FlowInstance) -> list[str]:
    """Collect file change paths from the flow's implement step outputs.

    Args:
        flow: The flow instance

    Returns:
        List of file path strings
    """
    file_paths: list[str] = []

    for step_id in flow.state.step_history:
        step = flow.state.steps.get(step_id)
        if step and step.step_type == StepType.IMPLEMENT:
            files_changed = step.outputs.get("files_changed", [])
            for f in files_changed:
                if isinstance(f, str):
                    file_paths.append(f)
                elif isinstance(f, dict):
                    file_paths.append(f.get("path", "?"))
                else:
                    file_paths.append(str(f))

    return file_paths


def _collect_test_results_from_flow(flow: FlowInstance) -> dict:
    """Collect the most recent test results from the flow's test step outputs.

    Args:
        flow: The flow instance

    Returns:
        Test results dict, or empty dict if no test step found
    """
    for step_id in reversed(flow.state.step_history):
        step = flow.state.steps.get(step_id)
        if step and step.step_type == StepType.TEST:
            return step.outputs.get("test_results") or {}

    return {}
