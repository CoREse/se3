"""Where an interjection goes: the two doors, and the pause-point semantics.

Decision 5 leaves an interjection exactly two routes — interrupt the running
call and open the dialog, or (at a pause point) open the dialog / become the
paused conversation's next reply. The retired third route, silently appending
to the next step's task description, must be gone.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo import stdin_channel
from tianluo.commands import run as run_mod
from tianluo.commands.run import (
    _DIALOG_AWAITING_WEB,
    _DIALOG_CONTINUE_STEP,
    _DIALOG_EXIT,
    _DIALOG_RESTARTED,
    _collect_pending_dialog_messages,
    _dialog_state,
    _dialog_subject_step,
    _drain_interjection_as_reply,
    _run_interjection_dialog_noninteractive,
)
from tianluo.engine import interaction_calls
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.persistence import PersistenceManager


@pytest.fixture(autouse=True)
def _clean_ambient():
    from tianluo.engine import llm_caller, rewind
    from tianluo.stop_signal import get_stop_signal

    get_stop_signal().clear()
    rewind.set_current_generation(0)
    llm_caller.clear_extra_prompt()
    llm_caller.consume_dialog_resume()
    yield
    get_stop_signal().clear()
    rewind.set_current_generation(0)
    llm_caller.clear_extra_prompt()
    llm_caller.consume_dialog_resume()


def _queue_interjection(root: Path, text: str) -> Path:
    return interaction_calls.write_interjection_request(
        interaction_calls.calls_dir_for(root), text, flow_id="f1"
    )


def _flow(*types, current_status=StepStatus.RUNNING):
    flow = FlowInstance(
        flow_id="f1",
        task_description="build the thing",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = list(types)
    for i, st in enumerate(types):
        flow.state.add_step(
            Step(
                step_id=f"{i + 1:02d}_{st.value}_x",
                step_type=st,
                status=StepStatus.COMPLETED,
                inputs={},
            )
        )
    flow.state.current_step_id = flow.state.step_history[-1]
    flow.state.steps[flow.state.current_step_id].status = current_status
    # Entry snapshots are written by run_step for every executed step; a rewind
    # refuses a target that has none, so a fixture flow needs them to stand in
    # for a flow that has actually run.
    from tianluo.engine.rewind import snapshot_step_entry

    for index, sid in enumerate(list(flow.state.step_history)):
        flow.state.current_step_index = index
        snapshot_step_entry(flow, sid)
    flow.state.current_step_index = max(0, len(flow.state.step_history) - 1)
    return flow


class TestNoAppendMechanism:
    def test_the_retired_drain_helpers_are_gone(self):
        """The append-to-task-description door is removed, not bypassed."""
        assert not hasattr(run_mod, "_drain_pending_interjections")
        assert not hasattr(run_mod, "_consume_paused_interjection_prefix")
        assert not hasattr(run_mod, "_handle_step_interrupt")

    def test_collecting_messages_does_not_touch_the_flow(self, tmp_path):
        _queue_interjection(tmp_path, "please switch to Postgres")
        texts = _collect_pending_dialog_messages(tmp_path)
        assert texts == ["please switch to Postgres"]

    def test_a_consumed_call_is_not_drained_twice(self, tmp_path):
        _queue_interjection(tmp_path, "once")
        assert _collect_pending_dialog_messages(tmp_path) == ["once"]
        assert _collect_pending_dialog_messages(tmp_path) == []


class TestStopSignalEntryPoint:
    def test_sigint_publishes_instead_of_raising_during_an_llm_call(self):
        """Raising would tear the runner's supervisor down before it can wind
        the child down gracefully — and would never reach a group worker."""
        from tianluo.stop_signal import get_stop_signal, llm_call_scope

        sig = get_stop_signal()
        with llm_call_scope(sig):
            run_mod._sigint_handler(2, None)  # must not raise
        assert sig.is_set()

    def test_sigint_still_raises_outside_an_llm_call(self):
        """A blocking lock wait / terminal read can only be broken this way."""
        from tianluo.stop_signal import get_stop_signal

        with pytest.raises(KeyboardInterrupt):
            run_mod._sigint_handler(2, None)
        assert get_stop_signal().is_set()

    def test_web_interjection_and_ctrl_c_share_one_signal(self, tmp_path):
        from tianluo.stop_signal import (
            STOP_REASON_INTERJECTION,
            InterjectionWatcher,
            get_stop_signal,
        )

        _queue_interjection(tmp_path, "hold on")
        sig = get_stop_signal()
        InterjectionWatcher(tmp_path, signal=sig).poll_once()
        assert sig.is_set()
        assert sig.pending.reason == STOP_REASON_INTERJECTION
        assert sig.pending.texts == ["hold on"]


class TestDiscoveryPauseRouting:
    def test_an_interjection_becomes_the_next_discovery_reply(self, tmp_path):
        """Discovery IS a conversation; the user is already at its prompt."""
        _queue_interjection(tmp_path, "also support MySQL")
        assert _drain_interjection_as_reply(tmp_path) == "also support MySQL"

    def test_several_queued_messages_are_joined_in_order(self, tmp_path):
        _queue_interjection(tmp_path, "first")
        _queue_interjection(tmp_path, "second")
        assert _drain_interjection_as_reply(tmp_path) == "first\nsecond"

    def test_nothing_queued_yields_none(self, tmp_path):
        assert _drain_interjection_as_reply(tmp_path) is None

    def test_no_project_root_yields_none(self):
        assert _drain_interjection_as_reply(None) is None

    def test_interactive_pause_returns_the_interjection_without_prompting(
        self, tmp_path,
    ):
        _queue_interjection(tmp_path, "use MySQL instead")
        flow = _flow(StepType.DISCOVERY, current_status=StepStatus.PAUSED)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)

        with patch("tianluo.commands.run._read_multiline_input") as read:
            reply = run_mod._handle_discovery_pause(
                flow, step, persistence, None, tmp_path
            )

        assert reply == "use MySQL instead"
        read.assert_not_called()

    def test_noninteractive_pause_consumes_the_interjection_as_the_reply(
        self, tmp_path,
    ):
        _queue_interjection(tmp_path, "add an index")
        flow = _flow(StepType.DISCOVERY, current_status=StepStatus.PAUSED)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)

        reply = run_mod._handle_discovery_pause_noninteractive(
            flow, step, persistence, tmp_path
        )
        assert reply == "add an index"

    def test_a_stale_discovery_call_is_cleaned_up(self, tmp_path):
        """The overtaken question must stop showing as a pending interaction."""
        flow = _flow(StepType.DISCOVERY, current_status=StepStatus.PAUSED)
        step = flow.state.steps[flow.state.current_step_id]
        call_file = run_mod._write_discovery_call(flow, step, tmp_path)
        step.outputs["discovery_call_file"] = str(call_file)
        _queue_interjection(tmp_path, "never mind, do Y")

        reply = run_mod._handle_discovery_pause_noninteractive(
            flow, step, MagicMock(spec=PersistenceManager), tmp_path
        )
        assert reply == "never mind, do Y"
        assert not call_file.exists()
        assert "discovery_call_file" not in step.outputs


class TestAStopAsDiscoveryPausesIsTheNextReply:
    """An interjection landing in the final tick of the discovery call.

    The watcher has already drained the call file into the stop request, so
    ``_drain_interjection_as_reply`` finds nothing: without this routing the
    text is handed to the small dialog and the operator has to retype it.
    """

    def _step(self, **outputs):
        flow = _flow(StepType.DISCOVERY, current_status=StepStatus.PAUSED)
        step = flow.state.steps[flow.state.current_step_id]
        step.outputs.update(outputs)
        return flow, step

    def test_the_stop_texts_become_the_discovery_reply(self):
        from tianluo.stop_signal import STOP_REASON_INTERJECTION, get_stop_signal

        _flow_obj, step = self._step()
        signal = get_stop_signal()
        signal.clear()
        signal.request(reason=STOP_REASON_INTERJECTION, text="use MySQL instead")
        try:
            reply = run_mod._stop_request_as_discovery_reply(
                step, StepStatus.PAUSED, False,
            )
            assert reply == "use MySQL instead"
            # Taken, so the small dialog will not also open on it.
            assert signal.is_set() is False
        finally:
            signal.clear()

    def test_a_genuinely_interrupted_round_still_opens_the_dialog(self):
        from tianluo.stop_signal import STOP_REASON_INTERJECTION, get_stop_signal

        _flow_obj, step = self._step()
        signal = get_stop_signal()
        signal.clear()
        signal.request(reason=STOP_REASON_INTERJECTION, text="stop that")
        try:
            assert run_mod._stop_request_as_discovery_reply(
                step, None, True,
            ) is None
            # The request is left published for the dialog to consume.
            assert signal.is_set() is True
        finally:
            signal.clear()

    def test_a_bare_ctrl_c_carries_no_reply(self):
        from tianluo.stop_signal import get_stop_signal

        _flow_obj, step = self._step()
        signal = get_stop_signal()
        signal.clear()
        signal.request()
        try:
            assert run_mod._stop_request_as_discovery_reply(
                step, StepStatus.PAUSED, False,
            ) is None
            assert signal.is_set() is True
        finally:
            signal.clear()

    def test_the_programmatic_confirm_gate_is_not_answered_by_a_message(self):
        from tianluo.stop_signal import STOP_REASON_INTERJECTION, get_stop_signal

        _flow_obj, step = self._step(awaiting_programmatic_confirm=True)
        signal = get_stop_signal()
        signal.clear()
        signal.request(reason=STOP_REASON_INTERJECTION, text="looks good?")
        try:
            assert run_mod._stop_request_as_discovery_reply(
                step, StepStatus.PAUSED, False,
            ) is None
        finally:
            signal.clear()

    def test_a_non_discovery_step_is_untouched(self):
        from tianluo.stop_signal import STOP_REASON_INTERJECTION, get_stop_signal

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        signal = get_stop_signal()
        signal.clear()
        signal.request(reason=STOP_REASON_INTERJECTION, text="hold on")
        try:
            assert run_mod._stop_request_as_discovery_reply(
                step, StepStatus.PAUSED, False,
            ) is None
        finally:
            signal.clear()


class TestAConsumedWebMessageIsNeverDropped:
    """The dual-wait pollers CONSUME as they read.

    When the terminal side completes inside the same poll tick, the message
    exists nowhere else — discarding it loses a decision the web operator
    already applied.
    """

    @pytest.fixture(autouse=True)
    def _clean_queue(self):
        run_mod._drain_deferred_web_messages()
        yield
        run_mod._drain_deferred_web_messages()

    def test_a_parked_message_wins_the_next_web_sweep(self, tmp_path):
        run_mod._defer_web_messages("apply that decision")
        assert run_mod._poll_web_answer(None, None) == "apply that decision"
        # Delivered once, not forever.
        assert run_mod._poll_web_answer(None, None) is None

    def test_parked_messages_reach_the_next_dialog_drain(self, tmp_path):
        run_mod._defer_web_messages(["first", "second"])
        assert run_mod._collect_pending_dialog_messages(tmp_path) == [
            "first", "second",
        ]
        assert run_mod._collect_pending_dialog_messages(tmp_path) == []

    def test_parked_messages_precede_what_is_still_on_disk(self, tmp_path):
        run_mod._defer_web_messages("consumed earlier")
        _queue_interjection(tmp_path, "still on disk")
        assert run_mod._collect_pending_dialog_messages(tmp_path) == [
            "consumed earlier", "still on disk",
        ]

    def test_empty_values_are_never_parked(self):
        run_mod._defer_web_messages(["", None])
        assert run_mod._drain_deferred_web_messages() == []


class TestTheDialogSentinelNeverEscapesItsDialog:
    """``_DIALOG_WEB_DECISION`` is a control string, not a user message.

    Its payload lives in the dialog's own state, so a sentinel parked in the
    process-wide deferred queue outlives the only thing that can read it — and
    the next wait then hands the raw NUL string out as if the operator had
    typed it (to the dialog LLM, or as a DISCOVERY reply).
    """

    @pytest.fixture(autouse=True)
    def _clean_queue(self):
        run_mod._drain_deferred_web_messages()
        yield
        run_mod._drain_deferred_web_messages()

    def test_the_sentinel_is_never_parked(self):
        run_mod._defer_web_messages(
            [run_mod._DIALOG_WEB_DECISION, "a real message"]
        )
        assert run_mod._drain_deferred_web_messages() == ["a real message"]

    def _race(self, tmp_path, first_message, terminal_answers, llm_replies=()):
        """Run a dialog whose FIRST wait loses the tick that ate a web reply."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)
        answers = list(terminal_answers)
        pushed = []

        def _fake_wait(call_file, **kwargs):
            if not pushed:
                # Exactly what the interactive dual-wait does when the operator
                # answers in the same poll tick a web reply lands: the poller
                # consumes the reply (turning it into the sentinel) and the
                # loser of the race is parked for the next wait.
                interaction_calls.write_response(call_file, {"response": "confirm"})
                pushed.append(kwargs["tick_callback"]())
                run_mod._defer_web_messages(pushed[0])
            return (run_mod._DISCOVERY_SRC_TERMINAL, answers.pop(0))

        with patch.object(run_mod, "_await_terminal_or_web", _fake_wait), patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.side_effect = list(llm_replies)
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
                initial_messages=[first_message],
                pause_context="failure",
            )
        assert pushed == [run_mod._DIALOG_WEB_DECISION]
        return outcome, make.return_value.call

    def test_a_terminal_confirmation_leaves_no_sentinel_behind(self, tmp_path):
        # "1" confirms the proposal parsed out of "continue", so the dialog
        # returns while the web payload is still parked — the leak's entry
        # point: nothing downstream may ever see the sentinel.
        outcome, caller = self._race(tmp_path, "continue", ["1"])

        # A pause-point continue goes back to waiting at the gate (decision 4).
        assert outcome == run_mod._DIALOG_RESUME_PAUSE
        assert run_mod._drain_deferred_web_messages() == []
        assert _collect_pending_dialog_messages(tmp_path) == []
        caller.assert_not_called()

    def test_a_stale_confirmation_is_not_applied_to_a_later_proposal(
        self, tmp_path
    ):
        # The terminal answer is prose, so the dialog runs on and the restart
        # the web operator confirmed is replaced by the agent's question. The
        # re-offered confirmation must not execute that vanished restart.
        outcome, caller = self._race(
            tmp_path,
            "restart",
            ["why did that fail?", ""],
            [json.dumps({"mode": "question", "content": "because X"})],
        )

        assert outcome == run_mod._DIALOG_RESUME_PAUSE  # not _DIALOG_RESTARTED
        # One round: the sentinel was never handed to the dialog LLM as prose.
        assert caller.call_count == 1
        assert run_mod._drain_deferred_web_messages() == []

    def test_a_confirmation_is_stale_once_the_terminal_edits_the_fields(
        self, tmp_path
    ):
        """The web confirmed ``restart``+``keep``; the terminal then edited the
        SAME object into ``restart``+``reset``. Binding the confirmation to the
        object rather than to the fields it was shown would discard the
        workspace on an approval nobody gave for it."""
        applied = []
        real = run_mod._confirm_and_apply_decision

        def _spy(flow, step, decision, *args, **kwargs):
            applied.append(decision.to_dict())
            return real(flow, step, decision, *args, **kwargs)

        with patch.object(run_mod, "_confirm_and_apply_decision", _spy):
            outcome, caller = self._race(
                tmp_path, "restart", ["workspace: reset", ""],
            )

        # The stale confirmation was re-offered, not executed: the empty line
        # that follows is what resolves the dialog, as a plain continue — the
        # edited restart+reset never reached the apply path at all.
        assert [(d["action"], d["workspace"]) for d in applied] == [
            ("continue", "keep")
        ]
        assert outcome == run_mod._DIALOG_RESUME_PAUSE  # not _DIALOG_RESTARTED
        caller.assert_not_called()
        assert run_mod._drain_deferred_web_messages() == []

    def test_a_confirmation_left_on_disk_is_bound_to_the_round_it_saw(
        self, tmp_path
    ):
        """The web confirmed ``restart``+``keep`` in the instant the terminal's
        poller stopped, so the reply sat unread on the call file; the terminal
        then edited the round into ``restart``+``reset``. The next round must
        bind that leftover confirmation to what the console PUBLISHED when it
        was written — republishing first and snapshotting the live proposal
        afterwards executed a workspace reset nobody approved.
        """
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)
        answers = ["workspace: reset", ""]
        applied = []
        real = run_mod._confirm_and_apply_decision

        def _spy(flow_, step_, decision, *args, **kwargs):
            applied.append(decision.to_dict())
            return real(flow_, step_, decision, *args, **kwargs)

        def _fake_wait(call_file, **kwargs):
            # The web answers the published round but the poller has already
            # gone: the response is only ever read by a LATER round's tick.
            if len(answers) == 2:
                interaction_calls.write_response(call_file, {"response": "confirm"})
            return (run_mod._DISCOVERY_SRC_TERMINAL, answers.pop(0))

        with patch.object(run_mod, "_await_terminal_or_web", _fake_wait), \
                patch.object(run_mod, "_confirm_and_apply_decision", _spy), patch(
                    "tianluo.engine.interjection_dialog."
                    "InterjectionDialog._make_caller"
                ) as make:
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
                initial_messages=["restart"],
                pause_context="failure",
            )

        assert [(d["action"], d["workspace"]) for d in applied] == [
            ("continue", "keep")
        ]
        assert outcome == run_mod._DIALOG_RESUME_PAUSE  # not _DIALOG_RESTARTED
        make.return_value.call.assert_not_called()
        assert run_mod._drain_deferred_web_messages() == []


class TestPausePointSubject:
    def test_a_confirm_gate_talks_to_the_reviewed_steps_session(self):
        """The operator is asking about the artefact, not about the gate."""
        flow = _flow(StepType.PLAN, StepType.CONFIRM)
        confirm = flow.state.steps["02_confirm_x"]
        confirm.inputs["step_to_review_id"] = "01_plan_x"
        assert _dialog_subject_step(flow, confirm).step_id == "01_plan_x"

    def test_other_steps_talk_to_themselves(self):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps["01_implement_x"]
        assert _dialog_subject_step(flow, step) is step

    def test_a_confirm_gate_with_a_missing_reviewee_falls_back(self):
        flow = _flow(StepType.CONFIRM)
        confirm = flow.state.steps["01_confirm_x"]
        confirm.inputs["step_to_review_id"] = "gone"
        assert _dialog_subject_step(flow, confirm) is confirm


class TestPausePointSemantics:
    """``continue`` at a gate means "keep waiting HERE", not "rerun the step"."""

    def test_confirm_continue_leaves_the_producer_untouched(self, tmp_path):
        flow = _flow(StepType.PLAN, StepType.CONFIRM, current_status=StepStatus.PAUSED)
        confirm = flow.state.steps["02_confirm_x"]
        confirm.inputs["step_to_review_id"] = "01_plan_x"
        producer = flow.state.steps["01_plan_x"]
        producer.inputs["retry_count"] = 1
        persistence = MagicMock(spec=PersistenceManager)

        with patch("tianluo.commands.run._read_multiline_input", return_value=""):
            outcome = run_mod._dialog_at_pause_point(
                flow, confirm, persistence, tmp_path,
                pause_context="confirm", output_format="cli",
            )

        assert outcome == run_mod._DIALOG_RESUME_PAUSE
        # The reviewed producer keeps its terminal status and its counter.
        assert producer.status == StepStatus.COMPLETED
        assert producer.inputs["retry_count"] == 1
        assert "resumed" not in producer.inputs

    def test_failure_continue_does_not_rearm_or_recount_the_failed_step(
        self, tmp_path,
    ):
        flow = _flow(StepType.IMPLEMENT, current_status=StepStatus.FAILED)
        step = flow.state.steps["01_implement_x"]
        step.inputs["retry_count"] = 1
        persistence = MagicMock(spec=PersistenceManager)

        with patch("tianluo.commands.run._read_multiline_input", return_value=""):
            outcome = run_mod._dialog_at_pause_point(
                flow, step, persistence, tmp_path,
                pause_context="failure", output_format="cli",
            )

        assert outcome == run_mod._DIALOG_RESUME_PAUSE
        # Still FAILED, so the loop presents Retry/Skip/Abort again instead of
        # rerunning the step straight away — and the counter did not move.
        assert step.status == StepStatus.FAILED
        assert step.inputs["retry_count"] == 1

    def test_a_mid_step_continue_still_rearms_the_step(self, tmp_path):
        """Outside a gate, ``continue`` IS the retry (decision 4)."""
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps["01_implement_x"]
        persistence = MagicMock(spec=PersistenceManager)

        with patch("tianluo.commands.run._read_multiline_input", return_value=""):
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
            )

        assert outcome == run_mod._DIALOG_CONTINUE_STEP
        assert step.status == StepStatus.PENDING
        assert step.inputs["retry_count"] == 1


class TestPausePointCallAttribution:
    def test_a_confirm_dialog_call_is_filed_against_the_gate(self, tmp_path):
        """The daemon drops any pending call whose step is no longer the flow's
        current one, so a call filed against the completed producer would be
        invisible to the web console."""
        from tianluo.engine import interaction_calls

        flow = _flow(StepType.PLAN, StepType.CONFIRM, current_status=StepStatus.PAUSED)
        confirm = flow.state.steps["02_confirm_x"]
        confirm.inputs["step_to_review_id"] = "01_plan_x"
        persistence = MagicMock(spec=PersistenceManager)

        turn_json = json.dumps({"mode": "question", "content": "Because X."})
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = turn_json
            run_mod._dialog_at_pause_point(
                flow, confirm, persistence, tmp_path,
                initial_messages=["why this plan?"],
                pause_context="confirm",
                output_format="json",
            )

        state = _dialog_state(flow)
        call = interaction_calls.read_call(Path(state["call_file"]))
        assert call["context"]["step_id"] == "02_confirm_x"
        assert call["context"]["step_id"] == flow.state.current_step_id
        # ...while the conversation still names the producer it talks to.
        assert call["context"]["subject_step_id"] == "01_plan_x"

    def test_the_gate_call_survives_the_daemons_stale_filter(self, tmp_path):
        from tianluo.daemon.aggregator import DaemonAggregator

        flow = _flow(StepType.PLAN, StepType.CONFIRM, current_status=StepStatus.PAUSED)
        confirm = flow.state.steps["02_confirm_x"]
        confirm.inputs["step_to_review_id"] = "01_plan_x"
        persistence = MagicMock(spec=PersistenceManager)

        turn_json = json.dumps({"mode": "question", "content": "Because X."})
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = turn_json
            run_mod._dialog_at_pause_point(
                flow, confirm, persistence, tmp_path,
                initial_messages=["why this plan?"],
                pause_context="confirm",
                output_format="json",
            )

        state = _dialog_state(flow)
        call_data = json.loads(
            Path(state["call_file"]).read_text(encoding="utf-8")
        )
        pending = types.SimpleNamespace(
            kind=call_data["kind"],
            context=call_data["context"],
            step_id=call_data["context"]["step_id"],
        )
        flow_state = {
            "current_step_id": "02_confirm_x",
            "steps": {
                "01_plan_x": {"status": "completed"},
                "02_confirm_x": {"status": "paused"},
            },
        }
        kept = DaemonAggregator._filter_stale_calls([pending], flow_state)
        assert kept == [pending]

        # Control: the old shape (filed against the completed producer) is
        # exactly what the filter drops.
        producer_call = types.SimpleNamespace(
            kind=call_data["kind"],
            context={"step_id": "01_plan_x"},
            step_id="01_plan_x",
        )
        assert DaemonAggregator._filter_stale_calls(
            [producer_call], flow_state
        ) == []


class TestNonLlmStepInterruption:
    """A step doing its work in Python never polls the cooperative flag, so a
    web interjection there must interrupt it the way Ctrl-C does — and a step
    that finished anyway must not be re-run for it."""

    def _step(self):
        flow = _flow(StepType.TEST)
        return flow, flow.state.steps[flow.state.current_step_id]

    def test_the_step_watcher_asks_for_ctrl_c_semantics(self, tmp_path):
        """The flag alone is polled by the LLM runners and nothing else, so a
        step doing its work in Python (TEST above all) would never see it. The
        watcher installed around every step must therefore be allowed to raise
        the same KeyboardInterrupt the SIGINT handler raises."""
        seen = {}

        class _Watcher:
            def __init__(self, root, **kwargs):
                seen.update(kwargs)

            def start(self):
                return self

            def stop(self):
                pass

        flow, step = self._step()
        with patch.object(run_mod, "InterjectionWatcher", _Watcher):
            state_machine = MagicMock()
            state_machine.run_step.return_value = StepStatus.COMPLETED
            run_mod._execute_step_with_interjections(
                state_machine, flow, step, tmp_path, lambda _s: None,
            )
        assert seen["escalate_to_main"] is True

    def test_an_interrupted_step_is_reported_as_cut_short(self, tmp_path, monkeypatch):
        """Whether the KeyboardInterrupt came from Ctrl-C or from the watcher's
        escalation, the step was stopped and its dialog re-runs it."""

        class _NoWatcher:
            def __init__(self, *a, **k):
                pass

            def start(self):
                return self

            def stop(self):
                pass

        monkeypatch.setattr(run_mod, "InterjectionWatcher", _NoWatcher)
        flow, step = self._step()
        state_machine = MagicMock()
        state_machine.run_step.side_effect = KeyboardInterrupt

        assert run_mod._execute_step_with_interjections(
            state_machine, flow, step, tmp_path, lambda _s: None,
        ) == (None, True, True)

    def test_a_step_that_finished_anyway_is_not_re_run(self, tmp_path, monkeypatch):
        """The request landed as the step was already finishing: the dialog
        still opens, but ``continue`` means carry on, not rerun."""
        from tianluo.stop_signal import STOP_REASON_INTERJECTION

        class _NoWatcher:
            def __init__(self, *a, **k):
                pass

            def start(self):
                return self

            def stop(self):
                pass

        monkeypatch.setattr(run_mod, "InterjectionWatcher", _NoWatcher)
        flow, step = self._step()

        def _run_step(_flow, _step, on_running=None):
            run_mod.get_stop_signal().request(
                reason=STOP_REASON_INTERJECTION, text="one more thing"
            )
            return StepStatus.COMPLETED

        state_machine = MagicMock()
        state_machine.run_step.side_effect = _run_step

        result, stopped, interrupted = run_mod._execute_step_with_interjections(
            state_machine, flow, step, tmp_path, lambda _s: None,
        )
        assert result == StepStatus.COMPLETED
        assert stopped is True
        assert interrupted is False
        run_mod.get_stop_signal().clear()

    def test_an_unfinished_step_counts_as_interrupted(self):
        """A cooperatively stopped LLM step surfaces as FAILED with partial
        output — that IS the evidence the work was cut short."""
        assert run_mod._is_incomplete_result(StepStatus.FAILED) is True
        assert run_mod._is_incomplete_result(None) is True
        assert run_mod._is_incomplete_result(StepStatus.COMPLETED) is False
        assert run_mod._is_incomplete_result(StepStatus.PARTIAL) is False

    def test_a_finished_step_opens_the_dialog_at_pause_semantics(
        self, tmp_path, monkeypatch
    ):
        flow, step = self._step()
        seen = {}

        def _fake(*args, **kwargs):
            seen.update(kwargs)
            return _DIALOG_AWAITING_WEB

        monkeypatch.setattr(
            run_mod, "_run_interjection_dialog_noninteractive", _fake
        )
        run_mod._open_dialog_after_stop(
            flow, step, MagicMock(spec=PersistenceManager), tmp_path, None,
            "json", interrupted=False,
        )
        assert seen["pause_context"] == "completed_step"

        seen.clear()
        run_mod._open_dialog_after_stop(
            flow, step, MagicMock(spec=PersistenceManager), tmp_path, None,
            "json", interrupted=True,
        )
        assert seen["pause_context"] is None

    def test_the_interrupted_state_is_persisted_before_the_dialog_blocks(
        self, tmp_path, monkeypatch
    ):
        """A DAG interrupt records its preserved worktrees / implemented groups
        in MEMORY only, and the interactive dialog can then sit at a prompt for
        hours. A process that dies there would resume against the pre-handler
        snapshot: the interrupted groups would be probed as unaccounted,
        misread as completed from their fork-heir commits, and their preserved
        worktrees force-cleaned with the uncommitted work still in them."""
        flow, step = self._step()
        persistence = MagicMock(spec=PersistenceManager)
        order = []

        def _fake_dialog(*args, **kwargs):
            order.append("dialog")
            return _DIALOG_AWAITING_WEB

        persistence.save_flow.side_effect = lambda *_a, **_k: order.append("save")
        monkeypatch.setattr(run_mod, "_run_interjection_dialog", _fake_dialog)
        monkeypatch.setattr(
            run_mod, "_run_interjection_dialog_noninteractive", _fake_dialog
        )

        run_mod._open_dialog_after_stop(
            flow, step, persistence, tmp_path, None, "cli", interrupted=True,
        )
        assert order[:2] == ["save", "dialog"]

        order.clear()
        run_mod._open_dialog_after_stop(
            flow, step, persistence, tmp_path, None, "json", interrupted=True,
        )
        assert order[:2] == ["save", "dialog"]

    def test_a_failed_persist_never_blocks_the_dialog(
        self, tmp_path, monkeypatch
    ):
        flow, step = self._step()
        persistence = MagicMock(spec=PersistenceManager)
        persistence.save_flow.side_effect = OSError("disk full")
        monkeypatch.setattr(
            run_mod, "_run_interjection_dialog_noninteractive",
            lambda *a, **k: _DIALOG_AWAITING_WEB,
        )
        outcome = run_mod._open_dialog_after_stop(
            flow, step, persistence, tmp_path, None, "json", interrupted=True,
        )
        assert outcome == _DIALOG_AWAITING_WEB

    def _confirm_gate_stop(self, tmp_path, monkeypatch, *, interrupted):
        flow = _flow(StepType.SELF_CHECK, StepType.CONFIRM)
        producer = flow.state.steps["01_self_check_x"]
        gate = flow.state.steps["02_confirm_x"]
        gate.inputs = {"step_to_review_id": producer.step_id}

        seen = {}

        def _fake(_flow, subject, *args, **kwargs):
            seen["subject"] = subject
            seen.update(kwargs)
            return _DIALOG_AWAITING_WEB

        monkeypatch.setattr(
            run_mod, "_run_interjection_dialog_noninteractive", _fake
        )
        run_mod._open_dialog_after_stop(
            flow, gate, MagicMock(spec=PersistenceManager), tmp_path, None,
            "json", interrupted=interrupted,
        )
        return producer, gate, seen

    def test_a_stop_on_a_confirm_gate_targets_the_producer(
        self, tmp_path, monkeypatch
    ):
        """The watcher polls the moment it starts, so an interjection that
        landed between two steps is drained as the NEXT step is entered — and
        that step can be the CONFIRM gate, which has no session of its own and
        is not what the operator is asking about."""
        producer, gate, seen = self._confirm_gate_stop(
            tmp_path, monkeypatch, interrupted=False,
        )

        assert seen["subject"] is producer
        # The call file stays filed against the flow's CURRENT step so the
        # daemon's stale-call filter keeps the conversation visible.
        assert seen["call_step"] is gate
        # ``continue`` at a gate means "go back to waiting here", never
        # "re-run the reviewed producer".
        assert seen["pause_context"] == "confirm"
        assert seen.get("apply_step") is None

    def test_a_confirm_cut_off_mid_call_re_runs_the_gate_not_the_producer(
        self, tmp_path, monkeypatch
    ):
        """A CONFIRM interrupted INSIDE run_step never reached its wait.

        There is no published gate to go back to, so ``continue`` is an ordinary
        retry of the CONFIRM step — while the conversation still belongs to the
        producer's session, which is the only one that exists.
        """
        producer, gate, seen = self._confirm_gate_stop(
            tmp_path, monkeypatch, interrupted=True,
        )

        assert seen["subject"] is producer
        assert seen["call_step"] is gate
        # NOT pause-point semantics: the gate never paused.
        assert seen["pause_context"] is None
        # ...and the decision lands on the gate, not on the reviewed producer.
        assert seen["apply_step"] is gate


class TestNonInteractiveDialogRounds:
    """The json/daemon shape: one call file per round, PAUSED between them."""

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)
        return flow, step, persistence

    def test_first_round_writes_a_dialog_call_and_pauses(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        turn_json = json.dumps({"mode": "question", "content": "Which database?"})

        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = turn_json
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path,
                initial_messages=["why SQLite?"],
            )

        assert outcome == _DIALOG_AWAITING_WEB
        assert flow.status == FlowStatus.PAUSED
        state = _dialog_state(flow)
        call_file = Path(state["call_file"])
        assert call_file.exists()
        payload = interaction_calls.read_call(call_file)
        assert payload["kind"] == interaction_calls.CALL_KIND_DIALOG
        assert payload["context"]["flow_id"] == "f1"
        assert payload["context"]["step_id"] == step.step_id
        assert payload["context"]["decision"] is None
        assert [t["content"] for t in payload["context"]["transcript"]] == [
            "why SQLite?", "Which database?",
        ]
        assert "Which database?" in payload["prompt"]

    def test_an_unanswered_call_keeps_the_flow_paused(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        turn_json = json.dumps({"mode": "question", "content": "q?"})
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = turn_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["hi"],
            )
            first_call = _dialog_state(flow)["call_file"]
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path
            )
        assert outcome == _DIALOG_AWAITING_WEB
        assert _dialog_state(flow)["call_file"] == first_call

    def test_a_text_reply_starts_the_next_round(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        replies = [
            json.dumps({"mode": "question", "content": "q1"}),
            json.dumps({"mode": "question", "content": "q2"}),
        ]
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.side_effect = replies
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["hi"],
            )
            call_file = Path(_dialog_state(flow)["call_file"])
            interaction_calls.write_response(call_file, {"response": "because X"})
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path
            )

        transcript = _dialog_state(flow)["transcript"]
        assert [t["content"] for t in transcript] == ["hi", "q1", "because X", "q2"]

    def test_a_proposed_decision_is_offered_with_a_confirm_option(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        decision_json = json.dumps({
            "mode": "decision",
            "content": "Restarting.",
            "decision": {"action": "restart", "restart_step_id": ""},
        })
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = decision_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["start over"],
            )

        payload = interaction_calls.read_call(
            Path(_dialog_state(flow)["call_file"])
        )
        assert payload["context"]["awaiting"] == "decision"
        assert payload["context"]["decision"]["action"] == "restart"
        assert payload["options"] == [{"label": "confirm", "value": "confirm"}]
        assert any(
            tgt["step_id"] == step.step_id
            for tgt in payload["context"]["rewind_targets"]
        )

    def test_confirming_a_decision_applies_it_and_clears_the_state(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        decision_json = json.dumps({
            "mode": "decision", "content": "ok",
            "decision": {"action": "continue"},
        })
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = decision_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["carry on"],
            )
            call_file = Path(_dialog_state(flow)["call_file"])
            interaction_calls.write_response(call_file, {"response": "confirm"})
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path
            )

        assert outcome == _DIALOG_CONTINUE_STEP
        assert _dialog_state(flow) is None
        assert step.status == StepStatus.PENDING
        assert step.inputs["retry_count"] == 1

    def test_the_web_may_edit_any_field_before_confirming(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        decision_json = json.dumps({
            "mode": "decision", "content": "ok",
            "decision": {"action": "continue"},
        })
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = decision_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["hmm"],
            )
            call_file = Path(_dialog_state(flow)["call_file"])
            interaction_calls.write_response(call_file, {
                "decision": {
                    "action": "continue",
                    "revised_description": "the corrected task",
                }
            })
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path
            )

        from tianluo.engine.state_machine import _effective_task_description_base

        assert _effective_task_description_base(flow) == "the corrected task"

    def test_an_interjection_arriving_mid_round_becomes_the_next_message(
        self, tmp_path
    ):
        """The daemon will not spawn a second resume while this process lives,
        so a message posted while the agent was answering has no other way in:
        the round must re-drain before it publishes and pauses."""
        flow, step, persistence = self._setup(tmp_path)
        replies = [
            json.dumps({"mode": "question", "content": "q1"}),
            json.dumps({"mode": "question", "content": "q2"}),
        ]

        def _answer(*_a, **_k):
            # Posted while the agent is answering the first message.
            if not _answer.queued:
                _answer.queued = True
                _queue_interjection(tmp_path, "and also the tests")
            return replies.pop(0)

        _answer.queued = False

        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.side_effect = _answer
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["why?"],
            )

        assert outcome == _DIALOG_AWAITING_WEB
        transcript = [t["content"] for t in _dialog_state(flow)["transcript"]]
        assert transcript == ["why?", "q1", "and also the tests", "q2"]

    def test_a_message_behind_a_proposed_decision_is_not_dropped(self, tmp_path):
        """A proposal is not a confirmation: everything the operator said after
        it is still theirs to have answered, and its call file is already
        gone."""
        flow, step, persistence = self._setup(tmp_path)
        replies = [
            json.dumps({
                "mode": "decision", "content": "Restarting.",
                "decision": {"action": "restart", "restart_step_id": ""},
            }),
            json.dumps({"mode": "question", "content": "understood, not yet"}),
        ]
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.side_effect = replies
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path,
                initial_messages=["start over", "wait, one more thing"],
            )

        transcript = [t["content"] for t in _dialog_state(flow)["transcript"]]
        assert transcript == [
            "start over", "Restarting.",
            "wait, one more thing", "understood, not yet",
        ]
        # The later message superseded the proposal — nothing is offered for
        # confirmation that the operator has already talked past.
        assert _dialog_state(flow)["decision"] is None

    def test_a_preview_request_republishes_instead_of_executing(self, tmp_path):
        """The web fields are editable, so an operator can turn a proposed
        ``continue`` into ``restart``+``reset``. That must fetch a preview of
        what it would discard, not discard it."""
        flow, step, persistence = self._setup(tmp_path)
        decision_json = json.dumps({
            "mode": "decision", "content": "ok",
            "decision": {"action": "continue"},
        })
        applied = []
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = decision_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["hmm"],
            )
            call_file = Path(_dialog_state(flow)["call_file"])
            interaction_calls.write_response(call_file, {
                "decision": {"action": "restart", "workspace": "reset"},
                "preview_request": True,
            })
            with patch(
                "tianluo.engine.interjection_dialog.apply_decision",
                side_effect=lambda *a, **k: applied.append(a),
            ):
                outcome = _run_interjection_dialog_noninteractive(
                    flow, step, persistence, tmp_path
                )

        # Not executed: the round is republished for confirmation, with the
        # edited decision now carried on the call file.
        assert outcome == _DIALOG_AWAITING_WEB
        assert applied == []
        state = _dialog_state(flow)
        assert state["decision"]["action"] == "restart"
        assert state["decision"]["workspace"] == "reset"
        payload = json.loads(Path(state["call_file"]).read_text(encoding="utf-8"))
        assert payload["context"]["reset_preview"] is not None

    def test_an_empty_reply_resumes_without_another_llm_round(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=[""],
            )
            make.assert_not_called()
        assert outcome == _DIALOG_CONTINUE_STEP
        assert step.status == StepStatus.PENDING

    def test_a_direct_decision_skips_the_llm(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["exit"],
            )
            make.assert_not_called()
        payload = interaction_calls.read_call(
            Path(_dialog_state(flow)["call_file"])
        )
        assert payload["context"]["decision"]["action"] == "exit"


class TestRewindRebuildRequest:
    """A confirmed ``restart`` leaves the rebuild for the run loop to perform.

    The request travels in flow state rather than being executed inside the
    dialog so it survives the process boundary of the json/daemon path, where
    the dialog round and the rebuild happen in different processes.
    """

    def test_a_restart_records_the_step_type_to_rebuild(self, tmp_path):
        from tianluo.engine.interjection_dialog import DialogDecision, apply_decision

        flow = _flow(StepType.PLAN, StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        outcome = apply_decision(
            flow, step, DialogDecision(action="restart"), tmp_path,
        )
        assert outcome.ok
        assert flow.state.context["pending_rewind_step_type"] == "implement"

    def test_continue_records_no_rebuild_request(self, tmp_path):
        from tianluo.engine.interjection_dialog import DialogDecision, apply_decision

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        apply_decision(flow, step, DialogDecision(action="continue"), tmp_path)
        assert "pending_rewind_step_type" not in flow.state.context


class TestPausePointOutputFormatRouting:
    """A daemon-spawned run has no terminal, so a pause-point dialog there has
    to travel through the call-file channel rather than a prompt."""

    def test_json_mode_routes_to_the_call_file_driver(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)

        turn_json = json.dumps({"mode": "question", "content": "Because X."})
        with patch("tianluo.commands.run._read_multiline_input") as read, patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = turn_json
            outcome = run_mod._dialog_at_pause_point(
                flow, step, persistence, tmp_path,
                initial_messages=["why did that fail?"],
                pause_context="failure",
                output_format="json",
            )

        read.assert_not_called()
        assert outcome == _DIALOG_AWAITING_WEB
        state = _dialog_state(flow)
        assert state["pause_context"] == "failure"
        assert Path(state["call_file"]).exists()

    def test_cli_mode_prompts_at_the_terminal(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)

        with patch(
            "tianluo.commands.run._read_multiline_input", return_value=""
        ) as read:
            outcome = run_mod._dialog_at_pause_point(
                flow, step, persistence, tmp_path,
                pause_context="confirm",
                output_format="cli",
            )
        read.assert_called()
        assert outcome == run_mod._DIALOG_RESUME_PAUSE


class TestConfirmIsOnlyAConfirmationWhenSomethingIsProposed:
    """The web's one-click confirm and the word an operator types are the same
    string. With nothing on the table it can only be the latter — treating it
    as the former resumed the flow behind their back."""

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        return flow, step, MagicMock(spec=PersistenceManager)

    def test_the_reply_carries_its_own_text_for_the_no_proposal_case(self, tmp_path):
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement", prompt="p",
        )
        interaction_calls.write_response(call_file, {"response": "confirm"})
        reply = interaction_calls.read_dialog_response(call_file)
        assert reply["confirm"] is True
        assert reply["text"] == "confirm"

    def test_json_mode_treats_it_as_the_next_message(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        replies = [
            json.dumps({"mode": "question", "content": "q1"}),
            json.dumps({"mode": "question", "content": "q2"}),
        ]
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.side_effect = replies
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["hi"],
            )
            call_file = Path(_dialog_state(flow)["call_file"])
            interaction_calls.write_response(call_file, {"response": "confirm"})
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path
            )

        # Another round, not a silent resume.
        assert outcome == _DIALOG_AWAITING_WEB
        assert [t["content"] for t in _dialog_state(flow)["transcript"]] == [
            "hi", "q1", "confirm", "q2",
        ]

    def test_an_applied_web_decision_survives_a_racing_interjection(self, tmp_path):
        """Apply is the operator's decision; a second operator's message
        arriving in the same wake-up must not silently discard it."""
        flow, step, persistence = self._setup(tmp_path)
        decision_json = json.dumps({
            "mode": "decision", "content": "ok",
            "decision": {"action": "continue"},
        })
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = decision_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["carry on"],
            )
            call_file = Path(_dialog_state(flow)["call_file"])
            interaction_calls.write_response(
                call_file,
                {"decision": {"action": "continue", "instruction": "use Postgres"}},
            )
            _queue_interjection(tmp_path, "one more thing")
            outcome = _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path
            )

        assert outcome == _DIALOG_CONTINUE_STEP
        assert _dialog_state(flow) is None
        # The raced message is recorded in the step's history rather than lost.
        from tianluo.engine import chat_history

        session = chat_history.get_step_history(tmp_path, "f1", step.step_id)
        assert any(
            "one more thing" in (m.content or "") for m in session.messages
        )


class TestEmptyInputAlwaysResumesUnchanged:
    """An empty line means "change nothing, continue now" at EVERY point, in
    both front ends — never a confirmation of whatever is on screen."""

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        return flow, step, MagicMock(spec=PersistenceManager)

    def test_an_empty_line_does_not_apply_a_pending_proposal(
        self, tmp_path, monkeypatch
    ):
        """The proposal was restart + workspace reset; pressing Enter to resume
        must not discard the workspace."""
        flow, step, persistence = self._setup(tmp_path)
        applied = []
        monkeypatch.setattr(
            "tianluo.engine.interjection_dialog.InterjectionDialog.ask",
            lambda self_, text: types.SimpleNamespace(
                content="Let us start over.",
                is_decision=True,
                decision=__import__(
                    "tianluo.engine.interjection_dialog", fromlist=["x"]
                ).DialogDecision(action="restart", workspace="reset"),
            ),
        )
        monkeypatch.setattr(
            run_mod, "_confirm_and_apply_decision",
            lambda flow_, step_, decision, *a, **k: (
                applied.append(decision) or run_mod._DIALOG_CONTINUE_STEP, ""
            ),
        )
        with patch(
            "tianluo.commands.run._read_multiline_input",
            side_effect=["start over?", ""],
        ):
            run_mod._run_interjection_dialog(flow, step, persistence, tmp_path)

        assert len(applied) == 1
        assert applied[0].action == "continue"
        assert applied[0].workspace != "reset"

    def test_an_empty_line_after_a_question_resumes_instead_of_reprompting(
        self, tmp_path, monkeypatch
    ):
        flow, step, persistence = self._setup(tmp_path)
        monkeypatch.setattr(
            "tianluo.engine.interjection_dialog.InterjectionDialog.ask",
            lambda self_, text: types.SimpleNamespace(
                content="I am editing app.py.", is_decision=False, decision=None,
            ),
        )
        with patch(
            "tianluo.commands.run._read_multiline_input",
            side_effect=["what are you doing?", ""],
        ) as read:
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
            )
        assert outcome == _DIALOG_CONTINUE_STEP
        assert read.call_count == 2


class TestInteractiveDialogIsAnswerableFromTheWeb:
    """A CLI-started dialog is mirrored to a call file and races both channels.

    Same reason the discovery pause is mirrored: an interaction the terminal is
    blocking on should be visible — and answerable — from the web console.
    """

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        return flow, step, MagicMock(spec=PersistenceManager)

    def test_the_round_is_mirrored_to_a_dialog_call_file(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        seen = {}

        def _capture(*_a, **_k):
            # Read it WHILE the round is live — the file is cleaned up when the
            # dialog ends, which is the behaviour the next test pins.
            calls = list(
                interaction_calls.calls_dir_for(tmp_path).glob("dialog_*.json")
            )
            seen["payloads"] = [interaction_calls.read_call(c) for c in calls]
            return ""  # empty → resume unchanged, ends the dialog

        with patch(
            "tianluo.commands.run._read_multiline_input", side_effect=_capture
        ):
            run_mod._run_interjection_dialog(flow, step, persistence, tmp_path)

        payloads = seen.get("payloads") or []
        assert payloads, "the round was not mirrored to a call file"
        payload = payloads[0]
        assert payload["kind"] == interaction_calls.CALL_KIND_DIALOG
        assert payload["context"]["step_id"] == step.step_id
        assert payload["context"]["flow_id"] == flow.flow_id

    def test_the_call_file_is_cleaned_up_when_the_dialog_ends(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        with patch("tianluo.commands.run._read_multiline_input", return_value=""):
            run_mod._run_interjection_dialog(flow, step, persistence, tmp_path)
        # No lingering "awaiting reply" chip on the web console.
        assert not list(
            interaction_calls.calls_dir_for(tmp_path).glob("dialog_*.json")
        )

    def test_a_web_decision_reply_is_applied_without_a_terminal_answer(
        self, tmp_path,
    ):
        """A structured decision from the console executes as-is; the operator
        never has to also answer at the terminal."""
        flow, step, persistence = self._setup(tmp_path)
        # The dialog call id is stable per step, so the answer can be parked
        # before the dialog opens — exactly the shape of a console reply that
        # lands while the terminal is still rendering the round.
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id=flow.flow_id, step_id=step.step_id,
            step_type="implement", prompt="p",
        )
        interaction_calls.write_response(
            call_file,
            {"decision": {"action": "continue", "instruction": "use Postgres"}},
        )

        with patch("tianluo.commands.run._read_multiline_input") as read:
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
            )

        read.assert_not_called()
        assert outcome == _DIALOG_CONTINUE_STEP
        assert step.status == StepStatus.PENDING
        assert step.inputs["retry_count"] == 1

    def test_a_web_text_reply_is_consumed_as_the_next_message(self, tmp_path):
        """Free text from the console is the conversation's next user turn."""
        flow, step, persistence = self._setup(tmp_path)
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id=flow.flow_id, step_id=step.step_id,
            step_type="implement", prompt="p",
        )
        # "exit" is a direct decision, so it becomes a proposal; the confirm
        # round is then answered from the terminal with "1". An EMPTY line is
        # deliberately not a confirmation — it means "resume unchanged".
        interaction_calls.write_response(call_file, {"response": "exit"})

        with patch(
            "tianluo.commands.run._read_multiline_input", return_value="1"
        ) as read:
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
            )

        assert outcome == _DIALOG_EXIT
        # Exactly one terminal read: the confirmation of the web-supplied
        # decision. The message itself never had to be typed.
        assert read.call_count == 1


class TestDialogCtrlC:
    """Ctrl-C inside the dialog has two distinct meanings, and both are load-
    bearing: at the input box it IS the exit decision, while during the agent's
    reply it cancels only that round."""

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        return flow, step, MagicMock(spec=PersistenceManager)

    def test_ctrl_c_while_the_agent_replies_cancels_only_that_round(
        self, tmp_path, monkeypatch
    ):
        """The conversation survives, the stop signal is released, and the
        input box comes back. A signal left set here would abort every later
        LLM attempt in the process (LLMCaller checks it before each spawn)."""
        from tianluo.stop_signal import get_stop_signal

        flow, step, persistence = self._setup(tmp_path)
        asked: list = []

        def _ask(self_, text):
            asked.append(text)
            # As a real interrupted round does: the signal is still set when
            # the KeyboardInterrupt propagates out of the LLM call.
            get_stop_signal().request()
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "tianluo.engine.interjection_dialog.InterjectionDialog.ask", _ask
        )
        # First message provokes the (interrupted) reply; the second is empty →
        # resume unchanged, which is only reachable if we got back to the box.
        with patch(
            "tianluo.commands.run._read_multiline_input",
            side_effect=["what are you doing?", ""],
        ) as read:
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
            )

        assert asked == ["what are you doing?"]
        assert read.call_count == 2, "the input box was not shown again"
        assert not get_stop_signal().is_set(), "the cancelled round left the signal set"
        assert outcome == _DIALOG_CONTINUE_STEP

    def test_ctrl_c_at_the_input_box_is_the_exit_decision(self, tmp_path):
        flow, step, persistence = self._setup(tmp_path)
        with patch(
            "tianluo.commands.run._read_multiline_input",
            side_effect=KeyboardInterrupt,
        ):
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
            )
        assert outcome == _DIALOG_EXIT

    def test_ctrl_c_at_the_confirm_gate_opens_the_dialog(self, tmp_path, monkeypatch):
        """Interrupting a review is a question ABOUT what is being reviewed;
        aborting the flow instead gave the operator nowhere to ask it."""
        flow = _flow(StepType.IMPLEMENT, StepType.CONFIRM)
        confirm = flow.state.steps[flow.state.current_step_id]
        confirm.inputs["step_to_review_id"] = flow.state.step_history[0]
        confirm.inputs["step_to_review_type"] = "implement"
        persistence = PersistenceManager(tmp_path)
        opened = {}

        def _fake_wait(call_file, **kwargs):
            raise KeyboardInterrupt

        def _fake_dialog(*args, **kwargs):
            opened["context"] = kwargs.get("pause_context")
            return run_mod._DIALOG_EXIT

        monkeypatch.setattr(run_mod, "_await_terminal_or_web_choice", _fake_wait)
        monkeypatch.setattr(run_mod, "_dialog_at_pause_point", _fake_dialog)

        result = run_mod._handle_confirm_pause(
            flow, confirm, persistence, tmp_path,
        )
        assert result == run_mod._DIALOG_EXIT
        assert opened["context"] == "confirm"


class TestGatesWatchForInterjections:
    """A pre-prompt drain only catches what had already arrived. An operator who
    interjects from the web AFTER a gate's menu is on screen must still reach
    the dialog — otherwise their message waits for the gate to be resolved some
    other way, which is exactly what they interrupted to avoid."""

    @pytest.fixture(autouse=True)
    def _no_leaked_reader(self):
        """The stdin funnel is process-wide state; keep it per-test."""
        stdin_channel.reset()
        yield
        stdin_channel.reset()

    def test_the_choice_wait_reports_an_interjection_that_lands_first(
        self, tmp_path, monkeypatch
    ):
        _queue_interjection(tmp_path, "wait, stop")
        sink: list = []
        # Not a TTY here, so the wait takes the blocking-prompt branch — the
        # already-on-disk probe runs before it and must win.
        source, choice = run_mod._await_terminal_or_web_choice(
            None, message="what now?", options=["a", "b"],
            interjection_sink=sink, project_root=tmp_path,
        )
        assert source == run_mod._FAILURE_SRC_INTERJECT
        assert choice is None
        assert sink == ["wait, stop"]

    def test_the_choice_wait_is_unchanged_without_the_channel(self, monkeypatch):
        monkeypatch.setattr(run_mod, "prompt_user_choice", lambda *a, **k: 1)
        source, choice = run_mod._await_terminal_or_web_choice(
            None, message="what now?", options=["a", "b"],
        )
        assert (source, choice) == (run_mod._FAILURE_SRC_TERMINAL, 1)

    def test_the_choice_wait_keeps_polling_while_the_menu_is_on_screen(
        self, tmp_path, monkeypatch
    ):
        """The regression: off a TTY the menu used to block in a read that only
        returns on a line or EOF, so an interjection arriving AFTER the menu was
        displayed was ignored for the gate's whole life. At the CONFIRM gate,
        which has no decision file to re-check afterwards, nothing was rechecked
        at all."""
        stdin_channel.feed_for_test("", eof=False)  # a pipe nobody types into
        arrivals = iter([[], ["wait, stop"]])
        monkeypatch.setattr(
            run_mod, "_collect_pending_dialog_messages",
            lambda _root: next(arrivals, []),
        )
        sink: list = []

        source, choice = run_mod._await_terminal_or_web_choice(
            None, message="what now?", options=["a", "b"],
            interjection_sink=sink, project_root=tmp_path,
            poll_interval=0.01,
        )

        assert (source, choice) == (run_mod._FAILURE_SRC_INTERJECT, None)
        assert sink == ["wait, stop"]

    def test_losing_the_choice_race_consumes_no_stdin(
        self, tmp_path, monkeypatch
    ):
        """The abandoned menu must leave the pipe untouched — the operator's
        next line belongs to whoever asks for it next."""
        stdin_channel.feed_for_test("2\n", eof=False)
        monkeypatch.setattr(
            run_mod, "_collect_pending_dialog_messages",
            lambda _root: ["stop"],
        )

        source, _choice = run_mod._await_terminal_or_web_choice(
            None, message="what now?", options=["a", "b"],
            interjection_sink=[], project_root=tmp_path, poll_interval=0.01,
        )

        assert source == run_mod._FAILURE_SRC_INTERJECT
        assert stdin_channel.read_line(timeout=0.5) == "2"

    def test_a_line_typed_after_the_menu_still_answers_it(
        self, tmp_path, monkeypatch
    ):
        """Polling must not steal the gate from the terminal: a line that shows
        up in the pipe several slices later is still the operator's choice."""
        stdin_channel.feed_for_test("", eof=False)
        polls = {"n": 0}

        def _poll(_root):
            polls["n"] += 1
            if polls["n"] == 2:
                stdin_channel.append_for_test("2\n")
            return []

        monkeypatch.setattr(run_mod, "_collect_pending_dialog_messages", _poll)

        source, choice = run_mod._await_terminal_or_web_choice(
            None, message="what now?", options=["a", "b"],
            interjection_sink=[], project_root=tmp_path, poll_interval=0.01,
        )

        assert (source, choice) == (run_mod._FAILURE_SRC_TERMINAL, 1)

    def test_a_web_decision_landing_mid_menu_wins_the_choice_race(
        self, tmp_path, monkeypatch
    ):
        """The failure gate's sibling of the same defect: its decision file was
        only re-read after the blocking read returned."""
        stdin_channel.feed_for_test("", eof=False)
        call_path = interaction_calls.write_retry_decision_call(
            tmp_path, flow_id="f1", step_id="s-gate", step_type="implement",
            error="boom",
        )

        def _poll(_root):
            interaction_calls.write_response(call_path, {"decision": "skip"})
            return []

        monkeypatch.setattr(run_mod, "_collect_pending_dialog_messages", _poll)

        source, choice = run_mod._await_terminal_or_web_choice(
            call_path, message="what now?", options=["a", "b", "c"],
            interjection_sink=[], project_root=tmp_path, poll_interval=0.01,
        )

        assert (source, choice) == (run_mod._FAILURE_SRC_WEB, 1)
        assert not call_path.exists()

    def test_the_confirm_gate_opens_the_dialog_on_an_interjection(
        self, tmp_path, monkeypatch
    ):
        flow = _flow(StepType.IMPLEMENT, StepType.CONFIRM)
        confirm = flow.state.steps[flow.state.current_step_id]
        confirm.inputs["step_to_review_id"] = flow.state.step_history[0]
        confirm.inputs["step_to_review_type"] = "implement"
        persistence = PersistenceManager(tmp_path)

        opened = {}

        def _fake_wait(call_file, **kwargs):
            kwargs["interjection_sink"].append("why this plan?")
            return (run_mod._FAILURE_SRC_INTERJECT, None)

        def _fake_dialog(*args, **kwargs):
            opened["messages"] = kwargs.get("initial_messages")
            opened["context"] = kwargs.get("pause_context")
            return run_mod._DIALOG_EXIT

        monkeypatch.setattr(run_mod, "_await_terminal_or_web_choice", _fake_wait)
        monkeypatch.setattr(run_mod, "_dialog_at_pause_point", _fake_dialog)

        result = run_mod._handle_confirm_pause(
            flow, confirm, persistence, tmp_path,
        )
        assert result == run_mod._DIALOG_EXIT
        assert opened["messages"] == ["why this plan?"]
        assert opened["context"] == "confirm"


class TestTheNonTtyWaitRacesTheWebChannel:
    """A CLI-started flow whose stdin is an open non-TTY pipe never reaches EOF.

    The wait therefore cannot be "read stdin, then check the web file once":
    a console reply landing while the process sits in that read must be acted
    on, and it must go through the STRUCTURED reply path — the generic response
    reader flattens a ``{"decision": {...}}`` payload to its ``str()`` repr and
    consumes the file, which fed the operator's Apply back to the dialog LLM as
    prose and left the decision unexecutable.
    """

    @pytest.fixture(autouse=True)
    def _no_leaked_reader(self):
        """The stdin funnel is process-wide state; keep it per-test."""
        stdin_channel.reset()
        yield
        stdin_channel.reset()

    def _blocking_stdin(self, released, before=None):
        """Stand in for a pipe stdin that stays open: no answer ever completes.

        The read is polled in bounded slices, so an open pipe shows up as a
        run of ``PENDING`` returns rather than a thread parked in the read —
        which is the whole point: abandoning this read consumes nothing.
        """

        fired = []

        def _read(**_kwargs):
            if before is not None and not fired:
                fired.append(True)
                before()
            released.wait(0.01)
            return stdin_channel.PENDING

        return _read

    def test_a_structured_decision_arriving_mid_wait_is_applied(self, tmp_path):
        import threading

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)
        released = threading.Event()

        def _answer_from_the_web():
            calls = list(
                interaction_calls.calls_dir_for(tmp_path).glob("dialog_*.json")
            )
            assert calls, "the round was not mirrored before the wait"
            interaction_calls.write_response(
                calls[0],
                {"decision": {"action": "continue", "instruction": "use Postgres"}},
            )

        try:
            with patch(
                "tianluo.commands.run._read_multiline_input",
                side_effect=self._blocking_stdin(released, _answer_from_the_web),
            ):
                outcome = run_mod._run_interjection_dialog(
                    flow, step, persistence, tmp_path,
                )
        finally:
            released.set()

        # Executed as a decision — not handed to the dialog LLM as a message.
        assert outcome == _DIALOG_CONTINUE_STEP
        assert step.status == StepStatus.PENDING
        assert step.inputs["retry_count"] == 1

    def test_a_web_text_reply_arriving_mid_wait_is_the_next_message(
        self, tmp_path,
    ):
        import threading

        released = threading.Event()
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement",
            prompt="p",
        )

        def _answer():
            interaction_calls.write_response(call_file, "what about caching?")

        try:
            with patch(
                "tianluo.commands.run._read_multiline_input",
                side_effect=self._blocking_stdin(released, _answer),
            ):
                source, text = run_mod._await_terminal_or_web(
                    call_file,
                    prompt_title="t",
                    prompt_message="m",
                    poll_interval=0.05,
                )
        finally:
            released.set()

        assert (source, text) == (run_mod._DISCOVERY_SRC_WEB, "what about caching?")
        # Consumed, so the same answer cannot be replayed as the next round's.
        assert not interaction_calls.response_path(call_file).exists()

    def test_a_terminal_answer_still_wins_when_stdin_speaks_first(self, tmp_path):
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s2", step_type="implement",
            prompt="p",
        )
        with patch(
            "tianluo.commands.run._read_multiline_input", return_value="typed"
        ):
            source, text = run_mod._await_terminal_or_web(
                call_file, prompt_title="t", prompt_message="m",
                poll_interval=0.05,
            )
        assert (source, text) == (run_mod._DISCOVERY_SRC_TERMINAL, "typed")

    def test_a_cancelled_terminal_read_is_still_a_cancel(self, tmp_path):
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s3", step_type="implement",
            prompt="p",
        )
        with patch(
            "tianluo.commands.run._read_multiline_input", return_value=None
        ):
            source, text = run_mod._await_terminal_or_web(
                call_file, prompt_title="t", prompt_message="m",
                poll_interval=0.05,
            )
        assert (source, text) == (run_mod._DISCOVERY_SRC_CANCEL, None)

    def test_a_reply_landing_between_the_two_reads_still_goes_structured(
        self, tmp_path,
    ):
        """The sweep peeks, then re-asks the tick.

        A payload that lands in the window between the tick and the generic
        read used to be flattened by the loser of that race — every other web
        Apply, in practice.
        """
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s5", step_type="implement",
            prompt="p",
        )
        state = {"asked": 0}

        def _tick():
            state["asked"] += 1
            if state["asked"] == 1:
                # Nothing yet — and the operator's Apply lands right here.
                interaction_calls.write_response(
                    call_file, {"decision": {"action": "exit"}}
                )
                return None
            return "structured"

        assert run_mod._poll_web_answer(call_file, _tick) == "structured"

    def test_the_tick_is_preferred_over_the_flattening_reader(self, tmp_path):
        """The structured tick is asked first everywhere the web is consulted."""
        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s4", step_type="implement",
            prompt="p",
        )
        interaction_calls.write_response(call_file, {"decision": {"action": "exit"}})

        answer = run_mod._poll_web_answer(call_file, lambda: "structured")

        assert answer == "structured"
        # The generic reader never ran, so the payload is still on disk for the
        # structured path to consume.
        assert interaction_calls.response_path(call_file).exists()


class TestResetConfirmationGating:
    def test_a_reset_without_a_usable_preview_is_refused(self, tmp_path, monkeypatch):
        """A reset whose preview never rendered is a blind discard."""
        from tianluo.engine.flow_workspace import ResetPreview
        from tianluo.engine.interjection_dialog import DialogDecision

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = PersistenceManager(tmp_path)
        applied = []

        monkeypatch.setattr(
            run_mod, "_reset_preview",
            lambda *a, **k: ResetPreview(ok=False, error="git exploded"),
        )
        monkeypatch.setattr(
            "tianluo.engine.interjection_dialog.apply_decision",
            lambda *a, **k: applied.append(a),
        )

        outcome, error = run_mod._confirm_and_apply_decision(
            flow, step,
            DialogDecision(action="restart", workspace="reset"),
            types.SimpleNamespace(transcript=lambda: []),
            persistence, tmp_path, pause_context=None,
        )
        assert applied == []
        # A refused confirmation stays IN the dialog (no outcome, an error) —
        # it must never degrade into the "continue" the operator rejected.
        assert outcome is None
        assert "git exploded" in error

    def test_a_failed_reset_still_shows_its_safety_ref_and_recovery(
        self, tmp_path, monkeypatch
    ):
        """Once a safety ref exists the tree HAS been changed; a later failure
        must not swallow the only handle on the discarded work."""
        from tianluo.engine.interjection_dialog import DecisionOutcome, DialogDecision

        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = PersistenceManager(tmp_path)
        from tianluo.engine.flow_workspace import ResetResult

        reset = ResetResult(
            ok=False, error="snapshot replay failed",
            safe_ref="refs/tianluo/discarded/f1/20260101-000000",
            safe_commit="abc1234", baseline_commit="base1234",
            discarded_summary=" M a.py", flow_commits=["c0ffee wip"],
        )
        rendered: list = []

        monkeypatch.setattr(run_mod, "_reset_preview", lambda *a, **k: None)
        monkeypatch.setattr(
            "tianluo.engine.interjection_dialog.apply_decision",
            lambda *a, **k: DecisionOutcome(
                action="restart", ok=False, error="snapshot replay failed",
                reset=reset,
            ),
        )
        monkeypatch.setattr(
            run_mod, "render_full",
            lambda body, **k: rendered.append(body),
        )

        run_mod._confirm_and_apply_decision(
            flow, step, DialogDecision(action="restart", workspace="reset"),
            types.SimpleNamespace(transcript=lambda: []),
            persistence, tmp_path, pause_context=None,
        )
        joined = "\n".join(rendered)
        assert "refs/tianluo/discarded/f1/20260101-000000" in joined
        assert "git checkout" in joined


class TestASettledSessionBindingSurvivesTheProcessBoundary:
    """A dialog that already resolved (or lost) its session must not resolve it
    again on the next round — including when the operator takes the paused
    conversation over at the terminal.
    """

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        return flow, step, MagicMock(spec=PersistenceManager)

    def test_a_persisted_null_binding_is_not_probed_again(self, tmp_path):
        """``None`` is a settled answer: the provider rejected the resume and
        the round fell back to the standalone read-only conversation. Looking
        the session up again would rediscover the rejected id from the earlier
        session-bearing record and probe the dead session a second time."""
        flow, step, persistence = self._setup(tmp_path)

        with patch(
            "tianluo.engine.interjection_dialog.find_dialog_session"
        ) as find, patch(
            "tianluo.commands.run._read_multiline_input", return_value=""
        ):
            run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
                prior_state={
                    "binding": None, "transcript": [], "decision": None,
                },
            )

        find.assert_not_called()

    def test_the_takeover_announces_a_standalone_conversation(self, tmp_path):
        """What the settled binding decides is what the operator is told — and
        what the mirrored round reports to the web console."""
        flow, step, persistence = self._setup(tmp_path)
        seen = {}

        def _capture(*_a, **_k):
            calls = list(
                interaction_calls.calls_dir_for(tmp_path).glob("dialog_*.json")
            )
            seen["payloads"] = [interaction_calls.read_call(c) for c in calls]
            return ""

        with patch(
            "tianluo.engine.interjection_dialog.find_dialog_session",
            return_value={
                "agent_name": "primary", "provider_session_id": "sid-dead",
                "runner_type": "claude-code", "session_cwd": str(tmp_path),
            },
        ), patch(
            "tianluo.commands.run._read_multiline_input", side_effect=_capture
        ):
            run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
                prior_state={
                    "binding": None, "transcript": [], "decision": None,
                },
            )

        payloads = seen.get("payloads") or []
        assert payloads, "the round was not mirrored to a call file"
        assert payloads[0]["context"]["same_session"] is False

    def test_a_dialog_opened_fresh_still_resolves_its_session(self, tmp_path):
        """No prior state means nothing has been settled yet."""
        flow, step, persistence = self._setup(tmp_path)

        with patch(
            "tianluo.engine.interjection_dialog.find_dialog_session",
            return_value=None,
        ) as find, patch(
            "tianluo.commands.run._read_multiline_input", return_value=""
        ):
            run_mod._run_interjection_dialog(flow, step, persistence, tmp_path)

        assert find.call_count == 1


class TestABareConfirmationIsBoundToItsRound:
    """A fieldless "confirm" applies the round its author SAW, or nothing.

    Every round of one conversation is republished under the same call id, so a
    confirmation carrying no fields ("apply what is shown") cannot be read
    against whatever the flow has published by the time the answer is picked
    up: the live proposal may already carry an edit made after that client
    rendered its round, and for ``restart`` + ``workspace: reset`` applying it
    discards the workspace on an approval nobody gave.
    """

    def _run(self, tmp_path, waits, spy_applied):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        persistence = MagicMock(spec=PersistenceManager)
        real = run_mod._confirm_and_apply_decision

        def _spy(flow_, step_, decision, *args, **kwargs):
            spy_applied.append(decision.to_dict())
            return real(flow_, step_, decision, *args, **kwargs)

        calls = []

        def _fake_wait(call_file, **kwargs):
            calls.append(call_file)
            return waits[len(calls) - 1](call_file, kwargs)

        with patch.object(run_mod, "_await_terminal_or_web", _fake_wait), \
                patch.object(run_mod, "_confirm_and_apply_decision", _spy), patch(
                    "tianluo.engine.interjection_dialog."
                    "InterjectionDialog._make_caller"
                ) as make:
            outcome = run_mod._run_interjection_dialog(
                flow, step, persistence, tmp_path,
                initial_messages=["restart"],
                pause_context="failure",
            )
        return outcome, make.return_value.call

    def test_a_confirmation_of_a_round_the_terminal_has_replaced_is_refused(
        self, tmp_path
    ):
        """The hole a published-value snapshot alone leaves open.

        The terminal edits ``keep`` into ``reset`` and the next round is
        republished BEFORE the console's answer lands. Binding the confirmation
        to "whatever was last published" reads that republication as the thing
        the operator approved.
        """
        applied = []

        def _edit(_call_file, _kwargs):
            return (run_mod._DISCOVERY_SRC_TERMINAL, "workspace: reset")

        def _stale_confirm(call_file, kwargs):
            # ``reset`` is on the console now; this client is still showing the
            # ``keep`` round it was given a round ago.
            interaction_calls.write_response(call_file, {"response": "confirm"})
            return (run_mod._DISCOVERY_SRC_WEB, kwargs["tick_callback"]())

        def _resume(_call_file, _kwargs):
            return (run_mod._DISCOVERY_SRC_TERMINAL, "")

        outcome, caller = self._run(
            tmp_path, [_edit, _stale_confirm, _resume], applied
        )

        # Only the empty line's plain continue ever reached the apply path.
        assert [(d["action"], d["workspace"]) for d in applied] == [
            ("continue", "keep")
        ]
        assert outcome == run_mod._DIALOG_RESUME_PAUSE  # not _DIALOG_RESTARTED
        caller.assert_not_called()

    def test_a_blind_retry_of_the_refused_confirmation_is_refused_again(
        self, tmp_path
    ):
        """The refusal is answered by EVIDENCE, never by repeating it.

        A client that times out and re-sends the same fieldless answer before
        it ever fetched the republished round has still seen only the round it
        was refused for. Letting the retry through — by forgetting the earlier
        rounds — is exactly how an approval of ``keep`` executed a ``reset``.
        Echoing the round id it rendered is what makes the answer bindable.
        """
        applied = []

        def _edit(_call_file, _kwargs):
            return (run_mod._DISCOVERY_SRC_TERMINAL, "instruction: use Postgres")

        def _confirm(call_file, kwargs):
            interaction_calls.write_response(call_file, {"response": "confirm"})
            return (run_mod._DISCOVERY_SRC_WEB, kwargs["tick_callback"]())

        def _echo_current(call_file, kwargs):
            revision = interaction_calls.read_call(call_file)["context"][
                "decision_revision"
            ]
            interaction_calls.write_response(
                call_file,
                {"response": "confirm", "decision_revision": revision},
            )
            return (run_mod._DISCOVERY_SRC_WEB, kwargs["tick_callback"]())

        outcome, _caller = self._run(
            tmp_path, [_edit, _confirm, _confirm, _echo_current], applied
        )

        # Neither blind confirmation reached the apply path; the one that named
        # the round it answered did.
        assert [(d["action"], d["instruction"]) for d in applied] == [
            ("restart", "use Postgres")
        ]
        assert outcome == run_mod._DIALOG_RESTARTED

    def test_a_lone_round_is_still_confirmable_without_an_echo(self, tmp_path):
        """Nothing has replaced it, so a fieldless answer is unambiguous."""
        applied = []

        def _confirm(call_file, kwargs):
            interaction_calls.write_response(call_file, {"response": "confirm"})
            return (run_mod._DISCOVERY_SRC_WEB, kwargs["tick_callback"]())

        outcome, _caller = self._run(tmp_path, [_confirm], applied)

        assert [(d["action"], d["workspace"]) for d in applied] == [
            ("restart", "keep")
        ]
        assert outcome == run_mod._DIALOG_RESTARTED

    def test_an_echoed_round_id_binds_the_confirmation_exactly(self, tmp_path):
        """A client that echoes the round it rendered is never mis-bound.

        The ledger of published rounds is a bound on what MIGHT have been on
        screen; the echoed id is what WAS, so it settles the question outright —
        including for a console left open across a later round.
        """
        applied = []
        seen = {}

        def _capture(call_file, _kwargs):
            seen["revision"] = interaction_calls.read_call(call_file)[
                "context"
            ]["decision_revision"]
            return (run_mod._DISCOVERY_SRC_TERMINAL, "workspace: reset")

        def _echo_stale(call_file, kwargs):
            # The id of the round this client rendered — the ``keep`` one,
            # which the terminal has since replaced.
            interaction_calls.write_response(
                call_file,
                {"response": "confirm", "decision_revision": seen["revision"]},
            )
            return (run_mod._DISCOVERY_SRC_WEB, kwargs["tick_callback"]())

        def _resume(_call_file, _kwargs):
            return (run_mod._DISCOVERY_SRC_TERMINAL, "")

        outcome, _caller = self._run(
            tmp_path, [_capture, _echo_stale, _resume], applied
        )

        assert seen["revision"]
        assert [(d["action"], d["workspace"]) for d in applied] == [
            ("continue", "keep")
        ]
        assert outcome == run_mod._DIALOG_RESUME_PAUSE

    def test_an_echoed_current_round_id_is_applied(self, tmp_path):
        applied = []

        def _echo_current(call_file, kwargs):
            revision = interaction_calls.read_call(call_file)["context"][
                "decision_revision"
            ]
            interaction_calls.write_response(
                call_file,
                {"response": "confirm", "decision_revision": revision},
            )
            return (run_mod._DISCOVERY_SRC_WEB, kwargs["tick_callback"]())

        outcome, _caller = self._run(tmp_path, [_echo_current], applied)

        assert [(d["action"], d["workspace"]) for d in applied] == [
            ("restart", "keep")
        ]
        assert outcome == run_mod._DIALOG_RESTARTED


class TestBareConfirmationBinding:
    """The binding rule itself, in isolation."""

    def _round(self, revision, at):
        return {"revision": revision, "at": at}

    def test_the_only_published_round_is_confirmable(self):
        assert not run_mod._bare_confirmation_is_stale(
            [self._round("a", 10.0)], "a", responded_at=11.0
        )

    def test_a_round_published_after_the_answer_cannot_have_been_seen(self):
        # The answer predates the republication, so it is an answer to the
        # round before it — which is no longer the one on the table.
        assert run_mod._bare_confirmation_is_stale(
            [self._round("a", 10.0), self._round("b", 12.0)], "b",
            responded_at=11.0,
        )

    def test_a_console_that_may_be_showing_either_round_is_ambiguous(self):
        assert run_mod._bare_confirmation_is_stale(
            [self._round("a", 10.0), self._round("b", 12.0)], "b",
            responded_at=13.0,
        )

    def test_an_unknown_answer_time_is_bound_to_every_round(self):
        assert run_mod._bare_confirmation_is_stale(
            [self._round("a", 10.0), self._round("b", 12.0)], "b",
        )

    def test_an_echoed_id_outranks_the_timing(self):
        rounds = [self._round("a", 10.0), self._round("b", 12.0)]
        assert not run_mod._bare_confirmation_is_stale(
            rounds, "b", echoed_revision="b", responded_at=None
        )
        assert run_mod._bare_confirmation_is_stale(
            rounds, "b", echoed_revision="a", responded_at=13.0
        )

    def test_a_message_round_carries_no_confirmable_decision(self):
        # Nothing with a decision was ever published: the word "confirm" is the
        # operator's next message, which the caller resolves — not a stale
        # confirmation of something never offered.
        assert not run_mod._bare_confirmation_is_stale(
            [self._round("", 10.0)], "", responded_at=11.0
        )
        # …but a proposal that has never been on the console cannot be
        # confirmed by a reply that predates it.
        assert run_mod._bare_confirmation_is_stale(
            [self._round("", 10.0)], "a", responded_at=11.0
        )

    def test_a_withdrawn_proposal_is_not_confirmable(self):
        assert run_mod._bare_confirmation_is_stale(
            [self._round("a", 10.0)], "", responded_at=11.0
        )


class TestNonInteractiveBareConfirmationBinding:
    """The json/daemon shape binds a fieldless confirmation the same way."""

    def _setup(self, tmp_path):
        flow = _flow(StepType.IMPLEMENT)
        step = flow.state.steps[flow.state.current_step_id]
        return flow, step, MagicMock(spec=PersistenceManager)

    def _propose_restart_keep(self, flow, step, persistence, tmp_path):
        decision_json = json.dumps({
            "mode": "decision", "content": "ok",
            "decision": {"action": "restart", "workspace": "keep"},
        })
        with patch(
            "tianluo.engine.interjection_dialog.InterjectionDialog._make_caller"
        ) as make:
            make.return_value.call.return_value = decision_json
            _run_interjection_dialog_noninteractive(
                flow, step, persistence, tmp_path, initial_messages=["start over"],
            )
        return Path(_dialog_state(flow)["call_file"])

    def test_a_confirmation_of_a_superseded_round_republishes_instead(
        self, tmp_path
    ):
        flow, step, persistence = self._setup(tmp_path)
        call_file = self._propose_restart_keep(flow, step, persistence, tmp_path)

        # The console edits the round into a workspace-discarding one and asks
        # for its preview: the republished round now proposes ``reset``.
        interaction_calls.write_response(call_file, {
            "decision": {"action": "restart", "workspace": "reset"},
            "preview_request": True,
        })
        _run_interjection_dialog_noninteractive(flow, step, persistence, tmp_path)
        assert _dialog_state(flow)["decision"]["workspace"] == "reset"

        # A client still showing the ``keep`` round now confirms it.
        call_file = Path(_dialog_state(flow)["call_file"])
        interaction_calls.write_response(call_file, {"response": "confirm"})
        outcome = _run_interjection_dialog_noninteractive(
            flow, step, persistence, tmp_path
        )

        assert outcome == _DIALOG_AWAITING_WEB
        state = _dialog_state(flow)
        assert state is not None  # the dialog is still open, nothing executed
        assert state["apply_error"]
        assert step.status == StepStatus.RUNNING

    def test_a_blind_retry_across_the_process_boundary_is_refused_again(
        self, tmp_path
    ):
        """The ledger survives the daemon's wake-ups, and keeps every round.

        Pruning it to the round being republished made the next fieldless
        answer bindable by construction — including a blind retry sent by a
        client that never fetched that round.
        """
        flow, step, persistence = self._setup(tmp_path)
        call_file = self._propose_restart_keep(flow, step, persistence, tmp_path)
        interaction_calls.write_response(call_file, {
            "decision": {"action": "restart", "instruction": "use Postgres"},
            "preview_request": True,
        })
        _run_interjection_dialog_noninteractive(flow, step, persistence, tmp_path)
        call_file = Path(_dialog_state(flow)["call_file"])
        interaction_calls.write_response(call_file, {"response": "confirm"})
        _run_interjection_dialog_noninteractive(flow, step, persistence, tmp_path)

        call_file = Path(_dialog_state(flow)["call_file"])
        interaction_calls.write_response(call_file, {"response": "confirm"})
        outcome = _run_interjection_dialog_noninteractive(
            flow, step, persistence, tmp_path
        )
        assert outcome == _DIALOG_AWAITING_WEB
        assert _dialog_state(flow)["apply_error"]
        assert step.status == StepStatus.RUNNING

        # Naming the round it answers is what makes it bindable — and it is
        # then executed, so the refusal is never an unbreakable loop.
        call_file = Path(_dialog_state(flow)["call_file"])
        interaction_calls.write_response(call_file, {
            "response": "confirm",
            "decision_revision": interaction_calls.read_call(call_file)[
                "context"
            ]["decision_revision"],
        })
        outcome = _run_interjection_dialog_noninteractive(
            flow, step, persistence, tmp_path
        )
        assert outcome == _DIALOG_RESTARTED

    def test_a_daemon_wrapped_bare_confirmation_confirms_the_proposal(
        self, tmp_path
    ):
        """The daemon's own envelope must not turn a confirm into a continue.

        A remote client's ``{"response": "confirm", "decision_revision": ...}``
        is re-wrapped by the daemon; read as a decision it has no ``action``,
        and the defaulted ``continue`` would resume the flow instead of running
        the ``restart`` the operator confirmed.
        """
        flow, step, persistence = self._setup(tmp_path)
        call_file = self._propose_restart_keep(flow, step, persistence, tmp_path)
        revision = interaction_calls.read_call(call_file)["context"][
            "decision_revision"
        ]
        interaction_calls.write_response(call_file, {
            "call_id": call_file.stem,
            "response": {"response": "confirm", "decision_revision": revision},
            "responded_at": 1234.5,
            "source": "daemon-client",
        })
        outcome = _run_interjection_dialog_noninteractive(
            flow, step, persistence, tmp_path
        )
        assert outcome == _DIALOG_RESTARTED

    def test_an_echoed_round_id_settles_it_across_the_process_boundary(
        self, tmp_path
    ):
        flow, step, persistence = self._setup(tmp_path)
        call_file = self._propose_restart_keep(flow, step, persistence, tmp_path)
        stale_revision = interaction_calls.read_call(call_file)["context"][
            "decision_revision"
        ]
        interaction_calls.write_response(call_file, {
            "decision": {"action": "restart", "workspace": "reset"},
            "preview_request": True,
        })
        _run_interjection_dialog_noninteractive(flow, step, persistence, tmp_path)

        call_file = Path(_dialog_state(flow)["call_file"])
        interaction_calls.write_response(call_file, {
            "response": "confirm", "decision_revision": stale_revision,
        })
        outcome = _run_interjection_dialog_noninteractive(
            flow, step, persistence, tmp_path
        )

        assert outcome == _DIALOG_AWAITING_WEB
        assert _dialog_state(flow)["apply_error"]
