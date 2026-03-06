"""Test step handler.

Runs tests to verify the implementation.
This is a non-LLM step that executes test commands.

Special mode for testing fix loop:
Set SE3_TEST_FAIL_LOOP=1 to enable first-run failure mode.
This introduces a temporary bug on the first test run to ensure
the test-verify-fix loop is triggered for testing purposes.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def test_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the test step.

    Runs the project's test suite to verify implementation.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    import os
    
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    
    # Check for fail-loop test mode (for testing fix loop)
    fail_loop_mode = os.environ.get("SE3_TEST_FAIL_LOOP", "").lower() in ("1", "true", "yes")
    if fail_loop_mode:
        tracker_file = project_root / ".se3_test_run_count"
        run_count = _get_test_run_count(tracker_file)
        
        if run_count == 0:
            logger.warning("TEST FAIL-LOOP MODE: Introducing temporary bug on first run")
            _introduce_temporary_bug(project_root)
        
        _increment_test_run_count(tracker_file)
        logger.info(f"Test run count: {run_count + 1}")

    # Determine test command based on project type
    test_command = _determine_test_command(project_root)

    logger.info(f"Running tests with: {' '.join(test_command)}")

    try:
        result = subprocess.run(
            test_command,
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=1800,  # 30 minute timeout
        )

        # Parse test results
        test_results = {
            "command": " ".join(test_command),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "passed": result.returncode == 0,
        }

        # Store outputs
        step.outputs["test_results"] = test_results
        step.outputs["tests_passed"] = result.returncode == 0

        if result.returncode == 0:
            logger.info("All tests passed")
            return StepStatus.COMPLETED
        else:
            logger.warning("Tests failed")
            step.error_message = f"Tests failed:\n{result.stderr[-500:]}"  # Last 500 chars
            # Don't fail the flow - let verify_spec handle the decision
            return StepStatus.COMPLETED

    except subprocess.TimeoutExpired:
        logger.error("Test execution timed out")
        step.error_message = "Tests timed out after 30 minutes"
        step.outputs["test_results"] = {"error": "timeout", "passed": False}
        return StepStatus.COMPLETED  # Continue to verify step

    except Exception as e:
        logger.exception("Test step failed")
        step.error_message = f"Failed to run tests: {str(e)}"
        step.outputs["test_results"] = {"error": str(e), "passed": False}
        return StepStatus.COMPLETED  # Continue to verify step


def _determine_test_command(project_root: Path) -> list[str]:
    """Determine the appropriate test command for the project.

    Args:
        project_root: Project root directory

    Returns:
        List of command arguments
    """
    # Check for pytest
    if (project_root / "pytest.ini").exists() or (project_root / "pyproject.toml").exists():
        return ["python", "-m", "pytest", "-v"]

    # Check for package.json (npm/yarn tests)
    if (project_root / "package.json").exists():
        return ["npm", "test"]

    # Check for Cargo.toml (Rust)
    if (project_root / "Cargo.toml").exists():
        return ["cargo", "test"]

    # Check for go.mod (Go)
    if (project_root / "go.mod").exists():
        return ["go", "test", "./..."]

    # Default: try pytest
    return ["python", "-m", "pytest", "-v"]


def _get_test_run_count(tracker_file: Path) -> int:
    """Get the current test run count for fail-loop mode."""
    if tracker_file.exists():
        try:
            with open(tracker_file) as f:
                data = json.load(f)
            return data.get("count", 0)
        except Exception:
            pass
    return 0


def _increment_test_run_count(tracker_file: Path) -> int:
    """Increment and save the test run count."""
    import json
    count = _get_test_run_count(tracker_file) + 1
    tracker_file.write_text(json.dumps({"count": count}))
    return count


def _introduce_temporary_bug(project_root: Path):
    """Introduce a temporary bug for testing fix loop.
    
    This modifies a common Python file to introduce a simple bug
    that will cause tests to fail on first run.
    """
    # Try to find a suitable Python file to modify
    potential_files = [
        project_root / "src/task_cli/calculator.py",
        project_root / "src/task_cli/math_utils.py",
    ]
    
    target_file = None
    for f in potential_files:
        if f.exists():
            target_file = f
            break
    
    if not target_file:
        logger.warning("No suitable file found to introduce temporary bug")
        return
    
    try:
        content = target_file.read_text()
        
        # Check if we can introduce a simple bug (change + to -)
        if "return a + b" in content and "return a - b" not in content:
            buggy_content = content.replace(
                "return a + b",
                "return a - b  # SE3_TEST_FAIL_LOOP: Intentional bug"
            )
            target_file.write_text(buggy_content)
            logger.info(f"Introduced temporary bug in {target_file}")
        else:
            logger.warning(f"Could not introduce bug in {target_file} - pattern not found")
            
    except Exception as e:
        logger.warning(f"Failed to introduce temporary bug: {e}")
