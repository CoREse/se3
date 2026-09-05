"""LLMCaller's native-resume strategy: selection, record ordering, fallback.

Strategy selection belongs to LLMCaller, never to a runner (charter: multi-command
rotation and recovery live above the runner layer). These tests drive it with a
mock runner so no CLI is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.agent_runner import AgentRunner, InfraErrorType, RunnerStartupMetadata
from tianluo.engine import chat_history
from tianluo.engine.llm_caller import LLMCallError, LLMCaller


@pytest.fixture(autouse=True)
def _clean_ambient_slots():
    """The dialog instruction / resume slots are process-wide and step-scoped —
    closed by ``run_step``'s finally, not by the LLM call that reads them (a DAG
    step's parallel groups each need their own copy). Tests that arm them
    directly therefore have to close them, or they leak into the next test."""
    from tianluo.engine import llm_caller as lc

    lc.clear_extra_prompt()
    lc.consume_dialog_resume()
    yield
    lc.clear_extra_prompt()
    lc.consume_dialog_resume()


class _Result:
    def __init__(self, returncode=0, output="", interrupted=False):
        self.returncode = returncode
        self.output = output
        self.cmd_used = "mock"
        self.cmd_index = 0
        self.was_retry = False
        self.interrupted = interrupted
        self.stderr_tail = ""

    @property
    def success(self):
        return self.returncode == 0


def _ndjson(text="ok"):
    return json.dumps({"type": "result", "subtype": "success", "result": text}) + "\n"


class MockRunner(AgentRunner):
    """Records every call so the test can assert on ordering and argv."""

    def __init__(self, *, supports_resume=True, session_prefix="sid", results=None):
        self.supports_native_resume = supports_resume
        self._session_prefix = session_prefix
        self._n = 0
        self.calls = []
        self.startup_calls = 0
        self.last_session_id = None
        self.results = list(results or [])

    def get_startup_metadata(self, env=None):
        self.startup_calls += 1
        self._n += 1
        self.last_session_id = f"{self._session_prefix}-{self._n}"
        return RunnerStartupMetadata(
            provider="mock", model="m", provider_session_id=self.last_session_id
        )

    def build_call_args(self, prompt, read_only, context_files=None, **_kw):
        return ["fresh", prompt]

    def build_resume_call_args(
        self, session_id, prompt, read_only, context_files=None
    ):
        return ["resume", session_id, prompt]

    def run(self, *a, **k):  # pragma: no cover - unused
        raise NotImplementedError

    def run_with_monitor(self, args=None, cwd=None, **kwargs):
        self.calls.append({"args": list(args or []), "cwd": cwd})
        if self.results:
            return self.results.pop(0)
        return _Result(output=_ndjson())

    def detect_infra_error(self, returncode, stdout, stderr):
        return InfraErrorType.NONE


def _caller(tmp_path, runner, **kwargs):
    caller = LLMCaller(
        project_root=tmp_path,
        flow_id="f1",
        step_id="s1",
        step_type="implement",
        agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
        **kwargs,
    )
    caller._runner_cache["primary"] = runner
    caller._runner = runner
    return caller


class TestRecordOrdering:
    def test_session_id_is_recorded_before_the_subprocess_is_spawned(self, tmp_path):
        """INVARIANT: an attempt interrupted at startup must still leave a
        named, resumable session in history."""
        order = []
        runner = MockRunner()
        original_startup = runner.get_startup_metadata

        def _startup(env=None):
            order.append("startup")
            return original_startup(env)

        runner.get_startup_metadata = _startup
        caller = _caller(tmp_path, runner)
        real_record = caller._record_prompt

        def _record(*a, **k):
            order.append("record_prompt")
            return real_record(*a, **k)

        caller._record_prompt = _record

        def _run(**kwargs):
            order.append("spawn")
            return _Result(output=_ndjson())

        runner.run_with_monitor = _run
        caller.call("do it", json_mode="off")

        assert order == ["startup", "record_prompt", "spawn"]

    def test_prompt_record_carries_the_session_binding(self, tmp_path):
        runner = MockRunner()
        caller = _caller(tmp_path, runner)
        caller.call("do it", json_mode="off")

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        prompt_msg = session.messages[0]
        assert prompt_msg.role == "user"
        assert prompt_msg.provider_session_id == "sid-1"
        assert prompt_msg.session_cwd == str(tmp_path)
        assert prompt_msg.resume_strategy == "rebuild"

    def test_response_record_carries_the_same_binding(self, tmp_path):
        runner = MockRunner()
        caller = _caller(tmp_path, runner)
        caller.call("do it", json_mode="off")

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        assistant = [m for m in session.messages if m.role == "assistant"][0]
        assert assistant.provider_session_id == "sid-1"
        assert assistant.resume_strategy == "rebuild"


class TestStrategySelection:
    def test_first_call_is_never_a_resume(self, tmp_path):
        runner = MockRunner()
        caller = _caller(tmp_path, runner)
        caller.call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"

    def test_retry_resumes_the_recorded_session(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")

        retry_runner = MockRunner()
        retry = _caller(tmp_path, retry_runner, external_attempt=1)
        retry.call("do it", json_mode="off")

        assert retry_runner.calls[0]["args"][0] == "resume"
        assert retry_runner.calls[0]["args"][1] == "sid-1"
        # A resume must not mint a new session id.
        assert retry_runner.startup_calls == 0

    def test_resume_runs_in_the_recorded_cwd(self, tmp_path):
        """A DAG group's session lives in its worktree — and so does its caller."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        chat_history.record_prompt(
            worktree, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-w", session_cwd=str(worktree),
            resume_strategy="rebuild", agent_name="primary",
            runner_type="claude-code",
        )
        runner = MockRunner()
        caller = _caller(worktree, runner, external_attempt=1)
        caller.call("do it", json_mode="off")
        call = caller._runner_cache["primary"].calls[0]
        assert call["args"][0] == "resume"
        assert call["cwd"] == worktree

    def test_session_bound_to_another_workspace_is_rebuilt(self, tmp_path):
        """The recorded cwd is not this caller's workspace → rebuild here.

        A group worktree that failed reuse still exists on disk; resuming into
        it would edit the wrong checkout while this caller's branch stayed
        empty.
        """
        stale_worktree = tmp_path / "old-wt"
        stale_worktree.mkdir()
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-w", session_cwd=str(stale_worktree),
            resume_strategy="rebuild", agent_name="primary",
            runner_type="claude-code",
        )
        runner = MockRunner()
        caller = _caller(tmp_path, runner, external_attempt=1)
        caller.call("do it", json_mode="off")
        call = caller._runner_cache["primary"].calls[0]
        assert call["args"][0] == "fresh"
        assert call["cwd"] == tmp_path

    def test_resume_prompt_carries_no_rebuilt_context(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("the original prompt", json_mode="off")

        retry_runner = MockRunner()
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "the original prompt", json_mode="off"
        )

        sent = retry_runner.calls[0]["args"][2]
        assert "[Previous conversation context" not in sent
        assert "the original prompt" not in sent
        assert "Continuation" in sent

    def test_resume_prompt_restates_the_json_output_contract(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("emit json", json_mode="off")

        retry_runner = MockRunner()
        caller = _caller(tmp_path, retry_runner, external_attempt=1)
        caller.call(
            'emit json\n\n```json\n{"files_changed": []}\n```',
            json_mode="off",
        )
        sent = retry_runner.calls[0]["args"][2]
        assert "Output contract" in sent
        assert '"files_changed"' in sent

    def test_rebuild_strategy_forces_history_reconstruction(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")

        retry_runner = MockRunner()
        retry = _caller(
            tmp_path, retry_runner, external_attempt=1, resume_strategy="rebuild"
        )
        retry.call("do it", json_mode="off")
        assert retry_runner.calls[0]["args"][0] == "fresh"

    def test_runner_without_the_capability_rebuilds(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")

        retry_runner = MockRunner(supports_resume=False)
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "do it", json_mode="off"
        )
        assert retry_runner.calls[0]["args"][0] == "fresh"

    def test_agent_no_longer_in_the_chain_rebuilds(self, tmp_path):
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-x", session_cwd=str(tmp_path),
            resume_strategy="rebuild", agent_name="retired-agent",
        )
        runner = MockRunner()
        caller = _caller(tmp_path, runner, external_attempt=1)
        caller.call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"

    def test_recorded_runner_type_must_still_match(self, tmp_path):
        """Re-pointing an agent name at another runner invalidates its session."""
        chat_history.record_response(
            tmp_path, "f1", "s1", "implement", _ndjson(), 0,
            provider_session_id="sid-y", session_cwd=str(tmp_path),
            resume_strategy="rebuild", agent_name="primary",
            usage_record={"runner_type": "codex", "agent_name": "primary"},
        )
        runner = MockRunner()
        caller = _caller(tmp_path, runner, external_attempt=1)
        caller.call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"

    def test_no_recorded_session_rebuilds(self, tmp_path):
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0, agent_name="primary",
        )
        runner = MockRunner()
        caller = _caller(tmp_path, runner, external_attempt=1)
        caller.call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"


class TestFallback:
    def test_failed_resume_falls_back_to_rebuild_on_the_same_agent(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")

        retry_runner = MockRunner(
            results=[_Result(returncode=1, output="session not found"),
                     _Result(output=_ndjson())]
        )
        caller = _caller(tmp_path, retry_runner, external_attempt=1)
        out = caller.call("do it", json_mode="off")

        assert retry_runner.calls[0]["args"][0] == "resume"
        assert retry_runner.calls[1]["args"][0] == "fresh"
        assert out
        # Falling back must not rotate away from a working agent.
        assert caller._current_agent_index == 0

    def test_a_resume_failure_is_not_a_hard_error(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")
        retry_runner = MockRunner(
            results=[_Result(returncode=2), _Result(output=_ndjson("recovered"))]
        )
        caller = _caller(tmp_path, retry_runner, external_attempt=1)
        assert "recovered" in caller.call("do it", json_mode="off")

    def test_native_stays_disabled_for_the_rest_of_the_sequence(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")
        retry_runner = MockRunner(
            results=[
                _Result(returncode=1),
                _Result(returncode=1),
                _Result(output=_ndjson()),
            ]
        )
        caller = _caller(tmp_path, retry_runner, external_attempt=1)
        caller.call("do it", json_mode="off")
        kinds = [c["args"][0] for c in retry_runner.calls]
        assert kinds[0] == "resume"
        assert "resume" not in kinds[1:]


    def test_a_raising_resume_also_falls_back_to_rebuild(self, tmp_path):
        """An installed CLI that rejects the resume argv raises rather than
        exiting non-zero. Treating only the non-zero shape as a resume failure
        left every remaining slot re-issuing the same broken resume, and the
        call ended as a hard LLMCallError without one rebuild attempt."""
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")

        class _RaisingResume(MockRunner):
            def build_resume_call_args(self, *a, **k):
                raise ValueError("--resume is not supported by this CLI")

        retry_runner = _RaisingResume(results=[_Result(output=_ndjson("recovered"))])
        caller = _caller(tmp_path, retry_runner, external_attempt=1)
        out = caller.call("do it", json_mode="off")

        assert "recovered" in out
        assert [c["args"][0] for c in retry_runner.calls] == ["fresh"]
        assert caller._current_agent_index == 0

    def test_a_resume_subprocess_that_cannot_start_falls_back(self, tmp_path):
        runner = MockRunner()
        _caller(tmp_path, runner).call("do it", json_mode="off")

        class _RaisingRun(MockRunner):
            def run_with_monitor(self, args=None, cwd=None, **kwargs):
                if list(args or [])[:1] == ["resume"]:
                    self.calls.append({"args": list(args or []), "cwd": cwd})
                    raise OSError("cannot spawn")
                return super().run_with_monitor(args=args, cwd=cwd, **kwargs)

        retry_runner = _RaisingRun(results=[_Result(output=_ndjson("recovered"))])
        caller = _caller(tmp_path, retry_runner, external_attempt=1)
        assert "recovered" in caller.call("do it", json_mode="off")
        assert [c["args"][0] for c in retry_runner.calls] == ["resume", "fresh"]


class TestStopSignalGate:
    def test_a_pending_stop_prevents_a_new_attempt(self, tmp_path):
        from tianluo.stop_signal import get_stop_signal

        runner = MockRunner()
        caller = _caller(tmp_path, runner)
        sig = get_stop_signal()
        sig.request()
        try:
            with pytest.raises(KeyboardInterrupt):
                caller.call("do it", json_mode="off")
        finally:
            sig.clear()
        assert runner.calls == []


class TestExplicitResumeBinding:
    def test_binding_resumes_on_the_first_attempt_with_a_verbatim_prompt(
        self, tmp_path,
    ):
        """The dialog talks to a specific session and asks a question — its
        prompt must not be replaced by a continuation directive."""
        runner = MockRunner()
        caller = LLMCaller(
            project_root=tmp_path,
            step_type="interjection_dialog",
            force_read_only=True,
            agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
            resume_binding={
                "agent_name": "primary",
                "runner_type": "claude-code",
                "provider_session_id": "sid-dialog",
                "session_cwd": str(tmp_path),
            },
        )
        caller._runner_cache["primary"] = runner
        caller._runner = runner
        caller.call("what are you doing?", json_mode="off")

        assert runner.calls[0]["args"][0] == "resume"
        assert runner.calls[0]["args"][1] == "sid-dialog"
        # The prompt travels verbatim (the read-only constraint block that
        # ``call()`` appends is the tool lock, not a continuation directive).
        assert runner.calls[0]["args"][2].startswith("what are you doing?")
        assert "Continuation" not in runner.calls[0]["args"][2]

    def test_binding_for_an_unknown_agent_falls_back_to_a_fresh_call(self, tmp_path):
        runner = MockRunner()
        caller = LLMCaller(
            project_root=tmp_path,
            step_type="interjection_dialog",
            agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
            resume_binding={
                "agent_name": "gone",
                "provider_session_id": "sid",
                "session_cwd": str(tmp_path),
            },
        )
        caller._runner_cache["primary"] = runner
        caller._runner = runner
        caller.call("hi", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"

    def test_the_fallback_prompt_replaces_the_session_relative_one(self, tmp_path):
        """Whoever answers instead of the recorded session holds none of its
        memory, so the caller's self-contained prompt is what it must get."""
        runner = MockRunner()
        caller = LLMCaller(
            project_root=tmp_path,
            step_type="interjection_dialog",
            agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
            resume_binding={
                "agent_name": "gone",
                "provider_session_id": "sid",
                "session_cwd": str(tmp_path),
            },
            resume_fallback_prompt="ASK-WITH-REBUILT-HISTORY",
        )
        caller._runner_cache["primary"] = runner
        caller._runner = runner
        caller.call("ASK-ALONE", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"
        assert runner.calls[0]["args"][1].startswith("ASK-WITH-REBUILT-HISTORY")

    def test_the_fallback_prompt_is_used_after_a_resume_failure(self, tmp_path):
        runner = MockRunner(
            results=[_Result(returncode=1), _Result(output=_ndjson("recovered"))]
        )
        caller = LLMCaller(
            project_root=tmp_path,
            step_type="interjection_dialog",
            agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
            resume_binding={
                "agent_name": "primary",
                "runner_type": "claude-code",
                "provider_session_id": "sid-dialog",
                "session_cwd": str(tmp_path),
            },
            resume_fallback_prompt="ASK-WITH-REBUILT-HISTORY",
        )
        caller._runner_cache["primary"] = runner
        caller._runner = runner
        caller.call("ASK-ALONE", json_mode="off")
        assert runner.calls[0]["args"][0] == "resume"
        assert runner.calls[1]["args"][0] == "fresh"
        assert runner.calls[1]["args"][1].startswith("ASK-WITH-REBUILT-HISTORY")


class TestResumeStrategyConfig:
    def test_default_is_native(self, tmp_path):
        caller = LLMCaller(
            project_root=tmp_path,
            agents=[{"name": "a", "type": "claude-code", "cmd": "claude"}],
        )
        assert caller.resume_strategy == "native"

    def test_project_config_can_force_rebuild(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "llm_caller:\n  resume_strategy: rebuild\n", encoding="utf-8"
        )
        caller = LLMCaller(
            project_root=tmp_path,
            agents=[{"name": "a", "type": "claude-code", "cmd": "claude"}],
        )
        assert caller.resume_strategy == "rebuild"

    def test_an_unknown_value_degrades_to_native(self, tmp_path):
        """A typo in a diagnostic switch must never abort a flow."""
        (tmp_path / "tianluo.yaml").write_text(
            "llm_caller:\n  resume_strategy: sideways\n", encoding="utf-8"
        )
        caller = LLMCaller(
            project_root=tmp_path,
            agents=[{"name": "a", "type": "claude-code", "cmd": "claude"}],
        )
        assert caller.resume_strategy == "native"


class TestInjectedInstructionCarry:
    """A dialog's instruction reaches the agent on BOTH continuation paths.

    The rebuild path gets it because ``call()`` appends it to the prompt; the
    native-resume path sends no rebuilt prompt at all, so it has to be carried
    across explicitly — otherwise the user's correction would be silently
    dropped on exactly the path the dialog exists to drive.
    """

    def test_the_instruction_reaches_a_native_resume_turn(self, tmp_path):
        from tianluo.engine import llm_caller as lc

        runner = MockRunner()
        _caller(tmp_path, runner).call("original", json_mode="off")

        lc.set_extra_prompt("use Postgres, not SQLite")
        retry_runner = MockRunner()
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "original", json_mode="off"
        )
        sent = retry_runner.calls[0]["args"][2]
        assert "use Postgres, not SQLite" in sent

    def test_the_dialog_framing_is_used_after_a_dialog(self, tmp_path):
        from tianluo.engine import llm_caller as lc

        runner = MockRunner()
        _caller(tmp_path, runner).call("original", json_mode="off")

        lc.mark_dialog_resume()
        retry_runner = MockRunner()
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "original", json_mode="off"
        )
        sent = retry_runner.calls[0]["args"][2]
        assert "interrupted" in sent.lower()
        assert "half-finished" in sent or "reconcile" in sent

    def test_a_stale_instruction_does_not_leak_into_a_later_call(self, tmp_path):
        from tianluo.engine import llm_caller as lc

        runner = MockRunner()
        _caller(tmp_path, runner).call("original", json_mode="off")

        lc.set_extra_prompt("one-off instruction")
        caller = _caller(tmp_path, MockRunner(), external_attempt=1)
        caller.call("original", json_mode="off")
        # Second call on the SAME caller: the transient injection is consumed,
        # so it must not reappear.
        caller.call("original", json_mode="off")
        sent = caller._runner_cache["primary"].calls[-1]["args"][2]
        assert "one-off instruction" not in sent

    def test_parallel_callers_each_receive_the_dialog_instruction(self, tmp_path):
        """A DAG IMPLEMENT step resumes several groups; the dialog conclusion is
        addressed to the STEP, so every group's caller must get it — the
        first-one-wins consume left the others resuming uninformed."""
        from tianluo.engine import llm_caller as lc

        for step_id in ("g1", "g2"):
            caller = LLMCaller(
                project_root=tmp_path, flow_id="f1", step_id=step_id,
                step_type="implement",
                agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
            )
            runner = MockRunner()
            caller._runner_cache["primary"] = runner
            caller._runner = runner
            caller.call("original", json_mode="off")

        lc.set_extra_prompt("use Postgres, not SQLite")
        lc.mark_dialog_resume()

        sent = []
        for step_id in ("g1", "g2"):
            caller = LLMCaller(
                project_root=tmp_path, flow_id="f1", step_id=step_id,
                step_type="implement", external_attempt=1,
                agents=[{"name": "primary", "type": "claude-code", "cmd": "claude"}],
            )
            runner = MockRunner()
            caller._runner_cache["primary"] = runner
            caller._runner = runner
            caller.call("original", json_mode="off")
            sent.append(runner.calls[0]["args"][2])

        assert all("use Postgres, not SQLite" in text for text in sent)
        assert all("interrupted" in text.lower() for text in sent)


class TestRebuildCarriesTheDecisionToo:
    """``rebuild`` changes only HOW the earlier conversation is supplied.

    It must never change WHAT the user decided: the dialog's instruction, the
    "your task description was replaced" notice and the interrupted framing all
    have to survive the fallback, because on that path the assembled prompt
    that carries them is not what gets sent.
    """

    def _rebuild_caller(self, tmp_path, runner, **kw):
        caller = _caller(tmp_path, runner, **kw)
        caller.resume_strategy = "rebuild"
        return caller

    def test_the_instruction_reaches_a_rebuilt_continuation(self, tmp_path):
        from tianluo.engine import llm_caller as lc

        self._rebuild_caller(tmp_path, MockRunner()).call("original", json_mode="off")

        lc.set_extra_prompt("use Postgres, not SQLite")
        lc.mark_dialog_resume()
        retry_runner = MockRunner()
        self._rebuild_caller(
            tmp_path, retry_runner, external_attempt=1
        ).call("original", json_mode="off")

        sent = retry_runner.calls[0]["args"][1]
        assert retry_runner.calls[0]["args"][0] == "fresh"  # rebuild, not resume
        assert "Continue the task from where you left off" in sent
        assert "use Postgres, not SQLite" in sent
        assert "interrupted" in sent.lower()

    def test_a_runner_without_native_resume_still_gets_the_instruction(
        self, tmp_path,
    ):
        from tianluo.engine import llm_caller as lc

        _caller(tmp_path, MockRunner(supports_resume=False)).call(
            "original", json_mode="off"
        )
        lc.set_extra_prompt("use Postgres, not SQLite")
        retry_runner = MockRunner(supports_resume=False)
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "original", json_mode="off"
        )
        assert "use Postgres, not SQLite" in retry_runner.calls[0]["args"][1]


class TestJsonRetryOnAContinuation:
    def test_the_json_fix_request_is_what_the_resumed_turn_says(self, tmp_path):
        """A continuation sends neither the original prompt nor the rebuilt
        json_prompt, so without the directive the agent would be told merely to
        "continue" and never learn its reply was not JSON."""
        prose = "sure thing, I will get right on it\n"
        runner = MockRunner(results=[_Result(output=prose), _Result(output=_ndjson('{"a": 1}'))])
        caller = _caller(tmp_path, runner, external_attempt=1)
        # Seed a session so the JSON retry plans a native resume.
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-json", session_cwd=str(tmp_path),
            resume_strategy="rebuild", agent_name="primary",
            runner_type="claude-code",
        )
        caller.call("give me json", json_mode="strict")

        second = runner.calls[1]
        assert second["args"][0] == "resume"
        sent = second["args"][2]
        assert "not in the required JSON format" in sent

    def test_the_directive_quotes_what_the_agent_actually_said(self):
        output = (
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "sure thing"}]},
            }) + "\n"
        )
        directive = LLMCaller._create_json_retry_directive(output)
        assert "not in the required JSON format" in directive
        assert "sure thing" in directive
        # It carries only the correction — the original prompt is already in
        # the session this turn is appended to.
        assert "give me json" not in directive

    def test_no_prompt_record_claims_an_unsent_json_prompt(self, tmp_path):
        prose = "prose, not json\n"
        runner = MockRunner(results=[_Result(output=prose), _Result(output=_ndjson('{"a": 1}'))])
        _caller(tmp_path, runner).call("give me json", json_mode="strict")

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        users = [m for m in session.messages if m.role == "user"]
        # Every recorded prompt belongs to an attempt that actually ran, and
        # therefore carries that attempt's strategy.
        assert all(m.resume_strategy for m in users)


class TestOutputContractRestatement:
    def test_the_injected_charter_tail_is_not_mistaken_for_the_contract(self):
        prompt = (
            "Do the work.\n\nOutput:\n```json\n{\"files_changed\": []}\n```\n"
            "\n\n## Project Charter\n"
            "The charter below is authoritative.\n"
            "```json\n{\"an\": \"example from the charter\"}\n```\n"
        )
        reminder = LLMCaller._output_contract_reminder(prompt, True)
        assert "files_changed" in reminder
        assert "example from the charter" not in reminder

    def test_a_prompt_without_injections_is_unchanged(self):
        prompt = "Do it.\n```json\n{\"summary\": \"...\"}\n```"
        reminder = LLMCaller._output_contract_reminder(prompt, True)
        assert "summary" in reminder


class TestBindingValidation:
    def test_a_binding_without_a_runner_type_is_refused(self, tmp_path):
        """A session id is only addressable together with the runner that owns
        it; a record that cannot say which runner that was must rebuild."""
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-legacy", session_cwd=str(tmp_path),
            agent_name="primary",
        )
        runner = MockRunner()
        _caller(tmp_path, runner, external_attempt=1).call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"

    def test_a_repointed_runner_type_is_refused(self, tmp_path):
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-claude", session_cwd=str(tmp_path),
            agent_name="primary", runner_type="claude-code",
        )
        runner = MockRunner()
        caller = LLMCaller(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", external_attempt=1,
            agents=[{"name": "primary", "type": "codex", "cmd": "codex"}],
        )
        caller._runner_cache["primary"] = runner
        caller._runner = runner
        caller.call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "fresh"

    def test_a_dialog_record_carries_the_full_binding(self, tmp_path):
        """A dialog turn is the newest session-bearing record, so it shadows the
        response record the resume would otherwise bind to."""
        chat_history.record_dialog_message(
            tmp_path, "f1", "s1", "implement", "user", "why?",
            agent_name="primary", provider_session_id="sid-dlg",
            runner_type="claude-code", session_cwd=str(tmp_path),
        )
        binding = chat_history.last_session_binding(tmp_path, "f1", "s1")
        assert binding["runner_type"] == "claude-code"
        assert binding["session_cwd"] == str(tmp_path)


class TestOnlyTheLatestAttemptsSessionIsResumable:
    def test_a_session_less_newer_attempt_forces_a_rebuild(self, tmp_path):
        """Agent A ran (session sA), rotation to B, B crashed before reporting a
        session. sA knows nothing about what happened since, so "continue where
        you left off" addressed at it would continue the wrong conversation."""
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sA", session_cwd=str(tmp_path),
            agent_name="a", runner_type="claude-code",
        )
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="b", runner_type="codex",
        )
        assert chat_history.last_session_binding(tmp_path, "f1", "s1") is None

    def test_an_interjection_between_attempts_is_not_an_attempt(self, tmp_path):
        """Commentary around the attempts neither establishes nor invalidates
        a binding."""
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sA", session_cwd=str(tmp_path),
            agent_name="a", runner_type="claude-code",
        )
        chat_history.record_user_interjection(
            tmp_path, "f1", "s1", "implement", "hold on",
        )
        binding = chat_history.last_session_binding(tmp_path, "f1", "s1")
        assert binding["provider_session_id"] == "sA"


class TestResumeFallbackBudget:
    def test_a_failed_resume_does_not_consume_a_retry_slot(self, tmp_path):
        """A resume the provider refuses says nothing about the agent's health;
        spending an attempt on it used to leave a healthy later agent untried."""
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-1", session_cwd=str(tmp_path),
            agent_name="a", runner_type="claude-code",
        )
        runners = {
            "a": MockRunner(results=[_Result(returncode=1), _Result(returncode=1)]),
            "b": MockRunner(results=[_Result(returncode=1)]),
            "c": MockRunner(results=[_Result(output=_ndjson("healthy"))]),
        }
        caller = LLMCaller(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", external_attempt=1, max_retries=3,
            retry_delay=0,
            agents=[
                {"name": "a", "type": "claude-code", "cmd": "claude"},
                {"name": "b", "type": "claude-code", "cmd": "claude"},
                {"name": "c", "type": "claude-code", "cmd": "claude"},
            ],
        )
        caller._runner_cache.update(runners)
        caller._runner = runners["a"]

        out = caller.call("do it", json_mode="off")

        assert "healthy" in out
        # agent a: one refused resume + one rebuilt attempt; b then c.
        assert runners["a"].calls[0]["args"][0] == "resume"
        assert runners["a"].calls[1]["args"][0] == "fresh"
        assert len(runners["b"].calls) == 1
        assert len(runners["c"].calls) == 1
        assert caller.native_resume_rejected is True

    def test_a_rejected_resume_reaches_the_agents_before_the_recorded_one(
        self, tmp_path
    ):
        """INVARIANT: a rejected resume leaves EVERY configured agent reachable.

        Planning a resume re-points the sequence at whichever agent owns the
        recorded session, and rotation is forward-only with no wrap. Staying on
        that index therefore excluded every agent before it — and a session
        recorded on the LAST agent left rotation exhausted on the first failure,
        spending the whole budget re-running that one agent while healthy ones
        got zero calls.
        """
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-c", session_cwd=str(tmp_path),
            agent_name="c", runner_type="claude-code",
        )
        runners = {
            # 'a' is healthy again; it must get the first rebuilt attempt.
            "a": MockRunner(results=[_Result(output=_ndjson("healthy-a"))]),
            "b": MockRunner(),
            # 'c' owns the recorded session and refuses to resume it.
            "c": MockRunner(results=[_Result(returncode=1)]),
        }
        caller = LLMCaller(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", external_attempt=1, max_retries=3,
            retry_delay=0,
            agents=[
                {"name": "a", "type": "claude-code", "cmd": "claude"},
                {"name": "b", "type": "claude-code", "cmd": "claude"},
                {"name": "c", "type": "claude-code", "cmd": "claude"},
            ],
        )
        caller._runner_cache.update(runners)

        out = caller.call("do it", json_mode="off")

        assert "healthy-a" in out
        assert [c["args"][0] for c in runners["c"].calls] == ["resume"]
        assert [c["args"][0] for c in runners["a"].calls] == ["fresh"]
        assert runners["b"].calls == []
        assert caller.native_resume_rejected is True

    def test_a_failure_after_a_successful_resume_is_not_a_rejection(
        self, tmp_path
    ):
        """Only a failure of the resume LAUNCH may count as a rejection.

        A resumed turn that answered and was recorded, and then raised
        downstream (the nested JSON retries exhausting their budget), says
        nothing about whether the provider accepted the session — classifying it
        as a rejection dropped a binding nobody refused and bought an extra
        attempt slot on top of ``max_retries``.
        """
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-1", session_cwd=str(tmp_path),
            agent_name="primary", runner_type="claude-code",
        )
        runner = MockRunner(
            results=[_Result(output=_ndjson("answered")) for _ in range(4)]
        )
        caller = _caller(
            tmp_path, runner, external_attempt=1, max_retries=1, retry_delay=0,
        )
        # Raised AFTER the response has been recorded — the same position the
        # nested JSON-retry LLMCallError unwinds from.
        def _boom(_output):
            raise RuntimeError("post-processing blew up")

        caller._extract_result_text = _boom

        with pytest.raises(LLMCallError):
            caller.call("do it", json_mode="off")

        # Exactly one attempt: no extra rebuild slot was granted...
        assert [c["args"][0] for c in runner.calls] == ["resume"]
        # ...and the session was never marked rejected — the provider answered.
        assert caller.native_resume_rejected is False

    def test_a_single_attempt_budget_still_rebuilds(self, tmp_path):
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            provider_session_id="sid-1", session_cwd=str(tmp_path),
            agent_name="primary", runner_type="claude-code",
        )
        runner = MockRunner(
            results=[_Result(returncode=1), _Result(output=_ndjson("rebuilt"))]
        )
        caller = _caller(
            tmp_path, runner, external_attempt=1, max_retries=1, retry_delay=0,
        )
        assert "rebuilt" in caller.call("do it", json_mode="off")
        assert runner.calls[0]["args"][0] == "resume"
        assert runner.calls[1]["args"][0] == "fresh"


class TestPerAttemptStrategyRecords:
    """Each attempt records the strategy IT used.

    ``TestRecordOrdering`` only ever covers a first call, which is always a
    rebuild — so the ``native`` half of the field, the one a troubleshooter
    actually reaches for ("did this retry continue the session or start over?"),
    was asserted nowhere. Both records of an attempt must agree, and the
    session id stamped on a resumed attempt must be the session it resumed.
    """

    def test_a_resumed_attempt_records_native_and_the_resumed_session(
        self, tmp_path
    ):
        _caller(tmp_path, MockRunner()).call("do it", json_mode="off")

        retry_runner = MockRunner(session_prefix="unused")
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "do it", json_mode="off"
        )
        assert retry_runner.calls[0]["args"][0] == "resume"

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        prompt = [m for m in session.messages if m.role == "user"][-1]
        response = [m for m in session.messages if m.role == "assistant"][-1]

        assert prompt.resume_strategy == "native"
        assert response.resume_strategy == "native"
        # The session id is the one CONTINUED, not a freshly minted one — a
        # resume must never mint an id, so "unused-1" must appear nowhere.
        assert prompt.provider_session_id == "sid-1"
        assert response.provider_session_id == "sid-1"

        # ...and the first call's own records still say rebuild, so the field
        # distinguishes the attempts rather than being rewritten wholesale.
        first_prompt = [m for m in session.messages if m.role == "user"][0]
        assert first_prompt.resume_strategy == "rebuild"

    def test_the_rebuilt_fallback_attempt_records_rebuild(self, tmp_path):
        """A resume that the provider refuses falls back to a rebuilt call on
        the same agent; that attempt is a rebuild and must say so."""
        _caller(tmp_path, MockRunner()).call("do it", json_mode="off")

        retry_runner = MockRunner(
            results=[_Result(returncode=1, output="session not found"),
                     _Result(output=_ndjson("recovered"))]
        )
        _caller(tmp_path, retry_runner, external_attempt=1).call(
            "do it", json_mode="off"
        )
        assert [c["args"][0] for c in retry_runner.calls] == ["resume", "fresh"]

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        response = [m for m in session.messages if m.role == "assistant"][-1]
        # The answer that came back is the REBUILT one, and the record it
        # carries has to name that strategy — otherwise a resume-fallback
        # incident reads in history as a clean native continuation.
        assert response.resume_strategy == "rebuild"

    def test_an_attempt_after_a_rotation_records_rebuild(self, tmp_path):
        """Rotation always rebuilds: the new agent has no session to continue,
        so its records must never inherit the previous agent's binding."""
        agents = [
            {"name": "primary", "type": "claude-code", "cmd": "claude"},
            {"name": "backup", "type": "claude-code", "cmd": "claude2"},
        ]

        first = LLMCaller(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agents=agents,
        )
        first._runner_cache["primary"] = MockRunner()
        first._runner = first._runner_cache["primary"]
        first.call("do it", json_mode="off")

        class _AlwaysInfra(MockRunner):
            def detect_infra_error(self, returncode, stdout, stderr):
                return InfraErrorType.USAGE_LIMIT

        broken = _AlwaysInfra(results=[_Result(returncode=1) for _ in range(6)])
        healthy = MockRunner(session_prefix="bk")

        retry = LLMCaller(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agents=agents, external_attempt=1,
        )
        retry._runner_cache["primary"] = broken
        retry._runner_cache["backup"] = healthy
        retry._runner = broken
        retry.call("do it", json_mode="off")

        # The healthy agent was reached, and it started over rather than
        # resuming a session that belongs to the agent that just failed.
        assert healthy.calls, "rotation never reached the second agent"
        assert healthy.calls[0]["args"][0] == "fresh"

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        response = [m for m in session.messages if m.role == "assistant"][-1]
        assert response.agent_name == "backup"
        assert response.resume_strategy == "rebuild"
        assert response.provider_session_id != "sid-1"


class TestTwoPhaseExtractionNeverBindsTheResume:
    """A two-phase step's Phase-2 extraction opens its own provider session
    whose whole content is "re-express this text as JSON". Being the step's
    NEWEST session-bearing record, it used to win ``last_session_binding`` — so
    the next retry natively resumed the re-formatter and the agent was told to
    "continue from where you left off" in a session that never did the task."""

    @staticmethod
    def _json_ndjson(payload):
        """A stream whose assistant turn carries the JSON the extractor parses."""
        return (
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": payload}]},
            })
            + "\n"
            + json.dumps({"type": "result", "subtype": "success", "result": payload})
            + "\n"
        )

    def _two_phase_caller(self, tmp_path, runner, **kwargs):
        caller = _caller(tmp_path, runner, **kwargs)
        return caller

    def test_phase2_records_are_tagged_and_skipped_by_the_binding(self, tmp_path):
        # Phase 1 answers in prose (no JSON), Phase 2 extracts it.
        runner = MockRunner(results=[
            _Result(output=_ndjson("I implemented the change in prose.")),
            _Result(output=self._json_ndjson('{"summary": "done"}')),
        ])
        caller = self._two_phase_caller(tmp_path, runner)
        caller.call("do it", json_mode="two_phase", required_keys=["summary"])

        session = chat_history.get_step_history(tmp_path, "f1", "s1")
        kinds = [(m.role, getattr(m, "kind", ""), m.provider_session_id)
                 for m in session.messages]
        # Phase 1 records are plain attempts; Phase 2 records are extraction.
        assert kinds[0][1] == "" and kinds[1][1] == ""
        assert kinds[-1][1] == "extraction"
        assert kinds[-2][1] == "extraction"

        binding = chat_history.last_session_binding(tmp_path, "f1", "s1")
        assert binding is not None
        # The WORKING session, not the extraction one.
        assert binding["provider_session_id"] == "sid-1"

    def test_a_retry_after_a_completed_phase2_resumes_phase1(self, tmp_path):
        runner = MockRunner(results=[
            _Result(output=_ndjson("prose only")),
            _Result(output=self._json_ndjson('{"summary": "done"}')),
        ])
        self._two_phase_caller(tmp_path, runner).call(
            "do it", json_mode="two_phase", required_keys=["summary"],
        )

        # The step handler failed after the call; the user picks Retry.
        retry_runner = MockRunner()
        retry = _caller(tmp_path, retry_runner, external_attempt=1)
        retry.call("do it", json_mode="off")

        assert retry_runner.calls[0]["args"][0] == "resume"
        assert retry_runner.calls[0]["args"][1] == "sid-1"

    def test_an_interruption_during_phase2_still_resumes_phase1(self, tmp_path):
        runner = MockRunner(results=[
            _Result(output=_ndjson("prose only")),
            _Result(returncode=1, output=_ndjson(""), interrupted=True),
        ])
        caller = self._two_phase_caller(tmp_path, runner)
        with pytest.raises(KeyboardInterrupt):
            caller.call("do it", json_mode="two_phase", required_keys=["summary"])

        binding = chat_history.last_session_binding(tmp_path, "f1", "s1")
        assert binding is not None
        assert binding["provider_session_id"] == "sid-1"


class TestAProviderThatMintsItsOwnSessionId:
    """codex has no caller-chosen ``--session-id``: its thread id first exists
    when the stream announces it. The binding must still be durable BEFORE the
    response record lands, or a parent killed mid-call leaves a live provider
    thread that no record names — and every later retry rebuilds from history
    instead of resuming it."""

    @staticmethod
    def _history_lines(tmp_path):
        path = chat_history._history_file(tmp_path, "f1", "s1")
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_the_stream_announcement_is_written_the_moment_it_arrives(
        self, tmp_path,
    ):
        from tianluo.engine.llm_caller import StreamJSONTracker

        tracker = StreamJSONTracker(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agent_name="primary", attempt=0,
            runner_type="codex", provider="openai",
        )
        tracker.process_line(
            json.dumps({"type": "init", "provider_session_id": "thread-42"})
        )

        announced = [
            r for r in self._history_lines(tmp_path)
            if r.get("type") == "stream_progress"
            and r.get("provider_session_id") == "thread-42"
        ]
        assert announced, "the streamed session id was never recorded"

    def test_one_record_carries_a_session_and_model_from_the_same_line(
        self, tmp_path,
    ):
        """An init line carrying both must not cost two identity records."""
        from tianluo.engine.llm_caller import StreamJSONTracker

        tracker = StreamJSONTracker(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agent_name="primary", attempt=0,
        )
        tracker.process_line(json.dumps({
            "type": "init", "provider_session_id": "thread-7", "model": "gpt-x",
        }))

        records = [
            r for r in self._history_lines(tmp_path)
            if r.get("type") == "stream_progress"
        ]
        assert len(records) == 1
        assert records[0]["provider_session_id"] == "thread-7"
        assert records[0]["model_name"] == "gpt-x"

    def test_a_killed_attempt_is_still_addressable(self, tmp_path):
        """Prompt record (no id, written pre-spawn) + the stream's announcement,
        and nothing else: the shape a hard-killed codex attempt leaves."""
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="primary", runner_type="codex",
            session_cwd=str(tmp_path),
        )
        chat_history.record_stream_progress(
            tmp_path, "f1", "s1", "implement", "", None, 0,
            agent_name="primary", provider_session_id="thread-42",
        )

        binding = chat_history.last_session_binding(tmp_path, "f1", "s1")
        assert binding is not None
        assert binding["provider_session_id"] == "thread-42"
        assert binding["runner_type"] == "codex"
        assert binding["session_cwd"] == str(tmp_path)

    def test_an_earlier_attempts_announcement_does_not_bind_a_later_one(
        self, tmp_path,
    ):
        """The invariant is unchanged: only the MOST RECENT attempt's session
        may be resumed. An announcement predating the newest attempt record
        belongs to the attempt before it."""
        chat_history.record_stream_progress(
            tmp_path, "f1", "s1", "implement", "", None, 0,
            timestamp="2000-01-01T00:00:00",
            agent_name="primary", provider_session_id="thread-old",
        )
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="primary", runner_type="codex",
        )

        assert chat_history.last_session_binding(tmp_path, "f1", "s1") is None

    def test_another_agents_announcement_does_not_bind(self, tmp_path):
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="backup", runner_type="codex",
        )
        chat_history.record_stream_progress(
            tmp_path, "f1", "s1", "implement", "", None, 0,
            agent_name="primary", provider_session_id="thread-other",
        )

        assert chat_history.last_session_binding(tmp_path, "f1", "s1") is None

    def test_an_announcement_past_the_next_record_does_not_bind(self, tmp_path):
        """A later fix-iteration's attempt is filtered out of the scan, but its
        announcement still sits behind the record this scan settled on. Binding
        to it would resume work this iteration never did."""
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="primary", runner_type="codex", fix_iteration=1,
        )
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="primary", runner_type="codex", fix_iteration=2,
        )
        chat_history.record_stream_progress(
            tmp_path, "f1", "s1", "implement", "", None, 0,
            agent_name="primary", provider_session_id="thread-fi2",
        )

        assert chat_history.last_session_binding(
            tmp_path, "f1", "s1", fix_iteration=1,
        ) is None


class TestBothRecordsOfAnAttemptNameTheSameSession:
    """INVARIANT: a capture-only runner's prompt record is written pre-spawn
    with no id, but once the stream announces the real thread id BOTH records
    of that attempt must point at it — the prompt record is rewritten in place
    rather than left inconsistent."""

    @staticmethod
    def _history_lines(tmp_path):
        path = chat_history._history_file(tmp_path, "f1", "s1")
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _prompt_records(self, tmp_path):
        return [
            r for r in self._history_lines(tmp_path)
            if not r.get("type") and r.get("role") == "user"
        ]

    def test_the_prompt_record_is_backfilled_when_the_stream_announces(
        self, tmp_path,
    ):
        from tianluo.engine.llm_caller import StreamJSONTracker

        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="primary", runner_type="codex",
            session_cwd=str(tmp_path),
        )
        assert not self._prompt_records(tmp_path)[0].get("provider_session_id")

        tracker = StreamJSONTracker(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agent_name="primary", attempt=0,
            runner_type="codex", provider="openai",
        )
        tracker.process_line(
            json.dumps({"type": "init", "provider_session_id": "thread-42"})
        )

        records = self._prompt_records(tmp_path)
        assert len(records) == 1, "backfill must rewrite, not append"
        assert records[0]["provider_session_id"] == "thread-42"
        # And the response record of the same attempt agrees.
        chat_history.record_response(
            tmp_path, "f1", "s1", "implement",
            json.dumps({"type": "result", "result": "ok"}), 0,
            agent_name="primary", provider_session_id="thread-42",
        )
        responses = [
            r for r in self._history_lines(tmp_path)
            if not r.get("type") and r.get("role") == "assistant"
        ]
        assert responses[0]["provider_session_id"] == "thread-42"

    def test_only_the_matching_attempt_is_touched(self, tmp_path):
        from tianluo.engine.llm_caller import StreamJSONTracker

        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "old", 0,
            agent_name="primary", runner_type="codex",
        )
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "new", 1,
            agent_name="primary", runner_type="codex",
        )

        tracker = StreamJSONTracker(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agent_name="primary", attempt=1,
            runner_type="codex", provider="openai",
        )
        tracker.process_line(
            json.dumps({"type": "init", "provider_session_id": "thread-9"})
        )

        by_attempt = {r["attempt"]: r for r in self._prompt_records(tmp_path)}
        assert not by_attempt[0].get("provider_session_id")
        assert by_attempt[1]["provider_session_id"] == "thread-9"

    def test_a_preallocated_id_is_left_alone(self, tmp_path):
        """The Claude adapters name their session pre-spawn; a stream echoing
        the same id must not rewrite anything."""
        from tianluo.engine.llm_caller import StreamJSONTracker

        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0,
            agent_name="primary", runner_type="claude-code",
            provider_session_id="sid-1",
        )
        before = chat_history._history_file(tmp_path, "f1", "s1").read_text(
            encoding="utf-8"
        )

        tracker = StreamJSONTracker(
            project_root=tmp_path, flow_id="f1", step_id="s1",
            step_type="implement", agent_name="primary", attempt=0,
            runner_type="claude-code", provider_session_id="sid-1",
        )
        tracker.process_line(
            json.dumps({"type": "init", "provider_session_id": "sid-1"})
        )

        assert chat_history._history_file(tmp_path, "f1", "s1").read_text(
            encoding="utf-8"
        ) == before

    def test_backfill_is_a_noop_without_a_matching_record(self, tmp_path):
        chat_history.record_prompt(
            tmp_path, "f1", "s1", "implement", "p", 0, agent_name="other",
        )
        assert not chat_history.backfill_prompt_session_id(
            tmp_path, "f1", "s1", attempt=0, session_id="x",
            agent_name="primary",
        )


class TestACallerSuppliedOnOutputStillBindsTheSession:
    """A caller that renders the stream itself (``on_output``) bypasses
    StreamJSONTracker — the only place a capture-only provider's announcement
    was ever observed. Without the relay the prompt record keeps its empty
    pre-spawn id while the response record names the real thread, splitting one
    attempt across two identities; and a parent killed before the response lands
    leaves the live thread named nowhere at all."""

    @staticmethod
    def _history_lines(tmp_path):
        path = chat_history._history_file(tmp_path, "f1", "s1")
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _records(self, tmp_path, role):
        return [
            r for r in self._history_lines(tmp_path)
            if not r.get("type") and r.get("role") == role
        ]

    class _CaptureOnlyRunner(MockRunner):
        """codex-shaped: no pre-allocated id; the thread announces itself."""

        def __init__(self, thread_id="thread-77", **kw):
            super().__init__(**kw)
            self.thread_id = thread_id

        def get_startup_metadata(self, env=None):
            self.startup_calls += 1
            return RunnerStartupMetadata(provider="openai", model="gpt-x")

        def run_with_monitor(self, args=None, cwd=None, on_output=None, **kwargs):
            self.calls.append({"args": list(args or []), "cwd": cwd})
            lines = [
                json.dumps({"type": "init", "provider_session_id": self.thread_id}),
                json.dumps({"type": "result", "subtype": "success", "result": "ok"}),
            ]
            if on_output is not None:
                for line in lines:
                    on_output(line)
            return _Result(output="\n".join(lines) + "\n")

    def _caller_for(self, tmp_path, runner):
        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="f1",
            step_id="s1",
            step_type="implement",
            agents=[{"name": "primary", "type": "codex", "cmd": "codex"}],
        )
        caller._runner_cache["primary"] = runner
        caller._runner = runner
        return caller

    def test_the_prompt_record_is_backfilled_on_the_on_output_path(self, tmp_path):
        runner = self._CaptureOnlyRunner()
        caller = self._caller_for(tmp_path, runner)

        caller.call("do the thing", require_json=False, on_output=lambda _l: None)

        prompts = self._records(tmp_path, "user")
        assert len(prompts) == 1, "backfill must rewrite, not append"
        assert prompts[0]["provider_session_id"] == "thread-77"

    def test_both_records_of_the_attempt_name_the_same_session(self, tmp_path):
        runner = self._CaptureOnlyRunner()
        caller = self._caller_for(tmp_path, runner)

        caller.call("do the thing", require_json=False, on_output=lambda _l: None)

        prompt = self._records(tmp_path, "user")[0]
        response = self._records(tmp_path, "assistant")[0]
        assert prompt["provider_session_id"] == response["provider_session_id"]
        assert response["provider_session_id"] == "thread-77"

    def test_the_callers_own_on_output_still_receives_every_line(self, tmp_path):
        seen = []
        runner = self._CaptureOnlyRunner()
        caller = self._caller_for(tmp_path, runner)

        caller.call("do the thing", require_json=False, on_output=seen.append)

        assert len(seen) == 2
        assert json.loads(seen[0])["provider_session_id"] == "thread-77"

    def test_a_captured_session_survives_a_crash_after_the_stream(self, tmp_path):
        """The response record is written on the failure path too, and it must
        name the thread the stream announced rather than the empty seed."""

        class _CrashingRunner(TestACallerSuppliedOnOutputStillBindsTheSession
                              ._CaptureOnlyRunner):
            def run_with_monitor(self, args=None, cwd=None, on_output=None, **kwargs):
                if on_output is not None:
                    on_output(
                        json.dumps(
                            {"type": "init", "provider_session_id": self.thread_id}
                        )
                    )
                raise RuntimeError("subprocess died mid-stream")

        runner = _CrashingRunner()
        caller = self._caller_for(tmp_path, runner)
        caller.max_retries = 1
        with pytest.raises(LLMCallError):
            caller.call("do the thing", require_json=False, on_output=lambda _l: None)

        prompt = self._records(tmp_path, "user")[0]
        response = self._records(tmp_path, "assistant")[0]
        assert prompt["provider_session_id"] == "thread-77"
        assert response["provider_session_id"] == "thread-77"

    def test_a_preallocated_id_is_not_disturbed_by_the_relay(self, tmp_path):
        """A pre-allocating adapter already named the session pre-spawn; the
        relay must leave that binding — and the file — alone."""
        runner = MockRunner()
        caller = _caller(tmp_path, runner)

        caller.call("do the thing", require_json=False, on_output=lambda _l: None)

        prompts = self._records(tmp_path, "user")
        assert len(prompts) == 1
        assert prompts[0]["provider_session_id"] == "sid-1"
