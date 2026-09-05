"""Abstract base class for agent runners.

Defines the unified interface that all agent runners must implement.
Currently only ClaudeCodeRunner (claude_runner.py) implements this,
but the abstraction allows future runner types (API-based, other CLIs).
"""

from __future__ import annotations

import json
import logging
import os
import signal as _signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class InfraErrorType(Enum):
    """Types of infrastructure errors that warrant agent rotation."""

    NONE = "none"
    USAGE_LIMIT = "usage_limit"
    TIMEOUT = "timeout"
    HANG = "hang"
    STARTUP_FAILURE = "startup_failure"


class AgentInvocationIntent(str, Enum):
    """Vendor-neutral intent for one runner invocation.

    The outer flow owns implementation strategy and recovery.  The intent is
    informational today: every runner executes the prompt through its normal
    autonomous interface regardless of the value.  It stays in the contract as
    the seam through which a future runner could translate an intent into a
    native feature of its own CLI — any such adapter must degrade to the
    ordinary command when the native feature cannot carry the call (Claude
    Code's /goal was retired here precisely because its goal-condition
    argument caps at 4000 characters, below any real implement prompt).
    """

    DEFAULT = "default"
    DIRECT_IMPLEMENTATION = "direct_implementation"


@dataclass
class RunResult:
    """Result from an agent runner execution.

    Attributes:
        returncode: Process exit code.
        stdout: Standard output content.
        stderr: Standard error content.
        infra_error_type: Type of infrastructure error detected, if any.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""
    infra_error_type: InfraErrorType = InfraErrorType.NONE


@dataclass(frozen=True)
class RunnerStartupMetadata:
    """Provider identity a runner can verify before stream metadata arrives."""

    provider: Optional[str] = None
    model: Optional[str] = None
    provider_session_id: Optional[str] = None


def is_message_boundary(line: str) -> bool:
    """Report whether one stream-json NDJSON line closes a semantic message.

    A graceful stop waits for the next boundary before signalling the child so
    the provider transcript is left at a point that can be resumed: a settled
    ``tool_result`` (the tool ran and its output is recorded) or an assistant
    turn that issued no ``tool_use`` (nothing is left dangling). The terminal
    ``result`` line also counts — the turn is over.

    WHY this is not simply "any line": interrupting between a ``tool_use`` and
    its ``tool_result`` leaves the transcript with a dangling call, which is the
    one shape a provider can refuse to resume. Codex's converter synthesizes
    interrupted tool_results for that case; Claude Code writes its own
    ``[Request interrupted by user]`` turn. Waiting for the boundary means
    neither recovery path is usually needed.

    Both runners emit Claude stream-json (the codex adapter converts), so a
    single predicate serves every runner. Unparseable input is never a
    boundary — an unknown line must not shorten the wait.
    """
    if not line:
        return False
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict):
        return False
    msg_type = obj.get("type")
    if msg_type == "result":
        return True
    if msg_type == "tool_result":
        return True
    content = obj.get("message", {})
    content = content.get("content", []) if isinstance(content, dict) else []
    if not isinstance(content, list):
        content = []
    if msg_type == "user":
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result"
            for item in content
        )
    if msg_type == "assistant":
        return not any(
            isinstance(item, dict) and item.get("type") == "tool_use"
            for item in content
        )
    return False


#: Tools through which a "read-only" Claude call could still mutate the
#: workspace by going around the file-editing tools: a shell, and the
#: delegation tools whose subagent would start without this lock. Both
#: delegation names are listed because the CLI renamed the tool (legacy
#: ``Task`` -> current ``Agent``) and a deny list that names only one of them
#: leaves a live escape hatch on whichever CLI build uses the other. Denied
#: only where a call asks for the strict posture (``deny_shell``) — the
#: ordinary read-only steps (review, self-check, analyze) legitimately run
#: ``git diff`` and friends through Bash, while the interruption dialog must
#: not be able to touch the tree at all.
READ_ONLY_SHELL_TOOLS = ["Bash", "BashOutput", "KillShell", "Task", "Agent"]


#: Extra grace given to grandchildren after the direct CLI has exited, before
#: the surviving process group is SIGKILL-ed. They already received the same
#: SIGINT as the CLI, so this only covers a tool that is finishing its own
#: teardown a moment later.
GROUP_DRAIN_SECONDS = 5.0


def _own_process_group() -> Optional[int]:
    """This process's own group id, or ``None`` where the OS has no groups."""
    getpgrp = getattr(os, "getpgrp", None)
    if getpgrp is None:
        return None
    try:
        return getpgrp()
    except OSError:  # pragma: no cover - defensive
        return None


def is_signalable_process_group(pgid: Any) -> bool:
    """Whether *pgid* may be signalled as one of our spawned children's groups.

    INVARIANT: no signal-delivering path in this module may target pgid <= 1 or
    ``luo run``'s own group. ``killpg(1, sig)`` is turned by the C library into
    ``kill(-1, sig)`` — every process the user owns — and ``killpg(0, sig)`` /
    our own group signals ``luo run`` and its siblings. This is not theoretical:
    a test double whose ``pid`` coerced to 1 resolved init's group and the
    reclaim path took the whole development machine down three times. The guard
    lives in production code rather than only in the test fixture because a real
    process-double, a wrapped Popen, or a child reaped between resolve and
    signal can put the same value on this path at runtime.
    """
    if pgid is None or isinstance(pgid, bool) or not isinstance(pgid, int):
        return False
    if pgid <= 1:
        return False
    return pgid != _own_process_group()


def _child_pid(proc: Any) -> Optional[int]:
    """A real child pid (> 1) from *proc*, or ``None``.

    Non-integer pids — the ``MagicMock`` attribute of a process double, most of
    all — are rejected outright rather than coerced: coercion is exactly how a
    fake process previously resolved to init's group.
    """
    pid = getattr(proc, "pid", None)
    if pid is None or isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid if pid > 1 else None


def resolve_process_group(proc: Any) -> Optional[int]:
    """Resolve the child's pgid *while its pid is still live*.

    WHY callers capture this eagerly: ``os.getpgid`` needs the pid, and the pid
    disappears the moment the direct child is reaped. Every cleanup path that
    must still reach surviving grandchildren therefore has to have taken the
    group identity before the reap, not after.

    Only a real child pid resolves a group, and only a group that passes
    :func:`is_signalable_process_group` is returned — see that predicate for why
    an unguarded resolve is a machine-wide hazard.
    """
    pid = _child_pid(proc)
    if pid is None or not hasattr(os, "getpgid"):
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if not is_signalable_process_group(pgid):
        logger.warning(
            "Refusing to treat pgid %s (from pid %s) as a child process group",
            pgid,
            pid,
        )
        return None
    return pgid


def process_group_alive(pgid: Optional[int]) -> bool:
    """Report whether any process still lives in *pgid*.

    WHY this is not a pid-reuse hazard on the paths that use it: a process
    group id stays allocated for as long as the group has members, so a
    successful probe after the group leader was reaped means our own
    grandchildren are what is keeping it alive.
    """
    if not is_signalable_process_group(pgid) or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - group exists, not ours to signal
        return True
    except OSError:  # pragma: no cover - defensive
        return False


def signal_process_group(
    proc: Any, sig: int, *, pgid: Optional[int] = None
) -> bool:
    """Send *sig* to the child's whole process group; report whether it landed.

    WHY the group and not the pid: every CLI runner spawns tool/bash
    grandchildren, and signalling only the direct child orphans them. The
    runners launch with ``start_new_session=True`` precisely so the group id
    equals the child's pid and this call cannot reach back into ``luo run``'s
    own group.

    *pgid* may be passed by callers that captured the group identity before the
    direct child was reaped, which is the only way to reach the group once the
    pid is gone.
    """
    target = pgid if pgid is not None else resolve_process_group(proc)
    if not is_signalable_process_group(target) or not hasattr(os, "killpg"):
        if target is not None:
            logger.warning(
                "Refusing to signal process group %s: not a child group", target
            )
        return False
    try:
        os.killpg(target, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def ensure_process_group_reclaimed(
    pgid: Optional[int],
    *,
    grace: float = GROUP_DRAIN_SECONDS,
    poll_interval: float = 0.25,
) -> None:
    """Wait out *grace*, then SIGKILL whatever still lives in *pgid*.

    WHY this exists separately from the direct child's own termination: the CLI
    exiting says nothing about the tools it spawned. A Bash grandchild that
    ignored (or never received) the wind-down signal keeps running — and keeps
    editing the workspace — after ``proc.wait()`` has returned. Ownership of the
    group therefore outlives ownership of the child, and every termination path
    ends here.
    """
    if not process_group_alive(pgid):
        return
    deadline = time.time() + max(grace, 0.0)
    while time.time() < deadline:
        if not process_group_alive(pgid):
            return
        time.sleep(poll_interval)
    if not process_group_alive(pgid):
        return
    logger.warning(
        "Process group %s still has members after the child exited; SIGKILL",
        pgid,
    )
    if not signal_process_group(None, _signal.SIGKILL, pgid=pgid):
        return
    # A bounded confirmation wait: SIGKILL is not refusable, but the reaping is
    # asynchronous and the caller may want the workspace quiescent.
    confirm_deadline = time.time() + 2.0
    while time.time() < confirm_deadline and process_group_alive(pgid):
        time.sleep(poll_interval)


def reclaim_process_group(
    proc: Any, *, wait: float = 5.0, pgid: Optional[int] = None
) -> None:
    """SIGKILL the child's whole process group, then reap the direct child.

    WHY every forced-termination path must go through this rather than
    ``proc.kill()``: the runners launch with ``start_new_session=True``, so the
    CLI's Bash/tool grandchildren live in ITS group, not ours. Killing only the
    direct child leaves them running — still editing the workspace — and, worse,
    reaping the CLI makes ``proc.poll()`` report "finished", so the caller's
    cleanup then skips group reclamation entirely and the grandchildren are
    orphaned for good. The group is therefore signalled BEFORE the child is
    reaped, while its pgid is still resolvable from the live pid, and the group
    is re-checked afterwards so a survivor cannot slip through.
    """
    if proc is None:
        return
    target = pgid if pgid is not None else resolve_process_group(proc)
    if not signal_process_group(proc, _signal.SIGKILL, pgid=target):
        try:
            proc.kill()
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        proc.wait(timeout=wait)
    except Exception:  # pragma: no cover - defensive
        pass
    ensure_process_group_reclaimed(target, grace=0.0)


def drain_available_output(
    stream: Any,
    consume: Callable[[str], None],
    *,
    budget: float = 0.5,
) -> None:
    """Consume whatever the child has already written, without waiting for more.

    WHY this must run while a stopping child is being waited on: the CLI's
    wind-down (a large ``tool_result``, the final ``result`` line) can easily
    exceed the 64 KiB pipe buffer. A parent that only polls ``proc.poll()``
    leaves the child blocked in ``write()`` forever, so the SIGINT it was given
    can never complete and it is SIGKILL-ed with its turn half-written — the
    exact shape that makes the provider session unresumable.

    Uses the same ``select``-then-``readline`` pairing as the runners' monitor
    loops rather than raw non-blocking reads: the caller's stream is a buffered
    text reader that the monitor loop is also reading, and bypassing its buffer
    would lose whatever it had already read ahead.
    """
    if stream is None:
        return
    try:
        import select as _select
    except Exception:  # pragma: no cover - select always exists on POSIX
        return
    deadline = time.time() + max(budget, 0.0)
    while True:
        try:
            ready, _, _ = _select.select([stream], [], [], 0)
        except Exception:
            return
        if not ready:
            return
        try:
            line = stream.readline()
        except Exception:
            return
        if not line:
            return
        try:
            consume(line)
        except Exception:  # pragma: no cover - defensive
            pass
        if time.time() >= deadline:
            return


def graceful_stop_process(
    proc: Any,
    *,
    exit_wait: float = 30.0,
    poll_interval: float = 0.25,
    pgid: Optional[int] = None,
    drain: Optional[Callable[[], None]] = None,
) -> None:
    """SIGINT the child's process group, then escalate to SIGKILL on timeout.

    The CLI agents treat SIGINT as "wind down the current turn" — Claude Code
    records a ``[Request interrupted by user]`` turn and exits 0, codex closes
    its thread — which is what makes the provider session resumable afterwards.
    Only a child that ignores that within *exit_wait* is killed outright, and
    the kill also targets the group so no grandchild survives the escalation.

    WHY the direct child exiting is not the end of the story: the CLI can wind
    itself down cleanly while a tool or shell grandchild ignores the same
    SIGINT. Returning there would leave that grandchild alive and still writing
    to the workspace, so the group is always drained before this returns.

    *drain* is invoked on every poll of the exit wait. WHY it is not optional
    in practice: a child winding down writes its remaining tool output and the
    terminal result line, which overruns the pipe buffer and blocks it in
    ``write()`` unless the parent keeps reading — a deadlock that would be
    resolved only by the SIGKILL escalation, losing both the tail of the output
    and the session's resumability.
    """
    if proc is None:
        return
    # Captured up front: once the direct child is reaped its pid is gone and
    # the group can no longer be located from it.
    group = pgid if pgid is not None else resolve_process_group(proc)
    if proc.poll() is None:
        if not signal_process_group(proc, _signal.SIGINT, pgid=group):
            # No usable group (a child not launched in its own session, or one
            # whose group was refused as unsafe): the direct child still has to
            # be asked to wind down, and it is the only thing we may address.
            try:
                proc.send_signal(_signal.SIGINT)
            except Exception:  # pragma: no cover - defensive
                pass
        deadline = time.time() + exit_wait
        while time.time() < deadline and proc.poll() is None:
            if drain is not None:
                drain()
            time.sleep(poll_interval)
        if drain is not None:
            drain()
        if proc.poll() is None:
            logger.warning(
                "Child process group did not exit %.0fs after SIGINT; "
                "escalating to SIGKILL",
                exit_wait,
            )
            if not signal_process_group(proc, _signal.SIGKILL, pgid=group):
                try:
                    proc.kill()
                except Exception:  # pragma: no cover - defensive
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:  # pragma: no cover - defensive
                pass
    ensure_process_group_reclaimed(group, poll_interval=poll_interval)


class AgentRunner(ABC):
    """Abstract base class for all agent runners.

    Defines the contract that LLMCaller uses to interact with agent
    implementations. Each runner wraps a specific agent type (e.g.,
    Claude Code CLI, API-based agents).
    """

    startup_provider: Optional[str] = None
    startup_model: Optional[str] = None

    #: Whether this runner's CLI can continue a previously recorded provider
    #: session in place (Claude Code's ``--resume``, codex's ``exec resume``).
    #: Declared per runner and *verified against the installed CLI* — a runner
    #: whose resume path was measured not to work declares ``False`` rather
    #: than leaving a broken code path reachable. LLMCaller reads this to
    #: decide between native resume and history rebuild; it is never a hint
    #: the runner acts on itself (rotation and strategy stay with LLMCaller,
    #: per the charter's execution-stack layering).
    supports_native_resume: bool = False

    def build_resume_call_args(
        self,
        session_id: str,
        prompt: str,
        read_only: bool,
        context_files: Optional[List[Path]] = None,
        deny_shell: bool = False,
    ) -> List[str]:
        """Translate a *resume* intent into this runner's CLI arguments.

        Same layer as :meth:`build_call_args` — intent in, argv out — but for
        the case where the conversation already exists provider-side. The
        caller supplies the recorded ``session_id`` and only the text to append
        as a new user turn; the runner never reconstructs context, because the
        provider still holds it.

        The default implementation refuses, so a runner that has not declared
        (and verified) :attr:`supports_native_resume` cannot be driven down a
        path its CLI does not implement.

        Args:
            session_id: Provider session/thread id recorded for the attempt
                being continued.
            prompt: The new user turn to append (a continuation directive or a
                dialog instruction) — never a rebuilt transcript.
            read_only: Whether this invocation must hold the tool-level
                read-only lock.
            context_files: Optional files to include as context.
            deny_shell: Whether that lock must also close the shell (and
                subagent delegation) — the strict posture the interruption
                dialog needs, where "read-only" has to mean the workspace
                cannot be touched at all, not merely that the edit tools are
                denied. Runners whose read-only posture is a sandbox already
                satisfy it and may ignore the flag.

        Returns:
            CLI argument list, in the same shape :meth:`build_call_args`
            returns (excluding the command name and the runner's own prefix
            flags).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement native session resume"
        )

    def get_startup_metadata(
        self, env: Optional[Dict[str, str]] = None
    ) -> RunnerStartupMetadata:
        """Return verified launch metadata without guessing from command names."""
        return RunnerStartupMetadata(
            provider=self.startup_provider,
            model=self.startup_model,
        )

    @abstractmethod
    def run(
        self,
        args: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_retry: Optional[Callable[[int, str], Optional[List[str]]]] = None,
    ) -> subprocess.CompletedProcess:
        """Run the agent synchronously.

        Args:
            args: Arguments to pass to the agent.
            timeout: Timeout in seconds.
            cwd: Working directory.
            env: Environment variables.
            on_retry: Optional callback for retry notification.

        Returns:
            subprocess.CompletedProcess with execution results.
        """
        ...

    @abstractmethod
    def run_with_monitor(
        self,
        args: List[str],
        log_file: Optional[Path] = None,
        wall_timeout: Optional[int] = None,
        inactivity_timeout: int = 1800,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_activity: Optional[Callable[[], None]] = None,
    ) -> Any:
        """Run the agent with activity monitoring.

        Args:
            args: Arguments to pass to the agent.
            log_file: Optional path to write output log.
            wall_timeout: Maximum total runtime in seconds.
            inactivity_timeout: Seconds without output before considering stuck.
            cwd: Working directory.
            env: Environment variables.
            on_output: Callback for each line of output.
            on_activity: Callback for activity detection.

        Returns:
            MonitoredResult (or compatible result type).
        """
        ...

    @abstractmethod
    def build_call_args(
        self,
        prompt: str,
        read_only: bool,
        context_files: Optional[List[Path]] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
        deny_shell: bool = False,
    ) -> List[str]:
        """Build CLI arguments from intent-level parameters.

        Translates the caller's *intent* (the effective prompt, whether the
        step is read-only, and any context files) into the concrete CLI
        argument list for this runner's agent.  Each runner subclass owns the
        mapping because different agents use different flags for the same
        semantics (e.g. ``--output-format stream-json`` for Claude Code vs.
        ``--json`` for Codex).

        Args:
            prompt: The effective prompt text (already includes retry context,
                extra-prompt injection, and read-only constraints).
            read_only: Whether the current step is read-only.  Runners
                translate this into agent-specific tool-restriction flags.
            context_files: Optional list of files to include as context.
                Runners translate this into agent-specific file-inclusion
                flags (or inline the content when no flag exists).
            invocation_intent: Semantic purpose of this invocation. Kept as
                information only: no current runner translates it into a
                native feature (Claude Code's /goal was retired — its
                goal-condition argument is capped far below real implement
                prompt sizes), so every runner executes the prompt through
                its normal autonomous interface.
            deny_shell: Whether the read-only lock must also close the shell
                (and subagent delegation). Separate from *read_only* because
                the ordinary read-only steps inspect the tree through the
                shell; only a call that must not touch the workspace at all —
                the interruption dialog — asks for it. Runners whose read-only
                posture is a sandbox already satisfy it and may ignore the flag.

        Returns:
            A list of CLI arguments to pass *after* the runner's base
            command and permission flags.  The prompt is typically embedded
            as a ``-p`` / positional argument; context files are appended
            as ``--file`` pairs or equivalent.
        """
        ...

    @abstractmethod
    def detect_infra_error(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> InfraErrorType:
        """Detect if the result indicates an infrastructure error.

        Infrastructure errors (usage limits, timeouts, hangs) warrant
        rotating to a different agent. Task-level failures do not.

        Args:
            returncode: Process exit code.
            stdout: Standard output content.
            stderr: Standard error content.

        Returns:
            The type of infrastructure error, or InfraErrorType.NONE.
        """
        ...
