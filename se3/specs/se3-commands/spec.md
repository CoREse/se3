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

# Loop mode (continuous execution)
se3 run --loop

# Specify task type
se3 run "Fix bug" --type=bugfix

# Discovery mode
se3 run --discover "I want to build..."
```

**Task Types:**
| Type | Description | Steps |
|------|-------------|-------|
| `feature` | New functionality | Full 10-step workflow |
| `bugfix` | Fixing a bug | Skip update_spec step |
| `review` | Code review/analysis | analyze → read_spec → verify_spec → summarize |
| `small` | Minor fix/typo | analyze → implement → test → commit → summarize |
| `directive` | Following specific instructions | analyze → read_spec → plan → implement → commit → summarize |

#### Scenario: New task execution
- **WHEN** user executes `se3 run "Implement user authentication"`
- **THEN** the flow engine creates a new flow instance
- **AND** starts execution from the analyze step

#### Scenario: Resume interrupted flow
- **WHEN** user executes `se3 run --resume` with an active flow
- **THEN** the flow engine loads the persisted state
- **AND** continues execution from the interrupted step

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
- **AND** after LLM confirms requirements are clear, presents a numbered choice to the user:
  1. Confirm and proceed to implementation planning
  2. Continue discovery with more questions
- **AND** only proceeds to analyze when the user explicitly selects option 1

### Requirement: `se3 init` Command

The `se3 init` command SHALL initialize a new SE3 project with the standard directory structure.

**Interface:**
```bash
se3 init [--project-root PATH] [--name PROJECT_NAME] [--force]
```

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

### Requirement: `se3 guardrails` Command

The `se3 guardrails` command SHALL check spec files against SE3 Spec Guardrails.

**Interface:**
```bash
se3 guardrails <spec-file> [--original <original-file>]
```

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
se3 history archived                 # List only archived flows
```

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
- **THEN** displays detailed step-by-step breakdown of the flow

#### Scenario: Show flow details with LLM call details
- **GIVEN** a valid flow_id with chat history
- **WHEN** user runs `se3 history show <flow_id> --detailed`
- **THEN** displays flow metadata and step table as usual
- **AND** appends each step's LLM call details: prompt is shown as structured segments (auto-detected sections such as JSON Mode Instruction, Step Instructions, Available Specifications, Discovery Context, Read-Only Constraint, Language Instruction, Additional User Instruction, etc.) using Rich Panels with labeled titles
- **AND** prompt segments containing embedded spec content are folded into compact reference annotations for readability:
  - Segments titled "Relevant Specifications" or "Specifications (for context only)" fold each `### spec-name` subsection into `[spec] @spec-name  (折叠, size)` with `bold magenta` Rich styling on `@spec-name`
  - Segments titled "Base Specification" fold the entire body into `[spec] @base  (折叠, size)`
  - Segments that only list spec names (e.g., "Available Specifications") are NOT folded
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

#### Scenario: Restore a flow
- **WHEN** user runs `se3 history restore <flow_id>`
- **THEN** delegates to `se3 run --resume --flow-id <flow_id>`

### Requirement: `se3 sync` Command

The `se3 sync` command SHALL check and synchronize `se3/specs/` with project code, identifying gaps, extensions, and conflicts between specifications and the actual codebase.

**Interface:**
```bash
se3 sync                    # Sync with default mode
se3 sync --mode=default     # Same as above (explicit)
se3 sync --mode=strict      # All conflicts require human decision
se3 sync --mode=fast        # LLM handles all conflicts automatically
```

**Mode Parameter:**
| Mode | Behavior |
|------|----------|
| `default` | LLM auto-resolves high-confidence conflicts; low-confidence conflicts are batched into an MCP call file for human decision |
| `strict` | All conflicts are batched into a single MCP call file for human decision |
| `fast` | LLM fully auto-resolves all conflicts; no human intervention |

#### Scenario: Sync with existing base spec
- **GIVEN** the project has a base spec at `se3/specs/base/`
- **WHEN** user runs `se3 sync`
- **THEN** the engine loads all specs starting from base
- **AND** performs LLM-driven comparison of each spec against project code
- **AND** classifies each difference as gap, extension, or conflict

#### Scenario: Sync without base spec (SE3 bootstrapping)
- **GIVEN** the project has no `se3/specs/base/` directory
- **WHEN** user runs `se3 sync`
- **THEN** the engine first explores the project codebase and generates a base spec
- **AND** then proceeds with the iterative sync flow

#### Scenario: Gap detected (spec leads code)
- **WHEN** a spec describes a requirement that is NOT implemented in the code
- **THEN** the engine creates an issue tagged `["auto-discovered", "source:sync"]`
- **AND** the issue title follows the format `[sync] {spec_name}: {description}`
- **AND** idempotency uses normalized matching: titles are normalized by extracting the description portion, lowercasing, removing articles (a/an/the), stripping punctuation, and collapsing whitespace before comparison
- **AND** if a normalized-matching open issue already exists, it is NOT created (idempotency)

#### Scenario: Extension detected (code extends spec)
- **WHEN** the code contains functionality that the spec does NOT describe, with no contradiction
- **THEN** the engine uses LLM to update the spec file to reflect the code's actual behavior
- **AND** the update preserves all existing requirements (add-only)
- **AND** a content length safety guard rejects suspiciously short LLM outputs (< 50% of original)
- **AND** markdown code fences wrapping the LLM response are stripped before writing to spec files

#### Scenario: Conflict detected in default mode
- **WHEN** the code implements something differently from what the spec describes
- **AND** mode is `default`
- **THEN** high-confidence conflicts are auto-resolved by LLM (update spec or create issue)
- **AND** low-confidence conflicts are collected for human decision

#### Scenario: Conflict detected in strict mode
- **WHEN** a conflict is detected and mode is `strict`
- **THEN** all conflicts are batched into a single MCP call file in `se3/calls/`
- **AND** flow pauses, awaiting human input

#### Scenario: Conflict detected in fast mode
- **WHEN** a conflict is detected and mode is `fast`
- **THEN** LLM auto-resolves every conflict (deciding update_spec or create_issue)
- **AND** no human intervention is triggered

#### Scenario: Conflict spec update safety guards
- **WHEN** a conflict is resolved by updating the spec (via LLM)
- **THEN** a content length safety guard rejects LLM outputs shorter than 50% of the original spec content
- **AND** markdown code fences wrapping the LLM response are stripped before writing to spec files

#### Scenario: Issue lifecycle — auto-close resolved gaps
- **WHEN** sync detects that a previously reported gap is no longer present
- **THEN** the corresponding sync-tagged issue is automatically closed using a three-layer matching strategy:
  1. **Normalized match**: the issue title is normalized and compared against current gap titles
  2. **Prefix fallback**: if normalized match fails but the issue's spec still has gaps, the issue is conservatively kept open
  3. **Close**: only when neither condition holds is the issue closed
- **AND** the close reason indicates the gap was resolved
- **AND** only gap issues are processed (conflict issues have their own lifecycle)

#### Scenario: MCP call file generation for human intervention
- **WHEN** conflicts require human decision (default or strict mode)
- **THEN** all pending conflicts are written to a single JSON file in `se3/calls/`
- **AND** the file includes conflict ID, spec name, description, code location, spec content (truncated to 2000 chars), and decision options
- **AND** the CLI displays the call file path and the `se3 sync-respond` command to process it

### Requirement: `se3 sync-respond` Command

The `se3 sync-respond` command SHALL process an MCP call response file for sync conflicts.

**Interface:**
```bash
se3 sync-respond <call-file-path>
```

#### Scenario: Process call response
- **GIVEN** an MCP call file has been created by `se3 sync`
- **AND** the user has filled in the `.response` file with decisions for each conflict
- **WHEN** user runs `se3 sync-respond <call-file-path>`
- **THEN** the engine reads each conflict decision from the response file
- **AND** for `update_spec` decisions: uses LLM to update the spec to match the code
- **AND** for `create_issue` decisions: creates an issue recording the discrepancy
- **AND** responses with invalid decision values (not `update_spec` or `create_issue`) are skipped
- **AND** responses referencing unknown conflict IDs (not present in the original call file) are skipped

### Requirement: Sync Operation Permission Limits

`se3 sync` SHALL only directly modify spec files (`se3/specs/`) and issue files (`se3/issues/`). All situations requiring code changes SHALL be recorded as issues, never applied directly to project source code.

## Command Summary

| Command | Purpose | Status |
|---------|---------|--------|
| `se3 run` | Unified workflow entry point | **Required** |
| `se3 init` | Initialize SE3 project structure | **Required** |
| `se3 guardrails` | Check spec against guardrails | **Required** |
| `se3 history` | View and manage flow history | **Required** |
| `se3 sync` | Check and synchronize specs with project code | **Required** |
| `se3 sync-respond` | Process MCP call response for sync conflicts | **Required** |

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
