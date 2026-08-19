"""Regression guards for the retired spec-write-protection runtime machinery.

The `tianluo/specs/` mirror is gone, and with it the whole three-layer guard that
kept non-`update_spec` steps from writing it:

  * the soft prompt injection (`## SPEC FILE WRITE PROTECTION`),
  * the PreToolUse guard plugin wired in via Claude's `--plugin-dir`,
  * the post-step spec-diff snapshot/revert fallback in the state machine.

These tests pin the *absence* of each layer, plus the two compatibility promises
the removal carries: the three runners keep a matching `build_call_args`
signature, and a `tianluo.yaml` still carrying a `spec_write_protection:` block
loads without raising.
"""

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.llm_caller import LLMCaller


# ---------------------------------------------------------------------------
# Layer 1 — the prompt injection is gone
# ---------------------------------------------------------------------------


class TestNoSpecWriteProtectionInPrompt:
    """No step's final prompt carries the spec-write-protection section."""

    def _final_prompt(self, step_type: str) -> str:
        caller = LLMCaller(
            project_root="/tmp/test_project",
            step_type=step_type,
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo test"}],
        )
        with patch.object(LLMCaller, "_call_with_retry") as mock_call:
            mock_call.return_value = "output"
            caller.call("do the work", json_mode="off")
        return mock_call.call_args[1]["prompt"]

    @pytest.mark.parametrize(
        "step_type",
        ["implement", "commit", "confirm", "charter_freshness", "update_spec"],
    )
    def test_writable_llm_step_prompt_has_no_spec_write_protection(self, step_type):
        prompt = self._final_prompt(step_type)
        assert "SPEC FILE WRITE PROTECTION" not in prompt
        assert "tianluo/specs" not in prompt

    def test_read_only_step_prompt_has_no_spec_write_protection(self):
        prompt = self._final_prompt("analyze")
        assert "SPEC FILE WRITE PROTECTION" not in prompt
        # The read-only layer itself is untouched by the removal.
        assert "READ-ONLY STEP CONSTRAINT" in prompt

    def test_context_builder_exposes_no_spec_write_symbols(self):
        from tianluo.engine import context_builder

        for name in (
            "get_spec_write_protection_injection",
            "_is_spec_write_protected_step",
            "SPEC_WRITE_ALLOWED_STEPS",
            "_WRITABLE_SYNC_STEPS",
            "_ALL_SYNC_STEPS",
            "_READ_ONLY_SYNC_STEPS",
        ):
            assert not hasattr(context_builder, name), name

    def test_llm_caller_exposes_no_guard_resolver(self):
        assert not hasattr(LLMCaller, "_resolve_spec_guard_settings")


# ---------------------------------------------------------------------------
# Layer 2 — the PreToolUse guard plugin is gone
# ---------------------------------------------------------------------------


class TestNoGuardPlugin:
    """No code path installs a guard plugin or passes --plugin-dir."""

    def test_spec_write_hook_module_is_gone(self):
        with pytest.raises(ImportError):
            __import__("tianluo.engine.spec_write_hook")

    @pytest.mark.parametrize("read_only", [False, True])
    def test_claude_runner_args_have_no_plugin_dir(self, read_only, tmp_path):
        from tianluo.claude_runner import ClaudeCodeRunner

        runner = ClaudeCodeRunner(commands=[{"cmd": "claude", "priority": 0}])
        ctx = tmp_path / "ctx.md"
        ctx.write_text("context", encoding="utf-8")
        args = runner.build_call_args(
            prompt="do the work", read_only=read_only, context_files=[ctx]
        )
        assert "--plugin-dir" not in args
        assert "--settings" not in args
        # Sanity: the argv the caller still relies on is intact.
        assert args[args.index("-p") + 1] == "do the work"
        assert str(ctx) in args

    def test_runner_build_call_args_signatures_match(self):
        """Interface parity: every runner takes the same build_call_args params."""
        from tianluo.agent_runner import AgentRunner
        from tianluo.claude_interactive_runner import ClaudeInteractiveRunner
        from tianluo.claude_runner import ClaudeCodeRunner
        from tianluo.codex_runner import CodexRunner

        expected = ["self", "prompt", "read_only", "context_files", "invocation_intent"]
        for cls in (
            AgentRunner,
            ClaudeCodeRunner,
            CodexRunner,
            ClaudeInteractiveRunner,
        ):
            params = list(
                inspect.signature(cls.build_call_args).parameters
            )
            assert params == expected, f"{cls.__name__}: {params}"

    def test_llm_caller_does_not_pass_spec_guard_plugin(self, tmp_path):
        """The intent handed to the runner carries no guard-plugin keyword."""
        captured = {}

        class _Result:
            success = True
            output = ""
            interrupted = False

        class _Runner:
            def build_call_args(self, **kwargs):
                captured.update(kwargs)
                return ["-p", kwargs["prompt"]]

            def run_with_monitor(self, args, **kwargs):
                return _Result()

        caller = LLMCaller(
            project_root=tmp_path,
            step_type="implement",
            max_retries=1,
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        with patch.object(LLMCaller, "_get_current_runner", return_value=_Runner()), \
             patch.object(LLMCaller, "_record_prompt"), \
             patch.object(LLMCaller, "_record_response"):
            caller.call("implement it", json_mode="off")

        assert "spec_guard_plugin" not in captured
        assert set(captured) <= {"prompt", "read_only", "context_files"}


# ---------------------------------------------------------------------------
# Layer 3 — the post-step spec-diff fallback is gone
# ---------------------------------------------------------------------------


class TestNoSpecDiffFallback:
    def test_state_machine_has_no_spec_diff_guard(self):
        from tianluo.engine.state_machine import StateMachine

        assert not hasattr(StateMachine, "_spec_diff_guard_enabled")

    def test_state_machine_source_never_snapshots_specs(self):
        import tianluo.engine.state_machine as sm

        source = Path(inspect.getfile(sm)).read_text(encoding="utf-8")
        for marker in (
            "spec_write_hook",
            "capture_spec_contents",
            "restore_spec_files",
            "SPEC_WRITE_ALLOWED_STEPS",
        ):
            assert marker not in source, marker


# ---------------------------------------------------------------------------
# Compatibility — a leftover config block must not break loading
# ---------------------------------------------------------------------------


class TestLegacyConfigBlockIgnored:
    """A `tianluo.yaml` still carrying `spec_write_protection:` loads cleanly.

    The loader for that section is gone; because config reads named sections on
    demand rather than validating a whole-file schema, a residual block is
    silently ignored. This test is the guard against someone re-introducing
    whole-file validation that would turn existing user configs into hard errors.
    """

    def _write_config(self, project_root: Path) -> None:
        (project_root / "tianluo.yaml").write_text(
            "spec_write_protection:\n"
            "  hook_enabled: true\n"
            "  diff_fallback_enabled: false\n"
            "test:\n"
            "  parallel: auto\n",
            encoding="utf-8",
        )

    def test_project_yaml_loads_with_legacy_block(self, tmp_path):
        from tianluo.config import load_project_yaml

        self._write_config(tmp_path)
        data, _src = load_project_yaml(tmp_path)
        assert "spec_write_protection" in data

    def test_config_loaders_ignore_legacy_block(self, tmp_path):
        from tianluo.config import (
            load_claude_subprocess_config,
            load_config,
            load_merge_config,
            load_step_config,
            load_workflow_config,
        )

        self._write_config(tmp_path)
        for loader in (
            load_config,
            load_workflow_config,
            load_step_config,
            load_merge_config,
            load_claude_subprocess_config,
        ):
            loader(tmp_path)

    def test_spec_write_protection_loader_is_gone(self):
        import tianluo.config as config_mod

        assert not hasattr(config_mod, "SpecWriteProtectionConfig")
        assert not hasattr(config_mod, "load_spec_write_protection_config")
