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
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.engine.llm_caller import LLMCaller


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
    ``se3.yaml`` actually reaches the subprocess. Without forwarding, the
    Runner falls back to the built-in default ``["user"]`` regardless of
    user config — silently neutralising the documented escape hatch.
    """

    def test_project_root_forwarded_so_yaml_setting_sources_takes_effect(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
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
        self, prompt, read_only, context_files=None, spec_guard_plugin=None
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
        if spec_guard_plugin is not None:
            args += ["--plugin-dir", str(spec_guard_plugin)]
        if context_files:
            for f in context_files:
                if f.exists():
                    args.extend(["--file", str(f)])
        return args

    def run_with_monitor(self, args, **kwargs):
        self.captured_args = list(args)
        return _FakeResult(output="")


class TestReadOnlyToolDisallowList:
    """Tool-layer enforcement: read-only steps append --disallowedTools for the
    write tools; writable steps (sync_resolve / implement) do not."""

    def _run_and_capture_args(self, step_type: str):
        caller = _make_caller(step_type=step_type)
        runner = _ArgsCapturingRunner()
        with patch.object(LLMCaller, "_get_current_runner", return_value=runner), \
             patch.object(LLMCaller, "_record_prompt"), \
             patch.object(LLMCaller, "_record_response"):
            caller.call("do the thing", json_mode="off")
        assert runner.captured_args is not None
        return runner.captured_args

    def test_sync_scan_args_contain_disallowed_write_tools(self):
        args = self._run_and_capture_args("sync_scan")
        assert "--disallowedTools" in args
        for tool in ("Write", "Edit", "NotebookEdit", "AskUserQuestion"):
            assert tool in args, f"{tool} should be in disallowed list"

    def test_sync_analyze_args_contain_disallowed_write_tools(self):
        args = self._run_and_capture_args("sync_analyze")
        assert "--disallowedTools" in args
        assert "Write" in args and "Edit" in args

    def test_read_tools_not_disallowed(self):
        """Read/Grep/Glob/Bash must remain available (not in the disallow list)."""
        args = self._run_and_capture_args("sync_scan")
        di = args.index("--disallowedTools")
        disallowed = args[di + 1:]
        for tool in ("Read", "Grep", "Glob", "Bash"):
            assert tool not in disallowed, f"{tool} must not be disallowed"

    def test_sync_resolve_args_have_no_disallowed_tools(self):
        """sync_resolve is the writable update path (Way A edits the spec)."""
        args = self._run_and_capture_args("sync_resolve")
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
