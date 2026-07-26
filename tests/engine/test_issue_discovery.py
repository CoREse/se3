"""Tests for the IssueDiscovery module.

Covers A-class triggers (fix loop exhaustion), B-class injection/collection,
deduplication, priority mapping, and tag management.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from tianluo.engine.issue_discovery import (
    IssueDiscovery,
    ISSUE_DISCOVERY_STEPS,
    ISSUE_FORBIDDEN_STEPS,
    ISSUE_DISCOVERY_PROMPT,
)
from tianluo.engine.issue_manager import Issue, IssueManager, IssueStatus
from tianluo.engine.models import (
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
    (tmp_path / "tianluo" / "issues" / "open").mkdir(parents=True)
    (tmp_path / "tianluo" / "issues" / "closed").mkdir(parents=True)
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
        {"iteration": 1, "timestamp": datetime.now().isoformat(), "reason": "test_failure"},
        {"iteration": 2, "timestamp": datetime.now().isoformat(), "reason": "test_failure"},
        {"iteration": 3, "timestamp": datetime.now().isoformat(), "reason": "test_failure"},
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
        assert issue.source == "system"

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

    def test_description_includes_flow_id(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert f"**Flow ID:** {basic_flow.flow_id}" in issue.description

    def test_description_includes_history_path(self, discovery, basic_flow):
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert f"**History path:** tianluo/history/{basic_flow.flow_id}" in issue.description

    def test_description_includes_refined_description(self, discovery, basic_flow):
        # Add a completed DISCOVERY step with a refined_description
        discovery_step = Step(step_type=StepType.DISCOVERY, status=StepStatus.COMPLETED)
        discovery_step.outputs = {
            "refined_description": "Implement user authentication with OAuth2 and 2FA support",
        }
        basic_flow.state.add_step(discovery_step)
        basic_flow.state.step_history.append(discovery_step.step_id)

        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert "**Refined description:**" in issue.description
        assert "OAuth2 and 2FA support" in issue.description

    def test_description_omits_refined_when_same_as_original(self, discovery, basic_flow):
        # No discovery step present in basic_flow → refined equals original
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert "**Refined description:**" not in issue.description

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
        for step_type in ["summarize"]:
            result = IssueDiscovery.get_injection_prompt(step_type)
            assert result is not None
            assert "discovered_issues" in result

    def test_verify_spec_no_longer_injected(self):
        result = IssueDiscovery.get_injection_prompt("verify_spec")
        assert result is None

    def test_forbidden_steps_return_none(self):
        for step_type in ["implement", "test"]:
            result = IssueDiscovery.get_injection_prompt(step_type)
            assert result is None

    def test_other_steps_return_none(self):
        for step_type in ["analyze", "plan", "commit"]:
            result = IssueDiscovery.get_injection_prompt(step_type)
            assert result is None

    def test_prompt_contains_json_format(self):
        prompt = IssueDiscovery.get_injection_prompt("summarize")
        assert "title" in prompt
        assert "description" in prompt
        assert "priority_hint" in prompt

    def test_prompt_uses_new_priority_values(self):
        prompt = IssueDiscovery.get_injection_prompt("summarize")
        assert "critical" in prompt
        assert "high" in prompt
        assert "medium" in prompt
        assert "low" in prompt


class TestCollectIssuesFromOutput:
    """Tests for B-class issue collection."""

    def test_critical_priority_hint_used_directly(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Critical flaw", "description": "System crash", "priority_hint": "critical"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "critical"

    def test_high_priority_hint_used_directly(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Security flaw", "description": "Token not validated", "priority_hint": "high"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "high"

    def test_medium_priority_hint_used_directly(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Missing logging", "description": "No logging in auth module", "priority_hint": "medium"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "medium"

    def test_low_priority_hint_used_directly(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Consider caching", "description": "Auth could use caching", "priority_hint": "low"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "low"

    def test_invalid_priority_hint_defaults_to_medium(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Bad hint", "description": "desc", "priority_hint": "warning"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "medium"

    def test_missing_priority_hint_defaults_to_medium(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "No hint", "description": "desc"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].priority == "medium"

    def test_adds_correct_tags(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Test issue", "description": "desc", "priority_hint": "medium"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert "auto-discovered" in issues[0].tags
        assert "source:summarize" in issues[0].tags
        assert issues[0].source == "system"

    def test_multiple_issues(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Issue A", "description": "desc A", "priority_hint": "medium"},
                {"title": "Issue B", "description": "desc B", "priority_hint": "low"},
                {"title": "Issue C", "description": "desc C", "priority_hint": "high"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 3

    def test_empty_discovered_issues(self, discovery, basic_flow):
        outputs = {"discovered_issues": []}

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 0

    def test_no_discovered_issues_key(self, discovery, basic_flow):
        outputs = {"some_other_key": "value"}

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 0

    def test_malformed_items_skipped(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                "not a dict",
                {"no_title": "missing title field"},
                {"title": "", "description": "empty title"},
                {"title": "Valid issue", "description": "This is valid", "priority_hint": "medium"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].title == "Valid issue"

    def test_non_whitelist_step_returns_empty(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Sneaky issue", "description": "desc", "priority_hint": "medium"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "implement", outputs)

        assert len(issues) == 0

    def test_verify_spec_no_longer_in_whitelist(self, discovery, basic_flow):
        """verify_spec is removed from B-class whitelist; scope mechanism replaces it."""
        outputs = {
            "discovered_issues": [
                {"title": "Should not collect", "description": "desc", "priority_hint": "high"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "verify_spec", outputs)

        assert len(issues) == 0


class TestDeduplication:
    """Tests for title-based deduplication."""

    def test_exact_duplicate_blocked(self, discovery, basic_flow):
        outputs = {
            "discovered_issues": [
                {"title": "Missing error handling", "description": "desc1", "priority_hint": "medium"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)
        issues2 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues1) == 1
        assert len(issues2) == 0

    def test_case_insensitive_dedup(self, discovery, basic_flow):
        outputs1 = {
            "discovered_issues": [
                {"title": "Missing Error Handling", "description": "d1", "priority_hint": "medium"},
            ]
        }
        outputs2 = {
            "discovered_issues": [
                {"title": "missing error handling", "description": "d2", "priority_hint": "medium"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs1)
        issues2 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs2)

        assert len(issues1) == 1
        assert len(issues2) == 0

    def test_punctuation_insensitive_dedup(self, discovery, basic_flow):
        outputs1 = {
            "discovered_issues": [
                {"title": "Missing: error handling!", "description": "d1", "priority_hint": "medium"},
            ]
        }
        outputs2 = {
            "discovered_issues": [
                {"title": "Missing error handling", "description": "d2", "priority_hint": "medium"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs1)
        issues2 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs2)

        assert len(issues1) == 1
        assert len(issues2) == 0

    def test_different_titles_not_deduped(self, discovery, basic_flow):
        outputs1 = {
            "discovered_issues": [
                {"title": "Missing error handling", "description": "d1", "priority_hint": "medium"},
            ]
        }
        outputs2 = {
            "discovered_issues": [
                {"title": "Security vulnerability in auth", "description": "d2", "priority_hint": "medium"},
            ]
        }

        issues1 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs1)
        issues2 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs2)

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
                {"title": issue1.title, "description": "dup", "priority_hint": "medium"},
            ]
        }
        issues2 = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert issue1 is not None
        assert len(issues2) == 0


class TestStateMachineIntegration:
    """Integration tests for IssueDiscovery with StateMachine."""

    def test_fix_loop_exhaustion_creates_issue(self, project_root):
        """When fix loop reaches max iterations, state machine creates an issue."""
        from tianluo.engine.state_machine import StateMachine

        with patch("tianluo.engine.state_machine.PersistenceManager"):
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
        from tianluo.engine.state_machine import StateMachine

        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=project_root)

        flow = FlowInstance(
            flow_id="test-flow-collect",
            task_description="Add feature X",
            status=FlowStatus.RUNNING,
        )

        # Create a summarize step that will return discovered issues
        step = Step(step_type=StepType.SUMMARIZE, status=StepStatus.PENDING)
        flow.state.add_step(step)

        # Mock handler that sets discovered_issues
        def mock_handler(s, f):
            s.outputs["discovered_issues"] = [
                {"title": "Missing test coverage", "description": "Auth module untested", "priority_hint": "medium"},
            ]
            return StepStatus.COMPLETED

        sm.register_handler(StepType.SUMMARIZE, mock_handler)

        with patch.object(sm.persistence, 'save_flow'):
            sm.run_step(flow, step)

        # Verify issue was collected
        mgr = IssueManager(project_root)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert issues[0].title == "Missing test coverage"
        assert "source:summarize" in issues[0].tags

    def test_implement_step_no_issue_collection(self, project_root):
        """Implement step's discovered_issues should not be collected."""
        from tianluo.engine.state_machine import StateMachine

        with patch("tianluo.engine.state_machine.PersistenceManager"):
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


class TestSourceSemantics:
    """Tests verifying that all programmatic discovery paths write source='system'."""

    def test_fix_loop_exhaustion_source_is_system(self, discovery, basic_flow):
        """A-class fix-loop issues have source='system'."""
        trigger = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
        trigger.outputs = {"fix_context": {}}

        issue = discovery.create_from_fix_loop_exhaustion(basic_flow, trigger)

        assert issue is not None
        assert issue.source == "system"

    def test_pre_existing_failures_source_is_system(self, discovery, basic_flow):
        """A-class pre-existing-failure issues have source='system'."""
        failures = [
            {"test_id": "test_auth", "reason": "assertion error"},
            {"test_id": "test_login", "reason": "timeout"},
        ]

        issue = discovery.create_from_pre_existing_failures(basic_flow, failures)

        assert issue is not None
        assert issue.source == "system"

    def test_b_class_discovered_issues_source_is_system(self, discovery, basic_flow):
        """B-class collected issues have source='system'."""
        outputs = {
            "discovered_issues": [
                {"title": "Missing caching", "description": "No cache layer", "priority_hint": "low"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 1
        assert issues[0].source == "system"

    def test_multiple_b_class_all_system(self, discovery, basic_flow):
        """Multiple B-class issues all have source='system'."""
        outputs = {
            "discovered_issues": [
                {"title": "Issue A", "description": "desc A", "priority_hint": "medium"},
                {"title": "Issue B", "description": "desc B", "priority_hint": "high"},
            ]
        }

        issues = discovery.collect_issues_from_output(basic_flow, "summarize", outputs)

        assert len(issues) == 2
        for issue in issues:
            assert issue.source == "system"

    def test_issue_manager_default_source_is_system(self, issue_manager):
        """IssueManager.create() defaults source to 'system' when omitted."""
        issue = issue_manager.create(description="Test default source")

        assert issue.source == "system"

    def test_issue_manager_explicit_source_preserved(self, issue_manager):
        """IssueManager.create() respects explicit source parameter."""
        issue = issue_manager.create(description="Test explicit source", source="human")

        assert issue.source == "human"

    def test_source_round_trips_via_yaml(self, issue_manager, project_root):
        """Source field survives YAML serialization and deserialization."""
        issue = issue_manager.create(description="Round-trip test", source="system")
        loaded = issue_manager.load(issue.id)

        assert loaded is not None
        assert loaded.source == "system"

    def test_missing_source_defaults_to_system_on_load(self, project_root):
        """Pre-source YAML files (missing source field) load as source='system'."""
        import yaml
        from tianluo.engine.issue_manager import Issue

        data = {
            "id": "999",
            "title": "Legacy issue",
            "description": "No source field",
            "status": "open",
            "tags": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        # Deliberately omit 'source' to simulate a pre-source YAML file
        assert "source" not in data

        issue = Issue.from_dict(data)
        assert issue.source == "system"

    def test_source_filter_list_issues(self, issue_manager):
        """list_issues(source_filter=...) correctly filters by source."""
        issue_manager.create(description="System issue", source="system")
        issue_manager.create(description="Human issue", source="human")

        system_only = issue_manager.list_issues(source_filter="system")
        human_only = issue_manager.list_issues(source_filter="human")
        all_issues = issue_manager.list_issues()

        assert len(system_only) == 1
        assert system_only[0].source == "system"
        assert len(human_only) == 1
        assert human_only[0].source == "human"
        assert len(all_issues) == 2
