"""Tests for the INVESTIGATE bounded loop and root-cause context injection.

Two hard constraints are pinned here, both of which would fail *silently* if
they regressed:

1. The loop is expressed as a repeat-step (handler returns COMPLETED, the
   router creates the next round without advancing ``current_step_index``) —
   NOT as REVISION_NEEDED. ``transition_to_next`` only honours REVISION_NEEDED
   for TEST / SELF_CHECK / INVARIANT_CHECK / VERIFY_SPEC, so an INVESTIGATE
   returning it would fall through to plain progression and no round 2 would
   ever run. ``TestRevisionNeededIsNotTheLoopMechanism`` is the counter-example
   that keeps that motivation alive in the test suite.

2. The root-cause report reaches PLAN / IMPLEMENT through dedicated inputs keys
   ONLY. It must never appear in ``task_description`` / ``task_description_base``
   nor in self_check's verbatim-quote source pool — report text in the pool
   would let an LLM cite its own hypothesis as user intent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tianluo.config import InvestigationConfig, WorkflowConfig
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import StateMachine
from tianluo.engine.steps.self_check import _build_source_pool


REPORT_TEXT = (
    "resolve_flow_project_root returns a relative path, so the snapshot's "
    "git call runs in the wrong cwd and always reports clean"
)


def _report(conclusive: bool = True, **overrides) -> dict:
    report = {
        "root_cause": REPORT_TEXT,
        "evidence": ["workspace_snapshot.py:42 passes cwd=Path('.')"],
        "files_involved": ["src/tianluo/engine/workspace_snapshot.py"],
        "suggested_fix_direction": "resolve the project root to an absolute path",
        "confidence": "high" if conclusive else "low",
        "conclusive": conclusive,
    }
    report.update(overrides)
    return report


def _section_is_framework_context(template: str, fields: dict) -> bool:
    """Whether ``template``'s ``{root_cause_section}`` slot is framework context.

    The slot's offset is recovered by diffing the template rendered with an
    empty section against the same template rendered with a real one, so the
    verdict depends on where the slot *sits*, never on what the section says
    (the section carries its own ``## `` heading, which would otherwise fool a
    heading-scan of the filled prompt).
    """
    import os

    from tianluo.engine.prompt_markers import USER_CONTENT_BEGIN, USER_CONTENT_END
    from tianluo.engine.steps.plan import render_root_cause_section

    baseline = template.format(root_cause_section="", **fields)
    filled = template.format(
        root_cause_section=render_root_cause_section(_report()), **fields
    )
    offset = len(os.path.commonprefix([baseline, filled]))
    # A section that renders to nothing would put the offset past every
    # boundary below and pass on a technicality.
    assert REPORT_TEXT in filled

    td_start = baseline.index("## Task Description")
    td_end = baseline.find("\n## ", td_start + 1)
    if td_end < 0:
        td_end = len(baseline)
    if offset <= td_end:
        return False

    begin = baseline.find(USER_CONTENT_BEGIN)
    end = baseline.find(USER_CONTENT_END, begin + 1) if begin >= 0 else -1
    if 0 <= begin < offset < end:
        return False
    return True


def _make_state_machine(tmp_path, max_investigation_rounds=3):
    """StateMachine with a stubbed investigation cap (no yaml on disk needed)."""
    with patch("tianluo.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
    cfg = WorkflowConfig()
    sm._get_workflow_config = lambda **kwargs: cfg
    inv_cfg = InvestigationConfig(max_iterations=max_investigation_rounds)
    sm._get_investigation_max_iterations = lambda: inv_cfg.max_iterations
    return sm


def _make_flow(selected_steps=None, task_description="Fix the silent no-op"):
    flow = FlowInstance(
        flow_id="test-investigation-flow",
        task_description=task_description,
        task_type="bugfix",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = selected_steps or [
        StepType.ANALYZE,
        StepType.INVESTIGATE,
        StepType.PLAN,
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.COMMIT,
    ]
    return flow


def _add_step(flow, step_type, status, outputs=None, inputs=None):
    step = Step(
        step_type=step_type,
        status=status,
        inputs=inputs or {},
        outputs=outputs or {},
    )
    flow.state.add_step(step)
    return step


def _investigate_round(flow, conclusive, status=StepStatus.COMPLETED):
    """Append one finished INVESTIGATE round and make it the current step."""
    report = _report(conclusive=conclusive)
    step = _add_step(
        flow,
        StepType.INVESTIGATE,
        status,
        outputs={**report, "root_cause_report": report},
    )
    flow.state.current_step_id = step.step_id
    flow.state.current_step_index = flow.state.selected_steps.index(
        StepType.INVESTIGATE
    )
    return step


def _count_investigate_steps(flow) -> int:
    return sum(
        1
        for sid in flow.state.step_history
        if flow.state.steps[sid].step_type == StepType.INVESTIGATE
    )


# ---------------------------------------------------------------------------
# 1. Repeat-step loop
# ---------------------------------------------------------------------------


class TestInvestigateRepeatLoop:
    def test_inconclusive_round_creates_repeat_without_advancing(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)
        index_before = flow.state.current_step_index

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.INVESTIGATE
        assert next_step.status == StepStatus.PENDING
        assert flow.state.current_step_index == index_before
        assert flow.state.current_step_id == next_step.step_id
        assert _count_investigate_steps(flow) == 2

    def test_rounds_accumulate_in_step_history(self, tmp_path):
        """Three inconclusive rounds leave three INVESTIGATE steps at one slot."""
        sm = _make_state_machine(tmp_path, max_investigation_rounds=3)
        flow = _make_flow()
        index_before = flow.state.selected_steps.index(StepType.INVESTIGATE)

        _investigate_round(flow, conclusive=False)
        for expected_rounds in (2, 3):
            nxt = sm.transition_to_next(flow)
            assert nxt.step_type == StepType.INVESTIGATE
            assert _count_investigate_steps(flow) == expected_rounds
            # Mark the freshly created round as a finished, inconclusive one.
            report = _report(conclusive=False)
            nxt.status = StepStatus.COMPLETED
            nxt.outputs.update({**report, "root_cause_report": report})

        assert flow.state.current_step_index == index_before

    def test_conclusive_round_advances_to_plan(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        _investigate_round(flow, conclusive=True)

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.PLAN
        assert flow.state.current_step_index == flow.state.selected_steps.index(
            StepType.PLAN
        )
        assert _count_investigate_steps(flow) == 1

    def test_repeat_round_carries_iteration_metadata(self, tmp_path):
        sm = _make_state_machine(tmp_path, max_investigation_rounds=5)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)

        second = sm.transition_to_next(flow)

        assert second.inputs["investigation_iteration"] == 2
        assert second.inputs["investigation_max_iterations"] == 5
        prev = second.inputs["previous_investigation_reports"]
        assert len(prev) == 1
        assert prev[0]["root_cause"] == REPORT_TEXT

    def test_third_round_sees_both_earlier_reports(self, tmp_path):
        sm = _make_state_machine(tmp_path, max_investigation_rounds=5)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)
        second = sm.transition_to_next(flow)
        report = _report(conclusive=False, root_cause="second-round hypothesis")
        second.status = StepStatus.COMPLETED
        second.outputs.update({**report, "root_cause_report": report})

        third = sm.transition_to_next(flow)

        assert third.inputs["investigation_iteration"] == 3
        prev = third.inputs["previous_investigation_reports"]
        assert [r["root_cause"] for r in prev] == [
            REPORT_TEXT,
            "second-round hypothesis",
        ]


# ---------------------------------------------------------------------------
# 2. Bounds
# ---------------------------------------------------------------------------


class TestInvestigationBounds:
    def test_exhausted_budget_advances_to_plan_without_failing(self, tmp_path):
        """Rounds run out while still inconclusive -> continue, do NOT fail."""
        sm = _make_state_machine(tmp_path, max_investigation_rounds=2)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)
        second = sm.transition_to_next(flow)
        assert second.step_type == StepType.INVESTIGATE
        report = _report(conclusive=False)
        second.status = StepStatus.COMPLETED
        second.outputs.update({**report, "root_cause_report": report})

        third = sm.transition_to_next(flow)

        assert third.step_type == StepType.PLAN
        assert flow.status == FlowStatus.RUNNING
        assert flow.state.context.get("investigation_exhausted") is True
        assert _count_investigate_steps(flow) == 2

    def test_exhausted_flag_reaches_plan_inputs(self, tmp_path):
        sm = _make_state_machine(tmp_path, max_investigation_rounds=1)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)

        plan_step = sm.transition_to_next(flow)

        assert plan_step.step_type == StepType.PLAN
        assert plan_step.inputs["investigation_exhausted"] is True
        assert plan_step.inputs["root_cause_report"]["root_cause"] == REPORT_TEXT

    def test_zero_is_the_unlimited_sentinel(self, tmp_path):
        """max_iterations=0 keeps looping; only `conclusive` ends the loop."""
        sm = _make_state_machine(tmp_path, max_investigation_rounds=0)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)

        for expected_rounds in range(2, 8):
            nxt = sm.transition_to_next(flow)
            assert nxt.step_type == StepType.INVESTIGATE
            assert _count_investigate_steps(flow) == expected_rounds
            report = _report(conclusive=False)
            nxt.status = StepStatus.COMPLETED
            nxt.outputs.update({**report, "root_cause_report": report})

        assert flow.state.context.get("investigation_exhausted") is not True

        # A conclusive round is the only exit under the unlimited sentinel.
        current = flow.state.steps[flow.state.current_step_id]
        conclusive_report = _report(conclusive=True)
        current.outputs.update(
            {**conclusive_report, "root_cause_report": conclusive_report}
        )
        assert sm.transition_to_next(flow).step_type == StepType.PLAN

    def test_invalid_cap_fails_fast_at_flow_init(self, tmp_path):
        """A typo'd cap must surface somewhere — transitions only degrade."""
        from tianluo.config import ConfigError

        (tmp_path / "tianluo.yaml").write_text(
            "investigation:\n  max_iterations: -1\n", encoding="utf-8",
        )
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)

        with pytest.raises(ConfigError):
            sm.init_flow(_make_flow())

    def test_invalid_cap_mid_flow_degrades_instead_of_crashing(self, tmp_path):
        """A hot-edit to invalid yaml must not abort a running transition."""
        (tmp_path / "tianluo.yaml").write_text(
            "investigation:\n  max_iterations: -1\n", encoding="utf-8",
        )
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
        sm._get_workflow_config = lambda **kwargs: WorkflowConfig()
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)

        next_step = sm.transition_to_next(flow)

        # Default cap (3) applies, so round 2 is scheduled normally.
        assert next_step.step_type == StepType.INVESTIGATE
        assert next_step.inputs["investigation_max_iterations"] == 3

    def test_config_read_once_per_transition(self, tmp_path):
        """The cap is memoized per transition (the branch reads it twice)."""
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
        sm._get_workflow_config = lambda **kwargs: WorkflowConfig()
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)

        with patch(
            "tianluo.config.InvestigationConfig.load",
            return_value=InvestigationConfig(max_iterations=3),
        ) as loader:
            sm.transition_to_next(flow)

        assert loader.call_count == 1


# ---------------------------------------------------------------------------
# 3. Counter-example: REVISION_NEEDED must NOT drive this loop
# ---------------------------------------------------------------------------


class TestRevisionNeededIsNotTheLoopMechanism:
    """Pin the reason the loop uses repeat-steps instead of REVISION_NEEDED.

    ``transition_to_next``'s REVISION_NEEDED branch is hardcoded to TEST /
    SELF_CHECK / INVARIANT_CHECK / VERIFY_SPEC. An INVESTIGATE returning
    REVISION_NEEDED therefore skips it entirely and lands in ordinary
    progression — no repeat round, no error, nothing to notice. These tests
    demonstrate that failure mode so nobody "simplifies" the loop into it.
    """

    def test_revision_needed_produces_no_repeat_round(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        _investigate_round(
            flow, conclusive=False, status=StepStatus.REVISION_NEEDED,
        )

        next_step = sm.transition_to_next(flow)

        # Falls straight through to the next selected step: the loop that a
        # REVISION_NEEDED-based implementation would expect never happens.
        assert next_step.step_type == StepType.PLAN
        assert _count_investigate_steps(flow) == 1

    def test_revision_needed_does_not_route_into_the_fix_loop(self, tmp_path):
        """It also must not borrow the fix loop's counter or IMPLEMENT routing."""
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        _add_step(flow, StepType.IMPLEMENT, StepStatus.COMPLETED)
        _investigate_round(
            flow, conclusive=False, status=StepStatus.REVISION_NEEDED,
        )

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type != StepType.IMPLEMENT
        assert flow.state.get_fix_iteration() == 0


class TestSkippedRoundDoesNotLoop:
    """"Skip" at the failure gate must skip, not schedule another round.

    ``run.py``'s Retry/Skip/Abort gate implements Skip by force-setting the
    FAILED step to COMPLETED and calling ``transition_to_next``. Such a round
    produced no report, so its ``outputs`` has no ``conclusive`` key at all —
    reading that absence as "not conclusive" would loop the user back into the
    step they just chose to leave, and every new round is a fresh Step that
    re-baselines the workspace on the still-unreverted tree.
    """

    def _skipped_round(self, flow, outputs=None):
        """A FAILED round the user skipped: COMPLETED, but with no report."""
        step = _add_step(
            flow, StepType.INVESTIGATE, StepStatus.FAILED, outputs=outputs or {},
        )
        flow.state.current_step_id = step.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.INVESTIGATE
        )
        # What the failure gate does on choice "Skip step".
        step.status = StepStatus.COMPLETED
        return step

    def test_skipped_round_advances_instead_of_repeating(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        self._skipped_round(flow)

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.PLAN
        assert _count_investigate_steps(flow) == 1

    def test_skipped_round_after_a_real_one_still_advances(self, tmp_path):
        """The first round looped legitimately; skipping the second ends it."""
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)
        second = sm.transition_to_next(flow)
        assert second.step_type == StepType.INVESTIGATE

        second.status = StepStatus.COMPLETED  # forced by the Skip branch
        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.PLAN
        assert _count_investigate_steps(flow) == 2
        # Round 1's report is still the one PLAN plans against.
        assert next_step.inputs["root_cause_report"]["root_cause"] == REPORT_TEXT

    def test_an_explicit_false_verdict_still_loops(self, tmp_path):
        """Guard the gate itself: presence of the key, not its truthiness."""
        sm = _make_state_machine(tmp_path)
        flow = _make_flow()
        self._skipped_round(flow, outputs={"conclusive": False})

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.INVESTIGATE
        assert _count_investigate_steps(flow) == 2


# ---------------------------------------------------------------------------
# 4. Context injection + intent-chain isolation
# ---------------------------------------------------------------------------


class TestRootCauseContextInjection:
    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(tmp_path)

    def test_plan_inputs_carry_the_report(self, sm, tmp_path):
        flow = _make_flow()
        _investigate_round(flow, conclusive=True)

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["root_cause_report"]["root_cause"] == REPORT_TEXT
        assert len(inputs["investigation_history"]) == 1

    def test_implement_inputs_carry_the_report(self, sm, tmp_path):
        flow = _make_flow()
        _investigate_round(flow, conclusive=True)

        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)

        assert inputs["root_cause_report"]["root_cause"] == REPORT_TEXT

    def test_report_never_enters_the_task_description(self, sm, tmp_path):
        flow = _make_flow()
        _investigate_round(flow, conclusive=True)

        for step_type in (StepType.PLAN, StepType.IMPLEMENT, StepType.SELF_CHECK):
            inputs = sm._build_step_inputs(flow, step_type)
            assert REPORT_TEXT not in (inputs.get("task_description") or "")
            assert REPORT_TEXT not in (inputs.get("task_description_base") or "")

    def test_report_never_enters_the_self_check_source_pool(self, sm, tmp_path):
        """The whole reason the report is a separate inputs key."""
        flow = _make_flow()
        _add_step(flow, StepType.IMPLEMENT, StepStatus.COMPLETED)
        _investigate_round(flow, conclusive=True)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        pool = _build_source_pool(inputs)

        assert pool, "source pool should still carry the real task description"
        assert all(REPORT_TEXT not in entry for entry in pool)
        assert "root_cause_report" not in inputs

    def test_no_investigation_means_no_report_key(self, sm, tmp_path):
        flow = _make_flow(
            selected_steps=[StepType.PLAN, StepType.IMPLEMENT, StepType.COMMIT]
        )
        _add_step(flow, StepType.PLAN, StepStatus.COMPLETED, outputs={"plan": {}})

        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)

        assert "root_cause_report" not in inputs
        assert "investigation_exhausted" not in inputs

    def test_latest_round_wins(self, sm, tmp_path):
        flow = _make_flow()
        _investigate_round(flow, conclusive=False)
        second = sm.transition_to_next(flow)
        newer = _report(conclusive=True, root_cause="the real mechanism")
        second.status = StepStatus.COMPLETED
        second.outputs.update({**newer, "root_cause_report": newer})

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["root_cause_report"]["root_cause"] == "the real mechanism"
        assert len(inputs["investigation_history"]) == 2


# ---------------------------------------------------------------------------
# 5. Fix iterations must not lose the root cause
# ---------------------------------------------------------------------------


class TestFixIterationKeepsRootCause:
    def test_fix_transition_copies_the_report_onto_implement(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow = _make_flow(
            selected_steps=[
                StepType.INVESTIGATE,
                StepType.PLAN,
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.COMMIT,
            ]
        )
        _investigate_round(flow, conclusive=True)
        implement = _add_step(flow, StepType.IMPLEMENT, StepStatus.COMPLETED)
        test_step = _add_step(
            flow,
            StepType.TEST,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "make the failing test pass",
                "fix_context": {"reason": "test_failure"},
            },
        )
        flow.state.current_step_id = test_step.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(StepType.TEST)

        fix_step = sm.transition_to_next(flow)

        assert fix_step.step_id == implement.step_id
        assert fix_step.inputs["root_cause_report"]["root_cause"] == REPORT_TEXT
        # The intent chain is untouched by the fix path.
        assert REPORT_TEXT not in fix_step.inputs["task_description"]

    def test_no_report_leaves_fix_inputs_alone(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow = _make_flow(
            selected_steps=[
                StepType.PLAN,
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.COMMIT,
            ]
        )
        _add_step(flow, StepType.IMPLEMENT, StepStatus.COMPLETED)
        test_step = _add_step(
            flow,
            StepType.TEST,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "make the failing test pass",
                "fix_context": {},
            },
        )
        flow.state.current_step_id = test_step.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(StepType.TEST)

        fix_step = sm.transition_to_next(flow)

        assert "root_cause_report" not in fix_step.inputs


# ---------------------------------------------------------------------------
# 6. Prompt sections
# ---------------------------------------------------------------------------


class TestRootCausePromptSections:
    def test_plan_prompt_renders_the_section(self):
        from tianluo.engine.steps.plan import _build_prompt, render_root_cause_section

        section = render_root_cause_section(_report())
        prompt = _build_prompt(
            task_description="TD",
            task_type="bugfix",
            scope="S",
            project_summary="PS",
            revision_section="",
            root_cause_section=section,
        )

        assert "## Root-Cause Investigation Report" in prompt
        assert REPORT_TEXT in prompt
        assert "resolve the project root to an absolute path" in prompt
        assert "**Confidence:** high" in prompt
        # Section sits before the output schema.
        assert prompt.index("## Root-Cause Investigation Report") < prompt.index(
            "Respond in JSON format"
        )

    def test_plan_prompt_unchanged_without_a_report(self):
        from tianluo.engine.steps.plan import _build_prompt

        kwargs = dict(
            task_description="TD",
            task_type="bugfix",
            scope="S",
            project_summary="PS",
            revision_section="",
        )
        assert _build_prompt(**kwargs, root_cause_section="") == _build_prompt(**kwargs)
        assert "Root-Cause" not in _build_prompt(**kwargs)

    def test_implement_prompts_render_the_section(self):
        from tianluo.engine.steps.implement import (
            FIX_PROMPT,
            IMPLEMENT_GROUP_PROMPT,
            IMPLEMENT_PROMPT,
        )
        from tianluo.engine.steps.plan import render_root_cause_section

        section = render_root_cause_section(_report())
        rendered = [
            IMPLEMENT_PROMPT.format(
                task_description="TD", task_type="bugfix",
                task_groups="TG", root_cause_section=section,
            ),
            IMPLEMENT_GROUP_PROMPT.format(
                task_description="TD", task_type="bugfix",
                current_group="CG", previous_results="PR",
                root_cause_section=section,
            ),
            FIX_PROMPT.format(
                task_description="TD",
                fix_instructions="FI", fix_context="FC", fix_history="FH",
                fix_iteration=1, root_cause_section=section,
            ),
        ]
        for prompt in rendered:
            assert "## Root-Cause Investigation Report" in prompt
            assert REPORT_TEXT in prompt
            assert prompt.index("## Root-Cause Investigation Report") < prompt.index(
                "output a JSON summary"
            )

    def test_empty_section_leaves_no_residue(self):
        from tianluo.engine.steps.implement import IMPLEMENT_PROMPT

        prompt = IMPLEMENT_PROMPT.format(
            task_description="TD", task_type="bugfix",
            task_groups="TG", root_cause_section="",
        )
        assert "Root-Cause" not in prompt
        assert "## Task Type\nbugfix\n\n\n## Task Groups" in prompt

    def test_exhausted_budget_marks_the_section_low_confidence(self):
        from tianluo.engine.steps.plan import render_root_cause_section

        section = render_root_cause_section(
            _report(conclusive=False), exhausted=True,
        )

        assert "LOW" in section
        assert "best current hypothesis" in section
        assert "conclusive" in section

    def test_section_stays_outside_the_user_content_segment(self):
        """The report is framework context, never a user literal.

        Checked positionally on where the ``{root_cause_section}`` slot lands,
        not on the rendered text: the section must sit past the end of the
        ``## Task Description`` block, and outside a three-segment
        ``USER_CONTENT`` bubble should any of these templates ever gain one.
        The two synthetic counter-examples keep the checker honest — asserting
        the current two-segment prompts merely *lack* a user bubble would hold
        no matter where the slot moved.
        """
        from tianluo.engine.prompt_markers import wrap_user_section
        from tianluo.engine.steps.implement import (
            FIX_PROMPT,
            IMPLEMENT_GROUP_PROMPT,
            IMPLEMENT_PROMPT,
        )

        cases = [
            (IMPLEMENT_PROMPT, dict(
                task_description="TD", task_type="bugfix",
                task_groups="TG",
            )),
            (IMPLEMENT_GROUP_PROMPT, dict(
                task_description="TD", task_type="bugfix",
                current_group="CG", previous_results="PR",
            )),
            (FIX_PROMPT, dict(
                task_description="TD",
                fix_instructions="FI", fix_context="FC", fix_history="FH",
                fix_iteration=1,
            )),
        ]
        for template, fields in cases:
            assert _section_is_framework_context(template, fields)

        # Counter-example 1: slot spliced into the user's task text.
        assert not _section_is_framework_context(
            "P\n## Task Description\n{task_description}\n{root_cause_section}\n"
            "\n## Tail\nx\n",
            {"task_description": "TD"},
        )
        # Counter-example 2: slot inside a genuine three-segment user bubble.
        assert not _section_is_framework_context(
            wrap_user_section(
                "P\n",
                "## Task Description\n{task_description}\n\n"
                "## Notes\n{root_cause_section}\n",
                "\n## Tail\nx\n",
            ),
            {"task_description": "TD"},
        )

    def test_missing_or_malformed_report_renders_nothing(self):
        from tianluo.engine.steps.plan import render_root_cause_section

        assert render_root_cause_section(None) == ""
        assert render_root_cause_section({}) == ""
        assert render_root_cause_section("a string") == ""
        assert render_root_cause_section({"root_cause": "   "}) == ""

    def test_plan_handler_wires_inputs_into_the_prompt(self, tmp_path):
        """End-to-end: step.inputs -> rendered section inside the plan prompt."""
        from tianluo.engine.steps import plan as plan_mod

        flow = _make_flow()
        step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Fix the silent no-op",
                "task_type": "bugfix",
                "root_cause_report": _report(),
                "investigation_exhausted": True,
            },
        )
        captured = {}

        class _Caller:
            def __init__(self, *a, **kw):
                pass

            def call(self, prompt, **kwargs):
                captured["prompt"] = prompt
                return '{"task_groups": []}'

        with patch.object(plan_mod, "LLMCaller", _Caller), patch.object(
            plan_mod, "resolve_flow_project_root", return_value=tmp_path
        ):
            plan_mod.plan_handler(step, flow)

        assert "## Root-Cause Investigation Report" in captured["prompt"]
        assert REPORT_TEXT in captured["prompt"]
        assert "LOW" in captured["prompt"]
