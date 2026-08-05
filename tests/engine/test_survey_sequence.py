"""Tests for the ``survey`` task type — a pure-investigation flow.

A survey's deliverable is a conclusion, not a diff, so its sequence is
ANALYZE → INVESTIGATE → SUMMARIZE: no IMPLEMENT/TEST/COMMIT (nothing to write)
and no VERSION_ANALYZE (nothing to version). Two consequences are worth pinning
down, because both would fail *quietly* rather than loudly:

1. **A survey must not mutate the repository.** The sequence carries no commit
   step, so a survey run is expected to leave ``git log`` and the version file
   exactly as it found them. Asserting that against a real temp repo catches a
   future sequence edit that slips a committing step back in.

2. **A survey still merges cleanly under ``--worktree``.** Without a COMMIT
   step, ``append_worktree_merge_steps`` takes its tail-append fallback, and the
   isolation branch reaches the merge with zero commits on it. That has to end
   as a graceful no-op: ``integrate()`` reports the branch already-ancestor, and
   ``version_reconcile`` short-circuits on "this sequence never had a
   version_analyze step" with ``channel == "noop"`` rather than hard-faulting on
   a missing version intent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tianluo.cli import EXPLICIT_TASK_TYPES
from tianluo.config import append_worktree_merge_steps
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from tianluo.engine.state_machine import StateMachine
from tianluo.engine.steps.analyze import (
    _extract_task_type,
    _sanitize_analyzed_type,
    _update_flow_steps,
)


# A conclusive round: without it the INVESTIGATE bounded loop would schedule
# repeat rounds up to investigation.max_iterations before advancing, which is
# the investigation-loop suite's subject, not this one's.
CONCLUSIVE_REPORT = {
    "root_cause": "the daemon re-stats every issue file on each poll",
    "evidence": ["daemon/poller.py:88 walks tianluo/issues on every tick"],
    "files_involved": ["src/tianluo/daemon/poller.py"],
    "suggested_fix_direction": "cache the parsed issue YAML keyed by mtime",
    "confidence": "high",
    "conclusive": True,
}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_git_project(root: Path) -> str:
    """A committed one-commit git project. Returns the default branch name."""
    (root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "11.12.0"\n', encoding="utf-8"
    )
    (root / "VERSIONS.md").write_text(
        "# Demo Version History\n\n## 11.12.0 - 2026-07-06\n- baseline entry\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _register_handlers(sm, *, keep_merge_real: bool = False) -> list:
    """Pass-through handlers for every step; returns the executed-type log."""
    executed: list = []

    def mock_handler(step, flow):
        executed.append(step.step_type)
        step.status = StepStatus.COMPLETED
        if step.step_type == StepType.INVESTIGATE:
            step.outputs.update(CONCLUSIVE_REPORT)
            step.outputs["root_cause_report"] = dict(CONCLUSIVE_REPORT)
        else:
            step.outputs.setdefault("mock", True)
        return StepStatus.COMPLETED

    for step_type in StepType:
        sm.register_handler(step_type, mock_handler)

    if keep_merge_real:
        from tianluo.engine.steps import (
            merge_integrate_handler,
            version_reconcile_handler,
        )

        def wrap(real):
            def handler(step, flow):
                executed.append(step.step_type)
                return real(step, flow)

            return handler

        sm.register_handler(StepType.MERGE_INTEGRATE, wrap(merge_integrate_handler))
        sm.register_handler(
            StepType.VERSION_RECONCILE, wrap(version_reconcile_handler)
        )

    return executed


def _run_to_completion(sm, flow, *, max_steps: int = 30) -> int:
    steps = 0
    while (
        flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED)
        and steps < max_steps
    ):
        step = flow.state.get_current_step()
        if not step:
            break
        sm.run_step(flow, step)
        if step.status == StepStatus.FAILED:
            break
        sm.transition_to_next(flow)
        steps += 1
    return steps


class TestSurveyDefaultSequence:
    def test_sequence_is_exactly_analyze_investigate_summarize(self):
        assert get_default_step_sequence("survey") == [
            StepType.ANALYZE,
            StepType.INVESTIGATE,
            StepType.SUMMARIZE,
        ]

    @pytest.mark.parametrize(
        "forbidden",
        [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.COMMIT,
            StepType.VERSION_ANALYZE,
            StepType.SELF_CHECK,
            StepType.INVARIANT_CHECK,
        ],
    )
    def test_sequence_excludes_code_change_and_version_steps(self, forbidden):
        assert forbidden not in get_default_step_sequence("survey")

    def test_survey_is_an_explicit_cli_type(self):
        assert "survey" in EXPLICIT_TASK_TYPES

    def test_analyze_keeps_a_survey_classification(self):
        flow = FlowInstance(flow_id="f", task_description="why is it slow?")
        assert _extract_task_type({"task_type": "survey"}, flow) == "survey"
        assert _sanitize_analyzed_type({"task_type": "survey"}) == "survey"


class TestSurveySyncFlow:
    def test_flow_completes_leaving_git_and_version_untouched(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        _make_git_project(root)
        head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
        version_before = (root / "pyproject.toml").read_text(encoding="utf-8")

        sm = StateMachine(root)
        executed = _register_handlers(sm)
        flow = sm.create_flow(task_description="Why is the daemon hot when idle?",
                              task_type="survey")

        _run_to_completion(sm, flow)

        assert flow.status == FlowStatus.COMPLETED
        assert StepType.ANALYZE in executed
        assert StepType.INVESTIGATE in executed
        assert StepType.SUMMARIZE in executed
        # No committing / versioning step was ever reached...
        assert StepType.COMMIT not in executed
        assert StepType.VERSION_ANALYZE not in executed
        # ...and the repository is byte-for-byte as it started.
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before
        assert (root / "pyproject.toml").read_text(encoding="utf-8") == version_before

    def test_survey_investigate_round_is_bounded_not_endless(self, tmp_path):
        """A conclusive report advances immediately — one INVESTIGATE round."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_git_project(root)

        sm = StateMachine(root)
        executed = _register_handlers(sm)
        flow = sm.create_flow(task_description="Survey the runner adapters",
                              task_type="survey")
        _run_to_completion(sm, flow)

        assert executed.count(StepType.INVESTIGATE) == 1


class TestSurveyDeliverableReachesTheReport:
    """The investigation report IS the survey's deliverable.

    A survey makes no changes, runs no tests and creates no commit, so every
    other summarize input is empty. If the root-cause report does not reach
    SUMMARIZE, the only artifact the flow writes to disk describes a session
    that accomplished nothing, while the answer the user asked for stays buried
    in engine.json's step outputs.
    """

    def _survey_flow_with_report(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        _make_git_project(root)
        sm = StateMachine(root)
        flow = sm.create_flow(
            task_description="Why is the daemon hot when idle?",
            task_type="survey",
        )
        step = flow.state.get_current_step()  # ANALYZE
        step.status = StepStatus.COMPLETED
        step.outputs.update({"task_type": "survey", "scope": "daemon"})
        investigate = sm.transition_to_next(flow)
        investigate.status = StepStatus.COMPLETED
        investigate.outputs.update(CONCLUSIVE_REPORT)
        investigate.outputs["root_cause_report"] = dict(CONCLUSIVE_REPORT)
        return sm, flow

    def test_summarize_inputs_carry_the_report(self, tmp_path):
        sm, flow = self._survey_flow_with_report(tmp_path)

        summarize = sm.transition_to_next(flow)

        assert summarize.step_type == StepType.SUMMARIZE
        assert (
            summarize.inputs["root_cause_report"]["root_cause"]
            == CONCLUSIVE_REPORT["root_cause"]
        )
        assert len(summarize.inputs["investigation_history"]) == 1

    def test_report_stays_out_of_the_summarize_task_description(self, tmp_path):
        """Same intent-chain isolation PLAN/IMPLEMENT get."""
        sm, flow = self._survey_flow_with_report(tmp_path)

        summarize = sm.transition_to_next(flow)

        assert CONCLUSIVE_REPORT["root_cause"] not in summarize.inputs[
            "task_description"
        ]

    def test_summarize_prompt_renders_the_report(self, tmp_path):
        from tianluo.engine.steps.summarize import (
            SUMMARIZE_PROMPT,
            _build_investigation_section,
        )

        sm, flow = self._survey_flow_with_report(tmp_path)
        summarize = sm.transition_to_next(flow)

        section = _build_investigation_section(summarize)
        assert CONCLUSIVE_REPORT["root_cause"] in section
        assert CONCLUSIVE_REPORT["evidence"][0] in section
        assert "high" in section
        # The prompt has a slot for it, and the slot is honoured.
        assert "{investigation_section}" in SUMMARIZE_PROMPT

    def test_prompt_is_unchanged_when_nothing_was_investigated(self, tmp_path):
        from tianluo.engine.models import Step
        from tianluo.engine.steps.summarize import _build_investigation_section

        assert _build_investigation_section(
            Step(step_type=StepType.SUMMARIZE, status=StepStatus.PENDING)
        ) == ""

    def test_fallback_summary_still_carries_the_finding(self, tmp_path):
        """An LLM failure must not cost the user their answer."""
        from tianluo.engine.steps.summarize import _create_basic_summary_text

        flow = FlowInstance(
            flow_id="f", task_description="Why is the daemon hot?",
            task_type="survey",
        )
        text = _create_basic_summary_text(
            flow, {}, {}, "Why is the daemon hot?",
            root_cause_report=dict(CONCLUSIVE_REPORT),
        )

        assert CONCLUSIVE_REPORT["root_cause"] in text
        assert CONCLUSIVE_REPORT["evidence"][0] in text
        assert CONCLUSIVE_REPORT["files_involved"][0] in text


class TestSurveyWorktreeMerge:
    def test_worktree_sequence_keeps_the_merge_tail(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        _make_git_project(root)

        sm = StateMachine(root)
        _register_handlers(sm)
        flow = sm.create_flow(
            task_description="Survey the merge orchestrator",
            task_type="survey",
            is_worktree_mode=True,
        )
        seq = flow.state.selected_steps
        # No COMMIT to anchor on, so the pair lands on the tail (in order).
        assert StepType.COMMIT not in seq
        assert seq[-2:] == [StepType.MERGE_INTEGRATE, StepType.VERSION_RECONCILE]

    def test_analyze_time_rebuild_does_not_drop_the_merge_tail(self, tmp_path):
        """_update_flow_steps re-derives the sequence; the merge pair must survive."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_git_project(root)

        flow = FlowInstance(flow_id="f", task_description="survey", task_type="survey")
        flow.is_worktree_mode = True
        flow.state.context["project_root"] = str(root)

        _update_flow_steps(flow, "survey")

        seq = flow.state.selected_steps
        assert seq[0] == StepType.ANALYZE
        assert StepType.INVESTIGATE in seq
        assert seq[-2:] == [StepType.MERGE_INTEGRATE, StepType.VERSION_RECONCILE]

    def test_append_worktree_merge_steps_tail_fallback(self):
        seq = append_worktree_merge_steps(get_default_step_sequence("survey"))
        assert seq == [
            StepType.ANALYZE,
            StepType.INVESTIGATE,
            StepType.SUMMARIZE,
            StepType.MERGE_INTEGRATE,
            StepType.VERSION_RECONCILE,
        ]

    def test_zero_commit_merge_and_noop_reconcile_complete_gracefully(self, tmp_path):
        """The real integrate()/reconcile() libraries on a zero-commit survey branch.

        The isolation branch carries no commits and no version intent — exactly
        what a survey produces. integrate() must report it already-ancestor
        (nothing to merge) and reconcile() must take its by-design no-intent
        short-circuit, leaving the version untouched.
        """
        root = tmp_path / "proj"
        root.mkdir()
        default = _make_git_project(root)
        # A survey worktree branch: forked from the default branch, zero commits.
        _git(root, "branch", "worktree/survey-run", default)
        head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
        version_before = (root / "pyproject.toml").read_text(encoding="utf-8")

        sm = StateMachine(root)
        executed = _register_handlers(sm, keep_merge_real=True)
        flow = sm.create_flow(
            task_description="Survey the daemon idle path",
            task_type="survey",
            is_worktree_mode=True,
        )
        flow.worktree_branch = "worktree/survey-run"
        flow.worktree_original_branch = default

        _run_to_completion(sm, flow)

        assert flow.status == FlowStatus.COMPLETED
        assert StepType.MERGE_INTEGRATE in executed
        assert StepType.VERSION_RECONCILE in executed

        integrate_step = next(
            s for s in flow.state.steps.values()
            if s.step_type == StepType.MERGE_INTEGRATE
        )
        assert integrate_step.status == StepStatus.COMPLETED
        assert "worktree/survey-run" in (
            integrate_step.outputs["merge_result"]["already_ancestor_branches"] or []
        )

        reconcile_step = next(
            s for s in flow.state.steps.values()
            if s.step_type == StepType.VERSION_RECONCILE
        )
        assert reconcile_step.status == StepStatus.COMPLETED
        assert reconcile_step.outputs["channel"] == "noop"
        assert reconcile_step.outputs["final_version"] is None

        # Nothing landed and nothing was versioned.
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before
        assert (root / "pyproject.toml").read_text(encoding="utf-8") == version_before
