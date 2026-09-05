"""The interjection dialog engine: context, reply parsing, decisions.

The dialog's interlocutor is the working agent inside its own session when one
is reachable, and a standalone read-only assistant otherwise. These tests cover
that choice, the structured reply contract, and what a confirmed decision does
to the flow — the terminal / web front ends are covered in ``tests/commands``.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from tianluo.engine import chat_history
from tianluo.engine.interjection_dialog import (
    ACTION_CONTINUE,
    ACTION_EXIT,
    ACTION_RESTART,
    MODE_DECISION,
    MODE_QUESTION,
    WORKSPACE_KEEP,
    WORKSPACE_RESET,
    DialogContext,
    DialogDecision,
    DialogTurn,
    InterjectionDialog,
    apply_decision,
    build_dialog_context,
    consume_gate_note,
    discard_gate_note,
    find_dialog_session,
    parse_dialog_reply,
    parse_direct_decision,
    summarize_transcript,
)
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


@pytest.fixture(autouse=True)
def _reset_ambient(monkeypatch):
    from tianluo.engine import llm_caller, rewind

    rewind.set_current_generation(0)
    llm_caller.clear_extra_prompt()
    llm_caller.consume_dialog_resume()
    yield
    rewind.set_current_generation(0)
    llm_caller.clear_extra_prompt()
    llm_caller.consume_dialog_resume()


def _flow(*step_types):
    flow = FlowInstance(
        flow_id="dlg-1",
        task_description="build the thing",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = list(step_types)
    for i, st in enumerate(step_types):
        step = Step(
            step_id=f"{i + 1:02d}_{st.value}_x",
            step_type=st,
            status=StepStatus.COMPLETED,
            inputs={},
        )
        flow.state.add_step(step)
    if flow.state.step_history:
        flow.state.current_step_id = flow.state.step_history[-1]
        flow.state.steps[flow.state.current_step_id].status = StepStatus.RUNNING
    # Entry snapshots are what makes a step rewindable; the real state machine
    # writes one at the top of every run_step, so a fixture flow needs them to
    # behave like a flow that has actually run.
    from tianluo.engine.rewind import snapshot_step_entry

    original_index = flow.state.current_step_index
    for index, sid in enumerate(list(flow.state.step_history)):
        flow.state.current_step_index = index
        snapshot_step_entry(flow, sid)
    flow.state.current_step_index = max(0, len(flow.state.step_history) - 1)
    del original_index
    return flow


class TestReplyParsing:
    def test_question_turn(self):
        turn = parse_dialog_reply(json.dumps({
            "mode": "question", "content": "Which database?", "decision": None,
        }))
        assert turn.mode == MODE_QUESTION
        assert turn.content == "Which database?"
        assert turn.decision is None
        assert not turn.is_decision

    def test_decision_turn(self):
        turn = parse_dialog_reply(json.dumps({
            "mode": "decision",
            "content": "Restarting from plan.",
            "decision": {
                "action": "restart",
                "restart_step_id": "01_plan_x",
                "workspace": "reset",
                "instruction": "be careful",
                "revised_description": "",
            },
        }))
        assert turn.is_decision
        assert turn.decision.action == ACTION_RESTART
        assert turn.decision.restart_step_id == "01_plan_x"
        assert turn.decision.workspace == WORKSPACE_RESET
        assert turn.decision.instruction == "be careful"

    def test_a_decision_with_no_body_is_a_question(self):
        turn = parse_dialog_reply(json.dumps({
            "mode": "decision", "content": "hmm", "decision": None,
        }))
        assert turn.mode == MODE_QUESTION

    def test_prose_reply_degrades_to_a_question(self):
        """Losing the conversation to a JSON slip would be far worse than
        showing the user a slightly ugly answer."""
        turn = parse_dialog_reply("I think you want Postgres, right?")
        assert turn.mode == MODE_QUESTION
        assert "Postgres" in turn.content

    def test_json_wrapped_in_prose_is_still_parsed(self):
        turn = parse_dialog_reply(
            'Sure!\n```json\n{"mode": "question", "content": "why?"}\n```\n'
        )
        assert turn.mode == MODE_QUESTION
        assert turn.content == "why?"

    def test_unknown_action_normalises_to_the_safest_choice(self):
        decision = DialogDecision.from_dict({"action": "teleport"})
        assert decision.action == ACTION_CONTINUE

    def test_unknown_workspace_normalises_to_keep(self):
        decision = DialogDecision.from_dict({"action": "restart", "workspace": "nuke"})
        assert decision.workspace == WORKSPACE_KEEP

    def test_non_mapping_decision_is_rejected(self):
        assert DialogDecision.from_dict("restart") is None


class TestDirectDecisions:
    def test_empty_input_means_resume_unchanged(self):
        decision = parse_direct_decision("")
        assert decision.action == ACTION_CONTINUE
        assert not decision.instruction

    def test_verbs_and_aliases(self):
        assert parse_direct_decision("continue").action == ACTION_CONTINUE
        assert parse_direct_decision("resume").action == ACTION_CONTINUE
        assert parse_direct_decision("restart").action == ACTION_RESTART
        assert parse_direct_decision("回退").action == ACTION_RESTART
        assert parse_direct_decision("exit").action == ACTION_EXIT
        assert parse_direct_decision("/quit").action == ACTION_EXIT

    def test_restart_takes_a_target_and_workspace(self):
        decision = parse_direct_decision("restart 02_implement_x reset")
        assert decision.action == ACTION_RESTART
        assert decision.restart_step_id == "02_implement_x"
        assert decision.workspace == WORKSPACE_RESET

    def test_restart_takes_the_workspace_word_alone(self):
        decision = parse_direct_decision("restart reset")
        assert decision.action == ACTION_RESTART
        assert decision.workspace == WORKSPACE_RESET
        assert not decision.restart_step_id

    def test_prose_is_not_a_direct_decision(self):
        assert parse_direct_decision("continue but use Postgres instead of SQLite") is None
        assert parse_direct_decision("why did you pick that library?") is None

    def test_a_verb_with_a_tail_is_prose_not_a_bare_decision(self):
        """Short prose that happens to start with a verb still goes to the LLM.

        Reading "continue use postgres" as a bare Continue silently dropped the
        instruction, resuming the step without what the operator asked for.
        Only the LLM can tell an instruction from a revised description.
        """
        assert parse_direct_decision("continue use postgres") is None
        assert parse_direct_decision("exit after this step") is None
        assert parse_direct_decision("restart implement use postgres") is None
        # Even a workspace word is meaningless outside ``restart``.
        assert parse_direct_decision("continue reset") is None


class TestSessionSelection:
    def test_session_is_found_from_the_step_history(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "p", 0,
            provider_session_id="sid-1", session_cwd=str(tmp_path),
            agent_name="dclaude",
        )
        binding = find_dialog_session(flow, step, tmp_path)
        assert binding["provider_session_id"] == "sid-1"
        assert binding["agent_name"] == "dclaude"

    def test_a_step_with_no_llm_call_has_no_session(self, tmp_path):
        """TEST / COMMIT / merge steps route to the standalone assistant."""
        flow = _flow(StepType.TEST)
        step = flow.state.steps[flow.state.current_step_id]
        assert find_dialog_session(flow, step, tmp_path) is None

    def test_rebuild_strategy_disables_the_session_path(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "llm_caller:\n  resume_strategy: rebuild\n", encoding="utf-8"
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "p", 0,
            provider_session_id="sid-1", agent_name="a",
        )
        assert find_dialog_session(flow, step, tmp_path) is None


class TestDialogContext:
    def test_context_carries_the_flow_shape(self, tmp_path):
        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        ctx = build_dialog_context(flow, step, tmp_path, binding={"x": 1})
        assert ctx.task_description == "build the thing"
        assert any("01_plan_x" in line for line in ctx.step_lines)
        assert any(line.startswith("▶") for line in ctx.step_lines)
        assert ctx.current_step_type == "implement"
        rendered = ctx.render()
        assert "Flow steps and their status" in rendered
        assert "Workspace changes so far" in rendered

    def test_rebuilt_history_is_rendered_only_without_a_session(self, tmp_path):
        """Re-feeding a lossy reconstruction to an agent that still HOLDS the
        conversation would invite it to contradict its own memory — but it is
        still ASSEMBLED, because whether that session is reachable is only
        knowable at call time and the fallback interlocutor needs it."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "MARKER", 0,
        )
        with_session = build_dialog_context(
            flow, step, tmp_path, binding={"provider_session_id": "s"}
        )
        assert "MARKER" in with_session.rebuilt_history
        assert "MARKER" not in with_session.render()
        # ...and the fallback rendering carries it.
        assert "MARKER" in with_session.render(include_history=True)

        standalone = build_dialog_context(flow, step, tmp_path, binding=None)
        assert "MARKER" in standalone.rebuilt_history
        assert "MARKER" in standalone.render()

    def test_rebuilt_history_is_scoped_to_the_current_fix_iteration(self, tmp_path):
        """IMPLEMENT re-uses one step_id across fix iterations, so an unscoped
        rebuild would hand the dialog agent superseded (and often
        contradictory) instructions from the iteration before."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.inputs["fix_iteration"] = 2
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "SUPERSEDED", 0,
            fix_iteration=1,
        )
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "LIVE", 0,
            fix_iteration=2,
        )
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "LEGACY", 0,
        )

        ctx = build_dialog_context(flow, step, tmp_path, binding=None)
        assert "LIVE" in ctx.rebuilt_history
        # Iteration 0 stays the compatibility wildcard for pre-field jsonl.
        assert "LEGACY" in ctx.rebuilt_history
        assert "SUPERSEDED" not in ctx.rebuilt_history


class TestSessionReachability:
    """A binding is a promise; it is only kept if the agent can honour it."""

    def _binding(self, runner_type="claude-code"):
        return {
            "agent_name": "primary",
            "provider_session_id": "sid-1",
            "session_cwd": "/tmp",
            "runner_type": runner_type,
        }

    def test_a_retyped_agent_drops_the_binding(self, tmp_path, monkeypatch):
        """A session id is only meaningful together with the runner that owns
        it, so re-pointing an agent name at another runner type invalidates it —
        exactly as LLMCaller's own resume precondition does."""
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod,
            "_agent_entry_for",
            lambda *_a, **_k: {"name": "primary", "type": "codex"},
        )
        monkeypatch.setattr(
            "tianluo.engine.llm_caller.runner_supports_native_resume",
            lambda _t: True,
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "MARKER", 0,
        )
        dialog = InterjectionDialog(
            flow, step, tmp_path, binding=self._binding("claude-code")
        )
        assert dialog.binding is None
        assert dialog.context.same_session is False
        assert "MARKER" in dialog._build_prompt("why?")

    def test_a_binding_without_a_runner_type_is_unusable(
        self, tmp_path, monkeypatch,
    ):
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod,
            "_agent_entry_for",
            lambda *_a, **_k: {"name": "primary", "type": "claude-code"},
        )
        monkeypatch.setattr(
            "tianluo.engine.llm_caller.runner_supports_native_resume",
            lambda _t: True,
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        binding = self._binding()
        binding.pop("runner_type")
        dialog = InterjectionDialog(flow, step, tmp_path, binding=binding)
        assert dialog.binding is None

    def test_a_removed_agent_drops_the_binding_and_restores_history(
        self, tmp_path, monkeypatch,
    ):
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod, "_agent_entry_for", lambda *_a, **_k: None
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "MARKER", 0,
        )
        dialog = InterjectionDialog(
            flow, step, tmp_path, binding=self._binding()
        )
        assert dialog.binding is None
        assert dialog.context.same_session is False
        assert "MARKER" in dialog._build_prompt("why?")

    def test_a_runner_without_resume_drops_the_binding(self, tmp_path, monkeypatch):
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod,
            "_agent_entry_for",
            lambda *_a, **_k: {"name": "primary", "type": "claude-code"},
        )
        monkeypatch.setattr(
            "tianluo.engine.llm_caller.runner_supports_native_resume",
            lambda _t: False,
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        dialog = InterjectionDialog(
            flow, step, tmp_path, binding=self._binding()
        )
        assert dialog.binding is None
        assert dialog.context.same_session is False

    def test_a_reachable_session_keeps_the_binding_but_still_arms_a_fallback(
        self, tmp_path, monkeypatch,
    ):
        """The provider can still refuse the session at call time, so the
        fallback prompt — the one carrying the rebuilt conversation — is handed
        to LLMCaller up front."""
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod,
            "_agent_entry_for",
            lambda *_a, **_k: {"name": "primary", "type": "claude-code"},
        )
        monkeypatch.setattr(
            "tianluo.engine.llm_caller.runner_supports_native_resume",
            lambda _t: True,
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "MARKER", 0,
        )
        dialog = InterjectionDialog(
            flow, step, tmp_path, binding=self._binding()
        )
        assert dialog.binding is not None
        assert "MARKER" not in dialog._build_prompt("why?")

        captured = {}

        class _Caller:
            def call(self, prompt, **_kw):
                captured["prompt"] = prompt
                return json.dumps({"mode": "question", "content": "because"})

        def _fake_llm_caller(**kwargs):
            captured["fallback"] = kwargs.get("resume_fallback_prompt")
            return _Caller()

        monkeypatch.setattr(
            "tianluo.engine.llm_caller.LLMCaller", _fake_llm_caller
        )
        dialog.ask("why?")
        assert "MARKER" not in captured["prompt"]
        assert "MARKER" in captured["fallback"]


class TestDialogTurnRecording:
    def _dialog(self, tmp_path, reply):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]

        class _Caller:
            def call(self, prompt, **kwargs):
                self.prompt = prompt
                return reply

        caller = _Caller()
        dialog = InterjectionDialog(
            flow, step, tmp_path, caller_factory=lambda _d: caller
        )
        return flow, step, dialog, caller

    def test_turns_are_recorded_as_dialog_kind(self, tmp_path):
        flow, step, dialog, _ = self._dialog(
            tmp_path,
            json.dumps({"mode": "question", "content": "Which database?"}),
        )
        dialog.ask("why are you using SQLite?")

        session = chat_history.get_step_history(tmp_path, flow.flow_id, step.step_id)
        kinds = [(m.kind, m.role, m.content) for m in session.messages]
        assert ("dialog", "user", "why are you using SQLite?") in kinds
        assert ("dialog", "assistant", "Which database?") in kinds

    def test_dialog_records_are_replayed_into_a_rebuilt_retry_context(
        self, tmp_path,
    ):
        """The conclusions reached mid-run are not carried anywhere else."""
        flow, step, dialog, _ = self._dialog(
            tmp_path, json.dumps({"mode": "question", "content": "Use Postgres."}),
        )
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "the step prompt", 0,
        )
        dialog.ask("switch to Postgres")

        rebuilt = chat_history.format_history_for_retry(
            tmp_path, flow.flow_id, step.step_id,
        )
        assert "Interjection Dialog" in rebuilt
        assert "switch to Postgres" in rebuilt
        assert "Use Postgres." in rebuilt

    def test_a_dialog_is_replayed_after_the_work_it_discussed(self, tmp_path):
        """A dialog record carries the STEP's retry_count while an attempt
        carries LLMCaller's external attempt, and the two diverge (a JSON retry
        advances the attempt without touching retry_count). Bucketing by attempt
        filed the dialog BEFORE the very work it was about."""
        flow, step, dialog, _ = self._dialog(
            tmp_path, json.dumps({"mode": "question", "content": "AGENT REPLY"}),
        )
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "FIRST PROMPT", 0,
        )
        # A JSON retry advanced LLMCaller's attempt; the step's retry_count did
        # not move, so this interrupted attempt is recorded as attempt 1.
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "SECOND PROMPT", 1,
        )
        dialog.ask("USER MESSAGE")

        rebuilt = chat_history.format_history_for_retry(
            tmp_path, flow.flow_id, step.step_id,
        )
        assert (
            rebuilt.index("FIRST PROMPT")
            < rebuilt.index("SECOND PROMPT")
            < rebuilt.index("USER MESSAGE")
            < rebuilt.index("AGENT REPLY")
        )
        # The dialog belongs to the attempt it interrupted; it does not open a
        # block of its own.
        assert rebuilt.count("=== Attempt ") == 2

    def test_legacy_interjection_records_stay_excluded(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "prompt", 0,
        )
        chat_history.record_user_interjection(
            tmp_path, flow.flow_id, step.step_id, "implement", "legacy insert",
        )
        rebuilt = chat_history.format_history_for_retry(
            tmp_path, flow.flow_id, step.step_id,
        )
        assert "legacy insert" not in rebuilt

    def test_prompt_carries_the_conversation_so_far(self, tmp_path):
        flow, step, dialog, caller = self._dialog(
            tmp_path, json.dumps({"mode": "question", "content": "ok"}),
        )
        dialog.ask("first question")
        dialog.ask("second question")
        assert "first question" in caller.prompt
        assert "This conversation so far" in caller.prompt

    def test_prompt_states_the_read_only_contract(self, tmp_path):
        _flow_, _step, dialog, caller = self._dialog(
            tmp_path, json.dumps({"mode": "question", "content": "ok"}),
        )
        dialog.ask("hello")
        assert "READ-ONLY" in caller.prompt


class TestUnreachableSessionCwd:
    """A DAG group's worktree can be removed between the interruption and the
    dialog. LLMCaller runs the native attempt AND its rebuild fallback in the
    same cwd, so a binding pointing at a deleted directory would fail twice and
    leave the round with no answer at all."""

    def _binding(self, cwd):
        return {
            "agent_name": "primary",
            "runner_type": "claude-code",
            "provider_session_id": "sid-1",
            "session_cwd": str(cwd),
        }

    def test_a_deleted_cwd_makes_the_binding_unusable(self, tmp_path, monkeypatch):
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod, "_agent_entry_for",
            lambda *_a, **_k: {"name": "primary", "type": "claude-code"},
        )
        monkeypatch.setattr(
            "tianluo.engine.llm_caller.runner_supports_native_resume",
            lambda _t: True,
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "MARKER", 0,
        )
        dialog = InterjectionDialog(
            flow, step, tmp_path,
            binding=self._binding(tmp_path / "gone-worktree"),
        )
        assert dialog.binding is None
        assert dialog.context.same_session is False
        # The standalone interlocutor has no memory of the step, so its prompt
        # must carry the rebuilt conversation.
        assert "MARKER" in dialog._build_prompt("why?")

    def test_a_cwd_deleted_mid_dialog_degrades_before_the_next_round(
        self, tmp_path
    ):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]

        class _Caller:
            native_resume_rejected = False

            def call(self, prompt, **kwargs):
                return json.dumps({"mode": "question", "content": "answered"})

        dialog = InterjectionDialog(
            flow, step, tmp_path, caller_factory=lambda _d: _Caller(),
        )
        dialog.binding = self._binding(worktree)
        import shutil

        shutil.rmtree(worktree)
        turn = dialog.ask("what now?")
        assert turn.content == "answered"
        assert dialog.binding is None

    def test_a_live_cwd_is_kept(self, tmp_path, monkeypatch):
        from tianluo.engine import interjection_dialog as mod

        monkeypatch.setattr(
            mod, "_agent_entry_for",
            lambda *_a, **_k: {"name": "primary", "type": "claude-code"},
        )
        monkeypatch.setattr(
            "tianluo.engine.llm_caller.runner_supports_native_resume",
            lambda _t: True,
        )
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        dialog = InterjectionDialog(
            flow, step, tmp_path, binding=self._binding(tmp_path)
        )
        assert dialog.binding is not None


class TestResumeFallbackAwareness:
    """After LLMCaller falls back from a refused native resume, the dialog is
    no longer talking to that session — and must stop saying (and recording)
    that it is."""

    def _dialog_with_binding(self, tmp_path, rejected):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        binding = {
            "agent_name": "primary",
            "runner_type": "claude-code",
            "provider_session_id": "sid-dead",
            "session_cwd": str(tmp_path),
        }

        class _Caller:
            def __init__(self):
                self.native_resume_rejected = rejected

            def call(self, prompt, **kwargs):
                return json.dumps({"mode": "question", "content": "answered"})

        dialog = InterjectionDialog(
            flow, step, tmp_path, caller_factory=lambda _d: _Caller(),
        )
        # caller_factory bypasses the session resolution, so set the binding
        # the way a live dialog would have it.
        dialog.binding = binding
        return flow, step, dialog

    def test_a_rejected_resume_drops_the_binding(self, tmp_path):
        _flow_, _step, dialog = self._dialog_with_binding(tmp_path, True)
        dialog.ask("why?")
        assert dialog.binding is None

    def test_a_working_session_keeps_the_binding(self, tmp_path):
        _flow_, _step, dialog = self._dialog_with_binding(tmp_path, False)
        dialog.ask("why?")
        assert dialog.binding is not None

    def test_later_turns_are_not_stamped_with_the_dead_session(self, tmp_path):
        flow, step, dialog = self._dialog_with_binding(tmp_path, True)
        dialog.ask("first")
        dialog.ask("second")
        session = chat_history.get_step_history(tmp_path, flow.flow_id, step.step_id)
        later = [m for m in session.messages if m.content == "second"]
        assert later and later[0].provider_session_id is None


class TestDialogNoteScoping:
    """The temporary instruction belongs to ONE step's ONE execution."""

    def _machine(self, tmp_path):
        from tianluo.engine.persistence import PersistenceManager
        from tianluo.engine.state_machine import StateMachine

        return StateMachine(
            project_root=tmp_path, persistence=PersistenceManager(tmp_path)
        )

    def _run(self, tmp_path, flow, step, handler):
        machine = self._machine(tmp_path)
        machine._handlers[step.step_type] = handler
        return machine.run_step(flow, step)

    def test_the_step_that_makes_an_llm_call_receives_it(self, tmp_path):
        from tianluo.engine import llm_caller

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.PENDING
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="use X"),
            tmp_path,
        )
        seen = {}

        def _handler(_step, _flow):
            seen["extra"] = llm_caller.get_extra_prompt()
            seen["dialog_resume"] = llm_caller.consume_dialog_resume()
            return StepStatus.COMPLETED

        self._run(tmp_path, flow, step, _handler)
        assert "use X" in (seen["extra"] or "")
        assert seen["dialog_resume"] is True

    def test_a_non_llm_step_does_not_leak_it_to_the_next_step(self, tmp_path):
        """TEST / COMMIT consume no extra prompt. Left armed, the user's
        step-specific instruction would surface in the next LLM step."""
        from tianluo.engine import llm_caller

        flow = _flow(StepType.TEST)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.PENDING
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="use X"),
            tmp_path,
        )

        self._run(tmp_path, flow, step, lambda *_a: StepStatus.COMPLETED)

        assert llm_caller.get_extra_prompt() is None
        assert llm_caller.consume_dialog_resume() is False
        # Consumed from the step too, so a later attempt does not re-apply a
        # one-off instruction.
        assert "dialog_note" not in step.inputs
        assert "dialog_resume" not in step.inputs


class TestDialogNoteInvalidatesThePhase1Cache:
    """INVARIANT: a dialog conclusion always reaches an agent.

    A ``two_phase`` step with a cached Phase-1 output skips Phase 1 outright on
    any ``external_attempt > 0`` re-arm and runs only the self-contained Phase-2
    extraction (retry context suppressed by contract). The note, the dialog
    framing and the recomposed description would then be delivered to nobody and
    the step would complete from the superseded Phase-1 output.
    """

    def _cache_path(self, tmp_path, flow, step):
        from tianluo.engine.chat_history import _history_dir

        path = _history_dir(tmp_path, flow.flow_id) / f"{step.step_id}_phase1.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale phase 1 output", encoding="utf-8")
        return path

    def _run(self, tmp_path, flow, step):
        from tianluo.engine.persistence import PersistenceManager
        from tianluo.engine.state_machine import StateMachine

        machine = StateMachine(
            project_root=tmp_path, persistence=PersistenceManager(tmp_path)
        )
        machine._handlers[step.step_type] = lambda *_a: StepStatus.COMPLETED
        return machine.run_step(flow, step)

    def test_a_continue_carrying_an_instruction_clears_the_cache(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.PENDING
        cache = self._cache_path(tmp_path, flow, step)
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="also add tests for X"),
            tmp_path,
        )

        self._run(tmp_path, flow, step)

        assert not cache.exists()

    def test_a_revised_description_alone_also_clears_it(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.PENDING
        cache = self._cache_path(tmp_path, flow, step)
        apply_decision(
            flow, step,
            DialogDecision(
                action=ACTION_CONTINUE, revised_description="the corrected task",
            ),
            tmp_path,
        )

        self._run(tmp_path, flow, step)

        assert not cache.exists()

    def test_an_empty_continue_keeps_the_cache(self, tmp_path):
        """"Change nothing, resume now" has nothing to say to an agent, and
        re-running a completed Phase 1 would only cost another call."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.PENDING
        cache = self._cache_path(tmp_path, flow, step)
        apply_decision(flow, step, DialogDecision(action=ACTION_CONTINUE), tmp_path)

        self._run(tmp_path, flow, step)

        assert cache.exists()


class TestApplyDecision:
    def test_continue_rearms_the_step_as_a_retry(self, tmp_path):
        from tianluo.engine import llm_caller

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.inputs["retry_count"] = 2

        outcome = apply_decision(
            flow, step, DialogDecision(action=ACTION_CONTINUE, instruction="use X"),
            tmp_path, dialog_summary="User: use X",
        )

        assert outcome.ok
        assert step.status == StepStatus.PENDING
        assert step.inputs["resumed"] is True
        assert step.inputs["retry_count"] == 3
        # Scoped to THIS step, not to LLMCaller's process-global slot: a
        # non-LLM step would never consume the latter and the instruction
        # would surface in an unrelated later step.
        assert "use X" in step.inputs["dialog_note"]
        assert step.inputs["dialog_resume"] is True
        assert llm_caller.get_extra_prompt() is None
        assert llm_caller.consume_dialog_resume() is False
        del llm_caller

    def test_continue_with_a_revision_records_it_and_recomposes(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step,
            DialogDecision(
                action=ACTION_CONTINUE, revised_description="the corrected task",
            ),
            tmp_path,
        )
        assert outcome.revised is True
        assert step.inputs["task_description"] == "the corrected task"

    def test_a_revision_forces_a_full_self_check_round(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.SELF_CHECK]
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, revised_description="new task"),
            tmp_path,
        )
        assert flow.state.context.get("self_check_review")

    def test_exit_does_not_rearm_the_step(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step, DialogDecision(action=ACTION_EXIT), tmp_path,
        )
        assert outcome.action == ACTION_EXIT
        assert step.status == StepStatus.RUNNING

    def test_exit_still_records_a_confirmed_revision(self, tmp_path):
        """The revision is a fact about the REQUIREMENT, not about this run:
        an operator who corrects it and then saves-and-leaves must find the
        correction still in force when they resume."""
        from tianluo.engine.state_machine import _effective_task_description_base

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_EXIT, revised_description="the new ask"),
            tmp_path,
        )
        assert outcome.action == ACTION_EXIT
        assert outcome.revised is True
        assert _effective_task_description_base(flow) == "the new ask"
        # ...without re-arming the step it is leaving.
        assert step.status == StepStatus.RUNNING

    def test_exit_with_a_revision_recomposes_the_interrupted_step(self, tmp_path):
        """``--resume`` only flips the retry flags, and a native resume sends
        nothing but the continuation directive — so without this the resumed
        agent carries on against the description the operator just replaced."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_EXIT, revised_description="the new ask"),
            tmp_path,
        )
        assert step.inputs["task_description"] == "the new ask"
        assert "the new ask" in step.inputs["dialog_note"]
        assert step.inputs["dialog_resume"] is True
        # Still not re-armed: exiting is not a retry.
        assert step.status == StepStatus.RUNNING
        assert "retry_count" not in step.inputs

    def test_a_plain_exit_leaves_the_step_alone(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        before = dict(step.inputs or {})
        apply_decision(
            flow, step, DialogDecision(action=ACTION_EXIT), tmp_path,
        )
        assert step.inputs == before

    def test_restart_rewinds_and_leaves_a_note(self, tmp_path):
        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_RESTART, restart_step_id="01_plan_x"),
            tmp_path, dialog_summary="User: start over",
        )
        assert outcome.ok
        assert outcome.rewind.target_step_id == "01_plan_x"
        assert flow.state.step_history == []
        note = flow.state.context["pending_dialog_note"]
        assert "workspace still contains" in note
        assert "start over" in note

    def test_keep_does_not_claim_deleted_group_work_is_still_there(
        self, tmp_path, monkeypatch,
    ):
        """``workspace: keep`` keeps the flow's OWN tree, but a parallel
        implement step's work was on leaf branches in their own worktrees and
        the rewind removed them. Telling the rebuilt step "the workspace still
        contains the changes from the previous attempt" sent it looking for work
        that is no longer on disk."""
        from tianluo.engine import rewind as rewind_mod

        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.inputs = {"task_groups": [{"group_id": "G1"}]}
        monkeypatch.setattr(
            rewind_mod, "_preserve_group_work",
            lambda *_a, **_k: ["refs/tianluo/discarded/f1/t/groups/impl_f1_G1"],
        )
        monkeypatch.setattr(
            rewind_mod, "_cleanup_branches",
            lambda _flow, branches, _root: list(branches),
        )

        outcome = apply_decision(
            flow, step,
            DialogDecision(
                action=ACTION_RESTART, restart_step_id="02_implement_x",
            ),
            tmp_path,
        )

        assert outcome.ok
        note = flow.state.context["pending_dialog_note"]
        assert "impl/dlg-1/G1" in note
        assert "no longer exist" in note
        assert "refs/tianluo/discarded/f1/t/groups/impl_f1_G1" in note

    def test_keep_without_dag_groups_keeps_the_plain_wording(self, tmp_path):
        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_RESTART, restart_step_id="01_plan_x"),
            tmp_path,
        )
        assert outcome.ok
        note = flow.state.context["pending_dialog_note"]
        assert "workspace still contains" in note
        assert "no longer exist" not in note

    def test_restart_with_reset_announces_the_clean_tree(self, tmp_path, monkeypatch):
        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]

        class _Reset:
            ok = True
            error = ""
            safe_ref = "refs/tianluo/discarded/x/1"
            restored_snapshot = True

            def recovery_hint(self):
                return "git checkout ..."

        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: _Reset(),
        )
        outcome = apply_decision(
            flow, step,
            DialogDecision(
                action=ACTION_RESTART, restart_step_id="01_plan_x",
                workspace=WORKSPACE_RESET,
            ),
            tmp_path,
        )
        assert outcome.ok
        assert "was reset" in flow.state.context["pending_dialog_note"]

    def test_a_failed_reset_aborts_the_restart(self, tmp_path, monkeypatch):
        """Rewinding after a failed reset would leave the tree and the flow
        state disagreeing about what happened."""
        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]

        class _Reset:
            ok = False
            error = "git exploded"
            safe_ref = ""
            restored_snapshot = False

            def recovery_hint(self):
                return ""

        monkeypatch.setattr(
            "tianluo.engine.flow_workspace.reset_workspace_to_baseline",
            lambda *_a, **_k: _Reset(),
        )
        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_RESTART, workspace=WORKSPACE_RESET),
            tmp_path,
        )
        assert outcome.ok is False
        assert "git exploded" in outcome.error
        assert flow.state.step_history  # nothing was rewound

    def test_an_invalid_restart_target_is_reported(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_RESTART, restart_step_id="99_nope"),
            tmp_path,
        )
        assert outcome.ok is False
        assert outcome.error


class TestPauseContinueRevision:
    """A pause-point ``continue`` must not move the step's status or retry
    counter — but the step IS going to run again (the failure gate's Retry),
    and it must not run against the description the operator just replaced."""

    def test_the_paused_step_picks_up_the_revised_description(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.FAILED
        step.inputs["task_description"] = "the old ask"
        step.inputs["retry_count"] = 1

        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, revised_description="the new ask"),
            tmp_path, continue_reenters_step=False,
        )

        assert outcome.ok and outcome.revised
        assert "the new ask" in step.inputs["task_description"]
        # The gate is still the gate: nothing about the step's own scheduling moved.
        assert step.status == StepStatus.FAILED
        assert step.inputs["retry_count"] == 1

    def test_the_revision_is_stated_in_the_dialog_note(self, tmp_path):
        """A native-resume continuation sends only the new user turn, so the
        replacement has to be said out loud, not merely swapped into a header.

        At a pause point the note is parked on the GATE (flow context), not in
        the step's inputs: the only consumer is the failure gate's Retry, and
        a fix-loop re-entry of the reused step object must never see it.
        """
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, revised_description="the new ask"),
            tmp_path, continue_reenters_step=False,
        )
        assert "dialog_note" not in step.inputs
        parked = flow.state.context.get("pending_gate_note")
        assert parked is not None
        assert parked["step_id"] == step.step_id
        assert "the new ask" in parked["note"]

    def test_a_pause_continue_leaves_no_note_on_the_step(self, tmp_path):
        """The finding this guards: a parked instruction must not leak into a
        later fix-loop re-entry of the same step object."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="try postgres"),
            tmp_path, continue_reenters_step=False,
        )
        assert "dialog_note" not in step.inputs
        assert "dialog_resume" not in step.inputs
        parked = flow.state.context["pending_gate_note"]
        assert "try postgres" in parked["note"]

    def test_gate_note_consumed_only_by_the_matching_step(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="try postgres"),
            tmp_path, continue_reenters_step=False,
        )
        # A different step asking consumes nothing — the note is dropped, not
        # delivered to a run it was never scoped to.
        other = Step(step_type=StepType.TEST, status=StepStatus.FAILED,
                     step_id="09_test_zz", inputs={})
        consume_gate_note(flow, other)
        assert "dialog_note" not in other.inputs
        assert "pending_gate_note" not in flow.state.context

    def test_gate_note_transfers_to_the_retried_step(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="try postgres"),
            tmp_path, continue_reenters_step=False,
        )
        consume_gate_note(flow, step)
        assert "try postgres" in step.inputs["dialog_note"]
        assert "pending_gate_note" not in flow.state.context

    def test_a_change_nothing_continue_keeps_the_parked_note(self, tmp_path):
        """The finding this guards: the empty-input path ("change nothing,
        continue immediately") confirms a bare decision at the SAME pause, and
        wiping the note there loses an instruction the operator confirmed —
        the Retry it was parked for would then run without it."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="use postgres"),
            tmp_path, continue_reenters_step=False,
        )
        apply_decision(
            flow, step, DialogDecision(action=ACTION_CONTINUE), tmp_path,
            continue_reenters_step=False,
        )

        consume_gate_note(flow, step)
        assert "use postgres" in step.inputs["dialog_note"]

    def test_a_later_round_at_the_same_pause_adds_to_the_note(self, tmp_path):
        """A second dialog at the same pause must not REPLACE the first
        conclusion — a summary-only round would otherwise drop the
        instruction."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="use postgres"),
            tmp_path, continue_reenters_step=False,
        )
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="and index it"),
            tmp_path, continue_reenters_step=False,
        )

        consume_gate_note(flow, step)
        note = step.inputs["dialog_note"]
        assert "use postgres" in note and "and index it" in note

    def test_re_confirming_the_same_instruction_does_not_duplicate_it(
        self, tmp_path
    ):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        for _ in range(3):
            apply_decision(
                flow, step,
                DialogDecision(action=ACTION_CONTINUE, instruction="use postgres"),
                tmp_path, continue_reenters_step=False,
            )
        consume_gate_note(flow, step)
        assert step.inputs["dialog_note"].count("use postgres") == 1

    def test_a_note_parked_for_another_step_is_replaced_not_merged(
        self, tmp_path
    ):
        """A note keyed to a step the flow has left behind belongs to a pause
        that is over; the new pause must not inherit its text."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        flow.state.context["pending_gate_note"] = {
            "step_id": "99_other_zz", "note": "stale advice",
        }
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="use postgres"),
            tmp_path, continue_reenters_step=False,
        )
        parked = flow.state.context["pending_gate_note"]
        assert parked["step_id"] == step.step_id
        assert "stale advice" not in parked["note"]

    def test_exit_at_a_pause_point_parks_no_instruction(self, tmp_path):
        """Leaving the flow RESOLVES the pause, so the one-shot dies with it.

        Otherwise the instruction survives the exit and the ``--resume``, and
        is handed to the next failure gate's Retry — a different pause, about
        a different run.
        """
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.FAILED

        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_EXIT, instruction="skip the slow suite"),
            tmp_path, continue_reenters_step=False,
        )

        assert outcome.action == ACTION_EXIT
        assert "pending_gate_note" not in flow.state.context
        assert "dialog_note" not in step.inputs

    def test_exit_at_a_pause_point_discards_an_earlier_parked_note(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="try postgres"),
            tmp_path, continue_reenters_step=False,
        )
        assert "pending_gate_note" in flow.state.context

        apply_decision(
            flow, step, DialogDecision(action=ACTION_EXIT), tmp_path,
            continue_reenters_step=False,
        )
        assert "pending_gate_note" not in flow.state.context

    def test_exit_at_a_pause_point_still_persists_a_revision(self, tmp_path):
        """The revision is flow-level: it outlives the pause it was made at."""
        from tianluo.engine.state_machine import _effective_task_description_base

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.status = StepStatus.FAILED
        step.inputs["task_description"] = "the old ask"

        outcome = apply_decision(
            flow, step,
            DialogDecision(action=ACTION_EXIT, revised_description="the new ask"),
            tmp_path, continue_reenters_step=False,
        )

        assert outcome.revised is True
        assert _effective_task_description_base(flow) == "the new ask"
        assert "the new ask" in step.inputs["task_description"]
        # ...but nothing one-shot is left behind for a later pause to pick up.
        assert "pending_gate_note" not in flow.state.context
        assert step.status == StepStatus.FAILED

    def test_discard_gate_note_silences_the_pause(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(
            flow, step,
            DialogDecision(action=ACTION_CONTINUE, instruction="try postgres"),
            tmp_path, continue_reenters_step=False,
        )
        discard_gate_note(flow)
        assert "pending_gate_note" not in flow.state.context


class TestTranscriptSummary:
    def test_summary_labels_both_speakers(self):
        text = summarize_transcript([
            {"role": "user", "content": "use Postgres"},
            {"role": "assistant", "content": "understood"},
        ])
        assert "User: use Postgres" in text
        assert "Agent: understood" in text

    def test_summary_is_bounded(self):
        turns = [{"role": "user", "content": "x" * 500} for _ in range(20)]
        text = summarize_transcript(turns, max_chars=200)
        assert len(text) < 300
        assert "earlier turns omitted" in text


class TestHistoryRendering:
    """``luo history show`` must surface a dialog turn as what it is.

    Reading it as an ordinary prompt/response pair would misrepresent when —
    and why — it happened; folding a typed user turn behind the collapse rule
    for generated prompts would hide the operator's own words.
    """

    def _session(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_prompt(
            tmp_path, flow.flow_id, step.step_id, "implement", "the step prompt", 0,
        )
        chat_history.record_dialog_message(
            tmp_path, flow.flow_id, step.step_id, "implement",
            "user", "why SQLite?",
        )
        chat_history.record_dialog_message(
            tmp_path, flow.flow_id, step.step_id, "implement",
            "assistant", "It was the smallest change.",
        )
        return chat_history.get_step_history(tmp_path, flow.flow_id, step.step_id)

    def test_plain_text_render_labels_both_dialog_speakers(self, tmp_path):
        text = chat_history.render_session_text(self._session(tmp_path))
        assert "[Interjection Dialog · you]" in text
        assert "[Interjection Dialog · agent]" in text
        assert "why SQLite?" in text
        assert "It was the smallest change." in text
        # The step's own prompt keeps its label.
        assert "[User Prompt]" in text

    def test_a_legacy_interjection_is_labelled_distinctly(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        chat_history.record_user_interjection(
            tmp_path, flow.flow_id, step.step_id, "implement", "legacy insert",
        )
        session = chat_history.get_step_history(
            tmp_path, flow.flow_id, step.step_id
        )
        text = chat_history.render_session_text(session)
        assert "[User Interjection]" in text
        assert "legacy insert" in text

    def test_detailed_render_does_not_raise_on_a_dialog_turn(self, tmp_path):
        renderables = chat_history.render_session_detailed(
            self._session(tmp_path)
        )
        assert renderables

    def test_a_dialog_record_round_trips_through_serialization(self, tmp_path):
        session = self._session(tmp_path)
        dialog_msgs = [m for m in session.messages if m.kind == "dialog"]
        assert [m.role for m in dialog_msgs] == ["user", "assistant"]
        assert dialog_msgs[0].content == "why SQLite?"


class TestDagGroupSummary:
    """A parallel implement step has N agents, not one — the dialog happens at
    the scheduling level and needs each group's state and session."""

    def _implement_flow(self):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.inputs["task_groups"] = [
            {"group_id": "G1"}, {"group_id": "G2"}, {"group_id": "G3"},
        ]
        step.outputs["implemented_groups"] = ["G1"]
        return flow, step

    def test_each_group_reports_its_state_and_session(self, tmp_path):
        from tianluo.engine.interjection_dialog import group_session_lines

        flow, step = self._implement_flow()
        worktree = tmp_path / "wt-g2"
        chat_history.record_prompt(
            tmp_path, flow.flow_id, f"{step.step_id}_G2", "implement", "p", 0,
            provider_session_id="sid-g2", session_cwd=str(worktree),
            agent_name="dclaude",
        )

        lines = group_session_lines(flow, step, tmp_path)

        assert any(line.startswith("- G1: completed") for line in lines)
        g2 = next(line for line in lines if line.startswith("- G2:"))
        assert "interrupted / not started" in g2
        assert "sid-g2" in g2
        # The session is bound to the group's OWN worktree, which is where a
        # resume of it must be issued.
        assert str(worktree) in g2
        assert "no recorded agent session" in next(
            line for line in lines if line.startswith("- G3:")
        )

    def test_the_summary_reaches_the_dialog_prompt(self, tmp_path):
        flow, step = self._implement_flow()
        ctx = build_dialog_context(flow, step, tmp_path)
        rendered = ctx.render()
        assert "Parallel implementation groups" in rendered
        assert "- G1: completed" in rendered

    def test_a_single_group_is_not_a_parallel_schedule(self, tmp_path):
        from tianluo.engine.interjection_dialog import group_session_lines

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        step.inputs["task_groups"] = [{"group_id": "G1"}]
        assert group_session_lines(flow, step, tmp_path) == []

    def test_a_non_implement_step_has_no_group_summary(self, tmp_path):
        from tianluo.engine.interjection_dialog import group_session_lines

        flow = _flow(StepType.TEST)
        step = flow.state.steps[flow.state.current_step_id]
        step.inputs["task_groups"] = [{"group_id": "G1"}, {"group_id": "G2"}]
        assert group_session_lines(flow, step, tmp_path) == []
