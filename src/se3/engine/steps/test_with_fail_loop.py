"""Test step handler with intentional first-run failure for testing fix loop.

This is a test-only version that simulates test failures on the first run
to verify the fix loop mechanism works correctly.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)

# File to track test run count for fix loop testing
TEST_RUN_TRACKER_FILE = ".test_run_count"


def test_handler_with_fail_loop(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute tests with intentional first-run failure for testing fix loop.
    
    This handler:
    1. On first run: Modifies code to introduce a bug, then runs tests (they fail)
    2. On subsequent runs: Runs tests normally
    
    This ensures the fix loop is triggered on the first test run.
    
    Args:
        step: The current test step
        flow: The flow instance
        
    Returns:
        StepStatus.COMPLETED with test results
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    tracker_file = project_root / TEST_RUN_TRACKER_FILE
    
    # Read current run count
    run_count = _get_run_count(tracker_file)
    logger.info(f"Test run count: {run_count}")
    
    # On first run (count == 0), introduce a bug to ensure test failure
    if run_count == 0:
        logger.warning("FIRST TEST RUN - Introducing intentional bug to test fix loop")
        _introduce_temporary_bug(project_root)
        run_count = _increment_run_count(tracker_file)
    
    try:
        # Run actual tests
        result = _run_pytest(project_root)
        
        # Parse results
        tests_passed = result.returncode == 0
        
        step.outputs["tests_passed"] = tests_passed
        step.outputs["test_results"] = {
            "command": f"{sys.executable} -m pytest -v",
            "returncode": result.returncode,
            "passed": tests_passed,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        step.outputs["result"] = StepStatus.COMPLETED.value
        
        if tests_passed:
            logger.info("All tests passed")
        else:
            logger.warning(f"Tests failed with return code: {result.returncode}")
            # Log a hint about fix loop
            if run_count == 1:
                logger.info("This is the intentional first-run failure - fix loop should be triggered")
        
        return StepStatus.COMPLETED
        
    except Exception as e:
        logger.exception("Test execution failed")
        step.error_message = f"Test execution failed: {str(e)}"
        return StepStatus.FAILED


def _get_run_count(tracker_file: Path) -> int:
    """Get the current test run count."""
    if tracker_file.exists():
        try:
            data = json.loads(tracker_file.read_text())
            return data.get("count", 0)
        except Exception:
            pass
    return 0


def _increment_run_count(tracker_file: Path) -> int:
    """Increment and save the test run count."""
    count = _get_run_count(tracker_file) + 1
    tracker_file.write_text(json.dumps({"count": count}))
    return count


def _introduce_temporary_bug(project_root: Path):
    """Introduce a temporary bug to ensure test failure on first run.
    
    This modifies the calculator.py file to introduce a simple bug
    that will cause tests to fail.
    """
    calc_file = project_root / "src/task_cli/calculator.py"
    if not calc_file.exists():
        logger.warning(f"Calculator file not found at {calc_file}")
        return
    
    original_content = calc_file.read_text()
    
    # Introduce a bug: modify add function to subtract
    buggy_content = original_content.replace(
        "return a + b",
        "return a - b  # BUG: Intentionally wrong for fix loop test"
    )
    
    calc_file.write_text(buggy_content)
    logger.info(f"Introduced temporary bug in {calc_file}")


def _run_pytest(project_root: Path) -> subprocess.CompletedProcess:
    """Run pytest and return result."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-v"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=300,
    )


def reset_test_tracker(project_root: Path):
    """Reset the test run tracker. Call this before starting a new test."""
    tracker_file = project_root / TEST_RUN_TRACKER_FILE
    if tracker_file.exists():
        tracker_file.unlink()
        logger.info("Test run tracker reset")
