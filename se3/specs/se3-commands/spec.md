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
- **AND** proceeds to analyze after user confirmation

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

#### Scenario: Restore a flow
- **WHEN** user runs `se3 history restore <flow_id>`
- **THEN** delegates to `se3 run --resume --flow-id <flow_id>`

## Command Summary

| Command | Purpose | Status |
|---------|---------|--------|
| `se3 run` | Unified workflow entry point | **Required** |
| `se3 init` | Initialize SE3 project structure | **Required** |
| `se3 guardrails` | Check spec against guardrails | **Required** |
| `se3 history` | View and manage flow history | **Required** |

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
