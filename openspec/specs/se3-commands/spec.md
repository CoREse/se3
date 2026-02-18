# se3-commands Specification

## Purpose

Define the command-line interface for SE3 core commands: `se3:start`, `se3:work`, `se3:done`, `se3:fc`, `se3 commit`, and `se3 update`. These commands form the primary interface between agents and the SE3 framework.

## Requirements

### Requirement: Command Interface Overview

The system SHALL provide a unified command interface for session management, work execution, and framework maintenance.

**Command Categories:**

| Category | Commands | Purpose |
|----------|----------|---------|
| Session | `se3:start`, `se3:done` | Session lifecycle |
| Work | `se3:work`, `se3:fc` | Task execution |
| Utility | `se3 commit`, `se3 update` | Framework operations |

#### Scenario: Command discovery
- **WHEN** an agent needs to understand available SE3 commands
- **THEN** the agent can read the command specifications in `.claude/commands/se3/`
- **AND** the specifications define the JSON action interface

### Requirement: se3:start Command

The `se3:start` command SHALL initialize a new SE3 work session with environment setup, context loading, and action generation.

**Interface:**
```bash
se3 start --format json
```

**JSON Response Fields:**
- `first_time`: Whether this is a new project
- `env_setup`: Whether init.sh needs to run
- `openspec`: Whether openspec is available/initialized
- `git`: Branch, uncommitted changes, recent commits
- `active_changes`: List of active openspec changes
- `pending_human_calls`: Human responses waiting to be processed
- `actions`: Array of actions to execute

**Action Types:**
- `ask_user`: Use AskUserQuestion tool with provided question
- `run_script`: Execute the command (e.g., `bash init.sh`)
- `init_openspec`: Run `openspec init --tools claude`
- `run_tests`: Run tests to establish baseline
- `process_human_call`: Read specified file in `human-calls/` and act on response
- `create_progress`: Create `progress.md` file
- `create_human_calls_dir`: Create `human-calls/` directory

#### Scenario: First-time project startup
- **WHEN** `se3:start` runs in a new project with no progress.md
- **THEN** it returns `first_time: true` and actions to bootstrap the project
- **AND** includes an `ask_user` action to determine project direction

#### Scenario: Mature project startup
- **WHEN** `se3:start` runs in a project with existing progress.md
- **THEN** it loads current state and returns actions to continue work
- **AND** includes any pending human calls that need processing

### Requirement: se3:work Command

The `se3:work` command SHALL manage work execution through workflow types with JSON action interfaces.

**Interface:**
```bash
# List active changes
se3 work --format json

# Continue specific change
se3 work <change-name> --format json

# Create new change
se3 work --new <type>/<kebab-name> --format json
```

**JSON Response Fields:**
- `change`: The change name
- `workflow`: Workflow type (bugfix/feature/review/directive/small)
- `current_step`: Current workflow step
- `steps`: All steps with their status
- `tasks`: List of tasks with done/not-done status
- `progress`: Task completion statistics
- `actions`: Array of actions to execute

**Action Types:**
- `ask_user`: Ask clarifying questions
- `create_change`: Run `se3 work --new <type>/<name>`
- `write_proposal`: Create `proposal.md` in change directory
- `write_spec`: Create/update specs in `openspec/specs/`
- `write_design`: Create `design.md`
- `write_tasks`: Create `tasks.md` breaking work into max 5 tasks
- `analyze_bug`: Reproduce, identify root cause, report findings
- `inspect_code`: Read and review code/files
- `report_review`: Present findings categorized as critical/warning/suggestion
- `implement_task`: Implement specified task, mark `- [x]` in tasks.md
- `implement`: Direct code implementation (small changes)
- `run_tests`: Run test suite, report results
- `run_lint`: Run `se3 lint` to validate specs
- `verify_scenarios`: Check all spec scenarios pass
- `archive_change`: Run `openspec archive <name>`
- `skip_step`: Skip a step (e.g., design for simple changes)
- `advance_step`: Trigger workflow step advancement
- `complete`: All steps done

#### Scenario: Continue existing change
- **WHEN** `se3 work <change-name> --format json` is executed
- **THEN** it returns the current workflow state and next actions
- **AND** the agent executes actions until `complete` or blocked

#### Scenario: Create new change
- **WHEN** `se3 work --new feature/my-feature --format json` is executed
- **THEN** it creates the change directory and returns initial actions
- **AND** includes `write_proposal` action for the first step

### Requirement: se3:done Command

The `se3:done` command SHALL end the current session with proper shutdown protocol.

**Interface:**
```bash
se3 done --format json
```

**JSON Response Fields:**
- `uncommitted_changes`: Count and list of uncommitted files
- `active_changes`: List of incomplete changes with remaining work
- `test_command`: Detected test command (if any)
- `actions`: Array of actions to execute

**Action Types:**
- `run_tests`: Run the test suite
- `commit`: Run `se3 commit` to commit uncommitted changes
- `update_change_status`: Note remaining work in change directory
- `create_human_call`: (Collab mode) Create human-call for orchestrator
- `handoff`: Run `se3 handoff` to generate session summary
- `archive_change`: Run `openspec archive <name>` to archive completed change
- `verify_scenarios`: Check that all spec scenarios pass
- `check_spec_drift`: Check if specs were inappropriately weakened during implementation

#### Scenario: Normal session shutdown
- **WHEN** `se3:done` runs with uncommitted changes
- **THEN** it returns actions to run tests, commit, and handoff
- **AND** blocks if tests fail

#### Scenario: Session with incomplete changes
- **WHEN** `se3:done` runs with active incomplete changes
- **THEN** it includes `update_change_status` action to document remaining work
- **AND** the handoff summary includes notes for next session

### Requirement: se3:fc Command

The `se3:fc` (full-cycle) command SHALL run complete start-work-done workflow in one command for simple/quick tasks.

**Interface:**
```bash
se3 full-cycle "description of work" [--quick] --json
```

**JSON Response Fields:**
- `phases.start`: Session initialization results
- `phases.work`: Change creation details
- `phases.implementation.actions`: Actions to execute
- `phases.done`: Completion check results
- `actions`: Complete action sequence

**Action Types:**
- `implement`: Implement the requested change
- `run_tests`: Run tests to verify implementation
- `commit`: Commit changes via `se3 commit`
- `handoff`: Complete session via `se3 handoff`

#### Scenario: Quick task execution
- **WHEN** `se3:fc "fix typo" --quick` is executed
- **THEN** it runs the full workflow without creating formal change
- **AND** completes in a single session

#### Scenario: Complex task detection
- **WHEN** `se3:fc` is used for a complex task requiring design/specs
- **THEN** it should recommend using separate `/se3:start` and `/se3:work` instead

### Requirement: se3 commit Command

The `se3 commit` command SHALL be the single entry point for all git commits, enforcing SE3 standards.

**Interface:**
```bash
se3 commit [-m "message"] [-f "file1 file2"] [--skip-tests] [--dry-run]
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

**Message Quality Thresholds:**
- Minimum length: 20 characters
- Should include `Status:` line describing where things stand
- Should include `Next:` line for what next session should do

**Process Steps:**
1. Check for changes
2. Run tests (blocked if fail)
3. Stage files and check sensitive patterns
4. Check version consistency (for framework changes)
5. Generate/validate commit message
6. Execute commit
7. Auto-append progress entry to progress.md

#### Scenario: Normal commit
- **WHEN** `se3 commit -m "message"` is executed
- **THEN** it runs tests, stages files, validates message, and commits
- **AND** appends entry to progress.md

#### Scenario: Sensitive file blocked
- **WHEN** commit includes files matching sensitive patterns
- **THEN** those files are auto-unstaged
- **AND** commit proceeds with remaining files (if any)

#### Scenario: Test failure
- **WHEN** tests fail during commit
- **THEN** commit is blocked
- **AND** user must fix tests or use `--skip-tests`

### Requirement: se3 update Command

The `se3 update` command SHALL update an existing SE3 project to the latest framework version.

**Interface:**
```bash
se3 update [--dry-run] [--force] [--se3-version X.Y.Z]
```

**Update Process:**
1. Get current framework version from `tools/se3_tools/__init__.py`
2. Get installed version from `.claude/SE3.md` metadata
3. If versions differ or `--force`:
   - Update `.claude/SE3.md` from `output/SE3.md.template`
   - Add metadata (date, version, checksum)
   - Sync commands from `output/commands/se3/` to `.claude/commands/se3/`

**Checksum Computation:**
- SHA-256 of template content
- Stored in metadata comment: `<!-- Checksum: <hash> -->`

#### Scenario: Normal update
- **WHEN** `se3 update` runs and versions differ
- **THEN** it updates SE3.md and syncs commands
- **AND** reports what was updated

#### Scenario: Already up to date
- **WHEN** `se3 update` runs and versions match
- **THEN** it reports "Already on SE3 version X.Y.Z"
- **AND** suggests using `--force` to update anyway

### Requirement: se3 work guardrails Command

The `se3 work guardrails` command SHALL check spec files against SE3 Spec Guardrails to verify requirements were not inappropriately weakened or deleted.

**Interface:**
```bash
se3 work guardrails <spec-file> [--original <original-file>]
```

**Guardrail Checks:**
1. **must_not_delete**: Detect deleted WHEN/THEN scenarios
2. **must_not_weaken**: Detect weakened language (SHALL → SHOULD, MUST → SHOULD, all → some)

**Violation Detection:**
- Compare original and modified spec content
- Check for deleted scenarios (missing WHEN clauses)
- Check for weakened language patterns

#### Scenario: Guardrails pass
- **WHEN** `se3 work guardrails openspec/specs/auth/spec.md` is executed
- **AND** no requirements were deleted or weakened
- **THEN** it reports "✓ All guardrails passed"

#### Scenario: Guardrails violation detected
- **WHEN** a spec has deleted scenarios or weakened requirements
- **THEN** it reports violations with type, message, and affected guardrail
- **AND** exits with code 1

### Requirement: Session Guard

All session-aware commands (`se3:work`, `se3:done`) SHALL check if session is properly started before proceeding.

**Session Guard Checks:**
- If `.claude/.session.json` does not exist → error `SESSION_NOT_STARTED`
- If session status is not "active" → error `SESSION_NOT_ACTIVE`
- If session file is corrupted → error `SESSION_INVALID`

**Resolution:**
- In all error cases, agent should run `se3 start` first

#### Scenario: Work without session
- **WHEN** `se3 work` runs without `.claude/.session.json`
- **THEN** it returns `SESSION_NOT_STARTED` error
- **AND** agent runs `se3 start` to initialize

#### Scenario: Done without active session
- **WHEN** `se3 done` runs with non-active session
- **THEN** it returns `SESSION_NOT_ACTIVE` error
- **AND** agent must start a session first

### Requirement: Input Classification

The `se3:start` command SHALL classify user input to determine appropriate workflow routing.

**Intent Types:**

| Intent Type | Description | Stage Entry |
|-------------|-------------|-------------|
| `directive` | Explicit self-iterate, "implement X" | Full SDD workflow |
| `bug-report` | Error description, stack trace | Bug fix workflow |
| `feature-request` | New capability, enhancement | Feature proposal workflow |
| `question` | How does X work? Why Y? | Knowledge query |
| `review` | "Check this", "What do you think" | Review workflow |
| `clarification` | Follow-up on previous topic | Resume/continue workflow |
| `meta` | About the project/process | Meta workflow |
| `off-topic` | Not related to project | Answer without modifying files |

**Classification Indicators:**
- Bug: "error", "bug", "broken", "fail", "crash", "exception", "stack trace", "not working"
- Review: "review", "check this", "look at", "what do you think", "is this correct"
- Feature: "add ", "implement", "create ", "build ", "support ", "feature", "new capability"
- Question: "how ", "why ", "what is", "explain", "?"
- Directive: "self-iterate", "continue", "proceed", "start ", "fix ", "update ", "refactor "

#### Scenario: Bug report classification
- **WHEN** input contains "error" or "broken"
- **THEN** system classifies as "bug-report"
- **AND** routes to bugfix workflow

#### Scenario: Feature request classification
- **WHEN** input contains "add" or "implement"
- **THEN** system classifies as "feature-request"
- **AND** routes to feature workflow

### Requirement: Workflow Type Mapping

Commands SHALL support five workflow types with appropriate step sequences.

| Type | Steps | When Used |
|------|-------|-----------|
| `bugfix` | analyze → fix → verify | Bug reports |
| `feature` | clarify → propose → spec → design → implement → verify | Feature requests |
| `review` | inspect → report → fix | Code review requests |
| `directive` | plan → implement → verify → check_coverage | "Implement X" commands |
| `small` | implement → verify | Simple changes, no openspec needed |

#### Scenario: Bugfix workflow
- **WHEN** workflow type is "bugfix"
- **THEN** steps are: ANALYZE → FIX → VERIFY
- **AND** includes bug analysis and root cause identification

#### Scenario: Feature workflow
- **WHEN** workflow type is "feature"
- **THEN** steps include: CLARIFY → PROPOSE → SPEC → DESIGN → IMPLEMENT → VERIFY
- **AND** design step is skipped for medium complexity

### Requirement: Commit Cadence

The command interface SHALL support mid-session commits when distinct units of work are complete.

**When to Commit:**
- After completing a coherent unit of work that passes tests
- Before starting a substantially different task
- Before context clearing if there are completed changes

**Commit Command:**
```bash
se3 commit -m "[context] Summary of what changed

Status: where things stand
Next: what the next session should do" -f "file1.py file2.py"
```

#### Scenario: Mid-session commit
- **WHEN** a task is completed and tested
- **THEN** agent runs `se3 commit` before next task
- **AND** message includes Status and Next context

#### Scenario: Final commit before handoff
- **WHEN** session is ending with `se3:done`
- **THEN** all uncommitted changes are committed
- **AND** `se3 handoff` generates session summary
