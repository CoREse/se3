"""Claude Code CLI adapter (single-command runner).

Provides a unified way to invoke Claude Code CLI from SE3 modules.
Each ClaudeCodeRunner instance wraps a single agent command.
Agent selection/rotation is handled by LLMCaller.

Historical note: This module was originally ``ClaudeRunner`` with built-in
command list traversal.  The traversal logic has been moved to LLMCaller;
this runner now only executes a single command.  The ``ClaudeRunner`` alias
is kept for backward compatibility.
"""

import os
import select
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .agent_runner import AgentRunner, InfraErrorType
from .config import load_claude_commands

# Platform-specific imports for process resource monitoring
try:
    if sys.platform.startswith('linux'):
        import psutil
    else:
        psutil = None
except ImportError:
    psutil = None

# Keywords indicating usage/rate limit in Claude CLI output
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


class ClaudeCodeRunner(AgentRunner):
    """Pure Claude Code CLI adapter — executes a single command.

    This runner wraps one specific Claude CLI command (e.g. ``claude`` or
    ``kclaude``).  It does NOT traverse a list of commands or implement
    fallback logic; that responsibility belongs to :class:`LLMCaller`.

    For backward compatibility, the constructor also accepts the legacy
    ``commands`` parameter (list of dicts); in that case, only the first
    command is used.
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        commands: Optional[List[Dict[str, Any]]] = None,
        command: Optional[Dict[str, Any]] = None,
    ):
        """Initialize with a single command.

        Args:
            project_root: Project root for loading config.
            commands: Legacy parameter — list of command dicts.  If provided
                and ``command`` is None, the first entry is used.
            command: Single command dict ``{cmd, priority}``.  Preferred.
        """
        if command is not None:
            self.command = command
        elif commands is not None:
            self.command = commands[0] if commands else {"cmd": "claude", "priority": 0}
        else:
            all_commands = load_claude_commands(project_root)
            self.command = all_commands[0] if all_commands else {"cmd": "claude", "priority": 0}

        # Keep a commands list view for backward compatibility (popen, retry_with_next, helpers)
        if commands is not None:
            self.commands = commands
        else:
            self.commands = [self.command]

    @staticmethod
    def _resolve_args(args: List[str], cwd: Optional[Path] = None) -> List[str]:
        """Resolve arguments, handling @file syntax for prompt files.

        If an argument starts with "@", treat it as a file path and read the
        contents. This handles cases where prompt content is too long for
        command-line arguments.

        Args:
            args: Original arguments list
            cwd: Working directory for relative paths

        Returns:
            Resolved arguments list with @file syntax replaced
        """
        resolved = []
        i = 0

        # Use se3/tmp/ for temp files (visible, auto-cleaned by se3 done)
        project_root = cwd or Path.cwd()
        temp_dir = project_root / "se3" / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        while i < len(args):
            arg = args[i]

            # Handle @file syntax
            if arg.startswith("@"):
                file_path = Path(arg[1:])
                # If path is relative, use cwd if provided
                if not file_path.is_absolute() and cwd:
                    file_path = cwd / file_path

                if file_path.exists():
                    # Write content to temp file and use @ syntax
                    # This avoids command line length limitations
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.prompt',
                                                   dir=temp_dir, delete=False,
                                                   encoding='utf-8') as f:
                        f.write(file_path.read_text(encoding='utf-8'))
                        temp_file = Path(f.name)
                    resolved.append(f"@{temp_file}")
                else:
                    # If file not found, keep original arg
                    resolved.append(arg)
            elif arg in ["-p", "--prompt"] and (i + 1) < len(args):
                # Handle -p/--prompt with file content
                i += 1
                prompt_arg = args[i]

                if prompt_arg.startswith("@"):
                    file_path = Path(prompt_arg[1:])
                    if not file_path.is_absolute() and cwd:
                        file_path = cwd / file_path

                    if file_path.exists():
                        # Write content to temp file and use @ syntax
                        import tempfile
                        with tempfile.NamedTemporaryFile(mode='w', suffix='.prompt',
                                                       dir=temp_dir, delete=False,
                                                       encoding='utf-8') as f:
                            f.write(file_path.read_text(encoding='utf-8'))
                            temp_file = Path(f.name)
                        resolved.append(arg)
                        resolved.append(f"@{temp_file}")
                    else:
                        resolved.append(arg)
                        resolved.append(prompt_arg)
                else:
                    # Regular prompt text - pass directly to avoid temp file issues
                    # in non-interactive environments (SSH, nohup, etc.)
                    resolved.append(arg)
                    resolved.append(prompt_arg)
            else:
                resolved.append(arg)

            i += 1

        return resolved

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
        temp_files = []

        try:
            # Resolve arguments (handle @file syntax)
            resolved_args = self._resolve_args(args, cwd)
            for arg in resolved_args:
                if arg.startswith("@"):
                    temp_files.append(Path(arg[1:]))

            full_cmd = [cmd_name, "--dangerously-skip-permissions"] + resolved_args

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
                )
                return result

            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(
                    args=full_cmd, returncode=124, stdout="", stderr="timeout"
                )
        finally:
            for temp_file in temp_files:
                try:
                    temp_file.unlink(missing_ok=True)
                except Exception:
                    pass

    def popen(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
        cmd_index: int = 0,
        **kwargs: Any,
    ) -> Tuple[subprocess.Popen, int]:
        """Start Claude asynchronously (for collab workers/managers).

        Handles @file syntax for prompt files to avoid command-line length issues.

        Args:
            args: Arguments to pass after the claude command (e.g. ["-p", prompt] or ["@prompt.txt"]).
            cwd: Working directory.
            env: Environment variables.
            stdout: stdout handling (default PIPE).
            stderr: stderr handling (default PIPE).
            cmd_index: Index into commands list to start from.
            **kwargs: Additional Popen arguments.

        Returns:
            Tuple of (Popen process, cmd_index used).
        """
        if cmd_index >= len(self.commands):
            cmd_index = len(self.commands) - 1

        cmd_entry = self.commands[cmd_index]
        # Resolve arguments (handle @file syntax)
        resolved_args = self._resolve_args(args, cwd)
        full_cmd = [cmd_entry["cmd"], "--dangerously-skip-permissions"] + resolved_args

        proc = subprocess.Popen(
            full_cmd,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            **kwargs,
        )

        return proc, cmd_index

    def retry_with_next(
        self,
        cmd_index: int,
        args: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
        **kwargs: Any,
    ) -> Optional[Tuple[subprocess.Popen, int]]:
        """Retry with the next command after the given index.

        .. deprecated::
            Agent rotation is now handled by LLMCaller.  This method is
            kept for backward compatibility with collab modules.

        Returns:
            Tuple of (Popen process, new cmd_index) or None if exhausted.
        """
        warnings.warn(
            "retry_with_next is deprecated; agent rotation is handled by LLMCaller",
            DeprecationWarning,
            stacklevel=2,
        )
        next_index = cmd_index + 1
        if next_index >= len(self.commands):
            return None

        return self.popen(
            args=args,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            cmd_index=next_index,
            **kwargs,
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

        Returns:
            MonitoredResult with exit code, output, and metadata.
        """
        start_time = time.time()
        cmd_name = self.command["cmd"]

        run_env = env
        if run_env is None:
            run_env = dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        temp_files = []
        try:
            # Resolve arguments (handle @file syntax)
            resolved_args = self._resolve_args(args, cwd)
            for arg in resolved_args:
                if arg.startswith("@"):
                    temp_files.append(Path(arg[1:]))

            full_cmd = [cmd_name, "--dangerously-skip-permissions"] + resolved_args

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

        finally:
            for temp_file in temp_files:
                try:
                    temp_file.unlink(missing_ok=True)
                except Exception:
                    pass

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
    ) -> "_SingleRunResult":
        """Run a single command with monitoring and enhanced hang detection."""

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
            )

        # Use stdin=None when in interactive terminal to support proper
        # Unicode input (e.g., Chinese character deletion). Use DEVNULL
        # in non-interactive mode to prevent hanging.
        stdin_arg = None if sys.stdin.isatty() else subprocess.DEVNULL

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=stdin_arg,
            cwd=cwd,
            env=env,
            bufsize=1,
            universal_newlines=True,
        )

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
                    )

            return _SingleRunResult(
                returncode=returncode,
                output=output,
                success=returncode == 0,
                should_retry=False,
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
            )

        finally:
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


# Backward-compatibility alias
ClaudeRunner = ClaudeCodeRunner
