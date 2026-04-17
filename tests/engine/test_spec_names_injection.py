"""Tests for context_builder.get_spec_names_injection.

Covers whitelist/blacklist gating, yaml override behavior, forbidden-step
precedence, loaded-specs rendering, all-specs sorting, and empty-input edge
cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.context_builder import (
    SPEC_NAMES_INJECTION_DEFAULT_STEPS,
    SPEC_NAMES_INJECTION_FORBIDDEN_STEPS,
    get_spec_names_injection,
)


def _make_project_root(tmp_path: Path, spec_names: list[str]) -> Path:
    """Create a minimal project root with se3/specs/<name>/spec.md files."""
    specs_dir = tmp_path / "se3" / "specs"
    specs_dir.mkdir(parents=True)
    for name in spec_names:
        spec_dir = specs_dir / name
        spec_dir.mkdir()
        (spec_dir / "spec.md").write_text(f"# {name}\n", encoding="utf-8")
    return tmp_path


def test_forbidden_step_returns_empty(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine"])
    for step in SPEC_NAMES_INJECTION_FORBIDDEN_STEPS:
        assert get_spec_names_injection(step, project_root, ["base"]) == ""


def test_default_whitelist_returns_non_empty(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine"])
    result = get_spec_names_injection("plan", project_root, ["base"])
    assert "## Available Specifications" in result
    assert "base" in result
    assert "flow-engine" in result
    assert "se3/specs/<name>/spec.md" in result
    assert "MAY" in result
    assert "avoid reading broadly" in result


def test_yaml_override_narrows_whitelist(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine"])
    (project_root / "se3.yaml").write_text(
        "spec_names_injection:\n  steps: [only_plan]\n",
        encoding="utf-8",
    )
    # implement is in DEFAULT but not in the override -> empty
    assert get_spec_names_injection("implement", project_root, ["base"]) == ""
    # only_plan is in the override -> non-empty
    result = get_spec_names_injection("only_plan", project_root, ["base"])
    assert "## Available Specifications" in result


def test_forbidden_takes_precedence_over_yaml(tmp_path):
    project_root = _make_project_root(tmp_path, ["base"])
    (project_root / "se3.yaml").write_text(
        "spec_names_injection:\n  steps: [summarize, plan]\n",
        encoding="utf-8",
    )
    # summarize is FORBIDDEN — yaml cannot re-enable it
    assert get_spec_names_injection("summarize", project_root, ["base"]) == ""
    # plan remains enabled via yaml
    assert "## Available Specifications" in get_spec_names_injection(
        "plan", project_root, ["base"]
    )


def test_loaded_list_rendering(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine", "issue-discovery"])
    result = get_spec_names_injection(
        "plan", project_root, ["base", "flow-engine"]
    )
    assert "Specs already loaded above: base, flow-engine" in result


def test_all_spec_names_sorted(tmp_path):
    # Intentionally create specs in non-alphabetical order
    project_root = _make_project_root(tmp_path, ["zulu", "alpha", "mike"])
    result = get_spec_names_injection("plan", project_root, None)
    listing_line = next(
        line for line in result.splitlines()
        if line.startswith("All available specs in this project:")
    )
    # Extract names in the rendered order
    tail = listing_line.split(":", 1)[1].rstrip(".").strip()
    names = [n.strip() for n in tail.split(",")]
    assert names == ["alpha", "mike", "zulu"]


def test_empty_relevant_specs_renders_none(tmp_path):
    project_root = _make_project_root(tmp_path, ["base"])
    # None
    result_none = get_spec_names_injection("plan", project_root, None)
    assert "Specs already loaded above: none" in result_none
    # Empty list
    result_empty = get_spec_names_injection("plan", project_root, [])
    assert "Specs already loaded above: none" in result_empty


def test_defaults_cover_expected_steps():
    # Sanity check: the task spec requires these steps to be default-enabled
    for step in [
        "plan",
        "plan_tasks",
        "implement",
        "verify_spec",
        "update_spec",
        "self_check",
        "design",
    ]:
        assert step in SPEC_NAMES_INJECTION_DEFAULT_STEPS


# ---------------------------------------------------------------------------
# Integration tests: verify each handler actually appends the injection to its
# prompt, and that excluded steps do not. These invoke the real handlers with a
# mocked LLMCaller so the captured prompt reflects end-to-end integration.
# ---------------------------------------------------------------------------

import json
from unittest.mock import Mock, patch

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)

INJECTION_MARKER = "All available specs in this project:"
INJECTION_HEADING = "## Available Specifications"


def _setup_project(tmp_path: Path) -> Path:
    """Create a project root with two sample specs under se3/specs/."""
    _make_project_root(tmp_path, ["base", "flow-engine"])
    return tmp_path


def _make_flow(tmp_path: Path, step_type: StepType, task_type: str = "feature") -> FlowInstance:
    flow = FlowInstance(
        flow_id="test-flow-inject",
        task_description="t",
        task_type=task_type,
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "x",
    )
    flow.state.selected_steps = [step_type]
    return flow


class TestHandlerIntegrationPositive:
    """Each whitelisted handler appends the spec-names injection to its prompt."""

    def test_plan_handler_injects_spec_names(self, tmp_path):
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.PLAN)
        step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Add feature",
                "task_type": "feature",
                "scope": "engine",
                "spec_content": {"base": "base content"},
                "project_summary": "A project",
            },
        )
        llm_response = json.dumps({
            "plan": {
                "proposal": {"summary": "s"},
                "design": {"overview": "o"},
            },
            "task_groups": [
                {
                    "group_id": "G1", "name": "g", "description": "d",
                    "group_order": 1, "depends_on": [],
                    "tasks": [{"id": 1, "description": "t", "complexity": "small", "estimated_loc": 10}],
                }
            ],
            "total_complexity": "small",
        })
        with patch("se3.engine.steps.plan.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            from se3.engine.steps.plan import plan_handler
            with patch("se3.engine.steps.plan._display_plan"):
                plan_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt

    def test_implement_handler_injects_spec_names(self, tmp_path):
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.IMPLEMENT)
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-inj",
            inputs={
                "task_description": "t",
                "task_type": "feature",
                "task_groups": [
                    {"group_id": "G1", "group_order": 1, "depends_on": [],
                     "tasks": [{"id": 1, "description": "t", "estimated_loc": 10}]}
                ],
                "spec_content": {"base": "content"},
                "relevant_specs": ["base"],
                "design_doc": {},
            },
        )
        parsed = {
            "files_changed": ["a.py"], "tests_added": [],
            "test_mapping": {}, "summary": "done",
            "completion_status": "complete", "incomplete_tasks": [],
            "restricted_edits": [],
        }
        with patch("se3.engine.steps.implement.LLMCaller") as mock_cls, \
             patch("se3.engine.steps.implement.parse_json_response", return_value=parsed), \
             patch("se3.engine.steps.implement._resolve_files_changed"), \
             patch("se3.engine.steps.implement._display_task_plan"):
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps(parsed)
            mock_cls.return_value = mock_caller
            from se3.engine.steps.implement import implement_handler
            implement_handler(step, flow)
            prompt_arg = mock_caller.call.call_args.kwargs.get("prompt")
            if prompt_arg is None:
                prompt_arg = mock_caller.call.call_args.args[0]
        assert INJECTION_HEADING in prompt_arg
        assert INJECTION_MARKER in prompt_arg

    def test_verify_spec_handler_injects_spec_names(self, tmp_path):
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.VERIFY_SPEC)
        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "t",
                "spec_content": {"base": "c"},
                "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0, "stdout": ""},
                "relevant_specs": ["base"],
            },
        )
        response = json.dumps({
            "issues": [], "summary": "", "recommendations": [],
            "test_analysis": {"tests_passed": True, "failure_summary": "", "root_cause": ""},
            "fix_instructions": "",
        })
        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            from se3.engine.steps.verify_spec import verify_spec_handler
            verify_spec_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt

    def test_update_spec_handler_injects_spec_names(self, tmp_path):
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.UPDATE_SPEC)
        step = Step(
            step_type=StepType.UPDATE_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "t",
                "changes_made": {"files_changed": ["src/foo.py"]},
                "verification_result": {"verified": True, "summary": "OK"},
                "relevant_specs": ["flow-engine"],
            },
        )
        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller
            from se3.engine.steps.update_spec import update_spec_handler
            update_spec_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt

    def test_self_check_handler_injects_spec_names(self, tmp_path):
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.SELF_CHECK)
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "t",
                "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0, "stdout": ""},
                "spec_content": {"base": "c"},
                "relevant_specs": ["base", "flow-engine"],
            },
        )
        response = json.dumps({"issues": [], "summary": "OK"})
        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            from se3.engine.steps.self_check import self_check_handler
            self_check_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt


class TestHandlerIntegrationNegative:
    """Non-whitelisted steps do not receive the spec-names injection.

    Handlers that build their own prompts (analyze, discovery) keep their
    pre-existing spec listing unchanged (regression). summarize/commit receive
    no injection at all — commit has no LLM call so is verified at the helper
    contract level.
    """

    def test_analyze_retains_original_spec_listing(self, tmp_path):
        """analyze is NOT in the new injection whitelist — its own
        '## Available Specs' section (from ANALYZE_PROMPT) still lists specs,
        but the new injection marker must be absent."""
        project_root = _setup_project(tmp_path)
        # The new-injection marker must not be produced for 'analyze'
        assert INJECTION_MARKER not in get_spec_names_injection(
            "analyze", project_root, ["base"],
        )
        # analyze's own template still contains its original heading
        from se3.engine.steps.analyze import ANALYZE_PROMPT
        assert "## Available Specs" in ANALYZE_PROMPT

    def test_discovery_retains_original_spec_listing(self, tmp_path):
        """discovery is NOT in the new injection whitelist — its own
        '## Available Specifications' section (from INITIAL_DISCOVERY_PROMPT)
        remains, but the new injection's unique marker must be absent."""
        project_root = _setup_project(tmp_path)
        assert INJECTION_MARKER not in get_spec_names_injection(
            "discovery", project_root, ["base"],
        )
        from se3.engine.steps.discovery import INITIAL_DISCOVERY_PROMPT
        # The original template-level section heading is preserved
        assert "## Available Specifications" in INITIAL_DISCOVERY_PROMPT

    def test_summarize_handler_prompt_lacks_injection(self, tmp_path):
        """summarize is FORBIDDEN — invoking it must not include the new
        injection in its LLM prompt."""
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.SUMMARIZE)
        step = Step(
            step_type=StepType.SUMMARIZE,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "t",
                "changes_made": {},
                "test_results": {},
                "verification_result": {},
                "commit_hash": "abc1234",
                "relevant_specs": ["base"],
            },
        )
        with patch("se3.engine.steps.summarize.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = "Summary text"
            mock_cls.return_value = mock_caller
            from se3.engine.steps.summarize import summarize_handler
            summarize_handler(step, flow)
            call_kwargs = mock_caller.call.call_args
            prompt = call_kwargs.kwargs.get("prompt")
            if prompt is None and call_kwargs.args:
                prompt = call_kwargs.args[0]
        assert INJECTION_MARKER not in prompt
        assert INJECTION_HEADING not in prompt

    def test_commit_step_never_receives_injection(self, tmp_path):
        """commit has no LLM call — verified via the helper contract that
        the 'commit' step type is in the FORBIDDEN set and returns empty
        regardless of yaml override attempts."""
        project_root = _setup_project(tmp_path)
        assert "commit" in SPEC_NAMES_INJECTION_FORBIDDEN_STEPS
        # yaml override cannot enable 'commit'
        (project_root / "se3.yaml").write_text(
            "spec_names_injection:\n  steps: [commit]\n",
            encoding="utf-8",
        )
        assert get_spec_names_injection("commit", project_root, ["base"]) == ""
