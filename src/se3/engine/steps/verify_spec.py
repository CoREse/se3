"""Verify Spec step handler.

Checks implementation against specifications for consistency.
Uses LLM to verify that requirements are met.
Detects test failures and triggers fix loop when appropriate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)

DEFAULT_MAX_FIX_ITERATIONS = 3


VERIFY_PROMPT = """You are an expert software quality assurance engineer. Verify that the implementation matches the specifications.

## Task Description
{task_description}

## Relevant Specifications
{spec_content}

## Changes Made
{changes_made}

## Test Results
{test_results}

## Fix Context
{fix_context}

## Instructions
Verify the implementation against the specifications. Check:

1. **Requirements Met**: Are all requirements from specs implemented?
2. **Design Compliance**: Does the code follow the specified design?
3. **No Unintended Changes**: Are there any changes that weren't specified?
4. **Correctness**: Is the implementation logically correct?
5. **Test Results**: Did all tests pass? If not, analyze the failures.

### Test Failure Analysis
If tests failed, analyze the test output to identify:
- Which specific tests failed
- What error messages were produced
- Root cause of the failures (implementation bug, missing code, etc.)
- Specific fix instructions for the implement step

Respond in JSON format:
```json
{{
    "verified": true|false,
    "issues": [
        {{
            "severity": "error|warning|info",
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

Set "verified" to true if the implementation is acceptable (may have minor issues).
Set "verified" to false only if there are critical errors that must be fixed.

The "fix_instructions" field is REQUIRED when tests failed. Provide clear, actionable instructions that the implement step can use to fix the issues.
"""


def verify_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the verify_spec step.

    Verifies implementation against specifications using LLM.
    Detects test failures and triggers REVISION_NEEDED when appropriate.

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

    # Get fix iteration count from inputs
    fix_iteration = step.inputs.get("fix_iteration", 0)
    max_iterations = _get_max_fix_iterations(flow)

    # Format inputs
    spec_text = _format_spec_content(spec_content)
    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    fix_context_text = _format_fix_context(fix_iteration, max_iterations)

    # Build prompt
    prompt = VERIFY_PROMPT.format(
        task_description=task_description,
        spec_content=spec_text,
        changes_made=changes_text,
        test_results=test_text,
        fix_context=fix_context_text,
    )

    logger.info(f"Verifying implementation against specifications (fix iteration: {fix_iteration})...")

    try:
        # Call LLM for verification (use EXTRACT mode to avoid retry on format issues)
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="extract",
            json_schema_hint='{"verified": true|false, "issues": [{"severity": "error|warning", "message": "..."}], "summary": "...", "recommendations": [], "test_analysis": {"tests_passed": true|false, "failure_summary": "...", "root_cause": "..."}, "fix_instructions": "..."}',
        )

        # Parse JSON response
        verification = parse_json_response(response, required_keys=["verified"])

        if not verification:
            step.error_message = "Failed to parse verification from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["verification_result"] = verification
        step.outputs["verified"] = verification.get("verified", False)
        step.outputs["issues"] = verification.get("issues", [])

        # Transparently pass through discovered_issues for B-class collection
        discovered_issues = verification.get("discovered_issues", [])
        if discovered_issues:
            step.outputs["discovered_issues"] = discovered_issues

        issues = verification.get("issues", [])
        error_count = sum(1 for i in issues if i.get("severity") == "error")

        # Check test results - support both old flat format and new structured format
        if test_results and isinstance(test_results, dict):
            # New structured format has "overall_passed"; old format has "passed"
            tests_passed = test_results.get("overall_passed", test_results.get("passed", False))
            returncode = test_results.get("returncode", 0)
            if returncode != 0 and tests_passed:
                logger.warning(f"Test return code is {returncode}, marking as failed")
                tests_passed = False
        else:
            tests_passed = True
        
        test_analysis = verification.get("test_analysis", {})
        fix_instructions = verification.get("fix_instructions", "")
        
        # If tests failed but LLM didn't provide fix instructions, generate default
        if not tests_passed and not fix_instructions:
            stdout = test_results.get("stdout", "") if isinstance(test_results, dict) else ""
            stderr = test_results.get("stderr", "") if isinstance(test_results, dict) else ""
            fix_instructions = f"Tests are failing. Please review and fix the implementation.\n\nTest output:\n{stdout[:1000]}\n\nStderr:\n{stderr[:500]}"
            logger.warning("Tests failed but LLM didn't provide fix instructions - using default")

        # Store fix instructions in outputs
        if fix_instructions:
            step.outputs["fix_instructions"] = fix_instructions

        # Determine if we need to fix
        if not tests_passed:
            logger.warning(f"TESTS FAILED - fix iteration {fix_iteration}/{max_iterations}")

            if fix_iteration < max_iterations:
                # Return REVISION_NEEDED to trigger fix loop
                logger.info(f"Returning REVISION_NEEDED - will attempt fix (iteration {fix_iteration + 1})")

                # Store fix context for next implement step
                step.outputs["fix_needed"] = True
                step.outputs["fix_iteration"] = fix_iteration
                step.outputs["max_fix_iterations"] = max_iterations
                step.outputs["fix_context"] = {
                    "test_results": test_results,
                    "test_analysis": test_analysis,
                    "fix_instructions": fix_instructions,
                    "iteration": fix_iteration + 1,
                }

                return StepStatus.REVISION_NEEDED
            else:
                # Max iterations reached - complete with warning
                logger.warning(f"Max fix iterations ({max_iterations}) reached - completing with test failures")
                step.outputs["max_iterations_reached"] = True
                step.outputs["warning"] = f"Tests still failing after {max_iterations} fix attempts"

                return StepStatus.COMPLETED

        # Tests passed - check spec compliance
        if verification.get("verified", False):
            logger.info(f"Verification passed ({len(issues)} issues found, {error_count} errors)")
        else:
            logger.warning(f"Verification failed: {error_count} errors found")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Verify step failed")
        step.error_message = f"Verification failed: {str(e)}"
        return StepStatus.FAILED


def _get_max_fix_iterations(flow: FlowInstance) -> int:
    """Get max fix iterations from config or use default.

    Args:
        flow: Current flow instance

    Returns:
        Maximum number of fix iterations (default: 3)
    """
    # Try to get from flow context first
    max_iter = flow.state.context.get("max_fix_iterations")
    if max_iter is not None:
        return int(max_iter)

    # Try to load from se3.yaml
    try:
        # Get project root from context or derive from change_path
        project_root_str = flow.state.context.get("project_root")
        if project_root_str:
            project_root = Path(project_root_str)
        elif flow.change_path:
            # Fallback: derive from change_path (parent of change_path.parent)
            # change_path is typically openspec/changes/change_name
            # so we need to go up 3 levels to get project root
            project_root = flow.change_path.parent.parent.parent
        else:
            project_root = Path.cwd()

        config_path = project_root / "se3.yaml"
        if config_path.exists():
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            workflow_config = config.get("workflow", {})
            max_iter = workflow_config.get("max_fix_iterations", DEFAULT_MAX_FIX_ITERATIONS)
            return int(max_iter)
    except Exception as e:
        logger.debug(f"Could not load max_fix_iterations from config: {e}")

    return DEFAULT_MAX_FIX_ITERATIONS


def _format_spec_content(spec_content: dict[str, str]) -> str:
    """Format spec content for inclusion in prompt."""
    if not spec_content:
        return "No specifications provided."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        # Truncate very long specs
        if len(content) > 3000:
            content = content[:3000] + "\n... [truncated]"
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
                lines.append(f"Output (last 1000 chars):\n{stdout[-1000:]}")
            stderr = phase.get("stderr", "")
            if stderr and not passed:
                lines.append(f"Stderr (last 500 chars):\n{stderr[-500:]}")

        return "\n".join(lines)

    # Old flat format (backward compat)
    passed = test_results.get("passed", False)
    returncode = test_results.get("returncode", "?")
    lines.append(f"Tests passed: {passed} (exit code: {returncode})")

    stdout = test_results.get("stdout", "")
    if stdout:
        lines.append(f"\nTest output:\n{stdout[-1000:]}")

    stderr = test_results.get("stderr", "")
    if stderr:
        lines.append(f"\nError output:\n{stderr[-500:]}")

    return "\n".join(lines)


def _format_fix_context(fix_iteration: int, max_iterations: int) -> str:
    """Format fix context for inclusion in prompt."""
    if fix_iteration == 0:
        return "This is the initial verification (no previous fix attempts)."

    lines = [
        f"Fix iteration: {fix_iteration} of {max_iterations}",
        f"Previous fix attempts: {fix_iteration}",
    ]

    if fix_iteration >= max_iterations:
        lines.append("WARNING: This is the final fix attempt. If tests still fail, the flow will complete with failures.")

    return "\n".join(lines)
