"""Regression coverage for task_groups' non-authoritative SELF_CHECK role."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from tianluo.engine.steps.self_check import (
    _build_source_pool,
    _validate_and_filter_issues,
    self_check_handler,
)


@pytest.fixture
def flow(tmp_path):
    return FlowInstance(
        flow_id="test-flow-tg",
        task_description="Implement feature X",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "tg",
    )


def _groups():
    return [
        {
            "group_id": "G1",
            "name": "Historical schedule",
            "tasks": [
                {
                    "id": 1,
                    "description": "Plan-only requirement must never expand scope",
                    "acceptance_criteria": ["Plan-only acceptance criterion"],
                }
            ],
        }
    ]


def _step():
    return Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Implement feature X",
            "task_description_base": "Implement feature X",
            "changes_made": {"files_changed": ["src/feature.py"]},
            "test_results": {"passed": True, "returncode": 0, "stdout": "ok"},
            "task_groups": _groups(),
            "implement_summary": "Implementation-only claim must not be authority",
        },
    )


def _capture_prompt(step, flow):
    response = json.dumps({"issues": [], "summary": "ok"})
    with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
        caller = Mock()
        caller.call.return_value = response
        mock_cls.return_value = caller
        status = self_check_handler(step, flow)
    assert status == StepStatus.COMPLETED
    return caller.call.call_args.kwargs["prompt"]


def test_task_groups_and_implementation_summary_are_absent_from_prompt(flow):
    prompt = _capture_prompt(_step(), flow)
    assert "## Plan Task Groups" not in prompt
    assert "Plan-only requirement must never expand scope" not in prompt
    assert "Plan-only acceptance criterion" not in prompt
    assert "Implementation-only claim must not be authority" not in prompt
    assert "only navigation clues" in prompt


def test_task_groups_do_not_enter_requirement_source_pool():
    inputs = _step().inputs
    pool = _build_source_pool(inputs)
    assert pool == ["Implement feature X"]
    assert not any("Plan-only" in item for item in pool)


def test_new_plan_task_finding_is_rejected_even_when_quote_matches_plan():
    issue = {
        "severity": "high",
        "location": "src/feature.py:4",
        "actual_behavior": "the plan-only behavior is absent",
        "expected_behavior": "the plan-only behavior exists",
        "divergence": "a plan-only input produces no result",
        "expectation_source": {
            "type": "plan_task",
            "verbatim_quote": "Plan-only requirement must never expand scope",
        },
        "evidence_lines": ["src/feature.py:4"],
        "missing_in": [],
    }
    kept, stats = _validate_and_filter_issues([issue], _step().inputs)
    assert kept == []
    assert stats["unsupported_source_type_count"] == 1


def test_legacy_plan_task_finding_shape_remains_serializable_and_displayable():
    issue = {
        "severity": "medium",
        "location": "src/legacy.py:8",
        "description": "legacy finding",
        "expectation_source": {"type": "plan_task", "verbatim_quote": "old task"},
    }
    encoded = json.dumps({"issues": [issue], "adjudicated_plan": _groups()})
    restored = json.loads(encoded)
    assert restored["issues"][0]["expectation_source"]["type"] == "plan_task"
    assert restored["adjudicated_plan"] == _groups()
