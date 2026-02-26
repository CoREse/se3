"""LLM caller for step execution.

Handles subprocess calls to Claude CLI with retry and fallback logic.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..claude_runner import ClaudeRunner

logger = logging.getLogger(__name__)

# Module-level extra prompt state for Ctrl+C injection
_extra_prompt: Optional[str] = None


def set_extra_prompt(prompt: Optional[str]) -> None:
    """Set an extra prompt to inject into the next LLM call."""
    global _extra_prompt
    _extra_prompt = prompt


def get_extra_prompt() -> Optional[str]:
    """Get the current extra prompt (None if not set)."""
    return _extra_prompt


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
        # Inject extra prompt if set (from Ctrl+C user injection)
        global _extra_prompt
        if _extra_prompt:
            prompt = f"{prompt}\n\n[Additional user instruction]: {_extra_prompt}"
            logger.info(f"Injected extra prompt: {_extra_prompt[:80]}")
            _extra_prompt = None  # Consume after use

        # Use stream-json format for streaming JSON output with verbose mode
        args = ["--output-format", "stream-json", "--verbose", "-p", prompt]

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
                    duration_s = time.time() - start_time
                    logger.debug(f"LLM call succeeded in {int(duration_s * 1000)}ms")
                    _print_response_summary(result.output, duration_s)
                    return result.output

                last_error = f"Command '{result.cmd_used}' failed with exit code {result.returncode}"
                logger.warning(f"LLM call failed: {last_error}, attempt {attempt + 1}/{self.max_retries}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM call exception: {last_error}, attempt {attempt + 1}/{self.max_retries}")

            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise LLMCallError(f"LLM call failed after {self.max_retries} attempts: {last_error}")


def _print_response_summary(output: str, duration_s: float) -> None:
    """Print a summary of the LLM response to terminal."""
    size_kb = len(output.encode("utf-8")) / 1024
    duration_str = f"{duration_s:.1f}s"

    text = output.strip()

    # Check for NDJSON format (stream-json output)
    lines = text.split('\n')
    if len(lines) > 1:
        # Try to parse first few lines to detect NDJSON
        json_lines = 0
        for line in lines[:10]:  # Check first 10 lines
            line = line.strip()
            if line:
                try:
                    json.loads(line)
                    json_lines += 1
                except json.JSONDecodeError:
                    pass

        if json_lines > 1:
            # This is NDJSON format
            print(f"  [llm-response] ✓ NDJSON received: {len(lines)} lines ({size_kb:.1f}KB, {duration_str})")
            return

    # Try to detect and parse single JSON in the output
    # Handle markdown code blocks wrapping JSON
    if text.startswith("```"):
        first_newline = text.find("\n")
        last_fence = text.rfind("```")
        if first_newline != -1 and last_fence > first_newline:
            text = text[first_newline + 1:last_fence].strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            keys = ", ".join(parsed.keys())
            print(f"  [llm-response] ✓ JSON received: {{{keys}}} ({size_kb:.1f}KB, {duration_str})")
        else:
            print(f"  [llm-response] ✓ JSON received ({size_kb:.1f}KB, {duration_str})")
    except (json.JSONDecodeError, ValueError):
        # Not JSON — show text length summary
        lines = output.count("\n") + 1
        print(f"  [llm-response] ✓ Text received: {lines} lines ({size_kb:.1f}KB, {duration_str})")
