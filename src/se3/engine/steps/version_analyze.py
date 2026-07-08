"""Version analyze step handler.

Analyzes changes using LLM to determine the appropriate SemVer bump type.
This step runs after update_spec and before commit to intelligently determine
version changes based on actual implementation, not just task type.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..context import effective_task_type
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary

logger = logging.getLogger(__name__)


VERSION_RULES_FILE_RELPATH = "se3/version-rules.md"
VERSION_RULES_MAX_BYTES = 64 * 1024


VERSION_ANALYZE_PROMPT = """You are an expert in Semantic Versioning 2.0.0. Analyze the following changes and decide the new version number for this project.

## Default Rules: Semantic Versioning 2.0.0

Given a version number MAJOR.MINOR.PATCH, increment the:

1. **MAJOR** version when you make incompatible API changes
   - Breaking changes to public APIs
   - Removing functionality
   - Changing behavior in a backward-incompatible way
   - Changes that require users to modify their code

2. **MINOR** version when you add functionality in a backward compatible manner
   - New features
   - New functions, classes, or methods
   - New optional parameters
   - Deprecating functionality (without removing)
   - Substantial new capabilities

3. **PATCH** version when you make backward compatible bug fixes
   - Bug fixes that don't change intended behavior
   - Performance improvements
   - Internal refactoring with no API changes
   - Documentation fixes
   - Test additions/improvements

## Project-Specific Version Rules

{custom_rules}

## Task Information

**Task Type:** {task_type}
**Task Description:** {task_description}

## Changes Made

{changes_made}

## Spec Changes (API Contract)

{spec_changes}

## Verification Results

{verification_result}

## Current Version

{current_version}

**Pre-Session Version (baseline for version analysis):** {pre_session_version}

## Session-Introduced Commits

{session_commits}

## Instructions

implement 阶段可能已在主分支上提交了若干变更（见 Session-Introduced Commits），其中可能包含对版本文件的修改。请将这些 commit 视为未发生，以 Pre-Session Version 为 current_version 基线计算 suggested_version。

`suggested_version` is the AUTHORITATIVE field — the commit step writes exactly this value to the project version file. `bump_type` is auxiliary, used only for display and commit-message decoration; it does NOT recompute the version. Derive `suggested_version` directly from the Pre-Session Version baseline and the rules above.

When the project-specific rules section above conflicts with the default SemVer 2.0.0 description, the project-specific rules take priority.

Analyze the changes above and produce:

1. **suggested_version** (AUTHORITATIVE): The exact new version string (e.g., "1.3.0"). Must be a concrete version number derived from current version + rules. Required.

2. **bump_type** (auxiliary, for display only): One of "major", "minor", "patch", or "none". Describes the nature of the change; does not have to be a strict mathematical match against `suggested_version` if the project rules allow non-standard transitions.

3. **reasoning**: Explain your decision, referencing the active rules (default SemVer or project rules) and specific files / API changes / issues.

4. **confidence**: How confident are you? ("high", "medium", or "low")

5. **commit_message**: A concise git commit summary (first line only, max 72 characters).
   - Use imperative mood (e.g., "Add feature" not "Added feature")
   - Start with a verb describing the action
   - Be specific but brief — describe what was done, not what was planned
   - Do NOT include the task type prefix (like "feat:" or "fix:")

6. **versions_changes**: A list of 3-8 changelog-grade bullet strings describing
   this release's actual user-facing changes, written for the project's
   `VERSIONS.md` changelog.
   - Each entry is an imperative sentence (e.g., "Add localized README naming
     convention", "Fix badge insertion under YAML front-matter").
   - Audience is the end user / downstream reader — describe what changed and
     why it matters, not internal mechanics.
   - This is DISTINCT from `commit_message` (a single 72-char subject line) and
     from `reasoning` (the SemVer rating justification). Do NOT just repeat the
     commit subject; enumerate the concrete changes that went into this version.
   - Provide between 3 and 8 entries; if the change is genuinely tiny, fewer is
     acceptable, but prefer enumerating distinct user-visible effects.

Respond in valid JSON format:

```json
{{
  "suggested_version": "X.Y.Z",
  "bump_type": "major|minor|patch|none",
  "reasoning": "Detailed explanation referencing the active rules and specific changes",
  "confidence": "high|medium|low",
  "commit_message": "Concise imperative commit summary (max 72 chars)",
  "versions_changes": [
    "Imperative changelog bullet describing one user-facing change",
    "Another distinct user-facing change in this version"
  ]
}}
```

IMPORTANT:
- `suggested_version` MUST be present and MUST be a concrete version string — the commit step uses it verbatim.
- API contract changes (spec_changes) are the strongest indicator under default SemVer rules.
- Project-specific rules (when given) override the default rules on conflict.
- If unsure between minor and patch under default rules, be conservative and choose patch.
"""

# Two-segment marker only: USER_CONTENT region is empty.
# version_analyze consumes upstream artifacts (changes_made / summary /
# verification_result / task_type); no user-literal field is appended here.
# The web console renders the whole post-BEGIN tail inside the collapsed
# system-prompt chip.
VERSION_ANALYZE_PROMPT = inject_boundary(
    VERSION_ANALYZE_PROMPT, "## Task Information\n",
)


_NO_CUSTOM_RULES_PLACEHOLDER = (
    "_No project-specific rules file found at `se3/version-rules.md`. "
    "Use the default Semantic Versioning 2.0.0 rules above._"
)


def version_analyze_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the version analyze step.

    Uses LLM to decide the new version number based on the actual changes,
    project-specific version rules (optional), and Semantic Versioning 2.0.0
    as the default.

    The step's product depends on the flow's isolation mode:

    * **Non-worktree (synchronous) flow** — the commit that follows is the
      release point (baseline == current version), so the LLM-returned
      ``suggested_version`` is authoritative and the commit step writes it
      verbatim into the project version file. Output shape is unchanged.
    * **Worktree flow** (``flow.is_worktree_mode``) — the release point is the
      later merge, not this session's commit, so this step emits a branch-
      committed :class:`~se3.engine.version_intent.VersionIntent` (change
      summary + changelog bullets + auxiliary bump hint + provisional version)
      instead of an authoritative ``suggested_version``. The merge-side
      ``version_reconcile`` step derives the final version once, against
      master's then-current version. See :func:`_emit_version_intent`.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED when the LLM call
        fails or the response does not include a valid ``suggested_version``.
    """
    # Use the real analyzed type (never a run mode like 'discovery') so the
    # prompt's Task Type and the _fallback_commit_message both reflect what
    # analyze inferred, not the --discover run mode carried on flow.task_type.
    task_type = effective_task_type(
        getattr(getattr(flow, "state", None), "context", None), flow.task_type
    )
    task_description = step.inputs.get("task_description", flow.task_description) or ""

    # Get changes - implement step uses different output names
    changes_made = step.inputs.get("changes_made") or {}
    if not changes_made:
        # Try alternative output names from implement step
        files_changed = step.inputs.get("files_changed", [])
        implemented_groups = step.inputs.get("implemented_groups", [])
        changes_made = {
            "files_changed": files_changed,
            "implemented_groups": implemented_groups,
        }

    verification_result = step.inputs.get("verification_result", {})

    # Get spec changes - handle both dict (legacy) and list (current) formats
    spec_changes_raw = step.inputs.get("updated_specs", {})  # From update_spec step
    if isinstance(spec_changes_raw, list):
        # Convert list format to dict format for consistent handling
        spec_changes = {"updated_specs": spec_changes_raw}
    else:
        spec_changes = spec_changes_raw

    # Get current version if available
    current_version = _get_current_version(flow)

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Read optional project-specific version rules
    rules_text = _read_version_rules_file(project_root)
    custom_rules_block = rules_text if rules_text else _NO_CUSTOM_RULES_PLACEHOLDER

    # Pre-session version + session-introduced commits (G3 forwards these
    # into step.inputs from the implement step). pre_session_version falls
    # back to the disk-read current_version when absent.
    pre_session_version = step.inputs.get("pre_session_version")
    if pre_session_version is None or (
        isinstance(pre_session_version, str) and not pre_session_version.strip()
    ):
        logger.warning(
            "version_analyze: pre_session_version missing from step.inputs; "
            "falling back to disk-read current_version=%s",
            current_version,
        )
        pre_session_version = current_version
    session_commits = step.inputs.get("session_commits") or []

    # Format inputs for prompt
    changes_text = _format_changes(changes_made)
    spec_changes_text = _format_spec_changes(spec_changes)
    verification_text = _format_verification(verification_result)
    session_commits_text = _format_session_commits(session_commits)

    prompt = VERSION_ANALYZE_PROMPT.format(
        task_type=task_type,
        task_description=task_description,
        changes_made=changes_text,
        spec_changes=spec_changes_text,
        verification_result=verification_text,
        current_version=current_version,
        pre_session_version=pre_session_version,
        session_commits=session_commits_text,
        custom_rules=custom_rules_block,
    )

    # Append issue discovery injection if applicable
    from ..context_builder import get_issue_discovery_injection
    injection = get_issue_discovery_injection("version_analyze", project_root)
    if injection:
        prompt += injection

    logger.info("Analyzing changes to determine new project version...")

    retry_count = step.inputs.get("retry_count", 0)
    caller = LLMCaller(
        project_root,
        flow_id=flow.flow_id,
        step_id=step.step_id,
        step_type=step.step_type.value,
        external_attempt=retry_count,
        fix_iteration=step.inputs.get("fix_iteration", 0),
    )

    try:
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
        )
        result = _parse_response(response)
    except Exception as e:
        logger.exception("Version analysis failed")
        step.error_message = (
            f"version_analyze failed: {e}. "
            f"Current version is '{current_version}'. "
            "suggested_version could not be determined; the commit step will "
            "halt until a version is supplied via human intervention."
        )
        step.outputs["current_version"] = current_version
        return StepStatus.FAILED

    step.outputs["bump_type"] = result["bump_type"]
    step.outputs["reasoning"] = result["reasoning"]
    step.outputs["confidence"] = result["confidence"]
    step.outputs["current_version"] = current_version
    step.outputs["commit_message"] = (
        result.get("commit_message")
        or _fallback_commit_message(task_type, task_description)
    )

    # versions_changes feeds the commit step's DocumentationUpdater wiring
    # (VERSIONS.md changelog entry). _validate_result has already filtered to
    # non-empty strings; when nothing usable survives, fall back to a single
    # entry equal to the authoritative commit_message so VERSIONS.md still
    # records a bullet for this version. .get keeps the access defensive.
    versions_changes = result.get("versions_changes") or []
    if not versions_changes:
        versions_changes = [step.outputs["commit_message"]]
    step.outputs["versions_changes"] = versions_changes

    # De-versioning split (accident-driven, 2026-07-06): a worktree session's
    # release point is the merge, not this commit, so it must NOT emit an
    # authoritative suggested_version — two concurrent worktree flows diverging
    # from one baseline would each compute (and one silently drop) the same
    # number. Instead we persist a *VersionIntent* on the flow branch; the
    # merge-side version_reconcile step derives the final number once, against
    # master's then-current version. A non-worktree (synchronous) flow's commit
    # IS its release point (baseline == current version), so it keeps writing
    # the authoritative suggested_version verbatim — behaviour unchanged.
    if getattr(flow, "is_worktree_mode", False):
        if not _version_bumping_enabled(project_root):
            # version.enabled=false: preserve the "no automatic version bump"
            # contract in worktree mode too. Emitting an intent would make the
            # merge-side version_reconcile bump the detected version file (or
            # fail with ReconcileError when no version file exists), so skip it —
            # the merge lands with no version change, exactly as a non-worktree
            # flow does (the commit step skips bumping when disabled).
            step.outputs["current_version"] = current_version
            logger.info(
                "Version analysis (worktree): version bumping disabled; emitting "
                "no version intent — merge lands with no automatic bump."
            )
            return StepStatus.COMPLETED
        try:
            _emit_version_intent(
                step,
                flow,
                project_root=project_root,
                result=result,
                pre_session_version=pre_session_version,
                versions_changes=versions_changes,
                changes_text=changes_text,
                spec_changes_text=spec_changes_text,
                verification_text=verification_text,
            )
        except OSError as exc:
            # The persisted intent is the merge side's sole input for deciding
            # this feature's version; without it version_reconcile would treat
            # the session as contributing no bump and merge it with no version
            # or changelog. Fail loudly (and resumably) instead.
            step.outputs["current_version"] = current_version
            step.error_message = (
                f"version_analyze: could not persist the version intent for "
                f"worktree flow {flow.flow_id}: {exc}. The merge-side "
                f"version_reconcile step depends on "
                f"se3/version-intents/{flow.flow_id}.json to derive the final "
                f"version; without it this feature would merge with no version "
                f"bump or changelog entry. Fix the cause and resume."
            )
            logger.error(step.error_message)
            return StepStatus.FAILED
        logger.info(
            "Version analysis (worktree, intent-only): bump_type=%s, "
            "provisional_suggested_version=%s (pre_session_baseline=%s), "
            "confidence=%s — final version deferred to merge-side reconcile",
            result["bump_type"],
            result["suggested_version"],
            pre_session_version,
            result["confidence"],
        )
        return StepStatus.COMPLETED

    step.outputs["suggested_version"] = result["suggested_version"]

    logger.info(
        "Version analysis complete: suggested_version=%s (current=%s), "
        "bump_type=%s, confidence=%s",
        result["suggested_version"],
        current_version,
        result["bump_type"],
        result["confidence"],
    )

    return StepStatus.COMPLETED


def _emit_version_intent(
    step: Step,
    flow: FlowInstance,
    *,
    project_root: Path,
    result: dict[str, Any],
    pre_session_version: Any,
    versions_changes: list[str],
    changes_text: str,
    spec_changes_text: str,
    verification_text: str,
) -> None:
    """Persist a worktree session's version bump as a branch-committed intent.

    Builds a :class:`VersionIntent` from the analysis and writes it to the
    flow-branch-tracked ``se3/version-intents/`` directory so the merge-side
    ``version_reconcile`` step can read it after the merge and derive the final
    version. ``suggested_version`` is carried only as
    ``provisional_suggested_version`` (a non-authoritative reference) and is
    deliberately NOT placed in ``step.outputs['suggested_version']`` — that key
    is the commit step's authoritative version, which a worktree flow must not
    supply.

    ``change_summary`` is the intent's substance for the custom-rules (LLM)
    reconcile channel, which cannot rely on ``bump_type`` (that field is
    auxiliary and MAY be lossy under non-SemVer rules). It is the inductive
    digest of the same changes / spec / verification material this step already
    formatted for its prompt, so no usable intent is lost when ``bump_type`` is.

    A write failure is NOT swallowed: it propagates as :class:`OSError` for the
    caller to turn into a FAILED step. The merge-side ``version_reconcile``
    derives the final version SOLELY from this persisted intent, so a missing
    intent silently drops the whole bump + changelog — a visible, resumable
    failure here is strictly safer than an invisibly version-less merge.
    """
    from ..version_intent import VersionIntent, write_intent

    change_summary = _build_change_summary(
        changes_text, spec_changes_text, verification_text
    )
    baseline = pre_session_version if isinstance(pre_session_version, str) else None

    intent = VersionIntent(
        flow_id=flow.flow_id,
        change_summary=change_summary,
        versions_changes=list(versions_changes),
        bump_type=result.get("bump_type"),
        pre_session_baseline=baseline,
        provisional_suggested_version=result.get("suggested_version"),
    )

    # Expose the intent (never the authoritative suggested_version) so the web
    # console / template summary and any resume can see what this session
    # contributed without treating it as a version to write.
    step.outputs["version_intent"] = intent.to_dict()
    step.outputs["provisional_suggested_version"] = result.get("suggested_version")

    # Let OSError propagate: the intent is this worktree session's ONLY carrier
    # of its version bump to the merge side, so a failed persist must fail the
    # step (handled by the caller) rather than degrade to a warning.
    path = write_intent(project_root, intent)
    step.outputs["version_intent_path"] = str(path)


def _build_change_summary(
    changes_text: str, spec_changes_text: str, verification_text: str
) -> str:
    """Compose the free-form intent digest from the formatted prompt sections.

    Reuses the already-formatted Changes / Spec Changes / Verification blocks
    (the same material the LLM saw) rather than re-deriving them, so the intent
    the merge side reads is faithful to what drove this session's bump decision.
    Empty / placeholder sections are dropped to keep the digest compact.
    """
    sections: list[str] = []
    for heading, body in (
        ("Changes Made", changes_text),
        ("Spec Changes", spec_changes_text),
        ("Verification", verification_text),
    ):
        text = (body or "").strip()
        if not text:
            continue
        sections.append(f"## {heading}\n\n{text}")
    return "\n\n".join(sections)


def _read_version_rules_file(project_root: Path) -> Optional[str]:
    """Read project-specific version rules from ``se3/version-rules.md``.

    The file is a free-form Markdown / natural-language document. It is
    injected verbatim into the version_analyze prompt so the LLM can use
    it as decision criteria, overriding the default SemVer 2.0.0 rules
    on conflict.

    Args:
        project_root: The project root directory.

    Returns:
        The file contents (possibly truncated to ``VERSION_RULES_MAX_BYTES``
        bytes when oversized) or ``None`` when the file is absent or
        unreadable. Read errors never propagate.
    """
    try:
        rules_path = project_root / VERSION_RULES_FILE_RELPATH
    except Exception:
        return None

    try:
        if not rules_path.is_file():
            return None
    except OSError as e:
        logger.warning("Could not stat version rules file %s: %s", rules_path, e)
        return None

    try:
        data = rules_path.read_bytes()
    except (OSError, PermissionError) as e:
        logger.warning("Could not read version rules file %s: %s", rules_path, e)
        return None

    truncated = False
    if len(data) > VERSION_RULES_MAX_BYTES:
        data = data[:VERSION_RULES_MAX_BYTES]
        truncated = True

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning("Version rules file %s is not valid UTF-8: %s", rules_path, e)
        return None

    if truncated:
        logger.warning(
            "Version rules file %s exceeds %d bytes; truncated for prompt injection.",
            rules_path,
            VERSION_RULES_MAX_BYTES,
        )
        text += (
            "\n\n_[Truncated by SE3: original file exceeds "
            f"{VERSION_RULES_MAX_BYTES} bytes.]_\n"
        )

    return text


def _version_bumping_enabled(project_root: Path) -> bool:
    """Whether the project has version bumping enabled.

    Mirrors the commit step's ``version_config.enabled`` gate so worktree mode
    honours the same "no automatic version bump" contract. Any config-load fault
    defaults to enabled (the safe default: better to compute a version than to
    silently skip a bump on a healthy project).
    """
    try:
        from ...config import load_version_config

        return bool(load_version_config(project_root).enabled)
    except Exception:  # noqa: BLE001 - config load must not abort the step
        return True


def _get_current_version(flow: FlowInstance) -> str:
    """Get the current version from the project.

    Uses VersionBumper to read the version, which supports both
    script mode and built-in handler mode.

    Args:
        flow: The flow instance

    Returns:
        Current version string or "unknown"
    """
    try:
        from ...config import load_version_config
        from ..version_bumper import VersionBumper

        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        config = load_version_config(project_root)

        if not config.enabled:
            return "unknown (version bumping disabled)"

        bumper = VersionBumper(config)
        version_file = bumper.detect_version_file(project_root)

        if version_file:
            version = bumper.read_version(version_file)
            return version

        return "unknown (no version file found)"

    except Exception as e:
        logger.debug(f"Could not detect current version: {e}")
        return "unknown"


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Format changes for inclusion in prompt.
    
    Args:
        changes_made: Changes made during implementation
        
    Returns:
        Formatted changes text
    """
    if not changes_made:
        return "No changes recorded."
    
    lines = []
    
    # Format files changed
    files_changed = changes_made.get("files_changed", [])
    if files_changed:
        lines.append("### Files Changed:")
        for file_change in files_changed:
            if isinstance(file_change, str):
                # implement step may output plain file paths
                lines.append(f"- [modified] {file_change}")
            elif isinstance(file_change, dict):
                path = file_change.get("path", "?")
                action = file_change.get("action", "?")
                explanation = file_change.get("explanation", "")
                lines.append(f"- [{action}] {path}")
                if explanation:
                    lines.append(f"  Reason: {explanation}")
            else:
                lines.append(f"- {file_change}")
        lines.append("")
    
    # Format implementation summary if available
    implemented_groups = changes_made.get("implemented_groups", [])
    if implemented_groups:
        lines.append("### Implementation Groups:")
        for group in implemented_groups:
            if isinstance(group, str):
                # Group-by-group execution stores just group IDs
                lines.append(f"- {group}")
            elif isinstance(group, dict):
                group_name = group.get("name", "?")
                group_desc = group.get("description", "")
                lines.append(f"- {group_name}: {group_desc}")
            else:
                lines.append(f"- {group}")
        lines.append("")
    
    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_spec_changes(spec_changes: dict[str, Any]) -> str:
    """Format spec changes for inclusion in prompt.
    
    Spec changes are the primary indicator for API contract changes
    and are key to determining breaking vs non-breaking changes.
    
    Args:
        spec_changes: Spec changes from update_spec step (dict or list)
        
    Returns:
        Formatted spec changes text
    """
    if not spec_changes:
        return "No spec changes recorded."
    
    # Handle list format (direct list of specs)
    if isinstance(spec_changes, list):
        spec_changes = {"updated_specs": spec_changes}
    
    lines = []
    
    # Format updated specs
    updated_specs = spec_changes.get("updated_specs", []) if isinstance(spec_changes, dict) else []
    if updated_specs:
        lines.append("### Spec Files Updated:")
        for spec in updated_specs:
            spec_name = spec.get("spec_name", spec.get("path", "?"))
            change_desc = spec.get("change_description", "")
            lines.append(f"- {spec_name}")
            if change_desc:
                lines.append(f"  {change_desc}")
            # Legacy format support
            changes = spec.get("changes", [])
            for change in changes:
                change_type = change.get("type", "?")
                description = change.get("description", "")
                lines.append(f"  - [{change_type}] {description}")
        lines.append("")
    
    # Format API changes summary
    api_changes = spec_changes.get("api_changes", [])
    if api_changes:
        lines.append("### API Changes:")
        for change in api_changes:
            api_name = change.get("name", "?")
            change_type = change.get("type", "?")  # e.g., "added", "removed", "modified"
            impact = change.get("impact", "")  # e.g., "breaking", "non-breaking"
            lines.append(f"- [{change_type}] {api_name} ({impact})")
        lines.append("")
    
    # If no structured data, try to use summary
    summary = spec_changes.get("summary", "")
    if summary and not lines:
        lines.append("### Summary:")
        lines.append(summary)
        lines.append("")
    
    return "\n".join(lines) if lines else "Spec was checked but no API changes were recorded."


_SESSION_COMMITS_RENDER_LIMIT = 50
_SESSION_COMMITS_FILES_FOLD = 10


def _format_session_commits(commits: list[dict[str, Any]] | None) -> str:
    """Format implement-stage commits introduced into the main branch.

    Renders as a markdown list. Empty list produces an explanatory line so
    the prompt is unambiguous. Caps rendering at ``_SESSION_COMMITS_RENDER_LIMIT``
    entries (trailing note explains how many were omitted). Each commit's
    files list is folded when it exceeds ``_SESSION_COMMITS_FILES_FOLD``.

    Args:
        commits: List of commit dicts with keys ``sha``, ``subject``, ``files``.

    Returns:
        Markdown text safe for direct prompt embedding.
    """
    if not commits:
        return "implement 阶段未在主分支留下任何 commit。"

    total = len(commits)
    limit = _SESSION_COMMITS_RENDER_LIMIT
    rendered = commits[:limit]
    omitted = total - len(rendered)

    lines: list[str] = []
    for commit in rendered:
        if not isinstance(commit, dict):
            lines.append(f"- {commit}")
            continue
        sha = str(commit.get("sha", "") or "")
        sha_short = sha[:8] if sha else "(no-sha)"
        subject = str(commit.get("subject", "") or "").strip() or "(no subject)"
        lines.append(f"- {sha_short} {subject}")

        files = commit.get("files") or []
        if not isinstance(files, list):
            files = []
        if files:
            fold = _SESSION_COMMITS_FILES_FOLD
            if len(files) > fold:
                shown = files[:fold]
                more = len(files) - fold
                files_line = ", ".join(str(f) for f in shown) + f", ... 还有 {more} 个文件未展示"
            else:
                files_line = ", ".join(str(f) for f in files)
            lines.append(f"  files: {files_line}")

    if omitted > 0:
        lines.append(f"- ... 还有 {omitted} 个未展示")

    return "\n".join(lines)


def _format_verification(verification_result: dict[str, Any]) -> str:
    """Format verification results for inclusion in prompt.
    
    Args:
        verification_result: Verification results
        
    Returns:
        Formatted verification text
    """
    if not verification_result:
        return "No verification results available."
    
    lines = []
    
    verified = verification_result.get("verified", False)
    summary = verification_result.get("summary", "")
    
    lines.append(f"Verification passed: {verified}")
    if summary:
        lines.append(f"Summary: {summary}")
    
    issues = verification_result.get("issues", [])
    if issues:
        error_count = sum(1 for i in issues if i.get("priority") in ("critical", "high"))
        lines.append(f"Issues found: {len(issues)} ({error_count} critical/high)")

        for i, issue in enumerate(issues):
            priority = issue.get("priority", "unknown")
            message = issue.get("message", "")
            lines.append(f"  - [{priority}] {message}")
    
    return "\n".join(lines)


def _parse_response(response: str) -> dict[str, Any]:
    """Parse the LLM response to extract version analysis.
    
    Parses and validates the LLM response.

    Args:
        response: Raw LLM response
        
    Returns:
        Parsed result dictionary
    """
    if not response or not response.strip():
        raise ValueError("Empty response from LLM")
    
    # two_phase mode should provide valid JSON via extraction
    # Use the shared json_parser for consistency
    from ..utils.json_parser import parse_json_response

    result = parse_json_response(response, required_keys=["suggested_version"])

    if result is None:
        preview = response[:200].replace('\n', ' ')
        raise ValueError(
            "LLM response did not contain a parsable JSON object with "
            f"suggested_version. Preview: {preview}..."
        )

    return _validate_result(result)


def _validate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the parsed result.

    ``suggested_version`` is the authoritative output field and is required.
    Raises ``ValueError`` if it is missing or empty. ``bump_type`` is
    auxiliary and defaulted to ``"patch"`` when invalid or absent — it does
    not gate success.

    Args:
        result: Parsed result dictionary

    Returns:
        Validated and normalized result
    """
    suggested_version = result.get("suggested_version")
    if not isinstance(suggested_version, str) or not suggested_version.strip():
        raise ValueError(
            "LLM response is missing the required 'suggested_version' field."
        )
    suggested_version = suggested_version.strip()

    bump_type = str(result.get("bump_type", "patch")).lower()
    if bump_type not in ("major", "minor", "patch", "none"):
        bump_type = "patch"

    confidence = str(result.get("confidence", "medium")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    # versions_changes: changelog-grade bullets for VERSIONS.md. Only string
    # elements are kept (non-string entries are dropped); empty / whitespace-only
    # strings are also discarded. A missing or non-list value yields an empty
    # list here; the handler applies the `[commit_message]` fallback once the
    # authoritative commit_message is known. Using .get keeps older persisted
    # payloads (which never carried this key) safe.
    raw_changes = result.get("versions_changes")
    versions_changes: list[str] = []
    if isinstance(raw_changes, list):
        versions_changes = [
            c.strip() for c in raw_changes if isinstance(c, str) and c.strip()
        ]

    return {
        "bump_type": bump_type,
        "reasoning": result.get("reasoning", "No reasoning provided"),
        "confidence": confidence,
        "suggested_version": suggested_version,
        "commit_message": result.get("commit_message", ""),
        "versions_changes": versions_changes,
    }


def _fallback_commit_message(task_type: str, task_description: str) -> str:
    """Generate a fallback commit message from task type and description.

    Used when the LLM does not produce a commit_message field or when
    the entire version_analyze LLM call fails.

    Args:
        task_type: The type of task
        task_description: The task description

    Returns:
        A commit message string (without task_type prefix)
    """
    desc = task_description.strip()
    if not desc:
        return "Update project"
    # Use first sentence, truncated to 72 chars
    first_line = desc.split(".")[0].strip()
    if len(first_line) > 72:
        first_line = first_line[:69] + "..."
    return first_line
