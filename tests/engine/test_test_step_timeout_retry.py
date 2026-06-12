"""Tests for the test step's timeout in-place retry (item 2) and the passed-phase
history archive slimming (item 4).

Item 2 — timeout retry:
- A primary-command timeout triggers ONE in-place retry with the same args.
- If the retry passes, the run continues normally (no fix loop, no timeout
  context).
- If the retry also times out, the fix loop is entered and fix_context labels
  the failure as a timeout (not an assertion failure).
- The in-place retry does NOT increment the fix_iteration counter.

Item 4 — archive slimming:
- A PASSED phase's stored stdout/stderr (step.outputs["test_results"]) is
  replaced with a compact count + tail summary, not the full ``pytest -v``
  stdout.
- A FAILED phase keeps its full stdout (behavior unchanged).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from se3.engine.models import FlowInstance, Step, StepStatus, StepType


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/engine/test_test_handler_fix_loop.py)
# ---------------------------------------------------------------------------

_PATCHES = {
    "config": "se3.config.TestConfig",
    "run_cmd": "se3.engine.steps.test._run_command",
    "record": "se3.engine.steps.test._record_test_history",
    "report": "se3.engine.steps.test._report_pre_existing_issues",
}


def _make_flow(tmp_path: Path) -> FlowInstance:
    flow = FlowInstance(task_description="test task")
    flow.change_path = tmp_path / "se3.yaml"
    return flow


def _make_step(
    tests_added: list[str] | None = None,
    is_fix: bool = False,
    baseline_failures: list[str] | None = None,
    fix_iteration: int = 0,
    estimated_test_duration: int | None = None,
) -> Step:
    step = Step(step_type=StepType.TEST)
    step.inputs = {
        "tests_added": tests_added or [],
        "is_fix_iteration": is_fix,
        "baseline_failures": baseline_failures or [],
        "fix_iteration": fix_iteration,
    }
    if estimated_test_duration is not None:
        step.inputs["estimated_test_duration"] = estimated_test_duration
    return step


def _config(**overrides) -> MagicMock:
    base = dict(
        command="python -m pytest -v",
        timeout=1800,
        timeout_multiplier=2.0,
        min_dynamic_timeout=30,
        max_dynamic_timeout=14400,
        critical_tests=[],
        get_phases_for_run=MagicMock(return_value=[]),
    )
    base.update(overrides)
    return MagicMock(**base)


def _primary_result(passed: bool, stdout: str = "", stderr: str = "") -> dict:
    return {
        "command": "python -m pytest -v",
        "returncode": 0 if passed else 1,
        "stdout": stdout,
        "stderr": stderr,
        "passed": passed,
        "timed_out": False,
    }


def _timeout_result(timeout: int) -> dict:
    return {
        "command": "python -m pytest -v",
        "returncode": -1,
        "stdout": "",
        "stderr": f"\nTimeout after {timeout}s",
        "passed": False,
        "timed_out": True,
    }


STDOUT_ALL_PASS = """\
tests/test_a.py::test_one PASSED
tests/test_a.py::test_two PASSED
"""

STDOUT_NEW_FAIL = """\
tests/test_a.py::test_one PASSED
tests/test_new.py::test_fresh FAILED
"""


# ---------------------------------------------------------------------------
# Item 2 — timeout in-place retry
# ---------------------------------------------------------------------------

class TestTimeoutRetry:
    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_retry_success_continues_normally(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """First run times out → one in-place retry → retry passes → COMPLETED.

        No fix loop is entered and no timeout metadata is attached.
        """
        mock_config.load.return_value = _config()
        # First call times out, the retry passes.
        mock_run.side_effect = [
            _timeout_result(200),
            _primary_result(True, STDOUT_ALL_PASS),
        ]

        flow = _make_flow(tmp_path)
        step = _make_step(estimated_test_duration=100)

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is True
        # Exactly one retry (primary + retry), not a loop.
        assert mock_run.call_count == 2
        # No fix context at all on the success path.
        assert step.outputs.get("fix_needed") is not True

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_retry_still_timeout_enters_fix_with_timeout_label(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """First run AND the retry both time out → fix loop, timeout-labeled."""
        mock_config.load.return_value = _config()
        # Both the primary and the retry time out.
        mock_run.return_value = _timeout_result(200)

        flow = _make_flow(tmp_path)
        step = _make_step(estimated_test_duration=100)

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        # primary + exactly one retry.
        assert mock_run.call_count == 2

        fix_context = step.outputs["fix_context"]
        assert fix_context["timed_out_not_assertion"] is True
        assert fix_context["timeout_retried"] is True
        # Human-readable reason still carries the canonical phrasing and the
        # explicit "not an assertion failure" labeling.
        reason = fix_context["timeout_reason"]
        assert "timed out after 200s" in reason
        assert "not an assertion" in reason.lower()
        assert "retry" in reason.lower()
        assert fix_context["previous_timeout"] == 200

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_retry_does_not_increment_fix_iteration(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """The in-place retry must not bump the fix-iteration counter.

        fix_context["iteration"] is ``(input fix_iteration) + 1``; the retry
        does not add another increment, and only one retry is performed.
        """
        mock_config.load.return_value = _config()
        mock_run.return_value = _timeout_result(200)

        flow = _make_flow(tmp_path)
        step = _make_step(estimated_test_duration=100, fix_iteration=2)

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        fix_context = step.outputs["fix_context"]
        # iteration reflects (2 + 1), proving the retry added nothing.
        assert fix_context["iteration"] == 3
        # Only a single retry happened (no runaway loop).
        assert mock_run.call_count == 2

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_non_timeout_failure_does_not_retry(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """An ordinary (assertion) failure must NOT trigger the timeout retry."""
        mock_config.load.return_value = _config()
        mock_run.return_value = _primary_result(False, STDOUT_NEW_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        # Primary only — no retry for a real failure.
        assert mock_run.call_count == 1
        fix_context = step.outputs["fix_context"]
        assert "timed_out_not_assertion" not in fix_context
        assert "timeout_reason" not in fix_context


# ---------------------------------------------------------------------------
# Item 4 — passed-phase archive slimming
# ---------------------------------------------------------------------------

def _big_passing_stdout(n: int = 200) -> str:
    """Build a verbose pytest stdout with many PASSED lines + a summary tail.

    The unique sentinel ``ARCHIVE_HEAD_SENTINEL`` is placed near the very start
    so a test can assert it falls outside the kept tail (proving the full stdout
    was not archived).
    """
    lines = ["ARCHIVE_HEAD_SENTINEL first line of verbose output"]
    for i in range(n):
        lines.append(f"tests/test_big.py::test_case_{i:04d} PASSED")
    lines.append("")
    lines.append("==================== 200 passed in 12.34s ====================")
    return "\n".join(lines)


class TestPassedPhaseArchiveSlimming:
    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_passed_phase_stdout_is_slimmed(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        mock_config.load.return_value = _config()
        big = _big_passing_stdout()
        # sanity: the stdout is large enough that its head falls outside the tail
        assert len(big) > 2000
        mock_run.return_value = _primary_result(True, big, stderr="some warning")

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.COMPLETED
        tr = step.outputs["test_results"]

        # Top-level stored stdout is the compact summary, not the full stdout.
        assert tr["stdout"].startswith("[archived summary — passed phase:")
        assert "200 passed" in tr["stdout"]  # count summary
        assert "0 failed" in tr["stdout"]
        # The full verbose output's head sentinel is gone (only the tail kept).
        assert "ARCHIVE_HEAD_SENTINEL" not in tr["stdout"]
        # The final pytest summary line survives in the tail.
        assert "200 passed in 12.34s" in tr["stdout"]
        # Stored copy is bounded.
        assert len(tr["stdout"]) < len(big)

        # The default phase entry is slimmed identically.
        default_phase = tr["phases"][0]
        assert default_phase["name"] == "default"
        assert default_phase["stdout"].startswith("[archived summary — passed phase:")
        assert "ARCHIVE_HEAD_SENTINEL" not in default_phase["stdout"]

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_failed_phase_stdout_preserved(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """A FAILED phase keeps its full stdout (behavior unchanged)."""
        mock_config.load.return_value = _config()
        failing_stdout = (
            "FAILURE_HEAD_SENTINEL\n"
            "tests/test_a.py::test_one PASSED\n"
            "tests/test_new.py::test_fresh FAILED\n"
            "==================== 1 failed, 1 passed ====================\n"
        )
        mock_run.return_value = _primary_result(False, failing_stdout)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        tr = step.outputs["test_results"]
        # Full stdout preserved verbatim — NOT replaced with a summary.
        assert tr["stdout"] == failing_stdout
        assert "FAILURE_HEAD_SENTINEL" in tr["stdout"]
        assert not tr["stdout"].startswith("[archived summary")
        default_phase = tr["phases"][0]
        assert default_phase["stdout"] == failing_stdout

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_mixed_phases_slims_only_passed(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """With a passing primary + a failing required phase, only the passing
        phase's stored stdout is slimmed; the failing phase keeps its full
        stdout."""
        mock_config.load.return_value = _config(
            get_phases_for_run=MagicMock(return_value=[
                {"name": "e2e", "command": "pytest tests/e2e", "timeout": 300,
                 "required": True},
            ]),
        )
        big = _big_passing_stdout()
        phase_fail_stdout = (
            "PHASE_FAIL_SENTINEL\n"
            "tests/e2e/test_x.py::test_flow FAILED\n"
        )
        mock_run.side_effect = [
            _primary_result(True, big),
            _primary_result(False, phase_fail_stdout),
        ]

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        tr = step.outputs["test_results"]
        # phases[0] = default (passed) → slimmed
        assert tr["phases"][0]["stdout"].startswith("[archived summary")
        assert "ARCHIVE_HEAD_SENTINEL" not in tr["phases"][0]["stdout"]
        # phases[1] = e2e (failed) → preserved
        assert tr["phases"][1]["stdout"] == phase_fail_stdout
        assert "PHASE_FAIL_SENTINEL" in tr["phases"][1]["stdout"]
