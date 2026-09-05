"""The generic rewind facility: step deletion, state restoration, generations.

Rewinding is not "delete some steps" — it is "put the flow back the way the
target step first found it". These tests pin the two halves of that: what is
restored (derived state) and what is deliberately NOT (flow-level facts).
"""

from __future__ import annotations

import json

import pytest

from tianluo.engine import rewind
from tianluo.engine.chat_history import (
    format_history_for_retry,
    record_prompt,
    record_response,
)
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.rewind import (
    ENTRY_SNAPSHOT_CONTEXT_KEY,
    GENERATION_CONTEXT_KEY,
    RewindError,
    bind_flow_generation,
    current_generation,
    flow_generation,
    rewind_to_step,
    set_current_generation,
    snapshot_step_entry,
)


@pytest.fixture(autouse=True)
def _reset_ambient_generation():
    set_current_generation(0)
    yield
    set_current_generation(0)


def _flow_with_steps(*types):
    flow = FlowInstance(
        flow_id="rw-1",
        task_description="original task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = list(types)
    for index, step_type in enumerate(types):
        step = Step(
            step_id=f"{index + 1:02d}_{step_type.value}_x",
            step_type=step_type,
            status=StepStatus.COMPLETED,
        )
        flow.state.add_step(step)
    flow.state.current_step_id = flow.state.step_history[-1]
    flow.state.current_step_index = len(types) - 1
    return flow


def _snapshot_all_entries(flow):
    """Snapshot every step as if it had been entered in order.

    The real state machine snapshots at the top of ``run_step``, so each
    snapshot carries the routing index that step actually held. Replaying that
    ordering matters here: a rewind restores the target's OWN index, so a
    fixture that snapshotted everything at the final index would hide exactly
    the bug the index restore exists to fix.
    """
    original_index = flow.state.current_step_index
    for index, sid in enumerate(list(flow.state.step_history)):
        flow.state.current_step_index = index
        snapshot_step_entry(flow, sid)
    flow.state.current_step_index = original_index


class TestEntrySnapshot:
    def test_snapshot_captures_derived_state(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        flow.state.context["review_scope"] = {"files": ["a.py"]}
        flow.state.fix_iterations = 2
        snapshot_step_entry(flow, "02_implement_x")
        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["02_implement_x"]
        assert snap["context"]["review_scope"] == {"files": ["a.py"]}
        assert snap["state"]["fix_iterations"] == 2

    def test_snapshot_excludes_flow_level_facts(self):
        flow = _flow_with_steps(StepType.IMPLEMENT)
        flow.state.context["user_interjections"] = [{"text": "x"}]
        flow.state.context["invariant_anchors"] = {"charter": "c"}
        snapshot_step_entry(flow, "01_implement_x")
        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["01_implement_x"]
        assert "user_interjections" not in snap["context"]
        assert "invariant_anchors" not in snap["context"]

    def test_snapshot_is_taken_only_on_first_entry(self):
        """A retry must not overwrite the state the step FIRST saw."""
        flow = _flow_with_steps(StepType.IMPLEMENT)
        flow.state.context["review_scope"] = "first"
        snapshot_step_entry(flow, "01_implement_x")
        flow.state.context["review_scope"] = "after a failed attempt"
        snapshot_step_entry(flow, "01_implement_x")
        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["01_implement_x"]
        assert snap["context"]["review_scope"] == "first"

    def test_snapshot_is_a_deep_copy(self):
        flow = _flow_with_steps(StepType.IMPLEMENT)
        nested = {"issues": []}
        flow.state.context["self_check_review"] = nested
        snapshot_step_entry(flow, "01_implement_x")
        nested["issues"].append("mutated later")
        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["01_implement_x"]
        assert snap["context"]["self_check_review"]["issues"] == []

    def test_snapshot_stores_step_types_by_value(self):
        """``default=str`` would render a live enum as ``"StepType.PLAN"``,
        which nothing can parse back; the snapshot is stored JSON-native."""
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        snapshot_step_entry(flow, "02_implement_x")
        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["02_implement_x"]
        assert snap["state"]["selected_steps"] == ["plan", "implement"]

    def test_missing_step_id_is_a_noop(self):
        flow = _flow_with_steps(StepType.IMPLEMENT)
        snapshot_step_entry(flow, "")
        assert ENTRY_SNAPSHOT_CONTEXT_KEY not in flow.state.context


class TestRewindShape:
    def test_target_and_everything_after_it_are_removed(self):
        flow = _flow_with_steps(
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST, StepType.SELF_CHECK
        )
        _snapshot_all_entries(flow)

        result = rewind_to_step(flow, "02_implement_x")

        assert result.target_step_id == "02_implement_x"
        assert result.removed_step_ids == [
            "02_implement_x", "03_test_x", "04_self_check_x",
        ]
        assert flow.state.step_history == ["01_plan_x"]
        assert set(flow.state.steps) == {"01_plan_x"}

    def test_step_index_winds_back_to_the_target_type(self):
        flow = _flow_with_steps(
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST
        )
        _snapshot_all_entries(flow)
        rewind_to_step(flow, "02_implement_x")
        assert flow.state.current_step_index == 1
        assert flow.state.current_step_id == "01_plan_x"

    def test_rewinding_to_the_first_step_clears_the_current_id(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        rewind_to_step(flow, "01_plan_x")
        assert flow.state.step_history == []
        assert flow.state.current_step_id is None
        assert flow.state.current_step_index == 0

    def test_default_target_is_the_current_step(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        result = rewind_to_step(flow, None)
        assert result.target_step_id == "02_implement_x"

    def test_unknown_target_is_rejected(self):
        flow = _flow_with_steps(StepType.PLAN)
        with pytest.raises(RewindError):
            rewind_to_step(flow, "99_nope_x")

    def test_removed_step_snapshots_are_discarded(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        rewind_to_step(flow, "02_implement_x")
        snapshots = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]
        assert "02_implement_x" not in snapshots
        assert "01_plan_x" in snapshots


    def test_a_repeated_step_type_rewinds_to_its_own_position(self):
        """With confirmations after several steps, ``selected_steps`` carries
        CONFIRM more than once. Deriving the index by step TYPE would pick the
        first occurrence, so approving the rebuilt gate would drop the flow
        back into an earlier segment instead of continuing after the target."""
        flow = _flow_with_steps(
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
            StepType.CONFIRM,
            StepType.TEST,
        )
        _snapshot_all_entries(flow)

        rewind_to_step(flow, "04_confirm_x")

        assert flow.state.current_step_index == 3
        assert flow.state.current_step_id == "03_implement_x"


class TestRewindStateInvariants:
    def _prepared(self):
        flow = _flow_with_steps(
            StepType.PLAN, StepType.IMPLEMENT, StepType.SELF_CHECK
        )
        snapshot_step_entry(flow, "01_plan_x")
        flow.state.context["review_scope"] = "as implement entered"
        flow.state.context["implementation_baseline"] = "base-1"
        flow.state.fix_iterations = 0
        snapshot_step_entry(flow, "02_implement_x")
        # Work happens after implement is entered.
        flow.state.context["review_scope"] = "after self_check"
        flow.state.context["self_check_review"] = {"round": 3}
        flow.state.context["latest_fix_baseline"] = "base-9"
        flow.state.fix_iterations = 4
        flow.state.fix_history = [{"iteration": 1}]
        flow.state.review_iterations = {"03_self_check_x": 2}
        snapshot_step_entry(flow, "03_self_check_x")
        return flow

    def test_derived_context_is_restored_to_the_targets_entry(self):
        flow = self._prepared()
        rewind_to_step(flow, "02_implement_x")
        assert flow.state.context["review_scope"] == "as implement entered"
        assert flow.state.context["implementation_baseline"] == "base-1"
        # Written only after the target was entered — must be gone.
        assert "self_check_review" not in flow.state.context
        assert "latest_fix_baseline" not in flow.state.context

    def test_fix_and_review_counters_are_restored(self):
        flow = self._prepared()
        rewind_to_step(flow, "02_implement_x")
        assert flow.state.fix_iterations == 0
        assert flow.state.fix_history == []
        assert flow.state.review_iterations == {}

    def test_flow_level_facts_never_rewind(self):
        flow = self._prepared()
        flow.state.context["user_interjections"] = [{"text": "keep me"}]
        flow.state.context["description_revisions"] = [{"text": "new task"}]
        flow.state.context["invariant_anchors"] = {"charter": "c"}
        flow.state.context["explicit_type"] = "feature"
        flow.state.baseline_failures = ["test_a"]
        flow.baseline_commit = "abc123"

        rewind_to_step(flow, "02_implement_x")

        assert flow.state.context["user_interjections"] == [{"text": "keep me"}]
        assert flow.state.context["description_revisions"] == [{"text": "new task"}]
        assert flow.state.context["invariant_anchors"] == {"charter": "c"}
        assert flow.state.context["explicit_type"] == "feature"
        assert flow.state.baseline_failures == ["test_a"]
        assert flow.baseline_commit == "abc123"
        assert flow.task_description == "original task"

    def test_once_per_flow_side_effect_guards_never_rewind(self):
        """The guards record an EXTERNAL effect already performed.

        TEST files inherited-failure issues under ``tianluo/issues/`` once per
        flow; a rewind does not un-write those files, so resetting the guard
        would have the rebuilt TEST file the same issues a second time — the
        duplicate-issue explosion the guard exists to prevent.
        """
        flow = self._prepared()
        flow.state.context["inherited_failures_filed"] = True
        flow.state.context["e2e_suggestion_shown"] = True

        rewind_to_step(flow, "02_implement_x")

        assert flow.state.context["inherited_failures_filed"] is True
        assert flow.state.context["e2e_suggestion_shown"] is True

    def test_a_keep_rewind_carries_the_implementation_review_baseline(self):
        """With ``keep`` the abandoned attempt's edits stay on disk.

        They are still this flow's work, so they must stay inside every later
        review's diff scope. The baseline is amended into the entry snapshot
        only at IMPLEMENT's OWN first entry, so a rewind to an EARLIER target
        restores a context with none — and the rebuilt IMPLEMENT would
        photograph a tree that already contains those edits, shipping them
        unreviewed.
        """
        flow = self._prepared()
        flow.state.context["review_scope"] = {
            "implementation_baseline": {"baseline_id": "impl-1"},
        }

        rewind_to_step(flow, "01_plan_x")

        assert flow.state.context["review_scope"]["implementation_baseline"] == {
            "baseline_id": "impl-1",
        }

    def test_a_reset_rewind_drops_the_review_baseline(self):
        """``reset`` puts the tree back to the flow's own baseline, so nothing
        of the abandoned attempt remains and a fresh capture is the accurate
        one."""
        flow = self._prepared()
        flow.state.context["review_scope"] = {
            "implementation_baseline": {"baseline_id": "impl-1"},
        }

        rewind_to_step(flow, "01_plan_x", carry_step_inputs=False)

        scope = flow.state.context.get("review_scope") or {}
        assert "implementation_baseline" not in scope

    def test_the_targets_own_baseline_wins_over_the_carried_one(self):
        """Rewinding to a step at/after IMPLEMENT restores the baseline that
        step actually ran against; the carried value must not overwrite it."""
        flow = self._prepared()
        snapshots = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]
        snapshots["02_implement_x"]["context"]["review_scope"] = {
            "implementation_baseline": {"baseline_id": "at-entry"},
        }
        flow.state.context["review_scope"] = {
            "implementation_baseline": {"baseline_id": "much-later"},
        }

        rewind_to_step(flow, "02_implement_x")

        assert flow.state.context["review_scope"]["implementation_baseline"] == {
            "baseline_id": "at-entry",
        }

    def test_session_usage_ledger_is_untouched(self):
        flow = self._prepared()
        from tianluo.usage import UsageRecord

        flow.state.add_session_usage_record(
            UsageRecord(call_id="c1", attempt=0, agent_name="a")
        )
        before = len(flow.state.session_usage_records)
        rewind_to_step(flow, "02_implement_x")
        assert len(flow.state.session_usage_records) == before

    def test_a_target_without_a_snapshot_is_refused_without_mutating(self):
        """A flow from before entry snapshots existed cannot be rewound: a
        routing-only rewind would rerun an early step with the LATER steps'
        fix counters, review scope and self-check rounds still active."""
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        flow.state.fix_iterations = 3
        flow.state.context["review_scope"] = {"files": ["late.py"]}
        with pytest.raises(RewindError):
            rewind_to_step(flow, "02_implement_x")
        # Nothing moved.
        assert flow.state.step_history == ["01_plan_x", "02_implement_x"]
        assert set(flow.state.steps) == {"01_plan_x", "02_implement_x"}
        assert flow.state.current_step_id == "02_implement_x"
        assert flow.state.fix_iterations == 3
        assert flow_generation(flow) == 1


class TestGenerations:
    def test_rewind_bumps_the_generation(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        assert flow_generation(flow) == 1  # every flow starts at 1
        result = rewind_to_step(flow, "02_implement_x")
        assert result.generation == 2
        assert flow.state.context[GENERATION_CONTEXT_KEY] == 2
        # Published as ambient so the next LLM call stamps its records with it.
        assert current_generation() == 2

    def test_bind_flow_generation_publishes_the_persisted_value(self):
        flow = _flow_with_steps(StepType.PLAN)
        flow.state.context[GENERATION_CONTEXT_KEY] = 7
        assert bind_flow_generation(flow) == 7
        assert current_generation() == 7

    def test_bind_flow_generation_seeds_a_flow_that_has_none(self):
        """Generation 0 is reserved as the legacy wildcard, so a live flow
        must start at 1 — otherwise its records could never be superseded."""
        flow = _flow_with_steps(StepType.PLAN)
        assert bind_flow_generation(flow) == 1
        assert flow.state.context[GENERATION_CONTEXT_KEY] == 1

    def test_generation_is_not_itself_rewound(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        rewind_to_step(flow, "02_implement_x")
        # Re-snapshot and rewind again — the counter keeps climbing.
        snapshot_step_entry(flow, "01_plan_x")
        result = rewind_to_step(flow, "01_plan_x")
        assert result.generation == 3

    def test_retry_context_excludes_superseded_generations(self, tmp_path):
        """Rewinding must not resurrect the conversation it discarded."""
        record_prompt(
            tmp_path, "f1", "s1", "implement", "abandoned attempt", 0, generation=1,
        )
        record_response(
            tmp_path, "f1", "s1", "implement",
            json.dumps({"type": "result", "result": "old work"}), 0, generation=1,
        )
        record_prompt(
            tmp_path, "f1", "s1", "implement", "fresh attempt", 0, generation=2,
        )
        rebuilt = format_history_for_retry(
            tmp_path, "f1", "s1", current_generation=2,
        )
        assert "fresh attempt" in rebuilt
        assert "abandoned attempt" not in rebuilt

    def test_generation_zero_is_a_wildcard_for_legacy_records(self, tmp_path):
        """jsonl written before the field existed must stay visible."""
        record_prompt(tmp_path, "f1", "s2", "implement", "legacy prompt", 0)
        rebuilt = format_history_for_retry(
            tmp_path, "f1", "s2", current_generation=3,
        )
        assert "legacy prompt" in rebuilt

    def test_generation_is_omitted_from_serialization_when_zero(self, tmp_path):
        record_prompt(tmp_path, "f1", "s3", "implement", "p", 0)
        line = (
            tmp_path / "tianluo" / "history" / "f1" / "s3.jsonl"
        ).read_text(encoding="utf-8").splitlines()[0]
        assert "generation" not in json.loads(line)


class TestRebuildAfterRewind:
    """A rewind must leave the flow able to RE-ENTER the target step.

    The rewind deletes the step object, so without a rebuild the run loop
    would either read "no current step" as "flow finished" (rewind to the
    first step) or advance past the target (the step before it is COMPLETED).
    """

    def _state_machine(self, tmp_path):
        from tianluo.engine.persistence import PersistenceManager
        from tianluo.engine.state_machine import StateMachine

        return StateMachine(
            project_root=tmp_path, persistence=PersistenceManager(tmp_path)
        )

    def test_rebuild_produces_a_fresh_step_not_a_retry(self, tmp_path):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        old_id = "02_implement_x"
        flow.state.steps[old_id].inputs["retry_count"] = 3
        rewind_to_step(flow, old_id)

        step = self._state_machine(tmp_path).rebuild_rewound_step(
            flow, StepType.IMPLEMENT
        )

        assert step.step_id != old_id
        assert step.status == StepStatus.PENDING
        # A FRESH call: no retry markers, so nothing injects the abandoned
        # attempt's context into it.
        assert "retry_count" not in step.inputs
        assert "resumed" not in step.inputs
        assert flow.state.current_step_id == step.step_id
        assert flow.state.current_step_index == 1

    def test_rebuild_after_rewinding_to_the_first_step(self, tmp_path):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        rewind_to_step(flow, "01_plan_x")
        assert flow.state.current_step_id is None

        step = self._state_machine(tmp_path).rebuild_rewound_step(
            flow, StepType.PLAN
        )
        assert flow.state.current_step_id == step.step_id
        assert flow.state.current_step_index == 0
        assert flow.state.step_history == [step.step_id]

    def test_the_rebuilt_step_carries_the_dialog_note_once(self, tmp_path):
        """The note describes the situation the rebuilt step walks into, so it
        is consumed by that step and is stale advice for every step after."""
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        rewind_to_step(flow, "02_implement_x")
        flow.state.context["pending_dialog_note"] = "workspace already has edits"

        machine = self._state_machine(tmp_path)
        first = machine.rebuild_rewound_step(flow, StepType.IMPLEMENT)
        assert first.inputs["dialog_note"] == "workspace already has edits"
        assert "pending_dialog_note" not in flow.state.context

        second = machine.rebuild_rewound_step(flow, StepType.IMPLEMENT)
        assert "dialog_note" not in second.inputs

    def test_the_rebuilt_step_keeps_the_pre_step_workspace_baseline(
        self, tmp_path
    ):
        """INVESTIGATE's net-zero-diff guard is only meaningful against the
        tree as it was BEFORE the step first ran. The rewind deletes the step
        object holding that photograph, and a fresh capture would re-baseline
        onto the abandoned attempt's own unreverted probe edits and pass."""
        flow = _flow_with_steps(StepType.ANALYZE, StepType.INVESTIGATE)
        _snapshot_all_entries(flow)
        baseline = {"files": {"a.py": "hash-before"}, "available": True}
        flow.state.steps["02_investigate_x"].inputs["workspace_baseline"] = baseline

        rewind_to_step(flow, "02_investigate_x", cleanup_worktrees=False)
        step = self._state_machine(tmp_path).rebuild_rewound_step(
            flow, StepType.INVESTIGATE
        )

        assert step.inputs["workspace_baseline"] == baseline
        # Still a fresh call in every other respect.
        assert "retry_count" not in step.inputs
        assert rewind.PENDING_REWIND_INPUTS_KEY not in flow.state.context

    def test_a_reset_restart_does_not_carry_the_stale_baseline(self, tmp_path):
        """``workspace: reset`` puts the tree back to the flow's own baseline,
        so the leftovers the carried photograph exists to expose are gone and a
        fresh capture is the accurate one."""
        flow = _flow_with_steps(StepType.ANALYZE, StepType.INVESTIGATE)
        _snapshot_all_entries(flow)
        flow.state.steps["02_investigate_x"].inputs["workspace_baseline"] = {
            "files": {"a.py": "hash-before"}, "available": True,
        }

        rewind_to_step(
            flow, "02_investigate_x", cleanup_worktrees=False,
            carry_step_inputs=False,
        )
        step = self._state_machine(tmp_path).rebuild_rewound_step(
            flow, StepType.INVESTIGATE
        )
        assert "workspace_baseline" not in step.inputs

    def test_a_completed_target_carries_nothing_over(self, tmp_path):
        """A step that reached its clean end popped the baseline itself, so the
        re-entry photographs the tree it is actually walking into."""
        flow = _flow_with_steps(StepType.ANALYZE, StepType.INVESTIGATE)
        _snapshot_all_entries(flow)

        rewind_to_step(flow, "02_investigate_x", cleanup_worktrees=False)
        step = self._state_machine(tmp_path).rebuild_rewound_step(
            flow, StepType.INVESTIGATE
        )
        assert "workspace_baseline" not in step.inputs


class TestSelectedStepsIsRewound:
    """``selected_steps`` looks frozen at flow creation but is not: the state
    machine splices an ADJUDICATE slot in when self-check triggers adjudication
    and a CONFIRM gate after a confirmable step. A rewind that deleted those
    step objects while leaving their slots behind made the next transition
    rebuild an un-triggered ADJUDICATE, and shifted every later index."""

    def _flow_with_inserted_adjudicate(self):
        flow = _flow_with_steps(
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.COMMIT,
        )
        # Only the steps up to SELF_CHECK have actually run.
        for sid in list(flow.state.step_history):
            if sid.endswith("_commit_x"):
                flow.state.steps.pop(sid)
                flow.state.step_history.remove(sid)
        flow.state.current_step_id = flow.state.step_history[-1]
        flow.state.current_step_index = 4
        _snapshot_all_entries(flow)

        # SELF_CHECK triggers adjudication: a slot is spliced in ahead of it
        # and an ADJUDICATE step object is added.
        flow.state.selected_steps.insert(4, StepType.ADJUDICATE)
        adjudicate = Step(
            step_id="05_adjudicate_x",
            step_type=StepType.ADJUDICATE,
            status=StepStatus.RUNNING,
        )
        flow.state.add_step(adjudicate)
        flow.state.current_step_id = adjudicate.step_id
        flow.state.current_step_index = 4
        snapshot_step_entry(flow, adjudicate.step_id)
        return flow

    def test_the_spliced_slot_is_unwound_with_the_step_it_belonged_to(self):
        flow = self._flow_with_inserted_adjudicate()
        assert StepType.ADJUDICATE in flow.state.selected_steps

        rewind_to_step(flow, "03_implement_x", cleanup_worktrees=False)

        assert flow.state.selected_steps == [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.COMMIT,
        ]
        assert flow.state.current_step_index == 2

    def test_restarting_the_triggering_step_does_not_run_it_twice(self):
        """The SELF_CHECK snapshot holds the PRE-insertion index 4. Restoring
        it against a sequence that still carried the ADJUDICATE slot pointed at
        ADJUDICATE, so SELF_CHECK ran a second time after it."""
        flow = self._flow_with_inserted_adjudicate()

        rewind_to_step(flow, "05_self_check_x", cleanup_worktrees=False)

        index = flow.state.current_step_index
        assert flow.state.selected_steps[index] == StepType.SELF_CHECK

    def test_the_snapshot_survives_the_json_round_trip(self):
        """The snapshot lives in ``flow.state.context``, which persistence
        serialises with ``default=str`` — and that renders a ``StepType`` as
        the unparseable string ``"StepType.IMPLEMENT"``. Only the TOP-LEVEL
        ``selected_steps`` is re-coerced on load, so a snapshot left as enums
        came back from every daemon/json resume as junk strings and was
        restored verbatim, bricking the next transition."""
        flow = _flow_with_steps(
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
        )
        _snapshot_all_entries(flow)

        # Exactly what the persistence layer does to the context.
        reloaded = json.loads(
            json.dumps(flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY], default=str)
        )
        flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY] = reloaded

        rewind_to_step(flow, "02_implement_x", cleanup_worktrees=False)

        assert flow.state.selected_steps == [
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
        ]
        assert all(
            isinstance(item, StepType) for item in flow.state.selected_steps
        )

    def test_a_legacy_snapshot_of_stringified_enums_is_restored(self):
        """Snapshots written before the encoding existed hold
        ``"StepType.IMPLEMENT"``; an in-flight flow must stay rewindable
        across the upgrade rather than restoring junk."""
        flow = _flow_with_steps(
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
        )
        _snapshot_all_entries(flow)
        snapshots = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]
        for snap in snapshots.values():
            snap["state"]["selected_steps"] = [
                f"StepType.{s.name}" for s in flow.state.selected_steps
            ]

        rewind_to_step(flow, "02_implement_x", cleanup_worktrees=False)

        assert flow.state.selected_steps == [
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
        ]

    def test_a_flow_that_never_spliced_keeps_its_sequence(self):
        flow = _flow_with_steps(
            StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
        )
        _snapshot_all_entries(flow)
        before = list(flow.state.selected_steps)

        rewind_to_step(flow, "02_implement_x", cleanup_worktrees=False)

        assert flow.state.selected_steps == before


class TestRewindTargetMustNotBeLater:
    """Only the current step or an earlier one is a valid restart target: a
    rewind deletes forward, so a later target removes only itself while
    restoring a snapshot that unwinds the counters the surviving steps in
    between were re-armed under."""

    def _fix_loop_flow(self):
        flow = _flow_with_steps(
            StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
        )
        _snapshot_all_entries(flow)
        # TEST failed; the fix loop re-armed IMPLEMENT, which is now current.
        flow.state.fix_iterations = 1
        flow.state.steps["03_implement_x"].status = StepStatus.PENDING
        flow.state.current_step_id = "03_implement_x"
        flow.state.current_step_index = 2
        return flow

    def test_a_later_history_entry_is_rejected(self):
        flow = self._fix_loop_flow()
        with pytest.raises(RewindError):
            rewind.resolve_rewind_target(flow, "04_test_x")

    def test_the_flow_is_left_untouched(self):
        flow = self._fix_loop_flow()
        with pytest.raises(RewindError):
            rewind_to_step(flow, "04_test_x", cleanup_worktrees=False)
        assert flow.state.fix_iterations == 1
        assert "04_test_x" in flow.state.steps

    def test_the_current_step_itself_is_still_valid(self):
        flow = self._fix_loop_flow()
        assert rewind.resolve_rewind_target(flow, "03_implement_x") == (
            "03_implement_x"
        )

    def test_an_earlier_step_is_still_valid(self):
        flow = self._fix_loop_flow()
        assert rewind.resolve_rewind_target(flow, "02_plan_x") == "02_plan_x"


class TestGroupWorkIsPreservedBeforeDeletion:
    """A parallel implement step's work lives on leaf branches in their own
    worktrees — invisible to the main tree's status and to ``baseline..HEAD``.
    A restart deletes both, so it must save them first and say so."""

    def _dag_flow(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        step = flow.state.steps["02_implement_x"]
        step.inputs = {"task_groups": [{"group_id": "G1"}, {"group_id": "G2"}]}
        step.outputs = {"implemented_groups": ["G1"]}
        return flow

    def test_branches_are_preserved_before_they_are_cleaned(self, monkeypatch):
        order = []
        monkeypatch.setattr(
            rewind, "_preserve_group_work",
            lambda flow, branches, root: (
                order.append(("preserve", tuple(branches)))
                or ["refs/tianluo/discarded/rw-1/x/groups/impl_rw-1_G1"]
            ),
        )
        monkeypatch.setattr(
            rewind, "_cleanup_branches",
            lambda flow, branches, root: (
                order.append(("cleanup", tuple(branches))) or list(branches)
            ),
        )
        flow = self._dag_flow()
        result = rewind_to_step(flow, "02_implement_x", project_root="/tmp/p")

        assert [name for name, _ in order] == ["preserve", "cleanup"]
        assert result.preserved_refs == [
            "refs/tianluo/discarded/rw-1/x/groups/impl_rw-1_G1"
        ]
        assert result.to_dict()["preserved_refs"] == result.preserved_refs

    def test_rewind_group_branches_lists_planned_and_implemented(self):
        flow = self._dag_flow()
        assert rewind.rewind_group_branches(flow, "02_implement_x") == [
            "impl/rw-1/G1", "impl/rw-1/G2",
        ]

    def test_no_dag_step_means_no_branches(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.TEST)
        _snapshot_all_entries(flow)
        assert rewind.rewind_group_branches(flow, "01_plan_x") == []

    def test_a_failed_preservation_aborts_the_rewind_and_deletes_nothing(
        self, monkeypatch
    ):
        """A logged-and-skipped capture failure used to be followed by the
        unconditional cleanup, which deleted an interrupted group's worktree
        with no recovery ref pointing at its uncommitted edits."""
        from tianluo.engine.flow_workspace import GroupPreservationError

        cleaned = []
        monkeypatch.setattr(
            rewind, "_cleanup_branches",
            lambda flow, branches, root: cleaned.extend(branches),
        )

        def explode(root, flow_id, branches):
            raise GroupPreservationError(branches[0], "update-ref exploded")

        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.preserve_group_work", explode
        )
        flow = self._dag_flow()
        before = list(flow.state.step_history)
        generation_before = rewind.flow_generation(flow)

        with pytest.raises(rewind.RewindError) as excinfo:
            rewind_to_step(flow, "02_implement_x", project_root="/tmp/p")

        assert "impl/rw-1/G1" in str(excinfo.value)
        assert cleaned == []
        # The refusal happens before any state mutation, so the flow is intact.
        assert list(flow.state.step_history) == before
        assert "02_implement_x" in flow.state.steps
        assert rewind.flow_generation(flow) == generation_before

    def test_an_unexpected_preservation_error_also_aborts(self, monkeypatch):
        cleaned = []
        monkeypatch.setattr(
            rewind, "_cleanup_branches",
            lambda flow, branches, root: cleaned.extend(branches),
        )

        def explode(root, flow_id, branches):
            raise OSError("git is gone")

        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.preserve_group_work", explode
        )
        flow = self._dag_flow()

        with pytest.raises(rewind.RewindError) as excinfo:
            rewind_to_step(flow, "02_implement_x", project_root="/tmp/p")

        assert "git is gone" in str(excinfo.value)
        assert cleaned == []
        assert "02_implement_x" in flow.state.steps

    def test_a_refused_restart_surfaces_as_a_failed_outcome(self, tmp_path):
        """``apply_decision`` must report the refusal rather than crash — the
        flow is still runnable, so the operator gets to fix git and retry."""
        from tianluo.engine.flow_workspace import GroupPreservationError
        from tianluo.engine.interjection_dialog import (
            ACTION_RESTART, DialogDecision, apply_decision,
        )

        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]

        def explode(root, flow_id, branches):
            raise GroupPreservationError(branches[0], "commit-tree exploded")

        import tianluo.engine.flow_workspace as fw

        original = fw.preserve_group_work
        fw.preserve_group_work = explode
        try:
            outcome = apply_decision(
                flow, step,
                DialogDecision(
                    action=ACTION_RESTART, restart_step_id="02_implement_x",
                ),
                tmp_path,
            )
        finally:
            fw.preserve_group_work = original

        assert outcome.ok is False
        assert "impl/rw-1/G1" in outcome.error
        assert "02_implement_x" in flow.state.steps


class TestRestartRefusalsPrecedeTheReset:
    """A refused restart must leave the tree exactly as a refused target does.

    The reset discards the working tree to a safety ref and winds it back to
    ``baseline_commit``. A refusal discovered after that point (a group whose
    work cannot be captured or whose branch cannot be removed) used to return
    "nothing was deleted" while the tree was already emptied and every step
    still claimed to be done.
    """

    def _restart(self, flow, step, tmp_path, **kwargs):
        from tianluo.engine.interjection_dialog import (
            ACTION_RESTART, WORKSPACE_RESET, DialogDecision, apply_decision,
        )

        return apply_decision(
            flow, step,
            DialogDecision(
                action=ACTION_RESTART, restart_step_id="02_implement_x",
                workspace=WORKSPACE_RESET,
            ),
            tmp_path, **kwargs,
        )

    def _dag_flow(self):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        step = flow.state.steps["02_implement_x"]
        step.inputs = {"task_groups": [{"group_id": "G1"}]}
        return flow

    def _no_reset(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: calls.append("reset") or _Reset(),
        )
        return calls

    def test_a_failed_capture_leaves_the_tree_alone(self, tmp_path, monkeypatch):
        from tianluo.engine.flow_workspace import GroupPreservationError

        resets = self._no_reset(monkeypatch)
        monkeypatch.setattr(
            rewind, "_cleanup_branches", lambda *_a, **_k: [],
        )

        def explode(root, flow_id, branches):
            raise GroupPreservationError(branches[0], "update-ref exploded")

        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.preserve_group_work", explode
        )
        flow = self._dag_flow()
        outcome = self._restart(flow, flow.state.steps["02_implement_x"], tmp_path)

        assert outcome.ok is False
        assert resets == []
        assert outcome.reset is None
        assert "02_implement_x" in flow.state.steps

    def test_a_failed_group_cleanup_leaves_the_tree_alone(
        self, tmp_path, monkeypatch,
    ):
        resets = self._no_reset(monkeypatch)
        monkeypatch.setattr(
            rewind, "_preserve_group_work", lambda *_a, **_k: ["refs/x/1"],
        )

        def explode(flow, branches, root):
            raise RewindError("worktree still registered")

        monkeypatch.setattr(rewind, "_cleanup_branches", explode)
        flow = self._dag_flow()
        outcome = self._restart(flow, flow.state.steps["02_implement_x"], tmp_path)

        assert outcome.ok is False
        assert "worktree still registered" in outcome.error
        assert resets == []
        assert flow.state.step_history == ["01_plan_x", "02_implement_x"]

    def test_the_reset_runs_after_the_last_refusal_and_before_the_rewind(
        self, tmp_path, monkeypatch,
    ):
        order = []
        monkeypatch.setattr(
            rewind, "_preserve_group_work",
            lambda *_a, **_k: order.append("preserve") or ["refs/x/1"],
        )
        monkeypatch.setattr(
            rewind, "_cleanup_branches",
            lambda _flow, branches, _root: (
                order.append("cleanup") or list(branches)
            ),
        )
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: order.append("reset") or _Reset(),
        )
        flow = self._dag_flow()
        outcome = self._restart(flow, flow.state.steps["02_implement_x"], tmp_path)

        assert outcome.ok
        assert order == ["preserve", "cleanup", "reset"]
        assert flow.state.step_history == ["01_plan_x"]

    def test_a_failed_reset_still_names_the_preserved_group_refs(
        self, tmp_path, monkeypatch,
    ):
        """The groups are captured and removed while the rewind is planned, so
        a reset failure must not swallow the refs holding their work."""
        monkeypatch.setattr(
            rewind, "_preserve_group_work", lambda *_a, **_k: ["refs/x/1"],
        )
        monkeypatch.setattr(
            rewind, "_cleanup_branches",
            lambda _flow, branches, _root: list(branches),
        )
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: _Reset(ok=False, error="git exploded"),
        )
        flow = self._dag_flow()
        outcome = self._restart(flow, flow.state.steps["02_implement_x"], tmp_path)

        assert outcome.ok is False
        assert outcome.preserved_refs == ["refs/x/1"]
        # The rewind never committed, so the flow is still runnable.
        assert "02_implement_x" in flow.state.steps


class _Reset:
    """Stand-in for a :func:`reset_workspace_to_baseline` result."""

    def __init__(self, ok=True, error=""):
        self.ok = ok
        self.error = error
        self.safe_ref = "refs/tianluo/discarded/rw-1/t" if ok else ""
        self.restored_snapshot = ok
        self.warning = ""

    def recovery_hint(self):
        return "git checkout ..."


class TestEntrySnapshotAmend:
    def test_amend_recaptures_over_an_existing_snapshot(self):
        flow = _flow_with_steps(StepType.IMPLEMENT)
        flow.state.context["review_scope"] = "before"
        snapshot_step_entry(flow, "01_implement_x")
        flow.state.context["review_scope"] = "after-baseline"

        snapshot_step_entry(flow, "01_implement_x", amend=True)

        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["01_implement_x"]
        assert snap["context"]["review_scope"] == "after-baseline"

    def test_without_amend_the_first_capture_wins(self):
        flow = _flow_with_steps(StepType.IMPLEMENT)
        flow.state.context["review_scope"] = "before"
        snapshot_step_entry(flow, "01_implement_x")
        flow.state.context["review_scope"] = "after"

        snapshot_step_entry(flow, "01_implement_x")

        snap = flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY]["01_implement_x"]
        assert snap["context"]["review_scope"] == "before"


class TestAbandonedRestartInvalidatesDeletedGroupState:
    """A restart that is refused AFTER the group cleanup ran must not leave the
    flow believing in groups whose only copy it just deleted.

    The plan captures every DAG group worktree/leaf branch to a safety ref and
    removes them before the workspace reset runs. When the reset then fails the
    restart is handed back as refused and the flow keeps running — so a
    completed group still listed in ``implemented_groups`` would be skipped by
    the continuation while its branch no longer exists, and the end-of-DAG leaf
    merge would find nothing to merge. The flow would report work it does not
    have.
    """

    def _dag_flow(self, status=StepStatus.RUNNING):
        flow = _flow_with_steps(StepType.PLAN, StepType.IMPLEMENT)
        _snapshot_all_entries(flow)
        step = flow.state.steps["02_implement_x"]
        step.status = status
        step.inputs = {
            "task_groups": [{"group_id": "G1"}, {"group_id": "G2"}],
            "resumed": True,
        }
        step.outputs = {
            "implemented_groups": ["G1"],
            "group_summaries": [{"group_id": "G1", "summary": "did G1"}],
            "dag_preserved_worktrees": {
                "G1": {"branch": "impl/rw-1/G1", "status": "completed"},
                "G2": {"branch": "impl/rw-1/G2", "status": "running"},
            },
        }
        return flow

    def _patch_cleanup(self, monkeypatch, materialised=None, explode=None):
        monkeypatch.setattr(
            rewind, "_preserve_group_work", lambda *_a, **_k: ["refs/x/1"],
        )
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.materialised_group_branches",
            lambda _root, branches: (
                list(branches) if materialised is None else list(materialised)
            ),
        )
        if explode is not None:
            monkeypatch.setattr(rewind, "_cleanup_branches", explode)
        else:
            monkeypatch.setattr(
                rewind, "_cleanup_branches",
                lambda _flow, branches, _root: list(branches),
            )

    def _restart(self, flow, step, tmp_path, target="02_implement_x"):
        from tianluo.engine.interjection_dialog import (
            ACTION_RESTART, WORKSPACE_RESET, DialogDecision, apply_decision,
        )

        return apply_decision(
            flow, step,
            DialogDecision(
                action=ACTION_RESTART, restart_step_id=target,
                workspace=WORKSPACE_RESET,
            ),
            tmp_path,
        )

    def _failing_reset(self, monkeypatch):
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: _Reset(ok=False, error="git exploded"),
        )

    def test_a_failed_reset_drops_the_deleted_groups_results(
        self, tmp_path, monkeypatch,
    ):
        self._patch_cleanup(monkeypatch)
        self._failing_reset(monkeypatch)
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == ["02_implement_x"]
        # The flow is still runnable — and a `continue` from here re-runs G1
        # instead of skipping it onto a branch that no longer exists.
        assert "02_implement_x" in flow.state.steps
        assert not step.outputs.get("implemented_groups")
        assert not step.outputs.get("group_summaries")
        assert not step.outputs.get("dag_preserved_worktrees")
        # The work itself is not lost: it is under the safety refs.
        assert outcome.preserved_refs == ["refs/x/1"]

    def test_a_successful_restart_does_not_need_the_invalidation(
        self, tmp_path, monkeypatch,
    ):
        self._patch_cleanup(monkeypatch)
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: _Reset(),
        )
        flow = self._dag_flow()
        outcome = self._restart(flow, flow.state.steps["02_implement_x"], tmp_path)

        assert outcome.ok
        assert outcome.invalidated_group_steps == []
        # The step (and its group state with it) is gone entirely.
        assert "02_implement_x" not in flow.state.steps

    def test_a_completed_step_keeps_its_merged_group_results(
        self, tmp_path, monkeypatch,
    ):
        """A completed implement step's groups were merged into the flow's own
        tree, so deleting their leaf branches loses nothing and downstream
        steps still read the record. Only the worktree pointers go."""
        self._patch_cleanup(monkeypatch)
        self._failing_reset(monkeypatch)
        flow = self._dag_flow(status=StepStatus.COMPLETED)
        step = flow.state.steps["02_implement_x"]

        outcome = self._restart(flow, step, tmp_path, target="01_plan_x")

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == []
        assert step.outputs["implemented_groups"] == ["G1"]
        assert not step.outputs.get("dag_preserved_worktrees")

    def test_branches_that_never_existed_are_not_treated_as_discarded_work(
        self, tmp_path, monkeypatch,
    ):
        """A sequential (non-DAG) group run implements into the flow's own
        tree and materialises no branch at all — the derived branch names name
        nothing, so deleting them discards nothing and its record stands."""
        self._patch_cleanup(monkeypatch, materialised=[])
        self._failing_reset(monkeypatch)
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]
        step.outputs.pop("dag_preserved_worktrees")

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == []
        assert step.outputs["implemented_groups"] == ["G1"]

    def test_a_part_way_cleanup_failure_invalidates_what_it_deleted(
        self, tmp_path, monkeypatch,
    ):
        """The cleanup loop deletes branch by branch and only reports its
        leftovers at the end, so its refusal has real deletions behind it."""

        def explode(_flow, _branches, _root):
            raise RewindError(
                "worktree still registered",
                cleaned_branches=["impl/rw-1/G1"],
            )

        self._patch_cleanup(monkeypatch, explode=explode)
        resets = []
        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: resets.append("reset") or _Reset(),
        )
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert resets == []
        assert outcome.invalidated_group_steps == ["02_implement_x"]
        assert not step.outputs.get("implemented_groups")
        # Only the deleted group's record goes. G2's branch and worktree
        # survived the failed cleanup, so its pointer must survive too — it is
        # what lets the continuation adopt that worktree instead of trying to
        # create a second one for a branch that is still checked out there.
        assert step.outputs["dag_preserved_worktrees"] == {
            "G2": {"branch": "impl/rw-1/G2", "status": "running"},
        }

    def test_a_deleted_ref_under_a_failed_verification_is_invalidated(
        self, tmp_path, monkeypatch,
    ):
        """The refusal may come from the VERIFICATION, not the deletion: the
        residue probe can raise (git stopped answering) or a worktree directory
        can survive its rmtree while the branch ref really did go away. That
        group is both refused and dangling, so its record must still go."""

        def explode(_flow, _branches, _root):
            raise RewindError(
                "git for-each-ref timed out",
                cleaned_branches=[],
                deleted_branches=["impl/rw-1/G1"],
            )

        self._patch_cleanup(monkeypatch, explode=explode)
        self._failing_reset(monkeypatch)
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == ["02_implement_x"]
        assert not step.outputs.get("implemented_groups")
        assert set(step.outputs["dag_preserved_worktrees"]) == {"G2"}

    def test_a_part_way_cleanup_keeps_the_surviving_groups_worktree(
        self, tmp_path, monkeypatch,
    ):
        """A step owning one cleaned and one residual group keeps the residual
        group's adoption record — worktree directory, branch and all."""

        def explode(_flow, _branches, _root):
            raise RewindError(
                "worktree still registered",
                cleaned_branches=["impl/rw-1/G1"],
            )

        self._patch_cleanup(monkeypatch, explode=explode)
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]
        step.outputs["implemented_groups"] = ["G1", "G2"]
        step.outputs["group_summaries"] = [
            {"group_id": "G1", "summary": "did G1"},
            {"group_id": "G2", "summary": "did G2"},
        ]
        step.outputs["dag_preserved_worktrees"]["G2"]["worktree"] = str(
            tmp_path / "wt-g2"
        )

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == ["02_implement_x"]
        # G2 was never deleted: it stays both skippable and adoptable.
        assert step.outputs["implemented_groups"] == ["G2"]
        assert step.outputs["group_summaries"] == [
            {"group_id": "G2", "summary": "did G2"},
        ]
        assert set(step.outputs["dag_preserved_worktrees"]) == {"G2"}
        assert step.outputs["dag_preserved_worktrees"]["G2"]["worktree"] == str(
            tmp_path / "wt-g2"
        )

    def test_a_relay_heir_is_invalidated_by_its_recorded_branch(
        self, tmp_path, monkeypatch,
    ):
        """A relay heir has no branch of its own — it inherited its
        predecessor's, so that ref's deletion is what invalidates it, and its
        derived ``impl/<flow>/<group>`` name never existed."""
        self._patch_cleanup(monkeypatch, materialised=["impl/rw-1/G1"])
        self._failing_reset(monkeypatch)
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]
        step.outputs["implemented_groups"] = ["G1", "G2"]
        # G2 relays off G1: same branch, same worktree.
        step.outputs["dag_preserved_worktrees"]["G2"]["branch"] = "impl/rw-1/G1"

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == ["02_implement_x"]
        assert not step.outputs.get("implemented_groups")
        assert not step.outputs.get("dag_preserved_worktrees")

    def test_a_refusal_before_any_deletion_changes_nothing(
        self, tmp_path, monkeypatch,
    ):
        """A cleanup that deleted nothing leaves the record true."""

        def explode(_flow, _branches, _root):
            raise RewindError("worktree still registered")

        self._patch_cleanup(monkeypatch, explode=explode)
        flow = self._dag_flow()
        step = flow.state.steps["02_implement_x"]

        outcome = self._restart(flow, step, tmp_path)

        assert outcome.ok is False
        assert outcome.invalidated_group_steps == []
        assert step.outputs["implemented_groups"] == ["G1"]
        assert step.outputs["dag_preserved_worktrees"]


class TestCarriedInputKeyMatchesTheHandler:
    def test_the_carried_key_is_investigates_own_constant(self):
        """The rewind spells the key out to keep its no-step-imports posture,
        so a rename on the handler side has to be caught here."""
        from tianluo.engine.steps.investigate import BASELINE_INPUT_KEY

        assert BASELINE_INPUT_KEY in rewind._CARRIED_STEP_INPUT_KEYS
