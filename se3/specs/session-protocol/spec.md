# session-protocol Specification

## Purpose

Define the session lifecycle protocol for SE3 3.0 agents. This spec governs progressive startup via `se3 run`, execution boundaries, shutdown procedures, and state persistence across sessions through the Flow Engine.

## Requirements

### Requirement: Core Principles

The system SHALL adhere to the following core principles that govern all SE3 operations:

**1. Human-as-MCP**: All human input is obtained on-demand via conversation. No pre-written requirement files.

**2. Progressive Loading**: Start with minimal context. Load deeper only when the task needs it.

**3. Specs as Truth**: SE3 specs (`se3/specs/`) are the single source of truth for requirements. Agents MUST NOT weaken or delete existing requirements without explicit human approval.

**4. Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria, not documentation.

**5. Tool-Assisted Enforcement**: Use `se3 guardrails` to validate spec integrity.

**6. State Machine Driven**: Flow is controlled programmatically, not by LLM decisions.

**7. Interruptible & Resumable**: Any flow can be interrupted and resumed from the exact step.

#### Scenario: Agent follows core principles
- **WHEN** an agent is working on a flow
- **THEN** the agent follows all seven core principles throughout the session

#### Scenario: Spec violation detected
- **WHEN** an agent attempts to modify a spec they are implementing against
- **THEN** the system blocks the change and reports a guardrail violation

### Requirement: Session Startup Protocol

The system SHALL define a unified session startup protocol via `se3 run`.

**Startup Flow:**
1. User executes `se3 run [task_description]`
2. Flow Engine checks for existing active flow
3. If active flow exists:
   - Prompt user to resume or start new
   - If resume: load persisted state and continue
   - If new: create new flow instance
4. If no active flow:
   - Create new flow instance with task description
   - Start from analyze step

**Discovery Mode:**
- If `--discover` flag is used, start with discovery step
- Explore requirements through multi-turn conversation
- Proceed to analyze after user confirms refined description

#### Scenario: New flow startup
- **WHEN** agent runs `se3 run "Implement feature X"`
- **THEN** a new flow is created starting from analyze step

#### Scenario: Discovery mode startup
- **WHEN** agent runs `se3 run --discover "Idea"`
- **THEN** flow starts with discovery step
- **AND** explores requirements through conversation

#### Scenario: Resume interrupted flow
- **WHEN** agent runs `se3 run` with interrupted flow existing
- **THEN** agent is prompted to resume or start new
- **AND** resume continues from exact interruption point

### Requirement: Input Classification and Step Routing

The system SHALL classify user input in the analyze step to determine the appropriate workflow.

**Task Types:**
| Task Type | Description | Steps Used |
|-----------|-------------|------------|
| `feature` | New functionality or significant enhancement | Full 11-step workflow |
| `bugfix` | Fixing a bug or issue | analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → update_spec → commit → summarize |
| `review` | Code review, audit, or analysis | analyze → read_spec → verify_spec → summarize |
| `small` | Minor fix, typo, or simple change | analyze → implement → test → commit → summarize |
| `directive` | Following specific instructions | analyze → read_spec → plan_tasks → implement → test → verify_spec → commit → summarize |

**Classification Indicators:**
- Bugfix: "error", "bug", "broken", "fail", "crash", "exception", "not working"
- Review: "review", "check this", "look at", "what do you think", "is this correct"
- Feature: "add ", "implement", "create ", "build ", "support ", "feature"
- Small: "typo", "fix typo", "minor", "small change"
- Directive: "refactor", "update", "change to"

#### Scenario: Bug fix classification
- **WHEN** user input contains bug indicators
- **THEN** system classifies task type as "bugfix"
- **AND** routes to bugfix workflow (skips design step)

#### Scenario: Feature classification
- **WHEN** user input contains feature indicators
- **THEN** system classifies task type as "feature"
- **AND** routes to feature workflow (full 11 steps)

#### Scenario: Review classification
- **WHEN** user input contains review indicators
- **THEN** system classifies task type as "review"
- **AND** routes to review workflow (minimal steps)

### Requirement: Session Execution Boundary

Each flow MUST focus on a limited scope of work and MUST NOT attempt to complete too many tasks in a single flow.

**Scope Guidelines:**
- A flow should complete in a reasonable number of steps
- Complex work should be broken into multiple flows
- Loop mode (`se3 run --loop`) handles multiple related tasks

#### Scenario: Flow scope limitation
- **WHEN** the flow has determined work scope through analyze step
- **THEN** the flow only executes tasks within that scope

### Requirement: Session Shutdown Protocol

Session ending MUST leave code in a mergeable state.

**Shutdown Flow:**
1. Complete summarize step
2. Generate session summary
3. Mark flow as COMPLETED
4. Clean up temporary state (cache files)

**Manual Shutdown (Ctrl+C):**
1. First Ctrl+C: interrupt current step, allow prompt injection
2. Second Ctrl+C: save state and exit
3. Flow can be resumed later with `se3 run --resume`

#### Scenario: Normal flow completion
- **WHEN** flow reaches summarize step
- **THEN** generate session summary
- **AND** mark flow as COMPLETED

#### Scenario: Interrupt and resume
- **WHEN** user interrupts with Ctrl+C twice
- **THEN** state is saved
- **AND** flow can be resumed later

### Requirement: Session Commit Cadence

Commits occur during the commit step when distinct units of work are complete.

**When to Commit:**
- Commit step executes automatically in the workflow
- After completing a coherent unit of work that passes tests
- Version bumping happens during commit step if enabled

**Commit Rules:**
- Commit step stages and commits all changes
- Commit messages include context
- Version is bumped according to task type and bump rules

#### Scenario: Commit step execution
- **WHEN** flow reaches commit step
- **THEN** changes are committed
- **AND** version is bumped if enabled

### Requirement: State Persistence

The system SHALL persist flow state after each step.

**Persistence Details:**
- File location: `se3/state/engine.json`
- Atomic writes (temp file + rename)
- Includes: flow metadata, current step, step history, all outputs

**Recovery:**
- `se3 run --resume` loads persisted state
- Prompts user if multiple active flows
- Continues from exact interruption point

#### Scenario: State persistence
- **WHEN** each step completes
- **THEN** state is automatically saved

#### Scenario: State recovery
- **WHEN** `se3 run --resume` is executed
- **THEN** flow continues from last saved state

### Requirement: Loop Mode

The system SHALL support continuous task execution via `se3 run --loop`.

**Loop Mode Behavior:**
1. Execute current task flow to completion
2. Look for next task from:
   - `se3/specs/_backlog/*.md`
   - TODO comments in code
3. If task found: create new flow and execute
4. If no tasks: exit loop

**Loop Options:**
- `--max-iterations N`: Limit iterations
- `--type TYPE`: Filter task types
- `--no-worktree`: Disable branch isolation (run on current branch)
- `--merge BRANCH`: Merge an existing loop branch

#### Scenario: Loop execution
- **WHEN** `se3 run --loop` is executed
- **THEN** tasks are discovered and executed continuously

#### Scenario: Loop with task filter
- **WHEN** `se3 run --loop --type=bugfix` is executed
- **THEN** only bugfix tasks are executed

### Requirement: Loop Mode Branch Isolation

Loop mode SHALL use git worktree-based branch isolation by default.

**Worktree Lifecycle:**
1. Before loop: create `se3-loop/{timestamp}` branch from HEAD, create worktree at `se3/worktrees/{branch_safe_name}`
2. During loop: all task flows execute in the worktree (worktree path passed as `project_root` to `run_flow()`)
3. Task discovery (`find_next_task()`) uses the original project root
4. After loop: prompt user — merge now / merge later / discard

**FlowInstance Fields:**
- `loop_branch`: The `se3-loop/{timestamp}` branch name
- `loop_worktree_path`: Path to the active worktree directory
- `loop_original_branch`: Branch to merge back to

**Interrupt Behavior:**
- On Ctrl-C during loop: remove worktree, preserve branch
- Print instructions for deferred merge: `se3 run --loop --merge {branch}`
- User can discard with `git branch -D {branch}`

**Deferred Merge:**
- `se3 run --loop --merge se3-loop/{timestamp}` merges the specified branch into current branch
- On merge conflict: abort merge, report error, user resolves manually

**Resume Semantics:**
- On `se3 run --resume` with a loop flow, detect stale worktrees
- If worktree path exists but is stale, clean up before resuming

#### Scenario: Worktree isolation
- **WHEN** `se3 run --loop` is executed without `--no-worktree`
- **THEN** a `se3-loop/{timestamp}` branch is created
- **AND** a git worktree is set up at `se3/worktrees/`
- **AND** tasks execute in the isolated worktree

#### Scenario: Loop interrupt cleanup
- **WHEN** user presses Ctrl-C during loop mode
- **THEN** worktree is removed
- **AND** loop branch is preserved for later merge
- **AND** instructions are printed for deferred merge

#### Scenario: Deferred merge
- **WHEN** `se3 run --loop --merge se3-loop/20260324-120000` is executed
- **THEN** the specified branch is merged into current branch
- **AND** success or conflict is reported
