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


class AgentRunner(ABC):
    """Abstract base class for all agent runners.

    Defines the contract that LLMCaller uses to interact with agent
    implementations. Each runner wraps a specific agent type (e.g.,
    Claude Code CLI, API-based agents).
    """

    startup_provider: Optional[str] = None
    startup_model: Optional[str] = None

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
        spec_guard_plugin: Optional[Path] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
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
            spec_guard_plugin: Optional path to the guard plugin directory
                installing the spec-write PreToolUse hook.  Only
                ``ClaudeCodeRunner`` honors it (via ``--plugin-dir``); other
                runners ignore the intent (their sandboxing is handled
                separately).
            invocation_intent: Semantic purpose of this invocation. Kept as
                information only: no current runner translates it into a
                native feature (Claude Code's /goal was retired — its
                goal-condition argument is capped far below real implement
                prompt sizes), so every runner executes the prompt through
                its normal autonomous interface.

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
