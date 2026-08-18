"""Test step handler.

Runs tests to verify the implementation.
This is a non-LLM step that executes test commands.

Supports:
- Auto-detection of project type (Python/Node/Rust/Go)
- Custom test command via tianluo.yaml test.command
- Multi-phase test execution via tianluo.yaml test.phases
- Opt-in pytest-xdist parallelism for the primary command via test.parallel
- Result classification into new_tests vs regression
- Fix-loop aware phase filtering
- Dot progress indicator during test execution
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from ...i18n import t
from ..baseline_fix_memory import load_given_up, record_given_up
from ..models import FlowInstance, Step, StepStatus
from ._project_root import resolve_flow_project_root
from ..truncation import (
    FAILURES_SECTION_MAX_CHARS,
    FIX_STDERR_TAIL_CHARS,
    TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS,
    TEST_HISTORY_STDERR_TAIL_CHARS,
    TEST_HISTORY_STDOUT_TAIL_CHARS,
)
from .implement import _sanitize_estimated_test_duration

logger = logging.getLogger(__name__)


@dataclass
class TestVerdict:
    """The classified result of one test-suite run.

    The shared core (:func:`run_and_classify_tests`) returns this so that both
    the ``test`` step handler and mechanism A's SPEC_GATE step can re-run the
    full suite through one code path (same command, phases, dynamic timeout,
    critical-gate detection, and baseline provenance split) and consume an
    identical verdict — a single source of truth that cannot drift between the
    two callers.

    Fields:
        test_results: the structured ``test_results`` dict written to
            ``step.outputs["test_results"]`` verbatim by the handler.
        overall_passed: whether the run passed (the raw pytest gate, reflecting
            actual exit status plus the critical-acceptance gate). Written to
            ``step.outputs["tests_passed"]``.
        should_fix: the authoritative fix-loop trigger (``tests_blocking``).
            True for introduced/critical failures AND for in-budget baseline
            failures (mechanism B).
        fix_instructions: LLM-facing fix guidance (empty when ``should_fix`` is
            False).
        fix_context: structured fix context for the implement step (empty when
            ``should_fix`` is False).
        inherited_list: the inherited (baseline) failures surfaced for 留痕 and
            once-per-flow issue filing (``[{"test_id", "reason"}]``).
    """

    test_results: Dict[str, Any]
    overall_passed: bool
    should_fix: bool
    fix_instructions: str = ""
    fix_context: Dict[str, Any] = field(default_factory=dict)
    inherited_list: List[Dict[str, str]] = field(default_factory=list)


def _extract_failures_section(stdout: str, max_chars: int = FAILURES_SECTION_MAX_CHARS) -> str:
    """Extract the FAILURES/ERRORS section from pytest output.

    Intelligently extracts diagnostic information from pytest output:
    1. Locates '= FAILURES =' or '= ERRORS =' section boundaries
    2. If content fits within max_chars, returns it in full
    3. If too long, truncates each test block keeping the last traceback
       frames and assertion message
    4. Falls back to the last max_chars of stdout if no section found

    Args:
        stdout: Full pytest stdout output
        max_chars: Maximum characters to return

    Returns:
        Extracted diagnostic text, or empty string if stdout is empty
    """
    if not stdout:
        return ""

    # Try to locate FAILURES or ERRORS section
    # pytest uses patterns like "= FAILURES =" or "= ERRORS ="
    section_start = re.search(
        r'^={2,}\s+(FAILURES|ERRORS)\s+=', stdout, re.MULTILINE,
    )
    if not section_start:
        # No FAILURES/ERRORS section — fallback to tail
        return stdout[-max_chars:]

    # Find the end of the section (next '=' separator line or end of string)
    section_body = stdout[section_start.start():]
    # The section ends at the next top-level separator (e.g., "= short test summary info =")
    end_match = re.search(
        r'\n={2,}\s+(?!FAILURES|ERRORS)', section_body[1:],
    )
    if end_match:
        section_text = section_body[: end_match.start() + 1]
    else:
        section_text = section_body

    # If the section fits, return it entirely
    if len(section_text) <= max_chars:
        return section_text

    # Section is too long — truncate per test block
    # pytest separates test blocks with lines like "_ test_name _"
    block_pattern = re.compile(r'^_{2,}\s+.+\s+_{2,}$', re.MULTILINE)
    block_starts = [m.start() for m in block_pattern.finditer(section_text)]

    if not block_starts:
        # Can't parse blocks — return tail of section
        return section_text[-max_chars:]

    # Split into blocks
    blocks: list[str] = []
    for i, start in enumerate(block_starts):
        end = block_starts[i + 1] if i + 1 < len(block_starts) else len(section_text)
        blocks.append(section_text[start:end])

    # Budget per block
    header_line = section_text[: block_starts[0]] if block_starts[0] > 0 else ""
    available = max_chars - len(header_line) - 40  # 40 chars overhead
    per_block = max(200, available // max(len(blocks), 1))

    truncated_blocks: list[str] = []
    for block in blocks:
        if len(block) <= per_block:
            truncated_blocks.append(block)
        else:
            # Keep the block header line + last portion (assertion + traceback tail)
            first_newline = block.find("\n")
            block_header = block[: first_newline + 1] if first_newline != -1 else ""
            remaining_budget = per_block - len(block_header) - 20
            tail = block[-remaining_budget:] if remaining_budget > 0 else ""
            truncated_blocks.append(
                block_header + "    ... (truncated) ...\n" + tail,
            )

    result = header_line + "".join(truncated_blocks)
    return result[:max_chars]


def _parse_test_summary_counts(stdout: str) -> tuple[int, int] | None:
    """Best-effort parse of aggregate pass/fail counts from a runner summary.

    Fallback for when per-test lines (:func:`_parse_test_ids`) are absent —
    quiet pytest (no ``-v``), cargo test, jest summary, etc. Recognizes the
    aggregate summary line emitted by common runners:

    - pytest:  ``=== 5 passed, 2 failed, 1 skipped in 1.2s ===``
    - cargo:   ``test result: ok. 5 passed; 0 failed; 0 ignored; ...``
    - jest:    ``Tests: 2 failed, 5 passed, 7 total``

    The generic ``\\d+ passed`` / ``\\d+ failed`` token search covers all three
    (cargo and jest both use the lowercase ``passed`` / ``failed`` wording, as
    does pytest's final summary line — distinct from the per-test ``PASSED`` /
    ``FAILED`` upper-case lines handled by :func:`_parse_test_ids`).

    Counts are summed across *every* summary line rather than across every
    token occurrence: a cargo workspace run emits one ``test result:`` line per
    test binary (e.g. ``7 passed`` then ``5 passed`` then ``3 passed``), so
    totalling each binary's numbers gives the phase-wide count instead of
    understating it with just the first binary's numbers. Identical summary
    lines are NOT de-duplicated — two test binaries that each independently
    report the same numbers (e.g. a workspace where two crates each run
    ``5 passed; 0 failed``) are genuinely separate results that must total to
    ``10 passed``, not be collapsed to ``5``. Single-run output (pytest / jest,
    one summary line) is unaffected — one line yields exactly its own counts.
    A pytest-xdist run is likewise a single-summary-line run: the workers report
    back to one controller, so the parallel output carries exactly one aggregate
    line and the summing behaviour above does not inflate it (covered by a test
    over a recorded xdist run).

    Returns ``(passed, failed)``, or ``None`` when no recognizable count is
    found (so the caller can fall back to a truthful phase-level statement).
    """
    if not stdout:
        return None
    passed = 0
    failed = 0
    found = False
    for line in stdout.splitlines():
        passed_tokens = re.findall(r'(\d+)\s+passed', line)
        failed_tokens = re.findall(r'(\d+)\s+failed', line)
        if not passed_tokens and not failed_tokens:
            continue
        passed += sum(int(n) for n in passed_tokens)
        failed += sum(int(n) for n in failed_tokens)
        found = True
    if found:
        return passed, failed
    return None


def _summarize_passed_phase_output(stdout: str, stderr: str) -> tuple[str, str]:
    """Build the slimmed archive summary for a PASSED test phase.

    A passed phase's full ``pytest -v`` stdout is pure noise in the archived
    history (every line is a ``... PASSED`` line). Replace it with a compact
    summary: a passed/failed count line plus the tail of the output (which keeps
    the final ``=== N passed in Ts ===`` summary line). Both stdout and stderr
    tails are bounded by the centralized ``TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS``
    limit.

    The count line is derived with a three-tier fallback so a passing custom /
    quiet-pytest / cargo phase is NOT archived with a false ``0 passed,
    0 failed`` header:

    1. per-test lines parsed by :func:`_parse_test_ids` (verbose pytest / jest /
       go), counted directly;
    2. failing that, the aggregate runner summary parsed by
       :func:`_parse_test_summary_counts` (quiet pytest / cargo / jest summary);
    3. failing that, a truthful phase-level statement — this function is only
       called for a PASSED phase, so we can honestly report that it passed even
       when per-test counts cannot be parsed, rather than fabricating zeros.

    Returns ``(slimmed_stdout, slimmed_stderr)``.
    """
    stdout = stdout or ""
    stderr = stderr or ""
    ids = _parse_test_ids(stdout)
    if ids:
        passed = sum(1 for _tid, ok in ids if ok)
        failed = sum(1 for _tid, ok in ids if not ok)
        count_line = f"{passed} passed, {failed} failed"
    else:
        counts = _parse_test_summary_counts(stdout)
        if counts is not None:
            passed, failed = counts
            count_line = f"{passed} passed, {failed} failed"
        else:
            # Truthful fallback: the phase passed (this helper is only called
            # for passed phases) but no per-test or aggregate count could be
            # parsed from the runner output.
            count_line = "passed (per-test counts unavailable)"
    tail = stdout[-TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS:]
    slimmed_stdout = (
        f"[archived summary — passed phase: {count_line}; "
        f"full stdout omitted to keep history compact, output tail follows]\n"
        f"{tail}"
    )
    slimmed_stderr = stderr[-TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS:]
    return slimmed_stdout, slimmed_stderr


def _slim_passed_phase_archive(test_results: Dict[str, Any]) -> None:
    """Slim the STORED stdout/stderr of PASSED phases in ``test_results``.

    Mutates ``test_results`` in place: for every phase whose ``passed`` flag is
    True, replaces its ``stdout`` / ``stderr`` with the compact summary from
    :func:`_summarize_passed_phase_output`. The top-level ``stdout`` / ``stderr``
    mirror the primary (default) phase, so they are slimmed together based on the
    top-level ``passed`` flag. Failed phases are left untouched so their full
    output remains available for diagnosis.
    """
    for phase in test_results.get("phases", []):
        if phase.get("passed"):
            phase["stdout"], phase["stderr"] = _summarize_passed_phase_output(
                phase.get("stdout", ""), phase.get("stderr", ""),
            )
    # Top-level stdout/stderr are a copy of the primary (default) phase result.
    if test_results.get("passed"):
        test_results["stdout"], test_results["stderr"] = _summarize_passed_phase_output(
            test_results.get("stdout", ""), test_results.get("stderr", ""),
        )


def _render_estimate(value: float | int | None) -> str:
    """Render an estimated_test_duration for display.

    The LLM is asked to output an integer; sanitation stores it as a float
    for numeric consistency. When whole-valued, render as int to avoid
    echoing '300.0' back where the LLM wrote '300'.
    """
    if value is None:
        return "not set"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def test_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the test step.

    Thin shell around the shared :func:`run_and_classify_tests` core: it loads
    the test config, runs+classifies the suite, then performs the step-level
    side effects (write outputs, record history, file the inherited-failure
    issue at most once per flow) and maps the verdict onto a ``StepStatus``.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success,
        StepStatus.REVISION_NEEDED when tests fail (triggers fix loop),
        StepStatus.FAILED when the test environment itself is unusable (e.g.
        ``test.parallel`` is set but pytest-xdist is not installed) — that is
        not a code defect, so it goes to the human instead of the fix loop.
    """
    from ...config import TestConfig

    project_root = resolve_flow_project_root(flow)
    config = TestConfig.load(project_root)

    try:
        verdict = run_and_classify_tests(
            project_root=project_root,
            flow=flow,
            tests_added=step.inputs.get("tests_added", []),
            baseline_failures=step.inputs.get("baseline_failures") or [],
            is_fix_iteration=step.inputs.get("is_fix_iteration", False),
            fix_iteration=step.inputs.get("fix_iteration", 0),
            estimated_test_duration=step.inputs.get("estimated_test_duration"),
            config=config,
        )
    except XdistUnavailableError as exc:
        return _fail_test_environment(step, exc)

    # Write structured outputs.
    step.outputs["test_results"] = verdict.test_results
    step.outputs["tests_passed"] = verdict.overall_passed
    # Retain the legacy ``pre_existing_failures`` output key for backward
    # compatibility with downstream renderers; it now carries baseline-inherited
    # failures rather than known-list failures.
    step.outputs["pre_existing_failures"] = verdict.inherited_list
    step.outputs["inherited_failures"] = verdict.inherited_list

    # Surface the "this project looks like a fit for e2e" hint, at most once per
    # flow (the fix loop re-runs this step, and repeating the same sentence every
    # iteration would turn a suggestion into noise).
    if not flow.state.context.get("e2e_suggestion_shown"):
        suggestion = _e2e_enable_suggestion(project_root)
        if suggestion:
            step.outputs["e2e_suggestion"] = suggestion
            flow.state.context["e2e_suggestion_shown"] = True

    # Record test results in history (phase_results == test_results["phases"]).
    _record_test_history(
        project_root, flow, step,
        verdict.test_results.get("phases", []),
        verdict.overall_passed,
    )

    if not verdict.overall_passed and verdict.should_fix:
        stderr = verdict.test_results.get("stderr", "") or ""
        stderr_tail = stderr[-FIX_STDERR_TAIL_CHARS:] if stderr else ""
        step.error_message = f"Tests failed:\n{stderr_tail}"

    # Report inherited failures via A-class issue discovery — at most ONCE per
    # flow. Each fix iteration re-runs the test step on the same baseline
    # failures; without this flow-level guard the same issue would be re-filed
    # every iteration (the 189-duplicate-issue explosion this guard fixes).
    if verdict.inherited_list and not flow.state.context.get("inherited_failures_filed"):
        _report_pre_existing_issues(project_root, flow, verdict.inherited_list)
        flow.state.context["inherited_failures_filed"] = True

    if verdict.should_fix:
        step.outputs["fix_needed"] = True
        step.outputs["fix_instructions"] = verdict.fix_instructions
        step.outputs["fix_context"] = verdict.fix_context
        return StepStatus.REVISION_NEEDED

    return StepStatus.COMPLETED


def _fail_test_environment(step: Step, exc: XdistUnavailableError) -> StepStatus:
    """Record a host-side test-environment problem as FAILED + guidance.

    INVARIANT: ``fix_needed`` is deliberately NOT written here. It is the single
    flag the state machine consults to enter the fix loop, so leaving it unset
    is what keeps a missing pytest-xdist out of the fix loop (and out of the
    fix-iteration budget) and routes it to the human instead — the same contract
    the e2e step's environment failures follow.
    """
    logger.error("test environment failure: %s", exc.message)
    step.outputs["environment_error"] = exc.message
    step.outputs["test_remediation"] = exc.remediation
    step.outputs["tests_passed"] = False
    step.outputs["test_results"] = {
        "passed": False,
        "overall_passed": False,
        "environment_error": exc.message,
        "remediation": exc.remediation,
        "phases": [],
    }
    step.error_message = "\n".join(
        part for part in (exc.message, exc.remediation) if part
    )
    return StepStatus.FAILED


def _e2e_enable_suggestion(project_root: Path) -> str:
    """Text suggesting the user enable e2e, or ``""`` when there is nothing to say.

    WHY it is raised from *this* step: the ``E2E`` step only joins the sequence
    once ``e2e.enabled`` is true, so a suggestion to turn the switch on can only
    come from a step that runs while it is off — and the test step is exactly
    where e2e would sit. The flow says it and never writes it: flipping
    ``e2e.enabled`` asserts something about the user's machine (an unprivileged
    Docker or Podman) and about how much time the fix loop may spend, which is
    theirs to promise.

    Imported inside the function for the charter's core/extra isolation, and
    failure-tolerant because a hint must never be able to fail a passing suite.
    """
    try:
        from ...e2e.bootstrap import suggest_enable

        return suggest_enable(project_root)
    except Exception as exc:  # pragma: no cover - defensive; a hint is optional
        logger.debug("e2e enable suggestion unavailable: %s", exc)
        return ""


def run_and_classify_tests(
    project_root: Path,
    flow: FlowInstance,
    tests_added: List[str],
    baseline_failures: List[str],
    is_fix_iteration: bool,
    fix_iteration: int,
    estimated_test_duration: Any,
    config: Any,
) -> TestVerdict:
    """Run the full test suite and classify the result into a :class:`TestVerdict`.

    This is the single source of truth shared by the ``test`` step and
    mechanism A's SPEC_GATE re-test: it runs the primary command + phases with
    the same dynamic timeout, performs the critical-acceptance gate, splits
    failures into inherited (baseline) vs introduced, applies mechanism B
    (bounded looping of inherited baseline failures), and builds the
    fix_instructions / fix_context for the fix loop.

    It is intentionally decoupled from ``Step`` so callers with a different step
    object (e.g. SPEC_GATE) can reuse it. Step-level side effects (writing
    outputs, recording history, the once-per-flow issue filing) stay in the
    caller; the only persistent side effect performed here is recording a
    baseline failure as *given up* when its independent budget is exhausted.

    Args:
        project_root: Project root directory.
        flow: The flow instance (used to read ``baseline_fix_attempts`` from the
            flow context for mechanism B's per-flow budget).
        tests_added: New test file paths from the implement step.
        baseline_failures: The frozen pre-implement baseline failing test ids.
        is_fix_iteration: Whether this run is inside a fix iteration (controls
            phase filtering).
        fix_iteration: The current fix-loop iteration count (for fix_context).
        estimated_test_duration: The implement step's runtime estimate (drives
            the dynamic timeout); may be None / invalid (falls back to config).
        config: A ``TestConfig`` (command, timeouts, phases, critical_tests).

    Returns:
        A :class:`TestVerdict`.
    """
    # Compute dynamic timeout for primary test command.
    # - Minimum floor prevents instantaneous timeouts if the LLM returns a
    #   nonsense estimate (e.g. 0 or 1 second).
    # - Maximum cap prevents runaway compounding in the fix loop: without
    #   it, repeated timeouts cause the LLM to escalate estimated_test_duration
    #   each iteration, which the multiplier then scales further — a hung
    #   test could end up with an hours-long timeout.
    raw_estimated_test_duration = estimated_test_duration
    # Reuse the implement-side sanitizer so the "valid estimate" contract is
    # defined once: any future tightening (NaN, upper sanity bound, …) stays
    # consistent between the producer (implement) and the consumer (test).
    sanitized_estimated_duration = _sanitize_estimated_test_duration(
        raw_estimated_test_duration
    )
    if sanitized_estimated_duration is not None:
        computed = int(sanitized_estimated_duration * config.timeout_multiplier)
        floored = max(computed, config.min_dynamic_timeout)
        primary_timeout = min(floored, config.max_dynamic_timeout)
        # Signal whether the cap clamped the computed value — raising the
        # estimate further would not produce a larger timeout.
        primary_timeout_at_cap = computed > config.max_dynamic_timeout
        logger.info(
            "Using dynamic timeout: %ds (estimated_test_duration=%s, multiplier=%.1f, floor=%ds, cap=%ds%s)",
            primary_timeout,
            sanitized_estimated_duration,
            config.timeout_multiplier,
            config.min_dynamic_timeout,
            config.max_dynamic_timeout,
            ", clamped at cap" if primary_timeout_at_cap else "",
        )
    else:
        primary_timeout = config.timeout
        primary_timeout_at_cap = False
        logger.info("Using fallback timeout: %ds (no usable estimated_test_duration)", primary_timeout)

    # 1. Run primary test command
    primary_command = (
        shlex.split(config.command) if config.command
        else _detect_test_command(project_root)
    )
    # When critical acceptance tests are configured, make sure a pytest
    # command surfaces skipped tests so the critical skip/missing gate can
    # parse them (no-op when no critical_tests are configured).
    primary_command = _ensure_verbose_pytest(
        primary_command, bool(config.critical_tests),
    )
    # Parallelism is applied HERE and nowhere else: the primary command is
    # already framework-shaped (auto-detected, -v appended above), whereas the
    # phases below are the user's own commands and run verbatim.
    parallel = getattr(config, "parallel", None)
    primary_command = _apply_parallel(primary_command, parallel)
    # True exactly when the run about to happen depends on xdist: the switch is
    # on and the command is pytest, so it carries -n either because we appended
    # it or because the user had already written one. Both halves of the
    # missing-plugin diagnosis key off this, so a project that pinned its own
    # -n gets the same actionable error instead of a fix loop.
    parallel_active = bool(parallel) and _is_pytest_command(primary_command)
    if parallel_active:
        # Raises before a single test runs when the test interpreter has no
        # xdist — the whole point is that a missing plugin reaches the user as
        # an install instruction, never as a suite full of failures.
        _preflight_xdist(primary_command, parallel, project_root)
    # 1b. Timeout retry: a timeout is NOT an assertion/test failure — it can be
    #     a transient slowdown (machine load, cold caches, a one-off hang). Before
    #     treating it as a real failure, retry the command ONCE in place with the
    #     same command and timeout (see _run_command_with_timeout_retry). The
    #     retry recognizes the full set of timeout-class signals (structured
    #     timed_out flag, the 'Timeout after' marker, or the returncode==-1
    #     timeout sentinel) rather than only the structured flag, and does NOT
    #     increment the fix_iteration
    #     counter. Only if the retry ALSO times out do we fall through to the
    #     existing timeout→fix_context path (which explicitly labels the failure
    #     as a timeout, not an assertion failure).
    primary_result, primary_retried_after_timeout = _run_command_with_timeout_retry(
        primary_command, project_root, primary_timeout, "primary test command",
    )
    if parallel_active and _is_missing_xdist_result(primary_result):
        # The bare-`pytest` counterpart of the pre-flight probe: same absence,
        # same actionable error, just diagnosed from the output because the
        # command never named an interpreter to probe.
        raise _xdist_unavailable_error(parallel)

    # 2. Classify primary results
    new_tests, regression = _classify_results(
        primary_result["stdout"], tests_added,
    )

    # 3. Run additional phases
    phases_to_run = config.get_phases_for_run(is_fix_iteration)
    phase_results = [
        {"name": "default", **primary_result},
    ]
    # Track phases that persistently timed out (their one-shot in-place retry
    # also timed out). Used below to attach timeout-not-assertion metadata to
    # fix_context for a hung *required* phase, mirroring the primary path.
    phase_timeout_info: List[Dict[str, Any]] = []
    for phase in phases_to_run:
        phase_cmd = shlex.split(phase["command"])
        phase_cwd = _resolve_cwd(project_root, phase.get("cwd"))
        phase_timeout = phase.get("timeout", config.timeout)

        # Phases get the same one-shot in-place timeout retry as the primary
        # command: a timed-out (required) phase is retried once before being
        # treated as a failure, so a transient slowdown does not push the flow
        # into the fix loop.
        result, _phase_retried = _run_command_with_timeout_retry(
            phase_cmd, phase_cwd, phase_timeout, f"test phase '{phase['name']}'",
        )
        phase_results.append({"name": phase["name"], **result})
        if _is_timeout_result(result):
            phase_timeout_info.append({
                "name": phase["name"],
                "timeout": phase_timeout,
                "retried": _phase_retried,
                "required": phase.get("required", True),
            })

    # 4. Compute overall_passed (reflects actual pytest exit status)
    overall_passed = primary_result["passed"]
    for i, phase in enumerate(phases_to_run):
        if phase.get("required", True) and not phase_results[i + 1]["passed"]:
            overall_passed = False

    # 4b. Critical acceptance test gate.
    # pytest returns 0 for a SKIPPED test, and a critical test that has been
    # renamed / un-collected (import error, typo'd pattern) silently vanishes
    # while the run still exits 0. Either case would otherwise let
    # tests_passed (and downstream `verified`) go false-green. A skipped or
    # missing critical test is treated as "not verified" → overall_passed False.
    critical_skipped: List[str] = []
    critical_missing: List[str] = []
    if config.critical_tests:
        ran_ids = [tid for tid, _passed in _parse_test_ids(primary_result["stdout"])]
        skipped_ids = _parse_skipped_test_ids(primary_result["stdout"])
        if not ran_ids and not skipped_ids:
            logger.warning(
                "critical_tests is configured but no per-test results could be "
                "parsed from the test output; skipping critical-missing "
                "detection to avoid false positives. Ensure the test command "
                "emits verbose per-test output (e.g. `python -m pytest -v`)."
            )
        critical_skipped, critical_missing = _detect_critical_failures(
            ran_ids, skipped_ids, list(config.critical_tests),
        )
        if critical_skipped or critical_missing:
            overall_passed = False
            logger.warning(
                "Critical acceptance test gate failed (skip != pass): "
                "skipped=%s missing=%s",
                critical_skipped, critical_missing,
            )
    critical_failed = bool(critical_skipped) or bool(critical_missing)

    # 5. Distinguish inherited (pre-implement baseline) failures from
    #    introduced ones.
    #
    # ``baseline_failures`` is the frozen set of test IDs that were already
    # failing *before* this flow's implement step ran (captured at flow start
    # by the state machine and injected into this step's inputs). A failing
    # test is INHERITED iff its id is in the baseline; otherwise it is
    # INTRODUCED and must be fixed.
    #
    # This replaces the old auto-accumulated ``known_test_failures.json``
    # exemption, which was a laundering vector: it appended every regression
    # failure to the store, so a genuinely new failure became "known" after a
    # single occurrence and was then forgiven forever. The baseline is frozen
    # before implement runs, so an introduced regression can never launder
    # itself into it.
    baseline_failures_set = set(baseline_failures or [])

    all_failed = list(new_tests["failed"]) + list(regression["failed"])
    introduced_failures = [
        tid for tid in all_failed if tid not in baseline_failures_set
    ]
    inherited_failures = [
        tid for tid in all_failed if tid in baseline_failures_set
    ]

    # Introduced regressions: regression failures NOT in the baseline. New-test
    # failures are handled separately below (they are always introduced — a
    # newly added test file does not exist at baseline capture time).
    introduced_regression = [
        tid for tid in regression["failed"] if tid not in baseline_failures_set
    ]

    # Detect timeout in the primary test result via the shared timeout-class
    # classifier: the structured ``timed_out`` flag, the ``Timeout after``
    # marker, OR the ``returncode == -1`` timeout sentinel. The generic
    # exception-fallback path uses a distinct ``-2`` sentinel, so it is never
    # misclassified as a timeout here.
    primary_timed_out = _is_timeout_result(primary_result)

    # The "introduced or critical" group is the always-blocking category: a new
    # test failure, an introduced regression, an unparseable failure, or a
    # skipped/missing critical acceptance test. These keep the normal fix-loop
    # guardrails (no scope relaxation).
    unparseable_failure = (
        not overall_passed
        and not regression["failed"]
        and not new_tests["failed"]
        and not critical_failed
    )
    introduced_or_critical = (
        bool(new_tests["failed"])
        or bool(introduced_regression)
        or unparseable_failure
        or critical_failed
    )

    # ------------------------------------------------------------------
    # Mechanism B: bounded looping of inherited (baseline) failures.
    #
    # Historically inherited failures were surfaced but NEVER looped, so a
    # repo's pre-existing red tests stayed red forever (the "baseline zombie"
    # gap). Mechanism B lets the fix loop ALSO attempt inherited baseline
    # failures, but only:
    #   - excluding ids already *given up* on in a previous flow (cross-flow
    #     persistent memory — avoids every flow re-attempting an unfixable
    #     failure such as a missing system library / flaky test / human call);
    #   - within an independently bounded per-flow budget
    #     (``workflow.baseline_fix_max_attempts``, default 3, NOT shared with
    #     the possibly-unlimited global ``max_fix_iterations``);
    #   - ``0`` disables baseline looping entirely (pure surface, as before).
    # When the budget is exhausted the active baseline failures are recorded as
    # given-up (so future flows skip them) and surfaced without looping.
    # ------------------------------------------------------------------
    active_baseline: List[str] = []
    baseline_budget = 0
    baseline_attempts_so_far = 0
    baseline_should_loop = False
    baseline_budget_exhausted = False
    # Only consult the given-up memory and the workflow budget when there are
    # inherited failures to consider. This keeps the common (no-inherited) path
    # free of the WorkflowConfig YAML/git resolution, which is both unnecessary
    # work and a subprocess probe that callers stubbing subprocess would trip on.
    if inherited_failures:
        given_up = load_given_up(project_root)
        active_baseline = [t for t in inherited_failures if t not in given_up]
        if active_baseline:
            from ...config import WorkflowConfig
            baseline_budget = WorkflowConfig.load(project_root).baseline_fix_max_attempts
            baseline_attempts_so_far = flow.state.context.get("baseline_fix_attempts", 0)
            if not isinstance(baseline_attempts_so_far, int) or baseline_attempts_so_far < 0:
                baseline_attempts_so_far = 0

            budget_enabled = baseline_budget > 0
            baseline_should_loop = (
                budget_enabled
                and baseline_attempts_so_far < baseline_budget
            )
            # Budget genuinely exhausted (we DID enable looping and burned the
            # budget) — distinct from "disabled" (budget == 0), which never
            # attempted anything.
            baseline_budget_exhausted = (
                budget_enabled
                and baseline_attempts_so_far >= baseline_budget
            )

    # Fix loop triggers on the introduced/critical group OR an in-budget
    # baseline failure (mechanism B). ``should_fix`` is the authoritative
    # ``tests_blocking`` verdict consumed by verify_spec.
    should_fix = introduced_or_critical or baseline_should_loop

    # 6. Build structured test_results dict (written to step.outputs by caller)
    test_results: Dict[str, Any] = {
        "new_tests": new_tests,
        "regression": regression,
        "phases": phase_results,
        "overall_passed": overall_passed,
        # Critical acceptance test gate signals (consumed defensively by
        # verify_spec to force tests_passed=False even if overall_passed were
        # mis-set upstream). Empty lists when no critical_tests are configured.
        "critical_skipped": critical_skipped,
        "critical_missing": critical_missing,
        # Baseline-based provenance split. ``introduced_failures`` are failures
        # NOT in the frozen pre-implement baseline (this session's fault, must
        # be fixed); ``inherited_failures`` were already failing at flow start.
        # verify_spec consumes these as the single source of truth so its
        # fix-loop test gate blocks on exactly the same condition as test.py.
        "introduced_failures": introduced_failures,
        "inherited_failures": inherited_failures,
        # Mechanism B: the subset of inherited failures still eligible to loop
        # (inherited − given_up). Surfaced for transparency / downstream use.
        "active_baseline": list(active_baseline),
        # Authoritative test-gate verdict: True when the test step demands a
        # fix (introduced failures / unparseable / critical gate / in-budget
        # baseline). verify_spec reads this directly so the two steps can never
        # disagree on whether tests block the flow.
        "tests_blocking": should_fix,
        # Backward compat fields
        "command": " ".join(primary_command),
        "returncode": primary_result["returncode"],
        "stdout": primary_result["stdout"],
        "stderr": primary_result["stderr"],
        "passed": primary_result["passed"],
    }

    # 7. Handle inherited (baseline) failures: leave a trace (留痕) every time,
    #    regardless of whether they are also being looped this round.
    inherited_list: List[Dict[str, str]] = []
    if inherited_failures:
        for tid in inherited_failures:
            reason = _extract_failure_reason(primary_result["stdout"], tid)
            inherited_list.append({"test_id": tid, "reason": reason})
        logger.warning(
            f"{len(inherited_failures)} inherited test failure(s) detected "
            f"(present in the pre-implement baseline, NOT introduced by this "
            f"flow): {', '.join(inherited_failures)}"
        )

    # Mechanism B budget exhaustion: when we enabled baseline looping but burned
    # the per-flow budget without fixing the active baseline failures, give up
    # on them persistently so future flows do not re-attempt the same un-fixable
    # failures, and surface them (do NOT loop).
    if baseline_budget_exhausted and not baseline_should_loop:
        record_given_up(
            project_root,
            active_baseline,
            attempts=baseline_attempts_so_far,
            reason="exhausted",
        )
        logger.warning(
            "Baseline-fix budget exhausted (%d attempt(s) >= cap %d) for %d "
            "active baseline failure(s); recorded as given-up and surfaced "
            "(not looped): %s",
            baseline_attempts_so_far, baseline_budget, len(active_baseline),
            ", ".join(active_baseline),
        )

    if overall_passed:
        logger.info("All tests passed")
    elif not should_fix:
        logger.info(
            "Tests failed but no failure triggers the fix loop (all failures "
            "are inherited and either given-up or out of budget) — surfacing, "
            "not looping"
        )
    elif baseline_should_loop and not introduced_or_critical:
        logger.info(
            "Only inherited (baseline) failures remain; looping within the "
            "baseline-fix budget (%d/%d): %s",
            baseline_attempts_so_far, baseline_budget,
            ", ".join(active_baseline),
        )
    else:
        logger.warning("Tests failed with new/introduced-regression failures")

    # 8. Build fix instructions / fix context (only when looping).
    fix_instructions = ""
    fix_context: Dict[str, Any] = {}
    if should_fix:
        stdout = primary_result.get("stdout", "")
        stderr = primary_result.get("stderr", "")

        # Build fix instructions from test output
        failures_section = _extract_failures_section(stdout)
        stderr_tail = stderr[-FIX_STDERR_TAIL_CHARS:] if stderr else ""
        fix_instructions = f"""Tests are failing. Please review and fix the implementation.

Test output:
{failures_section}

Error output:
{stderr_tail}
"""

        # Prepend targeted guidance when a critical acceptance test was
        # skipped or is missing — these never produce a FAILURES section, so
        # without this the implement step would see only generic test output.
        if critical_failed:
            critical_parts: list[str] = []
            if critical_skipped:
                critical_parts.append(
                    "The following CRITICAL acceptance test(s) were SKIPPED and "
                    "therefore did NOT actually run:\n  - "
                    + "\n  - ".join(critical_skipped)
                )
            if critical_missing:
                critical_parts.append(
                    "The following CRITICAL acceptance test pattern(s) matched "
                    "neither a test that ran nor a test that was skipped — they "
                    "appear to be MISSING (renamed, un-collected due to an import "
                    "error, or a typo in the configured pattern):\n  - "
                    + "\n  - ".join(critical_missing)
                )
            fix_instructions = (
                "CRITICAL ACCEPTANCE TESTS NOT VERIFIED (skip/missing == "
                "verification failure)\n\n"
                + "\n\n".join(critical_parts)
                + "\n\nThese critical acceptance tests MUST actually run and "
                "pass; a skip or a missing test is treated as a verification "
                "failure (skip != pass). Do NOT silence them with skip guards. "
                "Instead: install any required dependencies so the test can run, "
                "remove the skip guard, and fix test collection "
                "(imports/renames) so each critical test is collected and "
                "executed. If the feature under test is genuinely broken, fix "
                "the implementation so the assertions pass.\n\n"
                + fix_instructions
            )

        # Mechanism B: prepend a dedicated baseline section listing the active
        # baseline failures the fix loop is also expected to repair this round.
        # Prepended even when introduced/critical failures also triggered the
        # loop (the baseline failures are handled in PARALLEL, not preempting).
        if baseline_should_loop:
            fix_instructions = (
                _build_baseline_fix_section(active_baseline) + fix_instructions
            )

        # Store fix context in outputs for the fix loop. The reason prefers the
        # introduced/critical category; a pure-baseline loop is "baseline_failure".
        if critical_failed:
            reason = "critical_acceptance_not_verified"
        elif introduced_or_critical:
            reason = "test_failure"
        else:
            reason = "baseline_failure"
        fix_context = {
            "test_failed": True,
            "test_results": test_results,
            "reason": reason,
            "iteration": (fix_iteration or 0) + 1,
            "critical_skipped": critical_skipped,
            "critical_missing": critical_missing,
        }
        # Mechanism B: record exactly which baseline failures the relaxation
        # applies to. The unlock semantics apply ONLY to these annotated ids.
        if baseline_should_loop:
            fix_context["baseline_failures_targeted"] = list(active_baseline)

        # If primary test timed out, include timeout info so implement can
        # provide a higher estimated_test_duration next iteration.
        # Report the *sanitized* estimate (i.e. what was actually used to
        # compute primary_timeout) rather than the raw input, so the LLM's
        # reasoning input matches the actual behavior when the state machine
        # forwards a malformed value that falls through to config.timeout.
        if primary_timed_out:
            reported_estimate = (
                _render_estimate(sanitized_estimated_duration)
                if sanitized_estimated_duration is not None
                else "not set"
            )
            cap_note = (
                " The computed timeout was clamped at the max_dynamic_timeout "
                f"cap ({config.max_dynamic_timeout}s), so raising the estimate "
                "further will not produce a larger timeout — investigate "
                "splitting the suite or fixing a slow/hung test instead."
                if primary_timeout_at_cap
                else ""
            )
            retry_note = (
                " An automatic in-place retry of the same command also timed "
                "out, so this is a persistent timeout rather than a one-off "
                "slowdown."
                if primary_retried_after_timeout
                else ""
            )
            fix_context["timeout_reason"] = (
                f"Tests timed out after {primary_timeout}s. This was a TIMEOUT, "
                f"NOT an assertion / test-logic failure — the test suite did not "
                f"finish within the time budget.{retry_note} "
                f"Previous estimated_test_duration was {reported_estimate}. "
                f"The timeout_multiplier is {config.timeout_multiplier}. "
                f"Please provide a significantly higher estimated_test_duration to avoid repeated timeouts."
                f"{cap_note}"
            )
            fix_context["previous_timeout"] = primary_timeout
            fix_context["previous_estimated_test_duration"] = sanitized_estimated_duration
            fix_context["timeout_multiplier"] = config.timeout_multiplier
            fix_context["timeout_at_cap"] = primary_timeout_at_cap
            # Explicit machine-readable flags so the implement step (and any
            # other consumer) can distinguish a timeout from an assertion
            # failure without parsing the human-readable reason text.
            fix_context["timed_out_not_assertion"] = True
            fix_context["timeout_retried"] = primary_retried_after_timeout
            logger.warning(
                "Test timed out (timeout=%ds, estimated=%s, at_cap=%s). Including timeout info in fix_context.",
                primary_timeout, sanitized_estimated_duration, primary_timeout_at_cap,
            )

        # Phase-level timeout handling. The dynamic timeout applies only to the
        # primary command, but a hung phase should still be flagged to the LLM
        # so it can diagnose the hang (even though the fix is not to raise
        # estimated_test_duration).
        if phase_timeout_info:
            phase_list = ", ".join(p["name"] for p in phase_timeout_info)
            fix_instructions += (
                f"\nNote: the following test phase(s) timed out: {phase_list}. "
                "Dynamic timeout applies only to the primary test command; "
                "investigate whether these phases are hanging or need their "
                "phase-level `timeout` raised in tianluo.yaml.\n"
            )

            # When a *required* phase persistently timed out and the run is NOT
            # also failing on a genuine assertion failure (no new-test failure,
            # no regression failure, no critical-gate failure) and the primary
            # command did not itself time out, the failure is purely a TIMEOUT.
            # Surface the same machine-readable timeout-not-assertion signal the
            # primary path emits, so the implement step does not receive generic
            # test_failure context for a hung phase. The dynamic-estimate fields
            # are deliberately NOT set: phases use their own fixed `timeout`, not
            # implement's estimated_test_duration.
            required_phase_timeouts = [
                p for p in phase_timeout_info if p["required"]
            ]
            no_assertion_failures = (
                not new_tests["failed"]
                and not regression["failed"]
                and not critical_failed
            )
            if (
                required_phase_timeouts
                and not primary_timed_out
                and no_assertion_failures
            ):
                any_retried = any(p["retried"] for p in required_phase_timeouts)
                names = ", ".join(p["name"] for p in required_phase_timeouts)
                retried_note = (
                    " An automatic in-place retry of the phase also timed out, "
                    "so this is a persistent timeout rather than a one-off "
                    "slowdown."
                    if any_retried else ""
                )
                fix_context["timed_out_not_assertion"] = True
                fix_context["timeout_retried"] = any_retried
                fix_context.setdefault(
                    "timeout_reason",
                    f"Required test phase(s) timed out: {names}. This was a "
                    f"TIMEOUT, NOT an assertion / test-logic failure — the "
                    f"phase did not finish within its configured time budget."
                    f"{retried_note} Dynamic timeout applies only to the primary "
                    f"test command, so raising estimated_test_duration will not "
                    f"help; raise the phase-level `timeout` in tianluo.yaml or "
                    f"investigate whether the phase is hanging.",
                )

    # 9. Archive slimming of the STORED test_results copy.
    #
    # The full ``pytest -v`` stdout is carried on ``step.outputs["test_results"]``
    # and written verbatim to the per-step history jsonl by HistorySink — the
    # real bloat source. For a PASSED phase that stdout is pure noise (every line
    # is a ``... PASSED`` line), so we replace the stored copy with a compact
    # pass/fail count summary + a bounded tail (keeping the final
    # ``=== N passed in Ts ===`` line). This runs AFTER every live use: the
    # classification (_classify_results), the critical-acceptance gate, and the
    # fix-instruction extraction all read the LOCAL ``primary_result`` (and
    # fix_instructions was built from it above), never this stored dict, so the
    # live verdict is fully decoupled from the slimmed archive copy. A FAILED
    # phase keeps its full stdout so the failure stays diagnosable, and the
    # independent ``critical_skipped`` / ``critical_missing`` fields are
    # untouched.
    _slim_passed_phase_archive(test_results)

    return TestVerdict(
        test_results=test_results,
        overall_passed=overall_passed,
        should_fix=should_fix,
        fix_instructions=fix_instructions,
        fix_context=fix_context,
        inherited_list=inherited_list,
    )


def _build_baseline_fix_section(active_baseline: List[str]) -> str:
    """Build the prepended fix-instruction section for mechanism B.

    Lists the active baseline (inherited) failures the fix loop is also expected
    to repair, with a tightly-scoped relaxation of the user-prompt focus limits
    that applies ONLY to these annotated ids — never to introduced failures and
    never past the luo guardrails' SHALL/MUST contracts.
    """
    bullet_ids = "\n".join(f"  - {tid}" for tid in active_baseline)
    return (
        "BASELINE (PRE-EXISTING) TEST FAILURES — MUST ALSO BE FIXED\n\n"
        "The following test(s) were already failing before this flow began — "
        "they are baseline / inherited failures, NOT introduced by your change:\n"
        f"{bullet_ids}\n\n"
        "These baseline failures MUST ALSO be fixed and are to be treated with "
        "EQUAL priority to the main task — work on them in PARALLEL; they do NOT "
        "preempt or push aside the primary task. The following scope relaxation "
        "applies ONLY to fixing these specifically-listed baseline failures:\n"
        "  - ONLY these listed baseline failures are in scope for the "
        "relaxation; introduced failures and everything else keep their normal "
        "boundaries.\n"
        "  - You MAY step beyond the user-prompt's stated scope / focus limits, "
        "but ONLY as far as needed to fix these listed baseline failures.\n"
        "  - You MUST NOT cross any luo guardrail: do not delete, weaken, or "
        "modify the SHALL / MUST contracts of any spec. The spec guardrails "
        "apply in full.\n"
        "  - Code-first: do NOT revert a legitimate spec/code change merely to "
        "placate a brittle test. When a test is stale relative to a correct "
        "change, the right fix is to UPDATE the test (e.g. 44 → 45), not to undo "
        "the change.\n\n"
    )


def _extract_failure_reason(stdout: str, test_id: str) -> str:
    """Extract a brief failure reason for a test from pytest output.

    Looks for the assertion error message associated with the test_id.
    Returns a short description or a generic message.
    """
    if not stdout or not test_id:
        return "unknown failure"

    # Look for the test in the short test summary (most concise)
    # Format: "FAILED tests/test_foo.py::test_bar - AssertionError: ..."
    pattern = re.escape(test_id) + r"\s*-\s*(.+)"
    m = re.search(pattern, stdout)
    if m:
        return m.group(1).strip()[:200]

    return "test failed (details in pytest output)"


def _report_pre_existing_issues(
    project_root: Path,
    flow: FlowInstance,
    pre_existing_failures: List[Dict[str, str]],
) -> None:
    """Report inherited (pre-implement baseline) test failures via A-class
    issue discovery.

    Creates a medium priority issue with all inherited failures listed. The
    caller is responsible for the flow-level "file at most once" guard; this
    helper just files whatever it is handed.
    """
    try:
        from ..issue_discovery import IssueDiscovery
        from ..issue_manager import IssueManager

        manager = IssueManager(project_root)
        discovery = IssueDiscovery(manager, flow.flow_id)
        discovery.create_from_pre_existing_failures(flow, pre_existing_failures)
    except Exception as e:
        logger.debug(f"Failed to report pre-existing failures as issue: {e}")


def _is_timeout_result(result: dict[str, Any]) -> bool:
    """Return True if a command result represents a timeout-class failure.

    A timeout is fundamentally different from an assertion / test-logic failure:
    the suite never finished within its time budget, so it deserves a one-off
    in-place retry before being treated as a real failure. This recognizes the
    signals documented by the flow-engine *Test Dynamic Timeout* requirement:

    - the structured ``timed_out`` flag set by :func:`_run_command` on the
      timeout path (the authoritative, unambiguous signal);
    - the ``Timeout after <N>s`` stderr marker appended on that same path;
    - a ``returncode == -1``: the timeout path's sentinel exit code. The generic
      subprocess-spawn exception path deliberately uses a *distinct* sentinel
      (``-2``), so a bare ``-1`` (e.g. a legacy or mocked result that has lost
      its ``timed_out`` flag and ``Timeout after`` marker) is still recognized
      as a timeout and retried, while a genuine spawn error is not.

    Recognizing any of these (not just the structured flag) lets a
    timeout-shaped result that is missing ``timed_out=true`` still be retried.

    A PASSING result is never a timeout-class failure, regardless of the
    substring/returncode signals: a green suite that exercises timeout handling
    (or a non-pytest runner passing stderr through) can legitimately emit a
    ``Timeout after`` marker while still passing. The authoritative pass/fail
    verdict therefore short-circuits the heuristics, so a passing primary run,
    a passing configured phase, or a passing in-place retry is never recorded
    as a (persistent) timeout failure.
    """
    if result.get("passed"):
        return False
    if result.get("timed_out"):
        return True
    stderr = result.get("stderr") or ""
    if "Timeout after" in stderr:
        return True
    if result.get("returncode") == -1:
        return True
    return False


def _run_command_with_timeout_retry(
    command: list[str], cwd: Path, timeout: int, label: str,
) -> tuple[dict[str, Any], bool]:
    """Run a command and retry ONCE in place on a timeout-class failure.

    A timeout can be a transient slowdown (machine load, cold caches, a one-off
    hang) rather than a genuine failure, so before treating it as one we re-run
    the exact same command with the same timeout a single time. Because this
    retry happens entirely within one test-step execution, it does NOT increment
    the fix_iteration counter (the state machine bumps that only on a
    REVISION_NEEDED transition back to implement).

    Applies uniformly to the primary command and to every configured phase, so a
    timed-out required phase gets the same one-shot retry the primary command
    does instead of dropping straight into the failure path.

    Returns ``(result, retried)`` where ``retried`` is True iff a timeout was
    detected and the in-place retry was performed.
    """
    result = _run_command(command, cwd, timeout)
    # A passing result is never a timeout-class failure, no matter what its
    # stderr contains. _is_timeout_result keys off substrings like
    # "Timeout after" / returncode==-1, which a green suite that exercises
    # timeout handling (or a non-pytest runner with stderr passthrough) can
    # legitimately emit. Short-circuit on the authoritative pass/fail verdict
    # so we never discard a passing run and re-run it as if it had timed out.
    if result.get("passed"):
        return result, False
    if not _is_timeout_result(result):
        return result, False

    logger.warning(
        "%s timed out after %ds; retrying once in place before treating it as a "
        "failure (this retry does not count as a fix iteration).",
        label, timeout,
    )
    result = _run_command(command, cwd, timeout)
    if _is_timeout_result(result):
        logger.warning(
            "In-place retry of %s also timed out after %ds; treating it as a "
            "persistent timeout failure.",
            label, timeout,
        )
    return result, True


def _run_command(
    command: list[str], cwd: Path, timeout: int,
) -> dict[str, Any]:
    """Run a test command and return result dict.

    Shows dot progress indicator while tests are running to indicate
    the process is still active and not stuck.
    """
    logger.info(f"Running: {' '.join(command)} in {cwd}")
    cmd_str = " ".join(command)

    try:
        # Guard against recursive test invocation: if a test spawns luo run
        # which spawns test_handler which spawns pytest again, the inner
        # pytest would run the same tests forever.  Set a sentinel env var
        # so nested invocations can be detected.
        env = dict(__import__("os").environ)
        if env.get("SE3_TEST_RUNNING"):
            logger.warning("Recursive test invocation detected, skipping")
            return {
                "command": " ".join(command),
                "returncode": 0,
                "stdout": "Skipped: recursive test invocation detected (SE3_TEST_RUNNING set)",
                "stderr": "",
                "passed": True,
                "timed_out": False,
            }
        env["SE3_TEST_RUNNING"] = "1"

        # Start the process with Popen to enable progress indication
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )

        # Show dot progress while waiting, enforce timeout
        print(t("engine.test.running", command=cmd_str), end="", flush=True)
        dots = 0
        max_dots = 60
        elapsed = 0

        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    elapsed += 1
                    print(".", end="", flush=True)
                    dots += 1
                    if dots >= max_dots:
                        print("\n" + t("engine.test.still_running"), flush=True)
                        dots = 0
                    # Enforce overall timeout
                    if elapsed >= timeout:
                        process.kill()
                        stdout, stderr = process.communicate()
                        print(flush=True)
                        logger.error(f"Test timed out after {timeout}s: {cmd_str}")
                        print(t("engine.test.timeout", timeout=timeout), flush=True)
                        return {
                            "command": cmd_str,
                            "returncode": -1,
                            "stdout": stdout or "",
                            "stderr": (stderr or "") + f"\nTimeout after {timeout}s",
                            "passed": False,
                            "timed_out": True,
                        }
                    continue

            # Newline after progress dots
            print(flush=True)

            return {
                "command": cmd_str,
                "returncode": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "passed": process.returncode == 0,
                "timed_out": False,
            }

        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
    except Exception as e:
        logger.exception(f"Test execution failed: {command}")
        print("\n" + t("engine.test.error", error=e), flush=True)
        # Use a DISTINCT sentinel returncode (-2) for the generic
        # subprocess-spawn failure path (e.g. command not found / OSError). The
        # timeout path uses -1, and ``_is_timeout_result`` recognizes -1 as a
        # timeout signal; keeping the exception path on -2 prevents a genuine
        # spawn error from being misclassified as a timeout (and given timeout
        # guidance / retried as if it were one).
        return {
            "command": cmd_str,
            "returncode": -2,
            "stdout": "",
            "stderr": str(e),
            "passed": False,
            "timed_out": False,
        }


def _classify_results(
    stdout: str, tests_added: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Classify test results into new_tests and regression.

    Parses test output to extract individual test IDs, then splits them
    based on whether their file path matches any entry in tests_added.
    Best-effort: if parsing fails, everything goes to regression.

    Args:
        stdout: Test runner stdout
        tests_added: List of new test file paths from implement step

    Returns:
        (new_tests_dict, regression_dict)
    """
    test_ids = _parse_test_ids(stdout)

    new_passed, new_failed = [], []
    reg_passed, reg_failed = [], []

    for tid, passed in test_ids:
        is_new = any(tid.startswith(f) for f in tests_added) if tests_added else False
        if is_new:
            (new_passed if passed else new_failed).append(tid)
        else:
            (reg_passed if passed else reg_failed).append(tid)

    return (
        {"passed": new_passed, "failed": new_failed, "count": len(new_passed) + len(new_failed)},
        {"passed": reg_passed, "failed": reg_failed, "count": len(reg_passed) + len(reg_failed)},
    )


# The two per-test line shapes pytest emits under ``-v``. Both are anchored at
# a line start on purpose: the ``-r`` short-summary block emits status-first
# lines (``PASSED file::test``, ``FAILED file::test - reason``) that are NOT
# per-test results, and the FAILURES block quotes arbitrary source text — an
# unanchored pattern would harvest those and double-count or invent tests.
_PYTEST_PER_TEST_STATUSES = "PASSED|FAILED|SKIPPED"

# Serial pytest: the test id leads the line, status follows.
#   ``tests/test_foo.py::test_bar PASSED  [ 12%]``
# The gap is ``\s+`` (which spans newlines) purely to preserve the long-standing
# behaviour of this pattern; narrowing it would be an unrelated behaviour change.
_PYTEST_SERIAL_PER_TEST_RE = re.compile(
    rf'^(\S+::\S+)\s+({_PYTEST_PER_TEST_STATUSES})\b',
    re.MULTILINE,
)

# pytest-xdist: a worker prefix and the status come *before* the id, so the
# serial pattern above cannot match such a line at all.
#   ``[gw3] [ 42%] PASSED tests/test_foo.py::test_bar``
# The ``[gwN]`` prefix is what makes this pattern safe to run over the whole
# output — it is the one thing that distinguishes a real xdist result line from
# the status-first short-summary lines above. The progress percentage is left
# optional so the match does not hinge on a cosmetic field.
_PYTEST_XDIST_PER_TEST_RE = re.compile(
    rf'^\[gw\d+\]\s+(?:\[\s*\d+%\]\s+)?({_PYTEST_PER_TEST_STATUSES})\s+(\S+::\S+)',
    re.MULTILINE,
)


def _iter_pytest_per_test_results(stdout: str) -> list[tuple[str, str]]:
    """Extract ``(test_id, status)`` for every pytest per-test line, in order.

    Recognises both the serial verbose form and the pytest-xdist verbose form
    so that everything built on per-test results — new-vs-regression
    classification (:func:`_classify_results`) and the critical skipped/missing
    gate (:func:`_detect_critical_failures`) — keeps working identically when a
    project runs its suite in parallel. Without this, an xdist run parses to
    zero per-test results and those checks silently degrade to no-ops while
    still exiting 0.

    Results are ordered by position in the output and de-duplicated on the
    ``(test_id, status)`` pair. De-duplicating on the id alone would be wrong:
    a rerun plugin can legitimately report the same test as FAILED then PASSED,
    and collapsing that to the first (or last) verdict would either hide a
    failure or invent one. Identical pairs are collapsed, which is what makes a
    blob containing both output forms safe to parse.
    """
    if not stdout:
        return []
    matches: list[tuple[int, str, str]] = []
    for m in _PYTEST_SERIAL_PER_TEST_RE.finditer(stdout):
        matches.append((m.start(), m.group(1), m.group(2)))
    for m in _PYTEST_XDIST_PER_TEST_RE.finditer(stdout):
        matches.append((m.start(), m.group(2), m.group(1)))
    matches.sort(key=lambda item: item[0])

    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, str]] = []
    for _pos, tid, status in matches:
        key = (tid, status)
        if key in seen:
            continue
        seen.add(key)
        results.append(key)
    return results


def _parse_test_ids(stdout: str) -> list[tuple[str, bool]]:
    """Parse test IDs and pass/fail status from test runner output.

    Supports pytest (serial and pytest-xdist parallel output), jest, go test,
    cargo test output formats.
    Returns list of (test_id, passed) tuples.
    """
    results = []

    # pytest: "tests/test_foo.py::test_bar PASSED" / "FAILED", or the xdist
    # form "[gw0] [ 42%] PASSED tests/test_foo.py::test_bar".
    for tid, status in _iter_pytest_per_test_results(stdout):
        if status in ("PASSED", "FAILED"):
            results.append((tid, status == "PASSED"))

    if results:
        return results

    # jest: "✓ test name" or "✕ test name" (with file context)
    for m in re.finditer(r'^\s+(✓|✕|√|×)\s+(.+)$', stdout, re.MULTILINE):
        passed = m.group(1) in ("✓", "√")
        results.append((m.group(2).strip(), passed))

    if results:
        return results

    # go test: "--- PASS: TestFoo" or "--- FAIL: TestFoo"
    for m in re.finditer(r'^--- (PASS|FAIL): (\S+)', stdout, re.MULTILINE):
        results.append((m.group(2), m.group(1) == "PASS"))

    return results


def _parse_skipped_test_ids(stdout: str) -> list[str]:
    """Parse SKIPPED test IDs from pytest verbose output.

    Matches the per-test lines emitted by ``pytest -v``, e.g.
    ``tests/test_foo.py::test_bar SKIPPED`` (optionally followed by a reason
    in parentheses/brackets), and their pytest-xdist counterpart
    ``[gw0] [ 42%] SKIPPED tests/test_foo.py::test_bar``. The ``-rs``
    short-summary form (``SKIPPED [1] file:line: reason``) is intentionally NOT
    parsed here: it carries ``file:line`` rather than ``file::test`` and so
    cannot be matched against critical-test patterns by test name.

    Returns the skipped test IDs in order of first appearance, deduplicated.
    """
    seen: set[str] = set()
    skipped: list[str] = []
    for tid, status in _iter_pytest_per_test_results(stdout):
        if status != "SKIPPED":
            continue
        if tid not in seen:
            seen.add(tid)
            skipped.append(tid)
    return skipped


def _critical_pattern_matches(pattern: str, test_id: str) -> bool:
    """Return True if a critical-test pattern matches a test ID.

    Matching is by substring (which also covers exact-ID and prefix forms),
    so a pattern like ``test_render_paradigm_in_headless_browser`` or
    ``tests/test_console_real_daemon_e2e.py::test_render`` both work.
    """
    if not pattern:
        return False
    return pattern in test_id


def _detect_critical_failures(
    ran_ids: list[str],
    skipped_ids: list[str],
    critical_patterns: list[str],
) -> tuple[list[str], list[str]]:
    """Detect critical acceptance tests that were skipped or are missing.

    A pytest run that *skips* a critical acceptance test still exits 0, and a
    critical test that has been renamed / silently un-collected (import error,
    typo'd pattern) simply disappears from the output while the run still
    exits 0. Either case would otherwise yield a false-green ``tests_passed``
    downstream. This helper flags both.

    For each entry in ``critical_patterns`` (matched as a substring of a test
    ID via :func:`_critical_pattern_matches`):

    - if it matches one or more SKIPPED tests, those test IDs are added to
      ``critical_skipped`` (skip != pass for a critical test);
    - else if it matches one or more tests that actually ran (PASSED/FAILED),
      the pattern is considered genuinely exercised and ignored here — a real
      FAILED critical test is surfaced through the normal failure path;
    - else the pattern matched neither a run nor a skip and is added to
      ``critical_missing`` — but ONLY when the run produced parseable per-test
      results (``ran_ids`` or ``skipped_ids`` non-empty). Under a non-verbose
      command nothing is parseable, so flagging every pattern as missing would
      be a false positive; in that case missing-detection is skipped.

    Returns ``(critical_skipped, critical_missing)``; both empty when
    ``critical_patterns`` is empty.
    """
    if not critical_patterns:
        return [], []

    parseable = bool(ran_ids) or bool(skipped_ids)
    critical_skipped: list[str] = []
    critical_missing: list[str] = []
    skipped_seen: set[str] = set()
    missing_seen: set[str] = set()

    for pattern in critical_patterns:
        matched_skipped = [
            tid for tid in skipped_ids if _critical_pattern_matches(pattern, tid)
        ]
        if matched_skipped:
            for tid in matched_skipped:
                if tid not in skipped_seen:
                    skipped_seen.add(tid)
                    critical_skipped.append(tid)
            continue

        if any(_critical_pattern_matches(pattern, tid) for tid in ran_ids):
            # Critical test actually ran (passed or failed) — not our concern.
            continue

        if parseable and pattern not in missing_seen:
            missing_seen.add(pattern)
            critical_missing.append(pattern)

    return critical_skipped, critical_missing


# Only the per-test verbose flags make pytest emit the ``file::test SKIPPED``
# / ``PASSED`` / ``FAILED`` lines that _parse_skipped_test_ids / _parse_test_ids
# can match. The ``-r`` report flags (``-rs``/``-ra``/``-rA``) only produce
# short-summary ``SKIPPED [n] file:line: reason`` lines, which are NOT parseable
# by test name, so they MUST NOT count as sufficient for critical-test gating.
_VERBOSE_PYTEST_FLAGS = ("-v", "-vv", "-vvv", "--verbose")


def _is_pytest_command(command: list[str]) -> bool:
    """Heuristic: does ``command`` invoke pytest?

    Recognises ``pytest ...``, ``.../pytest ...``, and ``python -m pytest ...``.
    """
    if not command:
        return False
    for tok in command:
        if tok == "pytest" or tok.rsplit("/", 1)[-1] == "pytest":
            return True
    for i, tok in enumerate(command):
        if tok == "-m" and i + 1 < len(command) and command[i + 1] == "pytest":
            return True
    return False


def _ensure_verbose_pytest(command: list[str], has_critical: bool) -> list[str]:
    """Ensure a pytest command emits per-test skip information for the gate.

    When ``has_critical`` is set and ``command`` is a pytest invocation that
    lacks a per-test verbose flag (``-v`` / ``-vv`` / ``-vvv`` / ``--verbose``),
    ``-v`` is appended so skipped tests are surfaced as parseable
    ``file::test SKIPPED`` lines. The ``-r`` report flags are deliberately NOT
    accepted as sufficient: they only emit ``SKIPPED [n] file:line: reason``
    short-summary lines that cannot be matched against critical-test patterns by
    name. When the command is not recognisably pytest, a warning is logged
    (critical skip/missing detection may not work) and the command is returned
    unchanged. A no-op when ``has_critical`` is False.
    """
    if not has_critical:
        return command
    if not _is_pytest_command(command):
        logger.warning(
            "critical_tests is configured but the test command %r does not "
            "look like pytest; per-test SKIPPED/PASSED/FAILED lines may not be "
            "parseable, so critical-test skip/missing detection may not work.",
            " ".join(command),
        )
        return command
    if any(flag in command for flag in _VERBOSE_PYTEST_FLAGS):
        return command
    logger.info(
        "critical_tests configured; appending -v to the pytest command to "
        "surface per-test SKIPPED lines for critical-test detection."
    )
    return [*command, "-v"]


# Flag spellings that already pin xdist's worker count / distribution mode. A
# user who wrote either of them has made a deliberate choice, so the switch
# tops the command up rather than overriding it.
_XDIST_NUMPROCESSES_LONG = "--numprocesses"
_XDIST_DIST_LONG = "--dist"

# What pytest prints when it is handed -n / --dist without xdist installed
# (argparse rejects the unknown option). The message alone is NOT the
# diagnosis: any suite whose own output quotes this text (a CLI/argparse test,
# a printed assertion body) would otherwise be misread as a missing plugin, so
# _is_missing_xdist_result additionally requires the run to show no sign of
# having collected anything.
_XDIST_MISSING_STDOUT_RE = re.compile(
    r"unrecognized arguments:[^\n]*"
    r"(?:(?<![\w-])-n(?![\w-])|--numprocesses|--dist)"
)

# pytest's ExitCode.USAGE_ERROR — what it returns when argument parsing fails,
# as it does for an unknown -n. Distinct from the ordinary test-failure codes
# (1 failed, 2 interrupted, 3 internal error), so it separates "pytest refused
# the command line" from "the suite ran and something went wrong".
_PYTEST_USAGE_ERROR_RETURNCODE = 4


def _has_numprocesses_flag(command: list[str]) -> bool:
    """Does ``command`` already pin an xdist worker count?

    Covers every spelling pytest accepts: ``-n 4``, ``-n4``, ``-nauto``,
    ``--numprocesses 4`` and ``--numprocesses=4``.
    """
    for tok in command:
        if tok == _XDIST_NUMPROCESSES_LONG or tok.startswith(
            _XDIST_NUMPROCESSES_LONG + "="
        ):
            return True
        if tok == "-n" or (tok.startswith("-n") and not tok.startswith("--")):
            return True
    return False


def _has_dist_flag(command: list[str]) -> bool:
    """Does ``command`` already pin an xdist distribution mode?"""
    for tok in command:
        if tok == _XDIST_DIST_LONG or tok.startswith(_XDIST_DIST_LONG + "="):
            return True
    return False


def _render_parallel_workers(parallel: Any) -> str:
    """Render a validated ``test.parallel`` value as xdist's ``-n`` argument."""
    return "auto" if isinstance(parallel, str) else str(parallel)


def _apply_parallel(command: list[str], parallel: Any) -> list[str]:
    """Append xdist parallel flags to a pytest PRIMARY test command.

    WHY ``--dist loadgroup`` rides along with ``-n``: it is the only scheduling
    mode under which pytest's ``xdist_group`` marker means anything — same group
    means same worker, hence sequential. Projects use that marker to keep their
    genuinely order-dependent tests (shared git worktree, shared mutable global
    state) out of each other's way, so turning on ``-n`` without it would not
    merely lose a hint, it would break those tests. If the command already picks
    a ``--dist`` mode we leave it alone: the user's explicit choice wins, at the
    cost of that guarantee.

    Only ever applied to the primary command, never to configured phases —
    those are the user's own commands and are executed verbatim. A non-pytest
    command is returned unchanged (the flags are pytest-specific), and so is
    every command when ``parallel`` is unset.
    """
    if not parallel:
        return command
    if not _is_pytest_command(command):
        logger.debug(
            "test.parallel=%r ignored: the test command %r is not pytest, so "
            "the pytest-xdist flags do not apply.",
            parallel, " ".join(command),
        )
        return command

    result = list(command)
    if _has_numprocesses_flag(result):
        logger.debug(
            "test.parallel=%r: the test command already pins a worker count; "
            "leaving it as written.",
            parallel,
        )
    else:
        result += ["-n", _render_parallel_workers(parallel)]
    if _has_dist_flag(result):
        logger.debug(
            "test.parallel=%r: the test command already pins a --dist mode; "
            "xdist_group serial grouping is only guaranteed under loadgroup.",
            parallel,
        )
    else:
        result += ["--dist", "loadgroup"]
    return result


class XdistUnavailableError(Exception):
    """pytest-xdist is missing from the environment that runs the tests.

    WHY it is an exception rather than a failed :class:`TestVerdict`: nothing in
    the code under test can fix a package that is not installed, so this must
    reach the human through a FAILED step (like the e2e missing-extra path) and
    must never be dressed up as a test failure — a fix loop would burn its whole
    iteration budget rewriting innocent code. Falling back to a serial run is
    rejected for the same reason it is elsewhere: it would report success for
    something the user asked for and did not get.

    :attr:`remediation` carries the localized install line and is appended to
    ``str(exc)`` so a bare print still tells the user what to do.
    """

    def __init__(self, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def __str__(self) -> str:
        if self.remediation:
            return f"{self.message}\n{self.remediation}"
        return self.message


def _pytest_module_launcher(command: list[str]) -> list[str] | None:
    """The ``... -m`` prefix of a ``<launcher> -m pytest ...`` command, else None.

    WHY the launcher prefix and not ``sys.executable``: tianluo may well be
    installed in a different environment from the project it drives, so
    "can *I* import xdist" answers the wrong question. Only the ``-m pytest``
    shape names the environment that will actually run the tests; a bare
    ``pytest`` command does not (it resolves through PATH to a console script
    whose interpreter we cannot know without running it), which is why that
    shape is diagnosed after the fact instead.

    WHY the whole prefix rather than the interpreter token alone: the shape is
    routinely wrapped (``uv run python -m pytest``, ``poetry run ...``,
    ``pixi run ...``, ``env <python> -m pytest``). ``command[0]`` names the
    wrapper (probing it asks a question about the wrong program) and the bare
    token before ``-m`` is often a PATH-relative ``python`` that resolves to a
    different interpreter outside the wrapper. Re-running the user's own prefix
    with ``-c`` instead of ``-m pytest`` is the only spelling that reaches the
    same environment pytest will run in — and it doubles as a runnable
    ``<prefix> -m pip install pytest-xdist`` remediation line.
    """
    for i, tok in enumerate(command):
        if tok == "-m" and i + 1 < len(command) and command[i + 1] == "pytest":
            return list(command[:i]) or None
    return None


# A probe that exits non-zero has NOT proved xdist missing: a wrapper launcher
# can fail for its own reasons (no project/lockfile, no TTY, network), and
# treating that as an absence produces a FAILED step — with no fix loop to
# recover through — for a plugin that is in fact installed. Only the
# interpreter's own "no such module" verdict is accepted as conclusive.
_XDIST_IMPORT_ERROR_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError)[^\n]*xdist", re.IGNORECASE,
)


def _preflight_xdist(
    command: list[str], parallel: Any, project_root: Path,
) -> None:
    """Raise :class:`XdistUnavailableError` if the test environment lacks xdist.

    Only decides anything for the ``<launcher> -m pytest`` shape; for any other
    command it returns silently and leaves the diagnosis to
    :func:`_is_missing_xdist_result`. A probe that cannot be run at all (missing
    interpreter, sandbox refusal) or that fails for a reason other than the
    import itself is treated as inconclusive rather than as "missing", so the
    run proceeds and the post-hoc path still catches a real absence.

    WHY the probe runs in ``project_root``: the working directory is part of the
    environment being probed, not incidental context. Wrapper launchers
    (``uv run``, ``poetry run``, ``pixi run``) select their project and virtualenv
    from the cwd, and a relative interpreter path (``.venv/bin/python``) resolves
    against it. tianluo never chdirs — under ``--worktree`` its own cwd is the
    main checkout while the tests run in the worktree — so probing without a cwd
    would interrogate a different environment than :func:`_run_command` uses and
    could raise "install pytest-xdist", with no fix loop to recover through, for
    a plugin that is installed where the tests would actually have run.
    """
    launcher = _pytest_module_launcher(command)
    if not launcher:
        return
    try:
        probe = subprocess.run(
            [*launcher, "-c", "import xdist"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("pytest-xdist pre-flight probe could not run: %s", exc)
        return
    if probe.returncode == 0:
        return
    probe_output = f"{probe.stdout or ''}\n{probe.stderr or ''}"
    if not _XDIST_IMPORT_ERROR_RE.search(probe_output):
        logger.debug(
            "pytest-xdist pre-flight probe (%s) failed for an unrelated reason "
            "(rc=%s); leaving the diagnosis to the run itself: %s",
            shlex.join(launcher), probe.returncode, probe_output.strip(),
        )
        return
    raise _xdist_unavailable_error(parallel, shlex.join(launcher))


def _is_missing_xdist_result(result: dict[str, Any]) -> bool:
    """Does a FAILED run look like "pytest does not know about -n"?

    The post-hoc half of xdist detection, for commands whose interpreter we
    cannot name.

    WHY the message match is not enough on its own: the caller turns a True
    here into a FAILED step that never reaches the fix loop, so a false
    positive silently swallows a genuine regression and answers it with an
    install instruction the user does not need. Test suites legitimately print
    ``unrecognized arguments: -n ...`` — a CLI/argparse test asserting on that
    text, or pytest echoing the source of a failing assertion that contains it.
    So the message is only believed when the run also shows that pytest never
    got as far as running anything: rejecting the command line happens during
    argument parsing, before collection, hence no per-test lines, no aggregate
    summary, and pytest's usage-error exit code. A run that produced real
    results falls through to the normal REVISION_NEEDED / fix-loop path.

    The exit code is only *required* when the result carries one — a caller
    that hands over a partial result dict should not lose the diagnosis over a
    missing key.
    """
    if result.get("passed"):
        return False
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if not _XDIST_MISSING_STDOUT_RE.search(f"{stdout}\n{stderr}"):
        return False
    # Corroboration, over stdout AND stderr: whichever stream the runner used,
    # any parsed test result at all means the suite started, which a rejected
    # command line makes impossible.
    haystack = f"{stdout}\n{stderr}"
    if _parse_test_ids(haystack):
        return False
    if _parse_test_summary_counts(haystack) is not None:
        return False
    returncode = result.get("returncode")
    if returncode is not None and returncode != _PYTEST_USAGE_ERROR_RETURNCODE:
        return False
    return True


def _xdist_unavailable_error(
    parallel: Any, environment: str | None = None,
) -> XdistUnavailableError:
    """Build the one actionable "install pytest-xdist" error both paths raise.

    ``environment`` is the test command's launcher prefix as the user wrote it
    (e.g. ``uv run python``), so the remediation line is a command they can
    paste; the post-hoc path has no such prefix and falls back to plain ``pip``.
    """
    where = environment or t("engine.test.xdist_missing_environment_unknown")
    install_command = (
        f"{environment} -m pip install pytest-xdist"
        if environment
        else "pip install pytest-xdist"
    )
    return XdistUnavailableError(
        t(
            "engine.test.xdist_missing",
            parallel=_render_parallel_workers(parallel),
            environment=where,
        ),
        t("engine.test.xdist_missing_remediation", command=install_command),
    )


def _resolve_cwd(project_root: Path, cwd: str | None) -> Path:
    """Resolve phase cwd to an absolute path."""
    if not cwd:
        return project_root
    p = Path(cwd)
    if p.is_absolute():
        return p
    return project_root / p


def _detect_test_command(project_root: Path) -> list[str]:
    """Auto-detect the appropriate test command for the project.

    Args:
        project_root: Project root directory

    Returns:
        List of command arguments
    """
    if (project_root / "pytest.ini").exists() or (project_root / "pyproject.toml").exists():
        return [sys.executable, "-m", "pytest", "-v"]

    if (project_root / "package.json").exists():
        return ["npm", "test"]

    if (project_root / "Cargo.toml").exists():
        return ["cargo", "test"]

    if (project_root / "go.mod").exists():
        return ["go", "test", "./..."]

    return [sys.executable, "-m", "pytest", "-v"]


def _record_test_history(
    project_root: Path,
    flow: FlowInstance,
    step: Step,
    phase_results: list[dict],
    overall_passed: bool,
) -> None:
    """Record test execution results in the chat history system.

    This ensures test step results are preserved in tianluo/history/
    alongside LLM step histories, enabling continuity across
    worktree isolation and fix loop iterations.

    Args:
        project_root: Project root directory
        flow: Current flow instance
        step: Current step instance
        phase_results: List of phase result dicts
        overall_passed: Whether all tests passed
    """
    try:
        from ..chat_history import record_prompt, record_response
        import json as _json

        fix_iteration = step.inputs.get("fix_iteration", 0)

        # Record a synthetic "prompt" summarizing what was tested
        commands_run = [p.get("command", p.get("name", "?")) for p in phase_results]
        prompt_summary = f"Test execution (fix iteration {fix_iteration}): {', '.join(commands_run)}"
        record_prompt(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            prompt=prompt_summary,
            attempt=fix_iteration,
        )

        # Record test results as synthetic "response"
        result_summary = {
            "overall_passed": overall_passed,
            "phases": [
                {
                    "name": p.get("name", "?"),
                    "passed": p.get("passed", False),
                    "returncode": p.get("returncode", -1),
                }
                for p in phase_results
            ],
        }
        # Include failure output for failed phases (truncated)
        for i, p in enumerate(phase_results):
            if not p.get("passed", False):
                stdout_tail = (p.get("stdout", "") or "")[-TEST_HISTORY_STDOUT_TAIL_CHARS:]
                stderr_tail = (p.get("stderr", "") or "")[-TEST_HISTORY_STDERR_TAIL_CHARS:]
                result_summary["phases"][i]["stdout_tail"] = stdout_tail
                result_summary["phases"][i]["stderr_tail"] = stderr_tail

        record_response(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            raw_ndjson=_json.dumps(result_summary, ensure_ascii=False),
            attempt=fix_iteration,
        )
    except Exception as e:
        logger.debug(f"Failed to record test history: {e}")
