<!-- spec-format: v1 -->
# agent-runner-infrastructure Specification

## Purpose

The agent-runner-infrastructure subsystem is the subprocess execution layer that drives agent CLIs for every LLM call made by SE3. It defines an abstract `AgentRunner` interface (with a `RunResult` dataclass and `InfraErrorType` taxonomy) plus two concrete adapters: a `ClaudeCodeRunner` (Claude Code CLI) and a `CodexRunner` (OpenAI Codex CLI). The Claude adapter handles process spawning, real-time output streaming, stdout/stderr capture, hang detection via psutil resource probes, wall-clock and inactivity timeout enforcement, usage-limit keyword scanning, a large-prompt rerouting path that moves oversized `-p`/`--prompt` values to stdin to avoid Linux `execve()` `E2BIG` failures, and a conservative CLI-subprocess confirmation-prompt capture path that surfaces interactive child prompts (e.g. `按 1 确定` / `Press 1 to confirm`) to the engine via an optional `on_confirm` callback. The `CodexRunner` wraps a single `codex exec --json` command and normalizes Codex's JSONL event stream into Claude-compatible stream-json NDJSON so all upstream consumers are runner-agnostic. Each runner wraps exactly one CLI command per instance; multi-command rotation/fallback is owned by `LLMCaller` upstream.

LLM-agnostic concerns (the stream-json NDJSON contract, history recording, retry-context reconstruction, web-console rendering) are shared and taken from Claude's stream-json model; LLM-specific concerns (CLI argument construction and output-message parsing) are each runner's own responsibility, surfaced through the `build_call_args` intent-translation method.

## Requirements

### Requirement: Abstract AgentRunner Interface

The subsystem MUST expose an `AgentRunner` abstract base class defining the contract that `LLMCaller` (and any other caller) uses to interact with agent implementations. The contract has four abstract methods — `run`, `run_with_monitor`, `detect_infra_error`, and `build_call_args` — and is implementation-agnostic so future runner types (API-based, other CLIs) can satisfy it. Two concrete implementations exist: `ClaudeCodeRunner` and `CodexRunner`.

`build_call_args` is the intent-translation seam that keeps `LLMCaller` free of any LLM-specific CLI knowledge: the caller passes the *intent* of a call (the effective prompt, whether the step is read-only, and the list of context files), and each runner translates that intent into its own concrete CLI flags. This is what lets a single centralized dispatch in `LLMCaller` serve every runner without per-runner branching.

#### Scenario: run method signature
- **WHEN** a subclass implements `AgentRunner.run`
- **THEN** it MUST accept `args: List[str]`, optional `timeout: int`, optional `cwd: Path`, optional `env: Dict[str,str]`, and an optional `on_retry` callback
- **AND** it MUST return a `subprocess.CompletedProcess`

#### Scenario: run_with_monitor method signature
- **WHEN** a subclass implements `AgentRunner.run_with_monitor`
- **THEN** it MUST accept `args`, optional `log_file`, optional `wall_timeout`, `inactivity_timeout` defaulting to 1800 seconds, optional `cwd`/`env`, optional `on_output`/`on_activity` callbacks, and an optional `on_confirm` callback
- **AND** it MUST return a `MonitoredResult` (or compatible type)

#### Scenario: detect_infra_error method signature
- **WHEN** a subclass implements `AgentRunner.detect_infra_error`
- **THEN** it MUST accept `returncode: int`, `stdout: str`, `stderr: str`
- **AND** it MUST return an `InfraErrorType` enum value

#### Scenario: build_call_args method signature
- **WHEN** a subclass implements `AgentRunner.build_call_args`
- **THEN** it MUST accept the effective `prompt: str`, a `read_only: bool` flag indicating whether the step is read-only, and a `context_files: List[str]` (or equivalent) list
- **AND** it MUST return a `List[str]` of the runner-specific CLI args that express that intent (the CLI-flag translation is the runner's own responsibility, not `LLMCaller`'s)

### Requirement: InfraErrorType Taxonomy

The subsystem MUST define an `InfraErrorType` enum that classifies failures into infrastructure errors (warranting agent rotation) versus task errors (not warranting rotation).

#### Scenario: enum members
- **WHEN** `InfraErrorType` is referenced
- **THEN** it MUST define exactly five members: `NONE` (value `"none"`), `USAGE_LIMIT` (`"usage_limit"`), `TIMEOUT` (`"timeout"`), `HANG` (`"hang"`), and `STARTUP_FAILURE` (`"startup_failure"`)

#### Scenario: shell-snapshot startup-failure classification
- **GIVEN** a Codex run that exits with no effective output and whose stderr tail contains a shell-snapshot validation failure pattern (e.g. `codex_core::shell_snapshot: Shell snapshot validation failed ... syntax error near unexpected token '('`)
- **WHEN** `detect_infra_error(returncode, stdout, stderr)` is called
- **THEN** the result is classified as `InfraErrorType.STARTUP_FAILURE` rather than `NONE` or `TIMEOUT`
- **AND** the synthesized error result carries the original stderr context so users can diagnose the shell snapshot generation or validation failure

### Requirement: RunResult Dataclass

The subsystem MUST define a `RunResult` dataclass that bundles the outcome of an agent execution for callers that prefer a typed record over `subprocess.CompletedProcess`.

#### Scenario: dataclass fields
- **WHEN** a `RunResult` is constructed
- **THEN** it MUST have `returncode: int` (required), `stdout: str` (default `""`), `stderr: str` (default `""`), and `infra_error_type: InfraErrorType` (default `InfraErrorType.NONE`)

### Requirement: Single-Command ClaudeCodeRunner

`ClaudeCodeRunner` MUST wrap exactly one Claude CLI command (e.g. `claude` or `kclaude`) per instance and MUST NOT perform multi-command traversal or fallback internally; that responsibility belongs to `LLMCaller`.

#### Scenario: explicit command argument
- **WHEN** constructed with `command={"cmd": "...", "priority": N}`
- **THEN** `self.command` is set to that dict

#### Scenario: legacy commands list
- **WHEN** constructed with only the legacy `commands=[...]` parameter
- **THEN** `self.command` is set to the first entry, or to `{"cmd": "claude", "priority": 0}` if the list is empty

#### Scenario: no command supplied
- **WHEN** constructed with neither `command` nor `commands`
- **THEN** the runner loads available commands via `load_claude_commands(project_root)`
- **AND** uses the first entry, falling back to `{"cmd": "claude", "priority": 0}` if none are configured

#### Scenario: backward-compatible commands view
- **WHEN** any of the constructor paths above is used
- **THEN** the runner exposes a `self.commands` list view (either the legacy list if supplied, or `[self.command]`) for callers that still iterate

### Requirement: ClaudeCodeRunner Argument Construction

`ClaudeCodeRunner.build_call_args` MUST translate the caller's call-intent into the exact Claude CLI argv that `LLMCaller` previously hard-coded, so that the parameter-construction down-shift from `LLMCaller` into the runner is behavior-preserving. The translated argv MUST be byte-for-byte identical to the prior hard-coded form.

#### Scenario: Claude argv translated from intent
- **WHEN** `build_call_args(prompt, read_only, context_files)` is called
- **THEN** it emits `["--output-format", "stream-json", "--verbose", "-p", prompt]` as the base argv
- **AND** each entry in `context_files` is appended as a `--file <path>` pair
- **AND** when `read_only` is true it appends `--disallowedTools Write Edit NotebookEdit AskUserQuestion` (tool-layer read-only enforcement), leaving the read tools `Read` / `Grep` / `Glob` / `Bash` available
- **AND** when `read_only` is false no `--disallowedTools` argument is added

### Requirement: Codex CLI Runner

`CodexRunner` (`src/se3/codex_runner.py`) MUST implement the `AgentRunner` ABC (`run`, `run_with_monitor`, `build_call_args`, `detect_infra_error`) to wrap a single OpenAI Codex CLI command, registered for `type: codex` agents via `LLMCaller._create_runner`. Like `ClaudeCodeRunner` it wraps exactly one command per instance and performs no rotation/fallback internally. The runner's design split mirrors the subsystem's principle: the LLM-agnostic transport (stream-json NDJSON, history, retry-context) is shared, while the Codex-specific argv construction and event parsing live entirely inside this runner. Authentication is out of scope — the `codex` command is assumed to be runnable in the environment, the same assumption made for `claude`.

#### Scenario: Codex argv translated from intent
- **WHEN** `build_call_args(prompt, read_only, context_files)` is called
- **THEN** the base argv form is `codex exec --json --skip-git-repo-check` followed by a `--sandbox <mode>` pair and then the prompt as a positional argument (`--skip-git-repo-check` so Codex runs outside a git repo check)
- **AND** the argv MUST NOT contain `-a` / `--ask-for-approval` (the `codex exec` subcommand is non-interactive and has no approval step; passing `-a` causes `error: unexpected argument '-a' found`)
- **AND** the argv MUST NOT contain the legacy `--dangerously-bypass-approvals-and-sandbox` flag (superseded by the explicit `--sandbox danger-full-access` mode below)
- **AND** when `read_only` is true the sandbox flag `--sandbox read-only` is added (OS-level enforcement, stronger than Claude's tool-level `--disallowedTools`)
- **AND** when `read_only` is false the flag `--sandbox danger-full-access` is added (the non-legacy explicit "no sandbox" mode; under the non-interactive `exec` mode its effect is equivalent to the legacy `--dangerously-bypass-approvals-and-sandbox`, while remaining symmetric with the read-only branch's `--sandbox read-only`)
- **AND** because Codex has no `--file` equivalent, each `context_files` entry's content is inlined into the prompt rather than passed as a flag
- **AND** a prompt whose UTF-8 byte length exceeds the safe argv threshold is routed to the child's stdin (via the `-` marker) rather than passed as a positional argument

#### Scenario: Codex JSONL normalized to Claude-compatible stream-json
- **GIVEN** a `CodexEventConverter` consuming Codex's `--json` JSONL event stream (`thread.started`, `turn.started` / `turn.completed` / `turn.failed`, and `item.*` events such as `agent_message`, `command_execution`, `file_change`, `mcp_tool_call`)
- **WHEN** each event is converted in real time
- **THEN** an `agent_message` becomes an `assistant` text event
- **AND** a `command_execution` becomes a `tool_use` (Bash semantics) plus its result event
- **AND** a `file_change` becomes a file-writing `tool_use` (so `_last_touched_files` extraction works unchanged)
- **AND** an `mcp_tool_call` becomes the corresponding `tool_use` / result events
- **AND** a `turn.completed` (carrying usage) becomes the final `type: "result"` event with usage
- **AND** a `turn.failed` / `error` event becomes an error `type: "result"` event
- **AND** the output is Claude-compatible stream-json NDJSON, so `StreamJSONTracker`, chat-history persistence, retry/continue context reconstruction, web-console rendering, `last_raw_result`, and `_last_touched_files` all consume it with zero changes

#### Scenario: Codex usage tokens extracted independently of cost
- **GIVEN** a `turn.completed` (or `turn.failed`) event whose usage payload — located defensively across `event.usage` / `data.usage` / `turn.usage` / `message.usage` — carries `input_tokens` / `output_tokens` and Codex's `cached_input_tokens`, but whose `total_cost_usd` is absent or `null` (the Codex CLI does not always report a USD cost)
- **WHEN** `CodexEventConverter` builds the terminal `type: "result"` event
- **THEN** `input_tokens`, `output_tokens`, and the cache token fields are extracted into the result's usage, with `cached_input_tokens` mapped onto `cache_read_input_tokens`
- **AND** `total_cost_usd` defaults to `0` (a missing / `null` cost is treated as `0`) rather than dropping the usage record
- **AND** the usage record is always emitted, so its tokens flow intact through `StreamJSONTracker._capture_usage` → `add_call_usage` → `parse_usage_from_ndjson` into SE3's per-call, per-step, and per-session usage totals even when the cost is `0`
- **AND** a `turn.failed` event that carries usage preserves that usage rather than zeroing it

#### Scenario: Unknown Codex event types tolerated
- **GIVEN** the Codex `--json` schema is historically unversioned and has changed over time
- **WHEN** the converter encounters an event type it does not recognize, or a non-JSON line
- **THEN** it degrades to a log line and skips the event rather than raising, so an unknown event never crashes the conversion
- **AND** a `finalize()` fallback synthesizes a terminal `result` event when the stream ended without one

#### Scenario: Codex infrastructure-error classification
- **WHEN** `detect_infra_error(returncode, stdout, stderr)` is called for a Codex run
- **THEN** a usage-limit signal (an `error` / `turn.failed` message containing a "usage limit"-class substring, matched defensively) or an authentication failure (e.g. `401 Unauthorized`) is classified as `USAGE_LIMIT`
- **AND** `returncode == 124` (the shared inactivity/timeout signal) is classified as `TIMEOUT`
- **AND** a shell-snapshot validation failure (stderr containing `shell_snapshot` with a syntax/validation error, with no effective stdout output) is classified as `STARTUP_FAILURE`
- **AND** a successful run is classified as `NONE`
- **AND** this lets a Codex agent participate correctly in `LLMCaller`'s rotation mechanism

#### Scenario: Retry/continue reuses the agent-agnostic mechanism
- **WHEN** a Codex attempt is retried or continued
- **THEN** the runner does NOT use Codex's native `codex exec resume`; retry context is reconstructed from the chat-history NDJSON by SE3's existing agent-agnostic retry-context mechanism
- **AND** this guarantees a Claude attempt and a Codex attempt can hand off to each other within a single rotation chain

### Requirement: Setting-Sources Injection

Every spawned Claude subprocess MUST be invoked with `--setting-sources <csv>` so SE3 workers are not constrained by a downstream project's `.claude/settings.json` `permissions.deny` rules.

#### Scenario: explicit setting_sources
- **WHEN** the constructor receives `setting_sources=[...]`
- **THEN** that list is used verbatim

#### Scenario: project-derived setting_sources
- **WHEN** `setting_sources` is `None` but `project_root` is provided
- **THEN** the list is loaded from `load_claude_subprocess_config(project_root).setting_sources`

#### Scenario: default setting_sources
- **WHEN** neither `setting_sources` nor `project_root` is provided
- **THEN** the runner defaults to `["user"]`

#### Scenario: argv injection
- **WHEN** any `run` / `run_with_monitor` invocation builds the argv
- **THEN** the argv MUST be `[cmd_name, "--dangerously-skip-permissions", "--setting-sources", <csv>] + resolved_args`

### Requirement: Large-Prompt Auto-Filing via Stdin

When a `-p` / `--prompt` value would exceed the safe argv byte threshold, the runner MUST reroute the prompt to the child's stdin rather than passing it as an argv element. This avoids both Linux `execve()` `E2BIG` (`MAX_ARG_STRLEN` is 128 KB) and the older `-p @tmpfile` fallback that caused Claude Code to interpret the file as a referenced Read (subject to the 25k-token Read ceiling) rather than as the user message.

#### Scenario: threshold
- **WHEN** computing whether to reroute
- **THEN** the runner MUST compare `len(prompt.encode("utf-8"))` against `_MAX_ARG_BYTES = 102400` (100 KB, leaving ~28 KB safety margin under the 128 KB `MAX_ARG_STRLEN`)

#### Scenario: oversized -p value
- **WHEN** `_resolve_args` encounters `-p` (or `--prompt`) followed by a value whose UTF-8 byte length exceeds `_MAX_ARG_BYTES`
- **THEN** the flag is kept in argv but the value is dropped
- **AND** the value is returned as the second tuple element (`stdin_prompt`)

#### Scenario: small -p value pass-through
- **WHEN** `-p`/`--prompt` value is within the threshold
- **THEN** both the flag and the value are preserved verbatim in the resolved argv
- **AND** `stdin_prompt` is `None`

#### Scenario: @file reference pass-through (bare)
- **WHEN** an argument begins with `@`
- **THEN** it is appended unchanged so Claude Code can apply its own `@file` semantics

#### Scenario: @file reference following -p
- **WHEN** `-p @path` is encountered
- **THEN** both `-p` and the `@path` value are forwarded unchanged regardless of size

#### Scenario: multiple oversized prompts
- **WHEN** `_resolve_args` sees more than one oversized `-p` in one invocation
- **THEN** a `UserWarning` is emitted
- **AND** only the last oversized value is routed to stdin (last one wins)

### Requirement: Background Stdin Writer for Large Prompts

When a stdin-routed prompt is present, the writing MUST happen on a background daemon thread so the main thread can concurrently drain stdout. Writing inline would deadlock once the kernel pipe buffer (typically 64 KB) fills, because the child cannot drain stdin while waiting on EOF before it begins work.

#### Scenario: writer thread lifecycle
- **WHEN** `_spawn_stdin_writer(proc, payload)` is called
- **THEN** a daemon thread named `"claude-stdin-writer"` is started that writes `payload` to `proc.stdin`, flushes, and closes the pipe to deliver EOF
- **AND** `BrokenPipeError` / `OSError` during the write are swallowed (errors surface later via stdout/returncode)

#### Scenario: synchronous run with stdin prompt
- **WHEN** `run()` is called with a large `-p`
- **THEN** the prompt is passed to `subprocess.run` via the `input=` parameter (`subprocess.run` handles concurrent draining internally)

#### Scenario: popen with stdin prompt
- **WHEN** `popen()` is called with a large `-p`
- **THEN** the runner forces `stdin=subprocess.PIPE` regardless of caller kwargs
- **AND** spawns `_spawn_stdin_writer` after `Popen` returns

#### Scenario: run_with_monitor with stdin prompt
- **WHEN** `run_with_monitor()` is called with a large `-p`
- **THEN** the child is spawned with `stdin=subprocess.PIPE` and `_spawn_stdin_writer` is started
- **OTHERWISE** stdin is `None` if `sys.stdin.isatty()` (interactive Unicode support) else `subprocess.DEVNULL` (prevent hangs in non-interactive runs)

### Requirement: Synchronous run() Execution

`ClaudeCodeRunner.run` MUST execute the configured command synchronously, capturing stdout/stderr as text and enforcing a wall timeout if supplied.

#### Scenario: CLAUDECODE environment scrubbing
- **WHEN** building the child environment
- **THEN** the `CLAUDECODE` env var MUST be popped from the env dict before spawning (avoids the parent's own Claude Code session leaking into the child)

#### Scenario: default environment
- **WHEN** `env=None` is passed
- **THEN** the runner copies `os.environ` (then scrubs `CLAUDECODE`)

#### Scenario: timeout returns synthetic CompletedProcess
- **WHEN** `subprocess.run` raises `TimeoutExpired`
- **THEN** the method returns `subprocess.CompletedProcess(args=full_cmd, returncode=124, stdout="", stderr="timeout")`

#### Scenario: on_retry ignored
- **WHEN** `on_retry` callback is supplied
- **THEN** it is ignored (kept only for interface compatibility, since rotation now lives in `LLMCaller`)

### Requirement: Monitored Execution with Activity Streaming

`run_with_monitor` MUST stream the child's stdout line-by-line, recording per-line activity, optionally writing to a log file in real time, and invoking `on_output` / `on_activity` callbacks. The child's stderr is kept on a separate pipe (NOT merged into stdout) and drained by a background daemon thread, so the NDJSON carried on stdout stays clean for downstream JSON parsing.

#### Scenario: child spawn settings
- **WHEN** the monitored child is started
- **THEN** it is spawned with `stdout=PIPE`, `stderr=PIPE` (separate, not merged), `bufsize=1`, `universal_newlines=True`
- **AND** a background daemon thread drains `proc.stderr` so the pipe never fills, optionally echoing it to the parent's stderr and to a dedicated `<log_file>.stderr` sidecar file when a `log_file` is configured

#### Scenario: stderr status messages isolated from parsed stdout
- **GIVEN** the runner prints status messages such as `[claude-runner] Running command: '<cmd>'` and `[claude-runner] Command '<cmd>' succeeded`
- **WHEN** the monitored child runs
- **THEN** those status messages are written only to the parent's `sys.stderr`, never to the captured stdout stream
- **AND** because the child's own stderr is on a separate pipe, no `[claude-runner]` status text or child stderr noise is interleaved into the stdout NDJSON consumed by `LLMCaller`'s JSON parser

#### Scenario: missing command short-circuit
- **WHEN** `shutil.which(cmd_name)` returns `None`
- **THEN** the method records the "not found" message (also written to `log_file` if provided) and returns `_SingleRunResult(returncode=127, success=False, should_retry=True)`

#### Scenario: line-oriented draining loop
- **WHEN** the child is running
- **THEN** the monitor calls `select.select([stdout], [], [], 1.0)` each iteration
- **AND** on `InterruptedError` it retries (handles EINTR)
- **AND** each non-empty line read updates `last_activity = time.time()`, appends to the buffer, flushes to `log_file`, and invokes `on_output` / `on_activity` callbacks

#### Scenario: log header
- **WHEN** a `log_file` is configured
- **THEN** the runner creates the parent directory, opens the file in append mode, and writes `\n=== Starting: <full_cmd> ===\n` before the loop

#### Scenario: final stdout drain
- **WHEN** the child exits
- **THEN** any remaining buffered output is read via `proc.stdout.read()` and appended to the result/log

### Requirement: Wall-Clock Timeout

`run_with_monitor` MUST kill the child and return `_SingleRunResult(returncode=124, success=False, should_retry=True)` when total elapsed time exceeds `wall_timeout` seconds.

#### Scenario: wall timeout breach
- **WHEN** `wall_timeout` is set and `time.time() - start_time > wall_timeout`
- **THEN** the runner calls `proc.kill()`, waits, appends `"[claude-runner] Wall timeout (Ns) exceeded"` to output and log, and returns with `returncode=124`

### Requirement: Inactivity-Based Hang Detection

When no output has appeared for `inactivity_timeout` seconds (default 1800), the runner MUST classify and act on the silence. Detection MUST first attempt a resource-based confirmation via `psutil` (Linux only); regardless of the resource probe outcome, the hang is treated as confirmed and the child is killed.

#### Scenario: high-CPU hang signature
- **WHEN** `psutil` is available and `p.cpu_percent(interval=0.5) > 80.0%`
- **THEN** the runner logs `"Hang detected - high CPU usage (X%) without output for Ns"` and marks `hang_confirmed = True`

#### Scenario: excessive-memory hang signature
- **WHEN** CPU is not high but `p.memory_info().rss > 1 GiB`
- **THEN** the runner logs `"Hang detected - excessive memory usage (NMB) without output for Ns"` and marks `hang_confirmed = True`

#### Scenario: fallback inactivity timeout
- **WHEN** psutil is unavailable, the psutil probe raises, or neither CPU nor memory thresholds are met
- **THEN** the runner falls back to `"Hang detected - inactivity timeout (Ns) - no output for Ns"` and marks `hang_confirmed = True`

#### Scenario: kill with terminate fallback
- **WHEN** a hang is confirmed
- **THEN** the runner first attempts `proc.kill(); proc.wait(timeout=10)`
- **AND** on failure falls back to `proc.terminate(); proc.wait(timeout=5)`
- **AND** returns `_SingleRunResult(returncode=124, success=False, should_retry=True)` with the hang message included in output

#### Scenario: psutil unavailable on non-Linux
- **WHEN** the platform is not Linux or `psutil` import fails
- **THEN** the module-level `psutil` symbol is `None` and the resource probe is skipped

### Requirement: Usage-Limit Detection

The runner MUST classify Claude CLI usage/rate-limit failures so `LLMCaller` can rotate to a fresh agent rather than retrying against the same exhausted credential.

#### Scenario: keyword set
- **WHEN** scanning for limits
- **THEN** the runner MUST check the case-insensitive presence of any of these substrings: `"usage limit"`, `"rate limit"`, `"too many requests"`, `"rate_limit"`, `"overloaded"`, `"capacity"`, `"hit your limit"`, `"you've hit your limit"`

#### Scenario: only checked on failure
- **WHEN** `returncode == 0`
- **THEN** `detect_usage_limit` returns `False` immediately (success cannot be a limit hit)

#### Scenario: scoped to output tail
- **WHEN** scanning for keywords
- **THEN** the runner inspects only the last 3000 characters of combined stdout+stderr AND the last 20 lines, to avoid false positives from source-code docstrings (e.g. this module itself) printed earlier in the output

#### Scenario: exit code 2 special case
- **WHEN** `returncode == 2`
- **THEN** the tail is additionally scanned for `"rate_limit"` or `"usage limit"` and either match returns `True`

#### Scenario: usage limit detected in monitored run
- **WHEN** the monitored child exits with output that satisfies `detect_usage_limit`
- **THEN** a `"[claude-runner] Usage limit detected for '<cmd>'"` message is appended, and the result is marked `success=False, should_retry=True`

### Requirement: Timeout Detection

Exit code `124` MUST be treated as the canonical timeout signal (compatible with `timeout(1)`).

#### Scenario: timeout exit code
- **WHEN** `detect_timeout(returncode)` is called
- **THEN** it returns `True` iff `returncode == 124`

### Requirement: Combined Infrastructure-Error Classification

`detect_infra_error` MUST combine usage-limit and timeout checks into a single `InfraErrorType` for `LLMCaller`'s rotation logic.

#### Scenario: precedence
- **WHEN** a result satisfies both usage-limit and timeout
- **THEN** `USAGE_LIMIT` wins (it is checked first)

#### Scenario: no infra error
- **WHEN** neither detector fires
- **THEN** `InfraErrorType.NONE` is returned

#### Scenario: HANG not emitted by detect_infra_error
- **WHEN** classifying a completed `(returncode, stdout, stderr)` triple
- **THEN** `detect_infra_error` MUST NOT return `InfraErrorType.HANG` directly — hangs are surfaced via `run_with_monitor`'s synthetic `returncode=124` (which `detect_timeout` reports as `TIMEOUT`); the `HANG` enum member exists for callers that distinguish via separate signaling

### Requirement: Unusual Exit Codes Retry-Worthy Only with Hang Context

The monitored run MUST treat exit codes `1`, `137` (SIGKILL), and `143` (SIGTERM) as retry-worthy only when there is independent evidence that the run hung; otherwise they are reported as ordinary task failures (`should_retry=False`).

#### Scenario: SIGKILL with prior hang detection
- **WHEN** the child exits with `returncode` in `{1, 137, 143}` AND `hang_detected` is set OR the output contains `"timeout"` (case-insensitive)
- **THEN** the result is `_SingleRunResult(returncode=<rc>, success=False, should_retry=True)`

#### Scenario: SIGKILL without hang context
- **WHEN** the child exits with `returncode` in `{1, 137, 143}` but no hang/timeout marker is present
- **THEN** the run is returned as a non-retry failure (`should_retry=False`)

### Requirement: KeyboardInterrupt Handling in run_with_monitor

User-initiated Ctrl+C MUST not lose buffered output; the monitor MUST kill the child, drain remaining stdout, and surface the partial output to the caller with the `interrupted` flag set so callers can persist it to history before re-raising.

#### Scenario: Ctrl+C during monitored run
- **WHEN** `KeyboardInterrupt` is raised inside the monitor loop
- **THEN** the runner calls `proc.kill(); proc.wait(timeout=5)`, drains any remaining stdout into the buffer, and returns `_SingleRunResult(returncode=-2, output=<partial>, success=False, should_retry=False, interrupted=True)`

#### Scenario: interrupted propagated to MonitoredResult
- **WHEN** `_run_single_with_monitor` returns with `interrupted=True`
- **THEN** the outer `run_with_monitor` returns a `MonitoredResult(interrupted=True)` carrying the same returncode and prefixed output

### Requirement: CLI-Subprocess Confirmation-Prompt Capture

A child Claude process may, at the CLI/PTY layer, print an interactive confirmation prompt (e.g. `按 1 确定` or `Press 1 to confirm`) and then block waiting for a keystroke on stdin. `run_with_monitor` MUST accept an optional `on_confirm` callback so such prompts are surfaced to the engine and the chosen answer routed back to the child's stdin, closing the previously-missing channel for subprocess-level confirmations. When `on_confirm` is not supplied, this path is an exact no-op and existing stdout parsing / streaming behavior is unchanged.

**Detection (`detect_confirmation_prompt`):**

The module exposes `detect_confirmation_prompt(line) -> Optional[Tuple[str, List[str]]]`. The detection MUST be deliberately *conservative*: a line is treated as a confirmation prompt only when it strongly matches one of a fixed set of `_CONFIRM_PATTERNS` (Chinese `按 N …(确定|确认|继续|是)` / `输入 N …`, English `press <key> to (confirm|continue|proceed)`, `[y/N]`-style yes/no bracket prompts, and explicit `Do you want to (continue|proceed)` questions). Structured NDJSON lines (a stripped line whose first character is `{` or `[`) and any non-matching prose MUST yield `None` so callers treat them as ordinary output. On a match it returns `(prompt_text, options)` where `prompt_text` is the stripped line and `options` is a best-effort list of inline numeric labels (possibly empty).

**Callback contract:**

- `on_confirm` has the signature `(prompt_text: str, options: List[str], is_alive: Callable[[], bool]) -> Optional[str]`. `is_alive()` reports whether the child is still running, so a blocking callback can stop waiting once the child exits.
- When `on_confirm` is supplied, the child MUST be spawned with a writable stdin pipe (`stdin=subprocess.PIPE`) so the answer can be delivered, even when no large-prompt stdin payload is present.
- A non-`None` return value is written to the child's stdin (a trailing newline is appended when absent); a `None` return leaves the prompt unanswered. Stdin-write failures (closed pipe, already-exited child) are swallowed and degrade to a no-op.
- An exception raised inside `on_confirm` MUST NOT abort the monitor loop: it is caught, logged to `sys.stderr` as `[claude-runner] on_confirm callback error: <e>`, and treated as `None`.
- Because a confirmation callback may block while awaiting a response, the activity clock (`last_activity`) is reset after the callback returns so the wait does not trip the inactivity-hang detector.

#### Scenario: confirmation prompt detected and answered
- **WHEN** a monitored child emits a line that matches a `_CONFIRM_PATTERNS` entry and `on_confirm` is supplied
- **THEN** `detect_confirmation_prompt` returns `(prompt_text, options)` and `on_confirm(prompt_text, options, is_alive)` is invoked
- **AND** a non-`None` answer is written back to the child's stdin with a trailing newline
- **AND** `last_activity` is reset so the blocking callback does not trip inactivity-hang detection

#### Scenario: structured and non-matching lines are a no-op
- **WHEN** a monitored line is NDJSON (starts with `{` or `[`) or does not match any confirmation pattern
- **THEN** `detect_confirmation_prompt` returns `None` and `on_confirm` is not invoked
- **AND** the line flows through the normal stdout draining path unchanged

#### Scenario: on_confirm absent
- **WHEN** `run_with_monitor` is called without an `on_confirm` callback
- **THEN** no confirmation detection is performed and the child's stdin handling is unchanged (`None` on a TTY, `subprocess.DEVNULL` otherwise, unless a large-prompt stdin payload forces a `PIPE`)

#### Scenario: callback failure does not abort the monitor
- **WHEN** `on_confirm` raises an exception
- **THEN** the exception is caught and logged to `sys.stderr` as `[claude-runner] on_confirm callback error: <e>`
- **AND** the prompt is treated as unanswered and the monitor loop continues

### Requirement: Resource Cleanup on Exit Paths

Every exit path from `run_with_monitor` MUST close the log file handle (if any) and ensure the child is no longer running.

#### Scenario: finally block enforcement
- **WHEN** `_run_single_with_monitor` exits via any path (success, hang, timeout, exception, KeyboardInterrupt)
- **THEN** the `finally` block closes `log_fh` (if opened) and, if `proc.poll() is None`, calls `proc.kill(); proc.wait(timeout=5)` with a force-kill fallback on `TimeoutExpired`/`KeyboardInterrupt`

### Requirement: Top-Level Exception Wrapping in run_with_monitor

Unexpected exceptions raised while preparing or running the monitored command MUST be caught at the outer `run_with_monitor` level and surfaced as a `MonitoredResult` rather than propagating.

#### Scenario: arbitrary exception
- **WHEN** an exception other than `KeyboardInterrupt` reaches the outer `try` in `run_with_monitor`
- **THEN** the runner prints `"[claude-runner] Error running command '<cmd>': <e>"` to stderr and returns `MonitoredResult(returncode=1, output=<msg>, cmd_used=<cmd>, cmd_index=0, was_retry=False)`

### Requirement: MonitoredResult Dataclass

`run_with_monitor` MUST return a `MonitoredResult` dataclass that records the exit code, captured output (prefixed with `"=== Command: <cmd> ==="`), which command was used, the command index (always 0 for the single-command runner), a `was_retry` flag (always `False` for the single-command runner), an `interrupted` flag, and an optional `stderr_tail` field.

#### Scenario: success property
- **WHEN** a caller reads `result.success`
- **THEN** it returns `True` iff `returncode == 0`

#### Scenario: stderr_tail field
- **WHEN** a `MonitoredResult` is constructed
- **THEN** it carries an optional `stderr_tail: str` field (default `""`)
- **AND** `CodexRunner` populates this field with the bounded tail of the child's stderr output for use by `detect_infra_error` in classifying startup failures (e.g. shell-snapshot validation errors)
- **AND** `LLMCaller` passes this field to `detect_infra_error` on the failure path so infrastructure errors in stderr are not lost

#### Scenario: output prefixing
- **WHEN** a non-interrupted monitored run completes
- **THEN** the returned `output` is `"=== Command: <cmd> ===\n<raw output>"`

### Requirement: Backward-Compatible popen() and retry_with_next()

The runner MUST retain `popen` (async start used by collab worker/manager modules) and a deprecated `retry_with_next` for callers that have not yet migrated to `LLMCaller`-driven rotation.

#### Scenario: popen returns process and index
- **WHEN** `popen(args, ..., cmd_index=i)` is called
- **THEN** the runner clamps `i` to `len(self.commands) - 1`, builds argv from `self.commands[i]`, applies `_resolve_args`, starts the subprocess, and returns `(proc, i)`

#### Scenario: popen attaches temp files list
- **WHEN** `popen` returns
- **THEN** `proc._se3_temp_files` is set to `[]` (a no-op compatibility hook for callers that iterate over it; the runner no longer creates temp files)

#### Scenario: retry_with_next deprecation
- **WHEN** `retry_with_next` is invoked
- **THEN** it emits `DeprecationWarning("retry_with_next is deprecated; agent rotation is handled by LLMCaller")`
- **AND** returns `None` when no further command exists, else delegates to `popen(cmd_index=current+1)`

### Requirement: Command Lookup Helpers

The runner MUST expose `get_command(index)` and `get_next_command(current_cmd)` helpers used by legacy collab code paths.

#### Scenario: get_command clamps out-of-range index
- **WHEN** `get_command(i)` is called with `i >= len(self.commands)`
- **THEN** the last command's `"cmd"` value is returned

#### Scenario: get_next_command lookup
- **WHEN** `get_next_command(current_cmd)` finds `current_cmd` at index `i` with `i + 1 < len(self.commands)`
- **THEN** it returns `self.commands[i+1]["cmd"]`
- **OTHERWISE** it returns `None`

### Requirement: Backward-Compatibility Alias

The module MUST expose `ClaudeRunner = ClaudeCodeRunner` so legacy imports continue to resolve after the rename from a traversing runner to a single-command runner.

#### Scenario: legacy import
- **WHEN** code imports `from se3.claude_runner import ClaudeRunner`
- **THEN** the symbol resolves to `ClaudeCodeRunner`