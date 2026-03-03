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


def truncate_preview(text: str, max_length: int = 60, ellipsis_str: str = '...') -> str:
    """Truncate text to a preview with ellipsis if too long.

    Args:
        text: The text to truncate
        max_length: Maximum length before truncation
        ellipsis_str: String to append when truncated

    Returns:
        Truncated text with ellipsis if needed
    """
    if not text:
        return ""
    text = str(text).replace('\n', ' ')
    if len(text) <= max_length:
        return text
    return text[:max_length] + ellipsis_str


def format_tool_use_preview(tool_name: str, input_data: dict) -> str:
    """Format a tool_use preview showing key parameters.

    Args:
        tool_name: Name of the tool being called
        input_data: Tool input parameters dictionary

    Returns:
        Formatted preview string like 'Tool: <name> | Input: <param1>=<value1>, ...'
    """
    if not input_data or not isinstance(input_data, dict):
        return f"Tool: {tool_name} | Input: (none)"

    # Extract key parameters - show up to 3 key=value pairs
    params = []
    for i, (key, value) in enumerate(input_data.items()):
        if i >= 3:
            params.append("...")
            break

        # Format the value based on type
        if isinstance(value, str):
            val_preview = truncate_preview(value, max_length=30)
            params.append(f"{key}={val_preview}")
        elif isinstance(value, (int, float, bool)):
            params.append(f"{key}={value}")
        elif isinstance(value, (list, dict)):
            val_str = json.dumps(value, ensure_ascii=False)
            val_preview = truncate_preview(val_str, max_length=30)
            params.append(f"{key}={val_preview}")
        else:
            val_preview = truncate_preview(str(value), max_length=30)
            params.append(f"{key}={val_preview}")

    if not params:
        return f"Tool: {tool_name} | Input: (none)"

    return f"Tool: {tool_name} | Input: {', '.join(params)}"


def format_tool_result_preview(result_data: Any) -> str:
    """Format a tool_result preview.

    Args:
        result_data: Tool result data (can be string, dict, list, etc.)

    Returns:
        Formatted preview string like 'Result: <preview>'
    """
    if result_data is None:
        return "Result: (empty)"

    if isinstance(result_data, str):
        if not result_data.strip():
            return "Result: (empty)"
        return f"Result: {truncate_preview(result_data)}"

    if isinstance(result_data, dict):
        # Check for error in result
        if result_data.get('isError') or result_data.get('is_error'):
            error_msg = result_data.get('content', 'Unknown error')
            return f"Result (error): {truncate_preview(str(error_msg))}"

        # Format dict as JSON string
        result_str = json.dumps(result_data, ensure_ascii=False)
        return f"Result: {truncate_preview(result_str)}"

    if isinstance(result_data, list):
        result_str = json.dumps(result_data, ensure_ascii=False)
        return f"Result: {truncate_preview(result_str)}"

    # Default to string representation
    return f"Result: {truncate_preview(str(result_data))}"


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
                                    preview = truncate_preview(text)
                                    print(f"  [llm-stream] 💬 Text chunk #{self.text_chunks}: {preview}...")
                        elif item.get('type') == 'tool_use':
                            name = item.get('name', 'unknown')
                            tool_input = item.get('input', {})
                            self.tool_calls.append(name)
                            # Format and print tool_use preview
                            preview = format_tool_use_preview(name, tool_input)
                            print(f"  [llm-stream] 🔧 {preview}...")

            elif msg_type == 'tool_result':
                result = data.get('result', {})
                tool_use_id = result.get('toolUseId', 'unknown')
                self.tool_results.append(tool_use_id)
                # Extract and format tool result preview
                content = result.get('content', '')
                is_error = result.get('isError', False)

                if is_error:
                    error_preview = truncate_preview(str(content)) if content else "Unknown error"
                    print(f"  [llm-stream] ❌ Tool error: {error_preview}...")
                else:
                    preview = format_tool_result_preview(content)
                    print(f"  [llm-stream] ✅ {preview}...")

            elif msg_type == 'error':
                error_msg = data.get('error', 'Unknown error')
                print(f"  [llm-stream] ❌ Error: {truncate_preview(str(error_msg))}")

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
        flow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step_type: Optional[str] = None,
        external_attempt: int = 0,
    ):
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.flow_id = flow_id
        self.step_id = step_id
        self.step_type = step_type or ""
        self.external_attempt = external_attempt  # Track external retry (e.g., from implement.py)
        self._runner = ClaudeRunner(self.project_root)

    def call(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        context_files: Optional[List[Path]] = None,
        on_output: Optional[Callable[[str], None]] = None,
        require_json: bool = False,
        json_mode: Optional[str] = None,
        two_phase_json: bool = False,
        json_schema_hint: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Call LLM with prompt and return output text.

        Three JSON extraction modes are supported:

        1. STRICT (require_json=True, default):
           - Wraps prompt with strict JSON constraints
           - Retries up to max_retries if output is invalid
           - Best for: Simple, reliable outputs

        2. EXTRACT (json_mode="extract"):
           - Wraps prompt with JSON constraints
           - NO retries on parse failure
           - Uses LLM extraction as recovery instead
           - Best for: Balanced reliability and efficiency

        3. TWO_PHASE (json_mode="two_phase" or two_phase_json=True):
           - Clean prompt without JSON constraints
           - LLM extracts JSON from natural output
           - Best for: Complex outputs, avoiding prompt pollution

        Args:
            prompt: Main prompt text
            timeout: Deprecated, kept for API compatibility. Only inactivity timeout is used.
            context_files: Optional files to include as context
            on_output: Optional callback for real-time output
            require_json: Legacy flag for STRICT mode (kept for compatibility)
            json_mode: Explicit mode selection - "strict", "extract", "two_phase", or "off"
            two_phase_json: Legacy flag for TWO_PHASE mode (kept for compatibility)
            json_schema_hint: Optional hint about expected JSON schema for extraction
            **kwargs: Ignored (accepts model, max_tokens, temperature
                      for forward-compatibility but they don't apply
                      to claude -p subprocess calls)

        Returns:
            LLM output text (JSON if json_mode is not "off")

        Raises:
            LLMCallError: If all retries exhausted or extraction fails
        """
        # Resolve JSON mode from various parameter combinations
        mode = self._resolve_json_mode(json_mode, require_json, two_phase_json)

        # Inject extra prompt if set (from Ctrl+C user injection)
        global _extra_prompt
        if _extra_prompt:
            prompt = f"{prompt}\n\n[Additional user instruction]: {_extra_prompt}"
            logger.info(f"Injected extra prompt: {_extra_prompt[:80]}")
            _extra_prompt = None  # Consume after use

        # Dispatch to appropriate handler based on mode
        if mode == "two_phase":
            return self._call_two_phase(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                json_schema_hint=json_schema_hint,
            )
        elif mode == "extract":
            return self._call_extract(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                json_schema_hint=json_schema_hint,
            )
        elif mode == "strict":
            return self._call_strict(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
            )
        else:  # mode == "off"
            return self._call_with_retry(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                require_json=False,
                json_retry_count=0,
            )

    @staticmethod
    def _resolve_json_mode(
        json_mode: Optional[str],
        require_json: bool,
        two_phase_json: bool,
    ) -> str:
        """Resolve JSON mode from various parameter combinations.

        Priority:
        1. Explicit json_mode parameter
        2. two_phase_json=True -> "two_phase"
        3. require_json=True -> "strict"
        4. Default -> "off"
        """
        if json_mode is not None:
            mode = json_mode.lower()
            if mode in ("strict", "extract", "two_phase", "off"):
                return mode
            logger.warning(f"Unknown json_mode '{json_mode}', defaulting to 'off'")
            return "off"

        if two_phase_json:
            return "two_phase"

        if require_json:
            return "strict"

        return "off"

    def _call_strict(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
    ) -> str:
        """Mode 1: STRICT - Force JSON with retry on failure."""
        # Wrap prompt with strict JSON constraints
        json_prompt = (
            "CRITICAL: You MUST respond with ONLY valid JSON. "
            "Do NOT include any text, explanation, or markdown before or after the JSON.\n\n"
            f"{prompt}\n\n"
            "REMINDER: Respond with ONLY the JSON object. No other text."
        )

        return self._call_with_retry(
            prompt=json_prompt,
            timeout=timeout,
            context_files=context_files,
            on_output=on_output,
            require_json=True,
            json_retry_count=0,
        )

    def _call_extract(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        json_schema_hint: Optional[str],
    ) -> str:
        """Mode 2: EXTRACT - Request JSON, extract with LLM on failure."""
        # Wrap prompt with JSON constraints (like STRICT)
        json_prompt = (
            "CRITICAL: You MUST respond with ONLY valid JSON. "
            "Do NOT include any text, explanation, or markdown before or after the JSON.\n\n"
            f"{prompt}\n\n"
            "REMINDER: Respond with ONLY the JSON object. No other text."
        )

        # Call without JSON retry - extraction is the recovery
        output = self._call_with_retry(
            prompt=json_prompt,
            timeout=timeout,
            context_files=context_files,
            on_output=on_output,
            require_json=False,  # Don't retry on JSON error
            json_retry_count=0,
        )

        # Check if output is valid JSON
        if self._contains_valid_json(output):
            return output

        # Extract JSON using LLM
        print("  [llm-caller] 🔍 Extracting JSON from output (extract mode)...")

        from .json_extractor import JSONExtractor

        extractor = JSONExtractor(
            project_root=self.project_root,
            timeout=300,  # 5 minutes for large outputs
            max_content_length=200000,  # ~200KB for code with file contents
        )

        result = extractor.extract(
            raw_output=output,
            schema_hint=json_schema_hint,
        )

        if result is None:
            raise LLMCallError(
                "JSON extraction failed: Could not extract valid JSON from output"
            )

        # Return as JSON string (parse_json_response will handle it)
        json_str = json.dumps(result, ensure_ascii=False, indent=2)

        print("  [llm-caller] ✅ JSON extraction complete")
        return json_str

    def _call_two_phase(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        json_schema_hint: Optional[str],
    ) -> str:
        """Mode 3: TWO_PHASE - Natural generation + LLM extraction."""
        logger.info("Using two-phase JSON extraction")

        # Phase 1: Generate without JSON constraints (clean prompt)
        phase1_output = self._call_with_retry(
            prompt=prompt,
            timeout=timeout,
            context_files=context_files,
            on_output=on_output,
            require_json=False,  # No JSON constraint in phase 1
            json_retry_count=0,
        )

        # Phase 2: Extract JSON
        print("  [llm-caller] 🔍 Phase 2: Extracting JSON from output...")

        from .json_extractor import JSONExtractor

        extractor = JSONExtractor(
            project_root=self.project_root,
            timeout=300,  # 5 minutes for large outputs
            max_content_length=200000,  # ~200KB for code with file contents
        )

        result = extractor.extract(
            raw_output=phase1_output,
            schema_hint=json_schema_hint,
        )

        if result is None:
            raise LLMCallError(
                "Two-phase JSON extraction failed: Could not extract valid JSON from output"
            )

        # Return as JSON string (parse_json_response will handle it)
        json_str = json.dumps(result, ensure_ascii=False, indent=2)

        print("  [llm-caller] ✅ JSON extraction complete")
        return json_str

    @staticmethod
    def _format_as_stream_json(content: str) -> str:
        """Format content as stream-json (NDJSON) format for compatibility.

        Args:
            content: The text content to format

        Returns:
            NDJSON-formatted string
        """
        message = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": content}]
            }
        }
        return json.dumps(message, ensure_ascii=False)

    def _record_prompt(self, prompt: str, attempt: int) -> None:
        """Record a prompt to chat history if flow context is available."""
        if not self.flow_id or not self.step_id:
            return
        try:
            from .chat_history import record_prompt
            record_prompt(
                self.project_root, self.flow_id, self.step_id,
                self.step_type, prompt, attempt,
            )
        except Exception as e:
            logger.debug(f"Failed to record prompt to history: {e}")

    def _record_response(self, raw_ndjson: str, attempt: int) -> None:
        """Record an LLM response to chat history if flow context is available."""
        if not self.flow_id or not self.step_id:
            return
        try:
            from .chat_history import record_response
            record_response(
                self.project_root, self.flow_id, self.step_id,
                self.step_type, raw_ndjson, attempt,
            )
        except Exception as e:
            logger.debug(f"Failed to record response to history: {e}")

    def _get_retry_context(self) -> Optional[str]:
        """Get previous conversation context for retry injection."""
        if not self.flow_id or not self.step_id:
            return None
        try:
            from .chat_history import format_history_for_retry
            return format_history_for_retry(
                self.project_root, self.flow_id, self.step_id,
            )
        except Exception as e:
            logger.debug(f"Failed to get retry context: {e}")
            return None

    def _call_with_retry(
        self,
        prompt: str,
        timeout: int,
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        require_json: bool,
        json_retry_count: int,
        max_json_retries: int = 2,
    ) -> str:
        """Internal method to call LLM with retry logic."""
        original_prompt = prompt

        env = dict(os.environ)
        env.pop("CLAUDECODE", None)

        start_time = time.time()
        last_error = ""

        for internal_attempt in range(self.max_retries):
            # Combine external attempt (from caller) with internal attempt (network retries)
            # to determine if we should inject history context
            total_attempt = self.external_attempt * self.max_retries + internal_attempt
            
            # On retry (either external or internal), inject previous conversation context
            if total_attempt > 0:
                retry_context = self._get_retry_context()
                if retry_context:
                    effective_prompt = f"{retry_context}\n{original_prompt}"
                else:
                    effective_prompt = original_prompt
            else:
                effective_prompt = prompt

            args = ["--output-format", "stream-json", "--verbose", "-p", effective_prompt]

            if context_files:
                for f in context_files:
                    if f.exists():
                        args.extend(["--file", str(f)])

            # Record the prompt to chat history using external_attempt for grouping
            self._record_prompt(effective_prompt, self.external_attempt)

            try:
                logger.debug(f"LLM call internal_attempt {internal_attempt + 1}/{self.max_retries}, external_attempt {self.external_attempt}")

                if on_output:
                    result = self._runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        cwd=self.project_root,
                        env=env,
                        on_output=on_output,
                    )
                else:
                    stream_tracker = StreamJSONTracker()

                    def on_stream_output(line: str) -> None:
                        stream_tracker.process_line(line)

                    result = self._runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        cwd=self.project_root,
                        env=env,
                        on_output=on_stream_output,
                    )

                    if result.success:
                        stream_tracker.print_summary()

                # Record the response (whether success or failure)
                self._record_response(result.output or "", self.external_attempt)

                if result.success:
                    # Check if JSON is required but not received
                    if require_json and json_retry_count < max_json_retries:
                        if not self._contains_valid_json(result.output):
                            print(f"  [llm-caller] ⚠️  Response is not valid JSON, requesting JSON format (retry {json_retry_count + 1}/{max_json_retries})")
                            json_prompt = self._create_json_retry_prompt(prompt, result.output)
                            # Record the JSON retry prompt too (use a distinct attempt number for JSON retries)
                            json_attempt = self.external_attempt * 100 + json_retry_count  # Distinguish JSON retries
                            self._record_prompt(json_prompt, json_attempt)
                            # Increment external_attempt to ensure retry context is injected
                            # This is crucial because JSON retry needs the previous conversation context
                            # (including tool calls/results) to avoid re-reading files
                            self.external_attempt += 1
                            return self._call_with_retry(
                                prompt=json_prompt,
                                timeout=timeout,
                                context_files=context_files,
                                on_output=on_output,
                                require_json=require_json,
                                json_retry_count=json_retry_count + 1,
                                max_json_retries=max_json_retries,
                            )

                    duration_s = time.time() - start_time
                    logger.debug(f"LLM call succeeded in {int(duration_s * 1000)}ms")
                    return result.output

                last_error = f"Command '{result.cmd_used}' failed with exit code {result.returncode}"
                logger.warning(f"LLM call failed: {last_error}, internal attempt {internal_attempt + 1}/{self.max_retries}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM call exception: {last_error}, internal attempt {internal_attempt + 1}/{self.max_retries}")

            if internal_attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise LLMCallError(f"LLM call failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _contains_valid_json(output: str) -> bool:
        """Check if the output contains valid JSON in the assistant's text content."""
        from .utils.json_parser import parse_json_response
        result = parse_json_response(output)
        return result is not None

    @staticmethod
    def _create_json_retry_prompt(original_prompt: str, bad_output: str) -> str:
        """Create a prompt asking LLM to return JSON format."""
        # Extract what the LLM said (from assistant messages)
        text_content = ""
        for line in bad_output.strip().split('\n'):
            try:
                data = json.loads(line)
                if isinstance(data, dict) and data.get('type') == 'assistant':
                    message = data.get('message', {})
                    content = message.get('content', [])
                    for item in content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text = item.get('text', '')
                            if text:
                                text_content += text
            except json.JSONDecodeError:
                continue

        retry_prompt = f"""{original_prompt}

IMPORTANT: Your previous response was not in the required JSON format. You responded with:
---
{text_content[:500]}
---

Please respond ONLY with valid JSON as specified in the instructions above. Do not include any explanatory text before or after the JSON."""

        return retry_prompt
