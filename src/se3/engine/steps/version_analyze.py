"""Version analyze step handler.

Analyzes changes using LLM to determine the appropriate SemVer bump type.
This step runs after update_spec and before commit to intelligently determine
version changes based on actual implementation, not just task type.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


VERSION_ANALYZE_PROMPT = """You are an expert in Semantic Versioning 2.0.0. Analyze the following changes and determine the appropriate version bump type.

## Semantic Versioning 2.0.0 Rules

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

## Instructions

Analyze the changes above and determine:

1. **bump_type**: Which version component should be incremented? ("major", "minor", "patch", or "none")
   - Focus on API contract changes (spec_changes) as the primary indicator
   - "major": Breaking API changes, removed parameters/functions, incompatible behavior
   - "minor": New APIs, new features, backward-compatible additions
   - "patch": Bug fixes, internal changes, documentation, tests
   - "none": Truly trivial changes (formatting, comments only)

2. **reasoning**: Explain your reasoning based on SemVer rules. Reference specific files, API changes, or issues.

3. **confidence**: How confident are you? ("high", "medium", or "low")

4. **suggested_version**: What should the new version be? (e.g., "1.3.0")

Respond in valid JSON format:

```json
{{
  "bump_type": "major|minor|patch|none",
  "reasoning": "Detailed explanation referencing specific changes and SemVer rules",
  "confidence": "high|medium|low",
  "suggested_version": "X.Y.Z"
}}
```

IMPORTANT:
- API contract changes (spec_changes) are the strongest indicator of major/minor
- If spec_changes shows "added function" → likely minor; "removed parameter" → likely major
- If unsure between minor and patch, be conservative and choose patch
- Use "none" only for truly trivial changes
"""


def version_analyze_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the version analyze step.
    
    Uses LLM to analyze the actual changes and determine the appropriate
    SemVer bump type based on the content of changes, not just task type.
    
    Prioritizes spec_changes (API contract changes) as the primary indicator
    for breaking vs non-breaking changes.
    
    Args:
        step: The current step being executed
        flow: The flow instance containing context
        
    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_type = flow.task_type or "feature"
    task_description = flow.task_description or ""
    changes_made = step.inputs.get("changes_made", {})
    verification_result = step.inputs.get("verification_result", {})
    spec_changes = step.inputs.get("updated_specs", {})  # From update_spec step
    
    # Get current version if available
    current_version = _get_current_version(flow)
    
    # Format inputs for prompt
    changes_text = _format_changes(changes_made)
    spec_changes_text = _format_spec_changes(spec_changes)
    verification_text = _format_verification(verification_result)
    
    # Build prompt
    prompt = VERSION_ANALYZE_PROMPT.format(
        task_type=task_type,
        task_description=task_description,
        changes_made=changes_text,
        spec_changes=spec_changes_text,
        verification_result=verification_text,
        current_version=current_version,
    )
    
    logger.info("Analyzing changes to determine version bump type...")
    
    try:
        # Call LLM for version analysis
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value if isinstance(step.step_type, str) else step.step_type.value
        )
        response = caller.call(
            prompt=prompt,
            json_mode="on",  # Request JSON output
        )
        
        # Parse the response
        result = _parse_response(response)
        
        # Store outputs
        step.outputs["bump_type"] = result["bump_type"]
        step.outputs["reasoning"] = result["reasoning"]
        step.outputs["confidence"] = result["confidence"]
        step.outputs["suggested_version"] = result["suggested_version"]
        
        # Log the decision
        logger.info(
            f"Version analysis complete: bump_type={result['bump_type']}, "
            f"confidence={result['confidence']}, "
            f"version={current_version} -> {result['suggested_version']}"
        )
        
        # Display to user
        print(f"\n{'='*60}")
        print("📊 VERSION ANALYSIS")
        print(f"{'='*60}")
        print(f"Current Version: {current_version}")
        print(f"Suggested Version: {result['suggested_version']}")
        print(f"Bump Type: {result['bump_type']}")
        print(f"Confidence: {result['confidence']}")
        print(f"\nReasoning:")
        print(result['reasoning'])
        print(f"{'='*60}\n")
        
        return StepStatus.COMPLETED
        
    except Exception as e:
        logger.exception("Version analysis failed")
        # Use fallback based on task type
        fallback_bump = _get_fallback_bump_type(task_type)
        step.outputs["bump_type"] = fallback_bump
        step.outputs["reasoning"] = f"LLM analysis failed ({e}), using fallback based on task type"
        step.outputs["confidence"] = "low"
        step.outputs["suggested_version"] = "auto"  # Will be calculated by commit step
        
        logger.warning(f"Using fallback bump type: {fallback_bump}")
        return StepStatus.COMPLETED


def _get_current_version(flow: FlowInstance) -> str:
    """Get the current version from the project.
    
    Args:
        flow: The flow instance
        
    Returns:
        Current version string or "unknown"
    """
    try:
        from ...config import load_version_config
        from ..version_bumper import VersionDetector
        
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        config = load_version_config(project_root)
        
        if not config.enabled:
            return "unknown (version bumping disabled)"
        
        detector = VersionDetector()
        version_file = detector.get_version_file_path(project_root)
        
        if version_file:
            version = detector.read_version(version_file)
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
            path = file_change.get("path", "?")
            action = file_change.get("action", "?")
            explanation = file_change.get("explanation", "")
            lines.append(f"- [{action}] {path}")
            if explanation:
                lines.append(f"  Reason: {explanation}")
        lines.append("")
    
    # Format implementation summary if available
    implemented_groups = changes_made.get("implemented_groups", [])
    if implemented_groups:
        lines.append("### Implementation Groups:")
        for group in implemented_groups:
            group_name = group.get("name", "?")
            group_desc = group.get("description", "")
            lines.append(f"- {group_name}: {group_desc}")
        lines.append("")
    
    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_spec_changes(spec_changes: dict[str, Any]) -> str:
    """Format spec changes for inclusion in prompt.
    
    Spec changes are the primary indicator for API contract changes
    and are key to determining breaking vs non-breaking changes.
    
    Args:
        spec_changes: Spec changes from update_spec step
        
    Returns:
        Formatted spec changes text
    """
    if not spec_changes:
        return "No spec changes recorded."
    
    lines = []
    
    # Format updated specs
    updated_specs = spec_changes.get("updated_specs", [])
    if updated_specs:
        lines.append("### Spec Files Updated:")
        for spec in updated_specs:
            spec_path = spec.get("path", "?")
            changes = spec.get("changes", [])
            lines.append(f"- {spec_path}")
            if changes:
                for change in changes:
                    change_type = change.get("type", "?")  # e.g., "added", "modified", "removed"
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
        error_count = sum(1 for i in issues if i.get("severity") == "error")
        lines.append(f"Issues found: {len(issues)} ({error_count} errors)")
        
        # Show first few issues
        for i, issue in enumerate(issues[:3]):
            severity = issue.get("severity", "unknown")
            message = issue.get("message", "")
            lines.append(f"  - [{severity}] {message}")
        if len(issues) > 3:
            lines.append(f"  ... and {len(issues) - 3} more issues")
    
    return "\n".join(lines)


def _parse_response(response: str) -> dict[str, Any]:
    """Parse the LLM response to extract version analysis.
    
    Args:
        response: Raw LLM response
        
    Returns:
        Parsed result dictionary
    """
    # Try to extract JSON from the response
    try:
        # First, try to parse as-is
        result = json.loads(response)
        return _validate_result(result)
    except json.JSONDecodeError:
        pass
    
    # Try to extract JSON from markdown code blocks
    import re
    
    # Look for JSON in ```json blocks
    json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1))
            return _validate_result(result)
        except json.JSONDecodeError:
            pass
    
    # Look for JSON in ``` blocks
    json_match = re.search(r'```\s*(\{.*?)\s*```', response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1))
            return _validate_result(result)
        except json.JSONDecodeError:
            pass
    
    # Look for JSON object directly
    json_match = re.search(r'(\{[^{}]*"bump_type"[^{}]*\})', response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1))
            return _validate_result(result)
        except json.JSONDecodeError:
            pass
    
    # If all parsing fails, raise an error
    raise ValueError(f"Could not parse version analysis response: {response[:200]}...")


def _validate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the parsed result.
    
    Args:
        result: Parsed result dictionary
        
    Returns:
        Validated and normalized result
    """
    # Ensure required fields exist
    bump_type = result.get("bump_type", "patch").lower()
    
    # Normalize bump_type
    valid_bumps = ["major", "minor", "patch", "none"]
    if bump_type not in valid_bumps:
        bump_type = "patch"  # Default to patch if invalid
    
    # Normalize confidence
    confidence = result.get("confidence", "medium").lower()
    valid_confidences = ["high", "medium", "low"]
    if confidence not in valid_confidences:
        confidence = "medium"
    
    return {
        "bump_type": bump_type,
        "reasoning": result.get("reasoning", "No reasoning provided"),
        "confidence": confidence,
        "suggested_version": result.get("suggested_version", "auto"),
    }


def _get_fallback_bump_type(task_type: str) -> str:
    """Get fallback bump type based on task type.
    
    Args:
        task_type: The type of task
        
    Returns:
        Fallback bump type string
    """
    fallback_map = {
        "feature": "minor",
        "feat": "minor",
        "bugfix": "patch",
        "fix": "patch",
        "small": "patch",
        "refactor": "patch",
        "docs": "patch",
        "test": "patch",
        "chore": "patch",
        "breaking": "major",
    }
    return fallback_map.get(task_type, "patch")
