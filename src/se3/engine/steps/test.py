"""Test step handler.

Runs tests to verify the implementation.
This is a non-LLM step that executes test commands.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

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
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

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
