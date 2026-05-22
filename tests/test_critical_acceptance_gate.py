"""Tests for the critical acceptance test gate (G1).

Covers:
- _parse_skipped_test_ids parsing pytest -v SKIPPED lines
- _detect_critical_failures: skip hit, missing hit, normal-skip no-op,
  no-patterns no-op, and the non-parseable no-false-positive guard
- test_handler end-to-end: critical skip and critical missing both force
  tests_passed=False and REVISION_NEEDED with the right test_results fields,
  a non-verbose unparseable run does not falsely report critical_missing,
  and an ordinary (non-critical) skip stays COMPLETED.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.test import (
    _detect_critical_failures,
    _parse_skipped_test_ids,
    test_handler as run_test_step,
)
from se3.engine.steps.verify_spec import verify_spec_handler


# ---------------------------------------------------------------------------
# _parse_skipped_test_ids
# ---------------------------------------------------------------------------

class TestParseSkippedTestIds:
    def test_parses_verbose_skipped_lines(self):
        stdout = (
            "tests/test_ui.py::test_render PASSED\n"
            "tests/test_ui.py::test_browser SKIPPED (chromium not installed)\n"
            "tests/test_ui.py::test_other FAILED\n"
            "tests/test_ui.py::test_platform SKIPPED\n"
        )
        assert _parse_skipped_test_ids(stdout) == [
            "tests/test_ui.py::test_browser",
            "tests/test_ui.py::test_platform",
        ]

    def test_empty_when_no_skips(self):
        stdout = (
            "tests/test_ui.py::test_render PASSED\n"
            "tests/test_ui.py::test_other PASSED\n"
        )
        assert _parse_skipped_test_ids(stdout) == []

    def test_empty_stdout(self):
        assert _parse_skipped_test_ids("") == []

    def test_dedups_preserving_order(self):
        stdout = (
            "tests/test_ui.py::test_browser SKIPPED\n"
            "tests/test_ui.py::test_browser SKIPPED\n"
        )
        assert _parse_skipped_test_ids(stdout) == ["tests/test_ui.py::test_browser"]


# ---------------------------------------------------------------------------
# _detect_critical_failures
# ---------------------------------------------------------------------------

class TestDetectCriticalFailures:
    def test_skip_hit_records_critical_skipped(self):
        ran = ["tests/test_ui.py::test_x"]
        skipped = ["tests/test_ui.py::test_render_paradigm"]
        critical_skipped, critical_missing = _detect_critical_failures(
            ran, skipped, ["test_render_paradigm"],
        )
        assert critical_skipped == ["tests/test_ui.py::test_render_paradigm"]
        assert critical_missing == []

    def test_missing_hit_records_critical_missing(self):
        ran = ["tests/test_ui.py::test_x", "tests/test_ui.py::test_y"]
        skipped: list[str] = []
        critical_skipped, critical_missing = _detect_critical_failures(
            ran, skipped, ["test_render_paradigm"],
        )
        assert critical_skipped == []
        assert critical_missing == ["test_render_paradigm"]

    def test_normal_skip_does_not_match_critical(self):
        # Critical pattern actually RAN (passed); an unrelated test was
        # skipped — neither should be flagged.
        ran = ["tests/test_ui.py::test_render_paradigm"]
        skipped = ["tests/test_ui.py::test_optional_dep"]
        critical_skipped, critical_missing = _detect_critical_failures(
            ran, skipped, ["test_render_paradigm"],
        )
        assert critical_skipped == []
        assert critical_missing == []

    def test_no_patterns_returns_empty(self):
        ran = ["tests/test_ui.py::test_x"]
        skipped = ["tests/test_ui.py::test_y"]
        assert _detect_critical_failures(ran, skipped, []) == ([], [])

    def test_not_parseable_no_false_positive_missing(self):
        # Nothing parseable (non-verbose command) -> do NOT flag missing.
        critical_skipped, critical_missing = _detect_critical_failures(
            [], [], ["test_render_paradigm"],
        )
        assert critical_skipped == []
        assert critical_missing == []

    def test_skip_takes_precedence_over_run(self):
        # Pattern matches both a skipped and a run test -> skip wins
        # (the critical test was skipped in at least one case).
        ran = ["tests/test_ui.py::test_render_b"]
        skipped = ["tests/test_ui.py::test_render_a"]
        critical_skipped, critical_missing = _detect_critical_failures(
            ran, skipped, ["test_render"],
        )
        assert critical_skipped == ["tests/test_ui.py::test_render_a"]
        assert critical_missing == []


# ---------------------------------------------------------------------------
# TestConfig.critical_tests loading
# ---------------------------------------------------------------------------

class TestCriticalTestsConfigLoading:
    def test_default_is_empty_list(self, tmp_path):
        from se3.config import TestConfig

        # No se3.yaml present -> defaults, critical_tests empty.
        cfg = TestConfig.load(tmp_path)
        assert cfg.critical_tests == []

    def test_loads_string_list(self, tmp_path):
        from se3.config import TestConfig

        (tmp_path / "se3.yaml").write_text(
            "test:\n"
            "  critical_tests:\n"
            "    - tests/test_ui.py::test_render_paradigm\n"
            "    - test_browser\n"
        )
        cfg = TestConfig.load(tmp_path)
        assert cfg.critical_tests == [
            "tests/test_ui.py::test_render_paradigm",
            "test_browser",
        ]

    def test_non_list_falls_back_to_empty(self, tmp_path):
        from se3.config import TestConfig

        (tmp_path / "se3.yaml").write_text(
            "test:\n  critical_tests: not-a-list\n"
        )
        cfg = TestConfig.load(tmp_path)
        assert cfg.critical_tests == []

    def test_elements_coerced_to_str(self, tmp_path):
        from se3.config import TestConfig

        (tmp_path / "se3.yaml").write_text(
            "test:\n  critical_tests:\n    - 123\n    - test_x\n"
        )
        cfg = TestConfig.load(tmp_path)
        assert cfg.critical_tests == ["123", "test_x"]


# ---------------------------------------------------------------------------
# test_handler end-to-end
#
# These patch TestConfig.load directly to isolate the handler's gate logic
# from YAML/git config resolution (the latter calls subprocess.run, which the
# global subprocess.Popen patch would otherwise break on a fresh tmp_path).
# subprocess.Popen is still mocked so no real test command runs.
# ---------------------------------------------------------------------------

from se3.config import TestConfig


def _make_flow_and_step(tmp_path):
    flow = FlowInstance(task_description="Critical gate test")
    # project_root = change_path.parent
    flow.change_path = tmp_path / "dummy"
    step = Step(step_type=StepType.TEST)
    return flow, step


def _mock_process(returncode: int, stdout: str, stderr: str = ""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class TestHandlerCriticalGate:
    @patch("se3.config.TestConfig.load")
    @patch("subprocess.Popen")
    def test_critical_skip_triggers_revision(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(critical_tests=["test_render_paradigm"])
        stdout = (
            "tests/test_ui.py::test_render_paradigm SKIPPED (chromium missing)\n"
            "tests/test_ui.py::test_other PASSED\n"
        )
        mock_popen.return_value = _mock_process(0, stdout)

        flow, step = _make_flow_and_step(tmp_path)
        result = run_test_step(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["tests_passed"] is False
        tr = step.outputs["test_results"]
        assert tr["critical_skipped"] == ["tests/test_ui.py::test_render_paradigm"]
        assert tr["critical_missing"] == []
        assert tr["overall_passed"] is False
        # Targeted fix guidance is present
        assert "CRITICAL ACCEPTANCE TESTS NOT VERIFIED" in step.outputs["fix_instructions"]
        assert "SKIPPED" in step.outputs["fix_instructions"]

    @patch("se3.config.TestConfig.load")
    @patch("subprocess.Popen")
    def test_critical_missing_triggers_revision(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(critical_tests=["test_render_paradigm"])
        # Critical pattern appears nowhere, but other tests ran (parseable).
        stdout = (
            "tests/test_ui.py::test_other PASSED\n"
            "tests/test_ui.py::test_more PASSED\n"
        )
        mock_popen.return_value = _mock_process(0, stdout)

        flow, step = _make_flow_and_step(tmp_path)
        result = run_test_step(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["tests_passed"] is False
        tr = step.outputs["test_results"]
        assert tr["critical_missing"] == ["test_render_paradigm"]
        assert tr["critical_skipped"] == []
        assert "MISSING" in step.outputs["fix_instructions"]

    @patch("se3.config.TestConfig.load")
    @patch("subprocess.Popen")
    def test_non_verbose_unparseable_no_false_missing(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="python -m pytest -q",
            critical_tests=["test_render_paradigm"],
        )
        # Non-verbose output: no per-test ::name lines to parse.
        stdout = "..\n2 passed in 0.10s\n"
        mock_popen.return_value = _mock_process(0, stdout)

        flow, step = _make_flow_and_step(tmp_path)
        result = run_test_step(step, flow)

        # No parseable results -> missing detection skipped, no false positive.
        assert result == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is True
        tr = step.outputs["test_results"]
        assert tr["critical_missing"] == []
        assert tr["critical_skipped"] == []

    @patch("se3.config.TestConfig.load")
    @patch("subprocess.Popen")
    def test_ordinary_skip_stays_completed(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(critical_tests=["test_render_paradigm"])
        # The critical test runs+passes; an unrelated optional test is skipped.
        stdout = (
            "tests/test_ui.py::test_render_paradigm PASSED\n"
            "tests/test_ui.py::test_optional_dep SKIPPED (platform)\n"
            "tests/test_ui.py::test_other PASSED\n"
        )
        mock_popen.return_value = _mock_process(0, stdout)

        flow, step = _make_flow_and_step(tmp_path)
        result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is True
        tr = step.outputs["test_results"]
        assert tr["critical_skipped"] == []
        assert tr["critical_missing"] == []

    @patch("se3.config.TestConfig.load")
    @patch("subprocess.Popen")
    def test_no_critical_config_unaffected(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        # No critical_tests configured -> a skipped test that would otherwise
        # be critical is ignored entirely.
        mock_load.return_value = TestConfig()
        stdout = (
            "tests/test_ui.py::test_render_paradigm SKIPPED (chromium missing)\n"
            "tests/test_ui.py::test_other PASSED\n"
        )
        mock_popen.return_value = _mock_process(0, stdout)

        flow, step = _make_flow_and_step(tmp_path)
        result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is True
        tr = step.outputs["test_results"]
        assert tr["critical_skipped"] == []
        assert tr["critical_missing"] == []


# ---------------------------------------------------------------------------
# verify_spec_handler authoritative consumption of critical signals (G2)
#
# verify_spec is the authoritative `verified` computation point. Even if some
# upstream branch left test_results["overall_passed"] truthy, a non-empty
# critical_skipped/critical_missing must force tests_passed=False (and thus
# verified=False) and route through REVISION_NEEDED. These tests deliberately
# set overall_passed=True to exercise the defensive backstop in isolation.
# ---------------------------------------------------------------------------

# LLM verification response that finds no spec issues and (from the LLM's
# limited view) believes tests passed — so the gate must come from the
# rule-based critical-signal backstop, not from the LLM.
_NO_ISSUE_LLM_RESPONSE = json.dumps(
    {
        "issues": [],
        "summary": "All good",
        "recommendations": [],
        "test_analysis": {"tests_passed": True, "failure_summary": "", "root_cause": ""},
        "fix_instructions": "",
    }
)


def _make_verify_flow_and_step(tmp_path, test_results):
    flow = FlowInstance(
        flow_id="verify-flow",
        task_description="Critical gate verify",
        task_type="feature",
    )
    flow.change_path = tmp_path / "changes" / "c"
    step = Step(
        step_type=StepType.VERIFY_SPEC,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Critical gate verify",
            "spec_content": {"spec.md": "spec"},
            "changes_made": {"files_changed": []},
            "test_results": test_results,
        },
    )
    return flow, step


def _run_verify(flow, step, llm_response=_NO_ISSUE_LLM_RESPONSE):
    with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_cls:
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_cls.return_value = mock_caller
        return verify_spec_handler(step, flow)


class TestVerifySpecCriticalGate:
    def test_critical_skipped_forces_verified_false(self, tmp_path):
        flow, step = _make_verify_flow_and_step(
            tmp_path,
            {
                "overall_passed": True,
                "returncode": 0,
                "critical_skipped": ["tests/test_ui.py::test_render_paradigm"],
                "critical_missing": [],
                "stdout": "",
                "stderr": "",
            },
        )
        result = _run_verify(flow, step)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["verified"] is False
        # fix_instructions names the skipped critical acceptance test(s).
        fix = step.outputs["fix_instructions"]
        assert "CRITICAL ACCEPTANCE TESTS NOT VERIFIED" in fix
        assert "SKIPPED" in fix
        assert step.outputs["fix_context"]["reason"] == "critical_acceptance_not_verified"

    def test_critical_missing_forces_verified_false(self, tmp_path):
        flow, step = _make_verify_flow_and_step(
            tmp_path,
            {
                "overall_passed": True,
                "returncode": 0,
                "critical_skipped": [],
                "critical_missing": ["test_render_paradigm"],
                "stdout": "",
                "stderr": "",
            },
        )
        result = _run_verify(flow, step)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["verified"] is False
        fix = step.outputs["fix_instructions"]
        assert "CRITICAL ACCEPTANCE TESTS NOT VERIFIED" in fix
        assert "MISSING" in fix
        assert step.outputs["fix_context"]["critical_missing"] == ["test_render_paradigm"]

    def test_no_critical_and_tests_pass_verified_true(self, tmp_path):
        flow, step = _make_verify_flow_and_step(
            tmp_path,
            {
                "overall_passed": True,
                "returncode": 0,
                "critical_skipped": [],
                "critical_missing": [],
                "stdout": "",
                "stderr": "",
            },
        )
        result = _run_verify(flow, step)

        assert result == StepStatus.COMPLETED
        assert step.outputs["verified"] is True
        assert step.outputs["in_scope_count"] == 0
        assert "fix_instructions" not in step.outputs
