"""Tests for wiring the ``E2E`` step into the flow engine.

Two guarantees are pinned here, and they pull in opposite directions:

1. **A project that has not enabled e2e must see a byte-identical sequence** to
   the one it saw before the subsystem existed. That is why E2E is absent from
   every default table and is inserted conditionally instead; the parametrized
   tests below assert element-for-element equality for every task type.
2. **When it IS enabled, the step must actually run and route like ``test``** —
   inserted right after TEST (hence before SELF_CHECK), surviving analyze's
   sequence rebuild, and driving the shared fix loop with the same iteration
   budget and the same exhaustion outcome.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tianluo.config import insert_e2e_step
from tianluo.engine.models import (
    STEP_POOL,
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from tianluo.engine.schema import StepTypeValue
from tianluo.engine.state_machine import StateMachine, _infer_fix_reason
from tianluo.engine.step_renderers import STEP_RENDERERS, STEP_TITLE_KEYS
from tianluo.engine.steps import STEP_HANDLERS

TASK_TYPES = ["feature", "bugfix", "small", "discovery", "review", "survey"]
# The task types whose default sequence carries a TEST step — the only ones e2e
# can attach to (review/survey produce no code change).
TASK_TYPES_WITH_TEST = ["feature", "bugfix", "small", "discovery"]


def _enable(project_root, enabled=True):
    (project_root / "tianluo.yaml").write_text(
        "e2e:\n  enabled: {}\n".format("true" if enabled else "false"),
        encoding="utf-8",
    )
    return project_root


# ---------------------------------------------------------------------------
# Registry / metadata
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_step_type_and_schema_mirror(self):
        assert StepType.E2E.value == "e2e"
        assert StepTypeValue.E2E.value == "e2e"

    def test_handler_is_registered_and_callable(self):
        assert StepType.E2E in STEP_HANDLERS
        assert callable(STEP_HANDLERS[StepType.E2E])

    def test_step_pool_metadata(self):
        info = STEP_POOL[StepType.E2E]
        assert info["name"] == "e2e"
        # Program-driven: the third assertion tier's LLM call is an internal,
        # scenario-declared option, not the step's mode of operation.
        assert info["uses_llm"] is False
        # The environment binds the source tree in and scenarios produce
        # artefacts, so the step is not read-only.
        assert info["read_only"] is False
        assert info["inputs"] == ["changes_made", "test_results"]
        assert info["outputs"] == [
            "e2e_results",
            "scenarios_passed",
            "scenarios_failed",
            "environment_error",
        ]

    def test_renderer_and_title_registered(self):
        assert STEP_TITLE_KEYS[StepType.E2E] == "cli.steprender.title.e2e"
        assert StepType.E2E in STEP_RENDERERS

    def test_fix_reason_is_mapped(self):
        assert _infer_fix_reason("e2e") == "e2e_failure"

    def test_e2e_absent_from_every_default_sequence(self):
        """The default tables must stay free of E2E — that is what keeps a
        project which never enabled it unaffected."""
        for task_type in TASK_TYPES:
            assert StepType.E2E not in get_default_step_sequence(task_type)


# ---------------------------------------------------------------------------
# insert_e2e_step
# ---------------------------------------------------------------------------


class TestConditionalInsertion:
    @pytest.mark.parametrize("task_type", TASK_TYPES)
    def test_disabled_leaves_sequence_identical(self, tmp_path, task_type):
        _enable(tmp_path, enabled=False)
        original = get_default_step_sequence(task_type)

        result = insert_e2e_step(list(original), tmp_path)

        assert result == original
        assert StepType.E2E not in result

    @pytest.mark.parametrize("task_type", TASK_TYPES)
    def test_absent_config_leaves_sequence_identical(self, tmp_path, task_type):
        """No tianluo.yaml at all: the default is off."""
        original = get_default_step_sequence(task_type)

        assert insert_e2e_step(list(original), tmp_path) == original

    @pytest.mark.parametrize("task_type", TASK_TYPES_WITH_TEST)
    def test_enabled_inserts_right_after_test(self, tmp_path, task_type):
        _enable(tmp_path)
        original = get_default_step_sequence(task_type)

        result = insert_e2e_step(list(original), tmp_path)

        assert result.index(StepType.E2E) == result.index(StepType.TEST) + 1
        # Everything else is untouched: removing E2E gives the original back.
        assert [s for s in result if s is not StepType.E2E] == original

    def test_enabled_places_e2e_before_self_check(self, tmp_path):
        _enable(tmp_path)
        result = insert_e2e_step(get_default_step_sequence("feature"), tmp_path)

        assert result.index(StepType.E2E) < result.index(StepType.SELF_CHECK)

    @pytest.mark.parametrize("task_type", ["review", "survey"])
    def test_sequence_without_test_is_untouched(self, tmp_path, task_type):
        _enable(tmp_path)
        original = get_default_step_sequence(task_type)

        result = insert_e2e_step(list(original), tmp_path)

        assert result == original
        assert StepType.E2E not in result

    def test_idempotent(self, tmp_path):
        _enable(tmp_path)
        once = insert_e2e_step(get_default_step_sequence("feature"), tmp_path)
        twice = insert_e2e_step(list(once), tmp_path)

        assert twice == once
        assert twice.count(StepType.E2E) == 1

    def test_input_list_is_not_mutated(self, tmp_path):
        _enable(tmp_path)
        original = get_default_step_sequence("feature")
        snapshot = list(original)

        insert_e2e_step(original, tmp_path)

        assert original == snapshot

    def test_inserts_after_the_first_test_when_several_exist(self, tmp_path):
        _enable(tmp_path)
        steps = [StepType.TEST, StepType.SELF_CHECK, StepType.TEST]

        result = insert_e2e_step(steps, tmp_path)

        assert result == [
            StepType.TEST,
            StepType.E2E,
            StepType.SELF_CHECK,
            StepType.TEST,
        ]


# ---------------------------------------------------------------------------
# Sequence-building call sites
# ---------------------------------------------------------------------------


class TestSequenceBuilders:
    def _state_machine(self, project_root):
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=project_root)

    def test_create_flow_inserts_when_enabled(self, tmp_path):
        _enable(tmp_path)
        machine = self._state_machine(tmp_path)

        flow = machine.create_flow("add a login form", task_type="feature")

        steps = flow.state.selected_steps
        assert steps.index(StepType.E2E) == steps.index(StepType.TEST) + 1

    def test_create_flow_untouched_when_disabled(self, tmp_path):
        _enable(tmp_path, enabled=False)
        machine = self._state_machine(tmp_path)

        flow = machine.create_flow("add a login form", task_type="feature")

        assert StepType.E2E not in flow.state.selected_steps

    def test_worktree_flow_keeps_e2e_and_both_merge_steps(self, tmp_path):
        _enable(tmp_path)
        machine = self._state_machine(tmp_path)

        flow = machine.create_flow(
            "add a login form", task_type="feature", is_worktree_mode=True
        )
        steps = flow.state.selected_steps

        assert StepType.E2E in steps
        assert StepType.MERGE_INTEGRATE in steps
        assert StepType.VERSION_RECONCILE in steps
        assert steps.index(StepType.E2E) < steps.index(StepType.MERGE_INTEGRATE)

    def test_analyze_rebuild_keeps_e2e(self, tmp_path):
        """ANALYZE re-derives the sequence from the default table on every flow.

        Without the mirrored insert there, the E2E step create_flow added would be
        dropped the moment the first step completes — and ANALYZE is the first
        step of every sequence, so e2e would never run at all.
        """
        from tianluo.engine.steps.analyze import _update_flow_steps

        _enable(tmp_path)
        flow = FlowInstance(task_description="t", task_type="feature")
        flow.state.context["project_root"] = str(tmp_path)

        _update_flow_steps(flow, "feature")

        steps = flow.state.selected_steps
        assert steps.index(StepType.E2E) == steps.index(StepType.TEST) + 1

    def test_analyze_rebuild_keeps_e2e_with_merge_steps(self, tmp_path):
        from tianluo.engine.steps.analyze import _update_flow_steps

        _enable(tmp_path)
        flow = FlowInstance(task_description="t", task_type="feature")
        flow.state.context["project_root"] = str(tmp_path)
        flow.is_worktree_mode = True

        _update_flow_steps(flow, "feature")

        steps = flow.state.selected_steps
        assert StepType.E2E in steps
        assert StepType.MERGE_INTEGRATE in steps
        assert StepType.VERSION_RECONCILE in steps

    def test_analyze_rebuild_omits_e2e_when_disabled(self, tmp_path):
        from tianluo.engine.steps.analyze import _update_flow_steps

        _enable(tmp_path, enabled=False)
        flow = FlowInstance(task_description="t", task_type="feature")
        flow.state.context["project_root"] = str(tmp_path)

        _update_flow_steps(flow, "feature")

        assert StepType.E2E not in flow.state.selected_steps


# ---------------------------------------------------------------------------
# Fix-loop routing
# ---------------------------------------------------------------------------


def _fix_loop_flow():
    """A flow sitting on an E2E step that just reported a scenario failure."""
    flow = FlowInstance(
        flow_id="flow-e2e-routing",
        task_description="add a login form",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.E2E,
        StepType.SELF_CHECK,
        StepType.COMMIT,
    ]
    implement = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        inputs={"task_groups": []},
        outputs={"files_changed": ["app.py"]},
    )
    flow.state.add_step(implement)
    e2e_step = Step(
        step_type=StepType.E2E,
        status=StepStatus.REVISION_NEEDED,
        outputs={
            "fix_needed": True,
            "fix_instructions": "1 e2e scenario(s) failed. Fix the code under test.",
            "fix_context": {
                "reason": "e2e_failure",
                "scenarios_failed": ["login"],
                "issues": [
                    {
                        "scenario": "login",
                        "kind": "dom",
                        "tier": 1,
                        "expected": "#welcome visible",
                        "actual": "#welcome absent",
                    }
                ],
            },
        },
    )
    flow.state.add_step(e2e_step)
    flow.state.current_step_id = e2e_step.step_id
    return flow, implement, e2e_step


class TestFixLoopRouting:
    @pytest.fixture
    def state_machine(self, tmp_path):
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    def test_revision_needed_routes_back_to_implement(self, state_machine):
        flow, implement, _ = _fix_loop_flow()

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        # The SAME implement step is reused, marked as a fix iteration.
        assert next_step.step_id == implement.step_id
        assert next_step.inputs["is_fix_iteration"] is True
        assert next_step.status == StepStatus.PENDING
        assert flow.state.get_fix_iteration() == 1

    def test_fix_reason_recorded_as_e2e_failure(self, state_machine):
        flow, _, _ = _fix_loop_flow()

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3):
            state_machine.transition_to_next(flow)

        # increment_fix_iteration spreads the fix context into the flat entry.
        entry = flow.state.fix_history[-1]
        assert entry["reason"] == "e2e_failure"
        assert entry["trigger_step_type"] == "e2e"
        assert entry["issues"][0]["scenario"] == "login"

    def test_fix_instructions_reach_the_implement_step(self, state_machine):
        flow, _, _ = _fix_loop_flow()

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3):
            next_step = state_machine.transition_to_next(flow)

        assert "e2e scenario" in next_step.inputs["fix_instructions"]
        assert next_step.inputs["fix_context"]["reason"] == "e2e_failure"
        assert next_step.inputs["fix_iteration"] == 1

    def test_exhaustion_creates_issue_and_fails_the_flow(self, state_machine):
        flow, _, e2e_step = _fix_loop_flow()
        flow.state.fix_iterations = 3
        discovery = MagicMock()

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3), \
                patch.object(state_machine, "_get_issue_discovery", return_value=discovery):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED
        discovery.create_from_fix_loop_exhaustion.assert_called_once()
        args = discovery.create_from_fix_loop_exhaustion.call_args.args
        assert args[1] is e2e_step
        # No extra iteration was charged on the way out.
        assert flow.state.get_fix_iteration() == 3

    def test_environment_failure_does_not_touch_the_fix_budget(self, state_machine):
        """A FAILED E2E step (unusable runtime) is not a REVISION_NEEDED, so the
        fix loop is never entered and no iteration is charged."""
        flow, _, e2e_step = _fix_loop_flow()
        e2e_step.status = StepStatus.FAILED
        e2e_step.outputs = {
            "environment_error": "no usable container runtime",
            "e2e_remediation": "add your user to the docker group",
        }

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.state.get_fix_iteration() == 0

    def test_e2e_does_not_route_to_adjudicate(self, state_machine):
        """Adjudication is fed by SELF_CHECK's cross-round ledger only."""
        flow, _, _ = _fix_loop_flow()

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3), \
                patch.object(
                    state_machine, "_maybe_transition_to_adjudicate"
                ) as adjudicate:
            next_step = state_machine.transition_to_next(flow)

        adjudicate.assert_not_called()
        assert next_step.step_type == StepType.IMPLEMENT

    def test_completed_e2e_advances_to_the_next_step(self, state_machine):
        flow, _, e2e_step = _fix_loop_flow()
        e2e_step.status = StepStatus.COMPLETED
        e2e_step.outputs = {"e2e_results": {"failed": 0}, "scenarios_failed": []}

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.SELF_CHECK
        assert flow.state.get_fix_iteration() == 0


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_renderer_summarizes_without_dumping_logs(self):
        """The renderer is a summary: the container log tail belongs in the fix
        instructions and the history record, not in the console panel."""
        from tianluo.engine.step_renderers import _render_e2e

        log_dump = "\n".join("container log line {}".format(i) for i in range(40))
        step = Step(step_type=StepType.E2E, status=StepStatus.REVISION_NEEDED)
        step.outputs = {
            "e2e_results": {
                "runtime": "podman",
                "total": 2,
                "passed": 1,
                "failed": 1,
                "duration": 12.34,
                "scenarios": [
                    {"name": "healthcheck", "passed": True, "assertions": []},
                    {
                        "name": "login",
                        "passed": False,
                        "logs": log_dump,
                        "assertions": [
                            {
                                "kind": "dom",
                                "tier": 1,
                                "passed": False,
                                "expected": "#welcome visible",
                                "actual": "#welcome absent",
                            }
                        ],
                    },
                ],
            }
        }

        with patch("tianluo.engine.step_renderers.render_full") as render_full:
            _render_e2e(step)

        content = render_full.call_args[0][0]
        assert "login" in content
        assert "podman" in content
        assert "#welcome absent" in content
        assert log_dump not in content

    def test_renderer_shows_remediation_for_environment_failure(self):
        from tianluo.engine.step_renderers import _render_e2e

        step = Step(step_type=StepType.E2E, status=StepStatus.FAILED)
        step.outputs = {
            "environment_error": "no usable container runtime",
            "e2e_remediation": "add your user to the docker group",
            "e2e_results": {
                "environment_error": "no usable container runtime",
                "remediation": "add your user to the docker group",
            },
        }

        with patch("tianluo.engine.step_renderers.render_full") as render_full:
            _render_e2e(step)

        content = render_full.call_args[0][0]
        assert "no usable container runtime" in content
        assert "docker group" in content

    @pytest.mark.parametrize(
        "outputs",
        [
            {},
            {"e2e_results": None},
            {"e2e_results": "not a mapping"},
            {"e2e_results": {"scenarios": "not a list", "total": None}},
            {"e2e_results": {"skipped": True, "reason": "disabled"}},
        ],
    )
    def test_renderer_tolerates_missing_or_odd_shapes(self, outputs):
        """A step can fail before producing structured results; the renderer must
        degrade instead of replacing the diagnosis with a traceback."""
        from tianluo.engine.step_renderers import _render_e2e

        step = Step(step_type=StepType.E2E, status=StepStatus.FAILED)
        step.outputs = dict(outputs)
        step.error_message = "boom"

        with patch("tianluo.engine.step_renderers.render_full"):
            _render_e2e(step)
