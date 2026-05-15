<!-- spec-format: v1 -->
# se3-commands Specification

## Purpose

Define the command-line interface for SE3 core commands. SE3 3.0 uses `se3 run` as the unified entry point for all development workflows, with supporting commands for project initialization and spec protection.

## Requirements

### Requirement: Unified Entry Point `se3 run`

The system SHALL provide `se3 run` as the primary entry point for all SE3 workflows.

**Interface:**
```bash
# New task
se3 run "Implement feature X"

# Resume interrupted flow
se3 run --resume

# Resume a specific flow by ID
se3 run --flow-id <flow-id>

# Loop mode (continuous execution)
se3 run --loop

# Specify task type
se3 run "Fix bug" --type=bugfix

# Discovery mode
se3 run --discover "I want to build..."

# Run flow from an existing issue (interactive selection or by ID)
se3 run --from-issue ""
se3 run --from-issue <issue-id>

# Attach a named "change" label to the flow
se3 run "Implement feature X" --change feature-x
se3 run "Implement feature X" -c feature-x
```

**Top-level options:**
| Option | Default | Behavior |
|--------|---------|----------|
| `--type, -t` | `feature` | Task type (see Task Types table). |
| `--flow-id` | none | Resume a specific flow by its ID, independent of the generic `--resume` interactive selector. When supplied, the command loads the named flow and resumes it directly without prompting; when both `--flow-id` and `--resume` are given, `--flow-id` takes precedence and the interactive selector is skipped. When `--flow-id` is supplied alone (without `--resume`) the behavior is identical to `--resume --flow-id <id>` — resume of the named flow is implied. |
| `--change, -c` | none | Optional human-readable change name attached to the new flow. The change name is recorded on the flow record (`change_name`) at creation time and displayed in the "New Flow" startup panel as `Change: <name>` when set. The option applies to standard `se3 run "<task>"` invocations as well as to `se3 run --from-issue`; it does NOT apply to `--resume` (resuming a flow does not relabel it) and does NOT alter task-type selection. When omitted, no change label is attached to the flow. |

**Option aliases:** The long-form options on `se3 run` accept short aliases for ergonomic use:
| Long form | Short alias |
|-----------|-------------|
| `--resume` | `-r` |
| `--discover` | `-d` |
| `--loop` | `-l` |
| `--max-iterations` | `-n` |
| `--type` | `-t` |
| `--change` | `-c` |

The short aliases are interchangeable with their long forms (e.g. `se3 run -r` is equivalent to `se3 run --resume`, and `se3 run -d "Idea"` is equivalent to `se3 run --discover "Idea"`).

**Task Types:**
| Type | Description | Steps |
|------|-------------|-------|
| `feature` | New functionality | Full 10-step workflow |
| `bugfix` | Fixing a bug | Skip update_spec step |
| `review` | Code review/analysis | analyze → verify_spec → summarize |
| `small` | Minor fix/typo | analyze → implement → test → commit → summarize |
| `directive` | Following specific instructions | analyze → plan → implement → commit → summarize |

#### Scenario: New task execution
- **WHEN** user executes `se3 run "Implement user authentication"`
- **THEN** the flow engine creates a new flow instance
- **AND** starts execution from the analyze step

#### Scenario: New task execution with --change label
- **WHEN** user executes `se3 run "Implement feature X" --change feature-x` (or the short form `-c feature-x`)
- **THEN** a new flow is created with `change_name` set to `feature-x`
- **AND** the startup "New Flow" panel includes a `Change: feature-x` line
- **AND** flow behavior is otherwise identical to a `se3 run "<task>"` invocation without `--change`

#### Scenario: Resume interrupted flow
- **WHEN** user executes `se3 run --resume` with an active flow
- **THEN** the flow engine loads the persisted state
- **AND** continues execution from the interrupted step

#### Scenario: Resume a specific flow by ID
- **GIVEN** a known flow ID `<flow-id>` (e.g., as printed by `se3 history`)
- **WHEN** the user executes `se3 run --flow-id <flow-id>` (with or without `--resume`)
- **THEN** the flow engine loads that specific flow's persisted state and continues execution from the interrupted step
- **AND** no interactive resume-selection prompt is displayed

#### Scenario: Loop mode execution
- **WHEN** user executes `se3 run --loop`
- **THEN** the flow engine continuously executes tasks

#### Scenario: Loop mode with branch isolation
- **WHEN** user executes `se3 run --loop` (without `--no-worktree`)
- **THEN** creates a `se3-loop/{timestamp}` branch and git worktree
- **AND** all tasks execute in the worktree
- **AND** on completion, prompts user to merge/defer/discard

#### Scenario: List loop branches
- **WHEN** user executes `se3 run --list-loops`
- **THEN** displays all unmerged loop branches with commit counts
- **AND** shows instructions for merging or discarding

#### Scenario: Merge loop branch with diff summary
- **WHEN** user executes `se3 run --loop --merge <branch>`
- **THEN** shows diff stat summary before merging
- **AND** prompts for confirmation before proceeding
- **AND** on conflict, displays conflicting file list with resolution instructions

#### Scenario: Discovery mode execution
- **WHEN** user executes `se3 run --discover "Idea"`
- **THEN** the flow engine starts in discovery mode
- **AND** explores requirements through multi-turn conversation
- **AND** after LLM confirms requirements are clear, prompts the user via the regular discovery input; typing the exact string `1` confirms and proceeds to analyze, any other non-empty input is treated as the next user turn of discovery (empty input is a no-op: the prompt is re-displayed), no separate numbered-choice UI

#### Scenario: Discovery mode forces task type to "discovery"
- **WHEN** user executes `se3 run --discover "Idea"` (regardless of any `--type` value supplied on the same invocation)
- **THEN** the flow is started with its task type set to the literal string `"discovery"`
- **AND** the `"discovery"` task type is a discovery-mode-only task type, distinct from the standard task-type set of `feature` / `bugfix` / `review` / `small` / `directive` in the Task Types table
- **AND** the standard Task Types step-skipping rules do NOT apply to the `"discovery"` task type; the flow is governed by the flow engine's discovery-mode handling

### Requirement: `se3 run --from-issue` Option

`se3 run` SHALL accept a `--from-issue` option that sources the flow's task description from an existing SE3 issue and synchronizes the issue's status with the flow result. The option is mutually meaningful with the standard run path: when supplied, the flow is non-loop and the task description argument is ignored in favor of the issue's description.

**Interface:**
```bash
se3 run --from-issue ""               # Interactive selection from open issues (explicit empty string)
se3 run --from-issue <issue-id>       # Load the named issue by ID
```

**Behavior contract:**

1. **Interactive selection.** Because `--from-issue` is declared as a typer option that requires a value, the user MUST supply an explicit empty string (`--from-issue ""`) to trigger interactive selection; running `se3 run --from-issue` with no value at all fails at typer/click argument parsing and never enters the command body. When the supplied value is the empty string, the command lists all open (non-closed) issues with their IDs, titles, and priorities, then prompts the user to enter an issue ID. When no open issues exist, the command prints a "No open issues found" message and exits with a non-zero exit code.
2. **Issue lookup.** The supplied (or interactively entered) ID is loaded via the issue manager. When no issue with that ID exists, the command prints an error and exits with a non-zero exit code.
3. **In-progress rejection.** When the loaded issue is already in `in_progress` status, the command refuses to start a new flow and tells the user to run `se3 issue reset <id>` first. Exit code is non-zero.
4. **Status transition on start.** Before running the flow, the issue's status is transitioned to `in_progress`. When the transition itself raises (invalid transition for the issue's current state), the command prints the error and exits non-zero without starting the flow.
5. **Flow execution.** The flow runs with the issue's description as the task description, the `--type` option as the task type (default `feature`), `is_loop_mode=False`, and the issue ID recorded on the flow as its source issue.
6. **Status transition on completion.** When the flow exits with code 0, the issue is transitioned to `resolved`. When the flow exits with any non-zero code, the issue is transitioned back to `open`. Failures of these final status transitions are best-effort (swallowed) so that the flow's exit code remains the command's exit code.

#### Scenario: --from-issue with explicit ID resolves to flow run
- **GIVEN** an open issue with ID `<id>` and a non-empty description
- **WHEN** the user runs `se3 run --from-issue <id>`
- **THEN** the issue is transitioned to `in_progress`
- **AND** a flow is started with the issue's description as the task and the issue ID recorded as the flow's source issue

#### Scenario: --from-issue with empty-string value triggers interactive selection
- **GIVEN** at least one open issue exists
- **WHEN** the user runs `se3 run --from-issue ""` (explicit empty-string value)
- **THEN** the command lists all open issues with ID, title, and priority
- **AND** prompts the user to enter an issue ID
- **AND** proceeds with the entered ID as if it had been supplied directly

#### Scenario: --from-issue with no value at all is rejected by argument parsing
- **WHEN** the user runs `se3 run --from-issue` with no value following the flag
- **THEN** typer/click rejects the invocation at argument parsing because `--from-issue` requires a value
- **AND** the command never enters interactive selection (the user must pass `--from-issue ""` to reach that path)

#### Scenario: --from-issue interactive selection with no open issues
- **GIVEN** there are no open issues
- **WHEN** the user runs `se3 run --from-issue ""` (explicit empty-string value)
- **THEN** the command prints a "No open issues found" message and exits with a non-zero exit code
- **AND** no flow is started

#### Scenario: --from-issue rejects unknown issue ID
- **GIVEN** no issue exists with the supplied ID
- **WHEN** the user runs `se3 run --from-issue <id>`
- **THEN** the command prints an error indicating the issue was not found and exits with a non-zero exit code

#### Scenario: --from-issue rejects in-progress issue
- **GIVEN** an issue whose status is already `in_progress`
- **WHEN** the user runs `se3 run --from-issue <id>`
- **THEN** the command prints an error directing the user to run `se3 issue reset <id>` first
- **AND** exits with a non-zero exit code without starting a flow

#### Scenario: --from-issue marks issue resolved on success
- **GIVEN** the flow started from issue `<id>` exits with code 0
- **WHEN** the command finalizes
- **THEN** the issue's status is transitioned to `resolved`
- **AND** the command exits with code 0

#### Scenario: --from-issue reopens issue on flow failure
- **GIVEN** the flow started from issue `<id>` exits with a non-zero code
- **WHEN** the command finalizes
- **THEN** the issue's status is transitioned back to `open`
- **AND** the command exits with the flow's non-zero code

### Requirement: `se3 init` Command

The `se3 init` command SHALL initialize a new SE3 project with the standard directory structure.

**Interface:**
```bash
se3 init [--project-root PATH] [--name PROJECT_NAME] [--force]
```

**Option aliases:** The long-form options above accept short aliases for ergonomic use:
| Long form | Short alias |
|-----------|-------------|
| `--project-root` | `-p` |
| `--name` | `-n` |
| `--force` | `-f` |

**Created Structure:**
```
project/
├── se3.yaml              # Project configuration
└── se3/
    └── specs/
        └── base/
            └── spec.md   # Base project specification
```

#### Scenario: Initialize new project
- **GIVEN** a directory without SE3 configuration
- **WHEN** user runs `se3 init`
- **THEN** it creates se3.yaml, se3/specs/, and se3/specs/base/spec.md

#### Scenario: Initialize with custom name
- **GIVEN** a directory at /path/to/my-project
- **WHEN** user runs `se3 init --name "My Project"`
- **THEN** the base spec contains "My Project" as project name

### Requirement: `se3 init` Git Repository Initialization

In addition to creating the SE3 directory structure, `se3 init` SHALL ensure the project root is a git repository and that an appropriate `.gitignore` is present. Both side effects are part of the standard initialization flow and SHALL NOT require any opt-in flag.

**Git repository handling:**
- When the project root is not already inside a git repository (no `.git` directory found in the root or any ancestor directory), `se3 init` SHALL run `git init` in the project root.
- When the project root is already inside a git repository, `se3 init` SHALL leave git state untouched and report that a repository already exists.
- When `git` is not installed (or not on `PATH`) and a `git init` is needed, the failure SHALL be surfaced as a non-fatal status message; the rest of the initialization (configuration file, base spec, `.gitignore` handling) SHALL still complete.

**`.gitignore` handling:** `se3 init` SHALL create or update a `.gitignore` file at the project root with patterns appropriate for an SE3 project. The function returns one of five outcomes, which are surfaced distinctly so the user is never misled about what changed:

| Outcome | Meaning |
|---------|---------|
| `created` | The file did not exist (or `--force` was passed); the full default template was written. |
| `appended` | The file existed without an `se3.local.yaml` ignore pattern; the local-config-ignore block was appended (idempotent — re-running is a no-op). This happens even without `--force`, because the task requires the pattern to be present. |
| `negated` | The file existed and contained a narrow explicit negation `!se3.local.yaml` (or another negation that targets `se3.local.yaml` without also matching `se3.yaml`); the file is left untouched and a warning is surfaced rather than creating two conflicting last-line-wins rules. |
| `unchanged` | The file existed and already ignored `se3.local.yaml` (literally or via a broader matching glob such as `*.local.yaml`, `*.local.*`, `se3.local.*`). |
| `error` | An I/O error prevented reading or writing the file; surfaced distinctly from `unchanged` so the operator sees the real failure. |

**Pattern recognition rules:**
- Existing pattern detection SHALL normalize gitignore syntax that `fnmatch` does not model: a leading `/` anchor, a leading `**/` recursive-glob, and a trailing `/` directory marker are all stripped before matching.
- Comment lines (`#…`) and negation lines (`!…`) are ignored when checking whether `se3.local.yaml` is already ignored.
- A negation line is treated as "narrow" (and triggers the `negated` outcome) only when its pattern matches `se3.local.yaml` but does NOT also match `se3.yaml`. Broad negations such as `!*.yaml`, `!se3.*`, or `!*` are not treated as narrow negations because the user was not explicitly un-ignoring the local config; the standard `appended` path applies.

**Default `.gitignore` template content:** The default template SHALL include at least the following sections:
- Python build/cache artifacts (`__pycache__/`, `*.py[cod]`, `build/`, `dist/`, `*.egg-info/`, etc.).
- Virtual environment directories (`venv/`, `.venv`, `env/`, `ENV/`).
- Common IDE/editor artifacts (`.vscode/`, `.idea/`, `*.swp`, `.DS_Store`, etc.).
- An SE3 runtime-content block that ignores everything under `/se3/` and then explicitly whitelists `/se3/specs/`, `/se3/issues/`, `/se3/scripts/`, and `/se3/version-rules.md`.
- An SE3 local-only config block that ignores `se3.local.yaml`.

**`se3.local.yaml` shadowing detection:** When `se3 init` runs, it SHALL detect (but never modify) an existing `se3.local.yaml` file at the project root using `is_file()` semantics — a real regular file shadows `se3.yaml` at load time, while a directory or dangling symlink at that path does not and SHALL NOT trigger the warning. When shadowing is detected, the result surfaces a `local_overrides_yaml` flag so the operator knows the just-generated `se3.yaml` will be shadowed at load time.

**Created Structure (extended):**
```
project/
├── .git/                       # Initialized when not already in a git repo
├── .gitignore                  # Created or updated with SE3 patterns
├── se3.yaml                    # Project configuration
└── se3/
    └── specs/
        └── base/
            └── spec.md         # Base project specification
```

#### Scenario: Initialize git repository when not already in one
- **GIVEN** a directory that is not inside any git repository
- **WHEN** the user runs `se3 init`
- **THEN** `git init` is executed in the project root
- **AND** the result reports that a git repository was initialized

#### Scenario: Skip git init when already inside a repository
- **GIVEN** a directory that is already inside a git repository (a `.git` directory exists in the root or any ancestor)
- **WHEN** the user runs `se3 init`
- **THEN** no `git init` is executed
- **AND** the result reports that a git repository already exists

#### Scenario: Git not installed does not abort init
- **GIVEN** a directory that is not inside a git repository
- **AND** the `git` executable is not on `PATH`
- **WHEN** the user runs `se3 init`
- **THEN** the git initialization step records a failure message
- **AND** the remaining steps (config file, base spec, `.gitignore`) still complete

#### Scenario: Create .gitignore from default template
- **GIVEN** a directory with no existing `.gitignore`
- **WHEN** the user runs `se3 init`
- **THEN** `.gitignore` is created with the default template containing Python, virtualenv, IDE, SE3 runtime whitelist (`/se3/*` with explicit unignore for `/se3/specs/`, `/se3/issues/`, `/se3/scripts/`, `/se3/version-rules.md`), and `se3.local.yaml` blocks
- **AND** the outcome is reported as `created`

#### Scenario: Append local-config block to existing .gitignore
- **GIVEN** an existing `.gitignore` that does not already ignore `se3.local.yaml` and does not narrowly un-ignore it
- **WHEN** the user runs `se3 init`
- **THEN** the local-config-ignore block (comment line + `se3.local.yaml` line) is appended to the file
- **AND** the outcome is reported as `appended`
- **AND** running `se3 init` again is a no-op (the next run reports `unchanged`)

#### Scenario: Recognize existing broader pattern
- **GIVEN** an existing `.gitignore` containing a pattern that already matches `se3.local.yaml` (e.g. the literal line `se3.local.yaml`, or a broader glob such as `*.local.yaml`, `*.local.*`, or `se3.local.*`)
- **WHEN** the user runs `se3 init`
- **THEN** the file is not modified
- **AND** the outcome is reported as `unchanged`

#### Scenario: Refuse to append when narrow negation exists
- **GIVEN** an existing `.gitignore` that contains an explicit narrow negation such as `!se3.local.yaml` (matches `se3.local.yaml` but not `se3.yaml`)
- **WHEN** the user runs `se3 init`
- **THEN** the file is left untouched
- **AND** the outcome is reported as `negated` with a warning so the operator sees that two conflicting rules were avoided

#### Scenario: Broad negation does not block append
- **GIVEN** an existing `.gitignore` that contains only a broad negation such as `!*.yaml` (which would also un-ignore `se3.yaml`)
- **WHEN** the user runs `se3 init`
- **THEN** the local-config-ignore block is appended normally
- **AND** the outcome is reported as `appended`

#### Scenario: Force rewrite of existing .gitignore
- **GIVEN** an existing `.gitignore` with arbitrary user content
- **WHEN** the user runs `se3 init --force`
- **THEN** `.gitignore` is overwritten with the full default template
- **AND** the outcome is reported as `created`

#### Scenario: .gitignore I/O failure reported distinctly
- **GIVEN** an existing `.gitignore` that cannot be read or written (permission error, etc.)
- **WHEN** the user runs `se3 init`
- **THEN** the outcome is reported as `error` with the underlying failure message
- **AND** the outcome is NOT reported as `unchanged`

#### Scenario: Detect shadowing se3.local.yaml
- **GIVEN** a project root that contains an existing `se3.local.yaml` regular file
- **WHEN** the user runs `se3 init`
- **THEN** the existing `se3.local.yaml` is not modified
- **AND** the result surfaces a flag indicating that `se3.local.yaml` will shadow `se3.yaml` at load time

#### Scenario: Directory or dangling symlink at se3.local.yaml does not trigger shadow warning
- **GIVEN** a project root where `se3.local.yaml` is a directory or a dangling symlink (i.e. not a regular file)
- **WHEN** the user runs `se3 init`
- **THEN** the shadow-detection flag is NOT raised (the check uses `is_file()`, not `exists()`)

### Requirement: `se3 guardrails` Command

The `se3 guardrails` command SHALL check spec files against SE3 Spec Guardrails.

**Interface:**
```bash
se3 guardrails <spec-file> [--original <original-file>]
```

**Option aliases:** The long-form options above accept short aliases for ergonomic use:
| Long form | Short alias |
|-----------|-------------|
| `--original` | `-o` |

**Guardrail Checks:**
1. **must_not_delete**: Detect deleted WHEN/THEN scenarios
2. **must_not_weaken**: Detect weakened language (SHALL → SHOULD, MUST → SHOULD)

#### Scenario: Detect spec violations
- **GIVEN** a modified spec file
- **WHEN** user runs `se3 guardrails <spec-file>`
- **THEN** the command compares with original spec
- **AND** reports any deleted requirements or weakened language

#### Scenario: No violations found
- **GIVEN** a spec file with no guardrail violations
- **WHEN** user runs `se3 guardrails <spec-file>`
- **THEN** the command reports success

### Requirement: `se3 history` Command

The `se3 history` command SHALL list and inspect all flow executions across three data sources: the active engine state, the archive, and chat-history-only directories.

**Interface:**
```bash
se3 history                          # List all flows (default)
se3 history list                     # List all flows
se3 history list --active-only       # Show only the active flow
se3 history list --archived-only     # Show only archived flows
se3 history list --json              # Output as JSON
se3 history show <flow_id>           # Show detailed info for a flow
se3 history show <flow_id> --detailed          # Show LLM call details (structured prompt + final response)
se3 history show <flow_id> --detailed --verbose  # Show full response including tool calls
se3 history show <flow_id> --detailed --json     # Output detailed chat history as JSON
se3 history restore <flow_id>        # Resume a flow (delegates to se3 run --resume)
se3 history restore <flow_id> --dry-run    # Print the resume command without executing it
se3 history archived                 # List only archived flows
se3 history archived --json          # Output archived flows as JSON
```

**Option aliases:** The long-form options on `se3 history list`, `se3 history show`, `se3 history restore`, and `se3 history archived` accept short aliases for ergonomic use:
| Subcommand | Long form | Short alias |
|------------|-----------|-------------|
| `list` | `--archived-only` | `-a` |
| `list` | `--json` | `-j` |
| `show` | `--json` | `-j` |
| `show` | `--detailed` | `-d` |
| `show` | `--verbose` | `-v` |
| `restore` | `--dry-run` | `-n` |
| `archived` | `--json` | `-j` |

The `--active-only` option on `se3 history list` (and the default `se3 history` invocation) is supported as shown in the interface examples but has NO short alias; it MUST be supplied in its long form.

**Data Sources (aggregated by `list` / default command):**
| Source | Path | Label |
|--------|------|-------|
| Active | `se3/state/engine.json` | `active` |
| Archived | `se3/state/archive/engine_*.json` | `archived` |
| History-only | `se3/history/{flow_id}/` | `history` |

Results are de-duplicated by `flow_id` and sorted by `updated_at` descending.

#### Scenario: List all flows
- **WHEN** user runs `se3 history` or `se3 history list`
- **THEN** all flows from all three sources are displayed in a table
- **AND** each row includes flow_id, status, task description, progress, updated time, and source

#### Scenario: Filter active or archived flows
- **WHEN** user adds `--active-only` or `--archived-only`
- **THEN** only flows matching that source are displayed

#### Scenario: Show flow details
- **GIVEN** a valid flow_id (or unambiguous prefix)
- **WHEN** user runs `se3 history show <flow_id>`
- **THEN** the system searches for the flow across three data sources in order: active flow, archived flows, then history-only flows
- **AND** if an exact flow_id match is not found, performs prefix matching against all flows from all three sources
- **AND** displays detailed step-by-step breakdown of the flow

#### Scenario: Show flow details with LLM call details
- **GIVEN** a valid flow_id with chat history
- **WHEN** user runs `se3 history show <flow_id> --detailed`
- **THEN** displays flow metadata and step table as usual
- **AND** sessions are passed through `interleave_sessions_for_display()` before rendering so that fix-loop `implement` sessions are split into virtual `-iter{N}` sub-sessions and chronologically interleaved with `test` / `self_check` sessions (see the Chat History interleave scenario in `flow-engine` spec)
- **AND** appends each step's LLM call details: prompt is shown as structured segments (auto-detected sections such as JSON Mode Instruction, Step Instructions, Available Specifications, Discovery Context, Read-Only Constraint, Language Instruction, Additional User Instruction, etc.) grouped under a left-aligned bold-blue markdown-style heading (e.g. `## Prompt` or `## Prompt (attempt N)`) with no outer Rich `Panel` border; the assistant response is shown under a corresponding bold-green `## Response` heading (see the Chat History detailed rendering scenario in `flow-engine` spec for the canonical color/heading mapping)
- **AND** prompt segments containing embedded spec content are folded into compact reference annotations for readability:
  - Segments titled "Relevant Specifications", "Specifications (for context only)", or "Project Conventions" fold each `### spec-name` subsection into `[spec] @spec-name  (折叠, size)` with `bold magenta` Rich styling on `@spec-name`
  - Segments titled "Base Specification" fold the entire body into `[spec] @base  (折叠, size)`
  - Segments that only list spec names (e.g., "Available Specifications") are NOT folded
  - `### name` headings that appear inside fenced code blocks (``` or ~~~) or indented code blocks are NOT treated as spec subsections and are preserved as-is; only `### name` headings outside code contexts qualify for folding
  - Spec recognition tolerates arbitrary blank lines between the `### spec-name` marker and its following `# Title` H1 heading, so unusually formatted specs are still folded via the primary recognition path
- **AND** response shows only the final assistant text block (skipping intermediate tool calls and tool results)
- **AND** multiple attempts within a step are shown separately with attempt labels

#### Scenario: Detailed with verbose response
- **WHEN** user runs `se3 history show <flow_id> --detailed --verbose`
- **THEN** prompt display is identical to `--detailed` (structured segments)
- **AND** response display reuses `_render_ndjson_for_human()` to show the full conversation flow including text content and tool call/result summaries (via `format_tool_use_preview()` / `format_tool_result_preview()`), consistent with `se3 run` streaming style
- **AND** `--verbose` implies `--detailed`

#### Scenario: Detailed JSON output
- **WHEN** user runs `se3 history show <flow_id> --detailed --json`
- **THEN** outputs structured JSON containing flow metadata plus a `chat_history` array
- **AND** each entry in `chat_history` includes `step_id`, `step_type`, and `messages`
- **AND** user messages include `segments` (auto-segmented prompt sections) and full `content`
- **AND** assistant messages include `content`, and `raw_json` (original NDJSON data)
- **AND** the array order matches the Rich display path: fix-loop implement sessions are virtually split into `-iter{N}` entries and interleaved chronologically with test/self_check entries (via the same `interleave_sessions_for_display()` applied in `get_detailed_json`)

#### Scenario: Restore a flow
- **WHEN** user runs `se3 history restore <flow_id>`
- **THEN** delegates to `se3 run --resume --flow-id <flow_id>`
- **AND** the supplied `<flow_id>` is validated against the union of active, archived, and history-only flows; an exact match wins, otherwise prefix matching is attempted
- **AND** when the prefix matches multiple flows the command lists the candidates and exits non-zero without delegating
- **AND** when no exact or unambiguous prefix match exists the command prints `Flow '<flow_id>' not found.` to stderr and exits non-zero

#### Scenario: Restore in dry-run mode prints the resume command without executing
- **GIVEN** a valid `<flow_id>` that matches a flow (exactly or by unambiguous prefix)
- **WHEN** the user runs `se3 history restore <flow_id> --dry-run` (or the short form `-n`)
- **THEN** the command prints `Would restore flow: <resolved-flow-id>` followed by `Command: se3 run --resume --flow-id <resolved-flow-id>`
- **AND** does NOT invoke `se3 run --resume`
- **AND** exits with code 0

#### Scenario: List archived flows as JSON
- **WHEN** the user runs `se3 history archived --json` (or the short form `-j`)
- **THEN** the command emits the archived flow list as JSON (indented, with non-JSON-serializable values such as datetimes coerced to strings) to stdout instead of rendering the Rich table
- **AND** when there are no archived flows the output is the JSON serialization of an empty list (no "No archived flows found." message is printed under `--json`)

### Requirement: `se3 sync` Command

The `se3 sync` command SHALL refresh `se3/specs/` so that each spec file reflects the **current state of project code**. Sync is one-directional (code → spec): when a spec drifts from the code, the spec is updated. The command iterates rounds until a fixed point is reached (no spec changes), or a hard cap is hit, or oscillation is detected.

**Philosophy:** Specs are the documented snapshot of code (a spec-assistant view). Future intent enters through issues, not through specs. Sync therefore never modifies project source code, never creates issues from spec/code drift, and never propagates spec content back to code.

**Interface:**
```bash
se3 sync                              # Default: loop until convergence
se3 sync --once                       # Run a single round (legacy / CI-friendly)
se3 sync --max-rounds 10              # Hard cap on number of rounds (default 10)
se3 sync --stable-rounds 1            # Consecutive zero-change rounds required to declare convergence (default 1)
se3 sync --interactive                # Pause for human approval on high-impact deletions
se3 sync --show-diff                  # Print the full spec diff at end of run
se3 sync --validate-only              # Only validate on-disk specs against the spec-format v1 structural contract; never call the LLM
se3 sync --resume                     # Resume a previously interrupted sync run from se3/state/sync_checkpoint.json
```

**Option summary:**
| Option | Default | Behavior |
|--------|---------|----------|
| `--once` | off | Run exactly one round and exit, regardless of whether drift remains. Useful for CI gates and single-step inspection. |
| `--max-rounds N` | 10 | Maximum number of rounds before aborting as non-converged. |
| `--stable-rounds N` | 1 | Number of consecutive zero-change rounds required to declare convergence. Raise to 2+ for higher confidence. |
| `--interactive`, `-i` | off | When set, sync pauses and writes a `sync_high_impact_deletion` call file before deleting an entire `### Requirement:` block. Other updates are still applied automatically. The short alias `-i` is equivalent to `--interactive`. |
| `--show-diff` | off | Print the full aggregated spec diff after the final round. |
| `--validate-only` | off | Skip every LLM call. Walk `se3/specs/**/spec.md` and run the spec-format v1 structural validator on each file. Exit `0` when every spec passes, `1` when any spec fails. Mutually exclusive with `--resume`. |
| `--resume` | off | Read `se3/state/sync_checkpoint.json`, skip specs whose content hash still matches the checkpoint's `in_sync_specs` entry, and continue from the saved `round_index`. Mutually exclusive with `--validate-only`. |

**Drift classification (used for log readability only):** Each round's analyzer still classifies drift as *gap* (spec describes something not in code → delete that spec section), *extension* (code does something the spec omits → add a section), or *conflict* (spec describes the behavior differently from the code → modify the section). All three classes resolve to the same kind of action: update the spec. The classification is preserved in round reports so humans can scan what changed.

**Per-round honesty:** Each round's LLM call is stateless — it sees only the current spec content plus a fresh project-code snapshot. The sync driver process retains cross-round history for convergence detection, oscillation detection, and final reporting; it does NOT feed this history back into the LLM prompts.

#### Scenario: Sync with existing base spec
- **GIVEN** the project has a base spec at `se3/specs/base/`
- **WHEN** user runs `se3 sync`
- **THEN** the engine loads all specs starting from base
- **AND** performs an LLM-driven comparison of each spec against project code
- **AND** the analyzer's classification (gap / extension / conflict) is recorded for logging
- **AND** every drift, regardless of classification, is resolved by updating the spec to reflect the code

#### Scenario: Sync without base spec (SE3 bootstrapping)
- **GIVEN** the project has no `se3/specs/base/` directory
- **WHEN** user runs `se3 sync`
- **THEN** the engine first explores the project codebase and generates a base spec in the first round
- **AND** subsequent rounds only update existing specs (no new discovery)

#### Scenario: Spec drift detected — unified update
- **WHEN** any drift is found between a spec and the code, of any classification (gap / extension / conflict)
- **THEN** the engine uses an LLM to update the spec file to match the code's actual behavior
- **AND** for *gap* drift the corresponding spec section is removed (the code no longer implements that requirement)
- **AND** for *extension* drift a new section is added
- **AND** for *conflict* drift the existing section is rewritten
- **AND** a content length safety guard rejects suspiciously short LLM outputs (< 50% of original)
- **AND** markdown code fences wrapping the LLM response are stripped before writing to spec files
- **AND** sync NEVER creates issues, edits source code, or touches files outside `se3/specs/`

#### Scenario: Convergence reached
- **GIVEN** `--stable-rounds` defaults to 1
- **WHEN** a round completes with zero spec changes
- **THEN** the loop terminates as converged
- **AND** the final report states: `Converged after N rounds. Total M specs updated. Final round: 0 changes.`
- **AND** the report includes an explicit honesty disclaimer: *"Convergence means the LLM detected no drift in the final round; it does not guarantee absolute spec/code consistency."*

#### Scenario: Convergence with --stable-rounds 2
- **GIVEN** the user runs `se3 sync --stable-rounds 2`
- **WHEN** a round produces zero changes but the prior round produced changes
- **THEN** the loop continues for one more round
- **AND** the loop only terminates after two consecutive zero-change rounds

#### Scenario: Max-rounds reached without convergence
- **GIVEN** `--max-rounds N` (default 10)
- **WHEN** the loop has executed N rounds and the last round still produced spec changes
- **THEN** the loop terminates and the command exits with a non-zero status
- **AND** the report clearly states: `sync did not converge within N rounds` and lists which specs were still being modified

#### Scenario: Oscillation detected
- **WHEN** the sync driver observes that the SHA-256 of a spec's content has cycled (e.g. A → B → A → B over the last K rounds, K=4 by default)
- **THEN** the loop aborts immediately
- **AND** the report names the oscillating spec(s), prints the cycle, and asks the user to inspect manually
- **AND** the command exits with a non-zero status

#### Scenario: --once mode
- **WHEN** the user runs `se3 sync --once`
- **THEN** the engine performs exactly one round and exits
- **AND** the report distinguishes the single-round case explicitly (it does NOT claim "converged")
- **AND** any remaining drift is reported with the suggestion to re-run without `--once`

#### Scenario: --interactive mode and high-impact deletion
- **GIVEN** the user runs `se3 sync --interactive`
- **WHEN** within a round the LLM concludes that an entire `### Requirement:` block (with its scenarios) should be deleted from a spec
- **THEN** the engine writes a single MCP call file in `se3/calls/` of type `sync_high_impact_deletion`
- **AND** the call file lists each pending deletion with spec name, requirement name, and the spec content that would be removed (truncated to 2000 chars per item)
- **AND** the available decision values are `approve` and `skip`
- **AND** non-deletion updates inside the same round are still applied automatically
- **AND** the round pauses until the user runs `se3 sync-respond` with their decisions

#### Scenario: Non-interactive default does not pause
- **GIVEN** the user runs `se3 sync` without `--interactive`
- **WHEN** any drift is found, including deletions of entire requirement blocks
- **THEN** the engine applies all updates automatically without writing any call file
- **AND** the loop relies on oscillation detection and `--max-rounds` to bound risk

#### Scenario: Sub-agent prompt offers two write paths (Way A and Way B)
- **GIVEN** the sync engine prepares a spec-update prompt for a sub-agent invocation
- **WHEN** the prompt is rendered
- **THEN** it presents two paths the sub-agent MAY choose between, without disabling any tools:
  - **Way A** — use the `Edit` tool to modify `se3/specs/<name>/spec.md` in place; the reply only needs to describe the change
  - **Way B** — output the complete new content of `spec.md` as a single markdown code block; the framework writes it to disk
- **AND** in both cases the prompt declares the final `spec.md` MUST start with `<!-- spec-format: v1 -->`, then `# <spec-name> Specification`, contain a `## Purpose` section, and contain at least one `### Requirement:` section

#### Scenario: Way A — sub-agent edits the spec file directly
- **GIVEN** the engine snapshots the target spec's `(mtime, sha256)` before invoking the sub-agent
- **WHEN** the sub-agent uses `Edit` to modify `se3/specs/<name>/spec.md` and the on-disk file's hash changes after the call
- **THEN** the engine re-reads the file from disk
- **AND** runs `validate_spec_structure(content, spec_name)` from the spec-format structural contract
- **AND** when validation passes, refreshes the in-memory `_specs[name]["content"]` to match disk and counts the spec as updated
- **AND** when validation fails, restores the file via `git checkout HEAD -- <spec-path>` and surfaces a structured error so the round records a rollback rather than a successful update

#### Scenario: Way B — sub-agent returns full rewrite as markdown
- **GIVEN** the on-disk spec hash is unchanged after the sub-agent call
- **AND** the sub-agent's stdout contains a complete spec.md body
- **WHEN** the engine parses the response
- **THEN** the full-rewrite write path is taken (markdown code fences are stripped first)
- **AND** the written content is validated with `validate_spec_structure(...)`
- **AND** the in-memory cache is refreshed only after validation passes
- **AND** a content length safety guard observes when the rewrite is < 50% of the prior length but downgrades the observation to a warning rather than rejecting the update, so legitimate condensations are not blocked

#### Scenario: Way B — neither disk change nor inline spec content
- **GIVEN** the on-disk spec hash is unchanged after the sub-agent call
- **AND** the sub-agent's stdout does NOT contain a complete spec.md body
- **WHEN** the engine evaluates the response
- **THEN** the spec is left unchanged on disk
- **AND** the round records an error for that spec and continues with the remaining specs
- **AND** no in-memory cache refresh occurs

#### Scenario: LLM output format error does not fabricate a CONFLICT diff
- **GIVEN** the analyzer receives a non-empty response whose JSON payload cannot be parsed
- **WHEN** the analyzer processes the response
- **THEN** the spec's `SpecAnalysis` is recorded with `failed_analysis_reason = "llm_output_format_error"` and an empty diff list
- **AND** the analyzer does NOT synthesize a `CONFLICT` diff to represent the failure
- **AND** the round's stability calculation treats this spec as a non-blocking failed analysis rather than as an open drift

#### Scenario: Infrastructure failure is distinguished from format error
- **GIVEN** the analyzer receives an empty, truncated, or otherwise unusable response (network/quota/empty body)
- **WHEN** the analyzer processes the response
- **THEN** `failed_analysis_reason = "infrastructure_failure"` is recorded for that spec
- **AND** the spec is reported under a "partial success" section in the final report, separate from genuinely in-sync specs
- **AND** the spec is retried on the next round

#### Scenario: Round stability tolerates failed analyses
- **GIVEN** a round produces zero spec updates
- **WHEN** every spec in the round is either `is_in_sync = True` or carries a non-null `failed_analysis_reason`
- **THEN** the round is treated as stable for convergence purposes
- **AND** the final report enumerates the failed-analysis specs separately so the operator can act on them without the loop spinning forever

#### Scenario: Newly created spec must pass structural validation
- **GIVEN** sync discovery invokes the LLM to generate a brand-new `se3/specs/<name>/spec.md`
- **WHEN** the LLM returns content
- **THEN** the discovery layer calls `validate_spec_structure(content, name)` instead of the legacy "length ≥ 50 chars" heuristic
- **AND** rejects responses that are sub-agent meta summaries (no v1 marker, no `# <name> Specification` heading, no `## Purpose`, no `### Requirement:`, or a narrative-prose first line such as "I have enough context...")
- **AND** the file is not created when validation fails

#### Scenario: Quota exhaustion triggers interactive pause and checkpoint
- **GIVEN** the loop layer counts consecutive infrastructure failures across LLM calls
- **WHEN** a single call returns a quota-exhaustion signal (e.g., `402 Insufficient Balance` or `InfraErrorType.USAGE_LIMIT`) OR the consecutive infrastructure-failure count crosses the configured threshold (default `3`, configurable as `sync.infrastructure_failure_threshold`)
- **THEN** the loop writes a checkpoint to `se3/state/sync_checkpoint.json` containing `checkpoint_version=1`, `started_at`, `round_index`, `max_rounds`, the SHA-256 of each currently in-sync spec under `in_sync_specs`, the `failed_analyses` map, and `reason` set to `quota_exhausted` or `manual_interrupt`
- **AND** prints a status summary (completed specs, current round, in-sync spec names, checkpoint path)
- **AND** blocks waiting for a user keypress: pressing `Enter` resumes the same in-process run; pressing `Ctrl-C` exits cleanly with a message telling the user to re-run `se3 sync --resume`

#### Scenario: --resume continues from checkpoint and rehashes in-sync specs
- **GIVEN** a checkpoint exists at `se3/state/sync_checkpoint.json`
- **WHEN** the user runs `se3 sync --resume`
- **THEN** the loop loads the checkpoint
- **AND** for every entry in `in_sync_specs` re-computes the on-disk spec's SHA-256
- **AND** specs whose hash still matches the checkpoint value are skipped from analysis (they remain considered in-sync)
- **AND** specs whose hash differs are re-analyzed in the resumed round
- **AND** the loop continues from `round_index` with the remaining round budget = `max_rounds - round_index`
- **AND** on successful convergence (or on any normal completion path) the checkpoint file is deleted

#### Scenario: --validate-only audits on-disk specs without invoking the LLM
- **GIVEN** the user runs `se3 sync --validate-only`
- **WHEN** the command executes
- **THEN** every file under `se3/specs/**/spec.md` is read and passed through `validate_spec_structure(content, spec_name)`
- **AND** failing specs are listed with the specific validation errors (missing v1 marker, missing title, missing Purpose, missing Requirement, narrative first line, etc.)
- **AND** the exit code is `0` when all specs pass and `1` when at least one spec fails
- **AND** no sub-agent or LLM call is made under this option

#### Scenario: --validate-only and --resume are mutually exclusive
- **GIVEN** the user supplies both `--validate-only` and `--resume`
- **WHEN** the command parses options
- **THEN** the command exits with a usage error stating the two options cannot be combined

#### Scenario: --validate-only warns when combined with ignored flags
- **GIVEN** the user supplies `--validate-only` together with one or more of `--once`, `--interactive`, or `--show-diff`
- **WHEN** the command parses options
- **THEN** the command emits a warning that the supplied loop-mode flags are ignored under `--validate-only`
- **AND** still proceeds to run the structural validation pass (the combination is permitted, only `--resume` is mutually exclusive)

#### Scenario: Range validation on --max-rounds and --stable-rounds
- **GIVEN** the user supplies `--max-rounds` or `--stable-rounds`
- **WHEN** the command parses options
- **THEN** both values MUST be integers >= 1
- **AND** `--stable-rounds` MUST NOT exceed `--max-rounds`
- **AND** any violation of these constraints produces a usage error and the command exits with a non-zero status without invoking the LLM or starting the sync loop

### Requirement: `se3 sync-respond` Command

The `se3 sync-respond` command SHALL process an MCP call response file produced by `se3 sync --interactive` for high-impact deletions.

**Interface:**
```bash
se3 sync-respond <call-file-path>
```

The call file SHALL have `type: sync_high_impact_deletion`. The only valid decision values are `approve` and `skip`.

#### Scenario: Process high-impact deletion response
- **GIVEN** an MCP call file of type `sync_high_impact_deletion` has been created by `se3 sync --interactive`
- **AND** the user has filled in the `.response` file with `approve` or `skip` per pending deletion
- **WHEN** the user runs `se3 sync-respond <call-file-path>`
- **THEN** for each `approve` decision the engine deletes the named `### Requirement:` block (and its scenarios) from the spec
- **AND** for each `skip` decision the deletion is recorded as deferred and the spec block is left intact for this round
- **AND** responses with invalid decision values (not `approve` or `skip`) are skipped
- **AND** responses referencing unknown deletion IDs (not present in the original call file) are skipped
- **AND** after responses are applied, the user is expected to re-run `se3 sync` to continue the loop (the response itself does not auto-resume the loop)

### Requirement: Sync Operation Permission Limits

`se3 sync` SHALL only directly modify spec files (`se3/specs/`). It SHALL NOT modify project source code, SHALL NOT create or modify issues, and SHALL NOT touch any other runtime files. If the code itself looks wrong, the wrongness will be honestly reflected in the updated spec where a human reviewer can spot it — sync does not act as a guardian of code correctness.

### Requirement: `se3 merge` Command

The `se3 merge` command SHALL sequentially merge one or more named branches into the current branch, targeting same-repo multi-task parallel aggregation. Branches are merged pairwise in the order given (no octopus merge); the command is unaware of the source workflow that produced each branch and coexists with `se3 run --loop --merge` (which remains the in-loop single-branch path).

**Interface:**
```bash
se3 merge <branch> [<branch> ...] [--strategy fast|safe|strict] [--delete-merged | --no-delete-merged]
```

**Option aliases:** The long-form options above accept short aliases for ergonomic use:
| Long form | Short alias |
|-----------|-------------|
| `--strategy` | `-s` |
| `--delete-merged` | `-d` |

When `--strategy` is omitted, the default tier is **`fast`**. The legacy strategy names `default` and `robust` have been removed; passing them to `--strategy` (or setting them in `se3.yaml`'s `merge.strategy`) SHALL be rejected fail-fast with a migration hint pointing at the new name (`safe` replaces `default`; `fast` replaces `robust`). No deprecation-silent alias is provided.

**Behavior contract:**

1. **Sequential pairwise merge.** Git owns the merge topology. For each branch in argument order, the command runs `git merge <branch>` against the current HEAD. The minimum unit of conflict resolution is a single `git merge` invocation: all conflicting files of that one merge are handed to the LLM in a single call, written back, committed, and only then does the next branch start.

2. **Conflict-resolution context contract.** When a `git merge` reports conflicts, the LLM call SHALL receive at minimum:
   - Merge metadata: ours/theirs branch names, merge-base commit, both HEAD commit hashes and messages.
   - For every conflicting file: the full base/ours/theirs three-way contents (`git show :1:`/`:2:`/`:3:`) plus the working-tree file with `<<<<<<<` / `=======` / `>>>>>>>` markers.
   - The path and hunk line ranges of each conflict.
   - The selected strategy tier (`fast` / `safe` / `strict`).

   The call SHOULD additionally receive `git log <merge-base>..<theirs>` and `git log <merge-base>..<ours>` (oneline), a flag identifying spec files (subject to spec-guardrails), and a project-conventions summary.

3. **LLM-as-editor output.** The LLM SHALL directly edit the working-tree conflict files (e.g., via an `Edit` tool) to remove every `<<<<<<<` / `=======` / `>>>>>>>` marker; it MUST NOT return a JSON `decision` field or a `resolved_content` blob for the orchestrator to splice in. The single batched call is the unit of work — all conflict files of one `git merge` invocation are passed in one call (see the flow-engine spec's `se3 merge` Conflict Resolution Mechanism Requirement). After each round, the orchestrator scans every target file for residual markers; files that still contain a marker form the next round's batch. The cap is `merge.max_conflict_resolve_iterations` (default 10). The merge SHALL NEVER fall back to take-theirs / take-ours under any failure mode (context-build error, LLM exception, parse failure, write failure, iteration-cap exhaustion).

4. **Three strategy tiers (`fast` is the default):**
   | Tier | Behavior |
   |------|----------|
   | `fast` (default) | LLM-as-editor resolves conflicts in batched rounds up to `merge.max_conflict_resolve_iterations`. On cap exhaustion the merge exits with a failure — no human call and no take-theirs. Inherits the original `robust`-strategy dirty-worktree behavior: stashes a dirty working tree before the merge and pops the stash back on failure rollback. Post-merge guardrails violation still feeds back into the LLM repair loop (see "Fast-Mode Guardrail Repair Stall Escalation"). |
   | `safe` | LLM-as-editor resolves conflicts in batched rounds up to `merge.max_conflict_resolve_iterations`. On cap exhaustion the merge escalates to a human MCP call (`reviewer: human`): the user edits the residual files until every conflict marker is gone, and the merge then resumes. Requires a clean working tree before starting (no built-in stash path). Never falls back to take-theirs. Post-merge guardrails violation → rollback + human call. |
   | `strict` | LLM is NOT invoked for conflicts at all. Every conflict (and every post-merge guardrails violation) escalates directly to a human MCP call from the first iteration. Never falls back to take-theirs. |

   <!-- Preserved original table row for guardrails compatibility:
   | `strict` | Accept only when every hunk is high-confidence AND guardrails pass; otherwise raise a human call. |
   -->

5. **Spec-guardrail enforcement.** Whenever a merge touches a `se3/specs/**/spec.md` file (whether or not it had a textual conflict), the merge product SHALL be re-checked by `se3 guardrails`. The check is mandatory in all three tiers. Violations (deleted requirements, weakened language SHALL→SHOULD, weakened quantifiers all→some, deleted scenarios) cause the merge to be rolled back and escalated to a human call.

6. **Failure handling.** A merge that cannot be accepted (rejected by strategy, guardrails violation, LLM failure) defaults to `git merge --abort`, restoring the working tree. Branches successfully merged earlier in the sequence are preserved.

7. **SemVer aggregation after merge.** After all branches are processed, the per-branch SemVer bump types (patch/minor/major) are reduced via SemVer's max rule and a single `pyproject.toml` update is amended onto the last merge commit. Each per-branch bump type is computed as an **end-to-end diff** from the version at that branch's merge-base (the commit where the branch diverged from the current branch) to the version at the branch tip; intra-branch intermediate bumps are NOT accumulated (symmetric with the cross-branch max rule, and robust to noisy intermediate version commits). The **application base** for the chosen aggregated bump is the current branch's pre-merge version (which may be ahead of any branch's merge-base version). Example: pre-merge `4.6.0`, branch `B` whose merge-base version is `4.4.0` and tip `4.4.1` (PATCH end-to-end), branch `C` whose merge-base version is `4.4.0` and tip `4.6.0` (MINOR end-to-end, even if its history walked through `4.5.0` → `4.5.1` → `4.6.0`) → `max(PATCH, MINOR) = MINOR`, applied to `4.6.0` yields `4.7.0`. Per-branch historical commits are NOT rewritten — SemVer uniqueness is guaranteed by tags.

8. **Branch and worktree cleanup.** Default behavior is to delete merged branches and archive their worktrees to `.se3/archive/`. Pass `--no-delete-merged` to keep them. When deletion runs (either because the default applies or because `--delete-merged` is explicitly given):
   - Each merged branch is removed via `git branch -d` (lowercase) so that branches not reachable from HEAD are not silently destroyed.
   - If a branch has a bound git worktree, the worktree is first archived to `<project_root>/.se3/archive/<slug>-<ts>/` along with an `.se3-archive-meta.json` capturing the HEAD SHA, then `git worktree remove` is called when the worktree is clean (`git status --porcelain` empty); when dirty the cleanup is refused with an error and `--force` is NEVER used.
   - The current branch and `main`/`master` are NEVER deleted.

9. **Infrastructure reuse.** Execution logs go to `se3/logs/`. Human-decision artifacts go to `se3/calls/` as MCP call files (e.g., `se3/calls/merge_<timestamp>_<branch>.json`), consistent with `se3 sync` and the existing `merge_loop_branch` flow.

**Out of scope (first version):** octopus merge (git's strategy supports only conflict-free combinations); single LLM call resolving multiple branches simultaneously (no ground truth); cross-branch hunk-level batching (git does not support partial layered merges); rewriting per-branch historical commits' versions; auto-deciding which branches to merge (the list MUST be explicit); injecting unrelated full-file context or historical-merge few-shot examples into the LLM prompt (possible later enhancement).

#### Scenario: Successful sequential merge with no conflicts
- **GIVEN** branches `feat/a`, `feat/b`, `feat/c` all merge cleanly into the current branch
- **WHEN** user runs `se3 merge feat/a feat/b feat/c`
- **THEN** each branch is merged in order, producing one merge commit per branch
- **AND** the aggregated SemVer bump (max of each branch's bump type) is applied as a single `pyproject.toml` update amended onto the last merge commit

#### Scenario: Conflict resolved automatically in safe strategy
- **GIVEN** merging `feat/x` produces text conflicts in non-spec files
- **AND** the LLM-as-editor loop removes every `<<<<<<<` / `=======` / `>>>>>>>` marker within `merge.max_conflict_resolve_iterations` rounds
- **WHEN** strategy is `safe`
- **THEN** the cleaned working-tree files are staged and committed
- **AND** the merge proceeds to the next branch
- **AND** the orchestrator does NOT invoke any take-theirs / take-ours fallback at any point

#### Scenario: Iteration cap escalates safe to human call
- **GIVEN** the LLM-as-editor loop reaches `merge.max_conflict_resolve_iterations` with at least one file still containing a conflict marker
- **WHEN** strategy is `safe`
- **THEN** an MCP call file is created at `se3/calls/merge_<timestamp>_<branch>.json` containing the residual conflict files, the merge context, and the iteration history
- **AND** the user is expected to edit the residual files until no conflict marker remains, at which point the merge resumes
- **AND** subsequent branches in the argument list are NOT attempted while the human call is pending
- **AND** the orchestrator does NOT invoke any take-theirs / take-ours fallback

#### Scenario: Strict strategy skips LLM and escalates directly to human
- **GIVEN** a merge produces conflicts in any file
- **WHEN** strategy is `strict`
- **THEN** the LLM is NOT invoked for conflict resolution
- **AND** a human call is created directly at `se3/calls/` from the first iteration
- **AND** previously successfully merged branches in the same invocation are preserved
- **AND** the orchestrator does NOT invoke any take-theirs / take-ours fallback

#### Scenario: Fast strategy still enforces guardrails on spec files
- **GIVEN** a merge produces a change to `se3/specs/foo/spec.md` that weakens a SHALL to SHOULD
- **WHEN** strategy is `fast`
- **THEN** the guardrails check fails after the merge commit
- **AND** the violation list is fed to the LLM to repair the spec file (in a bounded repair loop, see "Fast-Mode Guardrail Repair Stall Escalation")
- **AND** if the LLM repair succeeds, the merge commit is amended with the corrected spec
- **AND** if the LLM repair fails *and* repair iterations are still making progress, the merge is aborted without creating a human call
- **AND** if the LLM repair *stalls* (no-progress detection fires), the merge is escalated to a human call instead of aborting
- **AND** fast does NOT bypass spec guardrails detection

#### Scenario: Fast strategy exits without human fallback when cap is reached
- **GIVEN** merging `feat/z` produces text conflicts and `merge.max_conflict_resolve_iterations` rounds of LLM-as-editor leave at least one residual conflict marker
- **WHEN** strategy is `fast`
- **THEN** the merge exits with a failure
- **AND** no human MCP call is created
- **AND** the orchestrator does NOT invoke any take-theirs / take-ours fallback

#### Scenario: Default strategy when --strategy is omitted is `fast`
- **GIVEN** the user runs `se3 merge feat/x` with no `--strategy` argument and no `merge.strategy` override in `se3.yaml`
- **WHEN** the CLI resolves the active strategy
- **THEN** the effective strategy is `fast`

#### Scenario: Removed `default` strategy name rejected fail-fast
- **GIVEN** the user runs `se3 merge feat/x --strategy default` (or sets `merge.strategy: default` in `se3.yaml`)
- **WHEN** strategy is `default`
- **THEN** the command exits immediately with a configuration error pointing the user at the replacement strategy `safe`
- **AND** no `git merge` is attempted

#### Scenario: Removed `default` strategy in se3.yaml rejected at load time
- **GIVEN** `merge.strategy: default` is present in se3.yaml
- **WHEN** strategy is `default`
- **THEN** the framework raises `ConfigError` before any `se3 merge` invocation runs

#### Scenario: Removed `robust` strategy name rejected fail-fast
- **WHEN** the user runs `se3 merge feat/x --strategy robust` (or sets `merge.strategy: robust` in `se3.yaml`)
- **THEN** the command exits immediately with a configuration error pointing the user at the replacement strategy `fast`
- **AND** no `git merge` is attempted

#### Scenario: Branch cleanup default is delete-and-archive
- **GIVEN** `feat/x` was merged successfully and has a bound worktree with a clean working tree
- **WHEN** the user runs `se3 merge feat/x` with no `--delete-merged` / `--no-delete-merged` flag and no `merge.delete_merged_default` override
- **THEN** the worktree is archived to `<project_root>/.se3/archive/<slug>-<ts>/` with `.se3-archive-meta.json` capturing the HEAD SHA
- **AND** `git branch -d feat/x` removes the branch
- **AND** `main`/`master` and the current branch are never touched

#### Scenario: --no-delete-merged keeps branch and worktree
- **GIVEN** `feat/y` was merged successfully and has a bound worktree
- **WHEN** the user passes `--no-delete-merged`
- **THEN** the branch is NOT deleted
- **AND** the worktree is NOT archived or removed

#### Scenario: Branch cleanup with --delete-merged
- **GIVEN** `feat/x` was merged successfully and has no bound worktree
- **WHEN** the user passes `--delete-merged`
- **THEN** `git branch -d feat/x` removes the branch
- **AND** `main`/`master` and the current branch are never touched

#### Scenario: Cleanup refuses to delete a dirty worktree
- **GIVEN** `feat/y` was merged and has a bound worktree containing uncommitted changes
- **WHEN** `--delete-merged` is in effect
- **THEN** the cleanup reports an error for `feat/y`, leaves both worktree and branch intact, and does NOT use `git worktree remove --force`

#### Scenario: Failure messages distinguish conflict vs guardrails categories
- **GIVEN** a merge fails for any reason
- **WHEN** the CLI renders the failure summary and the log file is written
- **THEN** the message clearly identifies the failure category, distinguishing at minimum:
  - `git merge conflict (could not be resolved)` — text conflicts the resolver could not handle
  - `post-merge guardrails violation` — spec guardrails rejected the merge result
  - `failed to build conflict context` — the resolver could not even prepare conflict input (strategy-neutral phrasing applies to `fast`, `safe`, and `strict`)
  - `runtime_sync_collision` — post-merge runtime data synchronization (see "`se3 merge` Runtime Data Synchronization") detected a tier A relative-path collision in strict mode and halted the sequence
  - fast-mode aborts (`fast strategy could not resolve conflict`, `fast strategy could not auto-repair guardrails violation`, `fast strategy LLM resolution failed`)
- **AND** the same category labels are used in the CLI summary and the corresponding log entry, so that users do not confuse a guardrails-driven failure with an unresolved git conflict

#### Scenario: Human call required but call file cannot be written
- **GIVEN** the merge needs to escalate to a human call (iteration-cap exhaustion in `safe`, first-iteration escalation in `strict`, post-merge guardrails violation in `safe`/`strict`, etc.)
- **AND** writing the MCP call file fails (filesystem error, permission issue, etc.)
- **WHEN** the merge command finalizes the report
- **THEN** the report is treated as an outright failure rather than a pending-human state
- **AND** the CLI exits with the general-failure code rather than the interrupted/paused code, because there is no call file for the user to respond to with `se3 merge-respond`
- **AND** the summary explicitly states that the human call file could not be written

### Requirement: `se3 merge` Runtime Data Synchronization

After each successful `git merge` of a branch, `se3 merge` SHALL synchronize gitignored runtime content under `se3/` from the merged branch's bound worktree into the current branch's project root. Because `git merge` only handles tracked files, runtime data excluded by `.gitignore` (logs, history, archived state, etc.) would otherwise be silently dropped. The synchronization is partitioned into three tiers with distinct semantics.

**Tier A — Append-with-collision (relative-path keyed, structure preserved):**
- Paths: `se3/history/`, `se3/logs/`, `se3/state/archive/`, `se3/collab/tasks/`, plus the direct-children glob patterns `se3/state/summary-*` and `se3/calls/confirm_*`.
- Semantics: For every file under these tier A paths in the source worktree, copy it into the same relative path under the current project root only if no file already exists at that target relative path. Collisions are tested at the **relative-path** level — files with the same base name in different subdirectories do NOT collide.
- Collision policy (lenient mode, default): When a tier A relative path is already present in the current `se3/` with different content, the source version SHALL be written to a sidecar file `<dest>.from-<branch>` in the same directory. The target file remains unchanged. The collision is recorded in the merge report with source branch, original path, sidecar path, and both content hashes for auditability. The merge sequence SHALL continue with the next branch. Sidecar self-collisions (the sidecar file already exists) are handled by idempotency check (identical content = no-op) or hash-suffix disambiguation (`<dest>.from-<branch>.<short_hash>`).
- Collision policy (strict mode): When `merge.strict_runtime_sync: true` is configured, a tier A relative-path collision with different content SHALL halt the entire `se3 merge` invocation with the `runtime_sync_collision` failure category. The just-completed git merge commit is NOT rolled back; subsequent branches in the argument list are NOT attempted.
- Idempotency: When the source and destination files have identical content (byte-for-byte), the destination is treated as already-synced and the file is skipped silently rather than being reported as a collision. This allows safe re-runs of `se3 merge` against the same branch.

**Tier B — Discard branch-side (preserve current state):**
- Paths: `se3/state/engine.json`, `se3/state/known_test_failures.json`, `se3/calls/active/`.
- Semantics: The current project's tier B content is preserved as-is; the merged branch's tier B content is recorded as discarded but NOT copied. Rationale: these files describe live flow-engine runtime state and overwriting them would corrupt the current run.

**Tier C — Skip entirely (neither read nor written):**
- Paths: `se3/cache/`, `se3/tmp/`, `se3/worktrees/`.
- Semantics: Tier C content is ignored on both sides. These are derived caches or nested worktree pointers that have no meaningful merge.

**Source-worktree absence:** When the merged branch has no bound worktree, or the worktree's filesystem path is missing (e.g. force-removed externally), runtime sync logs a warning and skips that branch's sync without treating it as a failure. The runtime data is simply unavailable; the merge sequence continues.

#### Scenario: Tier A files synced from merged branch
- **GIVEN** branch `feat/x` has files `se3/history/run-001.json` and `se3/logs/2026-04-30.log` in its bound worktree
- **AND** the current project's `se3/` has no files at those relative paths
- **WHEN** `se3 merge feat/x` completes the git merge
- **THEN** both files are copied into the current project root at their original relative paths

#### Scenario: Tier A collision bypassed by sidecar in lenient mode
- **GIVEN** branch `feat/y` has `se3/history/run-001.json` in its worktree with different content from the current project's `se3/history/run-001.json`
- **AND** `merge.strict_runtime_sync` is not set (defaults to lenient mode)
- **WHEN** `se3 merge feat/y feat/z` runs and the git merge of `feat/y` succeeds
- **THEN** the runtime sync detects the collision and writes the source version to `se3/history/run-001.json.from-feat/y` (with `/` in the branch name replaced by `__`)
- **AND** the current project's `se3/history/run-001.json` remains unchanged
- **AND** the collision is recorded in the merge report with branch `feat/y`, original path `history/run-001.json`, sidecar path, and both content hashes
- **AND** the merge sequence continues to `feat/z`

#### Scenario: Strict mode preserves halt behavior on tier A collision
- **GIVEN** branch `feat/y` has `se3/history/run-001.json` in its worktree with different content from the current project's `se3/history/run-001.json`
- **AND** `merge.strict_runtime_sync: true` is configured
- **WHEN** `se3 merge feat/y feat/z` runs and the git merge of `feat/y` succeeds
- **THEN** the runtime sync detects the collision and the entire invocation halts with the `runtime_sync_collision` failure category
- **AND** the just-completed merge commit for `feat/y` is preserved (not aborted)
- **AND** `feat/z` is NOT attempted

#### Scenario: Identical-content tier A file is treated as idempotent no-op
- **GIVEN** branch `feat/q` has `se3/history/run-007.json` whose content is byte-for-byte identical to the current project's `se3/history/run-007.json`
- **WHEN** `se3 merge feat/q` runs runtime sync
- **THEN** the file is skipped without being reported as a collision
- **AND** the merge sequence proceeds to the next branch

#### Scenario: Tier B preserves current runtime state
- **GIVEN** branch `feat/a` has its own `se3/state/engine.json` and entries under `se3/calls/active/`
- **WHEN** `se3 merge feat/a` completes
- **THEN** the current project's `se3/state/engine.json` and `se3/calls/active/` are unchanged
- **AND** `feat/a`'s tier B content is recorded as discarded but not copied

#### Scenario: Tier C is not touched
- **GIVEN** branch `feat/b` has `se3/cache/index.db` and `se3/tmp/scratch.txt` in its worktree
- **WHEN** `se3 merge feat/b` completes
- **THEN** runtime sync neither reads nor writes any tier C path

#### Scenario: Source worktree missing
- **GIVEN** branch `feat/c` has no bound worktree (or its worktree directory has been removed externally)
- **WHEN** `se3 merge feat/c` completes the git merge
- **THEN** runtime sync logs a warning and skips that branch's sync
- **AND** the merge sequence continues normally with subsequent branches

### Requirement: Fast-Mode Guardrail Repair Stall Escalation

The fast-strategy post-merge guardrail repair loop SHALL detect when the LLM is no longer making progress and escalate to a human MCP call instead of aborting.

**Stall detection contract:**

1. After each guardrail repair iteration, the orchestrator SHALL compute a deterministic hash of the current violation set, derived from `(file, violation_type, normalized_message)` triples sorted to be order-insensitive.
2. When two consecutive repair iterations produce the *same* violation-set hash (the LLM's repair did not change the violation set), the orchestrator SHALL stop further repair attempts and treat the situation as a *stall*.
3. On stall, the merge SHALL NOT be aborted. Instead, the orchestrator SHALL write a human MCP call file under `se3/calls/` with a distinct call type (e.g., `guardrail_repair_stalled`) and route the merge to a `pending_human` state, consistent with how the `safe` and `strict` tiers escalate guardrail violations.
4. The stalled-repair call file SHALL embed the structured detector evidence from each violation (paired strong/weak lines, line numbers, branch identification) so the human reviewer can act without re-running the detector.
5. If repair iterations are still changing the violation set but the maximum iteration cap is exhausted, fast mode SHALL fall back to the original abort-without-human-call behavior — only the *stall* condition triggers escalation.
6. The repair loop's per-iteration semantics (one LLM call per iteration) live in the orchestrator/strategy layer, not inside the single-call repair primitive.

**Rationale:** A persistently false-positive detector or a degenerate repair prompt can otherwise burn many minutes of LLM time looping without convergence; the stall exit lets a human resolve the disagreement quickly while preserving fast mode's "no human in the loop unless absolutely necessary" guarantee for the common case.

#### Scenario: Repair loop stalls on repeated identical violation set
- **GIVEN** a fast-mode merge whose post-merge guardrails check reports a violation
- **AND** the LLM repair iteration produces a fix that the detector still rejects with the *same* violation set on the next iteration
- **WHEN** the orchestrator hashes the new violation set and finds it equal to the previous iteration's hash
- **THEN** further repair attempts are stopped at the second consecutive identical hash
- **AND** a `guardrail_repair_stalled` MCP call file is written under `se3/calls/` with the detector evidence
- **AND** the merge enters `pending_human` rather than aborting

#### Scenario: Repair loop makes progress but never converges
- **GIVEN** a fast-mode merge whose post-merge guardrails check reports a violation
- **AND** each LLM repair iteration produces a *different* violation set (hash keeps changing)
- **WHEN** the iteration cap is reached without the violation set becoming empty
- **THEN** the merge is aborted with the standard fast-mode "could not auto-repair guardrails violation" failure
- **AND** no human call file is created (stall escalation does not apply)

### Requirement: `se3 merge` Concurrency Lock

`se3 merge` SHALL serialize concurrent invocations within the same project root via an exclusive non-blocking file lock at `se3/state/merge.lock`. A second `se3 merge` invoked while another is in progress SHALL fail immediately with the `lock_busy` failure category rather than queue or wait.

**Lock contract:**
- The lock file records the holder process's PID.
- A new invocation acquires the lock with `fcntl.flock(LOCK_EX | LOCK_NB)`; on contention, the call surfaces `MergeLockBusy` and the CLI exits with the general-failure code.
- A lock file whose recorded PID no longer exists is considered stale and MAY be reclaimed (with jittered backoff to avoid thundering herd) and the failure category is `lock_stale` if reclamation itself fails.
- The lock is released automatically on process exit, context-manager exit, or explicit release.
- An inner caller (e.g. the orchestrator) that detects the lock is already held by the *same* process MUST skip re-acquisition rather than risk a same-process flock collision.

#### Scenario: Concurrent merge rejected
- **GIVEN** an `se3 merge` invocation is in progress and currently holds `se3/state/merge.lock`
- **WHEN** a second `se3 merge` is launched in the same project root
- **THEN** the second invocation exits immediately with the `lock_busy` failure category
- **AND** the first invocation continues unaffected

#### Scenario: Stale lock reclaimed
- **GIVEN** `se3/state/merge.lock` records a holder PID that no longer exists in the OS process table
- **WHEN** a new `se3 merge` is invoked
- **THEN** the stale lock is reclaimed and the merge proceeds normally

### Requirement: `se3 merge` Success Post-Conditions

Every branch that `se3 merge` reports as successfully merged SHALL pass three independent post-condition checks before the per-branch outcome is finalized. A violation produces a typed failure in the `postcond_*` family and halts the sequence; subsequent branches SHALL NOT be attempted.

**Post-conditions:**
1. **Ancestry** — `git merge-base --is-ancestor <branch> HEAD` returns 0 (the branch is reachable from HEAD). A `1` returncode produces `postcond_branch_not_merged`; any other non-zero returncode (git error, signalled child) produces `postcond_branch_unresolvable` rather than a definitive merge-loss diagnosis.
2. **Merge commit** — HEAD has at least 2 parents. This check is skipped when the branch was already an ancestor of HEAD before the merge attempt (a no-op produces no merge commit). A failure is reported as `postcond_head_not_merge_commit`.
3. **Version bumped** — when version aggregation ran, the on-disk version (read with size and duration caps via O_NOFOLLOW) is strictly greater than the pre-merge version. A failure is reported as `postcond_version_not_bumped`.

A timeout while reading the version file or running `git merge-base --is-ancestor` produces `postcond_check_timeout` rather than a silent skip.

#### Scenario: Branch reported merged but is not an ancestor of HEAD
- **GIVEN** a merge step reports success for branch `feat/x`
- **AND** post-condition checks find `git merge-base --is-ancestor feat/x HEAD` returns 1
- **WHEN** the orchestrator finalizes the per-branch outcome
- **THEN** the outcome is failed with `postcond_branch_not_merged`
- **AND** subsequent branches in the argument list are NOT attempted

#### Scenario: Merge commit lost after guardrail repair
- **GIVEN** a guardrail repair step produces a HEAD that has only one parent (the merge commit was overwritten)
- **WHEN** the post-condition check runs
- **THEN** the outcome is failed with `postcond_head_not_merge_commit`

#### Scenario: Version aggregation reports success but on-disk version unchanged
- **GIVEN** the version aggregator returns `success=True` but the on-disk version equals the pre-merge version
- **WHEN** the post-condition check runs
- **THEN** the outcome is failed with `postcond_version_not_bumped`

### Requirement: `se3 merge` Repository State Preconditions

`se3 merge` SHALL fail-fast — before attempting any merge — when the repository is in an unsupported state. Each rejection produces a typed failure category drawn from `repo_empty`, `repo_detached_head`, `repo_shallow`, `repo_unsupported_state`.

**Rejected states:**
- **Empty repository** — no commits on the current branch.
- **Detached HEAD** — `git symbolic-ref HEAD` fails to resolve a branch name.
- **Shallow clone** — `.git/shallow` exists, since `git merge-base` and ancestry post-conditions cannot give correct verdicts on truncated history.

#### Scenario: Detached HEAD rejected
- **GIVEN** the project root has detached HEAD
- **WHEN** the user runs `se3 merge feat/x`
- **THEN** the command exits with `repo_detached_head` failure category
- **AND** no git merge is attempted

#### Scenario: Shallow clone rejected
- **GIVEN** the project root is a shallow clone (`.git/shallow` exists)
- **WHEN** the user runs `se3 merge feat/x`
- **THEN** the command exits with `repo_shallow` failure category

### Requirement: `se3 merge` Input Validation

`se3 merge` SHALL validate branch arguments before invoking any git command and reject malformed or unsafe inputs with typed failure categories.

**Validation rules:**
- An empty branch list exits with `no_branches` failure category. Pass-through to the orchestrator with zero loops is NOT permitted (it would falsely report success on no-op input).
- Branch names beginning with `-` (which git would interpret as an option) are rejected.
- Branch names containing shell metacharacters or unprintable characters that could be exploited via subprocess invocation are rejected.
- Branch existence is validated via `git show-ref --verify` whose returncode is explicitly checked; a non-existent branch is rejected before the merge step is entered.

#### Scenario: Empty branch list rejected
- **WHEN** the user runs `se3 merge` with no branch arguments
- **THEN** the command exits with `no_branches` failure category and a non-zero exit code
- **AND** no merge work is performed

#### Scenario: Leading-dash branch name rejected
- **WHEN** the user runs `se3 merge -- --force` or `se3 merge -delete`
- **THEN** the command rejects the branch name without invoking git
- **AND** the failure message identifies the unsafe argument

#### Scenario: Output distinguishes newly-merged from already-merged
- **GIVEN** an `se3 merge` invocation that includes both branches that produced new merge commits and branches that were already ancestors of HEAD
- **WHEN** the CLI prints the summary
- **THEN** newly-merged branches and already-ancestor branches are listed in distinct buckets
- **AND** the wording does not conflate the two categories

### Requirement: `se3 merge` LLM Call Tracing

Every LLM call issued during `se3 merge` (conflict resolution, guardrail repair, version analysis prompts, etc.) SHALL be recorded as a JSON-Lines record under `se3/logs/llm/`. Trace files SHALL be named `merge_<timestamp>_<seq>.jsonl` and rotate when a single file exceeds an implementation-defined size cap so a long-running merge does not produce an unbounded single file.

**Per-record fields (minimum):**
- Sequence number, ISO timestamp, agent identifier.
- Prompt preview and response preview (truncated; full prompts live elsewhere if needed).
- Duration in seconds.
- Outcome (`success`, `error`, `timeout`, `retry`, `cancelled`).
- Optional error detail.
- Free-form metadata dict (model name, temperature, etc.).

Trace files SHALL be append-only and fsync'd after each record. Concurrent writes within a single process SHALL be serialized via a threading lock; cross-process concurrency is prevented by the merge lock requirement above.

The orchestrator SHALL share a single `LLMCaller` instance across `ConflictResolver` and `GuardrailRepairer` so that prompt-cache reuse and per-call quota are shared across steps.

#### Scenario: Each LLM call produces a trace record
- **GIVEN** a merge whose conflict resolution and guardrail repair each issue one LLM call
- **WHEN** the merge completes
- **THEN** `se3/logs/llm/merge_<timestamp>_<seq>.jsonl` contains at least two records, one per call
- **AND** each record carries the agent name, duration, and outcome fields

### Requirement: `se3 merge` Secret Redaction in Logs

Diffs, prompts, and trace records written by `se3 merge` SHALL pass through a secret redactor before persistence. The redactor masks common credential patterns including API keys (`sk-...`, `ak-...`), GitHub personal access tokens (`ghp_...`), PyPI / npm tokens, Bearer header values, and `password` fields in TOML / JSON / YAML.

An optional allowlist MAY exempt specific keys or patterns from redaction (e.g. test fixtures that intentionally contain dummy tokens).

#### Scenario: API key in diff is redacted before logging
- **GIVEN** a conflict diff that contains the literal string `sk-1234567890abcdef`
- **WHEN** the diff is written to the merge log file or LLM trace record
- **THEN** the persisted text replaces the secret with a masked form (e.g. `sk-***`)

### Requirement: `se3 merge` Amend Safety Contract

Any `git commit --amend` performed by `se3 merge` (version aggregation, guardrail repair, etc.) SHALL save the pre-amend HEAD SHA before the amend so that rollback uses `git reset --soft <pre_amend_sha>` rather than `git reset --soft HEAD~1`. Direct `git reset --soft HEAD~1` after amending a merge commit is prohibited because `HEAD~1` then points to the merge commit's first parent (the pre-merge HEAD) rather than to the merge commit itself, silently discarding the entire merge.

**Repair-path preference:**
- The guardrail repair step SHALL prefer creating a *fix-up commit* on top of the merge commit (a new commit whose parent is the merge commit). The amend path is reserved as a last-resort.
- Before any amend, the repair step SHALL assert that HEAD is still a merge commit (`git rev-parse HEAD^2` succeeds) and abort with `inconsistent_repair_state` if not.
- The repair-stall detector SHALL initialize the stall-tracking hash to a sentinel value (not the empty string) so that the first iteration's hash cannot accidentally match it and bypass the stall check.

A new Requirement (the version aggregator) similarly atomic-writes `pyproject.toml` via temp-file + `os.replace` and restores the original file content + `git reset HEAD pyproject.toml` on every failure path.

#### Scenario: Guardrail repair rollback uses pre-amend SHA
- **GIVEN** the guardrail repair step uses the amend path on a merge commit
- **AND** the repair fails the post-repair guardrails re-check
- **WHEN** the orchestrator rolls back
- **THEN** rollback uses `git reset --soft <pre_amend_sha>` (the SHA captured before the amend)
- **AND** the merge commit is preserved (not discarded by `HEAD~1` over-reset)

#### Scenario: Repair detects HEAD is no longer a merge commit
- **GIVEN** an external process modified HEAD between merge and repair
- **AND** HEAD now has only one parent
- **WHEN** the guardrail repair step attempts to amend
- **THEN** the step refuses to amend and reports `inconsistent_repair_state`

### Requirement: `se3 merge` Typed Failure Reasons

`se3 merge` SHALL report failures via a closed enumeration (typed `FailureReason`). Each reason has a stable lower-case string spelling for legacy compatibility and a numeric grouping by subsystem so new values can be added without renumbering existing ones.

**Subsystem groupings:**
| Range | Family |
|-------|--------|
| `0xx` | Clean exit / no failure |
| `1xx` | Pending-human (paused) |
| `2xx` | Git-level merge failures |
| `3xx` | Conflict resolution / LLM resolution |
| `4xx` | Fast-strategy aborts |
| `5xx` | Guardrail violations and repair |
| `6xx` | Rollback failures |
| `7xx` | Human-call write failures |
| `8xx` | Runtime data sync failures |
| `9xx` | Post-condition violations |
| `91x` | Silent merge loss / timeout variants |
| `92x` | Version anomaly (already-at-target / higher-than-target) |
| `93x` | Unsupported repository state |
| `98x` | Input validation |
| `99x` | Lock contention |

`MergeReport` SHALL expose both the legacy string and the typed enum (`failure_reason_enum` property) so consumers can switch on the typed value while persistence keeps the stable string surface. Compound diagnostic strings (e.g. `"fast_abort: <stderr>"`) SHALL be decomposed into a base reason plus a separate `failure_detail` string rather than embedded in the reason field. `MergeReport` SHALL also expose three semantically distinct branch-outcome buckets — `newly_merged_branches`, `already_ancestor_branches`, `merged_with_warnings` — replacing the legacy overloaded `merged_branches` aggregate (which is preserved as a backward-compatible aggregate).

#### Scenario: Failure reason carries typed enum and string spelling
- **GIVEN** a merge fails with a guardrail violation that the LLM repair could not fix
- **WHEN** the report is persisted
- **THEN** the persisted JSON contains the legacy string `guardrail_repair_failed`
- **AND** the in-memory `MergeReport.failure_reason_enum` returns `FailureReason.GUARDRAIL_REPAIR_FAILED`

### Requirement: `se3 merge-respond` Command

The `se3 merge-respond` command SHALL process an MCP call response file produced by `se3 merge` when conflicts were escalated for human decision.

**Interface:**
```bash
se3 merge-respond <call-file-path>
```

#### Scenario: Process merge call response
- **GIVEN** an MCP call file has been created by `se3 merge`
- **AND** the user has filled in the `.response` file with resolutions or directives
- **WHEN** user runs `se3 merge-respond <call-file-path>`
- **THEN** the engine consumes the response and resumes the merge sequence (re-applying the resolved contents or skipping the merge per the user's decision)

### Requirement: `se3 issue` Command

The `se3 issue` command group SHALL provide subcommands for managing SE3 project issues. Invoking `se3 issue` without a subcommand SHALL default to listing open issues.

**Interface:**
```bash
se3 issue                              # List open issues (default)
se3 issue list                         # List open issues
se3 issue list --all                   # List all issues including closed
se3 issue list --type <type>           # Filter by issue type
se3 issue show <id>                    # Show detailed information about an issue
se3 issue create                       # Create a new issue interactively
se3 issue reset <id>                   # Reset an in-progress issue back to open
```

**Project root resolution:** The command walks up from the current working directory looking for `.git` or any SE3 config file (`se3.yaml`, `se3.local.yaml`, `se3.config.yaml`). If none is found, the current working directory is used.

**Option aliases:** The long-form options on `se3 issue list` accept short aliases for ergonomic use:
| Subcommand | Long form | Short alias |
|------------|-----------|-------------|
| `list` | `--all` | `-a` |
| `list` | `--type` | `-t` |

**Issue fields and rendering:**
- Each issue carries an ID, title, type, status, priority, tags, description, created timestamp, and updated timestamp.
- Status values include `open`, `in_progress`, `resolved`, `wont_fix`, and `closed`.
- Lists render via a Rich table whose columns are ID, Title (truncated to 50 characters with an ellipsis when longer), Type, Status, Priority, Tags, and Created.
- Status, priority, and type are color-coded for readability.

#### Scenario: Default invocation lists open issues
- **WHEN** the user runs `se3 issue` with no subcommand
- **THEN** the command lists open issues (equivalent to `se3 issue list`)

#### Scenario: List all issues including closed
- **WHEN** the user runs `se3 issue list --all`
- **THEN** the table includes resolved, closed, and won't-fix issues in addition to open ones

#### Scenario: Filter list by type
- **WHEN** the user runs `se3 issue list --type bug`
- **THEN** only issues with type `bug` are displayed

#### Scenario: Empty list message
- **GIVEN** no issues match the active filter
- **WHEN** the user runs `se3 issue list` (with or without `--all` / `--type`)
- **THEN** the command prints a "No issues found" style message instead of rendering an empty table

#### Scenario: Show issue details
- **GIVEN** an issue with ID `<id>` exists
- **WHEN** the user runs `se3 issue show <id>`
- **THEN** the command renders the issue's title, type, status, priority, tags, created/updated timestamps, and description inside a bordered display block

#### Scenario: Show non-existent issue
- **GIVEN** no issue exists with the supplied ID
- **WHEN** the user runs `se3 issue show <id>`
- **THEN** the command prints an error message to stderr and exits with a non-zero exit code

#### Scenario: Create issue interactively
- **WHEN** the user runs `se3 issue create`
- **THEN** the command prompts for title, description, type (default `bug`), priority (default `medium`), and comma-separated tags
- **AND** persists a new issue and prints its assigned ID and title

#### Scenario: Reset in-progress issue
- **GIVEN** an issue currently in `in_progress` status
- **WHEN** the user runs `se3 issue reset <id>`
- **THEN** the issue's status is reset to `open`
- **AND** the command prints a confirmation message

#### Scenario: Reset rejects invalid transitions
- **WHEN** `se3 issue reset <id>` is called against an issue whose current status cannot transition back to `open`
- **THEN** the command prints the underlying error message to stderr and exits with a non-zero exit code

### Requirement: `se3 salvage` Command

The `se3 salvage` command SHALL perform best-effort recovery from an abnormally terminated SE3 session. The pipeline is composed of five independently fault-tolerant steps; the failure of any one step SHALL NOT abort the remaining steps.

**Interface:**
```bash
se3 salvage [--project-root PATH]
```

**Pipeline steps (executed in order):**
1. **Read session state** — tolerantly load the active flow via the persistence manager's `load_flow_tolerant()` path. Corrupted or missing state SHALL NOT raise; warnings are logged and the step records `SKIP` when no session is found.
2. **Assess git diff** — collect `git status --porcelain`, `git diff --stat`, and a truncated `git diff HEAD` (capped at 4000 chars) along with the list of changed files.
3. **Commit changes** — when there are uncommitted changes, `git add -A` and create a salvage commit with message `[salvage] <task_desc>\n\nSalvage commit: <N> files from interrupted session.` (`<task_desc>` is the loaded flow's task description truncated to 80 chars, or `unknown task` when no flow is available). The commit uses `--no-verify`. When nothing is to be committed, the step records `SKIP`.
4. **Create salvage issues** — when a flow was loaded, create issues capturing the incomplete task, the completed-step trail, and the current step at interruption.
5. **Archive session** — move the active session state into the archive area; `SKIP` when no active session exists.

**Project root resolution:** When `--project-root` is omitted, the command walks up from the current working directory looking for `.git` or any SE3 config file (`se3.yaml`, `se3.local.yaml`, `se3.config.yaml`). If no project root can be located, the command prints a red error and exits non-zero.

**Exit code:** `0` when every step finished with `OK` or `SKIP`; `1` when any step recorded a `FAIL` status or no project root could be located.

After salvage, the user is expected to continue work via `se3 run --from-issue`.

#### Scenario: Salvage with uncommitted changes and a loaded flow
- **GIVEN** an interrupted SE3 session whose state is loadable and whose working tree has uncommitted changes
- **WHEN** the user runs `se3 salvage`
- **THEN** the read-session step records `OK` with the loaded flow ID
- **AND** the assess-git-diff step records the changed file count
- **AND** the commit-changes step stages all changes, writes a `[salvage] <task_desc>` commit using `--no-verify`, and records the resulting commit hash
- **AND** the create-issues step creates one or more issues describing the incomplete task and the completed-step trail
- **AND** the archive-session step archives the active session
- **AND** the command exits with code 0

#### Scenario: Salvage with no session and no uncommitted changes
- **GIVEN** a project with no active session and a clean working tree
- **WHEN** the user runs `se3 salvage`
- **THEN** the read-session step records `SKIP`
- **AND** the commit-changes step records `SKIP` with the reason `Nothing to commit`
- **AND** the create-issues step records `SKIP`
- **AND** the archive-session step records `SKIP`
- **AND** the command exits with code 0

#### Scenario: Salvage step failures are isolated
- **GIVEN** a project where one pipeline step (e.g., create-issues) raises an exception
- **WHEN** the user runs `se3 salvage`
- **THEN** the failing step is recorded as `FAIL` with a truncated error detail
- **AND** all remaining steps still execute in order
- **AND** the command exits with code 1 because at least one step failed

#### Scenario: Salvage cannot find a project root
- **GIVEN** the current working directory is not inside a project containing `.git`, `se3.yaml`, `se3.local.yaml`, or `se3.config.yaml`
- **WHEN** the user runs `se3 salvage` without `--project-root`
- **THEN** the command prints an error indicating no project root was found
- **AND** exits with code 1 without executing any pipeline step

## Command Summary

| Command | Purpose | Status |
|---------|---------|--------|
| `se3 run` | Unified workflow entry point | **Required** |
| `se3 init` | Initialize SE3 project structure | **Required** |
| `se3 guardrails` | Check spec against guardrails | **Required** |
| `se3 history` | View and manage flow history | **Required** |
| `se3 issue` | Manage SE3 project issues (list/show/create/reset) | **Required** |
| `se3 sync` | Check and synchronize specs with project code | **Required** |
| `se3 sync-respond` | Process MCP call response for sync conflicts | **Required** |
| `se3 merge` | Sequentially merge one or more branches into current with LLM-assisted conflict resolution | **Required** |
| `se3 merge-respond` | Process MCP call response for merge conflicts | **Required** |
| `se3 salvage` | Best-effort recovery from an abnormally terminated session | **Required** |

### Requirement: Loop Mode CLI Options

The system SHALL provide the following CLI options for loop mode:

| Option | Description |
|--------|-------------|
| `--loop, -l` | Enable loop mode (continuous task execution) |
| `--max-iterations, -n` | Maximum iterations for loop mode (default: 10) |
| `--no-worktree` | Disable branch isolation in loop mode |
| `--merge BRANCH` | Merge an existing loop branch (shows diff summary, prompts confirmation) |
| `--list-loops` | List existing unmerged loop branches with commit counts |

## Error Codes

| Code | Description |
|------|-------------|
| 0 | Success |
| 1 | General error / Guardrails violation |
| 130 | Interrupted by user (Ctrl+C) |
