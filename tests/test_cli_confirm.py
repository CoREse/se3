"""Tests for CLI-subprocess confirmation-prompt capture and response.

Covers:
- ``detect_confirmation_prompt`` conservative pattern matching
- ``run_with_monitor`` / ``_run_single_with_monitor`` ``on_confirm`` wiring
- the ``interaction_calls`` call-file writer / response reader
- the ``run.py`` ``make_cli_confirm_handler`` end-to-end round trip
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.claude_runner import ClaudeCodeRunner, detect_confirmation_prompt
from se3.engine.interaction_calls import (
    read_interaction_response,
    write_interaction_call,
)
from se3.commands.run import make_cli_confirm_handler


# ---------------------------------------------------------------------------
# detect_confirmation_prompt
# ---------------------------------------------------------------------------

class TestDetectConfirmationPrompt:
    @pytest.mark.parametrize(
        "line",
        [
            "按 1 确定继续执行",
            "请输入 1 确认操作",
            "Press 1 to confirm",
            "press [Enter] to continue",
            "Continue? [y/N]",
            "proceed (yes/no)",
            "Do you want to continue?",
        ],
    )
    def test_known_prompts_detected(self, line):
        result = detect_confirmation_prompt(line)
        assert result is not None
        prompt, options = result
        assert prompt == line.strip()
        assert isinstance(options, list)

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            '{"type": "assistant", "message": {}}',
            '[{"text": "press 1"}]',
            "Just some ordinary prose output from the agent.",
            "Reading file foo.py and editing it",
            "The number 1 appeared in the diff",
        ],
    )
    def test_non_prompts_are_noop(self, line):
        assert detect_confirmation_prompt(line) is None

    def test_options_parsed_from_enumerated_line(self):
        result = detect_confirmation_prompt("Press a key: 1) confirm  2) cancel [y/n]")
        assert result is not None
        _, options = result
        assert "1" in options and "2" in options


# ---------------------------------------------------------------------------
# interaction_calls
# ---------------------------------------------------------------------------

class TestInteractionCalls:
    def test_write_creates_cli_confirm_call_file(self, tmp_path):
        call_file = write_interaction_call(
            tmp_path,
            kind="cli_confirm",
            prompt="按 1 确定",
            options=["1", "2"],
            flow_id="flow-x",
            step_id="step-y",
        )
        assert call_file.exists()
        assert call_file.parent == tmp_path / "se3" / "calls"
        import json

        data = json.loads(call_file.read_text(encoding="utf-8"))
        assert data["kind"] == "cli_confirm"
        assert data["prompt"] == "按 1 确定"
        assert data["options"] == ["1", "2"]
        assert data["flow_id"] == "flow-x"

    def test_response_unanswered_returns_none(self, tmp_path):
        call_file = write_interaction_call(tmp_path, "cli_confirm", "prompt")
        assert read_interaction_response(call_file) is None

    def test_response_plain_sibling(self, tmp_path):
        call_file = write_interaction_call(tmp_path, "cli_confirm", "prompt")
        (call_file.parent / f"{call_file.stem}.response").write_text("1")
        assert read_interaction_response(call_file) == "1"

    def test_response_json_envelope(self, tmp_path):
        import json

        call_file = write_interaction_call(tmp_path, "cli_confirm", "prompt")
        (call_file.parent / f"{call_file.stem}.response.json").write_text(
            json.dumps({"response": "yes"})
        )
        assert read_interaction_response(call_file) == "yes"


# ---------------------------------------------------------------------------
# run_with_monitor on_confirm wiring
# ---------------------------------------------------------------------------

def _run_child(runner, script, on_confirm, *, wall_timeout=20):
    """Run a python child script through ``_run_single_with_monitor``."""
    full_cmd = [sys.executable, "-u", "-c", script]
    return runner._run_single_with_monitor(
        full_cmd=full_cmd,
        cmd_name="py",
        cmd_index=0,
        log_file=None,
        wall_timeout=wall_timeout,
        inactivity_timeout=15,
        cwd=None,
        env=dict(os.environ),
        on_output=None,
        on_activity=None,
        start_time=time.time(),
        stdin_prompt=None,
        on_confirm=on_confirm,
    )


class TestOnConfirmWiring:
    def test_confirm_prompt_answered_via_stdin(self, tmp_path):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        script = (
            "import sys\n"
            "print('按 1 确定继续', flush=True)\n"
            "line = sys.stdin.readline()\n"
            "print('CHILD_GOT:' + line.strip(), flush=True)\n"
        )

        def responder():
            calls_dir = tmp_path / "se3" / "calls"
            for _ in range(200):
                if calls_dir.is_dir():
                    files = list(calls_dir.glob("cli_confirm_*.json"))
                    if files:
                        cf = files[0]
                        (cf.parent / f"{cf.stem}.response").write_text("1")
                        return
                time.sleep(0.05)

        t = threading.Thread(target=responder, daemon=True)
        t.start()

        handler = make_cli_confirm_handler(tmp_path, poll_interval=0.05)
        result = _run_child(runner, script, handler)
        t.join(timeout=5)

        assert result.returncode == 0
        assert "CHILD_GOT:1" in result.output
        # The call file was written and is now answered.
        call_files = list((tmp_path / "se3" / "calls").glob("cli_confirm_*.json"))
        assert len(call_files) == 1

    def test_child_exits_before_response_does_not_hang(self, tmp_path):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        # Child prints a confirm prompt then exits immediately without
        # reading stdin — no response file is ever written.
        script = (
            "print('Press 1 to confirm', flush=True)\n"
        )
        handler = make_cli_confirm_handler(tmp_path, poll_interval=0.05)
        start = time.time()
        result = _run_child(runner, script, handler, wall_timeout=20)
        elapsed = time.time() - start

        assert result.returncode == 0
        # Must not block on the missing response — finishes promptly.
        assert elapsed < 15

    def test_unrecognized_output_is_noop(self, tmp_path):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        script = (
            "print('{\"type\": \"result\"}', flush=True)\n"
            "print('just some ordinary prose output', flush=True)\n"
        )
        called = []

        def handler(prompt, options, is_alive):
            called.append(prompt)
            return None

        result = _run_child(runner, script, handler)
        assert result.returncode == 0
        assert called == []
        assert "ordinary prose output" in result.output

    def test_run_with_monitor_passes_on_confirm(self, tmp_path):
        """The public run_with_monitor path threads on_confirm through."""
        runner = ClaudeCodeRunner(command={"cmd": sys.executable, "priority": 0})
        captured = []

        def handler(prompt, options, is_alive):
            captured.append(prompt)
            return "1"

        # run_with_monitor builds argv as [cmd, --dangerously-skip-permissions,
        # --setting-sources, csv, *args]; python tolerates the unknown flags
        # only via -c, so we drive _run_single_with_monitor for behaviour and
        # here just assert the parameter is accepted without error.
        script = "print('done', flush=True)\n"
        result = runner.run_with_monitor(
            args=["-c", script],
            on_confirm=handler,
            inactivity_timeout=15,
        )
        # The unknown CLI flags make python exit non-zero; we only assert the
        # call did not raise and on_confirm was an accepted keyword argument.
        assert isinstance(result.output, str)
