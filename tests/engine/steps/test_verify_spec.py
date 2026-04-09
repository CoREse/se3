"""Tests for the verify_spec step handler."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from se3.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus, State
from se3.engine.steps.verify_spec import (
    verify_spec_handler,
    _format_spec_content,
    _format_changes,
    _format_test_results,
    _format_fix_context,
    _format_spec_changes,
    _get_max_fix_iterations,
    _file_out_of_scope_issues,
    VERIFY_PROMPT,
)


class TestVerifySpecHandler:
    """Test cases for verify_spec_handler."""

    @pytest.fixture
    def flow(self, tmp_path):
        """Create a test flow instance."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]
        return flow

    @pytest.fixture
    def step(self):
        """Create a test step."""
        return Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {"spec.md": "Test spec content"},
                "changes_made": {"files_changed": [{"path": "test.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0, "stdout": "All tests passed"},
            },
        )

    @pytest.fixture
    def mock_llm_response(self):
        """Create a mock LLM response with no issues (passes verification)."""
        return json.dumps({
            "issues": [],
            "summary": "All good",
            "recommendations": [],
            "test_analysis": {"tests_passed": True, "failure_summary": "", "root_cause": ""},
            "fix_instructions": "",
        })

    def test_verify_prompt_uses_priority_not_severity(self):
        """Test that VERIFY_PROMPT uses priority (critical|high|medium|low) instead of severity."""
        assert "critical|high|medium|low" in VERIFY_PROMPT
        assert '"priority":' in VERIFY_PROMPT
        # severity should no longer be in the JSON schema
        assert '"severity":' not in VERIFY_PROMPT

    def test_verify_prompt_includes_scope_definitions(self):
        """Test that VERIFY_PROMPT includes scope (in_scope|out_of_scope) definitions."""
        assert "in_scope|out_of_scope" in VERIFY_PROMPT
        assert '"scope":' in VERIFY_PROMPT
        assert "### Issue Scope" in VERIFY_PROMPT

    def test_verify_prompt_includes_priority_definitions(self):
        """Test that VERIFY_PROMPT includes priority level definitions."""
        assert "### Issue Priority Levels" in VERIFY_PROMPT
        assert "**critical**:" in VERIFY_PROMPT
        assert "**high**:" in VERIFY_PROMPT
        assert "**medium**:" in VERIFY_PROMPT
        assert "**low**:" in VERIFY_PROMPT

    def test_verify_prompt_includes_test_failure_analysis(self):
        """Test that VERIFY_PROMPT includes test failure analysis instructions."""
        assert "Test Failure Analysis" in VERIFY_PROMPT
        assert "test_analysis" in VERIFY_PROMPT
        assert "fix_instructions" in VERIFY_PROMPT
        assert "fix_context" in VERIFY_PROMPT

    def test_verify_prompt_includes_spec_changes_placeholder(self):
        """Test that VERIFY_PROMPT includes {spec_changes} placeholder."""
        assert "{spec_changes}" in VERIFY_PROMPT

    def test_verify_prompt_includes_planned_changes_instruction(self):
        """Test that VERIFY_PROMPT instruction 6 covers planned spec changes judgment rule."""
        assert "Planned Spec Changes" in VERIFY_PROMPT
        assert "intentional" in VERIFY_PROMPT

    def test_verify_prompt_no_verified_field_in_schema(self):
        """Test that VERIFY_PROMPT JSON schema does not ask for 'verified' from LLM."""
        # The JSON example block should not contain "verified" as an output field
        # We check the JSON block specifically (between ```json and ```)
        json_block_start = VERIFY_PROMPT.index('```json')
        json_block_end = VERIFY_PROMPT.index('```', json_block_start + 7)
        json_block = VERIFY_PROMPT[json_block_start:json_block_end]
        assert '"verified"' not in json_block

    def test_handler_returns_completed_when_no_issues(self, flow, step, mock_llm_response):
        """Test that handler returns COMPLETED when no issues found."""
        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_llm_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs["verified"] is True
            assert step.outputs["in_scope_count"] == 0
            assert step.outputs["out_of_scope_count"] == 0

    def test_verified_is_rule_based_not_from_llm(self, flow, step):
        """Test that verified is computed from rule, ignoring LLM's verified field."""
        # LLM says verified=True but has in_scope issues → rule says False
        mock_response = json.dumps({
            "verified": True,  # LLM says True, but should be ignored
            "issues": [
                {"priority": "high", "scope": "in_scope", "message": "Missing implementation"}
            ],
            "summary": "Has issues",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "Fix it",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            # verified should be False because there's an in_scope issue
            assert step.outputs["verified"] is False
            assert result == StepStatus.REVISION_NEEDED

    def test_verified_true_when_only_out_of_scope_issues(self, flow, step):
        """Test that verified=True when all issues are out_of_scope."""
        mock_response = json.dumps({
            "issues": [
                {"priority": "high", "scope": "out_of_scope", "message": "Pre-existing bug"},
                {"priority": "medium", "scope": "out_of_scope", "message": "Old tech debt"},
            ],
            "summary": "Only pre-existing issues",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs["verified"] is True
            assert step.outputs["in_scope_count"] == 0
            assert step.outputs["out_of_scope_count"] == 2

    def test_in_scope_issue_triggers_revision_needed(self, flow, step):
        """Test that any in_scope issue triggers REVISION_NEEDED."""
        step.inputs["fix_iteration"] = 0

        mock_response = json.dumps({
            "issues": [
                {"priority": "low", "scope": "in_scope", "message": "Minor in-scope issue"},
            ],
            "summary": "Has in-scope issue",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "Fix minor issue",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                result = verify_spec_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["fix_needed"] is True
            assert step.outputs["in_scope_count"] == 1

    def test_handler_returns_revision_needed_when_tests_fail_and_under_max_iterations(self, flow, step):
        """Test that handler returns REVISION_NEEDED when tests fail and under max iterations."""
        step.inputs["test_results"] = {"passed": False, "returncode": 1, "stdout": "Test failed", "stderr": "AssertionError"}
        step.inputs["fix_iteration"] = 0

        mock_response = json.dumps({
            "issues": [{"priority": "high", "scope": "in_scope", "message": "Tests failed"}],
            "summary": "Tests failed - fix needed",
            "recommendations": ["Fix the test"],
            "test_analysis": {"tests_passed": False, "failure_summary": "Assertion error", "root_cause": "Bug in code"},
            "fix_instructions": "Fix the assertion in test.py line 10",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                result = verify_spec_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["fix_needed"] is True
            assert step.outputs["fix_instructions"] == "Fix the assertion in test.py line 10"
            assert step.outputs["fix_context"]["iteration"] == 1

    def test_handler_returns_completed_when_max_iterations_reached(self, flow, step):
        """Test that handler returns COMPLETED with warning when max iterations reached."""
        step.inputs["test_results"] = {"passed": False, "returncode": 1, "stdout": "Test failed", "stderr": "AssertionError"}
        step.inputs["fix_iteration"] = 3  # At max iterations

        mock_response = json.dumps({
            "issues": [{"priority": "high", "scope": "in_scope", "message": "Tests failed"}],
            "summary": "Tests still failing",
            "recommendations": [],
            "test_analysis": {"tests_passed": False, "failure_summary": "Still failing", "root_cause": "Unknown"},
            "fix_instructions": "Keep trying",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._get_max_fix_iterations", return_value=3):
                with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                    result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs.get("max_iterations_reached") is True
            assert "3" in step.outputs.get("warning", "")

    def test_handler_reads_fix_iteration_from_inputs(self, flow, step):
        """Test that handler reads fix_iteration from step inputs."""
        step.inputs["fix_iteration"] = 2
        step.inputs["test_results"] = {"passed": False, "returncode": 1, "stdout": "Failed", "stderr": ""}

        mock_response = json.dumps({
            "issues": [],
            "summary": "",
            "recommendations": [],
            "test_analysis": {"tests_passed": False, "failure_summary": "", "root_cause": ""},
            "fix_instructions": "Fix it",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                verify_spec_handler(step, flow)

            # Should include iteration 2 in prompt
            call_args = mock_caller.call.call_args
            prompt = call_args[1]["prompt"]
            assert "Fix iteration: 2" in prompt or "fix iteration: 2" in prompt.lower()

    def test_handler_stores_fix_context_in_outputs(self, flow, step):
        """Test that handler stores fix_context in step outputs when tests fail."""
        test_results = {"passed": False, "returncode": 1, "stdout": "Test error", "stderr": ""}
        step.inputs["test_results"] = test_results
        step.inputs["fix_iteration"] = 1

        mock_response = json.dumps({
            "issues": [],
            "summary": "",
            "recommendations": [],
            "test_analysis": {"tests_passed": False, "failure_summary": "Summary", "root_cause": "Root cause"},
            "fix_instructions": "Instructions here",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                verify_spec_handler(step, flow)

            fix_context = step.outputs.get("fix_context")
            assert fix_context is not None
            assert fix_context["test_results"] == test_results
            assert fix_context["test_analysis"]["tests_passed"] is False
            assert fix_context["fix_instructions"] == "Instructions here"
            assert fix_context["iteration"] == 2  # Incremented

    def test_out_of_scope_issues_filed(self, flow, step):
        """Test that out-of-scope issues are filed via IssueManager."""
        mock_response = json.dumps({
            "issues": [
                {"priority": "medium", "scope": "out_of_scope", "message": "Pre-existing bug in auth", "suggestion": "Refactor auth module"},
            ],
            "summary": "All in-scope checks passed",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues") as mock_file:
                result = verify_spec_handler(step, flow)

                assert result == StepStatus.COMPLETED
                assert step.outputs["verified"] is True
                # Verify _file_out_of_scope_issues was called with the out-of-scope issues
                mock_file.assert_called_once()
                filed_issues = mock_file.call_args[0][0]
                assert len(filed_issues) == 1
                assert filed_issues[0]["scope"] == "out_of_scope"

    def test_mixed_scope_issues(self, flow, step):
        """Test handling of mixed in_scope and out_of_scope issues."""
        step.inputs["fix_iteration"] = 0

        mock_response = json.dumps({
            "issues": [
                {"priority": "high", "scope": "in_scope", "message": "Missing error handling"},
                {"priority": "medium", "scope": "out_of_scope", "message": "Old tech debt"},
                {"priority": "low", "scope": "out_of_scope", "message": "Style issue"},
            ],
            "summary": "Mixed issues found",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "Add error handling",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                result = verify_spec_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["in_scope_count"] == 1
            assert step.outputs["out_of_scope_count"] == 2
            assert step.outputs["verified"] is False

    def test_scope_defaults_to_in_scope(self, flow, step):
        """Test that issues without explicit scope default to in_scope."""
        step.inputs["fix_iteration"] = 0

        mock_response = json.dumps({
            "issues": [
                {"priority": "high", "message": "No scope specified"},
            ],
            "summary": "Issue without scope",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "Fix it",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                result = verify_spec_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["in_scope_count"] == 1

    def test_max_iterations_reached_with_in_scope_issues(self, flow, step):
        """Test that max iterations reached with in-scope issues completes with warning."""
        step.inputs["fix_iteration"] = 3

        mock_response = json.dumps({
            "issues": [
                {"priority": "high", "scope": "in_scope", "message": "Persistent issue"},
            ],
            "summary": "Still has issues",
            "recommendations": [],
            "test_analysis": {"tests_passed": True},
            "fix_instructions": "Need more work",
        })

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._get_max_fix_iterations", return_value=3):
                with patch("se3.engine.steps.verify_spec._file_out_of_scope_issues"):
                    result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs.get("max_iterations_reached") is True
            assert step.outputs["in_scope_count"] == 1


class TestFileOutOfScopeIssues:
    """Test cases for _file_out_of_scope_issues."""

    def test_files_issues_via_issue_manager(self, tmp_path):
        """Test that out-of-scope issues are filed as YAML issue files."""
        (tmp_path / "se3" / "issues" / "open").mkdir(parents=True)
        (tmp_path / "se3" / "issues" / "closed").mkdir(parents=True)

        flow = FlowInstance(
            flow_id="test-flow",
            task_description="Test",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test",
        )

        issues = [
            {"priority": "medium", "scope": "out_of_scope", "message": "Pre-existing bug", "suggestion": "Fix it"},
            {"priority": "low", "scope": "out_of_scope", "message": "Style issue"},
        ]

        _file_out_of_scope_issues(issues, flow, tmp_path)

        # Verify files were created
        from se3.engine.issue_manager import IssueManager
        mgr = IssueManager(tmp_path)
        created = mgr.list_issues()
        assert len(created) == 2
        assert created[0].scope == "out_of_scope"
        assert created[1].scope == "out_of_scope"
        assert "auto-discovered" in created[0].tags
        assert "source:verify-spec" in created[0].tags
        assert "out-of-scope" in created[0].tags

    def test_empty_list_is_noop(self, tmp_path):
        """Test that empty out-of-scope list does nothing."""
        flow = Mock()
        _file_out_of_scope_issues([], flow, tmp_path)
        # No error, no files created

    def test_handles_exception_gracefully(self, tmp_path):
        """Test that filing errors are caught and logged, not raised."""
        flow = Mock()
        # tmp_path doesn't have issue dirs, IssueManager.create will fail
        # but _file_out_of_scope_issues should handle it gracefully
        issues = [{"priority": "high", "scope": "out_of_scope", "message": "Test"}]
        # Should not raise
        _file_out_of_scope_issues(issues, flow, tmp_path / "nonexistent")


class TestFormatSpecContent:
    """Test cases for _format_spec_content."""

    def test_empty_content(self):
        assert _format_spec_content({}) == "No specifications provided."

    def test_single_spec(self):
        content = {"spec.md": "This is the spec content"}
        result = _format_spec_content(content)
        assert "### spec.md" in result
        assert "This is the spec content" in result

    def test_multiple_specs(self):
        content = {"spec1.md": "Content 1", "spec2.md": "Content 2"}
        result = _format_spec_content(content)
        assert "### spec1.md" in result
        assert "### spec2.md" in result
        assert "Content 1" in result
        assert "Content 2" in result

    def test_long_content_not_truncated(self):
        long_content = "x" * 4000
        content = {"long.md": long_content}
        result = _format_spec_content(content)
        assert long_content in result


class TestFormatChanges:
    """Test cases for _format_changes."""

    def test_empty_changes(self):
        assert _format_changes({}) == "No changes recorded."

    def test_files_changed(self):
        changes = {
            "files_changed": [
                {"path": "file1.py", "action": "modify", "explanation": "Fixed bug"},
                {"path": "file2.py", "action": "create"},
            ]
        }
        result = _format_changes(changes)
        assert "modify: file1.py" in result
        assert "(Fixed bug)" in result
        assert "create: file2.py" in result

    def test_no_files_changed(self):
        assert _format_changes({"files_changed": []}) == "Changes made but details unavailable."


class TestFormatTestResults:
    """Test cases for _format_test_results."""

    def test_no_results(self):
        assert _format_test_results({}) == "No test results available."

    def test_passed_tests(self):
        results = {"passed": True, "returncode": 0, "stdout": "All good", "stderr": ""}
        result = _format_test_results(results)
        assert "Tests passed: True" in result
        assert "All good" in result

    def test_failed_tests(self):
        results = {"passed": False, "returncode": 1, "stdout": "Output", "stderr": "Error"}
        result = _format_test_results(results)
        assert "Tests passed: False" in result
        assert "Output" in result
        assert "Error" in result

    def test_stdout_truncation(self):
        long_stdout = "x" * 1500
        results = {"passed": True, "returncode": 0, "stdout": long_stdout, "stderr": ""}
        result = _format_test_results(results)
        # Should include last 1000 chars
        assert len(result) < len(long_stdout) + 200


class TestFormatFixContext:
    """Test cases for _format_fix_context."""

    def test_initial_iteration(self):
        result = _format_fix_context(0, 3)
        assert "initial verification" in result.lower()
        assert "no previous fix attempts" in result.lower()

    def test_fix_iteration(self):
        result = _format_fix_context(2, 3)
        assert "Fix iteration: 2 of 3" in result
        assert "Previous fix attempts: 2" in result

    def test_max_iterations_warning(self):
        result = _format_fix_context(3, 3)
        assert "WARNING" in result
        assert "final fix attempt" in result


class TestFormatSpecChanges:
    """Test cases for _format_spec_changes."""

    def test_empty_list(self):
        assert _format_spec_changes([]) == "No planned spec changes."

    def test_none_input(self):
        assert _format_spec_changes(None) == "No planned spec changes."

    def test_single_change(self):
        changes = [
            {
                "spec_name": "flow-engine",
                "change_type": "add_requirement",
                "target": "Requirement: New Feature",
                "description": "Add new feature requirement",
            }
        ]
        result = _format_spec_changes(changes)
        assert "- [add_requirement] flow-engine :: Requirement: New Feature" in result
        assert "  Add new feature requirement" in result

    def test_multiple_changes(self):
        changes = [
            {
                "spec_name": "flow-engine",
                "change_type": "add_requirement",
                "target": "Requirement: A",
                "description": "First change",
            },
            {
                "spec_name": "se3-workflows",
                "change_type": "modify_requirement",
                "target": "Requirement: B",
                "description": "Second change",
            },
        ]
        result = _format_spec_changes(changes)
        assert "[add_requirement] flow-engine :: Requirement: A" in result
        assert "[modify_requirement] se3-workflows :: Requirement: B" in result
        assert "First change" in result
        assert "Second change" in result

    def test_change_without_description(self):
        changes = [
            {
                "spec_name": "spec",
                "change_type": "deprecate_requirement",
                "target": "Requirement: Old",
            }
        ]
        result = _format_spec_changes(changes)
        assert "- [deprecate_requirement] spec :: Requirement: Old" in result
        # No indented description line
        assert result.count("\n") == 0


class TestGetMaxFixIterations:
    """Test cases for _get_max_fix_iterations."""

    def test_from_flow_context(self, tmp_path):
        flow = Mock()
        flow.state.context = {"max_fix_iterations": 5}
        flow.change_path = tmp_path

        result = _get_max_fix_iterations(flow)
        assert result == 5

    def test_default_value(self, tmp_path):
        flow = Mock()
        flow.state.context = {}
        flow.change_path = tmp_path / "nonexistent"

        result = _get_max_fix_iterations(flow)
        assert result == 3  # Default

    def test_from_config_file(self, tmp_path):
        # Create project root and change path
        project_root = tmp_path
        change_path = project_root / "openspec" / "changes" / "test-change"
        change_path.mkdir(parents=True)

        flow = Mock()
        flow.state.context = {"project_root": str(project_root)}
        flow.change_path = change_path

        # Create se3.yaml with custom max_fix_iterations in project root
        config = """
workflow:
  max_fix_iterations: 7
"""
        (project_root / "se3.yaml").write_text(config)

        result = _get_max_fix_iterations(flow)
        assert result == 7


class TestIntegration:
    """Integration tests for verify_spec step."""

    def test_prompt_includes_all_sections(self, tmp_path):
        """Test that the prompt includes all required sections."""
        # Create test flow and step locally
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {"spec.md": "Test spec content"},
                "changes_made": {"files_changed": [{"path": "test.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0, "stdout": "All tests passed"},
            },
        )

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"issues": []}'
            mock_caller_class.return_value = mock_caller

            verify_spec_handler(step, flow)

            call_args = mock_caller.call.call_args
            prompt = call_args[1]["prompt"]

            # Check all sections are present
            assert "## Task Description" in prompt
            assert "## Relevant Specifications" in prompt
            assert "## Changes Made" in prompt
            assert "## Planned Spec Changes" in prompt
            assert "## Test Results" in prompt
            assert "## Fix Context" in prompt
            assert "### Test Failure Analysis" in prompt

    def test_prompt_includes_spec_changes_when_provided(self, tmp_path):
        """Test that spec_changes from inputs are injected into the prompt."""
        flow = FlowInstance(
            flow_id="test-flow-456",
            task_description="Feature task",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [StepType.VERIFY_SPEC]

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Feature task",
                "spec_content": {"spec.md": "Spec content"},
                "changes_made": {"files_changed": []},
                "test_results": {"passed": True, "returncode": 0, "stdout": "OK"},
                "spec_changes": [
                    {
                        "spec_name": "flow-engine",
                        "change_type": "add_requirement",
                        "target": "Requirement: Plan spec_changes Output",
                        "description": "New output field for spec change intent",
                    }
                ],
            },
        )

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"issues": []}'
            mock_caller_class.return_value = mock_caller

            verify_spec_handler(step, flow)

            call_args = mock_caller.call.call_args
            prompt = call_args[1]["prompt"]

            assert "## Planned Spec Changes" in prompt
            assert "[add_requirement] flow-engine :: Requirement: Plan spec_changes Output" in prompt
            assert "New output field for spec change intent" in prompt

    def test_prompt_shows_no_planned_changes_when_empty(self, tmp_path):
        """Test that empty spec_changes results in 'No planned spec changes.' in prompt."""
        flow = FlowInstance(
            flow_id="test-flow-789",
            task_description="Bugfix task",
            task_type="bugfix",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [StepType.VERIFY_SPEC]

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Bugfix task",
                "spec_content": {},
                "changes_made": {},
                "test_results": {},
            },
        )

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"issues": []}'
            mock_caller_class.return_value = mock_caller

            verify_spec_handler(step, flow)

            call_args = mock_caller.call.call_args
            prompt = call_args[1]["prompt"]

            assert "## Planned Spec Changes" in prompt
            assert "No planned spec changes." in prompt
