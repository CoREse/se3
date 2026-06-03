"""Cross-step integration: baseline-driven classification agreement (G7 closure).

The per-step *unit* tests already cover each side in isolation:
- ``tests/engine/test_test_handler_fix_loop.py`` — test.py inherited-vs-
  introduced classification, no-auto-populate, file-once dedup.
- ``tests/engine/steps/test_verify_spec.py`` — verify_spec consuming a
  *hand-built* test_results dict, out_of_scope logged-not-filed,
  ``_evaluate_test_gate`` branches.
- ``tests/engine/steps/test_self_check.py`` — self_check out_of_scope 留痕.

What none of them exercises — and what this file locks — is the *seam* between
the two steps on a SINGLE real test_results object: the proposal requires
verify_spec to "consume the SAME baseline-based verdict as test.py". A
hand-built dict can drift from what test.py actually emits; feeding test.py's
real output into verify_spec proves the two steps can never disagree on whether
the test failures block the flow. This is the acceptance-criterion-level
guarantee (criteria 1-4) at the integration layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
# Import the module (not the function) so pytest does not collect the
# ``test_handler`` symbol as a test case just because its name starts with
# ``test_``. Reference it as ``_test_mod.test_handler`` instead.
from se3.engine.steps import test as _test_mod
from se3.engine.steps.verify_spec import verify_spec_handler


# Two pre-existing-style regression failures + one pass.
STDOUT_TWO_REG_FAIL = """\
tests/test_core.py::test_alpha PASSED
tests/test_core.py::test_beta FAILED
tests/test_core.py::test_gamma FAILED
"""
FAILED_IDS = ["tests/test_core.py::test_beta", "tests/test_core.py::test_gamma"]


def _make_flow(tmp_path: Path) -> FlowInstance:
    flow = FlowInstance(task_description="scoped task")
    # parent → tmp_path for both the test step and verify_spec.
    flow.change_path = tmp_path / "se3.yaml"
    return flow


def _run_test_step(flow, tmp_path, baseline_failures):
    """Run the real test step over STDOUT_TWO_REG_FAIL and return its
    ``test_results`` output dict (the object verify_spec will consume).

    The baseline-fix budget (mechanism B) is disabled here so that *inherited*
    failures stay on the surface-not-loop path: this file exercises the
    test.py↔verify_spec verdict seam, which is only reachable when test.py
    returns COMPLETED (a looping baseline returns REVISION_NEEDED and never
    reaches verify_spec). Mechanism B's looping path is covered separately in
    ``tests/engine/test_baseline_fix_loop.py``.
    """
    with patch("se3.engine.steps.test._report_pre_existing_issues"), \
         patch("se3.engine.steps.test._record_test_history"), \
         patch("se3.engine.steps.test._run_command") as mock_run, \
         patch("se3.config.WorkflowConfig") as mock_wf, \
         patch("se3.config.TestConfig") as mock_config:
        mock_wf.load.return_value = MagicMock(baseline_fix_max_attempts=0)
        mock_config.load.return_value = MagicMock(
            command="python -m pytest -v", timeout=60, critical_tests=[],
            get_phases_for_run=MagicMock(return_value=[]),
        )
        mock_run.return_value = {
            "command": "python -m pytest -v", "returncode": 1,
            "stdout": STDOUT_TWO_REG_FAIL, "stderr": "", "passed": False,
        }
        step = Step(step_type=StepType.TEST)
        step.inputs = {"tests_added": [], "baseline_failures": list(baseline_failures)}
        status = _test_mod.test_handler(step, flow)
    return status, step.outputs["test_results"]


def _run_verify_spec(flow, tmp_path, test_results, *, issues=None):
    """Run verify_spec consuming ``test_results`` with a mocked LLM returning
    ``issues`` (default: none). Returns (status, step)."""
    response = json.dumps({
        "issues": issues or [],
        "summary": "verified",
        "recommendations": [],
        "test_analysis": {"tests_passed": True, "failure_summary": "", "root_cause": ""},
        "fix_instructions": "",
    })
    step = Step(
        step_type=StepType.VERIFY_SPEC,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "scoped task",
            "spec_content": {"spec.md": "content"},
            "changes_made": {"files_changed": [{"path": "x.py", "action": "modify"}]},
            "test_results": test_results,
            "fix_iteration": 0,
        },
    )
    with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
        mock_caller = Mock()
        mock_caller.call.return_value = response
        mock_caller_class.return_value = mock_caller
        status = verify_spec_handler(step, flow)
    return status, step


class TestTestStepVerifySpecVerdictAgreement:
    """The verdict test.py emits is exactly the verdict verify_spec consumes."""

    def test_inherited_only_no_loop_and_committable(self, tmp_path):
        """Both failures in the frozen baseline → test.py COMPLETED, and
        verify_spec consuming that real output → COMPLETED + verified=True.
        (Acceptance criterion 1: a scoped flow on a repo with pre-existing
        failures completes without looping and can commit its scoped work.)"""
        flow = _make_flow(tmp_path)
        status, test_results = _run_test_step(flow, tmp_path, baseline_failures=FAILED_IDS)

        # test.py side: inherited only, not blocking.
        assert status == StepStatus.COMPLETED
        assert test_results["tests_blocking"] is False
        assert sorted(test_results["inherited_failures"]) == sorted(FAILED_IDS)
        assert test_results["introduced_failures"] == []

        # verify_spec side: consumes the SAME object → does not loop.
        vs_status, vs_step = _run_verify_spec(flow, tmp_path, test_results)
        assert vs_status == StepStatus.COMPLETED
        assert vs_step.outputs["verified"] is True
        assert vs_step.outputs.get("fix_needed") is not True

    def test_introduced_failure_loops_in_both_steps(self, tmp_path):
        """Empty baseline → every failure is introduced. test.py REVISION_NEEDED
        AND verify_spec REVISION_NEEDED on the same output. (Acceptance
        criterion 2: an introduced regression always triggers the fix loop and
        can never be laundered into the frozen baseline.)"""
        flow = _make_flow(tmp_path)
        status, test_results = _run_test_step(flow, tmp_path, baseline_failures=[])

        assert status == StepStatus.REVISION_NEEDED
        assert test_results["tests_blocking"] is True
        assert sorted(test_results["introduced_failures"]) == sorted(FAILED_IDS)

        vs_status, vs_step = _run_verify_spec(flow, tmp_path, test_results)
        assert vs_status == StepStatus.REVISION_NEEDED
        assert vs_step.outputs["verified"] is False
        assert vs_step.outputs["fix_needed"] is True

    def test_partial_baseline_splits_consistently(self, tmp_path):
        """One failure inherited, one introduced → the introduced one drives the
        loop in both steps; the verdicts stay consistent across the seam."""
        flow = _make_flow(tmp_path)
        status, test_results = _run_test_step(
            flow, tmp_path, baseline_failures=["tests/test_core.py::test_beta"],
        )

        assert status == StepStatus.REVISION_NEEDED
        assert test_results["introduced_failures"] == ["tests/test_core.py::test_gamma"]
        assert test_results["inherited_failures"] == ["tests/test_core.py::test_beta"]
        assert test_results["tests_blocking"] is True

        vs_status, vs_step = _run_verify_spec(flow, tmp_path, test_results)
        assert vs_status == StepStatus.REVISION_NEEDED
        assert vs_step.outputs["verified"] is False


class TestNoAutoPopulateEndToEnd:
    """Driving both steps with an inherited failure never resurrects the
    retired ``known_test_failures.json`` laundering store (acceptance
    criterion 3: the baseline is the sole exemption source)."""

    def test_known_test_failures_json_never_written(self, tmp_path):
        flow = _make_flow(tmp_path)
        status, test_results = _run_test_step(flow, tmp_path, baseline_failures=FAILED_IDS)
        assert status == StepStatus.COMPLETED
        _run_verify_spec(flow, tmp_path, test_results)

        kf_path = tmp_path / "se3" / "state" / "known_test_failures.json"
        assert not kf_path.exists()


class TestOutOfScopeLoggedNotFiledAcrossIterations:
    """verify_spec logs out_of_scope observations (留痕) but never files them as
    issues, and the issue tracker does not balloon across fix iterations
    (acceptance criterion 4)."""

    def _issues_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "se3" / "issues"

    def test_out_of_scope_logged_not_filed_and_no_balloon(self, tmp_path, caplog):
        flow = _make_flow(tmp_path)
        # Tests pass; the only finding is an out-of-scope observation.
        passing_results = {
            "new_tests": {"failed": [], "passed": ["t::ok"], "count": 1},
            "regression": {"failed": [], "passed": [], "count": 0},
            "overall_passed": True,
            "introduced_failures": [],
            "inherited_failures": [],
            "tests_blocking": False,
            "critical_skipped": [],
            "critical_missing": [],
        }
        oos_issues = [{
            "priority": "medium", "scope": "out_of_scope",
            "message": "Pre-existing global in legacy module",
            "suggestion": "track separately",
        }]

        # Simulate three fix iterations all surfacing the same out_of_scope item.
        for _ in range(3):
            with caplog.at_level("INFO"):
                status, step = _run_verify_spec(
                    flow, tmp_path, passing_results, issues=oos_issues,
                )
            # No in-scope issues, tests pass → COMPLETED, verified.
            assert status == StepStatus.COMPLETED
            assert step.outputs["verified"] is True
            assert step.outputs["out_of_scope_count"] == 1

        # 留痕: the observation was logged (not silently dropped).
        assert any(
            "out-of-scope observation" in rec.message.lower()
            or "out-of-scope" in rec.message.lower()
            for rec in caplog.records
        )

        # Not filed: no issue files were ever created across the 3 iterations.
        issues_dir = self._issues_dir(tmp_path)
        created = list(issues_dir.rglob("*.yaml")) if issues_dir.exists() else []
        assert created == []
