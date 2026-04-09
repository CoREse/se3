"""Tests for fix loop refactoring, known failures persistence, and pre-existing issue reporting.

Covers:
- _load_known_failures / _save_known_failures (Task 7)
- Refactored overall_passed logic distinguishing pre-existing vs new failures (Task 6)
- Pre-existing failures output and persistence (Task 8)
- IssueDiscovery.create_from_pre_existing_failures (Task 9)
- _extract_failure_reason helper
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.steps.test import (
    _extract_failure_reason,
    _load_known_failures,
    _save_known_failures,
)
from se3.engine.issue_discovery import IssueDiscovery
from se3.engine.issue_manager import IssueManager
from se3.engine.models import (
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
    (tmp_path / "se3" / "state").mkdir(parents=True)
    (tmp_path / "se3" / "issues" / "open").mkdir(parents=True)
    (tmp_path / "se3" / "issues" / "closed").mkdir(parents=True)
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
# _load_known_failures
# ---------------------------------------------------------------------------

class TestLoadKnownFailures:
    def test_file_not_exists_returns_empty(self, tmp_path):
        result = _load_known_failures(tmp_path)
        assert result == {}

    def test_valid_json_returns_dict(self, project_root):
        data = {
            "tests/test_a.py::test_foo": {
                "reason": "assert 1 == 2",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-02T00:00:00",
            }
        }
        path = project_root / "se3" / "state" / "known_test_failures.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        result = _load_known_failures(project_root)
        assert result == data

    def test_invalid_json_returns_empty(self, project_root):
        path = project_root / "se3" / "state" / "known_test_failures.json"
        path.write_text("{invalid json", encoding="utf-8")

        result = _load_known_failures(project_root)
        assert result == {}

    def test_non_dict_json_returns_empty(self, project_root):
        path = project_root / "se3" / "state" / "known_test_failures.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")

        result = _load_known_failures(project_root)
        assert result == {}


# ---------------------------------------------------------------------------
# _save_known_failures
# ---------------------------------------------------------------------------

class TestSaveKnownFailures:
    def test_creates_file(self, project_root):
        data = {
            "tests/test_a.py::test_foo": {
                "reason": "assert fail",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, data)

        path = project_root / "se3" / "state" / "known_test_failures.json"
        assert path.exists()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == data

    def test_creates_state_directory(self, tmp_path):
        """Should auto-create se3/state/ if it doesn't exist."""
        _save_known_failures(tmp_path, {"t": {"reason": "x", "first_seen": "x", "last_seen": "x"}})

        path = tmp_path / "se3" / "state" / "known_test_failures.json"
        assert path.exists()

    def test_overwrites_existing(self, project_root):
        path = project_root / "se3" / "state" / "known_test_failures.json"
        path.write_text('{"old": {}}', encoding="utf-8")

        _save_known_failures(project_root, {"new": {"reason": "x", "first_seen": "x", "last_seen": "x"}})

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert "new" in loaded
        assert "old" not in loaded

    def test_roundtrip(self, project_root):
        data = {
            "tests/test_cli.py::test_delete": {
                "reason": "AssertionError: assert 1 == 0",
                "first_seen": "2026-04-09T12:00:00",
                "last_seen": "2026-04-09T13:00:00",
            },
            "tests/test_cli.py::test_search": {
                "reason": "AssertionError: assert 'found' in ''",
                "first_seen": "2026-04-09T12:00:00",
                "last_seen": "2026-04-09T12:00:00",
            },
        }
        _save_known_failures(project_root, data)
        loaded = _load_known_failures(project_root)
        assert loaded == data


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
# IssueDiscovery.create_from_pre_existing_failures
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
    """Tests for the refactored test_handler fix loop trigger logic."""

    def _make_step_and_flow(self, project_root):
        """Create a Step and FlowInstance for test_handler."""
        step = Step(step_type=StepType.TEST, status=StepStatus.PENDING)
        step.inputs = {"tests_added": [], "is_fix_iteration": False}

        flow = FlowInstance(
            flow_id="test-flow-fix",
            task_description="Fix a bug",
            status=FlowStatus.RUNNING,
        )
        flow.change_path = project_root / "se3.yaml"
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

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_pre_existing_failure_no_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Pre-existing failures should NOT trigger fix loop."""
        # Set up known failures
        known = {
            "tests/test_old.py::test_broken": {
                "reason": "known issue",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, known)

        # pytest output with only the known failure
        stdout = "tests/test_old.py::test_broken FAILED\ntests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        result = step_handler_call(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["test_results"]["overall_passed"] is False
        assert step.outputs.get("fix_needed") is not True

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
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

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_net_new_regression_triggers_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """A regression failure NOT in known failures should trigger fix loop."""
        stdout = (
            "tests/test_existing.py::test_was_passing FAILED\n"
            "tests/test_ok.py::test_pass PASSED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        result = step_handler_call(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["fix_needed"] is True

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_mixed_pre_existing_and_new_triggers_fix_loop(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Mix of pre-existing and new failures: fix loop should still trigger."""
        known = {
            "tests/test_old.py::test_broken": {
                "reason": "known issue",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, known)

        stdout = (
            "tests/test_old.py::test_broken FAILED\n"
            "tests/test_new.py::test_feature FAILED\n"
            "tests/test_ok.py::test_pass PASSED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step.inputs["tests_added"] = ["tests/test_new.py"]
        result = step_handler_call(step, flow)

        assert result == StepStatus.REVISION_NEEDED

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
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

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_overall_passed_reflects_actual_exit_status(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """overall_passed should reflect pytest exit code, not fix logic."""
        known = {
            "tests/test_old.py::test_broken": {
                "reason": "known issue",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, known)

        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        result = step_handler_call(step, flow)

        # overall_passed reflects actual exit code (False)
        assert step.outputs["test_results"]["overall_passed"] is False
        # But fix loop is NOT triggered
        assert result == StepStatus.COMPLETED


class TestPreExistingFailuresOutput:
    """Tests for pre-existing failures in step outputs."""

    def _make_step_and_flow(self, project_root):
        step = Step(step_type=StepType.TEST, status=StepStatus.PENDING)
        step.inputs = {"tests_added": [], "is_fix_iteration": False}

        flow = FlowInstance(
            flow_id="test-flow-pre",
            task_description="Fix a bug",
            status=FlowStatus.RUNNING,
        )
        flow.change_path = project_root / "se3.yaml"
        return step, flow

    def _mock_run_command(self, passed, stdout="", stderr=""):
        return {
            "command": "pytest -v",
            "returncode": 0 if passed else 1,
            "stdout": stdout,
            "stderr": stderr,
            "passed": passed,
        }

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_pre_existing_in_outputs(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Pre-existing failures should appear in outputs['pre_existing_failures']."""
        known = {
            "tests/test_old.py::test_broken": {
                "reason": "assert 1 == 2",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, known)

        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        pre_existing = step.outputs["pre_existing_failures"]
        assert len(pre_existing) == 1
        assert pre_existing[0]["test_id"] == "tests/test_old.py::test_broken"
        assert pre_existing[0]["reason"] == "assert 1 == 2"

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_known_failures_updated_with_last_seen(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """Known failures should have last_seen updated."""
        known = {
            "tests/test_old.py::test_broken": {
                "reason": "assert 1 == 2",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, known)

        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        updated = _load_known_failures(project_root)
        assert "tests/test_old.py::test_broken" in updated
        # last_seen should be updated (not the old date)
        assert updated["tests/test_old.py::test_broken"]["last_seen"] != "2026-01-01T00:00:00"

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_new_regression_added_to_known(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """A net-new regression should be added to known_test_failures.json."""
        stdout = (
            "FAILED tests/test_new_reg.py::test_regressed - AssertionError: oops\n"
            "tests/test_new_reg.py::test_regressed FAILED\n"
        )
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        updated = _load_known_failures(project_root)
        assert "tests/test_new_reg.py::test_regressed" in updated
        entry = updated["tests/test_new_reg.py::test_regressed"]
        assert "first_seen" in entry
        assert "last_seen" in entry

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_no_pre_existing_when_all_pass(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """When all tests pass, pre_existing_failures should be empty."""
        stdout = "tests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(True, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        assert step.outputs["pre_existing_failures"] == []

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_report_called_for_pre_existing(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """_report_pre_existing_issues should be called when pre-existing failures exist."""
        known = {
            "tests/test_old.py::test_broken": {
                "reason": "known issue",
                "first_seen": "2026-01-01T00:00:00",
                "last_seen": "2026-01-01T00:00:00",
            }
        }
        _save_known_failures(project_root, known)

        stdout = "tests/test_old.py::test_broken FAILED\n"
        mock_run.return_value = self._mock_run_command(False, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        mock_report.assert_called_once()
        call_args = mock_report.call_args
        assert len(call_args[0][2]) == 1  # 1 pre-existing failure

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.engine.steps.test._detect_test_command", return_value=["pytest", "-v"])
    def test_report_not_called_when_no_pre_existing(
        self, mock_detect, mock_run, mock_history, mock_report, project_root,
    ):
        """_report_pre_existing_issues should NOT be called when no pre-existing failures."""
        stdout = "tests/test_ok.py::test_pass PASSED\n"
        mock_run.return_value = self._mock_run_command(True, stdout=stdout)

        step, flow = self._make_step_and_flow(project_root)
        step_handler_call(step, flow)

        mock_report.assert_not_called()


# ---------------------------------------------------------------------------
# Helper: call test_handler with proper TestConfig mock
# ---------------------------------------------------------------------------

def step_handler_call(step, flow):
    """Call test_handler with TestConfig mocked to avoid se3.yaml lookup."""
    from se3.engine.steps.test import test_handler

    mock_config = MagicMock()
    mock_config.command = None
    mock_config.timeout = 300
    mock_config.get_phases_for_run.return_value = []

    with patch("se3.config.TestConfig") as MockTestConfig:
        MockTestConfig.load.return_value = mock_config
        return test_handler(step, flow)
