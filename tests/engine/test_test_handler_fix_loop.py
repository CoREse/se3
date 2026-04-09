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
