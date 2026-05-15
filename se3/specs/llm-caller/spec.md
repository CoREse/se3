<!-- spec-format: v1 -->
# llm-caller Specification

## Purpose

The llm-caller subsystem orchestrates LLM step execution above the `agent-runner` subprocess layer. It owns a list of agents and rotates to the next agent when the current one fails; injects persistent and one-shot "extra prompts" into outgoing calls (used by loop-mode context and Ctrl+C interjections); reconstructs retry context from chat history, deduplicates repeated line blocks in the combined prompt, and applies a post-dedup safety cap that preserves the tail (new prompt) over the head (retry history); supports three JSON-extraction modes (strict / extract / two-phase, with a disk-cached Phase 1 for restart resilience); streams NDJSON output through a tracker that renders tool-use/result previews and inline diffs for `Edit`/`Write`; and exposes a single `LLMCaller.call()` entry point that returns text or extracted JSON.

## Requirements

### Requirement: Agent List Management and Rotation on Failure

The caller maintains an ordered list of agents resolved from configuration. Each agent has a `Runner` instance (cached). On a failed call the caller rotates to the next agent in the list and retries; rotation is attempted on *any* failure (USAGE_LIMIT, TIMEOUT, OTHER are all treated identically — `detect_infra_error` is used only for log labelling).

#### Scenario: Agents resolved from explicit argument
- **WHEN** `LLMCaller` is constructed with a non-empty `agents` list argument
- **THEN** that list is used verbatim
- **AND** no resolution from `se3-config` is performed

#### Scenario: Agents resolved from per-step override
- **WHEN** no `agents` argument is provided and `llm_caller.steps.<step_type>` declares an override
- **THEN** the override list is used as a *hard* override (no fallback to the default chain)
- **AND** an info log records that a per-step override is in effect

#### Scenario: Agents resolved from default chain
- **WHEN** no `agents` argument is provided and no per-step override exists
- **THEN** the default chain returned by `resolve_agents` is used (top-level `agents` / legacy `claude_commands` / built-in default)

#### Scenario: Empty agent list rejected at construction
- **WHEN** the resolved agent list is empty
- **THEN** `LLMCaller.__init__` raises `ValueError` referencing both `llm_caller.defaults`/`llm_caller.steps` config and the explicit `agents` argument

#### Scenario: Rotation on call failure
- **WHEN** a call returns `result.success == False` and another agent exists in the list
- **THEN** the internal `_current_agent_index` advances by one
- **AND** `_get_current_runner` is reused (cached per agent name/cmd) for the next attempt
- **AND** rotation consumes one of the `max_retries` internal-attempt slots after a `retry_delay` sleep
- **AND** an info log records `"Rotating agent: <old> → <new>"`

#### Scenario: Rotation exhausted, tail attempts on last agent
- **WHEN** `_current_agent_index` is already at the last position and a failure occurs
- **THEN** `_rotate_agent` returns `False` and logs `"All agents exhausted"`
- **AND** the remaining `max_retries` attempts run on the last agent without further rotation

#### Scenario: Task-level failure still consumes internal retries
- **WHEN** any failure occurs (the caller does not distinguish infra vs. task-level for rotation purposes)
- **THEN** all failure types attempt rotation; only the State Machine layer decides whether to retry the whole step externally

### Requirement: Extra Prompt Injection (Persistent + Transient)

The caller exposes module-level state for prompts that must be injected into the next outgoing LLM call. Two channels coexist: a `persistent` prompt (used by loop-mode context injection, survives across calls) and a transient prompt (used by Ctrl+C interjection, consumed after one call). Both are protected by a `threading.Lock`.

#### Scenario: Setting a persistent extra prompt
- **WHEN** `set_extra_prompt(prompt, persistent=True)` is called
- **THEN** the value is stored in `_persistent_extra_prompt` under the lock

#### Scenario: Setting a transient extra prompt
- **WHEN** `set_extra_prompt(prompt, persistent=False)` is called (default)
- **THEN** the value is stored in `_extra_prompt` under the lock

#### Scenario: Injection on call
- **WHEN** `call()` runs and either prompt is set
- **THEN** the prompt is appended to the outgoing prompt as `\n\n[Additional user instruction]: <persistent>\n<transient>` (persistent first if both set)
- **AND** an info log records the first 80 chars of each injected prompt

#### Scenario: Transient prompt consumed after one call
- **WHEN** `call()` injects `_extra_prompt` (transient)
- **THEN** `_extra_prompt` is set to `None` immediately within the same locked block
- **AND** `_persistent_extra_prompt` is NOT cleared

#### Scenario: get_extra_prompt is non-consuming
- **WHEN** `get_extra_prompt()` is called
- **THEN** it returns the combined persistent + transient prompt joined by `\n\n`
- **AND** neither variable is mutated

#### Scenario: Clearing prompts
- **WHEN** `clear_extra_prompt()` is called
- **THEN** both transient and persistent variables are set to `None`
- **WHEN** `clear_persistent_extra_prompt()` is called
- **THEN** only the persistent variable is cleared

#### Scenario: Read-only step constraint also injected
- **WHEN** `get_read_only_injection(step_type)` returns a non-empty constraint
- **THEN** it is appended directly to the prompt (without an `[Additional user instruction]:` header) after the extra-prompt injection

### Requirement: JSON Mode Resolution and Dispatch

`call()` accepts both legacy boolean flags (`require_json`, `two_phase_json`) and an explicit `json_mode` string. Resolution priority is: explicit `json_mode` > `two_phase_json` > `require_json` > `"off"`. Unknown explicit modes fall back to `"off"` with a warning.

#### Scenario: Strict mode wraps prompt and retries on bad JSON
- **WHEN** `json_mode="strict"` (or legacy `require_json=True`)
- **THEN** the prompt is wrapped with `CRITICAL: You MUST respond with ONLY valid JSON` headers/footers
- **AND** if the response does not parse, up to `max_json_retries=2` further calls are made with a retry prompt that quotes the first 1500 chars of the bad text content
- **AND** each JSON retry increments `external_attempt` so retry-context injection includes the prior conversation

#### Scenario: Extract mode does not retry, uses LLM extractor
- **WHEN** `json_mode="extract"`
- **THEN** the prompt is wrapped with the same CRITICAL JSON header
- **AND** if the response is not valid JSON, `JSONExtractor.extract(raw_output, schema_hint=…)` is invoked (5-minute timeout)
- **AND** if extraction returns `None`, `LLMCallError` is raised

#### Scenario: Two-phase mode runs clean prompt, then extracts
- **WHEN** `json_mode="two_phase"` (or legacy `two_phase_json=True`)
- **THEN** Phase 1 calls the LLM with the original prompt unmodified (no JSON wrapper)
- **AND** if Phase 1 output already contains valid JSON satisfying `required_keys`, Phase 2 is skipped and the parsed JSON is returned
- **AND** otherwise Phase 2 invokes `JSONExtractor.extract(raw_output, schema_hint, required_keys)`

#### Scenario: Off mode returns raw text extracted from NDJSON
- **WHEN** `json_mode="off"` (or no JSON flags set)
- **THEN** the call runs without JSON wrapping and without JSON retries
- **AND** on success the assistant text content is extracted from NDJSON via `_extract_text_from_ndjson`

### Requirement: Two-Phase Phase-1 Disk Cache

To make Two-Phase JSON resilient against Phase-2 failures and external step retries, Phase 1 output is persisted to `<history_dir>/<step_id>_phase1.txt`.

#### Scenario: Phase 1 cached on first run
- **WHEN** `flow_id` and `step_id` are both set and Phase 1 succeeds
- **THEN** the output is written to `<history_dir(project_root, flow_id)>/<step_id>_phase1.txt`
- **AND** failure to write is logged as a warning but does not fail the call

#### Scenario: Phase 1 skipped on retry when cache exists
- **WHEN** `external_attempt > 0` and the cache file exists
- **THEN** the cached content is read and used directly as Phase 1 output
- **AND** a "Phase 1 skipped (cached)" message is printed

#### Scenario: Cache deleted on full success
- **WHEN** Phase 1 contained valid JSON with required keys (Phase 2 skipped), or Phase 2 succeeds
- **THEN** the cache file is unlinked

#### Scenario: Cache cleared on explicit restart
- **WHEN** `clear_phase1_cache(project_root, flow_id, step_id)` is called (revision / fix-loop restart)
- **THEN** the cache file is removed if present; missing file is a no-op

### Requirement: Retry Context Reconstruction and Injection

On retries (either `external_attempt > 0` or `internal_attempt > 0`), the caller fetches a previous-conversation block from chat history via `format_history_for_retry` and prepends it to the prompt. Mode `"continue"` appends a "continue from where you left off" instruction instead of re-including the original prompt; mode `"retry"` re-prepends the original prompt after the history.

#### Scenario: First call uses prompt as-is
- **WHEN** `external_attempt == 0` and this is the first internal attempt
- **THEN** `effective_prompt` equals the input prompt (no retry context appended)
- **AND** dedup is NOT applied

#### Scenario: Continue mode appends continuation instruction
- **WHEN** retrying with `retry_mode == "continue"` and a non-empty retry context is returned
- **THEN** `effective_prompt` is `<retry_context>\nContinue the task from where you left off based on the conversation history above. Do NOT repeat work already completed.`

#### Scenario: Retry mode re-prepends original prompt
- **WHEN** retrying with `retry_mode == "retry"` and a non-empty retry context
- **THEN** `effective_prompt` is `<retry_context>\n<original_prompt>`

#### Scenario: History fetch failure falls back to original prompt
- **WHEN** `format_history_for_retry` raises any exception
- **THEN** the exception is caught and logged; `effective_prompt` falls back to the original prompt

#### Scenario: Fix-iteration scoping for history
- **WHEN** `fix_iteration` is non-zero
- **THEN** history records are tagged with that value when recording
- **AND** retry-context reconstruction filters to messages from the same iteration so prior fix-loop iterations do not leak into the next prompt

#### Scenario: Original prompt (not effective) is recorded
- **WHEN** `_record_prompt` runs before each subprocess call
- **THEN** it records `original_prompt`, never `effective_prompt`
- **AND** this prevents second-order recursive bloat: the next retry's `format_history_for_retry` would otherwise read the marker+separator block back as a user message and re-embed it inside a fresh retry context

### Requirement: Line-Block Deduplication

Before dispatching a retry prompt, `deduplicate_prompt_lines` scans the prompt for contiguous blocks of ≥ `min_block_lines` (default 3) identical lines that appear more than once. The first occurrence is kept verbatim; subsequent occurrences are replaced by a content-addressed marker line. Dedup is invoked from the caller only on retries.

#### Scenario: Literal escape conversion before dedup
- **WHEN** dedup is about to run on the effective prompt (retry path)
- **THEN** literal `\n` two-character sequences in the prompt (from JSON-encoded tool_result previews) are replaced with real newlines first
- **AND** other escapes (`\t`, `\\`, `\"`) are left intact

#### Scenario: Marker format is content-addressed
- **WHEN** a duplicate block of length `match_len` is detected starting at line `i` with source `src`
- **THEN** the inserted marker is `[DUPLICATED CONTENT: <match_len> lines #<sha256[:8]>, from "<first_line_strip[:80]>" to "<last_line_strip[:80]>"]`
- **AND** no line-number reference is included (line numbers become stale across retries)

#### Scenario: Previously-inserted markers are not re-deduped
- **WHEN** a line starts with `[DUPLICATED CONTENT:`
- **THEN** the dedup loop skips it as a candidate window start

#### Scenario: Blank-only blocks excluded
- **WHEN** every line in the candidate fingerprint window is empty (after `.strip()`)
- **THEN** the block is not deduplicated

#### Scenario: Block extension beyond min_block_lines
- **WHEN** a 3-line fingerprint matches a registered source
- **THEN** the match is extended line-by-line while corresponding source/duplicate lines are equal, neither side has been replaced, and `src + match_len < i` (source range may not overlap the duplicate range)

#### Scenario: Source-validity check on re-registration
- **WHEN** a fingerprint match resolves to a source whose lines have already been replaced
- **THEN** the dictionary entry is overwritten with the current position and no replacement is emitted this iteration

#### Scenario: Dedup failure is non-fatal
- **WHEN** `deduplicate_prompt_lines` raises any exception
- **THEN** the caller logs a warning with traceback and falls back to the un-deduplicated prompt
- **AND** the post-dedup safety cap is still applied

### Requirement: Post-Dedup Safety Cap

After dedup, `_post_dedup_safety_cap` enforces an upper bound (default `500_000` chars, overridable by env var `SE3_POST_DEDUP_SAFETY_LIMIT`) on the effective prompt. It truncates the *head* of the retry-history section (between marker and separator) while preserving the *tail* (new prompt) in full. The cap relies on the invariant that `format_history_for_retry` emits exactly one `RETRY_HISTORY_MARKER` at position 0 and exactly one `RETRY_HISTORY_SEPARATOR` per retry-context block.

#### Scenario: Under-limit prompts pass through
- **WHEN** `len(effective_prompt) <= limit`
- **THEN** the prompt is returned unchanged

#### Scenario: No marker → no truncation
- **WHEN** the prompt exceeds the limit but contains no `RETRY_HISTORY_MARKER`
- **THEN** the prompt is returned unchanged (first-call / non-retry path)

#### Scenario: Marker must be at position 0
- **WHEN** the marker is found at position > 0
- **THEN** an `AssertionError` is raised (a stray prefix would be silently discarded by the rebuild — assert to fail loud on wiring changes)

#### Scenario: Separator located with rfind
- **WHEN** the prompt contains multiple `RETRY_HISTORY_SEPARATOR` occurrences (retry-of-retry chain where a prior `effective_prompt` containing an inner anchor was replayed verbatim)
- **THEN** the cap uses `rfind` so the OUTER separator (always last) anchors the tail

#### Scenario: Separator missing → cannot truncate
- **WHEN** the marker is present but no separator appears after it
- **THEN** a warning is logged and the prompt is returned unchanged

#### Scenario: Header + tail rebuild preserves invariant
- **WHEN** truncation runs
- **THEN** the output is `<header>\n[... retry history truncated (head) to stay under safety limit ...]\n<kept_body><tail>` where `tail` starts at the separator
- **AND** the rebuilt output contains exactly one marker and exactly one separator so the cap can re-anchor on a future retry

#### Scenario: Tail-only fallback when tail exceeds limit
- **WHEN** `len(header) + len(tail) >= limit` (negative or zero budget for `kept_body`)
- **THEN** `kept_body` is dropped entirely and `header + tail` is returned with a distinct warning noting the output still exceeds `limit`

#### Scenario: Small-budget fallback drops kept_body
- **WHEN** budget is positive but smaller than the separator length
- **THEN** `kept_body` is skipped to avoid emitting a partial-line fragment; only `header + tail` is returned

#### Scenario: Sliced body rounds to next newline
- **WHEN** budget < `len(history_body)` and budget ≥ separator length
- **THEN** the last `budget` characters of `history_body` are taken, then advanced past the first `\n` so the kept body begins on a clean line boundary (avoiding a half `[User Prompt]:` header)
- **AND** if no newline is found in the slice, the raw slice is kept as-is

#### Scenario: Safety limit configurable via env var
- **WHEN** `SE3_POST_DEDUP_SAFETY_LIMIT` is set to a positive integer at import time
- **THEN** that value replaces the default `500_000`
- **AND** invalid (non-integer, zero, negative) values fall back to the default

### Requirement: Streaming NDJSON Output Display

When no explicit `on_output` callback is given, the caller installs a `StreamJSONTracker` that consumes each NDJSON line from the subprocess and prints a human-readable summary with tool-use previews and inline diffs.

#### Scenario: Text and thinking streamed inline
- **WHEN** an `assistant` message contains a `text` or `thinking` content item
- **THEN** text is printed directly (flush=True)
- **AND** `thinking` is printed in gray italic (ANSI `\033[90m\033[3m...\033[0m`)
- **AND** the tracker remembers whether the last chunk ended with a newline so the following tool preview adds a leading newline only when needed

#### Scenario: Tool use rendered with preview
- **WHEN** an `assistant` message contains a `tool_use` content item
- **THEN** the tool name and a `format_tool_use_preview`-formatted input are printed as `  <stream_prefix>[llm-stream] 🔧 <preview>...`
- **AND** the tool-use id is recorded in `_tool_use_id_to_name`

#### Scenario: Edit/Write inputs cached for diff
- **WHEN** the tool is `Edit` or `Write`
- **THEN** the input dict is cached under the tool-use id
- **AND** for `Write`, the current contents of `file_path` are read into `_tool_use_id_to_old_content` (None on OS or decode error or missing path)
- **AND** if the cache exceeds `_MAX_CACHE_SIZE = 100`, the oldest entry is evicted

#### Scenario: Tool result rendered with preview and diff
- **WHEN** a `tool_result` arrives (legacy top-level or nested inside a `user` message)
- **THEN** if `is_error` is true, an error preview is printed and the caches for that id are popped
- **AND** if successful, `format_tool_result_preview` is printed and, for cached `Edit`/`Write` ids, `format_tool_diff` is rendered using the cached input and old content

#### Scenario: Stream-level error printed
- **WHEN** an NDJSON line has `type == "error"`
- **THEN** a single `❌ Error: <truncated>` line is printed

#### Scenario: Malformed JSON tolerated
- **WHEN** a streamed line is not valid JSON
- **THEN** `json.JSONDecodeError` is caught and the line is silently skipped

#### Scenario: Final summary and cache cleanup
- **WHEN** `print_summary` is called on success
- **THEN** a summary line shows message count, tool-call count, total text chars, and elapsed seconds
- **AND** all three id→cache dicts are cleared to prevent leaks on stream interruption

### Requirement: Result Text Extraction Modes

The caller exposes two post-call extractors used by different consumers.

#### Scenario: Off-mode text extraction from NDJSON
- **WHEN** `_call_with_retry` succeeds with `require_json=False` and `result.output` is non-empty
- **THEN** `_extract_text_from_ndjson` concatenates all `assistant`/`text` chunks from the stream and replaces `result.output` with that text if non-empty
- **AND** lines starting with `=== Command:` and ending with `===` are skipped
- **AND** non-JSON lines are skipped

#### Scenario: Final result text available via last_raw_result
- **WHEN** any call returns
- **THEN** `self.last_raw_result` is set to the `result` field of the NDJSON line with `type == "result"` (the LLM's synthesized final output), or `None` if no such line exists

#### Scenario: Validity check via shared parser
- **WHEN** `_contains_valid_json(output)` is called
- **THEN** it delegates to `parse_json_response` (the same parser used elsewhere) and returns `True` iff the parser returned a non-`None` value

### Requirement: Subprocess Invocation and History Recording

Each call invokes the current agent's `Runner.run_with_monitor` with stream-json output, no wall-time limit, and a 1800-second (30-minute) inactivity timeout. Prompts and responses are recorded to chat history (whether the call succeeded, failed, or was interrupted) if `flow_id` and `step_id` are set.

#### Scenario: Args composed for subprocess
- **WHEN** the call is dispatched
- **THEN** args are `["--output-format", "stream-json", "--verbose", "-p", effective_prompt]`
- **AND** existing `context_files` are appended as `--file <path>` pairs

#### Scenario: CLAUDECODE env var stripped
- **WHEN** the subprocess env is built
- **THEN** `CLAUDECODE` is removed from the copied environment

#### Scenario: Inactivity timeout fixed at 30 minutes
- **WHEN** `run_with_monitor` is invoked
- **THEN** `wall_timeout=None` and `inactivity_timeout=1800` are passed regardless of the deprecated `timeout` argument

#### Scenario: Prompt recorded before each attempt
- **WHEN** an internal attempt begins (after retry-context injection but before dispatch)
- **THEN** `_record_prompt(original_prompt, external_attempt)` is called
- **AND** failures inside `record_prompt` are caught and debug-logged (do not fail the call)

#### Scenario: Response always recorded
- **WHEN** `run_with_monitor` returns (success, failure, or interrupted)
- **THEN** `_record_response(result.output or "", external_attempt)` is called
- **AND** failures inside `record_response` are caught and debug-logged

#### Scenario: Ctrl+C re-raised after partial save
- **WHEN** `result.interrupted` is `True`
- **THEN** an info log records partial-output save, and `KeyboardInterrupt` is raised
- **AND** chat history already has the partial response from the preceding `_record_response`

#### Scenario: JSON retry recorded with distinct attempt number
- **WHEN** a strict-mode JSON retry fires
- **THEN** the new JSON-retry prompt is recorded with attempt = `external_attempt * 100 + json_retry_count` to distinguish it from normal attempts in history

### Requirement: Final Failure Behavior

#### Scenario: All retries exhausted
- **WHEN** `max_retries` internal attempts have all failed
- **THEN** `LLMCallError(f"LLM call failed after {max_retries} attempts: {last_error}")` is raised
- **AND** between attempts the caller sleeps `retry_delay` seconds

#### Scenario: Exception path captures last_error
- **WHEN** any exception (other than `KeyboardInterrupt`) is raised inside the attempt loop
- **THEN** `str(e)` is stored in `last_error`, a warning is logged, and the loop continues until `max_retries` is exhausted

### Requirement: Stream-JSON Formatting Helper

The caller exposes a static helper `_format_as_stream_json` that wraps a plain text string in a single NDJSON line compatible with the stream-json subprocess output format. The helper is provided for callers that need to synthesize stream-json content from a plain text payload (e.g., bridging non-streaming sources into code paths that expect NDJSON).

#### Scenario: Plain text wrapped as assistant/text NDJSON line
- **WHEN** `LLMCaller._format_as_stream_json(content)` is called with a string `content`
- **THEN** it returns a single JSON-serialized line shaped as `{"type": "assistant", "message": {"content": [{"type": "text", "text": content}]}}`
- **AND** the JSON is serialized with `ensure_ascii=False` so non-ASCII content is preserved verbatim

#### Scenario: Static method, no instance state touched
- **WHEN** `_format_as_stream_json` is invoked
- **THEN** it is a `@staticmethod` and does not read or mutate any `LLMCaller` instance attributes
- **AND** it may be called without constructing an `LLMCaller`