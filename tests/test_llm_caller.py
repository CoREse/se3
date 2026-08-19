"""Tests for LLMCaller._call_two_phase required_keys validation.

Tests cover:
- Fast path: valid JSON with matching required_keys → skip Phase 2
- Fast path: valid JSON missing required_keys → fallback to Phase 2
- Fast path: required_keys=None → same as before (no key check)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.engine.llm_caller import LLMCallError, LLMCaller
from tianluo.agent_runner import AgentInvocationIntent, RunnerStartupMetadata
from tianluo.claude_runner import ClaudeCodeRunner
from tianluo.engine.token_usage import accumulate_step_usage
from tianluo.usage import UsageStatus


def _make_caller(**kwargs) -> LLMCaller:
    """Create an LLMCaller with defaults suitable for testing."""
    defaults = dict(
        project_root=Path("/tmp/test"),
        max_retries=1,
        flow_id="test-flow",
        step_id="test-step",
        step_type="self_check",
        agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
    )
    defaults.update(kwargs)
    return LLMCaller(**defaults)


class TestCallTwoPhaseRequiredKeys:
    """Test required_keys validation in the two-phase fast path."""

    @patch.object(LLMCaller, "_call_with_retry")
    @patch.object(LLMCaller, "_get_phase1_cache_path", return_value=None)
    def test_fast_path_with_matching_required_keys(self, _mock_cache, mock_retry):
        """When Phase 1 returns valid JSON containing required_keys, skip Phase 2."""
        phase1_json = json.dumps({"issues": [], "summary": "All good"})
        mock_retry.return_value = phase1_json

        caller = _make_caller()
        result = caller._call_two_phase(
            prompt="test",
            timeout=None,
            context_files=None,
            on_output=None,
            json_schema_hint=None,
            required_keys=["issues"],
        )

        parsed = json.loads(result)
        assert "issues" in parsed
        # _call_with_retry called once (Phase 1 only, no Phase 2)
        assert mock_retry.call_count == 1

    @patch.object(LLMCaller, "_call_with_retry")
    @patch.object(LLMCaller, "_get_phase1_cache_path", return_value=None)
    def test_fast_path_missing_required_keys_falls_back_to_phase2(
        self, _mock_cache, mock_retry
    ):
        """When Phase 1 JSON is valid but missing required_keys, fall back to
        Phase 2 — and Phase 2 now runs through THIS caller's own
        ``_call_with_retry`` (a second sequence on the same agent chain),
        NOT a fresh ``JSONExtractor``-spawned ``LLMCaller``."""
        # Phase 1 returns valid JSON but without the "issues" key; Phase 2's
        # extraction sequence (the second _call_with_retry) then returns the
        # complete JSON.
        phase1_json = json.dumps({"summary": "All good"})
        phase2_json = json.dumps({"issues": [], "summary": "All good"})
        mock_retry.side_effect = [phase1_json, phase2_json]

        caller = _make_caller()
        result = caller._call_two_phase(
            prompt="test",
            timeout=None,
            context_files=None,
            on_output=None,
            json_schema_hint=None,
            required_keys=["issues"],
        )

        parsed = json.loads(result)
        assert "issues" in parsed
        # Two _call_with_retry sequences: Phase 1 generation + Phase 2 extraction
        # on the same caller (no separate JSONExtractor LLMCaller involved).
        assert mock_retry.call_count == 2

    @patch.object(LLMCaller, "_call_with_retry")
    @patch.object(LLMCaller, "_get_phase1_cache_path", return_value=None)
    def test_fast_path_with_none_required_keys(self, _mock_cache, mock_retry):
        """When required_keys=None, fast path accepts any valid JSON (backward compat)."""
        phase1_json = json.dumps({"summary": "All good"})
        mock_retry.return_value = phase1_json

        caller = _make_caller()
        result = caller._call_two_phase(
            prompt="test",
            timeout=None,
            context_files=None,
            on_output=None,
            json_schema_hint=None,
            required_keys=None,
        )

        parsed = json.loads(result)
        assert "summary" in parsed
        # Phase 1 only — no Phase 2 needed
        assert mock_retry.call_count == 1

    def test_phase1_and_json_phase2_are_distinct_usage_calls(self, tmp_path):
        outputs = [
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {"type": "text", "text": "not json yet"}
                                ]
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "usage_event_id": "phase-1-usage",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ),
                ]
            ),
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {"type": "text", "text": '{"issues": []}'}
                                ]
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "usage_event_id": "phase-2-usage",
                            "usage": {"input_tokens": 6, "output_tokens": 1},
                        }
                    ),
                ]
            ),
        ]

        class TwoPhaseRunner:
            def build_call_args(self, prompt, read_only, **kwargs):
                return [prompt]

            def run_with_monitor(self, args, **kwargs):
                output = outputs.pop(0)
                for line in output.splitlines():
                    kwargs["on_output"](line)
                return SimpleNamespace(
                    success=True,
                    output=output,
                    interrupted=False,
                    returncode=0,
                    cmd_used="two-phase",
                )

        caller = _make_caller(project_root=tmp_path, step_type="analyze")
        runner = TwoPhaseRunner()
        with patch.object(
            caller, "_get_current_runner", return_value=runner
        ), patch.object(caller, "_record_prompt"), patch.object(
            caller, "_record_response"
        ), accumulate_step_usage() as totals:
            result = caller.call(
                "analyze",
                json_mode="two_phase",
                required_keys=["issues"],
            )

        assert json.loads(result) == {"issues": []}
        assert len(totals.usage_records) == 2
        assert [record.usage_event_ids for record in totals.usage_records] == [
            ["phase-1-usage"],
            ["phase-2-usage"],
        ]
        assert totals.input_tokens == 16
        assert totals.output_tokens == 3


class TestCallPassesRequiredKeys:
    """Test that call() passes required_keys to _call_two_phase()."""

    @patch.object(LLMCaller, "_call_two_phase")
    def test_call_passes_required_keys_to_two_phase(self, mock_two_phase):
        """call() with json_mode='two_phase' passes required_keys through."""
        mock_two_phase.return_value = '{"issues": []}'

        caller = _make_caller()
        caller.call(
            prompt="test",
            json_mode="two_phase",
            required_keys=["issues"],
        )

        call_kwargs = mock_two_phase.call_args.kwargs
        assert call_kwargs["required_keys"] == ["issues"]


class TestCreateRunnerForwardsProjectRoot:
    """Regression: _create_runner must forward project_root to ClaudeCodeRunner
    so that ``claude_subprocess.setting_sources`` from the project's
    ``tianluo.yaml`` actually reaches the subprocess. Without forwarding, the
    Runner falls back to the built-in default ``["user"]`` regardless of
    user config — silently neutralising the documented escape hatch.
    """

    def test_project_root_forwarded_so_yaml_setting_sources_takes_effect(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "claude_subprocess:\n  setting_sources: [user, project]\n",
            encoding="utf-8",
        )
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(
                project_root=tmp_path,
                max_retries=1,
                flow_id="test-flow",
                step_id="test-step",
                step_type="implement",
                agents=[{"name": "test", "type": "claude-code", "cmd": "claude-a"}],
            )
        runner = caller._get_current_runner()
        assert runner.setting_sources == ["user", "project"]

    def test_default_setting_sources_when_no_yaml(self, tmp_path):
        """When no project YAML is present, runner falls back to ['user']."""
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(
                project_root=tmp_path,
                max_retries=1,
                flow_id="test-flow",
                step_id="test-step",
                step_type="implement",
                agents=[{"name": "test", "type": "claude-code", "cmd": "claude-a"}],
            )
        runner = caller._get_current_runner()
        assert runner.setting_sources == ["user"]


class _FakeResult:
    """Minimal stand-in for the runner result accessed by _call_with_retry."""

    def __init__(self, output: str = "") -> None:
        self.success = True
        self.output = output
        self.interrupted = False


class _ArgsCapturingRunner:
    """Fake runner that records the args passed to run_with_monitor.

    Implements ``build_call_args`` so the new intent-delegation path works.
    The implementation mirrors ``ClaudeCodeRunner.build_call_args`` so the
    existing end-to-end assertions remain valid.
    """

    def __init__(self) -> None:
        self.captured_args = None

    def build_call_args(
        self, prompt, read_only, context_files=None
    ):
        args = ["--output-format", "stream-json", "--verbose", "-p", prompt]
        if read_only:
            args += [
                "--disallowedTools",
                "Write",
                "Edit",
                "NotebookEdit",
                "AskUserQuestion",
            ]
        if context_files:
            for f in context_files:
                if f.exists():
                    args.extend(["--file", str(f)])
        return args

    def run_with_monitor(self, args, **kwargs):
        self.captured_args = list(args)
        return _FakeResult(output="")


def test_direct_intent_keeps_legacy_runner_on_plain_autonomous_prompt():
    """A pre-intent third-party runner remains a valid direct executor."""
    caller = _make_caller(step_type="implement")
    runner = _ArgsCapturingRunner()
    with patch.object(LLMCaller, "_get_current_runner", return_value=runner), \
         patch.object(LLMCaller, "_record_prompt"), \
         patch.object(LLMCaller, "_record_response"):
        caller.call(
            "implement all requirements",
            json_mode="off",
            invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
        )

    prompt = runner.captured_args[runner.captured_args.index("-p") + 1]
    assert prompt.startswith("implement all requirements")
    assert not prompt.startswith("/goal")


class TestReadOnlyToolDisallowList:
    """Tool-layer enforcement: read-only steps append --disallowedTools for the
    write tools; writable steps (implement / commit) do not."""

    def _run_and_capture_args(self, step_type: str):
        caller = _make_caller(step_type=step_type)
        runner = _ArgsCapturingRunner()
        with patch.object(LLMCaller, "_get_current_runner", return_value=runner), \
             patch.object(LLMCaller, "_record_prompt"), \
             patch.object(LLMCaller, "_record_response"):
            caller.call("do the thing", json_mode="off")
        assert runner.captured_args is not None
        return runner.captured_args

    def test_code_index_args_contain_disallowed_write_tools(self):
        """code_index is an internal pure-data sub-agent: SE3 writes the map."""
        args = self._run_and_capture_args("code_index")
        assert "--disallowedTools" in args
        for tool in ("Write", "Edit", "NotebookEdit", "AskUserQuestion"):
            assert tool in args, f"{tool} should be in disallowed list"

    def test_migrate_args_contain_disallowed_write_tools(self):
        args = self._run_and_capture_args("migrate")
        assert "--disallowedTools" in args
        assert "Write" in args and "Edit" in args

    def test_read_tools_not_disallowed(self):
        """Read/Grep/Glob/Bash must remain available (not in the disallow list)."""
        args = self._run_and_capture_args("code_index")
        di = args.index("--disallowedTools")
        disallowed = args[di + 1:]
        for tool in ("Read", "Grep", "Glob", "Bash"):
            assert tool not in disallowed, f"{tool} must not be disallowed"

    def test_unknown_step_args_have_no_disallowed_tools(self):
        """An unregistered step type is not read-only and gets no tool lock."""
        args = self._run_and_capture_args("not_a_registered_step")
        assert "--disallowedTools" not in args

    def test_implement_args_have_no_disallowed_tools(self):
        args = self._run_and_capture_args("implement")
        assert "--disallowedTools" not in args

    def test_update_spec_args_have_no_disallowed_tools(self):
        args = self._run_and_capture_args("update_spec")
        assert "--disallowedTools" not in args

    def test_analyze_read_only_step_has_disallowed_tools(self):
        """STEP_POOL read-only steps also get tool-layer enforcement."""
        args = self._run_and_capture_args("analyze")
        assert "--disallowedTools" in args


class TestForceReadOnlyOverride:
    """Call-level read-only override: a step whose registry read_only is False
    (its handler writes files) can still hold a single LLM sub-call read-only by
    constructing LLMCaller(force_read_only=True). Both enforcement points must
    fire — the prompt READ-ONLY injection and the runner --disallowedTools lock —
    without mutating is_step_read_only. charter_freshness is the motivating step
    (read_only flipped to False for its in-handler charter write)."""

    def _run_and_capture_args(self, step_type: str, force_read_only: bool):
        caller = _make_caller(step_type=step_type, force_read_only=force_read_only)
        runner = _ArgsCapturingRunner()
        with patch.object(LLMCaller, "_get_current_runner", return_value=runner), \
             patch.object(LLMCaller, "_record_prompt"), \
             patch.object(LLMCaller, "_record_response"):
            caller.call("do the thing", json_mode="off")
        assert runner.captured_args is not None
        return runner.captured_args

    def test_baseline_charter_freshness_is_writable(self):
        """Sanity: with the read_only flip, charter_freshness alone (no force)
        gets NO tool-level lock — the handler is the writer."""
        from tianluo.engine.context_builder import is_step_read_only

        assert is_step_read_only("charter_freshness") is False
        args = self._run_and_capture_args("charter_freshness", force_read_only=False)
        assert "--disallowedTools" not in args

    def test_force_read_only_adds_runner_disallowed_tools(self):
        """Enforcement point 1: runner receives read_only=True even though the
        step's registry read_only is False."""
        args = self._run_and_capture_args("charter_freshness", force_read_only=True)
        assert "--disallowedTools" in args
        for tool in ("Write", "Edit", "NotebookEdit"):
            assert tool in args

    @patch.object(LLMCaller, "_call_with_retry")
    def test_force_read_only_injects_prompt_constraint(self, mock_retry):
        """Enforcement point 2: the prompt gains the READ-ONLY STEP CONSTRAINT
        block when force_read_only=True on an otherwise-writable step."""
        mock_retry.return_value = "ok"
        _make_caller(
            step_type="charter_freshness", force_read_only=True
        ).call("propose charter patch", json_mode="off")
        assert "READ-ONLY STEP CONSTRAINT" in mock_retry.call_args[1]["prompt"]

    @patch.object(LLMCaller, "_call_with_retry")
    def test_default_no_force_no_prompt_constraint(self, mock_retry):
        """Default force_read_only=False leaves a writable step's prompt clean —
        behavior identical to before the override existed."""
        mock_retry.return_value = "ok"
        _make_caller(
            step_type="charter_freshness", force_read_only=False
        ).call("propose charter patch", json_mode="off")
        assert "READ-ONLY STEP CONSTRAINT" not in mock_retry.call_args[1]["prompt"]


def _streaming_side_effect(outputs, captured_args):
    """run_with_monitor side effect: replay canned NDJSON outputs in order."""
    pending = list(outputs)

    def run(args, **kwargs):
        captured_args.append(list(args))
        output = pending.pop(0)
        on_output = kwargs.get("on_output")
        if on_output:
            for line in output.splitlines():
                on_output(line)
        return SimpleNamespace(
            success=True,
            output=output,
            interrupted=False,
            returncode=0,
            cmd_used="claude",
        )

    return run


class TestDirectIntentPlainExecution:
    """DIRECT_IMPLEMENTATION runs one plain print-mode call — /goal retired.

    Claude Code's /goal goal-condition argument is hard-capped at 4000
    characters, far below any real implement prompt, so the runner no longer
    translates the intent into anything: same argv as DEFAULT, exactly one
    subprocess, no fallback machinery.
    """

    def test_direct_intent_runs_single_plain_call(self, tmp_path):
        caller = _make_caller(
            project_root=tmp_path,
            step_type="implement",
            max_retries=1,
        )
        runner = ClaudeCodeRunner(
            project_root=tmp_path,
            command={"cmd": "claude", "priority": 0},
        )
        completed = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "implementation completed",
        })
        captured_args = []
        runner.run_with_monitor = MagicMock(
            side_effect=_streaming_side_effect([completed], captured_args)
        )

        with patch.object(caller, "_get_current_runner", return_value=runner), \
             patch.object(caller, "_record_prompt"), \
             patch.object(caller, "_record_response"), \
             patch("tianluo.engine.llm_caller.add_call_usage"):
            result = caller.call(
                "implement all requirements",
                json_mode="off",
                invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
            )

        assert "implementation completed" in result
        assert len(captured_args) == 1
        prompt_arg = captured_args[0][captured_args[0].index("-p") + 1]
        assert prompt_arg.startswith("implement all requirements")
        assert not prompt_arg.startswith("/goal")


class TestDirectCallerRotation:
    """Direct intent keeps LLMCaller's existing workspace handoff semantics."""

    _streaming_side_effect = staticmethod(_streaming_side_effect)

    def test_successor_continues_partial_workspace_with_retry_context(
        self, tmp_path,
    ):
        calls = []

        class Runner:
            def __init__(self, name, fails=False):
                self.name = name
                self.fails = fails

            def build_call_args(
                self,
                prompt,
                read_only,
                context_files=None,
                invocation_intent=AgentInvocationIntent.DEFAULT,
            ):
                calls.append((self.name, prompt, invocation_intent))
                return [prompt]

            def run_with_monitor(self, args, **kwargs):
                if self.fails:
                    (tmp_path / "partial-work.txt").write_text(
                        "kept for successor", encoding="utf-8",
                    )
                    return SimpleNamespace(
                        success=False,
                        output="infrastructure failure",
                        interrupted=False,
                        returncode=1,
                        cmd_used=self.name,
                    )
                assert (tmp_path / "partial-work.txt").read_text(
                    encoding="utf-8"
                ) == "kept for successor"
                output = json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "continued and completed",
                })
                if kwargs.get("on_output"):
                    kwargs["on_output"](output)
                return SimpleNamespace(
                    success=True,
                    output=output,
                    interrupted=False,
                    returncode=0,
                    cmd_used=self.name,
                )

            def detect_infra_error(self, returncode, stdout, stderr):
                from tianluo.agent_runner import InfraErrorType

                return InfraErrorType.STARTUP_FAILURE

        caller = LLMCaller(
            project_root=tmp_path,
            max_retries=2,
            retry_delay=0,
            flow_id="rotation-flow",
            step_id="implement-step",
            step_type="implement",
            agents=[
                {"name": "first", "type": "claude-code", "cmd": "first"},
                {"name": "second", "type": "codex", "cmd": "second"},
            ],
        )
        caller._runner_cache = {
            "first": Runner("first", fails=True),
            "second": Runner("second"),
        }

        with patch.object(caller, "_record_prompt"), \
             patch.object(caller, "_record_response"), \
             patch.object(
                 caller,
                 "_get_retry_context",
                 return_value="Prior caller failed after partial workspace work.",
             ):
            result = caller.call(
                "implement the entire requirement",
                json_mode="off",
                invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
            )

        assert "continued and completed" in result
        assert [entry[0] for entry in calls] == ["first", "second"]
        assert all(
            entry[2] == AgentInvocationIntent.DIRECT_IMPLEMENTATION
            for entry in calls
        )
        assert "Prior caller failed" in calls[1][1]
        assert all("/goal" not in entry[1] for entry in calls)

    @pytest.mark.parametrize("failure_reports_usage", [False, True])
    def test_failed_attempt_and_rotation_share_authoritative_usage_ledger(
        self, tmp_path, monkeypatch, failure_reports_usage
    ):
        monkeypatch.setenv("FIRST_MODEL", "claude-configured-first")

        class LedgerRunner:
            def __init__(self, name, succeeds):
                self.name = name
                self.succeeds = succeeds

            def get_startup_metadata(self, env=None):
                return RunnerStartupMetadata(
                    provider="anthropic" if self.name == "first" else "openai",
                    model="runner-startup-fallback",
                )

            def build_call_args(self, prompt, read_only, **kwargs):
                return [prompt]

            def run_with_monitor(self, args, **kwargs):
                if self.succeeds:
                    lines = [
                        json.dumps(
                            {
                                "type": "init",
                                "provider": "openai",
                                "session_id": "second-session",
                                "model": "GPT-5-Codex",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "result",
                                "usage_event_id": "second-result",
                                "usage": {
                                    "input_tokens": 20,
                                    "cached_input_tokens": 5,
                                    "output_tokens": 3,
                                },
                                "result": "completed",
                            }
                        ),
                    ]
                    output = "\n".join(lines)
                    for line in lines:
                        kwargs["on_output"](line)
                    return SimpleNamespace(
                        success=True,
                        output=output,
                        interrupted=False,
                        returncode=0,
                        cmd_used=self.name,
                    )

                failure = {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "quota exceeded",
                }
                if failure_reports_usage:
                    failure.update(
                        {
                            "usage_event_id": "failed-result",
                            "usage": {"input_tokens": 7, "output_tokens": 1},
                        }
                    )
                line = json.dumps(failure)
                kwargs["on_output"](line)
                return SimpleNamespace(
                    success=False,
                    output=line,
                    interrupted=False,
                    returncode=1,
                    cmd_used=self.name,
                )

            def detect_infra_error(self, returncode, stdout, stderr):
                from tianluo.agent_runner import InfraErrorType

                return InfraErrorType.USAGE_LIMIT

        caller = LLMCaller(
            project_root=tmp_path,
            max_retries=2,
            retry_delay=0,
            flow_id="usage-rotation-flow",
            step_id="usage-step",
            step_type="implement",
            agents=[
                {
                    "name": "first",
                    "type": "claude-code",
                    "cmd": "first",
                    "provider": "anthropic",
                    "model": "$FIRST_MODEL",
                },
                {
                    "name": "second",
                    "type": "codex",
                    "cmd": "second",
                    "provider": "openai",
                    "model": "configured-second",
                },
            ],
        )
        caller._runner_cache = {
            "first": LedgerRunner("first", succeeds=False),
            "second": LedgerRunner("second", succeeds=True),
        }

        with patch.object(caller, "_record_prompt"), patch.object(
            caller, "_record_response"
        ) as record_response, accumulate_step_usage() as totals:
            output = caller.call("finish", json_mode="off")

        assert "completed" in output
        assert len(totals.usage_records) == 2
        failed, succeeded = totals.usage_records
        assert failed.usage_status == (
            UsageStatus.AVAILABLE
            if failure_reports_usage
            else UsageStatus.UNAVAILABLE
        )
        assert failed.agent_name == "first"
        assert failed.runner_type == "claude-code"
        assert failed.provider == "anthropic"
        assert failed.configured_model == "claude-configured-first"
        assert failed.resolved_model_source == "agent_config"
        assert succeeded.usage_status == UsageStatus.AVAILABLE
        assert succeeded.agent_name == "second"
        assert succeeded.provider_session_id == "second-session"
        assert succeeded.reported_model == "GPT-5-Codex"
        assert succeeded.resolved_model == "gpt-5-codex"
        assert succeeded.resolved_model_source == "provider"
        assert failed.call_id != succeeded.call_id
        assert record_response.call_count == 2
        assert [
            call.kwargs["usage_record"].call_id
            for call in record_response.call_args_list
        ] == [failed.call_id, succeeded.call_id]

    def test_runner_exception_still_records_unavailable_attempt(self, tmp_path):
        class ExplodingRunner:
            def get_startup_metadata(self, env=None):
                return RunnerStartupMetadata(
                    provider="compatible", model="verified-startup-model"
                )

            def build_call_args(self, prompt, read_only, **kwargs):
                return [prompt]

            def run_with_monitor(self, args, **kwargs):
                raise RuntimeError("spawn transport failed")

        caller = _make_caller(project_root=tmp_path, max_retries=1)
        runner = ExplodingRunner()
        with patch.object(
            caller, "_get_current_runner", return_value=runner
        ), patch.object(caller, "_record_prompt"), patch.object(
            caller, "_record_response"
        ) as response, accumulate_step_usage() as totals:
            with pytest.raises(LLMCallError, match="spawn transport failed"):
                caller.call("work", json_mode="off")

        assert len(totals.usage_records) == 1
        record = totals.usage_records[0]
        assert record.usage_status == UsageStatus.UNAVAILABLE
        assert record.provider == "compatible"
        assert record.resolved_model == "verified-startup-model"
        assert record.resolved_model_source == "runner_startup"
        assert response.call_args.kwargs["usage_record"].call_id == record.call_id

