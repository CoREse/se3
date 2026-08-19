"""Regression: the retired spec mirror leaves no residue in any step prompt.

``tianluo/specs/`` is gone; project conventions now reach an LLM step through
the charter + code-index injection alone. These tests pin the *absence* of the
old spec copy and of the step-to-step spec data channels, so a future edit that
reintroduces either fails here rather than silently shipping a prompt that
points an agent at files which no longer exist.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import (
    STEP_POOL,
    FlowInstance,
    Step,
    StepStatus,
    StepType,
)


# --------------------------------------------------------------------------
# implement: the four prompt paths carry no ## Project Conventions section
# --------------------------------------------------------------------------

_DUMMY = "X"


def _implement_templates() -> dict[str, tuple[str, dict]]:
    """Every implement prompt path, with fields that render it end to end."""
    from tianluo.engine.steps.implement import (
        FIX_PROMPT,
        HOLISTIC_IMPLEMENT_PROMPT,
        IMPLEMENT_CAPABILITY_GROUP_PROMPT,
        IMPLEMENT_GROUP_PROMPT,
        IMPLEMENT_PROMPT,
    )

    grouped_fields = dict(
        task_description=_DUMMY,
        task_type="feature",
        root_cause_section="",
        current_group=_DUMMY,
        previous_results=_DUMMY,
    )
    return {
        # Whole-task single call over all groups.
        "single_call": (IMPLEMENT_PROMPT, dict(
            task_description=_DUMMY,
            task_type="feature",
            root_cause_section="",
            task_groups=_DUMMY,
        )),
        # Whole-task holistic call (small / single group / legacy direct).
        "holistic": (HOLISTIC_IMPLEMENT_PROMPT, dict(
            task_description=_DUMMY,
            task_type="small",
            root_cause_section="",
            execution_mode=_DUMMY,
            analysis_context=_DUMMY,
            continuation_context=_DUMMY,
        )),
        # Sequential grouped execution.
        "grouped": (IMPLEMENT_GROUP_PROMPT, dict(grouped_fields)),
        # DAG-parallel execution under the capability doctrine.
        "capability_group": (IMPLEMENT_CAPABILITY_GROUP_PROMPT, dict(grouped_fields)),
        # Fix iteration.
        "fix": (FIX_PROMPT, dict(
            task_description=_DUMMY,
            root_cause_section="",
            fix_instructions=_DUMMY,
            fix_context=_DUMMY,
            fix_history=_DUMMY,
            fix_iteration=1,
        )),
    }


@pytest.mark.parametrize("path", sorted(_implement_templates()))
def test_implement_prompt_paths_have_no_project_conventions(path):
    template, fields = _implement_templates()[path]
    rendered = template.format(**fields)
    assert "## Project Conventions" not in rendered
    assert "spec_summary" not in template


def test_implement_module_exposes_no_spec_brief_formatter():
    """The spec-summary renderer and its input channel are gone."""
    import tianluo.engine.steps.implement as implement_mod

    assert not hasattr(implement_mod, "_format_spec_brief")


def test_implement_handler_ignores_a_legacy_spec_content_input():
    """A resumed old flow still carrying spec_content injects nothing.

    The key survives in persisted step inputs; implement must simply not read
    it, so no spec text can re-enter the prompt through a resume.
    """
    from tianluo.engine.steps import implement as implement_mod

    source = inspect.getsource(implement_mod)
    assert 'inputs.get("spec_content"' not in source


def test_merge_conflict_resolver_takes_no_spec_context():
    """The DAG leaf-merge conflict prompt lost its spec context parameter."""
    from tianluo.engine.steps.implement import (
        _attempt_merge_with_resolution,
        _merge_leaf_branch,
    )
    from tianluo.engine.worktree import resolve_merge_conflicts_with_context

    for fn in (
        resolve_merge_conflicts_with_context,
        _merge_leaf_branch,
        _attempt_merge_with_resolution,
    ):
        assert "spec_content" not in inspect.signature(fn).parameters, fn.__name__


# --------------------------------------------------------------------------
# version_analyze: the spec_changes / updated_specs channel is gone
# --------------------------------------------------------------------------


def test_version_analyze_prompt_has_no_spec_changes_section():
    from tianluo.engine.steps.version_analyze import VERSION_ANALYZE_PROMPT

    assert "## Spec Changes" not in VERSION_ANALYZE_PROMPT
    assert "spec_changes" not in VERSION_ANALYZE_PROMPT


def test_version_analyze_declares_no_updated_specs_input():
    assert "updated_specs" not in STEP_POOL[StepType.VERSION_ANALYZE]["inputs"]


@patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
@patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
@patch("tianluo.engine.steps.version_analyze.LLMCaller")
def test_version_analyze_ignores_legacy_updated_specs_input(
    mock_caller_cls, _ver, _inject, tmp_path,
):
    """A persisted flow's leftover updated_specs reaches no prompt."""
    mock_caller = MagicMock()
    mock_caller.call.return_value = json.dumps({
        "bump_type": "patch",
        "reasoning": "r",
        "confidence": "high",
        "suggested_version": "1.2.4",
        "commit_message": "m",
    })
    mock_caller_cls.return_value = mock_caller

    flow = MagicMock(spec=FlowInstance)
    flow.flow_id = "test-flow-va-spec"
    flow.task_description = "t"
    flow.task_type = "feature"
    flow.change_path = tmp_path / "tianluo.yaml"
    flow.is_worktree_mode = False

    step = MagicMock(spec=Step)
    step.step_type = StepType.VERSION_ANALYZE
    step.step_id = "va-spec-residue"
    step.outputs = {}
    step.inputs = {
        "task_description": "t",
        "task_type": "feature",
        "changes_made": {"files_changed": []},
        "updated_specs": [
            {"spec_name": "flow-engine", "change_description": "MARKER-SPEC-TEXT"},
        ],
    }

    from tianluo.engine.steps.version_analyze import version_analyze_handler

    assert version_analyze_handler(step, flow) == StepStatus.COMPLETED

    prompt = mock_caller.call.call_args.kwargs["prompt"]
    assert "MARKER-SPEC-TEXT" not in prompt
    assert "## Spec Changes" not in prompt


# --------------------------------------------------------------------------
# plan / state_machine: PLAN emits no spec_changes channel
# --------------------------------------------------------------------------


def test_plan_declares_no_spec_changes_output():
    assert "spec_changes" not in STEP_POOL[StepType.PLAN]["outputs"]


def test_analyze_declares_no_spec_content_output():
    assert "spec_content" not in STEP_POOL[StepType.ANALYZE]["outputs"]


# --------------------------------------------------------------------------
# self_check / discovery / runtime environment wording
# --------------------------------------------------------------------------


def test_self_check_prompt_does_not_defer_to_verify_spec():
    from tianluo.engine.steps.self_check import SELF_CHECK_PROMPT

    assert "verify_spec" not in SELF_CHECK_PROMPT
    assert "Spec compliance" not in SELF_CHECK_PROMPT


def test_self_check_module_exposes_no_spec_content_formatter():
    import tianluo.engine.steps.self_check as self_check_mod

    assert not hasattr(self_check_mod, "_format_spec_content")


def test_discovery_prompts_point_at_charter_not_specifications():
    from tianluo.engine.steps.discovery import (
        CONTINUE_DISCOVERY_PROMPT,
        INITIAL_DISCOVERY_PROMPT,
    )

    for prompt in (INITIAL_DISCOVERY_PROMPT, CONTINUE_DISCOVERY_PROMPT):
        assert "available specifications" not in prompt
        assert "code-index" in prompt


def test_runtime_environment_no_longer_warns_about_luo_sync(tmp_path):
    """`luo sync` wrote spec files; with the mirror retired it is not a command
    the injection has any reason to name."""
    from tianluo.engine.context_builder import get_runtime_environment_injection

    injection = get_runtime_environment_injection("implement", tmp_path)
    assert injection
    assert "luo sync" not in injection
