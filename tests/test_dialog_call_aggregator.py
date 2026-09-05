"""The ``dialog`` call kind on the daemon side.

An interjection dialog opened at a failure-decision pause is keyed to the
FAILED step and is the operator's only channel for resolving it, so it must be
exempt from the staleness filter that otherwise hides a failed step's calls —
the same reason ``retry_decision`` is exempt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.aggregator import DaemonAggregator, PendingCall
from tianluo.engine import interaction_calls


class TestProtocol:
    def test_dialog_is_a_recognised_call_kind(self):
        assert protocol.CALL_KIND_DIALOG == "dialog"
        assert protocol.CALL_KIND_DIALOG in protocol.CALL_KINDS

    def test_the_failed_step_exemption_is_left_to_retry_decision(self):
        """``dialog`` has its own, strictly broader branch in the staleness
        filter (see TestStaleFilterDialogExemption), so it must NOT also sit in the
        narrower failed-only exemption — a reader would take the narrower rule
        for the one in force. The pre-existing member stays untouched."""
        from tianluo.daemon.aggregator import _FAILED_EXEMPT_CALL_KINDS

        assert _FAILED_EXEMPT_CALL_KINDS == {protocol.CALL_KIND_RETRY_DECISION}


class TestCallFile:
    def test_written_call_carries_the_conversation_state(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path,
            flow_id="f1",
            step_id="03_implement_ab",
            step_type="implement",
            prompt="Which database?",
            transcript=[
                {"role": "user", "content": "why SQLite?"},
                {"role": "assistant", "content": "Which database?"},
            ],
            decision=None,
            rewind_targets=[{"step_id": "01_plan_aa", "step_type": "plan"}],
            same_session=True,
            agent_name="dclaude",
        )
        data = interaction_calls.read_call(path)
        assert data["kind"] == protocol.CALL_KIND_DIALOG
        assert data["context"]["flow_id"] == "f1"
        assert data["context"]["step_id"] == "03_implement_ab"
        assert data["context"]["awaiting"] == "message"
        assert data["context"]["same_session"] is True
        assert data["context"]["agent_name"] == "dclaude"
        assert len(data["context"]["transcript"]) == 2
        assert data["context"]["rewind_targets"][0]["step_id"] == "01_plan_aa"

    def test_a_proposed_decision_adds_a_confirm_option(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s", step_type="implement",
            prompt="p", decision={"action": "restart"},
        )
        data = interaction_calls.read_call(path)
        assert data["context"]["awaiting"] == "decision"
        assert data["options"] == [{"label": "confirm", "value": "confirm"}]

    def test_the_call_id_is_stable_across_rounds(self, tmp_path):
        """One growing conversation, not one call per turn."""
        first = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement", prompt="a",
        )
        second = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement", prompt="b",
        )
        assert first == second
        assert interaction_calls.read_call(second)["prompt"] == "b"

    def test_each_round_carries_its_own_prompt_revision(self, tmp_path):
        """The rounds share a call_id, so a consumer that caches anything per
        call_id has no other way to tell one round's prompt from the next — and
        the web console does exactly that with the untruncated prompt body it
        fetches on demand."""
        first = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement", prompt="a",
        )
        rev_a = interaction_calls.read_call(first)["context"]["prompt_revision"]
        interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement", prompt="b",
        )
        rev_b = interaction_calls.read_call(first)["context"]["prompt_revision"]

        assert rev_a and rev_b
        assert rev_a != rev_b
        # Content-derived, so an unchanged body republishes unchanged.
        interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement", prompt="a",
        )
        assert interaction_calls.read_call(first)["context"][
            "prompt_revision"
        ] == rev_a

    def test_the_aggregator_reads_it_as_a_pending_call(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement",
            prompt="Which database?",
        )
        data = interaction_calls.read_call(path)
        call = PendingCall(
            call_id=data["call_id"],
            path=str(path),
            project_root=str(tmp_path),
            kind=data["kind"],
            prompt=data["prompt"],
            context=data["context"],
            options=data["options"],
            step_id=data["context"].get("step_id"),
        )
        payload = call.to_dict()
        assert payload["kind"] == "dialog"
        assert payload["context"]["flow_id"] == "f1"


class TestResponseShapes:
    def test_free_text_is_the_next_dialog_message(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
        )
        interaction_calls.write_response(path, {"response": "use Postgres"})
        assert interaction_calls.read_dialog_response(path) == {
            "text": "use Postgres"
        }

    def test_a_structured_decision_is_recognised(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
        )
        interaction_calls.write_response(
            path, {"response": {"decision": {"action": "restart"}}}
        )
        assert interaction_calls.read_dialog_response(path) == {
            "decision": {"action": "restart"}
        }

    def test_a_top_level_decision_envelope_is_recognised(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
        )
        interaction_calls.write_response(path, {"decision": {"action": "exit"}})
        assert interaction_calls.read_dialog_response(path) == {
            "decision": {"action": "exit"}
        }

    def test_the_one_click_confirm_is_recognised(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "continue"},
        )
        interaction_calls.write_response(path, {"response": "confirm"})
        # The text travels with it: with no proposal on the table the caller
        # falls back to reading the word as the operator's next message.
        assert interaction_calls.read_dialog_response(path) == {
            "confirm": True, "text": "confirm",
        }

    def test_the_published_round_carries_an_id_derived_from_its_values(
        self, tmp_path
    ):
        """A bare confirmation names the round it answers, or nothing can.

        Every round of one conversation shares a ``call_id``, so the id has to
        come off the decision's values: republishing the same round keeps it,
        and any field edited anywhere produces a new one.
        """
        decision = {"action": "restart", "workspace": "keep"}
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision=decision,
        )
        revision = interaction_calls.read_call(path)["context"]["decision_revision"]
        assert revision == interaction_calls.dialog_decision_revision(decision)

        same = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement",
            prompt="p (republished)", decision=dict(decision),
        )
        assert interaction_calls.read_call(same)["context"][
            "decision_revision"
        ] == revision

        edited = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "restart", "workspace": "reset"},
        )
        assert interaction_calls.read_call(edited)["context"][
            "decision_revision"
        ] != revision

    def test_a_round_that_proposes_nothing_has_no_id(self, tmp_path):
        # Nothing to confirm, so no confirmation can be an answer to it.
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
        )
        assert interaction_calls.read_call(path)["context"]["decision_revision"] == ""

    def test_the_binding_reports_when_the_answer_was_written(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "continue"},
        )
        assert interaction_calls.dialog_response_binding(path) == {
            "responded_at": None, "decision_revision": "",
        }
        interaction_calls.write_response(path, {"response": "confirm"})
        binding = interaction_calls.dialog_response_binding(path)
        assert binding["responded_at"] is not None
        assert binding["decision_revision"] == ""

    def test_an_echoed_round_id_is_reported_from_either_level(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "continue"},
        )
        interaction_calls.write_response(
            path, {"response": "confirm", "decision_revision": "abc123"}
        )
        assert interaction_calls.dialog_response_binding(path)[
            "decision_revision"
        ] == "abc123"
        interaction_calls.write_response(
            path, {"response": {"decision_revision": "def456"}}
        )
        assert interaction_calls.dialog_response_binding(path)[
            "decision_revision"
        ] == "def456"

    def test_the_binding_never_changes_the_reply_shape(self, tmp_path):
        # Transport metadata, deliberately NOT folded into the answer: the
        # reply shape is a contract the two dialog drivers read.
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "continue"},
        )
        interaction_calls.write_response(
            path, {"response": "confirm", "decision_revision": "abc123"}
        )
        assert interaction_calls.read_dialog_response(path) == {
            "confirm": True, "text": "confirm",
        }

    def test_the_answer_time_comes_off_the_file_not_the_writers_clock(
        self, tmp_path
    ):
        """A stamp from another host's clock cannot order the rounds.

        The instant this is compared against is a call file's mtime, so the
        answer has to be timed the same way. The daemon that writes the answer
        may sit on a different machine of a shared project directory: a stamp
        from its lagging clock orders a fresh confirmation before every round
        that could have been on screen, and the dialog can then never be
        confirmed at all.
        """
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "continue"},
        )
        interaction_calls.write_response(
            path, {"response": "confirm", "responded_at": 1234.5}
        )
        binding = interaction_calls.dialog_response_binding(path)
        assert binding["responded_at"] != 1234.5
        assert binding["responded_at"] == pytest.approx(
            interaction_calls.response_path(path).stat().st_mtime
        )

    def test_a_daemon_wrapped_bare_confirmation_stays_a_confirmation(
        self, tmp_path
    ):
        """The daemon re-wraps a remote answer in its own envelope.

        A client's ``{"response": "confirm", "decision_revision": ...}`` lands
        nested inside that envelope. Read as a decision it carries no
        ``action`` — and the default ``continue`` would run in place of the
        ``restart`` the operator was confirming.
        """
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "restart"},
        )
        interaction_calls.write_response(path, {
            "call_id": "dialog_s",
            "response": {"response": "confirm", "decision_revision": "abc123"},
            "responded_at": 1234.5,
            "source": "daemon-client",
        })
        assert interaction_calls.read_dialog_response(path) == {
            "confirm": True, "text": "confirm",
        }
        # …and the round id it echoed is still readable as binding metadata.
        assert interaction_calls.dialog_response_binding(path)[
            "decision_revision"
        ] == "abc123"

    def test_a_daemon_wrapped_revision_only_confirmation_is_not_a_decision(
        self, tmp_path
    ):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "exit"},
        )
        interaction_calls.write_response(
            path, {"response": {"decision_revision": "def456"}}
        )
        assert interaction_calls.read_dialog_response(path) == {
            "confirm": True, "text": "confirm",
        }

    def test_a_daemon_wrapped_edited_decision_is_still_a_decision(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
            decision={"action": "continue"},
        )
        interaction_calls.write_response(path, {
            "response": {
                "decision": {"action": "restart", "workspace": "reset"},
                "preview_request": True,
            },
        })
        assert interaction_calls.read_dialog_response(path) == {
            "decision": {"action": "restart", "workspace": "reset"},
            "preview": True,
        }

    def test_no_response_yet_is_none(self, tmp_path):
        path = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f", step_id="s", step_type="implement", prompt="p",
        )
        assert interaction_calls.read_dialog_response(path) is None


class TestHistoryCommandRendering:
    """``luo history show`` must surface a dialog turn distinctly.

    It shares ``render_session_detailed`` with the CLI, so pinning that
    renderer pins the command.
    """

    def test_show_renders_dialog_turns(self, tmp_path, capsys):
        from rich.console import Console

        from tianluo.engine import chat_history

        chat_history.record_prompt(
            tmp_path, "f1", "01_implement_a", "implement", "the step prompt", 0,
        )
        chat_history.record_dialog_message(
            tmp_path, "f1", "01_implement_a", "implement",
            "user", "why SQLite?",
        )
        chat_history.record_dialog_message(
            tmp_path, "f1", "01_implement_a", "implement",
            "assistant", "It was the smallest change.",
        )
        session = chat_history.get_step_history(tmp_path, "f1", "01_implement_a")

        console = Console(width=100, force_terminal=False)
        with console.capture() as capture:
            for renderable in chat_history.render_session_detailed(session):
                console.print(renderable)
        text = capture.get()

        assert "Interjection" in text
        assert "why SQLite?" in text
        assert "It was the smallest change." in text


class TestStaleFilterDialogExemption:
    """A dialog filed against the flow's CURRENT step is the flow's one open
    interaction, whatever status that step holds — including COMPLETED, which
    is exactly where a dialog opened just as the step finished lands. Only the
    flow walking PAST the step retires the call."""

    @staticmethod
    def _state(current: str, statuses: dict) -> dict:
        return {
            "current_step_id": current,
            "steps": {
                sid: {"status": status} for sid, status in statuses.items()
            },
        }

    def _call(self, step_id: str, kind: str = "dialog") -> PendingCall:
        return PendingCall(
            call_id=f"dialog_{step_id}_1", path="", project_root="",
            kind=kind, step_id=step_id,
        )

    def test_dialog_on_a_completed_current_step_survives(self):
        state = self._state("05_test_a", {"05_test_a": "completed"})
        calls = [self._call("05_test_a")]
        kept = DaemonAggregator._filter_stale_calls(calls, state)
        assert [c.call_id for c in kept] == ["dialog_05_test_a_1"]

    def test_dialog_on_a_partial_current_step_survives(self):
        state = self._state("05_test_a", {"05_test_a": "partial"})
        kept = DaemonAggregator._filter_stale_calls(
            [self._call("05_test_a")], state,
        )
        assert len(kept) == 1

    def test_dialog_on_a_step_the_flow_walked_past_is_still_dropped(self):
        state = self._state(
            "06_self_check_b",
            {"05_test_a": "completed", "06_self_check_b": "running"},
        )
        kept = DaemonAggregator._filter_stale_calls(
            [self._call("05_test_a")], state,
        )
        assert kept == []

    def test_other_kinds_on_a_completed_current_step_stay_dropped(self):
        state = self._state("05_test_a", {"05_test_a": "completed"})
        kept = DaemonAggregator._filter_stale_calls(
            [self._call("05_test_a", kind="call")], state,
        )
        assert kept == []
