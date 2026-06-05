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
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "13 步流程池"},
            ],
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
        # selected_specs must NOT appear in outputs (output-side cleanup)
        assert "selected_specs" not in step.outputs

    @patch("se3.engine.steps.analyze.list_spec_names", return_value=["base"])
    @patch("se3.engine.steps.analyze.ProjectContextCollector")
    @patch("se3.engine.steps.analyze.ContextBuilder")
    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_empty_selected_items_loads_base(self, MockLLMCaller, MockContextBuilder, MockCollector, mock_list_specs):
        """Test that base spec is loaded even when LLM returns empty selected_items."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "small",
            "scope": "readme",
            "complexity": "simple",
            "reasoning": "Typo fix",
            "selected_items": [],
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
            "selected_items": [
                {"spec": "nonexistent-spec", "requirement_name": "X"},
                {"spec": "also-fake", "requirement_name": "Y"},
                {"spec": "flow-engine", "requirement_name": "13 步流程池"},
            ],
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

    @patch("se3.engine.steps.analyze.list_spec_names", return_value=["base", "flow-engine"])
    @patch("se3.engine.steps.analyze.ProjectContextCollector")
    @patch("se3.engine.steps.analyze.ContextBuilder")
    @patch("se3.engine.steps.analyze.LLMCaller")
    def test_analyze_outputs_omit_selected_specs(
        self, MockLLMCaller, MockContextBuilder, MockCollector, mock_list_specs
    ):
        """analyze handler MUST NOT write 'selected_specs' to step.outputs.

        The output-side has been migrated to 'selected_items'. This guards
        against accidental re-introduction of the legacy key.
        """
        mock_caller = MagicMock()
        # Even if the LLM emits both keys, only selected_items should appear
        # in step.outputs.
        mock_caller.call.return_value = json.dumps({
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "test",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "13 步流程池"},
            ],
            "selected_specs": ["flow-engine"],
        })
        MockLLMCaller.return_value = mock_caller

        mock_collector_inst = MagicMock()
        mock_collector_inst.collect.return_value = {"git": {"branch": "main"}}
        MockCollector.return_value = mock_collector_inst

        mock_builder = MagicMock()
        mock_builder.specs_dir = Path("/tmp/specs")
        mock_builder._load_spec_content.side_effect = lambda name: f"# {name}"
        MockContextBuilder.return_value = mock_builder

        flow = FlowInstance(task_description="Test")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test"

        result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert "selected_specs" not in step.outputs
        assert "selected_items" in step.outputs

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

    @pytest.fixture(autouse=True)
    def _warm_main_repo_root_cache(self):
        """Pre-warm the git-probe lru_cache before ``@patch('subprocess.Popen')``.

        ``TestConfig.load`` (called at the top of ``test_handler``) resolves the
        main-repo root via ``_resolve_main_repo_root_cached``, which shells out
        to git through ``subprocess.run`` → ``subprocess.Popen``. The tests below
        patch ``subprocess.Popen`` to stub the test-command execution, which also
        corrupts that git probe when its cache is cold — so the tests only pass
        in the full suite (where an earlier test happens to warm the cache) and
        fail in isolation. This autouse fixture runs *before* the patch is
        applied, warming the cache for the current working directory so the
        probe never runs under the patched Popen.
        """
        import se3.config as _cfg

        _cfg._resolve_main_repo_root(Path.cwd())
        yield

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
        """Test handling of test failures.

        pytest exited non-zero but no per-test ``file::test FAILED`` lines are
        parseable from the output, so this is an *unparseable* failure. Under
        the baseline-driven gate (steps/test.py) an unparseable failure is
        treated as introduced (not inherited from any baseline) and triggers
        the fix loop — the handler returns REVISION_NEEDED, not COMPLETED.
        (The old known_test_failures.json exemption that let some failures pass
        through as COMPLETED has been removed; the frozen pre-implement baseline
        is now the sole exemption source, and an empty baseline exempts nothing.)
        """
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate.return_value = ("3 passed, 2 failed", "FAILED test_foo")
        mock_popen.return_value = mock_process

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["tests_passed"] is False
        # An unparseable failure with an empty baseline is an introduced
        # (blocking) failure, so the test step demands a fix.
        assert step.outputs["test_results"]["tests_blocking"] is True


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

    def test_summarize_not_injected_by_default(self, tmp_path):
        """summarize is no longer in the default whitelist (B-class removed),
        so it receives no injection by default."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result == ""

    def test_verify_spec_not_injected_by_default(self, tmp_path):
        """verify_spec was removed from the default whitelist; it files
        out-of-scope issues deterministically instead."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result == ""

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

    def test_missing_config_uses_empty_default(self, tmp_path):
        """Missing se3.yaml uses the default whitelist, which is now empty —
        summarize receives no injection."""
        from .context_builder import get_issue_discovery_injection

        # No se3.yaml exists in tmp_path
        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result == ""

    def test_default_whitelist_is_empty(self):
        """The default whitelist no longer contains any step."""
        from .context_builder import ISSUE_DISCOVERY_DEFAULT_STEPS

        assert ISSUE_DISCOVERY_DEFAULT_STEPS == []


class TestSummarizeHandlerIssueDiscoveryIntegration:
    """Integration test: summarize handler no longer injects issue discovery
    nor produces discovered_issues. Its job is a pure session report."""

    @patch("se3.engine.steps.summarize.LLMCaller")
    def test_summarize_prompt_omits_issue_discovery(self, MockLLMCaller):
        """The prompt sent to LLM by summarize must NOT contain issue discovery
        text, and the step must NOT produce discovered_issues."""
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

            # Verify the prompt sent to LLM does NOT contain issue discovery text
            assert mock_caller.call.called
            call_kwargs = mock_caller.call.call_args
            prompt = call_kwargs.kwargs.get("prompt", "")
            if not prompt and call_kwargs.args:
                prompt = call_kwargs.args[0]
            assert "discovered_issues" not in prompt, (
                "summarize must not inject issue discovery into its prompt."
            )
            # And no discovered_issues output is produced.
            assert "discovered_issues" not in step.outputs


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
        # Simulate review flow: only ANALYZE completed (READ_SPEC removed)
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

        # verify_spec must get spec_content from ANALYZE (READ_SPEC removed)
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

    def test_old_persisted_read_spec_step_type_raises(self):
        """Persisted flows with old READ_SPEC step type cannot be deserialized."""
        with pytest.raises(ValueError):
            StepType("read_spec")


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
        """Review sequence: ANALYZE → VERIFY_SPEC → SUMMARIZE."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("review")
        assert seq == [
            StepType.ANALYZE,
            StepType.VERIFY_SPEC,
            StepType.SUMMARIZE,
        ]

    def test_discovery_sequence_starts_with_discovery_then_analyze(self):
        """Discovery sequence starts with DISCOVERY → ANALYZE (no PROJECT_SUMMARY)."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("discovery")
        assert seq[0] == StepType.DISCOVERY
        assert seq[1] == StepType.ANALYZE
        assert StepType.PROJECT_SUMMARY not in seq


class TestFormatRoundUsageFooter:
    """The shared compact per-round usage footer formatter (G1 task 1)."""

    def test_normal_values_render_round_and_cumulative(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(
            UsageTotals(input_tokens=1234, output_tokens=567),
            UsageTotals(input_tokens=12345, output_tokens=6789),
        )
        assert footer == "本轮 1,234 in / 567 out · 累计 12,345 in / 6,789 out"

    def test_uses_thousands_separators_not_abbreviation(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(
            UsageTotals(input_tokens=1200, output_tokens=3400),
            UsageTotals(input_tokens=1200, output_tokens=3400),
        )
        # Comma thousands separators (consistent with render_usage_block),
        # never a 1.2k-style abbreviation.
        assert "1,200" in footer
        assert "3,400" in footer
        assert "1.2k" not in footer

    def test_zero_values_render_zeros(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(UsageTotals(), UsageTotals())
        assert footer == "本轮 0 in / 0 out · 累计 0 in / 0 out"

    def test_none_inputs_degrade_to_zero(self):
        from .token_usage import format_round_usage_footer

        # The function does not gate on emptiness itself — that is the caller's
        # job — but None must degrade to zeros rather than raise.
        assert format_round_usage_footer(None, None) == (
            "本轮 0 in / 0 out · 累计 0 in / 0 out"
        )

    def test_only_input_output_shown(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(
            UsageTotals(
                input_tokens=10,
                output_tokens=20,
                cache_read_input_tokens=999,
                cache_creation_input_tokens=888,
                total_cost_usd=1.23,
            ),
            UsageTotals(input_tokens=30, output_tokens=40),
        )
        # Per the task copy format only input/output are surfaced — no cache
        # breakdown and no cost in the compact footer.
        assert footer == "本轮 10 in / 20 out · 累计 30 in / 40 out"
        assert "cache" not in footer
        assert "$" not in footer


class TestDiscoveryCarriedTokenUsage:
    """Discovery carries token usage across rounds despite step.outputs.clear().

    Regression for the bug where discovery_handler's mid-handler
    ``step.outputs.clear()`` wiped the ``carried_token_usage`` written by
    run_step's finally block on the previous PAUSED round, so the terminal
    ``token_usage`` only reflected the last round instead of the whole
    discovery's real cumulative total (G1 task 2).
    """

    def test_carried_usage_accumulates_across_rounds_and_clear(self):
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod
        from .token_usage import UsageTotals, add_call_usage

        rounds = [
            (
                UsageTotals(input_tokens=100, output_tokens=10, total_cost_usd=0.01),
                {"mode": "question", "content": "c1", "questions": ["q1?"]},
            ),
            (
                UsageTotals(input_tokens=50, output_tokens=5, total_cost_usd=0.02),
                {"mode": "question", "content": "c2", "questions": ["q2?"]},
            ),
            (
                UsageTotals(input_tokens=30, output_tokens=3, total_cost_usd=0.03),
                {"mode": "confirmation", "content": "c3", "refined_description": "final task"},
            ),
        ]
        calls = {"n": 0}

        def fake_round(*args, **kwargs):
            idx = calls["n"]
            calls["n"] += 1
            usage, result = rounds[idx]
            add_call_usage(usage)
            return result, f"raw {idx}"

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)

            flow = sm.create_flow("multi-round discovery", task_type="discovery")
            step = flow.state.get_current_step()
            assert step.step_type == StepType.DISCOVERY
            step.inputs["task_description"] = "build something"

            with patch.object(discovery_mod, "_run_discovery_round", side_effect=fake_round), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                # Round 1 (question) — PAUSED: carry holds round 1 only.
                sm.run_step(flow, step)
                assert step.status == StepStatus.PAUSED
                assert "token_usage" not in step.outputs
                assert step.outputs["carried_token_usage"]["input_tokens"] == 100

                # Round 2 (question) — PAUSED: carry survives the clear and
                # accumulates round 1 + round 2.
                step.status = StepStatus.PENDING
                step.inputs["resumed"] = True
                step.inputs["user_response"] = "answer 1"
                sm.run_step(flow, step)
                assert step.status == StepStatus.PAUSED
                assert "token_usage" not in step.outputs
                assert step.outputs["carried_token_usage"]["input_tokens"] == 150

                # Round 3 (confirmation gate) — still PAUSED, carry now sums all
                # three rounds.
                step.status = StepStatus.PENDING
                step.inputs["user_response"] = "answer 2"
                sm.run_step(flow, step)
                assert step.status == StepStatus.PAUSED
                assert step.outputs.get("awaiting_programmatic_confirm") is True
                assert step.outputs["carried_token_usage"]["input_tokens"] == 180

            # Final round: user confirms via the programmatic gate (no LLM call).
            # The terminal token_usage must reflect the FULL cumulative total of
            # every round, and the carry is cleared.
            step.status = StepStatus.PENDING
            step.inputs["programmatic_confirmed"] = True
            sm.run_step(flow, step)
            assert step.status == StepStatus.COMPLETED
            assert "carried_token_usage" not in step.outputs
            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 180  # 100 + 50 + 30
            assert tu["output_tokens"] == 18  # 10 + 5 + 3
            assert tu["total_cost_usd"] == pytest.approx(0.06)  # 0.01+0.02+0.03

            # The CLI authoritative session total folds every run independently
            # and must agree with the rolled-up terminal record.
            su = flow.state.session_token_usage
            assert su.input_tokens == 180
            assert su.total_cost_usd == pytest.approx(0.06)


class TestDiscoveryRoundUsageFooter:
    """Per-round CLI usage footer rendering and gating (G2 tasks 1-3).

    The discovery message block appends a compact dim ``本轮 … · 累计 …`` footer
    only when this round actually invoked the LLM (a non-empty round usage); an
    empty / ``None`` round usage (empty-input redraw, ``--resume`` re-display)
    must render no footer.
    """

    def _render_to_text(self, **kwargs) -> str:
        import io

        from rich.console import Console

        from . import display
        from .steps import discovery as discovery_mod

        buf = Console(file=io.StringIO(), width=200, record=True)
        prev = display.get_console()
        display.set_console(buf)
        try:
            discovery_mod._display_discovery_message("hello world", None, **kwargs)
        finally:
            display.set_console(prev)
        return buf.export_text()

    def test_footer_rendered_when_round_usage_non_empty(self):
        from .token_usage import UsageTotals

        out = self._render_to_text(
            round_usage=UsageTotals(input_tokens=1234, output_tokens=567),
            cumulative_usage=UsageTotals(input_tokens=12345, output_tokens=6789),
        )
        assert "本轮 1,234 in / 567 out · 累计 12,345 in / 6,789 out" in out

    def test_no_footer_when_round_usage_none(self):
        out = self._render_to_text()
        assert "本轮" not in out
        assert "累计" not in out

    def test_no_footer_when_round_usage_empty(self):
        from .token_usage import UsageTotals

        out = self._render_to_text(
            round_usage=UsageTotals(),
            cumulative_usage=UsageTotals(input_tokens=100, output_tokens=10),
        )
        # An empty round increment (no LLM call this round) suppresses the footer
        # even though a cumulative total exists.
        assert "本轮" not in out
        assert "累计" not in out

    def test_handler_passes_round_and_cumulative_usage(self):
        """discovery_handler computes round=current_step_usage(), cumulative=carried+round."""
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod
        from .token_usage import UsageTotals, add_call_usage

        captured = {}

        def fake_display(*args, **kwargs):
            captured["round_usage"] = kwargs.get("round_usage")
            captured["cumulative_usage"] = kwargs.get("cumulative_usage")

        def fake_round(*args, **kwargs):
            add_call_usage(UsageTotals(input_tokens=30, output_tokens=3))
            return (
                {"mode": "question", "content": "c2", "questions": ["q2?"]},
                "raw 2",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)

            flow = sm.create_flow("usage footer discovery", task_type="discovery")
            step = flow.state.get_current_step()
            step.inputs["task_description"] = "build something"
            # Simulate a prior round's carried cumulative total surviving the
            # outputs.clear() inside the handler.
            step.outputs["carried_token_usage"] = UsageTotals(
                input_tokens=100, output_tokens=10
            ).to_dict()
            step.inputs["resumed"] = True
            step.inputs["user_response"] = "answer 1"

            with patch.object(discovery_mod, "_run_discovery_round", side_effect=fake_round), \
                 patch.object(discovery_mod, "_display_discovery_message", side_effect=fake_display):
                sm.run_step(flow, step)

        assert step.status == StepStatus.PAUSED
        # This round's increment only.
        assert captured["round_usage"].input_tokens == 30
        assert captured["round_usage"].output_tokens == 3
        # Carried prior total (100/10) + this round (30/3).
        assert captured["cumulative_usage"].input_tokens == 130
        assert captured["cumulative_usage"].output_tokens == 13

    def test_programmatic_confirm_path_renders_no_footer(self):
        """The programmatic-confirmed early return makes no LLM call and no display."""
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)

            flow = sm.create_flow("confirm discovery", task_type="discovery")
            step = flow.state.get_current_step()
            step.inputs["task_description"] = "build something"
            step.inputs["programmatic_confirmed"] = True

            with patch.object(discovery_mod, "_display_discovery_message") as mock_display:
                sm.run_step(flow, step)

            assert step.status == StepStatus.COMPLETED
            # No footer / message rendered on the confirm fast-path.
            mock_display.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
