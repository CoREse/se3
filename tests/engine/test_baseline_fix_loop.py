"""Tests for mechanism B: bounded looping of inherited (baseline) failures.

Mechanism B lets the fix loop ALSO attempt inherited (baseline) test failures,
but only:
- excluding ids already *given up* on (cross-flow persistent memory);
- within an independently bounded per-flow budget
  (``workflow.baseline_fix_max_attempts``, default 3, NOT shared with the global
  ``max_fix_iterations``);
- ``0`` disables baseline looping entirely (pure surface, as before).

When the budget is exhausted the active baseline failures are recorded as
given-up and surfaced (not looped).

These tests drive the real ``test_handler`` with ``_run_command`` /
``_record_test_history`` / ``_report_pre_existing_issues`` mocked, and control
the budget by patching ``tianluo.config.WorkflowConfig``. The cross-flow given-up
memory uses the real on-disk store under ``tmp_path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine import baseline_fix_memory as bfm
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
# Import the module (not the function) so pytest does not collect the
# ``test_handler`` symbol as a test case just because its name starts with
# ``test_``. Reference it as ``_test_mod.test_handler`` instead.
from tianluo.engine.steps import test as _test_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

BASELINE_ID = "tests/test_a.py::test_two"
NEW_ID = "tests/test_new.py::test_fresh"

STDOUT_BASELINE_FAIL = (
    "tests/test_a.py::test_one PASSED\n"
    "tests/test_a.py::test_two FAILED\n"
)
STDOUT_BASELINE_PLUS_NEW_FAIL = (
    "tests/test_a.py::test_one PASSED\n"
    "tests/test_a.py::test_two FAILED\n"
    "tests/test_new.py::test_fresh FAILED\n"
)
STDOUT_NEW_FAIL = (
    "tests/test_a.py::test_one PASSED\n"
    "tests/test_new.py::test_fresh FAILED\n"
)


def _make_flow(tmp_path, *, attempts=0) -> FlowInstance:
    flow = FlowInstance(task_description="scoped task")
    flow.change_path = tmp_path / "tianluo.yaml"
    if attempts:
        flow.state.context["baseline_fix_attempts"] = attempts
    return flow


def _make_step(*, tests_added=None, baseline_failures=None) -> Step:
    step = Step(step_type=StepType.TEST)
    step.inputs = {
        "tests_added": tests_added or [],
        "is_fix_iteration": False,
        "baseline_failures": baseline_failures or [],
    }
    return step


def _primary_result(stdout, *, passed=False) -> dict:
    return {
        "command": "python -m pytest -v",
        "returncode": 0 if passed else 1,
        "stdout": stdout,
        "stderr": "",
        "passed": passed,
    }


def _run(flow, step, *, budget, stdout, tmp_path):
    """Run ``test_handler`` with the budget set to *budget* and the test command
    returning *stdout*."""
    with patch("tianluo.engine.steps.test._report_pre_existing_issues"), \
         patch("tianluo.engine.steps.test._record_test_history"), \
         patch("tianluo.engine.steps.test._run_command",
               return_value=_primary_result(stdout)), \
         patch("tianluo.config.TestConfig") as mock_tc, \
         patch("tianluo.config.WorkflowConfig") as mock_wf:
        mock_tc.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60, critical_tests=[],
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_wf.load.return_value = MagicMock(baseline_fix_max_attempts=budget)
        return _test_mod.test_handler(step, flow)


# ---------------------------------------------------------------------------
# Pure baseline within budget → loops
# ---------------------------------------------------------------------------

class TestBaselineLoopsWithinBudget:
    def test_pure_baseline_loops_with_reason_baseline_failure(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(baseline_failures=[BASELINE_ID])

        status = _run(flow, step, budget=3, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        assert status == StepStatus.REVISION_NEEDED
        tr = step.outputs["test_results"]
        assert tr["tests_blocking"] is True
        assert tr["introduced_failures"] == []
        assert tr["inherited_failures"] == [BASELINE_ID]
        assert tr["active_baseline"] == [BASELINE_ID]

        fc = step.outputs["fix_context"]
        assert fc["reason"] == "baseline_failure"
        assert fc["baseline_failures_targeted"] == [BASELINE_ID]

    def test_baseline_fix_instructions_section_present(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(baseline_failures=[BASELINE_ID])

        _run(flow, step, budget=3, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        instr = step.outputs["fix_instructions"]
        # Section header + the specific baseline id are listed.
        assert "BASELINE (PRE-EXISTING) TEST FAILURES" in instr
        assert BASELINE_ID in instr
        # Wording: equal treatment / parallel-not-preempt / guardrails / code-first.
        assert "EQUAL priority" in instr
        assert "PARALLEL" in instr
        assert "guardrail" in instr.lower()
        assert "SHALL" in instr and "MUST" in instr

    def test_given_up_recorded_only_on_exhaustion_not_while_looping(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(baseline_failures=[BASELINE_ID])

        _run(flow, step, budget=3, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        # Still within budget → must NOT be recorded as given-up yet.
        assert bfm.load_given_up(tmp_path) == set()


# ---------------------------------------------------------------------------
# Budget disabled (0) → surface, never loops, never records given-up
# ---------------------------------------------------------------------------

class TestBaselineBudgetDisabled:
    def test_disabled_budget_surfaces_without_loop(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(baseline_failures=[BASELINE_ID])

        status = _run(flow, step, budget=0, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        assert status == StepStatus.COMPLETED
        assert step.outputs["test_results"]["tests_blocking"] is False
        assert step.outputs.get("fix_needed") is not True
        # Disabled is not "exhausted" — nothing is given up.
        assert bfm.load_given_up(tmp_path) == set()


# ---------------------------------------------------------------------------
# Budget exhausted → record given-up + surface
# ---------------------------------------------------------------------------

class TestBaselineBudgetExhausted:
    def test_exhausted_records_given_up_and_surfaces(self, tmp_path):
        # attempts already at the cap → exhausted on this run.
        flow = _make_flow(tmp_path, attempts=2)
        step = _make_step(baseline_failures=[BASELINE_ID])

        status = _run(flow, step, budget=2, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        assert status == StepStatus.COMPLETED
        assert step.outputs["test_results"]["tests_blocking"] is False
        # Persistently recorded as given-up with the exhausted reason.
        details = bfm.load_given_up_details(tmp_path)
        assert BASELINE_ID in details
        assert details[BASELINE_ID]["reason"] == "exhausted"

    def test_exhausted_inherited_still_surfaced(self, tmp_path):
        flow = _make_flow(tmp_path, attempts=3)
        step = _make_step(baseline_failures=[BASELINE_ID])

        _run(flow, step, budget=3, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        # 留痕: inherited still reported in outputs.
        assert step.outputs["inherited_failures"][0]["test_id"] == BASELINE_ID


# ---------------------------------------------------------------------------
# Given-up ids are skipped (not re-looped)
# ---------------------------------------------------------------------------

class TestGivenUpSkipped:
    def test_given_up_id_not_looped(self, tmp_path):
        # Pre-mark the only baseline failure as given-up from a prior flow.
        bfm.record_given_up(tmp_path, [BASELINE_ID], attempts=3, reason="exhausted")

        flow = _make_flow(tmp_path)
        step = _make_step(baseline_failures=[BASELINE_ID])

        status = _run(flow, step, budget=3, stdout=STDOUT_BASELINE_FAIL, tmp_path=tmp_path)

        # active_baseline is empty (the only id is given-up) → no loop.
        assert status == StepStatus.COMPLETED
        tr = step.outputs["test_results"]
        assert tr["tests_blocking"] is False
        assert tr["active_baseline"] == []
        # Inherited still surfaced (留痕).
        assert tr["inherited_failures"] == [BASELINE_ID]


# ---------------------------------------------------------------------------
# Mixed introduced + baseline → introduced reason, baseline section in parallel
# ---------------------------------------------------------------------------

class TestMixedIntroducedAndBaseline:
    def test_mixed_uses_introduced_reason_but_targets_baseline(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(
            tests_added=["tests/test_new.py"],
            baseline_failures=[BASELINE_ID],
        )

        status = _run(
            flow, step, budget=3,
            stdout=STDOUT_BASELINE_PLUS_NEW_FAIL, tmp_path=tmp_path,
        )

        assert status == StepStatus.REVISION_NEEDED
        fc = step.outputs["fix_context"]
        # Reason prefers introduced/critical.
        assert fc["reason"] == "test_failure"
        # Baseline failures are still annotated and the section is prepended.
        assert fc["baseline_failures_targeted"] == [BASELINE_ID]
        instr = step.outputs["fix_instructions"]
        assert "BASELINE (PRE-EXISTING) TEST FAILURES" in instr

    def test_only_introduced_has_no_baseline_section(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(tests_added=["tests/test_new.py"], baseline_failures=[])

        status = _run(flow, step, budget=3, stdout=STDOUT_NEW_FAIL, tmp_path=tmp_path)

        assert status == StepStatus.REVISION_NEEDED
        fc = step.outputs["fix_context"]
        assert fc["reason"] == "test_failure"
        assert "baseline_failures_targeted" not in fc
        assert "BASELINE (PRE-EXISTING)" not in step.outputs["fix_instructions"]
