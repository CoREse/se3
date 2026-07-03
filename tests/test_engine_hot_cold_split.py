"""Hot/cold split serialization for engine models (issue #244 phase 1).

Part B format core: engine.json shrinks to a KB-scale *header* (flow identity,
status, per-step status table, small scalars) while each step's heavy
inputs/outputs/artifacts and the shared ``State.context`` are externalized to
per-flow *cold* files loaded on demand. This module covers the models layer
(``Step`` / ``State`` / ``FlowInstance`` serialization) added in group G4:

  * header/cold round-trip equivalence (new format save -> load),
  * legacy inline ``from_dict`` compatibility (old engine.json still loads),
  * the header excludes the heavy fields (bounded size),
  * ``content_hash`` stability (write-path incremental-rewrite key).

The persistence/daemon read-side wiring lives in sibling groups; here the cold
loader is an in-memory callback so the format contract is exercised in
isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine.models import (
    CONTEXT_COLD_KEY,
    HOT_COLD_FORMAT,
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.token_usage import UsageTotals


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _make_step(step_id: str = "01_implement_abcd1234") -> Step:
    """A step carrying deliberately heavy inputs/outputs/artifacts."""
    return Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        step_id=step_id,
        retry_count=1,
        max_retries=3,
        model="opus",
        fallback_model="sonnet",
        inputs={"test_results": "x" * 5000, "spec_content": "y" * 3000},
        outputs={"files_changed": ["a.py", "b.py"], "blob": "z" * 4000},
        artifacts=[Path("out/a.py"), Path("out/b.py")],
        error_message=None,
        error_details=None,
    )


def _make_flow(flow_id: str = "20260704-000000_deadbeef") -> FlowInstance:
    """A worktree-mode flow with two heavy steps and a heavy shared context."""
    state = State(
        selected_steps=[StepType.IMPLEMENT, StepType.TEST],
        current_step_index=1,
        review_iterations={"01_implement_abcd1234": 2},
        fix_iterations=3,
        fix_history=[{"iteration": 1}, {"iteration": 2}, {"iteration": 3}],
        baseline_failures=["tests/test_x.py::test_a"],
        context={"spec_content": "s" * 10000, "resolved_type": "feature", "fix_history": []},
        session_token_usage=UsageTotals(input_tokens=100, output_tokens=200),
    )
    s1 = _make_step("01_implement_abcd1234")
    s2 = _make_step("02_test_beefcafe")
    s2.step_type = StepType.TEST
    state.add_step(s1)
    state.add_step(s2)
    state.current_step_id = s1.step_id
    return FlowInstance(
        flow_id=flow_id,
        status=FlowStatus.RUNNING,
        task_description="do the thing",
        task_type="feature",
        state=state,
        is_worktree_mode=True,
        worktree_branch="impl/x",
        worktree_path="/tmp/wt",
        worktree_original_branch="master",
    )


def _cold_loader_from(flow: FlowInstance):
    """An in-memory cold loader over a flow's extracted cold payloads."""
    payloads = flow.state.extract_cold_payloads()
    calls: list[str] = []

    def loader(key: str):
        calls.append(key)
        return payloads.get(key)

    return loader, calls


# --------------------------------------------------------------------------- #
# Step
# --------------------------------------------------------------------------- #


def test_step_header_excludes_heavy_fields():
    """The step header carries the status table but never the heavy payload."""
    header = _make_step().to_header_dict()
    assert not ({"inputs", "outputs", "artifacts"} & set(header)), header.keys()
    # Status-table fields the hot read path needs are present.
    for key in ("step_id", "step_type", "status", "cold_ref", "content_hash"):
        assert key in header
    # cold_ref points at this step's cold file key (its step_id).
    assert header["cold_ref"] == "01_implement_abcd1234"


def test_step_header_cold_roundtrip_equivalent():
    """New-format save -> load reproduces the original step exactly."""
    step = _make_step()
    header = step.to_header_dict()
    cold = step.extract_cold()

    loaded = Step.from_dict(header, cold_loader=lambda k: cold)

    assert loaded.step_id == step.step_id
    assert loaded.step_type == step.step_type
    assert loaded.status == step.status
    assert loaded.retry_count == step.retry_count
    assert loaded.model == step.model
    assert loaded.inputs == step.inputs
    assert loaded.outputs == step.outputs
    assert loaded.artifacts == step.artifacts


def test_step_header_only_load_without_loader_is_empty_but_intact():
    """A header-only read (no loader) keeps the status table, drops cold data.

    This is the daemon hot path: it wants status/identity, never the payload.
    """
    step = _make_step()
    loaded = Step.from_dict(step.to_header_dict())
    assert loaded.status == step.status
    assert loaded.step_type == step.step_type
    assert loaded.inputs == {}
    assert loaded.outputs == {}
    assert loaded.artifacts == []


def test_step_old_inline_from_dict_compatible():
    """A legacy inline step dict still deserializes with its payload intact."""
    step = _make_step()
    inline = step.to_dict()
    assert "inputs" in inline and "cold_ref" not in inline

    loaded = Step.from_dict(inline)
    assert loaded.inputs == step.inputs
    assert loaded.outputs == step.outputs
    assert loaded.artifacts == step.artifacts


def test_step_missing_cold_file_degrades_without_crashing(caplog):
    """A loader that returns None (missing/corrupt cold file) degrades to empty."""
    header = _make_step().to_header_dict()
    with caplog.at_level("WARNING"):
        loaded = Step.from_dict(header, cold_loader=lambda k: None)
    assert loaded.inputs == {}
    assert loaded.outputs == {}
    assert loaded.status == StepStatus.COMPLETED
    assert any("missing or unreadable" in r.message for r in caplog.records)


def test_step_content_hash_is_stable_and_order_independent():
    """content_hash is deterministic and independent of dict insertion order."""
    a = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={"x": 1, "y": 2}, outputs={})
    b = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={"y": 2, "x": 1}, outputs={})
    assert a.cold_content_hash() == a.cold_content_hash()
    assert a.cold_content_hash() == b.cold_content_hash()


def test_step_content_hash_changes_with_payload():
    """A changed payload yields a different hash (write-path rewrite trigger)."""
    step = _make_step()
    before = step.cold_content_hash()
    step.outputs = {**step.outputs, "new": "value"}
    assert step.cold_content_hash() != before


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def test_state_header_excludes_context_and_step_payloads():
    """The state header externalizes context and every step's heavy fields."""
    header = _make_flow().state.to_header_dict()
    assert "context" not in header
    assert header["context_ref"] == CONTEXT_COLD_KEY
    assert "context_hash" in header
    for step_header in header["steps"].values():
        assert not ({"inputs", "outputs", "artifacts"} & set(step_header))


def test_state_header_cold_roundtrip_equivalent():
    """State header + cold payloads rehydrate to an equivalent State."""
    flow = _make_flow()
    state = flow.state
    header = state.to_header_dict()
    loader, _ = _cold_loader_from(flow)

    loaded = State.from_dict(header, cold_loader=loader)

    assert loaded.current_step_id == state.current_step_id
    assert loaded.selected_steps == state.selected_steps
    assert loaded.current_step_index == state.current_step_index
    assert loaded.review_iterations == state.review_iterations
    assert loaded.fix_iterations == state.fix_iterations
    assert loaded.fix_history == state.fix_history
    assert loaded.baseline_failures == state.baseline_failures
    assert loaded.context == state.context
    assert loaded.session_token_usage.to_dict() == state.session_token_usage.to_dict()
    for sid, step in state.steps.items():
        assert loaded.steps[sid].inputs == step.inputs
        assert loaded.steps[sid].outputs == step.outputs


def test_state_rehydrate_loads_cold_context():
    """rehydrate pulls the shared context through the cold loader."""
    flow = _make_flow()
    header = flow.state.to_header_dict()
    loader, calls = _cold_loader_from(flow)

    loaded = State.rehydrate(header, cold_loader=loader)
    assert loaded.context == flow.state.context
    assert CONTEXT_COLD_KEY in calls


def test_state_missing_context_cold_degrades_to_empty():
    """A missing context cold file loads an empty context, not a crash."""
    header = _make_flow().state.to_header_dict()
    loaded = State.from_dict(header, cold_loader=lambda k: None)
    assert loaded.context == {}
    # Header-carried small fields survive.
    assert loaded.fix_iterations == 3


def test_state_old_inline_from_dict_compatible():
    """A legacy inline state dict (context + inline steps) still loads."""
    state = _make_flow().state
    inline = state.to_dict()
    assert "context" in inline and "context_ref" not in inline

    loaded = State.from_dict(inline)
    assert loaded.context == state.context
    for sid, step in state.steps.items():
        assert loaded.steps[sid].inputs == step.inputs


def test_state_extract_cold_payloads_covers_steps_and_context():
    """extract_cold_payloads maps every step_id plus the reserved context key."""
    state = _make_flow().state
    payloads = state.extract_cold_payloads()
    assert set(payloads) == set(state.steps) | {CONTEXT_COLD_KEY}
    assert payloads[CONTEXT_COLD_KEY] == {"context": state.context}


# --------------------------------------------------------------------------- #
# FlowInstance
# --------------------------------------------------------------------------- #


def test_flow_header_keeps_top_level_fields():
    """The flow header keeps identity + worktree metadata for the hot read path."""
    header = _make_flow().to_header_dict()
    assert header["format"] == HOT_COLD_FORMAT
    assert header["flow_id"] == "20260704-000000_deadbeef"
    assert header["status"] == "running"
    assert header["is_worktree_mode"] is True
    assert header["worktree_branch"] == "impl/x"
    # State is externalized under the header, context lives cold.
    assert "context" not in header["state"]


def test_flow_header_cold_roundtrip_equivalent():
    """Full flow header + cold payloads round-trip to an equivalent flow."""
    flow = _make_flow()
    header = flow.to_header_dict()
    loader, _ = _cold_loader_from(flow)

    loaded = FlowInstance.from_dict(header, cold_loader=loader)
    assert loaded.flow_id == flow.flow_id
    assert loaded.status == flow.status
    assert loaded.task_description == flow.task_description
    assert loaded.is_worktree_mode == flow.is_worktree_mode
    assert loaded.worktree_branch == flow.worktree_branch
    assert loaded.state.context == flow.state.context
    for sid, step in flow.state.steps.items():
        assert loaded.state.steps[sid].inputs == step.inputs
        assert loaded.state.steps[sid].outputs == step.outputs


def test_flow_header_only_load_gives_identity_and_status():
    """Daemon hot path: header-only load (no loader) yields identity/status."""
    flow = _make_flow()
    loaded = FlowInstance.from_dict(flow.to_header_dict())
    assert loaded.flow_id == flow.flow_id
    assert loaded.status == FlowStatus.RUNNING
    assert loaded.is_worktree_mode is True
    # Heavy data absent without a loader.
    assert loaded.state.context == {}
    assert loaded.state.steps[flow.state.current_step_id].inputs == {}


def test_flow_old_inline_from_dict_compatible():
    """A legacy inline engine.json dict still deserializes fully."""
    flow = _make_flow()
    inline = flow.to_dict()
    assert "format" not in inline
    loaded = FlowInstance.from_dict(inline)
    assert loaded.flow_id == flow.flow_id
    assert loaded.state.context == flow.state.context
    for sid, step in flow.state.steps.items():
        assert loaded.state.steps[sid].inputs == step.inputs


def test_flow_header_is_bounded_despite_heavy_payload():
    """The header stays KB-scale even with multi-KB step payloads and context.

    Acceptance criterion: a same-scale flow's engine.json header is <100KB. Here
    the inline form is already tens of KB from the heavy fields; the header must
    be a small fraction of it.
    """
    flow = _make_flow()
    header_bytes = len(json.dumps(flow.to_header_dict(), ensure_ascii=False, default=str))
    inline_bytes = len(json.dumps(flow.to_dict(), ensure_ascii=False, default=str))
    assert header_bytes < 100_000
    assert header_bytes < inline_bytes // 2


def test_flow_waiting_for_lock_only_emitted_when_true():
    """waiting_for_lock stays out of the header unless actually waiting."""
    flow = _make_flow()
    assert "waiting_for_lock" not in flow.to_header_dict()
    flow.waiting_for_lock = True
    header = flow.to_header_dict()
    assert header["waiting_for_lock"] is True
    assert FlowInstance.from_dict(header).waiting_for_lock is True
