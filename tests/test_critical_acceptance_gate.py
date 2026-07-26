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

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.test import (
    _detect_critical_failures,
    _ensure_verbose_pytest,
    _parse_skipped_test_ids,
    test_handler as run_test_step,
)


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
# _ensure_verbose_pytest
# ---------------------------------------------------------------------------

class TestEnsureVerbosePytest:
    def test_noop_when_no_critical(self):
        cmd = ["python", "-m", "pytest", "-q"]
        assert _ensure_verbose_pytest(cmd, False) == cmd

    def test_appends_v_when_missing(self):
        # A bare pytest command gets -v (the per-test form parseable by
        # _parse_skipped_test_ids), NOT a -r report flag.
        assert _ensure_verbose_pytest(["python", "-m", "pytest"], True) == [
            "python", "-m", "pytest", "-v",
        ]

    def test_report_flags_are_not_sufficient(self):
        # -rs / -ra / -rA only emit "SKIPPED [n] file:line" summary lines that
        # _parse_skipped_test_ids cannot match by test name, so -v is still
        # appended to force parseable per-test output.
        for report_flag in ("-rs", "-ra", "-rA"):
            cmd = ["python", "-m", "pytest", report_flag]
            assert _ensure_verbose_pytest(cmd, True) == [*cmd, "-v"]

    def test_verbose_flag_present_is_unchanged(self):
        for verbose_flag in ("-v", "-vv", "-vvv", "--verbose"):
            cmd = ["python", "-m", "pytest", verbose_flag]
            assert _ensure_verbose_pytest(cmd, True) == cmd

    def test_non_pytest_command_unchanged(self):
        cmd = ["npm", "test"]
        assert _ensure_verbose_pytest(cmd, True) == cmd


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
        from tianluo.config import TestConfig

        # No tianluo.yaml present -> defaults, critical_tests empty.
        cfg = TestConfig.load(tmp_path)
        assert cfg.critical_tests == []

    def test_loads_string_list(self, tmp_path):
        from tianluo.config import TestConfig

        (tmp_path / "tianluo.yaml").write_text(
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
        from tianluo.config import TestConfig

        (tmp_path / "tianluo.yaml").write_text(
            "test:\n  critical_tests: not-a-list\n"
        )
        cfg = TestConfig.load(tmp_path)
        assert cfg.critical_tests == []

    def test_elements_coerced_to_str(self, tmp_path):
        from tianluo.config import TestConfig

        (tmp_path / "tianluo.yaml").write_text(
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

from tianluo.config import TestConfig


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
    @patch("tianluo.config.TestConfig.load")
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

    @patch("tianluo.config.TestConfig.load")
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

    @patch("tianluo.config.TestConfig.load")
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

    @patch("tianluo.config.TestConfig.load")
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

    @patch("tianluo.config.TestConfig.load")
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
# summarize: pure session report + verified=False completion gate (G3)
#
# summarize must (a) gate completion claims when verified=False on BOTH the
# LLM path (gate instruction injected into the prompt) and the fallback path
# (basic summary text), and (b) no longer inject or collect B-class
# issue-discovery.
# ---------------------------------------------------------------------------

from tianluo.engine.steps.summarize import (
    summarize_handler,
    _build_completion_section,
    _create_basic_summary_text,
    _format_test_results,
    _gated_tests_passed,
)


def _make_summarize_flow_and_step(tmp_path, verification_result):
    flow = FlowInstance(
        flow_id="sum-flow",
        task_description="Summarize session",
        task_type="feature",
    )
    flow.change_path = tmp_path / "dummy"
    step = Step(
        step_type=StepType.SUMMARIZE,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Summarize session",
            "changes_made": {"files_changed": ["a.py"]},
            "test_results": {"passed": True},
            "verification_result": verification_result,
            "commit_hash": "abc1234",
        },
    )
    return flow, step


def _run_summarize(flow, step, llm_text="Session report."):
    with patch("tianluo.engine.steps.summarize.LLMCaller") as mock_cls:
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_text
        mock_cls.return_value = mock_caller
        result = summarize_handler(step, flow)
    prompt = ""
    if mock_caller.call.called:
        ca = mock_caller.call.call_args
        prompt = ca.kwargs.get("prompt", "")
        if not prompt and ca.args:
            prompt = ca.args[0]
    return result, prompt


class TestSummarizeCompletionGate:
    def test_llm_path_prompt_carries_gate_when_verified_false(self, tmp_path):
        flow, step = _make_summarize_flow_and_step(tmp_path, {"verified": False})
        result, prompt = _run_summarize(flow, step)

        assert result == StepStatus.COMPLETED
        # The prompt instructs the LLM not to claim completion / all-green.
        assert "Verification Status: NOT PASSED" in prompt
        assert "verified=False" in prompt
        assert "MUST NOT" in prompt

    def test_llm_path_no_gate_when_verified_true(self, tmp_path):
        flow, step = _make_summarize_flow_and_step(tmp_path, {"verified": True})
        result, prompt = _run_summarize(flow, step)

        assert result == StepStatus.COMPLETED
        assert "Verification Status: NOT PASSED" not in prompt

    def test_fallback_path_summary_not_complete_when_verified_false(self, tmp_path):
        # Empty LLM output forces the basic-summary fallback path.
        flow, step = _make_summarize_flow_and_step(tmp_path, {"verified": False})
        result, _ = _run_summarize(flow, step, llm_text="")

        assert result == StepStatus.COMPLETED
        summary = step.outputs["summary"]
        assert "NOT PASSED" in summary
        assert "Not verified" in summary
        assert "all green" not in summary.lower()

    def test_basic_summary_text_gate_direct(self, tmp_path):
        flow = FlowInstance(
            flow_id="f", task_description="t", task_type="feature",
        )
        flow.change_path = tmp_path / "dummy"
        text = _create_basic_summary_text(
            flow, {"files_changed": ["a.py"]}, {"passed": True},
            "task", [], "complete", verified=False,
        )
        assert "Not verified (incomplete)" in text
        assert "NOT PASSED" in text
        assert "all green" not in text.lower()
        # Verified=True keeps the normal "Completed" report.
        text_ok = _create_basic_summary_text(
            flow, {"files_changed": ["a.py"]}, {"passed": True},
            "task", [], "complete", verified=True,
        )
        assert "Completed" in text_ok
        assert "NOT PASSED" not in text_ok

    def test_completion_section_gate_only_when_verified_false(self):
        section = _build_completion_section("complete", [], "", [], [], verified=False)
        assert "NOT PASSED" in section
        assert "MUST NOT" in section
        # Common case: complete + verified True/unknown -> empty section.
        assert _build_completion_section("complete", [], "", [], [], verified=True) == ""
        assert _build_completion_section("complete", [], "", [], []) == ""


class TestSummarizeGatedTestStatus:
    """The session report's test status must reflect the gated ``overall_passed``,
    not the backward-compat ``passed`` key (raw returncode==0, True on a skip)."""

    def test_gated_prefers_overall_passed_false(self):
        # Critical skip: overall_passed forced False while passed stays True.
        tr = {"passed": True, "overall_passed": False}
        assert _gated_tests_passed(tr) is False
        assert "Tests passed: False" in _format_test_results(tr)

    def test_gated_uses_overall_passed_true(self):
        tr = {"passed": True, "overall_passed": True}
        assert _gated_tests_passed(tr) is True
        assert "Tests passed: True" in _format_test_results(tr)

    def test_gated_falls_back_to_passed_for_legacy_dict(self):
        # Legacy test_results without overall_passed -> fall back to passed.
        assert _gated_tests_passed({"passed": True}) is True
        assert _gated_tests_passed({"passed": False}) is False

    def test_basic_summary_does_not_report_green_on_critical_skip(self, tmp_path):
        flow = FlowInstance(
            flow_id="f", task_description="t", task_type="feature",
        )
        flow.change_path = tmp_path / "dummy"
        # overall_passed False (critical skip) but passed True (skip exits 0).
        text = _create_basic_summary_text(
            flow, {"files_changed": ["a.py"]},
            {"passed": True, "overall_passed": False},
            "task", [], "complete", verified=False,
        )
        assert "Tests passed" not in text
        assert "Test status unknown" in text


class TestSummarizeNoIssueDiscovery:
    def test_no_injection_in_prompt_and_no_discovered_issues_output(self, tmp_path):
        flow, step = _make_summarize_flow_and_step(tmp_path, {"verified": True})
        result, prompt = _run_summarize(flow, step)

        assert result == StepStatus.COMPLETED
        # No B-class issue-discovery injection in the prompt.
        assert "discovered_issues" not in prompt
        # And the step produces no discovered_issues output.
        assert "discovered_issues" not in step.outputs

    def test_summarize_default_not_whitelisted(self, tmp_path):
        from tianluo.engine.context_builder import get_issue_discovery_injection

        assert get_issue_discovery_injection("summarize", tmp_path) == ""
