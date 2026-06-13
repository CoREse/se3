"""Verify Spec step handler.

Checks implementation against specifications for consistency.
Uses LLM to verify that requirements are met.
Detects test failures and triggers fix loop when appropriate.

Scope of this check (see ``engine.spec_role`` for the canonical definition):
this is a **within-flow drift guard**, not a claim that the spec overrides the
code. For the duration of the current flow the already-recorded spec acts as
the implementation contract *for that flow*, so the implementation does not
silently drift away from requirements while it is being built. se3 stays
code-first overall — code is primary and ``se3 sync`` refreshes the spec from
the code; verify_spec only prevents mid-flow drift, it does not make the spec
authoritative over the code in general.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary
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

## Spec Access Protocol (index-first — do NOT read whole specs or the index cache)

The selected Requirements are embedded above. If you need MORE spec context than
is shown, obtain it through the read-only `se3 spec` index commands — this is a
read-only step, so the Bash and Read tools are available:

- `se3 spec index` — root view: every spec's name, a one-sentence locator, and item count.
- `se3 spec index <spec> [<group>...]` — drill into one spec's Requirement index; trailing group-path components open a folded domain group or a `pN` page.
- `se3 spec show <spec>::<requirement>` — the authoritative body of ONE Requirement plus its physical location (file path + line range).

You MUST NOT, for the purpose of gathering context, read an entire large spec
file with the Read tool, and you MUST NOT read the index cache file
`se3/cache/spec-index.json` directly — it is an internal, program-maintained
format. Query the index commands above instead, then `se3 spec show` only the
specific Requirements you need.

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

# Two-segment marker only: USER_CONTENT region is empty.
# verify_spec consumes upstream artifacts (changes_made / test_results /
# spec_content / spec_changes / relevant_specs); no user-literal field is
# appended here. The web console renders the whole post-BEGIN tail inside
# the collapsed system-prompt chip.
VERIFY_PROMPT = inject_boundary(VERIFY_PROMPT, "## Task Description\n")


def verify_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the verify_spec step.

    Verifies implementation against specifications using LLM.
    Detects test failures and triggers REVISION_NEEDED when appropriate.

    The ``verified`` field is computed by rule, not by LLM:
        verified = (in_scope_count == 0) and tests_passed

    where ``tests_passed`` consumes the SAME baseline-based verdict as the
    test step (steps/test.py): only *introduced* test failures (not in the
    frozen pre-implement baseline) block the flow. Inherited (baseline)
    failures are surfaced/logged but never drive REVISION_NEEDED — looping on
    failures a scoped flow structurally cannot fix is the infinite-loop bug
    the baseline mechanism eliminates.

    REVISION_NEEDED is triggered when:
        in_scope_count > 0 or tests_passed == False

    Out-of-scope issues are logged (留痕) via ``_log_out_of_scope_issues``,
    NOT filed as tracked issues, to avoid the issue tracker ballooning across
    fix iterations.

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
    # Frozen pre-implement baseline (injected by state_machine). Used only as a
    # fallback to recompute the introduced/inherited split when an older
    # test_results dict predates the baseline split fields.
    baseline_failures = step.inputs.get("baseline_failures", []) or []

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
    from ..context_builder import (
        get_issue_discovery_injection,
        get_runtime_environment_injection,
    )
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    injection = get_issue_discovery_injection("verify_spec", project_root)
    if injection:
        prompt += injection

    # NOTE: the legacy ``get_spec_names_injection`` is deliberately NOT appended
    # here. Its guidance permits reading ``se3/specs/<name>/spec.md`` directly,
    # which directly contradicts this step's index-first Spec Access Protocol
    # (do NOT read whole specs). The root view obtained via ``se3 spec index``
    # already enumerates every spec, so the names list is both redundant and
    # contradictory; appending it would let the LLM follow the later injection
    # and read an entire large spec, defeating the bounded-context protocol.

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("verify_spec", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(f"Verifying implementation against specifications (fix iteration: {fix_iteration})...")

    try:
        # Call LLM for verification
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
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

        # Check test results — consume the SAME baseline-based verdict that
        # the test step (steps/test.py) computed, so the two steps can never
        # disagree on whether the test failures block the flow.
        #
        # The fix-loop test gate blocks on *introduced* failures only (failures
        # NOT in the frozen pre-implement baseline). Inherited (baseline)
        # failures are surfaced/logged below but never drive REVISION_NEEDED —
        # looping on failures a scoped flow structurally cannot fix is exactly
        # the infinite-loop bug the baseline mechanism eliminates.
        critical_skipped: list[str] = []
        critical_missing: list[str] = []
        inherited_failures: list[str] = []
        if test_results and isinstance(test_results, dict):
            tests_passed = _evaluate_test_gate(test_results, baseline_failures)
            inherited_failures = list(test_results.get("inherited_failures") or [])

            # Defensive backstop for the critical acceptance gate, scoped to
            # THIS session's tests. The test step flags critical acceptance
            # tests that were skipped or never collected via these fields. As
            # the authoritative ``verified`` computation point, verify_spec
            # consumes them explicitly so a skipped/missing critical test can
            # never count as passing — even if some upstream branch left the
            # gate truthy. Skip != pass for these tests.
            critical_skipped = list(test_results.get("critical_skipped") or [])
            critical_missing = list(test_results.get("critical_missing") or [])
            if (critical_skipped or critical_missing) and tests_passed:
                logger.warning(
                    "Critical acceptance test(s) skipped/missing "
                    f"(skipped={critical_skipped}, missing={critical_missing}); "
                    "marking tests as not passed"
                )
                tests_passed = False
        else:
            tests_passed = True

        # Surface inherited (baseline) failures once (留痕). They do not block
        # the flow, but a correctly-scoped flow that commits its work should
        # still leave a trace of the pre-existing red tests it could not fix.
        if inherited_failures:
            logger.info(
                "%d inherited (pre-implement baseline) test failure(s) present "
                "but NOT blocking verify_spec (surfaced, not looped): %s",
                len(inherited_failures),
                ", ".join(str(t) for t in inherited_failures),
            )

        # Rule-based verified: ignore the LLM's verified field. verified is
        # True only when there are no in-scope spec issues AND no introduced
        # test failures AND the critical acceptance gate is satisfied.
        # Inherited failures are deliberately excluded from ``tests_passed`` so
        # a correctly-scoped flow can commit its work.
        verified = (in_scope_count == 0) and tests_passed

        # Bridge the authoritative verdict into the verification_result dict so
        # downstream consumers that only receive ``verification_result`` (the
        # summarize step) read the rule-based ``verified`` rather than the LLM's
        # untrustworthy/absent value. This overwrites any ``verified`` the LLM
        # may have emitted, keeping the rule the single source of truth.
        if isinstance(verification, dict):
            verification["verified"] = verified

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

        # Log out-of-scope issues (留痕) instead of filing them as tracked
        # issues, to avoid the issue tracker ballooning across fix iterations.
        _log_out_of_scope_issues(out_of_scope_issues, flow, project_root)

        test_analysis = verification.get("test_analysis", {})
        fix_instructions = verification.get("fix_instructions", "")

        # If tests failed but LLM didn't provide fix instructions, generate default
        if not tests_passed and not fix_instructions:
            stdout = (test_results.get("stdout") or "") if isinstance(test_results, dict) else ""
            stderr = (test_results.get("stderr") or "") if isinstance(test_results, dict) else ""
            fix_instructions = f"Tests are failing. Please review and fix the implementation.\n\nTest output:\n{_extract_failures_section(stdout, max_chars=FAILURES_SECTION_MAX_CHARS)}\n\nStderr:\n{stderr[-FIX_STDERR_TAIL_CHARS:]}"
            logger.warning("Tests failed but LLM didn't provide fix instructions - using default")

        # Prepend targeted guidance when the gate failed because a critical
        # acceptance test was skipped or is missing. These never surface in a
        # FAILURES section, so without this the implement step would see only
        # generic test output (or none) and miss why verification failed.
        if critical_skipped or critical_missing:
            fix_instructions = _build_critical_fix_instructions(
                critical_skipped, critical_missing, fix_instructions,
            )

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
                "reason": (
                    "critical_acceptance_not_verified"
                    if (critical_skipped or critical_missing)
                    else "test_failure"
                ),
                "iteration": fix_iteration + 1,
                "critical_skipped": critical_skipped,
                "critical_missing": critical_missing,
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


def _build_critical_fix_instructions(
    critical_skipped: list[str],
    critical_missing: list[str],
    base_instructions: str,
) -> str:
    """Build fix instructions for a critical acceptance gate failure.

    A skipped or missing critical acceptance test is treated as a
    verification failure (skip != pass): the test never ran its assertions,
    so the implementation is not actually verified. This guidance tells the
    implement step to make the test run for real rather than silence it.
    Any pre-existing ``base_instructions`` (e.g. LLM-provided or default
    test output) are preserved underneath.
    """
    parts: list[str] = []
    if critical_skipped:
        parts.append(
            "The following CRITICAL acceptance test(s) were SKIPPED and "
            "therefore did NOT actually run:\n  - "
            + "\n  - ".join(critical_skipped)
        )
    if critical_missing:
        parts.append(
            "The following CRITICAL acceptance test pattern(s) matched neither "
            "a test that ran nor a test that was skipped — they appear to be "
            "MISSING (renamed, un-collected due to an import error, or a typo "
            "in the configured pattern):\n  - "
            + "\n  - ".join(critical_missing)
        )

    note = (
        "CRITICAL ACCEPTANCE TESTS NOT VERIFIED (skip/missing == verification "
        "failure)\n\n"
        + "\n\n".join(parts)
        + "\n\nThese critical acceptance tests MUST actually run and pass; a "
        "skip or a missing test is treated as a verification failure (skip != "
        "pass). Do NOT silence them with skip guards. Instead: install any "
        "required dependencies so the test can run, remove the skip guard, and "
        "fix test collection (imports/renames) so each critical test is "
        "collected and executed. If the feature under test is genuinely broken, "
        "fix the implementation so the assertions pass."
    )

    if base_instructions:
        return f"{note}\n\n{base_instructions}"
    return note


def _compute_introduced_failures(
    test_results: dict[str, Any],
    baseline_failures: list[str],
) -> list[str]:
    """Recompute the introduced-failure list from a test_results dict.

    Used only as a fallback when ``test_results`` predates the baseline split
    (no ``tests_blocking`` / ``introduced_failures`` field). Mirrors test.py's
    classification: a failing test is *introduced* iff its id is NOT in the
    frozen pre-implement baseline.
    """
    baseline = set(baseline_failures or [])
    new_tests = test_results.get("new_tests") or {}
    regression = test_results.get("regression") or {}
    all_failed = list(new_tests.get("failed") or []) + list(
        regression.get("failed") or []
    )
    return [tid for tid in all_failed if tid not in baseline]


def _evaluate_test_gate(
    test_results: dict[str, Any],
    baseline_failures: list[str],
) -> bool:
    """Compute ``tests_passed`` from a test_results dict (baseline-aware).

    Consumes the SAME baseline-based verdict as the test step so the two steps
    never disagree on whether test failures block the flow:

    - **Primary** — ``test_results["tests_blocking"]``: the authoritative flag
      test.py sets (True iff there are introduced failures, an unparseable
      failure, or a critical-gate trip). ``tests_passed = not tests_blocking``.
    - **Secondary** — ``introduced_failures`` present without the blocking
      flag: block iff that list is non-empty.
    - **Fallback** — ``test_results`` predates the baseline split: recompute
      the introduced-failure set from the structured new_tests/regression
      lists against the injected ``baseline_failures``; block on any introduced
      failure or an unparseable failure (pytest failed yet no individual test
      could be classified). Oldest flat-format results with no structure fall
      back to ``overall_passed``/``passed`` (with a non-zero returncode
      overriding a stale truthy ``passed``).

    Inherited (baseline) failures never make this return ``False``.
    """
    # Primary: authoritative verdict from test.py.
    if "tests_blocking" in test_results:
        return not bool(test_results["tests_blocking"])

    # Secondary: introduced_failures present without the blocking flag.
    if "introduced_failures" in test_results:
        return not bool(test_results.get("introduced_failures"))

    # Fallback: structured results present — recompute the introduced split.
    if "new_tests" in test_results or "regression" in test_results:
        introduced = _compute_introduced_failures(test_results, baseline_failures)
        if introduced:
            return False
        overall_passed = test_results.get(
            "overall_passed", test_results.get("passed", False)
        )
        new_failed = (test_results.get("new_tests") or {}).get("failed", [])
        reg_failed = (test_results.get("regression") or {}).get("failed", [])
        # Unparseable failure: pytest failed yet no individual test could be
        # classified — block (mirrors test.py's unparseable_failure trigger).
        if not overall_passed and not new_failed and not reg_failed:
            return False
        return True

    # Oldest flat format: no structure to split — honor the raw verdict.
    passed = bool(
        test_results.get("overall_passed", test_results.get("passed", False))
    )
    returncode = test_results.get("returncode", 0)
    if returncode not in (0, "?", None) and passed:
        logger.warning(
            "Test return code is %s but passed is truthy; marking as failed",
            returncode,
        )
        return False
    return passed


def _log_out_of_scope_issues(
    out_of_scope_issues: list[dict[str, Any]],
    flow: FlowInstance,
    project_root: Path,
) -> None:
    """Log out-of-scope observations (留痕) instead of filing them as issues.

    Out-of-scope items are surfaced to the flow log / telemetry but
    deliberately NOT filed via ``IssueManager.create()``: filing one issue per
    out-of-scope observation per fix iteration is exactly the issue-explosion
    (189 duplicate issues) this change eliminates. Provenance ≠ relevance — an
    LLM-reported out-of-scope item is best-effort discovery, so it is recorded
    for a human to triage rather than auto-filed and never silently dropped.
    """
    if not out_of_scope_issues:
        return

    for issue_data in out_of_scope_issues:
        message = issue_data.get("message", "Untitled out-of-scope issue")
        suggestion = issue_data.get("suggestion", "")
        priority = issue_data.get("priority", "medium")
        logger.info(
            "verify_spec out-of-scope observation (logged, not filed) "
            "[priority=%s]: %s%s",
            priority,
            message,
            f" | suggestion: {suggestion}" if suggestion else "",
        )


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
