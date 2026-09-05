"""Process-wide cooperative stop signal for in-flight LLM subprocess calls.

WHY this lives at the package top level rather than under ``engine/``: the
agent runners are its primary consumers and they sit above ``engine`` in the
import order, so an ``engine`` home would make every runner import pull in the
whole flow engine.

WHY this exists instead of ``KeyboardInterrupt``: the DAG-parallel implement
step runs every group inside a :class:`~concurrent.futures.ThreadPoolExecutor`
worker, and CPython delivers ``KeyboardInterrupt`` only to the main thread. A
Ctrl-C (or a web-pushed interjection) therefore cannot reach a group runner
through the exception channel at all. The stop request is instead published as
a *flag* that every runner's monitor loop polls once per tick, so the same
signal reaches the sequential runner on the main thread and N parallel group
runners in worker threads through exactly one mechanism.

INVARIANT: setting the signal never terminates anything by itself. The
requester only publishes intent; each runner owns its own child's graceful
shutdown (wait for a stream message boundary, SIGINT the process group, then
escalate to SIGKILL) and each :class:`~tianluo.engine.llm_caller.LLMCaller`
checks the flag before spawning a new attempt. This keeps process-group
lifetime ownership where the process was created.

The signal is a module-level singleton because a stop request is a property of
the whole ``luo run`` process (the operator pressed Ctrl-C *once*), not of any
one call site; :func:`get_stop_signal` is the only accessor.
"""

from __future__ import annotations

import _thread
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Reason codes carried by a :class:`StopRequest`.
STOP_REASON_INTERRUPT = "interrupt"
STOP_REASON_INTERJECTION = "interjection"

#: Seconds a runner waits for the next stream message boundary after a stop
#: request before it stops waiting and signals the child anyway.
BOUNDARY_WAIT_SECONDS = 30.0

#: Seconds a runner waits for the child process group to exit after SIGINT
#: before escalating to SIGKILL.
EXIT_WAIT_SECONDS = 30.0


@dataclass
class StopRequest:
    """One published request to stop the in-flight LLM work.

    ``texts`` carries the user messages that arrived with the request (a web
    interjection's body; empty for a bare Ctrl-C). The dialog layer consumes
    them as its opening user turns, which is why they travel with the signal
    rather than through a separate queue that could be drained out of order.
    """

    reason: str = STOP_REASON_INTERRUPT
    texts: List[str] = field(default_factory=list)
    call_ids: List[str] = field(default_factory=list)
    requested_at: float = field(default_factory=time.time)


class StopSignal:
    """Thread-safe, level-triggered stop flag with an attached payload."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._event = threading.Event()
        self._request: Optional[StopRequest] = None
        # Count of LLM subprocess calls currently in flight. The SIGINT handler
        # reads it to decide whether Ctrl-C should raise: while a runner is
        # supervising a child, raising would tear the supervisor down before it
        # can wind the child down gracefully, so the signal is published and
        # the runner acts on it. Outside such a call (lock wait, terminal read)
        # there is nothing to wind down and the exception is the right — and
        # the only — way to interrupt a blocking read.
        self._llm_calls = 0
        # Depth of nested non-interruptible sections. An LLM step does more
        # than call an LLM: it merges leaf branches, stashes, checks out. Those
        # are multi-command git sequences with an intermediate on-disk state
        # (MERGE_HEAD, a live stash), and a KeyboardInterrupt landing inside one
        # leaves the repository mid-merge — a state the very step the user asked
        # to interrupt then refuses to re-run. Escalation therefore waits for
        # such a section to finish; the request itself stays published, so
        # nothing is lost, only deferred.
        self._uninterruptible = 0
        # Set once the pending request has been delivered to the main thread as
        # a KeyboardInterrupt — by the SIGINT handler, the watcher's
        # interrupt_main, or an uninterruptible_scope exit raising a deferred
        # one. INVARIANT: delivery is claimed through
        # :meth:`claim_interrupt_delivery`, which checks AND sets this flag in
        # one ``self._lock`` acquisition. A split check-then-note would let two
        # channels racing the same publication both deliver — the second
        # exception landing while the first still propagates can escape the
        # step's interrupt handlers entirely, losing the interjection text the
        # watcher already drained off disk and stranding the step RUNNING.
        self._interrupt_delivered = False
        # Set when the claim above was taken by a channel that does not itself
        # raise but ASKS the main thread's SIGINT handler to (the watcher's
        # ``_thread.interrupt_main``, which only trips SIGINT). INVARIANT: the
        # handler is the one that turns such a claim into the exception, so it
        # must be able to tell "someone else already delivered this" (stand
        # down) from "I am the delivery that was claimed for me" (raise) —
        # without the distinction the escalation claims the single delivery and
        # then nobody raises at all, and the request dies silently.
        self._escalation_handoff = False

    # -- in-flight LLM call tracking -------------------------------------

    @property
    def llm_active(self) -> bool:
        with self._lock:
            return self._llm_calls > 0

    def enter_llm_call(self) -> None:
        with self._lock:
            self._llm_calls += 1

    def exit_llm_call(self) -> None:
        with self._lock:
            self._llm_calls = max(0, self._llm_calls - 1)

    # -- non-interruptible sections --------------------------------------

    @property
    def uninterruptible(self) -> bool:
        with self._lock:
            return self._uninterruptible > 0

    def enter_uninterruptible(self) -> None:
        with self._lock:
            self._uninterruptible += 1

    def exit_uninterruptible(self) -> None:
        with self._lock:
            self._uninterruptible = max(0, self._uninterruptible - 1)

    # -- publishing ------------------------------------------------------

    def request(
        self,
        reason: str = STOP_REASON_INTERRUPT,
        text: str = "",
        call_id: str = "",
    ) -> StopRequest:
        """Publish (or extend) a stop request and return the current one.

        Re-requesting while a request is already outstanding does NOT create a
        second request: the extra text is appended to the pending one. A user
        who pastes two interjections in quick succession must get one dialog
        carrying both messages, not two nested interrupts.
        """
        with self._lock:
            if self._request is None:
                self._request = StopRequest(reason=reason)
                # A fresh request has not been delivered to anyone yet; the
                # flag from a request the dialog already consumed must not
                # suppress this one's interrupt — nor may a handoff armed for
                # that older request be spent on this one.
                self._interrupt_delivered = False
                self._escalation_handoff = False
            if text:
                self._request.texts.append(text)
            if call_id:
                self._request.call_ids.append(call_id)
            self._event.set()
            return self._request

    def clear(self) -> None:
        """Drop any outstanding request (called once the dialog has consumed it)."""
        with self._lock:
            self._request = None
            self._interrupt_delivered = False
            self._escalation_handoff = False
            self._event.clear()

    # -- interrupt delivery ----------------------------------------------

    @property
    def interrupt_delivered(self) -> bool:
        with self._lock:
            return self._interrupt_delivered

    def note_interrupt_delivered(self) -> None:
        """Record that the pending request was raised into the main thread.

        For channels that deliver unconditionally and only need to record the
        fact afterwards. Channels that must be sure they are the ONLY one
        delivering use :meth:`claim_interrupt_delivery` instead.
        """
        with self._lock:
            self._interrupt_delivered = True

    def claim_interrupt_delivery(self) -> bool:
        """Atomically claim the right to deliver the pending request's interrupt.

        Returns ``True`` to exactly one caller per published request; every
        later call returns ``False`` until the request is cleared (``clear`` /
        ``take``) or a fresh one is published. All main-thread delivery
        channels — the SIGINT handler, the watcher's ``interrupt_main`` (via
        :meth:`claim_escalation_delivery`), and
        :meth:`maybe_raise_deferred_interrupt` — route through this one flag so
        a request whose publication raced two channels interrupts the operator
        exactly once.
        """
        with self._lock:
            if self._interrupt_delivered:
                return False
            self._interrupt_delivered = True
            return True

    def claim_escalation_delivery(self) -> bool:
        """Claim the delivery on behalf of a channel that only *asks* to raise.

        WHY: ``_thread.interrupt_main`` does not raise — on every supported
        Python it merely trips SIGINT, so the exception is produced later, by
        the installed handler, on the main thread. A channel delivering that
        way must still win the single-delivery claim against the other channels
        (nothing else stops two of them from interrupting the operator twice),
        but the claim alone would then silence the very handler that has to do
        the raising. So the claim is taken AND a handoff is armed for it:
        :meth:`consume_escalation_handoff` lets exactly one subsequent raise
        site recognise the claim as its own.
        """
        with self._lock:
            if self._interrupt_delivered:
                return False
            self._interrupt_delivered = True
            self._escalation_handoff = True
            return True

    def consume_escalation_handoff(self) -> bool:
        """Take the armed handoff, if any: "the claim on file is mine to raise".

        Returns ``True`` to exactly one caller per armed handoff. Callers must
        only consume it when they are about to raise — consuming it and then
        standing down would strand the request undeliverable.
        """
        with self._lock:
            if not self._escalation_handoff:
                return False
            self._escalation_handoff = False
            return True

    def maybe_raise_deferred_interrupt(self) -> None:
        """Raise the interrupt a stop request deferred while a section ran.

        Called by :func:`uninterruptible_scope` on normal exit: the
        multi-command sequence has completed, so the KeyboardInterrupt the
        SIGINT handler held back can land safely now — this is the "next
        breakpoint" the deferral was waiting for. Main thread only: a worker
        thread must never raise the process-wide interrupt, and a pending
        request already delivered (or being wound down by a runner) must not
        be raised twice.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        with self._lock:
            # Eligibility and the delivery claim live in ONE lock acquisition
            # (claim_interrupt_delivery re-enters this RLock): the watcher may
            # be retrying a deferred escalation for this same request right
            # now, and only one of the two may deliver it.
            #
            # An armed handoff makes this the raise site too: the escalation
            # claimed the delivery expecting the SIGINT handler to raise, but
            # the handler stands down inside a non-interruptible section, and
            # this exit IS that section's end. Without this the raise the
            # escalation claimed would never happen.
            claimed = (
                self._event.is_set()
                and self._llm_calls == 0
                and self._uninterruptible == 0
                and (
                    self.claim_interrupt_delivery()
                    or self.consume_escalation_handoff()
                )
            )
        if claimed:
            raise KeyboardInterrupt

    # -- consuming -------------------------------------------------------

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def pending(self) -> Optional[StopRequest]:
        with self._lock:
            return self._request

    def take(self) -> Optional[StopRequest]:
        """Atomically return the pending request and clear the signal."""
        with self._lock:
            request = self._request
            self._request = None
            self._interrupt_delivered = False
            self._escalation_handoff = False
            self._event.clear()
            return request

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)


_STOP_SIGNAL = StopSignal()


def get_stop_signal() -> StopSignal:
    """Return the process-wide stop signal singleton."""
    return _STOP_SIGNAL


@contextmanager
def llm_call_scope(signal: Optional[StopSignal] = None):
    """Mark the enclosed block as an in-flight LLM subprocess call.

    Every runner wraps its supervised child in this so ``luo run``'s SIGINT
    handler knows a graceful stop is possible and must not raise instead.
    """
    sig = signal or get_stop_signal()
    sig.enter_llm_call()
    try:
        yield sig
    finally:
        sig.exit_llm_call()


@contextmanager
def uninterruptible_scope(signal: Optional[StopSignal] = None):
    """Mark the enclosed block as one the watcher may not interrupt.

    For multi-command sequences that leave a recoverable-but-broken on-disk
    state if torn apart mid-way — a leaf-branch merge back, a stash push/pop
    pair, a worktree checkout. The stop request is still published and still
    honoured; only the KeyboardInterrupt escalation waits for the block to end.

    On normal exit the scope itself raises a deferred interrupt the SIGINT
    handler held back (via :meth:`StopSignal.maybe_raise_deferred_interrupt`):
    the watcher alone cannot cover that case — its deferral retry is driven by
    its own drain, which a bare Ctrl-C never feeds. When the body raised, the
    section did NOT complete, so no deferred interrupt is raised on top of the
    real error.
    """
    sig = signal or get_stop_signal()
    sig.enter_uninterruptible()
    try:
        yield sig
    finally:
        sig.exit_uninterruptible()
    sig.maybe_raise_deferred_interrupt()


class InterjectionWatcher:
    """Background poller turning web interjection call files into stop requests.

    Runs for the lifetime of one LLM subprocess call. Ctrl-C and a web
    interjection must be the *same* path (decision 5), and the only difference
    between them upstream is who publishes the signal — the SIGINT handler or
    this poller — so everything downstream of :class:`StopSignal` is shared.

    The call file is *consumed* (drained) at detection time rather than left on
    disk: the dialog carries the text forward as its first user message, so a
    surviving file would be replayed a second time by the next drain.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        poll_interval: float = 1.0,
        signal: Optional[StopSignal] = None,
        drain_fn: Optional[Callable[[Any], List[Dict[str, Any]]]] = None,
        escalate_to_main: bool = False,
        interrupt_main: Optional[Callable[[], None]] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._poll_interval = poll_interval
        self._signal = signal or get_stop_signal()
        self._drain_fn = drain_fn
        # Opt-in: escalation raises in the MAIN thread, so only a caller that
        # owns that thread's control flow (the run loop around one step) may ask
        # for it. A bare poller — a test, a probe — must never be able to throw
        # into whatever its process happens to be doing.
        self._escalate_to_main = escalate_to_main
        # INVARIANT: escalation is only valid when the thread that owns the
        # work being interrupted IS the main thread — the only thread Python
        # can raise into from outside. ``luo run``'s step loop is that thread;
        # a watcher constructed anywhere else would throw a KeyboardInterrupt
        # into an unrelated control flow instead (a pytest-xdist worker, whose
        # test body runs off the main thread, dies outright that way).
        self._owner_is_main = (
            threading.current_thread() is threading.main_thread()
        )
        self._interrupt_main = interrupt_main
        self._escalated = False
        # Set when a stop was published but escalation had to wait (an LLM call
        # was in flight, or the main thread was inside a non-interruptible
        # section). Retried on every later tick — a deferral must postpone the
        # interrupt, never cancel it.
        self._escalation_pending = False
        # Serialises "decide to escalate" against "stop watching", so no
        # KeyboardInterrupt can be issued after stop() has returned — the caller
        # is then outside the region that catches it.
        self._escalate_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _drain(self) -> List[Dict[str, Any]]:
        drain_fn = self._drain_fn
        if drain_fn is None:
            from .engine.interaction_calls import drain_interjection_requests

            drain_fn = drain_interjection_requests
        return drain_fn(self._project_root) or []

    def poll_once(self) -> bool:
        """Drain pending interjections; publish a stop request if any arrived."""
        try:
            drained = self._drain()
        except Exception:  # pragma: no cover - defensive; never break the call
            logger.exception("Interjection watcher drain failed")
            return False
        if not drained:
            return False
        for item in drained:
            self._signal.request(
                reason=STOP_REASON_INTERJECTION,
                text=str(item.get("text") or ""),
                call_id=str(item.get("call_id") or ""),
            )
        self._escalation_pending = True
        self._escalate_if_no_llm_call()
        return True

    def _escalate_if_no_llm_call(self) -> None:
        """Raise in the main thread when the flag alone would not be noticed.

        WHY: the cooperative flag is polled by the LLM runners' monitor loops
        and nothing else. A step doing its work in Python — TEST above all —
        never looks at it, so a web interjection arriving there would sit
        unnoticed until the step finished on its own; the dialog would then open
        against work already done, and a confirmed ``continue`` would rerun a
        whole test suite the user had asked to interrupt. Ctrl-C at that same
        moment stops it at once, and decision 5 makes the two entry points ONE
        path — so when no runner is supervising a child, this delivers the same
        ``KeyboardInterrupt`` Ctrl-C would, literally through the same SIGINT
        handler (the default ``interrupt_main`` only trips the signal; see
        :meth:`StopSignal.claim_escalation_delivery` for why the claim has to
        travel with a handoff).

        Escalates once per watcher: the request is already published, and a
        second exception could land in the caller's own cleanup.
        """
        if not self._escalate_to_main:
            self._escalation_pending = False
            return
        # The owner check applies to the DEFAULT delivery only: a caller that
        # supplies its own callback owns where the interrupt lands.
        if self._interrupt_main is None and not self._owner_is_main:
            logger.debug(
                "Interjection watcher not owned by the main thread; the stop "
                "request stays cooperative"
            )
            self._escalation_pending = False
            return
        # A non-interruptible section DEFERS: the flag stays pending and a later
        # tick escalates. Raising into a half-finished git merge would leave a
        # MERGE_HEAD behind, and the step the operator asked to interrupt would
        # then refuse to re-run at all — the opposite of what they asked for.
        if self._signal.uninterruptible:
            logger.debug(
                "Interjection watcher deferring escalation: the main thread is "
                "inside a non-interruptible section"
            )
            return
        with self._escalate_lock:
            if self._escalated or self._stop.is_set():
                self._escalation_pending = False
                return
            if self._signal.llm_active:
                self._escalation_pending = False
                return
            # The SIGINT handler or an uninterruptible_scope exit may be
            # delivering this same request right now; the atomic claim decides
            # the single winner, so a raced publication can never produce a
            # second KeyboardInterrupt behind the first. It is the ESCALATION
            # claim because this channel does not raise by itself: the default
            # delivery trips SIGINT and the main thread's handler raises, so
            # the claim has to travel with a handoff that authorises that
            # handler to go through with the raise instead of standing down.
            if not self._signal.claim_escalation_delivery():
                self._escalation_pending = False
                return
            self._escalated = True
            self._escalation_pending = False
        interrupt = self._interrupt_main
        if interrupt is None:
            interrupt = _thread.interrupt_main
        try:
            interrupt()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Failed to interrupt the main thread", exc_info=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            # A deferred escalation is retried here rather than only at the next
            # drain: the interjection that triggered it has already been
            # consumed off disk, so no future drain would ever re-raise it.
            if self._escalation_pending:
                self._escalate_if_no_llm_call()
            self._stop.wait(self._poll_interval)

    def start(self) -> "InterjectionWatcher":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._escalated = False
        self._escalation_pending = False
        self._thread = threading.Thread(
            target=self._loop, name="interjection-watcher", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._escalate_lock:
            self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def __enter__(self) -> "InterjectionWatcher":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()
