from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from tianluo.config import WorkflowConfig
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.review_scope import ReviewBaseline
from tianluo.engine.review_scope import ReviewScopeManager
from tianluo.engine.review_scope import SelfCheckRoundController
from tianluo.engine.state_machine import StateMachine


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _machine_and_flow(tmp_path: Path, passes: int = 1):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "scope@example.com")
    _git(root, "config", "user.name", "Scope Test")
    (root / ".gitignore").write_text("/tianluo/state/\n", encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")

    with patch("tianluo.engine.state_machine.PersistenceManager"):
        machine = StateMachine(root)
    machine._get_workflow_config = lambda **kwargs: WorkflowConfig(
        self_check_passes_required=passes,
    )
    flow = FlowInstance(
        flow_id="scope-flow",
        task_description="Change value safely",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.context["project_root"] = str(root)
    flow.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
        StepType.INVARIANT_CHECK,
        StepType.COMMIT,
    ]
    baseline = ReviewScopeManager(root, flow.flow_id).capture("implementation")
    flow.state.context["review_scope"] = {
        "implementation_baseline": baseline.to_dict(),
    }
    (root / "app.py").write_text("value = 2\n", encoding="utf-8")
    implement = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        outputs={"files_changed": ["app.py"]},
    )
    test = Step(
        step_type=StepType.TEST,
        status=StepStatus.COMPLETED,
        outputs={"test_results": {"passed": True}},
    )
    flow.state.add_step(implement)
    flow.state.add_step(test)
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    return root, machine, flow, implement


def _add_current(flow: FlowInstance, inputs: dict, status=StepStatus.COMPLETED) -> Step:
    step = Step(
        step_type=StepType.SELF_CHECK,
        status=status,
        inputs=inputs,
        outputs={"issues": [], "actionable_count": 0},
    )
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    return step


def _complete_initial_full(machine: StateMachine, flow: FlowInstance) -> Step:
    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    step = _add_current(flow, inputs)
    next_step = machine.transition_to_next(flow)
    assert next_step is not None
    assert next_step.step_type == StepType.INVARIANT_CHECK
    return next_step


def test_initial_full_multi_pass_keeps_one_round_and_skips_closure(tmp_path):
    _root, machine, flow, _implement = _machine_and_flow(tmp_path, passes=2)
    first_inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    first = _add_current(flow, first_inputs)

    assert first.inputs["scope_mode"] == "full"
    assert first.inputs["self_check_pass_index"] == 1
    assert first.inputs["scope_changed_paths"] == ["app.py"]

    second = machine.transition_to_next(flow)
    assert second is not None
    assert second.step_type == StepType.SELF_CHECK
    assert second.inputs["scope_mode"] == "full"
    assert second.inputs["baseline_id"] == first.inputs["baseline_id"]
    assert second.inputs["self_check_round_id"] == first.inputs["self_check_round_id"]
    assert second.inputs["self_check_pass_index"] == 2

    second.status = StepStatus.COMPLETED
    second.outputs = {"issues": []}
    flow.state.current_step_id = second.step_id
    next_step = machine.transition_to_next(flow)
    assert next_step is not None
    assert next_step.step_type == StepType.INVARIANT_CHECK
    assert flow.state.context["self_check_review"]["completed_full_rounds"] == 1


def test_full_round_after_the_flow_commits_still_carries_the_diff(tmp_path):
    """A planned multi-group IMPLEMENT merges every DAG leaf branch back onto
    the working branch, so HEAD advances before SELF_CHECK runs. The first full
    round must still receive the real baseline-to-current diff — an empty scope
    would hide from the reviewer exactly the change under review."""
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "impl: group G1")

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "full"
    assert inputs["scope_undecidable"] is False
    assert inputs["scope_changed_paths"] == ["app.py"]
    assert "+value = 2" in inputs["scope_diff"]
    assert inputs["scope_causal_anchors"]["app.py"]


def test_post_fix_incremental_clean_inserts_full_closure(tmp_path):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    (root / "app.py").write_text("value = 3\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)

    incremental_inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    incremental = _add_current(flow, incremental_inputs)
    assert incremental.inputs["scope_mode"] == "incremental"
    assert incremental.inputs["baseline_id"] == fix_baseline.baseline_id
    assert "+value = 3" in incremental.inputs["scope_diff"]

    closure = machine.transition_to_next(flow)
    assert closure is not None
    assert closure.step_type == StepType.SELF_CHECK
    assert closure.inputs["scope_mode"] == "full"
    assert closure.inputs["self_check_pass_index"] == 1
    assert closure.inputs["self_check_round_id"] != incremental.inputs["self_check_round_id"]
    assert closure.inputs["self_check_round_reason"] == "full_closure"

    closure.status = StepStatus.COMPLETED
    closure.outputs = {"issues": []}
    flow.state.current_step_id = closure.step_id
    next_step = machine.transition_to_next(flow)
    assert next_step is not None
    assert next_step.step_type == StepType.INVARIANT_CHECK


def test_findings_from_initial_full_round_switch_post_fix_to_incremental(tmp_path):
    """Initial full SELF_CHECK → finding → FIX → post-FIX incremental.

    The initial full round surfaced findings, so it never completed cleanly —
    but it DID run, so the round after FIX must use the persisted fix baseline
    with incremental scope (then a full closure once clean), not another full
    implementation-baseline round.
    """
    root, machine, flow, implement = _machine_and_flow(tmp_path)
    first_inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    first = _add_current(flow, first_inputs, status=StepStatus.REVISION_NEEDED)
    first.outputs = {
        "fix_needed": True,
        "fix_instructions": "fix the initial full finding",
        "fix_context": {"reason": "self_check", "issues": [{"location": "app.py:1"}]},
    }
    flow.state.current_step_id = first.step_id
    with patch.object(machine, "_maybe_transition_to_adjudicate", return_value=None):
        returned = machine.transition_to_next(flow)
    assert returned is implement
    latest = flow.state.context["review_scope"]["latest_fix_baseline"]
    assert latest["kind"] == "fix-1"

    (root / "app.py").write_text("value = 8\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    again = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    assert again["scope_mode"] == "incremental"
    assert again["baseline_id"] == latest["baseline_id"]
    assert again["self_check_round_reason"] == "post_fix_incremental"


def test_untracked_runtime_dir_asset_is_reviewed_but_runtime_state_is_not(tmp_path):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    # A new project asset under tianluo/e2e/ (a whitelisted, trackable subtree
    # of the runtime directory) must enter the full review scope...
    scenario = root / "tianluo" / "e2e" / "scenarios" / "new.yaml"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("name: new\n", encoding="utf-8")
    # ...while transient runtime-state artifacts must never leak into it.
    runtime_state = root / "tianluo" / "state" / "engine.json"
    runtime_state.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.write_text("{}\n", encoding="utf-8")

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    assert inputs["scope_mode"] == "full"
    assert "tianluo/e2e/scenarios/new.yaml" in inputs["scope_changed_paths"]
    assert "tianluo/state/engine.json" not in inputs["scope_changed_paths"]
    assert "+name: new" in inputs["scope_diff"]


def test_test_fix_before_any_full_round_still_uses_full(tmp_path):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    (root / "app.py").write_text("value = 4\n", encoding="utf-8")

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    assert inputs["scope_mode"] == "full"
    assert inputs["self_check_round_reason"] == "initial_full"
    assert inputs["baseline_id"] != fix_baseline.baseline_id


def test_requirement_mutation_discards_incremental_and_restarts_full(tmp_path):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)
    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    (root / "app.py").write_text("value = 5\n", encoding="utf-8")
    incremental = Step(
        step_type=StepType.SELF_CHECK,
        inputs=machine._build_step_inputs(flow, StepType.SELF_CHECK),
    )
    old_round = incremental.inputs["self_check_round_id"]
    assert incremental.inputs["scope_mode"] == "incremental"

    flow.state.context["user_interjections"] = [{"text": "Also preserve zero."}]
    machine._refresh_self_check_scope(flow, incremental)

    assert incremental.inputs["scope_mode"] == "full"
    assert incremental.inputs["self_check_pass_index"] == 1
    assert incremental.inputs["self_check_round_id"] != old_round
    assert incremental.inputs["self_check_round_reason"] == "effective_requirements_changed"


def test_requirement_mutation_after_self_check_reflows_from_downstream_gate(tmp_path):
    _root, machine, flow, _implement = _machine_and_flow(tmp_path)
    invariant = _complete_initial_full(machine, flow)
    invariant.status = StepStatus.COMPLETED
    flow.state.current_step_id = invariant.step_id
    flow.state.context["user_interjections"] = [{"text": "Also preserve zero."}]
    SelfCheckRoundController(flow.state.context).force_full(
        "effective_requirements_changed"
    )

    reflow = machine.transition_to_next(flow)

    assert reflow is not None
    assert reflow.step_type == StepType.SELF_CHECK
    assert reflow.inputs["scope_mode"] == "full"
    assert reflow.inputs["self_check_pass_index"] == 1
    assert reflow.inputs["self_check_round_reason"] == "effective_requirements_changed"


def test_full_closure_finding_routes_to_fix_then_incremental_again(tmp_path):
    root, machine, flow, implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)
    first_fix = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = first_fix.to_dict()
    flow.state.fix_iterations = 1
    (root / "app.py").write_text("value = 6\n", encoding="utf-8")
    incremental = _add_current(
        flow, machine._build_step_inputs(flow, StepType.SELF_CHECK)
    )
    closure = machine.transition_to_next(flow)
    assert closure is not None and closure.inputs["scope_mode"] == "full"

    closure.status = StepStatus.REVISION_NEEDED
    closure.outputs = {
        "fix_needed": True,
        "fix_instructions": "fix closure finding",
        "fix_context": {"reason": "self_check", "issues": [{"location": "app.py:1"}]},
    }
    flow.state.current_step_id = closure.step_id
    with patch.object(machine, "_maybe_transition_to_adjudicate", return_value=None):
        returned = machine.transition_to_next(flow)
    assert returned is implement
    latest = flow.state.context["review_scope"]["latest_fix_baseline"]
    assert latest["kind"] == "fix-2"

    (root / "app.py").write_text("value = 7\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    again = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    assert again["scope_mode"] == "incremental"
    assert again["baseline_id"] == latest["baseline_id"]


def test_test_and_e2e_inputs_never_consume_review_scope(tmp_path):
    _root, machine, flow, _implement = _machine_and_flow(tmp_path)
    flow.state.context["review_scope"]["scope_changed_paths"] = ["app.py"]
    flow.state.context["review_scope"]["scope_mode"] = "incremental"

    test_inputs = machine._build_step_inputs(flow, StepType.TEST)
    e2e_inputs = machine._build_step_inputs(flow, StepType.E2E)
    for inputs in (test_inputs, e2e_inputs):
        assert "scope_mode" not in inputs
        assert "scope_changed_paths" not in inputs
        assert "baseline_id" not in inputs


def test_test_inputs_identical_across_scope_modes(tmp_path):
    """TEST runs the project's full configured scope in every review mode.

    The incremental attention scope is a SELF_CHECK prompt concern only; the
    inputs the TEST step receives must be byte-for-byte scope-independent, so
    no changed-path list can ever narrow the executed test selection.
    """
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    flow.state.context["review_scope"]["scope_changed_paths"] = ["app.py"]
    flow.state.context["review_scope"]["scope_mode"] = "incremental"
    incremental_test_inputs = machine._build_step_inputs(flow, StepType.TEST)

    flow.state.context["review_scope"]["scope_mode"] = "full"
    flow.state.context["review_scope"]["scope_changed_paths"] = []
    full_test_inputs = machine._build_step_inputs(flow, StepType.TEST)

    assert incremental_test_inputs == full_test_inputs
    assert incremental_test_inputs["test_results"] == {"passed": True}
    assert "changed" not in incremental_test_inputs["task_description"].lower()


def test_run_step_persists_implementation_baseline_before_handler_write(tmp_path):
    root, machine, flow, _old_implement = _machine_and_flow(tmp_path)
    flow.state.context.pop("review_scope", None)
    flow.state.baseline_failures = []
    observed = {}

    def writable_handler(step, current_flow):
        descriptor = current_flow.state.context["review_scope"][
            "implementation_baseline"
        ]
        observed["baseline_id"] = descriptor["baseline_id"]
        observed["saved_before_write"] = machine.persistence.save_flow.called
        (root / "app.py").write_text("value = 9\n", encoding="utf-8")
        step.outputs["files_changed"] = ["app.py"]
        return StepStatus.COMPLETED

    implement = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
    flow.state.add_step(implement)
    flow.state.current_step_id = implement.step_id
    machine.register_handler(StepType.IMPLEMENT, writable_handler)

    assert machine.run_step(flow, implement) == StepStatus.COMPLETED
    assert observed["saved_before_write"] is True
    baseline = flow.state.context["review_scope"]["implementation_baseline"]
    scope = ReviewScopeManager(root, flow.flow_id).reconstruct(
        "full",
        machine._review_baseline_from(baseline),
    )
    assert scope.changed_paths == ["app.py"]
    assert "+value = 9" in scope.unified_diff


class TestCrossProcessScopeResume:
    """Fresh-process recovery of persisted review rounds and baselines.

    Unlike the tests above (which mock PersistenceManager), these persist a
    real engine.json plus the runtime blob store and then construct a
    brand-new StateMachine — the same facts a crash-and-resume must rely on.
    """

    def _real_machine(self, tmp_path: Path, passes: int = 1):
        root = tmp_path / "project"
        root.mkdir()
        _git(root, "init")
        _git(root, "config", "user.email", "scope@example.com")
        _git(root, "config", "user.name", "Scope Test")
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "initial")
        machine = StateMachine(root)
        machine._get_workflow_config = lambda **kwargs: WorkflowConfig(
            self_check_passes_required=passes,
        )
        return root, machine

    @staticmethod
    def _patch_workflow_config(machine: StateMachine, passes: int = 1) -> None:
        machine._get_workflow_config = lambda **kwargs: WorkflowConfig(
            self_check_passes_required=passes,
        )

    def _flow_to_incremental(self, tmp_path: Path):
        """Run a flow through initial full and into pass #1 of incremental.

        Returns ``(root, first_machine, flow, fix_baseline, incremental_inputs)``
        with everything persisted to disk.
        """
        root, machine = self._real_machine(tmp_path)
        flow = FlowInstance(
            flow_id="cross-process-flow",
            task_description="Change value safely",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.context["project_root"] = str(root)
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.INVARIANT_CHECK,
            StepType.COMMIT,
        ]
        baseline = ReviewScopeManager(root, flow.flow_id).capture("implementation")
        flow.state.context["review_scope"] = {
            "implementation_baseline": baseline.to_dict(),
        }
        (root / "app.py").write_text("value = 2\n", encoding="utf-8")
        flow.state.add_step(
            Step(
                step_type=StepType.IMPLEMENT,
                status=StepStatus.COMPLETED,
                outputs={"files_changed": ["app.py"]},
            )
        )
        flow.state.add_step(
            Step(
                step_type=StepType.TEST,
                status=StepStatus.COMPLETED,
                outputs={"test_results": {"passed": True}},
            )
        )
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.SELF_CHECK
        )
        machine.persistence.save_flow(flow)

        _complete_initial_full(machine, flow)

        fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
        flow.state.context["review_scope"]["latest_fix_baseline"] = (
            fix_baseline.to_dict()
        )
        flow.state.fix_iterations = 1
        (root / "app.py").write_text("value = 3\n", encoding="utf-8")
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.SELF_CHECK
        )
        incremental_inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
        assert incremental_inputs["scope_mode"] == "incremental"
        _add_current(flow, incremental_inputs)
        machine.persistence.save_flow(flow)
        return root, machine, flow, fix_baseline, incremental_inputs

    def test_incremental_round_state_survives_fresh_process(self, tmp_path):
        root, _first, _flow, fix_baseline, before = self._flow_to_incremental(
            tmp_path
        )

        restarted = StateMachine(root)
        self._patch_workflow_config(restarted)
        restored = restarted.persistence.load_flow()
        assert restored is not None

        again = restarted._build_step_inputs(restored, StepType.SELF_CHECK)
        assert again["self_check_round_id"] == before["self_check_round_id"]
        assert again["scope_mode"] == "incremental"
        assert again["requested_scope_mode"] == "incremental"
        assert again["baseline_id"] == fix_baseline.baseline_id
        assert again["self_check_pass_index"] == before["self_check_pass_index"]
        assert again["self_check_round_reason"] == "post_fix_incremental"
        assert again["requirement_fingerprint"] == before["requirement_fingerprint"]
        assert again["scope_changed_paths"] == ["app.py"]
        # The diff is rebuilt from the persisted blob store, not carried in
        # memory across the restart.
        assert "+value = 3" in again["scope_diff"]
        assert again["scope_fallback_from_incremental"] is False
        # The fix iteration is part of the persisted State itself.
        assert restored.state.fix_iterations == 1
        context_round = restored.state.context["self_check_review"]["active_round"]
        assert context_round["fix_iteration"] == 1

    def test_requirement_fingerprint_detects_mutation_after_restart(self, tmp_path):
        root, _first, _flow, _fix_baseline, before = self._flow_to_incremental(
            tmp_path
        )
        restarted = StateMachine(root)
        self._patch_workflow_config(restarted)
        restored = restarted.persistence.load_flow()

        # A user interjection mutates the effective requirement in the NEW
        # process; the persisted fingerprint must still match what the fresh
        # machine computes, or the mutation would silently stay incremental.
        restored.state.context["user_interjections"] = [
            {"text": "Also preserve zero."}
        ]
        again = restarted._build_step_inputs(restored, StepType.SELF_CHECK)

        assert again["scope_mode"] == "full"
        assert again["self_check_pass_index"] == 1
        assert again["self_check_round_id"] != before["self_check_round_id"]
        assert again["self_check_round_reason"] == "effective_requirements_changed"

    def test_corrupted_baseline_descriptor_degrades_to_full(self, tmp_path):
        root, _first, _flow, fix_baseline, before = self._flow_to_incremental(
            tmp_path
        )
        descriptor = (
            ReviewScopeManager(root, _flow.flow_id)._baseline_dir(
                fix_baseline.baseline_id
            )
            / "descriptor.json"
        )
        descriptor.write_text("{corrupt", encoding="utf-8")

        restarted = StateMachine(root)
        self._patch_workflow_config(restarted)
        restored = restarted.persistence.load_flow()
        again = restarted._build_step_inputs(restored, StepType.SELF_CHECK)

        assert again["scope_mode"] == "full"
        assert again["scope_fallback_from_incremental"] is True
        assert again["self_check_round_reason"] == (
            "incremental_undecidable_full_fallback"
        )
        assert again["baseline_id"] != before["baseline_id"]
        # Full fallback covers the implementation baseline onward — never an
        # empty diff masquerading as an incremental review.
        assert "+value = 3" in again["scope_diff"]
        assert "undecidable" in again["scope_diagnostic"].lower()

    def test_missing_baseline_blob_degrades_to_full(self, tmp_path):
        root, _first, _flow, fix_baseline, _before = self._flow_to_incremental(
            tmp_path
        )
        # app.py is dirty at capture time, so its content lives in a blob; the
        # descriptor alone cannot rebuild the diff.
        import shutil

        blob_dir = (
            ReviewScopeManager(root, _flow.flow_id)._baseline_dir(
                fix_baseline.baseline_id
            )
            / "blobs"
        )
        shutil.rmtree(blob_dir)

        restarted = StateMachine(root)
        self._patch_workflow_config(restarted)
        restored = restarted.persistence.load_flow()
        again = restarted._build_step_inputs(restored, StepType.SELF_CHECK)

        assert again["scope_mode"] == "full"
        assert again["scope_fallback_from_incremental"] is True

    def test_missing_fix_baseline_context_degrades_to_full(self, tmp_path):
        root, _first, _flow, _fix_baseline, _before = self._flow_to_incremental(
            tmp_path
        )
        restarted = StateMachine(root)
        self._patch_workflow_config(restarted)
        restored = restarted.persistence.load_flow()
        # Simulate partial context corruption: the active round references a
        # fix baseline the context no longer carries.
        restored.state.context["review_scope"].pop("latest_fix_baseline", None)
        again = restarted._build_step_inputs(restored, StepType.SELF_CHECK)

        assert again["scope_mode"] == "full"
        assert again["scope_fallback_from_incremental"] is True
        assert again["self_check_round_reason"] == (
            "incremental_undecidable_full_fallback"
        )

    def test_legacy_flow_without_scope_state_resumes_to_full(self, tmp_path):
        root, machine = self._real_machine(tmp_path)
        legacy = FlowInstance(
            flow_id="legacy-scope-flow",
            task_description="Legacy flow predates diff scoping",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        legacy.state.context["project_root"] = str(root)
        legacy.state.selected_steps = [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.INVARIANT_CHECK,
        ]
        original_steps = list(legacy.state.selected_steps)
        plan = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            outputs={
                "task_groups": [
                    {
                        "group_id": "G1",
                        "tasks": [{"description": "plan-only scheduling data"}],
                    }
                ]
            },
        )
        legacy.state.add_step(plan)
        legacy.state.add_step(
            Step(
                step_type=StepType.IMPLEMENT,
                status=StepStatus.COMPLETED,
                outputs={"files_changed": ["app.py"]},
            )
        )
        legacy.state.current_step_index = legacy.state.selected_steps.index(
            StepType.SELF_CHECK
        )
        machine.persistence.save_flow(legacy)

        restarted = StateMachine(root)
        self._patch_workflow_config(restarted)
        restored = restarted.persistence.load_flow()
        assert restored is not None
        # Old persisted paths are never rewritten by resume.
        assert restored.state.selected_steps == original_steps
        # Describing an old flow must never inject the new model's keys into it.
        assert "plan_decomposition" not in restored.state.context
        assert "plan_granularity" not in restored.state.context
        assert "self_check_review" not in restored.state.context

        again = restarted._build_step_inputs(restored, StepType.SELF_CHECK)
        assert again["scope_mode"] == "full"
        # Scheduling data is not requirement authority for the new check.
        assert "plan-only scheduling data" not in again["task_description"]
        assert again["task_description"] == "Legacy flow predates diff scoping"


def test_resume_honors_persisted_pending_closure_marker(tmp_path):
    """A flow interrupted between the in-memory clean-incremental completion
    and the closure step's save persists ``next_scope_mode`` with no active
    round. The transition must recreate the full closure round instead of
    advancing past SELF_CHECK having reviewed only the incremental delta."""
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    flow.state.context["self_check_review"] = {
        "next_scope_mode": "full",
        "full_round_occurred": True,
    }
    _add_current(
        flow,
        {
            "self_check_round_id": "scr-interrupted",
            "self_check_pass_index": 1,
            "self_check_passes_required": 1,
        },
    )

    next_step = machine.transition_to_next(flow)
    assert next_step is not None
    assert next_step.step_type == StepType.SELF_CHECK
    assert next_step.inputs["self_check_round_reason"] == "full_closure"
    assert next_step.inputs["scope_mode"] == "full"
    # The marker was consumed by the closure round's preparation.
    controller = SelfCheckRoundController(flow.state.context)
    assert controller.active_round is not None
    assert controller.active_round["scope_mode"] == "full"
    assert "next_scope_mode" not in flow.state.context["self_check_review"]


def test_resume_without_pending_closure_advances_to_next_gate(tmp_path):
    """With neither an active round nor a pending-closure marker, the
    ordinary progression applies (legacy behavior)."""
    _root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _add_current(
        flow,
        {
            "self_check_round_id": "scr-finished",
            "self_check_pass_index": 1,
            "self_check_passes_required": 1,
        },
    )

    next_step = machine.transition_to_next(flow)
    assert next_step is not None
    assert next_step.step_type == StepType.INVARIANT_CHECK


def test_interjection_after_step_build_refreshes_source_pool_inputs(tmp_path):
    """A late interjection must become part of the verbatim-quote source pool.

    Both interjection paths rewrite only ``inputs['task_description']`` on the
    already-built step; the pre-run refresh must re-derive the clean base and
    the structured interjection list too, otherwise a finding quoting the new
    instruction is dropped as quote-not-in-source and the requirement can never
    be enforced by SELF_CHECK.
    """
    from tianluo.engine.steps.self_check import _build_source_pool

    _root, machine, flow, _implement = _machine_and_flow(tmp_path)
    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    step = _add_current(flow, inputs, status=StepStatus.PENDING)

    assert not any(
        "rename widget to gadget" in text for text in _build_source_pool(step.inputs)
    )

    # What run.py's interrupt / drain paths do: append to the persisted list
    # and re-compose only the composed description on the pending step.
    flow.state.context["user_interjections"] = [
        {"text": "also rename widget to gadget", "timestamp": "t"}
    ]
    step.inputs["task_description"] = "recomposed"

    machine._refresh_self_check_scope(flow, step)

    pool = _build_source_pool(step.inputs)
    assert any("rename widget to gadget" in text for text in pool)
    assert step.inputs["user_interjections"][0]["text"] == (
        "also rename widget to gadget"
    )
    # The round is forced back to full: the requirements themselves moved.
    assert step.inputs["scope_mode"] == "full"


def test_legacy_resume_of_pending_pass_keeps_its_persisted_pass_index(tmp_path):
    """A pre-upgrade flow resumed mid-chain must adopt the consecutive-completed
    tail exactly once. ``run_step`` refreshes the scope before the handler runs;
    constructing the round controller there must not look like persisted round
    state, or the remaining pass restarts the whole N-pass chain (extra paid
    LLM calls on every resume of the same shape)."""
    _root, machine, flow, _implement = _machine_and_flow(tmp_path, passes=3)
    for index in (1, 2):
        _add_current(
            flow,
            {
                "self_check_pass_index": index,
                "self_check_passes_required": 3,
            },
        )
    pending = _add_current(
        flow,
        {"self_check_pass_index": 3, "self_check_passes_required": 3},
        status=StepStatus.PENDING,
    )
    assert "self_check_review" not in flow.state.context

    machine._refresh_self_check_scope(flow, pending)

    assert pending.inputs["self_check_pass_index"] == 3
    assert pending.inputs["self_check_passes_required"] == 3
    assert flow.state.context["self_check_review"]["active_round"]["pass_index"] == 3

    # The adoption happens once: a second refresh (another resume of the same
    # pending step) keeps the round the controller now owns.
    machine._refresh_self_check_scope(flow, pending)
    assert pending.inputs["self_check_pass_index"] == 3


def test_confirm_revision_of_self_check_reruns_full_scope(tmp_path):
    """A confirmation revision is not a FIX — no code changed since the
    rejected round — so the feedback-carrying re-run must not be scoped to the
    stale previous fix delta."""
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()

    reviewed_inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    reviewed = _add_current(flow, reviewed_inputs)
    assert reviewed.inputs["scope_mode"] == "incremental"

    confirm = Step(
        step_type=StepType.CONFIRM,
        status=StepStatus.COMPLETED,
        outputs={
            "revision_feedback": "You missed a defect in untouched code.",
            "review_result": {"approved": False, "step_to_review_id": reviewed.step_id},
        },
    )
    flow.state.add_step(confirm)
    machine._transition_to_revision(flow, confirm, reviewed.step_id)

    machine._refresh_self_check_scope(flow, reviewed)
    assert reviewed.inputs["scope_mode"] == "full"
    assert reviewed.inputs["self_check_round_reason"] == "confirmation_revision"


def test_gitignored_file_written_by_the_flow_reaches_the_review_scope(tmp_path):
    """A git-ignored file the implement step wrote is invisible to baseline
    capture, so without the self-reported path it would never be diffed,
    anchored or reviewed — and a finding citing it would be dropped."""
    root, machine, flow, implement = _machine_and_flow(tmp_path)
    (root / ".gitignore").write_text(
        "/tianluo/state/\ngenerated/\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")
    (root / "generated").mkdir()
    (root / "generated" / "out.js").write_text("var x = 1;\n", encoding="utf-8")
    implement.outputs["files_changed"] = ["app.py", "generated/out.js"]

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_undecidable"] is False
    assert "generated/out.js" in inputs["scope_changed_paths"]
    assert "generated/out.js" in inputs["scope_diff"]
    # Anchor-less: no baseline content exists, so no line anchor is invented.
    assert not inputs["scope_causal_anchors"].get("generated/out.js")


def test_declared_paths_are_persisted_for_the_read_only_diff_command(tmp_path):
    """The round records the declared paths it was scoped with.

    ``luo review-scope diff`` rebuilds the same round from the persisted
    baselines; without the persisted declared paths it would rebuild WITHOUT
    the git-ignored files the round's manifest advertises and reject them as
    out of scope.
    """
    root, machine, flow, implement = _machine_and_flow(tmp_path)
    (root / ".gitignore").write_text(
        "/tianluo/state/\ngenerated/\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")
    (root / "generated").mkdir()
    (root / "generated" / "out.js").write_text("var x = 1;\n", encoding="utf-8")
    implement.outputs["files_changed"] = ["app.py", "generated/out.js"]

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert "generated/out.js" in inputs["scope_changed_paths"]
    persisted = ReviewScopeManager.declared_changed_paths(flow.state.context)
    assert "generated/out.js" in persisted

    rebuilt = ReviewScopeManager(root, flow.flow_id).reconstruct(
        "full",
        ReviewBaseline.from_dict(
            flow.state.context["review_scope"]["implementation_baseline"]
        ),
        declared_paths=persisted,
        write_artifact=False,
    )
    assert "generated/out.js" in rebuilt.changed_paths


def test_incremental_round_inputs_carry_the_whole_task_evidence_domain(tmp_path):
    """End-to-end: an incremental round keeps a finding anchored in earlier work.

    The fix delta is the round's attention, but a defect the checker spots on a
    line the ORIGINAL implement wrote is grounded in git fact — before the
    whole-task domain existed it was discarded as bad evidence while the same
    finding routed through ``missing_in`` landed unconditionally.
    """
    from tianluo.engine.steps.self_check import _validate_and_filter_issues

    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    # The fix touches a different file than the implement step did.
    (root / "other.py").write_text("helper = 1\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "incremental"
    assert inputs["scope_changed_paths"] == ["other.py"]
    assert "app.py" not in inputs["scope_causal_anchors"]
    assert inputs["scope_task_available"] is True
    assert set(inputs["scope_task_changed_paths"]) == {"app.py", "other.py"}
    assert inputs["scope_task_causal_anchors"]["app.py"] == [[1, 1]]
    assert inputs["scope_task_baseline_id"] == (
        flow.state.context["review_scope"]["implementation_baseline"]["baseline_id"]
    )

    issue = {
        "severity": "high",
        "actual_behavior": "value is 2",
        "expected_behavior": "value stays safe",
        "divergence": "consumers read the wrong constant",
        "expectation_source": {
            "type": "task_description",
            "verbatim_quote": "Change value safely",
        },
        "evidence_lines": ["app.py:1"],
        "missing_in": [],
    }
    kept, stats = _validate_and_filter_issues([issue], inputs)
    assert kept == [issue]
    assert stats["bad_evidence_count"] == 0


def test_incremental_round_inputs_name_binaries_that_carry_earlier_work(tmp_path):
    """End-to-end: a binary IMPLEMENT and the fix both touched is not delta-only.

    An anchor-less path owns no added range, so the manifest's earlier-work
    mark can only come from comparing the two persisted baseline snapshots.
    """
    from tianluo.engine.steps.self_check import _format_review_scope

    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    (root / "asset.bin").write_bytes(b"\x00base\xff")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "asset")
    # The implementation baseline predates the commit above, so re-capture it
    # the way the flow would have: before any of this task's writes.
    baseline = ReviewScopeManager(root, flow.flow_id).capture("implementation")
    flow.state.context["review_scope"]["implementation_baseline"] = baseline.to_dict()
    (root / "asset.bin").write_bytes(b"\x00implemented\xff")
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    (root / "asset.bin").write_bytes(b"\x00fixed\xff")
    (root / "fresh.bin").write_bytes(b"\x00fresh\xff")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "incremental"
    assert set(inputs["scope_changed_paths"]) == {"asset.bin", "fresh.bin"}
    assert inputs["scope_prior_work_paths"] == ["asset.bin"]
    rendered = _format_review_scope(inputs)
    assert "asset.bin" in rendered
    assert "domain: this fix + earlier work in this task" in rendered


def test_full_closure_round_inputs_carry_no_separate_task_domain(tmp_path):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    (root / "other.py").write_text("helper = 1\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)

    incremental = _add_current(
        flow, machine._build_step_inputs(flow, StepType.SELF_CHECK)
    )
    assert incremental.inputs["scope_mode"] == "incremental"

    closure = machine.transition_to_next(flow)
    assert closure is not None
    assert closure.inputs["scope_mode"] == "full"
    # The closure round diffs from the implementation baseline itself, so its
    # own anchors already are the whole-task anchors.
    assert closure.inputs["scope_task_available"] is False
    assert closure.inputs["scope_task_changed_paths"] == []
    assert closure.inputs["scope_task_causal_anchors"] == {}
    assert set(closure.inputs["scope_changed_paths"]) == {"app.py", "other.py"}


def test_initial_full_round_has_no_fix_delta_annotation(tmp_path):
    _root, machine, flow, _implement = _machine_and_flow(tmp_path)
    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "full"
    # Nothing has been fixed yet, so there is no "since the last full round"
    # slice to mark — and inventing one would label the original implement
    # work as a fix.
    assert inputs["scope_fix_delta_available"] is False
    assert inputs["scope_fix_delta_changed_paths"] == []
    assert inputs["scope_fix_delta_baseline_id"] == ""


def test_full_closure_round_marks_the_fix_changes_since_the_last_full_round(
    tmp_path,
):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = fix_baseline.to_dict()
    flow.state.fix_iterations = 1
    (root / "other.py").write_text("helper = 1\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)

    incremental = _add_current(
        flow, machine._build_step_inputs(flow, StepType.SELF_CHECK)
    )
    assert incremental.inputs["scope_mode"] == "incremental"

    closure = machine.transition_to_next(flow)
    assert closure is not None
    assert closure.inputs["scope_mode"] == "full"
    # The round reviews the whole task (app.py + other.py) but the manifest can
    # still say which of it the fix produced.
    assert set(closure.inputs["scope_changed_paths"]) == {"app.py", "other.py"}
    assert closure.inputs["scope_fix_delta_available"] is True
    assert closure.inputs["scope_fix_delta_baseline_id"] == fix_baseline.baseline_id
    assert closure.inputs["scope_fix_delta_changed_paths"] == ["other.py"]
    assert "app.py" not in closure.inputs["scope_fix_delta_causal_anchors"]

    from tianluo.engine.steps.self_check import _format_review_scope

    rendered = _format_review_scope(closure.inputs)
    assert "changed by fixes since the last full round" in rendered
    assert "already present at the last full round" in rendered
    # Every marking is a git fact: no iteration counter, no trigger reason.
    assert "fix_iteration" not in rendered
    assert "round_reason" not in rendered


def test_full_round_marker_advances_so_the_next_one_measures_from_it(tmp_path):
    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)
    scope_context = flow.state.context["review_scope"]

    manager = ReviewScopeManager(root, flow.flow_id)
    first_fix = manager.capture("fix-1")
    scope_context["latest_fix_baseline"] = first_fix.to_dict()
    scope_context["fix_baseline_history"] = [
        {"baseline_id": first_fix.baseline_id}
    ]
    flow.state.fix_iterations = 1
    (root / "other.py").write_text("helper = 1\n", encoding="utf-8")
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    _add_current(flow, machine._build_step_inputs(flow, StepType.SELF_CHECK))
    closure = machine.transition_to_next(flow)
    assert closure is not None
    assert closure.inputs["scope_fix_delta_baseline_id"] == first_fix.baseline_id
    # The closure round consumed the first fix, so the marker now sits on it.
    assert scope_context["full_round_fix_head"] == first_fix.baseline_id

    # A later fix + its own closure round measures from the SECOND fix only:
    # the first one was already inside a full round.
    second_fix = manager.capture("fix-2")
    scope_context["latest_fix_baseline"] = second_fix.to_dict()
    scope_context["fix_baseline_history"].append(
        {"baseline_id": second_fix.baseline_id}
    )
    (root / "third.py").write_text("third = 1\n", encoding="utf-8")
    controller = SelfCheckRoundController(flow.state.context)
    controller.mark_findings()
    flow.state.fix_iterations = 2
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    incremental = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    assert incremental["scope_mode"] == "incremental"
    _add_current(flow, incremental)

    later_closure = machine.transition_to_next(flow)
    assert later_closure is not None
    assert later_closure.inputs["scope_mode"] == "full"
    assert (
        later_closure.inputs["scope_fix_delta_baseline_id"]
        == second_fix.baseline_id
    )
    assert later_closure.inputs["scope_fix_delta_changed_paths"] == ["third.py"]


def test_a_pass_one_full_fallback_also_advances_the_full_round_marker(tmp_path):
    """The marker follows the ACCOUNTING, not the mode a round was prepared in.

    An incremental round whose fix baseline cannot be rebuilt degrades to the
    implementation baseline and — when nothing has been reviewed yet on it —
    is credited as the flow's full round. If the "since the last full round"
    marker stayed behind on the previous full round, the NEXT closure round
    would search from a position this one already reviewed past: it would
    re-select the very fix baseline whose corruption forced the degrade and
    lose the annotation of everything the later fixes produced.
    """
    import shutil

    root, machine, flow, _implement = _machine_and_flow(tmp_path)
    _complete_initial_full(machine, flow)
    scope_context = flow.state.context["review_scope"]

    manager = ReviewScopeManager(root, flow.flow_id)
    first_fix = manager.capture("fix-1")
    scope_context["latest_fix_baseline"] = first_fix.to_dict()
    scope_context["fix_baseline_history"] = [{"baseline_id": first_fix.baseline_id}]
    flow.state.fix_iterations = 1
    (root / "other.py").write_text("helper = 1\n", encoding="utf-8")
    # The descriptor survives (so the baseline is still a selectable history
    # entry) but its content blobs are gone, so the round cannot be rebuilt
    # from it — the degrade this test is about.
    shutil.rmtree(manager._baseline_dir(first_fix.baseline_id) / "blobs")

    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    degraded = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert degraded["scope_mode"] == "full"
    assert degraded["scope_fallback_from_incremental"] is True
    assert scope_context["full_round_fix_head"] == first_fix.baseline_id
    _add_current(flow, degraded)
    assert machine.transition_to_next(flow) is not None

    # A later, healthy fix and its closure round must measure from the fix the
    # degraded round already covered — not from the corrupt one.
    second_fix = manager.capture("fix-2")
    scope_context["latest_fix_baseline"] = second_fix.to_dict()
    scope_context["fix_baseline_history"].append(
        {"baseline_id": second_fix.baseline_id}
    )
    (root / "third.py").write_text("third = 1\n", encoding="utf-8")
    SelfCheckRoundController(flow.state.context).mark_findings()
    flow.state.fix_iterations = 2
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    incremental = machine._build_step_inputs(flow, StepType.SELF_CHECK)
    assert incremental["scope_mode"] == "incremental"
    _add_current(flow, incremental)

    closure = machine.transition_to_next(flow)
    assert closure is not None
    assert closure.inputs["scope_mode"] == "full"
    assert closure.inputs["scope_fix_delta_baseline_id"] == second_fix.baseline_id
    assert closure.inputs["scope_fix_delta_changed_paths"] == ["third.py"]


def test_gitignored_path_is_listed_without_a_domain_mark(tmp_path):
    """No baseline snapshot holds a git-ignored path, on either side.

    Its membership in a domain comes from the step's self-report, not from a
    diff, so no persisted git fact can attribute it — and manufacturing one
    would take execution-side bookkeeping of who declared what, which is
    deliberately not kept. The manifest therefore lists the path and stops.
    """
    from tianluo.engine.steps.self_check import _format_review_scope

    root, machine, flow, implement = _machine_and_flow(tmp_path)
    (root / ".gitignore").write_text(
        "/tianluo/state/\ngenerated/\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")
    (root / "generated").mkdir()
    (root / "generated" / "shared.js").write_text(
        "var shared = 1;\n", encoding="utf-8"
    )
    implement.outputs["files_changed"] = ["app.py", "generated/shared.js"]
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = (
        fix_baseline.to_dict()
    )
    flow.state.fix_iterations = 1
    (root / "generated" / "shared.js").write_text(
        "var shared = 2;\n", encoding="utf-8"
    )
    flow.state.add_step(
        Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            inputs={"is_fix_iteration": True},
            outputs={"files_changed": ["generated/shared.js"]},
        )
    )
    flow.state.current_step_index = flow.state.selected_steps.index(
        StepType.SELF_CHECK
    )

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "incremental"
    assert "generated/shared.js" in inputs["scope_changed_paths"]
    assert inputs["scope_declared_only_paths"] == ["generated/shared.js"]
    rendered = _format_review_scope(inputs)
    manifest_line = next(
        line for line in rendered.splitlines()
        if line.strip().startswith("- generated/shared.js")
    )
    # By path ALONE: no domain mark, and no `+N -M` sizes either — a
    # domain-labelled zero pair would read as "both baselines compared this
    # path and found it unchanged", the same claim no snapshot supports.
    assert manifest_line.strip() == "- generated/shared.js"
    assert "domain:" not in manifest_line
    # A path the baselines CAN compare still carries its mark, so the absence
    # above is the declared-path exception and not a lost feature.
    assert "domain: " in rendered
    # And the exception is stated, not silent: the pull command cannot render
    # such a path either, so the checker is told to open the file instead of
    # concluding from an out-of-scope answer that it needs no review.
    assert "no `luo review-scope diff` rendering" in rendered
    assert "open the file itself to review it: generated/shared.js" in rendered


def test_declared_paths_accumulate_across_the_flow(tmp_path):
    """A later FIX's report must not erase what an earlier step declared.

    Declared paths exist for one case: a file git ignores, invisible to
    baseline capture. Replacing the list on every round would delete the whole
    task's record of the ignored file the first IMPLEMENT created, and with it
    the ``--baseline implementation`` view of that file.
    """
    root, machine, flow, implement = _machine_and_flow(tmp_path)
    (root / ".gitignore").write_text(
        "/tianluo/state/\ngenerated/\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")
    (root / "generated").mkdir()
    (root / "generated" / "early.js").write_text(
        "var early = 1;\n", encoding="utf-8"
    )
    implement.outputs["files_changed"] = ["app.py", "generated/early.js"]
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = (
        fix_baseline.to_dict()
    )
    flow.state.fix_iterations = 1
    (root / "app.py").write_text("value = 3\n", encoding="utf-8")
    # The fix reports only its own file.
    flow.state.add_step(
        Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            inputs={"is_fix_iteration": True},
            outputs={"files_changed": ["app.py"]},
        )
    )
    flow.state.current_step_index = flow.state.selected_steps.index(
        StepType.SELF_CHECK
    )

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "incremental"
    assert "generated/early.js" in ReviewScopeManager.declared_changed_paths(
        flow.state.context
    )
    assert "generated/early.js" in inputs["scope_task_changed_paths"]


def test_declared_path_keeps_the_spelling_the_step_reported(tmp_path):
    """A repository path may legitimately begin or end with a space.

    Such a path is anchor-less by construction — no baseline holds it, so no
    diff can correct a name the pipeline rewrote on the way in. Trimming the
    report would make reconstruction look for a file that does not exist and
    drop the real change out of the round's scope entirely, while the trimmed
    spelling is still accepted for the ordinary case of a report that carries
    accidental whitespace.
    """
    from tianluo.engine.steps.self_check import _format_review_scope

    root, machine, flow, implement = _machine_and_flow(tmp_path)
    (root / ".gitignore").write_text(
        "/tianluo/state/\ngenerated/\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")
    (root / "generated").mkdir()
    # A trailing space is part of the name: ``"generated/trailing.js ".strip()``
    # is a DIFFERENT, nonexistent path.
    (root / "generated" / "trailing.js ").write_text(
        "var spaced = 1;\n", encoding="utf-8"
    )
    # One report spelled exactly, one carrying stray whitespace around a
    # path that really has none.
    implement.outputs["files_changed"] = [
        "generated/trailing.js ",
        "  app.py  ",
    ]

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    persisted = ReviewScopeManager.declared_changed_paths(flow.state.context)
    assert "generated/trailing.js " in persisted
    assert inputs["scope_declared_only_paths"] == ["generated/trailing.js "]
    assert "generated/trailing.js " in inputs["scope_changed_paths"]
    rendered = _format_review_scope(inputs)
    # Rendered through ``quote_diff_path`` like every manifest row, so the
    # trailing space survives as part of the quoted token rather than being
    # lost to the line's own trimming.
    assert '- "generated/trailing.js "' in rendered
    # The whitespace-slop report resolves to the real tracked file, which the
    # diff already covers, so it is NOT admitted as an anchor-less path.
    assert "  app.py  " not in rendered
    assert "app.py" in inputs["scope_changed_paths"]


def test_path_ignored_after_capture_keeps_the_anchors_the_task_diff_holds(tmp_path):
    """A declared-only verdict from ONE domain never hides the other's anchors.

    A tracked file the implementation baseline snapshotted, then removed from
    git's index and ignored mid-flow, is invisible to the LATER fix baseline —
    so the fix reconstruction can only classify it as a declared-only
    self-report, while the whole-task comparison still holds real line anchors
    for it. An incremental round grounds on the UNION of the two, so listing
    such a path as anchor-less would advertise "cite the bare path" for the one
    citation form ``_validate_evidence`` then drops as bad evidence.
    """
    from tianluo.engine.steps.self_check import (
        _format_review_scope,
        _validate_and_filter_issues,
    )

    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "scope@example.com")
    _git(root, "config", "user.name", "Scope Test")
    (root / ".gitignore").write_text("/tianluo/state/\n", encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "secret.py").write_text("token = 0\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")

    with patch("tianluo.engine.state_machine.PersistenceManager"):
        machine = StateMachine(root)
    machine._get_workflow_config = lambda **kwargs: WorkflowConfig(
        self_check_passes_required=1,
    )
    flow = FlowInstance(
        flow_id="ignored-after-capture",
        task_description="Change value safely",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.context["project_root"] = str(root)
    flow.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
        StepType.INVARIANT_CHECK,
        StepType.COMMIT,
    ]
    baseline = ReviewScopeManager(root, flow.flow_id).capture("implementation")
    flow.state.context["review_scope"] = {
        "implementation_baseline": baseline.to_dict(),
    }

    # IMPLEMENT edits both files and takes secret.py out of git's sight.
    (root / "app.py").write_text("value = 2\n", encoding="utf-8")
    (root / "secret.py").write_text("token = 0\ntoken = 1\n", encoding="utf-8")
    _git(root, "rm", "--cached", "secret.py")
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("secret.py\n")
    # The flow commits during IMPLEMENT (each DAG leaf branch is merged back),
    # so the removal is in HEAD by the time the fix baseline is captured — which
    # is what makes secret.py invisible to that LATER snapshot while the earlier
    # implementation baseline still holds it.
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "stop tracking secret.py")
    flow.state.add_step(
        Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["app.py", "secret.py"]},
        )
    )
    flow.state.add_step(
        Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
    )
    flow.state.current_step_index = flow.state.selected_steps.index(
        StepType.SELF_CHECK
    )
    _complete_initial_full(machine, flow)

    fix_baseline = ReviewScopeManager(root, flow.flow_id).capture("fix-1")
    flow.state.context["review_scope"]["latest_fix_baseline"] = (
        fix_baseline.to_dict()
    )
    flow.state.fix_iterations = 1
    (root / "app.py").write_text("value = 3\n", encoding="utf-8")
    flow.state.add_step(
        Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            inputs={"is_fix_iteration": True},
            outputs={"files_changed": ["app.py", "secret.py"]},
        )
    )
    flow.state.current_step_index = flow.state.selected_steps.index(
        StepType.SELF_CHECK
    )

    inputs = machine._build_step_inputs(flow, StepType.SELF_CHECK)

    assert inputs["scope_mode"] == "incremental"
    assert inputs["scope_task_available"] is True
    # The whole-task comparison places the path and anchors it...
    assert "secret.py" in inputs["scope_task_changed_paths"]
    assert inputs["scope_task_causal_anchors"].get("secret.py")
    # ...while the fix baseline cannot see it at all, so its reconstruction can
    # only call the path a declared-only self-report.
    assert not inputs["scope_causal_anchors"].get("secret.py")
    # That verdict must not survive the union: the round grounds on both
    # domains at once, so the path is anchor-BEARING for this round.
    assert "secret.py" not in inputs["scope_declared_only_paths"]

    rendered = _format_review_scope(inputs)
    manifest_line = next(
        line for line in rendered.splitlines()
        if line.strip().startswith("- secret.py:")
    )
    assert "added lines (current file)" in manifest_line
    assert "domain:" in manifest_line
    # The anchor-less note must not name a path the round can anchor, or the
    # checker is steered straight into the bad-evidence drop.
    assert "no line anchors" not in rendered or "secret.py" not in next(
        line for line in rendered.splitlines() if "no line anchors" in line
    )

    kept, _stats = _validate_and_filter_issues(
        [
            {
                "severity": "medium",
                "file_path": "secret.py",
                # A regression source bypasses the verbatim-quote pool and
                # leans on diff grounding alone — the strictest reading of the
                # union domain, so it proves the anchors really are citable.
                "expectation_source": {"type": "regression"},
                "evidence_lines": ["secret.py:2"],
                "actual_behavior": "a secret is written to a git-ignored file",
                "expected_behavior": "no secret is written",
                "divergence": "the token leaks to disk",
            }
        ],
        inputs,
    )
    assert len(kept) == 1
