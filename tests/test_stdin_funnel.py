"""One owner for a non-TTY stdin (``tianluo.stdin_channel``).

A CLI-mode flow can be launched with stdin as a pipe the launcher holds open.
The dual-channel dialog wait reads that pipe, and so do the gates that follow
it — ``prompt_user_choice``'s menu and the CONFIRM gate's feedback read. The
defect these cover: the wait used to park a thread in ``sys.stdin.read()`` and
abandon it whenever the web channel answered first. The parked reader outlived
its wait, and the operator's next answer — a gate choice, or the feedback text
behind "request changes" — went into its queue instead of to the consumer that
had asked for it, where the loss read as EOF and exited the flow.
"""

from __future__ import annotations

import io
import threading

import pytest

from tianluo import stdin_channel
from tianluo.cli import _read_multiline_input
from tianluo.commands import run as run_mod


@pytest.fixture(autouse=True)
def _clean_funnel():
    stdin_channel.reset()
    yield
    stdin_channel.reset()


@pytest.fixture
def _not_a_tty(monkeypatch):
    monkeypatch.setattr(run_mod.sys.stdin, "isatty", lambda: False)


class TestAnAbandonedReadConsumesNothing:
    def test_a_timed_out_read_leaves_the_bytes_for_the_next_consumer(self):
        """The whole point: losing the race must not eat the pipe."""
        stdin_channel.feed_for_test("", eof=False)

        assert stdin_channel.read_all(timeout=0.01) is stdin_channel.PENDING

        # The operator now types their gate choice into the same pipe.
        stdin_channel.append_for_test("2\n")
        assert stdin_channel.read_line(timeout=0.5) == "2"

    def test_the_gate_choice_survives_an_abandoned_dialog_read(
        self, _not_a_tty, capsys
    ):
        """End to end over the two real consumers, in the order the flow hits
        them: the dialog's bounded read gives up, then the menu reads."""
        stdin_channel.feed_for_test("", eof=False)
        assert (
            _read_multiline_input(
                prompt_title="t", prompt_message="m", timeout=0.01
            )
            is stdin_channel.PENDING
        )

        stdin_channel.append_for_test("1\n")
        assert run_mod.prompt_user_choice("pick", ["Retry", "Abort"]) == 0

    def test_confirm_feedback_is_not_swallowed_after_an_abandoned_read(
        self, _not_a_tty
    ):
        """The 'request changes' feedback read is the other victim; a lost
        answer there became ``feedback=None`` and exited the flow."""
        stdin_channel.feed_for_test("", eof=False)
        assert (
            _read_multiline_input(prompt_title="t", timeout=0.01)
            is stdin_channel.PENDING
        )

        stdin_channel.append_for_test("please rename the module\n", eof=True)
        assert _read_multiline_input(prompt_title="t") == "please rename the module"


class TestTheFunnelIsTheOnlyReader:
    def test_read_all_returns_everything_up_to_eof(self, monkeypatch):
        monkeypatch.setattr(run_mod.sys, "stdin", io.StringIO("a\nb\n"))
        assert stdin_channel.read_all(timeout=2) == "a\nb\n"

    def test_read_line_hands_out_one_line_at_a_time(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO("first\nsecond\n"))
        assert stdin_channel.read_line(timeout=2) == "first"
        assert stdin_channel.read_line(timeout=2) == "second"
        # EOF with nothing left is the caller's non-interactive fallback.
        assert stdin_channel.read_line(timeout=2) is None

    def test_an_unterminated_tail_is_still_a_line(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO("3"))
        assert stdin_channel.read_line(timeout=2) == "3"

    def test_a_menu_answer_reaches_the_menu_off_a_tty(self, _not_a_tty, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO("2\n"))
        assert run_mod.prompt_user_choice("pick", ["Retry", "Abort"]) == 1

    def test_eof_with_no_answer_selects_the_non_interactive_default(
        self, _not_a_tty, monkeypatch
    ):
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert run_mod.prompt_user_choice("pick", ["Retry", "Abort"]) == 1

    def test_a_swapped_stdin_starts_a_fresh_funnel(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO("old\n"))
        assert stdin_channel.read_line(timeout=2) == "old"
        monkeypatch.setattr(sys, "stdin", io.StringIO("new\n"))
        assert stdin_channel.read_line(timeout=2) == "new"

    def test_an_unreadable_stdin_reads_as_eof_rather_than_hanging(
        self, monkeypatch
    ):
        import sys

        class _Refuses:
            def readline(self):
                raise OSError("reading from stdin while output is captured")

            def isatty(self):
                return False

        monkeypatch.setattr(sys, "stdin", _Refuses())
        assert stdin_channel.read_all(timeout=2) is None


class TestTheDualWaitStillRacesBothChannels:
    def test_a_web_answer_wins_while_the_pipe_stays_open(self, tmp_path):
        from tianluo.engine import interaction_calls

        call_file = interaction_calls.write_dialog_call(
            tmp_path, flow_id="f1", step_id="s1", step_type="implement",
            prompt="p",
        )
        stdin_channel.feed_for_test("", eof=False)
        answered = threading.Event()

        def _tick():
            if not answered.is_set():
                answered.set()
                interaction_calls.write_response(call_file, "from the web")
            return None

        source, text = run_mod._await_terminal_or_web_non_tty(
            call_file,
            prompt_title="t",
            prompt_message="m",
            history=None,
            strip=True,
            poll_interval=0.01,
            tick_callback=_tick,
        )

        assert (source, text) == (run_mod._DISCOVERY_SRC_WEB, "from the web")
        # And nothing of stdin was taken by the wait it just lost.
        stdin_channel.append_for_test("2\n")
        assert stdin_channel.read_line(timeout=0.5) == "2"
