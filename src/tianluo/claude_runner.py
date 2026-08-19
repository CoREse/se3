"""Claude Code CLI adapter (single-command runner).

Provides a unified way to invoke Claude Code CLI from SE3 modules.
Each ClaudeCodeRunner instance wraps a single agent command.
Agent selection/rotation is handled by LLMCaller.

Historical note: This module was originally ``ClaudeRunner`` with built-in
command list traversal.  The traversal logic has been moved to LLMCaller;
this runner now only executes a single command.  The ``ClaudeRunner`` alias
is kept for backward compatibility.
"""

import collections
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .agent_runner import AgentInvocationIntent, AgentRunner, InfraErrorType
from .config import load_claude_commands, load_claude_subprocess_config

# Platform-specific imports for process resource monitoring
try:
    if sys.platform.startswith('linux'):
        import psutil
    else:
        psutil = None
except ImportError:
    psutil = None

# Keywords indicating usage/rate limit in Claude CLI output
# Threshold for auto-filing prompt arguments to avoid execve() E2BIG.
# Linux MAX_ARG_STRLEN is 128 KB; 100 KB leaves ~28 KB safety margin.
_MAX_ARG_BYTES = 102400

def _spawn_stdin_writer(proc: subprocess.Popen, payload: str) -> threading.Thread:
    """Write ``payload`` to ``proc.stdin`` in a daemon thread and close it.

    Used for the large-prompt path where the prompt is piped to Claude Code
    via stdin. Writing inline would deadlock once the pipe buffer (typically
    64KB) fills and the child has not drained enough to make room — the
    child can't drain aggressively because it's waiting for EOF on stdin
    before starting its work. Doing the write from a background thread lets
    the main thread continue reading stdout in parallel.

    The stream is closed after the payload is flushed so Claude sees EOF
    and proceeds. Failures are swallowed; they'll surface as a subprocess
    error via stdout/returncode.
    """
    def _writer() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_writer, name="claude-stdin-writer", daemon=True)
    t.start()
    return t


# Maximum number of stderr lines retained in the bounded tail buffer.  The
# runner exposes this tail on its result so callers (e.g. infra-error
# detection) can see CLI-level reports that never reach the NDJSON stdout
# stream.
_STDERR_BUFFER_MAXLEN = 200


def _spawn_stderr_reader(
    proc: subprocess.Popen,
    log_file: Optional[Path] = None,
    stderr_buffer: Optional[Deque[str]] = None,
) -> threading.Thread:
    """Drain ``proc.stderr`` in a daemon thread so the pipe never fills.

    The child process's stderr is kept separate from stdout so that NDJSON
    on stdout stays clean.  Stderr lines are written to ``log_file`` when
    provided, captured into ``stderr_buffer`` when provided, and also echoed
    to the parent's stderr for live visibility.  Failures are swallowed;
    they'll surface via the child's returncode.
    """
    def _reader() -> None:
        log_fh = None
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(log_file, "a", encoding="utf-8")
            except Exception:
                pass
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                if stderr_buffer is not None:
                    stderr_buffer.append(line)
                if log_fh is not None:
                    try:
                        log_fh.write(line)
                        log_fh.flush()
                    except Exception:
                        pass
                try:
                    print(line.rstrip("\n"), file=sys.stderr)
                except Exception:
                    pass
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            if log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass

    t = threading.Thread(target=_reader, name="claude-stderr-reader", daemon=True)
    t.start()
    return t


USAGE_LIMIT_KEYWORDS = [
    "usage limit",
    "rate limit",
    "too many requests",
    "rate_limit",
    "overloaded",
    "capacity",
    "hit your limit",
    "you've hit your limit",
]

# --- CLI-subprocess confirmation-prompt capture -------------------------------
#
# A child Claude process may, at the CLI/PTY layer, print an interactive
# confirmation prompt (e.g. ``按 1 确定`` or ``Press 1 to confirm``) and then
# block waiting for a keystroke on stdin.  ``run_with_monitor`` can be given an
# ``on_confirm`` callback so such prompts are surfaced to the engine and the
# answer routed back to the child's stdin.
#
# The pattern set below is intentionally *conservative*: a line is treated as a
# confirmation prompt only when it strongly resembles one.  Ordinary NDJSON
# stream output and prose never match, so unrecognized lines are an exact
# no-op and existing stdout parsing / streaming rendering is unaffected.

_CONFIRM_PATTERNS = [
    # Chinese: 按 1 确定 / 按 1 继续 / 输入 1 确认
    re.compile(r"按\s*\d.*?(确定|确认|继续|是)"),
    re.compile(r"(?:请)?输入\s*\d.*?(确定|确认|继续)"),
    # English: "Press 1 to confirm" / "press [Enter] to continue"
    re.compile(r"\bpress\s+\S+\s+to\s+(?:confirm|continue|proceed)\b", re.IGNORECASE),
    # English yes/no bracket prompts: [y/N] (y/n) [Y/n] [n/y]
    re.compile(r"[\[(]\s*y(?:es)?\s*/\s*no?\s*[\])]", re.IGNORECASE),
    re.compile(r"[\[(]\s*no?\s*/\s*y(?:es)?\s*[\])]", re.IGNORECASE),
    # Explicit confirm question: "Do you want to continue?"
    re.compile(r"\bdo you want to (?:continue|proceed)\b", re.IGNORECASE),
]

# Best-effort extraction of selectable option labels from "1) foo  2) bar"
# style lines.  Options may legitimately be empty for prompts that do not
# enumerate their choices inline.
_OPTION_PATTERN = re.compile(r"(?:^|[\s,，、(\[])(\d)\s*[\.)、:：]")


def detect_confirmation_prompt(line: str) -> Optional[Tuple[str, List[str]]]:
    """Detect a CLI-subprocess confirmation prompt in a line of child output.

    Returns ``(prompt_text, options)`` when ``line`` conservatively matches a
    known confirmation-prompt pattern, otherwise ``None``.  ``prompt_text`` is
    the stripped line; ``options`` is a best-effort list of the numeric labels
    found inline (possibly empty).  Structured NDJSON lines (starting with
    ``{`` or ``[``) and any non-matching prose yield ``None`` so callers treat
    them as ordinary output.
    """
    if not line:
        return None
    stripped = line.strip()
    if not stripped:
        return None
    # NDJSON stream lines are structured output, never interactive prompts.
    if stripped[0] in "{[":
        return None
    for pattern in _CONFIRM_PATTERNS:
        if pattern.search(stripped):
            options = [m.group(1) for m in _OPTION_PATTERN.finditer(stripped)]
            return stripped, options
    return None


def _write_stdin_response(proc: subprocess.Popen, text: str) -> None:
    """Write a confirmation response to the child's stdin.

    A trailing newline is appended when absent so the child reads a complete
    line.  Failures (closed pipe, already-exited child) are swallowed — a
    confirmation that cannot be delivered degrades to a no-op rather than
    crashing the monitor loop.
    """
    if proc.stdin is None:
        return
    try:
        payload = text if text.endswith("\n") else text + "\n"
        proc.stdin.write(payload)
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


class ClaudeCodeRunner(AgentRunner):
    """Pure Claude Code CLI adapter — executes a single command.

    This runner wraps one specific Claude CLI command (e.g. ``claude`` or
    ``kclaude``).  It does NOT traverse a list of commands or implement
    fallback logic; that responsibility belongs to :class:`LLMCaller`.

    For backward compatibility, the constructor also accepts the legacy
    ``commands`` parameter (list of dicts); in that case, only the first
    command is used.
    """

    startup_provider = "anthropic"

    def __init__(
        self,
        project_root: Optional[Path] = None,
        commands: Optional[List[Dict[str, Any]]] = None,
        command: Optional[Dict[str, Any]] = None,
        setting_sources: Optional[List[str]] = None,
    ):
        """Initialize with a single command.

        Args:
            project_root: Project root for loading config.
            commands: Legacy parameter — list of command dicts.  If provided
                and ``command`` is None, the first entry is used.
            command: Single command dict ``{cmd, priority}``.  Preferred.
            setting_sources: Optional explicit list of Claude CLI setting
                sources (subset of ``{"user", "project", "local"}``). When
                ``None``, the value is loaded from ``project_root``'s
                ``claude_subprocess.setting_sources`` config, falling back
                to ``["user"]`` if no project root is provided. The chosen
                list is injected into every spawned Claude subprocess via
                ``--setting-sources`` so SE3 workers are not locked by a
                downstream project's ``.claude/settings.json``
                ``permissions.deny`` rules.
        """
        if command is not None:
            self.command = command
        elif commands is not None:
            self.command = commands[0] if commands else {"cmd": "claude", "priority": 0}
        else:
            all_commands = load_claude_commands(project_root)
            self.command = all_commands[0] if all_commands else {"cmd": "claude", "priority": 0}

        # Keep a commands list view for backward compatibility (get_command, get_next_command helpers)
        if commands is not None:
            self.commands = commands
        else:
            self.commands = [self.command]

        if setting_sources is not None:
            self.setting_sources = list(setting_sources)
        elif project_root is not None:
            self.setting_sources = list(
                load_claude_subprocess_config(project_root).setting_sources
            )
        else:
            self.setting_sources = ["user"]
        self._setting_sources_arg = ",".join(self.setting_sources)

    @staticmethod
    def _resolve_args(
        args: List[str], cwd: Optional[Path] = None
    ) -> Tuple[List[str], Optional[str]]:
        """Resolve arguments, rewriting oversized `-p` values to stdin.

        Behavior:
        - Arguments starting with ``@`` and ``-p @file`` are left alone: that
          is Claude CLI's documented file-reference syntax and callers that
          use it have asked for that semantic explicitly.
        - ``-p <text>`` / ``--prompt <text>`` where ``<text>`` is too large
          to safely pass via argv (``> _MAX_ARG_BYTES``) has its value moved
          to stdin. The second element of the return tuple carries that
          stdin content; the returned argv keeps ``-p`` with no following
          value. This avoids the older ``-p @tmpfile`` fallback that caused
          Claude Code to *read the file as a referenced file* (subject to
          the Read tool's 25k-token ceiling) rather than treating it as the
          user message.

        Args:
            args: Original arguments list.
            cwd: Working directory for resolving relative ``@file`` paths.

        Returns:
            ``(resolved_args, stdin_prompt)``. ``stdin_prompt`` is ``None``
            unless a large ``-p`` value was rerouted to stdin.
        """
        resolved: List[str] = []
        stdin_prompt: Optional[str] = None
        i = 0

        while i < len(args):
            arg = args[i]

            if arg.startswith("@"):
                # Explicit file reference — pass through unchanged so Claude
                # Code can apply its own @file semantics.
                resolved.append(arg)
            elif arg in ["-p", "--prompt"] and (i + 1) < len(args):
                i += 1
                prompt_arg = args[i]

                if prompt_arg.startswith("@"):
                    # Explicit @file reference following -p — pass through.
                    resolved.append(arg)
                    resolved.append(prompt_arg)
                elif len(prompt_arg.encode("utf-8")) > _MAX_ARG_BYTES:
                    # Too large for argv. Keep the flag, drop the value, and
                    # route the prompt via stdin.
                    if stdin_prompt is not None:
                        # Multiple oversized -p values in one invocation is
                        # not a supported pattern; last one wins.
                        warnings.warn(
                            "Multiple oversized -p prompts in a single claude "
                            "invocation; only the last one is routed to stdin.",
                            stacklevel=2,
                        )
                    resolved.append(arg)
                    stdin_prompt = prompt_arg
                else:
                    resolved.append(arg)
                    resolved.append(prompt_arg)
            else:
                resolved.append(arg)

            i += 1

        return resolved, stdin_prompt

    def run(
        self,
        args: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_retry: Optional[Callable[[int, str], Optional[List[str]]]] = None,
    ) -> subprocess.CompletedProcess:
        """Run Claude synchronously (single command, no fallback).

        Args:
            args: Arguments to pass after the claude command.
            timeout: Timeout in seconds.
            cwd: Working directory.
            env: Environment variables.
            on_retry: Ignored (kept for interface compatibility).

        Returns:
            subprocess.CompletedProcess from the attempt.
        """
        cmd_name = self.command["cmd"]

        resolved_args, stdin_prompt = self._resolve_args(args, cwd)
        full_cmd = [
            cmd_name,
            "--dangerously-skip-permissions",
            "--setting-sources",
            self._setting_sources_arg,
        ] + resolved_args

        run_env = env
        if run_env is None:
            run_env = dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=run_env,
                input=stdin_prompt,
            )
            return result

        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=full_cmd, returncode=124, stdout="", stderr="timeout"
            )

    def get_command(self, index: int = 0) -> str:
        """Get the command string at the given index."""
        if index >= len(self.commands):
            return self.commands[-1]["cmd"]
        return self.commands[index]["cmd"]

    def get_next_command(self, current_cmd: str) -> Optional[str]:
        """Get the next command after the given one.

        Returns None if current_cmd is the last or not found.
        """
        for i, entry in enumerate(self.commands):
            if entry["cmd"] == current_cmd:
                if i + 1 < len(self.commands):
                    return self.commands[i + 1]["cmd"]
                return None
        return None

    def build_call_args(
        self,
        prompt: str,
        read_only: bool,
        context_files: Optional[List[Path]] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
    ) -> List[str]:
        """Build Claude Code CLI arguments from intent-level parameters.

        Produces the argv that :class:`LLMCaller` previously assembled inline,
        with one deliberate divergence from that historical form: a single
        ``--disallowedTools`` flag is now emitted on *every* call (see below).
        Flag identity and ordering are otherwise unchanged:

        * Base flags: ``--output-format stream-json --verbose -p <prompt>``
        * Tool denial — exactly one ``--disallowedTools`` flag, always
          present: ``Write Edit NotebookEdit AskUserQuestion ReportFindings``
          for read-only steps, ``ReportFindings`` otherwise. The write tools
          are the read-only enforcement; ``ReportFindings`` is denied
          unconditionally because it is a claude CLI *host-UI* tool (the one
          ``/code-review`` uses to hand findings to Claude Code's own
          interface), not a subagent — under headless ``claude -p`` nothing
          receives its output, so it only ever yields ``No findings
          reported.`` or an ``InputValidationError`` in history. The two lists
          MUST stay merged into one flag: the claude CLI resolves a repeated
          flag last-one-wins, so a second ``--disallowedTools`` would silently
          drop the read-only write-tool lock.
        * Context files: ``--file <path>`` for each existing file

        WHY (no ``--settings`` here): this runner never appends a ``--settings``
        flag of its own. The Claude CLI treats a duplicated ``--settings`` as
        last-wins, so a second occurrence would wholly override an agent
        wrapper's settings file — silently discarding the model it selects. The
        default ``--setting-sources user`` isolation is preserved instead.

        Args:
            prompt: The effective prompt text.
            read_only: Whether the current step is read-only.
            context_files: Optional list of files to include as context.

        Returns:
            CLI argument list (excluding the command name and the runner's
            own ``--dangerously-skip-permissions`` / ``--setting-sources``
            flags, which are prepended by the execution methods).
        """
        # invocation_intent is deliberately not translated into anything:
        # Claude Code's /goal was tried here and retired — its goal-condition
        # argument is hard-capped at 4000 characters, far below any real
        # implement prompt, so the prefix made every DIRECT_IMPLEMENTATION
        # call fail outright. Completion pressure is owned by the outer flow
        # (tests / review / fix-iterations), so all intents share one argv
        # shape.
        args: List[str] = [
            "--output-format", "stream-json",
            "--verbose",
            "-p", prompt,
        ]

        # WHY: one merged --disallowedTools flag, never two — the claude CLI
        # resolves a repeated flag last-one-wins, so appending a second flag
        # for ReportFindings would silently discard the read-only write-tool
        # lock built above it.
        disallowed: List[str] = []
        if read_only:
            disallowed += ["Write", "Edit", "NotebookEdit", "AskUserQuestion"]
        disallowed.append("ReportFindings")
        args += ["--disallowedTools"] + disallowed

        if context_files:
            for f in context_files:
                if f.exists():
                    args.extend(["--file", str(f)])

        return args

    @staticmethod
    def detect_usage_limit(returncode: int, stdout: str, stderr: str) -> bool:
        """Detect if the failure is due to usage/rate limit.

        Checks the last part of output for known limit indicators.
        Only checks when returncode is non-zero and only examines
        the last few lines to avoid false positives from source code.
        """
        # Only check for usage limit if command actually failed
        if returncode == 0:
            return False

        # Only check the last part of output (last 3000 chars or last 20 lines)
        # Error messages are typically at the end, while source code reading
        # (like claude_runner.py docstrings) appears earlier in the output
        combined = (stdout or "") + (stderr or "")

        # Get last 3000 characters
        tail_content = combined[-3000:].lower()

        # Also get last 20 lines for line-based filtering
        lines = combined.split('\n')
        last_lines = '\n'.join(lines[-20:]).lower()

        for keyword in USAGE_LIMIT_KEYWORDS:
            # Check both tail content and last lines for robustness
            if keyword in tail_content or keyword in last_lines:
                return True

        # Exit code 2 is sometimes used for API errors including rate limits
        if returncode == 2:
            tail_lower = tail_content.lower()
            if "rate_limit" in tail_lower or "usage limit" in tail_lower:
                return True

        return False

    @staticmethod
    def detect_timeout(returncode: int) -> bool:
        """Detect if the failure is due to timeout.

        Exit code 124 is the standard timeout(1) exit code.
        """
        return returncode == 124

    def detect_infra_error(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> InfraErrorType:
        """Detect infrastructure errors from execution results.

        Combines usage-limit and timeout detection into a single
        :class:`InfraErrorType` result for use by :class:`LLMCaller`.
        """
        if self.detect_usage_limit(returncode, stdout, stderr):
            return InfraErrorType.USAGE_LIMIT
        if self.detect_timeout(returncode):
            return InfraErrorType.TIMEOUT
        return InfraErrorType.NONE

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
        on_confirm: Optional[
            Callable[[str, List[str], Callable[[], bool]], Optional[str]]
        ] = None,
    ) -> "MonitoredResult":
        """Run Claude with activity-based monitoring (single command, no fallback).

        Provides real-time monitoring of the Claude process:
        - Reads stdout/stderr continuously to detect "stuck" state
        - Records last activity timestamp
        - If no output for inactivity_timeout seconds, kills process
        - Optionally writes output to log file in real-time

        Args:
            args: Arguments to pass after the claude command.
            log_file: Optional path to write all output.
            wall_timeout: Maximum total runtime in seconds.
            inactivity_timeout: Seconds without output before considering stuck.
            cwd: Working directory.
            env: Environment variables.
            on_output: Callback for each line of output.
            on_activity: Callback for activity detection.
            on_confirm: Optional callback invoked when a CLI-subprocess
                confirmation prompt is detected in the child's output. It
                receives ``(prompt_text, options, is_alive)`` and returns the
                answer string to write back to the child's stdin, or ``None``
                to leave the prompt unanswered. ``is_alive()`` reports whether
                the child is still running, so a blocking callback can stop
                waiting once the child exits. When supplied, the child is
                spawned with a writable stdin pipe.

        Returns:
            MonitoredResult with exit code, output, and metadata.
        """
        start_time = time.time()
        cmd_name = self.command["cmd"]

        run_env = env
        if run_env is None:
            run_env = dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        try:
            resolved_args, stdin_prompt = self._resolve_args(args, cwd)
            full_cmd = [
                cmd_name,
                "--dangerously-skip-permissions",
                "--setting-sources",
                self._setting_sources_arg,
            ] + resolved_args

            print(
                f"[claude-runner] Running command: '{cmd_name}'",
                file=sys.stderr,
            )

            result = self._run_single_with_monitor(
                full_cmd=full_cmd,
                cmd_name=cmd_name,
                cmd_index=0,
                log_file=log_file,
                wall_timeout=wall_timeout,
                inactivity_timeout=inactivity_timeout,
                cwd=cwd,
                env=run_env,
                on_output=on_output,
                on_activity=on_activity,
                start_time=start_time,
                stdin_prompt=stdin_prompt,
                on_confirm=on_confirm,
            )

            output = f"=== Command: {cmd_name} ===\n{result.output}"

            if result.interrupted:
                return MonitoredResult(
                    returncode=result.returncode,
                    output=output,
                    cmd_used=cmd_name,
                    cmd_index=0,
                    was_retry=False,
                    interrupted=True,
                    stderr_tail=result.stderr_tail,
                )

            if result.success:
                print(
                    f"[claude-runner] Command '{cmd_name}' succeeded",
                    file=sys.stderr,
                )

            return MonitoredResult(
                returncode=result.returncode,
                output=output,
                cmd_used=cmd_name,
                cmd_index=0,
                was_retry=False,
                stderr_tail=result.stderr_tail,
            )

        except Exception as e:
            msg = f"[claude-runner] Error running command '{cmd_name}': {e}"
            print(msg, file=sys.stderr)
            return MonitoredResult(
                returncode=1,
                output=msg,
                cmd_used=cmd_name,
                cmd_index=0,
                was_retry=False,
            )

    def _run_single_with_monitor(
        self,
        full_cmd: List[str],
        cmd_name: str,
        cmd_index: int,
        log_file: Optional[Path],
        wall_timeout: Optional[int],
        inactivity_timeout: int,
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        on_output: Optional[Callable[[str], None]],
        on_activity: Optional[Callable[[], None]],
        start_time: float,
        stdin_prompt: Optional[str] = None,
        on_confirm: Optional[
            Callable[[str, List[str], Callable[[], bool]], Optional[str]]
        ] = None,
    ) -> "_SingleRunResult":
        """Run a single command with monitoring and enhanced hang detection."""

        # Bounded tail of the child's stderr, filled by _spawn_stderr_reader
        # and exposed on the result for CLI-level diagnostics.
        stderr_buffer: Deque[str] = collections.deque(
            maxlen=_STDERR_BUFFER_MAXLEN
        )

        # Check if command exists
        import shutil
        if not shutil.which(full_cmd[0]):
            msg = f"\n[claude-runner] Command '{cmd_name}' not found, skipping...\n"
            if log_file:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(msg)
            return _SingleRunResult(
                returncode=127,  # Command not found
                output=msg,
                success=False,
                should_retry=True,
                stderr_tail="".join(stderr_buffer),
            )

        # If we need to feed a prompt via stdin (large-prompt path), open a
        # PIPE and write the prompt in a background thread so the monitor
        # loop can concurrently drain stdout without deadlocking on a full
        # pipe buffer.
        # Otherwise: stdin=None in interactive terminal to support proper
        # Unicode input (e.g., Chinese character deletion); DEVNULL in
        # non-interactive mode to prevent hanging.
        if stdin_prompt is not None:
            stdin_arg = subprocess.PIPE
        elif on_confirm is not None:
            # Keep a writable stdin pipe open so confirmation-prompt answers
            # captured via ``on_confirm`` can be routed back to the child.
            stdin_arg = subprocess.PIPE
        else:
            stdin_arg = None if sys.stdin.isatty() else subprocess.DEVNULL

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin_arg,
            cwd=cwd,
            env=env,
            bufsize=1,
            universal_newlines=True,
        )

        if stdin_prompt is not None:
            _spawn_stdin_writer(proc, stdin_prompt)

        # Drain stderr in a background thread so the pipe never fills and
        # the NDJSON on stdout stays clean.  Use a dedicated log file so
        # stderr content is auditable without mixing into stdout.
        _stderr_log = None
        if log_file is not None:
            _stderr_log = log_file.parent / f"{log_file.name}.stderr"
        _stderr_thread = _spawn_stderr_reader(
            proc, log_file=_stderr_log, stderr_buffer=stderr_buffer,
        )

        def _stderr_tail() -> str:
            # The child has exited on every path reaching here; a bounded
            # join lets the reader drain the last buffered lines before the
            # tail is captured.
            try:
                _stderr_thread.join(timeout=5)
            except Exception:
                pass
            return "".join(stderr_buffer)

        output_buffer = []
        last_activity = time.time()
        log_fh = None
        hang_detected = False

        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_file, "a", encoding="utf-8")
            log_fh.write(f"\n=== Starting: {' '.join(full_cmd)} ===\n")
            log_fh.flush()

        try:
            while proc.poll() is None:
                # Check wall timeout
                if wall_timeout and (time.time() - start_time) > wall_timeout:
                    proc.kill()
                    proc.wait()
                    msg = f"\n[claude-runner] Wall timeout ({wall_timeout}s) exceeded\n"
                    output_buffer.append(msg)
                    if log_fh:
                        log_fh.write(msg)
                        log_fh.flush()
                    return _SingleRunResult(
                        returncode=124,
                        output="".join(output_buffer),
                        success=False,
                        should_retry=True,
                        stderr_tail=_stderr_tail(),
                    )

                # Check for output with timeout (handle EINTR from signals)
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                except InterruptedError:
                    continue  # Signal received, retry
                except Exception as e:
                    output_buffer.append(f"\n[claude-runner] select() error: {e}\n")
                    continue

                if ready:
                    try:
                        line = proc.stdout.readline()
                        if line:
                            last_activity = time.time()
                            output_buffer.append(line)
                            if log_fh:
                                log_fh.write(line)
                                log_fh.flush()
                            if on_output:
                                on_output(line)
                            if on_activity:
                                on_activity()
                            if on_confirm is not None:
                                detected = detect_confirmation_prompt(line)
                                if detected is not None:
                                    prompt_text, options = detected
                                    try:
                                        answer = on_confirm(
                                            prompt_text,
                                            options,
                                            lambda: proc.poll() is None,
                                        )
                                    except Exception as exc:  # noqa: BLE001
                                        answer = None
                                        print(
                                            f"[claude-runner] on_confirm callback "
                                            f"error: {exc}",
                                            file=sys.stderr,
                                        )
                                    if answer is not None:
                                        _write_stdin_response(proc, answer)
                                    # The callback may have blocked while
                                    # awaiting a response; reset the activity
                                    # clock so that wait does not trip the
                                    # inactivity-hang detector.
                                    last_activity = time.time()
                    except Exception:
                        pass
                else:
                    # No output available - check inactivity and detect possible hang
                    inactive_time = time.time() - last_activity
                    if inactive_time > inactivity_timeout:
                        # Enhanced hang detection
                        hang_confirmed = False

                        # Check if process is consuming excessive resources (CPU/memory) without output
                        if psutil:
                            try:
                                p = psutil.Process(proc.pid)
                                cpu_percent = p.cpu_percent(interval=0.5)
                                mem_info = p.memory_info()
                                # Check for high CPU usage without output (potential hang)
                                if cpu_percent > 80.0:
                                    msg = (
                                        f"\n[claude-runner] Hang detected - high CPU usage "
                                        f"({cpu_percent:.1f}%) without output for {int(inactive_time)}s\n"
                                    )
                                    hang_confirmed = True
                                # Check for excessive memory growth without output
                                elif mem_info.rss > 1024 * 1024 * 1024:  # 1GB
                                    msg = (
                                        f"\n[claude-runner] Hang detected - excessive memory usage "
                                        f"({mem_info.rss // (1024*1024)}MB) without output for {int(inactive_time)}s\n"
                                    )
                                    hang_confirmed = True
                            except Exception:
                                pass

                        if not hang_confirmed:
                            # Default to inactivity timeout
                            msg = (
                                f"\n[claude-runner] Hang detected - inactivity timeout "
                                f"({inactivity_timeout}s) - no output for {int(inactive_time)}s\n"
                            )
                            hang_confirmed = True

                        if hang_confirmed:
                            try:
                                proc.kill()
                                proc.wait(timeout=10)  # Wait for process to terminate
                            except Exception:
                                try:
                                    proc.terminate()
                                    proc.wait(timeout=5)
                                except Exception:
                                    pass

                            output_buffer.append(msg)
                            if log_fh:
                                log_fh.write(msg)
                                log_fh.flush()
                            hang_detected = True
                            return _SingleRunResult(
                                returncode=124,
                                output="".join(output_buffer),
                                success=False,
                                should_retry=True,
                                stderr_tail=_stderr_tail(),
                            )

            # Process finished - read remaining output
            remaining = proc.stdout.read()
            if remaining:
                output_buffer.append(remaining)
                if log_fh:
                    log_fh.write(remaining)
                    log_fh.flush()

            returncode = proc.returncode
            output = "".join(output_buffer)

            # Check for usage limit in output
            if self.detect_usage_limit(returncode, output, ""):
                msg = f"\n[claude-runner] Usage limit detected for '{cmd_name}'\n"
                output += msg
                if log_fh:
                    log_fh.write(msg)
                    log_fh.flush()
                return _SingleRunResult(
                    returncode=returncode,
                    output=output,
                    success=False,
                    should_retry=True,
                    stderr_tail=_stderr_tail(),
                )

            # Check if process terminated with unusual exit codes that might indicate a hang
            if returncode in [1, 137, 143]:  # 137=SIGKILL, 143=SIGTERM
                # If we have output but process terminated with error, check if it was a hang
                if hang_detected or (len(output_buffer) > 0 and "timeout" in output.lower()):
                    return _SingleRunResult(
                        returncode=returncode,
                        output=output,
                        success=False,
                        should_retry=True,
                        stderr_tail=_stderr_tail(),
                    )

            return _SingleRunResult(
                returncode=returncode,
                output=output,
                success=returncode == 0,
                should_retry=False,
                stderr_tail=_stderr_tail(),
            )

        except KeyboardInterrupt:
            # Ctrl+C: kill subprocess and return partial output so callers
            # can persist it to history before re-raising the interrupt.
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            # Drain any remaining buffered output
            try:
                remaining = proc.stdout.read()
                if remaining:
                    output_buffer.append(remaining)
            except Exception:
                pass
            return _SingleRunResult(
                returncode=-2,
                output="".join(output_buffer),
                success=False,
                should_retry=False,
                interrupted=True,
                stderr_tail=_stderr_tail(),
            )

        finally:
            # Close the stdin pipe (if any) so a child still blocked on a
            # confirmation read sees EOF and can wind down cleanly.
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            if log_fh:
                log_fh.close()
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)  # Non-blocking wait with timeout
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    # Force kill if still running or interrupted
                    try:
                        proc.kill()
                    except Exception:
                        pass


@dataclass
class MonitoredResult:
    """Result from run_with_monitor."""

    returncode: int
    output: str
    cmd_used: str
    cmd_index: int
    was_retry: bool
    interrupted: bool = False  # True if stopped by KeyboardInterrupt
    # Bounded tail of the child's stderr, for CLI-level diagnostics (e.g.
    # infra-error reports) that never reach the NDJSON stdout stream.
    stderr_tail: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0


@dataclass
class _SingleRunResult:
    """Internal result from a single command run."""

    returncode: int
    output: str
    success: bool
    should_retry: bool
    interrupted: bool = False  # True if stopped by KeyboardInterrupt
    stderr_tail: str = ""


# Backward-compatibility alias
ClaudeRunner = ClaudeCodeRunner
