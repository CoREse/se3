"""Cooperative stop signal, stream message boundaries, and process-group stop.

The stop signal is what makes a mid-flow interruption *graceful*: the request
is published as a flag every runner polls (so it also reaches DAG group runners
on worker threads, which a ``KeyboardInterrupt`` never could), the runner waits
for a stream message boundary before signalling its child, and the child's
whole process group is wound down SIGINT-first so the provider session stays
resumable.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from tianluo.agent_runner import (
    drain_available_output,
    graceful_stop_process,
    is_message_boundary,
    reclaim_process_group,
    signal_process_group,
)
from tianluo.stop_signal import (
    STOP_REASON_INTERJECTION,
    STOP_REASON_INTERRUPT,
    InterjectionWatcher,
    StopSignal,
    llm_call_scope,
)


class TestStopSignal:
    def test_request_sets_and_carries_payload(self):
        sig = StopSignal()
        assert not sig.is_set()
        sig.request(reason=STOP_REASON_INTERJECTION, text="use Postgres", call_id="c1")
        assert sig.is_set()
        request = sig.pending
        assert request.reason == STOP_REASON_INTERJECTION
        assert request.texts == ["use Postgres"]
        assert request.call_ids == ["c1"]

    def test_second_request_extends_rather_than_nesting(self):
        """Two quick interjections must produce ONE dialog carrying both."""
        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERJECTION, text="first")
        sig.request(reason=STOP_REASON_INTERJECTION, text="second")
        assert sig.pending.texts == ["first", "second"]

    def test_take_is_atomic_and_clears(self):
        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        taken = sig.take()
        assert taken is not None
        assert not sig.is_set()
        assert sig.take() is None

    def test_llm_call_scope_tracks_nesting(self):
        sig = StopSignal()
        assert not sig.llm_active
        with llm_call_scope(sig):
            assert sig.llm_active
            with llm_call_scope(sig):
                assert sig.llm_active
            assert sig.llm_active
        assert not sig.llm_active

    def test_exit_never_goes_negative(self):
        sig = StopSignal()
        sig.exit_llm_call()
        assert not sig.llm_active


class TestInterjectionWatcher:
    def test_poll_publishes_drained_interjections(self, tmp_path):
        sig = StopSignal()
        drained = [
            {"text": "stop and explain", "call_id": "c1"},
            {"text": "also check the tests", "call_id": "c2"},
        ]
        watcher = InterjectionWatcher(
            tmp_path, signal=sig, drain_fn=lambda _root: drained
        )
        assert watcher.poll_once() is True
        assert sig.is_set()
        assert sig.pending.reason == STOP_REASON_INTERJECTION
        assert sig.pending.texts == ["stop and explain", "also check the tests"]

    def test_poll_with_nothing_queued_is_a_noop(self, tmp_path):
        sig = StopSignal()
        watcher = InterjectionWatcher(tmp_path, signal=sig, drain_fn=lambda _r: [])
        assert watcher.poll_once() is False
        assert not sig.is_set()

    def test_drain_failure_never_propagates(self, tmp_path):
        """A broken drain must not take the in-flight LLM call down with it."""
        sig = StopSignal()

        def _boom(_root):
            raise OSError("calls dir unreadable")

        watcher = InterjectionWatcher(tmp_path, signal=sig, drain_fn=_boom)
        assert watcher.poll_once() is False
        assert not sig.is_set()

    def test_a_step_with_no_llm_call_is_interrupted_like_ctrl_c(self, tmp_path):
        """TEST and friends do their work in Python and never poll the flag, so
        the request would sit unnoticed until the step finished on its own — and
        a confirmed ``continue`` would then rerun a whole suite the user asked to
        stop. Ctrl-C stops it at once; decision 5 makes the two one path."""
        sig = StopSignal()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "stop that", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        assert watcher.poll_once() is True
        assert raised == [True]

    def test_an_in_flight_llm_call_is_stopped_cooperatively(self, tmp_path):
        """While a runner supervises a child, raising would tear the supervisor
        down before it can wind the child down — the flag is the whole point."""
        sig = StopSignal()
        sig.enter_llm_call()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "stop that", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        watcher.poll_once()
        assert raised == []
        assert sig.is_set()

    def test_escalation_needs_a_main_thread_owner(self, tmp_path):
        """Python can only raise into the main thread, so a watcher owned by
        any other thread must stay cooperative — otherwise the exception lands
        in an unrelated control flow (a pytest-xdist worker dies outright)."""
        import threading
        import types

        import tianluo.stop_signal as mod

        raised = []
        built = []

        def _build():
            built.append(
                InterjectionWatcher(
                    tmp_path, signal=StopSignal(),
                    drain_fn=lambda _r: [{"text": "hi", "call_id": "c1"}],
                    escalate_to_main=True,
                )
            )

        worker = threading.Thread(target=_build)
        worker.start()
        worker.join()

        real_thread = mod._thread
        mod._thread = types.SimpleNamespace(
            interrupt_main=lambda: raised.append(True)
        )
        try:
            built[0].poll_once()
        finally:
            mod._thread = real_thread
        assert raised == []

    def test_escalation_is_opt_in(self, tmp_path):
        """A bare poller must never be able to throw into whatever its process
        happens to be doing."""
        sig = StopSignal()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "hi", "call_id": "c1"}],
            interrupt_main=lambda: raised.append(True),
        )
        watcher.poll_once()
        assert raised == []

    def test_escalation_happens_once(self, tmp_path):
        sig = StopSignal()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "hi", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        watcher.poll_once()
        watcher.poll_once()
        assert raised == [True]

    def test_no_escalation_after_stop(self, tmp_path):
        """Past stop() the caller is outside the region that catches it."""
        sig = StopSignal()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "hi", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        watcher.stop()
        watcher.poll_once()
        assert raised == []

    def test_start_stop_is_idempotent(self, tmp_path):
        sig = StopSignal()
        watcher = InterjectionWatcher(
            tmp_path, signal=sig, poll_interval=0.01, drain_fn=lambda _r: []
        )
        watcher.start().start()
        watcher.stop()
        watcher.stop()


class TestMessageBoundary:
    def test_settled_tool_result_is_a_boundary(self):
        line = json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok"}]},
        })
        assert is_message_boundary(line)

    def test_assistant_without_tool_use_is_a_boundary(self):
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done"}]},
        })
        assert is_message_boundary(line)

    def test_assistant_issuing_a_tool_use_is_not_a_boundary(self):
        """Stopping here would leave a dangling tool_use in the transcript."""
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "let me look"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ]},
        })
        assert not is_message_boundary(line)

    def test_terminal_result_is_a_boundary(self):
        assert is_message_boundary(json.dumps({"type": "result", "result": "x"}))

    def test_unparseable_line_is_never_a_boundary(self):
        # An unknown line must not shorten the bounded wait.
        assert not is_message_boundary("not json at all")
        assert not is_message_boundary("")
        assert not is_message_boundary(json.dumps([1, 2, 3]))

    def test_system_init_line_is_not_a_boundary(self):
        assert not is_message_boundary(
            json.dumps({"type": "system", "subtype": "init"})
        )


def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
class TestGracefulStop:
    def test_child_runs_in_its_own_process_group(self):
        """The child must not share ``luo run``'s group, or a terminal Ctrl-C
        would kill it outright instead of letting the runner wind it down."""
        proc = _spawn("import os,time,sys; print(os.getpgid(0), flush=True); time.sleep(30)")
        try:
            child_pgid = int(proc.stdout.readline().strip())
            assert child_pgid == proc.pid
            assert child_pgid != os.getpgid(0)
        finally:
            signal_process_group(proc, signal.SIGKILL)
            proc.wait(timeout=10)

    def test_sigint_is_tried_before_sigkill(self):
        """A child that handles SIGINT gets to wind down and exit on its own.

        That ordering is what leaves a provider session resumable, so it is
        asserted from the child's OWN evidence — it ran its wind-down and
        exited 0 — rather than from wall-clock timing, which a loaded parallel
        run cannot bound reliably. An escalation would have shown as -SIGKILL.

        WHY the handler only sets a flag and the MAIN loop does the printing:
        the parent sends SIGINT the moment it reads "ready", and under load the
        child can still be inside that very ``print`` when the signal lands.
        A handler that wrote to the same stream would re-enter its
        ``BufferedWriter`` and die with ``RuntimeError`` (exit 1) — a defect in
        this fixture's child, not in the stop path it is meant to exercise.
        """
        proc = _spawn(
            """
            import signal, sys, time
            _stopping = False
            def _bye(signum, frame):
                global _stopping
                _stopping = True
            signal.signal(signal.SIGINT, _bye)
            print("ready", flush=True)
            deadline = time.time() + 60
            while time.time() < deadline:
                if _stopping:
                    print("clean-exit", flush=True)
                    sys.exit(0)
                time.sleep(0.01)
            """
        )
        assert proc.stdout.readline().strip() == "ready"
        graceful_stop_process(proc, exit_wait=30, poll_interval=0.05)
        assert proc.poll() == 0
        assert "clean-exit" in proc.stdout.read()

    def test_escalates_to_sigkill_when_sigint_is_ignored(self):
        proc = _spawn(
            """
            import signal, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            print("ready", flush=True)
            time.sleep(60)
            """
        )
        assert proc.stdout.readline().strip() == "ready"
        graceful_stop_process(proc, exit_wait=1.0, poll_interval=0.05)
        assert proc.poll() is not None
        assert proc.returncode == -signal.SIGKILL

    def test_grandchildren_are_reaped_with_the_group(self):
        """The escalation targets the GROUP, so tool/bash grandchildren die too."""
        proc = _spawn(
            """
            import signal, subprocess, sys, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(kid.pid, flush=True)
            time.sleep(60)
            """
        )
        grandchild = int(proc.stdout.readline().strip())
        graceful_stop_process(proc, exit_wait=1.0, poll_interval=0.05)
        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(grandchild, 0)
            except OSError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive, "grandchild survived the process-group kill"

    def test_wind_down_output_larger_than_the_pipe_buffer_is_drained(self):
        """A child writing its closing output must not deadlock on a full pipe.

        Claude Code's wind-down after SIGINT can be a large ``tool_result`` plus
        the terminal ``result`` line — well past the 64 KiB pipe buffer. A
        parent that only polls ``proc.poll()`` leaves the child blocked in
        ``write()``, so it never exits, is SIGKILL-ed at the deadline, and its
        session is left mid-tool.
        """
        proc = _spawn(
            """
            import signal, sys, time
            _stopping = False
            def _bye(signum, frame):
                global _stopping
                _stopping = True
            signal.signal(signal.SIGINT, _bye)
            print("ready", flush=True)
            deadline = time.time() + 60
            while time.time() < deadline:
                if _stopping:
                    # ~1 MiB: far beyond any pipe buffer.
                    for i in range(4096):
                        sys.stdout.write("x" * 255 + "\\n")
                    sys.stdout.write("clean-exit\\n")
                    sys.stdout.flush()
                    sys.exit(0)
                time.sleep(0.01)
            """
        )
        assert proc.stdout.readline().strip() == "ready"
        collected = []
        graceful_stop_process(
            proc,
            exit_wait=15,
            poll_interval=0.02,
            drain=lambda: drain_available_output(proc.stdout, collected.append),
        )
        assert proc.poll() == 0, "child was killed instead of winding down"
        collected.append(proc.stdout.read())
        blob = "".join(collected)
        assert "clean-exit" in blob
        assert len(blob) > 64 * 1024

    def test_drain_available_output_never_blocks(self):
        proc = _spawn("import time; print('one', flush=True); time.sleep(30)")
        try:
            seen = []
            deadline = time.time() + 10
            while not seen and time.time() < deadline:
                drain_available_output(proc.stdout, seen.append)
                if not seen:
                    time.sleep(0.02)
            assert seen == ["one\n"]
            # Nothing more is pending: this must return at once, not block.
            drain_available_output(proc.stdout, seen.append)
            assert seen == ["one\n"]
        finally:
            reclaim_process_group(proc)

    def test_stop_on_an_already_dead_process_is_a_noop(self):
        proc = _spawn("pass")
        proc.wait(timeout=10)
        graceful_stop_process(proc, exit_wait=0.1)

    def test_signal_process_group_reports_failure_for_a_dead_pid(self):
        proc = _spawn("pass")
        proc.wait(timeout=10)
        assert signal_process_group(proc, signal.SIGKILL) is False


class TestUninterruptibleSections:
    """An LLM step does more than call an LLM: it merges leaf branches, stashes,
    checks out. A KeyboardInterrupt landing inside one of those multi-command
    git sequences leaves a MERGE_HEAD behind, and the very step the operator
    asked to interrupt then refuses to re-run."""

    def test_the_scope_marks_and_clears_the_flag(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        assert not sig.uninterruptible
        with uninterruptible_scope(sig):
            assert sig.uninterruptible
        assert not sig.uninterruptible

    def test_nesting_is_counted(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        with uninterruptible_scope(sig):
            with uninterruptible_scope(sig):
                assert sig.uninterruptible
            assert sig.uninterruptible
        assert not sig.uninterruptible

    def test_exit_never_goes_negative(self):
        sig = StopSignal()
        sig.exit_uninterruptible()
        assert not sig.uninterruptible

    def test_escalation_is_deferred_inside_a_section(self, tmp_path):
        sig = StopSignal()
        sig.enter_uninterruptible()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "stop that", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        assert watcher.poll_once() is True
        # The request IS published — only the exception waits.
        assert sig.is_set()
        assert raised == []

    def test_the_deferred_escalation_fires_once_the_section_ends(self, tmp_path):
        sig = StopSignal()
        sig.enter_uninterruptible()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "stop that", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        watcher.poll_once()
        assert raised == []

        # The call file is already consumed, so no later drain would re-raise
        # it: the retry has to come from the pending flag.
        sig.exit_uninterruptible()
        watcher._drain_fn = lambda _r: []
        watcher.poll_once()
        watcher._escalate_if_no_llm_call()
        assert raised == [True]

    def test_a_deferred_escalation_survives_an_llm_call_too(self, tmp_path):
        sig = StopSignal()
        sig.enter_llm_call()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "stop", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        watcher.poll_once()
        assert raised == []
        sig.exit_llm_call()
        watcher._escalate_if_no_llm_call()
        assert raised == [True]


class TestDeferredInterruptDelivery:
    """Ctrl-C inside a non-interruptible section must not tear a multi-command
    git sequence apart: the SIGINT handler publishes the request and defers the
    raise to the section's exit (the same deferral the watcher performs for the
    web path), and each pending request interrupts the main thread exactly once.
    """

    def test_scope_exit_raises_a_request_published_inside(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        with pytest.raises(KeyboardInterrupt):
            with uninterruptible_scope(sig):
                # What _sigint_handler does while the section runs.
                sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.interrupt_delivered

    def test_the_raise_waits_for_the_outermost_section(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        with pytest.raises(KeyboardInterrupt):
            with uninterruptible_scope(sig):
                sig.request(reason=STOP_REASON_INTERRUPT)
                with uninterruptible_scope(sig):
                    pass  # inner exit must NOT raise: the sequence continues

    def test_no_raise_when_the_section_body_failed(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        with pytest.raises(RuntimeError, match="boom"):
            with uninterruptible_scope(sig):
                sig.request(reason=STOP_REASON_INTERRUPT)
                raise RuntimeError("boom")
        # The interrupt was NOT delivered by the scope — the section did not
        # complete — but the request stays published for the next breakpoint.
        assert sig.is_set()
        assert not sig.interrupt_delivered

    def test_no_second_delivery_of_the_same_request(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        sig.note_interrupt_delivered()  # the SIGINT handler already raised it
        with uninterruptible_scope(sig):
            pass

    def test_no_raise_on_a_worker_thread(self):
        import threading

        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        outcome = []

        def _worker():
            try:
                with uninterruptible_scope(sig):
                    sig.request(reason=STOP_REASON_INTERRUPT)
                outcome.append("no-raise")
            except KeyboardInterrupt:
                outcome.append("raised")

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(10)
        assert outcome == ["no-raise"]
        assert sig.is_set()

    def test_no_raise_while_an_llm_call_owns_the_stop(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        sig.enter_llm_call()
        with uninterruptible_scope(sig):
            sig.request(reason=STOP_REASON_INTERRUPT)
        # The runner consumes the request cooperatively; nobody may raise.
        assert not sig.interrupt_delivered

    def test_the_next_request_interrupts_again(self):
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        sig.note_interrupt_delivered()
        sig.clear()  # the dialog consumed the first request

        with pytest.raises(KeyboardInterrupt):
            with uninterruptible_scope(sig):
                sig.request(reason=STOP_REASON_INTERRUPT)

    def test_the_watcher_does_not_double_deliver(self, tmp_path):
        """A request the scope exit already raised is not escalated again."""
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        sig.enter_uninterruptible()
        raised = []
        watcher = InterjectionWatcher(
            tmp_path, signal=sig,
            drain_fn=lambda _r: [{"text": "stop", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=lambda: raised.append(True),
        )
        watcher.poll_once()
        assert raised == []  # deferred while the section runs
        sig.exit_uninterruptible()
        # The section exit delivered the interrupt itself.
        try:
            sig.maybe_raise_deferred_interrupt()
        except KeyboardInterrupt:
            pass
        # The watcher's retry must observe the delivery and stand down.
        watcher._drain_fn = lambda _r: []
        watcher._escalate_if_no_llm_call()
        assert raised == []
        assert not watcher._escalation_pending


class TestSigintHandlerDeferral:
    """run.py's SIGINT handler: publish always, raise only when safe."""

    def _handler(self):
        from tianluo.commands.run import _sigint_handler

        return _sigint_handler

    def test_ctrl_c_inside_a_section_defers_the_raise(self, monkeypatch):
        from tianluo import stop_signal
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        monkeypatch.setattr(stop_signal, "_STOP_SIGNAL", sig)
        handler = self._handler()
        with pytest.raises(KeyboardInterrupt):
            with uninterruptible_scope(sig):
                handler(signal.SIGINT, None)  # publishes, must NOT raise here
                assert sig.is_set()

    def test_ctrl_c_with_nothing_in_flight_raises_at_once(self, monkeypatch):
        from tianluo import stop_signal

        sig = StopSignal()
        monkeypatch.setattr(stop_signal, "_STOP_SIGNAL", sig)
        with pytest.raises(KeyboardInterrupt):
            self._handler()(signal.SIGINT, None)
        assert sig.interrupt_delivered

    def test_ctrl_c_with_an_llm_in_flight_publishes_only(self, monkeypatch):
        from tianluo import stop_signal

        sig = StopSignal()
        monkeypatch.setattr(stop_signal, "_STOP_SIGNAL", sig)
        sig.enter_llm_call()
        self._handler()(signal.SIGINT, None)  # no raise
        assert sig.is_set()
        assert not sig.interrupt_delivered


class TestInterruptDeliveryClaim:
    """The delivered check-and-claim is ONE atomic operation: whichever
    delivery channel (SIGINT handler / watcher / scope exit) reaches a pending
    request first delivers it, and every other channel stands down — a raced
    publication must interrupt the main thread exactly once."""

    def test_exactly_one_of_many_racing_claimants_wins(self):
        import threading

        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        outcomes = []
        barrier = threading.Barrier(8)

        def _claim():
            barrier.wait(5)
            outcomes.append(sig.claim_interrupt_delivery())

        threads = [threading.Thread(target=_claim) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        assert outcomes.count(True) == 1
        assert outcomes.count(False) == 7

    def test_a_fresh_request_is_claimable_again(self):
        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.claim_interrupt_delivery() is True
        assert sig.claim_interrupt_delivery() is False
        sig.clear()
        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.claim_interrupt_delivery() is True

    def test_scope_exit_and_watcher_retry_never_both_deliver(
        self, tmp_path, monkeypatch
    ):
        """The reported race: the watcher's deferred retry runs while the
        scope exit claims the same request. Whichever order they run in, the
        request is delivered exactly once.

        The watcher's delivery goes through ``_sigint_handler`` because that is
        what ``_thread.interrupt_main`` really does — it trips SIGINT rather
        than raising — so the handoff, not the callback, is what turns the
        escalation's claim into the exception.
        """
        from tianluo import stop_signal as mod
        from tianluo.commands.run import _sigint_handler
        from tianluo.stop_signal import uninterruptible_scope

        for order in ("scope-first", "watcher-first"):
            sig = StopSignal()
            monkeypatch.setattr(mod, "_STOP_SIGNAL", sig)
            raised = []

            def _trip_sigint():
                raised.append("watcher")
                _sigint_handler(signal.SIGINT, None)

            watcher = InterjectionWatcher(
                tmp_path, signal=sig,
                drain_fn=lambda _r: [{"text": "stop", "call_id": "c1"}],
                escalate_to_main=True,
                interrupt_main=_trip_sigint,
            )
            sig.enter_uninterruptible()
            watcher.poll_once()  # publishes + defers
            sig.exit_uninterruptible()
            if order == "scope-first":
                with pytest.raises(KeyboardInterrupt):
                    sig.maybe_raise_deferred_interrupt()
                watcher._drain_fn = lambda _r: []
                watcher._escalate_if_no_llm_call()
            else:
                watcher._drain_fn = lambda _r: []
                with pytest.raises(KeyboardInterrupt):
                    watcher._escalate_if_no_llm_call()
                sig.maybe_raise_deferred_interrupt()  # must NOT raise
            assert raised == ([] if order == "scope-first" else ["watcher"])
            assert not watcher._escalation_pending

    def test_the_sigint_handler_stands_down_when_already_claimed(
        self, monkeypatch
    ):
        from tianluo import stop_signal

        sig = StopSignal()
        monkeypatch.setattr(stop_signal, "_STOP_SIGNAL", sig)
        from tianluo.commands.run import _sigint_handler

        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.claim_interrupt_delivery() is True  # watcher won the race
        # The handler must NOT raise a second interrupt for the same request.
        _sigint_handler(signal.SIGINT, None)


class TestEscalationHandoff:
    """``_thread.interrupt_main`` does not raise — it trips SIGINT, so the
    watcher's escalation reaches the main thread THROUGH ``_sigint_handler``.
    The single-delivery claim must therefore travel with a handoff, or the
    escalation silences the very handler that has to do the raising and the
    web interjection never interrupts anything.
    """

    def _watcher(self, tmp_path, sig, interrupt_main):
        return InterjectionWatcher(
            tmp_path,
            signal=sig,
            drain_fn=lambda _r: [{"text": "stop that", "call_id": "c1"}],
            escalate_to_main=True,
            interrupt_main=interrupt_main,
        )

    def _handler_delivery(self, monkeypatch, sig):
        """Wire ``interrupt_main`` to what tripping SIGINT actually does."""
        from tianluo import stop_signal as mod
        from tianluo.commands.run import _sigint_handler

        monkeypatch.setattr(mod, "_STOP_SIGNAL", sig)
        return lambda: _sigint_handler(signal.SIGINT, None)

    def test_escalation_through_the_sigint_handler_really_raises(
        self, tmp_path, monkeypatch
    ):
        """The reported defect: the watcher took the single claim, the handler
        it woke found the request already claimed and stood down, and no
        KeyboardInterrupt ever reached the main thread."""
        sig = StopSignal()
        watcher = self._watcher(
            tmp_path, sig, self._handler_delivery(monkeypatch, sig)
        )
        with pytest.raises(KeyboardInterrupt):
            watcher.poll_once()
        assert sig.is_set()
        assert sig.interrupt_delivered

    def test_a_web_interjection_and_ctrl_c_deliver_identically(
        self, tmp_path, monkeypatch
    ):
        """Decision 5: the two entry points are ONE path. A TEST step busy in
        Python must be cut short the same way by either."""
        from tianluo import stop_signal as mod
        from tianluo.commands.run import _sigint_handler

        ctrl_c = StopSignal()
        monkeypatch.setattr(mod, "_STOP_SIGNAL", ctrl_c)
        with pytest.raises(KeyboardInterrupt):
            _sigint_handler(signal.SIGINT, None)

        web = StopSignal()
        watcher = self._watcher(
            tmp_path, web, self._handler_delivery(monkeypatch, web)
        )
        with pytest.raises(KeyboardInterrupt):
            watcher.poll_once()

    def test_the_handoff_is_spent_by_the_first_raise_only(
        self, tmp_path, monkeypatch
    ):
        """A second SIGINT for the same request must not interrupt twice."""
        sig = StopSignal()
        deliver = self._handler_delivery(monkeypatch, sig)
        watcher = self._watcher(tmp_path, sig, deliver)
        with pytest.raises(KeyboardInterrupt):
            watcher.poll_once()
        deliver()  # must NOT raise: the handoff is already spent

    def test_a_plain_claim_still_stands_the_handler_down(self, monkeypatch):
        """Only an escalation arms a handoff. A channel that raised by itself
        leaves none, and the handler stays silent as before."""
        from tianluo import stop_signal as mod
        from tianluo.commands.run import _sigint_handler

        sig = StopSignal()
        monkeypatch.setattr(mod, "_STOP_SIGNAL", sig)
        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.claim_interrupt_delivery() is True
        _sigint_handler(signal.SIGINT, None)  # no raise

    def test_a_handoff_stranded_by_a_section_is_raised_at_its_exit(
        self, tmp_path, monkeypatch
    ):
        """The escalation may win the claim just as the main thread enters a
        non-interruptible section; the handler then stands down and the
        section's exit is the next breakpoint that may raise."""
        from tianluo.stop_signal import uninterruptible_scope

        sig = StopSignal()
        deliver = self._handler_delivery(monkeypatch, sig)
        watcher = self._watcher(tmp_path, sig, deliver)
        sig.enter_uninterruptible()
        try:
            watcher.poll_once()  # claims + hands off; the handler defers
        finally:
            sig.exit_uninterruptible()
        with pytest.raises(KeyboardInterrupt):
            with uninterruptible_scope(sig):
                pass

    def test_a_handoff_does_not_outlive_its_request(self, tmp_path):
        """The dialog consumed the request; a stale handoff must not make the
        next Ctrl-C raise on a delivery nobody claimed."""
        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERJECTION, text="hi")
        assert sig.claim_escalation_delivery() is True
        assert sig.take() is not None
        assert sig.consume_escalation_handoff() is False

        sig.request(reason=STOP_REASON_INTERJECTION, text="again")
        assert sig.claim_escalation_delivery() is True
        sig.clear()
        assert sig.consume_escalation_handoff() is False

    def test_a_fresh_request_disarms_a_stale_handoff(self):
        """A handoff armed for a request nobody ever raised must not be spent
        on the NEXT request's delivery."""
        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.claim_escalation_delivery() is True
        sig.take()
        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.consume_escalation_handoff() is False
        assert sig.claim_interrupt_delivery() is True

    def test_only_one_escalation_claim_per_request(self):
        sig = StopSignal()
        sig.request(reason=STOP_REASON_INTERRUPT)
        assert sig.claim_escalation_delivery() is True
        assert sig.claim_escalation_delivery() is False
        assert sig.claim_interrupt_delivery() is False
