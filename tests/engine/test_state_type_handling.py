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

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.context import Context, RUN_MODE_TYPES, effective_task_type


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
            "tests/engine/test_charter.py::TestLoadCharter::test_missing_file",
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
        from tianluo.engine.steps.analyze import _handle_type_conflict

        with caplog.at_level("WARNING"):
            # Both are "feature"
            _handle_type_conflict(self.flow, "feature")

        # Should not have warning
        assert "conflict" not in caplog.text.lower()
        assert "differs" not in caplog.text.lower()

    def test_explicit_type_overrides_analyzed(self, caplog):
        """Explicit --type should override LLM analysis and log info."""
        from tianluo.engine.steps.analyze import _handle_type_conflict

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
        from tianluo.engine.steps.analyze import _handle_type_conflict

        with caplog.at_level("INFO"):
            result = _handle_type_conflict(self.flow, "small")

        assert result == "feature"
        assert "feature" in caplog.text
        assert "small" in caplog.text

    def test_no_override_without_explicit_type(self, caplog):
        """Without explicit --type, analyzed type is used as-is."""
        from tianluo.engine.steps.analyze import _handle_type_conflict

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
        valid_types = ["feature", "bugfix", "review", "small", "survey"]

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
            task_type = "small"

        context = Context("Test task", MockState())

        # Should fall back to state's task_type attribute
        assert context.task_type == "small"

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


class TestEffectiveTaskType:
    """effective_task_type: the single source of truth for the real task type.

    Guarantees no consumer (commit message / version) ever sees a run mode
    (e.g. 'discovery') as the type.
    """

    def test_never_returns_a_run_mode(self):
        """Every branch must resolve to a non-run-mode value."""
        cases = [
            ({"explicit_type": "discovery"}, "discovery"),
            ({"analyzed_type": "discovery"}, "discovery"),
            ({}, "discovery"),
            ({}, None),
        ]
        for context, fallback in cases:
            result = effective_task_type(context, fallback)
            assert result not in RUN_MODE_TYPES

    def test_real_explicit_type_wins(self):
        """A real --type overrides analyzed_type and the flow fallback."""
        context = {"explicit_type": "bugfix", "analyzed_type": "feature"}
        assert effective_task_type(context, "discovery") == "bugfix"

    def test_discovery_explicit_falls_through_to_analyzed(self):
        """explicit_type='discovery' is a run mode → use analyzed_type."""
        context = {"explicit_type": "discovery", "analyzed_type": "feature"}
        assert effective_task_type(context, "discovery") == "feature"

    def test_analyzed_type_used_when_no_real_explicit(self):
        """analyzed_type is the real type behind a discovery run."""
        context = {"analyzed_type": "bugfix"}
        assert effective_task_type(context, "discovery") == "bugfix"

    def test_missing_analyzed_type_discovery_fallback_is_feature(self):
        """Old state: no analyzed_type + flow.task_type='discovery' → 'feature'."""
        assert effective_task_type({}, "discovery") == "feature"

    def test_regular_feature_flow_unaffected(self):
        """A normal flow with no run mode resolves to its flow type."""
        assert effective_task_type({}, "feature") == "feature"
        assert effective_task_type({}, "bugfix") == "bugfix"

    def test_non_dict_context_is_tolerated(self):
        """A missing/non-dict context degrades to the sanitized fallback."""
        assert effective_task_type(None, "bugfix") == "bugfix"
        assert effective_task_type(None, "discovery") == "feature"
        assert effective_task_type(None, None) == "feature"


class TestContextDiscoverySanitization:
    """Context.task_type / display_type must not surface 'discovery'."""

    def test_task_type_falls_back_to_analyzed_when_discovery(self):
        """resolved_type='discovery' + analyzed_type='feature' → task_type='feature'."""
        state = State()
        state.context["analyzed_type"] = "feature"
        state.update_task_type("discovery")  # resolved_type = discovery

        context = Context("Test task", state)

        assert context.task_type == "feature"
        assert context.task_type not in RUN_MODE_TYPES

    def test_display_type_never_returns_discovery(self):
        """display_type shows the analyzed type, not the run mode."""
        state = State()
        state.context["analyzed_type"] = "bugfix"
        state.update_task_type("discovery")

        context = Context("Test task", state)

        assert context.display_type == "bugfix"
        assert context.display_type not in RUN_MODE_TYPES

    def test_discovery_without_analyzed_type_degrades_to_feature(self):
        """Old state: resolved_type='discovery', no analyzed_type → 'feature'."""
        state = State()
        state.update_task_type("discovery")

        context = Context("Test task", state)

        assert context.task_type == "feature"
        assert context.display_type == "feature"

    def test_regular_resolved_type_unchanged(self):
        """A non-run-mode resolved type is returned verbatim (behavior unchanged)."""
        state = State()
        state.update_task_type("bugfix")

        context = Context("Test task", state)

        assert context.task_type == "bugfix"
        assert context.display_type == "bugfix"

    def test_is_type_pending_semantics_preserved(self):
        """Sanitization does not change is_type_pending (still keyed on resolved_type)."""
        state = State()
        # No resolved_type yet → pending, regardless of analyzed_type presence.
        state.context["analyzed_type"] = "feature"
        context = Context("Test task", state)
        assert context.is_type_pending() is True

        state.update_task_type("discovery")
        assert context.is_type_pending() is False


class TestAnalyzePersistsAnalyzedType:
    """analyze persists the real analyzed type without touching the sequence type."""

    def _run_analyze(
        self, project_root, explicit_type, llm_task_type, root_cause_clear=True
    ):
        """Drive analyze_handler with a stubbed LLM and collector."""
        from tianluo.engine.steps import analyze as analyze_mod
        from tianluo.engine.models import Step, StepType

        flow = FlowInstance(
            task_description="Add a new capability",
            task_type=explicit_type or "pending",
            change_name="test-change",
            change_path=project_root / "tianluo.yaml",
        )
        if explicit_type:
            flow.state.context["explicit_type"] = explicit_type

        step = Step(step_type=StepType.ANALYZE, step_id="analyze-001")
        step.inputs["task_description"] = "Add a new capability"

        llm_result = {
            "task_type": llm_task_type,
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "because",
        }
        # ``None`` models an LLM that omitted the field entirely, so callers can
        # exercise the conservative default path.
        if root_cause_clear is not None:
            llm_result["root_cause_clear"] = root_cause_clear

        with patch.object(analyze_mod, "_collect_project_summary", return_value="ctx"), \
            patch.object(analyze_mod, "get_charter_injection", return_value="", create=True), \
            patch.object(analyze_mod, "LLMCaller") as MockCaller, \
            patch.object(analyze_mod, "parse_json_response", return_value=llm_result):
            # context_builder helpers are imported lazily inside the handler;
            # patch them on that module.
            import tianluo.engine.context_builder as cb
            with patch.object(cb, "get_issue_discovery_injection", return_value=""), \
                patch.object(cb, "get_charter_injection", return_value=""), \
                patch.object(cb, "get_code_index_injection", return_value=""), \
                patch.object(cb, "ensure_code_index_fresh", return_value=None), \
                patch.object(cb, "get_runtime_environment_injection", return_value=""):
                MockCaller.return_value.call.return_value = "{}"
                status = analyze_mod.analyze_handler(step, flow)

        return flow, step, status

    def test_discovery_flow_persists_real_analyzed_type(self):
        """A --discover run keeps flow.task_type='discovery' but records the real type."""
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            flow, step, status = self._run_analyze(
                project_root, explicit_type="discovery", llm_task_type="feature"
            )

            assert status == StepStatus.COMPLETED
            # Real analyzed type persisted, never 'discovery'.
            assert flow.state.context["analyzed_type"] == "feature"
            assert step.outputs["analyzed_type"] == "feature"
            assert flow.state.context["analyzed_type"] not in RUN_MODE_TYPES
            # Sequence type stays 'discovery' so the step sequence / resume hold.
            assert flow.task_type == "discovery"
            assert flow.state.context["resolved_type"] == "discovery"

    def test_regular_feature_flow_analyzed_type_matches(self):
        """A normal flow records analyzed_type equal to its real type."""
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            flow, step, status = self._run_analyze(
                project_root, explicit_type=None, llm_task_type="bugfix"
            )

            assert status == StepStatus.COMPLETED
            assert flow.state.context["analyzed_type"] == "bugfix"
            assert step.outputs["analyzed_type"] == "bugfix"
            assert flow.task_type == "bugfix"
            assert flow.state.context["resolved_type"] == "bugfix"

    def test_root_cause_clear_defaults_to_false_when_absent(self):
        """A missing judgement is recorded as 'not established', not as clear."""
        with tempfile.TemporaryDirectory() as td:
            _flow, step, status = self._run_analyze(
                Path(td),
                explicit_type=None,
                llm_task_type="bugfix",
                root_cause_clear=None,
            )

            assert status == StepStatus.COMPLETED
            assert step.outputs["root_cause_clear"] is False


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
        from tianluo.engine.steps.analyze import _extract_task_type

        result = _extract_task_type({"task_type": "bugfix"}, self._make_flow())
        assert result == "bugfix"

    def test_extract_defaults_to_feature(self):
        """Should default to feature when task_type missing."""
        from tianluo.engine.steps.analyze import _extract_task_type

        result = _extract_task_type({}, self._make_flow())
        assert result == "feature"

    def test_extract_defaults_invalid_type(self):
        """Should default to feature for invalid task_type."""
        from tianluo.engine.steps.analyze import _extract_task_type

        result = _extract_task_type({"task_type": "invalid_type"}, self._make_flow())
        assert result == "feature"

    def test_extract_all_valid_types(self):
        """Should accept all valid task types."""
        from tianluo.engine.steps.analyze import _extract_task_type

        # Mirrors analyze._extract_task_type's own valid set — an unlisted type
        # here would silently be left unasserted while the extractor quietly
        # coerced it to 'feature'.
        valid_types = ["feature", "bugfix", "review", "small", "survey"]
        flow = self._make_flow()

        for task_type in valid_types:
            result = _extract_task_type({"task_type": task_type}, flow)
            assert result == task_type


class TestRetiredTaskTypeCompatibility:
    """A flow persisted under a since-retired task type must still resume/display.

    'directive' was removed from the classification space, but old engine.json
    files on disk still carry it. Resume must replay their stored selected_steps
    verbatim rather than re-deriving a sequence from the (now absent) table
    entry, and the display layer must echo the raw string back.
    """

    # The sequence a directive flow was created with, before the type was retired.
    LEGACY_DIRECTIVE_STEPS = [
        StepType.ANALYZE,
        StepType.PLAN,
        StepType.IMPLEMENT,
        StepType.CHARTER_FRESHNESS,
        StepType.VERSION_ANALYZE,
        StepType.COMMIT,
        StepType.SUMMARIZE,
    ]

    def _persist_legacy_flow(self, project_root: Path) -> FlowInstance:
        from tianluo.engine.persistence import PersistenceManager

        state = State()
        state.selected_steps = list(self.LEGACY_DIRECTIVE_STEPS)
        state.current_step_index = 2
        state.update_task_type("directive")
        state.context["explicit_type"] = "directive"

        flow = FlowInstance(
            flow_id="20260101-000000_legacy01",
            status=FlowStatus.PAUSED,
            task_description="A directive-era task",
            task_type="directive",
            state=state,
        )

        persistence = PersistenceManager(project_root)
        persistence.ensure_directories()
        persistence.save_flow(flow)
        return flow

    def test_legacy_directive_flow_reloads_stored_sequence(self):
        from tianluo.engine.persistence import PersistenceManager

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._persist_legacy_flow(project_root)

            loaded = PersistenceManager(project_root).load_flow()

            assert loaded is not None
            assert loaded.task_type == "directive"
            # Stored sequence is replayed item-for-item, not re-derived.
            assert loaded.state.selected_steps == self.LEGACY_DIRECTIVE_STEPS
            assert loaded.state.current_step_index == 2

    def test_legacy_directive_flow_displays_raw_type(self):
        from tianluo.commands.run import _get_display_task_type

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = self._persist_legacy_flow(Path(tmpdir))

            assert _get_display_task_type(flow) == "directive"

    def test_get_default_step_sequence_tolerates_retired_type(self):
        """The lookup must fall back, never raise, for a retired/unknown type."""
        from tianluo.engine.models import get_default_step_sequence

        seq = get_default_step_sequence("directive")
        assert seq == get_default_step_sequence("feature")
