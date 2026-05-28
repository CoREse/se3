"""Tests for the run-loop interjection drain + PAUSED prefix consumer.

Covers G2's behavior contract in ``src/se3/commands/run.py``:

* :func:`_drain_pending_interjections` writes a ``record_user_interjection``
  entry to the per-step jsonl when the current step has a step_id;
* it buffers drained texts into
  ``flow.state.context['_pending_paused_interjections']`` only when the
  current step's status is PAUSED (non-PAUSED steps do not accumulate stale
  buffer);
* :func:`_consume_paused_interjection_prefix` joins buffered entries into a
  ``[interjection: ...]\\n`` prefix and clears the buffer once consumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from se3.commands import run as run_mod
from se3.engine import interaction_calls
from se3.engine.models import StepStatus, StepType


class _RecordingPersistence:
    def __init__(self):
        self.saved = 0

    def save_flow(self, flow):  # noqa: D401 - test stub
        self.saved += 1


def _make_state():
    return SimpleNamespace(
        context={},
        step_history=[],
        steps={},
        get_current_step=lambda: None,
    )


def _make_flow(flow_id="flow-G2", step=None):
    state = _make_state()
    if step is not None:
        state.get_current_step = lambda: step
    return SimpleNamespace(
        flow_id=flow_id,
        state=state,
        task_description="base task",
    )


def _make_step(*, step_id="01_discovery_abc", step_type=StepType.DISCOVERY,
               status=StepStatus.PAUSED, retry_count=0):
    return SimpleNamespace(
        step_id=step_id,
        step_type=step_type,
        status=status,
        inputs={"retry_count": retry_count, "task_description": "base task"},
        outputs={},
    )


# --------------------------------------------------------------------------
# _drain_pending_interjections
# --------------------------------------------------------------------------


def test_drain_buffers_only_for_paused_step(tmp_path: Path):
    """Buffered prefix list grows only when current step is PAUSED."""
    step = _make_step(status=StepStatus.PAUSED)
    flow = _make_flow(step=step)
    persistence = _RecordingPersistence()

    interaction_calls.write_interjection_request(
        tmp_path / "se3" / "calls", "hello A", flow_id=flow.flow_id, call_id="iA"
    )
    drained = run_mod._drain_pending_interjections(flow, tmp_path, persistence)
    assert drained == ["hello A"]
    assert flow.state.context["_pending_paused_interjections"] == ["hello A"]
    # The user_interjections persisted-list also has a matching entry.
    items = flow.state.context["user_interjections"]
    assert len(items) == 1
    assert items[0]["text"] == "hello A"
    assert items[0]["source"] == "web-console"


def test_drain_does_not_buffer_for_running_step(tmp_path: Path):
    """A non-PAUSED step's drain does NOT add to the prefix buffer."""
    step = _make_step(status=StepStatus.PENDING)
    flow = _make_flow(step=step)
    persistence = _RecordingPersistence()

    interaction_calls.write_interjection_request(
        tmp_path / "se3" / "calls", "running", flow_id=flow.flow_id, call_id="iR"
    )
    drained = run_mod._drain_pending_interjections(flow, tmp_path, persistence)
    assert drained == ["running"]
    # Buffer is still empty (or the key absent).
    assert not flow.state.context.get("_pending_paused_interjections")


def test_drain_does_not_buffer_for_confirm_paused_step(tmp_path: Path):
    """CONFIRM-paused steps do NOT populate the prefix buffer.

    The buffer is consumed only by discovery reply paths. Populating it during
    a CONFIRM pause would leak stale interjections into a later DISCOVERY pause's
    LLM call. Interjections during CONFIRM pauses still reach the LLM via the
    user_interjections list + task_description recomposition.
    """
    step = _make_step(
        step_id="02_confirm_abc",
        step_type=StepType.CONFIRM,
        status=StepStatus.PAUSED,
    )
    flow = _make_flow(step=step)
    persistence = _RecordingPersistence()

    interaction_calls.write_interjection_request(
        tmp_path / "se3" / "calls", "confirm interject", flow_id=flow.flow_id, call_id="iC"
    )
    drained = run_mod._drain_pending_interjections(flow, tmp_path, persistence)
    assert drained == ["confirm interject"]
    # Prefix buffer MUST NOT be populated for CONFIRM-paused steps.
    assert not flow.state.context.get("_pending_paused_interjections")
    # user_interjections list is still populated (normal task_description path).
    items = flow.state.context["user_interjections"]
    assert len(items) == 1
    assert items[0]["text"] == "confirm interject"
    assert items[0]["source"] == "web-console"


def test_drain_writes_history_jsonl(tmp_path: Path):
    """Each drained interjection lands as a user/interjection jsonl line."""
    step = _make_step(
        step_id="01_discovery_xyz",
        step_type=StepType.DISCOVERY,
        status=StepStatus.PAUSED,
    )
    flow = _make_flow(step=step)
    persistence = _RecordingPersistence()

    interaction_calls.write_interjection_request(
        tmp_path / "se3" / "calls", "history bubble", flow_id=flow.flow_id, call_id="iH"
    )
    run_mod._drain_pending_interjections(flow, tmp_path, persistence)

    jsonl = tmp_path / "se3" / "history" / flow.flow_id / "01_discovery_xyz.jsonl"
    assert jsonl.exists()
    lines = [
        json.loads(line)
        for line in jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        line.get("kind") == "interjection"
        and line.get("role") == "user"
        and line.get("content") == "history bubble"
        for line in lines
    )


def test_drain_with_no_calls_is_noop(tmp_path: Path):
    """No call files → return empty list, no side effects."""
    flow = _make_flow()
    drained = run_mod._drain_pending_interjections(
        flow, tmp_path, _RecordingPersistence()
    )
    assert drained == []
    assert "user_interjections" not in flow.state.context


# --------------------------------------------------------------------------
# _consume_paused_interjection_prefix
# --------------------------------------------------------------------------


def test_consume_prefix_joins_and_clears_buffer():
    flow = _make_flow()
    flow.state.context["_pending_paused_interjections"] = ["one", "two"]
    prefix = run_mod._consume_paused_interjection_prefix(flow)
    assert prefix == "[interjection: one]\n[interjection: two]\n"
    assert flow.state.context["_pending_paused_interjections"] == []


def test_consume_prefix_empty_returns_empty():
    flow = _make_flow()
    assert run_mod._consume_paused_interjection_prefix(flow) == ""


def test_consume_prefix_tolerates_missing_state():
    """A flow stub without ``.state`` does not crash the consumer."""
    flow = SimpleNamespace(flow_id="x")  # no .state
    assert run_mod._consume_paused_interjection_prefix(flow) == ""


def test_consume_prefix_skips_whitespace_entries():
    flow = _make_flow()
    flow.state.context["_pending_paused_interjections"] = ["   ", "real"]
    prefix = run_mod._consume_paused_interjection_prefix(flow)
    assert prefix == "[interjection: real]\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
