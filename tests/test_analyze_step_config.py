"""Tests for analyze._update_flow_steps preserving steps.append config.

Regression test for Bug 1: analyze's _update_flow_steps rebuilt the step
sequence from the default and dropped steps appended via se3.yaml's
``steps.append`` (e.g. summarize). The fix re-applies apply_step_config
after the rebuild, mirroring state_machine.create_flow's ordering.
"""

from __future__ import annotations

import yaml

from se3.engine.models import FlowInstance, FlowStatus, StepType
from se3.engine.steps.analyze import _update_flow_steps


def _make_flow(project_root) -> FlowInstance:
    """Build a flow whose change_path.parent resolves to project_root."""
    flow = FlowInstance(
        task_description="do something",
        task_type="feature",
        status=FlowStatus.INIT,
    )
    # _update_flow_steps derives project_root from change_path.parent
    flow.change_path = project_root / "change"
    return flow


def test_update_flow_steps_keeps_appended_summarize(tmp_path):
    """summarize configured via steps.append survives the analyze rebuild."""
    (tmp_path / "se3.yaml").write_text(
        yaml.dump({"steps": {"append": ["summarize"]}})
    )

    flow = _make_flow(tmp_path)
    _update_flow_steps(flow, "feature")

    steps = flow.state.selected_steps
    assert StepType.SUMMARIZE in steps, "summarize must survive _update_flow_steps"
    assert steps[-1] == StepType.SUMMARIZE, "summarize must be appended at the end"


def test_update_flow_steps_no_duplicate_append(tmp_path):
    """The appended step is added exactly once, not duplicated."""
    (tmp_path / "se3.yaml").write_text(
        yaml.dump({"steps": {"append": ["summarize"]}})
    )

    flow = _make_flow(tmp_path)
    _update_flow_steps(flow, "feature")

    steps = flow.state.selected_steps
    assert steps.count(StepType.SUMMARIZE) == 1, "summarize must not be duplicated"


def test_update_flow_steps_summarize_present_without_config(tmp_path):
    """summarize is a default step, so it survives the analyze rebuild at the
    end even with no steps.append config."""
    flow = _make_flow(tmp_path)
    _update_flow_steps(flow, "feature")

    steps = flow.state.selected_steps
    assert StepType.SUMMARIZE in steps
    assert steps[-1] == StepType.SUMMARIZE
    assert steps.count(StepType.SUMMARIZE) == 1


def test_update_flow_steps_append_summarize_noop(tmp_path):
    """With summarize now a default step, configuring steps.append: [summarize]
    is a no-op: it stays a single entry at the end after the analyze rebuild."""
    (tmp_path / "se3.yaml").write_text(
        yaml.dump({"steps": {"append": ["summarize"]}})
    )

    flow = _make_flow(tmp_path)
    _update_flow_steps(flow, "feature")

    steps = flow.state.selected_steps
    assert steps.count(StepType.SUMMARIZE) == 1
    assert steps[-1] == StepType.SUMMARIZE
