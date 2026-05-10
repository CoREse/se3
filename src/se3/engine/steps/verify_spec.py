"""Verify Spec step handler.

Checks implementation against specifications for consistency.
Uses LLM to verify that requirements are met.
Detects test failures and triggers fix loop when appropriate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..issue_manager import IssueManager
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..truncation import (
    FAILURES_SECTION_MAX_CHARS,
    FIX_STDERR_TAIL_CHARS,
    PHASE_STDERR_TAIL_CHARS,
    PHASE_STDOUT_TAIL_CHARS,
)
from ..utils.json_parser import parse_json_response
from ...config import DEFAULT_MAX_FIX_ITERATIONS
from ._fix_context import (
    format_fix_iteration_display,
    render_fix_context,
)
from .test import _extract_failures_section

logger = logging.getLogger(__name__)


VERIFY_PROMPT = """You are an expert software quality assurance engineer. Verify that the implementation matches the specifications.

## Task Description
{task_description}

## Relevant Specifications
{spec_content}

## Changes Made
{changes_made}

## Planned Spec Changes
{spec_changes}

## Test Results
{test_results}

## Fix Context
{fix_context}

{previous_verification}

## Instructions
Verify the implementation against the specifications. Check:

1. **Requirements Met**: Are all requirements from specs implemented?
2. **Design Compliance**: Does the code follow the specified design?
3. **No Unintended Changes**: Are there any changes that weren't specified?
4. **Correctness**: Is the implementation logically correct?
5. **Test Results**: Did all tests pass? If not, analyze the failures.
6. **Planned Spec Changes**: If "Planned Spec Changes" lists specific changes declared by the plan step, treat deviations matching those declarations as intentional changes (scope: out_of_scope, priority: low), NOT regressions. Only deviations NOT covered by planned changes should be flagged as in_scope issues.
7. **Previous Verification**: If a "Previous Verification" section is present, issues listed there were already reported and a fix was attempted. Only re-report an issue if it STILL EXISTS in the current code. Do NOT re-report issues that have been successfully resolved.

### Issue Priority Levels
Use the following priority levels for each issue:
- **critical**: The implementation is fundamentally broken — core functionality does not work, data loss or corruption is possible, or security vulnerabilities are introduced. Must be fixed immediately.
- **high**: A requirement is not met, a specified behavior is incorrect, or tests fail due to implementation bugs. Should be fixed before shipping.
- **medium**: A partial implementation gap, missing edge case handling, or a non-critical deviation from the spec. Important but not blocking.
- **low**: Minor style issues, documentation gaps, or suggestions for improvement that don't affect correctness.

### Issue Scope
For each issue, determine its scope:
- **in_scope**: The issue was directly introduced by the current task's implementation, or the current task claims to address it but has not. These issues block the current flow and must be fixed.
- **out_of_scope**: The issue is a pre-existing problem discovered during verification, or relates to functionality outside the current task's boundaries. These issues will be tracked separately and do not block the current flow.

### Test Failure Analysis
If tests failed, analyze the test output to identify:
- Which specific tests failed
- What error messages were produced
- Root cause of the failures (implementation bug, missing code, etc.)
- Specific fix instructions for the implement step

Respond in JSON format:
```json
{{
    "issues": [
        {{
            "priority": "critical|high|medium|low",
            "scope": "in_scope|out_of_scope",
            "message": "Description of the issue",
            "suggestion": "How to fix (if applicable)"
        }}
    ],
    "summary": "Brief summary of verification results",
    "recommendations": ["recommendation1", "recommendation2"],
    "test_analysis": {{
        "tests_passed": true|false,
        "failure_summary": "Summary of test failures (if any)",
        "root_cause": "Root cause analysis of failures (if any)"
    }},
    "fix_instructions": "Detailed instructions for fixing issues, especially test failures. Include specific file paths, code changes needed, and approach to resolve."
}}
```

The "fix_instructions" field is REQUIRED when tests failed or when in_scope issues exist. Provide clear, actionable instructions that the implement step can use to fix the issues.
"""


def verify_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the verify_spec step.

    Verifies implementation against specifications using LLM.
    Detects test failures and triggers REVISION_NEEDED when appropriate.

    The ``verified`` field is computed by rule, not by LLM:
        verified = (in_scope_count == 0) and tests_passed

    REVISION_NEEDED is triggered when:
        in_scope_count > 0 or tests_passed == False

    Out-of-scope issues are filed via IssueManager.create() and do not
    block the flow.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success,
        StepStatus.REVISION_NEEDED when tests failed and fix needed,
        StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    spec_content = step.inputs.get("spec_content", {})
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})
    spec_changes = step.inputs.get("spec_changes", [])

    # Get fix iteration count from inputs
    fix_iteration = step.inputs.get("fix_iteration", 0)
    # Honor an explicit 0 from inputs (the unlimited sentinel); fall back to
    # the default only when the input is genuinely missing.
    raw_max = step.inputs.get("max_fix_iterations")
    max_iterations = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else DEFAULT_MAX_FIX_ITERATIONS
    prev_issues = step.inputs.get("prev_issues", [])
    prev_fix_instructions = step.inputs.get("prev_fix_instructions", "")
    fix_history = step.inputs.get("fix_history", [])

    # Format inputs
    spec_text = _format_spec_content(spec_content)
    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    fix_context_text = _format_fix_context(
        fix_iteration, max_iterations,
        fix_history=fix_history,
    )
    spec_changes_text = _format_spec_changes(spec_changes)
    prev_verification_text = _format_previous_verification(prev_issues, prev_fix_instructions)

    # Build prompt
    prompt = VERIFY_PROMPT.format(
        task_description=task_description,
        spec_content=spec_text,
        changes_made=changes_text,
        test_results=test_text,
        fix_context=fix_context_text,
        spec_changes=spec_changes_text,
        previous_verification=prev_verification_text,
    )

    # Append issue discovery injection if applicable
    from ..context_builder import get_issue_discovery_injection, get_spec_names_injection
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    injection = get_issue_discovery_injection("verify_spec", project_root)
    if injection:
        prompt += injection

    # Append available-specs names injection if applicable
    spec_names = get_spec_names_injection(
        "verify_spec", project_root, step.inputs.get("relevant_specs"),
    )
    if spec_names:
        prompt += spec_names

    logger.info(f"Verifying implementation against specifications (fix iteration: {fix_iteration})...")

    try:
        # Call LLM for verification
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"issues": [{"priority": "critical|high|medium|low", "scope": "in_scope|out_of_scope", "message": "..."}], "summary": "...", "recommendations": [], "test_analysis": {"tests_passed": true|false, "failure_summary": "...", "root_cause": "..."}, "fix_instructions": "..."}',
        )

        # Parse JSON response — "issues" is the primary required key
        verification = parse_json_response(response, required_keys=["issues"])

        if not verification:
            step.error_message = "Failed to parse verification from LLM response"
            return StepStatus.FAILED

        # Extract issues and classify by scope
        issues = verification.get("issues", [])
        in_scope_issues = [i for i in issues if i.get("scope", "in_scope") == "in_scope"]
        out_of_scope_issues = [i for i in issues if i.get("scope") == "out_of_scope"]
        in_scope_count = len(in_scope_issues)
        out_of_scope_count = len(out_of_scope_issues)

        # Check test results - support both old flat format and new structured format
        if test_results and isinstance(test_results, dict):
            tests_passed = test_results.get("overall_passed", test_results.get("passed", False))
            returncode = test_results.get("returncode", 0)
            if returncode != 0 and tests_passed:
                logger.warning(f"Test return code is {returncode}, marking as failed")
                tests_passed = False
        else:
            tests_passed = True

        # Rule-based verified: ignore LLM's verified field
        verified = (in_scope_count == 0) and tests_passed

        # Store outputs
        step.outputs["verification_result"] = verification
        step.outputs["verified"] = verified
        step.outputs["issues"] = issues
        step.outputs["in_scope_count"] = in_scope_count
        step.outputs["out_of_scope_count"] = out_of_scope_count

        # Transparently pass through discovered_issues for B-class collection
        discovered_issues = verification.get("discovered_issues", [])
        if discovered_issues:
            step.outputs["discovered_issues"] = discovered_issues

        # File out-of-scope issues via IssueManager
        _file_out_of_scope_issues(out_of_scope_issues, flow, project_root)

        test_analysis = verification.get("test_analysis", {})
        fix_instructions = verification.get("fix_instructions", "")

        # If tests failed but LLM didn't provide fix instructions, generate default
        if not tests_passed and not fix_instructions:
            stdout = (test_results.get("stdout") or "") if isinstance(test_results, dict) else ""
            stderr = (test_results.get("stderr") or "") if isinstance(test_results, dict) else ""
            fix_instructions = f"Tests are failing. Please review and fix the implementation.\n\nTest output:\n{_extract_failures_section(stdout, max_chars=FAILURES_SECTION_MAX_CHARS)}\n\nStderr:\n{stderr[-FIX_STDERR_TAIL_CHARS:]}"
            logger.warning("Tests failed but LLM didn't provide fix instructions - using default")

        # Store fix instructions in outputs
        if fix_instructions:
            step.outputs["fix_instructions"] = fix_instructions

        # Determine if we need to fix — tests failure path
        if not tests_passed:
            iter_display = format_fix_iteration_display(fix_iteration, max_iterations)
            logger.warning(f"TESTS FAILED - fix iteration {iter_display}")
            logger.info(f"Returning REVISION_NEEDED - will attempt fix (iteration {fix_iteration + 1})")

            step.outputs["fix_needed"] = True
            step.outputs["fix_iteration"] = fix_iteration
            # ``max_fix_iterations <= 0`` is the unlimited sentinel.
            step.outputs["max_fix_iterations"] = max_iterations
            step.outputs["fix_context"] = {
                "test_results": test_results,
                "test_analysis": test_analysis,
                "fix_instructions": fix_instructions,
                "reason": "test_failure",
                "iteration": fix_iteration + 1,
            }

            return StepStatus.REVISION_NEEDED

        # Tests passed — check in-scope spec compliance issues
        if in_scope_count > 0:
            logger.warning(f"Spec verification failed: {in_scope_count} in-scope issue(s) found")
            logger.info(f"Returning REVISION_NEEDED for spec compliance fix (iteration {fix_iteration + 1})")

            # Build fix instructions if LLM didn't provide them
            if not fix_instructions:
                issue_details = "\n".join(
                    f"- [{i.get('priority', 'high')}] {i.get('message', '')}"
                    for i in in_scope_issues
                )
                fix_instructions = (
                    f"Spec verification failed with {in_scope_count} in-scope issue(s):\n{issue_details}\n\n"
                    "Fix the implementation to match the specifications."
                )

            step.outputs["fix_needed"] = True
            step.outputs["fix_iteration"] = fix_iteration
            step.outputs["max_fix_iterations"] = max_iterations
            step.outputs["fix_instructions"] = fix_instructions
            step.outputs["fix_context"] = {
                "spec_issues": in_scope_issues,
                "test_results": test_results,
                "test_analysis": test_analysis,
                "reason": "spec_compliance",
                "iteration": fix_iteration + 1,
            }

            return StepStatus.REVISION_NEEDED

        logger.info(f"Verification {'passed' if verified else 'completed'} ({len(issues)} issues: {in_scope_count} in-scope, {out_of_scope_count} out-of-scope)")
        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Verify step failed")
        step.error_message = f"Verification failed: {str(e)}"
        return StepStatus.FAILED


def _file_out_of_scope_issues(
    out_of_scope_issues: list[dict[str, Any]],
    flow: FlowInstance,
    project_root: Path,
) -> None:
    """File out-of-scope issues as tracked issues via IssueManager.

    Each out-of-scope issue is persisted as a YAML issue file so it can
    be addressed in a future flow without blocking the current one.
    """
    if not out_of_scope_issues:
        return

    try:
        mgr = IssueManager(project_root)
        for issue_data in out_of_scope_issues:
            title = issue_data.get("message", "Untitled out-of-scope issue")
            suggestion = issue_data.get("suggestion", "")
            description = title
            if suggestion:
                description = f"{title}\n\nSuggestion: {suggestion}"
            priority = issue_data.get("priority", "medium")
            tags = ["auto-discovered", "source:verify-spec", "out-of-scope"]

            mgr.create(
                title=title,
                description=description,
                priority=priority,
                scope="out_of_scope",
                tags=tags,
                type="bug",
            )
            logger.info(f"Filed out-of-scope issue: {title}")
    except Exception as e:
        logger.warning(f"Failed to file out-of-scope issues: {e}")


def _get_max_fix_iterations(flow: FlowInstance) -> int:
    """Disk-reload helper preserved for tests/external callers.

    Production code receives ``max_fix_iterations`` via
    ``state_machine._build_step_inputs``; this helper exists for tests
    that exercise the disk-reload semantics directly.

    Returns ``DEFAULT_MAX_FIX_ITERATIONS`` when the project root cannot
    be determined. A value of 0 is the sentinel meaning "unlimited".
    """
    from ...config import WorkflowConfig

    project_root_str = flow.state.context.get("project_root") if flow.state else None
    if project_root_str:
        project_root = Path(project_root_str)
    elif flow.change_path:
        project_root = flow.change_path.parent
    else:
        return DEFAULT_MAX_FIX_ITERATIONS

    return WorkflowConfig.load(project_root).max_fix_iterations


def _format_spec_content(spec_content) -> str:
    """Format spec content for inclusion in prompt."""
    if not spec_content:
        return "No specifications provided."

    if isinstance(spec_content, str):
        return spec_content

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        parts.append(content)
        parts.append("")

    return "\n".join(parts)


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Format changes for inclusion in prompt."""
    if not changes_made:
        return "No changes recorded."

    lines = []
    files_changed = changes_made.get("files_changed", [])
    for file_change in files_changed:
        if isinstance(file_change, str):
            # implement step may output plain file paths
            lines.append(f"- modified: {file_change}")
        elif isinstance(file_change, dict):
            path = file_change.get("path", "?")
            action = file_change.get("action", "?")
            explanation = file_change.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {file_change}")

    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_test_results(test_results: dict[str, Any]) -> str:
    """Format test results for inclusion in prompt.

    Supports both old flat format (has "stdout" at top level) and
    new structured format (has "phases" and "new_tests"/"regression").
    """
    if not test_results:
        return "No test results available."

    lines = []

    # New structured format
    if "phases" in test_results:
        overall = test_results.get("overall_passed", False)
        lines.append(f"Overall passed: {overall}")

        new_tests = test_results.get("new_tests", {})
        if new_tests.get("count", 0) > 0:
            lines.append(f"\nNew tests ({new_tests['count']}):")
            for t in new_tests.get("failed", []):
                lines.append(f"  FAILED: {t}")
            lines.append(f"  Passed: {len(new_tests.get('passed', []))}, Failed: {len(new_tests.get('failed', []))}")

        regression = test_results.get("regression", {})
        if regression.get("failed"):
            lines.append(f"\nRegression failures:")
            for t in regression["failed"]:
                lines.append(f"  FAILED: {t}")

        for phase in test_results["phases"]:
            name = phase.get("name", "?")
            passed = phase.get("passed", False)
            lines.append(f"\nPhase '{name}': {'PASSED' if passed else 'FAILED'} (exit code: {phase.get('returncode', '?')})")
            stdout = phase.get("stdout", "")
            if stdout and not passed:
                lines.append(f"Output (last {PHASE_STDOUT_TAIL_CHARS} chars):\n{stdout[-PHASE_STDOUT_TAIL_CHARS:]}")
            stderr = phase.get("stderr", "")
            if stderr and not passed:
                lines.append(f"Stderr (last {PHASE_STDERR_TAIL_CHARS} chars):\n{stderr[-PHASE_STDERR_TAIL_CHARS:]}")

        return "\n".join(lines)

    # Old flat format (backward compat)
    passed = test_results.get("passed", False)
    returncode = test_results.get("returncode", "?")
    lines.append(f"Tests passed: {passed} (exit code: {returncode})")

    stdout = test_results.get("stdout", "")
    if stdout:
        lines.append(f"\nTest output:\n{stdout[-PHASE_STDOUT_TAIL_CHARS:]}")

    stderr = test_results.get("stderr", "")
    if stderr:
        lines.append(f"\nError output:\n{stderr[-PHASE_STDERR_TAIL_CHARS:]}")

    return "\n".join(lines)


def _format_spec_changes(spec_changes: list[dict[str, str]] | None) -> str:
    """Format spec_changes list into readable text for the prompt.

    Args:
        spec_changes: List of spec change declarations from the plan step.
            Each dict has keys: spec_name, change_type, target, description.

    Returns:
        Formatted text describing planned spec changes.
    """
    if not spec_changes:
        return "No planned spec changes."

    lines = []
    for change in spec_changes:
        spec_name = change.get("spec_name", "unknown")
        change_type = change.get("change_type", "unknown")
        target = change.get("target", "unknown")
        description = change.get("description", "")
        lines.append(f"- [{change_type}] {spec_name} :: {target}")
        if description:
            lines.append(f"  {description}")
    return "\n".join(lines)


def _format_fix_context(
    fix_iteration: int,
    max_iterations: int,
    fix_history: list | None = None,
) -> str:
    """Format fix context for inclusion in the verify_spec prompt.

    Thin wrapper around the shared ``render_fix_context`` helper —
    delegates all branching/copy to a single source of truth shared with
    self_check. prev_issues are rendered by _format_previous_verification()
    into the {previous_verification} slot, so they're not passed here.
    """
    return render_fix_context(
        fix_iteration,
        max_iterations,
        step_label="verification",
        fix_history=fix_history,
    )


def _format_previous_verification(
    prev_issues: list | None,
    prev_fix_instructions: str | None,
) -> str:
    if not prev_issues:
        return ""

    lines = [
        "## Previous Verification",
        "The following issues were reported in the previous verification round.",
        "The implement step has attempted to fix them.",
        "Verify whether each issue has been resolved — only report issues that STILL EXIST.",
        "",
    ]
    max_issues = 20
    for issue in prev_issues[:max_issues]:
        scope = issue.get("scope", "in_scope")
        priority = issue.get("priority", "high")
        msg = issue.get("message", "")
        lines.append(f"- [{priority}/{scope}] {msg}")
    if len(prev_issues) > max_issues:
        lines.append(f"- ... and {len(prev_issues) - max_issues} more issues (truncated)")

    if prev_fix_instructions:
        lines.append("")
        lines.append("### Fix Instructions Given to Implement Step")
        lines.append(prev_fix_instructions[:3000])

    return "\n".join(lines)
