"""Tests for fix loop logic, baseline-driven inherited/introduced split, and
inherited-failure issue reporting.

Covers:
- _extract_failure_reason helper
- IssueDiscovery.create_from_pre_existing_failures (now fed baseline-inherited
  failures)
- Baseline-driven test_handler fix loop trigger logic (inherited vs introduced)
- inherited_failures output + no auto-populate of known_test_failures.json
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.steps.test import _extract_failure_reason
from tianluo.engine.issue_discovery import IssueDiscovery
from tianluo.engine.issue_manager import IssueManager
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project structure."""
    (tmp_path / "tianluo" / "state").mkdir(parents=True)
    (tmp_path / "tianluo" / "issues" / "open").mkdir(parents=True)
    (tmp_path / "tianluo" / "issues" / "closed").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def basic_flow():
    """Create a basic flow instance for testing."""
    return FlowInstance(
        flow_id="test-flow-001",
        task_description="Fix authentication bug",
        status=FlowStatus.RUNNING,
    )


@pytest.fixture
def issue_manager(project_root):
    return IssueManager(project_root)


@pytest.fixture
def discovery(issue_manager):
    return IssueDiscovery(issue_manager, flow_id="test-flow-001")


# ---------------------------------------------------------------------------
# _extract_failure_reason
# ---------------------------------------------------------------------------

class TestExtractFailureReason:
    def test_extracts_from_short_summary(self):
        stdout = (
            "FAILED tests/test_cli.py::test_delete - AssertionError: assert 1 == 0\n"
            "FAILED tests/test_cli.py::test_search - AssertionError: assert 'found' in ''\n"
        )
        reason = _extract_failure_reason(stdout, "tests/test_cli.py::test_delete")
        assert "AssertionError" in reason
        assert "1 == 0" in reason

    def test_returns_generic_when_not_found(self):
        stdout = "all tests passed"
        reason = _extract_failure_reason(stdout, "tests/test_missing.py::test_x")
        assert "test failed" in reason

    def test_empty_inputs(self):
        assert _extract_failure_reason("", "test_id") == "unknown failure"
        assert _extract_failure_reason("output", "") == "unknown failure"
        assert _extract_failure_reason("", "") == "unknown failure"

    def test_truncates_long_reason(self):
        long_msg = "x" * 500
        stdout = f"FAILED tests/test_a.py::test_b - {long_msg}\n"
        reason = _extract_failure_reason(stdout, "tests/test_a.py::test_b")
        assert len(reason) <= 200


# ---------------------------------------------------------------------------
# IssueDiscovery.create_from_pre_existing_failures (now fed inherited failures)
# ---------------------------------------------------------------------------

class TestCreateFromPreExistingFailures:
    def test_creates_medium_priority_issue(self, discovery, basic_flow):
        failures = [
            {"test_id": "tests/test_a.py::test_foo", "reason": "assert 1 == 2"},
        ]
        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert issue is not None
        assert issue.priority == "medium"

    def test_correct_tags(self, discovery, basic_flow):
        failures = [
            {"test_id": "tests/test_a.py::test_foo", "reason": "assert fail"},
        ]
        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert "auto-discovered" in issue.tags
        assert "source:test-pre-existing" in issue.tags

    def test_includes_all_test_names(self, discovery, basic_flow):
        failures = [
            {"test_id": "tests/test_a.py::test_foo", "reason": "assert 1 == 2"},
            {"test_id": "tests/test_b.py::test_bar", "reason": "KeyError: 'x'"},
        ]
        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert issue is not None
        assert "test_foo" in issue.description
        assert "test_bar" in issue.description
        assert "assert 1 == 2" in issue.description
        assert "KeyError" in issue.description

    def test_title_contains_count(self, discovery, basic_flow):
        failures = [
            {"test_id": "t1", "reason": "r1"},
            {"test_id": "t2", "reason": "r2"},
            {"test_id": "t3", "reason": "r3"},
        ]
        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert issue is not None
        assert "3 tests" in issue.title

    def test_singular_count_in_title(self, discovery, basic_flow):
        failures = [{"test_id": "t1", "reason": "r1"}]
        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert issue is not None
        assert "1 test)" in issue.title

    def test_empty_failures_returns_none(self, discovery, basic_flow):
        issue = discovery.create_from_pre_existing_failures(basic_flow, [])
        assert issue is None

    def test_deduplicates(self, discovery, basic_flow):
        failures = [{"test_id": "t1", "reason": "r1"}]
        issue1 = discovery.create_from_pre_existing_failures(basic_flow, failures)
        issue2 = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert issue1 is not None
        assert issue2 is None

    def test_description_mentions_task(self, discovery, basic_flow):
        failures = [{"test_id": "t1", "reason": "r1"}]
        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert "Fix authentication bug" in issue.description


# ---------------------------------------------------------------------------
# test_handler fix loop logic (integration-style with mocks)
# ---------------------------------------------------------------------------

class TestFixLoopLogic:
    """Tests for the baseline-driven test_handler fix loop trigger logic."""

    def _make_step_and_flow(self, project_root, baseline_failures=None):
        """Create a Step and FlowInstance for test_handler."""
        step = Step(step_type=StepType.TEST, status=StepStatus.PENDING)
        step.inputs = {
            "tests_added": [],
            "is_fix_iteration": False,
            "baseline_failures": baseline_failures or [],
        }

        flow = FlowInstance(
            flow_id="test-flow-fix",
            task_description="Fix a bug",
            status=FlowStatus.RUNNING,
        )
        flow.change_path = project_root / "tianluo.yaml"
        return step, flow

    def _mock_run_command(self, passed, stdout="", stderr=""):
        """Create a mock for _run_command."""
        return {
            "command": "pytest -v",
            "returncode": 0 if passed else 1,
            "stdout": stdout,
            "stderr": stderr,
            "passed": passed,
        }

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_inherited_failure_no_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Inherited (baseline) failures should NOT trigger fix loop."""
        # pytest output with only the inherited failure
        stdout = "tests/test_old.py::test_broken FAILED\ntests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(
            project_root, baseline_failures=["tests/test_old.py::test_broken"],
        )
        result = step_handler_call(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["test_results"]["overall_passed"] is False
        assert step.outputs.get("fix_needed") is not True

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_new_test_failure_triggers_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """New test failures should trigger fix loop."""
        stdout = (
            "tests/test_new.py::test_feature FAILED\n"
            "tests/test_ok.py::test_pass PASSED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step.inputs["tests_added"] = ["tests/test_new.py"]
        result = step_handler_call(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["fix_needed"] is True

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_introduced_regression_triggers_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """A regression failure NOT in the baseline should trigger fix loop."""
        stdout = (
            "tests/test_existing.py::test_was_passing FAILED\n"
            "tests/test_ok.py::test_pass PASSED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        # Empty baseline → the regression is introduced.
        step, flow = self._make_step_and_flow(project_root, baseline_failures=[])
        result = step_handler_call(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["fix_needed"] is True

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_mixed_inherited_and_new_triggers_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Mix of inherited and new failures: fix loop should still trigger."""
        stdout = (
            "tests/test_old.py::test_broken FAILED\n"
            "tests/test_new.py::test_feature FAILED\n"
            "tests/test_ok.py::test_pass PASSED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(
            project_root, baseline_failures=["tests/test_old.py::test_broken"],
        )
        step.inputs["tests_added"] = ["tests/test_new.py"]
        result = step_handler_call(step, flow)

        assert result == StepStatus.REVISION_NEEDED

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_all_pass_returns_completed(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """All tests passing should return COMPLETED."""
        stdout = "tests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(True, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        result = step_handler_call(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["test_results"]["overall_passed"] is True

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_overall_passed_reflects_actual_exit_status(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """overall_passed should reflect pytest exit code, not fix logic."""
        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(
            project_root, baseline_failures=["tests/test_old.py::test_broken"],
        )
        result = step_handler_call(step, flow)

        # overall_passed reflects actual exit code (False)
        assert step.outputs["test_results"]["overall_passed"] is False
        # But fix loop is NOT triggered (the failure is inherited)
        assert result == StepStatus.COMPLETED


class TestInheritedFailuresOutput:
    """Tests for inherited failures in step outputs and no auto-populate."""

    def _make_step_and_flow(self, project_root, baseline_failures=None):
        step = Step(step_type=StepType.TEST, status=StepStatus.PENDING)
        step.inputs = {
            "tests_added": [],
            "is_fix_iteration": False,
            "baseline_failures": baseline_failures or [],
        }

        flow = FlowInstance(
            flow_id="test-flow-pre",
            task_description="Fix a bug",
            status=FlowStatus.RUNNING,
        )
        flow.change_path = project_root / "tianluo.yaml"
        return step, flow

    def _mock_run_command(self, passed, stdout="", stderr=""):
        return {
            "command": "pytest -v",
            "returncode": 0 if passed else 1,
            "stdout": stdout,
            "stderr": stderr,
            "passed": passed,
        }

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_inherited_in_outputs(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Inherited failures appear in outputs (both legacy + new keys)."""
        stdout = (
            "FAILED tests/test_old.py::test_broken - assert 1 == 2\n"
            "tests/test_old.py::test_broken FAILED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(
            project_root, baseline_failures=["tests/test_old.py::test_broken"],
        )
        step_handler_call(step, flow)

        for key in ("pre_existing_failures", "inherited_failures"):
            entries = step.outputs[key]
            assert len(entries) == 1
            assert entries[0]["test_id"] == "tests/test_old.py::test_broken"
            assert "1 == 2" in entries[0]["reason"]

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_introduced_regression_not_persisted(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """An introduced regression is NOT auto-populated into a known-list file."""
        stdout = (
            "FAILED tests/test_new_reg.py::test_regressed - AssertionError: oops\n"
            "tests/test_new_reg.py::test_regressed FAILED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root, baseline_failures=[])
        step_handler_call(step, flow)

        kf_path = project_root / "tianluo" / "state" / "known_test_failures.json"
        assert not kf_path.exists()
        # It is classified as introduced, not inherited.
        tr = step.outputs["test_results"]
        assert "tests/test_new_reg.py::test_regressed" in tr["introduced_failures"]
        assert tr["inherited_failures"] == []

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_no_inherited_when_all_pass(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """When all tests pass, inherited_failures should be empty."""
        stdout = "tests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(True, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        assert step.outputs["pre_existing_failures"] == []
        assert step.outputs["inherited_failures"] == []

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_report_called_for_inherited(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """_report_pre_existing_issues is called once when inherited failures exist."""
        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(
            project_root, baseline_failures=["tests/test_old.py::test_broken"],
        )
        step_handler_call(step, flow)

        mock_report.assert_called_once()
        call_args = mock_report.call_args
        assert len(call_args[0][2]) == 1  # 1 inherited failure

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_report_not_called_when_no_inherited(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """_report_pre_existing_issues should NOT be called when no inherited failures."""
        stdout = "tests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(True, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        mock_report.assert_not_called()

    @patch("tianluo.engine.steps.test._report_pre_existing_issues")
    @patch("tianluo.engine.steps.test._record_test_history")
    @patch("tianluo.engine.steps.test._run_command")
    @patch("tianluo.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_inherited_issue_filed_once_across_iterations(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """The inherited-failure issue is filed at most once per flow even across
        repeated test-step invocations (fix iterations)."""
        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        flow = FlowInstance(
            flow_id="test-flow-once",
            task_description="Fix a bug",
            status=FlowStatus.RUNNING,
        )
        flow.change_path = project_root / "tianluo.yaml"

        for _ in range(3):
            step = Step(step_type=StepType.TEST, status=StepStatus.PENDING)
            step.inputs = {
                "tests_added": [],
                "is_fix_iteration": False,
                "baseline_failures": ["tests/test_old.py::test_broken"],
            }
            step_handler_call(step, flow)

        mock_report.assert_called_once()
        assert flow.state.context.get("inherited_failures_filed") is True


# ---------------------------------------------------------------------------
# Helper: call test_handler with proper TestConfig mock
# ---------------------------------------------------------------------------

def step_handler_call(step, flow):
    """Call test_handler with TestConfig mocked to avoid tianluo.yaml lookup.

    The baseline-fix budget (mechanism B) is disabled here so these
    classification tests stay on the surface-not-loop path for *inherited*
    failures (introduced failures still loop). Mechanism B's in-budget looping
    is covered in ``tests/engine/test_baseline_fix_loop.py``.
    """
    from tianluo.engine.steps.test import test_handler

    mock_config = MagicMock()
    mock_config.command = None
    mock_config.timeout = 300
    mock_config.critical_tests = []
    mock_config.get_phases_for_run.return_value = []

    with patch("tianluo.config.TestConfig") as MockTestConfig, \
         patch("tianluo.config.WorkflowConfig") as MockWorkflowConfig:
        MockTestConfig.load.return_value = mock_config
        MockWorkflowConfig.load.return_value = MagicMock(baseline_fix_max_attempts=0)
        return test_handler(step, flow)
