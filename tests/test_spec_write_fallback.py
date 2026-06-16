"""G5 task 3 — the hard fallback layer (post-step spec-diff guard).

The PreToolUse hook (the primary hard layer) only matches Write/Edit/NotebookEdit,
so a step that writes ``se3/specs/**`` via a ``Bash`` redirect / ``sed`` / ``tee``
slips past it. ``StateMachine.run_step`` therefore snapshots every spec file's
content hash before a non-exempt step and diffs after the handler returns,
failing the step when any spec file changed.

These tests drive ``run_step`` with synthetic handlers that mutate spec files
directly (standing in for a Bash-bypass write) and assert:

* a non-exempt step (``implement``) that writes a spec is set to FAILED with the
  changed file named in ``error_message``;
* ``update_spec`` writing a spec is exempt (NOT flagged);
* every sync step (incl. ``sync_respond``) is exempt at the guard-decision level
  via the shared ``SPEC_WRITE_ALLOWED_STEPS`` set;
* the guard is off when ``spec_write_protection.diff_fallback_enabled`` is false;
* a handler that fails for an unrelated reason keeps its own error (the guard
  only overrides a non-FAILED status).
"""

from __future__ import annotations

from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path):
    """Lay down a minimal project with one committed spec file."""
    spec = tmp_path / "se3" / "specs" / "base" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# base Specification\n\n## Purpose\nx\n", encoding="utf-8")
    return spec


def _make_machine(tmp_path):
    return StateMachine(project_root=tmp_path)


def _make_flow(tmp_path):
    flow = FlowInstance(
        flow_id="test-fallback",
        task_description="t",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "se3" / "changes" / "test",
    )
    # Pre-seed the baseline so run_step's IMPLEMENT pre-hook
    # (_ensure_baseline_ready) is a no-op and never runs the test suite.
    flow.state.baseline_failures = []
    return flow


def _step(step_type):
    return Step(step_type=step_type, status=StepStatus.PENDING, inputs={})


# ---------------------------------------------------------------------------
# Non-exempt step writing a spec via "Bash" is caught and failed
# ---------------------------------------------------------------------------

class TestBashBypassCaught:
    def test_implement_writing_spec_is_failed(self, tmp_path):
        spec = _make_project(tmp_path)
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.IMPLEMENT)

        def handler(_step, _flow):
            # Stand-in for a Bash redirect/sed/tee that the tool-matcher hook
            # never sees: mutate the spec file directly during the step.
            spec.write_text("# base Specification\n\n## Purpose\nTAMPERED\n", encoding="utf-8")
            return StepStatus.COMPLETED

        machine.register_handler(StepType.IMPLEMENT, handler)
        status = machine.run_step(flow, step)

        assert status == StepStatus.FAILED
        assert "se3/specs/base/spec.md" in (step.error_message or "")
        # The illegal write must be reverted on disk, not just flagged — else it
        # survives a later `se3 run --resume`.
        assert spec.read_text(encoding="utf-8") == (
            "# base Specification\n\n## Purpose\nx\n"
        )
        assert "reverted" in (step.error_message or "")

    def test_error_message_explains_channel(self, tmp_path):
        spec = _make_project(tmp_path)
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.IMPLEMENT)

        def handler(_step, _flow):
            spec.write_text("changed", encoding="utf-8")
            return StepStatus.COMPLETED

        machine.register_handler(StepType.IMPLEMENT, handler)
        machine.run_step(flow, step)

        assert step.status == StepStatus.FAILED
        # The guidance must point at update_spec / spec_changes, not forbid
        # behavior change.
        assert "update_spec" in step.error_message
        assert "spec_changes" in step.error_message

    def test_creating_new_spec_file_is_failed(self, tmp_path):
        _make_project(tmp_path)
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.IMPLEMENT)

        new_spec = tmp_path / "se3" / "specs" / "new" / "spec.md"

        def handler(_step, _flow):
            new_spec.parent.mkdir(parents=True, exist_ok=True)
            new_spec.write_text("# new Specification\n", encoding="utf-8")
            return StepStatus.COMPLETED

        machine.register_handler(StepType.IMPLEMENT, handler)
        machine.run_step(flow, step)

        assert step.status == StepStatus.FAILED
        assert "se3/specs/new/spec.md" in step.error_message
        # The newly-created illegal spec file must be removed, not left on disk.
        assert not new_spec.exists()

    def test_non_writing_step_passes(self, tmp_path):
        _make_project(tmp_path)
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.IMPLEMENT)

        def handler(_step, _flow):
            # Touch a non-spec file only — must not be flagged.
            (tmp_path / "src.py").write_text("print('hi')", encoding="utf-8")
            return StepStatus.COMPLETED

        machine.register_handler(StepType.IMPLEMENT, handler)
        status = machine.run_step(flow, step)
        assert status == StepStatus.COMPLETED


# ---------------------------------------------------------------------------
# update_spec is exempt — its legitimate spec write is never flagged
# ---------------------------------------------------------------------------

class TestUpdateSpecExempt:
    def test_update_spec_writing_spec_not_flagged(self, tmp_path):
        spec = _make_project(tmp_path)
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.UPDATE_SPEC)

        def handler(_step, _flow):
            spec.write_text("# base Specification\n\n## Purpose\nupdated\n", encoding="utf-8")
            return StepStatus.COMPLETED

        machine.register_handler(StepType.UPDATE_SPEC, handler)
        status = machine.run_step(flow, step)

        assert status == StepStatus.COMPLETED
        assert step.status == StepStatus.COMPLETED


# ---------------------------------------------------------------------------
# Guard-decision exemption for every sync step (incl. sync_respond)
# ---------------------------------------------------------------------------

class TestGuardDecisionExemption:
    """Sync pseudo-steps never reach run_step (they are not StepType members),
    so the exemption is asserted at the decision helper ``_spec_diff_guard_enabled``,
    which keys off ``step.step_type.value in SPEC_WRITE_ALLOWED_STEPS``."""

    @pytest.mark.parametrize(
        "value",
        ["update_spec", "sync_scan", "sync_analyze", "sync_resolve", "sync_respond"],
    )
    def test_exempt_values_disable_guard(self, tmp_path, value):
        assert value in SPEC_WRITE_ALLOWED_STEPS
        machine = _make_machine(tmp_path)
        fake_step = SimpleNamespace(step_type=SimpleNamespace(value=value))
        assert machine._spec_diff_guard_enabled(fake_step) is False

    @pytest.mark.parametrize("value", ["implement", "plan_tasks", "test", "commit"])
    def test_non_exempt_values_enable_guard(self, tmp_path, value):
        machine = _make_machine(tmp_path)
        fake_step = SimpleNamespace(step_type=SimpleNamespace(value=value))
        assert machine._spec_diff_guard_enabled(fake_step) is True


# ---------------------------------------------------------------------------
# Config toggle — diff_fallback_enabled: false disables the guard
# ---------------------------------------------------------------------------

class TestConfigDisable:
    def test_disabled_guard_does_not_flag_bash_write(self, tmp_path):
        spec = _make_project(tmp_path)
        (tmp_path / "se3.yaml").write_text(
            "spec_write_protection:\n  diff_fallback_enabled: false\n",
            encoding="utf-8",
        )
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.IMPLEMENT)

        def handler(_step, _flow):
            spec.write_text("tampered-but-allowed-by-config", encoding="utf-8")
            return StepStatus.COMPLETED

        machine.register_handler(StepType.IMPLEMENT, handler)
        status = machine.run_step(flow, step)
        assert status == StepStatus.COMPLETED

    def test_decision_helper_off_when_disabled(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "spec_write_protection:\n  diff_fallback_enabled: false\n",
            encoding="utf-8",
        )
        machine = _make_machine(tmp_path)
        fake_step = SimpleNamespace(step_type=SimpleNamespace(value="implement"))
        assert machine._spec_diff_guard_enabled(fake_step) is False


# ---------------------------------------------------------------------------
# A handler that fails for its own reason keeps its own error
# ---------------------------------------------------------------------------

class TestHandlerErrorPreserved:
    def test_handler_exception_error_not_overwritten(self, tmp_path):
        spec = _make_project(tmp_path)
        machine = _make_machine(tmp_path)
        flow = _make_flow(tmp_path)
        step = _step(StepType.IMPLEMENT)

        def handler(_step, _flow):
            # Even if it also touched a spec, an already-FAILED step keeps its
            # own error (the guard only overrides a non-FAILED status).
            spec.write_text("side-effect", encoding="utf-8")
            raise RuntimeError("boom from handler")

        machine.register_handler(StepType.IMPLEMENT, handler)
        status = machine.run_step(flow, step)

        assert status == StepStatus.FAILED
        assert "boom from handler" in step.error_message
        assert "illegally modified" not in (step.error_message or "")
