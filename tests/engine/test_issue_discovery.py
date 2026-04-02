"""Tests for the IssueDiscovery module.

Covers A-class triggers (fix loop exhaustion), B-class injection/collection,
deduplication, priority mapping, and tag management.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from se3.engine.issue_discovery import (
    IssueDiscovery,
    ISSUE_DISCOVERY_STEPS,
    ISSUE_FORBIDDEN_STEPS,
    ISSUE_DISCOVERY_PROMPT,
)
from se3.engine.issue_manager import Issue, IssueManager, IssueStatus
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project directory."""
    (tmp_path / "se3" / "issues" / "open").mkdir(parents=True)
    (tmp_path / "se3" / "issues" / "closed").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def issue_manager(project_root):
    return IssueManager(project_root)


@pytest.fixture
def discovery(issue_manager):
    return IssueDiscovery(issue_manager, flow_id="test-flow-001")


@pytest.fixture
def basic_flow():
    """Create a basic flow instance for testing."""
    flow = FlowInstance(
        flow_id="test-flow-001",
        task_description="Implement user authentication",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [StepType.IMPLEMENT, StepType.TEST, StepType.VERIFY_SPEC]
    return flow


@pytest.fixture
def flow_with_fix_history(basic_flow):
    """Create a flow with fix history."""
    basic_flow.state.fix_iterations = 3
    basic_flow.state.fix_history = [
        {"iteration": 1, "timestamp": datetime.now().isoformat(), "context": {"reason": "test_failure"}},
        {"iteration": 2, "timestamp": datetime.now().isoformat(), "context": {"reason": "test_failure"}},
        {"iteration": 3, "timestamp": datetime.now().isoformat(), "context": {"reason": "test_failure"}},
    ]
    return basic_flow


class TestCreateFromFixLoopExhaustion:
    """Tests for A-class trigger: fix loop exhaustion."""

    def test_creates_high_priority_issue(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {
            "fix_needed": True,
            "fix_context": {"test_results": {"passed": False, "stdout": "FAILED test_auth"}},
        }

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert issue.priority == "high"
        assert "auto-discovered" in issue.tags
        assert "source:fix-loop" in issue.tags

    def test_title_contains_task_description(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.TEST, status=StepStatus.COMPLETED)
        trigger.outputs = {}

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert "Implement user authentication" in issue.title

    def test_includes_fix_history(self, discovery, flow_with_fix_history):
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue = discovery.create_from_fix_loop_exhaustion(flow_with_fix_history, trigger)

        assert issue is not None
        assert "Fix attempt history" in issue.description
        assert "test_failure" in issue.description

    def test_includes_test_output(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {
            "fix_context": {
                "test_results": {"passed": False, "stdout": "AssertionError: expected 42 got 0"}
            },
        }

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert "AssertionError" in issue.description

    def test_includes_fix_instructions(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.TEST, status=StepStatus.COMPLETED)
        trigger.outputs = {
            "fix_instructions": "Fix the auth token validation logic",
            "fix_context": {},
        }

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert "Fix the auth token validation" in issue.description

    def test_deduplicates(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue1 = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)
        issue2 = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue1 is not None
        assert issue2 is None  # Deduplicated


class TestGetInjectionPrompt:
    """Tests for B-class prompt injection."""

    def test_whitelist_steps_return_prompt(self):
        for step_type in ["verify_spec", "summarize"]:
            result = IssueDiscovery.get_injection_prompt(step_type)
            assert result is not None
            assert "discovered_issues" in result

    def test_forbidden_steps_return_none(self):
        for step_type in ["implement", "test"]:
            result = IssueDiscovery.get_injection_prompt(step_type)
            assert result is None

    def test_other_steps_return_none(self):
        for step_type in ["analyze", "plan", "commit"]:
            result = IssueDiscovery.get_injection_prompt(step_type)
            assert result is None

    def test_prompt_contains_json_format(self):
        prompt = IssueDiscovery.get_injection_prompt("verify_spec")
        assert "title" in prompt
        assert "description" in prompt
        assert "priority_hint" in prompt


class TestCollectIssuesFromOutput:
    """Tests for B-class issue collection."""

    def test_verify_spec_warning_maps_to_medium(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Missing logging", "description": "No logging in auth module", "priority_hint": "warning"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "medium"

    def test_verify_spec_info_maps_to_low(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Consider caching", "description": "Auth could use caching", "priority_hint": "info"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "low"

    def test_verify_spec_error_maps_to_high(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Security flaw", "description": "Token not validated", "priority_hint": "error"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "high"

    def test_summarize_default_priority(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "TODO cleanup", "description": "Several TODO comments left", "priority_hint": "info"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "low"

    def test_adds_correct_tags(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Test issue", "description": "desc", "priority_hint": "warning"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 1
        assert "auto-discovered" in issues[0].tags
        assert "source:verify-spec" in issues[0].tags

    def test_summarize_source_tag(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Note", "description": "A note", "priority_hint": "info"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert "source:summarize" in issues[0].tags

    def test_multiple_issues(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Issue A", "description": "desc A", "priority_hint": "warning"},
                {"title": "Issue B", "description": "desc B", "priority_hint": "info"},
                {"title": "Issue C", "description": "desc C", "priority_hint": "error"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 3

    def test_empty_discovered_issues(self, discovery, basic_flow):
        outputs = {"discovered_issues": []}

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 0

    def test_no_discovered_issues_key(self, discovery, basic_flow):
        outputs = {"some_other_key": "value"}

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 0

    def test_malformed_items_skipped(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                "not a dict",
                {"no_title": "missing title field"},
                {"title": "", "description": "empty title"},
                {"title": "Valid issue", "description": "This is valid", "priority_hint": "warning"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 1
        assert issues[0].title == "Valid issue"

    def test_non_whitelist_step_returns_empty(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Sneaky issue", "description": "desc", "priority_hint": "warning"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "implement", outputs)

        assert len(issues) == 0


class TestDeduplication:
    """Tests for title-based deduplication."""

    def test_exact_duplicate_blocked(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Missing error handling", "description": "desc1", "priority_hint": "warning"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)
        issues2 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues1) == 1
        assert len(issues2) == 0

    def test_case_insensitive_dedup(self, discovery, basic_flow):
        outputs1 = {
            "discovered_issues": [
                {"title": "Missing Error Handling", "description": "d1", "priority_hint": "warning"},
            ]
        }
        outputs2 = {
            "discovered_issues": [
                {"title": "missing error handling", "description": "d2", "priority_hint": "warning"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs1)
        issues2 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs2)

        assert len(issues1) == 1
        assert len(issues2) == 0

    def test_punctuation_insensitive_dedup(self, discovery, basic_flow):
        outputs1 = {
            "discovered_issues": [
                {"title": "Missing: error handling!", "description": "d1", "priority_hint": "warning"},
            ]
        }
        outputs2 = {
            "discovered_issues": [
                {"title": "Missing error handling", "description": "d2", "priority_hint": "warning"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs1)
        issues2 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs2)

        assert len(issues1) == 1
        assert len(issues2) == 0

    def test_different_titles_not_deduped(self, discovery, basic_flow):
        outputs1 = {
            "discovered_issues": [
                {"title": "Missing error handling", "description": "d1", "priority_hint": "warning"},
            ]
        }
        outputs2 = {
            "discovered_issues": [
                {"title": "Security vulnerability in auth", "description": "d2", "priority_hint": "warning"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs1)
        issues2 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs2)

        assert len(issues1) == 1
        assert len(issues2) == 1

    def test_cross_mechanism_dedup(self, discovery, basic_flow, flow_with_fix_history):
        """A-class and B-class issues with same title should deduplicate."""
        # First create via A-class
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}
        issue1 = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        # Then try B-class with similar title
        outputs = {
            "discovered_issues": [
                {"title": issue1.title, "description": "dup", "priority_hint": "warning"},
            ]
        }
        issues2 = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert issue1 is not None
        assert len(issues2) == 0


class TestStateMachineIntegration:
    """Integration tests for IssueDiscovery with StateMachine."""

    def test_fix_loop_exhaustion_creates_issue(self, project_root):
        """When fix loop reaches max iterations, state machine creates an issue."""
        from se3.engine.state_machine import StateMachine

        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=project_root)

        # Create a flow at max iterations
        flow = FlowInstance(
            flow_id="test-flow-int",
            task_description="Fix the login bug",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT, StepType.TEST, StepType.VERIFY_SPEC,
            StepType.COMMIT, StepType.SUMMARIZE,
        ]

        # Add steps
        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
        flow.state.add_step(impl_step)
        flow.state.step_history.append(impl_step.step_id)

        test_step = Step(step_type=StepType.TEST, status=StepStatus.COMPLETED)
        flow.state.add_step(test_step)
        flow.state.step_history.append(test_step.step_id)

        verify_step = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.REVISION_NEEDED)
        verify_step.outputs = {
            "fix_needed": True,
            "fix_context": {"test_failed": True},
        }
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id
        flow.state.current_step_index = 2

        # Set fix iterations to max
        flow.state.fix_iterations = 3

        with patch.object(sm, '_get_max_fix_iterations', return_value=3):
            with patch.object(sm.persistence, 'save_flow'):
                next_step = sm.transition_to_next(flow)

        # Verify an issue was created
        mgr = IssueManager(project_root)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert issues[0].priority == "high"
        assert "source:fix-loop" in issues[0].tags

    def test_step_completion_collects_issues(self, project_root):
        """When a whitelist step completes with discovered_issues, they are collected."""
        from se3.engine.state_machine import StateMachine

        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=project_root)

        flow = FlowInstance(
            flow_id="test-flow-collect",
            task_description="Add feature X",
            status=FlowStatus.RUNNING,
        )

        # Create a verify_spec step that will return discovered issues
        step = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.PENDING)
        flow.state.add_step(step)

        # Mock handler that sets discovered_issues
        def mock_handler(s, f):
            s.outputs["discovered_issues"] = [
                {"title": "Missing test coverage", "description": "Auth module untested", "priority_hint": "warning"},
            ]
            return StepStatus.COMPLETED

        sm.register_handler(StepType.VERIFY_SPEC, mock_handler)

        with patch.object(sm.persistence, 'save_flow'):
            sm.run_step(flow, step)

        # Verify issue was collected
        mgr = IssueManager(project_root)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert issues[0].title == "Missing test coverage"
        assert "source:verify-spec" in issues[0].tags

    def test_implement_step_no_issue_collection(self, project_root):
        """Implement step's discovered_issues should not be collected."""
        from se3.engine.state_machine import StateMachine

        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=project_root)

        flow = FlowInstance(
            flow_id="test-flow-no-collect",
            task_description="Add feature X",
            status=FlowStatus.RUNNING,
        )

        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(step)

        def mock_handler(s, f):
            s.outputs["discovered_issues"] = [
                {"title": "Sneaky issue", "description": "Should not be created", "priority_hint": "warning"},
            ]
            return StepStatus.COMPLETED

        sm.register_handler(StepType.IMPLEMENT, mock_handler)

        with patch.object(sm.persistence, 'save_flow'):
            sm.run_step(flow, step)

        # Verify no issue was created (implement is not in whitelist)
        mgr = IssueManager(project_root)
        issues = mgr.list_issues()
        assert len(issues) == 0
