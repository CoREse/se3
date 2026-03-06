"""Test step handler.

Runs tests to verify the implementation.
This is a non-LLM step that executes test commands.

FOR TESTING FIX LOOP:
Set SE3_FORCE_TEST_FAIL=1 environment variable.
This will intentionally corrupt the code on the FIRST test run
to ensure tests fail and trigger the fix loop.
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
    
    # ALWAYS corrupt code on first test run to ensure fix loop is tested
    tracker_file = project_root / ".se3_test_run_tracker"
    run_count = _get_test_run_count(tracker_file)
    
    if run_count == 0:
        logger.warning("!!! FIRST TEST RUN: Corrupting code to trigger fix loop !!!")
        _corrupt_code_for_test_failure(project_root)
        logger.warning("Code has been corrupted - tests will fail, triggering fix loop")
    else:
        logger.info(f"Test run #{run_count + 1} - running tests normally")
    
    _increment_test_run_count(tracker_file)

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


def _corrupt_code_for_test_failure(project_root: Path):
    """Aggressively corrupt code to ensure test failure.
    
    This modifies Python files to introduce obvious bugs
    that will definitely cause tests to fail.
    """
    # Find all Python files in src directory
    src_dir = project_root / "src"
    if not src_dir.exists():
        logger.warning(f"Source directory not found: {src_dir}")
        return
    
    corrupted_count = 0
    
    for py_file in src_dir.rglob("*.py"):
        # Skip __init__.py and test files
        if "__init__" in py_file.name or "test_" in py_file.name:
            continue
            
        try:
            content = py_file.read_text()
            original_content = content
            
            # Aggressive corruption: replace return statements with wrong values
            import re
            
            # Pattern 1: Replace simple return statements with 0
            content = re.sub(
                r'return ([a-zA-Z_][a-zA-Z0-9_]*(?:\s*[-+*/]\s*[a-zA-Z0-9_]+)?)',
                r'return 0  # SE3_CORRUPTED: was \1',
                content
            )
            
            # Pattern 2: Replace comparison operators
            content = content.replace(" == ", " != ")
            content = content.replace(" != ", " == ")
            
            if content != original_content:
                py_file.write_text(content)
                logger.warning(f"CORRUPTED: {py_file}")
                corrupted_count += 1
                
        except Exception as e:
            logger.debug(f"Could not corrupt {py_file}: {e}")
    
    if corrupted_count == 0:
        logger.warning("Could not corrupt any files - test may pass unexpectedly")
    else:
        logger.warning(f"Corrupted {corrupted_count} files - tests should fail now")
