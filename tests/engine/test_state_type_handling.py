"""Tests for state management and task type handling.

Tests cover:
- State initialization with 'pending' default type
- Type updates after analyze step
- Conflict warning when --type differs from analyze result
- Context display_type behavior with pending/resolved types
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.context import Context


class TestStateInitialization:
    """Test state initialization with pending type."""

    def test_state_default_type_is_pending(self):
        """New state should have pending type status."""
        state = State()

        # Type should be pending initially
        assert state.is_type_pending() is True
        assert "resolved_type" not in state.context

    def test_state_with_explicit_type_still_pending(self):
        """State with explicit_type should still be pending until analyze."""
        state = State()
        state.context["explicit_type"] = "feature"

        # Should still be pending (resolved_type not set yet)
        assert state.is_type_pending() is True
        assert state.context.get("explicit_type") == "feature"

    def test_state_update_task_type_resolves_pending(self):
        """update_task_type should resolve the pending status."""
        state = State()

        # Update with analyzed type
        state.update_task_type("bugfix")

        # Should no longer be pending
        assert state.is_type_pending() is False
        assert state.context["resolved_type"] == "bugfix"

    def test_state_serialization_preserves_type_info(self):
        """to_dict/from_dict should preserve type information."""
        state = State()
        state.context["explicit_type"] = "feature"
        state.update_task_type("bugfix")

        # Serialize
        data = state.to_dict()

        # Verify context has both types
        assert data["context"]["explicit_type"] == "feature"
        assert data["context"]["resolved_type"] == "bugfix"

        # Deserialize
        restored = State.from_dict(data)

        # Verify both types restored
        assert restored.context["explicit_type"] == "feature"
        assert restored.context["resolved_type"] == "bugfix"
        assert restored.is_type_pending() is False

    def test_state_deserialization_backward_compat(self):
        """from_dict should handle data without resolved_type."""
        # Simulate old state data without resolved_type
        data = {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "context": {"explicit_type": "feature"},
            "selected_steps": [],
            "current_step_index": 0,
            "review_iterations": {},
        }

        state = State.from_dict(data)

        # Should be pending (no resolved_type)
        assert state.is_type_pending() is True
        assert state.context.get("explicit_type") == "feature"


class TestBaselineFailuresPersistence:
    """Tri-state round-trip for State.baseline_failures (None / [] / [...])."""

    def test_default_is_none(self):
        """A fresh State has no baseline captured yet."""
        assert State().baseline_failures is None

    def test_none_round_trips(self):
        """None ('not yet captured') survives serialization as None."""
        state = State()
        data = state.to_dict()
        assert data["baseline_failures"] is None
        restored = State.from_dict(data)
        assert restored.baseline_failures is None

    def test_empty_list_round_trips_distinct_from_none(self):
        """[] ('captured, zero failures') is preserved, NOT collapsed to None."""
        state = State()
        state.baseline_failures = []
        data = state.to_dict()
        assert data["baseline_failures"] == []
        restored = State.from_dict(data)
        assert restored.baseline_failures == []
        # The two captured-states must stay distinguishable.
        assert restored.baseline_failures is not None

    def test_populated_list_round_trips(self):
        """A concrete failure set round-trips intact."""
        failures = [
            "tests/engine/test_spec_format.py::TestRealSpecFiles::test_base_spec_requirement_count",
            "tests/test_daemon.py::test_flaky",
        ]
        state = State()
        state.baseline_failures = list(failures)
        data = state.to_dict()
        restored = State.from_dict(data)
        assert restored.baseline_failures == failures

    def test_missing_key_loads_as_none(self):
        """An older engine.json without the key loads as None (no error)."""
        data = {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "context": {},
            "selected_steps": [],
            "current_step_index": 0,
            "review_iterations": {},
            # no "baseline_failures" key (pre-baseline build)
        }
        state = State.from_dict(data)
        assert state.baseline_failures is None

    def test_round_trip_through_json(self):
        """End-to-end JSON dump/load preserves the tri-state."""
        for value in (None, [], ["a::b", "c::d"]):
            state = State()
            state.baseline_failures = value
            reloaded = State.from_dict(json.loads(json.dumps(state.to_dict())))
            assert reloaded.baseline_failures == value


class TestTypeUpdateAfterAnalyze:
    """Test type updates after analyze step completes."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create a flow with pending type
        self.flow = FlowInstance(
            task_description="Test task",
            task_type="pending",  # Default pending
            change_name="test-change",
            change_path=self.project_root,
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_analyze_updates_task_type(self):
        """Simulate analyze step and verify type is updated."""
        # Initial state should be pending
        assert self.flow.state.is_type_pending() is True

        # Simulate analyze output
        analyze_result = {
            "task_type": "feature",
            "scope": "Test scope",
            "complexity": "medium",
        }

        # Update state with resolved type (as analyze handler would)
        resolved_type = analyze_result.get("task_type", "feature")
        self.flow.state.update_task_type(resolved_type)

        # Verify type is no longer pending
        assert self.flow.state.is_type_pending() is False
        assert self.flow.state.context["resolved_type"] == "feature"

    def test_analyze_updates_type_from_pending_to_bugfix(self):
        """Test type update from pending to bugfix."""
        # Start with pending
        assert self.flow.state.is_type_pending() is True

        # Simulate analyze determining it's a bugfix
        self.flow.state.update_task_type("bugfix")

        assert self.flow.state.is_type_pending() is False
        assert self.flow.state.context["resolved_type"] == "bugfix"

    def test_analyze_updates_type_from_pending_to_review(self):
        """Test type update from pending to review."""
        # Start with pending
        assert self.flow.state.is_type_pending() is True

        # Simulate analyze determining it's a review
        self.flow.state.update_task_type("review")

        assert self.flow.state.is_type_pending() is False
        assert self.flow.state.context["resolved_type"] == "review"

    def test_analyze_step_output_stored(self):
        """Analyze step outputs should include task_type."""
        # Create an analyze step
        analyze_step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.COMPLETED,
            step_id="analyze-001",
        )

        # Simulate analyze output
        analyze_step.outputs["task_type"] = "feature"
        analyze_step.outputs["scope"] = "Test scope"
        analyze_step.outputs["complexity"] = "medium"

        # Update state
        self.flow.state.update_task_type(analyze_step.outputs["task_type"])
        self.flow.state.add_step(analyze_step)

        # Verify
        assert self.flow.state.context["resolved_type"] == "feature"
        assert self.flow.state.is_type_pending() is False


class TestTypeConflictWarning:
    """Test conflict warning when --type differs from analyze result."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create a flow with explicit type
        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",  # User specified
            change_name="test-change",
            change_path=self.project_root,
        )
        self.flow.state.context["explicit_type"] = "feature"

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_warning_when_types_match(self, caplog):
        """No warning when explicit type matches analyzed type."""
        from se3.engine.steps.analyze import _handle_type_conflict

        with caplog.at_level("WARNING"):
            # Both are "feature"
            _handle_type_conflict(self.flow, "feature")

        # Should not have warning
        assert "conflict" not in caplog.text.lower()
        assert "differs" not in caplog.text.lower()

    def test_explicit_type_overrides_analyzed(self, caplog):
        """Explicit --type should override LLM analysis and log info."""
        from se3.engine.steps.analyze import _handle_type_conflict

        with caplog.at_level("INFO"):
            # Explicit is "feature", analyzed is "bugfix"
            result = _handle_type_conflict(self.flow, "bugfix")

        # Explicit type wins
        assert result == "feature"
        assert "overrides" in caplog.text.lower()
        assert "feature" in caplog.text
        assert "bugfix" in caplog.text

    def test_override_returns_explicit_type(self, caplog):
        """Return value should be the explicit type, not the analyzed one."""
        from se3.engine.steps.analyze import _handle_type_conflict

        with caplog.at_level("INFO"):
            result = _handle_type_conflict(self.flow, "small")

        assert result == "feature"
        assert "feature" in caplog.text
        assert "small" in caplog.text

    def test_no_override_without_explicit_type(self, caplog):
        """Without explicit --type, analyzed type is used as-is."""
        from se3.engine.steps.analyze import _handle_type_conflict

        # Create flow without explicit_type
        flow_no_explicit = FlowInstance(
            task_description="Test task",
            task_type="pending",
        )

        with caplog.at_level("INFO"):
            result = _handle_type_conflict(flow_no_explicit, "feature")

        assert result == "feature"
        assert "overrides" not in caplog.text.lower()


class TestContextDisplayType:
    """Test Context display_type behavior."""

    def test_display_type_is_none_when_pending(self):
        """display_type should be None when type is pending."""
        state = State()
        context = Context("Test task", state)

        # Should be None when pending
        assert context.display_type is None
        assert context.is_type_pending() is True

    def test_display_type_returns_type_after_analyze(self):
        """display_type should return actual type after analyze."""
        state = State()
        context = Context("Test task", state)

        # Initially None
        assert context.display_type is None

        # Update type (as analyze would)
        state.update_task_type("feature")

        # Should now return the type
        assert context.display_type == "feature"
        assert context.is_type_pending() is False

    def test_display_type_returns_resolved_over_explicit(self):
        """display_type should prefer resolved_type over explicit_type."""
        state = State()
        state.context["explicit_type"] = "bugfix"  # User specified
        state.update_task_type("feature")  # But analyze says feature

        context = Context("Test task", state)

        # Should return resolved type (from analyze)
        assert context.display_type == "feature"
        assert context.task_type == "feature"

    def test_display_type_various_types(self):
        """display_type should work for all valid task types."""
        valid_types = ["feature", "bugfix", "review", "small", "directive"]

        for task_type in valid_types:
            state = State()
            state.update_task_type(task_type)
            context = Context("Test task", state)

            assert context.display_type == task_type
            assert context.is_type_pending() is False

    def test_task_type_property_returns_explicit_when_no_resolved(self):
        """task_type property should return explicit_type when no resolved_type."""
        state = State()
        state.context["explicit_type"] = "bugfix"

        context = Context("Test task", state)

        # Should return explicit type when no resolved type
        assert context.task_type == "bugfix"
        # But display_type should still be None (pending)
        assert context.display_type is None
        assert context.is_type_pending() is True

    def test_task_type_property_returns_flow_type_as_fallback(self):
        """task_type property should check flow's task_type as fallback."""
        # Create a mock state that behaves like older version
        state = State()
        # No explicit_type or resolved_type in context
        # But we can simulate the flow's task_type attribute access

        # Create context with a mock state that has task_type attr
        class MockState:
            context = {}
            task_type = "directive"

        context = Context("Test task", MockState())

        # Should fall back to state's task_type attribute
        assert context.task_type == "directive"

    def test_context_repr(self):
        """Context repr should show type and pending status."""
        state = State()
        context = Context("Test task", state)

        repr_str = repr(context)

        assert "Context" in repr_str
        assert "pending" in repr_str.lower()

    def test_context_repr_after_resolve(self):
        """Context repr should show resolved type."""
        state = State()
        state.update_task_type("feature")
        context = Context("Test task", state)

        repr_str = repr(context)

        assert "feature" in repr_str
        assert "pending=False" in repr_str or "pending: False" in repr_str


class TestFlowInstanceTypeHandling:
    """Test FlowInstance integration with type handling."""

    def test_flow_instance_creation_with_pending_type(self):
        """Flow can be created with pending type."""
        flow = FlowInstance(
            task_description="Test task",
            task_type="pending",
        )

        assert flow.task_type == "pending"
        assert flow.state.is_type_pending() is True

    def test_flow_instance_creation_with_explicit_type(self):
        """Flow can be created with explicit type."""
        flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
        )

        assert flow.task_type == "feature"
        # But state is still pending until analyze
        assert flow.state.is_type_pending() is True

    def test_flow_serialization_preserves_task_type(self):
        """FlowInstance to_dict/from_dict preserves task_type."""
        flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
        )
        flow.state.update_task_type("bugfix")  # Analyze says bugfix

        # Serialize
        data = flow.to_dict()

        # Verify both task_type values
        assert data["task_type"] == "feature"  # Original explicit
        assert data["state"]["context"]["resolved_type"] == "bugfix"

        # Deserialize
        restored = FlowInstance.from_dict(data)

        assert restored.task_type == "feature"
        assert restored.state.context["resolved_type"] == "bugfix"


class TestAnalyzeStepTypeExtraction:
    """Test the _extract_task_type helper function."""

    def _make_flow(self):
        """Create a minimal flow for _extract_task_type."""
        return FlowInstance(
            flow_id="test-flow",
            task_description="test",
            status=FlowStatus.RUNNING,
        )

    def test_extract_valid_task_type(self):
        """Should extract valid task types."""
        from se3.engine.steps.analyze import _extract_task_type

        result = _extract_task_type({"task_type": "bugfix"}, self._make_flow())
        assert result == "bugfix"

    def test_extract_defaults_to_feature(self):
        """Should default to feature when task_type missing."""
        from se3.engine.steps.analyze import _extract_task_type

        result = _extract_task_type({}, self._make_flow())
        assert result == "feature"

    def test_extract_defaults_invalid_type(self):
        """Should default to feature for invalid task_type."""
        from se3.engine.steps.analyze import _extract_task_type

        result = _extract_task_type({"task_type": "invalid_type"}, self._make_flow())
        assert result == "feature"

    def test_extract_all_valid_types(self):
        """Should accept all valid task types."""
        from se3.engine.steps.analyze import _extract_task_type

        valid_types = ["feature", "bugfix", "review", "small", "directive"]
        flow = self._make_flow()

        for task_type in valid_types:
            result = _extract_task_type({"task_type": task_type}, flow)
            assert result == task_type
