"""Tests for the investigate step handler (net-zero-diff root-cause investigation)."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tianluo.engine.models import (
    STEP_POOL,
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.steps import STEP_HANDLERS
from tianluo.engine.steps.investigate import (
    INVESTIGATE_PROMPT,
    REVERT_PROMPT,
    _coerce_conclusive,
    _format_previous_reports,
    investigate_handler,
)
from tianluo.engine.workspace_snapshot import WorkspaceSnapshot


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "src.py").write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "src.py")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


@pytest.fixture
def flow(repo: Path) -> FlowInstance:
    flow = FlowInstance(
        flow_id="test-flow-inv",
        task_description="Uploads intermittently return 500",
        task_type="bugfix",
        status=FlowStatus.RUNNING,
        change_path=repo / "changes" / "test-change",
    )
    flow.state.context["project_root"] = str(repo)
    flow.state.selected_steps = [
        StepType.ANALYZE,
        StepType.INVESTIGATE,
        StepType.PLAN,
    ]
    return flow


@pytest.fixture
def step() -> Step:
    return Step(
        step_type=StepType.INVESTIGATE,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Uploads intermittently return 500",
            "scope": "src/upload/*",
            "investigation_iteration": 1,
            "investigation_max_iterations": 3,
        },
    )


CONCLUSIVE_RESPONSE = json.dumps({
    "root_cause": "the temp path is joined before normalization, so a relative "
                  "upload root silently resolves outside the storage dir",
    "evidence": ["src.py:12 joins before normpath", "reproduced with a relative root"],
    "files_involved": ["src.py"],
    "suggested_fix_direction": "normalize the root once at config load",
    "confidence": "high",
    "conclusive": True,
})

INCONCLUSIVE_RESPONSE = json.dumps({
    "root_cause": "possibly a race between the writer and the reaper",
    "evidence": ["failures cluster under concurrency"],
    "files_involved": ["src.py"],
    "suggested_fix_direction": "unclear yet — needs a narrower repro",
    "confidence": "low",
    "conclusive": False,
})


def _patched_caller(*responses: str):
    """Patch LLMCaller with a mock returning ``responses`` in order."""
    mock_caller = Mock()
    mock_caller.call.side_effect = list(responses)
    patcher = patch("tianluo.engine.steps.investigate.LLMCaller")
    mock_cls = patcher.start()
    mock_cls.return_value = mock_caller
    return patcher, mock_caller


class TestRegistration:
    def test_handler_is_registered(self) -> None:
        assert STEP_HANDLERS[StepType.INVESTIGATE] is investigate_handler

    def test_step_pool_entry_is_not_read_only(self) -> None:
        from tianluo.engine.context_builder import is_step_read_only

        info = STEP_POOL[StepType.INVESTIGATE]
        assert info["name"] == "investigate"
        assert info["read_only"] is False
        # WHY read_only=False: the tool layer must not ban writes, because the
        # net-zero-diff contract is enforced by snapshot comparison instead.
        assert is_step_read_only("investigate") is False


class TestPromptConstraints:
    def test_prompt_forbids_git_commit(self) -> None:
        assert "git commit" in INVESTIGATE_PROMPT
        assert "NO GIT COMMIT" in INVESTIGATE_PROMPT

    def test_prompt_routes_the_fix_to_plan_implement(self) -> None:
        assert "PLAN -> IMPLEMENT" in INVESTIGATE_PROMPT
        assert "DO NOT FIX THE PROBLEM" in INVESTIGATE_PROMPT

    def test_prompt_states_the_net_zero_diff_contract(self) -> None:
        assert "NET-ZERO DIFF" in INVESTIGATE_PROMPT
        assert "reverted" in INVESTIGATE_PROMPT

    def test_prompt_forbids_report_files(self) -> None:
        assert "do NOT write them into any project file" in INVESTIGATE_PROMPT

    def test_revert_prompt_forbids_destructive_git(self) -> None:
        for destructive in ("git reset --hard", "git stash", "git clean"):
            assert destructive in REVERT_PROMPT


class TestConclusiveParsing:
    def test_conclusive_report_lands_in_outputs(self, flow, step) -> None:
        patcher, _caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            result = investigate_handler(step, flow)
        finally:
            patcher.stop()

        assert result == StepStatus.COMPLETED
        assert step.outputs["conclusive"] is True
        assert step.outputs["confidence"] == "high"
        assert "normalization" in step.outputs["root_cause"]
        assert step.outputs["files_involved"] == ["src.py"]
        assert len(step.outputs["evidence"]) == 2
        assert step.outputs["suggested_fix_direction"]
        assert step.outputs["investigation_iteration"] == 1
        assert step.outputs["root_cause_report"]["conclusive"] is True

    def test_inconclusive_report_still_completes(self, flow, step) -> None:
        patcher, _caller = _patched_caller(INCONCLUSIVE_RESPONSE)
        try:
            result = investigate_handler(step, flow)
        finally:
            patcher.stop()

        # The handler never expresses looping itself — the state machine decides
        # from ``conclusive``. So an inconclusive round is still a COMPLETED step.
        assert result == StepStatus.COMPLETED
        assert step.outputs["conclusive"] is False
        assert step.outputs["confidence"] == "low"

    def test_missing_conclusive_defaults_to_false(self, flow, step) -> None:
        patcher, _caller = _patched_caller(json.dumps({"root_cause": "unclear"}))
        try:
            result = investigate_handler(step, flow)
        finally:
            patcher.stop()

        assert result == StepStatus.COMPLETED
        assert step.outputs["conclusive"] is False

    def test_unparsable_response_fails(self, flow, step) -> None:
        patcher, _caller = _patched_caller("I looked around and found nothing useful.")
        try:
            result = investigate_handler(step, flow)
        finally:
            patcher.stop()

        assert result == StepStatus.FAILED
        assert "root_cause" in step.error_message

    def test_llm_exception_fails_with_reason(self, flow, step) -> None:
        mock_caller = Mock()
        mock_caller.call.side_effect = RuntimeError("agent died")
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            result = investigate_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "agent died" in step.error_message


class TestNetZeroDiffEnforcement:
    def test_unrestored_changes_trigger_revert_then_fail(
        self, repo, flow, step
    ) -> None:
        """Leftover experimental changes: revert instruction first, THEN failure."""
        def _call(prompt: str = "", **_kwargs) -> str:
            # Both the investigation call and the revert call leave the probe
            # file behind, so the second snapshot comparison still fails.
            (repo / "probe.log").write_text("leftover\n", encoding="utf-8")
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _call
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            result = investigate_handler(step, flow)

        assert result == StepStatus.FAILED
        # Exactly two calls: the investigation, then one revert instruction.
        assert mock_caller.call.call_count == 2
        revert_prompt = mock_caller.call.call_args_list[1].kwargs["prompt"]
        assert "revert" in revert_prompt.lower()
        assert "probe.log" in revert_prompt
        assert "net-zero-diff" in step.error_message
        assert "probe.log" in step.outputs["workspace_delta"]

    def test_engine_never_reverts_the_workspace_itself(
        self, repo, flow, step
    ) -> None:
        """The FAILED path must run no destructive git and leave the tree alone."""
        def _call(prompt: str = "", **_kwargs) -> str:
            (repo / "probe.log").write_text("leftover\n", encoding="utf-8")
            (repo / "src.py").write_text("value = 1\nprint('probe')\n", encoding="utf-8")
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _call

        real_run = subprocess.run
        seen: list[list[str]] = []

        def _tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)):
                seen.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            with patch(
                "tianluo.engine.workspace_snapshot.subprocess.run",
                side_effect=_tracking_run,
            ):
                result = investigate_handler(step, flow)

        assert result == StepStatus.FAILED

        # ``subprocess.run`` is a module-global, so this sees every subprocess
        # the step triggered (snapshotting plus any injection helper), which is
        # exactly the surface the guard must cover.
        git_args = [cmd for cmd in seen if cmd and cmd[0] == "git"]
        destructive = {"reset", "checkout", "stash", "clean", "restore",
                       "commit", "rm", "apply"}
        for cmd in git_args:
            assert not (destructive & set(cmd)), (
                f"engine ran a destructive git command: {cmd}"
            )

        # The LLM's leftovers are STILL there — the engine cleaned up nothing.
        assert (repo / "probe.log").exists()
        assert "probe" in (repo / "src.py").read_text(encoding="utf-8")

    def test_revert_instruction_names_the_tracked_file(
        self, repo, flow, step
    ) -> None:
        """The revert call is a fresh one-shot with no memory of the probe.

        A temporary ``logger.debug`` left in an already-tracked file is the
        canonical instrument this step exists to allow, so the delta it produces
        must name the file — otherwise the reverting agent guesses against a tree
        that may also hold unrelated uncommitted work.
        """
        def _call(prompt: str = "", **_kwargs) -> str:
            (repo / "src.py").write_text(
                "value = 1\nlogger.debug('probe')\n", encoding="utf-8"
            )
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _call
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            result = investigate_handler(step, flow)

        assert result == StepStatus.FAILED
        revert_prompt = mock_caller.call.call_args_list[1].kwargs["prompt"]
        assert "src.py" in revert_prompt
        # The human reading the failure needs the same pointer.
        assert "src.py" in step.error_message
        assert "src.py" in step.outputs["workspace_delta"]

    def test_retry_after_a_violation_keeps_the_original_baseline(
        self, repo, flow, step
    ) -> None:
        """Retry must not re-baseline on a workspace still holding leftovers.

        run.py's Retry branch resets the step to PENDING and re-enters this
        handler with the unreverted probe still on disk. Taking a fresh baseline
        there would make round two byte-identical to its own dirty start — the
        step would report COMPLETED and the probe patch would travel on into
        PLAN/IMPLEMENT and get committed.
        """
        def _leaves_probe(prompt: str = "", **_kwargs) -> str:
            (repo / "src.py").write_text(
                "value = 1\nlogger.debug('probe')\n", encoding="utf-8"
            )
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _leaves_probe
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            assert investigate_handler(step, flow) == StepStatus.FAILED

        # What run.py's Retry choice does to the step.
        step.status = StepStatus.PENDING
        step.inputs["resumed"] = True
        step.inputs["retry_count"] = step.inputs.get("retry_count", 0) + 1

        # Round two changes nothing further — but the leftovers are still there.
        quiet_caller = Mock()
        quiet_caller.call.side_effect = lambda *a, **k: CONCLUSIVE_RESPONSE
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = quiet_caller
            assert investigate_handler(step, flow) == StepStatus.FAILED

        assert "src.py" in step.error_message
        assert "probe" in (repo / "src.py").read_text(encoding="utf-8")

    def test_retry_passes_once_the_leftovers_are_restored_by_hand(
        self, repo, flow, step
    ) -> None:
        """The persisted baseline still lets a genuinely clean retry succeed."""
        def _leaves_probe(prompt: str = "", **_kwargs) -> str:
            (repo / "probe.log").write_text("leftover\n", encoding="utf-8")
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _leaves_probe
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            assert investigate_handler(step, flow) == StepStatus.FAILED

        # The user restores the tree manually, then retries.
        (repo / "probe.log").unlink()
        step.status = StepStatus.PENDING
        step.inputs["retry_count"] = 1

        patcher, _caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            assert investigate_handler(step, flow) == StepStatus.COMPLETED
        finally:
            patcher.stop()

        assert step.outputs["conclusive"] is True
        # A satisfied contract drops the baseline rather than persisting a
        # per-file hash table with the flow.
        assert "workspace_baseline" not in step.inputs
        # The failed attempt's marker must not survive into the passing round,
        # or the step card would claim a dirty tree the check just found clean.
        assert "workspace_delta" not in step.outputs

    def test_retry_clears_an_earlier_undecidable_marker(
        self, repo, flow, step
    ) -> None:
        """A stale 'could not verify' note must not outlive its own attempt."""
        step.outputs["workspace_check"] = "- Workspace comparison unavailable"

        patcher, _caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            assert investigate_handler(step, flow) == StepStatus.COMPLETED
        finally:
            patcher.stop()

        assert "workspace_check" not in step.outputs

    def test_baseline_is_recorded_on_the_first_attempt(
        self, repo, flow, step
    ) -> None:
        def _leaves_probe(prompt: str = "", **_kwargs) -> str:
            (repo / "probe.log").write_text("leftover\n", encoding="utf-8")
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _leaves_probe
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            investigate_handler(step, flow)

        stored = step.inputs["workspace_baseline"]
        assert isinstance(stored, dict)
        # Persisted with the flow, so it must be plain JSON-able data.
        json.dumps(stored)
        assert "probe.log" not in stored["untracked"]

    def test_commit_made_during_the_step_fails_it(self, repo, flow, step) -> None:
        """A committed probe patch leaves no diff — HEAD is what exposes it.

        Without the HEAD comparison this step would pass while the experiment
        stays on the branch forever (and, under --worktree, gets merged back).
        """
        def _call(prompt: str = "", **_kwargs) -> str:
            (repo / "src.py").write_text("value = 1\nprint('probe')\n", encoding="utf-8")
            _git(repo, "commit", "-am", "debug")
            return CONCLUSIVE_RESPONSE

        mock_caller = Mock()
        mock_caller.call.side_effect = _call
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            result = investigate_handler(step, flow)

        assert result == StepStatus.FAILED
        assert mock_caller.call.call_count == 2
        assert "HEAD moved" in mock_caller.call.call_args_list[1].kwargs["prompt"]
        assert "HEAD moved" in step.outputs["workspace_delta"]

    def test_restored_workspace_after_revert_instruction_passes(
        self, repo, flow, step
    ) -> None:
        calls = {"n": 0}

        def _call(prompt: str = "", **_kwargs) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                (repo / "probe.log").write_text("leftover\n", encoding="utf-8")
                return CONCLUSIVE_RESPONSE
            # Revert call: the LLM cleans up its own experiment.
            (repo / "probe.log").unlink()
            return "reverted probe.log"

        mock_caller = Mock()
        mock_caller.call.side_effect = _call
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = mock_caller
            result = investigate_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert calls["n"] == 2
        assert not (repo / "probe.log").exists()
        assert "workspace_delta" not in step.outputs

    def test_preexisting_dirty_tree_does_not_fail_the_step(
        self, repo, flow, step
    ) -> None:
        """Uncommitted work carried in from earlier steps must not fail this one."""
        (repo / "src.py").write_text("value = 42\n", encoding="utf-8")
        (repo / "wip.txt").write_text("earlier work\n", encoding="utf-8")

        patcher, _caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            result = investigate_handler(step, flow)
        finally:
            patcher.stop()

        assert result == StepStatus.COMPLETED
        assert (repo / "wip.txt").exists()

    def test_undecidable_snapshot_does_not_fail_the_step(
        self, flow, step
    ) -> None:
        unavailable = WorkspaceSnapshot(available=False, unavailable_reason="no git")
        patcher, _caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            with patch(
                "tianluo.engine.steps.investigate.snapshot_workspace",
                return_value=unavailable,
            ):
                result = investigate_handler(step, flow)
        finally:
            patcher.stop()

        assert result == StepStatus.COMPLETED
        assert "unavailable" in step.outputs["workspace_check"].lower()


class TestBaselineSurvivesAHardKill:
    """The baseline must be on disk before the investigation call can be killed.

    ``run_step`` persists the flow exactly once between marking the step RUNNING
    and invoking the handler; the next save is after the handler returns. A
    baseline first written inside the handler therefore lives only in memory for
    the whole investigation call — the longest part of the step. A SIGKILL/OOM
    there loses it, and ``luo run --resume`` re-enters the step (PENDING, inputs
    preserved) to re-baseline on a tree that still carries the interrupted
    round's probe patch, which would then pass the net-zero-diff check silently.
    """

    @staticmethod
    def _state_machine(repo: Path):
        from tianluo.engine.state_machine import StateMachine

        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=repo)
        sm.register_handler(StepType.INVESTIGATE, investigate_handler)
        return sm

    def test_baseline_is_persisted_before_the_handler_runs(
        self, repo, flow, step
    ) -> None:
        saved_inputs: list = []

        sm = self._state_machine(repo)
        sm.persistence.save_flow.side_effect = lambda f: saved_inputs.append(
            dict(step.inputs)
        )

        patcher, _caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            assert sm.run_step(flow, step) == StepStatus.COMPLETED
        finally:
            patcher.stop()

        # The very first persist — the one that precedes the LLM call — already
        # carries the baseline.
        assert saved_inputs
        assert "workspace_baseline" in saved_inputs[0]

    def test_resume_after_a_mid_call_kill_still_catches_the_leftover(
        self, repo, flow, step
    ) -> None:
        """A SIGKILL during the LLM call: only the pre-handler save reached disk.

        The kill is modelled faithfully by discarding every in-memory change made
        after that save — which is exactly what a hard kill does — and rebuilding
        the resumed step from the persisted inputs alone.
        """
        sm = self._state_machine(repo)
        persisted: list = []
        sm.persistence.save_flow.side_effect = lambda f: persisted.append(
            copy.deepcopy(step.inputs)
        )

        def _probe_then_die(prompt: str = "", **_kwargs) -> str:
            (repo / "src.py").write_text(
                "value = 1\nlogger.debug('probe')\n", encoding="utf-8"
            )
            raise RuntimeError("the process dies here; nothing else is persisted")

        killed_caller = Mock()
        killed_caller.call.side_effect = _probe_then_die
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = killed_caller
            sm.run_step(flow, step)

        # persisted[0] is the save that precedes the handler — the last one a
        # mid-call kill lets through. run.py's --resume rebuilds the step from
        # exactly that, flipping RUNNING back to PENDING.
        resumed = Step(
            step_type=StepType.INVESTIGATE,
            status=StepStatus.PENDING,
            inputs=copy.deepcopy(persisted[0]),
        )
        resumed.inputs["resumed"] = True

        quiet_caller = Mock()
        quiet_caller.call.side_effect = lambda *a, **k: CONCLUSIVE_RESPONSE
        with patch("tianluo.engine.steps.investigate.LLMCaller") as mock_cls:
            mock_cls.return_value = quiet_caller
            assert sm.run_step(flow, resumed) == StepStatus.FAILED

        assert "src.py" in resumed.error_message
        assert "probe" in (repo / "src.py").read_text(encoding="utf-8")

    def test_capture_failure_does_not_break_the_step(self, repo, flow, step) -> None:
        """A failed capture degrades the guard; it must not abort the step."""
        sm = self._state_machine(repo)

        with patch(
            "tianluo.engine.steps.investigate.snapshot_workspace",
            side_effect=OSError("git blew up"),
        ):
            sm._ensure_investigation_baseline(flow, step)

        # Nothing stored, so a later attempt can still capture a real baseline.
        assert "workspace_baseline" not in step.inputs


class TestPromptAssembly:
    def test_previous_reports_are_injected_on_a_repeat_round(
        self, flow, step
    ) -> None:
        step.inputs["investigation_iteration"] = 2
        step.inputs["previous_investigation_reports"] = [
            {
                "root_cause": "suspected caching layer",
                "evidence": ["cache hit ratio drops"],
                "confidence": "low",
            }
        ]
        patcher, caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            investigate_handler(step, flow)
        finally:
            patcher.stop()

        prompt = caller.call.call_args_list[0].kwargs["prompt"]
        assert "suspected caching layer" in prompt
        assert "round 2 of at most 3" in prompt

    def test_first_round_has_no_previous_findings_section(self, flow, step) -> None:
        patcher, caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            investigate_handler(step, flow)
        finally:
            patcher.stop()

        prompt = caller.call.call_args_list[0].kwargs["prompt"]
        assert "previous rounds" not in prompt

    def test_unlimited_max_iterations_renders_as_unlimited(self, flow, step) -> None:
        step.inputs["investigation_max_iterations"] = 0
        patcher, caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            investigate_handler(step, flow)
        finally:
            patcher.stop()

        prompt = caller.call.call_args_list[0].kwargs["prompt"]
        assert "at most unlimited" in prompt

    def test_runtime_environment_block_is_injected_by_default(
        self, flow, step
    ) -> None:
        """Regression: 'investigate' must be in the runtime-env default
        whitelist — an unknown root cause is exactly the case where prior
        sessions (`luo history`) and issues are the best leads."""
        patcher, caller = _patched_caller(CONCLUSIVE_RESPONSE)
        try:
            investigate_handler(step, flow)
        finally:
            patcher.stop()

        prompt = caller.call.call_args_list[0].kwargs["prompt"]
        assert "## tianluo Runtime Environment" in prompt
        assert "luo history list" in prompt

    def test_missing_task_description_fails_fast(self, flow) -> None:
        flow.task_description = ""
        empty_step = Step(step_type=StepType.INVESTIGATE, inputs={})
        assert investigate_handler(empty_step, flow) == StepStatus.FAILED
        assert "task description" in empty_step.error_message.lower()


class TestPureHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, True), (False, False),
            ("true", True), ("TRUE", True), ("yes", True),
            ("false", False), ("maybe", False),
            (None, False), (1, False), ({}, False),
        ],
    )
    def test_coerce_conclusive(self, value, expected) -> None:
        assert _coerce_conclusive(value) is expected

    def test_format_previous_reports_empty(self) -> None:
        assert _format_previous_reports(None) == ""
        assert _format_previous_reports([]) == ""

    def test_format_previous_reports_renders_rounds(self) -> None:
        out = _format_previous_reports([
            {"root_cause": "A", "evidence": ["e1", "e2"], "confidence": "low"},
            {"root_cause": "B"},
        ])
        assert "Round 1" in out and "Round 2" in out
        assert "e1; e2" in out


class TestCliRendering:
    """The CLI must show a localized title + structured panel for the step."""

    @pytest.fixture(autouse=True)
    def _isolate_console(self):
        from tianluo.engine import display

        saved = display._console
        yield
        display._console = saved

    def _render(self, lang: str, step_obj) -> str:
        from io import StringIO

        from rich.console import Console

        from tianluo import i18n
        from tianluo.engine.display import set_console
        from tianluo.engine.step_renderers import render_step_output

        i18n.set_language(lang)
        buf = StringIO()
        set_console(Console(file=buf, width=100, force_terminal=False, highlight=False))
        render_step_output(step_obj)
        return buf.getvalue()

    def _completed_step(self) -> Step:
        return Step(
            step_type=StepType.INVESTIGATE,
            status=StepStatus.COMPLETED,
            outputs={
                "root_cause": "path joined before normalization",
                "evidence": ["src.py:12"],
                "files_involved": ["src.py"],
                "suggested_fix_direction": "normalize at config load",
                "confidence": "high",
                "conclusive": True,
                "investigation_iteration": 2,
            },
        )

    def test_renders_localized_panel_en_us(self) -> None:
        out = self._render("en-US", self._completed_step())
        assert "Root-Cause Investigation" in out
        assert "CONCLUSIVE" in out
        assert "Evidence" in out
        assert "Suggested Fix Direction" in out
        # LLM content passes through verbatim.
        assert "path joined before normalization" in out

    def test_renders_localized_panel_zh_cn(self) -> None:
        out = self._render("zh-CN", self._completed_step())
        assert "根因调查" in out
        assert "根因结论" in out
        assert "建议修复方向" in out
        # No raw step key leaks into the header.
        assert "investigate" not in out

    def test_step_display_title_is_localized(self) -> None:
        from tianluo import i18n
        from tianluo.engine.step_renderers import step_display_title

        i18n.set_language("en-US")
        assert step_display_title(StepType.INVESTIGATE) == "Root-Cause Investigation"
        i18n.set_language("zh-CN")
        assert step_display_title(StepType.INVESTIGATE) == "根因调查"

    def test_failed_step_surfaces_the_workspace_delta(self) -> None:
        failed = Step(
            step_type=StepType.INVESTIGATE,
            status=StepStatus.FAILED,
            outputs={"workspace_delta": "- New untracked files left behind: probe.log"},
            error_message="net-zero-diff violated",
        )
        out = self._render("en-US", failed)
        assert "probe.log" in out
        assert "net-zero-diff violated" in out
