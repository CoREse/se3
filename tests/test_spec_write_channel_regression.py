"""G5 task 4 — the core hard boundary: the behavior-change channel survives.

The new spec-write guards (soft injection + PreToolUse hook + post-step diff)
restrict only *who may write spec files*. They MUST NOT touch the legitimate
behavior-change channel:

    plan.spec_changes  →  verify_spec (lenient out_of_scope/low judgement)
                       →  update_spec (writes the new behavior back into specs)

This file pins that the guards left the channel fully usable:

* ``verify_spec``'s lenient judgement is intact — a deviation matching a
  plan-declared ``spec_changes`` entry is treated as out_of_scope and the flow
  passes (``verified`` True, COMPLETED), exactly as before the guards;
* the ``verified`` rule ``(in_scope_count == 0) and tests_passed`` is unchanged
  (an LLM-emitted ``verified`` is overridden by the rule), and the new guards
  add no new path that flips it;
* ``update_spec`` is exempt from BOTH hard layers, so its write-back is never
  blocked;
* a deviation NOT covered by any planned change is still flagged in_scope
  (the leniency is scoped to declared changes — it is not a blanket pass).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from se3.engine.context_builder import SPEC_WRITE_ALLOWED_STEPS
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine.state_machine import StateMachine
from se3.engine.steps.verify_spec import verify_spec_handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _flow(tmp_path):
    change_path = tmp_path / "se3" / "changes" / "test"
    change_path.mkdir(parents=True, exist_ok=True)
    return FlowInstance(
        flow_id="test-channel",
        task_description="Intentionally change existing behavior of X",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=change_path,
    )


# A test_results dict the test step would emit on a green run: not blocking.
_GREEN_TESTS = {"tests_blocking": False, "inherited_failures": []}

# A plan that declared an intended spec change for the behavior being changed.
_PLANNED_SPEC_CHANGES = [
    {
        "spec_name": "flow-engine",
        "change_type": "modify_requirement",
        "target": "Requirement: Behavior X",
        "description": "X now returns Y instead of Z",
        "rationale": "the task intentionally changes this behavior",
    }
]


def _verify_step(spec_changes, issues):
    return Step(
        step_type=StepType.VERIFY_SPEC,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Change behavior of X",
            "spec_content": {"flow-engine": "..."},
            "changes_made": {"files": ["src/x.py"]},
            "test_results": _GREEN_TESTS,
            "spec_changes": spec_changes,
            "baseline_failures": [],
            "fix_iteration": 0,
        },
    ), issues


def _run_verify(tmp_path, spec_changes, issues):
    """Run verify_spec_handler with a mocked LLM returning *issues*."""
    flow = _flow(tmp_path)
    step, _ = _verify_step(spec_changes, issues)

    llm_payload = {
        "issues": issues,
        "summary": "verified",
        "recommendations": [],
        "test_analysis": {"tests_passed": True},
        # The LLM may emit verified; the rule must override it.
        "verified": True,
        "fix_instructions": "",
    }

    with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_cls:
        caller = Mock()
        caller.call.return_value = __import__("json").dumps(llm_payload)
        mock_cls.return_value = caller
        status = verify_spec_handler(step, flow)
    return status, step


# ---------------------------------------------------------------------------
# verify_spec leniency for planned changes is intact
# ---------------------------------------------------------------------------

class TestVerifySpecLeniencyIntact:
    def test_planned_deviation_out_of_scope_passes(self, tmp_path):
        # A deviation matching a plan-declared change, classified out_of_scope.
        issues = [
            {
                "priority": "low",
                "scope": "out_of_scope",
                "message": "behavior of X changed per planned spec_change",
            }
        ]
        status, step = _run_verify(tmp_path, _PLANNED_SPEC_CHANGES, issues)

        assert status == StepStatus.COMPLETED
        assert step.outputs["verified"] is True
        assert step.outputs["in_scope_count"] == 0
        assert step.outputs["out_of_scope_count"] == 1

    def test_no_issues_passes(self, tmp_path):
        status, step = _run_verify(tmp_path, _PLANNED_SPEC_CHANGES, [])
        assert status == StepStatus.COMPLETED
        assert step.outputs["verified"] is True

    def test_unplanned_in_scope_deviation_still_blocks(self, tmp_path):
        # The leniency is scoped to declared changes: an in_scope deviation
        # still drives REVISION_NEEDED — the guards did not turn this into a
        # blanket pass.
        issues = [
            {
                "priority": "high",
                "scope": "in_scope",
                "message": "unrelated regression introduced",
            }
        ]
        status, step = _run_verify(tmp_path, _PLANNED_SPEC_CHANGES, issues)

        assert status == StepStatus.REVISION_NEEDED
        assert step.outputs["verified"] is False
        assert step.outputs["in_scope_count"] == 1


# ---------------------------------------------------------------------------
# The verified rule itself is unchanged by the guard work
# ---------------------------------------------------------------------------

class TestVerifiedRuleUnchanged:
    def test_llm_verified_true_overridden_when_in_scope(self, tmp_path):
        # LLM says verified=True but there is an in_scope issue → rule wins.
        issues = [{"priority": "high", "scope": "in_scope", "message": "bug"}]
        _status, step = _run_verify(tmp_path, [], issues)
        assert step.outputs["verified"] is False

    def test_verified_true_only_when_no_in_scope_and_tests_pass(self, tmp_path):
        issues = [{"priority": "low", "scope": "out_of_scope", "message": "note"}]
        _status, step = _run_verify(tmp_path, _PLANNED_SPEC_CHANGES, issues)
        assert step.outputs["verified"] is True


# ---------------------------------------------------------------------------
# update_spec write-back is exempt from BOTH hard layers
# ---------------------------------------------------------------------------

class TestUpdateSpecWriteBackNotBlocked:
    def test_update_spec_in_exemption_set(self):
        assert "update_spec" in SPEC_WRITE_ALLOWED_STEPS

    def test_hook_not_installed_for_update_spec(self, tmp_path):
        from se3.engine.llm_caller import LLMCaller

        caller = LLMCaller(
            project_root=tmp_path,
            step_type="update_spec",
            agents=[{"cmd": "claude", "name": "claude", "priority": 0}],
        )
        # The PreToolUse hook must not be installed → no --settings → its
        # Write/Edit of se3/specs is never denied.
        assert caller._resolve_spec_guard_settings() is None

    def test_diff_fallback_not_applied_to_update_spec(self, tmp_path):
        machine = StateMachine(project_root=tmp_path)
        fake_step = SimpleNamespace(step_type=SimpleNamespace(value="update_spec"))
        assert machine._spec_diff_guard_enabled(fake_step) is False

    def test_update_spec_actually_writes_spec_without_failing(self, tmp_path):
        # End-to-end: update_spec writing a spec through run_step is COMPLETED,
        # never failed by the diff fallback.
        spec = tmp_path / "se3" / "specs" / "flow-engine" / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("# flow-engine Specification\n\n## Purpose\nold\n", encoding="utf-8")

        machine = StateMachine(project_root=tmp_path)
        flow = _flow(tmp_path)
        flow.state.baseline_failures = []
        step = Step(step_type=StepType.UPDATE_SPEC, status=StepStatus.PENDING, inputs={})

        def handler(_step, _flow):
            # Write the new behavior back into the spec — the legitimate channel.
            spec.write_text(
                "# flow-engine Specification\n\n## Purpose\nnew behavior X→Y\n",
                encoding="utf-8",
            )
            return StepStatus.COMPLETED

        machine.register_handler(StepType.UPDATE_SPEC, handler)
        status = machine.run_step(flow, step)

        assert status == StepStatus.COMPLETED
        assert "new behavior" in spec.read_text(encoding="utf-8")
