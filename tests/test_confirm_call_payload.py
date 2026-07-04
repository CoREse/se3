"""Tests for the enriched CONFIRM call-file payload (group G1).

The confirm gate writes a call file the web console renders into Approve/Reject
buttons (plus, for an adjudicate ruling, a before/after diff). These tests lock:

* every confirm call carries ``kind='confirm'`` and a non-empty human-readable
  ``prompt`` — the discriminant the daemon aggregator / web console dispatch on;
* a NON-adjudicate confirm call carries none of the adjudicate-only context
  fields (so a plain plan confirm stays lean);
* an adjudicate confirm call embeds ``adjudication_rationale`` /
  ``adjudicated_description`` (post-ruling) / ``baseline`` (pre-ruling), and the
  multi-round direction is correct — ``baseline`` is the PRIOR round's
  description and ``adjudicated_description`` is THIS round's, never flipped;
* the enriched file is still consumable by ``run.py:_check_confirm_response``.
"""

import json
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.daemon.protocol import CALL_KIND_CONFIRM
from se3.engine.models import (
    FlowInstance,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.steps.confirm import _create_call_file


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def _make_flow(project_root: Path, task_description: str = "Original task") -> FlowInstance:
    """A flow whose ``change_path.parent`` resolves back to *project_root*."""
    return FlowInstance(
        task_description=task_description,
        task_type="feature",
        change_name="test-change",
        change_path=project_root / "test-change",
    )


def _add_confirm_step(
    flow: FlowInstance, *, step_to_review_id: str, step_to_review_type: str
) -> Step:
    confirm_step = Step(
        step_type=StepType.CONFIRM,
        status=StepStatus.PENDING,
        step_id="confirm-001",
        inputs={
            "step_to_review_id": step_to_review_id,
            "step_to_review_type": step_to_review_type,
            "reviewer": "human",
        },
    )
    flow.state.add_step(confirm_step)
    flow.state.current_step_id = confirm_step.step_id
    return confirm_step


def _add_adjudicate_step(
    flow: FlowInstance,
    step_id: str,
    *,
    description: str,
    rationale: str,
) -> Step:
    adj = Step(
        step_type=StepType.ADJUDICATE,
        status=StepStatus.COMPLETED,
        step_id=step_id,
    )
    adj.outputs["adjudicated_description"] = description
    adj.outputs["adjudication_rationale"] = rationale
    flow.state.add_step(adj)
    return adj


@pytest.fixture
def tmp_project():
    d = Path(tempfile.mkdtemp())
    (d / "se3" / "calls").mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _read_call(call_file: Path) -> dict:
    return json.loads(call_file.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Non-adjudicate confirm payload
# --------------------------------------------------------------------------- #


def test_plain_confirm_payload_has_kind_and_prompt(tmp_project):
    flow = _make_flow(tmp_project)
    plan_step = Step(step_type=StepType.PLAN, status=StepStatus.COMPLETED, step_id="plan-001")
    flow.state.add_step(plan_step)
    confirm_step = _add_confirm_step(
        flow, step_to_review_id="plan-001", step_to_review_type="plan"
    )

    call_file = _create_call_file(confirm_step, flow, tmp_project)
    data = _read_call(call_file)

    assert data["kind"] == CALL_KIND_CONFIRM
    assert data["kind"] == "confirm"
    assert isinstance(data["prompt"], str) and data["prompt"].strip()
    # Legacy discriminant preserved for backward compatibility.
    assert data["type"] == "confirm"

    ctx = data["context"]
    assert ctx["step_to_review_type"] == "plan"
    assert ctx["step_to_review_id"] == "plan-001"
    # A non-adjudicate confirm carries NONE of the adjudicate-only fields.
    for adj_only in ("adjudication_rationale", "adjudicated_description", "baseline"):
        assert adj_only not in ctx


def test_plain_confirm_payload_matches_by_step(tmp_project):
    """The ``step`` key the resume/consumer path matches on is still present."""
    flow = _make_flow(tmp_project)
    plan_step = Step(step_type=StepType.PLAN, status=StepStatus.COMPLETED, step_id="plan-001")
    flow.state.add_step(plan_step)
    confirm_step = _add_confirm_step(
        flow, step_to_review_id="plan-001", step_to_review_type="plan"
    )

    call_file = _create_call_file(confirm_step, flow, tmp_project)
    data = _read_call(call_file)
    assert data["step"] == confirm_step.step_id


# --------------------------------------------------------------------------- #
# Adjudicate confirm payload
# --------------------------------------------------------------------------- #


def test_adjudicate_confirm_first_round_baseline_falls_back_to_original(tmp_project):
    """With no prior ruling, baseline is the original/refined description."""
    flow = _make_flow(tmp_project, task_description="ORIGINAL TASK")
    adj = _add_adjudicate_step(
        flow, "adj-001", description="ROUND1 DESC", rationale="ruling one"
    )
    confirm_step = _add_confirm_step(
        flow, step_to_review_id=adj.step_id, step_to_review_type="adjudicate"
    )

    call_file = _create_call_file(confirm_step, flow, tmp_project)
    ctx = _read_call(call_file)["context"]

    assert ctx["adjudication_rationale"] == "ruling one"
    assert ctx["adjudicated_description"] == "ROUND1 DESC"
    # No prior adjudication and no discovery refinement → the raw original.
    assert ctx["baseline"] == "ORIGINAL TASK"
    # baseline (pre-ruling) must differ from the post-ruling description.
    assert ctx["baseline"] != ctx["adjudicated_description"]


def test_adjudicate_confirm_multi_round_baseline_is_prior_ruling(tmp_project):
    """Two-round adjudication: baseline == prior ruling, description == this one.

    Locks the ``exclude_step_id`` direction so the diff anchor is never flipped.
    """
    flow = _make_flow(tmp_project, task_description="ORIGINAL TASK")
    _add_adjudicate_step(flow, "adj-001", description="ROUND1 DESC", rationale="r1")
    adj2 = _add_adjudicate_step(
        flow, "adj-002", description="ROUND2 DESC", rationale="r2"
    )
    confirm_step = _add_confirm_step(
        flow, step_to_review_id=adj2.step_id, step_to_review_type="adjudicate"
    )

    call_file = _create_call_file(confirm_step, flow, tmp_project)
    ctx = _read_call(call_file)["context"]

    assert ctx["adjudicated_description"] == "ROUND2 DESC"
    assert ctx["adjudication_rationale"] == "r2"
    # Baseline is the round-1 ruling (the reviewed round-2 step is excluded),
    # NOT round 2's own rewrite and NOT the pristine original.
    assert ctx["baseline"] == "ROUND1 DESC"
    assert ctx["baseline"] != ctx["adjudicated_description"]


def test_adjudicate_confirm_prompt_mentions_ruling(tmp_project):
    flow = _make_flow(tmp_project)
    adj = _add_adjudicate_step(
        flow, "adj-001", description="D", rationale="R"
    )
    confirm_step = _add_confirm_step(
        flow, step_to_review_id=adj.step_id, step_to_review_type="adjudicate"
    )
    data = _read_call(_create_call_file(confirm_step, flow, tmp_project))
    assert "adjudicat" in data["prompt"].lower()


# --------------------------------------------------------------------------- #
# Backward-compatible consumption by run.py
# --------------------------------------------------------------------------- #


def test_enriched_call_file_still_consumable_by_check_confirm_response(tmp_project):
    """The richer payload does not break ``_check_confirm_response`` unwrap.

    A structured ``{approved, feedback}`` response (the new front-end shape) as
    well as the daemon's ``{"response": {...}}`` envelope must both land a
    ``review_result`` and drive COMPLETED / REVISION_NEEDED.
    """
    from se3.commands.run import _check_confirm_response

    flow = _make_flow(tmp_project)
    adj = _add_adjudicate_step(flow, "adj-001", description="D", rationale="R")
    confirm_step = _add_confirm_step(
        flow, step_to_review_id=adj.step_id, step_to_review_type="adjudicate"
    )
    call_file = _create_call_file(confirm_step, flow, tmp_project)

    # Structured approval wrapped in the daemon envelope (front-end sends
    # ``{response: {approved, feedback}}``).
    response_path = call_file.parent / f"{call_file.stem}.response.json"
    response_path.write_text(
        json.dumps({"call_id": "x", "response": {"approved": True, "feedback": "ok"}}),
        encoding="utf-8",
    )

    status = _check_confirm_response(flow, confirm_step, tmp_project)
    assert status == StepStatus.COMPLETED
    rr = confirm_step.outputs["review_result"]
    assert rr["approved"] is True
    assert rr["feedback"] == "ok"


def test_enriched_call_file_structured_rejection_routes_to_revision(tmp_project):
    from se3.commands.run import _check_confirm_response

    flow = _make_flow(tmp_project)
    plan_step = Step(step_type=StepType.PLAN, status=StepStatus.COMPLETED, step_id="plan-001")
    flow.state.add_step(plan_step)
    confirm_step = _add_confirm_step(
        flow, step_to_review_id="plan-001", step_to_review_type="plan"
    )
    call_file = _create_call_file(confirm_step, flow, tmp_project)

    response_path = call_file.parent / f"{call_file.stem}.response.json"
    response_path.write_text(
        json.dumps({"response": {"approved": False, "feedback": "redo the plan"}}),
        encoding="utf-8",
    )

    status = _check_confirm_response(flow, confirm_step, tmp_project)
    assert status == StepStatus.REVISION_NEEDED
    assert confirm_step.outputs["review_result"]["feedback"] == "redo the plan"
