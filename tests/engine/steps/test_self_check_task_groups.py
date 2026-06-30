"""Tests for self_check task_groups injection.

Covers the `_format_task_groups` helper and the handler-level behavior of
injecting the Plan Task Groups (Authoritative Task List — HARD AUDIT) section
into the LLM prompt when `task_groups` is present in step.inputs.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine.steps.self_check import (
    _TASK_GROUPS_SECTION_INTRO,
    _build_task_groups_section,
    _format_task_groups,
    self_check_handler,
)
from se3.engine.truncation import SELF_CHECK_TASK_GROUPS_MAX_CHARS


# ---------------------------------------------------------------------------
# _format_task_groups — branch coverage
# ---------------------------------------------------------------------------


class TestFormatTaskGroups:
    def test_none_returns_empty(self):
        assert _format_task_groups(None) == ""

    def test_empty_list_returns_empty(self):
        assert _format_task_groups([]) == ""

    def test_non_list_returns_empty(self):
        assert _format_task_groups({"not": "a list"}) == ""
        assert _format_task_groups("string") == ""
        assert _format_task_groups(42) == ""

    def test_list_with_non_dict_entries_returns_empty(self):
        assert _format_task_groups(["str", 1, None]) == ""

    def test_typical_input_renders_markdown(self):
        task_groups = [
            {
                "group_id": "G1",
                "name": "Auth flow",
                "tasks": [
                    {
                        "id": 1,
                        "description": "Add login endpoint",
                        "acceptance_criteria": [
                            "Returns 200 on valid creds",
                            "Returns 401 otherwise",
                        ],
                    },
                    {
                        "id": 2,
                        "description": "Add session table",
                        "acceptance_criteria": ["Migration applies cleanly"],
                    },
                ],
            },
        ]
        out = _format_task_groups(task_groups)
        assert "### G1 — Auth flow" in out
        assert "- [1] Add login endpoint" in out
        assert "  - AC: Returns 200 on valid creds" in out
        assert "  - AC: Returns 401 otherwise" in out
        assert "- [2] Add session table" in out
        assert "  - AC: Migration applies cleanly" in out

    def test_multiple_groups_all_rendered(self):
        task_groups = [
            {"group_id": "G1", "name": "First", "tasks": [{"id": 1, "description": "task one"}]},
            {"group_id": "G2", "name": "Second", "tasks": [{"id": 2, "description": "task two"}]},
        ]
        out = _format_task_groups(task_groups)
        assert "### G1 — First" in out
        assert "### G2 — Second" in out
        assert "- [1] task one" in out
        assert "- [2] task two" in out

    def test_group_without_tasks_shows_placeholder(self):
        task_groups = [{"group_id": "G1", "name": "Empty", "tasks": []}]
        out = _format_task_groups(task_groups)
        assert "### G1 — Empty" in out
        assert "_(no tasks)_" in out

    def test_group_without_id_or_name(self):
        task_groups = [{"tasks": [{"id": 1, "description": "orphan"}]}]
        out = _format_task_groups(task_groups)
        assert "(unnamed group)" in out
        assert "- [1] orphan" in out

    def test_task_without_description(self):
        task_groups = [{"group_id": "G1", "name": "g", "tasks": [{"id": 5}]}]
        out = _format_task_groups(task_groups)
        assert "- [5] (no description)" in out

    def test_task_without_id(self):
        task_groups = [
            {"group_id": "G1", "name": "g", "tasks": [{"description": "no id here"}]}
        ]
        out = _format_task_groups(task_groups)
        assert "- no id here" in out

    def test_acceptance_criteria_not_a_list_is_skipped(self):
        task_groups = [
            {
                "group_id": "G1",
                "name": "g",
                "tasks": [{"id": 1, "description": "t", "acceptance_criteria": "not a list"}],
            }
        ]
        out = _format_task_groups(task_groups)
        assert "- [1] t" in out
        assert "AC:" not in out

    def test_empty_acceptance_criteria_entry_skipped(self):
        task_groups = [
            {
                "group_id": "G1",
                "name": "g",
                "tasks": [
                    {"id": 1, "description": "t", "acceptance_criteria": ["", "  ", "real one"]}
                ],
            }
        ]
        out = _format_task_groups(task_groups)
        assert "- AC: real one" in out
        assert out.count("AC:") == 1

    def test_truncation_applied_when_too_long(self):
        # Build a large task_groups that exceeds the cap.
        big_desc = "x" * 500
        tasks = [{"id": i, "description": big_desc} for i in range(50)]
        task_groups = [{"group_id": "G1", "name": "big", "tasks": tasks}]
        out = _format_task_groups(task_groups)
        assert len(out) <= SELF_CHECK_TASK_GROUPS_MAX_CHARS + 50  # ellipsis overhead
        assert "… (truncated)" in out

    def test_no_truncation_when_under_cap(self):
        task_groups = [
            {"group_id": "G1", "name": "small", "tasks": [{"id": 1, "description": "tiny"}]}
        ]
        out = _format_task_groups(task_groups)
        assert "… (truncated)" not in out

    def test_non_dict_task_entries_skipped(self):
        task_groups = [
            {
                "group_id": "G1",
                "name": "g",
                "tasks": ["not a dict", 123, {"id": 1, "description": "ok"}],
            }
        ]
        out = _format_task_groups(task_groups)
        assert "- [1] ok" in out


# ---------------------------------------------------------------------------
# _build_task_groups_section — wraps summary with intro or returns ""
# ---------------------------------------------------------------------------


class TestBuildTaskGroupsSection:
    def test_empty_input_returns_empty(self):
        assert _build_task_groups_section(None) == ""
        assert _build_task_groups_section([]) == ""

    def test_populated_input_includes_intro(self):
        task_groups = [
            {"group_id": "G1", "name": "X", "tasks": [{"id": 1, "description": "t"}]}
        ]
        out = _build_task_groups_section(task_groups)
        assert "## Plan Task Groups (Authoritative Task List — HARD AUDIT)" in out
        assert "hard audit" in out.lower()
        assert "authoritative list" in out.lower()
        assert "- [1] t" in out

    def test_intro_matches_constant(self):
        task_groups = [
            {"group_id": "G1", "name": "X", "tasks": [{"id": 1, "description": "t"}]}
        ]
        out = _build_task_groups_section(task_groups)
        assert _TASK_GROUPS_SECTION_INTRO in out


# ---------------------------------------------------------------------------
# self_check_handler — prompt content varies by task_groups presence
# ---------------------------------------------------------------------------


@pytest.fixture
def flow(tmp_path):
    f = FlowInstance(
        flow_id="test-flow-tg",
        task_description="Implement feature X",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "tg",
    )
    f.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
        StepType.VERIFY_SPEC,
    ]
    return f


def _make_step(task_groups=None):
    inputs = {
        "task_description": "Implement feature X",
        "changes_made": {"files_changed": ["src/feature.py"]},
        "test_results": {"passed": True, "returncode": 0, "stdout": "ok"},
        "spec_content": {"base": "Base spec content"},
    }
    if task_groups is not None:
        inputs["task_groups"] = task_groups
    return Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.PENDING,
        inputs=inputs,
    )


def _call_handler_capture_prompt(step, flow):
    """Run self_check_handler with a mocked LLM, return the prompt passed in."""
    response = json.dumps({"issues": [], "summary": "ok"})
    with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
        mock_caller = Mock()
        mock_caller.call.return_value = response
        mock_cls.return_value = mock_caller
        result = self_check_handler(step, flow)
    assert result == StepStatus.COMPLETED
    return mock_caller.call.call_args.kwargs["prompt"]


class TestHandlerPromptInjection:
    def test_prompt_includes_section_when_task_groups_present(self, flow):
        task_groups = [
            {
                "group_id": "G1",
                "name": "Auth flow",
                "tasks": [
                    {
                        "id": 1,
                        "description": "Add login endpoint",
                        "acceptance_criteria": ["Returns 200 on valid creds"],
                    }
                ],
            }
        ]
        step = _make_step(task_groups=task_groups)
        prompt = _call_handler_capture_prompt(step, flow)

        assert "## Plan Task Groups (Authoritative Task List — HARD AUDIT)" in prompt
        assert "Add login endpoint" in prompt
        assert "AC: Returns 200 on valid creds" in prompt
        assert "hard audit" in prompt.lower()
        # Guard the load-bearing hard-audit phrasing: task_groups is the
        # authoritative task list, audited strictly per task. These lines must
        # reach the final prompt verbatim — future edits to
        # _TASK_GROUPS_SECTION_INTRO that weaken the audit should fail here.
        assert "authoritative list" in prompt.lower()
        assert 'expectation_source.type = "plan_task"' in prompt
        # The old soft-reference disclaimer must be gone.
        assert "NOT a strict specification" not in prompt
        assert "Reasonable deviations from the plan" not in prompt
        assert "missing-plan-compliance" not in prompt

    def test_prompt_omits_section_when_task_groups_absent(self, flow):
        step = _make_step(task_groups=None)
        prompt = _call_handler_capture_prompt(step, flow)
        assert "## Plan Task Groups" not in prompt

    def test_prompt_omits_section_when_task_groups_empty_list(self, flow):
        step = _make_step(task_groups=[])
        prompt = _call_handler_capture_prompt(step, flow)
        assert "## Plan Task Groups" not in prompt

    def test_prompt_has_no_orphan_blank_lines_when_omitted(self, flow):
        """Sanity: the absent-section path should not leave a dangling heading or giant gap."""
        step = _make_step(task_groups=None)
        prompt = _call_handler_capture_prompt(step, flow)
        # Spec content section should flow directly into Fix Context without an orphan heading.
        assert "## Plan Task Groups" not in prompt
        # No 4+ consecutive blank lines produced by the conditional.
        assert "\n\n\n\n" not in prompt
