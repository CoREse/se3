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


def test_malformed_yaml_falls_back_to_defaults(tmp_path):
    """A broken se3.yaml must not raise — it should silently fall back to
    defaults so project misconfiguration doesn't crash the flow engine."""
    project_root = _make_project_root(tmp_path, ["base"])
    # Unterminated list — yaml.safe_load will raise
    (project_root / "se3.yaml").write_text(
        "spec_names_injection:\n  steps: [plan",
        encoding="utf-8",
    )
    result = get_spec_names_injection("plan", project_root, ["base"])
    # Defaults still in effect -> 'plan' whitelisted -> non-empty injection
    assert "## Available Specifications" in result


def test_non_list_yaml_override_ignored(tmp_path):
    """If the user types `steps: plan` instead of `steps: [plan]`, the
    loader must ignore the malformed override rather than treating the
    string as a whitelist (which would make `'p' in 'plan'` true)."""
    project_root = _make_project_root(tmp_path, ["base"])
    (project_root / "se3.yaml").write_text(
        "spec_names_injection:\n  steps: plan\n",
        encoding="utf-8",
    )
    # 'plan' is in defaults, so injection still fires — but not because the
    # string 'plan' was treated as a whitelist. Verify defaults are used by
    # checking a non-default step is still denied.
    assert "## Available Specifications" in get_spec_names_injection(
        "plan", project_root, ["base"]
    )
    # 'not_a_real_step' would substring-match the string 'plan' if the bug
    # existed (letter 'p' etc.), but must not match the DEFAULT list.
    assert get_spec_names_injection("not_a_real_step", project_root, ["base"]) == ""


def test_defaults_cover_expected_steps():
    # Sanity check: the task spec requires these steps to be default-enabled.
    # Deprecated types ('design', 'propose') are intentionally NOT in defaults
    # because their stub handlers forward to plan_handler, which keys its
    # injection on "plan" — so a 'design' entry here would be unreachable.
    for step in [
        "plan",
        "plan_tasks",
        "implement",
        "verify_spec",
        "update_spec",
        "self_check",
    ]:
        assert step in SPEC_NAMES_INJECTION_DEFAULT_STEPS
    assert "design" not in SPEC_NAMES_INJECTION_DEFAULT_STEPS


def test_null_yaml_falls_back_to_defaults(tmp_path):
    """`spec_names_injection: null` (explicit key with null value) must fall
    back to defaults rather than raising AttributeError on .get('steps')."""
    project_root = _make_project_root(tmp_path, ["base"])
    (project_root / "se3.yaml").write_text(
        "spec_names_injection: null\n",
        encoding="utf-8",
    )
    # Defaults still apply — 'plan' is whitelisted.
    assert "## Available Specifications" in get_spec_names_injection(
        "plan", project_root, ["base"],
    )


def test_non_string_relevant_specs_are_filtered(tmp_path):
    """Defensive filter: malformed upstream input with non-string entries
    (e.g. dicts) must not raise TypeError from sorted(); non-strings are
    silently dropped."""
    project_root = _make_project_root(tmp_path, ["base"])
    relevant = ["flow-engine", {"unexpected": "dict"}, "base", 42]  # type: ignore[list-item]
    result = get_spec_names_injection("plan", project_root, relevant)
    # The two string entries are preserved and sorted; non-strings are dropped.
    assert "Specs already loaded above: base, flow-engine" in result


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
# The fixtures create real specs named "base" and "flow-engine" under the
# derived project_root; the injection MUST list them by name so regressions in
# path resolution / spec enumeration are actually caught.
INJECTION_REAL_SPECS = "All available specs in this project: base, flow-engine"


def _setup_project(tmp_path: Path) -> Path:
    """Create a project root with two sample specs under se3/specs/."""
    _make_project_root(tmp_path, ["base", "flow-engine"])
    return tmp_path


def _make_flow(tmp_path: Path, step_type: StepType, task_type: str = "feature") -> FlowInstance:
    # Handlers derive project_root = flow.change_path.parent; set change_path so
    # the derived project_root is `tmp_path` (where _setup_project writes specs).
    flow = FlowInstance(
        flow_id="test-flow-inject",
        task_description="t",
        task_type=task_type,
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "x",
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
        assert INJECTION_REAL_SPECS in prompt

    def test_plan_tasks_handler_injects_spec_names(self, tmp_path):
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.PLAN_TASKS)
        step = Step(
            step_type=StepType.PLAN_TASKS,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "t",
                "design_doc": {"overview": "o"},
                "proposal": {"summary": "s"},
                "spec_content": {"base": "c"},
                "relevant_specs": ["base"],
            },
        )
        llm_response = json.dumps({
            "task_groups": [
                {
                    "group_id": "G1", "name": "g", "description": "d",
                    "group_order": 1, "depends_on": [],
                    "tasks": [{"id": 1, "description": "t", "complexity": "small", "estimated_loc": 10}],
                }
            ],
            "total_complexity": "small",
        })
        with patch("se3.engine.steps.plan_tasks.LLMCaller") as mock_cls, \
             patch("se3.engine.steps.plan_tasks.get_console"), \
             patch("se3.engine.steps.plan_tasks.TaskFormatter"), \
             patch("se3.engine.steps.plan_tasks.format_task_groups", return_value=""):
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            from se3.engine.steps.plan_tasks import plan_tasks_handler
            plan_tasks_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt
        assert INJECTION_REAL_SPECS in prompt

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
        assert INJECTION_REAL_SPECS in prompt_arg

    def test_implement_handler_sequential_group_path_injects_spec_names(self, tmp_path):
        """Regression guard for implement.py:487-496 (IMPLEMENT_GROUP_PROMPT
        sequential group-by-group loop). The positive single-group test only
        exercises IMPLEMENT_PROMPT; this test uses multiple groups with
        total_loc > threshold and forces the sequential fallback by mocking
        _should_use_dag. Every per-group prompt must include the injection."""
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.IMPLEMENT)
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-seq",
            inputs={
                "task_description": "t",
                "task_type": "feature",
                # total_loc = 1000 > default threshold 300, so LOC-merge path
                # is skipped and we fall through to the group-by-group loop
                # once _should_use_dag is forced False.
                "task_groups": [
                    {"group_id": "G1", "group_order": 1, "depends_on": [],
                     "tasks": [{"id": 1, "description": "t", "estimated_loc": 500}]},
                    {"group_id": "G2", "group_order": 2, "depends_on": [],
                     "tasks": [{"id": 2, "description": "t", "estimated_loc": 500}]},
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
             patch("se3.engine.steps.implement._display_task_plan"), \
             patch("se3.engine.steps.implement._should_use_dag", return_value=False):
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps(parsed)
            mock_cls.return_value = mock_caller
            from se3.engine.steps.implement import implement_handler
            implement_handler(step, flow)
            prompts = []
            for call in mock_caller.call.call_args_list:
                p = call.kwargs.get("prompt")
                if p is None:
                    p = call.args[0]
                prompts.append(p)
        # Sequential path: one LLM call per group.
        assert len(prompts) == 2
        for prompt in prompts:
            assert INJECTION_HEADING in prompt
            assert INJECTION_MARKER in prompt
            assert INJECTION_REAL_SPECS in prompt

    def test_implement_dag_execute_fn_injects_spec_names(self, tmp_path):
        """Regression guard for implement.py:836-845 (IMPLEMENT_GROUP_PROMPT
        inside the DAG parallel execute_fn closure). Calling the full DAG
        parallel path requires worktrees and threading; instead invoke
        _make_execute_fn directly with an injection string and verify the
        closure concatenates it onto the prompt passed to LLMCaller.call."""
        from unittest.mock import MagicMock
        from se3.engine.dag_scheduler import RelayContext
        from se3.engine.steps.implement import _make_execute_fn

        project_root = _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.IMPLEMENT)
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-dag",
            inputs={},
            outputs={},
        )
        parsed = {
            "files_changed": ["a.py"], "tests_added": [],
            "test_mapping": {}, "summary": "done",
            "completion_status": "complete", "incomplete_tasks": [],
            "restricted_edits": [],
        }

        def fake_run_git(root, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        injection_text = (
            "\n\n## Available Specifications\n"
            "All available specs in this project: base, flow-engine.\n\n"
            "Specs already loaded above: base.\n"
        )

        with patch("se3.engine.steps.implement.LLMCaller") as mock_cls, \
             patch("se3.engine.steps.implement.parse_json_response", return_value=parsed), \
             patch("se3.engine.steps.implement._run_git", side_effect=fake_run_git), \
             patch("se3.engine.steps.implement.force_cleanup_worktree"), \
             patch("se3.engine.steps.implement.create_worktree", return_value=project_root), \
             patch("se3.engine.steps.implement._restore_history_to_worktree"):
            mock_caller = MagicMock()
            mock_caller.call.return_value = json.dumps(parsed)
            mock_cls.return_value = mock_caller
            execute_fn = _make_execute_fn(
                project_root=project_root,
                original_branch="master",
                flow=flow,
                step=step,
                task_description="t",
                task_type="feature",
                design_section="",
                spec_summary="",
                injection=injection_text,
                retry_count=0,
            )
            group = {
                "group_id": "G1", "group_order": 1, "depends_on": [],
                "tasks": [{"id": 1, "description": "t", "estimated_loc": 10}],
            }
            execute_fn(group, {}, RelayContext())
            call = mock_caller.call.call_args
            prompt = call.kwargs.get("prompt") or call.args[0]
        # The injection text passed into _make_execute_fn must appear verbatim
        # in the DAG closure's prompt — catching any regression that drops
        # `prompt += injection` at line 844.
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt

    def test_implement_fix_path_injects_spec_names(self, tmp_path):
        """Regression guard for implement.py:230-248 (FIX_PROMPT path in fix
        iterations). A verify_spec-triggered fix loop re-enters implement
        with is_fix_iteration=True; the spec-names injection must still be
        appended so the fix-loop LLM can pull in specs flagged during
        verify_spec."""
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.IMPLEMENT)
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-fix",
            inputs={
                "task_description": "t",
                "task_type": "feature",
                "task_groups": [
                    {"group_id": "G1", "group_order": 1, "depends_on": [],
                     "tasks": [{"id": 1, "description": "t", "estimated_loc": 10}]},
                ],
                "spec_content": {"base": "content"},
                "relevant_specs": ["base"],
                "design_doc": {},
                "is_fix_iteration": True,
                "fix_iteration": 1,
                "fix_instructions": "Fix the broken assertion",
                "fix_context": {"reason": "self_check"},
                "fix_history": [],
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
             patch("se3.engine.steps.implement._resolve_files_changed"):
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps(parsed)
            mock_cls.return_value = mock_caller
            from se3.engine.steps.implement import implement_handler
            implement_handler(step, flow)
            call = mock_caller.call.call_args
            prompt = call.kwargs.get("prompt")
            if prompt is None:
                prompt = call.args[0]
        assert INJECTION_HEADING in prompt
        assert INJECTION_MARKER in prompt
        assert INJECTION_REAL_SPECS in prompt
        # Sanity: confirm we actually took the fix path, not a regular one.
        assert "## Fix Instructions" in prompt

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
        assert INJECTION_REAL_SPECS in prompt

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
        assert INJECTION_REAL_SPECS in prompt

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
        assert INJECTION_REAL_SPECS in prompt


class TestHandlerIntegrationNegative:
    """Non-whitelisted steps do not receive the spec-names injection.

    Handlers that build their own prompts (analyze, discovery) keep their
    pre-existing spec listing unchanged (regression). summarize/commit receive
    no injection at all — commit has no LLM call so is verified at the helper
    contract level.
    """

    def test_analyze_handler_prompt_lacks_injection(self, tmp_path):
        """analyze is NOT in the injection whitelist. Invoke the real handler
        with a mocked LLMCaller and assert the new-injection marker is absent
        from the captured prompt — this catches future regressions where
        someone accidentally wires get_spec_names_injection into analyze."""
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.ANALYZE)
        step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.PENDING,
            inputs={"task_description": "Add feature"},
        )
        llm_response = json.dumps({
            "task_type": "feature",
            "scope": "engine",
            "complexity": "simple",
            "reasoning": "r",
            "selected_specs": [],
        })
        with patch("se3.engine.steps.analyze.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            from se3.engine.steps.analyze import analyze_handler
            analyze_handler(step, flow)
            prompt = mock_caller.call.call_args.kwargs.get("prompt")
            if prompt is None:
                prompt = mock_caller.call.call_args.args[0]
        # The new-injection marker/heading must not appear in analyze's prompt.
        assert INJECTION_MARKER not in prompt
        assert INJECTION_HEADING not in prompt
        # analyze's own template heading is a distinct string ("Available
        # Specs", no "ifications") — ensure it survived.
        assert "## Available Specs" in prompt

    def test_discovery_handler_prompt_lacks_injection(self, tmp_path):
        """discovery is NOT in the injection whitelist. Invoke the real
        handler with a mocked LLMCaller and assert the new-injection marker
        is absent — this catches future regressions where someone accidentally
        wires get_spec_names_injection into discovery.

        Note: we only check INJECTION_MARKER, not INJECTION_HEADING — the
        discovery template itself contains a '## Available Specifications'
        heading, so INJECTION_HEADING is intentionally non-unique."""
        _setup_project(tmp_path)
        flow = _make_flow(tmp_path, StepType.DISCOVERY, task_type="discovery")
        step = Step(
            step_type=StepType.DISCOVERY,
            status=StepStatus.PENDING,
            inputs={"task_description": "Explore X"},
        )
        # Minimal valid response — mode=question keeps the step PAUSED.
        llm_response = json.dumps({
            "mode": "question",
            "content": "?",
            "questions": ["q1"],
            "refined_description": "",
            "thinking": "",
        })
        with patch("se3.engine.steps.discovery.LLMCaller") as mock_cls, \
             patch("se3.engine.steps.discovery._display_discovery_message"):
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            from se3.engine.steps.discovery import discovery_handler
            discovery_handler(step, flow)
            prompt = mock_caller.call.call_args.kwargs.get("prompt")
            if prompt is None:
                prompt = mock_caller.call.call_args.args[0]
        # The discovery template contains '## Available Specifications' as its
        # own heading, but NOT the injection's 'All available specs in this
        # project:' marker — that string is unique to the injection helper.
        assert INJECTION_MARKER not in prompt

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
