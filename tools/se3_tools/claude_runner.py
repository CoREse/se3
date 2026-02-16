"""Claude Command Resolver with priority-based fallback.

Provides a unified way to invoke Claude CLI across SE3 modules.
Supports multiple configured commands with automatic fallback on
usage limits or timeouts.
"""

import os
import select
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import load_claude_commands

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


class ClaudeRunner:
    """Runs Claude CLI commands with priority-based fallback.

    On usage limit or timeout, automatically retries with the next
    configured command in priority order.
    """

    def __init__(self, project_root: Optional[Path] = None, commands: Optional[List[Dict[str, Any]]] = None):
        """Initialize with command list.

        Args:
            project_root: Project root for loading config. Ignored if commands is given.
            commands: Explicit command list. If None, loaded from config.
        """
        if commands is not None:
            self.commands = commands
        else:
            self.commands = load_claude_commands(project_root)

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
                    temp_dir = cwd or Path.cwd()
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
                        temp_dir = cwd or Path.cwd()
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
                    # Regular prompt text - write to temp file to avoid CLI issues
                    import tempfile
                    temp_dir = cwd or Path.cwd()
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.prompt',
                                                   dir=temp_dir, delete=False,
                                                   encoding='utf-8') as f:
                        f.write(prompt_arg)
                        temp_file = Path(f.name)
                    resolved.append(arg)
                    resolved.append(f"@{temp_file}")
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
        """Run Claude synchronously with fallback.

        Tries each command in priority order. On usage limit or timeout,
        switches to the next command. Handles @file syntax for prompt files.

        Args:
            args: Arguments to pass after the claude command (e.g. ["-p", prompt] or ["@prompt.txt"]).
            timeout: Timeout in seconds.
            cwd: Working directory.
            env: Environment variables.
            on_retry: Optional callback(cmd_index, failed_cmd) -> new_args or None.
                      If it returns new args, those replace the original args.
                      If None or returns None, original args are reused.

        Returns:
            subprocess.CompletedProcess from the successful (or last) attempt.

        Raises:
            AllCommandsExhausted: If all commands fail with usage limit/timeout.
        """
        last_result = None
        temp_files = []

        for i, cmd_entry in enumerate(self.commands):
            cmd_name = cmd_entry["cmd"]
            current_args = args

            # If retrying (not first attempt), consult callback
            if i > 0 and on_retry is not None:
                new_args = on_retry(i, self.commands[i - 1]["cmd"])
                if new_args is not None:
                    current_args = new_args

            # Resolve arguments (handle @file syntax)
            resolved_args = self._resolve_args(current_args, cwd)
            # Collect temp files to clean up later
            for arg in resolved_args:
                if arg.startswith("@"):
                    temp_files.append(Path(arg[1:]))

            full_cmd = [cmd_name] + resolved_args

            try:
                result = subprocess.run(
                    full_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=env,
                )
                last_result = result

                # Check for usage limit
                if self.detect_usage_limit(result.returncode, result.stdout, result.stderr):
                    print(
                        f"[claude-runner] Usage limit hit with '{cmd_name}', "
                        f"trying next command...",
                        file=sys.stderr,
                    )
                    continue

                # Success or non-limit failure — return as-is
                return result

            except subprocess.TimeoutExpired:
                print(
                    f"[claude-runner] Timeout with '{cmd_name}', "
                    f"trying next command...",
                    file=sys.stderr,
                )
                # Create a synthetic CompletedProcess for the timeout case
                last_result = subprocess.CompletedProcess(
                    args=full_cmd, returncode=124, stdout="", stderr="timeout"
                )
                continue

        # All commands exhausted
        if last_result is not None:
            return last_result

        # Clean up temp files
        for temp_file in temp_files:
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Should not reach here, but just in case
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="No claude commands configured"
        )

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
        full_cmd = [cmd_entry["cmd"]] + resolved_args

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

        Args:
            cmd_index: Index of the command that failed.
            args: Arguments for the new process.
            cwd: Working directory.
            env: Environment variables.
            stdout: stdout handling.
            stderr: stderr handling.
            **kwargs: Additional Popen arguments.

        Returns:
            Tuple of (Popen process, new cmd_index) or None if exhausted.
        """
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

        Checks exit code and output for known limit indicators.
        """
        combined = (stdout or "").lower() + (stderr or "").lower()

        for keyword in USAGE_LIMIT_KEYWORDS:
            if keyword in combined:
                return True

        # Exit code 2 is sometimes used for API errors including rate limits
        if returncode == 2 and ("error" in combined or "limit" in combined):
            return True

        return False

    @staticmethod
    def detect_timeout(returncode: int) -> bool:
        """Detect if the failure is due to timeout.

        Exit code 124 is the standard timeout(1) exit code.
        """
        return returncode == 124

    def run_with_monitor(
        self,
        args: List[str],
        log_file: Optional[Path] = None,
        wall_timeout: Optional[int] = None,
        inactivity_timeout: int = 300,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> "MonitoredResult":
        """Run Claude with activity-based monitoring and automatic command fallback.

        This method provides real-time monitoring of the Claude process:
        - Reads stdout/stderr continuously to detect "stuck" state
        - Records last activity timestamp (any output)
        - If no output for inactivity_timeout seconds, kills process and tries next command
        - Optionally writes all output to log file in real-time
        - Automatically switches to next configured command on usage limit / inactivity
        - Handles @file syntax for prompt files

        Args:
            args: Arguments to pass after the claude command (e.g. ["-p", prompt] or ["@prompt.txt"]).
            log_file: Optional path to write all output (real-time).
            wall_timeout: Maximum total runtime in seconds (None for no limit).
            inactivity_timeout: Seconds without output before considering stuck.
            cwd: Working directory.
            env: Environment variables.
            on_output: Optional callback for each line of output.

        Returns:
            MonitoredResult with exit code, output, and metadata.
        """
        start_time = time.time()
        all_outputs = []

        temp_files = []
        for cmd_index, cmd_entry in enumerate(self.commands):
            cmd_name = cmd_entry["cmd"]

            # Resolve arguments (handle @file syntax)
            resolved_args = self._resolve_args(args, cwd)
            # Collect temp files to clean up later
            for arg in resolved_args:
                if arg.startswith("@"):
                    temp_files.append(Path(arg[1:]))

            full_cmd = [cmd_name] + resolved_args

            result = self._run_single_with_monitor(
                full_cmd=full_cmd,
                cmd_name=cmd_name,
                cmd_index=cmd_index,
                log_file=log_file,
                wall_timeout=wall_timeout,
                inactivity_timeout=inactivity_timeout,
                cwd=cwd,
                env=env,
                on_output=on_output,
                start_time=start_time,
            )

            all_outputs.append(f"=== Command: {cmd_name} ===")
            all_outputs.append(result.output)

            if result.success:
                return MonitoredResult(
                    returncode=result.returncode,
                    output="\n".join(all_outputs),
                    cmd_used=cmd_name,
                    cmd_index=cmd_index,
                    was_retry=cmd_index > 0,
                )

            # Check if we should retry
            if result.should_retry and cmd_index < len(self.commands) - 1:
                print(
                    f"[claude-runner] Switching from '{cmd_name}' to "
                    f"'{self.commands[cmd_index + 1]['cmd']}'...",
                    file=sys.stderr,
                )
                continue

            # Final command failed
            return MonitoredResult(
                returncode=result.returncode,
                output="\n".join(all_outputs),
                cmd_used=cmd_name,
                cmd_index=cmd_index,
                was_retry=cmd_index > 0,
            )

        # Clean up temp files
        for temp_file in temp_files:
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Should not reach here
        return MonitoredResult(
            returncode=1,
            output="\n".join(all_outputs),
            cmd_used="none",
            cmd_index=-1,
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
        start_time: float,
    ) -> "_SingleRunResult":
        """Run a single command with monitoring."""

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

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            bufsize=1,
            universal_newlines=True,
        )

        output_buffer = []
        last_activity = time.time()
        log_fh = None

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
                    except Exception:
                        pass
                else:
                    # No output available - check inactivity
                    inactive_time = time.time() - last_activity
                    if inactive_time > inactivity_timeout:
                        proc.kill()
                        proc.wait()
                        msg = (
                            f"\n[claude-runner] Inactivity timeout "
                            f"({inactivity_timeout}s) - no output for {int(inactive_time)}s\n"
                        )
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

            return _SingleRunResult(
                returncode=returncode,
                output=output,
                success=returncode == 0,
                should_retry=False,
            )

        finally:
            if log_fh:
                log_fh.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@dataclass
class MonitoredResult:
    """Result from run_with_monitor."""

    returncode: int
    output: str
    cmd_used: str
    cmd_index: int
    was_retry: bool

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
