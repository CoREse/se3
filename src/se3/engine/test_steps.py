"""Integration tests for flow engine step handlers.

Tests step implementations with mocked LLM calls.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .models import FlowInstance, Step, StepStatus, StepType
from .steps import (
    STEP_HANDLERS,
    analyze_handler,
    test_handler as run_test_step,
    commit_handler,
)


class TestAnalyzeStep:
    """Tests for the analyze step."""

    @patch("se3.engine.steps.analyze.list_spec_names", return_value=["base", "flow-engine", "se3-workflows"])
    @patch("se3.engine.steps.analyze.ProjectContextCollector")
    @patch("se3.engine.steps.analyze.ContextBuilder")
    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_success(self, MockLLMCaller, MockContextBuilder, MockCollector, mock_list_specs):
        """Test successful analysis includes all new output fields."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "feature",
            "scope": "backend",
            "complexity": "medium",
            "reasoning": "New user login feature",
            "selected_specs": ["flow-engine"],
        })
        MockLLMCaller.return_value = mock_caller

        # Mock project context collector
        mock_collector_inst = MagicMock()
        mock_collector_inst.collect.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "specs": ["base", "flow-engine"],
        }
        MockCollector.return_value = mock_collector_inst

        # Mock context builder for spec loading
        mock_builder = MagicMock()
        mock_builder.specs_dir = Path("/tmp/specs")
        def mock_load_spec(name):
            return f"# {name} spec content"
        mock_builder._load_spec_content.side_effect = mock_load_spec
        MockContextBuilder.return_value = mock_builder

        flow = FlowInstance(task_description="Add user login")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Add user login"

        result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Original outputs
        assert step.outputs["task_type"] == "feature"
        assert step.outputs["scope"] == "backend"
        assert step.outputs["complexity"] == "medium"
        assert step.outputs["reasoning"] == "New user login feature"
        # New merged outputs
        assert "project_summary" in step.outputs
        assert isinstance(step.outputs["project_summary"], str)
        assert len(step.outputs["project_summary"]) > 0
        assert "relevant_specs" in step.outputs
        assert "base" in step.outputs["relevant_specs"]
        assert "flow-engine" in step.outputs["relevant_specs"]
        assert "spec_content" in step.outputs
        assert "base" in step.outputs["spec_content"]
        assert "flow-engine" in step.outputs["spec_content"]

    @patch("se3.engine.steps.analyze.list_spec_names", return_value=["base"])
    @patch("se3.engine.steps.analyze.ProjectContextCollector")
    @patch("se3.engine.steps.analyze.ContextBuilder")
    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_empty_selected_specs_loads_base(self, MockLLMCaller, MockContextBuilder, MockCollector, mock_list_specs):
        """Test that base spec is loaded even when LLM returns empty selected_specs."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "small",
            "scope": "readme",
            "complexity": "simple",
            "reasoning": "Typo fix",
            "selected_specs": [],
        })
        MockLLMCaller.return_value = mock_caller

        mock_collector_inst = MagicMock()
        mock_collector_inst.collect.return_value = {"git": {"branch": "main"}}
        MockCollector.return_value = mock_collector_inst

        mock_builder = MagicMock()
        mock_builder.specs_dir = Path("/tmp/specs")
        mock_builder._load_spec_content.side_effect = lambda name: f"# {name} content" if name == "base" else None
        MockContextBuilder.return_value = mock_builder

        flow = FlowInstance(task_description="Fix typo")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Fix typo"

        result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert "base" in step.outputs["spec_content"]
        assert step.outputs["relevant_specs"] == ["base"]

    @patch("se3.engine.steps.analyze.list_spec_names", return_value=["base", "flow-engine"])
    @patch("se3.engine.steps.analyze.ProjectContextCollector")
    @patch("se3.engine.steps.analyze.ContextBuilder")
    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_invalid_spec_names_skipped(self, MockLLMCaller, MockContextBuilder, MockCollector, mock_list_specs):
        """Test that invalid spec names from LLM are skipped without failure."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "feature",
            "scope": "auth module",
            "complexity": "medium",
            "reasoning": "Auth feature",
            "selected_specs": ["nonexistent-spec", "also-fake", "flow-engine"],
        })
        MockLLMCaller.return_value = mock_caller

        mock_collector_inst = MagicMock()
        mock_collector_inst.collect.return_value = {"git": {"branch": "main"}}
        MockCollector.return_value = mock_collector_inst

        mock_builder = MagicMock()
        mock_builder.specs_dir = Path("/tmp/specs")
        mock_builder._load_spec_content.side_effect = lambda name: f"# {name} content"
        MockContextBuilder.return_value = mock_builder

        flow = FlowInstance(task_description="Add auth")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Add auth"

        result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Invalid specs should be skipped, only base and flow-engine loaded
        assert "nonexistent-spec" not in step.outputs["spec_content"]
        assert "also-fake" not in step.outputs["spec_content"]
        assert "base" in step.outputs["spec_content"]
        assert "flow-engine" in step.outputs["spec_content"]
        assert "nonexistent-spec" not in step.outputs["relevant_specs"]
        assert "also-fake" not in step.outputs["relevant_specs"]

    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_invalid_json(self, MockLLMCaller):
        """Test handling of invalid JSON from LLM."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = "not valid json"
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test task"

        result = analyze_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message is not None

    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_llm_error(self, MockLLMCaller):
        """Test handling of LLM call failure."""
        from .llm_caller import LLMCallError

        mock_caller = MagicMock()
        mock_caller.call.side_effect = LLMCallError("API error")
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test task"

        result = analyze_handler(step, flow)

        assert result == StepStatus.FAILED


class TestTestStep:
    """Tests for the test step."""

    @patch("subprocess.Popen")
    def test_test_success(self, mock_popen):
        """Test successful test execution."""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = ("5 passed, 0 failed", "")
        mock_popen.return_value = mock_process

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert "test_results" in step.outputs

    @patch("subprocess.Popen")
    def test_test_failure(self, mock_popen):
        """Test handling of test failures."""
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate.return_value = ("3 passed, 2 failed", "FAILED test_foo")
        mock_popen.return_value = mock_process

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        # test_handler returns COMPLETED even on test failure
        # (to allow verify_spec to decide what to do)
        assert result == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is False


class TestCommitStep:
    """Tests for the commit step."""

    @patch("subprocess.run")
    def test_commit_success(self, mock_run):
        """Test successful commit."""
        # Mock git status (has changes) then git add, git commit, git rev-parse
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="M file.py", stderr=""),  # git status
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=0, stdout="[main abc123] message", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc123def456", stderr=""),  # git rev-parse
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = FlowInstance(task_description="Test commit")
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.COMMIT)

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED

    @patch("subprocess.run")
    def test_commit_no_changes(self, mock_run):
        """Test commit when there are no changes."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",  # No changes
            stderr="",
        )

        flow = FlowInstance(task_description="Test commit")
        step = Step(step_type=StepType.COMMIT)

        result = commit_handler(step, flow)

        # Should succeed even with no changes
        assert result == StepStatus.COMPLETED


class TestStepHandlers:
    """Tests for the STEP_HANDLERS registry."""

    def test_all_step_types_have_handlers(self):
        """Verify all step types have registered handlers."""
        for step_type in StepType:
            assert step_type in STEP_HANDLERS, f"Missing handler for {step_type}"

    def test_handler_consistency(self):
        """Test that handlers have required interface."""
        for step_type, handler_func in STEP_HANDLERS.items():
            assert callable(handler_func), f"Handler for {step_type} must be callable"


class TestLLMCallerIntegration:
    """Tests for LLM caller integration with retry logic."""

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_llm_retry_success(self, MockRunner):
        """Test that retries eventually succeed."""
        from .llm_caller import LLMCaller

        # Set up mock runner to fail twice, then succeed
        mock_runner = MagicMock()
        mock_result_fail = MagicMock(success=False, cmd_used="claude", returncode=1)
        mock_result_ok = MagicMock(success=True, output="success")
        mock_runner.run_with_monitor.side_effect = [
            mock_result_fail,
            mock_result_fail,
            mock_result_ok,
        ]
        MockRunner.return_value = mock_runner

        caller = LLMCaller(max_retries=3, retry_delay=0.01)
        result = caller.call(prompt="test prompt")

        assert result == "success"
        assert mock_runner.run_with_monitor.call_count == 3

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_llm_retry_exhausted(self, MockRunner):
        """Test that retry exhaustion raises LLMCallError."""
        from .llm_caller import LLMCaller, LLMCallError

        mock_runner = MagicMock()
        mock_result_fail = MagicMock(success=False, cmd_used="claude", returncode=1)
        mock_runner.run_with_monitor.return_value = mock_result_fail
        MockRunner.return_value = mock_runner

        caller = LLMCaller(max_retries=2, retry_delay=0.01)

        with pytest.raises(LLMCallError):
            caller.call(prompt="test prompt")

        assert mock_runner.run_with_monitor.call_count == 2


class TestContextBuilder:
    """Tests for context builder after dead code removal."""

    def test_dead_methods_removed(self):
        """Verify dead methods are no longer available."""
        from .context_builder import ContextBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            builder = ContextBuilder(Path(tmpdir))
            assert not hasattr(builder, "build_step_context")
            assert not hasattr(builder, "_build_header")
            assert not hasattr(builder, "get_step_prompt_template")

    def test_retained_methods_exist(self):
        """Verify retained methods still work."""
        from .context_builder import ContextBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            builder = ContextBuilder(Path(tmpdir))
            assert hasattr(builder, "specs_dir")
            assert hasattr(builder, "_load_spec_content")


class TestIssueDiscoveryInjection:
    """Tests for get_issue_discovery_injection() function."""

    def test_whitelisted_step_returns_prompt(self, tmp_path):
        """Whitelisted step (summarize) returns non-empty prompt."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

    def test_whitelisted_step_verify_spec(self, tmp_path):
        """Whitelisted step (verify_spec) returns non-empty prompt."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

    def test_non_whitelisted_step_returns_empty(self, tmp_path):
        """Non-whitelisted step (propose) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("propose", tmp_path)
        assert result == ""

    def test_forbidden_step_implement_returns_empty(self, tmp_path):
        """Forbidden step (implement) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("implement", tmp_path)
        assert result == ""

    def test_forbidden_step_test_returns_empty(self, tmp_path):
        """Forbidden step (test) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("test", tmp_path)
        assert result == ""

    def test_custom_whitelist_from_config(self, tmp_path):
        """Custom se3.yaml whitelist is respected."""
        from .context_builder import get_issue_discovery_injection

        # Create se3.yaml with custom whitelist including 'design'
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(
            "issue_discovery:\n  steps:\n    - design\n    - summarize\n"
        )

        # 'design' should now return injection
        result = get_issue_discovery_injection("design", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

        # 'verify_spec' is no longer in custom whitelist
        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result == ""

    def test_forbidden_step_overrides_config(self, tmp_path):
        """Forbidden step returns empty even if config includes it."""
        from .context_builder import get_issue_discovery_injection

        # Create se3.yaml that tries to whitelist forbidden 'implement'
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(
            "issue_discovery:\n  steps:\n    - implement\n    - summarize\n"
        )

        result = get_issue_discovery_injection("implement", tmp_path)
        assert result == ""

    def test_missing_config_uses_defaults(self, tmp_path):
        """Missing se3.yaml uses default whitelist."""
        from .context_builder import get_issue_discovery_injection

        # No se3.yaml exists in tmp_path
        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result != ""
        assert "discovered_issues" in result


class TestSummarizeHandlerIssueDiscoveryIntegration:
    """Integration test: verify summarize handler prompt includes issue discovery."""

    @patch("se3.engine.steps.summarize.LLMCaller")
    def test_summarize_prompt_contains_issue_discovery(self, MockLLMCaller):
        """The actual prompt sent to LLM by summarize handler contains issue discovery text."""
        mock_caller = MagicMock()
        # Return valid NDJSON with summary text
        mock_caller.call.return_value = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Summary here"}]}}'
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        # Set change_path to a temp dir so project_root resolves
        with tempfile.TemporaryDirectory() as tmpdir:
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.SUMMARIZE)
            step.inputs["task_description"] = "Test task"

            from .steps.summarize import summarize_handler
            summarize_handler(step, flow)

            # Verify the prompt sent to LLM contains issue discovery text
            assert mock_caller.call.called
            call_kwargs = mock_caller.call.call_args
            prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt") or call_kwargs[0][0] if call_kwargs[0] else ""
            # The prompt keyword argument
            if not prompt and call_kwargs.kwargs:
                prompt = call_kwargs.kwargs.get("prompt", "")
            assert "discovered_issues" in prompt, (
                "Issue discovery injection was NOT found in the summarize handler prompt. "
                "This means the injection is not reaching the LLM."
            )


class TestBuildStepInputs:
    """Tests for _build_step_inputs ANALYZE mapping and downstream consumption."""

    def _make_state_machine(self, tmp_path):
        """Create a StateMachine with a temp project root."""
        from .state_machine import StateMachine
        return StateMachine(project_root=tmp_path)

    def _make_flow_with_analyze(self, task_desc="Test task", analyze_outputs=None):
        """Create a FlowInstance with a completed ANALYZE step in history."""
        flow = FlowInstance(task_description=task_desc)
        analyze_step = Step(step_type=StepType.ANALYZE)
        analyze_step.status = StepStatus.COMPLETED
        defaults = {
            "task_type": "feature",
            "scope": "backend",
            "project_summary": "Branch: main\nRecent commits:\n  - abc123",
            "relevant_specs": ["base", "flow-engine"],
            "spec_content": {
                "base": "# base spec content",
                "flow-engine": "# flow-engine spec content",
            },
        }
        if analyze_outputs:
            defaults.update(analyze_outputs)
        analyze_step.outputs = defaults
        flow.state.steps[analyze_step.step_id] = analyze_step
        flow.state.step_history.append(analyze_step.step_id)
        return flow

    def test_analyze_mapping_includes_new_fields(self, tmp_path):
        """ANALYZE outputs include spec_content, relevant_specs, project_summary."""
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_analyze()

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["project_summary"] == "Branch: main\nRecent commits:\n  - abc123"
        assert inputs["relevant_specs"] == ["base", "flow-engine"]
        assert inputs["spec_content"] == {
            "base": "# base spec content",
            "flow-engine": "# flow-engine spec content",
        }
        assert inputs["task_type"] == "feature"
        assert inputs["scope"] == "backend"

    def test_plan_step_receives_spec_content_and_project_summary(self, tmp_path):
        """PLAN step inputs include spec_content and project_summary from ANALYZE."""
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_analyze()

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert "spec_content" in inputs
        assert "project_summary" in inputs
        assert "base" in inputs["spec_content"]

    def test_verify_spec_receives_spec_content(self, tmp_path):
        """VERIFY_SPEC step inputs include spec_content from ANALYZE (review flow key path)."""
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_analyze()

        inputs = sm._build_step_inputs(flow, StepType.VERIFY_SPEC)

        assert "spec_content" in inputs
        assert inputs["spec_content"]["base"] == "# base spec content"
        assert inputs["spec_content"]["flow-engine"] == "# flow-engine spec content"

    def test_implement_step_receives_spec_content(self, tmp_path):
        """IMPLEMENT step inputs include spec_content from ANALYZE."""
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_analyze()

        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)

        assert "spec_content" in inputs
        assert "relevant_specs" in inputs

    def test_review_flow_verify_spec_gets_spec_content_without_read_spec(self, tmp_path):
        """In review flow (ANALYZE → VERIFY_SPEC), verify_spec gets spec_content directly from ANALYZE."""
        sm = self._make_state_machine(tmp_path)
        # Simulate review flow: only ANALYZE completed, no READ_SPEC step
        flow = self._make_flow_with_analyze(
            task_desc="Review auth module",
            analyze_outputs={
                "task_type": "review",
                "scope": "auth",
                "spec_content": {"base": "# base", "auth-spec": "# auth spec"},
                "relevant_specs": ["base", "auth-spec"],
                "project_summary": "Branch: review-branch",
            },
        )

        inputs = sm._build_step_inputs(flow, StepType.VERIFY_SPEC)

        # verify_spec must get spec_content even though there's no READ_SPEC step
        assert inputs["spec_content"] == {"base": "# base", "auth-spec": "# auth spec"}
        assert inputs["relevant_specs"] == ["base", "auth-spec"]

    def test_deprecated_project_summary_step_backward_compat(self, tmp_path):
        """Persisted flows with old PROJECT_SUMMARY step still provide project_summary."""
        sm = self._make_state_machine(tmp_path)
        flow = FlowInstance(task_description="Old flow")

        # Simulate old flow with separate PROJECT_SUMMARY step
        ps_step = Step(step_type=StepType.PROJECT_SUMMARY)
        ps_step.status = StepStatus.COMPLETED
        ps_step.outputs = {"project_summary": "Old-style project summary"}
        flow.state.steps[ps_step.step_id] = ps_step
        flow.state.step_history.append(ps_step.step_id)

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["project_summary"] == "Old-style project summary"

    def test_deprecated_read_spec_step_backward_compat(self, tmp_path):
        """Persisted flows with old READ_SPEC step still provide spec_content."""
        sm = self._make_state_machine(tmp_path)
        flow = FlowInstance(task_description="Old flow")

        # Simulate old flow with separate READ_SPEC step
        rs_step = Step(step_type=StepType.READ_SPEC)
        rs_step.status = StepStatus.COMPLETED
        rs_step.outputs = {
            "relevant_specs": ["base"],
            "spec_content": {"base": "# old base content"},
        }
        flow.state.steps[rs_step.step_id] = rs_step
        flow.state.step_history.append(rs_step.step_id)

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["relevant_specs"] == ["base"]
        assert inputs["spec_content"] == {"base": "# old base content"}


class TestStepSequences:
    """Tests verifying step sequences do not contain deprecated PROJECT_SUMMARY/READ_SPEC."""

    def test_all_task_types_exclude_project_summary(self):
        """All 6 task type sequences must not contain PROJECT_SUMMARY."""
        from .models import get_default_step_sequence
        for task_type in ["feature", "bugfix", "review", "small", "directive", "discovery"]:
            seq = get_default_step_sequence(task_type)
            assert StepType.PROJECT_SUMMARY not in seq, (
                f"{task_type} sequence still contains PROJECT_SUMMARY"
            )

    def test_all_task_types_exclude_read_spec(self):
        """All 6 task type sequences must not contain READ_SPEC."""
        from .models import get_default_step_sequence
        for task_type in ["feature", "bugfix", "review", "small", "directive", "discovery"]:
            seq = get_default_step_sequence(task_type)
            assert StepType.READ_SPEC not in seq, (
                f"{task_type} sequence still contains READ_SPEC"
            )

    def test_small_sequence_unchanged(self):
        """Small task sequence: ANALYZE → IMPLEMENT → TEST → VERSION_ANALYZE → COMMIT → SUMMARIZE."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("small")
        expected = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ]
        assert seq == expected

    def test_feature_sequence_starts_with_analyze(self):
        """Feature sequence starts with ANALYZE (not PROJECT_SUMMARY)."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("feature")
        assert seq[0] == StepType.ANALYZE

    def test_review_sequence_has_verify_spec(self):
        """Review sequence: ANALYZE → VERIFY_SPEC → SUMMARIZE (no READ_SPEC between)."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("review")
        assert seq == [
            StepType.ANALYZE,
            StepType.VERIFY_SPEC,
            StepType.SUMMARIZE,
        ]

    def test_discovery_sequence_starts_with_discovery_then_analyze(self):
        """Discovery sequence starts with DISCOVERY → ANALYZE (no PROJECT_SUMMARY/READ_SPEC)."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("discovery")
        assert seq[0] == StepType.DISCOVERY
        assert seq[1] == StepType.ANALYZE
        assert StepType.PROJECT_SUMMARY not in seq
        assert StepType.READ_SPEC not in seq


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
