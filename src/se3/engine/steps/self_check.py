"""Self Check step handler.

LLM-based code review after tests pass. Checks logic completeness,
robustness, and test coverage gaps — explicitly excludes spec compliance
(that's verify_spec's job).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response
from .verify_spec import _get_max_fix_iterations

logger = logging.getLogger(__name__)


SELF_CHECK_PROMPT = """You are an expert code reviewer. Review the implementation for logic completeness, robustness, and potential issues that tests may not have caught.

## Task Description
{task_description}

## Changes Made
{changes_made}

## Test Results
{test_results}

## Specifications (for context only)
{spec_content}

## Fix Context
{fix_context}

## Review Dimensions

Focus your review on these dimensions. Do NOT check spec compliance — that is handled by a separate verification step.

1. **Logic Completeness**: Are there unhandled boundary conditions, missing error paths, or incomplete control flow? Look for edge cases the implementation should handle but doesn't.
2. **Code Robustness**: Is exception handling adequate? Are resources properly managed (files, connections, locks)? Are there concurrency safety issues?
3. **Functional Gaps**: Are there related modules that should have been modified but weren't? Are there integration points that were missed?
4. **Test Coverage Gaps**: Based on the test results, which logic paths are NOT exercised by existing tests? Are there critical paths that lack test coverage?

## What NOT to check
- **Spec compliance** — this is handled by the verify_spec step, do NOT duplicate that check.
- **Code style or formatting** — not actionable here.
- **Performance optimization suggestions** — only flag if there's a clear correctness issue.

## Severity Levels
- **critical**: Logic error that will cause incorrect behavior, data corruption, or crashes in normal usage paths.
- **high**: Missing error handling or boundary condition that will cause failures in reasonably common scenarios.
- **medium**: Defensive improvement that would prevent issues in edge cases, or a minor gap in test coverage.
- **low**: Nice-to-have improvement, minor robustness enhancement, or additional test suggestion.

Respond in JSON format:
```json
{{
    "issues": [
        {{
            "severity": "critical|high|medium|low",
            "description": "Clear description of the issue",
            "location": "File path and/or function name where the issue exists"
        }}
    ],
    "summary": "Brief overall assessment of the implementation quality"
}}
```

If the implementation is solid with no issues found, return an empty issues array.
"""


def self_check_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the self_check step.

    Performs LLM-based code review checking logic completeness,
    robustness, and test coverage gaps. Does NOT check spec compliance.

    Returns REVISION_NEEDED when critical/high issues are found and
    fix iterations remain. Returns COMPLETED when no actionable issues
    exist or max iterations are exhausted.
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})
    spec_content = step.inputs.get("spec_content", {})

    fix_iteration = step.inputs.get("fix_iteration", 0)
    max_iterations = _get_max_fix_iterations(flow)

    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    spec_text = _format_spec_content(spec_content)
    fix_context_text = _format_fix_context(fix_iteration, max_iterations)

    prompt = SELF_CHECK_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        test_results=test_text,
        spec_content=spec_text,
        fix_context=fix_context_text,
    )

    logger.info(f"Running self-check code review (fix iteration: {fix_iteration})...")

    try:
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"issues": [{"severity": "critical|high|medium|low", "description": "...", "location": "..."}], "summary": "..."}',
        )

        result = parse_json_response(response, required_keys=["issues"])

        if not result:
            step.error_message = "Failed to parse self-check result from LLM response"
            return StepStatus.FAILED

        issues = result.get("issues", [])
        actionable = [
            i for i in issues if i.get("severity") in ("critical", "high")
        ]
        actionable_count = len(actionable)

        step.outputs["self_check_result"] = result
        step.outputs["issues"] = issues
        step.outputs["actionable_count"] = actionable_count

        if actionable_count == 0:
            logger.info(
                f"Self-check passed ({len(issues)} issues, {actionable_count} actionable)"
            )
            return StepStatus.COMPLETED

        logger.warning(
            f"Self-check found {actionable_count} actionable issue(s) "
            f"(fix iteration {fix_iteration}/{max_iterations})"
        )

        if fix_iteration >= max_iterations:
            logger.warning(
                f"Max fix iterations ({max_iterations}) reached — "
                "completing with outstanding issues"
            )
            step.outputs["max_iterations_reached"] = True
            step.outputs["warning"] = (
                f"Self-check still has {actionable_count} actionable issue(s) "
                f"after {max_iterations} fix attempts"
            )
            return StepStatus.COMPLETED

        issue_details = "\n".join(
            f"- [{i.get('severity', 'high')}] {i.get('location', '?')}: "
            f"{i.get('description', '')}"
            for i in actionable
        )
        fix_instructions = (
            f"Self-check found {actionable_count} issue(s) that need fixing:\n"
            f"{issue_details}\n\n"
            "Fix the issues listed above and ensure the logic is correct."
        )

        step.outputs["fix_needed"] = True
        step.outputs["fix_iteration"] = fix_iteration
        step.outputs["max_fix_iterations"] = max_iterations
        step.outputs["fix_instructions"] = fix_instructions
        step.outputs["fix_context"] = {
            "reason": "self_check",
            "issues": actionable,
            "iteration": fix_iteration + 1,
        }

        return StepStatus.REVISION_NEEDED

    except Exception as e:
        logger.exception("Self-check step failed")
        step.error_message = f"Self-check failed: {str(e)}"
        return StepStatus.FAILED


def _format_changes(changes_made: dict[str, Any]) -> str:
    if not changes_made:
        return "No changes recorded."

    lines = []
    for file_change in changes_made.get("files_changed", []):
        if isinstance(file_change, str):
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
    if not test_results:
        return "No test results available."

    lines = []

    if "phases" in test_results:
        overall = test_results.get("overall_passed", False)
        lines.append(f"Overall passed: {overall}")

        new_tests = test_results.get("new_tests", {})
        if new_tests.get("count", 0) > 0:
            lines.append(f"\nNew tests ({new_tests['count']}):")
            for t in new_tests.get("failed", []):
                lines.append(f"  FAILED: {t}")
            lines.append(
                f"  Passed: {len(new_tests.get('passed', []))}, "
                f"Failed: {len(new_tests.get('failed', []))}"
            )

        regression = test_results.get("regression", {})
        if regression.get("failed"):
            lines.append("\nRegression failures:")
            for t in regression["failed"]:
                lines.append(f"  FAILED: {t}")

        for phase in test_results["phases"]:
            name = phase.get("name", "?")
            passed = phase.get("passed", False)
            lines.append(
                f"\nPhase '{name}': {'PASSED' if passed else 'FAILED'} "
                f"(exit code: {phase.get('returncode', '?')})"
            )
            stdout = phase.get("stdout", "")
            if stdout and not passed:
                lines.append(f"Output (last 1000 chars):\n{stdout[-1000:]}")
            stderr = phase.get("stderr", "")
            if stderr and not passed:
                lines.append(f"Stderr (last 500 chars):\n{stderr[-500:]}")

        return "\n".join(lines)

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


def _format_spec_content(spec_content: dict[str, str]) -> str:
    if not spec_content:
        return "No specifications provided."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        parts.append(content)
        parts.append("")

    return "\n".join(parts)


def _format_fix_context(fix_iteration: int, max_iterations: int) -> str:
    if fix_iteration == 0:
        return "This is the initial self-check (no previous fix attempts)."

    lines = [
        f"Fix iteration: {fix_iteration} of {max_iterations}",
        f"Previous fix attempts: {fix_iteration}",
    ]

    if fix_iteration >= max_iterations:
        lines.append(
            "WARNING: This is the final fix attempt. "
            "If issues remain, the flow will proceed with outstanding issues."
        )

    return "\n".join(lines)
