"""Abstract base class for agent runners.

Defines the unified interface that all agent runners must implement.
Currently only ClaudeCodeRunner (claude_runner.py) implements this,
but the abstraction allows future runner types (API-based, other CLIs).
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class InfraErrorType(Enum):
    """Types of infrastructure errors that warrant agent rotation."""

    NONE = "none"
    USAGE_LIMIT = "usage_limit"
    TIMEOUT = "timeout"
    HANG = "hang"


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


class AgentRunner(ABC):
    """Abstract base class for all agent runners.

    Defines the contract that LLMCaller uses to interact with agent
    implementations. Each runner wraps a specific agent type (e.g.,
    Claude Code CLI, API-based agents).
    """

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
