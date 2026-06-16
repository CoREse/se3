"""Tests for the hard fallback layer: within-flow spec-diff step lifecycle guard.

The second hard layer of the spec-write protection lives in
``StateMachine.run_step``: for every step NOT in the shared exemption set
``context_builder.SPEC_WRITE_ALLOWED_STEPS`` (``update_spec`` + all sync steps),
the state machine snapshots ``se3/specs/**`` content hashes before the handler
runs and diffs them afterwards. A change — most notably one a ``Bash`` redirect
slipped past the PreToolUse hook — fails the step. The guard only asks "did this
step touch a spec file at all"; it is wholly orthogonal to verify_spec's
in_scope/out_of_scope judgement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.context_builder import SPEC_WRITE_ALLOWED_STEPS
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


def _make_project(tmp_path: Path, se3_yaml: str | None = None) -> Path:
    """Lay down a minimal se3 project with one base spec."""
    specs_dir = tmp_path / "se3" / "specs" / "base"
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / "spec.md").write_text(
        "<!-- spec-format: v1 -->\n\n# Base\n\n## Purpose\n\nTest.\n",
        encoding="utf-8",
    )
    if se3_yaml is not None:
        (tmp_path / "se3.yaml").write_text(se3_yaml, encoding="utf-8")
    return tmp_path


def _make_flow(tmp_path: Path) -> FlowInstance:
    return FlowInstance(
        flow_id="test-flow-diff-guard",
        task_description="Test task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )


def _spec_writing_handler(text: str):
    """Return a handler that simulates a Bash-bypass spec write and succeeds."""

    def handler(step: Step, flow: FlowInstance):
        # Direct filesystem write simulates a `Bash` redirect / sed / tee that
        # the PreToolUse tool-matcher hook (Write|Edit|NotebookEdit) never sees.
        spec = Path(flow.state.context.get("project_root", "")) / "se3" / "specs" / "base" / "spec.md"
        if not spec.parent.exists():
            spec = step.inputs["__spec_path__"]
        spec.write_text(text, encoding="utf-8")
        return StepStatus.COMPLETED

    return handler


def _noop_handler(step: Step, flow: FlowInstance):
    """A handler that touches no spec file and succeeds."""
    return StepStatus.COMPLETED


def _run(sm: StateMachine, flow: FlowInstance, step_type: StepType, handler) -> Step:
    # Avoid IMPLEMENT/UPDATE_SPEC pre-handler machinery interfering with the test.
    sm._ensure_baseline_ready = lambda f: None  # type: ignore[assignment]
    sm._snapshot_specs_before_update = lambda f: None  # type: ignore[assignment]
    sm.register_handler(step_type, handler)
    step = Step(step_type=step_type)
    spec_path = sm.project_root / "se3" / "specs" / "base" / "spec.md"
    step.inputs["__spec_path__"] = spec_path
    flow.state.context["project_root"] = str(sm.project_root)
    sm.run_step(flow, step)
    return step


class TestSpecDiffGuardEnabled:
    """Unit tests for the exemption + config gate."""

    def test_update_spec_exempt(self, tmp_path):
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        step = Step(step_type=StepType.UPDATE_SPEC)
        assert sm._spec_diff_guard_enabled(step) is False

    def test_implement_guarded_by_default(self, tmp_path):
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        step = Step(step_type=StepType.IMPLEMENT)
        assert sm._spec_diff_guard_enabled(step) is True

    def test_disabled_via_config(self, tmp_path):
        _make_project(
            tmp_path,
            se3_yaml="spec_write_protection:\n  diff_fallback_enabled: false\n",
        )
        sm = StateMachine(tmp_path)
        step = Step(step_type=StepType.IMPLEMENT)
        assert sm._spec_diff_guard_enabled(step) is False

    def test_exemption_uses_shared_constant_not_literal(self, tmp_path):
        # The guard must route exemptions through SPEC_WRITE_ALLOWED_STEPS — so
        # update_spec AND all sync steps are exempt — never a bare != UPDATE_SPEC.
        assert "update_spec" in SPEC_WRITE_ALLOWED_STEPS
        for sync_step in ("sync_scan", "sync_analyze", "sync_resolve", "sync_respond"):
            assert sync_step in SPEC_WRITE_ALLOWED_STEPS


class TestRunStepDiffGuard:
    def test_bash_bypass_write_fails_implement(self, tmp_path):
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _run(sm, flow, StepType.IMPLEMENT, _spec_writing_handler("CHANGED"))

        assert step.status == StepStatus.FAILED
        assert "se3/specs/base/spec.md" in step.error_message
        assert "update_spec" in step.error_message

    def test_update_spec_write_not_flagged(self, tmp_path):
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _run(sm, flow, StepType.UPDATE_SPEC, _spec_writing_handler("LEGIT UPDATE"))

        # update_spec is exempt; a legitimate spec write keeps it COMPLETED.
        assert step.status == StepStatus.COMPLETED

    def test_non_writing_step_passes(self, tmp_path):
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _run(sm, flow, StepType.ANALYZE, _noop_handler)

        assert step.status == StepStatus.COMPLETED

    def test_spec_gate_not_flagged_when_no_spec_write(self, tmp_path):
        # spec_gate is read_only:False and not exempt, but it does not write
        # specs — so a clean spec_gate run must pass the diff guard.
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _run(sm, flow, StepType.SPEC_GATE, _noop_handler)

        assert step.status == StepStatus.COMPLETED

    def test_disabled_config_does_not_flag(self, tmp_path):
        _make_project(
            tmp_path,
            se3_yaml="spec_write_protection:\n  diff_fallback_enabled: false\n",
        )
        sm = StateMachine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _run(sm, flow, StepType.IMPLEMENT, _spec_writing_handler("CHANGED"))

        # With the fallback disabled, the diff guard never runs.
        assert step.status == StepStatus.COMPLETED

    def test_handler_failure_keeps_original_error(self, tmp_path):
        # A handler that both writes a spec AND raises keeps its own error
        # (the diff guard only overrides a non-FAILED status).
        _make_project(tmp_path)
        sm = StateMachine(tmp_path)
        flow = _make_flow(tmp_path)

        def failing_handler(step: Step, flow: FlowInstance):
            spec = sm.project_root / "se3" / "specs" / "base" / "spec.md"
            spec.write_text("CHANGED", encoding="utf-8")
            raise RuntimeError("handler boom")

        step = _run(sm, flow, StepType.IMPLEMENT, failing_handler)
        assert step.status == StepStatus.FAILED
        assert "handler boom" in step.error_message
