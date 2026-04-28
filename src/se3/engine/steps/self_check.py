"""Self Check step handler.

LLM-based code review after tests pass. Checks logic completeness,
robustness, and test coverage gaps — explicitly excludes spec compliance
(that's verify_spec's job).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..truncation import (
    PHASE_STDERR_TAIL_CHARS,
    PHASE_STDOUT_TAIL_CHARS,
    SELF_CHECK_TASK_GROUPS_MAX_CHARS,
)
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
{task_groups_section}
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


_TASK_GROUPS_SECTION_INTRO = (
    "## Plan Task Groups (Scope Reference)\n\n"
    "The following is the plan's task breakdown (task_groups). It is a "
    "**scope reference**, NOT a strict specification:\n"
    "- Use it to help judge the **Functional Gaps** dimension: cross-check that "
    "each planned task's deliverables appear in the implementation.\n"
    "- Reasonable deviations from the plan (logic correct, functionality covered, "
    "quality acceptable) do NOT count as issues.\n"
    "- Do NOT flag missing-plan-compliance as an issue — this is self_check, not a "
    "plan-conformance audit. Functional-gap judgments should weigh the original "
    "Task Description together with this list.\n\n"
)


def _format_task_groups(task_groups: Any) -> str:
    """Render plan task_groups as a compact Markdown summary for self_check.

    Returns an empty string when input is missing, None, empty, or not a list —
    the caller omits the whole prompt section in that case.

    Output is head-truncated to SELF_CHECK_TASK_GROUPS_MAX_CHARS with an
    explicit ellipsis marker appended when truncation occurs.
    """
    if not task_groups or not isinstance(task_groups, list):
        return ""

    lines: list[str] = []
    for group in task_groups:
        if not isinstance(group, dict):
            continue
        group_id = group.get("group_id") or ""
        group_name = group.get("name") or ""
        header_bits = [b for b in (group_id, group_name) if b]
        header = " — ".join(header_bits) if header_bits else "(unnamed group)"
        lines.append(f"### {header}")

        tasks = group.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            lines.append("_(no tasks)_")
            lines.append("")
            continue

        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id")
            desc = (task.get("description") or "").strip()
            id_prefix = f"[{task_id}] " if task_id is not None else ""
            lines.append(f"- {id_prefix}{desc}" if desc else f"- {id_prefix}(no description)")

            criteria = task.get("acceptance_criteria") or []
            if isinstance(criteria, list) and criteria:
                for c in criteria:
                    c_text = str(c).strip()
                    if c_text:
                        lines.append(f"  - AC: {c_text}")
        lines.append("")

    summary = "\n".join(lines).rstrip()
    if not summary:
        return ""

    if len(summary) > SELF_CHECK_TASK_GROUPS_MAX_CHARS:
        cut = summary[:SELF_CHECK_TASK_GROUPS_MAX_CHARS]
        # Prefer cutting at the last newline so we don't split a markdown
        # bullet or `### Group` header mid-line. Fall back to the raw slice
        # if no newline exists within the window.
        nl = cut.rfind("\n")
        if nl > 0:
            cut = cut[:nl]
        summary = cut.rstrip() + "\n… (truncated)"
    return summary


def _build_task_groups_section(task_groups: Any) -> str:
    """Build the full `## Plan Task Groups` prompt section, or empty string.

    When task_groups is absent/empty, returns "" so the caller can inline it
    without producing an orphan heading or blank lines in the prompt.
    """
    summary = _format_task_groups(task_groups)
    if not summary:
        return ""
    return "\n" + _TASK_GROUPS_SECTION_INTRO + summary + "\n"


def self_check_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the self_check step.

    Performs LLM-based code review checking logic completeness,
    robustness, and test coverage gaps. Does NOT check spec compliance.

    Returns COMPLETED when no issues are found.
    Returns REVISION_NEEDED when issues exist (regardless of iteration count),
    letting the state machine handle exhaustion centrally.
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})
    spec_content = step.inputs.get("spec_content", {})
    task_groups = step.inputs.get("task_groups")

    fix_iteration = step.inputs.get("fix_iteration", 0)
    max_iterations = step.inputs.get("max_fix_iterations") or _get_max_fix_iterations(flow)
    prev_issues = step.inputs.get("prev_self_check_issues", [])
    fix_history = step.inputs.get("fix_history", [])
    convergence_enabled = step.inputs.get("self_check_convergence_enabled", False)
    pass_index = step.inputs.get("self_check_pass_index", 1)
    passes_required = step.inputs.get("self_check_passes_required", 1)

    # Write back so history renderers can read the pass position
    step.outputs["self_check_pass_index"] = pass_index
    step.outputs["self_check_passes_required"] = passes_required

    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    spec_text = _format_spec_content(spec_content)
    task_groups_section = _build_task_groups_section(task_groups)
    fix_context_text = _format_fix_context(
        fix_iteration, max_iterations,
        prev_issues=prev_issues,
        fix_history=fix_history,
    )

    prompt = SELF_CHECK_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        test_results=test_text,
        spec_content=spec_text,
        task_groups_section=task_groups_section,
        fix_context=fix_context_text,
    )

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Append available-specs names injection if applicable
    from ..context_builder import get_spec_names_injection
    spec_names = get_spec_names_injection(
        "self_check", project_root, step.inputs.get("relevant_specs"),
    )
    if spec_names:
        prompt += spec_names

    logger.info(
        f"Running self-check code review "
        f"#{pass_index}/{passes_required} (fix iteration: {fix_iteration})..."
    )

    try:
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
            required_keys=["issues"],
        )

        result = parse_json_response(response, required_keys=["issues"])

        if not result:
            step.error_message = "Failed to parse self-check result from LLM response"
            return StepStatus.FAILED

        issues = result.get("issues", [])

        step.outputs["self_check_result"] = result
        step.outputs["issues"] = issues
        step.outputs["actionable_count"] = len(issues)

        if not issues:
            logger.info(f"Self-check #{pass_index}/{passes_required} passed (no issues found)")
            return StepStatus.COMPLETED

        # NOTE: When convergence_enabled=True and N>1, pass #1 may return
        # COMPLETED via the convergence shortcut. Pass #2+ deliberately strips
        # prev_self_check_issues (no intra-round comparison), so if pass #2
        # finds the same issues it returns REVISION_NEEDED, triggering another
        # fix loop. This is intentional: convergence breaks stalled fix loops
        # across rounds, not within a single N-pass round.
        if convergence_enabled and _issues_converged(issues, prev_issues):
            logger.warning(
                f"Self-check #{pass_index}/{passes_required} converged: "
                f"{len(issues)} issue(s) match previous iteration's signatures "
                f"— stopping fix loop to avoid re-flagging already-fixed or "
                f"not-real issues"
            )
            step.outputs["converged"] = True
            step.outputs["convergence_reason"] = (
                f"Same {len(issues)} issue signature(s) reported in consecutive iterations"
            )
            step.outputs["unresolved_issues"] = list(issues)
            return StepStatus.COMPLETED

        logger.warning(
            f"Self-check #{pass_index}/{passes_required} found {len(issues)} "
            f"issue(s) (fix iteration {fix_iteration}/{max_iterations})"
        )

        issue_details = "\n".join(
            f"- [{i.get('severity', 'high')}] {i.get('location', '?')}: "
            f"{i.get('description', '')}"
            for i in issues
        )
        fix_instructions = (
            f"Self-check found {len(issues)} issue(s) that need fixing:\n"
            f"{issue_details}\n\n"
            "Fix the issues listed above and ensure the logic is correct."
        )

        step.outputs["fix_needed"] = True
        step.outputs["fix_iteration"] = fix_iteration
        step.outputs["max_fix_iterations"] = max_iterations
        step.outputs["fix_instructions"] = fix_instructions
        step.outputs["fix_context"] = {
            "reason": "self_check",
            "issues": issues,
            "iteration": fix_iteration + 1,
        }

        return StepStatus.REVISION_NEEDED

    except Exception as e:
        logger.exception("Self-check step failed")
        step.error_message = f"Self-check failed: {str(e)}"
        return StepStatus.FAILED


_DESC_STOPWORDS = frozenset({
    "a", "an", "the", "of", "on", "in", "at", "to", "for", "from",
    "is", "are", "be", "been", "by", "with", "and", "or", "not",
    "was", "were", "has", "have", "had", "this", "that", "these", "those",
    "can", "could", "should", "would", "may", "might", "will", "shall",
    "as", "but", "so", "if", "it", "its", "do", "does", "did", "no",
})
_DESC_PUNCT_RE = re.compile(r"[^\w\s]+")


def _normalize_description(text: str) -> str:
    """Normalize free-text issue descriptions for fuzzy convergence comparison.

    Lowercases, strips punctuation, drops common English stopwords, and sorts
    the remaining tokens. This makes minor LLM paraphrasing (different word
    order, inserted punctuation, added articles) compare equal, so the
    convergence check isn't defeated by trivial wording changes.
    """
    lower = text.lower()
    cleaned = _DESC_PUNCT_RE.sub(" ", lower)
    tokens = [t for t in cleaned.split() if t and t not in _DESC_STOPWORDS]
    tokens.sort()
    return " ".join(tokens)


def _issue_signature(issues: list) -> set:
    """Compute a set of (location, normalized_description) tuples for convergence detection.

    Location is stripped and lowercased. Description is token-normalized via
    _normalize_description so LLM paraphrasing of the same logical issue still
    hashes to the same signature.
    """
    sigs = set()
    for i in issues:
        if not isinstance(i, dict):
            continue
        loc = str(i.get("location", "")).strip().lower()
        desc = _normalize_description(str(i.get("description", "")))
        if loc or desc:
            sigs.add((loc, desc))
    return sigs


def _issues_converged(current_issues: list, prev_issues: list | None) -> bool:
    """Return True if the current issues appear to repeat the previous set.

    Signals the LLM is re-reporting the same issues after a fix attempt, meaning
    the fix loop has stalled. Requires at least one issue on each side.

    Detection is deliberately lenient — subset (not equality) semantics are
    intentional: when prev=[A,B,C] and current=[A], issue A has survived a
    full fix attempt and further iterations are unlikely to resolve it, so we
    stop. step.outputs["issues"] still carries the remaining issue list so
    downstream steps can react.

    A location-only second layer catches paraphrase-heavy convergence: if every
    current issue lives at a location already flagged by prev (regardless of
    wording), treat as converged. LLMs routinely rewrite descriptions for the
    same underlying problem.
    """
    if not prev_issues or not current_issues:
        return False
    current_sig = _issue_signature(current_issues)
    prev_sig = _issue_signature(prev_issues)
    if not current_sig or not prev_sig:
        return False
    if current_sig.issubset(prev_sig):
        return True
    current_locs = {loc for loc, _ in current_sig if loc}
    prev_locs = {loc for loc, _ in prev_sig if loc}
    if current_locs and prev_locs and current_locs.issubset(prev_locs):
        return True
    return False


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
                lines.append(f"Output (last {PHASE_STDOUT_TAIL_CHARS} chars):\n{stdout[-PHASE_STDOUT_TAIL_CHARS:]}")
            stderr = phase.get("stderr", "")
            if stderr and not passed:
                lines.append(f"Stderr (last {PHASE_STDERR_TAIL_CHARS} chars):\n{stderr[-PHASE_STDERR_TAIL_CHARS:]}")

        return "\n".join(lines)

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


def _format_spec_content(spec_content) -> str:
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


def _format_fix_context(
    fix_iteration: int,
    max_iterations: int,
    prev_issues: list | None = None,
    fix_history: list | None = None,
) -> str:
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

    if prev_issues:
        lines.append("")
        lines.append("## Previously Reported Issues")
        lines.append("The following issues were reported in the previous self-check.")
        lines.append("Only report issues that STILL EXIST after the fix attempt.")
        lines.append("Do NOT re-report issues that have been successfully fixed.")
        lines.append("")
        for issue in prev_issues:
            severity = issue.get("severity", "high")
            desc = issue.get("description", "")
            location = issue.get("location", "")
            loc_suffix = f" @ {location}" if location else ""
            lines.append(f"- [{severity}] {desc}{loc_suffix}")

    if fix_history:
        lines.append("")
        lines.append("## Fix History")
        for entry in fix_history:
            it = entry.get("iteration", "?")
            reason = entry.get("reason", "unknown")
            trigger = entry.get("trigger_step_type", "unknown")
            lines.append(f"- Iteration {it}: triggered by {trigger} ({reason})")

    return "\n".join(lines)
