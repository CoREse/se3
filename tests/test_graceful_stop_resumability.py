"""The graceful stop must leave a transcript the provider will RESUME.

The other graceful-stop suites cover the mechanism — process-group isolation,
the boundary wait, SIGINT before SIGKILL, partial-output recording. None of
them covers the reason the mechanism exists: native resume (decision 1) is the
main continuation path, and it only works if the session the stop left behind
can actually be continued. Until now that was asserted only in prose comments,
so a change to ``is_message_boundary``, to the confirmation-prompt early-stop
branch, or to the codex converter's synthesized ``tool_result`` could produce
the dangling shape providers refuse and the whole suite would stay green.

Two layers:

* an offline, always-run structural check — the recorded NDJSON of an
  interrupted call carries no ``tool_use`` without its ``tool_result``, which
  is the exact shape that makes a transcript unresumable. It runs against the
  real monitor loops over a real child process, for both runners;
* an opt-in live check against the installed CLIs (``TIANLUO_LIVE_CLI_TESTS=1``)
  that graceful-stops a REAL agent turn through the signal path and then
  resumes its session, which is the end-to-end claim itself. It is skipped by
  default because it spends provider quota and needs credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time

import pytest

from tianluo.claude_runner import ClaudeCodeRunner
from tianluo.codex_runner import CodexRunner
from tianluo.stop_signal import get_stop_signal


@pytest.fixture(autouse=True)
def _clean_signal():
    get_stop_signal().clear()
    yield
    get_stop_signal().clear()


# Comfortably above the monitor loops' 1s select tick, so each write lands on
# its own iteration (see tests/test_codex_graceful_stop.py's module docstring).
_GAP = 1.3


def _child(script: str) -> list:
    return [sys.executable, "-u", "-c", textwrap.dedent(script)]


def _emitter(*lines: str, tail: float = 60.0) -> str:
    body = ["import time"]
    for line in lines:
        body.append(f"print({line!r}, flush=True)")
        body.append(f"time.sleep({_GAP})")
    body.append(f"time.sleep({tail})")
    return "\n".join(body)


TOOL_USE = json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
    ]},
})
TOOL_RESULT = json.dumps({
    "type": "user",
    "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
    ]},
})
TEXT_ONLY = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "thinking"}]},
})

CODEX_THREAD_STARTED = json.dumps(
    {"type": "thread.started", "thread_id": "th_resume"}
)
CODEX_ITEM_STARTED = json.dumps({
    "type": "item.started",
    "item": {"id": "item_1", "type": "command_execution", "command": "ls"},
})
CODEX_ITEM_COMPLETED = json.dumps({
    "type": "item.completed",
    "item": {
        "id": "item_1", "type": "command_execution", "command": "ls",
        "aggregated_output": "ok", "exit_code": 0,
    },
})


def dangling_tool_uses(output: str) -> list:
    """Tool calls in *output* that never got a result — the unresumable shape.

    Both runners emit Claude stream-json (codex converts), so one reader serves
    both. A ``tool_result`` without a ``tool_use_id`` closes the oldest open
    call: the synthesized interrupted results and some provider builds omit the
    back-reference, and treating those as closing nothing would report a
    dangling call that is not there.
    """
    open_ids: list = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        content = obj.get("message") or {}
        content = content.get("content") if isinstance(content, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                open_ids.append(block.get("id"))
            elif block.get("type") == "tool_result":
                ref = block.get("tool_use_id")
                if ref in open_ids:
                    open_ids.remove(ref)
                elif open_ids:
                    open_ids.pop(0)
    return open_ids


def _stop_on_tool_use(sink=None):
    def _on_output(line):
        if sink is not None:
            sink.append(line)
        if "tool_use" in line:
            get_stop_signal().request()
    return _on_output


def _run_claude(runner, script, **kwargs):
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


def _run_codex(runner, script, **kwargs):
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


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestInterruptedTranscriptStaysResumable:
    """The recorded transcript never ends on a dangling tool call."""

    def test_the_reader_does_detect_a_dangling_call(self):
        """Guard on the guard: a check that can never fail proves nothing."""
        assert dangling_tool_uses(TOOL_USE) == ["t1"]
        assert dangling_tool_uses(TOOL_USE + "\n" + TOOL_RESULT) == []

    def test_claude_stopped_mid_tool_closes_the_call_first(self):
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        result = _run_claude(
            runner,
            _emitter(TOOL_USE, TOOL_RESULT),
            on_output=_stop_on_tool_use(),
        )
        assert result.interrupted is True
        assert dangling_tool_uses(result.output) == []

    def test_claude_stopped_at_a_text_turn_leaves_nothing_open(self):
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        result = _run_claude(
            runner,
            _emitter(TOOL_USE, TOOL_RESULT, TEXT_ONLY),
            on_output=_stop_on_tool_use(),
        )
        assert result.interrupted is True
        assert dangling_tool_uses(result.output) == []

    def test_codex_stopped_mid_item_closes_the_call_first(self):
        result = _run_codex(
            CodexRunner(command={"cmd": sys.executable, "priority": 0}),
            _emitter(
                CODEX_THREAD_STARTED, CODEX_ITEM_STARTED, CODEX_ITEM_COMPLETED
            ),
            on_output=_stop_on_tool_use(),
        )
        assert result.interrupted is True
        assert dangling_tool_uses(result.output) == []

    def test_codex_synthesizes_a_result_when_the_boundary_never_comes(
        self, monkeypatch
    ):
        """The boundary wait is bounded, so the stop CAN land mid-tool. That is
        the case the converter's ``finalize`` exists for — without it the
        recorded thread would end on an open call."""
        monkeypatch.setattr("tianluo.codex_runner.BOUNDARY_WAIT_SECONDS", 1.0)
        result = _run_codex(
            CodexRunner(command={"cmd": sys.executable, "priority": 0}),
            _emitter(CODEX_THREAD_STARTED, CODEX_ITEM_STARTED),
            on_output=_stop_on_tool_use(),
        )
        assert result.interrupted is True
        assert "tool_result" in result.output
        assert dangling_tool_uses(result.output) == []


# ---------------------------------------------------------------------------
# Live provider verification (opt-in)
# ---------------------------------------------------------------------------

_LIVE = os.environ.get("TIANLUO_LIVE_CLI_TESTS") == "1"


def _live_skip(cmd: str) -> str:
    if not _LIVE:
        return "set TIANLUO_LIVE_CLI_TESTS=1 to run the live provider checks"
    if shutil.which(cmd) is None:
        return f"{cmd} CLI is not installed"
    return ""


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestLiveResumeAfterGracefulStop:
    """Graceful-stop a REAL agent turn, then resume the session it left.

    This is the requirement's final clause end to end. It is opt-in: it spends
    provider quota, needs working credentials, and depends on the CLI versions
    installed on the machine.
    """

    def test_claude_session_resumes_after_a_graceful_stop(self, tmp_path):
        reason = _live_skip("claude")
        if reason:
            pytest.skip(reason)
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        # Metadata FIRST: it pre-allocates the session id that build_call_args
        # then injects as ``--session-id`` — the id this test resumes.
        session_id = runner.get_startup_metadata(
            dict(os.environ)
        ).provider_session_id
        assert session_id
        args = runner.build_call_args(
            prompt=(
                "Remember the word CANARY-7. Then list the files in this "
                "directory one at a time using the Bash tool, describing each."
            ),
            read_only=True,
        )

        def _on_output(line):
            if "tool_use" in line:
                get_stop_signal().request()

        result = runner.run_with_monitor(
            args=args, cwd=tmp_path, on_output=_on_output,
            inactivity_timeout=180,
        )
        assert result.interrupted is True, result.output
        assert dangling_tool_uses(result.output) == []

        resume_args = runner.build_resume_call_args(
            session_id=session_id,
            prompt="What word were you asked to remember? Answer with the word only.",
            read_only=True,
        )
        resumed = runner.run(resume_args, cwd=tmp_path, timeout=300)
        assert resumed.returncode == 0, resumed.stderr
        assert "CANARY-7" in resumed.stdout

    def test_codex_thread_resumes_after_a_graceful_stop(self, tmp_path):
        reason = _live_skip("codex")
        if reason:
            pytest.skip(reason)
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(
            prompt=(
                "Remember the word CANARY-7. Then list the files in this "
                "directory one at a time, describing each."
            ),
            read_only=True,
        )

        def _on_output(line):
            if "tool_use" in line:
                get_stop_signal().request()

        result = runner.run_with_monitor(
            args=args, cwd=tmp_path, on_output=_on_output,
            inactivity_timeout=180,
        )
        assert result.interrupted is True, result.output
        assert dangling_tool_uses(result.output) == []
        thread_id = runner.get_startup_metadata(dict(os.environ)).provider_session_id
        assert thread_id, "codex must have captured its thread id before the stop"

        resume_args = runner.build_resume_call_args(
            session_id=thread_id,
            prompt="What word were you asked to remember? Answer with the word only.",
            read_only=True,
        )
        resumed = runner.run(resume_args, cwd=tmp_path, timeout=300)
        assert resumed.returncode == 0, resumed.stderr
        assert "CANARY-7" in resumed.stdout
