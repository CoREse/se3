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
