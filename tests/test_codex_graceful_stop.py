"""End-to-end graceful stop inside ``CodexRunner``'s monitor loop.

The mirror of ``tests/test_runner_graceful_stop.py``, which drives only
``ClaudeCodeRunner``. Codex is not a second copy of that path: it reads the
provider's OWN JSONL (``thread.started`` / ``item.*`` / ``turn.*``) and the
boundary predicate runs over the CONVERTED stream-json, so a converter change
can break the stop semantics while the claude test stays green. The parts that
only exist here are covered against a real child process:

* boundary detection runs on the converted events — a ``command_execution``
  item's ``item.completed`` is the boundary, its ``item.started`` is not, and a
  line that converts only to an ``init`` (``thread.started``) must not shorten
  the wait;
* ``_stop_gracefully`` calls ``converter.finalize()``, so an item cut short
  mid-flight is closed with the synthesized ``[interrupted]`` ``tool_result``
  instead of leaving a dangling ``tool_use`` in the recorded transcript;
* the SIGINT → wait → SIGKILL escalation and the bounded boundary wait behave
  as they do for claude, over a child in its own process group.

WHY every emitted line is spaced by ``_GAP``: the monitor loop selects on the
pipe fd but reads through a buffered ``readline``, so two lines printed
back-to-back can be pulled into one buffer and leave the fd unready — the
second line is then only delivered when a LATER write wakes select. Spacing the
writes is what makes "the stop arrives at this exact line" a fact of the test
rather than of the buffering.
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import time

import pytest

from tianluo.codex_runner import CodexRunner
from tianluo.stop_signal import get_stop_signal


@pytest.fixture(autouse=True)
def _clean_signal():
    get_stop_signal().clear()
    yield
    get_stop_signal().clear()


# Comfortably above the loop's 1s select tick, so each write lands on its own
# iteration.
_GAP = 1.3


def _emitter(*lines: str, tail: float = 60.0) -> str:
    """A child that writes *lines* one poll-tick apart, then parks."""
    body = ["import time"]
    for line in lines:
        body.append(f"print({line!r}, flush=True)")
        body.append(f"time.sleep({_GAP})")
    body.append(f"time.sleep({tail})")
    return "\n".join(body)


def _child(script: str) -> list:
    return [sys.executable, "-u", "-c", textwrap.dedent(script)]


# Real ``codex exec --json`` ThreadItem shapes (the schema the runner's own
# TestRealSchema* family asserts against), not the legacy fallback shapes.
THREAD_STARTED = json.dumps({"type": "thread.started", "thread_id": "th_test"})
ITEM_STARTED = json.dumps({
    "type": "item.started",
    "item": {"id": "item_1", "type": "command_execution", "command": "ls"},
})
ITEM_COMPLETED = json.dumps({
    "type": "item.completed",
    "item": {
        "id": "item_1", "type": "command_execution", "command": "ls",
        "aggregated_output": "ok", "exit_code": 0,
    },
})
AGENT_MESSAGE = json.dumps({
    "type": "item.completed",
    "item": {"id": "item_0", "type": "agent_message", "text": "MARKER"},
})


def _run(runner, script, **kwargs):
    return runner._run_single_with_monitor(
        full_cmd=_child(script),
        cmd_name=sys.executable,
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


def _runner():
    return CodexRunner(command={"cmd": sys.executable, "priority": 0})


def _stop_on_tool_use(sink=None):
    def _on_output(line):
        if sink is not None:
            sink.append(line)
        if "tool_use" in line:
            get_stop_signal().request()
    return _on_output


def _payloads(output: str) -> list:
    out = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestCodexGracefulStop:
    def test_the_child_gets_its_own_process_group(self):
        """A terminal Ctrl-C must reach ``luo run`` alone, so the runner keeps
        the choice of WHEN and HOW the codex thread winds down."""
        result = _run(_runner(), """
            import os, json
            print(json.dumps({"type": "item.completed", "item": {
                "id": "i0", "type": "agent_message",
                "text": "pgid=%d" % os.getpgid(0)}}), flush=True)
            """)
        match = re.search(r"pgid=(\d+)", result.output)
        assert match, result.output
        assert int(match.group(1)) != os.getpgid(0)

    def test_no_stop_request_leaves_the_run_untouched(self):
        result = _run(_runner(), _emitter(THREAD_STARTED, AGENT_MESSAGE, tail=0))
        assert result.interrupted is False
        assert result.success is True
        assert "MARKER" in result.output

    def test_a_stop_waits_for_the_converted_message_boundary(self):
        """The boundary is decided on the CONVERTED stream: a
        ``command_execution`` item's ``item.started`` is a bare ``tool_use``,
        and stopping there would leave the call dangling."""
        seen = []
        result = _run(
            _runner(),
            _emitter(THREAD_STARTED, ITEM_STARTED, ITEM_COMPLETED),
            on_output=_stop_on_tool_use(seen),
        )

        assert result.interrupted is True
        # The item.completed converted to the tool_result that IS the boundary,
        # and it was read before the child was signalled.
        assert any("tool_result" in line for line in seen)
        assert "tool_result" in result.output
        # Boundary-reached, not boundary-timeout: the real tool_result is the
        # one recorded, so nothing was synthesized over the top of it.
        closes = [
            block
            for p in _payloads(result.output)
            for block in (p.get("message", {}) or {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert len(closes) == 1
        assert closes[0]["is_error"] is False

    def test_an_agent_message_is_a_boundary(self):
        """An agent_message converts to an assistant turn carrying no tool_use,
        so nothing is left dangling and it is a legitimate stopping point."""
        result = _run(
            _runner(),
            _emitter(THREAD_STARTED, ITEM_STARTED, AGENT_MESSAGE),
            on_output=_stop_on_tool_use(),
        )
        assert result.interrupted is True
        assert "MARKER" in result.output

    def test_a_line_carrying_no_boundary_does_not_shorten_the_wait(
        self, monkeypatch
    ):
        """``thread.started`` converts to an ``init`` event, which is not a
        boundary — the stop must fall through to the bounded wait instead of
        cutting on the next line that merely happens to arrive."""
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 4.0)

        started = time.time()
        result = _run(
            _runner(),
            _emitter(ITEM_STARTED, THREAD_STARTED),
            on_output=_stop_on_tool_use(),
        )
        elapsed = time.time() - started
        assert result.interrupted is True
        # Cut by the boundary TIMEOUT, not by the init line: cutting on the
        # init would land at roughly _GAP, well under the patched 4s wait.
        assert elapsed >= 4.0

    def test_the_boundary_wait_is_bounded(self, monkeypatch):
        """A stream that never reaches a boundary must not hang the stop."""
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 1.0)

        started = time.time()
        result = _run(
            _runner(), _emitter(ITEM_STARTED), on_output=_stop_on_tool_use()
        )
        elapsed = time.time() - started
        assert result.interrupted is True
        assert elapsed < 15

    def test_finalize_closes_the_item_the_stop_cut_short(self, monkeypatch):
        """WHY this matters more for codex than for claude: the claude CLI
        writes its own interrupted turn, while a codex thread stopped mid-item
        would end on a dangling ``tool_use`` unless the converter is finalized
        by the stop path itself."""
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 1.0)

        result = _run(
            _runner(),
            _emitter(THREAD_STARTED, ITEM_STARTED),
            on_output=_stop_on_tool_use(),
        )

        assert result.interrupted is True
        payloads = _payloads(result.output)
        # The dangling tool_use was closed with the synthesized interrupted
        # tool_result, carrying the SAME tool_use_id.
        closes = [
            block
            for p in payloads
            for block in (p.get("message", {}) or {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert closes, f"no closing tool_result in:\n{result.output}"
        assert closes[0]["tool_use_id"] == "item_1"
        assert closes[0]["is_error"] is True
        assert "[interrupted]" in closes[0]["content"]
        # ...and a terminal result event still lands, so the recorded
        # transcript is never left without one.
        assert any(p.get("type") == "result" for p in payloads)

    def test_partial_output_is_returned_for_history(self, monkeypatch):
        """LLMCaller records the partial output before opening the dialog."""
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 1.0)

        result = _run(
            _runner(),
            _emitter(THREAD_STARTED, AGENT_MESSAGE, ITEM_STARTED),
            on_output=_stop_on_tool_use(),
        )
        assert "MARKER" in result.output
        assert result.interrupted is True
        assert result.success is False
        # An interrupted call is NOT an infrastructure failure, so the runner
        # must not ask for its own retry — the flow decides what happens next.
        assert result.should_retry is False

    def test_the_wind_down_lines_reach_the_output_callback(self, monkeypatch):
        """Same contract as the claude path: the ``turn.completed`` codex
        writes while winding down — and the converter's own ``finalize()``
        output — carry the turn's usage, and LLMCaller reads an attempt's usage
        only from the tracker fed by this callback."""
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 1.0)
        seen = []

        turn_completed = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 1234, "output_tokens": 56},
        })
        result = _run(
            _runner(),
            f"""
            import signal, sys, time
            def _wind_down(signum, frame):
                print({turn_completed!r}, flush=True)
                sys.exit(0)
            signal.signal(signal.SIGINT, _wind_down)
            print({THREAD_STARTED!r}, flush=True)
            time.sleep({_GAP})
            print({ITEM_STARTED!r}, flush=True)
            time.sleep(60)
            """,
            on_output=_stop_on_tool_use(seen),
        )

        assert result.interrupted is True
        finals = [
            p for p in _payloads("".join(seen)) if p.get("type") == "result"
        ]
        assert finals, f"the wind-down result never reached on_output: {seen}"
        assert finals[0].get("usage", {}).get("input_tokens") == 1234
        # The interrupted tool_result synthesized by ``finalize()`` is part of
        # the same stream, so the live view closes the tool chip it opened.
        closes = [
            block
            for p in _payloads("".join(seen))
            for block in (p.get("message", {}) or {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert closes and closes[0]["tool_use_id"] == "item_1"

    def test_a_child_that_ignores_sigint_is_killed(self, monkeypatch):
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 0.5)
        monkeypatch.setattr("tianluo.codex_runner.EXIT_WAIT_SECONDS", 1.0)

        started = time.time()
        result = _run(_runner(), f"""
            import signal, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            print({ITEM_STARTED!r}, flush=True)
            time.sleep(60)
            """, on_output=_stop_on_tool_use())
        assert result.interrupted is True
        assert time.time() - started < 20
