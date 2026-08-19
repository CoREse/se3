"""Tests for ClaudeInteractiveRunner (claude_interactive_runner module).

Group G1 scope — PTY process driving & lifecycle skeleton:
- build_call_args: interactive-mode flag translation (read-only, context
  files, NO print-only flags, prompt stashed for PTY feeding)
- PTY driving: _feed_prompt / _drain_pty threads via a fake PTY stub
- Lifecycle: wall / inactivity double timeout, _terminate (process-group
  kill + reap), finally-guaranteed reclamation, I/O thread join
- Result dataclasses (MonitoredResult / _SingleRunResult)
- detect_infra_error baseline classification

These tests use a fake PTY stub and monkeypatching; they never spawn a real
``claude`` or a real pseudo-terminal.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# pexpect is the declared runtime dep for the claude-interactive runner, but the
# runner itself imports it lazily, so guard collection: skip (rather than error
# out the whole module) when it is absent from the test environment.
pexpect = pytest.importorskip("pexpect")  # noqa: E402  (used for TIMEOUT/EOF)

import json  # noqa: E402

from tianluo.agent_runner import (  # noqa: E402
    AgentInvocationIntent,
    AgentRunner,
    InfraErrorType,
)
import tianluo.claude_interactive_runner as cir  # noqa: E402
from tianluo.claude_interactive_runner import (  # noqa: E402
    ClaudeInteractiveRunner,
    MonitoredResult,
    SessionTranscriptWatcher,
    USAGE_TOKEN_KEYS,
    _ActivityClock,
    _MeaningfulContentTracker,
    _normalize_pty_lines,
    _SingleRunResult,
    _is_terminal_record,
    claude_projects_dir,
    extract_usage_from_record,
    locate_session_file,
    munge_cwd,
    snapshot_session_files,
    synthesize_result_line,
    tail_new_records,
    to_stream_json_ndjson,
    turn_complete,
)


def test_interactive_startup_metadata_does_not_guess_wrapper_model():
    runner = ClaudeInteractiveRunner(
        command={"cmd": "company-wrapper", "priority": 0}
    )
    metadata = runner.get_startup_metadata()
    assert metadata.provider == "anthropic"
    assert metadata.model is None


# =============================================================================
# Fake PTY stub
# =============================================================================


class FakePty:
    """Minimal stand-in for ``pexpect.spawn``.

    Yields ``chunks`` from ``read_nonblocking`` until exhausted; afterwards it
    either keeps raising ``pexpect.TIMEOUT`` (when ``die_when_empty`` is False —
    simulating a still-running process) or flips to dead and raises
    ``pexpect.EOF`` (simulating normal exit).
    """

    def __init__(
        self,
        chunks=None,
        die_when_empty=False,
        exitstatus=0,
        signalstatus=None,
        pid=999999,
    ):
        self._chunks = list(chunks or [])
        self._die_when_empty = die_when_empty
        self._alive = True
        self.exitstatus = exitstatus
        self.signalstatus = signalstatus
        self.pid = pid
        self.sent = []
        self.closed = False

    def isalive(self):
        return self._alive

    def read_nonblocking(self, size=1, timeout=None):
        if self._chunks:
            return self._chunks.pop(0)
        if self._die_when_empty:
            self._alive = False
            raise pexpect.EOF("eof")
        raise pexpect.TIMEOUT("timeout")

    def send(self, s):
        self.sent.append(s)
        return len(s)

    def sendline(self, s=""):
        self.sent.append(s + "\n")
        return len(s) + 1

    def close(self, force=False):
        self.closed = True
        self._alive = False

    def terminate(self, force=False):
        self._alive = False
        return True


def _make_runner():
    return ClaudeInteractiveRunner(command={"cmd": "claude", "priority": 0})


@pytest.fixture
def no_kill(monkeypatch):
    """Record process-group signals instead of sending them to real PIDs."""
    calls = []

    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))

    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    return calls


# =============================================================================
# Class contract
# =============================================================================


class TestContract:
    def test_is_agent_runner_subclass(self):
        assert issubclass(ClaudeInteractiveRunner, AgentRunner)

    def test_instantiable_without_pexpect_import(self):
        # Construction must not require pexpect or config; just works.
        runner = _make_runner()
        assert runner.command["cmd"] == "claude"
        assert runner.setting_sources == ["user"]

    def test_legacy_commands_list_view(self):
        runner = ClaudeInteractiveRunner(
            commands=[{"cmd": "kclaude", "priority": 1}]
        )
        assert runner.command["cmd"] == "kclaude"
        assert runner.commands == [{"cmd": "kclaude", "priority": 1}]


# =============================================================================
# build_call_args — intent → interactive-mode flags
# =============================================================================


class TestBuildCallArgs:
    def test_read_only_adds_disallowed_tools(self):
        runner = _make_runner()
        args = runner.build_call_args("do a thing", read_only=True)
        # One flag only: the claude CLI resolves a repeated flag last-one-wins,
        # so a second --disallowedTools would drop the write-tool lock.
        assert args.count("--disallowedTools") == 1
        idx = args.index("--disallowedTools")
        assert args[idx + 1 : idx + 6] == [
            "Write",
            "Edit",
            "NotebookEdit",
            "AskUserQuestion",
            "ReportFindings",
        ]

    def test_writable_disallows_only_report_findings(self):
        """Writable steps deny no write tool, but still deny ReportFindings —
        a host-UI tool whose output nothing receives when tianluo drives the
        CLI headlessly."""
        runner = _make_runner()
        args = runner.build_call_args("do a thing", read_only=False)
        assert args.count("--disallowedTools") == 1
        idx = args.index("--disallowedTools")
        assert args[idx + 1 :] == ["ReportFindings"]
        for write_tool in ("Write", "Edit", "NotebookEdit", "AskUserQuestion"):
            assert write_tool not in args

    def test_report_findings_denied_on_both_read_only_modes(self):
        runner = _make_runner()
        for read_only in (True, False):
            args = runner.build_call_args("p", read_only=read_only)
            assert args.count("--disallowedTools") == 1
            assert "ReportFindings" in args

    def test_disallowed_tools_precedes_add_dir(self, tmp_path):
        """The merged denial list must not swallow the --add-dir flags that
        follow it."""
        f = tmp_path / "pkg" / "mod.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")
        runner = _make_runner()
        args = runner.build_call_args("p", read_only=False, context_files=[f])
        assert args == [
            "--disallowedTools",
            "ReportFindings",
            "--add-dir",
            str(tmp_path / "pkg"),
        ]

    def test_no_print_only_flags(self):
        runner = _make_runner()
        args = runner.build_call_args("hello", read_only=False)
        for flag in ("--output-format", "--input-format", "-p", "--prompt"):
            assert flag not in args

    def test_prompt_stashed_not_in_argv(self):
        runner = _make_runner()
        args = runner.build_call_args("my effective prompt", read_only=False)
        assert "my effective prompt" not in args
        assert runner._pending_prompt == "my effective prompt"

    def test_direct_intent_uses_plain_interactive_prompt(self):
        runner = _make_runner()
        args = runner.build_call_args(
            "implement everything",
            read_only=False,
            invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
        )
        assert "/goal" not in runner._pending_prompt
        assert runner._pending_prompt == "implement everything"
        assert "/goal" not in " ".join(args)
        assert not getattr(runner, "supports_native_goal", False)

    def test_context_files_translated_to_add_dir(self, tmp_path):
        f1 = tmp_path / "a" / "x.py"
        f2 = tmp_path / "a" / "y.py"  # same parent → deduped
        f3 = tmp_path / "b" / "z.py"
        for f in (f1, f2, f3):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x = 1\n")
        runner = _make_runner()
        args = runner.build_call_args(
            "p", read_only=False, context_files=[f1, f2, f3]
        )
        add_dir_values = [
            args[i + 1] for i, a in enumerate(args) if a == "--add-dir"
        ]
        assert str((tmp_path / "a")) in add_dir_values
        assert str((tmp_path / "b")) in add_dir_values
        # Deduplicated: parent "a" appears once even though two files share it.
        assert add_dir_values.count(str(tmp_path / "a")) == 1

    def test_nonexistent_context_files_skipped(self, tmp_path):
        runner = _make_runner()
        missing = tmp_path / "nope" / "ghost.py"
        args = runner.build_call_args("p", read_only=False, context_files=[missing])
        assert "--add-dir" not in args

    def test_full_cmd_injects_prefix(self):
        runner = _make_runner()
        full = runner._build_full_cmd(["--disallowedTools", "Write"])
        assert full[0] == "claude"
        assert "--dangerously-skip-permissions" in full
        assert "--setting-sources" in full
        idx = full.index("--setting-sources")
        assert full[idx + 1] == "user"

    def test_full_cmd_omits_session_id_when_unset(self):
        # Before a run assigns a session id, no --session-id flag is emitted.
        runner = _make_runner()
        full = runner._build_full_cmd([])
        assert "--session-id" not in full

    def test_full_cmd_injects_session_id_when_set(self):
        runner = _make_runner()
        runner._session_id = "deadbeef-dead-4bee-8bee-deadbeefdead"
        full = runner._build_full_cmd([])
        assert "--session-id" in full
        idx = full.index("--session-id")
        assert full[idx + 1] == "deadbeef-dead-4bee-8bee-deadbeefdead"


# =============================================================================
# PTY driving threads
# =============================================================================


class TestPtyDriving:
    def test_feed_prompt_sends_text_and_enter(self):
        runner = _make_runner()
        fake = FakePty()
        runner._feed_prompt(fake, "hello world", ready_delay=0)
        joined = "".join(fake.sent)
        # Prompt is delivered wrapped in a bracketed paste, then submitted.
        assert "\x1b[200~" in joined  # bracketed-paste start
        assert "hello world" in joined
        assert "\x1b[201~" in joined  # bracketed-paste end
        # The carriage return (submit) is the last thing written.
        assert fake.sent[-1] == "\r"

    def test_feed_prompt_multiline_uses_bracketed_paste(self):
        # Multi-line prompts must be delivered as ONE bracketed-paste message
        # (newlines inserted as input), not split into multiple sends/turns.
        runner = _make_runner()
        fake = FakePty()
        prompt = "line one\nline two\nline three"
        runner._feed_prompt(fake, prompt, ready_delay=0)
        # Everything before the final submit \r is a single payload that wraps
        # the full multi-line prompt in bracketed paste with newlines intact.
        body = "".join(fake.sent[:-1])
        assert body == "\x1b[200~" + prompt + "\x1b[201~"
        assert fake.sent[-1] == "\r"

    def test_await_input_ready_returns_after_output_settles(self):
        runner = _make_runner()
        clock = _ActivityClock()
        buf = ["rendered input box"]  # PTY produced output already
        t0 = time.time()
        # settle window short; clock not bumped again -> already idle -> returns
        runner._await_input_ready(buf, clock, ready_timeout=5.0, settle=0.05)
        assert time.time() - t0 < 2.0

    def test_await_input_ready_waits_for_first_output(self):
        runner = _make_runner()
        clock = _ActivityClock()
        buf = []  # no output yet

        def produce():
            time.sleep(0.2)
            buf.append("now rendering")
            clock.update()

        threading.Thread(target=produce, daemon=True).start()
        t0 = time.time()
        runner._await_input_ready(buf, clock, ready_timeout=5.0, settle=0.05)
        elapsed = time.time() - t0
        # Must have waited for output to appear (>=0.2s) but not the full cap.
        assert 0.2 <= elapsed < 4.0

    def test_await_input_ready_caps_at_timeout_without_output(self):
        runner = _make_runner()
        clock = _ActivityClock()
        buf = []  # output never appears
        t0 = time.time()
        runner._await_input_ready(buf, clock, ready_timeout=0.2, settle=0.05)
        elapsed = time.time() - t0
        # Proceeds best-effort once the readiness window elapses.
        assert 0.2 <= elapsed < 2.0

    def test_feed_prompt_swallows_errors(self):
        runner = _make_runner()

        class Boom:
            def send(self, s):
                raise RuntimeError("pipe closed")

        # Must not raise.
        runner._feed_prompt(Boom(), "x", ready_delay=0)

    def test_drain_pty_reads_until_eof_and_updates_activity(self):
        runner = _make_runner()
        fake = FakePty(chunks=["foo", "bar"], die_when_empty=True)
        activity = _ActivityClock()
        before = activity.last()
        time.sleep(0.01)
        stop = threading.Event()
        buf = []
        outputs = []
        runner._drain_pty(
            fake,
            activity,
            stop,
            buf,
            on_output=outputs.append,
            on_activity=None,
        )
        assert "".join(buf) == "foobar"
        assert outputs == ["foo", "bar"]
        assert activity.last() > before

    def test_drain_pty_stops_on_stop_event(self):
        runner = _make_runner()
        fake = FakePty(chunks=[], die_when_empty=False)  # never dies
        activity = _ActivityClock()
        stop = threading.Event()
        buf = []

        t = threading.Thread(
            target=runner._drain_pty,
            args=(fake, activity, stop, buf, None, None),
            daemon=True,
        )
        t.start()
        time.sleep(0.05)
        stop.set()
        t.join(timeout=3)
        assert not t.is_alive()


# =============================================================================
# _terminate — process-group kill + reap
# =============================================================================


class TestTerminate:
    def test_terminate_kills_group_and_closes(self, no_kill):
        runner = _make_runner()
        fake = FakePty()  # stays alive until close()
        runner._terminate(fake)
        sigs = [s for (_pgid, s) in no_kill]
        assert signal.SIGTERM in sigs
        assert signal.SIGKILL in sigs  # still alive after SIGTERM (killpg mocked)
        assert fake.closed is True

    def test_terminate_none_is_noop(self):
        runner = _make_runner()
        runner._terminate(None)  # must not raise

    def test_terminate_swallows_lookup_error(self, monkeypatch):
        runner = _make_runner()
        fake = FakePty()

        def boom_getpgid(pid):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "getpgid", boom_getpgid)
        runner._terminate(fake)  # must not raise
        assert fake.closed is True


# =============================================================================
# Lifecycle — run_with_monitor
# =============================================================================


class TestLifecycle:
    def test_normal_exit_captures_output(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake = FakePty(chunks=["hello\n"], die_when_empty=True, exitstatus=0)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[])

        assert isinstance(result, MonitoredResult)
        assert result.returncode == 0
        assert result.success is True
        assert result.output.startswith("=== Command: claude ===")
        assert "hello" in result.output
        # Reclamation happened.
        assert fake.closed is True

    def test_wall_timeout_kills_and_returns_124(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake = FakePty(chunks=[], die_when_empty=False)  # never finishes
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[], wall_timeout=0.2)

        assert result.returncode == 124
        assert result.success is False
        assert "Wall timeout" in result.output
        assert fake.closed is True

    def test_inactivity_timeout_kills_and_returns_124(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake = FakePty(chunks=[], die_when_empty=False)  # silent + alive
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(
            args=[], wall_timeout=None, inactivity_timeout=0
        )

        assert result.returncode == 124
        assert "inactivity timeout" in result.output.lower()
        assert fake.closed is True

    def test_finally_reclaims_on_spawn_failure(self, monkeypatch):
        runner = _make_runner()

        def boom_spawn(*a, **k):
            raise RuntimeError("no pty available")

        monkeypatch.setattr(runner, "_spawn_pty", boom_spawn)
        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[])
        assert result.returncode == 127
        assert result.success is False

    def test_threads_joined_no_leak(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake = FakePty(chunks=["x\n"], die_when_empty=True)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        before = {t.name for t in threading.enumerate()}
        runner.build_call_args("prompt", read_only=False)
        runner.run_with_monitor(args=[])
        # Give daemon threads a beat to wind down after join timeouts.
        time.sleep(0.1)
        after = {t.name for t in threading.enumerate()}
        leaked = {
            n for n in (after - before)
            if "claude-interactive" in n
        }
        assert leaked == set()

    def test_keyboard_interrupt_preserves_partial_output(
        self, monkeypatch, no_kill
    ):
        runner = _make_runner()
        fake = FakePty(chunks=[], die_when_empty=False)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        # Inject a KeyboardInterrupt from the supervisor's sleep.
        real_sleep = time.sleep
        state = {"raised": False}

        def fake_sleep(s):
            if not state["raised"]:
                state["raised"] = True
                raise KeyboardInterrupt()
            real_sleep(s)

        monkeypatch.setattr(time, "sleep", fake_sleep)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[])
        assert result.interrupted is True
        assert result.returncode == -2
        assert fake.closed is True

    def test_run_returns_completed_process(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake = FakePty(chunks=["done\n"], die_when_empty=True, exitstatus=0)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        cp = runner.run(args=[])
        assert cp.returncode == 0
        assert "done" in cp.stdout


# =============================================================================
# Degraded-mode (no-watcher) meaningful-content inactivity signal
# =============================================================================


class _SpinnerPty(FakePty):
    """A FakePty that re-renders a cosmetic spinner footer forever (alive).

    Simulates the interactive TUI's behavior of re-drawing its animated
    spinner / footer on the PTY roughly once a second even when the turn is
    finished or stalled — the noise that must NOT keep the inactivity clock
    fresh in degraded (no-transcript) mode.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        super().__init__(chunks=[], die_when_empty=False)
        self._i = 0

    def read_nonblocking(self, size=1, timeout=None):
        g = self._FRAMES[self._i % len(self._FRAMES)]
        self._i += 1
        # Carriage-return overwrite + clear-line + glyph + volatile counters.
        return (
            f"\r\x1b[2K{g} Thinking… "
            f"({self._i}s · {self._i * 7} tokens · esc to interrupt)"
        )


class TestMeaningfulContentTracker:
    def test_normalize_strips_ansi_and_collapses_digits(self):
        lines = _normalize_pty_lines(
            "\x1b[2K\r⠋ Thinking… (12s · 340 tokens)\n"
        )
        assert lines == ["⠋ Thinking… (#s · # tokens)"]

    def test_blank_and_pure_ansi_lines_dropped(self):
        assert _normalize_pty_lines("\x1b[2K\r\n   \n") == []

    def test_cosmetic_rerenders_do_not_advance_clock(self):
        tr = _MeaningfulContentTracker()
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        buf = [
            f"\r\x1b[2K{g} Thinking… ({i}s · {i * 10} tokens)"
            for i, g in enumerate(frames * 3)
        ]
        tr.scan(buf)
        t_after_first = tr.clock.last()
        time.sleep(0.02)
        # Further re-renders: same normalized frames, different volatile digits.
        buf.extend(
            f"\r\x1b[2K{g} Thinking… ({i + 999}s · {i} tokens)"
            for i, g in enumerate(frames * 3)
        )
        tr.scan(buf)
        assert tr.clock.last() == t_after_first

    def test_genuinely_new_content_advances_clock(self):
        tr = _MeaningfulContentTracker()
        buf = ["first real line of output\n"]
        tr.scan(buf)
        t1 = tr.clock.last()
        time.sleep(0.02)
        buf.append("a genuinely different second line\n")
        tr.scan(buf)
        assert tr.clock.last() > t1

    def test_partial_line_held_until_terminator(self):
        tr = _MeaningfulContentTracker()
        buf = ["no terminator yet"]
        tr.scan(buf)
        t0 = tr.clock.last()
        # Without a line terminator the fragment is held back, not counted.
        time.sleep(0.02)
        buf.append(" continues and ends\n")
        tr.scan(buf)
        assert tr.clock.last() > t0

    def test_degraded_inactivity_fires_despite_cosmetic_rerenders(
        self, monkeypatch, no_kill
    ):
        # Regression: an interactive TUI re-renders its spinner forever and
        # never self-exits.  With wall_timeout=None (what LLMCaller passes) and
        # no watcher (no cwd), the supervisor must still escape via the
        # inactivity timeout — the cosmetic re-renders must not keep it fresh.
        runner = _make_runner()
        fake = _SpinnerPty()
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(
            args=[], cwd=None, wall_timeout=None, inactivity_timeout=0.3
        )
        assert result.returncode == 124
        assert "inactivity timeout" in result.output.lower()
        assert fake.closed is True


# =============================================================================
# detect_infra_error — baseline classification
# =============================================================================


class TestDetectInfraError:
    def test_success_is_none(self):
        runner = _make_runner()
        assert runner.detect_infra_error(0, "all good", "") == InfraErrorType.NONE

    def test_timeout_124(self):
        runner = _make_runner()
        assert (
            runner.detect_infra_error(124, "", "") == InfraErrorType.TIMEOUT
        )

    def test_usage_limit(self):
        runner = _make_runner()
        out = "some output\nyou've hit your limit, try later\n"
        assert (
            runner.detect_infra_error(1, out, "") == InfraErrorType.USAGE_LIMIT
        )

    def test_usage_limit_precedence_over_timeout(self):
        runner = _make_runner()
        # rc 124 but output has a usage-limit keyword → USAGE_LIMIT wins.
        assert (
            runner.detect_infra_error(124, "rate limit reached", "")
            == InfraErrorType.USAGE_LIMIT
        )

    def test_ordinary_failure_is_none(self):
        runner = _make_runner()
        assert (
            runner.detect_infra_error(1, "syntax error in task", "")
            == InfraErrorType.NONE
        )


# =============================================================================
# Result dataclasses
# =============================================================================


class TestResultDataclasses:
    def test_monitored_result_success_property(self):
        ok = MonitoredResult(0, "out", "claude", 0, False)
        bad = MonitoredResult(1, "out", "claude", 0, False)
        assert ok.success is True
        assert bad.success is False

    def test_monitored_result_fields(self):
        r = MonitoredResult(
            returncode=124,
            output="=== Command: claude ===\nx",
            cmd_used="claude",
            cmd_index=0,
            was_retry=False,
            interrupted=True,
            stderr_tail="tail",
        )
        assert r.cmd_index == 0
        assert r.was_retry is False
        assert r.interrupted is True
        assert r.stderr_tail == "tail"

    def test_single_run_result_defaults(self):
        r = _SingleRunResult(0, "o", True, False)
        assert r.interrupted is False
        assert r.stderr_tail == ""


# =============================================================================
# G2 — JSONL transcript watching & parsing
# =============================================================================


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _assistant_record(cwd="/proj", text="hi", model="claude-opus-4-8",
                      stop_reason="end_turn", usage=None, content=None):
    msg = {
        "model": model,
        "role": "assistant",
        "content": content if content is not None else [
            {"type": "text", "text": text}
        ],
        "stop_reason": stop_reason,
    }
    if usage is not None:
        msg["usage"] = usage
    return {
        "type": "assistant",
        "cwd": cwd,
        "sessionId": "sess-abc",
        "uuid": "u1",
        "message": msg,
    }


class TestMungeCwd:
    def test_known_path(self):
        assert munge_cwd("/data/cre/workspace/se3.0") == "-data-cre-workspace-se3-0"

    def test_dots_and_separators_become_dash(self):
        assert munge_cwd("/a.b/c_d") == "-a-b-c-d"

    def test_accepts_path_object(self):
        assert munge_cwd(Path("/x/y")) == "-x-y"

    def test_alphanumerics_preserved(self):
        assert munge_cwd("/Foo123/Bar") == "-Foo123-Bar"


class TestProjectsDir:
    def test_default_is_home_claude_projects(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        d = claude_projects_dir()
        assert d.name == "projects"
        assert d.parent.name == ".claude"

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        d = claude_projects_dir()
        assert d == (tmp_path / "cfg" / "projects")


class TestLocateSessionFile:
    def test_diff_finds_new_file(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        munged = munge_cwd(cwd)
        target_dir = projects / munged
        # pre-existing file
        old = target_dir / "old.jsonl"
        _write_jsonl(old, [_assistant_record(cwd=cwd)])
        pre = snapshot_session_files(projects)
        # new file created by "this launch"
        new = target_dir / "new.jsonl"
        _write_jsonl(new, [_assistant_record(cwd=cwd)])
        located = locate_session_file(projects, pre, cwd)
        assert located == new

    def test_no_new_file_returns_none(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        f = projects / munge_cwd(cwd) / "a.jsonl"
        _write_jsonl(f, [_assistant_record(cwd=cwd)])
        pre = snapshot_session_files(projects)
        assert locate_session_file(projects, pre, cwd) is None

    def test_concurrent_flow_isolated_by_cwd(self, tmp_path):
        # Two new files appear in DIFFERENT munged dirs; only the one whose
        # records carry our cwd is selected.
        projects = tmp_path / "projects"
        my_cwd = "/proj/mine"
        other_cwd = "/proj/other"
        pre = snapshot_session_files(projects)  # empty
        mine = projects / munge_cwd(my_cwd) / "mine.jsonl"
        other = projects / munge_cwd(other_cwd) / "other.jsonl"
        _write_jsonl(mine, [_assistant_record(cwd=my_cwd)])
        _write_jsonl(other, [_assistant_record(cwd=other_cwd)])
        located = locate_session_file(projects, pre, my_cwd)
        assert located == mine

    def test_munged_dir_missing_falls_back_to_tree_with_cwd_match(self, tmp_path):
        # Simulate the munge rule "failing": the file lands in an unexpected
        # directory, but its cwd field still matches.
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        pre = snapshot_session_files(projects)
        weird = projects / "unexpected-dir-name" / "s.jsonl"
        _write_jsonl(weird, [_assistant_record(cwd=cwd)])
        located = locate_session_file(projects, pre, cwd)
        assert located == weird

    def test_fallback_requires_positive_cwd_match(self, tmp_path):
        # A new file outside the munged dir whose cwd does NOT match is rejected.
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        pre = snapshot_session_files(projects)
        weird = projects / "unexpected" / "s.jsonl"
        _write_jsonl(weird, [_assistant_record(cwd="/somewhere/else")])
        assert locate_session_file(projects, pre, cwd) is None

    def test_primary_dir_picks_most_recent(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        target = projects / munge_cwd(cwd)
        pre = snapshot_session_files(projects)
        a = target / "a.jsonl"
        b = target / "b.jsonl"
        _write_jsonl(a, [_assistant_record(cwd=cwd)])
        _write_jsonl(b, [_assistant_record(cwd=cwd)])
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        assert locate_session_file(projects, pre, cwd) == b

    def test_session_id_binds_exact_file(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        target = projects / munge_cwd(cwd)
        pre = snapshot_session_files(projects)
        sid = "11111111-1111-4111-8111-111111111111"
        mine = target / f"{sid}.jsonl"
        _write_jsonl(mine, [_assistant_record(cwd=cwd)])
        assert locate_session_file(projects, pre, cwd, sid) == mine

    def test_session_id_isolates_concurrent_same_cwd_flows(self, tmp_path):
        # Two flows with the SAME cwd write two transcripts into the same munged
        # dir; both carry the matching cwd so a cwd-field check cannot tell them
        # apart.  The per-launch session id binds each flow to its own file —
        # even when the *other* flow's file was written more recently.
        projects = tmp_path / "projects"
        cwd = "/proj/shared"
        target = projects / munge_cwd(cwd)
        pre = snapshot_session_files(projects)
        my_sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        other_sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        mine = target / f"{my_sid}.jsonl"
        other = target / f"{other_sid}.jsonl"
        _write_jsonl(mine, [_assistant_record(cwd=cwd)])
        _write_jsonl(other, [_assistant_record(cwd=cwd)])
        # The sibling flow's file is the most recently modified one.
        os.utime(mine, (1000, 1000))
        os.utime(other, (2000, 2000))
        # Each flow latches onto its own session id, never the sibling's.
        assert locate_session_file(projects, pre, cwd, my_sid) == mine
        assert locate_session_file(projects, pre, cwd, other_sid) == other

    def test_session_id_absent_until_file_appears(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        pre = snapshot_session_files(projects)
        sid = "22222222-2222-4222-8222-222222222222"
        # No transcript yet — refuse to latch onto any sibling file.
        other = projects / munge_cwd(cwd) / "other.jsonl"
        _write_jsonl(other, [_assistant_record(cwd=cwd)])
        assert locate_session_file(projects, pre, cwd, sid) is None

    def test_session_id_tolerates_munged_dir_drift(self, tmp_path):
        # The transcript lands under an unexpected munged dir, but its unique
        # stem still identifies it.
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        pre = snapshot_session_files(projects)
        sid = "33333333-3333-4333-8333-333333333333"
        weird = projects / "unexpected-dir" / f"{sid}.jsonl"
        _write_jsonl(weird, [_assistant_record(cwd=cwd)])
        assert locate_session_file(projects, pre, cwd, sid) == weird


class TestTailNewRecords:
    def test_incremental_read(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
        recs, cur = tail_new_records(f, 0)
        assert recs == [{"a": 1}]
        assert cur > 0
        # append more, read only the new part
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"b": 2}) + "\n")
        recs2, cur2 = tail_new_records(f, cur)
        assert recs2 == [{"b": 2}]
        assert cur2 > cur

    def test_partial_line_not_consumed(self, tmp_path):
        f = tmp_path / "t.jsonl"
        # complete line + a partial (no trailing newline)
        f.write_text(json.dumps({"a": 1}) + "\n" + '{"b": 2', encoding="utf-8")
        recs, cur = tail_new_records(f, 0)
        assert recs == [{"a": 1}]
        # cursor sits right after the complete line, before the partial
        # complete the partial line
        with open(f, "a", encoding="utf-8") as fh:
            fh.write('}\n')
        recs2, cur2 = tail_new_records(f, cur)
        assert recs2 == [{"b": 2}]

    def test_malformed_lines_skipped(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("not json\n" + json.dumps({"ok": 1}) + "\n", encoding="utf-8")
        recs, _ = tail_new_records(f, 0)
        assert recs == [{"ok": 1}]

    def test_truncation_resets_cursor(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
        recs, cur = tail_new_records(f, 0)
        # shrink the file below cursor
        f.write_text(json.dumps({"z": 9}) + "\n", encoding="utf-8")
        recs2, cur2 = tail_new_records(f, cur if cur < f.stat().st_size else cur + 100)
        assert {"z": 9} in recs2

    def test_missing_file_returns_cursor(self, tmp_path):
        recs, cur = tail_new_records(tmp_path / "nope.jsonl", 5)
        assert recs == []
        assert cur == 5


class TestToStreamJson:
    def test_assistant_message_extracted(self):
        rec = _assistant_record(usage={"input_tokens": 3, "output_tokens": 1})
        nd = to_stream_json_ndjson(rec)
        obj = json.loads(nd)
        assert obj["type"] == "assistant"
        assert obj["message"] == rec["message"]
        # wrapper fields stripped
        assert "cwd" not in obj
        assert "sessionId" not in obj

    def test_user_with_tool_result_kept(self):
        rec = {
            "type": "user",
            "cwd": "/p",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                ],
            },
        }
        nd = to_stream_json_ndjson(rec)
        obj = json.loads(nd)
        assert obj["type"] == "user"
        assert obj["message"]["content"][0]["type"] == "tool_result"

    def test_user_plain_text_dropped(self):
        rec = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        assert to_stream_json_ndjson(rec) is None

    def test_irrelevant_types_dropped(self):
        assert to_stream_json_ndjson({"type": "summary", "summary": "x"}) is None
        assert to_stream_json_ndjson({"type": "system", "subtype": "hook"}) is None
        assert to_stream_json_ndjson(
            {"type": "file-history-snapshot"}
        ) is None

    def test_sidechain_dropped(self):
        rec = _assistant_record()
        rec["isSidechain"] = True
        assert to_stream_json_ndjson(rec) is None

    def test_result_passthrough(self):
        rec = {"type": "result", "usage": {"input_tokens": 5}, "total_cost_usd": 0}
        nd = to_stream_json_ndjson(rec)
        assert json.loads(nd)["type"] == "result"

    def test_non_dict_dropped(self):
        assert to_stream_json_ndjson(["not", "a", "dict"]) is None

    def test_output_parsed_by_upstream(self):
        # Round-trip through the real upstream parsers.
        from tianluo.engine.chat_history import (
            parse_usage_from_ndjson,
            extract_assistant_text,
        )
        rec = _assistant_record(
            text="hello there",
            usage={"input_tokens": 12, "output_tokens": 4},
        )
        nd = to_stream_json_ndjson(rec)
        result = synthesize_result_line(
            {"input_tokens": 12, "output_tokens": 4}, 0.0
        )
        combined = nd + "\n" + result
        assert "hello there" in extract_assistant_text(combined)
        usage = parse_usage_from_ndjson(combined)
        assert usage["input_tokens"] == 12
        assert usage["output_tokens"] == 4

    def test_tool_use_feeds_last_touched_files(self):
        from tianluo.engine.llm_caller import StreamJSONTracker

        rec = _assistant_record(
            stop_reason="tool_use",
            content=[
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Read",
                    "input": {"file_path": "/proj/foo.py"},
                }
            ],
        )
        nd = to_stream_json_ndjson(rec)
        tracker = StreamJSONTracker(project_root=Path("/proj"))
        tracker.process_line(nd)
        assert "foo.py" in tracker.touched_files


class TestUsageExtraction:
    def test_four_tokens_from_message_usage(self):
        rec = _assistant_record(
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 10,
            }
        )
        u = extract_usage_from_record(rec)
        assert u == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 10,
        }

    def test_missing_fields_default_zero(self):
        rec = _assistant_record(usage={"input_tokens": 7})
        u = extract_usage_from_record(rec)
        assert u["input_tokens"] == 7
        assert u["output_tokens"] == 0
        assert u["cache_creation_input_tokens"] == 0
        assert u["cache_read_input_tokens"] == 0

    def test_no_usage_all_zero(self):
        u = extract_usage_from_record(_assistant_record())
        assert all(u[k] == 0 for k in USAGE_TOKEN_KEYS)

    def test_flat_top_level_usage(self):
        rec = {"type": "result", "usage": {"input_tokens": 9, "output_tokens": 2}}
        u = extract_usage_from_record(rec)
        assert u["input_tokens"] == 9
        assert u["output_tokens"] == 2

    def test_malformed_usage_swallowed(self):
        rec = {"type": "assistant", "message": {"usage": "not a dict"}}
        u = extract_usage_from_record(rec)
        assert all(u[k] == 0 for k in USAGE_TOKEN_KEYS)

    def test_cache_creation_ttl_split_preserved_through_synthesized_result(self):
        from tianluo.usage import parse_usage_record

        rec = _assistant_record(
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 25,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 15,
                    "ephemeral_1h_input_tokens": 10,
                },
            }
        )
        u = extract_usage_from_record(rec)
        assert u["cache_creation_5m_input_tokens"] == 15
        assert u["cache_creation_1h_input_tokens"] == 10
        line = synthesize_result_line(u, 0.5, provider_session_id="s1")
        record = parse_usage_record(line, call_id="c1", provider="anthropic")
        # The TTL split reaches the unified parser instead of collapsing into
        # generic cache creation, so the two TTL rates stay distinguishable.
        assert record.cache_creation_5m_input_tokens == 15
        assert record.cache_creation_1h_input_tokens == 10
        assert record.cache_creation_input_tokens == 0  # 25 - 15 - 10

    def test_ttl_split_absent_keeps_flat_shape(self):
        u = extract_usage_from_record(_assistant_record(usage={"input_tokens": 7}))
        assert "cache_creation_5m_input_tokens" not in u
        assert "cache_creation_1h_input_tokens" not in u

    def test_synthesize_result_has_usage_and_cost(self):
        line = synthesize_result_line(
            {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 4,
            },
            total_cost_usd=0.5,
        )
        obj = json.loads(line)
        assert obj["type"] == "result"
        assert obj["usage"]["input_tokens"] == 1
        assert obj["total_cost_usd"] == 0.5

    def test_synthesize_result_omits_unreported_cost(self):
        obj = json.loads(synthesize_result_line({}))
        assert "total_cost_usd" not in obj
        assert obj["usage"] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_synthesize_result_omits_unreported_usage(self):
        obj = json.loads(synthesize_result_line(None))
        assert "usage" not in obj
        assert "total_cost_usd" not in obj

    def test_reused_interactive_session_has_delta_tokens_and_cumulative_cost(self):
        from tianluo.usage import aggregate_usage_records, parse_usage_record

        first = parse_usage_record(
            synthesize_result_line(
                {"input_tokens": 10, "output_tokens": 2},
                0.1,
                provider_session_id="interactive-shared",
                usage_event_id="interactive-turn-1",
            ),
            call_id="interactive-call-1",
        )
        second = parse_usage_record(
            synthesize_result_line(
                {"input_tokens": 20, "output_tokens": 3},
                0.25,
                provider_session_id="interactive-shared",
                usage_event_id="interactive-turn-2",
            ),
            call_id="interactive-call-2",
        )
        aggregate = aggregate_usage_records([first, second])
        assert aggregate.logical_input_tokens == 30
        assert aggregate.output_tokens == 5
        assert aggregate.actual_cost_usd == pytest.approx(0.25)

    def test_real_terminal_cost_record_reuses_accumulated_message_usage(
        self, tmp_path
    ):
        transcript = tmp_path / "interactive-cost.jsonl"
        records = [
            {
                "type": "assistant",
                "message": {
                    "model": "claude-test-model",
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                },
            },
            {
                "type": "result",
                "result": "done",
                "total_cost_usd": 0.02,
            },
        ]
        transcript.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        watcher = SessionTranscriptWatcher(
            tmp_path,
            projects_dir=tmp_path,
            session_id="interactive-cost-session",
        )
        watcher.path = transcript
        output = watcher.poll()
        assert watcher.synthesize_result() is None

        from tianluo.usage import parse_usage_record

        usage = parse_usage_record("\n".join(output))
        assert usage.uncached_input_tokens == 12
        assert usage.output_tokens == 3
        assert usage.actual_cost_usd == pytest.approx(0.02)
        assert usage.provider_session_id == "interactive-cost-session"

    def test_multi_result_transcript_emits_per_result_deltas(self, tmp_path):
        # A user replying before the turn-silence window closes yields several
        # result records in one run.  Each must carry only the tokens
        # accumulated since the previous emission — re-emitting the running
        # session total as event_delta would double-count earlier turns.
        from tianluo.usage import parse_usage_record

        transcript = tmp_path / "interactive-multi-result.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(record)
                for record in [
                    {
                        "type": "assistant",
                        "message": {
                            "model": "claude-test-model",
                            "content": [{"type": "text", "text": "turn 1"}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                        },
                    },
                    {"type": "result", "result": "turn 1"},
                    {
                        "type": "assistant",
                        "message": {
                            "model": "claude-test-model",
                            "content": [{"type": "text", "text": "turn 2"}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 50, "output_tokens": 5},
                        },
                    },
                    {"type": "result", "result": "turn 2"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        watcher = SessionTranscriptWatcher(
            tmp_path,
            projects_dir=tmp_path,
            session_id="interactive-multi-session",
        )
        watcher.path = transcript
        output = watcher.poll()
        assert watcher.synthesize_result() is None

        result_lines = [
            json.loads(line)
            for line in output
            if json.loads(line).get("type") == "result"
        ]
        assert [line["usage"]["input_tokens"] for line in result_lines] == [100, 50]
        assert [line["usage"]["output_tokens"] for line in result_lines] == [10, 5]

        usage = parse_usage_record(
            "\n".join(output), call_id="multi-turn", provider="anthropic"
        )
        assert usage.logical_input_tokens == 150
        assert usage.output_tokens == 15

    def test_regressed_cumulative_snapshot_marks_record_partial(self, tmp_path):
        # A second result whose usage snapshot went backwards must mark the
        # emitted delta record partial with a diagnostic — a silent all-zero
        # delta is legal-looking and would hide the anomaly from the
        # downstream aggregator entirely.
        from tianluo.usage import UsageStatus, parse_usage_record

        transcript = tmp_path / "interactive-regressed.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(record)
                for record in [
                    {
                        "type": "assistant",
                        "message": {
                            "model": "claude-test-model",
                            "content": [{"type": "text", "text": "turn 1"}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                        },
                    },
                    {"type": "result", "result": "turn 1"},
                    # Re-reports a LOWER cumulative snapshot.
                    {
                        "type": "result",
                        "result": "turn 2",
                        "usage": {"input_tokens": 40, "output_tokens": 4},
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        watcher = SessionTranscriptWatcher(
            tmp_path,
            projects_dir=tmp_path,
            session_id="interactive-regressed-session",
        )
        watcher.path = transcript
        output = watcher.poll()

        result_lines = [
            json.loads(line)
            for line in output
            if json.loads(line).get("type") == "result"
        ]
        second = result_lines[1]
        assert second["usage"]["input_tokens"] == 0
        assert second.get("partial") is True
        assert any(
            "non-monotonic" in diagnostic
            for diagnostic in second.get("diagnostics", [])
        )

        usage = parse_usage_record(
            "\n".join(output), call_id="regressed-session"
        )
        assert usage.usage_status == UsageStatus.PARTIAL
        assert any(
            "non-monotonic" in diagnostic for diagnostic in usage.diagnostics
        )

    def test_real_interactive_fixture_emits_delta_usage_with_session_metadata(
        self, tmp_path, capsys
    ):
        from tianluo.engine.llm_caller import StreamJSONTracker
        from tianluo.usage import UsageStatus, parse_usage_record

        fixture = (
            Path(__file__).parent / "fixtures" / "usage" / "claude_interactive.jsonl"
        )
        transcript = tmp_path / "interactive-provider-session.jsonl"
        transcript.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        watcher = SessionTranscriptWatcher(
            tmp_path,
            projects_dir=tmp_path,
            session_id="interactive-provider-session",
        )
        watcher.path = transcript
        output_lines = watcher.poll()
        synthesized = watcher.synthesize_result()
        assert synthesized is not None
        output_lines.append(synthesized)
        raw = "\n".join(output_lines)

        parsed = parse_usage_record(
            raw,
            call_id="interactive-fixture",
            runner_type="claude-interactive",
        )
        tracker = StreamJSONTracker(
            call_id="interactive-fixture",
            usage_attempt=0,
            runner_type="claude-interactive",
        )
        for line in output_lines:
            tracker.process_line(line)
        capsys.readouterr()

        assert tracker.usage_record == parsed
        assert parsed.provider == "anthropic"
        assert parsed.provider_session_id == "interactive-provider-session"
        # Anthropic's input_tokens EXCLUDES the cache categories (the real
        # cache-heavy shape: single-digit uncached input beside tens of
        # thousands of cached tokens), so the logical input total is their sum.
        assert parsed.uncached_input_tokens == 7
        assert parsed.cache_read_input_tokens == 14036 + 38452
        assert parsed.cache_creation_input_tokens == 24399
        assert parsed.logical_input_tokens == 7 + 14036 + 38452 + 24399
        assert parsed.usage_status == UsageStatus.AVAILABLE
        assert parsed.output_tokens == 27
        assert parsed.actual_cost_usd is None


class TestTurnComplete:
    def test_jsonl_idle_terminal_completes(self):
        rec = _assistant_record(stop_reason="end_turn")
        assert turn_complete(True, rec) is True

    def test_jsonl_still_writing_not_complete(self):
        rec = _assistant_record(stop_reason="end_turn")
        assert turn_complete(False, rec) is False

    def test_pty_idleness_is_not_required(self):
        # Regression: PTY activity must NOT gate turn completion — the TUI keeps
        # re-rendering its footer/spinner after the turn ends.  As long as the
        # JSONL is idle and the last meaningful record is terminal, the turn is
        # complete regardless of any PTY chatter.
        rec = _assistant_record(stop_reason="end_turn")
        assert turn_complete(True, rec) is True

    def test_tool_use_stop_reason_not_terminal(self):
        rec = _assistant_record(stop_reason="tool_use")
        assert turn_complete(True, rec) is False

    def test_user_record_not_terminal(self):
        rec = {"type": "user", "message": {"content": []}}
        assert turn_complete(True, rec) is False

    def test_result_record_is_terminal(self):
        assert turn_complete(True, {"type": "result"}) is True

    def test_none_last_record_not_complete(self):
        assert turn_complete(True, None) is False

    def test_stop_sequence_and_max_tokens_terminal(self):
        for sr in ("stop_sequence", "max_tokens"):
            rec = _assistant_record(stop_reason=sr)
            assert turn_complete(True, rec) is True


class TestSessionTranscriptWatcher:
    def test_snapshot_locate_poll_flow(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/proj/here"
        target = projects / munge_cwd(cwd)
        target.mkdir(parents=True, exist_ok=True)

        watcher = SessionTranscriptWatcher(cwd=cwd, projects_dir=projects)
        watcher.snapshot()

        # Launch writes a new transcript file.
        f = target / "live.jsonl"
        _write_jsonl(
            f,
            [
                _assistant_record(
                    cwd=cwd,
                    usage={"input_tokens": 10, "output_tokens": 4},
                )
            ],
        )
        assert watcher.locate() is True
        assert watcher.path == f

        lines = watcher.poll()
        # init (model) line + assistant line
        types = [json.loads(ln).get("type") for ln in lines]
        assert "init" in types
        assert "assistant" in types
        # usage accumulated
        assert watcher.usage["input_tokens"] == 10
        assert watcher.usage["output_tokens"] == 4
        assert watcher.line_count == 1

    def test_usage_accumulates_across_messages(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/p"
        target = projects / munge_cwd(cwd)
        watcher = SessionTranscriptWatcher(cwd=cwd, projects_dir=projects)
        watcher.snapshot()
        f = target / "s.jsonl"
        _write_jsonl(
            f,
            [
                _assistant_record(cwd=cwd, stop_reason="tool_use",
                                  usage={"input_tokens": 100, "output_tokens": 10}),
                _assistant_record(cwd=cwd, stop_reason="end_turn",
                                  usage={"input_tokens": 50, "output_tokens": 20}),
            ],
        )
        watcher.locate()
        watcher.poll()
        assert watcher.usage["input_tokens"] == 150
        assert watcher.usage["output_tokens"] == 30
        # synthesized result reflects the accumulated total
        res = json.loads(watcher.synthesize_result())
        assert res["usage"]["input_tokens"] == 150

    def test_init_emitted_only_once(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/p"
        target = projects / munge_cwd(cwd)
        watcher = SessionTranscriptWatcher(cwd=cwd, projects_dir=projects)
        watcher.snapshot()
        f = target / "s.jsonl"
        _write_jsonl(f, [_assistant_record(cwd=cwd), _assistant_record(cwd=cwd)])
        watcher.locate()
        lines = watcher.poll()
        init_count = sum(
            1 for ln in lines if json.loads(ln).get("type") == "init"
        )
        assert init_count == 1

    def test_carried_result_usage_is_emitted_as_increment(self, tmp_path):
        # A transcript ``result`` record that carries its own usage carries a
        # cumulative session snapshot. It must be re-emitted as the increment
        # beyond what earlier result records already accounted for — otherwise
        # the shared aggregator sums the snapshot on top of the earlier deltas
        # and every turn before it is double-counted.
        projects = tmp_path / "projects"
        cwd = "/p"
        target = projects / munge_cwd(cwd)
        watcher = SessionTranscriptWatcher(cwd=cwd, projects_dir=projects)
        watcher.snapshot()
        f = target / "s.jsonl"
        _write_jsonl(
            f,
            [
                _assistant_record(
                    cwd=cwd, stop_reason="end_turn",
                    usage={"input_tokens": 100, "output_tokens": 10},
                ),
                {"type": "result", "cwd": cwd, "sessionId": "sess-abc"},
                _assistant_record(
                    cwd=cwd, stop_reason="end_turn",
                    usage={"input_tokens": 50, "output_tokens": 5},
                ),
                {
                    "type": "result",
                    "cwd": cwd,
                    "sessionId": "sess-abc",
                    "usage": {"input_tokens": 150, "output_tokens": 15},
                },
                _assistant_record(
                    cwd=cwd, stop_reason="end_turn",
                    usage={"input_tokens": 20, "output_tokens": 5},
                ),
                {"type": "result", "cwd": cwd, "sessionId": "sess-abc"},
            ],
        )
        watcher.locate()
        lines = watcher.poll()
        results = [
            json.loads(ln) for ln in lines
            if json.loads(ln).get("type") == "result"
        ]
        assert len(results) == 3
        # Turn 1 delta, turn 2 snapshot-increment, turn 3 delta after snapshot.
        assert results[0]["usage"]["input_tokens"] == 100
        assert results[0]["usage"]["output_tokens"] == 10
        assert results[0]["usage"]["cache_creation_input_tokens"] == 0
        assert results[0]["usage"]["cache_read_input_tokens"] == 0
        assert results[1]["usage"]["input_tokens"] == 50
        assert results[1]["usage"]["output_tokens"] == 5
        assert results[2]["usage"]["input_tokens"] == 20
        assert results[2]["usage"]["output_tokens"] == 5
        assert results[1]["usage_semantics"] == "event_delta"
        # Every turn's tokens are aggregated exactly once: 100+50+20 / 10+5+5.
        assert sum(r["usage"]["input_tokens"] for r in results) == 170
        assert sum(r["usage"]["output_tokens"] for r in results) == 20

    def test_write_activity_tracks_mtime(self, tmp_path):
        projects = tmp_path / "projects"
        cwd = "/p"
        target = projects / munge_cwd(cwd)
        watcher = SessionTranscriptWatcher(cwd=cwd, projects_dir=projects)
        watcher.snapshot()
        f = target / "s.jsonl"
        _write_jsonl(f, [_assistant_record(cwd=cwd)])
        os.utime(f, (12345.0, 12345.0))
        watcher.locate()
        assert watcher.write_activity() == 12345.0


# =============================================================================
# G2 — run_with_monitor integration with the transcript watcher
# =============================================================================


class _FakeWatcher:
    """Stand-in for SessionTranscriptWatcher driving the supervisor loop."""

    def __init__(self, lines, last_record, result_line):
        self._lines = list(lines)
        self._last_record = last_record
        self._result_line = result_line
        self.path = None
        self.last_record = None
        self.last_meaningful_record = None
        self.snapshotted = False
        # Faithful to the real watcher: write_activity tracks the transcript
        # mtime, which is fresh right after records are written and only goes
        # stale once the JSONL stops growing.
        self._write_ts = 0.0

    def snapshot(self):
        self.snapshotted = True

    def locate(self):
        self.path = "fake-path"
        return True

    def poll(self):
        if self._lines:
            out, self._lines = self._lines, []
            self.last_record = self._last_record
            self.last_meaningful_record = self._last_record
            self._write_ts = time.time()
            return out
        return []

    def write_activity(self):
        # Recent right after a poll yielded records; left fixed afterwards so a
        # genuinely stalled transcript still goes idle (matching real mtime).
        return self._write_ts

    def synthesize_result(self):
        return self._result_line


class TestWatcherIntegration:
    def test_ndjson_output_and_turn_end(self, monkeypatch, no_kill):
        runner = _make_runner()
        # Process stays alive (interactive claude never self-exits after a turn).
        fake_pty = FakePty(chunks=[], die_when_empty=False)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        # Make the silence window zero so turn-end fires immediately.
        monkeypatch.setattr(cir, "TURN_SILENCE_WINDOW", 0.0)

        assistant = _assistant_record(
            text="done", usage={"input_tokens": 8, "output_tokens": 2}
        )
        nd = to_stream_json_ndjson(assistant)
        result_line = synthesize_result_line(
            {"input_tokens": 8, "output_tokens": 2}, 0.0
        )
        fake_watcher = _FakeWatcher(
            lines=[nd], last_record=assistant, result_line=result_line
        )
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: fake_watcher)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[], cwd=Path("/proj"))

        assert fake_watcher.snapshotted is True
        assert result.returncode == 0
        assert result.success is True
        # The returned output is the stream-json NDJSON, not raw PTY.
        assert "done" in result.output
        assert '"type": "result"' in result.output
        # Reclaimed even though the process was still alive at turn-end.
        assert fake_pty.closed is True

    def test_pty_rerender_after_terminal_does_not_hang(self, monkeypatch, no_kill):
        """Regression (high): once the transcript shows a terminal record and
        stops growing, the turn must complete even though the interactive TUI
        keeps emitting cosmetic PTY output (footer/spinner re-renders) that bumps
        the PTY activity clock on every read.  Before the fix, gating turn-end on
        PTY idleness meant the supervisor loop spun forever on a finished turn
        (and the same PTY chatter also kept the inactivity timer fresh)."""

        class _RerenderPty(FakePty):
            """Stays alive forever, emitting a re-render chunk on every read so
            the PTY activity clock never goes idle."""

            def __init__(self):
                super().__init__(chunks=[], die_when_empty=False)

            def isalive(self):
                return True

            def read_nonblocking(self, size=1, timeout=None):
                time.sleep(0.005)
                return "\x1b[2K footer re-render \r"

        runner = _make_runner()
        fake_pty = _RerenderPty()
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        # A small positive window so turn-end depends on JSONL idleness, not the
        # (never-idle) PTY clock.
        monkeypatch.setattr(cir, "TURN_SILENCE_WINDOW", 0.05)

        assistant = _assistant_record(
            text="done", stop_reason="end_turn",
            usage={"input_tokens": 8, "output_tokens": 2},
        )
        nd = to_stream_json_ndjson(assistant)
        fake_watcher = _FakeWatcher(
            lines=[nd], last_record=assistant,
            result_line=synthesize_result_line({"input_tokens": 8}, 0.0),
        )
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: fake_watcher)

        runner.build_call_args("prompt", read_only=False)

        holder = {}

        def _go():
            holder["result"] = runner.run_with_monitor(
                args=[], cwd=Path("/proj"),
                wall_timeout=None, inactivity_timeout=600,
            )

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        t.join(timeout=10)
        # The loop must have returned promptly despite the relentless PTY output.
        assert not t.is_alive(), "run_with_monitor hung on a finished turn"
        result = holder["result"]
        assert result.returncode == 0
        assert result.success is True
        assert fake_pty.closed is True

    def test_no_watcher_falls_back_to_raw_pty(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake_pty = FakePty(chunks=["raw output\n"], die_when_empty=True)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: None)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[], cwd=Path("/proj"))
        assert "raw output" in result.output

    def test_make_watcher_none_without_cwd(self):
        runner = _make_runner()
        assert runner._make_watcher(None) is None

    def test_make_watcher_propagates_session_id(self):
        runner = _make_runner()
        runner._session_id = "feedface-feed-4ace-8ace-feedfacefeed"
        watcher = runner._make_watcher(Path("/proj"))
        assert watcher is not None
        assert watcher.session_id == "feedface-feed-4ace-8ace-feedfacefeed"

    def test_compose_output_prefers_ndjson(self):
        out = ClaudeInteractiveRunner._compose_output(
            ['{"type":"assistant"}'], ["raw pty noise"]
        )
        assert out == '{"type":"assistant"}'

    def test_compose_output_falls_back_to_raw(self):
        out = ClaudeInteractiveRunner._compose_output([], ["raw pty noise"])
        assert out == "raw pty noise"

    def test_usage_limit_in_pty_detected_after_ndjson_captured(
        self, monkeypatch, no_kill
    ):
        """Regression: a usage/rate-limit message renders in the PTY stream
        (not the transcript JSONL).  Once at least one assistant line has been
        written to the transcript (``ndjson_buffer`` non-empty), ``_compose_output``
        reduces the returned output to the NDJSON alone, dropping the PTY text.
        Usage-limit detection must still draw on the PTY buffer so the limit is
        caught, the marker propagated, and ``detect_infra_error`` returns
        USAGE_LIMIT for agent rotation."""
        runner = _make_runner()

        class _LimitThenDiePty:
            """Renders a usage-limit line, then reports the process dead with a
            non-zero exit code after the supervisor has polled the watcher."""

            def __init__(self):
                self._chunks = ["You've hit your limit. Try again later.\n"]
                self._deadline = None
                self.exitstatus = 1
                self.signalstatus = None
                self.pid = 999999
                self.sent = []
                self.closed = False

            def isalive(self):
                if self._deadline is None:
                    self._deadline = time.time() + 0.3
                return time.time() < self._deadline

            def read_nonblocking(self, size=1, timeout=None):
                if self._chunks:
                    return self._chunks.pop(0)
                raise pexpect.EOF("eof")

            def send(self, s):
                self.sent.append(s)
                return len(s)

            def sendline(self, s=""):
                self.sent.append(s + "\n")
                return len(s) + 1

            def close(self, force=False):
                self.closed = True

            def terminate(self, force=False):
                return True

        fake_pty = _LimitThenDiePty()
        # Non-terminal last_record so the turn never "completes" (which would
        # yield returncode 0) — the run ends via process death with exit 1.
        assistant = _assistant_record(text="partial work", stop_reason=None)
        nd = to_stream_json_ndjson(assistant)
        result_line = synthesize_result_line({}, 0.0)
        fake_watcher = _FakeWatcher(
            lines=[nd], last_record=assistant, result_line=result_line
        )
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: fake_watcher)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[], cwd=Path("/proj"))

        # ndjson was captured (assistant line present), yet the PTY-only limit
        # message was still detected and the marker propagated into the output.
        assert result.returncode == 1
        assert '"type": "assistant"' in result.output  # NDJSON was captured
        assert "Usage limit detected" in result.output
        assert (
            runner.detect_infra_error(result.returncode, result.output, "")
            == InfraErrorType.USAGE_LIMIT
        )

    def test_usage_limit_via_inactivity_classified_not_timeout(
        self, monkeypatch, no_kill
    ):
        """Regression: a usage limit that leaves the interactive process alive at
        the input box writes the limit message only into the PTY stream and never
        a terminal JSONL record, so ``turn_complete`` never fires and the loop
        exits via the inactivity-timeout early return.  That early return must
        still scan the raw PTY buffer for the limit keywords so the failure is
        classified USAGE_LIMIT (for agent rotation) rather than masked as a
        generic inactivity TIMEOUT."""
        runner = _make_runner()
        # PTY renders the limit line then stays alive and silent (input box).
        fake_pty = FakePty(
            chunks=["You've hit your limit. Try again later.\n"],
            die_when_empty=False,
        )
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        monkeypatch.setattr(cir, "TURN_SILENCE_WINDOW", 1e9)  # never turn-complete

        # A non-terminal assistant line is written, but no terminal record — so
        # turn_complete can never fire; the loop exits only via inactivity.
        assistant = _assistant_record(text="partial", stop_reason=None)
        nd = to_stream_json_ndjson(assistant)
        fake_watcher = _FakeWatcher(
            lines=[nd], last_record=assistant, result_line=synthesize_result_line({})
        )
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: fake_watcher)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(
            args=[], cwd=Path("/proj"), wall_timeout=None, inactivity_timeout=0
        )

        assert result.returncode == 124
        assert "Usage limit detected" in result.output
        assert (
            runner.detect_infra_error(result.returncode, result.output, "")
            == InfraErrorType.USAGE_LIMIT
        )


class TestWatcherLastMeaningfulRecord:
    """Turn terminality is judged against the last *meaningful* record, not a
    benign trailing system / summary / file-history-snapshot line."""

    def _write_watcher(self, tmp_path, records):
        path = tmp_path / "sess.jsonl"
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )
        w = SessionTranscriptWatcher(cwd="/proj", projects_dir=tmp_path)
        w.path = path
        return w

    def test_trailing_system_record_does_not_block_turn_end(self, tmp_path):
        assistant = _assistant_record(text="done", stop_reason="end_turn")
        trailing = {"type": "system", "cwd": "/proj", "subtype": "hook"}
        w = self._write_watcher(tmp_path, [assistant, trailing])
        w.poll()

        # last_record holds the trailing non-terminal system line ...
        assert w.last_record == trailing
        # ... but last_meaningful_record holds the terminal assistant, so the
        # composite turn-end signal still fires.
        assert _is_terminal_record(w.last_record) is False
        assert _is_terminal_record(w.last_meaningful_record) is True
        assert turn_complete(True, w.last_meaningful_record) is True

    def test_file_history_snapshot_after_end_turn_ignored(self, tmp_path):
        assistant = _assistant_record(text="done", stop_reason="end_turn")
        snap = {"type": "file-history-snapshot", "cwd": "/proj"}
        w = self._write_watcher(tmp_path, [assistant, snap])
        w.poll()
        assert turn_complete(True, w.last_meaningful_record) is True


# =============================================================================
# G3 — contract integration: detect_infra_error rewrite, run(), composite
#      inactivity, and LLMCaller._create_runner dispatch
# =============================================================================


class TestDetectInfraErrorG3:
    """Interactive-mode infra-error classification (PTY output + JSONL state)."""

    def test_returncode_zero_is_none_even_with_keyword(self):
        runner = _make_runner()
        # A successful turn is never an infra error, even if a limit-like word
        # happens to appear in the transcript text.
        assert (
            runner.detect_infra_error(0, "rate limit mentioned in a doc", "")
            == InfraErrorType.NONE
        )

    def test_returncode_124_is_timeout(self):
        runner = _make_runner()
        assert runner.detect_infra_error(124, "", "") == InfraErrorType.TIMEOUT

    def test_usage_limit_tail_keyword(self):
        runner = _make_runner()
        out = "blah\n" * 50 + "you've hit your limit\n"
        assert (
            runner.detect_infra_error(1, out, "") == InfraErrorType.USAGE_LIMIT
        )

    def test_startup_failure_failed_to_start_marker(self):
        runner = _make_runner()
        out = "=== Command: claude ===\n[claude-interactive] Failed to start 'claude': no pty\n"
        assert (
            runner.detect_infra_error(127, out, "")
            == InfraErrorType.STARTUP_FAILURE
        )

    def test_startup_failure_transcript_never_created_marker(self):
        runner = _make_runner()
        out = "some pty noise\n[claude-interactive] session transcript never created\n"
        assert (
            runner.detect_infra_error(1, out, "")
            == InfraErrorType.STARTUP_FAILURE
        )

    def test_startup_failure_signal_from_stderr(self):
        runner = _make_runner()
        # Signal may ride on either stream; combined scan finds it.
        assert (
            runner.detect_infra_error(1, "", "[claude-interactive] Failed to start 'claude': x")
            == InfraErrorType.STARTUP_FAILURE
        )

    def test_usage_limit_precedence_over_startup(self):
        runner = _make_runner()
        out = "Failed to start ... but also rate limit reached"
        assert (
            runner.detect_infra_error(1, out, "")
            == InfraErrorType.USAGE_LIMIT
        )

    def test_timeout_precedence_over_startup(self):
        runner = _make_runner()
        out = "[claude-interactive] session transcript never created"
        # rc 124 wins over a startup marker.
        assert (
            runner.detect_infra_error(124, out, "")
            == InfraErrorType.TIMEOUT
        )

    def test_ordinary_failure_is_none(self):
        runner = _make_runner()
        assert (
            runner.detect_infra_error(1, "task-level error: bad json", "")
            == InfraErrorType.NONE
        )


class TestStartupFailureMarkerEmission:
    """The supervisor emits the transcript-never-created marker for an active
    watcher that never located its file, so detect_infra_error can classify it."""

    def test_marker_emitted_when_watcher_never_locates(self, monkeypatch, no_kill):
        runner = _make_runner()
        # Process dies quickly with no PTY output and exit code 1.
        fake_pty = FakePty(chunks=[], die_when_empty=True, exitstatus=1)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        class _NeverLocates:
            path = None
            last_record = None

            def snapshot(self):
                pass

            def locate(self):
                return False

            def poll(self):
                return []

            def write_activity(self):
                return 0.0

            def synthesize_result(self):
                return ""

        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: _NeverLocates())

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[], cwd=Path("/proj"))
        assert "session transcript never created" in result.output
        assert (
            runner.detect_infra_error(result.returncode, result.output, "")
            == InfraErrorType.STARTUP_FAILURE
        )

    def test_no_marker_for_watcherless_degraded_mode(self, monkeypatch, no_kill):
        # A deliberately watcher-less run (no cwd) is a supported degraded mode,
        # NOT a startup failure — no marker emitted.
        runner = _make_runner()
        fake_pty = FakePty(chunks=["raw\n"], die_when_empty=True, exitstatus=0)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: None)

        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(args=[], cwd=None)
        assert "session transcript never created" not in result.output


class TestRunSynchronous:
    """run() reuses the PTY+JSONL path, returns pure NDJSON stdout, scrubs env."""

    def test_run_returns_pure_ndjson_stdout(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake_pty = FakePty(chunks=[], die_when_empty=False)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        monkeypatch.setattr(cir, "TURN_SILENCE_WINDOW", 0.0)

        assistant = _assistant_record(
            text="hi there", usage={"input_tokens": 5, "output_tokens": 1}
        )
        nd = to_stream_json_ndjson(assistant)
        result_line = synthesize_result_line(
            {"input_tokens": 5, "output_tokens": 1}, 0.0
        )
        fake_watcher = _FakeWatcher(
            lines=[nd], last_record=assistant, result_line=result_line
        )
        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: fake_watcher)

        runner.build_call_args("prompt", read_only=False)
        cp = runner.run(args=[], cwd=Path("/proj"))
        # No "=== Command:" wrapper — the monitored path adds that, run() does not.
        assert not cp.stdout.startswith("=== Command:")
        assert "hi there" in cp.stdout
        assert '"type": "result"' in cp.stdout
        # Every non-empty line parses as JSON (pure stream-json NDJSON).
        for line in cp.stdout.splitlines():
            if line.strip():
                json.loads(line)

    def test_run_timeout_returns_124(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake_pty = FakePty(chunks=[], die_when_empty=False)  # never finishes
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        cp = runner.run(args=[], timeout=0.2)
        assert cp.returncode == 124

    def test_run_scrubs_claudecode_env(self, monkeypatch, no_kill):
        runner = _make_runner()
        captured = {}

        def fake_spawn(full_cmd, cwd, env):
            captured["env"] = env
            return FakePty(chunks=["x\n"], die_when_empty=True, exitstatus=0)

        monkeypatch.setattr(runner, "_spawn_pty", fake_spawn)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        runner.build_call_args("prompt", read_only=False)
        runner.run(args=[], env={"CLAUDECODE": "1", "KEEP": "yes"})
        assert "CLAUDECODE" not in captured["env"]
        assert captured["env"].get("KEEP") == "yes"

    def test_run_ignores_on_retry(self, monkeypatch, no_kill):
        runner = _make_runner()
        fake_pty = FakePty(chunks=["x\n"], die_when_empty=True, exitstatus=0)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)

        called = {"n": 0}

        def on_retry(idx, msg):
            called["n"] += 1
            return None

        runner.build_call_args("prompt", read_only=False)
        cp = runner.run(args=[], on_retry=on_retry)
        assert cp.returncode == 0
        assert called["n"] == 0  # never invoked


class TestCompositeInactivity:
    """Inactivity uses the more-recent of PTY output and JSONL write activity."""

    def test_jsonl_writing_pty_silent_not_a_hang(self, monkeypatch, no_kill):
        runner = _make_runner()
        # PTY is silent and alive (would trip a PTY-only inactivity check).
        fake_pty = FakePty(chunks=[], die_when_empty=False)
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        monkeypatch.setattr(cir, "TURN_SILENCE_WINDOW", 1e9)  # never turn-complete

        # Watcher reports a fresh JSONL write on every check (mtime = now), so the
        # composite activity stays current despite PTY silence.
        class _FreshJsonl:
            path = "p"
            last_record = None

            def snapshot(self):
                pass

            def locate(self):
                return True

            def poll(self):
                return []

            def write_activity(self):
                return time.time()  # always "just written"

            def synthesize_result(self):
                return ""

        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: _FreshJsonl())

        # Force the supervisor to terminate after a few iterations via wall
        # timeout, NOT inactivity — proving inactivity never fired.
        runner.build_call_args("prompt", read_only=False)
        result = runner.run_with_monitor(
            args=[], cwd=Path("/proj"), wall_timeout=0.3, inactivity_timeout=0
        )
        # Despite inactivity_timeout=0, the fresh JSONL write keeps it alive
        # until the wall timeout fires.
        assert result.returncode == 124
        assert "Wall timeout" in result.output
        assert "inactivity timeout" not in result.output.lower()

    def test_stalled_model_with_pty_spinner_is_a_hang(self, monkeypatch, no_kill):
        """Regression (high): a model call that stalls mid-turn while the
        interactive TUI keeps animating its spinner/footer on the PTY must be
        classified as a hang.  The cosmetic PTY re-renders bump the PTY activity
        clock every read, but no new JSONL transcript records are written, so the
        meaningful (JSONL) activity signal goes stale and the inactivity timeout
        must fire — exactly like the print-mode runner.  Before the fix the PTY
        clock masked the stall and the supervisor loop spun forever."""

        class _RerenderPty(FakePty):
            """Alive forever; emits a re-render chunk on every read so the PTY
            activity clock never goes idle."""

            def __init__(self):
                super().__init__(chunks=[], die_when_empty=False)

            def isalive(self):
                return True

            def read_nonblocking(self, size=1, timeout=None):
                time.sleep(0.005)
                return "\x1b[2K footer re-render \r"

        runner = _make_runner()
        fake_pty = _RerenderPty()
        monkeypatch.setattr(runner, "_spawn_pty", lambda *a, **k: fake_pty)
        monkeypatch.setattr(runner, "_feed_prompt", lambda *a, **k: None)
        # Never turn-complete: the record is non-terminal and the window is huge.
        monkeypatch.setattr(cir, "TURN_SILENCE_WINDOW", 1e9)

        # Watcher located its transcript but it stopped growing: a non-terminal
        # record and a stale write_activity (far in the past) — the JSONL has
        # genuinely stalled even though the PTY keeps chattering.
        stale_ts = time.time() - 10_000.0

        class _StalledJsonl:
            path = "p"
            last_record = None
            last_meaningful_record = {"type": "assistant", "stop_reason": None}

            def snapshot(self):
                pass

            def locate(self):
                return True

            def poll(self):
                return []

            def write_activity(self):
                return stale_ts  # stalled — no new records

            def synthesize_result(self):
                return ""

        monkeypatch.setattr(runner, "_make_watcher", lambda cwd: _StalledJsonl())

        runner.build_call_args("prompt", read_only=False)

        holder = {}

        def _go():
            holder["result"] = runner.run_with_monitor(
                args=[], cwd=Path("/proj"),
                wall_timeout=None, inactivity_timeout=0.2,
            )

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "run_with_monitor hung on a stalled model call"
        result = holder["result"]
        # Stalled JSONL → inactivity hang, despite the never-idle PTY spinner.
        assert result.returncode == 124
        assert "inactivity timeout" in result.output.lower()
        assert "Wall timeout" not in result.output
        assert fake_pty.closed is True


class TestCreateRunnerDispatch:
    """LLMCaller._create_runner dispatches type: claude-interactive."""

    def _caller(self):
        from unittest.mock import patch
        from tianluo.engine.llm_caller import LLMCaller

        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            return caller

    def test_claude_interactive_type_creates_interactive_runner(self):
        caller = self._caller()
        runner = caller._create_runner(
            {"type": "claude-interactive", "cmd": "claude", "priority": 0}
        )
        assert isinstance(runner, ClaudeInteractiveRunner)
        assert runner.command["cmd"] == "claude"

    def test_claude_interactive_preserves_priority(self):
        caller = self._caller()
        runner = caller._create_runner(
            {"type": "claude-interactive", "cmd": "kclaude", "priority": 7}
        )
        assert runner.command["priority"] == 7

    def test_default_type_still_creates_claude_code_runner(self):
        from tianluo.claude_runner import ClaudeCodeRunner

        caller = self._caller()
        runner = caller._create_runner({"type": "claude-code", "cmd": "claude"})
        assert isinstance(runner, ClaudeCodeRunner)
        assert not isinstance(runner, ClaudeInteractiveRunner)

    def test_missing_type_defaults_to_claude_code_runner(self):
        from tianluo.claude_runner import ClaudeCodeRunner

        caller = self._caller()
        # No "type" key -> defaults to claude-code, not claude-interactive.
        runner = caller._create_runner({"cmd": "claude"})
        assert isinstance(runner, ClaudeCodeRunner)
        assert not isinstance(runner, ClaudeInteractiveRunner)

    def test_unknown_type_still_raises_value_error(self):
        caller = self._caller()
        with pytest.raises(ValueError, match="Unknown agent type"):
            caller._create_runner({"type": "no-such-runner", "cmd": "claude"})
