"""LLM caller for step execution.

Handles subprocess calls to Claude CLI with retry and fallback logic.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..claude_runner import ClaudeRunner

logger = logging.getLogger(__name__)


class LLMCallError(Exception):
    """Error during LLM call."""

    pass


class LLMCaller:
    """Manages LLM calls within flow engine steps.

    Wraps ClaudeRunner with flow-engine-specific retry and fallback logic.
    Provides a simple interface for step handlers.
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._runner = ClaudeRunner(self.project_root)

    def call(
        self,
        prompt: str,
        timeout: int = 600,
        context_files: Optional[List[Path]] = None,
        on_output: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> str:
        """Call LLM with prompt and return output text.

        Args:
            prompt: Main prompt text
            timeout: Timeout in seconds
            context_files: Optional files to include as context
            on_output: Optional callback for real-time output
            **kwargs: Ignored (accepts model, max_tokens, temperature
                      for forward-compatibility but they don't apply
                      to claude -p subprocess calls)

        Returns:
            LLM output text

        Raises:
            LLMCallError: If all retries exhausted
        """
        args = ["-p", prompt]

        if context_files:
            for f in context_files:
                if f.exists():
                    args.extend(["--file", str(f)])

        env = dict(os.environ)
        # Remove CLAUDECODE to avoid nested session detection
        # This allows se3 run to invoke Claude CLI from within a Claude session
        env.pop("CLAUDECODE", None)

        start_time = time.time()
        last_error = ""

        for attempt in range(self.max_retries):
            try:
                logger.debug(f"LLM call attempt {attempt + 1}/{self.max_retries}")

                # Use run() instead of run_with_monitor() for better compatibility
                # with non-interactive shells (e.g., SSH + nohup environments)
                if on_output:
                    result = self._runner.run_with_monitor(
                        args=args,
                        wall_timeout=timeout,
                        inactivity_timeout=300,
                        cwd=self.project_root,
                        env=env,
                        on_output=on_output,
                    )
                else:
                    result = self._runner.run(
                        args=args,
                        timeout=timeout,
                        cwd=self.project_root,
                        env=env,
                    )
                    # Convert CompletedProcess to MonitoredResult-like object
                    from ..claude_runner import MonitoredResult
                    result = MonitoredResult(
                        returncode=result.returncode,
                        output=result.stdout or "",
                        cmd_used="claude",
                        cmd_index=0,
                        was_retry=False,
                    )

                if result.success:
                    duration_ms = int((time.time() - start_time) * 1000)
                    logger.debug(f"LLM call succeeded in {duration_ms}ms")
                    return result.output

                last_error = f"Command '{result.cmd_used}' failed with exit code {result.returncode}"
                logger.warning(f"LLM call failed: {last_error}, attempt {attempt + 1}/{self.max_retries}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM call exception: {last_error}, attempt {attempt + 1}/{self.max_retries}")

            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise LLMCallError(f"LLM call failed after {self.max_retries} attempts: {last_error}")
