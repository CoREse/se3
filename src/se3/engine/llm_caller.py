"""LLM caller for step execution.

Handles subprocess calls to Claude CLI with retry and fallback logic.
Manages agent selection and rotation on infrastructure errors.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..agent_runner import AgentRunner, InfraErrorType
from ..claude_runner import ClaudeCodeRunner, ClaudeRunner

logger = logging.getLogger(__name__)

# Module-level extra prompt state for Ctrl+C injection (transient, consumed after one use)
_extra_prompt: Optional[str] = None
# Persistent extra prompt state for loop context injection (survives across LLM calls)
_persistent_extra_prompt: Optional[str] = None
# Lock protecting _extra_prompt and _persistent_extra_prompt for thread safety
_extra_prompt_lock = threading.Lock()


def set_extra_prompt(prompt: Optional[str], persistent: bool = False) -> None:
    """Set an extra prompt to inject into LLM calls.

    Args:
        prompt: The prompt text to inject, or None to clear.
        persistent: If True, the prompt survives across multiple LLM calls
                   (used for loop context injection). If False (default),
                   the prompt is consumed after one LLM call (used for
                   Ctrl+C interrupt injection).
    """
    with _extra_prompt_lock:
        if persistent:
            global _persistent_extra_prompt
            _persistent_extra_prompt = prompt
        else:
            global _extra_prompt
            _extra_prompt = prompt


def get_extra_prompt() -> Optional[str]:
    """Get the current extra prompt (None if not set).

    Returns the combined transient + persistent prompt without consuming either.
    """
    with _extra_prompt_lock:
        parts = []
        if _persistent_extra_prompt:
            parts.append(_persistent_extra_prompt)
        if _extra_prompt:
            parts.append(_extra_prompt)
        return "\n\n".join(parts) if parts else None


def clear_extra_prompt() -> None:
    """Clear both transient and persistent extra prompts."""
    with _extra_prompt_lock:
        global _extra_prompt, _persistent_extra_prompt
        _extra_prompt = None
        _persistent_extra_prompt = None


def clear_persistent_extra_prompt() -> None:
    """Clear only the persistent extra prompt (for cleanup between loop iterations)."""
    with _extra_prompt_lock:
        global _persistent_extra_prompt
        _persistent_extra_prompt = None


def clear_phase1_cache(project_root: Path, flow_id: str, step_id: str) -> None:
    """Clear the Phase 1 cache file for a step.

    Called when a step is being restarted from scratch (revision or fix loop),
    so the next run performs a fresh Phase 1 LLM call instead of reusing a
    cached output from a previous attempt.

    Args:
        project_root: Project root directory
        flow_id: Flow instance ID
        step_id: Step instance ID
    """
    from .chat_history import _history_dir
    cache_path = _history_dir(project_root, flow_id) / f"{step_id}_phase1.txt"
    if cache_path.exists():
        try:
            cache_path.unlink()
            logger.info(f"Cleared Phase 1 cache for step {step_id}")
        except OSError as e:
            logger.warning(f"Failed to clear Phase 1 cache for {step_id}: {e}")


from .tool_formatters import (
    format_tool_diff,
    format_tool_result_preview,
    format_tool_use_preview,
    set_project_root,
    truncate_preview,
)


class LLMCallError(Exception):
    """Error during LLM call."""

    pass


class StreamJSONTracker:
    """Tracks and prints real-time summary for stream-json output.

    Processes each line of NDJSON output immediately and prints a summary,
    allowing users to see progress as Claude Code runs.
    """

    # Maximum number of cached tool entries before oldest are evicted
    _MAX_CACHE_SIZE = 100

    def __init__(self, stream_prefix: str = ''):
        self.stream_prefix = stream_prefix
        self.message_count = 0
        self.tool_calls = []
        self.tool_results = []
        self.text_chunks = 0
        self.total_text_len = 0
        self.start_time = time.time()
        self._last_ended_with_newline = True
        self._tool_use_id_to_name: Dict[str, str] = {}  # Map tool_use_id -> tool_name
        self._tool_use_id_to_input: Dict[str, dict] = {}  # Cache Edit/Write inputs for diff
        self._tool_use_id_to_old_content: Dict[str, Optional[str]] = {}  # Cache Write target file content

    def _handle_tool_result(self, tool_use_id: str, content: Any, is_error: bool) -> None:
        """Handle a single tool_result event.

        Shared by both the legacy top-level tool_result format and the
        newer type='user' nested format.
        """
        self.tool_results.append(tool_use_id)
        tool_name = self._tool_use_id_to_name.get(tool_use_id, '')

        if is_error:
            error_preview = truncate_preview(str(content)) if content else "Unknown error"
            print(f"  {self.stream_prefix}[llm-stream] ❌ Tool error: {error_preview}...")
            # Clean up caches for failed tool calls to prevent leaks
            self._tool_use_id_to_input.pop(tool_use_id, None)
            self._tool_use_id_to_old_content.pop(tool_use_id, None)
            self._tool_use_id_to_name.pop(tool_use_id, None)
        else:
            preview = format_tool_result_preview(tool_name, content)
            print(f"  {self.stream_prefix}[llm-stream] ✅ {preview}...")
            # Render diff for Edit/Write tools
            cached_input = self._tool_use_id_to_input.pop(tool_use_id, None)
            old_content = self._tool_use_id_to_old_content.pop(tool_use_id, None)
            if cached_input and tool_name in ("Edit", "Write"):
                format_tool_diff(tool_name, cached_input, content, old_content=old_content)
            self._tool_use_id_to_name.pop(tool_use_id, None)
        self._last_ended_with_newline = True

    def process_line(self, line: str) -> None:
        """Process a single line of NDJSON output."""
        line = line.strip()
        if not line:
            return

        # ANSI color codes
        GRAY = "\033[90m"      # Bright black (silver/gray)
        ITALIC = "\033[3m"     # Italic
        RESET = "\033[0m"      # Reset

        try:
            data = json.loads(line)
            msg_type = data.get('type', '')

            if msg_type == 'assistant':
                self.message_count += 1
                message = data.get('message', {})
                content = message.get('content', [])
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get('type', '')
                        if item_type == 'text':
                            text = item.get('text', '')
                            if text:
                                self.text_chunks += 1
                                self.total_text_len += len(text)
                                # Stream full text content directly
                                print(text, end='', flush=True)
                                self._last_ended_with_newline = text.endswith('\n')
                        elif item_type == 'thinking':
                            thinking = item.get('thinking', '')
                            if thinking:
                                # Stream thinking content in gray italic
                                print(f"{GRAY}{ITALIC}{thinking}{RESET}", end='', flush=True)
                                self._last_ended_with_newline = thinking.endswith('\n')
                        elif item_type == 'tool_use':
                            name = item.get('name', 'unknown')
                            tool_input = item.get('input', {})
                            tool_use_id = item.get('id', '')
                            self.tool_calls.append(name)
                            if tool_use_id:
                                self._tool_use_id_to_name[tool_use_id] = name
                                if name in ("Edit", "Write"):
                                    self._tool_use_id_to_input[tool_use_id] = tool_input
                                    # For Write tools, cache the current file content for diff
                                    if name == "Write":
                                        file_path = tool_input.get("file_path", "")
                                        if file_path:
                                            try:
                                                self._tool_use_id_to_old_content[tool_use_id] = Path(file_path).read_text(encoding="utf-8")
                                            except (OSError, UnicodeDecodeError):
                                                self._tool_use_id_to_old_content[tool_use_id] = None
                                        else:
                                            self._tool_use_id_to_old_content[tool_use_id] = None
                                    # Evict oldest entries if cache exceeds limit
                                    if len(self._tool_use_id_to_input) > self._MAX_CACHE_SIZE:
                                        oldest = next(iter(self._tool_use_id_to_input))
                                        self._tool_use_id_to_input.pop(oldest, None)
                                        self._tool_use_id_to_old_content.pop(oldest, None)
                                        self._tool_use_id_to_name.pop(oldest, None)
                            # Format and print tool_use preview
                            preview = format_tool_use_preview(name, tool_input)
                            # Only add leading newline if previous output didn't end with one
                            if not self._last_ended_with_newline:
                                print()
                            print(f"  {self.stream_prefix}[llm-stream] 🔧 {preview}...")
                            self._last_ended_with_newline = True

            elif msg_type == 'tool_result':
                # Legacy top-level tool_result format (backward compat)
                result = data.get('result', {})
                tool_use_id = result.get('toolUseId', result.get('tool_use_id', 'unknown'))
                content = result.get('content', '')
                is_error = result.get('isError', result.get('is_error', False))
                self._handle_tool_result(tool_use_id, content, is_error)

            elif msg_type == 'user':
                # CLI actual format: tool_result blocks nested inside user messages
                message = data.get('message', {})
                msg_content = message.get('content', [])
                for item in msg_content:
                    if isinstance(item, dict) and item.get('type') == 'tool_result':
                        tool_use_id = item.get('tool_use_id', 'unknown')
                        content = item.get('content', '')
                        is_error = item.get('is_error', False)
                        self._handle_tool_result(tool_use_id, content, is_error)

            elif msg_type == 'error':
                error_msg = data.get('error', 'Unknown error')
                print(f"  {self.stream_prefix}[llm-stream] ❌ Error: {truncate_preview(str(error_msg))}")
                self._last_ended_with_newline = True

        except json.JSONDecodeError:
            # Not valid JSON, might be a partial line
            pass

    def print_summary(self) -> None:
        """Print final summary of the stream."""
        duration = time.time() - self.start_time
        print(f"  {self.stream_prefix}[llm-stream] ✓ Stream complete: {self.message_count} messages, "
              f"{len(self.tool_calls)} tool calls, {self.total_text_len} chars "
              f"({duration:.1f}s)")
        # Clean up caches to prevent memory leaks on stream interruption
        self._tool_use_id_to_input.clear()
        self._tool_use_id_to_old_content.clear()
        self._tool_use_id_to_name.clear()


class LLMCaller:
    """Manages LLM calls within flow engine steps.

    Wraps agent runners with flow-engine-specific retry, JSON handling,
    chat history, and agent rotation logic.  Maintains a list of available
    agents and rotates to the next one on infrastructure errors (usage
    limit, timeout, hang).  Task-level failures are *not* rotated — those
    are left for the State Machine layer to retry.
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
        retry_mode: str = "continue",
        agents: Optional[List[Dict[str, Any]]] = None,
        stream_prefix: str = '',
    ):
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.flow_id = flow_id
        self.step_id = step_id
        self.step_type = step_type or ""
        self.external_attempt = external_attempt  # Track external retry (e.g., from implement.py)
        self.retry_mode = retry_mode  # 'continue' (resume from breakpoint) or 'retry' (restart)
        self.stream_prefix = stream_prefix

        # Last raw result text from `type: "result"` NDJSON message.
        # Available after call() returns, for callers that need the full
        # LLM output text (not just the parsed JSON).
        self.last_raw_result: Optional[str] = None

        # Agent management
        if agents is not None:
            self._agents = agents
        else:
            from ..config import load_agents
            self._agents = load_agents(self.project_root)
        self._current_agent_index = 0
        self._runner_cache: Dict[str, AgentRunner] = {}

        # Legacy: expose a single _runner for backward compat (uses current agent)
        self._runner = self._get_current_runner()

    def _create_runner(self, agent_config: Dict[str, Any]) -> AgentRunner:
        """Create a Runner instance for the given agent config.

        Args:
            agent_config: Agent dict with name, type, cmd, priority.

        Returns:
            An AgentRunner implementation.
        """
        agent_type = agent_config.get("type", "claude-code")
        if agent_type == "claude-code":
            return ClaudeCodeRunner(
                command={"cmd": agent_config["cmd"], "priority": agent_config.get("priority", 0)},
            )
        # Future: add other agent types here
        raise ValueError(f"Unknown agent type: {agent_type}")

    def _get_current_runner(self) -> AgentRunner:
        """Get (or create and cache) the Runner for the current agent."""
        agent = self._agents[self._current_agent_index]
        cache_key = agent.get("name", agent.get("cmd", str(self._current_agent_index)))
        if cache_key not in self._runner_cache:
            self._runner_cache[cache_key] = self._create_runner(agent)
        return self._runner_cache[cache_key]

    def _rotate_agent(self) -> bool:
        """Rotate to the next agent in the list.

        Returns:
            True if rotation succeeded, False if all agents are exhausted.
        """
        if self._current_agent_index + 1 >= len(self._agents):
            logger.warning("All agents exhausted — no more agents to rotate to")
            return False
        old_name = self._agents[self._current_agent_index].get("name", "?")
        self._current_agent_index += 1
        new_agent = self._agents[self._current_agent_index]
        new_name = new_agent.get("name", "?")
        logger.info(f"Rotating agent: '{old_name}' → '{new_name}' (index {self._current_agent_index})")
        self._runner = self._get_current_runner()
        return True

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

        # Inject extra prompts if set (persistent for loop context, transient for Ctrl+C)
        with _extra_prompt_lock:
            global _extra_prompt
            injected_parts = []
            if _persistent_extra_prompt:
                injected_parts.append(_persistent_extra_prompt)
                logger.info(f"Injected persistent extra prompt: {_persistent_extra_prompt[:80]}")
            if _extra_prompt:
                injected_parts.append(_extra_prompt)
                logger.info(f"Injected transient extra prompt: {_extra_prompt[:80]}")
                _extra_prompt = None  # Consume transient after use
        if injected_parts:
            prompt = f"{prompt}\n\n[Additional user instruction]: {chr(10).join(injected_parts)}"

        # Inject read-only constraint for read-only steps
        from .context_builder import get_read_only_injection
        read_only_constraint = get_read_only_injection(self.step_type)
        if read_only_constraint:
            prompt = f"{prompt}{read_only_constraint}"
            logger.debug(f"Injected read-only constraint for step '{self.step_type}'")

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
        print(f"  {self.stream_prefix}[llm-caller] 🔍 Extracting JSON from output (extract mode)...")

        from .json_extractor import JSONExtractor

        extractor = JSONExtractor(
            project_root=self.project_root,
            timeout=300,  # 5 minutes for large outputs
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

        print(f"  {self.stream_prefix}[llm-caller] ✅ JSON extraction complete")
        return json_str

    def _get_phase1_cache_path(self) -> Optional[Path]:
        """Return the Phase 1 cache file path for this step, or None if no context."""
        if not self.flow_id or not self.step_id:
            return None
        from .chat_history import _history_dir
        return _history_dir(self.project_root, self.flow_id) / f"{self.step_id}_phase1.txt"

    def _call_two_phase(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        json_schema_hint: Optional[str],
    ) -> str:
        """Mode 3: TWO_PHASE - Natural generation + LLM extraction.

        If phase 1 output already contains valid JSON (detected by the
        shared parse_json_response logic), skips phase 2.

        Phase 1 output is cached to disk so that if Phase 2 fails and the
        step is retried (external_attempt > 0), Phase 1 is skipped entirely
        and we go straight to Phase 2 extraction. Cache is cleared when a
        step is restarted from scratch (revision / fix-loop).
        """
        logger.info("Using two-phase JSON extraction")

        cache_path = self._get_phase1_cache_path()

        # On retry: check if Phase 1 was already completed in a previous attempt
        if self.external_attempt > 0 and cache_path and cache_path.exists():
            try:
                phase1_output = cache_path.read_text(encoding="utf-8")
                print(f"  {self.stream_prefix}[llm-caller] ⏩ Phase 1 skipped (cached from previous attempt)")
                logger.info(f"Using cached Phase 1 output ({len(phase1_output)} chars)")
            except OSError as e:
                logger.warning(f"Failed to read Phase 1 cache, re-running Phase 1: {e}")
                phase1_output = None
        else:
            phase1_output = None

        if phase1_output is None:
            # Phase 1: Generate with clean prompt (JSON requirement is in the
            # step's prompt template itself, not added by the caller)
            phase1_output = self._call_with_retry(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                require_json=False,  # No strict JSON constraint
                json_retry_count=0,
            )

            # Persist Phase 1 output so retries can skip it
            if cache_path:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(phase1_output, encoding="utf-8")
                    logger.info(f"Cached Phase 1 output ({len(phase1_output)} chars)")
                except OSError as e:
                    logger.warning(f"Failed to cache Phase 1 output: {e}")

        # Check if phase 1 output already contains valid JSON (skip phase 2)
        if self._contains_valid_json(phase1_output):
            logger.info("Two-phase: phase 1 output contained valid JSON, skipping phase 2")
            print(f"  {self.stream_prefix}[llm-caller] ✅ Phase 1 output contained valid JSON, phase 2 skipped")
            # Step fully done — delete cache
            if cache_path and cache_path.exists():
                try:
                    cache_path.unlink()
                except OSError as e:
                    logger.warning(f"Failed to delete Phase 1 cache: {e}")
            from .utils.json_parser import parse_json_response
            result = parse_json_response(phase1_output)
            return json.dumps(result, ensure_ascii=False, indent=2)

        # Phase 2: Extract JSON via LLM
        print(f"  {self.stream_prefix}[llm-caller] 🔍 Phase 2: Extracting JSON from output...")

        from .json_extractor import JSONExtractor

        extractor = JSONExtractor(
            project_root=self.project_root,
            timeout=300,  # 5 minutes for large outputs
        )

        result = extractor.extract(
            raw_output=phase1_output,
            schema_hint=json_schema_hint,
        )

        if result is None:
            raise LLMCallError(
                "Two-phase JSON extraction failed: Could not extract valid JSON from output"
            )

        # Phase 2 succeeded — delete the Phase 1 cache (step is fully done)
        if cache_path and cache_path.exists():
            try:
                cache_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete Phase 1 cache after success: {e}")

        # Return as JSON string (parse_json_response will handle it)
        json_str = json.dumps(result, ensure_ascii=False, indent=2)

        print(f"  {self.stream_prefix}[llm-caller] ✅ JSON extraction complete")
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
                mode=self.retry_mode,
            )
        except Exception as e:
            logger.warning(f"Failed to get retry context (falling back to original prompt): {e}")
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
        """Internal method to call LLM with retry and agent rotation logic.

        On infrastructure errors (usage limit, timeout, hang), rotates to the
        next agent and retries.  On task-level failures, retries with the same
        agent up to ``max_retries`` times before raising.
        """
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
                    if self.retry_mode == "continue":
                        # In continue mode, the original prompt is already in the history.
                        # Append a short continuation instruction instead of re-prepending the full prompt.
                        effective_prompt = (
                            f"{retry_context}\n"
                            "Continue the task from where you left off based on the conversation history above. "
                            "Do NOT repeat work already completed."
                        )
                    else:
                        # In retry mode, prepend history + original prompt (old behavior)
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
                current_runner = self._get_current_runner()
                current_agent_name = self._agents[self._current_agent_index].get("name", "?")
                logger.debug(
                    f"LLM call internal_attempt {internal_attempt + 1}/{self.max_retries}, "
                    f"external_attempt {self.external_attempt}, agent '{current_agent_name}'"
                )

                if on_output:
                    result = current_runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        cwd=self.project_root,
                        env=env,
                        on_output=on_output,
                    )
                else:
                    set_project_root(self.project_root)
                    stream_tracker = StreamJSONTracker(stream_prefix=self.stream_prefix)

                    def on_stream_output(line: str) -> None:
                        stream_tracker.process_line(line)

                    result = current_runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        cwd=self.project_root,
                        env=env,
                        on_output=on_stream_output,
                    )

                    if result.success:
                        stream_tracker.print_summary()

                # Record the response (whether success, failure, or interrupted)
                self._record_response(result.output or "", self.external_attempt)

                # Extract the type: "result" message's text for callers that
                # need the full LLM output (e.g. discovery multi-turn context)
                self.last_raw_result = self._extract_result_text(result.output or "")

                # If interrupted by Ctrl+C, re-raise after saving partial output
                if isinstance(getattr(result, 'interrupted', False), bool) and result.interrupted:
                    logger.info("LLM call interrupted by user, partial output saved to history")
                    raise KeyboardInterrupt

                if result.success:
                    # When require_json=False, extract text content from NDJSON
                    # so callers get usable text instead of raw stream-json output
                    if not require_json and result.output:
                        extracted = self._extract_text_from_ndjson(result.output)
                        if extracted:
                            result.output = extracted

                    # Check if JSON is required but not received
                    if require_json and json_retry_count < max_json_retries:
                        if not self._contains_valid_json(result.output):
                            print(f"  {self.stream_prefix}[llm-caller] ⚠️  Response is not valid JSON, requesting JSON format (retry {json_retry_count + 1}/{max_json_retries})")
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

                # --- Failure path: check for infrastructure error → rotate agent ---
                infra_error = current_runner.detect_infra_error(
                    result.returncode, result.output or "", ""
                )
                if infra_error != InfraErrorType.NONE:
                    logger.warning(
                        f"Infrastructure error ({infra_error.value}) on agent '{current_agent_name}', "
                        f"attempting agent rotation..."
                    )
                    if self._rotate_agent():
                        # Rotation succeeded — retry immediately with the new agent
                        # (don't count this as a regular internal_attempt)
                        time.sleep(self.retry_delay)
                        continue

                last_error = f"Command '{result.cmd_used}' failed with exit code {result.returncode}"
                logger.warning(f"LLM call failed: {last_error}, internal attempt {internal_attempt + 1}/{self.max_retries}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM call exception: {last_error}, internal attempt {internal_attempt + 1}/{self.max_retries}")

            if internal_attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise LLMCallError(f"LLM call failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _extract_text_from_ndjson(output: str) -> Optional[str]:
        """Extract text content from NDJSON stream output.

        Parses the raw NDJSON output from Claude CLI's stream-json format
        and extracts text from assistant messages. Falls back to None if
        no text content can be extracted (caller should use raw output).

        Args:
            output: Raw NDJSON output string from Claude CLI.

        Returns:
            Extracted text content, or None if no text could be extracted.
        """
        lines = output.strip().split('\n')
        text_parts = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Strip '=== Command: ... ===' prefix line
            if line.startswith('=== Command:') and line.endswith('==='):
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(data, dict):
                continue

            if data.get('type') == 'assistant':
                message = data.get('message', {})
                content = message.get('content', [])
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        text = item.get('text', '')
                        if text:
                            text_parts.append(text)

        if not text_parts:
            return None

        return ''.join(text_parts)

    @staticmethod
    def _extract_result_text(raw_ndjson: str) -> Optional[str]:
        """Extract the result text from a type: "result" NDJSON message.

        This is the LLM's complete final output text — the synthesized
        conclusion after all tool calls and reasoning.

        Args:
            raw_ndjson: Raw NDJSON output string from Claude CLI.

        Returns:
            The result text, or None if no result message found.
        """
        if not raw_ndjson:
            return None
        for line in raw_ndjson.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get('type') == 'result':
                result_text = data.get('result')
                if result_text:
                    return result_text
        return None

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
