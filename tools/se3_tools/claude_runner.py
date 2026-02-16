"""Claude Command Resolver with priority-based fallback.

Provides a unified way to invoke Claude CLI across SE3 modules.
Supports multiple configured commands with automatic fallback on
usage limits or timeouts.
"""

import subprocess
import sys
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
        switches to the next command.

        Args:
            args: Arguments to pass after the claude command (e.g. ["-p", prompt]).
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

        for i, cmd_entry in enumerate(self.commands):
            cmd_name = cmd_entry["cmd"]
            current_args = args

            # If retrying (not first attempt), consult callback
            if i > 0 and on_retry is not None:
                new_args = on_retry(i, self.commands[i - 1]["cmd"])
                if new_args is not None:
                    current_args = new_args

            full_cmd = [cmd_name] + current_args

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

        Args:
            args: Arguments to pass after the claude command.
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
        full_cmd = [cmd_entry["cmd"]] + args

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
