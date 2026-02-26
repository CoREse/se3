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


class StreamJSONTracker:
    """Tracks and prints real-time summary for stream-json output.
    
    Processes each line of NDJSON output immediately and prints a summary,
    allowing users to see progress as Claude Code runs.
    """
    
    def __init__(self):
        self.message_count = 0
        self.tool_calls = []
        self.tool_results = []
        self.text_chunks = 0
        self.total_text_len = 0
        self.start_time = time.time()
    
    def process_line(self, line: str) -> None:
        """Process a single line of NDJSON output."""
        line = line.strip()
        if not line:
            return
        
        try:
            data = json.loads(line)
            msg_type = data.get('type', '')
            
            if msg_type == 'assistant':
                self.message_count += 1
                message = data.get('message', {})
                content = message.get('content', [])
                for item in content:
                    if isinstance(item, dict):
                        if item.get('type') == 'text':
                            text = item.get('text', '')
                            if text:
                                self.text_chunks += 1
                                self.total_text_len += len(text)
                                # Print progress for text chunks
                                if self.text_chunks <= 3 or self.text_chunks % 10 == 0:
                                    preview = text[:60].replace('\n', ' ')
                                    print(f"  [llm-stream] 💬 Text chunk #{self.text_chunks}: {preview}...")
                        elif item.get('type') == 'tool_use':
                            name = item.get('name', 'unknown')
                            self.tool_calls.append(name)
                            print(f"  [llm-stream] 🔧 Tool call: {name}")
                            
            elif msg_type == 'tool_result':
                result = data.get('result', {})
                tool_use_id = result.get('toolUseId', 'unknown')
                self.tool_results.append(tool_use_id)
                # Check if there's content in the result
                content = result.get('content', '')
                if content:
                    content_preview = str(content)[:60].replace('\n', ' ')
                    print(f"  [llm-stream] ✅ Tool result: {content_preview}...")
                else:
                    print(f"  [llm-stream] ✅ Tool result received")
                    
            elif msg_type == 'error':
                error_msg = data.get('error', 'Unknown error')
                print(f"  [llm-stream] ❌ Error: {error_msg}")
                
        except json.JSONDecodeError:
            # Not valid JSON, might be a partial line
            pass
    
    def print_summary(self) -> None:
        """Print final summary of the stream."""
        duration = time.time() - self.start_time
        print(f"  [llm-stream] ✓ Stream complete: {self.message_count} messages, "
              f"{len(self.tool_calls)} tool calls, {self.total_text_len} chars "
              f"({duration:.1f}s)")


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

        # Use stream-json format for real-time streaming output (requires --verbose)
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
                    # Use stream-json format with real-time tracking
                    stream_tracker = StreamJSONTracker()
                    
                    def on_stream_output(line: str) -> None:
                        """Process each line of output in real-time."""
                        stream_tracker.process_line(line)
                    
                    result = self._runner.run_with_monitor(
                        args=args,
                        wall_timeout=timeout,
                        inactivity_timeout=300,
                        cwd=self.project_root,
                        env=env,
                        on_output=on_stream_output,
                    )
                    
                    if result.success:
                        stream_tracker.print_summary()

                if result.success:
                    duration_s = time.time() - start_time
                    logger.debug(f"LLM call succeeded in {int(duration_s * 1000)}ms")
                    return result.output

                last_error = f"Command '{result.cmd_used}' failed with exit code {result.returncode}"
                logger.warning(f"LLM call failed: {last_error}, attempt {attempt + 1}/{self.max_retries}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM call exception: {last_error}, attempt {attempt + 1}/{self.max_retries}")

            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise LLMCallError(f"LLM call failed after {self.max_retries} attempts: {last_error}")


