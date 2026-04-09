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

import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def _extract_failures_section(stdout: str, max_chars: int = 3000) -> str:
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

    # 1. Run primary test command
    primary_command = (
        shlex.split(config.command) if config.command
        else _detect_test_command(project_root)
    )
    primary_result = _run_command(
        primary_command, project_root, config.timeout,
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

    # 4. Compute overall_passed (only required phases count)
    overall_passed = primary_result["passed"]
    for i, phase in enumerate(phases_to_run):
        if phase.get("required", True) and not phase_results[i + 1]["passed"]:
            overall_passed = False

    # 5. Store structured output
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

    # Record test results in history
    _record_test_history(project_root, flow, step, phase_results, overall_passed)

    if overall_passed:
        logger.info("All tests passed")
    else:
        logger.warning("Tests failed")
        stderr_tail = primary_result["stderr"][-500:] if primary_result["stderr"] else ""
        step.error_message = f"Tests failed:\n{stderr_tail}"

    # If tests failed, prepare fix loop context and return REVISION_NEEDED
    if not overall_passed:
        stdout = primary_result.get("stdout", "")
        stderr = primary_result.get("stderr", "")

        # Build default fix instructions from test output
        failures_section = _extract_failures_section(stdout)
        stderr_tail = stderr[-500:] if stderr else ""
        fix_instructions = f"""Tests are failing. Please review and fix the implementation.

Test output:
{failures_section}

Error output:
{stderr_tail}
"""

        # Store fix context in outputs for the fix loop
        step.outputs["fix_needed"] = True
        step.outputs["fix_instructions"] = fix_instructions
        step.outputs["fix_context"] = {
            "test_failed": True,
            "test_results": step.outputs["test_results"],
            "reason": "test_failure",
        }

        return StepStatus.REVISION_NEEDED

    return StepStatus.COMPLETED


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
                stdout_tail = (p.get("stdout", "") or "")[-500:]
                stderr_tail = (p.get("stderr", "") or "")[-300:]
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
