"""End-to-end graceful stop inside the runners' monitor loops.

Drives ``ClaudeCodeRunner._run_single_with_monitor`` against a real child
process that emits stream-json, so the parts that only exist inside that loop
are covered for real: the child gets its own process group, a stop request does
not cut the stream mid-tool-call, the partial output survives, and the result
is marked interrupted so LLMCaller records it and opens the dialog.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time

import pytest

from tianluo.claude_runner import ClaudeCodeRunner
from tianluo.stop_signal import get_stop_signal


@pytest.fixture(autouse=True)
def _clean_signal():
    get_stop_signal().clear()
    yield
    get_stop_signal().clear()


def _child(script: str) -> list:
    return [sys.executable, "-u", "-c", textwrap.dedent(script)]


TOOL_USE = json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
    ]},
})
TOOL_RESULT = json.dumps({
    "type": "user",
    "message": {"content": [{"type": "tool_result", "content": "ok"}]},
})
TEXT_ONLY = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "thinking"}]},
})


def _run(runner, script, **kwargs):
    return runner._run_single_with_monitor(
        full_cmd=_child(script),
        cmd_name=sys.executable,
        cmd_index=0,
        log_file=None,
        wall_timeout=None,
        inactivity_timeout=1800,
        cwd=None,
        env=dict(os.environ),
        on_output=kwargs.pop("on_output", None),
        on_activity=None,
        start_time=time.time(),
        **kwargs,
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestGracefulStopInMonitorLoop:
    def test_the_child_gets_its_own_process_group(self):
        """A terminal Ctrl-C must reach ``luo run`` alone, so the runner can
        decide WHEN and HOW the child winds down."""
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        result = _run(runner, """
            import os, json, sys
            print(json.dumps({"type": "result", "pgid": os.getpgid(0)}), flush=True)
            """)
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["pgid"] != os.getpgid(0)

    def test_a_stop_waits_for_the_next_message_boundary(self):
        """Stopping between a tool_use and its tool_result would leave a
        dangling call — the one shape a provider can refuse to resume."""
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        seen = []

        def _on_output(line):
            seen.append(line)
            if TOOL_USE in line:
                # Request the stop exactly at the un-resumable point.
                get_stop_signal().request()

        result = _run(runner, f"""
            import sys, time
            print({TOOL_USE!r}, flush=True)
            time.sleep(4)  # > the runner's 1s poll tick, even under load
            print({TOOL_RESULT!r}, flush=True)
            time.sleep(60)
            """, on_output=_on_output)

        assert result.interrupted is True
        # The tool_result — the boundary — was read before the child was signalled.
        assert any("tool_result" in line for line in seen)
        assert "tool_result" in result.output

    def test_an_assistant_turn_without_a_tool_call_is_a_boundary(self):
        """A turn that issued no tool call leaves nothing dangling, so it is a
        legitimate place to stop even though the turn is not over."""
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})

        def _on_output(line):
            # Request the stop on the NON-boundary line, the way an
            # asynchronous Ctrl-C / watcher thread would.
            if "tool_use" in line:
                get_stop_signal().request()

        result = _run(runner, f"""
            import time
            print({TOOL_USE!r}, flush=True)
            time.sleep(4)
            print({TEXT_ONLY!r}, flush=True)
            time.sleep(60)
            """, on_output=_on_output)
        assert result.interrupted is True
        assert "thinking" in result.output

    def test_the_boundary_wait_is_bounded(self, monkeypatch):
        """A stream that never reaches a boundary must not hang the stop."""
        monkeypatch.setattr("tianluo.claude_runner.BOUNDARY_WAIT_SECONDS", 1.0)
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})

        def _on_output(line):
            if TOOL_USE in line:
                get_stop_signal().request()

        started = time.time()
        result = _run(runner, f"""
            import time
            print({TOOL_USE!r}, flush=True)
            time.sleep(60)
            """, on_output=_on_output)
        elapsed = time.time() - started
        assert result.interrupted is True
        # Bounded by the (patched) boundary wait, not by the child's sleep.
        assert elapsed < 15

    def test_partial_output_is_returned_for_history(self):
        """LLMCaller records the partial output before opening the dialog."""
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})

        def _on_output(line):
            if "tool_use" in line:
                get_stop_signal().request()

        result = _run(runner, f"""
            import json, time
            print({TOOL_USE!r}, flush=True)
            time.sleep(4)
            print(json.dumps({{"type": "assistant", "message": {{"content": [
                {{"type": "text", "text": "MARKER"}}]}}}}), flush=True)
            time.sleep(60)
            """, on_output=_on_output)
        assert "MARKER" in result.output
        assert result.interrupted is True
        assert result.success is False
        # An interrupted call is NOT an infrastructure failure, so the runner
        # must not ask for its own retry — the flow decides what happens next.
        assert result.should_retry is False

    def test_the_wind_down_lines_reach_the_output_callback(self):
        """The caller's callback IS the stream tracker, and LLMCaller reads the
        attempt's token usage from that tracker alone — so the terminal
        ``result`` line the CLI writes AFTER the stop signal must be handed
        over like any other line, or a fully-paid interrupted call books zero
        tokens."""
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        seen = []

        def _on_output(line):
            seen.append(line)
            if "tool_use" in line:
                get_stop_signal().request()

        result = _run(runner, f"""
            import json, signal, sys, time
            def _wind_down(signum, frame):
                print(json.dumps({{"type": "result", "subtype": "success",
                    "result": "[Request interrupted by user]",
                    "usage": {{"input_tokens": 1234, "output_tokens": 56}}}}),
                    flush=True)
                sys.exit(0)
            signal.signal(signal.SIGINT, _wind_down)
            print({TOOL_USE!r}, flush=True)
            time.sleep(1.5)
            print({TOOL_RESULT!r}, flush=True)
            time.sleep(60)
            """, on_output=_on_output)

        assert result.interrupted is True
        finals = [
            json.loads(ln) for ln in seen
            if ln.strip() and json.loads(ln).get("type") == "result"
        ]
        assert finals, f"the wind-down result never reached on_output: {seen}"
        assert finals[0]["usage"]["input_tokens"] == 1234
        # Whatever the callback saw is also what the recorded history holds.
        assert "1234" in result.output

    def test_a_child_that_ignores_sigint_is_killed(self, monkeypatch):
        monkeypatch.setattr("tianluo.claude_runner.BOUNDARY_WAIT_SECONDS", 0.5)
        monkeypatch.setattr("tianluo.claude_runner.EXIT_WAIT_SECONDS", 1.0)
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})

        def _on_output(line):
            if "READY" in line:
                get_stop_signal().request()

        started = time.time()
        result = _run(runner, """
            import json, signal, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "READY"}]}}), flush=True)
            time.sleep(60)
            """, on_output=_on_output)
        assert result.interrupted is True
        assert time.time() - started < 20

    def test_no_stop_request_leaves_the_run_untouched(self):
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        result = _run(runner, f"""
            print({TEXT_ONLY!r}, flush=True)
            print({json.dumps(json.dumps({"type": "result", "result": "done"}))[1:-1]!r}, flush=True)
            """)
        assert result.interrupted is False
        assert result.success is True
        assert "thinking" in result.output


    def test_a_stop_at_a_confirmation_prompt_is_observed(self, monkeypatch):
        """The confirm callback blocks INLINE in the monitor loop — it is the
        one place the loop hands control away. A handler that watched only the
        child never let the loop poll the stop signal, so Ctrl-C at a
        confirmation prompt did nothing at all and the child was never
        signalled.

        The two waits are pinned apart so the assertion is about WHICH wait ran,
        not about how fast the machine is: a child parked at a prompt has
        already emitted a complete message and emits nothing more until
        answered, so the boundary wait must be skipped outright.

        The child is reaped rather than expected to die of SIGINT — a pytest-xdist
        worker ignores SIGINT and the child inherits that disposition, so its
        survival says nothing about this code path.
        """
        monkeypatch.setattr("tianluo.claude_runner.BOUNDARY_WAIT_SECONDS", 120.0)
        monkeypatch.setattr("tianluo.claude_runner.EXIT_WAIT_SECONDS", 1.0)
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        alive_reports = []

        def _on_confirm(prompt, options, is_alive):
            # The web answer never comes; the operator presses Ctrl-C instead.
            get_stop_signal().request()
            alive_reports.append(is_alive())
            return None

        started = time.time()
        result = _run(runner, """
            import time
            print("Do you want to continue? [y/N]", flush=True)
            time.sleep(120)
            """, on_confirm=_on_confirm)
        elapsed = time.time() - started

        assert alive_reports == [False], (
            "is_alive must report False once a stop is published"
        )
        assert result.interrupted is True
        # Well under the 120s boundary wait: the stop was acted on at once.
        assert elapsed < 30

    def test_an_answered_confirmation_is_unaffected(self):
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})

        def _on_confirm(prompt, options, is_alive):
            assert is_alive() is True
            return "y"

        result = _run(runner, f"""
            import sys, time
            print("Do you want to continue? [y/N]", flush=True)
            sys.stdin.readline()
            print({TEXT_ONLY!r}, flush=True)
            """, on_confirm=_on_confirm)
        assert result.interrupted is False
        assert "thinking" in result.output


class TestInteractiveGracefulStop:
    """The PTY runner's stop shape must match the print-mode one.

    Its ``_terminate`` is a *reclamation* step (SIGTERM after a 0.2s grace),
    not a stop protocol: reaching it first kills the CLI mid-tool_use and
    leaves exactly the dangling transcript a resume cannot continue from.
    """

    def _runner(self):
        pytest.importorskip("pexpect")
        from tianluo.claude_interactive_runner import ClaudeInteractiveRunner

        return ClaudeInteractiveRunner(command={"cmd": "claude", "priority": 0})

    def test_a_boundary_from_before_the_stop_does_not_end_the_wait(self):
        runner = self._runner()
        buffer = [TEXT_ONLY, TOOL_USE]
        # Stop requested now: the completed assistant message is behind us, and
        # the outstanding tool_use is what the wait is for.
        stop_mark = len(buffer)
        assert runner._boundary_reached_since(buffer, stop_mark) is False

        buffer.append(TOOL_RESULT)
        assert runner._boundary_reached_since(buffer, stop_mark) is True

    def test_a_boundary_after_the_stop_ends_the_wait(self):
        runner = self._runner()
        buffer = [TOOL_USE]
        stop_mark = len(buffer)
        buffer.append(TEXT_ONLY)
        assert runner._boundary_reached_since(buffer, stop_mark) is True

    def test_the_timeout_path_reports_the_timeout_reason(self):
        """A boundary that never arrived must not be reported as "boundary
        reached" — the print-mode runners already distinguish the two, and the
        message is what the operator reads in history."""
        runner = self._runner()
        assert runner._stop_reason_key(True) == "runner.stop.reason.boundary_reached"
        assert runner._stop_reason_key(False) == "runner.stop.reason.boundary_timeout"

    def test_graceful_stop_sends_sigint_to_the_group_and_waits(self):
        import signal
        import subprocess

        runner = self._runner()
        # WHY the child polls a flag instead of handling SIGINT inside one long
        # sleep: a signal delivered between ``signal.signal`` and the entry into
        # ``time.sleep`` only sets CPython's pending flag — there is no EINTR to
        # cut the sleep short, so the Python-level handler would not run until
        # the sleep ended. Under a loaded parallel run the parent wins that race
        # often enough to make a single-sleep child flake. The poll loop resolves
        # the pending flag on its next tick, which is what a real CLI (busy, not
        # parked in one uninterruptible sleep) does naturally.
        #
        # The wait is deliberately far longer than the runner's exit_wait so a
        # child that timed out on its own can never be mistaken for one that
        # wound down on SIGINT: returncode 0 here is only reachable via _bye.
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "import signal, sys, time\n"
             "_stopping = False\n"
             "def _bye(*a):\n"
             "    global _stopping\n"
             "    _stopping = True\n"
             "signal.signal(signal.SIGINT, _bye)\n"
             "print('up', flush=True)\n"
             "deadline = time.time() + 300\n"
             "while time.time() < deadline:\n"
             "    if _stopping:\n"
             "        sys.exit(0)\n"
             "    time.sleep(0.01)\n"
             "sys.exit(3)\n"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            proc.stdout.readline()

            class _Handle:
                pid = proc.pid

                @staticmethod
                def isalive():
                    return proc.poll() is None

            runner._graceful_stop(_Handle())
            # The child's OWN evidence, not wall-clock timing (which a loaded
            # parallel run cannot bound): it can only reach 0 by running its
            # SIGINT wind-down. An escalation would be -SIGKILL, and its own
            # 300s timeout would be 3.  The bounded wait absorbs the scheduling
            # slack of a loaded parallel run: with no transcript to observe, the
            # stop gives a blind child only a short grace before returning.
            proc.wait(timeout=60)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)



class _StubWatcher:
    """Minimal ``SessionTranscriptWatcher`` stand-in for the stop wait.

    Only the three members the wait consults are modelled: whether a transcript
    was located, the lines a poll yields, and when the transcript was last
    written.
    """

    def __init__(self, path="/tmp/transcript.jsonl", lines=None, last_write=None):
        self.path = path
        self._lines = [list(chunk) for chunk in (lines or [])]
        self.last_meaningful_record = None
        self._last_write = last_write if last_write is not None else 0.0
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self._lines.pop(0) if self._lines else []

    def write_activity(self):
        return self._last_write() if callable(self._last_write) else self._last_write


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestInteractiveStopEndsOnTheTurnNotOnTheChild:
    """The interactive TUI answers SIGINT by closing its turn and going back to
    its input box — it never exits. Waiting for the pid to disappear therefore
    always burns the whole 30s exit wait and delays the interjection dialog the
    operator is waiting for by exactly that much, on EVERY stop.
    """

    def _runner(self):
        pytest.importorskip("pexpect")
        from tianluo.claude_interactive_runner import ClaudeInteractiveRunner

        return ClaudeInteractiveRunner(command={"cmd": "claude", "priority": 0})

    def _stays_alive_child(self):
        """A child shaped like the TUI: SIGINT closes the turn, never exits."""
        import subprocess

        return subprocess.Popen(
            [sys.executable, "-u", "-c",
             "import signal, time\n"
             "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
             "print('up', flush=True)\n"
             "time.sleep(300)\n"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )

    def _handle_for(self, proc):
        class _Handle:
            pid = proc.pid

            @staticmethod
            def isalive():
                return proc.poll() is None

        return _Handle()

    def _kill(self, proc):
        import signal

        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)

    def test_a_settled_transcript_ends_the_wait_while_the_child_lives_on(
        self, capsys
    ):
        from tianluo.claude_interactive_runner import TURN_SILENCE_WINDOW
        from tianluo.stop_signal import EXIT_WAIT_SECONDS

        runner = self._runner()
        proc = self._stays_alive_child()
        try:
            proc.stdout.readline()
            # Transcript finalised long ago and quiet since: the turn is closed,
            # which is the only thing the stop is actually waiting for.
            watcher = _StubWatcher(last_write=time.time() - 60)

            started = time.time()
            runner._graceful_stop(self._handle_for(proc), watcher=watcher)
            elapsed = time.time() - started

            # Settled after the silence window, NOT after the 30s exit wait.
            assert elapsed >= TURN_SILENCE_WINDOW - 0.5
            assert elapsed < EXIT_WAIT_SECONDS / 2
            # The child is left for ``_terminate`` to reclaim — a TUI that stays
            # at its input box is not a child that ignored the stop.
            assert proc.poll() is None
            # Locale-independent: the escalation notice names SIGKILL in every
            # language pack.
            assert "SIGKILL" not in capsys.readouterr().err
        finally:
            self._kill(proc)

    def test_the_wind_down_records_reach_the_caller(self):
        runner = self._runner()
        proc = self._stays_alive_child()
        collected = []
        try:
            proc.stdout.readline()
            # First poll yields the record the CLI wrote while winding down; it
            # belongs to this attempt's output exactly as in print mode.
            watcher = _StubWatcher(
                lines=[[TEXT_ONLY]], last_write=time.time() - 60
            )

            runner._graceful_stop(
                self._handle_for(proc),
                watcher=watcher,
                on_records=collected.extend,
            )

            assert collected == [TEXT_ONLY]
        finally:
            self._kill(proc)

    def test_a_still_writing_transcript_holds_the_wait_open(self):
        """A transcript still being appended to is a turn still closing."""
        runner = self._runner()
        now = time.time()
        watcher = _StubWatcher(last_write=lambda: time.time())
        assert runner._transcript_settled(watcher, now) is False

        quiet = _StubWatcher(last_write=now - 60)
        # Quiet for ages, but the stop only JUST landed: the CLI still gets a
        # full window to record its wind-down.
        assert runner._transcript_settled(quiet, time.time()) is False
        assert runner._transcript_settled(quiet, now - 60) is True
        # No transcript located — nothing to key the wait on.
        assert runner._transcript_settled(None, now - 60) is False
        assert runner._transcript_settled(_StubWatcher(path=None), now - 60) is False

    def test_without_a_transcript_the_blind_wait_is_short(self, capsys):
        """No transcript ⇒ the turn's end is unobservable, and the TUI will
        never exit — so the wait cannot be the full 30s either."""
        from tianluo.claude_interactive_runner import STOP_BLIND_GRACE_SECONDS
        from tianluo.stop_signal import EXIT_WAIT_SECONDS

        runner = self._runner()
        proc = self._stays_alive_child()
        try:
            proc.stdout.readline()
            started = time.time()
            runner._graceful_stop(self._handle_for(proc))
            elapsed = time.time() - started

            assert elapsed >= STOP_BLIND_GRACE_SECONDS - 0.5
            assert elapsed < EXIT_WAIT_SECONDS / 2
            assert proc.poll() is None
            # A child that outlived the whole wait IS reported: the finally
            # block's _terminate is what reclaims it.
            assert "SIGKILL" in capsys.readouterr().err
        finally:
            self._kill(proc)

@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestTimeoutReclaimsTheWholeGroup:
    """A timeout usually fires while a tool/bash GRANDCHILD is the thing
    hanging. Killing only the CLI leaves it running — still changing the
    workspace — and reaping the CLI makes ``proc.poll()`` say "finished", so
    the loop's own cleanup then skips group reclamation entirely."""

    def _grandchild_script(self, marker):
        return f"""
            import json, os, subprocess, sys, time
            child = subprocess.Popen([
                sys.executable, "-u", "-c",
                "import time\\nwhile True:\\n    time.sleep(0.05)",
            ])
            print(json.dumps({{"type": "system", "grandchild": child.pid}}), flush=True)
            open({marker!r}, "w").write(str(child.pid))
            while True:
                time.sleep(0.05)
            """

    def _assert_reaped(self, pid):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        pytest.fail(f"grandchild {pid} survived the timeout kill")

    def test_a_wall_timeout_kills_tool_grandchildren(self, tmp_path):
        marker = str(tmp_path / "pid")
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        result = runner._run_single_with_monitor(
            full_cmd=_child(self._grandchild_script(marker)),
            cmd_name=sys.executable,
            cmd_index=0,
            log_file=None,
            wall_timeout=1,
            inactivity_timeout=1800,
            cwd=None,
            env=dict(os.environ),
            on_output=None,
            on_activity=None,
            start_time=time.time(),
        )
        assert result.returncode == 124
        self._assert_reaped(int(open(marker).read()))

    def test_an_inactivity_timeout_kills_tool_grandchildren(self, tmp_path):
        marker = str(tmp_path / "pid")
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        result = runner._run_single_with_monitor(
            full_cmd=_child(self._grandchild_script(marker)),
            cmd_name=sys.executable,
            cmd_index=0,
            log_file=None,
            wall_timeout=None,
            inactivity_timeout=1,
            cwd=None,
            env=dict(os.environ),
            on_output=None,
            on_activity=None,
            start_time=time.time(),
        )
        assert result.returncode == 124
        self._assert_reaped(int(open(marker).read()))


class TestProcessGroupGuards:
    """INVARIANT: no signal path may target pgid <= 1 or our own group.

    ``killpg(1, sig)`` becomes ``kill(-1, sig)`` — every process the user owns.
    A test double whose ``pid`` coerced to 1 resolved init's group and took the
    development machine down three times, so the refusal lives in production
    code, not only in the suite's guard fixture.
    """

    def test_a_process_double_resolves_no_group(self):
        from unittest.mock import MagicMock

        from tianluo.agent_runner import resolve_process_group

        assert resolve_process_group(MagicMock()) is None

    def test_pid_one_resolves_no_group(self):
        import types

        from tianluo.agent_runner import resolve_process_group

        assert resolve_process_group(types.SimpleNamespace(pid=1)) is None
        assert resolve_process_group(types.SimpleNamespace(pid=0)) is None
        assert resolve_process_group(types.SimpleNamespace(pid=None)) is None

    def test_lethal_groups_are_never_signalable(self):
        from tianluo.agent_runner import is_signalable_process_group

        assert is_signalable_process_group(0) is False
        assert is_signalable_process_group(1) is False
        assert is_signalable_process_group(None) is False
        assert is_signalable_process_group("2") is False
        assert is_signalable_process_group(os.getpgrp()) is False

    def test_signalling_a_lethal_group_is_refused(self):
        import signal as _signal

        from tianluo.agent_runner import (
            process_group_alive,
            signal_process_group,
        )

        # Refused before it reaches the OS: the suite's guard fixture would
        # otherwise be the only thing standing between this and the machine.
        assert signal_process_group(None, _signal.SIGKILL, pgid=1) is False
        assert signal_process_group(None, _signal.SIGKILL, pgid=0) is False
        assert (
            signal_process_group(None, _signal.SIGKILL, pgid=os.getpgrp())
            is False
        )
        assert process_group_alive(1) is False
        assert process_group_alive(os.getpgrp()) is False

    def test_reclaim_of_a_lethal_group_delivers_nothing(self):
        from tianluo.agent_runner import ensure_process_group_reclaimed

        # Returns without signalling; the guard fixture would fail the test if
        # anything reached os.killpg.
        ensure_process_group_reclaimed(1, grace=0.0)
        ensure_process_group_reclaimed(os.getpgrp(), grace=0.0)
