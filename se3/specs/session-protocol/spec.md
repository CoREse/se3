# session-protocol Specification

## Purpose

Define the session lifecycle protocol for SE3 3.0 agents. This spec governs progressive startup via `se3 run`, execution boundaries, shutdown procedures, and state persistence across sessions through the Flow Engine.

## Requirements

### Requirement: Core Principles

The system SHALL adhere to the following core principles that govern all SE3 operations:

**1. Human-as-MCP**: All human input is obtained on-demand via human calls. No pre-written requirement files.

**2. Progressive Loading**: Start with minimal context. Load deeper only when the task needs it.

**3. Specs as Truth**: SE3 specs (`se3/specs/`) are the single source of truth for requirements. Agents MUST NOT weaken or delete existing requirements without explicit human approval.

**4. Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria, not documentation.

**5. Tool-Assisted Enforcement**: Use CLI tools (`se3 lint`, `se3 verify`, `se3 status`) to validate specs, verify coverage, and diagnose issues.

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

**First-Time Bootstrap:**
- If no `progress.md` and no git history:
  - Ask human: "What should this project do?"
  - Create initial flow from response
  - Create `progress.md`

#### Scenario: New flow startup
- **WHEN** agent runs `se3 run "Implement feature X"`
- **THEN** a new flow is created starting from analyze step

#### Scenario: Resume interrupted flow
- **WHEN** agent runs `se3 run` with interrupted flow existing
- **THEN** agent is prompted to resume or start new
- **AND** resume continues from exact interruption point

#### Scenario: Mature project startup
- **WHEN** agent starts a flow in project with existing progress.md
- **THEN** flow engine loads context automatically
- **AND** executes without requiring manual state loading

### Requirement: Input Classification and Step Routing

The system SHALL classify user input in the analyze step to determine the appropriate workflow.

**Intent Types:**
| Intent Type | Description | Steps Used |
|-------------|-------------|------------|
| `directive` | Explicit self-iterate, "implement X" | analyze → read_spec → plan_tasks → implement → test → verify_spec → commit → summarize |
| `bug-report` | Error description, stack trace, broken behavior | analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → update_spec → commit → summarize |
| `feature-request` | New capability, enhancement idea | Full 11-step workflow |
| `question` | How does X work? Why Y? | Knowledge query (no flow) |
| `review` | "Check this", "What do you think", "Is this correct" | analyze → read_spec → verify_spec → summarize |
| `clarification` | Follow-up on previous topic | Continue from last flow |
| `meta` | About the project/process itself | Meta workflow |
| `off-topic` | Not related to project | Answer without modifying project files |

**Classification Indicators:**
- Bug: "error", "bug", "broken", "fail", "crash", "exception", "stack trace", "not working"
- Review: "review", "check this", "look at", "what do you think", "is this correct"
- Feature: "add ", "implement", "create ", "build ", "support ", "feature", "new capability"
- Question: "how ", "why ", "what is", "explain", "?"
- Directive: "self-iterate", "continue", "proceed", "start ", "fix ", "update ", "refactor "

#### Scenario: Bug report classification
- **WHEN** user input contains bug indicators
- **THEN** system classifies intent as "bug-report"
- **AND** routes to bugfix workflow (skips design step)

#### Scenario: Feature request classification
- **WHEN** user input contains feature indicators
- **THEN** system classifies intent as "feature-request"
- **AND** routes to feature workflow (full 11 steps)

#### Scenario: Review request classification
- **WHEN** user input contains review indicators
- **THEN** system classifies intent as "review"
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
2. Generate handoff summary
3. Update progress.md via `se3 handoff`
4. Mark flow as COMPLETED

**Manual Shutdown (Ctrl+C):**
1. First Ctrl+C: interrupt current step, allow prompt injection
2. Second Ctrl+C: save state and exit
3. Flow can be resumed later with `se3 run --resume`

#### Scenario: Normal flow completion
- **WHEN** flow reaches summarize step
- **THEN** generate handoff summary
- **AND** mark flow as COMPLETED

#### Scenario: Interrupt and resume
- **WHEN** user interrupts with Ctrl+C twice
- **THEN** state is saved
- **AND** flow can be resumed later

### Requirement: Session Commit Cadence

Commits SHOULD occur during a flow when distinct units of work are complete.

**When to Commit:**
- After completing a coherent unit of work that passes tests
- Before starting a substantially different task
- The commit step handles this automatically

**Commit Rules:**
- Flow executes commit step automatically
- Commit messages include context
- progress.md is updated automatically

#### Scenario: Mid-flow commit
- **WHEN** flow reaches commit step
- **THEN** changes are committed automatically
- **AND** progress.md is updated

### Requirement: Progress Tracking

The system SHALL use `progress.md` as the cumulative cross-session progress record.

**Progress Entries:**
- Commit step appends commit entries
- Summarize step appends session summary
- Loop mode appends iteration summaries

**Flow State:**
- Persisted in `se3/state/engine.json`
- Updated after each step completion
- Enables precise resume

#### Scenario: Auto-append commit entry
- **WHEN** commit step succeeds
- **THEN** a record is automatically appended to progress.md

#### Scenario: Finalize flow
- **WHEN** summarize step completes
- **THEN** a formal session record is generated
- **AND** flow is marked COMPLETED

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
   - `specs/_backlog/*.md`
   - `roadmap.md` unchecked items
   - TODO comments in code
3. If task found: create new flow and execute
4. If no tasks: exit loop

**Loop Options:**
- `--max-iterations N`: Limit iterations
- `--task-type TYPE`: Filter task types

#### Scenario: Loop execution
- **WHEN** `se3 run --loop` is executed
- **THEN** tasks are discovered and executed continuously

#### Scenario: Loop with task filter
- **WHEN** `se3 run --loop --task-type=bugfix` is executed
- **THEN** only bugfix tasks are executed
