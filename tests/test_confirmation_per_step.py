"""Tests for the per-step confirmation schema.

Covers:
- ``confirmation.steps`` dict-based parsing in ``load_confirmation_config``.
- The three reviewer paths in ``_build_step_inputs`` / ``confirm_handler``:
  agent name → that agent only; ``'human'`` → MCP call file;
  omitted/None → fall back to ``llm_caller.defaults`` chain.
- Steps not listed under ``confirmation.steps`` do not trigger CONFIRM.
- Deprecated fields (``confirmation.enabled``, top-level
  ``confirmation.reviewer``, ``confirmation.llm_reviewer``, list-form
  ``confirmation.steps``) emit warnings and are ignored.
- Unknown agent names referenced from ``reviewer`` raise ``ValueError``
  at startup with a helpful message.
- ``max_iterations`` works per step and falls back to the default of 3.
- Global + project ``confirmation.steps`` merge entry-level.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import se3.config as config_module  # noqa: E402
from se3.config import (  # noqa: E402
    insert_confirmation_steps,
    load_confirmation_config,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_warn_dedup():
    """Clear warning-dedup sets between tests so caplog assertions stable."""
    config_module._warned_confirmation_enabled_for.clear()
    config_module._warned_confirmation_top_reviewer_for.clear()
    config_module._warned_confirmation_llm_reviewer_for.clear()
    config_module._warned_confirmation_steps_list_for.clear()
    config_module._warned_confirmation_unknown_fields_for.clear()
    yield


@pytest.fixture
def isolated_global_home(monkeypatch, tmp_path):
    """Point ``Path.home()`` at a clean temp dir to neutralize the
    real ``~/.se3/config.yaml``. Tests that want a global config should
    write into ``tmp_path/.se3/config.yaml`` after invoking this fixture.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


# ---------------------------------------------------------------------------
# load_confirmation_config: shape + per-step parsing
# ---------------------------------------------------------------------------


class TestLoadConfirmationConfigShape:
    def test_returns_steps_dict_only(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = load_confirmation_config(tmp_path)

        assert set(result.keys()) == {"steps"}
        assert "enabled" not in result
        assert "reviewer" not in result
        assert "llm_reviewer" not in result
        assert result["steps"]["plan"]["reviewer"] == "human"

    def test_no_yaml_returns_empty_steps(self, tmp_path, isolated_global_home):
        result = load_confirmation_config(tmp_path)
        assert result == {"steps": {}}

    def test_empty_section_returns_empty_steps(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text("confirmation: {}\n")
        result = load_confirmation_config(tmp_path)
        assert result == {"steps": {}}

    def test_human_reviewer_parsed(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = load_confirmation_config(tmp_path)
        assert result["steps"]["plan"] == {"reviewer": "human", "max_iterations": None}

    def test_omitted_reviewer_is_none(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {}\n"
        )
        result = load_confirmation_config(tmp_path)
        assert result["steps"]["plan"] == {"reviewer": None, "max_iterations": None}

    def test_max_iterations_parsed(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human, max_iterations: 7}\n"
        )
        result = load_confirmation_config(tmp_path)
        assert result["steps"]["plan"]["max_iterations"] == 7

    def test_invalid_max_iterations_warned_and_dropped(
        self, tmp_path, isolated_global_home, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human, max_iterations: 0}\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            result = load_confirmation_config(tmp_path)
        assert result["steps"]["plan"]["max_iterations"] is None
        assert any("max_iterations" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Deprecated fields are warned + ignored
# ---------------------------------------------------------------------------


class TestDeprecatedFieldsIgnored:
    def test_enabled_warned_and_ignored(self, tmp_path, isolated_global_home, caplog):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  enabled: false\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            result = load_confirmation_config(tmp_path)
        # enabled=false has no effect on the new schema
        assert "plan" in result["steps"]
        assert any(
            "confirmation.enabled' is deprecated" in r.getMessage()
            for r in caplog.records
        )

    def test_top_level_reviewer_warned_and_ignored(
        self, tmp_path, isolated_global_home, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  reviewer: human\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            result = load_confirmation_config(tmp_path)
        # No top-level reviewer in the returned shape
        assert "reviewer" not in result
        assert any(
            "top-level 'confirmation.reviewer' is deprecated" in r.getMessage()
            for r in caplog.records
        )

    def test_llm_reviewer_warned_and_ignored(
        self, tmp_path, isolated_global_home, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  llm_reviewer:\n"
            "    model: claude-sonnet\n"
            "    max_iterations: 5\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            result = load_confirmation_config(tmp_path)
        assert "llm_reviewer" not in result
        # The retained step entry doesn't inherit the deprecated llm_reviewer
        # max_iterations value.
        assert result["steps"]["plan"]["max_iterations"] is None
        assert any(
            "confirmation.llm_reviewer' is deprecated" in r.getMessage()
            for r in caplog.records
        )

    def test_list_form_steps_warned_and_ignored(
        self, tmp_path, isolated_global_home, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps: [plan, design]\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            result = load_confirmation_config(tmp_path)
        assert result == {"steps": {}}
        assert any(
            "'confirmation.steps' is a list" in r.getMessage()
            for r in caplog.records
        )

    def test_dedup_warnings_per_source(
        self, tmp_path, isolated_global_home, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  enabled: false\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            load_confirmation_config(tmp_path)
            load_confirmation_config(tmp_path)
        enabled_warnings = [
            r for r in caplog.records
            if "confirmation.enabled' is deprecated" in r.getMessage()
        ]
        assert len(enabled_warnings) == 1


# ---------------------------------------------------------------------------
# Unknown agent reference raises at startup
# ---------------------------------------------------------------------------


class TestUnknownAgentReference:
    def test_unknown_agent_in_reviewer_raises(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text(
            "agents:\n"
            "  primary: {cmd: claude}\n"
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: nonexistent_bot}\n"
        )
        with pytest.raises(ValueError) as exc_info:
            load_confirmation_config(tmp_path)
        msg = str(exc_info.value)
        assert "confirmation.steps.plan.reviewer" in msg
        assert "nonexistent_bot" in msg
        assert "primary" in msg  # available agents listed

    def test_known_agent_in_reviewer_succeeds(self, tmp_path, isolated_global_home):
        (tmp_path / "se3.yaml").write_text(
            "agents:\n"
            "  reviewer_bot: {cmd: claude-opus}\n"
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: reviewer_bot}\n"
        )
        result = load_confirmation_config(tmp_path)
        assert result["steps"]["plan"]["reviewer"] == "reviewer_bot"

    def test_human_value_does_not_query_registry(
        self, tmp_path, isolated_global_home,
    ):
        # No agents defined; 'human' must not raise.
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = load_confirmation_config(tmp_path)
        assert result["steps"]["plan"]["reviewer"] == "human"


# ---------------------------------------------------------------------------
# Global + project entry-level merge
# ---------------------------------------------------------------------------


class TestGlobalProjectMerge:
    def test_merge_entry_level(self, tmp_path, isolated_global_home):
        global_dir = isolated_global_home / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
            "    design: {reviewer: human, max_iterations: 5}\n"
        )
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    design: {reviewer: human, max_iterations: 9}\n"
            "    implement: {}\n"
        )
        result = load_confirmation_config(tmp_path)

        # plan only in global → kept
        assert result["steps"]["plan"]["reviewer"] == "human"
        # design overridden by project entry
        assert result["steps"]["design"]["max_iterations"] == 9
        # implement only in project → kept
        assert result["steps"]["implement"]["reviewer"] is None


# ---------------------------------------------------------------------------
# insert_confirmation_steps respects the dict
# ---------------------------------------------------------------------------


class TestInsertConfirmationSteps:
    def test_step_in_dict_triggers_confirm(self, tmp_path, isolated_global_home):
        from se3.engine.models import StepType

        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = insert_confirmation_steps(
            [StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT],
            tmp_path,
        )
        plan_idx = result.index(StepType.PLAN)
        assert result[plan_idx + 1] == StepType.CONFIRM

    def test_step_not_in_dict_no_confirm(self, tmp_path, isolated_global_home):
        from se3.engine.models import StepType

        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = insert_confirmation_steps(
            [StepType.ANALYZE, StepType.IMPLEMENT, StepType.TEST],
            tmp_path,
        )
        assert StepType.CONFIRM not in result

    def test_empty_dict_no_confirm(self, tmp_path, isolated_global_home):
        from se3.engine.models import StepType

        (tmp_path / "se3.yaml").write_text("confirmation: {steps: {}}\n")
        result = insert_confirmation_steps(
            [StepType.PLAN, StepType.IMPLEMENT], tmp_path,
        )
        assert StepType.CONFIRM not in result


# ---------------------------------------------------------------------------
# state_machine._build_step_inputs CONFIRM branch — three reviewer paths
# ---------------------------------------------------------------------------


class TestBuildStepInputsConfirm:
    """Verify the three reviewer paths in state_machine when building
    a CONFIRM step's inputs from per-step config."""

    def _make_state_machine(self, project_root):
        from se3.engine.state_machine import StateMachine
        from se3.engine.persistence import PersistenceManager

        (project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)
        persistence = PersistenceManager(project_root)
        return StateMachine(project_root, persistence)

    def _make_flow_with_plan(self, project_root):
        from se3.engine.models import (
            FlowInstance,
            Step,
            StepStatus,
            StepType,
        )

        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
            change_name="t",
            change_path=project_root / "t",
        )
        flow.state.selected_steps = [StepType.PLAN, StepType.CONFIRM]
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        plan_step.outputs["proposal"] = "T"
        flow.state.add_step(plan_step)
        flow.state.current_step_id = "plan-001"
        flow.state.current_step_index = 0
        return flow

    def test_human_reviewer_path(self, tmp_path, isolated_global_home):
        from se3.engine.models import StepType

        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_plan(tmp_path)

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] == "human"
        # Human path doesn't get an agents list
        assert "agents" not in next_step.inputs

    def test_agent_name_reviewer_path(self, tmp_path, isolated_global_home):
        from se3.engine.models import StepType

        (tmp_path / "se3.yaml").write_text(
            "agents:\n"
            "  reviewer_bot: {cmd: claude-opus}\n"
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: reviewer_bot, max_iterations: 4}\n"
        )
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_plan(tmp_path)

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] == "reviewer_bot"
        assert next_step.inputs["max_iterations"] == 4
        agents = next_step.inputs["agents"]
        assert isinstance(agents, list) and len(agents) == 1
        assert agents[0]["cmd"] == "claude-opus"

    def test_omitted_reviewer_falls_back_to_defaults(
        self, tmp_path, isolated_global_home,
    ):
        from se3.engine.models import StepType

        (tmp_path / "se3.yaml").write_text(
            "agents:\n"
            "  primary: {cmd: claude, priority: 10}\n"
            "  backup:  {cmd: claude-dev, priority: 5}\n"
            "llm_caller:\n"
            "  defaults: [primary, backup]\n"
            "confirmation:\n"
            "  steps:\n"
            "    plan: {}\n"
        )
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_plan(tmp_path)

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] is None
        agents = next_step.inputs["agents"]
        # Defaults chain produces both agents, sorted by priority desc.
        cmds = [a["cmd"] for a in agents]
        assert cmds == ["claude", "claude-dev"]
        assert next_step.inputs["max_iterations"] == 3  # default


# ---------------------------------------------------------------------------
# confirm_handler dispatch on the three reviewer values
# ---------------------------------------------------------------------------


class TestConfirmHandlerDispatch:
    def _make_flow(self, project_root):
        from se3.engine.models import (
            FlowInstance,
            Step,
            StepStatus,
            StepType,
        )

        flow = FlowInstance(
            task_description="T",
            task_type="feature",
            change_name="t",
            change_path=project_root / "t",
        )
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"proposal": "P"},
        )
        flow.state.add_step(plan_step)
        return flow

    def test_human_reviewer_creates_call_file(self, tmp_path):
        from se3.engine.models import Step, StepStatus, StepType
        from se3.engine.steps.confirm import confirm_handler

        (tmp_path / "se3" / "calls").mkdir(parents=True, exist_ok=True)
        flow = self._make_flow(tmp_path)
        confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-1",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
                "reviewer": "human",
            },
        )
        flow.state.add_step(confirm)

        result = confirm_handler(confirm, flow)

        assert result == StepStatus.PAUSED
        assert "call_file" in confirm.outputs
        assert Path(confirm.outputs["call_file"]).exists()

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_agent_name_reviewer_uses_llm_path(self, MockLLMCaller, tmp_path):
        import json
        from se3.engine.models import Step, StepStatus, StepType
        from se3.engine.steps.confirm import confirm_handler

        (tmp_path / "se3" / "calls").mkdir(parents=True, exist_ok=True)
        flow = self._make_flow(tmp_path)

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(
            {"approved": True, "feedback": "OK"}
        )
        MockLLMCaller.return_value = mock_caller

        agents_list = [
            {"name": "reviewer_bot", "type": "claude-code",
             "cmd": "claude-opus", "priority": 0}
        ]
        confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-2",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
                "reviewer": "reviewer_bot",
                "agents": agents_list,
                "max_iterations": 3,
            },
        )
        flow.state.add_step(confirm)

        result = confirm_handler(confirm, flow)

        assert result == StepStatus.COMPLETED
        # No call file created
        call_files = list((tmp_path / "se3" / "calls").glob("confirm_*.json"))
        assert call_files == []
        # Verify LLMCaller was constructed with the explicit agents kwarg.
        kwargs = MockLLMCaller.call_args.kwargs
        assert kwargs.get("agents") == agents_list

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_none_reviewer_uses_llm_path_with_passed_agents(
        self, MockLLMCaller, tmp_path,
    ):
        import json
        from se3.engine.models import Step, StepStatus, StepType
        from se3.engine.steps.confirm import confirm_handler

        (tmp_path / "se3" / "calls").mkdir(parents=True, exist_ok=True)
        flow = self._make_flow(tmp_path)

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(
            {"approved": True, "feedback": "OK"}
        )
        MockLLMCaller.return_value = mock_caller

        defaults_chain = [
            {"name": "claude", "type": "claude-code",
             "cmd": "claude", "priority": 0}
        ]
        confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-3",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
                "reviewer": None,
                "agents": defaults_chain,
                "max_iterations": 3,
            },
        )
        flow.state.add_step(confirm)

        result = confirm_handler(confirm, flow)

        assert result == StepStatus.COMPLETED
        kwargs = MockLLMCaller.call_args.kwargs
        assert kwargs.get("agents") == defaults_chain
