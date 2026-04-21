"""Test step handler.

Runs tests to verify the implementation.
This is a non-LLM step that executes test commands.

Supports:
- Auto-detection of project type (Python/Node/Rust/Go)
- Custom test command via se3.yaml test.command
- Multi-phase test execution via se3.yaml test.phases
- Result classification into new_tests vs regression
- Fix-loop aware phase filtering
- Dot progress indicator during test execution
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from ..models import FlowInstance, Step, StepStatus
from ..truncation import FAILURES_SECTION_MAX_CHARS, FIX_STDERR_TAIL_CHARS, TEST_HISTORY_STDERR_TAIL_CHARS, TEST_HISTORY_STDOUT_TAIL_CHARS
from .implement import _sanitize_estimated_test_duration

logger = logging.getLogger(__name__)


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


def _load_known_failures(project_root: Path) -> Dict[str, Any]:
    """Load known test failures from se3/state/known_test_failures.json.

    Returns a dict of {test_id: {reason, first_seen, last_seen}}.
    Returns empty dict if file doesn't exist or is corrupted.
    """
    path = project_root / "se3" / "state" / "known_test_failures.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("known_test_failures.json has unexpected format, ignoring")
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load known_test_failures.json: {e}")
        return {}


def _save_known_failures(project_root: Path, failures: Dict[str, Any]) -> None:
    """Save known test failures to se3/state/known_test_failures.json.

    Uses atomic write (temp file + rename) to avoid corruption.
    Automatically creates se3/state/ directory if needed.
    """
    state_dir = project_root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "known_test_failures.json"

    content = json.dumps(failures, indent=2, ensure_ascii=False)
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(state_dir), suffix=".tmp", prefix="known_failures_",
        )
        closed = False
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            closed = True
            os.replace(tmp_path, str(target))
        except Exception:
            if not closed:
                os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except Exception as e:
        logger.warning(f"Failed to save known_test_failures.json: {e}")


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

    Runs the project's test suite, optionally with additional phases.
    Classifies results into new_tests and regression based on implement
    step's tests_added output.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success,
        StepStatus.REVISION_NEEDED when tests fail (triggers fix loop)
    """
    from ...config import TestConfig

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    config = TestConfig.load(project_root)
    tests_added = step.inputs.get("tests_added", [])
    is_fix_iteration = step.inputs.get("is_fix_iteration", False)

    # Compute dynamic timeout for primary test command.
    # - Minimum floor prevents instantaneous timeouts if the LLM returns a
    #   nonsense estimate (e.g. 0 or 1 second).
    # - Maximum cap prevents runaway compounding in the fix loop: without
    #   it, repeated timeouts cause the LLM to escalate estimated_test_duration
    #   each iteration, which the multiplier then scales further — a hung
    #   test could end up with an hours-long timeout.
    raw_estimated_test_duration = step.inputs.get("estimated_test_duration")
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
    primary_result = _run_command(
        primary_command, project_root, primary_timeout,
    )

    # 2. Classify primary results
    new_tests, regression = _classify_results(
        primary_result["stdout"], tests_added,
    )

    # 3. Run additional phases
    phases_to_run = config.get_phases_for_run(is_fix_iteration)
    phase_results = [
        {"name": "default", **primary_result},
    ]
    for phase in phases_to_run:
        phase_cmd = shlex.split(phase["command"])
        phase_cwd = _resolve_cwd(project_root, phase.get("cwd"))
        phase_timeout = phase.get("timeout", config.timeout)

        result = _run_command(phase_cmd, phase_cwd, phase_timeout)
        phase_results.append({"name": phase["name"], **result})

    # 4. Compute overall_passed (reflects actual pytest exit status)
    overall_passed = primary_result["passed"]
    for i, phase in enumerate(phases_to_run):
        if phase.get("required", True) and not phase_results[i + 1]["passed"]:
            overall_passed = False

    # 5. Distinguish pre-existing failures from net-new regressions
    known_failures = _load_known_failures(project_root)
    known_ids = set(known_failures.keys())

    # Net-new regressions: regression failures NOT in known failures list
    net_new_regression = [
        tid for tid in regression["failed"] if tid not in known_ids
    ]
    # Pre-existing: regression failures that ARE known
    pre_existing_ids = [
        tid for tid in regression["failed"] if tid in known_ids
    ]

    # Detect timeout in primary test result via the structured flag set by
    # _run_command. Previously this matched a stderr substring, which would
    # misclassify the exception-fallback path if its error message ever
    # happened to contain the same text.
    primary_timed_out = bool(primary_result.get("timed_out"))

    # Fix loop triggers on:
    # 1. New test failures
    # 2. Net-new regressions (not in known failures)
    # 3. Unparseable failures (pytest failed but we can't classify individual tests)
    unparseable_failure = (
        not overall_passed
        and not regression["failed"]
        and not new_tests["failed"]
    )
    should_fix = bool(new_tests["failed"]) or bool(net_new_regression) or unparseable_failure

    # 6. Store structured output
    step.outputs["test_results"] = {
        "new_tests": new_tests,
        "regression": regression,
        "phases": phase_results,
        "overall_passed": overall_passed,
        # Backward compat fields
        "command": " ".join(primary_command),
        "returncode": primary_result["returncode"],
        "stdout": primary_result["stdout"],
        "stderr": primary_result["stderr"],
        "passed": primary_result["passed"],
    }
    step.outputs["tests_passed"] = overall_passed

    # 7. Handle pre-existing failures: report and persist
    now_iso = datetime.now().isoformat()
    pre_existing_list: List[Dict[str, str]] = []
    if pre_existing_ids:
        for tid in pre_existing_ids:
            reason = known_failures.get(tid, {}).get("reason", "previously known failure")
            pre_existing_list.append({"test_id": tid, "reason": reason})
        logger.warning(
            f"{len(pre_existing_ids)} pre-existing test failure(s) detected "
            f"(not introduced by this change): {', '.join(pre_existing_ids)}"
        )

    # Also treat regression failures not in known list as potentially new
    # pre-existing entries if overall_passed is False but should_fix is False
    # (i.e., all failures are pre-existing)

    # Update known failures with all current regression failures
    updated_known = dict(known_failures)
    for tid in regression["failed"]:
        if tid in updated_known:
            updated_known[tid]["last_seen"] = now_iso
        else:
            # New failure — extract reason from stdout if possible
            reason = _extract_failure_reason(primary_result["stdout"], tid)
            updated_known[tid] = {
                "reason": reason,
                "first_seen": now_iso,
                "last_seen": now_iso,
            }
            # If this failure is not in net_new_regression, it's actually
            # a first-time failure that we haven't seen before; add to
            # pre_existing for next run but it IS a regression for this run

    _save_known_failures(project_root, updated_known)

    step.outputs["pre_existing_failures"] = pre_existing_list

    # Record test results in history
    _record_test_history(project_root, flow, step, phase_results, overall_passed)

    if overall_passed:
        logger.info("All tests passed")
    elif not should_fix:
        logger.info(
            "Tests failed but all failures are pre-existing — not triggering fix loop"
        )
    else:
        logger.warning("Tests failed with new/regression failures")
        stderr_tail = primary_result["stderr"][-FIX_STDERR_TAIL_CHARS:] if primary_result["stderr"] else ""
        step.error_message = f"Tests failed:\n{stderr_tail}"

    # 8. Report pre-existing failures via A-class issue discovery
    if pre_existing_list:
        _report_pre_existing_issues(project_root, flow, pre_existing_list)

    # 9. Determine return status: fix loop only for new/regression failures
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

        # Store fix context in outputs for the fix loop
        fix_context: dict[str, Any] = {
            "test_failed": True,
            "test_results": step.outputs["test_results"],
            "reason": "test_failure",
            "iteration": step.inputs.get("fix_iteration", 0) + 1,
        }

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
            fix_context["timeout_reason"] = (
                f"Tests timed out after {primary_timeout}s. "
                f"Previous estimated_test_duration was {reported_estimate}. "
                f"The timeout_multiplier is {config.timeout_multiplier}. "
                f"Please provide a significantly higher estimated_test_duration to avoid repeated timeouts."
                f"{cap_note}"
            )
            fix_context["previous_timeout"] = primary_timeout
            fix_context["previous_estimated_test_duration"] = sanitized_estimated_duration
            fix_context["timeout_multiplier"] = config.timeout_multiplier
            fix_context["timeout_at_cap"] = primary_timeout_at_cap
            logger.warning(
                "Test timed out (timeout=%ds, estimated=%s, at_cap=%s). Including timeout info in fix_context.",
                primary_timeout, sanitized_estimated_duration, primary_timeout_at_cap,
            )

        # Phase-level timeout hint: dynamic timeout applies only to the
        # primary command, but a hung required phase should still be flagged
        # to the LLM so it can diagnose the hang (even though the fix is not
        # to raise estimated_test_duration).
        phase_timeouts = [
            pr["name"] for pr in phase_results[1:]
            if pr.get("timed_out")
        ]
        if phase_timeouts:
            phase_list = ", ".join(phase_timeouts)
            fix_instructions += (
                f"\nNote: the following test phase(s) timed out: {phase_list}. "
                "Dynamic timeout applies only to the primary test command; "
                "investigate whether these phases are hanging or need their "
                "phase-level `timeout` raised in se3.yaml.\n"
            )

        step.outputs["fix_needed"] = True
        step.outputs["fix_instructions"] = fix_instructions
        step.outputs["fix_context"] = fix_context

        return StepStatus.REVISION_NEEDED

    return StepStatus.COMPLETED


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
    """Report pre-existing test failures via A-class issue discovery.

    Creates a medium priority issue with all pre-existing failures listed.
    """
    try:
        from ..issue_discovery import IssueDiscovery
        from ..issue_manager import IssueManager

        manager = IssueManager(project_root)
        discovery = IssueDiscovery(manager, flow.flow_id)
        discovery.create_from_pre_existing_failures(flow, pre_existing_failures)
    except Exception as e:
        logger.debug(f"Failed to report pre-existing failures as issue: {e}")


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
        # Guard against recursive test invocation: if a test spawns se3 run
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
        print(f"Running tests: {cmd_str}", end="", flush=True)
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
                        print("\n  ... still running ...", flush=True)
                        dots = 0
                    # Enforce overall timeout
                    if elapsed >= timeout:
                        process.kill()
                        stdout, stderr = process.communicate()
                        print(flush=True)
                        logger.error(f"Test timed out after {timeout}s: {cmd_str}")
                        print(f"[timeout after {timeout}s]", flush=True)
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
        print(f"\n[error: {e}]", flush=True)
        return {
            "command": cmd_str,
            "returncode": -1,
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


def _parse_test_ids(stdout: str) -> list[tuple[str, bool]]:
    """Parse test IDs and pass/fail status from test runner output.

    Supports pytest, jest, go test, cargo test output formats.
    Returns list of (test_id, passed) tuples.
    """
    results = []

    # pytest: "tests/test_foo.py::test_bar PASSED" or "FAILED"
    for m in re.finditer(r'^(\S+::\S+)\s+(PASSED|FAILED)', stdout, re.MULTILINE):
        results.append((m.group(1), m.group(2) == "PASSED"))

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
        return ["python", "-m", "pytest", "-v"]

    if (project_root / "package.json").exists():
        return ["npm", "test"]

    if (project_root / "Cargo.toml").exists():
        return ["cargo", "test"]

    if (project_root / "go.mod").exists():
        return ["go", "test", "./..."]

    return ["python", "-m", "pytest", "-v"]


def _record_test_history(
    project_root: Path,
    flow: FlowInstance,
    step: Step,
    phase_results: list[dict],
    overall_passed: bool,
) -> None:
    """Record test execution results in the chat history system.

    This ensures test step results are preserved in se3/history/
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
