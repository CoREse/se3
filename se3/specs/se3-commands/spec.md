# se3-commands Specification

## Purpose

Define the command-line interface for SE3 core commands. SE3 3.0 uses `se3 run` as the unified entry point, replacing the traditional start/work/done workflow with a state machine-driven approach.

## Requirements

### Requirement: Unified Entry Point `se3 run`

The system SHALL provide `se3 run` as the primary entry point for all SE3 workflows, replacing the manual start/work/done command chain.

**Interface:**
```bash
# New flow
se3 run "Implement feature X"

# Resume interrupted flow
se3 run --resume

# Loop mode (continuous execution)
se3 run --loop

# Specify task type
se3 run "Fix bug" --type=bugfix
```

**Task Types:**
| Type | Description | Steps |
|------|-------------|-------|
| `feature` | New functionality or significant enhancement | Full 11-step workflow |
| `bugfix` | Fixing a bug or issue | Skip design step |
| `review` | Code review, audit, or analysis | analyze → read_spec → verify_spec → summarize |
| `small` | Minor fix, typo, or simple change | analyze → implement → test → commit → summarize |
| `directive` | Following specific instructions or requirements | analyze → read_spec → plan_tasks → implement → test → verify_spec → commit → summarize |

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
- **THEN** the flow engine continuously finds and executes tasks
- **AND** discovers tasks from backlog, roadmap, or TODOs

### Requirement: Legacy Command Compatibility

The system MAY support legacy commands (`se3:start`, `se3:work`, `se3:done`) for backward compatibility, but they SHALL be deprecated in favor of `se3 run`.

**Deprecation Notice:**
- Legacy commands continue to work but print deprecation warnings
- New projects SHOULD use `se3 run` exclusively
- Documentation prioritizes `se3 run` workflow

#### Scenario: Legacy command execution
- **GIVEN** a user executes a legacy command like `se3:start`
- **WHEN** the command runs
- **THEN** it executes successfully but displays a deprecation warning
- **AND** suggests using `se3 run` instead

#### Scenario: New project uses modern command
- **GIVEN** a new SE3 project
- **WHEN** user runs `se3 run "task description"`
- **THEN** the command executes without deprecation warnings

### Requirement: `se3 status` Command

The `se3 status` command SHALL display the current project and flow state.

**Interface:**
```bash
se3 status [--format json]
```

**Output Fields:**
- `project_root`: Project root directory
- `git`: Git status (branch, uncommitted changes, recent commits)
- `flow`: Active flow information (if any)
  - flow_id
  - status
  - current_step
  - progress (completed/total steps)
- `pending_human_calls`: Pending human calls waiting for response

#### Scenario: Check status with active flow
- **WHEN** `se3 status` runs with an active flow
- **THEN** it displays flow progress and current step

#### Scenario: Check status with no active flow
- **WHEN** `se3 status` runs with no active flow
- **THEN** it displays "No active flow" and suggests `se3 run`

### Requirement: `se3 commit` Command

The `se3 commit` command SHALL be the single entry point for all git commits, enforcing SE3 standards.

**Interface:**
```bash
se3 commit [-m "message"] [-f "file1 file2"] [--skip-tests] [--dry-run] [--no-ai]
```

**Enforced Standards:**
1. Tests must pass before commit (no override without explicit `--skip-tests`)
2. Sensitive files are blocked (.env, credentials, secrets)
3. Commit message follows SE3 conventions
4. Only tracked/specified files are staged

**Sensitive File Patterns (blocked):**
- `.env`, `.env.*`
- `*.pem`, `*.key`, `*.p12`
- `credentials.json`, `secrets.yaml`, `secrets.yml`
- `.secret*`, `*_secret*`, `*.credential*`
- `token.json`, `service-account*.json`

#### Scenario: Normal commit
- **WHEN** `se3 commit -m "message"` is executed
- **THEN** it runs tests, stages files, validates message, and commits
- **AND** appends entry to progress.md

#### Scenario: Test failure
- **WHEN** tests fail during commit
- **THEN** commit is blocked
- **AND** user must fix tests or use `--skip-tests`

### Requirement: `se3 handoff` Command

The `se3 handoff` command SHALL generate a session summary and transfer control to the human.

**Interface:**
```bash
se3 handoff [message] [--project-root <path>] [--dry-run] [--skip-commit]
```

**Process:**
1. Check for uncommitted changes
2. Auto-commit if changes exist (unless `--skip-commit`)
3. Generate session summary in `progress.md`
4. Mark flow as completed

#### Scenario: Normal handoff
- **WHEN** `se3 handoff` is executed
- **THEN** it generates a session summary in `progress.md`
- **AND** marks the current flow as completed

### Requirement: `se3 lint` Command

The `se3 lint` command SHALL validate spec files against SE3 conventions.

**Interface:**
```bash
se3 lint [<path>] [--fix]
```

**Validation Rules:**
1. All spec files must have required sections (Purpose, Requirements)
2. Scenario format must be correct (WHEN/THEN)
3. No duplicate scenario names
4. Requirement IDs must be unique

#### Scenario: Valid specs pass linting
- **GIVEN** all spec files follow SE3 conventions
- **WHEN** user runs `se3 lint`
- **THEN** the command reports success with no errors

#### Scenario: Invalid specs report errors
- **GIVEN** a spec file is missing required sections
- **WHEN** user runs `se3 lint`
- **THEN** the command reports validation errors
- **AND** indicates which files need fixes

### Requirement: `se3 verify` Command

The `se3 verify` command SHALL verify that spec scenarios have corresponding implementation.

**Interface:**
```bash
se3 verify [<spec-path>] [--format json]
```

#### Scenario: Verify implementation matches spec
- **GIVEN** a spec file with defined scenarios
- **WHEN** user runs `se3 verify <spec-path>`
- **THEN** the command checks if all scenarios are implemented
- **AND** reports any missing implementations

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

### Requirement: `se3 health` Command

The `se3 health` command SHALL check SE3 system integrity and report health issues.

**Interface:**
```bash
se3 health [--format json] [--stale-days <n>]
```

**Health Checks:**
- Zombie flows (no activity for extended periods)
- Stale flows (no activity for specified days)
- Directory structure drift
- Orphaned state files

#### Scenario: Check system health
- **GIVEN** an SE3 project with active flows
- **WHEN** user runs `se3 health`
- **THEN** the command checks for zombie/stale flows
- **AND** reports any integrity issues found

### Requirement: `se3 init` Command

The `se3 init` command SHALL initialize a new SE3 project.

**Interface:**
```bash
se3 init [--force]
```

**Process:**
1. Create `.claude/` directory
2. Create `CLAUDE.md` with project-specific content
3. Create `se3.yaml` configuration file (if not exists)
4. Create `se3/` directory structure

#### Scenario: Initialize new project
- **GIVEN** a directory without SE3 configuration
- **WHEN** user runs `se3 init`
- **THEN** the command creates `.claude/` directory
- **AND** creates `CLAUDE.md` and `se3.yaml` files
- **AND** sets up `se3/` directory structure

### Requirement: `se3 migrate` Command

The `se3 migrate` command SHALL migrate legacy directory structures to the current format.

**Interface:**
```bash
se3 migrate [--dry-run] [--force]
```

**Migration Steps:**
1. Detect legacy directories (`human-cells/`, `.collab/`)
2. Create new `se3/` structure
3. Move existing data preserving structure
4. Update `.gitignore`

#### Scenario: Migrate legacy project
- **GIVEN** a project with legacy SE3 structure
- **WHEN** user runs `se3 migrate`
- **THEN** the command detects legacy directories
- **AND** migrates data to new `se3/` structure
- **AND** updates `.gitignore` accordingly

### Requirement: `se3 history` Command

The `se3 history` command SHALL provide human-readable browsing of LLM chat history recorded by the flow engine.

**Interface:**
```bash
# List all flows with history
se3 history

# Show all step conversations for a flow
se3 history <flow_id>

# Show detailed conversation for a specific step
se3 history <flow_id> <step_type>

# JSON output
se3 history <flow_id> --format json
```

**Rendering Rules:**
- `role=user`: Displayed as `[User Prompt]` + prompt text (truncatable)
- `role=assistant`: Parsed from raw NDJSON:
  - Text content: displayed directly
  - Tool calls: shown as `[Tool Call: {name}]` + parameter summary
  - Tool results: shown as `[Tool Result]` + content summary
- LLM output JSON (e.g. analyze results): shown as-is (it is content, not protocol)

#### Scenario: List all flows
- **WHEN** user executes `se3 history` with existing history
- **THEN** it lists all flow IDs with step count and message count

#### Scenario: View flow details
- **WHEN** user executes `se3 history <flow_id>`
- **THEN** it shows all step conversations for that flow
- **AND** renders assistant responses from NDJSON as human-readable text

#### Scenario: No history exists
- **WHEN** user executes `se3 history` with no recorded history
- **THEN** it displays "No chat history found" with guidance

## Command Categories

| Category | Commands | Purpose |
|----------|----------|---------|
| **Core** | `se3 run` | Unified workflow entry point |
| **Status** | `se3 status`, `se3 history` | Project and flow state |
| **Quality** | `se3 lint`, `se3 verify`, `se3 guardrails` | Validation and verification |
| **Maintenance** | `se3 health`, `se3 migrate`, `se3 init` | Framework maintenance |
| **Session** | `se3 commit`, `se3 handoff` | Session lifecycle |

## Legacy Command Deprecation

The following commands are deprecated and mapped to `se3 run`:

| Legacy | Modern Equivalent |
|--------|-------------------|
| `se3:start` | `se3 run` (auto-detects new project) |
| `se3:work` | `se3 run "task description"` |
| `se3:done` | `se3 handoff` |
| `se3:fc` | `se3 run "task" --type=small` |

## Error Codes

| Code | Description |
|------|-------------|
| 0 | Success |
| 1 | General error |
| 130 | Interrupted by user (Ctrl+C) |
