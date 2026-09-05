"""Native-session-resume contract across the three agent runners.

Each runner declares whether its CLI can continue a recorded provider session
and, if so, translates a resume intent into argv. The declarations here were
verified against the CLIs installed on the development machine
(``claude 2.1.258`` / ``codex-cli 0.147.0``); these tests pin the *shape* of
what that verification established so a regression is caught without a network
call.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tianluo.agent_runner import AgentRunner, RunnerStartupMetadata
from tianluo.claude_runner import ClaudeCodeRunner
from tianluo.codex_runner import CodexRunner


def _uuid_like(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class TestAgentRunnerContract:
    def test_base_class_declines_resume_by_default(self):
        """A runner that has not declared the capability cannot be driven
        down a path its CLI may not implement."""

        class Bare(AgentRunner):
            def run(self, *a, **k):  # pragma: no cover - not exercised
                raise NotImplementedError

            def run_with_monitor(self, *a, **k):  # pragma: no cover
                raise NotImplementedError

            def build_call_args(self, *a, **k):  # pragma: no cover
                return []

            def detect_infra_error(self, *a, **k):  # pragma: no cover
                raise NotImplementedError

        runner = Bare()
        assert runner.supports_native_resume is False
        with pytest.raises(NotImplementedError):
            runner.build_resume_call_args("sid", "hi", True)


class TestClaudeCodeRunnerResume:
    def test_session_id_is_preallocated_before_launch(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        meta = runner.get_startup_metadata()
        assert isinstance(meta, RunnerStartupMetadata)
        assert _uuid_like(meta.provider_session_id)

    def test_each_attempt_gets_a_fresh_session_id(self):
        """Two attempts must never share a provider session."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        first = runner.get_startup_metadata().provider_session_id
        second = runner.get_startup_metadata().provider_session_id
        assert first != second

    def test_build_call_args_injects_the_preallocated_session_id(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        sid = runner.get_startup_metadata().provider_session_id
        args = runner.build_call_args("do the thing", read_only=False)
        assert "--session-id" in args
        assert args[args.index("--session-id") + 1] == sid
        # The prompt still travels as -p and the tool lock is still one flag.
        assert args[args.index("-p") + 1] == "do the thing"
        assert args.count("--disallowedTools") == 1

    def test_build_call_args_without_startup_metadata_omits_session_id(self):
        """Callers that never asked for startup metadata are unaffected."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args("x", read_only=True)
        assert "--session-id" not in args

    def test_resume_argv_shape(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args("sid-123", "carry on", read_only=True)
        assert args[:2] == ["--output-format", "stream-json"]
        assert "--resume" in args
        assert args[args.index("--resume") + 1] == "sid-123"
        assert args[args.index("-p") + 1] == "carry on"
        # --session-id and --resume are mutually exclusive.
        assert "--session-id" not in args

    def test_resume_keeps_one_merged_disallowed_tools_flag(self):
        """A repeated --disallowedTools is last-wins in the claude CLI, so the
        read-only lock must ride in a single flag on the resume path too."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args("sid", "go", read_only=True)
        assert args.count("--disallowedTools") == 1
        idx = args.index("--disallowedTools")
        locked = args[idx + 1 : idx + 6]
        assert locked == [
            "Write", "Edit", "NotebookEdit", "AskUserQuestion", "ReportFindings",
        ]

    def test_resume_writable_still_denies_report_findings_only(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args("sid", "go", read_only=False)
        idx = args.index("--disallowedTools")
        assert args[idx + 1] == "ReportFindings"

    def test_resume_never_appends_settings(self):
        """A duplicated --settings is last-wins and would drop the agent
        wrapper's model selection."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args("sid", "go", read_only=False)
        assert "--settings" not in args

    def test_resume_requires_a_session_id(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        with pytest.raises(ValueError):
            runner.build_resume_call_args("", "go", read_only=False)

    def test_resume_forwards_existing_context_files(self, tmp_path):
        present = tmp_path / "a.md"
        present.write_text("x", encoding="utf-8")
        missing = tmp_path / "gone.md"
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args(
            "sid", "go", read_only=False, context_files=[present, missing]
        )
        assert args.count("--file") == 1
        assert str(present) in args

    def test_capability_is_declared(self):
        assert ClaudeCodeRunner.supports_native_resume is True


class TestStrictReadOnlyLock:
    """The dialog's read-only lock has to close the shell too.

    The CLI is launched with --dangerously-skip-permissions, so nothing outside
    --disallowedTools stands between the agent and the workspace: denying only
    the edit tools leaves `rm` and `>` reachable through Bash while the user is
    still deciding what happens to that workspace.
    """

    def test_resume_denies_the_shell_when_asked(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args(
            "sid", "go", read_only=True, deny_shell=True
        )
        assert args.count("--disallowedTools") == 1
        idx = args.index("--disallowedTools")
        locked = args[idx + 1 :]
        for tool in (
            "Write", "Edit", "Bash", "BashOutput", "KillShell", "Task", "Agent",
        ):
            assert tool in locked

    def test_a_fresh_call_denies_the_shell_when_asked(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args("go", read_only=True, deny_shell=True)
        assert "Bash" in args

    def test_both_delegation_tool_names_are_denied(self):
        """The CLI renamed Task -> Agent; a subagent spawned through either one
        would start WITHOUT the dialog's lock and could edit the tree while the
        user is still deciding what happens to it."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        for args in (
            runner.build_call_args("go", read_only=True, deny_shell=True),
            runner.build_resume_call_args("sid", "go", read_only=True, deny_shell=True),
        ):
            idx = args.index("--disallowedTools")
            locked = args[idx + 1 :]
            assert "Task" in locked
            assert "Agent" in locked

    def test_ordinary_read_only_steps_keep_the_shell(self):
        """review / self-check / analyze inspect the tree with git and grep."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        assert "Bash" not in runner.build_call_args("go", read_only=True)
        assert "Bash" not in runner.build_resume_call_args(
            "sid", "go", read_only=True
        )

    def test_a_writable_call_is_unaffected(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args("go", read_only=False, deny_shell=True)
        idx = args.index("--disallowedTools")
        assert args[idx + 1 :] == ["ReportFindings"]

    def test_the_interactive_runner_locks_the_shell_too(self):
        from tianluo.claude_interactive_runner import ClaudeInteractiveRunner

        runner = ClaudeInteractiveRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_resume_call_args(
            "sid", "go", read_only=True, deny_shell=True
        )
        assert args.count("--disallowedTools") == 1
        assert "Bash" in args
        assert "Bash" not in runner.build_resume_call_args(
            "sid", "go", read_only=True
        )

    def test_codex_expresses_it_as_a_sandbox(self):
        """codex's read-only posture is a sandbox, which already refuses shell
        writes — the flag needs no translation there."""
        from tianluo.codex_runner import CodexRunner

        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_resume_call_args(
            "th", "go", read_only=True, deny_shell=True
        )
        assert 'sandbox_mode="read-only"' in args

    def test_llm_caller_asks_for_it_only_when_strict(self):
        from tianluo.engine.llm_caller import LLMCaller

        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        strict = LLMCaller.__new__(LLMCaller)
        strict.deny_shell = True
        kwargs = {}
        strict._add_deny_shell(kwargs, runner.build_resume_call_args)
        assert kwargs == {"deny_shell": True}

        lenient = LLMCaller.__new__(LLMCaller)
        lenient.deny_shell = False
        kwargs = {}
        lenient._add_deny_shell(kwargs, runner.build_resume_call_args)
        assert kwargs == {}

    def test_a_runner_without_the_keyword_is_still_callable(self):
        """A third-party adapter written against the pre-strict interface stays
        valid; it simply cannot enforce the boundary."""
        from tianluo.engine.llm_caller import LLMCaller

        def _old_style(session_id, prompt, read_only, context_files=None):
            return []

        caller = LLMCaller.__new__(LLMCaller)
        caller.deny_shell = True
        kwargs = {}
        caller._add_deny_shell(kwargs, _old_style)
        assert kwargs == {}


class TestCodexRunnerResume:
    def test_capability_is_declared(self):
        assert CodexRunner.supports_native_resume is True

    def test_startup_metadata_has_no_session_before_the_first_run(self):
        """codex mints its own thread id, so there is nothing to pre-allocate;
        LLMCaller learns it from the attempt's response record instead."""
        runner = CodexRunner()
        assert runner.get_startup_metadata().provider_session_id is None

    def test_startup_metadata_never_reports_a_previous_thread_id(self):
        """A fresh attempt must not inherit the preceding thread: LLMCaller
        would record the stale id with precedence over the real
        ``thread.started`` one, and the next retry would resume the wrong
        thread."""
        runner = CodexRunner()
        runner._last_provider_session_id = "01a0-thread"
        assert runner.get_startup_metadata().provider_session_id is None

    def test_resume_argv_uses_the_exec_resume_subcommand(self):
        runner = CodexRunner()
        args = runner.build_resume_call_args("th-1", "carry on", read_only=True)
        assert args[:3] == ["exec", "resume", "th-1"]
        assert "--json" in args
        assert args[-1] == "carry on"

    def test_resume_expresses_the_lock_through_config_overrides(self):
        """``codex exec resume`` has no --sandbox flag, so the read-only lock
        travels as a -c config override."""
        runner = CodexRunner()
        ro = runner.build_resume_call_args("th", "go", read_only=True)
        assert "--sandbox" not in ro
        assert 'sandbox_mode="read-only"' in ro
        assert ro[ro.index('sandbox_mode="read-only"') - 1] == "-c"

        rw = runner.build_resume_call_args("th", "go", read_only=False)
        assert 'sandbox_mode="danger-full-access"' in rw

    def test_fresh_call_uses_the_same_config_override(self):
        """One posture spelling for both invocation shapes (runner contract)."""
        runner = CodexRunner()
        args = runner.build_call_args("go", read_only=True)
        assert "--sandbox" not in args
        assert 'sandbox_mode="read-only"' in args
        assert args[args.index('sandbox_mode="read-only"') - 1] == "-c"

        rw = runner.build_call_args("go", read_only=False)
        assert "--sandbox" not in rw
        assert 'sandbox_mode="danger-full-access"' in rw

    def test_resume_requires_a_session_id(self):
        runner = CodexRunner()
        with pytest.raises(ValueError):
            runner.build_resume_call_args("", "go", read_only=False)

    def test_resume_inlines_context_files(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("CONTENT-MARKER", encoding="utf-8")
        runner = CodexRunner()
        args = runner.build_resume_call_args(
            "th", "go", read_only=True, context_files=[f]
        )
        assert "CONTENT-MARKER" in args[-1]

    def test_oversized_resume_prompt_routes_to_stdin(self):
        runner = CodexRunner()
        huge = "x" * 200_000
        args = runner.build_resume_call_args("th", huge, read_only=True)
        assert args[-1] == "-"
        assert runner._pending_stdin_prompt == huge


class TestClaudeInteractiveRunnerResume:
    def _runner(self):
        pytest.importorskip("pexpect")
        from tianluo.claude_interactive_runner import ClaudeInteractiveRunner

        return ClaudeInteractiveRunner(command={"cmd": "claude", "priority": 0})

    def test_capability_is_declared(self):
        pytest.importorskip("pexpect")
        from tianluo.claude_interactive_runner import ClaudeInteractiveRunner

        assert ClaudeInteractiveRunner.supports_native_resume is True

    def test_session_id_is_preallocated_and_launched_as_session_id(self):
        runner = self._runner()
        sid = runner.get_startup_metadata().provider_session_id
        assert _uuid_like(sid)
        cmd = runner._build_full_cmd(["--foo"])
        assert "--session-id" in cmd
        assert cmd[cmd.index("--session-id") + 1] == sid

    def test_resume_launches_with_resume_not_session_id(self):
        """``--session-id`` asks the CLI to CREATE that id; resuming an existing
        session must use ``--resume`` or the launch conflicts."""
        runner = self._runner()
        runner.build_resume_call_args("existing-sid", "hello", read_only=True)
        cmd = runner._build_full_cmd([])
        assert "--session-id" not in cmd
        assert cmd[cmd.index("--resume") + 1] == "existing-sid"

    def test_resume_keeps_the_bound_session_id_for_the_transcript_watcher(self):
        runner = self._runner()
        runner.build_resume_call_args("existing-sid", "hello", read_only=True)
        # The transcript is the SAME <session_id>.jsonl, so the watcher binding
        # must follow the resumed id rather than a freshly minted one.
        assert runner._ensure_session_id() == "existing-sid"
        assert runner._make_watcher(Path("/tmp")).session_id == "existing-sid"

    def test_a_failed_resume_falls_back_to_a_genuinely_fresh_session(self):
        """LLMCaller asks for startup metadata only on the rebuild path, so
        reaching it after a resume means the next launch must NOT re-issue
        --resume against the session that just failed."""
        runner = self._runner()
        runner.build_resume_call_args("dead-sid", "hello", read_only=True)
        fresh = runner.get_startup_metadata().provider_session_id
        assert _uuid_like(fresh)
        assert fresh != "dead-sid"
        cmd = runner._build_full_cmd([])
        assert "--resume" not in cmd
        assert cmd[cmd.index("--session-id") + 1] == fresh

    def test_resume_feeds_the_prompt_through_the_tui_not_argv(self):
        """Interactive mode carries no -p; the prompt is typed into the box."""
        runner = self._runner()
        args = runner.build_resume_call_args("sid", "the prompt", read_only=True)
        assert "-p" not in args
        assert "the prompt" not in args
        assert runner._pending_prompt == "the prompt"

    def test_resume_keeps_one_merged_disallowed_tools_flag(self):
        runner = self._runner()
        args = runner.build_resume_call_args("sid", "x", read_only=True)
        assert args.count("--disallowedTools") == 1

    def test_resume_requires_a_session_id(self):
        runner = self._runner()
        with pytest.raises(ValueError):
            runner.build_resume_call_args("", "x", read_only=True)
