"""Cross-module contract tests for the pre-implement baseline (G7 closure).

The exhaustive *unit* coverage of ``engine/test_baseline.py`` — key
sensitivity, cache hit/miss/corruption, ``BaselineCapture`` wait/timeout/
sentinel — already lives in ``tests/engine/test_baseline_module.py`` (created
alongside the module). The wiring into the state machine (background launch,
``_ensure_baseline_ready`` sync fallback, input injection) is covered by
``tests/engine/test_state_machine_baseline_failures.py``.

This file deliberately does NOT re-test those in isolation. It locks the one
contract that spans two modules and is therefore not covered by either side's
unit tests: the baseline module and the ``test`` step MUST agree on *which
test IDs failed* for the exact same runner output. The whole inherited-vs-
introduced exemption rests on that agreement — the architecture decision to
have ``BaselineCapture`` reuse ``steps/test.py``'s ``_parse_test_ids`` exists
precisely so the frozen baseline and the per-iteration test step never drift.
A regression that changed parsing on one side but not the other would silently
re-classify inherited failures as introduced (reviving the fix-loop) or vice
versa; these tests catch that.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from se3.engine import test_baseline
from se3.engine.steps import test as test_step
from se3.engine.models import FlowInstance, Step, StepStatus, StepType


# A realistic mixed pytest -v output: two passes, two failures across two files.
SAMPLE_OUTPUT = """\
tests/test_a.py::test_one PASSED
tests/test_a.py::test_two FAILED
tests/test_b.py::test_three FAILED
tests/test_b.py::test_four PASSED
"""
EXPECTED_FAILED = {"tests/test_a.py::test_two", "tests/test_b.py::test_three"}


def _failing_set_from_test_step(output: str) -> set[str]:
    """The failing-id set as the *test step* would derive it from ``output``."""
    return {tid for tid, passed in test_step._parse_test_ids(output) if not passed}


class TestBaselineParsingMatchesTestStep:
    """BaselineCapture's parse must equal the test step's parse, byte-for-byte
    output in → identical failing set out."""

    def test_capture_parse_equals_test_step_parse(self, tmp_path):
        cmd = [
            sys.executable, "-c",
            f"import sys; print({SAMPLE_OUTPUT!r}); sys.exit(1)",
        ]
        captured = test_baseline.BaselineCapture(tmp_path, command=cmd).launch().wait()

        assert captured == EXPECTED_FAILED
        # The same output run through steps/test.py's parser yields the same set.
        assert captured == _failing_set_from_test_step(SAMPLE_OUTPUT)

    def test_all_pass_output_agrees_on_empty_set(self, tmp_path):
        all_pass = (
            "tests/test_a.py::test_one PASSED\n"
            "tests/test_a.py::test_two PASSED\n"
        )
        cmd = [sys.executable, "-c", f"print({all_pass!r})"]
        captured = test_baseline.BaselineCapture(tmp_path, command=cmd).launch().wait()

        assert captured == set()
        assert captured == _failing_set_from_test_step(all_pass)


class TestBaselineRoundTripExemptsInheritedFailures:
    """End-to-end provenance contract: a failure *measured* by the baseline,
    when injected back into the test step as ``baseline_failures``, is treated
    as inherited (not introduced) — so it does NOT trigger the fix loop.

    This ties the producer (BaselineCapture) to the consumer (test_handler)
    on a single shared output, which neither module's own unit tests do.
    """

    def _make_flow(self, tmp_path: Path) -> FlowInstance:
        flow = FlowInstance(task_description="scoped task")
        flow.change_path = tmp_path / "se3.yaml"
        return flow

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.config.TestConfig")
    def test_measured_baseline_exempts_same_failures_in_test_step(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        # 1. Measure the baseline from the runner output.
        cmd = [
            sys.executable, "-c",
            f"import sys; print({SAMPLE_OUTPUT!r}); sys.exit(1)",
        ]
        baseline = test_baseline.BaselineCapture(tmp_path, command=cmd).launch().wait()
        assert baseline == EXPECTED_FAILED

        # 2. Feed that frozen baseline into the test step on the SAME output.
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60, critical_tests=[],
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = {
            "command": "python -m pytest -v", "returncode": 1,
            "stdout": SAMPLE_OUTPUT, "stderr": "", "passed": False,
        }
        flow = self._make_flow(tmp_path)
        step = Step(step_type=StepType.TEST)
        step.inputs = {"tests_added": [], "baseline_failures": sorted(baseline)}

        # This asserts the capture→exemption round-trip on the surface-not-loop
        # path, so disable mechanism B's baseline-fix budget (otherwise the
        # inherited failures would be looped within budget and return
        # REVISION_NEEDED). The looping path is covered in test_baseline_fix_loop.py.
        with patch("se3.config.WorkflowConfig") as mock_wf:
            mock_wf.load.return_value = MagicMock(baseline_fix_max_attempts=0)
            status = test_step.test_handler(step, flow)

        # Every failure was already in the measured baseline → inherited only.
        assert status == StepStatus.COMPLETED
        tr = step.outputs["test_results"]
        assert tr["tests_blocking"] is False
        assert set(tr["introduced_failures"]) == set()
        assert set(tr["inherited_failures"]) == EXPECTED_FAILED

    @patch("se3.engine.steps.test._report_pre_existing_issues")
    @patch("se3.engine.steps.test._record_test_history")
    @patch("se3.engine.steps.test._run_command")
    @patch("se3.config.TestConfig")
    def test_failure_absent_from_baseline_is_introduced(
        self, mock_config, mock_run, mock_record, mock_report, tmp_path,
    ):
        """The negative half of the contract: a failure NOT present in the
        measured baseline is introduced and DOES block. Guards against a
        baseline that over-exempts (which would launder real regressions)."""
        # Baseline measured on an all-green tree → empty set.
        cmd = [sys.executable, "-c", "print('tests/test_a.py::test_one PASSED')"]
        baseline = test_baseline.BaselineCapture(tmp_path, command=cmd).launch().wait()
        assert baseline == set()

        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60, critical_tests=[],
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = {
            "command": "python -m pytest -v", "returncode": 1,
            "stdout": SAMPLE_OUTPUT, "stderr": "", "passed": False,
        }
        flow = self._make_flow(tmp_path)
        step = Step(step_type=StepType.TEST)
        step.inputs = {"tests_added": [], "baseline_failures": sorted(baseline)}

        status = test_step.test_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        tr = step.outputs["test_results"]
        assert tr["tests_blocking"] is True
        assert set(tr["introduced_failures"]) == EXPECTED_FAILED
        assert tr["inherited_failures"] == []
