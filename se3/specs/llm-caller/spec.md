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

#### Scenario: Sync read-only pseudo-steps receive the prompt-level constraint
- **GIVEN** the step type is `sync_scan` or `sync_analyze` (sync-engine read-only pseudo-steps that are absent from the `se3 run` STEP_POOL/StepType)
- **WHEN** the read-only decision is resolved via `is_step_read_only(step_type)` — the single source of truth that consults STEP_POOL's `read_only` attribute and additionally classifies `sync_scan`/`sync_analyze` as read-only
- **THEN** `get_read_only_injection(step_type)` returns the non-empty constraint and it is injected into the prompt
- **AND** `sync_resolve` is deliberately excluded (its Way-A update path edits `se3/specs/<name>/spec.md` in place via `Edit`), so it is classified writable and receives no constraint

### Requirement: Tool-Layer Read-Only Enforcement

Because read-only sub-agents run without a permission gate (`--dangerously-skip-permissions` for Claude, the bypass flag for Codex), the prompt-level read-only constraint alone cannot reliably stop a sub-agent from writing files. For read-only steps the caller therefore ALSO enforces read-only at the CLI layer. The caller does this by passing the read-only *intent* through `build_call_args`, NOT by appending CLI flags itself; each runner then translates that intent into its own enforcement mechanism: `ClaudeCodeRunner` forbids the write tools (`--disallowedTools`, a tool-level restriction) while keeping the read tools available, and `CodexRunner` selects `--sandbox read-only` (an OS-level restriction, stronger than the tool-level form). This makes se3 itself the only writer for sync-discovered specs and prevents stray files from being created in a managed project.

The read-only decision uses the same `is_step_read_only(step_type)` classifier as the prompt-level injection — it remains the single source of truth — so the prompt and tool layers can never disagree about which steps are read-only. The classifier output is the intent the caller hands to the runner; the runner owns the translation.

#### Scenario: Read-only step passes read-only intent to the runner
- **GIVEN** a read-only step (any STEP_POOL step with `read_only=True`, or the sync pseudo-steps `sync_scan` / `sync_analyze`)
- **WHEN** the caller builds the agent-runner args via `build_call_args(prompt, read_only=True, context_files)`
- **THEN** the runner translates the read-only intent into its own enforcement: `ClaudeCodeRunner` appends `--disallowedTools Write Edit NotebookEdit AskUserQuestion` (read tools `Read` / `Grep` / `Glob` / `Bash` remain available), while `CodexRunner` adds `--sandbox read-only`

#### Scenario: Read-only step disallows the write tools
- **GIVEN** a read-only step (any STEP_POOL step with `read_only=True`, or the sync pseudo-steps `sync_scan` / `sync_analyze`)
- **WHEN** the caller builds the agent-runner args
- **THEN** it appends `--disallowedTools Write Edit NotebookEdit AskUserQuestion` to the args
- **AND** the read tools `Read`, `Grep`, `Glob`, and `Bash` are NOT disallowed and remain available

#### Scenario: Writable steps are unaffected by the tool-layer restriction
- **GIVEN** a writable step (e.g., `implement`, `update_spec`, or the sync update path `sync_resolve`)
- **WHEN** the caller builds the agent-runner args via `build_call_args(prompt, read_only=False, context_files)`
- **THEN** no read-only restriction is added by either runner, so the step retains its full default tool set
- **AND** `sync_resolve` in particular keeps `Edit` so its Way-A path can modify `se3/specs/<name>/spec.md` in place

### Requirement: JSON Mode Resolution and Dispatch

`call()` accepts both legacy boolean flags (`require_json`, `two_phase_json`) and an explicit `json_mode` string. Resolution priority is: explicit `json_mode` > `two_phase_json` > `require_json` > `"off"`. Unknown explicit modes fall back to `"off"` with a warning.

#### Scenario: Strict mode wraps prompt and retries on bad JSON
- **WHEN** `json_mode="strict"` (or legacy `require_json=True`)
- **THEN** the prompt is wrapped with `CRITICAL: You MUST respond with ONLY valid JSON` headers/footers
- **AND** if the response does not parse, up to `max_json_retries=2` further calls are made with a retry prompt that quotes the first 1500 chars of the bad text content
- **AND** each JSON retry increments `external_attempt` so retry-context injection includes the prior conversation

#### Scenario: Extract mode does not retry, uses LLM extractor on fast-path miss
- **WHEN** `json_mode="extract"`
- **THEN** the prompt is wrapped with the same CRITICAL JSON header
- **AND** the raw output is first run through a lenient fast-path parser (`_lenient_parse_extract`) that tolerates bare JSON, markdown-fenced JSON, and narrative-prose-wrapped JSON, and aggregates multi-line NDJSON stream output via `parse_json_response`
- **AND** if the fast-path returns a value, it is re-serialized via `json.dumps(result, ensure_ascii=False, indent=2)` and returned (byte-identical formatting to TWO_PHASE) so downstream strict `json.loads` consumers can read it directly
- **AND** otherwise `JSONExtractor.extract(raw_output, schema_hint, required_keys)` is invoked (5-minute timeout) and its dict result is likewise serialized via `json.dumps(...)` and returned
- **AND** if extraction also returns `None`, `LLMCallError` is raised

#### Scenario: Extract mode honors required_keys for dict contracts
- **GIVEN** `json_mode="extract"` and a non-empty `required_keys` list (a dict-only contract — `required_keys` is meaningless for list outputs)
- **WHEN** the fast-path parses the output as a dict containing all `required_keys`
- **THEN** the dict is serialized via `json.dumps(...)` and returned
- **WHEN** the fast-path parses the output as a dict that is missing any required key
- **THEN** the fast-path returns `None` and the call falls back to `JSONExtractor.extract(..., required_keys=required_keys)` for Phase-2 LLM re-extraction
- **WHEN** the fast-path parses the output as a list while `required_keys` is non-empty
- **THEN** the contract is treated as mismatched: the fast-path returns `None` and the Phase-2 extractor runs (conservative fallback)

#### Scenario: Extract mode without required_keys accepts dict or list
- **GIVEN** `json_mode="extract"` and `required_keys` is `None` or empty
- **WHEN** the fast-path parses the output as a dict OR as a list (e.g., a top-level JSON array from `sync_discovery`'s LLM response shaped as a narrative + bracketed list)
- **THEN** the parsed value is serialized via `json.dumps(...)` and returned, so a downstream `strict json.loads(response)` on the returned string yields the same list/dict without needing additional fence-stripping or extraction
- **AND** the `LLMCaller.call(json_mode="extract", ...)` dispatch forwards `required_keys` to `_call_extract` symmetrically with the way TWO_PHASE forwards them, so both modes share the same `required_keys` contract

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

The tracker has **two output channels that MUST be formatted independently**:

1. The **CLI terminal stdout** — emoji-prefixed lines (`🔧 <preview>`, `✅ <preview>`, `❌ ...`) written via `print(...)`. This is the human-readable terminal stream and its byte sequence MUST remain stable across changes to the web-progress channel.
2. The **web/jsonl progress channel** — the `content` string passed to `_emit_progress` and persisted in `stream_progress` records that the daemon forwards to the running-flow console. This channel MUST emit tool events as **bracket-marker strings** (`[<tool_name>: <detail>]`, `[<tool_name> ✓ ...]`, `[<tool_name> ✗ <error_preview>]` / `[Tool error: <preview>]`) so the marker text is byte-identical to what `chat_history.extract_assistant_text` writes in the final assistant turn. This is what enables the running-flow console's `TOOL_MARKER_RE` / `renderToolMarkers` to render the same `.tool-marker` boxes during streaming as in the final settled view (see the `running-flow-console` *Live Per-Turn Stream Accumulation* and *Tool Call Chip State Machine* requirements).

The bracket-marker convention covers all three tool-event kinds — `tool_use`, successful `tool_result`, and `tool_error` (both the `is_error` branch of a tool result and a stream-level `error` line) — so the live and final views share a single marker grammar with no second emoji-only parsing path on the frontend.

**Per-chip extension fields (single-chip protocol).** Beyond the bracket-marker `content` string, every `tool_use` / `tool_result` / `tool_error` event the tracker emits to the web/jsonl progress channel MUST also carry three structured fields so the frontend can collapse the two physical events (use + result) of a single tool call into one progressively-upgraded chip:

- `tool_use_id: str` — the originating Anthropic `tool_use_id`; this is the chip-identity key the frontend uses to pair a later `tool_result` against the earlier `tool_use`. Stream-level `error` lines (no originating tool call) MAY omit this field.
- `is_error: bool` — present on terminal (result) events only; `true` for a result whose Anthropic `is_error` flag is set or for a stream-level `error`, `false` for a successful result. Absent (or `None`) on the in-flight `tool_use` event.
- `tool_detail: dict | None` — the structured detail payload (produced by `tool_formatters.build_tool_detail_payload`, keyed by a `kind` field such as `edit_diff` / `write_full` / `write_diff` / `read_text` / `bash_output` / `grep_matches` / `glob_matches` / `text`); present on terminal events only. Absent (or `None`) on the in-flight `tool_use` event so the in-flight chip carries the header only.

These fields ride alongside `content` inside each `stream_progress` record's payload (e.g. `chat_history.record_stream_progress` accepts the matching keyword-only `tool_use_id` / `is_error` / `tool_detail` parameters, defaulting to `None`). When all three default to `None` the persisted jsonl record's key set MUST stay byte-identical to the pre-extension schema, so legacy readers and the existing CLI history view continue to work unchanged.

**Single-record-per-phase rule.** Each tool call produces **exactly two** `stream_progress` records on the web/jsonl channel — one in-flight record at `tool_use` time and one terminal record at `tool_result` time — and **no more**. In particular, the tracker MUST NOT emit a separate result-preview chip in addition to the terminal record: the terminal event's header (computed by `tool_formatters.format_tool_chip_header(...)` from both the cached `tool_use` input and the arriving `tool_result` payload) and its `tool_detail` payload together carry every piece of information the frontend chip needs, so any third `[<preview>]` chip would be a duplicate that the frontend would render as a zombie sibling. The CLI terminal stdout is unaffected by this rule — its emoji-prefixed lines (`🔧` for use, `✅` / `❌` for result) continue to be printed byte-for-byte as before.

#### Scenario: Text and thinking streamed inline
- **WHEN** an `assistant` message contains a `text` or `thinking` content item
- **THEN** text is printed directly (flush=True)
- **AND** `thinking` is printed in gray italic (ANSI `\033[90m\033[3m...\033[0m`)
- **AND** the tracker remembers whether the last chunk ended with a newline so the following tool preview adds a leading newline only when needed

#### Scenario: Tool use rendered with preview
- **WHEN** an `assistant` message contains a `tool_use` content item
- **THEN** the tool name and a `format_tool_use_preview`-formatted input are printed to stdout as `  <stream_prefix>[llm-stream] 🔧 <preview>...`
- **AND** the same event is recorded into the web/jsonl progress channel with content `[<preview>]` (bracket-marker form), not `🔧 <preview>`, so the running-flow console parses it as a tool marker
- **AND** the `stream_progress` record carries the originating `tool_use_id` (the chip-identity key) and omits / defaults `is_error` and `tool_detail` to `None`, so the frontend creates an in-flight chip header-only
- **AND** the tool-use id is recorded in `_tool_use_id_to_name`

#### Scenario: Edit/Write inputs cached for diff
- **WHEN** the tool is `Edit` or `Write`
- **THEN** the input dict is cached under the tool-use id
- **AND** for `Write`, the current contents of `file_path` are read into `_tool_use_id_to_old_content` (None on OS or decode error or missing path)
- **AND** if the cache exceeds `_MAX_CACHE_SIZE = 100`, the oldest entry is evicted

#### Scenario: Tool result rendered with preview and diff
- **WHEN** a `tool_result` arrives (legacy top-level or nested inside a `user` message)
- **THEN** if `is_error` is true, an error preview is printed to stdout (emoji-prefixed, byte-identical to prior behavior) and the caches for that id are popped, and the web/jsonl progress channel emits **exactly one** terminal `stream_progress` record whose `content` is the failure bracket marker `[<tool_name> ✗ <error_preview>]` (falling back to `[Tool error: <error_preview>]` when the tool name is unknown), carrying `tool_use_id = <id>`, `is_error = true`, and a `tool_detail` payload describing the error
- **AND** if successful, `format_tool_result_preview` is printed to stdout and the web/jsonl progress channel emits **exactly one** terminal `stream_progress` record whose `content` is the merged success header produced by `format_tool_chip_header(...)` (combining the cached `tool_use` input summary and the arriving result summary, e.g. `[Read ✓ path · 87 lines]`), carrying `tool_use_id = <id>`, `is_error = false`, and the structured `tool_detail` payload returned by `tool_formatters.build_tool_detail_payload(...)`; the tracker MUST NOT emit a separate `[<preview>]` result chip in addition to this terminal record, and, for cached `Edit`/`Write` ids, `format_tool_diff` is rendered to stdout using the cached input and old content

#### Scenario: Stream-level error printed
- **WHEN** an NDJSON line has `type == "error"`
- **THEN** a single `❌ Error: <truncated>` line is printed to stdout
- **AND** the web/jsonl progress channel records the event as `[Tool error: <truncated>]` so the running-flow console renders it as a `.tool-marker` consistent with the final-state form

#### Scenario: Web/jsonl progress channel uses bracket markers matching the final assistant turn
- **GIVEN** a streaming call producing one `tool_use`, one successful `tool_result`, and one `tool_error` (or stream-level `error`)
- **WHEN** the `stream_progress` records produced by the tracker are inspected
- **THEN** every tool-event `content` string begins with `[` and ends with `]` — using the same bracket-marker grammar `chat_history.extract_assistant_text` writes for the final assistant turn — never the emoji-prefixed CLI form
- **AND** the CLI stdout for the same call still contains the emoji-prefixed lines (`🔧`, `✅`, `❌`) byte-identical to the pre-change behavior, so terminal users see no regression

#### Scenario: In-flight tool_use emits a header-only chip record
- **GIVEN** a streaming call that has just dispatched a `Read` `tool_use` (no `tool_result` yet)
- **WHEN** the corresponding `stream_progress` record is inspected
- **THEN** the record's `content` is the in-flight bracket marker `[Read: <path>:<offset>-<end>]` (the `format_tool_chip_in_flight_header` form computed from the use input alone)
- **AND** the record's `tool_use_id` equals the originating Anthropic `tool_use_id`, and `is_error` / `tool_detail` are absent (or `None`)
- **AND** the tracker has emitted **only one** record for this tool call so far (no terminal record yet), so the frontend chip state machine creates a single in-flight chip keyed by that `tool_use_id`

#### Scenario: tool_result success emits a single terminal upgrade record
- **GIVEN** the same call as above, where the matching `Read` `tool_result` now arrives without `is_error`
- **WHEN** the `stream_progress` records produced by the tracker are inspected after the `tool_result`
- **THEN** the tracker has emitted **exactly one** additional record (the terminal record) — not two — so the call's two physical events produce two `stream_progress` records total
- **AND** the terminal record's `content` is the merged success header `[Read ✓ <path> · <N> lines]` from `format_tool_chip_header(...)`, its `tool_use_id` matches the in-flight record's, its `is_error` is `false`, and its `tool_detail` is the structured payload from `build_tool_detail_payload(...)` (e.g. `{"kind": "read_text", ...}`)
- **AND** no separate `[<preview>]` chip is emitted in addition to the terminal record, so the frontend can upgrade the existing in-flight chip in place without producing a sibling zombie chip

#### Scenario: tool_result failure emits a single terminal failure record with detail
- **GIVEN** a `Bash` `tool_use` followed by a matching `tool_result` whose `is_error` flag is `true` (or, equivalently, a stream-level `error` line that pairs with an open in-flight call)
- **WHEN** the `stream_progress` records produced by the tracker are inspected after the failure
- **THEN** the tracker has emitted **exactly one** terminal record for the failure (no extra preview chip), and its `content` is the failure bracket marker `[Bash ✗ <error_preview>]` (or `[Tool error: <preview>]` when the tool name is unknown)
- **AND** the terminal record carries the matching `tool_use_id`, `is_error = true`, and a `tool_detail` payload that describes the error so the frontend chip can default the detail panel to expanded for failures (per the `running-flow-console` *Tool Call Chip State Machine* requirement)
- **AND** the CLI terminal stdout for the same failure still prints the emoji-prefixed `❌` line byte-for-byte as before

#### Scenario: Malformed JSON tolerated
- **WHEN** a streamed line is not valid JSON
- **THEN** `json.JSONDecodeError` is caught and the line is silently skipped

#### Scenario: Final summary and cache cleanup
- **WHEN** `print_summary` is called on success
- **THEN** a summary line shows message count, tool-call count, total text chars, and elapsed seconds
- **AND** all three id→cache dicts are cleared to prevent leaks on stream interruption

### Requirement: Touched-Files Capture for Dependency Tracking

The `StreamJSONTracker` SHALL capture, gcc `-M`-style, the set of file paths touched by `Read`, `Grep`, and `Glob` tool calls during a `call()` invocation, normalized to project-relative paths. This lets the `se3 sync` engine discover each spec's dependency file set from the analyzer agent's actual execution, rather than relying on the LLM to self-report dependencies.

When a streamed `assistant` message contains a `tool_use` content item:
- If the tool name is `Read`, the input's `file_path` is recorded.
- If the tool name is `Grep` or `Glob`, the input's `path` (or equivalent search path) is recorded.
- Each recorded path is normalized to a project-relative path before being added to the tracker's accumulating set.

The tracker exposes the accumulated set via a `touched_files` property; `LLMCaller` resets the set at the start of each `call()` and exposes the most recent call's result via a `last_touched_files` property for the analyzer to consume.

#### Scenario: Read/Grep/Glob tool calls recorded as touched files
- **WHEN** an `assistant` message contains a `tool_use` item for `Read`, `Grep`, or `Glob`
- **THEN** the file path / search path from the tool input is normalized to a project-relative path and added to the tracker's `touched_files` set
- **AND** `tool_use` items for other tools (e.g. `Edit`, `Write`, `Bash`) do not add entries to the touched-files set

#### Scenario: touched-files set exposed per call
- **WHEN** a `call()` invocation completes
- **THEN** `LLMCaller.last_touched_files` returns the set of project-relative paths touched by that call's `Read`/`Grep`/`Glob` tool calls
- **AND** the set is reset at the start of the next `call()` so it never leaks across invocations

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

### Requirement: Result Usage and Cost Capture

Beyond the synthesized final `result` text (see *Result Text Extraction Modes*), the `StreamJSONTracker` SHALL also capture the token-usage and cost telemetry carried on the stream's `type == "result"` line, which the prior implementation read for its `result` field and then discarded. This lets the flow engine aggregate per-step and per-session token usage and cost (see the `flow-engine` *State Tracking Fields and Helper API* and *Step Output Renderer Registry* requirements) without changing the agent-runner subprocess contract.

The captured fields are the four usage token counts — `usage.input_tokens`, `usage.output_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens` — and the top-level `total_cost_usd`. The tracker tolerates both the nested `message.usage` shape and a flat top-level `usage` object, defaults any missing field to `0`, and swallows any parsing exception so usage capture is strictly best-effort and never disturbs the main call path.

Because a single step may issue several subprocess calls (retry, agent rotation, and the JSON two-phase extractor each spawn their own subprocess), `_call_with_retry` SHALL fold the tracker's captured usage into the current step-scoped accumulator after **every** subprocess returns — on both the success and the failure paths — so the per-step total is the sum across all of that step's calls. The accumulator is the contextvar-scoped one owned by `token_usage.add_call_usage` (see the `flow-engine` requirements); the caller does not retain or expose per-call usage detail.

#### Scenario: Usage and cost captured from the result line
- **WHEN** the stream emits a `type == "result"` line carrying `usage.input_tokens` / `usage.output_tokens` / `usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens` and a top-level `total_cost_usd`
- **THEN** the tracker records all four token counts and the cost, exposed via its read-only `usage` property
- **AND** both the nested `message.usage` form and the flat top-level `usage` form are accepted

#### Scenario: Missing usage fields default to zero
- **WHEN** the `type == "result"` line omits one or more usage fields (or omits `total_cost_usd`)
- **THEN** each absent field is recorded as `0` rather than raising
- **AND** a malformed or unparseable usage payload is swallowed silently, leaving the call result unaffected

#### Scenario: Per-step usage merges across retries, rotations, and two-phase extraction
- **WHEN** a step issues multiple subprocess calls (internal retry, agent rotation, or the two-phase JSON extractor)
- **THEN** `_call_with_retry` folds each call's `stream_tracker.usage` into the current step-scoped accumulator via `token_usage.add_call_usage`, on both success and failure paths
- **AND** the step's reported usage is the accumulated sum across all those calls, with no single-call breakdown retained

### Requirement: Subprocess Invocation and History Recording

Each call invokes the current agent's `Runner.run_with_monitor` with stream-json output, no wall-time limit, and a 1800-second (30-minute) inactivity timeout. Prompts and responses are recorded to chat history (whether the call succeeded, failed, or was interrupted) if `flow_id` and `step_id` are set.

#### Scenario: Args composed for subprocess
- **WHEN** the call is dispatched
- **THEN** `LLMCaller` does NOT assemble any LLM-specific CLI flags itself; it calls `current_runner.build_call_args(effective_prompt, read_only, context_files)` and uses the returned arg list verbatim
- **AND** the read-only / writable intent and the `context_files` list are passed as intent, leaving each runner to translate them into its own flags (`ClaudeCodeRunner` emits `--output-format stream-json --verbose -p <prompt>` with `--file <path>` pairs for context files; `CodexRunner` emits its `codex exec --json` form with context files inlined)

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

#### Scenario: Per-call token usage parsed into the assistant record
- **GIVEN** `record_response` is writing an `assistant` chat-history record whose
  `raw_ndjson` contains a `type == "result"` line carrying `usage.input_tokens` /
  `usage.output_tokens` / `usage.cache_creation_input_tokens` /
  `usage.cache_read_input_tokens` and a top-level `total_cost_usd`
- **WHEN** the record is persisted
- **THEN** `record_response` parses that result line's usage (via the shared
  `parse_usage_from_ndjson(raw_ndjson)` helper, defaulting any missing field to
  `0` and swallowing any parse exception) and stores it on the record's optional
  `token_usage` field, which is included in the record's serialization
- **AND** the parsed per-call `token_usage` is the per-round increment the
  running-flow console renders as an interactive-turn usage footnote (see the
  `running-flow-console` *Per-Step Report Cards* requirement)
- **AND** a legacy `assistant` record written before this field existed (no
  `token_usage` key) still deserializes without error, so the change is backward
  compatible

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