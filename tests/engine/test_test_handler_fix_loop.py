"""Tests for test_handler fix loop trigger logic.

Verifies:
1. Pre-existing failures do NOT trigger fix loop
2. New test failures DO trigger fix loop
3. Net-new regressions DO trigger fix loop
4. Mixed scenario: pre-existing + new failures triggers fix loop
5. pre_existing_failures written to outputs
6. known_test_failures.json read/write
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus, StepType


def _make_flow(tmp_path: Path) -> FlowInstance:
    """Create a minimal FlowInstance with change_path set."""
    flow = FlowInstance(task_description="test task")
    flow.change_path = tmp_path / "se3.yaml"
    return flow


def _make_step(tests_added: list[str] | None = None, is_fix: bool = False) -> Step:
    """Create a TEST step with given inputs."""
    step = Step(step_type=StepType.TEST)
    step.inputs = {
        "tests_added": tests_added or [],
        "is_fix_iteration": is_fix,
    }
    return step


def _primary_result(passed: bool, stdout: str = "", stderr: str = "") -> dict:
    """Build a _run_command-style result dict."""
    return {
        "command": "python -m pytest -v",
        "returncode": 0 if passed else 1,
        "stdout": stdout,
        "stderr": stderr,
        "passed": passed,
    }


# Typical pytest stdout used by _classify_results
STDOUT_ALL_PASS = """\
tests/test_a.py::test_one PASSED
tests/test_a.py::test_two PASSED
"""

STDOUT_NEW_FAIL = """\
tests/test_a.py::test_one PASSED
tests/test_new.py::test_fresh FAILED
"""

STDOUT_REGRESSION_FAIL = """\
tests/test_a.py::test_one PASSED
tests/test_a.py::test_two FAILED
"""

STDOUT_MIXED = """\
tests/test_a.py::test_one PASSED
tests/test_a.py::test_two FAILED
tests/test_new.py::test_fresh FAILED
"""


# ---------------------------------------------------------------------------
# Patches applied to every test in this module
# ---------------------------------------------------------------------------

_PATCHES = {
    "config": "se3.config.TestConfig",
    "run_cmd": "se3.engine.steps.test._run_command",
    "record": "se3.engine.steps.test._record_test_history",
    "report": "se3.engine.steps.test._report_pre_existing_issues",
}


class TestFixLoopPreExistingOnly:
    """Pre-existing failures should NOT trigger fix loop."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_returns_completed(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        # Setup: test_two is a known failure
        state_dir = tmp_path / "se3" / "state"
        state_dir.mkdir(parents=True)
        known = {"tests/test_a.py::test_two": {"reason": "old bug", "first_seen": "2026-01-01", "last_seen": "2026-01-01"}}
        (state_dir / "known_test_failures.json").write_text(json.dumps(known))

        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_REGRESSION_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs.get("fix_needed") is None or step.outputs.get("fix_needed") is not True
        assert step.outputs["tests_passed"] is False  # overall_passed still reflects actual result

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_writes_pre_existing_to_outputs(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        state_dir = tmp_path / "se3" / "state"
        state_dir.mkdir(parents=True)
        known = {"tests/test_a.py::test_two": {"reason": "old bug", "first_seen": "2026-01-01", "last_seen": "2026-01-01"}}
        (state_dir / "known_test_failures.json").write_text(json.dumps(known))

        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_REGRESSION_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        pre = step.outputs["pre_existing_failures"]
        assert len(pre) == 1
        assert pre[0]["test_id"] == "tests/test_a.py::test_two"

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_reports_pre_existing_via_issue_discovery(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        state_dir = tmp_path / "se3" / "state"
        state_dir.mkdir(parents=True)
        known = {"tests/test_a.py::test_two": {"reason": "old bug", "first_seen": "2026-01-01", "last_seen": "2026-01-01"}}
        (state_dir / "known_test_failures.json").write_text(json.dumps(known))

        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_REGRESSION_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        mock_report.assert_called_once()


class TestFixLoopNewTestFail:
    """New test failures DO trigger fix loop."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_returns_revision_needed(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_NEW_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        assert step.outputs["fix_needed"] is True
        assert "fix_instructions" in step.outputs


class TestFixLoopNetNewRegression:
    """Net-new regressions (not in known failures) trigger fix loop."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_returns_revision_needed(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        # No known_test_failures.json → all regression failures are net-new
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_REGRESSION_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED


class TestFixLoopMixedScenario:
    """Pre-existing + new failures: fix loop triggers (due to new failures)."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_triggers_fix_and_reports_pre_existing(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        # test_two is known, test_fresh is new
        state_dir = tmp_path / "se3" / "state"
        state_dir.mkdir(parents=True)
        known = {"tests/test_a.py::test_two": {"reason": "old bug", "first_seen": "2026-01-01", "last_seen": "2026-01-01"}}
        (state_dir / "known_test_failures.json").write_text(json.dumps(known))

        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_MIXED)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        # Fix loop triggers because test_fresh (new test) failed
        assert status == StepStatus.REVISION_NEEDED

        # Pre-existing still reported
        pre = step.outputs["pre_existing_failures"]
        assert len(pre) == 1
        assert pre[0]["test_id"] == "tests/test_a.py::test_two"
        mock_report.assert_called_once()


class TestFixLoopAllPass:
    """All tests pass — no fix loop."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_returns_completed(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(True, STDOUT_ALL_PASS)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is True
        assert step.outputs["pre_existing_failures"] == []
        mock_report.assert_not_called()


class TestKnownFailuresPersistence:
    """known_test_failures.json is read and written correctly."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_new_regression_saved(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        """A net-new regression failure gets persisted to known failures."""
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_REGRESSION_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        kf_path = tmp_path / "se3" / "state" / "known_test_failures.json"
        assert kf_path.exists()
        data = json.loads(kf_path.read_text())
        assert "tests/test_a.py::test_two" in data

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_existing_failure_last_seen_updated(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        """A known failure's last_seen is updated when seen again."""
        state_dir = tmp_path / "se3" / "state"
        state_dir.mkdir(parents=True)
        known = {
            "tests/test_a.py::test_two": {
                "reason": "old bug",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            },
        }
        (state_dir / "known_test_failures.json").write_text(json.dumps(known))

        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_REGRESSION_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step()

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        data = json.loads((state_dir / "known_test_failures.json").read_text())
        entry = data["tests/test_a.py::test_two"]
        # first_seen preserved, last_seen updated
        assert entry["first_seen"] == "2026-01-01T00:00:00"
        assert entry["last_seen"] != "2026-01-01T00:00:00"


def _timeout_result(timeout: int) -> dict:
    """Build a _run_command-style result dict for a timeout."""
    return {
        "command": "python -m pytest -v",
        "returncode": -1,
        "stdout": "",
        "stderr": f"\nTimeout after {timeout}s",
        "passed": False,
        "timed_out": True,
    }


class TestDynamicTimeout:
    """Dynamic timeout based on estimated_test_duration from implement step."""

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_uses_dynamic_timeout_when_estimated_duration_provided(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.5,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(True, STDOUT_ALL_PASS)

        flow = _make_flow(tmp_path)
        step = _make_step()
        step.inputs["estimated_test_duration"] = 100

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        # Should use 100 * 2.5 = 250s timeout
        call_args = mock_run.call_args
        assert call_args[0][2] == 250  # timeout arg is the 3rd positional arg

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_uses_fallback_timeout_when_no_estimated_duration(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(True, STDOUT_ALL_PASS)

        flow = _make_flow(tmp_path)
        step = _make_step()
        # No estimated_test_duration in inputs

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        # Should use fallback config.timeout
        call_args = mock_run.call_args
        assert call_args[0][2] == 1800

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_timeout_in_fix_context_when_test_times_out(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _timeout_result(200)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])
        step.inputs["estimated_test_duration"] = 100

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        fix_context = step.outputs["fix_context"]
        assert fix_context["timeout_reason"]
        assert "timed out after 200s" in fix_context["timeout_reason"]
        assert fix_context["previous_timeout"] == 200
        assert fix_context["previous_estimated_test_duration"] == 100
        assert fix_context["timeout_multiplier"] == 2.0

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_phases_use_fixed_timeout_not_dynamic(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[
                {"name": "lint", "command": "flake8", "timeout": 300, "required": True},
            ]),
        )
        mock_run.return_value = _primary_result(True, STDOUT_ALL_PASS)

        flow = _make_flow(tmp_path)
        step = _make_step()
        step.inputs["estimated_test_duration"] = 100

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        # Primary call: dynamic timeout
        assert mock_run.call_args_list[0][0][2] == 200  # 100 * 2.0
        # Phase call: should use phase's explicit timeout
        assert mock_run.call_args_list[1][0][2] == 300

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_no_timeout_context_when_not_timeout(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _primary_result(False, STDOUT_NEW_FAIL)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])
        step.inputs["estimated_test_duration"] = 100

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        fix_context = step.outputs["fix_context"]
        assert "timeout_reason" not in fix_context
        assert "previous_timeout" not in fix_context

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_timeout_at_cap_flagged_in_fix_context(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        """When computed timeout exceeds max_dynamic_timeout, timeout_at_cap is set."""
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        # 10000 * 2.0 = 20000 > 14400 cap → clamped
        mock_run.return_value = _timeout_result(14400)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])
        step.inputs["estimated_test_duration"] = 10000

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        fix_context = step.outputs["fix_context"]
        assert fix_context["timeout_at_cap"] is True
        assert "max_dynamic_timeout" in fix_context["timeout_reason"] or "cap" in fix_context["timeout_reason"]

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_timeout_at_cap_false_when_below_cap(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        """When computed timeout is below cap, timeout_at_cap is False."""
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _timeout_result(200)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])
        step.inputs["estimated_test_duration"] = 100

        from se3.engine.steps.test import test_handler
        test_handler(step, flow)

        fix_context = step.outputs["fix_context"]
        assert fix_context["timeout_at_cap"] is False

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_timeout_fix_context_flows_to_rendered_prompt(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        """End-to-end: test_handler produces fix_context that, when rendered
        by _format_fix_context_structured, exposes all four timeout fields.

        This guards against regressions in the producer→formatter contract:
        if a key is dropped in either place (e.g., by a dict filter or a
        deepcopy losing a field), this test fails even when the individual
        unit tests still pass.
        """
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = _timeout_result(200)

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])
        step.inputs["estimated_test_duration"] = 100

        from se3.engine.steps.test import test_handler
        from se3.engine.steps.implement import _format_fix_context_structured

        test_handler(step, flow)

        fix_context = step.outputs["fix_context"]
        rendered = _format_fix_context_structured(fix_context)

        # All four timeout fields must appear in the rendered text.
        assert "Timeout reason:" in rendered
        assert "timed out after 200s" in rendered
        assert "Previous timeout: 200s" in rendered
        # Whole-valued float (100.0) must render as '100', not '100.0'
        assert "Previous estimated_test_duration: 100" in rendered
        assert "Previous estimated_test_duration: 100.0" not in rendered
        assert "Timeout multiplier: 2.0" in rendered

    @patch(_PATCHES["report"])
    @patch(_PATCHES["record"])
    @patch(_PATCHES["run_cmd"])
    @patch(_PATCHES["config"])
    def test_phase_timeout_hint_in_fix_instructions(self, mock_config, mock_run, mock_record, mock_report, tmp_path):
        """When a required phase times out, the hint is added to fix_instructions.

        Dynamic timeout applies only to the primary command, but a hung
        required phase should still surface in fix_instructions so the LLM
        can diagnose the hang.
        """
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=1800,
            timeout_multiplier=2.0,
            min_dynamic_timeout=30, max_dynamic_timeout=14400,
            get_phases_for_run=MagicMock(return_value=[
                {"name": "lint", "command": "flake8", "timeout": 30, "required": True},
            ]),
        )

        # Primary: a new test failure (so fix loop triggers). Phase: timeout.
        mock_run.side_effect = [
            _primary_result(False, STDOUT_NEW_FAIL),
            _timeout_result(30),
        ]

        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"])

        from se3.engine.steps.test import test_handler
        status = test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        fix_instructions = step.outputs["fix_instructions"]
        assert "lint" in fix_instructions
        assert "timed out" in fix_instructions.lower()
